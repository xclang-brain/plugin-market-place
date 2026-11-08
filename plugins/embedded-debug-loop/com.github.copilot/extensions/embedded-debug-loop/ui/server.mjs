// Web管理画面のサーバー。Node.js の標準の部品だけを使う。
// - 127.0.0.1 だけで待ち受け、番号は OS に選ばせる。
// - 起動ごとに合言葉（24 文字）を作る。すべての問い合わせで、URL の ?k= か、最初に開いたときに渡すクッキーで確かめる。
// - Host が 127.0.0.1 か localhost で、番号が合うものだけを受ける（ほかのサイトからの呼び出しを防ぐ）。
// - 書き込みの要求（POST）は、この画面から送ったもの（Origin が同じ）で、JSON のものだけを受ける。
//   同じ PC のほかのポートの画面からの送信（クッキーはポートを区別しない）を断るため。
// api：state()、saveSettings(body)、start(body)、stop()、runDetail(id)。誤りは status と message を持つ例外で返す。

import crypto from 'node:crypto';
import fsp from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';

const MAX_BODY_BYTES = 64 * 1024;
const STATIC = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/index.html': ['index.html', 'text/html; charset=utf-8'],
  '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
  '/style.css': ['style.css', 'text/css; charset=utf-8'],
};
// 動かしてよいスクリプトは、このサーバーのファイル（app.js）だけ。画面のファイルに直接書いたスクリプトは動かない
const HEADERS = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Content-Security-Policy':
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
};

const BAD_KEY = 'この画面の URL が正しくないか、古くなっています。チャットに出た URL を開き直してください。';
const badKeyPage = () => `<!doctype html><meta charset="utf-8"><title>Web管理画面</title><p>${BAD_KEY}</p>`;

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function send(res, status, type, body, extra = {}) {
  res.writeHead(status, { ...HEADERS, 'Content-Type': type, ...extra });
  res.end(body);
}
const sendJson = (res, status, value) => send(res, status, 'application/json; charset=utf-8', JSON.stringify(value));

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
        // 下で断る
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

export async function startServer({ staticDir, api }) {
  const token = crypto.randomBytes(18).toString('base64url');
  const tokenBytes = Buffer.from(token);
  const matches = (given) => {
    if (typeof given !== 'string') return false;
    const bytes = Buffer.from(given);
    return bytes.length === tokenBytes.length && crypto.timingSafeEqual(bytes, tokenBytes);
  };
  let port = 0;
  let cookieName = '';

  // POST は、この画面から送った JSON だけを受ける（Origin がなければ、ブラウザーからではないとみなす）
  function checkPost(req) {
    const origin = req.headers.origin;
    if (origin !== undefined && origin !== `http://127.0.0.1:${port}` && origin !== `http://localhost:${port}`) {
      throw new HttpError(403, 'ほかの画面からの操作は受け付けません。');
    }
    if (!/^application\/json\b/i.test(String(req.headers['content-type'] ?? ''))) {
      throw new HttpError(415, '送る内容は JSON にしてください（Content-Type: application/json）。');
    }
  }

  async function route(req, res, url) {
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
      // 画面の部品（app.js など）とデータの取得も通るよう、合言葉をクッキーでも渡す（番号ごとに別の名前）
      return send(res, 200, file[1], body, { 'Set-Cookie': `${cookieName}=${token}; Path=/; HttpOnly; SameSite=Strict` });
    }
    const runMatch = /^\/api\/runs\/([^/]+)$/.exec(url.pathname);
    if (method === 'GET' && url.pathname === '/api/state') return sendJson(res, 200, await api.state());
    if (method === 'GET' && runMatch) return sendJson(res, 200, await api.runDetail(runMatch[1]));
    if (method === 'POST' && url.pathname === '/api/settings') return sendJson(res, 200, await api.saveSettings(await readBody(req)));
    if (method === 'POST' && url.pathname === '/api/start') return sendJson(res, 200, await api.start(await readBody(req)));
    if (method === 'POST' && url.pathname === '/api/stop') return sendJson(res, 200, await api.stop(await readBody(req)));
    if (['/api/state', '/api/settings', '/api/start', '/api/stop'].includes(url.pathname) || runMatch) {
      throw new HttpError(405, 'この操作はできません。');
    }
    throw new HttpError(404, 'そのページはありません。');
  }

  const server = http.createServer(async (req, res) => {
    try {
      const host = String(req.headers.host ?? '');
      if (host !== `127.0.0.1:${port}` && host !== `localhost:${port}`) throw new HttpError(403, 'この URL では開けません。チャットに出た URL（http://127.0.0.1: で始まるもの）から開いてください。');
      const url = new URL(req.url ?? '/', `http://${host}`);
      if (!matches(url.searchParams.get('k')) && !matches(cookieOf(req, cookieName))) {
        // 画面を開こうとしたときは、案内の短いページを返す（JSON の文字をそのまま見せない）
        if (STATIC[url.pathname]?.[0] === 'index.html') return send(res, 403, 'text/html; charset=utf-8', badKeyPage());
        throw new HttpError(403, BAD_KEY);
      }
      await route(req, res, url);
    } catch (error) {
      const known = Number.isInteger(error?.status);
      const status = known ? error.status : 500;
      if (!known) console.error('[Web管理画面] 問い合わせの処理で失敗しました：', error);
      else if (status >= 500) console.error(`[Web管理画面] ${error.message}`);
      const message = known ? error.message : 'Web管理画面の処理で思わぬ失敗が起きました。もう一度試してください。';
      if (!res.headersSent) sendJson(res, status, { error: message });
      else res.end();
    }
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      resolve();
    });
  });
  port = server.address().port;
  cookieName = `edl_k_${port}`;
  server.unref?.();

  return {
    url: `http://127.0.0.1:${port}/?k=${token}`,
    port,
    token,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}
