# -*- coding: utf-8 -*-
"""Inventory Pulse — 最小UI（Phase3 先行版）。

GCS FUSE 上の在庫スナップショット Parquet を読み、
①今日の在庫アラート ②全SKU一覧（シート同順・カンマ区切り）
③在庫推移（行=SKU × 列=日付、時間軸は横）を表示する。

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
for _c in ("シート販売数(総)", "シート販売数(Amazon)", "シート販売数(ココ)"):
    _BLOCK_SHADES[_c] = (_TINT_SALES, "#dbeedd")          # 販売=薄緑
for _c in ("シート在庫日数(総)", "bot総在庫日数", "bot発注点ROP"):
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


def _apply_days_alert(sty, cols):
    """在庫日数セルの警告色（<120日=赤/<180日=橙。閾値は発注sevと同じ既定値）。"""
    def _cell(v):
        if not _isnum(v):
            return ""
        if v < 120:
            return _bg(_DAYS_URGENT, "font-weight:600")
        if v < 180:
            return _bg(_DAYS_WARN)
        return ""
    subset = [c for c in ("シート在庫日数(総)", "bot総在庫日数") if c in cols]
    return _cellmap(sty, _cell, subset) if subset else sty


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


df = load()
if df.empty:
    st.warning(f"スナップショットがまだ無い（{_ROOT}）")
    st.stop()

# 日付は書き込み側（inventory_snapshot.py）が常に %Y-%m-%d で出すため辞書順=時系列。
if st.sidebar.button("🔄 最新データに更新"):
    load.clear()
    st.rerun()
brands = sorted(df["ブランド"].unique())
brand = st.sidebar.selectbox("ブランド", brands, index=0)
b = df[df["ブランド"] == brand]
dates = sorted(b["日付"].unique())
latest = dates[-1]
today = b[b["日付"] == latest]

# ── ヘッダ＋判断サマリ ──────────────────────────────────────────────────────
st.title("📦 Inventory Pulse")
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

# ── ①今日の在庫アラート ─────────────────────────────────────────────────────
st.subheader("① 今日の在庫アラート（bot算出・優先度順）")
if alerts.empty:
    st.success("フラグの立っているSKUはありません")
else:
    _acols = ["bot優先度", "bot区分", "商品名", "サイズ", "FBA在庫", "ココ在庫",
              "総在庫", "botFBA在庫日数", "bot総在庫日数", "bot在庫切れ予想(総)",
              "bot推奨アクション"]
    st.dataframe(
        _apply_sev_rows(_style_commas(alerts[_acols]), "bot優先度"),
        use_container_width=True, hide_index=True, height=min(420, 60 + 36 * len(alerts)))

# ── ②全SKU一覧（今日・シートと同じ並び）──────────────────────────────────────
st.subheader("② 全SKU一覧（最新スナップショット・シートと同順）")
_ALL_COLS = ["商品名", "サイズ", "ASIN", "SKU",
             "総在庫", "FBA在庫", "ココ在庫",
             "マイクロアルジェAmazon在庫", "マイクロアルジェ楽天在庫",
             "自社在庫", "依頼済数量",
             "シート販売数(総)", "シート販売数(Amazon)", "シート販売数(ココ)",
             "シート在庫日数(総)", "bot総在庫日数", "bot発注点ROP",
             "bot優先度", "bot推奨アクション"]
all_rows = today[[c for c in _ALL_COLS if c in today.columns]]
q = st.text_input("絞り込み（商品名/サイズ/SKU/ASIN 部分一致）", "")
if q.strip():
    mask = pd.Series(False, index=all_rows.index)
    for c in ("商品名", "サイズ", "SKU", "ASIN"):
        if c in all_rows.columns:
            mask |= all_rows[c].astype(str).str.contains(
                q.strip(), case=False, na=False, regex=False)
    all_rows = all_rows[mask]
_sty = _style_commas(all_rows)
_sty = _apply_product_bands(_sty, all_rows)
_sty = _apply_days_alert(_sty, all_rows.columns)
if "bot優先度" in all_rows.columns:
    _sty = _cellmap(_sty, lambda v: _bg(_SEV_BG[str(v).strip()])
                    if str(v).strip() in _SEV_BG else "", ["bot優先度"])
st.dataframe(_sty, use_container_width=True, hide_index=True,
             column_config=_pin_cols(["商品名", "サイズ"]),
             height=min(700, 60 + 36 * max(1, len(all_rows))))
st.caption(f"{len(all_rows)} SKU 表示（行順は在庫管理シートと同じ・商品ごとに明暗の縞＋境界線）。"
           "色: 🟦在庫 🟩販売 🟧在庫日数・bot指標 ／ 在庫日数セル 赤=120日未満・橙=180日未満")

# ── ③在庫推移（行=SKU × 列=日付）────────────────────────────────────────────
st.subheader("③ 在庫推移（列=日付・新しい日付が右）")
metric = st.selectbox(
    "指標", ["FBA在庫", "総在庫", "ココ在庫", "シート在庫日数(総)", "シート在庫日数(Amazon)",
             "シート販売数(Amazon)", "botFBA在庫日数"], index=0)
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
_psty = _style_commas(pv_flat)
_psty = _apply_heat(_psty, pv_flat, date_cols)
if "Δ期間" in pv_flat.columns:
    _psty = _apply_delta(_psty, "Δ期間")
st.dataframe(_psty, use_container_width=True, hide_index=True,
             column_config=_pin_cols(["商品名", "サイズ"]), height=560)
st.caption("並び順は最新値の昇順（少ない・危ないものが上）。Δ期間 = 最新 − 蓄積初日"
           "（薄赤=減少・薄緑=増加）。濃淡=値の大小（表全体で正規化）。"
           "蓄積が貯まるほど推移の解像度が上がります。")
