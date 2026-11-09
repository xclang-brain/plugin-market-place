/**
 * @file    proto.h
 * @brief   通信の手順（CRC、上りリンクのフレーム、保守用の文字のコマンド）
 */
#ifndef EM4_PROTO_H
#define EM4_PROTO_H

#include "em4_types.h"
#include "em4_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ===================================================================== */
/* CRC                                                                   */
/* ===================================================================== */
/* CRC16-CCITT（多項式 0x1021、初期値 0xFFFF、反転なし） */
uint16_t crc16_ccitt(const uint8_t *data, size_t len);
uint16_t crc16_ccitt_update(uint16_t crc, const uint8_t *data, size_t len);
/* Sensirion の CRC8（多項式 0x31、初期値 0xFF） */
uint8_t  crc8_sensirion(const uint8_t *data, size_t len);

/* ===================================================================== */
/* フレーム                                                              */
/* ===================================================================== */
typedef struct {
    uint8_t cmd;
    uint8_t seq;
    uint8_t len;
    uint8_t payload[FRAME_PAYLOAD_MAX];
} frame_t;

typedef enum {
    FDEC_SOF = 0,
    FDEC_LEN,
    FDEC_CMD,
    FDEC_SEQ,
    FDEC_PAYLOAD,
    FDEC_CRC_H,
    FDEC_CRC_L
} fdec_state_t;

typedef struct {
    fdec_state_t st;
    uint8_t      buf[FRAME_MAX];
    size_t       pos;
    uint8_t      need;
    uint32_t     last_byte_ms;
    uint32_t     n_ok;
    uint32_t     n_crc_err;
    uint32_t     n_len_err;
    uint32_t     n_timeout;
} frame_dec_t;

#define FRAME_DEC_TIMEOUT_MS    (200U)

/* フレームを組み立てる。out の長さを返す。失敗は -1 */
int       frame_encode(uint8_t out[FRAME_MAX], const frame_t *f);
void      frame_dec_init(frame_dec_t *d);
/* 1 バイトずつ渡す。1: フレームがそろった、0: 続きを待つ、負: 誤り */
int       frame_dec_push(frame_dec_t *d, uint8_t b, uint32_t now_ms, frame_t *out);

/* ペイロードを組み立てる補助（ビッグエンディアン） */
typedef struct {
    frame_t *f;
    bool     overflow;
} frame_writer_t;

void      fw_begin(frame_writer_t *w, frame_t *f, uint8_t cmd, uint8_t seq);
void      fw_put_u8(frame_writer_t *w, uint8_t v);
void      fw_put_u16(frame_writer_t *w, uint16_t v);
void      fw_put_u32(frame_writer_t *w, uint32_t v);
void      fw_put_i32(frame_writer_t *w, int32_t v);
void      fw_put_bytes(frame_writer_t *w, const uint8_t *p, size_t n);
em4_err_t fw_end(const frame_writer_t *w);

/* ===================================================================== */
/* 保守用のコマンド（文字）                                              */
/* ===================================================================== */
typedef struct {
    char   buf[CMD_OUT_MAX];
    size_t len;
    bool   truncated;
} cmd_out_t;

void      cmd_out_init(cmd_out_t *out);
void      cmd_printf(cmd_out_t *out, const char *fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 2, 3)))
#endif
    ;
void      cmd_puts(cmd_out_t *out, const char *s);

int       cmd_tokenize(char *line, char *argv[], int max_args);
/* 1 行を解釈して実行する。結果の文字列は out に入る */
em4_err_t cmd_execute(char *line, cmd_out_t *out);
uint32_t  cmd_exec_count(void);

#ifdef __cplusplus
}
#endif

#endif /* EM4_PROTO_H */
