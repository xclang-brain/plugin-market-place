import { LIMITS } from './constants.mjs';
import { ROLE_SHORT } from './messages.mjs';
import { clone, summarizeText } from './util.mjs';

// One run in memory, and the "run" part of the state the screen reads.

const ACTIVE = new Set(['running', 'waiting']);
export const isActiveAgent = (rec) => ACTIVE.has(rec.status);
export const ACTIVE_RUN = new Set(['starting', 'running', 'summarizing', 'stopping']);

function makeSlot(slot, role, label, selectedModel) {
  return {
    slot,
    role,
    label,
    status: 'standby',
    task: '',
    selectedModel: selectedModel ?? '',
    actualModel: '',
    lastActiveAt: null,
    replySummary: '',
    hasReply: false,
    agentId: null,
  };
}

const workerLabel = (n) => `${ROLE_SHORT.worker} ${n}`;

export function createRun(ctx, { id, settings, requestText, isAuto, startedAt, parallel }) {
  const workers = Math.max(1, Math.min(8, Number.isInteger(parallel) ? parallel : 3));
  const slots = [
    makeSlot('orchestrator', 'orchestrator', ROLE_SHORT.orchestrator, settings.models.orchestrator),
    ...Array.from({ length: workers }, (_, i) => makeSlot(`worker-${i + 1}`, 'worker', workerLabel(i + 1), settings.models.worker)),
    makeSlot('reviewer', 'reviewer', ROLE_SHORT.reviewer, settings.models.reviewer),
    makeSlot('integrator', 'integrator', ROLE_SHORT.integrator, settings.models.integrator),
  ];
  return {
    id,
    startedAt,
    finishedAt: null,
    sessionId: ctx.sessionId,
    cwd: ctx.cwd,
    status: 'starting',
    settings,
    requestText,
    isAuto: Boolean(isAuto),
    runDir: null,
    repoRoot: null,
    newRun: null,
    rootId: null,
    roots: [],
    restarts: 0,
    tree: new Map(),
    slots,
    st: null, // the last `status` output
    stError: null,
    stSignature: null,
    stAt: null,
    summaryData: null,
    summaryError: null,
    links: null,
    guiError: null,
    notes: [], // the extension's own lines for "recent activity": { at, text }
    endReason: null,
    pendingEnd: null,
    issues: [],
    entries: [],
    waits: [],
    limit: null,
    stopRequested: false,
    stopAt: null,
    stopSignal: null,
    resolveStop: null,
    closeRequested: false,
    closing: false,
    unstopped: [],
    unstoppedJobs: [],
    stoppedJobs: [],
    overridesTouched: false,
    startFailed: false,
  };
}

export function attachStopSignal(run) {
  run.stopSignal = new Promise((resolve) => {
    run.resolveStop = resolve;
  });
}

export const slotByName = (run, name) => run.slots.find((s) => s.slot === name) ?? null;

// Keeps the worker slots at `parallel` (status tells the real number)
export function ensureWorkerSlots(run, parallel) {
  if (!Number.isInteger(parallel) || parallel < 1) return;
  const workers = run.slots.filter((s) => s.role === 'worker');
  for (let n = workers.length + 1; n <= Math.min(parallel, 8); n += 1) {
    const slot = makeSlot(`worker-${n}`, 'worker', workerLabel(n), run.settings.models.worker);
    const lastWorker = run.slots.map((s) => s.role).lastIndexOf('worker');
    run.slots.splice(lastWorker + 1, 0, slot);
  }
}

// A free slot of the role: never used, or its agent has ended. More agents than slots → a new slot.
export function assignSlot(run, rec) {
  const role = rec.role;
  if (!role || role === 'other') return null;
  const own = run.slots.filter((s) => s.role === role);
  let slot = own.find((s) => !s.agentId) ?? own.find((s) => !isActiveAgent(run.tree.get(s.agentId) ?? { status: 'completed' }));
  if (!slot) {
    const n = own.length + 1;
    slot = makeSlot(`${role}-${n}`, role, role === 'worker' ? workerLabel(n) : `${ROLE_SHORT[role]} ${n}`, run.settings.models[role]);
    const last = run.slots.map((s) => s.role).lastIndexOf(role);
    run.slots.splice(last + 1, 0, slot);
  }
  slot.agentId = rec.agentId;
  rec.slotName = slot.slot;
  syncSlot(run, rec);
  return slot;
}

// Copies an agent record onto its slot (only while it is the slot's latest agent)
export function syncSlot(run, rec) {
  if (!rec?.slotName) return;
  const slot = slotByName(run, rec.slotName);
  if (!slot || slot.agentId !== rec.agentId) return;
  slot.status = rec.status;
  slot.task = rec.description ?? '';
  slot.actualModel = rec.model ?? '';
  slot.lastActiveAt = rec.lastEventAt ?? slot.lastActiveAt;
  slot.replySummary = rec.reply ? summarizeText(rec.reply, LIMITS.REPLY_SUMMARY_CHARS) : '';
  slot.hasReply = rec.reply != null;
}

// Plan / Do / Study (design 8.2): a running reviewer → study; workers, integrators, launched tasks → do;
// stages the orchestrator or a detached job works on without agents (initial analysis, fetching the warnings
// and their paths, build verification) → do; nothing running between ticks → plan.
const LONG_STAGES = new Set(['initial', 'fetch', 'details', 'verify']);
export function computePhase(run, force = false) {
  if (!force && run.status !== 'running') return null;
  let doing = false;
  for (const rec of run.tree.values()) {
    if (!isActiveAgent(rec)) continue;
    if (rec.role === 'reviewer') return 'study';
    if (rec.role === 'worker' || rec.role === 'integrator' || rec.role === 'other') doing = true;
  }
  if (doing) return 'do';
  const st = run.st;
  const tasks = Array.isArray(st?.running_tasks) ? st.running_tasks : [];
  if (tasks.length > 0) return tasks.every((t) => t?.role === 'reviewer') ? 'study' : 'do';
  if (st && LONG_STAGES.has(st.stage) && !st.finished) return 'do';
  return 'plan';
}

const pad = (n) => String(n).padStart(2, '0');
export function clockText(ms) {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function secondsOfDay(line) {
  const m = /^(\d{1,2}):(\d{2}):(\d{2})/.exec(line);
  return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) : -1;
}

// "Recent activity": the batch run's lines and the extension's own notes, newest first
export function recentLines(run) {
  const fromStatus = (Array.isArray(run.st?.recent) ? run.st.recent : []).filter((l) => typeof l === 'string');
  const own = run.notes.map((n) => `${clockText(n.at)} ${n.text}`);
  const merged = [...own.map((l, i) => ({ l, k: secondsOfDay(l), o: -1 - i })), ...fromStatus.map((l, i) => ({ l, k: secondsOfDay(l), o: i }))];
  merged.sort((a, b) => b.k - a.k || a.o - b.o);
  return merged.slice(0, LIMITS.RECENT_KEPT).map((x) => x.l);
}

export function addNote(run, at, textLine) {
  run.notes.unshift({ at, text: textLine });
  run.notes.length = Math.min(run.notes.length, LIMITS.RECENT_KEPT);
}

// The parts of the `status` output the screen uses (design 4.12)
const STATUS_KEYS = [
  'run_id', 'stage', 'stage_label', 'finished', 'stopped', 'has_summary', 'review', 'integration', 'max_rework', 'parallel',
  'counts', 'items', 'running_tasks', 'errors', 'initial', 'progress', 'run_checks', 'criteria', 'criteria_results', 'issues_found',
];
export function pickStatus(st) {
  if (!st || typeof st !== 'object') return null;
  const out = {};
  for (const key of STATUS_KEYS) if (st[key] !== undefined) out[key] = clone(st[key]);
  return out;
}

const SUMMARY_KEYS = ['counts', 'approvals_counts', 'results', 'unprocessed', 'run_checks', 'say', 'criteria', 'criteria_results'];
export function pickSummary(data) {
  if (!data || typeof data !== 'object') return null;
  const out = {};
  for (const key of SUMMARY_KEYS) if (data[key] !== undefined) out[key] = clone(data[key]);
  return out;
}

function pastAgents(run) {
  const shown = new Set(run.slots.map((s) => s.agentId).filter(Boolean));
  return [...run.tree.values()]
    .filter((rec) => !isActiveAgent(rec) && !shown.has(rec.agentId))
    .sort((a, b) => a.startedAt - b.startedAt)
    .map((rec) => ({
      agentId: rec.agentId,
      role: rec.role ?? 'other',
      status: rec.status,
      task: rec.description ?? '',
      model: rec.model ?? '',
      startedAt: rec.startedAt,
      endedAt: rec.endedAt ?? null,
      replySummary: rec.reply ? summarizeText(rec.reply, LIMITS.REPLY_SUMMARY_CHARS) : '',
      hasReply: rec.reply != null,
    }));
}

export function viewRun(ctx, run, issuesView) {
  return {
    id: run.id,
    status: run.status,
    endReason: run.endReason ? clone(run.endReason) : null,
    settings: clone(run.settings),
    isAuto: run.isAuto,
    phase: computePhase(run),
    runDir: run.runDir,
    permissionSteps: run.newRun?.permission_steps ?? [],
    restarts: run.restarts,
    maxRestarts: LIMITS.MAX_RESTARTS,
    agents: run.slots.map((s) => ({ ...s })),
    pastAgents: pastAgents(run),
    live: pickStatus(run.st),
    statusError: run.stError,
    recent: recentLines(run),
    summary: run.summaryData ? clone(run.summaryData) : null,
    summaryError: run.summaryError,
    links: run.links ? clone(run.links) : null,
    guiError: run.guiError,
    issues: issuesView,
    canFile: clone(ctx.canFile),
    unstopped: run.unstopped.map((u) => ({ ...u })),
    unstoppedJobs: run.unstoppedJobs.map((u) => ({ ...u })),
    waits: run.waits.map((w) => ({ requestId: w.requestId, slot: w.slot, role: w.role, kind: w.kind, detail: clone(w.detail) })),
  };
}
