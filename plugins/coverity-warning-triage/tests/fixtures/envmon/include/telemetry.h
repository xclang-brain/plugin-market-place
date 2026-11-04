/**
 * @file    telemetry.h
 * @brief   テレメトリーの集計（C++ で実装、C から呼ぶ口）
 *
 * 実装は src/telemetry/。例外と RTTI は使わない（-fno-exceptions -fno-rtti）。
 */
#ifndef EM4_TELEMETRY_H
#define EM4_TELEMETRY_H

#include "em4_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* チャンネル */
#define TELEM_CH_TEMP       (0U)    /* 0.01 °C */
#define TELEM_CH_HUM        (1U)    /* 0.01 %RH */
#define TELEM_CH_PRESS      (2U)    /* Pa */
#define TELEM_CH_COUNT      (3U)

typedef struct {
    uint32_t count;
    int32_t  min;
    int32_t  max;
    int32_t  mean;
    int32_t  last;
    uint32_t last_ts_ms;
    uint32_t alarms;
} telem_summary_t;

em4_err_t telemetry_init(uint32_t window);
void      telemetry_shutdown(void);
em4_err_t telemetry_add(uint8_t ch, int32_t value, uint32_t ts_ms);
/* 平均。ch は TELEM_CH_COUNT 未満であること */
int32_t   telemetry_mean(uint8_t ch);
em4_err_t telemetry_get(uint8_t ch, telem_summary_t *out);
em4_err_t telemetry_set_alarm(uint8_t ch, int32_t lo, int32_t hi);
/* 上りリンクのペイロードに詰める。詰めたバイト数を返す */
size_t    telemetry_pack(uint8_t *buf, size_t max);
void      telemetry_reset(void);

#ifdef __cplusplus
}
#endif

#endif /* EM4_TELEMETRY_H */
