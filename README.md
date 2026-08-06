# kagoshima-tennis-alert

鹿児島県のテニスコート予約サイトを確認し、直近15日間の土日祝にある8:00〜13:00の空き候補を、GitHub PagesとLINEで知らせるプロジェクトです。

> [!IMPORTANT]
> 鴨池県営テニスコート、SuMIzeiテニスコート、東開庭球場の空き取得は、いずれも認証不要の実画面に対応済みです。スクレイパーは予約サイトの利用者ID・パスワードを使用・保存せず、自動予約も行いません。会員ログインはこれとは分離したSupabase Authのメールマジックリンクを使用します。

## Documentation

- [Project Vision](docs/PROJECT_VISION.md)
- [Development Roadmap](docs/DEVELOPMENT_ROADMAP.md)
- [Service Specification](docs/SERVICE_SPECIFICATION.md)
- [Phase 1 Auth Design](docs/PHASE1_AUTH_DESIGN.md)

## 現在の機能

- 今日を含む直近15日間から土曜日、日曜日、日本の祝日を抽出
- 8:00〜13:00内で1時間以上ある空きだけを保持
- 同一コートの連続した空きセルを結合
- 鴨池県営のVue生成DOMをコート行・時刻ヘッダー・状態セル単位で解析
- SuMIzeiと東開のP-Kashikan公開フォームを施設設定と対象日で遷移し、共通処理でコート行の状態セルを解析
- 成功、空き0件、取得エラーを区別して `data/availability.json` に保存
- 鴨池県営とSuMIzeiで、前回データにない `slot_id` だけをLINE通知
- 東開庭球場はWeb表示のみで、LINE通知対象外
- JSONを読み込むスマートフォン向けGitHub Pages画面
- 施設ごとの取得状態・最終確認時刻と、全体・施設別の空き候補件数を表示
- 最終更新から60分を超えると更新遅延、120分を超えると更新停止を警告
- 空きなしの正常取得日は初期状態で折りたたみ、施設ごとに表示を切り替え
- 成功・失敗を問わず診断用HTMLとPNGを保存
- pytest、GitHub Actions、Pages自動配信
- Supabase Authのメールマジックリンク送信、PKCE callback、セッション確認、ログアウト
- 認証用画面（ログイン・会員登録、認証callback、マイページ、利用規約、プライバシーポリシー）
- 固定版 `supabase-js` v2と、Repository Variablesから生成するブラウザ公開設定

空き状況は候補です。予約前に必ず公式サイトで最新情報を確認してください。

## Phase 1 認証プロジェクト基盤

2026-08-04時点で、Supabase Auth、メールのマジックリンク認証、GitHub Pages、PKCEを正式方針としました。ブラウザは固定版 `@supabase/supabase-js@2.106.2` を使用し、公開Project URLとpublishable keyだけで接続します。`flowType: "pkce"`、`persistSession: true`、`autoRefreshToken: true` を明示しています。

実装済みの範囲は、メール形式・利用規約同意の確認、`signInWithOtp` によるマジックリンク送信、codeの `exchangeCodeForSession`、認証URLの消去、`getSession` によるログイン画面とマイページのセッション確認、会員profileと規約同意履歴の本人表示、現行規約への同意、現在のブラウザを対象にした `signOut({ scope: "local" })` です。成功・失敗文言からアカウントの存在有無を推測しにくくし、メールアドレス・code・token・認証URLをconsoleへ出しません。

`persistSession: true` と `autoRefreshToken: true` により、ログアウトしない限り、同じブラウザでは通常セッションが保持されます。ログインページはフォーム表示前に既存セッションを確認し、ログイン済みならマイページへ移動します。ブラウザを閉じても通常は次回そのまま利用できますが、ログアウト、ブラウザデータの削除、セッションの無効化、別の端末やブラウザからの利用時には再認証が必要です。ログアウトは操作したブラウザのセッションだけを終了し、全端末ログアウトは行いません。このセッション確認は画面UXのためのもので、会員データの最終的な認可境界は引き続きPostgreSQLのRLSです。

PKCEのcode verifierはリンクを要求したブラウザ側に保存されるため、マジックリンクは原則としてログイン操作を開始した同じブラウザで開く必要があります。別端末・別ブラウザで開いて認証に失敗した場合は、利用するブラウザでログイン画面から再送してください。

`supabase/migrations/20260804000000_create_member_profiles.sql` に `legal_document_versions`、`profiles`、`terms_acceptances`、新規Authユーザー用trigger、既存ユーザーbackfill、RLS、最小権限Grant、引数なしの `accept_current_terms()` RPCを実装しています。`20260806000000_fix_accept_current_terms_conflict.sql` は、適用済みの関数を制約名指定の `ON CONFLICT` へ置き換えます。ブラウザから会員データを直接変更する権限はなく、同意登録だけをRPCへ集約します。退会Edge Function、通知設定、LINE連携、課金は未実装で、退会ボタンは準備中のままです。

開発用の現行規約版は `2026-08-04-draft` です。一般公開前に正式な規約本文・版番号・発効日へ更新し、開発用版への同意済み利用者にも正式版への再同意を求めてください。

追加した画面は次のとおりです。すべてGitHub Pagesのリポジトリ配下で動く相対リンクを使用します。

- `auth/login.html`: マジックリンクによるログイン・会員登録画面
- `auth/callback.html`: メール認証callback画面
- `account/index.html`: 最小限のマイページ
- `legal/terms.html`: 利用規約の暫定案
- `legal/privacy.html`: プライバシーポリシーの暫定案

法務ページは暫定案であり、会員登録の一般公開前に運営者表示、問い合わせ窓口、版番号、発効日、保持・削除方針などの内容確認が必要です。詳細と未決事項は[Phase 1 Auth Design](docs/PHASE1_AUTH_DESIGN.md)を参照してください。

## ファイル構成

```text
.
├── .github/workflows/update-availability.yml
├── account/
│   └── index.html
├── assets/
│   ├── config/auth-config.example.js
│   ├── css/auth.css
│   └── js/auth-foundation.js
├── auth/
│   ├── callback.html
│   └── login.html
├── data/availability.json
├── data/notification-state.json
├── docs/
│   ├── DEVELOPMENT_ROADMAP.md
│   ├── PHASE1_AUTH_DESIGN.md
│   ├── PROJECT_VISION.md
│   └── SERVICE_SPECIFICATION.md
├── legal/
│   ├── privacy.html
│   └── terms.html
├── scripts/
│   ├── __init__.py
│   ├── generate_auth_config.py
│   └── scrape.py
├── tests/
│   ├── fixtures/kamoike_schedule.html
│   ├── fixtures/sumizei_schedule.html
│   ├── fixtures/toukai_schedule.html
│   ├── test_auth_foundation.py
│   ├── test_notifications.py
│   ├── test_page.py
│   └── test_scrape.py
├── index.html
├── requirements.txt
└── README.md
```

実行時には鴨池県営を `snapshots/kamoike-prefectural/YYYY-MM-DD.html`、P-Kashikanの2施設を `snapshots/{sumizei|toukai-tennis}/YYYY-MM-DD-step-name.html` と同名のPNGへ保存します。P-Kashikanはトップ、施設検索、施設選択後、対象日の空き状況を段階別に保存します。スナップショットはGit管理せず、GitHub ActionsのArtifactとして7日間保存します。

## 鴨池県営の抽出方式

2026年7月21日に対象サイトへPlaywrightでアクセスし、次の実DOMを確認しました。

- 予約結果全体: `.rsv__result[data-reserve]`
- コート行: `.rsv__result[data-reserve] > section.rsv__field`
- コート名: `h3.rsv__result__item:not(.major--item--color) em`
- 時刻ヘッダー: `.rsv__result__time > li`
- 状態帯: `.rsv__result__situation > li`
- 予約可: `.rsv--result--yes` と `area-label="予約可"`
- 予約済み: `.rsv--result--no`
- 予約不可: `.rsv--result--out`

状態セルは開始・終了時刻を直接持たず、`style="width: ...%"` で時間幅を表します。各コート行の時刻ヘッダー先頭・末尾を時間軸の境界とし、分類済み状態セルの合計幅に対する各セルの割合から時刻を復元します。行外の `.rsv__result__example` は凡例なので解析対象にしません。

非表示の予約結果・コート行は除外し、同じ `slot_id` は重複除去します。DOM構造が不足している場合は、空き0件として扱わず `unexpected_dom` を記録します。

## P-Kashikan施設の抽出方式

SuMIzeiは2026年7月21日、東開は2026年7月24日に、Playwrightで認証なしの画面遷移と通信を確認しました。

1. トップの「施設 の空きを見る」から `index.php` の施設空き状況へ遷移
2. `input[name="ShisetsuCode"]` から施設設定に一致するラジオボタンを選択
3. 公開画面が通常使用するフォーム値を対象日に変更して日別画面を表示
4. `.SelectCalendar` 内の時間ヘッダーとコート行だけを解析

施設設定はSuMIzeiが `#scd029`（値 `029`、画面表記「ＳｕＭＩｚｅｉテニスコート」）、東開が `#scd131`（値 `131`、画面表記「東開庭球場」）です。コードと選択後の施設見出しの両方を照合し、対象が見つからない場合は `facility_not_found` としてその施設だけをエラーにします。

内部APIやJSONエンドポイントは使用されていませんでした。画面遷移は `index.php` への通常のPOSTで、次の値を送信します。

| Form値 | 内容 |
| --- | --- |
| `op` | `srch_sst`（施設の空き状況） |
| `ShisetsuCode` | 施設設定のコード（SuMIzei `029`、東開 `131`） |
| `UseYM` | `YYYYMM` |
| `UseDay` | 月内の日 |
| `UseDate` | `YYYYMMDD` |
| `disp_span` | `0`（1日表示） |

実DOMでは、時間軸が `.SelectCalendar table.koma-table th`、各コート名が `td.name` にあります。インターネット予約可能な空きセルは `○` と表示され、セルの `id` と `onmousedown` に施設・コート識別子、日付、`HHMMHHMM` 形式の開始・終了時刻が含まれます。実際に確認した例は次の形です。

```html
<td id="131|003|...#2026/07/25#1"
    onmousedown="setAppStatus('131|003|...', '2026/07/25', 0, '08300900', ...);">
  ○
</td>
```

パーサーはコート行内の `●`、`○`、`〇` だけを空き候補とし、`×`、`-`、`確認中`、予約済み、抽選、メンテナンスなどを除外します。`○` は実属性の時間帯を優先し、`●` は同じ行のセル幅と時間ヘッダーから時間を復元します。凡例は `.SelectCalendar` 外なので解析対象になりません。施設コード、選択日、時間ヘッダー、コート行のいずれかが不整合なら、空き0件ではなく施設単位のエラーにします。

P-Kashikanでは公式画面の時刻境界が内部値やセル幅計算上 `:29` / `:59` になる場合があるため、それぞれ1分進めて `:30` / 次の正時へ補正してから連続枠を結合します。この補正はSuMIzeiと東開だけに適用し、鴨池県営の時刻解析には適用しません。補正前の既存slot_idは同じ施設・日付・コート名・補正後時刻に一致するIDへ通知基準を移行するため、時刻修正だけで再通知しません。

東開の実画面では、コート名は「Aコート(ナイターあり)」「Bコート(ナイターなし)」「C・Dコート(ナイターあり)」です。時間軸は8時台の最初が8:30〜9:00の30分枠、その後は通常60分枠です。監視境界は共通の8:00〜13:00ですが、東開の実データは営業時間に従って8:30からとなり、結合後60分未満の空きは除外します。同じ表示名が複数の内部コート行に現れるため、連続枠はDOM上の同一行内だけで結合してから重複除去します。

## 連続枠の扱い

同じ日・同じコートで終了時刻と次の開始時刻が一致する場合は結合します。JSONには結合後の枠だけを保存し、元の細分化された枠は残しません。これにより差分通知とPages表示で同じ空きを重複して扱いません。

8:00〜13:00の境界で空き枠を切り詰め、結合後の長さが60分未満の候補は除外します。

## availability.json

現在のスキーマバージョンは2です。空き枠には次の情報を保存します。

```json
{
  "facility_id": "kamoike-prefectural",
  "facility_name": "鴨池県営テニスコート",
  "date": "2026-08-01",
  "court_name": "コート２",
  "start_time": "11:00",
  "end_time": "13:00",
  "duration_minutes": 120,
  "status": "available",
  "reservation_url": "https://v2.spm-cloud.com/user/kamoike-undo/reserves/daily?date=2026-08-01&category_id=483&area_id=289",
  "slot_id": "安定したSHA-256由来の24文字ID"
}
```

`slot_id` は `facility_id + date + court_name + start_time + end_time` から生成します。

日別データは次の状態を持ちます。

- `success`: 正常取得。空きがない場合も `availability: []` で成功
- `error`: 取得またはDOM解析に失敗。`error_type` と `error_message` を保持
- `selector_pending`: 旧データとの互換用。現在の3施設では生成しない

エラー時も `checked_at` と `reservation_url` を保存します。通常は空の `availability` を保存しますが、P-KashikanがHTTP 403を返した場合は、直前の正常取得データがあれば `status: error` のまま `availability` を保持し、`fallback_from_previous: true` と `last_success_checked_at` を記録します。画面上では取得エラーとして扱い、保持した枠を現在の空き件数には加えません。主な `error_type` は `navigation_timeout`、`navigation_error`、`access_denied`、`facility_not_found`、`date_selection_failed`、`no_schedule_table`、`unexpected_dom` です。

## notification-state.json

`data/availability.json` は最新の取得結果とPages表示用、`data/notification-state.json` はLINE通知済み範囲の比較基準です。役割を分離しているため、LINE APIが失敗しても最新の空き状況は更新できます。

通知状態には次を保存します。

- `schema_version`: 通知状態のスキーマ
- `initialized`: 初回基準化が完了したか
- `initialized_facility_ids`: 初回基準化が完了した施設ID
- `updated_at`: 状態を最後に変更した日時
- `observed_slot_ids`: 通知比較で既に観測済みとする `slot_id`
- `observed_slot_scopes`: 施設・日付単位のエラー復旧を誤通知しないための補助情報
- `last_notification_status`: 直前の基準化・送信・抑止・失敗状態

ファイルがない、壊れている、または `initialized=false` の場合は、現在の空きを基準として保存するだけで通知しません。既存の状態に新しい施設が加わった場合は、その施設の正常取得分だけを初回基準化し、同時に既存施設で見つかった新規空きは通常どおり通知候補にします。リポジトリには3施設の現行枠を基準化済みとして登録してあります。

初期化後は現在値と `observed_slot_ids` の差だけを通知します。消えた枠は通知しません。正常取得後に消えた枠を基準から外し、その枠が後日再出現した場合は新規空きとして通知します。施設取得が `error` の間は、その施設・日付の既存IDを保持し、復旧だけを新規空きと誤認しません。

`observed_slot_ids` はLINE通知対象外の東開庭球場も含め、全施設の観測状態を保持します。東開の新規枠や再出現枠は観測済みとして基準を更新しますが、LINE送信候補には加えません。

## ローカルセットアップ

Python 3.11以上を使用します。

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --requirement requirements.txt
python -m playwright install chromium
```

### テスト

```bash
python -m pytest
```

テストfixtureは実DOMから抽出した必要最小限の構造だけを匿名化して保存しています。取得したページ全体はfixtureとしてコミットしません。

### ローカルでの認証画面確認

PowerShellでは、ローカルまたは検証用Supabaseプロジェクトの公開値を現在のプロセスへ設定し、Pagesと同じ生成スクリプトを実行します。

```powershell
$env:SUPABASE_URL = "https://<project-ref>.supabase.co"
$env:SUPABASE_PUBLISHABLE_KEY = "<publishable-key>"
$env:AUTH_CALLBACK_URL = "http://localhost:8765/auth/callback.html"
.\.venv\Scripts\python.exe scripts\generate_auth_config.py
.\.venv\Scripts\python.exe -m http.server 8765
```

ブラウザで `http://localhost:8765/auth/login.html` を開きます。Supabase DashboardのAuth Redirect URLsにも `http://localhost:8765/auth/callback.html` を登録してください。生成される `assets/config/auth-config.js` はGit管理外です。確認後もコミットせず、実値をREADMEやテストへ貼り付けないでください。

生成スクリプトは3変数の空値、URL形式、secret/service role形式、publishable keyでない値を拒否し、JavaScript文字列を安全にエスケープします。

### Supabase migrationの適用

このリポジトリはmigrationを自動適用しません。migrationは次の順序で適用します。

1. `supabase/migrations/20260804000000_create_member_profiles.sql`
2. `supabase/migrations/20260806000000_fix_accept_current_terms_conflict.sql`

現在の開発Supabaseには第1migrationが適用済みです。第1migrationを再実行せず、DashboardのSQL Editorで第2migrationの全文を新しいqueryへ貼り付け、1回だけ実行してください。新規環境では第1、第2の順でそれぞれ1回実行します。

第2migrationの実行後はTable Editorで変更せず、SQL Editorで次を確認してください。

1. `legal_document_versions` に `terms / 2026-08-04-draft / is_current=true` が1件ある。
2. 既存の `auth.users` ごとに `profiles` があり、`membership_status=pending_terms` である。
3. 既存ユーザー向けの `terms_acceptances` は自動生成されていない。
4. 新しい開発用ユーザーでマジックリンクを開き、同意後にprofileが `active` となり、同意履歴が1件追加される。
5. 同じ規約への再同意で履歴が重複しない。

SQL Editorへ貼り付ける前に、プロジェクト名と環境を再確認してください。service role key、secret key、DBパスワードはこの手順では使用しません。

### データ更新

```bash
python scripts/scrape.py
```

鴨池県営には追加のセレクタ設定は不要です。固定パラメータとして `category_id=483`、`area_id=289` を使用し、対象日ごとに `date=YYYY-MM-DD` を付加します。

SuMIzeiと東開は共通のP-Kashikan処理を使用します。公開トップURLと日別表示 `disp_span=0` は共通で、施設設定のコード（`029` / `131`）、施設名、対象日だけを変更します。P-Kashikanに限り、`ja-JP`、`Asia/Tokyo`、Desktop ChromeのUser-Agent、`Accept-Language`、1440×1000 viewport、JavaScript有効の通常ブラウザ設定を使用し、同一実行内ではブラウザセッションを再利用します。`navigator.webdriver`を隠すなどのアクセス制限回避は行いません。

P-KashikanがHTTP 403を返した場合は、その実行内の残りのP-Kashikanアクセスを中止します。SuMIzeiまたは東開の直前の正常データと通知基準は保持し、鴨池県営の取得は継続します。

### P-Kashikan診断情報

各P-Kashikanナビゲーションでは、HTML・PNGに加えて `*-diagnostics.json` を `snapshots/<facility-id>/` に保存します。診断JSONとActionsログには、実行環境、最終URL、HTTPステータス、ページタイトル、response/request headers、User-Agent、`navigator.webdriver`、Cookie名、本文中のアクセス制限関連マーカーを記録します。

Cookie値、Authorization、APIキー、token・secretを含むヘッダー値は `<redacted>` とし、Cookieは名前だけを保存します。403本文では `Access denied`、`Forbidden`、Cloudflare、Akamai、Imperva、Incapsula、Bot、Request ID、IP restriction、rate limitを確認します。

## LINE通知

GitHubリポジトリの `Settings` → `Secrets and variables` → `Actions` で次のRepository secretsを登録します。

| Secret | 用途 |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging APIのチャネルアクセストークン |
| `LINE_USER_ID` | Push messageの通知先ユーザーID |

Secretsが未設定の場合は通知だけをスキップし、取得とJSON更新は継続します。Secretsの値はログに出力しません。

LINE通知対象は鴨池県営テニスコートとSuMIzeiテニスコートです。東開庭球場は監視・`availability.json`への保存・GitHub Pages表示・エラー時のfallbackを継続しますが、LINE通知対象外です。

通常通知には施設名、日付と日本語曜日、コート名、時間、予約ページURLを含め、同一施設・同一日付をまとめます。[LINE Messaging APIの仕様](https://developers.line.biz/en/reference/messaging-api/#text-message)に合わせ、UTF-16で5000文字以下のテキストへ分割し、1リクエスト最大5メッセージ、超過分は複数リクエストで送ります。HTTPタイムアウトは20秒です。

全リクエストが2xxで完了した場合だけ、通知対象施設の通知比較基準を現在値へ進めます。HTTPエラー、タイムアウト、通信エラー、Secrets不足の場合は `availability.json` を更新したまま、通知対象施設の候補IDを基準へ追加しません。次回実行で同じ候補を再検出できます。通知対象外の東開は送信結果にかかわらず観測済みへ進め、再通知候補として保持しません。レスポンス本文、トークン、ユーザーIDはログへ出しません。

通常実行で `send_notification=false` を明示した場合は、通知を送らず現在値へ基準を進めます。これは通知を再度有効にした際に、抑止期間中の古い空きをまとめて送らないためです。候補を将来再通知したい場合は `send_notification=true` のままSecretsやAPIエラーを解消してください。

`test_notification=true` は「鹿児島テニス空き通知の接続テストです。」という固定文面を1件だけ送ります。実在する空きや通知比較基準は使用しません。

## GitHub Actionsの安全な開始手順

`Actions` → `Update tennis availability` → `Run workflow` から、次の順序で確認します。

1. `dry_run=true`、他はすべて `false` で実行
2. `reservation-page-snapshots` Artifact内の3施設のHTML・PNG・P-Kashikan診断JSON、`run-output/availability.json`、`run-output/notification-state.json` を確認
3. `dry_run=false`、`initialize_notification_baseline=true`、他は `false` で基準化
4. `dry_run=false`、`test_notification=true` で固定テストメッセージを1件送信
5. `dry_run=false`、`send_notification=true` で実差分通知を確認
6. Repository Variablesを設定して定期実行を有効化

`dry_run=true` が最優先です。取得とArtifact生成は行いますが、LINE送信、リポジトリ内JSON更新、commit、push、Pagesデプロイは行いません。`test_notification`、`initialize_notification_baseline`、`send_notification` を同時に指定してもdry-run中はすべて抑止されます。

初回基準化では現在枠を `notification-state.json` に保存し、空き通知は送りません。既に基準化済みでも `initialize_notification_baseline=true` を指定すれば、通知なしで現在値へ再基準化できます。

### Actions Variables

`Settings` → `Secrets and variables` → `Actions` → `Variables` で設定します。

| Variable | 用途 |
| --- | --- |
| `ENABLE_SCHEDULED_RUNS` | `true` のときだけcron実行を許可 |
| `ENABLE_LINE_NOTIFICATIONS` | `true` のときだけ定期実行の差分通知を許可 |
| `SUPABASE_URL` | ブラウザ公開用のSupabase Project URL |
| `SUPABASE_PUBLISHABLE_KEY` | ブラウザ公開用のpublishable key |
| `AUTH_CALLBACK_URL` | Supabaseに許可登録した本番callback URL |

有効化フラグが未設定または `true` 以外の場合は安全側に倒します。定期実行自体を開始するには `ENABLE_SCHEDULED_RUNS=true`、定期LINE通知も行うには加えて `ENABLE_LINE_NOTIFICATIONS=true` が必要です。手動実行は `ENABLE_SCHEDULED_RUNS` に関係なく利用できます。Pagesデプロイ時は認証用3変数がすべて必須で、空値なら設定生成を失敗させてデプロイしません。これらは公開値でありRepository SecretsではなくVariablesへ設定します。secret key、service role key、DBパスワードは登録・使用しません。

## GitHub ActionsとPages

cronは `0,30 0-14,22-23 * * *` を維持しています。UTCから換算すると、JST 07:00〜23:30の30分間隔です。ただし `ENABLE_SCHEDULED_RUNS=true` になるまで定期ジョブは実行されません。

Pages画面の「最終更新」は、`availability.json` 全体が生成された `generated_at` を示します。各施設の「最終確認」は、その施設の日別データにある最新の `checked_at` を示すため、施設間や最終更新との間に時刻差が生じることがあります。画面は最終更新から60分超で「更新が遅れています」、120分超で「2時間以上更新されていません」と警告します。取得エラーは別に表示し、取得できた日と空き候補は引き続き表示します。

1. 固定済み依存関係とChromiumをセットアップ
2. pytestを実行
3. `scripts/scrape.py` で全施設と通知状態を更新
4. スナップショット、実行時JSON、`index.html`、Phase 1静的画面と共通assetsを `reservation-page-snapshots` Artifactとして常時保存
5. dry-runでなければ意味のある2つのJSON変更だけをコミット
6. 別ジョブがRepository Variablesから `_site/assets/config/auth-config.js` を生成
7. Pages専用権限で `index.html`、最新JSON、認証画面、法務画面、共通assetsをデプロイ

取得ジョブだけが `contents: write`、Pagesジョブだけが `pages: write` と `id-token: write` を持ちます。dry-runではcommitとPagesジョブを実行しません。一部施設の取得失敗は日別のエラーとしてJSONへ記録し、他施設の処理を継続します。初回実行前に、GitHubリポジトリの `Settings` → `Pages` でSourceを `GitHub Actions` に設定してください。

`concurrency` はブランチごとの `tennis-availability-${{ github.ref }}`、`cancel-in-progress=false` です。同一ブランチのActions実行は直列化されます。Actions以外から同時にpushされてpush競合が起きた場合は上書きせずジョブを警告付きで失敗させます。Artifactはcommitより先に保存されるため、内容を確認してworkflowを再実行してください。

## 今後の作業

1. Supabaseプロジェクト、リージョン、料金枠、環境分離を決定・作成する
2. GitHub Pagesの本番callback URLをSupabaseのRedirect URLsへ登録し、Repository Variablesを設定する
3. メールマジックリンク、SMTP、リンク有効期限、レート制限を設定して実環境smoke testを行う
4. migrationを開発用Supabaseへ手動適用し、複数の架空ユーザーでRLSと同意RPCを実DB検証する
5. 退会Edge Functionと保持・削除方針を別実装する
6. 利用規約とプライバシーポリシーの暫定初版を確認し、版番号・発効日・問い合わせ先を確定する
7. GitHub Actionsの外部ActionをコミットSHAで固定する
8. サイト利用規約と適切なアクセス頻度を継続確認する

## 注意事項

- 自動予約は実装していません。
- 会員DB、規約同意履歴、RLS、規約同意RPC、会員情報表示はmigrationと静的フロントエンドへ実装済みですが、Supabase環境への適用と実DB RLS検証は人手で行う必要があります。退会処理は未実装です。
- 短い間隔でのアクセスや過剰な並列実行は避けてください。
- 予約サイトの仕様変更により取得できなくなる可能性があります。
- `availability.json` とGitHub Pagesは公開情報として扱ってください。

## ライセンス

ライセンスは未設定です。再利用・配布条件を明確にする場合は、運用開始前に追加してください。
