# -*- coding: utf-8 -*-
"""Inventory Pulse — 最小UI（Phase3 先行版）。

GCS FUSE 上の在庫スナップショット Parquet を読み、
①今日の在庫アラート ②在庫推移（行=SKU × 列=日付、時間軸は横）を表示する。

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
    st.dataframe(
        alerts[["bot優先度", "bot区分", "商品名", "サイズ", "FBA在庫", "ココ在庫",
                "総在庫", "botFBA在庫日数", "bot総在庫日数", "bot在庫切れ予想(総)",
                "bot推奨アクション"]],
        use_container_width=True, hide_index=True, height=min(420, 60 + 36 * len(alerts)))

# ── ②在庫推移（行=SKU × 列=日付）────────────────────────────────────────────
st.subheader("② 在庫推移（列=日付・新しい日付が右）")
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
st.dataframe(pv, use_container_width=True, height=560)
st.caption("並び順は最新値の昇順（少ない・危ないものが上）。Δ期間 = 最新 − 蓄積初日。"
           "蓄積が貯まるほど推移の解像度が上がります。")
