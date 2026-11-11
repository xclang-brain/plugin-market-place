// Web管理画面。記録から出す文字は、すべて textContent で表示する（HTML として扱わない）。
"use strict";

(() => {
  const TOKEN_KEY = "debug-dashboard-token";
  const POLL_MS = 2000;          // 記録を読み直す間隔
  const VERIFIER = "debug-verifier";
  const $ = (id) => document.getElementById(id);

  const token = takeToken();
  let selected = null;    // 表示しているセッション
  let shown = null;       // 表示しているセッションの要約
  let follow = true;      // 統括エージェントの新しいセッションを表示し続けるか
  let shownKey = "";      // 表示中の内容。変わったときだけ描き直す
  let listKey = "";
  let busy = false;

  // URL の #k=<トークン> を sessionStorage に移し、アドレスバーから消す
  function takeToken() {
    const match = /^#k=([A-Za-z0-9_-]+)$/.exec(location.hash);
    let value = match ? match[1] : "";
    try {
      if (value) sessionStorage.setItem(TOKEN_KEY, value);
      else value = sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (e) {
      // sessionStorage が使えなくても、このページを開いている間は使える
    }
    if (match) history.replaceState(null, "", location.pathname);
    return value;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function notice(text) {
    $("notice").textContent = text || "";
    $("notice").hidden = !text;
  }

  // "2026-11-09T14:03:21+09:00" → "11/09 14:03:21"
  function when(time, withDate) {
    if (!time) return "";
    const clock = time.slice(11, 19);
    return withDate ? time.slice(5, 10).replace("-", "/") + " " + clock.slice(0, 5) : clock;
  }

  async function api(path, body) {
    const options = { headers: { "X-Dashboard-Token": token }, cache: "no-store" };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (e) {
      throw new Error("画面のサーバーにつながりません。終了したかもしれません。スキルで起動し直してください。");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || "サーバーがエラーを返しました（" + response.status + "）。");
    return data;
  }

  function showTab(name) {
    for (const tab of document.querySelectorAll('[role="tab"]')) {
      const on = tab.id === "tab-" + name;
      tab.setAttribute("aria-selected", String(on));
      $(tab.getAttribute("aria-controls")).hidden = !on;
    }
  }

  // ---- 設定 ----
  // 項目は、ここには書かない。サーバーが設定ファイルのコメントから読んだものを、そのまま入力欄にする。

  function itemInput(skill, item) {
    let input;
    if (item.choices && item.choices.length) {
      input = el("select");
      for (const choice of [""].concat(item.choices)) input.appendChild(el("option", "", choice));
      input.value = item.value || "";
    } else if (Array.isArray(item.value) || /1行に1つ/.test(item.comment || "")) {
      // 値を2つ以上書ける項目（今の値がリストか、コメントに「1行に1つ」とある）
      const values = [].concat(item.value || []);
      input = el("textarea");
      input.rows = Math.max(2, values.length + 1);
      input.value = values.join("\n");
    } else {
      input = el("input");
      input.type = "text";
      input.value = item.value || "";
      if (item.examples && item.examples.length) input.placeholder = "例：" + item.examples.join("、");
    }
    input.id = "skill-" + skill + "-" + item.key;
    return input;
  }

  function renderSkillItem(skill, item, missing) {
    const row = el("div", "item");
    const label = el("label", "", item.key + (item.required ? "（必須）" : ""));
    row.appendChild(label);
    if (item.comment) row.appendChild(el("p", "hint", item.comment));
    if (item.secret) {
      const how = item.env_key ? "下の " + item.env_key + " に、パスワードを入れた環境変数の名前を入れてください。"
        : "設定ファイルに、自分で書いてください。";
      row.appendChild(el("p", "hint", "この画面では扱いません。" + how));
      return row;
    }
    const input = itemInput(skill, item);
    const result = el("span", "hint", missing ? "まだ空です。" : "");
    label.htmlFor = input.id;
    input.addEventListener("change", async () => {
      const value = input.tagName === "TEXTAREA" ? input.value.split(/\r?\n/) : input.value;
      try {
        await api("/api/config", { skill: skill, key: item.key, value: value });
        result.textContent = "設定ファイルに書きました。";
      } catch (e) {
        result.textContent = e.message;
      }
    });
    row.appendChild(input);
    row.appendChild(result);
    return row;
  }

  async function loadSkills() {
    const box = $("skills");
    $("skills-result").textContent = "読んでいます。";
    let data;
    try {
      data = await api("/api/config");
    } catch (e) {
      $("skills-result").textContent = e.message;
      return;
    }
    box.textContent = "";
    for (const skill of data.skills || []) {
      const group = el("fieldset");
      group.appendChild(el("legend", "", skill.name));
      if (skill.config) group.appendChild(el("p", "hint", skill.config));
      if (!skill.ok) group.appendChild(el("p", "hint", skill.message));
      for (const item of skill.items) {
        group.appendChild(renderSkillItem(skill.name, item, skill.missing.indexOf(item.key) >= 0));
      }
      box.appendChild(group);
    }
    $("skills-result").textContent = (data.skills || []).length ? ""
      : "設定ファイルがありません。CLI で「スキルを使う準備をして」と依頼してください。";
  }

  // ---- 進み具合と結果 ----

  function speaker(e) {
    return e.agent ? e.agent + " " : "メインの会話";
  }

  function messageLabel(e) {
    if (e.kind === "prompt") return "利用者の依頼";
    if (e.kind === "request") return speaker(e) + "への依頼" + (e.denied ? "（止めたので断りました）" : "");
    if (e.kind === "report") return speaker(e) + ((e.agent || "").endsWith(VERIFIER) ? "の判定" : "の報告");
    return "お知らせ";
  }

  function messageItem(e) {
    const item = el("li", "message " + e.kind);
    const head = el("p", "head");
    head.append(el("time", "", when(e.time)), el("span", "who", messageLabel(e)));
    item.append(head, el("pre", "text", e.text || "（本文がありません）"));
    return item;
  }

  function toolItem(e) {
    const item = el("li", e.denied ? "tool denied" : "tool");
    const head = el("p", "head");
    head.append(el("time", "", when(e.time)), el("span", "who", e.agent || "メインの会話"),
      el("strong", "", e.tool || "?"));
    if (e.denied) head.append(el("span", "badge", "断りました"));
    item.append(head);
    if (e.input) item.append(el("pre", "input", e.input));
    return item;
  }

  function title(session) {
    if (!session) return "まだ記録がありません";
    return (session.title || "（依頼の記録がありません）") + "（" + when(session.started, true) + " から）";
  }

  // 追うセッション：いちばん新しく動いたセッション
  function latest(sessions) {
    return sessions.length ? sessions[0].id : null;
  }

  function render(detail) {
    const s = detail ? detail.session : null;
    shown = s;
    const key = s ? [s.id, s.count, s.running.join(","), s.stopped].join("|") : "";
    if (key === shownKey) return;
    shownKey = key;

    $("progress-title").textContent = title(s);
    $("results-title").textContent = title(s);
    $("running").textContent = !s ? "" : s.running.length
      ? "動いているエージェント：" + s.running.join("、")
      : "動いているエージェントはありません。";
    $("stop").disabled = !s || s.stopped;
    $("stop-result").textContent = s && s.stopped
      ? "止めています。次のツールの呼び出しから断ります。このセッションで次の依頼を送ると解除されます。"
      : "";

    const events = detail ? detail.events : [];
    $("messages").replaceChildren(...events.filter((e) => e.kind !== "tool").map(messageItem));
    $("tools").replaceChildren(...events.filter((e) => e.kind === "tool").reverse().map(toolItem));
    $("last-report").textContent = (s && s.last_report) || "まだ報告がありません。";
    $("verdict").hidden = !(s && s.last_verdict);
    $("last-verdict").textContent = (s && s.last_verdict) || "";
  }

  function renderSessions(sessions) {
    const key = selected + "|" + sessions.map((s) => s.id + s.updated).join(",");
    if (key === listKey) return;
    listKey = key;
    const items = sessions.map((s) => {
      const button = el("button", s.id === selected ? "session current" : "session");
      button.type = "button";
      button.append(el("span", "title", s.title || "（依頼の記録がありません）"),
        el("span", "meta", when(s.started, true) + " 〜 " + when(s.updated, true) + "・" + s.count + " 件"));
      if (s.id === selected) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => {
        selected = s.id;
        follow = s.id === latest(sessions);
        shownKey = listKey = "";
        tick();
      });
      const item = el("li");
      item.append(button);
      return item;
    });
    if (!items.length) items.push(el("li", "hint", "まだ記録がありません。"));
    $("sessions").replaceChildren(...items);
  }

  async function stopRun() {
    if (!shown) return;
    const id = shown.id;
    const name = "「" + (shown.title || "（依頼の記録がありません）") + "」（" + when(shown.started, true) + " から）";
    if (!confirm(name + "を止めますか？\n次のツールの呼び出しから断り、作業を終えるよう伝えます。")) return;
    try {
      const result = await api("/api/stop", { session: id });
      $("stop-result").textContent = result.message;
      shownKey = "";
      tick();
    } catch (e) {
      notice(e.message);
    }
  }

  async function refresh() {
    if (!token) {
      notice("URL にトークンがありません。スキルが返した URL を開き直してください。");
      return;
    }
    const state = await api("/api/state");
    $("workdir").textContent = state.workdir || "―";
    const sessions = state.sessions || [];
    if (follow || !sessions.some((s) => s.id === selected)) {
      selected = latest(sessions);
      follow = true;
    }
    renderSessions(sessions);
    render(selected ? await api("/api/sessions/" + encodeURIComponent(selected)) : null);
    notice("");
  }

  async function tick() {
    if (busy) return;
    busy = true;
    try {
      await refresh();
    } catch (e) {
      notice(e.message);
    } finally {
      busy = false;
    }
  }

  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener("click", () => {
      showTab(tab.id.slice("tab-".length));
      if (tab.id === "tab-settings") loadSkills();
    });
  }
  $("skills-reload").addEventListener("click", loadSkills);
  $("stop").addEventListener("click", stopRun);
  if (token) loadSkills();
  tick();
  setInterval(tick, POLL_MS);
})();
