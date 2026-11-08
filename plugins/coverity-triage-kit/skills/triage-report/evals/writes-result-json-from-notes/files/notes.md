# CID 20001 を調べたメモ

- チェッカー：RESOURCE_LEAK、Impact Medium、src/reader.c の read_config()
- 12 行で fopen した fp を、18 行の `return -2;` で閉じずに戻っている。21 行の fclose を通らない。
- 呼び出し元は main.c の load_settings() で、-2 のあとも処理を続けるので、設定の行が悪いたびに fp が漏れる。
- 本物のバグ。コードで最後まで追えた。
- 直し方：18 行の return の前に fclose(fp); を足す。正常なときの動きは変わらない。
- 解析したリビジョンと手元のコードは同じ（ずれなし）。
