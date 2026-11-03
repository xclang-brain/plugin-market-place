// 2段目 部品の試験（Web管理画面を、このプラグインの本物の作業の定義で）。
//
// 試験の対象：com.github.copilot/extensions/coverity-warning-triage/ の本物のつなぎ口（adapter.mjs）・中心の処理（core/）・
// 画面（ui/）・作業の定義（task.json）。Copilot CLI の代わりに、偽物の SDK（tests/stage2/adapter/fake-sdk.mjs）を使う。
// 統括・作業・検証のエージェントの代わりに、試験が決まった手順で記録のツール（coverity-warning-triage_plan・_show・
// _verdict など）を呼ぶ。表示の値は、作業役の定義（com.github.copilot/agents/worker.agent.md の「表示」の節）のとおりに作る。
//
// 「実際の流れ」の組は、本物の ct.py（uv run --native-tls ct.py）・MCP サーバー（uv run ... coverity-triage-mcp を stdio で）・
// Coverity Connect のエミュレーター・題材の登録（seed.py）・模擬の解析コマンドを、tests/stage2/task/e2e_flow_support.py で動かす
// （小さな Python の道具を一時のフォルダに書いて、子のプロセスとして動かす。標準のライブラリだけを使うので、依存のない Python でもよい）。
// 画面の設定から作った統括への指示文を、作業役の決まりどおりに ct.py の引数にして実行し、その途中と終わりの本物の値を画面に送る。
// 道具（uv、git、svn、make、gcc、g++、clang、cppcheck）が足りないときは、その組だけを飛ばす。
//
// 動かし方：python3 tests/run.py（ほかの試験と一緒に動く）。直接は node --test tests/stage2/task/web-ui.test.mjs。
//   Playwright（tests/node_modules）とブラウザー（PLUGIN_TEST_BROWSER_CHANNEL の Chrome か Edge、なければ Playwright の Chromium）が要る。
//   環境変数 CWT_CAPTURE_DIR があると、画面の写真（png、deviceScaleFactor 2）をそこに撮る（名前は webui- で始まる）。
// プラグインの中には何も作らない（記録は一時の COPILOT_HOME、Python は __pycache__ を書かない）。一時のフォルダは試験の終わりに消す。
// 不具合を見つけた試験は、正しい振る舞いを期待して書いてある（不合格のまま残す）。
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { after, before, describe, test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { createFakeSdk, sdkEvent } from '../adapter/fake-sdk.mjs';
import { contrastProblems, layoutProblems } from '../ui/page-checks.mjs';

// ---- 場所と、本物の作業の定義 -------------------------------------------------------------------------

const HERE = path.dirname(fileURLToPath(import.meta.url)); // tests/stage2/task
const PLUGIN_ROOT = path.resolve(HERE, '..', '..', '..');
const NAME = 'coverity-warning-triage';
const EXTENSION_DIR = path.join(PLUGIN_ROOT, 'com.github.copilot', 'extensions', NAME);
const TASK = JSON.parse(fs.readFileSync(path.join(EXTENSION_DIR, 'task.json'), 'utf8'));
const CONSTANTS = await import(pathToFileURL(path.join(EXTENSION_DIR, 'core', 'constants.mjs')).href);
const SHOW = `${NAME}_show`;
const REQUEST = 'Coverity の警告をトリアージしてください';
const CAPTURE = process.env.CWT_CAPTURE_DIR || '';

// Copilot CLI が返すモデルの一覧（エージェントの定義の model に書かれた名前を含む）
const MODELS = [
  { id: 'claude-sonnet-5.5', name: 'Claude Sonnet 5.5' },
  { id: 'gpt-6.1-sol', name: 'GPT-6.1 Sol' },
  { id: 'gpt-6-luna', name: 'GPT-6 Luna' },
  { id: 'claude-haiku-5.5', name: 'Claude Haiku 5.5' },
];

const RUNNING_KEYS = TASK.displays.filter((d) => d.when === 'running').map((d) => d.key);
const FINISHED_KEYS = TASK.displays.filter((d) => d.when === 'finished').map((d) => d.key);
const LABEL = Object.fromEntries(TASK.displays.map((d) => [d.key, d.label]));

// ---- 一時のフォルダと、試験のプロセスだけの環境 ----------------------------------------------------------

const TEMP = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cwt-webui-')));
process.setMaxListeners(Math.max(process.getMaxListeners(), 200)); // つなぎ口を何度も起動する（終了の合図の受け口が増える）
// 起票できるかの確かめで、本物の gh（ネットワークと利用者のログイン）を使わない：いつも失敗する偽物の gh を先に置く
if (process.platform !== 'win32') {
  const bin = path.join(TEMP, 'bin');
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, 'gh'), '#!/bin/sh\necho "gh: 試験の偽物です" >&2\nexit 1\n');
  fs.chmodSync(path.join(bin, 'gh'), 0o755);
  process.env.PATH = `${bin}${path.delimiter}${process.env.PATH ?? ''}`;
}
const cleanups = [];
after(async () => {
  while (cleanups.length > 0) {
    try {
      await cleanups.pop()();
    } catch {
      // 片づけの失敗は無視する
    }
  }
  fs.rmSync(TEMP, { recursive: true, force: true });
});

// ---- Playwright とブラウザー（screen.test.mjs と同じ探し方） -------------------------------------------------

async function loadPlaywright() {
  try {
    const module = await import('playwright');
    const chromium = module.chromium ?? module.default?.chromium;
    if (chromium) return chromium;
  } catch {
    // 次の探し方へ
  }
  const require = createRequire(import.meta.url);
  for (const dir of (process.env.NODE_PATH ?? '').split(path.delimiter).filter(Boolean)) {
    try {
      const module = require(path.join(dir, 'playwright'));
      if (module.chromium) return module.chromium;
    } catch {
      // 次の場所へ
    }
  }
  throw new Error('Playwright（ライブラリ）が見つかりません。プラグインのフォルダで npm install --prefix tests --no-save --no-package-lock playwright@1.56.1 を実行してください。');
}

const chromium = await loadPlaywright();
let browser;
before(async () => {
  const channel = process.env.PLUGIN_TEST_BROWSER_CHANNEL;
  if (channel && channel !== 'chromium') {
    try {
      browser = await chromium.launch({ channel });
      return;
    } catch {
      // Playwright の Chromium を使う
    }
  }
  browser = await chromium.launch();
});
after(async () => {
  await browser?.close();
});

// ---- つなぎ口（本物）と偽物の SDK ---------------------------------------------------------------------------

const { startAdapter } = await import(pathToFileURL(path.join(EXTENSION_DIR, 'adapter.mjs')).href);
let sequence = 0;

/** 本物のつなぎ口を、偽物の SDK で起動し、画面を開くツールを呼ぶ。cwd は Copilot CLI の作業フォルダ（利用者のリポジトリ）。 */
async function startPlugin({ cwd } = {}) {
  sequence += 1;
  const work = cwd ?? fs.realpathSync(fs.mkdtempSync(path.join(TEMP, 'work-')));
  process.env.COPILOT_HOME = fs.mkdtempSync(path.join(TEMP, 'copilot-home-')); // 実行ごとに記録の置き場所を分ける
  const fake = createFakeSdk({ cwd: work, sessionId: `webui-${sequence}` });
  fake.models = MODELS.map((m) => ({ ...m }));
  fake.currentModel = { modelId: 'claude-sonnet-5.5' };
  const handle = await startAdapter({ joinSession: fake.joinSession });
  cleanups.push(() => handle.shutdown());
  const opened = await fake.callTool(`${NAME}_open`, { requestText: REQUEST });
  assert.equal(opened.ok, true, opened.error);
  const url = opened.text.match(/http:\/\/[^\s）]+/)[0];
  return { fake, handle, core: handle.core, url, work, workerId: null, verifierId: null, rootId: null };
}

const orchestratorType = `${NAME}:orchestrator`;

/** 開始ボタンで起動された統括の開始のイベントを流す。統括の task（偽物の SDK の一覧の1件）を返す。 */
async function beginRoot(p) {
  await eventually(() => p.fake.tasks.some((t) => t.agentType === orchestratorType && !t.handled), '統括が起動されない');
  const root = p.fake.tasks.find((t) => t.agentType === orchestratorType && !t.handled);
  root.handled = true;
  p.fake.emit(sdkEvent.subagentStarted(root.id, { agentType: orchestratorType, agentDisplayName: root.displayName, agentDescription: '統括', executionMode: 'background', model: root.model }));
  await p.core.whenIdle();
  p.rootId = root.id;
  return root;
}

/** 統括の下のエージェントを起こす（実機の順：統括の task の開始 → サブエージェントの開始）。 */
async function startChild(p, { agentId, agent, model, description, parentAgentId = p.rootId }) {
  const agentType = `${NAME}:${agent}`;
  const toolCallId = `tc-task-${agentId}`;
  p.fake.spawn({ agentId, parentAgentId, agentType, executionMode: 'sync' });
  p.fake.emit(sdkEvent.toolStart(parentAgentId, toolCallId, 'task', { description, agent_type: agentType, prompt: '頼む内容', mode: 'sync' }));
  p.fake.emit(sdkEvent.subagentStarted(agentId, { agentType, agentName: agentType, agentDisplayName: agent, agentDescription: `${agent} の定義`, toolCallId, executionMode: 'sync', parentId: parentAgentId, model }));
  await p.core.whenIdle();
}

/** エージェントが手番を終える（最後の返事 → 一覧の状態 → 終わりのイベント）。 */
async function endTurn(p, agentId, reply, { status = 'completed' } = {}) {
  await p.fake.callHook('onSubagentStop', { agentId, response: reply });
  p.fake.task(agentId).status = status;
  p.fake.emit(sdkEvent.subagentCompleted(agentId));
  if (status === 'idle') p.fake.emit(sdkEvent.agentIdle(agentId));
  await p.core.whenIdle();
}

const record = (p, short, args, agentId) => p.fake.callTool(`${NAME}_${short}`, args, { agentId });
/** 作業役（またはほかのエージェント）として、coverity-warning-triage_show で表示に値を送る。{ ok, text } か { ok: false, error } */
const show = (p, key, value, agentId = p.workerId) => p.fake.callTool(SHOW, { key, value }, { agentId });

const planArgs = () => ({ iteration: 1, criteria: TASK.criteria.map(({ id, text }) => ({ id, text })), plan: '設定の値で ct.py の手順（new-run → tick → summary → gui）を進め、結果を確かめる。' });

/** 計画を記録し、作業役（トリアージ役）を起こす。 */
async function toWorker(p, id = 'ag-worker') {
  const planned = await record(p, 'plan', planArgs(), p.rootId);
  assert.equal(planned.ok, true, planned.error);
  await startChild(p, { agentId: id, agent: 'worker', model: 'gpt-6-luna', description: '設定の値で Coverity の警告をトリアージする' });
  p.workerId = id;
}

/** 作業役が返事をして終わり、検証役を起こす。 */
async function toVerifier(p, runDir = '（実行フォルダ）', id = 'ag-verifier') {
  await endTurn(p, p.workerId, `成果物：${runDir}\nしたこと：設定の値で ct.py new-run から summary までを進めた。\nできなかったこと：なし`);
  await startChild(p, { agentId: id, agent: 'verifier', model: 'claude-sonnet-5.5', description: '成果物を合格の条件で確かめる' });
  p.verifierId = id;
}

/** 検証が全部合格を記録し、統括がまとめて終わる。 */
async function toFinished(p) {
  const verdict = await record(p, 'verdict', { iteration: 1, verdicts: TASK.criteria.map((c) => ({ criterionId: c.id, pass: true, note: '' })) }, p.verifierId);
  assert.equal(verdict.ok, true, verdict.error);
  await endTurn(p, p.verifierId, 'すべての基準を確かめた。');
  for (const [short, args] of [['review', {}], ['finish', { summary: '1回の試行で、すべての基準を満たしました。' }]]) {
    const r = await record(p, short, args, p.rootId);
    assert.equal(r.ok, true, r.error);
  }
  await endTurn(p, p.rootId, '終わりました。', { status: 'idle' });
  await eventually(() => p.core.getState().run?.status === 'finished', '実行が終わらない', 15000);
}

// ---- 画面 ----------------------------------------------------------------------------------------------

async function openPage(url, { width = 1280, height = 900 } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: CAPTURE ? 2 : 1, locale: 'ja-JP' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  await page.goto(url);
  await page.waitForFunction(() => !document.getElementById('loading'), null, { timeout: 8000 });
  return { page, context, errors };
}

/** つなぎ口を起動して画面を開き、fn に渡す。終わったら片付ける。allowErrors：出てよい画面のエラーの形（正規表現の配列） */
async function withScreen(options, fn) {
  const { cwd, viewport, allowErrors = [] } = options;
  const p = await startPlugin({ cwd });
  const { page, context, errors } = await openPage(p.url, viewport);
  try {
    const result = await fn({ p, page, context });
    assert.deepEqual(errors.filter((m) => !allowErrors.some((re) => re.test(m))), [], '画面にエラーが出た');
    return result;
  } finally {
    await context.close();
    await p.handle.shutdown();
  }
}

async function shot(page, name, { fullPage = true } = {}) {
  if (!CAPTURE) return;
  fs.mkdirSync(CAPTURE, { recursive: true });
  await page.screenshot({ path: path.join(CAPTURE, `webui-${name}.png`), fullPage });
}

async function eventually(check, message, timeout = 15000) {
  const startedAt = Date.now();
  for (;;) {
    if (await check()) return;
    if (Date.now() - startedAt > timeout) assert.fail(message);
    await new Promise((resolve) => setTimeout(resolve, 40));
  }
}

const until = (page, fn, arg, timeout = 15000) => page.waitForFunction(fn, arg, { timeout });
const displayText = (page, key) => page.locator(`.display[data-key="${key}"]`).innerText();
const linksOf = (page, key) => page.locator(`.display[data-key="${key}"] a`).evaluateAll((els) => els.map((a) => a.getAttribute('href')));
const shownKeys = (page) => page.locator('#s-displays .display').evaluateAll((els) => els.map((el) => el.dataset.key));
const tableOf = (page, key) =>
  page.locator(`.display[data-key="${key}"]`).evaluate((el) => ({
    head: [...el.querySelectorAll('thead th')].map((th) => th.textContent),
    rows: [...el.querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent)),
  }));
/** 画面の表示が、送った値に変わるまで待つ（1秒ごとに取り直す画面なので） */
const waitDisplay = (page, key, text, timeout = 15000) => until(page, ([k, t]) => document.querySelector(`.display[data-key="${k}"]`)?.innerText.includes(t), [key, text], timeout);
const waitPhase = (page, phase) => until(page, (ph) => document.querySelector('#phases [data-active="true"]')?.dataset.phase === ph, phase);
const waitStatus = (page, text) => until(page, (t) => document.getElementById('run-status')?.textContent.includes(t), text, 15000);

/** 開始を押した結果（統括の起動か、開始できない理由の表示）が出るまで待つ */
async function startOutcome(p, page) {
  await eventually(async () => p.fake.callsOf('tasks.startAgent').length > 0 || (await page.locator('#start-message').isVisible()), '開始を押しても、何も起きない');
  await page.waitForTimeout(300);
}

async function fillSettings(page, { condition, impact, maxItems, initial } = {}) {
  if (condition !== undefined) await page.fill('#setting-condition_file', condition);
  if (impact !== undefined) await page.selectOption('#setting-impact', impact);
  if (maxItems !== undefined) await page.fill('#setting-max_items', maxItems);
  if (initial !== undefined) await page.selectOption('#setting-initial_analysis', initial);
}

/** 統括への指示文の「作業ごとの設定の値」を { key: 値の文字 } にする */
function settingValues(prompt) {
  const block = (prompt.split('## 作業ごとの設定の値\n')[1] ?? '').split('\n\n')[0];
  return Object.fromEntries(
    block
      .split('\n')
      .map((line) => /^- (.+?)（(\w+)）：(.*)$/.exec(line))
      .filter(Boolean)
      .map((m) => [m[2], m[3]]),
  );
}

/** 作業役の定義（worker.agent.md「設定の値から引数への置き換え」）のとおりに、設定の値を ct.py new-run の引数にする */
function argsFromSettings(values) {
  const args = [];
  if (values.condition_file) args.push('--filter', values.condition_file);
  if (values.impact && values.impact !== '絞らない') args.push('--impact', values.impact);
  if (values.max_items && values.max_items !== '（未入力）') args.push('--limit', values.max_items);
  if (values.initial_analysis === 'する') args.push('--initial');
  if (values.initial_analysis === 'しない') args.push('--no-initial');
  return args;
}

// ---- 作業役の指示（worker.agent.md「表示」の節）のとおりの値 -----------------------------------------------------

const WORKER_COLUMNS = ['作業', '段', '経過時間', '状態'];
const ERROR_COLUMNS = ['作業', '種類', '内容'];
const RESULT_COLUMNS = ['承認', 'ID', '推奨', '確信度', '見立て', '詳細'];
const UNPROCESSED_COLUMNS = ['作業', '理由'];
const COUNT_LABELS = ['警告', '作業', '修正の推奨', '逸脱の推奨', '確信度 低', 'エラー', '未処理', '承認 修正', '承認 逸脱', '承認 却下', '承認 （空）', '推奨から変えたもの'];
const STATUS_JA = { pending: '未処理', in_progress: '処理中', done: '終わった', error: 'エラー', merged: 'まとめた' };
const INITIAL_STEPS = ['setup', 'build', 'analyze', 'commit']; // ctlib/initial.py の STEPS の順
const DECISION_JA = { fix: '修正', deviation: '逸脱', reject: '却下' };
const CONFIDENCE_JA = { low: '低', medium: '中', high: '高' };
const CONFIDENCE_ORDER = { low: 0, medium: 1, high: 2 };
const JUDGEMENT_JA = { true_bug: '本物の不具合', false_positive: '誤検知', intentional: '意図的', undetermined: '判定できない' };

const elapsedText = (seconds) => {
  const s = Math.max(0, Math.round(seconds));
  return s >= 60 ? `${Math.floor(s / 60)}分${s % 60}秒` : `${s}秒`;
};

/** 作業役の指示：recent_activity は「ct.py status の最近の動きを、新しいものから5行まで」 */
function recentFromStatus(status) {
  const list = status?.recent_activity ?? status?.recent ?? status?.activity ?? [];
  return (Array.isArray(list) ? list : []).slice(0, 5).map((x) => (typeof x === 'string' ? x : (x?.text ?? JSON.stringify(x))));
}

/** ct.py tick のたびに読み直した実行の様子（本物の ct.py status・initial-status・run.json・progress.json）から、実行中の5つの表示の値を作る */
function runningValues(snap, { initial }) {
  const workerRows = (snap.items ?? []).map((item) => {
    const running = item.status === 'in_progress' && item.started;
    const seconds = running ? snap.now - item.started : item.seconds;
    const state = running && snap.now - item.started >= 600 ? '⚠ 停止?' : (STATUS_JA[item.status] ?? item.status);
    return [item.id, snap.progress?.[item.id]?.phase ?? '-', seconds == null ? '-' : elapsedText(seconds), state];
  });
  const errorRows = (snap.status?.errors ?? []).map((e) => [e.item, '処理できなかった作業', e.reason]);
  if (snap.initial?.status === 'failed') errorRows.unshift(['初回解析', '初回解析の失敗', snap.initial.error ?? '']);
  let initialProgress = { value: 0, max: 100, label: '初回解析はしない' };
  if (initial) {
    const st = snap.initial ?? {};
    if (st.status === 'done') initialProgress = { value: INITIAL_STEPS.length, max: INITIAL_STEPS.length, label: '登録まで終わった' };
    else if (st.status === 'running' || st.status === 'failed') initialProgress = { value: Math.max(0, INITIAL_STEPS.indexOf(st.step)), max: INITIAL_STEPS.length, label: st.step_label };
    else initialProgress = { value: 0, max: INITIAL_STEPS.length, label: '始まる前' };
  }
  const counts = snap.status?.counts ?? snap.tick?.counts ?? {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const finished = (counts.done ?? 0) + (counts.error ?? 0);
  return {
    worker_status: { columns: WORKER_COLUMNS, rows: workerRows },
    errors: { columns: ERROR_COLUMNS, rows: errorRows },
    recent_activity: recentFromStatus(snap.status).join('\n'),
    initial_analysis_progress: initialProgress,
    overall_progress: { value: finished, max: total, label: `${finished} / ${total}` },
  };
}

/** ct.py summary の結果と実行フォルダのファイルから、終わった後の6つの表示の値を作る（detail_reports と patches はファイルの場所） */
function finishedValues(fin) {
  const items = (fin.items ?? []).filter((i) => i.status !== 'merged');
  const checkers = new Map((fin.issues ?? []).map((i) => [i.cid, i.checker]));
  const result = (item) => fin.results?.[item.id];
  const done = items.filter((i) => i.status === 'done' && result(i));
  const order = [...done].sort((a, b) => CONFIDENCE_ORDER[result(a).confidence] - CONFIDENCE_ORDER[result(b).confidence] || a.id.localeCompare(b.id));
  const decisionOf = (id) => fin.approvals?.items?.[id]?.decision ?? null;
  const rows = order.map((item) => {
    const r = result(item);
    const id = item.id.startsWith('G') ? `${item.id}（${item.cids.length} 件）` : item.id;
    const checker = [...new Set(item.cids.map((c) => checkers.get(c)).filter(Boolean))].join('、');
    return [DECISION_JA[decisionOf(item.id)] ?? '（空）', `${id}（${checker}）`, r.recommendation === 'fix' ? '修正' : '逸脱', CONFIDENCE_JA[r.confidence], `${r.verdict.summary}（${JUDGEMENT_JA[r.verdict.judgement]}）`, `cid/${item.id}.html`];
  });
  const errors = items.filter((i) => i.status === 'error');
  const pending = items.filter((i) => i.status === 'pending' || i.status === 'in_progress');
  const decisions = items.map((i) => decisionOf(i.id));
  const counts = {
    警告: items.reduce((n, i) => n + i.cids.length, 0),
    作業: items.length,
    修正の推奨: done.filter((i) => result(i).recommendation === 'fix').length,
    逸脱の推奨: done.filter((i) => result(i).recommendation === 'deviation').length,
    '確信度 低': done.filter((i) => result(i).confidence === 'low').length,
    エラー: errors.length,
    未処理: pending.length,
    '承認 修正': decisions.filter((d) => d === 'fix').length,
    '承認 逸脱': decisions.filter((d) => d === 'deviation').length,
    '承認 却下': decisions.filter((d) => d === 'reject').length,
    '承認 （空）': decisions.filter((d) => d == null).length,
    推奨から変えたもの: done.filter((i) => decisionOf(i.id) && decisionOf(i.id) !== result(i).recommendation).length,
  };
  const values = {
    counts,
    results: { columns: RESULT_COLUMNS, rows },
    unprocessed: { columns: UNPROCESSED_COLUMNS, rows: [...errors.map((i) => [i.id, i.error || 'エラー']), ...pending.map((i) => [i.id, '未処理'])] },
    approval_links: `結果の画面：${fin.gui?.url ?? ''}\n承認の選択と逸脱コメントの手直しは、この画面の「結果」で行い、/coverity-apply で反映する。`,
  };
  const first = order[0];
  if (first) {
    values.detail_reports = path.join(fin.run_dir, 'cid', `${first.id}.html`);
    const patch = path.join(fin.run_dir, 'patches', `${first.id}-fix.patch`);
    if ((fin.files ?? []).includes(patch)) values.patches = patch;
  }
  return values;
}

// 題材（envmon）の5件を処理している途中の、作業役の指示どおりの値（本物の実行で出た ID・チェッカー・見立て）
const SAMPLE_RUNNING = {
  worker_status: {
    columns: WORKER_COLUMNS,
    rows: [
      ['10014', 'result', '1分12秒', '終わった'],
      ['10017', 'read', '12分40秒', '⚠ 停止?'],
      ['10027', 'read', '35秒', '処理中'],
      ['10041', '-', '-', '未処理'],
      ['10043', '-', '-', '未処理'],
    ],
  },
  errors: { columns: ERROR_COLUMNS, rows: [['10041', '処理できなかった作業', '結果ファイルの形が違います（verdict.evidence がありません）']] },
  recent_activity: ['17:34:28 10027 の作業エージェントを起動しました', '17:34:27 10017 の警告経路を読んでいます', '17:34:26 10014 の結果を確かめ、差分を作りました', '17:34:24 警告経路を 5 件保存しました', '17:34:23 警告が 5 件見つかりました（上限の件数で止めました）'].join('\n'),
  initial_analysis_progress: { value: 2, max: 4, label: '解析（cov-analyze）' },
  overall_progress: { value: 1, max: 5, label: '1 / 5' },
};
const SAMPLE_FINISHED = {
  counts: { 警告: 5, 作業: 5, 修正の推奨: 3, 逸脱の推奨: 2, '確信度 低': 0, エラー: 0, 未処理: 0, '承認 修正': 3, '承認 逸脱': 2, '承認 却下': 0, '承認 （空）': 0, 推奨から変えたもの: 0 },
  results: {
    columns: RESULT_COLUMNS,
    rows: [
      ['修正', '10014（RESOURCE_LEAK）', '修正', '高', '長すぎる行で line を解放せずに戻る（本物の不具合）', 'cid/10014.html'],
      ['逸脱', '10017（NULL_RETURNS）', '逸脱', '高', 'str_is_kv_line() が = があることを確かめている（誤検知）', 'cid/10017.html'],
      ['修正', '10027（NULL_RETURNS）', '修正', '高', '[sensor] がないと NULL のセクションを参照する（本物の不具合）', 'cid/10027.html'],
      ['修正', '10041（NULL_RETURNS）', '修正', '高', 'fopen の戻り値を確かめずに fprintf に渡している（本物の不具合）', 'cid/10041.html'],
      ['逸脱', '10043（MISSING_BREAK）', '逸脱', '高', 'MISSING_BREAK の警告を確かめた（意図的）', 'cid/10043.html'],
    ],
  },
  unprocessed: { columns: UNPROCESSED_COLUMNS, rows: [['10049', '警告経路を取れませんでした（SOAP の Fault）']] },
  approval_links: '結果の画面：http://127.0.0.1:43813/?t=hBlp3g36SN30UQkHFYm73Q3rBzaTkc0u#run=20261103-173908&view=summary\n承認の選択と逸脱コメントの手直しは、この画面の「結果」で行い、/coverity-apply で反映する。',
};

/** 値をまとめて送る。断られたものを [{ key, error }] で返す */
async function showAll(p, values, agentId = p.workerId) {
  const refused = [];
  for (const [key, value] of Object.entries(values)) {
    const r = await show(p, key, value, agentId);
    if (!r.ok) refused.push({ key, error: r.error });
  }
  return refused;
}

// ct.py の出力先（../coverity-triage-out/<日時>/）と同じ形の、作業フォルダの外の実行フォルダを作る
function makeRunFolderOutside(work) {
  const runDir = path.join(path.dirname(work), `coverity-triage-out-${path.basename(work)}`, '20261103-173405');
  fs.mkdirSync(path.join(runDir, 'cid'), { recursive: true });
  fs.mkdirSync(path.join(runDir, 'patches'), { recursive: true });
  fs.writeFileSync(path.join(runDir, 'cid', '10014.html'), '<!doctype html><title>10014</title><p>詳細レポート</p>');
  fs.writeFileSync(path.join(runDir, 'patches', '10014-fix.patch'), 'diff --git a/src/app/config_ini.c b/src/app/config_ini.c\n');
  return runDir;
}

// =================================================================================================
// 1. 開始前の設定
// =================================================================================================

describe('開始前の設定（本物の task.json。REQ-102、REQ-031、REQ-007）', () => {
  test('REQ-102 4つの設定（条件ファイル・Impact で絞る・調べる件数の上限・最初に解析するか）が、見出し・種類・選択肢・初めの値・説明つきで出る', async () => {
    await withScreen({}, async ({ page }) => {
      const fieldset = page.locator('#task-settings');
      const text = await fieldset.innerText();
      assert.match(text, /条件ファイル\s*（必須）/);
      assert.match(text, /Impact で絞る\s*（必須）/);
      assert.match(text, /調べる件数の上限/);
      assert.doesNotMatch(text, /調べる件数の上限\s*（必須）/, '件数の上限は必須でない');
      assert.match(text, /最初に解析するか\s*（必須）/);
      for (const def of TASK.settings) assert.ok(text.includes(def.help), `「${def.label}」の説明が出る`);
      assert.deepEqual(await page.locator('#task-settings [data-key]').evaluateAll((els) => els.map((el) => el.dataset.key)), ['condition_file', 'impact', 'max_items', 'initial_analysis'], '定義の順');
      // 種類と初めの値
      assert.equal(await page.getAttribute('#setting-condition_file', 'type'), 'text');
      assert.equal(await page.inputValue('#setting-condition_file'), 'untriaged.yaml');
      assert.equal(await page.evaluate(() => document.getElementById('setting-impact').tagName), 'SELECT');
      assert.deepEqual(await page.locator('#setting-impact option').evaluateAll((els) => els.map((el) => el.textContent)), ['絞らない', 'High', 'Medium', 'Low', 'Unspecified'], 'Unspecified も選べる（K-101）');
      assert.equal(await page.inputValue('#setting-impact'), '絞らない');
      assert.equal(await page.getAttribute('#setting-max_items', 'type'), 'number');
      assert.equal(await page.inputValue('#setting-max_items'), '20', '初めの値は既定の条件ファイルの上限 20（K-19）');
      assert.deepEqual(await page.locator('#setting-initial_analysis option').evaluateAll((els) => els.map((el) => el.textContent)), ['設定どおり', 'する', 'しない']);
      assert.equal(await page.inputValue('#setting-initial_analysis'), '設定どおり');
      // 見出しを押すと入力欄に移る（見出しと欄が結び付いている）
      for (const key of ['condition_file', 'impact', 'max_items', 'initial_analysis']) assert.equal(await page.locator(`label[for="setting-${key}"]`).count(), 1, key);
      // 役割ごとのモデルの初めの値は、エージェントの定義の最初のモデル（統括・検証は思考力の高いモデル、作業は費用の低いモデル）
      assert.equal(await page.inputValue('#setting-model-orchestrator'), 'claude-sonnet-5.5');
      assert.equal(await page.inputValue('#setting-model-worker'), 'gpt-6-luna');
      assert.equal(await page.inputValue('#setting-model-verifier'), 'claude-sonnet-5.5');
      assert.equal(await page.inputValue('#setting-maxIterations'), String(TASK.maxIterationsDefault));
      assert.match(await page.locator('#effort-settings').innerText(), /作業（トリアージ役）の思考の深さ/);
      assert.match(await page.locator('h1').innerText(), /Coverity 警告のトリアージ/);
      await shot(page, 'prep-defaults-1280');
    });
  });

  test('REQ-102 設定を変えずに開始すると、初めの値が統括への指示文にそのまま入り、作業役の決まりで ct.py の引数になる。指示文にはプラグインのフォルダ・繰り返しの上限・合否の基準 C-01〜C-28 が入る', async () => {
    await withScreen({}, async ({ p, page }) => {
      await page.click('#start', { clickCount: 2, delay: 0 });
      const root = await beginRoot(p);
      await page.waitForTimeout(500);
      assert.equal(p.fake.callsOf('tasks.startAgent').length, 1, '2回押しても統括は1回だけ起動される');
      const prompt = root.prompt;
      assert.ok(prompt.startsWith(CONSTANTS.INSTRUCTION_HEAD), '指示文の始めの1文');
      assert.ok(prompt.includes(`## プラグインのフォルダ\n${PLUGIN_ROOT}\n`), '作業役がスキルを絶対パスで読めるよう、プラグインのフォルダが入る');
      assert.ok(prompt.includes(`## 頼まれた作業\n${REQUEST}\n`));
      assert.ok(prompt.includes(`## 繰り返しの上限\n${TASK.maxIterationsDefault} 回\n`));
      const block = prompt.split('## 作業ごとの設定の値\n')[1].split('\n\n')[0];
      assert.deepEqual(block.split('\n'), [
        '- 条件ファイル（condition_file）：untriaged.yaml',
        '- Impact で絞る（impact）：絞らない',
        '- 調べる件数の上限（max_items）：20',
        '- 最初に解析するか（initial_analysis）：設定どおり',
      ]);
      const criteria = prompt.split('## ユーザーと確かめた合否の基準')[1].split('\n').filter((l) => l.startsWith('- '));
      assert.deepEqual(criteria, TASK.criteria.map((c) => `- ${c.id}：${c.text}`), '28 の基準が番号と文のまま入る');
      assert.deepEqual(argsFromSettings(settingValues(prompt)), ['--filter', 'untriaged.yaml', '--limit', '20']);
      assert.equal(root.model, 'claude-sonnet-5.5', '統括は選んだモデルで起動される');
      assert.deepEqual(p.fake.overrides, { agents: { [`${NAME}:worker`]: { model: 'gpt-6-luna' }, [`${NAME}:verifier`]: { model: 'claude-sonnet-5.5' } } }, '作業と検証のモデル');
      await waitStatus(page, '実行中');
      assert.equal(await page.locator('#s-setup').isHidden(), true, '開始後は設定の欄を隠す');
    });
  });

  test('REQ-102 一覧にない条件ファイル（misra.yaml）、Impact の5つの選択肢（Unspecified を含む）、最初に解析するかの3つの選択肢が、選んだとおり指示文に入り、作業役の決まりの引数になる', async () => {
    const cases = [
      [{ condition: 'misra.yaml', impact: 'High', initial: 'する' }, ['--filter', 'misra.yaml', '--impact', 'High', '--limit', '20', '--initial']],
      [{ condition: 'cert-high.yaml', impact: 'Medium', initial: 'しない' }, ['--filter', 'cert-high.yaml', '--impact', 'Medium', '--limit', '20', '--no-initial']],
      [{ condition: 'all.yaml', impact: 'Low', initial: '設定どおり' }, ['--filter', 'all.yaml', '--impact', 'Low', '--limit', '20']],
      [{ condition: 'untriaged.yaml', impact: 'Unspecified', initial: 'する' }, ['--filter', 'untriaged.yaml', '--impact', 'Unspecified', '--limit', '20', '--initial']],
      [{ condition: '社内の条件.yaml', impact: '絞らない', initial: 'しない', maxItems: '7' }, ['--filter', '社内の条件.yaml', '--limit', '7', '--no-initial']],
    ];
    for (const [settings, expected] of cases) {
      await withScreen({}, async ({ p, page }) => {
        await fillSettings(page, settings);
        await page.click('#start');
        const root = await beginRoot(p);
        const values = settingValues(root.prompt);
        assert.equal(values.condition_file, settings.condition);
        assert.equal(values.impact, settings.impact);
        assert.equal(values.initial_analysis, settings.initial);
        assert.deepEqual(argsFromSettings(values), expected, JSON.stringify(settings));
      });
    }
  });

  test('REQ-102 調べる件数の上限の端（1 と 100）は、その数のまま指示文に入る', async () => {
    for (const value of ['1', '100']) {
      await withScreen({}, async ({ p, page }) => {
        await fillSettings(page, { maxItems: value });
        await page.click('#start');
        const root = await beginRoot(p);
        assert.equal(settingValues(root.prompt).max_items, value);
      });
    }
  });

  test('REQ-102 調べる件数の上限が 0・-3・101・1.5 のときは、統括を起動せずに、1 から 100 までの数で入れるよう示す', async () => {
    const problems = [];
    for (const value of ['0', '-3', '101', '1.5']) {
      await withScreen({ allowErrors: [/status of 400/] }, async ({ p, page }) => {
        await fillSettings(page, { maxItems: value });
        await page.click('#start');
        await startOutcome(p, page);
        const started = p.fake.callsOf('tasks.startAgent');
        const message = (await page.locator('#start-message').isVisible()) ? await page.locator('#start-message').innerText() : '';
        if (started.length > 0) {
          const line = settingValues(started[0].args.prompt).max_items;
          problems.push(`「${value}」：開始され、統括への指示文に「調べる件数の上限（max_items）：${line}」が入った（作業役の引数は --limit ${line}）`);
          await shot(page, `prep-max-items-${value.replace('.', '_')}-started`);
        } else if (!/1/.test(message) || !/100/.test(message)) {
          problems.push(`「${value}」：開始されなかったが、1〜100 で入れる案内が出ない（${message || '何も出ない'}）`);
        }
      });
    }
    assert.deepEqual(problems, [], '範囲の外の件数の上限で開始された');
  });

  test('REQ-102 調べる件数の上限を空にすると、「（未入力）」として指示文に入り、作業役は --limit を付けない（条件ファイルの件数が使われる）', async () => {
    await withScreen({}, async ({ p, page }) => {
      await fillSettings(page, { maxItems: '' });
      await page.click('#start');
      const root = await beginRoot(p);
      const values = settingValues(root.prompt);
      assert.equal(values.max_items, '（未入力）', `空にした件数の上限が「${values.max_items}」として届いた（作業役の決まり「値が（未入力）なら --limit を付けない」が働かない）`);
      assert.deepEqual(argsFromSettings(values), ['--filter', 'untriaged.yaml']);
    });
  });

  test('REQ-102 条件ファイルが空（空白だけも）のときは、統括を起動せずに「条件ファイル」を入れるよう示し、入れ直せば開始できる', async () => {
    await withScreen({ allowErrors: [/status of 400/] }, async ({ p, page }) => {
      for (const value of ['', '   ']) {
        await fillSettings(page, { condition: value });
        await page.click('#start');
        await until(page, () => !document.getElementById('start-message').hidden);
        assert.match(await page.locator('#start-message').innerText(), /「条件ファイル」を入力してください。/, `「${value}」`);
        await until(page, () => !document.getElementById('start').disabled);
      }
      await shot(page, 'prep-condition-file-empty');
      assert.equal(p.fake.callsOf('tasks.startAgent').length, 0, '起動されていない');
      await fillSettings(page, { condition: 'all.yaml' });
      await page.click('#start');
      const root = await beginRoot(p);
      assert.equal(settingValues(root.prompt).condition_file, 'all.yaml');
    });
  });
});

// =================================================================================================
// 2. 実行中の表示（作業役の指示どおりの値を、coverity-warning-triage_show で送る）
// =================================================================================================

/** 開始して、統括 → 作業役（実行の段）まで進めた画面を fn に渡す */
async function withRunning(options, fn) {
  return withScreen(options, async (ctx) => {
    await ctx.page.click('#start');
    await beginRoot(ctx.p);
    await toWorker(ctx.p);
    await waitPhase(ctx.page, 'do');
    return fn(ctx);
  });
}

describe('実行中の表示（作業役の指示どおりの値。REQ-103、REQ-031、REQ-008）', () => {
  test('REQ-103 5つの表示（作業ごとの様子・重大なエラーと注意・最近の動き・初回解析の進み具合・全体の進み具合）が、定義の見出しと順で出て、状態は文字でも分かる', async () => {
    await withRunning({}, async ({ p, page }) => {
      assert.deepEqual(await showAll(p, SAMPLE_RUNNING), [], '作業役の指示どおりの値が断られた');
      await waitDisplay(page, 'overall_progress', '1 / 5');
      assert.deepEqual(await shownKeys(page), RUNNING_KEYS, '実行中は running の表示だけが、定義の順で出る');
      for (const key of RUNNING_KEYS) assert.equal(await page.locator(`.display[data-key="${key}"] h3`).innerText(), LABEL[key]);
      assert.deepEqual(await tableOf(page, 'worker_status'), { head: WORKER_COLUMNS, rows: SAMPLE_RUNNING.worker_status.rows });
      assert.match(await displayText(page, 'worker_status'), /⚠ 停止\?/, '止まったかもしれない作業が、色でなく文字で分かる');
      assert.deepEqual(await tableOf(page, 'errors'), { head: ERROR_COLUMNS, rows: SAMPLE_RUNNING.errors.rows });
      const recent = await page.locator('.display[data-key="recent_activity"] .text-block').innerText();
      assert.deepEqual(recent.split('\n'), SAMPLE_RUNNING.recent_activity.split('\n'), '最近の動きの5行が、行に分かれて出る');
      assert.equal(await page.locator('.display[data-key="recent_activity"] details').count(), 0, '5行の短い文は畳まない');
      assert.equal(await page.locator('.display[data-key="initial_analysis_progress"] .progress-text').innerText(), '解析（cov-analyze）：50%');
      assert.equal(await page.locator('.display[data-key="overall_progress"] .progress-text').innerText(), '1 / 5：20%');
      assert.deepEqual(await page.locator('.display[data-key="overall_progress"] progress').evaluate((el) => [el.value, el.max]), [1, 5]);
      assert.match(await page.locator('#phases').innerText(), /実行[\s\S]*（いま）/, '今の段が文字で出る');
      assert.match(await page.locator('.agent[data-role="worker"]').innerText(), /トリアージ役[\s\S]*実行中/);
      await shot(page, 'running-do-worker-1280');
    });
  });

  test('REQ-103 初回解析をしないときの初回解析の進み具合（value 0・max 100・「初回解析はしない」）が、そのとおりに出る', async () => {
    await withRunning({}, async ({ p, page }) => {
      const r = await show(p, 'initial_analysis_progress', { value: 0, max: 100, label: '初回解析はしない' });
      assert.equal(r.ok, true, r.error);
      await waitDisplay(page, 'initial_analysis_progress', '初回解析はしない');
      assert.equal(await page.locator('.display[data-key="initial_analysis_progress"] .progress-text').innerText(), '初回解析はしない：0%');
    });
  });

  test('REQ-103 同じ表示に送り直すと、再読み込みしなくても新しい値に変わる（全体の進み具合 1 / 5 → 5 / 5、作業ごとの様子の行）', async () => {
    await withRunning({}, async ({ p, page }) => {
      await showAll(p, SAMPLE_RUNNING);
      await waitDisplay(page, 'overall_progress', '1 / 5');
      for (const n of [2, 3, 5]) {
        const r = await show(p, 'overall_progress', { value: n, max: 5, label: `${n} / 5` });
        assert.equal(r.ok, true, r.error);
        await waitDisplay(page, 'overall_progress', `${n} / 5：${n * 20}%`);
      }
      await show(p, 'worker_status', { columns: WORKER_COLUMNS, rows: SAMPLE_RUNNING.worker_status.rows.map((row) => [row[0], 'result', row[2], '終わった']) });
      await until(page, () => !document.querySelector('.display[data-key="worker_status"]').innerText.includes('停止'));
      assert.deepEqual((await tableOf(page, 'worker_status')).rows.map((row) => row[3]), ['終わった', '終わった', '終わった', '終わった', '終わった']);
    });
  });

  test('REQ-103 警告が0件のとき、作業役の指示どおりの全体の進み具合（value 0・max 0・「0 / 0」）が受け付けられ、「0 / 0」と出る', async () => {
    await withRunning({}, async ({ p, page }) => {
      const r = await show(p, 'overall_progress', { value: 0, max: 0, label: '0 / 0' });
      await shot(page, 'running-zero-warnings-overall-progress');
      assert.equal(r.ok, true, `警告0件の全体の進み具合が断られた：${r.error}`);
      await waitDisplay(page, 'overall_progress', '0 / 0');
    });
  });

  test('REQ-103 形の合わない値は、どこをどう直すかの理由を返して断り、前の表示は残る（列の数の違い・空の列・値の形・範囲・知らない key）', async () => {
    await withRunning({}, async ({ p, page }) => {
      await showAll(p, SAMPLE_RUNNING);
      await waitDisplay(page, 'overall_progress', '1 / 5');
      const keys = TASK.displays.map((d) => d.key).join('、');
      const cases = [
        ['worker_status', { columns: WORKER_COLUMNS, rows: [['10014', 'read', '1分']] }, /rows の各行は、columns と同じ数の値の一覧にしてください。/],
        ['worker_status', { columns: WORKER_COLUMNS, rows: [['10014', 'read', '1分', '処理中', '余分']] }, /rows の各行は、columns と同じ数の値の一覧にしてください。/],
        ['worker_status', { columns: [], rows: [] }, /columns は見出しの文字列の一覧にしてください。/],
        ['worker_status', { columns: WORKER_COLUMNS, rows: [['10014', { phase: 'read' }, '1分', '処理中']] }, /rows の各行は/],
        ['errors', [['10041', '処理できなかった作業', '理由']], /\{ columns: \[見出し\], rows: \[\[値\]\] \} の形で渡してください。/],
        ['recent_activity', ['1行目', '2行目'], /文字列で渡してください。/],
        ['overall_progress', { value: 6, max: 5, label: '6 / 5' }, /value は 0 以上 max 以下にしてください。/],
        ['overall_progress', { value: -1, max: 5, label: '-1 / 5' }, /value は 0 以上 max 以下にしてください。/],
        ['overall_progress', 120, /数で渡すときは 0 から 100 にしてください。/],
        ['initial_analysis_progress', { value: '2', max: 4, label: 'ビルド' }, /\{ value, max, label\? \} の形で渡してください。/],
        ['counts', { 警告: { n: 5 } }, /\{ 見出し: 値 \} の形で渡してください。/],
        ['detail_reports', '', /作業フォルダの中の場所を、文字列で渡してください。/],
        ['detail_reports', 'cid/99999.html', /ファイルが見つかりません：cid\/99999\.html/],
        ['progress', { value: 1, max: 2 }, new RegExp(`作業ごとの指示にない表示です：progress。使える表示の名前（key）：${keys}`)],
      ];
      const problems = [];
      for (const [key, value, expected] of cases) {
        const r = await show(p, key, value);
        if (r.ok) problems.push(`${key} ${JSON.stringify(value)}：断られなかった`);
        else if (!expected.test(r.error)) problems.push(`${key} ${JSON.stringify(value)}：理由が「${r.error}」`);
        else if (/やり直しは、あと/.test(r.error)) problems.push(`${key}：表示の断りが、やり直しの回数に数えられた`);
      }
      assert.deepEqual(problems, []);
      await page.waitForTimeout(1300);
      assert.deepEqual(await tableOf(page, 'worker_status'), { head: WORKER_COLUMNS, rows: SAMPLE_RUNNING.worker_status.rows }, '断られた値で、前の表示が消えない');
      assert.equal(await page.locator('.display[data-key="overall_progress"] .progress-text').innerText(), '1 / 5：20%');
      assert.equal(p.core.getState().run.status, 'running', '表示を何度断られても、実行は続く');
    });
  });

  test('REQ-030 とても長い文字（長い内容の行、長い最近の動き、10万字のエラー）でも、表は画面の外にはみ出さず、長い文章は畳まれる（390px〜1920px）', async () => {
    await withRunning({ viewport: { width: 390, height: 844 } }, async ({ p, page }) => {
      const longPath = `/build/agent/work/envmon/${'src/very/deep/folder/'.repeat(20)}config_ini.c`;
      const values = {
        worker_status: { columns: WORKER_COLUMNS, rows: [['G1（3 件）', `read：${'警告経路を読んでいる'.repeat(40)}`, '1時間2分3秒', '⚠ 停止?'], ['10017', longPath, '12分40秒', '処理中']] },
        errors: { columns: ERROR_COLUMNS, rows: [['（全体）', '接続のエラー', `Coverity に接続できません（401）。${'x'.repeat(100000)}`], ['10041', '処理できなかった作業', longPath]] },
        recent_activity: Array.from({ length: 5 }, (_, i) => `17:34:2${i} ${longPath} の警告経路を読んでいます。${'長い説明が続きます。'.repeat(10)}`).join('\n'),
        initial_analysis_progress: { value: 1, max: 4, label: `ビルド（cov-build）${'とても長い段の名前'.repeat(20)}` },
        overall_progress: { value: 12345, max: 99999, label: '12345 / 99999' },
      };
      assert.deepEqual(await showAll(p, values), [], '長い値が断られた');
      await waitDisplay(page, 'overall_progress', '12345 / 99999');
      const fold = page.locator('.display[data-key="recent_activity"] details');
      assert.equal(await fold.count(), 1, '長い最近の動きは畳まれる');
      assert.equal(await fold.getAttribute('open'), null);
      await shot(page, 'running-long-text-390');
      for (const width of [390, 768, 1280, 1920]) {
        await page.setViewportSize({ width, height: 900 });
        await page.waitForTimeout(150);
        assert.deepEqual(await page.evaluate(layoutProblems), [], `${width}px`);
      }
      await fold.locator('summary').click();
      assert.deepEqual(await page.evaluate(layoutProblems), [], '畳みを開いても崩れない');
      await shot(page, 'running-long-text-1920');
    });
  });

  test('REQ-006 表示の値（表の見出し・行・文章・進み具合の名前・値の一覧）に HTML やスクリプトを入れても、文字として出るだけで動かない', async () => {
    const evil = ['<img src=x onerror="window.__pwned=1">', '<script>window.__pwned=1</script>', '"><svg onload="window.__pwned=1">', '<a href="javascript:window.__pwned=1">押す</a>'];
    await withRunning({}, async ({ p, page }) => {
      const values = {
        worker_status: { columns: [evil[0], '段', '経過時間', '状態'], rows: [[evil[1], evil[2], evil[3], '⚠ 停止?']] },
        errors: { columns: ERROR_COLUMNS, rows: [['10041', evil[0], evil[1]]] },
        recent_activity: evil.join('\n'),
        initial_analysis_progress: { value: 1, max: 4, label: evil[2] },
        overall_progress: { value: 1, max: 5, label: evil[3] },
        counts: { [evil[0]]: evil[1] },
        results: { columns: RESULT_COLUMNS, rows: [[evil[0], evil[1], evil[2], evil[3], evil[0], evil[1]]] },
        approval_links: `結果の画面：${evil[3]}`,
      };
      assert.deepEqual(await showAll(p, values), []);
      await waitDisplay(page, 'recent_activity', 'svg onload');
      await page.waitForTimeout(300);
      assert.equal(await page.evaluate(() => window.__pwned), undefined, 'スクリプトが動いた');
      const found = await page.evaluate(() => ({
        tags: document.querySelectorAll('#s-displays img, #s-displays script, #s-displays svg, #s-displays a[href^="javascript"]').length,
      }));
      assert.equal(found.tags, 0, 'HTML の要素ができた');
      const text = await page.locator('#s-displays').innerText();
      for (const e of evil) assert.ok(text.includes(e), `文字のまま出る：${e}`);
      await shot(page, 'running-html-in-values');
    });
  });

  test('REQ-103 表示を送れるのは統括と作業だけで、検証からは断られる', async () => {
    await withRunning({}, async ({ p, page }) => {
      const byRoot = await show(p, 'recent_activity', '統括から送った最近の動き', p.rootId);
      assert.equal(byRoot.ok, true, byRoot.error);
      await waitDisplay(page, 'recent_activity', '統括から送った最近の動き');
      await toVerifier(p);
      const byVerifier = await show(p, 'recent_activity', '検証から送った最近の動き', p.verifierId);
      assert.equal(byVerifier.ok, false);
      assert.match(byVerifier.error, /表示の記録は、統括エージェントか作業エージェントだけが行えます。/);
      assert.ok(!(await displayText(page, 'recent_activity')).includes('検証から'));
    });
  });

  test('REQ-103 終わった後の表示は、実行中に送っても出ず、終わると出る。実行中の表示は、終わると出なくなる', async () => {
    await withRunning({}, async ({ p, page }) => {
      assert.deepEqual(await showAll(p, { ...SAMPLE_RUNNING, ...SAMPLE_FINISHED }), []);
      await waitDisplay(page, 'overall_progress', '1 / 5');
      await page.waitForTimeout(1200);
      assert.deepEqual(await shownKeys(page), RUNNING_KEYS);
      await toVerifier(p);
      await waitPhase(page, 'study');
      assert.deepEqual(await shownKeys(page), RUNNING_KEYS, '検証の段でも、実行中の表示が出ている');
      await toFinished(p);
      await waitStatus(page, '終了');
      await until(page, () => document.querySelector('.display[data-key="counts"]'));
      assert.deepEqual(await shownKeys(page), FINISHED_KEYS, '終わった後は finished の表示だけが、定義の順で出る');
    });
  });
});

// =================================================================================================
// 3. 終わった後の表示（作業役の指示どおりの値）
// =================================================================================================

describe('終わった後の表示（作業役の指示どおりの値。REQ-103、REQ-031）', () => {
  test('REQ-103 件数と承認の内訳（12項目）・一覧（6列）・処理できなかった作業（2列）・承認の画面の案内が、見出しつきで出る', async () => {
    await withRunning({}, async ({ p, page }) => {
      assert.deepEqual(await showAll(p, SAMPLE_FINISHED), []);
      await toVerifier(p);
      await toFinished(p);
      await waitStatus(page, '終了');
      await until(page, () => document.querySelector('.display[data-key="results"] table'));
      for (const key of FINISHED_KEYS) assert.equal(await page.locator(`.display[data-key="${key}"] h3`).innerText(), LABEL[key]);
      const pairs = await page.locator('.display[data-key="counts"] .fact').evaluateAll((els) => els.map((el) => [el.querySelector('dt').textContent, el.querySelector('dd').textContent]));
      assert.deepEqual(pairs, COUNT_LABELS.map((label) => [label, String(SAMPLE_FINISHED.counts[label])]), '12項目が、指示の順で出る');
      assert.deepEqual(await tableOf(page, 'results'), { head: RESULT_COLUMNS, rows: SAMPLE_FINISHED.results.rows });
      assert.deepEqual(await tableOf(page, 'unprocessed'), { head: UNPROCESSED_COLUMNS, rows: SAMPLE_FINISHED.unprocessed.rows });
      assert.match(await displayText(page, 'approval_links'), /承認の選択と逸脱コメントの手直しは、この画面の「結果」で行い、\/coverity-apply で反映する。/);
      await shot(page, 'finished-1280');
    });
  });

  test('REQ-103 処理できなかった作業がないとき（行が0の表）、終わった後に「まだありません」（これから出るという意味）とは出ない', async () => {
    await withRunning({}, async ({ p, page }) => {
      assert.deepEqual(await showAll(p, { ...SAMPLE_FINISHED, unprocessed: { columns: UNPROCESSED_COLUMNS, rows: [] } }), []);
      await toVerifier(p);
      await toFinished(p);
      await waitStatus(page, '終了');
      await until(page, () => document.querySelector('.display[data-key="unprocessed"]'));
      const text = await displayText(page, 'unprocessed');
      await shot(page, 'finished-no-unprocessed');
      assert.doesNotMatch(text, /まだありません/, `終わった後の「${LABEL.unprocessed}」が「${text.replace(/\n/g, ' ')}」と出る`);
    });
  });
});

// =================================================================================================
// 4. 実際の流れ：本物の ct.py・MCP サーバー・エミュレーター
// =================================================================================================

const PYTHON = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

// e2e_flow_support.py を使って、作業役が動かす ct.py の流れを再現する小さな道具（標準入力で頼み、標準出力に1行1つの JSON）
const HELPER_SOURCE = String.raw`"""web-ui.test.mjs の道具（試験ではない）。e2e_flow_support.py で、本物の ct.py・MCP サーバー・エミュレーターを動かす。"""
import json
import sys
import time
import traceback
from pathlib import Path

sys.dont_write_bytecode = True  # プラグインの中に __pycache__ を作らない
sys.path.insert(0, sys.argv[1])


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


try:
    import e2e_flow_support as s
except Exception:  # noqa: BLE001
    emit({"event": "error", "error": traceback.format_exc()[-3000:]})
    sys.exit(0)

STREAM = "envmon-main"
NONE_FILTER = ("name: 分類ずみ（Bug）\nproject: envmon\nstreams: [envmon-main]\n"
               "triage:\n  classification: [Bug]\nlimit: 20\n")
missing = s.missing_tools()
if missing:
    emit({"event": "skip", "reason": "通しの流れの道具が足りません: " + ", ".join(missing)})
    sys.exit(0)

world = None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def snapshot(run_dir, result):
    rd = Path(run_dir)
    run = read_json(rd / "run.json", {}) or {}
    progress = {}
    for item in run.get("items", []):
        found = read_json(rd / "items" / item["id"] / "progress.json")
        if found is not None:
            progress[item["id"]] = found
    return {"tick": {k: result.get(k) for k in ("stage", "stage_label", "say", "counts", "done", "wait", "stop", "error")},
            "status": dict(world.ct("status", "--run", run_dir)),
            "initial": dict(world.ct("initial-status", "--run", run_dir)),
            "items": [{k: i.get(k) for k in ("id", "cids", "status", "started", "seconds", "error")}
                      for i in run.get("items", [])],
            "progress": progress, "now": time.time()}


def do_run(cmd):
    rid = cmd["id"]
    made = world.ct("new-run", "--repo", str(world.repo), *cmd["args"])
    emit({"event": "new_run", "id": rid, "code": made.code, "result": dict(made)})
    if not made.get("ok"):
        emit({"event": "finished", "id": rid, "ok": False, "error": made.get("error")})
        return
    run_dir = made["run_dir"]
    wait = str(cmd.get("wait", 2))

    class Recording(s.Orchestrator):
        def tick(self, *extra, env=None):
            result = self.world.ct("tick", "--run", self.run_dir, "--wait", wait, *extra, env=env)
            result.at = time.monotonic()
            self.ticks.append(result)
            if not result.get("ok", False):
                raise AssertionError(f"ct.py tick が ok: false: {result.get('error')}")
            emit({"event": "snapshot", "id": rid, **snapshot(run_dir, result)})
            return result

    Recording(world, run_dir, s.Actors(world), "webui").go()
    summary = world.ct("summary", "--run", run_dir)
    gui = world.ct("gui", "--repo", str(world.repo), "--run", run_dir, "--view", "summary", "--no-browser")
    if gui.get("url"):
        try:
            s.gui_call(gui["url"], "POST", "/api/close", {})
        except Exception:  # noqa: BLE001 - 止まっていればよい
            pass
    rd = Path(run_dir)
    run = read_json(rd / "run.json", {}) or {}
    results = {i["id"]: read_json(rd / "items" / i["id"] / "result.json") for i in run.get("items", [])}
    issues = (read_json(rd / "issues.json", {}) or {}).get("issues") or []
    emit({"event": "finished", "id": rid, "ok": True, "run_dir": run_dir, "summary": dict(summary),
          "summary_code": summary.code, "gui": dict(gui), "approvals": read_json(rd / "approvals.json"),
          "items": run.get("items", []), "results": {k: v for k, v in results.items() if v is not None},
          "issues": [{k: i.get(k) for k in ("cid", "checker", "impact", "file", "line")} for i in issues],
          "files": sorted(str(p) for sub in ("cid", "patches") for p in (rd / sub).glob("*") if p.is_file()),
          "status": dict(world.ct("status", "--run", run_dir))})


def do_limits(cmd):
    out = {}
    for value in cmd["values"]:
        made = world.ct("new-run", "--repo", str(world.repo), "--filter", "untriaged.yaml", "--limit", value,
                        "--no-initial")
        out[value] = {"code": made.code, "ok": bool(made.get("ok")), "error": made.get("error"),
                      "limit": made.get("limit")}
    emit({"event": "finished", "id": cmd["id"], "ok": True, "limits": out})


try:
    world = s.World("webui")
    setup = world.setup(STREAM, verify_default="none", initial=False)
    world.write_filter("none.yaml", NONE_FILTER)
    emit({"event": "ready", "repo": str(world.repo), "root": str(s.Session.dir()),
          "setup": {k: bool(v.get("ok")) for k, v in setup.items()},
          "filters": sorted(p.name for p in (world.repo / ".coverity-triage" / "filters").glob("*.yaml"))})
    for line in sys.stdin:
        if not line.strip():
            continue
        cmd = json.loads(line)
        if cmd.get("cmd") == "quit":
            break
        try:
            {"run": do_run, "limits": do_limits}[cmd["cmd"]](cmd)
        except Exception:  # noqa: BLE001
            emit({"event": "finished", "id": cmd.get("id"), "ok": False, "error": traceback.format_exc()[-3000:]})
except Exception:  # noqa: BLE001
    emit({"event": "error", "error": traceback.format_exc()[-3000:]})
finally:
    try:
        if world is not None:
            world.close()
    finally:
        s.Session.cleanup()
    emit({"event": "bye"})
`;

/** 道具を起動する。ready（{ event: 'ready' | 'skip' | 'error' }）と、頼む request(cmd, onEvent) を返す */
function startHelper() {
  const dir = path.join(TEMP, 'helper');
  fs.mkdirSync(dir, { recursive: true });
  const script = path.join(dir, 'webui_flow.py');
  fs.writeFileSync(script, HELPER_SOURCE);
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8' };
  delete env.CWT_CAPTURE_DIR; // 通しの道具の写真は撮らない（この試験が撮る）
  let stderr = '';
  const waiters = new Map();
  let settleReady;
  const ready = new Promise((resolve) => {
    settleReady = resolve;
  });
  let child;
  try {
    child = spawn(PYTHON, ['-u', script, HERE], { cwd: dir, env, stdio: ['pipe', 'pipe', 'pipe'] });
  } catch (error) {
    settleReady({ event: 'skip', reason: `Python（${PYTHON}）を起動できません：${error.message}` });
    return { ready, request: async () => assert.fail('道具がない'), quit: async () => {}, stderr: () => '' };
  }
  child.on('error', (error) => settleReady({ event: 'skip', reason: `Python（${PYTHON}）を起動できません：${error.message}` }));
  child.stderr.on('data', (chunk) => {
    stderr = (stderr + chunk).slice(-8000);
  });
  readline.createInterface({ input: child.stdout }).on('line', (line) => {
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      return;
    }
    if (!event.id) {
      if (['ready', 'skip', 'error'].includes(event.event)) settleReady(event);
      return;
    }
    const waiter = waiters.get(event.id);
    if (!waiter) return;
    waiter.chain = waiter.chain
      .then(async () => {
        if (event.event !== 'finished') return waiter.onEvent?.(event);
        waiters.delete(event.id);
        if (waiter.error) waiter.reject(waiter.error);
        else waiter.resolve(event);
      })
      .catch((error) => {
        waiter.error ??= error;
      });
  });
  const exited = new Promise((resolve) => child.on('exit', (code) => {
    settleReady({ event: 'error', error: `道具が終わりました（終了コード ${code}）：${stderr.slice(-3000)}` });
    for (const waiter of waiters.values()) waiter.reject(new Error(`道具が途中で終わりました：${stderr.slice(-3000)}`));
    resolve(code);
  }));
  let seq = 0;
  return {
    ready,
    stderr: () => stderr,
    request(cmd, onEvent) {
      seq += 1;
      const id = `r${seq}`;
      return new Promise((resolve, reject) => {
        waiters.set(id, { onEvent, resolve, reject, chain: Promise.resolve(), error: null });
        child.stdin.write(`${JSON.stringify({ ...cmd, id })}\n`);
      });
    },
    async quit() {
      try {
        child.stdin.write('{"cmd": "quit"}\n');
        child.stdin.end();
      } catch {
        // もう終わっている
      }
      const timer = setTimeout(() => child.kill('SIGKILL'), 60000);
      await exited;
      clearTimeout(timer);
    },
  };
}

describe('実際の流れ：画面の設定 → 本物の ct.py・MCP サーバー・エミュレーター → 画面の表示（REQ-102、REQ-103、REQ-106、REQ-008）', () => {
  const rec = {};
  const errors = {};
  let skipReason = null;

  async function step(name, fn) {
    try {
      rec[name] = await fn();
    } catch (error) {
      errors[name] = String(error?.stack ?? error).slice(0, 4000);
    }
    return rec[name];
  }
  function need(t, ...names) {
    if (skipReason) {
      t.skip(skipReason);
      return false;
    }
    for (const name of names) if (!(name in rec)) assert.fail(`段「${name}」の結果がありません：${errors[name] ?? errors.flow ?? '動いていません'}`);
    return true;
  }

  before(async () => {
    const helper = startHelper();
    cleanups.push(() => helper.quit());
    const ready = await helper.ready;
    if (ready.event === 'skip') {
      skipReason = ready.reason;
      return;
    }
    if (ready.event !== 'ready') {
      errors.flow = ready.error;
      return;
    }
    rec.ready = ready;
    try {
      // ---- 利用者のリポジトリで Copilot CLI を動かし、画面を開く ----
      const p = await startPlugin({ cwd: ready.repo });
      const { page, context, errors: pageErrors } = await openPage(p.url, { width: 1280, height: 900 });
      cleanups.push(() => context.close());
      rec.pageErrors = pageErrors;
      await shot(page, 'real-prep');

      // ---- 1回目：上限 5 件・最初に解析する ----
      await fillSettings(page, { maxItems: '5', initial: 'する' });
      await page.click('#start');
      const root = await beginRoot(p);
      rec.prompt = root.prompt;
      rec.values = settingValues(root.prompt);
      rec.args = argsFromSettings(rec.values);
      await waitPhase(page, 'plan');
      rec.planObserved = { phases: await page.locator('#phases').innerText(), orchestrator: await page.locator('.agent[data-role="orchestrator"]').innerText() };
      await shot(page, 'real-running-plan-orchestrator');
      await toWorker(p);
      await waitPhase(page, 'do');

      const initial = rec.args.includes('--initial');
      rec.live = [];
      rec.snapshots = [];
      rec.observed = {};
      const sendRunning = async (snap, stage) => {
        const values = runningValues(snap, { initial });
        for (const [key, value] of Object.entries(values)) {
          const r = await show(p, key, value);
          rec.live.push({ stage, key, value, ok: r.ok, error: r.error ?? null });
        }
        return values;
      };
      rec.run = await helper.request({ cmd: 'run', args: rec.args, wait: 2 }, async (event) => {
        if (event.event === 'new_run') {
          rec.newRun = event;
          if (event.result?.ok) await sendRunning({ items: [], status: { counts: {} }, initial: { status: 'none' }, now: Date.now() / 1000 }, 'start'); // 実行を始めたとき
          return;
        }
        rec.snapshots.push(event);
        const values = await sendRunning(event, event.tick.stage);
        const running = event.items.find((i) => i.status === 'in_progress');
        if (running && !rec.observed.items) {
          await waitDisplay(page, 'worker_status', '処理中'); // 前の様子（全部が未処理）から変わるまで待つ
          await waitDisplay(page, 'overall_progress', values.overall_progress.label);
          rec.observed.items = { id: running.id, worker: await displayText(page, 'worker_status'), overall: await displayText(page, 'overall_progress'), recent: await displayText(page, 'recent_activity'), worker_card: await page.locator('.agent[data-role="worker"]').innerText(), phases: await page.locator('#phases').innerText() };
          await shot(page, 'real-running-do-items');
        }
        if (event.initial?.status === 'running' && !rec.observed.initial) {
          const label = values.initial_analysis_progress.label;
          await waitDisplay(page, 'initial_analysis_progress', label);
          rec.observed.initial = { label, text: await displayText(page, 'initial_analysis_progress') };
          await shot(page, 'real-running-do-initial-analysis');
        }
        if (event.tick.done) {
          await waitDisplay(page, 'overall_progress', values.overall_progress.label);
          rec.observed.last = { overall: await displayText(page, 'overall_progress') };
        }
      });
      assert.equal(rec.run.ok, true, rec.run.error);
      // ---- ct.py summary を終えたとき：終わった後の表示 ----
      rec.finishedValues = finishedValues(rec.run);
      rec.finishedShows = {};
      for (const [key, value] of Object.entries(rec.finishedValues)) rec.finishedShows[key] = await show(p, key, value);
      await toVerifier(p, rec.run.run_dir);
      await waitPhase(page, 'study');
      rec.studyObserved = { phases: await page.locator('#phases').innerText(), verifier: await page.locator('.agent[data-role="verifier"]').innerText() };
      await shot(page, 'real-running-study-verifier');
      await toFinished(p);
      await waitStatus(page, '終了');
      await until(page, () => document.querySelector('.display[data-key="results"] table'), null, 10000);
      rec.finishedPage = {
        keys: await shownKeys(page),
        counts: await page.locator('.display[data-key="counts"] .fact').evaluateAll((els) => els.map((el) => [el.querySelector('dt').textContent, el.querySelector('dd').textContent])),
        results: await tableOf(page, 'results'),
        unprocessed: await displayText(page, 'unprocessed'),
        detail: { text: await displayText(page, 'detail_reports'), href: (await linksOf(page, 'detail_reports'))[0] ?? null },
        patches: { text: await displayText(page, 'patches'), href: (await linksOf(page, 'patches'))[0] ?? null },
        approval: { text: await displayText(page, 'approval_links'), links: await linksOf(page, 'approval_links') },
      };
      if (rec.finishedPage.detail.href) {
        const response = await page.request.get(new URL(rec.finishedPage.detail.href, p.url).toString());
        rec.finishedPage.detail.status = response.status();
      }
      await shot(page, 'real-finished');

      // ---- 2回目：警告が0件になる条件ファイル（none.yaml）・最初に解析しない ----
      const reopened = await p.fake.callTool(`${NAME}_open`, { requestText: REQUEST });
      assert.equal(reopened.ok, true, reopened.error);
      await page.reload();
      await page.waitForFunction(() => document.getElementById('setting-condition_file'), null, { timeout: 8000 });
      await fillSettings(page, { condition: 'none.yaml', initial: 'しない' });
      await page.click('#start');
      const root2 = await beginRoot(p);
      rec.zeroArgs = argsFromSettings(settingValues(root2.prompt));
      await toWorker(p, 'ag-worker-2');
      await waitPhase(page, 'do');
      rec.zeroLive = [];
      rec.zeroRun = await helper.request({ cmd: 'run', args: rec.zeroArgs, wait: 2 }, async (event) => {
        if (event.event !== 'snapshot') return;
        for (const [key, value] of Object.entries(runningValues(event, { initial: false }))) {
          const r = await show(p, key, value);
          rec.zeroLive.push({ stage: event.tick.stage, key, value, ok: r.ok, error: r.error ?? null });
        }
      });
      assert.equal(rec.zeroRun.ok, true, rec.zeroRun.error);
      await page.waitForTimeout(1200);
      rec.zeroRunningPage = { overall: await displayText(page, 'overall_progress') };
      await shot(page, 'real-zero-warnings-running');
      rec.zeroFinishedValues = finishedValues(rec.zeroRun);
      rec.zeroFinishedShows = {};
      for (const [key, value] of Object.entries(rec.zeroFinishedValues)) rec.zeroFinishedShows[key] = await show(p, key, value);
      await toVerifier(p, rec.zeroRun.run_dir, 'ag-verifier-2');
      await toFinished(p);
      await waitStatus(page, '終了');
      await until(page, () => document.querySelector('.display[data-key="counts"] dl'), null, 10000);
      rec.zeroFinishedPage = { counts: await page.locator('.display[data-key="counts"] .fact').evaluateAll((els) => els.map((el) => [el.querySelector('dt').textContent, el.querySelector('dd').textContent])) };
      await shot(page, 'real-zero-warnings-finished');

      // ---- 件数の上限の範囲：ct.py new-run と画面 ----
      rec.ctLimits = (await helper.request({ cmd: 'limits', values: ['0', '-3', '101', '1.5', '1', '100'] })).limits;
      rec.uiLimits = {};
      for (const value of Object.keys(rec.ctLimits)) {
        await withScreen({ allowErrors: [/status of 400/] }, async ({ p: q, page: pg }) => {
          await fillSettings(pg, { maxItems: value });
          await pg.click('#start');
          await startOutcome(q, pg);
          rec.uiLimits[value] = q.fake.callsOf('tasks.startAgent').length > 0;
        });
      }
      await p.handle.shutdown();
    } catch (error) {
      errors.flow = `${String(error?.stack ?? error).slice(0, 3000)}\n道具の標準エラー：${helper.stderr().slice(-2000)}`;
    }
  }, { timeout: 900000 });

  test('REQ-102 画面の設定（上限 5・最初に解析する）から作った指示文を、作業役の決まりどおりに引数にすると、ct.py new-run がその条件で受け付ける', (t) => {
    if (!need(t, 'values', 'run')) return;
    assert.deepEqual(rec.args, ['--filter', 'untriaged.yaml', '--limit', '5', '--initial']);
    assert.equal(rec.newRun.result.ok, true, rec.newRun.result.error);
    assert.equal(rec.newRun.result.limit, 5);
    assert.equal(rec.newRun.result.initial, true);
    assert.equal(rec.run.items.length, 5, '5件で止まる');
  });

  test('REQ-102 件数の上限の範囲が、画面と ct.py で合っている（ct.py が断る 0・-3・101・1.5 では、画面でも開始しない）', (t) => {
    if (!need(t, 'ctLimits', 'uiLimits')) return;
    const mismatch = Object.entries(rec.ctLimits)
      .filter(([value, ct]) => rec.uiLimits[value] !== ct.ok)
      .map(([value, ct]) => `「${value}」：画面は${rec.uiLimits[value] ? '開始した（統括を起動した）' : '開始しない'}が、ct.py new-run は${ct.ok ? '受け付ける' : `断る（${ct.error.slice(0, 80)}）`}`);
    assert.deepEqual(mismatch, []);
  });

  test('REQ-106 実行フォルダ（ct.py の output_dir の中）は、利用者のリポジトリ（Copilot CLI の作業フォルダ）の外にある', (t) => {
    if (!need(t, 'run')) return;
    const relative = path.relative(rec.ready.repo, rec.run.run_dir);
    assert.ok(relative.startsWith('..'), `${rec.run.run_dir} が ${rec.ready.repo} の中にある`);
  });

  test('REQ-008 実行中、統括（計画）・作業（実行）・検証（評価）が動いていることが、フェーズの「（いま）」と状態の札の文字で出る', (t) => {
    if (!need(t, 'run')) return;
    assert.match(rec.planObserved.phases, /計画[\s\S]*（いま）/);
    assert.match(rec.planObserved.orchestrator, /統括エージェント[\s\S]*実行中/);
    assert.match(rec.observed.items.phases, /実行[\s\S]*（いま）/);
    assert.match(rec.observed.items.worker_card, /トリアージ役[\s\S]*実行中/);
    assert.match(rec.studyObserved.phases, /評価[\s\S]*（いま）/);
    assert.match(rec.studyObserved.verifier, /検証エージェント[\s\S]*実行中/);
  });

  test('REQ-103 実行中、作業ごとの様子の表に、実際に処理中の作業の ID・段・経過時間・状態が、再読み込みなしで出る', (t) => {
    if (!need(t, 'run')) return;
    assert.ok(rec.observed.items, '処理中の作業の様子を見られなかった');
    assert.match(rec.observed.items.worker, new RegExp(`${rec.observed.items.id}\\s+\\S+\\s+\\d+秒\\s+処理中`));
    for (const column of WORKER_COLUMNS) assert.ok(rec.observed.items.worker.includes(column));
  });

  test('REQ-103 初回解析をするとき、初回解析の進み具合に、実際の段（ct.py initial-status）が出る', (t) => {
    if (!need(t, 'run')) return;
    assert.ok(rec.observed.initial, '初回解析の途中の様子を見られなかった');
    assert.ok(rec.observed.initial.text.includes(rec.observed.initial.label), rec.observed.initial.text);
    const labels = new Set(rec.live.filter((l) => l.key === 'initial_analysis_progress').map((l) => l.value.label));
    assert.ok([...labels].some((l) => /ビルド|解析|登録|環境/.test(l)), [...labels].join('、'));
  });

  test('REQ-103 全体の進み具合が、処理した件数 / 全体で増え、最後は 5 / 5 になる', (t) => {
    if (!need(t, 'run')) return;
    const accepted = rec.live.filter((l) => l.key === 'overall_progress' && l.ok).map((l) => l.value.value);
    assert.deepEqual(accepted, [...accepted].sort((a, b) => a - b), '減らない');
    assert.equal(accepted.at(-1), 5);
    assert.match(rec.observed.last.overall, /5 \/ 5：100%/);
  });

  test('REQ-103 作業役が指示どおりに送った実行中の表示は、どれも断られない（実行を始めたとき・初回解析と検索の間を含む）', (t) => {
    if (!need(t, 'run')) return;
    const refused = rec.live.filter((l) => !l.ok).map((l) => `段 ${l.stage}：${l.key} ${JSON.stringify(l.value)} → ${l.error}`);
    assert.deepEqual([...new Set(refused)], []);
  });

  test('REQ-103 最近の動きに、ct.py status の最近の動き（新しいものから5行まで）が出る（作業役の指示）', (t) => {
    if (!need(t, 'run')) return;
    const during = rec.snapshots.filter((s) => s.tick.stage === 'items');
    assert.ok(during.length > 0);
    const keys = Object.keys(during[0].status).sort();
    const withLines = during.filter((s) => recentFromStatus(s.status).length > 0);
    assert.ok(withLines.length > 0, `警告を調べている間の ct.py status に最近の動きがない（返る項目：${keys.join('、')}）。画面の「${LABEL.recent_activity}」は「${rec.observed.items?.recent.replace(/\n/g, ' ')}」のまま`);
  });

  test('REQ-103 終わった後、件数と承認の内訳の12項目が、実際の一覧と approvals.json の値で出る', (t) => {
    if (!need(t, 'run', 'finishedPage')) return;
    assert.equal(rec.finishedShows.counts.ok, true, rec.finishedShows.counts.error);
    assert.deepEqual(rec.finishedPage.counts, COUNT_LABELS.map((label) => [label, String(rec.finishedValues.counts[label])]));
    const counts = Object.fromEntries(rec.finishedPage.counts);
    assert.equal(Number(counts['修正の推奨']), rec.run.summary.recommended.fix);
    assert.equal(Number(counts['逸脱の推奨']), rec.run.summary.recommended.deviation);
    assert.equal(Number(counts['作業']), rec.run.summary.items);
  });

  test('REQ-103 終わった後、一覧（承認・ID・推奨・確信度・見立て・詳細）が、実際の結果で確信度の低い順に出る', (t) => {
    if (!need(t, 'run', 'finishedPage')) return;
    assert.equal(rec.finishedShows.results.ok, true, rec.finishedShows.results.error);
    assert.deepEqual(rec.finishedPage.results.head, RESULT_COLUMNS);
    assert.equal(rec.finishedPage.results.rows.length, rec.run.items.length);
    const order = rec.finishedPage.results.rows.map((r) => CONFIDENCE_ORDER[Object.keys(CONFIDENCE_JA).find((k) => CONFIDENCE_JA[k] === r[3])]);
    assert.deepEqual(order, [...order].sort((a, b) => a - b));
    for (const row of rec.finishedPage.results.rows) assert.match(row[1], /^(\d+|G\d+（\d+ 件）)（[A-Za-z_.*0-9 -]+）$/, 'ID 欄にチェッカー名');
  });

  test('REQ-103 終わった後、詳細レポート（確信度の低い順の1件目の cid/<ID>.html）が画面に出て、開ける', (t) => {
    if (!need(t, 'run', 'finishedPage')) return;
    const shown = rec.finishedShows.detail_reports;
    assert.ok(shown, '詳細レポートを送っていない');
    assert.equal(shown.ok, true, `作業役が送った ${rec.finishedValues.detail_reports} が断られた：${shown.error}`);
    assert.ok(rec.finishedPage.detail.href, `画面の「${LABEL.detail_reports}」が「${rec.finishedPage.detail.text.replace(/\n/g, ' ')}」`);
    assert.equal(rec.finishedPage.detail.status, 200);
  });

  test('REQ-103 終わった後、差分（patches/<ID>-fix.patch）が画面に出て、開ける', (t) => {
    if (!need(t, 'run', 'finishedPage')) return;
    const shown = rec.finishedShows.patches;
    assert.ok(shown, '差分を送っていない（修正の作業がない）');
    assert.equal(shown.ok, true, `作業役が送った ${rec.finishedValues.patches} が断られた：${shown.error}`);
    assert.ok(rec.finishedPage.patches.href, `画面の「${LABEL.patches}」が「${rec.finishedPage.patches.text.replace(/\n/g, ' ')}」`);
  });

  test('REQ-103 終わった後、承認の画面（ct.py gui の「結果」）の URL が出て、押して開けるリンクになっている', (t) => {
    if (!need(t, 'run', 'finishedPage')) return;
    assert.equal(rec.finishedShows.approval_links.ok, true, rec.finishedShows.approval_links.error);
    assert.ok(rec.finishedPage.approval.text.includes(rec.run.gui.url), 'URL が全部出る');
    assert.ok(rec.finishedPage.approval.links.includes(rec.run.gui.url), `URL が文字だけで出て、押せない（リンクの数 ${rec.finishedPage.approval.links.length}）`);
  });

  test('REQ-103 警告が0件の実行でも、全体の進み具合が断られずに「0 / 0」と出て、終わった後の件数は0で出る', (t) => {
    if (!need(t, 'zeroRun', 'zeroFinishedPage')) return;
    assert.equal(rec.zeroRun.items.length, 0, '警告は0件');
    const counts = Object.fromEntries(rec.zeroFinishedPage.counts);
    assert.equal(counts['警告'], '0');
    assert.equal(counts['作業'], '0');
    const refused = rec.zeroLive.filter((l) => !l.ok).map((l) => `段 ${l.stage}：${l.key} ${JSON.stringify(l.value)} → ${l.error}`);
    assert.deepEqual([...new Set(refused)], [], `画面の「${LABEL.overall_progress}」は「${rec.zeroRunningPage.overall.replace(/\n/g, ' ')}」`);
  });

  test('REQ-030 実際の流れのあいだ、画面にエラーが出ない', (t) => {
    if (!need(t, 'run')) return;
    assert.deepEqual(rec.pageErrors, []);
  });
});

// =================================================================================================
// 5. 見た目（開始前・実行中・終わった後）
// =================================================================================================

/** 見た目の試験の画面：prep（開始前）、plan・do・study（実行中）、finished（終わった後） */
async function withState(state, options, fn) {
  return withScreen(options, async (ctx) => {
    const { p, page } = ctx;
    if (state !== 'prep') {
      await page.click('#start');
      await beginRoot(p);
      await waitPhase(page, 'plan');
    }
    if (['do', 'study', 'finished'].includes(state)) {
      await toWorker(p);
      await showAll(p, { ...SAMPLE_RUNNING, ...SAMPLE_FINISHED });
      const runDir = makeRunFolderOutside(p.work);
      await showAll(p, { detail_reports: path.join(runDir, 'cid', '10014.html'), patches: path.join(runDir, 'patches', '10014-fix.patch') });
      await waitDisplay(page, 'overall_progress', '1 / 5');
    }
    if (['study', 'finished'].includes(state)) {
      await toVerifier(p);
      await waitPhase(page, 'study');
    }
    if (state === 'finished') {
      await toFinished(p);
      await waitStatus(page, '終了');
      await until(page, () => document.querySelector('.display[data-key="results"] table'));
    }
    return fn(ctx);
  });
}

describe('見た目（REQ-030）', () => {
  const SCREENS = [
    ['開始前', 'prep'],
    ['実行中（作業）', 'do'],
    ['終わった後', 'finished'],
  ];

  test('REQ-030 実行中の統括・作業・検証のそれぞれの段で、今の段と各エージェントの状態が文字で出る', async () => {
    for (const [phase, role, label] of [['plan', 'orchestrator', '計画'], ['do', 'worker', '実行'], ['study', 'verifier', '評価']]) {
      await withState(phase, {}, async ({ page }) => {
        assert.match(await page.locator('#phases').innerText(), new RegExp(`${label}[\\s\\S]*（いま）`));
        assert.match(await page.locator(`.agent[data-role="${role}"] .agent-status`).innerText(), /実行中/);
        const statuses = await page.locator('#agents .agent .agent-status').allInnerTexts();
        assert.equal(statuses.length, 3, '統括1・作業1・検証1');
        for (const s of statuses) assert.ok(s.trim() !== '', '状態の札に文字がある');
        await shot(page, `running-${phase}-${role}-1280`);
      });
    }
  });

  test('REQ-030 作業ごとの表示があっても、今の段と各エージェントの状態が、画面を開いてすぐスクロールせずに見える（1280×720・1920×1080）', async () => {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 1920, height: 1080 }]) {
      await withState('do', { viewport }, async ({ page }) => {
        await page.reload();
        await page.waitForFunction(() => document.querySelector('.display[data-key="worker_status"] table'), null, { timeout: 8000 });
        const rects = await page.evaluate(() => {
          const box = (el) => el.getBoundingClientRect();
          return { scrollY: window.scrollY, height: window.innerHeight, phases: box(document.getElementById('phases')).bottom, cards: [...document.querySelectorAll('#agents .agent .agent-status')].map((el) => box(el).bottom) };
        });
        assert.equal(rects.scrollY, 0);
        assert.ok(rects.phases <= rects.height, `${viewport.width}×${viewport.height}：段が見える`);
        for (const bottom of rects.cards) assert.ok(bottom <= rects.height, `${viewport.width}×${viewport.height}：エージェントの状態が見える`);
        await shot(page, `running-do-first-view-${viewport.width}x${viewport.height}`, { fullPage: false });
      });
    }
  });

  for (const [name, state] of SCREENS) {
    test(`REQ-030 ${name}の画面は、幅 390・768・1280・1920 で、重なったり、はみ出したりしない`, async () => {
      await withState(state, { viewport: { width: 1280, height: 900 } }, async ({ page }) => {
        for (const width of [390, 768, 1280, 1920]) {
          await page.setViewportSize({ width, height: 900 });
          await page.waitForTimeout(150);
          assert.deepEqual(await page.evaluate(layoutProblems), [], `${name} ${width}px`);
          await shot(page, `layout-${state}-${width}`);
        }
      });
    });

    test(`REQ-030 ${name}の画面は、文字を200%にしても崩れない（幅を半分にした200%の拡大と、文字だけの200%）`, async () => {
      await withState(state, { viewport: { width: 1280, height: 900 } }, async ({ page }) => {
        await page.setViewportSize({ width: 640, height: 450 });
        await page.waitForTimeout(150);
        assert.deepEqual(await page.evaluate(layoutProblems), [], `${name} 拡大200%（幅640px）`);
        for (const width of [1280, 640]) {
          await page.setViewportSize({ width, height: 900 });
          await page.evaluate(() => {
            document.documentElement.style.fontSize = '200%';
          });
          await page.waitForTimeout(150);
          assert.deepEqual(await page.evaluate(layoutProblems), [], `${name} 文字200%（幅${width}px）`);
          await shot(page, `zoom200-${state}-${width}`);
          await page.evaluate(() => {
            document.documentElement.style.fontSize = '';
          });
        }
        await page.evaluate(() => {
          document.documentElement.style.fontSize = '200%';
        });
        assert.ok((await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('h2')).fontSize))) >= 32, '文字が大きくなっている');
      });
    });

    test(`REQ-030 ${name}の画面の、文字と背景の色の差が 4.5:1 以上（WCAG 2.2）`, async () => {
      await withState(state, {}, async ({ page }) => {
        await page.evaluate(() => document.querySelectorAll('details').forEach((d) => (d.open = true)));
        const { problems, checked } = await page.evaluate(contrastProblems, 4.5);
        assert.ok(checked > 30, `調べた文字の数が少なすぎる（${checked}）`);
        assert.deepEqual(problems, [], `${name} の色の差が足りない文字`);
      });
    });
  }

  test('REQ-030 終わった後の一覧（6列）は、ノートパソコンから大きな画面（1280・1920px）で、横にスクロールしなくても全部の列が見える', async () => {
    const problems = [];
    await withState('finished', { viewport: { width: 1280, height: 900 } }, async ({ page }) => {
      for (const width of [1280, 1920]) {
        await page.setViewportSize({ width, height: 900 });
        await page.waitForTimeout(150);
        const wrap = page.locator('.display[data-key="results"] .table-wrap');
        const m = await wrap.evaluate((el) => {
          const box = el.getBoundingClientRect();
          const hidden = [...el.querySelectorAll('thead th')].filter((th) => th.getBoundingClientRect().right > box.right + 1).map((th) => th.textContent);
          return { width: Math.round(box.width), scroll: el.scrollWidth, client: el.clientWidth, hidden };
        });
        if (CAPTURE) await page.locator('.display[data-key="results"]').screenshot({ path: path.join(CAPTURE, `webui-finished-results-table-${width}.png`) });
        if (m.scroll > m.client + 1) problems.push(`${width}px：一覧の枠（幅 ${m.width}px）に表（幅 ${m.scroll}px）が入らず、横にスクロールしないと「${m.hidden.join('」「')}」の列が見えない`);
      }
    });
    assert.deepEqual(problems, []);
  });

  test('REQ-030 状態は色だけでなく文字でも示される（段の「（いま）」、状態の札、進み具合の割合、⚠ 停止?、終わった理由）', async () => {
    await withState('do', {}, async ({ page }) => {
      assert.match(await page.locator('#phases [data-active="true"]').innerText(), /（いま）/);
      for (const text of await page.locator('#agents .badge').allInnerTexts()) assert.ok(text.trim().length > 0, '札に文字がある');
      for (const key of ['initial_analysis_progress', 'overall_progress']) assert.match(await page.locator(`.display[data-key="${key}"] .progress-text`).innerText(), /\d+%$/, `${key} は割合の文字がある`);
      assert.match(await displayText(page, 'worker_status'), /⚠ 停止\?/);
    });
    await withState('finished', {}, async ({ page }) => {
      assert.match(await page.locator('#run-status').innerText(), /終了/);
      assert.match(await page.locator('body').innerText(), /評価基準をすべて満たした/, '終わった理由が文字で出る');
    });
  });
});
