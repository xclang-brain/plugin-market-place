/**
 * @file    em4_config.h
 * @brief   ビルド時に決まる定数（バッファの大きさ、周期、版数）
 *
 * 設計メモ（メモリ）
 *   - バッファは原則として固定長の配列で持つ。
 *   - 動的メモリ（malloc / free）は、起動時の初期化、設定ファイルの読み込み、
 *     保守コマンドの処理に限って使う。周期処理（sched の各タスク）では使わない。
 *   - C++ の telemetry は起動時に一度だけ new (std::nothrow) で確保する。
 */
#ifndef EM4_CONFIG_H
#define EM4_CONFIG_H

/* ---- 版数 ---- */
#define EM4_FW_VERSION_MAJOR    (2U)
#define EM4_FW_VERSION_MINOR    (3U)
#define EM4_FW_VERSION_PATCH    (1U)
#define EM4_FW_VERSION_STR      "2.3.1"
#define EM4_HW_REVISION_STR     "EM-4 rev.C"

/* ---- 時間 ---- */
#define TIMER_TICK_MS           (10U)       /* SysTick の周期 */
#define TIMER_SW_MAX            (4U)        /* ソフトウェアタイマーの数 */
#define WDT_TIMEOUT_MS          (2000U)

/*
 * ---- 上りリンクのフレーム ----
 *
 *   +-----+-----+-----+-----+------------------+-------+-------+
 *   | SOF | LEN | CMD | SEQ | PAYLOAD (LEN 個) | CRC_H | CRC_L |
 *   +-----+-----+-----+-----+------------------+-------+-------+
 *
 *   CRC16-CCITT は LEN から PAYLOAD の最後までを対象にする。
 */
#define FRAME_SOF               (0xA5U)
#define FRAME_HDR_LEN           (4U)        /* SOF, LEN, CMD, SEQ */
#define FRAME_CRC_LEN           (2U)
#define FRAME_PAYLOAD_MAX       (64U)
/* 1 フレームの最大の長さ。送受信のバッファの大きさに使う */
#define FRAME_MAX               (FRAME_HDR_LEN + FRAME_PAYLOAD_MAX)

/* フレームの CMD */
#define FRAME_CMD_TELEMETRY     (0x10U)
#define FRAME_CMD_EVENT         (0x11U)
#define FRAME_CMD_ECHO          (0x20U)
#define FRAME_CMD_ACK           (0x7EU)
#define FRAME_CMD_NAK           (0x7FU)

/* ---- センサー ---- */
#define SHT_I2C_BUS             (0U)
#define SHT_I2C_ADDR            (0x44U)
#define SHT_AVG_DEPTH           (8U)        /* 移動平均の段数 */
#define SHT_RETRIES_DEFAULT     (2U)
#define SHT_RETRIES_LIMIT       (5U)

#define BMP_SPI_BUS             (0U)
#define BMP_SPI_CS              (0U)
#define BMP_AVG_DEPTH           (8U)

/* ---- GPIO ---- */
#define GPIO_PIN_COUNT          (16U)
#define GPIO_PIN_LED_STATUS     (0U)
#define GPIO_PIN_LED_FAULT      (1U)
#define GPIO_PIN_SENSOR_PWR     (2U)
#define GPIO_PIN_DOOR_SW        (8U)        /* 筐体の扉スイッチ（割り込み） */
#define GPIO_PIN_TAMPER         (9U)

/* ---- スケジューラー ---- */
#define SCHED_MAX_TASKS         (8U)

/* ---- ログ ---- */
#define LOG_LINE_MAX            (128U)
#define LOG_RING_SIZE           (4096U)
#define LOG_DIR_MAX             (48U)
#define LOG_PATH_MAX            (64U)
#define LOG_ROTATE_BYTES        (16384UL)
#define LOG_KEEP_FILES          (4U)
#define LOG_FILE_BASENAME       "em4.log"

/* ---- 設定ファイル（ini） ---- */
#define INI_LINE_MAX            (128U)
#define INI_KEY_MAX             (32U)
#define INI_VAL_MAX             (64U)
#define INI_MAX_SECTIONS        (12U)
#define INI_MAX_ENTRIES         (64U)
#define CFG_NAME_MAX            (16U)       /* 機器名（表示とフレームに使う） */

/* ---- フラッシュ（ホストではファイルで模擬） ---- */
#define FLASH_SECTOR_SIZE       (4096U)
#define FLASH_SECTOR_COUNT      (16U)
#define FLASH_TOTAL_SIZE        (FLASH_SECTOR_SIZE * FLASH_SECTOR_COUNT)
#define FLASH_PROGRAM_UNIT      (4U)

/* ---- コマンド（保守用のコンソール） ---- */
#define CMD_LINE_MAX            (96U)
#define CMD_MAX_ARGS            (6U)
#define CMD_OUT_MAX             (1024U)
#define CMD_LOG_DUMP_DEFAULT    (256U)
#define CMD_LOG_DUMP_LIMIT      (2048U)

/* ---- テレメトリー ---- */
#define TELEM_WINDOW_DEFAULT    (60U)       /* 平均をとるサンプルの数 */

#endif /* EM4_CONFIG_H */
