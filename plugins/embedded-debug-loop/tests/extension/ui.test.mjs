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

// 選ぶ欄の選択肢（値）
const choices = (select) => select.querySelectorAll('option').map((o) => o.getAttribute('value'));

test('設定：作業エージェントの欄には選んでいる検証エージェントを、検証エージェントの欄には選んでいる作業エージェントを出さない', async () => {
  const Z = 'demo-plugin:helper-z';
  const { $, box, settle, poll } = await screen(snap({ draft: draftWith(null) }));
  assert.deepEqual(choices($('f-worker')), [W, Z]);
  assert.deepEqual(choices($('f-verifier')), [Z, V]);

  // 作業エージェントを選び直すと、検証エージェントの欄が変わる（前の作業エージェントは選べるようになる）
  $('f-worker').value = Z;
  $('f-worker').dispatch('change');
  assert.deepEqual(choices($('f-verifier')), [W, V]);
  assert.equal($('f-verifier').value, V);
  assert.deepEqual(choices($('f-worker')), [W, Z]);

  // 検証エージェントを選び直すと、作業エージェントの欄が変わる
  $('f-verifier').value = W;
  $('f-verifier').dispatch('change');
  assert.deepEqual(choices($('f-worker')), [Z, V]);
  assert.equal($('f-worker').value, Z);
  assert.deepEqual(choices($('f-verifier')), [W, V]);

  // 読み直しても、選び直した値は残る。保存すると、その値を送る
  await poll();
  assert.equal($('f-worker').value, Z);
  assert.equal($('f-verifier').value, W);
  $('btn-save').dispatch('click');
  await settle();
  const sent = box.posts.find((p) => p.path === '/api/settings').body;
  assert.deepEqual([sent.worker, sent.verifier], [Z, W]);
});

test('設定：名前の付け方（プラグインの名前の有無）が違っても、同じエージェントとみなして除く', async () => {
  const { $ } = await screen(snap({ settings: { ...snap().settings, worker: 'helper-a', verifier: 'checker-b' } }));
  // 今の値（一覧にない書き方）は最後に足して残す
  assert.deepEqual(choices($('f-worker')), [W, 'demo-plugin:helper-z', 'helper-a']);
  assert.deepEqual(choices($('f-verifier')), ['demo-plugin:helper-z', V, 'checker-b']);
});

// ---------- チャットで答えを待っているあいだ（質問・許可） ----------

function runningRun(over = {}) {
  return {
    ...finishedRun([], 'success'),
    status: 'running', finishedAt: null, finish: null, activity: [], iteration: 1,
    ...over,
  };
}
const question = { kind: 'question', text: '直したので続けますか？' };
const permission = { kind: 'permission', text: 'rm -rf out' };
const orgNode = ($, cls) => $('run-now').querySelector(`div.${cls}`);

test('実行のようす：質問の答えを待っているあいだは、段も図も試行のカードも「動いている」と言わない', async () => {
  // 試行 1 の作業の報告があり、評価はまだ。統括エージェントが ask_user で待っている
  const run = runningRun({ iterations: [{ n: 1, plan, do: done, study: null }], active: { role: 'orchestrator', since: at }, waiting: question });
  const { $ } = await screen(snap({ run, waiting: question }));
  assert.deepEqual(steps($).map((s) => s.state), ['is-done', 'is-done', 'is-todo'], '評価の段を「いま」にしない');
  assert.equal($('run-now').querySelectorAll('li[aria-current]').length, 0);
  assert.equal(textOf($('run-now').querySelector('p.cycle-wait')), '！あなたの答えを待っています');
  assert.equal(textOf($('now-text')), 'チャットで、あなたの答えを待っています。');

  const o = orgNode($, 'org-o');
  assert.ok(o.classList.contains('is-waiting') && !o.classList.contains('is-active'));
  assert.match(textOf(o), /チャットで答えを待っています/);
  assert.ok(!textOf($('run-now')).includes('動いています'), '図に「動いています」と出さない');

  const chip = $('run-iters').querySelector('summary').querySelector('span.chip');
  assert.equal(textOf(chip), '答えを待っています');
  assert.ok(chip.classList.contains('chip-wait'));
  assert.ok(!textOf($('run-iters')).includes('評価しています'));
  assert.ok(!textOf($('run-iters')).includes('確かめています'));
});

test('実行のようす：作業エージェントの許可を待っているあいだも、作業の段を「いま」にせず、「作業をしています」と言わない', async () => {
  const run = runningRun({ iterations: [{ n: 1, plan, do: null, study: null }], active: { role: 'worker', since: at }, waiting: permission });
  const { $ } = await screen(snap({ run, waiting: permission }));
  assert.deepEqual(steps($).map((s) => s.state), ['is-done', 'is-todo', 'is-todo']);
  assert.equal(textOf($('run-now').querySelector('p.cycle-wait')), '！あなたの答えを待っています');
  assert.ok(orgNode($, 'org-w').classList.contains('is-waiting'));
  assert.ok(!orgNode($, 'org-o').classList.contains('is-waiting'));
  assert.ok(!textOf($('run-now')).includes('動いています'));
  const card = textOf($('run-iters'));
  assert.match(card, /答えを待っています/);
  assert.ok(!card.includes('作業しています') && !card.includes('作業をしています'), card);
});

test('実行のようす：答えが届いたら、いまの段と「動いています」にもどる', async () => {
  const run = runningRun({ iterations: [{ n: 1, plan, do: done, study: null }], active: { role: 'orchestrator', since: at }, waiting: question });
  const { $, box, poll } = await screen(snap({ run, waiting: question }));
  box.state = snap({ run: { ...run, waiting: null }, waiting: null });
  await poll();
  assert.deepEqual(steps($).map((s) => s.state), ['is-done', 'is-done', 'is-now']);
  assert.equal($('run-now').querySelector('p.cycle-wait'), null);
  assert.ok(orgNode($, 'org-o').classList.contains('is-active'));
  assert.match(textOf(orgNode($, 'org-o')), /動いています/);
  assert.equal(textOf($('run-iters').querySelector('summary').querySelector('span.chip')), '評価しています');
});

test('実行のようす：評価せずに次の試行へ進んだ前の試行は「評価しています」と言わない', async () => {
  const run = runningRun({ iteration: 2, iterations: [{ n: 1, plan, do: done, study: null }, { n: 2, plan, do: null, study: null }], active: { role: 'worker', since: at } });
  const { $ } = await screen(snap({ run }));
  const chips = $('run-iters').querySelectorAll('summary').map((s) => textOf(s.querySelector('span.chip')));
  assert.deepEqual(chips, ['作業しています', '評価なし']);
});

// ---------- 画面の読み上げの欄（#live） ----------

test('読み上げ：質問が終わったら、「チャットで質問に答えてください」を読み上げの欄に残さない', async () => {
  // 実行の前（下書き）と、終わった後。画面を開いた後に、質問が始まる
  for (const base of [{ draft: draftWith(null) }, { run: finishedRun([{ n: 1, plan, do: done, study }], 'success') }]) {
    const { $, box, poll } = await screen(snap(base));
    await poll();
    box.state = snap({ ...base, waiting: question });
    await poll();
    await poll(); // 読み上げの欄に書く
    assert.equal(textOf($('live')), 'チャットで質問に答えてください');
    box.state = snap({ ...base, waiting: null });
    await poll();
    assert.equal(textOf($('live')), '');
    await poll();
    assert.equal(textOf($('live')), '', '後から書き戻さない');
  }
});

test('読み上げ：答える前に質問が終わっても、後から質問の文を書かない。次の質問は、また読み上げる', async () => {
  const base = { draft: draftWith(null) };
  const { $, box, poll } = await screen(snap({ ...base, waiting: question }));
  box.state = snap({ ...base, waiting: null });
  await poll(); // 読み上げの予約と、次の読み直しが、同じときに動く
  await poll();
  assert.equal(textOf($('live')), '');
  box.state = snap({ ...base, waiting: permission });
  await poll();
  await poll();
  assert.equal(textOf($('live')), 'チャットで許可を求めています');
});

test('読み上げ：実行中に答えが届いたら、読み上げの欄は新しい「いま」の文になる', async () => {
  const run = runningRun({ iterations: [{ n: 1, plan, do: done, study: null }], active: { role: 'orchestrator', since: at }, waiting: question });
  const { $, box, poll } = await screen(snap({ run, waiting: question }));
  await poll();
  assert.equal(textOf($('live')), 'いま：チャットで、あなたの答えを待っています。');
  box.state = snap({ run: { ...run, waiting: null }, waiting: null });
  await poll();
  await poll();
  assert.equal(textOf($('live')), 'いま：統括エージェントが試行 1 の結果を評価しています');
});

// ---------- くわしい記録のコマンドの行 ----------

test('くわしい記録：長いコマンドの行は、頭と終わりを残して真ん中を省き、全文を title に残す', async () => {
  const head = `python3 /tmp/work/${'a'.repeat(80)}`;
  const tail = `${'b'.repeat(40)}/scripts/run.py run --file out/app.bin --dest /opt/app`;
  const command = `${head}/middle/${tail}`;
  const long = `コマンドを実行：${command}（失敗）`;
  const run = finishedRun([], 'success');
  const t = (s) => new Date(Date.now() - s * 1000).toISOString();
  run.activity = [
    { at: t(1), who: '作業', text: long },
    { at: t(2), who: '作業', text: 'コマンドを実行：python3 run.py run' },
    { at: t(3), who: '作業', text: `ファイルを読む：${'/x'.repeat(100)}` },
  ];
  const { $, poll } = await screen(snap({ run }));
  const details = $('run-log').querySelector('details');
  details.open = true;
  details.dispatch('toggle');
  await poll();
  const [first, second, third] = $('run-log').querySelectorAll('span.act-text');
  assert.equal(textOf(first), `コマンドを実行：${command.slice(0, 70)}…${command.slice(-70)}（失敗）`);
  assert.ok(textOf(first).endsWith('--file out/app.bin --dest /opt/app（失敗）'), '終わり（引数）が見える');
  assert.equal(first.getAttribute('title'), long);
  // 短いコマンドと、コマンドでない行は、そのまま（title も付けない）
  assert.equal(textOf(second), 'コマンドを実行：python3 run.py run');
  assert.equal(second.getAttribute('title'), null);
  assert.equal(textOf(third), `ファイルを読む：${'/x'.repeat(100)}`);
  assert.equal(third.getAttribute('title'), null);
});

test('くわしい記録：説明のあるコマンドの行は、説明をそのまま出し（省かない）、コマンドの全文を title に出す', async () => {
  const command = `python3 /tmp/work/${'a'.repeat(200)}/scripts/run.py run --workdir /w/project`;
  const description = `Run the tool ${'x'.repeat(150)}`;
  const run = finishedRun([], 'success');
  const t = (s) => new Date(Date.now() - s * 1000).toISOString();
  run.activity = [
    { at: t(1), who: '作業', text: `コマンドを実行：${description}（失敗）`, command },
    { at: t(2), who: '作業', text: 'コマンドを実行：一覧を見る', command: 'ls -la' },
  ];
  const { $, poll } = await screen(snap({ run }));
  const details = $('run-log').querySelector('details');
  details.open = true;
  details.dispatch('toggle');
  await poll();
  const [first, second] = $('run-log').querySelectorAll('span.act-text');
  assert.equal(textOf(first), `コマンドを実行：${description}（失敗）`);
  assert.equal(first.getAttribute('title'), command);
  assert.equal(textOf(second), 'コマンドを実行：一覧を見る');
  assert.equal(second.getAttribute('title'), 'ls -la');
});

// ---------- 結果の振り返り ----------

test('結果：振り返りがないとき、止めて終わった実行・途中で終えた実行では「まだ」と言わない', async () => {
  const retroOf = async (run) => {
    const { $ } = await screen(snap({ run, runs: [{ id: run.id, startedAt: at, outcome: run.finish?.outcome ?? null }] }));
    return textOf($('h-retro').parent.querySelector('p'));
  };
  const noRetro = (outcome) => {
    const run = finishedRun([{ n: 1, plan, do: null, study: null }], outcome);
    run.finish.retro = '';
    return run;
  };
  assert.equal(await retroOf(noRetro('stopped')), '止めたため、振り返りはありません。');
  assert.equal(await retroOf(noRetro('needs-human')), '途中で終えたため、振り返りはありません。');
  assert.equal(await retroOf(finishedRun([], 'stopped')), 'なし', '振り返りがあれば、そのまま出す');
  const running = { ...noRetro('stopped'), status: 'running', finishedAt: null, finish: null };
  assert.equal(await retroOf(running), '振り返りはまだありません。', '実行中は「まだ」');
});
