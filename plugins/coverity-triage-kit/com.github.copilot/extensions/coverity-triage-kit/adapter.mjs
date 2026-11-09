// The only Copilot-CLI-dependent part. It receives joinSession as an argument (tests pass a fake),
// registers the tool, the hooks and the permission handler, turns SDK events into neutral events,
// builds the port from session.rpc, and starts the core and (lazily) the screen.
// stdout is Copilot CLI's JSON-RPC channel: never console.log here; diagnostics go to console.error.

import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { realClock } from './core/clock.mjs';
import { readDefaultsFile } from './core/defaults.mjs';
import { createCore } from './core/index.mjs';
import { OPEN_FAILED, OPEN_PARAM_DESCRIPTION, OPEN_TOOL_DESCRIPTION, openText } from './core/messages.mjs';
import { normalizeModels } from './core/models.mjs';
import { runProcess } from './core/process.mjs';
import { strings } from './core/util.mjs';
import { startApp } from './ui/app.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TASK_TOOL = 'task'; // the orchestrator starts agents with it; its `description` becomes the slot's assignment
const UNKNOWN_TOOL = '不明なツール';

function readPluginName(pluginRoot) {
  try {
    const name = JSON.parse(readFileSync(path.join(pluginRoot, 'plugin.json'), 'utf8').replace(/^﻿/, ''))?.name;
    if (typeof name === 'string' && name !== '') return name;
  } catch {
    // fall back to the extension folder name
  }
  return path.basename(HERE);
}

async function readWorkingDirectory(session) {
  try {
    const cwd = (await session.rpc.workspaces?.getWorkspace?.())?.workspace?.cwd;
    if (typeof cwd === 'string' && cwd !== '') return cwd;
  } catch {
    // try the next way
  }
  try {
    const dir = (await session.rpc.metadata?.snapshot?.())?.workingDirectory;
    if (typeof dir === 'string' && dir !== '') return dir;
  } catch {
    // the process folder (Copilot CLI starts the extension in the session folder)
  }
  return process.cwd();
}

// ---- permission requests → wait details ----------------------------------------------------

function toolKindOf(request, kind) {
  const named = (...names) => names.filter((n) => typeof n === 'string' && n !== '').join('/');
  if (kind === 'mcp') return named(request.serverName, request.toolName) || kind;
  if (kind === 'custom-tool' || kind === 'hook') return named(request.toolName) || kind;
  return kind;
}

export function describePermission(request) {
  const req = request !== null && typeof request === 'object' ? request : {};
  const kind = typeof req.kind === 'string' ? req.kind : '';
  const detail = { permissionKind: kind, toolKind: toolKindOf(req, kind) };
  switch (kind) {
    case 'shell':
      detail.command = String(req.fullCommandText ?? '');
      detail.paths = strings(req.possiblePaths);
      detail.urls = strings((Array.isArray(req.possibleUrls) ? req.possibleUrls : []).map((u) => (typeof u === 'string' ? u : u?.url)));
      if (typeof req.warning === 'string' && req.warning !== '') detail.warning = req.warning;
      break;
    case 'write':
      detail.paths = strings([req.fileName]);
      break;
    case 'read':
      detail.paths = strings([req.path]);
      break;
    case 'url':
      detail.urls = strings([req.url]);
      break;
    default:
      break;
  }
  detail.sandboxPermissive = Boolean(req.requestSandboxPermissive);
  detail.sandboxBypass = Boolean(req.requestSandboxBypass) && !detail.sandboxPermissive;
  return detail;
}

// ---- SDK events → neutral events ------------------------------------------------------------

function timeOf(event) {
  const at = Date.parse(event.timestamp);
  return Number.isFinite(at) ? at : Date.now();
}

const hasRequestId = (data) => typeof data.requestId === 'string' && data.requestId !== '';
const nonEmpty = (v) => (typeof v === 'string' && v.trim() !== '' ? v : undefined);

export function createEventConverter() {
  const parents = new Map();
  const toolNames = new Map();
  const lastReplies = new Map();
  const calledTasks = new Map();

  function convert(event) {
    if (event === null || typeof event !== 'object' || typeof event.type !== 'string') return null;
    const data = event.data !== null && typeof event.data === 'object' ? event.data : {};
    const at = timeOf(event);
    const agentId = typeof event.agentId === 'string' && event.agentId !== '' ? event.agentId : undefined;
    switch (event.type) {
      case 'subagent.started': {
        if (!agentId) return null;
        const parentAgentId = nonEmpty(data.parentId);
        if (parentAgentId) parents.set(agentId, parentAgentId);
        const called = calledTasks.get(data.toolCallId);
        calledTasks.delete(data.toolCallId);
        return {
          type: 'agent.started',
          at,
          agentId,
          parentAgentId,
          agentType: nonEmpty(data.agentType) ?? nonEmpty(data.agentName) ?? '',
          model: data.model,
          executionMode: data.executionMode,
          description: parentAgentId ? (called ?? nonEmpty(data.agentDisplayName) ?? nonEmpty(data.agentDescription)) : undefined,
        };
      }
      case 'subagent.configured':
        return agentId ? { type: 'agent.configured', at, agentId, model: data.model } : null;
      case 'subagent.completed': {
        if (!agentId) return null;
        const response = lastReplies.get(agentId);
        lastReplies.delete(agentId);
        return { type: 'agent.completed', at, agentId, cancelled: Boolean(data.cancelled), response };
      }
      case 'subagent.failed':
        return agentId ? { type: 'agent.failed', at, agentId, error: typeof data.error === 'string' ? data.error : data.error?.message } : null;
      case 'system.notification':
        return data.kind?.type === 'agent_idle' && typeof data.kind.agentId === 'string' ? { type: 'agent.idle', at, agentId: data.kind.agentId } : null;
      case 'tool.execution_start':
        if (typeof data.toolCallId === 'string') {
          toolNames.set(data.toolCallId, data.toolName);
          if (data.toolName === TASK_TOOL && typeof data.arguments?.description === 'string') calledTasks.set(data.toolCallId, data.arguments.description);
        }
        return { type: 'tool.started', at, agentId, toolCallId: data.toolCallId, toolName: data.toolName, arguments: data.arguments };
      case 'tool.execution_complete': {
        const toolName = toolNames.get(data.toolCallId) ?? UNKNOWN_TOOL;
        toolNames.delete(data.toolCallId);
        calledTasks.delete(data.toolCallId);
        const error = typeof data.error === 'string' ? data.error : data.error?.message;
        return { type: 'tool.completed', at, agentId, toolCallId: data.toolCallId, toolName, success: data.success, error };
      }
      case 'permission.requested':
        return hasRequestId(data)
          ? { type: 'wait.started', at, requestId: data.requestId, agentId, kind: 'permission', detail: describePermission(data.permissionRequest), resolvedByHook: Boolean(data.resolvedByHook) }
          : null;
      case 'user_input.requested':
      case 'elicitation.requested':
        return hasRequestId(data)
          ? { type: 'wait.started', at, requestId: data.requestId, agentId, kind: 'question', detail: { question: String(data.question ?? data.message ?? '') }, resolvedByHook: false }
          : null;
      case 'session_limits_exhausted.requested':
        return hasRequestId(data) ? { type: 'wait.started', at, requestId: data.requestId, agentId, kind: 'limit', detail: {}, resolvedByHook: false } : null;
      case 'permission.completed':
        return hasRequestId(data) ? { type: 'wait.ended', at, requestId: data.requestId, kind: 'permission' } : null;
      case 'user_input.completed':
      case 'elicitation.completed':
        return hasRequestId(data) ? { type: 'wait.ended', at, requestId: data.requestId, kind: 'question' } : null;
      case 'session_limits_exhausted.completed':
        return hasRequestId(data) ? { type: 'wait.ended', at, requestId: data.requestId, kind: 'limit', action: data.response?.action } : null;
      default:
        return null;
    }
  }

  return {
    convert,
    parentOf: (agentId) => parents.get(agentId) ?? null,
    noteReply(agentId, reply) {
      if (typeof agentId === 'string' && agentId !== '' && typeof reply === 'string') lastReplies.set(agentId, reply);
    },
  };
}

// ---- the port, built on session.rpc -----------------------------------------------------------

function createPort(session, converter) {
  return {
    async startAgent({ agentType, name, model, prompt }) {
      const started = await session.rpc.tasks.startAgent({ agentType, name, prompt, ...(model ? { model } : {}) });
      return { agentId: started?.agentId };
    },
    async cancelAgent(agentId) {
      await session.rpc.tasks.cancel({ id: agentId });
    },
    // tasks.list holds shells too: agents only. It has no parent id, so the converter remembers them.
    async listTasks() {
      const listed = await session.rpc.tasks.list();
      return (Array.isArray(listed?.tasks) ? listed.tasks : [])
        .filter((t) => t?.type === 'agent')
        .map((t) => ({ agentId: t.id, displayName: t.displayName ?? '', status: t.status, agentType: t.agentType ?? '', parentAgentId: converter.parentOf(t.id) }));
    },
    // map: { '<agent id>': { model?, effort? } }; null removes this session's overrides (all of them)
    async setSubagentSettings(map) {
      const entryOf = (s) => ({ ...(s?.model ? { model: s.model } : {}), ...(s?.effort ? { effortLevel: s.effort } : {}) });
      const agents = map === null || map === undefined ? null : Object.fromEntries(Object.entries(map).map(([agent, s]) => [agent, entryOf(s)]));
      await session.rpc.tools.updateSubagentSettings({ subagents: agents === null ? null : { agents } });
    },
    async getCurrentModel() {
      const current = await session.rpc.model.getCurrent();
      const model = typeof current?.modelId === 'string' ? current.modelId : '';
      return { model, isAuto: model.trim().toLowerCase() === 'auto' };
    },
    async listModels() {
      const listed = await session.rpc.model.list();
      return normalizeModels(listed?.list ?? listed?.models ?? listed);
    },
  };
}

/**
 * @param {object} options
 * @param {Function} options.joinSession  `@github/copilot-sdk/extension` joinSession (a fake in tests)
 * @param {string} [options.pluginRoot]   the plugin folder (default: three folders up)
 * @param {string} [options.defaultsFile] defaults.json (default: next to this file)
 * @param {string} [options.dataRoot]     records folder (default: <COPILOT_HOME or ~/.copilot>/plugin-data/<plugin>/runs)
 * @param {Function} [options.runner]     child process runner (tests pass a fake)
 * @param {object} [options.clock]
 * @param {string} [options.staticDir]
 * @param {number} [options.preferredPort] default: defaults.json port
 */
export async function startAdapter(options = {}) {
  const { joinSession } = options;
  if (typeof joinSession !== 'function') throw new Error('joinSession が渡されていません。');
  const pluginRoot = options.pluginRoot ?? path.resolve(HERE, '..', '..', '..');
  const pluginName = readPluginName(pluginRoot);
  const defaults = readDefaultsFile(options.defaultsFile ?? path.join(HERE, 'defaults.json'));
  const copilotHome = process.env.COPILOT_HOME || path.join(os.homedir(), '.copilot');
  const dataRoot = options.dataRoot ?? path.join(copilotHome, 'plugin-data', pluginName, 'runs');
  const runner = options.runner ?? runProcess;
  const clock = options.clock ?? realClock;
  const converter = createEventConverter();
  const toolName = `${pluginName}_open`;

  let core = null;
  let markReady;
  let markFailed;
  const ready = new Promise((resolve, reject) => {
    markReady = resolve;
    markFailed = reject;
  });
  ready.catch(() => {});

  // Never throw from a handler: a throw reaches the model only as "Tool execution failed"
  async function openHandler(args) {
    try {
      const readyCore = await ready;
      return openText(await readyCore.open({ requestText: args?.requestText }));
    } catch (error) {
      console.error(`[Web管理画面] ツール「${toolName}」の処理で失敗しました：`, error);
      return { textResultForLlm: OPEN_FAILED, resultType: 'failure' };
    }
  }

  // Answers at once and never throws (a throw lets the call through)
  function onPreToolUse(input) {
    try {
      const verdict = core?.preToolUse(input);
      if (verdict?.deny) return { permissionDecision: 'deny', permissionDecisionReason: String(verdict.reason ?? '') };
    } catch (error) {
      console.error('[Web管理画面] ツールの問い合わせを処理できませんでした：', error);
    }
    return undefined;
  }

  function onSubagentStop(input) {
    try {
      converter.noteReply(input?.agentId, input?.response);
    } catch (error) {
      console.error('[Web管理画面] エージェントの終わりを処理できませんでした：', error);
    }
    return undefined;
  }

  // Observe only: always no-result (the user answers in the chat), never throw
  function onPermissionRequest(request) {
    try {
      core?.onPermissionRequest(request);
    } catch (error) {
      console.error('[Web管理画面] 許可の求めを処理できませんでした：', error);
    }
    return { kind: 'no-result' };
  }

  const tools = [
    {
      name: toolName,
      description: OPEN_TOOL_DESCRIPTION,
      parameters: {
        type: 'object',
        properties: { requestText: { type: 'string', description: OPEN_PARAM_DESCRIPTION } },
        required: ['requestText'],
        additionalProperties: false,
      },
      skipPermission: true,
      defer: 'never', // keep it out of deferred tool search, so the entry skill always finds it
      handler: (args) => openHandler(args),
    },
  ];

  const session = await joinSession({ tools, hooks: { onPreToolUse, onSubagentStop }, onPermissionRequest });

  let unsubscribe = () => {};
  try {
    const cwd = await readWorkingDirectory(session);
    core = createCore({
      port: createPort(session, converter),
      runner,
      defaults,
      pluginRoot,
      pluginName,
      dataRoot,
      sessionId: session.sessionId,
      cwd,
      clock,
      env: options.env,
      ghCommand: options.ghCommand,
      startUi: (api) => startApp({ core: api, staticDir: options.staticDir, preferredPort: options.preferredPort ?? defaults.port }),
    });
    unsubscribe = session.on((event) => {
      try {
        const neutral = converter.convert(event);
        if (neutral) core.onEvent(neutral);
      } catch (error) {
        console.error('[Web管理画面] イベントを処理できませんでした：', error);
      }
    });
    markReady(core);
  } catch (error) {
    markFailed(error);
    throw error;
  }

  let stopping = null;
  const shutdown = () => {
    stopping ??= (async () => {
      try {
        unsubscribe();
      } catch {
        // the session may be gone already
      }
      try {
        await core.shutdown();
      } catch (error) {
        console.error('[Web管理画面] 終わる処理に失敗しました：', error);
      }
    })();
    return stopping;
  };

  return { pluginName, pluginRoot, dataRoot, defaults, toolName, session, core, shutdown };
}
