"""Amazon カート/出品 緊急監視ポーラー（悩み解決ラボ）。

GitHub Actions で15分毎にクラウド実行（PCオフでも動く）。SP-API getItemOffers で
各ASINの Buy Box 状態を取得し、以下を検知して新規発生(onset)時のみ Chatwork に通知する:
  - カート喪失      : 自社オファーはあるが Buy Box を取れていない
  - 自社出品消失/停止: 自社オファーがオファー一覧に存在しない（出品停止・カート出せず）
  - 他社相乗り      : 自社以外のセラーがオファーに存在
状態は制御スプレッドシート（ASIN/アラート除外/最終ステータス…）を読み書きして保持する。
「アラート除外」=TRUE のASINは通知しない（ミュート）。
カート喪失/自社出品消失は未解決の間、ミュートまで毎回(15分毎)通知し続ける。相乗りは新規セラー出現時のみ通知。

運用系の通知（2026-07-23追加・2026-08-05改定）:
  - エラー通知  : サイクル失敗（トークン/シート/API致命）が3回連続した時のみChatwork通知
                  （単発の一時失敗では鳴らさない）。継続中は60分間隔に抑制し、通知済みエラーが
                  復旧した最初の成功サイクルで「✅復旧」を通知。認可失効(invalid_grant)のみ
                  自然復旧が無いため1回目から即通知。
                  ※毎朝の「✅正常稼働」通知は2026-08-05に廃止（正常時は無通知。
                    Actionsごと停止した場合はCF Worker cart-monitor-dispatcher のwatchdogが検知）。
  - 新商品同期  : 日次1回 searchListingsItems で出品一覧とシートを突合し、未登録のBUYABLE出品を
                  A:E列へ自動追記（商品名はAmazon商品名からの仮短縮名「…(仮)」・要人手修正）。
  - 再認可期限  : SP-API認可は付与から365日で失効する。Actions variable REAUTH_DEADLINE_<TENANT>
                  (YYYY-MM-DD) が設定されていれば、期限の60/30/14/7/3/2/1日前と当日以降毎日、
                  [toall]付き高視認リマインドを日1回通知。トークンが実際に失効(invalid_grant)した
                  場合はエラー通知を🚨認可失効の専用文言に格上げする。

※ 検索対象外(サーチ抑制だが購入可)の厳密検知は getListingsItem ベースの v1.1 で追加予定。
   本v1は「カート落ち（喪失/停止/相乗り）」を対象（最優先要件）。

環境変数（GitHub Actions secrets から注入）:
  SPAPI_REFRESH_TOKEN / SPAPI_LWA_CLIENT_ID / SPAPI_LWA_CLIENT_SECRET
  SPAPI_MARKETPLACE_ID (例 A1VC38T7YXB528) / SPAPI_HOST (例 sellingpartnerapi-fe.amazon.com)
  OWN_SELLER_ID (例 A308PH94VO9URO)
  CHATWORK_TOKEN / CHATWORK_ROOM_ID (例 439649765)
  GOOGLE_SA_JSON (サービスアカウントJSON文字列) / SHEET_ID
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

JST = timezone(timedelta(hours=9))
# 監視ASIN・SKU・商品名・ミュート・状態の正本＝非公開の管理スプレッドシート（SHEET_ID）。
# リポジトリには商品リストを置かない（public化のため）。新規ASINはシートに行追加で対応。

# 列: A=ASIN B=商品名(簡易) C=SKU D=アラート除外 E=メモ F=最終ステータス G=最終チェック
#     H=相乗りセラーID I=ベストセラー J=バリエーション親 K=ブラウズノード（F以降は自動更新）
# 列I の値: TRUE=タグ推定 / RANK1=カテゴリ1位だがノード商品数<100でタグ対象外 / FALSE。
# ノード商品数のキャッシュは別タブ NODE_TAB（自動作成・日次更新）。
(COL_ASIN, COL_NAME, COL_SKU, COL_MUTE, COL_MEMO, COL_STATUS, COL_CHECK,
 COL_SELLERS, COL_BEST, COL_PARENT, COL_BROWSE) = range(11)
HEADER = ["ASIN", "商品名", "SKU", "アラート除外", "メモ",
          "最終ステータス(自動)", "最終チェック(自動)", "相乗りセラーID(自動)",
          "ベストセラー(自動)", "バリエーション親(自動)", "ブラウズノード(自動)"]
STOREFRONT = "https://www.amazon.co.jp/sp?seller={}"  # セラー名はSP-APIで取れずURLで代替

# ベストセラータグ付与のノード商品数条件（親ASIN単位・Amazon非公開の経験則）。
# カテゴリ1位でもノード商品数が閾値未満だとタグは付かない（例: 微量ミネラル96件のピュアビタC）。
BSR_MIN_NODE_ITEMS = 100
NODE_TAB = "ノードサイズ"      # ノード商品数キャッシュ用タブ（自動作成）
NODE_TTL_HOURS = 24            # 商品数の再計測間隔（計測は数十APIコールかかるため日次）
NODE_TAB_HEADER = ["ノードID", "ノード名", "推定商品数(自動)", "最終計測(自動)"]

META_TAB = "監視メタ"          # 新商品同期/再認可リマインドの実施日などを保持するタブ（自動作成）
META_TAB_HEADER = ["キー", "値", "更新(自動)"]

# SP-API再認可期限リマインドを出す「残日数」の節目（当日以降は毎日）。
# 期限日は public リポにコミットせず Actions variable REAUTH_DEADLINE_<TENANT> で注入する。
REAUTH_MILESTONES = {60, 30, 14, 7, 3, 2, 1}

# エラー通知は連続失敗がこの回数に達した時のみ発火（単発の一時失敗で鳴らさない・2026-08-05）。
# 認可失効(invalid_grant)は自然復旧が無いため回数に関係なく即通知。
ERR_NOTIFY_MIN_STREAK = 3
# 通知後の継続エラーの再通知間隔。状態ファイルは同一Actionsジョブ内(~5.5h)で持続し、
# ジョブ再起動で消える＝継続エラーが跨いだ場合は再カウント(最大3サイクル≒45分の遅延)を許容。
ERR_NOTIFY_INTERVAL_SEC = 3600

# ステータス定義（severityで通知要否を判定）
ST_OK = "正常"
ST_MUTED = "ミュート中"
BAD = {
    "カート喪失": "🔴 Buy Box を他社に奪われています（カート喪失）",
    "自社出品消失": "🔴 自社オファーが消えています（出品停止・カート出せず）",
    "他社相乗り": "🟠 他社セラーが相乗りしています",
}


# 監視対象の会社（テナント）。argv[1] で指定。空＝従来の無印secrets（悩み解決ラボ互換）。
_TENANT = ""


def _env(name: str, required: bool = True) -> str:
    """環境変数を読む。会社別運用では `<NAME>_<TENANT>` を優先し、無ければ無印にフォールバック。

    例: _TENANT="nature" → SPAPI_REFRESH_TOKEN_NATURE があればそれ、無ければ SPAPI_REFRESH_TOKEN。
    lwa_client_id/secret・marketplace・host・chatwork_token・SA_JSON は全社共通＝無印を流用。
    """
    v = ""
    if _TENANT:
        v = os.getenv(f"{name}_{_TENANT.upper()}", "")
    if not v:
        v = os.getenv(name, "")
    if required and not v:
        suffix = f"（または {name}_{_TENANT.upper()}）" if _TENANT else ""
        sys.exit(f"[FATAL] 環境変数 {name}{suffix} が未設定")
    return v


# ── SP-API ───────────────────────────────────────────────────────────────
def lwa_token() -> str:
    r = requests.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": _env("SPAPI_REFRESH_TOKEN"),
        "client_id": _env("SPAPI_LWA_CLIENT_ID"),
        "client_secret": _env("SPAPI_LWA_CLIENT_SECRET"),
    }, timeout=30)
    if r.status_code == 400 and "invalid_grant" in r.text:
        raise RuntimeError(
            "SP-API認可失効(invalid_grant): refresh_tokenが無効です。セラー側の認可取消または"
            "365日期限切れの可能性。再連携（ワンクリック認可URL再発行→認可→secrets差替）が必要")
    r.raise_for_status()
    return r.json()["access_token"]


def get_item_offers(token: str, asin: str) -> dict | None:
    """getItemOffers (v0)。429/503 は指数バックオフで数回リトライ。失敗は None。"""
    host = _env("SPAPI_HOST")
    mp = _env("SPAPI_MARKETPLACE_ID")
    url = f"https://{host}/products/pricing/v0/items/{asin}/offers"
    params = {"MarketplaceId": mp, "ItemCondition": "New"}
    for attempt in range(4):
        resp = requests.get(url, params=params,
                            headers={"x-amz-access-token": token}, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("payload", resp.json())
        if resp.status_code in (429, 503):
            time.sleep(2 * (2 ** attempt))
            continue
        if resp.status_code == 404:
            return {"_notfound": True}
        # その他は一旦Noneで握る（個別ASINの失敗で全体を止めない）
        print(f"  [warn] {asin}: HTTP {resp.status_code} {resp.text[:120]}")
        return None
    return None


def _price(o: dict) -> float | None:
    p = (o or {}).get("ListingPrice") or {}
    try:
        return float(p.get("Amount"))
    except (TypeError, ValueError):
        return None


def get_listings_item(token: str, seller: str, sku: str) -> dict | None:
    """getListingsItem。status(DISCOVERABLE/BUYABLE)とERROR有無を返す。失敗None。"""
    host = _env("SPAPI_HOST")
    mp = _env("SPAPI_MARKETPLACE_ID")
    url = f"https://{host}/listings/2021-08-01/items/{seller}/{urllib.parse.quote(sku, safe='')}"
    params = {"marketplaceIds": mp, "includedData": "summaries,issues"}
    for attempt in range(3):
        resp = requests.get(url, params=params, headers={"x-amz-access-token": token}, timeout=30)
        if resp.status_code == 200:
            d = resp.json()
            summaries = d.get("summaries") or []
            summ = next((s for s in summaries if s.get("marketplaceId") == mp),
                        summaries[0] if summaries else {})
            status = summ.get("status") or []
            issues = d.get("issues") or []
            return {"searchable": "DISCOVERABLE" in status, "buyable": "BUYABLE" in status,
                    "error": any(i.get("severity") == "ERROR" for i in issues)}
        if resp.status_code in (429, 503):
            time.sleep(2 * (2 ** attempt))
            continue
        if resp.status_code == 404:
            return {"searchable": False, "buyable": False, "error": True}
        return None
    return None


def get_fba_inventory(token: str) -> dict:
    """FBA在庫サマリ → {ASIN: 出荷可能数}。失敗時は空（致命でない）。"""
    host = _env("SPAPI_HOST")
    mp = _env("SPAPI_MARKETPLACE_ID")
    url = f"https://{host}/fba/inventory/v1/summaries"
    out, next_tok = {}, None
    for _ in range(60):
        params = {"granularityType": "Marketplace", "granularityId": mp, "marketplaceIds": mp, "details": "true"}
        if next_tok:
            params["nextToken"] = next_tok
        ok = False
        for attempt in range(4):   # ページ単位で429リトライ（ページ予算を消費しない）
            resp = requests.get(url, params=params, headers={"x-amz-access-token": token}, timeout=30)
            if resp.status_code == 200:
                ok = True
                break
            if resp.status_code in (429, 503):
                time.sleep(2 * (2 ** attempt))
                continue
            print(f"  [warn] FBA在庫取得 HTTP {resp.status_code}")
            return out
        if not ok:
            return out
        body = resp.json()
        for s in (body.get("payload", {}) or {}).get("inventorySummaries", []):
            a = s.get("asin")
            q = (s.get("inventoryDetails") or {}).get("fulfillableQuantity")
            if q is None:
                q = s.get("totalQuantity")
            try:
                q = int(q) if q is not None else None
            except (TypeError, ValueError):
                q = None
            if a:
                out[a] = q
        next_tok = (body.get("pagination") or {}).get("nextToken")
        if not next_tok:
            break
    return out


def get_catalog_all(token: str, asins: list) -> dict:
    """ASIN群の {asin: {rank1_node:str, rank1_title:str, display_best:bool,
    rank:int|None, cat:str, parents:[..], browse_id:str, browse_name:str}} を batch取得。
    rank1_node/rank1_title=classificationRanks で rank==1 のノードID/名称（無ければ空）。
    タグ推定の最終判定は main 側（ノード商品数>=100 条件を加味）。
    display_best=displayGroupRanks(ドラッグストア等の表示グループ)で rank==1。
    parents=VARIATION親ASIN。browse_*=summaries.browseClassification（カテゴリ移動検知用）。"""
    host = _env("SPAPI_HOST")
    mp = _env("SPAPI_MARKETPLACE_ID")
    url = f"https://{host}/catalog/2022-04-01/items"
    out: dict = {}
    uniq = sorted({a for a in asins if a})
    for i in range(0, len(uniq), 20):
        chunk = uniq[i:i + 20]
        params = {"identifiers": ",".join(chunk), "identifiersType": "ASIN", "marketplaceIds": mp,
                  "includedData": "salesRanks,relationships,summaries", "pageSize": 20}
        resp = None
        for attempt in range(4):
            resp = requests.get(url, params=params, headers={"x-amz-access-token": token}, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 503):
                time.sleep(2 * (2 ** attempt))
                continue
            print(f"  [warn] catalog HTTP {resp.status_code}")
            resp = None
            break
        if resp is None or resp.status_code != 200:
            continue
        try:
            items = resp.json().get("items", [])
        except ValueError:
            print("  [warn] catalog JSON parse失敗")
            continue
        for it in items:
            a = it.get("asin")
            ranks, best_cat = [], None
            rank1_node = rank1_title = ""
            display_best = False
            for blk in it.get("salesRanks", []) or []:
                for rr in blk.get("classificationRanks", []) or []:
                    rk = rr.get("rank")
                    if isinstance(rk, int):
                        ranks.append(rk)
                        if rk == 1 and not rank1_node:
                            rank1_node = str(rr.get("classificationId") or "")
                            rank1_title = str(rr.get("title") or "")
                            best_cat = best_cat or rr.get("title")
                for rr in blk.get("displayGroupRanks", []) or []:
                    rk = rr.get("rank")
                    if isinstance(rk, int):
                        ranks.append(rk)
                        if rk == 1:
                            display_best = True
                            best_cat = best_cat or rr.get("title")
            parents = []
            for blk in it.get("relationships", []) or []:
                for rel in blk.get("relationships", []) or []:
                    if rel.get("type") == "VARIATION":
                        parents += rel.get("parentAsins") or []
            # ブラウズノード（summaries.browseClassification）＝対象marketplace優先
            summaries = it.get("summaries", []) or []
            summ = next((s for s in summaries if s.get("marketplaceId") == mp),
                        summaries[0] if summaries else {})
            bc = summ.get("browseClassification") or {}
            if a:
                out[a] = {"rank1_node": rank1_node, "rank1_title": rank1_title,
                          "display_best": display_best,
                          "rank": min(ranks) if ranks else None,
                          "cat": best_cat, "parents": sorted(set(parents)),
                          "browse_id": str(bc.get("classificationId") or ""),
                          "browse_name": str(bc.get("displayName") or "")}
        time.sleep(0.3)
    return out


def _node_keywords(title: str) -> list:
    """ノード名からsearchCatalogItems用キーワード候補を生成。

    classificationIds は単独指定不可（keywords必須）のため、ノード名そのもの＋
    サプリ系サフィックスを落とした短縮形で検索する。較正(2026-07-08): この2語で
    微量ミネラル=最大rank94(店頭96) / ヒアルロン酸=248 を再現。"""
    kws = [title]
    for suffix in ("サプリメント", "サプリ"):
        if title.endswith(suffix) and len(title) > len(suffix):
            kws.append(title[: -len(suffix)])
    return kws


def estimate_node_size(token: str, node_id: str, title: str) -> int | None:
    """ブラウズノード内のランク付き商品数（親ASIN単位）を推定。取得失敗は None。

    手法: searchCatalogItems(keywords=ノード名, classificationIds=node) で子ASINを
    収集し、salesRanks の当該ノード最大rank を返す（rankはバリエーション親単位で
    採番されるため 最大rank ≒ 親ASIN数。numberOfResults は子ASIN込み＋検索網羅性で
    大きくブレるため不採用）。約20〜50APIコール/ノードかかるので呼び出しは日次。"""
    host = _env("SPAPI_HOST")
    mp = _env("SPAPI_MARKETPLACE_ID")
    url = f"https://{host}/catalog/2022-04-01/items"
    if not title:
        return None
    asins: set = set()
    for kw in _node_keywords(title):
        page_token = None
        for _page in range(15):     # 300件で打ち切り（閾値100の判定には十分）
            params = {"marketplaceIds": mp, "classificationIds": node_id,
                      "keywords": kw, "pageSize": 20}
            if page_token:
                params["pageToken"] = page_token
            resp = None
            for attempt in range(4):
                resp = requests.get(url, params=params,
                                    headers={"x-amz-access-token": token}, timeout=30)
                if resp.status_code == 200:
                    break
                if resp.status_code in (429, 503):
                    time.sleep(2 * (2 ** attempt))
                    continue
                print(f"  [warn] node {node_id}: search HTTP {resp.status_code}")
                resp = None
                break
            if resp is None or resp.status_code != 200:
                break
            try:
                j = resp.json()
            except ValueError:
                break
            asins.update(it.get("asin") for it in j.get("items", []) if it.get("asin"))
            page_token = (j.get("pagination") or {}).get("nextToken")
            time.sleep(1.1)
            if not page_token:
                break
    if not asins:
        return None
    ranks = []
    uniq = sorted(asins)
    for i in range(0, len(uniq), 20):
        chunk = uniq[i:i + 20]
        params = {"identifiers": ",".join(chunk), "identifiersType": "ASIN",
                  "marketplaceIds": mp, "includedData": "salesRanks", "pageSize": 20}
        resp = None
        for attempt in range(4):
            resp = requests.get(url, params=params,
                                headers={"x-amz-access-token": token}, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 503):
                time.sleep(2 * (2 ** attempt))
                continue
            resp = None
            break
        if resp is None or resp.status_code != 200:
            continue
        try:
            items = resp.json().get("items", [])
        except ValueError:
            continue
        for it in items:
            for blk in it.get("salesRanks", []) or []:
                for rr in blk.get("classificationRanks", []) or []:
                    if (str(rr.get("classificationId")) == node_id
                            and isinstance(rr.get("rank"), int)):
                        ranks.append(rr["rank"])
        time.sleep(1.1)
    if not ranks:
        return None
    return max(max(ranks), len(ranks))


def list_all_listings(token: str, seller: str) -> list | None:
    """searchListingsItems でセラーの全出品を列挙 → [{sku, asin, title, buyable}]。失敗は None。

    20件/頁で最大60頁（1200 SKU）。ページ取得が途中で失敗した場合も None
    （部分リストで差分判定すると誤追記しないが取りこぼすため、全量成功時のみ使う）。"""
    host = _env("SPAPI_HOST")
    mp = _env("SPAPI_MARKETPLACE_ID")
    url = f"https://{host}/listings/2021-08-01/items/{seller}"
    out: list = []
    page_token = None
    for _page in range(60):
        params = {"marketplaceIds": mp, "pageSize": 20, "includedData": "summaries"}
        if page_token:
            params["pageToken"] = page_token
        resp = None
        for attempt in range(4):
            resp = requests.get(url, params=params,
                                headers={"x-amz-access-token": token}, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 503):
                time.sleep(2 * (2 ** attempt))
                continue
            print(f"  [warn] 出品一覧 HTTP {resp.status_code} {resp.text[:120]}")
            return None
        if resp is None or resp.status_code != 200:
            return None
        try:
            j = resp.json()
        except ValueError:
            print("  [warn] 出品一覧 JSON parse失敗")
            return None
        for it in j.get("items", []) or []:
            summaries = it.get("summaries") or []
            summ = next((s for s in summaries if s.get("marketplaceId") == mp),
                        summaries[0] if summaries else {})
            out.append({"sku": str(it.get("sku") or ""),
                        "asin": str(summ.get("asin") or ""),
                        "title": str(summ.get("itemName") or ""),
                        "buyable": "BUYABLE" in (summ.get("status") or [])})
        page_token = (j.get("pagination") or {}).get("nextToken") or j.get("nextToken")
        if not page_token:
            break
        time.sleep(0.3)
    else:
        # 60頁(1200 SKU)取り切れず途中終了＝部分リスト。成功扱いにすると当日中の
        # 差分検出を取りこぼすため失敗として返し、次サイクルで再試行させる。
        print(f"  [warn] 出品一覧が上限{len(out)}件で未完（nextToken残あり）→失敗扱い")
        return None
    return out


def tentative_short_name(title: str) -> str:
    """Amazonフル商品名 → 仮の短縮商品名（B列用・人手修正前提で「(仮)」を付す）。

    【】/[]の販促ブロックと括弧・区切り記号を除去し、先頭の実語（短ければ2語連結）
    を最大20文字＋容量表記（30日分/90粒 等）で構成する。"""
    if not title:
        return "(名称不明)(仮)"
    # 容量表記は「期間・個数系」を「成分量系(mg等)」より優先して拾う（例: 60粒 > 1000mg）
    m = (re.search(r"(\d+(?:\.\d+)?\s*(?:日分|ヶ月分|か月分|粒|袋|包|回分|錠|本|個))", title)
         or re.search(r"(\d+(?:\.\d+)?\s*(?:mg|g|ml|mL|kg))", title))
    cap = re.sub(r"\s+", "", m.group(1)) if m else ""
    t = re.sub(r"【[^】]*】|\[[^\]]*\]", " ", title)
    t = re.sub(r"[（）()「」『』〈〉《》｜|/／,、。・×]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    parts = [p for p in t.split(" ") if p]
    base = parts[0] if parts else title.strip()
    if len(base) < 6 and len(parts) > 1:
        base = base + parts[1]
    base = base[:20]
    if cap and cap not in base:
        base = f"{base} {cap}"
    return base + "(仮)"


def sync_new_listings(gtok: str, sheet_id: str, token: str, seller: str) -> int:
    """出品一覧とシートを突合し、未登録のBUYABLE出品をA:E列へ自動追記。

    返り値: 追記件数（0=新規なし）／-1=出品一覧の取得失敗（呼び出し側で当日中に再試行）。
    既知ASIN/SKUのいずれかに一致すればスキップ（同一ASINの複数SKUは1行に集約）。
    廃盤・出品停止（BUYABLEでない）出品は追加しない。"""
    listings = list_all_listings(token, seller)
    if listings is None:
        print("  [warn] 新商品同期: 出品一覧の取得失敗→次サイクルで再試行")
        return -1
    rows = sheet_get(gtok, sheet_id, "A2:C")
    known_asins = {str(r[0]).strip() for r in rows if r and str(r[0]).strip()}
    known_skus = {str(r[2]).strip() for r in rows if len(r) > 2 and str(r[2]).strip()}
    new = [l for l in listings
           if l["buyable"] and l["asin"] and l["sku"]
           and l["asin"] not in known_asins and l["sku"] not in known_skus]
    seen: set = set()
    uniq = []
    for l in sorted(new, key=lambda x: x["sku"]):
        if l["asin"] in seen:
            continue
        seen.add(l["asin"])
        uniq.append(l)
    print(f"  [sync] 出品{len(listings)}件 / シート既登録{len(rows)}行 / 新規{len(uniq)}件")
    if not uniq:
        return 0
    today = datetime.now(JST).strftime("%Y-%m-%d")
    add_rows = [[l["asin"], tentative_short_name(l["title"]), l["sku"], "FALSE",
                 f"自動追加{today}・仮名要修正"] for l in uniq]
    start = 2 + len(rows)
    sheet_update(gtok, sheet_id, f"A{start}:E{start + len(add_rows) - 1}", add_rows)
    lines = [f"・{r[0]} {r[1]}（SKU: {r[2]}）" for r in add_rows[:20]]
    if len(add_rows) > 20:
        lines.append(f"…他{len(add_rows) - 20}件")
    body = (f"[info][title]📦 新商品を監視シートへ自動追加（{_tenant_label()}） {today}[/title]\n"
            + "\n".join(lines)
            + "\n[hr]商品名はAmazon商品名からの仮短縮名です。管理シートB列を確認・修正してください。"
              "アラート除外=FALSE（監視対象）で追加済み。[/info]")
    try:
        chatwork_post(body)
    except Exception as e:   # 追記自体は完了済み。通知失敗は握らず記録のみ（fail-loud）
        print(f"  [warn] 新商品追加の通知失敗（シート追記は完了）: {e}")
    return len(add_rows)


def classify(payload: dict, own_seller: str) -> str:
    """getItemOffers payload → ステータス文字列。"""
    if not payload or payload.get("_notfound"):
        return "自社出品消失"
    offers = payload.get("Offers", []) or []
    own = [o for o in offers if o.get("SellerId") == own_seller]
    own_has_bb = any(o.get("IsBuyBoxWinner") for o in own)
    if not own:
        return "自社出品消失"
    if not own_has_bb:
        return "カート喪失"
    return ST_OK


# ── Google Sheets ─────────────────────────────────────────────────────────
def sheets_service_token() -> str:
    info = json.loads(_env("GOOGLE_SA_JSON"))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(GoogleRequest())
    return creds.token


def sheet_get(token: str, sheet_id: str, rng: str) -> list[list]:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("values", [])


def sheet_update(token: str, sheet_id: str, rng: str, values: list[list]) -> None:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
    r = requests.put(url, headers={"Authorization": f"Bearer {token}"},
                     params={"valueInputOption": "RAW"},
                     json={"values": values}, timeout=30)
    r.raise_for_status()


def _add_tab(token: str, sheet_id: str, title: str) -> None:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"},
                      json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
                      timeout=30)
    if r.status_code == 400 and "already exists" in r.text:
        return
    r.raise_for_status()


def load_node_sizes(token: str, sheet_id: str) -> dict:
    """ノードサイズタブ → {node_id: {"title","size","ts"}}。タブが無ければ作成して空。"""
    try:
        rows = sheet_get(token, sheet_id, f"'{NODE_TAB}'!A2:D")
    except requests.HTTPError:
        _add_tab(token, sheet_id, NODE_TAB)
        sheet_update(token, sheet_id, f"'{NODE_TAB}'!A1:D1", [NODE_TAB_HEADER])
        return {}
    out = {}
    for r in rows:
        r = (r + [""] * 4)[:4]
        nid = str(r[0]).strip()
        if not nid:
            continue
        try:
            size = int(str(r[2]).strip())
        except ValueError:
            size = None
        ts = None
        try:
            ts = datetime.strptime(str(r[3]).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            pass
        out[nid] = {"title": str(r[1]).strip(), "size": size, "ts": ts}
    return out


def save_node_sizes(token: str, sheet_id: str, sizes: dict) -> None:
    rows = [[nid, e.get("title") or "",
             "" if e.get("size") is None else e["size"],
             e["ts"].strftime("%Y-%m-%d %H:%M") if e.get("ts") else ""]
            for nid, e in sorted(sizes.items())]
    if rows:
        sheet_update(token, sheet_id, f"'{NODE_TAB}'!A2:D{1 + len(rows)}", rows)


def load_meta(token: str, sheet_id: str) -> dict:
    """監視メタタブ → {キー: 値}。タブが無ければ作成して空を返す。"""
    try:
        rows = sheet_get(token, sheet_id, f"'{META_TAB}'!A2:B")
    except requests.HTTPError:
        _add_tab(token, sheet_id, META_TAB)
        sheet_update(token, sheet_id, f"'{META_TAB}'!A1:C1", [META_TAB_HEADER])
        return {}
    return {str(r[0]).strip(): (str(r[1]).strip() if len(r) > 1 else "")
            for r in rows if r and str(r[0]).strip()}


def save_meta(token: str, sheet_id: str, meta: dict) -> None:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    rows = [[k, meta[k], now] for k in sorted(meta)]
    if rows:
        sheet_update(token, sheet_id, f"'{META_TAB}'!A2:C{1 + len(rows)}", rows)


# ── Chatwork ──────────────────────────────────────────────────────────────
def chatwork_post(message: str) -> None:
    tok = _env("CHATWORK_TOKEN")
    room = _env("CHATWORK_ROOM_ID")
    r = requests.post(f"https://api.chatwork.com/v2/rooms/{room}/messages",
                      headers={"X-ChatWorkToken": tok},
                      data={"body": message, "self_unread": "1"}, timeout=30)
    r.raise_for_status()


# ── エラー即時通知 / 復旧通知 ─────────────────────────────────────────────
def _tenant_label() -> str:
    return _TENANT or "thirdknowledge"


def _err_state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f".errnotify_{_tenant_label()}")


def _load_err_state() -> tuple[int, float]:
    """状態ファイル → (連続失敗回数, 最終通知ts)。

    形式は「回数,最終通知ts」。旧形式（ts単独＝通知済みエラー継続中）は
    通知済み扱い(回数=ERR_NOTIFY_MIN_STREAK)で引き継ぐ。"""
    try:
        with open(_err_state_path(), encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return 0, 0.0
    try:
        if "," in raw:
            cnt, ts = raw.split(",", 1)
            return int(cnt), float(ts)
        return ERR_NOTIFY_MIN_STREAK, float(raw or 0)
    except ValueError:
        return 0, 0.0


def _save_err_state(fails: int, last_notify: float) -> None:
    try:
        with open(_err_state_path(), "w", encoding="utf-8") as f:
            f.write(f"{fails},{last_notify}")
    except OSError as e:
        print(f"  [warn] エラー状態ファイル書込失敗: {e}")


def _notify_error(detail: str) -> None:
    """サイクル失敗を記録し、ERR_NOTIFY_MIN_STREAK 回連続した時のみChatworkへ通知。

    単発の一時失敗（API瞬断等）では鳴らさない。認可失効(invalid_grant)は自然復旧が
    無いため回数に関係なく即通知。通知後の継続エラーは ERR_NOTIFY_INTERVAL_SEC 間隔に抑制。"""
    now = datetime.now(JST)
    fails, last_notify = _load_err_state()
    fails += 1
    fatal = "invalid_grant" in detail
    if not fatal and fails < ERR_NOTIFY_MIN_STREAK:
        _save_err_state(fails, last_notify)
        print(f"[error] 連続{fails}回目（{ERR_NOTIFY_MIN_STREAK}回連続で通知）: {detail[:200]}")
        return
    if now.timestamp() - last_notify < ERR_NOTIFY_INTERVAL_SEC:
        _save_err_state(fails, last_notify)
        print(f"[error] 通知は{ERR_NOTIFY_INTERVAL_SEC // 60}分間隔に抑制中"
              f"（前回 {now.timestamp() - last_notify:.0f}秒前・連続{fails}回目）: {detail[:200]}")
        return
    if "invalid_grant" in detail:
        # 認可失効はデータ取得の全停止＝最重要。[toall]+🚨で格上げ通知する。
        body = ("[toall]\n"
                f"[info][title]🚨🚨🚨【超重要】SP-API認可 失効（{_tenant_label()}） "
                f"{now:%Y-%m-%d %H:%M} 🚨🚨🚨[/title]\n"
                "🚨‼️🚨‼️🚨‼️🚨‼️🚨‼️🚨‼️🚨\n"
                "⛔ Amazon連携の認可が失効し、カート監視・データ取得がすべて停止しています。\n"
                "🔧 復旧には再連携が必要です（ワンクリック認可URLの再発行→セラーセントラルで認可）。\n"
                "[hr]📞 対応: このメッセージを見たら滝谷へ至急ご連絡ください。\n"
                f"[hr]🔁 解消まで{ERR_NOTIFY_INTERVAL_SEC // 60}分毎に再通知します。"
                "復旧すると✅を通知します。[/info]")
    else:
        body = (f"[info][title]🔴 カート監視エラー（{_tenant_label()}） {now:%Y-%m-%d %H:%M}[/title]\n"
                f"監視サイクルが{fails}回連続で失敗し、データ取得/シート更新が止まっています。\n"
                f"原因: {detail[:300]}\n"
                f"[hr]解消まで{ERR_NOTIFY_INTERVAL_SEC // 60}分毎に再通知します。"
                "復旧すると✅を通知します。[/info]")
    try:
        chatwork_post(body)
        _save_err_state(fails, now.timestamp())
        print(f"[error] Chatworkへエラー通知済み（連続{fails}回目）: {detail[:120]}")
    except BaseException as ce:   # 通知経路自体の死(トークン未設定等)もログには必ず残す
        _save_err_state(fails, last_notify)   # 通知に失敗しても連続回数は必ず記録する
        print(f"[error] エラー通知自体が失敗: {ce} / 元エラー: {detail[:200]}")


def _notify_recovery_if_needed() -> None:
    """直前までエラー通知していた場合のみ、復旧をChatworkへ通知して状態を消す。

    通知に至らなかった失敗痕跡（連続3回未満で復旧）は✅を出さず黙って消す
    （正常時は無通知の方針・2026-08-05）。"""
    path = _err_state_path()
    if not os.path.exists(path):
        return
    _, last_notify = _load_err_state()
    if last_notify <= 0:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    try:
        chatwork_post(f"[info][title]✅ カート監視 復旧（{_tenant_label()}） "
                      f"{datetime.now(JST):%Y-%m-%d %H:%M}[/title]\n"
                      "監視サイクルが正常に完了しました。エラーは解消しています。[/info]")
    except Exception as ce:
        print(f"  [warn] 復旧通知失敗（次の成功サイクルで再試行）: {ce}")
        return
    try:
        os.remove(path)
    except OSError:
        pass


# ── SP-API再認可期限リマインド ────────────────────────────────────────────
def notify_reauth_deadline(gtok: str, sheet_id: str, meta: dict) -> None:
    """再認可期限の高視認リマインド（節目日と期限当日以降は毎日・日1回）。

    期限は Actions variable REAUTH_DEADLINE_<TENANT>（YYYY-MM-DD）。未設定なら何もしない。
    無印フォールバックでの他社誤通知を避けるため _env を使わずテナント別のみ読む。
    通知失敗時は実施日を更新せず次サイクル(15分後)で再試行される（fail-soft）。"""
    raw = os.getenv(f"REAUTH_DEADLINE_{_tenant_label().upper()}", "")
    if not raw:
        return
    try:
        deadline = datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        print(f"  [warn] REAUTH_DEADLINE が YYYY-MM-DD 形式でない: {raw}")
        return
    today = datetime.now(JST).date()
    days = (deadline - today).days
    if days > 0 and days not in REAUTH_MILESTONES:
        return
    if meta.get("reauth_notice_last_date") == today.strftime("%Y-%m-%d"):
        return
    siren = "🚨‼️🚨‼️🚨‼️🚨‼️🚨‼️🚨‼️🚨"   # 視認性最優先の警報ライン（Chatworkはmd非描画のため絵文字で強調）
    if days > 0:
        title = f"🚨⏰【超重要】SP-API再認可期限 あと{days}日（{_tenant_label()}）⏰🚨"
        lead = (f"{siren}\n"
                f"🗓️ Amazon連携の認可期限: {deadline:%Y-%m-%d}\n"
                f"⏳ 残り {days}日\n"
                "⛔ 期限を過ぎるとカート監視・売上/在庫データ取得がすべて停止します。")
        steps = ("💡 対応（期限前ならボタンを押すだけ・約1分）\n"
                 "1️⃣ セラーセントラルにログイン\n"
                 "2️⃣ 設定 → アプリの管理（Manage Your Apps）を開く\n"
                 "3️⃣ 「KeyPath_AI_amazon」の「再承認」を押す\n"
                 "[hr]✅ 完了したら滝谷へ一報ください（期限の更新登録を行います）")
    elif days == 0:
        title = f"🚨🚨🚨【超重要】SP-API再認可期限 本日まで（{_tenant_label()}）🚨🚨🚨"
        lead = (f"{siren}\n"
                f"🗓️ Amazon連携の認可期限: 本日 {deadline:%Y-%m-%d}\n"
                "⛔ 本日中に再承認しないとカート監視・データ取得がすべて停止します。")
        steps = ("💡 対応（本日中・約1分）\n"
                 "1️⃣ セラーセントラルにログイン\n"
                 "2️⃣ 設定 → アプリの管理（Manage Your Apps）を開く\n"
                 "3️⃣ 「KeyPath_AI_amazon」の「再承認」を押す\n"
                 "[hr]✅ 完了したら滝谷へ一報ください")
    else:
        title = f"🚨🚨🚨【超重要】SP-API認可 期限超過{-days}日（{_tenant_label()}）🚨🚨🚨"
        lead = (f"{siren}\n"
                f"🗓️ Amazon連携の認可期限: {deadline:%Y-%m-%d}（⌛超過{-days}日）\n"
                "⛔ 認可が失効している場合、カート監視・データ取得が止まっています。")
        steps = "📞 対応: 再連携が必要な可能性があります。滝谷へ至急ご連絡ください"
    body = f"[toall]\n[info][title]{title}[/title]\n{lead}\n[hr]{steps}[/info]"
    try:
        chatwork_post(body)
        meta["reauth_notice_last_date"] = today.strftime("%Y-%m-%d")
        save_meta(gtok, sheet_id, meta)
        print(f"[reauth] 再認可期限リマインド通知（期限 {deadline} / 残{days}日）")
    except Exception as e:
        print(f"  [warn] 再認可リマインド通知失敗（次サイクルで再試行）: {e}")


# ── メイン ────────────────────────────────────────────────────────────────
def main() -> int:
    global _TENANT
    _TENANT = (sys.argv[1].strip() if len(sys.argv) > 1 else "")
    if _TENANT:
        print(f"=== カート監視 tenant={_TENANT} ===")
    own_seller = _env("OWN_SELLER_ID")
    sheet_id = _env("SHEET_ID")
    gtok = sheets_service_token()

    # ヘッダを設定
    sheet_update(gtok, sheet_id, "A1:K1", [HEADER])

    meta = load_meta(gtok, sheet_id)

    # SP-API再認可期限リマインド。トークン失効後も出せるよう lwa_token() より先に実施。
    notify_reauth_deadline(gtok, sheet_id, meta)

    token = lwa_token()

    # 0) 新商品の自動追記（日次1回）: 出品一覧とシートを突合し未登録のBUYABLE出品をA:E列へ追加。
    #    失敗時は実施日を更新せず次サイクルで再試行（fail-soft・当日中はリトライされ続ける）。
    today = datetime.now(JST).strftime("%Y-%m-%d")
    if meta.get("listing_sync_last_date") != today:
        try:
            if sync_new_listings(gtok, sheet_id, token, own_seller) >= 0:
                meta["listing_sync_last_date"] = today
                save_meta(gtok, sheet_id, meta)
        except Exception as e:
            print(f"  [warn] 新商品同期失敗（次サイクルで再試行）: {e}")

    # 1) シート読込（ヘッダ除く）＝監視対象の正本。新規ASINは日次自動追記または手動行追加。
    rows = sheet_get(gtok, sheet_id, "A2:K")

    # 2) 各ASINを判定
    inv = get_fba_inventory(token)   # {asin: 出荷可能数}（原因＝在庫切れ判定用）
    cat = get_catalog_all(token, [r[COL_ASIN].strip() for r in rows
                                  if r and len(r) > COL_ASIN and r[COL_ASIN].strip()])

    # ベストセラータグ推定用: カテゴリ1位ASINのノード商品数（親ASIN単位）を計測。
    # 計測は重い(数十コール/ノード)ため NODE_TAB に永続化し NODE_TTL_HOURS ごとに更新。
    node_sizes = load_node_sizes(gtok, sheet_id)
    need = {c["rank1_node"]: c.get("rank1_title") or ""
            for c in cat.values() if c.get("rank1_node")}
    now_dt = datetime.now(JST)
    sizes_dirty = False
    for nid, title in need.items():
        ent = node_sizes.get(nid)
        if ent and ent.get("size") is not None and ent.get("ts") and \
                (now_dt - ent["ts"]) < timedelta(hours=NODE_TTL_HOURS):
            continue
        size = estimate_node_size(token, nid, title or (ent or {}).get("title", ""))
        if size is None:
            print(f"  [warn] node {nid}({title}): 商品数計測失敗→前回値を継続使用")
            continue
        node_sizes[nid] = {"title": title or (ent or {}).get("title", ""),
                           "size": size, "ts": now_dt}
        sizes_dirty = True
        print(f"  [node] {nid}({title}): 推定商品数={size}")
    if sizes_dirty:
        save_node_sizes(gtok, sheet_id, node_sizes)

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    writes = []          # 各行の [F状態, G時刻, H相乗りID, Iベストセラー, Jバリ親, Kブラウズ]
    problems = []        # 通知ブロック（ASIN単位）
    n_cart = n_search = n_hijack = n_best = n_var = n_browse = 0
    for r in rows:
        r = (r + [""] * 11)[:11]
        asin = r[COL_ASIN].strip()
        if not asin:
            writes.append([r[COL_STATUS], r[COL_CHECK], r[COL_SELLERS], r[COL_BEST],
                           r[COL_PARENT], r[COL_BROWSE]])
            continue
        sku, name = r[COL_SKU].strip(), r[COL_NAME].strip()
        muted = str(r[COL_MUTE]).strip().upper() in ("TRUE", "1", "YES", "✓")
        prev_sellers = {s.strip() for s in str(r[COL_SELLERS]).split(",") if s.strip()}
        prev_best, prev_parent = r[COL_BEST].strip(), r[COL_PARENT].strip()
        prev_browse = r[COL_BROWSE].strip()
        if muted:
            writes.append([ST_MUTED, now, r[COL_SELLERS], r[COL_BEST], r[COL_PARENT], r[COL_BROWSE]])
            continue
        payload = get_item_offers(token, asin)
        if payload is None:
            writes.append([r[COL_STATUS] or "判定不可", now, r[COL_SELLERS], r[COL_BEST],
                           r[COL_PARENT], r[COL_BROWSE]])
            time.sleep(0.6)
            continue
        offers = payload.get("Offers", []) or []
        own = [o for o in offers if o.get("SellerId") == own_seller]
        others = sorted({o.get("SellerId") for o in offers
                         if o.get("SellerId") and o.get("SellerId") != own_seller})
        bb = next(iter(payload.get("Summary", {}).get("BuyBoxPrices") or []), {})
        own_p = _price(own[0]) if own else None
        bb_p = _price(bb)
        comp = [p for p in (_price(o) for o in offers if o.get("SellerId") != own_seller) if p is not None]
        comp_min = min(comp) if comp else None
        stock = inv.get(asin)
        cart_st = classify(payload, own_seller)

        # 検索対象外（購入可だが検索から抑制）= getListingsItem。
        search_off, li = False, None
        if sku:
            li = get_listings_item(token, own_seller, sku)
            if li is None:
                print(f"  [warn] {sku}: getListingsItem失敗→検索対象外判定スキップ")
            elif li.get("buyable") and not li.get("searchable"):
                search_off = True

        issues = []
        if cart_st in ("カート喪失", "自社出品消失"):
            issues.append(cart_st)
        if search_off:
            issues.append("検索対象外")

        # ベストセラー / バリエーション / ブラウズノード（Catalog）。
        # 初回(prev空)はbaseline記録のみ＝通知しない。
        # 列I: TRUE=タグ推定(1位×ノード商品数>=100 or 表示グループ1位) /
        #      RANK1=カテゴリ1位だがノード商品数<100でタグ対象外 / FALSE=1位でない。
        c = cat.get(asin)
        best_event = best_note = best_sev = None
        var_event = browse_event = None
        if c is None:   # Catalog未取得 → I/J/K保持・イベント判定スキップ（誤検知防止）
            best_str, parents_now, browse_str = prev_best, (prev_parent or ""), prev_browse
        else:
            parents_now = ",".join(c.get("parents") or []) or "なし"
            prev_b = prev_best.strip().upper()
            nid = c.get("rank1_node")
            nsize = (node_sizes.get(nid) or {}).get("size") if nid else None
            if c.get("display_best"):
                best_str = "TRUE"
            elif nid and nsize is None:
                # ノード商品数が未計測/計測失敗 → 判定保留（前回値保持・イベント無し）
                best_str = prev_b if prev_b in ("TRUE", "FALSE", "RANK1") else ""
            elif nid and nsize >= BSR_MIN_NODE_ITEMS:
                best_str = "TRUE"
            elif nid:
                best_str = "RANK1"
            else:
                best_str = "FALSE"
            if (prev_b in ("TRUE", "FALSE", "RANK1")
                    and best_str in ("TRUE", "FALSE", "RANK1") and best_str != prev_b):
                if best_str == "TRUE":
                    best_event, best_sev = "ベストセラータグ点灯(推定)", "🏅"
                    if c.get("display_best") and not (nid and nsize is not None
                                                      and nsize >= BSR_MIN_NODE_ITEMS):
                        best_note = f"表示グループ「{c.get('cat') or '—'}」で1位"
                    else:
                        best_note = (f"カテゴリ「{c.get('rank1_title') or '—'}」1位・"
                                     f"ノード商品数{nsize}件（{BSR_MIN_NODE_ITEMS}件以上でタグ対象）")
                elif best_str == "RANK1" and prev_b == "TRUE":
                    best_event, best_sev = "ベストセラータグ対象外へ(1位維持)", "🔻"
                    best_note = (f"カテゴリ「{c.get('rank1_title') or '—'}」1位は維持も、"
                                 f"ノード商品数{nsize}件<{BSR_MIN_NODE_ITEMS}のためタグ非付与(推定)")
                elif best_str == "RANK1":
                    best_event, best_sev = "カテゴリ1位到達(タグ対象外)", "🟡"
                    best_note = (f"カテゴリ「{c.get('rank1_title') or '—'}」で1位。ただし"
                                 f"ノード商品数{nsize}件<{BSR_MIN_NODE_ITEMS}のためタグは"
                                 f"付かない(推定)・あと{BSR_MIN_NODE_ITEMS - nsize}件で対象")
                elif prev_b == "RANK1":
                    best_event, best_sev = "カテゴリ1位から陥落", "🔻"
                    best_note = f"カテゴリ1位から陥落（現順位 {c.get('rank') or '—'}）"
                else:
                    best_event, best_sev = "ベストセラー消失", "🔻"
                    best_note = f"ベストセラー圏から外れました（現順位 {c.get('rank') or '—'}）"
            if prev_parent and prev_parent != "なし":
                if parents_now == "なし":
                    var_event = "バリエーション解体"
                elif parents_now != prev_parent:
                    var_event = "バリエーション構成変化"
            # ブラウズノード＝classificationId主軸で比較（同名別ノード移動を検知し
            # 表示名リネームの誤検知を回避）、表示は "ID｜名称"。取得空は保持。
            bid = str(c.get("browse_id") or "").strip()
            bname = str(c.get("browse_name") or "").strip()
            if not (bid or bname):
                browse_str = prev_browse
            else:
                browse_str = f"{bid}｜{bname}" if bid else bname
                if prev_browse and prev_browse != "なし" and prev_browse != browse_str:
                    prev_id = prev_browse.split("｜", 1)[0].strip()
                    if bid and prev_id:        # 双方IDあり＝IDで判定（表示名揺れを無視）
                        if bid != prev_id:
                            browse_event = "ブラウズノード変更"
                    else:                       # ID欠落（旧baseline等）＝文字列差で判定
                        browse_event = "ブラウズノード変更"

        status_parts = list(issues) + (["相乗りあり"] if others else [])
        status_str = "／".join(status_parts) if status_parts else ST_OK
        writes.append([status_str, now, ",".join(others), best_str, parents_now, browse_str])

        new_sellers = [s for s in others if s not in prev_sellers]
        if not (issues or new_sellers or best_event or var_event or browse_event):
            time.sleep(0.6)
            continue

        # 通知ブロック（ASIN単位）
        if any(i in ("カート喪失", "自社出品消失") for i in issues):
            sev = "🔴"
        elif var_event or browse_event or best_sev == "🔻":
            sev = "🔻"
        elif best_sev:
            sev = best_sev
        else:
            sev = "🟠"
        heads = list(issues) + [e for e in (best_event, var_event, browse_event) if e]
        block = [f"{sev} {'／'.join(heads) if heads else '他社相乗り'}",
                 f"  {name}（{sku}）" if sku else f"  {name}"]
        if "自社出品消失" in issues:
            if stock == 0:
                block.append("  原因: 在庫切れ（FBA在庫0）")
            elif isinstance(stock, int) and stock > 0:
                block.append(f"  原因: 出品停止/カート出せず（在庫{stock}あり＝出品状態を要確認）")
            else:
                block.append("  原因: 出品停止 or 在庫切れ（在庫不明）")
        if "カート喪失" in issues:
            if own_p is not None and bb_p is not None and own_p > bb_p:
                block.append(f"  原因: 価格負け（自社¥{own_p:,.0f} > カート¥{bb_p:,.0f}）")
            elif not others:
                block.append("  原因: Buy Box抑制（高値等で誰もカート取得せず）")
            else:
                block.append("  原因: 競合がカート取得")
        if "検索対象外" in issues:
            errnote = "出品ERRORあり" if (li and li.get("error")) else "出品状態/規約を要確認"
            block.append(f"  原因: 検索から抑制の可能性（購入は可・{errnote}）")
        if var_event:
            block.append(f"  ※バリエーション: 親 {prev_parent} → {parents_now}（レビュー/順位の統合に影響）")
        if best_note:
            block.append(f"  ※{best_note}")
        if browse_event:
            block.append(f"  ※ブラウズノード: {prev_browse} → {browse_str}"
                         "（カテゴリ移動＝検索面/ベストセラー基準カテゴリ/サジェストに影響）")
        ctx = []
        if own_p is not None:
            ctx.append(f"自社¥{own_p:,.0f}")
        if bb_p is not None:
            ctx.append(f"カート¥{bb_p:,.0f}")
        if comp_min is not None:
            ctx.append(f"競合最安¥{comp_min:,.0f}")
        if stock is not None:
            ctx.append(f"在庫{stock}")
        if ctx:
            block.append("  " + " / ".join(ctx))
        block.append(f"  https://www.amazon.co.jp/dp/{asin}")
        for sid in new_sellers:
            block.append(f"  ↳ 新規相乗りセラー {sid}\n    ストア: {STOREFRONT.format(sid)}")
        if others and len(others) > len(new_sellers):
            block.append(f"  （相乗り合計 {len(others)}社）")
        problems.append("\n".join(block))
        n_cart += 1 if cart_st in ("カート喪失", "自社出品消失") else 0
        n_search += 1 if search_off else 0
        n_hijack += 1 if new_sellers else 0
        n_best += 1 if best_event else 0
        n_var += 1 if var_event else 0
        n_browse += 1 if browse_event else 0
        time.sleep(0.6)

    # 3) 自動列をシートへ書き戻し（F:K＝状態/時刻/相乗りID/ベストセラー/バリ親/ブラウズ）
    if writes:
        sheet_update(gtok, sheet_id, f"F2:K{1 + len(writes)}", writes)

    # 4) Chatwork へ集約通知
    if problems:
        body = (f"[info][title]【緊急】Amazonカート/出品アラート {now}[/title]\n"
                + "\n".join(problems)
                + "\n[hr]※セラー名はストアURLをクリックで確認。"
                  "カート落ち/検索対象外は解消かミュート(管理シート「アラート除外」TRUE)まで毎回通知。"
                  "ベストセラー/バリエーション/ブラウズノードは変化時のみ。"
                  "ベストセラータグ=カテゴリ1位×ノード商品数100件以上の推定。[/info]")
        chatwork_post(body)
        print(f"[alert] cart={n_cart} search={n_search} hijack={n_hijack} "
              f"best={n_best} var={n_var} browse={n_browse} ASIN={len(problems)} 通知")
    else:
        print("[ok] 新規異常なし")

    # 毎朝9時台の「✅正常稼働」通知は2026-08-05に廃止（正常時は無通知の方針）。
    # Actionsごと停止した場合の検知は CF Worker cart-monitor-dispatcher のwatchdogが担う。
    return 0


def run() -> int:
    """main() を実行し、失敗時は Chatwork へ即時エラー通知（継続中は60分間隔に抑制）。

    成功時は、直前までエラー通知していた場合のみ✅復旧を通知する。
    _env 未設定の sys.exit（SystemExit）も「データ取得できない」状態として通知対象。"""
    global _TENANT
    _TENANT = (sys.argv[1].strip() if len(sys.argv) > 1 else "")
    try:
        rc = main()
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        if isinstance(e, SystemExit) and (e.code is None or e.code == 0):
            _notify_recovery_if_needed()
            return 0
        traceback.print_exc()
        _notify_error(str(e) or type(e).__name__)
        return 1
    _notify_recovery_if_needed()
    return rc


def handler(request=None):
    """Cloud Functions(2nd gen, HTTP) / Cloud Scheduler 用エントリ。run()を実行。"""
    rc = run()
    return (f"done rc={rc}", 200)


if __name__ == "__main__":
    raise SystemExit(run())
