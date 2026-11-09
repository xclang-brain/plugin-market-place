import { randomBytes } from 'node:crypto';

// Wraps a call with a time limit.
// Result: { status: 'ok', value } | { status: 'error', error } | { status: 'timeout' } | { status: 'stopped' }
// stopSignal is a Promise that resolves when the user pressed stop (used while starting).
export function callWithTimeout(clock, fn, ms, stopSignal = null) {
  return new Promise((resolve) => {
    if (!(ms > 0)) {
      resolve({ status: 'timeout' });
      return;
    }
    let done = false;
    let timer = null;
    const finish = (result) => {
      if (done) return;
      done = true;
      if (timer !== null) clock.clearTimeout(timer);
      resolve(result);
    };
    timer = clock.setTimeout(() => finish({ status: 'timeout' }), ms);
    if (stopSignal) stopSignal.then(() => finish({ status: 'stopped' }));
    let p;
    try {
      p = Promise.resolve(fn());
    } catch (error) {
      finish({ status: 'error', error });
      return;
    }
    p.then(
      (value) => finish({ status: 'ok', value }),
      (error) => finish({ status: 'error', error }),
    );
  });
}

export function sleep(clock, ms) {
  return new Promise((resolve) => {
    clock.setTimeout(resolve, Math.max(0, ms));
  });
}

export function errorMessage(e) {
  if (e == null) return '';
  if (typeof e === 'string') return e;
  return String(e.message ?? e);
}

export const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
export const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
export const strings = (list) => (Array.isArray(list) ? list.filter((v) => typeof v === 'string' && v !== '') : []);

// Collapses white space and cuts long text
export function summarizeText(value, maxChars) {
  const flat = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (flat.length <= maxChars) return flat;
  return `${flat.slice(0, maxChars)}…`;
}

// Reads JSON Lines; skips unreadable lines (a half-written last line)
export function parseJsonLines(content) {
  const out = [];
  for (const line of String(content ?? '').split('\n')) {
    const s = line.trim();
    if (!s) continue;
    try {
      out.push(JSON.parse(s));
    } catch {
      // half-written line
    }
  }
  return out;
}

// Record id; also the folder name. Sorts oldest first.
export function newRecordId(nowMs) {
  const d = new Date(nowMs);
  const p = (n, w = 2) => String(n).padStart(w, '0');
  const stamp = `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}-${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
  return `${stamp}-${randomBytes(3).toString('hex')}`;
}

export function newAttemptId() {
  return randomBytes(6).toString('hex');
}

// Key-order independent JSON text (to compare tool arguments)
export function stableStringify(v) {
  if (v === null || typeof v !== 'object') return JSON.stringify(v) ?? 'undefined';
  if (Array.isArray(v)) return `[${v.map(stableStringify).join(',')}]`;
  return `{${Object.keys(v)
    .sort()
    .map((k) => `${JSON.stringify(k)}:${stableStringify(v[k])}`)
    .join(',')}}`;
}

export function clone(v) {
  return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
}

// Tool arguments arrive as an object, sometimes as JSON text
export function argsOf(value) {
  let v = value;
  if (typeof v === 'string') {
    try {
      v = JSON.parse(v);
    } catch {
      return {};
    }
  }
  return isPlainObject(v) ? v : {};
}
