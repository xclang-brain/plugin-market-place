// 描画の部品。生成プラグインの状態に依らない部品だけを置く（クリエイターの画面からも使う。設計書 9章）。
// 生成プラグインの画面を組み立てる部分は app.js にある。
//
// 決まり
// - エージェントや記録から来た文字は、必ず文字として出す（文字のノードにする）。HTML としては扱わない。
//   innerHTML などは使わない（試験が確かめる）。
// - 使い方：window.PluginUi.h('p', { class: 'x' }, '文字') のように要素を作る。
(function (global) {
  'use strict';

  // ---- 要素を作る ----------------------------------------------------------

  const URL_ATTRIBUTES = new Set(['href', 'src']);
  const FORBIDDEN_PROPS = new Set(['innerHTML', 'outerHTML', 'srcdoc']);

  /** 開いてよい URL か（http、https、同じサーバーの中の場所）。それ以外は付けない。 */
  function safeUrl(value) {
    try {
      const url = new URL(String(value), global.location.href);
      return url.protocol === 'http:' || url.protocol === 'https:' ? String(value) : null;
    } catch {
      return null;
    }
  }

  function appendChildren(parent, children) {
    for (const child of children) {
      if (child == null || child === false) continue;
      if (Array.isArray(child)) appendChildren(parent, child);
      else if (child instanceof global.Node) parent.appendChild(child);
      else parent.appendChild(global.document.createTextNode(String(child)));
    }
  }

  /**
   * 要素を作る。props：class、dataset（オブジェクト）、on（{ イベント名: 関数 }）、そのほかは属性。
   * 子は文字・数・要素・それらの配列（null と false は飛ばす）。文字は文字のノードにする。
   */
  function h(tag, props, ...children) {
    const el = global.document.createElement(tag);
    for (const [key, value] of Object.entries(props ?? {})) {
      if (value == null || value === false) continue;
      if (FORBIDDEN_PROPS.has(key)) throw new Error('HTML は組み立てません');
      if (key === 'class') el.className = value;
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key === 'on') for (const [name, fn] of Object.entries(value)) el.addEventListener(name, fn);
      else if (URL_ATTRIBUTES.has(key)) {
        const safe = safeUrl(value);
        if (safe !== null) el.setAttribute(key, safe);
      } else el.setAttribute(key, value === true ? '' : String(value));
    }
    appendChildren(el, children);
    return el;
  }

  // ---- 小さな部品 ----------------------------------------------------------

  /** 状態を文字で示す札。色は補助で、文字がいつも付く。tone：standby、running、waiting、completed、failed、stopped、unstopped、info、warn、neutral */
  function badge(text, tone) {
    return h('span', { class: `badge badge-${tone ?? 'neutral'}` }, text);
  }

  /** 畳んだ内容。key で開いているかを覚える（描き直しても開いたまま）。open は Set。 */
  function collapsible({ key, summary, open, onOpen }, ...children) {
    const isOpen = open instanceof Set ? open.has(key) : false;
    return h(
      'details',
      {
        class: 'fold',
        open: isOpen,
        dataset: { key },
        on: {
          toggle: (event) => {
            const el = event.currentTarget;
            if (open instanceof Set) {
              if (el.open) open.add(key);
              else open.delete(key);
            }
            if (el.open && onOpen) onOpen(el);
          },
        },
      },
      h('summary', null, summary),
      h('div', { class: 'fold-body' }, children),
    );
  }

  /** 時刻（ミリ秒か ISO 形式の文字）を「時:分:秒」にする。読めなければ null。 */
  function formatTime(value) {
    if (value == null || value === '') return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  /** 日付と時刻（終わった時刻など）。読めなければ null。 */
  function formatDateTime(value) {
    if (value == null || value === '') return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${formatTime(value)}`;
  }

  // ---- 設定の部品（text、number、select、checkbox、path） ---------------------

  function fieldId(key) {
    return `setting-${String(key).replace(/[^\w-]/g, '_')}`;
  }

  function optionOf(option) {
    return typeof option === 'object' && option !== null
      ? { value: String(option.value ?? option.id ?? ''), label: String(option.label ?? option.name ?? option.value ?? option.id ?? '') }
      : { value: String(option), label: String(option) };
  }

  /**
   * 設定の欄を作る。def は作業の定義の settings の1件（key、label、type、default、required、help、options）。
   * @returns {{ el: Element, read: () => (string|number|boolean|null), isEmpty: () => boolean, input: Element }}
   */
  function settingField(def, value, onInput) {
    const id = fieldId(def.key);
    const initial = value ?? def.default ?? '';
    const notify = () => onInput && onInput(def.key, read());
    const helpId = def.help ? `${id}-help` : null;
    const common = { id, dataset: { key: def.key }, 'aria-describedby': helpId, on: { input: notify, change: notify } };
    let input;
    let read;

    if (def.type === 'checkbox') {
      input = h('input', { ...common, type: 'checkbox', checked: initial === true || initial === 'true' });
      read = () => input.checked;
    } else if (def.type === 'select') {
      const options = (def.options ?? []).map(optionOf);
      input = h(
        'select',
        common,
        options.map((o) => h('option', { value: o.value, selected: o.value === String(initial) }, o.label)),
      );
      read = () => input.value;
    } else if (def.type === 'number') {
      input = h('input', { ...common, type: 'number', value: initial, step: def.step ?? 'any', min: def.min, max: def.max });
      read = () => (input.value.trim() === '' ? null : Number(input.value));
    } else {
      // text と path（ファイルやフォルダの場所を文字で入れる）
      input = h('input', {
        ...common,
        type: 'text',
        value: initial,
        placeholder: def.type === 'path' ? 'ファイルやフォルダの場所を入力' : null,
        autocomplete: 'off',
      });
      read = () => input.value;
    }

    const required = def.required ? h('span', { class: 'required' }, '（必須）') : null;
    const help = def.help ? h('p', { class: 'help', id: helpId }, def.help) : null;
    const el =
      def.type === 'checkbox'
        ? h('div', { class: 'field field-check' }, h('label', { for: id }, input, h('span', null, def.label ?? def.key)), help)
        : h('div', { class: 'field' }, h('label', { for: id }, def.label ?? def.key, required), input, help);

    return {
      el,
      input,
      read,
      isEmpty: () => def.type !== 'checkbox' && (read() === null || read() === ''),
    };
  }

  // ---- 表示の部品（kv、table、progress、text、image、file） ----------------------
  //
  // 値の形
  //   text      文字（中の http・https の URL は、押せるリンクにする）
  //   kv        { "見出し": "値" } か [{ label, value }]
  //   table     { columns: ["見出し", ...], rows: [["値", ...], ...] }
  //   progress  0〜100 の数、または { value, max, label }（max が 0 のときは 0%）
  //   image     { fileId, name }（fileId は記録のツールで知らされたファイルの番号）
  //   file      { fileId, name }

  const LONG_TEXT_CHARS = 300;
  const LONG_TEXT_LINES = 8;

  function none() {
    return h('p', { class: 'muted' }, 'まだありません');
  }

  /** 値は送られたが、中身が0件（行のない表、項目のない一覧） */
  function emptyList() {
    return h('p', { class: 'muted' }, 'ありません');
  }

  function invalid() {
    return h('p', { class: 'muted' }, '表示できない形の値です');
  }

  // 文章の中の http・https の URL。URL に入れるのは ASCII の表示できる文字だけで、空白・< > " ' ` と、
  // ASCII でない文字（ひらがな・漢字・全角の句読点など）で切る。終わりの英字の句読点は URL に入れない
  const URL_IN_TEXT = /https?:\/\/[!#-&(-;=?-_a-~]+/g;
  const URL_TAIL = /[.,;:!?)\]}]+$/;

  /** 文章を、文字のノードと、URL のリンク（新しいタブで開く）に分ける。文字は文字のまま出す。 */
  function linkedText(text) {
    const parts = [];
    let last = 0;
    for (const match of text.matchAll(URL_IN_TEXT)) {
      const url = match[0].replace(URL_TAIL, '');
      if (!/^https?:\/\/[^/?#]/.test(url) || safeUrl(url) === null) continue;
      if (match.index > last) parts.push(text.slice(last, match.index));
      parts.push(h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, url));
      last = match.index + url.length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts;
  }

  function textPart(value, ctx) {
    const text = String(value);
    if (text === '') return none();
    const long = text.length > LONG_TEXT_CHARS || text.split('\n').length > LONG_TEXT_LINES;
    if (!long) return h('p', { class: 'text-block' }, linkedText(text));
    return collapsible({ key: ctx.foldKey, summary: `全文を開く（${text.length}文字）`, open: ctx.open }, h('p', { class: 'text-block' }, linkedText(text)));
  }

  function kvPart(value) {
    const pairs = Array.isArray(value)
      ? value.map((item) => [item?.label ?? item?.key, item?.value])
      : value && typeof value === 'object'
        ? Object.entries(value)
        : null;
    if (!pairs) return invalid();
    if (pairs.length === 0) return emptyList();
    return h(
      'dl',
      { class: 'kv' },
      pairs.map(([k, v]) => h('div', { class: 'fact' }, h('dt', null, k), h('dd', null, v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : v))),
    );
  }

  function tablePart(value) {
    if (!value || !Array.isArray(value.columns) || !Array.isArray(value.rows)) return invalid();
    if (value.rows.length === 0) return emptyList();
    return h(
      'div',
      { class: 'table-wrap', tabindex: '0', role: 'region', 'aria-label': '表' },
      h(
        'table',
        null,
        h('thead', null, h('tr', null, value.columns.map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', null, value.rows.map((row) => h('tr', null, (Array.isArray(row) ? row : [row]).map((cell) => h('td', null, cell == null ? '' : cell))))),
      ),
    );
  }

  function progressPart(value) {
    const spec = typeof value === 'number' ? { value, max: 100 } : value;
    if (!spec || typeof spec !== 'object' || !Number.isFinite(Number(spec.value))) return invalid();
    const max = Number(spec.max) > 0 ? Number(spec.max) : 100;
    const now = Math.min(Math.max(Number(spec.value), 0), max);
    const percent = Math.round((now / max) * 100);
    return h(
      'div',
      { class: 'progress' },
      h('progress', { value: now, max, 'aria-label': spec.label ?? '進み具合' }),
      h('span', { class: 'progress-text' }, `${spec.label ? `${spec.label}：` : ''}${percent}%`),
    );
  }

  function fileRef(value) {
    const ref = typeof value === 'string' ? { fileId: value, name: value } : value;
    return ref && ref.fileId != null && ref.fileId !== '' ? { fileId: String(ref.fileId), name: String(ref.name ?? ref.fileId) } : null;
  }

  function imagePart(value, ctx) {
    const ref = fileRef(value);
    if (!ref) return invalid();
    return h('img', { class: 'display-image', src: ctx.fileUrl(ref.fileId), alt: ref.name });
  }

  function filePart(value, ctx) {
    const ref = fileRef(value);
    if (!ref) return invalid();
    return h('p', null, h('a', { class: 'file-link', href: ctx.fileUrl(ref.fileId), download: ref.name }, `${ref.name}（ダウンロード）`));
  }

  const DISPLAY_PARTS = { text: textPart, kv: kvPart, table: tablePart, progress: progressPart, image: imagePart, file: filePart };

  /**
   * 表示の部品を作る。def は作業の定義の displays の1件（key、label、type）。
   * ctx：{ fileUrl(fileId) -> 場所, open: 開いている畳みの Set }
   */
  function displayPart(def, value, ctx) {
    const make = DISPLAY_PARTS[def.type] ?? textPart;
    const empty = value == null || value === '';
    const body = empty ? none() : make(value, { ...ctx, foldKey: `display:${def.key}` });
    return h('div', { class: 'display', dataset: { key: def.key, type: def.type ?? 'text' } }, h('h3', null, def.label ?? def.key), body);
  }

  global.PluginUi = { h, safeUrl, badge, collapsible, formatTime, formatDateTime, settingField, displayPart };
})(window);
