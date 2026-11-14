/*
 * 説明サイトの表示を補助するスクリプト。
 * Material の instant navigation では、ページを再読み込みせずに本文だけを差し替えるため、
 * 処理はページを表示するたびに通知される document$ の中で、表示中の本文に対して行う。
 */

// ---------- コードブロックの種類 ----------
// コードブロックの題（title="…"）の文から種類を判定し、ブロックに data-ctk-kind 属性を付ける。
// CSS が、種類ごとに色の線、題の色、アイコンを付ける。
//   「チャットに入力」→ chat（青緑・吹き出し）
//   「ターミナル」　　→ term（灰色・ターミナル）
//   「表示の例」　　　→ output（薄い灰色・モニター。長い行は折り返す）
const KINDS = [["チャットに入力", "chat"], ["ターミナル", "term"], ["表示の例", "output"]];

function markCodeTitles(root) {
  root.querySelectorAll(".highlight > span.filename").forEach((title) => {
    const found = KINDS.find(([word]) => title.textContent.trim().startsWith(word));
    if (found) title.parentElement.dataset.ctkKind = found[1];
  });
}

// ---------- 「選択」列の色付け ----------
// 見出しが「選択」の列で、セルの文が3つの選択肢のどれかと同じとき、色付きのラベルに置き換える。
// 修正を取り込む → 緑、逸脱コメントを登録する → 青、保留にする → 黄（色は CSS の .ctk-choice--*）
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

// ---------- トラブルシューティングの絞り込み ----------
// <div class="ctk-filter"> の位置に入力欄を作り、入力した文で、ページ内の表の行を絞り込む。
// 比較の前に、全角と半角、大文字と小文字、空白の違いをなくす。
const normalize = (text) => text.normalize("NFKC").toLowerCase().replace(/\s+/g, "");

function setUpFilters(root) {
  root.querySelectorAll(".ctk-filter").forEach((box) => {
    // 同じページを表示し直したときに、入力欄を二重に作らない
    if (box.dataset.ready) return;
    box.dataset.ready = "1";

    // 入力欄と、その右に出す件数
    const input = Object.assign(document.createElement("input"), {
      type: "search",
      placeholder: box.dataset.placeholder || "絞り込む",
      ariaLabel: "表の行を絞り込む",
    });
    const count = document.createElement("output");
    box.append(input, count);

    // 行ごとに、比較用の文と、左の列を「…」で区切った断片（4文字以上）を先に作っておく。
    // 断片があるのは、表示されたメッセージ全体を貼り付けても当たるようにするため。
    // 例：「ストリーム … が見つかりません」の行は、「ストリーム app-main が見つかりません。…」を貼り付けると当たる
    const page = box.closest("article") || document;
    const rows = [...page.querySelectorAll("table tbody tr")].map((row) => {
      const first = row.children[0] ? row.children[0].textContent : "";
      return {
        row,
        text: normalize(row.textContent),
        pieces: first.split(/…|\.\.\./).map(normalize).filter((piece) => piece.length >= 4),
      };
    });

    // 当てはまる行が1つもないときに、入力欄の下に出す文
    const empty = Object.assign(document.createElement("p"), {
      className: "ctk-filter-empty",
      textContent: "当てはまる行がありません。メッセージの別の部分を入れてください。",
      hidden: true,
    });
    box.after(empty);

    // 入力のたびに、行を表示するか隠すかを決める
    input.addEventListener("input", () => {
      const query = normalize(input.value);
      let shown = 0;
      rows.forEach(({ row, text, pieces }) => {
        const hit = !query || text.includes(query) || pieces.some((piece) => query.includes(piece));
        row.hidden = !hit;
        shown += hit ? 1 : 0;
      });
      // 行が1つも残らない表は、表とその直前の見出しをまとめて隠す
      page.querySelectorAll("table").forEach((table) => {
        const block = table.closest(".md-typeset__scrollwrap") || table;
        const visible = [...table.querySelectorAll("tbody tr")].some((row) => !row.hidden);
        block.hidden = !visible;
        let heading = block.previousElementSibling;
        while (heading && !/^H[2-6]$/.test(heading.tagName)) heading = heading.previousElementSibling;
        if (heading) heading.hidden = !visible;
      });
      // 件数と、当てはまる行がないときの文（入力が空のときは、どちらも出さない）
      count.textContent = query ? `${shown} 件` : "";
      empty.hidden = !query || shown > 0;
    });
  });
}

// ページを表示するたびに、表示中の本文（.md-content）に対して上の3つを実行する
document$.subscribe(() => {
  const root = document.querySelector(".md-content") || document;
  markCodeTitles(root);
  markChoices(root);
  setUpFilters(root);
});
