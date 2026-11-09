/**
 * @file    em4_types.h
 * @brief   EM-4 ファームウェア共通の型、エラーコード、補助マクロ
 *
 * EM-4 environment monitor firmware.
 * Copyright (c) 2023-2026 EM-4 firmware team. All rights reserved.
 */
#ifndef EM4_TYPES_H
#define EM4_TYPES_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * エラーコード
 *   0 が成功、負の値が失敗。関数は原則としてこの型を戻り値で返す。
 */
typedef enum {
    EM4_OK           = 0,
    EM4_ERR_PARAM    = -1,   /* 引数の誤り */
    EM4_ERR_IO       = -2,   /* バス・ファイルの入出力の失敗 */
    EM4_ERR_TIMEOUT  = -3,
    EM4_ERR_NOMEM    = -4,
    EM4_ERR_CRC      = -5,
    EM4_ERR_FULL     = -6,
    EM4_ERR_EMPTY    = -7,
    EM4_ERR_NOTFOUND = -8,
    EM4_ERR_FORMAT   = -9,
    EM4_ERR_STATE    = -10,  /* 初期化前など、状態の誤り */
    EM4_ERR_RANGE    = -11,
    EM4_ERR_BUSY     = -12
} em4_err_t;

/* ---- 補助マクロ ---- */
#define EM4_ARRAY_SIZE(a)   (sizeof(a) / sizeof((a)[0]))
#define EM4_MIN(a, b)       (((a) < (b)) ? (a) : (b))
#define EM4_MAX(a, b)       (((a) > (b)) ? (a) : (b))
#define EM4_BIT(n)          (1UL << (n))
#define EM4_UNUSED(x)       ((void)(x))

/* 16 ビット値の上位・下位のバイト（ビッグエンディアンの送受信用） */
#define EM4_HI8(v)          ((uint8_t)(((uint16_t)(v) >> 8) & 0xFFU))
#define EM4_LO8(v)          ((uint8_t)((uint16_t)(v) & 0xFFU))
#define EM4_MK16(hi, lo)    ((uint16_t)(((uint16_t)(hi) << 8) | (uint16_t)(lo)))

/* 4 バイトの境界に切り上げる */
#define EM4_ALIGN4(n)       (((n) + 3U) & ~3U)

/*
 * 故障コード（fatal_error に渡す）
 * 値は故障記録の領域にそのまま残るので、途中に追加しないこと。
 */
typedef enum {
    FAULT_NONE         = 0,
    FAULT_ASSERT       = 1,
    FAULT_FLASH_LAYOUT = 2,
    FAULT_CONFIG       = 3,
    FAULT_SENSOR       = 4,
    FAULT_SCHED        = 5,
    FAULT_STACK        = 6,
    FAULT_WATCHDOG     = 7,
    FAULT_TELEMETRY    = 8
} em4_fault_t;

/*
 * 回復できない故障を記録し、機器をリセットする。
 * 実装は src/app/fault.c。
 */
void fatal_error(em4_fault_t code, uint32_t info);

/* EM4_ASSERT が失敗したときに呼ばれる（src/app/fault.c） */
void em4_assert_failed(const char *file, int line);

/*
 * リリースビルドでも有効なアサート。
 * 標準の assert() と違い、NDEBUG を定義しても消えない。
 */
#define EM4_ASSERT(cond)                                \
    do {                                                \
        if (!(cond)) {                                  \
            em4_assert_failed(__FILE__, __LINE__);      \
        }                                               \
    } while (0)

/* エラーコードを短い文字列にする（ログ用） */
const char *em4_strerror(em4_err_t err);

/* 割り込みの禁止・許可（ホストでは入れ子の数を数えるだけ） */
void em4_irq_disable(void);
void em4_irq_enable(void);

#ifdef __cplusplus
}
#endif

#endif /* EM4_TYPES_H */
