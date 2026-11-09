// Numbers and fixed sets. User-facing sentences live in messages.mjs.

export const LIMITS = Object.freeze({
  // Wait limit for Copilot CLI calls
  CALL_TIMEOUT_MS: 10_000,
  // After stop: time until an agent counts as "not stopped" (design 8.3)
  STOP_DEADLINE_MS: 8_000,
  STOP_POLL_MS: 250,
  // Status polling (design 8.1 step 3)
  STATUS_TIMEOUT_MS: 10_000,
  STATUS_FAST_MS: 2_000,
  STATUS_SLOW_MS: 5_000,
  // The screen counts as "being viewed" when it asked for the state this recently
  VIEW_FRESH_MS: 4_000,
  // Batch-run commands. The first uv run may download Python and libraries, so these are longer.
  SETTINGS_TIMEOUT_MS: 120_000,
  RUNS_TIMEOUT_MS: 120_000,
  NEW_RUN_TIMEOUT_MS: 120_000,
  SUMMARY_TIMEOUT_MS: 120_000,
  GUI_TIMEOUT_MS: 60_000,
  RUN_STOP_TIMEOUT_MS: 8_000,
  // gh (issue filing)
  GH_TIMEOUT_MS: 60_000,
  GH_CHECK_TIMEOUT_MS: 10_000,
  FILING_STALE_MS: 5 * 60_000,
  GH_LIST_LIMIT: 100,
  // Alive mark (records)
  ALIVE_INTERVAL_MS: 5_000,
  ALIVE_STALE_MS: 30_000,
  // Mechanical issue detection
  STALL_MS: 5 * 60_000,
  REPEAT_COUNT: 3,
  // Records kept
  KEEP_RUNS: 20,
  REPLY_SUMMARY_CHARS: 160,
  // Orchestrator restarts when it stops before the run finished (REQ-415)
  MAX_RESTARTS: 2,
  // Lines of recent activity on the screen
  RECENT_SHOWN: 5,
  RECENT_KEPT: 20,
});

// Copilot CLI --effort values. '' means "do not change".
export const EFFORT_LEVELS = Object.freeze(['low', 'medium', 'high', 'xhigh', 'max']);

// Roles of the run. Order = order on the screen.
export const ROLES = Object.freeze(['orchestrator', 'worker', 'reviewer', 'integrator']);
export const CHILD_ROLES = Object.freeze(['worker', 'reviewer', 'integrator']);

// Screen choices for the conditions (REQ-404)
export const IMPACT_CHOICES = Object.freeze(['絞らない', 'High', 'Medium', 'Low', 'Unspecified']);
export const INITIAL_CHOICES = Object.freeze(['設定どおり', 'する', 'しない']);
export const DEFAULT_FILTER = 'untriaged.yaml';
export const LIMIT_MIN = 1;
export const LIMIT_MAX = 100;
export const DEFAULT_LIMIT = 20; // the screen's first value for the count (the original's task.json default)
export const REWORK_MIN = 0;
export const REWORK_MAX = 2;

// Copilot CLI built-in tools the orchestrator may use (design 8.3). Names differ by model family.
export const ORCHESTRATOR_TOOLS = Object.freeze(new Set([
  'view', 'grep', 'glob', 'rg', // read / search
  'task', 'read_agent', 'list_agents', 'write_agent', // agents
  'skill',
]));
// Shell tools that start a command (checked against the allowed commands)
export const SHELL_TOOLS = Object.freeze(new Set(['bash', 'powershell', 'shell']));
// Shell tools that only look at or stop a command that is already running
export const SHELL_SIDE_TOOLS = Object.freeze(new Set(['read_bash', 'stop_bash', 'list_bash', 'read_powershell', 'stop_powershell', 'list_powershell']));
// File-writing tools
export const WRITE_TOOLS = Object.freeze(new Set(['edit', 'create', 'write', 'apply_patch', 'str_replace', 'str_replace_editor', 'insert']));

// Permission kinds of the extension itself (not shown as waits)
export const EXTENSION_PERMISSION_KINDS = Object.freeze(new Set(['extension-permission-access', 'extension-env-access']));
