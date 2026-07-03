# -*- coding: utf-8 -*-
"""在庫管理シートのデータ健全性チェック（サイレント障害の検知）。

在庫シートのデータ収集は自動化済み（テープス/IMPORTRANGE/販売数集計ジョブ）だが、
これらは「静かに止まる」——抽出ツールの失敗・IMPORTRANGEの#REF!・新SKU追加時の
タブ追記漏れは、フォーマットのSUMIFSが0を返すだけでエラーにならず、古い/欠けた
数値のまま発注判断に使われる。本モジュールは毎朝の在庫アラート実行時に以下を
点検し、異常のあった日だけChatworkへ警告する（在庫アラート本文の曜日ゲートとは
独立。サイレント障害検知が目的のため毎日投稿しうる）。

  1. エラー値スキャン : #REF!/#ERROR! 等（IMPORTRANGE切れ・数式破損）
  2. 更新停止（鮮度） : ソースタブのデータ指紋が stale_after_days 日以上不変
  3. 行数激減         : 前回比50%未満（抽出ツールの空出力・誤クリア検知）
  4. SKU突合の欠落    : フォーマットのSKU/ASINが参照先タブに無い（SUMIFS=0の温床）
  5. 基準日セルの停滞 : フォーマットの更新日セルが古い（在庫切れ予想日が全てズレる）

状態（前回指紋・前回警告）は bot専用の隠しタブ（STATE_TAB_TITLE）に保存する。
同じ警告の毎日連投は避け、内容が変化した時と週明け（月曜）のみ再投稿する。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from sales30d import _a1, _sheets_call, sheet_read, sheet_update

STATE_TAB_TITLE = "⚙️bot_state"
STATE_MARKER = "⚠️ bot自動生成・編集禁止（在庫アラートbotの状態保存）"

# 深刻なエラー値（IMPORTRANGE切れ・参照破壊・読込スタック）→ 即警告
SEVERE_TOKENS = ("#REF!", "#ERROR!", "#NAME?", "Loading...")
# 軽微なエラー値 → 突合キー列に出た時だけ警告（突合が壊れるため）
MILD_TOKENS = ("#N/A", "#VALUE!", "#DIV/0!")

FORMAT_DATE_STALE_DAYS = 3     # フォーマット基準日セルの許容停滞日数
ROW_DROP_RATIO = 0.5           # 行数がこれ未満に減ったら激減とみなす
ROW_DROP_MIN_PREV = 20         # 激減判定は前回行数がこれ以上ある時のみ
MAX_LIST = 10                  # 警告に列挙する明細の上限


@dataclass(frozen=True)
class SourceTab:
    """健全性チェック対象のソースタブ定義（ブランド別に brands.py で設定）。"""
    gid: int
    label: str                 # 表示名（タブ名と一致させる必要はない）
    data_start_row: int        # データ開始行（1始まり）
    key_kind: str = ""         # 突合キー種別: "asin" / "sku" / ""=突合チェックなし
    key_col: int = 0           # 突合キー列（0始まり）
    stale_after_days: int = 2  # 指紋不変がこの日数以上続いたら更新停止を疑う
    fingerprint: bool = True   # 鮮度（指紋）チェック対象か。IMPORTRANGE直参照等
                               # 「変化しないのが正常」なタブは False にする


def _resolve_title(token: str, sheet_id: str, gid: int) -> str:
    meta = _sheets_call("GET", token, sheet_id, "",
                        params={"fields": "sheets.properties"})
    for sh in meta.get("sheets", []):
        p = sh.get("properties", {})
        if p.get("sheetId") == gid:
            return p["title"]
    raise RuntimeError(f"gid={gid} のシートが見つからない")


def _col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _fp(rows: list) -> str:
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ── 状態タブ（隠し・bot専用） ───────────────────────────────────────────────
def _get_or_create_state_tab(token: str, sheet_id: str) -> None:
    meta = _sheets_call("GET", token, sheet_id, "",
                        params={"fields": "sheets.properties"})
    for sh in meta.get("sheets", []):
        if sh.get("properties", {}).get("title") == STATE_TAB_TITLE:
            return
    _sheets_call("POST", token, sheet_id, ":batchUpdate", body={"requests": [{
        "addSheet": {"properties": {
            "title": STATE_TAB_TITLE, "hidden": True,
            "gridProperties": {"rowCount": 10, "columnCount": 2},
        }}
    }]})
    sheet_update(token, sheet_id, _a1(STATE_TAB_TITLE, "A1:A2"),
                 [[STATE_MARKER], ["{}"]])


def load_state(token: str, sheet_id: str, brand_key: str) -> dict:
    """状態JSONを読む。タブが無ければ作る。壊れていれば空から再開。"""
    _get_or_create_state_tab(token, sheet_id)
    rows = sheet_read(token, sheet_id, _a1(STATE_TAB_TITLE, "A1:A2"))
    a1 = rows[0][0] if rows and rows[0] else ""
    if a1 and not str(a1).startswith("⚠️ bot自動生成"):
        raise RuntimeError(f"状態タブ '{STATE_TAB_TITLE}' のA1がbot所有印でない→中止")
    try:
        st = json.loads(rows[1][0]) if len(rows) > 1 and rows[1] else {}
    except (json.JSONDecodeError, IndexError):
        st = {}
    return st.get(brand_key, {}) if isinstance(st, dict) else {}


def save_state(token: str, sheet_id: str, brand_key: str, brand_state: dict) -> None:
    rows = sheet_read(token, sheet_id, _a1(STATE_TAB_TITLE, "A2"))
    try:
        st = json.loads(rows[0][0]) if rows and rows[0] else {}
    except (json.JSONDecodeError, IndexError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    st[brand_key] = brand_state
    sheet_update(token, sheet_id, _a1(STATE_TAB_TITLE, "A1:A2"),
                 [[STATE_MARKER], [json.dumps(st, ensure_ascii=False)]])


# ── 各チェック ─────────────────────────────────────────────────────────────
def _scan_errors(tab: SourceTab, rows: list) -> list[dict]:
    """エラー値スキャン。深刻トークンは全セル、軽微トークンはキー列のみ。"""
    issues = []
    severe, mild_key = [], []
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            v = str(cell).strip()
            if not v.startswith(("#", "L")):
                continue
            addr = f"{_col_letter(ci)}{ri + 1}"
            if any(v.startswith(t) for t in SEVERE_TOKENS):
                severe.append(addr)
            elif (tab.key_kind and ci == tab.key_col
                  and any(v.startswith(t) for t in MILD_TOKENS)):
                mild_key.append(addr)
    if severe:
        issues.append({"sev": "🚨", "title": f"「{tab.label}」にエラー値 {len(severe)}件",
                       "detail": f"セル: {', '.join(severe[:MAX_LIST])}"
                                 f"{' ほか' if len(severe) > MAX_LIST else ''}"
                                 "（IMPORTRANGE切れ/参照破壊の可能性）"})
    if mild_key:
        issues.append({"sev": "⚠️", "title": f"「{tab.label}」の突合キー列にエラー値 {len(mild_key)}件",
                       "detail": f"セル: {', '.join(mild_key[:MAX_LIST])}（SKU突合が壊れます）"})
    return issues


def _check_freshness(tab: SourceTab, data_rows: list, prev: dict,
                     today_s: str, today: datetime) -> tuple[list[dict], dict]:
    """指紋の変化で更新停止・行数激減を検知。新しいタブ状態を返す。"""
    issues = []
    fp = _fp(data_rows)
    n = len([r for r in data_rows if any(str(c).strip() for c in r)])
    since = today_s
    if prev.get("fp") == fp:
        since = prev.get("since", today_s)
        try:
            days = (today - datetime.strptime(since, "%Y/%m/%d")).days
        except ValueError:
            days = 0
        if days >= tab.stale_after_days:
            issues.append({"sev": "⚠️",
                "title": f"「{tab.label}」が{days}日間更新されていない可能性",
                "detail": f"{since} からデータ不変。取得ツール/ジョブの停止を確認してください。"})
    prev_n = prev.get("rows")
    if (isinstance(prev_n, int) and prev_n >= ROW_DROP_MIN_PREV
            and n < prev_n * ROW_DROP_RATIO):
        issues.append({"sev": "🚨",
            "title": f"「{tab.label}」の行数が激減（{prev_n}→{n}行）",
            "detail": "抽出ツールの空出力や誤クリアの可能性。直ちに確認を。"})
    return issues, {"fp": fp, "since": since, "rows": n}


def _check_join_gaps(tab: SourceTab, data_rows: list, fmt_rows: list) -> list[dict]:
    """フォーマットのSKU/ASINが参照先タブに無いものを列挙（SUMIFS=0の温床）。"""
    if not tab.key_kind:
        return []
    keys = set()
    for r in data_rows:
        if tab.key_col < len(r):
            v = str(r[tab.key_col]).strip()
            if v:
                keys.add(v)
    if not keys:  # タブ空の場合は行数激減/鮮度側で検知するため二重警告しない
        return []
    missing = []
    for s in fmt_rows:
        if "終売" in s.get("sku_comment", ""):
            continue
        k = s.get(tab.key_kind, "").strip()
        if k and k not in keys:
            missing.append(f"{s['product']} {s['size']}（{k}）".strip())
    if not missing:
        return []
    return [{"sev": "⚠️",
             "title": f"「{tab.label}」に無いSKUがフォーマットに{len(missing)}件",
             "detail": "突合できず在庫/販売が0扱いになります: "
                       + "、".join(missing[:MAX_LIST])
                       + ("　ほか" if len(missing) > MAX_LIST else "")}]


def _check_format_date(token: str, sheet_id: str, brand,
                       today: datetime) -> list[dict]:
    """フォーマットの基準日セル（在庫切れ予想日の起点）の停滞チェック。"""
    cell = getattr(brand, "format_date_cell", "")
    if not cell:
        return []
    title = _resolve_title(token, sheet_id, brand.format_gid)
    rows = sheet_read(token, sheet_id, _a1(title, cell))
    v = str(rows[0][0]).strip() if rows and rows[0] else ""
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", v)
    if not m:
        return [{"sev": "⚠️", "title": f"フォーマット基準日セル({cell})が日付でない",
                 "detail": f"値='{v}'。在庫切れ予想日の計算が壊れている可能性。"}]
    d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    days = (today.replace(hour=0, minute=0, second=0, microsecond=0) - d).days
    if days > FORMAT_DATE_STALE_DAYS:
        return [{"sev": "⚠️", "title": f"フォーマット基準日({cell})が{days}日前のまま",
                 "detail": f"{v} から未更新。在庫切れ予想日・アラートYが全て後ろズレしています。"}]
    return []


# ── まとめ ─────────────────────────────────────────────────────────────────
def run_health_checks(token: str, sheet_id: str, brand, fmt_rows: list,
                      now: datetime, dry_run: bool = False
                      ) -> tuple[list[dict], str, dict | None]:
    """全チェックを実行し (issues, chatwork本文, 状態) を返す。本文が空なら投稿不要。

    投稿判定: 警告内容が前回から変化した時、または月曜（週明けリマインド）のみ。
    解消時（前回警告あり→今回0件）は解消メッセージを1回だけ返す。

    ⚠️ 本関数は状態を保存しない。呼び出し側が Chatwork 投稿の成否を確定させた後、
    commit_state(state, posted=...) を呼ぶこと（投稿失敗時に通知状態を進めて
    警告を握りつぶさないため。観測状態[タブ指紋]と通知状態[issues_fp]は分離）。
    dry_run 時は前回状態を読まない（状態タブ作成という書込みを避ける）ため、
    鮮度チェックは常に「今日から観測開始」扱いになる。
    """
    if not getattr(brand, "health_tabs", ()):
        print(f"[info] 健全性チェック: {brand.key} は未設定→スキップ")
        return [], "", None

    today = now
    today_s = now.strftime("%Y/%m/%d")
    state = {} if dry_run else load_state(token, sheet_id, brand.key)
    tabs_state_prev = state.get("tabs", {})
    tabs_state_new = {}
    issues: list[dict] = []

    for tab in brand.health_tabs:
        try:
            title = _resolve_title(token, sheet_id, tab.gid)
            rows = sheet_read(token, sheet_id, _a1(title, "A1:AZ"))
        except Exception as e:
            issues.append({"sev": "🚨", "title": f"「{tab.label}」が読めない",
                           "detail": f"{type(e).__name__}: {str(e)[:80]}（タブ削除/リネーム？）"})
            continue
        data_rows = rows[tab.data_start_row - 1:]
        issues += _scan_errors(tab, rows)
        if tab.fingerprint:
            fresh_issues, tab_state = _check_freshness(
                tab, data_rows, tabs_state_prev.get(tab.label, {}), today_s, today)
            issues += fresh_issues
            tabs_state_new[tab.label] = tab_state
        issues += _check_join_gaps(tab, data_rows, fmt_rows)

    try:
        issues += _check_format_date(token, sheet_id, brand, today)
    except Exception as e:
        issues.append({"sev": "⚠️", "title": "基準日セルの確認に失敗",
                       "detail": f"{type(e).__name__}: {str(e)[:80]}"})

    issues.sort(key=lambda i: (i["sev"] != "🚨", i["title"]))
    issues_fp = _fp([(i["title"], i["detail"]) for i in issues]) if issues else ""
    prev_fp = state.get("issues_fp", "")

    body = ""
    if issues:
        changed = issues_fp != prev_fp
        monday_again = today.weekday() == 0 and state.get("last_post") != today_s
        if changed or monday_again:
            body = _build_message(brand, issues, now)
    elif prev_fp:
        body = (f"[info][title]✅ 在庫シートのデータ異常が解消（{brand.name}）[/title]"
                f"前回警告した項目はすべて確認できなくなりました。{today_s} 時点で正常です。[/info]")

    hs = None
    if not dry_run:
        hs = {"brand_key": brand.key, "tabs": tabs_state_new, "issues_fp": issues_fp,
              "prev_issues_fp": prev_fp, "prev_last_post": state.get("last_post", ""),
              "today": today_s, "pending": bool(body)}
    return issues, body, hs


def commit_state(token: str, sheet_id: str, hs: dict | None, posted: bool) -> None:
    """健全性チェックの状態保存。未配達の警告がある時は一切進めない。

    警告本文あり（pending）なのに投稿できなかった場合、通知状態（issues_fp）は
    もちろん観測状態（タブ指紋・行数）も保存しない——行数激減のような前回値
    比較の警告は、ベースラインを上書きすると次回再検知できなくなるため。
    状態を丸ごと据え置けば次回同一条件で再検知され、再送される。
    """
    if hs is None:
        return
    if hs["pending"] and not posted:
        print("[info] 健全性状態は保存せず据え置き（未配達の警告を次回再検知・再送させる）")
        return
    fp = hs["issues_fp"] if posted else hs["prev_issues_fp"]
    last = hs["today"] if posted else hs["prev_last_post"]
    save_state(token, sheet_id, hs["brand_key"],
               {"tabs": hs["tabs"], "issues_fp": fp, "last_post": last})


def _build_message(brand, issues: list[dict], now: datetime) -> str:
    n_crit = sum(1 for i in issues if i["sev"] == "🚨")
    head = f"🩺 在庫シートのデータ異常 {len(issues)}件（{brand.name}）"
    lines = [f"[info][title]{head}[/title]"]
    lines.append(f"{now:%m/%d %H:%M} 時点の点検で以下を検出しました。"
                 "数値が古い/欠けたまま発注判断に使われる恐れがあります。")
    lines.append("[hr]")
    for i in issues:
        lines.append(f"{i['sev']} {i['title']}")
        lines.append(f"　{i['detail']}")
    lines.append("[hr]")
    lines.append("💡 エラー値→該当タブの数式/IMPORTRANGEを開いて再認可・修正。"
                 "更新停止→抽出ツール/ジョブの稼働を確認。"
                 "SKU欠落→参照先タブへ該当SKUの行を追記。")
    if n_crit:
        lines.append(f"🚨 {n_crit}件は至急確認を推奨します。")
    lines.append("[/info]")
    return "\n".join(lines)
