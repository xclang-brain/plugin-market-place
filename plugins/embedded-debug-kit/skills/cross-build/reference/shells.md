# シェルごとの動き方と、出力の読み方

## 動かす順

run は1つのシェルで、source_dir を今のフォルダにして次の順に動かす。

1. sdk_env があれば読み込む。終了コードが 0 でなければ、読み込みの失敗として終える。
2. source_dir に戻る。sdk_env がほかのフォルダに移っても、ビルドは source_dir で動く。
3. build_command を動かす。

| shell | sdk_env の読み込み | build_command |
| --- | --- | --- |
| bash | `. <sdk_env>` | 同じ bash で動かす。`set -o pipefail` を付けるので、`make \| tee build.log` でも、make が失敗すればビルドの失敗になる |
| cmd | `call "<sdk_env>"` | 同じ行の `&&` の後に、`( )` で囲んで続ける。引用符の外に `)` を書くときは `^)` にする |
| powershell | `. <sdk_env>` | `Invoke-Expression` で動かす |

- sdk_env が設定した環境変数は、どのシェルでも build_command で使える。関数は bash と PowerShell で使える。
- cmd は、sdk_env を動かす前に、行全体の `%変数%` を置き換える。sdk_env が設定する変数を build_command に `%変数%` と書いて使うときは、手順をバッチファイルにまとめ、build_command でそれを呼ぶ。make などは環境変数をそのまま受け取るので、この心配はない。
- cmd は、`\\` で始まるネットワークのフォルダでは動けない。フォルダをドライブに割り当てるか、shell を powershell か bash にする。
- スクリプトは、Git for Windows などの bash を、PATH と既定のインストール先から探す。System32 と WindowsApps の bash.exe は選ばない。これらは WSL を起動するもので、環境変数も Windows のパスも渡らない。
- WSL の中の SDK を使うときは、sdk_env を空にし、build_command を `wsl bash -c ". /opt/sdk/environment-setup-xxx && make"` のようにする。

## 止めるとき

timeout_seconds を過ぎたときと、このスクリプトが Ctrl-C や SIGTERM・SIGHUP で止められたときは、シェルとその子や孫のプロセスをまとめて止める。

- Windows 以外は、プロセスグループに SIGTERM を送り、グループのすべてが終わるのを最長 5 秒待つ。残っていれば SIGKILL を送る。
- Windows は `taskkill /T /F` を使う。シェルはジョブに入れてあるので、このスクリプトが強制終了されたときも、ビルドのプロセスは一緒に終わる。

スクリプトは、シェルが終わった後も出力を開いたまま残る子プロセス（コンパイラーのサーバーなど）を止めない。その出力は 5 秒だけ待つ。

同じ作業フォルダで run がもう動いていれば、ビルドせずに終える（終了コード 1）。

## 出力の読み方

- 出力は1行ずつ UTF-8 として読み、UTF-8 でない行は OS の文字コード（日本語の Windows では cp932）として読む。標準エラー出力には、UTF-8 で書く。
- error_lines は、`error:`・`error C2065`・`error #20`・`Error[Pe020]`・`undefined reference`・`No such file or directory`・`エラー` などを含む行。make が表示するコマンドの中の `-Werror` や `error.c` は拾わない。warning_lines も同じように、`warning:` などを含む行。
- 次のどちらかのときは、`message` を「コマンドが見つかりません：…」にする。
  - 終了コードが 127（bash と PowerShell）か 9009（cmd）のとき
  - エラーの行がなく、シェルの決まった文（`command not found`・`is not recognized as an internal or external command`・`内部コマンドまたは外部コマンド`・`名前として認識されません` など）があるとき
- エラーの行があれば、`message` には最初のエラーの行を入れ、シェルの決まった文があれば、それも添える。
