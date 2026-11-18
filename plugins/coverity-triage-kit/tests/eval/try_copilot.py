# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""実機の GitHub Copilot CLI でしか確かめられないことを、1回の実行で確かめる。

    uv run --native-tls --quiet tests/eval/try_copilot.py [--skip-screen] [--model <名前>]
    （Windows では try-copilot.cmd をダブルクリックしても実行できます）

このフォルダーのプラグインを --plugin-dir で読み込みます（インストール済みのプラグインは変更しません）。
Coverity は tests/coverity_emulator（このスクリプトの中で動く、テスト用の代わりのサーバー）です。
本物の Coverity には接続しません。モデルを使うため、Copilot の利用量（3回の依頼）を消費します。

  1. プラグインの MCP サーバーが Copilot CLI で起動し、Coverity に接続できるか（自動）
  2. Coverity に書き込む前の確認（MCP の elicitation）に、Copilot CLI が対応しているか（自動）
     書き込みのツールを許可した状態で、対話のない実行（-p）から書き込みを依頼します。対応していれば、
     Copilot CLI が確認を断り、何も書き込まれません。対応していなければ、確認なしで書き込まれます。
  3. 確認が画面に表示され、承認すると登録されるか（Copilot CLI の画面を、人が操作します）

結果は、端末と、作業フォルダーの report.md に書きます。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "tests"))
from coverity_emulator import Emulator  # noqa: E402

STREAM = "Sample-main"
SAMPLE = PLUGIN / "tests" / "fixtures" / "sample" / "sample-target" / ".coverity-triage" / "fake-issues.yaml"
NAME = "coverity-triage-kit"
DECLINED = "利用者が Coverity への登録を承認しなかったため"  # the sentence of the server when the person says no
TIMEOUT = 600  # seconds one run of copilot -p may take


def entry(cid: int) -> dict:
    return {"cids": [cid], "classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
            "comment": f"誤検知。Copilot CLI の確認の試験（CID {cid}）。"}


class Check:
    def __init__(self, args):
        self.args, self.results = args, []
        self.copilot = shutil.which("copilot")
        self.top = Path(tempfile.mkdtemp(prefix="coverity-copilot-check-")).resolve()
        self.repo = self.top / "repo"
        self.env = dict(os.environ)

    # ---- the place -------------------------------------------------------------------------

    def prepare(self, emulator: Emulator) -> None:
        (self.repo / ".coverity-triage").mkdir(parents=True)
        (self.repo / ".coverity-triage" / "config.yaml").write_text(
            f'coverity:\n  url: "{emulator.url}"\n  stream: "{STREAM}"\n', encoding="utf-8")
        # the dummy credentials of the emulator; the proxy of the PC is not used for it
        local = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
        self.env.update(COV_USER=emulator.user, COV_AUTH_KEY=emulator.key, NO_PROXY=local, no_proxy=local)

    def entries(self, name: str, cids: list[int]) -> str:
        """A result folder with the entries file and its .sha256, as apply.py writes them after the OK."""
        folder = self.top / "coverity-triage-out" / name
        folder.mkdir(parents=True)
        file = folder / "coverity-entries.json"
        file.write_bytes(json.dumps({"stream": STREAM, "entries": [entry(cid) for cid in cids]},
                                    ensure_ascii=False, indent=2).encode("utf-8"))
        Path(f"{file}.sha256").write_text(hashlib.sha256(file.read_bytes()).hexdigest() + "\n", encoding="ascii")
        return file.as_posix()

    # ---- copilot ---------------------------------------------------------------------------

    def common(self) -> list[str]:
        return [self.copilot, "--plugin-dir", str(PLUGIN), "-C", str(self.repo), "--disable-builtin-mcps",
                *(["--model", self.args.model] if self.args.model else [])]

    def ask(self, prompt: str, tool: str) -> tuple[int, str]:
        """copilot -p with one tool of the plugin allowed: (exit code, every text of its JSONL output)."""
        command = [*self.common(), "--trust-folder", "--no-ask-user", "--output-format", "json",
                   "--allow-tool", f"coverity-connect({tool})", "-p", prompt]
        try:
            proc = subprocess.run(command, env=self.env, capture_output=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return -1, f"{TIMEOUT} 秒以内に終了しませんでした"
        lines = []
        for raw in (proc.stdout + b"\n" + proc.stderr).decode("utf-8", "replace").splitlines():
            try:  # JSON escapes Japanese: the text is compared as it reads
                lines.append(json.dumps(json.loads(raw), ensure_ascii=False))
            except ValueError:
                lines.append(raw)
        return proc.returncode, "\n".join(lines)

    def record(self, title: str, verdict: str, detail: str, log: str = "") -> None:
        self.results.append((title, verdict, detail))
        mark = {"合格": "○", "不合格": "×"}.get(verdict, "△")
        print(f"\n{mark} {title}：{verdict}\n  {detail}", flush=True)
        if log:
            path = self.top / f"{len(self.results)}-copilot.log"
            path.write_text(log, encoding="utf-8")
            print(f"  Copilot CLI の出力: {path}", flush=True)

    # ---- the checks ------------------------------------------------------------------------

    def connection(self, emulator: Emulator) -> bool:
        print("\n[1/3] MCP サーバーの起動と、Coverity への接続を確かめています（1〜2分）…", flush=True)
        code, log = self.ask(f"coverity-connect の coverity_check_connection ツールを、repo_root に {self.repo.as_posix()} "
                             "を指定して1回だけ呼び出し、応答の message をそのまま表示してください。ほかのことはしないでください。",
                             "coverity_check_connection")
        reached = any("/api/v2/streams" in request for request in emulator.requests)
        if reached:
            self.record("1. MCP サーバーの起動と Coverity への接続", "合格", "Copilot CLI からツールを呼び出し、Coverity に接続できました", log)
        elif re.search(r"log ?in|authenticat|認証", log, re.I) and code != 0:
            self.record("1. MCP サーバーの起動と Coverity への接続", "不合格",
                        "Copilot CLI にログインしていない可能性があります。copilot login を実行してから、もう一度実行してください", log)
        else:
            self.record("1. MCP サーバーの起動と Coverity への接続", "不合格",
                        f"Coverity への接続がありませんでした（終了コード {code}）。出力を確認してください", log)
        return reached

    def no_dialog_run(self, emulator: Emulator) -> None:
        print("\n[2/3] 書き込みの前の確認に、Copilot CLI が対応しているかを確かめています（1〜2分）…", flush=True)
        file = self.entries("check-2", [20004])
        before = len(emulator.writes)
        code, log = self.ask(f"coverity-connect の coverity_update_triage ツールを、repo_root に {self.repo.as_posix()}、"
                             f"entries_file に {file} を指定して1回だけ呼び出し、応答をそのまま表示してください。"
                             "エラーになっても、もう一度呼び出さないでください。ほかのことはしないでください。",
                             "coverity_update_triage")
        wrote = len(emulator.writes) - before
        title = "2. 書き込みの前の確認（elicitation）への対応"
        if wrote:
            self.record(title, "不合格", "ツールを許可した状態で、確認なしに Coverity に書き込まれました。"
                                       "この Copilot CLI は、サーバーからの確認に対応していません（許可の確認だけが歯止めです）", log)
        elif DECLINED in log:
            self.record(title, "合格", "Copilot CLI がサーバーの確認を受け付け、対話のない実行なので断りました。"
                                     "Coverity には何も書き込まれていません", log)
        else:
            self.record(title, "判定できず", f"ツールが呼び出されなかったようです（終了コード {code}）。出力を確認してください", log)

    def screen(self, emulator: Emulator) -> None:
        print("\n[3/3] 確認の画面を、実際に操作して確かめます。", flush=True)
        file = self.entries("check-3", [20005, 20006])
        print(f"""
  これから Copilot CLI が開き、Coverity への登録を依頼します。次のとおりに操作してください。

    1. フォルダーを信頼するかを聞かれたら、「Yes」を選びます。
    2. 登録の確認（「Coverity（ストリーム {STREAM}）に、逸脱コメント 2 件を登録します。CID: 20005, 20006 …」）が
       表示されたら、内容を読み、「登録する」を選んで承認します。
    3. 結果が表示されたら、/exit と入力して終了します。

  書き込みのツールは、あらかじめ許可しています（「このセッションでは許可」を選んだ状態と同じです）。
  そのため、Copilot CLI の許可の確認は表示されません。表示されるのは、サーバーからの確認だけのはずです。
""", flush=True)
        input("  準備ができたら Enter を押してください…")
        before = len(emulator.writes)
        prompt = (f"coverity-connect の coverity_update_triage ツールを、repo_root に {self.repo.as_posix()}、"
                  f"entries_file に {file} を指定して1回だけ呼び出し、応答をそのまま表示してください。"
                  "エラーになっても、もう一度呼び出さないでください。")
        subprocess.run([*self.common(), "--allow-tool", "coverity-connect(coverity_update_triage)", "-i", prompt], env=self.env)
        wrote = len(emulator.writes) - before
        seen = yes_no("\n  登録の確認（件数と CID）は表示されましたか？")
        title = "3. 確認の画面の表示と、承認したときの登録"
        if seen and wrote == 2:
            self.record(title, "合格", "確認が表示され、承認すると 2 件が登録されました")
        elif seen:
            self.record(title, "要確認", f"確認は表示されましたが、登録は {wrote} 件でした（承認しなかった場合は、これで正常です）")
        elif wrote:
            self.record(title, "不合格", f"確認が表示されないまま、{wrote} 件が登録されました")
        else:
            self.record(title, "不合格", "確認が表示されず、登録もされませんでした")

    # ---- the report ------------------------------------------------------------------------

    def report(self, version: str) -> Path:
        lines = ["# Copilot CLI での確認の結果", "",
                 f"- 日時: {datetime.now().isoformat(timespec='seconds')}",
                 f"- Copilot CLI: {version}", f"- OS: {sys.platform}、Python {sys.version.split()[0]}",
                 f"- プラグイン: {PLUGIN.as_posix()}（{git_head()}）", "",
                 "| 確認 | 結果 | 内容 |", "| --- | --- | --- |",
                 *(f"| {title} | {verdict} | {detail} |" for title, verdict, detail in self.results)]
        path = self.top / "report.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def yes_no(question: str) -> bool:
    while True:
        answer = input(f"{question} [y/n]: ").strip().lower()
        if answer in ("y", "yes", "はい"):
            return True
        if answer in ("n", "no", "いいえ"):
            return False


def git_head() -> str:
    try:
        out = subprocess.run(["git", "-C", str(PLUGIN), "log", "-1", "--format=%h %s"], capture_output=True, timeout=30)
        return out.stdout.decode("utf-8", "replace").strip() or "git の情報なし"
    except (OSError, subprocess.TimeoutExpired):
        return "git の情報なし"


def installed(copilot: str) -> str:
    """The name of an installed coverity-triage-kit ('' when there is none): it would be loaded beside the one of this folder."""
    out = subprocess.run([copilot, "plugin", "list"], capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    mine = out.split("Built-in plugins")[0]
    found = re.search(rf"{NAME}(@[\w.-]+)?", mine)
    return found[0] if found else ""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="try_copilot.py", description="実機の Copilot CLI でしか確かめられないことを確かめる")
    parser.add_argument("--skip-screen", action="store_true", help="画面を操作する確認（3）を行わない")
    parser.add_argument("--model", help="Copilot CLI のモデル（既定は Copilot CLI の設定のまま）")
    args = parser.parse_args()
    check = Check(args)
    if not check.copilot:
        print("copilot コマンドが見つかりません。GitHub Copilot CLI をインストールしてください")
        return 1
    version = subprocess.run([check.copilot, "version"], capture_output=True, timeout=60).stdout.decode(
        "utf-8", "replace").strip().splitlines()[0:1] or ["不明"]
    print(f"Copilot CLI: {version[0]}\nプラグイン: {PLUGIN.as_posix()}（{git_head()}）\n作業フォルダー: {check.top.as_posix()}")
    other = installed(check.copilot)
    disabled = False
    if other:
        print(f"\nインストール済みの {other} があります。2つが同時に読み込まれないように、確認の間だけ無効にして、終わったら有効に戻します。")
        if not yes_no("よろしいですか？"):
            return 1
        disabled = subprocess.run([check.copilot, "plugin", "disable", other], capture_output=True, timeout=60).returncode == 0
        if not disabled:
            print(f"{other} を無効にできませんでした。copilot plugin disable {other} を実行してから、もう一度実行してください")
            return 1
    try:
        with Emulator(SAMPLE) as emulator:
            check.prepare(emulator)
            if check.connection(emulator):
                check.no_dialog_run(emulator)
                if not args.skip_screen:
                    check.screen(emulator)
            time.sleep(0.5)
    finally:
        if disabled:
            subprocess.run([check.copilot, "plugin", "enable", other], capture_output=True, timeout=60)
            print(f"\n{other} を有効に戻しました。")
    path = check.report(version[0])
    print(f"\n結果: {path.as_posix()}\nこのファイルの内容を、開発者に送ってください。")
    return 0 if all(verdict == "合格" for _, verdict, _ in check.results) else 1


if __name__ == "__main__":
    sys.exit(main())
