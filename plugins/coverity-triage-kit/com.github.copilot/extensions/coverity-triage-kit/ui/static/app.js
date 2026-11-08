// The Web管理画面. Reads GET /api/state every second (every 5 s while the tab is hidden) and redraws
// only the parts whose input changed. Drawing parts are in render.js (window.PluginUi).
// Rule: text from agents, records and commands is always inserted as text (PluginUi.h makes text nodes).
(function () {
  'use strict';

  const { h, badge, collapsible, formatTime, formatDateTime, textBlock, facts, table, progress } = window.PluginUi;

  // The token comes in the URL once. Keep it in memory and remove it from the address bar and history;
  // reloading works with the cookie the server set.
  const KEY = new URLSearchParams(window.location.search).get('k') ?? '';
  if (KEY && window.history?.replaceState) window.history.replaceState(null, '', `${window.location.pathname}${window.location.hash}`);
  const POLL_MS = 1000;
  const POLL_HIDDEN_MS = 5000;
  const RECENT_SHOWN = 5;

  // ---- words ---------------------------------------------------------------------------

  const RUN_STATUS = { preparing: '準備中', starting: '開始中', running: '実行中', summarizing: 'まとめ中', stopping: '停止中', finished: '終了' };
  const ACTIVE = ['starting', 'running', 'summarizing', 'stopping'];
  const STOPPABLE = ['starting', 'running', 'summarizing'];
  const PHASES = [
    { id: 'plan', label: '計画' },
    { id: 'do', label: '実行' },
    { id: 'study', label: '評価' },
  ];
  const ROLES = { orchestrator: '統括エージェント', worker: '作業エージェント', reviewer: '検証エージェント', integrator: '統合エージェント', other: 'ほかのエージェント' };
  const AGENT_STATUS = {
    standby: ['待機', 'standby'],
    running: ['実行中', 'running'],
    waiting: ['待ち（チャットで確認を待っています）', 'waiting'],
    completed: ['完了', 'completed'],
    failed: ['失敗', 'failed'],
    stopped: ['停止', 'stopped'],
    unstopped: ['止まっていません', 'unstopped'],
  };
  const END_KINDS = {
    all_passed: ['すべて合格した', 'completed'],
    max_reached: ['上限の回数に達した', 'waiting'],
    user_stopped: ['ユーザーが止めた', 'stopped'],
    aborted: ['途中で終わった', 'failed'],
  };
  const ABORT_DETAILS = {
    run_halted: '一括実行が続けられなかった',
    orchestrator_ended: '統括エージェントが途中で終えた（始め直しの上限を超えた）',
    orchestrator_failed: '統括エージェントの失敗',
    chat_interrupted: 'チャットからの中断',
    credit_limit: 'AI クレジットの上限',
    cut_off: '/clear で切れた',
    extension_restarted: 'エクステンションの再起動',
  };
  const WAIT_KINDS = { permission: '許可', question: '質問', limit: 'AI クレジットの上限' };
  const PERMISSION_KINDS = {
    shell: 'コマンドの実行',
    write: 'ファイルの書き込み',
    read: 'ファイルの読み取り',
    url: 'Web の取得',
    mcp: '外部のツールの利用',
    memory: '記憶の保存',
    'custom-tool': '追加のツールの利用',
    hook: 'フックの実行',
    path: '場所（フォルダやファイル）の利用',
  };
  const FILING_STATES = {
    none: ['未起票', 'neutral'],
    filing: ['起票中', 'running'],
    filed: ['起票済み', 'completed'],
    failed: ['起票に失敗', 'failed'],
    unknown: ['起票したか分かりません', 'waiting'],
  };
  const ISSUE_SOURCES = { mechanical: '実行の記録から自動で見つけた課題', orchestrator: '統括エージェントが振り返って足した課題' };
  const ITEM_STATUS = { pending: ['待ち', 'standby'], in_progress: ['作業中', 'running'], done: ['終わった', 'completed'], error: ['エラー', 'failed'], merged: ['まとめた', 'neutral'] };
  const ITEM_PHASE = {
    read: '読む', judge: '判定', fix: '修正', deviation: '逸脱コメント', result: '結果を書く', check: '確かめ', write: '書く', compare: '比べる',
    copy: 'コピーの用意', save: '修正の保存', merge: 'まとめた修正',
  };
  const REVIEW_OUTCOME = { pass: ['合格', 'completed'], flag: ['要確認', 'waiting'], rework: ['差し戻し', 'failed'], unverified: ['未検証', 'standby'] };
  const DECISION = { fix: '修正', deviation: '逸脱', reject: '却下' };
  const CONFIDENCE = { high: '高', medium: '中', low: '低' };
  const CONFIDENCE_ORDER = { low: 0, medium: 1, high: 2 };
  const COUNT_LABELS = {
    issues: '警告', cids: '警告', warnings: '警告', items: '作業', tasks: '作業', groups: 'グループ', fix: '修正の推奨', deviation: '逸脱の推奨',
    low: '確信度 低', low_confidence: '確信度 低', error: 'エラー', errors: 'エラー', unprocessed: '未処理',
  };
  const APPROVAL_LABELS = { fix: '承認 修正', deviation: '承認 逸脱', reject: '承認 却下', none: '承認 （空）', null: '承認 （空）', empty: '承認 （空）', changed: '推奨から変えたもの' };
  const STATUS_COUNT_LABELS = { total: '作業', done: '終わった', in_progress: '作業中', pending: '待ち', error: 'エラー', merged: 'まとめた' };
  const DEFAULT_UNSTOP_HOW_TO = 'チャットで Esc を2回押すか、Copilot CLI を終えてください。';
  const AUTO_EXTRA = '実際に動いたモデルは、エージェントごとに表示します。';
  const CLEAR_SCREEN_NOTICE = '実行中は、チャットで /clear（会話を消すコマンド）をしないでください。画面の更新が止まり、実行が途中で切れます。';

  const text = (value) => (value == null ? '' : String(value));
  const endReasonText = (reason) => {
    if (!reason) return '';
    const [label] = END_KINDS[reason.kind] ?? [text(reason.kind)];
    const parts = [];
    if (reason.kind === 'aborted' && reason.detail) parts.push(ABORT_DETAILS[reason.detail] ?? text(reason.detail));
    for (const note of reason.notes ?? []) parts.push(text(note));
    return parts.length ? `${label}（${parts.join('、')}）` : label;
  };
  const itemLabel = (item) => {
    const cids = Array.isArray(item?.cids) ? item.cids : [];
    return /^G\d+$/.test(text(item?.id)) ? `${item.id}（${cids.length} 件）` : text(item?.id);
  };
  const elapsedText = (seconds) => {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
    return `${Math.floor(s / 3600)} 時間 ${Math.floor((s % 3600) / 60)} 分`;
  };

  // ---- talking to the server ----------------------------------------------------------------

  const withKey = (path) => (KEY ? `${path}${path.includes('?') ? '&' : '?'}k=${encodeURIComponent(KEY)}` : path);

  async function request(method, path, body) {
    const response = await fetch(withKey(path), {
      method,
      cache: 'no-store',
      credentials: 'same-origin',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      // not JSON
    }
    if (!response.ok) throw new Error(data?.error ?? `うまくいきませんでした（${response.status}）`);
    return data;
  }

  // ---- what the screen remembers ------------------------------------------------------------

  const ui = {
    state: null,
    version: null,
    connected: true,
    open: new Set(),
    draft: { models: {}, efforts: {}, maxRework: undefined, conditions: {} },
    startPending: false,
    startMessage: '',
    stopPending: false,
    stopMessage: '',
    issuePending: new Set(),
    issueMessages: {},
    replies: {},
    replyLoading: new Set(),
  };

  const sections = {};
  const signatures = {};

  /** Redraws a part only when its input (sig) changed. No nodes → the part is hidden. */
  function update(id, sig, build) {
    const json = JSON.stringify(sig);
    if (signatures[id] === json) return;
    signatures[id] = json;
    const nodes = (build() ?? []).flat(Infinity).filter(Boolean);
    const el = sections[id];
    el.replaceChildren(...nodes);
    el.hidden = nodes.length === 0;
  }

  const fact = (term, value, valueClass) => h('div', { class: 'fact' }, h('dt', null, term), h('dd', { class: valueClass }, value));

  // ---- head: state badge and stop -------------------------------------------------------------

  function statusPill(run) {
    const status = run?.status ?? 'preparing';
    if (status === 'finished') {
      const [label, tone] = END_KINDS[run.endReason?.kind] ?? ['', 'neutral'];
      return badge(label ? `終了（${label}）` : '終了', tone);
    }
    const tone = { preparing: 'neutral', starting: 'running', running: 'running', summarizing: 'running', stopping: 'waiting' }[status] ?? 'neutral';
    return badge(RUN_STATUS[status] ?? status, tone);
  }

  async function onStop(event) {
    event.currentTarget.disabled = true;
    ui.stopPending = true;
    ui.stopMessage = '';
    try {
      await request('POST', '/api/stop', {});
    } catch (error) {
      ui.stopPending = false;
      ui.stopMessage = error.message;
    }
    renderAll();
    await poll();
  }

  function buildHead(state) {
    const run = state.run;
    const status = run?.status ?? 'preparing';
    if (!STOPPABLE.includes(status)) ui.stopPending = false;
    const title = state.plugin?.title || 'Web管理画面';
    document.title = `${title} - Web管理画面`;
    const unstoppable = Boolean(run?.endReason?.unstoppable);
    update('head', [title, status, run?.endReason?.kind, unstoppable, ui.stopPending, ui.stopMessage], () => [
      h('h1', null, title),
      h('div', { class: 'top-state', id: 'run-status' }, statusPill(run), unstoppable ? badge('止めきれなかったものあり', 'unstopped') : null),
      STOPPABLE.includes(status) || status === 'stopping'
        ? h(
            'button',
            { class: 'btn btn-danger', id: 'stop', type: 'button', disabled: ui.stopPending || status === 'stopping', on: { click: onStop } },
            status === 'stopping' ? '止めています…' : '止める',
          )
        : null,
      ui.stopMessage ? h('p', { class: 'alert alert-error top-message', role: 'alert' }, ui.stopMessage) : null,
    ]);
  }

  function buildConnection() {
    update('connection', [ui.connected], () =>
      ui.connected
        ? null
        : [
            h(
              'p',
              { class: 'alert alert-error', role: 'alert', id: 'connection-lost' },
              'サーバーにつながりません。Copilot CLI を終えたか、チャットで /clear をしたかもしれません。画面の更新は止まっています。チャットでもう一度作業を頼み、示された画面を開き直してください。',
            ),
          ],
    );
  }

  function buildClearNotice(state) {
    const active = ACTIVE.includes(state.run?.status);
    update('clear', [active], () => (active ? [h('p', { class: 'note note-warn', id: 'clear-notice' }, CLEAR_SCREEN_NOTICE)] : null));
  }

  // ---- monitor: phase, slots, past agents, data flow ----------------------------------------

  async function loadReply(agentId) {
    if (ui.replyLoading.has(agentId)) return;
    ui.replyLoading.add(agentId);
    try {
      ui.replies[agentId] = { text: text((await request('GET', `/api/agents/${encodeURIComponent(agentId)}/reply`)).text) };
    } catch {
      ui.replies[agentId] = { error: true };
    }
    ui.replyLoading.delete(agentId);
    for (const el of document.querySelectorAll('[data-reply-body]')) {
      if (el.dataset.replyBody === String(agentId)) el.textContent = replyBodyText(agentId);
    }
  }

  function replyBodyText(agentId) {
    const reply = ui.replies[agentId];
    if (!reply) return '読み込んでいます…';
    return reply.error ? '返事の全文を取得できませんでした。' : reply.text;
  }

  function replyFold(agentId, summary) {
    const body = h('p', { class: 'text-block reply-full', dataset: { replyBody: agentId } }, replyBodyText(agentId));
    const fold = collapsible({ key: `reply:${agentId}`, summary, open: ui.open, onOpen: () => loadReply(agentId) }, body);
    if (ui.open.has(`reply:${agentId}`) && !ui.replies[agentId]) loadReply(agentId);
    return fold;
  }

  function agentCard(agent) {
    const [statusLabel, tone] = AGENT_STATUS[agent.status] ?? [text(agent.status), 'neutral'];
    const selected = text(agent.selectedModel);
    const actual = text(agent.actualModel);
    const differs = selected && actual && selected.toLowerCase() !== actual.toLowerCase();
    const time = formatTime(agent.lastActiveAt);
    if (!agent.agentId) {
      // a slot no agent has used yet: keep it short so every slot fits on the first screen
      return h(
        'article',
        { class: `agent agent-${tone} agent-compact`, dataset: { slot: agent.slot, role: agent.role, status: agent.status } },
        h('header', { class: 'agent-head' }, h('h3', null, ROLES[agent.role] ?? text(agent.role)), agent.role === 'worker' ? h('p', { class: 'agent-label' }, agent.label) : null),
        h('p', { class: 'agent-status' }, badge(statusLabel, tone), ' ', h('span', { class: 'help' }, `モデル：${selected || '（指定なし）'}`)),
      );
    }
    return h(
      'article',
      { class: `agent agent-${tone}`, dataset: { slot: agent.slot, role: agent.role, status: agent.status } },
      h('header', { class: 'agent-head' }, h('h3', null, ROLES[agent.role] ?? text(agent.role)), agent.role === 'worker' ? h('p', { class: 'agent-label' }, agent.label) : null),
      h('p', { class: 'agent-status' }, badge(statusLabel, tone)),
      h(
        'dl',
        { class: 'facts' },
        fact('担当', agent.task ? agent.task : '（なし）'),
        fact('選んだモデル', selected || '（指定なし）'),
        fact('実際に動いたモデル', [actual || '（まだ分かりません）', differs ? badge('選んだモデルと違います', 'warn') : null], 'actual-model'),
        fact('最後に動いた時刻', time ?? '（まだ動いていません）'),
      ),
      agent.replySummary ? h('p', { class: 'reply-summary' }, agent.replySummary) : null,
      agent.agentId && agent.hasReply ? replyFold(agent.agentId, '最後の返事の全文を開く') : null,
    );
  }

  function pastList(past) {
    if (!past.length) return null;
    return collapsible(
      { key: 'past-agents', summary: `これまでのエージェント（${past.length} 体）を開く`, open: ui.open },
      h(
        'ul',
        { class: 'past-list', id: 'past-agents' },
        past.map((a) => {
          const [label, tone] = AGENT_STATUS[a.status] ?? [text(a.status), 'neutral'];
          return h(
            'li',
            { class: 'past', dataset: { agentId: a.agentId, role: a.role } },
            h('p', null, h('strong', null, ROLES[a.role] ?? ROLES.other), ' ', badge(label, tone), a.task ? ` ${a.task}` : ''),
            h('p', { class: 'help' }, `モデル：${a.model || '（分かりません）'}${formatTime(a.startedAt) ? `／始めた時刻：${formatTime(a.startedAt)}` : ''}`),
            a.replySummary ? h('p', { class: 'reply-summary' }, a.replySummary) : null,
            a.hasReply ? replyFold(a.agentId, '最後の返事の全文を開く') : null,
          );
        }),
      ),
    );
  }

  // Data flow: orchestrator → worker → orchestrator → integrator → orchestrator → reviewer → orchestrator
  const FLOW = [
    { type: 'node', id: 'o1', label: '統括' },
    { type: 'link', id: 'to-worker', label: '指示ファイル' },
    { type: 'node', id: 'worker', label: '作業' },
    { type: 'link', id: 'from-worker', label: '結果と差分' },
    { type: 'node', id: 'o2', label: '統括' },
    { type: 'link', id: 'to-integrator', label: '同じ原因の候補', role: 'integrator' },
    { type: 'node', id: 'integrator', label: '統合', role: 'integrator' },
    { type: 'link', id: 'from-integrator', label: 'まとめ方', role: 'integrator' },
    { type: 'node', id: 'o3', label: '統括' },
    { type: 'link', id: 'to-reviewer', label: '評価基準と結果' },
    { type: 'node', id: 'reviewer', label: '検証' },
    { type: 'link', id: 'from-reviewer', label: '合否と指摘' },
    { type: 'node', id: 'o4', label: '統括' },
  ];

  function activeFlow(run) {
    if (!ACTIVE.includes(run.status)) return new Set();
    const roles = new Set(run.agents.filter((a) => a.status === 'running' || a.status === 'waiting').map((a) => a.role));
    const out = new Set();
    if (roles.has('worker')) ['to-worker', 'worker'].forEach((x) => out.add(x));
    if (roles.has('integrator')) ['to-integrator', 'integrator'].forEach((x) => out.add(x));
    if (roles.has('reviewer')) ['to-reviewer', 'reviewer'].forEach((x) => out.add(x));
    if (out.size === 0 && run.phase === 'plan') out.add('o1');
    return out;
  }

  function flowDiagram(run) {
    const active = activeFlow(run);
    const integrationOff = run.live?.integration === false;
    return h(
      'div',
      { class: 'flow-wrap' },
      h('p', { class: 'flow-title' }, 'エージェントのあいだのデータの受け渡し'),
      h(
        'ol',
        { class: 'flow', id: 'flow' },
        FLOW.map((item) => {
          const off = integrationOff && item.role === 'integrator';
          const on = active.has(item.id);
          return h(
            'li',
            { class: `flow-${item.type}${on ? ' is-active' : ''}${off ? ' is-off' : ''}`, dataset: { flow: item.id, active: on ? 'true' : 'false' } },
            item.label,
            on ? h('span', { class: 'now' }, '（いま）') : null,
            off && item.type === 'node' ? h('span', { class: 'off' }, '（使わない）') : null,
          );
        }),
      ),
    );
  }

  const PHASE_NOTES = { starting: '開始しています', summarizing: '結果をまとめています', stopping: '止めています', finished: '終了しました' };

  function buildMonitor(state) {
    const run = state.run;
    if (!run) return update('monitor', null, () => null);
    const live = run.live ?? {};
    const sig = [run.status, run.phase, run.agents, run.pastAgents, live.stage, live.stage_label, live.progress, live.integration, run.restarts, run.statusError];
    update('monitor', sig, () => {
      const stage = live.stage_label || live.stage;
      return [
        h(
          'div',
          { class: 'steps-row' },
          h(
            'ol',
            { class: 'phases', id: 'phases', 'aria-label': '今のフェーズ' },
            PHASES.map((phase, index) => {
              const on = run.phase === phase.id && ACTIVE.includes(run.status);
              return h(
                'li',
                { class: `phase${on ? ' is-active' : ''}`, 'aria-current': on ? 'step' : null, dataset: { phase: phase.id, active: on ? 'true' : 'false' } },
                h('span', { class: 'phase-no' }, index + 1),
                phase.label,
                on ? h('span', { class: 'now' }, '（いま）') : null,
              );
            }),
          ),
          h(
            'div',
            { class: 'stage', id: 'stage' },
            h('p', null, '今の段：', h('strong', null, stage ? text(stage) : '（まだ分かりません）')),
            live.progress ? h('p', { class: 'help' }, `処理した件数：${text(live.progress.label || `${live.progress.value} / ${live.progress.max}`)}`) : null,
            run.restarts ? h('p', { class: 'help' }, `統括エージェントの始め直し：${run.restarts} 回（上限 ${run.maxRestarts} 回）`) : null,
          ),
          !run.phase && PHASE_NOTES[run.status] ? h('p', { class: 'phase-note' }, PHASE_NOTES[run.status]) : null,
        ),
        run.statusError ? h('p', { class: 'alert alert-error', role: 'alert', id: 'status-error' }, run.statusError) : null,
        h('div', { class: 'agents', id: 'agents' }, (run.status === 'finished' ? run.agents.filter((a) => a.agentId) : run.agents).map(agentCard)),
        pastList(run.pastAgents ?? []),
        flowDiagram(run),
      ];
    });
  }

  // ---- waits ------------------------------------------------------------------------------------

  function waitItem(wait, agents) {
    const agent = wait.slot == null ? null : agents.find((a) => a.slot === wait.slot);
    const who = wait.slot == null && !wait.role ? '実行全体' : `${ROLES[agent?.role ?? wait.role] ?? ROLES.other}${agent?.role === 'worker' ? `（${agent.label}）` : ''}`;
    const detail = wait.detail ?? {};
    const list = (items) => (Array.isArray(items) && items.length ? h('ul', { class: 'plain-list' }, items.map((x) => h('li', null, x))) : null);
    const rows = [];
    if (wait.kind === 'permission') {
      if (detail.permissionKind) rows.push(['許可を求めている内容', PERMISSION_KINDS[detail.permissionKind] ?? text(detail.permissionKind)]);
      if (detail.toolKind) rows.push(['使おうとしているツール', text(detail.toolKind)]);
      if (detail.command) rows.push(['コマンドの全文', h('pre', { class: 'command' }, text(detail.command))]);
      if (list(detail.paths)) rows.push(['触るかもしれない場所', list(detail.paths)]);
      if (list(detail.urls)) rows.push(['つなぐかもしれない先', list(detail.urls)]);
      if (detail.warning) rows.push(['注意', h('span', { class: 'warning-text' }, text(detail.warning))]);
    } else if (wait.kind === 'question') {
      rows.push(['質問', h('span', { class: 'text-block' }, text(detail.question))]);
    } else if (wait.kind === 'limit') {
      rows.push(['内容', 'AI クレジットの上限に達しました。続けるかどうかを、チャットで答えてください。']);
    }
    return h(
      'article',
      { class: 'wait', dataset: { requestId: wait.requestId, kind: wait.kind } },
      h('h3', null, who, ' の確認：', WAIT_KINDS[wait.kind] ?? text(wait.kind)),
      detail.sandboxBypass && !detail.sandboxPermissive
        ? h('p', { class: 'alert alert-error', dataset: { sandboxBypass: 'true' } }, 'この操作は、安全のための囲い（サンドボックス）の外で動かす求めです。内容をよく確かめてから答えてください。')
        : null,
      h('dl', { class: 'facts wide' }, rows.map(([term, value]) => fact(term, value))),
    );
  }

  function buildWaits(state) {
    const run = state.run;
    const waits = run?.waits ?? [];
    update('waits', [waits, run?.agents?.map((a) => [a.slot, a.role, a.label])], () => {
      if (!waits.length) return null;
      return [
        h('h2', null, 'チャットで確認を待っています'),
        h('p', { class: 'help' }, '確認への答えは、Copilot CLI のチャットで行ってください。この画面には、答えるボタンはありません。'),
        waits.map((wait) => waitItem(wait, run.agents ?? [])),
      ];
    });
  }

  // ---- not stopped --------------------------------------------------------------------------------

  function unstoppedView(agents, jobs) {
    if (!agents.length && !jobs.length) return null;
    return [
      h('h2', null, '止まっていないもの'),
      h('p', null, '次のものは、止める操作をしても止まりませんでした。エージェントのツールは使えないようにしてあります。'),
      h(
        'ul',
        { class: 'unstopped-list' },
        agents.map((item) => h('li', { dataset: { agentId: item.agentId } }, h('strong', null, ROLES[item.role] ?? ROLES.other), '：', item.howTo || DEFAULT_UNSTOP_HOW_TO)),
        jobs.map((job) => h('li', { dataset: { job: job.name } }, h('strong', null, `処理「${text(job.name)}」${job.pid ? `（プロセスの番号 ${job.pid}）` : ''}`), '：', text(job.how) || DEFAULT_UNSTOP_HOW_TO)),
      ),
    ];
  }

  function buildUnstopped(state) {
    const agents = state.run?.unstopped ?? [];
    const jobs = state.run?.unstoppedJobs ?? [];
    update('unstopped', [agents, jobs], () => unstoppedView(agents, jobs));
  }

  // ---- end reason and overall summary ---------------------------------------------------------

  function reasonView(endReason) {
    if (!endReason) return null;
    return h(
      'div',
      { class: 'reason', id: 'end-reason' },
      h('p', null, '終わった理由：', h('strong', null, endReasonText(endReason))),
      endReason.message ? h('p', { class: 'help' }, `くわしい内容：${text(endReason.message)}`) : null,
      endReason.kind === 'user_stopped' || endReason.unstoppable ? h('p', { class: 'help' }, 'チャットに残った確認には、どちらで答えても動きません。') : null,
    );
  }

  function overallSummary(run) {
    const lines = [];
    const summary = run.summary;
    const live = run.live ?? {};
    const counts = live.counts ?? {};
    if (Number.isInteger(counts.total)) {
      lines.push(`作業 ${counts.total} 件のうち、終わった ${counts.done ?? 0} 件、エラー ${counts.error ?? 0} 件、未処理 ${(counts.pending ?? 0) + (counts.in_progress ?? 0)} 件。`);
    }
    for (const line of summary?.say ?? []) lines.push(text(line));
    return lines.join('\n');
  }

  function buildEnd(state) {
    const run = state.run;
    const finished = run?.status === 'finished';
    update('end', [finished, run?.endReason, run?.summary?.say, run?.live?.counts, run?.summaryError], () => {
      if (!finished) return null;
      return [
        h('h2', null, '結果'),
        reasonView(run.endReason),
        h('h3', null, '実行全体のまとめ'),
        textBlock(overallSummary(run), { key: 'overall-summary', open: ui.open }),
        run.summaryError ? h('p', { class: 'alert alert-error' }, run.summaryError) : null,
      ];
    });
  }

  // ---- notices ------------------------------------------------------------------------------------

  function buildNotices(state) {
    const isAuto = Boolean(state.run ? state.run.isAuto : state.setup?.isAuto);
    const autoText = `${state.setup?.autoNotice || 'Auto のため、役割ごとのモデルが効かないことがあります'}。${AUTO_EXTRA}`;
    const notices = (state.notices ?? []).map(text).filter((n) => n && !n.includes('/clear'));
    update('notices', [isAuto, autoText, notices], () => {
      if (!isAuto && notices.length === 0) return null;
      return [isAuto ? h('p', { class: 'note note-warn', id: 'auto-notice' }, autoText) : null, ...notices.map((n) => h('p', { class: 'note' }, n))];
    });
  }

  // ---- setup (before start) ------------------------------------------------------------------

  function selectField({ id, label, options, value, help, onChange }) {
    const helpId = help ? `${id}-help` : null;
    const select = h(
      'select',
      { id, 'aria-describedby': helpId, on: { change: (e) => onChange(e.currentTarget.value), input: (e) => onChange(e.currentTarget.value) } },
      options.map((o) => h('option', { value: o.value, selected: o.value === value }, o.label)),
    );
    return h('div', { class: 'field' }, h('label', { for: id }, label), select, help ? h('p', { class: 'help', id: helpId }, help) : null);
  }

  function inputField({ id, label, type = 'text', value, help, required, attrs = {}, onInput }) {
    const helpId = help ? `${id}-help` : null;
    const input = h('input', {
      id,
      type,
      value: value ?? '',
      autocomplete: 'off',
      'aria-describedby': helpId,
      ...attrs,
      on: { input: (e) => onInput(e.currentTarget.value), change: (e) => onInput(e.currentTarget.value) },
    });
    return h(
      'div',
      { class: 'field' },
      h('label', { for: id }, label, required ? h('span', { class: 'required' }, '（必須）') : null),
      input,
      help ? h('p', { class: 'help', id: helpId }, help) : null,
    );
  }

  const INITIAL_HELP = (settings) => {
    const configured = settings?.values?.initial;
    return `「設定どおり」は、今の設定（${configured === true ? 'する' : configured === false ? 'しない' : '分かりません'}）のとおりです。最初に解析するときは、数十分かかることがあります。`;
  };

  function settingsInfo(setup) {
    const s = setup.settings ?? {};
    if (s.state === 'loading') return h('p', { class: 'muted', id: 'settings-loading' }, '設定を読み込んでいます…');
    if (s.state === 'error') {
      return h(
        'div',
        { class: 'alert alert-error', id: 'settings-error', role: 'alert' },
        h('p', null, h('strong', null, '設定を読めませんでした。'), ` ${text(s.error)}`),
        s.problems?.length ? h('ul', null, s.problems.map((p) => h('li', null, p))) : null,
        setup.setupHint ? h('p', null, setup.setupHint) : null,
      );
    }
    const v = s.values ?? {};
    const yesNo = (x) => (x === true ? '使う' : x === false ? '使わない' : '（分かりません）');
    return h(
      'div',
      { class: 'settings-info', id: 'settings-info' },
      facts(
        [
          ['同じ原因の統合', `${yesNo(v.integration)}（設定で変えられます）`],
          ['結果の確かめ', 'いつも使う（この画面から始めたとき）'],
          ['同時に動かす作業エージェント', v.parallel != null ? `${v.parallel} 体` : '（分かりません）'],
          ['実行フォルダの置き場所', v.output_dir ? text(v.output_dir) : '（分かりません）'],
        ],
        'facts wide',
      ),
      s.problems?.length
        ? h('div', { class: 'alert alert-info', id: 'settings-problems' }, h('p', null, '設定に気になる点があります：'), h('ul', null, s.problems.map((p) => h('li', null, p))), setup.setupHint ? h('p', null, setup.setupHint) : null)
        : null,
    );
  }

  function requestView(setup) {
    const req = setup.request ?? { text: '', used: [], unused: [] };
    const FIELD = { filter: '条件ファイル', impact: 'Impact', limit: '件数', initial: '最初に解析するか' };
    return h(
      'div',
      { class: 'request', id: 'request' },
      h('h3', null, '頼みの文'),
      req.text ? h('p', { class: 'text-block request-text' }, req.text) : h('p', { class: 'muted' }, '（頼みの文はありません）'),
      req.used?.length ? h('p', { class: 'help', id: 'request-used' }, `頼みの文から入れた条件：${req.used.map((u) => `「${u.phrase}」→ ${FIELD[u.field] ?? u.field}`).join('、')}`) : null,
      req.unused?.length ? h('p', { class: 'note note-warn', id: 'request-unused' }, `頼みの文の「${req.unused.join('」「')}」は使いません。条件は画面で選んでください。`) : null,
    );
  }

  function readDraftConditions(setup) {
    const base = setup.conditions ?? {};
    const d = ui.draft.conditions;
    const pick = (k) => (Object.prototype.hasOwnProperty.call(d, k) ? d[k] : base[k]);
    return { filter: pick('filter') ?? '', impact: pick('impact') ?? '絞らない', limit: pick('limit'), initial: pick('initial') ?? '設定どおり' };
  }

  function validateDraft(body, setup) {
    if (!String(body.conditions.filter ?? '').trim()) return '「条件ファイルの名前」を入力してください。';
    const min = setup.reworkRange?.min ?? 0;
    const max = setup.reworkRange?.max ?? 2;
    if (!Number.isInteger(body.maxRework) || body.maxRework < min || body.maxRework > max) return `繰り返しの上限の回数は、${min} から ${max} までの整数にしてください。`;
    const limit = body.conditions.limit;
    if (limit !== null && (!Number.isInteger(limit) || limit < (setup.limitRange?.min ?? 1) || limit > (setup.limitRange?.max ?? 100))) {
      return `「調べる件数の上限」は、${setup.limitRange?.min ?? 1} から ${setup.limitRange?.max ?? 100} までの整数で入力してください（空なら条件ファイルの上限）。`;
    }
    return '';
  }

  function collectStart(setup) {
    const conditions = readDraftConditions(setup);
    const rawLimit = conditions.limit;
    const limit = rawLimit === null || rawLimit === undefined || String(rawLimit).trim() === '' ? null : Number(rawLimit);
    const rawRework = ui.draft.maxRework ?? setup.maxReworkDefault;
    return {
      models: Object.fromEntries((setup.roles ?? []).map(({ role }) => [role, ui.draft.models[role] ?? setup.defaults?.[role] ?? ''])),
      efforts: Object.fromEntries((setup.roles ?? []).map(({ role }) => [role, ui.draft.efforts[role] ?? ''])),
      maxRework: rawRework === '' || rawRework === null || rawRework === undefined ? NaN : Number(rawRework),
      conditions: { filter: String(conditions.filter ?? '').trim(), impact: conditions.impact, limit, initial: conditions.initial },
    };
  }

  function startErrorView(state) {
    const message = ui.startMessage || state.startError?.message || '';
    if (!message) return h('p', { class: 'alert alert-error', role: 'alert', id: 'start-message', hidden: true });
    const problems = ui.startMessage ? [] : (state.startError?.problems ?? []);
    const hint = !ui.startMessage && state.startError?.hint ? state.guide?.setup : '';
    return h(
      'div',
      { class: 'alert alert-error', role: 'alert', id: 'start-message' },
      h('p', null, message),
      problems.length ? h('ul', null, problems.map((p) => h('li', null, p))) : null,
      hint ? h('p', null, hint) : null,
    );
  }

  function buildSetup(state) {
    const prep = !state.run;
    if (!prep) ui.startPending = false;
    const setup = state.setup;
    update('setup', [prep, setup, ui.startPending, ui.startMessage, state.startError, state.guide], () => {
      if (!prep) return null;
      if (!setup) return [h('h2', null, '開始前の設定'), h('p', { class: 'muted' }, 'チャットで「Coverity の警告をトリアージして」と頼むと、ここに設定が出ます。')];
      const models = (setup.models ?? []).map((m) => ({ value: m.id, label: m.name || m.id }));
      const effortOptions = [{ value: '', label: '変えない' }, ...(setup.effortLevels ?? []).map((l) => ({ value: l, label: l }))];
      const roles = setup.roles ?? [];
      const modelFields = roles.map(({ role }) => {
        const chosen = ui.draft.models[role] ?? setup.defaults?.[role] ?? models[0]?.value ?? '';
        const options = models.some((m) => m.value === chosen) || !chosen ? models : [{ value: chosen, label: chosen }, ...models];
        return selectField({ id: `model-${role}`, label: `${ROLES[role]}のモデル`, options, value: chosen, onChange: (v) => (ui.draft.models[role] = v) });
      });
      const effortFields = roles.map(({ role }) =>
        selectField({ id: `effort-${role}`, label: `${ROLES[role]}の思考の深さ`, options: effortOptions, value: ui.draft.efforts[role] ?? '', onChange: (v) => (ui.draft.efforts[role] = v) }),
      );
      const c = readDraftConditions(setup);
      const filters = setup.settings?.filters ?? [];
      const listId = 'filter-choices';
      const conditionFields = [
        inputField({
          id: 'filter',
          label: '条件ファイルの名前',
          value: c.filter,
          required: true,
          help: `.coverity-triage/filters/ の中のファイルの名前を入れます。一覧にない名前も使えます。${filters.length ? `今あるもの：${filters.join('、')}` : ''}`,
          attrs: { list: listId, spellcheck: 'false' },
          onInput: (v) => (ui.draft.conditions.filter = v),
        }),
        h('datalist', { id: listId }, filters.map((f) => h('option', { value: f }))),
        selectField({
          id: 'impact',
          label: 'Impact で絞る',
          options: (setup.impactChoices ?? []).map((x) => ({ value: x, label: x })),
          value: c.impact,
          onChange: (v) => (ui.draft.conditions.impact = v),
        }),
        inputField({
          id: 'limit',
          label: '調べる件数の上限',
          type: 'number',
          value: c.limit ?? '',
          help: `${setup.limitRange?.min ?? 1} から ${setup.limitRange?.max ?? 100} まで。空なら、条件ファイルの上限です。`,
          attrs: { min: setup.limitRange?.min ?? 1, max: setup.limitRange?.max ?? 100, step: 1, inputmode: 'numeric' },
          onInput: (v) => (ui.draft.conditions.limit = v),
        }),
        selectField({
          id: 'initial',
          label: '最初に解析するか',
          options: (setup.initialChoices ?? []).map((x) => ({ value: x, label: x })),
          value: c.initial,
          help: INITIAL_HELP(setup.settings),
          onChange: (v) => (ui.draft.conditions.initial = v),
        }),
      ];
      const reworkField = inputField({
        id: 'maxRework',
        label: '繰り返しの上限の回数',
        type: 'number',
        value: ui.draft.maxRework ?? setup.maxReworkDefault ?? 1,
        required: true,
        help: `警告ごとに、検証で差し戻されたときに作り直す回数の上限です（${setup.reworkRange?.min ?? 0}〜${setup.reworkRange?.max ?? 2}）。最初の値は、設定の値です。`,
        attrs: { min: setup.reworkRange?.min ?? 0, max: setup.reworkRange?.max ?? 2, step: 1, inputmode: 'numeric' },
        onInput: (v) => (ui.draft.maxRework = v),
      });
      const loading = setup.settings?.state === 'loading';
      const start = h(
        'button',
        {
          class: 'btn btn-primary',
          id: 'start',
          type: 'button',
          disabled: ui.startPending || loading,
          on: {
            click: async (event) => {
              const button = event.currentTarget;
              const body = collectStart(setup);
              const problem = validateDraft(body, setup);
              if (problem) {
                ui.startMessage = problem;
                signatures.setup = null;
                renderAll();
                return;
              }
              button.disabled = true;
              button.textContent = '開始しています…';
              ui.startPending = true;
              ui.startMessage = '';
              try {
                await request('POST', '/api/start', body);
              } catch (error) {
                ui.startPending = false;
                ui.startMessage = error.message;
                signatures.setup = null;
                renderAll();
                return;
              }
              await poll();
            },
          },
        },
        ui.startPending ? '開始しています…' : '開始する',
      );
      const outputDir = setup.settings?.values?.output_dir;
      return [
        h('h2', null, '開始前の設定'),
        requestView(setup),
        settingsInfo(setup),
        h(
          'fieldset',
          { class: 'group', id: 'model-settings' },
          h('legend', null, '役割ごとのモデル'),
          h('p', { class: 'help' }, '最初は、統括と検証に思考力の高いモデル、作業と統合に費用の低いモデルが選ばれています（エージェントの定義の候補）。'),
          modelFields,
          setup.overrideNotice ? h('p', { class: 'help', id: 'override-notice' }, setup.overrideNotice) : null,
        ),
        h(
          'fieldset',
          { class: 'group', id: 'effort-settings' },
          h('legend', null, 'エージェントごとの思考の深さ'),
          h('p', { class: 'help' }, '「変えない」のときは、エージェントの定義のとおりに動きます。選んだモデルが受け付けない深さは使われません。'),
          effortFields,
        ),
        h('fieldset', { class: 'group', id: 'rework-settings' }, h('legend', null, '繰り返し'), reworkField),
        h('fieldset', { class: 'group', id: 'condition-settings' }, h('legend', null, '作業ごとの設定'), conditionFields),
        h(
          'div',
          { class: 'note', id: 'permission-hint' },
          h('p', null, '実行中に許可の確認で止まらないよう、開始の前に、チャットで次を打ち、最初の確認で「このセッションでは許可」を選んでおくと安心です。'),
          h('pre', { class: 'command' }, outputDir ? `/add-dir "${text(outputDir)}"` : '/add-dir "<実行フォルダの置き場所>"'),
          h('p', { class: 'help' }, 'Copilot CLI を起動するときに許可しておくしかたは、プラグインの README にあります。'),
        ),
        startErrorView(state),
        h('div', { class: 'actions' }, start, loading ? h('span', { class: 'help inline-help' }, '設定を読み込んでから押せます。') : null),
      ];
    });
  }

  // ---- live progress (while running, and the last values after the end) -------------------------

  function itemRows(items) {
    return items.map((item) => {
      const [label, tone] = ITEM_STATUS[item.status] ?? [text(item.status), 'neutral'];
      let state = badge(label, tone);
      if (item.preparing_copy) state = badge('修正用のコピーを用意中', 'running');
      else if (item.stalled) state = badge('⚠ 停止?', 'warn');
      return [
        itemLabel(item),
        ITEM_PHASE[item.phase] ?? text(item.phase || '—'),
        item.status === 'pending' || item.elapsed_seconds == null ? '—' : elapsedText(item.elapsed_seconds),
        String(item.rework_rounds ?? 0),
        state,
      ];
    });
  }

  function buildProgress(state) {
    const run = state.run;
    const live = run?.live;
    const finished = run?.status === 'finished';
    update('progress', [run?.status, live?.items, live?.errors, run?.recent, live?.initial, live?.progress, run?.permissionSteps], () => {
      if (!run || !live) return null;
      const items = Array.isArray(live.items) ? live.items : [];
      const errors = Array.isArray(live.errors) ? live.errors : [];
      const recent = (run.recent ?? []).slice(0, RECENT_SHOWN);
      const initial = live.initial ?? null;
      return [
        h('h2', null, finished ? '終わった時点の作業ごとの様子' : '作業ごとの様子'),
        h(
          'div',
          { class: 'displays' },
          h('div', { class: 'display wide', id: 'items' }, h('h3', null, '作業ごとの様子'), table(['作業', '段', '経過時間', '作り直し', '状態'], itemRows(items), '作業ごとの様子')),
          h(
            'div',
            { class: 'display wide', id: 'errors' },
            h('h3', null, '重大なエラーと注意'),
            table(
              ['作業', '種類', '内容'],
              errors.map((e) => [e.item ? text(e.item) : '実行全体', [badge(e.level === 'critical' ? '重大' : '注意', e.level === 'critical' ? 'failed' : 'warn'), ' ', text(e.kind)], text(e.detail)]),
              '重大なエラーと注意',
              [2],
            ),
          ),
          h('div', { class: 'display', id: 'recent' }, h('h3', null, '最近の動き'), recent.length ? h('ol', { class: 'recent-list' }, recent.map((line) => h('li', null, line))) : h('p', { class: 'muted' }, 'まだありません')),
          h('div', { class: 'display', id: 'initial-progress' }, h('h3', null, '初回解析の進み具合'), initial ? progress(initial, initial.used ? '初回解析' : '初回解析はしない') : h('p', { class: 'muted' }, 'まだありません')),
          h('div', { class: 'display', id: 'overall-progress' }, h('h3', null, '全体の進み具合'), progress(live.progress, '処理した件数 / 全体')),
        ),
        !finished && run.permissionSteps?.length
          ? collapsible(
              { key: 'permission-steps', summary: '許可の確認を減らすには（開く）', open: ui.open },
              h('ul', { class: 'plain-list', id: 'permission-steps' }, run.permissionSteps.map((step) => h('li', null, text(step)))),
            )
          : null,
      ];
    });
  }

  // ---- results (after the end) ---------------------------------------------------------------------

  function countPairs(obj, labels) {
    return Object.entries(obj ?? {})
      .filter(([, v]) => v === null || typeof v !== 'object')
      .map(([k, v]) => [labels[k] ?? k, v == null ? '' : String(v)]);
  }

  function itemLink(links, id) {
    if (!links?.summaryUrl) return null;
    const url = `${links.summaryUrl}${links.summaryUrl.includes('#') ? '&' : '#'}item=${encodeURIComponent(id)}`;
    return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, '開く');
  }

  function resultRows(results, links) {
    return [...results]
      .sort((a, b) => (CONFIDENCE_ORDER[a.confidence] ?? 3) - (CONFIDENCE_ORDER[b.confidence] ?? 3))
      .map((r) => {
        const decision = r.decision === undefined ? r.approval : r.decision;
        return [
          decision == null || decision === '' ? '（空）' : (DECISION[decision] ?? text(decision)),
          text(r.title) || itemLabel(r),
          DECISION[r.recommendation] ?? text(r.recommendation),
          CONFIDENCE[r.confidence] ?? text(r.confidence),
          text(r.summary ?? r.judgement ?? ''),
          itemLink(links, r.id) ?? '—',
        ];
      });
  }

  function linksView(run, guide) {
    const links = run.links;
    if (!links) return run.guiError ? h('p', { class: 'alert alert-error', id: 'links-error' }, run.guiError) : null;
    return h(
      'div',
      { class: 'links', id: 'result-links' },
      links.itemUrl
        ? h('p', null, h('a', { href: links.itemUrl, target: '_blank', rel: 'noopener noreferrer', id: 'link-item' }, `詳細レポートと差分と修正後のファイルを開く（確信度がいちばん低い ${text(links.item)}）`))
        : null,
      h('p', null, h('a', { href: links.summaryUrl, target: '_blank', rel: 'noopener noreferrer', id: 'link-approvals' }, '承認の画面を開く（結果と設定の画面）')),
      h('p', { class: 'help', id: 'apply-guide' }, text(guide?.apply)),
    );
  }

  function buildResults(state) {
    const run = state.run;
    const finished = run?.status === 'finished';
    update('results', [finished, run?.summary, run?.links, run?.guiError, run?.live?.counts, state.guide], () => {
      if (!finished) return null;
      const summary = run.summary;
      const results = Array.isArray(summary?.results) ? summary.results : [];
      const unprocessed = Array.isArray(summary?.unprocessed) ? summary.unprocessed : [];
      const countList = summary
        ? [...countPairs(summary.counts, COUNT_LABELS), ...countPairs(summary.approvals_counts, APPROVAL_LABELS)]
        : countPairs(run.live?.counts, STATUS_COUNT_LABELS);
      return [
        h('h2', null, '件数と一覧'),
        h('div', { class: 'display', id: 'counts' }, h('h3', null, '件数と承認の内訳'), countList.length ? facts(countList, 'kv') : h('p', { class: 'muted' }, 'まだありません')),
        summary
          ? h('div', { class: 'display wide', id: 'results' }, h('h3', null, '一覧'), table(['承認', 'ID', '推奨', '確信度', '見立て', '詳細'], resultRows(results, run.links), '一覧', [4]))
          : null,
        summary
          ? h(
              'div',
              { class: 'display wide', id: 'unprocessed' },
              h('h3', null, '処理できなかった作業と未処理の作業'),
              table(['作業', '理由'], unprocessed.map((u) => (typeof u === 'object' && u ? [itemLabel(u), text(u.reason ?? u.error ?? u.status ?? '')] : [text(u), ''])), '処理できなかった作業', [1]),
            )
          : null,
        linksView(run, state.guide),
      ];
    });
  }

  // ---- review results per criterion ------------------------------------------------------------

  function criterionBadges(c, result, check) {
    if (c.scope === 'run') {
      if (!check) return [badge('未確認', 'standby'), h('span', { class: 'help' }, ' 一覧ができてから確かめます')];
      return [badge(check.ok ? '合格' : '不合格', check.ok ? 'completed' : 'failed')];
    }
    if (!result) return [badge('まだ確かめていません', 'standby')];
    const out = [badge(`合格 ${result.passed ?? 0}`, 'completed'), ' ', badge(`不合格 ${result.failed ?? 0}`, (result.failed ?? 0) > 0 ? 'failed' : 'neutral')];
    if (result.not_applicable) out.push(' ', badge(`対象外 ${result.not_applicable}`, 'neutral'));
    return out;
  }

  function itemReviews(items) {
    const reviewed = items.filter((i) => i?.review && Array.isArray(i.review.findings));
    if (!reviewed.length) return null;
    return collapsible(
      { key: 'item-reviews', summary: `警告ごとの合否（${reviewed.length} 件）を開く`, open: ui.open },
      reviewed.map((item) => {
        const [label, tone] = REVIEW_OUTCOME[item.review.outcome] ?? [text(item.review.outcome || '未検証'), 'neutral'];
        return h(
          'article',
          { class: 'item-review', dataset: { item: item.id } },
          h('h4', null, itemLabel(item), ' ', badge(label, tone)),
          h(
            'ul',
            { class: 'findings' },
            item.review.findings.map((f) =>
              h(
                'li',
                { dataset: { check: f.check } },
                h('span', { class: 'criterion-id' }, text(f.check)),
                ' ',
                f.ok === null || f.ok === undefined ? badge('対象外', 'neutral') : badge(f.ok ? '合格' : '不合格', f.ok ? 'completed' : 'failed'),
                ' ',
                text(f.detail),
                f.file ? h('span', { class: 'help' }, `（${text(f.file)}${f.line ? `:${f.line}` : ''}）`) : null,
              ),
            ),
          ),
        );
      }),
    );
  }

  function buildCriteria(state) {
    const run = state.run;
    const source = run?.summary?.criteria?.length ? run.summary : run?.live;
    const criteria = Array.isArray(source?.criteria) ? source.criteria : [];
    const results = [...(run?.live?.criteria_results ?? []), ...(run?.summary?.criteria_results ?? [])];
    const checks = [...(run?.live?.run_checks ?? []), ...(run?.summary?.run_checks ?? [])];
    const items = Array.isArray(run?.live?.items) ? run.live.items : [];
    update('criteria', [criteria, results, checks, items.map((i) => [i.id, i.review]), run?.status], () => {
      if (!run || !criteria.length) return null;
      const resultOf = (id) => [...results].reverse().find((r) => r.id === id) ?? null;
      const checkOf = (id) => [...checks].reverse().find((r) => r.id === id) ?? null;
      const group = (scope, title, helpText) => {
        const list = criteria.filter((c) => (c.scope ?? 'item') === scope);
        if (!list.length) return null;
        return [
          h('h3', null, title),
          h('p', { class: 'help' }, helpText),
          h(
            'ul',
            { class: 'criteria' },
            list.map((c) => {
              const result = resultOf(c.id);
              const check = checkOf(c.id);
              return h(
                'li',
                { class: 'criterion', dataset: { criterion: c.id } },
                h('span', { class: 'criterion-id' }, text(c.id)),
                h('span', { class: 'criterion-text' }, text(c.text), c.applies_when ? h('span', { class: 'help' }, `（対象：${text(c.applies_when)}）`) : null),
                h('span', { class: 'criterion-badges' }, criterionBadges(c, result, check)),
                result?.items_failed?.length ? h('p', { class: 'criterion-note' }, h('strong', null, '不合格の警告：'), result.items_failed.join('、')) : null,
                check && !check.ok && check.detail ? h('p', { class: 'criterion-note' }, h('strong', null, '指摘：'), text(check.detail)) : null,
              );
            }),
          ),
        ];
      };
      return [
        h('h2', null, '検証の結果（評価基準ごと）'),
        group('item', '警告ごとに確かめる基準', '検証役のエージェントが、警告ごとに確かめます。数は、合格・不合格・対象外の警告の数です。'),
        itemReviews(items),
        group('run', '実行ごとに確かめる基準', '一括実行のスクリプトが、実行ごとに確かめます。'),
      ];
    });
  }

  // ---- issues and filing ---------------------------------------------------------------------------

  async function onApprove(issue, event) {
    event.currentTarget.disabled = true;
    ui.issuePending.add(issue.id);
    delete ui.issueMessages[issue.id];
    try {
      await request('POST', `/api/issues/${encodeURIComponent(issue.id)}/file`, {});
    } catch (error) {
      ui.issueMessages[issue.id] = error.message;
    }
    ui.issuePending.delete(issue.id);
    renderAll();
    await poll();
  }

  const safeLink = (url) => (typeof url === 'string' && /^https:\/\//i.test(url) ? url : null);

  function issueItem(issue, canFile) {
    const filing = issue.filing ?? { state: 'none' };
    const [stateLabel, tone] = FILING_STATES[filing.state] ?? [text(filing.state), 'neutral'];
    const busy = ui.issuePending.has(issue.id);
    const retry = filing.state === 'failed' || filing.state === 'unknown';
    const approvable = canFile.ok !== false && !busy && filing.state !== 'filing' && filing.state !== 'filed';
    const link = safeLink(filing.url);
    return h(
      'article',
      { class: 'issue', dataset: { issueId: issue.id, filing: filing.state } },
      h('h3', null, issue.title),
      h('p', { class: 'issue-meta' }, ISSUE_SOURCES[issue.source] ?? text(issue.source), ' ', badge(stateLabel, tone)),
      collapsible({ key: `issue:${issue.id}`, summary: '本文を開く', open: ui.open }, h('p', { class: 'text-block' }, issue.body)),
      filing.state === 'filed' && link ? h('p', null, h('a', { href: link, target: '_blank', rel: 'noopener noreferrer' }, '起票した Issue を開く')) : null,
      filing.reason ? h('p', { class: 'issue-reason' }, filing.reason) : null,
      ui.issueMessages[issue.id] ? h('p', { class: 'alert alert-error', role: 'alert' }, ui.issueMessages[issue.id]) : null,
      h(
        'div',
        { class: 'actions' },
        h(
          'button',
          { class: 'btn btn-primary approve', type: 'button', disabled: !approvable, on: { click: (event) => onApprove(issue, event) } },
          filing.state === 'filed' ? '起票済み' : filing.state === 'filing' || busy ? '起票しています…' : retry ? 'もう一度承認して起票する' : '承認して起票する',
        ),
      ),
    );
  }

  function cannotFileView(canFile) {
    const steps = Array.isArray(canFile.steps) ? canFile.steps : [];
    return h(
      'div',
      { class: 'alert alert-info', id: 'cannot-file' },
      h('p', null, h('strong', null, '今は Issue を起票できません。'), canFile.reason ? ` ${canFile.reason}` : ''),
      steps.length ? [h('p', null, '起票できるようにする手順：'), h('ol', null, steps.map((step) => h('li', null, step)))] : null,
    );
  }

  function issuesView(issues, canFile, finished) {
    if (issues.length === 0 && !finished) return null;
    return [
      h('h2', null, '課題と Issue の案'),
      h('p', { class: 'help' }, 'Issue は、GitHub に登録する課題の記録です。承認ボタンを押した案だけが、このプラグインの GitHub リポジトリに起票されます。'),
      canFile.ok === false && issues.length ? cannotFileView(canFile) : null,
      issues.length === 0 ? h('p', { class: 'muted' }, '今回の実行では、課題は見つかりませんでした。') : issues.map((issue) => issueItem(issue, canFile)),
    ];
  }

  function buildIssues(state) {
    const run = state.run;
    const issues = run?.issues ?? [];
    const canFile = run?.canFile ?? { ok: true };
    update('issues', [run?.status, issues, canFile, [...ui.issuePending], ui.issueMessages], () =>
      run?.status === 'finished' ? issuesView(issues, canFile, true) : null,
    );
  }

  // ---- previous run (setup screen only) ------------------------------------------------------------

  function buildPrevious(state) {
    const previous = state.previous;
    const prep = !state.run;
    update('previous', [prep, previous, [...ui.issuePending], ui.issueMessages, state.guide], () => {
      if (!prep || !previous) return null;
      const canFile = previous.canFile ?? { ok: true };
      const unfiled = (previous.issues ?? []).filter((i) => i.filing?.state !== 'filed');
      const finishedAt = formatDateTime(previous.finishedAt);
      return [
        h('h2', null, '前の実行の結果'),
        collapsible(
          { key: 'previous', summary: '前の結果を開く', open: ui.open },
          finishedAt ? h('p', { class: 'help' }, `終わった時刻：${finishedAt}`) : null,
          reasonView(previous.endReason),
          previous.runDir ? h('p', { class: 'help' }, `実行フォルダ：${text(previous.runDir)}`) : null,
          previous.links?.summaryUrl ? h('p', null, h('a', { href: previous.links.summaryUrl, target: '_blank', rel: 'noopener noreferrer' }, '前の実行の結果と設定の画面を開く')) : null,
          unstoppedView(previous.unstopped ?? [], previous.unstoppedJobs ?? []),
          unfiled.length ? issuesView(unfiled, canFile, false) : h('p', { class: 'muted' }, '起票していない Issue の案はありません。'),
        ),
      ];
    });
  }

  // ---- all ------------------------------------------------------------------------------------------

  function renderAll() {
    const state = ui.state;
    if (!state) return;
    buildHead(state);
    buildConnection();
    buildClearNotice(state);
    buildMonitor(state);
    buildWaits(state);
    buildUnstopped(state);
    buildEnd(state);
    buildNotices(state);
    buildSetup(state);
    buildPrevious(state);
    buildProgress(state);
    buildResults(state);
    buildCriteria(state);
    buildIssues(state);
  }

  async function poll() {
    try {
      const visible = document.hidden ? '0' : '1';
      const state = await request('GET', `/api/state?visible=${visible}`);
      const reconnected = !ui.connected;
      ui.connected = true;
      if (!ui.state || state.version !== ui.version) {
        ui.state = state;
        ui.version = state.version;
        renderAll();
      } else if (reconnected) {
        renderAll();
      }
    } catch {
      ui.connected = false;
      buildConnection();
    }
    document.getElementById('loading')?.remove();
  }

  function start() {
    const root = document.getElementById('app');
    for (const [id, tag, props] of [
      ['head', 'header', { class: 'top' }],
      ['connection', 'div', {}],
      ['clear', 'div', {}],
    ]) {
      sections[id] = h(tag, { ...props, id: `s-${id}`, hidden: true });
      root.appendChild(sections[id]);
    }
    const main = h('main', { class: 'main' });
    for (const id of ['monitor', 'waits', 'unstopped', 'end', 'notices', 'setup', 'previous', 'progress', 'results', 'criteria', 'issues']) {
      sections[id] = h('section', { class: 'panel', id: `s-${id}`, hidden: true });
      main.appendChild(sections[id]);
    }
    root.appendChild(main);
    root.removeAttribute('aria-busy');
    const loop = async () => {
      await poll();
      window.setTimeout(loop, document.hidden ? POLL_HIDDEN_MS : POLL_MS);
    };
    loop();
  }

  start();
})();
