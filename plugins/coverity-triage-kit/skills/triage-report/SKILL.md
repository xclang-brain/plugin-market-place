---
name: triage-report
description: 調べ終わった Coverity の警告1件（またはグループ1つ）の結果を、決まった項目（result.json の形。見立て・判定・推奨・確信度・根拠の場所・逸脱案・修正案・リビジョンのずれ）で書き、スクリプトで形と決まりを確かめる。result.json を読みやすい日本語の報告にもする。チャットで「調べた結果を決まった形で書いて」「この結果を result.json にして」「この result.json を確かめて」「この結果を説明して」のように頼まれたとき、警告のトリアージの結果をファイルに書くときに使う。結果を result.json の形で書くときは、必ずこのスキルを使う。
allowed-tools: ["shell(uv run:*)"]
---

# 結果の書き方（result.json）

1件の警告（またはグループ1つ）を調べた結果を、決まった形で書きます。
人は、まず一覧の「見立て」（`verdict.summary`）と、報告の結論だけを読んで判断します。そこだけで分かるように書きます。

## 進め方

```
- [ ] 1. 材料を確かめる（判定・根拠・修正案・逸脱案）
- [ ] 2. 形に沿って書く
- [ ] 3. 書く前の3つの確かめ
- [ ] 4. スクリプトで確かめる（だめなら直して、もう一度）
- [ ] 5. 返す
```

### 1. 材料を確かめる

- 要るもの：判定と根拠（実際に読んだコードの場所）、確信度とその理由、**修正案と逸脱案の両方**。誤検知でも修正案を、本物のバグでも逸脱案を作ります。どちらを採るかは人が決めます。
- 足りないときは、調べる・直す・逸脱コメントを書く手順があれば、それで作ります。なければ、頼んだ人に聞きます。聞けないとき（ほかのエージェントからの頼みなど）は、分かることだけを書き、足りないことを `confidence_reason` に書きます。推測で埋めません。
- リポジトリに `.coverity-triage/knowledge.md` があれば読み、使った知識は `verdict.rationale` に「プロジェクトの知識：…」と書きます。

### 2. 形に沿って書く

```json
{
  "item": "20001",
  "verdict": {
    "judgement": "true_bug",
    "summary": "エラーのとき fd を閉じずに戻っている",
    "rationale": "open（src/log.c:31）のあと、37 行の return で close を通らずに戻る。呼び出し元 log_init()（src/main.c:22）はエラーのあとも処理を続ける。",
    "evidence": [{"file": "src/log.c", "line": 37, "note": "close の前の return"}]
  },
  "recommendation": "fix",
  "confidence": "high",
  "confidence_reason": "経路をコードで最後まで追え、根拠がすべてコードにある",
  "deviation": {"classification": "Bug", "action": "Ignore", "severity": "Moderate",
                "comment": "本物の不具合。open_log()（src/log.c:37）はエラーのとき fd を閉じずに戻っており、直さない理由は説明できない。"},
  "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Moderate",
          "summary": "37 行の return の前で close する", "impact": "エラーのときだけ fd を閉じる。正常なときの動きは変わらない",
          "exceeded_constraints": [], "already_fixed_on_latest": false},
  "revision_drift": {"status": "none", "detail": ""},
  "group_excluded_cids": []
}
```

決まった項目のほかは書きません。文字列は空にしません（`detail` と `note` は空でよい）。形の正は `assets/result.schema.json` です。

| 項目 | 書くこと |
| --- | --- |
| `item` | 作業の ID。CID（`"20001"`）か、グループの ID（`"G1"`）。文字列で書く |
| `verdict.judgement` | `true_bug`（経路が成り立ち、実際に問題が起きる）/ `false_positive`（経路が成り立たない。すべての呼び出し元が条件を満たさないときも）/ `intentional`（成り立つが設計どおり）/ `undetermined`（コードからは決められない） |
| `verdict.summary` | 判断の決め手を40文字ほどの1行で。例「呼び出し元3か所で NULL を確かめている」 |
| `verdict.rationale` | 警告経路のどの段階を、どのコードで、どう確かめたかを順に。人が追い直さなくて済むように |
| `verdict.evidence` | 実際に読んだ場所だけ。`file` はリポジトリの一番上からの相対パス、`line` は1から（分からなければ `null`）、`note` に何があるか |
| `recommendation` | `fix` か `deviation`（下の「推奨の決め方」） |
| `confidence` / `confidence_reason` | 下の「確信度の決め方」。理由には、どの基準に当たるかを具体的に |
| `deviation` | 逸脱案。`comment` は下の「逸脱コメント」。Classification・Action・Severity は下の表 |
| `fix` | 修正案。`summary` は直した内容を1行で、`impact` はどの条件で何が変わるかと影響の範囲。決まり（最小・外部仕様を変えない・新しい違反を作らない）を守れなかったときは `exceeded_constraints` に「決まり：理由」を書く。最新のリビジョンで直っていれば `already_fixed_on_latest: true` にし、`summary` にどこで直っているかを書く |
| `revision_drift` | 解析したコードと読んだコードのずれ。`none`（合っている）/ `detected`（ずれがある。`detail` に場所）/ `unknown`（分からない） |
| `group_excluded_cids` | グループのときだけ。代表と理由が違うので外す CID（グループの一部だけ。全部は外さない）。グループでなければ空か、書かない |

**確信度の決め方**

- `high`：警告経路をコードで最後まで追え、根拠がすべてコードにある。
- `medium`：根拠の一部に推定がある（呼び出し元がリポジトリにない、マクロの展開を推定した など）。
- `low`：コードから決められない前提による（実行時の値、外部の仕様）か、リビジョンのずれがある。`undetermined` はいつも `low`。

**推奨の決め方**

- `true_bug` → `fix`。
- `false_positive` / `intentional` → どんな状況でも問題にならないことを、条件なしで逸脱コメントに書けるときだけ `deviation`。書けなければ `fix`（小さく安全な修正なら `fix` でもよい）。
- `undetermined` → リスクが小さければ `fix`、大きければ `deviation`。
- `knowledge.md` の「## 推奨の方針（修正か逸脱か）」があれば、それに従います。

**逸脱コメント**

- 日本語で、「誤検知。」「意図的。」「本物の不具合。」のどれかで始め、判定区分のあと1〜2文、200文字以内、前後に空白なし。見出し・改行・書式を使わない。
- 理由 → 根拠の場所（例 `open_session()（src/net/session.c:120）`）の順に、条件を付けずに言い切る。「ただし」「〜の場合は」「この判断の前提」「おそらく」、調べた過程、「修正を推奨」「確認してください」「見直すこと。」は書かない。
- 言い切れないときは、推奨を `fix` にし、言える範囲の理由だけを書く。
- 逸脱コメントを書く手順がほかにあれば、それに従って書きます（決まりは同じです）。

**Classification・Action・Severity の目安**

| 案 | Classification | Action | Severity |
| --- | --- | --- | --- |
| 逸脱（誤検知） | False Positive | Ignore | Unspecified |
| 逸脱（意図的） | Intentional | Ignore | Unspecified |
| 逸脱（本物の不具合で、直さない） | Bug | Ignore | Major / Moderate / Minor |
| 修正 | Bug | Fix Required | Impact に合わせる（High → Major、Medium → Moderate、Low → Minor） |

### 3. 書く前の3つの確かめ

結果をファイルに書く頼みのときは、書く前に確かめます。

1. 修正するように頼まれたフォルダ（作業用のコピー）を直したか。直していなければ、`already_fixed_on_latest` が `true` のときだけでよい。
2. アノテーション（コードの中の注釈）を入れるよう頼まれていれば、入れたか。
3. 調べるだけのコードと、利用者の作業中のファイルを変えていないか。

### 4. スクリプトで確かめる

ファイルに書いたら、次のスクリプトで確かめます（RUN。中を読む必要はありません）。

```
uv run --native-tls --quiet "<このスキルのフォルダ>/scripts/check_result.py" check --result "<result.json>" --item "<作業の ID>" --cids "<CID をカンマで区切る>"
```

- `<このスキルのフォルダ>` は、この SKILL.md があるフォルダです。`--cids` はグループのときに付けます（1件なら省いてよい）。分かれば `--impact High` のように警告の Impact も付けます。
- 結果は JSON が1つです。`ok` が `false` なら、`problems` の `message` のとおりに**結果のファイルだけ**を直し、もう一度確かめます。通るまで繰り返します。確かめを別のやり方で済ませません。
- `warnings` は目安から外れたところです（Classification の組など）。誤りなら直し、チームの決まりなどで正しければ、そのままでかまいません。
- 確かめだけを頼まれたとき（「ファイルはまだ変えないで」など）は、ファイルを変えず、`problems` を1つずつ、何をどう直すかと一緒に伝えます。直し方は、2 の表と決まりに合うものだけにします（修正案と逸脱案は両方が要るので `fix` を消したり null にしたりしない、決まった項目のほかは残さずに消す、など）。直した形を見せるときは、ファイルを作らずに `check --stdin` で確かめてから見せます（標準入力に `{"result": {…}, "item": "<作業の ID>"}` の JSON を渡す）。

コマンドを使えないとき（ほかのエージェントからの作業など）は、2の表と決まりを自分で1つずつ見て、最後まで進めます（決まった形の確かめは、頼んだ側が行います）。

### 5. 返す

- 頼みの文や、それが指すファイル（指示ファイル）が、書く場所と返す形を決めていれば、それに従います。決まっていなければ、1行で返します：`20001: 修正を推奨（確信度 高）— エラーのとき fd を閉じずに戻っている`
- チャットで頼まれたときは、ファイルに書くよう頼まれない限り、結果のファイルを作らず、チャットで返します。Coverity にも書き込みません。返す形は次のとおりです（一覧と同じ並びなので、人が結果を見比べやすくなります）。
  - 1行目の見出し：上の1行と同じ形（`<ID>: <推奨>を推奨（確信度 <高/中/低>）— <見立て>`）。推奨と確信度が先に来るのは、この見出しの決まりです。
  - その下に、次の6つをこの順で：
    1. 見立て（`verdict.summary`）と判定
    2. 推奨と確信度、確信度の理由
    3. 調べた経過と根拠の場所（ファイルと行）
    4. 逸脱案（Classification・Action・Severity と逸脱コメント）
    5. 修正案（直す内容と影響。決まりを超えたこと、もう直っていること）
    6. リビジョンのずれと、外した CID（グループでなければ「外した CID：なし」）
- 示された結果にない項目は作らず、「なし」と書きます。結果にない判断や助言（「ここを判断してください」など）も足しません。足したいことがあれば、6つの後に、結果の外のことだと分かるように短く書きます。
- result.json のファイルを渡されて「説明して」と頼まれたときは、次で上の形の文にできます（RUN）。出てきた `text` をそのまま見せ、`valid` が `false` なら `problems` も伝えます。

```
uv run --native-tls --quiet "<このスキルのフォルダ>/scripts/check_result.py" render --result "<result.json>"
```

ほかの例（誤検知・グループ・もう直っているとき）は [references/examples.md](references/examples.md) にあります。
