// イベントの記録：作業・検証の返事の取り込み、くわしい記録の行、待っていること、図で光らせるエージェント。

import assert from 'node:assert/strict';
import test from 'node:test';

import { VERIFIER, WORKER, startRun, startWith } from './helpers.mjs';

const lines = (app) => app.store.snapshot().run.activity.map((a) => `${a.who}|${a.text}`);

test('作業エージェントの返事は、計画があって作業の報告がまだの試行に入る', async (t) => {
  const { fake, app } = await startRun(t);
  fake.subagentStop({ agentType: WORKER, agentId: 'w0', response: '計画の前の返事' });
  assert.equal(app.store.snapshot().run.iterations.length, 0, '計画の前は記録しない');
  await fake.call('plan', { iteration: 1, plan: 'p1' });
  fake.subagentStop({ agentType: 'helper-a', agentId: 'w1', response: '報告：変えたこと…' });
  fake.subagentStop({ agentType: WORKER, agentId: 'w2', response: '2つ目の返事' });
  const it = app.store.snapshot().run.iterations[0];
  assert.equal(it.do.report, '報告：変えたこと…', '最初の返事を使う（名前の頭のプラグイン名は見ない）');
  assert.ok(it.do.at);
});

test('準備の確認のあいだ（下書き）の作業エージェントの返事は、記録しない', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業' });
  assert.equal(fake.subagentStop({ agentType: WORKER, agentId: 'w', response: '準備：できている' }), undefined);
  const s = app.store.snapshot();
  assert.equal(s.run, null);
  assert.equal(s.draft.prepared, null);
});

test('検証エージェントの返事は、評価の記録の前でも後でも、その試行に入る', async (t) => {
  const { fake, app } = await startRun(t);
  await fake.call('plan', { iteration: 1, plan: 'p1' });
  fake.subagentStop({ agentType: VERIFIER, agentId: 'v1', response: 'C1：合格。理由' });
  assert.equal(app.store.snapshot().run.iterations[0].study, null, '評価の記録まで study は出さない');
  await fake.call('study', { iteration: 1, verdicts: [{ criterion: 'C1', result: 'fail', reason: 'r' }], next: 'n' });
  assert.equal(app.store.snapshot().run.iterations[0].study.verifierReport, 'C1：合格。理由');
  await fake.call('plan', { iteration: 2, plan: 'p2' });
  await fake.call('study', { iteration: 2, verdicts: [{ criterion: 'C1', result: 'fail', reason: 'r' }], next: 'n' });
  fake.subagentStop({ agentType: VERIFIER, agentId: 'v2', response: '後から届いた返事' });
  assert.equal(app.store.snapshot().run.iterations[1].study.verifierReport, '後から届いた返事');
});

test('ツールの呼び出しを、ふつうの言葉の行にする（新しいものが先頭。失敗には印）', async (t) => {
  const { fake, app } = await startRun(t);
  fake.emit('subagent.started', { agentType: WORKER, agentName: 'helper-a', toolCallId: 't0' }, 'w1');
  fake.emit('subagent.started', { agentType: VERIFIER, agentName: 'checker-b', toolCallId: 't9' }, 'v1');
  fake.emit('subagent.started', { agentType: 'general-purpose', agentName: 'general-purpose', toolCallId: 't8' }, 'g1');
  const long = `echo ${'x'.repeat(300)}`;
  const calls = [
    [undefined, 'task', { agent_type: WORKER, description: '試行 1 の作業', prompt: '…' }],
    [undefined, 'task', { agent_type: VERIFIER, description: '試行 1 の評価' }],
    [undefined, 'task', { agent_type: 'general-purpose', description: '調べもの' }],
    [undefined, 'view', { path: '/w/a.c' }],
    [undefined, 'ask_user', { question: 'どうしますか' }],
    [undefined, 'embedded-debug-loop_plan', { iteration: 1 }],
    [undefined, 'embedded-debug-loop_study', {}],
    [undefined, 'embedded-debug-loop_finish', {}],
    ['w1', 'bash', { command: long }],
    ['w1', 'edit', { path: '/w/a.c' }],
    ['w1', 'create', { path: '/w/b.c' }],
    ['w1', 'apply_patch', '*** Begin Patch'],
    ['w1', 'grep', { pattern: 'TODO' }],
    ['w1', 'skill', { skill: 'some-skill' }],
    ['w1', 'report_intent', { intent: 'x' }],
    ['v1', 'glob', { pattern: '**/*.log' }],
    ['g1', 'rg', { pattern: 'main' }],
    ['w2', 'powershell', { command: 'dir' }],
  ];
  calls.forEach(([agentId, toolName, args], i) => fake.emit('tool.execution_start', { toolCallId: `c${i}`, toolName, arguments: args }, agentId));
  fake.emit('tool.execution_complete', { toolCallId: 'c8', success: false, error: { message: 'exit 1' } }, 'w1');
  fake.emit('tool.execution_complete', { toolCallId: 'c9', success: true }, 'w1');
  const got = lines(app).reverse();
  assert.deepEqual(got, [
    '統括|作業エージェントに頼む：試行 1 の作業',
    '統括|検証エージェントに頼む：試行 1 の評価',
    '統括|エージェント「general-purpose」に頼む：調べもの',
    '統括|ファイルを読む：/w/a.c',
    '統括|利用者に質問',
    '統括|記録：計画',
    '統括|記録：評価',
    '統括|記録：終わり',
    `作業|コマンドを実行：${long.slice(0, 160)}…（失敗）`,
    '作業|ファイルを直す：/w/a.c',
    '作業|ファイルを直す：/w/b.c',
    '作業|ファイルを直す：（パッチ）',
    '作業|探す：TODO',
    '作業|スキルを使う：some-skill',
    '検証|探す：**/*.log',
    'その他|探す：main',
    'その他|コマンドを実行：dir',
  ]);
});

test('くわしい記録は 300 行までで、子のエージェントの終わりも1行で残す', async (t) => {
  const { fake, app } = await startRun(t);
  for (let i = 0; i < 320; i += 1) fake.emit('tool.execution_start', { toolCallId: `c${i}`, toolName: 'view', arguments: { path: `/f${i}` } });
  const run = app.store.snapshot().run;
  assert.equal(run.activity.length, 300);
  assert.equal(run.activity[0].text, 'ファイルを読む：/f319');
  fake.emit('subagent.started', { agentType: WORKER, toolCallId: 't' }, 'w1');
  fake.emit('subagent.completed', { agentName: 'helper-a' }, 'w1');
  fake.emit('subagent.started', { agentType: VERIFIER, toolCallId: 't2' }, 'v1');
  fake.emit('subagent.failed', { agentName: 'checker-b', error: 'モデルの上限' }, 'v1');
  const top = lines(app).slice(0, 2);
  assert.deepEqual(top, ['検証|検証エージェントが失敗しました：モデルの上限', '作業|作業エージェントが返事をしました']);
});

test('待っていること：質問と許可を示し、答えが出たら消す。フックが答えた許可は示さない', async (t) => {
  const { fake, app } = await startRun(t);
  fake.emit('user_input.requested', { requestId: 'q1', question: 'どちらにしますか？' });
  assert.deepEqual(app.store.snapshot().run.waiting, { kind: 'question', text: 'どちらにしますか？' });
  fake.emit('user_input.completed', { requestId: 'q1', answer: 'A' });
  assert.equal(app.store.snapshot().run.waiting, null);
  fake.emit('permission.requested', { requestId: 'p0', resolvedByHook: true, permissionRequest: { kind: 'shell', fullCommandText: 'ls' } });
  assert.equal(app.store.snapshot().run.waiting, null);
  fake.emit('permission.requested', { requestId: 'p1', permissionRequest: { kind: 'shell', fullCommandText: 'rm -rf out' } }, 'w1');
  assert.deepEqual(app.store.snapshot().run.waiting, { kind: 'permission', text: 'rm -rf out' });
  fake.emit('permission.completed', { requestId: 'other', result: { kind: 'approved' } });
  assert.ok(app.store.snapshot().run.waiting, '別の求めの答えでは消さない');
  fake.emit('permission.completed', { requestId: 'p1', result: { kind: 'approved' } });
  assert.equal(app.store.snapshot().run.waiting, null);
  fake.emit('permission.requested', { requestId: 'p2', permissionRequest: { kind: 'write', fileName: '/w/a.c' } });
  assert.equal(app.store.snapshot().run.waiting.text, '/w/a.c');
});

test('図で光らせるエージェント：動いている子 → 統括 → なし', async (t) => {
  const { fake, app } = await startRun(t);
  const active = () => app.store.snapshot().run.active;
  fake.emit('assistant.turn_start', { turnId: '1' });
  assert.equal(active().role, 'orchestrator');
  fake.emit('subagent.started', { agentType: WORKER, toolCallId: 't1' }, 'w1');
  assert.equal(active().role, 'worker');
  assert.ok(active().since);
  fake.emit('subagent.completed', {}, 'w1');
  assert.equal(active().role, 'orchestrator');
  fake.emit('subagent.started', { agentType: 'checker-b', agentName: 'checker-b', toolCallId: 't2' }, 'v1');
  assert.equal(active().role, 'verifier');
  fake.emit('session.idle', {}, 'v1'); // 子のエージェントだけの idle は、セッション全体の終わりではない
  assert.equal(active().role, 'verifier');
  fake.emit('session.idle', {});
  assert.equal(active(), null);
});

test('統括エージェントのチャットの文と、エラーも記録に残す', async (t) => {
  const { fake, app } = await startRun(t);
  fake.emit('assistant.message', { messageId: 'm', content: '試行 1 を始めます。' });
  fake.emit('assistant.message', { messageId: 'm2', content: '子の文' }, 'w1');
  fake.emit('session.error', { errorType: 'x', message: '通信が切れました' });
  assert.deepEqual(lines(app).slice(0, 2), ['その他|エラー：通信が切れました', '統括|チャットに書く：試行 1 を始めます。']);
});

test('壊れたイベントでも例外を投げない', async (t) => {
  const { fake } = await startRun(t);
  const broken = [null, 1, 'x', {}, { type: 5 }, { type: 'tool.execution_start' }, { type: 'subagent.completed', data: null },
    { type: 'permission.requested', data: { permissionRequest: null } }, { type: 'tool.execution_start', data: { toolName: 7, arguments: 'x' } }];
  for (const event of broken) assert.doesNotThrow(() => fake.raw(event), JSON.stringify(event));
  assert.equal(fake.subagentStop(null), undefined);
  assert.equal(fake.config.hooks.onSubagentStop(undefined), undefined);
});

test('ask_user の質問（elicitation）：実行中は待っていることを示し、答えが出たら消す', async (t) => {
  const { fake, app } = await startRun(t);
  fake.emit('assistant.turn_start', {});
  fake.emit('elicitation.requested', {
    requestId: 'e1',
    message: '作業エージェントが進めません。\nどうしますか？',
    mode: 'form',
    requestedSchema: { type: 'object', properties: { answer: { type: 'string' } } },
  });
  let s = app.store.snapshot();
  assert.deepEqual(s.waiting, { kind: 'question', text: '作業エージェントが進めません。\nどうしますか？' });
  assert.deepEqual(s.run.waiting, s.waiting);
  fake.emit('elicitation.completed', { requestId: 'other', action: 'accept' });
  assert.ok(app.store.snapshot().waiting, '別の求めの答えでは消さない');
  fake.emit('elicitation.completed', { requestId: 'e1', action: 'accept', content: { answer: '続ける' } });
  s = app.store.snapshot();
  assert.equal(s.waiting, null);
  assert.equal(s.run.waiting, null);
});

test('実行の前（下書き）・実行のないとき・終わった後の質問も、待っていることとして示す', async (t) => {
  const a = await startWith(t);
  a.fake.emit('elicitation.requested', { requestId: 'e0', message: '何をしますか？' });
  assert.deepEqual(a.app.store.snapshot().waiting, { kind: 'question', text: '何をしますか？' }, '下書きもないとき');
  a.fake.emit('elicitation.completed', { requestId: 'e0', action: 'accept' });
  await a.fake.call('open', { request: '作業' });
  a.fake.emit('elicitation.requested', { requestId: 'e1', message: '条件はこれでよいですか？' });
  let s = a.app.store.snapshot();
  assert.equal(s.run, null);
  assert.deepEqual(s.waiting, { kind: 'question', text: '条件はこれでよいですか？' }, '下書きのとき');
  a.fake.emit('elicitation.completed', { requestId: 'e1', action: 'accept' });
  assert.equal(a.app.store.snapshot().waiting, null);

  const b = await startRun(t);
  await b.fake.call('finish', { outcome: 'success', conclusion: 'できた', retro: '頼み方を短く' });
  b.fake.emit('elicitation.requested', { requestId: 'e2', message: '振り返りの改善の案を Issue にしますか？' });
  s = b.app.store.snapshot();
  assert.equal(s.run.status, 'finished');
  assert.deepEqual(s.waiting, { kind: 'question', text: '振り返りの改善の案を Issue にしますか？' }, '終わった後');
  b.fake.emit('elicitation.completed', { requestId: 'e2', action: 'decline' });
  assert.equal(b.app.store.snapshot().waiting, null);
});

test('待っていることが重なったら、新しいものを示し、答えたものだけを消す。セッションが止まれば、すべて消す', async (t) => {
  const { fake, app } = await startRun(t);
  const waiting = () => app.store.snapshot().waiting;
  fake.emit('permission.requested', { requestId: 'p1', permissionRequest: { kind: 'shell', fullCommandText: 'ls out' } }, 'w1');
  fake.emit('elicitation.requested', { requestId: 'e1', message: '続けますか？' });
  assert.deepEqual(waiting(), { kind: 'question', text: '続けますか？' });
  fake.emit('elicitation.completed', { requestId: 'e1', action: 'accept' });
  assert.deepEqual(waiting(), { kind: 'permission', text: 'ls out' }, '残っている許可の求めにもどる');
  fake.emit('user_input.requested', { requestId: 'q1', question: 'どちらにしますか？' });
  fake.emit('session.idle', {});
  assert.equal(waiting(), null);
  assert.equal(app.store.snapshot().run.waiting, null);
});

test('質問の文は、行の区切りを残し、長いものは切る', async (t) => {
  const { fake, app } = await startRun(t);
  fake.emit('elicitation.requested', { requestId: 'e1', message: `  1行目  \n\n  2行目 ${'あ'.repeat(500)}` });
  const text = app.store.snapshot().waiting.text;
  assert.ok(text.startsWith('1行目\n2行目 あ'));
  assert.equal(text.length, 401);
  assert.ok(text.endsWith('…'));
});

test('検証エージェントを呼ばずに評価したときは、その印を残す。呼んだときは付けない', async (t) => {
  const { fake, app } = await startRun(t);
  const study = (n) => app.store.snapshot().run.iterations[n - 1].study;
  const verdicts = [{ criterion: 'C1', result: 'unknown', reason: '作業が進めなかった' }];

  await fake.call('plan', { iteration: 1, plan: 'p1' });
  fake.emit('subagent.started', { agentType: WORKER, toolCallId: 't1' }, 'w1');
  fake.subagentStop({ agentType: WORKER, agentId: 'w1', response: 'ツールが使えず、進めませんでした' });
  fake.emit('subagent.completed', {}, 'w1');
  await fake.call('study', { iteration: 1, verdicts, next: '利用者に聞く' });
  assert.equal(study(1).verifierCalled, false);
  assert.equal(study(1).verifierReport, '');

  await fake.call('plan', { iteration: 2, plan: 'p2' });
  fake.emit('subagent.started', { agentType: VERIFIER, toolCallId: 't2' }, 'v1');
  await fake.call('study', { iteration: 2, verdicts, next: 'n' });
  assert.equal(study(2).verifierCalled, true, '返事の前でも、呼んでいれば印は付けない');

  await fake.call('plan', { iteration: 3, plan: 'p3' });
  await fake.call('study', { iteration: 3, verdicts, next: 'n' });
  assert.equal(study(3).verifierCalled, false);
  fake.subagentStop({ agentType: VERIFIER, agentId: 'v2', response: '後から届いた返事' });
  assert.equal(study(3).verifierCalled, true, '評価の後に呼んだときは、印を外す');
  assert.equal(study(3).verifierReport, '後から届いた返事');
});
