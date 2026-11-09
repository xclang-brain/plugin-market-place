# cross-build の使い方

組み込み機器で動かすプログラムを、PC でビルドするスキルです。
SDK の環境設定のスクリプト（sdk_env）を読み込んでから、設定したビルドのコマンドを動かします。
結果として、エラーと警告の行と、できたファイルの有無・今回更新されたか・サイズ・CPU の種類（ARM など）を返します。

## 要るもの

- Python 3.8 以上。ほかに入れるものはありません。
- ビルドに使うツール（make など）と、機器の SDK（クロスツールチェーン）。
- シェル。Windows は cmd（既定）、PowerShell、Git for Windows の bash のどれか。ほかは bash（既定）か PowerShell（pwsh）。

## 入れ方

プラグインとして入れたときは、何もしなくても使えます。

プラグインなしで使うときは、`skills/cross-build/` フォルダを次のどれかにコピーし、CLI を起動し直します。`tests/` はコピーしなくてもかまいません。

| CLI | 1つのプロジェクトで使う | どのプロジェクトでも使う |
| --- | --- | --- |
| Claude Code | `<プロジェクト>/.claude/skills/cross-build/` | `~/.claude/skills/cross-build/` |
| GitHub Copilot CLI | `<プロジェクト>/.github/skills/cross-build/` | `~/.copilot/skills/cross-build/` |

`~` は、Windows では `%USERPROFILE%` です。
フォルダの名前を変えてコピーするときは、`SKILL.md` の `name` も同じ名前にします。設定とログのフォルダも `.<新しい名前>/` になります。

## 頼み方

1. 「cross-build の設定ファイルを作って」と頼みます。`<作業フォルダ>/.cross-build/config.ini` ができます。
2. 設定ファイルに値を書きます（[設定](#設定)）。機器向けにビルドするなら、`sdk_env` も書きます。
3. 「ビルドの設定を確かめて」と頼みます。フォルダやコマンドがあるかを見ます。ビルドせず、ファイルも作りません。
4. 「ビルドして」と頼みます。

作業フォルダは、CLI を起動したフォルダです。

## 設定

設定ファイルは `<作業フォルダ>/.cross-build/config.ini` です。各項目の意味は、ファイルの中のコメントにもあります。

| 項目 | 意味 | ひな形の値 |
| --- | --- | --- |
| `source_dir` | ビルドするフォルダ。作業フォルダからの相対パスか、絶対パス | `.` |
| `sdk_env` | クロスツールチェーンを使えるようにするスクリプト。`shell` が cmd なら `.bat` か `.cmd`、PowerShell なら `.ps1`。空なら読み込まない | 空 |
| `shell` | コマンドを動かすシェル（`auto` / `bash` / `cmd` / `powershell`）。`auto` は、Windows では cmd、ほかでは bash | `auto` |
| `build_command` | ビルドのコマンド（必須）。`source_dir` で動かす。1行で書く | 空 |
| `outputs` | ビルドでできるファイル（必須）。`source_dir` からの相対パス | 空 |
| `timeout_seconds` | ビルドを待つ最長の秒数。過ぎたら、ビルドを子のプロセスごと止める | `600` |

`outputs` が2つ以上なら、次の行から1行に1つずつ、行頭に空白を入れて書きます。

```ini
[settings]
source_dir = firmware
sdk_env = /opt/poky/4.0/environment-setup-cortexa7t2hf-neon-poky-linux-gnueabi
build_command = make -j4
outputs =
    build/my_app
    build/my_app.map
```

パスワードの項目はありません。
`.cross-build/` には、中身が `*` の `.gitignore` を置きます。設定とログは Git に入りません。

## 結果とログ

ビルドできたら、AI が、かかった時間と、できたファイルのサイズと CPU の種類を伝えます。前のビルドのままのファイルがあれば、それも伝えます。
失敗したら、起きたことと、主なエラーの行と、確かめることを伝えます。

ビルドするたびに、`.cross-build/logs/` にログ（UTF-8）が1つできます。ビルドの出力がすべて入ります。
設定を確かめたときは、ログを作りません。

## 困ったとき

| 言われたこと | 確かめること |
| --- | --- |
| コマンドが見つからない | ツールが入っているか。`PATH`。`sdk_env` が `PATH` を足しているか |
| `sdk_env` の読み込みに失敗した | SDK が入っているか。`shell` に合ったファイルか。スクリプトの最後のコマンドが 0 以外で終わっていないか |
| ファイルができていない | `outputs` の名前が合っているか。`source_dir` からの相対パスで書いたか |
| 更新されていない（前のビルドのまま） | ソースが変わっていないか（変わっていなければ、make は作り直さない）。毎回作り直すなら、`build_command` を `make clean all` などにする |
| CPU の種類が PC と同じ（x86-64 など） | `sdk_env` を書いたか。Makefile がコンパイラーを決め打ちしていないか |
| 時間切れで止めた | ビルドが入力を待っていないか。`timeout_seconds` |
| Windows で bash が見つからない | Git for Windows を入れるか、`shell` を cmd か powershell にする。WSL の bash は使えない |
| cmd で、`build_command` の `%変数%` が `sdk_env` の値にならない | cmd は、`sdk_env` を動かす前に `%変数%` を置き換える。手順をバッチファイルにまとめ、`build_command` でそれを呼ぶ |

WSL の中の SDK を使うときは、`sdk_env` を空にし、`build_command` を `wsl bash -c ". /opt/sdk/environment-setup-xxx && make"` のようにします。

## テストの動かし方

スキルのフォルダで、次を動かします（Windows では `python3` を `py -3` に替えます）。

```sh
python3 -m unittest discover -s tests
```

テストは、偽のビルドのコマンド（`tests/fake_build.py`）を使います。ほかに入れるものはなく、外のネットワークも使いません。
PowerShell（pwsh か powershell）がなければ、PowerShell のテストは飛ばします。

## してはいけないこと

- `build_command` に、機器への転送や書き込みのコマンドを入れない。ビルドだけにする。
- 同じ作業フォルダで、2つのビルドを同時に動かさない。
- `.cross-build/` の `.gitignore` を消さない。
