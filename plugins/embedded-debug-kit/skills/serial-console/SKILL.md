---
name: serial-console
description: シリアルコンソール（UART・COM ポート）で組み込み機器のシェルにつなぎ、必要ならログインして、コマンドを順に動かし、出力と終了コードを返す。機器でコマンドやプログラムを動かして出力やログを見るとき、その設定ファイル（ポート・ログイン・動かすコマンド）を作るときや確かめるときに使う。
---

# serial-console

シリアルポートで機器のシェルにつなぎ、コマンドを1つずつ動かして、出力と終了コードを JSON で返す。

設定ファイルは `<作業フォルダ>/.github/embedded-debug-kit/serial-console.yaml`。各項目の意味は、項目のすぐ上のコメントにある。
パスワードは、設定ファイルの `login_password` か、`login_password_env` に名前を書いた環境変数で渡す。

## コマンド

Python は、Windows では `py -3`、ほかでは `python3` で動かす（下の表は `python3` で書く）。
作業フォルダで動かすか、`--workdir "<作業フォルダ>"` を付ける。パスは `"…"` で囲み、Windows でも `/` で区切る。

| すること | コマンド |
| --- | --- |
| 設定の `commands` を順に動かす | `python3 "<このスキルのフォルダ>/scripts/serial_console.py" run` |
| 渡したコマンドを順に動かす（設定の `commands` は使わない） | `python3 "<このスキルのフォルダ>/scripts/serial_console.py" run --command 'uname -a' --command 'ls /opt/app'` |
| 標準入力の1行を1つのコマンドとして、順に動かす（設定の `commands` は使わない） | `python3 "<このスキルのフォルダ>/scripts/serial_console.py" run --stdin` |
| 設定を確かめる。必要ならログインし、プロンプトが出るまで進める。コマンドは動かさず、何も作らない | `python3 "<このスキルのフォルダ>/scripts/serial_console.py" check` |
| 設定ファイルをひな形から作る。もうあれば、そのままにする | `python3 "<このスキルのフォルダ>/scripts/serial_console.py" init` |

PowerShell で `"` を含むコマンドと、Windows の Git Bash で `/` で始まるコマンドは、`--command` では正しく渡らない。`--stdin` で渡す。

bash：

```bash
python3 "<このスキルのフォルダ>/scripts/serial_console.py" run --stdin <<'END'
/opt/app/my_app --selftest
grep "a b" /var/log/messages
END
```

PowerShell（1行目は、日本語などを化けずに渡すため）：

```powershell
$OutputEncoding = [Text.UTF8Encoding]::new($false)
@'
/opt/app/my_app --selftest
grep "a b" /var/log/messages
'@ | py -3 "<このスキルのフォルダ>/scripts/serial_console.py" run --stdin
```

run は、最長でおよそ「コマンドの数 ×（`command_timeout_seconds` ＋ 5 秒）＋ 1 分」かかる。`command_timeout_seconds` の既定は 30 秒。コマンドを動かすツールの待ち時間は、それより長くする。

`--command` の値の書き方、機器を再起動するコマンドの動かし方、機器とのやりとりの詳細は [reference/console.md](reference/console.md) にある。

## 結果

標準出力に JSON を1つ返す。終了コードは、0 が成功、1 がつながらない・ログインできない・時間切れなどの失敗、2 が設定や引数の問題。

| 項目 | 中身 |
| --- | --- |
| `ok` | 成功したら `true` |
| `message` | 起きたことと、確かめること。利用者にはこれを伝える |
| `problems` | 設定や引数の問題（終了コード 2 のとき）。直してから、もう一度動かす |
| `config`、`log` | 設定ファイルとログの場所。ログには、機器からの出力がすべて入る。ただし、色などの制御文字と、終了コードを読むやりとりは除く。パスワードは `***` に替える。check はログを作らない |
| `commands` | run：動かしたコマンドごとの `command`、`output`（色などの制御文字を除いた出力）、`exit_code`（機器での終了コード。読めなければ `null`）、`timed_out`（時間切れで Ctrl-C を送ったか）、`truncated`（`output` を最後の 60 行・8,000 字に切ったか） |
| `not_run` | run：動かさなかったコマンド |
| `ports` | ポートが見つからないとき：この PC で見つかったシリアルポート |
| `created` | init：設定ファイルを作ったか |

- 利用者には、`message` に加えて、コマンドごとの `exit_code` と `output` の要点を伝える。時間切れなら、`not_run` のコマンドも伝える。
- `exit_code` が 0 でなくても、`ok` は `true` になる。コマンドがうまく動いたかは、`exit_code` と `output` で判断する。
- 時間切れのときは `ok` が `false` になり、残りのコマンドは `not_run` に入る。
- `truncated` が `true` なら、出力の全部は `log` にある。

## してはいけないこと

- つながらないときやログインできないときに、何も変えずに同じコマンドを繰り返さない。`message` を伝えて終える。
- run や check を同時に2つ動かさない。前のものが終わるのを待つ。
- 終わらないコマンド、入力を待つコマンド、シェルを抜けるコマンド（`tail -f`、`top`、`vi`、`su`、`exit` など）を動かさない。時間切れになり、残りのコマンドが動かない。
- 機器の中身を変えるコマンド（フラッシュへの書き込み、ファイルの一括の削除、パスワードの変更など）を、利用者に確かめずに動かさない。
- このスクリプトを通さずに（端末ソフトや自分で書いたスクリプトで）、シリアルポートを使わない。
- パスワードをチャットで聞かない。設定ファイルの中身を見せるときも、パスワードは出さない。
- 依頼されていないのに、設定ファイルを作ったり書き換えたりしない。
