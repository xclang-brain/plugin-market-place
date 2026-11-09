/**
 * @file    gpio.c
 * @brief   GPIO（出力、入力、割り込み）
 *
 * ホスト向けのビルドでは、ピンの状態は配列に持つ。外からの電圧の変化は
 * gpio_sim_drive() で与える。割り込みは保留のビットを立て、
 * timer_tick_isr() の中で gpio_irq_dispatch() を呼んで処理する。
 */
#include <string.h>

#include "hal.h"

typedef struct {
    gpio_mode_t mode;
    bool        out_level;
    bool        in_level;
} gpio_pin_t;

static gpio_pin_t        s_pin[GPIO_PIN_COUNT];
static gpio_irq_fn       s_irq_handler[GPIO_PIN_COUNT];
static void             *s_irq_ctx[GPIO_PIN_COUNT];
static uint32_t          s_irq_count[GPIO_PIN_COUNT];
static volatile uint16_t s_irq_pending;     /* ISR と本体の両方から触る */

/* 出力のレジスタ（ODR）の模擬。ビット n がピン n */
static volatile uint32_t s_odr;

static bool gpio_is_irq_mode(gpio_mode_t mode)
{
    return (mode == GPIO_MODE_IRQ_RISING) || (mode == GPIO_MODE_IRQ_FALLING) ||
           (mode == GPIO_MODE_IRQ_BOTH);
}

em4_err_t gpio_config(uint8_t pin, gpio_mode_t mode)
{
    if (pin >= GPIO_PIN_COUNT) {
        return EM4_ERR_PARAM;
    }
    if (mode > GPIO_MODE_IRQ_BOTH) {
        return EM4_ERR_PARAM;
    }

    em4_irq_disable();
    s_pin[pin].mode = mode;
    if (mode != GPIO_MODE_OUTPUT) {
        s_pin[pin].out_level = false;
        s_odr &= ~(uint32_t)EM4_BIT(pin);
    }
    if (!gpio_is_irq_mode(mode)) {
        s_irq_pending &= (uint16_t)~(1U << pin);
    }
    em4_irq_enable();
    return EM4_OK;
}

em4_err_t gpio_set(uint8_t pin, bool level)
{
    if (pin > GPIO_PIN_COUNT) {
        return EM4_ERR_PARAM;
    }
    if (s_pin[pin].mode != GPIO_MODE_OUTPUT) {
        return EM4_ERR_STATE;
    }

    s_pin[pin].out_level = level;
    if (level) {
        s_odr |= (uint32_t)EM4_BIT(pin);
    } else {
        s_odr &= ~(uint32_t)EM4_BIT(pin);
    }
    return EM4_OK;
}

em4_err_t gpio_get(uint8_t pin, bool *level)
{
    if ((pin >= GPIO_PIN_COUNT) || (level == NULL)) {
        return EM4_ERR_PARAM;
    }
    if (s_pin[pin].mode == GPIO_MODE_OUTPUT) {
        *level = s_pin[pin].out_level;
    } else {
        *level = s_pin[pin].in_level;
    }
    return EM4_OK;
}

em4_err_t gpio_toggle(uint8_t pin)
{
    if (pin >= GPIO_PIN_COUNT) {
        return EM4_ERR_PARAM;
    }
    return gpio_set(pin, !s_pin[pin].out_level);
}

em4_err_t gpio_attach_irq(uint8_t pin, gpio_irq_fn fn, void *ctx)
{
    if (pin >= GPIO_PIN_COUNT) {
        return EM4_ERR_PARAM;
    }
    if (!gpio_is_irq_mode(s_pin[pin].mode)) {
        return EM4_ERR_STATE;
    }

    em4_irq_disable();
    s_irq_handler[pin] = fn;
    s_irq_ctx[pin]     = ctx;
    em4_irq_enable();
    return EM4_OK;
}

/*
 * 割り込みの本体。timer_tick_isr() から、保留のビットが立ったピンごとに呼ばれる。
 */
void gpio_irq_dispatch(uint8_t pin)
{
    s_irq_count[pin]++;
    if ((s_irq_handler[pin] != NULL) && (pin < GPIO_PIN_COUNT)) {
        s_irq_handler[pin](pin, s_irq_ctx[pin]);
    }
}

uint16_t gpio_irq_take_pending(void)
{
    uint16_t p;

    em4_irq_disable();
    p = s_irq_pending;
    s_irq_pending = 0U;
    em4_irq_enable();
    return p;
}

uint32_t gpio_irq_count(uint8_t pin)
{
    if (pin >= GPIO_PIN_COUNT) {
        return 0U;
    }
    return s_irq_count[pin];
}

void gpio_sim_drive(uint8_t pin, bool level)
{
    gpio_pin_t *p;
    bool        rising;
    bool        falling;

    if (pin >= GPIO_PIN_COUNT) {
        return;
    }
    p       = &s_pin[pin];
    rising  = (!p->in_level) && level;
    falling = p->in_level && (!level);
    p->in_level = level;

    if (((p->mode == GPIO_MODE_IRQ_RISING) && rising) ||
        ((p->mode == GPIO_MODE_IRQ_FALLING) && falling) ||
        ((p->mode == GPIO_MODE_IRQ_BOTH) && (rising || falling))) {
        s_irq_pending |= (uint16_t)(1U << pin);
    }
}
