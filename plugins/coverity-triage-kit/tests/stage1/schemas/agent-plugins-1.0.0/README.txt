Agent Plugins 1.0.0 公式 JSON スキーマの写し

1段目の構造の検査（../../check_structure.py）が使う。検査のときにネットワークから
取らないよう、手元に写して持つ。直さない（無加工の写し）。

出どころ
  リポジトリ  https://github.com/agentplugins/agent-plugins-spec
  取った日    2026-10-22
  取った版    main の最新コミット ff8ab5e392cc87bd88d87c060815a87490e51003（2026-08-19）
              タグ v1.0.0 は bd383552095128f6effe895b9257cfd580a6d179（2026-08-06）を指す
  元のパス    schemas/1.0.0/plugin.schema.json、schemas/1.0.0/mcp.schema.json
              （この2ファイルを最後に変えたコミットは d92e6f443b8edcea42c039727a82afdc565779e2。
               タグ v1.0.0 から main までの間に、1.0.0 のスキーマと仕様書 spec/1.0.0.md の変更はない）
  公式の公開 URL  https://agent-plugins.org/schemas/1.0.0/plugin.schema.json
                  https://agent-plugins.org/schemas/1.0.0/mcp.schema.json
                  2026-10-22 に取得して、リポジトリの写しと JSON として同一であることを確かめた
  写しは無加工（バイト単位で同じ）

SHA-256
  0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883  plugin.schema.json
  6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb  mcp.schema.json

使い方の注意
  - 仕様書（spec/1.0.0.md）とスキーマが食い違うときは、仕様書が正しい（仕様書 5.2、7.2.1）。
  - スキーマでは表せない決まりがある（相対パスの閉じ込め、mcp.json の $schema の版が
    plugin.json と同じこと、URL は絶対 URL・非ループバックは HTTPS など）。
    これらは check_structure.py が別に検査する。
  - スキルのフロントマター（SKILL.md）の JSON スキーマは、この仕様リポジトリにない。
    Agent Skills 仕様（https://agentskills.io/specification）の決まりで検査する。
  - 1.1.0 の作業中の草案のスキーマは写していない（公開 URL が 404 で、未公開）。
