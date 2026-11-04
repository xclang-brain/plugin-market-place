// 通しの試験（test_e2e_flow.py）の画面の写真を撮る道具（試験ではない）。
//   node e2e_flow_capture.mjs <指示の JSON>
// 指示の JSON：{"dir": "<写真を置くフォルダ>", "shots": [{"url", "name", "width"?, "height"?, "full_page"?, "wait_ms"?,
//               "wait_for"?（出るまで待つ CSS セレクター）}]}
// 写真は PNG、deviceScaleFactor 2。結果を1行の JSON で出す：{"shots": [{"name", "file", "ok", "error"?}]}。
// Playwright は tests/node_modules から探す。ブラウザーは PLUGIN_TEST_BROWSER_CHANNEL（chrome か msedge）があればそれ、
// なければ Playwright の Chromium を使う。
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(here, '..', '..', 'package.json'));

function loadChromium() {
  for (const name of ['playwright', 'playwright-core']) {
    try {
      const module = require(name);
      if (module.chromium) return module.chromium;
    } catch {
      // 次へ
    }
  }
  throw new Error('Playwright が見つかりません（tests/node_modules に playwright を入れてください）');
}

async function launch(chromium) {
  // 手元の Chrome か Edge（試験の入口と同じ順）、なければ Playwright の Chromium
  const wanted = process.env.PLUGIN_TEST_BROWSER_CHANNEL;
  const channels = [...new Set([wanted, 'chrome', 'msedge'].filter((c) => c && c !== 'chromium'))];
  const failures = [];
  for (const channel of channels) {
    try {
      return await chromium.launch({ channel });
    } catch (error) {
      failures.push(`${channel}: ${String(error.message).split('\n')[0]}`);
    }
  }
  try {
    return await chromium.launch();
  } catch (error) {
    failures.push(`chromium: ${String(error.message).split('\n')[0]}`);
    throw new Error(`ブラウザーを起動できません（${failures.join(' / ')}）`);
  }
}

const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'));
const out = { shots: [] };
let browser;
try {
  browser = await launch(loadChromium());
  for (const shot of spec.shots) {
    const file = path.join(spec.dir, `${shot.name}.png`);
    const context = await browser.newContext({
      viewport: { width: shot.width ?? 1280, height: shot.height ?? 900 },
      deviceScaleFactor: 2,
      locale: 'ja-JP',
    });
    const page = await context.newPage();
    try {
      await page.goto(shot.url, { waitUntil: 'networkidle', timeout: 30000 });
      if (shot.wait_for) await page.waitForSelector(shot.wait_for, { timeout: 15000 });
      await page.waitForTimeout(shot.wait_ms ?? 500);
      await page.screenshot({ path: file, fullPage: shot.full_page ?? true });
      out.shots.push({ name: shot.name, file, ok: true });
    } catch (error) {
      out.shots.push({ name: shot.name, file, ok: false, error: String(error.message).split('\n')[0] });
    } finally {
      await context.close();
    }
  }
} catch (error) {
  out.error = String(error.message).split('\n')[0];
} finally {
  if (browser) await browser.close();
}
process.stdout.write(`${JSON.stringify(out)}\n`);
