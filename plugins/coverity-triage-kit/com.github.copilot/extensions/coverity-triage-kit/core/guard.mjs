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

// The command with every quoted part replaced by Q, or null when a quote is left open or a double-quoted
// part could expand something (` or $ inside "…": bash and PowerShell expand $var, $(…) and `…`).
// Single-quoted text never expands (bash and PowerShell).
function outsideQuotes(c) {
  let out = '';
  for (let i = 0; i < c.length; i += 1) {
    const ch = c[i];
    if (ch === "'") {
      const end = c.indexOf("'", i + 1);
      if (end < 0) return null;
      out += 'Q';
      i = end;
    } else if (ch === '"') {
      let j = i + 1;
      for (; j < c.length && c[j] !== '"'; j += 1) {
        if (c[j] === '`' || c[j] === '$') return null;
        if (c[j] === '\\' && c[j + 1] === '"') j += 1; // \" inside "…" (bash)
      }
      if (j >= c.length) return null;
      out += 'Q';
      i = j;
    } else {
      out += ch;
    }
  }
  return out;
}

// outside quotes: chaining and redirection (bash, cmd), and PowerShell expressions ((…), @(…), {…}, $var)
const UNQUOTED_META = /[;&|`<>\r\n(){}@$]/;
const STDIN_INPUT = /^echo\s+'[^']*'\s*\|\s*/;

// "uv run ... "<the batch-run or screen script>" <command> ..." and nothing chained before or after it.
// Quoted arguments may hold any text (C code, error messages). The one input form allowed is
// echo '<single-quoted text>' | uv run ... "<script>" <command> ... --stdin
export function isAllowedCommand(command, scripts) {
  let c = String(command ?? '').trim().replace(/\s+2>&1\s*$/, '');
  if (!c) return false;
  const piped = STDIN_INPUT.exec(c);
  if (piped) c = c.slice(piped[0].length);
  const outside = outsideQuotes(c);
  if (outside === null || UNQUOTED_META.test(outside)) return false;
  if (piped && !/(?:^|\s)--stdin(?=\s|$)/.test(outside)) return false;
  // uv run [--flags] "<script>" <command> ...: the first argument after the flags must be the script itself
  const m = /^["']?uv(?:\.exe)?["']?\s+run\s+((?:--[\w-]+\s+)*)("[^"]+"|'[^']+'|\S+)/i.exec(c);
  if (!m) return false;
  const script = norm(m[2].replace(/^["']|["']$/g, ''));
  return scripts.filter(Boolean).some((s) => script === norm(s));
}

// A Coverity read tool: the bare name, or the name qualified by the Coverity server's registration name
// (defaults.json coverityServer) with any separator, e.g. <server>/x, <server>-x, mcp__<server>__x.
// Without coverityServer, any qualifier is accepted.
const SEP = /[/\-_:.]/;
function readToolMatches(tool, readTools, server) {
  return readTools.some((t) => {
    if (tool === t) return true;
    if (!tool.endsWith(t) || !SEP.test(tool.charAt(tool.length - t.length - 1))) return false;
    if (!server) return true;
    const head = tool.slice(0, tool.length - t.length).replace(/[/\-_:.]+$/, '');
    if (!head.endsWith(server)) return false;
    const before = head.charAt(head.length - server.length - 1);
    return before === '' || SEP.test(before);
  });
}

export function createGuard(ctx) {
  function orchestratorVerdict(tool, args) {
    if (ORCHESTRATOR_TOOLS.has(tool) || SHELL_SIDE_TOOLS.has(tool)) return null;
    if (readToolMatches(tool, ctx.defaults.coverityReadTools, ctx.defaults.coverityServer)) return null;
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
