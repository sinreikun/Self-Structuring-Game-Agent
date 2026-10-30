# 固定Launcherの更新プロトコル

`SelfStructLauncher.exe` はprotocol=1 / Launcher 1.0.0。通常の本体変更ではLauncherを作り直して配布する必要はない。初回公開のLauncherを使い続け、本体Manifestだけ更新する。ビルド工程は再現性確認のためLauncherも生成するが、既存ユーザーへ再ダウンロードを求めない。

公式URLはリポジトリmainの `updates/Stable.json` / `Development.json`。TLSに加え、Launcher内の固定公開鍵でEd25519署名を検証する。Manifestのschema、protocol、channel、sequence、expires、version、application（URL・size・SHA-256・license・trust）を検証する。未知schema/protocol、不正署名、期限切れの配布情報、逆行sequenceは採用しない。

ローカルの既に受理した版は署名・サイズ・SHA-256・channelを毎回照合し、配布Manifestの期限が切れてもオフライン起動できる。pendingは正常起動時にlast-known-goodへ昇格する。次回まで残ったpendingは失敗扱いにする。一度失敗した同一版を無限再試行しない。旧版も破損していれば起動しない。ロールバック対象はアプリだけで、Agent世代と経験は巻き戻さない。

本体は実行中ファイルへ上書きせず、別の版ディレクトリへ原子的に配置。GUI・DB・Wasmtime初期化後に一回限りのhealth markerを返す。異常終了・期限超過・実行不能なら旧版を起動する。Launcherの更新失敗は実験データを削除しない。Launcherはネットワーク障害の日本語メッセージをruntime/launcher-status.jsonへ保存し、本体GUIにも表示する。

## 開発者の公開手順

1. Windows CIで本体・Launcherをビルドし、テスト結果と成果物ハッシュを確認する。
2. 同じコミットへリリースタグを付ける。
3. CIから取得した本体を `python -m tools.prepare_application_manifest --private-key-file <別保管の秘密鍵> --binary <SelfStruct.exe> --tag <タグ> --version <版>` で署名する。
4. 生成した公開Manifestだけをupdatesへコミットする。秘密鍵は絶対にリポジトリ・EXE・診断へ入れない。
5. release-requestに検証済みrun_idとtagを記入し、公開ワークフローへ渡す。公開器はManifestの署名と実際のCI本体のハッシュ・サイズの一致も確認する。

今回の環境のGitHub資格情報ではSecrets登録APIが403になるため、署名はリポジトリ外の鍵ファイルを用いて行う。継続運用する開発者はこの鍵を安全に引き継ぎ、将来CIで署名する場合は管理者がリポジトリSecretsへ登録する。公開鍵を変更すると既存Launcherは新しい鍵を信頼しないため、通常更新のたびに鍵を生成し直してはいけない。

## 確認範囲

Launcherのテストは、署名検証、二世代の実体起動、ヘルス応答、実行不能な第三世代からの復帰、オフライン起動を含む。Windows CIのデスクトップでの自動検証と、ユーザーPC上での人による操作確認を区別する。Authenticode署名とは別の仕組みである。

本体には現時点でOSSライセンスファイルが指定されていないため、ManifestでMIT等のライセンスを推測して付与しない。公開はリポジトリ所有者の配布指示に基づくものと記録する。依存のライセンスはTHIRD_PARTY_NOTICESで別途保持する。
