import { EFFORT_LEVELS, INSTRUCTION_HEAD, LIMITS, ROLE_LABELS, UNSTOPPED_HOW_TO } from './constants.mjs';
import { effortAgents } from './task-def.mjs';
import { analyzeEntries, buildMechanicalSummary, numberIssues } from './analysis.mjs';
import { logEntry } from './log.mjs';
import { attachStopSignal, computePhase, createRun, roleForType, slotByName, syncSlot, viewRun } from './run.mjs';
import { callWithTimeout, errorMessage, newRunId, sleep, summarizeText } from './util.mjs';

// 実行の始まりから終わりまで（開始 4.3、止める 4.4、終わり 4.8・6.1）。
// Copilot CLI を待つ呼び出しは、すべて待ち時間の上限（10 秒）を付ける。止める処理は、押してから 8 秒で打ち切る。

const MODEL_ROLES = ['orchestrator', 'worker', 'verifier'];
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

// ---- 開始の前の確かめと、統括への指示文 -----------------------------------------------------------

function optionValue(o) {
  return typeof o === 'object' && o !== null ? o.value : o;
}

// 開始の設定を確かめる。合わなければ、平易な日本語の Error を投げる（必須の項目、上限の回数が 1 以上、選んだモデルと思考の深さが選択肢にあるか）。
export function validateStart(ctx, input) {
  const setup = ctx.setup;
  if (!setup) throw new Error('先に画面を開いてください。');
  const given = input?.models ?? {};
  const ids = new Set(setup.models.map((m) => m.id));
  const models = {};
  for (const role of MODEL_ROLES) {
    const chosen = typeof given[role] === 'string' && given[role] !== '' ? given[role] : setup.defaults[role];
    if (ids.size > 0 && !ids.has(chosen)) {
      throw new Error(`${ROLE_LABELS[role]}のモデル「${chosen}」は、選べるモデルにありません。`);
    }
    models[role] = chosen;
  }
  // 思考の深さは、エージェントごと。選ばない（''）ときは上書きしない（4.6）
  const givenEfforts = input?.efforts ?? {};
  const efforts = {};
  for (const { agent, label } of effortAgents(ctx.task)) {
    const chosen = typeof givenEfforts[agent] === 'string' ? givenEfforts[agent] : '';
    if (chosen !== '' && !EFFORT_LEVELS.includes(chosen)) {
      throw new Error(`${label}の思考の深さ「${chosen}」は、選べる値にありません。`);
    }
    efforts[agent] = chosen;
  }
  const rawMax = input?.maxIterations ?? ctx.task.maxIterationsDefault;
  const maxIterations = typeof rawMax === 'string' && rawMax.trim() !== '' ? Number(rawMax) : rawMax;
  if (!Number.isInteger(maxIterations) || maxIterations < 1) throw new Error('繰り返しの上限の回数は、1 以上の整数にしてください。');
  const given2 = input?.task ?? {};
  const task = {};
  for (const def of ctx.task.settings) {
    const label = def.label ?? def.key;
    const raw = given2[def.key];
    const empty = raw === undefined || raw === null || (typeof raw === 'string' && raw.trim() === '');
    if (empty) {
      if (def.type === 'checkbox') {
        task[def.key] = Boolean(def.default);
        continue;
      }
      if (def.required) throw new Error(`「${label}」を入力してください。`);
      task[def.key] = def.default ?? '';
      continue;
    }
    switch (def.type) {
      case 'number': {
        const n = typeof raw === 'number' ? raw : Number(raw);
        if (!Number.isFinite(n)) throw new Error(`「${label}」は数で入力してください。`);
        task[def.key] = n;
        break;
      }
      case 'select': {
        const values = (def.options ?? []).map(optionValue);
        if (!values.includes(raw)) throw new Error(`「${label}」は、選べる値から選んでください。`);
        task[def.key] = raw;
        break;
      }
      case 'checkbox':
        task[def.key] = raw === true || raw === 'true';
        break;
      default:
        task[def.key] = String(raw);
    }
  }
  return { models, efforts, maxIterations, task };
}

function formatSettingValue(def, value) {
  if (def.type === 'checkbox') return value ? 'はい' : 'いいえ';
  if (value === '' || value === undefined || value === null) return '（未入力）';
  return String(value);
}

// 統括への指示文（4.3）。決まった形で組み立てる。始めに、定義の手順で進めて作業・検証に任せる旨の 1 文を置く。
// プラグインのフォルダの絶対パスを入れる（4.7）。エージェントが、ルールのスキルの SKILL.md とスクリプトを絶対パスで読めるようにするためである。
export function buildInstruction(ctx, run) {
  const lines = [];
  lines.push(INSTRUCTION_HEAD, '');
  lines.push(`実行の番号：${run.id}`, '');
  if (ctx.pluginRoot) lines.push('## プラグインのフォルダ', ctx.pluginRoot, '');
  lines.push('## 頼まれた作業', run.requestText || '（作業の文は渡されませんでした）', '');
  lines.push('## 繰り返しの上限', `${run.settings.maxIterations} 回`, '');
  lines.push('## 作業ごとの設定の値');
  if (ctx.task.settings.length === 0) lines.push('- （なし）');
  for (const def of ctx.task.settings) {
    lines.push(`- ${def.label ?? def.key}（${def.key}）：${formatSettingValue(def, run.settings.task[def.key])}`);
  }
  lines.push('');
  lines.push('## ユーザーと確かめた合否の基準（計画の評価基準に、番号と文を変えずに、すべて入れてください）');
  if (ctx.task.criteria.length === 0) lines.push('- （なし）');
  for (const c of ctx.task.criteria) lines.push(`- ${c.id}：${c.text}`);
  return lines.join('\n');
}

// 作業・検証のモデルと、エージェントごとの思考の深さの上書き（4.3、4.6）。キーは <プラグイン>:<エージェント>、値は { model?, effort? }。
// 統括のモデルは起動のときに渡すので入れない。統括の思考の深さは、起動のときに渡せないので、ここに入れる。
export function buildOverrideMap(ctx, settings) {
  const models = settings?.models ?? {};
  const efforts = settings?.efforts ?? {};
  const map = {};
  const put = (agent, model) => {
    const entry = {};
    if (model) entry.model = model;
    if (efforts[agent]) entry.effort = efforts[agent];
    if (Object.keys(entry).length > 0) map[`${ctx.pluginName}:${agent}`] = entry;
  };
  put('orchestrator', '');
  for (const w of ctx.task.workers) put(w.agent, models.worker);
  put('verifier', models.verifier);
  return map;
}

// ---- 生きている印 ------------------------------------------------------------------------------

function startAlive(ctx, run) {
  stopAlive(ctx);
  ctx.aliveTimer = ctx.clock.setInterval(() => {
    try {
      ctx.store.writeAlive(run.id);
    } catch {
      // 書けなくても実行は続ける
    }
  }, LIMITS.ALIVE_INTERVAL_MS);
}

function stopAlive(ctx) {
  if (ctx.aliveTimer !== null && ctx.aliveTimer !== undefined) ctx.clock.clearInterval(ctx.aliveTimer);
  ctx.aliveTimer = null;
}

// ---- 窓口の返り値を読む部品 ----------------------------------------------------------------------

function normalizeTasks(list) {
  return list
    .filter((t) => t && (!t.type || t.type === 'agent') && (t.agentId ?? t.id) != null)
    .map((t) => ({
      agentId: t.agentId ?? t.id,
      displayName: t.displayName ?? '',
      status: t.status,
      executionMode: t.executionMode,
      agentType: t.agentType ?? '',
      parentAgentId: t.parentAgentId ?? null,
    }));
}

function denyTree(ctx, run) {
  for (const id of run.tree.keys()) ctx.deny.add(id);
}

// 一覧にあって、木に入っていない子を足す（取り消しの間に増えたエージェントを取りこぼさない）
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
      role: roleForType(ctx, t.agentType, parent),
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

// 木のエージェントのうち、一覧で動いている（running か idle）もの。一覧にないものは止まったと見る。
function activeTreeAgents(run, tasks) {
  const byId = new Map(tasks.map((t) => [t.agentId, t]));
  return [...run.tree.keys()].filter((id) => {
    const t = byId.get(id);
    return t && !TERMINAL.has(t.status);
  });
}

// 一覧が取れなかったとき、止まったと確かめられないエージェント
function unconfirmedAgents(run) {
  return [...run.tree.values()]
    .filter((rec) => rec.status === 'running' || rec.status === 'waiting' || (rec.agentId === run.rootId && rec.status !== 'stopped' && rec.status !== 'failed'))
    .map((rec) => rec.agentId);
}

function describeUnstopped(run, ids) {
  return ids.map((agentId) => ({ agentId, role: run.tree.get(agentId)?.role ?? 'worker', howTo: UNSTOPPED_HOW_TO }));
}

async function unsetOverrides(ctx, run) {
  if (!run.overridesTouched) return;
  const r = await callWithTimeout(ctx.clock, () => ctx.port.setSubagentSettings(null), LIMITS.CALL_TIMEOUT_MS);
  run.overridesTouched = false;
  if (r.status !== 'ok') {
    logEntry(ctx, run, { t: 'note', at: ctx.clock.now(), kind: 'override_not_removed', status: r.status });
  }
}

// ---- 開始 ------------------------------------------------------------------------------------

export function startRun(ctx, input) {
  const current = ctx.run;
  if (current && current.status !== 'finished') throw new Error('すでに実行中です。');
  const settings = validateStart(ctx, input);
  const now = ctx.clock.now();
  let id = newRunId(now);
  while (ctx.store.listRuns().includes(id)) id = newRunId(now);
  const run = createRun(ctx, { id, settings, requestText: ctx.requestText, isAuto: ctx.setup.isAuto, startedAt: now });
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
    requestText: ctx.requestText,
    settings,
    isAuto: run.isAuto,
    criteria: ctx.task.criteria.map((c) => ({ id: c.id, text: c.text })),
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

function setRoot(ctx, run, rootId) {
  const now = ctx.clock.now();
  run.rootId = rootId;
  ctx.knownRoots.add(rootId);
  const rec = {
    agentId: rootId,
    parentId: null,
    agentType: `${ctx.pluginName}:orchestrator`,
    slotName: 'orchestrator',
    role: 'orchestrator',
    status: 'running',
    description: summarizeText(run.requestText, 80),
    model: '',
    startedAt: now,
    lastEventAt: now,
    reply: null,
  };
  run.tree.set(rootId, rec);
  slotByName(run, 'orchestrator').agentId = rootId;
  syncSlot(run, rec);
  logEntry(ctx, run, { t: 'tree', at: now, agentIds: [rootId] });
}

async function startFlow(ctx, run) {
  const { port, clock } = ctx;
  const call = (fn) => callWithTimeout(clock, fn, LIMITS.CALL_TIMEOUT_MS, run.stopSignal);
  let failure = null;
  let launched = false;
  let rootId = null;

  const cur = await call(() => port.getCurrentModel());
  if (cur.status === 'ok' && cur.value) run.isAuto = Boolean(cur.value.isAuto);

  // 1. 作業・検証のモデルと、エージェントごとの思考の深さの上書き
  if (!run.stopRequested) {
    const map = buildOverrideMap(ctx, run.settings);
    if (Object.keys(map).length > 0) {
      run.overridesTouched = true;
      const r = await call(() => port.setSubagentSettings(map));
      if (r.status === 'error') failure = { message: `モデルと思考の深さを設定できませんでした：${errorMessage(r.error)}`, timedOut: false };
      else if (r.status === 'timeout') failure = { message: 'モデルと思考の深さの設定が時間切れになりました。', timedOut: true };
    }
  }

  // 2. 統括の起動
  if (!failure && !run.stopRequested) {
    launched = true;
    const started = Promise.resolve().then(() =>
      port.startAgent({
        agentType: `${ctx.pluginName}:orchestrator`,
        name: `統括 ${run.id}`,
        model: run.settings.models.orchestrator,
        prompt: buildInstruction(ctx, run),
      }),
    );
    // 待ちを打ち切った後に起動の結果が届いたら、その統括を取り消す（遅れて起動した統括。4.3）
    started.then(
      (res) => {
        if (run.startAbandoned && res && typeof res.agentId === 'string') {
          ctx.deny.add(res.agentId);
          if (!ctx.strayHandled.has(res.agentId)) {
            ctx.strayHandled.add(res.agentId);
            ctx.queue
              .enqueue(() => callWithTimeout(ctx.clock, () => ctx.port.cancelAgent(res.agentId), LIMITS.CALL_TIMEOUT_MS))
              .catch(() => {});
          }
        }
      },
      () => {},
    );
    const r = await call(() => started);
    if (r.status === 'ok') {
      rootId = r.value?.agentId;
      if (typeof rootId !== 'string' || rootId === '') {
        rootId = null;
        failure = { message: '統括の番号が返りませんでした。', timedOut: false };
      }
    } else if (r.status === 'error') {
      failure = { message: `統括を起動できませんでした：${errorMessage(r.error)}`, timedOut: false };
    } else {
      run.startAbandoned = true;
      if (r.status === 'timeout') failure = { message: '統括の起動が時間切れになりました。', timedOut: true };
    }
  }

  if (rootId) setRoot(ctx, run, rootId);
  if (run.stopRequested) return stopDuringStart(ctx, run, launched);
  if (failure) return failStart(ctx, run, failure, launched);
  run.status = 'running';
  logEntry(ctx, run, { t: 'status', at: clock.now(), status: 'running' });
  ctx.bump();
  return { runId: run.id };
}

// 起動したかが分からないとき、名前から統括を探す（4.3）。見つかったものを木に入れて、拒む一覧に入れる。
async function findOrchestratorByName(ctx, run, budgetMs) {
  const r = await callWithTimeout(ctx.clock, () => ctx.port.listTasks(), Math.min(LIMITS.CALL_TIMEOUT_MS, budgetMs));
  if (r.status !== 'ok' || !Array.isArray(r.value)) return [];
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
  throw new Error('開始している途中で止められました。');
}

async function failStart(ctx, run, failure, launched) {
  let found = [];
  if (failure.timedOut && launched) {
    found = await findOrchestratorByName(ctx, run, LIMITS.CALL_TIMEOUT_MS);
    for (const id of found) {
      logEntry(ctx, run, { t: 'tree', at: ctx.clock.now(), agentIds: [id] });
      await callWithTimeout(ctx.clock, () => ctx.port.cancelAgent(id), LIMITS.CALL_TIMEOUT_MS);
    }
  }
  await unsetOverrides(ctx, run);
  const unknownLaunch = failure.timedOut && launched && found.length === 0;
  let message = failure.message.replace(/[。.]?$/, '。');
  if (failure.timedOut && launched) {
    message += unknownLaunch ? '起動したかは分かりません。' : '起動していた統括は取り消しました。';
  }
  message += 'もう一度開始できます。';
  const now = ctx.clock.now();
  logEntry(ctx, run, { t: 'note', at: now, kind: 'start_failed', message, unknownLaunch });
  run.status = 'finished';
  run.startFailed = true;
  run.finishedAt = now;
  try {
    ctx.store.writeResult(run.id, {
      runId: run.id,
      id: run.id,
      sessionId: ctx.sessionId,
      cwd: ctx.cwd,
      startedAt: run.startedAt,
      finishedAt: now,
      status: 'finished',
      startFailed: true,
      startError: { message, unknownLaunch },
    });
  } catch (e) {
    ctx.recordingError = e;
  }
  stopAlive(ctx);
  if (ctx.run === run) ctx.run = null;
  ctx.startError = { message, unknownLaunch };
  ctx.bump();
  throw new Error(message);
}

// ---- 止める・終える ---------------------------------------------------------------------------

// 止めるを押した（4.4）。届いた時点で受け付けて「停止中」にし、以後この実行のエージェントのツールを拒む。
// 開始中なら、開始の処理が止めるを見て打ち切る（4.3）。
export function stopRun(ctx) {
  const run = ctx.run;
  if (!run || run.status === 'finished') throw new Error('止める実行がありません。');
  if (run.status === 'stopping' || run.closeRequested || run.closing) return { already: true };
  run.phaseAtClose = computePhase(run, true);
  run.stopRequested = true;
  run.stopAt = ctx.clock.now();
  run.pendingEnd = { kind: 'user_stopped', detail: null };
  const starting = run.status === 'starting';
  run.status = 'stopping';
  denyTree(ctx, run);
  logEntry(ctx, run, { t: 'status', at: run.stopAt, status: 'stopping' });
  ctx.bump();
  if (starting) {
    run.resolveStop();
  } else {
    run.closeRequested = true;
    ctx.queue.enqueue(() => closeRun(ctx, run, { userStop: true })).catch(() => {});
  }
  return { already: false };
}

// 実行を終えると決めた（統括が終わった、記録のやり直しが上限を超えた）。終わった理由は run.pendingEnd に入れてから呼ぶ。
export function requestClose(ctx, run, { userStop }) {
  if (run.closeRequested || run.closing) return;
  run.closeRequested = true;
  run.phaseAtClose ??= computePhase(run, true);
  ctx.queue.enqueue(() => closeRun(ctx, run, { userStop })).catch(() => {});
}

async function closeRun(ctx, run, { userStop }) {
  if (run.closing || run.status === 'finished') return;
  run.closing = true;
  const state = { tasks: null, unstopped: [] };
  try {
    await runClose(ctx, run, userStop, state);
  } catch (e) {
    ctx.recordingError = e;
    state.unstopped = unconfirmedAgents(run);
  }
  finalizeRun(ctx, run, state);
  await unsetOverrides(ctx, run);
}

async function runClose(ctx, run, userStop, state) {
  const { clock, port } = ctx;
  const startedAt = userStop ? run.stopAt : clock.now();
  const deadline = startedAt + LIMITS.STOP_DEADLINE_MS;
  const left = () => deadline - clock.now();
  const call = (fn) => callWithTimeout(clock, fn, Math.min(LIMITS.CALL_TIMEOUT_MS, left()));
  denyTree(ctx, run);

  const refresh = async () => {
    const r = await call(() => port.listTasks());
    if (r.status === 'ok' && Array.isArray(r.value)) {
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
    // 終わった統括は、一覧に idle で残る。残っていれば取り消す（6.1）。ほかのエージェントも残っていれば、止めると同じ手順を通す。
    const ok = await refresh();
    const active = ok ? activeTreeAgents(run, state.tasks) : null;
    if (active === null) {
      needCancel = true;
      goStopping();
    } else if (active.length === 0) {
      needCancel = false;
    } else if (active.length === 1 && active[0] === run.rootId && state.tasks.find((t) => t.agentId === run.rootId)?.status === 'idle') {
      needCancel = true; // 統括だけが待ちで残っている。表示は変えずに取り消す
    } else {
      needCancel = true;
      goStopping();
    }
  }

  if (needCancel && run.rootId) {
    const c = await call(() => port.cancelAgent(run.rootId));
    logEntry(ctx, run, { t: 'note', at: clock.now(), kind: 'cancel_root', status: c.status });
  }

  if (needCancel && run.tree.size > 0) {
    let knownActive = null; // 最後に一覧が取れたときの、動いているエージェント
    for (;;) {
      const ok = await refresh();
      if (ok) {
        knownActive = activeTreeAgents(run, state.tasks);
        if (knownActive.length === 0) break;
      }
      if (left() <= 0) {
        // 決めた時間のうちに止まったと確かめられなかったエージェントは、止まっていないものとして示す
        state.unstopped = describeUnstopped(run, knownActive ?? unconfirmedAgents(run));
        break;
      }
      await sleep(clock, Math.min(LIMITS.STOP_POLL_MS, Math.max(left(), 0)));
    }
  }
}

// 実行を終了にする。状態を確定させ、課題とまとめを作り、result.json を置く（4.10、6.5）。
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
      rec.status = ts === 'completed' ? 'completed' : ts === 'failed' ? 'failed' : 'stopped';
    }
    syncSlot(run, rec);
  }
  const pending = run.pendingEnd ?? { kind: 'aborted', detail: 'orchestrator_ended' };
  const endReason = { kind: pending.kind, detail: pending.detail ?? null, unstoppable: state.unstopped.length > 0 };
  if (pending.message) endReason.message = pending.message;
  run.unstopped = state.unstopped;
  run.waits = [];

  const normalEnd = endReason.kind === 'all_passed' || endReason.kind === 'max_iterations';
  const drafts = analyzeEntries(run.entries, {
    pluginName: ctx.pluginName,
    runId: run.id,
    endReason,
    endAt: now,
    unstopped: state.unstopped,
    isAuto: run.isAuto,
    models: run.settings.models,
    cause: normalEnd ? run.rootEndCause : null,
  });
  const orchestratorIssues = endReason.kind === 'user_stopped' || endReason.kind === 'aborted' ? [] : run.draftIssues;
  run.issues = [...numberIssues(drafts, 'M', 'mechanical'), ...orchestratorIssues];
  if (!(normalEnd && run.finishRecorded)) {
    run.summary = buildMechanicalSummary({ iterations: run.iterations, phase: run.phaseAtClose ?? null });
  }
  run.endReason = endReason;
  run.status = 'finished';
  run.finishedAt = now;
  logEntry(ctx, run, { t: 'status', at: now, status: 'finished', endReason });
  const snapshot = {
    ...viewRun(ctx, run),
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
