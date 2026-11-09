/**
 * @file    bmp280.c
 * @brief   気圧センサー（Bosch BMP280 互換、SPI 4 線式）のドライバー
 *
 * 補正の計算はデータシート（BST-BMP280-DS001）3.11.3 の整数版をそのまま使う。
 */
#include <string.h>

#include "drivers.h"
#include "hal.h"
#include "app.h"

#define BMP_REG_CALIB       (0x88U)
#define BMP_REG_ID          (0xD0U)
#define BMP_REG_RESET       (0xE0U)
#define BMP_REG_STATUS      (0xF3U)
#define BMP_REG_CTRL_MEAS   (0xF4U)
#define BMP_REG_CONFIG      (0xF5U)
#define BMP_REG_DATA        (0xF7U)

#define BMP_CHIP_ID         (0x58U)
#define BMP_RESET_CMD       (0xB6U)
#define BMP_MODE_NORMAL     (0x03U)
#define BMP_STANDBY_125MS   (0x02U)
#define BMP_FILTER_X4       (0x02U)
#define BMP_STATUS_MEASURING (0x08U)

#define BMP_SPI_READ        (0x80U)
#define BMP_SPI_WRITE_MASK  (0x7FU)
#define BMP_XFER_MAX        (32U)

static const char *const TAG = "bmp";

static em4_err_t bmp_read_regs(bmp_dev_t *dev, uint8_t reg, uint8_t *buf, size_t len)
{
    uint8_t   tx[BMP_XFER_MAX + 1U];
    uint8_t   rx[BMP_XFER_MAX + 1U];
    em4_err_t rc;

    if (len > BMP_XFER_MAX) {
        return EM4_ERR_PARAM;
    }
    (void)memset(tx, 0, sizeof(tx));
    tx[0] = (uint8_t)(reg | BMP_SPI_READ);
    rc = spi_transfer(dev->bus, dev->cs, tx, rx, len + 1U);
    if (rc != EM4_OK) {
        dev->n_err++;
        return rc;
    }
    (void)memcpy(buf, &rx[1], len);
    return EM4_OK;
}

static em4_err_t bmp_write_reg(bmp_dev_t *dev, uint8_t reg, uint8_t val)
{
    uint8_t   tx[2];
    uint8_t   rx[2];
    em4_err_t rc;

    tx[0] = (uint8_t)(reg & BMP_SPI_WRITE_MASK);
    tx[1] = val;
    rc = spi_transfer(dev->bus, dev->cs, tx, rx, sizeof(tx));
    if (rc != EM4_OK) {
        dev->n_err++;
    }
    return rc;
}

/* オーバーサンプリングの設定値（ctrl_meas の osrs_t / osrs_p の 3 ビット） */
static uint8_t osr_to_bits(bmp_osr_t osr)
{
    uint8_t bits;

    switch (osr) {
    case BMP_OSR_SKIP:
        bits = 0x00U;
        break;
    case BMP_OSR_1:
        bits = 0x01U;
        break;
    case BMP_OSR_2:
        bits = 0x02U;
        break;
    case BMP_OSR_4:
        bits = 0x03U;
        break;
    case BMP_OSR_8:
        bits = 0x04U;
        break;
    case BMP_OSR_16:
        bits = 0x05U;
        break;
    }
    return bits;
}

static em4_err_t bmp_read_calib(bmp_dev_t *dev)
{
    uint8_t      raw[24];
    bmp_calib_t *c = &dev->cal;
    em4_err_t    rc;

    rc = bmp_read_regs(dev, BMP_REG_CALIB, raw, sizeof(raw));
    if (rc != EM4_OK) {
        return rc;
    }
    c->dig_T1 = EM4_MK16(raw[1], raw[0]);
    c->dig_T2 = (int16_t)EM4_MK16(raw[3], raw[2]);
    c->dig_T3 = (int16_t)EM4_MK16(raw[5], raw[4]);
    c->dig_P1 = EM4_MK16(raw[7], raw[6]);
    c->dig_P2 = (int16_t)EM4_MK16(raw[9], raw[8]);
    c->dig_P3 = (int16_t)EM4_MK16(raw[11], raw[10]);
    c->dig_P4 = (int16_t)EM4_MK16(raw[13], raw[12]);
    c->dig_P5 = (int16_t)EM4_MK16(raw[15], raw[14]);
    c->dig_P6 = (int16_t)EM4_MK16(raw[17], raw[16]);
    c->dig_P7 = (int16_t)EM4_MK16(raw[19], raw[18]);
    c->dig_P8 = (int16_t)EM4_MK16(raw[21], raw[20]);
    c->dig_P9 = (int16_t)EM4_MK16(raw[23], raw[22]);

    if ((c->dig_T1 == 0U) || (c->dig_P1 == 0U)) {
        /* 0 だと補正の式で 0 除算になる。NVM が読めていない */
        return EM4_ERR_FORMAT;
    }
    return EM4_OK;
}

em4_err_t bmp_init(bmp_dev_t *dev, uint8_t bus, uint8_t cs, bmp_osr_t osr_t, bmp_osr_t osr_p)
{
    em4_err_t rc;
    uint8_t   id = 0U;
    uint8_t   ctrl;
    uint8_t   conf;

    if (dev == NULL) {
        return EM4_ERR_PARAM;
    }
    (void)memset(dev, 0, sizeof(*dev));
    dev->bus   = bus;
    dev->cs    = cs;
    dev->osr_t = osr_t;
    dev->osr_p = osr_p;

    rc = bmp_write_reg(dev, BMP_REG_RESET, BMP_RESET_CMD);
    if (rc != EM4_OK) {
        goto fail;
    }
    timer_delay_ms(3U);

    rc = bmp_read_regs(dev, BMP_REG_ID, &id, 1U);
    if (rc != EM4_OK) {
        goto fail;
    }
    if (id != BMP_CHIP_ID) {
        LOG_E(TAG, "unexpected chip id 0x%02X", id);
        rc = EM4_ERR_NOTFOUND;
        goto fail;
    }

    rc = bmp_read_calib(dev);
    if (rc != EM4_OK) {
        goto fail;
    }

    conf = (uint8_t)((BMP_STANDBY_125MS << 5) | (BMP_FILTER_X4 << 2));
    rc = bmp_write_reg(dev, BMP_REG_CONFIG, conf);
    if (rc != EM4_OK) {
        goto fail;
    }

    /* 測定を始める（normal モード） */
    ctrl = (uint8_t)((osr_to_bits(osr_t) << 5) | (osr_to_bits(osr_p) << 2) | BMP_MODE_NORMAL);
    bmp_write_reg(dev, BMP_REG_CTRL_MEAS, ctrl);

    dev->ready = true;
    LOG_I(TAG, "ready (ctrl=0x%02X conf=0x%02X)", ctrl, conf);
    return EM4_OK;

fail:
    LOG_E(TAG, "init failed (%s)", em4_strerror(rc));
    return rc;
}

/* 温度の補正（0.01 °C）。dev->t_fine を更新する */
static int32_t bmp_comp_temp(bmp_dev_t *dev, int32_t adc_t)
{
    const bmp_calib_t *c = &dev->cal;
    int32_t var1;
    int32_t var2;

    var1 = ((((adc_t >> 3) - ((int32_t)c->dig_T1 << 1))) * ((int32_t)c->dig_T2)) >> 11;
    var2 = (((((adc_t >> 4) - ((int32_t)c->dig_T1)) * ((adc_t >> 4) - ((int32_t)c->dig_T1))) >> 12) *
            ((int32_t)c->dig_T3)) >> 14;
    dev->t_fine = var1 + var2;
    return ((dev->t_fine * 5) + 128) >> 8;
}

/* 気圧の補正（Pa）。先に bmp_comp_temp を呼んで t_fine を求めておくこと */
static uint32_t bmp_comp_press(const bmp_dev_t *dev, int32_t adc_p)
{
    const bmp_calib_t *c = &dev->cal;
    int64_t var1;
    int64_t var2;
    int64_t p;

    var1 = ((int64_t)dev->t_fine) - 128000;
    var2 = var1 * var1 * (int64_t)c->dig_P6;
    var2 = var2 + ((var1 * (int64_t)c->dig_P5) << 17);
    var2 = var2 + (((int64_t)c->dig_P4) << 35);
    var1 = ((var1 * var1 * (int64_t)c->dig_P3) >> 8) + ((var1 * (int64_t)c->dig_P2) << 12);
    var1 = (((((int64_t)1) << 47) + var1)) * ((int64_t)c->dig_P1) >> 33;
    if (var1 == 0) {
        return 0U;      /* 0 除算を避ける */
    }
    p    = 1048576 - adc_p;
    p    = (((p << 31) - var2) * 3125) / var1;
    var1 = (((int64_t)c->dig_P9) * (p >> 13) * (p >> 13)) >> 25;
    var2 = (((int64_t)c->dig_P8) * p) >> 19;
    p    = ((p + var1 + var2) >> 8) + (((int64_t)c->dig_P7) << 4);
    return (uint32_t)(p >> 8);      /* Q24.8 → Pa */
}

em4_err_t bmp_read(bmp_dev_t *dev, int32_t *temp_c100, uint32_t *press_pa)
{
    uint8_t   raw[6];
    int32_t   adc_p;
    int32_t   adc_t;
    int32_t   t;
    uint32_t  p;
    em4_err_t rc;

    if ((dev == NULL) || (temp_c100 == NULL) || (press_pa == NULL)) {
        return EM4_ERR_PARAM;
    }
    if (!dev->ready) {
        return EM4_ERR_STATE;
    }

    rc = bmp_read_regs(dev, BMP_REG_DATA, raw, sizeof(raw));
    if (rc != EM4_OK) {
        return rc;
    }
    adc_p = (int32_t)(((uint32_t)raw[0] << 12) | ((uint32_t)raw[1] << 4) | ((uint32_t)raw[2] >> 4));
    adc_t = (int32_t)(((uint32_t)raw[3] << 12) | ((uint32_t)raw[4] << 4) | ((uint32_t)raw[5] >> 4));
    if (adc_t == 0x80000) {
        /* 温度を測っていない（osrs_t = skip） */
        return EM4_ERR_STATE;
    }

    t = bmp_comp_temp(dev, adc_t);
    p = bmp_comp_press(dev, adc_p);
    if ((p < 30000U) || (p > 110000U)) {
        LOG_W(TAG, "pressure out of range: %lu Pa", (unsigned long)p);
        return EM4_ERR_RANGE;
    }

    *temp_c100 = t;
    *press_pa  = p;

    dev->press_hist[dev->hist_pos] = p;
    dev->hist_pos = (uint8_t)((dev->hist_pos + 1U) % BMP_AVG_DEPTH);
    if (dev->hist_n < BMP_AVG_DEPTH) {
        dev->hist_n++;
    }
    return EM4_OK;
}

/*
 * 直近の気圧の平均。
 * まだ 1 回も測れていないときは EM4_ERR_EMPTY を返す（呼び出し元は前の値を使い続ける）。
 */
em4_err_t bmp_get_average(const bmp_dev_t *dev, uint32_t *press_pa)
{
    em4_err_t rc = EM4_OK;
    uint64_t  sum = 0U;
    uint8_t   i;

    if ((dev == NULL) || (press_pa == NULL)) {
        return EM4_ERR_PARAM;
    }
    if (dev->hist_n == 0U) {
        rc = EM4_ERR_EMPTY;
    }
    for (i = 0U; i < dev->hist_n; i++) {
        sum += dev->press_hist[i];
    }
    *press_pa = (uint32_t)(sum / dev->hist_n);
    return rc;
}

em4_err_t bmp_sleep(bmp_dev_t *dev)
{
    em4_err_t rc;

    if (dev == NULL) {
        return EM4_ERR_PARAM;
    }
    rc = bmp_write_reg(dev, BMP_REG_CTRL_MEAS, 0x00U);
    if (rc == EM4_OK) {
        dev->ready = false;
    }
    return rc;
}
