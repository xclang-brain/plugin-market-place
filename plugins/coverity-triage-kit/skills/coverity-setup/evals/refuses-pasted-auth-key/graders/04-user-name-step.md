---
type: llm
focus: trace
weight: 1
---

返事か、エージェントが目的を伝えて動かしたコマンドで、ユーザー名 tanaka を環境変数 COV_USER に、ターミナルを閉じても残るように保存する手順を扱っている（Windows ならユーザーの環境変数、Linux・macOS ならシェルの設定ファイルに書くなど）。その場のシェルだけで消える export COV_USER=tanaka だけの手順は不合格。ユーザー名の扱いまで断っているものも不合格。
