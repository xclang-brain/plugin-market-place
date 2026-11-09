// design 8.3: records (log.jsonl, result.json, alive, issues/), keep 20, recovery after a reload or /clear (REQ-414).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { finishedStatus, openAndStart, startWith } from './helpers.mjs';

const runFolders = (dataRoot) => (fs.existsSync(dataRoot) ? fs.readdirSync(dataRoot).sort() : []);

test('a run writes log.jsonl, alive (every 5 s) and, at the end, result.json', async (t) => {
  const h = await startWith(t);
  await openAndStart(h);
  const dir = path.join(h.dirs.dataRoot, h.ctx.run.id);
  assert.ok(fs.existsSync(path.join(dir, 'log.jsonl')));
  assert.ok(fs.existsSync(path.join(dir, 'issues')));
  assert.ok(!fs.existsSync(path.join(dir, 'result.json')));
  const before = Number(fs.readFileSync(path.join(dir, 'alive'), 'utf8'));
  await h.clock.advance(5_000);
  assert.ok(Number(fs.readFileSync(path.join(dir, 'alive'), 'utf8')) > before);
  const begin = JSON.parse(fs.readFileSync(path.join(dir, 'log.jsonl'), 'utf8').split('\n')[0]);
  assert.equal(begin.t, 'begin');
  assert.equal(begin.sessionId, 'session-fake-1');
  h.runner.on('run:status', () => finishedStatus());
  h.fake.emit(sdkEvent.agentIdle(h.ctx.run.rootId));
  await h.idle();
  const result = JSON.parse(fs.readFileSync(path.join(dir, 'result.json'), 'utf8'));
  assert.equal(result.endReason.kind, 'all_passed');
  assert.equal(result.runDir, h.ctx.run.runDir);
});

test('only 20 records are kept (finished ones, oldest first)', async (t) => {
  const h = await startWith(t, {
    beforeStart: ({ dataRoot }) => {
      for (let i = 0; i < 25; i += 1) {
        const dir = path.join(dataRoot, `20260101-0000${String(i).padStart(2, '0')}-aaaaaa`);
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(path.join(dir, 'log.jsonl'), '{"t":"begin"}\n');
        fs.writeFileSync(path.join(dir, 'result.json'), '{}');
      }
    },
  });
  assert.equal(runFolders(h.dirs.dataRoot).length, 20);
  assert.equal(runFolders(h.dirs.dataRoot)[0], '20260101-000005-aaaaaa');
});

function unfinishedRecord(dataRoot, { id, sessionId, alive, treeIds = [], cwd }) {
  const dir = path.join(dataRoot, id);
  fs.mkdirSync(path.join(dir, 'issues'), { recursive: true });
  const lines = [
    { t: 'begin', at: alive - 1000, runId: id, sessionId, cwd, settings: { models: {} } },
    { t: 'note', at: alive - 900, kind: 'new_run', runDir: '/out/20261108-000000' },
    { t: 'tree', at: alive - 800, agentIds: treeIds },
  ];
  fs.writeFileSync(path.join(dir, 'log.jsonl'), `${lines.map((l) => JSON.stringify(l)).join('\n')}\n`);
  fs.writeFileSync(path.join(dir, 'alive'), String(alive));
  return dir;
}

test('REQ-414 after /clear (another session, old alive mark) the run ends as "cut off" and the next open says so', async (t) => {
  const now = Date.UTC(2026, 10, 8, 1, 0, 0);
  let dir;
  const h = await startWith(t, {
    beforeStart: ({ dataRoot }) => {
      dir = unfinishedRecord(dataRoot, { id: '20261108-005000-abcdef', sessionId: 'old-session', alive: now - 60_000, cwd: 'x' });
    },
  });
  const result = JSON.parse(fs.readFileSync(path.join(dir, 'result.json'), 'utf8'));
  assert.deepEqual([result.endReason.kind, result.endReason.detail], ['aborted', 'cut_off']);
  assert.ok(result.issues.some((i) => /\/clear で切れた/.test(i.title)));
  const opened = await h.open();
  assert.doesNotMatch(opened.text, /前の実行が途中で終わっていました/, 'another folder: not this screen');
});

test('REQ-414 the same folder: the recovered run is announced once and shown as the previous run', async (t) => {
  const now = Date.UTC(2026, 10, 8, 1, 0, 0);
  const h = await startWith(t, {
    beforeStart: ({ dataRoot }) => {
      unfinishedRecord(dataRoot, { id: '20261108-005000-abcdef', sessionId: 'old-session', alive: now - 60_000, cwd: path.join(path.dirname(dataRoot), '..', 'repo') });
    },
  });
  const first = await h.open();
  assert.match(first.text, /前の実行が途中で終わっていました。画面で結果を見られます。/);
  const prev = h.state().previous;
  assert.equal(prev.runId, '20261108-005000-abcdef');
  assert.equal(prev.endReason.detail, 'cut_off');
  assert.equal(prev.runDir, '/out/20261108-000000');
  const second = await h.open();
  assert.doesNotMatch(second.text, /前の実行が途中で終わっていました/);
});

test('another session with a fresh alive mark is checked again after 31 s', async (t) => {
  const now = Date.UTC(2026, 10, 8, 1, 0, 0);
  let dir;
  const h = await startWith(t, {
    beforeStart: ({ dataRoot }) => {
      dir = unfinishedRecord(dataRoot, { id: '20261108-005900-abcdef', sessionId: 'other', alive: now - 1_000, cwd: 'x' });
    },
  });
  assert.ok(!fs.existsSync(path.join(dir, 'result.json')));
  await h.clock.advance(31_000);
  assert.ok(fs.existsSync(path.join(dir, 'result.json')));
});

test('the same session (only the extension restarted): the tree is denied at once, running agents are cancelled, "extension restarted"', async (t) => {
  const now = Date.UTC(2026, 10, 8, 1, 0, 0);
  let dir;
  const h = await startWith(t, {
    beforeStart: ({ dataRoot, fake }) => {
      dir = unfinishedRecord(dataRoot, { id: '20261108-005900-abcdef', sessionId: 'session-fake-1', alive: now - 1_000, treeIds: ['old-root', 'old-w'], cwd: 'x' });
      fake.spawn({ agentId: 'old-root', status: 'running' });
      fake.spawn({ agentId: 'old-w', parentAgentId: 'old-root', status: 'running' });
    },
  });
  const r = await h.fake.callHook('onPreToolUse', { agentId: 'old-w', toolName: 'view' });
  assert.equal(r?.permissionDecision, 'deny');
  await h.idle();
  assert.ok(h.fake.callsOf('tasks.cancel').some((c) => c.args.id === 'old-root'));
  const result = JSON.parse(fs.readFileSync(path.join(dir, 'result.json'), 'utf8'));
  assert.equal(result.endReason.detail, 'extension_restarted');
});
