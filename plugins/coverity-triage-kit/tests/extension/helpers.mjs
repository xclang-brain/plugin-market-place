// Test helpers for the extension: paths, a fake command runner (the batch-run and screen commands, gh),
// sample command outputs in the shapes of design 4.12 / 4.16, and a harness that starts the real adapter
// with a fake SDK, a fake runner and a fake clock. No Python, uv or Copilot CLI is needed.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { createFakeClock } from './fake-clock.mjs';
import { createFakeSdk } from './fake-sdk.mjs';

export const HERE = path.dirname(fileURLToPath(import.meta.url));
export const PLUGIN_DIR = path.resolve(HERE, '..', '..');
export const EXT_DIR = path.join(PLUGIN_DIR, 'com.github.copilot', 'extensions', 'coverity-triage-kit');
export const DEFAULTS_FILE = path.join(EXT_DIR, 'defaults.json');
export const REAL_DEFAULTS = JSON.parse(fs.readFileSync(DEFAULTS_FILE, 'utf8'));
export const PLUGIN_NAME = JSON.parse(fs.readFileSync(path.join(PLUGIN_DIR, 'plugin.json'), 'utf8')).name;
export const TOOL = `${PLUGIN_NAME}_open`;

export const importExt = (rel) => import(pathToFileURL(path.join(EXT_DIR, rel)).href);

export function tempDir(t, prefix = 'ext-test-') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

export const RUN_DIR = path.join(os.tmpdir(), 'out-fake', '20261108-101500');

export const CRITERIA = [
  { id: 'C-01', scope: 'item', text: '呼び出し元の判断：警告の経路が実際に通るかを、呼び出し元までたどって確かめた' },
  { id: 'C-12', scope: 'item', text: '根拠は、実際に読んだコードの場所（リポジトリからのパスと行）で示した' },
  { id: 'C-22', scope: 'item', applies_when: 'MISRA の警告のとき', text: 'MISRA の規則の見方に沿っている' },
  { id: 'C-25', scope: 'run', text: '一覧 summary.html の形が決まりどおり' },
];

export function item(id, over = {}) {
  return {
    id,
    cids: [Number(id) || 0],
    checker: 'NULL_RETURNS',
    status: 'done',
    phase: 'result',
    note: '',
    elapsed_seconds: 312,
    stalled: false,
    preparing_copy: false,
    attempts: 1,
    rework_rounds: 0,
    review: { outcome: 'pass', findings: [{ check: 'C-01', ok: true, detail: '呼び出し元を確かめた' }, { check: 'C-12', ok: true, detail: '場所は正しい', file: 'src/reader.c', line: 42 }] },
    recommendation: 'fix',
    confidence: 'high',
    error: null,
    ...over,
  };
}

export function statusOf(over = {}) {
  return {
    ok: true,
    run_id: path.basename(RUN_DIR),
    run_dir: RUN_DIR,
    repo_root: '/repo',
    stage: 'items',
    stage_label: '警告ごとの作業',
    finished: false,
    stopped: false,
    has_summary: false,
    review: true,
    integration: false,
    max_rework: 1,
    parallel: 3,
    counts: { pending: 1, in_progress: 1, done: 1, error: 0, merged: 0, total: 3 },
    items: [
      item('20001'),
      item('20002', { status: 'in_progress', phase: 'judge', review: null, recommendation: null, confidence: null }),
      item('20003', { status: 'pending', phase: null, review: null, recommendation: null, confidence: null, elapsed_seconds: 0 }),
    ],
    running_tasks: [{ task: 'worker:20002@1', role: 'worker', item: '20002', description: '20002 の作業' }],
    errors: [],
    recent: ['10:15:02 20001 の作業が終わった', '10:10:00 20002 の作業を始めた'],
    initial: { used: false, step: null, value: 0, max: 100, label: '初回解析はしない' },
    progress: { value: 1, max: 3, label: '1 / 3' },
    run_checks: [],
    criteria: CRITERIA,
    criteria_results: [
      { id: 'C-01', scope: 'item', passed: 1, failed: 0, not_applicable: 0, items_failed: [] },
      { id: 'C-12', scope: 'item', passed: 1, failed: 0, not_applicable: 0, items_failed: [] },
    ],
    issues_found: [],
    ...over,
  };
}

// A finished run where every item passed
export function finishedStatus(over = {}) {
  return statusOf({
    stage: 'summary',
    stage_label: '一覧',
    finished: true,
    has_summary: true,
    counts: { pending: 0, in_progress: 0, done: 2, error: 0, merged: 0, total: 2 },
    items: [item('20001', { confidence: 'medium' }), item('G1', { cids: [20004, 20005], confidence: 'low', recommendation: 'deviation' })],
    running_tasks: [],
    progress: { value: 2, max: 2, label: '2 / 2' },
    run_checks: [{ id: 'C-25', ok: true, detail: '形は正しい' }],
    ...over,
  });
}

export function summaryOf(over = {}) {
  return {
    ok: true,
    summary_html: path.join(RUN_DIR, 'summary.html'),
    counts: { warnings: 3, items: 2, groups: 1, fix: 1, deviation: 1, low: 1, errors: 0, unprocessed: 0 },
    approvals_counts: { fix: 1, deviation: 1, reject: 0, none: 0, changed: 0 },
    results: [
      { id: 'G1', title: 'G1（2 件）', cids: [20004, 20005], checker: 'NULL_RETURNS', approval: 'deviation', edited: false, recommendation: 'deviation', confidence: 'low', judgement: 'intentional', summary: '意図的な NULL の確かめ', report: path.join(RUN_DIR, 'cid', 'G1.html'), review: 'pass', already_fixed: false },
      { id: '20001', title: '20001', cids: [20001], checker: 'NULL_RETURNS', approval: 'fix', edited: false, recommendation: 'fix', confidence: 'medium', judgement: 'true_bug', summary: 'fgets の戻り値を見ていない', report: path.join(RUN_DIR, 'cid', '20001.html'), review: 'pass', already_fixed: false },
    ],
    unprocessed: [],
    run_checks: [{ id: 'C-25', ok: true, detail: '形は正しい' }],
    say: ['2 件の作業を終えました。'],
    criteria: CRITERIA,
    criteria_results: [
      { id: 'C-01', scope: 'item', passed: 2, failed: 0, not_applicable: 0, items_failed: [] },
      { id: 'C-25', scope: 'run', passed: 1, failed: 0, not_applicable: 0, items_failed: [] },
    ],
    ...over,
  };
}

export const SETTINGS = {
  ok: true,
  max_rework: 1,
  review: false,
  integration: false,
  initial: false,
  verify: 'none',
  max_items: 100,
  parallel: 3,
  output_dir: path.join(os.tmpdir(), 'out-fake'),
  filters: ['untriaged.yaml', 'misra.yaml'],
  problems: [],
};

export const GUI_URL = `http://127.0.0.1:9999/?t=guitoken#run=${path.basename(RUN_DIR)}&view=summary`;

/**
 * A fake runner. Handlers are keyed 'run:<command>', 'gui:<command>', 'gh:<first arg>'.
 * A handler is a value or a function ({ rest, opts }) => value. A value is:
 *   a JSON object (printed to stdout; ok:false → exit code 1), or { $raw: { code, stdout, stderr, timedOut, spawnError } }.
 */
export function createFakeRunner(defaults = REAL_DEFAULTS) {
  const runName = path.basename(defaults.runCommand);
  const guiName = path.basename(defaults.guiCommand);
  const calls = [];
  const handlers = {
    'run:settings': SETTINGS,
    'run:runs': { ok: true, runs: [] },
    'run:new-run': () => ({
      ok: true,
      run_dir: RUN_DIR,
      repo_root: '/repo',
      work_dir: path.join(RUN_DIR, 'work'),
      limit: 20,
      initial: false,
      review: true,
      integration: false,
      max_rework: 1,
      permission_steps: ['チャットで /add-dir を打つ'],
    }),
    'run:status': () => statusOf(),
    'run:summary': () => summaryOf(),
    'run:stop': { ok: true, stopped_jobs: [], unstopped: [] },
    'gui:open': ({ rest }) => {
      const at = rest.indexOf('--item');
      return { ok: true, url: at >= 0 ? `${GUI_URL}&item=${rest[at + 1]}` : GUI_URL, reused: false, browser_opened: false };
    },
    'gh:--version': { $raw: { code: 0, stdout: 'gh version 2.70.0' } },
    'gh:auth': { $raw: { code: 0, stdout: 'Logged in' } },
    'gh:issue': ({ rest }) => (rest[0] === 'create' ? { $raw: { code: 0, stdout: 'https://github.com/o/r/issues/7\n' } } : { $raw: { code: 0, stdout: '[]' } }),
  };
  const runner = async (opts) => {
    calls.push(opts);
    let key;
    let rest;
    if (opts.command === 'uv') {
      const script = path.basename(opts.args[3] ?? '');
      const kind = script === runName ? 'run' : script === guiName ? 'gui' : script;
      key = `${kind}:${opts.args[4]}`;
      rest = opts.args.slice(5);
    } else {
      key = `${opts.command}:${opts.args[0]}`;
      rest = opts.args.slice(1);
    }
    const h = handlers[key];
    if (h === undefined) return { code: 1, stdout: JSON.stringify({ ok: false, error: `fake runner: no handler for ${key}` }), stderr: '' };
    const value = typeof h === 'function' ? await h({ rest, opts }) : h;
    if (value && value.$raw) return { code: 0, stdout: '', stderr: '', ...value.$raw };
    return { code: value?.ok === false ? 1 : 0, stdout: JSON.stringify(value), stderr: '' };
  };
  runner.calls = calls;
  runner.handlers = handlers;
  runner.on = (key, value) => {
    handlers[key] = value;
  };
  runner.callsOf = (key) =>
    calls.filter((c) => {
      if (c.command === 'uv') {
        const script = path.basename(c.args[3] ?? '');
        const kind = script === runName ? 'run' : script === guiName ? 'gui' : script;
        return `${kind}:${c.args[4]}` === key;
      }
      return `${c.command}:${c.args[0]}` === key;
    });
  return runner;
}

// Agent definitions the defaults point to (only `model:` matters to the extension)
const MODEL_LINES = {
  orchestrator: 'model: [claude-sonnet-5.5, gpt-6.1-sol]',
  worker: 'model: [gpt-6-luna, claude-haiku-5.5]',
  reviewer: 'model: [claude-sonnet-5.5, gpt-6.1-sol]',
  integrator: 'model: [gpt-6-luna, claude-haiku-5.5]',
};

/**
 * Starts the real adapter in a temporary plugin layout.
 * @returns {{ fake, app, clock, runner, dirs, defaults, open, state, start, stop, idle }}
 */
export async function startWith(t, options = {}) {
  const root = tempDir(t);
  const pluginRoot = path.join(root, 'plugin');
  const dataRoot = path.join(root, 'data', 'runs');
  const cwd = options.cwd ?? path.join(root, 'repo');
  fs.mkdirSync(path.join(pluginRoot, 'com.github.copilot', 'agents'), { recursive: true });
  fs.mkdirSync(cwd, { recursive: true });
  fs.writeFileSync(path.join(pluginRoot, 'plugin.json'), JSON.stringify({ name: PLUGIN_NAME, version: '0.1.0' }));
  const defaults = { ...REAL_DEFAULTS, ...(options.defaults ?? {}), roles: { ...REAL_DEFAULTS.roles, ...(options.defaults?.roles ?? {}) } };
  const defaultsFile = path.join(root, 'defaults.json');
  fs.writeFileSync(defaultsFile, JSON.stringify(defaults, null, 2));
  const ids = { orchestrator: defaults.orchestrator, ...defaults.roles };
  for (const [role, id] of Object.entries(ids)) {
    const stem = String(id).split(':').pop();
    fs.writeFileSync(path.join(pluginRoot, 'com.github.copilot', 'agents', `${stem}.agent.md`), `---\ndescription: ${role}\n${MODEL_LINES[role]}\n---\n本文\n`);
  }
  const clock = options.clock ?? createFakeClock();
  const runner = options.runner ?? createFakeRunner(defaults);
  const fake = createFakeSdk({ cwd, sessionId: options.sessionId ?? 'session-fake-1' });
  if (options.beforeStart) options.beforeStart({ fake, runner, dataRoot, pluginRoot });
  const { startAdapter } = await importExt('adapter.mjs');
  const app = await startAdapter({ joinSession: fake.joinSession, pluginRoot, defaultsFile, dataRoot, runner, clock, preferredPort: 0, ghCommand: 'gh' });
  t.after(() => app.shutdown());
  const core = app.core;
  // settles the queue; jobs that wait on the (fake) clock get time in 250 ms steps, at most 20 s
  const idle = async () => {
    for (let i = 0; i < 80; i += 1) {
      await clock.flush?.();
      if (core.context.queue.pending === 0) break;
      if (clock.advance) await clock.advance(250);
      else await new Promise((resolve) => setTimeout(resolve, 20));
    }
    await clock.flush?.();
  };
  return {
    fake,
    app,
    core,
    ctx: core.context,
    clock,
    runner,
    defaults,
    dirs: { root, pluginRoot, dataRoot, cwd },
    idle,
    open: (requestText = 'Coverity の警告をトリアージして') => fake.callTool(TOOL, { requestText }),
    state: (opts) => core.getState(opts),
    start: (body = {}) => core.start(body),
    stop: () => core.stop(),
  };
}

// Opens the screen, waits for the settings, and starts a run. Returns the root agent id.
export async function openAndStart(h, body = {}) {
  await h.open();
  await h.idle();
  await h.clock.flush();
  await h.core.start(body);
  await h.idle();
  return h.ctx.run?.rootId ?? null;
}
