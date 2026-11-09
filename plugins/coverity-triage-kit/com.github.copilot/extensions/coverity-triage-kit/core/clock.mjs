// Clock port. Tests swap in a fake clock and advance time.
// Shape: { now(), setTimeout(fn, ms), clearTimeout(h), setInterval(fn, ms), clearInterval(h) }

export const realClock = Object.freeze({
  now: () => Date.now(),
  setTimeout: (fn, ms) => {
    const h = setTimeout(fn, ms);
    h.unref?.(); // timers alone must not keep the process alive
    return h;
  },
  clearTimeout: (h) => clearTimeout(h),
  setInterval: (fn, ms) => {
    const h = setInterval(fn, ms);
    h.unref?.();
    return h;
  },
  clearInterval: (h) => clearInterval(h),
});
