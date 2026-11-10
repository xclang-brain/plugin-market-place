---
type: llm
focus: trace
weight: 1
---

src/report.c の write_report() が user_name() の NULL を textbuf_name() に渡しうることに触れたうえで、textbuf_name() が src/textbuf.c の 35〜37 行で NULL のときに -1 を返し、buf_append を呼び出さないことを述べている（report.c の NULL を本物のバグの理由にしていない）。
