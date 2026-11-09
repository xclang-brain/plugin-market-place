// REQ-404, REQ-406, design 8.1 step 2 and 4.15: starting a run.
import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';

import { RUN_DIR, openAndStart, startWith } from './helpers.mjs';

const BODY = {
  models: { orchestrator: 'gpt-6.1-sol', worker: 'claude-haiku-5.5', reviewer: 'claude-sonnet-5.5', integrator: 'gpt-6-luna' },
  efforts: { orchestrator: 'high', worker: '', reviewer: 'xhigh', integrator: 'low' },
  maxRework: 2,
  conditions: { filter: 'misra.yaml', impact: 'Medium', limit: 15, initial: 'する' },
};

test('design 8.1-2.1 overrides: worker/reviewer/integrator models and efforts, orchestrator effort; keys are the defaults.json ids', async (t) => {
  const h = await startWith(t);
  await openAndStart(h, BODY);
  const [call] = h.fake.callsOf('tools.updateSubagentSettings');
  const { roles, orchestrator } = h.defaults;
  assert.deepEqual(call.args, {
    subagents: {
      agents: {
        [orchestrator]: { effortLevel: 'high' },
        [roles.worker]: { model: 'claude-haiku-5.5' },
        [roles.reviewer]: { model: 'claude-sonnet-5.5', effortLevel: 'xhigh' },
        [roles.integrator]: { model: 'gpt-6-luna', effortLevel: 'low' },
      },
    },
  });
});

test('design 8.1-2.2 new-run gets the screen values, always --review, the repo and the Coverity server name', async (t) => {
  const h = await startWith(t);
  await openAndStart(h, BODY);
  const [call] = h.runner.callsOf('run:new-run');
  assert.deepEqual(call.args.slice(5), ['--repo', h.dirs.cwd, '--filter', 'misra.yaml', '--impact', 'Medium', '--limit', '15', '--initial', '--review', '--max-rework', '2', ...h.defaults.coverityReadTools.flatMap((x) => ['--mcp-tool', `${h.defaults.coverityServer}(${x})`])]);
  assert.equal(call.env.PYTHONIOENCODING, 'utf-8');
  assert.equal(call.args[3], path.join(h.dirs.pluginRoot, ...h.defaults.runCommand.split('/')));
});

test('design 8.1-2.2 "絞らない", an empty count and "設定どおり" add no arguments; "しない" → --no-initial', async (t) => {
  const h = await startWith(t);
  await openAndStart(h, { conditions: { filter: 'untriaged.yaml', impact: '絞らない', limit: '', initial: '設定どおり' } });
  assert.deepEqual(h.runner.callsOf('run:new-run')[0].args.slice(5), ['--repo', h.dirs.cwd, '--filter', 'untriaged.yaml', '--review', '--max-rework', '1', ...h.defaults.coverityReadTools.flatMap((x) => ['--mcp-tool', `${h.defaults.coverityServer}(${x})`])]);
  const h2 = await startWith(t);
  await openAndStart(h2, { conditions: { initial: 'しない' } });
  assert.ok(h2.runner.callsOf('run:new-run')[0].args.includes('--no-initial'));
});

test('design 8.1-2.3 and 4.15 the orchestrator starts in the background with the chosen model and the request from messages.mjs', async (t) => {
  const h = await startWith(t);
  await h.open('High だけ、とくに reader.c を見て');
  await h.idle();
  await h.core.start(BODY);
  await h.idle();
  const [call] = h.fake.callsOf('tasks.startAgent');
  assert.equal(call.args.agentType, h.defaults.orchestrator);
  assert.equal(call.args.model, 'gpt-6.1-sol');
  assert.ok(call.args.name.includes(h.ctx.run.id));
  const prompt = call.args.prompt;
  assert.ok(prompt.includes(RUN_DIR), 'the run folder');
  assert.ok(prompt.includes(`"${path.join(h.dirs.pluginRoot, ...h.defaults.runCommand.split('/'))}"`), 'the absolute command location');
  for (const id of Object.values(h.defaults.roles)) assert.ok(prompt.includes(id), `role id ${id}`);
  assert.match(prompt, /新しく作らない/);
  assert.match(prompt, /再開の確認も、始める前の質問もせず、tick から/);
  assert.match(prompt, /利用者には質問しない/);
  assert.match(prompt, /許可は、利用者がチャットで答えます/);
  assert.match(prompt, /note-issue/);
  assert.match(prompt, /1行で返して/);
  assert.ok(!prompt.includes('reader.c'), 'the user request text is not in the request');
  assert.equal(h.ctx.run.status, 'running');
  assert.ok(h.fake.task(h.ctx.run.rootId), 'the root is the agent startAgent returned');
});

test('REQ-404 checks of the screen values', async (t) => {
  const h = await startWith(t);
  await h.open();
  await h.idle();
  const bad = [
    [{ maxRework: 3 }, /0 から 2 までの整数/],
    [{ maxRework: -1 }, /0 から 2 までの整数/],
    [{ conditions: { limit: 0 } }, /1 から 100 までの整数/],
    [{ conditions: { limit: 101 } }, /1 から 100 までの整数/],
    [{ conditions: { limit: '3.5' } }, /1 から 100 までの整数/],
    [{ conditions: { filter: '' } }, /条件ファイルの名前」を入力/],
    [{ conditions: { filter: '../x.yaml' } }, /ファイルの名前だけ/],
    [{ conditions: { filter: '--help' } }, /ファイルの名前だけ/],
    [{ conditions: { impact: 'Huge' } }, /選べる値から/],
    [{ conditions: { initial: 'たぶん' } }, /選べる値から/],
    [{ models: { worker: 'no-such-model' } }, /選べるモデルにありません/],
    [{ efforts: { reviewer: 'extreme' } }, /選べる値にありません/],
  ];
  for (const [body, re] of bad) assert.throws(() => h.core.start(body), re, JSON.stringify(body));
  assert.equal(h.runner.callsOf('run:new-run').length, 0);
  assert.equal(h.ctx.run, null);
});

test('start is refused while the settings are still loading, and while a run is active', async (t) => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const h = await startWith(t);
  h.runner.on('run:settings', async () => {
    await gate;
    return { ok: true, max_rework: 1, filters: [] };
  });
  await h.open();
  assert.throws(() => h.core.start({}), /設定を読み込んでいます/);
  release();
  await h.idle();
  await h.core.start({});
  await h.idle();
  assert.throws(() => h.core.start({}), /すでに実行中です/);
});

test('REQ-406 new-run fails: back to setup with the reason, the problems and how to prepare; overrides removed', async (t) => {
  const h = await startWith(t);
  h.runner.on('run:new-run', { ok: false, error: 'output_dir がリポジトリの中です', problems: ['output_dir を外にしてください'] });
  await h.open();
  await h.idle();
  await assert.rejects(h.core.start({}), /実行フォルダを作れませんでした：output_dir がリポジトリの中です。もう一度開始できます。/);
  await h.idle();
  const state = h.state();
  assert.equal(state.run, null);
  assert.deepEqual(state.startError.problems, ['output_dir を外にしてください']);
  assert.equal(state.startError.hint, true);
  assert.match(state.guide.setup, new RegExp(`/${h.defaults.setupSkill}`));
  assert.equal(h.fake.callsOf('tasks.startAgent').length, 0);
  assert.deepEqual(h.fake.callsOf('tools.updateSubagentSettings').at(-1).args, { subagents: null });
});

test('REQ-406 the orchestrator cannot start: back to setup with the reason; overrides removed; polling stops', async (t) => {
  const h = await startWith(t);
  h.fake.behave('tasks.startAgent', { error: new Error('no such agent') });
  await h.open();
  await h.idle();
  await assert.rejects(h.core.start({}), /統括エージェントを起動できませんでした：no such agent。もう一度開始できます。/);
  await h.idle();
  assert.equal(h.state().run, null);
  assert.match(h.state().startError.message, /no such agent/);
  assert.deepEqual(h.fake.callsOf('tools.updateSubagentSettings').at(-1).args, { subagents: null });
  const before = h.runner.callsOf('run:status').length;
  await h.clock.advance(30_000);
  assert.equal(h.runner.callsOf('run:status').length, before);
});

test('the start timing out: the late orchestrator is cancelled when it arrives', async (t) => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const h = await startWith(t);
  h.fake.behave('tasks.startAgent', { gate, value: { agentId: 'late-1' } });
  await h.open();
  await h.idle();
  const started = h.core.start({});
  started.catch(() => {});
  await h.clock.advance(10_001);
  await assert.rejects(started, /時間切れ/);
  release();
  await h.idle();
  assert.ok(h.fake.callsOf('tasks.cancel').some((c) => c.args.id === 'late-1'));
  assert.ok(h.ctx.deny.has('late-1'));
});
