// 記録のツール：開く → 準備 → 開始 → 計画 → 評価 → 終わり と、Issue。断るときの文も確かめる。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { VERIFIER, WORKER, startRun, startWith } from './helpers.mjs';

test('ツールは6つで、ツール検索の裏に回さず、確認なしで動く', async (t) => {
  const { fake } = await startWith(t);
  const names = fake.config.tools.map((x) => x.name).sort();
  assert.deepEqual(names, ['open', 'prepare', 'plan', 'study', 'finish', 'issue'].map((n) => `embedded-debug-loop_${n}`).sort());
  for (const tool of fake.config.tools) {
    assert.equal(tool.defer, 'never');
    assert.equal(tool.skipPermission, true);
    assert.equal(typeof tool.description, 'string');
    assert.equal(tool.parameters.type, 'object');
  }
});

test('open：URL と今の設定を返し、次に準備の確認を頼むよう伝える', async (t) => {
  const { fake, app } = await startWith(t);
  const r = await fake.call('open', { request: '表示を直す', criteria: ['完了と出る'] });
  assert.ok(r.ok);
  assert.match(r.text, /URL：http:\/\/127\.0\.0\.1:\d+\/\?k=[\w-]{24}/);
  assert.ok(r.text.includes(`作業エージェント：${WORKER}`));
  assert.ok(r.text.includes(`検証エージェント：${VERIFIER}`));
  assert.ok(r.text.includes('繰り返しの上限：3 回'));
  assert.ok(r.text.includes('モデル：既定'));
  assert.ok(r.text.includes(`次に、作業エージェント「${WORKER}」に、準備の確認だけを頼んでください（作業はさせない）。返事を embedded-debug-loop_prepare で記録してください。`));
  const s = app.store.snapshot();
  assert.deepEqual(s.draft, { request: '表示を直す', criteria: ['完了と出る'], prepared: null });
});

test('open：頼む作業が空なら断る。実行中なら URL を添えて断る', async (t) => {
  const { fake } = await startRun(t);
  const empty = await fake.call('open', { request: '  ' });
  assert.equal(empty.ok, false);
  const busy = await fake.call('open', { request: '別の作業' });
  assert.equal(busy.ok, false);
  assert.match(busy.text, /^実行中です。.*URL：http:\/\/127\.0\.0\.1/);
});

test('open：作業エージェントが決まっていなければ、画面で選んでもらうよう伝える', async (t) => {
  const { fake } = await startWith(t, { defaults: { worker: '' } });
  const r = await fake.call('open', { request: '作業' });
  assert.equal(r.ok, true);
  assert.match(r.text, /作業エージェントが決まっていません/);
  assert.ok(!r.text.includes('「」'));
});

test('prepare：返事を下書きに残し、開始を押してもらうよう伝える', async (t) => {
  const { fake, app } = await startWith(t);
  assert.equal((await fake.call('prepare', { worker: WORKER, ready: true, report: 'x' })).ok, false, 'open の前は断る');
  await fake.call('open', { request: '作業' });
  const ok = await fake.call('prepare', { worker: WORKER, ready: true, report: '準備：できている\n設定の要約' });
  assert.ok(ok.ok);
  assert.match(ok.text, /『開始』を押すよう頼んで、手番を終えてください。開始が押されると、このチャットに開始の知らせが届きます。/);
  assert.ok(!ok.text.includes('準備ができていない'));
  const p = app.store.snapshot().draft.prepared;
  assert.equal(p.worker, WORKER);
  assert.equal(p.ready, true);
  assert.equal(p.report, '準備：できている\n設定の要約');
  assert.ok(p.at);
  const ng = await fake.call('prepare', { worker: WORKER, ready: false, report: '準備：できていない\n足りないもの' });
  assert.match(ng.text, /準備ができていないことと、足りないものを利用者に伝えてください。/);
});

test('開始：チャットに開始の知らせを送る（準備ずみなら、確認の頼みは付けない）', async (t) => {
  const { fake, app } = await startRun(t);
  assert.equal(fake.sent.length, 1);
  const lines = fake.sent[0].split('\n');
  const run = app.store.snapshot().run;
  assert.deepEqual(lines, [
    '［Web管理画面からの開始の知らせ］',
    `実行の番号：${run.id}`,
    '頼まれた作業：表示の不具合を直す',
    '合格の条件：',
    '- C1：画面に「完了」と出る',
    '- C2：テストがすべて通る',
    '繰り返しの上限：3 回',
    `作業エージェント：${WORKER}`,
    `検証エージェント：${VERIFIER}`,
    '統括エージェントの手順に沿って、試行 1 の計画から始めてください。',
  ]);
  assert.match(run.id, /^R-\d{8}-\d{6}$/);
  assert.equal(run.status, 'running');
  assert.equal(run.prepared.ready, true);
  assert.equal(app.store.snapshot().draft, null);
});

test('開始：準備していない・作業エージェントを変えたときは、準備の確認を頼む行を付ける', async (t) => {
  const a = await startRun(t, { prepare: false });
  assert.ok(a.fake.sent[0].includes('作業エージェントの準備の確認がまだです。最初に準備の確認を頼んでから、試行を始めてください。'));

  const b = await startWith(t);
  await b.fake.call('open', { request: '作業', criteria: ['条件'] });
  await b.fake.call('prepare', { worker: WORKER, ready: true, report: '準備：できている' });
  await b.app.control.start({ worker: 'other-plugin:another' });
  assert.ok(b.fake.sent[0].includes('作業エージェント：other-plugin:another'));
  assert.ok(b.fake.sent[0].includes('作業エージェントの準備の確認がまだです。'));
});

test('開始：前の準備の確認が「できていない」なら、頼み直す行を付ける', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  await fake.call('prepare', { worker: WORKER, ready: false, report: '準備：できていない' });
  await app.control.start({});
  assert.ok(fake.sent[0].includes('前の準備の確認では「できていない」でした。最初に準備の確認を頼み直してから、試行を始めてください。'));
  assert.ok(!fake.sent[0].includes('準備の確認がまだ'));
});

test('開始：プラグインの名前の付け方が違っても、同じ作業エージェントなら準備ずみとみなす', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  await fake.call('prepare', { worker: 'helper-a', ready: true, report: '準備：できている' });
  await app.control.start({});
  assert.ok(!fake.sent[0].includes('準備の確認がまだ'));
});

test('開始：入力の誤りは 400 で、画面に出す日本語の理由を返す', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業' });
  const cases = [
    [{}, /合格の条件を1つ以上/],
    [{ criteria: ['c'], request: ' ' }, /頼む作業を書いてください/],
    [{ criteria: ['c'], maxIterations: 0 }, /1 から 20 までの整数/],
    [{ criteria: ['c'], maxIterations: 21 }, /1 から 20 までの整数/],
    [{ criteria: ['c'], maxIterations: 2.5 }, /1 から 20 までの整数/],
    [{ criteria: ['c'], worker: '' }, /作業エージェントを選んでください/],
    [{ criteria: ['c'], workerEffort: 'max' }, /考える深さ/],
  ];
  for (const [body, re] of cases) {
    await assert.rejects(app.control.start(body), (e) => e.status === 400 && re.test(e.message), JSON.stringify(body));
  }
  assert.equal(fake.sent.length, 0);
  assert.equal(app.store.run, null);
});

test('開始：モデルと考える深さを、子のエージェントの設定に書く（既定なら書かない・消す）', async (t) => {
  const keep = { 'someone-else': { model: 'm' } };
  const { fake, app } = await startWith(t, { fake: { subagents: { maxDepth: 2, agents: { ...keep } } } });
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  await app.control.start({ workerModel: 'model-x', workerEffort: 'high', verifierEffort: 'low' });
  assert.equal(fake.updates.length, 1);
  assert.deepEqual(fake.updates[0], {
    maxDepth: 2,
    agents: { ...keep, [WORKER]: { model: 'model-x', effortLevel: 'high' }, [VERIFIER]: { effortLevel: 'low' } },
  });
  await fake.call('finish', { outcome: 'success', conclusion: '済み', retro: 'なし' });
  await app.control.start({ request: '次', criteria: ['c'], workerModel: '', workerEffort: '既定', verifierEffort: '' });
  assert.equal(fake.updates.length, 2);
  assert.deepEqual(fake.updates[1], { maxDepth: 2, agents: { ...keep } });
  await fake.call('finish', { outcome: 'success', conclusion: '済み', retro: 'なし' });
  await app.control.start({ request: '次の次', criteria: ['c'] });
  assert.equal(fake.updates.length, 2, '変わらないときは書かない');
});

test('開始：知らせを送れなければ 500 を返し、下書きに戻す', async (t) => {
  const { fake, app, dataDir } = await startWith(t, { fake: { sendFails: true } });
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  await assert.rejects(app.control.start({}), (e) => e.status === 500 && /開始の知らせを送れませんでした/.test(e.message));
  const s = app.store.snapshot();
  assert.equal(s.run, null);
  assert.equal(s.draft.request, '作業');
  assert.deepEqual(s.runs, []);
  const runsDir = path.join(dataDir, 'runs');
  assert.deepEqual(fs.existsSync(runsDir) ? fs.readdirSync(runsDir) : [], []);
});

test('計画：実行中だけ。番号は次の番号か、作業の前なら同じ番号で置き換え。上限を超えると断る', async (t) => {
  const before = await startWith(t);
  await before.fake.call('open', { request: '作業' });
  const notYet = await before.fake.call('plan', { iteration: 1, plan: 'p' });
  assert.equal(notYet.ok, false);
  assert.match(notYet.text, /実行中ではありません/);

  const { fake, app } = await startRun(t);
  assert.equal((await fake.call('plan', { iteration: 2, plan: 'p' })).ok, false);
  assert.equal((await fake.call('plan', { iteration: 1, plan: '' })).ok, false);
  const ok = await fake.call('plan', { iteration: 1, plan: '最初の計画' });
  assert.equal(ok.text, `記録しました。作業エージェント「${WORKER}」に task で頼んでください。`);
  assert.ok((await fake.call('plan', { iteration: 1, plan: '直した計画' })).ok, '作業の前なら置き換えられる');
  let run = app.store.snapshot().run;
  assert.equal(run.iterations.length, 1);
  assert.equal(run.iterations[0].plan.text, '直した計画');
  fake.subagentStop({ agentType: WORKER, agentId: 'a1', response: '報告' });
  const again = await fake.call('plan', { iteration: 1, plan: 'もう一度' });
  assert.equal(again.ok, false, '作業の後は、同じ番号で置き換えられない');
  assert.match(again.text, /次の試行は 2 です/);
  await fake.call('plan', { iteration: 2, plan: '2回目' });
  await fake.call('plan', { iteration: 3, plan: '3回目' });
  const over = await fake.call('plan', { iteration: 4, plan: '4回目' });
  assert.equal(over.text, '上限（3 回）に達しています。embedded-debug-loop_finish で終えてください。');
  run = app.store.snapshot().run;
  assert.equal(run.iteration, 3);
});

test('評価：合否を記録し、次にすることを返す', async (t) => {
  const { fake, app } = await startRun(t);
  assert.match((await fake.call('study', { iteration: 1, verdicts: [], next: '' })).text, /先に試行の計画/);
  await fake.call('plan', { iteration: 1, plan: 'p1' });
  const bad = await fake.call('study', { iteration: 1, verdicts: [{ criterion: 'C1', result: 'ok', reason: 'r' }], next: 'n' });
  assert.equal(bad.ok, false);
  assert.equal((await fake.call('study', { iteration: 2, verdicts: [{ criterion: 'C1', result: 'pass', reason: 'r' }], next: 'n' })).ok, false);
  const mixed = await fake.call('study', {
    iteration: 1,
    verdicts: [{ criterion: 'C1', result: 'pass', reason: '出た' }, { criterion: 'C2', result: 'fail', reason: '1件落ちた' }],
    next: 'テストの落ちた所を直す',
  });
  assert.match(mixed.text, /次は試行 2 です。.*embedded-debug-loop_plan で記録してください。/);
  const st = app.store.snapshot().run.iterations[0].study;
  assert.deepEqual(st.verdicts.map((v) => v.result), ['pass', 'fail']);
  assert.equal(st.next, 'テストの落ちた所を直す');

  await fake.call('plan', { iteration: 2, plan: 'p2' });
  const pass = await fake.call('study', { iteration: 2, verdicts: [{ criterion: 'C1', result: 'pass', reason: 'r' }, { criterion: 'C2', result: 'pass', reason: 'r' }], next: '' });
  assert.equal(pass.text, 'すべての条件が合格です。embedded-debug-loop_finish（outcome は success）で終えてください。');

  await fake.call('plan', { iteration: 3, plan: 'p3' });
  const last = await fake.call('study', { iteration: 3, verdicts: [{ criterion: 'C1', result: 'unknown', reason: 'r' }], next: '' });
  assert.equal(last.text, '上限（3 回）に達しました。embedded-debug-loop_finish（outcome は failure）で終えてください。');
});

test('終わり：結論を記録して終える。記録のファイルと一覧にも残る', async (t) => {
  const { fake, app, dataDir } = await startRun(t);
  assert.equal((await fake.call('finish', { outcome: 'great', conclusion: 'x', retro: '' })).ok, false);
  assert.equal((await fake.call('finish', { outcome: 'success', conclusion: '', retro: '' })).ok, false);
  const r = await fake.call('finish', { outcome: 'needs-human', conclusion: '人の判断が要る', retro: '頼み方を短く' });
  assert.ok(r.ok);
  assert.match(r.text, /結果の画面：http:\/\/127\.0\.0\.1:\d+\/\?k=.*#result\n利用者に結論を短く伝えてください。/);
  const s = app.store.snapshot();
  assert.equal(s.run.status, 'finished');
  assert.deepEqual({ ...s.run.finish, at: undefined }, { outcome: 'needs-human', conclusion: '人の判断が要る', retro: '頼み方を短く', at: undefined });
  assert.equal(s.runs[0].outcome, 'needs-human');
  const saved = JSON.parse(fs.readFileSync(path.join(dataDir, 'runs', s.run.id, 'run.json'), 'utf8'));
  assert.equal(saved.finish.outcome, 'needs-human');
  const events = fs.readFileSync(path.join(dataDir, 'runs', s.run.id, 'events.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  assert.ok(events.some((e) => e.type === 'start'));
  assert.ok(events.some((e) => e.type === 'finish'));
  assert.equal((await fake.call('finish', { outcome: 'success', conclusion: 'もう一度' })).ok, false, '2回は終えられない');
  assert.equal((await fake.call('plan', { iteration: 1, plan: 'p' })).ok, false);
});

test('Issue：gh が URL を出したときだけ「作りました」と返す', async (t) => {
  const calls = [];
  let reply = { code: 0, stdout: 'https://github.com/owner/repo/issues/12\n', stderr: '' };
  const { fake } = await startWith(t, { runCommand: async (cmd, args, timeout) => (calls.push({ cmd, args, timeout }), reply) });
  const ok = await fake.call('issue', { title: '題', body: '本文' });
  assert.ok(ok.ok);
  assert.match(ok.text, /Issue を作りました：https:\/\/github\.com\/owner\/repo\/issues\/12/);
  assert.deepEqual(calls[0], { cmd: 'gh', args: ['issue', 'create', '--repo', 'owner/repo', '--title', '題', '--body', '本文'], timeout: 30000 });

  reply = { code: 0, stdout: '', stderr: '', missing: true };
  const missing = await fake.call('issue', { title: '題', body: '本文' });
  assert.equal(missing.ok, false);
  assert.match(missing.text, /gh（GitHub のコマンド）が見つからない/);
  assert.match(missing.text, /https:\/\/github\.com\/owner\/repo\/issues\/new/);
  assert.match(missing.text, /題：題\n本文：\n本文/);

  reply = { code: 1, stdout: '', stderr: 'HTTP 401: Bad credentials' };
  const denied = await fake.call('issue', { title: '題', body: '本文' });
  assert.equal(denied.ok, false);
  assert.match(denied.text, /Bad credentials/);
  assert.equal((await fake.call('issue', { title: '', body: '本文' })).ok, false);
});

test('ツールの処理が思わず失敗しても、例外を投げずに失敗の結果を返す', async (t) => {
  const { fake } = await startWith(t, { runCommand: async () => { throw new Error('壊れた'); } });
  const r = await fake.call('issue', { title: '題', body: '本文' });
  assert.equal(r.ok, false);
  assert.match(r.text, /思わぬ失敗/);
});
