// REQ-422: issue drafts (mechanical and from the orchestrator) and filing with gh (repo from defaults.json issueRepo).
import assert from 'node:assert/strict';
import test from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { finishedStatus, importExt, openAndStart, startWith, statusOf } from './helpers.mjs';

async function finished(t, options = {}) {
  const h = await startWith(t, options);
  h.runner.on('run:status', () => statusOf({ recent: [] }));
  const root = await openAndStart(h);
  await h.clock.advance(1);
  // something to report: a failing tool and a model that differs from the chosen one
  h.fake.emit(sdkEvent.subagentStarted('w1', { parentId: root, agentType: h.defaults.roles.worker, model: 'claude-haiku-5.5' }));
  h.fake.emit(sdkEvent.toolStart('w1', 't1', 'view', { path: '/x' }));
  h.fake.emit(sdkEvent.toolComplete('w1', 't1', { success: false, error: 'ファイルがない' }));
  h.fake.emit(sdkEvent.subagentCompleted('w1'));
  await h.idle();
  h.runner.on('run:status', () => finishedStatus({ issues_found: [{ title: '指示ファイルが長い', body: '短くしたい', at: '2026-11-08T01:00:00Z' }] }));
  h.fake.emit(sdkEvent.agentIdle(root));
  await h.idle();
  await h.clock.advance(500);
  await h.idle();
  assert.equal(h.ctx.run.status, 'finished');
  return h;
}

test('REQ-422 a finished run lists mechanical issues (M…) and the orchestrator\'s issues (O…)', async (t) => {
  const h = await finished(t);
  const issues = h.state().run.issues;
  assert.deepEqual(
    issues.map((i) => [i.id, i.source]),
    [['M1', 'mechanical'], ['M2', 'mechanical'], ['O1', 'orchestrator']],
  );
  assert.match(issues[0].title, /ツール「view」が失敗しました（作業エージェント）/);
  assert.match(issues[1].title, /選んだモデルと実際のモデルが違いました（作業エージェント）/);
  assert.equal(issues[2].title, '指示ファイルが長い');
  assert.ok(issues.every((i) => i.filing.state === 'none'));
});

test('REQ-422 approving one draft files only that one with gh, to defaults.json issueRepo, with a marker', async (t) => {
  const h = await finished(t);
  await h.core.fileIssue('O1');
  const creates = h.runner.callsOf('gh:issue').filter((c) => c.args[1] === 'create');
  assert.equal(creates.length, 1);
  assert.deepEqual(creates[0].args, ['issue', 'create', '--repo', h.defaults.issueRepo, '--title', '指示ファイルが長い', '--body-file', '-']);
  assert.match(creates[0].input, /^短くしたい\n\n<!-- coverity-triage-kit-issue:[\w-]+\/O1 -->\n$/);
  const issues = h.state().run.issues;
  assert.deepEqual(issues.find((i) => i.id === 'O1').filing, { state: 'filed', url: 'https://github.com/o/r/issues/7', reason: null });
  assert.equal(issues.find((i) => i.id === 'M1').filing.state, 'none');
  await assert.rejects(h.core.fileIssue('O1'), /すでに起票しました/);
  await assert.rejects(h.core.fileIssue('X9'), /見つかりません/);
});

test('REQ-422 gh fails → "failed" with the reason; a timeout → "unknown", and the retry searches before filing again', async (t) => {
  const h = await finished(t);
  h.runner.on('gh:issue', ({ rest }) => (rest[0] === 'create' ? { $raw: { code: 1, stderr: 'HTTP 403' } } : { $raw: { code: 0, stdout: '[]' } }));
  await assert.rejects(h.core.fileIssue('M1'), /gh が失敗しました。（HTTP 403）/);
  assert.equal(h.state().run.issues[0].filing.state, 'failed');

  h.runner.on('gh:issue', ({ rest }) => (rest[0] === 'create' ? { $raw: { timedOut: true } } : { $raw: { code: 0, stdout: '[]' } }));
  await assert.rejects(h.core.fileIssue('M1'), /起票できたかは分かりません/);
  assert.equal(h.state().run.issues[0].filing.state, 'unknown');

  let listed = 0;
  h.runner.on('gh:issue', ({ rest, opts }) => {
    if (rest[0] === 'list') {
      listed += 1;
      return { $raw: { code: 0, stdout: JSON.stringify([{ title: 'x', url: 'https://github.com/o/r/issues/9', body: `本文\n${opts.args.join(' ')}` }, { title: 'y', url: 'https://github.com/o/r/issues/8', body: `<!-- coverity-triage-kit-issue:${h.ctx.run.id}/M1 -->` }]) } };
    }
    throw new Error('must not create again');
  });
  await h.core.fileIssue('M1');
  assert.equal(listed, 1);
  assert.deepEqual(h.state().run.issues[0].filing, { state: 'filed', url: 'https://github.com/o/r/issues/8', reason: null });
});

test('REQ-422 when filing is impossible, the drafts stay and the steps are shown (gh missing / not logged in / no repository)', async (t) => {
  const noGh = await finished(t);
  noGh.runner.on('gh:--version', { $raw: { spawnError: 'ENOENT' } });
  noGh.ctx.canFileChecked = false;
  await assert.rejects(noGh.core.fileIssue('M1'), /今は Issue を起票できません。gh（GitHub の公式のコマンド）が見つかりません/);
  const view = noGh.state().run;
  assert.equal(view.canFile.ok, false);
  assert.ok(view.canFile.steps.some((s) => /gh auth login/.test(s)));
  assert.ok(view.canFile.steps.some((s) => s.includes(noGh.defaults.issueRepo)));
  assert.equal(view.issues.length, 3);

  const { checkCanFile } = await importExt('core/filing.mjs');
  const runner = async ({ args }) => (args[0] === 'auth' ? { code: 1 } : { code: 0 });
  const base = { runner, ghCommand: 'gh', clock: noGh.clock };
  const notLogged = await checkCanFile({ ...base, defaults: { issueRepo: 'o/r' } });
  assert.deepEqual([notLogged.ok, notLogged.code], [false, 'not_logged_in']);
  const noRepo = await checkCanFile({ ...base, runner: async () => ({ code: 0 }), defaults: { issueRepo: '' } });
  assert.deepEqual([noRepo.ok, noRepo.code], [false, 'no_repository']);
  assert.ok(noRepo.steps.some((s) => s.includes('issueRepo')));
  const bad = await checkCanFile({ ...base, runner: async () => ({ code: 0 }), defaults: { issueRepo: 'not a repo' } });
  assert.equal(bad.code, 'not_github');
  const good = await checkCanFile({ ...base, runner: async () => ({ code: 0 }), defaults: { issueRepo: 'https://github.com/o/r.git' } });
  assert.deepEqual([good.ok, good.repo], [true, 'o/r']);
});

test('REQ-422 a stopped run runs no agent for the review and shows only mechanical issues', async (t) => {
  const h = await startWith(t);
  h.runner.on('run:status', () => statusOf({ issues_found: [{ title: '統括の課題', body: 'b' }] }));
  await openAndStart(h);
  await h.clock.advance(1);
  await h.stop();
  await h.idle();
  const run = h.state().run;
  assert.ok(run.issues.every((i) => i.source === 'mechanical'));
  assert.equal(h.fake.callsOf('tasks.startAgent').length, 1);
});

test('two approvals at once file one issue (the marker is created exclusively)', async (t) => {
  const h = await finished(t);
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  h.runner.on('gh:issue', async () => {
    await gate;
    return { $raw: { code: 0, stdout: 'https://github.com/o/r/issues/5' } };
  });
  const a = h.core.fileIssue('M2');
  const b = h.core.fileIssue('M2');
  await assert.rejects(b, /いま起票しています/);
  release();
  await a;
  assert.equal(h.runner.callsOf('gh:issue').length, 1);
});

test('REQ-422 issue drafts are masked before they are shown and filed', async () => {
  const { createMasker } = await importExt('core/mask.mjs');
  const mask = createMasker({ env: { U_ENV: 'taro.yamada', K_ENV: 'k3y-Secret-0123' }, names: ['U_ENV', 'K_ENV'], user: 'taroy', home: 'C:\\Users\\taroy' });
  const text = 'HTTP 401 for taro.yamada with k3y-Secret-0123 at https://cov.corp.example:8443/api; file C:\\Users\\taroy\\repo\\a.c, C:/Users/taroy/x, ' +
    'mail taro@corp.example, host 10.1.2.3, see https://github.com/o/r/issues/1';
  const out = mask(text);
  for (const leak of ['taro.yamada', 'k3y-Secret', 'cov.corp.example', 'Users\\taroy', 'Users/taroy', 'taro@corp', '10.1.2.3']) assert.ok(!out.includes(leak), `${leak} in ${out}`);
  assert.ok(out.includes('https://github.com/o/r/issues/1'), 'GitHub links stay');
  const { numberIssues } = await importExt('core/analysis.mjs');
  const [draft] = numberIssues([{ title: 'x 10.0.0.1', body: 'y' }], 'M', 'mechanical', mask);
  assert.equal(draft.title, 'x *.*.*.*');
});
