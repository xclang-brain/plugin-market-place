import { commandPath } from './defaults.mjs';
import { LIMITS } from './constants.mjs';
import { COMMAND_FAILED, COMMAND_TIMEOUT, COMMAND_UNREADABLE, UV_MISSING } from './messages.mjs';
import { summarizeText } from './util.mjs';

// The batch-run and screen commands (design 4.12, 4.16), run through the runner port.
//   uv run --native-tls --quiet "<plugin>/<runCommand>" <command> ...   with PYTHONIOENCODING=utf-8
// Each returns { ok: true, data } or { ok: false, error, problems, kind, data? }. Never throws.

function parseJson(stdout) {
  const s = String(stdout ?? '').trim();
  if (!s) return undefined;
  try {
    return JSON.parse(s);
  } catch {
    // a stray line before the JSON: take the last line that is a JSON object
    for (const line of s.split(/\r?\n/).reverse()) {
      const t = line.trim();
      if (!t.startsWith('{')) continue;
      try {
        return JSON.parse(t);
      } catch {
        // keep looking
      }
    }
  }
  return undefined;
}

export function problemText(p) {
  if (typeof p === 'string') return p;
  if (p && typeof p === 'object') return String(p.message ?? p.detail ?? p.error ?? JSON.stringify(p));
  return String(p ?? '');
}

export function createCommands({ runner, pluginRoot, defaults, clock, env = process.env }) {
  const runScript = commandPath(pluginRoot, defaults.runCommand);
  const guiScript = commandPath(pluginRoot, defaults.guiCommand);

  async function call(script, args, timeoutMs) {
    let r;
    try {
      r = await runner({
        command: 'uv',
        args: ['run', '--native-tls', '--quiet', script, ...args],
        env: { ...env, PYTHONIOENCODING: 'utf-8' },
        timeoutMs,
        clock,
      });
    } catch (e) {
      r = { spawnError: String(e?.message ?? e) };
    }
    if (r?.spawnError) return { ok: false, kind: 'spawn', error: UV_MISSING, problems: [] };
    if (r?.timedOut) return { ok: false, kind: 'timeout', error: COMMAND_TIMEOUT, problems: [] };
    const data = parseJson(r?.stdout);
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      const tail = summarizeText(r?.stderr ?? '', 300);
      return { ok: false, kind: 'unreadable', error: tail ? `${COMMAND_UNREADABLE}（${tail}）` : COMMAND_UNREADABLE, problems: [] };
    }
    if (data.ok === false || (r.code !== 0 && r.code !== null && data.ok !== true)) {
      return {
        ok: false,
        kind: 'failed',
        error: typeof data.error === 'string' && data.error ? data.error : COMMAND_FAILED,
        // `how_to_fix` is an optional hint some commands add to a failure
        problems: [...(Array.isArray(data.problems) ? data.problems : []).map(problemText), ...(typeof data.how_to_fix === 'string' && data.how_to_fix ? [data.how_to_fix] : [])],
        data,
      };
    }
    return { ok: true, data };
  }

  return {
    runScript,
    guiScript,
    settings: (repo) => call(runScript, ['settings', '--repo', repo], LIMITS.SETTINGS_TIMEOUT_MS),
    runs: (repo) => call(runScript, ['runs', '--repo', repo], LIMITS.RUNS_TIMEOUT_MS),
    newRun(repo, { filter, impact, limit, initial, maxRework }) {
      const args = ['new-run', '--repo', repo];
      if (filter) args.push('--filter', filter);
      if (impact) args.push('--impact', impact);
      if (Number.isInteger(limit)) args.push('--limit', String(limit));
      if (initial === true) args.push('--initial');
      if (initial === false) args.push('--no-initial');
      args.push('--review'); // the screen always runs the review (spec ch.9)
      if (Number.isInteger(maxRework)) args.push('--max-rework', String(maxRework));
      return call(runScript, args, LIMITS.NEW_RUN_TIMEOUT_MS);
    },
    status: (runDir) => call(runScript, ['status', '--run', runDir], LIMITS.STATUS_TIMEOUT_MS),
    summary: (runDir) => call(runScript, ['summary', '--run', runDir], LIMITS.SUMMARY_TIMEOUT_MS),
    stop: (runDir) => call(runScript, ['stop', '--run', runDir], LIMITS.RUN_STOP_TIMEOUT_MS),
    guiOpen({ repo, runDir, view, item }) {
      const args = ['open', '--repo', repo];
      if (runDir) args.push('--run', runDir);
      if (view) args.push('--view', view);
      if (item) args.push('--item', item);
      args.push('--no-browser');
      return call(guiScript, args, LIMITS.GUI_TIMEOUT_MS);
    },
  };
}
