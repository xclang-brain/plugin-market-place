// The screen's HTTP server. Node.js standard modules only.
// - Listens on 127.0.0.1 only. The preferred port is tried first; when it is taken, the OS picks a free one (REQ-403).
// - A new token per start. A request passes with ?k=<token> or with the cookie set by the first page load;
//   the screen then removes the token from the address bar.
// - The Host header must be 127.0.0.1:<port> or localhost:<port> (DNS rebinding).
// - POST only from this screen (same Origin) and only JSON.
// - Strict CSP: only this server's scripts run; agent text is never HTML.

import crypto from 'node:crypto';
import fsp from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';

const MAX_BODY_BYTES = 64 * 1024;
const STATIC = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/index.html': ['index.html', 'text/html; charset=utf-8'],
  '/render.js': ['render.js', 'text/javascript; charset=utf-8'],
  '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
  '/style.css': ['style.css', 'text/css; charset=utf-8'],
};
export const SECURITY_HEADERS = Object.freeze({
  'Content-Security-Policy':
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Cache-Control': 'no-store',
});

const BAD_KEY = 'この画面の URL が正しくないか、古くなっています。チャットに出た URL を開き直してください。';
const BAD_HOST = 'この URL では開けません。チャットに出た URL（http://127.0.0.1: で始まるもの）から開いてください。';
const badKeyPage = () => `<!doctype html><html lang="ja"><meta charset="utf-8"><title>Web管理画面</title><p>${BAD_KEY}</p></html>`;

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function send(res, status, type, body, extra = {}) {
  res.writeHead(status, { ...SECURITY_HEADERS, 'Content-Type': type, ...extra });
  res.end(body);
}
const sendJson = (res, status, value, extra) => send(res, status, 'application/json; charset=utf-8', JSON.stringify(value), extra);

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size <= MAX_BODY_BYTES) chunks.push(chunk);
    });
    req.on('end', () => {
      if (size > MAX_BODY_BYTES) return reject(new HttpError(413, '送られた内容が大きすぎます（64 KB まで）。'));
      const raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) return resolve({});
      try {
        const value = JSON.parse(raw);
        if (value && typeof value === 'object' && !Array.isArray(value)) return resolve(value);
      } catch {
        // refused below
      }
      reject(new HttpError(400, '送られた内容を読み取れません（JSON の形にしてください）。'));
    });
    req.on('error', reject);
  });
}

function cookieOf(req, name) {
  for (const part of String(req.headers.cookie ?? '').split(';')) {
    const [key, ...rest] = part.trim().split('=');
    if (key === name) return rest.join('=');
  }
  return undefined;
}

function compileRoutes(routes) {
  return Object.entries(routes).map(([key, handler]) => {
    const [method, pattern] = key.split(' ');
    const names = [];
    const source = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/:(\w+)/g, (_, name) => {
      names.push(name);
      return '([^/]+)';
    });
    return { method, names, handler, re: new RegExp(`^${source}$`) };
  });
}

/**
 * @param {object} options
 * @param {number} [options.preferredPort] 0 = let the OS choose
 * @param {string} options.staticDir
 * @param {Record<string, Function>} options.routes  'GET /api/state': ({ params, query }) => value; 'POST /api/start': (req, body) => value
 */
export async function startServer({ preferredPort = 0, staticDir, routes = {} }) {
  const token = crypto.randomBytes(24).toString('base64url');
  const tokenBytes = Buffer.from(token);
  const compiled = compileRoutes(routes);
  const matches = (given) => {
    if (typeof given !== 'string') return false;
    const bytes = Buffer.from(given);
    return bytes.length === tokenBytes.length && crypto.timingSafeEqual(bytes, tokenBytes);
  };
  let port = 0;
  let cookieName = '';

  function checkPost(req) {
    const origin = req.headers.origin;
    if (origin !== undefined && origin !== `http://127.0.0.1:${port}` && origin !== `http://localhost:${port}`) {
      throw new HttpError(403, 'ほかの画面からの操作は受け付けません。');
    }
    const site = req.headers['sec-fetch-site'];
    if (site !== undefined && site !== 'same-origin') throw new HttpError(403, 'ほかの画面からの操作は受け付けません。');
    if (!/^application\/json\b/i.test(String(req.headers['content-type'] ?? ''))) {
      throw new HttpError(415, '送る内容は JSON にしてください（Content-Type: application/json）。');
    }
  }

  async function route(req, res, url, cookie) {
    const method = req.method ?? 'GET';
    if (method === 'POST') checkPost(req);
    const file = STATIC[url.pathname];
    if (file) {
      if (method !== 'GET') throw new HttpError(405, 'この操作はできません。');
      let body;
      try {
        body = await fsp.readFile(path.join(staticDir, file[0]));
      } catch {
        throw new HttpError(404, `画面のファイル ${file[0]} が見つかりません。`);
      }
      return send(res, 200, file[1], body, cookie);
    }
    for (const r of compiled) {
      if (r.method !== method) continue;
      const m = r.re.exec(url.pathname);
      if (!m) continue;
      let params;
      try {
        params = Object.fromEntries(r.names.map((name, i) => [name, decodeURIComponent(m[i + 1])]));
      } catch {
        throw new HttpError(400, '問い合わせの形が正しくありません。');
      }
      const body = method === 'POST' ? await readBody(req) : undefined;
      const result = await r.handler({ params, query: Object.fromEntries(url.searchParams), headers: req.headers }, body);
      return sendJson(res, 200, result ?? {}, cookie);
    }
    if (compiled.some((r) => r.re.test(url.pathname))) throw new HttpError(405, 'この操作はできません。');
    throw new HttpError(404, 'そのページはありません。');
  }

  const server = http.createServer(async (req, res) => {
    try {
      const host = String(req.headers.host ?? '').toLowerCase();
      if (host !== `127.0.0.1:${port}` && host !== `localhost:${port}`) throw new HttpError(403, BAD_HOST);
      const raw = req.url ?? '/';
      if (!raw.startsWith('/') || raw.startsWith('//')) throw new HttpError(400, '問い合わせの形が正しくありません。');
      const url = new URL(raw, `http://${host}`);
      const byQuery = matches(url.searchParams.get('k'));
      if (!byQuery && !matches(cookieOf(req, cookieName))) {
        if (STATIC[url.pathname]?.[0] === 'index.html') return send(res, 403, 'text/html; charset=utf-8', badKeyPage());
        throw new HttpError(403, BAD_KEY);
      }
      url.searchParams.delete('k');
      // the cookie lets the page load its parts and reload after the token left the address bar (per port)
      const cookie = byQuery ? { 'Set-Cookie': `${cookieName}=${token}; Path=/; HttpOnly; SameSite=Strict` } : {};
      await route(req, res, url, cookie);
    } catch (error) {
      const known = Number.isInteger(error?.status);
      const status = known ? error.status : 500;
      if (!known) console.error('[Web管理画面] 問い合わせの処理で失敗しました：', error);
      const message = known ? error.message : 'Web管理画面の処理で思わぬ失敗が起きました。もう一度試してください。';
      if (!res.headersSent) sendJson(res, status, { error: message });
      else res.end();
    }
  });

  const listen = (wanted) =>
    new Promise((resolve, reject) => {
      const onError = (error) => {
        server.off('listening', onListening);
        reject(error);
      };
      const onListening = () => {
        server.off('error', onError);
        resolve();
      };
      server.once('error', onError);
      server.once('listening', onListening);
      server.listen({ port: wanted, host: '127.0.0.1', exclusive: true });
    });

  try {
    await listen(preferredPort);
  } catch (error) {
    if (preferredPort === 0 || (error.code !== 'EADDRINUSE' && error.code !== 'EACCES')) throw error;
    await listen(0);
  }
  port = server.address().port;
  cookieName = `ctk_k_${port}`;
  server.unref?.();

  let closing = null;
  const close = () => {
    closing ??= new Promise((resolve) => {
      server.close(() => resolve());
      server.closeAllConnections?.();
    });
    return closing;
  };
  return { url: `http://127.0.0.1:${port}/?k=${token}`, port, token, close };
}
