/**
 * @file    stats.hpp
 * @brief   テレメトリーの集計のクラス
 *
 *   RunningStats : 区間の件数・最小・最大・平均・分散（近似）
 *   ChannelStats : 1 チャンネル分（換算、移動平均の窓、警報）
 *   AlarmState   : 上限・下限の警報（ヒステリシスつき）
 */
#ifndef EM4_TELEMETRY_STATS_HPP
#define EM4_TELEMETRY_STATS_HPP

#include <cstddef>
#include <cstdint>
#include <memory>

#include "sample_window.hpp"

namespace em4 {
namespace telem {

class RunningStats {
public:
    RunningStats();

    void     reset();
    void     add(int32_t v);
    uint32_t count() const { return count_; }
    int32_t  min() const { return min_; }
    int32_t  max() const { return max_; }
    int32_t  mean() const;
    uint32_t variance() const;     // 2 乗の単位。件数が 2 未満なら 0

private:
    int64_t  sum_;
    uint64_t sum_sq_;
    int32_t  min_;
    int32_t  max_;
    uint32_t count_;
};

class AlarmState {
public:
    enum class Level : uint8_t { Normal = 0, Low, High };

    AlarmState() : lo_(INT32_MIN), hi_(INT32_MAX), hyst_(0), level_(Level::Normal), events_(0U) {}

    void     configure(int32_t lo, int32_t hi, int32_t hyst);
    /* 状態が変わったら true */
    bool     update(int32_t v);
    Level    level() const { return level_; }
    uint32_t events() const { return events_; }

private:
    int32_t  lo_;
    int32_t  hi_;
    int32_t  hyst_;
    Level    level_;
    uint32_t events_;
};

struct ChannelConfig {
    int32_t  scale_num;     // 値 = raw * scale_num / scale_den + offset
    int32_t  scale_den;
    int32_t  offset;
    int32_t  alarm_lo;
    int32_t  alarm_hi;
    int32_t  alarm_hyst;
    uint32_t window;        // 移動平均の窓の大きさ
};

/*
 * 1 チャンネル分の集計。
 * 作った後に init() を呼ぶまでは無効で、add() しても捨てる。
 */
class ChannelStats {
public:
    explicit ChannelStats(uint8_t id);

    bool init(const ChannelConfig& cfg);
    bool enabled() const { return enabled_; }

    void add(int32_t raw, uint32_t ts_ms);
    void set_alarm(int32_t lo, int32_t hi);
    void reset();

    const RunningStats& stats() const { return stats_; }
    int32_t             last() const { return last_; }
    uint32_t            last_ts() const { return last_ts_; }
    uint32_t            alarms() const { return alarm_.events(); }
    AlarmState::Level   alarm_level() const { return alarm_.level(); }
    int32_t             window_mean() const;
    uint8_t             id() const { return id_; }

    ChannelStats(const ChannelStats&)            = delete;
    ChannelStats& operator=(const ChannelStats&) = delete;

private:
    int32_t scale(int32_t raw) const;

    uint8_t                                 id_;
    bool                                    enabled_;
    ChannelConfig                           cfg_;
    RunningStats                            stats_;
    AlarmState                              alarm_;
    std::unique_ptr<SampleWindow<int32_t>>  window_;
    int32_t                                 last_;
    uint32_t                                last_ts_;
};

}  // namespace telem
}  // namespace em4

#endif  // EM4_TELEMETRY_STATS_HPP
