# amazon-listing-monitor（Amazon カート/出品 緊急監視）

Amazon の **カート喪失 / 自社出品消失(停止) / 検索対象外 / 他社相乗り / ベストセラー点灯・消失 / バリエーション解体**
を15分毎にクラウド監視し、Chatwork へ通知する。PCオフでも動く。

## 仕組み
- `monitor.py` が SP-API（`getItemOffers`＝カート/相乗り・価格、`getListingsItem`＝検索対象外/出品状態、
  `searchCatalogItems`＝ベストセラー/バリエーション、FBA Inventory＝在庫）で各ASINを判定。
- **監視ASIN・SKU・商品名・ミュート・状態の正本＝非公開の Google スプレッドシート**（このリポジトリには商品リストを置かない）。
  - 「アラート除外」=TRUE のASINは通知しない（ミュート）。
  - カート落ち/検索対象外は解消かミュートまで毎回通知。相乗り/ベストセラー/バリエーションは変化時のみ。
  - ベストセラータグ推定 = **カテゴリ1位 × ノード商品数100件以上**（親ASIN単位・Amazon非公開の経験則）。
    ノード商品数は「ノード内アイテムの最大salesRank ≒ 親ASIN数」で推定し、管理シートの
    「ノードサイズ」タブ（自動作成）に日次キャッシュ。1位だが100件未満は `RANK1`（タグ対象外）として区別。
- 15分毎の起動は**外部cron（cron-job.org 等）→ GitHub `workflow_dispatch` API**で行う
  （GitHub内蔵cronは遅延/スキップが大きく15分を満たせないため不使用）。

## Secrets（Settings → Secrets and variables → Actions）
SPAPI_REFRESH_TOKEN / SPAPI_LWA_CLIENT_ID / SPAPI_LWA_CLIENT_SECRET /
SPAPI_MARKETPLACE_ID / SPAPI_HOST / OWN_SELLER_ID /
CHATWORK_TOKEN / CHATWORK_ROOM_ID / GOOGLE_SA_JSON / SHEET_ID

## 前提
- 管理シートをサービスアカウント（GOOGLE_SA_JSON の client_email）に「編集者」で共有。
- SP-API は Pricing / Listings / Catalog / Inventory 系ロール付きトークン。

🔴 このリポジトリは public だが、**認証情報・売上・顧客・商品リストは一切含まない**
（SP-API/Chatwork/SA トークンは GitHub Actions の暗号化シークレットのみ。商品リストは非公開シート）。

## 在庫シートのデータ健全性チェック（sheet_health.py）
在庫アラート（inventory_alert.py）実行時に、在庫管理シートのデータ収集が
「静かに止まっていないか」を毎朝点検する。異常があった日だけ Chatwork へ警告
（本文配信の曜日ゲートとは独立。同内容の連投は抑制し、変化時と月曜のみ再投稿）。

点検項目: ①エラー値（#REF!/#ERROR! 等＝IMPORTRANGE切れ・参照破壊）
②更新停止（ソースタブのデータ指紋が2日以上不変） ③行数激減（前回比50%未満）
④SKU突合の欠落（フォーマットのSKU/ASINが参照先タブに無い＝SUMIFSが0を返す温床）
⑤基準日セルの停滞（在庫切れ予想日の起点が古い）。

前回状態は bot 専用の隠しタブ「⚙️bot_state」に保存。対象タブはブランド別に
`brands.py` の `health_tabs` で定義（実シートで gid・キー列を実地検証してから追加。
未設定ブランドはスキップ）。`--no-health` で無効化。
