#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
ai_session_viewer.py
把 Claude Code / Codex 的 JSONL session 轉成可「翻閱」的 HTML 與 Markdown。
跨平台（Windows / Debian / macOS）、零外部相依（只用 Python 3 標準函式庫）。

用法：
    python3 ai_session_viewer.py                 # 自動偵測 Claude(~/.claude*) + Codex(~/.codex/sessions) -> ./out
    python3 ai_session_viewer.py --open          # 完成後打開 index.html
    python3 ai_session_viewer.py --no-codex      # 只轉 Claude（--no-claude 則只轉 Codex）
    python3 ai_session_viewer.py --account work  # 只轉 ~/.claude-work
    python3 ai_session_viewer.py --claude-source a=~/.claude/projects --codex-source b=/path/sessions
    python3 ai_session_viewer.py --project Obts --format html
    python3 ai_session_viewer.py --search "關鍵字" --open   # 全文搜尋既有輸出（結果頁在 out/search/）
（Windows 可用 py 取代 python3，或雙擊 run.cmd；Debian/macOS 用 run.sh）
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html
import json
import math
import re
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

# ---- 讓 Windows 主控台能印 Unicode ----
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

MAX_RESULT_CHARS = 20000           # tool 結果在 HTML 裡的最大字數（可摺疊，超過截斷）
MAX_WRITE_CHARS = 8000             # Write/檔案內容預覽上限
MAX_IMG_BYTES = 3 * 1024 * 1024    # 內嵌圖片上限（解碼後位元組）；超過只放佔位
SAFE_IMAGE_MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# 全部用 timezone-aware 的哨兵，避免和 aware 的事件時間比較時報錯
AWARE_MIN = datetime.min.replace(tzinfo=timezone.utc)
AWARE_MAX = datetime.max.replace(tzinfo=timezone.utc)

MANIFEST_NAME = ".build-manifest.json"
RENDERER_VERSION = 26  # 渲染邏輯版本；改變 session 呈現方式或 row 結構時 +1，會強制全部重建
SOURCE_CLAUDE = "claude-code"
SOURCE_CODEX = "codex"
SOURCE_LABELS = {
    SOURCE_CLAUDE: "Claude Code",
    SOURCE_CODEX: "Codex",
}


def source_label(kind: str) -> str:
    return SOURCE_LABELS.get(kind, kind or "未知")


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def esc_attr(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.I)


def _safe_href(url):
    """只允許 http/https/mailto/相對/錨點；其餘（javascript:、data: …）回 None。"""
    u = str(url or "").strip()
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:", "#", "/", "./", "../")):
        return u
    if _SCHEME_RE.match(low):            # 有不在白名單的 scheme
        return None
    return u                             # 視為相對路徑


def _md_link_sub(m):
    text, url = m.group(1), m.group(2)
    href = _safe_href(url)
    return f'<a href="{esc_attr(href)}" target="_blank" rel="noopener">{text}</a>' if href else text


def short_model(m):
    return re.sub(r"^claude-", "", str(m or ""))


def fmt_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_money(x):
    if x is None:
        return "?"
    if x <= 0:
        return "$0"
    if x < 0.01:
        return "<$0.01"
    if x < 1000:
        return f"${x:.2f}"
    return f"${x:,.0f}"


# 各模型每 1M token 單價（USD, 公告價, input/output）；用前綴 fallback，出新版本也不必馬上改。
# 價格會變動——這是「估算」，需要時更新此表即可。
PRICE_PER_M = {
    "claude-opus": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "claude-fable": (10.0, 50.0),
    "claude-mythos": (10.0, 50.0),
}
CACHE_WRITE_MULT = 1.25      # 快取寫入（5 分鐘 TTL；無細分資訊的舊資料也按此計，1 小時寫入會低估）
CACHE_WRITE_MULT_1H = 2.0    # 快取寫入（1 小時 TTL；usage.cache_creation 細分可辨識時精算）
CACHE_READ_MULT = 0.10    # 快取讀取
CACHE_COLD_PCT = 25       # 命中率低於此值視為「冷啟動」（該步幾乎整段重寫快取）→ 紅底白字標示。
# 冷啟動成因不只一種：(1) 長時間閒置／resume 後快取 TTL 過期；(2) 回合內快取斷點的一次性 miss——
# 不需長間隔，實測同一晚連續步驟（間隔 18s、同 model）也會 0% 後立刻復原，多與 prompt cache 斷點/
# 20-block 回溯限制、模型切換、前綴變動有關。本工具只標「該步很冷」這個事實，不臆測成因。

# ── 快取分析報告（健檢 cache-report.html ＋ 假說檢定 cache-hypotheses.html）參數 ──
# 思路：每次 API 呼叫都帶 usage，命中率＝cache_read/脈絡；把「與前一次呼叫的間隔」對上「這次是否冷啟」，
# 就能量測「閒置多久快取會失效」。新版 usage.cache_creation 有 5 分／1 小時 TTL 細分（實測本機資料
# 幾乎全為 1 小時寫入），因此 TTL 是「量測」而非推估；每個冷啟依成因分解（見 REPORT_CAUSES），
# 只有排除結構性成因（session 第一句、切帳號、換模型、壓縮）後的樣本才進 TTL 存活估計（競爭風險原則）。
REPORT_MIN_CTX = 500          # 脈絡低於此 token 數的步驟不納入分析（暖機/瑣碎呼叫，命中率無參考意義）
REPORT_BREAK_SEC = 30 * 60    # 全域閒置 ≥ 此秒數＝一次「中斷」，其後第一個冷啟步＝「重新開工」事件
REPORT_INTRA_SEC = 2 * 60     # 間隔 < 此秒數＝回合內連續呼叫（快取斷點/20-block 回溯等一次性 miss 雜訊）
REPORT_TTL_SAFE_SEC = 55 * 60  # 1h TTL 的「應命中」上界（留 5 分鐘餘裕）；band 內冷啟＝提早失效（異常）
# 存活分析的間隔分桶（秒上界, 標籤）：對數尺度近似；曲線 x 座標取各桶幾何中點。
REPORT_GAP_BUCKETS = [
    (60, "< 1 分"), (5 * 60, "1–5 分"), (10 * 60, "5–10 分"), (15 * 60, "10–15 分"),
    (30 * 60, "15–30 分"), (60 * 60, "30–60 分"), (2 * 3600, "1–2 時"),
    (6 * 3600, "2–6 時"), (float("inf"), "> 6 時"),
]
# ③ 伺服器負載假設：快取會不會在全球尖峰時段較易「提早失效」？依 UTC（伺服器時間）分析。
# 觀察帶＝1h 寫入 cohort 的「應命中帶」[REPORT_INTRA_SEC, REPORT_TTL_SAFE_SEC)——帶內冷啟即異常。
# 時段與間隔長短相關（午休/夜間閒置較久），故檢定前先依 gap 分層（CMH），避免把「閒得久」誤讀成「尖峰失效」。
REPORT_PEAK_UTC = set(range(13, 22))   # 13–21 UTC：歐洲午後＋美國上午，一般是 Anthropic 全球最重時段
REPORT_CMH_STRATA = [15 * 60, 30 * 60, REPORT_TTL_SAFE_SEC]   # 帶內 gap 分層上界：2–15 / 15–30 / 30–55 分
# ④ 脈絡大小假設：逐出機率會不會隨累積脈絡變大而上升？帶內樣本依「前一步脈絡」（＝前一步寫進快取、
# 這次要能活著回來的量）分箱；檢定以帶內中位數切大/小、沿用同一套 gap 分層 CMH（輸出在假說頁）。
REPORT_CTX_BINS = [(50_000, "< 50k"), (150_000, "50–150k"), (300_000, "150–300k"),
                   (500_000, "300–500k"), (float("inf"), "≥ 500k")]
# 冷啟成因（優先序判定，每個冷啟恰好一個成因）；label/css 供報告呈現。
REPORT_CAUSES = [
    ("first",   "session 第一句", "不可避免：全新前綴"),
    ("switch",  "limit/切帳號",   "不可避免：429/401 邊界後第一步（快取按組織隔離）"),
    ("model",   "換模型",         "結構性：快取按模型隔離"),
    ("compact", "壓縮後",         "結構性：/compact 或自動壓縮改寫前綴"),
    ("expiry",  "閒置過期",       "可避免：間隔超過 TTL"),
    ("evict",   "提早失效",       "異常：1h TTL 內（2–55 分）卻冷啟"),
    ("intra",   "回合內雜訊",     "快取斷點/20-block 回溯的一次性 miss，非 TTL"),
]


def model_price(model):
    m = str(model or "").lower()
    if m.startswith("<"):          # <synthetic> 等內部偽模型：不計費
        return (0.0, 0.0)
    for prefix, p in PRICE_PER_M.items():
        if m.startswith(prefix):
            return p
    return None


# 各模型 context 視窗（token）；用於把 resume 量換算成 %。會隨官方調整變動，需要時自己改。
# 前綴 fallback；查不到（如 Codex/gpt）就只顯示絕對值、不顯示 %。
CONTEXT_WINDOW = {
    "claude-opus-4": 1_000_000,
    "claude-sonnet-4": 1_000_000,
    "claude-haiku": 200_000,
    "claude-3": 200_000,
}


def context_window(model):
    m = str(model or "").lower()
    for prefix, w in CONTEXT_WINDOW.items():
        if m.startswith(prefix):
            return w
    return None


def _ephemeral_split(u):
    """從 usage.cache_creation 物件取 (5 分, 1 小時) TTL 寫入細分；舊資料無此欄回 (0, 0)。"""
    cc = u.get("cache_creation")
    if not isinstance(cc, dict):
        return 0, 0
    out = []
    for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        try:
            out.append(int(cc.get(key) or 0))
        except (TypeError, ValueError):
            out.append(0)
    return out[0], out[1]


def call_cost(model, inp, cache_create, cache_read, out, cc_5m=0, cc_1h=0):
    """單次 API 呼叫的估算成本（USD）；未知模型回 None。
    快取寫入依 TTL 細分計價（5 分 1.25×、1 小時 2×）；細分未涵蓋的部分（舊資料）按 5 分計，會低估。"""
    p = model_price(model)
    if not p:
        return None
    pin, pout = p
    legacy = max(cache_create - cc_5m - cc_1h, 0)      # 無細分資訊的寫入量
    write = (legacy + cc_5m) * CACHE_WRITE_MULT + cc_1h * CACHE_WRITE_MULT_1H
    return (inp * pin
            + write * pin
            + cache_read * pin * CACHE_READ_MULT
            + out * pout) / 1_000_000


def cost_label(cost, partial):
    """顯示估算花費：已知部分用金額，若含未知模型再加 +?；完全未知才回 ?。"""
    if partial and (cost or 0) <= 0:
        return "?"
    return fmt_money(cost) + ("+?" if partial else "")


def parse_ts(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def local_str(dt, fmt="%Y-%m-%d %H:%M"):
    if not dt:
        return ""
    try:
        return dt.astimezone().strftime(fmt)
    except Exception:
        return dt.strftime(fmt)


WEEKDAYS_TW = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def day_label(dt):
    """本地日期＋星期，例如 2026-05-21 週三（給 session 頁的換日分隔線用）。"""
    if not dt:
        return ""
    try:
        loc = dt.astimezone()
    except Exception:
        loc = dt
    return loc.strftime("%Y-%m-%d") + " " + WEEKDAYS_TW[loc.weekday()]


def fmt_dur(secs):
    if secs is None or secs < 0:
        return ""
    secs = int(secs)
    if secs < 60:
        return f"{secs}秒"
    if secs < 3600:
        return f"{secs // 60}分"
    return f"{secs // 3600}時{(secs % 3600) // 60}分"


def safe_name(s, maxlen=60):
    s = re.sub(r"[^\w.\-]+", "_", str(s)).strip(" ._")
    name = (s[:maxlen].rstrip(" .") or "untitled")
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if name.upper() in reserved:
        name = ("_" + name)[:maxlen]
    return name


def path_leaf_name(path_text, fallback=""):
    text = str(path_text or "").rstrip("\\/")
    if not text:
        return fallback
    if "\\" in text:
        return PureWindowsPath(text).name or fallback
    return Path(text).name or fallback


def first_line(s, n=70):
    if not s:
        return ""
    line = str(s).strip().splitlines()[0] if str(s).strip() else ""
    return (line[:n] + "…") if len(line) > n else line


# =========================================================================
# 迷你 Markdown -> HTML（夠用版：程式碼區塊/行內碼/標題/清單/引用/粗斜體/連結）
# =========================================================================
_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def md_to_html(text: str) -> str:
    if not text:
        return ""
    text = str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    blocks: list[tuple[str, str]] = []
    inlines: list[str] = []

    def stash_block(m):
        blocks.append((m.group(1).strip(), m.group(2)))
        return f"\x00B{len(blocks) - 1}\x00"

    def stash_inline(m):
        inlines.append(m.group(1))
        return f"\x00I{len(inlines) - 1}\x00"

    text = _FENCE_RE.sub(stash_block, text)
    text = _INLINE_CODE_RE.sub(stash_inline, text)
    text = html.escape(text, quote=False)

    def restore_block(ph):
        idx = int(re.match(r"\x00B(\d+)\x00", ph).group(1))
        lang, code = blocks[idx]
        cls = f' class="language-{esc(lang)}"' if lang else ""
        return f"<pre><code{cls}>{html.escape(code, quote=False)}</code></pre>"

    def inline_fmt(s: str) -> str:
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _md_link_sub, s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", s)
        s = re.sub(r"\x00I(\d+)\x00",
                   lambda m: "<code>" + html.escape(inlines[int(m.group(1))], quote=False) + "</code>", s)
        return s

    out: list[str] = []
    para: list[str] = []
    list_buf: list[str] = []
    list_tag = [None]  # mutable holder

    def flush_para():
        if para:
            out.append("<p>" + inline_fmt("<br>".join(para)) + "</p>")
            para.clear()

    def flush_list():
        if list_buf:
            tag = list_tag[0]
            out.append(f"<{tag}>" + "".join(f"<li>{inline_fmt(x)}</li>" for x in list_buf) + f"</{tag}>")
            list_buf.clear()
            list_tag[0] = None

    def is_sep_row(s):
        return "-" in s and bool(re.fullmatch(r"\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*", s))

    def split_cells(s):
        s = s.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped == "":
            flush_para(); flush_list(); i += 1; continue

        if re.fullmatch(r"\x00B\d+\x00", stripped):
            flush_para(); flush_list(); out.append(restore_block(stripped)); i += 1; continue

        m = re.match(r"(#{1,6})\s+(.*)$", line)
        if m:
            flush_para(); flush_list()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline_fmt(m.group(2).strip())}</h{lvl}>"); i += 1; continue

        # GFM 表格：標題列 + 分隔列(|---|---|) + 內容列
        if "|" in stripped and i + 1 < n and "|" in lines[i + 1] and is_sep_row(lines[i + 1]):
            flush_para(); flush_list()
            header = split_cells(stripped)
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(split_cells(lines[i])); i += 1
            thead = "".join(f"<th>{inline_fmt(c)}</th>" for c in header)
            tbody = "".join("<tr>" + "".join(f"<td>{inline_fmt(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f'<table class="md"><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>')
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_para(); flush_list(); out.append("<hr>"); i += 1; continue

        if line.lstrip().startswith("&gt;"):
            flush_para(); flush_list()
            q = re.sub(r"^\s*&gt;\s?", "", line)
            out.append(f"<blockquote>{inline_fmt(q)}</blockquote>"); i += 1; continue

        m = re.match(r"^\s*([-*+])\s+(.*)$", line)
        if m:
            flush_para()
            if list_tag[0] not in (None, "ul"):
                flush_list()
            list_tag[0] = "ul"; list_buf.append(m.group(2)); i += 1; continue

        m = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
        if m:
            flush_para()
            if list_tag[0] not in (None, "ol"):
                flush_list()
            list_tag[0] = "ol"; list_buf.append(m.group(2)); i += 1; continue

        flush_list()
        para.append(stripped); i += 1

    flush_para(); flush_list()
    result = "\n".join(out)
    # 保險：還原任何漏掉的佔位符
    result = re.sub(r"\x00B(\d+)\x00", lambda m: restore_block(m.group(0)), result)
    result = re.sub(r"\x00I(\d+)\x00",
                    lambda m: "<code>" + html.escape(inlines[int(m.group(1))], quote=False) + "</code>", result)
    return result


# =========================================================================
# 載入 session
# =========================================================================
class Session:
    def __init__(self, path: Path, proj_munged: str, source_kind: str = SOURCE_CLAUDE):
        self.path = path
        self.proj_munged = proj_munged
        self.source_kind = source_kind
        self.session_id = path.stem
        self.events: list[dict] = []
        self.title = ""
        self.cwd = ""
        self.branch = ""
        self.version = ""
        self.start = None
        self.end = None
        self.proj_display = proj_munged
        self.account = ""
        self.ai_title = ""
        self.rename = ""
        self.models = []
        self.tok_out = 0
        self.ctx_peak = 0
        self.cost = 0.0
        self.cost_partial = False
        self.cache_pct = 0
        self.cache_steps = []
        self.cache_models = []
        self.cache_events = []
        self.usage = {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0, "total_in": 0}
        self.n_user = self.n_assistant = self.n_tools = 0
        self.out_html = ""
        self.out_md = ""


def load_events(path: Path, sidechain_force=False):
    evs = []
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  ! 讀取失敗 {path.name}: {e}", file=sys.stderr)
        return evs
    with fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            ev["_i"] = i
            ev["_dt"] = parse_ts(ev.get("timestamp"))
            if sidechain_force:
                ev["isSidechain"] = True
            evs.append(ev)
    return evs


def load_session(path: Path, proj_munged: str, account: str = "", source_kind: str = SOURCE_CLAUDE) -> Session:
    s = Session(path, proj_munged, source_kind)
    s.account = account
    s.events = load_events(path)

    # 外部子代理轉錄：<projectDir>/<sessionId>/**/*.jsonl
    # 旁邊的 <agent>.meta.json 帶 toolUseId，用來把子代理對話接回主對話那個 Task 呼叫底下。
    side_dir = path.with_suffix("")
    if side_dir.is_dir():
        for sf in sorted(side_dir.rglob("*.jsonl")):
            evs = load_events(sf, sidechain_force=True)
            parent_tid, agent_type, agent_desc = None, "", ""
            meta_path = sf.parent / (sf.stem + ".meta.json")
            if meta_path.is_file():
                try:
                    m = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(m, dict):
                        parent_tid = m.get("toolUseId")
                        agent_type = m.get("agentType") or ""
                        agent_desc = m.get("description") or ""
                except Exception:
                    pass
            for ev in evs:
                ev["_parent_tool_use"] = parent_tid
                ev["_agent_type"] = agent_type
                ev["_agent_desc"] = agent_desc
            s.events.extend(evs)

    # /compact 邊界中繼資料：compact_boundary 系統事件帶 trigger/preTokens/postTokens，
    # 其 uuid = 對應 isCompactSummary 摘要事件的 parentUuid，據此把資訊接到摘要上。
    boundary_meta = {e.get("uuid"): e.get("compactMetadata")
                     for e in s.events
                     if e.get("type") == "system" and e.get("subtype") == "compact_boundary"
                     and isinstance(e.get("compactMetadata"), dict)}
    for e in s.events:
        if e.get("isCompactSummary"):
            e["_compact_meta"] = boundary_meta.get(e.get("parentUuid")) or {}

    # 中繼資料
    titles = [e.get("aiTitle") for e in s.events if e.get("type") == "ai-title" and e.get("aiTitle")]
    s.ai_title = titles[-1] if titles else ""
    s.rename = extract_rename(s.events)
    for e in s.events:
        if not s.cwd and e.get("cwd"):
            s.cwd = e["cwd"]
        if not s.branch and e.get("gitBranch"):
            s.branch = e["gitBranch"]
        if not s.version and e.get("version"):
            s.version = e["version"]
    dts = [e["_dt"] for e in s.events if e.get("_dt")]
    if dts:
        s.start, s.end = min(dts), max(dts)
    if s.cwd:
        s.proj_display = path_leaf_name(s.cwd, proj_munged)
    # 標題優先序：使用者 /rename 名稱 > AI 自動標題 > 第一句使用者訊息
    if s.rename:
        s.title = s.rename
    else:
        t = s.ai_title
        if not t or t.strip().startswith("/"):
            t = first_user_text(s.events) or t or f"(無對話) {s.session_id[:8]}"
        s.title = t
    return s


def _codex_content_text(content, text_type="output_text"):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == text_type or (not item.get("type") and item.get("text")):
            txt = item.get("text")
            if txt:
                parts.append(str(txt))
    return "\n".join(parts)


def _json_obj_maybe(text):
    if not isinstance(text, str):
        return text if text is not None else {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, (dict, list)) else {"value": parsed}
    except Exception:
        return text


def _codex_usage(usage):
    if not isinstance(usage, dict):
        return {}
    total_in = usage_int(usage, "input_tokens")
    cached = usage_int(usage, "cached_input_tokens")
    return {
        "input_tokens": max(total_in - cached, 0),
        "cache_read_input_tokens": cached,
        "output_tokens": usage_int(usage, "output_tokens"),
    }


def _codex_usage_sig(usage):
    if not isinstance(usage, dict):
        return None
    return tuple(usage_int(usage, k) for k in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens",
    ))


def _md_fence(text, info=""):
    text = "" if text is None else str(text)
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def _attach_codex_usage(event, usage, model):
    if not event:
        return
    msg = event.setdefault("message", {})
    msg["model"] = model or msg.get("model") or ""
    current = msg.get("usage")
    merged = dict(current) if isinstance(current, dict) else {}
    for key, value in _codex_usage(usage).items():
        merged[key] = usage_int(merged, key) + value
    msg["usage"] = merged


def _codex_tool_name(name):
    return {
        "shell_command": "Bash",
        "apply_patch": "Patch",
    }.get(str(name or ""), str(name or "tool"))


def _find_codex_index(source: Path):
    """從 source（sessions 目錄或單一 rollout JSONL）往上層找最近的 session_index.jsonl，
    搜尋止於 `.codex` 目錄或檔案系統根。"""
    start = source if source.is_dir() else source.parent
    for d in [start, *start.parents]:
        cand = d / "session_index.jsonl"
        if cand.is_file():
            return cand
        if d.name == ".codex":
            break
    return None


def load_codex_thread_names(sessions_root: Path) -> dict:
    """讀 ~/.codex/session_index.jsonl（在 sessions 目錄的上一層；來源是單一 JSONL 時往上層尋找），
    回傳 {session_id: 名稱}。這是 Codex 版的『對話命名』，相當於 Claude 的 /rename。"""
    names = {}
    index = _find_codex_index(sessions_root)
    if index is None:
        return names
    try:
        raw = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return names
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if isinstance(o, dict):
            sid = str(o.get("id") or "").strip()
            name = str(o.get("thread_name") or "").strip().strip('"').strip()
            if sid and name:
                names[sid] = name
    return names


def load_codex_session(path: Path, account: str = "default", thread_names=None) -> Session:
    s = Session(path, "codex", SOURCE_CODEX)
    s.account = account
    raw = load_events(path)
    s.events = []
    current_model = ""
    last_assistant_event = None
    attached_usage: dict[str, set[tuple[int, ...]]] = {}

    for i, e in enumerate(raw):
        payload = e.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        ptype = payload.get("type")
        ts = e.get("timestamp")
        dt = e.get("_dt")

        if e.get("type") == "session_meta":
            meta_id = (payload.get("id") or "").strip()
            if meta_id:
                s.session_id = meta_id
            s.cwd = payload.get("cwd") or s.cwd
            git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
            s.branch = git.get("branch") or s.branch
            s.version = payload.get("cli_version") or s.version
            continue

        if e.get("type") == "turn_context":
            s.cwd = payload.get("cwd") or s.cwd
            current_model = payload.get("model") or current_model
            continue

        if e.get("type") == "event_msg":
            if ptype == "user_message":
                # Codex also emits response_item role=user, but those can include injected context.
                # event_msg:user_message is the user-visible turn.
                text = payload.get("message") or ""
                s.events.append({
                    "type": "user",
                    "uuid": f"codex-user-{i}",
                    "timestamp": ts,
                    "_dt": dt,
                    "_i": i,
                    "cwd": s.cwd,
                    "gitBranch": s.branch,
                    "version": s.version,
                    "sessionId": s.session_id,
                    "message": {"role": "user", "content": str(text)},
                })
            elif ptype == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage = info.get("last_token_usage")
                msg = (last_assistant_event or {}).get("message") or {}
                mid, sig = msg.get("id"), _codex_usage_sig(usage)
                if mid and sig is not None and sig not in attached_usage.setdefault(mid, set()):
                    _attach_codex_usage(last_assistant_event, usage, current_model)
                    attached_usage[mid].add(sig)
            continue

        if e.get("type") != "response_item":
            continue

        if ptype == "message":
            role = payload.get("role")
            if role != "assistant":
                continue
            text = _codex_content_text(payload.get("content"), "output_text")
            if not text.strip():
                continue
            ev = {
                "type": "assistant",
                "uuid": f"codex-assistant-{i}",
                "timestamp": ts,
                "_dt": dt,
                "_i": i,
                "cwd": s.cwd,
                "gitBranch": s.branch,
                "version": s.version,
                "sessionId": s.session_id,
                "message": {"role": "assistant", "id": f"codex-msg-{i}",
                            "model": current_model, "content": [{"type": "text", "text": text}]},
            }
            s.events.append(ev)
            last_assistant_event = ev
        elif ptype == "reasoning":
            text = _codex_content_text(payload.get("summary"), "summary_text")
            if text.strip():
                s.events.append({
                    "type": "assistant",
                    "uuid": f"codex-reasoning-{i}",
                    "timestamp": ts, "_dt": dt, "_i": i,
                    "cwd": s.cwd, "gitBranch": s.branch, "version": s.version,
                    "sessionId": s.session_id,
                    "message": {"role": "assistant", "id": f"codex-reason-{i}", "model": current_model,
                                "content": [{"type": "thinking", "thinking": text}]},
                })
        elif ptype in ("function_call", "custom_tool_call"):
            call_id = payload.get("call_id") or f"codex-call-{i}"
            inp = payload.get("arguments") if ptype == "function_call" else payload.get("input")
            ev = {
                "type": "assistant",
                "uuid": f"codex-tool-{i}",
                "timestamp": ts,
                "_dt": dt,
                "_i": i,
                "cwd": s.cwd,
                "gitBranch": s.branch,
                "version": s.version,
                "sessionId": s.session_id,
                "message": {"role": "assistant", "id": f"codex-tool-msg-{i}", "model": current_model,
                            "content": [{"type": "tool_use", "id": call_id,
                                         "name": _codex_tool_name(payload.get("name")),
                                         "input": _json_obj_maybe(inp)}]},
            }
            s.events.append(ev)
            last_assistant_event = ev
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id") or f"codex-call-{i}"
            s.events.append({
                "type": "user",
                "uuid": f"codex-tool-result-{i}",
                "timestamp": ts,
                "_dt": dt,
                "_i": i,
                "cwd": s.cwd,
                "gitBranch": s.branch,
                "version": s.version,
                "sessionId": s.session_id,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": payload.get("output")}
                ]},
            })

    dts = [e.get("_dt") for e in raw if e.get("_dt")]
    if dts:
        s.start, s.end = min(dts), max(dts)
    if s.cwd:
        s.proj_display = path_leaf_name(s.cwd, "codex")
    named = (thread_names or {}).get(s.session_id)
    if named:
        s.rename = named            # Codex『對話命名』，比照 Claude /rename 以 ✎ 顯示
        s.title = named
    else:
        s.title = first_user_text(s.events) or f"(無對話) {s.session_id[:8]}"
    return s


# /rename 會寫成 system/local_command 事件；名稱在 stdout 或指令 args 裡，取最後一次
_RENAME_OUT_RE = re.compile(r'Session renamed to:\s*"?(.+?)"?\s*(?:</local-command-stdout>|$)')
_RENAME_ARG_RE = re.compile(
    r'<command-name>/rename</command-name>.*?<command-args>\s*"?(.+?)"?\s*</command-args>', re.DOTALL)


def extract_rename(events):
    """取出使用者最後一次 /rename 設定的名稱（限定 system/local_command 事件，避免誤抓內文）。"""
    name = ""
    for e in events:
        if e.get("type") != "system" or e.get("subtype") != "local_command":
            continue
        content = e.get("content")
        if not isinstance(content, str):
            continue
        m = _RENAME_OUT_RE.search(content) or _RENAME_ARG_RE.search(content)
        if m:
            cand = m.group(1).strip().strip('"').strip()
            if cand:
                name = cand
    return name


def first_user_text(events):
    for e in sorted(events, key=lambda x: (x.get("_dt") or AWARE_MAX, x.get("_i", 0))):
        if e.get("type") != "user" or e.get("isSidechain") or is_noise_user(e):
            continue
        txt = clean_user_text(extract_user_text(e))
        txt = re.sub(r"<[^>]+>", "", txt)                # 去掉殘餘標籤
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            return txt[:90]
    return ""


def extract_user_text(ev):
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(b.get("text") or "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


# =========================================================================
# 對話整理：依時間排序、去重，再把被拆成多筆事件的 assistant 回合併回單一回合
# （新版格式的 parentUuid 串接會有斷點，不適合用父子鏈重建，改用時間順序最穩）
# =========================================================================
def ts_key(e):
    return (e.get("_dt") or AWARE_MIN, e.get("_i", 0))


# 純 CLI 指令 / 系統注入的包裝塊：呈現時去掉；整則清乾淨後沒內容就視為雜訊
_WRAP_RE = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|command-name|"
    r"command-message|command-args|bash-input|bash-stdout|bash-stderr)>.*?</\1>",
    re.DOTALL,
)


def clean_user_text(t):
    if not t:
        return ""
    t = str(t)
    return _WRAP_RE.sub("", t).strip()


def is_noise_user(ev):
    """整則只剩指令 / 系統提醒包裝（清乾淨後沒內容）→ 視為雜訊，不呈現。"""
    if ev.get("type") != "user":
        return False
    if ev.get("isMeta"):
        return True
    txt = extract_user_text(ev)
    return bool(txt.strip()) and clean_user_text(txt) == ""


def dedup(events):
    seen, out = set(), []
    for e in events:
        u = e.get("uuid")
        if u and u in seen:
            continue
        if u:
            seen.add(u)
        out.append(e)
    return out


def group_has_text(g):
    return any(b.get("type") == "text" and clean_user_text(b.get("text", ""))
              for b in g["blocks"])


def block_is_renderable(b, role):
    t = b.get("type")
    if t == "text":
        text = str(b.get("text") or "")
        return bool(clean_user_text(text) if role == "user" else text.strip())
    if t == "thinking":
        return bool(str(b.get("thinking") or b.get("text") or "").strip())
    if t in ("redacted_thinking", "tool_use", "image"):
        return True
    return False


def _new_turn_usage():
    return {"ids": set(), "input": 0, "cache_create": 0, "cache_read": 0,
            "output": 0, "ctx_max": 0, "cost": 0.0, "unpriced": False}


def _acc_turn_usage(acc, msg):
    """把單筆 assistant 訊息的 usage 累加到該回合（依 message.id 去重）。"""
    mid = msg.get("id")
    if mid:
        if mid in acc["ids"]:
            return
        acc["ids"].add(mid)
    u = msg.get("usage") or {}
    if not isinstance(u, dict):
        return
    i = usage_int(u, "input_tokens", "inputTokens")
    c1 = usage_int(u, "cache_creation_input_tokens", "cacheCreationInputTokens")
    c2 = usage_int(u, "cache_read_input_tokens", "cacheReadInputTokens")
    o = usage_int(u, "output_tokens", "outputTokens")
    acc["input"] += i; acc["cache_create"] += c1; acc["cache_read"] += c2; acc["output"] += o
    acc["ctx_max"] = max(acc["ctx_max"], i + c1 + c2)
    c5, c1h = _ephemeral_split(u)
    c = call_cost(msg.get("model"), i, c1, c2, o, c5, c1h)
    if c is None:
        acc["unpriced"] = True
    else:
        acc["cost"] += c


def _step_usage(msg):
    """單一步驟（＝一次 API 呼叫、一個 message.id）的 usage，供回合內 per-step 徽章使用。
    只取顯示需要的三項：cache_read（命中分子）、total_in（脈絡＝命中分母/ctx）、output（產出）。"""
    if not isinstance(msg, dict):
        return None
    u = msg.get("usage") or {}
    if not isinstance(u, dict):
        return None
    i = usage_int(u, "input_tokens", "inputTokens")
    c1 = usage_int(u, "cache_creation_input_tokens", "cacheCreationInputTokens")
    c2 = usage_int(u, "cache_read_input_tokens", "cacheReadInputTokens")
    o = usage_int(u, "output_tokens", "outputTokens")
    total_in = i + c1 + c2
    if total_in <= 0 and o <= 0:
        return None
    return {"cache_read": c2, "total_in": total_in, "output": o}


def group_turns(events, per_step=True):
    """events 須為已排序、去重的訊息事件。
    新版把 assistant 的 thinking/text/tool_use 拆成多筆事件，這裡併回單一回合；
    純 tool_result 的 user 事件不另起回合（其輸出已附在對應的 tool_use 內）。

    per_step：是否在回合內插入「步驟」分隔標記（逐步快取命中率）。僅適用於 Claude——
    它一次 API 呼叫的多筆事件共用同一個 message.id 且每筆都帶 usage。Codex 不適用：
    每個事件都有獨立合成 id（會過度切碎），且 usage 只掛在某一筆（多數步驟會空白），
    故 Codex 傳 False，只保留整段彙總。"""
    turns, cur = [], None
    for e in events:
        role = e.get("type")
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict) and block_is_renderable(b, role)]
        elif isinstance(content, str) and content.strip():
            blocks = [{"type": "text", "text": content}]
        else:
            blocks = []
        if role == "user":
            if is_noise_user(e):
                continue
            if blocks:
                if cur:
                    turns.append(cur)
                    cur = None
                turns.append({"role": "user", "blocks": blocks,
                              "dt": e.get("_dt"), "side": bool(e.get("isSidechain")),
                              "compact": bool(e.get("isCompactSummary")),
                              "compact_meta": e.get("_compact_meta") or {}})
            # 否則（純 tool_result / 空白）略過，不打斷 assistant 回合
        elif role == "assistant":
            if not blocks:
                continue
            if cur is None or cur["role"] != "assistant":
                if cur:
                    turns.append(cur)
                cur = {"role": "assistant", "blocks": [],
                       "dt": e.get("_dt"), "side": bool(e.get("isSidechain")),
                       "u": _new_turn_usage(), "n_steps": 0, "_step_ids": set()}
            msg = e.get("message") or {}
            # 每個新的 message.id ＝ 回合內的一個步驟（一次 API 呼叫）；同一 id 拆成的多筆事件
            # 只在首見時插入分隔標記，標記後緊接該步驟的 blocks（思考/文字/工具…）。
            if per_step:
                mid = msg.get("id")
                step_key = mid if mid else f"_anon{id(e)}"
                if step_key not in cur["_step_ids"]:
                    cur["_step_ids"].add(step_key)
                    cur["n_steps"] += 1
                    cur["blocks"].append({"type": "_step", "idx": cur["n_steps"],
                                          "u": _step_usage(msg)})
            cur["blocks"].extend(blocks)
            _acc_turn_usage(cur["u"], msg)
    if cur:
        turns.append(cur)
    return turns


def analyze(s):
    """建立 main/side 回合、tool 結果對照表與統計（render 前先呼叫一次）。"""
    msg = [e for e in s.events if e.get("type") in ("user", "assistant")]
    main = dedup(sorted([e for e in msg if not e.get("isSidechain")], key=ts_key))
    side = dedup(sorted([e for e in msg if e.get("isSidechain")], key=ts_key))
    per_step = s.source_kind != SOURCE_CODEX   # 逐步快取命中率僅適用 Claude（見 group_turns 說明）
    s.main_groups = group_turns(main, per_step=per_step)
    # 子代理依「父 Task tool_use id」分組，之後就地接在該 Task 底下（無法對應者退回頁尾）
    by_parent = {}
    for e in side:
        by_parent.setdefault(e.get("_parent_tool_use") or "", []).append(e)
    s.subagent_map = {}        # tool_use_id -> [turn groups]
    s.subagent_meta = {}       # tool_use_id -> {"type":..., "desc":...}
    for tid, evs in by_parent.items():
        groups = group_turns(evs, per_step=per_step)
        if groups:
            s.subagent_map[tid] = groups
            s.subagent_meta[tid] = {"type": evs[0].get("_agent_type", ""),
                                    "desc": evs[0].get("_agent_desc", "")}
    s.side_groups = [g for groups in s.subagent_map.values() for g in groups]
    # 全文搜尋用的穩定錨點：主對話 t{n}、子代理 s{n}。錨點掛在 group dict 上，
    # HTML（id 屬性）與 Markdown（{#…} 標記）讀同一份，兩種輸出必然一致。
    for i, g in enumerate(s.main_groups, 1):
        g["anchor"] = f"t{i}"
    for i, g in enumerate(s.side_groups, 1):
        g["anchor"] = f"s{i}"
    # 子代理數量：優先用不重複的 agentId（外部檔），否則退回分組數
    agent_ids = {e.get("agentId") for e in side if e.get("agentId")}
    s.n_subagents = len(agent_ids) if agent_ids else len(s.subagent_map)
    s.n_compacts = sum(1 for e in s.events if e.get("isCompactSummary"))
    s.tmap = build_tool_result_map(s.events)
    s.n_user = sum(1 for g in s.main_groups if g["role"] == "user")
    s.n_assistant = sum(1 for g in s.main_groups if g["role"] == "assistant" and group_has_text(g))
    s.n_tools = sum(1 for g in (s.main_groups + s.side_groups)
                    for b in g["blocks"] if b.get("type") == "tool_use")
    s.n_turns = len(s.main_groups) + len(s.side_groups)
    _collect_usage(s)
    s.cache_steps, s.cache_models, s.cache_events = collect_cache_steps(s)


def _collect_usage(s):
    """彙整模型、token、估算成本與快取命中率（依 message.id 去重，避免拆成多筆事件時重複計）。"""
    models, seen = [], set()
    inp = cc = cr = out = 0
    ctx_peak = 0
    cost = 0.0
    unpriced = False
    resume_ctx = 0          # 最後一筆主對話 assistant 的脈絡 = resume 後大約載入的 context
    resume_model = ""
    for e in s.events:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mdl = msg.get("model")
        if mdl and mdl not in models:
            models.append(mdl)
        mid = msg.get("id")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        u = msg.get("usage") or {}
        if not isinstance(u, dict):
            continue
        i = usage_int(u, "input_tokens", "inputTokens")
        c1 = usage_int(u, "cache_creation_input_tokens", "cacheCreationInputTokens")
        c2 = usage_int(u, "cache_read_input_tokens", "cacheReadInputTokens")
        o = usage_int(u, "output_tokens", "outputTokens")
        inp += i; cc += c1; cr += c2; out += o
        ctx_peak = max(ctx_peak, i + c1 + c2)
        if not e.get("isSidechain") and (i + c1 + c2) > 0:
            resume_ctx = i + c1 + c2          # 主對話按時間在後者覆蓋前者 → 最終為最後一筆
            resume_model = mdl or resume_model
        c5, c1h = _ephemeral_split(u)
        c = call_cost(mdl, i, c1, c2, o, c5, c1h)
        if c is None:
            unpriced = True
        else:
            cost += c
    total_in = inp + cc + cr
    s.models = models
    s.tok_out = out
    s.ctx_peak = ctx_peak
    s.resume_ctx = resume_ctx
    s.resume_model = resume_model
    s.cost = cost
    s.cost_partial = unpriced
    s.cache_pct = round(100 * cr / total_in) if total_in else 0
    s.usage = {"input": inp, "cache_create": cc, "cache_read": cr, "output": out, "total_in": total_in}


def collect_cache_steps(s):
    """主對話每次 assistant API 呼叫的時間序列與快取邊界事件，供 cache-report 分析。回傳 (steps, models, events)：
      steps  = [[epoch, cache_read, 脈絡tokens, 寫入總量, 寫入5分, 寫入1h, 模型idx], …]（依時間排序、
               message.id 去重；寫入5分/1h 來自 usage.cache_creation 細分，舊資料無細分為 0；
               模型idx 指向 models，未知 -1）
      models = steps 用到的模型字串表（去重存一次，免每步重複存字串）
      events = [[epoch, kind], …]，kind ∈ "limit"(429)／"auth"(401)／"compact"——快取邊界標記，
               報告據此把其後第一步歸因為切帳號/登入/壓縮，而非 TTL 失效。
    存原始 cache_read（非預先四捨五入的命中率），冷啟判定才能精確、不會在門檻邊界因進位而誤分類。
    只取主對話：子代理有獨立的快取前綴，混進來會污染間隔判讀。
    Codex 略過：其 usage 只掛在某一筆事件、id 為合成，逐步序列不可靠（見 [[jsonl-transcript-format]]）。"""
    if s.source_kind == SOURCE_CODEX:
        return [], [], []
    steps, seen = [], set()
    models, midx = [], {}
    evs = sorted((e for e in s.events
                  if e.get("type") == "assistant" and not e.get("isSidechain")),
                 key=ts_key)
    for e in evs:
        msg = e.get("message") or {}
        mid = msg.get("id")
        if mid:
            if mid in seen:            # 同一次呼叫被拆成多筆事件 → 只算一步，留首見（呼叫起點）的時間
                continue
            seen.add(mid)
        u = msg.get("usage") or {}
        if not isinstance(u, dict):
            continue
        i = usage_int(u, "input_tokens", "inputTokens")
        c1 = usage_int(u, "cache_creation_input_tokens", "cacheCreationInputTokens")
        c2 = usage_int(u, "cache_read_input_tokens", "cacheReadInputTokens")
        total_in = i + c1 + c2
        dt = e.get("_dt")
        if total_in <= 0 or not dt:
            continue
        c5, c1h = _ephemeral_split(u)
        mdl = str(msg.get("model") or "")
        if mdl and mdl not in midx:
            midx[mdl] = len(models)
            models.append(mdl)
        steps.append([int(dt.timestamp()), c2, total_in, c1, c5, c1h, midx.get(mdl, -1)])
    events = []
    for e in s.events:
        if e.get("isSidechain"):
            continue
        dt = e.get("_dt")
        if not dt:
            continue
        kind = None
        if e.get("isApiErrorMessage"):
            status = str(e.get("apiErrorStatus") or "")
            if status == "429":
                kind = "limit"
            elif status == "401":
                kind = "auth"
        elif e.get("type") == "system" and e.get("subtype") == "api_error":
            # 重試迴圈的錯誤記錄：status 在 error.status。僅認證失效（401，之後要 /login／可能換帳號）
            # 當快取邊界；5xx／逾時（status 常缺）是暫時性重試、快取未失效，不當邊界。實測無 429 此形態
            #（plan limit 是上面的 isApiErrorMessage 終止形態）。
            err = e.get("error")
            status = str(err.get("status") or "") if isinstance(err, dict) else ""
            if not status.isdigit():
                m = re.search(r"""['"]?status['"]?\s*[:=]\s*(\d+)""", str(err or ""))
                status = m.group(1) if m else ""
            if status == "401":
                kind = "auth"
        elif e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            kind = "compact"
        if kind:
            events.append([int(dt.timestamp()), kind])
    events.sort()
    return steps, models, events


def _step_cold(st):
    """cache_steps 的一步是否冷啟：cache_read / 脈絡 < CACHE_COLD_PCT%。
    用整數交叉相乘精確比較（cr*100 < 門檻*脈絡），避免先四捨五入命中率而在門檻邊界誤判。"""
    return st[1] * 100 < CACHE_COLD_PCT * st[2]


def usage_int(usage, *keys):
    for key in keys:
        if key not in usage:
            continue
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def build_tool_result_map(events):
    """tool_use_id -> (raw_content, is_error)；保留原始內容，渲染時才處理文字/圖片。"""
    tmap = {}
    for e in events:
        if e.get("type") != "user":
            continue
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    tmap[tid] = (b.get("content"), bool(b.get("is_error")))
    return tmap


def normalize_result(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text") or ""))
                elif b.get("type") == "image":
                    parts.append("[圖片]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


# =========================================================================
# 工具呼叫呈現
# =========================================================================
def tool_summary(name, inp):
    g = (lambda k: inp.get(k)) if isinstance(inp, dict) else (lambda k: None)
    table = {
        "Bash": lambda: first_line(g("command")),
        "Edit": lambda: g("file_path"),
        "MultiEdit": lambda: g("file_path"),
        "Write": lambda: g("file_path"),
        "Read": lambda: g("file_path"),
        "Grep": lambda: g("pattern"),
        "Glob": lambda: g("pattern"),
        "Task": lambda: f'{g("subagent_type") or ""} {first_line(g("description") or "")}'.strip(),
        "Agent": lambda: f'{g("subagent_type") or ""} {first_line(g("description") or "")}'.strip(),
        "WebFetch": lambda: g("url"),
        "WebSearch": lambda: g("query"),
        "TodoWrite": lambda: "更新待辦清單",
    }
    extra = table.get(name, lambda: "")()
    return f"{name} · {extra}" if extra else name


def _pre_text(text):
    text = "" if text is None else str(text)
    note = ""
    if len(text) > MAX_RESULT_CHARS:
        note = f"\n…（已截斷，共 {len(text):,} 字）"
        text = text[:MAX_RESULT_CHARS]
    return f"<pre>{esc(text)}{esc(note)}</pre>"


def _clean_base64_image_data(data):
    if not isinstance(data, str) or not data.strip():
        return None, 0, "invalid"
    compact = re.sub(r"\s+", "", data)
    approx = len(compact) * 3 // 4
    if approx > MAX_IMG_BYTES:
        return None, approx, "large"
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None, 0, "invalid"
    if len(raw) > MAX_IMG_BYTES:
        return None, len(raw), "large"
    return compact, len(raw), ""


def render_image_block(block):
    src = block.get("source") or {}
    if not isinstance(src, dict):
        return '<div class="img-ph">🖼️ 圖片</div>'
    if src.get("type") == "base64" and src.get("data"):
        media = str(src.get("media_type") or "image/png").lower()
        data, size, err = _clean_base64_image_data(src.get("data"))
        if media not in SAFE_IMAGE_MEDIA:
            return f'<div class="img-ph">🖼️ 圖片（{esc(media)}，格式不支援內嵌）</div>'
        if err == "large":
            return f'<div class="img-ph">🖼️ 圖片（{esc(media)}，約 {size // 1024} KB，過大未內嵌）</div>'
        if err:
            return f'<div class="img-ph">🖼️ 圖片（{esc(media)}，資料無效）</div>'
        return f'<img class="msg-img" alt="圖片" loading="lazy" src="data:{esc_attr(media)};base64,{esc_attr(data)}">'
    if src.get("url"):
        href = _safe_href(src["url"])
        if href and not href.lower().startswith("mailto:"):
            return f'<img class="msg-img" alt="圖片" loading="lazy" src="{esc_attr(href)}">'
    return '<div class="img-ph">🖼️ 圖片</div>'


def render_result_html(tmap, tid):
    if tid not in tmap:
        return '<div class="tres none">（無結果 / 已中斷）</div>'
    content, is_err = tmap[tid]
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image":
                parts.append(render_image_block(b))
            elif isinstance(b, dict) and b.get("type") == "text":
                parts.append(_pre_text(b.get("text", "")))
            elif isinstance(b, dict):
                parts.append(_pre_text(json.dumps(b, ensure_ascii=False)))
            else:
                parts.append(_pre_text(str(b)))
    else:
        parts.append(_pre_text(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)))
    cls = "tres err" if is_err else "tres"
    lbl = "錯誤輸出" if is_err else "輸出"
    return f'<div class="{cls}"><div class="lbl">{lbl}</div>{"".join(parts)}</div>'


def render_tool_html(block, tmap, used_ids):
    name = block.get("name", "tool")
    inp = block.get("input", {})
    tid = block.get("id")
    if tid:
        used_ids.add(tid)
    summary = tool_summary(name, inp)

    if name in ("Edit", "MultiEdit") and isinstance(inp, dict):
        edits = inp.get("edits") or [{"old_string": inp.get("old_string", ""),
                                      "new_string": inp.get("new_string", "")}]
        diff = []
        for ed in edits:
            for ln in str(ed.get("old_string", "")).split("\n"):
                diff.append('<span class="del">- ' + esc(ln) + "</span>")
            for ln in str(ed.get("new_string", "")).split("\n"):
                diff.append('<span class="ins">+ ' + esc(ln) + "</span>")
        body = f'<div class="fp">{esc(inp.get("file_path",""))}</div><pre class="diff">' + "\n".join(diff) + "</pre>"
    elif name == "Bash" and isinstance(inp, dict):
        desc = inp.get("description")
        cap = f'<div class="cap">{esc(desc)}</div>' if desc else ""
        body = cap + f'<pre class="cmd">{esc(inp.get("command",""))}</pre>'
    elif name == "Write" and isinstance(inp, dict):
        c = str(inp.get("content", ""))
        if len(c) > MAX_WRITE_CHARS:
            c = c[:MAX_WRITE_CHARS] + "\n…（截斷）"
        body = f'<div class="fp">{esc(inp.get("file_path",""))}</div><pre>{esc(c)}</pre>'
    elif isinstance(inp, str):
        body = f"<pre>{esc(inp)}</pre>"
    elif name == "TodoWrite" and isinstance(inp, dict):
        icons = {"completed": "✅", "in_progress": "▶️", "pending": "⬜"}
        todos = inp.get("todos", [])
        if not isinstance(todos, list):
            todos = []
        items = "".join(
            f'<li>{icons.get(t.get("status"),"•")} {esc(t.get("content",""))}</li>'
            for t in todos if isinstance(t, dict))
        body = f'<ul class="todos">{items}</ul>'
    else:
        try:
            pretty = json.dumps(inp, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(inp)
        body = f"<pre>{esc(pretty)}</pre>"

    res = render_result_html(tmap, tid)
    return (f'<details class="tool"><summary><span class="ti">🔧</span> {esc(summary)}</summary>'
            f'<div class="tbody">{body}{res}</div></details>')


# =========================================================================
# 單則訊息 / 整個 session 呈現
# =========================================================================
def coldest_step(group):
    """回合內命中率最低的步驟（從 _step 標記算）→ (pct, idx)；無逐步資料回 (None, None)。
    冷啟動不一定在首步，也不一定要長間隔：可能是 resume／長閒置後 TTL 過期，也可能是回合內
    快取斷點的一次性 miss（實測同一晚間隔 18s 的中途步驟也會 0%）。成因不一而足，故只取「最低的那一步」。"""
    best_pct, best_idx = None, None
    for b in group.get("blocks", []):
        if b.get("type") != "_step":
            continue
        su = b.get("u")
        if not su or su["total_in"] <= 0:
            continue
        p = round(100 * su["cache_read"] / su["total_in"])
        if best_pct is None or p < best_pct:
            best_pct, best_idx = p, b.get("idx")
    return best_pct, best_idx


def render_turn_meters(group):
    """assistant 回合的小徽章：快取% / 估算花費 / 脈絡 / 產出（可由頁面勾選顯示）。
    若整段彙總本身是冷啟動則 ⚡ 轉紅；多步回合另標「最低 N%·步驟K」紅徽，揭露被彙總藏住的冷啟動步驟。"""
    u = group.get("u")
    if group.get("role") != "assistant" or not u:
        return ""
    total_in = u["input"] + u["cache_create"] + u["cache_read"]
    if total_in <= 0 and u["output"] <= 0:
        return ""
    pct = round(100 * u["cache_read"] / total_in) if total_in else 0
    cold = " cold" if pct < CACHE_COLD_PCT else ""
    cost = cost_label(u["cost"], u["unpriced"])
    out = (f'<span class="meter m-cache{cold}" title="快取命中率（整段彙總）">⚡{pct}%</span>'
           f'<span class="meter m-cost" title="估算花費">~{esc(cost)}</span>'
           f'<span class="meter m-ctx" title="脈絡 tokens（input+cache）">ctx {fmt_tokens(u["ctx_max"])}</span>'
           f'<span class="meter m-out" title="產出 tokens">↑{fmt_tokens(u["output"])}</span>')
    if group.get("n_steps", 0) >= 2:
        mp, mi = coldest_step(group)
        if mp is not None and mp < CACHE_COLD_PCT:
            out = (f'<span class="meter m-cache cold" '
                   f'title="回合內命中率最低的步驟。冷啟動成因可能是長閒置／resume 後快取過期，'
                   f'也可能是回合內快取斷點的一次性 miss（不一定要長間隔）">'
                   f'❄最低 {mp}%·步驟{mi}</span>') + out
    return out


def render_step_meters(idx, u):
    """回合內單一步驟（API 呼叫）的分隔列：步驟序號＋該步的快取% / ctx / 產出。
    沿用 m-cache/m-ctx/m-out class，所以跟整段彙總共用同一組勾選開關（cost 不在步驟層顯示）。
    該步為冷啟動（命中率 < CACHE_COLD_PCT）時 ⚡ 轉紅底白字。"""
    if not u:
        return f'<div class="step-sep"><span class="step-n">步驟 {idx}</span></div>'
    pct = round(100 * u["cache_read"] / u["total_in"]) if u["total_in"] else 0
    cold = " cold" if pct < CACHE_COLD_PCT else ""
    return (f'<div class="step-sep"><span class="step-n">步驟 {idx}</span>'
            f'<span class="meter m-cache{cold}" title="此步驟快取命中率">⚡{pct}%</span>'
            f'<span class="meter m-ctx" title="此步驟脈絡 tokens（input+cache）">ctx {fmt_tokens(u["total_in"])}</span>'
            f'<span class="meter m-out" title="此步驟產出 tokens">↑{fmt_tokens(u["output"])}</span></div>')


def subagent_summary_label(meta):
    """子代理摺疊框標題：類型＋描述（取自 .meta.json）。"""
    typ = first_line((meta or {}).get("type") or "")
    desc = first_line((meta or {}).get("desc") or "")
    head = f"子代理 {esc(typ)}" if typ else "子代理"
    return f"{head} · {esc(desc)}" if desc else head


def subagent_anchor(tid):
    """子代理摺疊框的頁內錨點 id；孤兒（無對應 Task）統一指向頁尾總區。"""
    if not tid:
        return "sub-orphans"
    return "sub-" + re.sub(r"[^A-Za-z0-9_-]", "-", tid)


def render_subagent_block(tid, smap, smeta, tmap, used_ids, rendered):
    """把某個 Task 派出的子代理對話，算繪成就地的摺疊區（可遞迴處理巢狀子代理）。"""
    if not tid or tid in rendered or tid not in smap:
        return ""
    rendered.add(tid)
    inner = [render_turn_html(g, tmap, used_ids, smap, smeta, rendered) for g in smap[tid]]
    inner = [x for x in inner if x]
    if not inner:
        return ""
    return (f'<details id="{subagent_anchor(tid)}" class="sidechain-wrap"><summary>↳ '
            + subagent_summary_label(smeta.get(tid))
            + f' · {len(inner)} 則</summary><div class="tbody">' + "".join(inner) + "</div></details>")


def render_turn_html(group, tmap, used_ids, subagent_map=None, subagent_meta=None, rendered_sub=None):
    role = group["role"]
    aid = f' id="{group["anchor"]}"' if group.get("anchor") else ""
    if group.get("compact"):
        # /compact 壓縮點：標示分隔線（手動/自動＋壓縮量），摘要收進摺疊（此摘要即下次送模型 context 的起點）
        summ = "".join(md_to_html(b.get("text") or "")
                       for b in group["blocks"] if b.get("type") == "text" and (b.get("text") or "").strip())
        m = group.get("compact_meta") or {}
        trig = {"manual": "手動 /compact", "auto": "自動壓縮"}.get(m.get("trigger"), "壓縮 (compact)")
        bits = [trig]
        pre, post = m.get("preTokens"), m.get("postTokens")
        if pre and post:
            bits.append(f"{fmt_tokens(pre)} → {fmt_tokens(post)} tokens")
        when = local_str(group.get("dt"), "%H:%M:%S")
        if when:
            bits.append(when)
        return (f'<div class="compact-sep"{aid}><span>✂ {esc(" · ".join(bits))}'
                ' — 先前對話已壓縮為摘要，以下即下次送模型 context 的起點</span></div>'
                '<details class="think compact-sum"><summary>📋 壓縮摘要</summary>'
                f'<div class="tbody">{summ}</div></details>')
    parts = []
    multi_step = group.get("n_steps", 0) >= 2   # 單步回合不必逐步標示（與整段彙總相同）
    for b in group["blocks"]:
        t = b.get("type")
        if t == "_step":
            if multi_step:
                parts.append(render_step_meters(b.get("idx", 0), b.get("u")))
            continue
        if t == "text":
            raw = b.get("text") or ""
            txt = clean_user_text(raw) if role == "user" else str(raw)
            if txt.strip():
                parts.append(md_to_html(txt))
        elif t == "thinking":
            think = b.get("thinking") or b.get("text") or ""
            if think.strip():
                parts.append('<details class="think"><summary>💭 思考</summary>'
                             f'<div class="tbody">{md_to_html(think)}</div></details>')
        elif t == "redacted_thinking":
            parts.append('<div class="think-redacted">💭 思考（已隱藏）</div>')
        elif t == "tool_use":
            parts.append(render_tool_html(b, tmap, used_ids))
            if subagent_map:
                parts.append(render_subagent_block(b.get("id"), subagent_map, subagent_meta or {},
                                                   tmap, used_ids, rendered_sub if rendered_sub is not None else set()))
        elif t == "image":
            parts.append(render_image_block(b))

    if not parts:
        return ""

    icon = "👤" if role == "user" else "🤖"
    who = "你" if role == "user" else "Claude"
    side_cls = " side" if group.get("side") else ""
    side_badge = ' <span class="badge">↳ 子代理</span>' if group.get("side") else ""
    dt = group.get("dt")
    when = local_str(dt, "%H:%M:%S")
    when_full = (day_label(dt) + " " + when) if dt else ""   # 游標停留顯示完整日期
    meters = render_turn_meters(group)
    return (f'<div class="turn {role}{side_cls}"{aid}>'
            f'<div class="head"><span class="who">{icon} {who}</span>{side_badge}'
            f'{meters}<span class="when" title="{esc_attr(when_full)}">{esc(when)}</span></div>'
            f'<div class="body">{"".join(parts)}</div></div>')


def render_session_html(s: Session, index_href: str, memory_href: str = "") -> str:
    if not hasattr(s, "main_groups"):
        analyze(s)
    smap = getattr(s, "subagent_map", {})
    smeta = getattr(s, "subagent_meta", {})
    used = set()
    rendered_sub = set()
    # 主對話：換日時插入日期分隔線
    turns = []
    last_day = None
    for g in s.main_groups:
        h = render_turn_html(g, s.tmap, used, smap, smeta, rendered_sub)
        if not h:
            continue
        d = local_str(g.get("dt"), "%Y-%m-%d") if g.get("dt") else ""
        if d and d != last_day:
            turns.append(f'<div class="day-sep"><span>{esc(day_label(g.get("dt")))}</span></div>')
            last_day = d
        turns.append(h)

    inline_tids = set(rendered_sub)   # 已就地接在 Task 底下的子代理

    # 只有無法對應到任何 Task 的（孤兒）子代理才退回頁尾
    side_html = ""
    orphan_tids = [tid for tid in smap if tid not in rendered_sub]
    if orphan_tids:
        blocks = []
        for tid in orphan_tids:
            rendered_sub.add(tid)
            inner = [render_turn_html(g, s.tmap, used, smap, smeta, rendered_sub) for g in smap[tid]]
            blocks += [x for x in inner if x]
        if blocks:
            side_html = ('<details id="sub-orphans" class="sidechain-wrap"><summary>↳ 子代理（subagent）對話 · '
                         f'{len(blocks)} 則</summary><div class="tbody">'
                         + "".join(blocks) + "</div></details>")

    # 頁頂子代理清單（點擊跳到頁內就地的子代理區並自動展開）
    sub_toc = ""
    if smap:
        items = []
        for tid in smap:
            anchor = subagent_anchor(tid) if tid in inline_tids else "sub-orphans"
            label = subagent_summary_label(smeta.get(tid))
            items.append(f'<li><a href="#{anchor}" onclick="return openSub(\'{anchor}\')">↳ {label}</a></li>')
        sub_toc = (f'<details class="sub-toc" open><summary>🧩 子代理 ({s.n_subagents})</summary>'
                   f'<ul>{"".join(items)}</ul></details>')

    meta = " · ".join(x for x in [
        esc(s.proj_display),
        esc(s.cwd),
        (f"帳號: {esc(s.account)}" if s.account else ""),
        (f"夾: {esc(s.proj_munged)}"
         if (s.source_kind == SOURCE_CLAUDE and s.proj_munged and s.proj_munged != s.proj_display) else ""),
        (f"branch: {esc(s.branch)}" if s.branch else ""),
        (f"v{esc(s.version)}" if s.version else ""),
    ] if x)
    when = ""
    if s.start:
        when = local_str(s.start) + (f" – {local_str(s.end,'%H:%M')}" if s.end else "")
        when += f" · {fmt_dur((s.end - s.start).total_seconds()) if s.end else ''}"
    usage = ""
    if s.models:
        usage += " · 🤖 " + esc(", ".join(short_model(m) for m in s.models))
    if s.usage.get("total_in") or s.tok_out:
        cost = cost_label(s.cost, s.cost_partial)
        usage += (f" · 💲~{cost} · ⚡快取 {s.cache_pct}%"
                  f" · 脈絡峰值 {fmt_tokens(s.ctx_peak)} · 產出 {fmt_tokens(s.tok_out)}")
    rc = getattr(s, "resume_ctx", 0)
    if rc:
        w = context_window(getattr(s, "resume_model", "") or (s.models[-1] if s.models else ""))
        pct = f"（≈{round(100 * rc / w)}% / {fmt_tokens(w)}）" if w else ""
        usage += f" · ↩ resume 約 ~{fmt_tokens(rc)}{pct}"
    stats = f"💬 {s.n_user} 問 / {s.n_assistant} 答 · 🔧 {s.n_tools} 次工具呼叫{usage}"
    h1 = f'<span class="named">✎ {esc(s.title)}</span>' if s.rename else esc(s.title)
    sub = (f'<div class="smeta">自動標題：{esc(s.ai_title)}</div>'
           if (s.rename and s.ai_title and s.ai_title != s.title) else "")

    body = f"""
<div class="wrap">
  <div class="topbar">
    <span>
      <a class="back" href="{index_href}">← 回索引</a>
      {f'<a class="back memlink" href="{esc_attr(memory_href)}" title="此專案 memory">🧠 專案 memory</a>' if memory_href else ''}
    </span>
    <span class="ctrl"><button onclick="toggleAll(true)">展開全部</button>
    <button onclick="toggleAll(false)">收合全部</button></span>
  </div>
  <div class="meterbar">每則顯示：
    <label><input type="checkbox" id="cb_cache" onchange="tm('cache')">⚡快取%</label>
    <label><input type="checkbox" id="cb_cost" onchange="tm('cost')">💲花費</label>
    <label><input type="checkbox" id="cb_ctx" onchange="tm('ctx')">脈絡</label>
    <label><input type="checkbox" id="cb_out" onchange="tm('out')">產出</label>
  </div>
  <h1>{h1}</h1>
  {sub}
  <div class="smeta">{meta}</div>
  <div class="smeta">{esc(when)} · {stats} · <span class="mono">{esc(s.session_id)}</span></div>
  {sub_toc}
  <div class="thread">{''.join(turns)}</div>
  {side_html}
</div>
<script>
function toggleAll(o){{document.querySelectorAll('details.tool,details.think,details.sidechain-wrap').forEach(function(d){{d.open=o;}});}}
function openSub(id){{var el=document.getElementById(id);if(!el)return true;
 document.querySelectorAll('.hl').forEach(function(x){{x.classList.remove('hl');}});
 var p=el;while(p){{if(p.tagName==='DETAILS')p.open=true;p=p.parentElement;}}
 if(el.classList.contains('turn')){{[].slice.call(el.querySelectorAll('details')).forEach(function(d){{
  if(d.classList.contains('sidechain-wrap'))return;
  var sw=d.closest('.sidechain-wrap');
  if(sw&&el.contains(sw))return;
  d.open=true;}});}}
 else if(el.classList.contains('compact-sep')){{var n=el.nextElementSibling;
  if(n&&n.tagName==='DETAILS')n.open=true;}}
 el.classList.add('hl');
 el.scrollIntoView({{behavior:'smooth',block:'start'}});history.replaceState(null,'',  '#'+id);return false;}}
if(location.hash.length>1){{setTimeout(function(){{openSub(location.hash.slice(1));}},0);}}
var MET=['cache','cost','ctx','out'],DEF={{cache:1,cost:1,ctx:0,out:0}};
function lsGet(k){{try{{return localStorage.getItem(k);}}catch(e){{return null;}}}}
function lsSet(k,v){{try{{localStorage.setItem(k,v);}}catch(e){{}}}}
function applyMet(){{MET.forEach(function(k){{var v=lsGet('m_'+k);v=(v===null)?DEF[k]:(v==='1'?1:0);document.body.classList.toggle('hide-'+k,!v);var cb=document.getElementById('cb_'+k);if(cb)cb.checked=!!v;}});}}
function tm(k){{var cb=document.getElementById('cb_'+k);lsSet('m_'+k,cb.checked?'1':'0');document.body.classList.toggle('hide-'+k,!cb.checked);}}
applyMet();
</script>
"""
    return html_page(s.title, body, body_class="hide-ctx hide-out")


# =========================================================================
# Markdown 輸出
# =========================================================================
def render_result_md(tmap, tid):
    if tid not in tmap:
        return "\n> （無結果 / 已中斷）\n"
    content, is_err = tmap[tid]
    text = normalize_result(content)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…（已截斷，共 {len(text):,} 字）"
    lbl = "錯誤輸出" if is_err else "輸出"
    return f"\n**{lbl}：**\n\n{_md_fence(text)}\n"


def render_tool_md(block, tmap):
    name = block.get("name", "tool")
    inp = block.get("input", {})
    tid = block.get("id")
    summary = tool_summary(name, inp)
    if name == "Bash" and isinstance(inp, dict):
        inner = _md_fence(inp.get("command", ""), "bash")
    elif name in ("Edit", "MultiEdit") and isinstance(inp, dict):
        edits = inp.get("edits") or [{"old_string": inp.get("old_string", ""),
                                      "new_string": inp.get("new_string", "")}]
        diff = []
        for ed in edits:
            for ln in str(ed.get("old_string", "")).split("\n"):
                diff.append("- " + ln)
            for ln in str(ed.get("new_string", "")).split("\n"):
                diff.append("+ " + ln)
        inner = f"`{inp.get('file_path','')}`\n" + _md_fence("\n".join(diff), "diff")
    elif isinstance(inp, str):
        inner = _md_fence(inp, "diff" if name == "Patch" else "")
    else:
        try:
            inner = _md_fence(json.dumps(inp, ensure_ascii=False, indent=2), "json")
        except Exception:
            inner = _md_fence(inp)
    return (f"\n<details><summary>🔧 {summary}</summary>\n\n{inner}\n"
            f"{render_result_md(tmap, tid)}\n</details>\n")


def turn_meters_md(group):
    u = group.get("u")
    if group.get("role") != "assistant" or not u:
        return ""
    total_in = u["input"] + u["cache_create"] + u["cache_read"]
    if total_in <= 0 and u["output"] <= 0:
        return ""
    pct = round(100 * u["cache_read"] / total_in) if total_in else 0
    cold = ""
    if group.get("n_steps", 0) >= 2:
        mp, mi = coldest_step(group)
        if mp is not None and mp < CACHE_COLD_PCT:
            cold = f" · ❄最低 {mp}%（步驟{mi}）"
    cost = cost_label(u["cost"], u["unpriced"])
    return f"  ·  ⚡{pct}%{cold} · ~{cost} · ctx {fmt_tokens(u['ctx_max'])} · ↑{fmt_tokens(u['output'])}"


def render_turn_md(group, tmap):
    role = group["role"]
    parts = []
    for b in group["blocks"]:
        t = b.get("type")
        if t == "text":
            raw = b.get("text") or ""
            txt = clean_user_text(raw) if role == "user" else str(raw)
            if txt.strip():
                parts.append(txt)
        elif t == "thinking":
            think = (b.get("thinking") or b.get("text") or "").strip()
            if think:
                parts.append(f"<details><summary>💭 思考</summary>\n\n{think}\n\n</details>")
        elif t == "tool_use":
            parts.append(render_tool_md(b, tmap))
        elif t == "image":
            parts.append("_[圖片]_")
    if not parts:
        return ""
    icon = "👤 You" if role == "user" else "🤖 Claude"
    side = "↳ " if group.get("side") else ""
    when = local_str(group.get("dt"), "%H:%M:%S")
    meters = turn_meters_md(group)
    # {#tN}/{#sN}＝對應 HTML 該則的錨點 id：--search 用它定位，手動 rg 到後也可接在 .html# 後跳到該則
    mark = f" {{#{group['anchor']}}}" if group.get("anchor") else ""
    return f"\n### {side}{icon} · {when}{meters}{mark}\n\n" + "\n\n".join(parts) + "\n"


def render_session_md(s: Session) -> str:
    if not hasattr(s, "main_groups"):
        analyze(s)
    head = [
        f"# {s.title}", "",
        (f"- 自動標題：{s.ai_title}" if (s.rename and s.ai_title and s.ai_title != s.title) else None),
        f"- 專案：`{s.proj_display}`",
        (f"- 工作目錄：`{s.cwd}`" if s.cwd else None),
        (f"- 帳號：`{s.account}`" if s.account else None),
        (f"- 夾：`{s.proj_munged}`"
         if (s.source_kind == SOURCE_CLAUDE and s.proj_munged and s.proj_munged != s.proj_display) else None),
        f"- 時間：{local_str(s.start)}" + (f" – {local_str(s.end,'%H:%M')}" if s.end else ""),
        (f"- 分支：`{s.branch}`" if s.branch else None),
        (f"- 模型：{', '.join(short_model(m) for m in s.models)}" if s.models else None),
        (f"- 估算花費：~{cost_label(s.cost, s.cost_partial)} · 快取命中 {s.cache_pct}%"
         if (s.usage.get("total_in") or s.tok_out) else None),
        (f"- 脈絡峰值：{fmt_tokens(s.ctx_peak)} · 產出 {fmt_tokens(s.tok_out)}"
         if (s.usage.get("total_in") or s.tok_out) else None),
        f"- Session：`{s.session_id}` · v{s.version}",
        "", "---",
    ]
    parts = [render_turn_md(g, s.tmap) for g in s.main_groups]
    parts = [p for p in parts if p]
    out = "\n".join(x for x in head if x is not None) + "\n" + "\n".join(parts)
    if s.side_groups:
        sparts = [render_turn_md(g, s.tmap) for g in s.side_groups]
        sparts = [p for p in sparts if p]
        if sparts:
            out += "\n\n---\n\n## ↳ 子代理（subagent）對話\n" + "\n".join(sparts)
    return out


# =========================================================================
# Claude 專案 memory（~/.claude/projects/<專案>/memory/）
# =========================================================================
_MEM_TYPE_LABEL = {"user": "使用者", "feedback": "回饋", "project": "專案", "reference": "參考"}
_MEM_TYPE_ORDER = {"user": 0, "feedback": 1, "project": 2, "reference": 3}
_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9._\-]+)\]\]")


def _parse_front_matter(text):
    """極簡 YAML frontmatter 解析（只需頂層 key: value 與單層 metadata: 巢狀）。
    回傳 (meta: dict, body: str)；沒有 frontmatter 就回 ({}, 原文)。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta, nested = {}, None
    for ln in lines[1:end]:
        if not ln.strip() or ":" not in ln:
            continue
        indent = len(ln) - len(ln.lstrip())
        k, _, v = ln.strip().partition(":")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if indent == 0:
            if v == "":
                nested = {}
                meta[k] = nested
            else:
                meta[k] = v
                nested = None
        elif nested is not None:
            nested[k] = v
    return meta, "\n".join(lines[end + 1:])


def _mem_anchor(slug):
    return "mem-" + re.sub(r"[^A-Za-z0-9_\-]", "-", str(slug))


_CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)


def _apply_outside_code(md_text, fn):
    """只在「非程式碼」段落套用 fn（保護 code fence 與行內碼，避免改寫程式碼示例裡的標記）。"""
    parts = _CODE_SPAN_RE.split(md_text)
    spans = _CODE_SPAN_RE.findall(md_text)
    out = []
    for i, seg in enumerate(parts):
        out.append(fn(seg))
        if i < len(spans):
            out.append(spans[i])          # 原樣保留程式碼段
    return "".join(out)


def _link_wikilinks(md_text):
    """事實檔內文的 [[slug]] → 指向同頁 memory 卡片的錨點連結（不動程式碼段）。"""
    return _apply_outside_code(
        md_text, lambda s: _WIKILINK_RE.sub(lambda m: f"[{m.group(1)}](#{_mem_anchor(m.group(1))})", s))


def _link_memory_index(md_text):
    """MEMORY.md 的 [Title](slug.md) → 同頁錨點 [Title](#mem-slug)（不動程式碼段）。"""
    return _apply_outside_code(
        md_text, lambda s: re.sub(r"\]\(([A-Za-z0-9._\-]+)\.md\)",
                                  lambda m: f"](#{_mem_anchor(m.group(1))})", s))


def memory_rel_path(account, proj_munged):
    """memory 頁相對 out 的路徑；用完整 munged 的 hash 後綴，避免不同專案截斷後檔名碰撞。
    16 碼（64-bit）後綴讓碰撞機率趨近於零（生日界約 40 億專案才 50%）。"""
    safe_acc = safe_name(account or "default", 24)
    h = hashlib.sha1(str(proj_munged).encode("utf-8")).hexdigest()[:16]
    return f"memory/claude-code/{safe_acc}/{safe_name(proj_munged, 60)}-{h}.html"


def load_memory_dir(mem_dir: Path):
    """讀一個專案的 memory/ 目錄；回傳 {index_md, docs:[...]}，沒有有效內容回 None。"""
    if not mem_dir.is_dir():
        return None
    index_md, docs = "", []
    for p in sorted(mem_dir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if p.name == "MEMORY.md":
            index_md = text
            continue
        meta, body = _parse_front_matter(text)
        md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        docs.append({
            "slug": p.stem,
            "name": str(meta.get("name") or p.stem),
            "desc": str(meta.get("description") or ""),
            "type": str(md.get("type") or "").strip(),
            "body": body,
        })
    if not docs and not index_md.strip():
        return None
    return {"index_md": index_md, "docs": docs}


def render_memory_html(mem, proj_display, proj_munged, index_href) -> str:
    """把一個專案的 memory 渲染成一頁：MEMORY.md 總覽 + 每則事實一張卡（type 標籤＋描述＋內文）。"""
    docs = sorted(mem["docs"], key=lambda d: (_MEM_TYPE_ORDER.get(d["type"], 9), d["name"].lower()))
    cards = []
    for d in docs:
        t = d["type"]
        chip = (f'<span class="chip mtype t-{esc_attr(t or "x")}">{esc(_MEM_TYPE_LABEL.get(t, t or "—"))}</span>')
        summary = f'{chip} <span class="mname">{esc(d["name"])}</span>'
        if d["desc"]:
            summary += f' <span class="mdesc">— {esc(first_line(d["desc"], 90))}</span>'
        body_html = md_to_html(_link_wikilinks(d["body"]))
        cards.append(f'<details id="{_mem_anchor(d["slug"])}" class="mcard"><summary>{summary}</summary>'
                     f'<div class="tbody">{body_html}</div></details>')
    overview = ""
    if mem["index_md"].strip():
        overview = ('<details class="mcard mindex" open><summary>📋 MEMORY.md 總覽</summary>'
                    f'<div class="tbody">{md_to_html(_link_memory_index(mem["index_md"]))}</div></details>')
    body = f"""
<div class="wrap">
  <div class="topbar"><a class="back" href="{esc_attr(index_href)}">← 回索引</a>
    <span class="ctrl"><button onclick="toggleAllM(true)">展開全部</button>
    <button onclick="toggleAllM(false)">收合全部</button></span></div>
  <h1>🧠 {esc(proj_display)} · memory</h1>
  <div class="smeta"><span class="mono">{esc(proj_munged)}</span> · {len(docs)} 則 memory</div>
  {overview}
  {''.join(cards)}
</div>
<script>
function toggleAllM(o){{document.querySelectorAll('details.mcard').forEach(function(d){{d.open=o;}});}}
if(location.hash.length>1){{var el=document.getElementById(location.hash.slice(1));if(el)el.open=true;}}
</script>
"""
    return html_page(f"{proj_display} memory", body)


# =========================================================================
# 索引頁
# =========================================================================
def cost_summary(rows):
    """估算花費摘要：單一來源直接顯示總額；多來源則分來源列出（未估價的來源標『不估價』，
    避免某個來源沒價格就把整體總花費弄成 +?）。"""
    if not rows:
        return ""
    by_src = {}
    for r in rows:
        src = r.get("source_kind", SOURCE_CLAUDE)
        c, p = by_src.get(src, (0.0, False))
        by_src[src] = (c + (r.get("cost") or 0), p or bool(r.get("cost_partial")))
    if len(by_src) == 1:
        (c, p), = by_src.values()
        if c <= 0 and p:
            return " · 估算總花費 不估價"
        return " · 估算總花費 ~" + cost_label(c, p)
    parts = []
    for src in sorted(by_src):
        c, p = by_src[src]
        parts.append(f"{source_label(src)} 不估價" if (c <= 0 and p) else f"{source_label(src)} ~{cost_label(c, p)}")
    return " · 估算花費：" + " · ".join(parts)


def render_index_html(rows, show_account=False, cache_report=False) -> str:
    rows = sorted(rows, key=lambda r: r.get("start_ts") or 0, reverse=True)
    projects = sorted({r["proj"] for r in rows})
    months = sorted({r["month"] for r in rows if r.get("month")}, reverse=True)
    accounts = sorted({r.get("account", "") for r in rows if r.get("account")})
    sources = sorted({r.get("source_kind", SOURCE_CLAUDE) for r in rows})
    show_source = len(sources) > 1
    proj_opts = "".join(f'<option value="{esc_attr(p)}">{esc(p)}</option>' for p in projects)
    month_opts = "".join(f'<option value="{esc_attr(m)}">{esc(m)}</option>' for m in months)
    acc_opts = "".join(f'<option value="{esc_attr(a)}">{esc(a)}</option>' for a in accounts)
    src_opts = "".join(f'<option value="{esc_attr(src)}">{esc(source_label(src))}</option>' for src in sources)
    acc_select = (f'<select id="fa" onchange="af()"><option value="">全部帳號/來源</option>{acc_opts}</select>'
                  if show_account else "")
    src_select = (f'<select id="fs" onchange="af()"><option value="">全部工具</option>{src_opts}</select>'
                  if show_source else "")
    acc_th = "<th>帳號/來源</th>" if show_account else ""
    src_th = "<th>工具</th>" if show_source else ""
    tr = []
    for r in rows:
        acc = r.get("account", "")
        src = r.get("source_kind", SOURCE_CLAUDE)
        chip_title = (("夾 " + r.get("proj_munged", "") + (" · " + r["cwd"] if r.get("cwd") else ""))
                      if src == SOURCE_CLAUDE else r.get("cwd", ""))   # Codex 的 proj_munged 不是真夾，不標「夾」
        ai = r.get("ai_title", "")
        blob = (source_label(src) + " " + acc + " " + r["proj"] + " "
                + r.get("cwd", "") + " " + r.get("proj_munged", "") + " "
                + r["title"] + " " + ai + " " + r.get("branch", "")).lower()
        acc_td = f'<td><span class="chip acc">{esc(acc)}</span></td>' if show_account else ""
        src_td = f'<td><span class="chip src">{esc(source_label(src))}</span></td>' if show_source else ""
        ns = r.get("n_subagents") or 0
        nc = r.get("n_compacts") or 0
        sub_badge = (f' <span class="chip sub" title="包含 {ns} 個子代理對話（其他 JSONL 內容）">🧩 ×{ns}</span>'
                     if ns else "")
        sub_badge += (f' <span class="chip compact" title="此 session 發生 {nc} 次壓縮（手動 /compact 或自動）">✂ ×{nc}</span>'
                      if nc else "")
        if r.get("rename"):
            title_html = f'<span class="named">✎ {esc(r["title"])}</span>{sub_badge}'
            if ai and ai != r["title"]:
                title_html += f'<div class="subtitle">{esc(ai)}</div>'
        else:
            title_html = esc(r["title"]) + sub_badge
        mem_link = (f'<a class="memlink" href="{esc_attr(r["mem_href"])}" title="此專案 memory">🧠</a>'
                    if r.get("mem_href") else "")
        cost_cell = cost_label(r.get("cost", 0) or 0, r.get("cost_partial"))
        rc = r.get("resume_ctx") or 0
        rw = r.get("resume_window")
        rc_title = f' title="≈{round(100 * rc / rw)}% / {fmt_tokens(rw)}"' if (rc and rw) else ""
        resume_cell = (f'<td class="num" data-sort="{rc}"{rc_title}>~{fmt_tokens(rc)}</td>'
                       if rc else '<td class="num" data-sort="0"></td>')
        tr.append(
            f'<tr data-source="{esc_attr(src)}" data-acc="{esc_attr(acc)}" data-proj="{esc_attr(r["proj"])}" '
            f'data-month="{esc_attr(r.get("month",""))}" data-text="{esc_attr(blob)}">'
            f'<td class="nowrap">{esc(r.get("date_str",""))}</td>'
            f'{src_td}'
            f'{acc_td}'
            f'<td><span class="chip" title="{esc_attr(chip_title)}">{esc(r["proj"])}</span></td>'
            f'<td><a href="sessions/{esc_attr(r["out_html"])}">{title_html}</a>{mem_link}</td>'
            f'<td class="num">{r["n_user"]}/{r["n_assistant"]}</td>'
            f'<td class="num">{r["n_tools"]}</td>'
            f'<td class="num" data-sort="{(r.get("cost", 0) or 0):.6f}">{cost_cell}</td>'
            f'{resume_cell}'
            f'<td class="nowrap">{esc(r.get("branch",""))}</td>'
            f'<td class="nowrap">{esc(r.get("dur",""))}</td></tr>'
        )
    # 專案 → memory 頁（選了專案時用，可能同名跨帳號故帶 account）
    mem_map = {}
    for r in rows:
        if not r.get("mem_href"):
            continue
        lst = mem_map.setdefault(r["proj"], [])
        ent = {"a": r.get("account", ""), "h": r["mem_href"]}
        if ent not in lst:
            lst.append(ent)
    # 內嵌到 <script>：跳脫 < > 與 JS 行終止符，避免 </script> 破出或 U+2028/9 截斷字串
    mem_js = (json.dumps(mem_map, ensure_ascii=False)
              .replace("<", "\\u003c").replace(">", "\\u003e")
              .replace(" ", "\\u2028").replace(" ", "\\u2029"))
    grand = cost_summary(rows)
    body = f"""
<div class="wrap">
  <h1>AI 對話紀錄</h1>
  <div class="smeta">{len(rows)} 個 session · {len(sources) or 1} 種工具 · {len(accounts) or 1} 個帳號 · {len(projects)} 個專案{grand} · 產生於 {esc(local_str(datetime.now()))}</div>
  {'<div class="smeta"><a href="cache-report.html">⚡ 快取分析報告 →</a>　<a href="cache-hypotheses.html">🧪 快取假說檢定 →</a></div>' if cache_report else ''}
  <div class="filters">
    <input id="q" class="search" placeholder="🔍 搜尋標題 / 專案 / 分支…" oninput="af()">
    {src_select}
    {acc_select}
    <select id="fp" onchange="af()"><option value="">全部專案</option>{proj_opts}</select>
    <select id="fm" onchange="af()"><option value="">全部月份</option>{month_opts}</select>
    <button id="clr" class="clr" type="button" onclick="clearF()">清除</button>
    <span id="cnt" class="cnt"></span>
  </div>
  <div id="memstrip" class="memstrip" style="display:none"></div>
  <table id="tbl">
    <thead><tr>
      <th>日期 ▾</th>{src_th}{acc_th}<th>專案</th><th>標題</th>
      <th class="num">問/答</th><th class="num">工具</th><th class="num">估算$</th><th class="num" title="resume 後約載入的 context（最後一輪脈絡）">resume</th><th>分支</th><th>時長</th>
    </tr></thead>
    <tbody>{''.join(tr)}</tbody>
  </table>
</div>
<script>
var Q=document.getElementById('q'),FS=document.getElementById('fs'),FP=document.getElementById('fp'),FM=document.getElementById('fm'),FA=document.getElementById('fa'),CNT=document.getElementById('cnt');
var ROWS=[].slice.call(document.querySelectorAll('#tbl tbody tr'));
var MEM={mem_js},MSTRIP=document.getElementById('memstrip');
function updMem(p,s,a){{
 var ents=(p&&MEM[p])?MEM[p]:[];
 if(s&&s!=='claude-code')ents=[];
 if(a)ents=ents.filter(function(e){{return e.a===a;}});
 MSTRIP.textContent='';
 if(!(p&&ents.length)){{MSTRIP.style.display='none';return;}}
 MSTRIP.style.display='';
 MSTRIP.appendChild(document.createTextNode('🧠 此專案 memory：'));
 ents.forEach(function(e,i){{
  if(i)MSTRIP.appendChild(document.createTextNode(' · '));
  var link=document.createElement('a');
  link.setAttribute('href',e.h);
  link.textContent=(e.a&&ents.length>1?e.a+' ':'')+'開啟 →';
  MSTRIP.appendChild(link);
 }});
}}
function lsGet(k){{try{{return localStorage.getItem(k);}}catch(e){{return null;}}}}
function lsSet(k,v){{try{{localStorage.setItem(k,v);}}catch(e){{}}}}
var FKEY='idx_filter_v1';
function saveF(){{lsSet(FKEY,JSON.stringify({{q:Q.value,s:FS?FS.value:'',p:FP.value,m:FM.value,a:FA?FA.value:''}}));}}
function setSel(el,v){{if(!el||!v)return;for(var i=0;i<el.options.length;i++){{if(el.options[i].value===v){{el.value=v;return;}}}}}}
function restoreF(){{var raw=lsGet(FKEY);if(!raw)return;try{{var f=JSON.parse(raw);if(f.q)Q.value=f.q;setSel(FS,f.s);setSel(FP,f.p);setSel(FM,f.m);setSel(FA,f.a);}}catch(e){{}}}}
function clearF(){{Q.value='';if(FS)FS.value='';FP.value='';FM.value='';if(FA)FA.value='';af();}}
function af(){{var q=Q.value.toLowerCase(),s=FS?FS.value:'',p=FP.value,m=FM.value,a=FA?FA.value:'',n=0;
 ROWS.forEach(function(r){{var ok=(!q||r.dataset.text.indexOf(q)>=0)&&(!s||r.dataset.source===s)&&(!p||r.dataset.proj===p)&&(!m||r.dataset.month===m)&&(!a||r.dataset.acc===a);
  r.style.display=ok?'':'none';if(ok)n++;}});
 CNT.textContent=n+' / '+ROWS.length;updMem(p,s,a);saveF();}}
restoreF();af();
document.querySelectorAll('#tbl th').forEach(function(th,i){{th.onclick=function(){{
 var tb=document.querySelector('#tbl tbody'),rs=[].slice.call(tb.rows);
 th._d=!th._d;rs.sort(function(a,b){{var ca=a.cells[i],cb=b.cells[i];
  var x=ca.dataset.sort!==undefined?ca.dataset.sort:ca.innerText,y=cb.dataset.sort!==undefined?cb.dataset.sort:cb.innerText;
  var nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)){{return th._d?nx-ny:ny-nx;}}
  return th._d?String(x).localeCompare(y):String(y).localeCompare(x);}});rs.forEach(function(r){{tb.appendChild(r);}});}};}});
</script>
"""
    return html_page("Claude Code 對話紀錄", body)


def render_index_md(rows, show_account=False, cache_report=False) -> str:
    by_source_proj = {}
    for r in rows:
        by_source_proj.setdefault((r.get("source_kind", SOURCE_CLAUDE), r["proj"]), []).append(r)
    sources = {r.get("source_kind", SOURCE_CLAUDE) for r in rows}
    n_acc = len({r.get("account", "") for r in rows if r.get("account")}) or 1
    out = ["# AI 對話紀錄", "",
           f"{len(rows)} 個 session · {len(sources) or 1} 種工具 · {n_acc} 個帳號 · {len({p for _, p in by_source_proj})} 個專案{cost_summary(rows)}", ""]
    if cache_report:
        out += ["⚡ [快取分析報告](cache-report.md) · 🧪 [快取假說檢定](cache-hypotheses.md)", ""]
    for src, proj in sorted(by_source_proj, key=lambda x: (source_label(x[0]), x[1])):
        out.append(f"## {source_label(src)} / {proj}")
        out.append("")
        for r in sorted(by_source_proj[(src, proj)], key=lambda x: x.get("start_ts") or 0, reverse=True):
            acc = f"`{r.get('account','')}` · " if (show_account and r.get("account")) else ""
            mark = "✎ " if r.get("rename") else ""
            cost = cost_label(r.get("cost", 0) or 0, r.get("cost_partial"))
            out.append(f"- {acc}[{mark}{r['title']}](sessions/{r['out_md']}) — {r.get('date_str','?')} · "
                       f"~{cost} · {r['n_user']}問/{r['n_assistant']}答 · 🔧{r['n_tools']}"
                       + (f" · `{r['branch']}`" if r.get("branch") else ""))
        out.append("")
    return "\n".join(out)


# =========================================================================
# 快取分析報告（cache-report.html / cache-report.md）
# =========================================================================
def _gap_bucket_index(gap):
    for i, (hi, _) in enumerate(REPORT_GAP_BUCKETS):
        if gap < hi:
            return i
    return len(REPORT_GAP_BUCKETS) - 1


def _ctx_bucket_index(v):
    for i, (hi, _) in enumerate(REPORT_CTX_BINS):
        if v < hi:
            return i
    return len(REPORT_CTX_BINS) - 1


def _wilson(k, n, z=1.96):
    """二項比率的 Wilson 95% 信賴區間。回傳 (p, lo, hi)，皆 0..1；n=0 回 (0,0,0)。"""
    if n <= 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, center - half), min(1.0, center + half)


def _ci_str(k, n):
    """『xx%（CI a–b%）』字串；n=0 回 —。"""
    if not n:
        return "—"
    p, lo, hi = _wilson(k, n)
    return f"{round(100 * p)}%（CI {round(100 * lo)}–{round(100 * hi)}%）"


def _cmh(strata):
    """Cochran–Mantel–Haenszel 合併勝算比：分層 2×2 下比較兩組事件率（控制分層變數的混雜）。
    每層 (a,b,c,d)＝(尖峰冷啟, 尖峰命中, 離峰冷啟, 離峰命中)。
    回傳 {"or","lo","hi","p","n"}；資訊不足（任一方向無不一致樣本）回 None。
    CI 用 Robins–Breslow–Greenland 變異數、p 用 CMH 卡方（連續性校正、1 自由度）——皆純 stdlib。"""
    R = S = 0.0
    num_rr = num_rs = num_ss = 0.0
    a_sum = e_sum = v_sum = 0.0
    total = 0
    for a, b, c, d in strata:
        n = a + b + c + d
        if n < 2:
            continue
        total += n
        r_i = a * d / n
        s_i = b * c / n
        p_i = (a + d) / n
        q_i = (b + c) / n
        R += r_i
        S += s_i
        num_rr += p_i * r_i
        num_rs += p_i * s_i + q_i * r_i
        num_ss += q_i * s_i
        row1, col1 = a + b, a + c
        a_sum += a
        e_sum += row1 * col1 / n
        v_sum += row1 * (n - row1) * col1 * (n - col1) / (n * n * (n - 1))
    if R <= 0 or S <= 0 or v_sum <= 0:
        return None
    or_ = R / S
    var_ln = num_rr / (2 * R * R) + num_rs / (2 * R * S) + num_ss / (2 * S * S)
    half = 1.96 * math.sqrt(var_ln)
    chi2 = max(abs(a_sum - e_sum) - 0.5, 0.0) ** 2 / v_sum
    p = math.erfc(math.sqrt(chi2 / 2))
    return {"or": or_, "lo": or_ * math.exp(-half), "hi": or_ * math.exp(half), "p": p, "n": total}


def _ca_trend(bins):
    """Cochran–Armitage 趨勢檢定（等距分數）：bins=[(冷啟數, 樣本數), …] 依分箱序。
    回傳 {"z","p"}，z>0＝率隨序上升；單箱或全冷/全熱（變異數 0）回 None。純 stdlib。"""
    N = sum(n for _, n in bins)
    K = sum(k for k, _ in bins)
    if len(bins) < 2 or N <= 0 or K <= 0 or K >= N:
        return None
    pbar = K / N
    T = sum(i * k for i, (k, _) in enumerate(bins))
    s1 = sum(i * n for i, (_, n) in enumerate(bins))
    s2 = sum(i * i * n for i, (_, n) in enumerate(bins))
    var = pbar * (1 - pbar) * (s2 - s1 * s1 / N)
    if var <= 0:
        return None
    z = (T - pbar * s1) / math.sqrt(var)
    return {"z": z, "p": math.erfc(abs(z) / math.sqrt(2))}


_CAUSE_LABEL = {k: lbl for k, lbl, _ in REPORT_CAUSES}
_CAUSE_DESC = {k: desc for k, _, desc in REPORT_CAUSES}
_AVOIDABLE_CAUSES = ("expiry", "evict")   # 「可避免/異常」：非結構性、與閒置行為相關


def _top_hours(counts, k=3):
    """取活躍時段（count>0）裡最高的 k 個小時，回傳由小到大排序的整數小時清單。"""
    active = sorted((h for h in range(24) if counts[h] > 0),
                    key=lambda h: counts[h], reverse=True)[:k]
    return sorted(active)


def _break_category(gap):
    if gap < 60 * 60:
        return "30–60 分"
    if gap < 3 * 3600:
        return "1–3 時（午休級）"
    if gap < 6 * 3600:
        return "3–6 時"
    return "> 6 時（隔夜／長假級）"


def build_cache_report(rows):
    """從各 session 的 cache_steps／cache_events 彙整快取分析（v2）。回傳 dict（has_data=False 代表沒資料）。
    四條主線：
      (A) 成因分解──每個冷啟依 REPORT_CAUSES 優先序恰好歸一因；結構性成因（第一句/切帳號/換模型/壓縮）
          的相鄰步 pair 不進 TTL 估計（競爭風險：它們死於別的原因，混入會污染 TTL 曲線）。
      (B) TTL 存活──分桶命中率＋Wilson CI；cohort 依「最近一次有寫入的步」之 TTL 分
          「1h／5m／未知(舊資料無細分)」——cur 讀的快取是那次寫入存的。
          KPI「TTL 遵約率」＝1h cohort 在應命中帶 [REPORT_INTRA_SEC, REPORT_TTL_SAFE_SEC) 的命中率，
          其補數＝「提早失效率」（1h 快取理論上帶內必命中；TTL 每次使用會刷新，gap＝距上次使用）。
      (C) 重暖作息──各帳號時間軸閒置 ≥ REPORT_BREAK_SEC 後的冷啟，本地時段直方圖
          （平日/假日 × 可避免/不可避免，正規化為 次/活躍日）。
      (D) 尖峰假設──應命中帶內（1h cohort）依 UTC 分時看提早失效率；依 gap 分層做 CMH 勝算比＋卡方，
          控制「午休/夜間本來就閒得久」的混雜。
      (E) 脈絡假設──同一批帶內樣本依「前一步脈絡」分箱看失效率（Cochran–Armitage 趨勢），
          並以帶內中位數切大/小脈絡、沿用 (D) 的 gap 分層 CMH；另記帶內失效的形態（殘餘命中、
          是否整段重寫）供機制判讀。(D)(E) 屬假說檢定，輸出在 cache-hypotheses.html（健檢頁另出）。"""
    claude = [r for r in rows
              if r.get("source_kind", SOURCE_CLAUDE) == SOURCE_CLAUDE and r.get("cache_steps")]
    accounts = sorted({r.get("account", "") for r in claude})
    cause_keys = [k for k, _, _ in REPORT_CAUSES]

    n_buckets = len(REPORT_GAP_BUCKETS)
    cohort_bins = {c: [{"n": 0, "hit": 0} for _ in range(n_buckets)] for c in ("1h", "5m", "unknown")}
    comply_n = comply_hit = 0                       # 1h cohort 應命中帶
    utc = [{"n": 0, "cold": 0, "ts": 0} for _ in range(24)]
    strata = [[0, 0, 0, 0] for _ in REPORT_CMH_STRATA]   # [尖峰冷, 尖峰命中, 離峰冷, 離峰命中]／gap 層
    band_pairs = []                                 # 應命中帶樣本：(前步脈絡, gap 層 idx, 冷啟)──假說 (E) 用
    evict_form = []                                 # 帶內冷啟形態：(殘餘 cache_read, 寫入量, 本步脈絡, 前步脈絡)
    causes_total = {k: 0 for k in cause_keys}
    cold_events = []                                # (epoch, cause)：供分期堆疊圖
    warm_max = 0
    top_warm = []                                   # (gap, epoch)：gap ≥ 1 小時仍命中的異常
    n_pairs = neg_gaps = 0
    avoid_usd = 0.0
    avoid_partial = False
    tok_read = tok_in = 0
    mode_n = {"1h": 0, "5m": 0, "unknown": 0}       # 資料世代組成（步數）
    timelines = {}                                  # account -> [(epoch, 冷啟, 成因 or None)]
    all_ts = []
    limit_ts = []                                   # 撞牆時刻（429；同 session 10 分內折疊為一次）

    for r in claude:
        raw = r["cache_steps"]
        models = r.get("cache_models") or []
        events = r.get("cache_events") or []
        for st in raw:
            tok_read += st[1]
            tok_in += st[2]
            mode_n["1h" if st[5] > 0 else ("5m" if st[4] > 0 else "unknown")] += 1
        steps = [(i, st) for i, st in enumerate(raw) if st[2] >= REPORT_MIN_CTX]
        last_lim = None
        for t, kind in events:
            if kind == "limit" and (last_lim is None or t - last_lim > 600):
                limit_ts.append(t)
                last_lim = t
        cause_by_t = {}
        if steps and steps[0][0] == 0 and _step_cold(steps[0][1]):   # 原始第 0 步＝session 第一句
            t0 = steps[0][1][0]
            causes_total["first"] += 1
            cold_events.append((t0, "first"))
            cause_by_t[t0] = "first"
        ei, n_ev = 0, len(events)
        last_write = None      # 最近一次有寫入的可分析步之 TTL（"1h"/"5m"）——cur 讀的快取以此為準；
        for (_, prev), (_, cur) in zip(steps, steps[1:]):   # 舊資料無細分則一路 None → cohort=unknown
            if prev[5] > 0:
                last_write = "1h"
            elif prev[4] > 0:
                last_write = "5m"
            gap = cur[0] - prev[0]
            if gap < 0:
                neg_gaps += 1
                continue
            n_pairs += 1
            while ei < n_ev and events[ei][0] <= prev[0]:
                ei += 1
            kinds = set()
            j = ei
            while j < n_ev and events[j][0] <= cur[0]:
                kinds.add(events[j][1])
                j += 1
            cold = _step_cold(cur)
            boundary = ("switch" if ("limit" in kinds or "auth" in kinds) else
                        "model" if (prev[6] >= 0 and cur[6] >= 0 and prev[6] != cur[6]) else
                        "compact" if "compact" in kinds else None)
            cohort = last_write or "unknown"
            ttl_bound = REPORT_TTL_SAFE_SEC if cohort == "1h" else 5 * 60
            if cold:
                if boundary:
                    cause = boundary
                elif gap >= ttl_bound:
                    cause = "expiry"
                elif gap >= REPORT_INTRA_SEC:
                    cause = "evict"
                else:
                    cause = "intra"
                causes_total[cause] += 1
                cold_events.append((cur[0], cause))
                cause_by_t[cur[0]] = cause
                if cause == "expiry":    # 可避免的重暖成本 ≈ 本步實際寫入成本 −（若命中）同量讀取成本
                    mi = cur[6]
                    price = model_price(models[mi]) if 0 <= mi < len(models) else None
                    if price:            # 寫入依本步自己的 TTL 細分計價（同 call_cost 規則）
                        legacy = max(cur[3] - cur[4] - cur[5], 0)
                        write = (legacy + cur[4]) * CACHE_WRITE_MULT + cur[5] * CACHE_WRITE_MULT_1H
                        avoid_usd += (write - cur[3] * CACHE_READ_MULT) * price[0] / 1_000_000
                    else:
                        avoid_partial = True
            if boundary:
                # 結構性成因：不進 TTL 存活／帶內統計（競爭風險）。並重置快取 lineage——
                # 切帳號/換模型/壓縮後是全新前綴，邊界前的寫入 TTL 不再適用；
                # 新 lineage 的 TTL 由邊界後第一個有寫入的步重新建立（其前皆視為 unknown）。
                last_write = None
                continue
            cb = cohort_bins[cohort][_gap_bucket_index(gap)]
            cb["n"] += 1
            if not cold:
                cb["hit"] += 1
                if gap > warm_max:
                    warm_max = gap
                if gap >= 3600:          # 1h TTL＋每次使用刷新之下，gap>1h 仍命中＝異常，列出供對照
                    top_warm.append((gap, cur[0]))
            if cohort == "1h" and REPORT_INTRA_SEC <= gap < REPORT_TTL_SAFE_SEC:
                comply_n += 1
                if not cold:
                    comply_hit += 1
                uh = datetime.fromtimestamp(cur[0], timezone.utc).hour   # 伺服器時間（與本地時區無關）
                u = utc[uh]
                u["n"] += 1
                u["ts"] = cur[0]
                if cold:
                    u["cold"] += 1
                si = 0
                while si < len(REPORT_CMH_STRATA) - 1 and gap >= REPORT_CMH_STRATA[si]:
                    si += 1
                col = 0 if uh in REPORT_PEAK_UTC else 2
                strata[si][col + (0 if cold else 1)] += 1
                band_pairs.append((prev[2], si, cold))
                if cold:
                    evict_form.append((cur[1], cur[3], cur[2], prev[2]))
        acct = r.get("account", "")
        tl = timelines.setdefault(acct, [])
        for _, st in steps:
            tl.append((st[0], _step_cold(st), cause_by_t.get(st[0])))
        all_ts.extend(st[0] for _, st in steps)

    # ── (C) 重暖作息：各帳號時間軸上 ≥ REPORT_BREAK_SEC 的閒置後、確實冷啟的第一步 ──
    resumes = []
    for tl in timelines.values():
        tl.sort()
        for (pt, _, _), (ct, ccold, ccause) in zip(tl, tl[1:]):
            gap = ct - pt
            if gap < REPORT_BREAK_SEC or not ccold:   # 閒置夠久卻仍命中（1h 快取還活著）→ 不算重暖
                continue
            loc = datetime.fromtimestamp(ct)
            resumes.append({"hour": loc.hour, "weekend": loc.weekday() >= 5, "gap": gap,
                            "avoid": ccause in _AVOIDABLE_CAUSES})
    wd = [{"a": 0, "u": 0} for _ in range(24)]
    we = [{"a": 0, "u": 0} for _ in range(24)]
    break_cats = {}
    for ev in resumes:
        (we if ev["weekend"] else wd)[ev["hour"]]["a" if ev["avoid"] else "u"] += 1
        cat = _break_category(ev["gap"])
        break_cats[cat] = break_cats.get(cat, 0) + 1
    act_wd, act_we = set(), set()                    # 活躍日數（有任何步驟的日子），供 次/日 正規化
    for t in all_ts:
        loc = datetime.fromtimestamp(t)
        (act_we if loc.weekday() >= 5 else act_wd).add(loc.strftime("%Y-%m-%d"))

    # ── 分期成因堆疊：預設按週（週一起算）；期間太長改按月 ──
    periods = []
    if cold_events:
        cold_events.sort()
        grp = {}
        for t, cause in cold_events:
            loc = datetime.fromtimestamp(t)
            monday = loc - timedelta(days=loc.weekday())
            grp.setdefault(monday.strftime("%Y-%m-%d"), {k: 0 for k in cause_keys})[cause] += 1
        if len(grp) > 16:
            grp = {}
            for t, cause in cold_events:
                grp.setdefault(datetime.fromtimestamp(t).strftime("%Y-%m"),
                               {k: 0 for k in cause_keys})[cause] += 1
            periods = [{"label": k, "counts": v} for k, v in sorted(grp.items())]
        else:
            periods = [{"label": f"{k[5:].replace('-', '/')} 週", "counts": v}
                       for k, v in sorted(grp.items())]

    span = ""
    span_weeks = 1.0
    if all_ts:
        lo_t, hi_t = min(all_ts), max(all_ts)
        lo_s = datetime.fromtimestamp(lo_t).strftime("%Y-%m-%d")
        hi_s = datetime.fromtimestamp(hi_t).strftime("%Y-%m-%d")
        span = lo_s if lo_s == hi_s else f"{lo_s} ～ {hi_s}"
        span_weeks = max((hi_t - lo_t) / 86400 / 7, 1 / 7)

    cmh = _cmh([tuple(sx) for sx in strata])
    peak_n = sum(sx[0] + sx[1] for sx in strata)
    peak_c = sum(sx[0] for sx in strata)
    off_n = sum(sx[2] + sx[3] for sx in strata)
    off_c = sum(sx[2] for sx in strata)

    # ── (E) 脈絡大小假設：帶內樣本依前步脈絡分箱＋趨勢；中位數切大/小、同一 gap 分層 CMH ──
    ctx_bins = [{"n": 0, "cold": 0} for _ in REPORT_CTX_BINS]
    ctx_med = 0
    ctx_cmh = ctx_trend = None
    ctx_big = {"n": 0, "cold": 0}
    ctx_small = {"n": 0, "cold": 0}
    if band_pairs:
        for pctx, _, was_cold in band_pairs:
            b = ctx_bins[_ctx_bucket_index(pctx)]
            b["n"] += 1
            b["cold"] += 1 if was_cold else 0
        ctx_med = sorted(p for p, _, _ in band_pairs)[len(band_pairs) // 2]
        strata_ctx = [[0, 0, 0, 0] for _ in REPORT_CMH_STRATA]   # [大冷, 大命中, 小冷, 小命中]／gap 層
        for pctx, si, was_cold in band_pairs:
            grp = ctx_big if pctx >= ctx_med else ctx_small
            grp["n"] += 1
            grp["cold"] += 1 if was_cold else 0
            col = 0 if pctx >= ctx_med else 2
            strata_ctx[si][col + (0 if was_cold else 1)] += 1
        ctx_cmh = _cmh([tuple(sx) for sx in strata_ctx])
        ctx_trend = _ca_trend([(b["cold"], b["n"]) for b in ctx_bins if b["n"]])
    ev_n = len(evict_form)
    ev_stats = {
        "n": ev_n,
        # 幾乎整段重寫（寫入 ≥ 75% 本步脈絡）＝快取真的不見了，非部分斷點雜訊
        "full": sum(1 for _, cc_w, ctx, _ in evict_form if cc_w * 4 >= ctx * 3),
        # 前後脈絡量相當（本步 ≥ 90% 前步）＝可排除壓縮/裁剪造成的假失效
        "same": sum(1 for _, _, ctx, pctx in evict_form if ctx * 10 >= pctx * 9),
        "res_med": sorted(cr for cr, _, _, _ in evict_form)[ev_n // 2] if ev_n else 0,
    }

    lim_hours = [0] * 24
    lim_wd = lim_we = 0
    for t in limit_ts:
        loc = datetime.fromtimestamp(t)
        lim_hours[loc.hour] += 1
        if loc.weekday() >= 5:
            lim_we += 1
        else:
            lim_wd += 1

    return {
        "has_data": bool(n_pairs or cold_events or resumes),
        "accounts": accounts,
        "n_sessions": len(claude),
        "span": span,
        "span_weeks": span_weeks,
        "mode_n": mode_n,
        "kpi": {
            "hit": (tok_read / tok_in) if tok_in else 0.0,
            "tok_in": tok_in,
            "comply_n": comply_n, "comply_hit": comply_hit,
            "avoid_usd": avoid_usd, "avoid_partial": avoid_partial,
            "avoid_week": causes_total["expiry"] / span_weeks,
        },
        # (B) 存活
        "cohort_bins": cohort_bins,
        "n_pairs": n_pairs,
        "warm_max": warm_max,
        "top_warm": sorted(top_warm, reverse=True)[:6],
        # (A) 成因
        "causes_total": causes_total,
        "cause_periods": periods,
        # (C) 作息
        "clock_wd": wd, "clock_we": we,
        "wd_days": len(act_wd), "we_days": len(act_we),
        "n_resumes": len(resumes),
        "break_cats": break_cats,
        # (D) 尖峰假設
        "utc": utc,
        "cmh": cmh,
        "peak_n": peak_n, "peak_c": peak_c, "off_n": off_n, "off_c": off_c,
        # (E) 脈絡假設與帶內失效形態（假說頁）
        "ctx_bins": ctx_bins, "ctx_med": ctx_med, "ctx_cmh": ctx_cmh, "ctx_trend": ctx_trend,
        "ctx_big": ctx_big, "ctx_small": ctx_small, "evict_form": ev_stats,
        # 撞牆時刻與資料品質
        "limits_n": len(limit_ts), "limit_hours": lim_hours,
        "limits_wd": lim_wd, "limits_we": lim_we,
        "neg_gaps": neg_gaps,
    }


def _active_hours(series):
    """跨多條序列找有活動的小時範圍 (lo, hi)，圖只畫這段、不浪費 24 列空白。無資料回上班時段。"""
    lo, hi = 24, -1
    for s in series:
        for h in range(24):
            if s[h]:
                lo, hi = min(lo, h), max(hi, h)
    return (lo, hi) if hi >= 0 else (8, 18)


def _pct(x, digits=0):
    return f"{100 * x:.{digits}f}%"


def _hour_chart2(cells, days, mx, lo, hi):
    """時段圖（每列一小時）：bar 分兩段＝可避免（實色）＋不可避免（同色調淡），
    寬度＝次/活躍日、依傳入 mx 正規化讓平日/假日共用刻度；只畫 lo..hi 小時。"""
    mx = mx or 1
    days = days or 1
    rows = []
    for h in range(lo, hi + 1):
        a, u = cells[h]["a"], cells[h]["u"]
        wa = max(3, round(200 * (a / days) / mx)) if a else 0
        wu = max(3, round(200 * (u / days) / mx)) if u else 0
        rows.append(
            f'<div class="hrow"><span class="hh">{h:02d} 時</span>'
            f'<span class="hbarwrap">'
            f'<span class="hseg a" style="width:{wa}px" title="可避免（閒置過期/提早失效）：{a} 次"></span>'
            f'<span class="hseg u" style="width:{wu}px" title="不可避免（第一句/切帳號/換模型/壓縮）：{u} 次"></span>'
            f'</span><span class="hn">{(a + u) or ""}</span></div>')
    return "".join(rows)


def _cause_bars_html(periods):
    """分期成因堆疊橫條：列寬∝該期冷啟總數（跨期可比），列內按成因比例分段（2px 底色縫）。"""
    mx = max((sum(p["counts"].values()) for p in periods), default=0) or 1
    rows = []
    for p in periods:
        total = sum(p["counts"].values())
        segs = "".join(
            f'<span class="cseg cz-{k}" style="flex:{p["counts"][k]}"'
            f' title="{esc(_CAUSE_LABEL[k])}：{p["counts"][k]} 次"></span>'
            for k, _, _ in REPORT_CAUSES if p["counts"].get(k))
        w = max(4, round(100 * total / mx))
        rows.append(f'<div class="crow"><span class="clab">{esc(p["label"])}</span>'
                    f'<span class="cbar" style="width:{w}%">{segs}</span>'
                    f'<span class="hn">{total}</span></div>')
    return "".join(rows)


def _cause_legend_html(counts):
    return ('<div class="legend">' + "".join(
        f'<span><span class="sw cz-{k}"></span>{esc(lbl)}　<span class="hn">{counts.get(k, 0)}</span></span>'
        for k, lbl, _ in REPORT_CAUSES) + "</div>")


_GAP_X_MIN, _GAP_X_MAX = 30.0, 24 * 3600.0     # 存活曲線 x 軸（秒，log 尺度）


def _bucket_mid(i):
    """第 i 個間隔桶的幾何中點（曲線 x 座標）。"""
    lo = REPORT_GAP_BUCKETS[i - 1][0] if i else _GAP_X_MIN
    hi = REPORT_GAP_BUCKETS[i][0]
    if hi == float("inf"):
        hi = _GAP_X_MAX
    return math.sqrt(max(lo, _GAP_X_MIN) * hi)


def _svg_survival(cohorts):
    """TTL 存活曲線：命中率 vs 閒置間隔（log x）折線＋Wilson CI 帶；5 分／1 時參考線。
    cohorts=[{"label","css","bins":[(n,hit)…依 REPORT_GAP_BUCKETS]}]，空桶跳過。"""
    W, H, L, R, T, B = 660, 250, 46, 14, 14, 40
    x0, x1 = math.log(_GAP_X_MIN), math.log(_GAP_X_MAX)

    def X(v):
        return L + (math.log(min(max(v, _GAP_X_MIN), _GAP_X_MAX)) - x0) / (x1 - x0) * (W - L - R)

    def Y(p):
        return T + (1 - p) * (H - T - B)

    parts = [f'<svg class="viz" viewBox="0 0 {W} {H}" role="img" aria-label="快取命中率對閒置間隔">']
    for frac in (0, .25, .5, .75, 1):
        y = Y(frac)
        parts.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{round(frac * 100)}%</text>')
    for sec, lab in ((60, "1 分"), (300, "5 分"), (900, "15 分"), (3600, "1 時"), (6 * 3600, "6 時")):
        x = X(sec)
        cls = "guide" if sec in (300, 3600) else "grid"
        parts.append(f'<line x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{H - B}" class="{cls}"/>')
        parts.append(f'<text x="{x:.1f}" y="{H - B + 16}" text-anchor="middle">{lab}</text>')
    for co in cohorts:
        pts = []
        for i, (n, hit) in enumerate(co["bins"]):
            if not n:
                continue
            p, lo, hi = _wilson(hit, n)
            pts.append((X(_bucket_mid(i)), p, lo, hi, n, hit, i))
        if not pts:
            continue
        band = " ".join(f"{x:.1f},{Y(hi):.1f}" for x, _, _, hi, *_ in pts)
        band += " " + " ".join(f"{x:.1f},{Y(lo):.1f}" for x, _, lo, *_ in reversed(pts))
        parts.append(f'<polygon points="{band}" class="band {co["css"]}"/>')
        line = " ".join(f"{x:.1f},{Y(p):.1f}" for x, p, *_ in pts)
        parts.append(f'<polyline points="{line}" class="curve {co["css"]}"/>')
        for x, p, lo, hi, n, hit, i in pts:
            parts.append(
                f'<circle cx="{x:.1f}" cy="{Y(p):.1f}" r="4.5" class="dot {co["css"]}">'
                f'<title>{esc(co["label"])}｜{esc(REPORT_GAP_BUCKETS[i][1])}：命中 {hit}/{n}（{_ci_str(hit, n)}）</title>'
                f'</circle>')
    parts.append("</svg>")
    return "".join(parts)


def _dotplot_scale(rows):
    """dot plot 的 x 軸上限與刻度步長（整數百分比）：0 起、涵蓋所有列的 Wilson CI 上界、
    3–5 段 nice 刻度——失效率通常只有個位數 %，固定 0–100% 會把點全擠在左端。"""
    hi_max = 0.0
    for row in rows:
        _, _, hi = _wilson(row["cold"], row["n"])
        hi_max = max(hi_max, hi)
    pct = max(hi_max * 100, 1.0)
    for step in (1, 2, 5, 10, 20):    # pct ≤ 100 → step 20 必成立（ceil(100/20)=5），無需更粗的階
        k = math.ceil(pct / step)
        if k <= 5:
            return min(step * k, 100), step
    return 100, 20


def _svg_dotplot(rows, aria):
    """帶內提早失效率 dot plot：每列一類別，點＋Wilson CI whisker；row["peak"] 的列鋪淡底強調。
    rows=[{"label","n","cold","peak"}]，僅含 n>0 的列；label 直接作列首文字（含任何標記）。
    x 軸依資料範圍縮放（_dotplot_scale），每條格線都標值。"""
    RH, W, L, R, TOP = 24, 660, 132, 52, 26
    H = TOP + RH * len(rows) + 8
    axis_max, step = _dotplot_scale(rows)

    def X(p):
        return L + min(p * 100 / axis_max, 1.0) * (W - L - R)

    parts = [f'<svg class="viz" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(aria)}">']
    for ri, row in enumerate(rows):        # 先鋪強調底色，再畫格線與點
        if row.get("peak"):
            y = TOP + RH * ri
            parts.append(f'<rect x="{L}" y="{y:.1f}" width="{W - L - R}" height="{RH}" class="peak"/>')
    for t in range(0, axis_max + 1, step):
        x = X(t / 100)
        parts.append(f'<line x1="{x:.1f}" y1="{TOP - 4}" x2="{x:.1f}" y2="{H - 6}" class="grid"/>')
        parts.append(f'<text x="{x:.1f}" y="{TOP - 8}" text-anchor="middle">{t}%</text>')
    for ri, row in enumerate(rows):
        y = TOP + RH * ri + RH / 2
        p, lo, hi = _wilson(row["cold"], row["n"])
        parts.append(f'<text x="{L - 8}" y="{y + 4:.1f}" text-anchor="end">{esc(row["label"])}</text>')
        parts.append(f'<line x1="{X(lo):.1f}" y1="{y:.1f}" x2="{X(hi):.1f}" y2="{y:.1f}" class="whisk"/>')
        parts.append(f'<circle cx="{X(p):.1f}" cy="{y:.1f}" r="5" class="dot co-evict">'
                     f'<title>{esc(row["label"])}：帶內冷啟 {row["cold"]}/{row["n"]}（{_ci_str(row["cold"], row["n"])}）</title>'
                     f'</circle>')
        parts.append(f'<text x="{W - R + 6}" y="{y + 4:.1f}">n={row["n"]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _hours_label(hours):
    return "、".join(f"{h:02d}" for h in hours) + " 時" if hours else "—"


def _cover_items(d):
    """報告頁「涵蓋範圍」條目——健檢頁與假說頁共用，兩頁引用的樣本數字必一致。"""
    cover = [f"{d['n_sessions']} 個 session", f"{d['n_pairs']} 個相鄰步樣本"]
    if d["span"]:
        cover.append(d["span"])
    if d["accounts"]:
        cover.append("帳號：" + "、".join(a or "default" for a in d["accounts"]))
    m = d["mode_n"]
    if m["1h"] or m["5m"]:
        gen = f"TTL 細分：1h 寫入 {m['1h']} 步"
        if m["5m"]:
            gen += f"、5 分 {m['5m']} 步"
        if m["unknown"]:
            gen += f"、未知（舊資料）{m['unknown']} 步"
        cover.append(gen)
    return cover


def render_cache_report_html(d) -> str:
    parts = []
    # 導言
    parts.append('<div class="report">')
    parts.append('<div class="topbar"><span><a class="back" href="index.html">← 回索引</a></span>'
                 '<span><a class="back" href="cache-hypotheses.html">🧪 假說檢定 →</a></span></div>')
    parts.append("<h1>⚡ 快取分析報告</h1>")
    parts.append(
        '<div class="lead">用每次 API 呼叫的 usage（含 cache_creation 的 5 分／1 小時 TTL 細分）量測：'
        '快取實際能撐多久、每個冷啟（整段重新暖機）是什麼成因、以及「1 小時內就失效」的異常有多常見。'
        '僅統計 Claude 主對話（子代理／Codex 不納入）；所有比率附 Wilson 95% 信賴區間（CI）。'
        '「提早失效」的成因假說（伺服器時段／脈絡大小）另在 <a href="cache-hypotheses.html">假說檢定頁</a> 逐一檢定。</div>')
    parts.append(f'<div class="lead">涵蓋範圍：{esc(" · ".join(_cover_items(d)))}</div>')

    # ── KPI 列 ──
    k = d["kpi"]
    tiles = [("整體命中率", _pct(k["hit"]) if k["tok_in"] else "—", "token 加權：讀取 ÷ 全部脈絡")]
    if k["comply_n"]:
        p, lo, hi = _wilson(k["comply_hit"], k["comply_n"])
        tiles.append(("TTL 遵約率", _pct(p),
                      f"1h 快取、閒置 2–55 分內回來仍命中；CI {_pct(lo)}–{_pct(hi)}，n={k['comply_n']}"))
        tiles.append(("提早失效", f"{k['comply_n'] - k['comply_hit']} 次",
                      f"應命中帶內卻冷啟＝{_pct(1 - p)}（1h TTL 下理論上應為 0）"))
    tiles.append(("可避免冷啟", f"{d['causes_total']['expiry']} 次",
                  f"閒置過期；約 {k['avoid_week']:.1f} 次/週"))
    tiles.append(("可避免浪費", fmt_money(k["avoid_usd"]) + ("+?" if k["avoid_partial"] else ""),
                  "過期重暖的估算額外成本（重寫 −0.1× 讀取）"))
    parts.append('<div class="kpis">' + "".join(
        f'<div class="kpi"><div class="kv2">{esc(v)}</div><div class="kl">{esc(t)}</div>'
        f'<div class="ks">{esc(s)}</div></div>' for t, v, s in tiles) + "</div>")

    # ── ① TTL 存活曲線 ──
    parts.append("<h2>① 快取能撐多久（TTL 存活曲線）</h2>")
    ins = []
    if k["comply_n"]:
        p, lo, hi = _wilson(k["comply_hit"], k["comply_n"])
        ins.append(f"1 小時快取在「應命中帶」（閒置 2–55 分）的命中率 <b>{_pct(p)}</b>"
                   f"（CI {_pct(lo)}–{_pct(hi)}，n={k['comply_n']}）——距 100% 的缺口就是「提早失效」。")
    if d["warm_max"]:
        ins.append(f"閒置最久仍命中：<b>{fmt_dur(d['warm_max'])}</b>。")
    if not ins:
        ins.append("尚無足夠的相鄰步樣本。")
    parts.append(f'<div class="insight">{"".join(ins)}</div>')
    cohort_defs = [("1h", "1h 寫入", "co-1h"), ("5m", "5 分寫入", "co-5m"), ("unknown", "TTL 未知（舊資料）", "co-unk")]
    cohorts = [{"label": lbl, "css": css, "bins": [(b["n"], b["hit"]) for b in d["cohort_bins"][key]]}
               for key, lbl, css in cohort_defs if any(b["n"] for b in d["cohort_bins"][key])]
    if cohorts:
        parts.append(_svg_survival(cohorts))
        if len(cohorts) > 1:
            parts.append('<div class="legend">' + "".join(
                f'<span><span class="sw {c["css"]}"></span>{esc(c["label"])}</span>' for c in cohorts) + "</div>")
        parts.append('<div class="lead">結構性冷啟（session 第一句／切帳號／換模型／壓縮後）已從曲線樣本排除'
                     '（它們不是 TTL 造成的）；「&lt; 1 分」桶的 miss 多為回合內快取斷點雜訊。'
                     'TTL 每次使用會刷新，故 x 軸＝距上一次使用的閒置時間。</div>')
        head = "".join(f'<th colspan="2">{esc(c["label"])}</th>' for c in cohorts)
        sub = "".join('<th class="num">樣本</th><th>命中率（CI）</th>' for _ in cohorts)
        rows = []
        for i, (_, lbl) in enumerate(REPORT_GAP_BUCKETS):
            cells = ""
            row_n = 0
            for c in cohorts:
                n, hit = c["bins"][i]
                row_n += n
                cells += (f'<td class="num">{n or "—"}</td><td>{_ci_str(hit, n) if n else "—"}</td>')
            if row_n:
                rows.append(f"<tr><td>{esc(lbl)}</td>{cells}</tr>")
        parts.append(f'<table class="rep"><thead><tr><th rowspan="2">閒置間隔</th>{head}</tr>'
                     f"<tr>{sub}</tr></thead><tbody>{''.join(rows)}</tbody></table>")
    if d["top_warm"]:
        items = "".join(
            f'<div class="kv"><b>{fmt_dur(g)}</b> 後仍命中　'
            f'<span class="hn">{esc(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"))}</span></div>'
            for g, ts in d["top_warm"])
        parts.append("<h3>異常存活（閒置超過 1 小時仍命中）</h3>"
                     '<div class="lead">1h TTL＋每次使用刷新之下理論上不該發生；可能是量測空窗內'
                     "另有同前綴使用（分支/重試）或時鐘偏移，列出供對照。</div>" + items)

    # ── ② 冷啟成因分解 ──
    parts.append("<h2>② 冷啟成因分解</h2>")
    total_cold = sum(d["causes_total"].values())
    if total_cold:
        ct = d["causes_total"]
        avoid = ct["expiry"] + ct["evict"]
        struct = ct["first"] + ct["switch"] + ct["model"] + ct["compact"]
        ins2 = (f"共 <b>{total_cold}</b> 次冷啟：結構性（避不掉）<b>{struct}</b>、"
                f"可避免/異常 <b>{avoid}</b>（閒置過期 {ct['expiry']}＋提早失效 {ct['evict']}）、"
                f"回合內雜訊 {ct['intra']}。")
        if ct["switch"]:
            ins2 += f"「limit/切帳號」{ct['switch']} 次已自動偵測（429/401 邊界），不會污染 TTL 統計。"
        parts.append(f'<div class="insight">{ins2}</div>')
        parts.append(_cause_legend_html(ct))
        if d["cause_periods"]:
            parts.append('<div class="causes">' + _cause_bars_html(d["cause_periods"]) + "</div>")
        rows = "".join(f"<tr><td><span class='sw cz-{key}'></span>{esc(lbl)}</td>"
                       f"<td class='num'>{d['causes_total'][key]}</td><td class='hn'>{esc(desc)}</td></tr>"
                       for key, lbl, desc in REPORT_CAUSES)
        parts.append('<table class="rep"><thead><tr><th>成因</th><th class="num">次數</th>'
                     f"<th>說明</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        parts.append('<div class="insight">期間內沒有冷啟。</div>')

    # ── ③ 重暖作息 ──
    parts.append("<h2>③ 你都什麼時段重新暖機（冷啟作息）</h2>")
    n_res = d["n_resumes"]
    parts.append(f'<div class="lead">各帳號活動軸上「閒置 ≥ {fmt_dur(REPORT_BREAK_SEC)} 後、確實冷啟的第一步」'
                 f"＝一次重新開工，共 <b>{n_res}</b> 次（閒置夠久卻仍命中 1h 快取者不算）。"
                 "bar 分兩段：<b>實色＝可避免</b>（閒置過期/提早失效——早點回來或先 ping 就能省）、"
                 "淡色＝不可避免（session 第一句/切帳號/換模型/壓縮）。已正規化為「次/活躍日」，平日假日可直接比。</div>")
    wd_tot = [c["a"] + c["u"] for c in d["clock_wd"]]
    we_tot = [c["a"] + c["u"] for c in d["clock_we"]]
    ins3 = []
    if sum(wd_tot):
        avg = sum(wd_tot) / d["wd_days"] if d["wd_days"] else 0
        ins3.append(f"平日 <b>{sum(wd_tot)}</b> 次／{d['wd_days']} 個活躍日（{avg:.1f} 次/日），"
                    f"集中在 <b>{_hours_label(_top_hours(wd_tot))}</b>；")
    if sum(we_tot):
        avg = sum(we_tot) / d["we_days"] if d["we_days"] else 0
        ins3.append(f"假日 <b>{sum(we_tot)}</b> 次／{d['we_days']} 個活躍日（{avg:.1f} 次/日），"
                    f"集中在 <b>{_hours_label(_top_hours(we_tot))}</b>。")
    if ins3:
        parts.append(f'<div class="insight">{"".join(ins3)}</div>')
    lo_h, hi_h = _active_hours([wd_tot, we_tot])
    mx = max([(wd_tot[h] / d["wd_days"]) if d["wd_days"] else 0 for h in range(24)] +
             [(we_tot[h] / d["we_days"]) if d["we_days"] else 0 for h in range(24)] + [0.001])
    parts.append('<div class="charts">'
                 f'<div class="chart"><h4>平日（{sum(wd_tot)} 次）</h4>'
                 f'{_hour_chart2(d["clock_wd"], d["wd_days"], mx, lo_h, hi_h)}</div>'
                 f'<div class="chart we"><h4>假日（{sum(we_tot)} 次）</h4>'
                 f'{_hour_chart2(d["clock_we"], d["we_days"], mx, lo_h, hi_h)}</div></div>')
    if d["break_cats"]:
        order = ["30–60 分", "1–3 時（午休級）", "3–6 時", "> 6 時（隔夜／長假級）"]
        items = "".join(f'<div class="kv">{esc(x)}：{d["break_cats"][x]} 次</div>'
                        for x in order if x in d["break_cats"])
        parts.append(f"<h3>中斷長度分佈</h3>{items}")

    # ── ④ 撞牆時刻 ──
    if d["limits_n"]:
        parts.append("<h2>④ 什麼時候撞到 limit（附帶觀察）</h2>")
        parts.append(f'<div class="lead">紀錄中共 <b>{d["limits_n"]}</b> 次撞到用量上限'
                     f"（429；同 session 10 分鐘內折疊為一次）：平日 {d['limits_wd']}、假日 {d['limits_we']}。"
                     "撞牆後切帳號續聊的冷啟已在②歸為「limit/切帳號」。</div>")
        hrs = [(h, n) for h, n in enumerate(d["limit_hours"]) if n]
        if hrs:
            cells = "".join(f"<tr><td>{h:02d} 時</td><td class='num'>{n}</td></tr>" for h, n in hrs)
            parts.append('<table class="rep"><thead><tr><th>本地時段</th><th class="num">次數</th>'
                         f"</tr></thead><tbody>{cells}</tbody></table>")

    tail = ("※ 全為估算：命中率取自各次呼叫 usage；③④時間為本機時區；成本按公告價與 TTL 細分倍率估。"
            "冷啟門檻＝命中率 < 25%。")
    if d["neg_gaps"]:
        tail += f"另有 {d['neg_gaps']} 對相鄰步時間倒退（時鐘偏移），已跳過。"
    parts.append(f'<div class="lead" style="margin-top:24px">{tail}</div>')
    parts.append("</div>")
    return html_page("快取分析報告", "".join(parts), body_class="report")


def render_cache_report_md(d) -> str:
    out = ["# ⚡ 快取分析報告", "",
           "用每次 API 呼叫的 usage（含 cache_creation 的 5 分／1 小時 TTL 細分）量測：快取實際能撐多久、"
           "每個冷啟的成因、以及「1 小時內就失效」的異常。僅統計 Claude 主對話；比率附 Wilson 95% CI。"
           "「提早失效」的成因假說檢定另見 [快取假說檢定](cache-hypotheses.md)。", ""]
    out.append("涵蓋範圍：" + " · ".join(_cover_items(d)))

    k = d["kpi"]
    out += ["", "## 總覽（KPI）", ""]
    out.append(f"- 整體命中率（token 加權）：**{_pct(k['hit'])}**")
    if k["comply_n"]:
        p, lo, hi = _wilson(k["comply_hit"], k["comply_n"])
        out.append(f"- TTL 遵約率（1h 快取、閒置 2–55 分仍命中）：**{_pct(p)}**"
                   f"（CI {_pct(lo)}–{_pct(hi)}，n={k['comply_n']}）；提早失效 {k['comply_n'] - k['comply_hit']} 次")
    out.append(f"- 可避免冷啟（閒置過期）：**{d['causes_total']['expiry']} 次**（約 {k['avoid_week']:.1f} 次/週）"
               f"；估算可避免浪費 **{fmt_money(k['avoid_usd'])}{'+?' if k['avoid_partial'] else ''}**")

    out += ["", "## ① 快取能撐多久（TTL 存活）", ""]
    if d["warm_max"]:
        out.append(f"- 閒置最久仍命中：**{fmt_dur(d['warm_max'])}**")
    cohort_defs = [("1h", "1h 寫入"), ("5m", "5 分寫入"), ("unknown", "TTL 未知（舊資料）")]
    active = [(key, lbl) for key, lbl in cohort_defs if any(b["n"] for b in d["cohort_bins"][key])]
    if active:
        head = " | ".join(f"{lbl}：樣本 | 命中率（CI）" for _, lbl in active)
        out += ["", f"| 閒置間隔 | {head} |",
                "|---|" + "---:|---|" * len(active)]
        for i, (_, lbl) in enumerate(REPORT_GAP_BUCKETS):
            cells = []
            row_n = 0
            for key, _ in active:
                n, hit = d["cohort_bins"][key][i]["n"], d["cohort_bins"][key][i]["hit"]
                row_n += n
                cells.append(f"{n or '—'} | {_ci_str(hit, n) if n else '—'}")
            if row_n:
                out.append(f"| {lbl} | " + " | ".join(cells) + " |")
        out.append("")
        out.append("> 結構性冷啟（第一句/切帳號/換模型/壓縮）已排除；「< 1 分」桶多為回合內快取斷點雜訊。"
                   "TTL 每次使用會刷新，間隔＝距上一次使用。")
    if d["top_warm"]:
        out += ["", "異常存活（>1 小時仍命中；理論上不該發生，供對照）："]
        for g, ts in d["top_warm"]:
            out.append(f"- {fmt_dur(g)} 後仍命中 · {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}")

    out += ["", "## ② 冷啟成因分解", ""]
    total_cold = sum(d["causes_total"].values())
    if total_cold:
        ct = d["causes_total"]
        out.append(f"共 {total_cold} 次冷啟；結構性 {ct['first'] + ct['switch'] + ct['model'] + ct['compact']}、"
                   f"可避免/異常 {ct['expiry'] + ct['evict']}、回合內雜訊 {ct['intra']}。")
        out += ["", "| 成因 | 次數 | 說明 |", "|---|---:|---|"]
        for key, lbl, desc in REPORT_CAUSES:
            out.append(f"| {lbl} | {d['causes_total'][key]} | {desc} |")
        if d["cause_periods"]:
            keys = [key for key, _, _ in REPORT_CAUSES]
            out += ["", "| 期間 | " + " | ".join(_CAUSE_LABEL[x] for x in keys) + " | 合計 |",
                    "|---|" + "---:|" * (len(keys) + 1)]
            for p in d["cause_periods"]:
                cells = " | ".join(str(p["counts"].get(x, 0) or "") for x in keys)
                out.append(f"| {p['label']} | {cells} | {sum(p['counts'].values())} |")
    else:
        out.append("期間內沒有冷啟。")

    out += ["", "## ③ 重暖作息（冷啟時段）", "",
            f"閒置 ≥ {fmt_dur(REPORT_BREAK_SEC)} 後、確實冷啟的第一步＝重新開工，共 {d['n_resumes']} 次"
            "（可避免＝閒置過期/提早失效；不可避免＝第一句/切帳號/換模型/壓縮）。"]
    wd_tot = [c["a"] + c["u"] for c in d["clock_wd"]]
    we_tot = [c["a"] + c["u"] for c in d["clock_we"]]
    if sum(wd_tot):
        out.append(f"- 平日 {sum(wd_tot)} 次／{d['wd_days']} 個活躍日，集中 {_hours_label(_top_hours(wd_tot))}")
    if sum(we_tot):
        out.append(f"- 假日 {sum(we_tot)} 次／{d['we_days']} 個活躍日，集中 {_hours_label(_top_hours(we_tot))}")
    out += ["", "| 時 | 平日可避免 | 平日不可避免 | 假日可避免 | 假日不可避免 |", "|---|---:|---:|---:|---:|"]
    for h in range(24):
        if wd_tot[h] or we_tot[h]:
            out.append(f"| {h:02d} | {d['clock_wd'][h]['a'] or ''} | {d['clock_wd'][h]['u'] or ''} "
                       f"| {d['clock_we'][h]['a'] or ''} | {d['clock_we'][h]['u'] or ''} |")
    if d["break_cats"]:
        out += ["", "中斷長度分佈："]
        for x in ["30–60 分", "1–3 時（午休級）", "3–6 時", "> 6 時（隔夜／長假級）"]:
            if x in d["break_cats"]:
                out.append(f"- {x}：{d['break_cats'][x]} 次")

    if d["limits_n"]:
        out += ["", "## ④ 什麼時候撞到 limit（附帶觀察）", "",
                f"共 {d['limits_n']} 次（429；同 session 10 分內折疊）：平日 {d['limits_wd']}、假日 {d['limits_we']}。"]
        hrs = [(h, n) for h, n in enumerate(d["limit_hours"]) if n]
        if hrs:
            out += ["", "| 本地時段 | 次數 |", "|---|---:|"]
            out += [f"| {h:02d} 時 | {n} |" for h, n in hrs]

    tail = "※ 全為估算：成本按公告價與 TTL 細分倍率；冷啟門檻＝命中率 < 25%。"
    if d["neg_gaps"]:
        tail += f"另有 {d['neg_gaps']} 對相鄰步時間倒退（時鐘偏移），已跳過。"
    out += ["", tail]
    return "\n".join(out)


def _ctx_verdict(d):
    """假說②（脈絡大小）的結論句素材。回傳 (has_test, verdict字串或None)。"""
    cc = d["ctx_cmh"]
    if cc and d["ctx_big"]["n"] >= 10 and d["ctx_small"]["n"] >= 10:
        verdict = ("支持假設（大脈絡的提早失效勝算顯著較高）" if cc["p"] < .05 and cc["or"] > 1 else
                   "與假設相反（大脈絡反而較低）" if cc["p"] < .05 and cc["or"] < 1 else
                   "看不出顯著差異")
        return True, verdict
    return False, None


def render_cache_hypotheses_html(d) -> str:
    k = d["kpi"]
    parts = ['<div class="report">']
    parts.append('<div class="topbar"><span><a class="back" href="index.html">← 回索引</a></span>'
                 '<span><a class="back" href="cache-report.html">⚡ 快取分析報告</a></span></div>')
    parts.append("<h1>🧪 快取假說檢定</h1>")
    parts.append(
        '<div class="lead">健檢報告量到「<b>提早失效</b>」——1 小時快取在應命中帶'
        f'（閒置 {fmt_dur(REPORT_INTRA_SEC)}–{fmt_dur(REPORT_TTL_SAFE_SEC)}，TTL 每次使用會刷新）'
        '內卻冷啟的異常。這頁把它的候選成因一個假說一節逐一檢定；樣本、門檻與健檢頁同一套'
        '（結構性冷啟已排除、比率附 Wilson 95% CI），拿自己的 JSONL 跑 viewer 就會得到同一組檢定，可直接對照。</div>')
    parts.append(f'<div class="lead">涵蓋範圍：{esc(" · ".join(_cover_items(d)))}</div>')
    if k["comply_n"]:
        ev = k["comply_n"] - k["comply_hit"]
        parts.append(f'<div class="insight">共用樣本：應命中帶相鄰步 <b>n={k["comply_n"]}</b>，'
                     f'其中提早失效 <b>{ev}</b> 次（{_ci_str(ev, k["comply_n"])}）。</div>')
    else:
        parts.append('<div class="insight">尚無應命中帶樣本（需要 1h 快取寫入、閒置 2–55 分的相鄰步）；'
                     '累積資料後這頁才有東西可檢定。</div>')

    # ── ① 尖峰時段假說 ──
    parts.append("<h2>① 伺服器時段：全球尖峰時較易被擠掉？</h2>")
    parts.append('<div class="lead">假設：全球尖峰時段伺服器壓力大，快取較易被擠掉、提早失效。'
                 '依 <b>UTC（伺服器時間）</b>分時、跨帳號匯總。時段和「閒多久」相關（午休/夜間閒得久），'
                 '所以檢定先依間隔分層（2–15／15–30／30–55 分）再合併（CMH），避免把「閒得久」誤讀成「尖峰失效」。</div>')
    if d["cmh"] and d["peak_n"] >= 10 and d["off_n"] >= 10:
        c = d["cmh"]
        verdict = ("支持假設（尖峰的提早失效勝算顯著較高）" if c["p"] < .05 and c["or"] > 1 else
                   "與假設相反（尖峰反而較低）" if c["p"] < .05 and c["or"] < 1 else
                   "看不出顯著差異")
        ins1 = (f"控制間隔後，尖峰(13–21 UTC) vs 離峰的提早失效勝算比 <b>OR={c['or']:.2f}</b>"
                f"（95% CI {c['lo']:.2f}–{c['hi']:.2f}，p={c['p']:.3f}，n={c['n']}）→ <b>{verdict}</b>。"
                f"<span class='hn'>粗率：尖峰 {_ci_str(d['peak_c'], d['peak_n'])}、"
                f"離峰 {_ci_str(d['off_c'], d['off_n'])}。</span>")
    else:
        ins1 = (f"帶內樣本：尖峰(13–21 UTC) <b>{d['peak_n']}</b> 筆、離峰 <b>{d['off_n']}</b> 筆——"
                "樣本不足以做分層檢定，暫不下結論。合併家用電腦／朋友的資料後（台北晚上正落在"
                "歐洲午後尖峰），尖峰樣本就會補上。")
    parts.append(f'<div class="insight">{ins1}</div>')
    urows = [{"label": f'{h:02d} UTC｜本地≈'
                       + (datetime.fromtimestamp(d["utc"][h]["ts"]).strftime("%H時") if d["utc"][h]["ts"] else "")
                       + ("🔺" if h in REPORT_PEAK_UTC else ""),
              "n": d["utc"][h]["n"], "cold": d["utc"][h]["cold"], "peak": h in REPORT_PEAK_UTC}
             for h in range(24) if d["utc"][h]["n"]]
    if urows:
        parts.append(_svg_dotplot(urows, "各 UTC 小時的提早失效率"))
        parts.append('<div class="lead">🔺＝全球尖峰參考帶（13–21 UTC，底色標示）。'
                     '點＝該時段的提早失效率、whisker＝95% CI；n 小時區間很寬，看重疊程度而非點值。'
                     '橫軸依資料範圍縮放（讀刻度標示）。</div>')

    # ── ② 脈絡大小假說 ──
    parts.append("<h2>② 累積脈絡：context 越大越容易被擠掉？</h2>")
    parts.append('<div class="lead">假設：脈絡（input＋cache）越大、快取佔用越多，越容易被提早逐出。'
                 '依<b>前一步的脈絡</b>分箱——前一步寫進快取的量，正是這次要能活著回來的量。'
                 '脈絡大小與「閒多久」可能相關，檢定同樣依間隔分層（2–15／15–30／30–55 分）做 CMH：'
                 '以帶內脈絡<b>中位數</b>切「大／小」兩組比較。</div>')
    big, small = d["ctx_big"], d["ctx_small"]
    has_test, verdict = _ctx_verdict(d)
    if has_test:
        cc = d["ctx_cmh"]
        ins2 = (f"控制間隔後，大脈絡（≥ 中位數 {fmt_tokens(d['ctx_med'])}）vs 小脈絡的提早失效勝算比 "
                f"<b>OR={cc['or']:.2f}</b>（95% CI {cc['lo']:.2f}–{cc['hi']:.2f}，p={cc['p']:.3f}，"
                f"n={cc['n']}）→ <b>{verdict}</b>。"
                f"<span class='hn'>粗率：大脈絡 {_ci_str(big['cold'], big['n'])}、"
                f"小脈絡 {_ci_str(small['cold'], small['n'])}。</span>")
        t = d["ctx_trend"]
        if t:
            ins2 += f"<span class='hn'>分箱趨勢檢定（Cochran–Armitage）：z={t['z']:+.2f}、p={t['p']:.3f}。</span>"
    else:
        ins2 = (f"帶內樣本：大脈絡 <b>{big['n']}</b> 筆、小脈絡 <b>{small['n']}</b> 筆——"
                "樣本不足以分層檢定，暫不下結論；累積更多資料後再看。")
    parts.append(f'<div class="insight">{ins2}</div>')
    crows = [{"label": lbl, "n": b["n"], "cold": b["cold"], "peak": False}
             for b, (_, lbl) in zip(d["ctx_bins"], REPORT_CTX_BINS) if b["n"]]
    if crows:
        parts.append(_svg_dotplot(crows, "各脈絡大小的提早失效率"))
        parts.append('<div class="lead">點＝該脈絡箱的提早失效率、whisker＝95% CI，看重疊程度而非點值；'
                     '橫軸依資料範圍縮放（讀刻度標示）。</div>')
        rows = "".join(f"<tr><td>{esc(lbl)}</td><td class='num'>{b['n']}</td>"
                       f"<td>{_ci_str(b['cold'], b['n'])}</td></tr>"
                       for b, (_, lbl) in zip(d["ctx_bins"], REPORT_CTX_BINS) if b["n"])
        parts.append('<table class="rep"><thead><tr><th>前步脈絡（tokens）</th><th class="num">樣本</th>'
                     f"<th>帶內提早失效率（CI）</th></tr></thead><tbody>{rows}</tbody></table>")
    ef = d["evict_form"]
    if ef["n"]:
        parts.append("<h3>附帶觀察：失效當下的形態（機制線索）</h3>")
        parts.append(f'<div class="lead">帶內提早失效共 <b>{ef["n"]}</b> 次：其中 {ef["same"]} 次前後脈絡量相當'
                     '（本步 ≥ 90% 前步——可排除壓縮/裁剪造成的假失效）、'
                     f'{ef["full"]} 次幾乎整段重寫（寫入 ≥ 75% 脈絡）＝快取真的不見了。'
                     f'失效當下仍命中的殘餘量中位數 <b>{fmt_tokens(ef["res_med"])}</b> tokens——'
                     '快取是前綴式的，殘餘＝請求最前端仍活著的一小段，與「跨 session 共用的開頭段'
                     '（工具定義等）存活、session 專屬的對話大段被逐出」一致：'
                     '逐出可能以快取斷點的 segment 為單位、與段落熱度相關，而非整條請求一起死。</div>')

    tail = ("※ 門檻與樣本定義同 <a href='cache-report.html'>健檢頁</a>（冷啟＝該步命中率 < 25%）；"
            "①時間為 UTC（伺服器時間）、與本地時區無關。假說清單會隨資料與想法擴充。")
    parts.append(f'<div class="lead" style="margin-top:24px">{tail}</div>')
    parts.append("</div>")
    return html_page("快取假說檢定", "".join(parts), body_class="report")


def render_cache_hypotheses_md(d) -> str:
    k = d["kpi"]
    out = ["# 🧪 快取假說檢定", "",
           f"健檢報告量到「提早失效」——1 小時快取在應命中帶（閒置 {fmt_dur(REPORT_INTRA_SEC)}–"
           f"{fmt_dur(REPORT_TTL_SAFE_SEC)}）內卻冷啟的異常。"
           "這頁逐一檢定它的候選成因；樣本、門檻與健檢頁（[快取分析報告](cache-report.md)）同一套，"
           "拿自己的 JSONL 跑 viewer 就會得到同一組檢定，可直接對照。", ""]
    out.append("涵蓋範圍：" + " · ".join(_cover_items(d)))
    if k["comply_n"]:
        ev = k["comply_n"] - k["comply_hit"]
        out += ["", f"共用樣本：應命中帶相鄰步 n={k['comply_n']}，其中提早失效 {ev} 次"
                    f"（{_ci_str(ev, k['comply_n'])}）。"]
    else:
        out += ["", "尚無應命中帶樣本（需要 1h 快取寫入、閒置 2–55 分的相鄰步）。"]

    out += ["", "## ① 伺服器時段：全球尖峰時較易被擠掉？", "",
            "依 UTC（伺服器時間）分時；依間隔分層（2–15/15–30/30–55 分）做 CMH，控制「閒多久」的混雜。"]
    if d["cmh"] and d["peak_n"] >= 10 and d["off_n"] >= 10:
        c = d["cmh"]
        verdict = ("支持假設" if c["p"] < .05 and c["or"] > 1 else
                   "與假設相反" if c["p"] < .05 and c["or"] < 1 else "無顯著差異")
        out.append(f"- 尖峰(13–21 UTC) vs 離峰：OR={c['or']:.2f}（95% CI {c['lo']:.2f}–{c['hi']:.2f}，"
                   f"p={c['p']:.3f}，n={c['n']}）→ **{verdict}**"
                   f"；粗率 尖峰 {_ci_str(d['peak_c'], d['peak_n'])}、離峰 {_ci_str(d['off_c'], d['off_n'])}")
    else:
        out.append(f"- 尖峰 {d['peak_n']} 筆、離峰 {d['off_n']} 筆——樣本不足以分層檢定；"
                   "合併家用電腦／朋友資料後再看。")
    out += ["", "| UTC 時 | 樣本 | 帶內提早失效率（CI） |", "|---|---:|---|"]
    for h in range(24):
        u = d["utc"][h]
        if not u["n"]:
            continue
        mark = " 🔺" if h in REPORT_PEAK_UTC else ""
        out.append(f"| {h:02d}{mark} | {u['n']} | {_ci_str(u['cold'], u['n'])} |")
    out.append("> 🔺＝全球尖峰參考帶（13–21 UTC）。n 小時 CI 很寬，看重疊而非點值。")

    out += ["", "## ② 累積脈絡：context 越大越容易被擠掉？", "",
            "依前一步的脈絡（前一步寫進快取、這次要能活著回來的量）分箱；"
            "以帶內中位數切大/小、依間隔分層（2–15/15–30/30–55 分）做 CMH，控制「大脈絡剛好閒得久」的混雜。"]
    big, small = d["ctx_big"], d["ctx_small"]
    has_test, verdict = _ctx_verdict(d)
    if has_test:
        cc = d["ctx_cmh"]
        line = (f"- 大脈絡（≥ 中位數 {fmt_tokens(d['ctx_med'])}）vs 小脈絡：OR={cc['or']:.2f}"
                f"（95% CI {cc['lo']:.2f}–{cc['hi']:.2f}，p={cc['p']:.3f}，n={cc['n']}）→ **{verdict}**"
                f"；粗率 大 {_ci_str(big['cold'], big['n'])}、小 {_ci_str(small['cold'], small['n'])}")
        out.append(line)
        t = d["ctx_trend"]
        if t:
            out.append(f"- 分箱趨勢檢定（Cochran–Armitage）：z={t['z']:+.2f}、p={t['p']:.3f}")
    else:
        out.append(f"- 大脈絡 {big['n']} 筆、小脈絡 {small['n']} 筆——樣本不足以分層檢定，暫不下結論。")
    crows = [(lbl, b) for b, (_, lbl) in zip(d["ctx_bins"], REPORT_CTX_BINS) if b["n"]]
    if crows:
        out += ["", "| 前步脈絡（tokens） | 樣本 | 帶內提早失效率（CI） |", "|---|---:|---|"]
        for lbl, b in crows:
            out.append(f"| {lbl} | {b['n']} | {_ci_str(b['cold'], b['n'])} |")
    ef = d["evict_form"]
    if ef["n"]:
        out += ["", f"附帶觀察（機制線索）：帶內提早失效 {ef['n']} 次——{ef['same']} 次前後脈絡量相當"
                    f"（排除壓縮/裁剪假失效）、{ef['full']} 次幾乎整段重寫（寫入 ≥ 75% 脈絡）；"
                    f"殘餘命中中位數 {fmt_tokens(ef['res_med'])} tokens（快取是前綴式的，殘餘＝最前端仍活著的一小段），"
                    "與「共用開頭段存活、session 專屬對話大段被逐出」一致。"]

    out += ["", "※ 門檻與樣本定義同健檢頁；①時間為 UTC（伺服器時間）。假說清單會隨資料與想法擴充。"]
    return "\n".join(out)


# =========================================================================
# HTML 外殼（CSS）
# =========================================================================
CSS = """
:root{--bg:#0d1117;--panel:#161b22;--panel2:#11161d;--border:#30363d;--text:#e6edf3;
--muted:#8b949e;--accent:#58a6ff;--user:#388bfd;--assistant:#3fb950;--err:#f85149;}
@media (prefers-color-scheme:light){:root{--bg:#fff;--panel:#f6f8fa;--panel2:#fbfcfd;
--border:#d0d7de;--text:#1f2328;--muted:#636c76;--accent:#0969da;--user:#0969da;
--assistant:#1a7f37;--err:#cf222e;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.65 -apple-system,"Segoe UI",system-ui,"Microsoft JhengHei","PingFang TC",sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:18px 20px 80px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:22px;margin:.4em 0 .2em}
.smeta{color:var(--muted);font-size:13px;margin:2px 0;word-break:break-all}
.mono,.nowrap{white-space:nowrap}.mono{font-family:ui-monospace,Consolas,monospace}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.ctrl button,.back{background:var(--panel);border:1px solid var(--border);color:var(--text);
border-radius:6px;padding:4px 10px;font-size:13px;cursor:pointer}
.back{display:inline-block}
/* 對話 */
.thread{margin-top:14px}
.turn{border:1px solid var(--border);border-radius:10px;margin:12px 0;background:var(--panel);overflow:hidden}
.turn.user{border-left:3px solid var(--user)}
.turn.assistant{border-left:3px solid var(--assistant)}
.turn.side{opacity:.95;background:var(--panel2)}
.turn .head{display:flex;align-items:center;gap:8px;padding:7px 12px;border-bottom:1px solid var(--border);
font-size:13px;background:rgba(127,127,127,.06)}
.turn .who{font-weight:600}.turn .when{margin-left:auto;color:var(--muted);font-size:12px;cursor:help;text-decoration:underline dotted transparent}
.turn .when:hover{text-decoration-color:var(--muted)}
.badge{background:var(--border);border-radius:10px;padding:1px 8px;font-size:11px;color:var(--muted)}
.meter{font-size:11px;color:var(--muted);background:rgba(127,127,127,.12);border-radius:8px;padding:1px 6px;margin-left:6px;white-space:nowrap}
.meter.m-cache{color:var(--assistant)}.meter.m-cost{color:#d29922}
.meter.cold{background:var(--err);color:#fff;font-weight:600}
body.hide-cache .m-cache,body.hide-cost .m-cost,body.hide-ctx .m-ctx,body.hide-out .m-out{display:none}
.step-sep{display:flex;align-items:center;flex-wrap:wrap;gap:0;margin:12px 0 4px;padding-top:7px;border-top:1px dashed var(--border)}
.step-sep .meter{margin-left:6px}
.step-n{font-size:11px;font-weight:600;color:var(--muted)}
.meterbar{color:var(--muted);font-size:12px;margin:6px 0 2px}
.meterbar label{margin-right:12px;cursor:pointer}.meterbar input{vertical-align:middle;margin-right:3px}
.body{padding:4px 14px}
.body p{margin:.5em 0}.body h1,.body h2,.body h3{font-size:1.05em;margin:.7em 0 .3em}
.body ul,.body ol{margin:.4em 0;padding-left:1.4em}
.body blockquote{border-left:3px solid var(--border);margin:.5em 0;padding:.1em .9em;color:var(--muted)}
.body hr{border:0;border-top:1px solid var(--border);margin:1em 0}
.body table.md{width:auto;border-collapse:collapse;margin:.6em 0;font-size:13px;display:block;overflow:auto}
.body table.md th,.body table.md td{border:1px solid var(--border);padding:4px 9px;text-align:left}
.body table.md th{position:static;cursor:default;background:rgba(127,127,127,.1);color:var(--text)}
code{background:rgba(127,127,127,.18);padding:.12em .35em;border-radius:4px;
font-family:ui-monospace,Consolas,monospace;font-size:.9em}
pre{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;
overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.5}
pre code{background:none;padding:0}
/* 工具 / 思考 */
details.tool,details.think,details.sidechain-wrap{border:1px solid var(--border);border-radius:8px;
margin:8px 0;background:var(--panel2)}
details>summary{cursor:pointer;padding:7px 12px;font-size:13px;list-style:none;user-select:none}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"▸ ";color:var(--muted)}
details[open]>summary:before{content:"▾ "}
details.tool>summary{color:var(--text);font-family:ui-monospace,Consolas,monospace}
.ti{color:#d29922}
.think>summary,.think-redacted{color:var(--muted);font-style:italic}
.tbody{padding:2px 12px 10px}
.cap,.fp{color:var(--muted);font-size:12px;margin:2px 0;font-family:ui-monospace,Consolas,monospace}
.tres{margin-top:6px}.tres .lbl{font-size:11px;color:var(--muted);margin:4px 0 2px}
.tres.err pre{border-color:var(--err)}.tres.err .lbl{color:var(--err)}
.tres.none{color:var(--muted);font-size:12px;font-style:italic}
pre.diff .del{color:var(--err);display:block}pre.diff .ins{color:var(--assistant);display:block}
ul.todos{list-style:none;padding-left:.3em}ul.todos li{margin:2px 0}
.img-ph,.think-redacted{color:var(--muted);font-size:13px;padding:4px 0}
/* 索引 */
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
.search{flex:1 1 220px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;
background:var(--panel);color:var(--text);font-size:14px}
.filters select{padding:8px 10px;border:1px solid var(--border);border-radius:8px;
background:var(--panel);color:var(--text);font-size:13px;max-width:45vw}
.filters button.clr{padding:8px 11px;border:1px solid var(--border);border-radius:8px;
background:var(--panel);color:var(--muted);font-size:13px;cursor:pointer}
.filters button.clr:hover{color:var(--text);border-color:var(--accent)}
.cnt{color:var(--muted);font-size:12px;margin-left:auto;white-space:nowrap}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{position:sticky;top:0;background:var(--bg);cursor:pointer;color:var(--muted);font-weight:600}
tr:hover td{background:rgba(127,127,127,.06)}
.num{text-align:right;color:var(--muted)}
.chip{background:rgba(127,127,127,.16);border-radius:10px;padding:2px 9px;font-size:12px;white-space:nowrap}
.chip.acc{background:rgba(88,166,255,.18)}
.chip.src{background:rgba(63,185,80,.18)}
.chip.sub{background:rgba(210,150,60,.22)}
.chip.compact{background:rgba(130,125,189,.25)}
/* 專案 memory */
.memlink{margin-left:6px;text-decoration:none;font-size:13px}
.memstrip{margin:6px 0 4px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;
background:rgba(130,125,189,.12);font-size:13px}
.memstrip a{margin-left:4px}
details.mcard{border:1px solid var(--border);border-radius:8px;margin:8px 0;background:var(--panel2)}
details.mcard>summary{cursor:pointer;padding:8px 12px;font-size:14px}
details.mcard.mindex>summary{color:var(--accent)}
.mname{font-weight:600}
.mdesc{color:var(--muted);font-size:13px;font-weight:400}
.chip.mtype{background:rgba(127,127,127,.18);color:var(--muted)}
.chip.mtype.t-feedback{background:rgba(210,150,60,.22);color:#d2963c}
.chip.mtype.t-project{background:rgba(63,185,80,.18);color:var(--assistant)}
.chip.mtype.t-user{background:rgba(88,166,255,.18);color:var(--accent)}
.chip.mtype.t-reference{background:rgba(130,125,189,.25)}
.day-sep{display:flex;align-items:center;text-align:center;color:var(--muted);font-size:12px;margin:18px 0 8px}
.day-sep::before,.day-sep::after{content:"";flex:1;border-top:1px solid var(--border)}
.day-sep span{padding:0 12px;white-space:nowrap}
.compact-sep{display:flex;align-items:center;text-align:center;color:#d2963c;font-size:12px;margin:20px 0 8px}
.compact-sep::before,.compact-sep::after{content:"";flex:1;border-top:1px dashed rgba(210,150,60,.5)}
.compact-sep span{padding:0 12px}
.compact-sum>summary{color:#d2963c}
.sub-toc{margin:12px 0;border:1px solid var(--border);border-radius:8px;background:var(--panel2);padding:6px 12px}
.sub-toc>summary{cursor:pointer;color:var(--muted);font-size:13px}
.sub-toc ul{margin:6px 0 2px;padding-left:18px}
.sub-toc li{margin:3px 0;font-size:13.5px}
.named{font-weight:600}
.subtitle{color:var(--muted);font-size:12px;margin-top:2px;font-weight:400}
.msg-img{max-width:100%;max-height:480px;border:1px solid var(--border);border-radius:8px;margin:6px 0;display:block}
/* 快取分析報告 */
.report h2{font-size:17px;margin:1.5em 0 .3em;border-bottom:1px solid var(--border);padding-bottom:5px}
.report h3{font-size:14px;margin:1.1em 0 .3em}
.report h4{font-size:13px;margin:.2em 0 .4em;color:var(--muted);font-weight:600}
.report .lead{color:var(--muted);font-size:13px;margin:.35em 0}
.report .insight{background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--accent);
border-radius:8px;padding:10px 14px;margin:12px 0;font-size:14px;line-height:1.7}
.report table.rep{width:auto;min-width:min(420px,100%);border-collapse:collapse;font-size:13px;margin:10px 0}
.report table.rep th,.report table.rep td{border-bottom:1px solid var(--border);padding:6px 12px;text-align:left}
.report table.rep th{position:static;cursor:default;color:var(--muted);font-weight:600}
.report .charts{display:flex;gap:28px;flex-wrap:wrap;margin:10px 0}
.report .chart{flex:1 1 280px}
.report .hrow{display:flex;align-items:center;gap:7px;line-height:1.85}
.report .hh{color:var(--muted);font-family:ui-monospace,Consolas,monospace;font-size:12px;width:46px;flex:none}
.report .hbarwrap{display:flex;gap:2px;align-items:center}
.report .hseg{display:inline-block;height:12px;border-radius:3px}
.report .hseg.a{background:var(--assistant)}
.report .hseg.u{background:var(--assistant);opacity:.35}
.report .chart.we .hseg{background:var(--accent)}
.report .hn{color:var(--muted);font-size:11px}
.report .kv{font-size:13px;margin:.25em 0}
.report .num{text-align:right}
.report .kpis{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}
.report .kpi{flex:1 1 150px;min-width:150px;background:var(--panel);border:1px solid var(--border);
border-radius:8px;padding:10px 12px}
.report .kpi .kv2{font-size:23px;font-weight:600;line-height:1.2}
.report .kpi .kl{font-size:12px;margin-top:3px}
.report .kpi .ks{color:var(--muted);font-size:11px;margin-top:2px;line-height:1.5}
.report .causes{margin:10px 0}
.report .crow{display:flex;align-items:center;gap:8px;line-height:2.1;font-size:12px}
.report .clab{color:var(--muted);width:74px;flex:none;text-align:right;
font-family:ui-monospace,Consolas,monospace}
.report .cbar{display:flex;gap:2px;height:14px}
.report .cseg{min-width:2px;border-radius:3px}
.report .sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.report .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:8px 0}
.report .cz-first{background:rgba(139,148,158,.6)}
.report .cz-switch{background:#9d7bd8}
.report .cz-model{background:#39c5cf}
.report .cz-compact{background:#d2963c}
.report .cz-expiry{background:var(--accent)}
.report .cz-evict{background:var(--err)}
.report .cz-intra{background:rgba(139,148,158,.28)}
/* 報告 SVG 圖表（存活曲線 / dot plot） */
.report svg.viz{width:100%;height:auto;max-width:680px;display:block;margin:8px 0}
.report svg.viz text{font:11px -apple-system,"Segoe UI",system-ui,sans-serif;fill:var(--muted)}
.report svg.viz .grid{stroke:var(--border);stroke-width:1}
.report svg.viz .guide{stroke:var(--muted);stroke-width:1}
.report svg.viz .curve{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.report svg.viz .band{stroke:none;opacity:.13}
.report svg.viz .dot{stroke:var(--bg);stroke-width:2}
.report svg.viz .whisk{stroke:var(--muted);stroke-width:2;stroke-linecap:round}
.report svg.viz .peak{fill:var(--muted);opacity:.10}
.report svg.viz .curve.co-1h{stroke:var(--accent)}
.report svg.viz .band.co-1h,.report svg.viz .dot.co-1h,.report .sw.co-1h{fill:var(--accent);background:var(--accent)}
.report svg.viz .curve.co-5m{stroke:var(--assistant)}
.report svg.viz .band.co-5m,.report svg.viz .dot.co-5m,.report .sw.co-5m{fill:var(--assistant);background:var(--assistant)}
.report svg.viz .curve.co-unk{stroke:var(--muted)}
.report svg.viz .band.co-unk,.report svg.viz .dot.co-unk,.report .sw.co-unk{fill:var(--muted);background:var(--muted)}
.report svg.viz .dot.co-evict{fill:var(--err)}
/* 全文搜尋（--search）結果頁 + 錨點跳轉高亮 */
mark{background:rgba(210,153,34,.45);color:inherit;border-radius:3px;padding:0 1px}
.hl{outline:2px solid var(--accent);outline-offset:2px}
details.sgroup{border:1px solid var(--border);border-radius:10px;margin:12px 0;background:var(--panel)}
details.sgroup>summary{padding:8px 12px;font-size:14px}
.stitle{font-weight:600}
.shead-meta{color:var(--muted);font-size:12px}
.sopen{font-size:12px;margin-left:6px;white-space:nowrap}
a.hit{display:block;margin:8px 12px;padding:7px 11px;border:1px solid var(--border);border-radius:8px;
background:var(--panel2);color:var(--text)}
a.hit:hover{border-color:var(--accent);text-decoration:none}
.hit .hmeta{color:var(--muted);font-size:12px;margin-bottom:2px}
.hit .snip{font-size:13.5px;line-height:1.7;word-break:break-word}
.hwhen{font-family:ui-monospace,Consolas,monospace}
.hmore{color:var(--muted);font-size:12px;margin:2px 14px 10px}
"""


def html_page(title, body, body_class=""):
    cls = f' class="{body_class}"' if body_class else ""
    return ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body{cls}>{body}</body></html>")


# =========================================================================
# 主程式
# =========================================================================
def iter_session_files(source: Path, project_filter):
    """產生 (專案munged名, session檔路徑)，不解析內容（給增量建置先比對指紋用）。"""
    if not source.is_dir():
        print(f"找不到來源資料夾：{source}", file=sys.stderr)
        return
    for proj_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        if project_filter and project_filter.lower() not in proj_dir.name.lower():
            continue
        for sf in sorted(proj_dir.glob("*.jsonl")):
            yield proj_dir.name, sf


def iter_codex_session_files(source: Path):
    if source.is_file() and source.suffix.lower() == ".jsonl":
        yield source
        return
    if not source.is_dir():
        print(f"找不到 Codex sessions 資料夾：{source}", file=sys.stderr)
        return
    for sf in sorted(source.rglob("*.jsonl")):
        yield sf


def session_signature(main_path: Path) -> str:
    """主檔 + 外部子代理檔 + 同專案 memory/ 的 (檔名, mtime, size) 組合，作為變動指紋。
    把 memory 納入：memory 新增/變動/移除時，session 會重建（頁頂 🧠 連結與 row 的 mem_href 才會更新）。"""
    paths = [main_path]
    side = main_path.with_suffix("")
    if side.is_dir():
        paths += sorted(side.rglob("*.jsonl"))
    mem_dir = main_path.parent / "memory"          # Claude 專案 memory（Codex 無，無副作用）
    if mem_dir.is_dir():
        paths += [("mem", p) for p in sorted(mem_dir.glob("*.md"))]
    parts = []
    for p in paths:
        tag, p = p if isinstance(p, tuple) else ("", p)
        try:
            st = p.stat()
            parts.append(f"{tag}{p.name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            pass
    return "|".join(parts)


def session_to_row(s: "Session") -> dict:
    dur = fmt_dur((s.end - s.start).total_seconds()) if (s.start and s.end) else ""
    return {
        "session_id": s.session_id,
        "source_kind": s.source_kind,
        "source_label": source_label(s.source_kind),
        "account": s.account,
        "proj": s.proj_display,
        "proj_munged": s.proj_munged,
        "mem_href": getattr(s, "mem_href", ""),
        "cwd": s.cwd,
        "title": s.title,
        "rename": s.rename,
        "ai_title": s.ai_title,
        "models": [short_model(m) for m in s.models],
        "cost": s.cost,
        "cost_partial": s.cost_partial,
        "cache_pct": s.cache_pct,
        "cache_steps": getattr(s, "cache_steps", []),   # cache-report 原料：[[epoch, cache_read, 脈絡, 寫總量, 寫5分, 寫1h, 模型idx], …]
        "cache_models": getattr(s, "cache_models", []),
        "cache_events": getattr(s, "cache_events", []),  # [[epoch, "limit"|"auth"|"compact"], …]
        "ctx_peak": s.ctx_peak,
        "tok_out": s.tok_out,
        "out_html": s.out_html,
        "out_md": s.out_md,
        "start_ts": s.start.timestamp() if s.start else None,
        "date_str": local_str(s.start) if s.start else "",
        "month": local_str(s.start, "%Y-%m") if s.start else "",
        "dur": dur,
        "branch": s.branch or "",
        "n_user": s.n_user,
        "n_assistant": s.n_assistant,
        "n_tools": s.n_tools,
        "n_subagents": getattr(s, "n_subagents", 0),
        "n_compacts": getattr(s, "n_compacts", 0),
        "resume_ctx": getattr(s, "resume_ctx", 0),
        "resume_window": context_window(getattr(s, "resume_model", "")),
        "empty": s.n_turns == 0,
    }


def load_manifest(out: Path) -> tuple:
    """回傳 (entries, stale)。stale=True 代表 manifest 檔存在但版本不符（renderer 升級）→ 需全建；
    用來在縮範圍模式時警告使用者：本次未涵蓋的範圍會暫時從索引/報告消失。"""
    try:
        data = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}, False
    if data.get("renderer_version") == RENDERER_VERSION:
        return data.get("entries", {}), False
    return {}, True


def save_manifest(out: Path, entries: dict):
    (out / MANIFEST_NAME).write_text(
        json.dumps({"renderer_version": RENDERER_VERSION, "entries": entries}, ensure_ascii=False),
        encoding="utf-8")


def manifest_key(source_kind: str, path: Path) -> str:
    return f"{source_kind}:{path.resolve()}"


def _dedupe_claude_sessions(files):
    """同一個 Claude session（相同 sessionId＝檔名）若被跨機同步複製到不同來源路徑，
    會在 index 與 cache report 雙倍計。依 sessionId 去重、保留檔案較大者（較完整）。
    （junction/symlink 指到同一真實檔的情形另由來源層 _dedupe_by_realpath 處理。）非 Claude 不動。"""
    out, pos = [], {}     # sessionId -> (out 內索引, 檔案大小)
    for item in files:
        source_kind, _, _, sf = item
        if source_kind != SOURCE_CLAUDE:
            out.append(item)
            continue
        sid = sf.stem
        try:
            size = sf.stat().st_size
        except OSError:
            size = 0
        if sid in pos:
            i, bsize = pos[sid]
            if size > bsize:          # 留較大（較完整）的那份
                out[i] = item
                pos[sid] = (i, size)
        else:
            pos[sid] = (len(out), size)
            out.append(item)
    return out


def munge_path(cwd) -> str:
    """模擬 Claude Code 建 projects 夾名的規則：把 ':' '\\' '/' 換成 '-'。
    用來判斷某個 cwd 是否「原生」於某個 munged 夾（munge(cwd)==夾名）。"""
    return str(cwd or "").replace(":", "-").replace("\\", "-").replace("/", "-")


def peek_cwd(path: Path, max_lines: int = 50) -> str:
    """只讀檔案開頭幾行、cheaply 取出 Claude JSONL 記錄的 cwd（不整檔解析）。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    c = json.loads(line).get("cwd")
                except Exception:
                    c = None
                if c:
                    return c
    except OSError:
        pass
    return ""


def build_project_names(files) -> dict:
    """每個 Claude (帳號, munged 夾) 算一個友善的正規顯示名（一夾一專案名）。
    取「原生 cwd」（munge 回來等於夾名）的葉名當專案名；搬遷後被複製進來的『外來』
    session（cwd 屬於別的夾）改用所屬夾的正規名，不再各自顯示舊路徑的葉名。
    整夾都無原生 cwd（全是外來複本）時退而用任一 cwd 葉名、再退回夾名。
    cwd 一律從主檔開頭 peek（不綁 manifest 指紋，免得只動到 memory/子代理就誤判 cwd）。"""
    cwds = {}   # (account, munged) -> [cwd, ...]
    for source_kind, acc, munged, sf in files:
        if source_kind != SOURCE_CLAUDE:
            continue
        cwds.setdefault((acc, munged), []).append(peek_cwd(sf))
    names = {}
    for (acc, munged), lst in cwds.items():
        native = [c for c in lst if c and munge_path(c) == munged]
        pool = native or [c for c in lst if c]
        names[(acc, munged)] = path_leaf_name(pool[0], munged) if pool else munged
    return names


def rel_index_href(session_rel_path: str) -> str:
    depth = len(PureWindowsPath(session_rel_path).parts)
    return "../" * depth + "index.html"


def account_label(dirname: str) -> str:
    """由資料夾名推標籤：.claude→default、.claude-<名稱>→<名稱>。"""
    if dirname == ".claude":
        return "default"
    if dirname.startswith(".claude-"):
        return dirname[len(".claude-"):] or "default"
    return dirname.lstrip(".") or dirname


def _default_label(projects_dir: Path) -> str:
    parent = projects_dir.parent.name            # 例 .claude / .claude-work
    if parent == ".claude" or parent.startswith(".claude-"):
        return account_label(parent)
    return parent or projects_dir.name           # 自訂位置：用上層目錄名當標籤


def _dedupe_labels(items):
    """items: [(標籤, projects目錄)]；重複標籤用上層(家)目錄名或編號區分，確保唯一。"""
    counts = {}
    for lab, _ in items:
        counts[lab] = counts.get(lab, 0) + 1
    used, out = set(), []
    for lab, path in items:
        name = lab
        if counts[lab] > 1:
            home = path.parent.parent.name
            name = f"{lab}-{home}" if home else lab
        base, k = name, 2
        while name in used:
            name = f"{base}-{k}"
            k += 1
        used.add(name)
        out.append((name, path))
    return out


def _dedupe_by_realpath(items):
    """junction/symlink 指到同一真實資料夾的來源只保留第一個。
    Path 物件在 Windows 比較不分大小寫、POSIX 分大小寫，剛好符合各平台檔案系統語意。"""
    seen, out = set(), []
    for lab, path in items:
        try:
            real = path.resolve()
        except OSError:
            real = path
        if real in seen:
            continue
        seen.add(real)
        out.append((lab, path))
    return out


def collect_sources(claude_source_args, account_arg):
    """回傳 [(標籤, projects目錄)]。
    通用方式：--claude-source 指定一個或多個輸入目錄（可寫「標籤=路徑」）；
    便利捷徑：--account 名稱 對應 ~/.claude[-名稱]/projects；
    皆未指定時：自動偵測 ~/.claude* 下所有含 projects 的目錄。"""
    home = Path.home()
    items = []
    for s in (claude_source_args or []):
        if "=" in s:
            lab, _, p = s.partition("=")
            items.append((lab.strip() or None, Path(p).expanduser()))
        else:
            items.append((None, Path(s).expanduser()))
    if account_arg:
        folder = ".claude" if account_arg in ("default", "main") else f".claude-{account_arg}"
        lab = "default" if account_arg in ("default", "main") else account_arg
        items.append((lab, home / folder / "projects"))
    if not items:
        for d in sorted(home.glob(".claude*")):
            if d.is_dir() and (d / "projects").is_dir():
                items.append((account_label(d.name), d / "projects"))
        if not items:
            items.append(("default", home / ".claude" / "projects"))
    items = _dedupe_by_realpath(items)
    labeled = [(lab or _default_label(path), path) for lab, path in items]
    return _dedupe_labels(labeled)


def collect_codex_sources(codex_source_args):
    """回傳 [(標籤, sessions目錄)]；未指定 --codex-source 時自動偵測 ~/.codex/sessions。"""
    items = []
    for s in (codex_source_args or []):
        if "=" in s:
            lab, _, p = s.partition("=")
            items.append((lab.strip() or None, Path(p).expanduser()))
        else:
            items.append((None, Path(s).expanduser()))
    if not items:
        default = Path.home() / ".codex" / "sessions"
        if default.is_dir():
            items.append(("default", default))
    items = _dedupe_by_realpath(items)
    labeled = [(lab or _default_label(path), path) for lab, path in items]
    return _dedupe_labels(labeled)


def project_matches_filter(s: Session, project_filter: str) -> bool:
    if not project_filter:
        return True
    needle = project_filter.lower()
    blob = f"{s.proj_display} {s.cwd} {s.path}".lower()
    return needle in blob


def row_matches_project(row: dict, project_filter: str) -> bool:
    if not project_filter:
        return True
    needle = project_filter.lower()
    blob = f"{row.get('proj','')} {row.get('cwd','')}".lower()
    return needle in blob


# =========================================================================
# 全文搜尋（--search）：掃既有輸出的 .md，產生可點擊跳到該則的結果頁（out/search/）
# =========================================================================
SEARCH_KEEP_DAYS = 30      # 結果頁保留天數；每次搜尋時清掉更舊的
SNIP_CTX = 60              # 命中詞前後各取的字元數
SNIP_MAX = 3               # 每則訊息最多顯示幾段片段
HITS_PER_SESSION = 30      # 每個 session 最多列出幾則命中
MAX_HIT_CARDS = 800        # 整頁命中上限（防極熱門詞把頁面撐爆）

# 只認我們自己產生的回合標頭（render_turn_md），把 .md 切回一則一則；
# 訊息內文若恰好偽裝出同款行會誤切——後果只是該筆跳錯位置，屬已知簡化。
_TURN_HEAD_RE = re.compile(r"^### (?P<side>↳ )?(?P<who>👤 You|🤖 Claude) ·(?P<rest>.*)\{#(?P<a>[ts]\d+)\}\s*$")
_TIME_RE = re.compile(r"\d\d:\d\d:\d\d")

# 查詢語法：空白=AND；獨立大寫 OR=前後任一；"片語"/'片語' 逐字（引號要在詞邊界，
# 所以 don't 這種撇號不受影響）。要搜字面 OR 請加引號。
_QUERY_TOKEN_RE = re.compile(
    r'(?:(?<=\s)|^)"([^"]*)"(?=\s|$)'       # "片語"
    r"|(?:(?<=\s)|^)'([^']*)'(?=\s|$)"      # '片語'
    r"|(\S+)")                              # 一般詞（含裸 OR 運算子）


def parse_query(q):
    """把查詢字串解析成 groups：[[(文字, 是否片語), …], …]。
    組與組之間是 AND；同組內是 OR。裸 token「OR」把下一項併入前一組
    （Google 式：A B OR C ＝ A 且 (B 或 C)）；開頭/結尾的懸空 OR 忽略。"""
    toks = []
    for m in _QUERY_TOKEN_RE.finditer(q or ""):
        if m.group(1) is not None or m.group(2) is not None:
            t = m.group(1) if m.group(1) is not None else m.group(2)
            if t.strip():
                toks.append((t, True))
        else:
            toks.append((m.group(3), False))
    groups, pend_or = [], False
    for text, is_phrase in toks:
        if not is_phrase and text == "OR":
            pend_or = bool(groups)
            continue
        if pend_or:
            groups[-1].append((text, is_phrase))
        else:
            groups.append([(text, is_phrase)])
        pend_or = False
    return groups


def _term_pattern(text, is_phrase, flags):
    """單一詞/片語 → regex。片語內的空白改成 \\s+，讓片語能跨換行/縮排比對。"""
    src = re.escape(text)
    if is_phrase:
        src = re.sub(r"(?:\\ )+", lambda _: r"\s+", src)
    return re.compile(src, flags)


def iter_turn_chunks(md_text):
    """把 session 的 .md 切成 (錨點, 是否子代理, 角色字串, 時間, 內文)；第一個回合前的 session 標頭不納入
    （標題/專案/cwd 等 metadata 搜尋 index.html 就有，這裡專搜對話內容）。"""
    cur, buf = None, []
    for line in md_text.splitlines():
        m = _TURN_HEAD_RE.match(line)
        if m:
            if cur:
                yield (*cur, "\n".join(buf))
            tm = _TIME_RE.search(m.group("rest") or "")
            cur = (m.group("a"), bool(m.group("side")), m.group("who"), tm.group(0) if tm else "")
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur:
        yield (*cur, "\n".join(buf))


def _merge_spans(spans, length):
    """命中位置擴成 ±SNIP_CTX 的窗並合併重疊，最多 SNIP_MAX 段。"""
    wins = []
    for s0, e0 in sorted(spans):
        a, b = max(0, s0 - SNIP_CTX), min(length, e0 + SNIP_CTX)
        if wins and a <= wins[-1][1]:
            wins[-1] = (wins[-1][0], max(wins[-1][1], b))
        else:
            wins.append((a, b))
    return wins[:SNIP_MAX]


def _snippet_html(chunk, spans, union_pat):
    """取命中片段、壓平空白、esc 後把命中詞包 <mark>。"""
    outp = []
    for a, b in _merge_spans(spans, len(chunk)):
        seg = re.sub(r"\s+", " ", chunk[a:b]).strip()
        parts, last = [], 0
        for m in union_pat.finditer(seg):
            parts.append(esc(seg[last:m.start()]))
            parts.append(f"<mark>{esc(m.group(0))}</mark>")
            last = m.end()
        parts.append(esc(seg[last:]))
        outp.append(("…" if a > 0 else "") + "".join(parts) + ("…" if b < len(chunk) else ""))
    return " ".join(outp)


def render_search_html(q, args, groups, n_hits, n_scope, n_missing, truncated):
    scope = []
    if args.project:
        scope.append(f"專案含「{args.project}」")
    if args.account:
        scope.append(f"帳號 {args.account}")
    if args.no_claude:
        scope.append("不含 Claude")
    if args.no_codex:
        scope.append("不含 Codex")
    if args.match_case:
        scope.append("區分大小寫")
    scope_txt = "、".join(scope) if scope else "全部"
    cards = []
    for g in groups:
        r = g["row"]
        has_html = g.get("has_html", True)
        href = f'../sessions/{r["out_html"] if has_html else r["out_md"]}'
        chips = f'<span class="chip src">{esc(r.get("source_label", ""))}</span>'
        if r.get("account"):
            chips += f' <span class="chip acc">{esc(r["account"])}</span>'
        if not has_html:
            chips += ' <span class="chip">僅 .md</span>'
        items = []
        for h in g["hits"]:
            icon = "👤" if "👤" in h["who"] else "🤖"
            side = ' <span class="badge">↳ 子代理</span>' if h["side"] else ""
            target = f'{href}#{h["anchor"]}' if has_html else href   # .md 無錨點可跳，開純文字後可搜 {#tN}
            items.append(
                f'<a class="hit" href="{esc_attr(target)}" target="_blank" rel="noopener">'
                f'<div class="hmeta">{icon}{side} <span class="hwhen">{esc(h["when"])}</span> · #{h["anchor"]}</div>'
                f'<div class="snip">{h["snip"]}</div></a>')
        more = (f'<div class="hmore">…此 session 另有 {g["more"]} 則命中未列出（開整頁後可用瀏覽器內搜尋）</div>'
                if g["more"] else "")
        head_meta = " · ".join(x for x in [esc(r.get("proj", "")), esc(r.get("date_str", "")),
                                           f'{len(g["hits"]) + g["more"]} 則'] if x)
        cards.append(
            f'<details class="sgroup" open><summary>{chips} <span class="stitle">{esc(r.get("title", ""))}</span>'
            f' <span class="shead-meta">{head_meta}</span>'
            f' <a class="sopen" href="{esc_attr(href)}" target="_blank" rel="noopener"'
            f' onclick="event.stopPropagation()">開整頁 ↗</a></summary>'
            + "".join(items) + more + "</details>")
    note_missing = f" · ⚠ {n_missing} 個缺/過時 .md 未納入" if n_missing else ""
    n_nohtml = sum(1 for g in groups if not g.get("has_html", True))
    if n_nohtml:
        note_missing += f" · ⚠ {n_nohtml} 個無 .html（連到 .md 純文字）"
    note_trunc = (f'<div class="smeta">⚠ 命中過多，僅列出前 {MAX_HIT_CARDS} 則——請加關鍵字縮小範圍。</div>'
                  if truncated else "")
    empty_note = ('<p class="smeta">沒有命中。空白分隔＝同一則訊息內全部出現（AND）——可減少詞數、'
                  '改用 <span class="mono">A OR B</span>、或把片語加引號；'
                  '標題/專案等 metadata 請用 index.html 的搜尋。</p>')
    # 重跑提示：查詢含雙引號時改用單引號包（PowerShell/bash 皆可貼）
    requote = f"'{q}'" if '"' in q else f'"{q}"'
    recmd = f"py ai_session_viewer.py --search {requote}" + (" --match-case" if args.match_case else "") + " --open"
    body = f"""
<div class="wrap">
  <div class="topbar"><span><a class="back" href="../index.html">← 回索引</a></span></div>
  <h1>🔍 {esc(q)}</h1>
  <div class="smeta">命中 {n_hits} 則 / {len(groups)} 個 session · 範圍：{esc(scope_txt)}
（{n_scope} 個 session{note_missing}）· {local_str(datetime.now().astimezone())}</div>
  <div class="smeta">重新搜尋：<span class="mono">{esc(recmd)}</span></div>
  {note_trunc}
  <div class="filters">
    <input id="q" class="search" placeholder="🔎 在結果內再過濾…" oninput="af()">
    <span id="cnt" class="cnt"></span>
  </div>
  {''.join(cards) if cards else empty_note}
</div>
<script>
var Q=document.getElementById('q'),CNT=document.getElementById('cnt');
var HITS=[].slice.call(document.querySelectorAll('a.hit'));
var GROUPS=[].slice.call(document.querySelectorAll('details.sgroup'));
function af(){{var q=Q.value.trim().toLowerCase(),n=0;
 HITS.forEach(function(h){{var ok=!q||h.textContent.toLowerCase().indexOf(q)>=0;h.style.display=ok?'':'none';if(ok)n++;}});
 GROUPS.forEach(function(g){{var any=false;[].slice.call(g.querySelectorAll('a.hit')).forEach(function(h){{if(h.style.display!=='none')any=true;}});g.style.display=any?'':'none';}});
 CNT.textContent=n+' / '+HITS.length+' 則';}}
af();
</script>
"""
    return html_page(f"搜尋：{q}", body)


def run_search(args):
    """--search 模式：不重新轉換，直接搜既有輸出並產生結果頁。"""
    groups_spec = parse_query(args.search)
    if not groups_spec:
        print('--search 需要至少一個關鍵字（語法：空白=AND、OR=任一、"片語"；見 --help）。',
              file=sys.stderr)
        raise SystemExit(2)
    out = Path(args.out)
    entries, stale = load_manifest(out)
    if not entries:
        hint = ("建置紀錄版本過舊（renderer 已更新）" if stale
                else f"找不到建置紀錄（{out / MANIFEST_NAME}）")
        print(f"{hint}；請先跑一次轉換（例：py ai_session_viewer.py --out {args.out}）再搜尋。", file=sys.stderr)
        raise SystemExit(2)

    rows = [e["row"] for e in entries.values() if e.get("row") and not e["row"].get("empty")]
    if args.project:
        rows = [r for r in rows if row_matches_project(r, args.project)]
    if args.account:
        rows = [r for r in rows if r.get("account", "") == args.account]
    if args.no_claude:
        rows = [r for r in rows if r.get("source_kind") != SOURCE_CLAUDE]
    if args.no_codex:
        rows = [r for r in rows if r.get("source_kind") != SOURCE_CODEX]
    rows.sort(key=lambda r: r.get("start_ts") or 0, reverse=True)

    # 比對/高亮共用同一套 regex，語意保證一致；預設不分大小寫（--match-case 切換）
    flags = 0 if args.match_case else re.IGNORECASE
    group_pats = [[_term_pattern(t, ph, flags) for t, ph in g] for g in groups_spec]
    union_pat = re.compile(
        "|".join(sorted((p.pattern for alts in group_pats for p in alts), key=len, reverse=True)),
        flags)

    def matches(text):          # 每組至少一個 alternative 命中（組間 AND、組內 OR）
        return all(any(p.search(text) for p in alts) for alts in group_pats)

    t0 = datetime.now()
    groups, n_hits, n_missing, truncated = [], 0, 0, False
    for r in rows:
        if n_hits >= MAX_HIT_CARDS:
            truncated = True
            break
        if not r.get("has_md", True):        # .md 缺或與 row 不同步（--format html 建置）→ 語料不可信，跳過
            n_missing += 1
            continue
        try:
            text = (out / "sessions" / r["out_md"]).read_text(encoding="utf-8")
        except OSError:
            n_missing += 1
            continue
        if not matches(text):                 # 整檔快篩：任一組整檔都沒中就不必切回合
            continue
        hits, more = [], 0
        for anchor, is_side, who, when, chunk in iter_turn_chunks(text):
            if not matches(chunk):
                continue
            if len(hits) >= HITS_PER_SESSION or n_hits >= MAX_HIT_CARDS:
                more += 1
                continue
            spans = set()                     # 每組取第一個命中的 alternative 當片段定位點
            for alts in group_pats:
                m = next((mm for mm in (p.search(chunk) for p in alts) if mm), None)
                if m:
                    spans.add((m.start(), m.end()))
            hits.append({"anchor": anchor, "side": is_side, "who": who, "when": when,
                         "snip": _snippet_html(chunk, sorted(spans), union_pat)})
            n_hits += 1
        if hits or more:
            # 沒有「與 row 同步的」.html（--format md 建置、或格式切換留下的過期檔）：
            # 結果頁退化連到 .md，避免連進無錨點/過期的頁
            has_html = r.get("has_html", True) and (out / "sessions" / r["out_html"]).exists()
            groups.append({"row": r, "hits": hits, "more": more, "has_html": has_html})
    elapsed = (datetime.now() - t0).total_seconds()

    sdir = out / "search"
    sdir.mkdir(parents=True, exist_ok=True)
    pruned = 0
    cutoff = datetime.now().timestamp() - SEARCH_KEEP_DAYS * 86400
    for f in sdir.glob("*.html"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                pruned += 1
        except OSError:
            pass
    q = args.search.strip()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = sdir / f"{stamp}__{safe_name(q, 40)}.html"
    dest.write_text(render_search_html(q, args, groups, n_hits, len(rows), n_missing, truncated),
                    encoding="utf-8")

    print(f"搜尋「{q}」：命中 {n_hits} 則 / {len(groups)} 個 session"
          f"（範圍 {len(rows)} 個 session，{elapsed:.1f} 秒）")
    if n_missing:
        print(f"  ⚠ {n_missing} 個 session 缺或過時 .md 未納入（--format html？重跑預設轉換可補齊）",
              file=sys.stderr)
    if pruned:
        print(f"  已清除 {pruned} 個超過 {SEARCH_KEEP_DAYS} 天的舊結果頁")
    print(f"  結果頁： {dest.resolve()}")
    if args.open:
        try:
            webbrowser.open(dest.resolve().as_uri())
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="把 Claude Code / Codex 的 JSONL session 轉成 HTML/Markdown")
    ap.add_argument("--claude-source", action="append", default=None,
                    help="輸入的 Claude projects 目錄，可多次指定，亦可寫「標籤=路徑」；未指定時自動偵測 ~/.claude*")
    ap.add_argument("--account", default="",
                    help="捷徑：等同 --claude-source 名稱=~/.claude-名稱/projects（預設帳號用 --account default）")
    ap.add_argument("--no-claude", action="store_true", help="不讀取 Claude Code 紀錄")
    ap.add_argument("--no-codex", action="store_true", help="不讀取 Codex 紀錄")
    ap.add_argument("--codex-source", action="append", default=None,
                    help="Codex sessions 目錄或單一 JSONL，可多次指定，亦可寫「標籤=路徑」；未指定時自動偵測 ~/.codex/sessions")
    ap.add_argument("--out", default="out", help="輸出資料夾（預設 ./out）")
    ap.add_argument("--format", choices=["html", "md", "both"], default="both")
    ap.add_argument("--project", default="", help="只轉名稱含此字串的專案")
    ap.add_argument("--include-empty", action="store_true",
                    help="連同只有 /指令、無實際對話的空 session 一起輸出")
    ap.add_argument("--force", action="store_true", help="忽略快取，全部重新產生")
    ap.add_argument("--open", action="store_true", help="完成後打開 index.html")
    ap.add_argument("--search", default=None, metavar="查詢",
                    help="全文搜尋既有輸出的對話內容並產生結果頁到 out/search/（不重新轉換，先跑過一次轉換）。"
                         "語法：空白分隔＝同一則訊息內全部命中（AND）；獨立大寫 OR＝前後任一命中"
                         "（A B OR C ＝ A 且 (B 或 C)）；\"片語\" 或 '片語' 逐字比對（片語內空白可跨換行），"
                         "要搜字面 OR 也用引號。可搭 --project/--account/--no-claude/--no-codex 縮範圍，"
                         "--open 直接打開結果頁")
    ap.add_argument("--match-case", action="store_true", help="--search 時區分大小寫（預設不分）")
    args = ap.parse_args()

    if args.search is not None:      # 有給 --search 就走搜尋；空字串/純空白由 run_search 報錯
        run_search(args)
        return

    out = Path(args.out)
    sess_dir = out / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)

    want_html = args.format in ("html", "both")
    want_md = args.format in ("md", "both")

    accounts = [] if args.no_claude else collect_sources(args.claude_source, args.account)
    codex_accounts = [] if args.no_codex else collect_codex_sources(args.codex_source)
    files = []   # (來源類型, 帳號, 專案munged名, session檔)
    for acc_name, proj_root in accounts:
        if not proj_root.is_dir():
            print(f"  (略過) 帳號 {acc_name} 找不到目錄：{proj_root}", file=sys.stderr)
            continue
        for proj_name, sf in iter_session_files(proj_root, args.project):
            files.append((SOURCE_CLAUDE, acc_name, proj_name, sf))
    for acc_name, sessions_root in codex_accounts:
        if not sessions_root.exists():
            print(f"  (略過) Codex {acc_name} 找不到目錄：{sessions_root}", file=sys.stderr)
            continue
        for sf in iter_codex_session_files(sessions_root):
            files.append((SOURCE_CODEX, acc_name, "codex", sf))
    n_raw = len(files)
    files = _dedupe_claude_sessions(files)          # 跨來源同 sessionId 的複本只留一份，避免雙倍計
    print(f"Claude 帳號：{', '.join(a for a, _ in accounts) or '(無)'}")
    if codex_accounts:
        print(f"Codex 來源：{', '.join(a for a, _ in codex_accounts)}")
    if not files:
        print("沒有找到任何 session 檔。")
        return
    if n_raw > len(files):
        print(f"  （去重 {n_raw - len(files)} 個跨來源重複的 Claude session）")
    print(f"掃描到 {len(files)} 個 session 檔，比對變動中…")

    old_manifest, manifest_stale = load_manifest(out)
    manifest = {} if args.force else old_manifest
    # 任何「縮小範圍」的旗標下，都沿用其他範圍的舊紀錄並略過孤兒清理，避免誤刪未處理的輸出
    filtering = bool(args.project or args.claude_source or args.account or args.no_claude
                     or args.codex_source or args.no_codex)
    # renderer 升級後 manifest 被視為空：明確「子集」建置會讓未涵蓋範圍暫時從索引/報告消失，提醒補跑全量。
    # 只對真正子集的旗標示警；--claude-source/--codex-source 是指定來源位置（常為正常全建），不算子集。
    subsetting = bool(args.project or args.account or args.no_claude or args.no_codex)
    if manifest_stale and subsetting:
        print("  ⚠ 偵測到 renderer 版本更新（manifest 需重建）：本次為縮範圍建置，未涵蓋的範圍會暫時"
              "從索引/報告消失；請另跑一次涵蓋全部來源的完整建置以補齊。", file=sys.stderr)
    # 預設（無縮範圍旗標）時 Claude + Codex 兩邊都會掃，可安全全量對帳
    new_entries = dict(old_manifest) if filtering else {}
    scanned_source_dirs = {safe_name(source_kind, 24) for source_kind, *_ in files}

    codex_titles = {}
    for _, sessions_root in codex_accounts:
        codex_titles.update(load_codex_thread_names(sessions_root))

    # Claude 專案 memory：本次掃描到的帳號 -> projects 目錄；memo 每個 (帳號, munged) 的 memory 內容（只讀一次）
    acc_root = dict(accounts)
    mem_cache = {}

    def get_mem(account, munged):
        k = (account, munged)
        if k not in mem_cache:
            root = acc_root.get(account)
            mem_cache[k] = load_memory_dir(root / munged / "memory") if root else None
        return mem_cache[k]

    proj_names = build_project_names(files)
    n_build = n_reuse = n_empty = 0
    for source_kind, acc_name, proj_name, sf in files:
        key = manifest_key(source_kind, sf)
        sig = session_signature(sf)
        cached = manifest.get(key) or {}
        row = cached.get("row")
        if source_kind == SOURCE_CODEX and args.project and row and not row_matches_project(row, args.project):
            continue
        new_entries.pop(key, None)
        reusable = (
            cached.get("sig") == sig and row
            and (source_kind != SOURCE_CLAUDE                  # 正規名變了就重建，避免同夾兩名/舊檔名殘留
                 or (row.get("proj") == proj_names.get((acc_name, proj_name))
                     and "cache_steps" in row))                # 缺 cache_steps（理論上不會）→ 重建補上
            and (args.include_empty or not row.get("empty"))
            # 檔案存在還不夠：has_* 旗標記錄該檔確實由本 row 的 sig+renderer 產生，
            # 擋掉「格式切換建置留下的過期檔」被當成現行輸出（--search 也靠同一旗標）
            and (not want_html or (row.get("has_html", True) and (sess_dir / row["out_html"]).exists()))
            and (not want_md or (row.get("has_md", True) and (sess_dir / row["out_md"]).exists()))
        )
        if reusable:
            new_entries[key] = {"sig": sig, "row": row}
            n_reuse += 1
            continue

        try:
            if source_kind == SOURCE_CODEX:
                s = load_codex_session(sf, acc_name, codex_titles)
            else:
                s = load_session(sf, proj_name, acc_name, source_kind)
        except Exception as e:
            print(f"  ! 解析失敗 {sf.name}: {e}", file=sys.stderr)
            continue
        if source_kind == SOURCE_CODEX and not project_matches_filter(s, args.project):
            continue
        if not any(e.get("type") in ("user", "assistant") for e in s.events):
            continue
        analyze(s)
        if s.n_turns == 0 and not args.include_empty:
            n_empty += 1
            continue
        if source_kind == SOURCE_CLAUDE:                       # 一夾一專案名：外來複本改用所屬夾的正規名
            s.proj_display = proj_names.get((s.account, s.proj_munged), s.proj_display)
        date = s.start.astimezone().strftime("%Y%m%d-%H%M") if s.start else "nodate"
        safe_source = safe_name(s.source_kind, 24)
        safe_acc = safe_name(s.account or "default", 24)
        (sess_dir / safe_source / safe_acc).mkdir(parents=True, exist_ok=True)
        base = f"{date}__{safe_name(s.proj_display,24)}__{s.session_id[:8]}"
        s.out_html = f"{safe_source}/{safe_acc}/{base}.html"
        s.out_md = f"{safe_source}/{safe_acc}/{base}.md"
        s.mem_href = ""
        if source_kind == SOURCE_CLAUDE and get_mem(s.account, s.proj_munged):
            s.mem_href = memory_rel_path(s.account, s.proj_munged)
        if want_html:
            mem_link = ("../" * len(PureWindowsPath(s.out_html).parts) + s.mem_href) if s.mem_href else ""
            (sess_dir / s.out_html).write_text(
                render_session_html(s, rel_index_href(s.out_html), mem_link), encoding="utf-8")
        if want_md:
            (sess_dir / s.out_md).write_text(render_session_md(s), encoding="utf-8")
        row = session_to_row(s)
        # has_html/has_md＝該檔與本 row 的 sig+renderer 同步。縮格式建置（--format md/html）時，
        # 另一格式若在「同一 sig」下產過且檔仍在，旗標沿用；sig 變了就不可信（過期檔）。
        prev = cached.get("row") if cached.get("sig") == sig else None
        row["has_html"] = want_html or bool(prev and prev.get("has_html")
                                            and (sess_dir / row["out_html"]).exists())
        row["has_md"] = want_md or bool(prev and prev.get("has_md")
                                        and (sess_dir / row["out_md"]).exists())
        new_entries[key] = {"sig": sig, "row": row}
        n_build += 1

    rows = [e["row"] for e in new_entries.values()
            if args.include_empty or not e["row"].get("empty")]
    show_account = len({r.get("account", "") for r in rows}) > 1

    # 產生「有 memory/ 的 Claude 專案」的 memory 頁。
    # 只處理「本次實際掃描到的」(帳號, munged) 專案——精準涵蓋 --project / --account / --no-claude 等所有縮範圍旗標；
    # 範圍外 row（filtering 模式沿用舊 manifest）完全不動其 mem_href，沿用磁碟上既有頁，避免誤清與寫回損壞。
    scanned_claude = {(a, p) for sk, a, p, _ in files if sk == SOURCE_CLAUDE}
    n_mem = 0
    if want_html:
        done_mem = set()
        for r in rows:
            account, munged = r.get("account", ""), r.get("proj_munged", "")
            if (account, munged) not in scanned_claude:   # 範圍外：保留舊連結，不重產不刪
                continue
            mem = get_mem(account, munged)
            if not mem:
                r["mem_href"] = ""               # 範圍內但無有效 memory：移除連結避免破連結
                continue
            r["mem_href"] = memory_rel_path(account, munged)
            if r["mem_href"] in done_mem:
                continue
            done_mem.add(r["mem_href"])
            dest = out / r["mem_href"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            idx_href = "../" * (len(PureWindowsPath(r["mem_href"]).parts) - 1) + "index.html"
            dest.write_text(render_memory_html(mem, r["proj"], munged, idx_href), encoding="utf-8")
            n_mem += 1
        # 清掉已不存在專案的孤兒 memory 頁（縮小範圍模式下不清，避免誤刪未處理範圍）
        mem_root = out / "memory"
        if not filtering and mem_root.is_dir():
            keep = {(out / r["mem_href"]).resolve() for r in rows if r.get("mem_href")}
            for f in mem_root.rglob("*.html"):
                if f.resolve() not in keep:
                    try:
                        f.unlink()
                    except OSError:
                        pass

    # 快取分析報告：彙整所有 Claude session 的 cache_steps（增量建置時沿用 row 內存的序列，免重讀）
    cache_data = build_cache_report(rows)
    has_report = cache_data["has_data"]
    if has_report and want_html:
        (out / "cache-report.html").write_text(render_cache_report_html(cache_data), encoding="utf-8")
        (out / "cache-hypotheses.html").write_text(render_cache_hypotheses_html(cache_data), encoding="utf-8")
    if has_report and want_md:
        (out / "cache-report.md").write_text(render_cache_report_md(cache_data), encoding="utf-8")
        (out / "cache-hypotheses.md").write_text(render_cache_hypotheses_md(cache_data), encoding="utf-8")
    if not has_report:                       # 沒有可分析資料：清掉舊報告，避免索引指向過期檔
        for stale in (out / "cache-report.html", out / "cache-report.md",
                      out / "cache-hypotheses.html", out / "cache-hypotheses.md"):
            try:
                stale.unlink()
            except OSError:
                pass

    if want_html:
        (out / "index.html").write_text(render_index_html(rows, show_account, has_report), encoding="utf-8")
    if want_md:
        (out / "index.md").write_text(render_index_md(rows, show_account, has_report), encoding="utf-8")

    # 清掉已不存在 session 的孤兒輸出檔（縮小範圍模式下不清，以免誤刪未處理的帳號/專案）
    removed = 0
    if not filtering:
        allowed = {r["out_html"] for r in rows} | {r["out_md"] for r in rows}
        for f in sess_dir.rglob("*"):
            rel = f.relative_to(sess_dir)
            if (f.is_file() and rel.parts and rel.parts[0] in scanned_source_dirs
                    and rel.as_posix() not in allowed):
                try:
                    f.unlink(); removed += 1
                except OSError:
                    pass

    save_manifest(out, new_entries)

    bits = [f"新建/更新 {n_build}", f"沿用 {n_reuse}"]
    if n_empty:
        bits.append(f"略過空 session {n_empty}")
    if n_mem:
        bits.append(f"memory 頁 {n_mem}")
    if removed:
        bits.append(f"清除孤兒檔 {removed}")
    print(f"完成 ✅ 共 {len(rows)} 個 session（" + "，".join(bits) + f"）→ {out.resolve()}")
    index_path = (out / "index.html").resolve()
    if want_html:
        print(f"  索引： {index_path}")
        if has_report:
            print(f"  快取分析報告： {(out / 'cache-report.html').resolve()}")
            print(f"  快取假說檢定： {(out / 'cache-hypotheses.html').resolve()}")
    if args.open and want_html:
        try:
            webbrowser.open(index_path.as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
