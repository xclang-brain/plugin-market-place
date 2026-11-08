/**
 * @file    test_main.c
 * @brief   ホストで動かす単体試験（make test）
 *
 * build/ で動かす。試験用のファイル（ini、フラッシュのイメージ、ログ）は build/ の下に作る。
 * main.c はつながないので、main.c にある口（app_*、uplink_send_frame）はここで代わりを用意する。
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

static int s_pass;
static int s_fail;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (cond) {                                                              \
            s_pass++;                                                            \
        } else {                                                                 \
            s_fail++;                                                            \
            (void)printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);       \
        }                                                                        \
    } while (0)

#define CHECK_NEAR(a, b, tol) CHECK((((a) > (b)) ? ((a) - (b)) : ((b) - (a))) <= (tol))

/* ===================================================================== */
/* main.c の代わり                                                       */
/* ===================================================================== */
static uint32_t s_stub_uplink_frames;
static uint8_t  s_stub_last_len;

em4_err_t app_latest_climate(sht_sample_t *out)
{
    out->temp_c100 = 2345;
    out->rh_c100   = 4100U;
    out->ts_ms     = 0U;
    out->valid     = true;
    return EM4_OK;
}

em4_err_t app_latest_pressure(uint32_t *press_pa)
{
    *press_pa = 101325U;
    return EM4_OK;
}

em4_err_t app_set_calibration(uint8_t ch, int32_t offset_c100)
{
    EM4_UNUSED(offset_c100);
    return (ch < 2U) ? EM4_OK : EM4_ERR_RANGE;
}

em4_err_t app_save_calibration(void)
{
    return EM4_OK;
}

void app_get_health(char *buf, size_t max)
{
    (void)snprintf(buf, max, "stub\n");
}

em4_err_t uplink_send_frame(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    EM4_UNUSED(cmd);
    EM4_UNUSED(payload);
    s_stub_uplink_frames++;
    s_stub_last_len = len;
    return EM4_OK;
}

void app_request_reset(void)
{
}

/* ===================================================================== */
/* 試験                                                                  */
/* ===================================================================== */
static void test_crc(void)
{
    static const uint8_t k_check[] = "123456789";
    static const uint8_t k_sht[]   = { 0xBEU, 0xEFU };

    CHECK(crc16_ccitt(k_check, 9U) == 0x29B1U);
    CHECK(crc16_ccitt_update(crc16_ccitt(k_check, 4U), &k_check[4], 5U) == 0x29B1U);
    CHECK(crc8_sensirion(k_sht, 2U) == 0x92U);
}

static void test_ringbuf(void)
{
    uint8_t   mem[8];
    uint8_t   out[8];
    ringbuf_t rb;
    uint8_t   b = 0U;
    size_t    n;

    CHECK(rb_init(&rb, mem, sizeof(mem)) == EM4_OK);
    CHECK(rb_push(&rb, 0x11U) == EM4_OK);
    CHECK(rb_push(&rb, 0x22U) == EM4_OK);
    CHECK(rb_count(&rb) == 2U);
    CHECK((rb_pop(&rb, &b) == EM4_OK) && (b == 0x11U));

    n = rb_write(&rb, (const uint8_t *)"ABCDEFGHIJ", 10U, true);
    CHECK(n == 10U);
    CHECK(rb_count(&rb) == 8U);
    CHECK(rb.dropped == 3U);
    n = rb_peek_tail(&rb, out, 4U);
    CHECK((n == 4U) && (memcmp(out, "GHIJ", 4U) == 0));
    n = rb_read(&rb, out, sizeof(out));
    CHECK((n == 8U) && (memcmp(out, "CDEFGHIJ", 8U) == 0));
    CHECK(rb_pop(&rb, &b) == EM4_ERR_EMPTY);
    CHECK(rb_write(&rb, (const uint8_t *)"0123456789", 10U, false) == 8U);
}

static void test_strutil(void)
{
    char     s1[] = "  hello world \r\n";
    char     small[6];
    uint8_t  bin[4];
    char     hex[9];
    uint32_t u = 0U;
    int32_t  i = 0;
    bool     b = false;

    CHECK(strcmp(str_trim(s1), "hello world") == 0);
    CHECK(str_is_kv_line("a=1"));
    CHECK(str_is_kv_line("key = "));
    CHECK(!str_is_kv_line(" = 3"));
    CHECK(!str_is_kv_line("no equal sign"));
    CHECK(str_casecmp("Info", "INFO") == 0);
    CHECK(str_casecmp("a", "b") < 0);
    CHECK(str_copy(small, sizeof(small), "abcdefgh") == 8U);
    CHECK(strcmp(small, "abcde") == 0);
    CHECK(str_hex_decode("DEADbeef", bin, sizeof(bin)) == 4U);
    CHECK((bin[0] == 0xDEU) && (bin[3] == 0xEFU));
    CHECK(str_hex_encode(bin, 4U, hex, sizeof(hex)) == 4U);
    CHECK(strcmp(hex, "DEADBEEF") == 0);
    CHECK(str_hex_decode("ABC", bin, sizeof(bin)) == 0U);
    CHECK((str_to_u32("0x44", &u) == EM4_OK) && (u == 0x44U));
    CHECK(str_to_u32("12x", &u) == EM4_ERR_FORMAT);
    CHECK(str_to_u32("-1", &u) == EM4_ERR_PARAM);
    CHECK((str_to_i32("-250", &i) == EM4_OK) && (i == -250));
    CHECK((str_to_bool("On", &b) == EM4_OK) && b);
    CHECK(str_to_bool("maybe", &b) == EM4_ERR_FORMAT);
    CHECK(str_hash("period_ms") != str_hash("uplink_ms"));
    CHECK(str_starts_with("sht.retries", "sht."));
}

static void test_frame(void)
{
    frame_t     f;
    frame_t     g;
    frame_dec_t d;
    uint8_t     wire[FRAME_MAX];
    int         n;
    int         i;
    int         r = 0;
    frame_writer_t w;

    fw_begin(&w, &f, FRAME_CMD_TELEMETRY, 7U);
    fw_put_u16(&w, 0x1234U);
    fw_put_i32(&w, -2);
    fw_put_bytes(&w, (const uint8_t *)"EM4", 3U);
    CHECK(fw_end(&w) == EM4_OK);
    CHECK(f.len == 9U);

    n = frame_encode(wire, &f);
    CHECK(n == (int)(FRAME_HDR_LEN + 9U + FRAME_CRC_LEN));
    CHECK(wire[0] == FRAME_SOF);

    frame_dec_init(&d);
    for (i = 0; i < n; i++) {
        r = frame_dec_push(&d, wire[i], 100U, &g);
    }
    CHECK(r == 1);
    CHECK((g.cmd == FRAME_CMD_TELEMETRY) && (g.seq == 7U) && (g.len == 9U));
    CHECK(memcmp(g.payload, f.payload, 9U) == 0);

    /* CRC の誤り */
    wire[5] ^= 0xFFU;
    r = 0;
    for (i = 0; i < n; i++) {
        r = frame_dec_push(&d, wire[i], 200U, &g);
    }
    CHECK(r == -1);
    CHECK(d.n_crc_err == 1U);

    /* LEN が大きすぎる */
    frame_dec_init(&d);
    (void)frame_dec_push(&d, FRAME_SOF, 300U, &g);
    CHECK(frame_dec_push(&d, (uint8_t)(FRAME_PAYLOAD_MAX + 1U), 300U, &g) == -1);

    /* ペイロードがあふれる */
    fw_begin(&w, &f, FRAME_CMD_EVENT, 0U);
    for (i = 0; i < 20; i++) {
        fw_put_u32(&w, (uint32_t)i);
    }
    CHECK(fw_end(&w) == EM4_ERR_FULL);
}

static void test_config(void)
{
    static const char k_ini[] =
        "; EM-4 settings for the test\n"
        "[device]\n"
        "name = TEST-UNIT\n"
        "id = 42\n"
        "\n"
        "[sampling]\n"
        "period_ms = 500\n"
        "uplink_ms = 10000\n"
        "window = 30\n"
        "\n"
        "[sensor]\n"
        "sht.retries = 3\n"
        "sht.addr = 0x45\n"
        "bmp.osr_t = 1\n"
        "bmp.osr_p = 8\n"
        "temp.offset = -25\n"
        "\n"
        "[log]\n"
        "level = debug\n"
        "dir = testlog\n";
    static const char k_bad[] =
        "[sampling]\n"
        "period_ms = 5\n"
        "[sensor]\n"
        "sht.retries = 1\n";
    em4_config_t cfg;
    FILE        *fp;

    fp = fopen("test_em4.ini", "w");
    CHECK(fp != NULL);
    if (fp == NULL) {
        return;
    }
    (void)fputs(k_ini, fp);
    (void)fclose(fp);

    CHECK(config_load("test_em4.ini", &cfg) == EM4_OK);
    CHECK(strcmp(cfg.device_name, "TEST-UNIT") == 0);
    CHECK(cfg.device_id == 42U);
    CHECK(cfg.sample_period_ms == 500U);
    CHECK(cfg.uplink_period_ms == 10000U);
    CHECK(cfg.telemetry_window == 30U);
    CHECK(cfg.sht_retries == 3U);
    CHECK(cfg.sht_addr == 0x45U);
    CHECK(cfg.bmp_osr_temp == BMP_OSR_1);
    CHECK(cfg.bmp_osr_press == BMP_OSR_8);
    CHECK(cfg.temp_offset_c100 == -25);
    CHECK(cfg.log_level == LOG_DEBUG);
    CHECK(strcmp(cfg.log_dir, "testlog") == 0);
    CHECK(cfg.uplink_enabled);          /* 書いていない項目は既定値 */

    fp = fopen("test_bad.ini", "w");
    CHECK(fp != NULL);
    if (fp == NULL) {
        return;
    }
    (void)fputs(k_bad, fp);
    (void)fclose(fp);
    CHECK(config_load("test_bad.ini", &cfg) == EM4_ERR_RANGE);
    CHECK(config_load("does_not_exist.ini", &cfg) == EM4_ERR_IO);
}

static void test_flash(void)
{
    static const uint8_t k_data[] = { 1U, 2U, 3U, 4U, 5U };
    static const char    k_name[] = "unit-07";
    flash_image_hdr_t    hdr;
    flash_chunk_t        chunks[2];
    uint8_t              buf[16];
    uint16_t             len = 0U;
    uint32_t             boots;

    (void)remove("test_flash.bin");
    CHECK(flash_store_mount("test_flash.bin") == EM4_OK);
    boots = flash_store_boot_count();
    CHECK(boots == 1U);

    CHECK(flash_rec_append(0x0101U, k_data, sizeof(k_data)) == EM4_OK);
    CHECK(flash_rec_append(0x0102U, k_name, sizeof(k_name)) == EM4_OK);
    CHECK(flash_rec_append(0x0101U, &k_data[1], 3U) == EM4_OK);
    CHECK(flash_rec_find(0x0101U, buf, sizeof(buf), &len) == EM4_OK);
    CHECK((len == 3U) && (buf[0] == 2U));
    CHECK(flash_rec_find(0x0102U, buf, sizeof(buf), &len) == EM4_OK);
    CHECK(strcmp((const char *)buf, k_name) == 0);
    CHECK(flash_rec_find(0x0999U, buf, sizeof(buf), &len) == EM4_ERR_NOTFOUND);

    chunks[0].data = k_name;
    chunks[0].len  = 4U;
    chunks[1].data = k_data;
    chunks[1].len  = sizeof(k_data);
    CHECK(flash_store_erase_sector(FLASH_ADDR_CAL / FLASH_SECTOR_SIZE) == EM4_OK);
    CHECK(flash_store_write_chunks(FLASH_ADDR_CAL, chunks, 2U) == EM4_OK);
    CHECK(flash_store_read(FLASH_ADDR_CAL, buf, 9U) == EM4_OK);
    CHECK((memcmp(buf, "unit", 4U) == 0) && (buf[4] == 1U) && (buf[8] == 5U));
    /* 消去していないところに 0 → 1 の書き込みをすると失敗する（NOR の制約） */
    chunks[0].data = "ZZZZ";
    CHECK(flash_store_write_chunks(FLASH_ADDR_CAL, chunks, 2U) == EM4_ERR_STATE);

    flash_store_unmount();
    CHECK(flash_store_mount("test_flash.bin") == EM4_OK);
    CHECK(flash_store_boot_count() == (boots + 1U));
    CHECK(flash_rec_find(0x0102U, buf, sizeof(buf), &len) == EM4_OK);
    flash_store_unmount();

    CHECK(flash_store_load("test_flash.bin", &hdr) == EM4_OK);
    CHECK((hdr.magic == FLASH_MAGIC) && (hdr.boot_count == (boots + 1U)));
    CHECK(flash_store_load("does_not_exist.bin", &hdr) == EM4_ERR_IO);
}

static uint32_t s_task_a_runs;
static uint32_t s_task_b_runs;

static em4_err_t task_a(void *ctx)
{
    EM4_UNUSED(ctx);
    s_task_a_runs++;
    return EM4_OK;
}

static em4_err_t task_b(void *ctx)
{
    EM4_UNUSED(ctx);
    s_task_b_runs++;
    return EM4_ERR_IO;
}

static void test_scheduler(void)
{
    static const sched_def_t k_a = { "a", task_a, NULL, 100U };
    static const sched_def_t k_b = { "b", task_b, NULL, 250U };
    sched_info_t info;
    uint8_t      id = 0xFFU;
    uint32_t     t;

    timer_init();
    sched_init();
    CHECK(sched_register(&k_a, &id) == EM4_OK);
    CHECK(id == 0U);
    CHECK(sched_register(&k_b, NULL) == EM4_OK);
    CHECK(sched_count() == 2U);
    CHECK(sched_find("b") == 1);
    CHECK(sched_find("zzz") == -1);

    for (t = 0U; t <= 1000U; t += TIMER_TICK_MS) {
        (void)sched_run_due(timer_now_ms());
        timer_sim_advance(TIMER_TICK_MS);
    }
    CHECK(s_task_a_runs == 10U);
    CHECK(s_task_b_runs == 4U);
    CHECK((sched_get_info(1U, &info) == EM4_OK) && (info.errors == 4U));
    CHECK(sched_set_period(0U, 50U) == EM4_OK);
    CHECK(sched_set_period(7U, 50U) == EM4_ERR_NOTFOUND);
}

static void test_sensors(void)
{
    sht_dev_t    sht;
    bmp_dev_t    bmp;
    sht_sample_t s;
    sht_stats_t  st;
    int32_t      t = 0;
    uint32_t     p = 0U;
    uint32_t     avg = 0U;

    timer_init();
    CHECK(i2c_init(SHT_I2C_BUS, 100000U) == EM4_OK);
    CHECK(spi_init(BMP_SPI_BUS, 1000000U) == EM4_OK);
    sim_env_set(2500, 5000U, 100000U);

    CHECK(sht_init(&sht, SHT_I2C_BUS, SHT_I2C_ADDR, 2U) == EM4_OK);
    sim_env_set(2500, 5000U, 100000U);
    CHECK(sht_read(&sht, &s) == EM4_OK);
    CHECK(s.valid);
    CHECK_NEAR(s.temp_c100, 2500, 5);
    CHECK_NEAR(s.rh_c100, 5000U, 5U);

    /* CRC の誤りを 1 回だけ起こす。やり直しで読める */
    sim_fault_inject(SIM_FAULT_I2C_CRC, 1U);
    CHECK(sht_read(&sht, &s) == EM4_OK);
    CHECK(sht.n_crc_err == 1U);
    CHECK(sht_filter_get(&sht, &s) == EM4_OK);
    CHECK_NEAR(s.temp_c100, 2500, 5);
    sht_get_stats(&sht, &st);
    CHECK((st.n_ok == 2U) && (st.n_crc_err == 1U));

    /* つながっていないアドレス */
    CHECK(sht_init(&sht, SHT_I2C_BUS, 0x45U, 2U) == EM4_ERR_IO);

    CHECK(bmp_init(&bmp, BMP_SPI_BUS, BMP_SPI_CS, BMP_OSR_2, BMP_OSR_16) == EM4_OK);
    CHECK(bmp_read(&bmp, &t, &p) == EM4_OK);
    CHECK_NEAR(t, 2500, 10);
    CHECK_NEAR(p, 100000U, 50U);
    CHECK(bmp_get_average(&bmp, &avg) == EM4_OK);
    CHECK(avg == p);
}

static void test_telemetry(void)
{
    telem_summary_t sum;
    uint8_t         buf[64];
    size_t          n;

    CHECK(telemetry_init(4U) == EM4_OK);
    CHECK(telemetry_init(4U) == EM4_ERR_STATE);
    CHECK(telemetry_add(TELEM_CH_TEMP, 2000, 10U) == EM4_OK);
    CHECK(telemetry_add(TELEM_CH_TEMP, 2200, 20U) == EM4_OK);
    CHECK(telemetry_add(TELEM_CH_TEMP, 2400, 30U) == EM4_OK);
    CHECK(telemetry_add(9U, 1, 30U) == EM4_ERR_PARAM);
    CHECK(telemetry_get(TELEM_CH_TEMP, &sum) == EM4_OK);
    CHECK(sum.count == 3U);
    CHECK(sum.min == 2000);
    CHECK(sum.mean == 2200);
    CHECK(sum.last == 2400);
    CHECK(telemetry_mean(TELEM_CH_TEMP) == 2200);

    n = telemetry_pack(buf, sizeof(buf));
    CHECK(n == (TELEM_CH_COUNT * 14U));
    n = telemetry_pack(buf, 20U);
    CHECK(n == 14U);

    telemetry_reset();
    CHECK((telemetry_get(TELEM_CH_TEMP, &sum) == EM4_OK) && (sum.count == 0U));
    telemetry_shutdown();
    CHECK(telemetry_get(TELEM_CH_TEMP, &sum) == EM4_ERR_STATE);
}

static void test_log(void)
{
    char buf[256];
    size_t n;

    CHECK(log_init("testlog", LOG_DEBUG) == EM4_OK);
    CHECK(log_level_from_str("warn") == (int)LOG_WARN);
    CHECK(log_level_from_str("Debug") == (int)LOG_DEBUG);
    LOG_I("test", "hello %d", 42);
    LOG_T("test", "not shown");
    n = log_ring_copy_tail(buf, sizeof(buf) - 1U);
    buf[n] = '\0';
    CHECK(strstr(buf, "hello 42") != NULL);
    CHECK(strstr(buf, "not shown") == NULL);
    log_flush();
}

static void test_cmd(void)
{
    char      line[CMD_LINE_MAX];
    char     *argv[CMD_MAX_ARGS];
    cmd_out_t out;

    (void)str_copy(line, sizeof(line), "  GET   stats  2 \r\n");
    CHECK(cmd_tokenize(line, argv, (int)CMD_MAX_ARGS) == 3);
    CHECK(strcmp(argv[1], "stats") == 0);

    cmd_out_init(&out);
    (void)str_copy(line, sizeof(line), "ver");
    CHECK(cmd_execute(line, &out) == EM4_OK);
    CHECK(strstr(out.buf, "EM-4 fw " EM4_FW_VERSION_STR) != NULL);

    cmd_out_init(&out);
    (void)str_copy(line, sizeof(line), "GET TEMP");
    CHECK(cmd_execute(line, &out) == EM4_OK);
    CHECK(strstr(out.buf, "temp=23.45 C") != NULL);

    cmd_out_init(&out);
    (void)str_copy(line, sizeof(line), "GET STATS 7");
    CHECK(cmd_execute(line, &out) == EM4_ERR_RANGE);

    cmd_out_init(&out);
    (void)str_copy(line, sizeof(line), "SET RATE");
    CHECK(cmd_execute(line, &out) == EM4_ERR_PARAM);

    cmd_out_init(&out);
    (void)str_copy(line, sizeof(line), "ECHO 0102030405");
    CHECK(cmd_execute(line, &out) == EM4_OK);
    CHECK((s_stub_uplink_frames == 1U) && (s_stub_last_len == 5U));

    cmd_out_init(&out);
    (void)str_copy(line, sizeof(line), "LOG DUMP 64");
    CHECK(cmd_execute(line, &out) == EM4_OK);

    cmd_out_init(&out);
    (void)str_copy(line, sizeof(line), "FROB");
    CHECK(cmd_execute(line, &out) == EM4_ERR_NOTFOUND);
}

static uint32_t s_irq_hits;

static void on_irq(uint8_t pin, void *ctx)
{
    EM4_UNUSED(ctx);
    if (pin == GPIO_PIN_DOOR_SW) {
        s_irq_hits++;
    }
}

static void test_gpio(void)
{
    bool level = false;

    CHECK(gpio_config(GPIO_PIN_LED_STATUS, GPIO_MODE_OUTPUT) == EM4_OK);
    CHECK(gpio_set(GPIO_PIN_LED_STATUS, true) == EM4_OK);
    CHECK((gpio_get(GPIO_PIN_LED_STATUS, &level) == EM4_OK) && level);
    CHECK(gpio_toggle(GPIO_PIN_LED_STATUS) == EM4_OK);
    CHECK((gpio_get(GPIO_PIN_LED_STATUS, &level) == EM4_OK) && !level);
    CHECK(gpio_set(GPIO_PIN_DOOR_SW, true) == EM4_ERR_STATE);
    CHECK(gpio_config(GPIO_PIN_COUNT, GPIO_MODE_OUTPUT) == EM4_ERR_PARAM);

    CHECK(gpio_config(GPIO_PIN_DOOR_SW, GPIO_MODE_IRQ_RISING) == EM4_OK);
    CHECK(gpio_attach_irq(GPIO_PIN_DOOR_SW, on_irq, NULL) == EM4_OK);
    gpio_sim_drive(GPIO_PIN_DOOR_SW, true);
    gpio_sim_drive(GPIO_PIN_DOOR_SW, false);    /* 立ち下がりは数えない */
    timer_tick_isr();
    CHECK(s_irq_hits == 1U);
    CHECK(gpio_irq_count(GPIO_PIN_DOOR_SW) == 1U);
}

int main(void)
{
    (void)printf("EM-4 host unit tests\n");
    test_crc();
    test_ringbuf();
    test_strutil();
    test_frame();
    test_log();
    test_config();
    test_flash();
    test_scheduler();
    test_sensors();
    test_telemetry();
    test_cmd();
    test_gpio();
    log_shutdown();

    (void)printf("%d passed, %d failed\n", s_pass, s_fail);
    return (s_fail == 0) ? 0 : 1;
}
