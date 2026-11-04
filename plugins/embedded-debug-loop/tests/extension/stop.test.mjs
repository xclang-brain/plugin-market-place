// 「止める」で終わったときの結論：どこで止めたかと、途中で変えたファイル（ファイルを直すツールで変えたもの）。
// 止めて切られた子のエージェントの記録の行と、止めて自動で終えたときにチャットへ出す知らせも確かめる。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { editedPaths } from '../../com.github.copilot/extensions/embedded-debug-loop/core/events.mjs';
import { stopPlace } from '../../com.github.copilot/extensions/embedded-debug-loop/core/store.mjs';
import { VERIFIER, WORKER, startRun } from './helpers.mjs';

const HEAD = '利用者が Web管理画面で止めました。';
const FILES = '途中で変えたファイル（元に戻していません）（コマンドで変わったファイルは含みません）：';
const settle = () => new Promise((resolve) => setImmediate(resolve));
const PATCH = ['*** Begin Patch', '*** Update File: src/x.c', '@@', '-a', '+b', '*** Add File: src/y.c', '+new', '*** End Patch'].join('\n');

// 試行 1 の計画を記録し、作業エージェントを動かす
async function working(t) {
  const ctx = await startRun(t);
  const { fake } = ctx;
  fake.emit('assistant.turn_start', {});
  await fake.call('plan', { iteration: 1, plan: '直してみる' });
  fake.emit('subagent.started', { agentType: WORKER, toolCallId: 't1' }, 'w1');
  return ctx;
}

const tool = (fake, id, toolName, args, agentId = 'w1') => fake.emit('tool.execution_start', { toolCallId: id, toolName, arguments: args }, agentId);
const done = (fake, id, success = true) => fake.emit('tool.execution_complete', { toolCallId: id, success }, 'w1');

test('止めて自動で終わると、結論に「試行 1 の作業の途中で止めました」と、途中で変えたファイルを書く', async (t) => {
  const { fake, app, dataDir } = await working(t);
  tool(fake, 'c1', 'edit', { path: '/w/project/a.c', old_str: 'x', new_str: 'y' });
  done(fake, 'c1');
  tool(fake, 'c2', 'create', { path: '/w/project/new.c', file_text: '' }); // 終わりの知らせが来ない（止めた）ものも入れる
  tool(fake, 'c3', 'apply_patch', PATCH);
  done(fake, 'c3');
  tool(fake, 'c4', 'edit', { path: '/w/project/bad.c' }); // 失敗したものは入れない
  done(fake, 'c4', false);
  tool(fake, 'c5', 'edit', { path: '/w/project/a.c' }); // 一度変えたファイルは、後で失敗しても変えたまま
  done(fake, 'c5', false);
  tool(fake, 'c6', 'bash', { command: 'echo hi > out.txt' });
  tool(fake, 'c7', 'view', { path: '/w/project/a.c' });
  assert.match(app.store.snapshot().run.activity.find((a) => a.text.includes('src/x.c')).text, /^ファイルを直す：src\/x\.c、src\/y\.c$/);

  await app.control.stop();
  let run = app.store.snapshot().run;
  assert.equal(run.status, 'stopping');
  assert.deepEqual([run.activity[0].who, run.activity[0].text], ['利用者', 'Web管理画面で「止める」を押しました']);
  // 止めた後に届いた返事は、途中で切れたもの。作業の報告にはしない（止めた所も変わらない）
  fake.subagentStop({ agentType: WORKER, agentId: 'w1', response: '途中までの報告' });
  fake.emit('subagent.completed', { agentType: WORKER }, 'w1');
  fake.emit('session.idle', { aborted: true });

  run = app.store.snapshot().run;
  assert.equal(run.iterations[0].do, null);
  const events = fs.readFileSync(path.join(dataDir, 'runs', run.id, 'events.jsonl'), 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  assert.ok(events.some((e) => e.type === 'worker.reply' && e.afterStop === true && e.report === '途中までの報告'), '振り返りの記録には残す');
  assert.equal(run.status, 'finished');
  assert.equal(run.finish.outcome, 'stopped');
  assert.equal(run.finish.conclusion, [
    HEAD,
    '試行 1 の作業の途中で止めました。',
    FILES,
    '- /w/project/a.c',
    '- /w/project/new.c',
    '- src/x.c',
    '- src/y.c',
  ].join('\n'));
});

test('変えたファイルがなければ、一覧の行は書かない。次の実行に、前の実行のファイルを持ち越さない', async (t) => {
  const { fake, app } = await working(t);
  tool(fake, 'c1', 'edit', { path: '/w/project/a.c' });
  done(fake, 'c1');
  await app.control.stop();
  fake.emit('session.idle', {});
  assert.match(app.store.snapshot().run.finish.conclusion, /a\.c/);

  await fake.call('open', { request: 'もう一度', criteria: ['完了と出る'] });
  await app.control.start({});
  await app.control.stop(); // 何も動いていないので、その場で終わる
  const run = app.store.snapshot().run;
  assert.equal(run.status, 'finished');
  assert.equal(run.finish.conclusion, `${HEAD}\n試行 1 の計画の途中で止めました。`);
});

test('止めた後に統括エージェントが終わりを記録したら、その結論に、途中で変えたファイルの一覧を足す（二重には書かない）', async (t) => {
  const { fake, app } = await working(t);
  tool(fake, 'c1', 'edit', { path: '/w/project/a.c' });
  done(fake, 'c1');
  await app.control.stop();
  await fake.call('finish', { outcome: 'stopped', conclusion: '途中で止めました。', retro: 'なし' });
  assert.equal(app.store.snapshot().run.finish.conclusion, `途中で止めました。\n${FILES}\n- /w/project/a.c`);

  const again = await working(t);
  tool(again.fake, 'c1', 'edit', { path: '/w/project/b.c' });
  done(again.fake, 'c1');
  await again.app.control.stop();
  const own = `止めました。\n${FILES}\n- /w/project/b.c（自分で書いた）`;
  await again.fake.call('finish', { outcome: 'stopped', conclusion: own, retro: 'なし' });
  assert.equal(again.app.store.snapshot().run.finish.conclusion, own);
});

test('止めていない終わり方の結論には、ファイルの一覧を足さない', async (t) => {
  const { fake, app } = await working(t);
  tool(fake, 'c1', 'edit', { path: '/w/project/a.c' });
  done(fake, 'c1');
  await fake.call('finish', { outcome: 'needs-human', conclusion: '人の判断が要ります。', retro: 'なし' });
  assert.equal(app.store.snapshot().run.finish.conclusion, '人の判断が要ります。');
});

test('stopPlace：記録といま動いているエージェントで、止めた所を決める', () => {
  const v = (result) => ({ verdicts: [{ result }] });
  const run = (iterations, role = null, max = 3) => ({ iteration: iterations.length, iterations, maxIterations: max, active: role ? { role } : null });
  const plan = { text: 'p' };
  const cases = [
    [run([]), { n: 1, step: 'plan' }],
    [run([], 'worker'), { n: 1, step: 'prep' }],
    [run([{ n: 1, plan, do: null, study: null }], 'worker'), { n: 1, step: 'do' }],
    [run([{ n: 1, plan, do: null, study: null }], 'orchestrator'), { n: 1, step: 'do' }],
    [run([{ n: 1, plan, do: null, study: null }], 'verifier'), { n: 1, step: 'study' }],
    [run([{ n: 1, plan, do: { report: 'r' }, study: null }]), { n: 1, step: 'study' }],
    [run([{ n: 1, plan, do: { report: 'r' }, study: v('fail') }]), { n: 2, step: 'plan' }],
    [run([{ n: 1, plan, do: { report: 'r' }, study: v('pass') }]), { n: 1, step: 'after' }],
    [run([{ n: 1, plan, do: { report: 'r' }, study: v('fail') }], null, 1), { n: 1, step: 'after' }],
  ];
  for (const [r, want] of cases) assert.deepEqual(stopPlace(r), want, JSON.stringify(r));
});

test('editedPaths：ファイルを直すツールだけ。パッチは、文でも input でも、ファイルの行から読む', () => {
  assert.deepEqual(editedPaths('edit', { path: ' /w/a.c ' }), ['/w/a.c']);
  assert.deepEqual(editedPaths('create', { path: 'b.c' }), ['b.c']);
  assert.deepEqual(editedPaths('apply_patch', PATCH), ['src/x.c', 'src/y.c']);
  assert.deepEqual(editedPaths('apply_patch', { input: '*** Update File: a.c\n*** Move to: b.c\n*** Delete File: c.c\n*** Update File: a.c' }), ['a.c', 'b.c', 'c.c']);
  assert.deepEqual(editedPaths('apply_patch', '*** Begin Patch'), []);
  assert.deepEqual(editedPaths('view', { path: 'a.c' }), []);
  assert.deepEqual(editedPaths('bash', { command: 'rm a.c' }), []);
  assert.deepEqual(editedPaths('edit', null), []);
});

test('変えたファイルの一覧の見出しに、コマンドで変わったファイルは含まないと書く', async (t) => {
  const { fake, app } = await working(t);
  tool(fake, 'c1', 'edit', { path: '/w/project/a.c' });
  done(fake, 'c1');
  tool(fake, 'c2', 'bash', { command: 'cp a.c /tmp/a.c && touch out.txt' });
  done(fake, 'c2');
  await app.control.stop();
  fake.emit('session.idle', {});
  const lines = app.store.snapshot().run.finish.conclusion.split('\n');
  const head = lines.find((l) => l.startsWith('途中で変えたファイル'));
  assert.ok(head.includes('（コマンドで変わったファイルは含みません）'), head);
  assert.deepEqual(lines.slice(lines.indexOf(head) + 1), ['- /w/project/a.c'], 'コマンドで変わったものは一覧に入らない');
});

test('止めて切られた子のエージェントは「途中で止まりました（利用者が止めたため）」と書き、「返事をしました」とは書かない', async (t) => {
  const { fake, app } = await working(t);
  fake.emit('subagent.started', { agentType: VERIFIER, toolCallId: 't2' }, 'v1');
  await app.control.stop();
  fake.emit('subagent.completed', { agentType: WORKER, cancelled: true }, 'w1');
  fake.emit('subagent.completed', { agentType: VERIFIER, cancelled: true }, 'v1');
  const rows = app.store.snapshot().run.activity.map((a) => `${a.who}|${a.text}`);
  assert.deepEqual(rows.slice(0, 2), [
    '検証|検証エージェントは途中で止まりました（利用者が止めたため）',
    '作業|作業エージェントは途中で止まりました（利用者が止めたため）',
  ]);
  assert.ok(!rows.some((r) => r.includes('返事をしました')), rows.join('\n'));
});

test('「止める」を押していないのに切られたときは、止めた理由を書かない。切られていなければ「返事をしました」', async (t) => {
  const { fake, app } = await working(t);
  fake.emit('subagent.completed', { agentType: WORKER, cancelled: true }, 'w1');
  assert.equal(app.store.snapshot().run.activity[0].text, '作業エージェントは途中で止まりました');
  fake.emit('subagent.started', { agentType: WORKER, toolCallId: 't3' }, 'w2');
  fake.emit('subagent.completed', { agentType: WORKER, cancelled: false }, 'w2');
  assert.equal(app.store.snapshot().run.activity[0].text, '作業エージェントが返事をしました');
});

test('止めて自動で終えたら、結論と結果の画面の URL を、チャットにも warning で出す（1回だけ）', async (t) => {
  const { fake, app } = await working(t);
  tool(fake, 'c1', 'edit', { path: '/w/project/a.c' });
  done(fake, 'c1');
  await app.control.stop();
  assert.deepEqual(fake.logs, [], '終わるまでは出さない');
  fake.emit('subagent.completed', { agentType: WORKER, cancelled: true }, 'w1');
  fake.emit('session.idle', { aborted: true });
  await settle();
  fake.emit('session.idle', {});
  await settle();
  const run = app.store.snapshot().run;
  assert.equal(run.status, 'finished');
  assert.deepEqual(fake.logs, [{ message: `${run.finish.conclusion}\n結果の画面：${app.store.url}#result`, options: { level: 'warning' } }]);
  assert.match(fake.logs[0].message, /^利用者が Web管理画面で止めました。\n試行 1 の作業の途中で止めました。\n/);
  assert.match(fake.logs[0].message, /- \/w\/project\/a\.c\n結果の画面：http:\/\/127\.0\.0\.1:\d+\/\?k=[\w-]{24}#result$/);
});

test('動いているエージェントがなく、その場で終えたときも、チャットに知らせを出す', async (t) => {
  const { fake, app } = await startRun(t);
  await app.control.stop();
  const run = app.store.snapshot().run;
  assert.equal(run.status, 'finished');
  assert.deepEqual(fake.logs, [{ message: `${HEAD}\n試行 1 の計画の途中で止めました。\n結果の画面：${app.store.url}#result`, options: { level: 'warning' } }]);
});

test('チャットに知らせを出せなくても、止める流れは変わらない', async (t) => {
  const quick = await startRun(t, { fake: { logFails: true } });
  assert.deepEqual(await quick.app.control.stop(), { ok: true });
  assert.equal(quick.app.store.snapshot().run.status, 'finished');

  const slow = await startRun(t, { fake: { logFails: true } });
  slow.fake.emit('subagent.started', { agentType: WORKER, toolCallId: 't1' }, 'w1');
  await slow.app.control.stop();
  slow.fake.emit('session.idle', {});
  await settle();
  assert.equal(slow.app.store.snapshot().run.status, 'finished');
  assert.equal(slow.app.store.snapshot().run.finish.outcome, 'stopped');
});

test('止めた後に統括エージェントが終わりを記録したときは、チャットに知らせを出さない（統括エージェントが伝える）', async (t) => {
  const { fake, app } = await working(t);
  await app.control.stop();
  await fake.call('finish', { outcome: 'stopped', conclusion: '途中で止めました。', retro: 'なし' });
  fake.emit('session.idle', {});
  await settle();
  assert.equal(app.store.snapshot().run.status, 'finished');
  assert.deepEqual(fake.logs, []);
});
