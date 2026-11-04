// Web管理画面の画面（app.js）の振る舞い。偽物の DOM（fakedom.mjs）で動かし、描いた結果を確かめる。

import assert from 'node:assert/strict';
import test from 'node:test';

import { loadScreen, textOf } from './fakedom.mjs';

const W = 'demo-plugin:helper-a';
const V = 'demo-plugin:checker-b';
const NOT_READY = '作業エージェントの準備ができていません。下の「作業エージェントの準備」の「足りないもの」を直してから、「開始」を押してください。'
  + '開始すると、統括エージェントが準備を確かめ直します。';

function snap(over = {}) {
  return {
    version: 1,
    settings: { worker: W, verifier: V, maxIterations: 3, workerModel: '', workerEffort: '', verifierModel: '', verifierEffort: '' },
    options: {
      agents: [{ name: W, description: '手伝う' }, { name: 'demo-plugin:helper-z', description: '別の手伝い' }, { name: V, description: '確かめる' }],
      models: [],
      efforts: ['low', 'medium', 'high', 'xhigh'],
    },
    draft: null,
    run: null,
    runs: [],
    waiting: null,
    folder: '/w/project',
    ...over,
  };
}

const draftWith = (prepared) => ({ request: '表示を直す', criteria: ['完了と出る'], prepared });
const prep = (ready, worker = W) => ({ worker, ready, report: ready ? '準備：できている' : '準備：できていない\n足りないもの：\n- 設定', at: new Date().toISOString() });

async function screen(state) {
  const box = { state, posts: [] };
  const s = await loadScreen(async (method, path, body) => {
    if (method === 'GET' && path === '/api/state') return { data: box.state };
    box.posts.push({ method, path, body });
    return { data: { ok: true, runId: 'R-1' } };
  });
  return { ...s, box };
}

test('設定：選んでいる作業エージェントの準備が「できていない」なら、上に見える警告と、開始のボタンの横の注意を出す（ボタンは押せる）', async () => {
  const { $ } = await screen(snap({ draft: draftWith(prep(false)) }));
  const note = $('settings-note');
  assert.equal(textOf(note), NOT_READY);
  assert.ok(note.classList.contains('note-warn'));
  assert.ok(!note.classList.contains('sr-only') && !note.hidden, '目に見える欄に出す');
  const side = $('start-note');
  assert.equal(side.hidden, false);
  assert.match(textOf(side), /準備ができていません/);
  assert.equal(side.parent, $('btn-start').parent, '開始のボタンの横に置く');
  assert.equal($('btn-start').disabled, false);
  assert.equal($('btn-start').getAttribute('aria-describedby'), 'start-note');
  assert.equal(textOf($('prep-badge')), 'できていない');
});

test('設定：準備ができているとき・確かめていないときは、警告を出さない', async () => {
  for (const prepared of [prep(true), null]) {
    const { $ } = await screen(snap({ draft: draftWith(prepared) }));
    assert.ok(!$('settings-note').classList.contains('note-warn'));
    assert.match(textOf($('settings-note')), /内容を確かめて「開始」を押してください/);
    assert.equal($('start-note').hidden, true);
    assert.equal($('btn-start').getAttribute('aria-describedby'), null);
  }
});

test('設定：準備を確かめたのと別の作業エージェントを選ぶと、警告を消す（もどすと、また出す）', async () => {
  const { $ } = await screen(snap({ draft: draftWith(prep(false)) }));
  const select = $('f-worker');
  select.value = 'demo-plugin:helper-z';
  select.dispatch('change');
  assert.ok(!$('settings-note').classList.contains('note-warn'));
  assert.equal($('start-note').hidden, true);
  select.value = W;
  select.dispatch('change');
  assert.equal(textOf($('settings-note')), NOT_READY);
  assert.equal($('start-note').hidden, false);

  // 別の名前の付け方（プラグインの名前なし）でも、同じエージェントとみなす
  const other = await screen(snap({ draft: draftWith(prep(false, 'helper-a')) }));
  assert.equal(textOf(other.$('settings-note')), NOT_READY);
});

test('設定：準備ができていなくても「開始」は送れる', async () => {
  const { $, box, settle } = await screen(snap({ draft: draftWith(prep(false)) }));
  $('settings-form').dispatch('submit');
  await settle();
  assert.deepEqual(box.posts.map((p) => p.path), ['/api/start']);
});

// 実行のようすの「計画 → 作業 → 評価」の段：{ 名前, 状態, 印 }
function steps($) {
  return $('run-now').querySelectorAll('li.step').map((li) => ({
    name: textOf(li.querySelector('.step-name')),
    state: li.className.split(' ').find((c) => c.startsWith('is-')),
    mark: textOf(li.querySelector('.step-no')),
    cut: textOf(li.querySelector('.step-cut')),
  }));
}

const at = new Date().toISOString();
function finishedRun(iterations, outcome) {
  return {
    id: 'R-20261104-101010', status: 'finished', startedAt: at, finishedAt: at, request: '表示を直す', criteria: ['完了と出る'],
    worker: W, verifier: V, maxIterations: 3, models: {}, prepared: null, iteration: iterations.length, active: null, waiting: null,
    iterations, finish: { outcome, conclusion: '結論', retro: 'なし', at },
    activity: [{ at, who: '利用者', text: 'Web管理画面で「止める」を押しました' }],
  };
}
const plan = { text: '試すこと', at };
const done = { report: '試行 1 の報告', at };
const study = { verdicts: [{ criterion: 'C1', no: 1, text: '完了と出る', result: 'pass', reason: 'よい' }], next: 'なし', verifierReport: '', at };

test('実行のようす：止めて終わったら、記録のある段だけを済み（✓）にし、止めた段に「途中で止めた」と出す', async () => {
  const cases = [
    // [試行の記録, 結果, 段ごとの状態]
    [[{ n: 1, plan, do: null, study: null }], 'stopped', ['is-done', 'is-cut', 'is-todo']],
    [[{ n: 1, plan, do: done, study: null }], 'needs-human', ['is-done', 'is-done', 'is-cut']],
    [[], 'stopped', ['is-cut', 'is-todo', 'is-todo']],
    [[{ n: 1, plan, do: done, study }], 'success', ['is-done', 'is-done', 'is-done']],
    // 止めたのではない終わり方で、記録のない段は、済みにも「途中で止めた」にもしない
    [[{ n: 1, plan, do: null, study: null }], 'failure', ['is-done', 'is-todo', 'is-todo']],
  ];
  for (const [iterations, outcome, want] of cases) {
    const { $ } = await screen(snap({ run: finishedRun(iterations, outcome), runs: [{ id: 'R-20261104-101010', startedAt: at, outcome }] }));
    const got = steps($);
    assert.deepEqual(got.map((s) => s.state), want, `${outcome} ${JSON.stringify(iterations.map((i) => [!!i.plan, !!i.do, !!i.study]))}`);
    for (const s of got) {
      assert.equal(s.mark === '✓', s.state === 'is-done', `${s.name}：✓ は済みの段だけ`);
      assert.equal(s.cut, s.state === 'is-cut' ? '途中で止めた' : '');
    }
  }
});

test('実行のようす：止めて終わったら、いまの文と試行のカードも「止めた」と言う', async () => {
  const { $ } = await screen(snap({ run: finishedRun([{ n: 1, plan, do: null, study: null }], 'stopped') }));
  assert.match(textOf($('now-text')), /^途中で止めました。/);
  const card = textOf($('run-iters'));
  assert.match(card, /途中で止めた/);
  assert.match(card, /報告はありません。/);
  assert.ok(!card.includes('まだ報告はありません'), '終わった後は「まだ」と言わない');
});

test('くわしい記録：利用者の操作の行は「利用者」と出す', async () => {
  const { $, poll } = await screen(snap({ run: finishedRun([], 'stopped') }));
  const details = $('run-log').querySelector('details');
  details.open = true;
  details.dispatch('toggle');
  await poll();
  const who = $('run-log').querySelector('.who');
  assert.equal(textOf(who), '利用者');
  assert.ok(who.classList.contains('who-u'));
});
