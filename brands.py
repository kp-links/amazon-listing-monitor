# -*- coding: utf-8 -*-
"""在庫アラートのブランド別設定としきい値。

横展開（ナチュレ→悩み解決ラボ→Qiera）を1コードで回すための設定レジストリ。
ブランド差は (1) 在庫管理シートの列レイアウト (2) 各タブの gid
(3) Chatwork メンション (4) シートURL のみ。秘密情報（シートID/Chatworkルーム/
SP-API refresh token）は環境変数で注入し、ここには置かない。

⚠️ フォーマット列マップはブランドごとに実シートで実地検証してから追加すること。
   悩み解決ラボはマイクロアルジェ仕入の在庫列が増えるためナチュレと列順が違う。
   （2026-06-26 ナチュレ検証済。2026-06-29 labo[フォーマットv2]/qiera[フォーマット]
     を実シート実地検証して追加。labo=マイクロアルジェ列(H,I)増設で自社以降がズレ、
     qiera=ラベル列なしで sku_comment が AM。ココ7d/30d は labo=RSL売上状況/
     qiera=NE売上状況 タブ。両者とも列構造は同一[sku=D,7d=E,30d=F,データ3行目開始]。）
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Thresholds:
    """発注・納品・トレンド判定のしきい値。原則全ブランド共通。"""
    lead_time_days: int = 135          # 製造依頼→納品。一律4.5ヶ月（滝谷さん指定 2026-06-26）
    safety_days: int = 30              # 発注点(ROP・参考値)に上乗せする安全在庫日数
    order_urgent_days: int = 120       # 製品計の物理在庫がこれ未満→🚨至急製造発注（滝谷さん指定）
    order_warn_days: int = 180         # これ未満→🔴製造発注を検討（滝谷さん指定）
    accel_hot: float = 1.5             # 7日日販 / 30日日販 がこれ以上で「加速」
    accel_cold: float = 0.6            # これ以下で「減速」
    trend_min_30d_units: int = 15      # 30日販売がこれ未満のSKUは加速判定しない（少数ノイズ除外）
    fba_low_days: int = 30             # FBA在庫日数がこれ未満→FBA納品アラート
    fba_fast_days: int = 45            # 高速回転SKUに適用する拡張しきい値
    fba_urgent_days: int = 14          # これ未満は🚨（FBA即枯渇）
    fba_target_days: int = 45          # FBA納品の推奨数量を算出する目標カバー日数
    coco_low_days: int = 30            # ココドット在庫日数がこれ未満→ココ補充
    overstock_days: int = 365          # 総在庫日数がこれ超＋減速→過剰フラグ


# フォーマットタブの列マップ（0始まり: A=0, B=1 ...）。実シート地検証済の値。
NATURE_FORMAT_COLS = {
    "product": 0, "size": 1, "asin": 2, "sku": 3,
    "stock_total": 4, "stock_fba": 5, "stock_coco": 6, "stock_own": 7,
    "requested_qty": 8,
    "sales_total": 9, "sales_amazon": 10, "sales_coco": 11,
    "days_total": 12, "days_amazon": 13, "days_coco": 14,
    "stockout_total": 15, "stockout_amazon": 16, "stockout_coco": 17,
    "delivery_deadline": 18, "repeat_order_deadline": 19,
    "alert_order": 20, "alert_fba": 21, "alert_coco": 22, "alert_done": 23,
    "lot_current": 24, "lot_ordered": 25, "delivery_plan": 26, "order_lot": 27,
    "new_lot_assign": 28, "aerologi": 29, "label": 30, "set_assembly": 31,
    "order_consider": 32,
    "amazon_todo_date": 33, "amazon_todo": 34, "amazon_todo_qty": 35,
    "coco_todo_date": 36, "coco_todo": 37, "coco_todo_qty": 38,
    "sku_comment": 39,
}
# NE売上状況タブ（ココドット/NEチャネルの 7日・30日 販売数）の列マップ。
# ※ Amazon ではない。Amazon の 7d/30d は SP-API から取得する。
NATURE_NE_COLS = {"sku": 3, "coco_7d": 4, "coco_30d": 5}

# 悩み解決ラボ「フォーマットv2」(gid=1674969562)。マイクロアルジェ在庫(H,I)が増設
# され、ナチュレ比で自社(J)以降が右に2列ずれる。直近販売数=L(総)/M(Amazon)/N(ココ)。
LABO_FORMAT_COLS = {
    "product": 0, "size": 1, "asin": 2, "sku": 3,
    "stock_total": 4, "stock_fba": 5, "stock_coco": 6,
    # H=マイクロアルジェAmazon在庫 / I=マイクロアルジェ楽天在庫（在庫日数計算には不使用）
    "stock_own": 9,
    "requested_qty": 10,
    "sales_total": 11, "sales_amazon": 12, "sales_coco": 13,
    "days_total": 14, "days_amazon": 15, "days_coco": 16,
    "stockout_total": 17, "stockout_amazon": 18, "stockout_coco": 19,
    "delivery_deadline": 20, "repeat_order_deadline": 21,
    "alert_order": 22, "alert_fba": 23, "alert_coco": 24, "alert_done": 25,
    "lot_current": 26, "lot_ordered": 27, "delivery_plan": 28, "order_lot": 29,
    "new_lot_assign": 30, "aerologi": 31, "set_assembly": 32, "order_consider": 33,
    "amazon_todo_date": 34, "amazon_todo": 35, "amazon_todo_qty": 36,
    "coco_todo_date": 37, "coco_todo": 38, "coco_todo_qty": 39,
    "sku_comment": 40,
}
# Qiera「フォーマット」(gid=1674969562)。ナチュレ同型だが「ラベル」列が無く、
# 新ロット振り分け以降が1列詰まる。sku_comment は AM(38)。
QIERA_FORMAT_COLS = {
    "product": 0, "size": 1, "asin": 2, "sku": 3,
    "stock_total": 4, "stock_fba": 5, "stock_coco": 6, "stock_own": 7,
    "requested_qty": 8,
    "sales_total": 9, "sales_amazon": 10, "sales_coco": 11,
    "days_total": 12, "days_amazon": 13, "days_coco": 14,
    "stockout_total": 15, "stockout_amazon": 16, "stockout_coco": 17,
    "delivery_deadline": 18, "repeat_order_deadline": 19,
    "alert_order": 20, "alert_fba": 21, "alert_coco": 22, "alert_done": 23,
    "lot_current": 24, "lot_ordered": 25, "delivery_plan": 26, "order_lot": 27,
    "new_lot_assign": 28, "aerologi": 29, "set_assembly": 30, "order_consider": 31,
    "amazon_todo_date": 32, "amazon_todo": 33, "amazon_todo_qty": 34,
    "coco_todo_date": 35, "coco_todo": 36, "coco_todo_qty": 37,
    "sku_comment": 38,
}
# ココ7d/30d タブ（labo=RSL売上状況 / qiera=NE売上状況）。列構造はナチュレと同一。
LABO_NE_COLS = {"sku": 3, "coco_7d": 4, "coco_30d": 5}
QIERA_NE_COLS = {"sku": 3, "coco_7d": 4, "coco_30d": 5}


@dataclass(frozen=True)
class Brand:
    key: str                       # 内部キー（環境変数 BRAND の値）
    name: str                      # 表示名
    format_gid: int                # フォーマットタブの gid
    format_cols: dict              # フォーマット列マップ
    format_data_start_row: int     # データ開始行（1始まり）
    ne_gid: int                    # NE売上状況タブ gid（ココ 7d/30d）
    ne_cols: dict                  # NE列マップ
    ne_data_start_row: int
    rec_tab_title: str             # 推奨事項（bot専用出力）タブ名
    chatwork_mentions: str         # Chatwork メンション文字列（[To:...]）
    thresholds: Thresholds = field(default_factory=Thresholds)


# シートURLは実行時に SALES_SHEET_ID（secret）から組み立てる（IDをコードに残さない）。
def sheet_url(sheet_id: str, gid: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit?gid={gid}"


BRANDS: dict[str, Brand] = {
    "nature": Brand(
        key="nature",
        name="ナチュレ（LUBEE）",
        format_gid=1674969562,
        format_cols=NATURE_FORMAT_COLS,
        format_data_start_row=6,
        ne_gid=491010538,
        ne_cols=NATURE_NE_COLS,
        ne_data_start_row=3,
        rec_tab_title="📊在庫アラート(bot)",
        # ナチュレ在庫の担当メンション。誤爆防止のため確定するまで暫定で空。
        # 実運用前に [To:id]名前さん 形式で設定する。
        chatwork_mentions="",
    ),
    "labo": Brand(
        key="labo",
        name="悩み解決ラボ",
        format_gid=1674969562,          # フォーマットv2
        format_cols=LABO_FORMAT_COLS,
        format_data_start_row=6,
        ne_gid=491010538,               # RSL売上状況（ココ7d/30d）
        ne_cols=LABO_NE_COLS,
        ne_data_start_row=3,
        rec_tab_title="📊在庫アラート(bot)",
        chatwork_mentions="",           # 配信先=【サドナレ】業務メンバーチャット
    ),
    "qiera": Brand(
        key="qiera",
        name="Qiera",
        format_gid=1674969562,          # フォーマット
        format_cols=QIERA_FORMAT_COLS,
        format_data_start_row=6,
        ne_gid=491010538,               # NE売上状況（ココ7d/30d）
        ne_cols=QIERA_NE_COLS,
        ne_data_start_row=3,
        rec_tab_title="📊在庫アラート(bot)",
        chatwork_mentions="",           # 配信先=【Qiera】社内物流チャット
    ),
}


def get_brand(key: str) -> Brand:
    if key not in BRANDS:
        raise SystemExit(
            f"[FATAL] 未知のブランド '{key}'。対応: {sorted(BRANDS)}")
    return BRANDS[key]
