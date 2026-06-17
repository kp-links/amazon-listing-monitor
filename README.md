# amazon-listing-monitor（Amazon カート/出品 緊急監視）

Amazon の **カート喪失 / 自社出品消失(停止) / 検索対象外 / 他社相乗り / ベストセラー点灯・消失 / バリエーション解体**
を15分毎にクラウド監視し、Chatwork へ通知する。PCオフでも動く。

## 仕組み
- `monitor.py` が SP-API（`getItemOffers`＝カート/相乗り・価格、`getListingsItem`＝検索対象外/出品状態、
  `searchCatalogItems`＝ベストセラー/バリエーション、FBA Inventory＝在庫）で各ASINを判定。
- **監視ASIN・SKU・商品名・ミュート・状態の正本＝非公開の Google スプレッドシート**（このリポジトリには商品リストを置かない）。
  - 「アラート除外」=TRUE のASINは通知しない（ミュート）。
  - カート落ち/検索対象外は解消かミュートまで毎回通知。相乗り/ベストセラー/バリエーションは変化時のみ。
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
