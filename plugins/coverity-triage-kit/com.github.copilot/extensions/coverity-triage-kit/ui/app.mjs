// The screen's API: maps HTTP routes to the core.
//   GET  /api/state                state of the setup, the run and the previous run (?visible=0 when the tab is hidden)
//   GET  /api/agents/:id/reply     an agent's last reply (full text)
//   POST /api/start                start with the screen's settings
//   POST /api/stop                 stop this run
//   POST /api/issues/:id/file      file one approved issue draft

import { fileURLToPath } from 'node:url';

import { HttpError, startServer } from './server.mjs';

const STATIC_DIR = fileURLToPath(new URL('./static/', import.meta.url));

async function refuseAsBadRequest(action) {
  try {
    return await action();
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, error instanceof Error && error.message ? error.message : '受け付けられませんでした。');
  }
}

export async function startApp({ core, staticDir, preferredPort = 0 }) {
  const routes = {
    'GET /api/state': async ({ query }) => core.getState({ visible: query.visible !== '0' }),
    'GET /api/agents/:id/reply': async ({ params }) => {
      const reply = core.getReply(params.id);
      if (reply == null) throw new HttpError(404, '返事が見つかりません。');
      return { text: String(reply) };
    },
    'POST /api/start': async (_req, body) => {
      const result = await refuseAsBadRequest(() => core.start(body));
      return { ok: true, ...(result?.runId ? { runId: result.runId } : {}) };
    },
    'POST /api/stop': async () => {
      await refuseAsBadRequest(() => core.stop());
      return { ok: true };
    },
    'POST /api/issues/:id/file': async ({ params }) => {
      const result = await refuseAsBadRequest(() => core.fileIssue(params.id));
      return { ok: true, url: result?.url ?? null };
    },
  };
  return startServer({ preferredPort, staticDir: staticDir ?? STATIC_DIR, routes });
}
