// REQ-404, REQ-405, REQ-411, REQ-412, REQ-414, REQ-421, REQ-422, REQ-423: the screen (app.js) drawn in a fake DOM
// from states made by the real core (fake SDK and runner).
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadScreen, textOf } from './fakedom.mjs';
import { sdkEvent } from './fake-sdk.mjs';
import { finishedStatus, item, openAndStart, startWith, statusOf } from './helpers.mjs';

async function screen(state) {
  const box = { state, posts: [] };
  const s = await loadScreen(async (method, path, body) => {
    if (method === 'GET' && path === '/api/state') return { data: box.state };
    box.posts.push({ method, path, body });
    if (box.fail) return { status: 400, data: { error: box.fail } };
    return { data: { ok: true } };
  });
  return { ...s, box };
}

async function setupState(t, request = 'misra.yaml で Coverity の警告をトリアージして。High だけ、NULL_RETURNS だけ', configure) {
  const h = await startWith(t);
  configure?.(h);
  await h.open(request);
  await h.idle();
  return { h, state: h.state() };
}

async function runningState(t) {
  const h = await startWith(t);
  h.runner.on('run:status', () =>
    statusOf({
      items: [
        item('20001'),
        item('20002', { status: 'in_progress', phase: 'judge', stalled: true, review: null }),
        item('20003', { status: 'in_progress', phase: 'fix', preparing_copy: true, review: null }),
        item('G1', { cids: [20004, 20005], status: 'pending', phase: null, review: null }),
      ],
      errors: [{ item: '20009', kind: '取得', detail: '経路を取れませんでした', level: 'critical' }],
      recent: ['10:06 a', '10:05 b', '10:04 c', '10:03 d', '10:02 e', '10:01 f'],
    }),
  );
  const root = await openAndStart(h);
  await h.clock.advance(1);
  h.fake.emit(sdkEvent.toolStart(root, 'tc-1', 'task', { description: '20002 の作業 <img src=x onerror=alert(1)>' }));
  h.fake.emit(sdkEvent.subagentStarted('w1', { parentId: root, agentType: h.defaults.roles.worker, toolCallId: 'tc-1', model: 'claude-haiku-5.5' }));
  h.fake.emit(sdkEvent.permissionRequested('w1', 'p1', { kind: 'shell', fullCommandText: 'uv run tick' }));
  await h.idle();
  return { h, state: h.state() };
}

async function finishedState(t) {
  const h = await startWith(t);
  h.runner.on('run:status', () => statusOf({ recent: [] }));
  const root = await openAndStart(h);
  await h.clock.advance(1);
  h.runner.on('run:status', () =>
    finishedStatus({
      items: [item('20001', { confidence: 'medium' }), item('G1', { cids: [20004, 20005], confidence: 'low', review: { outcome: 'flag', findings: [{ check: 'C-12', ok: false, detail: '行が違う', file: 'src/a.c', line: 3 }] } })],
      criteria_results: [{ id: 'C-12', scope: 'item', passed: 1, failed: 1, not_applicable: 0, items_failed: ['G1'] }],
      issues_found: [{ title: '統括の課題', body: '本文 <script>x</script>', at: 'x' }],
    }),
  );
  h.fake.emit(sdkEvent.agentIdle(root));
  await h.idle();
  return { h, state: h.state() };
}

test('the token is read once and removed from the address bar; requests keep it in memory', async (t) => {
  const { state } = await setupState(t);
  const { history, requests } = await screen(state);
  assert.deepEqual(history[0], [null, '', '/']);
  assert.equal(requests[0].query.k, 'test-token');
  assert.equal(requests[0].query.visible, '1');
});

test('REQ-405 the setup shows the request text, what it filled, and the phrases it does not use', async (t) => {
  const { state } = await setupState(t);
  const { $ } = await screen(state);
  assert.equal(textOf($('request')).includes('misra.yaml で Coverity の警告をトリアージして。High だけ、NULL_RETURNS だけ'), true);
  assert.equal(textOf($('request-unused')), '頼みの文の「NULL_RETURNS だけ」は使いません。条件は画面で選んでください。');
  assert.match(textOf($('request-used')), /「misra.yaml」→ 条件ファイル/);
  assert.equal($('filter').value, 'misra.yaml');
  assert.equal($('impact').querySelector('option[selected]').value, 'High');
  assert.equal($('limit').value, '');
  assert.equal($('initial').querySelector('option[selected]').value, '設定どおり');
});

test('REQ-404 models (defaults from the definitions), efforts "変えない", rework 1, conditions; start posts them', async (t) => {
  const { state } = await setupState(t, 'Coverity の警告をトリアージして');
  const { $, box, settle } = await screen(state);
  assert.equal($('model-orchestrator').querySelector('option[selected]').value, 'claude-sonnet-5.5');
  assert.equal($('model-worker').querySelector('option[selected]').value, 'gpt-6-luna');
  assert.equal($('model-reviewer').querySelector('option[selected]').value, 'claude-sonnet-5.5');
  assert.equal($('model-integrator').querySelector('option[selected]').value, 'gpt-6-luna');
  for (const role of ['orchestrator', 'worker', 'reviewer', 'integrator']) assert.equal(textOf($(`effort-${role}`).querySelector('option[selected]')), '変えない');
  assert.equal($('maxRework').value, '1');
  assert.match(textOf($('override-notice')), /指定も消えます/);
  assert.match(textOf($('permission-hint')), /\/add-dir/);
  const worker = $('model-worker');
  worker.value = 'claude-haiku-5.5';
  await worker.dispatch('change');
  const limit = $('limit');
  limit.value = '15';
  await limit.dispatch('input');
  await $('start').dispatch('click');
  await settle();
  const [post] = box.posts;
  assert.equal(post.path, '/api/start');
  assert.deepEqual(post.body, {
    models: { orchestrator: 'claude-sonnet-5.5', worker: 'claude-haiku-5.5', reviewer: 'claude-sonnet-5.5', integrator: 'gpt-6-luna' },
    efforts: { orchestrator: '', worker: '', reviewer: '', integrator: '' },
    maxRework: 1,
    conditions: { filter: 'untriaged.yaml', impact: '絞らない', limit: 15, initial: '設定どおり' },
  });
});

test('REQ-404 the screen checks values before posting; a refusal from the server is shown', async (t) => {
  const { state } = await setupState(t, 'x');
  const { $, box, settle } = await screen(state);
  const limit = $('limit');
  limit.value = '500';
  await limit.dispatch('input');
  await $('start').dispatch('click');
  await settle();
  assert.equal(box.posts.length, 0);
  assert.match(textOf($('start-message')), /1 から 100 までの整数/);
  const again = $('limit'); // the form was drawn again
  again.value = '';
  await again.dispatch('input');
  box.fail = '統括エージェントを起動できませんでした：x。もう一度開始できます。';
  await $('start').dispatch('click');
  await settle();
  assert.equal(box.posts.length, 1);
  assert.match(textOf($('start-message')), /統括エージェントを起動できませんでした/);
  assert.equal($('start').disabled, false);
});

test('REQ-406 settings that cannot be read, and a failed start, show how to prepare', async (t) => {
  const { h, state } = await setupState(t, 'x', (hh) => hh.runner.on('run:settings', { ok: false, error: '設定ファイルがありません', problems: ['config.yaml がない'] }));
  let s = await screen(state);
  assert.match(textOf(s.$('settings-error')), new RegExp(`設定ファイルがありません.*config.yaml がない.*/${h.defaults.setupSkill}`));
  h.runner.on('run:new-run', { ok: false, error: 'url がない', problems: ['coverity.url を書いてください'] });
  await assert.rejects(h.core.start({}));
  await h.idle();
  s = await screen(h.state());
  assert.match(textOf(s.$('start-message')), new RegExp(`実行フォルダを作れませんでした：url がない.*coverity.url を書いてください.*/${h.defaults.setupSkill}`));
});

test('REQ-404 while the settings load, start cannot be pressed', async (t) => {
  const { state } = await setupState(t, 'x', (hh) => hh.runner.on('run:settings', () => new Promise(() => {})));
  const { $ } = await screen(state);
  assert.equal($('start').disabled, true);
  assert.equal(textOf($('settings-loading')), '設定を読み込んでいます…');
});

test('REQ-411 REQ-412 REQ-414 running: phase, stage, slots, waits, progress tables, recent (5), clear notice, stop', async (t) => {
  const { state } = await runningState(t);
  const { $, doc, box, settle } = await screen(state);
  assert.equal(textOf($('run-status')), '実行中');
  assert.match(textOf($('clear-notice')), /\/clear/);
  assert.equal(doc.querySelectorAll('li[data-active="true"]').filter((li) => li.dataset.phase).map((li) => li.dataset.phase).join(), 'do');
  assert.match(textOf($('stage')), /今の段：\s?警告ごとの作業/);
  const worker = doc.querySelector('article[data-slot="worker-1"]');
  assert.match(textOf(worker), /待ち（チャットで確認を待っています）/);
  assert.match(textOf(worker), /20002 の作業 <img src=x onerror=alert\(1\)>/);
  assert.equal(worker.querySelectorAll('img').length, 0, 'agent text stays text');
  assert.match(textOf(worker), /選んだモデルと違います/);
  assert.match(textOf($('s-waits')), /チャットで確認を待っています.*作業エージェント（作業 1） の確認：\s?許可.*コマンドの全文 uv run tick/);
  const rows = $('items').querySelectorAll('tr').map(textOf);
  assert.equal(rows[0], '作業 段 経過時間 作り直し 状態');
  assert.match(rows[2], /^20002 判定 5 分 12 秒 0 ⚠ 停止\?$/);
  assert.match(rows[3], /^20003 修正 .* 修正用のコピーを用意中$/);
  assert.match(rows[4], /^G1（2 件） — — 0 待ち$/);
  assert.match(textOf($('errors')), /20009 重大 取得 経路を取れませんでした/);
  assert.equal($('recent').querySelectorAll('li').length, 5);
  assert.match(textOf($('overall-progress')), /1 \/ 3（33%）/);
  assert.match(textOf($('initial-progress')), /初回解析はしない/);
  assert.match(textOf($('s-criteria')), /C-01.*合格 1.*不合格 0/);
  await $('stop').dispatch('click');
  await settle();
  assert.deepEqual(box.posts.map((p) => p.path), ['/api/stop']);
});

test('REQ-421 REQ-422 finished: end reason, counts, list (low confidence first), links, apply guide, criteria, issues', async (t) => {
  const { h, state } = await finishedState(t);
  const { $, doc, box, settle } = await screen(state);
  assert.equal(textOf($('run-status')), '終了（上限の回数に達した）');
  assert.match(textOf($('end-reason')), /終わった理由：\s?上限の回数に達した（要確認 1 件）/);
  assert.match(textOf($('s-end')), /2 件の作業を終えました。/);
  assert.match(textOf($('counts')), /警告 3 作業 2 修正の推奨 1 逸脱の推奨 1 確信度 低 1 エラー 0 未処理 0 承認 修正 1 承認 逸脱 1 承認 却下 0 承認 （空） 0 推奨から変えたもの 0/);
  const rows = $('results').querySelectorAll('tr').map(textOf);
  assert.equal(rows[0], '承認 ID 推奨 確信度 見立て 詳細');
  assert.match(rows[1], /^逸脱 G1（2 件） 逸脱 低 /);
  assert.match(rows[2], /^修正 20001 修正 中 /);
  assert.match($('link-item').getAttribute('href'), /#run=.*&view=summary&item=G1$/);
  assert.match($('link-approvals').getAttribute('href'), /view=summary$/);
  assert.equal(textOf($('apply-guide')), `承認の選択と逸脱コメントの手直しは、この画面の「結果」で行い、/${h.defaults.applySkill} で反映する`);
  const c12 = doc.querySelector('li[data-criterion="C-12"]');
  assert.match(textOf(c12), /合格 1 不合格 1.*不合格の警告：\s?G1/);
  assert.match(textOf(doc.querySelector('li[data-criterion="C-25"]')), /合格/);
  assert.match(textOf(doc.querySelector('article[data-item="G1"]')), /要確認.*C-12 不合格 行が違う\s?（src\/a.c:3）/);
  assert.equal(doc.querySelectorAll('script').length, 2, 'only the page\'s own two scripts');
  assert.match(textOf($('s-issues')), /本文 <script>x<\/script>/, 'issue text stays text');
  const issue = doc.querySelector('article[data-issue-id="O1"]');
  assert.match(textOf(issue), /統括エージェントが振り返って足した課題 未起票/);
  await issue.querySelector('button').dispatch('click');
  await settle();
  assert.deepEqual(box.posts.map((p) => p.path), ['/api/issues/O1/file']);
  assert.equal($('stop'), null);
  assert.equal(doc.querySelectorAll('article[data-status="standby"]').length, 0, 'unused slots are not shown after the end');
});

test('REQ-421 a stopped run shows the reason and the last progress, without a list', async (t) => {
  const h = await startWith(t);
  await openAndStart(h);
  await h.clock.advance(1);
  await h.stop();
  await h.idle();
  const { $ } = await screen(h.state());
  assert.equal(textOf($('run-status')), '終了（ユーザーが止めた）');
  assert.match(textOf($('s-end')), /作業 3 件のうち、終わった 1 件、エラー 0 件、未処理 2 件。/);
  assert.equal($('results'), null);
  assert.match(textOf($('counts')), /作業 3/);
});

test('REQ-407 the previous run and its unfiled drafts are on the setup screen', async (t) => {
  const { h } = await finishedState(t);
  await h.open();
  await h.idle();
  const { $, doc } = await screen(h.state());
  assert.match(textOf($('s-previous')), /前の実行の結果.*上限の回数に達した/);
  assert.ok(doc.querySelector('article[data-issue-id="O1"]'));
  assert.ok($('start'));
});

test('the connection lost message appears when the server does not answer', async (t) => {
  const { state } = await setupState(t, 'x');
  const s = await screen(state);
  s.box.state = null;
  const original = s.context.fetch;
  s.context.fetch = async () => {
    throw new Error('down');
  };
  await s.poll();
  assert.match(textOf(s.$('connection-lost')), /サーバーにつながりません/);
  s.context.fetch = original;
});
