---
type: llm
focus: trace
weight: 1
---

返事の diff の枠をそのままファイルに保存し、置いたファイルの写しの一番上のフォルダで patch -p1 --dry-run（または git apply --check）を動かすと、エラーなく当たる。
