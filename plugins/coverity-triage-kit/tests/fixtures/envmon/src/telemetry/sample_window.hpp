/**
 * @file    sample_window.hpp
 * @brief   直近 N 個のサンプルを持つ窓（移動平均用）
 *
 * - 領域は作るときに一度だけ確保する（new (std::nothrow)）。確保に失敗したら
 *   capacity() が 0 になり、push しても何もしない。valid() で確かめること。
 * - コピーはできない（領域を二重に解放しないため）。
 */
#ifndef EM4_TELEMETRY_SAMPLE_WINDOW_HPP
#define EM4_TELEMETRY_SAMPLE_WINDOW_HPP

#include <cstddef>
#include <cstdint>
#include <new>

namespace em4 {
namespace telem {

template <typename T>
class SampleWindow {
public:
    explicit SampleWindow(std::size_t capacity)
        : buf_(new (std::nothrow) T[capacity]),
          cap_((buf_ != nullptr) ? capacity : 0U),
          head_(0U),
          size_(0U),
          sum_(0)
    {
    }

    ~SampleWindow()
    {
        delete buf_;
    }

    SampleWindow(const SampleWindow&)            = delete;
    SampleWindow& operator=(const SampleWindow&) = delete;

    bool        valid() const { return buf_ != nullptr; }
    std::size_t size() const { return size_; }
    std::size_t capacity() const { return cap_; }
    bool        full() const { return (cap_ != 0U) && (size_ == cap_); }

    void push(T v)
    {
        if (cap_ == 0U) {
            return;
        }
        if (size_ == cap_) {
            sum_ -= static_cast<int64_t>(buf_[head_]);     // 一番古いものを外す
        } else {
            ++size_;
        }
        buf_[head_] = v;
        sum_ += static_cast<int64_t>(v);
        head_ = (head_ + 1U) % cap_;
    }

    void clear()
    {
        head_ = 0U;
        size_ = 0U;
        sum_  = 0;
    }

    /* 平均。空なら T() */
    T mean() const
    {
        if (size_ == 0U) {
            return T();
        }
        return static_cast<T>(sum_ / static_cast<int64_t>(size_));
    }

    /* i = 0 が一番新しい */
    T at_newest(std::size_t i) const
    {
        if (i >= size_) {
            return T();
        }
        const std::size_t idx = (head_ + cap_ - 1U - i) % cap_;
        return buf_[idx];
    }

    template <typename F>
    void for_each_oldest_first(F fn) const
    {
        const std::size_t start = (head_ + cap_ - size_) % ((cap_ == 0U) ? 1U : cap_);
        for (std::size_t i = 0U; i < size_; ++i) {
            fn(buf_[(start + i) % cap_]);
        }
    }

private:
    T*          buf_;
    std::size_t cap_;
    std::size_t head_;
    std::size_t size_;
    int64_t     sum_;
};

}  // namespace telem
}  // namespace em4

#endif  // EM4_TELEMETRY_SAMPLE_WINDOW_HPP
