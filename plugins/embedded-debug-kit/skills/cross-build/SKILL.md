---
name: cross-build
description: 組み込み機器向けの C/C++ プログラムを、設定ファイルに書いたコマンドでビルドし、結果（エラーと警告の行、できたファイルの有無・更新・サイズ・CPU の種類）を返す。ビルドやクロスコンパイルを頼まれたとき、その設定ファイルを作るときや確かめるときに使う。
---

# cross-build

sdk_env（クロスツールチェーンを使えるようにするスクリプト）を読み込んでからビルドのコマンドを動かし、結果を返す。

設定ファイルは `<作業フォルダ>/.cross-build/config.ini`。各項目の意味は、項目のすぐ上のコメントにある。

## コマンド

Python は、Windows では `py -3`、ほかでは `python3` で動かす（下の表は `python3` で書く）。
作業フォルダで動かすか、`--workdir <作業フォルダ>` を付ける。

| すること | コマンド |
| --- | --- |
| ビルドする | `python3 <このスキルのフォルダ>/scripts/cross_build.py run` |
| 設定を確かめる。何も作らない | `python3 <このスキルのフォルダ>/scripts/cross_build.py check` |
| 設定ファイルをひな形から作る。もうあれば、そのままにする | `python3 <このスキルのフォルダ>/scripts/cross_build.py init` |

run は、最長で設定の `timeout_seconds`（既定 600 秒）かかる。コマンドを動かすツールの待ち時間は、それより長くする。

## 結果

標準出力に JSON を1つ返す。終了コードは、0 が成功、1 がビルドの失敗、2 が設定や引数の問題。

| 項目 | 中身 |
| --- | --- |
| `ok` | 成功したら `true` |
| `message` | 起きたことと、確かめること。利用者にはこれを伝える |
| `problems` | 設定の問題の一覧（終了コード 2 のとき） |
| `exit_code`、`duration_seconds`、`timed_out` | ビルドのコマンドの終了コード（時間切れなら `null`）、かかった秒数、時間切れで止めたか |
| `error_lines`、`warning_lines` | エラーと警告らしい行（それぞれ最大 20 行） |
| `tail` | 出力の最後の 30 行 |
| `outputs` | 設定の `outputs` のファイルごとに、`path`、`exists`、`updated`（今回のビルドで変わったか）、`size`（バイト数）、`elf_machine`（ELF なら CPU の種類。例：`ARM`、`AArch64`、`x86-64`） |
| `notes`、`settings` | check の注意と、使う設定の値 |
| `config`、`log` | 設定ファイルとログの場所。ログにはビルドの出力がすべて入る（`check` はログを作らない） |

- 失敗したら、`message` と、`error_lines`（なければ `tail`）の主な行を伝える。
- `updated` が `false` のファイルは前のビルドのままで、ソースの変更が入っていないかもしれない。
- `elf_machine` が機器の CPU と違えば、機器向けのコンパイラーが使われていない。`sdk_env` を確かめる。

シェルごとの動き方と、出力の読み方は [reference/shells.md](reference/shells.md) にある。

## してはいけないこと

- このスクリプトを通さずにビルドしない。sdk_env が読み込まれず、ログも残らない。
- 同じ作業フォルダで、run を同時に2つ動かさない。
- `updated` が `false` のファイルを、今回ビルドしたものとして扱わない。
- 頼まれていないのに、設定ファイルを書き換えない。
