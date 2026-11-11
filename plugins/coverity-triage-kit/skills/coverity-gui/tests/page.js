// Drives the page in Chromium: node page.js <url> <screenshot.png> <comment>
// CID 20001 has a fix, CID 20002 has none. Prints one JSON object with what the page showed.
const { chromium } = require('playwright');

(async () => {
  const [url, shot, comment] = process.argv.slice(2);
  const browser = await chromium.launch();
  const seen = { errors: [] };
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
    page.on('pageerror', (error) => seen.errors.push(String(error)));
    page.on('console', (message) => message.type() === 'error' && seen.errors.push(message.text()));
    await page.goto(url);
    await page.waitForSelector('#cid-20002 select');
    seen.table = await page.locator('tbody select').evaluateAll((picks) => picks.map((pick) => {
      const tr = pick.closest('tr');
      return { cid: tr.parentElement.id.slice(4), fix: tr.querySelector('.fix').innerText,
        deviation: tr.querySelector('.deviation').innerText, choice: pick.value };
    }));

    const first = page.locator('#cid-20001');
    await first.getByRole('button', { name: '20001' }).click(); // the opened row shows the diff
    seen.diff = (await first.locator('.diff .add').first().innerText()).includes('確認'); // cp932 in the patch
    await first.locator('select').selectOption({ label: '何もしない' }); // a choice is saved at once
    await first.getByText('画面で選択').waitFor();

    const second = page.locator('#cid-20002');
    await second.getByRole('button', { name: '20002' }).click();
    await second.locator('textarea').fill(comment);
    seen.counter = await second.locator('.tools span').innerText();
    await second.getByRole('button', { name: 'コメントを保存' }).click();
    await second.getByText('編集済み').waitFor();
    await page.getByText('何もしない 1 件').waitFor();
    seen.foot = await page.locator('#choices').innerText();
    await page.screenshot({ path: shot });

    await page.setViewportSize({ width: 390, height: 800 });
    seen.narrow = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, window: innerWidth }));
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(seen));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
