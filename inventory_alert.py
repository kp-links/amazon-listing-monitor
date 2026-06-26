# -*- coding: utf-8 -*-
"""在庫トレンド＆発注アラート（クラウド常駐 / GitHub Actions）。

単一の30日移動平均では直近の需要急変を拾えず欠品する——という課題に対し、
7日 / 30日 の日販から「加速度（7日÷30日）」を見て、加速SKUは速い側のペースで
在庫切れを再評価する。発注は一律リードタイム135日＋安全在庫で発注点(ROP)を引く。

データソース（SKU突合）:
  - フォーマットタブ  : 在庫(FBA/ココ/自社/依頼済)・現ロット/発注済 など
  - NE売上状況タブ    : ココドット(NE)チャネルの 7日 / 30日 販売数
  - SP-API           : Amazon の 7日 / 30日 販売数（ASIN突合・注文日ベース）
    ※ シートには Amazon の 7日が無いため、Amazon側のトレンドは SP-API で補完。
      --no-spapi 時はフォーマットK列(Amazon30日)のみで動作（7日Amazon=加速判定なし）。

出力:
  - 推奨事項タブ（bot専用・安全上書き）に全フラグSKUを1行ずつ記録（監査ログ）
  - Chatwork に区分別の要点を配信

環境変数:
  BRAND=nature                         （brands.py のキー）
  SPAPI_REFRESH_TOKEN / SPAPI_LWA_CLIENT_ID / SPAPI_LWA_CLIENT_SECRET
  SPAPI_MARKETPLACE_ID / SPAPI_HOST
  GOOGLE_SA_JSON（SA JSON文字列）または GOOGLE_CREDENTIALS_PATH（ローカル用ファイル）
  SALES_SHEET_ID（対象在庫シートID＝機密。secretで渡す）
  CHATWORK_TOKEN / CHATWORK_ROOM_ID
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import io
from datetime import datetime, timedelta

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account

# 実証済みの SP-API / Sheets ヘルパを流用（sales30d.py は __main__ ガード済で副作用なし）
from sales30d import (
    lwa_token, fetch_orders_tsv, sum_quantity_by_asin,
    _a1, _sheets_call, sheet_read, sheet_update, JST,
)
import brands as brands_mod
import inventory_format
from inventory_format import SEV_RANK  # 重複定義を避け1ヶ所に集約

MARKER_PREFIX = "⚠️ bot自動生成"   # 推奨事項タブのbot所有印（人手タブ誤上書き防止）
REC_HEADERS = [
    "更新日時", "優先度", "区分", "商品名", "サイズ", "SKU", "ASIN",
    "総在庫", "FBA", "ココ", "自社", "依頼済",
    "A日販7d", "A日販30d", "A加速", "コ日販7d", "コ日販30d", "コ加速",
    "FBA在庫日数", "総在庫日数", "在庫切れ予想(総)", "発注点ROP",
    "現→発注済", "推奨アクション", "根拠",
]
REC_COLS = len(REC_HEADERS)  # 25 → A..Y

# 推奨事項タブの見た目（視認性）設定 ───────────────────────────────────────
COL_WIDTHS = [86, 60, 122, 150, 44, 148, 100, 62, 56, 62, 48, 58,
              60, 66, 56, 60, 66, 56, 70, 70, 96, 70, 86, 400, 168]
INT_COLS = [7, 8, 9, 10, 11, 18, 19, 21]   # 桁区切り整数
DEC_COLS = [12, 13, 15, 16]                # 日販（小数1）
ACC_COLS = [14, 17]                        # 加速倍率
SEV_BG = {  # 優先度ごとの薄い行背景（スキャンしやすく）
    "🚨": {"red": 1.0, "green": 0.89, "blue": 0.89},
    "🔴": {"red": 1.0, "green": 0.95, "blue": 0.86},
    "🟡": {"red": 1.0, "green": 0.99, "blue": 0.85},
    "🔺": {"red": 0.89, "green": 0.94, "blue": 1.0},
    "🔻": {"red": 0.95, "green": 0.95, "blue": 0.95},
}
_HEADER_BG = {"red": 0.20, "green": 0.25, "blue": 0.35}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_NOTE_BG = {"red": 0.93, "green": 0.93, "blue": 0.93}
# 列グループ別の薄い背景（数値ブロックを見分けやすく）: (開始列, 終了列exclusive, 色)
COL_GROUPS = [
    (7, 12, {"red": 0.90, "green": 0.95, "blue": 1.00}),   # 在庫 H-L（薄青）
    (12, 15, {"red": 0.91, "green": 0.97, "blue": 0.91}),  # Amazon販売 M-O（薄緑）
    (15, 18, {"red": 0.86, "green": 0.95, "blue": 0.94}),  # ココ販売 P-R（薄青緑）
    (18, 21, {"red": 1.00, "green": 0.96, "blue": 0.87}),  # 在庫日数/予想 S-U（薄橙）
    (21, 23, {"red": 0.96, "green": 0.93, "blue": 0.99}),  # 発注 V-W（薄紫）
]
GROUP_BORDER_COLS = [7, 12, 15, 18, 21, 23]  # グループ境界に縦罫線
_BORDER = {"style": "SOLID", "color": {"red": 0.7, "green": 0.7, "blue": 0.7}}


# ── 環境変数 ───────────────────────────────────────────────────────────────
def _env(name: str, required: bool = True, default: str = "") -> str:
    v = os.getenv(name, default)
    if required and not v:
        sys.exit(f"[FATAL] 環境変数 {name} が未設定")
    return v


# ── パース ─────────────────────────────────────────────────────────────────
def _to_int(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "#N/A", "#DIV/0!", "#REF!", "#VALUE!"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_float(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "#N/A", "#DIV/0!", "#REF!", "#VALUE!"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _cell(row: list, idx: int) -> str:
    return row[idx] if idx < len(row) else ""


# ── Sheets 認証（クラウド=GOOGLE_SA_JSON / ローカル=ファイルパス）─────────────
def sheets_token() -> str:
    sa_json = os.getenv("GOOGLE_SA_JSON", "")
    if sa_json:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    else:
        path = _env("GOOGLE_CREDENTIALS_PATH")
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(GoogleRequest())
    return creds.token


def resolve_title(token: str, sheet_id: str, gid: int) -> str:
    meta = _sheets_call("GET", token, sheet_id, "",
                        params={"fields": "sheets.properties"})
    for sh in meta.get("sheets", []):
        p = sh.get("properties", {})
        if p.get("sheetId") == gid:
            return p["title"]
    raise RuntimeError(f"gid={gid} のシートが見つからない")


def get_or_create_tab(token: str, sheet_id: str, title: str) -> int:
    """推奨事項タブの gid を返す。無ければ作成する。"""
    meta = _sheets_call("GET", token, sheet_id, "",
                        params={"fields": "sheets.properties"})
    for sh in meta.get("sheets", []):
        p = sh.get("properties", {})
        if p.get("title") == title:
            return p.get("sheetId")
    res = _sheets_call("POST", token, sheet_id, ":batchUpdate", body={"requests": [{
        "addSheet": {"properties": {
            "title": title,
            "gridProperties": {"rowCount": 1000, "columnCount": 26},
        }}
    }]})
    return (res["replies"][0]["addSheet"]["properties"]["sheetId"])


def sheet_clear(token: str, sheet_id: str, rng: str) -> None:
    import urllib.parse
    suffix = "/values/" + urllib.parse.quote(rng, safe="") + ":clear"
    _sheets_call("POST", token, sheet_id, suffix, body={})


# ── 読み込み ───────────────────────────────────────────────────────────────
def load_format(token: str, sheet_id: str, brand) -> list[dict]:
    title = resolve_title(token, sheet_id, brand.format_gid)
    start = brand.format_data_start_row
    rows = sheet_read(token, sheet_id, _a1(title, f"A{start}:AN"))
    c = brand.format_cols
    out = []
    for r in rows:
        product = _cell(r, c["product"]).strip()
        sku = _cell(r, c["sku"]).strip()
        if not product or not sku:
            continue
        d = {
            "product": product, "size": _cell(r, c["size"]).strip(),
            "asin": _cell(r, c["asin"]).strip(), "sku": sku,
            "stock_total": _to_int(_cell(r, c["stock_total"])),
            "stock_fba": _to_int(_cell(r, c["stock_fba"])),
            "stock_coco": _to_int(_cell(r, c["stock_coco"])),
            "stock_own": _to_int(_cell(r, c["stock_own"])),
            "requested_qty": _to_int(_cell(r, c["requested_qty"])),
            "sales_amazon_sheet": _to_int(_cell(r, c["sales_amazon"])),
            "sales_coco_sheet": _to_int(_cell(r, c["sales_coco"])),
            "alert_order": _cell(r, c["alert_order"]).strip().upper() == "Y",
            "alert_fba": _cell(r, c["alert_fba"]).strip().upper() == "Y",
            "alert_coco": _cell(r, c["alert_coco"]).strip().upper() == "Y",
            "alert_done": _cell(r, c["alert_done"]).strip().upper() == "Y",
            "lot_current": _cell(r, c["lot_current"]).strip(),
            "lot_ordered": _cell(r, c["lot_ordered"]).strip(),
            "order_lot": _to_int(_cell(r, c["order_lot"])),
            "sku_comment": _cell(r, c["sku_comment"]).strip(),
        }
        out.append(d)
    return out


def load_ne(token: str, sheet_id: str, brand) -> dict:
    title = resolve_title(token, sheet_id, brand.ne_gid)
    start = brand.ne_data_start_row
    rows = sheet_read(token, sheet_id, _a1(title, f"A{start}:H"))
    c = brand.ne_cols
    m = {}
    for r in rows:
        sku = _cell(r, c["sku"]).strip()
        if not sku:
            continue
        m[sku] = (_to_int(_cell(r, c["coco_7d"])), _to_int(_cell(r, c["coco_30d"])))
    return m


# ── Amazon 7d / 30d（SP-API）────────────────────────────────────────────────
def fetch_amazon_windows(sp_token: str, today0: datetime, now: datetime) -> tuple[dict, dict]:
    def window(days):
        start = (today0 - timedelta(days=days - 1)).isoformat(timespec="seconds")
        tsv = fetch_orders_tsv(sp_token, start, now.isoformat(timespec="seconds"))
        return sum_quantity_by_asin(tsv)
    amz30 = window(30)
    amz7 = window(7)
    return amz7, amz30


# ── 分析 ───────────────────────────────────────────────────────────────────
def _eff_velocity(v7, v30, accel, hot):
    if v7 is not None and v30 is not None:
        return v7 if (accel is not None and accel >= hot) else v30
    return v7 if v7 is not None else v30


def _parse_mult(size: str) -> int:
    """サイズ表記から単品換算倍率を得る（'1個'→1, '2個'→2 …）。数値が無ければ1。"""
    m = re.search(r"\d+", size or "")
    return int(m.group()) if m else 1


def analyze(brand, fmt_rows, ne_map, amz7, amz30, today, use_spapi):
    """SKU別に在庫・販売を評価しフラグ付きSKUを返す。

    FBA納品 / ココ補充 / 加速注意 は SKU(出品ASIN)単位。
    製造発注 / 過剰在庫 は商品単位（1個/2個/3個を単品換算で合算し基準SKUに付与）。
    """
    th = brand.thresholds
    metrics = []
    for s in fmt_rows:
        if "終売" in s["sku_comment"]:
            continue
        asin, sku = s["asin"], s["sku"]
        if use_spapi:
            a30, a7 = amz30.get(asin), amz7.get(asin)
        else:
            a30, a7 = s["sales_amazon_sheet"], None
        c7, c30 = ne_map.get(sku, (None, None))
        if c30 is None:
            c30 = s["sales_coco_sheet"]

        v30a = a30 / 30 if a30 is not None else None
        v7a = a7 / 7 if a7 is not None else None
        v30c = c30 / 30 if c30 is not None else None
        v7c = c7 / 7 if c7 is not None else None
        accel_a = (v7a / v30a) if (v7a is not None and v30a) else None
        accel_c = (v7c / v30c) if (v7c is not None and v30c) else None
        ve_a = _eff_velocity(v7a, v30a, accel_a, th.accel_hot)
        ve_c = _eff_velocity(v7c, v30c, accel_c, th.accel_hot)

        stock_fba = s["stock_fba"] or 0
        stock_coco = s["stock_coco"] or 0
        stock_own = s["stock_own"] or 0
        stock_total = s["stock_total"]
        if stock_total is None:
            stock_total = stock_fba + stock_coco + stock_own
        requested = s["requested_qty"] or 0
        days_fba = stock_fba / ve_a if (ve_a and ve_a > 0) else None
        days_coco = stock_coco / ve_c if (ve_c and ve_c > 0) else None
        stockout_fba = today + timedelta(days=days_fba) if days_fba is not None else None

        m = dict(s)
        m.update({
            "mult": _parse_mult(s["size"]),
            "stock_fba": stock_fba, "stock_coco": stock_coco, "stock_own": stock_own,
            "stock_total": stock_total, "requested_qty": requested,
            "amazon_7d": a7, "amazon_30d": a30, "coco_7d": c7, "coco_30d": c30,
            "v7a": v7a, "v30a": v30a, "ve_a": ve_a, "accel_a": accel_a,
            "v7c": v7c, "v30c": v30c, "ve_c": ve_c, "accel_c": accel_c,
            "days_fba": days_fba, "days_coco": days_coco, "stockout_fba": stockout_fba,
            "days_total": None, "stockout_total": None, "rop": None,
            "done": s["alert_done"], "triggers": [],
        })
        _fulfillment_triggers(m, th)
        metrics.append(m)

    # 製造発注・過剰在庫は商品単位（パックを単品換算で合算）で基準SKUに付与
    groups: dict[str, list] = {}
    for m in metrics:
        groups.setdefault(m["product"], []).append(m)
    for rows in groups.values():
        _order_triggers(rows, th, today, use_spapi)

    results = [m for m in metrics if m["triggers"]]
    for m in results:
        primary = min(m["triggers"], key=lambda t: SEV_RANK.get(t["sev"], 9))
        m["primary_sev"], m["primary_kind"] = primary["sev"], primary["kind"]
    results.sort(key=lambda r: (SEV_RANK.get(r["primary_sev"], 9),
                                r["days_total"] if r["days_total"] is not None else 1e9))
    return {"brand": brand, "today": today, "total_skus": len(fmt_rows), "results": results}


def _fulfillment_triggers(m, th):
    """SKU(出品)単位: FBA納品 / ココドット補充 / 加速注意。"""
    ve_a, ve_c = m["ve_a"], m["ve_c"]
    stock_fba, stock_coco = m["stock_fba"], m["stock_coco"]
    days_fba, days_coco = m["days_fba"], m["days_coco"]
    accel_a, accel_c = m["accel_a"], m["accel_c"]
    fast = accel_a is not None and accel_a >= th.accel_hot

    fba_low = days_fba is not None and (
        days_fba < th.fba_low_days or (fast and days_fba < th.fba_fast_days))
    if fba_low and stock_coco > 0:
        ship = max(0, round((th.fba_target_days * ve_a - stock_fba) / 10) * 10)
        ship = min(ship, stock_coco)   # ココ在庫を超える納品提案はしない
        sev = "🚨" if days_fba <= th.fba_urgent_days else "🔴"
        m["triggers"].append({"kind": "FBA", "sev": sev,
            "action": f"FBA納品推奨。FBA残{days_fba:.0f}日（ココ在庫{stock_coco:,}）。目安{ship:,}個をFBAへ。",
            "reason": f"FBA{stock_fba:,}/日販{ve_a:.1f}"})
    elif m["alert_fba"] and stock_coco > 0:
        m["triggers"].append({"kind": "FBA", "sev": "🔴",
            "action": f"シートFBA納品アラートY（ココ在庫{stock_coco:,}）。", "reason": "FBA納品アラートY"})

    if days_coco is not None and days_coco < th.coco_low_days:
        m["triggers"].append({"kind": "COCO", "sev": "🟡",
            "action": f"ココドット在庫補充検討。ココ残{days_coco:.0f}日。",
            "reason": f"ココ{stock_coco:,}/日販{ve_c:.1f}"})
    elif m["alert_coco"]:
        m["triggers"].append({"kind": "COCO", "sev": "🟡",
            "action": "シートココドット納品アラートY。", "reason": "ココ納品アラートY"})

    hot_a = accel_a is not None and accel_a >= th.accel_hot and (m["amazon_30d"] or 0) >= th.trend_min_30d_units
    hot_c = accel_c is not None and accel_c >= th.accel_hot and (m["coco_30d"] or 0) >= th.trend_min_30d_units
    if hot_a or hot_c:
        which = []
        if hot_a:
            which.append(f"Amazon7日が30日の{accel_a:.1f}倍")
        if hot_c:
            which.append(f"ココ7日が30日の{accel_c:.1f}倍")
        so = m["stockout_fba"]
        act = (f"加速注意：{'・'.join(which)}。実ペースだと在庫切れ前倒し（FBA {so:%m/%d}）。発注/納品の前倒しを検討。"
               if so else f"加速注意：{'・'.join(which)}。発注/納品の前倒しを検討。")
        m["triggers"].append({"kind": "TREND", "sev": "🔺", "action": act, "reason": "・".join(which)})


def _order_triggers(rows, th, today, use_spapi):
    """商品単位: パックを単品換算で合算し、基準SKU(最小サイズ)に製造発注/過剰を付与。"""
    base = min(rows, key=lambda m: m["mult"])

    def wsum(field):  # 単品換算の加重合計（pack×倍率）
        return sum((m[field] or 0) * m["mult"] for m in rows)

    a30 = wsum("amazon_30d")
    a7 = wsum("amazon_7d") if use_spapi else None
    c30 = wsum("coco_30d")
    c7 = wsum("coco_7d") if any(m["coco_7d"] is not None for m in rows) else None
    base_stock = wsum("stock_total")

    v30a = a30 / 30 if a30 else None
    v7a = a7 / 7 if a7 is not None else None
    v30c = c30 / 30 if c30 else None
    v7c = c7 / 7 if c7 is not None else None
    accel_a = (v7a / v30a) if (v7a is not None and v30a) else None
    accel_c = (v7c / v30c) if (v7c is not None and v30c) else None
    ve_a = _eff_velocity(v7a, v30a, accel_a, th.accel_hot)
    ve_c = _eff_velocity(v7c, v30c, accel_c, th.accel_hot)
    total_eff = (ve_a or 0) + (ve_c or 0)
    days_total = base_stock / total_eff if total_eff > 0 else None  # 現物枯渇日数（製品計）
    rop = total_eff * (th.lead_time_days + th.safety_days) if total_eff > 0 else None

    base["days_total"] = days_total
    base["rop"] = rop
    base["stockout_total"] = today + timedelta(days=days_total) if days_total is not None else None
    base["base_stock"] = base_stock

    ordered = any(m["lot_ordered"] for m in rows)
    if not ordered and days_total is not None:
        sev = ("🚨" if days_total < th.order_urgent_days
               else "🔴" if days_total < th.order_warn_days else None)
        if sev:
            label = th.order_urgent_days if sev == "🚨" else th.order_warn_days
            verb = "至急製造発注" if sev == "🚨" else "製造発注を検討"
            act = f"{verb}。製品計 物理在庫{days_total:.0f}日（{label}日未満）・在庫{base_stock:,}本。"
            if base["order_lot"]:
                act += f" 推奨ロット目安{base['order_lot']:,}。"
            base["triggers"].append({"kind": "ORDER", "sev": sev, "action": act,
                "reason": (f"製品計物理{days_total:.0f}日/ROP{rop:,.0f}" if rop
                           else f"製品計物理{days_total:.0f}日")})
    elif any(m["alert_order"] for m in rows) and not ordered:
        base["triggers"].append({"kind": "ORDER", "sev": "🔴",
            "action": "シート発注アラートY・未発注。製造発注を検討。", "reason": "発注アラートY"})

    if (days_total is not None and days_total > th.overstock_days
            and (accel_a is None or accel_a <= th.accel_cold) and ordered):
        base["triggers"].append({"kind": "SLOW", "sev": "🔻",
            "action": f"過剰気味：製品計 物理在庫{days_total:.0f}日・減速。追加発注は見送り検討。",
            "reason": f"製品計物理{days_total:.0f}日"})


# ── 推奨事項シート書込（安全上書き: 既存読取→クリア→更新→検証）─────────────
def _rec_row(r: dict, ts: str) -> list:
    def fa(v): return "" if v is None else round(v, 1)
    return [
        ts, r["primary_sev"], "／".join(sorted({t["kind"] for t in r["triggers"]})),
        r["product"], r["size"], r["sku"], r["asin"],
        r["stock_total"], r["stock_fba"], r["stock_coco"], r.get("stock_own") or 0,
        r["requested_qty"],
        fa(r["v7a"]), fa(r["v30a"]),
        ("" if r["accel_a"] is None else round(r["accel_a"], 2)),
        fa(r["v7c"]), fa(r["v30c"]),
        ("" if r["accel_c"] is None else round(r["accel_c"], 2)),
        ("" if r["days_fba"] is None else round(r["days_fba"])),
        ("" if r["days_total"] is None else round(r["days_total"])),
        (r["stockout_total"].strftime("%Y/%m/%d") if r["stockout_total"] else ""),
        ("" if r["rop"] is None else round(r["rop"])),
        f"{r['lot_current'] or '-'}→{r['lot_ordered'] or '-'}",
        "／".join(t["action"] for t in r["triggers"]),
        "／".join(t["reason"] for t in r["triggers"]),
    ]


def _cell_fmt(gid, r0, r1, c0, c1, cell, fields):
    return {"repeatCell": {
        "range": {"sheetId": gid, "startRowIndex": r0, "endRowIndex": r1,
                  "startColumnIndex": c0, "endColumnIndex": c1},
        "cell": cell, "fields": fields}}


def _col_width(gid, idx, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": gid, "dimension": "COLUMNS",
                  "startIndex": idx, "endIndex": idx + 1},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def format_rec_tab(token, sheet_id, gid, sevs, prev_rows):
    """推奨事項タブに書式を当てる（毎回上書き＝冪等）。視認性のための整形。"""
    n = len(sevs)
    end = 2 + n  # データ最終行の次（0始まりexclusive）
    reqs = [
        {"updateSheetProperties": {
            "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
    ]
    reqs += [_col_width(gid, i, w) for i, w in enumerate(COL_WIDTHS)]
    # 注記行（A1）
    reqs.append(_cell_fmt(gid, 0, 1, 0, REC_COLS,
        {"userEnteredFormat": {"backgroundColor": _NOTE_BG,
            "textFormat": {"bold": True, "fontSize": 9}}},
        "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat"))
    # ヘッダ行（row2）
    reqs.append(_cell_fmt(gid, 1, 2, 0, REC_COLS,
        {"userEnteredFormat": {"backgroundColor": _HEADER_BG,
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
            "textFormat": {"bold": True, "fontSize": 9, "foregroundColor": _WHITE}}},
        "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,"
        "wrapStrategy,textFormat)"))
    # 旧データ以下に残る書式を白紙化（件数減で色が残らないよう余白をリセット）
    reset_to = max(end, prev_rows) + 1
    reqs.append(_cell_fmt(gid, end, reset_to + 200, 0, REC_COLS,
        {"userEnteredFormat": {"backgroundColor": _WHITE, "wrapStrategy": "CLIP"}},
        "userEnteredFormat.backgroundColor,userEnteredFormat.wrapStrategy"))

    if n > 0:
        # データ全体の基本書式
        reqs.append(_cell_fmt(gid, 2, end, 0, REC_COLS,
            {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "CLIP",
                "horizontalAlignment": "LEFT", "textFormat": {"fontSize": 9}}},
            "userEnteredFormat(verticalAlignment,wrapStrategy,horizontalAlignment,textFormat)"))
        # データ行を一旦白紙化（前回の全行塗りの残りを消す）
        reqs.append(_cell_fmt(gid, 2, end, 0, REC_COLS,
            {"userEnteredFormat": {"backgroundColor": _WHITE}},
            "userEnteredFormat.backgroundColor"))
        # 列グループ別の背景（在庫/Amazon販売/ココ販売/在庫日数/発注）
        for c0, c1, bg in COL_GROUPS:
            reqs.append(_cell_fmt(gid, 2, end, c0, c1,
                {"userEnteredFormat": {"backgroundColor": bg}},
                "userEnteredFormat.backgroundColor"))
        # グループ境界の縦罫線（ヘッダ行含む）
        for c in GROUP_BORDER_COLS:
            reqs.append(_cell_fmt(gid, 1, end, c, c + 1,
                {"userEnteredFormat": {"borders": {"left": _BORDER}}},
                "userEnteredFormat.borders"))
        # 数値書式＋右寄せ
        for c in INT_COLS:
            reqs.append(_cell_fmt(gid, 2, end, c, c + 1,
                {"userEnteredFormat": {"horizontalAlignment": "RIGHT",
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
                "userEnteredFormat(horizontalAlignment,numberFormat)"))
        for c in DEC_COLS:
            reqs.append(_cell_fmt(gid, 2, end, c, c + 1,
                {"userEnteredFormat": {"horizontalAlignment": "RIGHT",
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0.0"}}},
                "userEnteredFormat(horizontalAlignment,numberFormat)"))
        for c in ACC_COLS:
            reqs.append(_cell_fmt(gid, 2, end, c, c + 1,
                {"userEnteredFormat": {"horizontalAlignment": "RIGHT",
                    "numberFormat": {"type": "NUMBER", "pattern": "0.0\"×\""}}},
                "userEnteredFormat(horizontalAlignment,numberFormat)"))
        # 優先度チップ（行ごとに濃いめ背景＋中央太字。色分けはこの1列に集約）
        for i, sev in enumerate(sevs):
            reqs.append(_cell_fmt(gid, 2 + i, 3 + i, 1, 2,
                {"userEnteredFormat": {"backgroundColor": SEV_BG.get(sev, _WHITE),
                    "horizontalAlignment": "CENTER",
                    "textFormat": {"bold": True, "fontSize": 11}}},
                "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"))
        # 推奨アクション・根拠は折返し
        for c in (23, 24):
            reqs.append(_cell_fmt(gid, 2, end, c, c + 1,
                {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "userEnteredFormat.wrapStrategy"))
    _sheets_call("POST", token, sheet_id, ":batchUpdate", body={"requests": reqs})


def write_recommendation(token: str, sheet_id: str, brand, result, now: datetime) -> int:
    title = brand.rec_tab_title
    last_col = chr(ord("A") + REC_COLS - 1)  # 'Y'
    gid = get_or_create_tab(token, sheet_id, title)
    ts = now.strftime("%Y/%m/%d %H:%M JST")
    note = (f"{MARKER_PREFIX}・編集禁止（編集してもmon/wed/fri更新で消えます） ／ 更新 {ts}"
            f" ／ {len(result['results'])}件")

    # 既存内容を読取（範囲決定＋人手タブ誤上書きガード）。
    # bot所有印で始まらないA1＝人が作った同名タブの可能性→破壊せず中止。
    prev = sheet_read(token, sheet_id, _a1(title, f"A1:{last_col}"))
    prev_a1 = (prev[0][0].strip() if prev and prev[0] and prev[0][0] else "")
    if prev_a1 and not prev_a1.startswith(MARKER_PREFIX):
        raise RuntimeError(
            f"推奨事項タブ '{title}' のA1がbot所有印で始まらない（人手タブの可能性）"
            "→在庫データ破壊を避けるため書込み中止")
    prev_rows = len(prev)

    block = [[note] + [""] * (REC_COLS - 1), REC_HEADERS]
    for r in result["results"]:
        block.append(_rec_row(r, ts))
    sheet_update(token, sheet_id, _a1(title, f"A1:{last_col}{len(block)}"), block)

    # 旧データの余り行をクリア（新行数 < 旧行数のとき）
    if prev_rows > len(block):
        sheet_clear(token, sheet_id, _a1(title, f"A{len(block) + 1}:{last_col}{prev_rows}"))

    # 検証: A1所有印＋ヘッダ行を読み戻して一致確認（行数だけに頼らない）
    after = sheet_read(token, sheet_id, _a1(title, f"A1:{last_col}2"))
    a1_ok = bool(after) and after[0] and str(after[0][0]).startswith(MARKER_PREFIX)
    hdr_ok = len(after) >= 2 and (after[1][:1] == ["更新日時"])
    if not (a1_ok and hdr_ok):
        raise RuntimeError(f"推奨事項シート検証失敗: A1/ヘッダ不一致 (先頭2行={after[:2]})")

    # 書式付与（ヘッダ固定・列幅・色分け・桁区切り・折返し）
    sevs = [r["primary_sev"] for r in result["results"]]
    format_rec_tab(token, sheet_id, gid, sevs, prev_rows)
    return gid


# ── Chatwork ───────────────────────────────────────────────────────────────
def chatwork_post(token: str, room: str, body: str) -> dict:
    r = requests.post(f"https://api.chatwork.com/v2/rooms/{room}/messages",
                      headers={"X-ChatWorkToken": token},
                      data={"body": body, "self_unread": "1"}, timeout=30)
    r.raise_for_status()
    return r.json()


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=os.getenv("BRAND", "nature"))
    ap.add_argument("--dry-run", action="store_true",
                    help="シート書込・Chatwork投稿をせず本文を標準出力")
    ap.add_argument("--no-spapi", action="store_true",
                    help="SP-APIを使わずシートのAmazon30日のみで動作（Amazon加速なし）")
    ap.add_argument("--no-chatwork", action="store_true",
                    help="推奨事項シートは更新するがChatwork投稿はしない")
    args = ap.parse_args()

    brand = brands_mod.get_brand(args.brand)
    sheet_id = _env("SALES_SHEET_ID")
    now = datetime.now(JST).replace(tzinfo=None)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    gtok = sheets_token()
    fmt_rows = load_format(gtok, sheet_id, brand)
    ne_map = load_ne(gtok, sheet_id, brand)
    print(f"[info] {brand.name}: フォーマット{len(fmt_rows)}SKU / NE{len(ne_map)}SKU")

    use_spapi = not args.no_spapi
    amz7, amz30 = {}, {}
    if use_spapi:
        try:
            sp = lwa_token()
            amz7, amz30 = fetch_amazon_windows(sp, today0, now)
            print(f"[info] SP-API Amazon: 7d {sum(amz7.values())} / 30d {sum(amz30.values())}")
        except Exception as e:
            print(f"[warn] SP-API取得失敗→シートAmazon30日にフォールバック: "
                  f"{type(e).__name__}: {str(e)[:120]}")
            use_spapi = False

    result = analyze(brand, fmt_rows, ne_map, amz7, amz30, now, use_spapi)
    result["sheet_url"] = brands_mod.sheet_url(sheet_id, brand.format_gid)
    flagged = len(result["results"])
    print(f"[info] フラグSKU={flagged}")

    body = inventory_format.build_message(result)

    if args.dry_run:
        print("\n===== DRY RUN (Chatwork本文) =====\n")
        print(body)
        print("\n===== 推奨事項シート行（先頭5件）=====")
        ts = now.strftime("%Y/%m/%d %H:%M")
        for r in result["results"][:5]:
            print(_rec_row(r, ts))
        return 0

    gid = write_recommendation(gtok, sheet_id, brand, result, now)
    print(f"[ok] 推奨事項シート更新 gid={gid}（{flagged}件）")
    if args.no_chatwork:
        print("[info] --no-chatwork: Chatwork投稿をスキップ")
        return 0
    if brand.chatwork_mentions == "" and flagged == 0:
        print("[info] 要対応0件・メンション未設定→Chatwork投稿スキップ")
        return 0
    resp = chatwork_post(_env("CHATWORK_TOKEN"), _env("CHATWORK_ROOM_ID"), body)
    print(f"[ok] Chatwork投稿 message_id={resp.get('message_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
