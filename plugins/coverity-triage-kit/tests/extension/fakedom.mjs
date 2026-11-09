// A small fake DOM that runs the screen (index.html, render.js, app.js) without a browser.
// Only what the screen uses. /api/* requests are answered by the test's api(method, path, body).

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

import { EXT_DIR } from './helpers.mjs';

const STATIC = path.join(EXT_DIR, 'ui', 'static');
const VOID = new Set(['meta', 'link', 'input', 'br', 'img', 'hr']);

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

  get parentElement() {
    return this.parent;
  }

  remove() {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
}

class Text extends Node {
  constructor(value) {
    super();
    this.nodeType = 3;
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
    this.nodeType = 1;
    this.doc = doc;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.listeners = {};
    this.dataset = {};
    this.value = '';
    this.disabled = false;
    const el = this;
    this.classList = { contains: (c) => el.className.split(/\s+/).includes(c) };
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

  set innerHTML(_v) {
    throw new Error('innerHTML is not allowed');
  }

  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === 'value') this.value = String(value);
    if (name === 'disabled') this.disabled = true;
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

  appendChild(node) {
    node.parent?.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = this;
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    for (const c of this.children) c.parent = null;
    this.children = [];
    for (const n of nodes) this.appendChild(n instanceof Node ? n : new Text(n));
  }

  addEventListener(type, fn) {
    (this.listeners[type] ??= []).push(fn);
  }

  /** fire an event from the test (click, change, input, toggle) */
  async dispatch(type, extra = {}) {
    const event = { type, target: this, currentTarget: this, preventDefault() {}, ...extra };
    for (const fn of this.listeners[type] ?? []) await fn(event);
  }

  *walk() {
    for (const c of this.children) {
      if (c instanceof Element) {
        yield c;
        yield* c.walk();
      }
    }
  }

  matchesSimple(sel) {
    const m = /^([a-z0-9]+)?(?:#([\w-]+))?(?:\.([\w-]+))?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/i.exec(sel);
    if (!m) throw new Error(`the fake DOM does not know this selector: ${sel}`);
    const [, tag, id, cls, attr, attrValue] = m;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    if (id && this.id !== id) return false;
    if (cls && !this.classList.contains(cls)) return false;
    if (attr) {
      const key = attr.startsWith('data-') ? attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
      const has = this.attrs.has(attr) || (key && key in this.dataset);
      if (!has) return false;
      if (attrValue !== undefined) {
        const v = this.attrs.has(attr) ? this.attrs.get(attr) : String(this.dataset[key]);
        if (v !== attrValue) return false;
      }
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
      top.appendChild(el);
      if (!VOID.has(m[2].toLowerCase())) stack.push(el);
    } else if (m[4] && top.tagName !== 'SCRIPT' && top.tagName !== 'STYLE' && top.tagName !== 'NOSCRIPT') {
      top.appendChild(new Text(m[4]));
    }
  }
  return root;
}

/**
 * Runs the screen. api(method, path, body) returns { status?, data }.
 * Returns $, doc, settle, poll, requests, history.
 */
export async function loadScreen(api, { search = '?k=test-token' } = {}) {
  const timers = new Map();
  let lastTimer = 0;
  const requests = [];
  const history = [];
  const doc = {
    hidden: false,
    title: '',
    createElement: (tag) => new Element(doc, tag),
    createTextNode: (v) => new Text(v),
    getElementById: (id) => [...doc.root.walk()].find((el) => el.id === id) ?? null,
    querySelectorAll: (sel) => doc.root.querySelectorAll(sel),
    querySelector: (sel) => doc.root.querySelector(sel),
  };
  doc.root = parseHtml(doc, fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8'));
  const context = {
    document: doc,
    URLSearchParams,
    URL,
    console,
    location: { search, hash: '', pathname: '/', href: `http://127.0.0.1:1/${search}` },
    history: { replaceState: (...args) => history.push(args) },
    setTimeout: (fn) => {
      lastTimer += 1;
      timers.set(lastTimer, fn);
      return lastTimer;
    },
    clearTimeout: (n) => timers.delete(n),
    fetch: async (url, init = {}) => {
      const u = new URL(url, 'http://127.0.0.1:1/');
      const body = init.body ? JSON.parse(init.body) : undefined;
      requests.push({ method: init.method ?? 'GET', path: u.pathname, query: Object.fromEntries(u.searchParams), body, headers: init.headers ?? {} });
      const { status = 200, data = null } = await api(init.method ?? 'GET', u.pathname, body, u);
      return { ok: status < 400, status, json: async () => data };
    },
  };
  context.window = context;
  vm.createContext(context);
  for (const file of ['render.js', 'app.js']) vm.runInContext(fs.readFileSync(path.join(STATIC, file), 'utf8'), context, { filename: file });
  const settle = async () => {
    for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  const poll = async () => {
    const due = [...timers.values()];
    timers.clear();
    for (const fn of due) fn();
    await settle();
  };
  await settle();
  return { $: doc.getElementById, doc, settle, poll, requests, history, context };
}

// The text of an element, with a space between separate text nodes (easier to match than textContent)
function words(node, out) {
  if (node instanceof Text) out.push(node.value);
  else for (const c of node.children) words(c, out);
  return out;
}
export const textOf = (el) => (el ? words(el, []).join(' ').replace(/\s+/g, ' ').trim() : '');
