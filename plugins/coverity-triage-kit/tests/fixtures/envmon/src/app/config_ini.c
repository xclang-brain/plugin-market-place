/**
 * @file    config_ini.c
 * @brief   設定ファイル（ini）の読み込み
 *
 * 書式
 *   [section]
 *   key = value      ; 行の途中の注釈は書けない。行頭の ';' か '#' が注釈
 *
 * 書かれていない項目は既定値のまま。範囲の外の値は誤りにする（読み込みを失敗させ、
 * 前の設定を使い続ける）。
 *
 *   [device]   name, id
 *   [sampling] period_ms, uplink_ms, window
 *   [sensor]   sht.retries, sht.addr, sht.heater, bmp.osr_t, bmp.osr_p,
 *              temp.offset, hum.offset
 *   [log]      level, dir
 *   [uplink]   enabled, retries
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app.h"
#include "util.h"

typedef struct {
    char     name[INI_KEY_MAX];
    uint16_t first;     /* 最初の項目の番号 */
    uint16_t count;
} ini_section_t;

typedef struct {
    char     key[INI_KEY_MAX];
    char     val[INI_VAL_MAX];
    uint32_t hash;      /* key の str_hash */
    uint16_t line;
} ini_entry_t;

typedef struct {
    ini_section_t sec[INI_MAX_SECTIONS];
    uint16_t      nsec;
    ini_entry_t  *ent;
    uint16_t      nent;
    uint16_t      cap;
} ini_doc_t;

static const char *const TAG = "cfg";

static em4_config_t s_active;
static bool         s_active_valid;

/* ===================================================================== */
/* ini の読み込み                                                        */
/* ===================================================================== */
static em4_err_t ini_doc_alloc(ini_doc_t *doc)
{
    (void)memset(doc, 0, sizeof(*doc));
    /* 項目の表は大きい（約 6.5 KB）ので、読み込みの間だけヒープに置く */
    doc->ent = (ini_entry_t *)malloc(sizeof(ini_entry_t) * INI_MAX_ENTRIES);
    if (doc->ent == NULL) {
        return EM4_ERR_NOMEM;
    }
    doc->cap = (uint16_t)INI_MAX_ENTRIES;
    /* 最初のセクションより前の項目は、名前のないセクションに入れる */
    doc->sec[0].name[0] = '\0';
    doc->sec[0].first   = 0U;
    doc->sec[0].count   = 0U;
    doc->nsec = 1U;
    return EM4_OK;
}

static void ini_doc_free(ini_doc_t *doc)
{
    free(doc->ent);
    doc->ent  = NULL;
    doc->nent = 0U;
    doc->cap  = 0U;
}

static em4_err_t ini_parse(FILE *fp, ini_doc_t *doc, uint16_t *err_line)
{
    char     *line;
    em4_err_t rc = EM4_OK;
    uint16_t  lineno = 0U;

    line = (char *)malloc(INI_LINE_MAX);
    if (line == NULL) {
        return EM4_ERR_NOMEM;
    }

    while (fgets(line, (int)INI_LINE_MAX, fp) != NULL) {
        char        *p;
        char        *eq;
        char        *key;
        char        *val;
        ini_entry_t *e;

        lineno++;
        *err_line = lineno;
        if ((strchr(line, '\n') == NULL) && (feof(fp) == 0)) {
            /* 1 行が INI_LINE_MAX - 1 文字を超えた。続きを読むと別の行に見えてしまう */
            LOG_E(TAG, "line %u is too long", lineno);
            return EM4_ERR_RANGE;
        }
        p = str_trim(line);
        if ((*p == '\0') || (*p == ';') || (*p == '#')) {
            continue;
        }

        if (*p == '[') {
            char          *end = strchr(p, ']');
            ini_section_t *s;

            if (end == NULL) {
                rc = EM4_ERR_FORMAT;
                goto out;
            }
            *end = '\0';
            p = str_trim(p + 1);
            if ((strlen(p) >= INI_KEY_MAX) || (doc->nsec >= INI_MAX_SECTIONS)) {
                rc = EM4_ERR_FULL;
                goto out;
            }
            s = &doc->sec[doc->nsec];
            (void)str_copy(s->name, sizeof(s->name), p);
            s->first = doc->nent;
            s->count = 0U;
            doc->nsec++;
            continue;
        }

        if (!str_is_kv_line(p)) {
            rc = EM4_ERR_FORMAT;
            goto out;
        }
        eq  = strchr(p, '=');
        *eq = '\0';
        key = str_trim(p);
        val = str_trim(eq + 1);

        if (doc->nent >= doc->cap) {
            rc = EM4_ERR_FULL;
            goto out;
        }
        if ((strlen(key) >= INI_KEY_MAX) || (strlen(val) >= INI_VAL_MAX)) {
            rc = EM4_ERR_RANGE;
            goto out;
        }
        e = &doc->ent[doc->nent];
        (void)str_copy(e->key, sizeof(e->key), key);
        (void)str_copy(e->val, sizeof(e->val), val);
        e->hash = str_hash(e->key);
        e->line = lineno;
        doc->nent++;
        doc->sec[doc->nsec - 1U].count++;
    }
    *err_line = 0U;

out:
    free(line);
    return rc;
}

static const ini_section_t *ini_find_section(const ini_doc_t *doc, const char *name)
{
    uint16_t i;

    for (i = 0U; i < doc->nsec; i++) {
        if (str_casecmp(doc->sec[i].name, name) == 0) {
            return &doc->sec[i];
        }
    }
    return NULL;
}

static const char *ini_get(const ini_doc_t *doc, const char *section, const char *key)
{
    const ini_section_t *s = ini_find_section(doc, section);
    uint32_t             h = str_hash(key);
    uint16_t             i;

    if (s == NULL) {
        return NULL;
    }
    for (i = 0U; i < s->count; i++) {
        const ini_entry_t *e = &doc->ent[s->first + i];
        if ((e->hash == h) && (strcmp(e->key, key) == 0)) {
            return e->val;
        }
    }
    return NULL;
}

/* ===================================================================== */
/* 値の変換と確かめ                                                      */
/* ===================================================================== */
static em4_err_t get_u32_range(const ini_doc_t *doc, const char *sec, const char *key,
                               uint32_t lo, uint32_t hi, uint32_t *out)
{
    const char *v = ini_get(doc, sec, key);
    uint32_t    x;

    if (v == NULL) {
        return EM4_OK;      /* 書かれていなければ既定値のまま */
    }
    if (str_to_u32(v, &x) != EM4_OK) {
        LOG_E(TAG, "[%s] %s: not a number: %s", sec, key, v);
        return EM4_ERR_FORMAT;
    }
    if ((x < lo) || (x > hi)) {
        LOG_E(TAG, "[%s] %s: %lu is out of range %lu..%lu", sec, key, (unsigned long)x,
              (unsigned long)lo, (unsigned long)hi);
        return EM4_ERR_RANGE;
    }
    *out = x;
    return EM4_OK;
}

static em4_err_t get_bool(const ini_doc_t *doc, const char *sec, const char *key, bool *out)
{
    const char *v = ini_get(doc, sec, key);

    if (v == NULL) {
        return EM4_OK;
    }
    return str_to_bool(v, out);
}

/* BMP280 のオーバーサンプリング（1, 2, 4, 8, 16、0 は測らない） */
static bool osr_from_int(uint32_t v, bmp_osr_t *out)
{
    switch (v) {
    case 0U:  *out = BMP_OSR_SKIP; break;
    case 1U:  *out = BMP_OSR_1;    break;
    case 2U:  *out = BMP_OSR_2;    break;
    case 4U:  *out = BMP_OSR_4;    break;
    case 8U:  *out = BMP_OSR_8;    break;
    case 16U: *out = BMP_OSR_16;   break;
    default:
        return false;
    }
    return true;
}

static em4_err_t apply_sensor_entry(const ini_entry_t *e, em4_config_t *cfg)
{
    uint32_t u;
    int32_t  s;
    bool     b;

    if (strcmp(e->key, "sht.retries") == 0) {
        if ((str_to_u32(e->val, &u) != EM4_OK) || (u > SHT_RETRIES_LIMIT)) {
            return EM4_ERR_RANGE;
        }
        cfg->sht_retries = (uint8_t)u;
    } else if (strcmp(e->key, "sht.addr") == 0) {
        if ((str_to_u32(e->val, &u) != EM4_OK) || ((u != 0x44U) && (u != 0x45U))) {
            return EM4_ERR_RANGE;
        }
        cfg->sht_addr = (uint8_t)u;
    } else if (strcmp(e->key, "sht.heater") == 0) {
        if (str_to_bool(e->val, &b) != EM4_OK) {
            return EM4_ERR_FORMAT;
        }
        cfg->sht_heater = b;
    } else if ((strcmp(e->key, "bmp.osr_t") == 0) || (strcmp(e->key, "bmp.osr_p") == 0)) {
        bmp_osr_t osr;
        if ((str_to_u32(e->val, &u) != EM4_OK) || !osr_from_int(u, &osr)) {
            return EM4_ERR_RANGE;
        }
        if (e->key[8] == 't') {
            cfg->bmp_osr_temp = osr;
        } else {
            cfg->bmp_osr_press = osr;
        }
    } else if (strcmp(e->key, "temp.offset") == 0) {
        if ((str_to_i32(e->val, &s) != EM4_OK) || (s < -1000) || (s > 1000)) {
            return EM4_ERR_RANGE;
        }
        cfg->temp_offset_c100 = s;
    } else if (strcmp(e->key, "hum.offset") == 0) {
        if ((str_to_i32(e->val, &s) != EM4_OK) || (s < -1000) || (s > 1000)) {
            return EM4_ERR_RANGE;
        }
        cfg->hum_offset_c100 = s;
    } else {
        LOG_W(TAG, "[sensor] unknown key '%s' (line %u) ignored", e->key, e->line);
    }
    return EM4_OK;
}

static em4_err_t config_apply(const ini_doc_t *doc, em4_config_t *cfg)
{
    const ini_section_t *sec;
    const char          *v;
    uint32_t             u;
    uint16_t             i;
    em4_err_t            rc;

    /* [device] */
    v = ini_get(doc, "device", "name");
    if (v != NULL) {
        strcpy(cfg->device_name, v);
    }
    u = cfg->device_id;
    rc = get_u32_range(doc, "device", "id", 1U, 0xFFFFU, &u);
    if (rc != EM4_OK) {
        return rc;
    }
    cfg->device_id = (uint16_t)u;

    /* [sampling] */
    rc = get_u32_range(doc, "sampling", "period_ms", 100U, 60000U, &cfg->sample_period_ms);
    if (rc != EM4_OK) {
        return rc;
    }
    rc = get_u32_range(doc, "sampling", "uplink_ms", 1000U, 600000U, &cfg->uplink_period_ms);
    if (rc != EM4_OK) {
        return rc;
    }
    rc = get_u32_range(doc, "sampling", "window", 1U, 600U, &cfg->telemetry_window);
    if (rc != EM4_OK) {
        return rc;
    }

    /* [sensor] はキーが多いので、セクションの中を順に見る */
    sec = ini_find_section(doc, "sensor");
    for (i = 0U; i < sec->count; i++) {
        const ini_entry_t *e = &doc->ent[sec->first + i];
        rc = apply_sensor_entry(e, cfg);
        if (rc != EM4_OK) {
            LOG_E(TAG, "[sensor] %s = %s: invalid (line %u)", e->key, e->val, e->line);
            return rc;
        }
    }

    /* [log] */
    v = ini_get(doc, "log", "level");
    if (v != NULL) {
        int lv = log_level_from_str(v);
        if (lv < 0) {
            LOG_E(TAG, "[log] level: unknown level '%s'", v);
            return EM4_ERR_FORMAT;
        }
        cfg->log_level = (log_level_t)lv;
    }
    v = ini_get(doc, "log", "dir");
    if (v != NULL) {
        if (str_copy(cfg->log_dir, sizeof(cfg->log_dir), v) >= sizeof(cfg->log_dir)) {
            LOG_E(TAG, "[log] dir: too long");
            return EM4_ERR_RANGE;
        }
    }

    /* [uplink] */
    rc = get_bool(doc, "uplink", "enabled", &cfg->uplink_enabled);
    if (rc != EM4_OK) {
        return rc;
    }
    u = cfg->uplink_retries;
    rc = get_u32_range(doc, "uplink", "retries", 0U, 10U, &u);
    if (rc != EM4_OK) {
        return rc;
    }
    cfg->uplink_retries = (uint8_t)u;
    return EM4_OK;
}

/* ===================================================================== */
/* 公開の関数                                                            */
/* ===================================================================== */
void config_set_defaults(em4_config_t *cfg)
{
    (void)memset(cfg, 0, sizeof(*cfg));
    (void)str_copy(cfg->device_name, sizeof(cfg->device_name), "EM4");
    cfg->device_id        = 1U;
    cfg->sample_period_ms = 1000U;
    cfg->uplink_period_ms = 5000U;
    cfg->telemetry_window = TELEM_WINDOW_DEFAULT;
    cfg->sht_bus          = SHT_I2C_BUS;
    cfg->sht_addr         = SHT_I2C_ADDR;
    cfg->sht_retries      = SHT_RETRIES_DEFAULT;
    cfg->sht_heater       = false;
    cfg->bmp_osr_temp     = BMP_OSR_2;
    cfg->bmp_osr_press    = BMP_OSR_16;
    cfg->log_level        = LOG_INFO;
    (void)str_copy(cfg->log_dir, sizeof(cfg->log_dir), "log");
    cfg->uplink_enabled   = true;
    cfg->uplink_retries   = 3U;
}

em4_err_t config_load(const char *path, em4_config_t *cfg)
{
    FILE     *fp;
    ini_doc_t doc;
    uint16_t  err_line = 0U;
    em4_err_t rc;

    if ((path == NULL) || (cfg == NULL)) {
        return EM4_ERR_PARAM;
    }
    fp = fopen(path, "r");
    if (fp == NULL) {
        LOG_W(TAG, "cannot open %s", path);
        return EM4_ERR_IO;
    }

    rc = ini_doc_alloc(&doc);
    if (rc != EM4_OK) {
        goto out_close;
    }
    rc = ini_parse(fp, &doc, &err_line);
    if (rc != EM4_OK) {
        LOG_E(TAG, "%s:%u: parse error (%s)", path, err_line, em4_strerror(rc));
        goto out_free;
    }
    config_set_defaults(cfg);
    rc = config_apply(&doc, cfg);

out_free:
    ini_doc_free(&doc);
out_close:
    (void)fclose(fp);
    return rc;
}

em4_err_t config_init(const char *path)
{
    em4_config_t tmp;
    em4_err_t    rc = EM4_OK;
    uint32_t     rev = s_active.revision;

    if (!s_active_valid) {
        config_set_defaults(&s_active);
        s_active_valid = true;
    }
    if (path != NULL) {
        rc = config_load(path, &tmp);
        if (rc == EM4_OK) {
            s_active = tmp;
            LOG_I(TAG, "loaded %s", path);
        } else {
            LOG_W(TAG, "using previous/default settings (%s)", em4_strerror(rc));
        }
    }
    s_active.revision = rev + 1U;
    return rc;
}

const em4_config_t *config_get(void)
{
    if (!s_active_valid) {
        config_set_defaults(&s_active);
        s_active_valid = true;
    }
    return &s_active;
}

size_t config_dump(char *buf, size_t max)
{
    const em4_config_t *c = config_get();
    int                 n;

    if ((buf == NULL) || (max == 0U)) {
        return 0U;
    }
    n = snprintf(buf, max,
                 "device.name=%s\ndevice.id=%u\nsampling.period_ms=%lu\nsampling.uplink_ms=%lu\n"
                 "sampling.window=%lu\nsht.retries=%u\nsht.addr=0x%02X\nbmp.osr=%d/%d\n"
                 "log.level=%s\nlog.dir=%s\nuplink.enabled=%d\nrevision=%lu\n",
                 c->device_name, c->device_id, (unsigned long)c->sample_period_ms,
                 (unsigned long)c->uplink_period_ms, (unsigned long)c->telemetry_window,
                 c->sht_retries, c->sht_addr, (int)c->bmp_osr_temp, (int)c->bmp_osr_press,
                 log_level_name(c->log_level), c->log_dir, c->uplink_enabled ? 1 : 0,
                 (unsigned long)c->revision);
    if (n < 0) {
        buf[0] = '\0';
        return 0U;
    }
    return ((size_t)n < max) ? (size_t)n : (max - 1U);
}
