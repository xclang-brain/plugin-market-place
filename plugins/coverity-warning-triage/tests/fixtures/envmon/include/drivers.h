/**
 * @file    drivers.h
 * @brief   周辺装置のドライバー（温湿度センサー、気圧センサー、フラッシュ）
 */
#ifndef EM4_DRIVERS_H
#define EM4_DRIVERS_H

#include "em4_types.h"
#include "em4_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ===================================================================== */
/* 温湿度センサー（Sensirion SHT3x 互換、I2C）                           */
/* ===================================================================== */
typedef struct {
    int32_t  temp_c100;     /* 0.01 °C 単位 */
    uint32_t rh_c100;       /* 0.01 %RH 単位 */
    uint32_t ts_ms;
    bool     valid;
} sht_sample_t;

typedef struct {
    uint32_t n_ok;
    uint32_t n_crc_err;
    uint32_t n_bus_err;
    uint32_t err_pct;       /* 直近の失敗の割合（%） */
} sht_stats_t;

typedef struct {
    uint8_t  bus;
    uint8_t  addr;
    uint8_t  retries;       /* 失敗したときにやり直す回数（設定 sht.retries） */
    bool     heater_on;
    /* 移動平均 */
    int32_t  hist_t[SHT_AVG_DEPTH];
    uint32_t hist_h[SHT_AVG_DEPTH];
    uint8_t  hist_n;
    uint8_t  hist_pos;
    /* 統計 */
    uint32_t n_ok;
    uint32_t n_crc_err;
    uint32_t n_bus_err;
    bool     ready;
} sht_dev_t;

em4_err_t sht_init(sht_dev_t *dev, uint8_t bus, uint8_t addr, uint8_t retries);
em4_err_t sht_read(sht_dev_t *dev, sht_sample_t *out);
em4_err_t sht_filter_get(const sht_dev_t *dev, sht_sample_t *out);
em4_err_t sht_heater(sht_dev_t *dev, bool on);
em4_err_t sht_read_status(sht_dev_t *dev, uint16_t *status);
em4_err_t sht_soft_reset(sht_dev_t *dev);
void      sht_get_stats(const sht_dev_t *dev, sht_stats_t *st);

/* ===================================================================== */
/* 気圧センサー（Bosch BMP280 互換、SPI）                                */
/* ===================================================================== */
typedef enum {
    BMP_OSR_SKIP = 0,
    BMP_OSR_1,
    BMP_OSR_2,
    BMP_OSR_4,
    BMP_OSR_8,
    BMP_OSR_16
} bmp_osr_t;

typedef struct {
    uint16_t dig_T1;
    int16_t  dig_T2;
    int16_t  dig_T3;
    uint16_t dig_P1;
    int16_t  dig_P2;
    int16_t  dig_P3;
    int16_t  dig_P4;
    int16_t  dig_P5;
    int16_t  dig_P6;
    int16_t  dig_P7;
    int16_t  dig_P8;
    int16_t  dig_P9;
} bmp_calib_t;

typedef struct {
    uint8_t     bus;
    uint8_t     cs;
    bmp_osr_t   osr_t;
    bmp_osr_t   osr_p;
    bmp_calib_t cal;
    int32_t     t_fine;
    uint32_t    press_hist[BMP_AVG_DEPTH];
    uint8_t     hist_n;
    uint8_t     hist_pos;
    uint32_t    n_err;
    bool        ready;
} bmp_dev_t;

em4_err_t bmp_init(bmp_dev_t *dev, uint8_t bus, uint8_t cs, bmp_osr_t osr_t, bmp_osr_t osr_p);
em4_err_t bmp_read(bmp_dev_t *dev, int32_t *temp_c100, uint32_t *press_pa);
em4_err_t bmp_get_average(const bmp_dev_t *dev, uint32_t *press_pa);
em4_err_t bmp_sleep(bmp_dev_t *dev);

/* ===================================================================== */
/* フラッシュ（ホストではイメージファイルで模擬する NOR フラッシュ）      */
/* ===================================================================== */
#define FLASH_MAGIC         (0x454D3446UL)   /* "EM4F" */
#define FLASH_LAYOUT_VER    (2U)

typedef struct {
    uint32_t magic;
    uint16_t layout_ver;
    uint16_t sector_size;
    uint16_t sector_count;
    uint16_t reserved;
    uint32_t boot_count;
    uint16_t hdr_crc;       /* magic から boot_count までの CRC16 */
    uint16_t pad;
} flash_image_hdr_t;

typedef struct {
    const void *data;
    size_t      len;
} flash_chunk_t;

/* 区画の先頭の番地 */
#define FLASH_ADDR_CAL      (1U * FLASH_SECTOR_SIZE)   /* 校正値 */
#define FLASH_ADDR_BOOT     (2U * FLASH_SECTOR_SIZE)   /* 起動の記録 */

em4_err_t flash_store_mount(const char *image_path);
void      flash_store_unmount(void);
bool      flash_store_mounted(void);
em4_err_t flash_store_erase_sector(uint32_t sector);
em4_err_t flash_store_program(uint32_t addr, const void *data, size_t len);
em4_err_t flash_store_read(uint32_t addr, void *data, size_t len);
em4_err_t flash_store_write_chunks(uint32_t addr, const flash_chunk_t *chunks, size_t n);
em4_err_t flash_store_load(const char *path, flash_image_hdr_t *hdr);
uint32_t  flash_store_boot_count(void);

/* 記録（キーと値）。後から書いたものが有効になる */
em4_err_t flash_rec_append(uint16_t key, const void *data, uint16_t len);
em4_err_t flash_rec_find(uint16_t key, void *data, uint16_t max, uint16_t *out_len);
uint32_t  flash_rec_free_bytes(void);

#ifdef __cplusplus
}
#endif

#endif /* EM4_DRIVERS_H */
