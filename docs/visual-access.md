# 共通の視覚アクセスと視覚研究（0.5.0）

画面取得はHostの汎用的な「目」です。Agent、Scientist、Agentが依頼する非同期研究は同じVisualAccess / Eyesを利用します。Hostは色・画素・座標・幾何学的な処理結果を返し、ゲーム上の意味を付けません。従来の32×32輝度ABIは互換用に残します。

## 観測の要求

Agentは既存のrequest ABI、ScientistはJSONの`visual_requests`（1回最大4件、研究1件につき最大6回の追加観測）を使います。研究モデルの呼び出しは非同期研究スレッドにあり、追加の現在画面取得はPlayer自身の次のcaptureを待ちます。研究がゲーム用ハンドルを並行操作したり、Playerを待たせたりする経路はありません。停止・最小化・一時停止中のfresh要求は最大5秒で取得不能を返します。

```json
{"visual_requests":[
  {"op":"observe","event":1052,"phase":"before","size":[64,64],"mode":"RGB","output":"histogram"},
  {"op":"observe","event":1052,"phase":"after","box":[80,0,160,60],"size":[128,128],"output":"pixels","limit":128},
  {"op":"observe","event":1048,"phase":"before","compare":{"event":1052,"phase":"before"},"output":"statistics"}
]}
```

返った情報を受けて、モデルはさらに領域・解像度・処理方法を選べます。全経験の画像を自動で添付しません。領域は原画面の画素座標です。

| 項目 | 指定と結果 |
| --- | --- |
| 現在・再取得 | event/observationを省略すると現在画面。fresh=trueで次の取得を要求 |
| 過去経験 | event=Experience ID、phase=before/after/diff |
| 追加取得した画面の再利用 | observation=返却されたID。同じ画面を別領域・解像度で調査できる |
| Crop | box=[左,上,右,下]。右・下は排他的。未指定は全画面 |
| Resize | size=[幅,高さ]、またはnativeで原寸Crop。未指定は32×32 |
| チャンネル | mode=L/RGB |
| 原画面と座標変換 | width/height、box、coordinates.origin/scaleを返す |
| 画素配列 | output=pixels。RGBはインターリーブ配列。offset/limit/total_values/next_offsetで分割取得 |
| 一画素 | output=pixel、変換後のpoint=[x,y]。原寸を調べる場合はnative Cropを指定 |
| 汎用処理 | operations配列でgrayscale、threshold(value)、blur(radius)、edge、invertを合成 |
| 統計・形状 | output=statistics/histogram/components。componentsは閾値以上の4近傍連結領域のbox/area。意味ラベルはない |
| 比較 | compareに別のevent/observation/phase/box。対応領域の差分と平均絶対差に基づくsimilarity。位置探索や物体認識ではない |
| 画像入力 | output=image。RuntimeがVisionを明示したときPNGとして直接添付し、それ以外は同じ画像の画素配列を返す |

1要求は最大262,144画素、画素配列は1応答最大2,048値です。Agentの既存16KiB応答領域は拡張しません。Scientistでは未指定のlimitを128とし、小さいモデルでもページを選べるようにします。大きい画像を得る場合はCropやoffsetで分割します。処理列は最大12演算、Blur半径は最大8、連結領域は最大256件を返し、全件数と省略の有無も返します。原画面より大きい座標・不正な型・過大要求はエラーです。

## モデル能力と研究入力

LocalLLMは`/models`またはllama.cppの`/props`のmodalitiesから画像入力の可否を確認します。モデル名から推測しません。確認不能ならtextとして扱い、数値の視覚アクセスは継続できます。研究ごとに再確認し、個体のhost/model-capabilities.jsonへ保存します。管理済みモデルの起動記録にもcapabilitiesを残します。

確認済みの場合だけOpenAI互換のimage_url形式で選択画像を渡します。添付は最大4画像。各画像のattachment_indexと、元のExperience/Observationを対応付けます。モデル変更や再開時も、IDを用いて同じEvidenceから画像を再生成します。Base64や大量の一時CropはDBへ保存しません。

管理済みGGUFの導入で、任意のVisionモデル用projectorを自動探索・導入する機構は今回追加していません。Runtimeがmultimodalとして起動済みであることが画像直接入力の条件です。非Visionの管理済みモデルでも、同じ画素・統計・解析器の研究経路を使えます。

研究の入出力・要求・結果・途中応答を既存Jobへ保存します。入力枠が不足すると古い配列を明示的に省略し、元IDと再要求方法を残します。画像の意味を補って不足分を埋めません。追加観測予算を使い切ったときは、未検証の候補を自動採用せず、研究結果を保持して次の経験を待ちます。

## 解析器を作る・評価する・残す

WATによる自由な有限解析に加え、Hostの汎用演算を組み合わせた宣言的パイプラインを生成できます。Pythonのevalや任意Native Workerは導入しません。

```json
{"op":"analyzer_create","definition":{
  "description":"自分で選んだ領域の色分布を比較する",
  "query":{"box":[0,0,80,60],"size":[64,64],"mode":"RGB",
           "operations":[{"op":"blur","radius":1}],"output":"histogram"}
}}
```

定義のハッシュが解析器IDです。個体DBのvisual_analyzersへ不変の定義・作成時点・状態・評価・使用回数を保存します。モデルの切替では削除しません。定義の改良は新IDとして作り、旧版と比較・廃止できます。analyzer_listは8件ずつcursorで取得できます。analyzer_getで元定義を取得できます。analyzer_runは同じevent/observationを受け、未評価の試験だけexperimental=trueが必要です。試験結果はcandidate/rejectedとして明示され、通常利用の採用とは区別されます。

analyzer_evaluateのtrain/testは重複しない各8〜64件。実際に採用するための既存評価規則はテスト24件以上・変化あり/なし各4件以上を要求します。trainは作成以前、testは作成後の独立経験です。再評価に同じtestを再使用できません。入力前の特徴と許可入力から、訓練経験だけで構成した汎用の頻度プローブを用い、独立テストでBrier損失・両群均等損失・見逃し・誤反応・定数/過去頻度との比較・unknown・再現性・計算コストを記録します。処理は30秒まで、停止要求も検査します。

これは解析器が画面変化予測へ寄与するかの限定された評価です。意味・認識精度全般・攻略能力を検証した扱いにはしません。Agent本体への統合変更は従来のReplay・Regression・新経験の実入力比較でも評価します。役に立たない定数解析器や、既知の結果を見てから作った解析器を自動昇格させません。

## 出所・保存・容量

ExperienceのBefore/Afterは既存の内容ハッシュ画像です。派生結果はExperience ID、phase、元画像ハッシュ、box、size、座標変換、使用した解析器IDを持ちます。現在画面は要求時だけObservationとして保存し、後続Experienceとは保存画像ハッシュから対応を追えます。Experienceのない追加撮影を架空のExperienceへ結び付けません。

Crop、Resize、差分、配列は要求時に計算する一時物です。研究中の根拠はJobのpinでGCから保護し、完了時にJobのpinを外します。過去経験に対応する視覚取得結果の客観的メタデータはObservationとして研究ノートへ保存し、LLMによる意味解釈はVision出力も含めてinferred/hypothesisです。自動的にKnowledgeの確定事実へ昇格しません。過去の別Experienceを追加取得した場合、そのIDも仮説の根拠として利用できます。

現在画面を重要根拠として保持する場合はevidence_keepにobservationとreasonを指定できます。通常の追加画面は既存128件のホットウィンドウに従い整理され、明示的に保護されたものだけを残します。保存容量・WAL会計・予約領域・GCは既存Storeを使います。LLMを替えても、個体に保存された解析器、仮説、領域、研究Job、評価は保持されます。

## 外部ライブラリ

library_requestにname/purpose/reasonを指定するとHostへ要求を記録します。Agentからの要求は非同期Jobです。Hostはhost/visual-library-catalog.jsonの署名付きカタログを固定の公式公開鍵で検証します。期限・HTTPS信頼元・ライセンス・SHA-256・サイズ・Runtimeを確認し、既存Gatewayで取得します。

対応Runtimeはselfstruct-visual-pipeline-v1のみで、最大1MiBのJSON、最大16個の宣言的解析器です。署名された定義でも個体内の候補として取り込み、独立評価が済むまで採用しません。署名済みでもDLL・Python・任意Nativeコードは取得・実行経路へ入りません。今回、外部の公式視覚ライブラリそのものは同梱していません。カタログにない要求はawaiting_verified_catalogとして保持し、取得したふりをしません。

## GUI・因果的な評価

実際の要求に基づきAI注目領域の座標と元Evidenceを表示します。現在画面の要求は枠を重ね、過去Evidenceの領域を無関係な現在画面へ重ねません。「AIが調べている画面」を選ぶと、実際の元Evidenceと要求領域の枠を表示します。LIVE TRACEには実際に処理したOBS/VISION/ANALYZER/LIBRARYを流します。能力接続図はAgent自身の自己記述と、実在する解析器の定義・採用状態・使用回数を表示します。存在しない解析器や接続を推測して補完しません。

Replayと再起動時の学習復元でも可変解像度の要求を処理します。現在ExperienceはBeforeだけを公開し、After・Diff・未来のExperienceや撮影・書き込みは拒否します。結果は予測採点後にのみlearnへ公開します。

## 検証範囲

合成画像を使うtests/test_vision.pyで、RGB Crop・画素ページング・多段観測・比較・形状・上下限拒否・追加Evidenceの引用・Vision画像選択・個体資産の保持・解析器の独立評価と不採用・署名付きライブラリの型制限・GC保護・Replayの未来情報拒否を確認します。tests/test_llm_runtime.pyでは実際のローカルHTTP通信でmodalities検出と画像送信形式を確認します。

これらは制御されたモデル応答による検証です。任意の実LLMが自発的に有効な視覚解析器を発見できること、実ゲームの意味理解・攻略・長期間の安定性は別の実機検証を要します。
