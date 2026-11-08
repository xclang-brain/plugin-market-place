// A fake Copilot SDK: joinSession and the session it returns. Shaped like the real SDK
// (tools with handler(args, invocation), hooks, onPermissionRequest, session.on, session.rpc.*).
//
//   const fake = createFakeSdk({ cwd });
//   await startAdapter({ joinSession: fake.joinSession, ... });
//   fake.config            what joinSession received { tools, hooks, onPermissionRequest }
//   fake.calls / callsOf   rpc calls [{ method, args }]
//   fake.behave(m, { error | value | gate })
//   fake.spawn({...})      add a task to tasks.list
//   fake.emit(event)       send an SDK event (permission.requested also reaches onPermissionRequest)
//   fake.callTool(name, args, { agentId })   → { ok, text } or { ok: false, error }
//   fake.callHook(type, input)

let eventSeq = 0;

export function makeEvent(type, data = {}, { agentId, timestamp } = {}) {
  eventSeq += 1;
  return {
    id: `ev-${eventSeq}`,
    timestamp: timestamp ?? new Date(Date.UTC(2026, 10, 8, 1, 0, 0) + eventSeq * 1000).toISOString(),
    parentId: null,
    ...(agentId === undefined ? {} : { agentId }),
    type,
    data,
  };
}

export const sdkEvent = {
  subagentStarted: (agentId, data = {}, options = {}) =>
    makeEvent('subagent.started', { agentName: 'x', agentDisplayName: 'x', agentDescription: '', toolCallId: `tc-${agentId}`, ...data }, { agentId, ...options }),
  subagentConfigured: (agentId, data = {}) => makeEvent('subagent.configured', { model: 'm', ...data }, { agentId }),
  subagentCompleted: (agentId, data = {}) => makeEvent('subagent.completed', { toolCallId: `tc-${agentId}`, ...data }, { agentId }),
  subagentFailed: (agentId, data = {}) => makeEvent('subagent.failed', { toolCallId: `tc-${agentId}`, error: '失敗', ...data }, { agentId }),
  agentIdle: (agentId) => makeEvent('system.notification', { content: '手番を終えました', kind: { type: 'agent_idle', agentId, agentType: 'x' } }),
  toolStart: (agentId, toolCallId, toolName, args) => makeEvent('tool.execution_start', { toolCallId, toolName, ...(args === undefined ? {} : { arguments: args }) }, { agentId }),
  toolComplete: (agentId, toolCallId, data = {}) => makeEvent('tool.execution_complete', { toolCallId, success: true, ...data }, { agentId }),
  permissionRequested: (agentId, requestId, permissionRequest, data = {}) => makeEvent('permission.requested', { requestId, permissionRequest, ...data }, { agentId }),
  permissionCompleted: (requestId) => makeEvent('permission.completed', { requestId, result: { kind: 'approved' } }),
  userInputRequested: (agentId, requestId, question) => makeEvent('user_input.requested', { requestId, question }, { agentId }),
  userInputCompleted: (requestId) => makeEvent('user_input.completed', { requestId, answer: 'はい' }),
  limitRequested: (requestId) => makeEvent('session_limits_exhausted.requested', { requestId, maxAiCredits: 10, usedAiCredits: 10 }),
  limitCompleted: (requestId, action = 'cancel') => makeEvent('session_limits_exhausted.completed', { requestId, response: { action } }),
};

export function createFakeSdk({ sessionId = 'session-fake-1', cwd = process.cwd() } = {}) {
  const calls = [];
  const behaviors = {};
  const handlers = new Set();
  const parents = new Map();
  const answers = [];
  let seq = 0;

  const fake = {
    sessionId,
    cwd,
    config: null,
    calls,
    answers,
    tasks: [],
    overrides: undefined,
    currentModel: { modelId: 'claude-sonnet-5.5' },
    models: [
      { id: 'claude-sonnet-5.5', name: 'Claude Sonnet 5.5' },
      { id: 'gpt-6.1-sol', name: 'GPT-6.1 Sol' },
      { id: 'gpt-6-luna', name: 'GPT-6 Luna' },
      { id: 'claude-haiku-5.5', name: 'Claude Haiku 5.5' },
    ],

    behave(method, behavior) {
      behaviors[method] = behavior;
    },
    reset(method) {
      delete behaviors[method];
    },
    callsOf(method) {
      return calls.filter((c) => c.method === method);
    },
    spawn({ agentId, parentAgentId, agentType = 'x', status = 'running', displayName = '' }) {
      const task = { type: 'agent', id: agentId, displayName, status, agentType, executionMode: 'background' };
      fake.tasks.push(task);
      if (parentAgentId) parents.set(agentId, parentAgentId);
      return task;
    },
    task(agentId) {
      return fake.tasks.find((t) => t.id === agentId);
    },
    emit(event) {
      broadcast(event);
      // tasks.list follows the agents' ends, like the real one
      const ended = { 'subagent.completed': event.data?.cancelled ? 'cancelled' : 'completed', 'subagent.failed': 'failed' }[event.type];
      const target = ended ? fake.task(event.agentId) : event.data?.kind?.type === 'agent_idle' ? fake.task(event.data.kind.agentId) : null;
      if (target && !['completed', 'failed', 'cancelled'].includes(target.status)) target.status = ended ?? 'idle';
      for (const handler of [...handlers]) {
        try {
          handler(event);
        } catch {
          // the real SDK swallows listener errors
        }
      }
    },
    async callTool(name, args = {}, { agentId } = {}) {
      const tool = fake.config.tools.find((t) => t.name === name);
      if (!tool) throw new Error(`tool ${name} is not registered`);
      try {
        const raw = await tool.handler(args, { sessionId, toolCallId: `tc-call-${(seq += 1)}`, toolName: name, arguments: args, agentId });
        if (raw !== null && typeof raw === 'object' && typeof raw.textResultForLlm === 'string' && typeof raw.resultType === 'string') {
          const failed = raw.resultType !== 'success';
          return { ok: !failed, text: raw.textResultForLlm, resultType: raw.resultType, ...(failed ? { error: raw.textResultForLlm } : {}) };
        }
        return { ok: true, text: typeof raw === 'string' ? raw : JSON.stringify(raw) };
      } catch (error) {
        return { ok: false, thrown: true, error: error instanceof Error ? error.message : String(error) };
      }
    },
    async callHook(type, input = {}) {
      const handler = fake.config.hooks?.[type];
      if (!handler) return undefined;
      try {
        return await handler({ sessionId, timestamp: new Date(), workingDirectory: cwd, ...input }, { sessionId });
      } catch {
        return 'THREW';
      }
    },
    async joinSession(config) {
      fake.config = config;
      return session;
    },
  };
  fake.joinSession = fake.joinSession.bind(fake);

  function broadcast(event) {
    if (event.type !== 'permission.requested' || !fake.config?.onPermissionRequest) return;
    if (event.data?.resolvedByHook) return;
    void (async () => {
      try {
        const result = await fake.config.onPermissionRequest(event.data.permissionRequest, { sessionId });
        if (result?.kind !== 'no-result') answers.push({ requestId: event.data.requestId, result });
      } catch {
        answers.push({ requestId: event.data.requestId, result: { kind: 'user-not-available' } });
      }
    })();
  }

  function invoke(method, args, produce) {
    calls.push({ method, args });
    const behavior = behaviors[method] ?? {};
    return new Promise((resolve, reject) => {
      const settle = () => {
        if (behavior.error) return reject(behavior.error);
        try {
          if (behavior.value !== undefined) resolve(typeof behavior.value === 'function' ? behavior.value(args) : behavior.value);
          else resolve(produce());
        } catch (error) {
          reject(error);
        }
      };
      if (behavior.gate) behavior.gate.then(settle, reject);
      else settle();
    });
  }

  function descendantsOf(id) {
    const out = [id];
    for (const [child, parent] of parents) if (parent === id) out.push(...descendantsOf(child));
    return out;
  }

  const rpc = {
    tasks: {
      startAgent: (args) =>
        invoke('tasks.startAgent', args, () => {
          seq += 1;
          const agentId = `ag-root-${seq}`;
          fake.spawn({ agentId, agentType: args.agentType, displayName: args.name });
          return { agentId };
        }),
      cancel: (args) =>
        invoke('tasks.cancel', args, () => {
          const target = fake.task(args.id);
          if (!target || ['completed', 'failed', 'cancelled'].includes(target.status)) return { cancelled: false };
          for (const id of descendantsOf(args.id)) {
            const t = fake.task(id);
            if (t && !t.stubborn && !['completed', 'failed'].includes(t.status)) t.status = 'cancelled';
          }
          return { cancelled: true };
        }),
      list: () => invoke('tasks.list', undefined, () => ({ tasks: fake.tasks.map((t) => ({ ...t })) })),
    },
    tools: {
      updateSubagentSettings: (args) =>
        invoke('tools.updateSubagentSettings', args, () => {
          fake.overrides = args.subagents;
          return {};
        }),
    },
    model: {
      getCurrent: () => invoke('model.getCurrent', undefined, () => ({ ...fake.currentModel })),
      list: () => invoke('model.list', undefined, () => ({ list: fake.models.map((m) => ({ ...m })) })),
    },
    workspaces: {
      getWorkspace: () => invoke('workspaces.getWorkspace', undefined, () => ({ workspace: { id: 'ws-1', cwd } })),
    },
  };

  const session = {
    sessionId,
    rpc,
    log: (message, options) => invoke('session.log', { message, options }, () => undefined),
    on(handler) {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
  };
  fake.session = session;
  return fake;
}
