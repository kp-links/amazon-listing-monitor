# -*- coding: utf-8 -*-
"""分析結果 → Chatwork 装飾メッセージ。

Chatwork は Markdown を描画しない（**太字**/## は記号露出）。装飾は
[info][title]…[/title]…[/info] と [hr]、絵文字で行う。詳細はシートに置き、
Chatwork は「区分・最優先・推奨アクション」の要点に絞る。
"""
from __future__ import annotations
from datetime import datetime


# 区分メタ（表示順）。emoji はセクション見出し用。
KIND_META = [
    ("ORDER", "🏭", "製造発注"),
    ("FBA", "🔴", "FBA納品"),
    ("COCO", "🟡", "ココドット補充"),
    ("TREND", "🔺", "加速注意（トレンド変化）"),
    ("SLOW", "🔻", "過剰在庫・発注見送り検討"),
]
SEV_RANK = {"🚨": 0, "🔴": 1, "🟡": 2, "🔺": 3, "🔻": 4}
MAX_ITEMS_PER_SECTION = 15


def _i(v) -> str:
    if v is None:
        return "-"
    return f"{int(round(v)):,}"


def _f(v, nd=1) -> str:
    if v is None:
        return "-"
    return f"{v:,.{nd}f}"


def _d(v) -> str:
    if not v:
        return "-"
    return v.strftime("%m/%d")


def _weekday_jp(d: datetime) -> str:
    return ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]


def _accel_mark(accel, hot, cold) -> str:
    if accel is None:
        return ""
    if accel >= hot:
        return f" ⤴×{accel:.1f}"
    if accel <= cold:
        return " ⤵"
    return ""


def _item_lines(r: dict, kind: str, th) -> list[str]:
    name = f"{r['product']} {r['size']}".strip()
    # この区分のトリガー（action/reason）
    trig = next((t for t in r["triggers"] if t["kind"] == kind), None)
    sev = trig["sev"] if trig else r["primary_sev"]
    status = "✅対応済" if r["done"] else ""
    head = f"◆ {name}（{r['sku']}） {sev}{('  ' + status) if status else ''}"

    # 在庫＋日販＋切予想の要約（Amazon中心、加速マーク付き）
    amk = _accel_mark(r.get("accel_a"), th.accel_hot, th.accel_cold)
    cmk = _accel_mark(r.get("accel_c"), th.accel_hot, th.accel_cold)
    parts = [f"在庫計{_i(r['stock_total'])}（FBA{_i(r['stock_fba'])}/ココ{_i(r['stock_coco'])}"]
    if r.get("requested_qty"):
        parts[0] += f"/依頼{_i(r['requested_qty'])}"
    parts[0] += "）"
    sales = (f"日販A {_f(r.get('v30a'))}→7d {_f(r.get('v7a'))}{amk}")
    if r.get("v30c"):
        sales += f"／ココ {_f(r.get('v30c'))}→7d {_f(r.get('v7c'))}{cmk}"
    line2 = "　" + parts[0] + "　" + sales
    line3 = (f"　切予想 FBA {_d(r.get('stockout_fba'))}・総 {_d(r.get('stockout_total'))}"
             f"／ロット {r.get('lot_current') or '-'}→{r.get('lot_ordered') or '-'}")

    lines = [head, line2, line3]
    if trig:
        lines.append(f"　▶ {trig['action']}")
    return lines


def _section(kind: str, emoji: str, title: str, items: list[dict], th) -> str:
    n = len(items)
    out = [f"[info][title]{emoji} {title}（{n}件）[/title]"]
    if n == 0:
        out.append("該当なし ✅")
        out.append("[/info]")
        return "\n".join(out)

    items = sorted(items, key=lambda r: (
        SEV_RANK.get(_sev_for(r, kind), 9),
        r.get("days_total") if r.get("days_total") is not None else 1e9,
    ))
    shown = items[:MAX_ITEMS_PER_SECTION]
    for i, r in enumerate(shown):
        if i:
            out.append("")
        out.extend(_item_lines(r, kind, th))
    if n > len(shown):
        out.append("")
        out.append(f"…他 {n - len(shown)} 件はシート参照")
    out.append("[/info]")
    return "\n".join(out)


def _sev_for(r: dict, kind: str) -> str:
    t = next((t for t in r["triggers"] if t["kind"] == kind), None)
    return t["sev"] if t else r["primary_sev"]


def build_message(result: dict) -> str:
    brand = result["brand"]
    today = result["today"]
    th = brand.thresholds
    results = result["results"]

    # 区分ごとに振り分け（1SKUが複数区分に出ることを許容）
    by_kind: dict[str, list] = {k: [] for k, _, _ in KIND_META}
    for r in results:
        for t in r["triggers"]:
            if t["kind"] in by_kind:
                by_kind[t["kind"]].append(r)

    header_date = f"{today.year}/{today.month:02d}/{today.day:02d}（{_weekday_jp(today)}）"
    counts = "／".join(
        f"{emoji}{title.split('（')[0]} {len(by_kind[k])}"
        for k, emoji, title in KIND_META if by_kind[k]
    ) or "要対応なし ✅"

    blocks: list[str] = []
    if brand.chatwork_mentions:
        blocks.append(brand.chatwork_mentions)
        blocks.append("")
    blocks.append(f"📦 在庫トレンド＆発注アラート ／ {brand.name} ／ {header_date}")
    blocks.append(f"全 {result['total_skus']} SKU点検 → {counts}")
    blocks.append(
        f"基準: 発注点=日販×(LT{th.lead_time_days}+安全{th.safety_days})日"
        f"／FBA<{th.fba_low_days}日／加速=7日が30日の{th.accel_hot}倍超"
    )
    blocks.append("")

    for k, emoji, title in KIND_META:
        if by_kind[k]:
            blocks.append(_section(k, emoji, title, by_kind[k], th))
            blocks.append("")

    url = result.get("sheet_url", "")
    if url:
        blocks.append(f"🔎 推奨事項シート: {url}")
    return "\n".join(blocks).rstrip() + "\n"
