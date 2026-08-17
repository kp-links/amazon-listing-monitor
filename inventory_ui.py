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
    """1ブランド分の画面（サマリ→①アラート→②全SKU→③推移）。widget keyはブランド別。"""
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

    # ②全SKU一覧（今日・シートと同じ並び）
    st.subheader("② 全SKU一覧（最新スナップショット・シートと同順）")
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

    # ③在庫推移（行=SKU × 列=日付）
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
    st.rerun()

st.title("📦 Inventory Pulse")
present = set(df["ブランド"].unique())
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


st.divider()
with st.expander("⚙️ SKU別 発注LT設定（🧩SKUマスタdraft を直接編集・翌朝のbotから反映）"):
    if not _MASTER_SHEET:
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
