# -*- coding: utf-8 -*-
"""Inventory Pulse Phase1 — 在庫スナップショットの日次蓄積。

在庫管理シート（フォーマット / ココ販売数 / 📊在庫アラート(bot)）をリードオンリーで
読み、SKU×日付の1行を専用スプレッドシートの追記専用タブに積む。

■ なぜ必要か
  現行運用は「過去分」タブを手動コピーして履歴を残す方式だったが 2025-06-03 で停止し、
  在庫推移の履歴が存在しない。在庫の過去値は後から取り直せない（SP-API も NE も
  「今の在庫」しか返さない）ため、蓄積は1日でも早く始めるほど価値が出る。

■ 設計上の約束
  * 読み取り元シート（SALES_SHEET_ID）には一切書き込まない。蓄積先は必ず別スプレッド
    シート（SNAPSHOT_SHEET_ID）。同一IDを指定した場合は起動時に落とす
  * 蓄積先も追記のみ。既存行の更新・削除はしない
  * 冪等性は (日付, ブランド, SKU) 単位。前回が途中で落ちて一部SKUだけ入った場合でも、
    次回に欠けたSKUだけを補完できる
  * 指標の再計算はしない。ここで持つのは「その日シートに何が書かれていたか」の生値。
    計算の一本化は Phase2（inventory_alert の定義を正本に昇格）で行う

■ 列マップの出どころ
  brands.py の BRANDS を import して使う。brands.py にある列をここで再定義しないこと
  （二重管理した時点で必ず片方が腐る）。brands.py に載っていない列だけを
  EXTRA_STOCK_COLS で補い、実行時にヘッダ文字列で位置を検証する。

使い方:
    python inventory_snapshot.py --brand labo
    python inventory_snapshot.py --brand labo --dry-run
    python inventory_snapshot.py --brand labo --show-sample   # dry-run時に先頭行を表示

環境変数:
    GOOGLE_SA_JSON           サービスアカウントJSON（クラウド）
    GOOGLE_CREDENTIALS_PATH  同ファイルパス（ローカル実行時の代替）
    SALES_SHEET_ID           読み取り元＝在庫管理シートのID
    SNAPSHOT_SHEET_ID        蓄積先スプレッドシートID（必須・読み取り元と別であること）
    BRAND                    --brand 未指定時のフォールバック
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import brands as brands_mod
from inventory_alert import resolve_title, sheets_token
from sales30d import _a1, _sheets_call, sheet_read

JST = timezone(timedelta(hours=9))

SNAPSHOT_TAB = "📈在庫スナップショット(bot)"
MARKER = "⚠️ bot自動生成・追記専用（この表は編集しないでください。行や列を消すと履歴が失われます）"

# 追記する列。順序を変えると過去行と食い違うため、**末尾追加以外の変更は禁止**。
# 起動時に既存ヘッダとの prefix 一致を検証しており、順序を崩すと実行が止まる。
SNAPSHOT_HEADERS = [
    "日付", "シート更新日", "ブランド",
    "商品名", "サイズ", "ASIN", "SKU",
    "総在庫", "FBA在庫", "ココ在庫",
    "マイクロアルジェAmazon在庫", "マイクロアルジェ楽天在庫",
    "自社在庫", "依頼済数量",
    "シート販売数(総)", "シート販売数(Amazon)", "シート販売数(ココ)",
    "NEココ7d", "NEココ30d",
    "シート在庫日数(総)", "シート在庫日数(Amazon)", "シート在庫日数(ココ)",
    "シート在庫切れ(総)", "シート在庫切れ(Amazon)", "シート在庫切れ(ココ)",
    "発注アラート", "FBA納品アラート", "ココ納品アラート", "対応済",
    "現ロット", "発注済ロット", "発注ロット数",
    "bot優先度", "bot区分",
    "botA日販7d", "botA日販30d", "botA加速",
    "botコ日販7d", "botコ日販30d", "botコ加速",
    "botFBA在庫日数", "bot総在庫日数", "bot在庫切れ予想(総)", "bot発注点ROP",
    "bot推奨アクション",
    "取得時刻JST",
]
SKU_AT = SNAPSHOT_HEADERS.index("SKU")          # 既存行の突合キー位置
BRAND_AT = SNAPSHOT_HEADERS.index("ブランド")


def _col_letter(n: int) -> str:
    """1始まりの列番号 → A1 表記の列文字（46 → 'AT'）。"""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


LAST_COL = _col_letter(len(SNAPSHOT_HEADERS))

# brands.py の列マップに無い、ブランド固有の追加在庫列（0始まり）。
# labo のフォーマットv2 は H=マイクロアルジェAmazon在庫 / I=マイクロアルジェ楽天在庫。
# 在庫日数の計算には使われないため brands.py には載っていない（brands.py:66 のコメント）が、
# 総在庫の過半を占めるSKUがあり履歴としては必須。列ズレ検知のため、
# ヘッダ行に EXPECT の文字列が含まれるかを実行時に検証する。
EXTRA_STOCK_COLS: dict[str, dict] = {
    "labo": {"micro_amazon": 7, "micro_rakuten": 8},
}
EXTRA_STOCK_EXPECT = {"micro_amazon": "マイクロアルジェ", "micro_rakuten": "マイクロアルジェ"}

# 📊在庫アラート(bot) タブから引き継ぐ列（bot側の項目名 → 本表の列名）。
# 項目名で引くので、bot 側の列順が変わっても壊れない。
BOT_FIELDS = [
    ("優先度", "bot優先度"),
    ("区分", "bot区分"),
    ("A日販7d", "botA日販7d"),
    ("A日販30d", "botA日販30d"),
    ("A加速", "botA加速"),
    ("コ日販7d", "botコ日販7d"),
    ("コ日販30d", "botコ日販30d"),
    ("コ加速", "botコ加速"),
    ("FBA在庫日数", "botFBA在庫日数"),
    ("総在庫日数", "bot総在庫日数"),
    ("在庫切れ予想(総)", "bot在庫切れ予想(総)"),
    ("発注点ROP", "bot発注点ROP"),
    ("推奨アクション", "bot推奨アクション"),
]


def _env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or v == "":
        raise SystemExit(f"[FATAL] 環境変数 {name} が未設定")
    return v


def _status_of(e: Exception) -> int | None:
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None) if resp is not None else None


def _safe_err(e: Exception) -> str:
    """例外を要約する。requests の例外文には URL（＝スプレッドシートID）が載るため、
    Actions のログに素で出さない（機密情報漏洩防止）。

    Google API の error.status（PERMISSION_DENIED / SERVICE_DISABLED 等の列挙値）は
    切り分けに必須なので載せる。message は ID を含みうるので載せない。
    """
    st = _status_of(e)
    api = ""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            api = (resp.json().get("error") or {}).get("status", "") or ""
        except Exception:  # noqa: BLE001 — 本文がJSONでない場合は無視
            api = ""
    bits = [b for b in (f"HTTP {st}" if st else "", api) if b]
    return f"{type(e).__name__}" + (f"({', '.join(bits)})" if bits else "")


def sa_identity() -> str:
    """実行中のサービスアカウントのメールアドレス。

    アドレスは秘密ではない識別子。403 の切り分けが「どのSAで動いているか分からない」
    ために推測になるのを防ぐため、起動時に必ず出す（2026-08-13 に実際に詰まった）。
    """
    try:
        raw = os.getenv("GOOGLE_SA_JSON", "")
        if raw:
            return json.loads(raw).get("client_email", "(不明)")
        path = os.getenv("GOOGLE_CREDENTIALS_PATH", "")
        if path:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("client_email", "(不明)")
    except Exception:  # noqa: BLE001 — 診断用途なので失敗しても本処理は続ける
        pass
    return "(不明)"


def _cell(row: list, idx: int) -> str:
    if idx >= len(row):
        return ""
    v = row[idx]
    return v.strip() if isinstance(v, str) else v


def _num(s):
    """'75,864' / '386.43' → 数値。数値でなければ元の文字列（空なら空文字）を返す。

    Sheets に数値として入れておかないと後段の集計で使えないが、
    '2027/08/14' や 'Y' のような非数値を無理に落とすと情報が消えるため素通しする。
    """
    if s is None:
        return ""
    if isinstance(s, (int, float)):
        return s
    t = str(s).strip().replace(",", "")
    if not t:
        return ""
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return str(s).strip()


# ── 読み取り（すべて read-only。gid→タイトル解決は metadata の GET のみ）──────
def verify_extra_cols(token: str, sheet_id: str, brand) -> dict:
    """EXTRA_STOCK_COLS の列位置をフォーマットのヘッダ行で検証する。

    列がズレていた場合、黙って隣の列の値を履歴に焼き込むのが最悪なので、
    一致しなければその列を無効化（空で記録）し、警告を出す。
    """
    extra = EXTRA_STOCK_COLS.get(brand.key, {})
    if not extra:
        return {}
    title = resolve_title(token, sheet_id, brand.format_gid)
    header_row = brand.format_data_start_row - 1  # データ直上をヘッダとみなす
    rows = sheet_read(token, sheet_id,
                      _a1(title, f"A{header_row}:AZ{header_row}"))
    head = rows[0] if rows else []
    ok = {}
    for key, idx in extra.items():
        label = str(_cell(head, idx) or "")
        if EXTRA_STOCK_EXPECT[key] in label:
            ok[key] = idx
        else:
            print(f"[warn] {brand.key}: {key} の列位置がズレている可能性"
                  f"（{header_row}行目の該当セル='{label}'）。この列は空で記録する",
                  file=sys.stderr)
    return ok


def load_format_rows(token: str, sheet_id: str, brand, extra: dict) -> list[dict]:
    """フォーマットタブを生値で読む（inventory_alert.load_format とは別物：
    あちらは計算に要る列だけを型変換して返す。こちらは履歴として残す列を広く拾う）。"""
    title = resolve_title(token, sheet_id, brand.format_gid)
    rows = sheet_read(token, sheet_id,
                      _a1(title, f"A{brand.format_data_start_row}:AZ"))
    c = brand.format_cols
    out = []
    for r in rows:
        product = _cell(r, c["product"])
        sku = _cell(r, c["sku"])
        if not product or not sku:
            continue
        out.append({
            "商品名": product,
            "サイズ": _cell(r, c["size"]),
            "ASIN": _cell(r, c["asin"]),
            "SKU": sku,
            "総在庫": _num(_cell(r, c["stock_total"])),
            "FBA在庫": _num(_cell(r, c["stock_fba"])),
            "ココ在庫": _num(_cell(r, c["stock_coco"])),
            "マイクロアルジェAmazon在庫": (
                _num(_cell(r, extra["micro_amazon"])) if "micro_amazon" in extra else ""),
            "マイクロアルジェ楽天在庫": (
                _num(_cell(r, extra["micro_rakuten"])) if "micro_rakuten" in extra else ""),
            "自社在庫": _num(_cell(r, c["stock_own"])),
            "依頼済数量": _num(_cell(r, c["requested_qty"])),
            "シート販売数(総)": _num(_cell(r, c["sales_total"])),
            "シート販売数(Amazon)": _num(_cell(r, c["sales_amazon"])),
            "シート販売数(ココ)": _num(_cell(r, c["sales_coco"])),
            "シート在庫日数(総)": _num(_cell(r, c["days_total"])),
            "シート在庫日数(Amazon)": _num(_cell(r, c["days_amazon"])),
            "シート在庫日数(ココ)": _num(_cell(r, c["days_coco"])),
            "シート在庫切れ(総)": _cell(r, c["stockout_total"]),
            "シート在庫切れ(Amazon)": _cell(r, c["stockout_amazon"]),
            "シート在庫切れ(ココ)": _cell(r, c["stockout_coco"]),
            "発注アラート": _cell(r, c["alert_order"]),
            "FBA納品アラート": _cell(r, c["alert_fba"]),
            "ココ納品アラート": _cell(r, c["alert_coco"]),
            "対応済": _cell(r, c["alert_done"]),
            "現ロット": _cell(r, c["lot_current"]),
            "発注済ロット": _cell(r, c["lot_ordered"]),
            "発注ロット数": _num(_cell(r, c["order_lot"])),
        })
    return out


def load_ne_rows(token: str, sheet_id: str, brand) -> dict:
    title = resolve_title(token, sheet_id, brand.ne_gid)
    rows = sheet_read(token, sheet_id,
                      _a1(title, f"A{brand.ne_data_start_row}:H"))
    c = brand.ne_cols
    m = {}
    for r in rows:
        sku = _cell(r, c["sku"])
        if sku:
            m[sku] = (_num(_cell(r, c["coco_7d"])), _num(_cell(r, c["coco_30d"])))
    return m


def load_bot_alerts(token: str, sheet_id: str, brand) -> dict:
    """📊在庫アラート(bot) タブを SKU 索引で読む。

    このタブはフラグの立ったSKUだけを載せる（全SKUではない）ため、
    大半のSKUで bot 列は空になる。全SKU分の日販・在庫日数を持つのは Phase2 の宿題。

    タブ未作成（400/404）なら空で続行するが、認証・権限・API障害（401/403/429/5xx）は
    握りつぶさず落とす。それらを空で通すと「bot列が無い履歴」が恒久的に焼き付くため。
    タブ名で直接読む（gid 解決を挟まないのは、読み取り元へのアクセスを最小にするため）。
    """
    try:
        rows = sheet_read(token, sheet_id, _a1(brand.rec_tab_title, "A2:AZ"))
    except Exception as e:  # noqa: BLE001
        st = _status_of(e)
        if st in (400, 404):
            print(f"[warn] 在庫アラートタブが無い（{_safe_err(e)}）。bot 列は空で続行")
            return {}
        raise SystemExit(f"[FATAL] 在庫アラートタブの読み取りに失敗: {_safe_err(e)}")
    if len(rows) < 2:
        print("[warn] 在庫アラートタブに明細が無い（bot 未実行？）。bot 列は空で続行")
        return {}
    header = [str(h).strip() for h in rows[0]]
    if "SKU" not in header:
        print(f"[warn] 在庫アラートタブのヘッダに 'SKU' が無い（列数={len(header)}）",
              file=sys.stderr)
        return {}
    sku_at = header.index("SKU")
    idx = {name: header.index(name) for name, _ in BOT_FIELDS if name in header}
    missing = [name for name, _ in BOT_FIELDS if name not in idx]
    if missing:
        print(f"[warn] 在庫アラートタブに見つからない列: {missing}", file=sys.stderr)
    out = {}
    for r in rows[1:]:
        sku = _cell(r, sku_at)
        if sku:
            out[sku] = {dst: _num(_cell(r, idx[src]))
                        for src, dst in BOT_FIELDS if src in idx}
    return out


def load_sheet_date(token: str, sheet_id: str, brand) -> str:
    """フォーマットタブの基準日セル（labo は C2）。未設定ブランドは空。"""
    if not brand.format_date_cell:
        return ""
    title = resolve_title(token, sheet_id, brand.format_gid)
    vals = sheet_read(token, sheet_id, _a1(title, brand.format_date_cell))
    return _cell(vals[0], 0) if vals and vals[0] else ""


# ── 蓄積先 ─────────────────────────────────────────────────────────────────
def ensure_snapshot_tab(token: str, sheet_id: str) -> None:
    """スナップショットタブを用意する。既存なら**ヘッダの整合を検証**する。

    既存ヘッダが SNAPSHOT_HEADERS の prefix であることを要求する。
    列の挿入・並べ替え・削除があると過去行と意味が食い違うため、その場合は落とす。
    """
    meta = _sheets_call("GET", token, sheet_id, "",
                        params={"fields": "sheets.properties.title"})
    titles = {sh["properties"]["title"] for sh in meta.get("sheets", [])}
    if SNAPSHOT_TAB not in titles:
        _sheets_call("POST", token, sheet_id, ":batchUpdate", body={"requests": [{
            "addSheet": {"properties": {
                "title": SNAPSHOT_TAB,
                "gridProperties": {"rowCount": 2000,
                                   "columnCount": len(SNAPSHOT_HEADERS),
                                   "frozenRowCount": 2},
            }}
        }]})
        suffix = "/values/" + urllib.parse.quote(_a1(SNAPSHOT_TAB, "A1"), safe="")
        _sheets_call("PUT", token, sheet_id, suffix,
                     params={"valueInputOption": "RAW"},
                     body={"values": [[MARKER], SNAPSHOT_HEADERS]})
        print(f"[info] {SNAPSHOT_TAB} を作成した（{len(SNAPSHOT_HEADERS)}列）")
        return

    rows = sheet_read(token, sheet_id, _a1(SNAPSHOT_TAB, f"A2:{LAST_COL}2"))
    have = [str(h).strip() for h in (rows[0] if rows else [])]
    if not have:
        raise SystemExit(f"[FATAL] {SNAPSHOT_TAB} にヘッダ行が無い。手で編集された可能性")
    if have != SNAPSHOT_HEADERS[:len(have)]:
        diff = [(i, a, b) for i, (a, b)
                in enumerate(zip(have, SNAPSHOT_HEADERS)) if a != b][:3]
        raise SystemExit(
            f"[FATAL] {SNAPSHOT_TAB} のヘッダが定義と食い違う（列の挿入/並べ替え/削除）。"
            f"過去行と意味がズレるため中止する。不一致: {diff}")
    if len(have) < len(SNAPSHOT_HEADERS):
        raise SystemExit(
            f"[FATAL] 列が末尾に追加されている（既存{len(have)}列 → 定義{len(SNAPSHOT_HEADERS)}列）。"
            "ヘッダ行を先に手で拡張してから再実行すること")


def existing_skus(token: str, sheet_id: str, brand_key: str, today: str
                  ) -> tuple[set, list]:
    """当日・当ブランドの既存SKU集合と、当ブランドの蓄積日リストを返す。

    (日付, ブランド) 単位でスキップすると、前回が途中で落ちて一部SKUだけ
    入っていた場合に欠損が永久に埋まらない。SKU単位で差分を出す。
    """
    rng = _a1(SNAPSHOT_TAB, f"A3:{_col_letter(SKU_AT + 1)}")
    rows = sheet_read(token, sheet_id, rng)
    done, dates = set(), set()
    for r in rows:
        d, b = _cell(r, 0), _cell(r, BRAND_AT)
        if not d or b != brand_key:
            continue
        dates.add(d)
        if d == today:
            sku = _cell(r, SKU_AT)
            if sku:
                done.add(sku)
    return done, sorted(dates)


def append_rows(token: str, sheet_id: str, values: list[list]) -> None:
    """追記し、書き込み結果を検証する。部分書き込みや想定外範囲を見逃さない。"""
    rng = _a1(SNAPSHOT_TAB, f"A:{LAST_COL}")
    suffix = "/values/" + urllib.parse.quote(rng, safe="") + ":append"
    res = _sheets_call("POST", token, sheet_id, suffix,
                       params={"valueInputOption": "RAW",
                               "insertDataOption": "INSERT_ROWS"},
                       body={"values": values})
    upd = res.get("updates", {})
    wrote, rngs = upd.get("updatedRows"), upd.get("updatedRange", "")
    if wrote != len(values):
        raise SystemExit(
            f"[FATAL] 追記行数が一致しない（期待 {len(values)} / 実際 {wrote}）。"
            "部分書き込みの可能性。蓄積先を確認すること")
    if SNAPSHOT_TAB not in rngs:
        raise SystemExit(f"[FATAL] 想定外のタブに書き込まれた: {rngs}")


def warn_on_gap(dates: list, brand_key: str, today: str) -> None:
    """欠測を鳴らす。在庫の過去値は取り戻せないので、穴は静かに見逃さない。"""
    past = [d for d in dates if d < today]
    if not past:
        print(f"[info] {brand_key}: 当ブランドの初回蓄積")
        return
    last = past[-1]
    try:
        gap = (datetime.strptime(today, "%Y-%m-%d")
               - datetime.strptime(last, "%Y-%m-%d")).days
    except ValueError:
        return
    if gap > 1:
        print(f"[warn] 前回蓄積 {last} から {gap} 日空いている"
              f"（{gap - 1}日分の在庫履歴は取り戻せない）", file=sys.stderr)


# ── 本体 ───────────────────────────────────────────────────────────────────
def build_rows(fmt: list[dict], ne: dict, bot: dict,
               *, today: str, sheet_date: str, brand_key: str,
               fetched_at: str) -> list[list]:
    out = []
    for row in fmt:
        sku = row["SKU"]
        coco7, coco30 = ne.get(sku, ("", ""))
        rec = {
            "日付": today, "シート更新日": sheet_date, "ブランド": brand_key,
            "NEココ7d": coco7, "NEココ30d": coco30,
            "取得時刻JST": fetched_at,
            **row,
            **bot.get(sku, {}),
        }
        out.append([rec.get(h, "") for h in SNAPSHOT_HEADERS])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="在庫スナップショットを日次で蓄積する")
    ap.add_argument("--brand", default=os.getenv("BRAND", ""),
                    help="labo / nature / qiera")
    ap.add_argument("--dry-run", action="store_true",
                    help="読み取りと組み立てだけ行い、書き込まない")
    ap.add_argument("--show-sample", action="store_true",
                    help="dry-run 時に先頭行を表示（業務データが出るのでローカル専用）")
    args = ap.parse_args()

    if not args.brand:
        raise SystemExit("[FATAL] --brand か環境変数 BRAND が必要")
    brand = brands_mod.get_brand(args.brand)

    src_id = _env("SALES_SHEET_ID")
    dst_id = "" if args.dry_run else _env("SNAPSHOT_SHEET_ID")
    if dst_id and dst_id == src_id:
        raise SystemExit(
            "[FATAL] SNAPSHOT_SHEET_ID が読み取り元と同一。"
            "在庫管理シート本体には書き込まない方針のため中止する")

    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    fetched_at = now.strftime("%Y-%m-%d %H:%M JST")

    token = sheets_token()
    print(f"[info] 実行SA: {sa_identity()}"
          "（蓄積先シートにこのアドレスを編集者で共有していないと 403 になる）")
    extra = verify_extra_cols(token, src_id, brand)
    fmt = load_format_rows(token, src_id, brand, extra)
    if not fmt:
        raise SystemExit("[FATAL] フォーマットタブから1行も読めなかった（列マップ/gid を確認）")
    ne = load_ne_rows(token, src_id, brand)
    bot = load_bot_alerts(token, src_id, brand)
    sheet_date = load_sheet_date(token, src_id, brand)
    print(f"[info] {brand.name}: フォーマット {len(fmt)}行 / NE {len(ne)}件 / "
          f"botアラート {len(bot)}件 / シート更新日 {sheet_date or '未設定'}")

    rows = build_rows(fmt, ne, bot, today=today, sheet_date=sheet_date,
                      brand_key=brand.key, fetched_at=fetched_at)

    if args.dry_run:
        print(f"[dry-run] {len(rows)}行 × {len(SNAPSHOT_HEADERS)}列を組み立てた（書き込みなし）")
        if args.show_sample:
            print("[dry-run] 先頭行:", rows[0][:10])
        return 0

    ensure_snapshot_tab(token, dst_id)
    done, dates = existing_skus(token, dst_id, brand.key, today)
    warn_on_gap(dates, brand.key, today)

    pending = [r for r in rows if r[SKU_AT] not in done]
    if not pending:
        print(f"[info] {today} / {brand.key}: {len(done)}SKU すべて蓄積済み。スキップ")
        return 0
    if done:
        print(f"[info] {today} / {brand.key}: {len(done)}SKU は蓄積済み。"
              f"欠けている {len(pending)}SKU を補完する")

    append_rows(token, dst_id, pending)
    print(f"[ok] {today} / {brand.key}: {len(pending)}行を追記した")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # requests の例外文には URL＝スプレッドシートID が載る。ワークフローは
        # 「IDをコードに残さない」方針なので、ログにも残さない。
        # from None でチェーンを切り、元のトレースバックを出さない。
        raise SystemExit(f"[FATAL] 想定外のエラー: {_safe_err(exc)}") from None
