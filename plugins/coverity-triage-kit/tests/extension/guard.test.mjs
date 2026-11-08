// design 8.3: onPreToolUse — the orchestrator's allowlist, review.json only for the reviewer, never throws.
import assert from 'node:assert/strict';
import test from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { importExt, openAndStart, startWith } from './helpers.mjs';

async function running(t) {
  const h = await startWith(t);
  const root = await openAndStart(h);
  const { worker, reviewer } = h.defaults.roles;
  h.fake.emit(sdkEvent.subagentStarted('w1', { parentId: root, agentType: worker }));
  h.fake.emit(sdkEvent.subagentStarted('r1', { parentId: root, agentType: reviewer }));
  await h.idle();
  const ask = async (agentId, toolName, toolArgs, extra = {}) => h.fake.callHook('onPreToolUse', { agentId, toolName, toolArgs, ...extra });
  return { h, root, ask, run: h.ctx.commands.runScript, gui: h.ctx.commands.guiScript };
}

const denied = (r) => r?.permissionDecision === 'deny';

test('the orchestrator may read, search, call agents, use skill and the Coverity read tools', async (t) => {
  const { root, ask } = await running(t);
  for (const tool of ['view', 'grep', 'glob', 'rg', 'task', 'read_agent', 'list_agents', 'write_agent', 'skill', 'read_bash', 'stop_bash']) {
    assert.equal(await ask(root, tool, {}), undefined, tool);
  }
  for (const tool of ['coverity_search_issues', 'coverity-connect/coverity_get_issue_details', 'coverity-connect-coverity_check_connection', 'mcp__coverity-connect__coverity_preview_triage']) {
    assert.equal(await ask(root, tool, {}), undefined, tool);
  }
});

test('the orchestrator may not edit, write, fetch, use other tools, or write back to Coverity', async (t) => {
  const { root, ask } = await running(t);
  for (const tool of ['edit', 'create', 'apply_patch', 'web_fetch', 'write_bash', 'coverity-connect/coverity_update_triage', 'sql', 'todo', '']) {
    const r = await ask(root, tool, { path: '/x' });
    assert.ok(denied(r), tool);
    assert.match(r.permissionDecisionReason, /統括エージェントは、ツール/);
  }
});

test('the orchestrator may run only this run command or the screen command, one at a time', async (t) => {
  const { root, ask, run, gui } = await running(t);
  const ok = [
    `uv run --native-tls --quiet "${run}" tick --run "/out/20261108-101500" --wait 20`,
    `uv run --native-tls --quiet "${run}" note-issue --run "/o" --stdin`,
    `uv run --native-tls --quiet "${gui}" open --repo "/r" --no-browser 2>&1`,
    `uv run --native-tls --quiet "${run.replace(/\//g, '\\')}" status --run "C:\\o"`,
  ];
  for (const command of ok) {
    assert.equal(await ask(root, 'bash', { command }), undefined, command);
    assert.equal(await ask(root, 'powershell', JSON.stringify({ command })), undefined, `powershell ${command}`);
  }
  const bad = [
    'rm -rf /',
    `uv run --native-tls --quiet "${run}" tick --run "/o"; rm -rf /`,
    `uv run --native-tls --quiet "${run}" tick && curl http://x`,
    `uv run --native-tls --quiet "${run}" tick | tee x`,
    `uv run --native-tls --quiet "${run}" tick > /tmp/x`,
    `uv run --native-tls --quiet "${run}" $(whoami)`,
    'uv run --native-tls --quiet "/elsewhere/run.py" tick',
    `python "${run}" tick`,
    `uv run python -c "import os" "${run}"\nrm x`,
    `uv run python -c "import os" "${run}"`,
    `uv run --with requests "${run}" tick`,
  ];
  for (const command of bad) {
    const r = await ask(root, 'bash', { command });
    assert.ok(denied(r), command);
    assert.match(r.permissionDecisionReason, /シェルで動かせるのは/);
  }
});

test('only the reviewer may write items/<ID>/review.json', async (t) => {
  const { ask } = await running(t);
  const path = '/out/20261108-101500/items/20001/review.json';
  const r = await ask('w1', 'edit', { path });
  assert.ok(denied(r));
  assert.match(r.permissionDecisionReason, /検証役のエージェントだけ/);
  assert.ok(denied(await ask('w1', 'create', { file_path: 'C:\\out\\items\\G1\\review.json' })));
  assert.ok(denied(await ask('w1', 'apply_patch', { input: `*** Begin Patch\n*** Add File: ${path}\n+{}\n*** End Patch` })));
  assert.ok(denied(await ask('w1', 'bash', { command: `echo {} > "${path}"` })));
  assert.equal(await ask('w1', 'edit', { path: '/out/20261108-101500/items/20001/result.json' }), undefined);
  assert.equal(await ask('r1', 'create', { path }), undefined, 'the reviewer may');
  // a child of a worker that the extension has not seen yet still counts (its parent is in the tree)
  assert.ok(denied(await ask('w1-child', 'edit', { path }, { parentAgentId: 'w1' })));
});

test('the main chat agent and agents outside this run are never denied; no run → nothing is denied', async (t) => {
  const { ask } = await running(t);
  assert.equal(await ask(undefined, 'edit', { path: '/x/items/1/review.json' }), undefined);
  assert.equal(await ask('stranger', 'edit', { path: '/x/items/1/review.json' }), undefined);
  assert.equal(await ask('stranger', 'bash', { command: 'rm -rf /' }), undefined);
  const h2 = await startWith(t);
  assert.equal(await h2.fake.callHook('onPreToolUse', { agentId: 'ag-root-1', toolName: 'edit' }), undefined);
});

test('the hook never throws, whatever it gets', async (t) => {
  const { h, root } = await running(t);
  const hook = h.fake.config.hooks.onPreToolUse;
  const nasty = [undefined, null, 'x', 1, [], {}, { agentId: root }, { agentId: root, toolName: 5 }, { agentId: root, toolName: 'bash', toolArgs: '{not json' }];
  const throwing = {};
  Object.defineProperty(throwing, 'agentId', {
    get() {
      throw new Error('boom');
    },
  });
  nasty.push(throwing);
  for (const input of nasty) assert.doesNotThrow(() => hook(input));
});

test('isAllowedCommand: compares paths with either slash and any case', async () => {
  const { isAllowedCommand } = await importExt('core/guard.mjs');
  assert.equal(isAllowedCommand('uv run --native-tls --quiet "C:/Plugin/Skills/run.py" status', ['c:\\plugin\\skills\\run.py']), true);
  assert.equal(isAllowedCommand("uv.exe run --quiet 'C:/p/run.py' tick", ['C:\\p\\run.py']), true);
  assert.equal(isAllowedCommand('uv run C:/p/run.py.bak tick', ['C:/p/run.py']), false);
  assert.equal(isAllowedCommand('', ['x']), false);
  assert.equal(isAllowedCommand('uv run x', ['']), false);
});
