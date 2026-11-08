// The results and settings screen in Chromium (Playwright). Started by test_gui_screen.py, which prepares the
// repository, the run folder and the screen's server, and checks the files afterwards.
//
//   node gui_screen.mjs --check          {"playwright": true|false}
//   node gui_screen.mjs --selftest       the problems the page checks find on small pages made to fail (and to pass)
//   node gui_screen.mjs <config.json>    writes {"checks": [{"name", "problems": [...]}], "fatal": null|"..."} to config.out
//
// config: {base: "http://127.0.0.1:<port>/", token, run, rows: [<item id>...], reject: <item id>,
//          deviation: <item id>, comment: "<new comment>", settingsUrl: "<coverity.url of config.yaml>", out}
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

function loadPlaywright() {
  const require = createRequire(import.meta.url);
  try {
    return require('playwright');
  } catch {
    // ES modules do not look in NODE_PATH: look there too
    for (const dir of (process.env.NODE_PATH ?? '').split(path.delimiter).filter(Boolean)) {
      try {
        return require(path.join(dir, 'playwright'));
      } catch {
        // the next one
      }
    }
  }
  return null;
}

const playwright = loadPlaywright();
if (process.argv[2] === '--check') {
  process.stdout.write(JSON.stringify({ playwright: !!playwright }) + '\n');
  process.exit(0);
}
const config = process.argv[2] === '--selftest' ? {} : JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const report = { checks: [], fatal: null };
const check = (name, problems) => report.checks.push({ name, problems: problems ?? [] });
const WAIT = 20000;

// ---- checks that run in the page (no outside variables) ------------------------------------------------

/** The view's tab is selected, its section is shown and has content. */
function viewProblems(view) {
  const problems = [];
  const tab = document.querySelector(`[role="tab"][data-view="${view}"]`);
  if (!tab) problems.push(`タブ ${view} がない`);
  else if (tab.getAttribute('aria-selected') !== 'true') problems.push(`タブ ${view} が選ばれていない`);
  const section = document.querySelector(`#view-${view}`);
  if (!section || section.hidden) return [...problems, `#view-${view} が出ていない`];
  const rect = section.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) problems.push(`#view-${view} の大きさがない`);
  if ((section.innerText || '').trim().length < 20) problems.push(`#view-${view} に中身がない`);
  for (const other of ['monitor', 'summary', 'settings'].filter((v) => v !== view)) {
    const el = document.querySelector(`#view-${other}`);
    if (el && !el.hidden && el.getBoundingClientRect().height > 0) problems.push(`#view-${other} も出ている`);
  }
  return problems;
}

/** No horizontal page scroll, and nothing sticks out of the screen (outside the boxes that scroll by themselves). */
function overflowProblems() {
  const problems = [];
  const width = window.innerWidth;
  const doc = document.documentElement;
  if (doc.scrollWidth > width + 1) problems.push(`横スクロールが出る（${doc.scrollWidth} > ${width}）`);
  if (document.body.scrollWidth > width + 1) problems.push(`body が横にはみ出す（${document.body.scrollWidth} > ${width}）`);
  const scrolls = (el) => {
    for (let e = el.parentElement; e && e !== document.body; e = e.parentElement) {
      const x = getComputedStyle(e).overflowX;
      if (x === 'auto' || x === 'scroll' || x === 'hidden' || x === 'clip') return true;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.position === 'fixed') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0 || el.closest('dialog:not([open])')) continue;
    if ((rect.right > width + 1 || rect.left < -1) && !scrolls(el)) {
      const name = `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : ''}`;
      problems.push(`画面の外にはみ出す：${name}「${(el.textContent || '').trim().slice(0, 20)}」（${Math.round(rect.left)}〜${Math.round(rect.right)}）`);
      if (problems.length > 10) break;
    }
  }
  return problems;
}

/** Text whose contrast with its background is below WCAG 2.2 (4.5:1, large text 3:1), and the page background. */
function contrastProblems() {
  const parse = (value) => {
    const m = /rgba?\(([^)]+)\)/.exec(value || '');
    if (!m) return null;
    const [r, g, b, a = 1] = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return { r, g, b, a };
  };
  const over = (top, bottom) => ({ r: top.r * top.a + bottom.r * (1 - top.a), g: top.g * top.a + bottom.g * (1 - top.a), b: top.b * top.a + bottom.b * (1 - top.a), a: 1 });
  const background = (el) => {
    const chain = [];
    for (let e = el; e; e = e.parentElement) chain.push(e);
    let result = { r: 255, g: 255, b: 255, a: 1 };
    for (const e of chain.reverse()) {
      const color = parse(getComputedStyle(e).backgroundColor);
      if (color && color.a > 0) result = over(color, result);
    }
    return result;
  };
  const luminance = ({ r, g, b }) => {
    const [R, G, B] = [r, g, b].map((v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * R + 0.7152 * G + 0.0722 * B;
  };
  const ratio = (a, b) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  const problems = [];
  const page = background(document.body);
  if (luminance(page) > 0.2) problems.push(`ダークなのに、画面の背景が明るい（${JSON.stringify(page)}）`);
  let checked = 0;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!el || seen.has(el) || !node.textContent.trim()) continue;
    seen.add(el);
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || rect.width === 0 || rect.height === 0) continue;
    if (el.closest('[hidden], dialog:not([open]), [aria-hidden="true"], :disabled, option, select')) continue;
    if (Number(style.opacity) < 0.5) continue;
    const color = parse(style.color);
    if (!color) continue;
    const bg = background(el);
    const value = ratio(over(color, bg), bg);
    const size = parseFloat(style.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700);
    checked += 1;
    if (value < (large ? 3 : 4.5)) {
      problems.push(`文字が読みにくい（${value.toFixed(2)}:1）：${el.tagName.toLowerCase()}「${node.textContent.trim().slice(0, 24)}」`);
      if (problems.length > 10) break;
    }
  }
  if (checked < 5) problems.push(`確かめた文字が少なすぎる（${checked}）`);
  return problems;
}

// ---- the screen -------------------------------------------------------------------------------------------

// --selftest: the checks above find what they look for (on small pages made here)
if (process.argv[2] === '--selftest') {
  const browser = await playwright.chromium.launch();
  const found = {};
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 600 }, colorScheme: 'dark' });
    const text = '<p>' + '読める文字。'.repeat(3) + '</p>';
    await page.setContent(`<body style="margin:0;background:#111;color:#eee">${text.repeat(6)}<div style="width:900px">広い</div></body>`);
    found.overflow = await page.evaluate(overflowProblems);
    await page.setContent(`<body style="background:#111;color:#333">${text.repeat(6)}</body>`);
    found.lowContrast = await page.evaluate(contrastProblems);
    await page.setContent(`<body style="background:#fff;color:#000">${text.repeat(6)}</body>`);
    found.lightInDark = await page.evaluate(contrastProblems);
    await page.setContent(`<body style="margin:0;background:#111;color:#eee">${text.repeat(6)}<div style="overflow-x:auto"><table style="width:900px"><tr><td>表</td></tr></table></div></body>`);
    found.scrollBoxIsFine = await page.evaluate(overflowProblems);
    await page.setContent(`<body style="background:#111;color:#eee">${text.repeat(6)}</body>`);
    found.goodIsFine = await page.evaluate(contrastProblems);
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(found) + '\n');
  process.exit(0);
}

const url = (hash) => `${config.base}?t=${encodeURIComponent(config.token)}#${hash}`;

async function ready(page, view) {
  await page.waitForSelector(`#view-${view}:not([hidden])`, { timeout: WAIT });
  if (view === 'summary') await page.waitForFunction(() => document.querySelectorAll('#resBody select').length > 0 || document.querySelector('#apDecision'), null, { timeout: WAIT });
  if (view === 'monitor') await page.waitForFunction(() => document.querySelectorAll('#monItems tr').length > 1 || !document.querySelector('#monEmpty')?.hidden, null, { timeout: WAIT });
  if (view === 'settings') await page.waitForFunction((value) => document.querySelector('#f-url')?.value === value, config.settingsUrl, { timeout: WAIT });
  await page.waitForTimeout(150); // the last drawing
}

// The title of an item in the list is its id, for a group with the count: "G1（2 件）"
const titled = (text, id) => text === id || text.startsWith(`${id}（`) || text.startsWith(`${id} `);

async function rowSelect(page, id) {
  const rows = page.locator('#resBody tr').filter({ has: page.locator('button.linkish', { hasText: new RegExp(`^${id}(（| |$)`) }) });
  return rows.first().locator('select');
}

// A response the page will send; never an unhandled rejection (a timeout comes back as an Error)
function nextSave(page) {
  return page.waitForResponse((r) => r.url().includes('/approvals') && r.request().method() === 'POST', { timeout: WAIT })
    .catch((error) => error);
}

async function step(name, fn) {
  const problems = [];
  try {
    await fn(problems);
  } catch (error) {
    problems.push(`途中で止まった：${String(error).split('\n')[0]}`);
  }
  check(name, problems);
}

const browser = await playwright.chromium.launch();
try {
  for (const width of [390, 1280]) {
    for (const scheme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e)));
      page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
      for (const [view, hash] of [['monitor', `run=${config.run}&view=monitor`], ['summary', `run=${config.run}&view=summary`],
        ['settings', 'view=settings'], ['summary', `run=${config.run}&view=summary&item=${config.deviation}`]]) {
        const label = `${view}${hash.includes('item=') ? '（詳細）' : ''} ${width}px ${scheme === 'light' ? 'ライト' : 'ダーク'}`;
        try {
          await page.goto(url(hash));
          await ready(page, view);
        } catch (error) {
          check(`${label}：出る`, [`出ません：${String(error).split('\n')[0]}`]);
          continue;
        }
        check(`${label}：タブが出る`, await page.evaluate(viewProblems, view));
        if (view === 'summary' && !hash.includes('item=')) {
          const shown = await page.$$eval('#resBody button.linkish', (els) => els.map((e) => e.textContent.trim()));
          check(`${label}：作業がすべて並ぶ`, config.rows.filter((id) => !shown.some((s) => titled(s, id))).map((id) => `${id} がない`));
        }
        check(`${label}：横にはみ出さない`, await page.evaluate(overflowProblems));
        if (scheme === 'dark') check(`${label}：ダークで読める`, await page.evaluate(contrastProblems));
      }
      check(`${width}px ${scheme === 'light' ? 'ライト' : 'ダーク'}：画面のエラーがない`, errors);
      await context.close();
    }
  }

  // choosing an approval in the list saves it at once, and it is there after a reload
  {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    await step('承認の選択：一覧で選ぶと保存される', async (problems) => {
      await page.goto(url(`run=${config.run}&view=summary`));
      await ready(page, 'summary');
      const select = await rowSelect(page, config.reject);
      const saved = nextSave(page);
      await select.selectOption('reject', { timeout: WAIT });
      const response = await saved;
      if (response instanceof Error) throw response;
      const body = await response.json().catch(() => ({}));
      if (!response.ok() || body.ok === false) problems.push(`保存の応答が失敗：${response.status()} ${JSON.stringify(body).slice(0, 300)}`);
      await page.waitForFunction(() => /保存しました/.test(document.querySelector('#toast')?.textContent || ''), null, { timeout: WAIT })
        .catch(() => problems.push('「保存しました」が出ない'));
      await page.reload();
      await ready(page, 'summary');
      const value = await (await rowSelect(page, config.reject)).inputValue();
      if (value !== 'reject') problems.push(`読み込み直すと、承認が「${value}」に戻っている`);
    });
    await context.close();
  }

  // editing the deviation comment in the detail saves it with the approval
  {
    const context = await browser.newContext({ viewport: { width: 390, height: 900 } });
    const page = await context.newPage();
    await step('逸脱コメントの手直し：詳細で直して保存すると残る', async (problems) => {
      const hash = `run=${config.run}&view=summary&item=${config.deviation}`;
      await page.goto(url(hash));
      await ready(page, 'summary');
      await page.waitForSelector('#ap-comment', { timeout: WAIT });
      await page.fill('#ap-comment', config.comment);
      if (await page.isDisabled('#apSave')) problems.push('直しても「保存」を押せない');
      if (await page.isHidden('#ap-comment-mark')) problems.push('直しても「AI の案から変更」の印が出ない');
      const saved = nextSave(page);
      await page.click('#apSave', { timeout: WAIT });
      const response = await saved;
      if (response instanceof Error) throw response;
      const body = await response.json().catch(() => ({}));
      if (!response.ok() || body.ok === false) problems.push(`保存の応答が失敗：${response.status()} ${JSON.stringify(body).slice(0, 300)}`);
      await page.waitForFunction(() => /保存しました/.test(document.querySelector('#apMsg')?.textContent || ''), null, { timeout: WAIT })
        .catch(() => problems.push('「保存しました」が出ない'));
      await page.goto(url(`run=${config.run}&view=summary`));
      await ready(page, 'summary');
      await page.goto(url(hash));
      await page.waitForSelector('#ap-comment', { timeout: WAIT });
      const value = await page.inputValue('#ap-comment');
      if (value !== config.comment) problems.push(`読み込み直すと、逸脱コメントが「${value.slice(0, 40)}」になっている`);
    });
    await context.close();
  }
} catch (error) {
  report.fatal = String(error && error.stack ? error.stack : error).slice(0, 2000);
} finally {
  await browser.close();
  fs.writeFileSync(config.out, JSON.stringify(report, null, 1));
}
