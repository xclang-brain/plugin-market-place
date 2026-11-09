/**
 * @file    hal.h
 * @brief   ハードウェア抽象化層（I2C、SPI、UART、GPIO、タイマー、ウォッチドッグ）
 *
 * ホスト向けのビルドでは、周辺装置は src/hal/sim_bus.c で模擬する。
 * The register layout of the simulated I2C controller follows the
 * STM32F4 I2C block so that the driver code stays close to the target.
 */
#ifndef EM4_HAL_H
#define EM4_HAL_H

#include "em4_types.h"
#include "em4_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ===================================================================== */
/* I2C                                                                   */
/* ===================================================================== */
typedef struct {
    volatile uint32_t CR1;
    volatile uint32_t CR2;
    volatile uint32_t OAR1;
    volatile uint32_t DR;
    volatile uint32_t SR1;
    volatile uint32_t SR2;
    volatile uint32_t CCR;
} sim_i2c_regs_t;

#define I2C_CR1_PE          EM4_BIT(0)
#define I2C_CR1_START       EM4_BIT(8)
#define I2C_CR1_STOP        EM4_BIT(9)
#define I2C_CR1_ACK         EM4_BIT(10)
#define I2C_CR1_SWRST       EM4_BIT(15)

#define I2C_SR1_SB          EM4_BIT(0)
#define I2C_SR1_ADDR        EM4_BIT(1)
#define I2C_SR1_BTF         EM4_BIT(2)
#define I2C_SR1_RXNE        EM4_BIT(6)
#define I2C_SR1_TXE         EM4_BIT(7)
#define I2C_SR1_AF          EM4_BIT(10)

#define I2C_SR2_MSL         EM4_BIT(0)
#define I2C_SR2_BUSY        EM4_BIT(1)
#define I2C_SR2_TRA         EM4_BIT(2)

#define I2C_BUS_COUNT       (2U)
#define I2C_TIMEOUT_LOOPS   (1000U)

em4_err_t i2c_init(uint8_t bus, uint32_t speed_hz);
em4_err_t i2c_write(uint8_t bus, uint8_t addr7, const uint8_t *data, size_t len);
em4_err_t i2c_read(uint8_t bus, uint8_t addr7, uint8_t *data, size_t len);
uint32_t  i2c_error_count(uint8_t bus);

/* ===================================================================== */
/* SPI                                                                   */
/* ===================================================================== */
#define SPI_BUS_COUNT       (1U)
#define SPI_CS_COUNT        (2U)

em4_err_t spi_init(uint8_t bus, uint32_t speed_hz);
em4_err_t spi_transfer(uint8_t bus, uint8_t cs, const uint8_t *tx, uint8_t *rx, size_t len);

/* ===================================================================== */
/* UART（ポート 0 は保守用のコンソール、ポート 1 は上りリンク）          */
/* ===================================================================== */
#define UART_CONSOLE        (0U)
#define UART_UPLINK         (1U)
#define UART_PORT_COUNT     (2U)
#define UART_FIFO_SIZE      (512U)

em4_err_t uart_init(uint8_t port, uint32_t baud);
/* 受信したバイト数を返す。受信の誤り（フレーミング、オーバーラン）のときは -1 */
int       uart_read(uint8_t port, uint8_t *buf, size_t max);
int       uart_write(uint8_t port, const uint8_t *buf, size_t len);
/* 模擬：受信側にバイトを入れる／送信されたバイトを取り出す */
em4_err_t uart_sim_inject(uint8_t port, const uint8_t *data, size_t len);
size_t    uart_sim_drain(uint8_t port, uint8_t *out, size_t max);
void      uart_sim_echo_stdout(uint8_t port, bool on);

/* ===================================================================== */
/* 模擬環境                                                              */
/* ===================================================================== */
typedef enum {
    SIM_FAULT_NONE = 0,
    SIM_FAULT_I2C_NACK,
    SIM_FAULT_I2C_CRC,
    SIM_FAULT_SPI_STUCK,
    SIM_FAULT_UART_ERR
} sim_fault_t;

void sim_env_set(int32_t temp_c100, uint32_t rh_c100, uint32_t press_pa);
void sim_env_step(uint32_t elapsed_ms);
void sim_fault_inject(sim_fault_t fault, uint32_t count);

/* ===================================================================== */
/* GPIO                                                                  */
/* ===================================================================== */
typedef enum {
    GPIO_MODE_DISABLED = 0,
    GPIO_MODE_INPUT,
    GPIO_MODE_OUTPUT,
    GPIO_MODE_IRQ_RISING,
    GPIO_MODE_IRQ_FALLING,
    GPIO_MODE_IRQ_BOTH
} gpio_mode_t;

typedef void (*gpio_irq_fn)(uint8_t pin, void *ctx);

em4_err_t gpio_config(uint8_t pin, gpio_mode_t mode);
em4_err_t gpio_set(uint8_t pin, bool level);
em4_err_t gpio_get(uint8_t pin, bool *level);
em4_err_t gpio_toggle(uint8_t pin);
em4_err_t gpio_attach_irq(uint8_t pin, gpio_irq_fn fn, void *ctx);
/* 割り込みの処理（timer_tick_isr から呼ぶ） */
void      gpio_irq_dispatch(uint8_t pin);
uint16_t  gpio_irq_take_pending(void);
uint32_t  gpio_irq_count(uint8_t pin);
/* 模擬：外部からピンの電圧を変える */
void      gpio_sim_drive(uint8_t pin, bool level);

/* ===================================================================== */
/* タイマー、ウォッチドッグ、リセット                                    */
/* ===================================================================== */
typedef void (*timer_cb_t)(void *ctx);

void      timer_init(void);
uint32_t  timer_now_ms(void);
void      timer_delay_ms(uint32_t ms);
void      timer_tick_isr(void);
void      timer_sim_advance(uint32_t ms);
void      timer_set_realtime(bool on);
em4_err_t timer_sw_start(uint8_t id, uint32_t period_ms, timer_cb_t cb, void *ctx);
em4_err_t timer_sw_stop(uint8_t id);

void      wdt_init(uint32_t timeout_ms);
void      wdt_kick(void);
bool      wdt_expired(void);

/* 機器をリセットする（ホストではプロセスを終える） */
void      platform_reset(void);
void      platform_set_reset_hook(void (*hook)(void));

#ifdef __cplusplus
}
#endif

#endif /* EM4_HAL_H */
