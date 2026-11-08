// REQ-423 (and REQ-411's "visible without scrolling"): the real screen in Chromium through Playwright.
// Width 390 px and a laptop width, light and dark, no broken layout, WCAG 2.2 contrast, 200 % text.
// Playwright is required with require('playwright'); when it is missing the test is skipped with the reason.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import { after, before, describe, test } from 'node:test';

import { sdkEvent } from './fake-sdk.mjs';
import { finishedStatus, item, startWith, statusOf } from './helpers.mjs';
import { contrastProblems, layoutProblems } from './page-checks.mjs';

function loadPlaywright() {
  const require = createRequire(import.meta.url);
  try {
    return require('playwright');
  } catch {
    // NODE_PATH is not used by ES modules' resolution: look there too
    for (const dir of (process.env.NODE_PATH ?? '').split(path.delimiter).filter(Boolean)) {
      try {
        return require(path.join(dir, 'playwright'));
      } catch {
        // next
      }
    }
  }
  return null;
}

const playwright = loadPlaywright();
const SKIP = playwright ? false : 'Playwright がありません（npm で playwright を入れると動きます）';

describe('the screen in a browser', { skip: SKIP }, () => {
  let browser;
  const cleanups = [];
  const t = { after: (fn) => cleanups.push(fn) };

  before(async () => {
    browser = await playwright.chromium.launch();
  });
  after(async () => {
    await browser?.close();
    for (const fn of cleanups.reverse()) await fn();
  });

  async function harness() {
    const h = await startWith(t);
    h.runner.on('run:status', () =>
      statusOf({
        items: [item('20001'), item('20002', { status: 'in_progress', stalled: true, review: null }), item('G1', { cids: [1, 2, 3], status: 'pending', review: null })],
        errors: [{ item: '20007', kind: '取得', detail: '経路を取れませんでした。とても長い説明がここに続きます。'.repeat(3), level: 'critical' }],
      }),
    );
    const opened = await h.open('Coverity の警告をトリアージして。High だけ、NULL_RETURNS だけ');
    await h.idle();
    return { h, url: opened.text.match(/http:\/\/\S+/)[0] };
  }

  async function page(url, { width = 390, height = 844, scheme = 'light' } = {}) {
    const p = await browser.newPage({ viewport: { width, height }, colorScheme: scheme });
    const errors = [];
    p.on('pageerror', (e) => errors.push(String(e)));
    p.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
    await p.goto(url);
    await p.waitForSelector('#s-head:not([hidden])');
    return { p, errors };
  }

  async function check(p, label) {
    const layout = await p.evaluate(layoutProblems);
    assert.deepEqual(layout, [], `${label}: layout`);
    const { problems, checked } = await p.evaluate(contrastProblems, 4.5);
    assert.ok(checked > 10, `${label}: some text was checked`);
    assert.deepEqual(problems, [], `${label}: contrast`);
  }

  test('setup, running and finished: no layout problems and enough contrast, at 390 px and 1280 px, light and dark', async () => {
    const { h, url } = await harness();
    const views = [];
    for (const scheme of ['light', 'dark']) {
      for (const width of [390, 1280]) views.push(await page(url, { width, scheme, height: 800 }));
    }
    for (const { p } of views) await p.waitForSelector('#start');
    for (const [i, { p }] of views.entries()) await check(p, `setup ${i}`);

    // the token left the address bar; a reload still works with the cookie
    assert.doesNotMatch(views[0].p.url(), /k=/);
    await views[0].p.reload();
    await views[0].p.waitForSelector('#start');

    await views[0].p.click('#start');
    await h.idle();
    await h.clock.advance(1);
    const root = h.ctx.run.rootId;
    h.fake.emit(sdkEvent.toolStart(root, 'tc-1', 'task', { description: '20002 の作業' }));
    h.fake.emit(sdkEvent.subagentStarted('w1', { parentId: root, agentType: h.defaults.roles.worker, toolCallId: 'tc-1', model: 'gpt-6-luna' }));
    h.fake.emit(sdkEvent.permissionRequested('w1', 'p1', { kind: 'shell', fullCommandText: 'uv run --native-tls --quiet "/a/very/long/path/to/the/command/that/does/not/fit/on/a/phone/screen.py" tick --run "/out/20261108-101500"' }));
    await h.idle();
    for (const { p } of views) await p.waitForSelector('article[data-slot="worker-1"][data-status="waiting"]');
    for (const [i, { p }] of views.entries()) await check(p, `running ${i}`);

    // REQ-411: phases and every slot are visible without scrolling on a laptop screen
    const laptop = views[1].p;
    const boxes = await laptop.evaluate(() => [...document.querySelectorAll('#phases, #agents .agent')].map((el) => el.getBoundingClientRect().top));
    assert.ok(boxes.length >= 6);
    for (const top of boxes) assert.ok(top < 800, `top ${top} is above the fold`);

    h.runner.on('run:status', () => finishedStatus({ issues_found: [{ title: '統括の課題', body: '本文', at: 'x' }] }));
    h.fake.emit(sdkEvent.agentIdle(root));
    await h.idle();
    for (const { p } of views) await p.waitForSelector('#results');
    for (const [i, { p }] of views.entries()) await check(p, `finished ${i}`);

    // WCAG 1.4.4: 200 % text does not break the layout
    await laptop.evaluate(() => {
      document.documentElement.style.fontSize = '200%';
    });
    assert.deepEqual(await laptop.evaluate(layoutProblems), [], '200 % text');

    for (const { p, errors } of views) {
      assert.deepEqual(errors, []);
      await p.close();
    }
  });
});
