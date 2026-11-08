import { ORCHESTRATOR_TOOLS, SHELL_SIDE_TOOLS, SHELL_TOOLS, WRITE_TOOLS } from './constants.mjs';
import { roleOfType } from './defaults.mjs';
import { REVIEW_DENY, SHELL_DENY, orchestratorDeny, stoppedDeny } from './messages.mjs';
import { argsOf, isPlainObject } from './util.mjs';

// onPreToolUse (design 8.3). Answers at once and never throws (a throw would let the call through).
// - Agents of a stopped run (the whole tree): every tool is denied.
// - This run's orchestrator (the id startAgent returned): only reading/searching, agent tools, skill,
//   Coverity read tools, and the shell for this run's batch-run command or the screen command (defaults.json).
// - Agents of this run other than the reviewer: writing items/<ID>/review.json is denied.
// - The main chat agent and agents outside this run are never denied.
// Returns { deny: true, reason } or null.

const REVIEW_PATH = /(?:^|[\\/])items[\\/][^\\/\s"'`]+[\\/]review\.json(?=$|[\s"'`])/i;
const PATCH_FILES = /^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+)$/gim;
const PATH_KEYS = ['path', 'file_path', 'filePath', 'fileName', 'filename', 'target_file', 'targetFile', 'file'];

const field = (input, key) => {
  try {
    return input?.[key];
  } catch {
    return undefined;
  }
};

const norm = (p) => String(p ?? '').replace(/\\/g, '/').toLowerCase();

function writesReview(tool, args) {
  if (WRITE_TOOLS.has(tool)) {
    for (const key of PATH_KEYS) if (typeof args[key] === 'string' && REVIEW_PATH.test(args[key].trim())) return true;
    for (const value of Object.values(args)) {
      if (typeof value !== 'string') continue;
      for (const m of value.matchAll(PATCH_FILES)) if (REVIEW_PATH.test(m[1].trim())) return true;
    }
    return false;
  }
  if (SHELL_TOOLS.has(tool)) return REVIEW_PATH.test(String(args.command ?? args.cmd ?? args.script ?? ''));
  return false;
}

// "uv run ... "<the batch-run or screen script>" <command> ..." and nothing chained before or after it
export function isAllowedCommand(command, scripts) {
  const c = String(command ?? '').trim().replace(/\s+2>&1\s*$/, '');
  if (!c || /[;&|`<>\r\n]|\$\(/.test(c)) return false;
  // uv run [--flags] "<script>" <command> ...: the first argument after the flags must be the script itself
  const m = /^["']?uv(?:\.exe)?["']?\s+run\s+((?:--[\w-]+\s+)*)("[^"]+"|'[^']+'|\S+)/i.exec(c);
  if (!m) return false;
  const script = norm(m[2].replace(/^["']|["']$/g, ''));
  return scripts.filter(Boolean).some((s) => script === norm(s));
}

function readToolMatches(tool, readTools) {
  return readTools.some((t) => {
    if (tool === t) return true;
    if (!tool.endsWith(t)) return false;
    return /[/\-_:.]/.test(tool.charAt(tool.length - t.length - 1));
  });
}

export function createGuard(ctx) {
  function orchestratorVerdict(tool, args) {
    if (ORCHESTRATOR_TOOLS.has(tool) || SHELL_SIDE_TOOLS.has(tool)) return null;
    if (readToolMatches(tool, ctx.defaults.coverityReadTools)) return null;
    if (SHELL_TOOLS.has(tool)) {
      const command = args.command ?? args.cmd ?? args.script;
      return isAllowedCommand(command, [ctx.commands.runScript, ctx.commands.guiScript]) ? null : { deny: true, reason: SHELL_DENY };
    }
    return { deny: true, reason: orchestratorDeny(tool.slice(0, 80)) };
  }

  return function preToolUse(input) {
    try {
      if (!isPlainObject(input)) return null;
      const agentId = field(input, 'agentId');
      const parentId = field(input, 'parentAgentId');
      const rawTool = field(input, 'toolName');
      const tool = typeof rawTool === 'string' ? rawTool : '';
      if (agentId == null || agentId === '') return null; // the main chat agent
      if (ctx.deny.has(agentId) || (parentId != null && ctx.deny.has(parentId))) {
        ctx.deny.add(agentId);
        return { deny: true, reason: stoppedDeny(tool.slice(0, 80)) };
      }
      const run = ctx.run;
      if (!run || run.status === 'finished') return null;
      const inTree = run.tree.has(agentId) || (parentId != null && run.tree.has(parentId));
      if (!inTree && !run.roots.includes(agentId)) return null; // outside this run
      if (run.status === 'stopping') return { deny: true, reason: stoppedDeny(tool.slice(0, 80)) };
      const args = argsOf(field(input, 'toolArgs') ?? field(input, 'arguments') ?? field(input, 'args'));
      if (run.roots.includes(agentId)) return orchestratorVerdict(tool, args);
      const role = run.tree.get(agentId)?.role ?? roleOfType(ctx.defaults, field(input, 'agentType')) ?? run.tree.get(parentId)?.role;
      if (role !== 'reviewer' && writesReview(tool, args)) return { deny: true, reason: REVIEW_DENY };
      return null;
    } catch {
      return null;
    }
  };
}
