# kagoshima-tennis-alert

鹿児島県のテニスコート予約サイトを確認し、直近15日間の土日祝にある8:00〜13:00の空き候補を、GitHub PagesとLINEで知らせるためのプロジェクトです。

> [!IMPORTANT]
> 現在は実装基盤まで完成した段階です。鴨池県営とSuMIzeiの実DOMに対応するセレクタは未確定で、設定されるまではデータに `selector_pending` と記録されます。自動予約、ログイン、ID・パスワード保存は行いません。

## 現在の機能

- 今日を含む直近15日間から土曜日、日曜日、日本の祝日を抽出
- 8:00〜13:00と重なる空き候補だけを保持
- 鴨池県営とSuMIzeiを独立した取得関数として定義
- Playwrightによる取得処理を差し替え可能なインターフェースとして分離
- `data/availability.json` に取得結果を保存
- 前回データと比較し、新規の空き候補だけを検出
- 新規候補がある場合だけLINE Messaging APIで通知
- JSONを読み込むスマートフォン向けGitHub Pages画面
- pytestによる日付、時間帯、抽出、差分、JSONの単体テスト
- GitHub Actionsによる定期更新、テスト、Pages配信

空き状況は候補です。予約前に必ず公式サイトで最新情報を確認してください。

## ファイル構成

```text
.
├── .github/
│   └── workflows/
│       └── update-availability.yml  # テスト、データ更新、Pages配信
├── data/
│   └── availability.json            # Pagesと差分検出で使う公開データ
├── scripts/
│   ├── __init__.py
│   └── scrape.py                    # 日付生成、取得、差分、LINE通知
├── tests/
│   └── test_scrape.py               # 単体テスト
├── index.html                       # スマートフォン向け表示画面
├── requirements.txt                 # 固定済みPython依存関係
└── README.md
```

## availability.json

データは次の構造で保存します。

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-21T12:00:00+09:00",
  "window": {
    "days": 15,
    "start": "08:00",
    "end": "13:00",
    "timezone": "Asia/Tokyo"
  },
  "facilities": [
    {
      "id": "kamoike",
      "name": "鴨池県営テニスコート",
      "dates": [
        {
          "date": "2026-07-25",
          "day_type": "weekend",
          "holiday_name": null,
          "status": "ok",
          "message": null,
          "slots": [
            {
              "start": "08:00",
              "end": "09:00",
              "court": "Aコート 08:00〜09:00 予約可",
              "status": "available",
              "booking_url": "https://example.com/reserve"
            }
          ]
        }
      ]
    }
  ]
}
```

日別の `status` は、正常取得時が `ok`、セレクタ未設定時が `selector_pending`、取得失敗時が `error` です。

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

### データ更新

```bash
python scripts/scrape.py
```

DOMセレクタが未設定でも実行でき、その場合は各対象日が `selector_pending` になります。

## 施設別の取得設定

実サイトのDOM調査後、次の環境変数またはGitHub Actions Variablesを設定します。

| Variable | 用途 |
| --- | --- |
| `KAMOIKE_SLOT_SELECTOR` | 鴨池県営の空き枠候補を絞り込むPlaywrightセレクタ |
| `SUMIZEI_URL_TEMPLATE` | `{date}` を含むSuMIzeiの日別予約URLテンプレート |
| `SUMIZEI_SLOT_SELECTOR` | SuMIzeiの空き枠候補を絞り込むPlaywrightセレクタ |

施設固有の調整箇所は `scrape_kamoike()` と `scrape_sumizei()` に分離されています。セレクタは、時間、コート名、予約状態を含む要素へ可能な限り狭く指定してください。

## LINE通知の設定

GitHubリポジトリの `Settings` → `Secrets and variables` → `Actions` で、次のRepository secretsを登録します。

| Secret | 用途 |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging APIのチャネルアクセストークン |
| `LINE_USER_ID` | Push messageの通知先ユーザーID |

Secretsが未設定の場合は通知をスキップし、データ更新は継続します。通常実行でテストメッセージは送信しません。

## GitHub ActionsとPages

`Update tennis availability` ワークフローは手動実行に加え、JST 7:00〜23:30の間に30分間隔で実行する設定です。

処理内容は次のとおりです。

1. 依存関係とChromiumをセットアップ
2. pytestを実行
3. `scripts/scrape.py` でデータを更新
4. データに意味のある変更がある場合だけJSONをコミット
5. `index.html` とJSONをGitHub Pagesへデプロイ

初回実行前に、GitHubリポジトリの `Settings` → `Pages` でSourceを `GitHub Actions` に設定してください。ワークフローはデータ更新のため `contents: write`、Pages配信のため `pages: write` と `id-token: write` を使用します。

## 差分通知

差分キーには施設ID、日付、開始時刻、終了時刻、コート表記を使用します。前回のJSONに存在しなかった `available` スロットだけをLINE通知の対象にします。

次のケースは現段階では通知対象外です。

- 既存スロットの説明文以外の属性変更
- 空き候補が消えた場合
- 取得エラーやDOM変更そのもの

## 今後の作業

1. 鴨池県営の実DOMを調査し、セレクタと施設固有パーサーを調整する
2. SuMIzeiの正式な予約URLと実DOMを調査する
3. 保存済みHTMLを使った施設別パーサーの回帰テストを追加する
4. 取得失敗時のエラー通知とリトライ方針を追加する
5. GitHub Actionsの外部ActionをコミットSHAで固定する
6. サイト利用規約と適切なアクセス頻度を確認する

## 注意事項

- 自動予約は実装していません。
- ログイン処理や認証情報の保存は実装していません。
- 短い間隔でのアクセスや過剰な並列実行は避けてください。
- 予約サイトの仕様変更により取得できなくなる可能性があります。
- `availability.json` とGitHub Pagesは公開情報として扱ってください。

## ライセンス

ライセンスは未設定です。再利用・配布条件を明確にする場合は、運用開始前に追加してください。
