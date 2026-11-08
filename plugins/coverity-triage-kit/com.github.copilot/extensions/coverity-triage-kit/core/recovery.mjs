import { analyzeEntries, numberIssues } from './analysis.mjs';
import { LIMITS } from './constants.mjs';
import { UNSTOPPED_HOW_TO } from './messages.mjs';
import { callWithTimeout, sleep } from './util.mjs';

// When the extension starts (session start, reload after /clear or extensions_reload):
//   - end unfinished runs (classifyUnfinishedRun decides how)
//   - rebuild the deny list from this session's runs
//   - purge old records
//   - find the previous run for the setup screen

// The one place that decides what to do with an unfinished run found at startup.
//   'cancel_and_end' this session's run (only the extension restarted; the agents may still run): cancel them, end it
//   'cut_off'        another session's run whose alive mark is old: end it now
//   'check_later'    another session's run whose alive mark is fresh: look again after the stale time
export function classifyUnfinishedRun({ begin, aliveAt, now, sessionId }) {
  if (begin && begin.sessionId === sessionId) return 'cancel_and_end';
  if (aliveAt === null || aliveAt === undefined || now - aliveAt > LIMITS.ALIVE_STALE_MS) return 'cut_off';
  return 'check_later';
}

function lastTimestamp(entries, fallback) {
  for (let i = entries.length - 1; i >= 0; i -= 1) if (Number.isFinite(entries[i].at)) return entries[i].at;
  return fallback;
}

function treeIdsOf(entries) {
  const ids = [];
  for (const entry of entries) {
    if (entry.t === 'tree' && Array.isArray(entry.agentIds)) for (const id of entry.agentIds) if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

export function finishRecoveredRun(ctx, runId, entries, begin, { detail, unstopped = [] }) {
  const now = ctx.clock.now();
  const endReason = { kind: 'aborted', detail, notes: unstopped.length ? [`止めきれなかったエージェント ${unstopped.length} 体`] : [], unstoppable: unstopped.length > 0 };
  const drafts = analyzeEntries(entries, {
    runId,
    endReason,
    endAt: lastTimestamp(entries, now),
    unstopped,
    isAuto: begin?.isAuto ?? false,
    models: begin?.settings?.models ?? {},
    restarts: entries.filter((e) => e.t === 'note' && e.kind === 'restart').length,
  });
  const runDir = [...entries].reverse().find((e) => e.t === 'note' && e.kind === 'new_run')?.runDir ?? null;
  const result = {
    runId,
    id: runId,
    sessionId: begin?.sessionId ?? null,
    cwd: begin?.cwd ?? null,
    startedAt: begin?.at ?? null,
    finishedAt: now,
    status: 'finished',
    endReason,
    settings: begin?.settings ?? null,
    runDir,
    agents: [],
    pastAgents: [],
    issues: numberIssues(drafts, 'M', 'mechanical'),
    unstopped,
    unstoppedJobs: [],
    waits: [],
    recovered: true,
  };
  try {
    ctx.store.append(runId, { t: 'status', at: now, status: 'finished', endReason });
  } catch {
    // the result file is still written
  }
  ctx.store.writeResult(runId, result);
  // tell about it only for runs the setup screen shows (same session, else same folder)
  if (begin && (begin.sessionId === ctx.sessionId || begin.cwd === ctx.cwd)) ctx.endedAtStartup.push(runId);
  ctx.previousDirty = true;
}

export function rebuildDenyList(ctx) {
  for (const runId of ctx.store.listRuns()) {
    const entries = ctx.store.readLog(runId);
    const begin = entries.find((e) => e.t === 'begin');
    if (!begin || begin.sessionId !== ctx.sessionId) continue;
    for (const id of treeIdsOf(entries)) ctx.deny.add(id);
  }
}

function examine(ctx, runId) {
  const entries = ctx.store.readLog(runId);
  const begin = entries.find((e) => e.t === 'begin') ?? null;
  const action = classifyUnfinishedRun({ begin, aliveAt: ctx.store.readAlive(runId), now: ctx.clock.now(), sessionId: ctx.sessionId });
  return { entries, begin, action };
}

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

// This session's unfinished run (the extension restarted): cancel the tree's agents that still run, then end it
async function endRestartedRun(ctx, runId, entries, begin) {
  const { clock, port } = ctx;
  const ids = new Set(treeIdsOf(entries));
  const deadline = clock.now() + LIMITS.STOP_DEADLINE_MS;
  const left = () => deadline - clock.now();
  const call = (fn) => callWithTimeout(clock, fn, Math.min(LIMITS.CALL_TIMEOUT_MS, left()));
  let tasks = [];
  const refresh = async () => {
    const r = await call(() => port.listTasks());
    if (r.status !== 'ok' || !Array.isArray(r.value)) return false;
    tasks = r.value
      .filter((t) => t && (!t.type || t.type === 'agent') && (t.agentId ?? t.id) != null)
      .map((t) => ({ agentId: t.agentId ?? t.id, status: t.status, parentAgentId: t.parentAgentId ?? null }));
    for (const t of tasks) {
      if (t.parentAgentId && ids.has(t.parentAgentId) && !ids.has(t.agentId)) {
        ids.add(t.agentId);
        ctx.deny.add(t.agentId);
      }
    }
    return true;
  };
  const activeNow = () => tasks.filter((t) => ids.has(t.agentId) && !TERMINAL.has(t.status)).map((t) => t.agentId);

  const listed = await refresh();
  const active = listed ? activeNow() : [];
  if (active.length === 0) {
    finishRecoveredRun(ctx, runId, entries, begin, { detail: 'cut_off' });
    return;
  }
  const parentOf = new Map(tasks.map((t) => [t.agentId, t.parentAgentId]));
  const ordered = [...active].sort((a, b) => Number(active.includes(parentOf.get(a))) - Number(active.includes(parentOf.get(b))));
  for (const id of ordered) {
    const c = await call(() => port.cancelAgent(id));
    try {
      ctx.store.append(runId, { t: 'note', at: clock.now(), kind: 'restart_cancel', agentId: id, status: c.status });
    } catch {
      // go on without the note
    }
  }
  let unstopped = [];
  let knownActive = ordered;
  for (;;) {
    const ok = await refresh();
    if (ok) {
      knownActive = activeNow();
      if (knownActive.length === 0) break;
    }
    if (left() <= 0) {
      unstopped = knownActive.map((agentId) => ({ agentId, role: 'other', howTo: UNSTOPPED_HOW_TO }));
      break;
    }
    await sleep(clock, Math.min(LIMITS.STOP_POLL_MS, Math.max(left(), 0)));
  }
  finishRecoveredRun(ctx, runId, entries, begin, { detail: 'extension_restarted', unstopped });
}

export function recoverOnStartup(ctx) {
  try {
    ctx.store.ensureRoot();
  } catch {
    return;
  }
  const later = [];
  const own = [];
  for (const runId of ctx.store.listRuns()) {
    if (ctx.store.hasResult(runId)) continue;
    try {
      const { entries, begin, action } = examine(ctx, runId);
      if (action === 'cut_off') finishRecoveredRun(ctx, runId, entries, begin, { detail: 'cut_off' });
      else if (action === 'cancel_and_end') {
        for (const id of treeIdsOf(entries)) ctx.deny.add(id);
        own.push({ runId, entries, begin });
      } else later.push(runId);
    } catch {
      // an unreadable run stays as it is
    }
  }
  try {
    rebuildDenyList(ctx);
    ctx.store.purge(LIMITS.KEEP_RUNS);
  } catch {
    // startup goes on
  }
  if (own.length > 0) {
    ctx.recoveryPromise = ctx.queue
      .enqueue(async () => {
        for (const { runId, entries, begin } of own) {
          try {
            await endRestartedRun(ctx, runId, entries, begin);
          } catch {
            finishRecoveredRun(ctx, runId, entries, begin, { detail: 'cut_off' });
          }
        }
        ctx.bump();
      })
      .catch(() => {});
  }
  if (later.length > 0) {
    ctx.recheckTimer = ctx.clock.setTimeout(() => {
      ctx.recheckTimer = null;
      for (const runId of later) {
        if (ctx.store.hasResult(runId)) continue;
        try {
          const { entries, begin, action } = examine(ctx, runId);
          if (action === 'cut_off') finishRecoveredRun(ctx, runId, entries, begin, { detail: 'cut_off' });
        } catch {
          // stays as it is
        }
      }
      ctx.bump();
    }, LIMITS.ALIVE_STALE_MS + 1000);
  }
}

// The previous run for the setup screen: the last of this session, else the last in the same folder
export function loadPrevious(ctx) {
  let sameCwd = null;
  for (const runId of ctx.store.listRuns().reverse()) {
    const result = ctx.store.readResult(runId);
    if (!result || result.startFailed) continue;
    if (result.sessionId === ctx.sessionId) return { runId, result };
    if (!sameCwd && result.cwd === ctx.cwd) sameCwd = { runId, result };
  }
  return sameCwd;
}

export function refreshPrevious(ctx) {
  if (ctx.previousDirty && !ctx.run) {
    ctx.previous = loadPrevious(ctx);
    ctx.previousDirty = false;
  }
}
