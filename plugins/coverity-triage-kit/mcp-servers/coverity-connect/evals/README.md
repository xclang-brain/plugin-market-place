# 評価の課題（mcp-builder の形）

モデルにこの MCP サーバーのツールだけを渡し、課題を解かせて、答えを文字列で照合する試験です。AI のクレジットを使います。

| ファイル | 中身 |
| --- | --- |
| `evaluation.xml` | 12 問の課題と答え（`<qa_pair><question>…</question><answer>…</answer></qa_pair>`） |
| `data/.coverity-triage/fake-issues.yaml` | 偽の Coverity の警告（2つのストリーム app-main・app-release、38 件） |
| `data/.coverity-triage/config.yaml` | `api: fake` の設定（`repo_root` に `data` を渡して使うとき） |

- どの課題も読むだけで解けます（書き戻しは要りません）。互いに頼らず、答えは1つに決まり、データを変えない限り変わりません。
- 何度もツールを呼ぶ課題です（ページを続けて取る、2つのストリームを比べる、CID だけからストリームを見つけ、そのストリームのトリアージストアや状態を調べる、など）。
- 答えが今のデータとツールで出ることは、`tests/test_evals_data.py` が、ツールを順に呼んで確かめます（データを変えたら、この試験で答えを確かめ直します）。

## Claude Code で動かす

1. サーバーの用意を一度だけします（サーバーのフォルダの README.md の「置き方と準備」）。
2. 次の MCP の設定を、評価の作業のフォルダ（リポジトリの外）に `eval-mcp.json` として書きます。`<このフォルダ>` は、サーバーのフォルダの絶対パスです。課題には `repo_root` が出てこないので、環境変数だけの設定（偽の Coverity）で動かします。

```json
{
  "mcpServers": {
    "coverity-connect": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--native-tls", "--frozen", "--quiet", "--directory", "<このフォルダ>", "coverity-connect-mcp"],
      "env": {"COVERITY_API": "fake", "COVERITY_FAKE_DATA": "<このフォルダ>/evals/data/.coverity-triage/fake-issues.yaml"}
    }
  }
}
```

3. 課題ごとに、ほかの MCP サーバーと設定を読ませずに解かせます。書き戻しのツールは外しておきます（データを汚さないため）。

```text
claude -p "<question の文>" --mcp-config eval-mcp.json --strict-mcp-config --setting-sources project --allowedTools "mcp__coverity-connect__*" --disallowedTools "mcp__coverity-connect__coverity_update_triage" --append-system-prompt "最後に、答えだけを <response> と </response> の間に書いてください。" --output-format json
```

4. 出力の JSON の `result` から `<response>…</response>` の中を取り出し、前後の空白を除いて `<answer>` と比べます（完全に一致で正解）。かかった時間、ツールの呼び出しの数、エラーになった呼び出しも記録すると、ツールの説明の直しどころが分かります。

## mcp-builder の評価のスクリプトで動かす

mcp-builder の `scripts/evaluation.py` でも動きます（Anthropic の API キーが要ります）。uv の引数は `-` で始まるので、`uv sync` でできた起動のコマンドを直接渡します。

```text
# Windows（PowerShell）
python scripts/evaluation.py "<このフォルダ>/evals/evaluation.xml" -t stdio -c "<このフォルダ>/.venv/Scripts/coverity-connect-mcp.exe" -e COVERITY_API=fake "COVERITY_FAKE_DATA=<このフォルダ>/evals/data/.coverity-triage/fake-issues.yaml"
# Linux（bash）
python scripts/evaluation.py "<このフォルダ>/evals/evaluation.xml" -t stdio -c "<このフォルダ>/.venv/bin/coverity-connect-mcp" -e COVERITY_API=fake "COVERITY_FAKE_DATA=<このフォルダ>/evals/data/.coverity-triage/fake-issues.yaml"
```
