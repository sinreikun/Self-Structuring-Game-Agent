# 目的に沿う研究の検証

大目的は、画面と操作から対象の仕組み・目的を理解し、その目的に沿って行動すること。画像変化、仮説件数、ツール呼び出し成功、CI成功を代用しない。

## 解法を教えない検証

`tools.check_autonomous_research` は通常のController・Scientist・LocalLLM・視覚API・Experiment実行器を使用する。プロンプトや生成schemaへゲームの解法・入力順・観測先を追加しない。対象の説明は実際の画面内だけに表示する。初期位置と入力方向を変えられ、正解の内部状態はLLMへ渡さず、終了後にだけ評価側へ出す。最初の通常Player入力は初期Evidence用で、LLMの成果には数えない。その後はLLMが要求したExperiment以外の背景入力を送らない。

評価は、観測要求、空間情報の配信、仮説に結び付いた操作、同じJobでの結果受領、操作後の説明候補、対象内の達成、人間向け報告を分ける。画素配列が届いたことは、必要な文字・領域が届いたことを保証しない。説明・報告の内容は、独立した画像・状態・操作履歴との照合が必要。全自動条件を通っても `behavioral_candidate_requires_semantic_review` とし、意味理解済みとはしない。時間切れは判断戦略の失敗と区別する。

```sh
python -m tools.check_autonomous_research --model-root MODEL_ROOT --state STATE_JSON --output NEW_OUTPUT --seeds 271828 314159
```

出力にはモデル・エンジン・projectorのSHA、Runtime能力、ソースcommit、Harness SHA、実プロンプトと応答、配信されたEvidence、Job履歴、評価側の状態を残す。Windows実機の最終判定を代行するテストではない。

## 視覚経路だけを分離した検証

`tools.check_visual_perception` は実画像を明示的に渡し、画面内の指示と配置を短く説明させる。通常研究と異なる指示付き検証であり、自律的な観測選択・操作・ゲーム達成の証明には使わない。画像と応答を照合して初めて、知覚経路が役に立つかを判断する。

```sh
python -m tools.check_visual_perception --model-root MODEL_ROOT --state STATE_JSON --image SCREEN.png --report REPORT.json
```

## 0.7.4で対象にした障害

提出診断の同一モデル `unsloth/gemma-4-12b-it-GGUF` / Q4_K_M、重みSHA `0a270ec9fe6b34f4a0d33992b6135117b484ebc4766ab76b51d4ae8c457e4c42` を取得・照合した。contextは8192。Linux CPUのため、実機Windows Vulkanと速度は比較しない。

現行Text-only経路の通常研究では、LLMが320×144 RGBの前後画面を要求した。各画面は138240値あり、要求は2048値。その次の推論へ実際に渡ったのは、予算調整後の左上64値、画像添付0だった。この画面の左上64値には指示文も図形もなく、必要な情報は届いていなかった。2回目の推論はHarnessの660秒制限で中断し、操作0・報告0。生の初版判定器は `failed` と出すが、エラーは `Harness deadline reached; InterruptedError` であり、自律判断についての監査判定は実行時間制約による未確認とする。生記録を成功や戦略失敗へ書き換えない。

同じHF固定版のF16 projector（175115840 bytes、SHA `91f086971e56d7a7d8d39e271873fccdb49541bd259d6e02c401a4f1cb7a219e`）を対応付けるとRuntimeはVisionを申告した。指示付きの視覚単独検証では、モデルは画面の「MOVE WHITE BOX INTO GREEN FRAME」と「左に白い四角、右に緑の枠」を正しく説明した。画像と照合済み。したがって0.7.4は、同一モデルに存在していた視覚入力をアプリから利用できない欠落を修正する。目的の推定・操作の自律性・実ゲームでの有効性は別の達成条件である。

## 実機での判定

既存の個体・モデルでSTARTする。対応が確認できた場合のみ同一固定版の視覚ファイルを取得する。`host/llm.json`のprojector・vision_resolutionと、診断のRuntime `input=vision`を確認する。取得不可・非対応なら理由を残してText-onlyへ戻り、Vision成功とはしない。

その後に確認するのは、LLM自身が選んだ `observe output=image` が画像として配信されたか、画面内の指示・配置について根拠付きの説明があるか、それに結び付くExperiment、同じJobでの結果受領、目的に沿う進歩があるか、である。能力申告だけ・画像添付だけ・色統計だけでは合格しない。Text-only経路のまま同等の理解へ至ることは、この修正で実証していない。
