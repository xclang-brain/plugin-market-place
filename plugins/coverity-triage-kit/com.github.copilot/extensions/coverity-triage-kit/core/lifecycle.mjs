import path from 'node:path';

import { analyzeEntries, numberIssues, orchestratorIssues } from './analysis.mjs';
import {
  CHILD_ROLES,
  EFFORT_LEVELS,
  IMPACT_CHOICES,
  INITIAL_CHOICES,
  LIMIT_MAX,
  LIMIT_MIN,
  LIMITS,
  REWORK_MAX,
  REWORK_MIN,
  ROLES,
} from './constants.mjs';
import { finishedOutcome, haltedByStatus, withUnstoppable } from './ending.mjs';
import {
  OPEN_FIRST,
  ROLE_LABELS,
  SETTINGS_NOT_READY,
  START_ERRORS,
  START_WHILE_RUNNING,
  STOPPED_DURING_START,
  NOTHING_TO_STOP,
  UNSTOPPED_HOW_TO,
  buildRestartRequest,
  buildStartRequest,
  guiError,
  orchestratorName,
  restartNote,
  summaryError,
} from './messages.mjs';
import { fetchStatus, startPolling, stopPolling } from './poller.mjs';
import { ACTIVE_RUN, addNote, attachStopSignal, computePhase, createRun, pickSummary, slotByName, syncSlot, viewRun } from './run.mjs';
import { callWithTimeout, errorMessage, newRecordId, sleep } from './util.mjs';

// A run from start to end (design 8.1): start, status polling, orchestrator restarts, stop, results.
// Every call to Copilot CLI has a time limit. Stopping gives up after 8 s.

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

export function logEntry(ctx, run, entry) {
  run.entries.push(entry);
  try {
    ctx.store.append(run.id, entry);
  } catch (e) {
    ctx.recordingError = e;
  }
}

// ---- start: checking the screen values ------------------------------------------------------

const intOf = (v) => (typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN);

export function validateStart(ctx, input) {
  const setup = ctx.setup;
  if (!setup) throw new Error(OPEN_FIRST);
  if (setup.settings.state === 'loading') throw new Error(SETTINGS_NOT_READY);
  const ids = new Set(setup.models.map((m) => m.id));
  const givenModels = input?.models ?? {};
  const models = {};
  for (const role of ROLES) {
    const chosen = typeof givenModels[role] === 'string' && givenModels[role] !== '' ? givenModels[role] : setup.defaults[role];
    if (ids.size > 0 && !ids.has(chosen)) throw new Error(`${ROLE_LABELS[role]}のモデル「${chosen}」は、選べるモデルにありません。`);
    models[role] = chosen;
  }
  const givenEfforts = input?.efforts ?? {};
  const efforts = {};
  for (const role of ROLES) {
    const chosen = typeof givenEfforts[role] === 'string' ? givenEfforts[role] : '';
    if (chosen !== '' && !EFFORT_LEVELS.includes(chosen)) throw new Error(`${ROLE_LABELS[role]}の思考の深さ「${chosen}」は、選べる値にありません。`);
    efforts[role] = chosen;
  }
  const rawRework = input?.maxRework ?? setup.maxReworkDefault;
  const maxRework = intOf(rawRework);
  if (!Number.isInteger(maxRework) || maxRework < REWORK_MIN || maxRework > REWORK_MAX) {
    throw new Error(`繰り返しの上限の回数は、${REWORK_MIN} から ${REWORK_MAX} までの整数にしてください。`);
  }
  // a key that is not sent takes the screen's first value; an emptied field is sent as '' or null
  const first = setup.conditions ?? {};
  const given = input?.conditions ?? {};
  const c = Object.fromEntries(['filter', 'impact', 'limit', 'initial'].map((k) => [k, given[k] === undefined ? first[k] : given[k]]));
  const filter = typeof c.filter === 'string' ? c.filter.trim() : '';
  if (!filter) throw new Error('「条件ファイルの名前」を入力してください。');
  if (filter.length > 200 || filter.startsWith('-') || /[\\/:*?"<>|\u0000-\u001f]/.test(filter)) {
    throw new Error('「条件ファイルの名前」には、ファイルの名前だけを書いてください（フォルダの区切りや記号は使えません）。');
  }
  const impact = typeof c.impact === 'string' && c.impact !== '' ? c.impact : IMPACT_CHOICES[0];
  if (!IMPACT_CHOICES.includes(impact)) throw new Error('「Impact で絞る」は、選べる値から選んでください。');
  let limit = null;
  if (!(c.limit === undefined || c.limit === null || (typeof c.limit === 'string' && c.limit.trim() === ''))) {
    limit = intOf(c.limit);
    if (!Number.isInteger(limit) || limit < LIMIT_MIN || limit > LIMIT_MAX) {
      throw new Error(`「調べる件数の上限」は、${LIMIT_MIN} から ${LIMIT_MAX} までの整数で入力してください（空なら条件ファイルの上限）。`);
    }
  }
  const initial = typeof c.initial === 'string' && c.initial !== '' ? c.initial : INITIAL_CHOICES[0];
  if (!INITIAL_CHOICES.includes(initial)) throw new Error('「最初に解析するか」は、選べる値から選んでください。');
  return { models, efforts, maxRework, conditions: { filter, impact, limit, initial } };
}

// Model and effort overrides for this chat only (design 8.1 step 2.1). Keys are the defaults.json ids.
// The orchestrator's model goes to startAgent; its effort can only go here.
export function buildOverrideMap(ctx, settings) {
  const map = {};
  const put = (id, model, effort) => {
    if (!id) return;
    const entry = {};
    if (model) entry.model = model;
    if (effort) entry.effort = effort;
    if (Object.keys(entry).length > 0) map[id] = entry;
  };
  put(ctx.defaults.orchestrator, '', settings.efforts.orchestrator);
  for (const role of CHILD_ROLES) put(ctx.defaults.roles[role], settings.models[role], settings.efforts[role]);
  return map;
}

function newRunArgs(conditions) {
  return {
    filter: conditions.filter,
    impact: conditions.impact === IMPACT_CHOICES[0] ? null : conditions.impact,
    limit: conditions.limit,
    initial: conditions.initial === 'する' ? true : conditions.initial === 'しない' ? false : null,
  };
}

function requestArgs(ctx, run) {
  return { runDir: run.runDir, runCommandPath: ctx.commands.runScript, roles: ctx.defaults.roles };
}

// ---- alive mark ---------------------------------------------------------------------------

function startAlive(ctx, run) {
  stopAlive(ctx);
  ctx.aliveTimer = ctx.clock.setInterval(() => {
    try {
      ctx.store.writeAlive(run.id);
    } catch {
      // the run goes on without it
    }
  }, LIMITS.ALIVE_INTERVAL_MS);
}

function stopAlive(ctx) {
  if (ctx.aliveTimer !== null && ctx.aliveTimer !== undefined) ctx.clock.clearInterval(ctx.aliveTimer);
  ctx.aliveTimer = null;
}

// ---- tasks.list helpers ----------------------------------------------------------------------

function normalizeTasks(list) {
  return (Array.isArray(list) ? list : [])
    .filter((t) => t && (!t.type || t.type === 'agent') && (t.agentId ?? t.id) != null)
    .map((t) => ({
      agentId: t.agentId ?? t.id,
      displayName: t.displayName ?? '',
      status: t.status,
      agentType: t.agentType ?? '',
      parentAgentId: t.parentAgentId ?? null,
    }));
}

function denyTree(ctx, run) {
  for (const id of run.tree.keys()) ctx.deny.add(id);
}

function augmentTree(ctx, run, tasks) {
  for (const t of tasks) {
    if (run.tree.has(t.agentId) || !t.parentAgentId || !run.tree.has(t.parentAgentId)) continue;
    const parent = run.tree.get(t.parentAgentId);
    const now = ctx.clock.now();
    run.tree.set(t.agentId, {
      agentId: t.agentId,
      parentId: t.parentAgentId,
      agentType: t.agentType,
      slotName: null,
      role: parent.role === 'orchestrator' ? 'other' : parent.role,
      status: 'running',
      description: '',
      model: '',
      startedAt: now,
      lastEventAt: now,
      reply: null,
    });
    ctx.deny.add(t.agentId);
    logEntry(ctx, run, { t: 'tree', at: now, agentIds: [t.agentId] });
  }
}

function activeTreeAgents(run, tasks) {
  const byId = new Map(tasks.map((t) => [t.agentId, t]));
  return [...run.tree.keys()].filter((id) => {
    const t = byId.get(id);
    return t && !TERMINAL.has(t.status);
  });
}

function unconfirmedAgents(run) {
  return [...run.tree.values()]
    .filter((rec) => rec.status === 'running' || rec.status === 'waiting' || (rec.agentId === run.rootId && rec.status !== 'stopped' && rec.status !== 'failed'))
    .map((rec) => rec.agentId);
}

const describeUnstopped = (run, ids) => ids.map((agentId) => ({ agentId, role: run.tree.get(agentId)?.role ?? 'other', howTo: UNSTOPPED_HOW_TO }));

async function unsetOverrides(ctx, run) {
  if (!run.overridesTouched) return;
  const r = await callWithTimeout(ctx.clock, () => ctx.port.setSubagentSettings(null), LIMITS.CALL_TIMEOUT_MS);
  run.overridesTouched = false;
  if (r.status !== 'ok') logEntry(ctx, run, { t: 'note', at: ctx.clock.now(), kind: 'override_not_removed', status: r.status });
}

// ---- start ----------------------------------------------------------------------------------

export function startRun(ctx, input) {
  const current = ctx.run;
  if (current && current.status !== 'finished') throw new Error(START_WHILE_RUNNING);
  const settings = validateStart(ctx, input);
  const now = ctx.clock.now();
  let id = newRecordId(now);
  while (ctx.store.listRuns().includes(id)) id = newRecordId(now);
  const run = createRun(ctx, { id, settings, requestText: ctx.request.text, isAuto: ctx.setup.isAuto, startedAt: now, parallel: ctx.setup.settings.values?.parallel });
  attachStopSignal(run);
  ctx.run = run;
  ctx.startError = null;
  const begin = {
    t: 'begin',
    at: now,
    runId: id,
    sessionId: ctx.sessionId,
    cwd: ctx.cwd,
    pluginName: ctx.pluginName,
    requestText: ctx.request.text,
    settings,
    isAuto: run.isAuto,
  };
  run.entries.push(begin);
  try {
    ctx.store.createRun(id, begin);
    ctx.store.purge(LIMITS.KEEP_RUNS);
  } catch (e) {
    ctx.recordingError = e;
  }
  startAlive(ctx, run);
  ctx.bump();
  return ctx.queue.enqueue(() => startFlow(ctx, run));
}

export function setRoot(ctx, run, rootId) {
  const now = ctx.clock.now();
  const previous = run.rootId ? run.tree.get(run.rootId) : null;
  run.rootId = rootId;
  if (!run.roots.includes(rootId)) run.roots.push(rootId);
  ctx.knownRoots.add(rootId);
  const folder = run.runDir ? path.basename(run.runDir) : '';
  const rec = {
    agentId: rootId,
    parentId: null,
    agentType: ctx.defaults.orchestrator,
    slotName: 'orchestrator',
    role: 'orchestrator',
    status: 'running',
    description: run.restarts > 0 ? `一括のトリアージの指揮（続きから。始め直し ${run.restarts} 回目）` : `一括のトリアージの指揮${folder ? `（実行 ${folder}）` : ''}`,
    model: '',
    startedAt: now,
    lastEventAt: now,
    reply: null,
    isRoot: true,
  };
  if (previous && previous.slotName === 'orchestrator') previous.slotName = null;
  run.tree.set(rootId, rec);
  slotByName(run, 'orchestrator').agentId = rootId;
  syncSlot(run, rec);
  logEntry(ctx, run, { t: 'tree', at: now, agentIds: [rootId], root: true });
}

// startAgent, cancelling an orchestrator that arrives after we gave up waiting
function launchOrchestrator(ctx, run, prompt) {
  const started = Promise.resolve().then(() =>
    ctx.port.startAgent({ agentType: ctx.defaults.orchestrator, name: orchestratorName(run.id), model: run.settings.models.orchestrator, prompt }),
  );
  started.then(
    (res) => {
      if (run.startAbandoned && res && typeof res.agentId === 'string') {
        ctx.deny.add(res.agentId);
        if (!ctx.strayHandled.has(res.agentId)) {
          ctx.strayHandled.add(res.agentId);
          ctx.queue.enqueue(() => callWithTimeout(ctx.clock, () => ctx.port.cancelAgent(res.agentId), LIMITS.CALL_TIMEOUT_MS)).catch(() => {});
        }
      }
    },
    () => {},
  );
  return started;
}

async function startFlow(ctx, run) {
  const { port, clock } = ctx;
  const call = (fn) => callWithTimeout(clock, fn, LIMITS.CALL_TIMEOUT_MS, run.stopSignal);
  let failure = null;
  let launched = false;
  let rootId = null;

  const cur = await call(() => port.getCurrentModel());
  if (cur.status === 'ok' && cur.value) run.isAuto = Boolean(cur.value.isAuto);

  if (!ctx.defaults.orchestrator) failure = { message: START_ERRORS.noOrchestrator };

  // 1. overrides
  if (!failure && !run.stopRequested) {
    const map = buildOverrideMap(ctx, run.settings);
    if (Object.keys(map).length > 0) {
      run.overridesTouched = true;
      const r = await call(() => port.setSubagentSettings(map));
      if (r.status === 'error') failure = { message: START_ERRORS.overrides(errorMessage(r.error)) };
      else if (r.status === 'timeout') failure = { message: START_ERRORS.overridesTimeout };
    }
  }

  // 2. new-run (runs to its end even when stop is pressed; the result decides what to clean up)
  if (!failure && !run.stopRequested) {
    const r = await ctx.commands.newRun(ctx.cwd, { ...newRunArgs(run.settings.conditions), maxRework: run.settings.maxRework });
    if (r.ok && typeof r.data.run_dir === 'string' && r.data.run_dir !== '') {
      run.runDir = r.data.run_dir;
      run.repoRoot = typeof r.data.repo_root === 'string' && r.data.repo_root ? r.data.repo_root : ctx.cwd;
      run.newRun = {
        run_dir: r.data.run_dir,
        limit: r.data.limit ?? null,
        initial: r.data.initial ?? null,
        review: r.data.review ?? null,
        integration: r.data.integration ?? null,
        max_rework: r.data.max_rework ?? null,
        permission_steps: Array.isArray(r.data.permission_steps) ? r.data.permission_steps : [],
      };
      logEntry(ctx, run, { t: 'note', at: clock.now(), kind: 'new_run', runDir: run.runDir });
      startPolling(ctx, run);
    } else {
      const detail = r.ok ? '実行フォルダの場所が返りませんでした。' : r.error;
      failure = { message: START_ERRORS.newRun(detail), problems: r.ok ? [] : r.problems, hint: true };
    }
  }

  // 3. the orchestrator (background)
  if (!failure && !run.stopRequested) {
    launched = true;
    const started = launchOrchestrator(ctx, run, buildStartRequest(requestArgs(ctx, run)));
    const r = await call(() => started);
    if (r.status === 'ok') {
      rootId = r.value?.agentId;
      if (typeof rootId !== 'string' || rootId === '') {
        rootId = null;
        failure = { message: START_ERRORS.noAgentId };
      }
    } else if (r.status === 'error') {
      failure = { message: START_ERRORS.startAgent(errorMessage(r.error)) };
    } else {
      run.startAbandoned = true;
      if (r.status === 'timeout') failure = { message: START_ERRORS.startTimeout, timedOut: true };
    }
  }

  if (rootId) setRoot(ctx, run, rootId);
  if (run.stopRequested) return stopDuringStart(ctx, run, launched);
  if (failure) return failStart(ctx, run, failure, launched);
  run.status = 'running';
  logEntry(ctx, run, { t: 'status', at: clock.now(), status: 'running' });
  ctx.bump();
  return { runId: run.id, runDir: run.runDir };
}

async function findOrchestratorByName(ctx, run, budgetMs) {
  const r = await callWithTimeout(ctx.clock, () => ctx.port.listTasks(), Math.min(LIMITS.CALL_TIMEOUT_MS, budgetMs));
  if (r.status !== 'ok') return [];
  const hits = normalizeTasks(r.value).filter((t) => String(t.displayName).includes(run.id));
  for (const t of hits) {
    ctx.deny.add(t.agentId);
    ctx.knownRoots.add(t.agentId);
    ctx.strayHandled.add(t.agentId);
  }
  return hits.map((t) => t.agentId);
}

async function stopDuringStart(ctx, run, launched) {
  if (launched && !run.rootId) {
    const deadline = run.stopAt + LIMITS.STOP_DEADLINE_MS;
    const found = await findOrchestratorByName(ctx, run, deadline - ctx.clock.now());
    if (found.length > 0) setRoot(ctx, run, found[0]);
    else logEntry(ctx, run, { t: 'note', at: ctx.clock.now(), kind: 'start_unknown' });
  }
  await closeRun(ctx, run, { userStop: true });
  throw new Error(STOPPED_DURING_START);
}

async function failStart(ctx, run, failure, launched) {
  stopPolling(ctx, run);
  let found = [];
  if (failure.timedOut && launched) {
    found = await findOrchestratorByName(ctx, run, LIMITS.CALL_TIMEOUT_MS);
    for (const id of found) {
      logEntry(ctx, run, { t: 'tree', at: ctx.clock.now(), agentIds: [id] });
      await callWithTimeout(ctx.clock, () => ctx.port.cancelAgent(id), LIMITS.CALL_TIMEOUT_MS);
    }
  }
  await unsetOverrides(ctx, run);
  const unknownLaunch = Boolean(failure.timedOut && launched && found.length === 0);
  let message = failure.message.replace(/[。.]?$/, '。');
  if (failure.timedOut && launched) message += unknownLaunch ? '起動したかは分かりません。' : '起動していた統括エージェントは取り消しました。';
  message += 'もう一度開始できます。';
  const now = ctx.clock.now();
  const startError = { message, problems: failure.problems ?? [], hint: Boolean(failure.hint), unknownLaunch, runDir: run.runDir };
  logEntry(ctx, run, { t: 'note', at: now, kind: 'start_failed', ...startError });
  run.status = 'finished';
  run.startFailed = true;
  run.finishedAt = now;
  try {
    ctx.store.writeResult(run.id, { runId: run.id, id: run.id, sessionId: ctx.sessionId, cwd: ctx.cwd, startedAt: run.startedAt, finishedAt: now, status: 'finished', startFailed: true, startError });
  } catch (e) {
    ctx.recordingError = e;
  }
  stopAlive(ctx);
  if (ctx.run === run) ctx.run = null;
  ctx.startError = startError;
  ctx.bump();
  throw new Error(message);
}

// ---- the orchestrator ended: restart, or end the run (REQ-415, design 8.1 step 4) -------------

export async function onRootEnded(ctx, run, cause, message) {
  if (run.closeRequested || run.closing || run.status !== 'running' || run.stopRequested) return;
  const r = await fetchStatus(ctx, run);
  if (run.closeRequested || run.closing || run.status !== 'running' || run.stopRequested) return;
  const st = run.st;
  const creditLimit = Boolean(run.limit && (!run.limit.answered || run.limit.action === 'cancel'));
  if (r.ok && st?.finished) {
    run.pendingEnd = { kind: 'finished' };
  } else if (r.ok && haltedByStatus(st)) {
    run.pendingEnd = { kind: 'aborted', detail: 'run_halted' };
  } else if (creditLimit) {
    run.pendingEnd = { kind: 'aborted', detail: 'credit_limit' };
  } else if (cause === 'cancelled') {
    run.pendingEnd = { kind: 'aborted', detail: 'chat_interrupted' };
  } else if (run.restarts < LIMITS.MAX_RESTARTS) {
    await restartOrchestrator(ctx, run, cause, message);
    return;
  } else {
    run.pendingEnd = { kind: 'aborted', detail: cause === 'failed' ? 'orchestrator_failed' : 'orchestrator_ended', ...(message ? { message } : {}) };
  }
  requestClose(ctx, run, { userStop: false });
}

async function restartOrchestrator(ctx, run, cause, message) {
  run.restarts += 1;
  const now = ctx.clock.now();
  logEntry(ctx, run, { t: 'note', at: now, kind: 'restart', n: run.restarts, cause, message: message || null });
  addNote(run, now, restartNote(run.restarts, LIMITS.MAX_RESTARTS));
  const prompt = buildRestartRequest({ restart: run.restarts, ...requestArgs(ctx, run) });
  const r = await callWithTimeout(ctx.clock, () => launchOrchestrator(ctx, run, prompt), LIMITS.CALL_TIMEOUT_MS, run.stopSignal);
  const agentId = r.status === 'ok' ? r.value?.agentId : null;
  if (typeof agentId === 'string' && agentId !== '') {
    setRoot(ctx, run, agentId);
    ctx.bump();
    return;
  }
  if (r.status === 'timeout' || r.status === 'stopped') run.startAbandoned = true;
  if (run.stopRequested) return;
  run.pendingEnd = { kind: 'aborted', detail: 'orchestrator_failed', message: r.status === 'error' ? START_ERRORS.startAgent(errorMessage(r.error)) : START_ERRORS.startTimeout };
  requestClose(ctx, run, { userStop: false });
}

// ---- stop -----------------------------------------------------------------------------------

export function stopRun(ctx) {
  const run = ctx.run;
  if (!run || run.status === 'finished') throw new Error(NOTHING_TO_STOP);
  if (run.status === 'stopping' || run.closeRequested || run.closing) return { already: true };
  run.phaseAtClose = computePhase(run, true);
  run.stopRequested = true;
  run.stopAt = ctx.clock.now();
  run.pendingEnd = { kind: 'user_stopped', detail: null };
  const starting = run.status === 'starting';
  run.status = 'stopping';
  stopPolling(ctx, run);
  denyTree(ctx, run);
  logEntry(ctx, run, { t: 'status', at: run.stopAt, status: 'stopping' });
  ctx.bump();
  run.resolveStop();
  if (!starting) {
    run.closeRequested = true;
    ctx.queue.enqueue(() => closeRun(ctx, run, { userStop: true })).catch(() => {});
  }
  return { already: false };
}

export function requestClose(ctx, run, { userStop }) {
  if (run.closeRequested || run.closing) return;
  run.closeRequested = true;
  run.phaseAtClose ??= computePhase(run, true);
  ctx.queue.enqueue(() => closeRun(ctx, run, { userStop })).catch(() => {});
}

async function closeRun(ctx, run, { userStop }) {
  if (run.closing || run.status === 'finished') return;
  run.closing = true;
  stopPolling(ctx, run);
  const state = { tasks: null, unstopped: [] };
  try {
    await runClose(ctx, run, userStop, state);
  } catch (e) {
    ctx.recordingError = e;
    state.unstopped = describeUnstopped(run, unconfirmedAgents(run));
  }
  if (run.pendingEnd?.kind === 'finished') {
    try {
      await collectResults(ctx, run);
    } catch (e) {
      ctx.recordingError = e;
    }
  }
  finalizeRun(ctx, run, state);
  await unsetOverrides(ctx, run);
}

async function stopJobs(ctx, run) {
  if (!run.runDir) return;
  const r = await ctx.commands.stop(run.runDir);
  if (r.ok) {
    run.stoppedJobs = (Array.isArray(r.data.stopped_jobs) ? r.data.stopped_jobs : []).map((j) => (typeof j === 'string' ? j : String(j?.name ?? '')));
    run.unstoppedJobs = (Array.isArray(r.data.unstopped) ? r.data.unstopped : []).map((j) => ({
      name: String(j?.name ?? ''),
      pid: Number.isInteger(j?.pid) ? j.pid : null,
      how: String(j?.how ?? ''),
    }));
  } else {
    run.unstoppedJobs = [{ name: '初回解析やビルドの処理', pid: null, how: `止めたかを確かめられませんでした（${r.error}）。` }];
  }
  logEntry(ctx, run, { t: 'note', at: ctx.clock.now(), kind: 'stop_jobs', ok: r.ok, stopped: run.stoppedJobs, unstopped: run.unstoppedJobs });
}

async function runClose(ctx, run, userStop, state) {
  const { clock, port } = ctx;
  const startedAt = userStop ? run.stopAt : clock.now();
  const deadline = startedAt + LIMITS.STOP_DEADLINE_MS;
  const left = () => deadline - clock.now();
  const call = (fn) => callWithTimeout(clock, fn, Math.min(LIMITS.CALL_TIMEOUT_MS, left()));
  denyTree(ctx, run);

  // detached jobs and the last status run in parallel with stopping the agents
  const side = userStop ? Promise.all([stopJobs(ctx, run), fetchStatus(ctx, run)]) : Promise.resolve();

  const refresh = async () => {
    const r = await call(() => port.listTasks());
    if (r.status === 'ok') {
      state.tasks = normalizeTasks(r.value);
      augmentTree(ctx, run, state.tasks);
      return true;
    }
    return false;
  };
  const goStopping = () => {
    run.status = 'stopping';
    logEntry(ctx, run, { t: 'status', at: clock.now(), status: 'stopping' });
    ctx.bump();
  };

  let needCancel = userStop;
  if (!userStop) {
    // an ended orchestrator stays idle in the list: cancel it quietly; others still running go through the stop steps
    const ok = await refresh();
    const active = ok ? activeTreeAgents(run, state.tasks) : null;
    if (active === null) {
      needCancel = true;
      goStopping();
    } else if (active.length === 0) {
      needCancel = false;
    } else if (active.every((id) => run.roots.includes(id) && state.tasks.find((t) => t.agentId === id)?.status === 'idle')) {
      needCancel = true; // only orchestrators that ended their turn are left (a restart leaves the earlier one idle)
    } else {
      needCancel = true;
      goStopping();
    }
  }

  if (needCancel) {
    // every orchestrator of this run (restarts leave earlier ones); cancelling one also cancels its agents
    const known = state.tasks ? new Set(activeTreeAgents(run, state.tasks)) : null;
    for (const id of run.roots) {
      if (known && !known.has(id)) continue;
      const c = await call(() => port.cancelAgent(id));
      logEntry(ctx, run, { t: 'note', at: clock.now(), kind: 'cancel_root', agentId: id, status: c.status });
    }
  }

  if (needCancel && run.tree.size > 0) {
    let knownActive = null;
    for (;;) {
      const ok = await refresh();
      if (ok) {
        knownActive = activeTreeAgents(run, state.tasks);
        if (knownActive.length === 0) break;
      }
      if (left() <= 0) {
        state.unstopped = describeUnstopped(run, knownActive ?? unconfirmedAgents(run));
        break;
      }
      await sleep(clock, Math.min(LIMITS.STOP_POLL_MS, Math.max(left(), 0)));
    }
  }
  await side;
}

// The screen URL (design 4.16) carries the view in the fragment: #run=<id>&view=<view>&item=<ID>
export function withoutItem(url) {
  const at = url.indexOf('#');
  if (at < 0) return url;
  const params = new URLSearchParams(url.slice(at + 1));
  params.delete('item');
  const rest = params.toString();
  return rest ? `${url.slice(0, at)}#${rest}` : url.slice(0, at);
}

const CONFIDENCE_ORDER = { low: 0, medium: 1, high: 2 };
export function lowestConfidenceItem(summaryData, st) {
  const fromSummary = Array.isArray(summaryData?.results) ? summaryData.results : [];
  const fromStatus = (Array.isArray(st?.items) ? st.items : []).filter((i) => i?.status === 'done');
  const list = (fromSummary.length > 0 ? fromSummary : fromStatus).filter((i) => i && (typeof i.id === 'string' || typeof i.id === 'number'));
  let best = null;
  for (const item of list) {
    const rank = CONFIDENCE_ORDER[item.confidence] ?? 3;
    if (!best || rank < best.rank) best = { id: String(item.id), rank };
  }
  return best ? best.id : null;
}

// The run finished: the summary (made here when missing), the last status, and the result-screen links (design 8.1 step 5)
async function collectResults(ctx, run) {
  run.status = 'summarizing';
  ctx.bump();
  const r = await ctx.commands.summary(run.runDir);
  if (r.ok) {
    run.summaryData = pickSummary(r.data);
    run.summaryError = null;
  } else {
    run.summaryError = summaryError(r.error);
  }
  await fetchStatus(ctx, run);
  const item = lowestConfidenceItem(run.summaryData, run.st);
  const g = await ctx.commands.guiOpen({ repo: run.repoRoot ?? ctx.cwd, runDir: run.runDir, view: 'summary', item });
  if (g.ok && typeof g.data.url === 'string' && /^http:\/\/(127\.0\.0\.1|localhost):\d+\//.test(g.data.url)) {
    const itemUrl = g.data.url;
    run.links = { summaryUrl: item ? withoutItem(itemUrl) : itemUrl, itemUrl: item ? itemUrl : null, item };
    run.guiError = null;
  } else {
    run.guiError = guiError(g.ok ? 'URL が返りませんでした。' : g.error);
  }
  logEntry(ctx, run, { t: 'note', at: ctx.clock.now(), kind: 'results', summary: r.ok, gui: Boolean(run.links) });
}

function finalizeRun(ctx, run, state) {
  const now = ctx.clock.now();
  const unstoppedIds = new Set(state.unstopped.map((u) => u.agentId));
  const taskById = new Map((state.tasks ?? []).map((t) => [t.agentId, t]));
  for (const rec of run.tree.values()) {
    ctx.deny.add(rec.agentId);
    if (rec.status !== 'running' && rec.status !== 'waiting') continue;
    if (unstoppedIds.has(rec.agentId)) rec.status = 'unstopped';
    else {
      const ts = taskById.get(rec.agentId)?.status;
      rec.status = ts === 'completed' || (rec.isRoot && ts === 'idle') ? 'completed' : ts === 'failed' ? 'failed' : 'stopped';
    }
    rec.endedAt ??= now;
    syncSlot(run, rec);
  }
  const pending = run.pendingEnd ?? { kind: 'aborted', detail: 'orchestrator_ended' };
  let endReason = pending.kind === 'finished' ? finishedOutcome(run.st, run.summaryData) : { kind: pending.kind, detail: pending.detail ?? null, notes: [] };
  if (pending.message) endReason.message = pending.message;
  endReason = withUnstoppable(endReason, state.unstopped, run.unstoppedJobs);
  run.unstopped = state.unstopped;
  run.waits = [];

  const normalEnd = endReason.kind === 'all_passed' || endReason.kind === 'max_reached';
  const drafts = analyzeEntries(run.entries, {
    runId: run.id,
    endReason,
    endAt: now,
    unstopped: state.unstopped,
    unstoppedJobs: run.unstoppedJobs,
    isAuto: run.isAuto,
    models: run.settings.models,
    restarts: run.restarts,
    st: run.st,
  });
  run.issues = [...numberIssues(drafts, 'M', 'mechanical'), ...(normalEnd ? orchestratorIssues(run.st) : [])];
  run.endReason = endReason;
  run.status = 'finished';
  run.finishedAt = now;
  logEntry(ctx, run, { t: 'status', at: now, status: 'finished', endReason });
  const snapshot = {
    ...viewRun(ctx, run, run.issues),
    runId: run.id,
    sessionId: ctx.sessionId,
    cwd: ctx.cwd,
    startedAt: run.startedAt,
    finishedAt: now,
    issues: run.issues,
  };
  try {
    ctx.store.writeResult(run.id, snapshot);
  } catch (e) {
    ctx.recordingError = e;
  }
  stopAlive(ctx);
  ctx.bump();
}

export const isActiveRun = (run) => Boolean(run && ACTIVE_RUN.has(run.status));
