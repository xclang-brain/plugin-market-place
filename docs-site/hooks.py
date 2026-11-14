"""Keep the pages faithful to the plugin: what a person copies and runs is taken from the plugin's own files.

    <!-- ctk:credentials:powershell -->   the block that setup.py prints (also :bash)
    <!-- ctk:config-template -->          the settings file that setup.py writes
    <!-- ctk:version -->                  the version of plugin.json
    [設計書](gh:docs/design/design.md)    a link to a file of the plugin on GitHub; the file must exist

A marker or a link that cannot be resolved stops the build. Every Mermaid diagram is also put in a frame
that scrolls sideways, so that a narrow screen shows it at a readable size (extra.css).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from mkdocs.exceptions import PluginError

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "coverity-triage-kit"
SETUP = PLUGIN / "skills" / "coverity-setup" / "scripts" / "setup.py"
BLOCK = re.compile(r"^(?P<indent>[ \t]*)<!-- ctk:(?P<name>[\w:-]+) -->[ \t]*$", re.M)
INLINE = re.compile(r"<!-- ctk:version -->")
LINK = re.compile(r"\]\(gh:(?P<path>[^)#\s]+)(?P<anchor>#[^)\s]*)?\)")
DIAGRAM = re.compile(r'<pre class="mermaid">.*?</pre>', re.S)


def _setup_literals() -> dict:
    """CREDENTIALS and TEMPLATE of setup.py, read without running it."""
    found = {}
    for node in ast.parse(SETUP.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("CREDENTIALS", "TEMPLATE"):
                found[node.targets[0].id] = ast.literal_eval(node.value)
    if set(found) != {"CREDENTIALS", "TEMPLATE"}:
        raise PluginError(f"{SETUP} に CREDENTIALS と TEMPLATE が見つかりません")
    return found


def _version() -> str:
    return json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]


def _blocks() -> dict[str, str]:
    literals = _setup_literals()
    template = literals["TEMPLATE"].format(url='"https://coverity.example.co.jp:8443"', stream='"app-main"')
    blocks = {f"credentials:{shell}": f"```{shell}\n{text}\n```" for shell, text in literals["CREDENTIALS"].items()}
    blocks["config-template"] = f"```yaml title=\".coverity-triage/config.yaml\"\n{template.rstrip()}\n```"
    return blocks


def on_config(config):
    config["copyright"] = f"coverity-triage-kit {_version()} ・ MIT License"
    return config


def on_page_markdown(markdown: str, page, config, files) -> str:
    blocks, where = _blocks(), page.file.src_uri
    base = config["repo_url"].rstrip("/")

    def block(match: re.Match) -> str:
        name, indent = match["name"], match["indent"]
        if name == "version":
            return indent + _version()
        if name not in blocks:
            raise PluginError(f"{where}: 不明な差し込み <!-- ctk:{name} -->")
        return "\n".join(indent + line if line else line for line in blocks[name].splitlines())

    def link(match: re.Match) -> str:
        target = PLUGIN / match["path"]
        if not target.exists():
            raise PluginError(f"{where}: プラグインに {match['path']} がありません")
        kind = "tree" if target.is_dir() else "blob"
        return f"]({base}/{kind}/main/plugins/coverity-triage-kit/{match['path']}{match['anchor'] or ''})"

    markdown = BLOCK.sub(block, markdown)
    markdown = INLINE.sub(_version(), markdown)
    return LINK.sub(link, markdown)


def on_page_content(html: str, page, config, files) -> str:
    return DIAGRAM.sub(lambda match: f'<div class="ctk-diagram">{match[0]}</div>', html)
