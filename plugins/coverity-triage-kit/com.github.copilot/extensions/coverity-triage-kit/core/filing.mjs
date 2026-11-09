import { LIMITS } from './constants.mjs';
import { FILING } from './messages.mjs';
import { refreshPrevious } from './recovery.mjs';
import { newAttemptId, summarizeText } from './util.mjs';

// Filing an issue draft with gh, after the user approves it on the screen (REQ-422).
// The target repository is defaults.json `issueRepo`. Only marker writes run in the queue; gh runs outside it.
// A marker per draft is created exclusively, so two sessions cannot file the same draft.

export function parseRepo(value) {
  const s = typeof value === 'string' ? value.trim() : '';
  const m = /^(?:https?:\/\/github\.com\/)?([A-Za-z0-9][A-Za-z0-9-]*)\/([A-Za-z0-9._-]+?)(?:\.git)?\/?$/.exec(s);
  return m ? `${m[1]}/${m[2]}` : null;
}

const gh = (ctx, args, extra = {}) =>
  Promise.resolve()
    .then(() => ctx.runner({ command: ctx.ghCommand, args, timeoutMs: LIMITS.GH_CHECK_TIMEOUT_MS, clock: ctx.clock, ...extra }))
    .catch((e) => ({ spawnError: String(e?.message ?? e) }));

// { ok, reason, code, steps, repo }
export async function checkCanFile(ctx) {
  const raw = ctx.defaults.issueRepo;
  const repo = parseRepo(raw);
  const steps = [];
  let code = null;
  if (!raw) {
    code = 'no_repository';
    steps.push(FILING.steps.repository);
  } else if (!repo) {
    code = 'not_github';
    steps.push(FILING.steps.repository);
  }
  const v = await gh(ctx, ['--version']);
  if (v.spawnError || v.timedOut || v.code !== 0) {
    code ??= 'no_gh';
    steps.push(FILING.steps.gh);
  } else {
    const a = await gh(ctx, ['auth', 'status']);
    if (a.spawnError || a.timedOut || a.code !== 0) {
      code ??= 'not_logged_in';
      steps.push(FILING.steps.login);
    }
  }
  if (repo) steps.push(FILING.steps.access(repo));
  return { ok: code === null, reason: code ? FILING.reasons[code] : null, code, steps: code ? steps : [], repo };
}

export async function refreshCanFile(ctx) {
  const result = await checkCanFile(ctx);
  ctx.canFile = result;
  ctx.canFileChecked = true;
  ctx.bump();
  return result;
}

export function startCanFileCheck(ctx) {
  ctx.canFilePromise ??= refreshCanFile(ctx)
    .catch(() => ctx.canFile)
    .finally(() => {
      ctx.canFilePromise = null;
    });
  return ctx.canFilePromise;
}

async function ensureCanFile(ctx) {
  if (ctx.canFileChecked) return ctx.canFile;
  await startCanFileCheck(ctx);
  return ctx.canFile;
}

export const issueMarkerText = (ctx, runId, issueId) => `<!-- ${ctx.pluginName}-issue:${runId}/${issueId} -->`;

function findTarget(ctx, issueId) {
  refreshPrevious(ctx);
  const run = ctx.run;
  if (run && run.status === 'finished') {
    const issue = run.issues.find((i) => i.id === issueId);
    return issue ? { runId: run.id, issue } : null;
  }
  if (!run && ctx.previous) {
    const issue = (ctx.previous.result.issues ?? []).find((i) => i.id === issueId);
    return issue ? { runId: ctx.previous.runId, issue } : null;
  }
  return null;
}

const effectiveState = (ctx, marker) => (marker.state === 'filing' && ctx.clock.now() - marker.startedAt > LIMITS.FILING_STALE_MS ? 'unknown' : marker.state);

export function filingOf(ctx, runId, issueId) {
  const m = ctx.store.readMarker(runId, issueId);
  if (!m) return { state: 'none', url: null, reason: null };
  const state = effectiveState(ctx, m);
  const reason = m.reason ?? (state === 'unknown' && m.state === 'filing' ? FILING.stale : null);
  return { state, url: m.url ?? null, reason };
}

export const viewIssues = (ctx, runId, issues) =>
  issues.map((i) => ({ id: i.id, title: i.title, body: i.body, source: i.source, filing: filingOf(ctx, runId, i.id) }));

function prepareMarker(ctx, runId, issueId) {
  const { store } = ctx;
  let previousState = 'none';
  const existing = store.readMarker(runId, issueId);
  if (existing) {
    const state = effectiveState(ctx, existing);
    if (state === 'filed') return { ok: false, error: FILING.already };
    if (state === 'filing') return { ok: false, error: FILING.inProgress };
    if (!store.retireMarker(runId, issueId, newAttemptId())) return { ok: false, error: FILING.other };
    previousState = state;
  }
  const attempt = newAttemptId();
  const marker = { runId, issueId, attempt, state: 'filing', startedAt: ctx.clock.now(), url: null, reason: null };
  if (!store.createMarkerExclusive(runId, issueId, marker)) return { ok: false, error: FILING.other };
  return { ok: true, attempt, previousState };
}

function commitMarker(ctx, runId, issueId, attempt, outcome) {
  const marker = ctx.store.readMarker(runId, issueId);
  if (!marker || marker.attempt !== attempt) return false;
  ctx.store.replaceMarker(runId, issueId, { ...marker, state: outcome.state, url: outcome.url ?? null, reason: outcome.reason ?? null, updatedAt: ctx.clock.now() });
  return true;
}

const URL_PATTERN = /https:\/\/\S+/;

async function searchExisting(ctx, repo, marker) {
  const r = await gh(ctx, ['issue', 'list', '--repo', repo, '--state', 'all', '--limit', String(LIMITS.GH_LIST_LIMIT), '--json', 'title,url,body'], { timeoutMs: LIMITS.GH_TIMEOUT_MS });
  if (r.spawnError) return { status: 'failed', reason: FILING.searchSpawn };
  if (r.timedOut) return { status: 'failed', reason: FILING.searchTimeout };
  if (r.code !== 0) return { status: 'failed', reason: FILING.searchFailed(summarizeText(r.stderr, 200)) };
  let list;
  try {
    list = JSON.parse(r.stdout);
  } catch {
    return { status: 'failed', reason: FILING.searchUnreadable };
  }
  if (!Array.isArray(list)) return { status: 'failed', reason: FILING.searchUnreadable };
  const hit = list.find((i) => typeof i?.body === 'string' && i.body.includes(marker));
  return hit ? { status: 'found', url: hit.url ?? null } : { status: 'none' };
}

async function createIssue(ctx, repo, issue, marker) {
  const r = await gh(ctx, ['issue', 'create', '--repo', repo, '--title', issue.title, '--body-file', '-'], {
    input: `${issue.body}\n\n${marker}\n`,
    timeoutMs: LIMITS.GH_TIMEOUT_MS,
  });
  if (r.spawnError) return { state: 'failed', reason: FILING.spawn };
  if (r.timedOut) return { state: 'unknown', reason: FILING.timeout(LIMITS.GH_TIMEOUT_MS / 1000) };
  if (r.code !== 0) return { state: 'failed', reason: FILING.failed(summarizeText(r.stderr || r.stdout, 300)) };
  const url = (String(r.stdout ?? '').match(URL_PATTERN) ?? [null])[0];
  if (!url) return { state: 'unknown', reason: FILING.noUrl };
  return { state: 'filed', url };
}

export async function fileIssue(ctx, issueId) {
  const target = findTarget(ctx, issueId);
  if (!target) throw new Error(FILING.notFound);
  const can = await ensureCanFile(ctx);
  if (!can.ok) throw new Error(FILING.cannot(can.reason));
  const { runId, issue } = target;
  const prep = await ctx.queue.enqueue(() => prepareMarker(ctx, runId, issue.id));
  ctx.bump();
  if (!prep.ok) throw new Error(prep.error);
  const marker = issueMarkerText(ctx, runId, issue.id);
  let outcome = null;
  if (prep.previousState === 'unknown') {
    const found = await searchExisting(ctx, can.repo, marker);
    if (found.status === 'found') outcome = { state: 'filed', url: found.url };
    else if (found.status === 'failed') outcome = { state: 'unknown', reason: found.reason };
  }
  outcome ??= await createIssue(ctx, can.repo, issue, marker);
  await ctx.queue.enqueue(() => commitMarker(ctx, runId, issue.id, prep.attempt, outcome));
  ctx.bump();
  if (outcome.state !== 'filed') throw new Error(outcome.reason);
  return { state: 'filed', url: outcome.url };
}
