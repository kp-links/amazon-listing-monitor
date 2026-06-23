"""悩み解決ラボ：直近30日の販売数量を ASIN 単位で集計し、管理シートのC列へ書き込む。

GitHub Actions で毎朝1回クラウド実行（PCオフでも動く）。SP-API Reports API の
GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL（買い手PIIを含まない GENERAL 版）で
直近30日の注文明細を取得し、order-status が Cancelled 以外の quantity を ASIN 別に合算する。

突合キーは ASIN（シートA列）。シートB列の SKU は楽天SKU/JAN が混在し Amazon SKU と
一致しないため使わない。1 ASIN = 1 行を前提に、各行の ASIN の30日数量をC列へ書く。
C1 には更新日時を記載する。

集計定義（テープス置換のため明示）:
  - 対象期間 : 実行時刻(JST)から遡って30日間（注文日ベース）
  - 数量     : quantity 列の合算（注文ユニット数）
  - 除外     : order-status == "Cancelled" の行（キャンセルは販売に数えない）
  - 含む     : Pending / Unshipped / PartiallyShipped / Shipped（=受注ベース）
  - 返品調整 : なし（All Orders レポートは返金/返品を反映しない受注ベースのため）
  - FBA/FBM  : 両方を含む（sales-channel でのフィルタはしない）

環境変数（GitHub Actions secrets / env から注入）:
  SPAPI_REFRESH_TOKEN / SPAPI_LWA_CLIENT_ID / SPAPI_LWA_CLIENT_SECRET
  SPAPI_MARKETPLACE_ID (例 A1VC38T7YXB528) / SPAPI_HOST (例 sellingpartnerapi-fe.amazon.com)
  GOOGLE_SA_JSON (サービスアカウントJSON文字列)
  SALES_SHEET_ID  (対象スプレッドシートID＝機密。secret で渡す)
  SALES_SHEET_GID (対象シートの gid。例 1707721264)
"""
from __future__ import annotations

import csv
import gzip
import io
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
ORDERS_REPORT_TYPE = "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL"
WINDOW_DAYS = 30
THROTTLE_CODES = {429, 503}


def _env(name: str, required: bool = True) -> str:
    v = os.getenv(name, "")
    if required and not v:
        sys.exit(f"[FATAL] 環境変数 {name} が未設定")
    return v


# ── SP-API 認証 ────────────────────────────────────────────────────────────
def lwa_token() -> str:
    r = requests.post("https://api.amazon.com/auth/o2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": _env("SPAPI_REFRESH_TOKEN"),
        "client_id": _env("SPAPI_LWA_CLIENT_ID"),
        "client_secret": _env("SPAPI_LWA_CLIENT_SECRET"),
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _sp_request(method: str, path: str, token: str, *,
                body: dict | None = None) -> dict:
    """SP-API へのJSONリクエスト。429/503は指数バックオフで数回リトライ。"""
    host = _env("SPAPI_HOST")
    url = f"https://{host}{path}"
    headers = {"x-amz-access-token": token, "Accept": "application/json",
               "User-Agent": "amazon-sales30d/1.0 (Language=Python)"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(6):
        resp = requests.request(method, url, headers=headers,
                                json=body if body is not None else None, timeout=60)
        if resp.status_code < 300:
            return resp.json() if resp.text else {}
        if resp.status_code in THROTTLE_CODES and attempt < 5:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 2 * (2 ** attempt)
            except (TypeError, ValueError):
                wait = 2 * (2 ** attempt)
            time.sleep(wait)
            continue
        raise RuntimeError(f"SP-API {method} {path} 失敗 HTTP {resp.status_code}: "
                           f"{resp.text[:300]}")
    raise RuntimeError(f"SP-API {method} {path} がリトライ上限")


# ── レポート取得（create → poll → document → download → 展開 → デコード）─────
def fetch_orders_tsv(token: str, start_iso: str, end_iso: str) -> str:
    mp = _env("SPAPI_MARKETPLACE_ID")
    created = _sp_request("POST", "/reports/2021-06-30/reports", token, body={
        "reportType": ORDERS_REPORT_TYPE,
        "marketplaceIds": [mp],
        "dataStartTime": start_iso,
        "dataEndTime": end_iso,
    })
    report_id = created.get("reportId")
    if not report_id:
        raise RuntimeError(f"reportId が応答に無い: {created}")

    # ポーリング（IN_QUEUE/IN_PROGRESS を待つ。最大15分）
    deadline = time.monotonic() + 900
    wait, doc_id = 5, None
    while True:
        st = _sp_request("GET", f"/reports/2021-06-30/reports/{report_id}", token)
        status = st.get("processingStatus")
        if status == "DONE":
            doc_id = st.get("reportDocumentId")
            if not doc_id:
                raise RuntimeError(f"DONEだが reportDocumentId 欠落: {st}")
            break
        if status == "CANCELLED":
            # Amazon自動キャンセル。アクティブ出店で直近30日に注文ゼロは考えにくく、
            # 空扱いでC列を全ゼロ上書きすると実データ消失になる→失敗で中止（書込まない）。
            raise RuntimeError(
                "レポートが CANCELLED（生成失敗 or 期間内データ無し）→ "
                "C列を破壊しないため書込まず中止")
        if status not in ("IN_QUEUE", "IN_PROGRESS"):
            raise RuntimeError(f"レポート生成が異常終了/不明status（{status}）: {st}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"レポート生成がtimeout（最終status={status}）")
        time.sleep(wait)
        wait = min(wait * 2, 60)

    meta = _sp_request("GET", f"/reports/2021-06-30/documents/{doc_id}", token)
    url = meta.get("url")
    if not url:
        raise RuntimeError(f"ドキュメントURLが応答に無い: {meta}")
    blob = requests.get(url, timeout=180)  # 署名済S3 URL。認証ヘッダ不要
    blob.raise_for_status()
    payload = blob.content
    if meta.get("compressionAlgorithm") == "GZIP":
        payload = gzip.decompress(payload)
    elif meta.get("compressionAlgorithm"):
        raise RuntimeError(f"未対応の圧縮方式: {meta.get('compressionAlgorithm')}")
    # 日本MPのフラットファイルは Shift_JIS のことが多い
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return payload.decode(enc)
        except UnicodeDecodeError:
            continue
    return payload.decode("cp932", errors="replace")


def sum_quantity_by_asin(tsv_text: str) -> dict[str, int]:
    """注文TSV → {asin: 30日数量}。Cancelled を除外して quantity を合算。"""
    if not tsv_text.strip():
        return {}
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    required = {"asin", "quantity", "order-status"}
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"注文レポートに必須列が無い: {sorted(missing)}"
                         "（レポート種別 / Amazon仕様変更を確認）")
    totals: dict[str, int] = {}
    for row in reader:
        if (row.get("order-status") or "").strip() == "Cancelled":
            continue
        asin = (row.get("asin") or "").strip()
        if not asin:
            continue
        q = (row.get("quantity") or "").replace(",", "").strip()
        try:
            qty = int(float(q)) if q else 0
        except ValueError:
            # 仕様変更/壊れ行の見逃し防止に警告（過少集計の沈黙を避ける）
            print(f"[warn] quantity 解析不可 asin={asin} 値='{q}' → 0 扱い")
            qty = 0
        totals[asin] = totals.get(asin, 0) + qty
    return totals


# ── Google Sheets ─────────────────────────────────────────────────────────
def sheets_token() -> str:
    info = json.loads(_env("GOOGLE_SA_JSON"))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(GoogleRequest())
    return creds.token


def _a1(title: str, cells: str) -> str:
    """A1 表記。タイトルを ' で引用しエスケープ（空白/日本語/記号入りタブ名対策）。"""
    return "'" + title.replace("'", "''") + "'!" + cells


def _sheets_call(method: str, token: str, sheet_id: str, suffix: str, *,
                 params: dict | None = None, body: dict | None = None) -> dict:
    """Sheets API 呼び出し。429/5xx を指数バックオフでリトライ。suffix はパス末尾。"""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}{suffix}"
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(6):
        r = requests.request(method, url, headers=headers, params=params or {},
                             json=body, timeout=30)
        if r.status_code < 300:
            return r.json() if r.text else {}
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 5:
            time.sleep(2 * (2 ** attempt))
            continue
        r.raise_for_status()
    raise RuntimeError(f"Sheets API {method} がリトライ上限: {suffix}")


def resolve_sheet_title(token: str, sheet_id: str, gid: int) -> str:
    """gid から対象シートのタイトルを解決（values API は A1 表記＝タイトルが要る）。"""
    meta = _sheets_call("GET", token, sheet_id, "",
                        params={"fields": "sheets.properties"})
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("sheetId") == gid:
            return props["title"]
    raise RuntimeError(f"gid={gid} のシートが見つからない（SALES_SHEET_GID を確認）")


def sheet_read(token: str, sheet_id: str, rng: str) -> list[list]:
    suffix = "/values/" + urllib.parse.quote(rng, safe="")
    return _sheets_call("GET", token, sheet_id, suffix).get("values", [])


def sheet_update(token: str, sheet_id: str, rng: str, values: list[list]) -> None:
    suffix = "/values/" + urllib.parse.quote(rng, safe="")
    _sheets_call("PUT", token, sheet_id, suffix,
                 params={"valueInputOption": "RAW"}, body={"values": values})


def main() -> int:
    sheet_id = _env("SALES_SHEET_ID")
    gid_raw = _env("SALES_SHEET_GID")
    try:
        gid = int(gid_raw)
    except ValueError:
        sys.exit(f"[FATAL] SALES_SHEET_GID が整数でない: '{gid_raw}'")

    # 1) SP-API：直近30日の注文を取得し ASIN 別数量を集計
    #    窓は JST 暦日基準（本日含む直近30日 = [本日-29日 00:00, 現在]）。
    #    cron が遅延しても開始境界が深夜0時で固定され、境界日の出入りを抑える。
    now = datetime.now(JST)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_iso = (today0 - timedelta(days=WINDOW_DAYS - 1)).isoformat(timespec="seconds")
    end_iso = now.isoformat(timespec="seconds")
    sp_token = lwa_token()
    print(f"[info] 注文レポート取得 {start_iso} 〜 {end_iso}")
    tsv = fetch_orders_tsv(sp_token, start_iso, end_iso)
    totals = sum_quantity_by_asin(tsv)
    print(f"[info] 集計対象ASIN数={len(totals)} 総数量={sum(totals.values())}")
    if not totals:
        # 直近30日で1件も取れない＝取得異常の可能性。C列の既存値を消さず中止。
        sys.exit("[FATAL] 集計結果が空（取得異常の可能性）→ C列を保全し書込まず中止")

    # 2) Sheets：A列ASINを正本に、各行のC列へ数量を書く
    gtok = sheets_token()
    title = resolve_sheet_title(gtok, sheet_id, gid)
    rows = sheet_read(gtok, sheet_id, _a1(title, "A1:B"))
    if not rows:
        sys.exit("[FATAL] 対象シートのA:B列が空（gid/共有設定を確認）")

    # ヘッダ検証：誤ったシートへC列を破壊上書きしないための硬ゲート。
    # A1 に ASIN（例 "(子) ASIN"）、B1 に SKU を期待する。
    a1 = (rows[0][0] if rows[0] else "").strip()
    b1 = (rows[0][1] if len(rows[0]) > 1 else "").strip()
    if "ASIN" not in a1.upper() or "SKU" not in b1.upper():
        sys.exit(f"[FATAL] ヘッダ不一致 A1='{a1}' B1='{b1}'（ASIN/SKU を期待）。"
                 "対象シート(gid)を誤っている可能性→中止")

    # 3) C列の値を行順に組み立て（A列にASINがある行のみ数量、無い行は空のまま温存）
    col_c: list[list] = []
    matched = 0
    for r in rows[1:]:                      # 2行目以降
        asin = (r[0].strip() if r and len(r) > 0 and r[0] else "")
        if asin and asin in totals:
            col_c.append([totals[asin]])
            matched += 1
        elif asin:
            col_c.append([0])               # 出品はあるが30日販売ゼロ
        else:
            col_c.append([""])              # ASIN無し行は触らない

    stamp = f"直近30日販売数(更新 {now.strftime('%Y/%m/%d %H:%M')} JST)"
    sheet_update(gtok, sheet_id, _a1(title, "C1"), [[stamp]])
    if col_c:
        last_row = 1 + len(col_c)           # C2..C{last}
        sheet_update(gtok, sheet_id, _a1(title, f"C2:C{last_row}"), col_c)
    print(f"[ok] 書込み完了 シート='{title}' 行数={len(col_c)} 一致ASIN={matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
