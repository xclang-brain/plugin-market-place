// 中心の処理：開始（設計書 4.3、6.1、10章「開始」）
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import test from 'node:test';
import { expectRefusal, LIMITS, makeHarness, PLUGIN, TASK } from './harness.mjs';

const resultOf = (h, runId) => JSON.parse(readFileSync(join(h.dataRoot, runId, 'result.json'), 'utf8'));
const lastRunId = (h) => readdirSync(h.dataRoot).sort().at(-1);

test('REQ-005 開始すると、選んだモデルで統括が起動され、指示文に基準と作業ごとの設定の値がすべて入る', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: '先週の週報を作ってください' });
  const p = h.core.start(h.settings({ maxIterations: 4, models: { orchestrator: 'gpt-6-luna', worker: 'claude-haiku-5-5', verifier: 'claude-opus-5-5' } }));
  await h.settle();
  await p;

  const starts = h.port.argsOf('startAgent');
  assert.equal(starts.length, 1);
  const [start] = starts;
  assert.equal(start.agentType, `${PLUGIN}:orchestrator`);
  assert.equal(start.model, 'gpt-6-luna');
  const run = h.run();
  assert.equal(run.status, 'running');
  assert.ok(start.name.includes(run.id), '名前に実行の番号が入る');
  // 指示文：頼まれた作業の文、上限、作業ごとの設定の値、基準の番号と文
  assert.ok(start.prompt.includes('先週の週報を作ってください'));
  // 始めに、定義の手順で進めて作業・検証に任せる旨の 1 文が来て、その後ろに実行の番号などが続く
  const head = 'あなたの定義の手順で、この実行を進めてください。成果物は作業エージェントに、確かめは検証エージェントに、`task` で頼みます。';
  assert.ok(start.prompt.startsWith(`${head}\n\n実行の番号：${run.id}\n`), start.prompt.slice(0, 120));
  assert.equal(start.prompt.split(head).length, 2, '1 文は 1 回だけ');
  assert.ok(start.prompt.includes('4 回'));
  for (const c of TASK.criteria) assert.ok(start.prompt.includes(`${c.id}：${c.text}`), `${c.id} の番号と文がそのまま入る`);
  assert.ok(start.prompt.includes('対象の週（week）：2026-W42'));
  assert.ok(start.prompt.includes('項目の数（items）：4'));
  assert.ok(start.prompt.includes('文体（tone）：desu'));
  assert.ok(start.prompt.includes('前回の週報をつける（attach）：はい'));
  // 作業・検証のモデルは、キーが <プラグイン>:<エージェント> の形で上書きされる
  assert.deepEqual(h.port.overrides, { [`${PLUGIN}:worker`]: { model: 'claude-haiku-5-5' }, [`${PLUGIN}:verifier`]: { model: 'claude-opus-5-5' } });
  // 実行の根は統括の番号
  assert.equal(run.agents.find((a) => a.slot === 'orchestrator').agentId, 'orch-1');
  assert.equal(run.maxIterations, 4);
  assert.deepEqual(run.settings.models, { orchestrator: 'gpt-6-luna', worker: 'claude-haiku-5-5', verifier: 'claude-opus-5-5' });
  // 思考の深さを選ばないときは、どのエージェントも上書きしない（定義のまま）
  assert.deepEqual(run.settings.efforts, { orchestrator: '', worker: '', verifier: '' });
  // 上書きしてから起動する順
  const order = h.port.calls.map((c) => c.method);
  assert.ok(order.indexOf('setSubagentSettings') < order.indexOf('startAgent'));
});

test('REQ-062 統括への指示文に、プラグインのフォルダの絶対パスが入る（エージェントがルールのスキルを絶対パスで読むため）', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'Coverity の警告をトリアージして' });
  const p = h.core.start(h.settings());
  await h.settle();
  await p;
  const prompt = h.port.argsOf('startAgent')[0].prompt;
  assert.ok(isAbsolute(h.pluginRoot));
  const section = `\n## プラグインのフォルダ\n${h.pluginRoot}\n`;
  assert.equal(prompt.split(section).length, 2, prompt.slice(0, 300));
  // 実行の番号の後、頼まれた作業の前に置く
  assert.ok(prompt.indexOf(`実行の番号：${h.run().id}`) < prompt.indexOf(section));
  assert.ok(prompt.indexOf(section) < prompt.indexOf('## 頼まれた作業'));
});

test('REQ-007 エージェントごとに選んだ思考の深さが、モデルと同じ上書きで渡る。統括の深さも上書きで渡す', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  const p = h.core.start(h.settings({ efforts: { orchestrator: 'high', worker: 'low' } }));
  await h.settle();
  await p;
  assert.deepEqual(h.port.overrides, {
    [`${PLUGIN}:orchestrator`]: { effort: 'high' },
    [`${PLUGIN}:worker`]: { model: 'gpt-6-luna', effort: 'low' },
    [`${PLUGIN}:verifier`]: { model: 'claude-opus-5-5' },
  });
  assert.deepEqual(h.run().settings.efforts, { orchestrator: 'high', worker: 'low', verifier: '' });
  // 統括のモデルは起動のときに渡す
  assert.equal(h.port.argsOf('startAgent')[0].model, 'claude-opus-5-5');
  const order = h.port.calls.map((c) => c.method);
  assert.ok(order.indexOf('setSubagentSettings') < order.indexOf('startAgent'));
});

test('REQ-031 作業ごとの設定の値を変えて開始すると、変えた値で実行される', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  const p = h.core.start(h.settings({ task: { week: '2026-W01', tone: 'da', attach: false } }));
  await h.settle();
  await p;
  const prompt = h.port.argsOf('startAgent')[0].prompt;
  assert.ok(prompt.includes('対象の週（week）：2026-W01'));
  assert.ok(prompt.includes('文体（tone）：da'));
  assert.ok(prompt.includes('前回の週報をつける（attach）：いいえ'));
  assert.equal(h.run().settings.task.week, '2026-W01');
});

test('REQ-005 開始中か実行中に届いた開始は断る。統括は 1 体だけ起動する', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', { delay: 3000 });
  const first = h.core.start(h.settings());
  await h.settle();
  assert.equal(h.run().status, 'starting');
  const second = await expectRefusal(h.core.start(h.settings()));
  assert.match(second, /すでに実行中/);
  await h.clock.advance(3000);
  await first;
  assert.equal(h.run().status, 'running');
  await expectRefusal(h.core.start(h.settings())); // 実行中も断る
  assert.equal(h.port.callsOf('startAgent').length, 1);
});

test('REQ-005 開始のとき、設定を確かめる（必須の項目、上限の回数、選んだモデル、思考の深さ）', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  const bad = [
    [h.settings({ task: { week: '' } }), /対象の週/],
    [h.settings({ maxIterations: 0 }), /1 以上の整数/],
    [h.settings({ maxIterations: 2.5 }), /1 以上の整数/],
    [h.settings({ models: { orchestrator: 'unknown-model' } }), /選べるモデルにありません/],
    [h.settings({ efforts: { worker: 'huge' } }), /作業（週報を書く係）の思考の深さ「huge」は、選べる値にありません/],
    [h.settings({ task: { items: 'たくさん' } }), /数で入力/],
    [h.settings({ task: { tone: 'unknown' } }), /選べる値/],
  ];
  for (const [settings, pattern] of bad) {
    const message = await expectRefusal(h.core.start(settings));
    assert.match(message, pattern);
  }
  assert.equal(h.port.callsOf('startAgent').length, 0);
  assert.equal(h.port.callsOf('setSubagentSettings').length, 0);
  assert.equal(h.run(), null, '準備中のまま');
});

test('REQ-005 起動に失敗すると、上書きが外れ、準備中に戻って理由が出る。もう一度開始できる', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', { error: new Error('このモデルは使えません') });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  const message = await refusal;
  assert.match(message, /このモデルは使えません/);
  const methods = h.port.calls.map((c) => c.method);
  assert.equal(methods.filter((m) => m === 'setSubagentSettings').length, 2);
  assert.equal(h.port.overrides, null, '上書きが外れている');
  assert.equal(h.run(), null);
  assert.match(h.state().startError.message, /このモデルは使えません/);
  assert.equal(h.port.callsOf('listTasks').length, 0, '失敗が分かっているので、統括は探さない');

  h.port.reset('startAgent');
  const again = h.core.start(h.settings());
  await h.settle();
  await again;
  assert.equal(h.run().status, 'running');
  assert.equal(h.state().startError, null);
});

test('REQ-005 モデルの上書きに失敗すると、統括は起動せず、準備中に戻る', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  let calls = 0;
  h.port.behave('setSubagentSettings', { value: () => { calls += 1; if (calls === 1) throw new Error('設定できません'); } });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  await refusal;
  assert.equal(h.port.callsOf('startAgent').length, 0);
  assert.equal(h.run(), null);
});

test('REQ-005 起動が時間切れになると、名前から統括を探して取り消し、拒む一覧に入れ、上書きを外して準備中に戻る', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  // 起動はできているが、呼び出しが返らない
  h.port.behave('startAgent', {
    value: (args) => {
      h.port.spawn({ agentId: 'late-1', agentType: args.agentType, displayName: args.name, status: 'running', executionMode: 'background' });
      return new Promise(() => {});
    },
  });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  await h.clock.advance(LIMITS.CALL_TIMEOUT_MS);
  const message = await refusal;
  assert.match(message, /時間切れ/);
  assert.match(message, /取り消しました/);
  assert.deepEqual(h.port.argsOf('cancelAgent'), ['late-1']);
  assert.equal(h.port.task('late-1').status, 'cancelled');
  assert.deepEqual(h.core.preToolUse({ agentId: 'late-1', toolName: 'edit' }).deny, true, '拒む一覧に入る');
  assert.equal(h.port.overrides, null, '上書きが外れている');
  assert.equal(h.run(), null);
  assert.equal(h.state().startError.unknownLaunch, false);
});

test('REQ-005 時間切れで統括が見つからなければ、「起動したかは分からない」として記録に残す', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', { hang: true });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  await h.clock.advance(LIMITS.CALL_TIMEOUT_MS);
  const message = await refusal;
  assert.match(message, /起動したかは分かりません/);
  assert.equal(h.state().startError.unknownLaunch, true);
  assert.equal(h.port.callsOf('cancelAgent').length, 0);
  const result = resultOf(h, lastRunId(h));
  assert.equal(result.startFailed, true);
  assert.equal(result.startError.unknownLaunch, true);
  // 開始できなかった実行は、準備中の画面の「前の結果」に出ない
  await h.core.open({ requestText: 'y' });
  assert.equal(h.state().previous, null);
});

test('REQ-005 起動が時間切れになった後に遅れて起動した統括は、取り消されて残らない', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', { delay: 15_000 });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  await h.clock.advance(LIMITS.CALL_TIMEOUT_MS);
  await refusal;
  assert.equal(h.run(), null);
  assert.equal(h.port.callsOf('cancelAgent').length, 0, '時間切れの時点ではまだ起動していない');

  // 遅れて起動し、つなぎ口が開始のイベントを流す
  await h.clock.advance(5000);
  const late = h.port.tasks.find((x) => x.agentType === `${PLUGIN}:orchestrator`);
  assert.ok(late, '遅れて起動した');
  await h.event({ type: 'agent.started', agentId: late.agentId, agentType: late.agentType, executionMode: 'background', model: 'claude-opus-5-5' });
  assert.deepEqual(h.port.argsOf('cancelAgent'), [late.agentId], '取り消しは 1 回だけ');
  assert.equal(h.port.task(late.agentId).status, 'cancelled');
  assert.equal(h.core.preToolUse({ agentId: late.agentId, toolName: 'edit' }).deny, true);
  assert.equal(h.run(), null, '画面には出ない');
});

test('REQ-005 どの実行の根でもない統括の開始のイベントが届いたら、取り消して拒む一覧に入れる', async (t) => {
  const h = await makeHarness(t);
  h.port.spawn({ agentId: 'stray-1', agentType: `${PLUGIN}:orchestrator`, status: 'running', executionMode: 'background' });
  await h.event({ type: 'agent.started', agentId: 'stray-1', agentType: `${PLUGIN}:orchestrator`, executionMode: 'background' });
  await h.event({ type: 'agent.started', agentId: 'stray-1', agentType: `${PLUGIN}:orchestrator`, executionMode: 'background' });
  assert.deepEqual(h.port.argsOf('cancelAgent'), ['stray-1']);
  assert.equal(h.core.preToolUse({ agentId: 'stray-1', toolName: 'x' }).deny, true);
});

test('REQ-009 開始中に止めても、起動が返らないまま、押してから 8 秒で終了の表示になる', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', { hang: true });
  h.port.behave('listTasks', { hang: true });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  await h.clock.advance(1000);
  const pressed = h.clock.now();
  await h.core.stop();
  assert.equal(h.run().status, 'stopping', '押した時点で、すぐ停止中になる');
  await h.clock.advance(LIMITS.STOP_DEADLINE_MS - 1);
  assert.equal(h.run().status, 'stopping');
  await h.clock.advance(1);
  assert.equal(h.clock.now() - pressed, LIMITS.STOP_DEADLINE_MS);
  const run = h.run();
  assert.equal(run.status, 'finished');
  assert.equal(run.endReason.kind, 'user_stopped');
  assert.ok(LIMITS.STOP_DEADLINE_MS < 10_000, '押してから 10 秒のうちに終わる');
  await refusal;
  assert.equal(h.port.overrides, null, '上書きが外れている');
});

test('REQ-009 開始中に止めると、名前から統括を探して取り消し、すぐ終了の表示になる', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', {
    value: (args) => {
      h.port.spawn({ agentId: 'orch-x', agentType: args.agentType, displayName: args.name, status: 'running', executionMode: 'background' });
      return new Promise(() => {});
    },
  });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  await h.core.stop();
  await h.settle();
  await refusal;
  assert.deepEqual(h.port.argsOf('cancelAgent'), ['orch-x']);
  assert.equal(h.run().status, 'finished');
  assert.equal(h.run().endReason.kind, 'user_stopped');
  assert.equal(h.run().endReason.unstoppable, false);
  assert.equal(h.core.preToolUse({ agentId: 'orch-x', toolName: 'x' }).deny, true);
});

test('REQ-009 開始中に止めた後に、起動の結果が遅れて届いても、その統括は取り消される', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', { delay: 5000 });
  const refusal = expectRefusal(h.core.start(h.settings()));
  await h.settle();
  await h.clock.advance(1000);
  await h.core.stop();
  await h.settle();
  await refusal;
  assert.equal(h.run().status, 'finished');
  await h.clock.advance(5000);
  const late = h.port.tasks.find((x) => x.agentType === `${PLUGIN}:orchestrator`);
  assert.deepEqual(h.port.argsOf('cancelAgent'), [late.agentId]);
  assert.equal(h.port.task(late.agentId).status, 'cancelled');
});

test('REQ-005 起動を待つあいだに届いたイベントと記録は、列で待たされ、起動が終わってから処理される', async (t) => {
  const h = await makeHarness(t);
  await h.core.open({ requestText: 'x' });
  h.port.behave('startAgent', { delay: 3000 });
  const p = h.core.start(h.settings());
  await h.settle();
  // 起動の結果より先に、統括の開始のイベントと最初の計画が届いた
  const root = 'orch-1';
  h.core.onEvent({ type: 'agent.started', agentId: root, agentType: `${PLUGIN}:orchestrator`, model: 'claude-opus-5-5', executionMode: 'background', at: h.clock.now() });
  const planned = h.tool('plan', h.planArgs(1), root);
  await h.settle();
  assert.equal(h.run().status, 'starting');
  assert.equal(h.run().iterations.length, 0);
  await h.clock.advance(3000);
  await p;
  assert.match(await planned, /試行 1 の計画を記録しました/);
  const run = h.run();
  assert.equal(run.status, 'running');
  assert.equal(run.agents.find((a) => a.slot === 'orchestrator').actualModel, 'claude-opus-5-5');
  assert.equal(run.iterations.length, 1);
});
