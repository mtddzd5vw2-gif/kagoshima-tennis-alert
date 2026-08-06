# 認証メール運用手順

## 1. 目的と対象

本書は、Tennis Court WatcherのSupabase Authが送信する初回登録確認メールと通常ログインメールについて、本番構成、設定値、日常確認、障害対応、秘密情報の管理方法を定める。

本書の対象はPhase 1の認証メールである。空き情報を利用者別に送るメール通知はPhase 3の責務であり、認証メールとは目的、テンプレート、配信停止、送信処理を分離する。

## 2. 構成概要

| 項目 | 本番設定 |
| --- | --- |
| サービスドメイン | `tenniscourtwatcher.com` |
| DNS管理・ドメイン登録 | Cloudflare Registrar |
| 認証メール送信用サブドメイン | `email.tenniscourtwatcher.com` |
| メール配信 | Resend |
| Resendリージョン | Tokyo |
| 送信元表示名 | Tennis Court Watcher |
| 送信元メール | `no-reply@email.tenniscourtwatcher.com` |
| 認証メール発行元 | Supabase Auth |
| DNS認証 | SPF、DKIM、DMARC（`p=none`） |
| Resend Enable Sending | 有効 |
| Resend Enable Receiving | 無効 |
| 同一利用者への最小送信間隔 | 60秒 |

認証メールは次の経路で送信される。

1. 利用者がGitHub Pages上の登録・ログイン画面からメール送信を要求する。
2. Supabase Authが初回登録か登録済みユーザーのログインかを判定し、対応するテンプレートを使用する。
3. Supabase AuthがResend Custom SMTPへ送信を依頼する。
4. Resendが `email.tenniscourtwatcher.com` の認証済みDNSを使用して受信側へ配信する。
5. 利用者が同じブラウザで認証リンクを開き、PKCE callbackがセッションを確立する。

## 3. 各サービスの責務

| サービス | 責務 | 保持しない・行わないこと |
| --- | --- | --- |
| Cloudflare Registrar / DNS | ドメイン登録の維持、DNSレコードの公開、SPF・DKIM・DMARCレコードの管理 | 認証メール本文の生成、Supabase利用者管理 |
| Resend | SMTP受け付け、送信ドメイン認証、メール配信、Delivered等の配信イベント表示 | Supabaseの会員認証、受信メールの運用 |
| Supabase Auth | Authユーザー管理、初回登録・通常ログインの判定、認証リンク生成、テンプレート選択、Auth Logs | DNS管理、一般利用者をOrganization Teamとして管理すること |
| GitHub Pages | 登録・ログイン画面、PKCE callback、公開設定によるSupabase接続 | SMTP password、Resend APIキー、service role keyの保持 |
| 運用者 | 設定、ログ、配信結果、DNS、受信側の順に確認し、秘密情報を安全にローテーションする | 一般利用者をSupabase Organization Teamへ追加すること |

## 4. 公開情報と秘密情報

### 4.1 公開情報

次はメール配送上公開される、または公開して差し支えない設定である。

- `tenniscourtwatcher.com`
- `email.tenniscourtwatcher.com`
- 送信元表示名 `Tennis Court Watcher`
- 送信元メール `no-reply@email.tenniscourtwatcher.com`
- SMTP host `smtp.resend.com`
- SMTP port `465`
- SMTP username `resend`
- SPF、DKIM、DMARCのDNSレコード
- Supabase project URL、publishable key、固定callback URL

公開情報であっても、運用画面全体のスクリーンショットには他の秘密値や個人情報が写り込む可能性がある。共有前に表示範囲を限定し、秘密値が写っていないことを確認する。

### 4.2 秘密情報

Resend APIキーは秘密情報であり、Supabase Custom SMTPのSMTP passwordとして使用する。APIキーの値自体は本書を含む文書へ絶対に記載しない。

次の場所へAPIキー、SMTP password、認証URL、実利用者のメールアドレスを出してはならない。

- GitHubリポジトリ、Issue、Pull Request
- GitHub PagesとPages Artifact
- GitHub Actions Artifact
- GitHub Actionsログ、アプリケーションログ、console
- テストfixture、スナップショット、画面録画
- チャット、問い合わせ本文、共有文書

秘密値をスクリーンショットへ写さない。設定確認の証跡が必要な場合は、値を非表示にした状態で項目名と更新日時だけを記録する。

## 5. Resend APIキー

Resend APIキーは次の最小権限で作成する。

- 権限: Sending access
- 対象ドメイン: `email.tenniscourtwatcher.com` のみに限定

全ドメインへ送信できるキーや不要な権限を持つキーは使用しない。キーの値は作成直後にSupabaseのSMTP passwordへ直接登録し、文書や一時ファイルへ転記しない。

## 6. Supabase Custom SMTP設定

Supabase DashboardのAuthenticationにあるSMTP設定で、次の値を使用する。

| 設定項目 | 値 |
| --- | --- |
| Custom SMTP | 有効 |
| Sender name | `Tennis Court Watcher` |
| Sender email | `no-reply@email.tenniscourtwatcher.com` |
| Host | `smtp.resend.com` |
| Port | `465` |
| Username | `resend` |
| Password | Resend APIキー |
| Minimum interval per user | `60` 秒 |

Password欄の実値はコピーして文書化しない。同一利用者への再送は60秒以上空ける。利用者から再送依頼を受けた場合も、連続操作を促さず、前回要求から60秒以上経過してから再試行する。

## 7. ResendとDNSの設定

### 7.1 送受信設定

- `email.tenniscourtwatcher.com` のEnable Sendingは有効にする。
- 認証メールは送信専用であり、ResendのEnable Receivingは不要である。無効のままとする。
- 返信受付が必要になった場合も、本設定を流用せず、問い合わせ窓口の要件として別途設計する。

### 7.2 SPF、DKIM、DMARCの確認方法

1. ResendのDomains画面で `email.tenniscourtwatcher.com` を開く。
2. ドメイン状態がVerifiedであることを確認する。
3. SPFとDKIMについて、Resendが提示するレコード名・種別・値とCloudflare DNSのレコードが一致していることを確認する。
4. DMARCレコードが対象ドメインに存在し、現在のポリシーが `p=none` であることを確認する。
5. Cloudflareでプロキシ対象にできないメール認証用DNSレコードはDNSとして公開されていることを確認する。
6. DNS変更後は反映を待ち、Resend側で再確認する。Verifiedになる前に本番送信確認を完了扱いにしない。

DNSレコードは公開情報だが、Resend APIキーやCloudflare API tokenはDNS確認に不要であり、記録やスクリーンショットへ含めない。

## 8. 日常確認

### 8.1 ResendでVerifiedを確認する

1. ResendのDomains画面を開く。
2. `email.tenniscourtwatcher.com` を選択する。
3. ドメイン状態がVerifiedであることを確認する。
4. SPF、DKIM、DMARCに警告がないことを確認する。
5. Enable Sendingが有効、Enable Receivingが無効であることを確認する。

### 8.2 ResendでDeliveredを確認する

1. ResendのEmails画面を開く。
2. 対象のテスト送信を時刻と件名で特定する。共有記録へ実メールアドレスを転記しない。
3. 配信状態がDeliveredであることを確認する。
4. Deliveredでない場合はイベント詳細を確認するが、宛先、ヘッダー、認証リンクをログやスクリーンショットへ転載しない。

Deliveredは受信側メールシステムが受け付けたことを示し、利用者の受信トレイ表示までは保証しない。

### 8.3 Supabase Auth Logsを確認する

1. Supabase Dashboardで対象の本番プロジェクトと環境を確認する。
2. Auth Logsを開く。
3. 送信要求と認証処理の成否を発生時刻で確認する。
4. レート制限、SMTP接続、テンプレート処理、callback関連のエラー有無を確認する。
5. ログを共有する場合は、メールアドレス、認証URL、code、token等を含めず、時刻と一般化したエラー種別だけを記録する。

## 9. 受信できない場合

- 会社メールでResendがDeliveredなのに届かない場合は、受信側の隔離領域、メールゲートウェイ、管理者側の検疫・拒否ルールを確認する。
- 個人メールでも迷惑メールへ入る場合は、利用者側で「迷惑メールではない」操作を行う。新規ドメインでは一部のメールサービスで迷惑メールへ分類されることがある。
- 受信トレイだけでなく迷惑メール、プロモーション等の分類も確認する。
- 再送する場合は前回から60秒以上空ける。
- 一般利用者の受信問題を解消する目的で、その利用者をSupabase Organization Teamへ追加してはならない。

## 10. 配信障害時の切り分け

次の順番で確認する。

1. Supabase Auth Logs
2. Resend Emails
3. Resendドメイン認証
4. Cloudflare DNS
5. 受信側の迷惑メール・隔離

判断の目安は次のとおりである。

| 確認結果 | 次の確認 |
| --- | --- |
| Supabaseに送信要求または認証処理のエラーがある | Custom SMTP設定、レート制限、テンプレートを確認する |
| Supabaseは成功、Resendにメールがない | SMTP host、port、username、password、送信元を確認する |
| Resendに失敗イベントがある | イベント種別とドメイン認証を確認する |
| ResendドメインがVerifiedでない | Cloudflare DNSとResend提示値を照合する |
| ResendがDelivered | 受信側の迷惑メール、隔離、メールゲートウェイを確認する |

詳細確認や共有の過程でも秘密値と個人メールアドレスを記録しない。

## 11. APIキー漏えい時の手順

漏えいまたは漏えいの疑いを認識した時点で、次の順に対応する。

1. Resendで該当キーを失効する。
2. Sending accessかつ `email.tenniscourtwatcher.com` 限定の新しいAPIキーを作成する。
3. Supabase Custom SMTPのpasswordを新しいResend APIキーへ更新する。
4. 一般利用者をTeamへ追加せず、管理されたテスト手順で認証メールを送信する。
5. Supabase Auth LogsとResend Emailsを確認し、ResendでDeliveredを確認する。
6. 漏えい経路を特定し、GitHub、Pages、Actions Artifact、ログ、スクリーンショット等への残存を確認する。

古いキーの再有効化や再利用は行わない。インシデント記録にはキーの値を記載しない。

## 12. ドメイン更新

Cloudflare Registrarのドメイン自動更新を維持し、登録情報と支払い方法の有効性を定期的に確認する。

`tenniscourtwatcher.com` が失効すると、送信用サブドメインとDNS認証を維持できず、認証メールが停止する。更新失敗の通知を見落とさない運用とし、失効後の復旧を通常手順として扱わない。

## 13. Supabase Organization TeamとAuthentication Users

Supabase Organization Teamは、Supabase Dashboardやプロジェクトを管理する運用者・開発者のための権限管理である。Authentication Usersは、Tennis Court Watcherへ登録・ログインする一般利用者である。

| 区分 | 対象 | 用途 |
| --- | --- | --- |
| Organization Team | 承認された運用者・開発者 | Dashboard、設定、ログ、プロジェクトの管理 |
| Authentication Users | Tennis Court Watcherの一般利用者 | サービスへの登録、メール認証、ログイン |

一般利用者をSupabase Organization Teamへ追加してはならない。Team所属はメール送信許可やログイン成功の前提ではない。本番では、Teamに所属していない一般メールアドレスへの送信とログインが成功することを確認済みである。

## 14. 認証メールテンプレート

Supabase Authでは、初回登録にConfirm sign up、登録済みユーザーの通常ログインにMagic link or OTPを使用する。両者は件名と本文を分け、利用者が操作の目的を判別できるようにする。

テンプレート内のSupabase変数 `{{ .ConfirmationURL }}` は変更してはならない。変数名、波括弧、先頭のドットを変更したり、固定URLへ置き換えたりしない。

以下は現在の文言を再現できる最小HTMLテンプレートである。外部画像、追跡スクリプト、秘密値を含めない。

### 14.1 Confirm sign up

件名:

```text
【Tennis Court Watcher】メールアドレスの確認
```

本文:

```html
<!doctype html>
<html lang="ja">
  <body>
    <h1>メールアドレスの確認</h1>
    <p>Tennis Court Watcherの会員登録がリクエストされました。</p>
    <p>次のリンクを押すと、メールアドレスの確認とログインが完了します。</p>
    <p>
      <a href="{{ .ConfirmationURL }}">メールアドレスを確認してログインする</a>
    </p>
    <p>この登録に心当たりがない場合は、このメールを削除してください。</p>
    <p>このメールは送信専用です。返信いただいても確認できません。</p>
  </body>
</html>
```

### 14.2 Magic link or OTP

件名:

```text
【Tennis Court Watcher】ログインリンクのお知らせ
```

本文:

```html
<!doctype html>
<html lang="ja">
  <body>
    <h1>ログインリンクのお知らせ</h1>
    <p>Tennis Court Watcherへのログインがリクエストされました。</p>
    <p>次のボタンからログインしてください。</p>
    <p>
      <a href="{{ .ConfirmationURL }}">Tennis Court Watcherにログインする</a>
    </p>
    <p>このリンクは短時間かつ一度のみ有効です。</p>
    <p>同じブラウザでは、ログアウトしない限り通常セッションが保持されます。</p>
    <p>この操作に心当たりがない場合は、このメールを削除してください。</p>
    <p>このメールは送信専用です。返信いただいても確認できません。</p>
  </body>
</html>
```

## 15. 本番確認済み事項

- SPF、DKIM、DMARC（`p=none`）が認証済みである。
- Resendで `email.tenniscourtwatcher.com` がVerifiedである。
- Enable Sendingが有効、Enable Receivingが無効である。
- Confirm sign upとMagic link or OTPを日本語化済みである。
- Supabase Organization Teamに所属していない一般メールアドレスへ送信できる。
- 一般利用者が初回登録と通常ログインを完了できる。
- 同じブラウザで通常セッションが保持される。
- ResendのEmails画面でDeliveredを確認できる。
