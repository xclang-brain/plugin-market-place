// 記録のツール：開く → 準備 → 開始 → 計画 → 評価 → 終わり と、Issue。断るときの文も確かめる。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { agentOf, matchCriterion } from '../../com.github.copilot/extensions/embedded-debug-loop/core/store.mjs';
import { CWD, LEAD, VERIFIER, WORKER, startRun, startWith, verdictsOf, verifierReplied } from './helpers.mjs';

test('ツールは6つで、ツール検索の裏に回さない。Issue のほかは確認なしで動く', async (t) => {
  const { fake } = await startWith(t);
  const names = fake.config.tools.map((x) => x.name).sort();
  assert.deepEqual(names, ['open', 'prepare', 'plan', 'study', 'finish', 'issue'].map((n) => `embedded-debug-loop_${n}`).sort());
  for (const tool of fake.config.tools) {
    assert.equal(tool.defer, 'never');
    // Issue は公開の場に書くので、Copilot CLI の許可の確認を通す
    assert.equal(tool.skipPermission, tool.name !== 'embedded-debug-loop_issue', tool.name);
    assert.equal(typeof tool.description, 'string');
    assert.equal(tool.parameters.type, 'object');
  }
});

test('open：URL と今の設定を返し、次に準備の確認を頼むよう伝える', async (t) => {
  const { fake, app } = await startWith(t);
  const r = await fake.call('open', { request: '表示を直す', criteria: ['完了と出る'] });
  assert.ok(r.ok);
  assert.match(r.text, /URL：http:\/\/127\.0\.0\.1:\d+\/\?k=[\w-]{24}/);
  assert.ok(r.text.includes(`作業エージェント：${WORKER}`));
  assert.ok(r.text.includes(`検証エージェント：${VERIFIER}`));
  assert.ok(r.text.includes('繰り返しの上限：3 回'));
  assert.ok(r.text.includes('モデル：既定'));
  assert.ok(r.text.includes(`次に、作業エージェント「${WORKER}」に、準備の確認だけを頼んでください（作業はさせない）。返事を embedded-debug-loop_prepare で記録してください。`));
  const s = app.store.snapshot();
  assert.deepEqual(s.draft, { request: '表示を直す', criteria: ['完了と出る'], prepared: null });
});

test('open：頼む作業が空なら断る。実行中なら URL を添えて断る', async (t) => {
  const { fake } = await startRun(t);
  const empty = await fake.call('open', { request: '  ' });
  assert.equal(empty.ok, false);
  const busy = await fake.call('open', { request: '別の作業' });
  assert.equal(busy.ok, false);
  assert.match(busy.text, /^実行中です。.*URL：http:\/\/127\.0\.0\.1/);
});

test('open：worker（作業エージェントの名前）は任意で、説明は日本語', async (t) => {
  const { fake } = await startWith(t);
  const { parameters } = fake.tool('open');
  assert.equal(parameters.properties.worker.type, 'string');
  assert.match(parameters.properties.worker.description, /作業エージェントの名前/);
  assert.match(parameters.properties.worker.description, /この会話のあいだ/);
  assert.ok(!parameters.required.includes('worker'));
});

test('open：worker を渡すと、この会話のあいだ、その名前で返事と準備の確認の頼みを書く（名前は保存しない）', async (t) => {
  const agents = [
    { id: WORKER, name: 'helper-a' },
    { id: LEAD, name: 'lead-c' },
    { id: VERIFIER, name: 'checker-b' },
    { id: 'host-sim-worker', name: 'host-sim-worker' },
  ];
  const { fake, app, dataDir } = await startWith(t, { fake: { agents } });
  const r = await fake.call('open', { request: '表示を直す', criteria: ['完了と出る'], worker: ' host-sim-worker ' });
  assert.ok(r.ok);
  const lines = r.text.split('\n');
  assert.match(lines[0], /^Web管理画面を開きました。URL：/);
  assert.equal(lines[1], '作業エージェントを「host-sim-worker」にしました（この会話のあいだ使います）。');
  assert.ok(lines.includes('- 作業エージェント：host-sim-worker'));
  assert.ok(lines.includes(`- 検証エージェント：${VERIFIER}`), 'ほかの設定は変えない');
  assert.equal(lines[lines.length - 1], '次に、作業エージェント「host-sim-worker」に、準備の確認だけを頼んでください（作業はさせない）。返事を embedded-debug-loop_prepare で記録してください。');
  assert.ok(!r.text.includes(WORKER), '既定の作業エージェントの名前は出さない');
  assert.equal(app.store.snapshot().settings.worker, 'host-sim-worker');
  // 部品を替えたとき前の名前が残らないように、使うエージェントの名前はファイルに保存しない
  const file = path.join(dataDir, 'settings.json');
  const saved = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : { folders: {} };
  assert.equal(saved.folders[CWD]?.worker, undefined);
  assert.equal(saved.folders[CWD]?.verifier, undefined);

  // 同じ会話で次に開くときは、同じ名前を使う（worker を渡さなくても）
  const again = await fake.call('open', { request: 'もう一度' });
  assert.ok(again.text.includes('次に、作業エージェント「host-sim-worker」に'));
  assert.ok(!again.text.includes('にしました'));
});

test('open：worker の名前は、エージェントの一覧の id にする。一覧にない名前・2つ以上に当たる名前は、使えるエージェントを示して断る', async (t) => {
  const agents = [
    { id: WORKER, name: 'helper-a', displayName: 'helper-a', source: 'plugin' },
    { id: VERIFIER, name: 'checker-b', displayName: 'checker-b', source: 'plugin' },
    { id: 'p1:twin', name: 'twin', displayName: 'twin', source: 'plugin' },
    { id: 'p2:twin', name: 'twin', displayName: 'twin', source: 'plugin' },
  ];
  const { fake, app } = await startWith(t, { fake: { agents } });
  const r = await fake.call('open', { request: '作業', worker: 'helper-a' });
  assert.ok(r.text.includes(`作業エージェントを「${WORKER}」にしました`));
  assert.equal(app.store.snapshot().settings.worker, WORKER);
  const draft = app.store.snapshot().draft;
  for (const [named, head] of [
    ['twin', '作業エージェント「twin」に当たるエージェントが2つ以上あります。'],
    ['unknown-agent', '作業エージェント「unknown-agent」に当たるエージェントがありません。'],
  ]) {
    const ng = await fake.call('open', { request: '別の作業', worker: named });
    assert.equal(ng.ok, false, named);
    assert.ok(ng.text.startsWith(head), ng.text);
    assert.ok(ng.text.includes(`使えるエージェント：「${WORKER}」（/agent では「helper-a · plugin」）、「${VERIFIER}」（/agent では「checker-b · plugin」）、「p1:twin」（/agent では「twin · plugin」）、「p2:twin」`), ng.text);
    assert.ok(ng.text.includes('ask_user で利用者に聞いてから、もう一度 embedded-debug-loop_open を呼んでください'));
    assert.equal(app.store.snapshot().settings.worker, WORKER, '断ったときは変えない');
    assert.deepEqual(app.store.snapshot().draft, draft, '断ったときは下書きを変えない');
  }
});

test('agentOf：id が同じものを先に選ぶ。なければ name か displayName が同じものが1つだけのとき', () => {
  const list = [
    { id: 'solo', name: 'solo', displayName: 'solo', source: 'project' },
    { id: 'p:solo', name: 'solo', displayName: 'solo', source: 'plugin' },
    { id: 'p:x', name: 'x', displayName: 'エックス', source: 'plugin' },
  ];
  assert.equal(agentOf('solo', list, 'A').id, 'solo', 'id と同じものがあれば、それ');
  assert.equal(agentOf('p:solo', list, 'A').id, 'p:solo');
  assert.equal(agentOf('x', list, 'A').id, 'p:x');
  assert.equal(agentOf('エックス', list, 'A').id, 'p:x');
  assert.throws(() => agentOf('q:x', list, 'A'), (e) => e.status === 400 && /^A「q:x」に当たるエージェントがありません。/.test(e.message));
  assert.throws(() => agentOf('x', [], 'A'), (e) => e.message.endsWith('使えるエージェント：なし'));
});

test('open：実行中は worker を渡しても、設定を変えずに断る', async (t) => {
  const { fake, app } = await startRun(t);
  const busy = await fake.call('open', { request: '別の作業', worker: 'host-sim-worker' });
  assert.equal(busy.ok, false);
  assert.equal(app.store.snapshot().settings.worker, WORKER);
});

test('open：作業エージェントが決まっていなければ、画面で選んでもらうよう伝える', async (t) => {
  const { fake } = await startWith(t, { defaults: { worker: '' } });
  const r = await fake.call('open', { request: '作業' });
  assert.equal(r.ok, true);
  assert.match(r.text, /作業エージェントが決まっていません/);
  assert.ok(!r.text.includes('「」'));
});

test('prepare：返事を下書きに残し、開始を押してもらうよう伝える', async (t) => {
  const { fake, app } = await startWith(t);
  assert.equal((await fake.call('prepare', { worker: WORKER, ready: true, report: 'x' })).ok, false, 'open の前は断る');
  await fake.call('open', { request: '作業' });
  const ok = await fake.call('prepare', { worker: WORKER, ready: true, report: '準備：できている\n設定の要約' });
  assert.ok(ok.ok);
  assert.match(ok.text, /『開始』を押すよう頼んで、手番を終えてください。開始が押されると、このチャットに開始の知らせが届きます。/);
  assert.ok(!ok.text.includes('準備ができていない'));
  const p = app.store.snapshot().draft.prepared;
  assert.equal(p.worker, WORKER);
  assert.equal(p.ready, true);
  assert.equal(p.report, '準備：できている\n設定の要約');
  assert.ok(p.at);
  const ng = await fake.call('prepare', { worker: WORKER, ready: false, report: '準備：できていない\n足りないもの' });
  assert.equal(app.store.snapshot().draft.prepared.ready, false);
  assert.match(ng.text, /作業エージェントの準備ができていません。/);
  assert.match(ng.text, /「足りないもの」の部分。書き換えずに、そのまま引用します。/);
  assert.match(ng.text, /直し方。返事に書かれた手順を、省かずに/);
  assert.match(ng.text, /直してから、Web管理画面（http:\/\/127\.0\.0\.1:\d+\/\?k=[^）]+）で『開始』を押すこと。開始すると、統括エージェントが準備を確かめ直すこと。/);
  assert.match(ng.text, /いますぐ『開始』を押すようには頼まないでください。/);
  assert.ok(!ng.text.includes('『開始』を押すよう頼んで'), '準備ができていないのに、すぐ開始を押すよう頼ませない');
  assert.ok(ng.text.includes(`- 準備を確かめた作業エージェント：「${WORKER}」（/agent では「helper-a · plugin」）。`), '作業エージェントは自分の名前を書かないので、名前と /agent での見え方は、こちらで添えさせる');
  assert.ok(!ng.text.includes('この作業エージェント'), '作業エージェントの返事の言い回しに頼らない');
});

test('prepare（実行中）：準備ができていなければ、足りないものをそのまま引用して伝え、ask_user で聞くよう伝える', async (t) => {
  const { fake } = await startRun(t, { prepare: false });
  const ok = await fake.call('prepare', { worker: WORKER, ready: true, report: '準備：できている' });
  assert.match(ok.text, /試行 1 の計画を立て/);
  const ng = await fake.call('prepare', { worker: WORKER, ready: false, report: '準備：できていない' });
  assert.equal(ng.text, `記録しました。準備ができていません。作業エージェントの返事の「足りないもの」の部分を、書き換えずにそのまま引用し、準備を確かめた作業エージェント「${WORKER}」（/agent では「helper-a · plugin」）と合わせて利用者に伝え、どうするかを ask_user で聞いてください。`);
  assert.ok(!ng.text.includes('この作業エージェント'));
  assert.ok(!ng.text.includes('開始'));
});

test('開始：チャットに開始の知らせを送る（準備ずみなら、確認の頼みは付けない）', async (t) => {
  const { fake, app } = await startRun(t);
  assert.equal(fake.sent.length, 1);
  const lines = fake.sent[0].split('\n');
  const run = app.store.snapshot().run;
  assert.deepEqual(lines, [
    '［Web管理画面からの開始の知らせ］',
    `実行の番号：${run.id}`,
    '頼まれた作業：表示の不具合を直す',
    '合格の条件：',
    '- C1：画面に「完了」と出る',
    '- C2：テストがすべて通る',
    '繰り返しの上限：3 回',
    `作業エージェント：${WORKER}`,
    `検証エージェント：${VERIFIER}`,
    '統括エージェントの手順に沿って、試行 1 の計画から始めてください。',
  ]);
  assert.match(run.id, /^R-\d{8}-\d{6}$/);
  assert.equal(run.status, 'running');
  assert.equal(run.prepared.ready, true);
  assert.equal(app.store.snapshot().draft, null);
});

test('開始：チャットで別のエージェントが選ばれていれば、統括エージェントを選んでから知らせを送る', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  fake.current = { name: 'helper-a', id: WORKER };
  await app.control.start({});
  assert.deepEqual(fake.selected, [LEAD]);
  assert.equal(fake.sent.length, 1);
});

test('開始：統括エージェントがもう選ばれていれば、選び直さない', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  fake.current = { name: 'lead-c', id: LEAD };
  await app.control.start({});
  assert.equal(fake.selected, undefined);
  assert.equal(fake.sent.length, 1);
});

test('開始：準備していない・作業エージェントを変えたときは、準備の確認を頼む行を付ける', async (t) => {
  const a = await startRun(t, { prepare: false });
  assert.ok(a.fake.sent[0].includes('作業エージェントの準備の確認がまだです。最初に準備の確認を頼んでから、試行を始めてください。'));

  const agents = [{ id: WORKER, name: 'helper-a' }, { id: VERIFIER, name: 'checker-b' }, { id: 'other-plugin:another', name: 'another' }];
  const b = await startWith(t, { fake: { agents } });
  await b.fake.call('open', { request: '作業', criteria: ['条件'] });
  await b.fake.call('prepare', { worker: WORKER, ready: true, report: '準備：できている' });
  await b.app.control.start({ worker: 'other-plugin:another' });
  assert.ok(b.fake.sent[0].includes('作業エージェント：other-plugin:another'));
  assert.ok(b.fake.sent[0].includes('作業エージェントの準備の確認がまだです。'));
});

test('開始：前の準備の確認が「できていない」なら、頼み直す行を付ける', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  await fake.call('prepare', { worker: WORKER, ready: false, report: '準備：できていない' });
  await app.control.start({});
  assert.ok(fake.sent[0].includes('前の準備の確認では「できていない」でした。最初に準備の確認を頼み直してから、試行を始めてください。'));
  assert.ok(!fake.sent[0].includes('準備の確認がまだ'));
});

test('prepare：worker は一覧の id にして記録する（開始のとき、同じ作業エージェントなら準備ずみ）。一覧にない名前は断る', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  const ng = await fake.call('prepare', { worker: 'gone:worker', ready: true, report: '準備：できている' });
  assert.equal(ng.ok, false);
  assert.match(ng.text, /^作業エージェント「gone:worker」に当たるエージェントがありません。使えるエージェント：/);
  assert.equal(app.store.snapshot().draft.prepared, null);
  await fake.call('prepare', { worker: 'helper-a', ready: true, report: '準備：できている' });
  assert.equal(app.store.snapshot().draft.prepared.worker, WORKER);
  await app.control.start({});
  assert.ok(!fake.sent[0].includes('準備の確認がまだ'));
});

test('開始：入力の誤りは 400 で、画面に出す日本語の理由を返す', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業' });
  const cases = [
    [{}, /合格の条件を1つ以上/],
    [{ criteria: ['c'], request: ' ' }, /頼む作業を書いてください/],
    [{ criteria: ['c'], maxIterations: 0 }, /1 から 20 までの整数/],
    [{ criteria: ['c'], maxIterations: 21 }, /1 から 20 までの整数/],
    [{ criteria: ['c'], maxIterations: 2.5 }, /1 から 20 までの整数/],
    [{ criteria: ['c'], worker: '' }, /作業エージェントを選んでください/],
    [{ criteria: ['c'], workerEffort: 'max' }, /考える深さ/],
  ];
  for (const [body, re] of cases) {
    await assert.rejects(app.control.start(body), (e) => e.status === 400 && re.test(e.message), JSON.stringify(body));
  }
  assert.equal(fake.sent.length, 0);
  assert.equal(app.store.run, null);
});

test('開始：モデルと考える深さを、セッションの子のエージェントの設定に書く。毎回、利用者の設定から作り直し、前の実行の分は残さない', async (t) => {
  const keep = { 'someone-else': { model: 'm' } };
  const agents = [{ id: WORKER }, { id: VERIFIER }, { id: 'other:w2' }];
  const { fake, app } = await startWith(t, { fake: { agents, subagents: { maxDepth: 2, agents: { ...keep, [VERIFIER]: { model: 'mine' } } } } });
  const runWith = async (body) => {
    fake.updates.length = 0;
    await fake.call('open', { request: '作業', criteria: ['条件'] });
    await app.control.start(body);
    await fake.call('finish', { outcome: 'needs-human', conclusion: '済み', retro: 'なし' });
  };
  await runWith({ workerModel: 'model-x', workerEffort: 'high', verifierEffort: 'low' });
  assert.deepEqual(fake.updates, [null, { maxDepth: 2, agents: { ...keep, [WORKER]: { model: 'model-x', effortLevel: 'high' }, [VERIFIER]: { effortLevel: 'low' } } }]);

  // 作業エージェントを替えた：前の作業エージェントの分は残さない。「既定」の検証エージェントは、利用者の設定にあっても外す
  await runWith({ worker: 'other:w2', workerModel: 'model-y', workerEffort: '', verifierEffort: '既定' });
  assert.deepEqual(fake.updates, [null, { maxDepth: 2, agents: { ...keep, 'other:w2': { model: 'model-y' } } }]);

  // 上書きが要らなければ、消したままにする（利用者の設定がそのまま効く）
  fake.subagents = { maxDepth: 2, agents: { ...keep } };
  await runWith({ workerModel: '' });
  assert.deepEqual(fake.updates, [null]);
  assert.equal(fake.override, null);
});

test('開始：知らせを送れなければ 500 を返し、下書きに戻す', async (t) => {
  const { fake, app, dataDir } = await startWith(t, { fake: { sendFails: true } });
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  await assert.rejects(app.control.start({}), (e) => e.status === 500 && /開始の知らせを送れませんでした/.test(e.message));
  const s = app.store.snapshot();
  assert.equal(s.run, null);
  assert.equal(s.draft.request, '作業');
  assert.deepEqual(s.runs, []);
  const runsDir = path.join(dataDir, 'runs');
  assert.deepEqual(fs.existsSync(runsDir) ? fs.readdirSync(runsDir) : [], []);
});

test('計画：実行中だけ。番号は次の番号か、作業の前なら同じ番号で置き換え。上限を超えると断る', async (t) => {
  const before = await startWith(t);
  await before.fake.call('open', { request: '作業' });
  const notYet = await before.fake.call('plan', { iteration: 1, plan: 'p' });
  assert.equal(notYet.ok, false);
  assert.match(notYet.text, /実行中ではありません/);

  const { fake, app } = await startRun(t);
  assert.equal((await fake.call('plan', { iteration: 2, plan: 'p' })).ok, false);
  assert.equal((await fake.call('plan', { iteration: 1, plan: '' })).ok, false);
  const ok = await fake.call('plan', { iteration: 1, plan: '最初の計画' });
  assert.equal(ok.text, `記録しました。作業エージェント「${WORKER}」に task で頼んでください。`);
  assert.ok((await fake.call('plan', { iteration: 1, plan: '直した計画' })).ok, '作業の前なら置き換えられる');
  let run = app.store.snapshot().run;
  assert.equal(run.iterations.length, 1);
  assert.equal(run.iterations[0].plan.text, '直した計画');
  fake.subagentStop({ agentType: WORKER, agentId: 'a1', response: '報告' });
  const again = await fake.call('plan', { iteration: 1, plan: 'もう一度' });
  assert.equal(again.ok, false, '作業の後は、同じ番号で置き換えられない');
  assert.match(again.text, /次の試行は 2 です/);
  await fake.call('plan', { iteration: 2, plan: '2回目' });
  await fake.call('plan', { iteration: 3, plan: '3回目' });
  const over = await fake.call('plan', { iteration: 4, plan: '4回目' });
  assert.equal(over.text, '上限（3 回）に達しています。embedded-debug-loop_finish で終えてください。');
  run = app.store.snapshot().run;
  assert.equal(run.iteration, 3);
});

test('評価：合否を記録し、次にすることを返す', async (t) => {
  const { fake, app } = await startRun(t);
  assert.match((await fake.call('study', { iteration: 1, verdicts: [], next: '' })).text, /先に試行の計画/);
  await fake.call('plan', { iteration: 1, plan: 'p1' });
  const bad = await fake.call('study', { iteration: 1, verdicts: [{ criterion: 'C1', result: 'ok', reason: 'r' }], next: 'n' });
  assert.equal(bad.ok, false);
  assert.equal((await fake.call('study', { iteration: 2, verdicts: verdictsOf('pass', 'pass'), next: 'n' })).ok, false);
  // 条件の評価が足りない・条件にない評価があるときは断る
  const short = await fake.call('study', { iteration: 1, verdicts: [{ criterion: 'C1', result: 'fail', reason: 'r' }], next: 'n' });
  assert.equal(short.text, '評価のない条件があります：C2。すべての条件の合否を記録してください（判断できなければ unknown）。');
  const extra = await fake.call('study', { iteration: 1, verdicts: [...verdictsOf('fail', 'fail'), { criterion: 'C3', result: 'fail', reason: 'r' }], next: 'n' });
  assert.match(extra.text, /^合格の条件にない評価があります（C3）。criterion には C1〜C2 を入れてください。$/);
  // 検証エージェントの評価がなければ、合格は記録できない
  const noVerifier = await fake.call('study', { iteration: 1, verdicts: verdictsOf('pass', 'fail'), next: 'n' });
  assert.equal(noVerifier.ok, false);
  assert.match(noVerifier.text, /検証エージェントの評価がないので、合格（pass）は記録できません/);
  assert.equal(app.store.snapshot().run.iterations[0].study, null);
  verifierReplied(fake);
  const mixed = await fake.call('study', {
    iteration: 1,
    verdicts: [{ criterion: 'C1', result: 'pass', reason: '出た' }, { criterion: 'C2', result: 'fail', reason: '1件落ちた' }],
    next: 'テストの落ちた所を直す',
  });
  assert.match(mixed.text, /次は試行 2 です。.*embedded-debug-loop_plan で記録してください。/);
  const st = app.store.snapshot().run.iterations[0].study;
  assert.deepEqual(st.verdicts.map((v) => v.result), ['pass', 'fail']);
  assert.equal(st.next, 'テストの落ちた所を直す');

  await fake.call('plan', { iteration: 2, plan: 'p2' });
  assert.equal((await fake.call('study', { iteration: 2, verdicts: verdictsOf('pass', 'pass'), next: '' })).ok, false, '試行ごとに検証が要る');
  verifierReplied(fake, 'v2');
  const pass = await fake.call('study', { iteration: 2, verdicts: verdictsOf('pass', 'pass'), next: '' });
  assert.equal(pass.text, 'すべての条件が合格です。embedded-debug-loop_finish（outcome は success）で終えてください。');

  await fake.call('plan', { iteration: 3, plan: 'p3' });
  const last = await fake.call('study', { iteration: 3, verdicts: verdictsOf('unknown', 'unknown'), next: '' });
  assert.equal(last.text, '上限（3 回）に達しました。embedded-debug-loop_finish（outcome は failure）で終えてください。');
});

test('終わり：結論を記録して終える。記録のファイルと一覧にも残る', async (t) => {
  const { fake, app, dataDir } = await startRun(t);
  assert.equal((await fake.call('finish', { outcome: 'great', conclusion: 'x', retro: '' })).ok, false);
  assert.equal((await fake.call('finish', { outcome: 'success', conclusion: '', retro: '' })).ok, false);
  // 最後の評価ですべて合格していなければ、success では終えられない
  const notYet = await fake.call('finish', { outcome: 'success', conclusion: '直った', retro: '' });
  assert.equal(notYet.text, '最後の評価で、すべての条件が合格していません。outcome は failure か needs-human にしてください。');
  const r = await fake.call('finish', { outcome: 'needs-human', conclusion: '人の判断が要る', retro: '頼み方を短く' });
  assert.ok(r.ok);
  assert.match(r.text, /結果の画面：http:\/\/127\.0\.0\.1:\d+\/\?k=.*#result\n利用者に結論を短く伝えてください。/);
  const s = app.store.snapshot();
  assert.equal(s.run.status, 'finished');
  assert.deepEqual({ ...s.run.finish, at: undefined }, { outcome: 'needs-human', conclusion: '人の判断が要る', retro: '頼み方を短く', at: undefined });
  assert.equal(s.runs[0].outcome, 'needs-human');
  const saved = JSON.parse(fs.readFileSync(path.join(dataDir, 'runs', s.run.id, 'run.json'), 'utf8'));
  assert.equal(saved.finish.outcome, 'needs-human');
  const events = fs.readFileSync(path.join(dataDir, 'runs', s.run.id, 'events.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  assert.ok(events.some((e) => e.type === 'start'));
  assert.ok(events.some((e) => e.type === 'finish'));
  assert.equal((await fake.call('finish', { outcome: 'success', conclusion: 'もう一度' })).ok, false, '2回は終えられない');
  assert.equal((await fake.call('plan', { iteration: 1, plan: 'p' })).ok, false);
});

test('Issue：gh が URL を出したときだけ「作りました」と返す', async (t) => {
  const calls = [];
  let reply = { code: 0, stdout: 'https://github.com/owner/repo/issues/12\n', stderr: '' };
  const { fake } = await startWith(t, { runCommand: async (cmd, args, timeout) => (calls.push({ cmd, args, timeout }), reply) });
  const ok = await fake.call('issue', { title: '題', body: '本文' });
  assert.ok(ok.ok);
  assert.match(ok.text, /Issue を作りました：https:\/\/github\.com\/owner\/repo\/issues\/12/);
  assert.deepEqual(calls[0], { cmd: 'gh', args: ['issue', 'create', '--repo', 'owner/repo', '--title', '題', '--body', '本文'], timeout: 30000 });

  reply = { code: 0, stdout: '', stderr: '', missing: true };
  const missing = await fake.call('issue', { title: '題', body: '本文' });
  assert.equal(missing.ok, false);
  assert.match(missing.text, /gh（GitHub のコマンド）が見つからない/);
  assert.match(missing.text, /https:\/\/github\.com\/owner\/repo\/issues\/new/);
  assert.match(missing.text, /題：題\n本文：\n本文/);

  reply = { code: 1, stdout: '', stderr: 'HTTP 401: Bad credentials' };
  const denied = await fake.call('issue', { title: '題', body: '本文' });
  assert.equal(denied.ok, false);
  assert.match(denied.text, /Bad credentials/);
  assert.equal((await fake.call('issue', { title: '', body: '本文' })).ok, false);
});

test('Issue：終わった実行の記録に、題・本文・送り先・URL を残す', async (t) => {
  const runCommand = async () => ({ code: 0, stdout: 'https://github.com/owner/repo/issues/7\n', stderr: '' });
  const { fake, app, dataDir } = await startRun(t, { runCommand });
  await fake.call('finish', { outcome: 'needs-human', conclusion: '人の判断が要る', retro: '頼み方を短く' });
  assert.ok((await fake.call('issue', { title: '題', body: '本文' })).ok);
  const id = app.store.snapshot().run.id;
  const events = fs.readFileSync(path.join(dataDir, 'runs', id, 'events.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const issue = events.find((e) => e.type === 'issue');
  assert.deepEqual({ ...issue, at: undefined }, { at: undefined, type: 'issue', title: '題', body: '本文', url: 'https://github.com/owner/repo/issues/7', repo: 'owner/repo' });
});

test('準備：準備ができているかは、返事の1行目で決める（1行目にその形がないときだけ ready を使う）', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['条件'] });
  const cases = [
    [{ ready: false, report: '準備：できている\n要約' }, true],
    [{ ready: true, report: '\n準備：できていない\n足りないもの' }, false],
    [{ ready: true, report: '準備: できていない' }, false],
    [{ ready: true, report: '要約だけ' }, true],
    [{ ready: false, report: '要約だけ' }, false],
  ];
  for (const [args, ready] of cases) {
    await fake.call('prepare', { worker: WORKER, ...args });
    assert.equal(app.store.snapshot().draft.prepared.ready, ready, JSON.stringify(args));
  }
});

test('ツールの処理が思わず失敗しても、例外を投げずに失敗の結果を返す', async (t) => {
  const { fake } = await startWith(t, { runCommand: async () => { throw new Error('壊れた'); } });
  const r = await fake.call('issue', { title: '題', body: '本文' });
  assert.equal(r.ok, false);
  assert.match(r.text, /思わぬ失敗/);
});

test('合格の条件の頭の番号（C1：など）は取り除く：open・設定の保存・開始のどこから入っても', async (t) => {
  const { fake, app } = await startWith(t);
  await fake.call('open', { request: '作業', criteria: ['C1：画面に「完了」と出る', 'C2: テストが通る', '- C3：警告が出ない', 'Ｃ４：全角の番号', 'Cで始まる条件'] });
  assert.deepEqual(app.store.snapshot().draft.criteria, ['画面に「完了」と出る', 'テストが通る', '警告が出ない', '全角の番号', 'Cで始まる条件']);

  await app.control.saveSettings({ criteria: 'C1：一つ目\nC2:二つ目\n' });
  assert.deepEqual(app.store.snapshot().draft.criteria, ['一つ目', '二つ目']);

  await app.control.start({ criteria: ['C1：開始のときの条件'] });
  assert.deepEqual(app.store.snapshot().run.criteria, ['開始のときの条件']);
  assert.ok(fake.sent[0].split('\n').includes('- C1：開始のときの条件'), '開始の知らせで番号が二重にならない');
  assert.ok(!fake.sent[0].includes('C1：C1'));
});

test('評価の criterion：番号でも文でも、実行の合格の条件の番号と文に結び付ける', async (t) => {
  const { fake, app } = await startRun(t); // 条件は「画面に「完了」と出る」「テストがすべて通る」
  await fake.call('plan', { iteration: 1, plan: 'p1' });
  verifierReplied(fake);
  const other = await fake.call('study', { iteration: 1, verdicts: [...verdictsOf('pass', 'fail'), { criterion: 'ほかの条件', result: 'unknown', reason: 'r' }], next: 'n' });
  assert.match(other.text, /^合格の条件にない評価があります（ほかの条件）。/);
  await fake.call('study', {
    iteration: 1,
    verdicts: [
      { criterion: 'C1', result: 'pass', reason: '出た' },
      { criterion: 'テストがすべて通る', result: 'fail', reason: '1件落ちた' },
      { criterion: 'C2：言い方を変えた条件', result: 'fail', reason: 'r' },
    ],
    next: 'n',
  });
  const v = app.store.snapshot().run.iterations[0].study.verdicts;
  assert.deepEqual(v.map((x) => [x.criterion, x.no, x.text]), [
    ['C1', 1, '画面に「完了」と出る'],
    ['テストがすべて通る', 2, 'テストがすべて通る'],
    ['C2：言い方を変えた条件', 2, 'テストがすべて通る'],
  ]);
});

test('matchCriterion：番号の書き方の違いを受け、範囲の外の番号は文なしで返す', () => {
  const criteria = ['落ちない', '値が出る'];
  assert.deepEqual(matchCriterion('c2', criteria), { no: 2, text: '値が出る' });
  assert.deepEqual(matchCriterion('Ｃ１', criteria), { no: 1, text: '落ちない' });
  assert.deepEqual(matchCriterion('C1. 落ちない', criteria), { no: 1, text: '落ちない' });
  assert.deepEqual(matchCriterion('C5', criteria), { no: 5, text: '' });
  assert.deepEqual(matchCriterion('C5：足した条件', criteria), { no: 5, text: '足した条件' });
  assert.deepEqual(matchCriterion('C1は満たした', criteria), { no: null, text: 'C1は満たした' });
});

test('open と study の説明で、番号の付け方を伝える', async (t) => {
  const { fake } = await startWith(t);
  assert.match(fake.tool('open').parameters.properties.criteria.description, /番号（C1 など）は付けない/);
  assert.match(fake.tool('study').parameters.properties.verdicts.items.properties.criterion.description, /番号（C1 など）/);
});
