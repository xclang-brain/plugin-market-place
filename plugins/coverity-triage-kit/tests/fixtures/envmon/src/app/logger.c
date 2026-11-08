/**
 * @file    logger.c
 * @brief   ログ（RAM のリングバッファと、ファイルへの書き出し）
 *
 * - すべての行を RAM のリングバッファ（LOG_RING_SIZE バイト）に入れる。古いものから消える。
 *   保守コマンド LOG DUMP で後ろの方を見られる。
 * - ファイル <dir>/em4.log にも書く。LOG_ROTATE_BYTES を超えたら名前を変えて新しく作る。
 *   em4.log → em4.log.1 → em4.log.2 → em4.log.3（LOG_KEEP_FILES 個まで残す）
 * - 実機では <dir> は SD カードのフォルダ。
 */
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

#include "app.h"
#include "hal.h"
#include "util.h"

typedef struct {
    log_level_t level;
    ringbuf_t   ring;
    uint8_t     ring_mem[LOG_RING_SIZE];
    FILE       *fp;
    char       *dir;
    char        path[LOG_PATH_MAX];
    uint32_t    file_bytes;
    uint32_t    n_error;
    uint32_t    n_warn_or_worse;
    uint32_t    n_other;
    uint32_t    n_rotate;
    bool        init;
    bool        busy;           /* 書いている途中で呼ばれたら捨てる */
} logger_t;

static logger_t s_log;

static const char *const k_level_name[LOG_LEVEL_COUNT] = {
    "ERROR", "WARN", "INFO", "DEBUG", "TRACE"
};

/* ===================================================================== */
/* 内部                                                                  */
/* ===================================================================== */
static void log_make_path(char *buf, size_t size, uint32_t idx)
{
    const char *dir = (s_log.dir != NULL) ? s_log.dir : ".";

    if (idx == 0U) {
        (void)snprintf(buf, size, "%s/%s", dir, LOG_FILE_BASENAME);
    } else {
        (void)snprintf(buf, size, "%s/%s.%lu", dir, LOG_FILE_BASENAME, (unsigned long)idx);
    }
}

static em4_err_t log_open_current(void)
{
    long pos;

    log_make_path(s_log.path, sizeof(s_log.path), 0U);
    s_log.fp = fopen(s_log.path, "a");
    if (s_log.fp == NULL) {
        (void)fprintf(stderr, "[log] cannot open %s: %s\n", s_log.path, strerror(errno));
        return EM4_ERR_IO;
    }
    pos = ftell(s_log.fp);
    s_log.file_bytes = (pos > 0L) ? (uint32_t)pos : 0U;
    return EM4_OK;
}

/*
 * ファイルを切り替える。
 * em4.log.(N-1) を消し、古い順に番号を 1 つずつずらしてから、em4.log を新しく作る。
 */
static em4_err_t log_rotate(void)
{
    char     from[LOG_PATH_MAX];
    char     to[LOG_PATH_MAX];
    uint32_t i;
    uint32_t now = timer_now_ms();

    if (s_log.fp != NULL) {
        (void)fclose(s_log.fp);
        s_log.fp = NULL;
    }

    log_make_path(to, sizeof(to), LOG_KEEP_FILES - 1U);
    (void)remove(to);                       /* なければ失敗するが、それでよい */
    for (i = LOG_KEEP_FILES - 1U; i > 0U; i--) {
        log_make_path(from, sizeof(from), i - 1U);
        log_make_path(to, sizeof(to), i);
        (void)rename(from, to);
    }

    log_make_path(s_log.path, sizeof(s_log.path), 0U);
    s_log.fp = fopen(s_log.path, "w");
    (void)fprintf(s_log.fp, "# EM-4 log, fw %s, rotated at %lu ms\n", EM4_FW_VERSION_STR,
                  (unsigned long)now);
    s_log.file_bytes = 0U;
    s_log.n_rotate++;
    return EM4_OK;
}

/* 段階ごとの件数を数える（保守画面の表示用） */
static void log_count_level(log_level_t lv)
{
    switch (lv) {
    case LOG_ERROR:
        s_log.n_error++;
        /* ERROR は「警告以上」の件数にも入れる。そのため break を書かずに次へ進む */
    case LOG_WARN:
        s_log.n_warn_or_worse++;
        break;
    case LOG_INFO:
    case LOG_DEBUG:
    case LOG_TRACE:
        s_log.n_other++;
        break;
    default:
        break;
    }
}

static void log_emit(const char *line, size_t len)
{
    (void)rb_write(&s_log.ring, (const uint8_t *)line, len, true);

    if (s_log.fp != NULL) {
        if (fwrite(line, 1U, len, s_log.fp) == len) {
            s_log.file_bytes += (uint32_t)len;
        }
        if (s_log.file_bytes >= LOG_ROTATE_BYTES) {
            (void)log_rotate();
        }
    }
}

/* ===================================================================== */
/* 公開の関数                                                            */
/* ===================================================================== */
em4_err_t log_init(const char *dir, log_level_t level)
{
    if ((dir == NULL) || (strlen(dir) >= LOG_DIR_MAX) || (level >= LOG_LEVEL_COUNT)) {
        return EM4_ERR_PARAM;
    }
    if (s_log.init) {
        return EM4_ERR_STATE;
    }

    (void)memset(&s_log, 0, sizeof(s_log));
    (void)rb_init(&s_log.ring, s_log.ring_mem, sizeof(s_log.ring_mem));
    s_log.level = level;
    s_log.dir   = str_dup(dir);
    if (s_log.dir == NULL) {
        return EM4_ERR_NOMEM;
    }
    if ((mkdir(dir, 0755) != 0) && (errno != EEXIST)) {
        (void)fprintf(stderr, "[log] cannot create %s: %s\n", dir, strerror(errno));
    }
    s_log.init = true;

    /* ファイルを開けなくても RAM には残すので、初期化は成功にする */
    (void)log_open_current();
    return EM4_OK;
}

void log_shutdown(void)
{
    if (s_log.fp != NULL) {
        (void)fflush(s_log.fp);
        (void)fclose(s_log.fp);
        s_log.fp = NULL;
    }
    free(s_log.dir);
    s_log.dir  = NULL;
    s_log.init = false;
}

/* ログのフォルダを変える（保守コマンド LOG DIR）。今のファイルを閉じ、新しいフォルダで開き直す */
em4_err_t log_set_dir(const char *dir)
{
    char *old = s_log.dir;
    char *nd;

    if ((dir == NULL) || (strlen(dir) >= LOG_DIR_MAX)) {
        return EM4_ERR_PARAM;
    }
    if (!s_log.init) {
        return EM4_ERR_STATE;
    }
    if ((old != NULL) && (strcmp(old, dir) == 0)) {
        return EM4_OK;
    }

    nd = (char *)malloc(strlen(dir));
    if (nd == NULL) {
        return EM4_ERR_NOMEM;
    }
    (void)strcpy(nd, dir);
    if ((mkdir(nd, 0755) != 0) && (errno != EEXIST)) {
        free(nd);
        return EM4_ERR_IO;
    }
    s_log.dir = nd;
    free(old);

    if (s_log.fp != NULL) {
        (void)fclose(s_log.fp);
        s_log.fp = NULL;
    }
    (void)log_open_current();
    LOG_I("log", "log dir changed: %s -> %s", (old != NULL) ? old : "(none)", nd);
    return EM4_OK;
}

void log_set_level(log_level_t lv)
{
    if (lv < LOG_LEVEL_COUNT) {
        s_log.level = lv;
    }
}

log_level_t log_get_level(void)
{
    return s_log.level;
}

int log_level_from_str(const char *s)
{
    size_t i;

    if (s == NULL) {
        return -1;
    }
    for (i = 0U; i <= LOG_LEVEL_COUNT; i++) {
        if (str_casecmp(s, k_level_name[i]) == 0) {
            return (int)i;
        }
    }
    return -1;
}

const char *log_level_name(log_level_t lv)
{
    return (lv < LOG_LEVEL_COUNT) ? k_level_name[lv] : "?";
}

void log_printf(log_level_t lv, const char *tag, const char *fmt, ...)
{
    char    line[LOG_LINE_MAX];
    va_list ap;
    int     n;
    int     m;

    if (lv >= LOG_LEVEL_COUNT) {
        return;
    }
    log_count_level(lv);
    if (lv > s_log.level) {
        return;
    }
    if (!s_log.init) {
        /* 初期化の前は標準エラーにだけ出す */
        va_start(ap, fmt);
        (void)fprintf(stderr, "[%s] ", k_level_name[lv]);
        (void)vfprintf(stderr, fmt, ap);
        (void)fputc('\n', stderr);
        va_end(ap);
        return;
    }
    if (s_log.busy) {
        return;
    }
    s_log.busy = true;

    n = snprintf(line, sizeof(line), "%8lu %-5s %-5s ", (unsigned long)timer_now_ms(),
                 k_level_name[lv], (tag != NULL) ? tag : "-");
    if ((n < 0) || ((size_t)n >= sizeof(line))) {
        s_log.busy = false;
        return;
    }
    va_start(ap, fmt);
    m = vsnprintf(&line[n], sizeof(line) - (size_t)n, fmt, ap);
    va_end(ap);
    if (m < 0) {
        m = 0;
    }
    n += m;
    if ((size_t)n > (sizeof(line) - 2U)) {
        n = (int)(sizeof(line) - 2U);       /* 長すぎる行は切る */
    }
    line[n]      = '\n';
    line[n + 1]  = '\0';
    log_emit(line, (size_t)n + 1U);

    if (lv <= LOG_WARN) {
        (void)fputs(line, stderr);
    }
    s_log.busy = false;
}

void log_flush(void)
{
    if (s_log.fp != NULL) {
        if (fflush(s_log.fp) != 0) {
            (void)fprintf(stderr, "[log] flush failed: %s\n", strerror(errno));
        }
    }
}

size_t log_ring_copy_tail(char *dst, size_t max)
{
    if ((dst == NULL) || (max == 0U)) {
        return 0U;
    }
    return rb_peek_tail(&s_log.ring, (uint8_t *)dst, max);
}

void log_get_counts(uint32_t *n_error, uint32_t *n_warn_or_worse, uint32_t *n_other)
{
    if (n_error != NULL) {
        *n_error = s_log.n_error;
    }
    if (n_warn_or_worse != NULL) {
        *n_warn_or_worse = s_log.n_warn_or_worse;
    }
    if (n_other != NULL) {
        *n_other = s_log.n_other;
    }
}
