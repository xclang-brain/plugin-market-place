/**
 * @file    main.c
 * @brief   EM-4 ファームウェアの本体（初期化、周期処理のタスク、上りリンク、コンソール）
 *
 * ホストでの使い方
 *   em4fw [--config em4.ini] [--flash em4flash.bin] [--logdir DIR]
 *         [--cycles N] [--realtime] [--cmd "GET TEMP"]...
 *
 *   --cycles N を付けると、N 回（N × 10 ms の模擬時刻）回して終わる。
 *   付けないと実機と同じく止まらない（Ctrl+C で止める）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "em4_types.h"
#include "em4_config.h"
#include "hal.h"
#include "drivers.h"
#include "proto.h"
#include "app.h"
#include "util.h"
#include "telemetry.h"

#define MAX_STARTUP_CMDS    (8U)
#define CAL_MAGIC           (0xCA11U)
#define BOOT_MAGIC          (0xB007U)
#define UART_CONSOLE_BAUD   (115200U)
#define UART_UPLINK_BAUD    (57600U)
#define UPLINK_ACK_WAIT_MS  (500U)

typedef struct {
    const char *config_path;
    const char *flash_path;
    const char *log_dir;
    uint32_t    cycles;
    bool        realtime;
    const char *cmds[MAX_STARTUP_CMDS];
    uint8_t     ncmds;
} app_opts_t;

typedef struct {
    uint16_t magic;
    uint16_t fw_version;
    uint32_t boot_count;
    uint32_t last_fault;
} boot_rec_t;

typedef struct {
    uint16_t magic;
    uint16_t count;
} cal_hdr_t;

static const char *const TAG = "main";

static app_opts_t    s_opts;
static sht_dev_t     s_sht;
static bmp_dev_t     s_bmp;
static sht_sample_t  s_last_climate;
static int32_t       s_cal[TELEM_CH_COUNT];
static volatile bool s_reset_req;
static volatile uint32_t s_door_events;
static volatile bool s_door_changed;

/* 上りリンク */
static frame_dec_t   s_dec;
static uint8_t       s_uplink_seq;
static uint8_t       s_retx_buf[FRAME_MAX];   /* NAK のときに送り直す写し */
static size_t        s_retx_len;
static uint8_t       s_retx_seq;
static uint8_t       s_retx_left;
static uint32_t      s_retx_sent_ms;
static uint32_t      s_n_ack;
static uint32_t      s_n_nak;
static uint32_t      s_n_retx;

/* コンソール */
static char          s_con_line[CMD_LINE_MAX];
static size_t        s_con_len;
static bool          s_con_overflow;

/* ===================================================================== */
/* 上りリンク                                                            */
/* ===================================================================== */
em4_err_t uplink_send_frame(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    const em4_config_t *cfg = config_get();
    frame_t             f;
    uint8_t             wire[FRAME_MAX];
    int                 n;

    if ((len > 0U) && (payload == NULL)) {
        return EM4_ERR_PARAM;
    }
    if (len > FRAME_PAYLOAD_MAX) {
        return EM4_ERR_RANGE;
    }
    if (!cfg->uplink_enabled) {
        return EM4_ERR_STATE;
    }

    f.cmd = cmd;
    f.seq = s_uplink_seq;
    f.len = len;
    if (len > 0U) {
        (void)memcpy(f.payload, payload, len);
    }
    n = frame_encode(wire, &f);
    if (n < 0) {
        return EM4_ERR_FORMAT;
    }
    if (uart_write(UART_UPLINK, wire, (size_t)n) != n) {
        return EM4_ERR_IO;
    }
    s_uplink_seq++;

    /* ACK が来るまで写しを残す。NAK なら送り直す */
    (void)memcpy(s_retx_buf, wire, (size_t)n);
    s_retx_len     = (size_t)n;
    s_retx_seq     = f.seq;
    s_retx_left    = cfg->uplink_retries;
    s_retx_sent_ms = timer_now_ms();
    return EM4_OK;
}

static void uplink_retransmit(void)
{
    if ((s_retx_len == 0U) || (s_retx_left == 0U)) {
        s_retx_len = 0U;
        return;
    }
    if (uart_write(UART_UPLINK, s_retx_buf, s_retx_len) == (int)s_retx_len) {
        s_retx_left--;
        s_n_retx++;
        s_retx_sent_ms = timer_now_ms();
    }
}

static void uplink_handle_frame(const frame_t *f)
{
    switch (f->cmd) {
    case FRAME_CMD_ACK:
        if ((f->len >= 1U) && (f->payload[0] == s_retx_seq)) {
            s_retx_len = 0U;
            s_n_ack++;
        }
        break;
    case FRAME_CMD_NAK:
        s_n_nak++;
        if ((f->len >= 1U) && (f->payload[0] == s_retx_seq)) {
            uplink_retransmit();
        }
        break;
    case FRAME_CMD_ECHO:
        /* 上位からの疎通の確認。同じペイロードを返す */
        (void)uplink_send_frame(FRAME_CMD_ECHO, f->payload, f->len);
        break;
    default:
        LOG_D(TAG, "uplink: ignored cmd 0x%02X", f->cmd);
        break;
    }
}

static em4_err_t task_uplink_rx(void *ctx)
{
    uint8_t  rx[64];
    frame_t  f;
    int      n;
    int      i;
    uint32_t now = timer_now_ms();

    EM4_UNUSED(ctx);
    n = uart_read(UART_UPLINK, rx, sizeof(rx));
    if (n < 0) {
        LOG_W(TAG, "uplink: rx error");
        frame_dec_init(&s_dec);
        return EM4_ERR_IO;
    }
    for (i = 0; i < n; i++) {
        if (frame_dec_push(&s_dec, rx[i], now, &f) == 1) {
            uplink_handle_frame(&f);
        }
    }

    /* ACK が来ないまま時間が過ぎたら送り直す */
    if ((s_retx_len > 0U) && ((now - s_retx_sent_ms) > UPLINK_ACK_WAIT_MS)) {
        uplink_retransmit();
    }
    return EM4_OK;
}

/*
 * テレメトリーのフレーム
 *   id(2) uptime_s(4) flags(1) name_len(1) name(name_len) チャンネルごとの要約
 */
static em4_err_t task_uplink(void *ctx)
{
    const em4_config_t *cfg = config_get();
    frame_t             f;
    frame_writer_t      w;
    size_t              name_len = strlen(cfg->device_name);
    size_t              packed;
    uint8_t             flags = 0U;

    EM4_UNUSED(ctx);
    if (!cfg->uplink_enabled) {
        return EM4_OK;
    }
    if (s_door_events > 0U) {
        flags |= 0x01U;
    }
    if (!s_last_climate.valid) {
        flags |= 0x02U;
    }

    fw_begin(&w, &f, FRAME_CMD_TELEMETRY, 0U);
    fw_put_u16(&w, cfg->device_id);
    fw_put_u32(&w, timer_now_ms() / 1000U);
    fw_put_u8(&w, flags);
    fw_put_u8(&w, (uint8_t)name_len);
    fw_put_bytes(&w, (const uint8_t *)cfg->device_name, name_len);
    if (fw_end(&w) != EM4_OK) {
        return EM4_ERR_FULL;
    }
    packed = telemetry_pack(&f.payload[f.len], FRAME_PAYLOAD_MAX - (size_t)f.len);
    f.len  = (uint8_t)(f.len + packed);

    return uplink_send_frame(f.cmd, f.payload, f.len);
}

/* ===================================================================== */
/* センサー                                                              */
/* ===================================================================== */
static em4_err_t task_sample_sht(void *ctx)
{
    sht_sample_t s;
    em4_err_t    rc;

    EM4_UNUSED(ctx);
    rc = sht_read(&s_sht, &s);
    if (rc != EM4_OK) {
        return rc;
    }
    (void)telemetry_add(TELEM_CH_TEMP, s.temp_c100 + s_cal[TELEM_CH_TEMP], s.ts_ms);
    (void)telemetry_add(TELEM_CH_HUM, (int32_t)s.rh_c100 + s_cal[TELEM_CH_HUM], s.ts_ms);

    rc = sht_filter_get(&s_sht, &s_last_climate);
    if (rc == EM4_OK) {
        int32_t rh = (int32_t)s_last_climate.rh_c100 + s_cal[TELEM_CH_HUM];

        s_last_climate.temp_c100 += s_cal[TELEM_CH_TEMP];
        s_last_climate.rh_c100    = (rh < 0) ? 0U : (uint32_t)rh;
    }
    return rc;
}

static em4_err_t task_sample_bmp(void *ctx)
{
    int32_t   t;
    uint32_t  p;
    em4_err_t rc;

    EM4_UNUSED(ctx);
    rc = bmp_read(&s_bmp, &t, &p);
    if (rc != EM4_OK) {
        return rc;
    }
    return telemetry_add(TELEM_CH_PRESS, (int32_t)p, timer_now_ms());
}

em4_err_t app_latest_climate(sht_sample_t *out)
{
    if (out == NULL) {
        return EM4_ERR_PARAM;
    }
    if (!s_last_climate.valid) {
        return EM4_ERR_EMPTY;
    }
    *out = s_last_climate;
    return EM4_OK;
}

em4_err_t app_latest_pressure(uint32_t *press_pa)
{
    return bmp_get_average(&s_bmp, press_pa);
}

/* ===================================================================== */
/* 校正値と起動の記録（フラッシュ）                                      */
/* ===================================================================== */
em4_err_t app_set_calibration(uint8_t ch, int32_t offset_c100)
{
    if (ch > TELEM_CH_HUM) {
        return EM4_ERR_RANGE;       /* 気圧は校正しない */
    }
    s_cal[ch] = offset_c100;
    LOG_I(TAG, "calibration ch%u = %ld", ch, (long)offset_c100);
    return EM4_OK;
}

em4_err_t app_save_calibration(void)
{
    cal_hdr_t     hdr;
    uint16_t      crc;
    flash_chunk_t chunks[3];
    em4_err_t     rc;

    hdr.magic = CAL_MAGIC;
    hdr.count = (uint16_t)TELEM_CH_COUNT;
    crc = crc16_ccitt((const uint8_t *)s_cal, sizeof(s_cal));

    chunks[0].data = &hdr;
    chunks[0].len  = sizeof(hdr);
    chunks[1].data = s_cal;
    chunks[1].len  = sizeof(s_cal);
    chunks[2].data = &crc;
    chunks[2].len  = sizeof(crc);

    rc = flash_store_erase_sector(FLASH_ADDR_CAL / FLASH_SECTOR_SIZE);
    if (rc != EM4_OK) {
        return rc;
    }
    rc = flash_store_write_chunks(FLASH_ADDR_CAL, chunks, EM4_ARRAY_SIZE(chunks));
    if (rc == EM4_OK) {
        LOG_I(TAG, "calibration saved");
    }
    return rc;
}

static void load_calibration(void)
{
    cal_hdr_t hdr;
    int32_t   cal[TELEM_CH_COUNT];
    uint16_t  crc;

    if (flash_store_read(FLASH_ADDR_CAL, &hdr, sizeof(hdr)) != EM4_OK) {
        return;
    }
    if ((hdr.magic != CAL_MAGIC) || (hdr.count != TELEM_CH_COUNT)) {
        return;     /* まだ保存していない */
    }
    if ((flash_store_read(FLASH_ADDR_CAL + sizeof(hdr), cal, sizeof(cal)) != EM4_OK) ||
        (flash_store_read(FLASH_ADDR_CAL + sizeof(hdr) + sizeof(cal), &crc, sizeof(crc)) != EM4_OK)) {
        return;
    }
    if (crc != crc16_ccitt((const uint8_t *)cal, sizeof(cal))) {
        LOG_W(TAG, "calibration crc mismatch, ignored");
        return;
    }
    (void)memcpy(s_cal, cal, sizeof(s_cal));
    LOG_I(TAG, "calibration loaded (t=%ld h=%ld)", (long)s_cal[0], (long)s_cal[1]);
}

static em4_err_t persist_boot_info(void)
{
    boot_rec_t    rec;
    uint16_t      crc;
    flash_chunk_t chunks[2];
    em4_err_t     rc;

    rec.magic      = BOOT_MAGIC;
    rec.fw_version = (uint16_t)((EM4_FW_VERSION_MAJOR << 8) | EM4_FW_VERSION_MINOR);
    rec.boot_count = flash_store_boot_count();
    rec.last_fault = 0U;
    crc = crc16_ccitt((const uint8_t *)&rec, sizeof(rec));

    chunks[0].data = &rec;
    chunks[0].len  = sizeof(rec);
    chunks[1].data = &crc;
    chunks[1].len  = sizeof(crc);

    rc = flash_store_erase_sector(FLASH_ADDR_BOOT / FLASH_SECTOR_SIZE);
    if (rc != EM4_OK) {
        return rc;
    }
    return flash_store_write_chunks(FLASH_ADDR_BOOT, chunks, EM4_ARRAY_SIZE(chunks));
}

/* ===================================================================== */
/* コンソール                                                            */
/* ===================================================================== */
static void console_exec_line(void)
{
    static cmd_out_t out;

    s_con_line[s_con_len] = '\0';
    cmd_out_init(&out);
    (void)cmd_execute(s_con_line, &out);
    if (out.len > 0U) {
        (void)uart_write(UART_CONSOLE, (const uint8_t *)out.buf, out.len);
    }
    s_con_len = 0U;
}

static void console_feed(char c)
{
    if ((c == '\r') || (c == '\n')) {
        if (s_con_overflow) {
            (void)uart_write(UART_CONSOLE, (const uint8_t *)"ERR line too long\n", 18U);
            s_con_overflow = false;
            s_con_len = 0U;
        } else if (s_con_len > 0U) {
            console_exec_line();
        } else {
            /* 空の行 */
        }
        return;
    }
    if (s_con_len < (sizeof(s_con_line) - 1U)) {
        s_con_line[s_con_len] = c;
        s_con_len++;
    } else {
        s_con_overflow = true;
    }
}

static em4_err_t task_console(void *ctx)
{
    uint8_t rx[32];
    int     n;
    size_t  i;

    EM4_UNUSED(ctx);
    n = uart_read(UART_CONSOLE, rx, sizeof(rx));
    for (i = 0U; i < n; i++) {
        console_feed((char)rx[i]);
    }
    return EM4_OK;
}

/* ===================================================================== */
/* その他のタスク                                                        */
/* ===================================================================== */
static void on_door_irq(uint8_t pin, void *ctx)
{
    EM4_UNUSED(pin);
    EM4_UNUSED(ctx);
    s_door_events++;
    s_door_changed = true;
}

static em4_err_t task_housekeeping(void *ctx)
{
    EM4_UNUSED(ctx);

    (void)gpio_toggle(GPIO_PIN_LED_STATUS);
    if (s_door_changed) {
        s_door_changed = false;
        LOG_W(TAG, "enclosure door changed (events=%lu)", (unsigned long)s_door_events);
    }
    log_flush();
    if (!wdt_expired()) {
        wdt_kick();
    }
    return EM4_OK;
}

void app_get_health(char *buf, size_t max)
{
    sht_stats_t st;
    uint32_t    n_err;
    uint32_t    n_warn;

    if ((buf == NULL) || (max == 0U)) {
        return;
    }
    sht_get_stats(&s_sht, &st);
    log_get_counts(&n_err, &n_warn, NULL);
    (void)snprintf(buf, max,
                   "sht: ok=%lu crc=%lu bus=%lu err=%lu%%\n"
                   "bmp: ready=%d err=%lu\n"
                   "uplink: ack=%lu nak=%lu retx=%lu dec_ok=%lu dec_crc=%lu\n"
                   "log: error=%lu warn+=%lu  door=%lu\n",
                   (unsigned long)st.n_ok, (unsigned long)st.n_crc_err, (unsigned long)st.n_bus_err,
                   (unsigned long)st.err_pct, s_bmp.ready ? 1 : 0, (unsigned long)s_bmp.n_err,
                   (unsigned long)s_n_ack, (unsigned long)s_n_nak, (unsigned long)s_n_retx,
                   (unsigned long)s_dec.n_ok, (unsigned long)s_dec.n_crc_err,
                   (unsigned long)n_err, (unsigned long)n_warn, (unsigned long)s_door_events);
}

void app_request_reset(void)
{
    s_reset_req = true;
}

/* ===================================================================== */
/* 初期化                                                                */
/* ===================================================================== */
/* 周期は既定値。sample / uplink は設定で上書きする */
static const sched_def_t k_task_defs[] = {
    { "sample",  task_sample_sht,   NULL, 1000U },
    { "baro",    task_sample_bmp,   NULL, 1000U },
    { "uplink",  task_uplink,       NULL, 5000U },
    { "linkrx",  task_uplink_rx,    NULL, 20U },
    { "console", task_console,      NULL, 50U },
    { "house",   task_housekeeping, NULL, 500U },
};

_Static_assert(EM4_ARRAY_SIZE(k_task_defs) <= SCHED_MAX_TASKS,
               "k_task_defs has more tasks than the scheduler can hold");

static void register_tasks(const em4_config_t *cfg)
{
    size_t i;

    sched_init();
    for (i = 0U; i < EM4_ARRAY_SIZE(k_task_defs); i++) {
        sched_def_t d = k_task_defs[i];

        if (strcmp(d.name, "sample") == 0) {
            d.period_ms = cfg->sample_period_ms;
        } else if (strcmp(d.name, "uplink") == 0) {
            d.period_ms = cfg->uplink_period_ms;
        } else {
            /* 既定値のまま */
        }
        if (sched_register(&d, NULL) != EM4_OK) {
            fatal_error(FAULT_SCHED, (uint32_t)i);
        }
    }
}

static void board_init(void)
{
    (void)gpio_config(GPIO_PIN_LED_STATUS, GPIO_MODE_OUTPUT);
    (void)gpio_config(GPIO_PIN_LED_FAULT, GPIO_MODE_OUTPUT);
    (void)gpio_config(GPIO_PIN_SENSOR_PWR, GPIO_MODE_OUTPUT);
    (void)gpio_config(GPIO_PIN_DOOR_SW, GPIO_MODE_IRQ_BOTH);
    (void)gpio_attach_irq(GPIO_PIN_DOOR_SW, on_door_irq, NULL);
    (void)gpio_set(GPIO_PIN_SENSOR_PWR, true);

    (void)i2c_init(SHT_I2C_BUS, 100000U);
    (void)spi_init(BMP_SPI_BUS, 1000000U);
    (void)uart_init(UART_CONSOLE, UART_CONSOLE_BAUD);
    (void)uart_init(UART_UPLINK, UART_UPLINK_BAUD);
    uart_sim_echo_stdout(UART_CONSOLE, true);
}

static void app_shutdown(void)
{
    telemetry_shutdown();
    flash_store_unmount();
    log_shutdown();
}

static void usage(const char *prog)
{
    (void)fprintf(stderr,
                  "usage: %s [--config FILE] [--flash FILE] [--logdir DIR] [--cycles N]\n"
                  "          [--realtime] [--cmd \"COMMAND\"]...\n", prog);
}

static bool parse_args(int argc, char *argv[], app_opts_t *o)
{
    int i;

    (void)memset(o, 0, sizeof(*o));
    o->flash_path = "em4flash.bin";
    for (i = 1; i < argc; i++) {
        const char *a    = argv[i];
        const char *next = (i + 1 < argc) ? argv[i + 1] : NULL;

        if ((strcmp(a, "--config") == 0) && (next != NULL)) {
            o->config_path = next;
            i++;
        } else if ((strcmp(a, "--flash") == 0) && (next != NULL)) {
            o->flash_path = next;
            i++;
        } else if ((strcmp(a, "--logdir") == 0) && (next != NULL)) {
            o->log_dir = next;
            i++;
        } else if ((strcmp(a, "--cycles") == 0) && (next != NULL)) {
            if (str_to_u32(next, &o->cycles) != EM4_OK) {
                return false;
            }
            i++;
        } else if (strcmp(a, "--realtime") == 0) {
            o->realtime = true;
        } else if ((strcmp(a, "--cmd") == 0) && (next != NULL)) {
            if (o->ncmds >= MAX_STARTUP_CMDS) {
                return false;
            }
            o->cmds[o->ncmds] = next;
            o->ncmds++;
            i++;
        } else {
            return false;
        }
    }
    return true;
}

static void main_loop_once(void)
{
    (void)sched_run_due(timer_now_ms());
    if (s_opts.realtime) {
        timer_delay_ms(TIMER_TICK_MS);
        timer_tick_isr();
    } else {
        timer_sim_advance(TIMER_TICK_MS);
    }
    if (s_reset_req) {
        LOG_I(TAG, "reset requested");
        platform_reset();
    }
}

int main(int argc, char *argv[])
{
    const em4_config_t *cfg;
    em4_err_t           rc;
    uint8_t             i;

    if (!parse_args(argc, argv, &s_opts)) {
        usage(argv[0]);
        return 2;
    }

    timer_init();
    timer_set_realtime(s_opts.realtime);
    platform_set_reset_hook(app_shutdown);
    board_init();

    (void)config_init(s_opts.config_path);
    cfg = config_get();

    rc = log_init((s_opts.log_dir != NULL) ? s_opts.log_dir : cfg->log_dir, cfg->log_level);
    if (rc != EM4_OK) {
        (void)fprintf(stderr, "log init failed: %s\n", em4_strerror(rc));
    }
    LOG_I(TAG, "EM-4 fw %s starting (%s, id=%u)", EM4_FW_VERSION_STR, cfg->device_name,
          cfg->device_id);

    rc = flash_store_mount(s_opts.flash_path);
    if (rc == EM4_OK) {
        load_calibration();
        rc = persist_boot_info();
        if (rc != EM4_OK) {
            LOG_W(TAG, "boot record not saved (%s)", em4_strerror(rc));
        }
    } else {
        LOG_E(TAG, "flash not available (%s), running without persistence", em4_strerror(rc));
    }

    rc = sht_init(&s_sht, cfg->sht_bus, cfg->sht_addr, cfg->sht_retries);
    if (rc != EM4_OK) {
        LOG_E(TAG, "temperature/humidity sensor not available");
    } else if (cfg->sht_heater) {
        (void)sht_heater(&s_sht, true);
    } else {
        /* ヒーターは使わない */
    }
    rc = bmp_init(&s_bmp, BMP_SPI_BUS, BMP_SPI_CS, cfg->bmp_osr_temp, cfg->bmp_osr_press);
    if (rc != EM4_OK) {
        LOG_E(TAG, "pressure sensor not available");
    }

    rc = telemetry_init(cfg->telemetry_window);
    if (rc != EM4_OK) {
        fatal_error(FAULT_TELEMETRY, (uint32_t)(-rc));
    }
    (void)telemetry_set_alarm(TELEM_CH_TEMP, -2000, 6000);
    (void)telemetry_set_alarm(TELEM_CH_HUM, 0, 9500);

    frame_dec_init(&s_dec);
    register_tasks(cfg);

    for (i = 0U; i < s_opts.ncmds; i++) {
        (void)uart_sim_inject(UART_CONSOLE, (const uint8_t *)s_opts.cmds[i], strlen(s_opts.cmds[i]));
        (void)uart_sim_inject(UART_CONSOLE, (const uint8_t *)"\n", 1U);
    }

    wdt_init(WDT_TIMEOUT_MS);
    LOG_I(TAG, "boot complete, %u tasks", sched_count());

    if (s_opts.cycles > 0U) {
        uint32_t c;
        for (c = 0U; c < s_opts.cycles; c++) {
            main_loop_once();
        }
        app_shutdown();
        return 0;
    }

    /* 実機と同じく、ここから先は戻らない。止まるのは電源断か、ウォッチドッグのリセットのとき */
    while (1U) {
        main_loop_once();
    }
}
