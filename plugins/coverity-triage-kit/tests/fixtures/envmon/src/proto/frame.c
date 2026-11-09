/**
 * @file    frame.c
 * @brief   上りリンクのフレームの組み立てと分解
 *
 * フレームの形は em4_config.h を参照。
 * 受信は 1 バイトずつ frame_dec_push() に渡す状態機械で行う。
 * バイトの間が FRAME_DEC_TIMEOUT_MS 以上あいたら、途中まで受けたものは捨てる。
 */
#include <string.h>

#include "proto.h"

/* ===================================================================== */
/* 組み立て                                                              */
/* ===================================================================== */
int frame_encode(uint8_t out[FRAME_MAX], const frame_t *f)
{
    uint16_t crc;
    size_t   pos = 0U;

    if ((out == NULL) || (f == NULL)) {
        return -1;
    }
    if (f->len > FRAME_PAYLOAD_MAX) {
        return -1;
    }

    out[pos++] = FRAME_SOF;
    out[pos++] = f->len;
    out[pos++] = f->cmd;
    out[pos++] = f->seq;
    if (f->len > 0U) {
        (void)memcpy(&out[pos], f->payload, f->len);
        pos += f->len;
    }

    /* CRC は LEN からペイロードの最後まで */
    crc = crc16_ccitt(&out[1], pos - 1U);
    out[pos++] = EM4_HI8(crc);
    out[pos++] = EM4_LO8(crc);

    return (int)pos;
}

/* ===================================================================== */
/* 分解                                                                  */
/* ===================================================================== */
static void frame_dec_reset(frame_dec_t *d)
{
    d->st   = FDEC_SOF;
    d->pos  = 0U;
    d->need = 0U;
}

void frame_dec_init(frame_dec_t *d)
{
    if (d == NULL) {
        return;
    }
    (void)memset(d, 0, sizeof(*d));
    frame_dec_reset(d);
}

int frame_dec_push(frame_dec_t *d, uint8_t b, uint32_t now_ms, frame_t *out)
{
    int result = 0;

    if ((d == NULL) || (out == NULL)) {
        return -1;
    }

    if ((d->st != FDEC_SOF) && ((now_ms - d->last_byte_ms) > FRAME_DEC_TIMEOUT_MS)) {
        d->n_timeout++;
        frame_dec_reset(d);
    }
    d->last_byte_ms = now_ms;

    switch (d->st) {
    case FDEC_SOF:
        if (b == FRAME_SOF) {
            d->buf[0] = b;
            d->pos    = 1U;
            d->st     = FDEC_LEN;
        }
        break;

    case FDEC_LEN:
        if (b > FRAME_PAYLOAD_MAX) {
            d->n_len_err++;
            frame_dec_reset(d);
            result = -1;
            break;
        }
        d->buf[d->pos++] = b;
        d->need = b;
        d->st   = FDEC_CMD;
        break;

    case FDEC_CMD:
        d->buf[d->pos++] = b;
        d->st = FDEC_SEQ;
        break;

    case FDEC_SEQ:
        d->buf[d->pos++] = b;
        d->st = (d->need > 0U) ? FDEC_PAYLOAD : FDEC_CRC_H;
        break;

    case FDEC_PAYLOAD:
        d->buf[d->pos++] = b;
        d->need--;
        if (d->need == 0U) {
            d->st = FDEC_CRC_H;
        }
        break;

    case FDEC_CRC_H:
        d->buf[d->pos++] = b;
        d->st = FDEC_CRC_L;
        break;

    case FDEC_CRC_L: {
        uint16_t crc_rx;
        uint16_t crc_calc;
        uint8_t  len;

        d->buf[d->pos++] = b;
        crc_rx   = EM4_MK16(d->buf[d->pos - 2U], d->buf[d->pos - 1U]);
        crc_calc = crc16_ccitt(&d->buf[1], d->pos - 3U);
        if (crc_rx != crc_calc) {
            d->n_crc_err++;
            frame_dec_reset(d);
            result = -1;
            break;
        }
        len      = d->buf[1];
        out->len = len;
        out->cmd = d->buf[2];
        out->seq = d->buf[3];
        (void)memcpy(out->payload, &d->buf[FRAME_HDR_LEN], len);
        d->n_ok++;
        frame_dec_reset(d);
        result = 1;
        break;
    }

    default:
        frame_dec_reset(d);
        break;
    }
    return result;
}

/* ===================================================================== */
/* ペイロードの組み立ての補助                                            */
/* ===================================================================== */
void fw_begin(frame_writer_t *w, frame_t *f, uint8_t cmd, uint8_t seq)
{
    w->f        = f;
    w->overflow = false;
    f->cmd = cmd;
    f->seq = seq;
    f->len = 0U;
}

static bool fw_room(frame_writer_t *w, size_t n)
{
    if (((size_t)w->f->len + n) > FRAME_PAYLOAD_MAX) {
        w->overflow = true;
        return false;
    }
    return true;
}

void fw_put_u8(frame_writer_t *w, uint8_t v)
{
    if (fw_room(w, 1U)) {
        w->f->payload[w->f->len] = v;
        w->f->len++;
    }
}

void fw_put_u16(frame_writer_t *w, uint16_t v)
{
    if (fw_room(w, 2U)) {
        w->f->payload[w->f->len]      = EM4_HI8(v);
        w->f->payload[w->f->len + 1U] = EM4_LO8(v);
        w->f->len = (uint8_t)(w->f->len + 2U);
    }
}

void fw_put_u32(frame_writer_t *w, uint32_t v)
{
    fw_put_u16(w, (uint16_t)(v >> 16));
    fw_put_u16(w, (uint16_t)(v & 0xFFFFU));
}

void fw_put_i32(frame_writer_t *w, int32_t v)
{
    fw_put_u32(w, (uint32_t)v);
}

void fw_put_bytes(frame_writer_t *w, const uint8_t *p, size_t n)
{
    if ((p != NULL) && fw_room(w, n)) {
        (void)memcpy(&w->f->payload[w->f->len], p, n);
        w->f->len = (uint8_t)(w->f->len + n);
    }
}

em4_err_t fw_end(const frame_writer_t *w)
{
    return w->overflow ? EM4_ERR_FULL : EM4_OK;
}
