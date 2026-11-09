// Checks that run inside the browser (functions passed to page.evaluate; they use no outside variables).
// Ported from the original plugin's screen tests.
//   layoutProblems()           horizontal scroll, overflow, overlap
//   contrastProblems(minimum)  text whose contrast with its background is below the minimum (WCAG 2.2: 4.5:1)

/** 崩れの検査：横スクロール、はみ出し、重なり。表の入れ物（.table-wrap）の中は、中で動かせるので除く。 */
export function layoutProblems() {
  const problems = [];
  const describe = (el) => `${el.tagName.toLowerCase()}${el.className ? `.${String(el.className).split(' ')[0]}` : ''}「${(el.textContent ?? '').trim().slice(0, 20)}」`;
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  if (document.documentElement.scrollWidth > window.innerWidth + 1) problems.push(`横スクロールが出る（${document.documentElement.scrollWidth} > ${window.innerWidth}）`);
  const all = [...document.querySelectorAll('body *')].filter(visible).filter((el) => !el.closest('.table-wrap'));
  for (const el of all) {
    const rect = el.getBoundingClientRect();
    if (rect.right > window.innerWidth + 1 || rect.left < -1) problems.push(`画面の外にはみ出す：${describe(el)}`);
    const parent = el.parentElement;
    if (parent && parent !== document.body && parent !== document.documentElement) {
      const parentRect = parent.getBoundingClientRect();
      if (rect.left < parentRect.left - 1 || rect.right > parentRect.right + 1) problems.push(`入れ物の外にはみ出す：${describe(el)} in ${describe(parent)}`);
    }
    const style = getComputedStyle(el);
    if (style.display !== 'inline' && style.overflowX !== 'visible' && el.scrollWidth > el.clientWidth + 1) problems.push(`文字が切れる：${describe(el)}`);
  }
  // 重なり：同じ入れ物の中の、流れに乗っている兄弟どうし
  const flowing = (el) => {
    const style = getComputedStyle(el);
    return !['inline', 'contents'].includes(style.display) && ['static', 'relative'].includes(style.position);
  };
  const parents = new Set(all.map((el) => el.parentElement).filter(Boolean));
  for (const parent of parents) {
    const children = [...parent.children].filter((el) => all.includes(el) && flowing(el));
    for (let i = 0; i < children.length; i++) {
      for (let j = i + 1; j < children.length; j++) {
        const a = children[i].getBoundingClientRect();
        const b = children[j].getBoundingClientRect();
        const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (width > 2 && height > 2) problems.push(`重なる：${describe(children[i])} と ${describe(children[j])}`);
      }
    }
  }
  return problems;
}

/** 文字と背景の色の差（WCAG 2.2 の 4.5:1）が足りない文字を返す。 */
export function contrastProblems(minimum) {
  const parse = (value) => {
    const match = /rgba?\(([^)]+)\)/.exec(value);
    if (!match) return null;
    const [r, g, b, a = 1] = match[1].split(/[\s,/]+/).filter(Boolean).map(Number);
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
  let checked = 0;
  for (const el of document.querySelectorAll('body *')) {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || rect.width === 0 || rect.height === 0) continue;
    const hasText = [...el.childNodes].some((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim() !== '');
    const isField = ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) && el.type !== 'checkbox';
    if (!hasText && !isField) continue;
    const bg = background(el);
    const fg = parse(style.color);
    if (!fg) {
      problems.push(`色を読めない：${el.tagName}`);
      continue;
    }
    checked += 1;
    const value = ratio(over(fg, bg), bg);
    if (value < minimum) problems.push(`${value.toFixed(2)}:1 ${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]}「${(el.textContent ?? '').trim().slice(0, 20)}」`);
  }
  return { problems, checked };
}
