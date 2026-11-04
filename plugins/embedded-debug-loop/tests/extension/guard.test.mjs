// ツールを使う前の問い合わせ（onPreToolUse）：統括エージェントの書き込み・コマンドを拒む、止めたあとは終わりの記録のほかを拒む。

import assert from 'node:assert/strict';
import test from 'node:test';

import { VERIFIER, WORKER, startRun, startWith } from './helpers.mjs';

const WRITE_OR_RUN = ['edit', 'create', 'apply_patch', 'bash', 'write_bash', 'read_bash', 'stop_bash', 'list_bash',
  'powershell', 'write_powershell', 'read_powershell', 'stop_powershell', 'skill'];

test('実行がないとき・下書きのときは、何も拒まない', async (t) => {
  const { fake } = await startWith(t);
  assert.equal(fake.pre({ toolName: 'bash', toolArgs: {} }), undefined);
  await fake.call('open', { request: '作業' });
  assert.equal(fake.pre({ toolName: 'edit', toolArgs: {} }), undefined);
});

test('実行中：統括エージェントの、ファイルを直すツールとコマンドのツールだけを拒む', async (t) => {
  const { fake } = await startRun(t);
  for (const toolName of WRITE_OR_RUN) {
    const r = fake.pre({ toolName, toolArgs: {} });
    assert.equal(r?.permissionDecision, 'deny', toolName);
    assert.equal(r.permissionDecisionReason, '統括エージェントは、ファイルを直したりコマンドを実行したりしません。作業は task で作業エージェントに頼んでください。');
  }
  for (const toolName of ['view', 'grep', 'glob', 'task', 'ask_user', 'embedded-debug-loop_plan', 'read_agent']) {
    assert.equal(fake.pre({ toolName, toolArgs: {} }), undefined, toolName);
  }
});

test('実行中：子のエージェント（agentId あり、または作業・検証の名前）のツールは拒まない', async (t) => {
  const { fake } = await startRun(t);
  assert.equal(fake.pre({ toolName: 'bash', toolArgs: {}, agentId: 'w1', agentType: WORKER }), undefined);
  assert.equal(fake.pre({ toolName: 'edit', toolArgs: {}, agentId: 'x1', agentType: 'general-purpose' }), undefined);
  assert.equal(fake.pre({ toolName: 'bash', toolArgs: {}, agentType: 'helper-a' }), undefined, 'agentId がない古い版でも、名前で見分ける');
  assert.equal(fake.pre({ toolName: 'skill', toolArgs: {}, agentType: VERIFIER }), undefined);
  assert.equal(fake.pre({ toolName: 'bash', toolArgs: {}, sessionId: 'child-1' }), undefined, '子のセッションの番号なら子とみなす');
  assert.equal(fake.pre({ toolName: 'bash', toolArgs: {}, sessionId: 's1' })?.permissionDecision, 'deny', '親のセッションの番号なら統括');
});

test('止めたあと：すべてのエージェントのツールを拒み、終わりの記録だけ通す', async (t) => {
  const { fake, app } = await startRun(t);
  fake.emit('assistant.turn_start', {});
  await app.control.stop();
  const reason = '利用者が Web管理画面で止めました。作業をやめ、embedded-debug-loop_finish（outcome は stopped）で終えてください。';
  for (const input of [{ toolName: 'view' }, { toolName: 'bash', agentId: 'w1', agentType: WORKER }, { toolName: 'task' }]) {
    const r = fake.pre({ toolArgs: {}, ...input });
    assert.equal(r?.permissionDecision, 'deny');
    assert.equal(r.permissionDecisionReason, reason);
  }
  assert.equal(fake.pre({ toolName: 'embedded-debug-loop_finish', toolArgs: {} }), undefined);
});

test('終わったあとは、何も拒まない', async (t) => {
  const { fake } = await startRun(t);
  await fake.call('finish', { outcome: 'success', conclusion: '済み', retro: 'なし' });
  assert.equal(fake.pre({ toolName: 'bash', toolArgs: {} }), undefined);
});

test('壊れた入力でも例外を投げない', async (t) => {
  const { fake } = await startRun(t);
  const hook = fake.config.hooks.onPreToolUse;
  for (const input of [null, undefined, 1, {}, { toolName: null }, Object.defineProperty({}, 'toolName', { get() { throw new Error('x'); } })]) {
    assert.doesNotThrow(() => hook(input, { sessionId: 's' }));
  }
});

// ---------- 作業の報告がある試行での、作業エージェントへの頼み直し ----------

const REPORTED_1 = 'この試行の作業は、もう報告されています。続けるときは、先に embedded-debug-loop_plan で試行 2 の計画を記録してから頼んでください。';
const task = (agentType, extra = {}) => ({ toolName: 'task', toolArgs: { agent_type: agentType, description: '作業', prompt: '…' }, ...extra });

test('実行中：今の試行に作業の報告があり、新しい計画がないときは、統括エージェントの作業エージェントへの task を拒む', async (t) => {
  const { fake } = await startRun(t);
  await fake.call('plan', { iteration: 1, plan: 'p1' });
  assert.equal(fake.pre(task(WORKER)), undefined, '報告の前は拒まない');
  fake.subagentStop({ agentType: WORKER, agentId: 'w1', response: '報告：できなかった' });
  for (const input of [
    task(WORKER),
    task('helper-a'), // プラグインの名前がなくても同じエージェント
    { toolName: 'task', toolArgs: JSON.stringify({ agent_type: WORKER, prompt: '…' }) }, // 引数が JSON の文でも読む
    task(WORKER, { sessionId: 's1' }),
  ]) {
    const r = fake.pre(input);
    assert.equal(r?.permissionDecision, 'deny', JSON.stringify(input));
    assert.equal(r.permissionDecisionReason, REPORTED_1);
  }
  // 検証エージェントやほかのエージェントへの頼み、子のエージェントの呼び出しは拒まない
  assert.equal(fake.pre(task(VERIFIER)), undefined);
  assert.equal(fake.pre(task('general-purpose')), undefined);
  assert.equal(fake.pre(task(WORKER, { agentId: 'x1', agentType: 'general-purpose' })), undefined);
  assert.equal(fake.pre(task(WORKER, { sessionId: 'child-1' })), undefined);
  assert.equal(fake.pre({ toolName: 'read_agent', toolArgs: { agent_id: 'w1' } }), undefined);

  // 評価の後でも、新しい計画がなければ拒む
  await fake.call('study', { iteration: 1, verdicts: [{ criterion: 'C1', result: 'fail', reason: 'r' }], next: 'n' });
  assert.equal(fake.pre(task(WORKER))?.permissionDecisionReason, REPORTED_1);

  // 試行 2 の計画を記録したら、頼める。報告が届いたら、また拒む（番号は今の試行に合わせる）
  await fake.call('plan', { iteration: 2, plan: 'p2' });
  assert.equal(fake.pre(task(WORKER)), undefined);
  fake.subagentStop({ agentType: WORKER, agentId: 'w2', response: '報告 2' });
  assert.equal(fake.pre(task(WORKER))?.permissionDecisionReason,
    'この試行の作業は、もう報告されています。続けるときは、先に embedded-debug-loop_plan で試行 3 の計画を記録してから頼んでください。');
});

test('準備の確認の頼みは拒まない：実行の前、下書きのとき、開始の後で試行 1 の計画の前', async (t) => {
  const a = await startWith(t);
  assert.equal(a.fake.pre(task(WORKER)), undefined, '実行のないとき');
  await a.fake.call('open', { request: '作業' });
  assert.equal(a.fake.pre(task(WORKER)), undefined, '下書きのとき');

  const b = await startRun(t, { prepare: false });
  assert.equal(b.fake.pre(task(WORKER)), undefined, '開始の後、計画の前');
  b.fake.subagentStop({ agentType: WORKER, agentId: 'w0', response: '準備：できている' });
  assert.equal(b.fake.pre(task(WORKER)), undefined, '準備の確認の返事の後も、計画の前は拒まない');

  // 終わった後の新しい頼み（下書き）でも拒まない
  await b.fake.call('plan', { iteration: 1, plan: 'p1' });
  b.fake.subagentStop({ agentType: WORKER, agentId: 'w1', response: '報告' });
  await b.fake.call('finish', { outcome: 'success', conclusion: '済み', retro: 'なし' });
  assert.equal(b.fake.pre(task(WORKER)), undefined, '終わった後');
  await b.fake.call('open', { request: '次の作業' });
  assert.equal(b.fake.pre(task(WORKER)), undefined, '終わった後の下書き');
});
