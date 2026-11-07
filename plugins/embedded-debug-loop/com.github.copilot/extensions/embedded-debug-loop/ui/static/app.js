/*
 * Web管理画面の動き。
 * 1.5 秒ごとに /api/state で今の状態を読み、画面を描き直す。
 * 外のファイルやライブラリは使わない。
 */
(function () {
  'use strict';

  // URL の合言葉。サーバーはクッキーでも確かめるが、問い合わせには念のため付ける。
  const K = new URLSearchParams(location.search).get('k') || '';
  const POLL_MS = 1500;
  const POLL_HIDDEN_MS = 5000;
  const PREVIEW_LINES = 12;

  // ---------- 画面に出す言葉 ----------

  const ROLE = {
    orchestrator: '統括エージェント',
    worker: '作業エージェント',
    verifier: '検証エージェント',
    other: 'ほかのエージェント',
  };
  const EFFORT = { low: '浅い（速い）', medium: 'ふつう', high: '深い', xhigh: 'とても深い（遅い）' };
  const DEFAULT_LABEL = '既定（エージェントの定義のまま）';
  const OUTCOME = {
    success: { label: '成功', cls: 'ok', mark: '✓' },
    failure: { label: '失敗', cls: 'ng', mark: '✗' },
    stopped: { label: '途中で止めた', cls: 'warn', mark: '■' },
    'needs-human': { label: '人の判断が必要', cls: 'warn', mark: '！' },
  };
  const VERDICT = {
    pass: { label: '合格', cls: 'ok', mark: '✓' },
    fail: { label: '不合格', cls: 'ng', mark: '✗' },
    unknown: { label: '判断できない', cls: 'unk', mark: '？' },
  };
  const WHO_CLASS = { 統括: 'o', 作業: 'w', 検証: 'v', 利用者: 'u' };
  const MSG = {
    offline: 'この画面のサーバーとつながりません。Copilot CLI を閉じたか、起動し直したのかもしれません。'
      + '起動し直したときは、統括エージェントに頼み直し、チャットに出る新しい URL を開いてください。',
    badKey: 'この画面の URL が正しくないか、古くなっています。チャットに出た URL を開き直してください。',
    notReady: '作業エージェントの準備ができていません。下の「作業エージェントの準備」の「足りないもの」を直してから、「開始」を押してください。'
      + '開始すると、統括エージェントが準備を確かめ直します。',
    notReadyShort: '準備ができていません。直してから押してください。',
  };

  // ---------- 画面の状態 ----------

  const ui = {
    tab: null, // 'settings' | 'run' | 'result'
    snap: null, // 最後に読んだ状態
    open: Object.create(null), // 開け閉めできる欄の開き具合
    expanded: Object.create(null), // 「全部を見る」を押した欄
    dirty: false, // 設定の欄を利用者が書き換えたか
    serverForm: '', // サーバーの設定の値（書き換え中に変わったかを見る）
    optionsKey: '',
    notice: null, // 開始したときの注意 { runId, text }
    pastRun: null, // 結果の画面で見ている、過去の実行
    drawn: Object.create(null), // 欄ごとに、最後に描いた内容
    rev: 0, // 開け閉めなどで増やし、描き直させる
    spoken: Object.create(null), // 読み上げた言葉（知らせの種類ごと）
    liveText: '', // 読み上げの欄に、最後に書いた（書く）言葉
    timer: 0,
    polling: false,
    failures: 0,
    busy: false,
  };

  const $ = (id) => document.getElementById(id);

  // ---------- 小さな道具 ----------

  function h(tag, props, ...kids) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value == null || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? '' : String(value));
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false || kid === '') continue;
      node.append(kid instanceof Node ? kid : String(kid));
    }
    return node;
  }

  const str = (v) => (v == null ? '' : String(v));
  const arr = (v) => (Array.isArray(v) ? v : []);
  const normalize = (text) => str(text).replace(/\r\n?/g, '\n');

  function toDate(v) {
    if (v == null || v === '') return null;
    const d = new Date(typeof v === 'number' ? v : String(v));
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const pad = (n) => String(n).padStart(2, '0');
  function hm(v) {
    const d = toDate(v);
    return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : '';
  }
  function hms(v) {
    const d = toDate(v);
    return d ? `${hm(d)}:${pad(d.getSeconds())}` : '';
  }
  function dateTime(v) {
    const d = toDate(v);
    if (!d) return '';
    const md = `${d.getMonth() + 1}月${d.getDate()}日 ${hm(d)}`;
    return d.getFullYear() === new Date().getFullYear() ? md : `${d.getFullYear()}年${md}`;
  }
  function span(ms) {
    const m = Math.floor(ms / 60000);
    if (m < 1) return '1 分未満';
    if (m < 60) return `${m} 分`;
    return `${Math.floor(m / 60)} 時間 ${m % 60} 分`;
  }
  function since(v) {
    const d = toDate(v);
    return d ? span(Math.max(0, Date.now() - d.getTime())) : '';
  }
  function firstLine(text, max = 80) {
    const line = normalize(text).split('\n').map((s) => s.trim()).find(Boolean) || '';
    return line.length > max ? `${line.slice(0, max)}…` : line;
  }

  // 報告の要点：頭の「試行 N の報告」のような見出しの行は飛ばし、2 行までをつなげる
  function gist(text, max = 120) {
    const lines = normalize(text).split('\n').map((x) => x.trim()).filter(Boolean);
    const body = lines.length > 1 && /^試行\s*[：:]?\s*\d+/.test(lines[0]) && lines[0].length <= 24 ? lines.slice(1) : lines;
    // 「できなかった」「失敗」の行があれば、それを先に見せる（何ができなかったかが分かるように）
    const bad = body.filter((x) => /できなかった|失敗/.test(x));
    const picked = bad.length ? [...bad, ...body.filter((x) => !bad.includes(x))] : body;
    const joined = picked.slice(0, 2).join(' ／ ');
    return joined.length > max ? `${joined.slice(0, max)}…` : joined;
  }

  // 「統括エージェント」を「統括」と「エージェント」の間でだけ折り返せるようにする
  function roleWords(head) {
    return [head, h('wbr'), 'エージェント'];
  }

  function isActive(run) {
    return !!run && (run.status === 'running' || run.status === 'stopping');
  }
  // チャットで答えを待っていること。実行の前や、終わった後でも出す
  function waitingOf(snap) {
    if (snap.waiting) return snap.waiting;
    return isActive(snap.run) ? snap.run.waiting || null : null;
  }
  // 実行中に、チャットで利用者の答え（質問の答え・許可）を待っているか。このあいだは、どの段も「動いている」と言わない
  function waitsOnUser(run) {
    const latest = ui.snap && ui.snap.run;
    return !!run && run.status === 'running' && !!latest && latest.id === run.id && !!waitingOf(ui.snap);
  }
  // 「<プラグイン>:<名前>」と「<名前>」を同じエージェントとみなす
  function sameAgent(a, b) {
    const bare = (x) => str(x).trim().slice(str(x).trim().lastIndexOf(':') + 1);
    return !!str(a).trim() && !!str(b).trim() && (str(a).trim() === str(b).trim() || bare(a) === bare(b));
  }
  // 評価の条件の、番号と文。記録に番号と文がない（古い記録の）ときは、「C1」を実行の条件の文に引き当てる
  function criterionOf(run, v) {
    const raw = str(v.criterion).trim();
    if (v.no || v.text) return { no: Number(v.no) || 0, text: str(v.text) || raw };
    const m = /^C(\d+)$/i.exec(raw);
    const list = arr(run && run.criteria);
    if (m && list[Number(m[1]) - 1]) return { no: Number(m[1]), text: str(list[Number(m[1]) - 1]) };
    return { no: 0, text: raw };
  }
  function criterionLine(run, v) {
    const c = criterionOf(run, v);
    return h('span', { class: 'crit' }, c.no ? h('span', { class: 'crit-no' }, `C${c.no}`) : null, c.text);
  }
  function stagePhase(snap) {
    if (isActive(snap.run)) return 'running';
    if (snap.draft) return 'draft';
    if (snap.run && snap.run.status === 'finished') return 'finished';
    return 'empty';
  }

  // 描き直すときに、フォーカスのあった部品へフォーカスを戻す
  function fill(container, nodes) {
    const active = document.activeElement;
    const fk = active && container.contains(active) && active.dataset ? active.dataset.fk : null;
    container.replaceChildren(...arr(nodes).filter(Boolean));
    if (fk) {
      const again = Array.from(container.querySelectorAll('[data-fk]')).find((n) => n.dataset.fk === fk);
      if (again) again.focus({ preventScroll: true });
    }
  }
  // 中身が変わったときだけ描き直す
  function changed(name, value) {
    const key = JSON.stringify(value) + '#' + ui.rev;
    if (ui.drawn[name] === key) return false;
    ui.drawn[name] = key;
    return true;
  }

  // 画面の読み上げ（aria-live）。同じ種類の知らせは、言葉が変わったときだけ読む。
  // 続けて読ませたときは、最後の言葉だけを欄に書く（ui.liveText）
  function say(text, channel = 'main') {
    if (!text || ui.spoken[channel] === text) return;
    ui.spoken[channel] = text;
    ui.liveText = text;
    const live = $('live');
    live.textContent = '';
    window.setTimeout(() => {
      if (ui.liveText === text) live.textContent = text;
    }, 50);
  }

  // 終わった知らせの言葉を、読み上げの欄から消す（ほかの言葉に変わっていれば、そのままにする）
  function unsay(channel) {
    const text = ui.spoken[channel];
    ui.spoken[channel] = ''; // 次に同じ知らせが出たときも、読み上げる
    if (!text || ui.liveText !== text) return;
    ui.liveText = '';
    $('live').textContent = '';
  }

  // ---------- サーバーとのやり取り ----------

  async function api(method, path, body) {
    const url = K ? `${path}${path.includes('?') ? '&' : '?'}k=${encodeURIComponent(K)}` : path;
    const init = { method, cache: 'no-store', headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, init);
    } catch (_) {
      const err = new Error(MSG.offline);
      err.status = 0;
      throw err;
    }
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      let text = data && typeof data.error === 'string' && data.error ? data.error : '';
      if (!text) text = res.status === 401 || res.status === 403 ? MSG.badKey : `うまくいきませんでした（サーバーの応答 ${res.status}）。もう一度試してください。`;
      const err = new Error(text);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function poll() {
    if (ui.polling) return;
    ui.polling = true;
    window.clearTimeout(ui.timer);
    try {
      const snap = await api('GET', '/api/state');
      ui.failures = 0;
      showConn('');
      apply(snap || {});
    } catch (err) {
      ui.failures += 1;
      // 1 回だけの失敗は、すぐには知らせない
      if (ui.failures >= 2 || err.status) showConn(err.message);
    } finally {
      ui.polling = false;
      ui.timer = window.setTimeout(poll, document.hidden ? POLL_HIDDEN_MS : POLL_MS);
    }
  }

  function showConn(text) {
    const box = $('conn');
    box.hidden = !text;
    box.textContent = text;
  }

  // ---------- 全体 ----------

  function apply(snap) {
    const prev = ui.snap;
    ui.snap = snap;
    pickTab(prev, snap);
    drawHeader();
    drawWaiting();
    drawSettings();
    drawRun();
    drawResult();
    $('foot-text').textContent = `表示を更新した時刻 ${hms(new Date())}　・　この画面は、この PC の中だけで開けます。`;
    document.body.removeAttribute('aria-busy');
  }

  function redraw() {
    ui.rev += 1;
    if (ui.snap) apply(ui.snap);
  }

  // ---------- タブ ----------

  const TABS = ['settings', 'run', 'result'];

  function setTab(name, focus) {
    if (!TABS.includes(name)) name = 'settings';
    ui.tab = name;
    for (const t of TABS) {
      const tab = $(`tab-${t}`);
      const on = t === name;
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      $(`panel-${t}`).hidden = !on;
    }
    if (focus) $(`tab-${name}`).focus();
    try {
      history.replaceState(null, '', `${location.pathname}${location.search}#${name}`);
    } catch (_) {
      /* 履歴を書き換えられなくても困らない */
    }
  }

  function pickTab(prev, snap) {
    const now = stagePhase(snap);
    if (!prev) {
      const fromHash = location.hash.replace('#', '');
      setTab(TABS.includes(fromHash) ? fromHash : { running: 'run', draft: 'settings', finished: 'result', empty: 'settings' }[now], false);
      return;
    }
    const before = stagePhase(prev);
    if (now === before) return;
    if (now === 'running') setTab('run', false);
    else if (before === 'running') { ui.pastRun = null; setTab('result', false); }
    else if (now === 'draft') setTab('settings', false);
  }

  function initTabs() {
    TABS.forEach((t, i) => {
      const tab = $(`tab-${t}`);
      tab.addEventListener('click', () => setTab(t, false));
      tab.addEventListener('keydown', (e) => {
        let j = -1;
        if (e.key === 'ArrowRight') j = (i + 1) % TABS.length;
        else if (e.key === 'ArrowLeft') j = (i + TABS.length - 1) % TABS.length;
        else if (e.key === 'Home') j = 0;
        else if (e.key === 'End') j = TABS.length - 1;
        if (j >= 0) {
          e.preventDefault();
          setTab(TABS[j], true);
        }
      });
    });
  }

  // ---------- 上の帯と、待っているときの知らせ ----------

  function iterNo(run) {
    const its = arr(run.iterations);
    return Number(run.iteration) || (its.length ? its[its.length - 1].n : 0);
  }

  function drawHeader() {
    const snap = ui.snap;
    const run = snap.run;
    const phase = stagePhase(snap);
    const pill = $('pill');
    let text = '';
    let cls = 'pill';
    if (phase === 'running' && run.status === 'stopping') {
      text = '止めています';
      cls += ' pill-warn';
    } else if (phase === 'running') {
      const n = iterNo(run);
      text = n ? `実行中・試行 ${n} / ${run.maxIterations}` : '実行中・試行の前';
      cls += ' pill-run';
    } else if (phase === 'draft') {
      text = '開始を待っています';
      cls += ' pill-wait';
    } else if (phase === 'finished') {
      const o = OUTCOME[run.finish && run.finish.outcome];
      text = o ? `終わり：${o.label}` : '終わり';
      cls += ` pill-${o ? o.cls : 'unk'}`;
    } else {
      text = 'まだ頼まれていません';
    }
    pill.className = cls;
    pill.textContent = text;
    $('run-dot').hidden = phase !== 'running';

    const waiting = waitingOf(snap);
    document.title = `${waiting ? '【チャットで答えを待っています】' : phase === 'running' ? '【実行中】' : ''}Web管理画面`;
  }

  function drawWaiting() {
    const w = waitingOf(ui.snap);
    const box = $('waiting');
    if (!changed('waiting', w)) return;
    if (!w) {
      box.hidden = true;
      box.replaceChildren();
      unsay('waiting'); // 「チャットで質問に答えてください」を、読み上げの欄に残さない
      return;
    }
    const permission = w.kind === 'permission';
    const title = permission ? 'チャットで許可を求めています' : 'チャットで質問に答えてください';
    const how = permission
      ? 'チャットの画面で、許可するかどうかを選んでください。選ぶまで、エージェントは先に進みません。'
      : 'チャットの画面で答えてください。答えるまで、エージェントは先に進みません。';
    fill(box, [
      h('span', { class: 'banner-icon', 'aria-hidden': 'true' }, '！'),
      h('div', { class: 'banner-body' },
        h('p', { class: 'banner-title' }, title),
        w.text ? h('pre', { class: 'banner-text' }, permission ? `許可を求めていること：${normalize(w.text)}` : normalize(w.text)) : null,
        h('p', { class: 'banner-how' }, how)),
    ]);
    box.hidden = false;
    say(title, 'waiting');
  }

  // ---------- 設定 ----------

  function formSource(snap) {
    const s = snap.settings || {};
    const run = snap.run;
    const d = snap.draft;
    const models = {
      workerModel: str(s.workerModel),
      workerEffort: str(s.workerEffort),
      verifierModel: str(s.verifierModel),
      verifierEffort: str(s.verifierEffort),
    };
    if (isActive(run)) {
      // 実行中は、その実行で使っている値を見せる
      const rm = run.models && typeof run.models === 'object' ? run.models : null;
      if (rm) for (const key of Object.keys(models)) models[key] = str(rm[key]);
      return {
        request: str(run.request), criteria: arr(run.criteria).map(str), worker: str(run.worker),
        verifier: str(run.verifier || s.verifier), maxIterations: run.maxIterations, ...models,
      };
    }
    return {
      request: d ? str(d.request) : '', criteria: d ? arr(d.criteria).map(str) : [], worker: str(s.worker),
      verifier: str(s.verifier), maxIterations: s.maxIterations, ...models,
    };
  }

  function setOptions(select, items, value, placeholder) {
    // items: [{value, label}]。今の値が一覧になければ、足して見せる。
    const list = items.slice();
    if (value && !list.some((o) => o.value === value)) list.push({ value, label: `${value}（一覧にありません）` });
    if (!value && placeholder) list.unshift({ value: '', label: placeholder });
    select.replaceChildren(...list.map((o) => h('option', { value: o.value }, o.label)));
    select.value = value;
  }

  function modelControl(role, models, value) {
    const slot = $(`slot-${role}-model`);
    const id = `f-${role}-model`;
    const want = models.length ? 'SELECT' : 'INPUT';
    let ctl = $(id);
    if (!ctl || ctl.tagName !== want) {
      ctl = want === 'SELECT'
        ? h('select', { id })
        : h('input', { id, type: 'text', placeholder: DEFAULT_LABEL, autocomplete: 'off', spellcheck: 'false' });
      ctl.addEventListener('input', markDirty);
      ctl.addEventListener('change', markDirty);
      const helpNode = want === 'INPUT'
        ? h('p', { class: 'help' }, 'モデルの一覧を読めませんでした。使うモデルの名前を書いてください。空のままなら既定です。')
        : null;
      slot.replaceChildren(ctl, ...(helpNode ? [helpNode] : []));
    }
    if (want === 'SELECT') {
      setOptions(ctl, [{ value: '', label: DEFAULT_LABEL }, ...models.map((m) => ({ value: str(m.id), label: str(m.name || m.id) }))], value);
    } else {
      ctl.value = value;
    }
  }

  function effortItems(efforts) {
    return [{ value: '', label: DEFAULT_LABEL }, ...efforts.map((e) => ({ value: str(e), label: EFFORT[e] || str(e) }))];
  }

  const optionsKeyOf = (opts) => JSON.stringify([opts.agents, opts.models, opts.efforts]);

  // エージェントの選択肢。もう一方の欄で選んでいるエージェントは出さない（own は、この欄の今の値）
  function agentItems(opts, other, own) {
    return arr(opts.agents)
      .map((a) => str(a.name))
      .filter((name) => name && (!other || !sameAgent(name, other) || (own && sameAgent(name, own))))
      .map((name) => ({ value: name, label: name }));
  }

  // 作業エージェントの欄には、選んでいる検証エージェントを出さない。検証エージェントの欄も同じ
  function writeAgentChoices(worker, verifier, opts) {
    setOptions($('f-worker'), agentItems(opts, verifier, worker), worker, '（選んでください）');
    setOptions($('f-verifier'), agentItems(opts, worker, verifier), verifier, '（選んでください）');
  }

  // 選ぶ欄に、選択肢と値を書く（今の値が一覧になくても残す）
  function writeChoices(v, opts) {
    writeAgentChoices(v.worker, v.verifier, opts);
    modelControl('worker', arr(opts.models), v.workerModel);
    modelControl('verifier', arr(opts.models), v.verifierModel);
    const efforts = effortItems(arr(opts.efforts));
    setOptions($('f-worker-effort'), efforts, v.workerEffort);
    setOptions($('f-verifier-effort'), efforts, v.verifierEffort);
  }

  // サーバーから来た値（または実行中の値）を、欄に書く
  function writeForm(src, opts) {
    $('f-request').value = src.request;
    $('f-criteria').value = src.criteria.join('\n');
    $('f-max').value = src.maxIterations == null ? '' : String(src.maxIterations);
    writeChoices(src, opts);
    autoGrow($('f-request'));
    autoGrow($('f-criteria'));
  }

  function readForm() {
    const val = (id) => ($(id) ? $(id).value : '');
    return {
      request: val('f-request').trim(),
      criteria: val('f-criteria').split('\n').map((x) => x.trim()).filter(Boolean),
      worker: val('f-worker').trim(),
      verifier: val('f-verifier').trim(),
      maxIterations: val('f-max') === '' ? null : Number(val('f-max')),
      workerModel: val('f-worker-model').trim(),
      workerEffort: val('f-worker-effort'),
      verifierModel: val('f-verifier-model').trim(),
      verifierEffort: val('f-verifier-effort'),
    };
  }

  function autoGrow(area) {
    area.style.height = 'auto';
    area.style.height = `${Math.min(area.scrollHeight + 2, 480)}px`;
  }

  function markDirty() {
    if (isActive(ui.snap && ui.snap.run)) return;
    ui.dirty = true;
    clearErrors();
  }

  function lockForm(locked) {
    const form = $('settings-form');
    form.classList.toggle('is-locked', locked);
    for (const id of ['f-request', 'f-criteria', 'f-max']) $(id).readOnly = locked;
    for (const ctl of form.querySelectorAll('select, #slot-worker-model input, #slot-verifier-model input')) {
      if (ctl.tagName === 'SELECT') ctl.disabled = locked;
      else ctl.readOnly = locked;
    }
    $('form-actions').hidden = locked;
  }

  function drawWorkerDesc() {
    const opts = (ui.snap && ui.snap.options) || {};
    const name = $('f-worker').value;
    const agents = arr(opts.agents);
    const found = agents.find((a) => str(a.name) === name);
    let text = '';
    if (found) text = str(found.description);
    else if (name && agents.length) text = 'このエージェントは、いま使えるエージェントの一覧にありません。入っているかを確かめるか、ほかを選んでください。';
    $('worker-desc').textContent = text;
    $('worker-desc').hidden = !text;
  }

  function drawSettings() {
    const snap = ui.snap;
    const opts = snap.options || {};
    const run = snap.run;
    const locked = isActive(run);
    const src = formSource(snap);
    const key = JSON.stringify(src);
    const optKey = optionsKeyOf(opts);

    if (locked || !ui.dirty) {
      if (key !== ui.serverForm || optKey !== ui.optionsKey) writeForm(src, opts);
      ui.dirty = false;
      ui.serverForm = key;
    } else {
      // 書き換え中は、選択肢の一覧だけを新しくする（書きかけの値は残す）
      if (optKey !== ui.optionsKey) writeChoices(readForm(), opts);
      if (key !== ui.serverForm) {
        ui.serverForm = key;
        showFormMsg('チャットの側で設定が変わりました。書きかけの内容は残しています。新しい内容を見るには、ページを読み込み直してください。', 'info');
      }
    }
    ui.optionsKey = optKey;
    lockForm(locked);
    drawWorkerDesc();
    drawNote(locked);

    // 設定は作業フォルダごとに保存する
    const folder = $('settings-folder');
    folder.textContent = snap.folder ? `設定は、作業フォルダごとに保存します。使うエージェントは保存せず、毎回、既定から始まります（いまの作業フォルダ：${str(snap.folder)}）。` : '';
    folder.hidden = !snap.folder;

    drawPrepared(locked);
  }

  // 選んでいる作業エージェントの準備が「できていない」と記録されているか（実行中・別のエージェントを選んだときは見ない）
  function notReady(locked) {
    const prep = !locked && ui.snap.draft && ui.snap.draft.prepared;
    if (!prep || prep.ready === true) return false;
    const selected = $('f-worker').value;
    return !(selected && str(prep.worker) && !sameAgent(selected, prep.worker));
  }

  // 上の案内と、開始のボタンの横の注意
  function drawNote(locked) {
    const snap = ui.snap;
    const warn = notReady(locked);
    const note = $('settings-note');
    note.className = 'note';
    if (locked) {
      note.textContent = '実行中は、設定を変えられません。いま使っている設定を表示しています。';
      note.classList.add('note-lock');
    } else if (warn) {
      note.textContent = MSG.notReady;
      note.classList.add('note-warn');
    } else if (snap.draft) {
      note.textContent = '内容を確かめて「開始」を押してください。開始すると、チャットに知らせが届き、統括エージェントが作業を始めます。';
    } else {
      note.textContent = 'チャットで統括エージェントに作業を頼むと、頼む作業と合格の条件がここに入ります。ここに直接書いてもかまいません。';
    }
    // 開始のボタンは押せるままにし、横に短い注意を出す
    const side = $('start-note');
    side.textContent = warn ? MSG.notReadyShort : '';
    side.hidden = !warn;
    if (warn) $('btn-start').setAttribute('aria-describedby', 'start-note');
    else $('btn-start').removeAttribute('aria-describedby');
  }

  function drawPrepared(locked) {
    const snap = ui.snap;
    // 実行中は、その実行が引き継いだ準備の返事だけを見せる（なければ、確かめていない）
    const prep = (locked ? snap.run.prepared : snap.draft && snap.draft.prepared) || null;
    const card = $('prep');
    card.hidden = false;
    const badge = $('prep-badge');
    const meta = $('prep-meta');
    const other = $('prep-other');
    const report = $('prep-report');
    if (!prep) {
      badge.className = 'badge badge-unk';
      badge.textContent = 'まだ確かめていない';
      meta.textContent = '';
      meta.hidden = true;
      other.hidden = true;
      report.textContent = locked
        ? '開始の前に、準備の確認はありませんでした。統括エージェントが、最初の試行の前に確かめます。'
        : '統括エージェントが作業エージェントに準備を確かめると、その返事がここにそのまま出ます。';
      report.classList.add('is-empty');
      return;
    }
    const ready = prep.ready === true;
    // 準備を確かめたのとは別の作業エージェントを選んだときは、その返事で「できている・できていない」と言わない
    const selected = locked ? str(snap.run.worker) : $('f-worker').value;
    const changedWorker = !!(selected && str(prep.worker) && !sameAgent(selected, prep.worker));
    if (changedWorker) {
      badge.className = 'badge badge-unk';
      badge.textContent = 'まだ確かめていない（開始すると確かめます）';
    } else {
      badge.className = `badge ${ready ? 'badge-ok' : 'badge-ng'}`;
      badge.textContent = ready ? 'できている' : 'できていない';
    }
    meta.textContent = `確かめた作業エージェント：${str(prep.worker)}${prep.at ? `（${hm(prep.at)}）` : ''}`;
    meta.hidden = false;
    other.hidden = !changedWorker;
    other.textContent = 'いま選んでいる作業エージェントとは違うエージェントの返事です。開始の後に、統括エージェントが確かめ直します。';
    report.textContent = normalize(prep.report) || '（返事の中身がありません）';
    report.classList.remove('is-empty');
    say(changedWorker ? '選んだ作業エージェントの準備は、まだ確かめていません。'
      : ready ? '作業エージェントの準備ができています。' : '作業エージェントの準備ができていません。', 'prepared');
  }

  // 誤りの説明の欄 → 入力の欄
  const ERR_FIELDS = { request: 'f-request', criteria: 'f-criteria', max: 'f-max', worker: 'f-worker', verifier: 'f-verifier' };

  function clearErrors() {
    for (const [id, input] of Object.entries(ERR_FIELDS)) {
      const e = $(`err-${id}`);
      e.hidden = true;
      e.textContent = '';
      $(input).removeAttribute('aria-invalid');
    }
  }

  function validate(v) {
    const problems = [];
    if (!v.request) problems.push(['request', '頼む作業を書いてください。']);
    if (!v.criteria.length) problems.push(['criteria', '合格の条件を 1 つ以上書いてください。']);
    if (!Number.isInteger(v.maxIterations) || v.maxIterations < 1 || v.maxIterations > 20) {
      problems.push(['max', '繰り返しの上限は、1 から 20 までの整数にしてください。']);
    }
    if (!v.worker) problems.push(['worker', '作業エージェントを選んでください。']);
    if (!v.verifier) problems.push(['verifier', '検証エージェントを選んでください。']);
    clearErrors();
    for (const [id, text] of problems) {
      const e = $(`err-${id}`);
      e.textContent = text;
      e.hidden = false;
      $(ERR_FIELDS[id]).setAttribute('aria-invalid', 'true');
    }
    if (problems.length) $(ERR_FIELDS[problems[0][0]]).focus();
    return problems.length === 0;
  }

  function showFormMsg(text, kind) {
    const m = $('form-msg');
    m.className = `form-msg${kind ? ` form-msg-${kind}` : ''}`;
    m.textContent = text;
  }

  // 保存・開始を送る。うまくいけばサーバーの返事を、だめなら null を返す
  async function send(path, okText) {
    if (ui.busy) return null;
    const v = readForm();
    if (path === '/api/start' && !validate(v)) {
      showFormMsg('足りない項目があります。項目の下の赤い字の説明を見てください。', 'error');
      return null;
    }
    ui.busy = true;
    $('btn-save').disabled = true;
    $('btn-start').disabled = true;
    showFormMsg(path === '/api/start' ? '開始しています…' : '保存しています…', '');
    try {
      const res = await api('POST', path, v);
      ui.dirty = false;
      ui.serverForm = '';
      showFormMsg(okText, 'ok');
      return res || {};
    } catch (err) {
      showFormMsg(err.message, 'error');
      return null;
    } finally {
      ui.busy = false;
      $('btn-save').disabled = false;
      $('btn-start').disabled = false;
    }
  }

  function initForm() {
    const form = $('settings-form');
    for (const id of ['f-request', 'f-criteria', 'f-max', 'f-worker', 'f-verifier', 'f-worker-effort', 'f-verifier-effort']) {
      $(id).addEventListener('input', markDirty);
      $(id).addEventListener('change', markDirty);
    }
    $('f-request').addEventListener('input', () => autoGrow($('f-request')));
    $('f-criteria').addEventListener('input', () => autoGrow($('f-criteria')));
    // 片方を選び直したら、もう一方の欄の選択肢を作り直す
    const rewriteAgents = () => writeAgentChoices($('f-worker').value, $('f-verifier').value, (ui.snap && ui.snap.options) || {});
    $('f-worker').addEventListener('change', () => {
      rewriteAgents();
      drawWorkerDesc();
      drawNote(false);
      drawPrepared(false);
    });
    $('f-verifier').addEventListener('change', rewriteAgents);
    $('btn-save').addEventListener('click', async () => {
      const res = await send('/api/settings', '保存しました。');
      if (!res) return;
      // ファイルに保存できなかったときなどの注意は、そのまま見せる
      if (res.warning) showFormMsg(str(res.warning), 'info');
      poll();
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const res = await send('/api/start', '開始しました。チャットに知らせを送りました。');
      if (res) {
        // 開始はできたが気をつけること（モデルを変えられなかったなど）は、実行のようすの画面に出す
        ui.notice = res.warning ? { runId: res.runId, text: str(res.warning) } : null;
        if (ui.notice) say(ui.notice.text, 'notice');
        ui.pastRun = null;
        setTab('run', false);
        poll();
      }
    });
  }

  // ---------- 実行のようす ----------

  // 今の試行が、計画・作業・評価のどこにいるか
  function cycleOf(run) {
    const its = arr(run.iterations);
    const last = its[its.length - 1];
    const max = Number(run.maxIterations) || 0;
    const role = run.active && run.active.role;
    if (run.status === 'finished') {
      // 終わった後は、最後の試行に記録がある段だけを済みにする。止めたときは、記録のない最初の段が止めた所
      const has = [!!(last && last.plan), !!(last && last.do), !!(last && last.study)];
      const outcome = run.finish && run.finish.outcome;
      const cut = outcome === 'stopped' || outcome === 'needs-human' ? has.indexOf(false) : -1;
      return { step: 'done', n: last ? last.n : 0, has, cut };
    }
    // 試行の前に作業エージェントが動いているのは、準備の確認
    if (!last) return { step: role === 'worker' ? 'prep' : 'plan', n: 1 };
    if (role === 'worker') return { step: 'do', n: last.n };
    if (role === 'verifier') return { step: 'study', n: last.n };
    if (!last.do) return { step: 'do', n: last.n };
    if (!last.study) return { step: 'study', n: last.n };
    const allPass = arr(last.study.verdicts).length > 0 && arr(last.study.verdicts).every((v) => v.result === 'pass');
    if (allPass || (max && last.n >= max)) return { step: 'wrap', n: last.n };
    return { step: 'plan', n: last.n + 1 };
  }

  function nowSentence(run) {
    if (run.status === 'finished') {
      const stopped = run.finish && run.finish.outcome === 'stopped';
      return { text: `${stopped ? '途中で止めました' : '終わりました'}。結論は「結果」の画面にあります。`, time: '' };
    }
    if (run.status === 'stopping') return { text: '止めています。統括エージェントが終わりの記録をするのを待っています。', time: '' };
    const c = cycleOf(run);
    const a = run.active;
    const time = a && a.since ? since(a.since) : '';
    const w = waitingOf(ui.snap);
    if (w) {
      return { text: w.kind === 'permission' ? 'チャットで、許可を待っています。' : 'チャットで、あなたの答えを待っています。', time: '' };
    }
    if (!a && !arr(run.iterations).length) return { text: '統括エージェントが動き出すのを待っています', time: '' };
    if (!a) return { text: 'いまは、どのエージェントも動いていません。チャットのようすを確かめてください。', time: '' };
    if (c.step === 'prep') return { text: '作業エージェントが、準備ができているかを確かめています', time };
    if (a.role === 'worker') return { text: `作業エージェントが試行 ${c.n} の作業をしています`, time };
    if (a.role === 'verifier') return { text: `検証エージェントが試行 ${c.n} の結果を確かめています`, time };
    if (a.role === 'orchestrator') {
      if (c.step === 'plan') return { text: `統括エージェントが試行 ${c.n} の計画を立てています`, time };
      if (c.step === 'do') return { text: `統括エージェントが、作業エージェントに試行 ${c.n} を頼んでいます`, time };
      if (c.step === 'study') return { text: `統括エージェントが試行 ${c.n} の結果を評価しています`, time };
      return { text: '統括エージェントが結論をまとめています', time };
    }
    return { text: 'ほかのエージェントが動いています', time };
  }

  // waiting：チャットで利用者の答えを待っている。このときは、最後に動いていたエージェントに「動いています」と出さない
  function orgChart(workerName, verifierName, activeRole, waiting) {
    const mark = (role) => {
      if (activeRole !== role) return null;
      if (waiting) return h('p', { class: 'org-wait' }, h('span', { class: 'wait-mark', 'aria-hidden': 'true' }, '！'), 'チャットで答えを待っています');
      return h('p', { class: 'org-live' }, h('span', { class: 'live-dot', 'aria-hidden': 'true' }), '動いています');
    };
    const state = (role) => (activeRole !== role ? '' : waiting ? ' is-waiting' : ' is-active');
    const node = (role, head, name, job) => h('div', { class: `org-node org-${role}${state(role)}` },
      h('p', { class: 'org-title' }, roleWords(head)),
      name ? h('p', { class: 'org-name' }, name) : null,
      h('p', { class: 'org-job' }, job),
      mark(role));
    return h('div', { class: 'org', role: 'group', 'aria-label': 'エージェントの構成' },
      h('div', { class: 'org-top' }, node('o', '統括', '', '計画を立て、評価を記録する')),
      h('p', { class: 'org-link' }, h('span', null, '頼む・報告を受ける')),
      h('div', { class: 'org-children' },
        node('w', '作業', workerName, '計画どおりに作業し、報告する'),
        node('v', '検証', verifierName, '合格の条件を確かめる')));
  }

  function cycleSteps(run) {
    const c = run ? cycleOf(run) : { step: 'none', n: 0 };
    const order = ['plan', 'do', 'study'];
    const at = order.indexOf(c.step);
    // 答えを待っているあいだは、どの段も「いま」にしない（済んだ段だけを済みにする）
    const wait = waitsOnUser(run);
    const stateOf = (i) => {
      if (c.step === 'done') return c.has[i] ? 'done' : i === c.cut ? 'cut' : 'todo';
      if (c.step === 'wrap') return 'done';
      if (at < 0) return 'todo';
      if (wait) return i < at ? 'done' : 'todo';
      return i < at ? 'done' : i === at ? 'now' : 'todo';
    };
    const steps = [
      ['plan', '計画', '統括', 'o'],
      ['do', '作業', '作業', 'w'],
      ['study', '評価', '検証', 'v'],
    ];
    const max = run ? run.maxIterations : (ui.snap.settings || {}).maxIterations;
    return h('div', { class: 'cycle-wrap' },
      wait ? h('p', { class: 'cycle-wait' },
        h('span', { class: 'wait-mark', 'aria-hidden': 'true' }, '！'), 'あなたの答えを待っています') : null,
      h('ol', { class: 'cycle', 'aria-label': '1 回の試行の流れ' },
        steps.map(([, name, who, cls], i) => {
          const st = stateOf(i);
          return h('li', { class: `step step-${cls} is-${st}`, 'aria-current': st === 'now' ? 'step' : null },
            h('span', { class: 'step-no', 'aria-hidden': 'true' }, st === 'done' ? '✓' : st === 'cut' ? '■' : String(i + 1)),
            h('span', { class: 'step-name' }, name),
            h('span', { class: 'step-who' }, roleWords(who)),
            st === 'cut' ? h('span', { class: 'step-cut' }, '途中で止めた') : null,
            st === 'now' ? h('span', { class: 'sr-only' }, '（いまここ）') : null);
        })),
      h('p', { class: 'cycle-loop' },
        h('span', { class: 'loop-mark', 'aria-hidden': 'true' }, '↻'),
        `合格しなければ、計画にもどって次の試行へ（上限 ${max || '？'} 回）`));
  }

  function drawRunNow() {
    const snap = ui.snap;
    const run = snap.run;
    const s = snap.settings || {};
    const box = $('run-now');

    if (!run) {
      if (!changed('run-now', ['none', s.worker, s.verifier, s.maxIterations])) return;
      fill(box, [h('section', { class: 'card' },
        h('h2', null, 'まだ実行していません'),
        h('p', { class: 'help' }, '「設定」の画面で「開始」を押すと、ここに進み方が出ます。'),
        orgChart(str(s.worker), str(s.verifier), null),
        cycleSteps(null))]);
      return;
    }

    const sentence = nowSentence(run);
    const c = cycleOf(run);
    const role = run.status === 'finished' ? null : run.active ? { orchestrator: 'o', worker: 'w', verifier: 'v' }[run.active.role] || null : null;
    const wait = waitsOnUser(run);
    const n = iterNo(run);
    const notice = ui.notice && ui.notice.runId === run.id ? ui.notice.text : '';
    let count = `試行 ${n} / ${run.maxIterations}`;
    if (run.status === 'finished') count = `試行 ${n} 回で終了`;
    else if (!n) count = `試行の前（上限 ${run.maxIterations} 回）`;

    if (changed('run-now', [run.id, run.status, sentence.text, role, wait, c, run.worker, run.verifier, run.request, count, notice])) {
      const stop = run.status === 'running'
        ? h('button', { type: 'button', class: 'btn btn-danger', dataset: { fk: 'stop' }, onclick: onStop }, '止める')
        : null;
      fill(box, [h('section', { class: 'card now-card', 'aria-labelledby': 'h-now' },
        h('div', { class: 'card-head' },
          h('h2', { id: 'h-now' }, 'いまのようす'),
          h('span', { class: 'iter-count' }, count)),
        notice ? h('p', { class: 'warn-line', role: 'note' }, notice) : null,
        h('p', { class: 'now-line' },
          h('span', { class: 'now-label' }, 'いま：'),
          h('span', { id: 'now-text' }, sentence.text),
          h('span', { id: 'now-time', class: 'now-time' }, sentence.time ? `（${sentence.time}）` : '')),
        h('p', { class: 'now-meta' },
          `実行の番号 ${str(run.id)}`, run.startedAt ? `　・　開始 ${hm(run.startedAt)}` : ''),
        run.request ? h('p', { class: 'now-request' }, h('span', { class: 'k' }, '頼んだ作業：'), firstLine(run.request, 140)) : null,
        h('div', { class: 'now-grid' },
          orgChart(str(run.worker), str(run.verifier), role, wait),
          cycleSteps(run)),
        stop ? h('div', { class: 'now-actions' }, stop,
          h('p', { class: 'help' }, '止めると、動いているエージェントがその場で止まり、この実行は終わります。')) : null)]);
    } else {
      // 経った時間だけを書き換える
      const t = $('now-time');
      if (t) t.textContent = sentence.time ? `（${sentence.time}）` : '';
    }
    say(`いま：${sentence.text}`, 'now');
  }

  async function onStop() {
    if (!window.confirm('止めますか？\n動いているエージェントは途中で止まり、この実行は終わります。')) return;
    try {
      await api('POST', '/api/stop', {});
      say('止めるように伝えました。');
    } catch (err) {
      window.alert(err.message);
    }
    poll();
  }

  function verdictChip(result) {
    const v = VERDICT[result] || VERDICT.unknown;
    return h('span', { class: `chip chip-${v.cls}` }, h('span', { 'aria-hidden': 'true' }, v.mark), v.label);
  }

  function longText(text, key, max = PREVIEW_LINES) {
    const body = normalize(text);
    const lines = body.split('\n');
    const pre = h('pre', { class: 'report' });
    if (lines.length <= max) {
      pre.textContent = body;
      return pre;
    }
    const full = !!ui.expanded[key];
    pre.textContent = full ? body : `${lines.slice(0, max).join('\n')}\n…`;
    const btn = h('button', {
      type: 'button',
      class: 'btn-link',
      'aria-expanded': String(full),
      dataset: { fk: `exp:${key}` },
      onclick: () => {
        ui.expanded[key] = !ui.expanded[key];
        redraw();
      },
    }, full ? '短くする' : `全部を見る（${lines.length} 行）`);
    return h('div', { class: 'long' }, pre, btn);
  }

  function counts(verdicts) {
    const c = { pass: 0, fail: 0, unknown: 0 };
    for (const v of arr(verdicts)) c[VERDICT[v.result] ? v.result : 'unknown'] += 1;
    return c;
  }

  function iterState(run, it) {
    if (it.study) {
      const c = counts(it.study.verdicts);
      if (c.fail) return { label: `不合格 ${c.fail}`, cls: 'ng' };
      if (c.unknown) return { label: `判断できない ${c.unknown}`, cls: 'unk' };
      return { label: 'すべて合格', cls: 'ok' };
    }
    if (run.status === 'finished') {
      const outcome = run.finish && run.finish.outcome;
      return outcome === 'stopped' ? { label: '途中で止めた', cls: 'warn' } : { label: '途中で終了', cls: 'unk' };
    }
    if (run.status === 'stopping') return { label: '止めています', cls: 'warn' };
    // 評価せずに次の試行へ進んだ、前の試行
    if (arr(run.iterations).slice(-1)[0] !== it) return { label: '評価なし', cls: 'unk' };
    if (waitsOnUser(run)) return { label: '答えを待っています', cls: 'wait' };
    if (it.do) return { label: '評価しています', cls: 'run' };
    return { label: '作業しています', cls: 'run' };
  }

  function iterationCard(run, it, openByDefault, prefix) {
    const key = `${prefix}:${run.id}:${it.n}`;
    const st = iterState(run, it);
    const open = key in ui.open ? ui.open[key] : openByDefault;
    // 答えを待っているあいだは、「…しています」と言わない
    const role = waitsOnUser(run) ? null : run.active && run.active.role;
    const isLast = arr(run.iterations).slice(-1)[0] === it;
    const yet = run.status === 'finished' ? '' : 'まだ'; // 終わった後は「まだ」と言わない

    const planCol = h('section', { class: 'pds-col pds-o' },
      h('h4', null, h('span', { class: 'pds-no', 'aria-hidden': 'true' }, '1'), '計画',
        h('span', { class: 'pds-who' }, `統括エージェント${it.plan && it.plan.at ? `・${hm(it.plan.at)}` : ''}`)),
      it.plan && it.plan.text ? longText(it.plan.text, `${key}:plan`) : h('p', { class: 'muted' }, `${yet}計画はありません。`));

    let doBody;
    if (it.do) doBody = longText(it.do.report, `${key}:do`);
    else if (isLast && run.status === 'running' && role === 'worker') doBody = h('p', { class: 'muted' }, '作業エージェントが作業をしています…');
    else doBody = h('p', { class: 'muted' }, `${yet}報告はありません。`);
    const doCol = h('section', { class: 'pds-col pds-w' },
      h('h4', null, h('span', { class: 'pds-no', 'aria-hidden': 'true' }, '2'), '作業の報告',
        h('span', { class: 'pds-who' }, `作業エージェント${it.do && it.do.at ? `・${hm(it.do.at)}` : ''}`)),
      doBody);

    let studyBody;
    if (it.study) {
      const st2 = it.study;
      const vkey = `${key}:vrep`;
      const vopen = !!ui.open[vkey];
      studyBody = [
        h('ul', { class: 'verdicts' }, arr(st2.verdicts).map((v) => h('li', null,
          h('div', { class: 'verdict-head' }, verdictChip(v.result), criterionLine(run, v)),
          v.reason ? h('p', { class: 'reason' }, str(v.reason)) : null))),
        st2.next ? h('div', { class: 'next' }, h('p', { class: 'k' }, '次にすること'), h('p', { class: 'pre' }, normalize(st2.next))) : null,
        // 検証エージェントを呼ばずに評価したとき（作業が進めなかったとき）は、空の欄の代わりにそのことを書く
        !st2.verifierReport && st2.verifierCalled === false
          ? h('p', { class: 'muted no-verifier' }, '評価：作業が進めなかったため、検証エージェントは呼んでいません')
          : null,
        st2.verifierReport ? h('details', {
          class: 'sub-details',
          open: vopen,
          ontoggle: (e) => {
            if (e.currentTarget.open !== vopen || vkey in ui.open) ui.open[vkey] = e.currentTarget.open;
          },
        },
        h('summary', { dataset: { fk: `sum:${vkey}` } }, '検証エージェントの返事'),
        longText(st2.verifierReport, `${key}:vr`, 30)) : null,
      ];
    } else if (isLast && run.status === 'running' && role === 'verifier') {
      studyBody = h('p', { class: 'muted' }, '検証エージェントが確かめています…');
    } else {
      studyBody = h('p', { class: 'muted' }, `${yet}評価はありません。`);
    }
    const noVerifier = !!(it.study && !it.study.verifierReport && it.study.verifierCalled === false);
    const studyCol = h('section', { class: 'pds-col pds-v' },
      h('h4', null, h('span', { class: 'pds-no', 'aria-hidden': 'true' }, '3'), '評価',
        h('span', { class: 'pds-who' }, `${noVerifier ? '統括エージェント' : '検証エージェント'}${it.study && it.study.at ? `・${hm(it.study.at)}` : ''}`)),
      studyBody);

    return h('details', {
      class: 'iter',
      open,
      // 描いた直後の知らせは無視し、利用者が開け閉めしたときだけ覚える
      ontoggle: (e) => {
        if (e.currentTarget.open !== open || key in ui.open) ui.open[key] = e.currentTarget.open;
      },
    },
    h('summary', { dataset: { fk: `sum:${key}` } },
      h('span', { class: 'iter-title' }, `試行 ${it.n}`),
      h('span', { class: `chip chip-${st.cls}` }, st.label),
      it.plan && it.plan.text ? h('span', { class: 'iter-sum' }, firstLine(it.plan.text, 60)) : null),
    h('div', { class: 'pds' }, planCol, doCol, studyCol));
  }

  function drawRunIters() {
    const run = ui.snap.run;
    const box = $('run-iters');
    if (!run) {
      if (changed('run-iters', null)) fill(box, []);
      return;
    }
    if (!changed('run-iters', [run.id, run.status, run.iterations, run.active && run.active.role, waitsOnUser(run)])) return;
    const its = arr(run.iterations).slice().reverse();
    fill(box, [h('section', { class: 'block', 'aria-labelledby': 'h-iters' },
      h('h2', { id: 'h-iters' }, '試行の記録'),
      h('p', { class: 'help' }, '新しい試行が上です。それぞれ、計画 → 作業の報告 → 評価 の順に並びます。'),
      its.length
        ? its.map((it, i) => iterationCard(run, it, i === 0, 'run'))
        : h('p', { class: 'muted card' }, '統括エージェントが、試行 1 の計画を立てるところです。'))]);
  }

  // コマンドの行は、長いときに頭と終わり（動かしたものの名前と引数）を残し、真ん中を省く
  const CMD_LABEL = 'コマンドを実行：';
  const CMD_FAILED = '（失敗）';
  const CMD_HEAD = 70;
  const CMD_TAIL = 70;
  function activityText(text) {
    if (!text.startsWith(CMD_LABEL)) return text;
    const failed = text.endsWith(CMD_FAILED);
    const command = text.slice(CMD_LABEL.length, failed ? text.length - CMD_FAILED.length : text.length);
    if (command.length <= CMD_HEAD + CMD_TAIL + 1) return text;
    return `${CMD_LABEL}${command.slice(0, CMD_HEAD)}…${command.slice(-CMD_TAIL)}${failed ? CMD_FAILED : ''}`;
  }

  function activityList(lines) {
    // 新しい順に並べる（時刻のないものは、届いた順の後ろから）
    const list = arr(lines).map((line, i) => ({ line, i, t: toDate(line.at) }))
      .sort((a, b) => (a.t && b.t ? b.t.getTime() - a.t.getTime() : 0) || b.i - a.i)
      .map((x) => x.line);
    return h('ol', { class: 'activity' }, list.map((line) => {
      const who = str(line.who) || 'その他';
      const full = str(line.text);
      // コマンドの説明の行は、説明をそのまま出し、コマンドの全文を title に出す
      const command = str(line.command);
      const shown = command ? full : activityText(full);
      return h('li', null,
        h('time', { class: 'act-time' }, hms(line.at)),
        h('span', { class: `who who-${WHO_CLASS[who] || 'x'}` }, who),
        // 省いたときは、全文を title に残す
        h('span', { class: 'act-text', title: command || (shown !== full ? full : null) }, shown));
    }));
  }

  function drawRunLog() {
    const run = ui.snap.run;
    const box = $('run-log');
    if (!run) {
      if (changed('run-log', null)) fill(box, []);
      return;
    }
    const open = !!ui.open.activity;
    const lines = arr(run.activity);
    if (!changed('run-log', [run.id, open, open ? lines : lines.length])) return;
    fill(box, [h('details', {
      class: 'card log',
      open,
      ontoggle: (e) => {
        if (ui.open.activity === e.currentTarget.open) return;
        ui.open.activity = e.currentTarget.open;
        drawRunLog();
      },
    },
    h('summary', { dataset: { fk: 'sum:activity' } },
      h('span', { class: 'log-title' }, 'くわしい記録'),
      h('span', { class: 'help' }, `エージェントがしたことの記録（${lines.length} 件・新しい順）`)),
    open ? (lines.length ? activityList(lines) : h('p', { class: 'muted' }, 'まだ記録はありません。')) : null)]);
  }

  function drawRun() {
    drawRunNow();
    drawRunIters();
    drawRunLog();
  }

  // ---------- 結果 ----------

  function outcomeBadge(outcome, big) {
    const o = OUTCOME[outcome];
    if (!o) return h('span', { class: `badge badge-unk${big ? ' badge-big' : ''}` }, 'まだ終わっていません');
    return h('span', { class: `badge badge-${o.cls}${big ? ' badge-big' : ''}` }, h('span', { 'aria-hidden': 'true' }, o.mark), o.label);
  }

  function lastVerdicts(run) {
    const its = arr(run.iterations);
    for (let i = its.length - 1; i >= 0; i -= 1) {
      if (its[i].study && arr(its[i].study.verdicts).length) return { n: its[i].n, verdicts: its[i].study.verdicts };
    }
    return null;
  }

  function summaryTable(run) {
    const its = arr(run.iterations);
    if (!its.length) return h('p', { class: 'muted' }, '試行の記録はありません。');
    return h('table', { class: 'sum-table' },
      h('caption', { class: 'sr-only' }, '試行ごとのまとめ'),
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, '試行'), h('th', { scope: 'col' }, '計画'),
        h('th', { scope: 'col' }, '作業'), h('th', { scope: 'col' }, '評価'))),
      h('tbody', null, its.map((it) => {
        const c = it.study ? counts(it.study.verdicts) : null;
        return h('tr', null,
          h('th', { scope: 'row', 'data-label': '試行' }, `試行 ${it.n}`),
          h('td', { 'data-label': '計画' }, it.plan ? firstLine(it.plan.text, 120) : '—'),
          h('td', { 'data-label': '作業' }, it.do ? gist(it.do.report, 120) : h('span', { class: 'muted' }, '報告なし')),
          h('td', { 'data-label': '評価' }, c
            ? h('div', { class: 'eval-cell' },
              h('div', { class: 'chips' },
                c.pass ? h('span', { class: 'chip chip-ok' }, `合格 ${c.pass}`) : null,
                c.fail ? h('span', { class: 'chip chip-ng' }, `不合格 ${c.fail}`) : null,
                c.unknown ? h('span', { class: 'chip chip-unk' }, `判断できない ${c.unknown}`) : null),
              it.study.next ? h('p', { class: 'next-short' }, `次：${firstLine(it.study.next, 80)}`) : null)
            : h('span', { class: 'muted' }, '評価なし')));
      })));
  }

  // 振り返りがないときの文。止めて終わった実行では「まだ」と言わない
  function noRetroText(run) {
    const outcome = run.status === 'finished' && run.finish ? run.finish.outcome : null;
    if (outcome === 'stopped') return '止めたため、振り返りはありません。';
    if (outcome === 'needs-human') return '途中で終えたため、振り返りはありません。';
    return '振り返りはまだありません。';
  }

  function resultView(run, isPast) {
    const fin = run.finish;
    const done = run.status === 'finished';
    const lv = lastVerdicts(run);
    const n = arr(run.iterations).length;
    const t0 = toDate(run.startedAt);
    const t1 = toDate(run.finishedAt);
    let timeText = t0 ? dateTime(t0) : '';
    if (t0 && t1) timeText += ` 〜 ${hm(t1)}（${span(t1.getTime() - t0.getTime())}）`;
    const allKey = `res:${run.id}:all`;

    return [
      isPast ? h('div', { class: 'past-bar' },
        h('p', null, `前の実行（${str(run.id)}）を見ています。`),
        h('button', { type: 'button', class: 'btn', dataset: { fk: 'back-latest' }, onclick: () => { ui.pastRun = null; redraw(); } }, 'いちばん新しい実行にもどる')) : null,
      !done ? h('p', { class: 'note' }, 'まだ終わっていません。終わると、ここに結論が出ます。いままでの試行のまとめを表示しています。') : null,
      h('section', { class: 'card conclusion', 'aria-labelledby': 'h-conclusion' },
        h('div', { class: 'card-head' },
          h('h2', { id: 'h-conclusion', tabindex: '-1' }, '結論'),
          outcomeBadge(fin && fin.outcome, true)),
        fin && fin.conclusion
          ? h('p', { class: 'conclusion-text pre' }, normalize(fin.conclusion))
          : h('p', { class: 'muted' }, done ? '結論は記録されていません。' : '結論はまだありません。'),
        h('dl', { class: 'facts' },
          h('div', null, h('dt', null, '頼んだ作業'), h('dd', { class: 'pre' }, normalize(run.request) || '—')),
          h('div', null, h('dt', null, '試行の回数'), h('dd', null, `${n} 回（上限 ${run.maxIterations || '？'} 回）`)),
          h('div', null, h('dt', null, '時間'), h('dd', null, timeText || '—')),
          h('div', null, h('dt', null, '作業エージェント'), h('dd', null, str(run.worker) || '—')),
          h('div', null, h('dt', null, '検証エージェント'), h('dd', null, str(run.verifier) || '—')),
          h('div', null, h('dt', null, '実行の番号'), h('dd', null, str(run.id))))),
      h('section', { class: 'card', 'aria-labelledby': 'h-criteria' },
        h('h2', { id: 'h-criteria' }, '合格の条件と、最後の評価'),
        lv ? h('p', { class: 'help' }, `試行 ${lv.n} の評価です。`) : null,
        h('ul', { class: 'verdicts' },
          (lv ? lv.verdicts : arr(run.criteria).map((c, i) => ({ no: i + 1, text: c, result: null }))).map((v) => h('li', null,
            h('div', { class: 'verdict-head' },
              v.result ? verdictChip(v.result) : h('span', { class: 'chip chip-unk' }, '評価なし'),
              criterionLine(run, v)),
            v.reason ? h('p', { class: 'reason' }, str(v.reason)) : null)))),
      h('section', { class: 'card', 'aria-labelledby': 'h-table' },
        h('h2', { id: 'h-table' }, '試行ごとのまとめ'),
        summaryTable(run),
        arr(run.iterations).length ? h('details', {
          class: 'sub-details',
          open: !!ui.open[allKey],
          ontoggle: (e) => {
            if (!!ui.open[allKey] === e.currentTarget.open) return;
            ui.open[allKey] = e.currentTarget.open;
            redraw();
          },
        },
        h('summary', { dataset: { fk: `sum:${allKey}` } }, '試行ごとの、くわしい内容を見る'),
        ui.open[allKey] ? arr(run.iterations).slice().reverse().map((it) => iterationCard(run, it, false, 'res')) : null) : null),
      h('section', { class: 'card', 'aria-labelledby': 'h-retro' },
        h('h2', { id: 'h-retro' }, '振り返り'),
        fin && fin.retro ? h('p', { class: 'pre' }, normalize(fin.retro)) : h('p', { class: 'muted' }, noRetroText(run))),
    ];
  }

  async function openPastRun(id) {
    try {
      const data = await api('GET', `/api/runs/${encodeURIComponent(id)}`);
      const run = data && data.run ? data.run : data;
      if (!run || !run.id) throw new Error('この実行の記録を読めませんでした。');
      const latest = ui.snap && ui.snap.run;
      ui.pastRun = latest && latest.id === run.id ? null : run;
      redraw();
      const head = $('h-conclusion');
      if (head) {
        head.focus({ preventScroll: true });
        $('panel-result').scrollIntoView({ block: 'start' });
      }
      say(`実行 ${run.id} の結果を表示しました。`);
    } catch (err) {
      window.alert(err.message);
    }
  }

  function drawResult() {
    const snap = ui.snap;
    const run = ui.pastRun || snap.run;
    const body = $('result-body');
    if (changed('result-body', [run, !!ui.pastRun])) {
      fill(body, run ? resultView(run, !!ui.pastRun) : [h('section', { class: 'card' },
        h('h2', null, '結果はまだありません'),
        h('p', { class: 'help' }, '実行が終わると、ここに結論・試行ごとのまとめ・振り返りが出ます。'))]);
    }

    const runs = arr(snap.runs); // サーバーが新しい順に並べて渡す
    const showing = run ? run.id : '';
    if (!changed('result-runs', [runs, showing, snap.run && snap.run.id, isActive(snap.run)])) return;
    fill($('result-runs'), [h('section', { class: 'card', 'aria-labelledby': 'h-runs' },
      h('h2', { id: 'h-runs' }, 'これまでの実行'),
      runs.length
        ? h('ul', { class: 'runs' }, runs.map((r) => h('li', null,
          h('button', {
            type: 'button',
            class: `run-item${r.id === showing ? ' is-current' : ''}`,
            'aria-current': r.id === showing ? 'true' : null,
            dataset: { fk: `run:${r.id}` },
            onclick: () => openPastRun(r.id),
          },
          h('span', { class: 'run-id' }, str(r.id)),
          h('span', { class: 'run-date' }, dateTime(r.startedAt)),
          r.outcome ? outcomeBadge(r.outcome, false)
            : snap.run && snap.run.id === r.id && isActive(snap.run) ? h('span', { class: 'badge badge-run' }, '実行中')
              : h('span', { class: 'badge badge-unk' }, '結論なし'),
          r.id === showing ? h('span', { class: 'run-showing' }, '表示中') : null))))
        : h('p', { class: 'muted' }, 'まだありません。'))]);
  }

  // ---------- 始める ----------

  function init() {
    initTabs();
    initForm();
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) poll();
    });
    poll();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
