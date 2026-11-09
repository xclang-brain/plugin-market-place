// REQ-401, REQ-403, REQ-404, REQ-405, REQ-407: the open tool, the setup values, the notices.
import assert from 'node:assert/strict';
import test from 'node:test';

import { SETTINGS, TOOL, openAndStart, startWith } from './helpers.mjs';

test('REQ-401 the tool is registered once: <plugin>_open, skipPermission, defer never, requestText required', async (t) => {
  const h = await startWith(t);
  const tools = h.fake.config.tools;
  assert.equal(tools.length, 1);
  const [tool] = tools;
  assert.equal(tool.name, TOOL);
  assert.equal(tool.skipPermission, true);
  assert.equal(tool.defer, 'never');
  assert.deepEqual(tool.parameters.required, ['requestText']);
  assert.equal(tool.parameters.additionalProperties, false);
  assert.match(tool.description, /requestText/);
  assert.equal(typeof h.fake.config.hooks.onPreToolUse, 'function');
  assert.equal(typeof h.fake.config.hooks.onSubagentStop, 'function');
  assert.equal(typeof h.fake.config.onPermissionRequest, 'function');
});

test('REQ-401 REQ-403 open returns a 127.0.0.1 URL with a token and the four notices', async (t) => {
  const h = await startWith(t);
  const r = await h.open();
  assert.equal(r.ok, true);
  assert.match(r.text, /^Web管理画面の URL：http:\/\/127\.0\.0\.1:\d+\/\?k=[\w-]{20,}$/m);
  assert.match(r.text, /「開始する」を押して/);
  assert.match(r.text, /\/clear を使わないで/);
  assert.match(r.text, /このチャットで答えて/);
  assert.match(r.text, /前もって許可するしかた.*README/);
});

test('REQ-405 the request text fills the setup values; other condition phrases are listed as not used', async (t) => {
  const h = await startWith(t);
  await h.open('misra.yaml で Coverity の警告をトリアージして。High だけ、10 件だけ、解析はしないで。NULL_RETURNS だけ');
  await h.idle();
  const { setup } = h.state();
  assert.equal(setup.request.text, 'misra.yaml で Coverity の警告をトリアージして。High だけ、10 件だけ、解析はしないで。NULL_RETURNS だけ');
  assert.deepEqual(setup.conditions, { filter: 'misra.yaml', impact: 'High', limit: 10, initial: 'しない' });
  assert.deepEqual(setup.request.unused, ['NULL_RETURNS だけ']);
});

test('REQ-404 the setup values come from `settings`; models from the agent definitions; efforts "do not change"', async (t) => {
  const h = await startWith(t);
  h.runner.on('run:settings', { ...SETTINGS, max_rework: 2, parallel: 5 });
  await h.open();
  await h.idle();
  const { setup } = h.state();
  assert.equal(h.runner.callsOf('run:settings').length, 1);
  assert.deepEqual(h.runner.callsOf('run:settings')[0].args.slice(-2), ['--repo', h.dirs.cwd]);
  assert.equal(h.runner.callsOf('run:settings')[0].env.PYTHONIOENCODING, 'utf-8');
  assert.deepEqual(h.runner.callsOf('run:settings')[0].args.slice(0, 3), ['run', '--native-tls', '--quiet']);
  assert.equal(setup.settings.state, 'ok');
  assert.equal(setup.maxReworkDefault, 2);
  assert.deepEqual(setup.settings.filters, ['untriaged.yaml', 'misra.yaml']);
  assert.deepEqual(setup.conditions, { filter: 'untriaged.yaml', impact: '絞らない', limit: 20, initial: '設定どおり' }, 'the count starts at 20, as in the original');
  assert.deepEqual(setup.defaults, { orchestrator: 'claude-sonnet-5.5', worker: 'gpt-6-luna', reviewer: 'claude-sonnet-5.5', integrator: 'gpt-6-luna' });
  assert.deepEqual(setup.effortLevels, ['low', 'medium', 'high', 'xhigh', 'max']);
  assert.match(setup.overrideNotice, /ほかのエージェントに設定したモデルと思考の深さの指定も消えます/);
  // the first open also lists the runs once, so uv is ready (design 8.1 step 1)
  assert.equal(h.runner.callsOf('run:runs').length, 1);
  await h.open();
  await h.idle();
  assert.equal(h.runner.callsOf('run:runs').length, 1);
});

test('REQ-404 Auto: the notice is in the setup state', async (t) => {
  const h = await startWith(t);
  h.fake.currentModel = { modelId: 'auto' };
  await h.open();
  await h.idle();
  const { setup } = h.state();
  assert.equal(setup.isAuto, true);
  assert.match(setup.autoNotice, /Auto のため、役割ごとのモデルが効かないことがあります/);
});

test('REQ-406 settings that cannot be read show the error and how to prepare (setup skill from defaults.json)', async (t) => {
  const h = await startWith(t);
  h.runner.on('run:settings', { ok: false, error: '設定ファイルがありません', problems: ['.coverity-triage/config.yaml がない'] });
  await h.open();
  await h.idle();
  const { setup } = h.state();
  assert.equal(setup.settings.state, 'error');
  assert.equal(setup.settings.error, '設定ファイルがありません');
  assert.deepEqual(setup.settings.problems, ['.coverity-triage/config.yaml がない']);
  assert.match(setup.setupHint, new RegExp(`/${h.defaults.setupSkill}`));
});

test('REQ-407 open while a run is active: the same URL, the busy notice, no new run, the request is ignored', async (t) => {
  const h = await startWith(t);
  const first = await h.open();
  await openAndStart(h);
  const runId = h.ctx.run.id;
  const again = await h.open('High だけ別の作業を');
  const url = (x) => x.text.match(/http:\/\/\S+/)[0];
  assert.equal(url(again), url(first));
  assert.match(again.text, /いまの実行が終わるまで、新しい作業は始められません。今の画面で実行の様子を見られます。変えた作業を頼むときは、画面で止めてから、もう一度チャットで頼んでください。/);
  assert.equal(h.ctx.run.id, runId);
  assert.equal(h.runner.callsOf('run:new-run').length, 1);
  assert.equal(h.state().setup.request.text, 'Coverity の警告をトリアージして');
});

test('a failure inside the tool returns a failure result (never throws)', async (t) => {
  const h = await startWith(t);
  h.core.open = async () => {
    throw new Error('boom');
  };
  const r = await h.open();
  assert.equal(r.thrown, undefined);
  assert.equal(r.ok, false);
  assert.equal(r.resultType, 'failure');
  assert.match(r.error, /Web管理画面を開けませんでした/);
});
