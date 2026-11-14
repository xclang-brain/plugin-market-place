"""説明サイトのビルド時の追加処理（mkdocs.yml の hooks で読み込む）。

利用者がコピーして実行する内容は、ページに書き写さず、プラグインのファイルから差し込む。
プラグインを変更すれば、サイトの記載も同じになる。ページには次のように書く。

    <!-- ctk:credentials:powershell -->   setup.py が表示する認証情報の設定手順（:bash も同様）
    <!-- ctk:config-template -->          setup.py が作成する設定ファイルの初期内容
    <!-- ctk:version -->                  plugin.json のバージョン
    [設計書](gh:docs/design/design.md)    プラグイン内のファイルへの GitHub のリンク（ファイルが存在すること）

差し込めない記述や、存在しないファイルへのリンクがあると、ビルドを失敗させる。
あわせて、Mermaid の図を横スクロールの枠（<div class="ctk-diagram">）で囲む。
狭い画面でも、図を縮めずに読める大きさで表示するため（大きさは extra.css）。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from mkdocs.exceptions import PluginError

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "coverity-triage-kit"
SETUP = PLUGIN / "skills" / "coverity-setup" / "scripts" / "setup.py"
# 1行だけの差し込みの記述。前の空白（タブの中など）を、差し込む内容の各行にも付ける
BLOCK = re.compile(r"^(?P<indent>[ \t]*)<!-- ctk:(?P<name>[\w:-]+) -->[ \t]*$", re.M)
# 文中のバージョンの差し込み
INLINE = re.compile(r"<!-- ctk:version -->")
# gh: で始まるリンク
LINK = re.compile(r"\]\(gh:(?P<path>[^)#\s]+)(?P<anchor>#[^)\s]*)?\)")
# Markdown から変換した Mermaid の図
DIAGRAM = re.compile(r'<pre class="mermaid">.*?</pre>', re.S)


def _setup_literals() -> dict:
    """setup.py の CREDENTIALS（認証情報の設定手順）と TEMPLATE（設定ファイル）を、実行せずに読み取る。"""
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
    """差し込みの名前と、差し込むコードブロック。設定ファイルの URL とストリームは、準備のページの例に合わせる。"""
    literals = _setup_literals()
    template = literals["TEMPLATE"].format(url='"https://coverity.example.co.jp:8443"', stream='"app-main"')
    blocks = {f"credentials:{shell}": f"```{shell}\n{text}\n```" for shell, text in literals["CREDENTIALS"].items()}
    blocks["config-template"] = f"```yaml title=\".coverity-triage/config.yaml\"\n{template.rstrip()}\n```"
    return blocks


def on_config(config):
    """全ページの下部に、プラグイン名とバージョンを表示する。"""
    config["copyright"] = f"coverity-triage-kit {_version()} ・ MIT License"
    return config


def on_page_markdown(markdown: str, page, config, files) -> str:
    """Markdown を HTML に変換する前に、差し込みの記述と gh: のリンクを置き換える。"""
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
        kind = "tree" if target.is_dir() else "blob"  # フォルダーは tree、ファイルは blob の URL
        return f"]({base}/{kind}/main/plugins/coverity-triage-kit/{match['path']}{match['anchor'] or ''})"

    markdown = BLOCK.sub(block, markdown)
    markdown = INLINE.sub(_version(), markdown)
    return LINK.sub(link, markdown)


def on_page_content(html: str, page, config, files) -> str:
    """HTML に変換した後に、Mermaid の図を横スクロールの枠で囲む。"""
    return DIAGRAM.sub(lambda match: f'<div class="ctk-diagram">{match[0]}</div>', html)
