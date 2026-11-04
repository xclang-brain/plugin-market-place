// 試験の道具：偽物の joinSession（Copilot CLI のセッションの代わり）と、一時フォルダ、HTTP の呼び出し。

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { startAdapter } from '../../com.github.copilot/extensions/embedded-debug-loop/adapter.mjs';

export const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const EXT_DIR = path.join(PLUGIN_ROOT, 'com.github.copilot', 'extensions', 'embedded-debug-loop');

export const WORKER = 'demo-plugin:helper-a';
export const VERIFIER = 'demo-plugin:checker-b';

export function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'edl-ext-'));
  t?.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

/** 偽物のセッション。登録されたツール・フックと、送った知らせ・止めた回数・子の設定の書き換えを残す */
export function fakeCopilot({ agents, models, subagents = null, sendFails = false, onAbort } = {}) {
  const handlers = [];
  const fake = {
    config: null,
    sent: [],
    aborts: 0,
    updates: [],
    subagents,
    tool(name) {
      return fake.config.tools.find((t) => t.name === `embedded-debug-loop_${name}`);
    },
    async call(name, args = {}) {
      const result = await fake.tool(name).handler(args, { sessionId: 's1', toolCallId: 'c1', toolName: name });
      return typeof result === 'string' ? { ok: true, text: result } : { ok: result.resultType !== 'failure', text: result.textResultForLlm };
    },
    pre(input) {
      return fake.config.hooks.onPreToolUse({ sessionId: 's1', timestamp: new Date(), workingDirectory: '/w', ...input }, { sessionId: 's1' });
    },
    subagentStop(input) {
      return fake.config.hooks.onSubagentStop({ sessionId: 's1', timestamp: new Date(), workingDirectory: '/w', stopReason: 'end_turn', ...input }, { sessionId: 's1' });
    },
    raw(event) {
      for (const h of [...handlers]) h(event);
    },
    emit(type, data = {}, agentId) {
      const event = { id: `e${Math.random()}`, parentId: null, timestamp: new Date().toISOString(), type, data, ...(agentId ? { agentId } : {}) };
      for (const h of [...handlers]) h(event);
    },
  };
  fake.session = {
    sessionId: 's1',
    on(typeOrHandler, maybe) {
      const h = typeof typeOrHandler === 'function' ? typeOrHandler : (e) => e.type === typeOrHandler && maybe(e);
      handlers.push(h);
      return () => handlers.splice(handlers.indexOf(h), 1);
    },
    async send(options) {
      if (sendFails) throw new Error('接続が切れています');
      fake.sent.push(typeof options === 'string' ? options : options.prompt);
      return 'm1';
    },
    async abort() {
      fake.aborts += 1;
      onAbort?.(fake);
    },
    async log() {},
    rpc: {
      agent: {
        list: async () => ({
          agents: agents ?? [
            { name: 'helper-a', id: WORKER, displayName: 'A', description: '手伝う' },
            { name: 'checker-b', id: VERIFIER, displayName: 'B', description: '確かめる' },
          ],
        }),
      },
      model: { list: async () => ({ list: models ?? [{ id: 'model-x', name: 'モデル X' }, { id: 'model-y' }] }) },
      tools: {
        getSubagentSettings: async () => ({ subagents: fake.subagents }),
        updateSubagentSettings: async ({ subagents: value }) => {
          fake.updates.push(value);
          fake.subagents = value;
          return {};
        },
      },
    },
  };
  fake.joinSession = async (config) => {
    fake.config = config;
    return fake.session;
  };
  return fake;
}

/** つなぎ口を、偽物のセッションと一時フォルダで起こす */
export async function startWith(t, options = {}) {
  const dir = tempDir(t);
  const defaultsFile = path.join(dir, 'defaults.json');
  fs.writeFileSync(defaultsFile, JSON.stringify({ worker: WORKER, verifier: VERIFIER, maxIterations: 3, issueRepo: 'owner/repo', ...options.defaults }));
  const staticDir = path.join(dir, 'static');
  fs.mkdirSync(staticDir);
  fs.writeFileSync(path.join(staticDir, 'index.html'), '<!doctype html><title>画面</title><script src="app.js"></script>');
  fs.writeFileSync(path.join(staticDir, 'app.js'), 'console.log(1)');
  fs.writeFileSync(path.join(staticDir, 'style.css'), 'body{}');
  const fake = fakeCopilot(options.fake);
  const dataDir = options.dataDir ?? path.join(dir, 'data');
  const app = await startAdapter({ joinSession: fake.joinSession, dataDir, defaultsFile, staticDir, runCommand: options.runCommand });
  t.after(() => app.shutdown());
  return { app, fake, dir, dataDir };
}

/** 開く → 準備 → 開始 まで進める */
export async function startRun(t, options = {}) {
  const ctx = await startWith(t, options);
  const { fake, app } = ctx;
  await fake.call('open', { request: '表示の不具合を直す', criteria: ['画面に「完了」と出る', 'テストがすべて通る'] });
  if (options.prepare !== false) await fake.call('prepare', { worker: WORKER, ready: true, report: '準備：できている\n要約' });
  await app.control.start({});
  return ctx;
}

export async function http(url, { method = 'GET', body, headers = {} } = {}) {
  const res = await fetch(url, {
    method,
    headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
  });
  const type = res.headers.get('content-type') ?? '';
  const data = type.includes('json') ? await res.json() : await res.text();
  return { status: res.status, data, headers: res.headers };
}
