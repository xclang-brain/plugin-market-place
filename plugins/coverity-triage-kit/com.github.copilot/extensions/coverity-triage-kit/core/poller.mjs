import { LIMITS } from './constants.mjs';
import { STATUS_TIMEOUT, statusError } from './messages.mjs';
import { ensureWorkerSlots } from './run.mjs';

// Reads `status` of the run (design 8.1 step 3).
// - Single flight: a new call waits for the one in progress instead of starting another.
// - Every 2 s while the state changes and the screen is viewed; 5 s otherwise.
// - The runner kills the process tree after 10 s.

// Fields that change on every call (elapsed time) are left out of the change check
function signatureOf(st) {
  const items = (Array.isArray(st.items) ? st.items : []).map((i) => [i?.id, i?.status, i?.phase, i?.note, i?.attempts, i?.rework_rounds, i?.stalled, i?.preparing_copy, i?.review?.outcome]);
  return JSON.stringify([st.stage, st.finished, st.stopped, st.has_summary, st.counts, items, st.running_tasks, st.errors, st.recent, st.initial, st.progress, st.run_checks, st.issues_found]);
}

export function fetchStatus(ctx, run) {
  if (!run.runDir) return Promise.resolve({ ok: false, changed: false });
  if (run.stInFlight) return run.stInFlight;
  run.stInFlight = (async () => {
    const r = await ctx.commands.status(run.runDir);
    let changed = false;
    if (r.ok) {
      const sig = signatureOf(r.data);
      changed = sig !== run.stSignature || run.stError !== null;
      run.st = r.data;
      run.stSignature = sig;
      run.stError = null;
      run.stAt = ctx.clock.now();
      ensureWorkerSlots(run, r.data.parallel);
    } else {
      const message = r.kind === 'timeout' ? STATUS_TIMEOUT : statusError(r.error);
      changed = run.stError !== message;
      run.stError = message;
    }
    ctx.bump(); // elapsed times move even when nothing else changed
    return { ok: r.ok, changed };
  })().finally(() => {
    run.stInFlight = null;
  });
  return run.stInFlight;
}

const polling = (run) => run.status === 'starting' || run.status === 'running';

export function startPolling(ctx, run) {
  if (run.pollTimer) return;
  const schedule = (ms) => {
    run.pollTimer = ctx.clock.setTimeout(tick, ms);
  };
  async function tick() {
    run.pollTimer = null;
    if (ctx.closed || ctx.run !== run || !polling(run)) return;
    let result = { changed: false };
    try {
      result = await fetchStatus(ctx, run);
    } catch {
      // the next call tries again
    }
    if (ctx.closed || ctx.run !== run || !polling(run)) return;
    const viewed = ctx.clock.now() - (ctx.lastViewAt ?? -Infinity) <= LIMITS.VIEW_FRESH_MS;
    schedule(result.changed && viewed ? LIMITS.STATUS_FAST_MS : LIMITS.STATUS_SLOW_MS);
  }
  schedule(0);
}

export function stopPolling(ctx, run) {
  if (run.pollTimer) ctx.clock.clearTimeout(run.pollTimer);
  run.pollTimer = null;
}
