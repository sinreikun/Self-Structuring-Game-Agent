# 複数ステップの研究（0.6.0）

研究LLMは同じJobの中で `actions` を返し、Hostが実行した結果を受けて再推論します。観測対象・領域・処理・入力の選択はLLMの判断です。ゲーム名、攻略知識、文字認識の意味ラベルはHostから与えません。従来の `visual_requests` も引き続き受け付けます。

## 操作とフェーズ

応答は `findings / actions / problem / reason / wat` のJSONです。`findings` の任意の `id`（例 `h1`）を、同じJobの後の実験・評価で参照できます。Hostが実際のKnowledge IDを返します。

- Observation/Hypothesis：何が分かったか、不明点、次に調べるEvidenceと実験を判断。WAT本文・ABIを一律に要求しません。
- Experiment planning/Experiment：明示的な許可入力計画をPlayerへ渡し、Before/Afterと完了・中断を受け取ります。
- Evaluation：仮説の事前登録確率と、実験で取得した画面変化を比較します。
- Analyzer creation：既存の安全な視覚演算を定義・試験・独立評価します。
- Candidate implementation：`candidate` で根拠Evidence、問題、必要挙動、評価条件を指定した後、現在の実装とABIを渡します。
- Repair：保存された候補と構文・ABI・評価エラーを渡し、世界理解や新仮説を同時に要求しません。

旧応答の直接 `wat` 提出も互換性のため受理します。既存の隔離事前検査・有限修正・Replay・回帰比較・実入力比較を通るまで採用しません。解析器も定義だけではcandidateのままです。定数0/0.5/1、過去頻度、現行Agentとの比較基準を緩めません。

## 観測・比較・解析

```json
{"actions":[{"op":"observe","event":10582,"phase":"before","box":[1040,0,1600,320],"size":[128,64],"mode":"RGB","output":"pixels","limit":128}],"findings":[],"wat":""}
```

`phase` はbefore/after/diff。event省略は現在画面、`fresh:true` はPlayerの新しい取得を待ちます。返ったobservation IDで同じ画面を再取得できます。boxは原画像座標、sizeは変換後サイズで、両者を取り違えません。

`compare` は同じ指定へ `compare:{event,phase,box}` を追加します。`analyze` は `operations` にgrayscale/threshold/blur/edge/invertを指定します。pixelsはoffset/limitでページ取得でき、statistics/histogram/componentsでも解析できます。画像上の意味は自動で確定しません。

Vision能力をRuntimeで確認できた場合、要求したoutput=imageだけを画像添付します。Text-onlyでは同じEvidenceを画素や数値で返します。最初の小さなBefore/After概観は、予測誤り・変化量・新しさで優先した1 Experienceだけです。全Experienceを画像で無条件投入しません。

`analyzer_create / analyzer_run / analyzer_evaluate / analyzer_list / analyzer_get / analyzer_retire / evidence_keep` もactions内で使用できます。独立経験を用いる解析器の昇格条件と資源取得の境界は従来通りです。

## 実験と仮説

```json
{
  "findings":[{"id":"h1","text":"入力4の後に画面が変化する確率を検証する。意味は不明。","evidence":[10582],"action":4,"change_probability":0.8}],
  "actions":[{"op":"experiment","hypothesis":"h1","action":4,"duration":60,"wait":100,"repeat":8}],
  "wat":""
}
```

actionは0始まりで、ロード中の個体の許可キーだけです。durationは10..500ms、waitは10..2000ms、repeatは1..8。1 Jobにつき最大4計画・32入力です。Scientistから直接ゲーム入力せず、唯一のPlayer入力スレッドが通常探索の代わりに実行します。候補の実入力比較・試用世代とは混在させません。

`condition:{box,size,mode,operations,mean_min,mean_max,max_wait_steps}` で、変換画像の画素平均が指定範囲となるまで待てます。max_wait_stepsは1..128（既定32）。これは汎用の数値条件で、ゲーム状態の意味判定ではありません。条件成立時にも画面が操作前に変化する可能性があり、因果関係の証明ではありません。

PAUSE中には入力しません。待ち時間上限120秒で期限終了できます。STOP・PAUSE中断・再起動で途中入力を再送しません。実験IDと送信済みExperience IDを永続化し、同じJobの復帰で二重実行しません。未完了経験は学習に使いません。中断した実験は部分完了数と理由を残します。

同じ入力を再試行する明示的Experimentは新しい経験を得るため重複観測とは区別し、上記の有限予算内で実行します。

`evaluate` では仮説IDとchange_probabilityを指定します。仮説作成時のaction/change_probabilityと一致し、同じ許可入力の完了経験が16件以上ある場合だけWilson 95%区間で支持／反証を記録します。事後に確率を選び直すこと、異なる入力の混在、少数例、条件付き実験から無条件仮説を結論することは判定不能です。自然言語の意味仮説や因果関係を確定扱いしません。未検証・検証中・支持・反証・判定不能を研究ノートで区別します。

## 停滞・予算・保存

「確認したい」などの意図だけが残る応答、または具体的行動のない研究が複数続く場合、未実行・未検証であることをLLMへ伝え、具体的actionsを同じJobで再要求します。Hostが自然文から勝手に領域やキーを決めることはありません。再誘導後も行動がなければ停滞と記録し、失敗を隠して実験成功と表示しません。観測・仮説だけで十分な研究は正当な完了です。

同じ過去Evidence・領域・解像度・処理は実行済み結果を参照して重複と記録します。新しいfresh観測や有限の反復実験は別です。1 Job最大10 LLM呼出し・24要求・600秒・画像添付合計4MiB。時間はステップ間にも確認し、実行中の単一推論や入力は既存のタイムアウト／中断境界に従います。再起動で予算をリセットしません。上限時は既に取得した根拠・仮説・結果を保存して終了します。

新しいEvidence・仮説・評価・実験・解析方法・実装候補、独立評価で実測したunknown削減をJobへ記録します。実測していないunknown削減はnullです。行動変更の実入力比較では各群の視覚ハッシュ種類数、観測時間当たりの種類数、変化数、同一状態／入力の反復数も記録します。これは観測指標で、ゲーム進行や因果効果を保証せず、予測評価の採用条件を緩めません。

一時Cropはファイルへ保存しません。要求元の原画像を参照し、Job中だけpinし、終了時に解放します。根拠Knowledge・Experimentに使ったイベントやevidence_keepで明示保護した観測は保持します。画像base64はJob・診断へ保存しません。

## Windows実機での確認手順

1. アプリを閉じ、既存Launcher 1.1.0を起動して0.6.0へ更新します。個体をロードし、許可キーを確認してSTARTします。新生個体へのリセットは不要です。
2. 研究開始後、LIVE TRACEの `RESEARCH ... job=... step=...`、`OBS` のExperience・crop・解像度、`result` と次の `resume` を確認します。現在研究欄にも実行中の処理を表示します。
3. 研究ノートの「研究履歴」で同じJobのaction_history／research_stepsを見ます。「実験」「検証中」「支持」「反証」「判定不能」でも記録を確認できます。解析器候補は能力接続図へ反映されます。
4. `EXPERIMENT queued/execute/complete` が発生したら、job・hypothesis・入力番号・repeat・イベントIDが対応するか確認します。PAUSEで実験入力が止まり、STARTで継続可能か確認します。既に中断した入力を再送しないため、実験が中断終了することは正常です。
5. 数件の研究が終わった時、または停滞・エラーが見えた時に、できればSTOP前に「診断ZIPをコピー」を押して提出してください。PAUSE中でも採取できます。採取によって状態は変更しません。

ZIPにはjobsの `research_steps`（開始・完了時刻、消費したaction_id、画像数）、`action_history`（要求、実行／重複／エラー、結果返却時刻、Evidence、領域、解像度）、`progress`、停滞・上限を含めます。`research_experiments` は要求Job・仮説・入力計画・送信イベント・Before/After参照・結果を含みます。Candidateのハッシュ、解析器の定義・評価、仮説の検証状態も追跡できます。中断中なら完了時刻がないことをそのまま残します。Prompt・候補ソース全文・画素配列・画像添付のbase64は診断へ出しません。

確認結果は「機構が動いたか」と「実ゲームで有効だったか」を分けます。LLMが適切な要求・解析器・実装を生成しない可能性は残るため、診断の実記録で判定します。

## 今回の自動検証の範囲

Text-onlyとVision対応クライアントを想定した決定論的LLM Harnessで、実画像と実際のPlayer入力経路を通じ、Crop→比較→再推論→反復Experiment→仮説の統計評価→解析器候補生成を同じJobで検証します。Visionでは要求画像が次の推論へ添付され、Text-onlyでは画素・統計が返されることを確認します。これは外部モデルが自律的に同じ判断をする証明ではありません。

CIでは既存の実モデル推論・研究も実行し、実際のactions数・再推論・停滞・上限を推論検証レポートへ残します。0.5.2の提出ZIPは読み取り専用で元画像のハッシュ、領域・RGB・エッジ・画素・統計・ヒストグラム・Components、有限修正とPAUSE状態を再確認します。実ゲームの最終確認は利用者のWindows実機で実行し、返送ZIPで確認します。
