// REQ-405: phrases of the request text fill the setup values; other condition phrases are reported as not used.
// Also the end-reason rules (design 8.2) as plain functions.
import assert from 'node:assert/strict';
import test from 'node:test';

import { importExt, item } from './helpers.mjs';

const { parseRequest } = await importExt('core/request.mjs');
const { finishedOutcome, haltedByStatus, withUnstoppable } = await importExt('core/ending.mjs');

const CASES = [
  ['Coverity の警告をトリアージして', {}, []],
  ['未処理の Coverity 警告を調べて修正案と逸脱コメントを作って', {}, []],
  ['Coverity の警告をトリアージして。High だけ、10 件だけ', { impact: 'High', limit: 10 }, []],
  ['misra.yaml の条件で、最初に解析して', { filter: 'misra.yaml', initial: 'する' }, []],
  ['解析はしないで調べて', { initial: 'しない' }, []],
  ['Ｈｉｇｈ だけ、１０件', { impact: 'High', limit: 10 }, []],
  ['impact が medium の警告', { impact: 'Medium' }, []],
  ['NULL_RETURNS だけ Coverity の警告をトリアージして', {}, ['NULL_RETURNS だけ']],
  ['src/app の警告だけトリアージして', {}, ['src/app の警告だけ']],
  ['High と Medium だけ', { impact: 'High' }, ['Medium だけ']],
  ['200 件だけ', {}, ['200 件だけ']],
  ['ビルドして確かめて', {}, ['ビルドして確かめて']],
  ['前回の続きから再開して', {}, ['前回の続きから再開して']],
  ['MISRA の警告だけ、同じ原因はまとめて', {}, ['MISRA の警告だけ', '同じ原因はまとめて']],
];

for (const [request, values, unused] of CASES) {
  test(`REQ-405 「${request}」`, () => {
    const r = parseRequest(request);
    assert.equal(r.text, request);
    assert.deepEqual(r.values, values);
    assert.deepEqual(r.unused, unused);
  });
}

test('REQ-405 no text, or not a string', () => {
  assert.deepEqual(parseRequest(undefined), { text: '', values: {}, used: [], unused: [] });
  assert.deepEqual(parseRequest(42).values, {});
});

test('design 8.2 end reasons from the finished status', () => {
  const st = (items, extra = {}) => ({ finished: true, review: true, items, counts: { total: items.length }, ...extra });
  assert.deepEqual(finishedOutcome(st([item('1'), item('2', { status: 'merged', review: null })]), null), { kind: 'all_passed', detail: null, notes: [] });
  assert.deepEqual(finishedOutcome(st([]), null).notes, ['条件に合う警告はなかった']);
  assert.deepEqual(finishedOutcome(st([item('1', { review: { outcome: 'rework', findings: [] } })]), null).notes, ['要確認 1 件']);
  assert.deepEqual(finishedOutcome(st([item('1', { review: null })], { review: false }), null).kind, 'all_passed', 'review off: no review needed');
  assert.deepEqual(finishedOutcome(st([item('1')]), { unprocessed: [{ id: '9' }] }).notes, ['未処理 1 件']);
  assert.equal(haltedByStatus({ errors: [{ level: 'critical', item: '1' }] }), false, 'an item error does not halt the run');
  assert.equal(haltedByStatus({ errors: [{ level: 'critical' }] }), true);
  assert.equal(haltedByStatus({ stopped: true, finished: false }), true);
  assert.equal(haltedByStatus(null), false);
  assert.deepEqual(withUnstoppable({ kind: 'user_stopped', notes: [] }, [{}], []), { kind: 'user_stopped', notes: ['止めきれなかったエージェント 1 体'], unstoppable: true });
});
