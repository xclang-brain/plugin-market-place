import { EXTENSION_PERMISSION_KINDS } from './constants.mjs';
import { roleOfType } from './defaults.mjs';
import { logEntry, onRootEnded } from './lifecycle.mjs';
import { assignSlot, isActiveAgent, syncSlot } from './run.mjs';
import { clone } from './util.mjs';

// Neutral events from the adapter → the run state (agent tree, slots, waits, the orchestrator's end).
// handleEvent runs inside the serial queue, one event at a time.

const SLOT_ROLES = new Set(['worker', 'reviewer', 'integrator']);

function activeRun(ctx) {
  const run = ctx.run;
  return run && run.status !== 'finished' ? run : null;
}

function logEvent(ctx, run, e, at, patch = null) {
  logEntry(ctx, run, { t: 'event', at, e: patch ? { ...e, ...patch } : e });
}

function addToTree(ctx, run, e, at) {
  const parent = run.tree.get(e.parentAgentId);
  const own = roleOfType(ctx.defaults, e.agentType);
  // an agent the orchestrator started that has no role is "other"; nested agents take their parent's role
  const role = own && own !== 'orchestrator' ? own : parent?.role === 'orchestrator' ? 'other' : (parent?.role ?? 'other');
  const rec = {
    agentId: e.agentId,
    parentId: e.parentAgentId,
    agentType: e.agentType ?? '',
    slotName: null,
    role,
    status: 'running',
    description: e.description ?? '',
    model: e.model ?? '',
    startedAt: at,
    lastEventAt: at,
    endedAt: null,
    reply: null,
  };
  run.tree.set(e.agentId, rec);
  if (own && SLOT_ROLES.has(own) && parent?.role === 'orchestrator') assignSlot(run, rec);
  if (run.status === 'stopping') ctx.deny.add(e.agentId);
  logEntry(ctx, run, { t: 'tree', at, agentIds: [e.agentId] });
  return rec;
}

function onAgentStarted(ctx, e, at) {
  const run = activeRun(ctx);
  const { agentId, parentAgentId } = e;
  if (!run) {
    if (parentAgentId && ctx.deny.has(parentAgentId)) ctx.deny.add(agentId);
    return;
  }
  if (run.tree.has(agentId) && run.tree.get(agentId).isRoot) {
    const rec = run.tree.get(agentId);
    rec.status = rec.status === 'waiting' ? 'waiting' : 'running';
    rec.model = e.model || rec.model;
    rec.lastEventAt = at;
    syncSlot(run, rec);
    logEvent(ctx, run, e, at, { role: 'orchestrator' });
    return;
  }
  if (parentAgentId && run.tree.has(parentAgentId)) {
    const rec = addToTree(ctx, run, e, at);
    logEvent(ctx, run, e, at, { role: rec.role });
    return;
  }
  if (parentAgentId && ctx.deny.has(parentAgentId)) ctx.deny.add(agentId);
}

function onAgentConfigured(ctx, e, at) {
  const run = activeRun(ctx);
  const rec = run?.tree.get(e.agentId);
  if (!rec) return;
  if (e.model) rec.model = e.model;
  rec.lastEventAt = at;
  syncSlot(run, rec);
  logEvent(ctx, run, e, at);
}

async function onAgentEnded(ctx, e, at, kind) {
  const run = activeRun(ctx);
  const rec = run?.tree.get(e.agentId);
  if (!rec) return;
  logEvent(ctx, run, e, at);
  rec.lastEventAt = at;
  const stopping = run.status === 'stopping';
  if (kind === 'completed') {
    if (e.response != null) rec.reply = String(e.response);
    rec.status = e.cancelled || stopping ? 'stopped' : 'completed';
  } else if (kind === 'failed') {
    rec.error = e.error ?? '';
    rec.status = stopping ? 'stopped' : 'failed';
  } else if (isActiveAgent(rec)) {
    rec.status = 'completed'; // the turn ended and the agent went idle
  }
  rec.endedAt ??= at;
  run.waits = run.waits.filter((w) => w.agentId !== e.agentId);
  syncSlot(run, rec);
  if (e.agentId !== run.rootId) return;
  const cause = kind === 'failed' ? 'failed' : kind === 'completed' && e.cancelled ? 'cancelled' : 'ended';
  await onRootEnded(ctx, run, cause, kind === 'failed' ? String(e.error ?? '') : '');
}

function onToolEvent(ctx, e, at) {
  const run = activeRun(ctx);
  const rec = run?.tree.get(e.agentId);
  if (!rec) return;
  rec.lastEventAt = at;
  syncSlot(run, rec);
  logEvent(ctx, run, e, at);
}

// The nearest agent (itself or above) that has a slot
function waitTarget(run, agentId) {
  const seen = new Set();
  for (let cur = run.tree.get(agentId); cur && !seen.has(cur.agentId); cur = cur.parentId ? run.tree.get(cur.parentId) : null) {
    if (cur.slotName) return cur;
    seen.add(cur.agentId);
  }
  return run.tree.get(agentId) ?? null;
}

// Waiting for an answer in the chat (REQ-412). Only this run's agents count.
function onWaitStarted(ctx, e, at) {
  const run = ctx.run;
  if (!run || (run.status !== 'running' && run.status !== 'summarizing')) return;
  if (e.resolvedByHook) return;
  const detail = e.detail ?? {};
  if (EXTENSION_PERMISSION_KINDS.has(detail.permissionKind) || EXTENSION_PERMISSION_KINDS.has(detail.toolKind)) return;
  if (e.agentId != null && !run.tree.has(e.agentId)) return; // other work in the chat
  if (e.agentId == null && e.kind !== 'limit') return; // the main agent's own confirmation
  if (run.waits.some((w) => w.requestId === e.requestId)) return;
  const target = e.agentId != null ? waitTarget(run, e.agentId) : null;
  const safeDetail = clone(detail);
  if (safeDetail.sandboxPermissive) safeDetail.sandboxBypass = false;
  run.waits.push({ requestId: e.requestId, agentId: e.agentId ?? null, slot: target?.slotName ?? null, role: target?.role ?? null, kind: e.kind, detail: safeDetail });
  if (e.kind === 'limit') run.limit = { requestId: e.requestId, answered: false, action: null };
  if (target) {
    if (isActiveAgent(target)) target.status = 'waiting';
    run.tree.get(e.agentId).lastEventAt = at;
    syncSlot(run, target);
  }
  logEvent(ctx, run, e, at);
}

function onWaitEnded(ctx, e, at) {
  const run = ctx.run;
  if (!run || run.status === 'finished') return;
  const index = run.waits.findIndex((w) => w.requestId === e.requestId);
  if (index < 0) return;
  const [wait] = run.waits.splice(index, 1);
  if (wait.kind === 'limit' && run.limit && run.limit.requestId === wait.requestId) {
    run.limit.answered = true;
    run.limit.action = e.action ?? null;
  }
  if (wait.agentId != null) {
    const target = waitTarget(run, wait.agentId);
    if (target) {
      const still = run.waits.some((w) => w.agentId != null && waitTarget(run, w.agentId) === target);
      if (!still && target.status === 'waiting') target.status = 'running';
      target.lastEventAt = at;
      syncSlot(run, target);
    }
  }
  logEvent(ctx, run, e, at, { agentId: wait.agentId });
}

export async function handleEvent(ctx, e) {
  if (!e || typeof e.type !== 'string') return;
  const at = Number.isFinite(e.at) ? e.at : ctx.clock.now();
  switch (e.type) {
    case 'agent.started':
      return onAgentStarted(ctx, e, at);
    case 'agent.configured':
      return onAgentConfigured(ctx, e, at);
    case 'agent.completed':
      return onAgentEnded(ctx, e, at, 'completed');
    case 'agent.failed':
      return onAgentEnded(ctx, e, at, 'failed');
    case 'agent.idle':
      return onAgentEnded(ctx, e, at, 'idle');
    case 'tool.started':
    case 'tool.completed':
      return onToolEvent(ctx, e, at);
    case 'wait.started':
      return onWaitStarted(ctx, e, at);
    case 'wait.ended':
      return onWaitEnded(ctx, e, at);
    default:
      return undefined;
  }
}
