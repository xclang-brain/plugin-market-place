"""通しの試験：/coverity-run と /coverity-apply を、本物の部品で最初から最後まで通す（REQ-101〜108）。

モデルの代わりに、試験が決まった手順で統括（skills/coverity-run/SKILL.md の手順）と、取得・作業・統合・検証の
エージェントの役をする（e2e_flow_support.py の Orchestrator と Actors）。作業の役は、指示ファイル brief.md を読み、
正解の記録（tests/fixtures/envmon-truth.yaml）にもとづいて、修正用のコピーを本当に直し、result.json を書く。

本物の部品：Coverity Connect のエミュレーター（coverity_emulator.server）、題材の登録（coverity_emulator.seed）、
模擬の解析コマンド（coverity_emulator/bin。本物の静的解析で判定する）、MCP サーバー（mcp.json と同じ
`uv run --native-tls --frozen --quiet --directory server coverity-triage-mcp` を stdio で動かす）、ct.py
（`uv run --native-tls ct.py`）、ct.py gui（画面のサーバー）、git と svn。

動かし方：
  - python3 tests/run.py が動かす（この試験は標準のライブラリだけを使う。依存のない Python でもよい）。
  - 道具（uv、git、svn、make、gcc、g++、clang、cppcheck）が足りないときは、理由を示して skip する。
  - 全部で 4〜5 分かかる（10 分を超えないので、速い組と全部の組には分けていない）。
  - 環境変数 CWT_CAPTURE_DIR があると、画面の写真（png、deviceScaleFactor 2）をそこに撮る（Playwright が要る）。
  - プラグインの中には何も作らない。一時のフォルダは試験の終わりに消す。所要時間は試験の終わりに標準エラーに出し、
    CWT_CAPTURE_DIR があれば e2e-timings.json にも書く。
"""
from __future__ import annotations

import json
import re
import sys
import traceback
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.dont_write_bytecode = True  # 道具のモジュールの __pycache__ をプラグインの中に作らない
sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_flow_support import (FRAME_MAX_CIDS, KEY, TIMINGS, USER, Actors, Brief, CtResult,  # noqa: E402
                              Orchestrator, Session, Stage, Template, World, capture, capture_dir, file_url,
                              gui_call, missing_tools, run)

MISSING = missing_tools()
MAIN = "envmon-main"
RESULT_KEYS = ["item", "verdict", "recommendation", "confidence", "confidence_reason", "deviation", "fix",
               "revision_drift", "group_excluded_cids"]
ROW = re.compile(r'<tr><td data-label="承認"><span class="badge (\w+)">[^<]*</span>(?:<span[^>]*>[^<]*</span>)*</td>'
                 r'<td data-label="ID" class="id">'
                 r'<a href="cid/([^"]+)\.html">.*?<span class="badge conf-(\w+)">', re.S)


def setUpModule() -> None:
    if MISSING:
        raise unittest.SkipTest(f"通しの試験の道具が足りません: {', '.join(MISSING)}")
    Template.get()


def tearDownModule() -> None:
    try:
        sys.stderr.write("\n" + TIMINGS.text() + "\n")
        folder = capture_dir()
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "e2e-timings.json").write_text(json.dumps(
                {"rows": TIMINGS.rows, "by_scene": TIMINGS.by_scene(), "total_seconds": TIMINGS.total()},
                ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        Session.cleanup()


def summary_rows(path: Path) -> Dict[str, Dict[str, str]]:
    """summary.html の表：ID → {approval, confidence}。"""
    return {m.group(2): {"approval": m.group(1), "confidence": m.group(3)}
            for m in ROW.finditer(path.read_text(encoding="utf-8"))}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def expected(world: World, stream: str, limit: int, checkers: Optional[List[str]] = None,
             impacts: Optional[List[str]] = None, classification: Optional[List[str]] = None,
             action: Optional[List[str]] = None, status: Optional[List[str]] = None,
             scope_latest: bool = True) -> List[int]:
    """エミュレーターの状態から求めた、条件に合う CID（CID の順に limit 件まで）。"""
    import fnmatch
    table = world.emu.table(stream)
    latest = set(world.emu.latest_cids(stream))
    found = []
    for cid, row in sorted(table.items()):
        if scope_latest and cid not in latest:
            continue
        if checkers and not any(fnmatch.fnmatchcase(row["checker"], p) for p in checkers):
            continue
        if impacts and row["impact"] not in impacts:
            continue
        if classification and row["classification"] not in classification:
            continue
        if action and row["action"] not in action:
            continue
        if status and row["status"] not in status:
            continue
        found.append(cid)
    return found[:limit]


def search_to_file(world: World, run: Dict[str, Any], calls: Optional[List[Any]] = None) -> Any:
    """取得のエージェントと同じく、search_issues を done になるまで同じ引数で呼ぶ。"""
    args = {"repo_root": run["repo_root"], "filter_file": run["filter_file"], "output_file": run["issues_file"]}
    result = None
    for _ in range(10):
        result = world.mcp().call("search_issues", args)
        if calls is not None:
            calls.append(result)
        if not result.ok or result["done"]:
            return result
    return result


class FlowCase(unittest.TestCase):
    """setUpClass で場面を最後まで動かし、段ごとの結果を rec に残す。試験の関数は、残した結果を確かめる。

    1つの段が失敗すると、その段を使う試験は、その段の誤りを理由に不合格になる（ほかの段の試験は動く）。"""

    scene = ""
    world: World
    rec: Dict[str, Any]
    errors: Dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.rec, cls.errors = {}, {}
        try:
            cls.flow()
        except Exception:  # noqa: BLE001 - 段の外の失敗も、試験で示す
            cls.errors["flow"] = traceback.format_exc()[-4000:]

    @classmethod
    def tearDownClass(cls) -> None:
        world = getattr(cls, "world", None)
        if world is not None:
            world.close()

    @classmethod
    def flow(cls) -> None:
        raise NotImplementedError

    @classmethod
    def step(cls, name: str, func: Callable[[], Any], *needs: str, stage: Optional[str] = None) -> Any:
        """段を1つ動かし、結果を rec[name] に残す。needs の段が失敗していれば動かさない。"""
        failed = [n for n in needs if n not in cls.rec]
        if failed:
            cls.errors[name] = f"前の段 {', '.join(failed)} が動かなかったため、動かしていません"
            return None
        try:
            if stage:
                with Stage(cls.scene, stage):
                    cls.rec[name] = func()
            else:
                cls.rec[name] = func()
        except Exception:  # noqa: BLE001
            cls.errors[name] = traceback.format_exc()[-4000:]
            return None
        return cls.rec[name]

    def need(self, *names: str) -> Any:
        for name in names:
            if name not in self.rec:
                why = self.errors.get(name) or self.errors.get("flow") or "動いていません"
                self.fail(f"段 {name} の結果がありません: {why}")
        return self.rec[names[-1]] if names else None


def gui_open(world: World, run_dir: str, view: str) -> CtResult:
    return world.ct("gui", "--repo", str(world.repo), "--run", run_dir, "--view", view, "--no-browser")


def gui_close(url: str) -> None:
    try:
        gui_call(url, "POST", "/api/close", {})
    except Exception:  # noqa: BLE001 - 止まっていればよい
        pass


# =================================================================================================
# 1. git・統合と検証あり・ビルドと再解析での検証
# =================================================================================================

FILTER_A = ("name: e2e\nproject: envmon\nstreams: [envmon-main]\n"
            "checkers: [DIVIDE_BY_ZERO, OVERRUN, RESOURCE_LEAK, STRING_OVERFLOW, 'UNUSED_*', NULL_RETURNS]\nlimit: 20\n")
REAL_FIXES_A = {"10014", "10027", "10041", "10054", "10109", "10158", "10220", "G2"}  # 本当に直した修正の推奨
DEVIATIONS_A = {"10017", "10162", "10172", "10243"}
ALREADY_FIXED = {"10025", "10131"}  # envmon-latest.patch で最新では直っている


def reviews_a(item: str, round_: int) -> Optional[Dict[str, Any]]:
    if item == "10243":
        return {"outcome": "flag", "cap": "medium", "note": "呼び出し元の一部を追いきれていない"}
    if item == "10158" and round_ == 1:
        return {"outcome": "rework", "rework": ["範囲の確かめを変えた影響（呼び出し元）を fix.impact に書く"],
                "note": "修正の影響の書き方が足りない"}
    return None


class GitReviewedFlowTest(FlowCase):
    """統合と検証あり（agents.integration・agents.review が true）、build+analyze で検証、git に反映する。"""

    scene = "git・検証あり"

    @classmethod
    def flow(cls) -> None:
        cls.world = w = World("git-review")
        cls.step("setup", lambda: w.setup(MAIN, agents={"integration": True, "review": True}))
        w.write_filter("e2e.yaml", FILTER_A)
        cls.step("before", w.fingerprint)
        cls.step("new_run", lambda: w.ct("new-run", "--repo", str(w.repo), "--filter", "e2e.yaml", "--no-initial",
                                         "--verify", "build+analyze"), "setup", stage="new-run")
        run_dir = (cls.rec.get("new_run") or {}).get("run_dir")
        cls.run_dir = Path(run_dir) if run_dir else None

        # 検索を 20 秒で区切らせる：エミュレーターのどの応答も 1.5 秒遅らせる（検索が終わったら戻す）。
        # チェッカーにワイルドカードがあるとサーバーでは絞らないので、273 件を 20 件ずつ 14 ページ読む
        def on_search(n: int, result: Any) -> None:
            if result.ok and result.get("done"):
                w.emu.config(latency_seconds=0)
        w.emu.config(latency_seconds=1.5)
        cls.actors = Actors(w, reviews=reviews_a, on_search_call=on_search)
        cls.orch = Orchestrator(w, run_dir or "", cls.actors, cls.scene)
        cls.step("run", cls.orch.go, "new_run")
        for stage, seconds in cls.orch.stage_seconds():
            TIMINGS.add(cls.scene, f"tick の段 {stage}", seconds)
        cls.step("expected", lambda: expected(w, MAIN, 20, checkers=["DIVIDE_BY_ZERO", "OVERRUN", "RESOURCE_LEAK",
                                                                       "STRING_OVERFLOW", "UNUSED_*", "NULL_RETURNS"]))
        cls.step("gui", lambda: gui_open(w, run_dir, "monitor"), "run", stage="画面を開く")
        url = (cls.rec.get("gui") or {}).get("url", "")
        run_id = Path(run_dir).name if run_dir else ""
        cls.step("approval_early", lambda: gui_call(url, "POST", f"/api/runs/{run_id}/approvals",
                                                    {"item": "10109", "decision": "fix"}), "gui")
        cls.step("summary", lambda: w.ct("summary", "--run", run_dir), "run", stage="summary")
        cls.step("defaults", lambda: read_json(Path(run_dir) / "approvals.json"), "summary")
        edited = {"classification": "Intentional", "action": "Ignore", "severity": "Unspecified",
                  "comment": "意図的。ADDR フラグを消すために SR1 と SR2 を順に読む仕様で、読んだ値は使わない（src/hal/sim_bus.c:336）。"}
        cls.edited = edited

        def approve() -> List[Any]:
            out = [gui_call(url, "POST", f"/api/runs/{run_id}/approvals", body) for body in (
                {"item": "10172", "decision": "deviation", "deviation": edited},
                {"item": "10162", "decision": "reject"},
                {"item": "10049", "decision": None})]
            return out
        cls.step("approvals", approve, "summary", "gui", stage="人の承認（画面）")
        cls.step("shots", lambda: capture([
            {"name": "e2e-git-review-summary", "url": file_url(Path(run_dir) / "summary.html")},
            {"name": "e2e-git-review-detail-merged-G2", "url": file_url(Path(run_dir) / "cid" / "G2.html")},
            {"name": "e2e-git-review-detail-verify-low-10049", "url": file_url(Path(run_dir) / "cid" / "10049.html")},
            {"name": "e2e-git-review-detail-flagged-10243", "url": file_url(Path(run_dir) / "cid" / "10243.html")},
            {"name": "e2e-git-review-gui-results", "url": url.split("#")[0] + f"#run={run_id}&view=summary",
             "full_page": False, "wait_ms": 1500},
            {"name": "e2e-git-review-gui-monitor", "url": url.split("#")[0] + f"#run={run_id}&view=monitor",
             "full_page": False, "wait_ms": 1500}]), "approvals", stage="写真")
        cls.step("preview", lambda: w.ct("preview", "--run", run_dir), "approvals", stage="preview")
        preview = cls.rec.get("preview") or {}
        plan_file, token = preview.get("plan_file", ""), preview.get("confirmation_token", "")
        mcp = w.mcp()
        cls.step("preview_triage", lambda: mcp.call("preview_triage", {"plan_file": plan_file}), "preview")
        cls.step("writes_before", w.emu.writes, "preview")
        cls.step("wrong_token", lambda: mcp.call("update_triage", {"repo_root": str(w.repo), "plan_file": plan_file,
                                                                   "confirmation_token": "000000000000"}), "preview")
        cls.step("writes_after_wrong", w.emu.writes, "wrong_token")

        def update() -> List[Any]:
            calls = []
            for _ in range(5):
                calls.append(mcp.call("update_triage", {"repo_root": str(w.repo), "plan_file": plan_file,
                                                        "confirmation_token": token}))
                if not calls[-1].ok or calls[-1]["done"]:
                    break
            return calls
        cls.step("update", update, "wrong_token", stage="update_triage")
        cls.step("writes", w.emu.writes, "update")
        cls.step("update_again", lambda: mcp.call("update_triage", {"repo_root": str(w.repo), "plan_file": plan_file,
                                                                    "confirmation_token": token}), "update")
        cls.step("writes_again", w.emu.writes, "update_again")
        cls.step("apply", lambda: w.ct("apply-code", "--run", run_dir, "--token", token), "update", stage="apply-code")
        cls.step("pushes", w.pushes, "apply")
        cls.step("apply_again", lambda: w.ct("apply-code", "--run", run_dir, "--token", token), "apply")
        cls.step("pushes_again", w.pushes, "apply_again")
        cls.step("after", w.fingerprint, "apply_again")
        if url:
            gui_close(url)

    # ---- 準備 ----

    def test_req004_setup_commands_and_doctor_ready(self) -> None:
        setup = self.need("setup")
        for name in ("init-config", "set-verify", "install-agent", "prewarm"):
            with self.subTest(name):
                self.assertTrue(setup[name].get("ok"), setup[name])
        doctor = setup["doctor"]
        bad = [c for c in doctor["checks"] if c["status"] == "ng"]
        self.assertTrue(doctor["ready"], f"doctor が ready になりません: {bad}")

    def test_req106_settings_hold_no_credentials(self) -> None:
        self.need("setup")
        text = self.world.config_file.read_text(encoding="utf-8")
        self.assertNotIn(KEY, text)
        self.assertIn("key_env: COV_AUTH_KEY", text)
        self.assertIn(f"url: {self.world.emu.url}", text)
        self.assertIn("- /build/agent/work/envmon/", text)

    # ---- 取得 ----

    def test_req108_search_called_again_until_done_within_client_limit(self) -> None:
        self.need("run")
        calls = self.actors.search_calls
        self.assertGreaterEqual(len(calls), 2, "20 秒で区切られて、呼び直しが起きていません")
        self.assertFalse(calls[0]["done"])
        self.assertIsNotNone(calls[0].get("next"))
        self.assertTrue(calls[-1]["done"])
        for call in calls:
            self.assertLess(call.seconds, 30, "1回のツールが Copilot の既定の 30 秒を超えました")
        issues = read_json(self.run_dir / "issues.json")
        self.assertEqual([i["cid"] for i in issues["issues"]], self.need("expected"))
        self.assertEqual(issues["analyzed_revision"], self.world.analyzed)
        self.assertEqual(calls[-1]["excluded"], len(self.world.emu.latest_cids(MAIN)) - len(self.need("expected")))

    def test_req108_paths_saved_for_every_cid(self) -> None:
        self.need("run")
        state = read_json(self.run_dir / "run.json")
        cids = [c for i in state["items"] if not i["merged_from"] for c in i["cids"]]
        self.assertEqual(sorted(cids), self.need("expected"))
        for item in state["items"]:
            for cid in item["cids"]:
                with self.subTest(item=item["id"], cid=cid):
                    data = read_json(self.run_dir / "items" / item["id"] / f"issue-{cid}.json")
                    self.assertNotIn("error", data)
                    self.assertTrue(any(e.get("main") for e in data["events"]))
        self.assertTrue(all(c.ok and c["done"] for c in self.actors.path_calls))

    # ---- 作業 ----

    def test_req101_every_item_has_judgement_confidence_and_both_plans(self) -> None:
        self.need("run")
        state = read_json(self.run_dir / "run.json")
        done = [i for i in state["items"] if i["status"] == "done"]
        self.assertEqual(len(done), 15)
        analyzed_root = self.world.repo  # 根拠のファイルは解析したリビジョンにもある
        for item in done:
            with self.subTest(item=item["id"]):
                result = read_json(self.run_dir / "items" / item["id"] / "result.json")
                self.assertEqual(sorted(result), sorted(RESULT_KEYS))  # K-48
                self.assertIn(result["verdict"]["judgement"], ("true_bug", "false_positive", "intentional",
                                                              "undetermined"))
                self.assertIn(result["confidence"], ("high", "medium", "low"))
                self.assertTrue(result["deviation"]["comment"].strip())
                self.assertTrue(result["fix"]["summary"].strip())
                for ev in result["verdict"]["evidence"]:
                    self.assertTrue((analyzed_root / ev["file"]).is_file(), ev)
                patch = self.run_dir / "patches" / f"{item['id']}-fix.patch"
                if item["id"] in ALREADY_FIXED:
                    self.assertFalse(patch.exists())
                else:
                    self.assertTrue(patch.is_file())
                    fixed = self.run_dir / "fixed" / item["id"] / "fix"
                    self.assertTrue(any(fixed.rglob("*.*")), f"{fixed} に修正後のファイルがありません")
                self.assertTrue((self.run_dir / "cid" / f"{item['id']}.html").is_file())

    def test_req101_already_fixed_on_latest_has_no_fix_and_says_so(self) -> None:
        self.need("run", "defaults", "preview")
        for item in sorted(ALREADY_FIXED):
            with self.subTest(item=item):
                result = read_json(self.run_dir / "items" / item / "result.json")
                self.assertTrue(result["fix"]["already_fixed_on_latest"])
                self.assertIsNone(self.rec["defaults"]["items"][item]["decision"])  # K-34：承認ははじめ空
                self.assertIn("最新", (self.run_dir / "cid" / f"{item}.html").read_text(encoding="utf-8"))
        self.assertEqual(sorted(self.rec["preview"].get("already_fixed_on_latest") or []), [])  # 承認が空なので出ない

    def test_req101_group_is_worked_on_as_one(self) -> None:
        self.need("run")
        brief = Brief(self.run_dir / "items" / "G1" / "brief.md")
        self.assertTrue(brief.group)
        self.assertEqual([w["cid"] for w in brief.warnings], [10063, 10064])
        self.assertEqual(read_json(brief.result)["item"], "G1")

    # ---- 統合と検証 ----

    def test_req105_integration_merges_items_of_one_cause(self) -> None:
        self.need("run")
        state = read_json(self.run_dir / "run.json")
        by_id = {i["id"]: i for i in state["items"]}
        self.assertEqual(by_id["G1"]["status"], "merged")
        self.assertEqual(by_id["10237"]["status"], "merged")
        self.assertEqual(by_id["G2"]["merged_from"], ["G1", "10237"])
        self.assertEqual(set(by_id["G2"]["cids"]), FRAME_MAX_CIDS)
        self.assertEqual(by_id["G2"]["status"], "done")
        patch = (self.run_dir / "patches" / "G2-fix.patch").read_text(encoding="utf-8")
        self.assertIn("+#define FRAME_MAX               (FRAME_HDR_LEN + FRAME_PAYLOAD_MAX + FRAME_CRC_LEN)", patch)
        self.assertEqual(patch.count("+++ "), 1)
        rows = summary_rows(self.run_dir / "summary.html")
        self.assertIn("G2", rows)
        self.assertNotIn("G1", rows)

    def test_req105_review_outcome_recorded_for_every_item(self) -> None:
        self.need("run")
        state = read_json(self.run_dir / "run.json")
        for item in state["items"]:
            if item["status"] != "done":
                continue
            with self.subTest(item=item["id"]):
                self.assertIn(item["review"], ("pass", "flag"))  # K-54
                review = read_json(self.run_dir / "items" / item["id"] / "review.json")
                self.assertEqual(review["item"], item["id"])
        by_id = {i["id"]: i for i in state["items"]}
        self.assertEqual(by_id["10243"]["review"], "flag")
        self.assertEqual(by_id["10158"]["review_rounds"], 1)
        self.assertEqual(self.actors.review_rounds["10158"], 2)
        self.assertEqual(self.actors.work_rounds["10158"], 2)  # 差し戻しで作業をやり直した
        brief = Brief(self.run_dir / "items" / "10158" / "brief.md")
        self.assertTrue(brief.rework)

    def test_req105_shown_confidence_is_the_lowest(self) -> None:
        self.need("summary")
        rows = summary_rows(self.run_dir / "summary.html")
        self.assertEqual(rows["10243"]["confidence"], "medium")  # 検証の上限（K-56）
        self.assertEqual(rows["10049"]["confidence"], "low")  # ビルドでの検証で警告が残った
        for item in REAL_FIXES_A:
            self.assertEqual(rows[item]["confidence"], "high", item)
        for item in ("10243", "10049"):  # result.json は書き換えない
            self.assertEqual(read_json(self.run_dir / "items" / item / "result.json")["confidence"], "high")

    def test_req105_reanalysis_resolves_real_fixes_and_keeps_the_rest(self) -> None:
        self.need("run")
        verify = read_json(self.run_dir / "verify.json")
        command = self.orch.commands[-1]
        self.assertTrue(command.get("ok"), command)
        self.assertEqual(command.get("downgraded_to_low"), ["10049"])
        for item in REAL_FIXES_A:
            with self.subTest(item=item):
                self.assertTrue(verify[item]["build_ok"])
                self.assertEqual(verify[item]["remaining_cids"], [])
                state = {i["id"]: i for i in read_json(self.run_dir / "run.json")["items"]}
                self.assertEqual(sorted(verify[item]["resolved_cids"]), sorted(state[item]["cids"]))
        for item in ("10049", "10162", "10172"):  # 原因を直していない修正：再解析で警告が残る
            with self.subTest(item=item):
                self.assertEqual(verify[item]["remaining_cids"], [int(item)])
        for item in ALREADY_FIXED:
            self.assertNotIn(item, verify)

    # ---- 一覧と承認 ----

    def test_req103_approvals_wait_until_the_summary_is_made(self) -> None:
        status, body = self.need("approval_early")
        self.assertFalse(body.get("ok"), body)
        self.assertIn("一覧ができてから", body.get("error", ""))  # K-80
        self.assertFalse(self.need("defaults")["items"]["10109"]["edited"])  # 断った承認は残っていない

    def test_req104_summary_and_reports(self) -> None:
        summary = self.need("summary")
        self.assertTrue(summary.get("ok"), summary)
        html = (self.run_dir / "summary.html").read_text(encoding="utf-8")
        self.assertIn("<h1>トリアージ結果", html)
        self.assertIn("<th>承認</th><th>ID</th><th>推奨</th><th>確信度</th><th>見立て</th><th>詳細</th>", html)  # K-83
        rows = summary_rows(self.run_dir / "summary.html")
        self.assertEqual(set(rows), REAL_FIXES_A | DEVIATIONS_A | ALREADY_FIXED | {"10049"})
        order = {"low": 0, "medium": 1, "high": 2}
        confidences = [order[r["confidence"]] for r in rows.values()]
        self.assertEqual(confidences, sorted(confidences))  # 確信度の低い順
        self.assertEqual(summary["recommended"], {"fix": 11, "deviation": 4})
        defaults = self.need("defaults")["items"]
        for item in REAL_FIXES_A:
            self.assertEqual(defaults[item]["decision"], "fix")
        for item in DEVIATIONS_A:
            self.assertEqual(defaults[item]["decision"], "deviation")
        detail = (self.run_dir / "cid" / "G2.html").read_text(encoding="utf-8")
        for heading in ("結論", "警告の概要", "真偽の根拠", "対応の案", "ビルドでの検証", "独立した検証", "処理情報"):
            self.assertIn(f"<h2>{heading}</h2>", detail)  # K-84
        self.assertEqual(summary["removed_work_copies"], ["analyzed", "fix-1", "verify"])

    def test_req104_screenshots_taken_when_asked(self) -> None:
        shots = self.need("shots")
        if capture_dir() is None:
            self.skipTest("CWT_CAPTURE_DIR がないため、写真は撮りません")
        self.assertNotIn("error", shots, shots)
        for shot in shots["shots"]:
            with self.subTest(shot["name"]):
                self.assertTrue(shot["ok"], shot)
                self.assertTrue(Path(shot["file"]).is_file())

    # ---- 書き戻しと反映 ----

    def test_req108_preview_triage_shows_count_content_and_same_token(self) -> None:
        preview = self.need("preview")
        triage = self.need("preview_triage")
        self.assertTrue(triage.ok, triage)
        self.assertEqual(triage["confirmation_token"], preview["confirmation_token"])
        self.assertEqual(triage["counts"], {"entries": 3, "cids": 3})
        self.assertEqual(preview["counts"], {"修正": 8, "逸脱": 3, "却下": 1})
        self.assertEqual(self.need("writes_before"), [])  # 見せただけでは書き込まれない

    def test_req106_wrong_confirmation_writes_nothing(self) -> None:
        wrong = self.need("wrong_token")
        self.assertFalse(wrong.ok)
        self.assertIn("確認用の文字列", wrong.error)
        self.assertEqual(self.need("writes_after_wrong"), [])

    def test_req106_only_approved_deviations_are_written(self) -> None:
        calls = self.need("update")
        self.assertTrue(calls[-1].ok and calls[-1]["done"], calls)
        writes = self.need("writes")
        got = {tuple(w["cids"]): (w["store"], {**w["attributes"], "Comment": w["comment"]}) for w in writes}
        self.assertEqual(len(got), len(writes), writes)
        self.assertEqual({w["user"] for w in writes}, {USER})
        expected_writes = {}
        for item in ("10017", "10243"):
            result = read_json(self.run_dir / "items" / item / "result.json")["deviation"]
            expected_writes[(int(item),)] = ("Default Triage Store", {
                "Classification": result["classification"], "Action": result["action"],
                "Severity": result["severity"], "Comment": result["comment"]})
        expected_writes[(10172,)] = ("Default Triage Store", {
            "Classification": self.edited["classification"], "Action": self.edited["action"],
            "Severity": self.edited["severity"], "Comment": self.edited["comment"]})
        self.assertEqual(got, expected_writes, writes)
        table = self.world.emu.table(MAIN)
        self.assertEqual(table[10172]["classification"], "Intentional")
        self.assertEqual(table[10162]["classification"], "Unclassified")  # 却下は書き込まない
        self.assertEqual(table[10109]["classification"], "Unclassified")  # 修正は書き込まない

    def test_req108_same_write_back_is_not_written_twice(self) -> None:
        again = self.need("update_again")
        self.assertTrue(again.ok, again)
        self.assertEqual(again["written"], [])
        self.assertEqual(again["written_total"], 3)
        self.assertEqual(len(self.need("writes_again")), len(self.need("writes")))

    def test_req106_apply_code_pushes_approved_fix_branches(self) -> None:
        apply = self.need("apply")
        self.assertTrue(apply.get("ok"), apply)
        self.assertEqual(apply["errors"], [])
        self.assertEqual(sorted(apply["coverity"]["registered"]), ["10017", "10172", "10243"])
        run_id = self.run_dir.name
        wanted = {f"refs/heads/coverity-fix/cid-{i}" for i in REAL_FIXES_A - {"G2"}} | \
            {f"refs/heads/coverity-fix/{run_id}-G2"}
        pushed = {line.split()[0] for line in self.need("pushes")}
        self.assertEqual(pushed, wanted)
        self.assertEqual(len(apply["pull_requests"]), 8)
        state = {i["id"]: i for i in read_json(self.run_dir / "run.json")["items"]}
        for ref in sorted(wanted):
            branch = ref[len("refs/heads/"):]
            message = run(["git", "log", "-1", "--format=%B", branch], cwd=self.world.remote,
                          env=self.world.env).stdout
            item = "G2" if branch.endswith("-G2") else branch.rsplit("-", 1)[1]
            cids = ", ".join(map(str, state[item]["cids"]))
            summary = read_json(self.run_dir / "items" / item / "result.json")["fix"]["summary"]
            with self.subTest(branch=branch):
                self.assertEqual(message.splitlines()[0],
                                 f"Coverity CID {cids}: 修正" + ("（統合）" if item == "G2" else ""))
                self.assertIn(summary, message)
                parent = run(["git", "rev-parse", f"{branch}^"], cwd=self.world.remote, env=self.world.env).stdout
                self.assertEqual(parent.strip(), self.world.latest)  # 取り込み先の最新のリビジョンの上
        for pr in apply["pull_requests"]:
            self.assertEqual(pr["base"], "main")
            self.assertTrue(pr["title"].startswith("Coverity CID "))

    def test_req106_apply_code_again_does_not_push_again(self) -> None:
        again = self.need("apply_again")
        self.assertTrue(again.get("ok"), again)
        self.assertTrue(again.get("repeated"))
        self.assertEqual(self.need("pushes_again"), self.need("pushes"))
        self.assertEqual(again["pull_requests"], self.need("apply")["pull_requests"])

    def test_req106_working_files_never_changed(self) -> None:
        self.assertEqual(self.need("after"), self.need("before"))  # K-65

    def test_req106_output_dir_is_outside_the_repository(self) -> None:
        self.need("new_run")
        self.assertNotIn(self.world.repo, self.run_dir.parents)  # K-72

    def test_req106_key_never_written_to_logs_or_records(self) -> None:
        self.need("apply_again")
        places = [self.run_dir, self.world.logs]
        found = []
        for place in places:
            for path in place.rglob("*"):
                if path.is_file() and path.stat().st_size < 5_000_000:
                    try:
                        if KEY.encode() in path.read_bytes():
                            found.append(str(path))
                    except OSError:
                        continue
        self.assertEqual(found, [], "認証キーがファイルに書かれています")


# =================================================================================================
# 2. git・統合と検証なし・取得のエージェントが使えない・ビルドを壊す修正
# =================================================================================================

FILTER_NULL = "name: nullret\nproject: envmon\nstreams: [{stream}]\ncheckers: [NULL_RETURNS]\nlimit: 20\n"


class GitPlainFlowTest(FlowCase):
    """検証なし。取得のエージェントが起動できず統括が MCP を呼ぶ。修正のない逸脱の結果は差し戻され、
    わざとビルドを壊す修正（10041）は、ビルドでの検証で確信度が低になる。"""

    scene = "git・検証なし"

    @classmethod
    def flow(cls) -> None:
        cls.world = w = World("git-plain")
        cls.step("setup", lambda: w.setup(MAIN, verify_default="build"))
        w.write_filter("null.yaml", FILTER_NULL.format(stream=MAIN))
        cls.step("before", w.fingerprint)
        cls.step("new_run", lambda: w.ct("new-run", "--repo", str(w.repo), "--filter", "null.yaml", "--no-initial"),
                 "setup", stage="new-run")
        run_dir = (cls.rec.get("new_run") or {}).get("run_dir", "")
        cls.run_dir = Path(run_dir) if run_dir else None
        cls.actors = Actors(w, fixes={"TB-02": "rotate_broken"}, first_without_fix=(10017,),
                            collector_unavailable=True)
        cls.orch = Orchestrator(w, run_dir, cls.actors, cls.scene)
        cls.step("run", cls.orch.go, "new_run")
        for stage, seconds in cls.orch.stage_seconds():
            TIMINGS.add(cls.scene, f"tick の段 {stage}", seconds)
        cls.step("summary", lambda: w.ct("summary", "--run", run_dir), "run", stage="summary")
        cls.step("gui", lambda: gui_open(w, run_dir, "summary"), "summary")
        url = (cls.rec.get("gui") or {}).get("url", "")
        run_id = Path(run_dir).name if run_dir else ""
        cls.step("approvals", lambda: gui_call(url, "POST", f"/api/runs/{run_id}/approvals",
                                               {"item": "10041", "decision": "reject"}), "gui")
        cls.step("shots", lambda: capture([
            {"name": "e2e-git-plain-summary", "url": file_url(Path(run_dir) / "summary.html")},
            {"name": "e2e-git-plain-detail-broken-build-10041", "url": file_url(Path(run_dir) / "cid" / "10041.html")}]),
            "approvals", stage="写真")
        cls.step("preview", lambda: w.ct("preview", "--run", run_dir), "approvals")
        preview = cls.rec.get("preview") or {}
        cls.step("update", lambda: w.mcp().call("update_triage", {
            "repo_root": str(w.repo), "plan_file": preview.get("plan_file", ""),
            "confirmation_token": preview.get("confirmation_token", "")}), "preview")
        cls.step("apply", lambda: w.ct("apply-code", "--run", run_dir, "--token",
                                       preview.get("confirmation_token", "")), "update", stage="apply-code")
        cls.step("after", w.fingerprint, "apply")
        if url:
            gui_close(url)

    def test_req108_main_ai_calls_the_tools_when_the_collector_cannot_start(self) -> None:
        self.need("run")
        mcp_actions = [a for t in self.orch.ticks for a in t.get("actions") or [] if a["type"] == "mcp"]
        tools = {a["tool"] for a in mcp_actions}
        self.assertEqual(tools, {"search_issues", "get_issues"})
        self.assertTrue(any("取得エージェントを使えませんでした" in s for s in self.orch.says()))
        issues = read_json(self.run_dir / "issues.json")
        self.assertEqual([i["cid"] for i in issues["issues"]], expected(self.world, MAIN, 20, ["NULL_RETURNS"]))

    def test_req101_deviation_without_a_fix_is_sent_back_and_then_done(self) -> None:
        self.need("run")
        relaunch = [a for t in self.orch.ticks for a in t.get("actions") or []
                    if a["type"] == "launch" and a.get("item") == "10017" and "#" in a["task"]]
        self.assertTrue(relaunch, "修正のない結果が差し戻されていません")
        self.assertIn("のファイルを修正してください", relaunch[0]["prompt"])  # K-35：誤検知でも修正案を作る
        state = {i["id"]: i for i in read_json(self.run_dir / "run.json")["items"]}
        self.assertEqual(state["10017"]["status"], "done")
        self.assertTrue((self.run_dir / "patches" / "10017-fix.patch").is_file())
        self.assertEqual(read_json(self.run_dir / "items" / "10017" / "result.json")["recommendation"], "deviation")

    def test_req105_a_fix_that_breaks_the_build_is_shown_low(self) -> None:
        self.need("summary")
        verify = read_json(self.run_dir / "verify.json")
        self.assertFalse(verify["10041"]["build_ok"])
        self.assertIn("ビルドエラーがこの修正案の変更したファイルで出ています", verify["10041"]["problems"])
        self.assertIn("logger.c", verify["10041"]["log_tail"])
        command = self.orch.commands[-1]
        self.assertEqual(command.get("downgraded_to_low"), ["10041"])
        rows = summary_rows(self.run_dir / "summary.html")
        self.assertEqual(rows["10041"]["confidence"], "low")
        self.assertEqual(read_json(self.run_dir / "items" / "10041" / "result.json")["confidence"], "high")
        for item in ("10027", "10220"):  # ほかの修正は確かめられなかったが、下げない
            self.assertIsNone(verify[item]["build_ok"])
            self.assertEqual(rows[item]["confidence"], "high")

    def test_req106_rejected_fix_is_not_pushed_and_the_others_are(self) -> None:
        apply = self.need("apply")
        self.assertTrue(apply.get("ok"), apply)
        pushed = {line.split()[0] for line in self.world.pushes()}
        self.assertEqual(pushed, {"refs/heads/coverity-fix/cid-10027", "refs/heads/coverity-fix/cid-10220"})
        self.assertEqual(sorted(apply["coverity"]["registered"]), ["10017", "10243"])
        self.assertEqual(apply["rejected"], ["10041"])
        self.assertEqual(self.need("after"), self.need("before"))


# =================================================================================================
# 3. svn
# =================================================================================================


class SvnFlowTest(FlowCase):
    """svn のリポジトリで同じ流れ。反映でワーキングコピーに差分が当たる（コミットはしない）。"""

    scene = "svn"

    @classmethod
    def flow(cls) -> None:
        cls.world = w = World("svn", vcs="svn")
        cls.step("setup", lambda: w.setup("envmon-svn", verify_default="build"))
        w.write_filter("null.yaml", FILTER_NULL.format(stream="envmon-svn"))
        cls.step("before", w.fingerprint)
        cls.step("new_run", lambda: w.ct("new-run", "--repo", str(w.repo), "--filter", "null.yaml", "--no-initial"),
                 "setup", stage="new-run")
        run_dir = (cls.rec.get("new_run") or {}).get("run_dir", "")
        cls.run_dir = Path(run_dir) if run_dir else None
        cls.actors = Actors(w)
        cls.orch = Orchestrator(w, run_dir, cls.actors, cls.scene)
        cls.step("run", cls.orch.go, "new_run")
        for stage, seconds in cls.orch.stage_seconds():
            TIMINGS.add(cls.scene, f"tick の段 {stage}", seconds)
        cls.step("summary", lambda: w.ct("summary", "--run", run_dir), "run", stage="summary")
        cls.step("shots", lambda: capture([{"name": "e2e-svn-summary", "url": file_url(Path(run_dir) / "summary.html")}]),
                 "summary")
        cls.step("before_apply", w.fingerprint, "summary")
        cls.step("preview", lambda: w.ct("preview", "--run", run_dir), "summary")
        preview = cls.rec.get("preview") or {}
        cls.step("update", lambda: w.mcp().call("update_triage", {
            "repo_root": str(w.repo), "plan_file": preview.get("plan_file", ""),
            "confirmation_token": preview.get("confirmation_token", "")}), "preview")
        cls.step("writes", w.emu.writes, "update")
        cls.step("apply", lambda: w.ct("apply-code", "--run", run_dir, "--token",
                                       preview.get("confirmation_token", "")), "update", stage="apply-code")
        cls.step("diff", lambda: run(["svn", "diff"], cwd=w.repo, env=w.env).stdout, "apply")
        cls.step("revision", lambda: run(["svn", "info", "--show-item", "revision", "-r", "HEAD"], cwd=w.repo,
                                         env=w.env).stdout.strip(), "apply")

    def test_req101_svn_items_are_all_done_with_svn_patches(self) -> None:
        self.need("summary")
        state = read_json(self.run_dir / "run.json")
        self.assertEqual(sorted(i["status"] for i in state["items"]), ["done"] * 5)
        self.assertEqual(state["analyzed_revision"], "1")
        self.assertEqual(state["latest_revision"], "2")
        patch = (self.run_dir / "patches" / "10220-fix.patch").read_text(encoding="utf-8")
        self.assertTrue(patch.startswith("Index: "), patch[:200])

    def test_req105_svn_build_verification_applies_the_patches(self) -> None:
        self.need("run")
        verify = read_json(self.run_dir / "verify.json")
        for item, checked in verify.items():
            with self.subTest(item=item):
                self.assertTrue(checked.get("applied"), checked)
                self.assertTrue(checked.get("build_ok"), checked)

    def test_req106_svn_working_copy_unchanged_until_applied(self) -> None:
        self.assertEqual(self.need("before_apply"), self.need("before"))

    def test_req106_svn_apply_code_patches_the_working_copy_without_commit(self) -> None:
        apply = self.need("apply")
        self.assertTrue(apply.get("ok"), apply)
        self.assertEqual(sorted(apply["svn_applied"]), ["10027:fix", "10041:fix", "10220:fix"])
        diff = self.need("diff")
        self.assertIn("+    if (buf == NULL) {", diff)
        self.assertIn("+    if (s_log.fp == NULL) {", diff)
        self.assertEqual(self.need("revision"), "2")
        writes = self.need("writes")
        self.assertEqual(sorted(c for w in writes for c in w.get("cids", [])), [10017, 10243])


# =================================================================================================
# 4. 初回解析
# =================================================================================================


class InitialAnalysisTest(FlowCase):
    """new-run --initial：tick が初回解析（cov-build → cov-analyze → cov-commit-defects）を動かす。"""

    scene = "初回解析"

    @classmethod
    def flow(cls) -> None:
        cls.world = w = World("initial")
        cls.step("setup", lambda: w.setup(MAIN))
        w.write_filter("div.yaml", "name: div\nproject: envmon\nstreams: [envmon-main]\ncheckers: [DIVIDE_BY_ZERO]\n"
                                   "limit: 5\n")
        cls.step("snapshots_before", lambda: len(w.emu.state()["snapshots"]))
        cls.step("head", lambda: run(["git", "rev-parse", "HEAD"], cwd=w.repo, env=w.env).stdout.strip())

        # (1) ビルドの失敗
        def build_failure() -> Dict[str, Any]:
            w.ct("set-verify", "--repo", str(w.repo), "--build-command", "make no-such-target", "--default",
                 "build+analyze", "--initial")
            run_ = w.ct("new-run", "--repo", str(w.repo), "--filter", "div.yaml", "--initial")
            orch = Orchestrator(w, run_["run_dir"], Actors(w), cls.scene)
            final = orch.go()
            w.ct("set-verify", "--repo", str(w.repo), "--build-command", "make", "--default", "build+analyze",
                 "--initial")
            return {"run": run_, "final": final, "orch": orch}
        cls.step("build_failure", build_failure, "setup", stage="初回解析（ビルドの失敗）")

        # (2) 登録の失敗（cov-commit-defects が終了コード 2）、(3) 直して resume で、もう一度初回解析から
        def commit_failure() -> Dict[str, Any]:
            run_ = w.ct("new-run", "--repo", str(w.repo), "--filter", "div.yaml", "--initial")
            orch = Orchestrator(w, run_["run_dir"], Actors(w), cls.scene)
            final = orch.go(env={"COV_EMU_COMMIT_RESULT": "2"})
            return {"run": run_, "final": final, "orch": orch}
        cls.step("commit_failure", commit_failure, "build_failure", stage="初回解析（登録の失敗）")

        def failure_shot() -> Any:
            opened = gui_open(w, cls.rec["commit_failure"]["run"]["run_dir"], "monitor")
            run_id = Path(cls.rec["commit_failure"]["run"]["run_dir"]).name
            try:
                return capture([{"name": "e2e-initial-commit-failure-monitor",
                                 "url": opened["url"].split("#")[0] + f"#run={run_id}&view=monitor",
                                 "full_page": False, "wait_ms": 1500}])
            finally:
                gui_close(opened["url"])
        cls.step("shots", failure_shot, "commit_failure", stage="写真")
        cls.step("snapshots_after_failures", lambda: len(w.emu.state()["snapshots"]), "commit_failure")
        open(w.cov_log, "w").close()

        def resumed() -> Dict[str, Any]:
            failed = cls.rec["commit_failure"]
            back = w.ct("resume", "--run", failed["run"]["run_dir"])
            orch = Orchestrator(w, failed["run"]["run_dir"], Actors(w), cls.scene)
            final = orch.go(until=lambda r: r.get("stage") == "items")
            return {"resume": back, "final": final, "orch": orch}
        cls.step("resumed", resumed, "commit_failure", stage="初回解析（resume で成功）")
        cls.step("fixed_run", lambda: w.ct("new-run", "--repo", str(w.repo), "--filter", "div.yaml", "--no-initial",
                                           "--checker", "RESOURCE_LEAK", "--checker", "STRING_OVERFLOW",
                                           "--status", "Fixed"), "resumed")
        cls.step("fixed_search", lambda: search_to_file(w, cls.rec["fixed_run"]), "fixed_run")
        cls.step("tracked", w.tracked_changes, "resumed")

    def test_req102_build_failure_stops_before_the_triage(self) -> None:
        got = self.need("build_failure")
        final = got["final"]
        self.assertTrue(final.get("stop"))
        self.assertEqual(final["stage"], "initial")
        self.assertEqual(final["initial"]["step_label"], "ビルド（cov-build）")
        self.assertIn("終了コード 2", final["error"])
        self.assertIn("no-such-target", final["initial"]["log_tail"])
        self.assertFalse(any(a["type"] == "launch" for t in got["orch"].ticks for a in t.get("actions") or []))
        self.assertFalse(Path(got["run"]["run_dir"], "issues.json").exists())  # K-76

    def test_req102_commit_failure_stops_before_the_triage(self) -> None:
        got = self.need("commit_failure")
        final = got["final"]
        self.assertTrue(final.get("stop"))
        self.assertEqual(final["initial"]["step_label"], "サーバへの登録（cov-commit-defects）")
        self.assertIn("終了コード 2", final["error"])
        self.assertTrue(final["initial"]["log_tail"].strip())
        self.assertIn("初回解析に失敗しました", " ".join(got["orch"].says()))
        self.assertEqual(self.need("snapshots_after_failures"), self.need("snapshots_before"))

    def test_req102_resume_runs_the_initial_analysis_again_and_commits_the_current_commit(self) -> None:
        got = self.need("resumed")
        self.assertTrue(got["resume"].get("ok"), got["resume"])
        self.assertEqual(got["final"]["stage"], "items")
        state = self.world.emu.state()
        self.assertEqual(len(state["snapshots"]), self.need("snapshots_before") + 1)
        newest = max(state["snapshots"].values(), key=lambda s: (s["dateCreated"], s["id"]))
        self.assertEqual(newest["stream"], MAIN)
        self.assertEqual(newest["sourceVersion"], self.need("head"))  # 版は今のコミット（K-05）
        tools = [c["tool"] for c in self.world.cov_calls()]
        self.assertEqual(tools, ["cov-build", "cov-analyze", "cov-commit-defects"])
        run_state = read_json(Path(got["resume"]["run_dir"]) / "run.json")
        self.assertEqual(run_state["analyzed_revision"], self.need("head"))

    def test_req106_key_is_passed_only_to_the_commit_and_never_on_a_command_line(self) -> None:
        self.need("resumed")
        calls = self.world.cov_calls()
        for call in calls:
            with self.subTest(tool=call["tool"]):
                self.assertNotIn(KEY, " ".join(call["argv"]))  # K-70
                self.assertFalse(call["key_env"], "COV_AUTH_KEY がビルドと解析に渡っています")
                self.assertEqual(call["passphrase_is_key"], call["tool"] == "cov-commit-defects")
        commit = next(c for c in calls if c["tool"] == "cov-commit-defects")
        self.assertIn("--user", commit["argv"])
        self.assertEqual(commit["argv"][commit["argv"].index("--stream") + 1], MAIN)
        for run_name in ("build_failure", "commit_failure"):
            run_dir = Path(self.need(run_name)["run"]["run_dir"])
            for path in run_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(KEY.encode(), path.read_bytes(), str(path))

    def test_req106_initial_analysis_leaves_tracked_files_unchanged(self) -> None:
        self.assertEqual(self.need("tracked"), "")

    def test_req108_fixed_warnings_are_found_by_status_after_reanalysis(self) -> None:
        self.need("resumed")
        table = self.world.emu.table(MAIN)
        self.assertEqual(table[10025]["status"], "Fixed")
        self.assertEqual(table[10131]["status"], "Fixed")
        result = self.need("fixed_search")
        self.assertTrue(result.ok, result)
        issues = read_json(Path(self.rec["fixed_run"]["issues_file"]))
        self.assertEqual(sorted(i["cid"] for i in issues["issues"]), [10025, 10131],
                         "Status に Fixed を指定しても、直った警告が見つかりません")


# =================================================================================================
# 5. 条件ファイルの組み合わせ
# =================================================================================================


class FilterSearchTest(FlowCase):
    """条件（Impact、チェッカー、分類・対応・状態、件数、ストリーム）で検索した issues.json の CID が、
    エミュレーターの状態から求めた正しい集まりと一致する。"""

    scene = "条件の組み合わせ"

    @classmethod
    def flow(cls) -> None:
        cls.world = w = World("filters")
        cls.step("setup", lambda: w.setup(MAIN, initial=False))
        # ほかの人が Coverity の画面でトリアージした警告（Default と Release のトリアージストア）
        def triage(store: str, cids: List[int], classification: str, action: str) -> Any:
            return w.emu.rest("PUT", "/api/v2/issues/triage?triageStoreName=" + store.replace(" ", "%20"),
                              {"cids": cids, "attributeValuesList": [
                                  {"attributeName": "Classification", "attributeValue": classification},
                                  {"attributeName": "Action", "attributeValue": action}]})
        cls.step("pretriage", lambda: [triage("Default Triage Store", [10109], "Bug", "Fix Required"),
                                       triage("Default Triage Store", [10017], "False Positive", "Ignore"),
                                       triage("Default Triage Store", [10172], "Intentional", "Ignore"),
                                       triage("Release Triage Store", [10220], "Bug", "Fix Required")], "setup")
        w.write_filter("all.yaml", "name: all\nproject: envmon\nstreams: [envmon-main]\nlimit: 100\n")
        w.write_filter("audit.yaml", "name: audit\nstreams: [envmon-main]\nimpacts: [Audit]\nlimit: 100\n")
        w.write_filter("unspecified.yaml", "name: unspecified\nstreams: [envmon-main]\nimpacts: [Unspecified]\n")
        w.write_filter("misra15.yaml", "name: misra15\nstreams: [envmon-main]\n"
                                       "checkers: ['MISRA C-2012 Rule 15.*']\nlimit: 100\n")
        w.write_filter("release.yaml", "name: release\nstreams: [envmon-release]\ncheckers: [NULL_RETURNS]\n"
                                       "triage:\n  classification: [Unclassified]\n")
        w.write_filter("several.yaml", "name: several\nstreams: [envmon-main]\n"
                                       "checkers: [DIVIDE_BY_ZERO, NULL_RETURNS, UNUSED_VALUE]\n")
        cls.cases: Dict[str, Dict[str, Any]] = {}

        def case(name: str, args: List[str], want: Callable[[], List[int]]) -> None:
            def go() -> Dict[str, Any]:
                made = w.ct("new-run", "--repo", str(w.repo), *args, "--no-initial")
                if not made.get("ok"):
                    return {"new_run": made}
                result = search_to_file(w, made)
                issues = read_json(Path(made["issues_file"])) if result.ok else {}
                return {"new_run": made, "result": result, "issues": issues, "want": want()}
            cls.cases[name] = cls.step(f"case:{name}", go, "pretriage") or {}

        with Stage(cls.scene, "検索（14 通り）"):
            case("impact-high", ["--filter", "all.yaml", "--impact", "High"], lambda: expected(w, MAIN, 100, impacts=["High"]))
            case("impact-high-medium", ["--filter", "all.yaml", "--impact", "High", "--impact", "Medium"],
                 lambda: expected(w, MAIN, 100, impacts=["High", "Medium"]))
            case("impact-low", ["--filter", "all.yaml", "--impact", "Low"], lambda: expected(w, MAIN, 100, impacts=["Low"]))
            case("impact-audit", ["--filter", "audit.yaml"], lambda: expected(w, MAIN, 100, impacts=["Audit"]))
            case("impact-unspecified", ["--filter", "unspecified.yaml"], lambda: [])
            case("checker-exact", ["--filter", "all.yaml", "--checker", "NULL_RETURNS", "--checker", "OVERRUN"],
                 lambda: expected(w, MAIN, 100, checkers=["NULL_RETURNS", "OVERRUN"]))
            case("checker-wildcard", ["--filter", "all.yaml", "--checker", "NULL_*"],
                 lambda: expected(w, MAIN, 100, checkers=["NULL_*"]))
            case("checker-misra-15", ["--filter", "misra15.yaml"],
                 lambda: expected(w, MAIN, 100, checkers=["MISRA C-2012 Rule 15.*"]))
            case("classification-unclassified", ["--filter", "untriaged.yaml"],
                 lambda: expected(w, MAIN, 20, classification=["Unclassified"]))
            case("status-new", ["--filter", "several.yaml", "--status", "New"],
                 lambda: expected(w, MAIN, 100, checkers=["DIVIDE_BY_ZERO", "NULL_RETURNS", "UNUSED_VALUE"],
                                  status=["New"]))
            case("action-fix-required", ["--filter", "all.yaml", "--action", "Fix Required"],
                 lambda: expected(w, MAIN, 100, action=["Fix Required"]))
            case("limit-5", ["--filter", "all.yaml", "--limit", "5"], lambda: expected(w, MAIN, 5))
            case("release-store", ["--filter", "release.yaml"],
                 lambda: expected(w, "envmon-release", 100, checkers=["NULL_RETURNS"], classification=["Unclassified"]))
            case("classification-and-action", ["--filter", "several.yaml", "--classification", "False Positive",
                                               "--classification", "Intentional", "--action", "Ignore"],
                 lambda: expected(w, MAIN, 100, checkers=["DIVIDE_BY_ZERO", "NULL_RETURNS", "UNUSED_VALUE"],
                                  classification=["False Positive", "Intentional"], action=["Ignore"]))

    def check(self, name: str, limited: Optional[bool] = None) -> Dict[str, Any]:
        got = self.need(f"case:{name}")
        self.assertTrue(got["new_run"].get("ok"), got["new_run"])
        self.assertTrue(got["result"].ok, got["result"])
        cids = [i["cid"] for i in got["issues"]["issues"]]
        self.assertEqual(len(cids), len(set(cids)), "同じ CID が2回あります")
        self.assertEqual(cids, got["want"])
        if limited is not None:
            self.assertEqual(got["result"]["limited"], limited)
        return got

    def test_req102_impacts(self) -> None:
        for name in ("impact-high", "impact-high-medium", "impact-low", "impact-audit", "impact-unspecified"):
            with self.subTest(name):
                got = self.check(name)
                # 題材には Impact が Audit の警告がない（CERT も Low）。Audit と Unspecified は 0 件が正しい
                self.assertEqual(bool(got["want"]), name not in ("impact-audit", "impact-unspecified"), name)
        self.assertTrue(self.rec["case:impact-low"]["result"]["limited"])

    def test_req108_checkers_exact_and_wildcards(self) -> None:
        for name in ("checker-exact", "checker-wildcard"):
            with self.subTest(name):
                self.check(name)
        with self.subTest("checker-misra-15"):
            got = self.check("checker-misra-15", limited=True)  # 140 件のうち 100 件
            self.assertEqual(len(got["want"]), 100)

    def test_req108_triage_filters(self) -> None:
        with self.subTest("classification-unclassified"):
            got = self.check("classification-unclassified", limited=True)
            self.assertNotIn(10017, got["want"])  # ほかの人が False Positive にした
        for name in ("status-new", "action-fix-required", "classification-and-action"):
            with self.subTest(name):
                self.check(name)
        self.assertEqual(self.rec["case:action-fix-required"]["want"], [10109])
        self.assertEqual(self.rec["case:classification-and-action"]["want"], [10017, 10172])
        self.assertNotIn(10109, self.rec["case:status-new"]["want"])

    def test_req102_limit(self) -> None:
        got = self.check("limit-5", limited=True)
        self.assertEqual(got["new_run"]["limit"], 5)
        self.assertEqual(got["issues"]["limit"], 5)
        for value, ok in (("0", False), ("-3", False), ("101", False), ("100", True)):  # 1 から 100 まで（K-19）
            with self.subTest(limit=value):
                made = self.world.ct("new-run", "--repo", str(self.world.repo), "--filter", "all.yaml", "--limit",
                                     value, "--no-initial")
                self.assertEqual(bool(made.get("ok")), ok, made)
                if not ok:
                    self.assertIn("1 から 100", made.get("error", ""))

    def test_req108_other_stream_uses_its_own_triage_store(self) -> None:
        got = self.check("release-store")
        self.assertIn(10017, got["want"])  # Default のトリアージストアの分類は、Release には効かない
        self.assertNotIn(10220, got["want"])  # Release のトリアージストアで分類した


class ReleaseStreamWriteBackTest(FlowCase):
    """別のトリアージストアを使うストリーム（envmon-release は Release Triage Store）の実行の書き戻し。"""

    scene = "別のストア"

    @classmethod
    def flow(cls) -> None:
        cls.world = w = World("release")
        cls.step("setup", lambda: w.setup("envmon-release", initial=False, verify_default="none"))
        w.write_filter("fp.yaml", "name: fp\nproject: envmon\nstreams: [envmon-release]\ncheckers: [NULL_RETURNS]\n"
                                  "limit: 20\n")
        cls.step("new_run", lambda: w.ct("new-run", "--repo", str(w.repo), "--filter", "fp.yaml", "--no-initial"),
                 "setup")
        run_dir = (cls.rec.get("new_run") or {}).get("run_dir", "")
        cls.orch = Orchestrator(w, run_dir, Actors(w), cls.scene)
        cls.step("run", cls.orch.go, "new_run", stage="検索〜作業")
        cls.step("summary", lambda: w.ct("summary", "--run", run_dir), "run")
        cls.step("preview", lambda: w.ct("preview", "--run", run_dir), "summary")
        preview = cls.rec.get("preview") or {}
        cls.step("update", lambda: w.mcp().call("update_triage", {
            "repo_root": str(w.repo), "plan_file": preview.get("plan_file", ""),
            "confirmation_token": preview.get("confirmation_token", "")}), "preview", stage="update_triage")
        cls.step("writes", w.emu.writes, "update")

    def test_req106_deviation_is_written_to_the_triage_store_of_the_stream(self) -> None:
        update = self.need("update")
        self.assertTrue(update.ok, update)
        writes = self.need("writes")
        self.assertEqual(sorted(c for w in writes for c in w["cids"]), [10017, 10243])
        self.assertEqual({w["store"] for w in writes}, {"Release Triage Store"},
                         "envmon-release の逸脱が、別のストリームのトリアージストアに書き込まれました")
        release = self.world.emu.table("envmon-release")
        self.assertEqual(release[10017]["classification"], "False Positive")
        main = self.world.emu.table(MAIN)
        self.assertEqual(main[10017]["classification"], "Unclassified", "envmon-main のトリアージが変わりました")


# =================================================================================================
# 6. 異常系
# =================================================================================================


class FaultCase(FlowCase):
    """異常系の場面。試験ごとに、新しい実行で検索から始める。"""

    scene = "異常系"
    world: World

    @classmethod
    def flow(cls) -> None:
        cls.world = World(cls.world_name)
        cls.world.setup(MAIN, initial=False, verify_default="none")
        cls.world.write_filter("null.yaml", FILTER_NULL.format(stream=MAIN))
        cls.world.write_filter("nullwild.yaml", "name: nullwild\nstreams: [envmon-main]\ncheckers: ['NULL_*']\n"
                                                "limit: 20\n")
        cls.world.write_filter("misra15.yaml", "name: misra15\nstreams: [envmon-main]\n"
                                               "checkers: ['MISRA C-2012 Rule 15.*']\nlimit: 100\n")
        cls.world.write_filter("invalid.yaml", "name: invalid\nstreams: [envmon-main]\ncheckers: [NULL_RETURNS]\n"
                                               "triage:\n  classification: [Untested, Unclassified]\n")
        cls.rec["world"] = True
    world_name = "faults"

    def setUp(self) -> None:
        self.need("world")
        self.world.emu.reset()
        self.world.credentials()  # 正しい認証情報で動いているサーバー

    def tearDown(self) -> None:
        self.world.emu.reset()

    def start(self, filter_name: str = "null.yaml") -> Dict[str, Any]:
        made = self.world.ct("new-run", "--repo", str(self.world.repo), "--filter", filter_name, "--no-initial")
        self.assertTrue(made.get("ok"), made)
        return made

    def drive(self, made: Dict[str, Any], until_items: bool = True, actors: Optional[Actors] = None,
              name: str = "") -> Orchestrator:
        orch = Orchestrator(self.world, made["run_dir"], actors or Actors(self.world), self.scene)
        with Stage(self.scene, name or self._testMethodName):
            orch.final = orch.go(until=(lambda r: r.get("stage") == "items") if until_items else None)
        return orch

    def assertStoppedClearly(self, orch: Orchestrator, *phrases: str) -> str:
        final = orch.final
        self.assertTrue(final.get("stop"), f"止まっていません: {dict(final)}")
        text = " ".join(orch.says())
        for phrase in phrases:
            self.assertIn(phrase, text)
        self.assertNotIn("Expecting value", text)
        self.assertNotIn("Traceback", text)
        return text

    def assertReachedItems(self, orch: Orchestrator, made: Dict[str, Any], want: List[int]) -> None:
        self.assertFalse(orch.final.get("stop"), f"止まりました: {orch.says()}")
        self.assertEqual(orch.final.get("stage"), "items", dict(orch.final))
        issues = read_json(Path(made["issues_file"]))
        cids = [i["cid"] for i in issues["issues"]]
        self.assertEqual(cids, want)


class FaultTest(FaultCase):
    """止まるべきものは分かる文で止まり、続けるべきものは続き、resume で続きから再開できる。"""

    def null_cids(self) -> List[int]:
        return expected(self.world, MAIN, 20, checkers=["NULL_RETURNS"])

    def test_req108_wrong_key_401_stops_clearly_and_resume_continues(self) -> None:
        made = self.start()
        self.world.credentials(COV_AUTH_KEY="wrong-key")
        orch = self.drive(made, name="認証の誤り（401）")
        if capture_dir() is not None:
            opened = gui_open(self.world, made["run_dir"], "monitor")
            try:
                shots = capture([{"name": "e2e-fault-401-stopped-monitor", "full_page": False, "wait_ms": 1500,
                                  "url": opened["url"].split("#")[0] + f"#run={Path(made['run_dir']).name}&view=monitor"}])
                self.assertTrue(all(s["ok"] for s in shots["shots"]) and "error" not in shots, shots)
            finally:
                gui_close(opened["url"])
        self.assertStoppedClearly(orch, "認証に失敗しました（401）", "ユーザ名と認証キー", "警告を検索できませんでした")
        self.world.credentials()  # 利用者が環境変数を直して起動し直した
        back = self.world.ct("resume", "--run", made["run_dir"])
        self.assertTrue(back.get("ok"), back)
        orch = self.drive(made, name="認証の誤り（401）の resume")
        self.assertReachedItems(orch, made, self.null_cids())

    def test_req108_wrong_key_302_is_not_reported_as_success(self) -> None:
        self.world.emu.config(auth_failure="302")
        self.world.credentials(COV_AUTH_KEY="wrong-key")
        mcp = self.world.mcp()
        check = mcp.call("check_connection", {"repo_root": str(self.world.repo)})
        self.assertTrue(check.ok, check)
        self.assertFalse(check["ok"])
        self.assertNotIn("認証できました", check["result"])
        self.assertIn("リダイレクト", check["result"])
        made = self.start()
        orch = self.drive(made, name="認証の誤り（302）")
        self.assertStoppedClearly(orch, "リダイレクト", "coverity.url")

    def test_req108_no_permission_403_names_the_permission(self) -> None:
        made = self.start()
        self.world.credentials(COV_USER="viewer", COV_AUTH_KEY="emu-viewer-key-0001")
        orch = self.drive(made, name="権限なし（403）")
        self.assertStoppedClearly(orch, "403", "Access Web services")

    def test_req108_server_error_mid_search_continues_from_the_saved_offset(self) -> None:
        made = self.start("nullwild.yaml")
        emu = self.world.emu
        emu.inject({"path": "^/api/v2/issues/search$", "delay": 12, "count": 1})
        state: Dict[str, Any] = {}

        def on_search(n: int, result: Any) -> None:
            if n == 1:
                state["first"] = result
                state["since"] = emu.requests()["last"]
                emu.inject({"path": "^/api/v2/issues/search$", "status": 503, "count": 1,
                            "message": "Service Unavailable"})
        orch = self.drive(made, actors=Actors(self.world, on_search_call=on_search), name="5xx（検索の途中で 503）")
        self.assertFalse(state["first"]["done"])
        self.assertTrue(any("503" in s for s in orch.says()), orch.says())
        self.assertReachedItems(orch, made, expected(self.world, MAIN, 20, checkers=["NULL_*"]))
        later = [r for r in emu.requests(state["since"], "/api/v2/issues/search")["requests"]]
        offsets = [int(r["query"]["offset"][0]) for r in later]
        self.assertGreater(min(offsets), 0, "検索を最初からやり直しています")

    def test_req108_soap_fault_for_one_cid_is_fetched_again(self) -> None:
        made = self.start()
        self.world.emu.inject({"operation": "getStreamDefects", "soap_fault": "Internal error: please retry",
                               "count": 1})
        orch = self.drive(made, name="SOAP の Fault（1件）")
        self.assertEqual(orch.final.get("stage"), "items")
        run_dir = Path(made["run_dir"])
        self.assertTrue((run_dir / "agents" / "collector" / "paths-2.md").is_file(), "取り直していません")
        for issue in read_json(Path(made["issues_file"]))["issues"]:
            path = next(run_dir.glob(f"items/*/issue-{issue['cid']}.json"))
            self.assertTrue(read_json(path).get("events"), path)

    def test_req108_paths_not_available_goes_on_without_them(self) -> None:
        made = self.start()
        self.world.emu.inject({"operation": "getStreamDefects", "soap_fault": "Internal error"})
        orch = self.drive(made, name="警告経路の取得の失敗")
        self.assertEqual(orch.final.get("stage"), "items")  # 経路なしで作業に進む
        self.assertTrue((Path(made["run_dir"]) / "agents" / "collector" / "paths-2.md").is_file(), "取り直していません")
        launch = next(a for a in orch.final["actions"] if a["type"] == "launch")
        brief = Brief(Path(re.search(r"指示ファイル (\S+?) を読み", launch["prompt"]).group(1)))
        self.assertTrue(brief.no_paths)
        self.assertIn("警告経路を Coverity から取れませんでした", brief.text)
        # 利用者にも、経路を取れなかった警告があることが伝わる（tick の say）
        self.assertTrue(any("警告経路を取れなかった警告が 5 件" in s for s in orch.says()), orch.says())

    def test_req108_html_answer_is_a_clear_error(self) -> None:
        made = self.start()
        self.world.emu.inject({"path": "^/api/v2/issues/search$", "html": True})
        orch = self.drive(made, name="HTML の本文")
        self.assertStoppedClearly(orch, "JSON ではありません", "coverity.url")

    def test_req108_connection_dropped_once_continues(self) -> None:
        made = self.start()
        self.world.emu.inject({"path": "^/api/v2/issues/search$", "drop": "before_response", "count": 1})
        orch = self.drive(made, name="接続が途中で切れる")
        self.assertTrue(any("接続できません" in s for s in orch.says()), orch.says())
        self.assertReachedItems(orch, made, self.null_cids())

    def test_req108_unknown_value_is_left_out_of_the_search(self) -> None:
        made = self.start("invalid.yaml")
        since = self.world.emu.requests()["last"]
        orch = self.drive(made, name="知らない値（Invalid key）")
        self.assertReachedItems(orch, made, expected(self.world, MAIN, 100, checkers=["NULL_RETURNS"],
                                                     classification=["Unclassified"]))
        statuses = [r["status"] for r in self.world.emu.requests(since, "/api/v2/issues/search")["requests"]]
        self.assertIn(500, statuses)  # Invalid key provided: Untested

    def test_req108_overlapping_pages_keep_each_cid_once(self) -> None:
        self.world.emu.config(page_overlap=5)
        made = self.start("misra15.yaml")
        want = expected(self.world, MAIN, 100, checkers=["MISRA C-2012 Rule 15.*"])
        rows = self.world.emu.latest_cids(MAIN)
        overlap = [c for c in rows[95:100] if c in set(expected(self.world, MAIN, 10_000,
                                                               checkers=["MISRA C-2012 Rule 15.*"]))]
        self.assertTrue(overlap, "重なる行に条件に合う警告がなく、試験になっていません")
        result = search_to_file(self.world, made)
        self.assertTrue(result.ok, result)
        cids = [i["cid"] for i in read_json(Path(made["issues_file"]))["issues"]]
        self.assertEqual(len(cids), len(set(cids)), f"同じ CID が2回あります: {sorted(c for c in set(cids) if cids.count(c) > 1)}")
        self.assertEqual(cids, want)

    def test_req108_answer_without_total_rows_reads_every_page(self) -> None:
        self.world.emu.config(omit_total_rows=True)
        made = self.start("nullwild.yaml")
        result = search_to_file(self.world, made)
        self.assertTrue(result.ok, result)
        cids = [i["cid"] for i in read_json(Path(made["issues_file"]))["issues"]]
        self.assertEqual(cids, expected(self.world, MAIN, 20, checkers=["NULL_*"]))
        self.assertFalse(result["limited"])
        made = self.start("misra15.yaml")
        result = search_to_file(self.world, made)
        cids = [i["cid"] for i in read_json(Path(made["issues_file"]))["issues"]]
        self.assertEqual(cids, expected(self.world, MAIN, 100, checkers=["MISRA C-2012 Rule 15.*"]))
        self.assertTrue(result["limited"])

    def test_req108_resume_after_search_failed_twice_searches_again(self) -> None:
        made = self.start()
        self.world.emu.inject({"path": "^/api/v2/issues/search$", "status": 503, "message": "Service Unavailable"})
        orch = self.drive(made, name="5xx が続く")
        self.assertStoppedClearly(orch, "503", "警告を検索できませんでした")
        self.world.emu.inject()  # サーバーが戻った
        back = self.world.ct("resume", "--run", made["run_dir"])
        self.assertTrue(back.get("ok"), back)
        orch = self.drive(made, name="5xx が続いた後の resume")
        self.assertFalse(orch.final.get("stop"), f"resume してもすぐに止まります: {orch.says()}")
        self.assertReachedItems(orch, made, self.null_cids())


class SlowFaultTest(FaultCase):
    """時間のかかる異常系：とても遅い応答、http から https へのリダイレクト。"""

    world_name = "faults-slow"

    def test_req108_very_slow_answer_times_out_within_the_client_limit(self) -> None:
        made = self.start()
        self.world.emu.inject({"path": "^/api/v2/issues/search$", "delay": 17, "count": 1})
        actors = Actors(self.world)
        orch = self.drive(made, actors=actors, name="とても遅い応答（タイムアウト）")
        first = actors.search_calls[0]
        self.assertFalse(first.ok)
        self.assertIn("接続できません", first.error)
        self.assertLess(first.seconds, 30, "Copilot の既定の 30 秒を超えました")
        self.assertReachedItems(orch, made, expected(self.world, MAIN, 20, checkers=["NULL_RETURNS"]))

    def test_req108_http_redirected_to_https_is_a_clear_error(self) -> None:
        tls = World("faults-tls", tls=True)
        try:
            tls.emu.config(http_to_https_redirect=301)
            tls.setup(MAIN, initial=False, verify_default="none", url=tls.emu.info["http_url"])
            tls.write_filter("null.yaml", FILTER_NULL.format(stream=MAIN))
            mcp = tls.mcp()
            check = mcp.call("check_connection", {"repo_root": str(tls.repo)})
            self.assertTrue(check.ok, check)
            self.assertFalse(check["ok"])
            self.assertIn("リダイレクト", check["result"])
            self.assertIn("https://", check["result"])
            made = tls.ct("new-run", "--repo", str(tls.repo), "--filter", "null.yaml", "--no-initial")
            orch = Orchestrator(tls, made["run_dir"], Actors(tls), self.scene)
            with Stage(self.scene, "http から https へのリダイレクト"):
                orch.final = orch.go(until=lambda r: r.get("stage") == "items")
            self.assertStoppedClearly(orch, "リダイレクト", "https", "coverity.url")
        finally:
            tls.close()


if __name__ == "__main__":
    unittest.main()
