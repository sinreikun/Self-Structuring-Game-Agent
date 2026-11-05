# 単一リンク Job 実装・テスト報告

接続状態：**接続未確認**。本タスクの指定に従い実機セッションを実施せず、製品コードの実装と自動テストを行った。実機での担当リンクの前進・画像配送・評価・報告は、サキ側の別セッションで確認する。

## 手順0：先行コミットとpush

ファイル指定どおり、次の2コミットへ分割した。

1. `28a34a8ffc2c86ceee9206682755a18b71ac4406` — observe無駄対策。`research_actions.py`、`test_vision.py`、`test_observe_waste.py`。
2. `fc81f9cd5841801dfe7793a5b3857dbe3c5e22c7` — アジェンダ駆動開始と文書。`research.py`、`job_agenda.py`、`test_job_agenda.py`、`ssga-direction-policy.md`、`single-link-job-design.md`。

`git push origin HEAD` は成功し、origin/mainは `d0fcf31 → fc81f9c` となった。証拠：`prework-commits.txt`、`prework-push.txt`。

コミット前の全件回帰は**1回だけ**実行。実測は **366 passed / 7 failed / 1 skipped** で、指定の **367 passed / process_selection 6 failed / 1 skipped と完全一致しなかった**。差分は既存GUIテスト `tests/test_observatory.py::test_lab_hypotheses_and_all_management_sashes_survive_restart` の失敗1件。コードを変更せず、この1件だけ再確認したところ **1 passed**。全件結果を367 passedと読み替えてはいない。既知process_selection 6件は下記の集合である。

- `test_attach_existing_process_controls_only_selection_and_stop_preserves_it`
- `test_launcher_exit_adopts_real_child_and_does_not_touch_other_game`
- `test_reused_selected_pid_is_rejected_even_if_executable_matches`
- `test_controller_attach_collects_experience_and_detaches_without_killing`
- `test_attach_closed_game_relaunches_origin_and_rebinds_new_pid`
- `test_controller_recovers_repeated_closures_and_continues_same_experiment`

最初の `python -m pytest` はシステムPython 3.10にpytestが無く、テスト収集前に終了した。その後、リポジトリの `.venv/Scripts/python.exe` で上記1回の全件回帰を行った。証拠：`baseline.txt/xml`、`baseline-gui-recheck.txt/xml`。

## 着手前の実機証拠

AGENTS.md、`docs/standing-instructions.md`、設計書§2〜§5、方針文書§8追記を確認した。CodeGraphを先に使用して関連コードを調査した。

直近の保存済み実機記録 `D:/AI/audit-evidence/model-ab-mine/summary.json` と `after.sqlite3` を読み取り確認した。Job `501663bd645d4819b687ad59ca733967` は2026-11-05 11:44〜11:54に20 calls、calls上限でcomplete。画像配送0 bytes、仮説駆動実験0件、評価0件、支持・反証更新0件、研究報告0件。全20 stepの画像件数は0。提示台帳の先頭はclaim 2116、8/16標本。今回の実装着手時に既存機構の接続成立を仮定していない。読み取り集計を `prework.json` に保存し、元の監査DB・セッション概要・ログの写しを `prework-source/` に保存した。

## 実装

### 選定

`single_link.assign` が `job_agenda.calculate` の先頭を `payload.assigned_link` に保存する。claim / hypothesis / situation_candidate の3種を扱い、未完リンクが無い場合は `assigned_link=null` のdiscovery Jobとして固定する。再開しても再選定しない。担当仮説は明示別名 `assigned` と実IDを参照ドメインへ追加する。

既存順位と完了・判定済みの除外を維持。放棄記録があるリンクは、通常順位の安定順序を保ったまま末尾へ移す。上位3件を切り出す前に並べ替えるため、先頭候補が放棄済みでも未放棄の下位候補を選べる。同一仮説のhypothesis→claimの変化でも放棄順位を引き継ぐ。

### 文脈と配送記録

`single_link.context` は担当仮説本文、現在のクレーム、標本、不足数、関連状態、ベースライン、候補を現在DBと保存済み候補参照から読み直す。担当を示す固定情報と、変化する現在記録を分離した。担当記録の根拠IDを参照可能にする。

単一リンクJobは、個体全体の記憶索引の代わりに担当記録を提示する。再開境界より前の視覚・アクション・メモリ取得履歴を冒頭へ再蓄積しない。ジャーナルを削除せず、明示要求による再取得を残す。discoveryの従来の提示は維持する。

全Jobに `image_delivery_totals.count/bytes` を保存し、各推論stepにも `image_count`、`image_bytes`、`image_delivery_status=attached/no_attachment` を保存する。ゼロ配送も記録される。bytesは既存画像予算と同じ、推論クライアントへ渡したdata URLのUTF-8バイト数であり、画像内容をモデルが理解した証明ではない。開始・終了時刻のあるstep台帳と併せて確認できる。

### ゲート

findingsからのclaim登録、experiment（inline claimを含む）、registrationフォームの経路で担当を確認する。担当外には `state=rejected`、`code=outside_assigned_link`、担当識別子、不足標本数等の事実を返し、`action_charged=false` として要求予算から除外する。入力提出とクレーム登録の前に拒否する。

担当外の新規仮説自体は保存し、付属するclaimだけを拒否する。observe / compare / memoryおよび新規仮説登録は、この担当ゲートで制限しない。candidate担当では、指定候補を使うモデルの遷移クレームを許可する。Hostが仮説本文・状態の意味・遷移・確率・予測を補うことはない。

### 終了

担当claimが16標本、担当hypothesisにclaimと当該Jobの実験提出が存在、担当candidateが独立採用または遷移クレーム同梱採用された場合、`link_end.reason=link_closed` として終了する。閉鎖後の追加要求は実行しない。

モデルはfindingsに `abandon_link=true`、`text`に理由、`evidence`に根拠を記録して放棄できる。`link_end.reason=abandoned` と `link_abandonments` DB台帳に理由・根拠・Job・時刻を残す。未閉鎖・未放棄の場合は既定予算内で継続し、既存の上限で終了する。製品のcalls / actions / seconds / image_bytes予算、Player、統計ゲート、自動評価・報告処理、モデル設定は変更していない。

終了時は既存 `finish_report` を通す。なお、担当の標本台帳は既存agenda同様に過去Jobを含むが、既存の自動評価成熟判定は当該Jobの標本を対象とする。過去Jobとの合計で16に達しただけでは、自動評価・報告の発火を保証しない。評価範囲と統計ゲートは本設計の変更対象外として保持した。

段間の担当と受け渡しは次の既存経路を使う。

|段間|担当と入力|保存・返却|
|---|---|---|
|仮説→実験|モデルがfindings本文・claim・hypothesis IDを指定。Hostは担当ゲートと事前登録を検査し、`Experiments.submit`へ渡す。入力操作はPlayerが行う。|`knowledge`の仮説ID、`research_claims`、`research_experiments`のplan ID|
|実験→評価|Playerが記録した完了Experience IDsを既存評価器が集計。条件・登録時刻・入力・セッション・標本数の統計ゲートを適用する。|`research_evaluations`の評価ID、支持/反証/保留、根拠ID・理由|
|評価→報告|モデルのresearch_reportまたは既存Job終了時 `finish_report` が保存評価を受け取る。|`research_reports`の報告IDと評価参照。モデルの意味記述とHostの実測集計を区別する。|

## テスト対応

`tests/test_single_link.py` に追加した検証：

|要件|対象テスト|
|---|---|
|(a) 最上位選定・固定・完了/判定除外|`test_a_fixed_selection_and_terminal_exclusion`|
|(a) 放棄台帳・順位、探索担当の固定|`test_a_discovery_and_abandoned_rank_persist`、`test_a_discovery_remains_fixed_after_new_hypothesis`|
|(b) 担当外experiment/新規claim/既存別名claimの非課金拒否|`test_b_outside_experiment_and_findings_claim_are_free`、`test_b_existing_outside_alias_claim_structured`|
|(b) 担当へのexperiment/claim登録許可、継続登録の拒否|`test_b_assigned_experiment_passes_and_closes`、`test_b_assigned_findings_claim_registered_without_experiment`、`test_b_registration_route_outside_free`|
|(c) DB再読込、旧視覚履歴の非提示と原本保持、ゼロ配送記録|`test_c_database_context_resume_and_image_receipts`|
|(c) 実際の画像添付と件数/bytes|`test_c_new_image_delivery_count_and_bytes`|
|(d) 16標本で追加推論前に終了|`test_d_claim_sixteen_closes_before_model`|
|(d) claimのみでは未終了、提出後にJob完了|`test_b_assigned_findings_claim_registered_without_experiment`、`test_d_hypothesis_study_closes_after_submission`|
|(d) candidate独立採用/遷移への使用|`test_d_candidate_adoption_and_bundle_gate`、`test_d_candidate_claim_bundle_closes`|
|(d) モデル理由付き放棄とJob完了|`test_d_abandon_study_finishes_once_and_saves_reason`|

既存の複数仮説・複数実験・メモリページ検証・改善ライフサイクル・旧Jobからの回復のハーネス5箇所には、discovery Jobを検証することを明示した。既存assertは削除・緩和していない。単一リンクの新しい制約は上記の独立テストで検証する。

初回対象テスト14 passed。追加検証では旧ハーネス設定との不整合等10 failed / 87 passedを検出し、修正後の `test_single_link.py`、`test_research_actions.py`、`test_research_memory.py` は **78 passed**。途中の失敗ログも証拠先へ保持した。

実装後の最初の全件回帰は **380 passed / 8 failed / 1 skipped**。既知process_selection 6件に加え、探索を前提とする改善ライフサイクルと旧Job回復のハーネス2件が単一リンクの担当条件へ入ったため失敗した。上記の探索設定を明示後、該当2件は **2 passed**（`discovery-lifecycle.txt/xml`）。製品側に探索への例外は追加していない。途中の全件結果は `regression-before-fixture-fixes.txt/xml` へ保存。

最終全件回帰は **382 passed / 既知process_selection 6 failed / 1 skipped（464.27秒）**。追加した単一リンク15テストはすべて通過した。失敗名集合は指定の既知6件と完全一致し、先行全件回帰の生の失敗集合に対しても**新規失敗0件**。先行回帰で失敗したGUIテストは最終全件回帰でも通過した。

証拠：`regression.txt/xml`、機械照合 `regression-comparison.json`。最終ソース9ファイルの写しは `final-source/`、ハッシュは `source-hashes.json`。`git diff --check` も通過。先行2コミットに続き、本実装・テスト・本報告を別コミットにまとめる。

## 常設指示§10

- 接続状態：**接続未確認**。
- 今回の実機発火段：なし。指定により実機セッションを実施していない。着手前の直近記録では観察はあるが、同Jobの仮説→実験→評価→報告の鎖は未成立。
- 今回の実機研究の前進：**評価0件 / 支持・反証更新0件 / 研究報告0件**。自動テストの合成記録や本監査報告を実機成果に加算しない。
- 未確認の受け渡し：担当提示→モデル自身のclaim/実験要求→Player実行→根拠付き評価→評価を含む研究報告、および実モデルへの画像配送。サキ側の別セッションで確認する。
- 本報告は実装・テスト範囲の報告であり、実機接続の完成宣言ではない。

証拠はすべて **`D:/AI/audit-evidence/single-link/`**。今回C側outputsに追加したのは本報告書だけで、既存の `model-ab3-report.md` は変更していない。
