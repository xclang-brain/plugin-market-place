/**
 * @file    sim_bus.c
 * @brief   模擬のバス（I2C、SPI、UART）と、つながる装置の模型
 *
 * ホスト向けのビルドで、実機の周辺装置の代わりに動く。
 *   - I2C0 : 温湿度センサー（SHT3x 互換、アドレス 0x44）
 *   - SPI0 : 気圧センサー（BMP280 互換、CS0）
 *   - UART0: 保守用のコンソール、UART1: 上りリンク
 *
 * I2C はレジスタの動きまで模擬する。ドライバーの書き方を実機（STM32F4）と
 * 同じにしておくため。
 */
#include <stdio.h>
#include <string.h>

#include "hal.h"
#include "proto.h"
#include "util.h"

/* ===================================================================== */
/* 模擬環境                                                              */
/* ===================================================================== */
typedef struct {
    int32_t  temp_c100;
    uint32_t rh_c100;
    uint32_t press_pa;
    int32_t  base_temp_c100;
    uint32_t t_ms;
} sim_env_t;

static sim_env_t s_env = { 2350, 4500U, 101325U, 2350, 0U };

static sim_fault_t s_fault = SIM_FAULT_NONE;
static uint32_t    s_fault_left = 0U;

static bool sim_fault_hit(sim_fault_t f)
{
    bool hit = false;

    if ((s_fault == f) && (s_fault_left > 0U)) {
        s_fault_left--;
        if (s_fault_left == 0U) {
            s_fault = SIM_FAULT_NONE;
        }
        hit = true;
    }
    return hit;
}

void sim_env_set(int32_t temp_c100, uint32_t rh_c100, uint32_t press_pa)
{
    s_env.temp_c100      = temp_c100;
    s_env.base_temp_c100 = temp_c100;
    s_env.rh_c100        = (rh_c100 > 10000U) ? 10000U : rh_c100;
    s_env.press_pa       = press_pa;
    s_env.t_ms           = 0U;          /* 揺らぎの位相も最初に戻す */
}

/*
 * 時間の経過で温度をゆっくり揺らす（周期 60 秒、振幅 0.40 °C の三角波）。
 * 位相 0 で揺らぎも 0。試験で値を固定したいときは sim_env_set を呼び直す。
 */
void sim_env_step(uint32_t elapsed_ms)
{
    uint32_t phase;
    int32_t  tri;

    s_env.t_ms += elapsed_ms;
    phase = (s_env.t_ms / 100U) % 600U;          /* 0..599 */
    if (phase < 150U) {
        tri = (int32_t)phase;                    /* 0..149 */
    } else if (phase < 450U) {
        tri = 300 - (int32_t)phase;              /* 150..-149 */
    } else {
        tri = (int32_t)phase - 600;              /* -150..-1 */
    }
    s_env.temp_c100 = s_env.base_temp_c100 + ((tri * 40) / 150);
}

void sim_fault_inject(sim_fault_t fault, uint32_t count)
{
    s_fault      = fault;
    s_fault_left = count;
}

/* ===================================================================== */
/* 温湿度センサーの模型（SHT3x）                                         */
/* ===================================================================== */
#define SHTM_CMD_MEAS_HIGH      (0x2400U)
#define SHTM_CMD_SOFT_RESET     (0x30A2U)
#define SHTM_CMD_HEATER_ON      (0x306DU)
#define SHTM_CMD_HEATER_OFF     (0x3066U)
#define SHTM_CMD_READ_STATUS    (0xF32DU)
#define SHTM_CMD_CLEAR_STATUS   (0x3041U)
#define SHTM_STATUS_HEATER      (0x2000U)
#define SHTM_STATUS_RESET       (0x0010U)

typedef struct {
    uint8_t  cmd_buf[2];
    uint8_t  cmd_len;
    uint8_t  out[6];
    uint8_t  out_len;
    uint8_t  out_pos;
    uint16_t status;
} sht_model_t;

static sht_model_t s_shtm = { .status = SHTM_STATUS_RESET };

static void shtm_put_word(uint8_t *dst, uint16_t w, bool corrupt)
{
    dst[0] = EM4_HI8(w);
    dst[1] = EM4_LO8(w);
    dst[2] = crc8_sensirion(dst, 2U);
    if (corrupt) {
        dst[2] ^= 0x5AU;
    }
}

static void shtm_measure(void)
{
    int32_t  t = s_env.temp_c100;
    uint32_t raw_t;
    uint32_t raw_h;
    bool     bad = sim_fault_hit(SIM_FAULT_I2C_CRC);

    if ((s_shtm.status & SHTM_STATUS_HEATER) != 0U) {
        t += 300;   /* ヒーターを入れると 3 °C ほど高く出る */
    }
    if (t < -4500) {
        t = -4500;
    }
    /* T = -45 + 175 * raw / 65535 の逆 */
    raw_t = ((uint32_t)(t + 4500) * 65535U) / 17500U;
    if (raw_t > 0xFFFFU) {
        raw_t = 0xFFFFU;
    }
    raw_h = (s_env.rh_c100 * 65535U) / 10000U;

    shtm_put_word(&s_shtm.out[0], (uint16_t)raw_t, bad);
    shtm_put_word(&s_shtm.out[3], (uint16_t)raw_h, false);
    s_shtm.out_len = 6U;
    s_shtm.out_pos = 0U;
}

static void shtm_command(uint16_t cmd)
{
    switch (cmd) {
    case SHTM_CMD_MEAS_HIGH:
        shtm_measure();
        break;
    case SHTM_CMD_SOFT_RESET:
        s_shtm.status  = SHTM_STATUS_RESET;
        s_shtm.out_len = 0U;
        break;
    case SHTM_CMD_HEATER_ON:
        s_shtm.status |= SHTM_STATUS_HEATER;
        break;
    case SHTM_CMD_HEATER_OFF:
        s_shtm.status &= (uint16_t)~SHTM_STATUS_HEATER;
        break;
    case SHTM_CMD_READ_STATUS:
        shtm_put_word(&s_shtm.out[0], s_shtm.status, false);
        s_shtm.out_len = 3U;
        s_shtm.out_pos = 0U;
        break;
    case SHTM_CMD_CLEAR_STATUS:
        s_shtm.status &= (uint16_t)~SHTM_STATUS_RESET;
        break;
    default:
        /* 知らないコマンドは無視する（実機は NACK を返す） */
        break;
    }
}

static void shtm_write_byte(uint8_t b)
{
    s_shtm.cmd_buf[s_shtm.cmd_len] = b;
    s_shtm.cmd_len++;
    if (s_shtm.cmd_len == 2U) {
        shtm_command(EM4_MK16(s_shtm.cmd_buf[0], s_shtm.cmd_buf[1]));
        s_shtm.cmd_len = 0U;
    }
}

static uint8_t shtm_read_byte(void)
{
    uint8_t b = 0xFFU;

    if (s_shtm.out_pos < s_shtm.out_len) {
        b = s_shtm.out[s_shtm.out_pos];
        s_shtm.out_pos++;
    }
    return b;
}

/* ===================================================================== */
/* I2C コントローラー                                                    */
/* ===================================================================== */
typedef enum {
    I2C_PH_IDLE = 0,
    I2C_PH_ADDR,
    I2C_PH_TX,
    I2C_PH_RX
} i2c_phase_t;

typedef struct {
    i2c_phase_t phase;
    bool        dr_written;
    bool        addr_pending;
    bool        init;
    uint32_t    speed_hz;
    uint32_t    errors;
} i2c_state_t;

static sim_i2c_regs_t s_i2c[I2C_BUS_COUNT];
static i2c_state_t    s_i2c_st[I2C_BUS_COUNT];

/* 模擬：ハードウェアを 1 歩進める */
static void i2c_hw_step(uint8_t bus)
{
    sim_i2c_regs_t *r  = &s_i2c[bus];
    i2c_state_t    *st = &s_i2c_st[bus];

    if ((r->CR1 & I2C_CR1_START) != 0U) {
        r->CR1 &= ~I2C_CR1_START;
        r->SR1 |= I2C_SR1_SB;
        r->SR2 |= (I2C_SR2_MSL | I2C_SR2_BUSY);
        st->phase      = I2C_PH_ADDR;
        st->dr_written = false;
        return;
    }
    if ((r->CR1 & I2C_CR1_STOP) != 0U) {
        r->CR1 &= ~I2C_CR1_STOP;
        r->SR1    = 0U;
        r->SR2    = 0U;
        st->phase = I2C_PH_IDLE;
        s_shtm.cmd_len = 0U;
        return;
    }

    switch (st->phase) {
    case I2C_PH_ADDR:
        if (st->dr_written) {
            uint8_t a    = (uint8_t)r->DR;
            bool    rd   = ((a & 0x01U) != 0U);
            bool    nack = sim_fault_hit(SIM_FAULT_I2C_NACK);

            st->dr_written = false;
            r->SR1 &= ~I2C_SR1_SB;
            if ((bus == SHT_I2C_BUS) && ((a >> 1) == SHT_I2C_ADDR) && !nack) {
                r->SR1 |= I2C_SR1_ADDR;
                st->addr_pending = true;
                if (rd) {
                    st->phase = I2C_PH_RX;
                } else {
                    st->phase = I2C_PH_TX;
                    r->SR2 |= I2C_SR2_TRA;
                    r->SR1 |= I2C_SR1_TXE;
                }
            } else {
                r->SR1 |= I2C_SR1_AF;
            }
        }
        break;

    case I2C_PH_TX:
        if (st->dr_written) {
            shtm_write_byte((uint8_t)r->DR);
            st->dr_written = false;
            r->SR1 |= (I2C_SR1_TXE | I2C_SR1_BTF);
        }
        break;

    case I2C_PH_RX:
        if (!st->addr_pending && ((r->SR1 & I2C_SR1_RXNE) == 0U)) {
            r->DR   = shtm_read_byte();
            r->SR1 |= I2C_SR1_RXNE;
        }
        break;

    case I2C_PH_IDLE:
    default:
        break;
    }
}

/* 模擬：SR1 → SR2 の読み出しで ADDR が消える、という副作用 */
static void i2c_hw_addr_cleared(uint8_t bus)
{
    s_i2c[bus].SR1 &= ~I2C_SR1_ADDR;
    s_i2c_st[bus].addr_pending = false;
}

static void i2c_put_dr(uint8_t bus, uint8_t b)
{
    s_i2c[bus].DR   = b;
    s_i2c[bus].SR1 &= ~(I2C_SR1_TXE | I2C_SR1_BTF);
    s_i2c_st[bus].dr_written = true;
}

static uint8_t i2c_get_dr(uint8_t bus)
{
    uint8_t b = (uint8_t)s_i2c[bus].DR;

    s_i2c[bus].SR1 &= ~I2C_SR1_RXNE;
    return b;
}

static em4_err_t i2c_wait(uint8_t bus, uint32_t flag)
{
    sim_i2c_regs_t *r = &s_i2c[bus];
    uint32_t loops = 0U;

    while ((r->SR1 & flag) == 0U) {
        if ((r->SR1 & I2C_SR1_AF) != 0U) {
            r->SR1 &= ~I2C_SR1_AF;
            return EM4_ERR_IO;          /* NACK */
        }
        if (++loops > I2C_TIMEOUT_LOOPS) {
            return EM4_ERR_TIMEOUT;
        }
        i2c_hw_step(bus);
    }
    return EM4_OK;
}

static void i2c_clear_addr(uint8_t bus)
{
    sim_i2c_regs_t *r = &s_i2c[bus];
    uint32_t tmp;

    /*
     * ADDR フラグは、SR1 を読んでから SR2 を読むと消える（RM0090 27.6.6）。
     * 読んだ値は使わない。読み出すこと自体が目的。
     */
    tmp = r->SR1;
    tmp = r->SR2;
    i2c_hw_addr_cleared(bus);
}

static em4_err_t i2c_start(uint8_t bus, uint8_t addr_rw)
{
    em4_err_t rc;

    s_i2c[bus].CR1 |= I2C_CR1_START;
    rc = i2c_wait(bus, I2C_SR1_SB);
    if (rc != EM4_OK) {
        return rc;
    }
    i2c_put_dr(bus, addr_rw);
    rc = i2c_wait(bus, I2C_SR1_ADDR);
    if (rc != EM4_OK) {
        return rc;
    }
    i2c_clear_addr(bus);
    return EM4_OK;
}

static void i2c_stop(uint8_t bus)
{
    s_i2c[bus].CR1 |= I2C_CR1_STOP;
    i2c_hw_step(bus);
}

em4_err_t i2c_init(uint8_t bus, uint32_t speed_hz)
{
    sim_i2c_regs_t *r;

    if ((bus >= I2C_BUS_COUNT) || (speed_hz == 0U) || (speed_hz > 400000U)) {
        return EM4_ERR_PARAM;
    }
    r = &s_i2c[bus];
    r->CR1 = I2C_CR1_SWRST;
    r->CR1 = 0U;
    r->CR2 = 42U;                              /* APB1 42 MHz */
    r->CCR = 42000000U / (speed_hz * 2U);
    r->SR1 = 0U;
    r->SR2 = 0U;
    r->CR1 = I2C_CR1_PE | I2C_CR1_ACK;

    (void)memset(&s_i2c_st[bus], 0, sizeof(s_i2c_st[bus]));
    s_i2c_st[bus].speed_hz = speed_hz;
    s_i2c_st[bus].init     = true;
    return EM4_OK;
}

em4_err_t i2c_write(uint8_t bus, uint8_t addr7, const uint8_t *data, size_t len)
{
    em4_err_t rc;
    size_t    i;

    if ((bus >= I2C_BUS_COUNT) || (data == NULL) || (len == 0U)) {
        return EM4_ERR_PARAM;
    }
    if (!s_i2c_st[bus].init) {
        return EM4_ERR_STATE;
    }

    rc = i2c_start(bus, (uint8_t)(addr7 << 1));
    if (rc != EM4_OK) {
        goto stop;
    }
    for (i = 0U; i < len; i++) {
        rc = i2c_wait(bus, I2C_SR1_TXE);
        if (rc != EM4_OK) {
            goto stop;
        }
        i2c_put_dr(bus, data[i]);
    }
    rc = i2c_wait(bus, I2C_SR1_BTF);

stop:
    i2c_stop(bus);
    if (rc != EM4_OK) {
        s_i2c_st[bus].errors++;
    }
    return rc;
}

em4_err_t i2c_read(uint8_t bus, uint8_t addr7, uint8_t *data, size_t len)
{
    em4_err_t rc;
    size_t    i;

    if ((bus >= I2C_BUS_COUNT) || (data == NULL) || (len == 0U)) {
        return EM4_ERR_PARAM;
    }
    if (!s_i2c_st[bus].init) {
        return EM4_ERR_STATE;
    }

    s_i2c[bus].CR1 |= I2C_CR1_ACK;
    rc = i2c_start(bus, (uint8_t)((addr7 << 1) | 0x01U));
    if (rc != EM4_OK) {
        goto stop;
    }
    for (i = 0U; i < len; i++) {
        if (i == (len - 1U)) {
            s_i2c[bus].CR1 &= ~I2C_CR1_ACK;    /* 最後のバイトは NACK */
        }
        rc = i2c_wait(bus, I2C_SR1_RXNE);
        if (rc != EM4_OK) {
            goto stop;
        }
        data[i] = i2c_get_dr(bus);
    }

stop:
    i2c_stop(bus);
    if (rc != EM4_OK) {
        s_i2c_st[bus].errors++;
    }
    return rc;
}

uint32_t i2c_error_count(uint8_t bus)
{
    return (bus < I2C_BUS_COUNT) ? s_i2c_st[bus].errors : 0U;
}

/* ===================================================================== */
/* 気圧センサーの模型（BMP280、SPI）                                     */
/* ===================================================================== */
#define BMPM_REG_CALIB      (0x88U)
#define BMPM_REG_ID         (0xD0U)
#define BMPM_REG_RESET      (0xE0U)
#define BMPM_REG_STATUS     (0xF3U)
#define BMPM_REG_CTRL       (0xF4U)
#define BMPM_REG_CONFIG     (0xF5U)
#define BMPM_REG_DATA       (0xF7U)

typedef struct {
    uint8_t regs[256];
    bool    powered;
} bmp_model_t;

static bmp_model_t s_bmpm;

/* データシートの例の校正値（BST-BMP280-DS001 3.12） */
static const int32_t k_bmpm_cal[12] = {
    27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000
};

static void bmpm_reset(void)
{
    size_t i;

    (void)memset(s_bmpm.regs, 0, sizeof(s_bmpm.regs));
    for (i = 0U; i < EM4_ARRAY_SIZE(k_bmpm_cal); i++) {
        uint16_t v = (uint16_t)k_bmpm_cal[i];
        s_bmpm.regs[BMPM_REG_CALIB + (2U * i)]      = EM4_LO8(v);
        s_bmpm.regs[BMPM_REG_CALIB + (2U * i) + 1U] = EM4_HI8(v);
    }
    s_bmpm.regs[BMPM_REG_ID] = 0x58U;
    /* 測定前のデータは 0x80000（データシートの初期値） */
    s_bmpm.regs[BMPM_REG_DATA + 0U] = 0x80U;
    s_bmpm.regs[BMPM_REG_DATA + 3U] = 0x80U;
    s_bmpm.powered = true;
}

static int32_t bmpm_temp(int32_t adc_t, int32_t *t_fine)
{
    int32_t v1;
    int32_t v2;

    v1 = ((((adc_t >> 3) - (k_bmpm_cal[0] << 1))) * k_bmpm_cal[1]) >> 11;
    v2 = (((((adc_t >> 4) - k_bmpm_cal[0]) * ((adc_t >> 4) - k_bmpm_cal[0])) >> 12) *
          k_bmpm_cal[2]) >> 14;
    *t_fine = v1 + v2;
    return ((*t_fine * 5) + 128) >> 8;
}

static uint32_t bmpm_press(int32_t adc_p, int32_t t_fine)
{
    int64_t v1;
    int64_t v2;
    int64_t p;

    v1 = (int64_t)t_fine - 128000;
    v2 = v1 * v1 * (int64_t)k_bmpm_cal[8];
    v2 = v2 + ((v1 * (int64_t)k_bmpm_cal[7]) * 131072);
    v2 = v2 + ((int64_t)k_bmpm_cal[6] * 34359738368LL);
    v1 = ((v1 * v1 * (int64_t)k_bmpm_cal[5]) / 256) + ((v1 * (int64_t)k_bmpm_cal[4]) * 4096);
    v1 = ((140737488355328LL + v1) * (int64_t)k_bmpm_cal[3]) / 8589934592LL;
    if (v1 == 0) {
        return 0U;
    }
    p  = 1048576 - adc_p;
    p  = (((p * 2147483648LL) - v2) * 3125) / v1;
    v1 = ((int64_t)k_bmpm_cal[11] * (p / 8192) * (p / 8192)) / 33554432;
    v2 = ((int64_t)k_bmpm_cal[10] * p) / 524288;
    p  = ((p + v1 + v2) / 256) + ((int64_t)k_bmpm_cal[9] * 16);
    return (uint32_t)(p / 256);
}

/* いまの環境の値になる ADC の値を二分探索で求める */
static void bmpm_update_data(void)
{
    int32_t lo = 0;
    int32_t hi = 0xFFFFF;
    int32_t t_fine = 0;
    int32_t adc_t;
    int32_t adc_p;

    while (lo < hi) {
        int32_t mid = lo + ((hi - lo) / 2);
        if (bmpm_temp(mid, &t_fine) < s_env.temp_c100) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    adc_t = lo;
    (void)bmpm_temp(adc_t, &t_fine);

    lo = 0;
    hi = 0xFFFFF;
    while (lo < hi) {           /* ADC の値が大きいほど気圧は低い */
        int32_t mid = lo + ((hi - lo) / 2);
        if (bmpm_press(mid, t_fine) > s_env.press_pa) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    adc_p = lo;

    s_bmpm.regs[BMPM_REG_DATA + 0U] = (uint8_t)((uint32_t)adc_p >> 12);
    s_bmpm.regs[BMPM_REG_DATA + 1U] = (uint8_t)((uint32_t)adc_p >> 4);
    s_bmpm.regs[BMPM_REG_DATA + 2U] = (uint8_t)(((uint32_t)adc_p & 0x0FU) << 4);
    s_bmpm.regs[BMPM_REG_DATA + 3U] = (uint8_t)((uint32_t)adc_t >> 12);
    s_bmpm.regs[BMPM_REG_DATA + 4U] = (uint8_t)((uint32_t)adc_t >> 4);
    s_bmpm.regs[BMPM_REG_DATA + 5U] = (uint8_t)(((uint32_t)adc_t & 0x0FU) << 4);
}

static void bmpm_write_reg(uint8_t reg, uint8_t val)
{
    if ((reg == BMPM_REG_RESET) && (val == 0xB6U)) {
        bmpm_reset();
        return;
    }
    if ((reg == BMPM_REG_CTRL) || (reg == BMPM_REG_CONFIG)) {
        s_bmpm.regs[reg] = val;
    }
}

static void bmpm_transfer(const uint8_t *tx, uint8_t *rx, size_t len)
{
    size_t  i;
    uint8_t reg;

    if (!s_bmpm.powered) {
        bmpm_reset();
    }
    rx[0] = 0xFFU;
    if ((tx[0] & 0x80U) != 0U) {
        /* 読み出し：アドレスは自動で進む */
        reg = tx[0];
        if ((reg <= BMPM_REG_DATA) && ((reg + len - 1U) > BMPM_REG_DATA)) {
            if ((s_bmpm.regs[BMPM_REG_CTRL] & 0x03U) != 0U) {   /* sleep でないときだけ測る */
                bmpm_update_data();
            }
        }
        for (i = 1U; i < len; i++) {
            rx[i] = s_bmpm.regs[reg];
            reg++;
        }
    } else {
        /* 書き込み：アドレスとデータの組を繰り返す */
        for (i = 0U; (i + 1U) < len; i += 2U) {
            bmpm_write_reg((uint8_t)(tx[i] | 0x80U), tx[i + 1U]);
            rx[i]      = 0xFFU;
            rx[i + 1U] = 0xFFU;
        }
    }
}

/* ===================================================================== */
/* SPI コントローラー                                                    */
/* ===================================================================== */
static bool     s_spi_init[SPI_BUS_COUNT];
static uint32_t s_spi_speed[SPI_BUS_COUNT];

em4_err_t spi_init(uint8_t bus, uint32_t speed_hz)
{
    if ((bus >= SPI_BUS_COUNT) || (speed_hz == 0U)) {
        return EM4_ERR_PARAM;
    }
    s_spi_speed[bus] = speed_hz;
    s_spi_init[bus]  = true;
    return EM4_OK;
}

em4_err_t spi_transfer(uint8_t bus, uint8_t cs, const uint8_t *tx, uint8_t *rx, size_t len)
{
    if ((bus >= SPI_BUS_COUNT) || (cs >= SPI_CS_COUNT) || (tx == NULL) || (rx == NULL) ||
        (len == 0U)) {
        return EM4_ERR_PARAM;
    }
    if (!s_spi_init[bus]) {
        return EM4_ERR_STATE;
    }
    if (sim_fault_hit(SIM_FAULT_SPI_STUCK)) {
        return EM4_ERR_TIMEOUT;     /* BSY が落ちない */
    }
    if ((bus == BMP_SPI_BUS) && (cs == BMP_SPI_CS)) {
        bmpm_transfer(tx, rx, len);
    } else {
        (void)memset(rx, 0xFF, len);   /* 何もつながっていない */
    }
    return EM4_OK;
}

/* ===================================================================== */
/* UART                                                                  */
/* ===================================================================== */
typedef struct {
    ringbuf_t rx;
    ringbuf_t tx;
    uint8_t   rx_mem[UART_FIFO_SIZE];
    uint8_t   tx_mem[UART_FIFO_SIZE];
    uint32_t  baud;
    uint32_t  n_err;
    bool      echo;
    bool      init;
} uart_sim_t;

static uart_sim_t s_uart[UART_PORT_COUNT];

em4_err_t uart_init(uint8_t port, uint32_t baud)
{
    uart_sim_t *u;

    if ((port >= UART_PORT_COUNT) || (baud == 0U)) {
        return EM4_ERR_PARAM;
    }
    u = &s_uart[port];
    (void)rb_init(&u->rx, u->rx_mem, sizeof(u->rx_mem));
    (void)rb_init(&u->tx, u->tx_mem, sizeof(u->tx_mem));
    u->baud  = baud;
    u->n_err = 0U;
    u->init  = true;
    return EM4_OK;
}

int uart_read(uint8_t port, uint8_t *buf, size_t max)
{
    uart_sim_t *u;

    if ((port >= UART_PORT_COUNT) || (buf == NULL)) {
        return -1;
    }
    u = &s_uart[port];
    if (!u->init) {
        return -1;
    }
    if (sim_fault_hit(SIM_FAULT_UART_ERR)) {
        /* フレーミングエラー。受信 FIFO の中身は信用できないので捨てる */
        rb_clear(&u->rx);
        u->n_err++;
        return -1;
    }
    return (int)rb_read(&u->rx, buf, max);
}

int uart_write(uint8_t port, const uint8_t *buf, size_t len)
{
    uart_sim_t *u;

    if ((port >= UART_PORT_COUNT) || (buf == NULL)) {
        return -1;
    }
    u = &s_uart[port];
    if (!u->init) {
        return -1;
    }
    if (u->echo) {
        (void)fwrite(buf, 1U, len, stdout);
        (void)fflush(stdout);
    }
    (void)rb_write(&u->tx, buf, len, true);
    return (int)len;
}

em4_err_t uart_sim_inject(uint8_t port, const uint8_t *data, size_t len)
{
    size_t i;

    if ((port >= UART_PORT_COUNT) || (data == NULL)) {
        return EM4_ERR_PARAM;
    }
    for (i = 0U; i < len; i++) {
        if (rb_push(&s_uart[port].rx, data[i]) != EM4_OK) {
            return EM4_ERR_FULL;
        }
    }
    return EM4_OK;
}

size_t uart_sim_drain(uint8_t port, uint8_t *out, size_t max)
{
    if ((port >= UART_PORT_COUNT) || (out == NULL)) {
        return 0U;
    }
    return rb_read(&s_uart[port].tx, out, max);
}

void uart_sim_echo_stdout(uint8_t port, bool on)
{
    if (port < UART_PORT_COUNT) {
        s_uart[port].echo = on;
    }
}
