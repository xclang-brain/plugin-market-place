"""The four agent definitions, in both forms, against the contract and the real triage.py.

No model. An agent text is followed word for word by a small model, so a user loses a run when a text
names a reply field or a command the script does not give, lets a loop go on after an error, or lets a
shell change what a reviewer records. The scripted agent of tests/flow calls the real MCP server. That the
files exist is in test_structure.py, the wording rules are in test_docs.py.

    python3 tests/run.py --only agents
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_structure import PLUGIN, frontmatter

sys.path.insert(0, str(PLUGIN / "tests" / "flow"))
import flowlib  # noqa: E402
from flowlib import Agent  # noqa: E402

APPLY = PLUGIN / "skills" / "coverity-apply" / "scripts" / "apply.py"
MCP = ["coverity_search_issues", "coverity_get_issue_details", "coverity_update_triage"]
TEAM = ["coverity-triage-kit:triage-worker", "coverity-triage-kit:triage-reviewer", "coverity-triage-kit:triage-integrator"]
DENY = "依頼できるのは、coverity-triage-kit:triage-worker、triage-reviewer、triage-integrator だけです。"
# name -> (description, Copilot CLI keys, development form: model and tools)
AGENTS = {
    "triage-worker": (
        "設定した条件に合う Coverity の警告をすべて取得し、CID ごとに修正コードと逸脱コメントを作成して、最後まで処置する。"
        "「条件に合う警告をすべて処置して」と依頼するときに使う。",
        {"tools": ["read", "edit", "search", "execute", "skill", "ask_user"], "mcp": True,
         "model": ["gpt-6-luna", "claude-haiku-5.5"]},
        ("haiku", "Read, Grep, Glob, Edit, Write, Bash, Skill, AskUserQuestion", True)),
    "triage-orchestrator": (
        "設定した条件に合う Coverity の警告を、複数のエージェントで並列に処置し、同じ原因の CID を統合して、"
        "すべての結果を別のエージェントに検証させる。`copilot --agent` で起動したメインの会話として使う。",
        {"tools": ["agent", "read", "search", "execute", "skill", "ask_user"], "mcp": True,
         "model": ["claude-sonnet-5.5", "gpt-6.1-sol"], "disable-model-invocation": True, "hooks": True},
        ("sonnet", f"Agent({', '.join(TEAM)}), Read, Grep, Glob, Bash, Skill, AskUserQuestion", True)),
    "triage-reviewer": (
        "別のエージェントが作成した CID の結果（判定、修正コード、逸脱コメント）を、コードを読んで検証し、"
        "合格・要確認・差し戻しを記録する。検証を依頼されたときに使う。",
        {"tools": ["read", "search", "execute"], "model": ["claude-sonnet-5.5", "gpt-6.1-sol"], "user-invocable": False},
        ("sonnet", "Read, Grep, Glob, Bash", False)),
    "triage-integrator": (
        "処置済みの CID から、同じ原因のものを見つけて1つに統合する。統合を依頼されたときに使う。",
        {"tools": ["read", "search", "execute"], "model": ["gpt-6-luna", "claude-haiku-5.5"], "user-invocable": False},
        ("haiku", "Read, Grep, Glob, Bash", False)),
}
REQUESTS = [  # contracts 7.2: exactly these three texts
    "Coverity の警告を処置してください。次のコマンドで CID を受け取り、スキル coverity-triage の手順で処置して保存します。"
    "保存の応答の next のコマンドを続けて実行し、done が true の応答で終了します。質問はしません。\n"
    "<next のコマンド>\n終了したら、処置した CID と、保存できなかった CID を1行で報告してください。",
    "処置済みの CID から、同じ原因のものを統合してください。候補（merge_candidates）と統合のコマンド（merge_command）は、"
    "次のコマンドで取得します。\n<状態のコマンド>\n"
    "終了したら、統合した CID、作り直しにした CID、統合しなかった候補とその理由を報告してください。",
    "次の CID の結果を検証してください。\nCID: <cids>\n修正前のコード: <work_dir>\n"
    "CID ごとのファイル: <run_dir>/cid/<CID>/result.json、fix.patch、issue.json、state.json\n"
    "検証の結果は、CID ごとに次のコマンドで記録します。\n<review のコマンド>\n"
    "終了したら、CID ごとに結果（合格・要確認・差し戻し）を1行ずつ報告してください。",
]
# Words in backticks that are not a field of a triage.py or plan reply: commands, values, and the fields of
# replies this test does not make (log: a job that still runs or failed; call: apply.py apply).
WORDS = {*MCP, *"start next save finish status review merge plan apply log call".split(),
         *"true false null pass flag rework unverified".split()}


def read(name: str, copilot: bool) -> tuple[dict, str]:
    """(frontmatter, body) of one form of an agent."""
    return frontmatter(PLUGIN / (f"com.github.copilot/agents/{name}.agent.md" if copilot else f"agents/{name}.md"))


def line_of(name: str, *rest) -> str:
    return flowlib.command(name, *rest)


def names(value, found: set) -> set:
    for key, inner in value.items() if isinstance(value, dict) else []:
        found.add(key)
        names(inner, found)
    for inner in value if isinstance(value, list) else []:
        names(inner, found)
    return found


class Texts(unittest.TestCase):
    def test_both_forms_have_one_body_and_the_frontmatter_of_the_contract(self):
        for name, (description, copilot, (model, tools, mcp)) in AGENTS.items():
            with self.subTest(agent=name):
                (cli, cli_body), (dev, dev_body) = read(name, True), read(name, False)
                self.assertEqual(cli_body, dev_body, "the body is the same in both forms")
                want = {"name": name, "description": description, **{k: v for k, v in copilot.items() if k != "mcp"}}
                want["tools"] = copilot["tools"] + [f"coverity-connect/{t}" for t in MCP if copilot.get("mcp")]
                self.assertEqual({**cli, "hooks": bool(cli.get("hooks"))} if "hooks" in cli else cli, want)
                both = [f"mcp__{server}__{t}" for t in MCP for server in
                        ("plugin_coverity-triage-kit_coverity-connect", "coverity-connect")] if mcp else []
                self.assertEqual((dev["name"], dev["description"], dev["model"], dev["tools"]),
                                 (name, description, model, ", ".join([tools, *both])))

    def test_the_orchestrator_sends_the_three_request_texts_of_the_contract(self):
        body = read("triage-orchestrator", True)[1].split("## 依頼文")[1]
        self.assertEqual(re.findall(r"```text\n(.*?)\n```", body, re.S), REQUESTS)

    def test_the_hook_lets_only_the_three_agents_through(self):
        hook = read("triage-orchestrator", True)[0]["hooks"]["PreToolUse"][0]
        self.assertEqual((hook["matcher"], sorted(set(hook) & {"bash", "powershell"})), ("Agent", ["bash", "powershell"]))
        self.assertEqual(hook["powershell"].count(hook["bash"].split("printf '%s\\n' ")[1].strip()), 1,
                         "PowerShell prints the same answer")
        # Each field in its shell, as Copilot CLI runs it. On Windows, bash in System32 starts WSL: it is no shell.
        bash = shutil.which("bash")
        shells = [[bash, "--norc", "--noprofile", "-c", hook["bash"]]] if bash and "system32" not in bash.lower() else []
        shells += [[shell, "-NoProfile", "-NoLogo", "-Command", hook["powershell"]]
                   for shell in (shutil.which("pwsh"), shutil.which("powershell")) if shell]
        if not shells:
            self.skipTest("bash も PowerShell もありません")
        env = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}  # not pwsh's, for 5.1
        for shell in shells:
            for agent in [*TEAM, "general-purpose", "explore", "coverity-triage-kit:triage-worker-2", ""]:
                asked = json.dumps({"tool_input": {"agent_type": agent, "prompt": f'"agent_type":"{TEAM[0]}"'}},
                                   ensure_ascii=False, separators=(",", ":"))
                done = subprocess.run(shell, input=asked.encode("utf-8"), capture_output=True, timeout=60, env=env)
                answer = json.loads(done.stdout) if done.stdout.strip() else {}
                want = {} if agent in TEAM else {"permissionDecision": "deny", "permissionDecisionReason": DENY}
                self.assertEqual((done.returncode, answer), (0, want), (shell[0], agent))

    def test_an_error_ends_a_loop_and_a_shell_does_not_change_a_point(self):
        """What the bodies say where a word-for-word reader would go wrong (see Replies for the replies)."""
        for name in ("triage-worker", "triage-orchestrator"):
            body = read(name, True)[1]
            again = [line for line in body.splitlines() if "`start` のコマンドの間" in line]
            self.assertTrue(again and all("`ok` が `true` で" in line for line in again),
                            f"{name}: start is repeated only while ok is true")
            self.assertIn("`ok` が `false` で `problems` がないとき、または MCP のツールがエラーを返したとき", body)
        self.assertIn("保存が拒否されたとき（`ok` が `false` で `problems` があるとき）", read("triage-worker", True)[1])
        for name in ("triage-reviewer", "triage-integrator"):  # inside "…" a shell expands ` and $
            self.assertIn('`"`、`` ` ``、`$` を含まない1文', read(name, True)[1], name)


class Replies:
    """The replies of the real scripts that the bodies speak of; GitReplies and SvnReplies run them."""

    def setUp(self):
        if self.kind == "svn" and not (shutil.which("svn") and shutil.which("svnadmin")):
            self.skipTest("svn コマンドがありません")
        try:  # a space and Japanese in every path; svn on a Windows PC that is not set to Japanese cannot take it
            "テスト".encode("mbcs" if self.kind == "svn" and os.name == "nt" else "utf-8")
            prefix = "agents テスト "
        except UnicodeEncodeError:
            prefix = "agents test "
        self.top = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        self.addCleanup(flowlib.remove_tree, self.top)
        self.repo = flowlib.make_repo(self.kind, self.top)
        self.seen = []

    def run_line(self, line: str) -> dict:
        self.seen.append(Agent(self.repo).run(line))
        return self.seen[-1]

    def begin(self, *flags) -> dict:
        """Start as the body says: its own start line, then the line under next while that is a start line."""
        agent = Agent(self.repo)
        reply = agent.run(line_of("start", "--repo", f'"{self.repo.as_posix()}"', *flags))
        while " start --repo " in reply["next"][0]:
            self.seen.append(reply)
            reply = agent.run(agent.act(reply))
        agent.act(reply)
        self.seen.append(reply)
        return reply

    def work(self, line: str, refuse: bool = False) -> int:
        """A worker with a next line: next and save until next has no CID. Returns how many CIDs it got."""
        agent, got = Agent(self.repo), 0
        while True:
            given = self.run_line(line)
            if given["cid"] is None:
                return got
            got += 1
            if refuse:  # save without a result: refused with problems, the same save line again
                refused = self.run_line(given["next"][0])
                self.assertEqual((refused["ok"], refused["next"], bool(refused["problems"])), (False, given["next"], True))
            saved = self.run_line(agent.act(given))
            self.assertTrue(saved["ok"], saved)
            if refuse:  # save of a saved CID: no problems, so the worker tells the sentence and ends
                again, refuse = self.run_line(given["next"][0]), False
                self.assertEqual((again["ok"], "problems" in again, again["next"]), (False, False, [line]))

    def test_every_field_and_line_the_bodies_name_is_in_a_reply(self):
        ready = self.begin("--review")
        run = Path(ready["run_dir"])
        status_line = line_of("status", "--run", f'"{run.as_posix()}"')  # the one line the orchestrator builds
        finish_line = line_of("finish", "--run", f'"{run.as_posix()}"')
        self.assertIn("pending", self.run_line(finish_line))
        for n, line in enumerate(ready["next"]):
            self.assertRegex(line, r' next --run ".*" --slot \d+ --batch \d+$')
            self.work(line, refuse=n == 0)
        status = self.run_line(status_line)
        merge = status["merge_command"]
        self.assertTrue(merge.endswith(" --leader <CID> --members <CID,CID> [--rework]"), merge)
        group = status["merge_candidates"][0]["cids"]
        point = "src/reader.c:58 同じ変換が3行にある"
        merged = self.run_line(merge.replace("<CID,CID>", ",".join(map(str, group[1:]))).replace("<CID>", str(group[0]))
                               .replace("[--rework]", f'--rework --point "{point}"'))
        self.assertTrue(merged["ok"], merged)
        for line in self.run_line(status_line)["next"]:  # the leader goes back to a worker
            self.work(line)
        self.assertIn("unreviewed", self.run_line(finish_line))
        status = self.run_line(status_line)
        template = status["next"][0]
        self.assertTrue(template.endswith(' --cid <CID> --outcome <pass|flag|rework> [--cap <high|medium|low>] '
                                          '[--point "<ファイル:行 指摘>"]'), template)
        example = re.search(r"差し戻し（(.*?)）\n", read("triage-reviewer", True)[1]).group(1)  # the body's own example
        for batch in status["review_batches"]:
            self.assertTrue(Path(batch["work_dir"]).is_dir())
            for cid in batch["cids"]:
                folder = run / "cid" / str(cid)
                self.seen += [json.loads((folder / name).read_bytes()) for name in ("result.json", "issue.json", "state.json")]
                line = template.replace("<CID>", str(cid)).replace("<pass|flag|rework>", "pass").replace(
                    "[--cap <high|medium|low>]", "--cap medium").replace('[--point "<ファイル:行 指摘>"]', f'--point "{example}"')
                self.assertTrue(self.run_line(line)["ok"])
                self.assertEqual(json.loads((folder / "review.json").read_bytes())["points"], [example])
        unverified = re.sub(r" \[[^\]]*\]", "", template).replace("<CID>", str(group[0])).replace(
            "<pass|flag|rework>", "unverified")  # what the orchestrator records itself
        self.assertEqual(self.run_line(unverified).get("outcome"), "unverified")
        done = self.run_line(self.run_line(status_line)["next"][0])  # finish gives the plan line: no agent builds it
        self.assertEqual((done["ok"], done["next"], Path(done["summary_file"]).is_file()), (True, [
            f'uv run --native-tls --quiet "{APPLY.as_posix()}" plan --repo "{self.repo.as_posix()}" --run "{run.as_posix()}"'], True))
        plan = self.run_line(done["next"][0])  # and the apply line waits for the OK: nothing more is run without it
        self.assertEqual((plan["next"], len(plan["after_ok"]), " apply --repo " in plan["after_ok"][0]), ([], 1, True))
        given = names(self.seen, set()) | WORDS
        for name in AGENTS:
            named = set(re.findall(r"`([a-z_]+)`", read(name, True)[1]))
            self.assertEqual(sorted(named - given), [], f"{name} names what no reply has")

    def test_one_agent_continues_a_folder_that_was_started_with_review(self):
        self.begin("--review")
        got = []
        for _ in range(10):  # the worker: its start line again after every done with an empty next
            reply = self.begin("--slots", 1)
            if " next --run " not in reply["next"][0]:
                break
            self.assertIn("--batch", reply["next"][0])
            got.append(self.work(reply["next"][0]))
            self.assertEqual((self.seen[-1]["done"], self.seen[-1]["next"]), (True, []))
        self.assertEqual(sum(got), len(flowlib.CIDS), "every CID, and the worker ends")
        self.assertIn(" review --run ", reply["next"][0], "then the review line, which the worker does not run")

    def test_a_start_that_keeps_failing_has_the_start_line_under_next(self):
        """So the bodies repeat start only while ok is true: ok false without problems ends the agent."""
        config = self.repo / ".coverity-triage" / "config.yaml"
        if self.kind == "git":  # the work copies cannot be made
            config.write_bytes(config.read_bytes() + b"base_branch: nosuch\n")
        else:
            shutil.move(self.top / "svnrepo", self.top / "svnrepo.gone")
        start, agent = line_of("start", "--repo", f'"{self.repo.as_posix()}"', "--slots", 1), Agent(self.repo)
        for _ in range(5):
            agent.act(self.run_line(start))  # the search call, when the reply asks for it
        # The job fails at once: a start may see that before it has asked for the list, so that reply comes at any place.
        failed = [reply for reply in self.seen if "fetch" not in reply]
        self.assertGreaterEqual(len(failed), 4)
        for reply in failed:
            self.assertEqual((reply["ok"], "problems" in reply, reply["next"], "log" in reply), (False, False, [start], True))


class GitReplies(Replies, unittest.TestCase):
    kind = "git"


class SvnReplies(Replies, unittest.TestCase):
    kind = "svn"


if __name__ == "__main__":
    unittest.main()
