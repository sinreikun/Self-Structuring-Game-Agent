# 目的ループの受け渡しと完成判定

常設指示は [standing-instructions.md](standing-instructions.md)。作業・バージョンの完了には未知対象・人間の介入なし・同一実機セッションで5段が渡る証拠が必要。Jobのstate=completeは有限処理の終了であり、接続確認・開発完了ではない。

## 担当・入力・出力

| 受け渡し | 担当と経路 | 入力 | 永続出力 |
|---|---|---|---|
| 観察→仮説 | Scientistがobserve/compareを要求しfindingsを登録 | 実画像・実Experience ID | knowledge.id、evidence、仮説文。画面変化率を検証する場合だけ実験前のaction/probabilityをclaimsへ登録 |
| 仮説→実験 | Scientist → research_actions.execute → Experiments.submit → 唯一のPlayer（Controller.step） | 仮説ID、LLMが選んだ許可入力・時間・反復・任意条件 | research_experimentsのjob/hypothesis/request/events/result、前後画像ハッシュ。中断・期限切れと完了を区別 |
| 実験→評価 | Scientistが同じ仮説IDのevaluateを要求 → Experiments.evaluate | 現在Job・同じ仮説の実験に属する完了Experienceのみ | research_evaluationsのID/job/hypothesis/result/created、action_history、EVALログ |
| 評価→報告 | Scientistがresearch_reportを要求 → research_memory.report | LLMの報告本文と、保存済みの実測評価 | research_reports.evaluationsへ評価時点の結果をコピー。評価ID・実験ID・根拠ID・セッション・標本数・判定根拠を保持し本文へ表示 |

現在Jobで完了した実験に根拠のない仮説へのevaluateは拒否し、利用可能な実験済み仮説IDと別名を返す。実験済みでも評価を1件も含まない報告要求はevaluateへ戻す。評価済みの鎖がある場合はその実測結果を報告し、他の未評価実験はID・根拠を未確認事項へ明記する。未評価分を評価済みと扱わない。報告から保存時点の評価を再現でき、後の仮説状態変更で過去の報告を書き換えない。診断ZIPにはresearch_evaluationsも含める。

## 集計と保留

画像変化の閾値は既存の0.01。完了Experience IDを重複排除して集計する。事前登録、同一入力、登録後の標本、単一セッション、無条件、16件以上が必要。既存のWilson 95%判定を維持し、意味理解・因果・目的達成を証明した扱いにしない。

change_probability省略時は既存の事前登録値だけを使用する。未登録の意味仮説へHostが確率を補わない。未登録・標本不足等は実験ID・根拠ID・理由付きのinconclusive（保留）として保存する。実験終了時のknowledge.validation=inconclusiveだけでは評価件数に含めない。

Job終了時に根拠付き評価が報告へ未収録なら、finish_reportが実測記録の報告を保存する。producer=host_record_summaryとし、LLMによる意味理解や対象の目的を創作しない。LLM報告にはproducer=llmを保存する。同じ評価を含む報告が既にあれば重複生成しない。Host生成の終端報告はoriginを明記して履歴・REPORTログへ残す。

LLMへ毎ターン不足段をloop_gapsとして提示する。Hostは見る場所・入力・仮説・ゲームの意味を選ばない。予算超過・停止・モデルの終了判断は保持し、未達を隠さない。

## 進展と接続

made_progressは実験件数と評価件数の両方を要求し、さらに履歴の同じ仮説・実験ID・完了根拠IDが対応することを検査する。観測・解析器・候補だけでは進展としない。

終了時にconnection_status=unverified、不足段、評価件数／支持・反証への実更新件数／報告件数を保存してLOOPログへ出す。報告のあることだけでは接続確認に昇格しない。実機、未知対象、人間の介入なし、同一セッションの一次証拠は別途監査する。

変更のたびに、同じ5段の回帰確認を実行する。合成環境・scripted LLMのテスト成功は配線検証の証拠であり、実機完走の代用ではない。GUI・管理・配布の変更を先行させない。
