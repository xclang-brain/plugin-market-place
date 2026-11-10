# cross-build の使い方

組み込み機器で動かすプログラムを、PC でビルドするスキルです。
SDK の環境設定スクリプト（`sdk_env`）を読み込んでから、設定したビルドのコマンドを動かします。
エラーと警告の行を返します。できたファイルについては、あるかどうか、今回更新されたか、サイズ、CPU の種類（ARM など）を返します。

## 必要なもの

- Python 3.8 以上。ほかにインストールするものはありません。
- ビルドに使うツール（make など）と、機器の SDK（クロスツールチェーン）。
- シェル。Windows は cmd（既定）、PowerShell、Git for Windows の bash のどれか。ほかの OS では bash（既定）か PowerShell（pwsh）。

## インストール

プラグインとしてインストールしたときは、何もしなくても使えます。

プラグインなしで使うときは、`skills/cross-build/` フォルダを次のどれかにコピーして、CLI を起動し直します。`tests/` はコピーしなくてもかまいません。

| CLI | 1つのプロジェクトで使う | どのプロジェクトでも使う |
| --- | --- | --- |
| Claude Code | `<プロジェクト>/.claude/skills/cross-build/` | `~/.claude/skills/cross-build/` |
| GitHub Copilot CLI | `<プロジェクト>/.github/skills/cross-build/` | `~/.copilot/skills/cross-build/` |

`~` はホームフォルダ（Windows では `C:\Users\<ユーザー名>`）です。
フォルダの名前を変えてコピーするときは、`SKILL.md` の `name` も同じ名前にします。設定とログのフォルダも `.<新しい名前>/` になります。

## 依頼のしかた

1. 「cross-build の設定ファイルを作って」と依頼します。`<作業フォルダ>/.cross-build/config.ini` ができます。
2. 設定ファイルに値を書きます（[設定](#設定)）。機器向けにビルドするなら、`sdk_env` も書きます。
3. 「ビルドの設定を確かめて」と依頼します。フォルダやコマンドがあるかを見ます。ビルドせず、ファイルも作りません。
4. 「ビルドして」と依頼します。

作業フォルダは、CLI を起動したフォルダです。

## 設定

設定ファイルは `<作業フォルダ>/.cross-build/config.ini` です。詳しい書き方は、ファイルの中で、各項目のすぐ上のコメントに書いてあります。

| 項目 | 意味 | ひな形の値 |
| --- | --- | --- |
| `source_dir` | ビルドするフォルダ。作業フォルダからの相対パスか、絶対パス | `.` |
| `shell` | コマンドを動かすシェル（`auto` / `bash` / `cmd` / `powershell`） | `auto` |
| `sdk_env` | クロスツールチェーンを使えるようにするスクリプト。作業フォルダからの相対パスか、絶対パス。`shell` に合ったもの（cmd は `.bat`、PowerShell は `.ps1`、bash はシェルスクリプト）にする。空なら読み込まない | 空 |
| `build_command` | ビルドのコマンド（必須）。`source_dir` で動かす | 空 |
| `outputs` | ビルドでできるファイル（必須）。`source_dir` からの相対パス | 空 |
| `timeout_seconds` | ビルドを待つ最長の秒数。過ぎたら、ビルドを子プロセスごと止める | `480` |

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

`.cross-build/` には、中身が `*` の `.gitignore` を置きます。設定とログは Git に入りません。

## 結果とログ

ビルドできたら、AI が、かかった時間と、できたファイルのサイズと CPU の種類を伝えます。前のビルドのままのファイルがあれば、それも伝えます。
失敗したら、起きたことと、主なエラーの行と、確かめることを伝えます。

ビルドするたびに、`.cross-build/logs/` にログ（UTF-8）が1つできます。ビルドの出力がすべて入ります。
設定を確かめたときは、ログを作りません。

## 困ったとき

| AI が伝えたこと | 確かめること |
| --- | --- |
| コマンドが見つからない | ツールがインストールされているか。`PATH`。`sdk_env` が `PATH` を足しているか |
| `sdk_env` の読み込みに失敗した | SDK がインストールされているか。`shell` に合ったファイルか。スクリプトの最後のコマンドが 0 以外で終わっていないか |
| ファイルができていない | `outputs` の名前が合っているか。`source_dir` からの相対パスで書いたか |
| 更新されていない（前のビルドのまま） | ソースが変わっていないか。毎回作り直すなら、`build_command` を `make clean all` などにする |
| CPU の種類が PC と同じ（x86-64 など） | `sdk_env` を書いたか。Makefile がコンパイラーを決め打ちしていないか |
| 時間切れで止めた | ビルドが入力を待っていないか。`timeout_seconds` |
| 結果が返ってこない（CLI の待ち時間切れ） | `timeout_seconds` を、CLI がコマンドを待てる時間（Claude Code は最長 10 分）より 1 分以上短くする。長いビルドは、バックグラウンドで動かすよう依頼する |
| 同じ作業フォルダで、ほかのビルドが動いている | そのビルドが終わるのを待つ |
| Windows で bash が見つからない | Git for Windows をインストールするか、`shell` を cmd か powershell にする。WSL の bash は使えない |
| cmd で、`build_command` の `%変数%` が `sdk_env` の値にならない | 手順をバッチファイルにまとめ、`build_command` でそれを呼ぶ |
| cmd がネットワークのフォルダ（`\\` で始まる）で動けない | フォルダをドライブに割り当てるか、`shell` を powershell か bash にする |

WSL の中の SDK を使うときは、`sdk_env` を空にし、`build_command` を `wsl bash -c ". /opt/sdk/environment-setup-xxx && make"` のようにします。

## テストの動かし方

スキルのフォルダで、次を動かします（Windows では `python3` を `py -3` に替えます）。

```sh
python3 -m unittest discover -s tests
```

テストは、偽のビルドのコマンド（`tests/fake_build.py`）を使います。ほかにインストールするものはなく、外のネットワークも使いません。
PowerShell（pwsh か powershell）がなければ、PowerShell のテストは飛ばします。

## してはいけないこと

- `build_command` に、機器への転送や書き込みのコマンドを入れないでください。ビルドだけにします。
- 同じ `source_dir` で、2つのビルドを同時に動かさないでください。
- `.cross-build/` の `.gitignore` を消さないでください。
