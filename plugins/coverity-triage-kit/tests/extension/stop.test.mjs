// REQ-413, design 8.3: stopping a run (cancel the tree, deny its tools, stop detached jobs, report what did not stop).
import assert from 'node:assert/strict';
import test from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { openAndStart, startWith, statusOf } from './helpers.mjs';

async function running(t) {
  const h = await startWith(t);
  const root = await openAndStart(h);
  await h.clock.advance(1);
  h.fake.emit(sdkEvent.toolStart(root, 'tc-1', 'task', { description: '20001 の作業' }));
  h.fake.emit(sdkEvent.subagentStarted('w1', { parentId: root, agentType: h.defaults.roles.worker, toolCallId: 'tc-1' }));
  h.fake.spawn({ agentId: 'w1', parentAgentId: root, agentType: h.defaults.roles.worker });
  await h.idle();
  return { h, root };
}

test('REQ-413 stop: the orchestrator is cancelled (its agents with it), detached jobs are stopped, the run ends as "user stopped" within 10 s', async (t) => {
  const { h, root } = await running(t);
  const t0 = h.clock.now();
  await h.stop();
  assert.equal(h.state().run.status, 'stopping');
  await h.idle();
  const run = h.state().run;
  assert.equal(run.status, 'finished');
  assert.ok(h.clock.now() - t0 <= 10_000);
  assert.deepEqual(run.endReason, { kind: 'user_stopped', detail: null, notes: [], unstoppable: false });
  assert.ok(h.fake.callsOf('tasks.cancel').some((c) => c.args.id === root));
  assert.equal(h.fake.task('w1').status, 'cancelled');
  const [stop] = h.runner.callsOf('run:stop');
  assert.deepEqual(stop.args.slice(4), ['stop', '--run', h.ctx.run.runDir]);
  assert.deepEqual(h.fake.callsOf('tools.updateSubagentSettings').at(-1).args, { subagents: null });
  // REQ-422: a stopped run keeps only the mechanical issues and runs no agent and no summary
  assert.ok(run.issues.every((i) => i.source === 'mechanical'));
  assert.equal(h.runner.callsOf('run:summary').length, 0);
  assert.equal(h.fake.callsOf('tasks.startAgent').length, 1);
});

test('REQ-413 after stop, every agent of the tree (and a new child) is denied every tool; other chat work is not', async (t) => {
  const { h, root } = await running(t);
  await h.stop();
  for (const agentId of [root, 'w1']) {
    const r = await h.fake.callHook('onPreToolUse', { agentId, toolName: 'view', toolArgs: { path: '/x' } });
    assert.equal(r?.permissionDecision, 'deny', agentId);
    assert.match(r.permissionDecisionReason, /この実行は止められたため、ツール「view」は使えません/);
  }
  const child = await h.fake.callHook('onPreToolUse', { agentId: 'w9', parentAgentId: 'w1', toolName: 'edit' });
  assert.equal(child?.permissionDecision, 'deny');
  assert.equal(await h.fake.callHook('onPreToolUse', { toolName: 'edit' }), undefined, 'the main chat agent');
  assert.equal(await h.fake.callHook('onPreToolUse', { agentId: 'stranger', toolName: 'edit' }), undefined, 'another agent');
  await h.idle();
  const after = await h.fake.callHook('onPreToolUse', { agentId: 'w1', toolName: 'view' });
  assert.equal(after?.permissionDecision, 'deny', 'still denied after the run ended');
});

test('REQ-413 agents and jobs that do not stop in 8 s are listed with how to stop them', async (t) => {
  const { h } = await running(t);
  h.fake.task('w1').stubborn = true;
  h.runner.on('run:stop', { ok: true, stopped_jobs: ['initial'], unstopped: [{ name: 'verify', pid: 4242, how: 'タスクマネージャーで 4242 を終える' }] });
  await h.stop();
  await h.idle();
  const run = h.state().run;
  assert.equal(run.status, 'finished');
  assert.deepEqual(run.unstopped.map((u) => [u.agentId, u.role]), [['w1', 'worker']]);
  assert.match(run.unstopped[0].howTo, /Esc を2回押す/);
  assert.deepEqual(run.unstoppedJobs, [{ name: 'verify', pid: 4242, how: 'タスクマネージャーで 4242 を終える' }]);
  assert.equal(run.endReason.unstoppable, true);
  assert.deepEqual(run.endReason.notes, ['止めきれなかったエージェント 1 体', '止めきれなかった処理 1 件']);
  assert.equal(run.agents.find((a) => a.agentId === 'w1').status, 'unstopped');
  assert.ok(run.issues.some((i) => /止めても止まらない処理がありました（verify）/.test(i.title)));
});

test('REQ-413 the stop command failing is shown as a job that may still run', async (t) => {
  const { h } = await running(t);
  h.runner.on('run:stop', { $raw: { timedOut: true } });
  await h.stop();
  await h.idle();
  const [job] = h.state().run.unstoppedJobs;
  assert.match(job.how, /止めたかを確かめられませんでした/);
});

test('REQ-413 stop while new-run is still running: no orchestrator is started; the run folder jobs are stopped', async (t) => {
  const h = await startWith(t);
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const newRun = h.runner.handlers['run:new-run'];
  h.runner.on('run:new-run', async (arg) => {
    await gate;
    return newRun(arg);
  });
  await h.open();
  await h.idle();
  const starting = h.core.start({});
  starting.catch(() => {});
  await h.clock.flush();
  assert.equal(h.state().run.status, 'starting');
  await h.stop();
  assert.equal(h.state().run.status, 'stopping');
  release();
  await assert.rejects(starting, /開始している途中で止められました/);
  await h.idle();
  assert.equal(h.fake.callsOf('tasks.startAgent').length, 0);
  assert.equal(h.runner.callsOf('run:stop').length, 1);
  assert.equal(h.state().run.endReason.kind, 'user_stopped');
});

test('stopping twice is harmless; stopping with no run is refused', async (t) => {
  const { h } = await running(t);
  await h.stop();
  assert.deepEqual(await h.stop(), { already: true });
  await h.idle();
  await assert.rejects(h.stop(), /止める実行がありません/);
  const h2 = await startWith(t);
  await assert.rejects(h2.stop(), /止める実行がありません/);
  void statusOf;
});
