// REQ-415, REQ-421, design 8.1 steps 4-5 and 8.2: restarts, end reasons, summary and result links.
import assert from 'node:assert/strict';
import test from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { GUI_URL, finishedStatus, item, openAndStart, startWith, statusOf, summaryOf } from './helpers.mjs';

async function started(t) {
  const h = await startWith(t);
  h.runner.on('run:status', () => statusOf({ recent: [] }));
  await openAndStart(h);
  await h.clock.advance(1);
  return h;
}

// The orchestrator stops: idle (turn ended), failed, or cancelled
async function rootEnds(h, how = 'idle') {
  const root = h.ctx.run.rootId;
  if (how === 'idle') h.fake.emit(sdkEvent.agentIdle(root));
  if (how === 'failed') h.fake.emit(sdkEvent.subagentFailed(root, { error: 'モデルの失敗' }));
  if (how === 'cancelled') h.fake.emit(sdkEvent.subagentCompleted(root, { cancelled: true }));
  await h.idle();
  await h.clock.advance(1_000);
  await h.idle();
}

test('REQ-415 an unfinished run: the orchestrator is restarted from the same run folder, up to 2 times, and recent activity says so', async (t) => {
  const h = await started(t);
  const first = h.ctx.run.rootId;
  await rootEnds(h);
  assert.equal(h.ctx.run.status, 'running');
  assert.equal(h.fake.callsOf('tasks.startAgent').length, 2);
  const restart = h.fake.callsOf('tasks.startAgent')[1].args;
  assert.equal(restart.agentType, h.defaults.orchestrator);
  assert.match(restart.prompt, /続きから進めてください（始め直し 1 回目）/);
  assert.ok(restart.prompt.includes(h.ctx.run.runDir));
  assert.notEqual(h.ctx.run.rootId, first);
  const view = h.state().run;
  assert.equal(view.restarts, 1);
  assert.match(view.recent[0], /続きから始め直しました（1 回目／上限 2 回）/);
  assert.equal(view.agents.find((a) => a.slot === 'orchestrator').agentId, h.ctx.run.rootId);
  assert.ok(view.pastAgents.some((a) => a.agentId === first && a.role === 'orchestrator'));
  assert.equal(h.runner.callsOf('run:new-run').length, 1, 'no new run folder');

  await rootEnds(h, 'failed');
  assert.equal(h.fake.callsOf('tasks.startAgent').length, 3);
  assert.equal(h.ctx.run.restarts, 2);
  await rootEnds(h);
  assert.equal(h.fake.callsOf('tasks.startAgent').length, 3, 'no third restart');
  const end = h.state().run;
  assert.equal(end.status, 'finished');
  assert.deepEqual([end.endReason.kind, end.endReason.detail], ['aborted', 'orchestrator_ended']);
  assert.ok(end.issues.some((i) => /統括エージェントが実行の途中で止まりました（2 回）/.test(i.title)));
  assert.equal(h.runner.callsOf('run:summary').length, 0, 'no summary for an unfinished run');
});

test('REQ-415 after the restarts, a failed orchestrator ends as "orchestrator_failed"', async (t) => {
  const h = await started(t);
  await rootEnds(h, 'failed');
  await rootEnds(h, 'failed');
  await rootEnds(h, 'failed');
  assert.equal(h.state().run.endReason.detail, 'orchestrator_failed');
});

test('design 8.2 no restart for: the credit limit, an interruption from the chat, a run the batch script cannot continue', async (t) => {
  const limit = await started(t);
  limit.fake.emit(sdkEvent.limitRequested('l1'));
  await limit.idle();
  await rootEnds(limit);
  assert.equal(limit.fake.callsOf('tasks.startAgent').length, 1);
  assert.equal(limit.state().run.endReason.detail, 'credit_limit');

  const cancelled = await started(t);
  await rootEnds(cancelled, 'cancelled');
  assert.equal(cancelled.state().run.endReason.detail, 'chat_interrupted');

  const halted = await started(t);
  halted.runner.on('run:status', () => statusOf({ errors: [{ item: null, kind: '認証', detail: '401 Unauthorized', level: 'critical' }] }));
  await rootEnds(halted);
  assert.equal(halted.fake.callsOf('tasks.startAgent').length, 1);
  const end = halted.state().run;
  assert.deepEqual([end.endReason.kind, end.endReason.detail], ['aborted', 'run_halted']);
  assert.ok(end.issues.some((i) => i.title === '一括実行で重大なエラーがありました'));
});

test('REQ-421 a finished run: summary, last status, a link to the lowest-confidence item, and "all passed"', async (t) => {
  const h = await started(t);
  h.runner.on('run:status', () => finishedStatus({ issues_found: [{ title: '統括が見つけた課題', body: '本文', at: '2026-11-08T01:00:00Z' }] }));
  await rootEnds(h);
  const run = h.state().run;
  assert.equal(run.status, 'finished');
  assert.deepEqual(run.endReason, { kind: 'all_passed', detail: null, notes: [], unstoppable: false });
  assert.equal(h.runner.callsOf('run:summary').length, 1);
  assert.deepEqual(h.runner.callsOf('run:summary')[0].args.slice(4), ['summary', '--run', h.ctx.run.runDir]);
  assert.deepEqual(run.summary.counts, summaryOf().counts);
  const [gui] = h.runner.callsOf('gui:open');
  assert.deepEqual(gui.args.slice(4), ['open', '--repo', '/repo', '--run', h.ctx.run.runDir, '--view', 'summary', '--item', 'G1', '--no-browser']);
  assert.deepEqual(run.links, { summaryUrl: GUI_URL, itemUrl: `${GUI_URL}&item=G1`, item: 'G1' });
  // the orchestrator's issues only after a normal end; overrides removed
  assert.deepEqual(run.issues.map((i) => [i.id, i.source, i.title]), [['O1', 'orchestrator', '統括が見つけた課題']]);
  assert.deepEqual(h.fake.callsOf('tools.updateSubagentSettings').at(-1).args, { subagents: null });
  // the idle orchestrator is cancelled quietly
  assert.ok(h.fake.callsOf('tasks.cancel').some((c) => c.args.id === h.ctx.run.rootId));
});

test('design 8.2 "max reached": flagged, unreviewed, failed or left-over items, with their numbers', async (t) => {
  const h = await started(t);
  h.runner.on(
    'run:status',
    () =>
      finishedStatus({
        items: [
          item('20001', { review: { outcome: 'flag', findings: [] } }),
          item('20002', { review: null }),
          item('20003', { status: 'error', review: null, error: '取得できない' }),
          item('20004', { status: 'pending', review: null }),
        ],
      }),
  );
  await rootEnds(h);
  const end = h.state().run.endReason;
  assert.equal(end.kind, 'max_reached');
  assert.deepEqual(end.notes, ['要確認 1 件', '未検証 1 件', 'エラー 1 件', '未処理 1 件']);
  assert.ok(h.state().run.issues.some((i) => /処理できなかった作業がありました（1 件）/.test(i.title)));
});

test('design 8.2 no warning matched: all passed with "条件に合う警告はなかった"; no item link', async (t) => {
  const h = await started(t);
  h.runner.on('run:status', () => finishedStatus({ items: [], counts: { pending: 0, in_progress: 0, done: 0, error: 0, merged: 0, total: 0 } }));
  h.runner.on('run:summary', () => summaryOf({ results: [], counts: { issues: 0, items: 0 } }));
  await rootEnds(h);
  const run = h.state().run;
  assert.deepEqual([run.endReason.kind, run.endReason.notes], ['all_passed', ['条件に合う警告はなかった']]);
  assert.ok(!h.runner.callsOf('gui:open')[0].args.includes('--item'));
  assert.equal(run.links.itemUrl, null);
});

test('summary or screen links fail: the run still finishes and the reasons are shown', async (t) => {
  const h = await started(t);
  h.runner.on('run:status', () => finishedStatus());
  h.runner.on('run:summary', { ok: false, error: '一覧を書けません' });
  h.runner.on('gui:open', { ok: false, error: 'ポートがありません' });
  await rootEnds(h);
  const run = h.state().run;
  assert.equal(run.status, 'finished');
  assert.match(run.summaryError, /一覧を書けません/);
  assert.match(run.guiError, /ポートがありません/);
  assert.equal(run.endReason.kind, 'all_passed', 'decided from the status items');
});
