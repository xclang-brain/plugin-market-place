// Drawing parts that do not depend on the state. The screen logic is in app.js.
// Rule: text from agents and records is always inserted as text nodes, never as HTML.
// innerHTML, outerHTML and srcdoc are refused (tests check it).
(function (global) {
  'use strict';

  const URL_ATTRIBUTES = new Set(['href', 'src']);
  const FORBIDDEN_PROPS = new Set(['innerHTML', 'outerHTML', 'srcdoc']);

  /** Only http(s) URLs (and places on this server) may be linked. */
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
      else if (typeof child === 'object' && child.nodeType) parent.appendChild(child);
      else parent.appendChild(global.document.createTextNode(String(child)));
    }
  }

  /** h('p', { class, dataset, on, ...attributes }, ...children). Strings become text nodes. */
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

  /** A state badge: always text; the color only helps. tone: standby running waiting completed failed stopped unstopped info warn neutral */
  function badge(content, tone) {
    return h('span', { class: `badge badge-${tone ?? 'neutral'}` }, content);
  }

  /** Folded content. `open` (a Set) remembers what is open across redraws. */
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

  function formatTime(value) {
    if (value == null || value === '') return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function formatDateTime(value) {
    if (value == null || value === '') return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${formatTime(value)}`;
  }

  // http(s) URLs in text become links. Only printable ASCII belongs to a URL; trailing punctuation does not.
  const URL_IN_TEXT = /https?:\/\/[!#-&(-;=?-_a-~]+/g;
  const URL_TAIL = /[.,;:!?)\]}]+$/;

  function linkedText(content) {
    const parts = [];
    let last = 0;
    for (const match of content.matchAll(URL_IN_TEXT)) {
      const url = match[0].replace(URL_TAIL, '');
      if (!/^https?:\/\/[^/?#]/.test(url) || safeUrl(url) === null) continue;
      if (match.index > last) parts.push(content.slice(last, match.index));
      parts.push(h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, url));
      last = match.index + url.length;
    }
    if (last < content.length) parts.push(content.slice(last));
    return parts;
  }

  const LONG_TEXT_CHARS = 300;
  const LONG_TEXT_LINES = 8;

  /** A block of text; long text is folded (REQ-423). */
  function textBlock(value, { key, open }) {
    const content = String(value ?? '');
    if (content === '') return h('p', { class: 'muted' }, 'まだありません');
    const long = content.length > LONG_TEXT_CHARS || content.split('\n').length > LONG_TEXT_LINES;
    if (!long) return h('p', { class: 'text-block' }, linkedText(content));
    return collapsible({ key, summary: `全文を開く（${content.length}文字）`, open }, h('p', { class: 'text-block' }, linkedText(content)));
  }

  /** Pairs of label and value. `pairs` is [[label, value], ...]; values may be nodes. */
  function facts(pairs, className = 'facts') {
    return h(
      'dl',
      { class: className },
      pairs.map(([term, value]) => h('div', { class: 'fact' }, h('dt', null, term), h('dd', null, value == null ? '' : value))),
    );
  }

  /** A table. Cells may be text or nodes. Wide tables scroll inside their box; only wrapColumns wrap their text. */
  function table(columns, rows, label, wrapColumns = []) {
    if (!rows.length) return h('p', { class: 'muted' }, 'ありません');
    return h(
      'div',
      { class: 'table-wrap', tabindex: '0', role: 'region', 'aria-label': label ?? '表' },
      h(
        'table',
        null,
        h('thead', null, h('tr', null, columns.map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', null, rows.map((row) => h('tr', null, row.map((cell, i) => h('td', { class: wrapColumns.includes(i) ? 'wrap' : null }, cell == null ? '' : cell))))),
      ),
    );
  }

  /** A progress bar: { value, max, label }. max 0 shows 0 %. */
  function progress(spec, fallbackLabel) {
    if (!spec || typeof spec !== 'object' || !Number.isFinite(Number(spec.value))) return h('p', { class: 'muted' }, 'まだありません');
    const max = Number(spec.max) > 0 ? Number(spec.max) : 0;
    const now = max > 0 ? Math.min(Math.max(Number(spec.value), 0), max) : 0;
    const percent = max > 0 ? Math.round((now / max) * 100) : 0;
    const label = spec.label ? String(spec.label) : fallbackLabel ?? '';
    return h(
      'div',
      { class: 'progress' },
      h('progress', { value: now, max: max > 0 ? max : 1, 'aria-label': label || '進み具合' }),
      h('span', { class: 'progress-text' }, `${label ? `${label}（` : ''}${percent}%${label ? '）' : ''}`),
    );
  }

  global.PluginUi = { h, safeUrl, badge, collapsible, formatTime, formatDateTime, linkedText, textBlock, facts, table, progress };
})(window);
