/**
 * @file    cmd_parser.c
 * @brief   保守用のコンソールのコマンド（文字）を解釈して実行する
 *
 * 1 行が 1 コマンド。語は空白で区切る。動詞と目的語は大文字小文字を問わない。
 *   例) GET TEMP / SET RATE sample 500 / LOG DUMP 512 / GPIO SET 3 1
 *
 * 結果は cmd_out_t に文字で入る。最後の行は "OK" か "ERR <理由>"。
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "proto.h"
#include "app.h"
#include "hal.h"
#include "util.h"
#include "telemetry.h"

typedef em4_err_t (*cmd_handler_t)(int argc, char *argv[], cmd_out_t *out);

typedef struct {
    const char   *verb;
    const char   *object;       /* NULL なら目的語なし */
    uint8_t       min_args;     /* 動詞・目的語を除いた引数の数 */
    uint8_t       max_args;
    cmd_handler_t fn;
    const char   *help;
} cmd_entry_t;

static uint32_t s_exec_count;

/* ===================================================================== */
/* 出力                                                                  */
/* ===================================================================== */
void cmd_out_init(cmd_out_t *out)
{
    out->buf[0]    = '\0';
    out->len       = 0U;
    out->truncated = false;
}

void cmd_printf(cmd_out_t *out, const char *fmt, ...)
{
    va_list ap;
    size_t  room;
    int     n;

    if (out->len >= (sizeof(out->buf) - 1U)) {
        out->truncated = true;
        return;
    }
    room = sizeof(out->buf) - out->len;
    va_start(ap, fmt);
    n = vsnprintf(&out->buf[out->len], room, fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    if ((size_t)n >= room) {
        out->len       = sizeof(out->buf) - 1U;
        out->truncated = true;
    } else {
        out->len += (size_t)n;
    }
}

void cmd_puts(cmd_out_t *out, const char *s)
{
    cmd_printf(out, "%s", s);
}

/* ===================================================================== */
/* 字句                                                                  */
/* ===================================================================== */
int cmd_tokenize(char *line, char *argv[], int max_args)
{
    int   argc = 0;
    char *p    = line;

    while ((*p != '\0') && (argc < max_args)) {
        while ((*p == ' ') || (*p == '\t')) {
            p++;
        }
        if ((*p == '\0') || (*p == '\r') || (*p == '\n')) {
            break;
        }
        argv[argc] = p;
        argc++;
        while ((*p != '\0') && (*p != ' ') && (*p != '\t') && (*p != '\r') && (*p != '\n')) {
            p++;
        }
        if (*p != '\0') {
            *p = '\0';
            p++;
        }
    }
    return argc;
}

static void fmt_c100(char *buf, size_t n, int32_t v)
{
    const char *sign = (v < 0) ? "-" : "";
    uint32_t    a    = (v < 0) ? (uint32_t)(-(int64_t)v) : (uint32_t)v;

    (void)snprintf(buf, n, "%s%lu.%02lu", sign, (unsigned long)(a / 100U), (unsigned long)(a % 100U));
}

/* ===================================================================== */
/* 各コマンド                                                            */
/* ===================================================================== */
static em4_err_t cmd_ver(int argc, char *argv[], cmd_out_t *out)
{
    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    cmd_printf(out, "EM-4 fw %s (%s)\n", EM4_FW_VERSION_STR, EM4_HW_REVISION_STR);
    return EM4_OK;
}

static em4_err_t cmd_get_temp(int argc, char *argv[], cmd_out_t *out)
{
    sht_sample_t s;
    char         t[16];
    em4_err_t    rc;

    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    rc = app_latest_climate(&s);
    if (rc != EM4_OK) {
        return rc;
    }
    fmt_c100(t, sizeof(t), s.temp_c100);
    cmd_printf(out, "temp=%s C\n", t);
    return EM4_OK;
}

static em4_err_t cmd_get_hum(int argc, char *argv[], cmd_out_t *out)
{
    sht_sample_t s;
    char         h[16];
    em4_err_t    rc;

    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    rc = app_latest_climate(&s);
    if (rc != EM4_OK) {
        return rc;
    }
    fmt_c100(h, sizeof(h), (int32_t)s.rh_c100);
    cmd_printf(out, "hum=%s %%RH\n", h);
    return EM4_OK;
}

static em4_err_t cmd_get_press(int argc, char *argv[], cmd_out_t *out)
{
    uint32_t  p = 0U;
    em4_err_t rc;

    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    rc = app_latest_pressure(&p);
    if (rc != EM4_OK) {
        return rc;
    }
    cmd_printf(out, "press=%lu Pa\n", (unsigned long)p);
    return EM4_OK;
}

static em4_err_t cmd_get_stats(int argc, char *argv[], cmd_out_t *out)
{
    static const char *const k_ch_name[TELEM_CH_COUNT] = { "temp", "hum", "press" };
    telem_summary_t sum;
    uint32_t        ch;
    em4_err_t       rc;

    EM4_UNUSED(argc);
    rc = str_to_u32(argv[2], &ch);
    if (rc != EM4_OK) {
        return rc;
    }
    if (ch >= TELEM_CH_COUNT) {
        cmd_printf(out, "channel must be 0..%u\n", TELEM_CH_COUNT - 1U);
        return EM4_ERR_RANGE;
    }
    rc = telemetry_get((uint8_t)ch, &sum);
    if (rc != EM4_OK) {
        return rc;
    }
    cmd_printf(out, "%s: n=%lu min=%ld max=%ld mean=%ld last=%ld alarms=%lu\n",
               k_ch_name[ch], (unsigned long)sum.count, (long)sum.min, (long)sum.max,
               (long)telemetry_mean((uint8_t)ch), (long)sum.last, (unsigned long)sum.alarms);
    return EM4_OK;
}

static em4_err_t cmd_set_rate(int argc, char *argv[], cmd_out_t *out)
{
    uint32_t  ms;
    int       id;
    em4_err_t rc;

    EM4_UNUSED(argc);
    id = sched_find(argv[2]);
    if (id < 0) {
        cmd_printf(out, "no such task: %s\n", argv[2]);
        return EM4_ERR_NOTFOUND;
    }
    rc = str_to_u32(argv[3], &ms);
    if (rc != EM4_OK) {
        return rc;
    }
    if ((ms < 50U) || (ms > 600000U)) {
        return EM4_ERR_RANGE;
    }
    return sched_set_period((uint8_t)id, ms);
}

static em4_err_t cmd_log_dump(int argc, char *argv[], cmd_out_t *out)
{
    uint32_t want = CMD_LOG_DUMP_DEFAULT;
    char    *buf;
    size_t   n;

    if (argc > 2) {
        if (str_to_u32(argv[2], &want) != EM4_OK) {
            return EM4_ERR_PARAM;
        }
        if ((want == 0U) || (want > CMD_LOG_DUMP_LIMIT)) {
            return EM4_ERR_RANGE;
        }
    }

    /* 保守コマンドなのでヒープを使ってよい（em4_config.h の設計メモ） */
    buf = (char *)malloc((size_t)want + 1U);
    (void)memset(buf, 0, (size_t)want + 1U);
    n = log_ring_copy_tail(buf, want);
    buf[n] = '\0';
    cmd_puts(out, buf);
    if ((n > 0U) && (buf[n - 1U] != '\n')) {
        cmd_puts(out, "\n");
    }
    free(buf);
    return EM4_OK;
}

static em4_err_t cmd_log_level(int argc, char *argv[], cmd_out_t *out)
{
    int lv;

    if (argc < 3) {
        cmd_printf(out, "level=%s\n", log_level_name(log_get_level()));
        return EM4_OK;
    }
    lv = log_level_from_str(argv[2]);
    if (lv < 0) {
        return EM4_ERR_PARAM;
    }
    log_set_level((log_level_t)lv);
    cmd_printf(out, "level=%s\n", log_level_name((log_level_t)lv));
    return EM4_OK;
}

static em4_err_t cmd_log_dir(int argc, char *argv[], cmd_out_t *out)
{
    em4_err_t rc;

    EM4_UNUSED(argc);
    rc = log_set_dir(argv[2]);
    if (rc == EM4_OK) {
        cmd_printf(out, "log dir=%s\n", argv[2]);
    }
    return rc;
}

static em4_err_t cmd_gpio_set(int argc, char *argv[], cmd_out_t *out)
{
    uint32_t  pin;
    bool      level;
    em4_err_t rc;

    EM4_UNUSED(argc);
    EM4_UNUSED(out);
    rc = str_to_u32(argv[2], &pin);
    if (rc != EM4_OK) {
        return rc;
    }
    rc = str_to_bool(argv[3], &level);
    if (rc != EM4_OK) {
        return rc;
    }
    /* ピンの番号の範囲は gpio_set が確かめる */
    return gpio_set((uint8_t)pin, level);
}

static em4_err_t cmd_gpio_get(int argc, char *argv[], cmd_out_t *out)
{
    uint32_t  pin;
    bool      level = false;
    em4_err_t rc;

    EM4_UNUSED(argc);
    rc = str_to_u32(argv[2], &pin);
    if (rc != EM4_OK) {
        return rc;
    }
    if (pin >= GPIO_PIN_COUNT) {
        return EM4_ERR_RANGE;
    }
    rc = gpio_get((uint8_t)pin, &level);
    if (rc != EM4_OK) {
        return rc;
    }
    cmd_printf(out, "gpio%lu=%d\n", (unsigned long)pin, level ? 1 : 0);
    return EM4_OK;
}

static em4_err_t cmd_cal_set(int argc, char *argv[], cmd_out_t *out)
{
    uint32_t  ch;
    int32_t   off;
    em4_err_t rc;

    EM4_UNUSED(argc);
    EM4_UNUSED(out);
    rc = str_to_u32(argv[2], &ch);
    if (rc != EM4_OK) {
        return rc;
    }
    rc = str_to_i32(argv[3], &off);
    if (rc != EM4_OK) {
        return rc;
    }
    if ((off < -1000) || (off > 1000)) {
        return EM4_ERR_RANGE;       /* ±10.00 まで */
    }
    return app_set_calibration((uint8_t)ch, off);
}

static em4_err_t cmd_cal_save(int argc, char *argv[], cmd_out_t *out)
{
    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    EM4_UNUSED(out);
    return app_save_calibration();
}

static em4_err_t cmd_flash_info(int argc, char *argv[], cmd_out_t *out)
{
    flash_image_hdr_t hdr;
    em4_err_t         rc;

    if (argc > 2) {
        rc = flash_store_load(argv[2], &hdr);
        if (rc != EM4_OK) {
            cmd_printf(out, "cannot read %s (%s)\n", argv[2], em4_strerror(rc));
            return rc;
        }
        cmd_printf(out, "image %s: layout=%u sectors=%u x %u boot=%lu\n", argv[2],
                   hdr.layout_ver, hdr.sector_count, hdr.sector_size, (unsigned long)hdr.boot_count);
        return EM4_OK;
    }
    if (!flash_store_mounted()) {
        return EM4_ERR_STATE;
    }
    cmd_printf(out, "mounted: boot=%lu rec_free=%lu\n", (unsigned long)flash_store_boot_count(),
               (unsigned long)flash_rec_free_bytes());
    return EM4_OK;
}

static em4_err_t cmd_echo(int argc, char *argv[], cmd_out_t *out)
{
    uint8_t buf[FRAME_PAYLOAD_MAX];
    size_t  n;

    EM4_UNUSED(argc);
    n = str_hex_decode(argv[1], buf, sizeof(buf));
    if (n == 0U) {
        return EM4_ERR_FORMAT;
    }
    cmd_printf(out, "echo %lu bytes on uplink\n", (unsigned long)n);
    return uplink_send_frame(FRAME_CMD_ECHO, buf, (uint8_t)n);
}

static em4_err_t cmd_health(int argc, char *argv[], cmd_out_t *out)
{
    char buf[256];

    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    app_get_health(buf, sizeof(buf));
    cmd_puts(out, buf);
    return EM4_OK;
}

static em4_err_t cmd_cfg(int argc, char *argv[], cmd_out_t *out)
{
    char buf[384];

    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    (void)config_dump(buf, sizeof(buf));
    cmd_puts(out, buf);
    return EM4_OK;
}

static em4_err_t cmd_reset(int argc, char *argv[], cmd_out_t *out)
{
    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    cmd_puts(out, "resetting\n");
    app_request_reset();
    return EM4_OK;
}

static em4_err_t cmd_help(int argc, char *argv[], cmd_out_t *out);

static const cmd_entry_t k_cmds[] = {
    { "VER",    NULL,    0U, 0U, cmd_ver,        "firmware version" },
    { "HELP",   NULL,    0U, 0U, cmd_help,       "this list" },
    { "GET",    "TEMP",  0U, 0U, cmd_get_temp,   "temperature (filtered)" },
    { "GET",    "HUM",   0U, 0U, cmd_get_hum,    "humidity (filtered)" },
    { "GET",    "PRESS", 0U, 0U, cmd_get_press,  "pressure (average)" },
    { "GET",    "STATS", 1U, 1U, cmd_get_stats,  "<ch> telemetry summary" },
    { "SET",    "RATE",  2U, 2U, cmd_set_rate,   "<task> <ms> task period" },
    { "LOG",    "DUMP",  0U, 1U, cmd_log_dump,   "[bytes] tail of the log ring" },
    { "LOG",    "LEVEL", 0U, 1U, cmd_log_level,  "[level] get/set log level" },
    { "LOG",    "DIR",   1U, 1U, cmd_log_dir,    "<dir> move log files (SD card)" },
    { "GPIO",   "SET",   2U, 2U, cmd_gpio_set,   "<pin> <0|1>" },
    { "GPIO",   "GET",   1U, 1U, cmd_gpio_get,   "<pin>" },
    { "CAL",    "SET",   2U, 2U, cmd_cal_set,    "<ch> <offset x100>" },
    { "CAL",    "SAVE",  0U, 0U, cmd_cal_save,   "store calibration to flash" },
    { "FLASH",  "INFO",  0U, 1U, cmd_flash_info, "[image] flash header" },
    { "ECHO",   NULL,    1U, 1U, cmd_echo,       "<hex> send echo frame on uplink" },
    { "HEALTH", NULL,    0U, 0U, cmd_health,     "sensor/link health" },
    { "CFG",    NULL,    0U, 0U, cmd_cfg,        "active configuration" },
    { "RESET",  NULL,    0U, 0U, cmd_reset,      "reboot" },
};

static em4_err_t cmd_help(int argc, char *argv[], cmd_out_t *out)
{
    size_t i;

    EM4_UNUSED(argc);
    EM4_UNUSED(argv);
    for (i = 0U; i < EM4_ARRAY_SIZE(k_cmds); i++) {
        cmd_printf(out, "  %-6s %-6s %s\n", k_cmds[i].verb,
                   (k_cmds[i].object != NULL) ? k_cmds[i].object : "", k_cmds[i].help);
    }
    return EM4_OK;
}

static const cmd_entry_t *cmd_lookup(const char *verb, const char *object)
{
    size_t i;

    for (i = 0U; i < EM4_ARRAY_SIZE(k_cmds); i++) {
        const cmd_entry_t *e = &k_cmds[i];
        if (str_casecmp(e->verb, verb) != 0) {
            continue;
        }
        if (e->object == NULL) {
            return e;
        }
        if ((object != NULL) && (str_casecmp(e->object, object) == 0)) {
            return e;
        }
    }
    return NULL;
}

em4_err_t cmd_execute(char *line, cmd_out_t *out)
{
    char              *argv[CMD_MAX_ARGS];
    const cmd_entry_t *e;
    int                argc;
    int                nargs;
    em4_err_t          rc;

    if ((line == NULL) || (out == NULL)) {
        return EM4_ERR_PARAM;
    }
    argc = cmd_tokenize(line, argv, (int)CMD_MAX_ARGS);
    if (argc == 0) {
        return EM4_OK;      /* 空の行 */
    }
    s_exec_count++;

    e = cmd_lookup(argv[0], (argc > 1) ? argv[1] : NULL);
    if (e == NULL) {
        cmd_printf(out, "ERR unknown command (try HELP)\n");
        return EM4_ERR_NOTFOUND;
    }
    nargs = argc - ((e->object != NULL) ? 2 : 1);
    if ((nargs < (int)e->min_args) || (nargs > (int)e->max_args)) {
        cmd_printf(out, "ERR usage: %s %s %s\n", e->verb, (e->object != NULL) ? e->object : "",
                   e->help);
        return EM4_ERR_PARAM;
    }

    rc = e->fn(argc, argv, out);
    if (rc == EM4_OK) {
        cmd_puts(out, "OK\n");
    } else {
        cmd_printf(out, "ERR %s\n", em4_strerror(rc));
    }
    return rc;
}

uint32_t cmd_exec_count(void)
{
    return s_exec_count;
}
