/**
 * @file    sht3x.c
 * @brief   温湿度センサー（Sensirion SHT3x 互換）のドライバー
 *
 * 単発測定（高い繰り返し精度、クロックストレッチなし）を使う。
 * Datasheet: SHT3x-DIS, Version 6 (2019).
 */
#include <string.h>

#include "drivers.h"
#include "hal.h"
#include "proto.h"
#include "app.h"

#define SHT_CMD_MEAS_HIGH       (0x2400U)
#define SHT_CMD_SOFT_RESET      (0x30A2U)
#define SHT_CMD_HEATER_ON       (0x306DU)
#define SHT_CMD_HEATER_OFF      (0x3066U)
#define SHT_CMD_READ_STATUS     (0xF32DU)
#define SHT_CMD_CLEAR_STATUS    (0x3041U)

#define SHT_MEAS_WAIT_MS        (16U)   /* 高い繰り返し精度で最大 15.5 ms */
#define SHT_RESET_WAIT_MS       (2U)
#define SHT_RETRY_WAIT_MS       (2U)

#define SHT_STATUS_ALERT        (0x8000U)
#define SHT_STATUS_HEATER       (0x2000U)
#define SHT_STATUS_RESET        (0x0010U)

static const char *const TAG = "sht";

static em4_err_t sht_send_cmd(const sht_dev_t *dev, uint16_t cmd)
{
    uint8_t buf[2];

    buf[0] = EM4_HI8(cmd);
    buf[1] = EM4_LO8(cmd);
    return i2c_write(dev->bus, dev->addr, buf, sizeof(buf));
}

/* 2 バイトの値と CRC8 の組を確かめる */
static bool sht_check_word(const uint8_t *p, uint16_t *out)
{
    if (crc8_sensirion(p, 2U) != p[2]) {
        return false;
    }
    *out = EM4_MK16(p[0], p[1]);
    return true;
}

static em4_err_t sht_measure_raw(sht_dev_t *dev, uint16_t *raw_t, uint16_t *raw_h)
{
    uint8_t   rx[6];
    em4_err_t rc;

    rc = sht_send_cmd(dev, SHT_CMD_MEAS_HIGH);
    if (rc != EM4_OK) {
        dev->n_bus_err++;
        return rc;
    }
    timer_delay_ms(SHT_MEAS_WAIT_MS);

    rc = i2c_read(dev->bus, dev->addr, rx, sizeof(rx));
    if (rc != EM4_OK) {
        dev->n_bus_err++;
        return rc;
    }
    if (!sht_check_word(&rx[0], raw_t) || !sht_check_word(&rx[3], raw_h)) {
        dev->n_crc_err++;
        return EM4_ERR_CRC;
    }
    return EM4_OK;
}

/*
 * 温度の換算: T[°C] = -45 + 175 * raw / 65535
 * 0.01 °C 単位で返す。
 */
static int32_t sht_raw_to_c100(uint16_t raw)
{
    return (int32_t)(((uint32_t)raw * 17500U) / 65535U) - 4500;
}

/* 湿度の換算: RH[%] = 100 * raw / 65535 */
static uint32_t sht_raw_to_rh100(uint16_t raw)
{
    return ((uint32_t)raw * 10000U) / 65535U;
}

em4_err_t sht_init(sht_dev_t *dev, uint8_t bus, uint8_t addr, uint8_t retries)
{
    em4_err_t rc;
    uint16_t  status = 0U;

    if (dev == NULL) {
        return EM4_ERR_PARAM;
    }
    (void)memset(dev, 0, sizeof(*dev));
    dev->bus     = bus;
    dev->addr    = addr;
    dev->retries = retries;

    rc = sht_soft_reset(dev);
    if (rc != EM4_OK) {
        LOG_E(TAG, "soft reset failed (%s)", em4_strerror(rc));
        return rc;
    }
    rc = sht_read_status(dev, &status);
    if (rc != EM4_OK) {
        return rc;
    }
    if ((status & SHT_STATUS_RESET) == 0U) {
        LOG_W(TAG, "reset flag not set after soft reset (status=0x%04X)", status);
    }
    (void)sht_send_cmd(dev, SHT_CMD_CLEAR_STATUS);

    dev->ready = true;
    LOG_I(TAG, "ready (bus=%u addr=0x%02X retries=%u)", bus, addr, retries);
    return EM4_OK;
}

em4_err_t sht_soft_reset(sht_dev_t *dev)
{
    em4_err_t rc;

    if (dev == NULL) {
        return EM4_ERR_PARAM;
    }
    rc = sht_send_cmd(dev, SHT_CMD_SOFT_RESET);
    if (rc == EM4_OK) {
        timer_delay_ms(SHT_RESET_WAIT_MS);
        dev->heater_on = false;
    }
    return rc;
}

em4_err_t sht_read_status(sht_dev_t *dev, uint16_t *status)
{
    uint8_t   rx[3];
    em4_err_t rc;

    if ((dev == NULL) || (status == NULL)) {
        return EM4_ERR_PARAM;
    }
    rc = sht_send_cmd(dev, SHT_CMD_READ_STATUS);
    if (rc != EM4_OK) {
        return rc;
    }
    rc = i2c_read(dev->bus, dev->addr, rx, sizeof(rx));
    if (rc != EM4_OK) {
        return rc;
    }
    if (!sht_check_word(rx, status)) {
        return EM4_ERR_CRC;
    }
    return EM4_OK;
}

em4_err_t sht_heater(sht_dev_t *dev, bool on)
{
    em4_err_t rc;

    if (dev == NULL) {
        return EM4_ERR_PARAM;
    }
    rc = sht_send_cmd(dev, on ? SHT_CMD_HEATER_ON : SHT_CMD_HEATER_OFF);
    if (rc == EM4_OK) {
        dev->heater_on = on;
    }
    return rc;
}

/*
 * 1 回測る。失敗したら dev->retries 回までやり直す。
 * 成功した値は移動平均の履歴にも入れる。
 */
em4_err_t sht_read(sht_dev_t *dev, sht_sample_t *out)
{
    em4_err_t rc;
    uint16_t  raw_t = 0U;
    uint16_t  raw_h = 0U;
    uint8_t   attempt;

    if ((dev == NULL) || (out == NULL)) {
        return EM4_ERR_PARAM;
    }
    if (!dev->ready) {
        return EM4_ERR_STATE;
    }

    for (attempt = 0U; attempt < dev->retries; attempt++) {
        rc = sht_measure_raw(dev, &raw_t, &raw_h);
        if (rc == EM4_OK) {
            break;
        }
        LOG_D(TAG, "measure failed (%s), retry %u", em4_strerror(rc), (unsigned)attempt + 1U);
        timer_delay_ms(SHT_RETRY_WAIT_MS);
    }
    if (rc != EM4_OK) {
        out->valid = false;
        return rc;
    }

    out->temp_c100 = sht_raw_to_c100(raw_t);
    out->rh_c100   = sht_raw_to_rh100(raw_h);
    out->ts_ms     = timer_now_ms();
    out->valid     = true;
    dev->n_ok++;

    dev->hist_t[dev->hist_pos] = out->temp_c100;
    dev->hist_h[dev->hist_pos] = out->rh_c100;
    dev->hist_pos = (uint8_t)((dev->hist_pos + 1U) % SHT_AVG_DEPTH);
    if (dev->hist_n < SHT_AVG_DEPTH) {
        dev->hist_n++;
    }
    return EM4_OK;
}

/* 移動平均（直近 SHT_AVG_DEPTH 回） */
em4_err_t sht_filter_get(const sht_dev_t *dev, sht_sample_t *out)
{
    uint32_t sum_t = 0U;
    uint32_t sum_h = 0U;
    uint8_t  i;
    uint8_t  n;

    if ((dev == NULL) || (out == NULL)) {
        return EM4_ERR_PARAM;
    }
    n = dev->hist_n;
    if (n == 0U) {
        out->valid = false;
        return EM4_ERR_EMPTY;
    }
    for (i = 0U; i < n; i++) {
        sum_t += dev->hist_t[i];
        sum_h += dev->hist_h[i];
    }
    out->temp_c100 = (int32_t)(sum_t / n);
    out->rh_c100   = sum_h / n;
    out->ts_ms     = timer_now_ms();
    out->valid     = true;
    return EM4_OK;
}

void sht_get_stats(const sht_dev_t *dev, sht_stats_t *st)
{
    const uint32_t total = dev->n_ok + dev->n_crc_err + dev->n_bus_err;

    if ((dev == NULL) || (st == NULL)) {
        return;
    }
    st->n_ok      = dev->n_ok;
    st->n_crc_err = dev->n_crc_err;
    st->n_bus_err = dev->n_bus_err;
    st->err_pct   = (total == 0U) ? 0U : (((dev->n_crc_err + dev->n_bus_err) * 100U) / total);
}
