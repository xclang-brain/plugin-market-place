# -*- coding: utf-8 -*-
"""Windows の PowerShell から、道具（scripts/config_tool.py）の set --stdin と get を使うテスト。

SKILL.md に書いた形（PowerShell の文字列をパイプで渡す）で値を渡し、設定ファイルの中身と、
PowerShell が受け取った結果の JSON を確かめる。次の3つの場合で動かす。

- その PC の既定の文字コードのまま（@' と '@ の here-string をパイプで渡す）
- 日本語の Windows と同じ組み合わせ（Python の出力も、PowerShell の読み方も cp932）
- 利用者のプロファイルが、PowerShell の $OutputEncoding を ANSI のコードページにしているとき。
  パイプで渡るバイト列は UTF-8 として読めないので、道具は OS の既定の文字コードで読む

Copilot CLI は、Windows ではコマンドを PowerShell（6 以上）で実行する。
そのため、このテストは、Windows で PowerShell 7 以上（pwsh）があるときだけ動く（ほかでは飛ばす）。
道具は、このテストを動かしている Python で動かす。

このスキルのフォルダで、次のように動かす。
  py -3 -m unittest discover -s tests -v
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
TOOL = os.path.join(SKILL_DIR, "scripts", "config_tool.py")
IS_WINDOWS = os.name == "nt"
PWSH = shutil.which("pwsh") if IS_WINDOWS else None
PLACE = "二階の台所"


def ps_quote(text):
    """PowerShell の、そのままの文字列（'…'）。"""
    return "'" + text.replace("'", "''") + "'"


def ansi_value():
    """OS の既定の文字コード（ANSI のコードページ）で書けて、そのバイト列が UTF-8 としては読めない値。
    (コードページの番号, 値) を返す。そういう値がなければ、値は None。"""
    import ctypes
    cp = ctypes.windll.kernel32.GetACP()
    for value in ("café", PLACE):
        try:
            data = value.encode("cp%d" % cp)
        except (LookupError, UnicodeEncodeError):
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return cp, value
    return cp, None


@unittest.skipUnless(PWSH, "Windows で、PowerShell 7 以上（pwsh）があるときだけ動かします")
class PowerShellStdinTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="setup-test-pwsh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "kettle.yaml")
        self.results = os.path.join(self.tmp, "results")
        os.makedirs(self.results)

    def write_config(self, value=""):
        """設定ファイル（UTF-8、BOM なし、改行は LF）。"""
        line = 'place: "{}"'.format(value) if value else "place: "
        with open(self.config, "wb") as f:
            f.write("# 置き場所\n# e.g. kitchen\n{}\n".format(line).encode("utf-8"))

    def config_text(self):
        with open(self.config, "rb") as f:
            data = f.read()
        self.assertNotIn(b"\r", data, "設定ファイルに CR が入りました")
        return data.decode("utf-8")

    def result(self, name):
        """PowerShell のスクリプトが Save で書いたもの。なければ None。"""
        path = os.path.join(self.results, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    def run_pwsh(self, body):
        """PowerShell のスクリプトを動かす。スクリプトは、結果を UTF-8 のファイルに書く。
        Save-Result <名前> $LASTEXITCODE <出力> で、<名前>.code（終了コード）・<名前>.raw（PowerShell が受け取った出力）・
        <名前>.value（出力の JSON の value）を書く。飛ばすときは、Save 'skipped' <理由> を書いて終える。
        コンソールの文字コードは、終わるときに元に戻す（このテストのプロセスと、同じコンソールを使うため）。"""
        lines = [
            "$ErrorActionPreference = 'Stop'",
            "$py = " + ps_quote(sys.executable),
            "$tool = " + ps_quote(TOOL),
            "$cfg = " + ps_quote(self.config),
            "$results = " + ps_quote(self.results),
            "function Save($name, $text) {",
            "  [IO.File]::WriteAllText((Join-Path $results $name), [string]$text, [Text.UTF8Encoding]::new($false))",
            "}",
            "function Save-Result($name, $code, $output) {",
            "  Save \"$name.code\" $code",
            "  $text = $output | Out-String",
            "  Save \"$name.raw\" $text",
            "  Save \"$name.value\" ($text | ConvertFrom-Json).value",
            "}",
            "Save 'versions' (\"PowerShell $($PSVersionTable.PSVersion) / OutputEncoding $($OutputEncoding.WebName)"
            " / Console $([Console]::OutputEncoding.WebName)\")",
            "$old = [Console]::OutputEncoding",
            "try {",
        ] + body + [
            "} finally { try { [Console]::OutputEncoding = $old } catch { } }",
            "exit 0",
        ]
        script = os.path.join(self.tmp, "run.ps1")
        with open(script, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        env = dict(os.environ)
        for name in ("PYTHONIOENCODING", "PYTHONUTF8"):
            env.pop(name, None)
        proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", script],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=180)
        self.report = "{}\n標準出力：{}\n標準エラー：{}".format(
            self.result("versions"), proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.returncode, 0, self.report)
        skipped = self.result("skipped")
        if skipped is not None:
            self.skipTest(skipped)

    def assert_result(self, name, value):
        """PowerShell が受け取った結果：終了コードが 0 で、JSON の value が value。"""
        raw = self.result(name + ".raw")
        self.assertEqual(self.result(name + ".code"), "0", "{} が失敗しました：{}\n{}".format(name, raw, self.report))
        self.assertEqual(self.result(name + ".value"), value,
                         "{} の標準出力の値が違います：{}\n{}".format(name, raw, self.report))
        return raw

    def test_here_string_through_the_pipe(self):
        """SKILL.md と同じ書き方（@' と '@ の here-string をパイプで渡す）。その PC の既定の文字コードのまま。"""
        self.write_config()
        self.run_pwsh([
            "$r = @'",
            PLACE,
            "'@ | & $py $tool set $cfg place --stdin",
            "Save-Result 'set' $LASTEXITCODE $r",
            "$r = & $py $tool get $cfg place",
            "Save-Result 'get' $LASTEXITCODE $r",
        ])
        self.assert_result("set", PLACE)
        self.assertRegex(self.config_text(), r'(?m)^place: "' + re.escape(PLACE) + r'"$', "設定ファイルの値が違います")
        # 標準出力は、コンソールの文字コードで書く。PowerShell も同じ文字コードで読む
        self.assert_result("get", PLACE)

    def test_japanese_windows_code_page(self):
        """日本語の Windows と同じ組み合わせ（Python の出力も、PowerShell の読み方も cp932）。
        日本語の値は、\\uXXXX にせず、そのまま出る。"""
        self.write_config(PLACE)
        self.run_pwsh([
            "[Text.Encoding]::RegisterProvider([Text.CodePagesEncodingProvider]::Instance)",
            "$env:PYTHONIOENCODING = 'cp932'",
            "try { [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(932) }",
            "catch { Save 'skipped' ('PowerShell の読み方を cp932 にできません：' + $_.Exception.Message); exit 0 }",
            "$r = & $py $tool get $cfg place",
            "Save-Result 'get' $LASTEXITCODE $r",
        ])
        raw = self.assert_result("get", PLACE)
        self.assertIn(PLACE, raw, "cp932 の標準出力に、日本語の値がそのまま出ていません\n" + self.report)

    def test_input_in_the_ansi_code_page(self):
        """$OutputEncoding が ANSI のコードページのとき。パイプで渡るバイト列は UTF-8 として読めないので、
        道具は OS の既定の文字コードで読む。"""
        cp, value = ansi_value()
        if value is None:
            self.skipTest("ANSI のコードページ {} では、UTF-8 として読めないバイト列になる値を書けません".format(cp))
        self.write_config()
        self.run_pwsh([
            "[Text.Encoding]::RegisterProvider([Text.CodePagesEncodingProvider]::Instance)",
            "$ansi = [Text.Encoding]::GetEncoding({})".format(cp),
            "$value = " + ps_quote(value),
            "$OutputEncoding = $ansi",
            "try { [Console]::OutputEncoding = $ansi }",
            "catch { Save 'skipped' ('PowerShell の読み方を ANSI のコードページにできません：' + $_.Exception.Message); exit 0 }",
            "$r = $value | & $py $tool set $cfg place --stdin",
            "Save-Result 'set' $LASTEXITCODE $r",
        ])
        self.assert_result("set", value)
        self.assertRegex(self.config_text(), r'(?m)^place: "' + re.escape(value) + r'"$', "設定ファイルの値が違います")


if __name__ == "__main__":
    unittest.main()
