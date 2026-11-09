// REQ-403, constitution art. 20: the screen server (127.0.0.1, token then cookie, Host check, same-origin JSON POST, CSP).
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import test from 'node:test';

import { importExt } from './helpers.mjs';

const { startApp } = await importExt('ui/app.mjs');

function fakeCore() {
  const calls = [];
  return {
    calls,
    getState: (opts) => {
      calls.push(['state', opts]);
      return { version: 1, run: null };
    },
    getReply: (id) => (id === 'a1' ? 'こんにちは' : null),
    start: async (body) => {
      calls.push(['start', body]);
      if (body?.bad) throw new Error('「条件ファイルの名前」を入力してください。');
      return { runId: 'R1' };
    },
    stop: async () => calls.push(['stop']),
    fileIssue: async (id) => {
      calls.push(['file', id]);
      return { url: 'https://github.com/o/r/issues/1' };
    },
  };
}

function request(port, { method = 'GET', path = '/', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path, headers: { host: `127.0.0.1:${port}`, ...headers } }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try {
          json = JSON.parse(text);
        } catch {
          // not JSON
        }
        resolve({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

async function server(t, options = {}) {
  const core = fakeCore();
  const app = await startApp({ core, preferredPort: options.preferredPort ?? 0 });
  t.after(() => app.close());
  const post = (path, body, headers = {}) =>
    request(app.port, { method: 'POST', path: `${path}?k=${app.token}`, body: JSON.stringify(body ?? {}), headers: { 'content-type': 'application/json', origin: `http://127.0.0.1:${app.port}`, ...headers } });
  return { app, core, post, get: (path, headers) => request(app.port, { path, headers }) };
}

test('REQ-403 listens on 127.0.0.1 only; the URL carries a fresh token', async (t) => {
  const { app } = await server(t);
  assert.match(app.url, new RegExp(`^http://127\\.0\\.0\\.1:${app.port}/\\?k=[\\w-]{30,}$`));
  const other = await server(t);
  assert.notEqual(other.app.token, app.token);
});

test('REQ-403 the preferred port is used when free; when taken, another free port', async (t) => {
  const blocker = net.createServer();
  await new Promise((resolve) => blocker.listen(0, '127.0.0.1', resolve));
  t.after(() => blocker.close());
  const taken = blocker.address().port;
  const { app } = await server(t, { preferredPort: taken });
  assert.notEqual(app.port, taken);
  const res = await request(app.port, { path: `/api/state?k=${app.token}` });
  assert.equal(res.status, 200);
});

test('every request needs the token or the cookie; the first page sets the cookie (HttpOnly, SameSite=Strict)', async (t) => {
  const { app, get } = await server(t);
  const none = await get('/');
  assert.equal(none.status, 403);
  assert.match(none.headers['content-type'], /text\/html/);
  assert.match(none.text, /チャットに出た URL を開き直してください/);
  assert.equal((await get('/api/state')).status, 403);
  assert.equal((await get('/api/state?k=wrong')).status, 403);
  assert.equal((await get('/style.css')).status, 403);
  const page = await get(`/?k=${app.token}`);
  assert.equal(page.status, 200);
  const cookie = page.headers['set-cookie'][0];
  assert.match(cookie, new RegExp(`^ctk_k_${app.port}=${app.token}; Path=/; HttpOnly; SameSite=Strict$`));
  const viaCookie = await get('/api/state', { cookie: cookie.split(';')[0] });
  assert.equal(viaCookie.status, 200);
  assert.equal((await get('/app.js', { cookie: cookie.split(';')[0] })).status, 200);
  assert.equal((await get('/api/state', { cookie: `ctk_k_${app.port}=bad` })).status, 403);
});

test('the Host header must be 127.0.0.1:<port> or localhost:<port>', async (t) => {
  const { app } = await server(t);
  for (const host of ['evil.example:80', `evil.example:${app.port}`, `127.0.0.1:${app.port + 1}`, '127.0.0.1']) {
    const res = await request(app.port, { path: `/api/state?k=${app.token}`, headers: { host } });
    assert.equal(res.status, 403, host);
  }
  const ok = await request(app.port, { path: `/api/state?k=${app.token}`, headers: { host: `localhost:${app.port}` } });
  assert.equal(ok.status, 200);
});

test('POST only from this screen (same Origin) and only JSON', async (t) => {
  const { app, core, post } = await server(t);
  assert.equal((await post('/api/start', {})).status, 200);
  assert.deepEqual(core.calls.at(-1), ['start', {}]);
  assert.equal((await post('/api/start', {}, { origin: 'http://evil.example' })).status, 403);
  assert.equal((await post('/api/start', {}, { origin: `http://127.0.0.1:${app.port + 1}` })).status, 403);
  assert.equal((await post('/api/start', {}, { 'sec-fetch-site': 'cross-site' })).status, 403);
  assert.equal((await post('/api/start', {}, { 'content-type': 'text/plain' })).status, 415);
  assert.equal((await post('/api/start', {}, { 'content-type': 'application/x-www-form-urlencoded' })).status, 415);
  const big = await request(app.port, { method: 'POST', path: `/api/start?k=${app.token}`, body: JSON.stringify({ x: 'a'.repeat(70_000) }), headers: { 'content-type': 'application/json' } });
  assert.equal(big.status, 413);
  const notJson = await request(app.port, { method: 'POST', path: `/api/start?k=${app.token}`, body: '[1,2]', headers: { 'content-type': 'application/json' } });
  assert.equal(notJson.status, 400);
});

test('a refusal from the core is a 400 with its plain message; routes map to the core', async (t) => {
  const { app, core, post, get } = await server(t);
  const refused = await post('/api/start', { bad: true });
  assert.equal(refused.status, 400);
  assert.equal(refused.json.error, '「条件ファイルの名前」を入力してください。');
  assert.equal((await post('/api/stop')).status, 200);
  const filed = await post('/api/issues/M1/file');
  assert.deepEqual(filed.json, { ok: true, url: 'https://github.com/o/r/issues/1' });
  assert.deepEqual(core.calls.at(-1), ['file', 'M1']);
  assert.deepEqual((await get(`/api/agents/a1/reply?k=${app.token}`)).json, { text: 'こんにちは' });
  assert.equal((await get(`/api/agents/zz/reply?k=${app.token}`)).status, 404);
  await get(`/api/state?k=${app.token}&visible=0`);
  assert.deepEqual(core.calls.filter((c) => c[0] === 'state').at(-1), ['state', { visible: false }]);
  assert.equal((await get(`/api/start?k=${app.token}`)).status, 405);
  assert.equal((await get(`/nothing?k=${app.token}`)).status, 404);
  assert.equal((await get(`/../defaults.json?k=${app.token}`)).status, 404);
  assert.equal((await get(`/adapter.mjs?k=${app.token}`)).status, 404);
});

test('every response has the strict CSP, nosniff, no-referrer and no-store', async (t) => {
  const { app, get } = await server(t);
  for (const path of ['/', '/app.js', '/api/state', '/nothing']) {
    const res = await get(`${path}?k=${app.token}`);
    assert.match(res.headers['content-security-policy'], /default-src 'none'; script-src 'self'/, path);
    assert.match(res.headers['content-security-policy'], /frame-ancestors 'none'/);
    assert.equal(res.headers['x-content-type-options'], 'nosniff');
    assert.equal(res.headers['referrer-policy'], 'no-referrer');
    assert.equal(res.headers['cache-control'], 'no-store');
  }
  const forbidden = await get('/api/state');
  assert.match(forbidden.headers['content-security-policy'], /default-src 'none'/);
});

test('an unexpected failure is a 500 without internals', async (t) => {
  const core = fakeCore();
  core.getState = () => {
    throw new Error('secret internal detail');
  };
  const app = await startApp({ core, preferredPort: 0 });
  t.after(() => app.close());
  const originalError = console.error;
  console.error = () => {};
  try {
    const res = await request(app.port, { path: `/api/state?k=${app.token}` });
    assert.equal(res.status, 500);
    assert.doesNotMatch(res.text, /secret internal detail/);
  } finally {
    console.error = originalError;
  }
});
