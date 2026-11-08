/**
 * @file    strutil.c
 * @brief   文字列の補助
 *
 * 標準ライブラリの strcasecmp / strdup は、使える処理系が限られるので自前で持つ。
 */
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

/* 前後の空白（改行を含む）を取る。s そのものを書き換え、先頭の位置を返す */
char *str_trim(char *s)
{
    char  *end;
    size_t len;

    if (s == NULL) {
        return NULL;
    }
    while ((*s != '\0') && (isspace((unsigned char)*s) != 0)) {
        s++;
    }
    len = strlen(s);
    if (len == 0U) {
        return s;
    }
    end = &s[len - 1U];
    while ((end > s) && (isspace((unsigned char)*end) != 0)) {
        *end = '\0';
        end--;
    }
    return s;
}

/*
 * "key = value" の形の行か。
 * '=' があり、その前に空白でない文字が 1 つ以上あれば真。値は空でもよい。
 */
bool str_is_kv_line(const char *s)
{
    const char *eq;
    const char *p;

    if (s == NULL) {
        return false;
    }
    eq = strchr(s, '=');
    if (eq == NULL) {
        return false;
    }
    for (p = s; p < eq; p++) {
        if (isspace((unsigned char)*p) == 0) {
            return true;
        }
    }
    return false;
}

int str_casecmp(const char *a, const char *b)
{
    int ca;
    int cb;

    do {
        ca = tolower((unsigned char)*a);
        cb = tolower((unsigned char)*b);
        a++;
        b++;
    } while ((ca != 0) && (ca == cb));
    return ca - cb;
}

bool str_starts_with(const char *s, const char *prefix)
{
    if ((s == NULL) || (prefix == NULL)) {
        return false;
    }
    return strncmp(s, prefix, strlen(prefix)) == 0;
}

/*
 * dst に src を写す（必ず '\0' で終える）。
 * src の長さを返す。戻り値が dst_size 以上なら切り詰めた。
 */
size_t str_copy(char *dst, size_t dst_size, const char *src)
{
    size_t len;

    if ((dst == NULL) || (dst_size == 0U) || (src == NULL)) {
        return 0U;
    }
    len = strlen(src);
    if (len < dst_size) {
        (void)memcpy(dst, src, len + 1U);
    } else {
        (void)memcpy(dst, src, dst_size - 1U);
        dst[dst_size - 1U] = '\0';
    }
    return len;
}

/* 文字列の複製（ヒープ）。不要になったら free すること */
char *str_dup(const char *s)
{
    char  *p;
    size_t len;

    if (s == NULL) {
        return NULL;
    }
    len = strlen(s);
    p = (char *)malloc(len + 1U);
    if (p != NULL) {
        (void)memcpy(p, s, len + 1U);
    }
    return p;
}

/*
 * djb2（h = h * 33 + c）。設定のキーを比べる前の絞り込みに使う。
 * h の桁あふれ（2^32 での剰余）を前提にした計算。
 */
uint32_t str_hash(const char *s)
{
    uint32_t h = 5381U;

    while (*s != '\0') {
        h = (h << 5) + h + (uint8_t)*s;
        s++;
    }
    return h;
}

em4_err_t str_to_u32(const char *s, uint32_t *out)
{
    char         *end;
    unsigned long v;

    if ((s == NULL) || (out == NULL) || (*s == '\0') || (*s == '-')) {
        return EM4_ERR_PARAM;
    }
    errno = 0;
    v = strtoul(s, &end, 0);
    if ((errno != 0) || (*end != '\0') || (v > 0xFFFFFFFFUL)) {
        return EM4_ERR_FORMAT;
    }
    *out = (uint32_t)v;
    return EM4_OK;
}

em4_err_t str_to_i32(const char *s, int32_t *out)
{
    char *end;
    long  v;

    if ((s == NULL) || (out == NULL) || (*s == '\0')) {
        return EM4_ERR_PARAM;
    }
    errno = 0;
    v = strtol(s, &end, 10);
    if ((errno != 0) || (*end != '\0') || (v < (long)INT32_MIN) || (v > (long)INT32_MAX)) {
        return EM4_ERR_FORMAT;
    }
    *out = (int32_t)v;
    return EM4_OK;
}

em4_err_t str_to_bool(const char *s, bool *out)
{
    if ((s == NULL) || (out == NULL)) {
        return EM4_ERR_PARAM;
    }
    if ((str_casecmp(s, "1") == 0) || (str_casecmp(s, "on") == 0) ||
        (str_casecmp(s, "yes") == 0) || (str_casecmp(s, "true") == 0)) {
        *out = true;
    } else if ((str_casecmp(s, "0") == 0) || (str_casecmp(s, "off") == 0) ||
               (str_casecmp(s, "no") == 0) || (str_casecmp(s, "false") == 0)) {
        *out = false;
    } else {
        return EM4_ERR_FORMAT;
    }
    return EM4_OK;
}

static int hex_nibble(char c)
{
    if ((c >= '0') && (c <= '9')) {
        return c - '0';
    }
    if ((c >= 'a') && (c <= 'f')) {
        return (c - 'a') + 10;
    }
    if ((c >= 'A') && (c <= 'F')) {
        return (c - 'A') + 10;
    }
    return -1;
}

/* 16 進の文字列をバイトにする。変換したバイト数を返す（誤りは 0） */
size_t str_hex_decode(const char *hex, uint8_t *out, size_t max)
{
    size_t n = 0U;

    if ((hex == NULL) || (out == NULL)) {
        return 0U;
    }
    while ((hex[0] != '\0') && (hex[1] != '\0')) {
        int hi = hex_nibble(hex[0]);
        int lo = hex_nibble(hex[1]);
        if ((hi < 0) || (lo < 0) || (n >= max)) {
            return 0U;
        }
        out[n] = (uint8_t)((hi << 4) | lo);
        n++;
        hex += 2;
    }
    if (hex[0] != '\0') {
        return 0U;          /* 奇数の桁 */
    }
    return n;
}

size_t str_hex_encode(const uint8_t *in, size_t n, char *out, size_t out_size)
{
    static const char k_hex[] = "0123456789ABCDEF";
    size_t i;

    if ((in == NULL) || (out == NULL) || (out_size == 0U)) {
        return 0U;
    }
    for (i = 0U; (i < n) && (((2U * i) + 2U) < out_size); i++) {
        out[2U * i]        = k_hex[(in[i] >> 4) & 0x0FU];
        out[(2U * i) + 1U] = k_hex[in[i] & 0x0FU];
    }
    out[2U * i] = '\0';
    return i;
}

void str_upper(char *s)
{
    if (s == NULL) {
        return;
    }
    for (; *s != '\0'; s++) {
        *s = (char)toupper((unsigned char)*s);
    }
}
