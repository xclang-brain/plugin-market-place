import { randomBytes } from 'node:crypto';

import { realClock } from './clock.mjs';
import { createCommands } from './commands.mjs';
import { DEFAULT_FILTER, DEFAULT_LIMIT, EFFORT_LEVELS, IMPACT_CHOICES, INITIAL_CHOICES, LIMITS, LIMIT_MAX, LIMIT_MIN, REWORK_MAX, REWORK_MIN, ROLES } from './constants.mjs';
import { handleEvent } from './events.mjs';
import { fileIssue, startCanFileCheck, viewIssues } from './filing.mjs';
import { createGuard } from './guard.mjs';
import { isActiveRun, logEntry, startRun, stopRun } from './lifecycle.mjs';
import {
  AUTO_NOTICE,
  BUSY_NOTICE,
  CHAT_CONFIRM_NOTICE,
  CLEAR_NOTICE,
  ENDED_AT_STARTUP_NOTICE,
  FILING,
  OPEN_START_NOTICE,
  OVERRIDE_NOTICE,
  PREALLOW_NOTICE,
  ROLE_SHORT,
  TITLE,
  applyGuide,
  setupHint,
} from './messages.mjs';
import { useSecretEnvs } from './mask.mjs';
import { normalizeModels, pickDefaultModel, readAgentModels } from './models.mjs';
import { stopPolling } from './poller.mjs';
import { SerialQueue } from './queue.mjs';
import { recoverOnStartup, refreshPrevious } from './recovery.mjs';
import { parseRequest } from './request.mjs';
import { viewRun } from './run.mjs';
import { createStore } from './store.mjs';
import { callWithTimeout, clone } from './util.mjs';

// The core of the extension. It does not depend on Copilot CLI: it talks to the adapter through
// the port (session calls), neutral events, and the runner (child processes). The screen calls the API below.

const SETTING_KEYS = ['max_rework', 'review', 'integration', 'initial', 'verify', 'max_items', 'parallel', 'output_dir'];

function filterNames(data) {
  const raw = data.filters ?? data.filter_names ?? data.filter_files ?? [];
  const names = (Array.isArray(raw) ? raw : []).map((f) => (typeof f === 'string' ? f : (f?.file ?? f?.name ?? ''))).filter((n) => typeof n === 'string' && n !== '');
  return [...new Set(names)];
}

function initialConditions(ctx) {
  const req = ctx.request.values;
  const filters = ctx.setup?.settings.filters ?? [];
  const suggested = ctx.setup?.settings.defaultFilter;
  return {
    filter: req.filter ?? suggested ?? (filters.length === 0 || filters.includes(DEFAULT_FILTER) ? DEFAULT_FILTER : filters[0]),
    impact: req.impact ?? IMPACT_CHOICES[0],
    limit: req.limit ?? DEFAULT_LIMIT,
    initial: req.initial ?? INITIAL_CHOICES[0],
  };
}

async function loadSettings(ctx) {
  const setup = ctx.setup;
  const r = await ctx.commands.settings(ctx.cwd);
  if (ctx.setup !== setup) return; // the screen was reset meanwhile
  if (r.ok) {
    useSecretEnvs(r.data.secret_envs);
    const values = {};
    for (const key of SETTING_KEYS) if (r.data[key] !== undefined) values[key] = clone(r.data[key]);
    setup.settings = {
      state: 'ok',
      values,
      filters: filterNames(r.data),
      defaultFilter: typeof r.data.default_filter === 'string' && r.data.default_filter ? r.data.default_filter : null,
      problems: (Array.isArray(r.data.problems) ? r.data.problems : []).map(String),
      error: null,
    };
    if (Number.isInteger(values.max_rework) && values.max_rework >= REWORK_MIN && values.max_rework <= REWORK_MAX) setup.maxReworkDefault = values.max_rework;
  } else {
    setup.settings = { state: 'error', values: {}, filters: [], problems: r.problems ?? [], error: r.error };
  }
  setup.conditions = initialConditions(ctx);
  ctx.bump();
  // the first time, also list the runs once so uv has everything ready (design 8.1 step 1)
  if (!ctx.warmed) {
    ctx.warmed = true;
    ctx.commands.runs(ctx.cwd).catch(() => {});
  }
}

async function refreshSetup(ctx) {
  const { port, clock } = ctx;
  const [listed, current] = await Promise.all([
    callWithTimeout(clock, () => port.listModels(), LIMITS.CALL_TIMEOUT_MS),
    callWithTimeout(clock, () => port.getCurrentModel(), LIMITS.CALL_TIMEOUT_MS),
  ]);
  let models = listed.status === 'ok' ? normalizeModels(listed.value) : [];
  const currentModel = current.status === 'ok' ? String(current.value?.model ?? current.value?.modelId ?? '') : '';
  const isAuto = current.status === 'ok' && Boolean(current.value?.isAuto);
  const ids = { orchestrator: ctx.defaults.orchestrator, ...ctx.defaults.roles };
  const candidates = Object.fromEntries(ROLES.map((role) => [role, readAgentModels(ctx.pluginRoot, ids[role], ctx.defaults.agentDir)]));
  if (models.length === 0) {
    // no list: offer the definitions' first choices and the current model
    const fallback = [...new Set([...ROLES.flatMap((r) => candidates[r].slice(0, 1)), currentModel].filter(Boolean))];
    models = fallback.map((id) => ({ id, name: id }));
  }
  const modelIds = models.map((m) => m.id);
  ctx.setup = {
    models,
    defaults: Object.fromEntries(ROLES.map((role) => [role, pickDefaultModel(candidates[role], modelIds, currentModel)])),
    roles: ROLES.map((role) => ({ role, label: ROLE_SHORT[role] })),
    effortLevels: [...EFFORT_LEVELS],
    isAuto,
    autoNotice: isAuto ? AUTO_NOTICE : null,
    overrideNotice: OVERRIDE_NOTICE,
    currentModel,
    maxReworkDefault: 1,
    reworkRange: { min: REWORK_MIN, max: REWORK_MAX },
    limitRange: { min: LIMIT_MIN, max: LIMIT_MAX },
    impactChoices: [...IMPACT_CHOICES],
    initialChoices: [...INITIAL_CHOICES],
    settings: { state: 'loading', values: {}, filters: [], problems: [], error: null },
    conditions: null,
    setupHint: setupHint(ctx.defaults.setupSkill),
  };
  ctx.setup.conditions = initialConditions(ctx);
  loadSettings(ctx).catch(() => {});
}

async function ensureUi(ctx, api) {
  if (ctx.uiHandle) return ctx.uiHandle.url;
  if (typeof ctx.startUi !== 'function') throw new Error('画面を起こす部品が渡されていません。');
  ctx.uiPromise ??= (async () => {
    const handle = await ctx.startUi(api);
    ctx.uiHandle = typeof handle === 'string' ? { url: handle } : handle;
    return ctx.uiHandle.url;
  })().catch((e) => {
    ctx.uiPromise = null;
    throw e;
  });
  return ctx.uiPromise;
}

function viewPrevious(ctx) {
  const previous = ctx.previous;
  if (!previous) return null;
  const { runId, result } = previous;
  return {
    runId,
    runDir: result.runDir ?? null,
    endReason: result.endReason ?? null,
    finishedAt: result.finishedAt ?? null,
    summary: result.summary ?? null,
    links: result.links ?? null,
    issues: viewIssues(ctx, runId, result.issues ?? []),
    unstopped: result.unstopped ?? [],
    unstoppedJobs: result.unstoppedJobs ?? [],
    canFile: clone(ctx.canFile),
  };
}

function buildState(ctx, { visible = true } = {}) {
  if (visible) ctx.lastViewAt = ctx.clock.now();
  refreshPrevious(ctx);
  const run = ctx.run;
  const view = run ? viewRun(ctx, run, run.status === 'finished' ? viewIssues(ctx, run.id, run.issues) : []) : null;
  const previous = run ? null : viewPrevious(ctx);
  const filings = (view ? view.issues : (previous?.issues ?? [])).map((i) => `${i.id}:${i.filing.state}`).join(',');
  if (filings !== ctx.filingSignature) {
    if (ctx.filingSignature !== null) ctx.version += 1;
    ctx.filingSignature = filings;
  }
  const active = isActiveRun(run);
  return {
    version: ctx.version,
    plugin: { name: ctx.pluginName, title: TITLE },
    guide: { apply: applyGuide(ctx.defaults.applySkill), setup: setupHint(ctx.defaults.setupSkill) },
    setup: ctx.setup ? { ...clone(ctx.setup), request: clone(ctx.request) } : null,
    run: view,
    previous,
    startError: ctx.startError ? clone(ctx.startError) : null,
    notices: active ? [CLEAR_NOTICE, CHAT_CONFIRM_NOTICE] : [],
  };
}

async function open(ctx, api, args) {
  const active = isActiveRun(ctx.run);
  if (!active) {
    if (ctx.run) stopPolling(ctx, ctx.run);
    ctx.run = null;
    ctx.startError = null;
    ctx.request = parseRequest(typeof args?.requestText === 'string' ? args.requestText : '');
    ctx.previousDirty = true;
    await refreshSetup(ctx);
  }
  const url = await ensureUi(ctx, api);
  startCanFileCheck(ctx);
  const notices = active ? [BUSY_NOTICE, CLEAR_NOTICE, CHAT_CONFIRM_NOTICE] : [OPEN_START_NOTICE, CLEAR_NOTICE, CHAT_CONFIRM_NOTICE, PREALLOW_NOTICE];
  if (!active && ctx.endedAtStartup.length > 0) {
    notices.push(ENDED_AT_STARTUP_NOTICE);
    ctx.endedAtStartup.length = 0;
  }
  ctx.bump();
  return { url, notices, busy: active };
}

/**
 * @param {object} options
 * @param {object} options.port        session calls: startAgent, cancelAgent, listTasks, setSubagentSettings, getCurrentModel, listModels
 * @param {Function} options.runner    child processes: ({ command, args, input, env, timeoutMs, clock }) => Promise<result>
 * @param {object} options.defaults    normalized defaults.json
 * @param {string} options.pluginRoot  the plugin folder (command locations are relative to it)
 * @param {string} options.pluginName
 * @param {string} options.dataRoot    records folder
 */
export function createCore(options = {}) {
  const { port, runner, defaults, pluginName, dataRoot } = options;
  if (!port) throw new Error('createCore needs port');
  if (typeof runner !== 'function') throw new Error('createCore needs runner');
  if (!defaults) throw new Error('createCore needs defaults');
  if (!pluginName) throw new Error('createCore needs pluginName');
  if (!dataRoot) throw new Error('createCore needs dataRoot');
  const clock = options.clock ?? realClock;
  const pluginRoot = options.pluginRoot ?? '';

  const ctx = {
    port,
    runner,
    defaults,
    pluginRoot,
    pluginName,
    dataRoot,
    sessionId: options.sessionId ?? randomBytes(8).toString('hex'),
    cwd: options.cwd ?? process.cwd(),
    clock,
    startUi: options.startUi ?? null,
    ghCommand: options.ghCommand ?? 'gh',
    commands: createCommands({ runner, pluginRoot, defaults, clock, env: options.env ?? process.env }),
    queue: new SerialQueue(),
    store: createStore({ dataRoot, clock }),
    version: 1,
    bump() {
      this.version += 1;
    },
    run: null,
    setup: null,
    request: parseRequest(''),
    previous: null,
    previousDirty: true,
    startError: null,
    deny: new Set(),
    knownRoots: new Set(),
    strayHandled: new Set(),
    canFile: { ok: false, reason: FILING.checking, code: 'checking', steps: [], repo: null },
    canFileChecked: false,
    canFilePromise: null,
    uiHandle: null,
    uiPromise: null,
    aliveTimer: null,
    recheckTimer: null,
    recoveryPromise: null,
    endedAtStartup: [],
    filingSignature: null,
    recordingError: null,
    lastViewAt: null,
    warmed: false,
    closed: false,
  };
  const fail = (e) => {
    ctx.recordingError = e;
  };
  const guard = createGuard(ctx);

  const api = {
    open: (args) => open(ctx, api, args),

    onEvent(e) {
      ctx.queue
        .enqueue(() => handleEvent(ctx, e))
        .then(() => ctx.bump())
        .catch(fail);
    },

    // onPreToolUse: answered at once, outside the queue; never throws
    preToolUse(input) {
      try {
        return guard(input);
      } catch {
        return null;
      }
    },

    // onPermissionRequest: observe only, never answer, never throw
    onPermissionRequest(req) {
      try {
        ctx.queue
          .enqueue(() => {
            const run = ctx.run;
            if (isActiveRun(run)) logEntry(ctx, run, { t: 'note', at: ctx.clock.now(), kind: 'permission_request_seen', requestId: req?.requestId ?? null });
          })
          .catch(fail);
      } catch {
        // observing only
      }
      return { kind: 'no-result' };
    },

    getState: (options) => buildState(ctx, options),
    start: (settings) => startRun(ctx, settings),
    stop: async () => stopRun(ctx),
    fileIssue: (issueId) => fileIssue(ctx, issueId),
    getReply(agentId) {
      const rec = ctx.run?.tree.get(agentId);
      return rec && rec.reply != null ? rec.reply : null;
    },
    whenIdle: () => ctx.queue.idle(),
    get context() {
      return ctx;
    },

    async shutdown() {
      ctx.closed = true;
      if (ctx.aliveTimer !== null) clock.clearInterval(ctx.aliveTimer);
      if (ctx.recheckTimer) clock.clearTimeout(ctx.recheckTimer);
      if (ctx.run) stopPolling(ctx, ctx.run);
      ctx.aliveTimer = null;
      ctx.recheckTimer = null;
      try {
        await ctx.uiHandle?.close?.();
      } catch {
        // ending anyway
      }
    },
  };

  recoverOnStartup(ctx);
  return api;
}
