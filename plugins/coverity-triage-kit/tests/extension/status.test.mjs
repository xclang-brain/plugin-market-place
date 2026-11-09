// REQ-411, design 8.1 step 3: status polling (single flight, 2 s / 5 s, 10 s limit and process-tree kill).
import assert from 'node:assert/strict';
import test from 'node:test';

import { importExt, openAndStart, startWith, statusOf } from './helpers.mjs';

const statusCalls = (h) => h.runner.callsOf('run:status').length;

test('status is read with the run folder, a 10 s limit and PYTHONIOENCODING=utf-8', async (t) => {
  const h = await startWith(t);
  await openAndStart(h);
  await h.clock.advance(1);
  const [call] = h.runner.callsOf('run:status');
  assert.deepEqual(call.args.slice(4), ['status', '--run', h.ctx.run.runDir]);
  assert.equal(call.timeoutMs, 10_000);
  assert.equal(call.env.PYTHONIOENCODING, 'utf-8');
});

test('every 2 s while the state changes and the screen is viewed; 5 s when nothing changes', async (t) => {
  const h = await startWith(t);
  let n = 0;
  h.runner.on('run:status', () => statusOf({ recent: [`10:00:0${n++ % 10} 動き`] }));
  await openAndStart(h);
  h.state(); // the screen is viewed
  await h.clock.advance(1);
  assert.equal(statusCalls(h), 1);
  h.state();
  await h.clock.advance(2_000);
  assert.equal(statusCalls(h), 2, 'changed → 2 s');
  h.runner.on('run:status', () => statusOf());
  h.state();
  await h.clock.advance(2_000);
  assert.equal(statusCalls(h), 3);
  h.state();
  await h.clock.advance(2_000); // the last answer changed (new content), so 2 s again
  assert.equal(statusCalls(h), 4);
  h.state();
  await h.clock.advance(2_000);
  assert.equal(statusCalls(h), 4, 'unchanged → not after 2 s');
  await h.clock.advance(3_000);
  assert.equal(statusCalls(h), 5, 'unchanged → 5 s');
});

test('every 5 s while the screen is not viewed, even when the state changes', async (t) => {
  const h = await startWith(t);
  let n = 0;
  h.runner.on('run:status', () => statusOf({ recent: [`10:00:0${n++ % 10} 動き`] }));
  await openAndStart(h);
  await h.clock.advance(10_000); // the last view is old now
  const before = statusCalls(h);
  h.state({ visible: false });
  await h.clock.advance(2_000);
  assert.equal(statusCalls(h), before);
  await h.clock.advance(3_000);
  assert.equal(statusCalls(h), before + 1);
});

test('single flight: a slow status is never called twice at once', async (t) => {
  const h = await startWith(t);
  let release;
  let inFlight = 0;
  let maxInFlight = 0;
  h.runner.on('run:status', async () => {
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    await new Promise((resolve) => {
      release = resolve;
    });
    inFlight -= 1;
    return statusOf();
  });
  await openAndStart(h);
  await h.clock.advance(30_000);
  assert.equal(statusCalls(h), 1);
  // the orchestrator ending also needs a fresh status: it joins the call in progress
  h.fake.emit({ type: 'system.notification', timestamp: new Date().toISOString(), data: { kind: { type: 'agent_idle', agentId: h.ctx.run.rootId } } });
  await h.clock.flush();
  assert.equal(statusCalls(h), 1);
  release();
  await h.idle();
  assert.equal(maxInFlight, 1);
});

test('a status that runs out of time is shown as an error, and polling goes on every 5 s', async (t) => {
  const h = await startWith(t);
  h.runner.on('run:status', { $raw: { timedOut: true } });
  await openAndStart(h);
  await h.clock.advance(1);
  assert.match(h.state().run.statusError, /時間の上限を超えたので止めました/);
  await h.clock.advance(5_000);
  assert.equal(statusCalls(h), 2);
  h.runner.on('run:status', { ok: false, error: 'tool_version が違います' });
  await h.clock.advance(5_000);
  assert.match(h.state().run.statusError, /実行の様子を読めませんでした：tool_version が違います/);
  h.runner.on('run:status', () => statusOf());
  await h.clock.advance(5_000);
  assert.equal(h.state().run.statusError, null);
});

test('REQ-411 worker slots follow `parallel`; the live view carries the status parts the screen needs', async (t) => {
  const h = await startWith(t);
  h.runner.on('run:status', () => statusOf({ parallel: 5 }));
  await openAndStart(h);
  await h.clock.advance(1);
  const run = h.state().run;
  assert.equal(run.agents.filter((a) => a.role === 'worker').length, 5);
  assert.deepEqual(run.agents.map((a) => a.role), ['orchestrator', 'worker', 'worker', 'worker', 'worker', 'worker', 'reviewer', 'integrator']);
  for (const key of ['stage', 'stage_label', 'items', 'errors', 'initial', 'progress', 'criteria', 'criteria_results', 'counts']) assert.ok(key in run.live, key);
  assert.equal(run.recent[0], '10:15:02 20001 の作業が終わった');
});

test('the real runner kills the whole process tree when the time is up', { skip: process.platform === 'win32' ? 'POSIX process groups' : false }, async () => {
  const { runProcess } = await importExt('core/process.mjs');
  const { realClock } = await importExt('core/clock.mjs');
  const script = "const c=require('child_process').spawn(process.execPath,['-e','setTimeout(()=>{},60000)'],{stdio:'ignore'});console.log(c.pid);setTimeout(()=>{},60000)";
  let grandchild = null;
  const started = Date.now();
  const r = await runProcess({ command: process.execPath, args: ['-e', script], timeoutMs: 800, clock: realClock });
  grandchild = Number(r.stdout.trim());
  assert.equal(r.timedOut, true);
  assert.ok(Date.now() - started < 5_000);
  assert.ok(Number.isInteger(grandchild) && grandchild > 0, `grandchild pid: ${r.stdout}`);
  let alive = true;
  for (let i = 0; i < 50 && alive; i += 1) {
    try {
      process.kill(grandchild, 0);
      await new Promise((resolve) => setTimeout(resolve, 50));
    } catch {
      alive = false;
    }
  }
  assert.equal(alive, false, 'the grandchild is gone');
});

test('the real runner reports a missing command without throwing', async () => {
  const { runProcess } = await importExt('core/process.mjs');
  const { realClock } = await importExt('core/clock.mjs');
  const r = await runProcess({ command: 'no-such-command-xyz', args: [], timeoutMs: 2_000, clock: realClock });
  assert.ok(r.spawnError);
});
