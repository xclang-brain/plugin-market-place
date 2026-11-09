/**
 * @file    scheduler.c
 * @brief   周期処理（協調型。割り込みからは呼ばない）
 *
 * タスクは起動時に main.c が sched_register() で登録する。実行中に増やしたり
 * 消したりはしない。周期は保守コマンド SET RATE で変えられる。
 *
 * 時刻の比較は (int32_t)(now - next) >= 0 で行う。uint32_t の ms は約 49.7 日で
 * 一周するが、この書き方なら一周しても正しく比べられる。
 */
#include <string.h>

#include "app.h"
#include "hal.h"

typedef struct {
    const char *name;
    sched_fn_t  fn;
    void       *ctx;
    uint32_t    period_ms;
    uint32_t    next_ms;
    uint32_t    last_ms;
    uint32_t    runs;
    uint32_t    errors;
    uint32_t    overruns;
    em4_err_t   last_rc;
    bool        in_use;
} sched_task_t;

static sched_task_t s_tasks[SCHED_MAX_TASKS];
static uint8_t      s_count;

static const char *const TAG = "sched";

void sched_init(void)
{
    (void)memset(s_tasks, 0, sizeof(s_tasks));
    s_count = 0U;
}

/* 空いている枠の番号。空きがなければ -1 */
static int find_free_slot(void)
{
    int i;

    for (i = 0; i < (int)SCHED_MAX_TASKS; i++) {
        if (!s_tasks[i].in_use) {
            return i;
        }
    }
    return -1;
}

em4_err_t sched_register(const sched_def_t *def, uint8_t *out_id)
{
    int slot;

    if ((def == NULL) || (def->fn == NULL) || (def->name == NULL) || (def->period_ms == 0U)) {
        return EM4_ERR_PARAM;
    }

    slot = find_free_slot();
    s_tasks[slot].name      = def->name;
    s_tasks[slot].fn        = def->fn;
    s_tasks[slot].ctx       = def->ctx;
    s_tasks[slot].period_ms = def->period_ms;
    s_tasks[slot].next_ms   = timer_now_ms() + def->period_ms;
    s_tasks[slot].runs      = 0U;
    s_tasks[slot].errors    = 0U;
    s_tasks[slot].overruns  = 0U;
    s_tasks[slot].last_rc   = EM4_OK;
    s_tasks[slot].in_use    = true;
    s_count++;

    if (out_id != NULL) {
        *out_id = (uint8_t)slot;
    }
    LOG_D(TAG, "task '%s' registered in slot %d (%lu ms)", def->name, slot,
          (unsigned long)def->period_ms);
    return EM4_OK;
}

em4_err_t sched_set_period(uint8_t id, uint32_t period_ms)
{
    if ((id >= SCHED_MAX_TASKS) || !s_tasks[id].in_use) {
        return EM4_ERR_NOTFOUND;
    }
    if (period_ms == 0U) {
        return EM4_ERR_PARAM;
    }
    em4_irq_disable();
    s_tasks[id].period_ms = period_ms;
    s_tasks[id].next_ms   = timer_now_ms() + period_ms;
    em4_irq_enable();
    LOG_I(TAG, "task '%s' period -> %lu ms", s_tasks[id].name, (unsigned long)period_ms);
    return EM4_OK;
}

int sched_find(const char *name)
{
    int i;

    if (name == NULL) {
        return -1;
    }
    for (i = 0; i < (int)SCHED_MAX_TASKS; i++) {
        if (s_tasks[i].in_use && (strcmp(s_tasks[i].name, name) == 0)) {
            return i;
        }
    }
    return -1;
}

em4_err_t sched_get_info(uint8_t id, sched_info_t *out)
{
    if (out == NULL) {
        return EM4_ERR_PARAM;
    }
    if ((id >= SCHED_MAX_TASKS) || !s_tasks[id].in_use) {
        return EM4_ERR_NOTFOUND;
    }
    out->name      = s_tasks[id].name;
    out->period_ms = s_tasks[id].period_ms;
    out->runs      = s_tasks[id].runs;
    out->errors    = s_tasks[id].errors;
    out->overruns  = s_tasks[id].overruns;
    out->last_ms   = s_tasks[id].last_ms;
    return EM4_OK;
}

uint8_t sched_count(void)
{
    return s_count;
}

/*
 * 時刻が来たタスクを動かす。動かした数を返す。
 * 1 周期以上遅れたタスクは、遅れた分をまとめて取り戻さずに次の周期へ合わせる。
 */
uint32_t sched_run_due(uint32_t now_ms)
{
    uint32_t ran = 0U;
    size_t   i;

    for (i = 0U; i < SCHED_MAX_TASKS; i++) {
        sched_task_t *t = &s_tasks[i];
        em4_err_t     rc;

        if (!t->in_use) {
            continue;
        }
        if ((int32_t)(now_ms - t->next_ms) < 0) {
            continue;
        }

        rc = t->fn(t->ctx);
        t->runs++;
        t->last_ms = now_ms;
        if (rc != EM4_OK) {
            t->errors++;
            if (rc != t->last_rc) {
                LOG_W(TAG, "task '%s' failed: %s", t->name, em4_strerror(rc));
            }
        }
        t->last_rc = rc;

        t->next_ms += t->period_ms;
        if ((int32_t)(now_ms - t->next_ms) >= 0) {
            t->overruns++;
            t->next_ms = now_ms + t->period_ms;
        }
        ran++;
    }
    return ran;
}
