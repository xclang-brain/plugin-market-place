// Copilot CLI のイベントを、実行の記録にする。
// - だれが動いているか（統括・作業・検証・その他）と、待っていること（質問・許可。実行の前や後でも示す）
// - くわしい記録の行（ツールの呼び出しを、ふつうの言葉にしたもの）
// - 作業エージェントと検証エージェントの返事（フック onSubagentStop で届く）
// エージェントの役は、名前が設定の作業エージェント・検証エージェントと同じかどうかだけで決める。

import { TOOL_PREFIX } from './messages.mjs';
import { sameAgent, text } from './store.mjs';

export const WHO = Object.freeze({ orchestrator: '統括', worker: '作業', verifier: '検証', other: 'その他' });
const ROLE_NAME = { orchestrator: '統括エージェント', worker: '作業エージェント', verifier: '検証エージェント' };
const RECORD_NAME = { open: 'Web管理画面を開く', prepare: '準備', plan: '計画', study: '評価', finish: '終わり', issue: 'Issue' };
// くわしい記録に出さない、細かいツール
const QUIET = new Set(['report_intent', 'read_bash', 'list_bash', 'read_powershell', 'list_powershell', 'read_agent', 'list_agents', 'think']);
const MAX_LINE = 160;
const MAX_QUESTION = 400;
const MAX_CALLS = 500;

const oneLine = (value) => {
  const s = String(value ?? '').replace(/\s+/g, ' ').trim();
  return s.length > MAX_LINE ? `${s.slice(0, MAX_LINE)}…` : s;
};
// 質問の文は、行の区切りを残す（長いものは切る）
const question = (value) => {
  const s = String(value ?? '').split(/\r?\n/).map((l) => l.replace(/\s+/g, ' ').trim()).filter(Boolean).join('\n');
  return s.length > MAX_QUESTION ? `${s.slice(0, MAX_QUESTION)}…` : s;
};
const argsOf = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});

export function createEventSink({ store }) {
  const calls = new Map(); // ツールの呼び出しの番号 → くわしい記録の行（失敗の印を付けるため）

  function roleOf(agentType) {
    const s = store.activeRun() ?? store.settings;
    if (sameAgent(agentType, s.worker)) return 'worker';
    if (sameAgent(agentType, s.verifier)) return 'verifier';
    return 'other';
  }
  const roleOfEvent = (event) => (event.agentId ? store.live.agents.get(event.agentId)?.role ?? 'other' : 'orchestrator');
  const targetName = (agentType) => {
    const role = roleOf(agentType);
    return ROLE_NAME[role] ?? `エージェント「${oneLine(agentType) || '名前なし'}」`;
  };

  /** ツールの呼び出しを、ふつうの言葉の1行にする。出さないものは null */
  function describeTool(name, rawArgs) {
    const a = argsOf(rawArgs);
    switch (name) {
      case 'bash':
      case 'powershell':
        return `コマンドを実行：${oneLine(a.command)}`;
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
      case 'apply_patch':
        return `ファイルを直す：${typeof a.path === 'string' ? oneLine(a.path) : '（パッチ）'}`;
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
          if (agent && run) {
            const name = ROLE_NAME[agent.role] ?? 'エージェント';
            const failed = event.type === 'subagent.failed';
            store.activity(WHO[agent.role], failed ? `${name}が失敗しました：${oneLine(data.error?.message ?? data.error)}` : `${name}が返事をしました`);
          }
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
          const line = run ? describeTool(text(data.toolName), data.arguments) : null;
          if (line) {
            const entry = store.activity(WHO[roleOfEvent(event)], line);
            if (entry && data.toolCallId) {
              calls.set(data.toolCallId, entry);
              if (calls.size > MAX_CALLS) calls.delete(calls.keys().next().value);
            }
          }
          break;
        }
        case 'tool.execution_complete': {
          const entry = calls.get(data.toolCallId);
          calls.delete(data.toolCallId);
          if (entry && data.success === false) {
            entry.text += '（失敗）';
            store.changed();
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
          if (run?.status === 'stopping') store.finishStopped();
          break;
        default:
          return;
      }
      updateActive();
    } catch (error) {
      console.error('[Web管理画面] イベントを記録できませんでした：', error);
    }
  }

  /** 子のエージェントが手番を終えたときの返事。作業の報告と、検証の返事を記録する */
  function onSubagentStop(input) {
    try {
      const run = store.activeRun();
      const response = typeof input?.response === 'string' ? input.response : '';
      if (!run || !response) return undefined;
      const role = roleOf(text(input.agentType) || input.agentName);
      const it = store.current();
      if (role === 'worker' && it && !it.do) {
        it.do = { report: response, at: store.iso() };
        store.log('worker.reply', { iteration: it.n, report: response });
        store.changed();
      } else if (role === 'verifier' && it) {
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
