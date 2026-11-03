/**
 * @file    ringbuf.c
 * @brief   バイトのリングバッファ
 *
 * 領域は呼び出し側が用意する（ヒープは使わない）。
 * 割り込みと本体の両方から触るときは、呼び出し側で割り込みを止めること。
 */
#include <string.h>

#include "util.h"

em4_err_t rb_init(ringbuf_t *rb, uint8_t *mem, size_t cap)
{
    if ((rb == NULL) || (mem == NULL) || (cap == 0U)) {
        return EM4_ERR_PARAM;
    }
    rb->buf     = mem;
    rb->cap     = cap;
    rb->head    = 0U;
    rb->tail    = 0U;
    rb->count   = 0U;
    rb->dropped = 0U;
    return EM4_OK;
}

void rb_clear(ringbuf_t *rb)
{
    if (rb != NULL) {
        rb->head  = 0U;
        rb->tail  = 0U;
        rb->count = 0U;
    }
}

em4_err_t rb_push(ringbuf_t *rb, uint8_t b)
{
    EM4_ASSERT((rb != NULL) && (rb->buf != NULL));

    if (rb->count >= rb->cap) {
        return EM4_ERR_FULL;
    }
    rb->buf[rb->head] = b;
    rb->head = (rb->head + 1U) % rb->cap;
    rb->count++;
    return EM4_OK;
}

em4_err_t rb_pop(ringbuf_t *rb, uint8_t *out)
{
    if ((rb == NULL) || (out == NULL)) {
        return EM4_ERR_PARAM;
    }
    if (rb->count == 0U) {
        return EM4_ERR_EMPTY;
    }
    *out = rb->buf[rb->tail];
    rb->tail = (rb->tail + 1U) % rb->cap;
    rb->count--;
    return EM4_OK;
}

size_t rb_write(ringbuf_t *rb, const uint8_t *data, size_t len, bool overwrite)
{
    size_t done = 0U;

    if ((rb == NULL) || (data == NULL)) {
        return 0U;
    }
    while (done < len) {
        size_t chunk;

        if (rb->count == rb->cap) {
            if (!overwrite) {
                break;
            }
            /* 古い 1 バイトを捨てる */
            rb->tail = (rb->tail + 1U) % rb->cap;
            rb->count--;
            rb->dropped++;
        }
        /* 折り返しまで、または空きの分だけまとめて写す */
        chunk = EM4_MIN(len - done, rb->cap - rb->head);
        chunk = EM4_MIN(chunk, rb->cap - rb->count);
        (void)memcpy(&rb->buf[rb->head], &data[done], chunk);
        rb->head   = (rb->head + chunk) % rb->cap;
        rb->count += chunk;
        done      += chunk;
    }
    return done;
}

size_t rb_read(ringbuf_t *rb, uint8_t *dst, size_t max)
{
    size_t done = 0U;

    if ((rb == NULL) || (dst == NULL)) {
        return 0U;
    }
    while ((done < max) && (rb->count > 0U)) {
        size_t chunk = EM4_MIN(max - done, rb->cap - rb->tail);
        chunk = EM4_MIN(chunk, rb->count);
        (void)memcpy(&dst[done], &rb->buf[rb->tail], chunk);
        rb->tail   = (rb->tail + chunk) % rb->cap;
        rb->count -= chunk;
        done      += chunk;
    }
    return done;
}

size_t rb_peek_tail(const ringbuf_t *rb, uint8_t *dst, size_t n)
{
    size_t start;
    size_t i;

    if ((rb == NULL) || (dst == NULL)) {
        return 0U;
    }
    if (n > rb->count) {
        n = rb->count;
    }
    /* 新しい方から n バイト前の位置 */
    start = (rb->head + rb->cap - n) % rb->cap;
    for (i = 0U; i < n; i++) {
        dst[i] = rb->buf[(start + i) % rb->cap];
    }
    return n;
}

size_t rb_count(const ringbuf_t *rb)
{
    return (rb != NULL) ? rb->count : 0U;
}

size_t rb_space(const ringbuf_t *rb)
{
    return (rb != NULL) ? (rb->cap - rb->count) : 0U;
}
