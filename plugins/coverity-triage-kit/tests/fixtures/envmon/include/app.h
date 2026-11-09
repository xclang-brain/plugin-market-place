/**
 * @file    app.h
 * @brief   アプリケーション層（設定、ログ、周期処理、本体）
 */
#ifndef EM4_APP_H
#define EM4_APP_H

#include "em4_types.h"
#include "em4_config.h"
#include "drivers.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ===================================================================== */
/* ログ                                                                  */
/* ===================================================================== */
typedef enum {
    LOG_ERROR = 0,
    LOG_WARN,
    LOG_INFO,
    LOG_DEBUG,
    LOG_TRACE,
    LOG_LEVEL_COUNT
} log_level_t;

em4_err_t   log_init(const char *dir, log_level_t level);
void        log_shutdown(void);
em4_err_t   log_set_dir(const char *dir);
void        log_set_level(log_level_t lv);
log_level_t log_get_level(void);
/* 名前（"info" など、大文字小文字は問わない）から段階を返す。見つからなければ -1 */
int         log_level_from_str(const char *s);
const char *log_level_name(log_level_t lv);
void        log_printf(log_level_t lv, const char *tag, const char *fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 3, 4)))
#endif
    ;
void        log_flush(void);
size_t      log_ring_copy_tail(char *dst, size_t max);
void        log_get_counts(uint32_t *n_error, uint32_t *n_warn_or_worse, uint32_t *n_other);

#define LOG_E(tag, ...)  log_printf(LOG_ERROR, (tag), __VA_ARGS__)
#define LOG_W(tag, ...)  log_printf(LOG_WARN,  (tag), __VA_ARGS__)
#define LOG_I(tag, ...)  log_printf(LOG_INFO,  (tag), __VA_ARGS__)
#define LOG_D(tag, ...)  log_printf(LOG_DEBUG, (tag), __VA_ARGS__)
#define LOG_T(tag, ...)  log_printf(LOG_TRACE, (tag), __VA_ARGS__)

/* ===================================================================== */
/* 設定                                                                  */
/* ===================================================================== */
typedef struct {
    /* [device] */
    char      device_name[CFG_NAME_MAX];
    uint16_t  device_id;
    /* [sampling] */
    uint32_t  sample_period_ms;     /* 100 .. 60000 */
    uint32_t  uplink_period_ms;     /* 1000 .. 600000 */
    uint32_t  telemetry_window;     /* 1 .. 600 */
    /* [sensor] */
    uint8_t   sht_bus;
    uint8_t   sht_addr;
    uint8_t   sht_retries;          /* 0 .. 5 */
    bool      sht_heater;
    bmp_osr_t bmp_osr_temp;
    bmp_osr_t bmp_osr_press;
    int32_t   temp_offset_c100;     /* 校正値 */
    int32_t   hum_offset_c100;
    /* [log] */
    log_level_t log_level;
    char      log_dir[LOG_DIR_MAX];
    /* [uplink] */
    bool      uplink_enabled;
    uint8_t   uplink_retries;
    /* 版数（読み込むたびに 1 増やす） */
    uint32_t  revision;
} em4_config_t;

void      config_set_defaults(em4_config_t *cfg);
em4_err_t config_load(const char *path, em4_config_t *cfg);
em4_err_t config_init(const char *path);
const em4_config_t *config_get(void);
size_t    config_dump(char *buf, size_t max);

/* ===================================================================== */
/* 周期処理                                                              */
/* ===================================================================== */
typedef em4_err_t (*sched_fn_t)(void *ctx);

typedef struct {
    const char *name;
    sched_fn_t  fn;
    void       *ctx;
    uint32_t    period_ms;
} sched_def_t;

typedef struct {
    const char *name;
    uint32_t    period_ms;
    uint32_t    runs;
    uint32_t    errors;
    uint32_t    overruns;
    uint32_t    last_ms;
} sched_info_t;

void      sched_init(void);
/* タスクを登録する。起動時に main.c から呼ぶ */
em4_err_t sched_register(const sched_def_t *def, uint8_t *out_id);
em4_err_t sched_set_period(uint8_t id, uint32_t period_ms);
int       sched_find(const char *name);
em4_err_t sched_get_info(uint8_t id, sched_info_t *out);
uint32_t  sched_run_due(uint32_t now_ms);
uint8_t   sched_count(void);

/* ===================================================================== */
/* 本体（main.c）がコマンドなどに出す口                                  */
/* ===================================================================== */
em4_err_t app_latest_climate(sht_sample_t *out);
em4_err_t app_latest_pressure(uint32_t *press_pa);
em4_err_t app_set_calibration(uint8_t ch, int32_t offset_c100);
em4_err_t app_save_calibration(void);
void      app_get_health(char *buf, size_t max);
em4_err_t uplink_send_frame(uint8_t cmd, const uint8_t *payload, uint8_t len);
void      app_request_reset(void);

#ifdef __cplusplus
}
#endif

#endif /* EM4_APP_H */
