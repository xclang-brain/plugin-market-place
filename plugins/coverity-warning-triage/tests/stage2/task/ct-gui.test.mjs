// 2段目 部品の試験（ct.py gui の画面）。設定・結果・監視の画面を、人と同じ操作で Playwright から試す。
//
// 実際の利用にできるだけ近づけるため、次のものを本物のまま使う（モデルは使わない）。
//   - Coverity Connect のエミュレーター（coverity_emulator.server）と、題材の登録（coverity_emulator.seed）。
//     envmon の git と svn のリポジトリ、解析したリビジョンと最新のリビジョン、/build/agent/work/envmon のパスの警告。
//   - MCP サーバー coverity-triage を、mcp.json と同じく stdio で起動する（uv run --native-tls --frozen --quiet
//     --directory <server> coverity-triage-mcp。UV_PROJECT_ENVIRONMENT は一時のフォルダ）。
//   - ct.py を、本物と同じく `uv run --native-tls <ct.py>` で動かす（init-config、new-run、tick、summary、gui）。
//   - 実行フォルダは、統括エージェントの代わりに、この試験が ct.py tick の指示どおりに動いて作る。
//     取得エージェントの仕事は MCP サーバーを呼び、作業エージェントの仕事は決まった result.json と修正を書く。
//   - 画面は `ct.py gui --no-browser`（裏で起動して、合言葉つきの URL を受け取る）で開く。svn の画面は --foreground。
//   - 模擬の解析コマンド（coverity_emulator/bin）を PATH の先頭に置く。
// プラグインの中には何も作らない（server/.venv も作らない）。一時のフォルダは、試験の終わりに消す。
//
// 動かし方：python3 tests/run.py（Playwright とブラウザー、uv、git が要る。無ければ理由を示して skip）。
//   直接動かすとき：node --test tests/stage2/task/ct-gui.test.mjs
//   ブラウザーは tests/stage2/ui/screen.test.mjs と同じく、PLUGIN_TEST_BROWSER_CHANNEL（chrome か msedge）、なければ Chromium。
//   CWT_CAPTURE_DIR=<フォルダ> を付けると、主な場面の写真（png、deviceScaleFactor 2）をそこに残す。
//
// 不具合を見つけた試験も、正しい振る舞いを期待して書いてある（不合格のまま残す）。
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { after, before, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { contrastProblems, layoutProblems } from '../ui/page-checks.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN = path.resolve(HERE, '..', '..', '..');
const CT = path.join(PLUGIN, 'skills', 'coverity-triage-scripts', 'scripts', 'ct.py');
const SERVER_DIR = path.join(PLUGIN, 'server');
const EMU_BIN = path.join(HERE, 'coverity_emulator', 'bin');
const CAPTURE = process.env.CWT_CAPTURE_DIR || '';
const USER = 'triager';
const KEY = 'emu-triager-key-0001';
const STREAM = 'envmon-main';
const REMOTE_ROOT = '/build/agent/work/envmon/';
const MIN = 60;

// ---- 道具を探す（無ければ、理由を示して skip） ---------------------------------------------------

function which(name) {
  for (const dir of (process.env.PATH ?? '').split(path.delimiter)) {
    for (const ext of process.platform === 'win32' ? ['.exe', '.cmd', ''] : ['']) {
      const full = path.join(dir, name + ext);
      try {
        if (fs.statSync(full).isFile()) return full;
      } catch {
        // 次へ
      }
    }
  }
  return null;
}

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
  return null;
}

const UV = which('uv');
const GIT = which('git');
const PYTHON = which('python3') ?? which('python');
const SVN = which('svn') && which('svnadmin');
const chromium = await loadPlaywright();
const SKIP = !chromium ? 'Playwright が見つかりません（npm install --prefix tests --no-save --no-package-lock playwright@1.56.1）'
  : !UV ? 'uv が見つかりません（ct.py と MCP サーバーを本物と同じく uv で動かすため）'
    : !GIT ? 'git が見つかりません' : !PYTHON ? 'python3 が見つかりません（Coverity のエミュレーターに要る）' : '';

async function launchBrowser() {
  const channel = process.env.PLUGIN_TEST_BROWSER_CHANNEL;
  const failures = [];
  if (channel && channel !== 'chromium') {
    try {
      return await chromium.launch({ channel });
    } catch (error) {
      failures.push(`${channel}：${String(error.message).split('\n')[0]}`);
    }
  }
  try {
    return await chromium.launch();
  } catch (error) {
    failures.push(`Playwright の Chromium：${error.message}`);
    throw new Error(`ブラウザーを起動できません。\n${failures.join('\n')}`);
  }
}

// ---- 一時のフォルダと、子のプロセス -------------------------------------------------------------

const W = { done: {} };  // 試験の世界：フォルダ、URL、実行フォルダ
const children = [];   // 終わりに止めるプロセス
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const writeJson = (file, data) => {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(data, null, 1), 'utf8');
};
const iso = (epochSeconds) => {
  const d = new Date(epochSeconds * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};
const now = () => Date.now() / 1000;

function run(command, args, { cwd, env, input, timeout = 300_000 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: cwd ?? W.tmp, env: env ?? W.env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (c) => { stdout += c; });
    child.stderr.on('data', (c) => { stderr += c; });
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new Error(`${path.basename(command)} ${args.slice(0, 6).join(' ')} が ${timeout / 1000} 秒で終わらない\n${stderr.slice(-1500)}`));
    }, timeout);
    child.on('error', (error) => { clearTimeout(timer); reject(error); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code, stdout, stderr }); });
    if (input != null) child.stdin.end(input); else child.stdin.end();
  });
}

/** ct.py を本物と同じく uv で動かし、標準出力の JSON を返す（ok: true でなければ例外）。 */
async function ct(args, { ok = true, env } = {}) {
  const r = await run(UV, ['run', '--native-tls', '--quiet', CT, ...args], { env });
  let data;
  try {
    data = JSON.parse(r.stdout);
  } catch {
    throw new Error(`ct.py ${args.join(' ')} の標準出力が JSON 1つではありません:\n${r.stdout.slice(0, 2000)}\n${r.stderr.slice(-2000)}`);
  }
  assert.doesNotMatch(r.stdout + r.stderr, /Traceback/, `ct.py ${args[0]} が Traceback を出した`);
  if (ok && data.ok !== true) throw new Error(`ct.py ${args.join(' ')} が失敗しました: ${JSON.stringify(data).slice(0, 3000)}`);
  return data;
}

async function git(cwd, ...args) {
  const r = await run(GIT, args, { cwd });
  if (r.code !== 0) throw new Error(`git ${args.join(' ')}: ${r.stderr}`);
  return r.stdout.trim();
}

/** 最初の1行（JSON）を出して動き続けるプロセスを起動する。 */
function startServer(command, args, { cwd, env, what }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] });
    children.push(child);
    let out = '';
    let err = '';
    const timer = setTimeout(() => reject(new Error(`${what}が 60 秒で起動しない: ${err.slice(-1500)}`)), 60_000);
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (c) => { err = (err + c).slice(-20000); });
    child.stdout.on('data', (c) => {
      out += c;
      const i = out.indexOf('\n');
      if (i >= 0) {
        clearTimeout(timer);
        try {
          resolve({ child, info: JSON.parse(out.slice(0, i)) });
        } catch (error) {
          reject(new Error(`${what}の最初の行が JSON ではない: ${out.slice(0, 500)}`));
        }
      }
    });
    child.on('exit', (code) => { clearTimeout(timer); reject(new Error(`${what}が終了した（${code}）: ${err.slice(-1500)}`)); });
  });
}

// ---- MCP サーバー（本物と同じく stdio。改行で区切った JSON-RPC） ------------------------------------

class Mcp {
  constructor(env) {
    this.env = env;
    this.id = 1;
    this.pending = new Map();
    this.buffer = '';
    this.stderr = '';
  }

  async start() {
    this.proc = spawn(UV, ['run', '--native-tls', '--frozen', '--quiet', '--directory', SERVER_DIR, 'coverity-triage-mcp'],
      { cwd: W.tmp, env: this.env, stdio: ['pipe', 'pipe', 'pipe'] });
    children.push(this.proc);
    this.proc.stdout.setEncoding('utf8');
    this.proc.stderr.setEncoding('utf8');
    this.proc.stderr.on('data', (c) => { this.stderr = (this.stderr + c).slice(-20000); });
    this.proc.stdout.on('data', (chunk) => {
      this.buffer += chunk;
      let i;
      while ((i = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, i);
        this.buffer = this.buffer.slice(i + 1);
        let message;
        try {
          message = JSON.parse(line);
        } catch {
          continue;
        }
        const waiter = this.pending.get(message.id);
        if (waiter) {
          this.pending.delete(message.id);
          waiter(message);
        }
      }
    });
    const init = await this.request('initialize', { protocolVersion: '2025-11-25', capabilities: {},
      clientInfo: { name: 'ct-gui-test', version: '1' } });
    assert.equal(init.result?.serverInfo?.name, 'coverity-triage', JSON.stringify(init));
    this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  }

  request(method, params, timeout = 120_000) {
    const id = this.id++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`MCP の ${method} が ${timeout / 1000} 秒で答えない: ${this.stderr.slice(-1500)}`)), timeout);
      this.pending.set(id, (message) => { clearTimeout(timer); resolve(message); });
      this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  async call(name, args) {
    const message = await this.request('tools/call', { name, arguments: args });
    if (message.error) throw new Error(`MCP の ${name}: ${JSON.stringify(message.error)}`);
    const result = message.result;
    const text = (result.content ?? []).map((c) => c.text ?? '').join('\n');
    if (result.isError) throw new Error(`MCP の ${name} がエラー: ${text}`);
    return result.structuredContent ?? JSON.parse(text || '{}');
  }

  /** done が false の間、同じ引数で呼び直す（取得エージェントの指示書のとおり）。 */
  async untilDone(name, args) {
    for (let n = 0; n < 30; n++) {
      const data = await this.call(name, args);
      if (data.done !== false) return data;
    }
    throw new Error(`MCP の ${name} が 30 回呼んでも done にならない`);
  }

  close() {
    try {
      this.proc.stdin.end();
    } catch {
      // もう止まっている
    }
    setTimeout(() => this.proc.kill('SIGKILL'), 3000).unref();
  }
}

// ---- 統括エージェントの代わり：ct.py tick の指示どおりに動く ---------------------------------------

const FIXED_LINE = (cid) => `/* coverity-triage: CID ${cid} を直した（試験の修正） */`;

function resultJson(item, spec, main) {
  const evidence = main ? [{ file: main.file, line: main.line ?? null, note: '警告の場所' }] : [];
  return {
    item,
    verdict: { judgement: spec.rec === 'fix' ? 'true_bug' : 'false_positive', summary: spec.summary ?? `${item} の戻り値を確かめずに使っている`,
      rationale: spec.rationale ?? '呼び出し元から定義まで警告経路をたどって確かめた', evidence },
    recommendation: spec.rec, confidence: spec.conf, confidence_reason: spec.reason ?? '経路をすべて読んだ',
    deviation: { classification: 'False Positive', action: 'Ignore', severity: 'Unspecified',
      comment: spec.comment ?? `呼び出し元 ${main ? `${main.file}:${main.line}` : 'src/app/main.c:1'} で値を確かめているため問題ない` },
    fix: { classification: 'Bug', action: 'Fix Required', severity: 'Major', summary: spec.fixSummary ?? '戻り値を確かめる',
      impact: '異常のときだけ動きが変わる', exceeded_constraints: [], already_fixed_on_latest: !!spec.alreadyFixed },
    revision_drift: { status: 'none', detail: '' },
    group_excluded_cids: [],
  };
}

function mappedIssues(runDir) {
  const out = new Map();
  for (const issue of readJson(path.join(runDir, 'issues.mapped.json'))) out.set(issue.cid, issue);
  return out;
}

/** 作業エージェントの仕事：修正用のコピーを直し、progress.json と result.json を書く。 */
function doWork(runDir, itemId, spec) {
  const state = readJson(path.join(runDir, 'run.json'));
  const item = state.items.find((i) => i.id === itemId);
  const fixDir = path.join(runDir, 'work', `fix-${item.slot}`);
  const main = mappedIssues(runDir).get(item.cids[0]);
  if (!spec.alreadyFixed) {
    const target = main && fs.existsSync(path.join(fixDir, main.file)) ? main.file : 'src/app/main.c';
    const file = path.join(fixDir, target);
    const lines = fs.readFileSync(file, 'utf8').split('\n');
    const at = Math.max(0, Math.min(lines.length, (main?.line ?? 1) - 1));
    lines.splice(at, 0, FIXED_LINE(item.cids[0]));
    fs.writeFileSync(file, lines.join('\n'), 'utf8');
  }
  for (const [rel, text] of Object.entries(spec.newFiles ?? {})) {
    fs.mkdirSync(path.dirname(path.join(fixDir, rel)), { recursive: true });
    fs.writeFileSync(path.join(fixDir, rel), text, 'utf8');
  }
  const folder = path.join(runDir, 'items', itemId);
  writeJson(path.join(folder, 'progress.json'), { item: itemId, phase: 'result', note: '結果を書いた' });
  writeJson(path.join(folder, 'result.json'), resultJson(itemId, spec, main));
}

/**
 * ct.py tick を、actions がなくなるまで（または held だけが残るまで）進める。
 * worker(itemId, n) は {rec, conf, ...}、{fail: 理由}、{hold: true, progress?} のどれかを返す。
 * reviewer(itemId, n) は ReviewResult か {hold: true} を返す。
 */
async function drive(runDir, mcp, { worker, reviewer, start = ['--wait', '0'] } = {}) {
  const state = () => readJson(path.join(runDir, 'run.json'));
  const repo = state().repo_root;
  let last = await ct(['tick', '--run', runDir, ...start]);
  const queue = [...last.actions];
  const held = [];
  let workers = 0;
  let reviews = 0;
  for (let guard = 0; guard < 200; guard++) {
    if (last.done || last.stop) break;
    const action = queue.shift();
    if (!action) {
      // 起動したあと、tick をもう一度呼ぶ（コピーを作る前に返した tick の続き。手順書 coverity-run のとおり）
      last = await ct(['tick', '--run', runDir, '--wait', '0']);
      if (!last.actions.length && !last.done) {
        if (held.length) break;
        throw new Error(`tick が何も指示しない: ${JSON.stringify(last).slice(0, 2000)}`);
      }
      queue.push(...last.actions);
      continue;
    }
    let outcome = ['--finished', action.task];
    if (action.type === 'launch' && action.agent === 'coverity-triage-collector') {
      if (action.task.startsWith('collector:search')) {
        await mcp.untilDone('search_issues', { repo_root: repo, filter_file: path.join(runDir, 'filter.json'),
          output_file: path.join(runDir, 'issues.json') });
      } else {
        const s = state();
        for (const item of s.items.filter((i) => i.status === 'pending')) {
          const folder = path.join(runDir, 'items', item.id);
          const cids = item.cids.filter((c) => !fs.existsSync(path.join(folder, `issue-${c}.json`)));
          if (cids.length) await mcp.untilDone('get_issues', { repo_root: repo, stream: s.stream, cids, output_dir: folder });
        }
      }
    } else if (action.type === 'launch' && action.agent === 'coverity-triage-worker') {
      const spec = worker(action.item, workers++);
      if (spec.hold) {
        if (spec.progress) writeJson(path.join(runDir, 'items', action.item, 'progress.json'), { item: action.item, ...spec.progress });
        held.push(action);
        continue;
      }
      if (spec.fail) outcome = ['--failed', action.task, '--reason', spec.fail];
      else doWork(runDir, action.item, spec);
    } else if (action.type === 'launch' && action.agent === 'coverity-triage-integrator') {
      writeJson(path.join(runDir, 'integration', 'plan.json'), { merges: [], conflicts: [] });
    } else if (action.type === 'launch' && action.agent === 'coverity-triage-reviewer') {
      const review = reviewer(action.item, reviews++);
      if (review.hold) {
        held.push(action);
        continue;
      }
      writeJson(path.join(runDir, 'items', action.item, 'review.json'), review);
    } else {
      throw new Error(`この試験が知らない指示: ${JSON.stringify(action)}`);
    }
    last = await ct(['tick', '--run', runDir, ...outcome]);
    queue.push(...last.actions);
  }
  return { last, held };
}

const passReview = (item) => ({ item, outcome: 'pass', findings: [{ check: '根拠の行', ok: true, detail: '根拠の行と経路の条件を確かめた' }],
  confidence_cap: null, rework: [], note: '根拠の行と経路の条件を確かめた' });

// ---- 世界を作る ---------------------------------------------------------------------------------

const LONG_TOKEN = 'A'.repeat(320);
const LONG_SUMMARY = `とても長い見立て：${'呼び出し元の関数 config_apply から定義 ini_parse までをたどり、'.repeat(12)}${LONG_TOKEN}`;
const XSS_SUMMARY = '見立てに HTML <img src=x onerror="window.__xss=2"> と <script>window.__xss=3</script> を含む';
const XSS_COMMENT = '逸脱コメント <b>太字</b> <script>window.__xss=5</script>';
const XSS_FILE = 'src/<img src=x onerror=window.__xss=4>.c';
const JA_FILE = 'src/センサー 補助/値 の変換.c';
const FAIL_REASON = '作業エージェントが途中で止まりました <b>太字</b>（試験で止めた）';
const XSS_FILTER_NAME = '失敗あり <script>window.__xss=1</script>';

const DONE_SPECS = [
  { rec: 'fix', conf: 'high', newFiles: { [JA_FILE]: '/* 日本語と空白の名前のファイル */\nint sensor_convert(int v) { return v * 2; }\n' } },
  { rec: 'deviation', conf: 'low', summary: XSS_SUMMARY, rationale: `根拠 ${XSS_SUMMARY}`, comment: XSS_COMMENT },
  { rec: 'fix', conf: 'medium', alreadyFixed: true },
  { rec: 'deviation', conf: 'high', summary: LONG_SUMMARY },
  { rec: 'fix', conf: 'low', newFiles: { [XSS_FILE]: '/* <script>window.__xss=6</script> */\nint x;\n' } },
];
const DONE_ROLES = ['ja', 'xss', 'already', 'long', 'xssFile'];
const doneSpec = (item, n) => {
  if (DONE_ROLES[n]) W.done[DONE_ROLES[n]] = item;
  return DONE_SPECS[n] ?? { rec: n % 2 ? 'deviation' : 'fix', conf: 'medium' };
};

function filterYaml(data) {
  return JSON.stringify(data, null, 1) + '\n';   // JSON は YAML としても読める
}

async function makeWorld() {
  const started = Date.now();
  W.tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ct-gui-test-')));
  W.base = path.join(W.tmp, '作業 フォルダ（日本語）');
  W.repo = path.join(W.base, 'envmon リポジトリ');
  W.out = path.join(W.base, '結果 出力');
  fs.mkdirSync(path.join(W.tmp, 'tmp'), { recursive: true });
  fs.writeFileSync(path.join(W.tmp, 'gitconfig'), '', 'utf8');
  const env = { ...process.env };
  for (const name of ['COV_USER', 'COV_AUTH_KEY', 'COVERITY_PASSPHRASE', 'VIRTUAL_ENV', 'CT_GUI_TOKEN']) delete env[name];
  W.env = {
    ...env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8', TMPDIR: path.join(W.tmp, 'tmp'),
    CT_GUI_STATE_DIR: path.join(W.tmp, 'gui-state'), COPILOT_HOME: path.join(W.tmp, 'copilot'),
    GIT_CONFIG_GLOBAL: path.join(W.tmp, 'gitconfig'), GIT_CONFIG_NOSYSTEM: '1', GIT_TERMINAL_PROMPT: '0',
    GIT_AUTHOR_NAME: 'tester', GIT_AUTHOR_EMAIL: 'tester@localhost', GIT_COMMITTER_NAME: 'tester', GIT_COMMITTER_EMAIL: 'tester@localhost',
    PATH: EMU_BIN + path.delimiter + (process.env.PATH ?? ''), COV_USER: USER, COV_AUTH_KEY: KEY,
    UV_PROJECT_ENVIRONMENT: path.join(W.tmp, 'uv-mcp-env'),
  };

  // Coverity Connect のエミュレーターと題材
  const emu = await startServer(PYTHON, ['-m', 'coverity_emulator.server', '--port', '0', '--data', path.join(W.tmp, 'emu')],
    { cwd: HERE, env: { ...W.env }, what: 'エミュレーター' });
  W.emuUrl = emu.info.url;
  const seeded = await run(PYTHON, ['-m', 'coverity_emulator.seed', '--url', W.emuUrl, '--workdir', path.join(W.tmp, 'seed'),
    '--no-large-sample', ...(SVN ? [] : ['--no-svn'])], { cwd: HERE, timeout: 600_000 });
  if (seeded.code !== 0) throw new Error(`seed が失敗: ${seeded.stderr.slice(-3000)}`);
  W.seed = JSON.parse(seeded.stdout);

  // 日本語と空白のあるパスに、利用者のリポジトリを置く（origin は題材の git リポジトリ）
  fs.mkdirSync(W.base, { recursive: true });
  await git(W.base, 'clone', '--quiet', W.seed.git.path, W.repo);
  W.latest = await git(W.repo, 'rev-parse', 'HEAD');
  W.analyzed = W.seed.git.analyzed;
  await ct(['init-config', '--repo', W.repo, '--url', W.emuUrl, '--project', 'envmon', '--stream', STREAM,
    '--output-dir', '../結果 出力', '--strip-prefix', REMOTE_ROOT]);
  await ct(['set-verify', '--repo', W.repo, '--build-command', 'make', '--default', 'none', '--no-initial']);
  const filters = path.join(W.repo, '.coverity-triage', 'filters');
  const flt = (name, data) => fs.writeFileSync(path.join(filters, name), filterYaml({ project: 'envmon', streams: [STREAM], ...data }), 'utf8');
  flt('gui-done.yaml', { name: '終わった実行', checkers: ['NULL_RETURNS', 'RESOURCE_LEAK', 'DIVIDE_BY_ZERO'], limit: 20 });
  flt('gui-fail.yaml', { name: XSS_FILTER_NAME, checkers: ['FORWARD_NULL', 'REVERSE_INULL'], limit: 20 });
  flt('gui-review.yaml', { name: '検証つき', checkers: ['UNINIT_CTOR', 'ALLOC_FREE_MISMATCH', 'CHECKED_RETURN'], limit: 20 });
  flt('gui-none.yaml', { name: '当たらない条件', checkers: ['NO_SUCH_CHECKER'] });
  flt('gui-mid.yaml', { name: '途中の実行', checkers: ['OVERRUN', 'UNINIT'], limit: 20 });
  W.configText = fs.readFileSync(path.join(W.repo, '.coverity-triage', 'config.yaml'), 'utf8');

  const mcp = new Mcp(W.env);
  await mcp.start();
  try {
    const newRun = async (filter, ...extra) => (await ct(['new-run', '--repo', W.repo, '--filter', filter, ...extra])).run_dir;

    // 1. 終わった実行（修正・逸脱、確信度の高・中・低、最新で直っている、HTML の入った見立て、長い見立て）
    W.runDone = await newRun('gui-done.yaml');
    await drive(W.runDone, mcp, { worker: doneSpec });
    await ct(['summary', '--run', W.runDone]);

    // 2. 失敗した作業のある実行（条件の名前に HTML）
    W.runFail = await newRun('gui-fail.yaml');
    await drive(W.runFail, mcp, { worker: (_, n) => (n === 1 ? { fail: FAIL_REASON } : { rec: n % 2 ? 'deviation' : 'fix', conf: 'medium' }) });
    await ct(['summary', '--run', W.runFail]);

    // 3. 検証つきの実行（統合と検証をオン。検証の途中で止める＝一覧はまだない）
    const configFile = path.join(W.repo, '.coverity-triage', 'config.yaml');
    fs.writeFileSync(configFile, W.configText + 'agents:\n  integration: true\n  review: true\n  max_rework: 1\n', 'utf8');
    W.runReview = await newRun('gui-review.yaml');
    fs.writeFileSync(configFile, W.configText, 'utf8');
    W.reviewHeld = (await drive(W.runReview, mcp, {
      worker: (_, n) => ({ rec: n % 2 ? 'deviation' : 'fix', conf: n % 2 ? 'medium' : 'high' }),
      reviewer: (item, n) => (n === 0 ? passReview(item) : { hold: true }),
    })).held;

    // 4. 作業が 0 件の実行
    W.runNone = await newRun('gui-none.yaml');
    await drive(W.runNone, mcp, {});
    await ct(['summary', '--run', W.runNone]);

    // 5. 途中の実行（1 件終わり、3 件が作業中）
    W.runMid = await newRun('gui-mid.yaml');
    W.midHeld = (await drive(W.runMid, mcp, {
      worker: (_, n) => (n === 0 ? { rec: 'fix', conf: 'high' }
        : { hold: true, progress: n === 1 ? { phase: 'read', note: '呼び出し元を読んでいる' } : n === 3 ? { phase: 'fix', note: '修正を作っている' } : null }),
    })).held;
  } finally {
    mcp.close();
  }

  // 6. 初回解析の途中の実行（初回解析の状態は、ct.py initial-run が書く形で置く）
  W.runInit = (await ct(['new-run', '--repo', W.repo, '--filter', 'untriaged.yaml', '--initial'])).run_dir;
  setInitial(W.runInit, { started: now() - 200, logAge: 5 });

  // 画面（裏で起動して、合言葉つきの URL を受け取る）
  W.launch = await ct(['gui', '--repo', W.repo, '--no-browser']);
  const url = new URL(W.launch.url);
  W.port = Number(url.port);
  W.token = url.searchParams.get('t');
  W.gui = `http://127.0.0.1:${W.port}/?t=${W.token}`;
  W.setupSeconds = Math.round((Date.now() - started) / 1000);
}

function setInitial(runDir, { started, logAge, step = 'analyze' }) {
  const folder = path.join(runDir, 'analysis');
  writeJson(path.join(folder, 'status.json'), { started, pid: process.pid, dirty: false, status: 'running', step });
  const log = path.join(folder, 'initial.log');
  fs.writeFileSync(log, '$ cov-build --dir idir make\n__CT_STEP__ build\nmake: 完了\n__CT_STEP__ analyze\n[STATUS] 解析しています\n', 'utf8');
  const t = now() - logAge;
  fs.utimesSync(log, t, t);
}

const runId = (dir) => path.basename(dir);

// ---- 画面の道具 ----------------------------------------------------------------------------------

let browser;

async function open(hash = '', { width = 1280, height = 900, colorScheme = 'light', scale } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: scale ?? (CAPTURE ? 2 : 1),
    colorScheme, locale: 'ja-JP' });
  const page = await context.newPage();
  const errors = [];
  const dialogs = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('dialog', (dialog) => {
    dialogs.push(`${dialog.type()}: ${dialog.message()}`);
    dialog.dismiss().catch(() => {});
  });
  await page.addInitScript(() => { window.__xss = 0; });
  await page.goto(W.gui + hash);
  return { page, context, errors, dialogs, close: () => context.close() };
}

async function shot(page, name, { fullPage = true } = {}) {
  if (!CAPTURE) return;
  fs.mkdirSync(CAPTURE, { recursive: true });
  await page.screenshot({ path: path.join(CAPTURE, `${name}.png`), fullPage });
}

const text = (page, selector) => page.locator(selector).first().innerText();

async function waitFor(page, fn, arg, timeout = 15_000, what = '') {
  try {
    await page.waitForFunction(fn, arg, { timeout, polling: 200 });
  } catch (error) {
    const body = await page.evaluate(() => document.body.innerText.slice(0, 3000)).catch(() => '');
    throw new Error(`待っても${what || '表示'}が変わらない（${timeout / 1000} 秒）\n--- 画面の文字 ---\n${body}`);
  }
}

const waitText = (page, selector, needle, timeout, what) => waitFor(page, ([s, n]) => {
  const node = document.querySelector(s);
  return !!node && node.innerText.includes(n);
}, [selector, needle], timeout, what || `「${needle}」（${selector}）`);

async function settingsReady(page) {
  await waitFor(page, () => document.querySelector('#repo')?.textContent !== '読み込んでいます…'
    && document.querySelector('#f-url')?.value !== undefined && document.readyState === 'complete', null, 15_000, '設定の読み込み');
  await page.waitForTimeout(300);
}
/** 設定の画面が読み込み終わるか、読み込めなかったことを示すまで待つ（異常の試験で、止まった画面を待ち続けないため）。 */
async function settingsSettled(page) {
  await waitFor(page, () => document.querySelector('#repo')?.textContent !== '読み込んでいます…'
    || (document.querySelector('#banners')?.innerText ?? '').trim() !== '', null, 15_000, '設定の読み込み');
  await page.waitForTimeout(300);
}
async function monitorReady(page) {
  await waitText(page, '#monUpdated', 'に更新', 15_000, '監視の更新');
}
async function summaryReady(page) {
  await waitFor(page, () => !!document.querySelector('#sumTiles .tile') || !!document.querySelector('#resBody .note.warn'), null, 15_000, '結果の一覧');
}
async function detailReady(page) {
  await waitFor(page, () => !!document.querySelector('aside.apanel') || !!document.querySelector('#resBody .note.warn'), null, 15_000, '詳細');
}

/**
 * page-checks の崩れの検査。次は除く。
 *  - 入力欄の中で文字が横に流れる（入力欄のふつうの動き）。コードの枠（差分・修正後のファイル）の中の横のスクロール。
 *  - 上の帯のリポジトリのパスと、設定のタブの帯（どの画面にも出るため、専用の試験で確かめる）。
 * 画面の横スクロールと、ほかのはみ出し・重なりは、そのまま数える。
 */
const SHARED_PARTS = /div\.repo「|nav\.tabs「|button「グループ化しない CID」/;
async function layout(page, { shared = false } = {}) {
  return (await page.evaluate(layoutProblems))
    .filter((p) => !/^文字が切れる：(input|textarea|select|pre\.(diff|code))/.test(p))
    .filter((p) => shared || !SHARED_PARTS.test(p));
}

/** この試験が入れた HTML・script が動いていないこと。 */
async function assertNoScript(view, where) {
  assert.equal(await view.page.evaluate(() => window.__xss), 0, `${where}: 文字の中の script が動いた`);
  assert.deepEqual(view.dialogs.filter((d) => d.startsWith('alert')), [], `${where}: alert が出た`);
}

// ---- HTTP の道具 ---------------------------------------------------------------------------------

function request({ method = 'GET', target = '/', headers = {}, body = null, port, host = '127.0.0.1', timeout = 8000 }) {
  return new Promise((resolve, reject) => {
    const length = body != null ? { 'Content-Length': Buffer.byteLength(body) } : {};
    const req = http.request({ host, port: port ?? W.port, method, path: target, agent: false,
      headers: { Host: `127.0.0.1:${port ?? W.port}`, ...length, ...headers } }, (res) => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { data += c; });
      res.on('end', () => {
        let json = null;
        try {
          json = JSON.parse(data);
        } catch {
          // 文字のまま
        }
        resolve({ status: res.statusCode, headers: res.headers, text: data, json });
      });
    });
    req.setTimeout(timeout, () => req.destroy(new Error('TIMEOUT')));
    req.on('error', reject);
    if (body != null) req.write(body);
    req.end();
  });
}
const api = (target, opts = {}) => request({ target, ...opts, headers: { 'X-CT-Token': W.token, ...(opts.headers ?? {}) } });
const post = (target, data, opts = {}) => api(target, { method: 'POST', body: JSON.stringify(data), ...opts,
  headers: { 'Content-Type': 'application/json', ...(opts.headers ?? {}) } });

/** 生の HTTP の要求を送り、答え（または TIMEOUT）を返す（Content-Length を自由に書くため）。all なら timeout まで全部を集める。 */
function rawRequest(textToSend, { port, timeout = 4000, all = false } = {}) {
  return new Promise((resolve) => {
    const socket = net.connect(port ?? W.port, '127.0.0.1');
    let data = '';
    const done = (value) => { socket.destroy(); resolve(value); };
    socket.setEncoding('utf8');
    socket.on('connect', () => socket.write(textToSend));
    socket.on('data', (c) => { data += c; if (!all && /\r\n\r\n/.test(data) && data.length > 20) setTimeout(() => done(data), 100); });
    socket.on('error', (error) => done(`ERROR ${error.code}`));
    setTimeout(() => done(data || 'TIMEOUT'), timeout);
  });
}

function approvals(runDir) {
  const file = path.join(runDir, 'approvals.json');
  return fs.existsSync(file) ? readJson(file).items : null;
}

/** 実行フォルダを別の名前に写す（異常なファイルを置くため）。 */
function copyRun(src, name) {
  const dest = path.join(W.out, name);
  fs.cpSync(src, dest, { recursive: true });
  const state = readJson(path.join(dest, 'run.json'));
  state.run_id = name;
  writeJson(path.join(dest, 'run.json'), state);
  return dest;
}

/** 設定のファイルを一時的に書き換え、終わったら元に戻す。 */
async function withFiles(changes, body) {
  const saved = [];
  for (const [file, content] of Object.entries(changes)) {
    saved.push([file, fs.existsSync(file) ? fs.readFileSync(file) : null]);
    fs.writeFileSync(file, content);
  }
  try {
    await body();
  } finally {
    for (const [file, content] of saved.reverse()) {
      if (content == null) fs.rmSync(file, { force: true }); else fs.writeFileSync(file, content);
    }
  }
}

/** 画面の「試しにビルドする」を押し、そのコマンドのビルドが終わって画面に出るまで待つ。 */
async function trialBuild(page, command, expect) {
  if (command != null) await page.locator('#f-bcmd').fill(command);
  await page.locator('#trialBtn').click();
  const wanted = command ?? await page.locator('#f-bcmd').inputValue();
  for (let i = 0; i < 900; i++) {
    const st = (await api('/api/trial-build')).json;
    if (st.status !== 'running' && st.commands?.build_command === wanted) break;
    await sleep(200);
  }
  await waitText(page, '#trialResult', expect, 30_000);
}

const cfgFile = () => path.join(W.repo, '.coverity-triage', 'config.yaml');
const filtersDir = () => path.join(W.repo, '.coverity-triage', 'filters');

// ---- 始めと終わり --------------------------------------------------------------------------------

if (SKIP) {
  test('ct.py gui の画面の試験', { skip: SKIP }, () => {});
} else {
  before(async () => {
    await makeWorld();
    browser = await launchBrowser();
  }, { timeout: 900_000 });

  after(async () => {
    await browser?.close().catch(() => {});
    for (const port of [W.port, W.svnPort].filter(Boolean)) {
      await request({ method: 'POST', target: '/api/close', port, body: '{}',
        headers: { 'Content-Type': 'application/json', 'X-CT-Token': port === W.port ? W.token : W.svnToken } }).catch(() => {});
    }
    for (const pid of [W.launch?.pid].filter(Boolean)) {
      try {
        process.kill(pid, 'SIGKILL');
      } catch {
        // もう止まっている
      }
    }
    for (const child of children) {
      try {
        child.kill('SIGKILL');
      } catch {
        // もう止まっている
      }
    }
    await sleep(300);
    if (W.tmp) fs.rmSync(W.tmp, { recursive: true, force: true });
  });

  // ==== 起動と接続の制限 ==========================================================================

  describe('起動と接続の制限', () => {
    test('REQ-029 ct.py gui は裏で起動して合言葉つきの URL を返し、同じリポジトリではそのサーバーを使い回す', async () => {
      console.log(`# 準備 ${W.setupSeconds} 秒`);
      assert.equal(W.launch.ok, true);
      assert.equal(W.launch.reused, false);
      assert.equal(W.launch.browser_opened, false);
      assert.match(W.launch.url, /^http:\/\/127\.0\.0\.1:\d+\/\?t=[A-Za-z0-9_-]{20,}$/);
      const again = await ct(['gui', '--repo', W.repo, '--no-browser', '--run', W.runMid, '--view', 'monitor']);
      assert.equal(again.reused, true, JSON.stringify(again));
      assert.equal(again.pid, W.launch.pid);
      assert.equal(again.url, `${W.launch.url}#run=${runId(W.runMid)}&view=monitor`);
      const ping = await api('/api/ping');
      assert.equal(ping.status, 200);
      assert.equal(ping.json.repo, W.repo);
      // 実行フォルダでないものを --run に渡すと、分かる文で断る（画面は動いたまま）
      const notRun = await ct(['gui', '--repo', W.repo, '--no-browser', '--run', W.base], { ok: false });
      assert.equal(notRun.ok, false);
      assert.match(notRun.error, /実行フォルダではありません/);
      assert.equal((await api('/api/ping')).status, 200);
    });

    test('REQ-006 合言葉のない・違う合言葉・違う Host・違う Content-Type の問い合わせは断られる', async () => {
      const page = await request({ target: '/' });
      assert.equal(page.status, 403);
      assert.match(page.text, /\?t=/);
      assert.equal((await request({ target: '/?t=wrong-token' })).status, 403);
      const ok = await request({ target: `/?t=${W.token}` });
      assert.equal(ok.status, 200);
      assert.match(ok.headers['set-cookie']?.[0] ?? '', new RegExp(`^ct_token_${W.port}=.+; HttpOnly; SameSite=Strict; Path=/$`));
      assert.equal(ok.headers['x-frame-options'], 'DENY');
      assert.match(ok.headers['content-security-policy'], /frame-ancestors 'none'/);
      assert.equal(ok.headers['cache-control'], 'no-store');

      const noToken = await request({ target: '/api/state' });
      assert.equal(noToken.status, 403);
      assert.deepEqual(noToken.json, { ok: false, error: 'forbidden' });
      assert.doesNotMatch(noToken.text, /envmon/);
      assert.equal((await api('/api/state', { headers: { 'X-CT-Token': 'wrong' } })).status, 403);
      // DNS リバインディング：Host が違えば、合言葉が合っていても断る
      assert.equal((await api('/api/state', { headers: { Host: `evil.example:${W.port}` } })).status, 403);
      assert.equal((await request({ target: `/?t=${W.token}`, headers: { Host: `evil.example:${W.port}` } })).status, 403);
      assert.equal((await api('/api/state', { headers: { Host: `localhost:${W.port}` } })).status, 200);
      // ほかのページからのフォームの送信（Content-Type が application/json でない）は断る
      const before = fs.statSync(cfgFile()).mtimeMs;
      const form = await api('/api/save', { method: 'POST', body: JSON.stringify({ knowledge: 'x' }), headers: { 'Content-Type': 'text/plain' } });
      assert.equal(form.status, 403);
      const noTokenPost = await request({ method: 'POST', target: '/api/close', body: '{}', headers: { 'Content-Type': 'application/json' } });
      assert.equal(noTokenPost.status, 403);
      assert.equal(fs.statSync(cfgFile()).mtimeMs, before);
      assert.equal((await api('/api/ping')).status, 200, '断ったあともサーバーは動いている');
    });

    test('REQ-006 画面のサーバーは 127.0.0.1 だけで待ち受け、この機械のほかの住所からはつながらない', async (t) => {
      const addresses = Object.values(os.networkInterfaces()).flat().filter((a) => a && a.family === 'IPv4' && !a.internal);
      if (!addresses.length) {
        t.skip('この機械に 127.0.0.1 のほかの IPv4 の住所がない');
        return;
      }
      for (const { address } of addresses) {
        const result = await new Promise((resolve) => {
          const socket = net.connect(W.port, address);
          socket.setTimeout(3000, () => { socket.destroy(); resolve('TIMEOUT'); });
          socket.on('connect', () => { socket.destroy(); resolve('CONNECTED'); });
          socket.on('error', (error) => resolve(error.code));
        });
        assert.notEqual(result, 'CONNECTED', `${address}:${W.port} につながった`);
      }
    });

    test('REQ-006 実行のファイルは決まったもの（一覧・詳細レポート・差分・修正後のファイル）だけを出し、.. やほかのファイルは出さない', async () => {
      const id = runId(W.runDone);
      assert.equal((await request({ target: `/files/${id}/summary.html` })).status, 403, '合言葉も Cookie もない');
      const cookie = `ct_token_${W.port}=${W.token}`;
      const summary = await request({ target: `/files/${id}/summary.html`, headers: { Cookie: cookie } });
      assert.equal(summary.status, 200);
      assert.match(summary.headers['content-type'], /^text\/html/);
      assert.match(summary.headers['content-security-policy'], /sandbox/);
      assert.doesNotMatch(summary.headers['content-security-policy'], /script-src/);
      for (const bad of [`/files/${id}/run.json`, `/files/${id}/approvals.json`, `/files/${id}/items/${W.done.ja}/result.json`,
        `/files/${id}/..%2Frun.json`, `/files/${id}/fixed/${W.done.ja}/fix/../../../run.json`, `/files/${id}/cid/..%2F..%2Frun.json`,
        `/view/${id}/summary.html`, `/files/../${id}/summary.html`, `/files/20200101-000000/summary.html`,
        `/files/${id}/fixed/${W.done.ja}/fix/%2e%2e/%2e%2e/%2e%2e/run.json`, `/view/${id}/patches/..%5C..%5Crun.json`]) {
        const res = await request({ target: bad, headers: { Cookie: cookie } });
        assert.equal(res.status, 404, `${bad} が ${res.status} で出た`);
        assert.doesNotMatch(res.text, /"run_id"|"decision"|"verdict"/, bad);
      }
      const patch = await request({ target: `/view/${id}/patches/${W.done.ja}-fix.patch`, headers: { Cookie: cookie } });
      assert.equal(patch.status, 200);
      assert.match(patch.text, /coverity-triage: CID/);
    });

    test('REQ-006 断った POST（違う Content-Type・合言葉なし）の本文が、同じ接続の次の問い合わせを壊さない', async () => {
      const body = JSON.stringify({ knowledge: 'x' });
      const refused = [
        `POST /api/save HTTP/1.1\r\nHost: 127.0.0.1:${W.port}\r\nX-CT-Token: ${W.token}\r\nContent-Type: text/plain\r\nContent-Length: ${body.length}\r\n\r\n${body}`,
        `POST /api/close HTTP/1.1\r\nHost: 127.0.0.1:${W.port}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}`,
      ];
      const ping = `GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:${W.port}\r\nX-CT-Token: ${W.token}\r\n\r\n`;
      for (const first of refused) {
        const answer = await rawRequest(first + ping, { timeout: 1500, all: true });
        const statuses = [...answer.matchAll(/HTTP\/1\.1 (\d{3})/g)].map((m) => m[1]);
        assert.deepEqual(statuses, ['403', '200'], `断った POST のあとの GET /api/ping が ${statuses[1] ?? '（答えなし）'}: ${first.split('\r\n')[0]}`);
      }
    });

    test('REQ-006 壊れた本文（配列・壊れた JSON・数でない長さ・負の長さ）は 400 で断られ、サーバーは止まらない', async () => {
      const head = (length) => `POST /api/save HTTP/1.1\r\nHost: 127.0.0.1:${W.port}\r\nX-CT-Token: ${W.token}\r\nContent-Type: application/json\r\nContent-Length: ${length}\r\n\r\n`;
      const array = await post('/api/save', []);
      assert.equal(array.status, 400);
      assert.match(array.json.error, /送られた内容を読めません/);
      assert.equal((await api('/api/save', { method: 'POST', body: '{"config":', headers: { 'Content-Type': 'application/json' } })).status, 400);
      assert.match(await rawRequest(head('abc') + '{}'), /^HTTP\/1\.1 400/);
      // 長さが負の数：答えが返らずに接続が固まってはいけない
      const negative = await rawRequest(head('-1') + '{}', { timeout: 4000 });
      assert.match(negative, /^HTTP\/1\.1 400/, `Content-Length: -1 に 4 秒たっても答えない（${negative.slice(0, 80)}）`);
      assert.equal((await api('/api/ping')).status, 200);
    });
  });

  // ==== 監視 ======================================================================================

  describe('監視', () => {
    const midState = () => readJson(path.join(W.runMid, 'run.json'));
    const busyItems = () => midState().items.filter((i) => i.status === 'in_progress').sort((a, b) => a.slot - b.slot);
    const slotText = async (page, item) => {
      const target = item.id.startsWith('G') ? `${item.id}（${item.cids.length} 件）` : `CID ${item.id}`;
      const li = page.locator('#slots li', { hasText: `#${item.slot} ${target}` });
      assert.equal(await li.count(), 1, `${target} の枠がない: ${await text(page, '#slots')}`);
      return li.innerText();
    };

    test('REQ-008 REQ-103 監視：作業ごとの段・経過時間・メモ、最近の動き、全体の進み具合が文字で出る', async () => {
      const v = await open(`#run=${runId(W.runMid)}&view=monitor`);
      try {
        await monitorReady(v.page);
        const state = midState();
        const busy = busyItems();
        assert.equal(busy.length, 3, '作業中が 3 件（parallel 3）');
        assert.equal(await text(v.page, '#monStage'), '警告を調べている');
        const board = await text(v.page, '#board');
        assert.match(board, /MAIN \(Copilot\)/);
        assert.match(board, new RegExp(`1/${state.items.length} 完了`));
        assert.match(board, /実行中 3/);
        assert.match(board, /collector/);
        for (const item of busy) {
          assert.match(board, new RegExp(`worker #${item.slot}`));
          assert.ok(board.includes(item.id.startsWith('G') ? `${item.id} (${item.cids.length}件)` : `CID ${item.id}`), `${item.id} が枠の図にない`);
        }
        assert.match(board, /\d\d:\d\d/, '経過時間');
        assert.match(await slotText(v.page, busy[0]), /調査中・コードを読む（1\/5）[\s\S]*メモ: 呼び出し元を読んでいる[\s\S]*経過 \d\d:\d\d・最後の動き \d+ 秒前/);
        assert.match(await slotText(v.page, busy[2]), /修正を作る（3\/5）[\s\S]*メモ: 修正を作っている/);
        assert.match(await slotText(v.page, busy[1]), /調査中/);
        const recent = await text(v.page, '#recent');
        assert.match(recent, /MCP/);
        assert.match(recent, /警告経路を取った/);
        assert.match(recent, /の調査を始めた（スロット \d）/);
        assert.match(await text(v.page, '#alerts'), /重大なエラーはありません/);
        const option = await v.page.locator(`#monRun option[value="${runId(W.runMid)}"]`).innerText();
        assert.match(option, /途中の実行.*（途中）/);
        await shot(v.page, 'monitor-running-1280');
        assert.deepEqual(v.errors, []);
      } finally {
        await v.close();
      }
    });

    test('REQ-008 監視：progress.json と operations.log を書き換え、作業を 1 件終えると、再読み込みしなくても表示が変わる', async () => {
      const v = await open(`#run=${runId(W.runMid)}&view=monitor`);
      try {
        await monitorReady(v.page);
        await v.page.evaluate(() => { window.__samePage = true; });
        const [a, b, c] = busyItems();
        // 作業エージェントが節目を書いた
        writeJson(path.join(W.runMid, 'items', a.id, 'progress.json'), { item: a.id, phase: 'judge', note: '呼び出し元 3 か所を読んだ（試験で書き換えた）' });
        await waitText(v.page, '#slots', '真偽を判断（2/5）');
        await waitText(v.page, '#slots', 'メモ: 呼び出し元 3 か所を読んだ（試験で書き換えた）');
        // 重大なエラーの記録が足された
        fs.appendFileSync(path.join(W.runMid, 'operations.log'), JSON.stringify({ time: iso(now()), event: 'error', level: 'critical',
          source: 'ct', command: 'finish', item: b.id, error: '修正を保存できませんでした（試験で足した記録）' }) + '\n', 'utf8');
        await waitText(v.page, '#alerts', `${b.id} の ct.py finish が失敗しました`);
        await waitText(v.page, '#recent', `${b.id} の ct.py finish が失敗した: 修正を保存できませんでした`);
        await waitText(v.page, '#board', '✖ エラー');
        assert.equal(await v.page.locator('#alerts details.alert.critical .pill').first().innerText(), '重大');
        assert.equal(await v.page.locator('nav.views [data-view="monitor"] .vmark.error').count(), 1, '「監視」に重大なエラーの印');
        await shot(v.page, 'monitor-critical-alert-1280');
        // 作業エージェントが 1 件を終え、統括（この試験）が tick --finished で知らせた
        const task = W.midHeld.find((h) => h.item === c.id).task;
        doWork(W.runMid, c.id, { rec: 'deviation', conf: 'medium' });
        await ct(['tick', '--run', W.runMid, '--finished', task]);   // 空いた枠に次の作業が入る（その作業は動かさない）
        await waitText(v.page, '#board', `2/${midState().items.length} 完了`);
        await waitText(v.page, '#recent', `${c.id} を終えた（推奨 逸脱、確信度 中）`);
        await shot(v.page, 'monitor-live-update-1280');
        assert.equal(await v.page.evaluate(() => window.__samePage), true, 'ページが読み込み直された');
        assert.equal(await v.page.evaluate(() => performance.getEntriesByType('navigation').length), 1);
        assert.deepEqual(v.errors, []);
      } finally {
        await v.close();
      }
    });

    test('REQ-103 監視：10 分動きのない作業に「⚠ 停止?」が出て、修正用のコピーを用意している作業には出ない（K-79）', async () => {
      const busy = busyItems();
      const a = busy.find((i) => i.slot === 1);
      const d = busy.find((i) => i.slot === 3);
      assert.ok(a && d, '作業中の枠 1 と 3');
      const ago = 11 * MIN;
      const state = midState();
      for (const item of state.items) if ([a.id, d.id].includes(item.id)) item.started -= ago;
      writeJson(path.join(W.runMid, 'run.json'), state);
      const log = path.join(W.runMid, 'operations.log');
      const records = fs.readFileSync(log, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
      const lastCopyOfD = records.map((r, i) => [r, i]).filter(([r]) => r.item === d.id && r.event === 'copy').at(-1)[1];
      const kept = records.filter((r, i) => !(r.item === d.id && i > lastCopyOfD && ['next', 'brief'].includes(r.event)))
        .map((r) => ([a.id, d.id].includes(r.item) ? { ...r, time: iso(Date.parse(r.time) / 1000 - ago) } : r));
      fs.writeFileSync(log, kept.map((r) => JSON.stringify(r)).join('\n') + '\n', 'utf8');
      for (const id of [a.id, d.id]) {
        const folder = path.join(W.runMid, 'items', id);
        for (const name of fs.readdirSync(folder)) fs.utimesSync(path.join(folder, name), now() - ago, now() - ago);
      }
      const v = await open(`#run=${runId(W.runMid)}&view=monitor`);
      try {
        await monitorReady(v.page);
        await waitText(v.page, '#board', '⚠ 停止?');
        assert.match(await slotText(v.page, a), /止まっているかもしれません/);
        assert.match(await text(v.page, '#alerts'), new RegExp(`${a.id} が 11 分動いていません`));
        const dText = await slotText(v.page, d);
        assert.match(dText, /修正用のコピーを用意中/);
        assert.doesNotMatch(dText, /止まっているかもしれません/, 'コピーを用意している作業に停止の疑いが出た');
        const board = await text(v.page, '#board');
        assert.equal(board.split('⚠ 停止?').length - 1, 1, `「⚠ 停止?」は 1 つだけ:\n${board}`);
        assert.doesNotMatch(await text(v.page, '#alerts'), new RegExp(`${d.id} が \\d+ 分動いていません`));
        await shot(v.page, 'monitor-stall-1280');
      } finally {
        await v.close();
      }
    });

    test('REQ-103 監視：初回解析の進み具合が出て、「⚠ 停止?」は 11 分では出ず、30 分を過ぎると出る（K-79）', async () => {
      const v = await open(`#run=${runId(W.runInit)}&view=monitor`);
      try {
        await monitorReady(v.page);
        assert.equal(await text(v.page, '#monStage'), '初回解析（ビルド・解析・サーバへの登録）');
        let board = await text(v.page, '#board');
        assert.match(board, /initial/);
        assert.match(board, /解析 3\/4/);
        assert.match(board, /▶ 実行中/);
        assert.match(board, /初回解析の終わりを待っている/);
        const slot = await text(v.page, '#slots');
        assert.match(slot, /初回解析（ビルド → 解析 → サーバへの登録）/);
        assert.match(slot, /解析（cov-analyze）（3\/4）・実行中/);
        assert.match(slot, /経過 0\d:\d\d・ログが最後に増えた \d+ 秒前/);
        await shot(v.page, 'monitor-initial-1280');
        setInitial(W.runInit, { started: now() - 12 * MIN, logAge: 11 * MIN });
        await waitText(v.page, '#slots', 'ログが最後に増えた 11 分前');
        await v.page.waitForTimeout(2500);
        board = await text(v.page, '#board');
        assert.doesNotMatch(board, /⚠ 停止\?/, '初回解析は 11 分では停止の疑いにしない');
        assert.doesNotMatch(await text(v.page, '#slots'), /止まっているかもしれません/);
        setInitial(W.runInit, { started: now() - 32 * MIN, logAge: 31 * MIN });
        await waitText(v.page, '#board', '⚠ 停止?');
        assert.match(await text(v.page, '#slots'), /止まっているかもしれません/);
        assert.match(await text(v.page, '#alerts'), /初回解析のログが 31 分増えていません/);
        await shot(v.page, 'monitor-initial-stall-1280');
      } finally {
        setInitial(W.runInit, { started: now() - 200, logAge: 5 });
        await v.close();
      }
    });

    test('REQ-103 監視：処理できなかった作業が「重大」の札と理由で出て、詳細の画面で分かる文になる', async () => {
      const failed = readJson(path.join(W.runFail, 'run.json')).items.find((i) => i.status === 'error');
      const v = await open(`#run=${runId(W.runFail)}&view=monitor`);
      try {
        await monitorReady(v.page);
        const alert = v.page.locator('#alerts details.alert.critical', { hasText: `${failed.id} を処理できませんでした` });
        assert.equal(await alert.count(), 1, await text(v.page, '#alerts'));
        assert.equal(await alert.locator('.pill').innerText(), '重大');
        await alert.locator('summary').click();
        assert.equal(await alert.locator('pre').innerText(), FAIL_REASON, '理由が文字のまま出る');
        assert.match(await text(v.page, '#alertCount'), /重大 1 件/);
        await shot(v.page, 'monitor-failed-item-1280');
        await alert.getByText('この作業の詳細を開く').click();
        await detailReady(v.page);
        const shown = await text(v.page, '#resBody');
        assert.doesNotMatch(shown, /まだ終わっていません|状態: error/, `処理できなかった作業を「まだ終わっていない」と示した: ${shown}`);
        assert.match(shown, /処理できませんでした|失敗/);
      } finally {
        await v.close();
      }
    });
  });

  // ==== 結果 ======================================================================================

  describe('結果', () => {
    const rowsOf = (page) => page.locator('#resBody table.items tbody tr');
    const doneResults = (runDir) => {
      const state = readJson(path.join(runDir, 'run.json'));
      return state.items.filter((i) => i.status === 'done').map((i) => ({ id: i.id, ...readJson(path.join(runDir, 'items', i.id, 'result.json')) }));
    };

    test('REQ-103 結果：実行の一覧が新しい順に並び、進み具合と状態が文字で出る', async () => {
      const v = await open('#view=summary');
      try {
        await waitFor(v.page, () => document.querySelectorAll('#resBody table.items tbody tr').length > 0, null, 15_000, '実行の一覧');
        const ids = await rowsOf(v.page).locator('td:first-child a').allInnerTexts();
        const expected = fs.readdirSync(W.out).filter((n) => /^\d{8}-\d{6}(-\d+)?$/.test(n) && fs.existsSync(path.join(W.out, n, 'run.json'))).sort().reverse();
        assert.deepEqual(ids, expected);
        const row = (dir) => rowsOf(v.page).filter({ hasText: runId(dir) }).innerText();
        assert.match(await row(W.runMid), /途中: 警告を調べている/);
        assert.match(await row(W.runDone), /8\/8 完了[\s\S]*一覧あり/);
        assert.match(await row(W.runFail), /エラー 1/);
        assert.match(await row(W.runFail), /失敗あり <script>window.__xss=1<\/script>/);
        assert.match(await row(W.runNone), /0\/0 完了/);
        assert.match(await row(W.runInit), /途中: 初回解析/);
        await shot(v.page, 'results-runs-1280');
        await rowsOf(v.page).filter({ hasText: runId(W.runDone) }).getByRole('link', { name: '結果' }).click();
        await summaryReady(v.page);
        assert.match(await text(v.page, '#resBody h2'), new RegExp(`トリアージ結果 ${runId(W.runDone)}`));
        await assertNoScript(v, '実行の一覧');
      } finally {
        await v.close();
      }
    });

    test('REQ-104 結果：作業の表は 6 列で確信度の低い順（summary.html と同じ順）に並び、件数のタイルが合う', async () => {
      const v = await open(`#run=${runId(W.runDone)}&view=summary`);
      try {
        await summaryReady(v.page);
        assert.deepEqual(await v.page.locator('#resBody table.items thead th').allInnerTexts(), ['承認', 'ID', '推奨', '確信度', '見立て', '状態']);
        const ids = await rowsOf(v.page).locator('button.linkish').allInnerTexts();
        const html = fs.readFileSync(path.join(W.runDone, 'summary.html'), 'utf8');
        const inHtml = [...new Set([...html.matchAll(/href="cid\/([^"]+)\.html"/g)].map((m) => m[1]))];
        assert.deepEqual(ids, inHtml, '画面と summary.html の並び');
        const results = new Map(doneResults(W.runDone).map((r) => [r.id, r]));
        const rank = { low: 0, medium: 1, high: 2 };
        const ranks = ids.map((id) => rank[results.get(id).confidence]);
        assert.deepEqual(ranks, [...ranks].sort((x, y) => x - y), '確信度の低い順');
        const conf = await rowsOf(v.page).locator('td:nth-child(4) .badge').allInnerTexts();
        assert.deepEqual(conf, ids.map((id) => ({ low: '低', medium: '中', high: '高' })[results.get(id).confidence]));
        const tile = async (label) => v.page.locator('#sumTiles .tile', { has: v.page.locator('.label', { hasText: new RegExp(`^${label}$`) }) }).locator('.value').innerText();
        const all = [...results.values()];
        assert.equal(await tile('作業'), String(all.length));
        assert.equal(await tile('修正の推奨'), String(all.filter((r) => r.recommendation === 'fix').length));
        assert.equal(await tile('逸脱の推奨'), String(all.filter((r) => r.recommendation === 'deviation').length));
        assert.equal(await tile('確信度 低'), String(all.filter((r) => r.confidence === 'low').length));
        assert.equal(await tile('エラー'), '0');
        assert.equal(await tile('承認の内訳'), `${all.length - 1}/${all.length}`, '最新で直っている 1 件は「反映しない」');
        assert.equal(await v.page.locator(`select[aria-label="${W.done.already} の承認"]`).inputValue(), '');
        await shot(v.page, 'results-summary-1280');
      } finally {
        await v.close();
      }
    });

    test('REQ-103 結果：表で承認を選び直すと（修正・逸脱・却下・反映しない）、すぐ approvals.json に保存される', async () => {
      const id = W.done.ja;
      const v = await open(`#run=${runId(W.runDone)}&view=summary`);
      try {
        await summaryReady(v.page);
        const sel = v.page.locator(`select[aria-label="${id} の承認"]`);
        const row = rowsOf(v.page).filter({ has: sel });
        assert.equal(await sel.inputValue(), 'fix');
        const summaryHtml = path.join(W.runDone, 'summary.html');
        for (const [value, label] of [['deviation', '逸脱'], ['reject', '却下'], ['', '反映しない'], ['fix', '修正']]) {
          const before = fs.statSync(summaryHtml).mtimeMs;
          await sel.selectOption(value);
          await waitText(v.page, '#toast', `${id} の承認を「${label}」にして保存しました`);
          const saved = approvals(W.runDone)[id];
          assert.equal(saved.decision, value || null, `${label} が approvals.json に入る`);
          assert.equal(saved.edited, value !== 'fix');
          assert.equal(await row.locator('.edited').isVisible(), value !== 'fix', '「推奨から変更」の印');
          assert.ok(fs.statSync(summaryHtml).mtimeMs > before, '一覧 summary.html も書き直す');
          if (value === 'deviation') await shot(v.page, 'results-approval-changed-1280');
          await v.page.waitForTimeout(150);
        }
      } finally {
        await v.close();
      }
    });

    test('REQ-104 結果：詳細レポート・差分・修正後のファイル（日本語と空白の名前）を、画面の中と新しいタブで開ける', async () => {
      const id = W.done.ja;
      const run = runId(W.runDone);
      const v = await open(`#run=${run}&view=summary&item=${id}`);
      try {
        await detailReady(v.page);
        const frame = v.page.frameLocator('#repFrame');
        assert.equal(await v.page.locator('#repFrame').getAttribute('src'), `/files/${run}/cid/${id}.html`);
        await waitFor(v.page, () => document.querySelector('#repFrame')?.contentDocument?.body?.innerText.includes('案B: 修正'), null, 15_000, '詳細レポート');
        const report = await frame.locator('body').innerText();
        assert.match(report, new RegExp(`${id}\\s*${mappedIssues(W.runDone).get(Number(id)).checker}`));
        assert.ok(report.indexOf('案A: 逸脱') < report.indexOf('案B: 修正'), '案の順は「逸脱 → 修正」');
        assert.match(await text(v.page, '#detailNav'), /\d+ \/ 8（確信度の低い順）/);
        await shot(v.page, 'results-detail-report-1280');
        const files = await v.page.locator('.files li button.linkish').allInnerTexts();
        assert.ok(files.includes(`修正: ${id}-fix.patch`), files.join(' / '));
        assert.ok(files.includes(`修正: ${JA_FILE}`), files.join(' / '));
        // 差分を画面の中に出す
        await v.page.locator('.files button.linkish', { hasText: `${id}-fix.patch` }).click();
        assert.equal(await v.page.locator('#repFrame').getAttribute('src'), `/view/${run}/patches/${id}-fix.patch`);
        await waitFor(v.page, (needle) => document.querySelector('#repFrame')?.contentDocument?.body?.innerText.includes(needle), FIXED_LINE(readJson(path.join(W.runDone, 'run.json')).items.find((i) => i.id === id).cids[0]).slice(0, 30), 15_000, '差分');
        assert.match(await text(v.page, '#frameBar'), new RegExp(`差分 ${id}-fix.patch[\\s\\S]*詳細レポートに戻る`));
        await shot(v.page, 'results-detail-patch-1280');
        // 日本語と空白の名前の、修正後のファイル
        await v.page.locator('.files button.linkish', { hasText: JA_FILE }).click();
        assert.equal(await v.page.locator('#repFrame').getAttribute('src'), `/view/${run}/fixed/${id}/fix/${JA_FILE.split('/').map(encodeURIComponent).join('/')}`);
        await waitFor(v.page, () => document.querySelector('#repFrame')?.contentDocument?.body?.innerText.includes('sensor_convert'), null, 15_000, '修正後のファイル');
        assert.match(await frame.locator('h1').innerText(), new RegExp(JA_FILE));
        await shot(v.page, 'results-detail-fixed-japanese-name-1280');
        // ↗ で新しいタブに開く（Cookie の合言葉で読める）
        const [tab] = await Promise.all([v.context.waitForEvent('page'),
          v.page.locator('.files li', { hasText: JA_FILE }).locator('a.newtab').click()]);
        await tab.waitForLoadState();
        assert.match(await tab.locator('body').innerText(), /sensor_convert/);
        // 元のファイル（そのまま）
        await tab.getByText('元のファイル').click();
        await tab.waitForLoadState();
        assert.match(await tab.locator('body').innerText(), /int sensor_convert\(int v\)/);
        await tab.close();
        // 詳細レポートに戻る
        await v.page.getByRole('button', { name: '詳細レポートに戻る' }).click();
        assert.equal(await v.page.locator('#repFrame').getAttribute('src'), `/files/${run}/cid/${id}.html`);
      } finally {
        await v.close();
      }
    });

    test('REQ-101 REQ-103 結果：逸脱コメントを手直しして保存すると approvals.json と詳細レポートに入り、空の欄は断られ、AI の案に戻せる', async () => {
      const id = W.done.xss;
      const run = runId(W.runDone);
      const edited = '呼び出し元 src/app/config_ini.c:120 で NULL を確かめているため問題ない（人が直した）';
      const v = await open(`#run=${run}&view=summary&item=${id}`);
      try {
        await detailReady(v.page);
        assert.equal(await v.page.locator('#ap-comment').inputValue(), XSS_COMMENT);
        assert.equal(await v.page.locator('#apSave').isDisabled(), true, '変更がなければ「保存」は押せない');
        await v.page.locator('#ap-comment').fill(edited);
        assert.equal(await text(v.page, '#apState'), '● 保存していない変更があります');
        assert.equal(await v.page.locator('#ap-comment-mark').isVisible(), true, '「AI の案から変更」の印');
        await v.page.locator('#apSave').click();
        await waitText(v.page, '#apMsg', '保存しました。一覧と詳細レポートも新しくしました。');
        let saved = approvals(W.runDone)[id];
        assert.equal(saved.decision, 'deviation');
        assert.equal(saved.deviation.comment, edited);
        assert.equal(saved.edited, true);
        assert.match(fs.readFileSync(path.join(W.runDone, 'cid', `${id}.html`), 'utf8'), new RegExp(edited.slice(0, 20)));
        await waitFor(v.page, (needle) => document.querySelector('#repFrame')?.contentDocument?.body?.innerText.includes(needle), edited.slice(0, 20), 15_000, '書き直した詳細レポート');
        await shot(v.page, 'results-deviation-edited-1280');
        // 空の Classification は断る（ファイルは変わらない）
        await v.page.locator('#ap-classification').fill('');
        await v.page.locator('#apSave').click();
        await waitText(v.page, '#apMsg', 'Classification が空です');
        assert.equal(approvals(W.runDone)[id].deviation.classification, 'False Positive');
        // AI の案に戻して保存すると、手直しがなくなる
        await v.page.getByRole('button', { name: 'AI の案に戻す' }).click();
        assert.equal(await v.page.locator('#ap-comment').inputValue(), XSS_COMMENT);
        await v.page.locator('#apSave').click();
        await waitText(v.page, '#apMsg', '保存しました');
        saved = approvals(W.runDone)[id];
        assert.equal(saved.deviation, undefined);
        assert.equal(saved.edited, false);
        await assertNoScript(v, '詳細');
      } finally {
        await v.close();
      }
    });

    test('REQ-103 結果：統合・検証つきの実行では、一覧ができるまで承認を選べず（K-80）、一覧ができると選べる', async () => {
      const run = runId(W.runReview);
      const v = await open(`#run=${run}&view=summary`);
      try {
        await summaryReady(v.page);
        const first = (await rowsOf(v.page).locator('button.linkish').allInnerTexts())[0];
        const sel = v.page.locator(`select[aria-label="${first} の承認"]`);
        const was = await sel.inputValue();
        if (!(await sel.isDisabled())) {
          await sel.selectOption(was === 'reject' ? 'fix' : 'reject');
          await waitText(v.page, '#resMsg', '統合・検証が終わって一覧ができてから承認できます');
          await v.page.waitForTimeout(300);
          assert.equal(await sel.inputValue(), was, '断られた承認は元の値に戻る');
        }
        assert.equal(approvals(W.runReview), null, '一覧ができる前に approvals.json ができた');
        await shot(v.page, 'results-review-locked-1280');
        // 詳細の承認の欄も同じ
        await rowsOf(v.page).locator('button.linkish').first().click();
        await detailReady(v.page);
        if (!(await v.page.locator('#apDecision').isDisabled())) {
          await v.page.locator('#apDecision').selectOption('reject');
          await v.page.locator('#apSave').click();
          await waitText(v.page, '#apMsg', '一覧ができてから承認できます');
        }
        assert.equal(approvals(W.runReview), null);
      } finally {
        await v.close();
      }
      // 検証を終えて（止めていた 1 件は要確認）一覧を作る
      const [h] = W.reviewHeld;
      writeJson(path.join(W.runReview, 'items', h.item, 'review.json'), { item: h.item, outcome: 'flag',
        findings: [{ check: '根拠の行', ok: false, detail: '根拠の行が警告の場所と合わない' }], confidence_cap: 'low', rework: [],
        note: '根拠の行が警告の場所と合わないため要確認' });
      W.flagged = h.item;
      const finished = await drive(W.runReview, null, { reviewer: (item) => passReview(item), start: ['--finished', h.task] });
      assert.equal(finished.last.done, true, JSON.stringify(finished.last).slice(0, 1000));
      await ct(['summary', '--run', W.runReview]);
      const v2 = await open(`#run=${run}&view=summary`);
      try {
        await summaryReady(v2.page);
        const row = rowsOf(v2.page).filter({ hasText: W.flagged });
        assert.match(await row.innerText(), /⚠ 検証 要確認/);
        assert.match(await row.innerText(), /低/, '検証の上限（低）が確信度になる');
        assert.match(await text(v2.page, '#resBody table.items'), /✔ 検証 合格/);
        const sel = v2.page.locator(`select[aria-label="${W.flagged} の承認"]`);
        await sel.selectOption('reject');
        await waitText(v2.page, '#toast', `${W.flagged} の承認を「却下」にして保存しました`);
        assert.equal(approvals(W.runReview)[W.flagged].decision, 'reject');
        await shot(v2.page, 'results-review-unlocked-1280');
      } finally {
        await v2.close();
      }
    });

    test('REQ-103 結果：検証つきの実行で一覧ができる前は、「ここで選んだ承認は保存されます」と案内しない（K-80）', async () => {
      // 新しく検証つきの実行を作り、作業を 1 件終えたところで画面を見る
      fs.writeFileSync(cfgFile(), W.configText + 'agents:\n  integration: false\n  review: true\n', 'utf8');
      let runDir;
      const mcp = new Mcp(W.env);
      try {
        runDir = (await ct(['new-run', '--repo', W.repo, '--filter', 'gui-review.yaml', '--limit', '1'])).run_dir;
        await mcp.start();
        await drive(runDir, mcp, { worker: () => ({ rec: 'fix', conf: 'high' }), reviewer: () => ({ hold: true }) });
      } finally {
        mcp.close();
        fs.writeFileSync(cfgFile(), W.configText, 'utf8');
      }
      const v = await open(`#run=${runId(runDir)}&view=summary`);
      try {
        await summaryReady(v.page);
        const shown = await text(v.page, '#resBody');
        assert.doesNotMatch(shown, /ここで選んだ承認は保存されます/, '保存できないのに「保存されます」と案内した');
        assert.doesNotMatch(shown, /表の「承認」で選ぶとすぐに保存します/, '保存できないのに「すぐに保存します」と案内した');
      } finally {
        await v.close();
        fs.rmSync(runDir, { recursive: true, force: true });
      }
    });

    test('REQ-103 結果：処理できなかった作業とその理由、グループの作業が出る', async () => {
      const v = await open(`#run=${runId(W.runFail)}&view=summary`);
      try {
        await summaryReady(v.page);
        const failed = readJson(path.join(W.runFail, 'run.json')).items.find((i) => i.status === 'error');
        const card = v.page.locator('#resBody .card', { has: v.page.locator('h3', { hasText: '処理できなかった作業' }) });
        assert.equal(await card.locator('li b').innerText(), failed.id);
        assert.match(await card.locator('li').innerText(), new RegExp(FAIL_REASON.replace(/[()（）<>]/g, '.')));
        const errTile = v.page.locator('#sumTiles .tile', { has: v.page.locator('.label', { hasText: /^エラー$/ }) });
        assert.equal(await errTile.locator('.value').innerText(), '1');
        assert.match(await errTile.getAttribute('class'), /bad/);
        assert.match(await text(v.page, '#resBody table.items'), /G1（2 件）/);
        assert.match(await text(v.page, '#resBody .lead'), /条件: 失敗あり <script>window.__xss=1<\/script>/);
        await shot(v.page, 'results-failed-items-1280');
        await assertNoScript(v, '失敗のある実行');
      } finally {
        await v.close();
      }
    });

    test('REQ-103 結果と監視：作業が 0 件の実行でも画面が止まらず、0 件であることが分かる', async () => {
      const v = await open(`#run=${runId(W.runNone)}&view=summary`);
      try {
        await summaryReady(v.page);
        const tile = v.page.locator('#sumTiles .tile', { has: v.page.locator('.label', { hasText: /^作業$/ }) });
        assert.equal(await tile.locator('.value').innerText(), '0');
        assert.match(await tile.innerText(), /警告 0 件/);
        assert.equal(await v.page.locator('#resBody .note.warn').count(), 0, await text(v.page, '#resBody'));
        await shot(v.page, 'results-zero-items-1280');
        await v.page.locator('nav.views [data-view="monitor"]').click();
        await monitorReady(v.page);
        assert.match(await text(v.page, '#board'), /0\/0 完了/);
        assert.match(await text(v.page, '#slots'), /いま動いているサブエージェントはありません/);
        assert.deepEqual(v.errors, []);
      } finally {
        await v.close();
      }
    });
  });

  // ==== 設定 ======================================================================================

  describe('設定', () => {
    /** .coverity-triage を写しておき、試験のあとで元に戻す。 */
    const keepSettings = () => {
      const dir = path.join(W.repo, '.coverity-triage');
      const copy = path.join(W.tmp, `settings-${Date.now()}`);
      fs.cpSync(dir, copy, { recursive: true });
      return () => {
        fs.rmSync(dir, { recursive: true, force: true });
        fs.cpSync(copy, dir, { recursive: true });
        fs.rmSync(copy, { recursive: true, force: true });
      };
    };
    const tab = async (page, name) => {
      await page.locator(`nav.tabs [data-tab="${name}"]`).click();
      await page.waitForTimeout(150);
    };
    const openSave = async (page) => {
      await page.locator('#saveBtn').click();
      await page.locator('#dlg').waitFor({ state: 'visible' });
      return { title: await text(page, '#dlg-title'), body: await text(page, '#dlg-body') };
    };
    const dialogRows = (page) => page.locator('#dlg-body table.changes tbody tr').evaluateAll((rows) => rows.map((r) => [...r.cells].map((c) => c.innerText)));
    const pickFilter = (page, file) => page.locator('#flist button.item', { has: page.locator('.file', { hasText: new RegExp(`^${file.replace(/\./g, '\\.')}$`) }) }).click();

    test('REQ-102 設定：config.yaml・条件・知識の今の値が画面に出て、バージョン管理の種類は変えられない', async () => {
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        assert.equal(await text(v.page, '#repo'), `${W.repo}（git）`);
        assert.equal(await v.page.locator('#f-url').inputValue(), W.emuUrl);
        assert.equal(await v.page.locator('#f-vcs').inputValue(), 'git');
        assert.equal(await v.page.locator('#f-vcs').getAttribute('readonly'), '');
        assert.equal(await v.page.locator('#f-base').inputValue(), 'main');
        assert.equal(await v.page.locator('#f-out').inputValue(), '../結果 出力');
        assert.match(await text(v.page, '#tab-basic'), /調べる警告の Coverity のプロジェクトとストリームは、条件ファイルに書いてあります/);
        await shot(v.page, 'settings-basic-1280');
        await tab(v.page, 'options');
        assert.equal(await v.page.locator('input[name="verify-default"][value="none"]').isChecked(), true);
        await v.page.locator('#more > summary').click();
        assert.equal(await v.page.locator('#f-max').inputValue(), '100');
        assert.equal(await v.page.locator('#f-par').inputValue(), '3');
        assert.equal(await v.page.locator('#f-strip').inputValue(), REMOTE_ROOT);
        assert.equal(await v.page.locator('#f-uenv').inputValue(), 'COV_USER');
        assert.equal(await v.page.locator('input[name="api"][value="auto"]').isChecked(), true);
        await v.page.locator('input[name="verify-default"][value="build"]').check();
        assert.equal(await v.page.locator('#f-bcmd').inputValue(), 'make', 'set-verify で入れたビルドのコマンド');
        await shot(v.page, 'settings-options-1280');
        await tab(v.page, 'filters');
        const files = await v.page.locator('#flist .file').allInnerTexts();
        assert.deepEqual(files, ['gui-done.yaml', 'gui-fail.yaml', 'gui-mid.yaml', 'gui-none.yaml', 'gui-review.yaml', 'untriaged.yaml']);
        await pickFilter(v.page, 'gui-done.yaml');
        assert.equal(await v.page.locator('#ff-stream').inputValue(), STREAM);
        assert.equal(await v.page.locator('#ff-checkers').inputValue(), 'NULL_RETURNS\nRESOURCE_LEAK\nDIVIDE_BY_ZERO');
        assert.equal(await v.page.locator('#ff-limit').inputValue(), '20');
        assert.deepEqual(await v.page.locator('#ff-impacts label').allInnerTexts(), ['High', 'Medium', 'Low', 'Unspecified'], 'Impact に Unspecified がある（K-101）');
        assert.equal(await v.page.locator('#ff-file').getAttribute('readonly'), '', '保存済みの条件ファイルの名前は変えられない');
        await shot(v.page, 'settings-filters-1280');
        await tab(v.page, 'knowledge');
        assert.match(await v.page.locator('#kn').inputValue(), /^# プロジェクトの知識/);
        await tab(v.page, 'nogroup');
        assert.equal(await text(v.page, '#chips'), 'ありません。');
        assert.deepEqual(v.errors, []);
        assert.equal(await v.page.locator('#dirty').isVisible(), true, 'ビルドの検証を選んだので未保存の印');
      } finally {
        await v.close();
      }
    });

    test('REQ-106 設定：Linux では認証情報を画面で保存できないことが示され、認証キーの値は画面にも問い合わせの答えにも出ない', async () => {
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        assert.equal(await v.page.locator('#cred-cannot').isVisible(), true);
        assert.match(await text(v.page, '#cred-cannot'), /この PC では環境変数を保存できません（Windows だけ）/);
        assert.equal(await v.page.locator('#f-user').isDisabled(), true);
        assert.equal(await v.page.locator('#f-key').isDisabled(), true);
        assert.equal(await v.page.locator('#f-user').inputValue(), USER);
        assert.equal(await v.page.locator('#f-key').inputValue(), '');
        assert.equal(await text(v.page, '#user-state'), '設定済み');
        assert.equal(await text(v.page, '#key-state'), '設定済み');
        assert.equal(await text(v.page, '#user-env-name'), 'COV_USER');
        assert.equal(await text(v.page, '#key-env-name'), 'COV_AUTH_KEY');
        assert.ok(!(await v.page.content()).includes(KEY), '画面に認証キーの値がある');
        assert.ok(!(await api('/api/state')).text.includes(KEY), '/api/state に認証キーの値がある');
        await shot(v.page, 'settings-credentials-linux-1280');
        // 画面を使わずに送っても、保存しない
        const refused = await post('/api/save', { credentials: [{ env: 'COV_USER', value: 'other' }] });
        assert.equal(refused.json.ok, false);
        assert.match(refused.json.problems.join('\n'), /この PC では保存できません（Windows だけ）/);
      } finally {
        await v.close();
      }
    });

    test('REQ-106 設定：ユーザ名の環境変数の名前に認証キーの変数名を入れても、認証キーの値は画面に出ない', async () => {
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await tab(v.page, 'options');
        await v.page.locator('#more > summary').click();
        await v.page.locator('#f-uenv').fill('COV_AUTH_KEY');
        await v.page.waitForTimeout(1200);
        await tab(v.page, 'basic');
        assert.notEqual(await v.page.locator('#f-user').inputValue(), KEY, 'ユーザ名の欄に認証キーの値が出た');
        assert.ok(!(await v.page.content()).includes(KEY), '画面に認証キーの値がある');
        const answer = await api('/api/credentials?user_env=COV_AUTH_KEY&key_env=COV_AUTH_KEY');
        assert.ok(!answer.text.includes(KEY), `/api/credentials が認証キーの値を返した: ${answer.text}`);
      } finally {
        await v.close();
      }
    });

    test('REQ-102 設定：新しい条件（チェッカーのワイルドカード・Impact・分類・対応・状態・上限）を作ると、保存の前に表で確かめられ、new-run がその条件で動く', async () => {
      const restore = keepSettings();
      const v = await open('#view=settings');
      let runDir;
      try {
        await settingsReady(v.page);
        await tab(v.page, 'filters');
        await v.page.locator('#addFilter').click();
        assert.equal(await v.page.locator('#ff-file').inputValue(), 'filter-1.yaml');
        await v.page.locator('#ff-file').fill('gui-misra');
        await v.page.locator('#ff-file').blur();
        assert.equal(await v.page.locator('#ff-file').inputValue(), 'gui-misra.yaml', '.yaml を足す');
        await v.page.locator('#ff-name').fill('MISRA の一部（画面で作った）');
        assert.equal(await v.page.locator('#ff-stream').inputValue(), STREAM, 'ストリームは最初の条件から写す');
        await v.page.locator('#ff-checkers').fill('MISRA C-2012 Rule 15.*\nMISRA C-2012 Rule 17.7');
        const check = (box, value) => v.page.locator(`${box} label`, { hasText: new RegExp(`^${value}$`) }).locator('input').check();
        await check('#ff-impacts', 'Medium');
        await check('#ff-impacts', 'Unspecified');
        await check('#ff-classification', 'Unclassified');
        await check('#ff-action', 'Undecided');
        await check('#ff-status', 'New');
        await v.page.getByLabel('管理者が足した Action').fill('Needs Review、要確認');
        await v.page.locator('#ff-limit').fill('5');
        assert.match(await v.page.locator('#flist button.item', { hasText: 'gui-misra.yaml' }).innerText(), /新規/);
        await shot(v.page, 'settings-filter-new-1280');
        const dlg = await openSave(v.page);
        assert.equal(dlg.title, '保存する内容');
        assert.match(dlg.body, /条件 gui-misra\.yaml を新しく作る/);
        const rows = Object.fromEntries((await dialogRows(v.page)).map(([what, , after]) => [what, after]));
        assert.equal(rows['表示名'], 'MISRA の一部（画面で作った）');
        assert.equal(rows['ストリーム'], STREAM);
        assert.equal(rows['チェッカー名'], 'MISRA C-2012 Rule 15.*, MISRA C-2012 Rule 17.7');
        assert.equal(rows.Impact, 'Medium, Unspecified');
        assert.equal(rows.Classification, 'Unclassified');
        assert.equal(rows.Action, 'Undecided, Needs Review, 要確認');
        assert.equal(rows.Status, 'New');
        assert.equal(rows['調べる警告の数（上限）'], '5');
        await shot(v.page, 'settings-save-confirm-1280', { fullPage: false });
        await v.page.locator('#dlgOk').click();
        await waitText(v.page, '#toast', '保存しました');
        await waitText(v.page, '#banners', '保存しました');
        assert.ok(fs.existsSync(path.join(filtersDir(), 'gui-misra.yaml')));
        // ct.py new-run（/coverity-run の最初）が、画面で作った条件をそのまま使う
        const made = await ct(['new-run', '--repo', W.repo, '--filter', 'gui-misra.yaml']);
        runDir = made.run_dir;
        assert.deepEqual(made.filter, { name: 'MISRA の一部（画面で作った）', project: 'envmon', streams: [STREAM],
          checkers: ['MISRA C-2012 Rule 15.*', 'MISRA C-2012 Rule 17.7'], impacts: ['Medium', 'Unspecified'],
          triage: { status: ['New'], classification: ['Unclassified'], action: ['Undecided', 'Needs Review', '要確認'] }, limit: 5 });
        assert.equal(made.limit, 5);
      } finally {
        await v.close();
        if (runDir) fs.rmSync(runDir, { recursive: true, force: true });
        restore();
      }
    });

    test('REQ-102 設定：条件を直す・削除すると保存の前の表に出て、そのとおりに書かれる。条件を 1 つも無くすことはできない', async () => {
      const restore = keepSettings();
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await tab(v.page, 'filters');
        await pickFilter(v.page, 'gui-none.yaml');
        await v.page.locator('#ff-limit').fill('7');
        assert.match(await v.page.locator('#flist button.item', { hasText: 'gui-none.yaml' }).innerText(), /変更あり/);
        await pickFilter(v.page, 'gui-review.yaml');
        await v.page.locator('#delFilter').click();
        assert.match(await v.page.locator('#flist button.item', { hasText: 'gui-review.yaml' }).innerText(), /削除する/);
        assert.equal(await text(v.page, '#delFilter'), '削除をやめる');
        assert.equal(await text(v.page, '#ff-state'), '保存すると、このファイルを削除します。');
        const dlg = await openSave(v.page);
        assert.match(dlg.body, /条件 gui-none\.yaml を変える/);
        assert.match(dlg.body, /条件 gui-review\.yaml を削除/);
        assert.deepEqual(await dialogRows(v.page), [['調べる警告の数（上限）', '（空）', '7']]);
        await v.page.locator('#dlgOk').click();
        await waitText(v.page, '#toast', '保存しました');
        assert.equal(fs.existsSync(path.join(filtersDir(), 'gui-review.yaml')), false);
        const state = (await api('/api/state')).json;
        assert.equal(state.filters.find((f) => f.file === 'gui-none.yaml').data.limit, 7);
        assert.deepEqual(state.filters.find((f) => f.file === 'gui-none.yaml').data.checkers, ['NO_SUCH_CHECKER'], 'ほかの項目はそのまま');
        // すべての条件を削除にすると、保存できない
        await v.page.reload();
        await settingsReady(v.page);
        await tab(v.page, 'filters');
        const count = await v.page.locator('#flist button.item').count();
        for (let i = 0; i < count; i++) {
          await v.page.locator('#flist button.item').nth(i).click();
          await v.page.locator('#delFilter').click();
        }
        const refused = await openSave(v.page);
        assert.equal(refused.title, '直すところがあります');
        assert.match(refused.body, /条件ファイルが 1 つもありません/);
        assert.equal(await v.page.locator('#dlgOk').isVisible(), false);
        await v.page.locator('#dlgCancel').click();
        assert.equal(fs.readdirSync(filtersDir()).length, count, 'ファイルは消えていない');
      } finally {
        await v.close();
        restore();
      }
    });

    test('REQ-102 設定：knowledge.md と「グループ化しない CID」を画面で直して保存できる（数字でない CID は入らない）', async () => {
      const restore = keepSettings();
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await tab(v.page, 'knowledge');
        const knowledge = (await v.page.locator('#kn').inputValue()) + '\n- fatal_error()（src/app/fault.c）は戻らない（試験で足した）\n';
        await v.page.locator('#kn').fill(knowledge);
        assert.equal(await v.page.locator('nav.tabs [data-tab="knowledge"] .mark.changed').count(), 1);
        await shot(v.page, 'settings-knowledge-1280');
        await tab(v.page, 'nogroup');
        await v.page.locator('#cidInput').fill('abc');
        await v.page.locator('#cidInput').press('Enter');
        assert.equal(await v.page.locator('#cidInput').getAttribute('aria-invalid'), 'true');
        assert.equal(await text(v.page, '#chips'), 'ありません。');
        await v.page.locator('#cidInput').fill('10220');
        await v.page.locator('#cidAdd').click();
        await v.page.locator('#cidInput').fill('10041');
        await v.page.locator('#cidInput').press('Enter');
        assert.deepEqual(await v.page.locator('#chips .chip.added').allInnerTexts().then((t) => t.map((x) => x.replace('×', '').trim())), ['10041', '10220']);
        await v.page.getByRole('button', { name: '10041 を外す' }).click();
        assert.equal(await v.page.locator('#chips .chip').count(), 1);
        await shot(v.page, 'settings-nogroup-1280');
        const dlg = await openSave(v.page);
        assert.match(dlg.body, /知識 knowledge\.md を変える/);
        assert.match(dlg.body, /グループ化しない CID を変える\s*足す: 10220/);
        await v.page.locator('#dlgOk').click();
        await waitText(v.page, '#toast', '保存しました');
        assert.equal(fs.readFileSync(path.join(W.repo, '.coverity-triage', 'knowledge.md'), 'utf8'), knowledge);
        const ng = fs.readFileSync(path.join(W.repo, '.coverity-triage', 'no-grouping.yaml'), 'utf8');
        assert.match(ng, /^# 却下されたグループの CID/);
        assert.match(ng, /cids:\n- 10220\n/);
        assert.equal(await v.page.locator('#dirty').isVisible(), false);
      } finally {
        await v.close();
        restore();
      }
    });

    test('REQ-102 設定：入力の誤り（http でない URL・使えない文字・範囲の外の数・長すぎる名前）が分かる文で示され、何も保存されない。http には注意が出る', async () => {
      const restore = keepSettings();
      const mtimes = () => [cfgFile(), ...fs.readdirSync(filtersDir()).map((n) => path.join(filtersDir(), n))].map((f) => `${f}:${fs.statSync(f).mtimeMs}`);
      const before = mtimes();
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await v.page.locator('#f-url').fill('ftp://coverity.example');
        let dlg = await openSave(v.page);
        assert.equal(dlg.title, '直すところがあります');
        await v.page.locator('#dlg-body .problems a', { hasText: 'Coverity の URL を、http:// か https:// で始まるアドレスで入れてください' }).click();
        assert.equal(await v.page.evaluate(() => document.activeElement.id), 'f-url', '項目を押すとその欄に移る');
        assert.equal(await v.page.locator('#f-url').getAttribute('aria-invalid'), 'true');
        assert.equal(await v.page.locator('nav.tabs [data-tab="basic"] .mark.error').count(), 1);
        await shot(v.page, 'settings-errors-1280');
        await v.page.locator('#f-url').fill('http://coverity.example:8080');
        dlg = await openSave(v.page);
        assert.equal(dlg.title, '保存する内容');
        assert.match(dlg.body, /注意[\s\S]*URL が http です。認証キーが暗号化されずに送られます/);
        await v.page.locator('#dlgCancel').click();
        await v.page.locator('#f-url').fill(W.emuUrl);
        await tab(v.page, 'options');
        await v.page.locator('#more > summary').click();
        await v.page.locator('#f-uenv').fill('COV-USER');
        await v.page.locator('#f-par').fill('9');
        dlg = await openSave(v.page);
        assert.equal(dlg.title, '直すところがあります');
        assert.match(dlg.body, /ユーザ名の環境変数の名前は、英字・数字・_ だけにしてください/);
        assert.match(dlg.body, /同時に調べる数は 1〜8 の整数にしてください/);
        await v.page.locator('#dlgCancel').click();
        await v.page.locator('#f-uenv').fill('COV_USER');
        await v.page.locator('#f-par').fill('3');
        await tab(v.page, 'filters');
        await v.page.locator('#addFilter').click();
        await v.page.locator('#ff-file').fill('a:b');
        await v.page.locator('#ff-file').blur();
        await v.page.locator('#ff-limit').fill('0');
        dlg = await openSave(v.page);
        assert.match(dlg.body, /条件ファイルの名前「a:b\.yaml」は使えません/);
        assert.match(dlg.body, /a:b\.yaml: 上限は 1 以上の整数にしてください/);
        await v.page.locator('#dlgCancel').click();
        // 画面の検査は通るが、サーバーが断る長さの名前：断られたことと理由が分かる
        const long = 'x'.repeat(90);
        await v.page.locator('#ff-file').fill(long);
        await v.page.locator('#ff-file').blur();
        await v.page.locator('#ff-limit').fill('');
        dlg = await openSave(v.page);
        if (dlg.title === '保存する内容') {
          await v.page.locator('#dlgOk').click();
          await waitText(v.page, '#dlg-title', '保存できませんでした');
        }
        assert.match(await text(v.page, '#dlg-body'), new RegExp(`「${long}\\.yaml」は使えません`));
        assert.equal(fs.existsSync(path.join(filtersDir(), `${long}.yaml`)), false);
        assert.deepEqual(mtimes(), before, '何も保存されていない');
        assert.deepEqual(v.errors, []);
      } finally {
        await v.close();
        restore();
      }
    });

    test('REQ-102 設定：ストリームの欄に「a, b」と 2 つ書くと、1 つだけにするよう示され、保存されない', async () => {
      const restore = keepSettings();
      const file = path.join(filtersDir(), 'gui-none.yaml');
      const before = fs.readFileSync(file, 'utf8');
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await tab(v.page, 'filters');
        await pickFilter(v.page, 'gui-none.yaml');
        await v.page.locator('#ff-stream').fill(`${STREAM}, envmon-release`);
        const dlg = await openSave(v.page);
        if (dlg.title === '保存する内容') {
          await v.page.locator('#dlgOk').click();
          await waitFor(v.page, () => /保存しました|保存できませんでした/.test(document.querySelector('#toast')?.textContent + document.querySelector('#dlg-title')?.textContent), null, 15_000, '保存の結果');
        }
        await shot(v.page, 'settings-two-streams-1280', { fullPage: false });
        assert.equal(fs.readFileSync(file, 'utf8'), before, `ストリーム「${STREAM}, envmon-release」が 1 つのストリーム名として保存された`);
        assert.match(await text(v.page, '#dlg-body'), /ストリーム.*1 つだけ/);
      } finally {
        await v.close();
        restore();
      }
    });

    test('REQ-102 設定：config.yaml を画面で保存しても、人が書いた注釈（# の行と行末の注釈）は元の場所に残る', async () => {
      const restore = keepSettings();
      const withNotes = W.configText
        .replace('max_items: 100', 'max_items: 100  # 夜間に 100 件まで（チームで決めた）')
        .replace('vcs:\n', '# --- ここからバージョン管理（変えるときはチームに相談） ---\nvcs:\n');
      assert.notEqual(withNotes, W.configText);
      fs.writeFileSync(cfgFile(), withNotes, 'utf8');
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await tab(v.page, 'options');
        await v.page.locator('#more > summary').click();
        await v.page.locator('#f-par').fill('2');
        await openSave(v.page);
        await v.page.locator('#dlgOk').click();
        await waitText(v.page, '#toast', '保存しました');
        const saved = fs.readFileSync(cfgFile(), 'utf8');
        assert.match(saved, /parallel: 2/);
        assert.match(saved, /max_items: 100 {2}# 夜間に 100 件まで（チームで決めた）/, '行末の注釈が消えた');
        assert.match(saved, /# --- ここからバージョン管理（変えるときはチームに相談） ---\nvcs:/, '注釈の行が元の場所（vcs の前）にない');
      } finally {
        await v.close();
        restore();
      }
    });

    test('REQ-102 REQ-106 設定：「試しにビルドする」で修正前の最新のコードを make し、成功・失敗とログが出る。ビルドに認証キーは渡らない', async () => {
      const before = fs.statSync(cfgFile()).mtimeMs;
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await tab(v.page, 'options');
        await v.page.locator('input[name="verify-default"][value="build"]').check();
        assert.equal(await v.page.locator('#trialBtn').isVisible(), true);
        await trialBuild(v.page, null, 'ビルドできました');
        const ok = await text(v.page, '#trialResult');
        assert.match(ok, new RegExp(`リビジョン ${W.latest}`), 'origin の最新のリビジョン');
        assert.match(ok, /ログ: .*_trial-build[/\\]trial-build\.log/);
        assert.ok(fs.existsSync(path.join(W.out, '_trial-build', 'src', 'build', 'em4fw')), 'make が em4fw を作った');
        await shot(v.page, 'settings-trial-build-ok-1280');
        await trialBuild(v.page, 'make no-such-target', 'ビルドに失敗しました');
        assert.match(await text(v.page, '#trialResult pre'), /No rule to make target/);
        await shot(v.page, 'settings-trial-build-fail-1280');
        // ビルドのコマンドには、認証キーの環境変数を渡さない（K-70）
        await trialBuild(v.page, 'env; exit 3', 'ビルドに失敗しました');
        const log = fs.readFileSync(path.join(W.out, '_trial-build', 'trial-build.log'), 'utf8');
        assert.match(log, /COV_USER=triager/);
        assert.ok(!log.includes(KEY), 'ビルドのログに認証キーの値がある');
        assert.ok(!(await text(v.page, '#trialResult')).includes(KEY));
        assert.equal(fs.statSync(cfgFile()).mtimeMs, before, '試しのビルドは設定を保存しない');
      } finally {
        await v.close();
      }
    });
  });

  // ==== 異常 ======================================================================================

  describe('異常', () => {
    const PY_ERROR = /Traceback|AttributeError|TypeError|KeyError|UnicodeDecodeError|ValueError|codec can't decode|object has no attribute|Input should be/;
    const cp932 = (textValue) => {
      const r = spawnSync(PYTHON, ['-c', 'import sys; sys.stdout.buffer.write(sys.stdin.read().encode("cp932"))'],
        { input: textValue, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
      assert.equal(r.status, 0, String(r.stderr));
      return r.stdout;
    };

    test('REQ-103 異常：run.json が壊れた実行は、一覧から黙って消えずに、読めないことが分かる', async () => {
      const broken = path.join(W.out, '20200101-000002');
      fs.mkdirSync(broken, { recursive: true });
      fs.writeFileSync(path.join(broken, 'run.json'), '{"run_id": "20200101-000002", ', 'utf8');
      try {
        await brokenRunChecks();
      } finally {
        fs.rmSync(broken, { recursive: true, force: true });
      }
    });

    async function brokenRunChecks() {
      const m = await open('#run=20200101-000002&view=monitor');
      try {
        await waitText(m.page, '#monError', 'run.json');
        assert.doesNotMatch(await text(m.page, '#monError'), PY_ERROR);
      } finally {
        await m.close();
      }
      const v = await open('#view=summary');
      try {
        await waitFor(v.page, () => document.querySelectorAll('#resBody table.items tbody tr').length > 0, null, 15_000, '実行の一覧');
        const listed = await v.page.locator('#resBody table.items tbody tr td:first-child a').allInnerTexts();
        assert.ok(listed.includes(runId(W.runDone)), 'ほかの実行は出る（画面は止まらない）');
        await shot(v.page, 'abnormal-broken-runjson-list-1280');
        assert.match(await text(v.page, '#resBody'), /20200101-000002/, '壊れた実行が一覧にも注意にも出ない');
      } finally {
        await v.close();
      }
    }

    test('REQ-103 異常：approvals.json が壊れた実行は、画面が止まらず、どのファイルが読めないかが分かる', async () => {
      const dir = copyRun(W.runDone, '20200101-000003');
      fs.writeFileSync(path.join(dir, 'approvals.json'), '{"version": 1, "items": {', 'utf8');
      const v = await open('#run=20200101-000003&view=summary');
      try {
        await summaryReady(v.page);
        const shown = await text(v.page, '#resBody');
        assert.match(shown, /approvals\.json/);
        assert.match(shown, /読めません/);
        await shot(v.page, 'abnormal-broken-approvals-1280');
        assert.deepEqual(v.errors, []);
      } finally {
        await v.close();
        fs.rmSync(dir, { recursive: true, force: true });
      }
    });

    test('REQ-103 REQ-025 異常：verify.json の形が違う実行（配列）でも、画面が止まらず、分かる文で示す', async () => {
      const dir = copyRun(W.runDone, '20200101-000004');
      fs.writeFileSync(path.join(dir, 'verify.json'), '[]', 'utf8');
      const v = await open('#run=20200101-000004&view=summary');
      try {
        await summaryReady(v.page);
        const shown = await text(v.page, '#resBody');
        await shot(v.page, 'abnormal-verify-json-array-1280');
        assert.doesNotMatch(shown, PY_ERROR, `Python の例外の文がそのまま出た: ${shown.slice(0, 300)}`);
        assert.ok(/verify\.json/.test(shown) || (await v.page.locator('#resBody table.items').count()) === 1,
          `表も、verify.json の何が悪いかも出ない: ${shown.slice(0, 300)}`);
      } finally {
        await v.close();
        fs.rmSync(dir, { recursive: true, force: true });
      }
    });

    test('REQ-103 異常：saved.json が壊れた作業の詳細は、画面が止まらず、どのファイルが読めないかが分かる', async () => {
      const dir = copyRun(W.runDone, '20200101-000005');
      fs.writeFileSync(path.join(dir, 'items', W.done.ja, 'saved.json'), '{"fix": ', 'utf8');
      const v = await open(`#run=20200101-000005&view=summary&item=${W.done.ja}`);
      try {
        await detailReady(v.page);
        const shown = await text(v.page, '#resBody');
        assert.match(shown, /saved\.json/);
        assert.doesNotMatch(shown, PY_ERROR);
        await v.page.getByText('← 一覧に戻る').click();
        await summaryReady(v.page);
        assert.equal(await v.page.locator('#resBody table.items tbody tr').count(), 8, '一覧は出る');
      } finally {
        await v.close();
        fs.rmSync(dir, { recursive: true, force: true });
      }
    });

    test('REQ-102 異常：config.yaml の書式が壊れていても、設定の画面は止まらず直すところが分かり、結果の画面も理由を示す', async () => {
      await withFiles({ [cfgFile()]: 'coverity: [\n  url: "x\n' }, async () => {
        const v = await open('#view=settings');
        try {
          await settingsSettled(v.page);
          assert.match(await text(v.page, '#banners'), /今の config\.yaml に直すところがあります[\s\S]*読めません/);
          assert.equal(await v.page.locator('#f-url').isVisible(), true);
          await shot(v.page, 'abnormal-broken-config-settings-1280');
          await v.page.locator('nav.views [data-view="summary"]').click();
          await waitFor(v.page, () => /実行を探せません|まだ実行がありません/.test(document.querySelector('#resBody')?.innerText ?? ''), null, 15_000, '結果の一覧の案内');
          const shown = await text(v.page, '#resBody');
          assert.match(shown, /実行を探せません: YAML の書式が正しくありません/);
          assert.deepEqual(v.errors, []);
        } finally {
          await v.close();
        }
      });
    });

    test('REQ-102 異常：条件ファイルに型の違う値（streams が文字列など）があっても、設定の画面は止まらない', async () => {
      const bad = path.join(filtersDir(), 'gui-bad.yaml');
      await withFiles({ [bad]: 'name: 型の違う値\nstreams: envmon-main\nimpacts: High\ntriage:\n  classification: Unclassified\n' }, async () => {
        const v = await open('#view=settings');
        try {
          await settingsSettled(v.page);
          await shot(v.page, 'abnormal-filter-wrong-types-1280');
          assert.deepEqual(v.errors, [], 'スクリプトのエラーで画面が止まった');
          assert.doesNotMatch(await text(v.page, '#banners'), /読み込めませんでした/);
          assert.equal(await v.page.locator('#f-url').inputValue(), W.emuUrl, '設定の値が出ない');
          await v.page.locator('nav.tabs [data-tab="filters"]').click();
          assert.match(await v.page.locator('#flist button.item', { hasText: 'gui-bad.yaml' }).innerText(), /読めない/);
        } finally {
          await v.close();
        }
      });
    });

    test('REQ-025 異常：条件ファイルの型の違いを知らせる文は日本語で、何を直すかが分かる', async () => {
      const bad = path.join(filtersDir(), 'gui-bad.yaml');
      await withFiles({ [bad]: 'name: 型の違う値\nstreams: envmon-main\n' }, async () => {
        const state = (await api('/api/state')).json;
        const error = state.filters.find((f) => f.file === 'gui-bad.yaml').error;
        assert.match(error, /streams/);
        assert.doesNotMatch(error, /Input should be/, `英語の検査の文がそのまま出る: ${error}`);
      });
    });

    test('REQ-102 異常：config.yaml に型の違う値（path_strip_prefixes が文字列）があっても、設定の画面は止まらない', async () => {
      const textValue = W.configText.replace(/path_strip_prefixes:\n\s*- \S+\n/, 'path_strip_prefixes: C:/build/\n');
      assert.notEqual(textValue, W.configText, '置き換える行が見つからない');
      await withFiles({ [cfgFile()]: textValue }, async () => {
        const v = await open('#view=settings');
        try {
          await settingsSettled(v.page);
          await shot(v.page, 'abnormal-config-wrong-types-1280');
          assert.deepEqual(v.errors, [], 'スクリプトのエラーで画面が止まった');
          assert.equal(await v.page.locator('#f-url').inputValue(), W.emuUrl);
          assert.match(await text(v.page, '#banners'), /直すところがあります/);
        } finally {
          await v.close();
        }
      });
    });

    test('REQ-102 REQ-025 異常：config.yaml の user_env が数（123）でも、設定の画面は止まらず、直すところが分かる', async () => {
      const textValue = W.configText.replace('user_env: COV_USER', 'user_env: 123');
      assert.notEqual(textValue, W.configText);
      await withFiles({ [cfgFile()]: textValue }, async () => {
        const v = await open('#view=settings');
        try {
          await settingsSettled(v.page);
          const banners = await text(v.page, '#banners');
          await shot(v.page, 'abnormal-config-user-env-number-1280');
          assert.doesNotMatch(banners, PY_ERROR, `Python の例外の文: ${banners}`);
          assert.equal(await v.page.locator('#f-url').inputValue(), W.emuUrl, '設定の値が出ない');
          assert.match(banners, /user_env/);
        } finally {
          await v.close();
        }
      });
    });

    test('REQ-102 REQ-025 異常：cp932 で保存された knowledge.md でも、設定の画面は止まらず、文字コードのことが分かる', async () => {
      const file = path.join(W.repo, '.coverity-triage', 'knowledge.md');
      await withFiles({ [file]: cp932('# プロジェクトの知識\n- fatal_error() は戻らない\n') }, async () => {
        const v = await open('#view=settings');
        try {
          await settingsSettled(v.page);
          const banners = await text(v.page, '#banners');
          await shot(v.page, 'abnormal-knowledge-cp932-1280');
          assert.doesNotMatch(banners, PY_ERROR, `Python の例外の文: ${banners}`);
          assert.equal(await v.page.locator('#f-url').inputValue(), W.emuUrl, '設定の値が出ない');
        } finally {
          await v.close();
        }
      });
    });

    test('REQ-103 REQ-025 異常：cp932 で保存された config.yaml のとき、結果の画面は分かる文で示す', async () => {
      await withFiles({ [cfgFile()]: cp932(`# 設定（メモ帳で cp932 のまま保存した）\n${W.configText}`) }, async () => {
        const v = await open('#view=summary');
        try {
          await waitFor(v.page, () => !/読み込んでいます/.test(document.querySelector('#resBody')?.innerText ?? '読み込んでいます'), null, 15_000, '結果の一覧');
          await v.page.waitForTimeout(500);
          const shown = await text(v.page, '#resBody');
          await shot(v.page, 'abnormal-config-cp932-results-1280');
          assert.doesNotMatch(shown, PY_ERROR, `Python の例外の文: ${shown.slice(0, 300)}`);
          assert.match(shown, /config\.yaml/);
        } finally {
          await v.close();
        }
      });
    });

    test('REQ-102 異常：cp932 で保存された config.yaml を、設定の画面で直して保存できる（画面の案内のとおり）', async () => {
      const restore = (() => {
        const before = fs.readFileSync(cfgFile());
        return () => fs.writeFileSync(cfgFile(), before);
      })();
      fs.writeFileSync(cfgFile(), cp932(`# 設定（メモ帳で cp932 のまま保存した）\n${W.configText}`));
      const v = await open('#view=settings');
      try {
        await settingsSettled(v.page);
        assert.match(await text(v.page, '#banners'), /画面で直して保存してください/);
        await v.page.locator('#f-url').fill(W.emuUrl);
        await v.page.locator('#f-out').fill('../結果 出力');
        await v.page.locator('#saveBtn').click();
        await v.page.locator('#dlg').waitFor({ state: 'visible' });
        if (await v.page.locator('#dlgOk').isVisible()) await v.page.locator('#dlgOk').click();
        await waitFor(v.page, () => /保存しました|保存できませんでした|直すところがあります/.test(document.querySelector('#toast')?.textContent + document.querySelector('#dlg-title')?.textContent), null, 15_000, '保存の結果');
        await shot(v.page, 'abnormal-config-cp932-save-1280', { fullPage: false });
        const body = (await text(v.page, '#dlg-body').catch(() => '')) || '';
        assert.doesNotMatch(body, PY_ERROR, `保存で Python の例外の文: ${body}`);
        assert.match(await text(v.page, '#toast'), /保存しました/, `保存できなかった: ${body}`);
        assert.doesNotThrow(() => new TextDecoder('utf-8', { fatal: true }).decode(fs.readFileSync(cfgFile())));
      } finally {
        await v.close();
        restore();
      }
    });

    test('REQ-102 異常：config.yaml の vcs.type が実際と違う（git のリポジトリに svn）とき、画面で直して保存できる', async () => {
      const textValue = W.configText.replace(/vcs:\n {2}type: git\n {2}base_branch: main\n {2}branch_prefix: coverity-fix\/\n/, 'vcs:\n  type: svn\n');
      assert.notEqual(textValue, W.configText, '置き換える行が見つからない');
      await withFiles({ [cfgFile()]: textValue }, async () => {
        const v = await open('#view=settings');
        try {
          await settingsSettled(v.page);
          await v.page.locator('nav.tabs [data-tab="options"]').click();
          await v.page.locator('#more > summary').click();
          await v.page.locator('#f-par').fill('2');
          await v.page.locator('#saveBtn').click();
          await v.page.locator('#dlg').waitFor({ state: 'visible' });
          if (await v.page.locator('#dlgOk').isVisible()) await v.page.locator('#dlgOk').click();
          await waitFor(v.page, () => /保存しました|保存できませんでした|直すところがあります/.test(document.querySelector('#toast')?.textContent + document.querySelector('#dlg-title')?.textContent), null, 15_000, '保存の結果');
          await shot(v.page, 'abnormal-vcs-type-mismatch-1280', { fullPage: false });
          const body = await text(v.page, '#dlg-body').catch(() => '');
          assert.match(await text(v.page, '#toast'), /保存しました/, `保存できず、画面で直す手段もない（バージョン管理の種類は書き換えられない）: ${body}`);
          assert.match(fs.readFileSync(cfgFile(), 'utf8'), /type: git/);
        } finally {
          await v.close();
        }
      });
    });

    test('REQ-103 異常：承認を保存したあとレポートを書き直せなかったとき、画面の承認と approvals.json が食い違わない', async () => {
      const dir = copyRun(W.runDone, '20200101-000006');
      fs.rmSync(path.join(dir, 'issues.mapped.json'));   // 詳細レポートを書き直すのに要るファイル
      const id = W.done.ja;
      const v = await open('#run=20200101-000006&view=summary');
      try {
        await summaryReady(v.page);
        const sel = v.page.locator(`select[aria-label="${id} の承認"]`);
        const was = await sel.inputValue();
        await sel.selectOption('reject');
        await waitFor(v.page, () => /保存しました|保存できませんでした/.test(document.querySelector('#toast')?.textContent ?? ''), null, 15_000, '保存の結果');
        await v.page.waitForTimeout(300);
        const shown = await sel.inputValue();
        const saved = approvals(dir)[id].decision ?? '';
        await shot(v.page, 'abnormal-approval-saved-but-report-failed-1280');
        assert.equal(shown, saved, `画面の承認は「${shown || '反映しない'}」、approvals.json は「${saved || '反映しない'}」（元は ${was}）。/coverity-apply は approvals.json のほうを反映する`);
      } finally {
        await v.close();
        fs.rmSync(dir, { recursive: true, force: true });
      }
    });

    test('REQ-106 異常：AI の文・条件の名前・ファイル名・理由に入った HTML や script は文字のまま出て、動かない', async () => {
      const run = runId(W.runDone);
      const views = [
        ['実行の一覧', '#view=summary', async (p) => waitFor(p, () => document.querySelectorAll('#resBody table.items tbody tr').length > 0)],
        ['一覧の表', `#run=${run}&view=summary`, summaryReady],
        ['詳細（見立て）', `#run=${run}&view=summary&item=${W.done.xss}`, detailReady],
        ['詳細（ファイル名）', `#run=${run}&view=summary&item=${W.done.xssFile}`, detailReady],
        ['監視', `#run=${runId(W.runFail)}&view=monitor`, monitorReady],
        ['設定の条件', '#view=settings', settingsSettled],
      ];
      for (const [name, hash, ready] of views) {
        const v = await open(hash);
        try {
          await ready(v.page);
          if (name === '設定の条件') await v.page.locator('nav.tabs [data-tab="filters"]').click();
          if (name === '監視') {
            for (const s of await v.page.locator('#alerts details summary').all()) await s.click();
          }
          await v.page.waitForTimeout(800);
          const shown = await v.page.evaluate(() => document.body.innerText);
          if (name === '実行の一覧' || name === '設定の条件') assert.ok(shown.includes(XSS_FILTER_NAME), `${name}: 条件の名前が文字のまま出ない`);
          if (name === '一覧の表' || name === '詳細（見立て）') assert.ok(shown.includes(XSS_SUMMARY), `${name}: 見立てが文字のまま出ない`);
          if (name === '詳細（見立て）') {
            assert.equal(await v.page.locator('#ap-comment').inputValue(), XSS_COMMENT);
            await waitFor(v.page, () => document.querySelector('#repFrame')?.contentDocument?.body?.innerText.includes('案A'), null, 15_000, '詳細レポート');
            const frame = v.page.frameLocator('#repFrame');
            assert.match(await frame.locator('body').innerText(), /<img src=x onerror="window.__xss=2">/);
            assert.equal(await frame.locator('img').count(), 0, '詳細レポートに img が入った');
            assert.equal(await frame.locator('script').evaluateAll((n) => n.filter((x) => /__xss/.test(x.textContent)).length), 0);
          }
          if (name === '詳細（ファイル名）') {
            assert.ok(shown.includes(XSS_FILE), `${name}: ファイル名が文字のまま出ない`);
            await v.page.locator('.files button.linkish', { hasText: XSS_FILE }).click();
            await waitFor(v.page, () => document.querySelector('#repFrame')?.contentDocument?.body?.innerText.includes('<script>window.__xss=6</script>'), null, 15_000, '修正後のファイル');
            const frame = v.page.frameLocator('#repFrame');
            assert.equal(await frame.locator('h1').innerText(), XSS_FILE);
            assert.equal(await frame.locator('img').count(), 0);
            await shot(v.page, 'abnormal-html-in-file-name-1280');
          }
          if (name === '監視') assert.ok(shown.includes(FAIL_REASON), '理由が文字のまま出ない');
          if (name === '一覧の表') await shot(v.page, 'abnormal-html-in-ai-text-1280');
          await assertNoScript(v, name);
          assert.deepEqual(v.errors, [], name);
        } finally {
          await v.close();
        }
      }
    });
  });

  // ==== 見た目 ====================================================================================

  describe('見た目', () => {
    const runsReady = (page) => waitFor(page, () => document.querySelectorAll('#resBody table.items tbody tr').length > 0, null, 15_000, '実行の一覧');
    const frameReady = (page) => waitFor(page, () => document.body && document.body.innerText.length > 50, null, 15_000, 'ページ');
    const SCREENS = [
      ['settings-basic', '設定（基本設定）', () => '#view=settings', settingsReady],
      ['settings-options', '設定（オプション設定。長い行のある試しのビルドのログ）', () => '#view=settings', settingsReady, async (p) => {
        await p.locator('nav.tabs [data-tab="options"]').click();
        await p.locator('input[name="verify-default"][value="build+analyze"]').check();
        await p.locator('#more > summary').click();
        await trialBuild(p, `echo ${LONG_TOKEN}; exit 2`, 'ビルドに失敗しました');
      }],
      ['settings-filters', '設定（条件）', () => '#view=settings', settingsReady, (p) => p.locator('nav.tabs [data-tab="filters"]').click()],
      ['settings-nogroup', '設定（グループ化しない CID）', () => '#view=settings', settingsReady, (p) => p.locator('nav.tabs [data-tab="nogroup"]').click()],
      ['monitor', '監視', () => `#run=${runId(W.runMid)}&view=monitor`, monitorReady],
      ['results-runs', '結果（実行の一覧）', () => '#view=summary', runsReady],
      ['results-summary', '結果（作業の表）', () => `#run=${runId(W.runDone)}&view=summary`, summaryReady],
      ['results-detail', '結果（詳細と承認。長い見立て）', () => `#run=${runId(W.runDone)}&view=summary&item=${W.done.long}`, detailReady],
      ['report-cid', '詳細レポート cid/<ID>.html', () => `/files/${runId(W.runDone)}/cid/${W.done.long}.html`, frameReady],
      ['view-patch', '差分の表示', () => `/view/${runId(W.runDone)}/patches/${W.done.ja}-fix.patch`, frameReady],
    ];
    const openScreen = async (hash, opts) => {
      if (!hash.startsWith('/')) return open(hash, opts);
      const v = await open('', opts);   // Cookie の合言葉を受け取ってから、実行のファイルを開く
      await v.page.goto(`http://127.0.0.1:${W.port}${hash}`);
      return v;
    };

    for (const [key, name, hash, ready, prepare] of SCREENS) {
      test(`REQ-030 ${name}：幅 390・768・1280・1920 で、はみ出し・重なり・切れがない`, async () => {
        const v = await openScreen(hash(), { width: 1280, height: 900 });
        try {
          await ready(v.page);
          if (prepare) await prepare(v.page);
          const found = [];
          for (const width of [390, 768, 1280, 1920]) {
            await v.page.setViewportSize({ width, height: 900 });
            await v.page.waitForTimeout(500);
            for (const p of await layout(v.page)) found.push(`${width}px ${p}`);
            await shot(v.page, `layout-${key}-${width}`);
          }
          if (found.length) assert.fail(`崩れ ${found.length} 件（最初の 12 件）:\n${found.slice(0, 12).join('\n')}`);
        } finally {
          await v.close();
        }
      });

      test(`REQ-030 ${name}：文字の大きさ 200%（ブラウザーの拡大。見かけの幅が半分）でも、はみ出し・重なり・切れがない`, async () => {
        const v = await openScreen(hash(), { width: 640, height: 450, scale: 2 });
        try {
          await ready(v.page);
          if (prepare) await prepare(v.page);
          const found = [];
          for (const width of [384, 640, 960]) {   // 768・1280・1920 の画面を 200% に拡大したとき
            await v.page.setViewportSize({ width, height: 450 });
            await v.page.waitForTimeout(500);
            for (const p of await layout(v.page)) found.push(`${width * 2}px の 200%: ${p}`);
            await shot(v.page, `zoom200-${key}-${width * 2}`);
          }
          if (found.length) assert.fail(`崩れ ${found.length} 件（最初の 12 件）:\n${found.slice(0, 12).join('\n')}`);
        } finally {
          await v.close();
        }
      });

      test(`REQ-030 ${name}：文字と背景の色の差が 4.5:1 以上（WCAG 2.2 AA。明るい配色と暗い配色）`, async () => {
        const found = [];
        for (const colorScheme of ['light', 'dark']) {
          const v = await openScreen(hash(), { colorScheme });
          try {
            await ready(v.page);
            if (prepare) await prepare(v.page);
            await v.page.evaluate(() => { for (const d of document.querySelectorAll('details')) d.open = true; });
            await v.page.waitForTimeout(300);
            const { problems, checked } = await v.page.evaluate(contrastProblems, 4.5);
            assert.ok(checked > 10, `${colorScheme}: 調べた文字が少なすぎる（${checked}）`);
            for (const p of problems) found.push(`${colorScheme}: ${p}`);
            if (colorScheme === 'dark') await shot(v.page, `dark-${key}-1280`);
          } finally {
            await v.close();
          }
        }
        assert.deepEqual(found, []);
      });
    }

    test('REQ-030 上の帯のリポジトリのパスは、狭い幅（390px と、1280px の 200%）でも切れずに読める', async () => {
      const found = [];
      for (const [width, scale] of [[390, 1], [640, 2]]) {
        const v = await open(`#run=${runId(W.runDone)}&view=summary`, { width, height: 800, scale });
        try {
          await summaryReady(v.page);
          const repo = await v.page.locator('#repo').evaluate((n) => ({ cut: n.scrollWidth > n.clientWidth + 1, overflow: getComputedStyle(n).textOverflow }));
          if (repo.cut) found.push(`${width === 640 ? '1280px の 200%' : `${width}px`}: パスが「…」で切れる（全体は title にしかない）`);
          await shot(v.page, `header-repo-path-${width === 640 ? 'zoom200-1280' : width}`, { fullPage: false });
        } finally {
          await v.close();
        }
      }
      assert.deepEqual(found, []);
    });

    test('REQ-030 設定のタブは、幅 390px（と 768px の 200%）でも、すべて画面の中に見える', async () => {
      const found = [];
      for (const [width, scale] of [[390, 1], [384, 2]]) {
        const v = await open('#view=settings', { width, height: 800, scale });
        try {
          await settingsReady(v.page);
          for (const b of await v.page.locator('nav.tabs button').all()) {
            const box = await b.boundingBox();
            if (box.x < 0 || box.x + box.width > width + 1) found.push(`${width === 384 ? '768px の 200%' : `${width}px`}: 「${(await b.innerText()).trim()}」が画面の外（${Math.round(box.x)}〜${Math.round(box.x + box.width)}px）`);
          }
          await shot(v.page, `settings-tabs-${width === 384 ? 'zoom200-768' : width}`, { fullPage: false });
        } finally {
          await v.close();
        }
      }
      assert.deepEqual(found, []);
    });

    test('REQ-030 状態は色だけでなく文字でも示される（札・確信度・検証・枠の図・凡例）', async () => {
      const v = await open(`#run=${runId(W.runFail)}&view=monitor`);
      try {
        await monitorReady(v.page);
        for (const pill of await v.page.locator('#alerts .pill').allInnerTexts()) assert.match(pill, /^(重大|注意)$/);
        assert.deepEqual((await v.page.locator('.legend span').allInnerTexts()).map((s) => s.trim()),
          ['動いている', '結果を直している・メインが代わりにしている', '止まっているかもしれない（10 分以上動きがない。初回解析は 30 分）', 'エラー', '終わった', '空き・待ち']);
        await v.page.goto(`${W.gui}#run=${runId(W.runReview)}&view=summary`);
        await summaryReady(v.page);
        for (const badge of await v.page.locator('#resBody .badge').allInnerTexts()) assert.match(badge, /^(修正|逸脱|高|中|低)$/);
        for (const pill of await v.page.locator('#resBody .pill').allInnerTexts()) assert.ok(pill.trim().length > 0, '文字のない札');
        assert.match(await text(v.page, '#resBody table.items'), /✔ 検証 合格|⚠ 検証 要確認/);
      } finally {
        await v.close();
      }
    });

    test('REQ-030 「保存していない変更」と「直すところ」の印は、色だけでなく文字（形）でも区別できる', async () => {
      const v = await open('#view=settings');
      try {
        await settingsReady(v.page);
        await v.page.locator('#f-url').fill(`${W.emuUrl}/x`);   // 基本設定：変更あり
        await v.page.locator('nav.tabs [data-tab="options"]').click();
        await v.page.locator('#more > summary').click();
        await v.page.locator('#f-par').fill('9');                // オプション設定：直すところ
        await v.page.locator('#saveBtn').click();
        await v.page.locator('#dlgCancel').click();
        const mark = (tab) => v.page.locator(`nav.tabs [data-tab="${tab}"] .mark`).evaluate((n) => ({
          cls: n.className, text: n.textContent, label: n.getAttribute('aria-label') ?? '', hidden: n.closest('button').innerText }));
        const changed = await mark('basic');
        const error = await mark('options');
        assert.match(changed.cls, /changed/);
        assert.match(error.cls, /error/);
        await shot(v.page, 'marks-changed-and-error-1280', { fullPage: false });
        assert.notEqual(changed.text + changed.label, error.text + error.label,
          `「変更あり」と「直すところ」の印が、どちらも「${changed.text}」で色だけが違う（文字は title にしかない）`);
      } finally {
        await v.close();
      }
    });
  });

  // ==== svn のリポジトリ、実行 0 件、サーバを止める ==================================================

  describe('svn・実行 0 件・止める', () => {
    test('REQ-029 REQ-103 svn のリポジトリ（実行 0 件）の画面を --foreground で開き、「サーバを止める」で止まる', async (t) => {
      if (!W.seed.svn) {
        t.skip('svn が無いので、svn の題材がない');
        return;
      }
      const wc = W.seed.svn.working_copy;
      await ct(['init-config', '--repo', wc, '--url', W.emuUrl, '--project', 'envmon', '--stream', 'envmon-svn', '--strip-prefix', REMOTE_ROOT]);
      const child = spawn(UV, ['run', '--native-tls', '--quiet', CT, 'gui', '--repo', wc, '--foreground', '--no-browser'],
        { cwd: W.tmp, env: W.env, stdio: ['ignore', 'pipe', 'pipe'] });
      children.push(child);
      let out = '';
      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (c) => { out += c; });
      const exited = new Promise((resolve) => child.on('exit', (code) => resolve(code)));
      // --foreground は終わるまで URL を出さないので、画面のサーバーが書く状態のファイルから読む（ct.py gui が使い回しに使うもの）
      const real = fs.realpathSync(wc);
      const stateFile = path.join(W.env.CT_GUI_STATE_DIR, `${crypto.createHash('sha256').update(real, 'utf8').digest('hex').slice(0, 16)}.json`);
      for (let i = 0; i < 150 && !fs.existsSync(stateFile); i++) await sleep(200);
      const record = readJson(stateFile);
      W.svnPort = record.port;
      W.svnToken = record.token;
      const base = `http://127.0.0.1:${record.port}/?t=${record.token}`;
      const v = await open('', {});
      try {
        await v.page.goto(`${base}#view=settings`);
        await settingsReady(v.page);
        assert.equal(await text(v.page, '#repo'), `${real}（svn）`);
        assert.equal(await v.page.locator('#f-vcs').inputValue(), 'svn');
        assert.equal(await v.page.locator('#f-base').isVisible(), false);
        assert.match(await v.page.locator('[data-show="svn"]').innerText(), /svn では使いません/);
        await shot(v.page, 'svn-settings-1280');
        await v.page.locator('nav.views [data-view="monitor"]').click();
        await waitText(v.page, '#monEmpty', 'まだ実行がありません');
        assert.equal(await v.page.locator('#monRun').isDisabled(), true);
        assert.equal(await v.page.locator('#monRun option').innerText(), '（実行がありません）');
        await shot(v.page, 'svn-zero-runs-monitor-1280');
        await v.page.locator('nav.views [data-view="summary"]').click();
        await waitText(v.page, '#resBody', 'まだ実行がありません（/coverity-run で始めます。結果を置く場所:');
        await shot(v.page, 'svn-zero-runs-results-1280');
        // 保存していない変更があるまま止める：止める前の確認に出る
        await v.page.locator('nav.views [data-view="settings"]').click();
        await v.page.locator('#f-url').fill(`${W.emuUrl}/changed`);
        await v.page.locator('#menuBtn').click();
        await v.page.locator('#stopBtn').click();
        await v.page.locator('#stopDlg').waitFor({ state: 'visible' });
        assert.match(await text(v.page, '#stopNotes'), /保存していない設定の変更があります/);
        await shot(v.page, 'server-stop-confirm-1280', { fullPage: false });
        await v.page.locator('#stopOk').click();
        await waitText(v.page, 'body', 'サーバを止めました');
        assert.equal(await v.page.title(), 'サーバを止めました — Coverity トリアージ');
        await shot(v.page, 'server-stopped-1280');
        const code = await Promise.race([exited, sleep(15_000).then(() => 'TIMEOUT')]);
        assert.equal(code, 0, `ct.py gui --foreground が終わらない（${code}）`);
        const result = JSON.parse(out);
        assert.equal(result.ok, true);
        assert.equal(result.closed_by, 'button');
        assert.deepEqual(result.saved, []);
        await assert.rejects(request({ target: '/api/ping', port: record.port, timeout: 2000 }), /ECONNREFUSED|TIMEOUT|socket hang up/);
        assert.equal(fs.existsSync(stateFile), false, '止まったサーバーの状態のファイルが残った');
        W.svnPort = null;
      } finally {
        await v.close();
      }
    });

    test('REQ-029 試しのビルドが動いている間に「サーバを止める」と、確認のとおりビルドも止まる（子のプロセスが残らない）', async (t) => {
      if (!W.seed.svn || !fs.existsSync('/proc')) {
        t.skip(!W.seed.svn ? 'svn が無いので、svn の題材がない' : 'プロセスの一覧（/proc）がない');
        return;
      }
      const wc = W.seed.svn.working_copy;
      const marker = '23.4567';   // ビルドのコマンドの目印（ps で探す）
      const child = spawn(UV, ['run', '--native-tls', '--quiet', CT, 'gui', '--repo', wc, '--foreground', '--no-browser'],
        { cwd: W.tmp, env: W.env, stdio: ['ignore', 'pipe', 'pipe'] });
      children.push(child);
      const exited = new Promise((resolve) => child.on('exit', (code) => resolve(code)));
      const real = fs.realpathSync(wc);
      const stateFile = path.join(W.env.CT_GUI_STATE_DIR, `${crypto.createHash('sha256').update(real, 'utf8').digest('hex').slice(0, 16)}.json`);
      for (let i = 0; i < 150 && !fs.existsSync(stateFile); i++) await sleep(200);
      const record = readJson(stateFile);
      W.svnPort = record.port;
      W.svnToken = record.token;
      const sleeping = () => fs.readdirSync('/proc').filter((n) => /^\d+$/.test(n)).filter((pid) => {
        try {
          return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').join(' ').includes(`sleep ${marker}`);
        } catch {
          return false;
        }
      });
      const v = await open('', {});
      try {
        await v.page.goto(`http://127.0.0.1:${record.port}/?t=${record.token}#view=settings`);
        await settingsReady(v.page);
        await v.page.locator('nav.tabs [data-tab="options"]').click();
        await v.page.locator('input[name="verify-default"][value="build"]').check();
        await v.page.locator('#f-bcmd').fill(`sleep ${marker}; echo done`);
        await v.page.locator('#trialBtn').click();
        await waitText(v.page, '#trialResult', 'ビルドしています');
        for (let i = 0; i < 50 && !sleeping().length; i++) await sleep(100);
        assert.ok(sleeping().length, 'ビルドのコマンドが動いていない');
        await v.page.locator('#menuBtn').click();
        await v.page.locator('#stopBtn').click();
        assert.match(await text(v.page, '#stopNotes'), /試しのビルドが動いています。止めると、ビルドも止まります。/);
        await v.page.locator('#stopOk').click();
        await waitText(v.page, 'body', 'サーバを止めました');
        assert.equal(await Promise.race([exited, sleep(15_000).then(() => 'TIMEOUT')]), 0);
        W.svnPort = null;
        await sleep(1000);
        const left = sleeping();
        for (const pid of left) {
          try {
            process.kill(Number(pid), 'SIGKILL');
          } catch {
            // もう止まっている
          }
        }
        assert.deepEqual(left, [], `サーバを止めたあとも、ビルドのコマンド（sleep ${marker}）が動き続けている`);
      } finally {
        await v.close();
      }
    });
  });
}
