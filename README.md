# kagoshima-tennis-alert

鹿児島県のテニスコート予約サイトを確認し、直近15日間の土日祝にある8:00〜13:00の空き候補を、GitHub PagesとLINEで知らせるプロジェクトです。

> [!IMPORTANT]
> 鴨池県営テニスコートとSuMIzeiテニスコートは、いずれも認証不要の実画面に対応済みです。自動予約、ログイン、利用者ID・パスワードの使用や保存は行いません。

## 現在の機能

- 今日を含む直近15日間から土曜日、日曜日、日本の祝日を抽出
- 8:00〜13:00内で1時間以上ある空きだけを保持
- 同一コートの連続した空きセルを結合
- 鴨池県営のVue生成DOMをコート行・時刻ヘッダー・状態セル単位で解析
- SuMIzeiの公開フォームを施設コード・対象日で遷移し、コート行の状態セルを解析
- 成功、空き0件、取得エラーを区別して `data/availability.json` に保存
- 前回データにない `slot_id` だけをLINE通知
- JSONを読み込むスマートフォン向けGitHub Pages画面
- 成功・失敗を問わず診断用HTMLとPNGを保存
- pytest、GitHub Actions、Pages自動配信

空き状況は候補です。予約前に必ず公式サイトで最新情報を確認してください。

## ファイル構成

```text
.
├── .github/workflows/update-availability.yml
├── data/availability.json
├── scripts/
│   ├── __init__.py
│   └── scrape.py
├── tests/
│   ├── fixtures/kamoike_schedule.html
│   ├── fixtures/sumizei_schedule.html
│   └── test_scrape.py
├── index.html
├── requirements.txt
└── README.md
```

実行時には鴨池県営を `snapshots/kamoike-prefectural/YYYY-MM-DD.html`、SuMIzeiを `snapshots/sumizei/YYYY-MM-DD-step-name.html` と同名のPNGへ保存します。SuMIzeiはトップ、施設検索、施設選択後、対象日の空き状況を段階別に保存します。スナップショットはGit管理せず、GitHub ActionsのArtifactとして7日間保存します。

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

## SuMIzeiの抽出方式

2026年7月21日にPlaywrightで認証なしの画面遷移と通信を確認しました。

1. トップの「施設 の空きを見る」から `index.php` の施設空き状況へ遷移
2. `input[name="ShisetsuCode"]` から `#scd029`（値 `029`）の「ＳｕＭＩｚｅｉテニスコート」を選択
3. 公開画面が通常使用するフォーム値を対象日に変更して日別画面を表示
4. `.SelectCalendar` 内の時間ヘッダーとコート行だけを解析

内部APIやJSONエンドポイントは使用されていませんでした。画面遷移は `index.php` への通常のPOSTで、次の値を送信します。

| Form値 | 内容 |
| --- | --- |
| `op` | `srch_sst`（施設の空き状況） |
| `ShisetsuCode` | `029` |
| `UseYM` | `YYYYMM` |
| `UseDay` | 月内の日 |
| `UseDate` | `YYYYMMDD` |
| `disp_span` | `0`（1日表示） |

実DOMでは、時間軸が `.SelectCalendar table.koma-table th`、各コート名が `td.name` にあります。インターネット予約可能な空きセルは `○` と表示され、セルの `id` と `onmousedown` に施設・コート識別子、日付、`HHMMHHMM` 形式の開始・終了時刻が含まれます。実際に確認した例は次の形です。

```html
<td id="029|004|...#2026/07/23#1"
    onmousedown="setAppStatus('029|004|...', '2026/07/23', 1, '09001000', ...);">
  ○
</td>
```

パーサーはコート行内の `●`、`○`、`〇` だけを空き候補とし、`×`、`-`、`確認中`、予約済みなどを除外します。`○` は実属性の時間帯を優先し、`●` は同じ行のセル幅と時間ヘッダーから時間を復元します。凡例は `.SelectCalendar` 外なので解析対象になりません。施設コード、選択日、時間ヘッダー、コート行のいずれかが不整合なら、空き0件ではなく施設単位のエラーにします。

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
- `selector_pending`: 旧データとの互換用。現在の2施設では生成しない

エラー時も `checked_at`、`reservation_url`、空の `availability` を保存します。主な `error_type` は `navigation_timeout`、`navigation_error`、`access_denied`、`facility_not_found`、`date_selection_failed`、`no_schedule_table`、`unexpected_dom` です。

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

### データ更新

```bash
python scripts/scrape.py
```

鴨池県営には追加のセレクタ設定は不要です。固定パラメータとして `category_id=483`、`area_id=289` を使用し、対象日ごとに `date=YYYY-MM-DD` を付加します。

SuMIzeiも追加のURL・セレクタ設定は不要です。公開トップURL、施設コード `029`、日別表示 `disp_span=0` を固定し、対象日だけを変更します。

## LINE通知

GitHubリポジトリの `Settings` → `Secrets and variables` → `Actions` で次のRepository secretsを登録します。

| Secret | 用途 |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging APIのチャネルアクセストークン |
| `LINE_USER_ID` | Push messageの通知先ユーザーID |

Secretsが未設定の場合は通知だけをスキップし、取得とJSON更新は継続します。Secretsの値はログに出力しません。

差分は施設・日付ごとに前回の `status` も確認します。前回が `success` だった日へ新しく現れた `slot_id` は通知しますが、`error` や `selector_pending` から `success` へ復旧した直後は、復旧を新規空きと誤認しないよう通知を抑止します。

## GitHub ActionsとPages

`Update tennis availability` は手動実行に加え、JST 7:00〜23:30の間に30分間隔で実行します。

1. 固定済み依存関係とChromiumをセットアップ
2. pytestを実行
3. `scripts/scrape.py` で全施設を更新
4. `snapshots` を `reservation-page-snapshots` Artifactとして常時保存
5. 意味のあるJSON変更だけをコミット
6. `index.html` とJSONをGitHub Pagesへデプロイ

一部施設の取得失敗は日別のエラーとしてJSONへ記録し、他施設の処理を継続します。初回実行前に、GitHubリポジトリの `Settings` → `Pages` でSourceを `GitHub Actions` に設定してください。

## 今後の作業

1. 取得失敗時のLINEエラー通知方針を追加する
2. GitHub Actionsの外部ActionをコミットSHAで固定する
3. サイト利用規約と適切なアクセス頻度を継続確認する

## 注意事項

- 自動予約は実装していません。
- ログイン処理や認証情報の保存は実装していません。
- 短い間隔でのアクセスや過剰な並列実行は避けてください。
- 予約サイトの仕様変更により取得できなくなる可能性があります。
- `availability.json` とGitHub Pagesは公開情報として扱ってください。

## ライセンス

ライセンスは未設定です。再利用・配布条件を明確にする場合は、運用開始前に追加してください。
