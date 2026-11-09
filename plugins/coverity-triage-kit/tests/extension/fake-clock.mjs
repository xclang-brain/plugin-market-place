// A fake clock with the shape of core/clock.mjs, plus advance(ms) and flush().

export function createFakeClock(start = Date.UTC(2026, 10, 8, 1, 0, 0)) {
  let now = start;
  let seq = 0;
  const timers = new Map();

  const clock = {
    now: () => now,
    setTimeout(fn, ms) {
      seq += 1;
      timers.set(seq, { at: now + Math.max(0, ms), fn, interval: null });
      return seq;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    setInterval(fn, ms) {
      seq += 1;
      timers.set(seq, { at: now + ms, fn, interval: ms });
      return seq;
    },
    clearInterval(id) {
      timers.delete(id);
    },
    async flush() {
      for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
    },
    // moves time forward; due timers run in order, with promises settled after each
    async advance(ms) {
      const target = now + ms;
      await clock.flush();
      for (;;) {
        let nextId = null;
        let nextAt = Infinity;
        for (const [id, t] of timers) {
          if (t.at <= target && t.at < nextAt) {
            nextAt = t.at;
            nextId = id;
          }
        }
        if (nextId === null) break;
        const t = timers.get(nextId);
        now = Math.max(now, t.at);
        if (t.interval !== null) t.at += t.interval;
        else timers.delete(nextId);
        t.fn();
        await clock.flush();
      }
      now = target;
      await clock.flush();
    },
    get pendingTimers() {
      return timers.size;
    },
  };
  return clock;
}
