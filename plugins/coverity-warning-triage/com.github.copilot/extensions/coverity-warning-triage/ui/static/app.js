// 生成プラグインの Web管理画面を組み立てる。状態の全体（GET /api/state）を1秒ごとに取り直して描く。
// 描画の部品は render.js（window.PluginUi）。状態の記号（英語）は、ここで日本語にする。
// 決まり：エージェントや記録から来た文字は、必ず文字として出す（PluginUi.h の子は文字のノードになる）。
(function () {
  'use strict';

  const { h, badge, collapsible, formatTime, formatDateTime, settingField, displayPart } = window.PluginUi;

  // ---- 日本語の表 ------------------------------------------------------------

  const RUN_STATUS = { preparing: '準備中', starting: '開始中', running: '実行中', summarizing: 'まとめ中', stopping: '停止中', finished: '終了' };
  const ACTIVE_STATUSES = ['starting', 'running', 'summarizing', 'stopping'];
  const STOPPABLE_STATUSES = ['starting', 'running', 'summarizing'];
  const PHASES = [
    { id: 'plan', label: '計画' },
    { id: 'do', label: '実行' },
    { id: 'study', label: '評価' },
  ];
  const ROLES = { orchestrator: '統括エージェント', worker: '作業エージェント', verifier: '検証エージェント' };
  const ROLE_ORDER = { orchestrator: 0, worker: 1, verifier: 2 };
  // [文字, 色の種類]。色は補助で、文字がいつも付く。
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
    all_passed: ['評価基準をすべて満たした', 'completed'],
    max_iterations: ['上限の回数に達した', 'waiting'],
    user_stopped: ['ユーザーが止めた', 'stopped'],
    aborted: ['途中で終わった', 'failed'],
  };
  // 「途中で終わった」の理由（中心の処理が入れる記号）。ここにない記号は、そのまま出す。
  const ABORT_DETAILS = {
    cut_off: '途中で切れた',
    credit_limit: 'AI クレジットの上限',
    chat_interrupted: 'チャットから中断された',
    orchestrator_failed: '統括エージェントの失敗',
    orchestrator_ended: '繰り返しを終えないまま統括エージェントが終わった',
    retry_limit: '記録のやり直しが上限を超えた',
    extension_restarted: 'エクステンションが再起動した',
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
    workflow: '手順（ワークフロー）の実行',
    'extension-management': '拡張機能の管理',
  };
  const FILING_STATES = {
    none: ['未起票', 'neutral'],
    filing: ['起票中', 'running'],
    filed: ['起票済み', 'completed'],
    failed: ['起票に失敗', 'failed'],
    unknown: ['起票したか分かりません', 'waiting'],
  };
  const ISSUE_SOURCES = { mechanical: '実行の記録から自動で見つけた課題', orchestrator: '統括エージェントが振り返って足した課題' };
  const DEFAULT_UNSTOP_HOW_TO = 'チャットで Esc を2回押すか、Copilot CLI を終えてください。';
  const AUTO_NOTICE = 'Auto のため、役割ごとのモデルが効かないことがあります。実際に動いたモデルは、エージェントごとに表示します。';
  const CLEAR_NOTICE = '実行中は、チャットで /clear（会話を消すコマンド）をしないでください。画面の更新が止まり、実行が途中で切れます。';

  const text = (value) => (value == null ? '' : String(value));
  const endReasonText = (reason) => {
    if (!reason) return '';
    const [label] = END_KINDS[reason.kind] ?? [text(reason.kind)];
    if (reason.kind !== 'aborted' || !reason.detail) return label;
    return `${label}（${ABORT_DETAILS[reason.detail] ?? text(reason.detail)}）`;
  };

  // ---- 通信 --------------------------------------------------------------------

  const key = new URLSearchParams(window.location.search).get('k') ?? '';
  const withKey = (path) => `${path}${path.includes('?') ? '&' : '?'}k=${encodeURIComponent(key)}`;
  const fileUrl = (fileId) => withKey(`/api/files/${encodeURIComponent(fileId)}`);

  async function request(method, path, body) {
    const response = await fetch(withKey(path), {
      method,
      cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      // 中身が JSON でないとき
    }
    if (!response.ok) throw new Error(data?.error ?? `うまくいきませんでした（${response.status}）`);
    return data;
  }

  // ---- 画面が覚えておくこと ----------------------------------------------------------

  const ui = {
    state: null,
    version: null,
    connected: true,
    open: new Set(), // 開いている畳み
    draft: { models: {}, efforts: {}, maxIterations: null, task: {} }, // 開始前の設定の入力中の値
    startPending: false,
    startMessage: '',
    stopPending: false,
    stopMessage: '',
    issuePending: new Set(),
    issueMessages: {},
    replies: {}, // エージェントの番号 -> { text } か { error }
    replyLoading: new Set(),
  };

  // ---- 部分ごとの描き直し ----------------------------------------------------------------

  const sections = {};
  const signatures = {};

  /** 入力（sig）が前と同じなら、その部分は描き直さない。nodes が null なら、その部分を隠す。 */
  function update(id, sig, build) {
    const json = JSON.stringify(sig);
    if (signatures[id] === json) return;
    signatures[id] = json;
    const nodes = (build() ?? []).flat(Infinity).filter(Boolean);
    const el = sections[id];
    el.replaceChildren(...nodes);
    el.hidden = nodes.length === 0;
  }

  // ---- 部分：見出し（状態の札と止めるボタン） -----------------------------------------------

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
    event.currentTarget.disabled = true; // 押したらすぐ押せなくする
    ui.stopPending = true;
    ui.stopMessage = '';
    try {
      await request('POST', '/api/stop');
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
    if (status !== 'starting' && status !== 'running' && status !== 'summarizing') ui.stopPending = false;
    const title = state.plugin?.title || '作業の画面';
    document.title = `${title} - Web管理画面`;
    const unstoppable = Boolean(run?.endReason?.unstoppable);
    const sig = [title, status, run?.endReason?.kind, unstoppable, ui.stopPending, ui.stopMessage];
    update('head', sig, () => {
      const stoppable = STOPPABLE_STATUSES.includes(status);
      return [
        h('h1', null, title),
        h(
          'div',
          { class: 'top-state', id: 'run-status' },
          statusPill(run),
          unstoppable ? badge('止めきれなかったエージェントあり', 'unstopped') : null,
        ),
        stoppable || status === 'stopping'
          ? h('button', { class: 'btn btn-danger', id: 'stop', type: 'button', disabled: ui.stopPending || status === 'stopping', on: { click: onStop } }, status === 'stopping' ? '止めています…' : '止める')
          : null,
        ui.stopMessage ? h('p', { class: 'alert alert-error top-message', role: 'alert' }, ui.stopMessage) : null,
      ];
    });
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
    const status = state.run?.status;
    const own = (state.notices ?? []).some((n) => text(n?.text ?? n).includes('/clear'));
    update('clear', [ACTIVE_STATUSES.includes(status), own], () =>
      ACTIVE_STATUSES.includes(status) && !own ? [h('p', { class: 'note note-warn', id: 'clear-notice' }, CLEAR_NOTICE)] : null,
    );
  }

  // ---- 部分：フェーズ、エージェントの枠、受け渡しの図 ------------------------------------------

  const sortedAgents = (run) =>
    [...(run.agents ?? [])].sort((a, b) => (ROLE_ORDER[a.role] ?? 9) - (ROLE_ORDER[b.role] ?? 9));

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

  /** 最後の返事の全文。最初は畳んでおき、開いたときに取る。 */
  function replyFold(agentId, summary) {
    const body = h('p', { class: 'text-block reply-full', dataset: { replyBody: agentId } }, replyBodyText(agentId));
    const fold = collapsible({ key: `reply:${agentId}`, summary, open: ui.open, onOpen: () => loadReply(agentId) }, body);
    if (ui.open.has(`reply:${agentId}`) && !ui.replies[agentId]) loadReply(agentId);
    return fold;
  }

  /** 見出しと値の1組。幅が足りないときは、値が見出しの下に回る。 */
  const fact = (term, value, valueClass) => h('div', { class: 'fact' }, h('dt', null, term), h('dd', { class: valueClass }, value));

  function agentCard(agent) {
    const [statusLabel, tone] = AGENT_STATUS[agent.status] ?? [text(agent.status), 'neutral'];
    const selected = text(agent.selectedModel);
    const actual = text(agent.actualModel);
    const differs = selected && actual && selected !== actual;
    const time = formatTime(agent.lastActiveAt);
    return h(
      'article',
      { class: `agent agent-${tone}`, dataset: { slot: agent.slot, role: agent.role, status: agent.status } },
      h('header', { class: 'agent-head' }, h('h3', null, ROLES[agent.role] ?? text(agent.role)), agent.role === 'worker' && agent.label ? h('p', { class: 'agent-label' }, agent.label) : null),
      h('p', { class: 'agent-status' }, badge(statusLabel, tone)),
      h(
        'dl',
        { class: 'facts' },
        fact('担当している作業', agent.task ? agent.task : '（なし）'),
        fact('選んだモデル', selected || '（指定なし）'),
        fact('実際に動いたモデル', [actual || '（まだ分かりません）', differs ? badge('選んだモデルと違います', 'warn') : null], 'actual-model'),
        fact('最後に動いた時刻', time ?? '（まだ動いていません）'),
      ),
      agent.replySummary ? h('p', { class: 'reply-summary' }, agent.replySummary) : null,
      // 返事がまだないエージェントには出さない（開いても、取れる全文がない）。hasReply がない状態では、これまでどおり出す
      agent.agentId && agent.hasReply !== false ? replyFold(agent.agentId, '最後の返事の全文を開く') : null,
    );
  }

  // 受け渡しの図：統括 → 作業 → 統括 → 検証 → 統括。動いているところを、フェーズで強調する。
  const FLOW = [
    { type: 'node', id: 'orchestrator-plan', label: '統括' },
    { type: 'link', id: 'to-worker', label: '計画と評価基準' },
    { type: 'node', id: 'worker', label: '作業' },
    { type: 'link', id: 'to-orchestrator', label: '成果物の場所' },
    { type: 'node', id: 'orchestrator-check', label: '統括' },
    { type: 'link', id: 'to-verifier', label: '評価基準と成果物の場所' },
    { type: 'node', id: 'verifier', label: '検証' },
    { type: 'link', id: 'to-orchestrator-last', label: '合否と指摘' },
    { type: 'node', id: 'orchestrator-last', label: '統括' },
  ];

  function activeFlowIds(run) {
    if (!ACTIVE_STATUSES.includes(run.status)) return [];
    if (run.phase === 'do') return ['to-worker', 'worker'];
    if (run.phase === 'study') return ['to-verifier', 'verifier'];
    if (run.phase === 'plan') {
      const last = (run.iterations ?? []).at(-1);
      const judged = last && (last.verdicts ?? []).length > 0 && (last.verdicts ?? []).length >= (last.criteria ?? []).length;
      return judged ? ['to-orchestrator-last', 'orchestrator-last'] : ['orchestrator-plan'];
    }
    return [];
  }

  function flowDiagram(run) {
    const active = new Set(activeFlowIds(run));
    return h(
      'div',
      { class: 'flow-wrap' },
      h('p', { class: 'flow-title' }, 'データの受け渡し'),
      h(
        'ol',
        { class: 'flow', id: 'flow' },
        FLOW.map((item) =>
          h(
            'li',
            { class: `flow-${item.type}${active.has(item.id) ? ' is-active' : ''}`, dataset: { flow: item.id, active: active.has(item.id) ? 'true' : 'false' } },
            item.label,
            active.has(item.id) ? h('span', { class: 'now' }, '（いま）') : null,
          ),
        ),
      ),
    );
  }

  const PHASE_NOTES = { starting: '開始しています', summarizing: 'まとめています', stopping: '止めています', finished: '終了しました' };

  function buildMonitor(state) {
    const run = state.run;
    if (!run || run.status === 'preparing') return update('monitor', null, () => null);
    const lastIteration = (run.iterations ?? []).at(-1);
    const sig = [run.status, run.phase, run.iteration, run.maxIterations, run.agents, lastIteration?.verdicts?.length, lastIteration?.criteria?.length];
    update('monitor', sig, () => {
      const trial = run.iteration ? `試行 ${run.iteration} 回目（上限 ${run.maxIterations} 回）` : `試行はまだ始まっていません（上限 ${run.maxIterations} 回）`;
      return [
        h(
          'div',
          { class: 'steps-row' },
          h(
            'ol',
            { class: 'phases', id: 'phases', 'aria-label': '今のフェーズ' },
            PHASES.map((phase, index) => {
              const active = run.phase === phase.id && ACTIVE_STATUSES.includes(run.status);
              return h(
                'li',
                { class: `phase${active ? ' is-active' : ''}`, 'aria-current': active ? 'step' : null, dataset: { phase: phase.id, active: active ? 'true' : 'false' } },
                h('span', { class: 'phase-no' }, index + 1),
                phase.label,
                active ? h('span', { class: 'now' }, '（いま）') : null,
              );
            }),
          ),
          h('p', { class: 'trial', id: 'trial' }, trial, h('span', { class: 'trial-help' }, '計画・実行・評価で1回の試行です')),
          !run.phase && PHASE_NOTES[run.status] ? h('p', { class: 'phase-note' }, PHASE_NOTES[run.status]) : null,
        ),
        h('div', { class: 'agents', id: 'agents' }, sortedAgents(run).map(agentCard)),
        flowDiagram(run),
      ];
    });
  }

  // ---- 部分：チャットで待っている確認 ---------------------------------------------------------

  function waitItem(wait, agents) {
    const agent = wait.slot == null ? null : agents.find((a) => a.slot === wait.slot);
    const who = wait.slot == null ? '実行全体' : agent ? `${ROLES[agent.role] ?? text(agent.role)}${agent.role === 'worker' && agent.label ? `（${agent.label}）` : ''}` : text(wait.slot);
    const detail = wait.detail ?? {};
    const list = (items) => (Array.isArray(items) && items.length ? h('ul', { class: 'plain-list' }, items.map((item) => h('li', null, item))) : null);
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
      // 囲いの外で動かす求めだけを目立たせる。「記録だけにして中で動かし直す」求め（sandboxPermissive）は、外で動かすとは書かない。
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

  // ---- 部分：止めきれなかったエージェント -----------------------------------------------------

  function buildUnstopped(state) {
    const items = state.run?.unstopped ?? [];
    update('unstopped', items, () =>
      items.length === 0
        ? null
        : [
            h('h2', null, '止まっていないエージェント'),
            h('p', null, '次のエージェントは、止める操作をしても止まりませんでした。ツールは使えないようにしてあります。'),
            h(
              'ul',
              { class: 'unstopped-list' },
              items.map((item) =>
                h('li', { dataset: { agentId: item.agentId } }, h('strong', null, ROLES[item.role] ?? text(item.role)), '：', item.howTo || DEFAULT_UNSTOP_HOW_TO),
              ),
            ),
          ],
    );
  }

  // ---- 部分：終わった理由とまとめ -----------------------------------------------------------

  function reasonView(endReason) {
    if (!endReason) return null;
    return h(
      'div',
      { class: 'reason', id: 'end-reason' },
      h('p', null, '終わった理由：', h('strong', null, endReasonText(endReason))),
      endReason.unstoppable ? h('p', null, badge('止めきれなかったエージェントあり', 'unstopped')) : null,
      endReason.kind === 'user_stopped' || endReason.unstoppable ? h('p', { class: 'help' }, 'チャットに残った確認には、どちらで答えても動きません。') : null,
    );
  }

  const displayContext = () => ({ fileUrl, open: ui.open });

  function buildEnd(state) {
    const run = state.run;
    const finished = run?.status === 'finished';
    update('end', [finished, run?.endReason, run?.summary], () => {
      if (!run || (!finished && !run.summary)) return null;
      return [
        finished ? h('h2', null, '結果') : null,
        finished ? reasonView(run.endReason) : null,
        run.summary ? displayPart({ key: 'summary', label: 'まとめ', type: 'text' }, run.summary, displayContext()) : null,
      ];
    });
  }

  // ---- 部分：注意 ---------------------------------------------------------------------------

  function buildNotices(state) {
    const isAuto = Boolean(state.run ? state.run.isAuto : state.setup?.isAuto);
    const autoText = state.setup?.autoNotice || AUTO_NOTICE;
    const notices = (state.notices ?? []).map((n) => text(n?.text ?? n)).filter(Boolean);
    update('notices', [isAuto, autoText, notices], () => {
      if (!isAuto && notices.length === 0) return null;
      return [
        isAuto ? h('p', { class: 'note note-warn', id: 'auto-notice' }, autoText) : null,
        ...notices.map((n) => h('p', { class: 'note' }, n)),
      ];
    });
  }

  // ---- 部分：開始前の設定 ---------------------------------------------------------------------

  const MODEL_ROLES = ['orchestrator', 'worker', 'verifier'];

  function validateSettings(fields) {
    const max = fields.max.read();
    if (!Number.isInteger(max) || max < 1) return '繰り返しの上限の回数は、1以上の整数で入力してください。';
    for (const [def, field] of fields.task) {
      if (def.required && field.isEmpty()) return `「${def.label ?? def.key}」を入力してください。`;
    }
    return '';
  }

  function collectSettings(fields) {
    return {
      models: Object.fromEntries(MODEL_ROLES.map((role) => [role, fields.models[role].read()])),
      efforts: Object.fromEntries(fields.efforts.map(([agent, field]) => [agent, field.read()])),
      maxIterations: fields.max.read(),
      task: Object.fromEntries(fields.task.map(([def, field]) => [def.key, field.read()])),
    };
  }

  function buildSetup(state) {
    const prep = !state.run || state.run.status === 'preparing';
    if (!prep) ui.startPending = false;
    const setup = state.setup ?? {};
    // 開始できなかったときの理由は、この画面から送った開始の答えか、中心の処理の状態（開き直したときも残る）から出す
    const startMessage = ui.startMessage || state.startError?.message || '';
    update('setup', [prep, setup, ui.startPending, startMessage], () => {
      if (!prep) return null;
      const models = (setup.models ?? []).map((m) => ({ value: m.id, label: m.name || m.id }));
      const fields = { models: {}, efforts: [], task: [], max: null };
      const modelFields = MODEL_ROLES.map((role) => {
        const chosen = ui.draft.models[role] ?? setup.defaults?.[role] ?? models[0]?.value ?? '';
        const options = models.some((m) => m.value === chosen) || !chosen ? models : [{ value: chosen, label: chosen }, ...models];
        const field = settingField(
          { key: `model-${role}`, label: `${ROLES[role]}のモデル`, type: 'select', options, default: chosen },
          chosen,
          (_k, value) => {
            ui.draft.models[role] = value;
          },
        );
        fields.models[role] = field;
        return field.el;
      });
      // 思考の深さは、エージェントごと。初めは「変えない」（上書きしない。設計書 4.6）
      const effortOptions = [{ value: '', label: '変えない' }, ...(setup.effortLevels ?? []).map((level) => ({ value: level, label: level }))];
      const effortFields = (setup.effortAgents ?? []).map(({ agent, label }) => {
        const chosen = ui.draft.efforts[agent] ?? '';
        const field = settingField(
          { key: `effort-${agent}`, label: `${label}の思考の深さ`, type: 'select', options: effortOptions, default: chosen },
          chosen,
          (_k, value) => {
            ui.draft.efforts[agent] = value;
          },
        );
        fields.efforts.push([agent, field]);
        return field.el;
      });
      fields.max = settingField(
        { key: 'maxIterations', label: '繰り返しの上限の回数', type: 'number', min: 1, step: 1, required: true, help: '計画・実行・評価を、最大で何回繰り返すかを決めます。' },
        ui.draft.maxIterations ?? setup.maxIterationsDefault ?? 3,
        (_k, value) => {
          ui.draft.maxIterations = value;
        },
      );
      const taskFields = (setup.taskSettings ?? []).map((def) => {
        // 初めの値は、まだ触っていない欄（下書きにキーがない）にだけ使う。空にした欄（null）は空のまま描き直す
        const drafted = Object.prototype.hasOwnProperty.call(ui.draft.task, def.key);
        const field = settingField(def, drafted ? (ui.draft.task[def.key] ?? '') : def.default, (k, value) => {
          ui.draft.task[k] = value;
        });
        fields.task.push([def, field]);
        return field.el;
      });
      const message = h('p', { class: 'alert alert-error', role: 'alert', id: 'start-message', hidden: !startMessage }, startMessage);

      const start = h(
        'button',
        {
          class: 'btn btn-primary',
          id: 'start',
          type: 'button',
          disabled: ui.startPending,
          on: {
            click: async (event) => {
              const button = event.currentTarget;
              const problem = validateSettings(fields);
              if (problem) {
                message.textContent = problem;
                message.hidden = false;
                ui.startMessage = problem;
                return;
              }
              button.disabled = true; // 押したらすぐ押せなくする
              button.textContent = '開始しています…';
              ui.startPending = true;
              ui.startMessage = '';
              message.hidden = true;
              try {
                await request('POST', '/api/start', collectSettings(fields));
              } catch (error) {
                ui.startPending = false;
                ui.startMessage = error.message;
                renderAll();
                return;
              }
              await poll();
            },
          },
        },
        ui.startPending ? '開始しています…' : '開始する',
      );

      return [
        h('h2', null, '開始前の設定'),
        h(
          'fieldset',
          { class: 'group' },
          h('legend', null, '役割ごとのモデル'),
          h('p', { class: 'help' }, '最初は、統括と検証に思考力の高いモデル、作業に費用の低いモデルが選ばれています。'),
          modelFields,
          // 上書きを外すと、ほかのエージェントのモデルの指定も消えること（設計書 4.6）。文は中心の処理が決める
          setup.overrideNotice ? h('p', { class: 'help', id: 'override-notice' }, setup.overrideNotice) : null,
        ),
        effortFields.length
          ? h(
              'fieldset',
              { class: 'group', id: 'effort-settings' },
              h('legend', null, 'エージェントごとの思考の深さ'),
              h('p', { class: 'help' }, '「変えない」のときは、エージェントの定義のとおりに動きます。選んだモデルが受け付けない深さは使われません。'),
              effortFields,
            )
          : null,
        h('fieldset', { class: 'group' }, h('legend', null, '繰り返し'), fields.max.el),
        taskFields.length ? h('fieldset', { class: 'group', id: 'task-settings' }, h('legend', null, '作業ごとの設定'), taskFields) : null,
        message,
        h('div', { class: 'actions' }, start),
      ];
    });
  }

  // ---- 部分：試行ごとの評価基準と合否 -----------------------------------------------------------

  function verdictBadge(verdict) {
    if (!verdict) return badge('評価前', 'standby');
    return verdict.pass ? badge('合格', 'completed') : badge('不合格', 'failed');
  }

  function iterationCard(iteration) {
    const verdicts = iteration.verdicts ?? [];
    const criteria = iteration.criteria ?? [];
    const failed = verdicts.some((v) => !v.pass);
    const judged = criteria.length > 0 && criteria.every((c) => verdicts.some((v) => v.criterionId === c.id));
    const summary = !judged ? badge('評価前', 'standby') : failed || verdicts.length === 0 ? badge('不合格あり', 'failed') : badge('すべて合格', 'completed');
    const agents = (iteration.agentIds ?? []).map((item, index) => (typeof item === 'object' && item ? item : { agentId: item, label: `エージェント ${index + 1}` }));
    // 返事がまだないエージェントには、全文を開く欄を出さない（replied がない状態では、これまでどおり出す）
    const replied = Array.isArray(iteration.replied) ? new Set(iteration.replied) : null;
    const hasReply = (agent) => agent.agentId && (!replied || replied.has(agent.agentId));
    return h(
      'article',
      { class: 'iteration', dataset: { iteration: iteration.n } },
      h('header', { class: 'iteration-head' }, h('h3', null, `試行 ${iteration.n}`), summary),
      h(
        'ul',
        { class: 'criteria' },
        criteria.map((criterion) => {
          const verdict = verdicts.find((v) => v.criterionId === criterion.id);
          return h(
            'li',
            { class: 'criterion', dataset: { criterion: criterion.id } },
            h('span', { class: 'criterion-id' }, criterion.id),
            h('span', { class: 'criterion-text' }, criterion.text),
            criterion.fromUser ? badge('ユーザーと確かめた基準', 'info') : null,
            verdictBadge(verdict),
            verdict?.note ? h('p', { class: 'criterion-note' }, h('strong', null, '指摘：'), verdict.note) : null,
          );
        }),
      ),
      iteration.plan
        ? collapsible({ key: `plan:${iteration.n}`, summary: `試行 ${iteration.n} の計画を開く`, open: ui.open }, h('p', { class: 'text-block' }, iteration.plan))
        : null,
      agents.length
        ? collapsible(
            { key: `agents:${iteration.n}`, summary: `この試行で動いたエージェント（${agents.length}体）を開く`, open: ui.open },
            h('ul', { class: 'plain-list' }, agents.map((agent) => h('li', null, h('span', null, agent.label ?? ROLES[agent.role] ?? 'エージェント'), hasReply(agent) ? replyFold(agent.agentId, '最後の返事の全文を開く') : null))),
          )
        : null,
    );
  }

  function buildIterations(state) {
    const iterations = state.run?.iterations ?? [];
    update('iterations', [iterations], () =>
      iterations.length === 0 ? null : [h('h2', null, '試行ごとの評価基準と合否'), iterations.map(iterationCard)],
    );
  }

  // ---- 部分：作業ごとの表示 --------------------------------------------------------------------

  function buildDisplays(state) {
    const run = state.run;
    const defs = state.taskDisplays ?? [];
    update('displays', [run?.status, run?.displays, defs], () => {
      if (!run || run.status === 'preparing') return null;
      const running = ACTIVE_STATUSES.includes(run.status);
      const shown = defs.filter((def) => (def.when ?? 'both') === 'both' || (def.when === 'running' && running) || (def.when === 'finished' && run.status === 'finished'));
      const extra = Object.keys(run.displays ?? {})
        .filter((k) => !defs.some((def) => def.key === k))
        .map((k) => ({ key: k, label: k, type: 'text' }));
      const parts = [...shown, ...extra];
      if (parts.length === 0) return null;
      return [h('h2', null, '作業ごとの表示'), h('div', { class: 'displays' }, parts.map((def) => displayPart(def, run.displays?.[def.key], displayContext())))];
    });
  }

  // ---- 部分：課題と Issue の案 --------------------------------------------------------------------

  async function onApprove(issue, event) {
    event.currentTarget.disabled = true; // 押したらすぐ押せなくする
    ui.issuePending.add(issue.id);
    delete ui.issueMessages[issue.id];
    try {
      await request('POST', `/api/issues/${encodeURIComponent(issue.id)}/file`);
    } catch (error) {
      ui.issueMessages[issue.id] = error.message;
    }
    ui.issuePending.delete(issue.id);
    renderAll();
    await poll();
  }

  function safeLink(url) {
    return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : null;
  }

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
    // 手順は、中心の処理が確かめた結果に合わせて決める（起票できるかを確かめている間などは、手順がない）
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
      !run || run.status === 'preparing' ? null : issuesView(issues, canFile, run.status === 'finished'),
    );
  }

  // ---- 部分：前の実行の結果（準備中だけ） ------------------------------------------------------------

  function buildPrevious(state) {
    const previous = state.previous;
    const prep = !state.run || state.run.status === 'preparing';
    update('previous', [prep, previous, [...ui.issuePending], ui.issueMessages], () => {
      if (!prep || !previous) return null;
      const canFile = previous.canFile ?? { ok: true };
      const issues = previous.issues ?? [];
      const finishedAt = formatDateTime(previous.finishedAt);
      return [
        h('h2', null, '前の実行の結果'),
        collapsible(
          { key: 'previous', summary: '前の結果を開く', open: ui.open },
          finishedAt ? h('p', { class: 'help' }, `終わった時刻：${finishedAt}`) : null,
          reasonView(previous.endReason),
          previous.summary ? displayPart({ key: 'previous-summary', label: 'まとめ', type: 'text' }, previous.summary, displayContext()) : null,
          issues.length ? issuesView(issues, canFile, false) : h('p', { class: 'muted' }, '承認していない Issue の案はありません。'),
        ),
      ];
    });
  }

  // ---- 全体 ----------------------------------------------------------------------------------------

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
    buildDisplays(state);
    buildIterations(state);
    buildIssues(state);
  }

  async function poll() {
    try {
      const state = await request('GET', '/api/state');
      const reconnected = !ui.connected;
      ui.connected = true;
      // 版の番号が変わったときだけ描き直す
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
    const layout = [
      ['head', 'header', { class: 'top' }],
      ['connection', 'div', {}],
      ['clear', 'div', {}],
    ];
    const main = h('main', { class: 'main' });
    const mainIds = ['monitor', 'waits', 'unstopped', 'end', 'notices', 'setup', 'previous', 'displays', 'iterations', 'issues'];
    for (const [id, tag, props] of layout) {
      sections[id] = h(tag, { ...props, id: `s-${id}`, hidden: true });
      root.appendChild(sections[id]);
    }
    for (const id of mainIds) {
      sections[id] = h('section', { class: 'panel', id: `s-${id}`, hidden: true });
      main.appendChild(sections[id]);
    }
    root.appendChild(main);
    root.removeAttribute('aria-busy');

    const loop = async () => {
      await poll();
      window.setTimeout(loop, 1000);
    };
    loop();
  }

  start();
})();
