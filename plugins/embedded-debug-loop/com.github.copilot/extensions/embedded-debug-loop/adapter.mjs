// Copilot CLI とのつなぎ口。joinSession を引数で受け取る（試験では偽物を渡す）。
// - 記録のツールと、フック（onPreToolUse・onSubagentStop）を登録する。
// - セッションのイベントを受けて、実行の記録にする。
// - Web管理画面のサーバーは、最初に open のツールが呼ばれたときに起動する。
// 作業エージェント・検証エージェント・統括エージェントの名前などの既定は、同じフォルダの defaults.json から読む（中身はただの文字として扱う）。
// 設定は、セッションの作業フォルダごとに保存する。
// 標準出力は Copilot CLI との通信に使われるので、console.log は使わない。

import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createControl } from './core/control.mjs';
import { createEventSink } from './core/events.mjs';
import { createGuard } from './core/guard.mjs';
import { createPersist } from './core/persist.mjs';
import { createStore } from './core/store.mjs';
import { createTools, runCommand } from './core/tools.mjs';
import { startServer } from './ui/server.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA_NAME = 'embedded-debug-loop';

/** defaults.json を読む。読めなければ空の既定にする（画面で選んでもらう） */
export function readDefaults(file) {
  try {
    const value = JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
    return value && typeof value === 'object' ? value : {};
  } catch (error) {
    console.error(`[Web管理画面] 既定の設定（${file}）を読めませんでした：${error.message}`);
    return {};
  }
}

export function defaultDataDir(env = process.env) {
  return path.join(env.COPILOT_HOME || path.join(os.homedir(), '.copilot'), 'plugin-data', DATA_NAME);
}

/**
 * @param {object} options
 * @param {Function} options.joinSession `@github/copilot-sdk/extension` の joinSession
 * @param {string} [options.dataDir] 記録の置き場所（試験用。既定は COPILOT_HOME の下）
 * @param {string} [options.defaultsFile] 既定の設定のファイル（試験用）
 * @param {string} [options.staticDir] 画面のファイルの置き場所（試験用）
 * @param {Function} [options.runCommand] gh を動かす関数（試験用）
 * @param {Function} [options.now] 今の時刻を返す関数（試験用）
 */
export async function startAdapter({ joinSession, dataDir, defaultsFile, staticDir, runCommand: run = runCommand, now } = {}) {
  if (typeof joinSession !== 'function') throw new Error('joinSession が渡されていません。');
  const defaults = readDefaults(defaultsFile ?? path.join(HERE, 'defaults.json'));
  const persist = createPersist(dataDir ?? defaultDataDir());
  const store = createStore({ defaults, persist, ...(now ? { now } : {}) });

  let session = null;
  const need = () => {
    if (!session) throw new Error('Copilot CLI とまだつながっていません。');
    return session;
  };
  const port = {
    send: (prompt) => need().send({ prompt }),
    abort: () => need().abort(),
    // チャットに知らせを出す（手番は始まらない）
    log: (message, options) => need().log(message, options),
    getSubagentSettings: () => need().rpc.tools.getSubagentSettings(),
    updateSubagentSettings: (value) => need().rpc.tools.updateSubagentSettings(value),
    listAgents: () => need().rpc.agent.list(),
    listModels: () => need().rpc.model.list(),
    // チャットで選ばれているエージェント。この版の Copilot CLI になければ null
    currentAgent: async () => (typeof need().rpc.agent.getCurrent === 'function' ? need().rpc.agent.getCurrent() : null),
    // チャットで使うエージェントを選ぶ（「開始」の知らせを統括エージェントに届けるため）。この版になければ false
    selectAgent: async (name) => {
      if (typeof need().rpc.agent.select !== 'function') return false;
      await need().rpc.agent.select({ name });
      return true;
    },
    // セッションの作業フォルダ（/cd で変わる）。読めなければ、このプロセスの今のフォルダ
    workingDirectory: async () => {
      try {
        const dir = (await need().rpc.metadata?.snapshot?.())?.workingDirectory;
        if (typeof dir === 'string' && dir.trim()) return dir;
      } catch (error) {
        console.error('[Web管理画面] セッションの作業フォルダを読めませんでした：', error?.message ?? error);
      }
      return process.cwd();
    },
  };
  const control = createControl({ store, port, persist, orchestrator: String(defaults.orchestrator ?? '') });

  let server = null;
  let opening = null;
  async function ensureServer() {
    if (server) return server.url;
    opening ??= startServer({ staticDir: staticDir ?? path.join(HERE, 'ui', 'static'), api: control }).finally(() => {
      opening = null;
    });
    server = await opening;
    store.url = server.url;
    return server.url;
  }

  const sink = createEventSink({ store, onStopped: control.announceStopped });
  const tools = createTools({
    store,
    ensureServer,
    refreshOptions: control.refreshOptions,
    refreshFolder: control.refreshFolder,
    issueRepo: String(defaults.issueRepo ?? ''),
    run,
  });
  session = await joinSession({
    tools,
    hooks: { onPreToolUse: createGuard({ store, mainSessionId: () => session?.sessionId }), onSubagentStop: sink.onSubagentStop },
  });
  const unsubscribe = session.on((event) => sink.onEvent(event));
  control.refreshFolder(); // 作業フォルダの設定を、先に読んでおく（待たない）

  let closing = null;
  function shutdown() {
    closing ??= (async () => {
      try {
        unsubscribe();
      } catch {
        // セッションがもう切れていてもよい
      }
      store.flush();
      if (server) await server.close();
    })();
    return closing;
  }

  return { session, store, control, ensureServer, shutdown };
}
