/**
 * @file    util.h
 * @brief   共通の補助（リングバッファ、文字列）
 */
#ifndef EM4_UTIL_H
#define EM4_UTIL_H

#include "em4_types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ===================================================================== */
/* リングバッファ（バイト単位、呼び出し側が領域を用意する）              */
/* ===================================================================== */
typedef struct {
    uint8_t *buf;
    size_t   cap;
    size_t   head;      /* 次に書く位置 */
    size_t   tail;      /* 次に読む位置 */
    size_t   count;
    uint32_t dropped;   /* 上書きで捨てたバイト数 */
} ringbuf_t;

em4_err_t rb_init(ringbuf_t *rb, uint8_t *mem, size_t cap);
void      rb_clear(ringbuf_t *rb);
em4_err_t rb_push(ringbuf_t *rb, uint8_t b);
em4_err_t rb_pop(ringbuf_t *rb, uint8_t *out);
/* overwrite が true なら、いっぱいのときに古いものを捨てて書く */
size_t    rb_write(ringbuf_t *rb, const uint8_t *data, size_t len, bool overwrite);
size_t    rb_read(ringbuf_t *rb, uint8_t *dst, size_t max);
/* 新しい方から n バイトを、古い順に dst へ写す（取り出さない） */
size_t    rb_peek_tail(const ringbuf_t *rb, uint8_t *dst, size_t n);
size_t    rb_count(const ringbuf_t *rb);
size_t    rb_space(const ringbuf_t *rb);

/* ===================================================================== */
/* 文字列                                                                */
/* ===================================================================== */
char     *str_trim(char *s);
bool      str_is_kv_line(const char *s);
int       str_casecmp(const char *a, const char *b);
bool      str_starts_with(const char *s, const char *prefix);
size_t    str_copy(char *dst, size_t dst_size, const char *src);
char     *str_dup(const char *s);
uint32_t  str_hash(const char *s);
em4_err_t str_to_u32(const char *s, uint32_t *out);
em4_err_t str_to_i32(const char *s, int32_t *out);
em4_err_t str_to_bool(const char *s, bool *out);
size_t    str_hex_decode(const char *hex, uint8_t *out, size_t max);
size_t    str_hex_encode(const uint8_t *in, size_t n, char *out, size_t out_size);
void      str_upper(char *s);

#ifdef __cplusplus
}
#endif

#endif /* EM4_UTIL_H */
