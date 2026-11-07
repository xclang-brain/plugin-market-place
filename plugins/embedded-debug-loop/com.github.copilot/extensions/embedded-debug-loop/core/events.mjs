// Copilot CLI のイベントを、実行の記録にする。
// - だれが動いているか（統括・作業・検証・その他）と、待っていること（質問・許可。実行の前や後でも示す）
// - くわしい記録の行（ツールの呼び出しを、ふつうの言葉にしたもの）
// - 作業エージェントと検証エージェントの返事（フック onSubagentStop で届く）。試行の記録に入れない返事も、理由を付けて events.jsonl に残す
// エージェントの役は、名前（Copilot CLI の agentType。エージェントの一覧の id と同じ）が、設定の作業エージェント・検証エージェントと同じかどうかだけで決める。

import { IGNORED, TOOL_PREFIX } from './messages.mjs';
import { text } from './store.mjs';

export const WHO = Object.freeze({ orchestrator: '統括', worker: '作業', verifier: '検証', other: 'その他', user: '利用者' });
const ROLE_NAME = { orchestrator: '統括エージェント', worker: '作業エージェント', verifier: '検証エージェント' };
const RECORD_NAME = { open: 'Web管理画面を開く', prepare: '準備', plan: '計画', study: '評価', finish: '終わり', issue: 'Issue' };
// くわしい記録に出さない、細かいツール
const QUIET = new Set(['report_intent', 'read_bash', 'list_bash', 'read_powershell', 'list_powershell', 'read_agent', 'list_agents', 'think']);
const MAX_LINE = 160;
const MAX_COMMAND = 1000;
const MAX_QUESTION = 400;
const MAX_CALLS = 500;

const oneLine = (value) => {
  const s = String(value ?? '').replace(/\s+/g, ' ').trim();
  return s.length > MAX_LINE ? `${s.slice(0, MAX_LINE)}…` : s;
};
// コマンドの文は、長くても頭と終わり（動かしたものの名前と引数）を残す。画面では、さらに真ん中を省いて見せる
const commandLine = (value) => {
  const s = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (s.length <= MAX_COMMAND) return s;
  const half = Math.floor((MAX_COMMAND - 1) / 2);
  return `${s.slice(0, half)}…${s.slice(-half)}`;
};
// 質問の文は、行の区切りを残す（長いものは切る）
const question = (value) => {
  const s = String(value ?? '').split(/\r?\n/).map((l) => l.replace(/\s+/g, ' ').trim()).filter(Boolean).join('\n');
  return s.length > MAX_QUESTION ? `${s.slice(0, MAX_QUESTION)}…` : s;
};
const argsOf = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});

// パッチの文の中の、ファイルの行（*** Add File: など）
const PATCH_FILE = /^\*\*\* (?:(?:Add|Update|Delete) File|Move to):[ \t]*(.+?)[ \t]*$/gm;

/** ファイルを直すツール（edit・create・apply_patch）が変えるファイルのパス。ほかのツールなら空の配列 */
export function editedPaths(name, rawArgs) {
  const a = argsOf(rawArgs);
  if (name !== 'edit' && name !== 'create' && name !== 'apply_patch') return [];
  if (text(a.path)) return [text(a.path)];
  if (name !== 'apply_patch') return [];
  const patch = typeof rawArgs === 'string' ? rawArgs : typeof a.input === 'string' ? a.input : typeof a.patch === 'string' ? a.patch : '';
  return [...new Set([...patch.matchAll(PATCH_FILE)].map((m) => m[1]))];
}

/**
 * 子のエージェントが終わったときの、くわしい記録の行。
 * cancelled は、返事をせずに途中で切られた印（「止める」で止めたときなど）。返事をしたとは書かない
 */
function endLine(role, type, data, run) {
  const name = ROLE_NAME[role] ?? 'エージェント';
  if (type === 'subagent.failed') return `${name}が失敗しました：${oneLine(data.error?.message ?? data.error)}`;
  if (data.cancelled === true) return `${name}は途中で止まりました${run.status === 'stopping' ? '（利用者が止めたため）' : ''}`;
  return `${name}が返事をしました`;
}

/**
 * @param {object} options
 * @param {Function} [options.onStopped] 止めている実行を、終わりの知らせ（session.idle）で終えたときに呼ぶ
 */
export function createEventSink({ store, onStopped = () => {} }) {
  const calls = new Map(); // ツールの呼び出しの番号 → くわしい記録の行（失敗の印を付けるため）
  const edits = new Map(); // ファイルを直すツールの呼び出しの番号 → 変えるファイルのパス

  // s は、作業・検証エージェントの名前を持つもの（実行か設定）
  function roleOf(agentType, s = store.activeRun() ?? store.settings) {
    const name = text(agentType);
    if (name === s.worker) return 'worker';
    if (name === s.verifier) return 'verifier';
    return 'other';
  }
  const roleOfEvent = (event) => (event.agentId ? store.live.agents.get(event.agentId)?.role ?? 'other' : 'orchestrator');
  const targetName = (agentType) => {
    const role = roleOf(agentType);
    return ROLE_NAME[role] ?? `エージェント「${oneLine(agentType) || '名前なし'}」`;
  };

  /**
   * ツールの呼び出しを、ふつうの言葉の1行にする。出さないものは null。
   * コマンドに説明（description）があれば、行には説明を出し、コマンドの全文は extra.command に残す（画面では title に出す）
   */
  function describeTool(name, rawArgs) {
    if (name === 'bash' || name === 'powershell') {
      const a = argsOf(rawArgs);
      const command = commandLine(a.command);
      const description = oneLine(a.description);
      if (!description) return { text: `コマンドを実行：${command}` };
      return { text: `コマンドを実行：${description}`, extra: command ? { command } : undefined };
    }
    const line = toolLine(name, rawArgs);
    return line === null ? null : { text: line };
  }

  /** コマンドのほかのツールの行 */
  function toolLine(name, rawArgs) {
    const a = argsOf(rawArgs);
    switch (name) {
      case 'write_bash':
      case 'write_powershell':
        return 'コマンドに入力';
      case 'stop_bash':
      case 'stop_powershell':
        return 'コマンドを止める';
      case 'view':
        return `ファイルを読む：${oneLine(a.path)}`;
      case 'edit':
      case 'create':
        return `ファイルを直す：${oneLine(a.path)}`;
      case 'apply_patch': {
        const paths = editedPaths(name, rawArgs);
        return `ファイルを直す：${paths.length ? oneLine(paths.join('、')) : '（パッチ）'}`;
      }
      case 'grep':
      case 'glob':
      case 'rg':
        return `探す：${oneLine(a.pattern ?? a.query)}`;
      case 'skill':
        return `スキルを使う：${oneLine(a.skill ?? a.name)}`;
      case 'task':
        return `${targetName(a.agent_type ?? a.agentType)}に頼む：${oneLine(a.description)}`;
      case 'ask_user':
        return '利用者に質問';
      default:
        if (name.startsWith(TOOL_PREFIX)) return `記録：${RECORD_NAME[name.slice(TOOL_PREFIX.length)] ?? name.slice(TOOL_PREFIX.length)}`;
        return QUIET.has(name) || name === '' ? null : `ツールを使う：${oneLine(name)}`;
    }
  }

  /** 図で光らせるエージェント：最後に始まって終わっていない子、なければ動いている統括 */
  function updateActive() {
    const run = store.activeRun();
    if (!run) return;
    const agents = [...store.live.agents.values()];
    const last = agents[agents.length - 1];
    const next = last ? { role: last.role, since: last.since } : store.live.mainSince ? { role: 'orchestrator', since: store.live.mainSince } : null;
    if (JSON.stringify(next) !== JSON.stringify(run.active)) {
      run.active = next;
      store.changed();
    }
  }

  // いまの試行で、検証エージェントを呼んだかの印。評価の記録の後に呼んだときは、評価の側に付ける
  function markVerifierCalled() {
    const it = store.current();
    if (!it) return;
    if (it.study) it.study.verifierCalled = true;
    else it._verifierCalled = true;
  }

  function onEvent(event) {
    try {
      if (!event || typeof event.type !== 'string') return;
      const data = argsOf(event.data);
      const at = Number.isFinite(Date.parse(event.timestamp)) ? new Date(event.timestamp).toISOString() : store.iso();
      const run = store.activeRun();
      switch (event.type) {
        case 'subagent.started': {
          if (!event.agentId) break;
          const role = roleOf(text(data.agentType) || data.agentName);
          store.live.agents.set(event.agentId, { role, since: at });
          if (role === 'verifier') markVerifierCalled();
          break;
        }
        case 'subagent.completed':
        case 'subagent.failed': {
          const agent = store.live.agents.get(event.agentId);
          store.live.agents.delete(event.agentId);
          if (agent && run) store.activity(WHO[agent.role], endLine(agent.role, event.type, data, run));
          break;
        }
        case 'assistant.turn_start':
        case 'user.message':
          if (!event.agentId) store.live.mainSince ??= at;
          break;
        case 'assistant.message':
          if (!event.agentId) {
            store.live.mainSince ??= at;
            if (run && text(data.content)) store.activity(WHO.orchestrator, `チャットに書く：${oneLine(data.content)}`);
          }
          break;
        case 'tool.execution_start': {
          if (!event.agentId) store.live.mainSince ??= at;
          const paths = run ? editedPaths(text(data.toolName), data.arguments) : [];
          if (paths.length) {
            store.noteFiles(paths, 'pending');
            if (data.toolCallId) {
              edits.set(data.toolCallId, paths);
              if (edits.size > MAX_CALLS) edits.delete(edits.keys().next().value);
            }
          }
          const line = run ? describeTool(text(data.toolName), data.arguments) : null;
          if (line) {
            const entry = store.activity(WHO[roleOfEvent(event)], line.text, line.extra);
            if (entry && data.toolCallId) {
              calls.set(data.toolCallId, entry);
              if (calls.size > MAX_CALLS) calls.delete(calls.keys().next().value);
            }
          }
          break;
        }
        case 'tool.execution_complete': {
          const edited = edits.get(data.toolCallId);
          edits.delete(data.toolCallId);
          if (edited) store.noteFiles(edited, data.success === false ? 'failed' : 'ok');
          const entry = calls.get(data.toolCallId);
          calls.delete(data.toolCallId);
          if (entry) {
            const exitCode = data.shellExecution?.exitCode;
            const denied = data.error?.code === 'denied' || /denied/i.test(String(data.error?.message ?? data.error ?? ''));
            if (data.success === false && denied) {
              entry.text += '（断りました）';
              store.log('denied', { tool: entry.text, reason: String(data.error?.message ?? data.error ?? '').slice(0, 300) });
              store.changed();
            } else if (data.success === false) {
              entry.text += '（失敗）';
              store.changed();
            } else if (typeof exitCode === 'number' && exitCode !== 0) {
              entry.text += `（失敗：終了コード ${exitCode}）`;
              store.changed();
            }
          }
          break;
        }
        // ask_user の質問。Copilot CLI の版によって、user_input と elicitation のどちらかで届く
        case 'user_input.requested':
          store.setWaiting(data.requestId, { kind: 'question', text: question(data.question) });
          break;
        case 'elicitation.requested':
          store.setWaiting(data.requestId, { kind: 'question', text: question(data.message) });
          break;
        case 'permission.requested': {
          if (data.resolvedByHook) break;
          const r = argsOf(data.permissionRequest);
          const what = r.fullCommandText ?? r.fileName ?? r.path ?? r.url ?? r.toolName ?? r.intention ?? r.kind;
          store.setWaiting(data.requestId, { kind: 'permission', text: oneLine(what) });
          break;
        }
        case 'user_input.completed':
        case 'elicitation.completed':
        case 'permission.completed':
          if (store.pending.size > 0) store.setWaiting(data.requestId, null);
          break;
        case 'session.error':
          if (run) store.activity(WHO.other, `エラー：${oneLine(data.message)}`);
          break;
        case 'session.idle':
          if (event.agentId) break; // セッション全体が止まったときだけを見る
          store.live.mainSince = null;
          store.live.agents.clear();
          if (store.pending.size > 0) store.setWaiting(null, null);
          if (run?.status === 'stopping' && store.finishStopped()) onStopped();
          break;
        default:
          return;
      }
      updateActive();
    } catch (error) {
      console.error('[Web管理画面] イベントを記録できませんでした：', error);
    }
  }

  /**
   * 子のエージェントが手番を終えたときの返事。作業の報告と、検証の返事を記録する。
   * 試行の記録に入れない返事も、捨てずに events.jsonl に ignored: true と理由を付けて残す
   * （下書きのときの準備の確認は、統括エージェントが prepare で記録するので除く）
   */
  function onSubagentStop(input) {
    try {
      const response = typeof input?.response === 'string' ? input.response : '';
      if (!response) return undefined;
      const agentType = text(input.agentType) || input.agentName;
      const run = store.activeRun();
      if (!run) {
        // 終わった実行の後に届いた返事。新しい下書き（open の後）があれば、その準備の確認なので残さない
        const last = store.run?.status === 'finished' && !store.draft ? store.run : null;
        const role = last ? roleOf(agentType, last) : 'other';
        if (role === 'worker' || role === 'verifier') {
          store.log(`${role}.reply`, { iteration: last.iteration, report: response, ignored: true, reason: IGNORED.finished }, { finished: true });
        }
        return undefined;
      }
      const role = roleOf(agentType);
      if (role !== 'worker' && role !== 'verifier') return undefined;
      const it = store.current();
      const ignore = (reason, extra = {}) => store.log(`${role}.reply`, { iteration: it?.n ?? 0, report: response, ...extra, ignored: true, reason });
      // 止めた後に届いた返事は、途中で切れたもの。試行の記録には入れず、振り返りの記録にだけ残す
      if (run.status === 'stopping') {
        ignore(IGNORED.afterStop, { afterStop: true });
        return undefined;
      }
      if (!it) {
        ignore(IGNORED.noPlan);
        return undefined;
      }
      if (role === 'worker') {
        if (it.do) {
          ignore(IGNORED.reported(it.n));
          return undefined;
        }
        it.do = { report: response, at: store.iso() };
        store.log('worker.reply', { iteration: it.n, report: response });
        store.changed();
      } else {
        markVerifierCalled();
        if (it.study) it.study.verifierReport = response;
        else it._verifierReport = response;
        store.log('verifier.reply', { iteration: it.n, report: response });
        store.changed();
      }
    } catch (error) {
      console.error('[Web管理画面] 子のエージェントの返事を記録できませんでした：', error);
    }
    return undefined;
  }

  return { onEvent, onSubagentStop, describeTool };
}
