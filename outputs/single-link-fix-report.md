# 単一リンク Job の摩擦2点 修正報告

対象: c61e3cd の単一リンク Job。初回実機検証 Job ccb44405d4264d4c90df753b8bf373af の2点に限定した。実機セッションは実施していない。再測はサキ側で行う。

## 接続状態と着手前の証拠

**接続未確認**。D:/AI/audit-evidence/single-link-verify/ の summary.json、runner.log、engine.log、before/after.sqlite3 を確認した。

- 実機検証では観察の記録、既存仮説2116への担当固定、登録済み変化率クレーム（action W / probability 0.0005）、標本8/16の提示、推論1回と claim_required 差し戻しまで証拠がある。
- このJobに紐付く実験レコード0件、評価0件、研究報告0件。既存仮説は以前の登録であり、このJobが新規登録から完走した証拠ではない。
- 検証前→後の全DB件数: events 11,036→11,080、knowledge 2,639→2,641、research_experiments 305→305、research_evaluations 24→24、支持・反証等の状態139→139、research_reports 23→23。詳細は本証拠フォルダの prework-counts.json。
- 研究の前進（当該検証Job／今回作業での追加実績）: **評価0／支持・反証更新0／研究報告0**。今回の作業はオフライン検証のみ。
- 未接続箇所: 仮説→実験は不変クレームゲートで拒否。次の画像結果の配送は入力予算超過。実験→評価→報告の実機での通過は未確認。今回の変更後も実機再測を行わないため接続済みとは判定しない。

## 修正1: 登録済みクレームとの差異の案内

変更前:

    {"state":"claim_required","fields":[{"field":"claim","reason":"differs from immutable registered claim; use a new hypothesis"}],"next":"Add or correct these claim fields in this experiment request and resend. No input executed."}

変更後（仮説2116の登録内容を例示）:

    {"state":"claim_required","hypothesis":2116,"fields":[{"field":"claim","reason":"differs from immutable registered claim"}],"registered_claim":{"type":"change_rate","probability":0.0005,"action":"W","action_index":0},"next":"Resend this hypothesis with the registered action and without a claim field to experiment using the immutable registered claim. No input executed."}

遷移型の場合は registered_claim に from_state/to_state も含む。拒否と非課金判定は維持した。次のモデル呼び出しでも登録内容と再送手順を読めるよう、履歴の表示フィルタに registered_claim / fields / next を保持した。同じ仮説・登録入力で claim フィールドを省略する再送を案内し、この差し戻しには新しい仮説の案内を含めない。

## 修正2: 単一リンクの冒頭文脈

保存済みの正本や許可Evidence集合は変更せず、担当ありのモデル表示だけを縮約した。仮説／状態の根拠ID、標本、候補フレーム、baselineフレーム、遷移の代表画像参照、担当のEvidence ID一覧は件数＋直近3件。独立した許可Evidence表示は全件数＋直近8件と取得手順を提示する。担当ID、登録クレームの値、現在標本数8件、不足8件、作業手順は保持。discovery（担当なし）の冒頭文脈構成は変更していない。

計測は実機検証の after.sqlite3 を読み取り専用で読み、メモリ内DBへ複製して当該Jobの冒頭を再構成した。ゲーム、Controller、Player、推論生成は起動していない。モデルの同一アーティファクトをローカルのtokenizerサーバーへロードし、製品の visual_dialogue → LocalLLM.count_tokens → /tokenize（add_special=true）経路で数えた。healthと推論完了のみオフライン用に置換し、生成前の完成プロンプトを捕捉した。予算式は製品と共通。

実機保存値は6,588 tokens / 11,324 chars / output 1,348。比較の前後には同じ再構成条件を用いた。変更前再構成は6,575 tokens / 11,312 charsで、保存済み完了payloadから履歴・call等を冒頭へ戻す再構成条件により実機値と13 tokens差がある。変更後は **3,697 tokens / 8,306 chars / output 2,048**。4,500以下を達成した。

|構成要素|変更前tokens|変更後tokens|
|---|---:|---:|
|Job agenda|97|97|
|MISSION（主目的・知識の線引き）|645|645|
|担当導入・許可入力・操作説明・視覚API|1698|1698|
|許可Evidence ID提示|1044|110|
|担当リンクの現在記録|2349|405|
|担当制約・放棄手順|73|73|
|参照中の仮説|28|28|
|新規ID・既存仮説の利用手順|91|91|
|状況候補agenda・採用手順|127|127|
|研究履歴|266|266|
|追加観測結果|32|32|
|省略情報|32|32|
|今回の作業手順|92|92|
|特殊トークン（空文字tokenize）|1|1|
|**input_count 合計**|**6,575**|**3,697**|

各区切りまでの累積 count_tokens の差分で配賦し、合計が製品input_countに一致する方式。文字列単独のtoken数を足す方式による境界誤差を避けた。区切りと文字数は before/after-measurement.json、完全な文字列は before/after-prompt.txt に保存した。

主な削減は許可ID表示 1,044→110、担当記録2,349→405。仮説の根拠IDは129件の正本を保持したまま、表示を件数＋直近3件へ変更。標本8件の正本も保持し、表示は samples_count=8 / samples=直近3件 / missing_samples=8。担当記録内部の補助配賦は assigned-context-components.json に保存（JSON境界が異なるため上の配賦と直接合算しない）。

画像2件と出力下限の余裕: 8192 - 3697 - 256 - 2*1024 = 2191。出力下限1,024に加え、後続の観測メタデータ等に1,167 tokens分の余裕がある。画像の実配送や次callの実測を保証する結果ではなく、その確認はサキ側の再測に残る。

## 検証

- 対象テスト: tests/test_single_link.py **18 passed**。変化率・遷移の登録内容と再送案内、禁止案内の不在、拒否の非課金、次call向け履歴表示、claimを省略した再送の成功、登録不変、表示縮約と正本保持を確認。
- 予算テスト: tools/check_single_link_budget.py が実tokenizerを用い、input_count <= 4500 と画像2件＋出力1,024の余裕をassertして成功。コード・生成プロンプト・計測結果を証拠へ保存。
- discovery冒頭の比較: c61e3cd の Scientist.visual_dialogue と変更後へ同一の担当なしpayloadを与え、完成プロンプト13,483文字が完全一致（discovery-check.txt / discovery-before/after.txt）。
- フル回帰: **385 passed / 既知 process_selection 6 failed / 1 skipped**（455.40秒）。基準382 passed / 6 failed / 1 skippedに対し、追加3テストが成功。JUnitのテスト名集合で比較し、新規失敗0件、既知失敗6件とskip1件は完全一致。失敗はいずれも既知の「Test window did not appear」。regression.txt / regression.xml / regression-comparison.json に保存。
- git diff --check: 成功。
- 初回の追加テスト2件は、同一callのaction tokenで再送していたテスト側の不備で失敗したため、次callとして再送する形へ直した。初回ログも targeted-initial.* に保持。修正中に開始した旧版回帰は停止し、最終ソースで全回帰を実行し直した。

再実行例（ゲーム操作・推論生成をしない）:

    .venv/Scripts/python.exe -m pytest tests/test_single_link.py -q
    .venv/Scripts/python.exe tools/check_single_link_budget.py D:/AI/audit-evidence/single-link-verify/after.sqlite3 D:/AI/audit-evidence/single-link-fix --settings D:/AI/SelfStruct/host/settings.json

予算テストには同じモデルの /tokenize を提供するローカルサーバー（既定18766）が必要。サーバー設定はtokenizerログとmodel identityの証拠を参照。

## 成果物・コミット・push

証拠: D:/AI/audit-evidence/single-link-fix/。C側 outputs にはこの報告書だけを追加した。既存の未追跡 outputs/model-ab3-report.md は変更していない。

実装・対象テスト・予算検証コード・この報告書を1コミットにまとめた（親 c61e3cd、この報告書を含むコミット）。push: **origin/main へのpush成功**。コミットIDと送信結果は証拠の commit.txt / push.txt / push-exit.txt を参照。
