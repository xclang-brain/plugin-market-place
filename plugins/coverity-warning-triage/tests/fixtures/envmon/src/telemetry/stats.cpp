/**
 * @file    stats.cpp
 * @brief   テレメトリーの集計のクラスの実装
 */
#include "stats.hpp"

#include <climits>
#include <new>

namespace em4 {
namespace telem {

/* ===================================================================== */
/* RunningStats                                                          */
/* ===================================================================== */
RunningStats::RunningStats()
    : sum_(0),
      sum_sq_(0U),
      min_(INT32_MAX),
      count_(0U)
{
}

void RunningStats::reset()
{
    sum_    = 0;
    sum_sq_ = 0U;
    min_    = INT32_MAX;
    max_    = INT32_MIN;
    count_  = 0U;
}

void RunningStats::add(int32_t v)
{
    if (v < min_) {
        min_ = v;
    }
    if (v > max_) {
        max_ = v;
    }
    sum_ += v;
    const int64_t v64 = static_cast<int64_t>(v);
    sum_sq_ += static_cast<uint64_t>(v64 * v64);
    if (count_ < UINT32_MAX) {
        ++count_;
    }
}

int32_t RunningStats::mean() const
{
    if (count_ == 0U) {
        return 0;
    }
    return static_cast<int32_t>(sum_ / static_cast<int64_t>(count_));
}

uint32_t RunningStats::variance() const
{
    if (count_ < 2U) {
        return 0U;
    }
    // E[x^2] - E[x]^2。桁が大きいので 64 ビットで計算する
    const int64_t  m   = sum_ / static_cast<int64_t>(count_);
    const uint64_t ex2 = sum_sq_ / count_;
    const uint64_t m2  = static_cast<uint64_t>(m * m);
    if (ex2 <= m2) {
        return 0U;
    }
    const uint64_t var = ex2 - m2;
    return (var > UINT32_MAX) ? UINT32_MAX : static_cast<uint32_t>(var);
}

/* ===================================================================== */
/* AlarmState                                                            */
/* ===================================================================== */
void AlarmState::configure(int32_t lo, int32_t hi, int32_t hyst)
{
    if (lo > hi) {
        const int32_t t = lo;
        lo = hi;
        hi = t;
    }
    lo_    = lo;
    hi_    = hi;
    hyst_  = (hyst < 0) ? 0 : hyst;
    level_ = Level::Normal;
}

bool AlarmState::update(int32_t v)
{
    Level next = level_;

    switch (level_) {
    case Level::Normal:
        if (v > hi_) {
            next = Level::High;
        } else if (v < lo_) {
            next = Level::Low;
        } else {
            // そのまま
        }
        break;
    case Level::High:
        if (v <= (hi_ - hyst_)) {
            next = Level::Normal;
        }
        break;
    case Level::Low:
        if (v >= (lo_ + hyst_)) {
            next = Level::Normal;
        }
        break;
    default:
        next = Level::Normal;
        break;
    }

    if (next == level_) {
        return false;
    }
    if (next != Level::Normal) {
        ++events_;
    }
    level_ = next;
    return true;
}

/* ===================================================================== */
/* ChannelStats                                                          */
/* ===================================================================== */
ChannelStats::ChannelStats(uint8_t id)
    : id_(id),
      enabled_(false),
      stats_(),
      alarm_(),
      window_(),
      last_(0),
      last_ts_(0U)
{
}

bool ChannelStats::init(const ChannelConfig& cfg)
{
    if ((cfg.scale_den == 0) || (cfg.window == 0U)) {
        return false;
    }
    std::unique_ptr<SampleWindow<int32_t>> w(new (std::nothrow) SampleWindow<int32_t>(cfg.window));
    if (!w || !w->valid()) {
        return false;
    }
    cfg_    = cfg;
    window_ = std::move(w);
    alarm_.configure(cfg.alarm_lo, cfg.alarm_hi, cfg.alarm_hyst);
    enabled_ = true;
    return true;
}

int32_t ChannelStats::scale(int32_t raw) const
{
    const int64_t v = (static_cast<int64_t>(raw) * cfg_.scale_num) / cfg_.scale_den;
    return static_cast<int32_t>(v + cfg_.offset);
}

void ChannelStats::add(int32_t raw, uint32_t ts_ms)
{
    if (!enabled_) {
        return;
    }
    const int32_t v = scale(raw);
    stats_.add(v);
    window_->push(v);
    (void)alarm_.update(v);
    last_    = v;
    last_ts_ = ts_ms;
}

void ChannelStats::set_alarm(int32_t lo, int32_t hi)
{
    cfg_.alarm_lo = lo;
    cfg_.alarm_hi = hi;
    alarm_.configure(lo, hi, cfg_.alarm_hyst);
}

void ChannelStats::reset()
{
    stats_.reset();
    if (window_) {
        window_->clear();
    }
    last_    = 0;
    last_ts_ = 0U;
}

int32_t ChannelStats::window_mean() const
{
    if (!window_) {
        return 0;
    }
    return window_->mean();
}

}  // namespace telem
}  // namespace em4
