---
type: llm
focus: trace
weight: 1
---

evidence に、fp を閉じずに返す行（src/loader.c の 18 行）が、/var/build/fw/ の付かないリポジトリからの相対パスで入っている。
