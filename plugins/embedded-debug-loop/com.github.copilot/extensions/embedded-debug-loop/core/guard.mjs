// ツールを使う前の問い合わせ（フック onPreToolUse）。すぐに答え、例外を投げない（投げると拒まずに進むため）。
// - 利用者が止めたあと：終わりの記録のツールのほかは、すべてのエージェントのツールを拒む。
// - 実行中：統括エージェント（親のエージェント）が、ファイルを直すツールやコマンドのツールを使うのを拒む。
// - 実行中：統括エージェントが task で頼めるのは、この実行の作業エージェントと検証エージェントだけ。
// - 実行中：今の試行に作業の報告があるのに、新しい計画なしで、統括エージェントが作業エージェントに頼み直すのを拒む。
//   （実行の前・下書きのときの準備の確認や、試行 1 の計画の前の準備の確認は拒まない）

import { MSG, TOOL, otherAgentDeny, workReportedDeny } from './messages.mjs';
import { text } from './store.mjs';

export const MAIN_DENIED = Object.freeze(new Set([
  'edit', 'create', 'apply_patch',
  'bash', 'write_bash', 'read_bash', 'stop_bash', 'list_bash',
  'powershell', 'write_powershell', 'read_powershell', 'stop_powershell', 'list_powershell',
  'skill',
]));

const deny = (reason) => ({ permissionDecision: 'deny', permissionDecisionReason: reason });

// task の agent_type が、設定のエージェント（id は「<プラグイン>:<名前>」のこともある）と同じか。名前だけでもよい
const sameAgent = (given, id) => Boolean(given) && (given === id || given === id.split(':').pop());

// ツールの引数。ふつうはオブジェクトだが、JSON の文で届いても読む
function argsOf(value) {
  let v = value;
  if (typeof v === 'string') {
    try {
      v = JSON.parse(v);
    } catch {
      return {};
    }
  }
  return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
}

export function createGuard({ store, mainSessionId = () => undefined }) {
  /** 統括エージェント（親のエージェント）の呼び出しか */
  function fromMain(input, run) {
    // 子のエージェントの呼び出しには agentId が付き、sessionId は子のものになる。
    // 古い版でそれらがないときに備え、役の名前でも見分ける。
    if (input.agentId) return false;
    const main = mainSessionId();
    if (input.sessionId && main && input.sessionId !== main) return false;
    const name = text(input.agentType);
    return name !== run.worker && name !== run.verifier;
  }

  return function onPreToolUse(input) {
    try {
      const run = store.run;
      if (!run) return undefined;
      const tool = String(input?.toolName ?? '');
      if (run.status === 'stopping') return tool === TOOL.finish ? undefined : deny(MSG.stopped);
      if (run.status !== 'running') return undefined;
      if (MAIN_DENIED.has(tool)) return fromMain(input, run) ? deny(MSG.orchestratorDeny) : undefined;
      if (tool === 'task' && fromMain(input, run)) {
        const args = argsOf(input.toolArgs ?? input.arguments);
        const target = text(args.agent_type) || text(args.agentType);
        if (target && !sameAgent(target, run.worker) && !sameAgent(target, run.verifier)) {
          return deny(otherAgentDeny(run.worker, run.verifier));
        }
        const it = run.iteration > 0 ? run.iterations[run.iteration - 1] : null;
        if (it?.do && sameAgent(target, run.worker)) return deny(workReportedDeny(it.n));
      }
      return undefined;
    } catch (error) {
      console.error('[Web管理画面] ツールの問い合わせを処理できませんでした：', error);
      return undefined;
    }
  };
}
