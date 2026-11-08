/**
 * @file    flash_store.c
 * @brief   フラッシュ（ホストではイメージファイルで模擬する NOR フラッシュ）
 *
 * NOR フラッシュと同じ制約を守る。
 *   - 消去はセクター単位（FLASH_SECTOR_SIZE）。消去すると 0xFF になる。
 *   - 書き込みはビットを 1 → 0 にしかできない（new = old & data）。
 *
 * 区画（セクター番号）
 *   0      : イメージの見出し
 *   1      : 校正値
 *   2      : 起動の記録
 *   3..11  : 記録（キーと値、追記型）
 *   12..15 : 予備
 */
#include <stdio.h>
#include <string.h>

#include "drivers.h"
#include "proto.h"
#include "app.h"

typedef struct {
    const char *name;
    uint32_t    first_sector;
    uint32_t    sector_count;
} flash_part_t;

static const flash_part_t k_parts[] = {
    { "hdr",  0U, 1U },
    { "cal",  1U, 1U },
    { "boot", 2U, 1U },
    { "rec",  3U, 9U },
    { "spare", 12U, 4U },
};

/* 記録の見出し（8 バイト）。state は消去後の 0xFF から順にビットを落とす */
#define REC_STATE_ERASED    (0xFFU)
#define REC_STATE_WRITING   (0xFEU)
#define REC_STATE_VALID     (0xFCU)
#define REC_STATE_DELETED   (0x00U)
#define REC_HDR_SIZE        (8U)
#define REC_KEY_ERASED      (0xFFFFU)
#define REC_DATA_MAX        (256U)

typedef struct {
    uint16_t key;
    uint16_t len;
    uint16_t crc;
    uint8_t  state;
    uint8_t  pad;
} rec_hdr_t;

static FILE    *s_fp;
static uint8_t  s_sector_buf[FLASH_SECTOR_SIZE];
static uint32_t s_rec_base;
static uint32_t s_rec_end;
static uint32_t s_rec_wp;           /* 次に記録を書く番地 */
static uint32_t s_boot_count;

static const char *const TAG = "flash";

static const flash_part_t *flash_find_partition(const char *name)
{
    size_t i;

    for (i = 0U; i < EM4_ARRAY_SIZE(k_parts); i++) {
        if (strcmp(k_parts[i].name, name) == 0) {
            return &k_parts[i];
        }
    }
    return NULL;
}

static em4_err_t flash_raw_read(uint32_t addr, void *data, size_t len)
{
    if (fseek(s_fp, (long)addr, SEEK_SET) != 0) {
        return EM4_ERR_IO;
    }
    if (fread(data, 1U, len, s_fp) != len) {
        return EM4_ERR_IO;
    }
    return EM4_OK;
}

static em4_err_t flash_raw_write(uint32_t addr, const void *data, size_t len)
{
    if (fseek(s_fp, (long)addr, SEEK_SET) != 0) {
        return EM4_ERR_IO;
    }
    if (fwrite(data, 1U, len, s_fp) != len) {
        return EM4_ERR_IO;
    }
    if (fflush(s_fp) != 0) {
        return EM4_ERR_IO;
    }
    return EM4_OK;
}

static uint16_t flash_hdr_crc(const flash_image_hdr_t *h)
{
    return crc16_ccitt((const uint8_t *)h, offsetof(flash_image_hdr_t, hdr_crc));
}

/* 新しいイメージを作る（全体を 0xFF にして見出しを書く） */
static em4_err_t flash_format(void)
{
    flash_image_hdr_t hdr;
    uint32_t          s;
    em4_err_t         rc;

    (void)memset(s_sector_buf, 0xFF, sizeof(s_sector_buf));
    for (s = 0U; s < FLASH_SECTOR_COUNT; s++) {
        rc = flash_raw_write(s * FLASH_SECTOR_SIZE, s_sector_buf, sizeof(s_sector_buf));
        if (rc != EM4_OK) {
            return rc;
        }
    }
    (void)memset(&hdr, 0, sizeof(hdr));
    hdr.magic        = FLASH_MAGIC;
    hdr.layout_ver   = FLASH_LAYOUT_VER;
    hdr.sector_size  = (uint16_t)FLASH_SECTOR_SIZE;
    hdr.sector_count = (uint16_t)FLASH_SECTOR_COUNT;
    hdr.boot_count   = 0U;
    hdr.hdr_crc      = flash_hdr_crc(&hdr);
    return flash_raw_write(0U, &hdr, sizeof(hdr));
}

static em4_err_t flash_scan_records(void)
{
    rec_hdr_t rh;
    uint32_t  addr = s_rec_base;
    em4_err_t rc;

    while ((addr + REC_HDR_SIZE) <= s_rec_end) {
        rc = flash_raw_read(addr, &rh, sizeof(rh));
        if (rc != EM4_OK) {
            return rc;
        }
        if ((rh.state == REC_STATE_ERASED) && (rh.key == REC_KEY_ERASED)) {
            break;
        }
        if (rh.len > REC_DATA_MAX) {
            LOG_W(TAG, "broken record at 0x%05lX, stop scanning", (unsigned long)addr);
            break;
        }
        addr += REC_HDR_SIZE + EM4_ALIGN4((uint32_t)rh.len);
    }
    s_rec_wp = addr;
    return EM4_OK;
}

em4_err_t flash_store_mount(const char *image_path)
{
    flash_image_hdr_t   hdr;
    const flash_part_t *part;
    em4_err_t           rc;

    if (image_path == NULL) {
        return EM4_ERR_PARAM;
    }
    if (s_fp != NULL) {
        return EM4_ERR_STATE;
    }

    s_fp = fopen(image_path, "r+b");
    if (s_fp == NULL) {
        /* まだない。新しく作る */
        s_fp = fopen(image_path, "w+b");
        if (s_fp == NULL) {
            LOG_E(TAG, "cannot create %s", image_path);
            return EM4_ERR_IO;
        }
        rc = flash_format();
        if (rc != EM4_OK) {
            goto fail;
        }
        LOG_I(TAG, "formatted new image %s", image_path);
    }

    rc = flash_raw_read(0U, &hdr, sizeof(hdr));
    if (rc != EM4_OK) {
        goto fail;
    }
    if ((hdr.magic != FLASH_MAGIC) || (hdr.hdr_crc != flash_hdr_crc(&hdr))) {
        LOG_W(TAG, "bad header, reformatting");
        rc = flash_format();
        if (rc != EM4_OK) {
            goto fail;
        }
        rc = flash_raw_read(0U, &hdr, sizeof(hdr));
        if (rc != EM4_OK) {
            goto fail;
        }
    }

    part = flash_find_partition("rec");
    if (part == NULL) {
        /* 区画の表はビルド時に決まる。見つからないのは作りの誤りなので止める */
        fatal_error(FAULT_FLASH_LAYOUT, 0U);
    }
    s_rec_base = part->first_sector * FLASH_SECTOR_SIZE;
    s_rec_end  = (part->first_sector + part->sector_count) * FLASH_SECTOR_SIZE;

    rc = flash_scan_records();
    if (rc != EM4_OK) {
        goto fail;
    }

    /* 起動の回数を数える（見出しを書き直すため、見出しのセクターを消去する） */
    s_boot_count = hdr.boot_count + 1U;
    hdr.boot_count = s_boot_count;
    hdr.hdr_crc    = flash_hdr_crc(&hdr);
    rc = flash_store_erase_sector(0U);
    if (rc == EM4_OK) {
        rc = flash_raw_write(0U, &hdr, sizeof(hdr));
    }
    if (rc != EM4_OK) {
        goto fail;
    }

    LOG_I(TAG, "mounted %s (boot #%lu, rec wp=0x%05lX)", image_path,
          (unsigned long)s_boot_count, (unsigned long)s_rec_wp);
    return EM4_OK;

fail:
    (void)fclose(s_fp);
    s_fp = NULL;
    return rc;
}

void flash_store_unmount(void)
{
    if (s_fp != NULL) {
        (void)fclose(s_fp);
        s_fp = NULL;
    }
}

bool flash_store_mounted(void)
{
    return s_fp != NULL;
}

uint32_t flash_store_boot_count(void)
{
    return s_boot_count;
}

em4_err_t flash_store_erase_sector(uint32_t sector)
{
    if (s_fp == NULL) {
        return EM4_ERR_STATE;
    }
    if (sector >= FLASH_SECTOR_COUNT) {
        return EM4_ERR_PARAM;
    }
    (void)memset(s_sector_buf, 0xFF, sizeof(s_sector_buf));
    return flash_raw_write(sector * FLASH_SECTOR_SIZE, s_sector_buf, sizeof(s_sector_buf));
}

/* NOR の書き込み：ビットは 1 → 0 にしかならない */
em4_err_t flash_store_program(uint32_t addr, const void *data, size_t len)
{
    const uint8_t *src = (const uint8_t *)data;
    uint8_t        old[64];
    size_t         done = 0U;
    em4_err_t      rc;

    if (s_fp == NULL) {
        return EM4_ERR_STATE;
    }
    if ((data == NULL) || (len == 0U) || ((addr + len) > FLASH_TOTAL_SIZE)) {
        return EM4_ERR_PARAM;
    }

    while (done < len) {
        size_t n = EM4_MIN(len - done, sizeof(old));
        size_t i;

        rc = flash_raw_read(addr + (uint32_t)done, old, n);
        if (rc != EM4_OK) {
            return rc;
        }
        for (i = 0U; i < n; i++) {
            if ((uint8_t)(old[i] & src[done + i]) != src[done + i]) {
                /* 消去していないところに 1 を書こうとした */
                return EM4_ERR_STATE;
            }
            old[i] &= src[done + i];
        }
        rc = flash_raw_write(addr + (uint32_t)done, old, n);
        if (rc != EM4_OK) {
            return rc;
        }
        done += n;
    }
    return EM4_OK;
}

em4_err_t flash_store_read(uint32_t addr, void *data, size_t len)
{
    if (s_fp == NULL) {
        return EM4_ERR_STATE;
    }
    if ((data == NULL) || ((addr + len) > FLASH_TOTAL_SIZE)) {
        return EM4_ERR_PARAM;
    }
    return flash_raw_read(addr, data, len);
}

/*
 * いくつかの断片を、addr から続けて書く。
 * 書く先は呼び出し元が消去しておくこと。
 */
em4_err_t flash_store_write_chunks(uint32_t addr, const flash_chunk_t *chunks, size_t n)
{
    em4_err_t rc;
    size_t    i;
    uint32_t  pos = addr;

    if ((s_fp == NULL) || (chunks == NULL)) {
        return EM4_ERR_STATE;
    }
    for (i = 0U; i < n; i++) {
        rc = flash_store_program(pos, chunks[i].data, chunks[i].len);
        if (rc != EM4_OK) {
            break;
        }
        pos += (uint32_t)chunks[i].len;
    }
    return rc;
}

/*
 * 別のイメージファイルの見出しだけを読む（保守コマンド FLASH INFO と、
 * イメージを入れ替える前の確認に使う）。マウント中のイメージには触らない。
 */
em4_err_t flash_store_load(const char *path, flash_image_hdr_t *hdr)
{
    FILE             *fp;
    flash_image_hdr_t tmp;

    if ((path == NULL) || (hdr == NULL)) {
        return EM4_ERR_PARAM;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        return EM4_ERR_IO;
    }
    if (fread(&tmp, sizeof(tmp), 1U, fp) != 1U) {
        (void)fclose(fp);
        return EM4_ERR_IO;
    }
    if (tmp.magic != FLASH_MAGIC) {
        return EM4_ERR_FORMAT;
    }
    if (tmp.hdr_crc != flash_hdr_crc(&tmp)) {
        (void)fclose(fp);
        return EM4_ERR_CRC;
    }
    (void)fclose(fp);

    *hdr = tmp;
    return EM4_OK;
}

/* ===================================================================== */
/* 記録（キーと値）                                                      */
/* ===================================================================== */
em4_err_t flash_rec_append(uint16_t key, const void *data, uint16_t len)
{
    rec_hdr_t rh;
    uint8_t   state;
    uint32_t  need;
    em4_err_t rc;

    if (s_fp == NULL) {
        return EM4_ERR_STATE;
    }
    if ((data == NULL) || (len == 0U) || (len > REC_DATA_MAX) || (key == REC_KEY_ERASED)) {
        return EM4_ERR_PARAM;
    }
    need = REC_HDR_SIZE + EM4_ALIGN4((uint32_t)len);
    if ((s_rec_wp + need) > s_rec_end) {
        return EM4_ERR_FULL;
    }

    rh.key   = key;
    rh.len   = len;
    rh.crc   = crc16_ccitt((const uint8_t *)data, len);
    rh.state = REC_STATE_WRITING;
    rh.pad   = 0xFFU;

    /* 見出し（WRITING）→ データ → state を VALID の順に書く。途中で電源が落ちても壊れない */
    rc = flash_store_program(s_rec_wp, &rh, sizeof(rh));
    if (rc != EM4_OK) {
        return rc;
    }
    rc = flash_store_program(s_rec_wp + REC_HDR_SIZE, data, len);
    if (rc != EM4_OK) {
        return rc;
    }
    state = REC_STATE_VALID;
    rc = flash_store_program(s_rec_wp + (uint32_t)offsetof(rec_hdr_t, state), &state, 1U);
    if (rc != EM4_OK) {
        return rc;
    }
    s_rec_wp += need;
    return EM4_OK;
}

em4_err_t flash_rec_find(uint16_t key, void *data, uint16_t max, uint16_t *out_len)
{
    rec_hdr_t rh;
    uint32_t  addr;
    uint32_t  found = 0U;
    uint16_t  found_len = 0U;
    em4_err_t rc;

    if (s_fp == NULL) {
        return EM4_ERR_STATE;
    }
    if ((data == NULL) || (out_len == NULL)) {
        return EM4_ERR_PARAM;
    }

    /* 後から書いたものが有効。最後まで見る */
    for (addr = s_rec_base; addr < s_rec_wp; addr += REC_HDR_SIZE + EM4_ALIGN4((uint32_t)rh.len)) {
        rc = flash_raw_read(addr, &rh, sizeof(rh));
        if (rc != EM4_OK) {
            return rc;
        }
        if ((rh.key == key) && (rh.state == REC_STATE_VALID)) {
            found     = addr;
            found_len = rh.len;
        }
    }
    if (found == 0U) {
        return EM4_ERR_NOTFOUND;
    }
    if (found_len > max) {
        return EM4_ERR_RANGE;
    }
    rc = flash_raw_read(found + REC_HDR_SIZE, data, found_len);
    if (rc != EM4_OK) {
        return rc;
    }
    if (crc16_ccitt((const uint8_t *)data, found_len) != rh.crc) {
        /* 最後に読んだ見出しではなく、見つけた記録の CRC と比べる */
        rc = flash_raw_read(found, &rh, sizeof(rh));
        if ((rc != EM4_OK) || (crc16_ccitt((const uint8_t *)data, found_len) != rh.crc)) {
            return EM4_ERR_CRC;
        }
    }
    *out_len = found_len;
    return EM4_OK;
}

uint32_t flash_rec_free_bytes(void)
{
    return (s_fp == NULL) ? 0U : (s_rec_end - s_rec_wp);
}
