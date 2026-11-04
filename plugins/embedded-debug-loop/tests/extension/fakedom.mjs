// 試験の道具：Web管理画面の画面（index.html と app.js）を、ブラウザーなしで動かす、小さな偽物の DOM。
// app.js が使う分だけを持つ。/api/state などの問い合わせは、試験が渡す関数（api）が答える。

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

import { EXT_DIR } from './helpers.mjs';

const STATIC = path.join(EXT_DIR, 'ui', 'static');
const VOID = new Set(['meta', 'link', 'input', 'br', 'wbr', 'img', 'hr']);

class Node {
  constructor() {
    this.parent = null;
    this.children = [];
  }

  get textContent() {
    return this.children.map((c) => c.textContent).join('');
  }

  set textContent(value) {
    this.replaceChildren(String(value ?? ''));
  }
}

class Text extends Node {
  constructor(value) {
    super();
    this.value = String(value);
  }

  get textContent() {
    return this.value;
  }

  set textContent(value) {
    this.value = String(value ?? '');
  }
}

class Element extends Node {
  constructor(doc, tag) {
    super();
    this.doc = doc;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.listeners = {};
    this.dataset = {};
    this.style = {};
    this.value = '';
    this.disabled = false;
    this.readOnly = false;
    this.tabIndex = 0;
    this.scrollHeight = 0;
    const el = this;
    this.classList = {
      contains: (c) => el.className.split(/\s+/).includes(c),
      add: (...cs) => {
        el.className = [...new Set([...el.className.split(/\s+/).filter(Boolean), ...cs])].join(' ');
      },
      remove: (...cs) => {
        el.className = el.className.split(/\s+/).filter((c) => c && !cs.includes(c)).join(' ');
      },
      toggle: (c, on) => {
        const want = on === undefined ? !el.classList.contains(c) : !!on;
        if (want) el.classList.add(c);
        else el.classList.remove(c);
        return want;
      },
    };
  }

  get id() {
    return this.attrs.get('id') ?? '';
  }

  get className() {
    return this.attrs.get('class') ?? '';
  }

  set className(value) {
    this.attrs.set('class', String(value));
  }

  // 属性と同じ値を持つもの
  get hidden() {
    return this.attrs.has('hidden');
  }

  set hidden(on) {
    if (on) this.attrs.set('hidden', '');
    else this.attrs.delete('hidden');
  }

  get open() {
    return this.attrs.has('open');
  }

  set open(on) {
    if (on) this.attrs.set('open', '');
    else this.attrs.delete('open');
  }

  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === 'value') this.value = String(value);
  }

  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }

  removeAttribute(name) {
    this.attrs.delete(name);
  }

  hasAttribute(name) {
    return this.attrs.has(name);
  }

  append(...nodes) {
    for (const n of nodes) {
      const node = n instanceof Node ? n : new Text(n);
      node.parent?.children.splice(node.parent.children.indexOf(node), 1);
      node.parent = this;
      this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    for (const c of this.children) c.parent = null;
    this.children = [];
    this.append(...nodes);
  }

  addEventListener(type, fn) {
    (this.listeners[type] ??= []).push(fn);
  }

  /** 試験から、出来事を起こす（click・change・toggle・submit） */
  dispatch(type, extra = {}) {
    const event = { type, target: this, currentTarget: this, preventDefault() {}, ...extra };
    for (const fn of this.listeners[type] ?? []) fn(event);
  }

  contains(node) {
    for (let n = node; n; n = n.parent) if (n === this) return true;
    return false;
  }

  focus() {
    this.doc.activeElement = this;
  }

  scrollIntoView() {}

  /** 子孫の要素（深さ優先） */
  *walk() {
    for (const c of this.children) {
      if (c instanceof Element) {
        yield c;
        yield* c.walk();
      }
    }
  }

  matchesSimple(sel) {
    const m = /^([a-z0-9]+)?(?:#([\w-]+))?(?:\.([\w-]+))?(?:\[([\w-]+)\])?$/i.exec(sel);
    if (!m) throw new Error(`偽物の DOM が知らない形の選び方です：${sel}`);
    const [, tag, id, cls, attr] = m;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    if (id && this.id !== id) return false;
    if (cls && !this.classList.contains(cls)) return false;
    if (attr) {
      const key = attr.startsWith('data-') ? attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
      if (!(this.attrs.has(attr) || (key && key in this.dataset))) return false;
    }
    return true;
  }

  matches(selector) {
    return selector.split(',').some((one) => {
      const parts = one.trim().split(/\s+/);
      if (!this.matchesSimple(parts[parts.length - 1])) return false;
      let i = parts.length - 2;
      for (let n = this.parent; n && i >= 0; n = n.parent) if (n instanceof Element && n.matchesSimple(parts[i])) i -= 1;
      return i < 0;
    });
  }

  querySelectorAll(selector) {
    return [...this.walk()].filter((el) => el.matches(selector));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

/** index.html を読み、要素の木にする（この画面のファイルに足りるだけの読み方） */
function parseHtml(doc, html) {
  const root = new Element(doc, 'root');
  const stack = [root];
  const re = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-z0-9]+)\s*>|<([a-z0-9]+)((?:\s+[^\s=>]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/gi;
  for (const m of html.matchAll(re)) {
    const top = stack[stack.length - 1];
    if (m[1]) {
      while (stack.length > 1 && stack.pop().tagName !== m[1].toUpperCase());
    } else if (m[2]) {
      const el = new Element(doc, m[2]);
      for (const a of (m[3] ?? '').matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) el.setAttribute(a[1], a[2] ?? '');
      top.append(el);
      if (!VOID.has(m[2].toLowerCase())) stack.push(el);
    } else if (m[4] && top.tagName !== 'SCRIPT' && top.tagName !== 'STYLE') {
      top.append(m[4]);
    }
  }
  return root;
}

/**
 * 画面を動かす。api(method, path, body) が { status, data } を返す（data は JSON の値）。
 * 返すもの：$（id で要素を探す）、settle（問い合わせと描き直しが終わるのを待つ）、poll（次の読み直しを動かす）、alerts
 */
export async function loadScreen(api) {
  const timers = new Map(); // 番号 → 時間がたったら動かす処理
  let lastTimer = 0;
  const alerts = [];
  const doc = {
    readyState: 'complete',
    hidden: false,
    title: '',
    activeElement: null,
    listeners: {},
    addEventListener(type, fn) {
      (this.listeners[type] ??= []).push(fn);
    },
    createElement: (tag) => new Element(doc, tag),
    getElementById: (id) => [...doc.root.walk()].find((el) => el.id === id) ?? null,
  };
  doc.root = parseHtml(doc, fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8'));
  doc.body = doc.root.querySelector('body');
  doc.activeElement = doc.body;

  const context = {
    document: doc,
    Node,
    URLSearchParams,
    console,
    location: { search: '?k=test', hash: '', pathname: '/' },
    history: { replaceState() {} },
    setTimeout: (fn) => {
      lastTimer += 1;
      timers.set(lastTimer, fn);
      return lastTimer;
    },
    clearTimeout: (n) => timers.delete(n),
    confirm: () => true,
    alert: (text) => alerts.push(text),
    fetch: async (url, init = {}) => {
      const u = new URL(url, 'http://127.0.0.1/');
      const { status = 200, data = null } = await api(init.method ?? 'GET', u.pathname, init.body ? JSON.parse(init.body) : undefined);
      return { ok: status < 400, status, json: async () => data };
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8'), context, { filename: 'app.js' });

  const settle = async () => {
    for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  /** いま待っている時間の処理（次の読み直しなど）を、すべて動かす */
  const poll = async () => {
    const due = [...timers.values()];
    timers.clear();
    for (const fn of due) fn();
    await settle();
  };
  await settle();
  return { $: doc.getElementById, doc, settle, poll, alerts };
}

/** 要素の中の文字（空白を詰める） */
export const textOf = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
