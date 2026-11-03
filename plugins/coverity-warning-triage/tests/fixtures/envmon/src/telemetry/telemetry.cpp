/**
 * @file    telemetry.cpp
 * @brief   テレメトリーの集計（C から呼ぶ口と、チャンネルをまとめる TelemetryHub）
 *
 * - 例外は使わない。確保は new (std::nothrow) で行い、失敗は戻り値で返す。
 * - 確保は telemetry_init() のときだけ（em4_config.h の設計メモ）。
 * - センサーのタスクと保守コマンドの両方から呼ばれるので、触るときは割り込みを止める。
 */
#include <cstring>
#include <memory>
#include <new>
#include <vector>

#include "telemetry.h"
#include "stats.hpp"

namespace em4 {
namespace telem {

/* 割り込みを止めている間だけ生きる（RAII） */
class ScopedIrqLock {
public:
    ScopedIrqLock() { em4_irq_disable(); }
    ~ScopedIrqLock() { em4_irq_enable(); }
    ScopedIrqLock(const ScopedIrqLock&)            = delete;
    ScopedIrqLock& operator=(const ScopedIrqLock&) = delete;
};

/* チャンネルごとの既定の設定 */
struct ChannelDef {
    uint8_t       id;
    const char*   name;
    ChannelConfig cfg;
};

static const ChannelDef k_channel_defs[TELEM_CH_COUNT] = {
    //                         num  den  off   lo      hi      hyst  window
    { TELEM_CH_TEMP,  "temp",  { 1,   1,   0,   -4000,  8500,   50,   0U } },
    { TELEM_CH_HUM,   "hum",   { 1,   1,   0,   0,      10000,  100,  0U } },
    { TELEM_CH_PRESS, "press", { 1,   1,   0,   30000,  110000, 100,  0U } },
};

class TelemetryHub {
public:
    TelemetryHub() : channels_(), packs_(0U) {}

    bool configure(uint32_t window)
    {
        channels_.reserve(TELEM_CH_COUNT);
        for (const ChannelDef& d : k_channel_defs) {
            std::unique_ptr<ChannelStats> ch(new (std::nothrow) ChannelStats(d.id));
            if (!ch) {
                return false;
            }
            ChannelConfig cfg = d.cfg;
            cfg.window = window;
            if (!ch->init(cfg)) {
                return false;
            }
            channels_.push_back(std::move(ch));
        }
        return true;
    }

    /* id が範囲の外なら nullptr */
    ChannelStats* channel(uint8_t id)
    {
        if (id >= channels_.size()) {
            return nullptr;
        }
        return channels_[id].get();
    }

    std::size_t channel_count() const { return channels_.size(); }

    void reset_all()
    {
        for (std::unique_ptr<ChannelStats>& ch : channels_) {
            ch->reset();
        }
    }

    /*
     * チャンネルごとに 14 バイト: mean(4) min(4) max(4) count(2)
     * 入りきらないチャンネルは送らない（受け側は長さから数を知る）。
     */
    std::size_t pack(uint8_t* buf, std::size_t max)
    {
        static const std::size_t kPerChannel = 14U;
        std::size_t pos = 0U;

        for (std::unique_ptr<ChannelStats>& ch : channels_) {
            if ((pos + kPerChannel) > max) {
                break;
            }
            const RunningStats& s = ch->stats();
            const uint32_t      n = s.count();
            put_i32(&buf[pos], ch->window_mean());
            put_i32(&buf[pos + 4U], (n > 0U) ? s.min() : 0);
            put_i32(&buf[pos + 8U], (n > 0U) ? s.max() : 0);
            put_u16(&buf[pos + 12U], (n > 0xFFFFU) ? static_cast<uint16_t>(0xFFFFU)
                                                   : static_cast<uint16_t>(n));
            pos += kPerChannel;
        }
        ++packs_;
        return pos;
    }

private:
    static void put_u16(uint8_t* p, uint16_t v)
    {
        p[0] = static_cast<uint8_t>(v >> 8);
        p[1] = static_cast<uint8_t>(v & 0xFFU);
    }

    static void put_i32(uint8_t* p, int32_t v)
    {
        const uint32_t u = static_cast<uint32_t>(v);
        put_u16(p, static_cast<uint16_t>(u >> 16));
        put_u16(p + 2, static_cast<uint16_t>(u & 0xFFFFU));
    }

    std::vector<std::unique_ptr<ChannelStats>> channels_;
    uint32_t                                   packs_;
};

static TelemetryHub* g_hub = nullptr;

}  // namespace telem
}  // namespace em4

using em4::telem::ChannelStats;
using em4::telem::RunningStats;
using em4::telem::ScopedIrqLock;
using em4::telem::TelemetryHub;
using em4::telem::g_hub;

extern "C" {

em4_err_t telemetry_init(uint32_t window)
{
    if (g_hub != nullptr) {
        return EM4_ERR_STATE;
    }
    if ((window == 0U) || (window > 600U)) {
        return EM4_ERR_PARAM;
    }
    TelemetryHub* hub = new (std::nothrow) TelemetryHub();
    if (hub == nullptr) {
        return EM4_ERR_NOMEM;
    }
    if (!hub->configure(window)) {
        delete hub;
        return EM4_ERR_NOMEM;
    }
    ScopedIrqLock lock;
    g_hub = hub;
    return EM4_OK;
}

void telemetry_shutdown(void)
{
    TelemetryHub* hub;
    {
        ScopedIrqLock lock;
        hub   = g_hub;
        g_hub = nullptr;
    }
    delete hub;
}

em4_err_t telemetry_add(uint8_t ch, int32_t value, uint32_t ts_ms)
{
    ScopedIrqLock lock;

    if (g_hub == nullptr) {
        return EM4_ERR_STATE;
    }
    ChannelStats* c = g_hub->channel(ch);
    if (c == nullptr) {
        return EM4_ERR_PARAM;
    }
    c->add(value, ts_ms);
    return EM4_OK;
}

int32_t telemetry_mean(uint8_t ch)
{
    ScopedIrqLock lock;

    if (g_hub == nullptr) {
        return 0;
    }
    return g_hub->channel(ch)->window_mean();
}

em4_err_t telemetry_get(uint8_t ch, telem_summary_t* out)
{
    if (out == nullptr) {
        return EM4_ERR_PARAM;
    }
    ScopedIrqLock lock;
    if (g_hub == nullptr) {
        return EM4_ERR_STATE;
    }
    const ChannelStats* c = g_hub->channel(ch);
    if (c == nullptr) {
        return EM4_ERR_PARAM;
    }
    const RunningStats& s = c->stats();
    std::memset(out, 0, sizeof(*out));
    out->count      = s.count();
    out->min        = (s.count() > 0U) ? s.min() : 0;
    out->max        = (s.count() > 0U) ? s.max() : 0;
    out->mean       = s.mean();
    out->last       = c->last();
    out->last_ts_ms = c->last_ts();
    out->alarms     = c->alarms();
    return EM4_OK;
}

em4_err_t telemetry_set_alarm(uint8_t ch, int32_t lo, int32_t hi)
{
    ScopedIrqLock lock;

    if (g_hub == nullptr) {
        return EM4_ERR_STATE;
    }
    ChannelStats* c = g_hub->channel(ch);
    if (c == nullptr) {
        return EM4_ERR_PARAM;
    }
    c->set_alarm(lo, hi);
    return EM4_OK;
}

size_t telemetry_pack(uint8_t* buf, size_t max)
{
    if (buf == nullptr) {
        return 0U;
    }
    ScopedIrqLock lock;
    if (g_hub == nullptr) {
        return 0U;
    }
    return g_hub->pack(buf, max);
}

void telemetry_reset(void)
{
    ScopedIrqLock lock;

    if (g_hub != nullptr) {
        g_hub->reset_all();
    }
}

}  // extern "C"
