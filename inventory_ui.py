# -*- coding: utf-8 -*-
"""Inventory Pulse — 最小UI（Phase3 先行版）。

GCS FUSE 上の在庫スナップショット Parquet を読み、
①今日の在庫アラート ②フォーマットv2ビュー（在庫管理シートのライブ表示・
シート同順・全列＋bot指標）③在庫推移（行=SKU × 列=日付、時間軸は横）を表示する。
②はライブ読み取りが使えない環境ではスナップショット表にフォールバックする。

設計方針:
  * 判断材料ファースト（policy_analysis_first_decision_tools）
    — データ陳列でなく、件数サマリ＋優先順に並べたアクションを先頭に置く
  * 時間軸は必ず横（policy_pulse_time_axis_horizontal）
  * 読み取り専用。書き戻し（対応済チェック等）は Phase4
  * 計算はしない。表示するのはスナップショットの生値と bot の算出値のみ
    （指標の一本化は Phase2。ここで独自計算を足すと並行計算の再生産になる）

起動（Cloud Run サービス）:
  streamlit run inventory_ui.py --server.port=8080 --server.address=0.0.0.0
  env: SNAPSHOT_DATA_ROOT=/mnt/gcs/data / UI_TOKEN=<アクセストークン>
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Inventory Pulse", page_icon="📦", layout="wide")

# ── ゲート（他Pulse同様、URLトークン方式。IAPはPhase4で検討）────────────────
# fail-closed: UI_TOKEN 未設定なら公開しない（--allow-unauthenticated 前提のため、
# 設定漏れ=全公開になる事故を構造的に防ぐ）。
_TOKEN = os.environ.get("UI_TOKEN", "")
if not _TOKEN:
    st.error("UI_TOKEN が未設定のため表示できません（サービスの env を確認）")
    st.stop()
if st.query_params.get("token", "") != _TOKEN:
    st.error("アクセストークンが必要です（URL 末尾に ?token=... を付けてください）")
    st.stop()

_ROOT = Path(os.environ.get("SNAPSHOT_DATA_ROOT", "data")) / "inventory_snapshot"
_SEV = {"🚨": 0, "🔴": 1, "🟡": 2, "🔺": 3, "🔻": 4}   # inventory_format と同順


# 識別子列はカンマ整形の対象外（JAN型の数字SKU等にカンマが付くと識別子が壊れる）
_ID_COLS = {"SKU", "ASIN", "商品名", "サイズ", "日付"}

# ── 配色（推奨事項タブ inventory_alert.SEV_BG と同じ視覚言語。色はステータスの
#    補助であり、優先度絵文字・数値が主情報＝色単独に依存しない）─────────────
_INK = "#1a1a1a"                       # 背景を塗るセルは文字色も固定（ダークテーマ対策）
_SEV_BG = {"🚨": "#ffe0e0", "🔴": "#fff1dc", "🟡": "#fffbd6",
           "🔺": "#e3efff", "🔻": "#f0f0f0"}
_TINT_STOCK = "#eef5ff"    # 在庫ブロック（薄青）
_TINT_SALES = "#edf7ed"    # 販売ブロック（薄緑）
_TINT_DAYS = "#fff5e6"     # 在庫日数/bot指標ブロック（薄橙）
_DAYS_URGENT = "#ffd2d2"   # 在庫日数<120日
_DAYS_WARN = "#ffe8c2"     # 在庫日数<180日


def _cellmap(sty, fn, subset):
    """pandas 2.1 で applymap→map に改名されたための互換ラッパ。"""
    mapper = getattr(sty, "map", None) or getattr(sty, "applymap")
    return mapper(fn, subset=subset)


def _isnum(v) -> bool:
    """numpy.int64/float64 を含む数値判定（bool除外・NaN除外）。"""
    import numbers
    return (isinstance(v, numbers.Number) and not isinstance(v, bool)
            and not pd.isna(v))


def _bg(color: str, extra: str = "") -> str:
    return f"background-color:{color};color:{_INK}" + (f";{extra}" if extra else "")


def _pin_cols(cols) -> dict:
    """指定列を左に固定する column_config（pinned 未対応の旧streamlitでは無効化）。"""
    try:
        return {c: st.column_config.Column(pinned=True) for c in cols}
    except TypeError:
        return {}


def _style_commas(frame: pd.DataFrame):
    """数値列をカンマ区切り（小数切捨て表示）で整形した Styler を返す。

    値そのものは変えない（表示のみ）。文字列で数値が入っている列（parquetの
    空欄""混在で object になった列）は to_numeric で寄せてから判定する。
    """
    out = frame.copy()
    fmt = {}
    for col in out.columns:
        if col in _ID_COLS:
            continue
        if out[col].dtype == object:
            conv = pd.to_numeric(out[col], errors="coerce")
            # 過半が数値なら数値列とみなす（SKU等の文字列列を巻き込まない）
            if conv.notna().sum() >= max(1, int(out[col].notna().sum() * 0.5)):
                out[col] = conv
        if pd.api.types.is_numeric_dtype(out[col]):
            fmt[col] = "{:,.0f}"
    return out.style.format(fmt, na_rep="")


def _apply_sev_rows(sty, sev_col: str):
    """優先度列の値に応じて行全体に薄い背景を敷く（推奨事項タブと同配色）。"""
    def _row(row):
        c = _SEV_BG.get(str(row.get(sev_col, "")).strip())
        return [_bg(c) if c else "" for _ in row]
    return sty.apply(_row, axis=1)


# 列ブロック色の明/暗2段（商品グループの偶奇で振る）。左=偶数グループ、右=奇数。
_BLOCK_SHADES = {}
for _c in ("総在庫", "FBA在庫", "ココ在庫", "マイクロアルジェAmazon在庫",
           "マイクロアルジェ楽天在庫", "自社在庫", "依頼済数量"):
    _BLOCK_SHADES[_c] = (_TINT_STOCK, "#dcebfd")          # 在庫=薄青
for _c in ("シート販売数(総)", "シート販売数(Amazon)", "シート販売数(ココ)",
           "NEココ30d", "botA日販30d", "botコ日販30d"):
    _BLOCK_SHADES[_c] = (_TINT_SALES, "#dbeedd")          # 販売=薄緑
for _c in ("シート在庫日数(総)", "シート在庫日数(Amazon)", "シート在庫日数(ココ)",
           "botFBA在庫日数", "bot総在庫日数", "bot発注点ROP",
           "シート在庫切れ(総)", "シート在庫切れ(Amazon)", "シート在庫切れ(ココ)",
           "bot在庫切れ予想(総)"):
    _BLOCK_SHADES[_c] = (_TINT_DAYS, "#fbe9cf")           # 日数/bot指標=薄橙
_BAND_ID = ("#ffffff", "#e8ecf1")                          # 識別子・その他の列


def _apply_product_bands(sty, frame: pd.DataFrame):
    """商品グループごとの行バンド配色。

    同一商品（連続行）は同トーン、次の商品で明/暗を切替え、商品の切れ目に
    上罫線を引く。列ブロック色（在庫=青/販売=緑/日数=橙）は保ったまま
    明暗2段で縞にするので、縦のブロック感と横の商品まとまりが両立する。
    """
    prod = frame["商品名"].astype(str)
    grp = (prod != prod.shift()).cumsum()
    odd = (grp % 2 == 1)
    first = (prod != prod.shift())

    def _row(row):
        is_odd = bool(odd.loc[row.name])
        border = "border-top:2px solid #9aa4b5;" if bool(first.loc[row.name]) else ""
        css = []
        for col in row.index:
            ev, od = _BLOCK_SHADES.get(col, _BAND_ID)
            css.append(f"{border}background-color:{od if is_odd else ev};color:{_INK}")
        return css
    return sty.apply(_row, axis=1)


# 在庫日数の警告閾値（列ごと）。総在庫=発注判定の既定（緊急120/警告180）、
# FBA/ココ=納品判定の既定（bot fba_low=30日、45日=納品在庫基準）に合わせる。
_DAYS_THRESH = {
    "シート在庫日数(総)": (120, 180), "bot総在庫日数": (120, 180),
    "シート在庫日数(Amazon)": (30, 45), "botFBA在庫日数": (30, 45),
    "シート在庫日数(ココ)": (30, 45),
}


def _apply_days_alert(sty, cols):
    """在庫日数セルの警告色（列別閾値: 赤=緊急未満/橙=警告未満）。"""
    for col, (urgent, warn) in _DAYS_THRESH.items():
        if col not in cols:
            continue

        def _cell(v, _u=urgent, _w=warn):
            if not _isnum(v):
                return ""
            if v < _u:
                return _bg(_DAYS_URGENT, "font-weight:600")
            if v < _w:
                return _bg(_DAYS_WARN)
            return ""
        sty = _cellmap(sty, _cell, [col])
    return sty


def _apply_heat(sty, frame: pd.DataFrame, cols):
    """推移ピボットの単色濃淡ヒートマップ（白→薄青、表全体でmin-max正規化）。

    文字は常に _INK＝濃色側でも可読な明度域（白〜#a9c9f5）に収める。
    """
    vals = frame[cols].apply(pd.to_numeric, errors="coerce")
    vmin, vmax = vals.min().min(), vals.max().max()
    span = (vmax - vmin) or 1.0

    def _cell(v):
        if not _isnum(v):
            return ""
        t = max(0.0, min(1.0, (v - vmin) / span))
        r = round(255 - (255 - 169) * t)   # 255→169 (#a9)
        g = round(255 - (255 - 201) * t)   # 255→201 (#c9)
        bl = round(255 - (255 - 245) * t)  # 255→245 (#f5)
        return _bg(f"rgb({r},{g},{bl})")
    return _cellmap(sty, _cell, [c for c in cols])


def _apply_delta(sty, col):
    """Δ期間: 減=薄赤/増=薄緑（ゼロ・欠損は無色）。"""
    def _cell(v):
        if not _isnum(v) or v == 0:
            return ""
        return _bg("#ffd9d9" if v < 0 else "#d9f0dd")
    return _cellmap(sty, _cell, [col])


@st.cache_data(ttl=600)
def load() -> pd.DataFrame:
    files = sorted(_ROOT.glob("inventory_*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)


# ── ② フォーマットv2ビュー（在庫管理シートのライブ表示・読み取り専用）──────────
# 列位置の正本は brands.py の format_cols＋inventory_snapshot.EXTRA_STOCK_COLS。
# ここで列番号を再定義しない（二重管理は必ず片方が腐る）。
# 表示名はスナップショット表と同じ語彙に寄せ、列ブロック色・警告閾値の定義を流用する。
_SRC_SHEET = os.environ.get("SALES_SHEET_ID", "")

_V2_LABEL = {
    "product": "商品名", "size": "サイズ", "asin": "ASIN", "sku": "SKU",
    "stock_total": "総在庫", "stock_fba": "FBA在庫", "stock_coco": "ココ在庫",
    "micro_amazon": "マイクロアルジェAmazon在庫",
    "micro_rakuten": "マイクロアルジェ楽天在庫",
    "stock_own": "自社在庫", "requested_qty": "依頼済数量",
    "sales_total": "シート販売数(総)", "sales_amazon": "シート販売数(Amazon)",
    "sales_coco": "シート販売数(ココ)",
    "days_total": "シート在庫日数(総)", "days_amazon": "シート在庫日数(Amazon)",
    "days_coco": "シート在庫日数(ココ)",
    "stockout_total": "シート在庫切れ(総)", "stockout_amazon": "シート在庫切れ(Amazon)",
    "stockout_coco": "シート在庫切れ(ココ)",
    "delivery_deadline": "在庫納品期限", "repeat_order_deadline": "リピート発注期限",
    "alert_order": "発注アラート", "alert_fba": "FBA納品アラート",
    "alert_coco": "ココ納品アラート", "alert_done": "対応済",
    "lot_current": "現ロット", "lot_ordered": "発注済ロット",
    "delivery_plan": "納品予定", "delivery_plan_qty": "納品予定数量",
    "order_lot": "発注ロット数", "sku_comment": "SKU全体コメント",
    # 出荷依頼のシート実ヘッダは「日付/…出荷依頼数/数量」の繰り返しで区別が
    # つかないため、表示名でチャネルを明示する
    "amazon_todo_date": "Amazon出荷依頼:日付", "amazon_todo": "Amazon出荷依頼:内容",
    "amazon_todo_qty": "Amazon出荷依頼:数量",
    "coco_todo_date": "ココ出荷依頼:日付", "coco_todo": "ココ出荷依頼:内容",
    "coco_todo_qty": "ココ出荷依頼:数量",
}
# 数値変換しない役割（識別子・日付・アラート・手入力テキスト）。SKUはJAN型の
# 数字列があるため数値化するとゼロ落ち・突合不能になる（識別子は文字列のまま）。
_V2_TEXT_ROLES = {
    "product", "size", "asin", "sku",
    "stockout_total", "stockout_amazon", "stockout_coco",
    "delivery_deadline", "repeat_order_deadline",
    "alert_order", "alert_fba", "alert_coco", "alert_done",
    "lot_current", "lot_ordered", "delivery_plan",
    "new_lot_assign", "aerologi", "set_assembly", "order_consider",
    "amazon_todo_date", "amazon_todo", "coco_todo_date", "coco_todo",
    "sku_comment",
}


@st.cache_data(ttl=600)
def _load_v2_live(brand_key: str) -> tuple[pd.DataFrame, list[str]]:
    """在庫管理シートのフォーマットタブを生値で読む（読み取り専用・書き込みなし）。

    戻り値 = (シート同順の全列DataFrame, ヘッダ検証の警告リスト)。
    表示名は _V2_LABEL を優先し、無い役割は実シートのヘッダ文字列を使う
    （エアロジ・ToDo列など、ラベルの正本はシート側）。
    """
    import brands as brands_mod
    from inventory_alert import resolve_title
    from inventory_snapshot import EXTRA_STOCK_COLS, _num, _token
    from sales30d import _a1, sheet_read

    brand = brands_mod.get_brand(brand_key)
    token = _token()
    title = resolve_title(token, _SRC_SHEET, brand.format_gid)
    start = brand.format_data_start_row
    hrow = start - 1
    head_rows = sheet_read(token, _SRC_SHEET, _a1(title, f"A{hrow}:AZ{hrow}"))
    head = [str(v).strip() for v in (head_rows[0] if head_rows else [])]
    issues = brands_mod.verify_format_headers(head, brand)

    cols = dict(brand.format_cols)
    for k, v in EXTRA_STOCK_COLS.get(brand_key, {}).items():
        cols.setdefault(k, v)
    ordered = sorted(cols.items(), key=lambda kv: kv[1])   # シートの列順
    names, used = [], set()
    for role, idx in ordered:
        nm = _V2_LABEL.get(role) or (head[idx] if idx < len(head) and head[idx]
                                     else role)
        if nm in used:   # 実ヘッダの重複（結合セル由来）は役割名で区別する
            nm = f"{nm}[{role}]"
        used.add(nm)
        names.append(nm)

    rows = sheet_read(token, _SRC_SHEET, _a1(title, f"A{start}:AZ"))
    p_at, s_at = cols["product"], cols["sku"]

    def cell(r, i):
        v = r[i] if i < len(r) else ""
        return v.strip() if isinstance(v, str) else v

    recs = []
    for r in rows:
        if not (cell(r, p_at) and cell(r, s_at)):
            continue
        rec = []
        for role, idx in ordered:
            v = cell(r, idx)
            rec.append(str(v) if role in _V2_TEXT_ROLES else _num(v))
        recs.append(rec)
    df = pd.DataFrame(recs, columns=names)
    if "SKU" in df.columns:
        df["SKU"] = df["SKU"].astype(str)
    return df, issues


def _apply_v2_alert_tint(sty, frame: pd.DataFrame):
    """アラート列: 値あり=薄赤（要対応）。対応済列: 値あり=薄緑。"""
    def _alert(v):
        return (_bg("#ffd2d2", "font-weight:600")
                if str(v).strip() not in ("", "nan") else "")
    for col in ("発注アラート", "FBA納品アラート", "ココ納品アラート"):
        if col in frame.columns:
            sty = _cellmap(sty, _alert, [col])
    if "対応済" in frame.columns:
        sty = _cellmap(sty, lambda v: _bg("#d9f0dd") if str(v).strip() else "",
                       ["対応済"])
    return sty


def _render_v2(today: pd.DataFrame, bkey: str) -> bool:
    """フォーマットv2ビューを描画する。描画できたら True（呼び出し側の

    フォールバック判定に使う）。ライブ読み取りは UI_BRAND 専用サービスのみ
    （SALES_SHEET_ID はそのブランドの在庫管理シートを指すため、開発用の
    全ブランド表示で他ブランドに使うと別シートを読んでしまう）。
    """
    if not (_SRC_SHEET and _UI_BRAND and bkey == _UI_BRAND):
        if bkey == _UI_BRAND or not _UI_BRAND:
            st.info("SALES_SHEET_ID / UI_BRAND が未設定のためライブ表示は無効です")
        return False
    try:
        v2, issues = _load_v2_live(bkey)
    except Exception as e:  # noqa: BLE001 — fail-loud: 理由を画面に出してから代替表示
        st.error("在庫管理シートのライブ読み込みに失敗しました"
                 f"（スナップショット表を表示します）: {type(e).__name__}: {e}")
        return False
    for issue in issues:
        st.warning(f"列ズレの可能性: {issue}")
    if v2.empty:
        st.warning("フォーマットタブから1行も読めませんでした（列マップ/gidを確認）")
        return False

    # bot指標（最新スナップショット）を右端に連結。ライブ値とbot判定を1枚で見る
    bot_cols = [c for c in ("bot優先度", "bot区分", "bot推奨アクション")
                if c in today.columns]
    if bot_cols and "SKU" in v2.columns:
        bot = today[["SKU", *bot_cols]].copy()
        bot["SKU"] = bot["SKU"].astype(str)
        v2 = v2.merge(bot, on="SKU", how="left")

    c1, c2 = st.columns([3, 1])
    q = c1.text_input("絞り込み（商品名/サイズ/SKU/ASIN 部分一致）", "",
                      key=f"v2q_{bkey}")
    todo_only = c2.checkbox("要対応のみ", key=f"v2todo_{bkey}",
                            help="発注/FBA納品/ココ納品アラートのいずれかが立っていて"
                                 "対応済が空の行だけを表示")
    view = v2
    if q.strip():
        mask = pd.Series(False, index=view.index)
        for c in ("商品名", "サイズ", "SKU", "ASIN"):
            if c in view.columns:
                mask |= view[c].astype(str).str.contains(
                    q.strip(), case=False, na=False, regex=False)
        view = view[mask]
    if todo_only:
        acols = [c for c in ("発注アラート", "FBA納品アラート", "ココ納品アラート")
                 if c in view.columns]
        if acols:
            flagged = pd.Series(False, index=view.index)
            for c in acols:
                flagged |= view[c].astype(str).str.strip().ne("")
            if "対応済" in view.columns:
                flagged &= view["対応済"].astype(str).str.strip().eq("")
            view = view[flagged]

    sty = _style_commas(view)
    sty = _apply_product_bands(sty, view)
    sty = _apply_days_alert(sty, view.columns)
    sty = _apply_v2_alert_tint(sty, view)
    if "bot優先度" in view.columns:
        sty = _cellmap(sty, lambda v: _bg(_SEV_BG[str(v).strip()])
                       if str(v).strip() in _SEV_BG else "", ["bot優先度"])
    st.dataframe(sty, use_container_width=True, hide_index=True,
                 column_config=_pin_cols(["商品名", "サイズ"]),
                 height=min(700, 60 + 36 * max(1, len(view))))
    st.caption(f"{len(view)} SKU 表示 ／ 在庫管理シートのライブ値（10分キャッシュ・"
               "サイドバーの更新ボタンで即時再読込）＋右端にbot指標。"
               "本画面からの書き込みはありません（手入力はシート側が正本）。"
               "色: 🟦在庫 🟩販売 🟧在庫日数 ／ 薄赤=アラートあり・薄緑=対応済")
    return True


# ブランドタブ（担当が自ブランドだけを見られるよう完全分離）
_BRAND_ORDER = ["labo", "nature", "qiera"]
_BRAND_LABEL = {"labo": "💊 悩み解決ラボ", "nature": "🧴 ナチュレ（LUBEE）", "qiera": "✨ Qiera"}

_ALL_COLS = ["商品名", "サイズ", "ASIN", "SKU",
             "総在庫", "FBA在庫", "ココ在庫",
             "マイクロアルジェAmazon在庫", "マイクロアルジェ楽天在庫",
             "自社在庫", "依頼済数量",
             "シート販売数(総)", "シート販売数(Amazon)", "シート販売数(ココ)",
             "シート在庫日数(総)", "シート在庫日数(Amazon)", "シート在庫日数(ココ)",
             "botFBA在庫日数", "bot総在庫日数", "bot発注点ROP",
             "シート在庫切れ(総)", "シート在庫切れ(Amazon)", "シート在庫切れ(ココ)",
             "発注アラート", "FBA納品アラート", "ココ納品アラート", "対応済",
             "現ロット", "発注済ロット",
             "bot優先度", "bot推奨アクション"]


def _render_brand(b: pd.DataFrame, bkey: str) -> None:
    """1ブランド分の画面（サマリ→①アラート→②v2ビュー→③推移）。widget keyはブランド別。"""
    dates = sorted(b["日付"].unique())
    latest = dates[-1]
    today = b[b["日付"] == latest]
    st.caption(f"最新スナップショット: {latest} ／ 蓄積 {len(dates)}日分 ／ {len(today)} SKU"
               "（正本は在庫管理シート。本画面は読み取り専用）")

    alerts = today[today["bot優先度"].astype(str).str.strip() != ""].copy()
    if not alerts.empty:
        alerts["_sev"] = alerts["bot優先度"].map(_SEV).fillna(9)
        alerts = alerts.sort_values(["_sev", "商品名"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🚨/🔴 発注・至急", int((alerts["_sev"] <= 1).sum()) if not alerts.empty else 0)
    c2.metric("🟡 納品補充", int((alerts["_sev"] == 2).sum()) if not alerts.empty else 0)
    c3.metric("🔺 加速注意", int((alerts["_sev"] == 3).sum()) if not alerts.empty else 0)
    c4.metric("🔻 過剰在庫", int((alerts["_sev"] == 4).sum()) if not alerts.empty else 0)

    # ①今日の在庫アラート
    st.subheader("① 今日の在庫アラート（bot算出・優先度順）")
    if alerts.empty:
        st.success("フラグの立っているSKUはありません")
    else:
        _acols = ["bot優先度", "bot区分", "商品名", "サイズ", "FBA在庫", "ココ在庫",
                  "総在庫", "botFBA在庫日数", "シート在庫日数(ココ)", "bot総在庫日数",
                  "bot在庫切れ予想(総)", "bot推奨アクション"]
        st.dataframe(
            _apply_sev_rows(_style_commas(alerts[_acols]), "bot優先度"),
            use_container_width=True, hide_index=True,
            height=min(420, 60 + 36 * len(alerts)))

    # ②フォーマットv2ビュー（在庫管理シートのライブ表示）。ライブが使えない
    # 環境（SALES_SHEET_ID未設定・読み取り失敗・開発用全ブランド表示）では
    # 従来のスナップショット表を出す。ライブ成功時もexpanderで併置する
    # （snapshot列にしかないbot日販・在庫日数等の検証用）。
    st.subheader("② フォーマットv2ビュー（在庫管理シートのライブ表示・シートと同順）")
    live_ok = _render_v2(today, bkey)
    if live_ok:
        with st.expander("②b 全SKU一覧（最新スナップショット・bot指標の全列）"):
            _render_snapshot_table(today, bkey)
    else:
        _render_snapshot_table(today, bkey)

    _render_trend(b, bkey)


def _render_snapshot_table(today: pd.DataFrame, bkey: str) -> None:
    """全SKU一覧（最新スナップショット・シートと同順）。"""
    all_rows = today[[c for c in _ALL_COLS if c in today.columns]]
    q = st.text_input("絞り込み（商品名/サイズ/SKU/ASIN 部分一致）", "",
                      key=f"q_{bkey}")
    if q.strip():
        mask = pd.Series(False, index=all_rows.index)
        for c in ("商品名", "サイズ", "SKU", "ASIN"):
            if c in all_rows.columns:
                mask |= all_rows[c].astype(str).str.contains(
                    q.strip(), case=False, na=False, regex=False)
        all_rows = all_rows[mask]
    sty = _style_commas(all_rows)
    sty = _apply_product_bands(sty, all_rows)
    sty = _apply_days_alert(sty, all_rows.columns)
    if "bot優先度" in all_rows.columns:
        sty = _cellmap(sty, lambda v: _bg(_SEV_BG[str(v).strip()])
                       if str(v).strip() in _SEV_BG else "", ["bot優先度"])
    st.dataframe(sty, use_container_width=True, hide_index=True,
                 column_config=_pin_cols(["商品名", "サイズ"]),
                 height=min(700, 60 + 36 * max(1, len(all_rows))))
    st.caption(f"{len(all_rows)} SKU 表示（行順は在庫管理シートと同じ・商品ごとに明暗の縞＋境界線）。"
               "色: 🟦在庫 🟩販売 🟧在庫日数・bot指標 ／ 警告色: 総在庫日数 赤<120日・橙<180日、"
               "FBA/ココ在庫日数 赤<30日・橙<45日 ／ bot列は 2026-08-18 以降の"
               "スナップショットから全SKUに値が入ります（それ以前はフラグSKUのみ）")


def _render_trend(b: pd.DataFrame, bkey: str) -> None:
    """③在庫推移（行=SKU × 列=日付）。"""
    st.subheader("③ 在庫推移（列=日付・新しい日付が右）")
    metric = st.selectbox(
        "指標", ["FBA在庫", "総在庫", "ココ在庫", "シート在庫日数(総)",
                 "シート在庫日数(Amazon)", "シート販売数(Amazon)", "botFBA在庫日数"],
        index=0, key=f"metric_{bkey}")
    pv = b.pivot_table(index=["商品名", "サイズ"], columns="日付",
                       values=metric, aggfunc="first")
    pv = pv[sorted(pv.columns)]
    if len(pv.columns) >= 2:
        first, last = pv.columns[0], pv.columns[-1]
        pv["Δ期間"] = pv[last] - pv[first]
    pv = pv.sort_values(pv.columns[-2] if "Δ期間" in pv.columns else pv.columns[-1],
                        na_position="last")
    pv_flat = pv.reset_index()
    date_cols = [c for c in pv_flat.columns if c not in ("商品名", "サイズ", "Δ期間")]
    psty = _style_commas(pv_flat)
    psty = _apply_heat(psty, pv_flat, date_cols)
    if "Δ期間" in pv_flat.columns:
        psty = _apply_delta(psty, "Δ期間")
    st.dataframe(psty, use_container_width=True, hide_index=True,
                 column_config=_pin_cols(["商品名", "サイズ"]), height=560)
    st.caption("並び順は最新値の昇順（少ない・危ないものが上）。Δ期間 = 最新 − 蓄積初日"
               "（薄赤=減少・薄緑=増加）。濃淡=値の大小（表全体で正規化）。"
               "蓄積が貯まるほど推移の解像度が上がります。")


df = load()
if df.empty:
    st.warning(f"スナップショットがまだ無い（{_ROOT}）")
    st.stop()

# 日付は書き込み側（inventory_snapshot.py）が常に %Y-%m-%d で出すため辞書順=時系列。
if st.sidebar.button("🔄 最新データに更新"):
    load.clear()
    _load_v2_live.clear()
    st.rerun()

# 会社別アプリ分離（2026-08-17 滝谷さん指示）: サードナレッジ/ナチュレ/ディアスリーは
# 別法人のため、1つのUIにブランドタブで同居させない。UI_BRAND を設定した
# サービスはそのブランド専用アプリになる（横展開時はサービスを会社ごとに分けて
# それぞれ別の UI_TOKEN を発行する）。未設定はローカル開発用の全ブランド表示。
_UI_BRAND = os.environ.get("UI_BRAND", "").strip()
present = set(df["ブランド"].unique())
if _UI_BRAND:
    st.title(f"📦 Inventory Pulse — {_BRAND_LABEL.get(_UI_BRAND, _UI_BRAND)}")
    if _UI_BRAND not in present:
        st.warning("このブランドのスナップショット蓄積はまだありません")
        st.stop()
    _render_brand(df[df["ブランド"] == _UI_BRAND], _UI_BRAND)
else:
    st.title("📦 Inventory Pulse（全ブランド・開発用）")
    tab_keys = _BRAND_ORDER + sorted(present - set(_BRAND_ORDER))
    tabs = st.tabs([_BRAND_LABEL.get(k, k) for k in tab_keys])
    for tab, bkey in zip(tabs, tab_keys):
        with tab:
            if bkey not in present:
                st.info("このブランドのスナップショット蓄積は未開始です"
                        "（Cloud Run Job 有効化で自動的に表示されます）")
                continue
            _render_brand(df[df["ブランド"] == bkey], bkey)


# ── Phase4a: SKU別 発注LT の手入力（🧩SKUマスタdraft へ書き戻し）─────────────
# 本画面で唯一の書き込み経路。対象は LT(日) 列（G列）1列のみ。
# 書き込みは fail-closed（例外は画面に出して止める・握りつぶさない）。
# 手順は load → 楽観ロック（SKU列再読で行ズレ検知）→ 変更セルのみ update → 読み戻し verify。
# 変更前後の値は stdout（Cloud Run ログ）に残す＝復元用の記録。
_MASTER_SHEET = os.environ.get("SNAPSHOT_SHEET_ID", "")
_MASTER_TAB = "🧩SKUマスタdraft"
_MASTER_START_ROW = 4          # データ開始行（inventory_alert.load_sku_master と同じ）
_LT_COL_LETTER = "G"           # LT(日)
_LT_MIN, _LT_MAX = 30, 365


@st.cache_data(ttl=60)
def _load_master() -> pd.DataFrame:
    from sales30d import _a1, sheet_read
    from inventory_snapshot import _token
    rows = sheet_read(_token(), _MASTER_SHEET, _a1(_MASTER_TAB, "A4:K"))
    recs = []
    for i, r in enumerate(rows):
        def cell(idx):
            return str(r[idx]).strip() if idx < len(r) else ""
        if not cell(3):
            continue
        lt = pd.to_numeric(cell(6), errors="coerce")
        recs.append({"行": _MASTER_START_ROW + i, "商品名": cell(0), "サイズ": cell(1),
                     "SKU": cell(3), "発注先": cell(4),
                     "LT(日)": None if pd.isna(lt) else int(lt), "LT根拠": cell(10)})
    return pd.DataFrame(recs)


def _save_lt(edited: pd.DataFrame, original: pd.DataFrame) -> tuple[int, list[str]]:
    """変更された LT(日) セルだけを書き戻す。(保存件数, エラーリスト) を返す。"""
    from sales30d import _a1, _sheets_call, sheet_read
    from inventory_snapshot import _token

    changes = []   # (行番号, SKU, 旧値, 新値)
    for idx in original.index:
        old_v, new_v = original.at[idx, "LT(日)"], edited.at[idx, "LT(日)"]
        old_n = None if pd.isna(old_v) else int(old_v)
        new_n = None if pd.isna(new_v) else int(new_v)
        if old_n == new_n:
            continue
        if new_n is not None and not (_LT_MIN <= new_n <= _LT_MAX):
            return 0, [f"{original.at[idx, 'SKU']}: LT {new_n} は範囲外"
                       f"（{_LT_MIN}〜{_LT_MAX}日。空欄=既定LTに戻す）"]
        changes.append((int(original.at[idx, "行"]), str(original.at[idx, "SKU"]),
                        old_n, new_n))
    if not changes:
        return 0, []

    token = _token()
    # 楽観ロック: SKU列（D列）を再読し、書込先の行に想定どおりのSKUがいるか確認。
    # タブ側で行の挿入/削除/並べ替えがあった場合に、隣のSKUのLTを壊すのを防ぐ。
    cur = sheet_read(token, _MASTER_SHEET, _a1(_MASTER_TAB, "D1:D"))
    for rownum, sku, _, _ in changes:
        got = (str(cur[rownum - 1][0]).strip()
               if rownum - 1 < len(cur) and cur[rownum - 1] else "")
        if got != sku:
            return 0, [f"行{rownum} のSKUが '{sku}' でなく '{got}'。"
                       "マスタタブ側で行が動いた可能性→画面を再読込してやり直してください"]

    data = [{"range": _a1(_MASTER_TAB, f"{_LT_COL_LETTER}{rownum}"),
             "values": [["" if new_n is None else new_n]]}
            for rownum, _, _, new_n in changes]
    res = _sheets_call("POST", token, _MASTER_SHEET, "/values:batchUpdate",
                       body={"valueInputOption": "RAW", "data": data})
    if res.get("totalUpdatedCells") != len(changes):
        raise RuntimeError(f"更新セル数が不一致（期待{len(changes)}/"
                           f"実際{res.get('totalUpdatedCells')}）。タブを直接確認してください")

    # 読み戻し verify ＋ 変更ログ（Cloud Run ログに復元用の旧値を残す）
    for rownum, sku, old_n, new_n in changes:
        back = sheet_read(token, _MASTER_SHEET,
                          _a1(_MASTER_TAB, f"{_LT_COL_LETTER}{rownum}"))
        got = str(back[0][0]).strip() if back and back[0] else ""
        want = "" if new_n is None else str(new_n)
        if got != want:
            raise RuntimeError(f"verify失敗: 行{rownum} {sku} のLTが '{got}'（期待 '{want}'）")
        print(f"[lt-edit] 行{rownum} {sku}: {old_n} → {new_n}")
    return len(changes), []


# ── Phase4b(Step2): 📦発注レコード（1ロット=1レコードの発注イベント台帳）────────
# 格納先は蓄積先スプシ（SNAPSHOT_SHEET_ID）の専用タブ。正本v2タブへは一切書かない
# （botの読み取り元と正本が二重化する事故を構造的に避ける＝Step2の確定方針）。
# ステータス語彙は v2 手入力列と同語彙の5段階（移行時に転記変換が要らない）。
# 書き込みは LT編集と同型: fail-closed／楽観ロック（ID列再読）／変更セルのみ
# batchUpdate／読み戻し verify／旧値は stdout（Cloud Run ログ）に記録。
_ORDER_TAB = "📦発注レコード"
_ORDER_MARK = "📦 Inventory Pulse 発注レコード"
_ORDER_HEADERS = ["ID", "記録日時", "ブランド", "SKU", "商品名", "サイズ",
                  "ロットNo", "数量", "発注日", "納品予定日", "ステータス",
                  "コメント", "更新日時"]
_ORDER_LAST_COL = "M"
_ORDER_STATUS = ["発注済", "新ロット振り分け済", "エアロジ入荷登録済",
                 "納品依頼済", "対応済"]
_ORDER_DONE = "対応済"
# UI から編集できる列（それ以外は入力時に確定）。列挿入したら要再採番。
_ORDER_EDITABLE = {"数量": "H", "納品予定日": "J", "ステータス": "K", "コメント": "L"}
_ORDER_TS_COL = "M"            # 更新日時（編集保存時に自動更新）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _order_now() -> str:
    from datetime import datetime
    from inventory_alert import JST
    return datetime.now(JST).strftime("%Y/%m/%d %H:%M JST")


def _ensure_order_tab(token: str) -> None:
    """タブが無ければ作成し、A1所有印＋ヘッダを検証する（人手タブ誤上書きガード）。"""
    from inventory_alert import get_or_create_tab
    from sales30d import _a1, sheet_read, sheet_update
    get_or_create_tab(token, _MASTER_SHEET, _ORDER_TAB)
    head = sheet_read(token, _MASTER_SHEET, _a1(_ORDER_TAB, f"A1:{_ORDER_LAST_COL}2"))
    a1 = str(head[0][0]).strip() if head and head[0] else ""
    if not a1:
        sheet_update(
            token, _MASTER_SHEET, _a1(_ORDER_TAB, f"A1:{_ORDER_LAST_COL}2"),
            [[f"{_ORDER_MARK}（UIから入力・行の挿入/並べ替え/削除は禁止）"]
             + [""] * (len(_ORDER_HEADERS) - 1), _ORDER_HEADERS])
        return
    if not a1.startswith(_ORDER_MARK):
        raise RuntimeError(f"タブ '{_ORDER_TAB}' のA1が所有印で始まらない"
                           "（人手タブの可能性）→書き込み中止")
    hdr = [str(v).strip() for v in (head[1] if len(head) >= 2 else [])]
    if hdr[:len(_ORDER_HEADERS)] != _ORDER_HEADERS:
        raise RuntimeError(f"タブ '{_ORDER_TAB}' のヘッダが想定と不一致→書き込み中止"
                           f"（実際: {hdr[:len(_ORDER_HEADERS)]}）")


@st.cache_data(ttl=60)
def _load_orders() -> pd.DataFrame:
    from sales30d import _a1, sheet_read
    from inventory_snapshot import _token
    token = _token()
    _ensure_order_tab(token)
    rows = sheet_read(token, _MASTER_SHEET, _a1(_ORDER_TAB, f"A3:{_ORDER_LAST_COL}"))
    recs = []
    for i, r in enumerate(rows):
        def cell(idx):
            return str(r[idx]).strip() if idx < len(r) else ""
        if not cell(0):
            continue
        rec = {"行": 3 + i}
        rec.update({name: cell(j) for j, name in enumerate(_ORDER_HEADERS)})
        recs.append(rec)
    df = pd.DataFrame(recs)
    if not df.empty:
        df["数量"] = pd.to_numeric(df["数量"], errors="coerce")
    return df


def _append_order(brand: str, sku: str, product: str, size: str, lot: str,
                  qty: int, order_date: str, eta: str, comment: str) -> int:
    """新規発注を1行 append する。IDは既存最大+1。書込後に読み戻しverify。"""
    import urllib.parse
    from sales30d import _a1, _sheets_call, sheet_read
    from inventory_snapshot import _token
    token = _token()
    _ensure_order_tab(token)
    cur = sheet_read(token, _MASTER_SHEET, _a1(_ORDER_TAB, "A3:A"))
    ids = [int(str(v[0]).strip()) for v in cur
           if v and str(v[0]).strip().isdigit()]
    new_id = (max(ids) + 1) if ids else 1
    now = _order_now()
    row = [new_id, now, brand, sku, product, size, lot, qty,
           order_date, eta, _ORDER_STATUS[0], comment, now]
    suffix = ("/values/"
              + urllib.parse.quote(_a1(_ORDER_TAB, f"A2:{_ORDER_LAST_COL}"), safe="")
              + ":append")
    res = _sheets_call("POST", token, _MASTER_SHEET, suffix,
                       params={"valueInputOption": "RAW",
                               "insertDataOption": "INSERT_ROWS"},
                       body={"values": [row]})
    rng = res.get("updates", {}).get("updatedRange", "")
    m = re.search(r"!A(\d+)", rng)
    if not m:
        raise RuntimeError(f"append結果のレンジが解釈できない: '{rng}'"
                           "→タブを直接確認してください")
    rownum = int(m.group(1))
    back = sheet_read(token, _MASTER_SHEET,
                      _a1(_ORDER_TAB, f"A{rownum}:D{rownum}"))
    got_id = str(back[0][0]).strip() if back and back[0] else ""
    got_sku = str(back[0][3]).strip() if back and back[0] and len(back[0]) > 3 else ""
    if got_id != str(new_id) or got_sku != sku:
        raise RuntimeError(f"verify失敗: 行{rownum} が ID'{got_id}'/SKU'{got_sku}'"
                           f"（期待 '{new_id}'/'{sku}'）")
    # ID重複ガード（Codex P2）: 同時送信で同じ max+1 を計算した場合、append自体は
    # 両方成功してIDが重複する。全ID再読で重複を検知したら自行だけ再採番する。
    all_ids = [int(str(v[0]).strip())
               for v in sheet_read(token, _MASTER_SHEET, _a1(_ORDER_TAB, "A3:A"))
               if v and str(v[0]).strip().isdigit()]
    if all_ids.count(new_id) > 1:
        fixed = max(all_ids) + 1
        _sheets_call("POST", token, _MASTER_SHEET, "/values:batchUpdate",
                     body={"valueInputOption": "RAW",
                           "data": [{"range": _a1(_ORDER_TAB, f"A{rownum}"),
                                     "values": [[fixed]]}]})
        back2 = sheet_read(token, _MASTER_SHEET, _a1(_ORDER_TAB, f"A{rownum}"))
        got2 = str(back2[0][0]).strip() if back2 and back2[0] else ""
        if got2 != str(fixed):
            raise RuntimeError(f"ID再採番のverify失敗: 行{rownum} が '{got2}'"
                               f"（期待 '{fixed}'）")
        print(f"[order-add] ID重複を検知し 行{rownum} を ID{new_id}→{fixed} に再採番")
        new_id = fixed
    print(f"[order-add] 行{rownum} ID{new_id} {sku} ロット'{lot}' {qty}個 "
          f"発注{order_date} 納品予定{eta}")
    return new_id


def _save_orders(edited: pd.DataFrame, original: pd.DataFrame) -> tuple[int, list[str]]:
    """変更された編集可能セルだけを書き戻す。(保存件数, エラーリスト) を返す。"""
    from sales30d import _a1, _sheets_call, sheet_read
    from inventory_snapshot import _token

    def norm(col: str, v) -> str:
        if col == "数量":
            n = pd.to_numeric(v, errors="coerce")
            return "" if pd.isna(n) else str(int(n))
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()

    changes = []   # (行, ID, 列レター, 列名, 旧, 新)
    for idx in original.index:
        for colname, letter in _ORDER_EDITABLE.items():
            old_s = norm(colname, original.at[idx, colname])
            new_s = norm(colname, edited.at[idx, colname])
            if old_s == new_s:
                continue
            rid = str(original.at[idx, "ID"]).strip()
            if colname == "数量" and (not new_s or int(new_s) <= 0):
                return 0, [f"ID{rid}: 数量 '{new_s}' が不正（1以上の整数）"]
            if colname == "納品予定日" and new_s and not _DATE_RE.match(new_s):
                return 0, [f"ID{rid}: 納品予定日 '{new_s}' はYYYY-MM-DD形式で"]
            if colname == "ステータス" and new_s not in _ORDER_STATUS:
                return 0, [f"ID{rid}: ステータス '{new_s}' は不正"]
            changes.append((int(original.at[idx, "行"]), rid, letter,
                            colname, old_s, new_s))
    if not changes:
        return 0, []

    token = _token()
    # 楽観ロック: ID列（A列）を再読し、書込先の行に想定どおりのIDがいるか確認。
    cur = sheet_read(token, _MASTER_SHEET, _a1(_ORDER_TAB, "A1:A"))
    for rownum, rid, _, _, _, _ in changes:
        got = (str(cur[rownum - 1][0]).strip()
               if rownum - 1 < len(cur) and cur[rownum - 1] else "")
        if got != rid:
            return 0, [f"行{rownum} のIDが '{rid}' でなく '{got}'。"
                       "タブ側で行が動いた可能性→画面を再読込してやり直してください"]

    now = _order_now()
    data = [{"range": _a1(_ORDER_TAB, f"{letter}{rownum}"), "values": [[new_s]]}
            for rownum, _, letter, _, _, new_s in changes]
    for rownum in sorted({c[0] for c in changes}):   # 変更行の更新日時
        data.append({"range": _a1(_ORDER_TAB, f"{_ORDER_TS_COL}{rownum}"),
                     "values": [[now]]})
    res = _sheets_call("POST", token, _MASTER_SHEET, "/values:batchUpdate",
                       body={"valueInputOption": "RAW", "data": data})
    if res.get("totalUpdatedCells") != len(data):
        raise RuntimeError(f"更新セル数が不一致（期待{len(data)}/"
                           f"実際{res.get('totalUpdatedCells')}）。タブを直接確認してください")

    for rownum, rid, letter, colname, old_s, new_s in changes:
        back = sheet_read(token, _MASTER_SHEET, _a1(_ORDER_TAB, f"{letter}{rownum}"))
        got = str(back[0][0]).strip() if back and back[0] else ""
        if got != new_s:
            raise RuntimeError(f"verify失敗: 行{rownum} ID{rid} {colname} が "
                               f"'{got}'（期待 '{new_s}'）")
        print(f"[order-edit] 行{rownum} ID{rid} {colname}: '{old_s}' → '{new_s}'")
    return len(changes), []


st.divider()
st.subheader("📦 発注レコード（1ロット=1レコード・格納先は蓄積先スプシの専用タブ）")
if _UI_BRAND not in ("", "labo"):
    st.info("発注レコードはこの会社のSKUマスタ整備後に開放します")
elif not _MASTER_SHEET:
    st.info("SNAPSHOT_SHEET_ID が未設定のため、発注レコードはこの環境では無効です")
else:
    if st.session_state.get("order_saved_msg"):
        st.success(st.session_state.pop("order_saved_msg"))
    try:
        orders_df = _load_orders()
        order_master = _load_master()
    except Exception as e:  # noqa: BLE001 — fail-closed: 読めないなら入力させない
        st.error(f"発注レコードの読み込みに失敗: {type(e).__name__}: {e}")
        st.stop()

    # サマリ（未納=対応済以外。納品予定日超過=遅延）
    today_str = pd.Timestamp.now(tz="Asia/Tokyo").strftime("%Y-%m-%d")
    if orders_df.empty:
        open_df = orders_df
        n_late = 0
    else:
        open_df = orders_df[orders_df["ステータス"] != _ORDER_DONE]
        _eta = open_df["納品予定日"].astype(str)
        n_late = int((_eta.str.match(_DATE_RE.pattern) & (_eta < today_str)).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("未納レコード", len(open_df))
    c2.metric("🔴 納品予定日超過（遅延）", n_late)
    c3.metric("未納合計数量", 0 if open_df.empty
              else int(open_df["数量"].fillna(0).sum()))

    with st.expander("➕ 新規発注を記録", expanded=orders_df.empty):
        opts = {f"{r['SKU']} ― {r['商品名']} {r['サイズ']}": r
                for _, r in order_master.iterrows()}
        if not opts:
            st.info("SKUマスタが空のため入力できません（🧩SKUマスタdraft を確認）")
        else:
            with st.form("order_add", clear_on_submit=True):
                sel = st.selectbox("SKU（🧩SKUマスタdraft から選択）", list(opts))
                f1, f2, f3, f4 = st.columns(4)
                lot = f1.text_input("ロットNo（何ロット目）")
                qty = f2.number_input("数量", min_value=1, step=1, value=1)
                odate = f3.date_input("発注日", format="YYYY-MM-DD")
                eta = f4.date_input("納品予定日", format="YYYY-MM-DD")
                comment = st.text_input("コメント（任意）")
                if st.form_submit_button("📦 発注を記録"):
                    if eta < odate:
                        st.error("納品予定日が発注日より前です")
                    else:
                        row = opts[sel]
                        try:
                            rid = _append_order(
                                _UI_BRAND or "labo", str(row["SKU"]),
                                str(row["商品名"]), str(row["サイズ"]), lot.strip(),
                                int(qty), odate.strftime("%Y-%m-%d"),
                                eta.strftime("%Y-%m-%d"), comment.strip())
                        except Exception as e:  # noqa: BLE001 — 失敗を画面に明示
                            st.error(f"記録に失敗: {type(e).__name__}: {e}")
                            st.stop()
                        st.session_state["order_saved_msg"] = \
                            f"ID{rid} を記録しました（ステータス=発注済）"
                        _load_orders.clear()
                        st.rerun()

    if orders_df.empty:
        st.info("発注レコードはまだありません（上のフォームから記録できます）")
    else:
        # 未納→納品予定日昇順で表示（危ないものが上）。編集は4列のみ。
        view = orders_df.copy()
        view["遅延"] = ""
        late_mask = ((view["ステータス"] != _ORDER_DONE)
                     & view["納品予定日"].astype(str).str.match(_DATE_RE.pattern)
                     & (view["納品予定日"].astype(str) < today_str))
        view.loc[late_mask, "遅延"] = "🔴 遅延"
        view = view.sort_values(
            by=["ステータス", "納品予定日"],
            key=lambda s: (s.map(lambda v: 1 if v == _ORDER_DONE else 0)
                           if s.name == "ステータス" else s))
        show_cols = ["遅延", "ID", "SKU", "商品名", "サイズ", "ロットNo", "数量",
                     "発注日", "納品予定日", "ステータス", "コメント",
                     "記録日時", "更新日時", "行"]
        view = view[[c for c in show_cols if c in view.columns]]
        _orev = st.session_state.get("order_rev", 0)
        edited_orders = st.data_editor(
            view, hide_index=True, key=f"order_editor_{_orev}", num_rows="fixed",
            disabled=[c for c in view.columns if c not in _ORDER_EDITABLE],
            column_config={
                "行": None,
                "数量": st.column_config.NumberColumn(min_value=1, step=1,
                                                      format="%d"),
                "ステータス": st.column_config.SelectboxColumn(
                    options=_ORDER_STATUS, required=True),
            },
            height=min(560, 60 + 36 * max(1, len(view))))
        st.caption(f"全{len(view)}件（未納→納品予定日順）。編集できるのは "
                   "**数量・納品予定日(YYYY-MM-DD)・ステータス・コメント** の4列。"
                   f"ステータスは {' → '.join(_ORDER_STATUS)} の5段階"
                   "（対応済=完納で未納集計から外れる）。")
        if st.button("💾 変更を保存", key="order_save"):
            try:
                n, errs = _save_orders(edited_orders, view)
            except Exception as e:  # noqa: BLE001 — 部分書込の可能性も画面に明示
                st.error(f"保存に失敗（部分的に書き込まれた可能性あり。"
                         f"タブを直接確認してください）: {type(e).__name__}: {e}")
                st.stop()
            if errs:
                st.error("保存を中止しました: " + " ／ ".join(errs))
            elif n == 0:
                st.info("変更はありません")
            else:
                st.session_state["order_saved_msg"] = f"{n} セルを保存しました"
                _load_orders.clear()
                st.session_state["order_rev"] = _orev + 1
                st.rerun()

        with st.expander("📊 SKU別 未納合計（依頼済数量の自動算出）"):
            if open_df.empty:
                st.info("未納レコードはありません")
            else:
                agg = (open_df.assign(数量=open_df["数量"].fillna(0))
                       .groupby(["SKU", "商品名", "サイズ"], as_index=False)
                       .agg(未納レコード=("ID", "count"), 未納合計数量=("数量", "sum")))
                st.dataframe(_style_commas(agg), use_container_width=True,
                             hide_index=True)
                st.caption("未納 = ステータスが「対応済」以外のレコード。"
                           "v2の手入力『依頼済数量』の代替となる自動算出値です"
                           "（両者の突合はStep2運用開始後に実施）。")


st.divider()
with st.expander("⚙️ SKU別 発注LT設定（🧩SKUマスタdraft を直接編集・翌朝のbotから反映）"):
    # 🧩SKUマスタdraft は現状 labo 147SKU の単一タブ（ブランド列なし）。
    # nature/qiera 横展時はマスタを会社別に分けてから開放する。
    if _UI_BRAND not in ("", "labo"):
        st.info("LT編集はこの会社のSKUマスタ整備後に開放します")
    elif not _MASTER_SHEET:
        st.info("SNAPSHOT_SHEET_ID が未設定のため、LT編集はこの環境では無効です")
    else:
        if st.session_state.get("lt_saved_msg"):
            st.success(st.session_state.pop("lt_saved_msg"))
        try:
            master_df = _load_master()
        except Exception as e:  # noqa: BLE001 — fail-closed: 読めないなら編集させない
            st.error(f"SKUマスタの読み込みに失敗: {type(e).__name__}: {e}")
            st.stop()
        st.caption(f"{len(master_df)} SKU ／ 編集できるのは **LT(日)** 列のみ"
                   f"（{_LT_MIN}〜{_LT_MAX}日・空欄=既定LT 135日）。"
                   "保存すると翌朝の在庫アラートbotから新LTで判定されます。"
                   "LT(ヶ月)・LT根拠列は書き換えません（根拠の正本はマスタタブ側）。")
        # 保存成功のたびに key を回し、data_editor の編集差分を確実にリセットする
        _rev = st.session_state.get("lt_rev", 0)
        edited_df = st.data_editor(
            master_df, hide_index=True, key=f"lt_editor_{_rev}", num_rows="fixed",
            disabled=[c for c in master_df.columns if c != "LT(日)"],
            column_config={
                "行": None,   # シート行番号は内部管理用（非表示）
                "LT(日)": st.column_config.NumberColumn(
                    min_value=_LT_MIN, max_value=_LT_MAX, step=1, format="%d"),
            },
            height=min(560, 60 + 36 * max(1, len(master_df))))
        if st.button("💾 変更したLTを保存", key="lt_save"):
            try:
                n, errs = _save_lt(edited_df, master_df)
            except Exception as e:  # noqa: BLE001 — 部分書込の可能性も画面に明示する
                st.error(f"保存に失敗（部分的に書き込まれた可能性あり。"
                         f"マスタタブを直接確認してください）: {type(e).__name__}: {e}")
                st.stop()
            if errs:
                st.error("保存を中止しました: " + " ／ ".join(errs))
            elif n == 0:
                st.info("変更はありません")
            else:
                # rerun で画面が消えるため、成功メッセージは次回描画で出す
                st.session_state["lt_saved_msg"] = f"{n} 件のLTを保存しました（翌朝のbotから反映）"
                _load_master.clear()
                st.session_state["lt_rev"] = _rev + 1
                st.rerun()
