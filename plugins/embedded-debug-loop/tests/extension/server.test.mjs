// Web管理画面のサーバー：合言葉、画面のファイル、状態、設定の保存、開始、止める、過去の実行。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import nodeHttp from 'node:http';
import path from 'node:path';
import test from 'node:test';

import { createPersist } from '../../com.github.copilot/extensions/embedded-debug-loop/core/persist.mjs';
import { CWD, EXT_DIR, LEAD, VERIFIER, WORKER, http, startRun, startWith, tempDir } from './helpers.mjs';

// 作業フォルダ。エクステンションと同じく path.resolve で作る（Windows ではドライブ名が付く）
const A = path.resolve('/w/a');
const B = path.resolve('/w/b');

// エージェントの一覧（既定の3つに、テストで選ぶ名前を足す）
const agentsWith = (...names) => [
  { id: WORKER, name: 'helper-a' },
  { id: LEAD, name: 'lead-c' },
  { id: VERIFIER, name: 'checker-b' },
  ...names.map((id) => ({ id, name: id.slice(id.lastIndexOf(':') + 1) })),
];

async function opened(t, options) {
  const ctx = await startWith(t, options);
  await ctx.fake.call('open', { request: '表示を直す', criteria: ['完了と出る'] });
  const url = new URL(ctx.app.store.url);
  const token = url.searchParams.get('k');
  const api = (p) => `${url.origin}${p}${p.includes('?') ? '&' : '?'}k=${token}`;
  return { ...ctx, url, token, api };
}

test('合言葉がないと断る。127.0.0.1 だけで待ち受ける', async (t) => {
  const { url, token, api } = await opened(t);
  assert.equal(url.hostname, '127.0.0.1');
  assert.equal(token.length, 24);
  for (const p of ['/', '/app.js', '/api/state']) {
    const r = await http(`${url.origin}${p}`);
    assert.equal(r.status, 403, p);
    assert.match(r.data.error, /合言葉が違います/);
  }
  assert.equal((await http(`${url.origin}/api/state?k=${'x'.repeat(24)}`)).status, 403);
  assert.equal((await http(api('/api/state'))).status, 200);
});

test('Host が違う問い合わせは断る（ほかのサイトからの呼び出しを防ぐ）', async (t) => {
  const { url, token } = await opened(t);
  const status = await new Promise((resolve, reject) => {
    const req = nodeHttp.request({ host: '127.0.0.1', port: url.port, path: `/api/state?k=${token}`, headers: { Host: `evil.example:${url.port}` } }, (res) => {
      res.resume();
      resolve(res.statusCode);
    });
    req.on('error', reject);
    req.end();
  });
  assert.equal(status, 403);
});

test('画面のファイル：正しい種類で返し、合言葉をクッキーでも渡す（app.js などは URL に合言葉がなくても読める）', async (t) => {
  const { url, api } = await opened(t);
  const page = await http(api('/'));
  assert.equal(page.status, 200);
  assert.match(page.headers.get('content-type'), /^text\/html/);
  assert.equal(page.headers.get('cache-control'), 'no-store');
  const cookie = page.headers.get('set-cookie').split(';')[0];
  assert.match(page.headers.get('set-cookie'), /HttpOnly; SameSite=Strict/);
  const js = await http(`${url.origin}/app.js`, { headers: { Cookie: cookie } });
  assert.equal(js.status, 200);
  assert.match(js.headers.get('content-type'), /^text\/javascript/);
  const css = await http(`${url.origin}/style.css`, { headers: { Cookie: cookie } });
  assert.match(css.headers.get('content-type'), /^text\/css/);
  const state = await http(`${url.origin}/api/state`, { headers: { Cookie: cookie } });
  assert.equal(state.status, 200);
  assert.equal((await http(`${url.origin}/app.js`, { headers: { Cookie: 'edl_k_1=wrong' } })).status, 403);
});

test('状態：設定・選べるエージェントとモデル・下書き・実行・一覧を返す', async (t) => {
  const { api } = await opened(t);
  const { data } = await http(api('/api/state'));
  assert.equal(typeof data.version, 'number');
  assert.deepEqual(data.settings, { worker: WORKER, verifier: VERIFIER, maxIterations: 3, workerModel: '', workerEffort: '', verifierModel: '', verifierEffort: '' });
  assert.deepEqual(data.options.agents, [{ name: WORKER, description: '手伝う' }, { name: VERIFIER, description: '確かめる' }]);
  assert.deepEqual(data.options.models, [{ id: 'model-x', name: 'モデル X' }, { id: 'model-y', name: 'model-y' }]);
  assert.deepEqual(data.options.efforts, ['low', 'medium', 'high', 'xhigh']);
  assert.deepEqual(data.draft, { request: '表示を直す', criteria: ['完了と出る'], prepared: null });
  assert.equal(data.run, null);
  assert.deepEqual(data.runs, []);
  assert.equal(data.waiting, null);
  assert.equal(data.folder, CWD);
});

test('状態：エージェントの一覧が読めなくても、前の一覧のまま返す', async (t) => {
  const ctx = await opened(t);
  await http(ctx.api('/api/state'));
  ctx.fake.session.rpc.agent.list = async () => {
    throw new Error('読めない');
  };
  const { data } = await http(ctx.api('/api/state'));
  assert.equal(data.options.agents.length, 2);
});

test('設定の保存：ファイルに残り、起動し直しても読み直す。誤りは 400', async (t) => {
  const dataDir = path.join(tempDir(t), 'data');
  const agents = agentsWith('x-plugin:other');
  const a = await opened(t, { dataDir, fake: { agents } });
  const body = { worker: 'x-plugin:other', verifier: VERIFIER, maxIterations: 7, workerModel: 'model-y', workerEffort: 'high', verifierModel: '', verifierEffort: '既定', request: '書き直した作業', criteria: '条件1\n\n条件2\n' };
  const r = await http(a.api('/api/settings'), { method: 'POST', body });
  assert.equal(r.status, 200);
  assert.equal(r.data.ok, true);
  const saved = JSON.parse(fs.readFileSync(path.join(dataDir, 'settings.json'), 'utf8'));
  // 使うエージェントの名前は保存しない（部品を替えたとき、前の名前が残らないように）
  assert.deepEqual(saved, { folders: { [CWD]: { maxIterations: 7, workerModel: 'model-y', workerEffort: 'high', verifierModel: '', verifierEffort: '' } } });
  assert.equal(a.app.store.settings.worker, 'x-plugin:other', 'この会話のあいだは、選んだ名前を使う');
  const state = (await http(a.api('/api/state'))).data;
  assert.deepEqual(state.draft.criteria, ['条件1', '条件2']);
  assert.equal(state.draft.request, '書き直した作業');

  const bad = await http(a.api('/api/settings'), { method: 'POST', body: { maxIterations: 'たくさん' } });
  assert.equal(bad.status, 400);
  assert.match(bad.data.error, /繰り返しの上限/);

  const b = await opened(t, { dataDir, fake: { agents } });
  assert.equal(b.app.store.settings.worker, WORKER, '起動し直すと、既定（defaults.json）の名前から始める');
  assert.equal(b.app.store.settings.maxIterations, 7);
});

test('設定の保存：下書きがなく、頼む作業も条件も空なら、下書きを作らない', async (t) => {
  const { fake, app } = await startRun(t);
  await fake.call('finish', { outcome: 'success', conclusion: 'できた', retro: 'なし' });
  assert.equal(app.store.draft, null);
  await app.control.saveSettings({ request: '', criteria: [], worker: WORKER, verifier: VERIFIER, maxIterations: 3 });
  assert.equal(app.store.snapshot().draft, null);
  await app.control.saveSettings({ request: '次の作業', worker: WORKER, verifier: VERIFIER, maxIterations: 3 });
  assert.equal(app.store.snapshot().draft.request, '次の作業');
});

test('設定の保存：ファイルに書けなければ、画面に出す注意を返す', async (t) => {
  const dir = tempDir(t);
  const blocker = path.join(dir, 'not-a-folder');
  fs.writeFileSync(blocker, 'x'); // フォルダの代わりにファイルがあり、書けない
  const { api } = await opened(t, { dataDir: path.join(blocker, 'data') });
  const r = await http(api('/api/settings'), { method: 'POST', body: { worker: WORKER, verifier: VERIFIER, maxIterations: 4 } });
  assert.equal(r.status, 200);
  assert.match(r.data.warning, /ファイルに保存できませんでした/);
});

test('問い合わせの形の誤り：大きすぎる・JSON でない・ない道・違う操作', async (t) => {
  const { api } = await opened(t);
  const big = await http(api('/api/settings'), { method: 'POST', body: JSON.stringify({ request: 'あ'.repeat(30000) }) });
  assert.equal(big.status, 413);
  assert.match(big.data.error, /大きすぎます/);
  const notJson = await http(api('/api/start'), { method: 'POST', body: '{こわれた' });
  assert.equal(notJson.status, 400);
  assert.match(notJson.data.error, /JSON/);
  assert.equal((await http(api('/api/nothing'))).status, 404);
  assert.equal((await http(api('/api/start'))).status, 405);
  assert.equal((await http(api('/'), { method: 'POST', body: {} })).status, 405);
});

test('開始と止める：止めると Copilot CLI に止める合図を送り、終わりの知らせ（idle）で「止めた」で終わる', async (t) => {
  const { fake, app, api } = await opened(t);
  const start = await http(api('/api/start'), { method: 'POST', body: { request: '表示を直す', criteria: ['完了と出る'], worker: WORKER, verifier: VERIFIER, maxIterations: 2 } });
  assert.equal(start.status, 200);
  assert.match(start.data.runId, /^R-/);
  assert.equal(fake.sent.length, 1);
  const again = await http(api('/api/start'), { method: 'POST', body: { request: 'x', criteria: ['y'] } });
  assert.equal(again.status, 409);
  assert.match(again.data.error, /実行中です/);
  assert.equal((await http(api('/api/settings'), { method: 'POST', body: { maxIterations: 4 } })).status, 409);

  fake.emit('assistant.turn_start', {});
  fake.emit('subagent.started', { agentType: WORKER, toolCallId: 't1' }, 'w1');
  const stop = await http(api('/api/stop'), { method: 'POST', body: {} });
  assert.equal(stop.status, 200);
  assert.equal(fake.aborts, 1);
  let run = (await http(api('/api/state'))).data.run;
  assert.equal(run.status, 'stopping');
  assert.equal(run.activity[0].who, '利用者');
  assert.equal(run.activity[0].text, 'Web管理画面で「止める」を押しました');
  const refused = await fake.call('plan', { iteration: 1, plan: 'p' });
  assert.equal(refused.ok, false);
  assert.match(refused.text, /^利用者が Web管理画面で止めました。/);

  fake.emit('session.idle', { aborted: true });
  run = (await http(api('/api/state'))).data.run;
  assert.equal(run.status, 'finished');
  assert.equal(run.finish.outcome, 'stopped');
  assert.equal(run.finish.conclusion, '利用者が Web管理画面で止めました。\n試行 1 の前の、準備の確認の途中で止めました。');
  assert.equal((await http(api('/api/stop'), { method: 'POST', body: {} })).status, 409);
  assert.equal(app.store.snapshot().runs[0].outcome, 'stopped');
});

test('止める：統括エージェントが終わりを記録すれば、その結論で「止めた」として終わる', async (t) => {
  const { fake, app } = await startRun(t);
  fake.emit('assistant.turn_start', {});
  await app.control.stop();
  const r = await fake.call('finish', { outcome: 'success', conclusion: '途中まで直した', retro: 'なし' });
  assert.ok(r.ok);
  const run = app.store.snapshot().run;
  assert.equal(run.status, 'finished');
  assert.equal(run.finish.outcome, 'stopped', '止めたときは outcome を stopped にそろえる');
  assert.equal(run.finish.conclusion, '途中まで直した');
});

test('止める：何も動いていなければ、その場で「止めた」で終わる', async (t) => {
  const { app, fake } = await startRun(t);
  fake.emit('session.idle', {});
  await app.control.stop();
  assert.equal(app.store.snapshot().run.status, 'finished');
  assert.equal(app.store.snapshot().run.finish.outcome, 'stopped');
});

test('止める：止める合図が失敗しても、画面には止めている途中と出し、失敗を記録に残す', async (t) => {
  const { app, fake } = await startRun(t);
  fake.session.abort = async () => {
    throw new Error('つながっていません');
  };
  fake.emit('assistant.turn_start', {});
  await app.control.stop();
  const run = app.store.snapshot().run;
  assert.equal(run.status, 'stopping');
  assert.match(run.activity[0].text, /止める合図を送れませんでした：つながっていません/);
});

test('過去の実行：番号で読む。いまの実行も、起動し直した後のファイルも読める', async (t) => {
  const dataDir = path.join(tempDir(t), 'data');
  const a = await startRun(t, { dataDir });
  await a.fake.call('plan', { iteration: 1, plan: '計画 1' });
  await a.fake.call('finish', { outcome: 'failure', conclusion: '直らなかった', retro: 'なし' });
  const id = a.app.store.run.id;
  await a.app.shutdown();

  const b = await opened(t, { dataDir });
  const list = (await http(b.api('/api/state'))).data.runs;
  assert.deepEqual(list.map((r) => [r.id, r.outcome]), [[id, 'failure']]);
  const past = await http(b.api(`/api/runs/${id}`));
  assert.equal(past.status, 200);
  assert.equal(past.data.iterations[0].plan.text, '計画 1');
  assert.equal(past.data.finish.conclusion, '直らなかった');
  assert.equal((await http(b.api('/api/runs/R-20000101-000000'))).status, 404);
  assert.equal((await http(b.api('/api/runs/..%2Fsettings'))).status, 404);
});

test('開始の失敗（知らせを送れない）は 500 で、画面に出す文を返す', async (t) => {
  const { api } = await opened(t, { fake: { sendFails: true } });
  const r = await http(api('/api/start'), { method: 'POST', body: { criteria: ['c'] } });
  assert.equal(r.status, 500);
  assert.match(r.data.error, /開始の知らせを送れませんでした/);
  const state = (await http(api('/api/state'))).data;
  assert.equal(state.run, null);
  assert.ok(state.draft);
});

test('CSP：スクリプトは、このサーバーのファイルだけを動かす（画面のファイルに直接書いたものは動かさない）', async (t) => {
  const { api } = await opened(t);
  for (const p of ['/', '/app.js', '/api/state']) {
    const policy = (await http(api(p))).headers.get('content-security-policy');
    assert.match(policy, /script-src 'self';/, p);
    assert.ok(!/script-src[^;]*unsafe-inline/.test(policy), p);
  }
});

test('同梱の画面のファイル（index.html）は、直接書いたスクリプトを持たない（CSP で止まるため）', () => {
  const html = fs.readFileSync(path.join(EXT_DIR, 'ui', 'static', 'index.html'), 'utf8');
  for (const m of html.matchAll(/<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
    assert.match(m[1] ?? '', /\ssrc\s*=/i, 'index.html に、src のない <script> があります');
    assert.equal(m[2].trim(), '');
  }
});

test('設定は作業フォルダごとに保存し、ほかの作業フォルダには持ち越さない', async (t) => {
  const dataDir = path.join(tempDir(t), 'data');
  const file = path.join(dataDir, 'settings.json');
  const agents = agentsWith('only-a:helper');
  const a = await opened(t, { dataDir, fake: { cwd: '/w/a', agents } });
  await http(a.api('/api/settings'), { method: 'POST', body: { worker: 'only-a:helper', verifier: VERIFIER, maxIterations: 4 } });
  assert.equal(a.app.store.snapshot().folder, A);

  const b = await opened(t, { dataDir, fake: { cwd: '/w/b', agents } });
  let state = (await http(b.api('/api/state'))).data;
  assert.equal(state.folder, B);
  assert.equal(state.settings.worker, WORKER, 'ほかのフォルダでは、既定（defaults.json）のまま');
  assert.equal(state.settings.maxIterations, 3);
  const r = await b.fake.call('open', { request: '別の作業' });
  assert.ok(r.text.includes(`作業エージェント「${WORKER}」`), 'open も、その作業フォルダの設定を伝える');
  await http(b.api('/api/settings'), { method: 'POST', body: { worker: WORKER, verifier: VERIFIER, maxIterations: 6 } });

  const c = await opened(t, { dataDir, fake: { cwd: '/w/a', agents } });
  state = (await http(c.api('/api/state'))).data;
  assert.equal(state.settings.worker, WORKER, '使うエージェントは保存しない');
  assert.equal(state.settings.maxIterations, 4);
  const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.deepEqual(Object.keys(saved.folders).sort(), [A, B].sort());
  assert.equal(saved.folders[B].maxIterations, 6);
});

test('設定：Windows では、作業フォルダの大文字と小文字を区別しない。ほかの OS では区別する', (t) => {
  const win = createPersist(tempDir(t), { platform: 'win32' });
  assert.equal(win.writeSettings('C:\\Work\\app', { worker: 'a' }), true);
  assert.deepEqual(win.readSettings('c:\\work\\APP'), { worker: 'a' });
  win.writeSettings('c:\\work\\app', { worker: 'b' });
  win.writeSettings('C:\\Other', { worker: 'c' });
  const saved = JSON.parse(fs.readFileSync(path.join(win.baseDir, 'settings.json'), 'utf8'));
  assert.deepEqual(saved, { folders: { 'c:\\work\\app': { worker: 'b' }, 'C:\\Other': { worker: 'c' } } }, '同じフォルダの前の書き方は残さない');

  const linux = createPersist(tempDir(t), { platform: 'linux' });
  linux.writeSettings('/w/App', { worker: 'a' });
  assert.equal(linux.readSettings('/w/app'), null);
  linux.writeSettings('/w/app', { worker: 'b' });
  assert.deepEqual(linux.readSettings('/w/App'), { worker: 'a' });
});

test('設定：フォルダで分けていない古い設定のファイルは読まず、既定を使う', async (t) => {
  const dataDir = path.join(tempDir(t), 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(path.join(dataDir, 'settings.json'), JSON.stringify({ worker: 'leaked:worker', verifier: VERIFIER, maxIterations: 9 }));
  const { app, api } = await opened(t, { dataDir });
  const state = (await http(api('/api/state'))).data;
  assert.equal(state.settings.worker, WORKER);
  assert.equal(app.store.settings.maxIterations, 3);
});

test('設定：セッションの作業フォルダを読む RPC がなければ、このプロセスの今のフォルダを使う', async (t) => {
  const { app, dataDir } = await startWith(t, { fake: { missing: ['metadata'] } });
  await app.control.saveSettings({ worker: 'here:helper', verifier: VERIFIER, maxIterations: 2 });
  assert.equal(app.store.folder, process.cwd());
  const saved = JSON.parse(fs.readFileSync(path.join(dataDir, 'settings.json'), 'utf8'));
  assert.equal(saved.folders[process.cwd()].maxIterations, 2);
  assert.equal(saved.folders[process.cwd()].worker, undefined, '使うエージェントの名前は保存しない');
});

test('作業・検証の選択肢から、統括エージェント（defaults.json の名前）と、チャットで選ばれているエージェントを除く', async (t) => {
  const agents = [
    { name: 'helper-a', id: WORKER, description: '手伝う' },
    { name: 'lead-c', id: 'demo-plugin:lead-c', description: '統括' },
    { name: 'boss', id: 'team:boss', description: 'チャットで選ばれている' },
    { name: 'checker-b', id: VERIFIER, description: '確かめる' },
  ];
  const { api } = await opened(t, { fake: { agents, current: { name: 'boss', id: 'team:boss' } } });
  const names = (await http(api('/api/state'))).data.options.agents.map((a) => a.name);
  assert.deepEqual(names, [WORKER, VERIFIER]);
});

test('選択肢：チャットで選ばれているのが、いま選んでいる作業エージェントなら除かない。読めなければ統括エージェントだけを除く', async (t) => {
  const agents = [
    { name: 'helper-a', id: WORKER, description: '手伝う' },
    { name: 'lead-c', id: 'demo-plugin:lead-c', description: '統括' },
    { name: 'boss', id: 'team:boss', description: 'ほかのエージェント' },
    { name: 'checker-b', id: VERIFIER, description: '確かめる' },
  ];
  const names = async (fake) => {
    const { api } = await opened(t, { fake: { agents, ...fake } });
    return (await http(api('/api/state'))).data.options.agents.map((x) => x.name);
  };
  assert.deepEqual(await names({ current: { name: 'helper-a', id: WORKER } }), [WORKER, 'team:boss', VERIFIER], 'いまの作業エージェントは残す');
  assert.deepEqual(await names({ current: new Error('読めない') }), [WORKER, 'team:boss', VERIFIER]);
  assert.deepEqual(await names({ missing: ['agent.getCurrent'] }), [WORKER, 'team:boss', VERIFIER], 'この RPC がない版');
  assert.deepEqual(await names({ current: null }), [WORKER, 'team:boss', VERIFIER], '既定のエージェントで話しているとき');
});

test('設定：作業フォルダが変わったら（/cd）、そのフォルダの設定を読み直す', async (t) => {
  const dataDir = path.join(tempDir(t), 'data');
  const a = await opened(t, { dataDir, fake: { cwd: '/w/a' } });
  await a.app.control.saveSettings({ worker: 'only-a:helper', verifier: VERIFIER, maxIterations: 4 });
  a.app.store.useFolder('/w/b');
  assert.equal(a.app.store.settings.worker, WORKER);
  a.app.store.useFolder('/w/a/');
  assert.equal(a.app.store.folder, A, '末尾の / は同じフォルダとみなす');
  assert.equal(a.app.store.settings.maxIterations, 4);
  assert.equal(a.app.store.settings.worker, WORKER, '使うエージェントの名前は保存しない');
});
