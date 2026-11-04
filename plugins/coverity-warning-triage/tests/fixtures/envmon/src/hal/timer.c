/**
 * @file    timer.c
 * @brief   SysTick、ソフトウェアタイマー、ウォッチドッグ、リセット
 *
 * ホストでは時刻を 2 通りで進められる。
 *   - 模擬時刻（既定）: timer_sim_advance() / timer_delay_ms() で進める。試験の結果が毎回同じになる。
 *   - 実時刻: timer_set_realtime(true) のとき。CLOCK_MONOTONIC を使う。
 */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#include "hal.h"

typedef struct {
    bool       active;
    uint32_t   period_ms;
    uint32_t   next_ms;
    timer_cb_t cb;
    void      *ctx;
} sw_timer_t;

static volatile uint32_t s_now_ms;
static volatile uint32_t s_tick_count;
static bool              s_realtime;
static uint32_t          s_rt_base_ms;
static sw_timer_t        s_sw[TIMER_SW_MAX];

static volatile uint32_t s_irq_nest;

static uint32_t          s_wdt_timeout_ms;
static uint32_t          s_wdt_last_kick_ms;
static bool              s_wdt_enabled;
static bool              s_wdt_fired;

static void            (*s_reset_hook)(void);

/* ===================================================================== */
/* 割り込みの禁止・許可                                                  */
/* ===================================================================== */
void em4_irq_disable(void)
{
    s_irq_nest++;
}

void em4_irq_enable(void)
{
    if (s_irq_nest > 0U) {
        s_irq_nest--;
    }
}

/* ===================================================================== */
/* 時刻                                                                  */
/* ===================================================================== */
static uint32_t host_monotonic_ms(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0U;
    }
    return (uint32_t)(((uint64_t)ts.tv_sec * 1000ULL) + ((uint64_t)ts.tv_nsec / 1000000ULL));
}

void timer_init(void)
{
    size_t i;

    s_now_ms     = 0U;
    s_tick_count = 0U;
    s_irq_nest   = 0U;
    for (i = 0U; i < TIMER_SW_MAX; i++) {
        s_sw[i].active = false;
    }
    s_rt_base_ms = host_monotonic_ms();
}

void timer_set_realtime(bool on)
{
    s_realtime   = on;
    s_rt_base_ms = host_monotonic_ms() - s_now_ms;
}

uint32_t timer_now_ms(void)
{
    if (s_realtime) {
        return host_monotonic_ms() - s_rt_base_ms;
    }
    return s_now_ms;
}

static void timer_service_sw(uint32_t now)
{
    size_t i;

    for (i = 0U; i < TIMER_SW_MAX; i++) {
        sw_timer_t *t = &s_sw[i];
        if (t->active && ((int32_t)(now - t->next_ms) >= 0)) {
            t->next_ms += t->period_ms;
            if (t->cb != NULL) {
                t->cb(t->ctx);
            }
        }
    }
}

static void timer_check_wdt(uint32_t now)
{
    if (s_wdt_enabled && !s_wdt_fired) {
        if ((now - s_wdt_last_kick_ms) > s_wdt_timeout_ms) {
            s_wdt_fired = true;
            (void)fprintf(stderr, "[wdt] watchdog expired at %lu ms\n", (unsigned long)now);
        }
    }
}

/*
 * SysTick の割り込み（TIMER_TICK_MS ごと）。
 * ソフトウェアタイマー、GPIO の割り込み、ウォッチドッグをここで処理する。
 */
void timer_tick_isr(void)
{
    uint16_t pending;
    uint8_t  pin;
    uint32_t now;

    if (!s_realtime) {
        s_now_ms += TIMER_TICK_MS;
    }
    s_tick_count++;
    now = timer_now_ms();

    timer_service_sw(now);

    pending = gpio_irq_take_pending();
    for (pin = 0U; pin < GPIO_PIN_COUNT; pin++) {
        if ((pending & (uint16_t)(1U << pin)) != 0U) {
            gpio_irq_dispatch(pin);
        }
    }

    timer_check_wdt(now);
}

void timer_sim_advance(uint32_t ms)
{
    uint32_t done = 0U;

    while (done < ms) {
        timer_tick_isr();
        sim_env_step(TIMER_TICK_MS);
        done += TIMER_TICK_MS;
    }
}

void timer_delay_ms(uint32_t ms)
{
    if (s_realtime) {
        struct timespec req;
        req.tv_sec  = (time_t)(ms / 1000U);
        req.tv_nsec = (long)(ms % 1000U) * 1000000L;
        (void)nanosleep(&req, NULL);
    } else {
        /* 模擬時刻では、待つ代わりに時刻を進める（端数は切り上げ） */
        timer_sim_advance(((ms + TIMER_TICK_MS) - 1U) / TIMER_TICK_MS * TIMER_TICK_MS);
    }
}

em4_err_t timer_sw_start(uint8_t id, uint32_t period_ms, timer_cb_t cb, void *ctx)
{
    if ((id >= TIMER_SW_MAX) || (period_ms == 0U) || (cb == NULL)) {
        return EM4_ERR_PARAM;
    }
    em4_irq_disable();
    s_sw[id].period_ms = period_ms;
    s_sw[id].next_ms   = timer_now_ms() + period_ms;
    s_sw[id].cb        = cb;
    s_sw[id].ctx       = ctx;
    s_sw[id].active    = true;
    em4_irq_enable();
    return EM4_OK;
}

em4_err_t timer_sw_stop(uint8_t id)
{
    if (id >= TIMER_SW_MAX) {
        return EM4_ERR_PARAM;
    }
    s_sw[id].active = false;
    return EM4_OK;
}

/* ===================================================================== */
/* ウォッチドッグ                                                        */
/* ===================================================================== */
void wdt_init(uint32_t timeout_ms)
{
    s_wdt_timeout_ms   = timeout_ms;
    s_wdt_last_kick_ms = timer_now_ms();
    s_wdt_enabled      = (timeout_ms != 0U);
    s_wdt_fired        = false;
}

void wdt_kick(void)
{
    s_wdt_last_kick_ms = timer_now_ms();
}

bool wdt_expired(void)
{
    return s_wdt_fired;
}

/* ===================================================================== */
/* リセット                                                              */
/* ===================================================================== */
void platform_set_reset_hook(void (*hook)(void))
{
    s_reset_hook = hook;
}

/*
 * 実機では NVIC_SystemReset() を呼ぶ。ホストでは後片付けをしてからプロセスを終える。
 * どちらの場合も呼び出し元には戻らない。
 */
void platform_reset(void)
{
    if (s_reset_hook != NULL) {
        s_reset_hook();
    }
    (void)fflush(NULL);
    exit(3);
}
