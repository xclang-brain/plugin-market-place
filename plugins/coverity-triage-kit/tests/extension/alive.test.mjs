// design 8.3: the alive mark keeps another session from cutting off a live run. It is written atomically,
// before the log header, and an empty or broken mark never counts as "very old".
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import path from 'node:path';
import test from 'node:test';

import { createFakeClock } from './fake-clock.mjs';
import { importExt, tempDir } from './helpers.mjs';

const { createStore } = await importExt('core/store.mjs');
const { classifyUnfinishedRun } = await importExt('core/recovery.mjs');
const { LIMITS } = await importExt('core/constants.mjs');

function storeIn(t) {
  const clock = createFakeClock();
  const dataRoot = tempDir(t, 'ext-alive-');
  return { clock, dataRoot, store: createStore({ dataRoot, clock }) };
}

test('createRun writes the alive mark before log.jsonl, and no temp file is left', (t) => {
  const { dataRoot, store, clock } = storeIn(t);
  store.createRun('r1', { t: 'begin', at: clock.now(), sessionId: 's' });
  assert.deepEqual(fs.readdirSync(path.join(dataRoot, 'r1')).sort(), ['alive', 'issues', 'log.jsonl']);
  assert.equal(store.readAlive('r1'), clock.now());
  // when the log header cannot be written, the mark is already there (it comes first)
  fs.mkdirSync(path.join(dataRoot, 'r2', 'log.jsonl'), { recursive: true });
  assert.throws(() => store.createRun('r2', { t: 'begin', at: clock.now(), sessionId: 's' }));
  assert.equal(store.readAlive('r2'), clock.now());
});

test('writeAlive replaces the mark through a temp file and leaves none behind', async (t) => {
  const { dataRoot, store, clock } = storeIn(t);
  store.createRun('r1', { t: 'begin', at: clock.now(), sessionId: 's' });
  await clock.advance(5_000);
  store.writeAlive('r1');
  assert.equal(store.readAlive('r1'), clock.now());
  assert.deepEqual(fs.readdirSync(path.join(dataRoot, 'r1')).filter((n) => n.includes('.tmp-')), []);
});

test('readAlive: missing, empty, zero, negative or garbled marks are null', (t) => {
  const { dataRoot, store } = storeIn(t);
  const dir = path.join(dataRoot, 'r1');
  fs.mkdirSync(dir, { recursive: true });
  assert.equal(store.readAlive('r1'), null, 'missing');
  for (const text of ['', '  \n', '0', '-5', 'abc', 'Infinity']) {
    fs.writeFileSync(path.join(dir, 'alive'), text);
    assert.equal(store.readAlive('r1'), null, JSON.stringify(text));
  }
  fs.writeFileSync(path.join(dir, 'alive'), '1762563600000\n');
  assert.equal(store.readAlive('r1'), 1762563600000);
});

test('classifyUnfinishedRun: with no readable mark, the begin time stands in for it', () => {
  const now = Date.UTC(2026, 10, 8, 1, 0, 0);
  const other = (at) => ({ t: 'begin', at, sessionId: 'other' });
  assert.equal(classifyUnfinishedRun({ begin: other(now - 1_000), aliveAt: null, now, sessionId: 'me' }), 'check_later');
  assert.equal(classifyUnfinishedRun({ begin: other(now - LIMITS.ALIVE_STALE_MS - 1), aliveAt: null, now, sessionId: 'me' }), 'cut_off');
  assert.equal(classifyUnfinishedRun({ begin: null, aliveAt: null, now, sessionId: 'me' }), 'cut_off');
  assert.equal(classifyUnfinishedRun({ begin: other(now - 60_000), aliveAt: now - 1_000, now, sessionId: 'me' }), 'check_later');
  assert.equal(classifyUnfinishedRun({ begin: other(now - 60_000), aliveAt: now - 60_000, now, sessionId: 'me' }), 'cut_off');
  assert.equal(classifyUnfinishedRun({ begin: { ...other(now - 60_000), sessionId: 'me' }, aliveAt: null, now, sessionId: 'me' }), 'cancel_and_end');
});

test('a run just created by another session (empty alive mark) is checked later, not cut off', (t) => {
  const { dataRoot, store, clock } = storeIn(t);
  store.createRun('r1', { t: 'begin', at: clock.now() - 500, sessionId: 'other' });
  fs.writeFileSync(path.join(dataRoot, 'r1', 'alive'), ''); // as a reader could see a non-atomic rewrite
  const begin = store.readLog('r1').find((e) => e.t === 'begin');
  const action = classifyUnfinishedRun({ begin, aliveAt: store.readAlive('r1'), now: clock.now(), sessionId: 'me' });
  assert.equal(action, 'check_later');
});

// Makes the next `count` reads of an alive mark fail with `code`; the store imports readFileSync by
// name, so the stub is pushed into the ESM bindings too.
function failAliveReads(t, count, code) {
  const real = fs.readFileSync;
  let left = count;
  fs.readFileSync = function stub(file, ...rest) {
    if (left > 0 && String(file).endsWith(`${path.sep}alive`)) {
      left -= 1;
      throw Object.assign(new Error(`${code}: operation not permitted`), { code });
    }
    return real.call(this, file, ...rest);
  };
  syncBuiltinESMExports();
  t.after(() => {
    fs.readFileSync = real;
    syncBuiltinESMExports();
  });
  return () => count - left;
}

test('readAlive: a single refused read (EPERM) is tried again and gives the mark', (t) => {
  const { store, clock } = storeIn(t);
  store.createRun('r1', { t: 'begin', at: clock.now(), sessionId: 's' });
  const used = failAliveReads(t, 1, 'EPERM');
  assert.equal(store.readAlive('r1'), clock.now());
  assert.equal(used(), 1);
});

test('readAlive: reads refused twice fall back to the file mtime, not null', (t) => {
  const { dataRoot, store, clock } = storeIn(t);
  store.createRun('r1', { t: 'begin', at: clock.now(), sessionId: 's' });
  const { mtimeMs } = fs.statSync(path.join(dataRoot, 'r1', 'alive'));
  failAliveReads(t, 2, 'EBUSY');
  assert.equal(store.readAlive('r1'), mtimeMs);
});

test('a live run of another session whose mark read is refused once is checked later, not cut off', (t) => {
  const { store, clock } = storeIn(t);
  // begun long ago, mark fresh: the begin time alone would cut it off
  store.createRun('r1', { t: 'begin', at: clock.now() - LIMITS.ALIVE_STALE_MS * 10, sessionId: 'other' });
  const begin = store.readLog('r1').find((e) => e.t === 'begin');
  failAliveReads(t, 1, 'EPERM');
  const action = classifyUnfinishedRun({ begin, aliveAt: store.readAlive('r1'), now: clock.now(), sessionId: 'me' });
  assert.equal(action, 'check_later');
});
