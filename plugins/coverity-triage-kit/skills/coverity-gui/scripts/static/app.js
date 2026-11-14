'use strict';
// The page of the screen: it shows /api/state every 3 seconds and saves what the person chooses.
const token = new URLSearchParams(location.search).get('t') || '';
const $ = (id) => document.getElementById(id);
const WORDS = {
  verdict: { true_bug: '本物のバグ', false_positive: '誤検知', intentional: '意図的', undetermined: '判定不能' },
  confidence: { high: '高', medium: '中', low: '低' },
  review: { pass: '合格', flag: '要確認', rework: '差し戻し', unverified: '未検証' },
  build: { ok: '成功', failed: '失敗', not_applied: '適用できず', unknown: '不明' },
  choice: { fix: '修正を取り込む', deviation: '逸脱コメントを登録する', none: '保留にする' },
};
const COMMENT_MAX = 200;
let state = null; // the last reply of /api/state
let layout = ''; // what the table is built for: the result folder, its columns and its CIDs
let open = null; // the opened row: {cid, version, tr, text, sync}
let saves = 0; // a reply of /api/state that was asked for before a save is not shown
const shown = new Map(); // CID -> the row as it is in the table (JSON)

function el(tag, props, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((child) => child != null));
  return node;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: { 'X-Token': token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error);
  return data;
}

function report(error, kind) {
  const offline = '画面に接続できません。チャットで「画面を開いて」と入力して、表示された URL を開いてください';
  $('message').textContent = error instanceof TypeError ? offline : error.message;
  $('message').dataset.kind = kind;
  $('message').hidden = false;
}

function settle(kind) { // the last problem of this kind is over
  if ($('message').dataset.kind === kind) $('message').hidden = true;
}

function columns() {
  return ['CID', '判定', '確信度', ...(state.review ? ['検証'] : []), ...(state.build ? ['ビルド'] : []),
    '修正コード', '逸脱コメント', '選択', '状態'];
}

function cell(label, className, ...children) {
  const td = el('td', { className }, ...children);
  td.dataset.label = label;
  return td;
}

function buildRow(row) {
  const name = `${row.cid}` + (row.members.length ? `（同じ原因: ${row.members.join(', ')}）` : '');
  const item = row.status === 'done';
  const head = item ? el('button', { type: 'button', className: 'open', onclick: () => toggle(row) }, name)
    : el('strong', {}, name);
  if (item) head.setAttribute('aria-expanded', String(!!open && open.cid === row.cid));
  const lead = cell('CID', 'cid', head, el('div', { className: 'sub' }, row.checker || ''),
    el('div', { className: 'sub' }, row.place));
  if (!item) {
    const text = { failed: `失敗: ${row.error}`, unreadable: 'この CID は読み取れません' }[row.status] || '未処置';
    return el('tr', {}, lead, el('td', { colSpan: columns().length - 1, className: 'state' }, text));
  }
  const pick = row.choice && el('select', { className: 'pick', onchange: () => save(row, pick.value) },
    ...Object.entries(WORDS.choice).map(([value, text]) =>
      el('option', { value, disabled: value === 'fix' && !row.can_fix }, text)));
  if (pick) {
    pick.value = row.choice;
    pick.setAttribute('aria-label', `CID ${row.cid} の選択`);
  }
  const note = row.reason || (row.choice_from === 'screen' ? '画面で選択' : 'AI の推奨');
  const tr = el('tr', {}, lead,
    cell('判定', 'short', WORDS.verdict[row.verdict] || '-'),
    cell('確信度', row.confidence === 'low' ? 'short warn' : 'short', WORDS.confidence[row.confidence] || '-'),
    state.review ? cell('検証', row.review === 'pass' ? 'short' : 'short warn', WORDS.review[row.review] || '-') : null,
    state.build ? cell('ビルド', row.build === 'failed' ? 'short warn' : 'short', WORDS.build[row.build] || '-') : null,
    cell('修正コード', 'fix', ...(row.files.length ? row.files.map((file) => el('code', {}, file)) : ['なし'])),
    cell('逸脱コメント', 'deviation', row.comment, row.comment_edited ? el('span', { className: 'badge' }, '編集済み') : null),
    cell('選択', '', pick || null, pick ? el('div', { className: 'sub' }, note) : null),
    cell('状態', 'state', { applied: '取り込み済み', written: '登録済み' }[row.done] || ''));
  tr.dataset.choice = row.choice || '';
  return tr;
}

function diff(text) {
  const pre = el('pre', { className: 'diff' });
  let hunk = false; // inside a hunk a line that starts with + or - is code, before it a file name
  for (const line of text.replace(/\r?\n$/, '').split('\n')) {
    if (line.startsWith('@@')) hunk = true;
    else if (/^(diff |Index: )/.test(line)) hunk = false;
    const kind = line.startsWith('@@') ? 'at' : hunk && line[0] === '+' ? 'add' : hunk && line[0] === '-' ? 'del' : '';
    pre.append(el('span', { className: kind }, line || ' '));
  }
  return pre;
}

function closeDetail() {
  if (!open) return;
  open.tr.remove();
  $(`cid-${open.cid}`)?.querySelector('.open')?.setAttribute('aria-expanded', 'false');
  open = null;
}

async function toggle(row) {
  const same = open && open.cid === row.cid;
  closeDetail();
  if (!same) await openDetail(row).catch((error) => report(error, 'save'));
}

async function openDetail(row) {
  const d = await api(`/api/cid/${row.cid}`);
  const body = $(`cid-${row.cid}`);
  if (!body || !d.result) return;
  const now = () => JSON.parse(shown.get(row.cid)); // the row as it is saved
  const text = el('textarea', { value: d.deviation.comment, rows: 4, oninput: () => sync() });
  text.setAttribute('aria-label', '逸脱コメント');
  const counter = el('span', {});
  const restore = el('button', { type: 'button' }, '元に戻す');
  restore.onclick = () => { text.value = d.deviation.original_comment; sync(); };
  const keep = el('button', { type: 'button', onclick: () => save(now(), now().choice) }, 'コメントを保存');
  const picks = Object.entries(WORDS.choice).map(([value, label]) =>
    el('button', { type: 'button', value, onclick: () => save(now(), value) }, label));
  const sync = () => { // after typing, and after the row was saved
    const saved = now(), length = [...text.value.trim()].length; // characters, as the server counts them
    counter.textContent = `${length} / ${COMMENT_MAX} 文字`;
    counter.className = length > COMMENT_MAX ? 'warn' : 'sub';
    text.readOnly = !saved.choice;
    restore.disabled = !saved.choice || text.value === d.deviation.original_comment;
    keep.disabled = !saved.choice || text.value.trim() === saved.comment;
    for (const button of picks) {
      button.disabled = !saved.choice || (button.value === 'fix' && !saved.can_fix);
      button.setAttribute('aria-pressed', String(button.value === saved.choice));
    }
  };
  const r = d.result, points = d.review ? d.review.points : [];
  const fix = d.patch == null ? [el('p', {}, `修正コードはありません。理由: ${r.no_fix_reason || ''}`)]
    : [el('p', {}, r.fix_summary || ''), diff(d.patch)];
  const tr = el('tr', { className: 'detail' }, el('td', { colSpan: columns().length },
    el('h3', {}, '概要'), el('p', {}, r.summary || ''),
    el('h3', {}, '根拠'),
    el('ul', {}, ...r.evidence.map((e) => el('li', {}, el('code', {}, `${e.file}:${e.line}`), ` ${e.note}`))),
    points.length ? el('h3', {}, '検証の指摘') : null,
    points.length ? el('ul', {}, ...points.map((point) => el('li', {}, point))) : null,
    el('div', { className: 'outputs' },
      el('div', {}, el('h3', {}, '修正コード'), ...fix),
      el('div', {}, el('h3', {}, '逸脱コメント'), text, el('p', { className: 'tools' }, counter, restore, keep),
        el('p', { className: 'sub' }, ['Classification', 'Action', 'Severity'].map((name) =>
          `${name}: ${d.deviation[name.toLowerCase()]}`).join('／')))),
    el('p', { className: 'picks' }, el('strong', {}, '選択'), ...picks)));
  closeDetail();
  body.append(tr);
  body.querySelector('.open').setAttribute('aria-expanded', 'true');
  open = { cid: row.cid, version: d.version, tr, text, sync };
  sync();
}

function put(row, own) {
  const body = $(`cid-${row.cid}`);
  if (!body) return;
  shown.set(row.cid, JSON.stringify(row));
  const old = body.rows[0], tr = buildRow(row);
  const focus = old && old.contains(document.activeElement) && document.activeElement.className;
  if (old) old.replaceWith(tr); else body.append(tr);
  if (focus) tr.querySelector(`.${focus}`)?.focus();
  if (!open || open.cid !== row.cid) return;
  if (own) open.sync();
  else if (row.status === 'done') openDetail(row).catch((error) => report(error, 'save')); // it may be a new result
  else closeDetail();
}

async function save(row, decision) {
  // The version of the result the person looks at: the server refuses the choice when the result has changed since.
  const opened = open && open.cid === row.cid;
  const comment = opened ? open.text.value.trim() : row.comment, version = opened ? open.version : row.version;
  saves += 1;
  try {
    put((await api('/api/decision', { run: state.run, cid: row.cid, decision, comment, version })).row, true);
    settle('save');
  } catch (error) {
    report(error, 'save');
    const pick = $(`cid-${row.cid}`)?.querySelector('select');
    if (pick) pick.value = JSON.parse(shown.get(row.cid)).choice; // the table shows what is saved
  }
  saves += 1;
  await refresh();
}

function render(s) {
  state = s;
  const c = s.conditions, n = s.counts, f = s.fetched;
  const all = (list) => (list.length ? list.join(', ') : 'すべて');
  const shownConditions = { ストリーム: c.stream, チェッカー: all(c.checkers), Impact: all(c.impacts),
    分類: all(c.classification), 上限: `${c.limit} 件` };
  $('conditions').replaceChildren(...Object.entries(shownConditions).map(([name, value]) =>
    el('div', {}, el('dt', {}, name), el('dd', {}, value))));
  $('progress').hidden = !f;
  $('bar').max = Math.max(n.total, 1);
  $('bar').value = n.done + n.failed;
  $('counts').textContent = n.total ? `${n.done} / ${n.total} 件、失敗 ${n.failed} 件` : '条件に合う警告はありません';
  $('working').textContent = s.working.length ? '処置中: ' + s.working.map((w) =>
    `${w.cid}（${String(w.claimed_at || '').slice(11, 16)}〜）`).join('、') : '';
  $('limited').textContent = !f || !f.limited ? ''
    : f.total_matched ? `条件に合う警告は ${f.total_matched} 件あります。設定の上限（${f.count} 件）まで取得しました`
      : `条件に合う警告が、設定の上限（${f.count} 件）より多くあります。上限まで取得しました`; // the search could not count
  $('results').hidden = $('foot').hidden = !s.rows.length;
  $('choices').textContent = Object.entries(WORDS.choice).map(([key, label]) =>
    `${label} ${s.choices[key]} 件`).join('、');
  const key = JSON.stringify([s.run, s.review, s.build, s.rows.map((row) => row.cid)]);
  if (key !== layout) {
    layout = key;
    open = null;
    shown.clear();
    $('table').replaceChildren(el('thead', {}, el('tr', {}, ...columns().map((name) => el('th', {}, name)))),
      ...s.rows.map((row) => el('tbody', { id: `cid-${row.cid}` })));
  }
  for (const row of s.rows) if (shown.get(row.cid) !== JSON.stringify(row)) put(row, false);
}

async function refresh() {
  const asked = saves;
  try {
    const s = await api('/api/state');
    if (asked === saves) render(s);
    settle('state');
  } catch (error) {
    report(error, 'state');
  }
}

$('message').onclick = () => { $('message').hidden = true; };
refresh();
setInterval(refresh, 3000);
