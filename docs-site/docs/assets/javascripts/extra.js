/* ページごとに実行します（Material の instant navigation では document$ がページの切り替えを知らせます）。 */

// コードの見出しの文から、種類を付けます（CSS がアイコンと色を付けます）
const KINDS = [["チャットに入力", "chat"], ["ターミナル", "term"], ["表示の例", "output"]];

function markCodeTitles(root) {
  root.querySelectorAll(".highlight > span.filename").forEach((title) => {
    const found = KINDS.find(([word]) => title.textContent.trim().startsWith(word));
    if (found) title.parentElement.dataset.ctkKind = found[1];
  });
}

// 見出しが「選択」の列の、3つの選択に色を付けます
const CHOICES = { "修正を取り込む": "fix", "逸脱コメントを登録する": "deviation", "保留にする": "hold" };

function markChoices(root) {
  root.querySelectorAll(".md-typeset table").forEach((table) => {
    const heads = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
    const column = heads.indexOf("選択");
    if (column < 0) return;
    table.querySelectorAll("tbody tr").forEach((row) => {
      const cell = row.children[column];
      const kind = cell && CHOICES[cell.textContent.trim()];
      if (kind && !cell.querySelector(".ctk-choice")) {
        cell.innerHTML = `<span class="ctk-choice ctk-choice--${kind}">${cell.textContent.trim()}</span>`;
      }
    });
  });
}

// 困ったとき：入力した文で、表の行を絞り込みます。表示されたメッセージをそのまま貼り付けても当たるように、
// 左の列の「…」で区切った断片が入力に含まれる行も残します
const normalize = (text) => text.normalize("NFKC").toLowerCase().replace(/\s+/g, "");

function setUpFilters(root) {
  root.querySelectorAll(".ctk-filter").forEach((box) => {
    if (box.dataset.ready) return;
    box.dataset.ready = "1";
    const input = Object.assign(document.createElement("input"), {
      type: "search",
      placeholder: box.dataset.placeholder || "絞り込む",
      ariaLabel: "表の行を絞り込む",
    });
    const count = document.createElement("output");
    box.append(input, count);

    const page = box.closest("article") || document;
    const rows = [...page.querySelectorAll("table tbody tr")].map((row) => {
      const first = row.children[0] ? row.children[0].textContent : "";
      return {
        row,
        text: normalize(row.textContent),
        pieces: first.split(/…|\.\.\./).map(normalize).filter((piece) => piece.length >= 4),
      };
    });
    const empty = Object.assign(document.createElement("p"), {
      className: "ctk-filter-empty",
      textContent: "当てはまる行がありません。メッセージの別の部分を入れてください。",
      hidden: true,
    });
    box.after(empty);

    input.addEventListener("input", () => {
      const query = normalize(input.value);
      let shown = 0;
      rows.forEach(({ row, text, pieces }) => {
        const hit = !query || text.includes(query) || pieces.some((piece) => query.includes(piece));
        row.hidden = !hit;
        shown += hit ? 1 : 0;
      });
      // 行が残らない表と、その見出しを隠します
      page.querySelectorAll("table").forEach((table) => {
        const block = table.closest(".md-typeset__scrollwrap") || table;
        const visible = [...table.querySelectorAll("tbody tr")].some((row) => !row.hidden);
        block.hidden = !visible;
        let heading = block.previousElementSibling;
        while (heading && !/^H[2-6]$/.test(heading.tagName)) heading = heading.previousElementSibling;
        if (heading) heading.hidden = !visible;
      });
      count.textContent = query ? `${shown} 件` : "";
      empty.hidden = !query || shown > 0;
    });
  });
}

document$.subscribe(() => {
  const root = document.querySelector(".md-content") || document;
  markCodeTitles(root);
  markChoices(root);
  setUpFilters(root);
});
