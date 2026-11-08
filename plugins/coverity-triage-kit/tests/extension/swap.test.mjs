// REQ-003, design 8.4: names live only in defaults.json. Renaming agents in a copied defaults.json still works.
import assert from 'node:assert/strict';
import test from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { REAL_DEFAULTS, finishedStatus, openAndStart, startWith } from './helpers.mjs';

test('a renamed worker (another id in defaults.json) gets the worker slot, the override and the request', async (t) => {
  const h = await startWith(t, { defaults: { roles: { worker: 'kitchen:cook' } } });
  const root = await openAndStart(h, { models: { worker: 'claude-haiku-5.5' } });
  const agents = h.fake.callsOf('tools.updateSubagentSettings')[0].args.subagents.agents;
  assert.deepEqual(agents['kitchen:cook'], { model: 'claude-haiku-5.5' });
  assert.equal(agents[REAL_DEFAULTS.roles.worker], undefined, 'the old name is not used');
  const prompt = h.fake.callsOf('tasks.startAgent')[0].args.prompt;
  assert.match(prompt, /作業役：kitchen:cook/);
  assert.ok(!prompt.includes(REAL_DEFAULTS.roles.worker));

  h.fake.emit(sdkEvent.subagentStarted('c1', { parentId: root, agentType: 'cook' }));
  h.fake.emit(sdkEvent.subagentStarted('old', { parentId: root, agentType: REAL_DEFAULTS.roles.worker.split(':').pop() }));
  await h.idle();
  const view = h.state().run;
  assert.equal(view.agents.find((a) => a.slot === 'worker-1').agentId, 'c1');
  assert.equal(h.ctx.run.tree.get('old').role, 'other', 'the previous worker name no longer counts as the worker');
});

test('every name renamed (orchestrator, worker, reviewer, integrator): open, start, slots, guard and end all work', async (t) => {
  const names = { orchestrator: 'kitchen:chef', roles: { worker: 'kitchen:cook', reviewer: 'kitchen:taster', integrator: 'kitchen:plater' } };
  const h = await startWith(t, { defaults: names });
  await h.open();
  await h.idle();
  assert.deepEqual(h.state().setup.defaults, { orchestrator: 'claude-sonnet-5.5', worker: 'gpt-6-luna', reviewer: 'claude-sonnet-5.5', integrator: 'gpt-6-luna' });
  await h.core.start({});
  await h.idle();
  const root = h.ctx.run.rootId;
  assert.equal(h.fake.callsOf('tasks.startAgent')[0].args.agentType, 'kitchen:chef');
  h.fake.emit(sdkEvent.subagentStarted('t1', { parentId: root, agentType: 'kitchen:taster' }));
  h.fake.emit(sdkEvent.subagentStarted('p1', { parentId: root, agentType: 'plater' }));
  await h.idle();
  assert.equal(h.state().run.agents.find((a) => a.slot === 'reviewer').agentId, 't1');
  assert.equal(h.state().run.agents.find((a) => a.slot === 'integrator').agentId, 'p1');
  const review = '/o/items/1/review.json';
  assert.equal(await h.fake.callHook('onPreToolUse', { agentId: 't1', toolName: 'edit', toolArgs: { path: review } }), undefined);
  assert.equal((await h.fake.callHook('onPreToolUse', { agentId: 'p1', toolName: 'edit', toolArgs: { path: review } }))?.permissionDecision, 'deny');
  assert.equal((await h.fake.callHook('onPreToolUse', { agentId: root, toolName: 'edit' }))?.permissionDecision, 'deny');
  h.runner.on('run:status', () => finishedStatus());
  h.fake.emit(sdkEvent.agentIdle(root));
  await h.idle();
  assert.equal(h.state().run.endReason.kind, 'all_passed');
});

test('another setup and apply skill name changes only the guidance text', async (t) => {
  const h = await startWith(t, { defaults: { setupSkill: 'prepare-it', applySkill: 'ship-it' } });
  await h.open();
  await h.idle();
  const state = h.state();
  assert.match(state.guide.setup, /\/prepare-it/);
  assert.match(state.guide.apply, /\/ship-it で反映する/);
});
