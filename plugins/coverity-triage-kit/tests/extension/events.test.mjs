// REQ-411, REQ-412, design 8.2: slots, roles from defaults.json ids, past agents, phase, waits.
import assert from 'node:assert/strict';
import test from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { openAndStart, startWith, statusOf } from './helpers.mjs';

async function started(t, options) {
  const h = await startWith(t, options);
  const root = await openAndStart(h);
  await h.clock.advance(1);
  let call = 0;
  // the orchestrator launches an agent with task(description)
  h.launch = async (agentId, agentType, description, extra = {}) => {
    call += 1;
    const tc = `tc-task-${call}`;
    h.fake.emit(sdkEvent.toolStart(root, tc, 'task', { description, agent_type: agentType }));
    h.fake.emit(sdkEvent.subagentStarted(agentId, { parentId: root, agentType, toolCallId: tc, model: 'gpt-6-luna', ...extra }));
    await h.idle();
  };
  return { h, root };
}

const slot = (h, name) => h.state().run.agents.find((a) => a.slot === name);

test('REQ-411 roles come from the defaults.json ids (qualified or after ":"); the assignment is the task description', async (t) => {
  const { h } = await started(t);
  const { worker, reviewer, integrator } = h.defaults.roles;
  await h.launch('w1', worker, '20001 の作業');
  await h.launch('w2', worker.split(':').pop(), '20002 の作業');
  await h.launch('r1', reviewer.split(':').pop(), '20001 の確かめ');
  await h.launch('i1', integrator, '統合');
  assert.equal(slot(h, 'worker-1').task, '20001 の作業');
  assert.equal(slot(h, 'worker-1').status, 'running');
  assert.equal(slot(h, 'worker-1').actualModel, 'gpt-6-luna');
  assert.equal(slot(h, 'worker-2').task, '20002 の作業');
  assert.equal(slot(h, 'reviewer').task, '20001 の確かめ');
  assert.equal(slot(h, 'integrator').task, '統合');
  assert.equal(h.state().run.phase, 'study', 'a running reviewer → study');
});

test('REQ-411 an ended agent goes to "past agents" when another one takes its slot; its reply can be read', async (t) => {
  const { h } = await started(t);
  const { worker } = h.defaults.roles;
  for (const id of ['w1', 'w2', 'w3']) await h.launch(id, worker, `${id} の作業`);
  await h.fake.callHook('onSubagentStop', { agentId: 'w1', response: 'w1: 修正（確信度 高）— fgets' });
  h.fake.emit(sdkEvent.subagentCompleted('w1'));
  await h.idle();
  assert.equal(slot(h, 'worker-1').status, 'completed');
  assert.equal(slot(h, 'worker-1').hasReply, true);
  assert.equal(h.core.getReply('w1'), 'w1: 修正（確信度 高）— fgets');
  assert.deepEqual(h.state().run.pastAgents, []);
  await h.launch('w4', worker, 'w4 の作業');
  assert.equal(slot(h, 'worker-1').agentId, 'w4');
  const past = h.state().run.pastAgents;
  assert.deepEqual(past.map((p) => [p.agentId, p.role, p.status, p.task]), [['w1', 'worker', 'completed', 'w1 の作業']]);
  assert.equal(past[0].hasReply, true);
});

test('more workers than slots get an extra slot; an agent with no role is "other" and has no slot', async (t) => {
  const { h } = await started(t);
  for (const id of ['w1', 'w2', 'w3', 'w4']) await h.launch(id, h.defaults.roles.worker, id);
  assert.equal(h.state().run.agents.filter((a) => a.role === 'worker').length, 4);
  await h.launch('g1', 'general-purpose', '代わりの作業');
  assert.equal(h.ctx.run.tree.get('g1').role, 'other');
  assert.ok(!h.state().run.agents.some((a) => a.agentId === 'g1'));
});

test('design 8.2 phase: worker or integrator → do; reviewer → study; nothing running → plan; running tasks or a long stage → do', async (t) => {
  const { h } = await started(t);
  h.runner.on('run:status', () => statusOf({ running_tasks: [] }));
  await h.clock.advance(5_000);
  assert.equal(h.state().run.phase, 'plan');
  h.runner.on('run:status', () => statusOf({ running_tasks: [], stage: 'initial' }));
  await h.clock.advance(5_000);
  assert.equal(h.state().run.phase, 'do');
  h.runner.on('run:status', () => statusOf({ running_tasks: [], stage: 'fetch' }));
  await h.clock.advance(5_000);
  assert.equal(h.state().run.phase, 'do', 'fetching the warnings is work, too');
  h.runner.on('run:status', () => statusOf({ running_tasks: [{ task: 'reviewer:20001@1', role: 'reviewer' }], stage: 'review' }));
  await h.clock.advance(5_000);
  assert.equal(h.state().run.phase, 'study');
  h.runner.on('run:status', () => statusOf({ running_tasks: [], stage: 'items' }));
  await h.clock.advance(5_000);
  await h.launch('i1', h.defaults.roles.integrator, '統合');
  assert.equal(h.state().run.phase, 'do');
  h.fake.emit(sdkEvent.subagentCompleted('i1'));
  await h.idle();
  assert.equal(h.state().run.phase, 'plan');
});

test('REQ-412 waits: permission details, question, credit limit; the slot shows "waiting"; answers end the wait', async (t) => {
  const { h } = await started(t);
  await h.launch('w1', h.defaults.roles.worker, '20001 の作業');
  h.fake.emit(sdkEvent.permissionRequested('w1', 'p1', { kind: 'shell', fullCommandText: 'uv run x', possiblePaths: ['/out'], warning: '注意' }));
  h.fake.emit(sdkEvent.userInputRequested('w1', 'q1', 'どちらにしますか'));
  h.fake.emit(sdkEvent.limitRequested('l1'));
  await h.idle();
  const waits = h.state().run.waits;
  assert.deepEqual(waits.map((w) => [w.kind, w.slot]), [['permission', 'worker-1'], ['question', 'worker-1'], ['limit', null]]);
  assert.equal(waits[0].detail.command, 'uv run x');
  assert.deepEqual(waits[0].detail.paths, ['/out']);
  assert.equal(waits[1].detail.question, 'どちらにしますか');
  assert.equal(slot(h, 'worker-1').status, 'waiting');
  // observe only: the extension never answers
  await h.clock.flush();
  assert.deepEqual(h.fake.answers, []);
  h.fake.emit(sdkEvent.permissionCompleted('p1'));
  h.fake.emit(sdkEvent.userInputCompleted('q1'));
  await h.idle();
  assert.equal(slot(h, 'worker-1').status, 'running');
  assert.equal(h.state().run.waits.length, 1);
});

test('REQ-412 waits of the main agent and of agents outside the run are not shown', async (t) => {
  const { h } = await started(t);
  h.fake.emit(sdkEvent.permissionRequested(undefined, 'p-main', { kind: 'write', fileName: '/x' }));
  h.fake.emit(sdkEvent.permissionRequested('stranger', 'p-other', { kind: 'write', fileName: '/x' }));
  h.fake.emit(sdkEvent.permissionRequested(h.ctx.run.rootId, 'p-ext', { kind: 'extension-permission-access' }));
  await h.idle();
  assert.deepEqual(h.state().run.waits, []);
});

test('onPermissionRequest always answers no-result and never throws', async (t) => {
  const { h } = await started(t);
  assert.deepEqual(await h.fake.config.onPermissionRequest({ kind: 'shell' }), { kind: 'no-result' });
  assert.deepEqual(await h.fake.config.onPermissionRequest(null), { kind: 'no-result' });
});
