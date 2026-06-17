"""Amazon カート/出品 緊急監視ポーラー（悩み解決ラボ）。

GitHub Actions で15分毎にクラウド実行（PCオフでも動く）。SP-API getItemOffers で
各ASINの Buy Box 状態を取得し、以下を検知して新規発生(onset)時のみ Chatwork に通知する:
  - カート喪失      : 自社オファーはあるが Buy Box を取れていない
  - 自社出品消失/停止: 自社オファーがオファー一覧に存在しない（出品停止・カート出せず）
  - 他社相乗り      : 自社以外のセラーがオファーに存在
状態は制御スプレッドシート（ASIN/アラート除外/最終ステータス…）を読み書きして保持する。
「アラート除外」=TRUE のASINは通知しない（ミュート）。
カート喪失/自社出品消失は未解決の間、ミュートまで毎回(15分毎)通知し続ける。相乗りは新規セラー出現時のみ通知。

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
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

JST = timezone(timedelta(hours=9))
# 監視ASIN・SKU・商品名・ミュート・状態の正本＝非公開の管理スプレッドシート（SHEET_ID）。
# リポジトリには商品リストを置かない（public化のため）。新規ASINはシートに行追加で対応。

# 列: A=ASIN B=商品名(簡易) C=SKU D=アラート除外 E=メモ F=最終ステータス G=最終チェック
#     H=相乗りセラーID I=ベストセラー J=バリエーション親（F以降は自動更新）
(COL_ASIN, COL_NAME, COL_SKU, COL_MUTE, COL_MEMO, COL_STATUS, COL_CHECK,
 COL_SELLERS, COL_BEST, COL_PARENT) = range(10)
HEADER = ["ASIN", "商品名", "SKU", "アラート除外", "メモ",
          "最終ステータス(自動)", "最終チェック(自動)", "相乗りセラーID(自動)",
          "ベストセラー(自動)", "バリエーション親(自動)"]
STOREFRONT = "https://www.amazon.co.jp/sp?seller={}"  # セラー名はSP-APIで取れずURLで代替

# ステータス定義（severityで通知要否を判定）
ST_OK = "正常"
ST_MUTED = "ミュート中"
BAD = {
    "カート喪失": "🔴 Buy Box を他社に奪われています（カート喪失）",
    "自社出品消失": "🔴 自社オファーが消えています（出品停止・カート出せず）",
    "他社相乗り": "🟠 他社セラーが相乗りしています",
}


def _env(name: str, required: bool = True) -> str:
    v = os.getenv(name, "")
    if required and not v:
        sys.exit(f"[FATAL] 環境変数 {name} が未設定")
    return v


# ── SP-API ───────────────────────────────────────────────────────────────
def lwa_token() -> str:
    r = requests.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": _env("SPAPI_REFRESH_TOKEN"),
        "client_id": _env("SPAPI_LWA_CLIENT_ID"),
        "client_secret": _env("SPAPI_LWA_CLIENT_SECRET"),
    }, timeout=30)
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
    """ASIN群の {asin: {best:bool, rank:int|None, cat:str, parents:[..]}} を batch取得。
    best=最小カテゴリで rank==1（ベストセラー推定）。parents=VARIATION親ASIN。"""
    host = _env("SPAPI_HOST")
    mp = _env("SPAPI_MARKETPLACE_ID")
    url = f"https://{host}/catalog/2022-04-01/items"
    out: dict = {}
    uniq = sorted({a for a in asins if a})
    for i in range(0, len(uniq), 20):
        chunk = uniq[i:i + 20]
        params = {"identifiers": ",".join(chunk), "identifiersType": "ASIN", "marketplaceIds": mp,
                  "includedData": "salesRanks,relationships", "pageSize": 20}
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
            for blk in it.get("salesRanks", []) or []:
                for rr in (blk.get("classificationRanks", []) or []) + (blk.get("displayGroupRanks", []) or []):
                    rk = rr.get("rank")
                    if isinstance(rk, int):
                        ranks.append(rk)
                        if rk == 1 and not best_cat:
                            best_cat = rr.get("title")
            parents = []
            for blk in it.get("relationships", []) or []:
                for rel in blk.get("relationships", []) or []:
                    if rel.get("type") == "VARIATION":
                        parents += rel.get("parentAsins") or []
            if a:
                out[a] = {"best": 1 in ranks, "rank": min(ranks) if ranks else None,
                          "cat": best_cat, "parents": sorted(set(parents))}
        time.sleep(0.3)
    return out


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


# ── Chatwork ──────────────────────────────────────────────────────────────
def chatwork_post(message: str) -> None:
    tok = _env("CHATWORK_TOKEN")
    room = _env("CHATWORK_ROOM_ID")
    r = requests.post(f"https://api.chatwork.com/v2/rooms/{room}/messages",
                      headers={"X-ChatWorkToken": tok},
                      data={"body": message, "self_unread": "1"}, timeout=30)
    r.raise_for_status()


# ── メイン ────────────────────────────────────────────────────────────────
def main() -> int:
    own_seller = _env("OWN_SELLER_ID")
    sheet_id = _env("SHEET_ID")
    gtok = sheets_service_token()

    # ヘッダを設定
    sheet_update(gtok, sheet_id, "A1:J1", [HEADER])

    # 1) シート読込（ヘッダ除く）＝監視対象の正本。新規ASINはシートに手動で行追加する。
    rows = sheet_get(gtok, sheet_id, "A2:J")

    # 2) 各ASINを判定
    token = lwa_token()
    inv = get_fba_inventory(token)   # {asin: 出荷可能数}（原因＝在庫切れ判定用）
    cat = get_catalog_all(token, [r[COL_ASIN].strip() for r in rows
                                  if r and len(r) > COL_ASIN and r[COL_ASIN].strip()])
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    writes = []          # 各行の [F状態, G時刻, H相乗りID, Iベストセラー, Jバリ親]
    problems = []        # 通知ブロック（ASIN単位）
    n_cart = n_search = n_hijack = n_best = n_var = 0
    for r in rows:
        r = (r + [""] * 10)[:10]
        asin = r[COL_ASIN].strip()
        if not asin:
            writes.append([r[COL_STATUS], r[COL_CHECK], r[COL_SELLERS], r[COL_BEST], r[COL_PARENT]])
            continue
        sku, name = r[COL_SKU].strip(), r[COL_NAME].strip()
        muted = str(r[COL_MUTE]).strip().upper() in ("TRUE", "1", "YES", "✓")
        prev_sellers = {s.strip() for s in str(r[COL_SELLERS]).split(",") if s.strip()}
        prev_best, prev_parent = r[COL_BEST].strip(), r[COL_PARENT].strip()
        if muted:
            writes.append([ST_MUTED, now, r[COL_SELLERS], r[COL_BEST], r[COL_PARENT]])
            continue
        payload = get_item_offers(token, asin)
        if payload is None:
            writes.append([r[COL_STATUS] or "判定不可", now, r[COL_SELLERS], r[COL_BEST], r[COL_PARENT]])
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

        # ベストセラー / バリエーション（Catalog）。初回(prev空)はbaseline記録のみ＝通知しない。
        c = cat.get(asin)
        best_event = var_event = None
        if c is None:   # Catalog未取得 → I/J保持・イベント判定スキップ（解体の誤検知防止）
            best_str, parents_now = prev_best, (prev_parent or "")
        else:
            best_now = bool(c.get("best"))
            parents_now = ",".join(c.get("parents") or []) or "なし"
            best_str = "TRUE" if best_now else "FALSE"
            prev_b = prev_best.strip().upper()
            if prev_b in ("TRUE", "FALSE"):
                if best_now and prev_b == "FALSE":
                    best_event = "ベストセラー点灯"
                elif not best_now and prev_b == "TRUE":
                    best_event = "ベストセラー消失"
            if prev_parent and prev_parent != "なし":
                if parents_now == "なし":
                    var_event = "バリエーション解体"
                elif parents_now != prev_parent:
                    var_event = "バリエーション構成変化"

        status_parts = list(issues) + (["相乗りあり"] if others else [])
        status_str = "／".join(status_parts) if status_parts else ST_OK
        writes.append([status_str, now, ",".join(others), best_str, parents_now])

        new_sellers = [s for s in others if s not in prev_sellers]
        if not (issues or new_sellers or best_event or var_event):
            time.sleep(0.6)
            continue

        # 通知ブロック（ASIN単位）
        if any(i in ("カート喪失", "自社出品消失") for i in issues):
            sev = "🔴"
        elif var_event or best_event == "ベストセラー消失":
            sev = "🔻"
        elif best_event == "ベストセラー点灯":
            sev = "🏅"
        else:
            sev = "🟠"
        heads = list(issues) + [e for e in (best_event, var_event) if e]
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
        if best_event == "ベストセラー点灯":
            block.append(f"  ※カテゴリ「{c.get('cat') or '—'}」で1位（ベストセラー圏）")
        elif best_event == "ベストセラー消失":
            block.append(f"  ※ベストセラー圏から外れました（現順位 {c.get('rank') or '—'}）")
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
        time.sleep(0.6)

    # 3) 自動列をシートへ書き戻し（F:J＝状態/時刻/相乗りID/ベストセラー/バリ親）
    if writes:
        sheet_update(gtok, sheet_id, f"F2:J{1 + len(writes)}", writes)

    # 4) Chatwork へ集約通知
    if problems:
        body = (f"[info][title]【緊急】Amazonカート/出品アラート {now}[/title]\n"
                + "\n".join(problems)
                + "\n[hr]※セラー名はストアURLをクリックで確認。"
                  "カート落ち/検索対象外は解消かミュート(管理シート「アラート除外」TRUE)まで毎回通知。"
                  "ベストセラー/バリエーションは変化時のみ。[/info]")
        chatwork_post(body)
        print(f"[alert] cart={n_cart} search={n_search} hijack={n_hijack} "
              f"best={n_best} var={n_var} ASIN={len(problems)} 通知")
    else:
        print("[ok] 新規異常なし")
    return 0


def handler(request=None):
    """Cloud Functions(2nd gen, HTTP) / Cloud Scheduler 用エントリ。main()を実行。"""
    rc = main()
    return (f"done rc={rc}", 200)


if __name__ == "__main__":
    raise SystemExit(main())
