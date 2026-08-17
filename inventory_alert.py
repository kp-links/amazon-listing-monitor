# -*- coding: utf-8 -*-
"""在庫トレンド＆発注アラート（クラウド常駐 / GitHub Actions）。

単一の30日移動平均では直近の需要急変を拾えず欠品する——という課題に対し、
7日 / 30日 の日販から「加速度（7日÷30日）」を見て、加速SKUは速い側のペースで
在庫切れを再評価する。発注はSKUマスタのリードタイム＋安全在庫で発注点(ROP)を引く
（マスタ未設定SKUは既定LT=Thresholds.lead_time_days にフォールバック）。

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
  SNAPSHOT_SHEET_ID（任意。蓄積先スプシID。設定時は🧩SKUマスタdraftタブから
    SKU別LT/専用品目/構成数を読む。未設定なら従来動作=一律LT・サイズ文字列換算）
  UNIT_CHECK_SHEET_ID（任意。『単袋単位の在庫切れ予想』スプシID。設定時は
    bot単袋換算とシート数式の一致チェックを行い、乖離をwarn出力）
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
import sheet_health
from inventory_format import SEV_RANK  # 重複定義を避け1ヶ所に集約

MARKER_PREFIX = "⚠️ bot自動生成"   # 推奨事項タブのbot所有印（人手タブ誤上書き防止）
REC_HEADERS = [
    "更新日時", "優先度", "区分", "対応済", "商品名", "サイズ", "SKU", "ASIN",
    "総在庫", "FBA", "ココ", "自社", "依頼済",
    "A日販7d", "A日販30d", "A加速", "コ日販7d", "コ日販30d", "コ加速",
    "FBA在庫日数", "総在庫日数", "在庫切れ予想(総)", "発注点ROP",
    "現→発注済", "推奨アクション", "根拠",
]
REC_COLS = len(REC_HEADERS)  # 26 → A..Z
# 「対応済」= フォーマットの対応済列（labo/nature=Z, qiera=X）が Y のとき ✅。
# FBA納品等のアラートに倉庫側が着手済みかを一目で分かるようにする列。

# 推奨事項タブの見た目（視認性）設定 ───────────────────────────────────────
# ※ 列を挿入したら下記の列インデックスをすべて再採番すること（データ正誤はヘッダ↔
#   行の対応で担保。ここは色/幅/罫線の見た目のみ）。
COL_WIDTHS = [86, 60, 122, 52, 150, 44, 148, 100, 62, 56, 62, 48, 58,
              60, 66, 56, 60, 66, 56, 70, 70, 96, 70, 86, 400, 168]
INT_COLS = [8, 9, 10, 11, 12, 19, 20, 22]  # 桁区切り整数
DEC_COLS = [13, 14, 16, 17]                # 日販（小数1）
ACC_COLS = [15, 18]                        # 加速倍率
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
    (8, 13, {"red": 0.90, "green": 0.95, "blue": 1.00}),   # 在庫（薄青）
    (13, 16, {"red": 0.91, "green": 0.97, "blue": 0.91}),  # Amazon販売（薄緑）
    (16, 19, {"red": 0.86, "green": 0.95, "blue": 0.94}),  # ココ販売（薄青緑）
    (19, 22, {"red": 1.00, "green": 0.96, "blue": 0.87}),  # 在庫日数/予想（薄橙）
    (22, 24, {"red": 0.96, "green": 0.93, "blue": 0.99}),  # 発注（薄紫）
]
GROUP_BORDER_COLS = [8, 13, 16, 19, 22, 24]  # グループ境界に縦罫線
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
    # ヘッダ行を突合して列の挿入/削除を検知（不一致なら fail-loud）。
    # 2026-08-17: labo でAD「数量」列挿入により sku_comment が「数量」列を読み
    # 終売判定がサイレント停止していた。誤列のまま判定を続けるより止める。
    if brand.header_expect:
        hrow = start - 1
        head = sheet_read(token, sheet_id, _a1(title, f"A{hrow}:AZ{hrow}"))
        issues = brands_mod.verify_format_headers(head[0] if head else [], brand)
        if issues:
            raise RuntimeError("フォーマット列ズレ検知: " + " / ".join(issues))
    # AZ まで広く読む（悩み解決ラボはマイクロアルジェ列増設＋AD数量列で
    # sku_comment が AP=41。旧 A:AN=39 止まりだと範囲外で欠落するため AZ に拡張）。
    rows = sheet_read(token, sheet_id, _a1(title, f"A{start}:AZ"))
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


# ── SKUマスタ（蓄積先スプシの🧩SKUマスタdraftタブ）──────────────────────────
MASTER_TAB = "🧩SKUマスタdraft"
# 列: A商品名 Bサイズ C ASIN D SKU E発注先 F LT(ヶ月) G LT(日) H専用品目
#     I基準単品SKU J構成数 K LT根拠 L販売30d M確認メモ（データは4行目から）


def load_sku_master(token: str) -> dict:
    """SKU別マスタ（LT日数・専用品目・基準単品SKU・構成数）を読む。

    SNAPSHOT_SHEET_ID 未設定なら空dict＝従来動作（一律LT・サイズ文字列換算）。
    設定済みで読めない/空の場合は fail-loud（誤ったLT・構成のまま判定しない）。
    """
    sid = os.getenv("SNAPSHOT_SHEET_ID", "")
    if not sid:
        print("[info] SNAPSHOT_SHEET_ID未設定→SKUマスタなし（一律LT・サイズ換算で動作）")
        return {}
    rows = sheet_read(token, sid, _a1(MASTER_TAB, "A4:J"))
    m = {}
    for r in rows:
        sku = _cell(r, 3).strip()
        if not sku:
            continue
        m[sku] = {
            "lt_days": _to_int(_cell(r, 6)),
            "dedicated": _cell(r, 7).strip().upper() == "TRUE",
            "base_sku": _cell(r, 8).strip(),
            "unit_qty": _to_int(_cell(r, 9)),
        }
    if not m:
        raise RuntimeError(
            f"SKUマスタ '{MASTER_TAB}' が0行（SNAPSHOT_SHEET_ID設定済み）→タブ名/内容を確認")
    n_lt = sum(1 for v in m.values() if v["lt_days"])
    print(f"[info] SKUマスタ読込: {len(m)}SKU / LT設定{n_lt}件")
    return m


# ── 単袋換算の一致チェック（並行計算の突合）─────────────────────────────────
# botの単袋換算（マスタ構成数×在庫）は、既存スプシ『単袋単位の在庫切れ予想＆
# 出荷依頼数確認表』の数式と同じ会計概念の独立計算にあたるため、一致アサーションを
# 置く（同一概念の並行計算禁止ルール）。乖離はwarn出力（アラート本体は止めない）。
UNIT_TAB_BY_BRAND = {"labo": "悩み解決ラボ", "nature": "LUBEE", "qiera": "Qiera"}


def unit_parity_check(token: str, brand, fmt_rows: list, master: dict) -> None:
    sid = os.getenv("UNIT_CHECK_SHEET_ID", "")
    tab = UNIT_TAB_BY_BRAND.get(brand.key)
    if not sid or not master or not tab:
        return
    try:
        rows = sheet_read(token, sid, _a1(tab, "A1:J"))
        # 単袋シート: A商品名 Bサイズ C在庫合計 D単袋換算(依頼済み込み) …（基準行のみ値あり）。
        # bot の stock_total（フォーマットE列）は依頼済み数量込みのため、突合先は
        # D列（在庫合計の単袋換算）。J列（実在庫のみ）と比べると依頼済み分だけ乖離する。
        sheet_bags = {}
        for r in rows:
            m = re.search(r"[\d,]+", str(_cell(r, 3)))
            if m:
                sheet_bags[(_cell(r, 0).strip(), _cell(r, 1).strip())] = \
                    int(m.group().replace(",", ""))
        # bot側: マスタの基準単品SKUでプール化し Σ(在庫合計×構成数)
        pools: dict[str, list] = {}
        for s in fmt_rows:
            mm = master.get(s["sku"])
            # 専用品目は単袋シートの「30日袋換算」規約と単位が異なるため突合対象外
            # （例: エクオルピュア90日はbot=専用袋数、シート=30日袋×3換算）。
            if not mm or mm.get("dedicated"):
                continue
            pools.setdefault(mm.get("base_sku") or s["product"], []).append(s)
        checked = ng = 0
        for members in pools.values():
            bags = sum((s["stock_total"] or 0) *
                       ((master[s["sku"]].get("unit_qty")) or _parse_mult(s["size"]))
                       for s in members)
            bkey = min(members,
                       key=lambda s: master[s["sku"]].get("unit_qty") or _parse_mult(s["size"]))
            key = (bkey["product"], bkey["size"])
            if key not in sheet_bags or bags <= 0:
                continue
            checked += 1
            ref = sheet_bags[key]
            if ref and abs(bags - ref) / ref > 0.02:
                ng += 1
                print(f"[warn] 単袋換算乖離 {key[0]}({key[1]}): "
                      f"bot={bags:,}袋 / 単袋シート={ref:,}袋（±2%超）")
        print(f"[info] 単袋換算一致チェック: {checked}品目中 乖離{ng}件"
              + ("" if ng == 0 else " → マスタ構成数 or 単袋シート数式を確認"))
    except Exception as e:
        print(f"[warn] 単袋換算一致チェック失敗（本体継続）: "
              f"{type(e).__name__}: {str(e)[:160]}")


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


def analyze(brand, fmt_rows, ne_map, amz7, amz30, today, use_spapi, master=None):
    """SKU別に在庫・販売を評価しフラグ付きSKUを返す。

    FBA納品 / ココ補充 / 加速注意 は SKU(出品ASIN)単位。
    製造発注 / 過剰在庫 は品目単位。SKUマスタがあれば基準単品SKUで束ね
    （専用品目=独立プール）、構成数で単袋換算して基準SKUに付与。
    マスタ無しは従来通り商品名で束ね、サイズ文字列の数字を倍率にする。
    """
    th = brand.thresholds
    master = master or {}
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

        mm = master.get(sku)
        m = dict(s)
        m.update({
            # 単袋換算倍率: マスタの構成数を優先（30日分=1, 60日分=2 …の実倍率）。
            # マスタ無しSKUはサイズ文字列の数字（30/60/90等）。分子分母で同じ係数が
            # 掛かるため在庫日数比は正しいが、絶対量（在庫N本表示）はマスタ時のみ正確。
            "mult": (mm.get("unit_qty") if mm else None) or _parse_mult(s["size"]),
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

    # 製造発注・過剰在庫は品目単位（パックを単袋換算で合算）で基準SKUに付与。
    # 束ねキーはマスタの基準単品SKU（専用品目は自分自身が基準=独立プール。
    # 例: エクオルピュア90日分 B0BWWN3D9Q は専用大容量サイズで単品プールと別製造）。
    # マスタ未登録SKUは従来通り商品名で束ねる。
    groups: dict[str, list] = {}
    for m in metrics:
        mm = master.get(m["sku"])
        if mm and mm.get("dedicated"):
            gkey = m["sku"]  # 専用品目は常に独立プール（base_sku誤設定でも混ぜない）
        elif mm:
            gkey = mm.get("base_sku") or m["product"]
        else:
            gkey = m["product"]
        groups.setdefault(gkey, []).append(m)
    for rows in groups.values():
        # 品目LT: グループ内マスタLTの最大値（保守的）。無ければ既定LTで従来挙動。
        lts = [master[x["sku"]]["lt_days"] for x in rows
               if x["sku"] in master and master[x["sku"]]["lt_days"]]
        _order_triggers(rows, th, today, use_spapi,
                        lt_days=max(lts) if lts else None)

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


def _order_triggers(rows, th, today, use_spapi, lt_days=None):
    """品目単位: パックを単袋換算で合算し、基準SKU(最小サイズ)に製造発注/過剰を付与。

    lt_days: SKUマスタ由来の品目別リードタイム（日）。None なら既定LT。
    発注sevの閾値は「既定LTでの指定値(order_urgent/warn_days)を、LT差分だけ
    平行移動」する＝既定LTのとき従来挙動と完全一致（閾値の絶対オフセット維持）。
    """
    base = min(rows, key=lambda m: m["mult"])
    lt = lt_days if lt_days is not None else th.lead_time_days
    cover = lt + th.safety_days                       # 発注点の在庫日数カバー
    lt_shift = cover - (th.lead_time_days + th.safety_days)
    urgent_days = th.order_urgent_days + lt_shift
    warn_days = th.order_warn_days + lt_shift

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
    rop = total_eff * cover if total_eff > 0 else None

    base["days_total"] = days_total
    base["rop"] = rop
    base["stockout_total"] = today + timedelta(days=days_total) if days_total is not None else None
    base["base_stock"] = base_stock

    ordered = any(m["lot_ordered"] for m in rows)
    if not ordered and days_total is not None:
        sev = ("🚨" if days_total < urgent_days
               else "🔴" if days_total < warn_days else None)
        if sev:
            label = urgent_days if sev == "🚨" else warn_days
            verb = "至急製造発注" if sev == "🚨" else "製造発注を検討"
            act = f"{verb}。製品計 物理在庫{days_total:.0f}日（{label}日未満）・在庫{base_stock:,}本。"
            if base["order_lot"]:
                act += f" 推奨ロット目安{base['order_lot']:,}。"
            base["triggers"].append({"kind": "ORDER", "sev": sev, "action": act,
                "reason": (f"製品計物理{days_total:.0f}日/ROP{rop:,.0f}(LT{lt}日)" if rop
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
        ("✅" if r.get("done") else ""),
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
        for c in (24, 25):
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
                    help="在庫アラート本文のChatwork投稿をしない（シート更新はする）。"
                         "※データ健全性の警告は本フラグと独立に投稿する（毎日監視のため）")
    ap.add_argument("--no-health", action="store_true",
                    help="データ健全性チェック（sheet_health）を実行しない")
    args = ap.parse_args()

    brand = brands_mod.get_brand(args.brand)
    sheet_id = _env("SALES_SHEET_ID")
    now = datetime.now(JST).replace(tzinfo=None)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    gtok = sheets_token()
    fmt_rows = load_format(gtok, sheet_id, brand)
    ne_map = load_ne(gtok, sheet_id, brand)
    sku_master = load_sku_master(gtok)
    print(f"[info] {brand.name}: フォーマット{len(fmt_rows)}SKU / NE{len(ne_map)}SKU")

    # データ健全性チェック（サイレント障害検知）。失敗しても本体は止めない。
    # 警告は曜日ゲート（--no-chatwork）と独立に投稿する（火木土日でも通知）。
    # 状態保存は投稿成否の確定後（失敗時は通知状態を進めず次回再送させる）。
    health_body = ""
    if not args.no_health:
        try:
            h_issues, health_body, h_state = sheet_health.run_health_checks(
                gtok, sheet_id, brand, fmt_rows, now, dry_run=args.dry_run)
            for i in h_issues:
                print(f"[health] {i['sev']} {i['title']} / {i['detail']}")
            if not h_issues:
                print("[info] 健全性チェック: 異常なし")
            if not args.dry_run:
                posted = False
                if health_body:
                    # _env(required=True)はSystemExitで本体ごと落とすため使わない
                    # （Chatwork未設定のローカル実行でもシート更新は完遂させる）。
                    cw_token = os.getenv("CHATWORK_TOKEN", "")
                    cw_room = os.getenv("CHATWORK_ROOM_ID", "")
                    if cw_token and cw_room:
                        try:
                            resp = chatwork_post(cw_token, cw_room, health_body)
                            posted = True
                            print(f"[ok] 健全性警告をChatwork投稿 "
                                  f"message_id={resp.get('message_id')}")
                        except Exception as e:
                            print(f"[warn] 健全性警告のChatwork投稿失敗（次回再送）: "
                                  f"{type(e).__name__}: {str(e)[:200]}")
                    else:
                        print("[warn] CHATWORK_TOKEN/ROOM未設定→健全性警告は投稿せず"
                              "（次回再送）。本文:\n" + health_body)
                sheet_health.commit_state(gtok, sheet_id, h_state, posted)
        except Exception as e:
            print(f"[warn] 健全性チェック失敗（在庫アラート本体は継続）: "
                  f"{type(e).__name__}: {str(e)[:200]}")

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

    result = analyze(brand, fmt_rows, ne_map, amz7, amz30, now, use_spapi,
                     master=sku_master)
    result["sheet_url"] = brands_mod.sheet_url(sheet_id, brand.format_gid)
    flagged = len(result["results"])
    print(f"[info] フラグSKU={flagged}")
    unit_parity_check(gtok, brand, fmt_rows, sku_master)

    body = inventory_format.build_message(result)

    if args.dry_run:
        print("\n===== DRY RUN (Chatwork本文) =====\n")
        print(body)
        if health_body:
            print("\n===== DRY RUN (健全性警告 Chatwork本文) =====\n")
            print(health_body)
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
