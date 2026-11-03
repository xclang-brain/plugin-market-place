/**
 * @file    fault.c
 * @brief   回復できない故障の処理と、エラーコードの文字列
 *
 * fatal_error() は呼び出し元に戻らない。
 *   1. 故障コードを記録する（実機ではバックアップ SRAM。リセットしても消えない）
 *   2. 故障の LED を点ける
 *   3. platform_reset() で機器をリセットする（ホストではプロセスを終える）
 *   4. 万一リセットできなかったときは、ウォッチドッグがリセットするまでここで止まる
 */
#include <stdio.h>

#include "em4_types.h"
#include "hal.h"

typedef struct {
    em4_fault_t code;
    uint32_t    info;
    uint32_t    uptime_ms;
    uint32_t    count;
} fault_record_t;

/* 実機ではバックアップ SRAM（.noinit）に置く */
static volatile fault_record_t s_fault_rec;

static const char *fault_name(em4_fault_t code)
{
    switch (code) {
    case FAULT_NONE:         return "none";
    case FAULT_ASSERT:       return "assert";
    case FAULT_FLASH_LAYOUT: return "flash-layout";
    case FAULT_CONFIG:       return "config";
    case FAULT_SENSOR:       return "sensor";
    case FAULT_SCHED:        return "sched";
    case FAULT_STACK:        return "stack";
    case FAULT_WATCHDOG:     return "watchdog";
    case FAULT_TELEMETRY:    return "telemetry";
    default:                 return "unknown";
    }
}

void fatal_error(em4_fault_t code, uint32_t info)
{
    em4_irq_disable();

    s_fault_rec.code      = code;
    s_fault_rec.info      = info;
    s_fault_rec.uptime_ms = timer_now_ms();
    s_fault_rec.count     = s_fault_rec.count + 1U;

    /* ログの仕組みが壊れているかもしれないので、標準エラーに直接書く */
    (void)fprintf(stderr, "FATAL: %s (code=%d info=0x%08lX) at %lu ms\n", fault_name(code),
                  (int)code, (unsigned long)info, (unsigned long)s_fault_rec.uptime_ms);

    (void)gpio_set(GPIO_PIN_LED_FAULT, true);
    platform_reset();

    /* ここには来ない。来たらウォッチドッグのリセットを待つ（wdt_kick はしない） */
    for (;;) {
    }
}

void em4_assert_failed(const char *file, int line)
{
    (void)fprintf(stderr, "ASSERT failed: %s:%d\n", (file != NULL) ? file : "?", line);
    fatal_error(FAULT_ASSERT, (uint32_t)line);
}

const char *em4_strerror(em4_err_t err)
{
    switch (err) {
    case EM4_OK:           return "ok";
    case EM4_ERR_PARAM:    return "bad parameter";
    case EM4_ERR_IO:       return "i/o error";
    case EM4_ERR_TIMEOUT:  return "timeout";
    case EM4_ERR_NOMEM:    return "out of memory";
    case EM4_ERR_CRC:      return "crc error";
    case EM4_ERR_FULL:     return "full";
    case EM4_ERR_EMPTY:    return "empty";
    case EM4_ERR_NOTFOUND: return "not found";
    case EM4_ERR_FORMAT:   return "format error";
    case EM4_ERR_STATE:    return "bad state";
    case EM4_ERR_RANGE:    return "out of range";
    case EM4_ERR_BUSY:     return "busy";
    default:               return "unknown error";
    }
}
