// Web管理画面。記録から出す文字は、すべて textContent で表示する（HTML として扱わない）。
"use strict";

(() => {
  const TOKEN_KEY = "debug-dashboard-token";
  const POLL_MS = 2000;          // 記録を読み直す間隔
  const SAVE_DELAY_MS = 600;     // 設定を打ち終えてから保存するまでの間
  const MAX_TRIALS = 20;
  const VERIFIER = "debug-verifier";
  const $ = (id) => document.getElementById(id);

  const token = takeToken();
  let selected = null;    // 表示している実行（チャット）
  let shown = null;       // 表示している実行の要約
  let follow = true;      // 統括エージェントの新しい実行を表示し続けるか
  let shownKey = "";      // 表示中の内容。変わったときだけ描き直す
  let listKey = "";
  let settingsFilled = false;
  let saveTimer = null;
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

  function buildRequest() {
    const goal = $("goal").value.trim();
    if (!goal) return "";
    const criteria = $("criteria").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    const trials = parseInt($("trials").value, 10);
    const parts = ["直したいこと：\n" + goal];
    if (criteria.length) parts.push("合格の条件：\n" + criteria.map((c) => "- " + c).join("\n"));
    if (trials >= 1 && trials <= MAX_TRIALS) parts.push("試行の上限：" + trials + "回");
    return parts.join("\n\n");
  }

  function updateRequest() {
    const text = buildRequest();
    $("request").value = text;
    $("request").hidden = !text;
    $("request-hint").hidden = Boolean(text);
    $("copy").disabled = !text;
    $("copy-result").textContent = "";
  }

  function fillSettings(settings) {
    settingsFilled = true;
    $("goal").value = settings.goal || "";
    $("criteria").value = settings.criteria || "";
    $("trials").value = settings.max_trials || 5;
    updateRequest();
  }

  async function saveSettings() {
    const trials = Number($("trials").value);
    if (!Number.isInteger(trials) || trials < 1 || trials > MAX_TRIALS) {
      $("settings-result").textContent = "試行の上限は、1 から " + MAX_TRIALS + " までの数にしてください。";
      return;
    }
    try {
      await api("/api/settings", { goal: $("goal").value, criteria: $("criteria").value, max_trials: trials });
      $("settings-result").textContent = "保存しました。";
    } catch (e) {
      $("settings-result").textContent = e.message;
    }
  }

  async function copyRequest() {
    const text = $("request").value;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      $("request").select();
      if (!document.execCommand("copy")) {
        $("copy-result").textContent = "コピーできませんでした。依頼文を選んでコピーしてください。";
        return;
      }
    }
    $("copy-result").textContent = "コピーしました。";
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

  // 追う実行：エージェントに依頼したチャットのうち、いちばん新しいもの。なければ、いちばん新しいチャット。
  // 画面を起動したチャットなど、エージェントを使わないチャットに移らないようにする
  function latest(sessions) {
    const run = sessions.find((s) => s.requests > 0) || sessions[0];
    return run ? run.id : null;
  }

  function render(detail) {
    const s = detail ? detail.session : null;
    shown = s;
    const key = s ? [s.id, s.count, s.stage, s.stopped].join("|") : "";
    if (key === shownKey) return;
    shownKey = key;

    $("progress-title").textContent = title(s);
    $("results-title").textContent = title(s);
    for (const step of document.querySelectorAll(".stages li")) {
      const on = Boolean(s) && step.dataset.stage === s.stage;
      step.classList.toggle("current", on);
      if (on) step.setAttribute("aria-current", "step");
      else step.removeAttribute("aria-current");
    }
    $("running").textContent = !s ? "" : s.running.length
      ? "動いているエージェント：" + s.running.join("、")
      : "動いているエージェントはありません。";
    $("stop").disabled = !s || s.stopped;
    $("stop-result").textContent = s && s.stopped
      ? "止めています。次のツールの呼び出しから断ります。このチャットで次の依頼を送ると解けます。"
      : "";

    const events = detail ? detail.events : [];
    $("messages").replaceChildren(...events.filter((e) => e.kind !== "tool").map(messageItem));
    $("tools").replaceChildren(...events.filter((e) => e.kind === "tool").reverse().map(toolItem));
    $("last-report").textContent = (s && s.last_report) || "まだ報告がありません。";
    $("last-verdict").textContent = (s && s.last_verdict) || "まだ判定がありません。";
  }

  function renderSessions(sessions) {
    const key = selected + "|" + sessions.map((s) => s.id + s.updated + s.stage).join(",");
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
    if (!settingsFilled) fillSettings(state.settings || {});
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
    tab.addEventListener("click", () => showTab(tab.id.slice("tab-".length)));
  }
  for (const id of ["goal", "criteria", "trials"]) {
    $(id).addEventListener("input", () => {
      updateRequest();
      $("settings-result").textContent = "";
      clearTimeout(saveTimer);
      saveTimer = setTimeout(saveSettings, SAVE_DELAY_MS);
    });
  }
  $("copy").addEventListener("click", copyRequest);
  $("stop").addEventListener("click", stopRun);
  updateRequest();
  tick();
  setInterval(tick, POLL_MS);
})();
