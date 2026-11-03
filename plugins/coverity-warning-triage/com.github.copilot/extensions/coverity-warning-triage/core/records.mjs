import { realpathSync, statSync } from 'node:fs';
import { basename, isAbsolute, relative, resolve } from 'node:path';
import { DISCONNECTED_MESSAGE, LIMITS, STOPPED_MESSAGE } from './constants.mjs';
import { analyzeEntries, formatReviewText, numberIssues } from './analysis.mjs';
import { currentIteration, isActiveAgent } from './run.mjs';
import { logEntry } from './log.mjs';
import { requestClose } from './lifecycle.mjs';
import { isPlainObject } from './util.mjs';

// 記録のツール（6.2）の決まりの確かめ（4.8、6.2）。
// 断るときは RecordRefusal を投げる。message は、ツールを呼んだエージェントに返る平易な日本語。

export class RecordRefusal extends Error {
  constructor(message) {
    super(message);
    this.name = 'RecordRefusal';
  }
}

const refuse = (message) => {
  throw new RecordRefusal(message);
};

const COUNTED_TOOLS = new Set(['plan', 'verdict', 'review', 'issue', 'finish']);
const IMAGE_EXT = /\.(png|jpe?g|gif|webp)$/i;
const isNonEmptyString = (v) => typeof v === 'string' && v.trim() !== '';

function tool(ctx, name) {
  return `${ctx.pluginName}_${name}`;
}

// ---- 計画 ------------------------------------------------------------------------------------

function normalizeText(s) {
  return String(s).replace(/\r\n/g, '\n').trim();
}

function recordPlan(ctx, run, args) {
  if (run.status !== 'running') {
    refuse(
      `繰り返しはもう終わっています。次の計画は記録できません。${tool(ctx, 'review')} で記録の要点を受け取り、${tool(ctx, 'issue')} と ${tool(ctx, 'finish')} でまとめに進んでください。`,
    );
  }
  if (!Number.isInteger(args?.iteration) || args.iteration < 1) refuse('試行の番号は 1 以上の整数で渡してください。');
  if (!Array.isArray(args.criteria) || args.criteria.length === 0) refuse('評価基準の一覧（番号と文）を渡してください。');
  if (!isNonEmptyString(args.plan)) refuse('この試行の計画の文を渡してください。');
  const ids = new Set();
  for (const c of args.criteria) {
    if (!isPlainObject(c) || !isNonEmptyString(c.id) || !isNonEmptyString(c.text)) {
      refuse('評価基準は、番号（id）と文（text）の組で渡してください。');
    }
    if (ids.has(c.id)) refuse(`評価基準の番号 ${c.id} が重なっています。`);
    ids.add(c.id);
  }
  const max = run.settings.maxIterations;
  const last = currentIteration(run);
  const expected = (last ? last.n : 0) + 1;
  if (args.iteration > max) refuse(`繰り返しの上限（${max} 回）を超えます。試行の番号は ${max} までです。`);
  if (args.iteration !== expected) refuse(`試行の番号が違います。次の試行の番号は ${expected} です。`);
  const missing = [];
  const changed = [];
  for (const u of ctx.task.criteria) {
    const found = args.criteria.find((c) => c.id === u.id);
    if (!found) missing.push(u.id);
    else if (normalizeText(found.text) !== normalizeText(u.text)) changed.push(u.id);
  }
  if (missing.length > 0) {
    refuse(`ユーザーと確かめた評価基準が足りません（${missing.join('、')}）。番号と文を変えずに、すべて入れてください。`);
  }
  if (changed.length > 0) {
    refuse(`ユーザーと確かめた評価基準の文が変わっています（${changed.join('、')}）。文を変えずに入れてください。`);
  }
  const userIds = new Set(ctx.task.criteria.map((c) => c.id));
  run.iterations.push({
    n: args.iteration,
    criteria: args.criteria.map((c) => ({ id: c.id, text: c.text, fromUser: userIds.has(c.id) })),
    plan: args.plan,
    verdicts: null,
    agentIds: [],
    workerIds: [],
  });
  return `試行 ${args.iteration} の計画を記録しました。次は、作業エージェントに頼んでください。`;
}

// ---- 合否 ------------------------------------------------------------------------------------

function recordVerdict(ctx, run, args, agentId) {
  const rec = run.tree.get(agentId);
  if (rec?.role !== 'verifier') refuse('合否の記録は、検証エージェントだけが行えます。');
  if (run.status !== 'running') refuse('繰り返しはもう終わっています。合否は記録できません。');
  const it = currentIteration(run);
  if (!it) refuse('まだ計画が記録されていません。');
  if (!Number.isInteger(args?.iteration)) refuse('試行の番号を整数で渡してください。');
  if (args.iteration !== it.n) refuse(`合否を記録できる試行は ${it.n} 回目です。`);
  if (it.verdicts) refuse(`試行 ${it.n} の合否は、すでに記録されています。`);
  const workers = it.workerIds.map((id) => run.tree.get(id)).filter(Boolean);
  if (workers.length === 0 || workers.some(isActiveAgent)) {
    refuse('この試行の作業エージェントが、まだ終わっていません。作業が終わってから合否を記録してください。');
  }
  if (!Array.isArray(args.verdicts) || args.verdicts.length === 0) refuse('評価基準ごとの合否（verdicts）を渡してください。');
  const wanted = new Set(it.criteria.map((c) => c.id));
  const seen = new Set();
  for (const v of args.verdicts) {
    if (!isPlainObject(v) || !isNonEmptyString(v.criterionId) || typeof v.pass !== 'boolean') {
      refuse('合否は、評価基準の番号（criterionId）と、合格かどうか（pass。true か false）の組で渡してください。');
    }
    if (!wanted.has(v.criterionId)) refuse(`この試行の評価基準にない番号です：${v.criterionId}`);
    if (seen.has(v.criterionId)) refuse(`評価基準の番号 ${v.criterionId} が重なっています。`);
    seen.add(v.criterionId);
  }
  const lacking = [...wanted].filter((id) => !seen.has(id));
  if (lacking.length > 0) refuse(`合否が足りない評価基準があります（${lacking.join('、')}）。すべての評価基準について記録してください。`);
  it.verdicts = args.verdicts.map((v) => ({ criterionId: v.criterionId, pass: v.pass, note: typeof v.note === 'string' ? v.note : '' }));
  const passed = it.verdicts.filter((v) => v.pass).length;
  if (passed === it.verdicts.length) run.outcome = 'all_passed';
  else if (it.n >= run.settings.maxIterations) run.outcome = 'max_iterations';
  if (run.outcome) run.status = 'summarizing'; // まとめ中に移るのは、すべて合格か、上限の試行の合否が記録されたとき
  return `試行 ${it.n} の合否を記録しました（${it.verdicts.length} 件中 ${passed} 件が合格）。結果を統括エージェントに返してください。`;
}

// ---- 表示 ------------------------------------------------------------------------------------

const isPrimitive = (v) => v === null || ['string', 'number', 'boolean'].includes(typeof v);

function resolveInsideCwd(ctx, p) {
  let real;
  let root;
  try {
    real = realpathSync(resolve(ctx.cwd, p));
  } catch {
    refuse(`ファイルが見つかりません：${p}`);
  }
  try {
    root = realpathSync(ctx.cwd);
  } catch {
    refuse('作業フォルダが見つかりません。');
  }
  const rel = relative(root, real);
  if (rel === '' || rel.startsWith('..') || isAbsolute(rel)) refuse('作業フォルダの外のファイルは、画面に出せません。');
  try {
    if (!statSync(real).isFile()) refuse(`ファイルではありません：${p}`);
  } catch (e) {
    if (e instanceof RecordRefusal) throw e;
    refuse(`ファイルが見つかりません：${p}`);
  }
  return real;
}

// 画像やファイルの場所が、作業フォルダの中にあるか（シンボリックリンクをたどった本当の場所で）を確かめる。
// 画面の窓口が返すときにも、もう一度使う。ふつうの値か null。
export function checkFileInsideCwd(ctx, p) {
  try {
    return resolveInsideCwd(ctx, p);
  } catch {
    return null;
  }
}

function registerFile(run, real) {
  for (const [id, f] of run.files) if (f.path === real) return id;
  const id = `f${run.files.size + 1}`;
  run.files.set(id, { path: real, name: basename(real) });
  return id;
}

function normalizeDisplay(ctx, run, def, value) {
  const bad = (hint) => refuse(`表示「${def.key}」（種類：${def.type}）の値の形が合いません。${hint}`);
  switch (def.type) {
    case 'text':
      if (typeof value !== 'string') bad('文字列で渡してください。');
      return value;
    case 'kv': {
      if (Array.isArray(value)) {
        if (!value.every((x) => isPlainObject(x) && typeof x.label === 'string' && isPrimitive(x.value))) {
          bad('{ 見出し: 値 } の形で渡してください。');
        }
        return value.map((x) => ({ label: x.label, value: x.value }));
      }
      if (!isPlainObject(value) || !Object.values(value).every(isPrimitive)) bad('{ 見出し: 値 } の形で渡してください。');
      return { ...value };
    }
    case 'table': {
      if (!isPlainObject(value) || !Array.isArray(value.columns) || !Array.isArray(value.rows)) {
        bad('{ columns: [見出し], rows: [[値]] } の形で渡してください。');
      }
      if (value.columns.length === 0 || !value.columns.every((c) => typeof c === 'string')) bad('columns は見出しの文字列の一覧にしてください。');
      for (const row of value.rows) {
        if (!Array.isArray(row) || row.length !== value.columns.length || !row.every(isPrimitive)) {
          bad('rows の各行は、columns と同じ数の値の一覧にしてください。');
        }
      }
      return { columns: [...value.columns], rows: value.rows.map((r) => [...r]) };
    }
    case 'progress': {
      if (typeof value === 'number') {
        if (!(value >= 0 && value <= 100)) bad('数で渡すときは 0 から 100 にしてください。');
        return value;
      }
      if (!isPlainObject(value) || typeof value.value !== 'number' || typeof value.max !== 'number') {
        bad('0 から 100 の数か、{ value, max, label? } の形で渡してください。');
      }
      // max が 0（数える物がまだない、または0件）も受け付ける。そのとき value は 0 だけ
      if (!Number.isFinite(value.max) || value.max < 0) bad('max は 0 以上の数にしてください。');
      if (!(value.value >= 0 && value.value <= value.max)) bad('value は 0 以上 max 以下にしてください。');
      const out = { value: value.value, max: value.max };
      if (typeof value.label === 'string') out.label = value.label;
      return out;
    }
    case 'image':
    case 'file': {
      if (!isNonEmptyString(value)) bad('作業フォルダの中の場所を、文字列で渡してください。');
      const real = resolveInsideCwd(ctx, value);
      return { fileId: registerFile(run, real), name: basename(real), image: def.type === 'image' && IMAGE_EXT.test(real) };
    }
    default:
      return refuse(`表示「${def.key}」の種類（${def.type}）には、まだ値を送れません。`);
  }
}

function recordShow(ctx, run, args, agentId) {
  const rec = run.tree.get(agentId);
  if (rec?.role !== 'orchestrator' && rec?.role !== 'worker') refuse('表示の記録は、統括エージェントか作業エージェントだけが行えます。');
  const def = ctx.task.displays.find((d) => d.key === args?.key);
  if (!def) {
    const keys = ctx.task.displays.map((d) => d.key);
    refuse(`作業ごとの指示にない表示です：${args?.key}。使える表示の名前（key）：${keys.length > 0 ? keys.join('、') : '（なし）'}`);
  }
  run.displays[def.key] = normalizeDisplay(ctx, run, def, args.value);
  return `表示「${def.key}」を記録しました。`;
}

// ---- 振り返りとまとめ ---------------------------------------------------------------------------

function requireSummarizing(ctx, run, name) {
  if (run.status !== 'summarizing') {
    refuse(
      `まだ繰り返しの途中です。${tool(ctx, name)} は、評価基準をすべて満たすか、上限の回数まで試行した後に呼んでください。`,
    );
  }
}

function mechanicalDrafts(ctx, run) {
  return analyzeEntries(run.entries, {
    pluginName: ctx.pluginName,
    runId: run.id,
    endReason: null, // 終わる前の途中経過なので、終わったときだけ分かる課題は含めない
    endAt: ctx.clock.now(),
    unstopped: [],
    isAuto: run.isAuto,
    models: run.settings.models,
  });
}

function recordReview(ctx, run) {
  requireSummarizing(ctx, run, 'review');
  const issues = numberIssues(mechanicalDrafts(ctx, run), 'M', 'mechanical');
  return formatReviewText({
    pluginName: ctx.pluginName,
    issues,
    iterations: run.iterations,
    finishRecorded: run.finishRecorded,
  });
}

function recordIssue(ctx, run, args) {
  requireSummarizing(ctx, run, 'issue');
  if (!isNonEmptyString(args?.title)) refuse('Issue の題（title）を渡してください。');
  if (!isNonEmptyString(args?.body)) refuse('Issue の本文（body）を渡してください。');
  const id = `O${run.draftIssues.length + 1}`;
  run.draftIssues.push({ id, title: args.title.trim(), body: args.body.trim(), source: 'orchestrator' });
  return `Issue の案を足しました（${id}）。起票は、ユーザーが画面で承認したときだけ行われます。`;
}

function recordFinish(ctx, run, args) {
  requireSummarizing(ctx, run, 'finish');
  if (run.finishRecorded) refuse('実行全体のまとめは、すでに記録されています。');
  if (!isNonEmptyString(args?.summary)) refuse('実行全体のまとめ（summary）を渡してください。');
  run.summary = args.summary.trim();
  run.finishRecorded = true;
  return '実行全体のまとめを記録しました。作業を終えてください。';
}

// ---- 入口 -----------------------------------------------------------------------------------

const HANDLERS = {
  plan: { role: 'orchestrator', run: recordPlan },
  verdict: { role: 'verifier', run: recordVerdict },
  show: { role: null, run: recordShow },
  review: { role: 'orchestrator', run: recordReview },
  issue: { role: 'orchestrator', run: recordIssue },
  finish: { role: 'orchestrator', run: recordFinish },
};

export function isRecordTool(shortName) {
  return Object.hasOwn(HANDLERS, shortName);
}

const ROLE_NAME = { orchestrator: '統括エージェント', verifier: '検証エージェント' };

// 列に入れる前の、すぐ断る確かめ（停止中や終了の後。6.1）。通るなら null。
export function refuseWithoutQueue(ctx) {
  const run = ctx.run;
  if (!run || run.status === 'finished') return DISCONNECTED_MESSAGE;
  if (run.status === 'stopping' || run.closeRequested || run.closing) return STOPPED_MESSAGE;
  return null;
}

// 順番待ちの列の中で動く。成功なら返す文、断るなら RecordRefusal を投げる。
export function applyRecord(ctx, shortName, args, agentId) {
  const run = ctx.run;
  if (!run || run.status === 'finished') throw new RecordRefusal(DISCONNECTED_MESSAGE);
  if (run.status === 'stopping' || run.closeRequested || run.closing) throw new RecordRefusal(STOPPED_MESSAGE);
  if (run.status === 'starting' || agentId == null || !run.tree.has(agentId)) throw new RecordRefusal(DISCONNECTED_MESSAGE);
  const handler = HANDLERS[shortName];
  if (!handler) throw new RecordRefusal('知らない記録のツールです。');
  const counted = COUNTED_TOOLS.has(shortName);
  try {
    if (handler.role && run.tree.get(agentId)?.role !== handler.role) {
      refuse(`この記録は、${ROLE_NAME[handler.role]}だけが行えます。`);
    }
    const text = handler.run(ctx, run, args ?? {}, agentId);
    logEntry(ctx, run, { t: 'record', at: ctx.clock.now(), tool: shortName, agentId, args: args ?? {} });
    run.refusals[shortName] = 0;
    return text;
  } catch (e) {
    if (!(e instanceof RecordRefusal)) throw e;
    logEntry(ctx, run, { t: 'refused', at: ctx.clock.now(), tool: shortName, agentId, reason: e.message });
    if (!counted) throw e;
    run.refusals[shortName] = (run.refusals[shortName] ?? 0) + 1;
    if (run.refusals[shortName] > LIMITS.RETRY_LIMIT) {
      exceedRetryLimit(ctx, run);
      throw new RecordRefusal(
        `記録のやり直しが上限（${LIMITS.RETRY_LIMIT} 回）を超えたため、この実行を終えます。作業を終えてください。`,
      );
    }
    const left = LIMITS.RETRY_LIMIT - run.refusals[shortName] + 1;
    throw new RecordRefusal(`${e.message}（やり直しは、あと ${left} 回までです）`);
  }
}

// 記録のやり直しが上限を超えた（4.8）。統括を取り消して、実行を終える。
function exceedRetryLimit(ctx, run) {
  logEntry(ctx, run, { t: 'note', at: ctx.clock.now(), kind: 'retry_limit' });
  if (run.status === 'summarizing') run.pendingEnd = { kind: run.outcome, detail: null };
  else run.pendingEnd = { kind: 'aborted', detail: 'retry_limit' };
  requestClose(ctx, run, { userStop: false });
}
