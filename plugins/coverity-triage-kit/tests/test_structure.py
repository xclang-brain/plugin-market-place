"""The plugin as Copilot CLI loads it: manifests, MCP registration, skills, agents, settings keys, MCP tools.

The other plugin-level tests take PLUGIN, load(), files(), frontmatter(), setup_init(), settings_keys() and
mcp_tools() from this file.

    python3 tests/run.py --only structure
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parents[1]
NAME = "coverity-triage-kit"
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/"
MANIFEST_KEYS = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license",
                 "keywords", "extensions"}  # Agent Plugins 1.0.0
SKILLS = ["coverity-apply", "coverity-gui", "coverity-setup", "coverity-triage"]
AGENTS = ["triage-integrator", "triage-orchestrator", "triage-reviewer", "triage-worker"]
# docs/specs/requirements.md, section 5
SETTINGS = ["coverity.url", "coverity.stream", "conditions.checkers", "conditions.impacts", "conditions.classification",
            "conditions.limit", "output_dir", "parallel", "base_branch", "build.command", "build.check_fixes",
            "build.initial_analysis", "build.analyze_args"]
TOOLS = ["coverity_check_connection", "coverity_search_issues", "coverity_get_issue_details", "coverity_update_triage"]


def load(name: str) -> dict:
    return json.loads((PLUGIN / name).read_text(encoding="utf-8"))


def files() -> list[Path]:
    """The files of the plugin that git tracks or would track, as paths below PLUGIN."""
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=PLUGIN,
                            capture_output=True, check=True).stdout.decode("utf-8")
    return [Path(name) for name in listed.split("\0") if name and (PLUGIN / name).is_file()]


def frontmatter(path: Path) -> tuple[dict, str]:
    """(frontmatter, body) of a SKILL.md or of an agent."""
    head, body = re.match(r"---\n(.*?)\n---\n(.*)", path.read_text(encoding="utf-8"), re.S).groups()
    return yaml.safe_load(head), body


def setup_init() -> tuple[dict, dict]:
    """setup.py init in a new repository, without credentials: (the settings file it writes, its reply)."""
    script = PLUGIN / "skills" / "coverity-setup" / "scripts" / "setup.py"
    env = {key: value for key, value in os.environ.items() if key not in ("COV_USER", "COV_AUTH_KEY")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as repo:
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
        done = subprocess.run([sys.executable, str(script), "init", "--repo", repo, "--stream", "app-main", "--url",
                               "https://coverity.example.co.jp:8443"], capture_output=True, env=env)
        settings = yaml.safe_load((Path(repo) / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8"))
    return settings, json.loads(done.stdout.decode("utf-8"))


def settings_keys() -> list[str]:
    """The keys of the settings file, in its order: "coverity.url", ..., "output_dir", ..."""
    keys = []
    for key, value in setup_init()[0].items():
        keys += [f"{key}.{inner}" for inner in value] if isinstance(value, dict) else [key]
    return keys


def mcp_tools() -> list[str]:
    """The tools the MCP server registers, read from its source ("" for a tool without name=)."""
    source = "".join(path.read_text(encoding="utf-8") for path in sorted(PLUGIN.glob("mcp-servers/*/src/**/*.py")))
    return re.findall(r'@mcp\.tool\((?:[^)]*?name="(\w+)")?', source)


class Structure(unittest.TestCase):
    def test_both_manifests_are_valid_and_agree(self):
        copilot, development = load("plugin.json"), load(".claude-plugin/plugin.json")
        # Copilot CLI reads the plugin.json with this $schema before .claude-plugin/plugin.json.
        self.assertEqual(copilot.get("$schema"), SCHEMA + "plugin.schema.json")
        self.assertEqual(sorted(set(copilot) - MANIFEST_KEYS), [])
        self.assertEqual(copilot["name"], NAME)
        self.assertRegex(copilot["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(copilot["description"].strip())
        self.assertEqual(*([manifest.get(key) for key in ("name", "version", "description")]
                           for manifest in (development, copilot)))

    def test_both_mcp_registrations_are_the_same_but_for_the_root_variable(self):
        copilot, development = load("mcp.json"), load(".mcp.json")
        self.assertEqual((copilot.pop("$schema"), list(copilot["mcpServers"])),
                         (SCHEMA + "mcp.schema.json", ["coverity-connect"]))
        # The credentials reach the server through this block, each variable under its own name.
        env = copilot["mcpServers"]["coverity-connect"]["env"]
        self.assertLessEqual({"COV_USER", "COV_AUTH_KEY"}, set(env))
        self.assertEqual(env, {key: "${" + key + "}" for key in env})
        # The development form: its own root variable, and ${NAME:-} for a variable that may be unset.
        same = json.dumps(development).replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}").replace(":-}", "}")
        self.assertEqual(json.loads(same), copilot)

    def test_the_four_skills_have_the_three_frontmatter_keys(self):
        self.assertEqual(sorted({path.parts[1] for path in files() if path.parts[0] == "skills"}), SKILLS)
        for name in SKILLS:
            with self.subTest(skill=name):
                head, body = frontmatter(PLUGIN / "skills" / name / "SKILL.md")
                self.assertEqual(list(head), ["name", "description", "allowed-tools"])
                self.assertEqual((head["name"], head["allowed-tools"]), (name, ["shell(uv run:*)"]))
                self.assertTrue(head["description"].strip() and body.strip())

    def test_the_four_agents_exist_in_both_forms(self):
        found = sorted(path.as_posix() for path in files() if path.parts[0] in ("agents", "com.github.copilot"))
        self.assertEqual(found, [f"agents/{name}.md" for name in AGENTS]
                         + [f"com.github.copilot/agents/{name}.agent.md" for name in AGENTS])

    def test_the_settings_file_has_the_13_keys_of_the_specification(self):
        self.assertEqual(settings_keys(), SETTINGS)

    def test_the_mcp_server_offers_the_four_tools(self):
        self.assertEqual(mcp_tools(), TOOLS)


if __name__ == "__main__":
    unittest.main()
