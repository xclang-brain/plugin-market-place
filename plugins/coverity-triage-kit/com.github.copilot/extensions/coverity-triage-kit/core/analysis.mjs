import { LIMITS } from './constants.mjs';
import { ABORT_REASON_TEXT, ROLE_LABELS } from './messages.mjs';
import { stableStringify, summarizeText } from './util.mjs';

// Issues found mechanically in the record (log entries) and the last status (design 8.1 step 6).
// Used when a run ends and when a cut-off run is recovered. Plain Japanese (the user reads and files them).

const roleLabel = (role) => ROLE_LABELS[role] ?? ROLE_LABELS.other;
const minutes = (ms) => Math.floor(ms / 60_000);

/**
 * @param {object[]} entries log entries ({ t: 'event', at, e: neutral event with role })
 * @param {object} opts { runId, endReason, endAt, unstopped, unstoppedJobs, isAuto, models, restarts, st, reviewOn }
 * @returns {{ title: string, body: string }[]}
 */
export function analyzeEntries(entries, opts) {
  const { runId, endReason, endAt, unstopped = [], unstoppedJobs = [], isAuto = false, models = {}, restarts = 0, st = null } = opts;
  const agents = new Map();
  const toolFailures = new Map();
  const agentFailures = [];
  const repeats = [];
  const stalled = new Map();
  const last = new Map();
  const waiting = new Map();
  const lastSig = new Map();
  const others = [];

  const chain = (id) => {
    const out = [];
    const seen = new Set();
    for (let cur = id; cur && agents.has(cur) && !seen.has(cur); cur = agents.get(cur).parent) {
      out.push(cur);
      seen.add(cur);
    }
    return out;
  };
  const roleOf = (id) => {
    for (const x of chain(id)) if (agents.get(x).role) return agents.get(x).role;
    return null;
  };
  const touch = (id, at) => {
    for (const x of chain(id)) {
      const prev = last.get(x);
      if (prev !== undefined && at - prev >= LIMITS.STALL_MS && !(waiting.get(x) > 0) && !stalled.has(x)) stalled.set(x, at - prev);
      last.set(x, at);
    }
  };

  for (const entry of entries) {
    if (entry.t !== 'event' || !entry.e) continue;
    const e = entry.e;
    const at = Number.isFinite(entry.at) ? entry.at : (e.at ?? 0);
    switch (e.type) {
      case 'agent.started': {
        agents.set(e.agentId, { id: e.agentId, parent: e.parentAgentId ?? null, role: e.role ?? null, model: e.model ?? null, ended: false });
        if (e.role === 'other') others.push({ agentId: e.agentId, type: e.agentType ?? '' });
        touch(e.agentId, at);
        break;
      }
      case 'agent.configured': {
        const a = agents.get(e.agentId);
        if (a && e.model) a.model = e.model;
        touch(e.agentId, at);
        break;
      }
      case 'agent.completed':
      case 'agent.idle':
      case 'agent.failed': {
        const a = agents.get(e.agentId);
        if (a) a.ended = true;
        if (e.type === 'agent.failed') agentFailures.push({ agentId: e.agentId, error: e.error });
        touch(e.agentId, at);
        break;
      }
      case 'tool.started': {
        touch(e.agentId, at);
        if (e.arguments !== undefined) {
          const sig = `${e.toolName}\u0000${stableStringify(e.arguments)}`;
          const prev = lastSig.get(e.agentId);
          if (prev && prev.sig === sig) {
            prev.count += 1;
            if (prev.count === LIMITS.REPEAT_COUNT) {
              prev.record = { agentId: e.agentId, tool: e.toolName, count: prev.count };
              repeats.push(prev.record);
            } else if (prev.record) prev.record.count = prev.count;
          } else {
            lastSig.set(e.agentId, { sig, count: 1, record: null });
          }
        }
        break;
      }
      case 'tool.completed': {
        touch(e.agentId, at);
        if (e.success === false) {
          const role = roleOf(e.agentId);
          const key = `${role}|${e.toolName}`;
          const f = toolFailures.get(key) ?? { role, tool: e.toolName, count: 0, firstError: e.error ?? '' };
          f.count += 1;
          toolFailures.set(key, f);
        }
        break;
      }
      case 'wait.started':
        if (e.agentId) {
          touch(e.agentId, at);
          for (const x of chain(e.agentId)) waiting.set(x, (waiting.get(x) ?? 0) + 1);
        }
        break;
      case 'wait.ended':
        if (e.agentId) {
          touch(e.agentId, at);
          for (const x of chain(e.agentId)) waiting.set(x, Math.max(0, (waiting.get(x) ?? 0) - 1));
        }
        break;
      default:
        break;
    }
  }

  const unstoppedIds = new Set(unstopped.map((u) => u.agentId));
  if (Number.isFinite(endAt)) {
    for (const a of agents.values()) {
      if (a.ended || unstoppedIds.has(a.id) || stalled.has(a.id) || waiting.get(a.id) > 0) continue;
      const l = last.get(a.id);
      if (l !== undefined && endAt - l >= LIMITS.STALL_MS) stalled.set(a.id, endAt - l);
    }
  }

  const issues = [];
  const add = (title, lines) => issues.push({ title, body: lines.filter((l) => l !== null && l !== undefined).join('\n') });
  const runLine = `実行の番号：${runId}`;

  // 1. failed runs
  if (endReason?.kind === 'aborted') {
    const reason = ABORT_REASON_TEXT[endReason.detail] ?? '理由は分かりません';
    add(`実行が途中で終わりました（${reason}）`, [
      `すべての警告を処理する前に、実行が途中で終わりました。理由：${reason}。`,
      endReason.message ? `くわしい内容：${summarizeText(endReason.message, 300)}` : null,
      runLine,
    ]);
  }
  if (restarts > 0) {
    add(`統括エージェントが実行の途中で止まりました（${restarts} 回）`, [
      `統括エージェントが、実行が終わる前に止まったので、エクステンションが続きから ${restarts} 回始め直しました。`,
      runLine,
    ]);
  }
  for (const f of toolFailures.values()) {
    add(`ツール「${f.tool}」が失敗しました（${roleLabel(f.role)}）`, [
      `${roleLabel(f.role)}が使った「${f.tool}」が ${f.count} 回失敗しました。`,
      f.firstError ? `最初の失敗の内容：${summarizeText(f.firstError, 300)}` : null,
      runLine,
    ]);
  }
  for (const f of agentFailures) {
    const role = roleOf(f.agentId);
    add(`${roleLabel(role)}が失敗しました`, [
      `${roleLabel(role)}（番号：${f.agentId}）が失敗して終わりました。`,
      f.error ? `失敗の内容：${summarizeText(f.error, 300)}` : null,
      runLine,
    ]);
  }
  const items = Array.isArray(st?.items) ? st.items : [];
  const failedItems = items.filter((i) => i?.status === 'error');
  if (failedItems.length > 0) {
    add(`処理できなかった作業がありました（${failedItems.length} 件）`, [
      '次の作業は、エラーで終わりました。',
      ...failedItems.slice(0, 20).map((i) => `- ${i.id}${i.error ? `：${summarizeText(i.error, 160)}` : ''}`),
      runLine,
    ]);
  }
  const critical = (Array.isArray(st?.errors) ? st.errors : []).filter((e) => e?.level === 'critical');
  if (critical.length > 0) {
    add('一括実行で重大なエラーがありました', [
      ...critical.slice(0, 20).map((e) => `- ${e.item ? `${e.item}：` : ''}${e.kind ? `${e.kind}：` : ''}${summarizeText(e.detail ?? '', 200)}`),
      runLine,
    ]);
  }

  // 2. stuck and stopped
  for (const [id, gap] of stalled) {
    const role = roleOf(id);
    add(`${roleLabel(role)}が ${minutes(gap)} 分以上動きませんでした`, [
      `${roleLabel(role)}（番号：${id}）から ${minutes(gap)} 分以上、何の知らせもありませんでした。詰まっていた可能性があります。`,
      runLine,
    ]);
  }
  for (const r of repeats) {
    const role = roleOf(r.agentId);
    add(`${roleLabel(role)}が同じ「${r.tool}」を同じ内容で繰り返しました`, [
      `${roleLabel(role)}（番号：${r.agentId}）が、同じツール「${r.tool}」を同じ内容で ${r.count} 回続けて呼びました。`,
      runLine,
    ]);
  }
  for (const u of unstopped) {
    add(`止めても止まらないエージェントがありました（${roleLabel(u.role)}）`, [
      `${roleLabel(u.role)}（番号：${u.agentId}）が、止めても止まりませんでした。そのエージェントのツールは使えないようにしてあります。`,
      runLine,
    ]);
  }
  for (const j of unstoppedJobs) {
    add(`止めても止まらない処理がありました（${j.name}）`, [
      `処理「${j.name}」${j.pid ? `（プロセスの番号：${j.pid}）` : ''}が、止めても止まりませんでした。`,
      j.how ? `止め方：${j.how}` : null,
      runLine,
    ]);
  }

  // 3. data flow
  if (others.length > 0) {
    add('役に合わないエージェントが動きました', [
      '統括エージェントが、作業役・検証役・統合役のどれでもないエージェントを呼びました。',
      ...others.map((o) => `- ${o.type || '（種類なし）'}（番号：${o.agentId}）`),
      runLine,
    ]);
  }
  if (st?.finished && st.has_summary === false && endReason?.kind !== 'aborted') {
    add('一覧ができないまま終わりました', ['一括実行は終わりましたが、一覧（summary）ができていませんでした。', runLine]);
  }
  const mismatch = new Set();
  for (const a of agents.values()) {
    const role = a.role ?? roleOf(a.id);
    const selected = role ? models[role] : null;
    if (!selected || !a.model || mismatch.has(role)) continue;
    if (String(selected).toLowerCase() !== String(a.model).toLowerCase()) {
      mismatch.add(role);
      add(`選んだモデルと実際のモデルが違いました（${roleLabel(role)}）`, [
        `選んだモデル：${selected}`,
        `実際に動いたモデル：${a.model}`,
        isAuto ? 'Copilot CLI のモデルが Auto のため、役割ごとのモデルが効かなかった可能性があります。' : null,
        runLine,
      ]);
    }
  }
  return issues;
}

// M1, M2… for mechanical issues; O1, O2… for issues the orchestrator wrote down
export function numberIssues(drafts, prefix, source) {
  return drafts.map((d, i) => ({ id: `${prefix}${i + 1}`, title: d.title, body: d.body, source }));
}

// issues_found from status: [{ title, body, at }]
export function orchestratorIssues(st) {
  const list = Array.isArray(st?.issues_found) ? st.issues_found : [];
  return numberIssues(
    list
      .filter((i) => i && typeof i.title === 'string' && i.title.trim() !== '')
      .map((i) => ({ title: summarizeText(i.title, 200), body: typeof i.body === 'string' ? i.body : '' })),
    'O',
    'orchestrator',
  );
}
