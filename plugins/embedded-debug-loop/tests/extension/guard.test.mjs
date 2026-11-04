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
