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
import os          # 封存的原子建檔（O_EXCL）與紀錄落磁（fsync）需要，見 `_archive_loop()`
import re
import shutil
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
# 渲染邏輯版本；改變 session 呈現方式或 row 結構時 +1，會強制全部重建。
# ⚠⚠ **這一格很容易漏，而漏掉是靜默的**：呈現層改了卻沒 +1，增量建置會沿用既有的
#    HTML／MD，新的顯示在**既有頁面上永遠不出現**，而快取報告那一頁每次都重建 →
#    變成「報告說有 N 次，逐步徽章一個都看不到」的不一致，沒有任何錯誤訊息。
#    2026-08-22 實測抓到這件事：`out/sessions` 的 1037 個頁面只有 1 個是當天寫的，
#    抽查到的那一份檔案時間停在 08-19，08-19 之後三批呈現層修正
#    （v36-fam3 F2 單步徽章、v36-fam4 #2 tooltip、v36-fam5 #1 並列標示）全部沒生效。
# 39 → 40（2026-08-22）：把上述三批補上。**動到 render_turn_html／render_turn_md／
#    _cold_cause_label／_cold_cause_title 的改動，都要記得動這裡。**
# 40 → 41（2026-08-22）：耐久回合錨點（`durable_anchor`）＋每輪標頭的 # 直達連結。
# 41 → 42（2026-08-22）：補 hashchange 進入點、橫幅改 fixed、tiebreak 改用來源檔行序。
#    ⚠ **前兩者是頁面 JS/CSS、第三者會改變 id 值**——三個都是呈現層。差點又漏掉這一格：
#    改完 JS 和 CSS 很容易覺得「沒動 render_turn_html 就不算呈現層」，那是錯的。
# 42 → 43（2026-08-22）：撞號後綴 `-<n>` → `-tb<n>`（錨點文法每段自帶前綴）、
#    isK 不准後面黏垃圾、退化改以 `bmTurnId()` 當底線、`.alink` 隱藏時關掉命中測試。
#    ⚠ **後綴格式改了＝既有的 id 值改了**，所以這也是呈現層。
# 43 → 44（2026-08-22）：橫幅移到視窗底部＋關閉鈕、命中撤橫幅、未命中清 .hl。
#    ⚠ 全是頁面 JS/CSS ＝ 呈現層。
# 44 → 45（2026-08-22）：撞號 tiebreak 的排序鍵改成 (來源檔, 行序)，整場可比。
#    ⚠ 這會改變撞號組裡誰拿無後綴錨點 ＝ id 值可能改變 ＝ 呈現層。
# 45 → 46（2026-08-22）：關閉鈕加大到 28×28、橫幅在小視窗收緊、bmWhen 驗日期範圍。
# 46 → 47（2026-08-22）：**書籤第 2 期**——每輪標頭加 ☆ 鈕、書籤對話窗、localStorage、
#    匯出匯入。⚠ 動到 HTML（新元素）＋CSS＋頁面 JS，三樣都是呈現層。
# 47 → 48（2026-08-22）：**書籤第 3 期**——書籤 JS 拆成 `_BOOKMARK_CORE_JS`（三頁共用）＋
#    `_BOOKMARK_PAGE_JS`（session 頁專屬）、對話窗的類別改成 chip ＋「＋ 新類別」就地新增、
#    多內嵌一個 `BK_MGR`。⚠ **session 頁的 JS 與 CSS 都變了 ＝ 呈現層**。
#    ⚠ 管理頁（`out/sessions/bookmarks.html`）與設定頁（`out/sessions/settings.html`）
#    本身**不受這個版本閘管**——它們和 `index.html` 一樣每次執行都無條件重產。
# 51 → 52（2026-08-23）：**書籤第 4 期**——區塊層級粒度。每個可標記區塊多一層 `.blk`
#    包裝＋自己的 `#`／☆，錨點 `k…[-tb<n>][-s<步epoch>]-b<n>`（見 `block_anchor()`）；
#    多步回合的步驟列掛上 `k…-s<ep>`（退化階梯的中間一階）；`bkSummary()` 改成先剔掉
#    控制項的文字再取摘要。⚠ **HTML＋CSS＋頁面 JS 三樣都變＝呈現層，一定要升。**
# 52 → 53（2026-08-23）：**第 4 期的冷眼輪處置**（`bookmarks-p4-fam`）——
#    ① 區塊錨點的 `-s` 段改用**毫秒**（新增 `_epoch_ms`／`_step.tms`），
#      並加一道底線：同一輪內 `tms` 撞號的那些步，整步不給錨點。
#      ⚠ **錨點字串改了**，但第 4 期未出貨，沒有人存過這種書籤。
#    ② `.blk-ctls` 容器改成透明＋`pointer-events:none`（背景移到按鈕自己身上）。
#    ⚠ CSS 與錨點值都變了 ＝呈現層，一定要升。
# 53 → 54（2026-08-23）：**跨模型輪的處置**（`bookmarks-p4-codex`）——
#    新增 `bmGrammarOk()`：文法不合的子定位符一律走未命中，**不准走退化階梯**
#    （舊版 `-s`、`-b`、`|`、引號、超長垃圾都會被剝成基底回合並框起來）。
#    另把區塊錨點的計算抽成 `turn_block_anchors()`，與探針共用。
#    ⚠ 頁面 JS 變了 ＝呈現層。
# 54 → 55（2026-08-25）：**使用者回合忠實度**——三種「使用者確實送出、但過去看不到或被
#    誤標成他的發言」的東西補回來（設計與全語料量測見 `planning/user-turn-fidelity.md`）：
#    ① **排隊送出的提示詞**：檔案裡沒有 `type:user` 事件，只有 `queue-operation` 與
#      `attachment.queued_command` ⇒ 過去是**答案在、問題不在**。`synth_queued_user_events()`
#      補成 user 事件，時刻取 `remove`（真正被讀到的一刻），按 Enter 的時刻進游標提示。
#    ② **背景任務通知**收成一列 `_notify` 摘要（原文摺疊），不再照使用者發言畫整坨 XML。
#    ③ **斜線指令／`!` bash** 變成 `_command` 列（指令＋參數＋結果），不再被
#      `is_noise_user()` 整則吃掉。
#    ⚠ **HTML＋CSS 都變，而且回合會變多 ⇒ 一定要升。**
#    ⚠⚠ **只有指令列不發耐久錨點；通知列照發。** 本行第一版寫成「指令列與通知列都不發」，
#      **那和實碼相反**（`_no_anchor` 只認 `_command`），而照著它改會弄丟 872 個既有錨點：
#      通知列在 v55 之前就是普通 user 回合、**本來就有錨點**，不發＝破壞；
#      指令列是這一版才長出來的，沒有人存過，不發才安全。
#      「新東西不拿」與「舊東西不再拿」在碼裡長得一樣，方向卻相反（教訓 53）。
# 55 → 56（2026-08-25）：排隊送出的那一句加上**「⏳ 中途插話」徽章**（HTML）與對應的
#    MD 記號。⚠ 這是 Will 在看過 55 之後追加的：位置已經裁決成「被讀到的那一刻」
#    ⇒ 它讀起來就像一般的一問一答，「這是助手工作到一半才收到的」如果只留在游標提示裡
#    等於沒有講。⚠ MD 那半落在 `_TURN_HEAD_RE` 的 `rest` 段，不影響 `--search` 切回合。
#    ⚠ **55 從未出貨**（沒有投影過），但版本照升不誤：manifest 的重建閘只認版本號，
#    不升的話**本機已經是 55 的 `out/` 會靜默不重建**——這一場才因為類似的事
#    整批頁面用錯版本產出過一次。
# 56 → 57（2026-08-25）：**中途插話不再切輪**，改成畫在該輪內部的子框。
#    ⚠⚠ 依據是量測，不是偏好：全語料 **125/127（98.4%）的插話是在同一輪之內送達的**
#    （前後兩個 assistant 事件之間沒有 `turn_duration`）⇒ 56 把它當獨立 user 回合，
#    等於把一輪切成兩輪，畫面上看起來像「助手講完 → 使用者說話 → 助手開新的一輪」，那是假的。
#    現在：`group_turns` 掛成 `_interject` 區塊；`render_turn_html` 畫成 `.ijbox` 子框，
#    其後**第一段回應**（一個步驟）縮排在 `.ijafter` 裡（Will 從三種畫法中挑的 B）。
#    ⚠ 真正跨輪的那 2/127 沒有開著的 assistant 回合可掛 ⇒ 自動落回一般 user 回合＋插話徽章。
#    ⚠⚠ **插話「不」算可標記區塊，拿不到 `-b<n>`。** 本行第一版寫的是相反的，
#      而第一版的碼也真的那樣做——結果是插話被插進 blocks 中間，
#      **同一步之內它後面每一個區塊的序號全部 +1**，既有的區塊書籤**指到別的內容**
#      （最壞的失敗方向：不失效、不報錯、安靜地指錯）。登記為
#      `SCOPE-INTERJECT-NO-BLOCK-ANCHOR`。**新長出來的東西一律不進序號。**
#    ⚠ HTML＋CSS＋MD 都變、而且回合數會**變少**（不再切輪）＝呈現層，一定要升。
# 57 → 58（2026-08-25）：**指令的第二種存法也補進來**。
#    ⚠⚠ 指令在 JSONL 裡有**兩種存法，而且並存**（實測，不是版本遷移）：
#    `type:user` 內含 `<command-name>`（575 則：`/effort`／`/model`／`/exit`／`!` bash）
#    與 `type:system, subtype:local_command`（345 則：`/context` 140、`/status` 83、
#    `/rename` 49、**`/remote-control` 29**、`/model` 21、`/resume` 13）。
#    57 只做了前者 ＝ **只做到 62.5%**，而 `/rc` 正好落在沒做的那一半
#    （Will 2026-08-25 驗收時抓到：「一開場我就用 /rc，這個沒有看到」）。
#    `synth_local_command_events()` 把後者轉成同一種事件，**沿用同一條指令路徑**。
#    ⚠ 兩種形狀實測**零重疊**，補了不會重複畫。
#    ⚠ 順帶修掉一個幽靈回合：沒有輸出的指令會另寫一則空的
#      `<local-command-stdout></local-command-stdout>`，它渲染成空字串**但已經佔掉一個回合**
#      （`t{n}` 序號往後推）。判空條件收斂到 `user_special_blocks()` 一處。
# 58 → 59（2026-08-26）：**跨模型收斂輪 `utf-fix-codex` 的處置**。呈現層真的變了三處，
#    所以一定要升（不升的話本機既有的 `out/` 會靜默沿用舊版）：
#    ① **通知的摺疊改回完整原文**（原本 `event or raw` ⇒ 帶 `<event>` 的 Monitor 型會
#       把 `<task-id>`／`<output-file>` 從 HTML 與 MD 全文索引裡弄不見，**比 v54 還差**）；
#       原文另外過 `_strip_ansi()`（先前通知那條路完全沒剝控制碼）。
#    ② `turn_duration` **之後**才被讀到的排隊句改標「這一輪結束後才讀到」。
#       ⚠ 位置**沒有變**（仍掛在該輪內部）——改成獨立回合會切輪，實測弄丟 54 個舊錨點。
#    ③ 插話是該輪最後一個區塊時，那個空的「↳ 回應這句」框整個撤掉（先前會畫出空框）。
#    另外：控制碼補上 DCS／SOS／PM／APC 與 C1 單位元組形式（先前只認 CSI 與 OSC，
#    `ESC P …payload… ESC \` 的 payload 會原樣進頁面）。
#    ⚠ 錨點集合仍逐字不變：`probe_user_turns.py anchors <開工前的 commit>` 現撈。
#    ⭐ **驗收輪 `utf-fix-codex-r2` 又補了五處**（同一個版本號，59 尚未出貨）：
#    ④ 未知內容的保守否決**只豁免 `str`**——`content` 是單一個 dict、或 list 裡混了
#       非 dict 的非空元素，第一版都當「認得」⇒ 照樣被搬走。
#    ⑤ `_turn_done` **在新的 assistant 內容抵達時要清掉**：只設不清的話，
#       下一輪跑到一半的插話會被誤標成「上一輪結束後才讀到」。
#    ⑥ 封存的順序改成 **佔名 → pending 落磁 → 刪頁 → 搬移**，而且**每一條失敗路徑
#       都收回佔位檔**：第一版注入一次 `fsync` 失敗就留下「頁面已刪 ＋ 0 位元組佔位檔」，
#       下一次執行永遠撞「目的地已有同名檔」。
#    ⑦ 排隊句的 attachment 以 `(來源檔, prompt)` 分組，而且**每個 remove 都消耗一個
#       時刻**（不論最後是合成事件還是真 user 事件呈現）——否則跨來源會憑空補一句，
#       重複送出會拿到前一次的按 Enter 時刻。
#    ⑧ `_CTRL_RE` 的範圍補到 `\x9f`（裸的 C1）；指令區塊多帶 `src`（哪一種存法），
#       那是 `render` 探針的身分，避免兩種存法互相冒領。
# 59 → 60（2026-09-01）：**排隊送出時貼的圖片也畫出來**。
#    先前補出來的那一則只帶文字、圖片 block 被丟掉 ⇒ 使用者貼的圖在頁面上不存在；
#    「只貼圖、一個字都沒打」的那一則甚至**整則不出現**（文字是空的，配不到 `remove`）。
#    ⚠⚠ **圖片畫在插話子框「裡面」，不另開可標記區塊。** `_interject` 之所以不佔
#    `-b<n>` 序號，就是為了保住既有區塊書籤（範圍限制 SCOPE-INTERJECT-NO-BLOCK-ANCHOR）；
#    把圖片當成獨立 block 塞進 `cur["blocks"]` 會讓**同一步之後每個區塊的序號 +1**。
#    ⚠ 跨輪送達（自成一輪）的那一種走一般 user 回合：圖片是**那一輪自己的新區塊**，
#    只往後長、不會動到別人的錨點。
#    ⚠ HTML 與 MD 都變 ⇒ 一定要升（不升的話既有的 `out/` 會靜默沿用舊版）。
RENDERER_VERSION = 60
SOURCE_CLAUDE = "claude-code"
SOURCE_CODEX = "codex"

# session 型態（自動分類）：review／exec／chat（一般互動）。快取統計上 review/exec 與互動
# session 是不同母體（2026-07-18 以 175 個本機 rollout 實測：本語料 85% 為 exec），故標記＋可篩選、
# 且 Claude 報告可依 review／一般 分層。
# - review：首句（去噪後）符合 REVIEW_RE——辨識「reviewer 角色/格式」，涵蓋 review runner 標頭
#   「# Review: <label>」、「… Review Prompt」H1、手寫「你是 Reviewer session…」/「You are (a) Reviewer」、
#   「你是一位資深…審查者…」；Claude/Codex 皆適用（反向流程 Claude 當審查者也會中）。
#   刻意只認 reviewer 角色措辭，不認裸 "review"／"審查"，以免誤傷一般 chat（例如 Worker 跑 /dflow:pr-review）。
# - exec：Codex 無頭執行（session_meta.originator == "codex_exec"／source == "exec"）。
# - 首句為 review 的 exec session 歸 review（review 是更有資訊量的標籤）。
REVIEW_RE = re.compile(
    r"(?i)"
    r"^#\s*review"                       # 「# Review」標頭（含 # Review:／# Review Prompt）
    r"|review\s+prompt\b"                # 「… Review Prompt」標頭
    r"|reviewer\s+session"               # 你是 Reviewer session／You are (a) Reviewer session
    r"|你是一位.{0,15}審查者"             # 資深程式碼審查者（cross-model review runner）
    r"|you\s+are\s+(?:an?\s+)?reviewer"  # 英文 reviewer 角色
)
KIND_LABELS = {"review": "review", "exec": "exec", "chat": "一般"}
KIND_TITLES = {"review": "首句為 reviewer 角色／review prompt——cross-model review session",
               "exec": "codex exec 無頭執行（session_meta.originator）"}
SOURCE_LABELS = {
    SOURCE_CLAUDE: "Claude Code",
    SOURCE_CODEX: "Codex",
}


def source_label(kind: str) -> str:
    return SOURCE_LABELS.get(kind, kind or "未知")


# 對話裡 AI 那一方的顯示名（回合標頭用）：Claude Code → Claude、Codex → Codex。
# 注意：MD 標頭的「🤖 <名>」被全文搜尋的 _TURN_HEAD_RE 比對，改這裡要同步放寬該正則。
AI_NAMES = {SOURCE_CLAUDE: "Claude", SOURCE_CODEX: "Codex"}


def ai_name(kind: str) -> str:
    return AI_NAMES.get(kind, "AI")


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def esc_attr(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def js_embed(obj) -> str:
    """把 Python 值變成可直接內嵌進 `<script>` 的 JS 字面值。

    ⚠ **四樣都要跳脫，少一樣就是一個洞**：
    - `<` / `>`：資料裡出現 `</script>` 會直接破出標籤（對話內容真的會有）；
    - U+2028 / U+2029：JS 的行終止符，不跳脫會把字串字面值攔腰截斷。

    `json.dumps` 自己**不做**這四樣，所以每個內嵌點都得補——補漏一處就白防。
    索引頁的 memory 對照表原本就是這樣寫的，抽出來共用，不要再寫第二份。"""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


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
                              # ——與 REPORT_GAP_BUCKETS 同供 Codex 存活頁（cache-codex）重用
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
    ("switch",  "limit/切帳號",   "不可避免：429/401 邊界後第一步（快取按組織／workspace 隔離）"),
    ("model",   "換模型",         "結構性：快取按模型隔離（API 自報 model_changed 亦歸此）"),
    ("tools",   "工具定義變動",   "結構性〔API 自報〕：載入/移除 MCP、skill、動態工具 → 工具區後整段重寫"),
    ("system",  "系統提示變動",   "結構性〔API 自報〕：CLAUDE.md／系統前綴改動 → 其後整段重寫"),
    ("msgs",    "前文內容變動",   "結構性〔API 自報〕：倒帶重編/前文被改寫 → 變動點後重寫"),
    ("unavail", "伺服器不可用",   "不可避免〔API 自報〕：伺服器側不可用，不是快取沒撐住"),
    ("unknown", "未知的自報成因", "〔API 自報〕Claude 回報了本工具還不認得的成因型別"),
    ("compact", "壓縮後",         "結構性：/compact 或自動壓縮改寫前綴"),
    ("expiry",  "閒置過期",       "可避免：間隔超過 TTL"),
    ("acct",    "自行切帳號",     "人因可避免：沒撞 limit 就換帳號（快取按組織／workspace 隔離）"),
    ("evict",   "提早失效",       "異常：1h TTL 內（2–55 分）卻冷啟"),
    ("intra",   "回合內雜訊",     "快取斷點/20-block 回溯的一次性 miss，非 TTL"),
]

# 逐步／回合徽章的冷啟著色：只有「真的把快取弄丟」（閒置過期、異常提早失效）才醒目紅標；
# 其餘結構性、本來就不會命中的（首呼叫、切帳號、換模型、壓縮後、回合內斷點）改中性灰，
# 避免被誤讀成「快取一直失效」。未歸因（如 ctx 太小的暖機、子代理另有前綴）比照結構性處理。
_COLD_GENUINE = {"expiry", "evict", "acct"}
_COLD_CAUSE_NOTE = {
    "first":   "首呼叫：全新前綴，無快取可命中（結構性、不可避免）",
    "switch":  "額度/帳號邊界後第一步：快取按組織／workspace 隔離（結構性）",
    "model":   "換模型：快取按模型隔離（結構性）",
    "tools":   "Claude 自報：工具定義變動（載入/移除 MCP、skill、動態工具）→ 工具區之後整段重寫（結構性）",
    "system":  "Claude 自報：系統提示變動（CLAUDE.md／系統前綴改動）→ 其後整段重寫（結構性）",
    "msgs":    "Claude 自報：前文內容變動（倒帶重編/前文被改寫）→ 變動點之後重寫（結構性）",
    "unavail": "Claude 自報：伺服器側不可用（不是使用者造成，也不是快取沒撐住）",
    "unknown": "Claude 自報了本工具還不認得的成因型別——先照實留著，不往下推論成別的成因",
    "compact": "壓縮後：前綴被改寫（結構性）",
    "intra":   "回合內快取斷點的一次性 miss，非 TTL 失效",
    "expiry":  "閒置超過 TTL，快取已過期（可避免）",
    "acct":    "這場中途換了帳號（沒撞 limit）：快取按組織／workspace 隔離，等於自己把前綴丟掉（人因可避免）",
    "evict":   "1h TTL 內（2–55 分）卻未命中＝提早失效（異常，值得注意）",
}


def _pct_display(cr, total):
    """命中率的顯示字串。冷熱判定一律用精確比值，但顯示若四捨五入到門檻上（24.5〜24.99% → 25%）
    就會出現「畫面寫 ⚡25%、卻著上冷啟色」而與報告寫的「門檻＝命中率 < 25%」打架 → 這種剛好踩線的
    情形多給一位小數。其餘照常取整。"""
    if not total:
        return "0"
    ratio = 100 * cr / total
    if round(ratio) == CACHE_COLD_PCT and ratio < CACHE_COLD_PCT:
        return f"{(1000 * cr // total) / 10:.1f}"   # 向下取到 0.1：連 24.99% 也要印成 24.9 而非 25.0
    return str(round(ratio))


def _cold_display(cause):
    """冷啟步的顯示類別（cause 由 classify_cache_causes 掛在 b["cause"]）：
      - 真失效 expiry/evict → 醒目紅標 'cold'；
      - None（未分析來源：Codex／子代理，沒跑成因分析）→ 維持原紅標 'cold'，不擅自改語意；
      - 其餘（結構性成因，或 "" ＝ Claude 已分析、非失效的暖機）→ 中性灰 'coldx'。"""
    if cause in _COLD_GENUINE or cause is None:
        return "cold"
    return "coldx"


# ── Claude API 自報的快取未命中成因（message.diagnostics.cache_miss_reason）──
# 2026-05 起的 Claude Code 版本才寫入這欄：平常是 null，只有這次呼叫真的沒命中（含只掉一段的
# 「部分失效」）才帶成因，部分成因另附 cache_missed_input_tokens＝**失效前綴有多長**
# （從多久以前的內容開始對不上），不等於這次重寫量——實測有 missed 遠大於本步寫入的案例。
# 有這欄時它是**實據**，優先於本工具依「間隔/邊界」推論的成因（見 classify_cache_causes）。
API_MISS_LABELS = {
    "tools_changed":              "工具定義變動",
    "system_changed":             "系統提示變動",
    "messages_changed":           "前文內容變動",
    "model_changed":              "換模型",
    "previous_message_not_found": "前文不在快取",
    "unavailable":                "快取暫時不可用",
    "unknown":                    "未知的新成因",
}
API_MISS_NOTES = {
    "tools_changed":              "工具定義變了（載入/移除 MCP、skill、動態工具）——工具區之後的前綴整段重寫",
    "system_changed":             "系統提示變了（CLAUDE.md／output style／系統前綴改動）——其後整段重寫",
    "messages_changed":           "前面訊息內容變了（倒帶重編、內容被改寫）——變動點之後重寫",
    "model_changed":              "換了模型：快取按模型隔離",
    "previous_message_not_found": "前一段前綴已不在快取（過期、被逐出，或壓縮/清空改寫了前綴）",
    "unavailable":                "快取本次暫時不可用（伺服器側）",
    "unknown":                    "Claude 回報了本工具還不認得的新成因型別（次數與失效前綴長度照計；型別名以通稱顯示）",
}
# 前綴被改掉＝client 造成、必然重寫，不是 TTL 失效 → 比照結構性邊界排除在 TTL 統計/假說樣本之外。
# previous_message_not_found／unavailable 不在此列：那正是「快取真的不在了」的形態，仍當失效候選
# （`unavailable` 另有成因對應，見下方 API_OTHER_CAUSE。⚠ **那條也會整對移出樣本**，只是走
# 另一個計數器 `srv_excluded`、另一句揭露 `_srv_excluded_note`——不是「只定成因」）。
# 前綴變動類對應到報告成因鍵（REPORT_CAUSES）；model_changed 併入既有的 "model"。
# （這個 dict 的 key 集合就是「前綴變動類」的唯一定義，不另立常數以免兩處分岔。）
# **範圍限制 SCOPE-UNMAPPED-MISS-CODE**：沒有列在這裡、也不在 API_OTHER_CAUSE 的自報碼，
# 在成因判定裡不參與——那一步會照「邊界／間隔」推論走完，推論的結果可能落進人因桶。
# `previous_message_not_found` 不列是刻意的：它是「快取沒中」的症狀本身，實測橫跨各種成因
# 都出現，不是成因。詳見 planning/scope-limits.md 的 SCOPE-UNMAPPED-MISS-CODE。
API_MISS_CAUSE = {"tools_changed": "tools", "system_changed": "system",
                  "messages_changed": "msgs", "model_changed": "model"}
# **非前綴變動**的自報成因：同一步的直接證據，優先於由「間隔」推論出來的 expiry／acct／evict／intra。
# 兩個成員：`unavailable`（伺服器側不可用）與 `unknown`（本工具還不認得的新型別，
# 由 `_step_miss` 以通稱回報）。**未知也算證據**——不映射的話那一步會繼續往下推論，
# 最後可能落進人因桶，等於把「我們看不懂的東西」算到使用者頭上。
# ⚠ 與 API_MISS_CAUSE **刻意分成兩個 dict**，因為兩者的下游用途不同：
#   ① 定成因——兩者都算數（伺服器掛掉不是使用者造成的，不該落進人因桶）。
#   ② TTL 樣本排除——**兩者都會整對移出**，但走的是不同的計數器與不同的揭露，不可合併：
#      前綴變動類記進 `api_excluded`／`band_excluded`，對外講成「因 API 自報**前綴被改掉**而移出」；
#      伺服器側不可用記進 `srv_excluded`，對外講成「因**伺服器不可用**而移出」。
#      理由不同（一個是分母被污染、一個是那次沒中不是快取的事），揭露的句子也不同。
#      更要緊的是 `band_excluded` 走反事實差額（`cf_comply_n − comply_n`）算，而反事實的定義是
#      「**API 自報前綴變動這條規則不存在**的世界」——伺服器側在那個世界裡照樣排除。合併成一個
#      集合的話，這些對會落進差額、被講成「因前綴變動移出」，那是錯誤歸因。
#      ⚠ 這裡刻意**不寫實測對數**。舊版寫「153 個相鄰步對」，之後有人拿別的量法得到 612 就以為
#      它過期了——其實 612 是同一批資料被數了四次（本機四個 config 的 `projects/` 是 junction，
#      指向同一實體；工具自己走 `_dedupe_by_realpath` 會收斂成一份，繞過去重的腳本不會）。
#      對數隨語料與量法變動，寫死在註解裡只會製造這種假的「已失效」。
# ⚠ 也**不參與**「向前借用被 ctx 門檻濾掉那一步的成因」：伺服器側是同一步的狀態，借給後一步
#   就變成推論了。
API_OTHER_CAUSE = {"unavailable": "unavail", "unknown": "unknown"}
# cache_steps 內以整數碼存成因（0＝無），避免 manifest 塞一堆重複字串。
API_MISS_CODES = ["", "tools_changed", "system_changed", "messages_changed",
                  "model_changed", "previous_message_not_found", "unavailable"]
# 未知新型別的哨兵碼刻意用固定高值、**不用 len(API_MISS_CODES)**：manifest 只要 RENDERER_VERSION 沒變
# 就會沿用舊 row，若哪天在上面 append 一種新 type，len 會位移、把存過的哨兵值重新解讀成那個新 type
# （未知的失效會變成「確信標好」的失效）。往 API_MISS_CODES 增類別時無須動這個常數。
API_MISS_UNKNOWN_CODE = 99


def _miss_reason(msg):
    """從 assistant message 取 API 自報的未命中成因 → (成因字串 or "", 失效前綴 token 數)。
    舊版資料沒有 diagnostics（或為 null）時回 ("", 0)。"""
    if not isinstance(msg, dict):
        return "", 0
    d = msg.get("diagnostics")
    if not isinstance(d, dict):
        return "", 0
    r = d.get("cache_miss_reason")
    if not isinstance(r, dict):
        return "", 0
    t = str(r.get("type") or "")
    try:                                     # 先取量再判型別：未知的新成因也要保住它附的長度，
        tok = int(r.get("cache_missed_input_tokens") or 0)   # 否則新 type 一出現就靜默少算
    except (TypeError, ValueError):
        tok = 0
    if not t:
        return "", 0
    return t, max(tok, 0)                    # 未知的新成因：照樣顯示原字串與長度，不吞掉


def miss_label(reason):
    return API_MISS_LABELS.get(reason, reason or "")


def rewrite_waste_usd(model, missed, c5=0, c1h=0, wrote=None):
    """前綴失效這件事「比直接命中多花多少」的估算；未知模型回 None。
    寫入倍率：知道本步實際寫入量 `wrote` 時（missed 已被它設上限），用 wrote 的 5m/1h breakdown
    逐段拆（與 call_cost／avoid_usd 同規則——missed==wrote 時恰化簡成 avoid_usd、missed<wrote 時按比例）；
    拿不到 wrote 才退回「有 1h 就整段 1h、否則 5m」的單一倍率近似。

    ⚠ `cache_missed_input_tokens` 量的是**失效前綴有多長**（從多久以前的內容開始對不上），
    不是「這次寫了多少」——實測有步驟回報 missed=344k 卻只寫入 8k、同時讀了 426k（命中 98%），
    那 344k 多半由別的快取段供應、根本沒重算。所以傳入本步實際寫入量 `wrote` 時以它為上限，
    多花的錢不可能超過「這次真的寫進去的量」。"""
    price = model_price(model)
    if not price or missed <= 0:
        return None
    if wrote is not None:
        missed = min(missed, max(wrote, 0))
        if missed <= 0:
            return None
    if wrote:                       # 有實際寫入量 → 用它的 5m/1h breakdown 逐段拆（同 call_cost／avoid_usd），
        legacy = max(wrote - c5 - c1h, 0)   # 混合 TTL 不再整段按 1h（那會對前綴變動步系統性高估）
        mult = ((legacy + c5) * CACHE_WRITE_MULT + c1h * CACHE_WRITE_MULT_1H) / wrote
    else:
        mult = CACHE_WRITE_MULT_1H if c1h > 0 else CACHE_WRITE_MULT
    return missed * (mult - CACHE_READ_MULT) * price[0] / 1_000_000


# Markdown（無 CSS 著色）用的短成因標籤，與 REPORT_CAUSES 對齊。
_COLD_CAUSE_LABEL = {k: lbl for k, lbl, _ in REPORT_CAUSES}
# (v36-fam5 #1) **顯示層專用的並列條件**：不是成因，刻意不進 `REPORT_CAUSES`／`_COLD_CAUSE_LABEL`
# ——那兩者是成因表與分割測試的母體，塞一個假成因進去會多出一列永遠是 0 的成因。
# 這些鍵只會出現在 `masked`（並列顯示）裡，不記帳、不計次、不影響任何統計。
_COLD_EXTRA_LABEL = {"limit_earlier": "同場稍早撞過 limit"}
# ⚠ 這些補述會排在**成因說明之後**（見 `_cold_cause_title`）：它們的作用是修正前一句，
#   排在前面就會與後面那句互相否定——`acct` 的說明本文寫著「沒撞 limit」，而這一句正好在說
#   「同場稍早撞過」。順序反了就是 v36-fam4 #2 那個「同一個 tooltip 兩句話互相否定」。
# ⚠ 不要用 `**`：tooltip 是純文字 title 屬性，星號會原樣露出（既有的 `_COLD_CAUSE_NOTE`
#   一個都沒有用）。
_COLD_EXTRA_NOTE = {
    "limit_earlier": "但同一場對話在這一步之前就出現過 429/401，所以「沒撞 limit」只對這一對"
                     "相鄰步成立——這次換帳號可能其實是被迫的。兩者相隔已久，工具判不出是不是"
                     "同一件事，因此兩個都標出來讓你自己判斷",
}


def _cold_cause_label(cause, masked=None):
    """冷啟成因的顯示標籤。同一步若還成立別的條件，並列成「自行切帳號＋伺服器不可用」。

    ⚠ 並列的是**顯示**，不是記帳：成因欄與金額仍只認 `cause` 那一個，不重複計。
    順序是「同時成立的條件在前、定成因的那個在後」——前者是使用者看得懂也最想知道的那半，
    後者是實際被記帳的那一個（說明文字會講清楚記在哪一邊）。"""
    lbl = _COLD_CAUSE_LABEL.get(cause, "")
    if not masked:
        return lbl
    return "＋".join([_COLD_CAUSE_LABEL.get(c) or _COLD_EXTRA_LABEL.get(c, c) for c in masked]
                     + ([lbl] if lbl else []))


def _cold_cause_title(cause, masked=None, head="此步驟未命中快取"):
    """冷啟徽章的說明：並列標籤 ＋ 各條件的解釋 ＋ 「錢只算一次、記在哪一邊」。"""
    note = _COLD_CAUSE_NOTE.get(cause)
    if not masked:
        return (head + "——" + note) if note else head + "（未歸因，多為暖機/瑣碎呼叫或未分析來源）"
    parts = [_COLD_CAUSE_NOTE[c] for c in masked if c in _COLD_CAUSE_NOTE]
    if note:
        parts.append(note)
    # (v36-fam5 #1) 顯示層的補述排在**最後**：它們是對成因說明的修正（例：`acct` 的本文寫
    # 「沒撞 limit」，而 `limit_earlier` 正好在說同場稍早撞過）。排在前面兩句就會互相否定。
    parts += [_COLD_EXTRA_NOTE[c] for c in masked if c in _COLD_EXTRA_NOTE]
    return (head + "——" + _cold_cause_label(cause, masked) + "。" + "；".join(parts)
            + f"。金額與人因次數只算一次，記在「{_COLD_CAUSE_LABEL.get(cause, cause)}」。")


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
    "claude-fable": 1_000_000,
    "claude-mythos": 1_000_000,
    "claude-haiku": 200_000,      # 含 haiku 4.5
    "claude-3": 200_000,          # 3.x 世代（含 3-5-sonnet）一律 200k
}
# opus／sonnet 的 1M 是**分版本**的，不能用裸前綴一律當 1M：
#   1M ＝ opus 4.6／4.7／4.8／5＋、sonnet 4.6／5＋
#   200k ＝ opus 4／4.1／4.5、sonnet 4／4.5
# 先前裸前綴 fail-open 會把 sonnet-4-5 的 100k 脈絡算成 10%（實際 50%），差 5 倍。
CONTEXT_1M_MIN = {"opus": (4, 6), "sonnet": (4, 6)}


def _model_version(rest):
    """從 'opus-4-8-20260101' 這種尾段取出 (主版, 次版)；取不到回 None。
    純日期段（8 位數）不算次版，否則 'opus-5-20260101' 會被讀成 5.20260101 也無妨、但 4-20250514
    會被讀成 (4, 20250514) 而誤判成新版 → 明確排除。"""
    parts = [p for p in rest.split("-") if p]
    if not parts or not parts[0].isdigit():
        return None
    major = int(parts[0])
    minor = 0
    if len(parts) > 1 and parts[1].isdigit() and len(parts[1]) < 8:   # 8 位＝日期，不是次版號
        minor = int(parts[1])
    return (major, minor)


def context_window(model):
    """該模型的 context 視窗（token）；查不到回 None（只顯示絕對值、不顯示 %）。
    ⚠ 官方調整時要跟著改。未知的新版本刻意 fail-open 成 1M（新模型只會更大不會更小），
    但**已知的舊版本必須落 200k**——寧可不顯示，也不要顯示一個差 5 倍的百分比。"""
    m = str(model or "").lower()
    if "[1m]" in m:
        # 後綴本身就是視窗大小（Claude Code 對 1M context 的標法），比版本表更直接也更可信。
        # 不先擋掉的話，`claude-sonnet-4-5-…[1m]` 會走版本表被判成 200k，ctx 佔比一口氣差 5 倍
        # （100 萬 ctx 顯示成 500%）。
        return 1_000_000
    for prefix, w in CONTEXT_WINDOW.items():
        if m.startswith(prefix):
            return w
    for fam, floor in CONTEXT_1M_MIN.items():
        head = f"claude-{fam}-"
        if m.startswith(head):
            v = _model_version(m[len(head):])
            return 1_000_000 if (v is None or v >= floor) else 200_000
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


def epoch_str(ts, fmt="%H:%M"):
    """epoch 秒 → 本地時間字串；取不出來就回空字串。

    比照 `local_str` 的防護：`datetime.fromtimestamp` 對負值／極大值在部分平台會丟 `OSError`，
    顯示層不該因為一個取不出來的時刻就讓整頁建置失敗。"""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime(fmt)
    except (OSError, OverflowError, ValueError):
        return ""


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
        self.exec_origin = False    # Codex 無頭執行（session_meta.originator == "codex_exec"）
        self.acct_times = []        # 切帳號時刻（epoch 秒，含小數；analyze 時由 acct_switches 填）
        self.kind = "chat"          # 型態 review/exec/chat（analyze 時判定，見 REVIEW_RE 註解）
        self.models = []
        self.tok_out = 0
        self.ctx_peak = 0
        self.cost = 0.0
        self.cost_partial = False
        self.cache_pct = 0
        self.cache_steps = []
        self.cache_models = []
        self.cache_events = []
        self.codex_steps = []
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
            # 來源檔識別：`_i` 是**逐檔**行序（`enumerate(fh)` 每個檔從 0 重新起算），
            # 單獨拿它跨檔比較沒有意義。撞號 tiebreak 需要一個**整場可比**的鍵，
            # 所以再帶一個檔案識別；主檔的事件沒有這個欄位＝空字串，排在所有子代理檔之前。
            # ⚠ 分隔符正規化成 `/`：Windows 的 `relative_to` 給反斜線，不正規化的話
            #   同一份語料在不同平台會排出不同順序。
            srcf = str(sf.relative_to(side_dir)).replace("\\", "/")
            for ev in evs:
                ev["_srcf"] = srcf
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


def _codex_normalize_content(content):
    """(v36-fam6 #3) content 被**序列化成 JSON 字串**時還原成 list；回 `(content, 漂了沒)`。

    `_codex_content_text` 對 `str` 型的 content 原封不動回傳，於是整包 JSON 原文會被當成
    prompt 收下——與 `v36-fam3` F3 修掉的「`str()` 的 repr 被當 prompt」是同一類失效，
    只是漂移點再往內一層，F3 的修法沒有涵蓋到，而且新舊兩種格式**都**中。
    這個漂移形態不是憑空假設：`_codex_container_user_like` 本來就特地涵蓋
    「被序列化成 JSON 字串的 dict」——上游會這樣漂是本檔早就認可的前提。

    ⚠ **判準刻意收得很緊**：只有「解得出來、是 list、而且元素是帶 `type` 的 dict」才算漂移。
    使用者本來就有可能**真的把一段 JSON 當成問題貼進來**，那時解析它會把使用者的原文
    換成抽出來的片段——那是比漏報更糟的竄改。條件不滿足就原樣當純文字。
    ⚠ 只解一層（同 `_codex_container_user_like`）：雙重序列化不在涵蓋範圍內。"""
    if not isinstance(content, str):
        return content, False
    s = content.strip()
    if not s.startswith("["):
        return content, False
    try:
        parsed = json.loads(s)
    except Exception:
        return content, False
    if (isinstance(parsed, list) and parsed
            and all(isinstance(b, dict) and b.get("type") for b in parsed)):
        return parsed, True
    return content, False


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


def _codex_content_unconsumed(content, text_type="output_text"):
    """`_codex_content_text` **沒有吃掉**的 block 數（消費規則與它逐條對應，改一邊要改兩邊）。

    ⚠ 只數 block、不看內容：目的是讓「有東西被丟掉」這件事**有人數得出來**，
    不是要把圖片也渲染出來（那是另一件事）。混合內容的 prompt（文字＋圖片）在舊寫法下
    只留文字、其餘靜默消失，而抽得到文字就不會觸發「取不出文字」那個哨兵——
    於是頁面看起來完整、卻少了一半內容，正是本檔一再要擋的那種形狀。"""
    if not isinstance(content, list):
        return 0
    n = 0
    for item in content:
        if not isinstance(item, dict):
            n += 1
            continue
        if ((item.get("type") == text_type or (not item.get("type") and item.get("text")))
                and item.get("text")):
            continue
        n += 1
    return n


def _codex_container_user_like(raw):
    """事件的**容器本身不是 dict** 時，它裡面看不看得出是使用者發言。
    `item_completed` 的 `item` 與事件的 `payload` 兩層共用——兩層的失效樣態完全相同。

    ⚠ 為什麼需要這個：舊寫法把非 dict 的容器直接換成 `{}`，於是那一層以下的哨兵全部看不到
    東西；而外面那一層又不帶 role（實測 485 個 rollout 皆然）、`_codex_event_user_like`
    也接不到 → 整則 prompt 靜默消失、沒有任何一個計數會動。
    判準沿用同一套（型別名帶 user，或自報 role=user），只是多走一層容器。

    涵蓋三種容器形態：list/tuple 包 dict、被序列化成 JSON 字串的 dict、JSON 字串包 list。
    ⚠ 只解一層字串：字串裡再包一層字串（雙重序列化）不在涵蓋範圍內。"""
    def _looks(d):
        return (isinstance(d, dict)
                and (str(d.get("role") or "").lower() == "user"
                     or "user" in str(d.get("type") or "").lower()))
    if isinstance(raw, str):
        text = raw.strip()
        if text[:1] not in ("{", "["):
            return False                     # 不是 JSON 字面值，沒有再往下解的意義
        try:
            raw = json.loads(text)
        except Exception:
            return False
        if isinstance(raw, dict):
            return _looks(raw)
    if isinstance(raw, (list, tuple)):
        return any(_looks(x) for x in raw)
    return False


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


# event_msg 這一層我們實際處理的型別；其餘一律不認得。哨兵用它當「已知」白名單，
# 不認得又長得像使用者發言時才出聲（見 _codex_event_user_like）。
_CODEX_HANDLED_EVENTS = ("task_started", "user_message", "item_completed", "token_count")
# (v36-fam5 #2) `response_item` 的 `payload.type` 白名單。**已知**＝處理過的 ＋ 刻意不處理的，
# 兩類都要列：這是「認不認得」的清單，不是「有沒有用」的清單。不在清單上就是上游加了新型別
# 或改了名，而 `response_item` 是助理訊息／reasoning／工具呼叫的唯一入口——改名之後那些內容
# 會整批解析不出來，頁面變成「N 則提問、零則回答」，而既有哨兵一個都接不到
# （容器那條只在 payload 不是 dict 時計數；`n_empty_turns` 那條被 `n_ai_turns` 擋住，
#  因為助理事件正好是 0）。實測：把 `message` 改成 `Message` → 47 則回答掉成 35、stderr 全空。
# ⚠ 後四個是**現有語料裡出現、但本工具刻意不呈現**的（實測 516 份 rollout：web_search_call 143／
#   tool_search_call 61／tool_search_output 61／agent_message 2）。不列的話哨兵一上線就對 267 筆
#   真實事件狂叫，而一個會誤報的警告很快就沒有人讀。
_CODEX_HANDLED_RESPONSE_ITEMS = frozenset((
    "message", "reasoning", "function_call", "custom_tool_call",
    "function_call_output", "custom_tool_call_output",
    "web_search_call", "tool_search_call", "tool_search_output", "agent_message",
))
# (v36-fam6 #2) `response_item:message` 的已知 `role`。上游改名或拿掉 role，`role != "assistant"`
# 那一行會**直接吞掉**整則助理訊息——症狀與型別改名一模一樣（頁面「N 則提問、零則回答」、
# stderr 全靜），而 v36-fam5 #2 補的三條哨兵一條都接不到。
# 實測 516 份 rollout：role 只有 developer 999／user 1203／assistant 6712，這三個蓋滿。
_CODEX_KNOWN_MESSAGE_ROLES = frozenset(("assistant", "user", "developer"))


def _codex_dual_shape(pending, text, kind):
    """`pending`（前一則尚未被內容事件隔開的 prompt）與這一則**看起來像**同一則的兩種表述。

    三個條件缺一不可：**文字相同**、**非空**、**來源格式不同**。
    ⚠ 「看起來像」不等於「是」：使用者在佇列裡連送兩次同一句話，若那兩次分別被記成不同格式，
    形狀與雙表示**完全相同**。兩種格式都不帶可互相關聯的 turn／item 身分 → 分不出來。
    所以本判斷**只用來出聲**（見 `n_dual_shape`），**不授權刪掉任何一則**：多顯示一則是看得見
    的雜訊，刪掉一則是看不見的資料遺失，兩者不對等。
    ⚠ 空字串不算：取不出文字該由 `n_empty_user_items` 那個哨兵接手。

    **範圍限制 SCOPE-DUAL-SHAPE-UNMARKED**，兩格：
    (a) 出聲的管道只有 stderr；兩則在**輸出頁面上不帶任何標記**，讀的人看到的是兩則相鄰的
        普通提問。
    (b) ⚠ **這個哨兵本身也幾乎恆為 False**（2026-08-21 登記，來源 `v36-fam3` F5）：它只認
        **緊鄰**，而 `pending_user` 會被**任何** `response_item` 無條件清掉。本機 516 份 rollout、
        672 則使用者紀錄，**672 則的前一筆事件全部都是 `response_item`** → 實跑觸發 0 次。
        所以就算上游開始雙發，中間夾一筆 `response_item` 就完全無聲。
        **不要再把「等 stderr 告警」當成回頭處理的觸發器**——要重掃 corpus 才看得到。
        放寬的作法是改成在同一個**回合窗**內比對（窗已有 `turn_prompts` 在記帳），
        但本機沒有正例可驗證，Will 2026-08-21 選擇不動。
    詳見 planning/scope-limits.md 的 SCOPE-DUAL-SHAPE-UNMARKED。"""
    if not pending or not text or not text.strip():
        return False
    return pending[0] == text and pending[2] != kind


def _codex_event_user_like(payload, ptype):
    """這個**未知的 event_msg 型別**看起來是不是「使用者發言」。
    0.147 的漂移是把 user_message 換成 item_completed（item 那一層有哨兵接住）；
    但下一次上游大可再換成別的 payload.type，那時整場 prompt 會一則不剩而毫無聲音——
    item 層的哨兵看不到這種，因為它根本進不了 item_completed 那一支。
    判準與 item 層同一套：**型別名帶 user，或 payload 自報 role=user**（實測 485 個本機 rollout：
    帶 user 的型別只有已處理的 user_message、event_msg 從不帶 role → 現有資料零誤報）。"""
    if not isinstance(payload, dict):
        return False
    if str(payload.get("role") or "").lower() == "user":
        return True
    return "user" in str(ptype or "").lower()


def load_codex_session(path: Path, account: str = "default", thread_names=None) -> Session:
    s = Session(path, "codex", SOURCE_CODEX)
    s.account = account
    raw = load_events(path)
    s.events = []
    current_model = ""
    subagent_thread = False     # session_meta.source.subagent：子代理執行緒，本來就沒有人打字
    step_first_event = None     # 本次呼叫（上個 token_count 之後）的第一筆事件＝步驟起點
    last_usage_target = None    # 上一次 usage 掛載點：呼叫無可呈現事件時的後備（重播去重靠它）
    attached_usage: dict[str, set[tuple[int, ...]]] = {}
    # 使用者可見的那一則 prompt：0.146 以前（及 0.147 的 exec 路徑）發 event_msg:user_message；
    # 0.147 的互動模式（originator=codex-tui）改發 item_completed(item.type == "UserMessage")。
    # 兩種都要收，且**不可整檔擇一**：resume 會往同一個 rollout 追加（本機 473 檔 ↔ 473 thread，
    # 每檔恰好一個 session_meta），而格式跟著「當下的執行模式」走、不跟著 thread 走 → 同一檔可以
    # 前半舊格式、後半新格式。整檔擇一會把其中一半整段吃掉，且無聲。
    # ⚠ **兩則都收，一則都不刪。** 「同一則被兩種格式各發一次」與「使用者在佇列裡連送兩次同一
    # 句話、剛好被記成不同格式」在資料上長得一模一樣，而兩種格式都沒有可互相關聯的身分欄位
    # → 判不出來。歧義下刪資料違反「判重不得誤刪真實資料」；多顯示一則只是雜訊，且看得見。
    # 實測本機 corpus 490 檔、646 則 user 紀錄：緊鄰同文（不論同格式或跨格式）出現 **0 次**，
    # 所以保留兩則在現有資料上不會產生任何重複顯示。
    # 形狀仍要記錄並出聲（`n_dual_shape`）：上游哪天真的開始雙發，這個數字會先跳出來。
    pending_user = None                      # (文字, raw index, 格式)：剛收下、尚未被內容事件隔開的 prompt
    n_empty_user_items = 0                   # 使用者訊息取不出文字的筆數（欄位形狀漂移訊號）
    n_unhandled_user_items = 0               # item.type 像使用者訊息、卻不是我們認得的那個名字
    n_dropped_user_blocks = 0                # 使用者訊息裡沒被吃掉的 block 數（混合內容的靜默丟失）
    n_unhandled_user_events = 0              # event_msg 的 payload.type 像使用者發言、卻不認得
    n_unhandled_user_outer = 0               # 最外層的事件 type 就不認得、而 payload 像使用者發言
    n_drifted_response_items = 0             # response_item 的 payload 容器不是物件（助理內容整批會消失）
    n_dual_shape = 0                         # 緊鄰的跨格式同文筆數（可能雙表示；全部保留，只出聲）
    n_empty_turns = 0                        # 開了回合、卻一則 prompt 都沒收到的窗數（中止的不算）
    turn_open = False                        # 目前在不在一個回合窗裡
    turn_prompts = 0                         # 這個窗收到幾則
    turn_aborted = False                     # 這個窗被中止了（沒有 prompt 是正常的）
    # ⚠⚠ **壓縮／失敗的那一輪是系統自己起的**，沒有 prompt 同樣是正常的。
    # 實測（同事 2026-09-01 的 rollout；本機 565 份、729 個窗裡 0 個）：
    #   ① 自動壓縮 → `task_started` → 外層 `compacted` ＋ `event_msg:context_compacted`
    #      → `task_complete`，整窗零則 prompt；
    #   ② 壓縮失敗 → `task_started` → `event_msg:error` → `task_complete`。
    # 兩種都會讓「回合開了卻沒收到 prompt」那條哨兵在**完全正常的資料**上出聲。
    turn_systemic = False
    turn_error = False                       # 這個窗出現過 `event_msg:error`
    turn_ai = False                          # 這個窗真的產出過助理內容
    # (v36-fam5 #2) `response_item` 的 payload.type 認不得的則數，與「content 元素型別漂掉、
    # 抽不出助理文字」的則數。兩者都是助理內容整批消失的入口，而容器那條哨兵只認 payload 不是
    # dict 的情形，接不到這兩種。
    n_unhandled_response_items = 0
    n_empty_assistant_items = 0
    # (v36-fam6 #2) 同一條路徑上另外兩格漂移：`role` 認不得、`content` 欄形狀不對。
    n_unknown_message_role = 0
    n_bad_message_content = 0
    # (v36-fam6 #3) content 被序列化成 JSON 字串（已還原，但仍是上游漂移，要出聲）。
    n_json_string_content = 0
    # (v36-fam5 #3) 整場收到幾則 prompt。⚠ **不可以**改用 `s.events` 裡 type=="user" 的則數
    # ——工具結果也是以 `user` 存的（見下方 `function_call_output` 那一支），一場有工具呼叫、
    # prompt 卻全丟時它不會是 0。獨立計數器才是「真的被當成 prompt 收下」的則數。
    n_prompts_total = 0

    for i, e in enumerate(raw):
        raw_payload = e.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        if not isinstance(raw_payload, dict):
            outer = str(e.get("type") or "")
            # (v36-fam4 #4) `response_item` 的容器漂了 → **助理內容整批靜默消失**：那一支是
            # 助理訊息、reasoning、工具呼叫的唯一入口，清成 `{}` 之後頁面會是「N 則提問、
            # 零則回答」，而收尾那條形狀無關的哨兵被 `n_ai_turns` 擋住（助理事件正好是 0）
            # → 完全無聲。下面那條哨兵只在容器「看起來像使用者發言」時才計數，接不到助理樣態。
            # ⚠ 這一格**刻意不看 role**：`role=user` 的 `response_item` 是注入的脈絡、不該當成
            #   prompt——但那是「算不算 prompt」的問題，與「容器形狀認不得」是兩回事。
            #   形狀認不得就是認不得，這裡只回報形狀。
            if outer == "response_item":
                n_drifted_response_items += 1
            if _codex_container_user_like(raw_payload):
                # `payload` **容器本身**漂了（list 包 dict，或整包被序列化成 JSON 字串）。清成 `{}`
                # 之後這一層以下每一個哨兵都失明：`ptype` 是 None → 進不了 user_message／
                # item_completed 任何一支，`_codex_event_user_like({}, None)` 也恆為 False
                # → 整則 prompt 靜默消失，所有計數都停在 0。哨兵要蓋滿每一層容器，少一層就是一個洞。
                if outer == "event_msg":
                    n_unhandled_user_events += 1
                elif outer != "response_item":
                    # ⚠ `response_item` 是**認得**的外層型別，而它的 role=user 本來就是注入的脈絡
                    # （AGENTS.md 全文之類），不是使用者打的字——歸到「最外層型別不認得」既指錯層
                    # 也會誤報。它那一層真正的漏收由上面那條與 `event_msg` 那條哨兵負責。
                    n_unhandled_user_outer += 1
        ptype = payload.get("type")
        ts = e.get("timestamp")
        dt = e.get("_dt")

        # ⚠⚠ **這個窗是系統自己起的嗎**（壓縮／失敗）——只認結構欄位，不讀訊息內容。
        # ⚠ 外層 `compacted` 的 payload 是 `{message, replacement_history}`、**沒有 `type`**，
        #   所以只能認外層那個名字。
        # ⚠ `error` 一併算進來的取捨：失敗的壓縮**只留下一個 `error`**（實測訊息是
        #   「Error running remote compact task…」），結構上和「一般回合失敗」分不開，
        #   而靠訊息文字去分是這份程式一貫拒絕的做法。代價是**「prompt 掉了、而且那一輪
        #   剛好也失敗」時這條哨兵不出聲**——那是很窄的一格，換到的是不再對正常資料吵。
        #   真正要擋的整批漂移會讓幾十個**沒有失敗**的窗一起變空，照樣看得見。
        if (e.get("type") == "compacted"
                or (e.get("type") == "event_msg" and ptype == "context_compacted")):
            turn_systemic = True
        # ⚠⚠ **`error` 不可以無條件豁免。** 第一版把任何 `error` 都當成「系統自己起的」，
        # 而跨模型 reviewer 實測示範了它的代價：`task_started` → prompt 事件**改名（沒收到）**
        # → 助理照常回答 → `error`，這個窗**真的漏收了一則 prompt**，哨兵卻完全靜默。
        # 分界線在**這個窗有沒有產出助理內容**：
        #   · 壓縮失敗那種只有 `task_started` → `error` → `task_complete`，**沒有助理內容**；
        #   · 使用者真的送出了什麼、模型也回了，然後才失敗的那種**有**。
        # ⇒ 只有前者豁免。這樣兩個 reviewer 的實測形狀同時成立，也不必去讀 error 的訊息文字。
        if e.get("type") == "event_msg" and ptype == "error":
            turn_error = True

        if e.get("type") == "session_meta":
            meta_id = (payload.get("id") or "").strip()
            if meta_id:
                s.session_id = meta_id
            if payload.get("originator") == "codex_exec" or payload.get("source") == "exec":
                s.exec_origin = True
            # `source` 多數是字串（exec／cli／vscode），子代理那種是 dict：
            # {"subagent": {"thread_spawn": {...}}}。只認結構、不認裡面的欄位名。
            subagent_thread = subagent_thread or isinstance(payload.get("source"), dict) and (
                "subagent" in payload["source"])
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
            if ptype == "task_started":
                if (turn_open and not turn_aborted and not turn_prompts
                        and not (turn_systemic or (turn_error and not turn_ai))):
                    n_empty_turns += 1           # 上一個窗開了卻一則都沒收到
                turn_open, turn_prompts, turn_aborted = True, 0, False
                turn_systemic = turn_error = turn_ai = False
                pending_user = None              # 新回合：上一則已不可能是「同一則」
                continue
            if ptype == "turn_aborted":
                turn_aborted = True              # 中止的回合沒有 prompt 是正常的
                continue
            user_text = None
            if ptype == "user_message":
                raw_msg = payload.get("message")
                # ⚠ (v36-fam3 F3) **非字串一律走與新格式同一組檢查**，兩側對稱。
                # 原本是 `str(payload.get("message") or "")`：`message` 從字串漂成 content block
                # 陣列時，`str()` 會把 Python 的 repr 整包當成 prompt 收下——實測收到的是
                # "[{'type': 'text', 'text': '…'}, {'type': 'image', …}]"，而三層哨兵全靜：
                # repr 非空 → 不觸發「取不出文字」；型別名還在 → 不觸發型別哨兵；
                # 不走 item 分支 → 不數 dropped block。圖片 block 無聲消失。
                # 那正是不變量①要擋的形狀，只是漂移點在舊格式這一側（新格式那條路徑早就有這兩層）。
                # (v36-fam6 #3) 先還原「被序列化成 JSON 字串的 content」，否則整包原文會被
                # 當成 prompt 收下（同上一段講的 repr 問題，只是漂移點再往內一層）。
                raw_msg, _jsond = _codex_normalize_content(raw_msg)
                if _jsond:
                    n_json_string_content += 1
                user_text = (raw_msg if isinstance(raw_msg, str)
                             else _codex_content_text(raw_msg, "text"))
                if not user_text.strip():
                    # 型別還在、文字取不出來＝承載文字的欄位換了位置（`message` 改名或搬層）。
                    # 不可當成空回合收下：空 content 的事件會被 group_turns 丟掉而完全看不見，
                    # 等於把漏收藏得更深。記成異常、由收尾哨兵出聲。
                    user_text = None
                    n_empty_user_items += 1
                    pending_user = None
                else:
                    if not isinstance(raw_msg, str):
                        # 同新格式：混合內容（文字＋圖片…）抽得到文字就不會觸發「取不出文字」，
                        # 但沒被吃掉的 block 一樣是被丟掉的內容，要單獨數。
                        n_dropped_user_blocks += _codex_content_unconsumed(raw_msg, "text")
                    if _codex_dual_shape(pending_user, user_text, "legacy"):
                        n_dual_shape += 1          # 反序（新格式先、舊格式後）也要數：形狀對稱
                    pending_user = (user_text, i, "legacy")
            elif ptype == "item_completed":
                raw_item = payload.get("item")
                item = raw_item if isinstance(raw_item, dict) else {}
                # 容器本身漂了（實測見過 list 包 dict；序列化成 JSON 字串的形態同樣涵蓋）：
                # 換成 {} 之後 item 層的哨兵全都看不到，而 payload 層不帶 role、
                # `_codex_event_user_like` 也接不到 → 靜默整則消失。
                drifted_user_item = (not isinstance(raw_item, dict)
                                     and _codex_container_user_like(raw_item))
                if drifted_user_item:
                    n_unhandled_user_items += 1
                itype = str(item.get("type") or "")
                if itype == "UserMessage":
                    # (v36-fam6 #3) 與舊格式對稱：先還原被序列化成 JSON 字串的 content。
                    # ⚠ 還原後要**寫回 `item`**，否則下面的 `_codex_content_unconsumed(item…)`
                    #   仍看到字串、回 0，沒被吃掉的 block（圖片…）又靜默消失一次。
                    _c, _jsond = _codex_normalize_content(item.get("content"))
                    if _jsond:
                        n_json_string_content += 1
                        item["content"] = _c
                    user_text = _codex_content_text(_c, "text")
                    if not user_text.strip():
                        # 同上：content 形狀與預期不符（element type 改名、純圖片/音訊…）。
                        user_text = None
                        n_empty_user_items += 1
                        pending_user = None
                    else:
                        # 混合內容（文字＋圖片…）：抽得到文字就不會觸發「取不出文字」那個哨兵，
                        # 但沒被吃掉的 block 一樣是被丟掉的內容，要單獨數。
                        # ⚠ 只在**抽得到文字**時才數：純圖片／音訊那格已經由上面的哨兵報過，
                        # 兩邊都數會讓同一則訊息出兩行警告，看起來像兩個獨立問題。
                        n_dropped_user_blocks += _codex_content_unconsumed(item.get("content"), "text")
                        if _codex_dual_shape(pending_user, user_text, "new"):
                            n_dual_shape += 1
                        pending_user = (user_text, i, "new")
                elif "user" in itype.lower() or str(item.get("role") or "").lower() == "user":
                    # 看得出是使用者訊息、卻不是我們認得的那個型別 → 上游可能又改名了
                    # （0.147 就是這樣整段消失的）。**型別名與 role 都要看**：若上游改成通用的
                    # item.type="Message" ＋ role="user"，只比對型別名會整個穿過去而毫無聲音。
                    n_unhandled_user_items += 1
                elif _codex_event_user_like(payload, ptype) and not drifted_user_item:
                    # item 這一層看不出東西（`item` 不是 dict、或 role/content 整組搬到 payload
                    # 這一層），但 payload 自己看得出是使用者發言。哨兵要**逐層**都有：只看 item
                    # 那一層的話，欄位往上搬一層就整段穿過去，又是「頁面看起來完整、prompt 一則
                    # 不剩」。已知的非 user item（AssistantMessage／Reasoning…）不帶 payload 層
                    # role，不會誤報。
                    n_unhandled_user_items += 1
            elif ptype not in _CODEX_HANDLED_EVENTS and _codex_event_user_like(payload, ptype):
                # 上一層的漂移：連 event_msg 的 payload.type 本身都換掉了。item 層那個哨兵
                # 接不到這種（根本進不了 item_completed 那一支），漏掉就又是「整場 prompt
                # 一則不剩、頁面看起來完整」——0.147 那次的失效樣態。
                n_unhandled_user_events += 1
            if user_text is not None:
                turn_prompts += 1
                n_prompts_total += 1         # (v36-fam5 #3) 收尾那條底線哨兵要用；與窗無關
                # Codex also emits response_item role=user, but those can include injected context
                # （實測 0.147 那些就是注入的 AGENTS.md 全文）。event_msg 這一則才是使用者可見的 turn。
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
                    "message": {"role": "user", "content": user_text},
                })
            elif ptype == "token_count":
                # usage 掛在該次呼叫的「第一筆」事件＝步驟起點（group_turns 據此切步，對齊 Claude
                # 的逐步語意）。呼叫沒有可呈現事件時退回上一個掛載點（該步徽章成為兩次呼叫合計）；
                # resume/重播重發的 token_count 會因「同掛載點＋同簽章」被去重——此語意經 173 個
                # 本機 rollout 對帳，與 Codex 自身 total_token_usage 累計吻合。
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage = info.get("last_token_usage")
                sig = _codex_usage_sig(usage)
                if sig is not None:
                    target = step_first_event or last_usage_target
                    mid = ((target or {}).get("message") or {}).get("id")
                    if mid and sig not in attached_usage.setdefault(mid, set()):
                        _attach_codex_usage(target, usage, current_model)
                        attached_usage[mid].add(sig)
                    if target is not None:
                        last_usage_target = target
                    step_first_event = None     # 呼叫結束；下一筆事件是新步驟起點
            continue

        if e.get("type") != "response_item":
            # **最外層**的漂移：連事件自己的 `type` 都換掉了（payload 仍是使用者發言）。
            # 內兩層的哨兵都進不來——它們掛在 `event_msg` 那一支底下。哨兵要蓋滿每一層，
            # 少一層就是一個洞，而每個洞的症狀都一樣：頁面看起來完整、prompt 一則不剩。
            # 實測本機 corpus 除了四種已處理的外層型別，另有 compacted／world_state／
            # inter_agent_communication_metadata，三者都不帶 user 樣態 → 現有資料零誤報。
            if (_codex_event_user_like(payload, ptype)
                    or "user" in str(e.get("type") or "").lower()):
                n_unhandled_user_outer += 1
            continue
        # 內容事件把「同一則的兩種表述」隔開：之後再出現同文字的 UserMessage 就是真的重打了。
        pending_user = None

        # (v36-fam5 #2) 型別改名／新型別：容器沒漂（payload 仍是 dict）、但 `payload.type` 認不得。
        # 歷史上真正發生過的漂移（0.147）就是改名，不是容器換型，而既有的容器哨兵只認後者。
        if str(ptype or "") not in _CODEX_HANDLED_RESPONSE_ITEMS:
            n_unhandled_response_items += 1

        if ptype == "message":
            role = payload.get("role")
            # (v36-fam6 #2) 兩格與型別改名同症狀、但既有哨兵都接不到的漂移：
            #   ① `role` 改名／缺欄 → 下一行直接 `continue`，助理內容整批消失、完全無聲。
            #   ② `content` 欄改名／型別不對 → `_codex_content_text` 取到 None 而回空字串，
            #      而 `n_empty_assistant_items` 的守門要求它是**非空 list**，於是那一格也不出聲。
            # 兩者在本機 516 份 rollout 上都是 0（role 三種、content 恆為非空 list）→ 零誤報。
            # ⚠ ②**不限於 assistant**：user／developer 的 content 漂掉同樣是上游換了形狀，
            #   而且它比 role 更早壞——先數再依 role 分流。
            if str(role or "") not in _CODEX_KNOWN_MESSAGE_ROLES:
                n_unknown_message_role += 1
            if not isinstance(payload.get("content"), list) or not payload["content"]:
                n_bad_message_content += 1
            if role != "assistant":
                continue
            text = _codex_content_text(payload.get("content"), "output_text")
            if not text.strip():
                # (v36-fam5 #2) content **元素**型別改名（`output_text` → 別的名字）：型別白名單
                # 接不到（`payload.type` 還是 `message`），這裡是唯一看得見的地方。
                # ⚠⚠ **判準是「這個元素還有沒有沒被讀到的東西」**，不是「list 空不空」，
                # 也不是「型別對不對」。三種形狀要同時處理好（前兩種都實測過）：
                #   ① `[{"type":"output_text","text":""}]` ＝ 那一則**真的沒有話講**
                #      ⇒ **不可以出聲**（同事 2026-09-01 的 rollout：三個檔各吵一次）。
                #   ② `[{"text":"   "}]` ＝ 沒有 `type`、只有空白文字。`_codex_content_text()`
                #      **明確支援這個形狀**（`not item.get("type") and item.get("text")` 那一支），
                #      它真的抽到了東西 ⇒ 也不可以說「抽不出文字」。
                #   ③ `[{"type":"output_text","text":"","content":"真正的文字"}]` ＝ 上游把內容
                #      **搬到別的欄位**。型別沒改、`text` 也還是字串 ⇒ 只看型別的判準會**吞掉它**，
                #      而那一則的文字整批消失。這種一定要出聲。
                # ⇒ 判準：**這個 block 除了「型別認得的空文字」之外還帶著別的東西嗎**。
                # ⚠ 全語料實測（本機 566 份 rollout ＋ 同事 4 份，12738 個 content block）：
                #   **欄位集合 100% 是 `{text,type}`**，空文字的那 3 個也沒有任何其他非空欄位
                #   ⇒ 「還帶著別的非空欄位」這一條在現有資料上**零誤報**。
                def _carries_unread(b):
                    if not isinstance(b, dict):
                        return True
                    # `_codex_content_text()` 收得下的兩種型別形狀之外 ⇒ 認不得
                    if b.get("type") not in ("output_text", None):
                        return True
                    if not isinstance(b.get("text"), str):
                        return True          # `text` 欄改名／型別不對
                    if b["text"].strip():
                        return True          # 有字卻沒被抽出來（型別對不上上面那支的 text_type）
                    # 空文字：只有「除了 type/text 以外還有**帶字的字串欄位**」時才算漂移（③）。
                    # ⚠⚠ **不可以看「任何非空值」**：`{"type":"output_text","text":"","index":0}`
                    # 這種結構性 metadata 會被誤判成「內容搬到別欄位」而誤報
                    # （`qimg-fix-codex` Medium#3）。承載文字的欄位一定是字串，
                    # 用型別把 metadata 擋在外面，比列舉欄位名穩。
                    return any(k not in ("type", "text") and isinstance(v, str) and v.strip()
                               for k, v in b.items())
                _c = payload.get("content")
                if isinstance(_c, list) and _c and any(_carries_unread(b) for b in _c):
                    n_empty_assistant_items += 1
                continue
            turn_ai = True          # 這個窗真的有助理內容（見上面 `error` 那段的分界線）
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
            if step_first_event is None:
                step_first_event = ev
        elif ptype == "reasoning":
            text = _codex_content_text(payload.get("summary"), "summary_text")
            if text.strip():
                ev = {
                    "type": "assistant",
                    "uuid": f"codex-reasoning-{i}",
                    "timestamp": ts, "_dt": dt, "_i": i,
                    "cwd": s.cwd, "gitBranch": s.branch, "version": s.version,
                    "sessionId": s.session_id,
                    "message": {"role": "assistant", "id": f"codex-reason-{i}", "model": current_model,
                                "content": [{"type": "thinking", "thinking": text}]},
                }
                turn_ai = True      # reasoning 也是助理內容（見 `error` 那段的分界線）
                s.events.append(ev)
                if step_first_event is None:
                    step_first_event = ev
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
            # ⚠⚠ **工具呼叫也算助理內容。** 只認 `message` 的話，
            # 「漏收 prompt → function_call → error」那個窗會被 `error` 錯誤豁免
            # （`qimg-fix-codex` Medium#5）。判準是「這個窗有沒有產出助理事件」，
            # 而不是「有沒有助理**文字**」。
            turn_ai = True
            s.events.append(ev)
            if step_first_event is None:
                step_first_event = ev
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

    # 格式漂移哨兵。Codex 的 user turn 記錄形狀已經改過（0.147 互動模式整段消失且無聲），
    # 失效樣態是「頁面看起來完整、只是一個 prompt 都沒有」——沒有哨兵就只能等人肉眼發現。
    # ⚠ 刻意**不用**「有 response_item role=user 卻沒有使用者回合」當訊號：那裡也躺著注入的脈絡
    # （<recommended_plugins>／<environment_context>／AGENTS.md 全文），所以它分不出「使用者根本
    # 沒打字」與「解析器跟不上」——實測 478 個本機 rollout 有 2 個純注入脈絡的 session 會被它誤報。
    # 改用「CLI 明說完成了一則 user 樣態的 item、我們卻沒收出東西」，語意上無法兩解。
    if n_unhandled_user_items:
        print(f"  ! {path.name}: {n_unhandled_user_items} 則 item_completed 的型別像使用者訊息卻不認得"
              f"——Codex 格式可能又改名了，這些回合不會出現在輸出裡", file=sys.stderr)
    if n_unhandled_user_events:
        print(f"  ! {path.name}: {n_unhandled_user_events} 則 event_msg 的型別像使用者發言卻不認得"
              f"——Codex 可能又換了事件形狀，這些回合不會出現在輸出裡", file=sys.stderr)
    if n_drifted_response_items:
        print(f"  ! {path.name}: {n_drifted_response_items} 則 response_item 的容器形狀不認得"
              f"（payload 不是物件）——助理內容、reasoning 與工具呼叫都走這一支，"
              f"整批消失時頁面仍會看起來完整", file=sys.stderr)
    if n_unhandled_response_items:
        print(f"  ! {path.name}: {n_unhandled_response_items} 則 response_item 的 payload.type 不認得"
              f"——容器沒漂但型別改名／上游加了新型別，助理內容、reasoning 與工具呼叫都走這一支，"
              f"整批消失時頁面仍會看起來完整", file=sys.stderr)
    if n_empty_assistant_items:
        print(f"  ! {path.name}: {n_empty_assistant_items} 則助理訊息有 content 卻抽不出文字"
              f"（承載文字的元素型別可能改名了）——那幾則不會出現在輸出裡", file=sys.stderr)
    if n_json_string_content:
        print(f"  ! {path.name}: {n_json_string_content} 則訊息的 content 是被序列化成 JSON 字串的"
              f"——已自動還原並照常收下，但那是上游換了形狀，值得回頭確認解析是否還正確",
              file=sys.stderr)
    if n_unknown_message_role:
        print(f"  ! {path.name}: {n_unknown_message_role} 則 response_item:message 的 role 不認得"
              f"——助理訊息是靠 role=='assistant' 認出來的，role 改名等於整批被吞掉，"
              f"頁面會變成「有提問、沒回答」", file=sys.stderr)
    if n_bad_message_content:
        print(f"  ! {path.name}: {n_bad_message_content} 則 response_item:message 的 content "
              f"不是非空清單（欄位改名或型別變了）——那幾則的內容抽不出來，不會出現在輸出裡",
              file=sys.stderr)
    if n_unhandled_user_outer:
        print(f"  ! {path.name}: {n_unhandled_user_outer} 則事件的最外層型別不認得、內容卻像"
              f"使用者發言——Codex 可能改了事件外殼，這些回合不會出現在輸出裡", file=sys.stderr)
    if n_dropped_user_blocks:
        print(f"  ! {path.name}: 使用者訊息裡有 {n_dropped_user_blocks} 個 block 沒被收進來"
              f"（混合內容的 prompt，例如文字＋圖片）——文字仍在，其餘內容不會出現在輸出裡",
              file=sys.stderr)
    if n_empty_user_items:
        print(f"  ! {path.name}: {n_empty_user_items} 則使用者訊息取不出文字"
              f"（承載文字的欄位形狀可能變了，或是純圖片/音訊的 prompt），該回合不會出現在輸出裡",
              file=sys.stderr)
    if n_dual_shape:
        print(f"  ! {path.name}: {n_dual_shape} 則 prompt 出現緊鄰的跨格式同文——可能是同一則的兩種"
              f"表述，也可能是使用者連送兩次；**兩則都已保留**，請確認顯示是否出現重複",
              file=sys.stderr)
    if (turn_open and not turn_aborted and not turn_prompts
            and not (turn_systemic or (turn_error and not turn_ai))):
        n_empty_turns += 1                       # 收尾：最後一個窗
    # 上面五個哨兵的判準同源：「型別名含 user，或自報 role=user」。上游若把型別改成不含 user 的
    # 名字（`Prompt`、`HumanTurn`…）又不帶 role，**五個會同時是 0**——而症狀正是 0.147 那次的
    # 樣態：頁面看起來完整、prompt 一則不剩、stderr 一片安靜。
    # 這一條改看**結果**而非形狀：CLI 自己標了每個回合的起點，逐窗看「這個窗收到幾則 prompt」。
    # ⚠ **逐窗**而不是整檔兩個總數相減：真實語料上有 session 一個回合連送多則（38 回合／47 則），
    #   相減之後那種 session 的漏收會全部沉在門檻底下、一則警告都不會出。逐窗才抓得到
    #   「一場中途才漂移」——前半收得到、後半整窗空。
    # ⚠ 三種本來就沒有 prompt 的情形要排除，否則會在正常資料上吵：
    #   ① 被中止的回合（`turn_aborted`）；② 子代理執行緒（沒有人打字）；
    #   ③ 完全沒有助理內容的空 session；④ **系統自己起的那一輪**（自動壓縮、
    #      壓縮失敗——`turn_systemic`，見迴圈裡那段）。
    # ⚠⚠ ④ 是 2026-09-01 才補的，而且**不是靠本機語料發現的**：本機 565 份 rollout、
    #   729 個回合窗裡**零個**空窗，所以「0 誤報」在這台機器上永遠成立。
    #   同事的機器上一個檔就吵了兩次，兩次都是完全正常的資料（教訓 44 的又一次應驗：
    #   **量到 0 講的是這份語料**）。
    # ⚠ 不可改用「`type == "user"` 的事件數」：工具結果也是以 `user` 存的（見上方
    #   `function_call_output` 那一支），一場有工具呼叫、prompt 卻全丟時它不會是 0。
    # ⚠ **範圍限制 SCOPE-PARTIAL-TURN-LOSS**：這一條只認「整窗零則」，窗內只要還收到一則就
    #   不出聲 → 一個窗裡**只丟掉部分 prompt** 時，連同上面五個形狀哨兵一共六個全靜。
    #   ⚠ (v36-fam5 #3) 下面那條底線哨兵**也接不到這一格**：它的判準是整場零則，窗內漏收時
    #   `n_prompts_total` 仍大於 0。哨兵從六個變七個，這個洞沒有變小。
    #   一個窗連送多則本來就合法（本機語料上是少數），所以不能把門檻從「零則」直接放寬——
    #   會在正常資料上吵。可靠的修法要「窗內宣告的則數 vs 收到的則數」相比，而那個欄位
    #   不確定存不存在。實測數字與回頭處理的條件見 planning/scope-limits.md。
    n_ai_turns = sum(1 for e in s.events if e.get("type") == "assistant")
    if n_empty_turns and n_ai_turns and not subagent_thread:
        print(f"  ! {path.name}: 有 {n_empty_turns} 個回合開始了卻沒收到任何使用者 prompt"
              f"——不論形狀漂成什麼樣，這都代表使用者那一側有東西沒解析出來", file=sys.stderr)
    # (v36-fam5 #3) **底線哨兵：不依賴任何事件名。** 上面那條雖然改看「結果」，卻仍要先有
    # `event_msg:task_started` 才數得出窗——上游若在同一版把 prompt 事件與 `task_started` 一起
    # 改名／拿掉，`turn_open` 永遠是 False，連同五個形狀哨兵**六個會一起歸零**，而那正是它們
    # 要擋的樣態。實測（真實 rollout 施加漂移）：prompt 改名但留著 `task_started` → 上面那條
    # 出聲；prompt 改名且拿掉 `task_started` → 同樣掉 4 則提問，stderr 一個字都沒有。
    # 這一條只問最終結果：有助理內容、卻整場零則 prompt。本機 516 份真實 rollout 實測 0 誤報。
    # ⚠ 子代理執行緒沒有人打字，本來就零則，要排除（同上面那條）。
    # ⚠ `not n_empty_turns`：上面那條已經出聲時就閉嘴——同一個根因出兩行警告會看起來像兩個
    #   獨立問題（本檔在 `n_dropped_user_blocks` 那格為同一理由做過同樣的取捨）。這一條的
    #   價值只在「上面那條數不出窗」的時候。
    if n_ai_turns and not n_prompts_total and not subagent_thread and not n_empty_turns:
        print(f"  ! {path.name}: 有助理內容卻整場零則使用者 prompt"
              f"——連回合窗都數不出來時只剩這一條，代表 prompt 那一側整批沒解析出來",
              file=sys.stderr)

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
        # ⚠ 指令列與背景任務通知**畫得出來，但不可以當標題**：那不是使用者在講的事。
        # 通知尤其危險——它是系統注入的，內文是 task-id 與檔案路徑，會變成一個看不懂的標題。
        if user_special_blocks(e):
            continue
        txt = clean_user_text(extract_user_text(e))
        txt = re.sub(r"<[^>]+>", "", txt)                # 去掉殘餘標籤
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            return txt[:90]
    return ""


def content_text(content):
    """`content`（字串**或 block 陣列**）取純文字：**只收 `type:"text"` 的 block**。

    ⚠⚠ 非文字的 block（圖片）**直接丟掉**，不可以像 `normalize_result()` 那樣
    `json.dumps` 出來——那會把一坨 base64 當成使用者說的話拿去比對與呈現。
    CLI 自己就是這樣取的（它內部同一支 helper：挑出 `type=="text"` 的 block、
    取 `text`、以換行接起來），user 訊息與 `queued_command` 附件共用那一套。

    ⚠ 這一支是從 `extract_user_text()` 抽出來的，因為**同一套規則有第二個呼叫點**
    （`synth_queued_user_events()` 的 `prompt`，見那支的註解）。各寫一份的話，
    「哪些 block 算使用者說的話」遲早會分岔。

    ⚠ 這一支**只負責文字**。排隊句裡貼的圖片由 `synth_queued_user_events()`
    另外從同一個 `prompt` 撈出來（`type:"image"` 的 block），不從這裡走。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(b.get("text") or "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def extract_user_text(ev):
    return content_text((ev.get("message") or {}).get("content"))


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


# =========================================================================
# 使用者實際送出、但過去被靜默丟掉或誤標的三種東西
# -------------------------------------------------------------------------
# 判準一律走**結構標籤／結構欄位**，不猜文字。設計、量測與取捨見
# `planning/user-turn-fidelity.md`；每一個數字都由
# `scripts/probe_user_turns.py inventory` 現產，**不要照抄文件裡的數字**。
#
# ⚠ 這三種都是**回合會變多**的改動 ⇒ 序號錨點 `t{n}` 會位移（那本來就不耐久，
#   `durable_anchor()` 的說明就是為此而寫）。耐久錨點 `k…` 的處理見 `analyze()`：
#   ⚠⚠ **只有指令列不拿耐久錨點；自成一列的通知照拿**（`_no_anchor` 只認 `_command`）。
#   通知列在 v55 之前就是普通 user 回合，**本來就有錨點**，不發＝破壞既有書籤；
#   指令列是新長出來的，不發才安全。既有錨點集合逐字不變的機械保證來自這個不對稱。
#   守它的是 `tests/test_smoke.py::test_user_turn_fidelity`（兩個方向各一格斷言）
#   與 `scripts/probe_user_turns.py anchors <開工前的 commit>`。
# =========================================================================
# ⚠⚠ 終端控制碼不是只有 CSI。第一版只認 `ESC [ … 字母`，於是
# `ESC ] 0 ; 標題 BEL`（OSC，`/status` 這類會用來設視窗標題）、`\x00`、`\x08`
# 全部原樣寫進 HTML。而守它的斷言（`"\x1b" not in html`）之所以綠，
# 是因為**素材只放了 `\x1b[1m`**——「素材涵蓋到哪裡就是驗證能力的上限」的活例。
# ⚠⚠ **第二版又漏一層**：OSC 以外還有四種「控制字串」——DCS（`ESC P`）、SOS（`ESC X`）、
# PM（`ESC ^`）、APC（`ESC _`），它們同樣是「引導字元 … 終止字元」中間夾任意 payload。
# 只認 OSC 的話，`ESC P …payload… ESC \` 會被最後那條「兩字元跳脫序列」吃掉 `ESC P`，
# **payload 原封不動留在頁面上**（`utf-fix-codex` 實測 `payload_left=True`）。
# 每一種都還有 C1 單位元組形式（CSI=\x9b、OSC=\x9d、DCS=\x90、SOS=\x98、PM=\x9e、APC=\x9f）。
# ⚠ 未終止的控制字串一路吃到字串結尾（`\Z`）：留著 payload 比多吃一段更糟——
#   這是終端輸出，不是使用者的散文。
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"           # CSI：ESC [ … 終止字元
    r"|\x9b[0-9;?]*[ -/]*[@-~]"            # CSI 的 C1 單位元組形式
    r"|(?:\x1b[\]P^_X]|[\x9d\x90\x9e\x9f\x98])"   # 控制字串引導：OSC/DCS/PM/APC/SOS
    r"[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|\Z)"    # …連 payload 帶終止字元一起吃掉
    r"|\x1b[@-Z\\-_]")                     # 其餘兩字元跳脫序列
# 剝完控制碼還要濾掉裸的控制字元（NUL/BS/VT/FF 等）。⚠ 保留 \t \n \r。
# ⚠⚠ **範圍要含 C1（`\x80`–`\x9f`），不是只到 `\x7f`。** 第一版漏掉 C1，實測
# `remaining_c1=['0x80', '0x91', '0x99']` 原樣進頁面（`utf-fix-codex-r2` 驗收）。
# ⚠ 守它的斷言要放**裸的 C1** 當素材——舊素材只有 OSC／NUL／BS，
#   而突變 ⑥b 是把整條規則關掉，抓不到「只漏幾個 C1」這種半修（教訓 55）。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# ⚠ 拼接不是裝飾：`tests/test_smoke.py` 有一格在掃「產品碼裡不該出現的字面」，
#   而本模組自己就是被掃的對象之一。要找的東西不可以出現在找它的人身上（教訓 15）。
_NOTIFY_TAG = "task-" + "notification"
# 成對的通知整段（含開閉標籤）。⚠ 用它而不是 substring，理由見 `parse_notify()`。
_NOTIFY_SPAN_RE = re.compile(r"<%s>.*?</%s>" % (_NOTIFY_TAG, _NOTIFY_TAG), re.DOTALL)

# CLI 在沒有輸出時會寫這個字面，不是真的結果文字。
_CMD_NO_OUTPUT = "(no content)"


def _strip_ansi(t):
    """終端控制碼：`/model`、`/output-style` 的結果帶 `ESC[1m…`，原樣畫出來是亂碼。"""
    return _CTRL_RE.sub("", _ANSI_RE.sub("", str(t or "")))


def _tag_text(txt, tag):
    """`<tag>…</tag>` 的內容；**沒有這個標籤才回 None**——空字串代表「有，但是空的」。

    ⚠ 兩者一定要分得開：`<command-args></command-args>` 是「這個指令沒有參數」，
    而沒有 `<command-args>` 是「這個版本的 CLI 根本不寫這一段」。
    """
    m = re.search(r"<%s>(.*?)</%s>" % (re.escape(tag), re.escape(tag)), txt or "", re.DOTALL)
    return m.group(1) if m else None


def parse_notify(txt):
    """背景任務通知 → `_notify` 區塊；不是就回 None。

    這是**系統以 user 身分注入**的事件（子代理／背景指令／Monitor 回報），不是使用者打的字。
    過去它照使用者發言畫出來，於是頁面上出現一坨看不懂的 XML。

    全語料實測：命中的那些**沒有一則混雜其他文字** ⇒ 命中就代表整則都是通知，
    可以整則換成一列摘要。兩種形狀：帶 `<status>` 的（背景指令／子代理完成）
    與帶 `<event>` 的（Monitor 事件，沒有 status）。

    ⚠⚠ **「實測 0 則混雜」不可以寫成 substring 判準。** 第一版只問
    「文字裡有沒有 `<task-notification>` 這幾個字」，於是**使用者自己打的字只要提到
    這個標籤，整則就變成一列通知**——而通知在封存分類器裡**不算對話**
    ⇒ 一場「下過任一指令 ＋ 問了一句關於這個標籤的話」的 session 會被判成
    `command_only` **搬離 `~/.claude/projects/`**（`utf-fix-codex` High#2 實測）。
    「量到 0」講的是這份語料，不是這個判準的定義域。

    現在的判準是**整則都是通知**，機械定義：
    **把通知那一段整段拿掉之後，剩下的東西 `clean_user_text()` 完是空的。**

    ⚠ 為什麼是「拿掉之後再 clean」而不是「檢查前後綴是空的」：通知常常被包在
    `<system-reminder>` 裡，直接切前後綴會拿到**沒有配對的**半個包裝標籤，
    `_WRAP_RE` 剝不掉它 ⇒ 真正的通知會被判成混雜。先整段拿掉，包裝就配對回來了。
    ⚠ 也**要求閉合標籤**（`_NOTIFY_SPAN_RE` 找的是成對的）：只有開標籤的一律當使用者發言。
    兩個方向都刻意選保守的那一邊——判錯的下游是**搬走使用者的檔案**。
    """
    s = str(txt or "")
    if not _NOTIFY_SPAN_RE.search(s):
        return None
    if clean_user_text(_NOTIFY_SPAN_RE.sub("", s)).strip():
        return None                       # 混著使用者自己的字 ⇒ 那是真的發言
    txt = s
    return {"type": "_notify",
            "summary": (_tag_text(txt, "summary") or "").strip(),
            "status": (_tag_text(txt, "status") or "").strip(),
            "event": (_tag_text(txt, "event") or "").strip(),
            "raw": str(txt)}


def parse_command(txt):
    """斜線指令／`!` bash 包裝 → `_command` 區塊；沒有就回 None。

    使用者下的指令**本來就是他送出的東西**（而且 `/model`、`/effort` 這種會改提示前綴、
    直接造成快取失效），過去卻因為 `_WRAP_RE` 把整段剝光、`is_noise_user()` 判為雜訊而
    整則不畫——看紀錄的人無從得知自己下過指令。

    四種形狀（全語料實測，各自的則數見探針）：

    | 形狀 | 標籤 | 意思 |
    |---|---|---|
    | ① | `<command-name>` [＋`<command-args>`] | 斜線指令本體 |
    | ② | 只有 `<local-command-stdout>` | ①的結果，**是同一時刻的下一則事件**（合併規則見 `group_turns`）|
    | ③ | `<bash-input>` | 使用者用 `!` 直接跑的指令 |
    | ④ | `<bash-stdout>`＋`<bash-stderr>` | ③的輸出，同樣是下一則 |

    ⚠ **判斷順序不可對調**：①的事件同時可能帶 `<local-command-stdout>`（指令與結果寫在同一則），
    先比 `out` 會把指令名丟掉。
    """
    if not txt:
        return None
    bash_in = _tag_text(txt, "bash-input")
    if bash_in is not None:
        return {"type": "_command", "name": "!", "args": bash_in.strip(), "out": ""}
    name = _tag_text(txt, "command-name")
    if name is not None:
        return {"type": "_command", "name": name.strip(),
                "args": (_tag_text(txt, "command-args") or "").strip(),
                "out": _clean_cmd_out(_tag_text(txt, "local-command-stdout"))}
    bash_out, bash_err = _tag_text(txt, "bash-stdout"), _tag_text(txt, "bash-stderr")
    if bash_out is not None or bash_err is not None:
        joined = "\n".join(x.strip() for x in (bash_out, bash_err) if x and x.strip())
        return {"type": "_command", "name": "", "args": "", "out": _clean_cmd_out(joined)}
    out = _tag_text(txt, "local-command-stdout")
    if out is not None:
        return {"type": "_command", "name": "", "args": "", "out": _clean_cmd_out(out)}
    return None


def _clean_cmd_out(t):
    if t is None:
        return ""
    t = _strip_ansi(t).strip()
    return "" if t == _CMD_NO_OUTPUT else t


def user_special_blocks(ev):
    """該 user 事件裡「不是對話、但確實發生過」的區塊：通知列或指令列。

    ⚠ **通知先判**：通知整則就是通知，不可能同時是指令。
    ⚠ 指令**不要求整則清乾淨後是空的**——實測語料裡 1170 則全是「只有包裝」，
      但斜線指令帶著注入內容（`/<skill>` 那種）在結構上就會是「包裝＋真文字」，
      那時兩個區塊都要有。以「有沒有那個標籤」為準，不以「剩下還有沒有字」為準。
    """
    txt = extract_user_text(ev)
    if not (txt or "").strip():
        return []
    n = parse_notify(txt)
    if n:
        return [n]
    c = parse_command(txt)
    if c:
        # ⚠ **這個指令是從哪一種存法來的。** 呈現層不用它，`probe_user_turns.py render`
        # 用它當身分：兩種存法在同一秒出現同一個指令名時，只比「名字＋秒」的話
        # **user 側畫出來的那一個會被 SYSCMD 那格冒領**，於是 system 側整批沒畫也照樣全綠
        # （`utf-fix-codex-r2` 實測 `[✓] CMDWRAP 1/1` ＋ `[✓] SYSCMD 1/1`，而產品只畫了一個）。
        c["src"] = "system" if ev.get("_synth") == "local_command" else "user"
    # ⚠ 三格都空的指令區塊要在這裡就丟掉（例：`<local-command-stdout></local-command-stdout>`
    # ——`/rc` 那種沒有輸出的指令會另外寫一則空的結果事件）。留著的話 `render_command_html()`
    # 會回空字串、那一輪畫不出東西，**但它已經佔掉一個回合**了：`t{n}` 序號往後推、
    # `n_turns` 多算一則。**判空的條件必須和渲染端同一個**，兩邊各寫一份就會分岔。
    if c and not (c.get("name") or c.get("args") or c.get("out")):
        return []
    return [c] if c else []


def is_noise_user(ev):
    """整則只剩指令 / 系統提醒包裝（清乾淨後沒內容）→ 視為雜訊，不呈現。

    ⚠ **這一支的語意刻意沒變**：它現在的唯一用途是「這一則能不能當 session 標題」
    （`first_user_text`）。呈現與否改由 `group_turns` 先問 `user_special_blocks()`
    決定——把兩件事綁在同一個判準上，就無法「畫出來但不拿來當標題」。
    """
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


# ⚠⚠ **這一份清單只有一個用途：封存分類器的保守否決**（`_has_unknown_content_block`）。
# 它列的是「這支程式**認得**的 content block 型別」——`block_is_renderable()` 逐條處理過的
# 那些，加上 `tool_result`（刻意不算對話）與內部合成的 `_` 開頭區塊。
# ⚠ 這**不是**第二套渲染判準，它只能把答案推向 `conversation`，永遠不能推向 `command_only`。
# ⚠ 改 `block_is_renderable()` 時要一起改這裡，守它的是
#   `tests/test_smoke.py::test_archive_unknown_block_is_conversation`。
KNOWN_BLOCK_TYPES = frozenset({
    "text", "thinking", "redacted_thinking", "tool_use", "image", "tool_result",
    "_command", "_notify", "_step", "_interject",
})


def _has_unknown_content_block(s) -> bool:
    """這一場裡有沒有**這支程式不認得的** content block（直接讀原始事件）。

    ⚠⚠ **為什麼不能只看 `s.main_groups`**：`group_turns()` 會把不認得的區塊**先丟掉**，
    於是「一場有新型內容 ＋ 下過一個指令」的 session 在渲染管線的產物裡看起來
    **就只有一列指令** ⇒ 分類成 `command_only` ⇒ **被搬離 `~/.claude/projects/`**
    （`utf-fix-codex` High#1 實測：塞一個 `{"type":"document"}` 的 user 區塊，
    `kind=command_only`、`source_exists=False`——檔案真的被搬走了）。

    ⇒ **破壞性的分類不可以對未知 schema fail-open。** CLI 之後新增 audio / document /
    server block 這類型別時，這一格會讓那些 session 一律留在原地，
    代價只是「少搬幾場」——而反方向的代價是使用者永久失去一場對話的 resume 入口。
    """
    for ev in getattr(s, "events", []) or []:
        if ev.get("type") not in ("user", "assistant"):
            continue
        content = (ev.get("message") or {}).get("content")
        # ⚠⚠ **只有 `str` 可以豁免。** 第一版寫「不是 list 就是純字串，認得」——
        # 那句話不成立：`content` 也可能是**單一個 dict**、或是別的形狀。
        # 實測（`utf-fix-codex-r2` 驗收）：`single_dict_block`、`scalar_inside_list`
        # 都讓 `has_unknown=False`、分類成 `command_only` ⇒ **檔案照樣被搬走**。
        # 保守方向只有一個：**看不懂的一律當對話。**
        if isinstance(content, str):
            continue
        if content is None or content == [] or content == {}:
            continue                      # 真的沒有內容，不是「看不懂」
        if not isinstance(content, list):
            return True                   # 單一 dict、字串以外的純量、其他形狀
        for b in content:
            if isinstance(b, dict):
                if b.get("type") not in KNOWN_BLOCK_TYPES:
                    return True
            elif b not in (None, "", [], {}):
                return True               # list 裡混了非 dict 的非空元素
    return False


def block_is_renderable(b, role):
    t = b.get("type")
    if t == "text":
        text = str(b.get("text") or "")
        return bool(clean_user_text(text) if role == "user" else text.strip())
    if t == "thinking":
        return bool(str(b.get("thinking") or b.get("text") or "").strip())
    if t in ("redacted_thinking", "tool_use", "image"):
        return True
    if t == "_notify":
        # ⚠⚠ **通知列要保住區塊錨點**：v55 之前它是普通 user 回合、內容是一個 `text`
        # 區塊，所以**本來就有 `k…-b1`**，使用者標得到書籤。改成 `_notify` 之後若不算
        # 可標記區塊，那個錨點就**消失**了（探針補上區塊錨點後立刻抓到）。
        # ⚠ 這裡不會推移別人：通知列整輪只有這一個區塊（`rest` 為空才會是 metarow）。
        # ⚠ 和下面 `_interject` 的決定相反，理由也相反——那個是**新長出來的**
        #   （不發才安全），這個是**本來就有的**（不發就是破壞）。
        # ⚠⚠ **但只有「自成一輪」的那種算**。被包在 `<system-reminder>` 裡的通知
        #   在 v54 是雜訊、整則被丟掉、**根本沒有錨點**；它們現在掛在回合內部
        #   （`inline`），若也佔序號就會把同一步後面的區塊全部往後推。
        return not b.get("inline")
    if t == "_interject":
        # ⚠⚠ **插話不算可標記區塊——它不可以佔 `-b<n>` 的序號。**
        # 第一版讓它算（理由是「插話是對話內容，該標得到書籤」），結果是：
        # `_interject` 被插進 `cur["blocks"]` **中間**，於是**同一步之內它後面每一個區塊
        # 的序號 +1** ⇒ 既有的區塊書籤 `-b3` 不會失效、不會退化，
        # **它會指到別的內容**——那是最壞的失敗方向（`utf-fam` High#3 實測）。
        # ⚠ 這一版宣告的相容性保證原本只涵蓋**回合**錨點 `k…`；區塊錨點
        #   `k…-s<ep>-b<n>` 同樣是使用者按 ☆ 存得下來的身分，而且**數量多一個數量級**
        #   （全語料 116758 個區塊 vs 9047 個回合）。
        # ⇒ 和指令列／通知列同一個決定：**新長出來的東西一律不進序號**，
        #   那是「既有錨點逐字不變」唯一能機械保證的做法。
        # 代價：插話本身標不到書籤（範圍限制 SCOPE-INTERJECT-NO-BLOCK-ANCHOR，
        #      詳見 planning/scope-limits.md）。它仍然畫得出來、仍然進 MD、仍然搜得到。
        return False
    return False


def _cause_key(t, n):
    """成因 join 的鍵：時間 `t` 那一秒的第 `n` 次呼叫（n 由兩端各自依序數，順序相同）。

    整數秒單獨當步驟身分證會**撞號**：同一秒內的兩次呼叫共用一把鍵，後者的成因蓋掉前者。
    自從首步也採用 API 自報成因後，撞號的後果從「推論成因貼錯」升級成「**沒有 diagnostics
    的那一步被標成 API 自報**」＝把推論講成實據，違反最高不變量①。
    這裡刻意只在**真的撞號時**才退化成 `(epoch, n)`：n==0 仍用裸 epoch，所以絕大多數
    （沒撞秒的）資料行為與先前完全相同，不動 d88f57f 才修好的那段 join。
    ⚠ 產生鍵與查鍵兩端**必須共用本函式**，否則一漂移就是整批成因錯位。"""
    return t if n == 0 else (t, n)


def _epoch(e):
    """事件所屬的 epoch 秒（int）或 None——供 `_step` 區塊的 `t`（顯示用時刻與「距上一步」）。

    ⚠ **用 `math.floor` 不用 `int()`**：`int()` 對負值是往零截（`-0.5` → `0`），會把 1969 年的
    時刻顯示成 1970 年——那就不是「那一步自己的時刻」了。floor 取的才是「包含這個時刻的那一秒」。
    `t >= 0` 時兩者完全相同，真實資料的行為不變。

    ⚠ 本函式**不是成因 join 的鍵**：join 走的是 `message.id`（見 `analyze` 裡的 `key_by_mid`），
    鍵由 `_cause_key` 從 `cache_steps` 算。兩邊不共用本函式，所以這裡取 floor 不會造成錯位。"""
    dt = e.get("_dt")
    return math.floor(dt.timestamp()) if dt else None


def _epoch_ms(e):
    """事件所屬的 epoch **毫秒**（int）或 None——只給區塊層級錨點的 `-s` 段用。

    ⚠⚠ **為什麼要另開一欄，而不是把 `_epoch()` 改成毫秒**：`_step` 的 `t` 是顯示用時刻與
    「距上一步」的來源，而且**同一個整數秒還是成因 join 那條線的語彙**（`_cause_key`）。
    改它的解析度會同時動到那三處，而區塊錨點只需要「同一輪內能不能分辨兩步」。
    兩個值各自都對，語意不同，所以分開存。

    ⚠⚠ **為什麼區塊錨點不能用秒**（`bookmarks-p4-fam` High，實測）：同一輪裡兩次 API 呼叫
    落在同一秒時，兩步底下的區塊會拿到**完全相同**的錨點 ⇒ 頁面 id 重複、
    `getElementById` 取第一個 ⇒ 在第二塊按 ☆ 存下來的是第一塊的摘要，
    而跳回去時 `openSub()` 回「精確命中」、`.hl` 框在第一塊、沒有橫幅、還改寫網址列。
    `durable_anchor()` 的註解隔壁就寫著「只截到整秒，撞號會從 1 變 31」——同一份道理。

    ⚠ **毫秒仍然不是保證**（回合層實測 9047 輪撞 1 次），所以 `render_turn_html` 另有一道
    底線：同一輪內 `tms` 重複的那些步，整步的區塊一律不給錨點。
    """
    dt = e.get("_dt")
    return math.floor(dt.timestamp() * 1000) if dt else None


def _new_turn_usage():
    return {"ids": set(), "input": 0, "cache_create": 0, "cache_read": 0,
            "output": 0, "ctx_max": 0, "ctx_win": None, "cost": 0.0, "unpriced": False,
            "miss": [], "miss_tok": 0, "miss_usd": 0.0, "miss_cold": 0,
            "miss_usd_partial": False}   # 有步驟是未知模型、金額估不出 → 顯示 +?（同 ②-b 的 usd_partial）


def _acc_turn_usage(acc, msg):
    """把單筆 assistant 訊息的 usage 累加到該回合（依 message.id 去重）。
    另收集該回合各步的 API 自報失效成因（含「命中率仍高但有一段被重寫」的部分失效）。"""
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
    ctx = i + c1 + c2
    if ctx > acc["ctx_max"]:            # ctx_max 換人時，連同**那一步自己的** context 視窗一起記下來：
        acc["ctx_max"] = ctx            # 同一顯示回合可能併了不同模型的呼叫（opus 1M → haiku 200k），
        acc["ctx_win"] = context_window(msg.get("model") or "")   # 拿別步的視窗去除會算出錯的佔比
    c5, c1h = _ephemeral_split(u)
    c = call_cost(msg.get("model"), i, c1, c2, o, c5, c1h)
    if c is None:
        acc["unpriced"] = True
    else:
        acc["cost"] += c
    reason, mtok = _miss_reason(msg)
    if reason:
        acc["miss"].append(reason)
        acc["miss_tok"] += mtok
        if c2 * 100 < CACHE_COLD_PCT * (i + c1 + c2):     # 這步整段沒命中（非只掉一段）
            acc["miss_cold"] += 1
        w = rewrite_waste_usd(msg.get("model"), mtok, c5, c1h, wrote=c1)
        if w:
            acc["miss_usd"] += w
        elif mtok > 0 and c1 > 0:
            acc["miss_usd_partial"] = True     # 未知模型：別靜默貢獻 0，會顯示成偏低的「多付」


def _step_usage(msg, ev=None):
    """單一步驟（＝一次 API 呼叫；Claude 為一個 message.id，Codex 為帶 usage 的步驟起點事件）的
    usage，供回合內 per-step 徽章使用。
    取顯示需要的欄位：input（全新輸入）、cache_create（快取寫入）、cache_read（命中分子）、
    total_in（脈絡＝命中分母/ctx）、output（產出）；後三者為可勾選的 token 細分徽章所用。
    另帶三個「多給一點資訊」的可勾選欄位：miss（API 自報的失效成因與重寫量/估算多花的錢）、
    win（該步脈絡佔 context 視窗的比例）、effort（該次呼叫的推理強度，取自事件層 `effort`）。"""
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
    reason, mtok = _miss_reason(msg)
    c5, c1h = _ephemeral_split(u)
    model = msg.get("model") or ""
    w = rewrite_waste_usd(model, mtok, c5, c1h, wrote=c1)
    return {"input": i, "cache_create": c1, "cache_read": c2, "total_in": total_in, "output": o,
            "miss": reason, "miss_tok": mtok, "miss_usd": w,
            # 未知模型：有重寫量卻算不出錢 → 徽章要標 ?，靜默省略會被讀成「沒多付」。
            # 條件與 _acc_turn_usage／session 表頭／②-b 三處字面一致（w is None 也可能是被 wrote
            # 上限壓成 0＝真的沒多付，那種不該標 ?）。
            "miss_usd_partial": w is None and mtok > 0 and c1 > 0,
            "win": context_window(model), "model": model,
            "effort": str((ev or {}).get("effort") or "")}


def synth_queued_user_events(events, name=""):
    """把**排隊送出**的使用者提示詞補成可呈現的 user 事件，回傳新事件的 list。

    ⚠⚠ **這一段補的不是格式，是一整句話。** 助手還在跑工具時打的字會走佇列，
    而那條路徑**在檔案裡完全沒有 `type:user` 事件**——只有 `queue-operation`
    與 `attachment.queued_command`。過去檢視器把兩者都當記帳事件濾掉，於是
    **答案在、問題不在**：頁面上會出現一段沒有任何人問過的回應。
    全語料實測 127 則（69 個檔）是這種情形。

    三個判準全部是結構欄位，不猜文字：

    1. `attachment.queued_command` 的 `origin.kind == "human"` ⇒ 人打的。
       系統注入的背景任務通知走同一個附件型別，靠這一欄分開（它們沒有 `origin`）。
    2. 要有一個 `queue-operation` 且 `operation == "remove"`、`content` 對得上該 prompt
       ⇒ 它**真的從佇列被取走送進模型**。
       ⚠⚠ **`popAll` 是撤回，一律不算。** 使用者常常打到一半改主意、清掉重打
       （實測 14 次），照 `enqueue` 畫就會多出一句他撤回過的幽靈重複句。
    3. 已經有一則**真的 user 事件**帶同樣文字的就不補（實測 1 次：撤回之後又重送）。

    時刻取 `remove` 的——那是它**真正被讀到**的一刻，也是 Will 2026-08-25 的裁決。
    使用者按下 Enter 的時刻另外存進 `_queued_at`，由呈現層放進游標提示，
    **不讓時間軸說謊，也不在版面上多一塊噪音**。
    """
    # ⚠⚠ **逐次（occurrence）配對，不是整場一個 set／dict。**
    # 舊版 `human` 是 `prompt -> 第一次的時刻`、`already` 是整場的文字集合，於是：
    #   ① 同一句排隊送出兩次（「繼續」這種），第二次的**按 Enter 時刻**是錯的；
    #   ② 整場任何一則真 user 事件曾出現同一句，**所有**同文字的排隊句都不補
    #      ⇒「先正常送一次、後來排隊再送一次」的第二次整句消失
    #      （`utf-fix-codex` Medium 實測 `QUEUED_REPEAT synth=0`）。
    # 真實語料量測（2026-08-25，594 個檔／133 個 remove）：同檔同句 remove **全部只有 1 次**、
    # 132 個沒有對應的真 user 事件、**1 個有而且落在 remove 之後**、sidechain 裡 0 個。
    # ⇒ 重複那一格目前語料命中 0，但結構上會發生（教訓 44），照修；
    #   而「真 user 事件在 remove **之後**」正是那 1 個的形狀 ⇒ 消耗條件用它。
    # (來源檔, 文字) -> [(按下 Enter 的時刻, 貼上的圖片 blocks)…]（同一句可排多次，順序保留）
    human = {}
    n_bad_prompt = 0            # `prompt` 形狀認不得的則數（見下方哨兵）
    for e in events:
        if e.get("type") != "attachment":
            continue
        a = e.get("attachment") or {}
        if a.get("type") != "queued_command":
            continue
        if ((a.get("origin") or {}).get("kind")) != "human":
            continue
        # ⚠⚠ **`prompt` 不一定是字串。** 排隊送出時**貼了圖片**的話，CLI 會把它改寫成
        # block 陣列：`[{"type":"text","text":<打的字>}, {"type":"image",…}]`
        # （CLI 只在 `pastedContents` 真的產得出圖片 block 時才走那條，所以本機語料
        # 588 則 `queued_command` 全是字串、這一格一則都沒驗到——**量到 0 講的是
        # 這份語料，不是這個欄位的形狀**，教訓 44）。
        # 舊寫法 `(a.get("prompt") or "").strip()` 遇到 list 直接 `AttributeError`，
        # 而 `analyze()` 那一層**沒有接** ⇒ 整支工具當場崩掉、**一個頁面都產不出來**
        # （2026-09-01 同事回報：321 個 session 檔全滅）。
        p = content_text(a.get("prompt")).strip()
        # ⚠⚠ **圖片要一起帶走。** `prompt` 是 block 陣列時，除了文字還有使用者
        # **貼上的圖片**（`type:"image"`，base64 就在裡面）。只取文字的話，那些圖在
        # 頁面上等於不存在——而它們是使用者真的送出去的東西。
        imgs = [b for b in a["prompt"] if isinstance(b, dict) and b.get("type") == "image"] \
            if isinstance(a.get("prompt"), list) else []
        # ⚠⚠ **崩潰不可以被換成靜默。** 修掉 `AttributeError` 之後，`prompt` 若漂成
        # 我們認不得的形狀（dict、數字、list of str…），`content_text()` 回 `""`、`imgs` 回 `[]`
        # ⇒ 下面 `if p or imgs:` 兩邊都假 ⇒ 這一則排隊句**連進都進不來**，頁面上又變回
        # 「答案在、問題不在」——而那正是這支函式存在的唯一理由。
        # ⚠ Codex 那一側有 13 條這種漂移哨兵，Claude 這一側先前一條都沒有。
        # 判準只認結構：`prompt` 不是 str 也不是 list，或 block 陣列裡出現既不是 `text`
        # 也不是 `image` 的元素。
        _pr = a.get("prompt")
        if _pr is not None and not isinstance(_pr, (str, list)):
            n_bad_prompt += 1
        # ⚠ `type` 對得上還不夠：`[{"type":"text","content":"lost"}]`（`text` 欄搬走了）
        #   在第一版是「認得的形狀」⇒ 取不出文字、又不出聲 ⇒ 整則靜默消失
        #   （`qimg-fix-codex` Medium#4 實測 `synth=0, warned=False`）。
        elif isinstance(_pr, list) and any(
                not (isinstance(b, dict)
                     and (b.get("type") == "image"
                          or (b.get("type") == "text" and isinstance(b.get("text"), str))))
                for b in _pr):
            n_bad_prompt += 1
        # ⚠ **`or imgs`**：只貼了圖、一個字都沒打的那一則，文字是空字串。
        #   沒有這一半的話它連進都進不來，整則靜靜消失（那正是這個功能要修的失效模式）。
        if p or imgs:
            # ⚠⚠ **鍵要帶來源檔。** 只用 prompt 的話，附件在子代理轉錄檔、`remove`
            # 在主檔時會**跨來源配對**，於是主檔憑空多出一句話
            # （`utf-fix-codex-r2` 實測 `QUEUED cross_source: count=1 src=['main.jsonl']`）。
            human.setdefault((e.get("_srcf") or "", p), []).append(
                (a.get("timestamp") or e.get("timestamp"), imgs))
    if n_bad_prompt:
        print(f"  ! {name or '(未命名來源)'}: {n_bad_prompt} 則排隊提示詞的 prompt 形狀認不得"
              f"（不是字串也不是 text/image 的 block 陣列）——那幾則不會出現在輸出裡",
              file=sys.stderr)
    if not human:
        return []
    # ⚠⚠ **比對前要先 `clean_user_text()`。** CLI 常在使用者訊息後面接
    # `<system-reminder>` 之類的注入區塊 ⇒ 原文與佇列裡的 `content` **不相等** ⇒
    # 同一句話會同時以 `_interject` 和真 user 回合**各畫一次**。
    already = []    # [(來源檔, 檔內行序, 清乾淨的文字)]，逐則保留、逐次消耗
    for e in events:
        if e.get("type") == "user" and not e.get("isMeta"):
            t = clean_user_text(extract_user_text(e) or "").strip()
            if t:
                already.append([e.get("_srcf") or "", e.get("_i", 0), t, False])
    used = {}       # prompt -> 已經用掉幾個「按 Enter 的時刻」
    out = []
    for e in events:
        if e.get("type") != "queue-operation" or e.get("operation") != "remove":
            continue
        raw_c = e.get("content")
        c = raw_c.strip() if isinstance(raw_c, str) else ""
        hkey = (e.get("_srcf") or "", c)
        # ⚠⚠ **`content` 整欄不存在 ≠ `content` 是空字串**，這兩件事一定要分開。
        # CLI 寫的是 `typeof value === "string" ? value : undefined`：佇列項的值不是
        # 字串時**整欄不寫**（本機 574 個 `remove` 有 49 個是這樣，而且都不是人打的
        # 排隊句）。那些一律不配對——沒有可比的文字，配上去就是猜的。
        # 空字串則相反：它是「只貼了圖、沒打字」那一則的正常長相，而且只有在
        # `human` 裡真的有一筆同來源、空文字、**帶圖**的排隊句時才配得上（上面那段
        # 只在 `p or imgs` 時才建鍵），所以放行的範圍是被結構卡住的。
        if not isinstance(raw_c, str) or hkey not in human:
            continue
        # ⚠⚠ **每一個對得上的 `remove` 都要消耗一個 attachment 時刻**，
        # 不管它最後是由合成事件還是由真 user 事件呈現。舊版只在「真的合成」時才前進，
        # 於是「第一次被真 user 代表、第二次才合成」的那一句拿到**第一次**的按 Enter 時刻
        # （`utf-fix-codex-r2` 實測 `queued_at=['…10:00:01Z']`，實際是 `10:00:10`）。
        queued_at, queued_imgs = _take_queued_at(human, used, hkey)
        # ⚠⚠ **只消耗「同一個來源檔、而且排在這個 remove 之後」的真 user 事件。**
        # 那是實測到的唯一形狀（撤回後又重送：真事件寫在 remove 後面）。
        # 用「整場有沒有出現過」的話，**排在前面的**那一次會把後面這次排隊句吃掉，
        # 而前面那次是使用者**另一次**送出，兩者不是同一件事。
        key = clean_user_text(c).strip()
        hit = None
        for row in already:
            if row[3] or row[2] != key:
                continue
            if row[0] == (e.get("_srcf") or "") and row[1] > e.get("_i", 0):
                hit = row
                break
        if hit is not None:
            hit[3] = True          # 這一則真 user 事件就是這一次送出，別再畫第二次
            continue
        # ⚠⚠ **不可以把 `c` 加進 `already`。** 那會讓「同一句話在整場只補一次」——
        # 而使用者**真的會**排隊送出兩次一樣的話（「繼續」「go on」這種），
        # 第二次就被靜默丟掉了：**那正是這個功能要修的失效模式**（答案在、問題不在）。
        # 每個 `remove` 事件就是一次真的送出，一次一則。
        out.append({
            "type": "user",
            # ⚠⚠ **沒有圖片時維持字串**，不要一律包成 block 陣列：那會讓**所有**既有的
            # 排隊句改走另一條呈現路徑，等於為了新功能動到已經審過的舊行為。
            # 有圖片才換成 `[文字, 圖片…]`——那正是真的 user 事件貼圖時的長相，
            # 下游（呈現、MD、搜尋、封存分類）本來就都認得它。
            "message": {"role": "user", "content": _queued_content(c, queued_imgs)},
            "timestamp": e.get("timestamp"),
            "uuid": f"_queued:{e.get('_i')}",
            # ⚠⚠ **不可以硬寫 `False`。** 排隊句也可能發生在子代理的轉錄檔裡，
            # 硬寫的話它會被塞進主對話（`utf-fix-codex` Medium）。
            # ⚠ 真實語料目前 0 則落在 sidechain——照樣沿用來源事件的身分，
            #   因為「量到 0」講的是這份語料，不是這個判準（教訓 44）。
            "isSidechain": bool(e.get("isSidechain")),
            "_srcf": e.get("_srcf") or "",
            "_dt": e.get("_dt"),
            "_i": e.get("_i"),          # 來源檔行序＝它在檔案裡真正的位置，撞號 tiebreak 用
            # ⚠ 同一句排隊送出多次時，**第 n 次要配第 n 個按 Enter 的時刻**（上面已經取好）。
            "_queued_at": queued_at,
            "_synth": "queued",
        })
    return out


def _queued_content(text, imgs):
    """補出來的那一則 user 事件的 `message.content`。

    ⚠ 沒有圖片時**回原本的字串**（不是只有一個 text block 的陣列）：
    兩種在呈現上等價，但換掉的話**每一則既有的排隊句**都改走另一條路徑，
    為了新功能動到舊行為是不划算的交換。
    ⚠ 文字是空的（只貼圖沒打字）就不放空的 text block——空文字不是可呈現區塊，
    留著只會在下游多一個要判空的東西。
    """
    if not imgs:
        return text
    return ([{"type": "text", "text": text}] if text else []) + list(imgs)


def _take_queued_at(human, used, key):
    """取這一句**這一次**送出的 `(按下 Enter 的時刻, 貼上的圖片 blocks)`，逐次往後走。

    `key` 是 `(來源檔, prompt)`——**來源檔一定要在鍵裡面**，否則子代理的附件會被
    主檔的 `remove` 配走。

    ⚠ 舊版是 `human.get(prompt)`＝永遠回第一次的時刻，於是同一句排隊兩次時
    第二次的游標提示顯示的是**第一次**打字的時間——時間軸在說謊，
    而那正是這個欄位存在的唯一理由。
    ⚠ 用完了就沿用最後一個（`remove` 比 attachment 多是壞檔，不該整句不畫）。
    """
    ts = human.get(key) or []
    if not ts:
        return None, []
    i = used.get(key, 0)
    used[key] = i + 1
    if i < len(ts):
        return ts[i]
    # ⚠⚠ **沿用的只有時刻，圖片不沿用。** `remove` 比 attachment 多是壞檔（docstring 上面
    # 那段），沿用最後一筆是為了「不要整句不畫」；但那一筆現在帶著圖，照抄的話同一張
    # 使用者只貼過一次的圖會被畫第二次——性質從「時間軸不準」變成「多畫了他沒送出的東西」。
    return (ts[-1][0], [])


def synth_local_command_events(events):
    """`type:system, subtype:local_command` 的指令補成可呈現的事件，回傳新事件的 list。

    ⚠⚠ **指令有兩種存法，而且並存**（全語料實測，不是版本遷移——兩種在每個 CLI 版本都有）：

    | 存法 | 則數 | 例 |
    |---|---:|---|
    | `type:user`，內容是 `<command-name>…` | 575 | `/effort`、`/model`、`/exit`、`/loop`、`!` bash |
    | `type:system, subtype:local_command` | 345 | `/context` 140、`/status` 83、`/rename` 49、`/remote-control` 29 |

    只做前者等於**只做到 62.5%**——而 `/rc`（`/remote-control`）正是後者，
    所以「一開場就下的那個指令看不到」。⚠ 兩種形狀**零重疊**（實測），補了不會重複畫。

    做法是把它轉成一則 `type:user` 事件，**沿用同一條指令路徑**（`user_special_blocks()` →
    `_command` 區塊 → 併結果 → 窄橫列 → 不發耐久錨點）。多開一條路就是第二個產出端。

    ⚠ 這裡**不寫回 `s.events`**：`extract_rename()` 讀的就是這些原始 system 事件，
    而且「檔案裡有什麼」與「畫了什麼」要分得開。

    範圍限制 SCOPE-CMD-DUAL-STORAGE-DUP：去重只依 `uuid`。同一次指令若被兩種存法
    **各寫一則**（同時戳、同名、同參數），會畫成兩列。本函式不做跨形狀的語意去重——
    那個判準會誤殺「真的連下兩次同一個指令」。詳見 planning/scope-limits.md。
    """
    out = []
    for e in events:
        if e.get("type") != "system" or e.get("subtype") != "local_command":
            continue
        c = e.get("content")
        if not isinstance(c, str) or not c.strip():
            continue
        out.append({
            "type": "user",
            "message": {"role": "user", "content": c},
            "timestamp": e.get("timestamp"),
            "uuid": f"_localcmd:{e.get('uuid') or e.get('_i')}",
            "isSidechain": bool(e.get("isSidechain")),
            "_dt": e.get("_dt"),
            "_i": e.get("_i"),
            "_srcf": e.get("_srcf") or "",
            "_synth": "local_command",
        })
    return out


def group_turns(events, per_step=True, step_by_usage=False):
    """events 須為已排序、去重的訊息事件。
    新版把 assistant 的 thinking/text/tool_use 拆成多筆事件，這裡併回單一回合；
    純 tool_result 的 user 事件不另起回合（其輸出已附在對應的 tool_use 內）。

    「步驟」＝一次 API 呼叫，兩種切法擇一（逐步快取命中率）：
    - per_step（Claude）：每個新 message.id 起一步——一次呼叫拆成的多筆事件共用同一 id 且每筆都帶 usage。
    - step_by_usage（Codex）：事件 id 為逐筆合成、不能當步界；改以「帶 usage 的事件」起一步——
      載入器把每次呼叫的 usage（token_count）掛在該呼叫的第一筆事件上，該事件即步驟起點。
      呼叫沒有可呈現事件時 usage 已併回上一步（該步徽章為兩次呼叫合計）。"""
    turns, cur = [], None
    pending_ms = {}        # message.id -> 尚未掛上的 turn_duration（見下方 assistant 無內容分支）
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
            # 指令列／通知列：**在雜訊判定之前**。這些過去被 `is_noise_user()` 整則吃掉
            # （全語料 1170 則指令包裝＋872 則通知），而它們確實發生過。
            special = user_special_blocks(e)
            if special:
                # ⚠ 每個特殊區塊帶自己的時刻：掛進回合內部之後，`group["dt"]` 是**整輪的**
                # 起始時刻，區塊自己的時刻就再也回不來了（探針要靠它分辨兩種存法的同名指令）。
                for _b in special:
                    _b["dt"] = e.get("_dt")
                sp = special[0]
                # ⚠⚠ **殘餘文字要現清，不能沿用 `blocks`。** 上面那段把整串原文
                # （含包裝標籤）包成一個 `text` block；它在呈現時會被 `clean_user_text()`
                # 清成空字串而看不見，**但它不是空的**——拿它判斷「這一則有沒有對話內容」
                # 會永遠得到「有」，於是指令列與通知列照樣拿到耐久錨點，
                # 上面那條「既有書籤逐字不變」的保證就整條失效（實測：旗標 0 次為真、
                # 1512 個新回合全部拿到錨點）。
                # ⚠⚠ **圖片不可以進 `rest`。** `not rest` 是「這一則除了包裝以外還有沒有東西」
                # 的述詞，而下面那條路靠它決定**要不要切輪**：指令包裝的文字被
                # `clean_user_text()` 清成空 ⇒ 舊資料一律 `rest == []` ⇒ 掛進回合內部、不切輪。
                # 排隊句帶圖之後那個 image block 會讓 `rest` 變成非空 ⇒ 條件失效 ⇒ 自成一輪
                # ⇒ **那一輪被切成兩半、同一步後面的區塊錨點整批改指**（`qimg-fam` Low#5 實測：
                # 少掉 `-b3`／`-b4`，內容搬到新回合的 `-b1`／`-b2`）。那正是這個 repo
                # 花最多力氣在擋的失敗方向。
                # ⇒ 圖片改掛在特殊區塊自己身上（和 `_interject` 的 `imgs` 同一套做法），
                #   照樣畫得出來，但**不參與切輪的判斷、也不佔區塊序號**。
                # ⚠ 可達性目前是 0（要 CLI 把 `<command-name>` 這類包裝寫進佇列項的值，
                #   它現在不會）——但失敗方向是「既有書籤安靜指到別的內容」，所以照修。
                sp_imgs = [b for b in blocks if b.get("type") == "image"]
                if sp_imgs:
                    sp["imgs"] = (sp.get("imgs") or []) + sp_imgs
                if sp["type"] == "_notify":
                    rest = []       # 通知整則就是通知，原文已收在 `_notify` 區塊裡
                else:
                    rest = []
                    for b in blocks:
                        if b.get("type") == "image":
                            continue        # 見上面：圖片已經掛到 `sp` 上了
                        if b.get("type") != "text":
                            rest.append(b)
                            continue
                        ct = clean_user_text(b.get("text") or "")
                        if ct.strip():
                            rest.append({"type": "text", "text": ct})
                # 合併規則（實測：結果事件永遠是**下一則 user 事件、時間戳完全相同**）：
                # 只有結果、沒有指令名的那一則，併進上一列指令，不另起一輪。
                # ⚠ 不合併的話會多出一個**同時間戳**的回合 ⇒ 撞號組多一個成員 ⇒
                #   可能換人拿無後綴的耐久錨點（SCOPE-BOOKMARK-TIEBREAK-INSERT）。
                #   合併同時是「畫得對」與「不動到既有書籤」，兩件事指向同一個做法。
                # ⚠ 指令列現在有**兩個落點**（掛在開著的回合內部／自成一列 metarow），
                # 所以「把結果併回上一列」要**兩邊都找**。只找 metarow 那一邊的話，
                # 掛在回合內部的那些指令，結果會自己另起一輪（`utf-fam` 之後新增的落點）。
                if sp["type"] == "_command" and not sp.get("name") and sp.get("out"):
                    prev = None
                    # ⚠⚠ **只認「上一個區塊就是它」**——`cur["blocks"][-1]`。
                    # 指令掛進回合內部之後，中間只要插進任何助手內容
                    # （`_step`／`text`／`tool_use`），那一列就**不再**是最後一個區塊，
                    # 這時併回去就是跨過了中間的發言，畫面順序與時間順序不符。
                    # ⚠ 不可以「往回找最近的 `_command`」：那正好會跨過去。
                    if cur is not None and cur["role"] == "assistant" and cur["blocks"]:
                        cand = cur["blocks"][-1]
                        if cand.get("type") == "_command" and cand.get("dt") == e.get("_dt"):
                            prev = cand
                    # ⚠ **`cur is None` 是必要條件**：開著的 assistant 回合在 `cur` 裡、
                    # **不在 `turns` 裡**，所以只看 `turns[-1]` 會跨過它——
                    # 「指令 → 助手發言 → 指令輸出（同時戳）」會把輸出併回指令那一列，
                    # 畫面順序與時間順序不符。
                    if (prev is None and cur is None and turns
                            and turns[-1].get("_metarow")
                            and turns[-1]["dt"] == e.get("_dt")
                            and turns[-1]["blocks"]):
                        cand = turns[-1]["blocks"][-1]
                        if cand.get("type") == "_command":
                            prev = cand
                    if prev is not None and not prev.get("out"):
                        prev["out"] = sp["out"]
                        continue
                # ⚠⚠ **改動之前會被丟掉的東西，改動之後也不可以切輪。**
                # v55 之前指令是**整則被丟掉**的（`is_noise_user` → `continue`），
                # 所以它**從來不會打斷回合**。現在讓它變成一輪 ⇒ 一輪被切成兩半 ⇒
                # 後半是新的一輪、新的 `kanchor` ⇒ **原本在後半的那些區塊
                # `k…-b5`、`-b6` 整批換了歸屬**：舊的區塊錨點不是消失就是改指到別的內容。
                # 實測（回合＋區塊雙層差分）：**舊錨點消失 725、改指 889**。
                # ⚠ 判準不是「哪種好看」，是**保住原本的行為**：
                #   指令以前不切輪 ⇒ 現在也不可以切；
                #   通知以前就是一個 user 回合、本來就會切 ⇒ 繼續切（見下面那條路）。
                # ⚠ 掛進回合內部不會推移序號：`_command` 在 `block_is_renderable` 回 False。
                # ⚠⚠ **述詞是「v54 會不會把這一則當雜訊丟掉」**，不是「它是指令還是通知」。
                # `clean_user_text(txt) == ""` 就是 `is_noise_user()` 當初的判準本身。
                #   · 指令包裝 → 剝完是空的 → **以前被丟掉** → 不可以切輪
                #   · `<task-notification>` **被包在 `<system-reminder>` 裡**的那種
                #     → 也是剝完就空 → **以前也被丟掉** → 一樣不可以切輪
                #     （這一格是實測抓到的：只看「是不是通知」的話，這種會開始切輪，
                #      而它在真實語料裡很常見——本 session 的背景任務通知全是這個形狀）
                #   · 裸的 `<task-notification>` → 剝完還有字 → **以前就是一個 user 回合**
                #     → 繼續切輪、繼續拿它的區塊錨點（不動＝不破壞）
                # ⚠ 掛進回合內部的那些**一律不可以佔區塊序號**：帶 `inline` 標記，
                #   由 `block_is_renderable()` 認它。
                # ⚠⚠⚠ **一定要呼叫 `is_noise_user()` 本人，不可以重寫它的條件。**
                # 這裡原本寫成 `clean_user_text(txt) == ""`，自以為那「就是」它的判準——
                # 但那只是它的**第二行**；**第一行是 `if ev.get("isMeta"): return True`**，
                # 跟文字內容完全無關。
                # 失效方向最糟：背景任務通知在真實語料裡**幾乎全掛在 `isMeta` 事件上**
                # （抽驗某檔 9 則通知：isMeta 9、非 isMeta 0），而它們清乾淨後還有
                # 七、八千字 ⇒ 只看文字就一律判成「以前是一個回合」⇒ 開始切輪 ⇒
                # **舊的區塊錨點整批消失**（實測 144 個）。
                # ⇒ 這是「同一條規則寫兩次就會分岔」的**部分複製**版本：前半看起來完全
                #   正確，所以比整段抄錯更難發現。**要問的不是「條件對不對」，
                #   是「為什麼不直接叫那支函式」。**
                _was_noise = is_noise_user(e)
                if (_was_noise and not rest
                        and cur is not None and cur["role"] == "assistant"):
                    cur["blocks"].extend(dict(b, inline=True) for b in special)
                    continue
                if cur:
                    turns.append(cur)
                    cur = None
                # 同一則若「包裝＋真文字」都有（`/<skill>` 那種注入型指令），兩個區塊都留，
                # 指令列在前——它是這一則的起因。
                turns.append({"role": "user", "blocks": special + rest,
                              "dt": e.get("_dt"), "side": bool(e.get("isSidechain")),
                              "src_i": e.get("_i"),
                              "src_f": e.get("_srcf") or "",
                              # 整則都是指令／通知 ⇒ 畫成窄橫列，不套「👤 你」對話框。
                              "_metarow": not rest,
                              # ⚠⚠ **不給耐久錨點的只有指令列。**
                              # 通知列在這一版之前就是普通 user 回合、**本來就有錨點**，
                              # 拿掉會讓標在通知上的既有書籤安靜斷掉（實測會少 872 個）。
                              # 指令列是這一版才出現的東西，沒有人存過 ⇒ 不發錨點是安全的，
                              # 而且那正是「既有錨點集合逐字不變」的機械保證來源。
                              "_no_anchor": (not rest) and sp["type"] == "_command",
                              "compact": bool(e.get("isCompactSummary")),
                              "compact_meta": e.get("_compact_meta") or {}})
                continue
            if is_noise_user(e):
                continue
            # ⚠⚠ **中途插話不切輪。** 助手還在跑工具時打的字，全語料實測 **125/127（98.4%）
            # 是在同一輪之內送達的**（前後兩個 assistant 事件之間沒有 `turn_duration`）。
            # 把它當成一個獨立的 user 回合會**把那一輪切成兩輪**，畫面上看起來像
            # 「助手講完了 → 使用者說話 → 助手開始新的一輪」——那是假的。
            # 改成掛在**開著的那個 assistant 回合**內部，成為一個 `_interject` 區塊。
            # ⚠ 真的跨輪的（`remove` 落在 `turn_duration` 之後）走下面的一般 user 回合。
            # ⚠⚠ **靠的是 `_turn_done`，不是「沒有開著的回合」。** 本註解第一版寫的是後者，
            #   那是錯的：`cur` 在 `turn_duration` 之後**仍然開著**（它要等下一則 assistant
            #   事件才換），所以真正把它擋下來的是中間剛好有一則真 user 事件
            #   ——沒有那一則時就會掛錯（`utf-fix-codex` Medium 實測）。
            if (blocks and e.get("_queued_at") and cur is not None
                    and cur["role"] == "assistant"):
                cur["blocks"].append({
                    "type": "_interject",
                    "text": "\n".join(str(b.get("text") or "") for b in blocks
                                      if b.get("type") == "text"),
                    # ⚠⚠ **圖片掛在這個 block 自己身上，不另外進 `cur["blocks"]`。**
                    # `_interject` 在 `block_is_renderable()` 回 False ＝ 不佔 `-b<n>`；
                    # 圖片跟著它走才保得住那條保證。當成獨立 block 塞進去的話，
                    # **同一步之後每一個區塊的序號 +1**，既有的區塊書籤會安靜指到別的內容
                    # （`utf-fam` High#3 實測過的失敗方向）。
                    "imgs": [b for b in blocks if b.get("type") == "image"],
                    "dt": e.get("_dt"),
                    "queued_at": e.get("_queued_at"),
                    # ⚠⚠ **這一句是在那一輪「結束之後」才被讀到的**（前面已經有
                    # `turn_duration`）。它仍然掛在同一輪內部——**不可以改成獨立回合**：
                    # 那會把一輪切成兩輪，同一步之後的區塊錨點全部位移
                    # （實測改成切輪之後：舊錨點消失 54 個、新增 59 個）。
                    # 但標籤不可以照寫「中途插話」，那是假的 ⇒ 用這一格讓呈現層改口。
                    "after_turn": bool(cur.get("_turn_done")),
                })
                continue
            if blocks:
                if cur:
                    turns.append(cur)
                    cur = None
                turns.append({"role": "user", "blocks": blocks,
                              "dt": e.get("_dt"), "side": bool(e.get("isSidechain")),
                              "src_i": e.get("_i"),      # 來源檔行序
                              "src_f": e.get("_srcf") or "",   # 來源檔識別（主檔為空字串）
                              # 排隊送出的那一句按下 Enter 的時刻（見 synth_queued_user_events）
                              "queued_at": e.get("_queued_at"),
                              "compact": bool(e.get("isCompactSummary")),
                              "compact_meta": e.get("_compact_meta") or {}})
            # 否則（純 tool_result / 空白）略過，不打斷 assistant 回合
        elif role == "assistant":
            if not blocks:
                # 無可呈現內容（如純 thinking 簽章）的收尾事件也可能掛著 turn_duration：回合已開著就
                # 先把耗時收下，否則那段 wall-clock 會靜默消失、⏱ 偏低（實測有 1 筆 223 秒被丟掉）。
                if e.get("_turn_ms"):
                    if cur is not None and cur["role"] == "assistant":
                        cur["dur_ms"] = cur.get("dur_ms", 0) + e["_turn_ms"]
                        cur["_turn_done"] = True    # 見 `_interject` 那一段
                    else:
                        # 此刻還沒有開著的 assistant 回合（例如這筆緊接在可見 user 事件之後、
                        # 同一次呼叫的可呈現內容還沒出現）→ 直接丟掉的話那段 wall-clock 永遠消失。
                        # 先按 message.id 暫存，等同一次呼叫的內容出現時掛回去（真的 join，不是近似）。
                        _mid = (e.get("message") or {}).get("id")
                        if _mid:
                            pending_ms[_mid] = pending_ms.get(_mid, 0) + e["_turn_ms"]
                continue
            if cur is None or cur["role"] != "assistant":
                if cur:
                    turns.append(cur)
                cur = {"role": "assistant", "blocks": [],
                       "dt": e.get("_dt"), "side": bool(e.get("isSidechain")),
                       "src_i": e.get("_i"),      # 來源檔行序
                       "src_f": e.get("_srcf") or "",   # 來源檔識別（主檔為空字串）
                       "u": _new_turn_usage(), "n_steps": 0, "_step_ids": set()}
            # ⚠⚠ **新的 assistant 內容抵達 ＝ 又有一輪在跑了，`_turn_done` 要清掉。**
            # 只設不清的話，`turn_duration → 下一輪 assistant → 這一輪跑到一半的插話`
            # 會被誤標成「上一輪結束後才讀到」——實測 `interject_after_turn_flags=
            # [('read after first turn', True), ('interjected during second turn', True)]`
            # （`utf-fix-codex-r2` 驗收）。⚠ 清在這裡、設在本分支最後的 `_turn_ms` 那格，
            # 順序不可以顛倒。
            cur["_turn_done"] = False
            msg = e.get("message") or {}
            # 每個新的 message.id ＝ 回合內的一個步驟（一次 API 呼叫）；同一 id 拆成的多筆事件
            # 只在首見時插入分隔標記，標記後緊接該步驟的 blocks（思考/文字/工具…）。
            if per_step:
                mid = msg.get("id")
                step_key = mid if mid else f"_anon{id(e)}"
                if step_key not in cur["_step_ids"]:
                    cur["_step_ids"].add(step_key)
                    cur["n_steps"] += 1
                    cur["blocks"].append({"type": "_step", "idx": cur["n_steps"], "mid": mid,
                                          "u": _step_usage(msg, e), "t": _epoch(e),
                                          "tms": _epoch_ms(e)})
            elif step_by_usage:
                su = _step_usage(msg, e)
                if su:
                    cur["n_steps"] += 1
                    cur["blocks"].append({"type": "_step", "idx": cur["n_steps"], "u": su,
                                          "t": _epoch(e), "tms": _epoch_ms(e)})
            cur["blocks"].extend(blocks)
            _acc_turn_usage(cur["u"], msg)
            _mid_now = msg.get("id")
            if _mid_now and _mid_now in pending_ms:   # 先前無呈現內容時暫存的耗時：同一次呼叫，掛回來
                cur["dur_ms"] = cur.get("dur_ms", 0) + pending_ms.pop(_mid_now)
            if e.get("_turn_ms"):
                # system/turn_duration 掛在「真實回合」最後一筆 assistant 上，但一個顯示回合可能併了
                # 好幾個真實回合（中間的 user 事件是純 tool_result／雜訊，不另起回合）→ 要累加，
                # 覆寫會只留最後一段（實測最壞：顯示 3 秒、其實跑了 651 秒）。
                cur["dur_ms"] = cur.get("dur_ms", 0) + e["_turn_ms"]
                # ⚠⚠ **這一輪到此為止。** `turn_duration` 是回合結束的訊號，
                # 而 `cur` 會一直開著等下一則 assistant 事件 ⇒ 不標記的話，
                # 落在這之後的排隊句仍然會被掛成「這一輪內部的插話」，
                # 畫面上變成他在一個**已經結束**的回合裡插話
                # （`utf-fix-codex` Medium 實測 `interject_hosts=[('assistant', …)]`）。
                cur["_turn_done"] = True
    if cur:
        turns.append(cur)
    return turns


def _tiebreak_key(g):
    """撞號組內的決定性排序鍵＝**來源檔行序**。**這不是身分**，只是讓「誰拿到無後綴的錨點」
    有一個與呈現結構無關的依據。

    ⚠⚠ 前一版用 `(bool(side), role, 首個 block 前 200 字)`，三個問題全被 `durable-anchor-r2`
    抓到，**而且它的註解宣稱修掉了其中一個、實際上沒有**：

    1. `bool(side)` 是第一個鍵，而 `False < True` ⇒ **主回合永遠排在子代理之前**，
       與 `s.main_groups + s.side_groups` 的 list 位置完全同序——它宣稱修掉的「主搶子」原封不動。
    2. `blocks[0]` 對 assistant 回合**永遠是 `_step` marker**（`group_turns` 在
       `cur["blocks"].extend(blocks)` **之前**就把它 append 進去了），所以鍵是常數
       `(False,'assistant','_step')`，「內容導出」在最常見的情況下等於沒有。
    3. 用文字當鍵讓**錨點指派依賴訊息內文**——任何改變首個 block 取法的變更（正規化、
       block 過濾、清洗規則）都會換人拿無後綴錨點。而本檔拒絕內容雜湊的理由正是
       「要一整套正規化規則」。自己踩了自己拒絕的那條。

    `src_i` 來自事件的 `_i`（兩邊 loader 覆蓋率實測皆 100%，Claude 36368 回合／Codex 1323
    回合都沒有缺）。⚠ **兩邊的語意不同**：Claude 是 `load_events` 的**逐檔行序**；
    Codex 是 `load_codex_session` 重新 `enumerate(raw)` 的**解析成功序**。兩者都對 append 穩定。

    **鍵是 `(來源檔識別, 逐檔行序)`。** 只有行序不夠：`enumerate(fh)` 每個檔從 0 重新起算，
    而一場 session 的事件來自**多個檔**（主檔 ＋ `<sid>/**/*.jsonl` 子代理轉錄，見
    `load_session`）。單拿行序比會打平，而 `list.sort` 是穩定排序 ⇒ 退回
    `s.main_groups + s.side_groups` 的位置 ⇒ 主搶子。⚠ **這個錯我犯過三次**
    （`durable-anchor-y1` #2 → `r2` #3 → `r4` #1），每次都是註解宣稱了保證卻沒人在守。
    現在守它的是 `tests/test_smoke.py::test_anchor_tiebreak_main_vs_side`。

    **它保證什麼**：主檔的事件（`src_f` 為空字串）排在所有子代理檔之前。
    這是刻意的方向——子代理轉錄檔是被主對話的 Task 呼叫起來的，**它在主回合之後才落地**。
    所以「主回合原本獨佔某毫秒、使用者存了書籤 → 子代理檔之後才出現」這個真實情境下，
    既有書籤**不會**被搶走。

    **它不保證什麼**（＝`SCOPE-BOOKMARK-TIEBREAK-INSERT` 的殘餘）：
    反方向不行（子代理先、主回合後出現時主回合仍會搶）；同一檔內插入行序更前的新輪會搶；
    新的子代理檔若檔名排序在既有那個之前也會搶。**要完全免疫只有讓每個錨點都帶內容雜湊**，
    那會把「撞號組只有 4 個」換成「正規化一改、全部書籤同時失效」。

    ⚠ 兩邊 loader 的 `_i` 語意不同：Claude 是 `load_events` 的**逐檔行序**；
    Codex 是 `load_codex_session` 重新 `enumerate(raw)` 的**解析成功序**（`durable-anchor-r4` #7）。
    兩者都對 append 穩定，覆蓋率實測皆 100%（Claude 36368 回合／Codex 1323 回合都沒缺）。
    取不到時退回 `-1`（排在最前），至少是決定性的。

    ⚠ 它仍**解不掉插入不穩定**：原本獨佔某毫秒的回合拿無後綴錨點，之後若有新輪撞進同一毫秒
    且行序更前，就會換人。要完全免疫只有「每個錨點一律帶內容雜湊後綴」，那等於把
    「9166 分之 1」換成「正規化一改、全部書籤同時失效」。
    **範圍限制 SCOPE-BOOKMARK-TIEBREAK-INSERT**：詳見 planning/scope-limits.md。
    """
    i = g.get("src_i")
    return (str(g.get("src_f") or ""), i if isinstance(i, int) else -1)


def durable_anchor(dt):
    """回合的**耐久錨點**（書籤用）：`k<YYYYMMDD>T<HHMMSS><mmm>Z`，UTC。沒有時間就回空字串。

    **為什麼是時間戳，不是內容雜湊**：`dt` 本來就掛在每一輪上（`group_turns` 建 turn 時就存了），
    全語料實測 9047 輪**缺漏 0**、**撞號 1**。內容雜湊要一整套正規化規則＋格式版本號＋改規則時
    的遷移，為那 0.01% 的差別不划算。⚠ 真正的理由不是機率而是**序號會位移**：
    `t{n}` 是 `enumerate` 出來的，任何讓分組多一輪的解析器修正都會把其後全部往後推——
    四週實測 Codex 側 6 輪這樣漂掉（其中一場從 1 輪變 37 輪），而時間戳側 0。
    量測隨時可重跑：`python scripts/probe_turn_identity.py drift <舊commit>`。

    **為什麼保留毫秒**：只截到整秒，撞號會從 1 變 31。

    **為什麼是 UTC**：檔名用的是 `s.start.astimezone()`（本地時間），機器時區一變整批就變
    ——錨點不能再犯同一個錯。使用者要看的本地時間由 `#` 連結的 `title` 提供：
    **網址給機器，UI 給人。**

    ⚠ 開頭一律是 `k`：HTML `id` 與 URL fragment 都不該以數字開頭。
    ⚠ **不要為了可讀性加冒號**：`getElementById` 沒事，但 `querySelector('#k2026:08')` 會炸。
    """
    if dt is None:
        return ""
    try:
        _tz = dt.tzinfo
    except AttributeError:      # 傳進來的不是 datetime（date／str／int）：不該讓整頁建置炸掉
        return ""
    if _tz is None or _tz.utcoffset(dt) is None:
        # ⚠ naive datetime 走 `astimezone()` 會**默默假設本機時區**——那正是本函式存在要避開的
        # 失效模式（換台機器就換一批錨點）。本 codebase 的 `dt` 全部來自 `parse_ts`（已正規化成
        # aware），實測 9166 輪 naive 0 個；但**寧可沒有錨點，也不要給一個會隨機器漂的**。
        return ""
    try:
        u = dt.astimezone(timezone.utc)
    except Exception:      # 極端時刻在部分平台會丟 OSError，顯示層不該因此整頁建置失敗
        return ""
    return f"k{u:%Y%m%dT%H%M%S}{u.microsecond // 1000:03d}Z"


def analyze(s, acct_switches=None):
    """建立 main/side 回合、tool 結果對照表與統計（render 前先呼叫一次）。
    acct_switches：load_account_switches() 的結果，供快取成因辨識「自願切帳號」。"""
    # 回合耗時：system/subtype=turn_duration 事件的 parentUuid 指向該回合最後一筆 assistant，
    # 據此把「送出→答完」的 wall-clock 掛回該事件（Claude 專屬；沒有此事件的舊版資料就不顯示）。
    dur_by_uuid = {}
    for e in s.events:
        if e.get("type") == "system" and e.get("subtype") == "turn_duration":
            ms = e.get("durationMs")
            pu = e.get("parentUuid")
            if pu and isinstance(ms, (int, float)) and ms > 0:
                dur_by_uuid[pu] = int(ms)
    if dur_by_uuid:
        for e in s.events:
            ms = dur_by_uuid.get(e.get("uuid"))
            if ms and e.get("type") == "assistant":
                e["_turn_ms"] = ms
    # 注意：parentUuid 指到非 assistant（user／tool_result／system，實測約 14%）或找不到對象的
    # turn_duration 一律不掛、該回合就不顯示 ⏱——刻意不做近似、也不改掛別的事件（G1 曾因 dur_ms
    # 覆寫踩過「顯示 3 秒、實際 651 秒」，這裡寧缺勿錯）。
    # 掛上之後還有一條會丟：目標事件若沒有可呈現內容，group_turns 會跳過它——那裡另有補收（見該處註解）。
    msg = [e for e in s.events if e.get("type") in ("user", "assistant")]
    # 排隊送出的提示詞在檔案裡沒有 user 事件，補進來才畫得出（見 synth_queued_user_events）。
    # ⚠ 只加進**這個區域變數**，不寫回 `s.events`：`collect_cache_steps()` 等消費端讀的是
    # 原始事件流，塞合成事件進去會讓「檔案裡有什麼」與「畫了什麼」變成同一份，之後
    # 任何歸因量測都分不出哪些是推出來的。
    if s.source_kind == SOURCE_CLAUDE:
        msg = msg + synth_queued_user_events(s.events, getattr(s.path, "name", ""))
        # 指令的**另一種存法**（`system/local_command`，全語料 345 則，含 `/rc`）
        msg = msg + synth_local_command_events(s.events)
    main = dedup(sorted([e for e in msg if not e.get("isSidechain")], key=ts_key))
    side = dedup(sorted([e for e in msg if e.get("isSidechain")], key=ts_key))
    codex = s.source_kind == SOURCE_CODEX      # 逐步切法依來源而異（見 group_turns 說明）
    s.main_groups = group_turns(main, per_step=not codex, step_by_usage=codex)
    # 子代理依「父 Task tool_use id」分組，之後就地接在該 Task 底下（無法對應者退回頁尾）
    by_parent = {}
    for e in side:
        by_parent.setdefault(e.get("_parent_tool_use") or "", []).append(e)
    s.subagent_map = {}        # tool_use_id -> [turn groups]
    s.subagent_meta = {}       # tool_use_id -> {"type":..., "desc":...}
    for tid, evs in by_parent.items():
        groups = group_turns(evs, per_step=not codex, step_by_usage=codex)
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
    # **耐久錨點**（書籤用）：見 `durable_anchor` 的說明。與 `t{n}`／`s{n}` **並存**——
    # 後者是全文搜尋既有的基礎設施（`--search` 的跳轉、MD 的 `{#tN}`），**不可拿掉**。
    # 命名空間是**整頁**（主對話＋子代理合起來）：HTML `id` 只需在單頁唯一，而一頁＝一場 session。
    _by_ts = {}
    for g in s.main_groups + s.side_groups:
        # 沒有時間的回合不給耐久錨點（實測本機語料 0 個，但 `dt` 可以是 None）。
        # ⚠ **不要退回 `t{n}`**：那會讓一個看起來一樣的連結帶著完全不同的耐久性質出去。
        g["kanchor"] = ""
        # ⚠⚠ **只有指令列不進撞號池**（`_no_anchor` 只在 `_command` 那一種為真）。
        # ⚠⚠ **自成一列的通知照進、照拿錨點**——它在 v55 之前就是普通 user 回合、
        #    本來就有錨點，拿掉會讓標在通知上的既有書籤安靜斷掉（實測會少 872 個）。
        #    「新東西不拿」安全、「舊東西不再拿」是破壞，兩者在碼裡長得一樣（教訓 53）。
        # 指令列不進池子的三個理由，缺一都不足以決定：
        #  ① 它不是對話——`/effort high` 加書籤沒有意義。
        #  ② **這是「既有書籤一個都不會動到」的唯一機械保證。** 錨點是時間戳，
        #     新回合只要不進池子，撞號組的成員就逐字不變，誰拿無後綴錨點也就不變
        #     （否則正中 SCOPE-BOOKMARK-TIEBREAK-INSERT 那一格）。
        #  ③ 少一批 `#`／`☆` 控制項的位元組（筆數自己跑 `inventory` 現撈）。
        # ⚠ **混著真文字的那種照給**（`_no_anchor` 為 False）：那一則有對話內容。
        if g.get("_no_anchor"):
            continue
        _ka = durable_anchor(g.get("dt"))
        if _ka:
            _by_ts.setdefault(_ka, []).append(g)
    for _ka, _grp in _by_ts.items():
        # ⚠ **撞號組「內」用來源檔行序排，不看 list 位置。** 用位置的話，
        # `s.main_groups + s.side_groups` 這個「主回合全排在子代理之前」的人為順序，
        # 會讓一個**新出現的主回合**直接搶走某個子代理回合原本的無後綴錨點
        # （`durable-anchor-y1` #2 實測過）。行序至少與訊息內文脫鉤。
        # ⚠⚠ **但它「沒有」解掉主/子互搶**——`_i` 是逐檔行號，主檔與子代理檔不可比，
        # 打平時穩定排序會退回 list 位置。這句話我在這裡寫錯過三次
        # （y1 #2 → r2 #3 → r4 #1），**現在是誠實版，不要再往上加保證**。
        # 未修，見 SCOPE-BOOKMARK-TIEBREAK-INSERT〈第四輪〉。
        # ⚠⚠ **但這解不掉「插入新輪可能換人拿無後綴錨點」**——那取決於撞號組的成員，
        # 不是排序法。**範圍限制 SCOPE-BOOKMARK-TIEBREAK-INSERT**，詳見 planning/scope-limits.md。
        if len(_grp) > 1:
            _grp.sort(key=_tiebreak_key)
        for _i, g in enumerate(_grp, 1):
            # 第一個不加後綴：後綴只在真的撞號時出現，沒撞號的錨點就是乾淨的時間戳。
            # ⚠ 後綴用 **`-tb<n>`** 而不是裸數字：錨點文法是
            # `k<時戳>[-tb<n>][-s<步時戳>][-b<區塊序>]`，**每一段都要自帶前綴**才分得開哪一段
            # 是什麼。裸數字會讓退化規則只能靠「最後一段是不是數字」猜——而第 4 期一旦讓
            # 子定位符自己帶 tiebreak（`k…-s…-2`），那種錨點就**永遠退化不了**
            # （`durable-anchor-r2` #9）。
            g["kanchor"] = _ka if _i == 1 else f"{_ka}-tb{_i}"
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
    s.cache_steps, s.cache_models, s.cache_events, _step_mids = collect_cache_steps(s, acct_switches)
    # 顯示層與歸因各拉一條線：這裡是切換時刻本身，`cache_events` 那條是與 limit/auth/compact
    # 合併排序的事件流。⚠ **兩者精度相同**（都保留 history 的次秒；實測 24 個切換時刻 24 個帶
    # 小數）——`int()` 截整秒只發生在歸因迴圈比對**上界**的那一刻（見 `classify_cache_causes`
    # 消費事件那段），不在存進 `cache_events` 的時候。分兩條的理由是**形狀**不同，不是精度。
    s.acct_times = sorted((acct_switches or {}).get(s.session_id, ()))
    s.waste_n, s.waste_usd, s.waste_partial = session_waste(s)
    # 步驟時間正規化成「該次呼叫的起點」＝該 message.id 第一筆事件的時間，與 cache_steps 同鍵。
    # 必要原因：`_step` 標記是掛在第一筆**有可呈現內容**的事件上（group_turns 對 assistant 有
    # `if not blocks: continue`），而實測近六成呼叫的首事件沒有可呈現內容（純 thinking 簽章等），
    # 兩者差 1〜30+ 秒 → ①成因 join 對不上（真失效步被畫成中性灰「未歸因」）②「距上一步」量到的
    # 是「上次吐出內容→這次吐出內容」而非呼叫間隔，甚至會跨過 2 分（回合內雜訊/TTL 樣本）的界線。
    call_ep = {}
    call_ep_ms = {}     # 同一把鍵的毫秒版，只給區塊錨點的 `-s` 段（見 `_epoch_ms`）
    for e in sorted((e for e in s.events if e.get("type") == "assistant"), key=ts_key):
        mid = (e.get("message") or {}).get("id")
        ep = _epoch(e)
        if mid and ep is not None and mid not in call_ep:
            call_ep[mid] = ep
            call_ep_ms[mid] = _epoch_ms(e)
    for g in s.main_groups + s.side_groups:
        for b in g.get("blocks", []):
            if b.get("type") == "_step" and b.get("mid") in call_ep:
                b["t"] = call_ep[b["mid"]]
                # ⚠ 兩欄必須**同時**正規化到同一次呼叫的起點，否則 `t` 指第一筆事件、
                # `tms` 指另一筆，錨點就會隨「哪一筆事件先有可呈現內容」而漂。
                b["tms"] = call_ep_ms[b["mid"]]
    # 逐步冷啟成因：掛到「Claude 主對話」各步驟徽章（依 epoch join），供 _cold_display／turn_cold_class
    # 決定紅標（真失效）或中性灰（結構性）。只有 Claude 主對話會被標：每個 _step 都設成成因字串或 ""
    #（＝已分析、非失效成因），故其 cause 永不為 None；Codex 與子代理不跑分析、cause 維持 None＝原本紅標。
    if s.source_kind != SOURCE_CODEX:
        masked = {}
        causes = (classify_cache_causes(s.cache_steps, s.cache_models, s.cache_events, masked)
                  if s.cache_steps else {})
        # join 用 **message.id**，不用「同秒第幾次」的序號：序號的兩端母體不同——生產端
        # （classify_cache_causes）只數得出 usage 的呼叫，消費端這裡走的是**所有** `_step` 區塊，
        # 而沒有 usage 的呼叫照樣會產生 `_step`（group_turns 對每個新 message.id 都插一個）。
        # 同一秒裡只要夾進一個沒有 usage 的步，其後的序號就整批錯位，成因掛到錯的步驟上，
        # 而徽章看起來一切正常。id 是兩端共有的身分，數不數得到 usage 都不會偏移。
        # 生產端的鍵仍照 _cause_key 算（同秒同 usage 的多次呼叫仍需序號消歧義），這裡只是把
        # 「哪個 id 對應哪把鍵」先建好，讓消費端不必自己數。
        seen_t, key_by_mid = {}, {}
        for st, mid in zip(s.cache_steps, _step_mids):
            n = seen_t.get(st[0], 0)
            seen_t[st[0]] = n + 1
            if mid:
                key_by_mid[mid] = _cause_key(st[0], n)
        for g in s.main_groups:
            for b in g.get("blocks", []):
                if b.get("type") == "_step":
                    # 對不到 id 的步（無 message.id、或不在 cache_steps 裡）留空＝「已分析、
                    # 非失效成因」。寧可不標，也不要靠位置猜一個掛上去——那是把推論講成實據。
                    k = key_by_mid.get(b.get("mid"))
                    b["cause"] = causes.get(k, "") if k is not None else ""
                    # 被實據蓋過的人因條件跟著同一把鍵走：成因欄只寫得下一個，這一欄負責
                    # 讓另一個仍然看得見（顯示端：`_cold_cause_label`；報告端：`_masked_human_note`）。
                    b["cause_masked"] = masked.get(k) if k is not None else None
    # 每步「距上一步多久」：同一條對話軸（主對話／子代理各自算）上一次 API 呼叫到這次的間隔——
    # 快取 TTL 是「距上次使用」在算的，這個數字才是判讀冷熱的直接依據（statusline 的 (Xs ago) 同義）。
    for groups in [s.main_groups] + list(s.subagent_map.values()):   # 子代理各自一條軸，不與主對話相混
        prev_t = None
        for g in groups:
            for b in g.get("blocks", []):
                if b.get("type") != "_step":
                    continue
                t = b.get("t")
                if t is None:
                    continue
                if prev_t is not None and t >= prev_t:
                    b["gap"] = t - prev_t
                prev_t = t
    first_txt = first_user_text(s.events)
    s.kind = ("review" if REVIEW_RE.search(first_txt)
              else "exec" if s.exec_origin else "chat")
    s.codex_steps = collect_codex_steps(s)


def _collect_usage(s):
    """彙整模型、token、估算成本與快取命中率（依 message.id 去重，避免拆成多筆事件時重複計）。"""
    models, seen = [], set()
    inp = cc = cr = out = 0
    ctx_peak = 0
    peak_model = ""
    cost = 0.0
    unpriced = False
    resume_ctx = 0          # 最後一筆主對話 assistant 的脈絡 = resume 後大約載入的 context
    resume_model = ""
    miss_kinds = {}         # API 自報失效成因 -> 次數（含部分失效）
    miss_tok = 0
    miss_usd = 0.0
    miss_usd_partial = False     # 有前綴變動步是未知模型、金額估不出（表頭顯示 +?）
    miss_cold = 0           # 其中「整段沒命中」的次數（其餘為只掉一段的部分失效）
    efforts = []            # 出現過的 effort 等級（依序、去重）——中途改 effort 會影響快取
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
        if i + c1 + c2 > ctx_peak:
            ctx_peak = i + c1 + c2
            peak_model = mdl or ""     # 峰值屬於哪個模型：換算 % 要用「那一步的」視窗，
                                       # 不能用主對話最後的模型（子代理可能是別的模型、視窗不同）

        if not e.get("isSidechain") and (i + c1 + c2) > 0:
            resume_ctx = i + c1 + c2          # 主對話按時間在後者覆蓋前者 → 最終為最後一筆
            resume_model = mdl or resume_model
        c5, c1h = _ephemeral_split(u)
        c = call_cost(mdl, i, c1, c2, o, c5, c1h)
        if c is None:
            unpriced = True
        else:
            cost += c
        eff = str(e.get("effort") or "")
        if eff and eff not in efforts:
            efforts.append(eff)
        reason, mtok = _miss_reason(msg)
        if reason:
            miss_kinds[reason] = miss_kinds.get(reason, 0) + 1
            miss_tok += mtok
            if c2 * 100 < CACHE_COLD_PCT * (i + c1 + c2):
                miss_cold += 1
            w = rewrite_waste_usd(mdl, mtok, c5, c1h, wrote=c1)
            if w:
                miss_usd += w
            elif mtok > 0 and c1 > 0:
                miss_usd_partial = True        # 同上：表頭金額要標 +?，不可靜默低估
    total_in = inp + cc + cr
    s.models = models
    s.tok_out = out
    s.ctx_peak = ctx_peak
    s.ctx_peak_win = context_window(peak_model)     # 峰值那一步模型的 context 視窗（查不到＝None）
    s.resume_ctx = resume_ctx
    s.resume_model = resume_model
    s.cost = cost
    s.cost_partial = unpriced
    s.miss_kinds = miss_kinds
    s.miss_tok = miss_tok
    s.miss_usd = miss_usd
    s.miss_usd_partial = miss_usd_partial
    s.miss_cold = miss_cold
    s.efforts = efforts
    s.cache_pct = round(100 * cr / total_in) if total_in else 0
    s.usage = {"input": inp, "cache_create": cc, "cache_read": cr, "output": out, "total_in": total_in}


def _step_miss(st):
    """cache_steps 一步的 API 自報成因 → (成因字串 or "", 失效前綴 token 數)；舊格式（7 欄）回 ("", 0)。
    code==API_MISS_UNKNOWN_CODE 是「未知的新成因」哨兵（collect_cache_steps 存的）：以通稱 "unknown"
    回報，好讓次數與失效前綴長度照樣進報告、不被靜默吞掉（逐步徽章那邊仍顯示原始型別字串）。"""
    if len(st) < 9:
        return "", 0
    code = st[7]
    if code == API_MISS_UNKNOWN_CODE:
        return "unknown", st[8]
    return (API_MISS_CODES[code] if 0 < code < len(API_MISS_CODES) else ""), st[8]


def session_waste(s):
    """本場「人因可避免」的冷啟次數與估算浪費金額 → (次數, 金額, 金額不完整?)。

    只算 _HUMAN_CAUSES（閒置過期／自行切帳號）——結構性的（第一句、被迫切帳號、換模型、壓縮、
    前綴變動）與伺服器側的 evict 都不算，那些不是改習慣能省的，算進去會讓使用者對著自己
    無能為力的事自責。
    金額口徑與 cache-report 的「可避免浪費」一致：重寫成本 −（若命中）同量讀取成本。
    成因沿用 classify_cache_causes，不另寫一套判定，免得索引與報告對同一場說法不一。"""
    raw = getattr(s, "cache_steps", None) or []
    if not raw:
        return 0, 0.0, False
    models = getattr(s, "cache_models", []) or []
    causes = classify_cache_causes(raw, models, getattr(s, "cache_events", []) or [])
    if not causes:
        return 0, 0.0, False
    if not any(k in _HUMAN_CAUSES for k in causes.values()):
        return 0, 0.0, False
    _seen, n, usd, partial = {}, 0, 0.0, False
    for st in raw:
        k = _seen.get(st[0], 0)
        _seen[st[0]] = k + 1
        if causes.get(_cause_key(st[0], k)) not in _HUMAN_CAUSES:
            continue
        n += 1
        mi = st[6]
        price = model_price(models[mi]) if 0 <= mi < len(models) else None
        if not price:
            partial = True
            continue
        legacy = max(st[3] - st[4] - st[5], 0)
        write = (legacy + st[4]) * CACHE_WRITE_MULT + st[5] * CACHE_WRITE_MULT_1H
        usd += (write - st[3] * CACHE_READ_MULT) * price[0] / 1_000_000
    return n, max(usd, 0.0), partial


def claude_config_dirs(project_roots=()) -> set:
    """本機所有 Claude config 目錄（放 history.jsonl 的那一層）。

    ⚠ **不可**從 collect_sources() 的結果推導：那份清單經過 _dedupe_by_realpath，而多帳號機器上
    各帳號的 `projects/` 常是指向同一實體的 junction（本機四個帳號就是），會被收斂成一個 —— 對
    掃 session 是對的（避免重複工），但 `history.jsonl` 是**各帳號各一份、沒有共用**，跟著收斂就
    只剩一份、切帳號永遠偵測不到（實測：真實建置得到 acct=0，逐檔跑卻有 10）。
    所以這裡獨立列舉 ~/.claude*，另把外部指定來源的上層也算進來（自訂佈局仍能work）。

    `project_roots` 這條補充路徑讓自訂佈局（config 目錄不在 `~/.claude*` 樣式底下）也涵蓋得到。
    ⚠ 呼叫端要餵**去重之前**的來源清單（`claude_source_items()`，不是 `collect_sources()`）：
    後者經過 realpath 去重，多個帳號的 `projects/` junction 到同一實體時只會剩一個，
    這條路徑就只補得進一個 config 目錄。"""
    dirs = {d for d in Path.home().glob(".claude*") if d.is_dir()}
    for root in project_roots:
        parent = Path(root).parent
        if parent.is_dir():
            dirs.add(parent)
    return dirs


# `history.jsonl` 的 timestamp 是 epoch **毫秒**。上游哪天改成秒的話，值會落在 1970 年附近，
# 除以 1000 之後仍是「合法的浮點數」——切帳號時刻靜默變成 1970 年，而壞列計數是 0、完全無聲。
# 用一個寬鬆的合理年代區間擋下來；範圍刻意開得很寬，只擋「量級整個錯掉」這一種。
_HISTORY_MS_MIN = 946_684_800_000        # 2000-01-01
_HISTORY_MS_MAX = 4_102_444_800_000      # 2100-01-01


def _history_epoch_ms(ts):
    """history.jsonl 一列的 timestamp → epoch 毫秒（float）；取不出來回 None。

    ⚠ 數字也可能被序列化成**字串**。只認 int/float 的話，整份 history 一列都收不到、回空 dict，
    而呼叫端看到的是「這台沒切過帳號」——與「讀不到」完全無法區分，正是「不得靜默丟資料」
    要擋的形狀。bool 是 int 的子型別，要先排掉，否則 `true` 會被當成 1 毫秒。
    ⚠ **非有限值一定要在這裡擋掉**（`inf`／`nan`；JSON 的裸 `Infinity`／`NaN` 與字串
    `"Infinity"` 都產得出來）。它們過得了 `float()`，卻會在後面轉 `int()` 時丟 OverflowError
    ——**一列壞資料就中止整個建置**。這是選配資料源，它的壞列只能被計為壞列，
    不可以把整個轉換一起拖下水。
    ⚠ **量級也要檢查**（`_HISTORY_MS_MIN`／`_MAX`）：只認「轉得成 float」的話，上游把單位從
    毫秒改成秒時整份 history 都會通過，切帳號時刻卻全部變成 1970 年——而壞列數是 0、沒有
    任何跡象。涵蓋的是「量級整個錯掉」，不是逐列的日期正確性。"""
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        val = float(ts)
    elif isinstance(ts, str):
        try:
            val = float(ts.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(val):
        return None
    return val if _HISTORY_MS_MIN <= val <= _HISTORY_MS_MAX else None


def load_account_switches(config_dirs, health=None) -> dict:
    """跨帳號切換邊界 → {sessionId: [切換時刻 epoch, …]}。

    偵測依據：每個帳號的 config 目錄各有**自己的** `history.jsonl`（與 `projects/` 不同，它不是
    junction 共用），每列記 sessionId ＋ timestamp。同一個 sessionId 出現在兩個帳號的 history
    裡，就代表那場對話中途換過帳號；切換時刻取「新帳號的第一個 prompt」。

    為什麼需要它：快取按 organization 隔離，Claude API 上另外還按 workspace 再隔一層；換帳號等於
    把整段前綴丟掉。本模組的判別粒度是**config 目錄**（history.jsonl 一列只有 sessionId 與時刻，
    沒有任何帳號／組織／workspace 身分欄位），涵蓋的是「不同 config 目錄＝不同登入」這個假設；
    範圍限制見 planning/scope-limits.md 的 SCOPE-CFG-PATH-IDENTITY。但沒撞 limit 的自願切換
    **不會**留下 429/401，transcript 裡沒有任何欄位標示帳號 → 那一步只會被看成「1h TTL 內卻冷啟」
    而歸成 evict（實測本機 22 個切換邊界有 10 次落在 evict，佔 evict 總數的 38%）。

    ⚠ 三個已知限制（報告文案要照實寫，不可假裝偵測完備）：
      ① 只有多帳號的機器才有東西可偵測；單帳號時回空 dict，整條路徑自然失效。
      ② history.jsonl 是**本機**狀態：跨機同步過來的 transcript，另一台的 history 不在這裡，
         那台發生的切換偵測不到。
      ③ 上游的列格式若整個換掉，這裡會收不到任何列。**回空 dict 有三種完全不同的意思**
         （沒切過／沒有檔案／讀得到但一列都認不得），所以 `health` 要一起帶出去讓報告揭露，
         否則「0 次切換」會被讀成「你沒切過帳號」。
    只讀 sessionId／timestamp；**不讀 `display`**（那是 prompt 內文，不該進入本工具的資料流）。

    `health`：呼叫端可傳入一個 dict 收集涵蓋率（掃了幾個目錄／幾個檔／幾列／幾列認不得）。
    刻意用「傳入被填寫」而不是改回傳值——回傳值有多個呼叫端與測試在用，形狀不動。
    """
    # `files_blank`（v36-fam6 #5）：有列、卻一列都認不得的 history 份數。
    h = {"dirs": 0, "files": 0, "rows": 0, "bad_rows": 0, "read_errors": 0, "files_blank": 0}
    by_sid: dict[str, list] = {}
    for cfg in config_dirs:
        h["dirs"] += 1
        hist = Path(cfg) / "history.jsonl"
        if not hist.is_file():
            continue
        # 帳號身分＝**正規化後的實體路徑**，不是目錄名。不同位置的 config 目錄很容易同名（都叫
        # `.claude`），拿目錄名當身分會把兩個帳號併成一個 → 它們之間的切換永遠偵測不到。
        # 反向也對：同一個目錄的別名（junction／symlink）會收斂成同一個身分，本來就不算切換。
        try:
            label = str(Path(cfg).resolve()).casefold()
        except OSError:
            label = str(cfg).casefold()
        # (v36-fam6 #5) `files` 在 `open()` **成功之後**才加：舊寫法開檔失敗時 `files` 與
        # `read_errors` 會同時加一，於是 `_acct_scope_note` 的「讀到 N 份 history」把讀不到的
        # 那幾份也算進去了——揭露涵蓋率的句子本身高估涵蓋率，方向剛好相反。
        try:
            fh = hist.open("r", encoding="utf-8", errors="replace")
        except Exception as e:
            h["read_errors"] += 1
            print(f"  ! 讀取失敗 {hist}: {e}", file=sys.stderr)
            continue
        h["files"] += 1
        # (v36-fam6 #5) 逐檔記「這一份有沒有貢獻任何認得的列」。舊哨兵的條件是
        # `h["files"] and not by_sid`＝**全部**檔案都認不得才出聲；而切帳號偵測本來就要靠
        # **跨帳號比對**才成立，少掉一邊等於這條路徑實質失效，回傳值卻與「真的沒切過」同形。
        rows_here = ok_here = 0
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                h["rows"] += 1
                rows_here += 1
                try:
                    o = json.loads(line)
                except Exception:
                    h["bad_rows"] += 1
                    continue
                sid = o.get("sessionId") if isinstance(o, dict) else None
                ms = _history_epoch_ms(o.get("timestamp")) if isinstance(o, dict) else None
                if isinstance(sid, str) and sid and ms is not None:
                    by_sid.setdefault(sid, []).append((ms / 1000.0, label))
                    ok_here += 1
                else:
                    h["bad_rows"] += 1
        if rows_here and not ok_here:
            h["files_blank"] += 1          # 有列、卻一列都認不得＝這一份的格式漂了
    out = {}
    for sid, rows in by_sid.items():
        # 同一個 timestamp 出現在多個帳號＝**同一列被複製到另一個 config**（手動跨機同步、備份
        # 還原、config 目錄複製都會這樣），不是真的換帳號。這種列無法歸屬給任一帳號，整組排除
        # ——寧可漏報，也不要把不是使用者造成的事算到他頭上。
        accs_at = {}
        for t, acc in rows:
            accs_at.setdefault(t, set()).add(acc)
        if any(len(a) > 1 for a in accs_at.values()):
            # ⚠ 判準在 **sessionId 層級**，不是逐列：只要有**任何**一列的時刻同時出現在兩個
            # 帳號，就代表這兩份 history 之間存在複製關係，這個 sid 的所有列都不可歸屬。
            # 只丟撞到的那幾列的話，「兩份曾經相同、之後其中一邊被裁切／輪替」會留下乾淨的
            # 「舊的只在 A、新的只在 B」——與真正切帳號完全同形，於是生出一個假標記、
            # 把不是使用者做的事算到他頭上。寧可漏報。
            continue
        clean = sorted(set(rows))
        marks = []
        prev_acc = None
        for t, acc in clean:
            if prev_acc is not None and acc != prev_acc:
                # 新帳號的第一個 prompt＝切換已經發生。**保留次秒精度**：history 的時刻本來就有
                # 毫秒，截成整秒會讓「同一秒內、但比切換更早」的那一則被判成在切換之後。
                # ⚠ 一律正規化到**毫秒**（history 的原生精度）：顯示定位用這個值，
                # `session_signature` 也用同一個值（`%.3f`）。兩邊必須同精度——留著比毫秒更細的
                # 位數，指紋就會把「落在同一回合兩側」的兩個標記寫成同一個字串，增量建置於是
                # 沿用畫錯位置的舊頁。歸因那條線用的是同一個值：`collect_cache_steps` 收下的
                # acct 事件保留同樣的毫秒精度（步驟時刻那一側仍是整秒）。
                marks.append(round(t, 3))
            prev_acc = acc
        if marks:
            out[sid] = marks
    h["accounts"] = len({acc for rows in by_sid.values() for _, acc in rows})
    h["switch_sessions"] = len(out)
    if h["files"] and not by_sid:
        # 檔案讀到了、卻一列都認不得＝上游列格式換過。這種「靜默歸零」與「真的沒切過」在
        # 回傳值上完全同形，不出聲就只能等人肉眼發現。
        print(f"  ! 切帳號偵測：讀了 {h['files']} 個 history.jsonl／{h['rows']} 列，"
              f"認得的列 0 筆——列格式可能變了，切帳號歸因這一輪等於沒有作用", file=sys.stderr)
    elif h["files_blank"]:
        # (v36-fam6 #5) **部分**檔案認不得。上面那條只在全部都認不得時出聲，而切帳號是
        # 靠**跨帳號比對**成立的：少掉一邊，這條路徑就實質失效，而回傳值仍與「真的沒切過」同形。
        print(f"  ! 切帳號偵測：{h['files']} 份 history 裡有 {h['files_blank']} 份有列、"
              f"卻一列都認不得——那幾個帳號的切換偵測不到，跨帳號比對會少一邊；"
              f"目前仍辨識出 {h['accounts']} 個帳號", file=sys.stderr)
    if isinstance(health, dict):
        health.update(h)
    return out


def collect_cache_steps(s, acct_switches=None):
    """主對話每次 assistant API 呼叫的時間序列與快取邊界事件，供 cache-report 分析。回傳 (steps, models, events)：
      steps  = [[epoch, cache_read, 脈絡tokens, 寫入總量, 寫入5分, 寫入1h, 模型idx, 自報成因碼, 重算tokens], …]
               （依時間排序、message.id 去重；寫入5分/1h 來自 usage.cache_creation 細分，舊資料無細分為 0；
               模型idx 指向 models，未知 -1；自報成因碼＝API_MISS_CODES 索引、0＝無此資料、
               API_MISS_UNKNOWN_CODE 為未知新型別哨兵，
               重算tokens＝diagnostics 附的 cache_missed_input_tokens、無則 0）
      models = steps 用到的模型字串表（去重存一次，免每步重複存字串）
      events = [[epoch, kind], …]，kind ∈ "limit"(429)／"auth"(401)／"compact"——快取邊界標記，
               報告據此把其後第一步歸因為切帳號/登入/壓縮，而非 TTL 失效。
    存原始 cache_read（非預先四捨五入的命中率），冷啟判定才能精確、不會在門檻邊界因進位而誤分類。
    只取主對話：子代理有獨立的快取前綴，混進來會污染間隔判讀。
    Codex 略過：非因序列問題（usage 已按呼叫掛在步驟起點），而是本報告的 TTL 細分、計價與成因模型
    是 Anthropic 專屬——OpenAI 自動快取無 cache_creation/TTL 資料可對應。"""
    if s.source_kind == SOURCE_CODEX:
        return [], [], [], []
    steps, seen = [], set()
    step_mids = []                 # 與 steps 等長：每一步的 message.id（無則 None），供成因 join
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
        reason, mtok = _miss_reason(msg)
        code = (API_MISS_CODES.index(reason) if reason in API_MISS_CODES
                else API_MISS_UNKNOWN_CODE if reason else 0)   # 未知的新成因：存哨兵碼，次數/長度不吞
        steps.append([int(dt.timestamp()), c2, total_in, c1, c5, c1h, midx.get(mdl, -1), code, mtok])
        step_mids.append(mid)
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
    # 自願切帳號（沒撞 limit）在 transcript 裡沒有任何痕跡，靠外部的 history.jsonl 帶進來。
    # ⚠ **保留次秒精度**（history 的原生精度是毫秒）。步驟時刻是整秒，而消費事件的規則是
    # 「不晚於前一步就當成已過期、跳過」——把切換截成整秒，會讓「與前一步同秒、實際上卻更晚」
    # 的切換符合那條規則而被整組丟掉，該步的成因就從 acct 掉成 intra／evict。
    # ⚠ 步驟那一側刻意維持整秒：`cache_steps` 存進 manifest，改它的型別等於改存檔格式。
    # **範圍限制 SCOPE-ACCT-EVENT-SUBSEC**：兩側精度不同的涵蓋範圍寫在 `classify_cache_causes`
    # 消費事件那一段；詳見 planning/scope-limits.md 的 SCOPE-ACCT-EVENT-SUBSEC。
    for t in (acct_switches or {}).get(s.session_id, ()):
        events.append([float(t), "acct"])
    events.sort()
    # step_mids 刻意**不放進 steps 裡面**：steps 會被存進 manifest（增量建置沿用），改動它的形狀
    # 等於讓所有既有 manifest 失效。id 只有「掛徽章」這條路徑要用，而那條路徑不走 manifest。
    return steps, models, events, step_mids


def collect_codex_steps(s):
    """Codex 每次 API 呼叫的 [epoch, cache_read, 脈絡] 序列，供 Codex 存活統計頁（cache-codex）使用。
    與 Claude 的 cache_steps 刻意分開存：cache_steps 是 Anthropic TTL 報告的原料，兩家 TTL 機制不同，
    不得混入同一組統計。步驟＝載入器把 token_count 掛在呼叫首事件上的 usage（見 load_codex_session）；
    Codex 事件 id 逐筆合成且 usage 每呼叫恰掛一筆，故不需 message.id 去重。"""
    if s.source_kind != SOURCE_CODEX:
        return []
    steps = []
    for e in sorted((e for e in s.events if e.get("type") == "assistant"), key=ts_key):
        msg = e.get("message") or {}
        u = msg.get("usage") or {}
        if not isinstance(u, dict):
            continue
        c2 = usage_int(u, "cache_read_input_tokens")
        total_in = usage_int(u, "input_tokens") + c2
        dt = e.get("_dt")
        if total_in <= 0 or not dt:
            continue
        steps.append([int(dt.timestamp()), c2, total_in])
    return steps


def _step_cold(st):
    """cache_steps 的一步是否冷啟：cache_read / 脈絡 < CACHE_COLD_PCT%。
    用整數交叉相乘精確比較（cr*100 < 門檻*脈絡），避免先四捨五入命中率而在門檻邊界誤判。"""
    return st[1] * 100 < CACHE_COLD_PCT * st[2]


def _resets_lineage(api_cause, srv_cause, boundary, acct):
    """這一對之後，快取 lineage（`last_write`＝最近一次寫入的 TTL）要不要打掉重來。

    `classify_cache_causes` 與 `build_cache_report` 是刻意的雙胞胎，**必須用同一份判準**：
    兩邊各寫一份的話，同一步會在報告端與逐步徽章端落進不同的 cohort（一邊 `expiry` 記人因錢、
    一邊 `evict` 不記錢），同一頁上兩個地方對同一步講相反的話。這正是 `v36-fam3` F1 抓到的分岔
    ——`srv_cause` 只有報告端算進去。

    四個入口都代表「邊界前那次寫入的 TTL 對後續步不再適用」：
      - `api_cause`：API 自報前綴被改掉（工具／系統提示／前文／換模型）——前綴已經不是同一個。
      - `srv_cause`：伺服器側自報不可用——那次讀不到，舊 cohort 證明不了下一步該不該命中。
      - `boundary`：結構性邊界（limit/auth 切帳號、換模型、壓縮）——全新前綴。
      - `acct`：自行切帳號——同樣是全新前綴。
        ⚠ 判準是「這個窗裡有沒有切帳號」，**不是** `acct_kills_live`（切換有沒有落在 TTL 之前）。
        後者回答的是另一個問題——「這一對能不能當存活樣本」——拿來當 lineage 判準會讓
        「隔夜才切帳號」那種對保留舊 cohort，而前綴其實早就換了。反事實那側
        （`cf_last_write`）本來就是用「窗裡有沒有 acct」，三處統一到這一份之後才真的同進同出。

    重建交給迴圈頂端：下一輪 `prev` 就是本步，它若真有寫入自然會把 lineage 立回來。"""
    return bool(api_cause or srv_cause or boundary or acct)


def _masked_human(won, evidence, kinds, gap, ttl_bound):
    """實據成因勝出時，**同一步仍然成立**的人因條件（自行切帳號／閒置過期）。

    實據勝過推論是對的，但使用者造成的事不可以因此消失：這裡把它記下來，讓顯示層兩個都標。
    成因欄仍只有一個、錢也只算一次。`won` 不是實據時回空 list（推論本身已經選了最該說的那個）。
    `classify_cache_causes` 與 `build_cache_report` 共用這一份，免得兩處分岔。

    ⚠ `evidence` 要**列出每一個會蓋過人因條件的直接證據**——API 自報、伺服器側自報，
    以及結構性邊界（limit/auth 切帳號、換模型、壓縮）。少列一個入口，同一件事就會從那個
    入口溜過去：那一步照樣不算人因（對的），但「它其實也閒置超過 TTL／也換了帳號」
    在畫面上完全看不到（不對）。"""
    if not won or won not in evidence:
        return []
    # ⚠ `acct`（自行切帳號）在本檔是**專有名詞**：「沒撞 limit 就換帳號」。同一區間若有
    # limit/auth，那次換帳號是被迫的，標成「自行」就是把不是使用者選的事算到他頭上——
    # 與這個函式要防的那件事同等嚴重，只是方向相反。
    forced = bool({"limit", "auth"} & set(kinds))
    return [c for c, ok in (("acct", "acct" in kinds and not forced),
                            ("expiry", gap >= ttl_bound)) if ok]


def classify_cache_causes(raw, models, events, masked=None):
    """給一個 Claude session 的 cache_steps（raw）、模型表與邊界事件，回傳 {epoch: 冷啟成因}（只收冷啟步）。

    `masked`：呼叫端可傳入一個 dict 收集「實據成因勝出、但同一步仍成立的人因條件」
    → {key: [成因鍵, …]}。刻意用「傳入被填寫」而不是改回傳值——回傳值有多個呼叫端與測試在用，
    形狀不動。用途見 `_cold_cause_label`（徽章）與 `_masked_human_note`（報告）：
    記帳只認勝出的那一個，顯示要兩個都標。
    成因鍵與 REPORT_CAUSES 一致：first/switch/model/tools/system/msgs/compact/expiry/evict/intra，
    供逐步／回合徽章著色（_cold_display 據此決定紅標或中性）。這是 build_cache_report 逐步成因判定的
    獨立、無副作用複本：報告端在同一迴圈裡另做分桶/統計，兩邊共用同一組門檻常數，且 test_smoke 有
    一致性測試釘住以防漂移。
    與報告一致地只看 ctx ≥ REPORT_MIN_CTX 的步（暖機/瑣碎呼叫不歸因）；『first』僅限原始第 0 步且冷啟。
    **API 自報成因（diagnostics.cache_miss_reason）優先於一切推論**——那是實據；只有沒有這欄的
    資料（舊版 CLI）或 API 未給成因時，才退回「邊界/間隔」推論。"""
    causes = {}
    # 每個原始步的 join 鍵先算好（同秒的第 n 次呼叫）。**必須數過 raw 的每一步**、不能只數
    # ctx 過濾後的，否則與消費端（analyze 走全部 _step）的計數對不上、鍵會錯位。
    _seen, ckeys = {}, []
    for st in raw:
        n = _seen.get(st[0], 0)
        ckeys.append(_cause_key(st[0], n))
        _seen[st[0]] = n + 1
    steps = [(i, st) for i, st in enumerate(raw) if st[2] >= REPORT_MIN_CTX]
    if not steps:
        return causes
    if steps[0][0] == 0 and _step_cold(steps[0][1]):   # 原始第 0 步＝session 第一句
        # 首步也要「有自報就以自報為準」：快取跨 session 共用，新 session 的第一句仍可能命中上一場
        # 留下的前綴 → 此時 tools/system/msgs 才是真成因。標成 first（「不可避免：全新前綴」）
        # 會把可避免的成因塞進不可避免桶，還與旁邊的 ⚠ 徽章自打（同一步兩種說法）。
        # 直接證據**兩個 dict 都要查**：只查前綴變動那個的話，首步的 `unavailable`／未知型別
        # 會被 `first` 蓋掉——成因表寫「session 第一句」、API 自報表寫「伺服器不可用」，同一步兩種說法。
        fc = (API_MISS_CAUSE.get(_step_miss(steps[0][1])[0])
              or API_OTHER_CAUSE.get(_step_miss(steps[0][1])[0]))
        if fc == "msgs" and any(k == "compact" and t <= steps[0][1][0] for t, k in events):
            fc = "compact"                             # 同下方配對迴圈：壓縮邊界比通稱 msgs 更準
        causes[ckeys[0]] = fc or "first"      # steps[0][0] == 0 已保證這是原始第 0 步
    ei, n_ev = 0, len(events)
    # (v36-fam5 #1) 這一場出現過 limit/auth 的時刻，供「人因勝出但可能是被迫」的並列顯示用。
    # ⚠ 刻意**不**用上面那個會前進的事件游標：游標會先跳過第一步之前的事件（那些永遠進不了
    # 任何一對的 `kinds`），而「同場稍早撞過 limit」問的是整條時間軸，不是某一對的窗。
    limit_ts = sorted(float(t) for t, k in events if k in ("limit", "auth"))
    last_write = None      # 最近一次有寫入的可分析步之 TTL（"1h"/"5m"）——cur 讀的快取以此為準
    for (ip, prev), (ic, cur) in zip(steps, steps[1:]):
        if prev[5] > 0:
            last_write = "1h"
        elif prev[4] > 0:
            last_write = "5m"
        gap = cur[0] - prev[0]
        if gap < 0:            # 時序異常（跨機同步等）——比照報告略過
            continue
        # ⚠ 事件與步驟的時間精度不同：步驟時刻是整秒（存進 manifest 的型別），acct 事件保留
        # 毫秒。兩側同秒時，這條比較把事件視為落在該步**之後**——涵蓋到「切換與前一步同秒、
        # 但實際更晚」為止；反向那一格（切換其實早於前一步、只是同一秒）仍會歸給這一對。
        # **範圍限制 SCOPE-ACCT-EVENT-SUBSEC**，詳見 planning/scope-limits.md。
        while ei < n_ev and events[ei][0] <= prev[0]:
            ei += 1
        kinds = set()
        j = ei
        # ⚠ (v36-fam6 #4) 這個上界還有**另一個方向**沒被上面那段講到：切換落在當前這一步
        #   自己那一秒（步在 T+300、切換在 T+300.6）時，它會被算給 (prev, cur) 這一對並**記錢**，
        #   而 `ei = j` 又把標記消費掉，真正在切換之後的那一步反而拿不到。實測三種次秒位置
        #   （T+299.4／T+300.0／T+300.6）結果完全相同 → tie-break 一律往「算在使用者頭上」倒。
        #   **行為刻意不動**（本機沒有正例可驗、且動到金額），詳見 scope-limits.md 的
        #   SCOPE-ACCT-EVENT-SUBSEC〈2026-08-22 補上另一個方向〉那一列。
        # ⚠ 上界用**截到整秒**的事件時刻比：步驟時刻本身就是整秒，落在「當前這一步同一秒」的
        # 切換若照浮點比就會超出上界、被推到下一對去，該步的成因跟著掉。
        # 下界那條刻意維持浮點（見上面的說明）——兩條各自對齊它要比的那一端。
        while j < n_ev and int(events[j][0]) <= cur[0]:
            kinds.add(events[j][1])
            j += 1
        # ⚠ **消費過的事件不可以再被下一對看到。** 兩條界線精度不同（下界浮點、上界截整秒），
        # 於是 `floor(切換時刻)` 剛好等於某個步驟 epoch 的標記會同時滿足「這一對的上界」與
        # 「下一對的下界」——一次切換被記成兩次人因浪費、金額翻倍。把游標推過去才是一次性。
        ei = j
        boundary = ("switch" if ("limit" in kinds or "auth" in kinds) else
                    "model" if (prev[6] >= 0 and cur[6] >= 0 and prev[6] != cur[6]) else
                    "compact" if "compact" in kinds else None)
        api_cause = API_MISS_CAUSE.get(_step_miss(cur)[0])     # 實據優先
        # 同一步的非前綴變動實據。**不向前借**（伺服器側是同一步的狀態，借給後一步就變成推論了）；
        # 樣本排除由報告端負責（`srv_excluded`），本函式只定成因。
        srv_cause = API_OTHER_CAUSE.get(_step_miss(cur)[0])
        if not api_cause:
            # 兩個觀測步之間夾著被 REPORT_MIN_CTX 濾掉的「前綴變動」步時，這一對照樣被污染——
            # 前綴在中間就被改掉了。不看的話結構性實據會被分析門檻吃掉，下游冷啟被誤判成 evict
            # （實測 ctx 差 1 個 token 結論就翻轉）。ctx 門檻只該管「由命中率推論」，不管前綴事實。
            # **範圍限制 SCOPE-FILTERED-STEP-CAUSE**：借來的成因掛在**後一步**上，而它描述的是
            # 中間那一步。兩步的成因不同時，後一步顯示的是中間步的成因。詳見
            # planning/scope-limits.md 的 SCOPE-FILTERED-STEP-CAUSE。
            api_cause = next((c for r in raw[ip + 1:ic]
                              if (c := API_MISS_CAUSE.get(_step_miss(r)[0]))), None)
        if api_cause == "msgs" and boundary == "compact":
            # 壓縮就是「前文被改寫」的具體成因：通稱 msgs 會蓋掉更準的邊界實據（compact 是我們自己
            # 記錄到的事件），還會把它從「不可避免」挪進「習慣可避免」——自動壓縮不是改習慣能省的。
            api_cause = "compact"
        if _step_cold(cur):
            cohort = last_write or "unknown"
            ttl_bound = REPORT_TTL_SAFE_SEC if cohort == "1h" else 5 * 60
            if api_cause:
                causes[ckeys[ic]] = api_cause
            elif boundary:
                causes[ckeys[ic]] = boundary
            elif srv_cause:
                # 伺服器側自報排在結構性邊界之後、間隔推論之前：邊界同樣是直接證據且更早存在，
                # 而 expiry／acct／evict／intra 都是由間隔推論來的，讓推論蓋過同一步的自報是反的。
                causes[ckeys[ic]] = srv_cause
            elif gap >= ttl_bound:
                causes[ckeys[ic]] = "expiry"
            elif "acct" in kinds:
                # 帳號邊界是**直接證據**（history.jsonl 記著那一刻換了帳號），intra／evict 是由
                # 間隔推論出來的。快取此時本來還活著（沒過 TTL），冷啟的原因就是換了組織 → 直接
                # 證據勝過推論，**不分間隔長短**。
                # ⚠ 仍然讓位給更強的三種：API 自報、被迫切換/換模型/壓縮（boundary）、已過 TTL
                # （expiry —— 那時快取本來就會死，切帳號不是綁定成因）。三者都在上面先攔下。
                # 少了這一格，2 分鐘內的切換會被 intra 吃掉，人因浪費在索引與報告上一致地消失。
                causes[ckeys[ic]] = "acct"
            elif gap >= REPORT_INTRA_SEC:
                causes[ckeys[ic]] = "evict"
            else:
                causes[ckeys[ic]] = "intra"
            if masked is not None:
                also = _masked_human(causes[ckeys[ic]], (api_cause, srv_cause, boundary),
                                     kinds, gap, ttl_bound)
                # (v36-fam5 #1) 反方向的並列：**人因勝出、但結構性條件也可能成立**。
                # `boundary` 的「被迫/自願」只看**同一對相鄰步之間**的事件，429 與切換中間夾了
                # 任何一次 API 呼叫，那次被迫切換就會落成 `acct`（人因、記錢、紅色 🔥）。
                # 全語料實測：10 個 `acct` 標籤裡有 3 個同場稍早出現過 limit/auth
                # （間隔 5005／4942／11584 秒），涉及 $9.05 ＝ 人因浪費 $299.20 的 3.03%。
                # ⚠ **只並列顯示、不動記帳**（Will 2026-08-21 裁決）：那三筆的間隔都以小時計，
                #   工具判不出是不是同一件事；挑一個衰減窗把它們改判成被迫，會反過來把自願切換
                #   洗白，而那個方向量不到。兩個都標，讓讀的人自己判斷。
                # 「稍早」＝落在這一對的窗**之前**（`t <= prev[0]`）。窗內的那些已經進了 `kinds`，
                # 會讓 boundary 直接判成 `switch`，走不到這裡，不會重複標。
                limit_before = bool(limit_ts) and limit_ts[0] <= prev[0]
                if causes[ckeys[ic]] == "acct" and limit_before:
                    also = list(also) + ["limit_earlier"]
                if also:
                    masked[ckeys[ic]] = also
        # 與 build_cache_report 共用 `_resets_lineage`（判準與理由都在那支的 docstring）：
        # 不重置的話，下一個冷啟會被算進舊 cohort、標成「1h 內卻冷啟」的假 evict。
        if _resets_lineage(api_cause, srv_cause, boundary, "acct" in kinds):
            last_write = None
    return causes


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
    """回合內命中率最低的步驟（從 _step 標記算）→ (pct, idx, cause, cold, masked)；無逐步資料回
    (None, None, None, False, None)。`masked` 是同一步仍成立、但沒搶到成因欄的條件（見
    `_cold_cause_label`）——它與 cause 必須一起帶走，分開取就會標到別步的條件上。冷啟動不一定在首步，也不一定要長間隔：可能是 resume／長閒置後 TTL
    過期，也可能是回合內快取斷點的一次性 miss（實測同一晚間隔 18s 的中途步驟也會 0%）。成因由
    classify_cache_causes 掛在 b["cause"]（未歸因為 None）；取「最低的那一步」並帶回其成因與
    是否冷啟供徽章判定紅標／中性。"""
    best_pct, best_idx, best_cause, best_cold, best_masked = None, None, None, False, None
    best_key = None            # 選「最冷那步」用精確比值，不用四捨五入後的 pct——否則 25.0% 與 24.6%
    for b in group.get("blocks", []):   # 同 round 成 25 時會選錯步、把真正最冷（該顯示 ❄）的那步藏掉
        if b.get("type") != "_step":
            continue
        su = b.get("u")
        if not su or su["total_in"] <= 0:
            continue
        p = _pct_display(su["cache_read"], su["total_in"])
        key = su["cache_read"] * 1_000_000 // su["total_in"]
        if best_key is None or key < best_key:
            # 顯示走 _pct_display（踩門檻時給一位小數），選步／冷熱判定用整數比值（同 _step_cold）。
            best_key = key
            best_pct, best_idx = p, b.get("idx")
            bc = b.get("cause")                       # "" ＝已分析非失效（灰）、None ＝未分析軸（紅）——
            best_cause = bc if bc else (API_MISS_CAUSE.get(su.get("miss") or "") or bc)  # 別讓 or 把 "" 洗成 None
            best_cold = su["cache_read"] * 100 < CACHE_COLD_PCT * su["total_in"]
            best_masked = b.get("cause_masked")    # 與 cause 同一步，兩者要一起帶走
    return best_pct, best_idx, best_cause, best_cold, best_masked


def turn_cold_class(group):
    """整段彙總 ⚡ 的冷啟顯示類別：''（非冷啟）／'cold'（醒目）／'coldx'（中性灰）。
    紅：回合內含真失效步（expiry/evict），或含未分析來源的冷啟步（cause None＝Codex／子代理，維持原標示）；
    灰：Claude 主對話僅結構性/暖機冷啟（首呼叫、回合內斷點等）——避免把「本來就不會命中」誤讀成失效。"""
    u = group.get("u")
    total_in = u["input"] + u["cache_create"] + u["cache_read"]
    # 冷熱判定用整數交叉相乘（與 _step_cold／render_step_meters／coldest_step 同一判準），不先四捨五入：
    # 否則彙總落在 24.5〜24.99% 時整段 ⚡ 不著色，卻同時掛著已著色的 ❄ 與逐步 ⚡，自相矛盾。
    if not total_in or u["cache_read"] * 100 >= CACHE_COLD_PCT * total_in:
        return ""
    genuine = unclassified = False
    for b in group.get("blocks", []):
        if b.get("type") != "_step":
            continue
        su = b.get("u")
        if not su or su["total_in"] <= 0:
            continue
        if su["cache_read"] * 100 < CACHE_COLD_PCT * su["total_in"]:   # 該步冷啟
            c = b.get("cause")                        # 保留 ""（已分析非失效）與 None（未分析軸）的區別
            c = c if c else (API_MISS_CAUSE.get(su.get("miss") or "") or c)
            if c in _COLD_GENUINE:
                genuine = True
            elif c is None:
                unclassified = True
    return "cold" if (genuine or unclassified) else "coldx"


def _breakdown_meters(inp, cw, cr):
    """可勾選的 token 細分徽章（預設隱藏，見 meterbar）：新輸入 / 快取寫入 / 快取讀取。
    整段彙總與逐步分隔列共用，讓「只有一個下載量」攤成四種 token 的來源。"""
    return (f'<span class="meter m-in" title="全新輸入 tokens（未快取）">新輸入 {fmt_tokens(inp)}</span>'
            f'<span class="meter m-cw" title="快取寫入 tokens（建立快取）">寫入 {fmt_tokens(cw)}</span>'
            f'<span class="meter m-cr" title="快取讀取 tokens（命中量）">讀取 {fmt_tokens(cr)}</span>')


def _ctx_meter(total_in, win, prefix=""):
    """脈絡徽章；知道該模型 context 視窗時附「佔視窗 %」——這是 statusline 的 ctx% 同義值，
    看得出離自動壓縮還有多遠（壓縮會改寫前綴、整段快取重來）。"""
    pct = f" ({round(100 * total_in / win)}%)" if (win and total_in) else ""
    tip = "脈絡 tokens（input+cache）" + (f"；佔 {fmt_tokens(win)} context 視窗" if win else "")
    return f'<span class="meter m-ctx" title="{esc_attr(tip)}">{prefix}ctx {fmt_tokens(total_in)}{pct}</span>'


def _miss_meter(reason, mtok, usd, partial=False, unpriced=False):
    """API 自報的快取失效徽章（diagnostics.cache_miss_reason）——實據，不是推論。
    整段沒命中與「命中率仍高、只掉一段」的部分失效都會出現；後者標成中性的「部分失效」，
    因為它不是快取整個掉了（⚡% 上完全看不出來，只有這裡看得到）。
    `unpriced`＝未知模型算不出金額 → 標 ?（同回合／session 表頭／②-b）；整段省略會被讀成「沒多付」。"""
    if not reason:
        return ""
    extra = ""
    if mtok:
        money = f"（多付 ~{esc(cost_label(usd or 0.0, unpriced))}）" if (usd or unpriced) else ""
        extra = f" · 失效前綴 {fmt_tokens(mtok)}{money}"
    note = API_MISS_NOTES.get(reason, "Claude 自報的未命中成因")
    cls, tag = ("m-miss part", "部分失效·") if partial else ("m-miss", "")
    tip = ("Claude API 自報：" + note
           + ("（此步命中率仍在門檻之上，只掉一段）" if partial else "")
           + "。「失效前綴」＝從多久以前的內容開始對不上（API 回報值），"
             "不等於這次重寫量；金額以本步實際寫入量為上限估算")
    return (f'<span class="meter {cls}" title="{esc_attr(tip)}">'
            f'⚠{tag}{esc(miss_label(reason))}{extra}</span>')


def _extra_meters(u, gap=None):
    """可勾選的補充徽章：距上一次 API 呼叫多久（TTL 是按「距上次使用」在算）、該步 effort 等級
    （切 effort 會改變請求前綴 → 影響快取）。無資料的欄位不輸出。"""
    out = ""
    if gap is not None:
        out += (f'<span class="meter m-gap" title="距上一次 API 呼叫（快取 TTL 以距上次使用計）">'
                f'距上一步 {fmt_dur(gap)}</span>')
    if u and u.get("effort"):
        out += (f'<span class="meter m-eff" title="這次呼叫的推理強度（effort）；中途改 effort 會變動請求、影響快取">'
                f'effort {esc(u["effort"])}</span>')
    return out


def _miss_items(s):
    """session 內 API 自報失效的 (成因標籤, 次數) 清單，多的在前。"""
    kinds = getattr(s, "miss_kinds", None) or {}
    return [(miss_label(r), n) for r, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))]


def _miss_head(s):
    """session 表頭「快取被打斷」摘要的文字（HTML/MD 共用）；沒有自報成因回 ""。"""
    items = _miss_items(s)
    if not items:
        return "", ""
    n = sum(c for _, c in items)
    cold = getattr(s, "miss_cold", 0)
    detail = "、".join(f"{lbl} {c}" for lbl, c in items)
    tok = getattr(s, "miss_tok", 0)
    usd = getattr(s, "miss_usd", 0.0)
    upart = getattr(s, "miss_usd_partial", False)     # 混到未知模型時要標 ?／+?，不可靜默低估
    cost = (f"，失效前綴合計 {fmt_tokens(tok)}"
            + (f"（多付 ~{cost_label(usd, upart)}）" if (usd or upart) else "")) if tok else ""
    head = (f"⚠ 快取被打斷 {n} 次" if cold else f"⚠ 部分失效 {n} 次")
    if cold and cold < n:
        head += f"（整段 {cold}、只掉一段 {n - cold}）"
    return f"{head}（{detail}）{cost}", "Claude API 自報的成因；「只掉一段」在 ⚡% 上看不出來"


def _miss_summary_html(s):
    """session 表頭的「快取被打斷」摘要——Claude API 自報，含命中率仍高的部分失效。"""
    text, tip = _miss_head(s)
    if not text:
        return ""
    return f' · <span class="warn" title="{esc_attr(tip)}">{esc(text)}</span>'


def _lead_step(group):
    """回合內第一個 _step 的 (usage, gap)；沒有則 (None, None)。
    「距上一步」在回合層＝該回合**第一次**呼叫距上次呼叫多久（多步回合的後續步另有逐步列可看）。"""
    for b in group.get("blocks", []):
        if b.get("type") == "_step":
            return b.get("u"), b.get("gap")
    return None, None


def _group_window(group):
    """該回合 **ctx_max 那一步**所屬模型的 context 視窗；查不到回 None（只顯示絕對值）。
    ⚠ 不可改回「取回合內第一個知道視窗的步驟」：同一顯示回合可能併了不同模型的呼叫
    （如 opus 1M 視窗 → haiku 200k 視窗），拿第一步的視窗去除以另一步的 ctx_max，回合徽章會與
    逐步徽章對同一事實給出兩個百分比（實測 19% vs 95%）。"""
    u = group.get("u") or {}
    if "ctx_win" in u:                 # 正常路徑（_acc_turn_usage 記的）——查不到就是 None，
        return u["ctx_win"] or None    # 不借別步的視窗頂替（借了就是製造兩處說法不一）
    best_win, best_ctx = None, -1      # 手造 group 的退路：仍取 ctx 最大那步的視窗，規則一致
    for b in group.get("blocks", []):
        su = b.get("u") if b.get("type") == "_step" else None
        if su and (su.get("total_in") or 0) > best_ctx:
            best_ctx, best_win = su.get("total_in") or 0, su.get("win")
    return best_win or None


def render_turn_meters(group):
    """assistant 回合的小徽章：快取% / 花費 / (可勾) 新輸入·寫入·讀取 / 脈絡 / 產出。
    冷啟著色分兩級：真失效（閒置過期/異常逐出）紅標、結構性或未歸因（首呼叫/回合內斷點/換模型…）中性灰，
    避免把「本來就不會命中」誤讀成快取失效；多步回合另以 ❄ 揭露被彙總藏住的最冷步，同樣依成因決定紅或灰。
    **單步回合**不畫逐步列也不畫 ❄，成因改由這裡的 ⚡ 徽章直接帶（v36-fam3 F2）。"""
    u = group.get("u")
    if group.get("role") != "assistant" or not u:
        return ""
    total_in = u["input"] + u["cache_create"] + u["cache_read"]
    if total_in <= 0 and u["output"] <= 0:
        return ""
    pct = _pct_display(u["cache_read"], total_in)
    cold = ""
    ctitle = "快取命中率（整段彙總）"
    ctag = ""
    kind = turn_cold_class(group)
    if kind:
        cold = " " + kind
        ctitle = ("整段命中率低，且含真正的快取失效（閒置過期或異常提早失效）" if kind == "cold"
                  else "整段命中率低，但屬結構性/預期內（首呼叫、回合內斷點等）——非快取失效")
        if group.get("n_steps", 0) <= 1:
            # (v36-fam3 F2) 單步回合**兩者都不畫**：沒有逐步分隔列（下面刻意省略），也不符合
            # 下方 ❄ 的 `n_steps >= 2` → 成因與並列標籤在頁面上一個字都看不到。而
            # `_masked_human_note`／`_srv_excluded_note` 對讀者寫的正是「逐步徽章會兩個都標」，
            # 那句話在這種回合上是假的——本機有**一成左右**的並列步落在單步回合，
            # 那些步在頁面上一個字都看不到。
            # ⚠ 這裡刻意不寫死對數，理由與 `:294-296` 同一條：對數隨語料與量法變動，
            #   而這一行原本就寫錯過一次（抄了未去重的數字，是實際值的四倍）。
            # 這裡讓整段徽章直接吃該步的 cause／cause_masked，規則與下方多步的 ❄ 完全一致：
            # 成因平時只進 title（版面已經很擠），「同一步還成立別的條件」才攤到版面上。
            _mp, _mi, mc, mcold, mmask = coldest_step(group)
            if _mp is not None and mcold:
                # ⚠ 上面那句（紅/灰兩級的判讀：是不是真失效）**照留**，成因接在後面。
                # 整句換掉會把既有資訊換走——那是回歸，不是修正（test_cold_cause_badges 守著它）。
                if mmask:
                    # (v36-fam4 #2) **但有並列人因條件時它會與後半句打架**：`turn_cold_class`
                    # 只看勝出的那個成因（`unavail` 不在 `_COLD_GENUINE` → 灰、寫「非快取失效」），
                    # 而後半句正好在說「閒置過期，快取已過期（可避免）」——同一個 tooltip
                    # 兩句話互相否定。這種步改用不預判的措辭，讓成因自己講。
                    # ⚠ 只動文字、**不動著色**：`turn_cold_class`／`_cold_display` 要不要把並列
                    #   條件納入顏色判斷是另一個問題（會改全語料所有並列步的顏色），
                    #   Will 2026-08-21 選擇不在這條裡一起動。
                    ctitle = "整段命中率低"
                ctitle += "；" + _cold_cause_title(mc, mmask, "這一步的成因")
                ctag = f"·{_cold_cause_label(mc, mmask)}" if mmask else ""
    cost = cost_label(u["cost"], u["unpriced"])
    out = (f'<span class="meter m-cache{cold}" title="{esc_attr(ctitle)}">⚡{pct}%{esc(ctag)}</span>'
           f'<span class="meter m-cost" title="估算花費">~{esc(cost)}</span>'
           + _breakdown_meters(u["input"], u["cache_create"], u["cache_read"])
           + _ctx_meter(u["ctx_max"], _group_window(group)) +
           f'<span class="meter m-out" title="產出 tokens">↑{fmt_tokens(u["output"])}</span>')
    if group.get("dur_ms"):
        out += (f'<span class="meter m-dur" title="這個回合的實際耗時（你送出 → 答完，含工具執行）">'
                f'⏱{fmt_dur(group["dur_ms"] / 1000)}</span>')
    if group.get("n_steps", 0) <= 1:
        # 單步回合刻意不畫「步驟 1」分隔列（每一則都多一行、畫面很吵），但「距上一步／effort」
        # **只**掛在逐步列上 → 單次呼叫的回合這兩欄會完全看不到（而那是多數回合）。
        # 補在回合列尾端，順序與逐步列一致；沿用同一組 class，勾選開關自然生效。
        out += _extra_meters(*_lead_step(group))
    if u.get("miss"):
        # 回合彙總：把該回合各步的 API 自報失效收成一個徽章（多種成因就列出來，取最常見的在前）；
        # 全部都只掉一段時標「部分失效」中性色，不誇大成整段快取沒了。
        order = sorted(set(u["miss"]), key=lambda r: (-u["miss"].count(r), r))
        label = "、".join(miss_label(r) for r in order[:2]) + ("…" if len(order) > 2 else "")
        n = len(u["miss"])
        money = (f"（~{esc(cost_label(u['miss_usd'], u.get('miss_usd_partial')))}）"
                 if (u.get("miss_usd") or u.get("miss_usd_partial")) else "")
        tok = f" · 失效前綴 {fmt_tokens(u['miss_tok'])}{money}" if u.get("miss_tok") else ""
        tip = "；".join(API_MISS_NOTES.get(r, r) for r in order[:3])
        # 整段／只掉一段要照實拆（比照 _miss_head）：回合內只要有一步整段冷啟就把整個 ×N 都講成
        # 「整段快取沒了」是往嚴重方向誇大，與 session 表頭的「整段 a、只掉一段 b」自相矛盾。
        nc = u.get("miss_cold") or 0
        cls, tag = (("m-miss", "") if nc == n else
                    ("m-miss part", "部分失效·") if nc == 0 else
                    ("m-miss", f"整段{nc}·只掉一段{n - nc}·"))
        out = (f'<span class="meter {cls}" title="{esc_attr("Claude API 自報：" + tip)}">'
               f'⚠{tag}{esc(label)}×{n}{tok}</span>') + out
    if group.get("n_steps", 0) >= 2:
        mp, mi, mc, mcold, mmask = coldest_step(group)
        if mp is not None and mcold:
            title = _cold_cause_title(mc, mmask, "回合內命中率最低的步驟")
            # 成因平時只在 title 裡（版面已經很擠）；但「同一步還成立別的條件」是滑鼠不移上去
            # 就看不到的資訊，而它正是最容易被誤讀的那一格 → 只有這種步把標籤攤到版面上。
            tag = f"·{_cold_cause_label(mc, mmask)}" if mmask else ""
            out = (f'<span class="meter m-cache {_cold_display(mc)}" '
                   f'title="{esc_attr(title)}">'
                   f'❄最低 {mp}%·步驟{mi}{esc(tag)}</span>') + out
    return out


def block_anchor(kanchor, step_ep, n, in_step):
    """區塊層級錨點（第 4 期）：`k<回合時戳>[-tb<n>][-s<步epoch>]-b<n>`。不給就回 `""`。

    `in_step` 是**「有沒有走進某一步」**，不是 `step_ep is not None`——兩者不同，
    而混為一談會造出撞號：

    | 情境 | `in_step` | `step_ep` | 給什麼 |
    |---|---|---|---|
    | 第一個 `_step` 之前的區塊（user 回合全部如此）| False | None | `-b<n>`，序號**以回合為作用域** |
    | 步裡的區塊，該步有時戳 | True | int | `-s<epoch>-b<n>`，序號**以步為作用域** |
    | 步裡的區塊，該步**沒有**時戳 | True | None | **什麼都不給** |

    ⚠⚠ 最後那一列不可以「退回輪內序號」：那會把它丟進第一列同一個 `-b<n>` 命名空間，
    於是兩個不同的區塊拿到同一個錨點 ⇒ 書籤安靜指到另一塊。與 `durable_anchor()`
    拒絕 naive datetime 是同一條規則：**寧可沒有錨點，也不要給一個會指錯的**。
    實測 `_step.t` 缺漏 0/67876（`scripts/probe_turn_identity.py blocks`），所以這一列
    是防守用的，不是常態——但零誤報的路徑正是沒被執行的路徑，所以測試強制走它一次。

    ⚠ **為什麼序號要縮到「步」**：同一份實測量到一輪的可標記區塊數 Claude p99=101／
    max=286、Codex p99=129／max=412，**12.6%／34.4% 的回合超過 20 個區塊**——輪內序號的
    爆炸半徑等於整段對話。縮到步之後，一步底下的區塊數 p90 只有 2〜4。
    升級的兩個前提也是同一份實測給的：步時戳缺漏 0、同輪內撞號 0。

    ⚠ `-s` 用的是**原始 epoch 整數**，不是回合錨點那種可讀式 UTC。回合錨點要可讀是因為
    「失效時讀得出是哪一刻」；而區塊錨點失效時 `openSub()` 一定會退到回合，橫幅印的是
    **回合**的時刻，`-s` 這一段從來不會單獨拿給人看。省下來的是每個區塊 6 個字元。

    ⚠⚠ **負的 `step_ep` 一律不給**（`bookmarks-p4fix-codex` Medium）。理由不是
    「1970 年前不會發生」——是 **`-` 就是段落分隔符**，所以 `k…-s-500-b1` 對每一個
    用 `-` 切字串的消費者都是畸形：`bmGrammarOk()` 判它不合法（合法錨點被宣告未命中），
    `openSub()` 的 `lastIndexOf('-')` 退化階梯也會切在錯的地方。
    ⇒ 要修的是**產出端**：讓文法接受帶號數字，等於要求下游每個消費者各自處理負號。
    這與上表最後一列同一條規則：**寧可沒有錨點，也不要給一個會指錯的。**
    """
    if not kanchor:
        return ""
    if not in_step:
        return f"{kanchor}-b{n}"
    sa = step_anchor(kanchor, step_ep)
    return f"{sa}-b{n}" if sa else ""


def step_anchor(kanchor, step_ep):
    """一次 API 呼叫（「步」）的錨點 `k<回合時戳>[-tb<n>]-s<步epoch毫秒>`。不給就回 `""`。

    ⚠⚠ **`-s<ep>` 有兩個產出端**：步驟列自己的 `id`，以及區塊錨點的中段。
    **兩邊一律走這一支**，不可以各寫一份判斷（`bookmarks-p4fix-codex-r2` Low：
    第一版只在 `block_anchor()` 擋負值，於是同一個 group 裡區塊錨點正確消失、
    步驟列照樣輸出 `k…-s-900`——而那正是 `bmGrammarOk()` 會拒絕的字串，
    使用者點了只會得到「找不到」）。**同一條規則寫兩次就會分岔**，這條線為此付過三次。

    不給的兩種情況：
    - `step_ep is None`——那一步沒有時戳，或同輪內撞號被呼叫端歸零。
    - **`step_ep < 0`**——`-` 就是段落分隔符，帶號數字會讓每一個用 `-` 切字串的消費者
      （`bmGrammarOk()`、`openSub()` 的 `lastIndexOf('-')` 退化階梯）都切在錯的地方。

    守它的是 `test_bookmark_block_anchors` 第 7 節（純函式的四＋二條契約）與
    **第 7b 節（走 `render_turn_html()`，守的就是第二個產出端）**，各自帶正的對照組。
    """
    if not kanchor or step_ep is None or step_ep < 0:
        return ""
    return f"{kanchor}-s{step_ep}"


def turn_block_anchors(group):
    """這一輪每個 block 的區塊錨點，回一個**與 `group["blocks"]` 等長**的 list（沒有就是 `""`）。

    ⚠⚠ **產品碼與探針共用這一支，不可以各抄一份**（`bookmarks-p4-fam` Low ＋
    `bookmarks-p4-codex` Low，同一課付了兩次）：`scripts/probe_turn_identity.py` 的
    `blocks` 模式要統計「實際可標記的區塊有幾個」，而那個條件**不只是「渲染得出來」**——
    還要求該輪有 `kanchor`、所在步有 `tms`、且 `tms` 在同一輪內沒撞號。
    探針自己抄一份的話，統計出來的是「可渲染」而不是「可標記」，
    而那組數字**正是第 4 期的設計依據**。

    ⚠ `_step` 自己不是可標記區塊，對應的位置一律是 `""`。
    """
    blocks = group.get("blocks") or []
    kanc = group.get("kanchor") or ""
    role = group.get("role")
    # 同一輪內 `tms` 撞號的那些步，整步不給錨點（見 `_epoch_ms` 與 `block_anchor`）
    seen, dup = set(), set()
    for b in blocks:
        if b.get("type") != "_step":
            continue
        v = b.get("tms")
        if v is None:
            continue
        (dup if v in seen else seen).add(v)
    out = []
    in_step, step_ep, bn = False, None, 0
    for b in blocks:
        if b.get("type") == "_step":
            step_ep = b.get("tms")
            if step_ep in dup:
                step_ep = None
            in_step, bn = True, 0
            out.append("")
            continue
        # ⚠ 壓縮點那種回合 `render_turn_html` 提前 return，一個 `.blk` 都不產
        if group.get("compact") or not block_is_renderable(b, role):
            out.append("")
            continue
        bn += 1
        out.append(block_anchor(kanc, step_ep, bn, in_step))
    return out


def wrap_block(inner, anchor):
    """把一個可標記區塊包成 `.blk`，掛上錨點與 `#`／`☆`（第 4 期）。沒有錨點就原樣回傳。

    ⚠ **控制項放在內容前面、用 `position:absolute` 疊在右上角。** 放在後面的話，
    `md_to_html()` 產出的最後一個區塊元素（表格、`<pre>`）會把它擠到下一行去。

    ⚠ `class` 帶 `blk-ctl`：`bkMark()` 靠 `.bmk` 找按鈕、靠 `data-k` 認身分，所以行為那一半
    **必須沿用同一個 class**（多開一個 class 就要兩邊同步，遲早分岔）；`blk-ctl` 只給 CSS
    用來把顯形範圍從「滑過整輪」縮成「滑過這一塊」——一輪 p99 有 101 個區塊，
    沿用 `.turn:hover` 會讓滑過任一處就亮起一百組按鈕。

    ⚠ **這段標記是逐區塊展開的靜態文字**，每個可標記區塊約 475 bytes（全語料 1068 頁／116758
    區塊＝總量的 7.92%；⚠ 最大的幾頁只佔 1–2%，平均是被中小型頁面拉上來的）。這裡刻意**與整輪那組按鈕沿用同一套寫法**（`onclick` 直掛、
    錨點字串出現三次），不為了壓體積另開一條事件委派的路徑——**範圍限制
    `SCOPE-BOOKMARK-BLOCK-CTL-BYTES`：詳見 planning/scope-limits.md**。
    """
    if not anchor:
        return inner
    a = esc_attr(anchor)
    return (f'<div class="blk" id="{a}"><span class="blk-ctls">'
            f'<a class="alink blk-ctl" href="#{a}" title="這一塊的直達連結"'
            f' onclick="return openSub(\'{a}\')">#</a>'
            f'<button class="bmk blk-ctl" type="button" data-k="{a}"'
            f' title="加書籤／編輯書籤" aria-label="加書籤"'
            f' onclick="bkOpen(this.getAttribute(\'data-k\'))">☆</button>'
            f'</span>{inner}</div>')


def render_step_meters(idx, u, cause=None, gap=None, t=None, masked=None, anchor=""):
    """回合內單一步驟（API 呼叫）的分隔列：步驟序號＋該步的快取% / (可勾)細分 / ctx / 產出，
    另加 API 自報的失效成因（有才出現）與可勾選的「距上一步 / effort」。
    沿用 m-cache/m-in/m-cw/m-cr/m-ctx/m-out class，與整段彙總共用同一組勾選開關（cost 不在步驟層顯示）。
    冷啟著色分兩級：真失效（閒置過期/異常逐出）紅底白字；結構性或未歸因（首呼叫/回合內斷點…）中性灰，
    並在 title 說明成因。紅標＝有證據是真的快取失效；灰＝沒命中但非失效（或未分析，如 Codex）。

    `t`＝這一步的 epoch 秒（`_step` 區塊的 `t`），`analyze` 已把它正規化成**該次呼叫的起點**。
    標上時刻，頁面上才找得到「某個時間點發生了什麼事」。

    ⚠ **逐步時刻常比正上方的回合標頭更早**（本機語料上多數多步回合都如此；精確筆數隨語料
    浮動，重量測的口徑見 `planning/cache-attribution-next.md`），那不是 bug：
    步驟列所在的是 assistant 回合，它的標頭時間是**第一筆有可呈現內容的事件**，而呼叫起點在
    那之前——一次呼叫往往先產生思考或工具呼叫，之後才有可呈現的內容。兩個值各自都對。"""
    stamp = epoch_str(t, "%H:%M:%S")
    when = f" · {stamp}" if stamp else ""
    # 版面只印鐘點，完整日期掛在同一個 span 的 title——跨午夜的回合裡光看 00:01:00 無從判斷
    # 是哪一天；與回合標頭 `class="when"` 的 title 同一個作法，也不多包一層 span。
    full = epoch_str(t, "%Y-%m-%d %H:%M:%S")
    ntip = f' title="{esc_attr(full)}"' if full else ""
    # 退化階梯的**中間那一階**（第 4 期）：區塊錨點 `k…-s<ep>-b<n>` 找不到時，`openSub()`
    # 會退成 `k…-s<ep>`；沒有這一階的話，一個 286 個區塊的回合裡任何一次區塊漂移都直接
    # 彈回整輪最上面。⚠ 這個 id 只在**多步**回合出現（單步回合根本不畫步驟列，`multi_step`
    # 為假時 `render_turn_html` 不呼叫本函式）。單步回合的區塊照樣拿 `-s…-b<n>` 錨點，
    # 只是中間那一階不存在、直接退到回合——**那正是逐段退化本來就該有的行為**。
    sid_at = f' id="{esc_attr(anchor)}"' if anchor else ""
    if not u:
        return (f'<div class="step-sep"{sid_at}><span class="step-n"{ntip}>步驟 {idx}{when}</span>'
                '</div>')
    pct = _pct_display(u["cache_read"], u["total_in"])
    # 冷/熱的界線一律用整數交叉相乘（與 _step_cold／_acc_turn_usage／報告同一判準），不用四捨五入後的
    # pct——否則 24.6〜24.99% 這種邊界會顯示成「⚡25% 沒著色」卻同時掛著整段失效的 ⚠，自相矛盾。
    cold_now = bool(u["total_in"]) and u["cache_read"] * 100 < CACHE_COLD_PCT * u["total_in"]
    # 沒跑成因分析的軸（子代理／Codex）b["cause"] 為 None、_cold_display 預設塗紅；但 API 自報是實據——
    # 有前綴變動就直接接手。務必用 `if cause` 而非 `cause or …`：主對話「已分析、非失效」的步 cause=""
    # （falsy 但不是 None），不可被 or 洗成 None 而誤塗紅——那會動到無 diagnostics 的舊資料（違反不變量①）。
    cause = cause if cause else (API_MISS_CAUSE.get(u.get("miss") or "") or cause)
    cold = ""
    title = "此步驟快取命中率"
    if cold_now:
        cold = " " + _cold_display(cause)
        title = _cold_cause_title(cause, masked)
    return (f'<div class="step-sep"{sid_at}><span class="step-n"{ntip}>步驟 {idx}{when}</span>'
            f'<span class="meter m-cache{cold}" title="{esc_attr(title)}">⚡{pct}%</span>'
            + _miss_meter(u.get("miss"), u.get("miss_tok", 0), u.get("miss_usd"),
                          partial=(bool(u["total_in"]) and not cold_now),
                          unpriced=bool(u.get("miss_usd_partial")))
            + _breakdown_meters(u.get("input", 0), u.get("cache_create", 0), u["cache_read"])
            + _ctx_meter(u["total_in"], u.get("win"))
            + f'<span class="meter m-out" title="此步驟產出 tokens">↑{fmt_tokens(u["output"])}</span>'
            + _extra_meters(u, gap) + "</div>")


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


def render_subagent_block(tid, smap, smeta, tmap, used_ids, rendered, ai="Claude"):
    """把某個 Task 派出的子代理對話，算繪成就地的摺疊區（可遞迴處理巢狀子代理）。"""
    if not tid or tid in rendered or tid not in smap:
        return ""
    rendered.add(tid)
    inner = [render_turn_html(g, tmap, used_ids, smap, smeta, rendered, ai) for g in smap[tid]]
    inner = [x for x in inner if x]
    if not inner:
        return ""
    return (f'<details id="{subagent_anchor(tid)}" class="sidechain-wrap"><summary>↳ '
            + subagent_summary_label(smeta.get(tid))
            + f' · {len(inner)} 則</summary><div class="tbody">' + "".join(inner) + "</div></details>")


_NOTIFY_STATUS_LABEL = {"completed": "完成", "killed": "已中止", "failed": "失敗"}

def render_interject_html(b):
    """中途插話：助手還在工作時使用者插進來的那一句。**畫在該輪內部的子框**。

    ⚠ 位置是它**被讀到的那一刻**（`remove`），框頭另標按下送出的時刻——
    兩者最長差到 22 秒以上，只給一個會讓人讀錯。

    ⚠⚠ **`after_turn` 的那一種不可以叫「中途插話」。** 那一句是在這一輪
    `turn_duration` 之後才被讀到的——助手當時已經停了，不是「做到一半被插話」。
    它**仍然畫在這一輪內部**（改成獨立回合會切輪、位移區塊錨點），
    所以能改的只有標籤：改口叫「這一輪結束後才讀到」。
    """
    when = local_str(b.get("dt"), "%H:%M:%S")
    qs = ""
    qa = parse_ts(b.get("queued_at")) if b.get("queued_at") else None
    if qa:
        qs = local_str(qa, "%H:%M:%S")
    what = "這一輪結束後才讀到" if b.get("after_turn") else "中途插話"
    head = f"👤 你 · {what}" + (f" · {esc(qs)} 送出" if qs else "")
    body = md_to_html(clean_user_text(b.get("text") or ""))
    # 貼在這一句裡的圖片：畫在子框**內**，和文字同一格（見 `group_turns` 那段註解）
    body += "".join(render_image_block(im) for im in (b.get("imgs") or []))
    return (f'<div class="ijbox"><div class="ijhead">{esc(head)}'
            f'<span class="ijwhen" title="送出後排隊，這一刻才被讀到">{esc(when)} 讀到</span>'
            f'</div><div class="ijbody">{body}</div></div>')


def render_command_html(b):
    """指令列：`⬚ /effort high` ＋（有的話）`↳ 結果`。

    ⚠ 結果一律走 `<pre>`，**不進 `md_to_html()`**：那是終端輸出，裡面的 `*`、`#`、`_`
    是字面，套 Markdown 會把它改寫掉。單行也用 `<pre>`，兩種長度只有一個產出端。
    """
    name, args, out = b.get("name") or "", b.get("args") or "", b.get("out") or ""
    if not (name or args or out):
        return ""
    head = ""
    if name:
        # `!` 是使用者用 bash 模式直接跑的指令，參數就是那行指令本身
        label = f'{esc(name)} {esc(args)}' if args else esc(name)
        head = f'<div class="cmdline"><span class="cmdmark">⬚</span> <code>{label}</code></div>'
    body = f'<pre class="cmdout">{esc(out)}</pre>' if out else ""
    # 排隊句帶圖、而文字剛好是指令包裝時，圖掛在這個區塊上（見 `group_turns` 那段註解）
    body += "".join(render_image_block(im) for im in (b.get("imgs") or []))
    if not head and body:
        # 只有結果、沒能併回指令（來源檔缺了指令那一則）：明講它是某個指令的輸出，
        # 不要讓它看起來像使用者打了一段文字。
        head = '<div class="cmdline"><span class="cmdmark">⬚</span> <code>（指令輸出）</code></div>'
    return f'<div class="cmdrow">{head}{body}</div>'


def render_notify_html(b):
    """背景任務通知：收成一列摘要，原文收進摺疊（Will 2026-08-25 的選擇）。

    ⚠ 摘要取 `<summary>`；Monitor 型沒有 `<status>`、內容在 `<event>`。
    ⚠ 兩者都可能是空的（新版 CLI 隨時會長出新形狀）——**空的時候要退回原文**，
    否則畫出一列什麼都沒有的通知，比原本那坨 XML 更難查。

    ⚠⚠ **摺疊裡一定要有原文，不可以寫成 `event or raw`。**
    那個「或」是**只修一半**：帶 `<event>` 的那種（Monitor 型）於是只剩 event 內容，
    同一則的 `<task-id>`、`<output-file>` **從頁面和 MD 全文索引裡消失**
    ——而那些欄位在 v55 之前是搜得到的（整則原文就畫在頁面上）
    ⇒ 這一版把它變成**比舊版更差**（`utf-fix-codex` Medium 實測 `task_in_md=False`）。
    現在的做法：event 當摘要用，摺疊裡永遠是完整原文。
    """
    summ = b.get("summary") or ""
    status = b.get("status") or ""
    event = b.get("event") or ""
    raw = b.get("raw") or ""
    lead = summ or (event.splitlines()[0] if event.strip() else "")
    bits = ["背景任務"]
    if status:
        bits.append(_NOTIFY_STATUS_LABEL.get(status, status))
    head = " · ".join(bits) + (f" · {lead}" if lead else "")
    detail = _strip_ansi(raw or event)
    # 排隊句帶圖、而文字剛好是通知包裝時，圖掛在這個區塊上（見 `group_turns` 那段註解）
    _imgs = "".join(render_image_block(im) for im in (b.get("imgs") or []))
    return ('<details class="notify"><summary>'
            f'<span class="cmdmark">⚙</span> {esc(head)}</summary>'
            f'<div class="tbody"><pre class="cmdout">{esc(detail)}</pre></div>' + _imgs + '</details>')


def render_turn_html(group, tmap, used_ids, subagent_map=None, subagent_meta=None, rendered_sub=None, ai="Claude"):
    role = group["role"]
    # ⚠⚠ **耐久錨點掛在這個元素本身，`t{n}` 退成標頭裡的零尺寸 `<span class="tanchor">`。**
    # 理由是 `openSub()` 只對 `.turn`／`.compact-sep` **本身**展開內部折疊區塊、也只在它身上
    # 加 `.hl` 外框——而**那圈外框是「跳成功了」的唯一視覺訊號**。把錨點掛在標頭那個
    # `opacity:0` 的連結上，成功與失敗會長得一模一樣，整個設計的理由就沒了。
    # `#t{n}` 的行為不受影響：JS 會把 `tanchor` 往上提到所屬的 `.turn`（見 `openSub`）。
    aid = f' id="{group["kanchor"]}"' if group.get("kanchor") else ""
    tspan = f'<span class="tanchor" id="{group["anchor"]}"></span>' if group.get("anchor") else ""
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
        return (f'<div class="compact-sep"{aid}>{tspan}<span>✂ {esc(" · ".join(bits))}'
                ' — 先前對話已壓縮為摘要，以下即下次送模型 context 的起點</span></div>'
                '<details class="think compact-sum"><summary>📋 壓縮摘要</summary>'
                f'<div class="tbody">{summ}</div></details>')
    parts = []
    multi_step = group.get("n_steps", 0) >= 2   # 單步回合不必逐步標示（與整段彙總相同）
    # 區塊層級錨點（第 4 期）。三個狀態變數要一起讀，語意見 `block_anchor()` 的表：
    #   `in_step`＝走進某一步了沒（**不等於** `step_ep is not None`，混用會造出撞號）
    #   `step_ep`＝該步的 epoch（`analyze` 已正規化成該次呼叫的起點）
    #   `bn`＝目前作用域內的可標記區塊序號，**每進一步就歸零**
    kanc = group.get("kanchor") or ""     # 只給步驟列的 `-s` 錨點用
    # ⚠⚠ **區塊錨點一律由 `turn_block_anchors()` 算，這裡不重算一份。**
    # 探針要統計「實際可標記的區塊」，兩邊各算一份就會分岔——這條線為此付過兩次
    # （`bookmarks-p4-fam` Low：探針抄了 `block_is_renderable`；
    #   `bookmarks-p4-codex` Low：統計把「可渲染」誤稱「可標記」）。
    # 那一支也負責同一輪內 `tms` 撞號時整步不給錨點的底線
    # （由 `test_bookmark_block_anchor_same_second` 第 2 節在守）。
    _anchors = turn_block_anchors(group)
    _dup_tms = set()
    _seen = set()
    for _b in group["blocks"]:
        if _b.get("type") == "_step" and _b.get("tms") is not None:
            (_dup_tms if _b["tms"] in _seen else _seen).add(_b["tms"])
    bi = -1
    ij_open = False      # 「插話之後」那個縮排區塊開著沒
    ij_steps = 0         # 開著之後又走過幾個步驟（`step` 範圍用）
    ij_at = -1           # 那個開啟標籤在 `parts` 裡的位置（收框時判斷是不是空的）

    def _close_ij():
        """收掉「插話之後」那個縮排框；**裡面什麼都沒有的話連開啟標籤一起撤掉**。

        ⚠ 插話是那一輪**最後一個**區塊時（他打完字、助手沒有再回任何東西），
        舊版照樣開框 ⇒ 頁面上出現一個寫著「↳ 回應這句」、底下空無一物的框
        （`utf-fix-codex` Low 實測）。標籤是平衡的，但那句話在說謊。
        ⇒ 空的就整個撤掉；真的沒有後續回應時，畫面上就只有插話本身。
        """
        if len(parts) == ij_at + 1:
            parts.pop()
        else:
            parts.append("</div>")
    for b in group["blocks"]:
        bi += 1
        t = b.get("type")
        if t == "_step":
            # 「插話之後」的縮排**只包第一段回應**（Will 2026-08-25 從三種畫法裡挑的）：
            # 插話後的第一個步驟留在框內，**下一個步驟開始之前就收**。
            if ij_open:
                ij_steps += 1
                if ij_steps > 1:
                    _close_ij()
                    ij_open = False
            step_ep = b.get("tms")
            if step_ep in _dup_tms:
                step_ep = None          # 撞號 ⇒ 這一步當成「沒有時戳」處理
            if multi_step:
                parts.append(render_step_meters(
                    b.get("idx", 0), b.get("u"), b.get("cause"), b.get("gap"), b.get("t"),
                    b.get("cause_masked"),
                    anchor=step_anchor(kanc, step_ep)))
            continue
        # ⚠ 一律先算出 `html` 再統一包裝：舊版每個分支自己 `parts.append`，那樣「這一塊算不算
        # 一個可標記區塊」會散在五個地方，序號遲早對不上。空字串＝不渲染＝不佔序號。
        html = ""
        extra = ""          # 只有 tool_use 用得到：子代理區塊是它的**兄弟**，不進包裝
        if t == "text":
            raw = b.get("text") or ""
            txt = clean_user_text(raw) if role == "user" else str(raw)
            if txt.strip():
                html = md_to_html(txt)
        elif t == "thinking":
            think = b.get("thinking") or b.get("text") or ""
            if think.strip():
                html = ('<details class="think"><summary>💭 思考</summary>'
                        f'<div class="tbody">{md_to_html(think)}</div></details>')
        elif t == "redacted_thinking":
            html = '<div class="think-redacted">💭 思考（已隱藏）</div>'
        elif t == "tool_use":
            html = render_tool_html(b, tmap, used_ids)
            if subagent_map:
                # ⚠ **不包進 `.blk`**：子代理區塊裡面是一整批完整的回合，每一輪各自帶
                # `k…` 錨點。包進去會讓外層那個區塊 id 涵蓋幾十輪，`bkSummary()` 抓到的
                # 就變成整批子代理對話的前 120 字。
                extra = render_subagent_block(b.get("id"), subagent_map, subagent_meta or {},
                                              tmap, used_ids,
                                              rendered_sub if rendered_sub is not None else set(), ai)
        elif t == "image":
            html = render_image_block(b)
        elif t == "_command":
            html = render_command_html(b)
        elif t == "_notify":
            html = render_notify_html(b)
        elif t == "_interject":
            # 插話本身的子框；「插話之後」那一段的縮排由下面的 `ij_open` 控制
            html = render_interject_html(b)
        if html:
            parts.append(wrap_block(html, _anchors[bi]))
            if t == "_interject":
                # 前一段插話的縮排還開著就先收（同一輪插兩次 ⇒ 排成前後兩段，不做框中框）
                if ij_open:
                    _close_ij()
                parts.append('<div class="ijafter"><div class="ijafterhead">↳ 回應這句</div>')
                ij_at = len(parts) - 1
                ij_open = True
                ij_steps = 0
        if extra:
            parts.append(extra)
    if ij_open:      # ⚠ 走到回合結尾還開著就一定要收，否則 HTML 少一個 </div>
        _close_ij()
        ij_open = False

    if not parts:
        return ""

    # 指令列／通知列不套「👤 你」那個對話框：它們不是發言。只給一列窄的、帶時刻的橫列。
    # ⚠⚠ **有 `kanchor` 的照樣掛 `id` 與 `#`／`☆`。** 通知列在這一版之前就是普通 user 回合，
    # 標得到書籤；換了外觀不代表可以把身分拿掉，否則既有書籤會安靜斷掉。
    # 指令列則根本拿不到 `kanchor`（`analyze()` 不發），這裡自然什麼控制項都不會長出來。
    if group.get("_metarow"):
        when_c = local_str(group.get("dt"), "%H:%M:%S")
        full_c = (day_label(group.get("dt")) + " " + when_c) if group.get("dt") else ""
        ka = group.get("kanchor") or ""
        ctl = ""
        if ka:
            ctl = (f'<a class="alink" href="#{ka}" title="這一列的直達連結 · {esc_attr(full_c)}"'
                   f' onclick="return openSub(\'{ka}\')">#</a>'
                   f'<button class="bmk" type="button" data-k="{esc_attr(ka)}"'
                   f' title="加書籤／編輯書籤" aria-label="加書籤"'
                   f' onclick="bkOpen(this.getAttribute(\'data-k\'))">☆</button>')
        return (f'<div class="turn metarow"{aid}>{tspan}'
                f'<span class="when" title="{esc_attr(full_c)}">{esc(when_c)}</span>'
                f'<div class="metabody">{"".join(parts)}</div>{ctl}</div>')

    icon = "👤" if role == "user" else "🤖"
    who = "你" if role == "user" else ai
    side_cls = " side" if group.get("side") else ""
    side_badge = ' <span class="badge">↳ 子代理</span>' if group.get("side") else ""
    dt = group.get("dt")
    when = local_str(dt, "%H:%M:%S")
    when_full = (day_label(dt) + " " + when) if dt else ""   # 游標停留顯示完整日期
    # 排隊送出的那一句：**畫在它被讀到的時刻**（Will 2026-08-25 的裁決），但按下 Enter
    # 的時刻不能就這樣消失——時間軸會變成在說謊，所以進游標提示。
    # ⚠⚠ **而且要在版面上看得出來這是「助手工作到一半時插進來的」**（Will 2026-08-25 追加）：
    # 位置本身已經被裁決成「被讀到的那一刻」＝它看起來就像一般的一問一答，
    # 於是「這是插話」如果只留在游標提示裡，等於沒有講。所以另給一個徽章。
    _qa = parse_ts(group.get("queued_at")) if group.get("queued_at") else None
    q_badge = ""
    if _qa:
        _qs = local_str(_qa, "%H:%M:%S")
        when_full = f"{when_full}（排隊送出於 {_qs}）"
        q_badge = (f' <span class="badge queued" title="助手還在工作時就送出了：'
                   f'{esc_attr(_qs)} 按下送出、{esc_attr(when)} 才被讀到">'
                   f'⏳ 中途插話 · {esc(_qs)} 送出</span>')
    meters = render_turn_meters(group)
    # 直達連結：滑過該輪才顯形（GitHub 那種）。指向**耐久錨點**，因為使用者會把它
    # 存進瀏覽器的「我的最愛」——那是一串純網址、存在他的瀏覽器裡，**我們事後碰不到**。
    # `title` 給**本地時間**：錨點本身是 UTC，兩者可能差到跨天，不標會被讀錯。
    # `onclick` 比照子代理目錄走 `openSub`，否則「點頁內連結」與「用網址列進來」行為不一致。
    alink = (f'<a class="alink" href="#{group["kanchor"]}"'
             f' title="這一輪的直達連結 · {esc_attr(when_full)}"'
             f' onclick="return openSub(\'{group["kanchor"]}\')">#</a>'
             if group.get("kanchor") else "")
    # 加書籤鈕（第 2 期）。⚠ **和 `#` 連結同一個條件**：沒有耐久錨點就不給。
    # 書籤的身分是 `session_id + 耐久錨點`，沒有錨點就沒有能存下來的身分——
    # 給了鈕卻存不回來，比沒有鈕更糟。
    bmk = (f'<button class="bmk" type="button" data-k="{esc_attr(group["kanchor"])}"'
           f' title="加書籤／編輯書籤" aria-label="加書籤"'
           f' onclick="bkOpen(this.getAttribute(\'data-k\'))">☆</button>'
           if group.get("kanchor") else "")
    return (f'<div class="turn {role}{side_cls}"{aid}>'
            f'<div class="head">{tspan}<span class="who">{icon} {who}</span>{side_badge}{q_badge}'
            f'{meters}<span class="when" title="{esc_attr(when_full)}">{esc(when)}</span>'
            f'{alink}{bmk}</div>'
            f'<div class="body">{"".join(parts)}</div></div>')


def acct_marks(s):
    """這場 session 的切帳號時刻（epoch 秒，含小數，升冪）。

    來源是 `analyze()` 從 `load_account_switches` 填進來的 `s.acct_times`，**不是** `cache_events`
    裡那份。⚠ 理由是**形狀**不是精度：兩邊的值同源同精度（都帶次秒），但 `cache_events` 是與
    limit/auth/compact 合併排序的事件流，拿它畫線得先濾掉別種事件，濾錯就會在頁面上多畫或少畫。
    （`int()` 截整秒只發生在歸因迴圈比對上界那一刻，不在資料本身。）
    這裡不另拉資料線，也不碰任何成因判斷。"""
    marks = sorted(getattr(s, "acct_times", []) or [])
    # 兩端同一條規則：線的作用是「把前後隔開」，一端沒有東西就沒有可隔的。
    # 晚於最後一則的由 `pending_acct` 單調消耗天然不畫；最前面這端要自己擋，
    # 否則會在整頁最上面畫一條上方空無一物的線，暗示「這份 transcript 裡看得到一次切換」。
    # ⚠ 判準是**上面還有沒有回合**，不是「早於第一個有時間的回合」——開頭那幾則沒有時間時
    # 兩者不是同一件事：線會畫在第一個有時間的回合之前，而那上面仍有可隔的內容。
    # 範圍限制 SCOPE-ACCT-MARK-AFTER-LAST-TURN：涵蓋到「畫或不畫」為止——上下真的空無一物的
    # 標記兩端都不畫，輸出上也不另外標示被略過了幾筆。詳見 planning/scope-limits.md。
    groups = getattr(s, "main_groups", []) or []
    first_i = next((i for i, g in enumerate(groups) if g.get("dt")), None)
    if first_i is None or first_i > 0:
        # 沒有任何有時間的回合（`pending_acct` 推不動，天然畫不出來）；
        # 或第一個有時間的回合前面還有回合 → 線的上方不是空的，這裡不擋。
        return marks
    # ⚠ **要丟掉哪些，直接問 `pending_acct`，不要另寫一個比較。** 「這個標記收在這一則之前嗎」
    # 是同一條規則，各寫一份就會在邊角分岔——`ts > dt` 與 `pending_acct` 的
    # 「`< gt` 或（`== gt` 且該則是 user）」對**時刻完全相等**的處理不同：首則是 assistant 時
    # 前者把它丟掉，但它真正該畫的位置在第二則之前，上面明明有第一則可隔。
    # `pending_acct` 從 0 開始單調消耗，吐出來的必定是 marks 的前綴，所以切掉那段就是答案。
    dropped, _ = pending_acct(marks, 0, groups[first_i])
    return marks[len(dropped):]


def pending_acct(marks, i, group):
    """回合 `group` 之前應補畫幾條切帳號線。回傳 (要畫的時刻 list, 新索引)。

    沒有時間的回合不推進索引——留給下一個有時間的回合一起補，才不會把線畫丟。
    落在最後一個回合之後的切換不畫：那時對話已經結束，畫一條下面什麼都沒有的線只會誤導。

    時刻**完全相等**時只在 user 回合前收線：切換時刻取自新帳號的第一個 prompt，
    同一時刻的 assistant 回合是切換之前的那一則，線收在它前面會把前後兩邊畫反。

    **範圍限制 SCOPE-ACCT-SEP-HEAD-BASIS**：定位只看回合標頭的 `dt`（＝該回合第一筆**可呈現**
    事件的時刻）。同一次呼叫的**起點**可能更早——`_step` 的 `t` 印的就是那個，兩者常差幾十秒。
    切換時刻落在這兩者之間時，線收在整個回合之前，而該回合的首步其實在切換之前就開始了。
    詳見 planning/scope-limits.md 的 SCOPE-ACCT-SEP-HEAD-BASIS。"""
    dt = group.get("dt")
    if dt is None:
        return [], i
    gt = dt.timestamp()
    take_equal = group.get("role") == "user"
    out = []
    while i < len(marks) and (marks[i] < gt or (take_equal and marks[i] == gt)):
        out.append(marks[i])
        i += 1
    return out, i


def acct_sep_label(ts, day=""):
    """切帳號分隔線的文字。**只陳述實據**：這裡換了登入帳號，以及時刻。

    ⚠ **不得使用成因層的判定詞。**「自行切帳號」（`acct`）在本檔是專有名詞，意思是
    **沒撞 limit 就換**（人因可避免）；撞了 429/401 才換的，由 `classify_cache_causes` 的
    boundary 優先規則判成 `switch`（「limit/切帳號」，不可避免）。而畫線的條件只有
    「history 裡有這個時刻」，完全不看有沒有 limit/auth 邊界——貼上任一個判定詞，都會有
    一部分的線與正下方那一步的成因徽章講相反的話。**自行／被迫的判斷歸成因層。**

    同理也不講快取後果：畫線不要求任何命中率證據，而掛 `acct` 前要先過 `_step_cold`。

    `day`（`%Y-%m-%d`）是**讀者在那個位置已經看得到的日期**——HTML 傳該則所屬的日期
    （頁面上方就有日期分隔線可對照）。不同就補上日期，否則只印時分會被讀成當天的時刻。

    ⚠ **不給 `day`＝那個輸出沒有任何日期上下文**（MD 就是這樣：回合標頭只有 `%H:%M:%S`），
    此時一律帶日期。MD 不能挑基準：挑起始日，「切換在起始日、下一則在數日後」那格會裸奔；
    挑插入處那一則的日期，「切換在更早那天」那格又會裸奔——兩格都只有「一律帶」才自洽。"""
    stamp = epoch_str(ts, "%H:%M")
    if stamp and epoch_str(ts, "%Y-%m-%d") != day:
        stamp = epoch_str(ts, "%m-%d %H:%M")
    return f"換了登入帳號 · {stamp}" if stamp else "換了登入帳號"


# =========================================================================
# 書籤（第 2＋3 期）：核心、共用對話窗、匯出匯入、管理頁、設定頁
# =========================================================================
# ⚠⚠ **下面這幾個常數都是普通字串，不是 f-string——大括號寫一個就好。**
# session 頁的 `<script>` 區塊本身是 f-string（那裡的括號要 double），把這幾大段 JS
# 直接寫進去等於要手動 double 掉數百個括號，錯一個整頁 JS 就死。改用
# 「普通 raw 字串 ＋ `__佔位符__`」，由各自的 render 函式用 `js_embed()` 填值。
#
# 命名（**刻意分前綴**，免得日後改到錯的那一組）：
#   `bm*` 第 1 期「錨點找不到」的橫幅；`bk*` 三頁共用的核心與對話窗；
#   `bx*` 書籤管理頁；`bs*` 設定頁。
#
# ⚠ **`_BOOKMARK_CORE_JS` 由三個頁面共用**（session 頁／管理頁／設定頁），
#   對頁面有四項要求，缺一項就會壞：
#     1. 先定義 `lsGet`／`lsSet`（包 try/catch 的 localStorage 存取）
#     2. 先定義 `BK_MGR`＝管理頁的相對路徑（**空字串＝這一頁自己就是管理頁**，不畫那個連結）
#     3. 定義 `bkRefresh(msg)`：core 改完資料之後叫它，由各頁決定要重畫什麼
#     4. DOM 裡要有 `#bkModal` / `#bkCard`（共用對話窗的容器）與 `#bkMsg`（訊息列，由 bkFoot 產）
#   守這四項的是 `test_bookmark_core_contract`（三頁各驗一次）。
_BOOKMARK_CORE_JS = r"""
var BK_KEY='asv_bm_v1', BK_SKEY='asv_bmset_v1';
/* 複查間隔：**單選**（Will 的原話是「下拉使用上比較不方便」），預設半年。
   ⚠ 到期只是一個狀態，**書籤永遠不會自動刪**（Will 2026-08-22 裁決）。 */
var BK_SPANS=[['0','不提醒'],['14d','兩週'],['1m','一個月'],['3m','三個月'],
              ['6m','半年'],['1y','一年']];
var BK_FALLBACK='6m';   /* 設定頁還沒被寫過時的出廠值 */
var BK_CATN=6;          /* 對話窗預設露出幾顆類別 chip，其餘收在「更多…」後面 */
var BK_EDIT=null;       /* 對話窗正在編輯的那一筆（null＝沒開著編輯畫面）*/
var BK_CAT='';          /* 對話窗目前選著的類別（''＝沒有類別，那也是一種選擇）*/
var BK_MORE=false;      /* 對話窗的類別 chip 展開了沒 */
var BK_UNDO=null;       /* 剛移除的那一筆，讓「復原」不必再打一次備註 */

/* ── 匯入檔的型別閘（**進得來的東西一律先過這裡**）────────────────────
   ⚠⚠ 舊版對字串欄位一律 `String(v)`，於是 `String({})` ＝ `'[object Object]'`
   ⇒ 造得出 id 為 `[object Object]|[object Object]` 的**幽靈書籤**
   （跨模型 Medium#7）。`String()` 不是驗證，它是**強制轉型**——把「不是字串」
   悄悄變成一個看起來像字串的東西，正是驗證要擋的那件事。 */
function bkStr(v,max){
 if(typeof v==='number'&&isFinite(v))v=String(v);   /* 舊資料的 sid 可能是數字 */
 if(typeof v!=='string')return '';
 return v.slice(0,max||4000);}
/* `sid` 與 `anchor` 會被 `bkId()` 用 `|` 接成身分：任一邊含 `|` 就分不開了。
   空白與控制字元也擋掉——那種值只可能來自壞掉或偽造的檔。
   ⚠⚠ **這個字元類只擋三樣：`|`、空白、控制字元。其餘一律放行。**
   耐久錨點的文法是 `k<ts>[-tb<n>][-s<ts>][-b<n>]`，把 `-` 也擋掉的話，
   子代理與撞號那幾種錨點會被整批判成無效、匯入時無聲消失
   ——而第 4 期（區塊層級）還會再往上加一段。**不要把這裡收緊成白名單。**
   守它的是探針的 `durable_anchor_shapes_accepted`（四種形狀各一格）。 */
function bkIdPart(v){
 var s=bkStr(v,200).replace(/^\s+|\s+$/g,'');
 return /^[^|\s\u0000-\u001f]+$/.test(s)?s:'';}
/* `span` 走白名單：不認得的一律「不提醒」，和 `bkDue`／`bkSpanLabel` 的判定一致。 */
function bkSpanOk(v){
 var s=bkStr(v,8).replace(/^\s+|\s+$/g,''),i;
 for(i=0;i<BK_SPANS.length;i++)if(BK_SPANS[i][0]===s)return s;
 return '0';}
/* ⚠ `due` 收負值的話會顯示成 1970-01-01 **而且立刻算到期**；超界則是 Invalid Date。
   兩種都當「沒設」。守它的是探針的 `negative_due_rejected`（負值那半）
   與 `infinite_due_clamped`（超界那半，⑬e 那一組）。 */
function bkDueOk(v){
 var n=Number(v);
 return (isFinite(n)&&n>0&&n<=8.64e15)?n:0;}
/* ⚠⚠ 匯入檔的**時間戳**：缺漏或垃圾＝「未知」，權重 0，**不可以退回「匯入當下」**
   （跨模型 High#2）。退回當下有兩個後果：①同一份檔案匯入兩次結果不同（不冪等）；
   ②那個值永遠比本機大 ⇒ 一份舊備份可以**反覆**把本機後來的編輯壓掉。
   未知（0）在 `mtI>bkWhen(cur)` 這個比較裡一定輸 ⇒ 只會補本機缺的欄位，不會蓋掉。
   「什麼時候拿到的」另外記在 `im`，只給排序與顯示用，**絕不參與合併比較**。 */
function bkTs(v,now){
 var n=Number(v);
 if(!isFinite(n)||n<=0||n>now+86400000)return 0;    /* 允許來源機器的時鐘快一天 */
 return n;}

/* ── 存取 ───────────────────────────────────────────────────────────
   全部包 try/catch：`file://` 下 localStorage 可能丟例外、也可能回 null
   （lsGet/lsSet 已經擋了一層，這裡擋的是 JSON 壞掉那一層）。 */
/* ⚠⚠ **「讀不到」和「確定沒有書籤」是兩種狀態，不可以混成同一種。**
   舊版解析失敗就回空陣列，於是**下一次任何寫入**（加一筆、移除、匯入）都把那個空
   狀態寫回原鍵，把還救得回來的原始字串**永久輾掉**——等於所有書籤自動消失
   （跨模型 High#4）。現在壞掉時帶 `bad:1` ＋ `raw`，由 `bkStore()` 一律拒寫，
   footer 給一條「⛑ 匯出原始資料」的救援路。
   守它的是探針的 `corrupt_store_not_overwritten`／`corrupt_store_drop_also_blocked`。 */
function bkLoad(){
 var r=null;
 try{r=lsGet(BK_KEY);}catch(e){return {v:1,items:[],bad:1,raw:''};}
 if(!r)return {v:1,items:[]};
 var o=null;
 try{o=JSON.parse(r);}catch(e){return {v:1,items:[],bad:1,raw:r};}
 if(!o||!Array.isArray(o.items))return {v:1,items:[],bad:1,raw:r};
 /* ⚠ items 裡的**每一筆**也要驗。`{items:[null]}` 是合法 JSON、根形狀也對，
    但 `bkCatsAll()` 一碰就 `TypeError` ⇒ **整頁死掉**（跨模型 Medium#7）。
    ⚠ 個別壞掉的紀錄直接丟掉、**不封鎖寫入**：它們連 sid/anchor 都沒有，
    沒有任何可救的內容，而一個 null 不該讓使用者從此不能再加書籤。
    這和上面那個「整份讀不到」是**不同等級**的壞，處置也不一樣。 */
 var out=[],kept=[],i,x,sid,anch;
 for(i=0;i<o.items.length;i++){
  x=o.items[i];
  sid=(x&&typeof x==='object')?bkIdPart(x.sid):'';
  anch=(x&&typeof x==='object')?bkIdPart(x.anchor):'';
  if(!sid||!anch){
   /* ⚠⚠ **認不出來的不可以就這樣丟掉。** 舊版直接 `drop++;continue;`，而
      `bkStore()` 只寫 `st.items` ⇒ **下一次任何寫入就把它們永久移除**，
      連帶把裡面的備註一起弄丟。註解當時寫「它們連 sid/anchor 都沒有，
      沒有任何可救的內容」——**那句是錯的**：`{sid:"",note:"還沒補錨點的筆記"}`
      有滿滿的可救內容。這和「書籤永遠不會自動刪」直接牴觸。
      改成**原樣留著**（`kept`），由 `bkStore()` 一起寫回去：不顯示、不參與合併，
      但也絕不消失，`⬇ 完整備份`（`pay.kept`，見 `bkExport`）與 `⛑ 匯出原始資料`
      都救得到。⚠ 「救得到」＝**檔案裡有**，不是「匯入會還原」——它們沒有 sid，
      匯入端一定會略過。
      ⚠ 不走 `bad:1` 拒寫那條路：那會讓一筆垃圾紀錄害使用者從此不能再加書籤。
      守它的是探針的 `unreadable_record_survives_write`。 */
   kept.push(x);continue;}
  /* ⚠⚠ **逐欄位正規化，不是只驗 sid／anchor 就把原物件塞回去。** 舊版 `out.push(x)`
     ⇒ `{sid:'s',anchor:'k',cat:{}}` 過得了這一關，然後 `bkCatsUsed()` 的
     `(cat||'').replace` 直接 `TypeError`＝**整頁死掉**。那正是 `{items:[null]}`
     那一條（跨模型 Medium#7）的同一個病，只是換到欄位層——當時只補了最外層。
     ⚠ **`url` 刻意不在這裡過 `bkSafeRel`**：那會讓匯出端那道閘再也沒有素材走得到
     （手改進去的絕對路徑會在載入時就被清掉），縱深就變成只剩一道而且量不到。
     形狀驗證留給匯出端（`export_gate_drops_hand_edited_url` 在守）。
     守它的是探針的 `bad_field_type_does_not_kill_page`。 */
  out.push({id:(typeof x.id==='string'&&x.id)?x.id:bkId(sid,anch),
            sid:sid,anchor:anch,
            url:bkStr(x.url,400),
            stitle:bkStr(x.stitle,400),
            town:x.town?1:0,
            note:bkStr(x.note,20000),
            cat:bkStr(x.cat,200).replace(/^\s+|\s+$/g,'').slice(0,60),
            sum:bkStr(x.sum,400),
            ct:bkDueOk(x.ct),mt:bkDueOk(x.mt),im:bkDueOk(x.im),lr:bkDueOk(x.lr),
            span:bkSpanOk(x.span),due:bkDueOk(x.due)});}
 return kept.length?{v:1,items:out,kept:kept,drop:kept.length}
                   :{v:1,items:out};}
/* ⚠⚠ **回傳寫入成敗，呼叫端一定要看。** `lsSet` 把例外吞掉是對的（不能讓整頁死掉），
   但**吞掉之後還跟使用者說「已加入書籤」就是騙人**——`file://` 在 Chrome 眼裡是單一
   origin，配額由所有本機開過的頁面共用；隱私模式下 `setItem` 會直接丟例外。
   這是少數幾個該讓使用者**立刻知道**的錯誤：他以為存好了，其實一筆都沒有，
   而那句「真正的備份是匯出的 JSON」還會讓他覺得不急著匯出。
   守它的是探針的 `quota_failure_is_reported`。
   ⚠⚠ **壞掉的 store 一律拒寫，而且擋在這裡**——這是唯一的寫入口，
   `bkCommit`／`bkDrop`／`bkUndo`／`bkImport` 全部經過它。擋在各呼叫端就會漏
   （教訓：**保險只寫在一條分支上等於沒寫**）。 */
function bkStore(st){
 if(st&&st.bad)return false;
 /* ⚠⚠ **認不出形狀的紀錄要原樣寫回去**（見 `bkLoad()` 的 `kept`）。
    少了這一段，任何一次寫入都會把它們永久移除。 */
 var items=((st&&st.items)||[]).concat((st&&st.kept)||[]);
 return lsSet(BK_KEY,JSON.stringify({v:1,items:items}));}
/* 寫入失敗時統一的說法：講清楚「沒有存進去」，並指向唯一還有救的動作。 */
var BK_FAILMSG='⚠⚠ 沒有存進去（瀏覽器'
 +'儲存空間滿了，或這個模式不'
 +'允許寫入）。請先按「⬇ 完整'
 +'備份」把現有的書籤存成檔案。';
/* ⚠ 壞掉的 store 要講**不一樣的話**：說「空間滿了」會讓人去做沒有用的事（清空間），
   而真正該做的是先把原始字串救出來。 */
var BK_BADMSG='⚠⚠ 讀不到現有的書籤（'
 +'localStorage 裡那份資料壞了）'
 +'，所以這次沒有存進去——硬存'
 +'會把還救得回來的原始資料蓋'
 +'掉。請先按「⛑ 匯出原始資料'
 +'」存成檔案。';
function bkWhyFail(st){return (st&&st.bad)?BK_BADMSG:BK_FAILMSG;}
/* ── 兩把鍵要一起成立時的回捲 ──────────────────────────────────────
   ⚠⚠ 類別的改名／刪除／復原**先寫書籤那把鍵、再寫設定那把**。只有第二把失敗時，
   書籤其實**已經改掉了**，而畫面上卻印著通用的「沒有存進去」——使用者會以為
   什麼都沒發生，實際上書籤的類別已經全部換成新名字了。
   （寫入順序本身是對的：先書籤後設定，見 `bkImport` 那段的理由。）
   所以第二把失敗時要把第一把回捲，並依回捲結果講**不一樣的話**。
   守它的是探針的 `settings_second_write_rolls_back`。 */
function bkRawSnap(){try{return lsGet(BK_KEY);}catch(e){return null;}}
function bkRollback(raw){
 if(raw===null||raw===undefined)return false;
 try{return !!lsSet(BK_KEY,raw);}catch(e){return false;}}
var BK_PARTIALMSG='⚠⚠ 只做到一半：'
 +'書籤那邊已經改掉了，類別設定'
 +'沒有存進去，而且要復原回去也'
 +'失敗了。請先按「⬇ 完整備份」'
 +'存檔，再檢查瀏覽器的儲存空間'
 +'。';
/* ⛑ 救援：把 localStorage 裡那串**原始字串**原封不動存成檔案。
   ⚠ 不修補、不重新 JSON 化——壞在哪裡要留給人看得到。 */
function bkRescue(){
 var st=bkLoad(), raw=st.bad?(st.raw||''):(lsGet(BK_KEY)||'');
 /* ⚠⚠ **沒有原始字串就不要下載**：照存下去就是一個 0 位元組的檔 ＋ 一句
    「已把原始資料存成檔案」——訊息和實際發生的事相反。
    ⚠⚠ **但訊息要跟著實際狀態走。** 舊版一律說「瀏覽器拒絕存取 localStorage」，
    理由寫的是「`lsGet` 丟例外時 `bkLoad()` 回 `{bad:1,raw:''}`」——**那個狀態到不了**：
    `lsGet` 自己就 try/catch 回 `null`，`bkLoad` 的 `if(!r)` 先把它接走 ⇒ 永遠不是 `bad`。
    ⚠⚠ **訂正（`bookmarks-p4-fam` Low）：整個 `if(!raw)` 分支從 UI 都到不了，不只 `st.bad` 那半。**
    `bkFoot()` 的救援鈕**只在 `st.bad` 為真時**畫得出來，而 `bkLoad()` 在 `st.bad` 時
    `raw` 必定非空（`if(!r)` 先把 null 接走了，JSON 壞掉那條一定帶著原字串）
    ⇒ 按得到鈕的時候 `raw` 一定有東西。
    這裡原本寫「真正到得了這一格的只有『store 完全正常、只是還沒有任何書籤』」——
    **也不成立**（那個狀態下鈕根本不會出現）。
    ⇒ **這整段是純防禦**：唯一走得到的是探針**直接呼叫** `bkRescue()`。
    留著它的理由是「訊息不可以和實際發生的事相反」這條規則不該有例外，
    **不是**因為有哪個使用者操作序列走得到。
    ⚠ 所以這裡刻意**不寫**「守它的是 XXX」——探針那兩格驗的是直接呼叫下的行為，
    那證明不了任何 UI 路徑。 */
 if(!raw){
  bkSay(st.bad
        ?'連原始資料都讀不到（瀏覽器拒絕存取 localStorage）。'
         +'請改用一般視窗（非無痕）開這一頁再試一次——資料可能還在。'
        :'目前一筆書籤都沒有，沒有東西可以救。');
  return;}
 if(bkDownload('asv-bookmarks-raw-'+bkStamp()+'.txt',raw))
  bkSay('已把原始資料存成檔案（'+raw.length+' 個字元），可以用文字編輯器打開來救。');}

/* ── 設定（第 3 期的設定頁在寫它；第 2 期就已經在讀了）───────────────
   形狀：{rv:'6m', cats:['設計決策',…]}。`cats` 是**設定頁管理的清單**，
   有順序、可以含還沒被任何書籤用到的名字。 */
function bkCatsClean(a){
 /* ⚠ 去重用的表一律 `Object.create(null)`：拿 `{}` 當表時，名叫 `constructor`／
    `__proto__` 的類別會撞到原型上的成員 ⇒ 被當成「已經看過」而**無聲吃掉**。
    匯入檔是別人給的，那種名字進得來。守它的是 `cat_named_constructor_survives`。 */
 var out=[],seen=Object.create(null),i,c;
 if(!Array.isArray(a))return out;
 for(i=0;i<a.length&&out.length<200;i++){
  if(typeof a[i]!=='string')continue;
  c=a[i].replace(/^\s+|\s+$/g,'').slice(0,60);
  if(!c||seen[c])continue;
  seen[c]=1; out.push(c);}
 return out;}
/* `cta` ＝每個類別**第一次被建立**的時刻，只給對話窗的「最近用過」排序當第二把尺
   （見 `bkCatsRecent`）。⚠ 只留還在 `cats` 裡的鍵：改名／刪除之後不清就會一直累積死鍵。
   ⚠ 一律 `Object.create(null)`：類別名可能叫 `constructor`／`__proto__`。 */
function bkCatTimes(src,cats){
 var out=Object.create(null), i, t;
 if(!src||typeof src!=='object')return out;
 for(i=0;i<cats.length;i++){
  t=Number(Object.prototype.hasOwnProperty.call(src,cats[i])?src[cats[i]]:0);
  if(isFinite(t)&&t>0)out[cats[i]]=t;}
 return out;}
/* ⚠⚠ **「壞掉就拒寫」原本只做在書籤那把鍵上。** 類別清單是使用者自己建的資料、
   同樣只活在 localStorage 裡（README 特別說明過），卻是舊行為：解析失敗就當成
   「這台還沒有設定」，然後**下一次任何寫入**（加一個類別、按一顆間隔 chip、匯入）
   就把原字串輾掉。又一次「保險只寫在一條分支上」——兩把鍵同一個等級，只守了一把。
   守它的是探針的 `corrupt_settings_not_overwritten`。 */
function bkSetBad(){
 var r=null;
 try{r=lsGet(BK_SKEY);}catch(e){return true;}
 if(!r)return false;            /* 沒有＝還沒設定過，那不是壞掉 */
 try{var o=JSON.parse(r);
     return !(o&&typeof o==='object'
              &&(typeof o.rv==='string'||Array.isArray(o.cats)));}
 catch(e){return true;}}
function bkSettings(){
 /* ⚠⚠ **`rv` 缺漏不可以整份丟掉。** `{"cats":["甲","乙"]}` 原本會回 null ⇒
    對話窗與設定頁的**整份類別清單直接消失**，看起來像「你沒有任何類別」，
    接著第一次存偏好就寫成 `cats:[]`，永久。`cats` 是陣列就先救回來，
    `rv` 退回出廠值。守它的是探針的 `settings_without_rv_keeps_cats`。 */
 try{var r=lsGet(BK_SKEY);var o=r?JSON.parse(r):null;
     if(o&&typeof o==='object'
        &&(typeof o.rv==='string'||Array.isArray(o.cats))){
      var cs=bkCatsClean(o.cats);
      return {rv:(typeof o.rv==='string')?o.rv:BK_FALLBACK,
              cats:cs,cta:bkCatTimes(o.cta,cs)};}}catch(e){}
 return null;}                 /* null ＝ 這台還沒有個人設定（或讀不到，見 bkSetBad）*/
function bkSet(){var s=bkSettings();
 return s?s:{rv:BK_FALLBACK,cats:[],cta:Object.create(null)};}
var BK_SETBADMSG='⚠⚠ 讀不到現有的'
 +'類別設定（localStorage 裡那份'
 +'資料壞了），所以這次沒有存進'
 +'去——硬存會把還救得回來的原始'
 +'資料蓋掉。請先用瀏覽器的開發'
 +'者工具把 asv_bmset_v1 那一項'
 +'複製出來，再回來修改。';
/* 設定寫失敗時的說法：壞掉和「空間滿了」要講不一樣的話——講錯會讓人去清空間
   （沒有用），而真正該做的是先把原始字串救出來。和 `bkWhyFail` 是同一條規則。 */
function bkWhySetFail(){return bkSetBad()?BK_SETBADMSG:BK_FAILMSG;}
function bkSaveSet(o){
 if(bkSetBad())return false;
 var cs=bkCatsClean(o?o.cats:[]);
 return lsSet(BK_SKEY,JSON.stringify({rv:String(o&&o.rv?o.rv:BK_FALLBACK),
                                      cats:cs,cta:bkCatTimes(o?o.cta:null,cs)}));}
function bkDefSpan(){
 var s=bkSettings(); s=s?s.rv:null;
 for(var i=0;i<BK_SPANS.length;i++)if(BK_SPANS[i][0]===s)return s;
 return BK_FALLBACK;}          /* 沒設定、或設定值不認得，都退回半年——不要讓 UI 一片空白 */
function bkSpanLabel(v){
 for(var i=0;i<BK_SPANS.length;i++)if(BK_SPANS[i][0]===v)return BK_SPANS[i][1];
 return BK_SPANS[0][1];}       /* 不認得的值＝不提醒，和 bkDue 的判定一致 */
function bkId(sid,a){return sid+'|'+a;}
function bkFindId(st,id){
 for(var i=0;i<st.items.length;i++)if(st.items[i].id===id)return st.items[i];
 return null;}
/* ⚠ 匯入檔的數字欄位一律過這一層：`Number('1e999')` 是 `Infinity`，
   拿它當「比較新」的依據就會**永遠贏過任何本機值**（`bookmarks-p23` 的 High #2 順帶項）。
   守它的是探針的 `infinite_ct_clamped`／`infinite_due_clamped`。 */
function bkNum(v,def){var n=Number(v);return isFinite(n)?n:def;}
/* 合併時的「時刻」：優先用 `mt`（最後修改），沒有就退回 `ct`（建立）。
   ⚠ 舊版的備份沒有 `mt`，那條路一定要走得通。
   ⚠ 下界夾在 0：負值（手改過的 store、別的工具寫進來的）會讓
   `newer=incWhen>bkWhen(cur)` 對「未知時間戳（0）」的匯入檔**恆真**，
   而 `cur.mt` 只在 `newer&&incWhen>0` 時才寫回去 ⇒ 那個條件恆假
   ⇒ 同一份檔案每匯一次就再判一次衝突、備註尾巴每次長一截（＝不冪等）。
   ⚠⚠ **但這個夾制是純防禦，沒有素材走得到**（教訓 29）：`bkLoad()` 是唯一的讀取口，
   它已經用 `bkDueOk` 把 `mt` 正規化掉負值了，所以進到這裡的 `x.mt` 不可能是負的。
   突變檢驗實測：只把這裡的夾制拿掉，探針**全綠**。
   ⇒ **不要在這裡寫「守它的是 XXX」。** 真正在守那個不冪等症狀的是
   `bkLoad()` 的 `mt:bkDueOk(x.mt)`（探針 `negative_mt_import_is_idempotent` 測的是它）。
   ⚠⚠ **`mt` 是 0 要當成「沒有」，繼續退回 `ct`。** 不可以寫成
   `bkNum(x.mt, bkNum(x.ct,0))` 然後夾下界——`bkLoad()` 現在會把缺漏的 `mt`
   正規化成 **0**（而 0 是 finite），於是那個寫法再也不會退回 `ct`，
   所有舊格式紀錄的時刻一起變成 0 ⇒「最近用過」的排序整個垮掉。
   （這一格是探針的 `recent_used_first` 當場抓到的。） */
function bkWhen(x){
 var m=bkNum(x&&x.mt,0); if(m>0)return m;
 var c=bkNum(x&&x.ct,0); return c>0?c:0;}
/* 排序用的「時刻」：`ct` 未知（匯入檔沒帶時間戳）時退回 `im`＝什麼時候匯進來的。
   ⚠⚠ **`im` 只給排序與顯示，絕不參與 `bkWhen()` 的合併比較**——「拿收到的時間當
   修改時間」正是跨模型 High#2 的成因。兩個用途分開，才不會又混回去。 */
function bkOrd(x){return bkNum(x&&x.ct,0)||bkNum(x&&x.im,0);}

/* ── 合併一筆（匯入 ↔ 本機，兩個方向共用同一段）─────────────────────
   ⚠⚠ **「只增不減」是這一套的最高不變量**，而它一度是假的：舊版是**整筆勝者制**
   ——較新那一筆的非空欄位直接取代本機，輸家的 note/cat/sum **原地消失、不留痕跡**；
   反向分支自稱「只補缺欄位」卻漏掉 `url`／`town`／獨立的 `due`（跨模型 High#3）。
   上一輪的 commit 訊息寫「兩個方向都改成逐欄位、只增不減」，**那句話對非空衝突是假的**，
   而且沒有任何測試在守它。這次連測試一起補（探針 ⑭c）。

   欄位分兩類，處置不同：
   · **使用者自己寫的**（`note`／`cat`）：一個字都不可以掉。兩邊都有而且不同、
     **而且匯入的那邊比較新**時（＝本機那份即將被取代），贏的留在欄位裡、
     **輸的那一份寫進備註尾巴**，讓人自己決定要留哪個。
     ⚠⚠ **反過來（本機比較新）就不記。** 那個方向本機什麼都沒有被破壞，
     而匯入端那個值**還好端端躺在使用者剛剛選的那個檔案裡**——記進備註只是噪音，
     而且會讓「同一份檔案匯入兩次」在備註尾巴一直長東西（＝不冪等）。
     **判準是「這個值會不會就此消失」，不是「兩邊有沒有不一樣」。**
   · **快取欄位**（`url`／`stitle`／`sum`）：本來就可以重算（見 `bkEditRec`），
     新的贏、缺的補，衝突不記——那不是資料損失。
   · **`town`**：**永遠不吃匯入端的**，理由見 `bkImport` 裡那一段。
   ⚠ 兩個方向共用這一段，是因為「保險只寫在一條分支上等於沒寫」——這條線上
   同一個保證已經連續寫錯過三次，全部都是分支之間漏掉一邊。 */
var BK_LOST='—— 合併時保留的另一份值：';
function bkKeepLost(note,lost){
 if(!lost)return note||'';
 /* ⚠ 已經記過就不要再記：同一份檔案匯入兩次不可以讓備註一直長（冪等）。
    ⚠⚠ **比對要帶標記前綴。** 只比 `lost` 的話，輸家只要**碰巧是贏家的子字串**
    就被判成「已經記過」而整個吞掉——而那正是最該留下來的東西：
    本機備註「記得看設計決策那段」會讓舊類別名「設計決策」一聲不響地消失。
    帶上 `BK_LOST` 就只認我們自己寫過的那一行。
    守它的是探針的 `lost_substring_still_recorded`。 */
 if(note&&note.indexOf(BK_LOST+lost)>=0)return note;
 return (note?note+'\n':'')+BK_LOST+lost;}
function bkMerge(cur,inc,incWhen){
 var newer=incWhen>bkWhen(cur), n=0, lost;
 if(inc.note&&inc.note!==cur.note){
  if(!cur.note){cur.note=inc.note;n++;}
  else if(newer){lost=cur.note;cur.note=bkKeepLost(inc.note,lost);n++;}}
 /* 一筆只能掛一個類別，所以本機那個即將被取代的**名字**要寫進備註，不然它就真的沒了。 */
 if(inc.cat&&inc.cat!==cur.cat){
  if(!cur.cat){cur.cat=inc.cat;n++;}
  else if(newer){lost=cur.cat;cur.cat=inc.cat;cur.note=bkKeepLost(cur.note,lost);n++;}}
 if(inc.sum&&(newer||!cur.sum)){cur.sum=inc.sum;n++;}
 if(inc.stitle&&(newer||!cur.stitle)){cur.stitle=inc.stitle;n++;}
 if(inc.url&&(newer||!cur.url)){cur.url=inc.url;n++;}
 /* 提醒間隔與到期日一起搬（`bkOverdue` 只看 `due`，兩者不同步就會說謊）。
    ⚠⚠ **`cur.span==='0'` 不可以放進這個條件裡。** `'0'` 是使用者在對話窗裡
    **明確關掉提醒**的結果，不是「還沒設定」；把它當成缺值去補，就會讓一份**較舊**的
    備份把關掉的提醒無聲復活，而且連 `due` 一起帶回來 ⇒ 立刻算「該複查了」。
    這和下面那一行的註解本來是同一條規則，卻只寫在下面那半——又一次
    「保險只寫在一條分支上」：下面那道守著的東西，上面這道先放行了。
    只有「**根本沒有這個欄位**」（舊格式）才補。
    守它的是探針的 `older_import_does_not_revive_off_reminder`。 */
 if(inc.span!=='0'&&(newer||!cur.span)){
  cur.span=inc.span;cur.due=inc.due;n++;}
 /* ⚠⚠ **本機已經有間隔、卻沒有到期日**時才單獨補 `due`（那正是舊版漏掉的一格）。
    ⚠ 反過來**不可以**在 `cur.span==='0'` 時補 `due`：那是使用者明確關掉的提醒，
    補了會讓它無聲復活（`bkOverdue` 只看 `due`，畫面上的 chip 卻寫著「不提醒」）。 */
 else if(cur.span&&cur.span!=='0'&&!cur.due&&inc.due){cur.due=inc.due;n++;}
 if(newer&&incWhen>0){if(!cur.ct)cur.ct=inc.ct;cur.mt=incWhen;}
 else if(!cur.ct&&inc.ct)cur.ct=inc.ct;
 if(!cur.im&&inc.im)cur.im=inc.im;
 /* ⚠⚠ 真的改了東西就遞增本機修訂號，讓**已經開著的對話窗**察覺得到
    （見 `bkEditRec` 的 `lr0`）。`mt` 在「補缺欄位」那幾條路上刻意不動，
    所以不能只靠它。`lr` 不匯出、不參與比較。 */
 if(n)cur.lr=bkNum(cur.lr,0)+1;
 return n;}

/* ── 複查到期時刻 ───────────────────────────────────────────────────
   ⚠ **不可以直接 `d.setMonth(d.getMonth()+n)`**：1/31 加一個月會溢位成 3/2 或 3/3。
   先把日設成 1 再換月，最後夾到該月最後一天。 */
function bkAddMonths(ms,n){
 var d=new Date(ms), day=d.getDate();
 d.setDate(1); d.setMonth(d.getMonth()+n);
 var last=new Date(d.getFullYear(),d.getMonth()+1,0).getDate();
 d.setDate(day<last?day:last);
 return d.getTime();}
function bkDue(span,from){
 if(span==='14d')return from+14*86400000;
 if(span==='1m')return bkAddMonths(from,1);
 if(span==='3m')return bkAddMonths(from,3);
 if(span==='6m')return bkAddMonths(from,6);
 if(span==='1y')return bkAddMonths(from,12);
 return 0;}                    /* '0' 與任何不認得的值都當「不提醒」 */
function bkOverdue(x){return !!(x&&x.due&&x.due<=Date.now());}
function bkDate(ms){
 /* ⚠ 匯入檔的 `due` 可能超出 JS Date 的 ±8.64e15（`1e999` ⇒ Infinity）。
    超界就是 Invalid Date、`getFullYear()` 回 NaN，畫面會印出「複查 NaN-NaN-NaN」。
    **算不出來就什麼都不印**——和 `bmWhen` 那條（`durable-anchor-r4` #8）是同一課：
    不要自信地印一個不存在的日期。守它的是探針的 `overflow_due_no_nan`。 */
 if(!ms||!isFinite(ms)||Math.abs(ms)>8.64e15)return '';
 var d=new Date(ms);
 function p(n){return (n<10?'0':'')+n;}
 return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());}
function bkStamp(){var d=new Date();function p(n){return (n<10?'0':'')+n;}
 return ''+d.getFullYear()+p(d.getMonth()+1)+p(d.getDate());}

/* ⚠ 一律跳脫再進 innerHTML：備註與類別是使用者輸入、摘要是對話原文，
   而匯入的檔案可能來自別人。三個來源都不可信。 */
function bkEsc(s){return String(s==null?'':s)
 .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
 .replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

/* ⚠⚠ 記錄裡的 `url` **可能來自別人給的匯入檔**，而管理頁會拿它組 href。
   進 DOM 之前先驗形狀：只收「相對 out/ 的 sessions/… 路徑」，其餘一律回空字串，
   由呼叫端顯示「對不到檔案」而不是生一個怪連結。
   （`javascript:` 這種即使被前面接上 `../` 也只會變成一個不存在的相對路徑，
   但**不要靠那個巧合**——這裡明著擋。） */
function bkSafeRel(u){
 u=String(u==null?'':u);
 if(u.indexOf('sessions/')!==0)return '';
 if(u.indexOf(':')>=0||u.indexOf('\\')>=0)return '';
 if(u.indexOf('//')>=0||u.indexOf('../')>=0||u.indexOf('/..')>=0)return '';
 return u;}

/* 「這個識別字看起來像不像一條路徑」——像就回空字串。
   ⚠ 用在**遮蔽匯出**的 `sid`／`anchor`（見 `bkExport`）。
   ⚠ 判準刻意比 `bkSafeRel` 窄：只認 `/`、`\` 與開頭的磁碟機代號。
   拿「有沒有 `:`」當判準會誤殺時間戳形狀的識別字，而那不是洩漏。
   ⚠⚠ **這道閘的正當性不建立在「`sid` 來自哪裡」，而是建立在「像路徑就遮」。**
   （曾經寫成「`sid` 的來源是檔名主幹，任何平台上都不會含分隔符」——**那對 Codex 不成立**：
   Claude 的 `session_id` 確實是 `path.stem`，但 Codex 的來自 **JSONL 檔內**的
   `session_meta.payload.id`，那是資料不是檔名。實務上兩者都是 uuid，所以閘不會誤殺，
   但**別拿那個推理當放寬這道閘的理由**——store 裡的值本來就可能是手改的。）
   ⚠ 這裡**不做 trim、不做長度截斷**：那些在 `bkLoad`／`bkIdPart` 已經做過，
   在這裡重做只會讓「哪一層才承重」變模糊。 */
function bkSafePart(s){
 s=String(s==null?'':s);
 if(s.indexOf('/')>=0||s.indexOf('\\')>=0)return '';
 if(/^[A-Za-z]:/.test(s))return '';
 return s;}

/* ── 類別 ──────────────────────────────────────────────────────────
   來源有兩個：**設定頁管理的清單**（有順序、可含沒被用到的）與**書籤實際用到的**。
   ⚠⚠ 合起來才是完整清單。只認設定頁那一份的話，就退化成「只能從 Settings 選」
   ——那正是 Will 自己點出的那個問題（`bookmarks-proposal.md`〈類別的裁決〉）。 */
function bkCatsUsed(st){
 var seen=Object.create(null),out=[],i,c;
 for(i=0;i<st.items.length;i++){c=(st.items[i].cat||'').replace(/^\s+|\s+$/g,'');
  if(c&&!seen[c]){seen[c]=1;out.push(c);}}
 return out;}
function bkCatsAll(st){
 /* 順序＝設定頁的手動順序在前，其餘（只有書籤在用的）依字典序接在後面。
    ⚠ 這是**設定頁與管理頁篩選列**用的順序；對話窗另外照「最近用過」排（見下）。 */
 var out=bkCatsClean(bkSet().cats), seen=Object.create(null), used, i;
 for(i=0;i<out.length;i++)seen[out[i]]=1;
 used=bkCatsUsed(st).slice().sort();
 for(i=0;i<used.length;i++)if(!seen[used[i]]){seen[used[i]]=1;out.push(used[i]);}
 return out;}
function bkCatsRecent(st){
 /* 「最近用過的排前面」：用每個類別最後一次被存下的時間排。
    ⚠ 這個順序**只給對話窗用**——現場加書籤要快，不該要求 Will 手動維護顯示順序。
    Array.sort 是穩定的，所以同分時仍照 bkCatsAll 的順序。 */
 /* ⚠ 這裡要用 `mt`（最後修改）不是 `ct`（建立）：`ct` 只在新建時寫，
    把一筆舊書籤改成類別 X 之後 X 不會往前排——那和「最近用過」這個名字不符。
    ⚠ 剛用「＋ 新類別」建出來、還沒有任何書籤的類別沒有「用過」的時刻可用，
    但**剛建好的下一秒就被收進「更多…」是最糟的**。
    ⚠⚠ 舊版把**所有**沒被用到的類別都給 `Infinity`：於是它們全部同分，
    穩定排序保留 `bkCatsAll` 的順序（設定頁的手動順序），而新類別是**接在尾端**的
    ⇒ 已經有六個未使用類別時，剛建好的那顆排第七、直接被藏起來（跨模型 Low#10）。
    ⚠ 而舊探針只造了**一個**未使用類別，所以它必定排第一——**那一格是空心的**。
    改法：用 `cta`（什麼時候建的）當它的時刻，和「什麼時候用過」**同一把尺**比。
    沒有建立時刻的（舊資料、匯入來的）給 0：那種類別本來就不算「最近」。
    守它的是 `just_created_cat_ranks_first`／`reused_cat_moves_up`
    ＋`new_cat_visible_among_many_unused`（六個未使用類別的那一格）。 */
 var last=Object.create(null),i,c,s=bkSet(),setc=s.cats,used=Object.create(null);
 for(i=0;i<st.items.length;i++){c=(st.items[i].cat||'').replace(/^\s+|\s+$/g,'');
  if(c){used[c]=1;if(bkWhen(st.items[i])>(last[c]||0))last[c]=bkWhen(st.items[i]);}}
 for(i=0;i<setc.length;i++)if(!used[setc[i]])last[setc[i]]=bkNum(s.cta[setc[i]],0);
 return bkCatsAll(st).slice().sort(function(a,b){return (last[b]||0)-(last[a]||0);});}
function bkCatCount(st,c){
 var n=0,i;
 for(i=0;i<st.items.length;i++)if((st.items[i].cat||'')===c)n++;
 return n;}

/* ── 對話窗（三頁共用）─────────────────────────────────────────────── */
function bkModalEl(){return document.getElementById('bkModal');}
function bkClose(){var m=bkModalEl();if(m)m.classList.remove('open');BK_EDIT=null;}
function bkBackdrop(ev){if(ev.target===bkModalEl())bkClose();}
function bkSay(m){var e=document.getElementById('bkMsg');if(e&&m)e.textContent=m;}
/* 「全部書籤」＝離開編輯畫面、回到清單。預設就是把窗關掉（管理頁的清單本來就在窗後面）；
   ⚠ session 頁**覆寫**成打開「這一頁的書籤」清單——那一頁沒有別的清單可以回。 */
function bkList(){bkClose();}

/* 類別 chip：一排可點的 chip ＋ 最後固定一顆「＋ 新類別」（Will 2026-08-22 裁決）。
   ⚠ **新增就地做、整理才去設定頁**：現場想到新類別時跑一趟設定頁太麻煩。 */
function bkCatChips(cur){
 var st=bkLoad(), all=bkCatsAll(st), rec=bkCatsRecent(st);
 var show=[], seen=Object.create(null), h='', rest=0, i, c;
 /* ⚠⚠ 目前選著的那一顆**一定要看得見**，否則使用者會以為類別被清掉了。
    ⚠ 這條保險**收合與展開共用**，一定要放在分岔之前。原本只寫在收合分支，
    於是展開之後、選著的那顆若不在聯集裡（帶空白的、被 60 字截斷的、
    設定清單已滿 200 的）就整個消失——畫面看起來「沒有選類別」，
    但 `BK_CAT` 還在、一存就把它寫回去。
    守它的是探針的 `unlisted_cat_still_visible_when_expanded`／`_collapsed`。 */
 if(cur){show.push(cur);seen[cur]=1;}
 if(BK_MORE){for(i=0;i<all.length;i++)if(!seen[all[i]]){seen[all[i]]=1;show.push(all[i]);}}
 else{
  for(i=0;i<rec.length&&show.length<BK_CATN;i++)
   if(!seen[rec[i]]){seen[rec[i]]=1;show.push(rec[i]);}}
 for(i=0;i<all.length;i++)if(!seen[all[i]])rest++;
 for(i=0;i<show.length;i++){c=show[i];
  h+='<button type="button" class="bm-chip cat'+(c===cur?' on':'')+'"'
    +' data-c="'+bkEsc(c)+'" aria-pressed="'+(c===cur?'true':'false')+'"'
    +' onclick="bkCatPick(this)">'+bkEsc(c)+'</button>';}
 if(rest)h+='<button type="button" class="bm-chip more" id="bkCatMore"'
           +' onclick="bkCatMore()">\u66f4\u591a\u2026 ('+rest+')</button>';
 h+='<button type="button" class="bm-chip add" id="bkCatAdd"'
   +' onclick="bkCatNew()">\uff0b \u65b0\u985e\u5225</button>';
 return h;}
function bkCatDraw(){
 var g=document.getElementById('bkCats');
 if(g)g.innerHTML=bkCatChips(BK_CAT);}
function bkCatPick(btn){
 var c=btn.getAttribute('data-c');
 BK_CAT=(BK_CAT===c)?'':c;     /* 再點一次＝取消選取 */
 bkCatDraw();}
function bkCatMore(){BK_MORE=true;bkCatDraw();}
/* 「＋ 新類別」按下去**就地**變輸入框，Enter 建好並選起來，全程不離開對話窗。 */
function bkCatNew(){
 var b=document.getElementById('bkCatAdd');
 if(!b||!b.parentElement)return;
 var wrap=document.createElement('span');
 wrap.className='bm-newcat';
 var inp=document.createElement('input');
 inp.type='text'; inp.id='bkCatNew'; inp.maxLength=60;
 inp.setAttribute('aria-label','\u65b0\u985e\u5225\u540d\u7a31');
 /* ⚠⚠ Esc 只取消這個輸入框，**不可以往上冒泡把整個對話窗關掉**——
    那會連使用者剛打好的備註一起弄丟。守它的是探針的 `esc_keeps_note`
    （同一組還有 `esc_closes_only_the_input`／`esc_keeps_modal_open`）。 */
 inp.onkeydown=function(ev){
  if(ev.key==='Enter'){ev.preventDefault();bkCatAdd();}
  else if(ev.key==='Escape'){ev.stopPropagation();ev.preventDefault();bkCatDraw();}};
 var okb=document.createElement('button');
 okb.type='button'; okb.className='bm-chip ok'; okb.id='bkCatOk';
 okb.textContent='\u2713'; okb.onclick=bkCatAdd;
 wrap.appendChild(inp); wrap.appendChild(okb);
 b.parentElement.replaceChild(wrap,b);
 inp.focus();}
function bkCatAdd(){
 var inp=document.getElementById('bkCatNew');
 if(!inp)return;
 var c=String(inp.value==null?'':inp.value).replace(/^\s+|\s+$/g,'').slice(0,60);
 if(!c){bkCatDraw();return;}
 /* 順手寫進設定的類別清單，設定頁那邊立刻看得到、可以改名／排序。
    ⚠ 同時記下**建立時刻**（`cta`）：`bkCatsRecent` 靠它讓剛建好的那顆排第一，
    否則六個未使用類別在前時它會被藏進「更多…」（跨模型 Low#10）。 */
 var s=bkSet(), i, has=false;
 for(i=0;i<s.cats.length;i++)if(s.cats[i]===c)has=true;
 if(!has){s.cats.push(c);s.cta[c]=Date.now();
          if(!bkSaveSet(s))bkSay(bkWhySetFail());}
 BK_CAT=c; BK_MORE=true; bkCatDraw();}

/* 複查間隔 chip（單選）。 */
function bkSpanChips(span){
 var h='',i;
 for(i=0;i<BK_SPANS.length;i++)
  h+='<button type="button" class="bm-chip" role="radio" data-v="'+bkEsc(BK_SPANS[i][0])+'"'
    +' aria-checked="'+(BK_SPANS[i][0]===span?'true':'false')+'"'
    +' onclick="bkPick(this)">'+bkEsc(BK_SPANS[i][1])+'</button>';
 return h;}
function bkPick(btn){
 var all=document.getElementById('bkSpans').querySelectorAll('.bm-chip');
 for(var i=0;i<all.length;i++)all[i].setAttribute('aria-checked','false');
 btn.setAttribute('aria-checked','true');}
function bkPicked(){
 var g=document.getElementById('bkSpans');
 if(!g)return '0';
 var all=g.querySelectorAll('.bm-chip');
 for(var i=0;i<all.length;i++)
  if(all[i].getAttribute('aria-checked')==='true')return all[i].getAttribute('data-v');
 return '0';}

/* ── 編輯畫面（session 頁與管理頁走同一個）──────────────────────────
   `seed` ＝ {id,sid,anchor,url,stitle,town,sum}。已經存在的那一筆以 store 為準，
   只有 `url`／`stitle`／`town` 這三個**快取欄位**吃 seed 給的新值
   ——session 頁每次開都給當下這份輸出的值（檔名會變，見〈網址本身不耐久〉）；
   管理頁沒有那些資訊，就把記錄自己存的值原樣傳回來，不會把它洗掉。 */
function bkEditRec(seed){
 var st=bkLoad(), rec=bkFindId(st,seed.id), card=document.getElementById('bkCard');
 if(!card)return false;
 var span=rec?(rec.span||'0'):bkDefSpan();
 /* ⚠ 編輯既有書籤時**沿用當初存的摘要，不重抓**。摘要的用處是
    「跳過去之後，這一輪還是不是我當初存的那一輪」——重抓就把那個對照弄丟了。 */
 var sum=(rec&&rec.sum)?rec.sum:(seed.sum||'');
 BK_CAT=rec?(rec.cat||''):(seed.cat||'');
 BK_MORE=false;
 card.innerHTML=
  '<h3 id="bkTitle">'+(rec?'\u7de8\u8f2f\u66f8\u7c64':'\u52a0\u66f8\u7c64')+'</h3>'
 +'<div class="bm-lab">\u9019\u4e00\u8f2a\uff08\u6703\u4e00\u8d77\u5b58\u9032\u66f8\u7c64\uff0c'
 +'\u4e4b\u5f8c\u5728\u7ba1\u7406\u9801\u8a8d\u5f97\u51fa\u662f\u54ea\u4e00\u6bb5\uff09</div>'
 +'<div class="bm-prev">'+(bkEsc(sum)||'\uff08\u9019\u4e00\u8f2a\u6c92\u6709\u6587\u5b57\u5167\u5bb9\uff09')+'</div>'
 +'<label class="bm-lab" for="bkNote">\u5099\u8a3b</label>'
 +'<textarea id="bkNote">'+bkEsc(rec?rec.note:'')+'</textarea>'
 +'<div class="bm-lab">\u985e\u5225<span class="bmi-sum"> \u00b7 '
 +'\u9ede\u4e00\u4e0b\u9078\u8d77\u4f86\uff0c\u518d\u9ede\u4e00\u6b21\u53d6\u6d88'
 +'\uff1b\u6539\u540d\u8207\u6574\u7406\u5728\u8a2d\u5b9a\u9801</span></div>'
 +'<div class="bm-chips" id="bkCats">'+bkCatChips(BK_CAT)+'</div>'
 +'<div class="bm-lab">\u63d0\u9192\u6211\u8907\u67e5<span class="bmi-sum"> \u00b7 '
 +'\u5230\u671f\u53ea\u6703\u6a19\u8a18\uff0c\u66f8\u7c64\u6c38\u9060\u4e0d\u6703\u81ea\u5df1\u6d88\u5931</span></div>'
 +'<div class="bm-chips" id="bkSpans" role="radiogroup" aria-label="\u63d0\u9192\u6211\u8907\u67e5">'
 +bkSpanChips(span)+'</div>'
 +'<div class="bm-act"><button class="pri" onclick="bkCommit()">'
 +(rec?'\u5132\u5b58\u8b8a\u66f4':'\u52a0\u5165\u66f8\u7c64')+'</button>'
 +(rec?'<button class="dang" onclick="bkRemove()">\u79fb\u9664\u66f8\u7c64</button>':'')
 +'<span class="grow"></span>'
 +'<button onclick="bkList()">\u5168\u90e8\u66f8\u7c64</button>'
 +'<button onclick="bkClose()">\u53d6\u6d88</button></div>';
 /* ⚠ `span0` ＝開窗時的間隔。`bkCommit` 靠它分辨「使用者真的動了 chip」與「只改了備註」
    ——只有前者才該重算到期日，見那裡的說明。
    ⚠⚠ `mt0` ＝**開窗那一刻這一筆的修改時刻**。四個頁面吃同一份 localStorage，
    開著窗的時候別的分頁可能改過同一筆；沒有 `mt0` 就沒有任何辦法察覺
    ⇒ 存回去等於拿開窗時的表單**整筆覆寫**掉對方剛寫的東西（跨模型 Medium#8）。
    `null` ＝這一筆本來就不存在（新建），那沒有衝突可言。
    ⚠⚠ **光有 `mt0` 不夠。** `bkMerge()` 補缺欄位（`cat`／`url`／`due`）時
    **不會動 `mt`**——那是刻意的（`mt` 是合併排序用的尺，一動就會改變誰比較新）。
    於是「別的分頁匯入了一份較舊的備份、補上了本機空著的類別」這條路，
    `bkWhen(rec)` 完全沒變 ⇒ 開著的窗察覺不到 ⇒ 一存就把剛補進來的值洗掉，
    而且不會有衝突提示。所以另記一把**只在本機用**的尺 `lr`（local revision）：
    任何實際改動都遞增，**不匯出、也絕不參與合併比較**（和 `im` 同一個原則）。
    守它的是探針的 `import_fill_is_seen_as_clash`。 */
 BK_EDIT={id:seed.id,sid:seed.sid,anchor:seed.anchor,url:seed.url||'',
          stitle:seed.stitle||'',town:seed.town?1:0,sum:sum,span0:span,
          mt0:(rec?bkWhen(rec):null),
          lr0:(rec?bkNum(rec.lr,0):0)};
 bkModalEl().classList.add('open');
 var n=document.getElementById('bkNote'); if(n)n.focus();
 return false;}

function bkCommit(){
 if(!BK_EDIT)return;
 var st=bkLoad();
 /* ⚠ store 壞掉時**先擋在這裡**：讓使用者知道原因是「讀不到」，
    而不是走到下面才被 `bkStore` 拒掉、看到一句像是配額不足的訊息。 */
 if(st.bad){bkRefresh(BK_BADMSG);return;}
 var rec=bkFindId(st,BK_EDIT.id), now=Date.now(), isNew=!rec;
 var span=bkPicked();
 var note=(document.getElementById('bkNote')||{}).value||'';
 /* ⚠⚠ 開窗到現在，**別的分頁可能改過同一筆**（跨模型 Medium#8）。
    偵測到就把兩邊的備註都留著——和匯入衝突走**同一個 `bkKeepLost()`**，
    不要在這裡另寫一套（同一條規則寫兩次就會分岔）。 */
 var clash=(!isNew&&BK_EDIT.mt0!==null
            &&(bkWhen(rec)!==BK_EDIT.mt0||bkNum(rec.lr,0)!==BK_EDIT.lr0));
 /* ⚠⚠ **類別也要保留輸家，不是只有備註。** 上面那句「和匯入衝突走同一個
    `bkKeepLost()`，不要在這裡另寫一套」寫對了原則，實作卻只做了 `note` 那半
    ——`bkMerge()` 對同一件事的處置是「一筆只能掛一個類別，所以本機那個即將被
    取代的名字要寫進備註，不然它就真的沒了」，這裡漏了 ⇒ 另一個分頁選的類別
    **無聲消失、不留痕跡**。這正是「同一條規則寫兩次就會分岔」自己應驗了。
    守它的是探針的 `concurrent_edit_keeps_lost_cat`。 */
 /* ⚠⚠ **`rec.note!==note` 那個守門條件也要抄過來。** `bkMerge` 那半寫的是
    `if(inc.note&&inc.note!==cur.note)`——**兩邊一樣就不記**，因為那時候
    「沒有任何值即將消失」。這裡漏掉它的後果是：別的分頁改的是**別的欄位**
    （匯入補缺欄位、設定頁改類別名，這幾條路都會動 `lr`）而備註兩邊完全相同時，
    使用者一個字都沒改按下儲存，備註就被貼上自己一份，**每觸發一次長一截**
    （實測 48→111→237→489 字，唯一的煞車是 `bkLoad` 的 20000 字截斷）。
    那直接和「同一份檔案匯入兩次結果必須相同」那條不變量打架。
    ⚠ 這正是「同一條規則寫兩次就會分岔」第二次應驗——上一次漏的是 `cat`（就在下一行），
    這一次漏的是 `note` 的守門條件。**兩半的形狀現在一致了，改一邊要改兩邊。**
    守它的是探針的 `clash_does_not_duplicate_unchanged_note`，
    對照組 `clash_still_keeps_really_lost_note`（真的有東西要消失時仍要保留）。 */
 if(clash){
  if(rec.note&&rec.note!==note)note=bkKeepLost(note,rec.note);
  if(rec.cat&&rec.cat!==BK_CAT)note=bkKeepLost(note,rec.cat);}
 if(isNew){rec={id:BK_EDIT.id,sid:BK_EDIT.sid,anchor:BK_EDIT.anchor,ct:now};st.items.push(rec);}
 /* URL／標題只是快取，每次存都刷新成呼叫端給的那一份 */
 rec.url=BK_EDIT.url; rec.stitle=BK_EDIT.stitle; rec.town=BK_EDIT.town;
 rec.note=note; rec.cat=BK_CAT; rec.sum=BK_EDIT.sum;
 rec.span=span;
 /* ⚠⚠ **只有新建、或使用者真的動了間隔 chip 時才重算到期日。**
    一律用 `now` 重算的話，改一個錯字就等於把複查時鐘重新上緊：一筆已經到期的書籤
    被編輯之後就不再到期，索引頁的 ⏰、管理頁的「只看該複查的」、星星的 tooltip
    全部一起變回正常，而畫面上沒有任何地方講出到期日被改了
    ——那等於把「提醒我複查」這個功能本身抵銷掉。
    守它的是探針的 `edit_keeps_due`／`edit_still_overdue`，
    對照組是 `changing_span_recomputes_due`（真的改了 chip 就要重算）。 */
 if(isNew||span!==BK_EDIT.span0)rec.due=bkDue(span,now);
 /* ⚠⚠ **`span` 與 `due` 是不可拆的一對。** 上面那個條件只在「新建／真的動了 chip」
    時重算，於是分頁衝突時會留下**另一個分頁設的 `due` ＋ 這一頁寫回去的 `span`**：
    chip 上寫著「不提醒」，`bkOverdue()` 卻只看 `due` ⇒ 索引頁的 ⏰ 亮著、
    管理頁的「只看該複查的」抓得到它，而畫面上沒有任何地方解釋得通。
    ⚠ 這裡**不用 `now` 重算**：那會把複查時鐘重新上緊（`edit_keeps_due` 正是在守
    這件事）。只要守住「關掉提醒就不可以留著到期日」這個方向就夠了。
    守它的是探針的 `clash_no_span_due_mismatch`。 */
 if(rec.span==='0')rec.due=0;
 /* `mt` ＝最後修改時刻。⚠ 合併匯入檔時比的是它，不是 `ct`：`ct` 只在新建時寫，
    同一筆在兩台機器上永遠相等 ⇒ 沒有 `mt` 就分不出誰比較新。 */
 rec.mt=now;
 rec.lr=bkNum(rec.lr,0)+1;   /* 本機修訂號，見 bkEditRec 的 lr0 */
 /* ⚠ 寫失敗就**不可以**說「已加入書籤」——那句話會讓使用者不再去按匯出。 */
 if(!bkStore(st)){bkRefresh(bkWhyFail(st));return;}
 bkRefresh((isNew?'\u5df2\u52a0\u5165\u66f8\u7c64\u3002':'\u5df2\u5132\u5b58\u8b8a\u66f4\u3002')
          +(clash?('\u26a0 \u9019\u4e00\u7b46\u5728\u5225\u7684\u5206\u9801\u4e5f\u88ab'
                  +'\u6539\u904e\uff0c\u5169\u908a\u7684\u5099\u8a3b\u90fd\u7559\u4e0b\u4f86\u4e86'
                  +'\uff0c\u8acb\u81ea\u5df1\u770b\u4e00\u4e0b\u3002'):'')
          +'\u26a0 localStorage \u6e05\u6389\u5c31\u6c92\u4e86\uff0c'
          +'\u771f\u6b63\u7684\u5099\u4efd\u662f\u532f\u51fa\u7684 JSON\u3002');}

/* ⚠ 移除不跳 confirm()：瀏覽器 modal 會卡住整頁，而且「按錯」的代價是打好的備註消失。
   改成移除後留一顆「復原」——可逆比確認框好。 */
function bkRemove(){if(BK_EDIT)bkDrop(BK_EDIT.id);}
function bkDrop(id){
 var st=bkLoad();
 if(st.bad){bkRefresh(BK_BADMSG);return;}
 var out=[], hit=null, i;
 for(i=0;i<st.items.length;i++){
  if(st.items[i].id===id)hit=st.items[i]; else out.push(st.items[i]);}
 if(!hit)return;                 /* \u627e\u4e0d\u5230\u5c31\u4ec0\u9ebc\u90fd\u4e0d\u8981\u52d5\uff08\u4e5f\u4e0d\u8981\u767d\u5beb\u4e00\u6b21\uff09 */
 st.items=out;
 if(!bkStore(st)){bkRefresh(bkWhyFail(st));return;}
 /* \u26a0 **\u5beb\u5165\u6210\u529f\u4e4b\u5f8c\u624d\u52d5 `BK_UNDO`**\uff1a\u5148\u8a2d\u7684\u8a71\uff0c\u5beb\u5931\u6557\u6642\u756b\u9762\u4e0a\u6703\u591a\u4e00\u9846
    \u5c0d\u4e0d\u4e0a\u5be6\u969b\u72c0\u614b\u7684\u300c\u5fa9\u539f\u300d\u2014\u2014\u6309\u4e0b\u53bb\u53cd\u800c\u628a\u4e00\u7b46\u9084\u5728\u7684\u66f8\u7c64\u53c8\u63a8\u4e00\u6b21\u3002 */
 BK_UNDO=hit;
 bkRefresh('\u5df2\u79fb\u9664\u3002');}
function bkUndo(){
 if(!BK_UNDO)return;
 var st=bkLoad();
 if(st.bad){bkRefresh(BK_BADMSG);return;}
 if(!bkFindId(st,BK_UNDO.id))st.items.push(BK_UNDO);
 /* \u26a0\u26a0 **\u5beb\u5165\u6210\u529f\u4e86\u624d\u53ef\u4ee5\u6e05\u6389 `BK_UNDO`**\uff08\u8de8\u6a21\u578b Medium#6\uff09\u3002
    \u5148\u6e05\u7684\u8a71\u5beb\u5165\u4e00\u5931\u6557\uff0c\u552f\u4e00\u9084\u539f\u5f97\u56de\u4f86\u7684\u90a3\u4efd\u8cc7\u6599\u5c31\u6c92\u4e86\u3001\u800c\u4e14**\u7121\u6cd5\u91cd\u8a66**
    \u2014\u2014\u90a3\u9846\u300c\u5fa9\u539f\u300d\u9215\u4e5f\u8ddf\u8457\u6d88\u5931\uff0c\u4f7f\u7528\u8005\u9023\u518d\u6309\u4e00\u6b21\u7684\u6a5f\u6703\u90fd\u6c92\u6709\u3002
    \u5b88\u5b83\u7684\u662f\u63a2\u91dd\u7684 `undo_survives_write_failure`\uff0f`undo_retry_restores`\u3002 */
 if(!bkStore(st)){bkRefresh(bkWhyFail(st));return;}
 BK_UNDO=null;
 bkRefresh('\u5df2\u5fa9\u539f\u3002');}

/* ── 匯出／匯入那一排（**和存檔鈕同一批出貨**）────────────────────
   ⚠⚠ 真正的耐久保證是匯出的 JSON，不是 localStorage：第一顆書籤存下去的那一刻，
   資料就只在瀏覽器裡，一次「清除瀏覽資料」就沒了。所以這一排**就放在存完之後
   會看到的那一頁**，不是藏在別處。 */
function bkFoot(st){
 /* \u26a0\u26a0 store \u58de\u6389\u6642\uff0cfooter \u8981**\u540c\u6642**\u505a\u5169\u4ef6\u4e8b\uff1a\u8b1b\u51fa\u4f86\uff0c\u4e26\u7d66\u552f\u4e00\u9084\u6709\u6551\u7684\u52d5\u4f5c\u3002
    \u53ea\u8b1b\u4e0d\u7d66\u8def\uff0c\u4f7f\u7528\u8005\u80fd\u505a\u7684\u5c31\u53ea\u5269\u300c\u6e05\u6389\u91cd\u4f86\u300d\u2014\u2014\u90a3\u6b63\u662f\u628a\u8cc7\u6599\u5f04\u4e1f\u7684\u90a3\u689d\u8def\u3002 */
 if(st&&st.bad)
  return '<div class="bm-foot"><span class="bmi-due">\u26a0\u26a0 '
   +'\u8b80\u4e0d\u5230\u73fe\u6709\u7684\u66f8\u7c64\uff08\u8cc7\u6599\u58de\u4e86\uff09'
   +'\uff0c\u5df2\u505c\u6b62\u6240\u6709\u5beb\u5165\u4ee5\u514d\u8f3e\u6389\u539f\u59cb\u8cc7\u6599'
   +'\u3002</span><span class="grow"></span>'
   +'<button class="dang" onclick="bkRescue()">\u26d1 '
   +'\u532f\u51fa\u539f\u59cb\u8cc7\u6599</button>'
   +'</div><div class="bm-msg" id="bkMsg"></div>';
 return '<div class="bm-foot"><span>\u5171 '+st.items.length
  +' \u7b46\uff08\u6240\u6709 session\uff09</span>'
  /* \u26a0 \u63aa\u8fad\u8981\u548c `bkExport` \u5be6\u969b\u505a\u7684\u4e8b\u4e00\u81f4\uff1a\u5b8c\u6574\u5099\u4efd**\u539f\u6a23\u5e36\u8457**\u5b83\u5011\uff0c
     \u4f46\u532f\u5165\u7aef\u6703\u7565\u904e\uff08\u6c92\u6709 sid\uff0fanchor\uff09\u21d2 \u4fdd\u7684\u662f\u300c\u4f60\u7684\u5b57\u4e0d\u6703\u6d88\u5931\u300d\uff0c\u4e0d\u662f\u300c\u4e00\u9375\u9084\u539f\u300d\u3002
     \u8b1b\u6210\u300c\u5c31\u80fd\u62ff\u5230\u300d\u6703\u8b93\u4eba\u4ee5\u70ba\u9084\u539f\u5f97\u56de\u4f86\uff0c\u90a3\u662f\u53e6\u4e00\u7a2e\u300c\u8a0a\u606f\u548c\u4e8b\u5be6\u76f8\u53cd\u300d\u3002 */
  +(st&&st.drop?('<span class="bmi-due">\uff08\u53e6\u6709 '+st.drop
                 +' \u7b46\u8a8d\u4e0d\u51fa\u5f62\u72c0\uff0c\u756b\u9762\u4e0a\u4e0d\u986f\u793a\uff0c\u4f46\u5df2\u539f\u6a23\u4fdd\u7559\u3001\u4e0d\u6703\u88ab\u5beb\u6389\uff1b'
                 +'\u300c\u2b07 \u5b8c\u6574\u5099\u4efd\u300d\u6703\u539f\u6a23\u5e36\u8457\u5b83\u5011'
                 +'\uff08\u532f\u5165\u4e0d\u6703\u9084\u539f\uff0c\u8981\u81ea\u5df1\u5f9e\u6a94\u6848\u88e1\u6488\uff09\uff09</span>'):'')
  +(BK_MGR?('<a class="bm-mgr" href="'+bkEsc(BK_MGR)+'">🔖 \u7ba1\u7406\u9801</a>'):'')
  +'<span class="grow"></span>'
  +'<button onclick="bkExport(1)" title="\u542b\u6bcf\u4e00\u8f2a\u7684 120 \u5b57\u6458\u8981'
  +'\u2014\u2014\u90a3\u662f\u771f\u7684\u5c0d\u8a71\u5167\u5bb9">\u2b07 \u5b8c\u6574\u5099\u4efd</button>'
  +'<button onclick="bkExport(0)" title="\u4e0d\u542b\u5c0d\u8a71\u6458\u8981\u8207\u81ea\u52d5'
  +'\u6a19\u984c\uff0c\u53ef\u4ee5\u62ff\u7d66\u5225\u4eba">\u2b07 \u53ea\u532f\u66f8\u7c64</button>'
  +'<button onclick="bkPickFile()">\u2b06 \u532f\u5165</button>'
  +'<input type="file" id="bkFile" accept="application/json,.json" style="display:none"'
  +' onchange="bkImport(this)"></div><div class="bm-msg" id="bkMsg"></div>';}

/* 遮蔽規則（2026-08-22 設計諮詢，方向和初稿相反）：
   · **備註不遮** ——那是使用者自己知情打進去的字，而他按的是「備份」。
     **有損的備份是壞掉的備份**，這比洩漏風險更該優先。
   · **摘要要遮** ——那是真的對話內容，他不見得意識到被一起存進去了。
   · **自動標題也要遮** ——`s.title` 沒改名時就等於「第一則使用者訊息」，一樣是對話內容；
     使用者自己改過名的（town=1）才留著。
   · **路徑要過形狀閘** ——⚠⚠ 原本這裡寫的是「`url` 本來就是相對路徑，沒有絕對路徑
     可洩漏」。**那句話對「本機自己存的」成立，對「匯入進來的」不成立**
     （跨模型 High#1）：匯入端原樣收下 `url`，遮蔽匯出又無條件把它吐回去。
     現在匯入端擋一次（`bkSafeRel`）、這裡再擋一次——**入口與出口各一道**，
     因為「只寫一條分支」正是這條線上重複踩的那個坑。
   ⚠ **設定也要一起匯出**（含類別清單）：只在 localStorage 裡＝一次「清除瀏覽資料」就沒了。 */
function bkExport(full){
 var st=bkLoad(), out=[], i;
 /* ⚠ 壞掉的 store 匯出來會是一份**空的備份**，而使用者會以為備份好了
    ——那比不匯出更糟。導去救援那條路。 */
 if(st.bad){bkSay(BK_BADMSG);return;}
 for(i=0;i<st.items.length;i++){var x=st.items[i];
  /* ⚠ `mt` 一定要一起匯出：匯入端靠它分辨誰比較新（`ct` 只在新建時寫，兩台會永遠相等）。
     ⚠ `im`（什麼時候匯進來的）**不匯出**：那是本機的簿記，對別台沒有意義。 */
  var o={id:x.id,sid:x.sid,anchor:x.anchor,url:bkSafeRel(x.url),
         note:x.note||'',cat:x.cat||'',
         ct:x.ct||0,mt:bkWhen(x),span:x.span||'0',due:x.due||0,town:x.town?1:0};
  /* ⚠⚠ **`id`／`sid`／`anchor` 也要有出口閘**（收斂確認輪 Medium）。上面那道
     `bkSafeRel` 把 `url` 顧到了，但這三個欄位**入口與出口都沒有任何形狀檢查**：
     `bkIdPart` 只擋 `|`／空白／控制字元（那是對的——錨點文法 `k<ts>-tb<n>-s<ts>-b<n>`
     需要 `-`），`bkLoad` 又明著保留任何非空字串 `id`。於是手改過的 localStorage、
     別的工具寫的、舊版留下的紀錄，只要 `sid` 或 `id` 帶著絕對路徑，
     就從「可以拿給別人」的那一份原樣流出去，而 `_warning` 還寫著「相對路徑」。
     ⚠ 只在**遮蔽**那一份做（和 `stitle`／`town` 同一個道理）：完整備份本來就含私密。
     ⚠ 判準只認**真的像路徑**的（含 `/`、`\`，或開頭是磁碟機代號），不是任何 `:` ——
     時間戳形狀的識別字不該被誤殺。認不出來的一律遮成空字串：那一筆在別台會被
     `bkImport` 略過，而「少一筆別人本來就認不得的紀錄」遠比洩漏可回復。
     守它的是探針的 `redacted_export_gates_id_and_sid`，
     對照組 `redacted_export_still_keeps_note_after_gate`。 */
  if(!full){
   o.sid=bkSafePart(o.sid); o.anchor=bkSafePart(o.anchor);
   o.id=(o.sid&&o.anchor)?bkId(o.sid,o.anchor):'';}
  /* ⚠⚠ **遮蔽匯出一律不留 `stitle`，不看 `town`。**
     舊寫法是 `(full||x.town)`＝**無條件相信紀錄自己宣稱的 `town`**。匯入端雖然
     把 `town` 歸零了（`bkImport`），但那只擋住「從匯入進來」那一條路：
     手改過的 localStorage、別的工具寫的、或**這次修正之前那版**匯入寫進去的資料，
     都帶著 `town:1` 躺在 store 裡，於是一個由**第一則使用者訊息**衍生的標題
     就從「只匯書籤」流出去了。註解當時寫「入口與出口各一道」——那對 `url` 成立
     （`bkSafeRel` 在這裡再驗一次），對 `town`/`stitle` **從來沒有出口那一道**。
     ⚠ 為什麼不做「出口交叉驗證」而是直接遮掉：靜態頁沒有任何**不可偽造**的本機
     所有權證據可用——`town` 就在使用者可改的 localStorage 裡，怎麼比都是拿它自己
     證明它自己。而這是隱私保證（最高不變量 ①），**認不出來時寧可多遮**。
     代價：使用者自己 `/rename` 過的標題不再出現在遮蔽匯出裡（完整備份仍然有）。
     那是可回復的——回到那一場的頁面開一次書籤，`stitle` 就刷新回來；
     而洩漏出去是不可回復的。範圍限制 SCOPE-BOOKMARK-REDACTED-DROPS-ALL-TITLES。
     守它的是探針的 `red_masks_all_titles`／`red_drops_town_claim`，
     對照組是 `full_keeps_own_title`（完整備份仍要帶著自訂標題）。 */
  o.stitle=full?(x.stitle||''):'';
  if(!full)o.town=0;   /* 連宣稱本身都不要帶出去，免得下一版又有人拿它當依據 */
  if(full)o.sum=x.sum||'';
  out.push(o);}
 var pay={_warning:full
   ?'\u542b\u79c1\u5bc6\u5c0d\u8a71\u5167\u5bb9\uff08\u6bcf\u7b46\u5e36 120 \u5b57\u56de\u5408\u6458\u8981'
    +'\u8207\u81ea\u52d5\u6a19\u984c\uff09\uff0c\u52ff\u63d0\u4ea4\u7248\u63a7'
   :'\u542b\u4f60\u81ea\u5df1\u5beb\u7684\u5099\u8a3b\u8207\u76f8\u5c0d\u8def\u5f91\uff1b'
    +'\u5df2\u79fb\u9664\u5c0d\u8a71\u6458\u8981\u8207\u6240\u6709 session \u6a19\u984c',
  v:1,redacted:full?0:1,exported:new Date().toISOString(),
  settings:bkSet(),items:out};
 /* ⚠⚠ **認不出形狀的那些（`kept`）也要進完整備份。** `bkStore` 已經把它們寫回
    localStorage 了（`unreadable_record_survives_write` 在守），但這個函式只跑
    `st.items` ⇒ footer 那句「用『⬇ 完整備份』就能拿到」**是假的**；
    而那個狀態下 `st.bad` 是 false ⇒ 畫面上不會出現「⛑ 匯出原始資料」那顆鈕，
    唯一真的救得到的路使用者**按不到**。UI 一路在勸「真正的備份是匯出的 JSON」，
    照做之後清掉 localStorage，那些備註就真的沒了——那和「書籤永遠不會自動刪」牴觸。
    ⚠ 放在**自己的鍵**、不混進 `items`：`items` 的每一筆都被匯入端假設有
    `sid`／`anchor`，混進去只會讓對方的匯入報一堆「略過」。
    ⚠⚠ **遮蔽那一份絕對不帶**：`kept` 是原封不動的未知資料，裡面可能有絕對路徑、
    也可能有對話內容，而遮蔽那份的保證是「可以拿給別人」。
    ⚠ 誠實邊界：帶進備份**不等於**還原得回來——匯入端仍會略過它們（沒有 `sid`）。
    這一格保的是「**你的字不會消失**」，不是「一鍵還原」。footer 的措辭要跟著這一點。
    守它的是探針的 `full_export_carries_kept`／`redacted_export_drops_kept`。 */
 if(full&&st.kept&&st.kept.length)pay.kept=st.kept;
 if(bkDownload('asv-bookmarks-'+bkStamp()+(full?'':'-redacted')+'.json',
               JSON.stringify(pay,null,2)))
  bkSay('\u5df2\u532f\u51fa '+out.length+' \u7b46'
        +(full?'\uff08\u5b8c\u6574\uff09':'\uff08\u5df2\u906e\u8511\u5c0d\u8a71\u5167\u5bb9\uff09')+'\u3002');}

/* ⚠ `file://` 下也要能下載：Blob + createObjectURL + <a download>。
   ⚠ 一定要 revokeObjectURL——不撤的話每按一次就漏一整包 JSON 在記憶體裡。 */
function bkDownload(name,text){
 try{
  var u=URL.createObjectURL(new Blob([text],{type:'application/json'}));
  var a=document.createElement('a');
  a.href=u; a.download=name; a.style.display='none';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(function(){URL.revokeObjectURL(u);},2000);
  return true;}
 catch(e){bkSay('\u532f\u51fa\u5931\u6557\uff1a'+e);return false;}}

/* ⚠⚠ **一律合併，絕不整包覆蓋。** 這台可能有別台沒有的書籤，覆蓋＝無聲刪掉它們。
   ⚠ 欄位逐一挑（白名單），不要整包 assign——來源檔可能是別人給的。 */
function bkPickFile(){var f=document.getElementById('bkFile');if(f){f.value='';f.click();}}
function bkImport(inp){
 var f=inp&&inp.files&&inp.files[0];
 if(!f)return;
 var rd=new FileReader();
 rd.onload=function(){
  try{
   var o=JSON.parse(rd.result);
   var items=(o&&Array.isArray(o.items))?o.items:null;
   if(!items){bkSay('\u9019\u500b\u6a94\u770b\u4e0d\u51fa\u662f\u66f8\u7c64\u5099\u4efd'
                    +'\uff08\u627e\u4e0d\u5230 items \u9663\u5217\uff09\u3002');return;}
   var st=bkLoad(), by=Object.create(null), add=0, upd=0, skip=0, i;
   /* ⚠ store 壞掉時**整個匯入都不要開始**：合併是拿本機那份當基準的，
      基準讀不到卻照樣合併，等於把匯入檔當成全部（＝無聲覆蓋）。 */
   if(st.bad){bkRefresh(BK_BADMSG);return;}
   for(i=0;i<st.items.length;i++)by[st.items[i].id]=i;
   var nowI=Date.now();
   for(i=0;i<items.length;i++){
    var x=items[i];
    if(!x||typeof x!=='object'){skip++;continue;}
    /* ⚠⚠ **型別要真的是字串**，不可以 `String()` 硬轉——見 `bkIdPart` 的說明。
       `{sid:{},anchor:{}}` 原本會造出 id 為 `[object Object]|[object Object]`
       的幽靈書籤（跨模型 Medium#7）。 */
    var sidI=bkIdPart(x.sid), anchI=bkIdPart(x.anchor);
    if(!sidI||!anchI){skip++;continue;}
    /* ⚠⚠ **`id` 一律重算，不吃檔案裡的那個。** `id` 是可導出的（`sid|anchor`），
       信任它等於讓一份偽造／撞號的檔案**指定要取代本機哪一筆**——實測可以無聲刪掉
       不相干的書籤，也可以造出「標頭星星亮著、對話窗卻認不出來」的幽靈
       （`bkMark` 依 anchor 畫、`bkOpen` 依 sid|anchor 找，兩邊會對不起來）。
       守它的是探針的 `forged_id_does_not_replace`／`forged_id_recomputed`。 */
    var ridI=bkId(sidI,anchI);
    /* ⚠⚠ 缺漏／垃圾時間戳＝**未知（0）**，不是「匯入當下」。理由見 `bkTs`。 */
    var ctI=bkTs(x.ct,nowI), mtI=bkTs(x.mt,nowI)||ctI;
    var rec={id:ridI,sid:sidI,anchor:anchI,
             /* ⚠⚠ **`url` 一律過 `bkSafeRel()`**（跨模型 High#1）。匯入檔可以塞
                絕對路徑，而「只匯書籤」原本無條件把 `url` 再吐出去 ⇒「可以拿給別人」
                這個保證整個破掉，而且 `bkSafeRel()` 在這條路上**完全沒被用到**。
                對不到形狀就丟掉——那個路徑本來就是可以用 sid 重算的快取
                （見 `bxHref`），丟了不是資料損失。 */
             url:bkSafeRel(bkStr(x.url,400)),
             stitle:bkStr(x.stitle,400),
             /* ⚠⚠ **`town` 永遠不吃匯入端的值。** 它的意思是「這個標題是使用者自己
                取的」，而遮蔽匯出正是靠它決定要不要留 `stitle`。信任匯入端 ⇒ 別人
                （或自己舊機器）的檔可以把**自動標題**（＝第一則使用者訊息的內容）
                偽裝成自訂標題，再從「只匯書籤」流出去。
                只有本機 session 頁用 `BK_TOWN` 刷新過的才算數（見 `bkCommit`）：
                那台機器上真的有那份輸出，才有資格說這個標題是誰取的。
                ⚠ 代價講清楚：換機器還原時 `town` 退回 0，那筆的自訂標題在**遮蔽匯出**
                裡會被遮掉——直到你在該場 session 頁開一次書籤為止。
                保守的方向是對的：認不出來時寧可多遮，不可以少遮。
                範圍限制 SCOPE-BOOKMARK-IMPORTED-TITLE-OWNERSHIP：這個函式對
                「標題是誰取的」只認本機刷新過的那一份，詳見 planning/scope-limits.md。 */
             town:0,
             note:bkStr(x.note,20000),
             /* ⚠ 類別在**匯入這條路**也要正規化（trim ＋ 60 字，和 `bkCatsClean` 同一條規則）。
                不然會冒出「設定頁列得出來、卻改不動也刪不掉、計數 0 筆」的孤兒類別
                ——`bkCatsUsed` 顯示前有 trim，而 `bkCatCount`／`bsRenCommit`／`bsDel`
                比的是原字串，兩邊對不起來。 */
             cat:bkStr(x.cat,200).replace(/^\s+|\s+$/g,'').slice(0,60),
             sum:bkStr(x.sum,400),ct:ctI,mt:mtI,im:nowI,
             span:bkSpanOk(x.span),due:bkDueOk(x.due)};
    if(rec.span==='0')rec.due=0;      /* 不提醒就不該留著一個到期日 */
    if(by[rec.id]===undefined){st.items.push(rec);by[rec.id]=st.items.length-1;add++;}
    else{
     /* ⚠⚠ **比 `mt` 不是 `ct`。** `ct` 只在新建時寫（`bkCommit` 從不更新它），
        同一筆在兩台機器上**永遠相等** ⇒ 舊規則的 `>=` 讓匯入檔一定贏 ⇒
        「先加書籤、之後才補備註」的人一匯入舊備份就被空字串洗掉。
        而 UI 一直在勸使用者匯出備份——**有損的備份是壞掉的備份**。
        平手（兩邊都沒有 `mt` 的舊格式）一律**保留本機**。
        ⚠ 逐欄位的規則、以及非空衝突怎麼處理，**全部在 `bkMerge()` 裡**（兩個方向共用
        同一段——這條線上同一個保證已經因為「只寫了一條分支」錯過三次）。 */
     if(bkMerge(st.items[by[rec.id]],rec,mtI))upd++; else skip++;}}
   /* 設定：**類別清單取聯集**（那是清單，只增不減），`rv` 這種單值偏好**本機優先**
      ——本機已經有設定就代表使用者刻意選過（跨模型 High#3：舊版在本機已有設定時
      把匯入的類別**整份忽略**，於是「換機器還原」只還原得到一半）。
      ⚠ 這裡只算出要寫什麼，**先不寫**：寫入順序見下面那一段。 */
   var nset=null, os=(o&&o.settings&&typeof o.settings==='object')?o.settings:null;
   if(os){
    var mine=bkSettings();
    /* 兩條分支都過 `bkCatsClean`，形狀對稱。
       ⚠⚠ **但這一行不是承重的，不要以為它在守什麼。** 真正把類別正規化的是
       `bkSaveSet()`（它對每一次寫入都跑 `bkCatsClean`），所以就算這裡把 `os.cats`
       原封不動傳下去，髒資料**也到不了 localStorage**。
       這一行純粹是對稱／防禦，**沒有任何素材只會走到它**（教訓 29）。
       ⚠ 突變檢驗實測：把這裡改回 `cats:os.cats`，探針**全綠**。所以不要在這裡寫
       「守它的是 XXX」——那會變成又一句沒有東西在守的保證。真正在守正規化的是
       `bkSaveSet` 那一層（探針 `first_import_cleans_setting_cats` 測的是那一層）。
       ⚠ `rv` 這裡**刻意不動**：讀取端（`bkRv()`）本來就走白名單、認不得就退回
       `BK_FALLBACK`（半年）。在這裡套 `bkSpanOk` 反而會把認不得的值變成 `'0'`＝
       **不提醒**，那是比「退回半年」更糟的靜默選擇。 */
    if(!mine){if(typeof os.rv==='string')nset={rv:os.rv,cats:bkCatsClean(os.cats)};}
    else{
     var merged=bkCatsClean(mine.cats), inc=bkCatsClean(os.cats), j;
     for(j=0;j<inc.length;j++)if(merged.indexOf(inc[j])<0)merged.push(inc[j]);
     if(merged.length!==mine.cats.length)nset={rv:mine.rv,cats:merged,cta:mine.cta};}}
   /* ⚠⚠ **先寫書籤，成功了才寫設定**（跨模型 Medium#6）。反過來的話 bookmark 那把
      鎖被拒時設定已經改掉＝**半套匯入**：類別清單多了一批、書籤一筆也沒進來。
      兩個 key 之間沒有交易可用，能做的就是**把會失敗的那一邊放前面**。 */
   if(!bkStore(st)){bkRefresh(bkWhyFail(st));return;}
   if(nset&&!bkSaveSet(nset)){
    bkRefresh('書籤已匯入，但個人設定'
            +'（預設值與類別清單）'
            +'沒有存進去。');return;}
   bkRefresh('\u532f\u5165\u5b8c\u6210\uff1a\u65b0\u589e '+add+' \u7b46\u3001\u66f4\u65b0 '+upd
           +' \u7b46\u3001\u7565\u904e '+skip+' \u7b46\u3002');}
  catch(e){bkSay('\u532f\u5165\u5931\u6557\uff1a\u9019\u500b\u6a94\u4e0d\u662f\u6709\u6548\u7684 JSON\u3002');}};
 rd.onerror=function(){bkSay('\u8b80\u4e0d\u5230\u9019\u500b\u6a94\u3002');};
 rd.readAsText(f);}

addEventListener('keydown',function(ev){if(ev.key==='Escape')bkClose();});

/* ── 別的分頁改了資料，這一頁要跟著重畫 ─────────────────────────────
   ⚠⚠ 四個頁面（session／索引／管理／設定）吃**同一個** `file://` origin 的
   localStorage，而靜態頁沒有任何其他同步管道。沒有這一段的話：在 session 頁加了書籤，
   已經開著的索引頁與管理頁**永遠不會知道**——畫面停在舊狀態，而且沒有任何跡象
   （跨模型 Medium#8）。
   ⚠ **這一段只涵蓋三頁**（session／管理／設定）——索引頁不內嵌 core，
   它自己那一份在 `_INDEX_BOOKMARK_JS` 尾端。改同步行為時**兩邊都要改**。
   ⚠ `storage` 事件**只在別的分頁**觸發，自己的寫入不會叫到它 ⇒ 不會遞迴。
   ⚠⚠ **對話窗開著、而且正在編輯時不要重畫**：那會把使用者正在打的字洗掉。
   那條路的衝突交給 `bkCommit()` 的 `mt0` 比對處理（兩邊都要有，缺一邊就是
   「保險只寫在一條分支上」）。
   守它的是探針的 `storage_event_redraws_star`。

   ⚠⚠ **不可以在這裡叫 `bkRefresh()`。** `bkRefresh` 的語意是「**我自己**改完了」，
   各頁的版本因此都會改變畫面狀態：session 頁的會 `bkPanel()`＝**無條件開窗**、
   管理頁的會 `bkClose()`、設定頁的會把改名／新增的輸入框整個重建。
   別的分頁寫一次 localStorage 就讓所有開著的 session 分頁跳出全螢幕對話窗、
   或把使用者正在打的類別名洗掉——那是把「跟著更新」做成了「打斷你」。
   ⚠ 而且那道 `bkModalEl()` 的保險在**設定頁結構上不可能生效**（那一頁沒有對話窗、
   `bkModalEl()` 恆回 null）——又一次「保險只寫在一條分支上」。
   所以另立 `bkSync()`：語意是「**別人**改的，跟著更新，但**絕不改變這一頁目前的
   開闔與編輯狀態**」。各頁自己覆寫（和 `bkRefresh` 一樣是逐頁覆寫的慣例）。
   守它的是探針的 `storage_event_does_not_open_modal`（session 頁）／
   `storage_event_keeps_rename_input`（設定頁）。 */
function bkSync(){bkRefresh('');}
addEventListener('storage',function(ev){
 if(ev&&ev.key&&ev.key!==BK_KEY&&ev.key!==BK_SKEY)return;
 var m=bkModalEl();
 if(m&&m.classList.contains('open')&&BK_EDIT)return;
 try{bkSync();}catch(e){}});
"""


# ⚠ session 頁專屬的那一半（core 之後才載）。管理頁／設定頁**不會**有這一段。
_BOOKMARK_PAGE_JS = r"""
/* ⚠ 身分是 `session_id + 耐久錨點`，**URL 只當快取**：檔名由本地時間＋專案名＋帳號名
   組出來，專案改名或機器換時區就全變。BK_URL 存的是**相對 out/ 的相對路徑**
   ——順帶把「匯出檔含 C:\Users\<名字>\」那個洩漏面整個消掉。 */
var BK_SID=__BK_SID__, BK_URL=__BK_URL__, BK_TITLE=__BK_TITLE__, BK_TOWN=__BK_TOWN__;
var BK_ONLY_DUE=false;  /* ⏰ 該複查了：Will 要求它能當篩選條件 */

function bkMine(st){var out=[];
 for(var i=0;i<st.items.length;i++)if(st.items[i].sid===BK_SID)out.push(st.items[i]);
 return out;}

/* ── 那一輪的 120 字摘要 ────────────────────────────────────────────
   ⚠ **在加書籤的當下從 DOM 抓，不在建置期寫進 HTML。** 管理頁只看得到書籤自己存下來的
   東西，所以摘要非存不可；但每一輪都多渲染 120 字會把一千多頁一起變胖。
   從 DOM 抓是零頁面成本，而且永遠和使用者眼前看到的內容一致。 */
function bkSummary(anchor){
 var el=document.getElementById(anchor);
 if(!el)return '';
 var src=el.querySelector('.body')||el;
 /* \u26a0\u26a0 **\u63a7\u5236\u9805\u7684\u6587\u5b57\u5fc5\u9808\u5148\u5254\u6389**\uff08\u7b2c 4 \u671f\uff09\u3002`.blk-ctls` \u88e1\u662f\u771f\u7684\u6587\u5b57\u7bc0\u9ede\uff08`#` \u8207
    \u2606\uff0f\u2605\uff0c`bkMark()` \u8981\u9760 `textContent` \u6539\u90a3\u9846\u661f\uff09\uff0c\u6240\u4ee5 `textContent` \u6703\u628a\u5b83\u5011\u4e00\u8d77\u8b80\u9032\u4f86\u3002
    \u26a0 \u9019**\u4e0d\u53ea\u5f71\u97ff\u5340\u584a\u5c64\u7d1a\u7684\u66f8\u7c64**\uff1a\u6574\u8f2a\u7684 `.body` \u88e1\u73fe\u5728\u6bcf\u4e00\u584a\u90fd\u6709\u4e00\u7d44\u63a7\u5236\u9805 \u21d2
    \u4e0d\u5254\u7684\u8a71\uff0c\u9023\u65e2\u6709\u7684\u6574\u8f2a\u6458\u8981\u90fd\u6703\u8b8a\u6210\u300c#\u2606\u9019\u662f\u56de\u8986\u2026#\u2606\ud83d\udd27 Bash\u2026\u300d\u3002
    \u26a0 \u7528 clone \u518d\u522a\uff0c\u4e0d\u53ef\u4ee5\u76f4\u63a5\u52d5\u5be6\u9ad4 DOM\u2014\u2014\u90a3\u6703\u628a\u4f7f\u7528\u8005\u773c\u524d\u7684\u6309\u9215\u771f\u7684\u62ff\u6389\u3002
    \u53ea\u5728\u771f\u7684\u6709\u63a7\u5236\u9805\u6642\u624d\u4ed8 clone \u7684\u6210\u672c\uff08\u6574\u8f2a\u4e14\u5340\u584a\u591a\u6642\u90a3\u662f\u4e00\u68f5\u5927\u6a39\uff09\u3002 */
 if(src.querySelector('.blk-ctls')){
  src=src.cloneNode(true);
  var k=src.querySelectorAll('.blk-ctls'),i;
  for(i=0;i<k.length;i++)k[i].parentNode.removeChild(k[i]);}
 var t=(src.textContent||'').replace(/\s+/g,' ').trim();
 return t.length>120?t.slice(0,120)+'\u2026':t;}

function bkOpen(anchor){
 var st=bkLoad(), id=bkId(BK_SID,anchor), rec=bkFindId(st,id);
 return bkEditRec({id:id,sid:BK_SID,anchor:anchor,url:BK_URL,stitle:BK_TITLE,town:BK_TOWN,
                   sum:(rec&&rec.sum)?rec.sum:bkSummary(anchor)});}

/* ── 把「這一輪存過沒有」畫回標頭 ───────────────────────────────────
   ⚠ 那是使用者來這一頁要問的第一個問題；沒有這一步，加了書籤和沒加長得一模一樣。 */
function bkMark(){
 var st=bkLoad(), list=bkMine(st), mine=Object.create(null), i;
 for(i=0;i<list.length;i++)mine[list[i].anchor]=list[i];
 var btns=document.querySelectorAll('.bmk');
 for(i=0;i<btns.length;i++){
  var b=btns[i], r=mine[b.getAttribute('data-k')];
  b.classList.toggle('on',!!r);
  b.textContent=r?'\u2605':'\u2606';
  b.setAttribute('aria-label',r?'\u7de8\u8f2f\u66f8\u7c64':'\u52a0\u66f8\u7c64');
  b.title=r?('\u5df2\u52a0\u66f8\u7c64'+(r.cat?(' \u00b7 '+r.cat):'')
             +(bkOverdue(r)?' \u00b7 \u23f0 \u8a72\u8907\u67e5\u4e86':''))
           :'\u52a0\u66f8\u7c64';}
 var tb=document.getElementById('bkbtn');
 if(tb){var due=0;
  for(i=0;i<list.length;i++)if(bkOverdue(list[i]))due++;
  tb.textContent='🔖 \u66f8\u7c64'+(list.length?(' ('+list.length+')'):'')
                 +(due?(' \u23f0'+due):'');
  tb.classList.toggle('has',!!list.length);}}

/* ── 這一頁的書籤清單（完整的篩選在管理頁）───────────────────────── */
function bkToggleDue(cb){BK_ONLY_DUE=!!cb.checked;bkPanel();}
function bkPanel(msg){
 var st=bkLoad(), list=bkMine(st), i;
 /* ⚠ 排序用 `bkOrd` 不是 `x.ct`：匯入檔沒帶時間戳時 `ct` 是 0（未知），
    直接拿去排會讓那些書籤全部沉到最底，看起來像「最舊的」。`bkOrd` 會退回 `im`。 */
 list.sort(function(a,b){return bkOrd(b)-bkOrd(a);});
 var due=0; for(i=0;i<list.length;i++)if(bkOverdue(list[i]))due++;
 var shown=[];
 for(i=0;i<list.length;i++)if(!BK_ONLY_DUE||bkOverdue(list[i]))shown.push(list[i]);
 var rows='';
 for(i=0;i<shown.length;i++){var x=shown[i];
  rows+='<li class="bm-item"><div class="bmi-top">'
   +'<a href="#'+bkEsc(x.anchor)+'" data-a="'+bkEsc(x.anchor)+'"'
   +' onclick="bkClose();return openSub(this.getAttribute(\'data-a\'))">\u8df3\u904e\u53bb \u2192</a>'
   +(x.cat?'<span class="bmi-cat">'+bkEsc(x.cat)+'</span>':'')
   +(bkOverdue(x)?'<span class="bmi-due">\u23f0 \u8a72\u8907\u67e5\u4e86</span>'
     :(x.due?'<span class="bmi-sum">\u8907\u67e5 '+bkEsc(bkDate(x.due))+'</span>':''))
   +'<span class="grow"></span>'
   +'<button data-a="'+bkEsc(x.anchor)+'" onclick="bkOpen(this.getAttribute(\'data-a\'))">'
   +'\u7de8\u8f2f</button></div>'
   +(x.note?'<div class="bmi-note">'+bkEsc(x.note)+'</div>':'')
   +(x.sum?'<div class="bmi-sum">'+bkEsc(x.sum)+'</div>':'')
   +'</li>';}
 if(!shown.length)
  rows='<li class="bmi-sum">'+(BK_ONLY_DUE?'\u9019\u4e00\u9801\u6c92\u6709\u8a72\u8907\u67e5\u7684\u66f8\u7c64\u3002'
       :'\u9019\u4e00\u9801\u9084\u6c92\u6709\u66f8\u7c64\u3002\u6ed1\u904e\u4efb\u4e00\u8f2a\u3001'
       +'\u6309\u6a19\u982d\u53f3\u908a\u7684 \u2606 \u5c31\u80fd\u52a0\u3002')+'</li>';
 document.getElementById('bkCard').innerHTML=
  '<h3 id="bkTitle">\u9019\u4e00\u9801\u7684\u66f8\u7c64<span class="bmi-sum"> \u00b7 '
 +list.length+' \u7b46'+(due?('\uff0c'+due+' \u7b46\u8a72\u8907\u67e5'):'')+'</span></h3>'
 +'<label class="bm-lab"><input type="checkbox" id="bkOnlyDue" onchange="bkToggleDue(this)"'
 +(BK_ONLY_DUE?' checked':'')+'> \u53ea\u770b \u23f0 \u8a72\u8907\u67e5\u7684</label>'
 +'<ul class="bm-list">'+rows+'</ul>'
 +'<div class="bm-act">'
 +(BK_UNDO?'<button onclick="bkUndo()">\u5fa9\u539f\u525b\u79fb\u9664\u7684\u90a3\u4e00\u7b46</button>':'')
 +'<span class="grow"></span><button onclick="bkClose()">\u95dc\u9589</button></div>'
 +bkFoot(st);
 BK_EDIT=null;
 bkModalEl().classList.add('open');
 bkSay(msg);
 return false;}

/* core 改完資料就叫這個；這一頁要重畫的是標頭的星星與清單畫面。 */
function bkRefresh(msg){bkMark();bkPanel(msg);}
/* ⚠⚠ **別的分頁改的**：只重畫星星，**不可以開窗**。`bkPanel()` 會無條件
   `add('open')`，而這一頁的 `.bm-modal` 是 `position:fixed;inset:0` 的全螢幕遮罩
   ⇒ 別人在管理頁移除一筆，所有開著的 session 分頁就全部被蓋住（連書籤屬於別場的
   也會跳）。窗**本來就開著**時才順便把清單內容更新掉。
   守它的是探針的 `storage_event_does_not_open_modal`。 */
function bkSync(){
 var m=bkModalEl(), wasOpen=!!(m&&m.classList.contains('open'));
 bkMark();
 if(wasOpen)bkPanel('');}
/* ⚠ 覆寫 core 的預設（那個只是把窗關掉）：session 頁的「全部書籤」要打開這一頁的清單，
   因為窗後面沒有別的清單。**匯出／匯入那一排也只在這個清單畫面上**。 */
function bkList(){return bkPanel();}
bkMark();
"""


# ⚠⚠ 同樣是**普通 raw 字串，不是 f-string**（單大括號），理由見 `_BOOKMARK_CORE_JS`。
# 書籤管理頁（第 3 期）。⚠ **這一頁是空殼**：書籤在 localStorage 裡，建置期看不到任何一筆。
#
# 建置期唯一能給的是 `BX_SESS`＝「這次輸出裡有哪些 session、現在的檔名是什麼」
# ——那正是〈網址本身不耐久〉要的反查表：檔名由**本地時間＋專案名＋帳號名**組出來
# （`main()` 的 `base = f"{date}__{proj}__{sid[:8]}"`），專案改名或機器換時區就全變。
# 所以記錄裡的 `url` 只是快取，**每次開這一頁都拿 sid 重算一次**。
# ⚠ 不用 fetch 去讀 `.build-manifest.json`：`file://` 下 Chrome 擋 XHR／fetch，讀不到。
_MANAGE_JS = r"""
var BX_SESS=__BX_SESS__;
var BX_Q='', BX_DUE=false, BX_CAT='', BX_SORT='new';

/* ⚠ 用 hasOwnProperty 問，不要直接 `BX_SESS[sid]`：sid 可能來自別人給的匯入檔，
   叫 `constructor` 的話會撈到原型上的成員、被當成「這一場存在」。 */
function bxSess(sid){
 return (BX_SESS&&Object.prototype.hasOwnProperty.call(BX_SESS,String(sid)))
        ?BX_SESS[String(sid)]:null;}
/* ⚠⚠ 連結**只認以 sid 重算出來的檔名**，對不到就不生連結。
   一度寫成「對不到就退回記錄裡的 `url`」——那正是這整套設計要避免的事：
   sid 對不到的原因八成就是**檔名變了或那一場沒了**，此時那個快取路徑幾乎一定是死的，
   拿它生一個看起來正常的連結＝安靜地把過期資訊當現況。改成講清楚、另外把
   舊位置當**文字**印出來當線索。守它的是探針的 `missing_session_no_link`。
   ⚠ `bkSafeRel` 留著是對**我們自己烤進去的那張表**再驗一次形狀（便宜的不變量）。
   這一頁在 `out/sessions/` 底下、而路徑相對 `out/` ⇒ 前面補一層 `../`。 */
function bxHref(x){
 var s=bxSess(x.sid), u=s?bkSafeRel(s.u):'';
 return u?('../'+u+'#'+encodeURIComponent(String(x.anchor==null?'':x.anchor))):'';}

function bxMatch(x,q){
 var s=bxSess(x.sid);
 var hay=(x.note||'')+' '+(x.cat||'')+' '+(x.sum||'')+' '
        +(s?((s.t||'')+' '+(s.p||'')+' '+(s.a||'')):(x.stitle||''));
 return hay.toLowerCase().indexOf(q)>=0;}

function bxRows(st){
 var out=[], i, x;
 for(i=0;i<st.items.length;i++){x=st.items[i];
  if(BX_DUE&&!bkOverdue(x))continue;
  if(BX_CAT&&(x.cat||'')!==BX_CAT)continue;
  if(BX_Q&&!bxMatch(x,BX_Q))continue;
  out.push(x);}
 out.sort(function(a,b){
  /* ⚠ 一律用 `bkOrd`（`ct` 未知時退回 `im`），理由見 core 那裡的說明。 */
  if(BX_SORT==='old')return bkOrd(a)-bkOrd(b);
  if(BX_SORT==='due'){
   /* 「快到期的先」：沒設提醒的一律排最後，不要讓它們插在到期日中間。 */
   var da=a.due||Infinity, db=b.due||Infinity;
   if(da!==db)return da-db;
   return bkOrd(b)-bkOrd(a);}
  return bkOrd(b)-bkOrd(a);});
 return out;}

function bxFilters(st){
 var cats=bkCatsAll(st), h='', i;
 h+='<button type="button" class="bm-chip cat'+(BX_CAT===''?' on':'')+'" data-c=""'
   +' aria-pressed="'+(BX_CAT===''?'true':'false')+'"'
   +' onclick="bxPickCat(this)">全部類別</button>';
 for(i=0;i<cats.length;i++)
  h+='<button type="button" class="bm-chip cat'+(BX_CAT===cats[i]?' on':'')+'"'
    +' data-c="'+bkEsc(cats[i])+'" aria-pressed="'+(BX_CAT===cats[i]?'true':'false')+'"'
    +' onclick="bxPickCat(this)">'+bkEsc(cats[i])+' <span class="bmi-sum">'
    +bkCatCount(st,cats[i])+'</span></button>';
 return h;}
function bxPickCat(btn){
 BX_CAT=btn.getAttribute('data-c')||'';
 bxRender();}
function bxPickSort(btn){BX_SORT=btn.getAttribute('data-s')||'new';bxRender();}
function bxSearch(el){BX_Q=String(el.value==null?'':el.value).toLowerCase();bxRender();}
function bxToggleDue(cb){BX_DUE=!!cb.checked;bxRender();}

function bxRender(){
 var st=bkLoad(), rows=bxRows(st), i, x, h='', due=0;
 for(i=0;i<st.items.length;i++)if(bkOverdue(st.items[i]))due++;
 document.getElementById('bxCount').textContent=
  st.bad
  ? '讀不到（資料壞了）'      /* ⚠ 不可以印「共 0 筆」——那是在報一個我們並不知道的事實 */
  : ('共 '+st.items.length+' 筆'
     +(due?('，'+due+' 筆 ⏰ 該複查'):'')
     +((rows.length!==st.items.length)?('　·　篩選後 '+rows.length+' 筆'):''));
 document.getElementById('bxCats').innerHTML=bxFilters(st);
 for(i=0;i<rows.length;i++){x=rows[i];
  var s=bxSess(x.sid), href=bxHref(x);
  var title=s?(s.t||''):(x.stitle||'');
  h+='<li class="bm-item bx-item'+(bkOverdue(x)?' due':'')+'"><div class="bmi-top">'
   +(href?('<a class="bx-go" href="'+bkEsc(href)+'">跳過去 →</a>')
        :('<span class="bmi-due">⚠ 對不到檔案</span>'))
   +(x.cat?'<span class="bmi-cat">'+bkEsc(x.cat)+'</span>':'')
   +(bkOverdue(x)?'<span class="bmi-due">⏰ 該複查了</span>'
     :(x.due?'<span class="bmi-sum">複查 '+bkEsc(bkDate(x.due))+'</span>'
            :'<span class="bmi-sum">不提醒</span>'))
   +'<span class="grow"></span>'
   +'<button data-i="'+bkEsc(x.id)+'" onclick="bxEdit(this.getAttribute(\'data-i\'))">'
   +'編輯</button>'
   +'<button class="dang" data-i="'+bkEsc(x.id)+'" onclick="bxDrop(this.getAttribute(\'data-i\'))">'
   +'移除</button></div>'
   /* ⚠ 這一場**不在這次輸出裡**時要講出來，不要安靜地拿快取路徑當現況
      ——書籤本來就永遠不會自己消失，那條連結壞掉是使用者該知道的事。 */
   +'<div class="bx-sess">'
   +(s?'':'<span class="bmi-due">⚠ 不在這次的輸出裡：</span>')
   +bkEsc(title||'（沒有標題）')
   +(s?('<span class="bmi-sum"> · '+bkEsc(s.p||'')
        +(s.a?(' · '+bkEsc(s.a)):'')+(s.d?(' · '+bkEsc(s.d)):'')+'</span>')
      /* 舊位置只當**文字**線索印出來，不是連結——它多半已經失效（見 bxHref）。 */
      :(bkSafeRel(x.url)?('<span class="bmi-sum"> · 上次的位置 '
                          +bkEsc(bkSafeRel(x.url))+'</span>'):''))
   +'</div>'
   +(x.note?'<div class="bmi-note">'+bkEsc(x.note)+'</div>':'')
   +(x.sum?'<div class="bmi-sum">'+bkEsc(x.sum)+'</div>':'')
   +'</li>';}
 if(!rows.length)
  h='<li class="bmi-sum" id="bxEmpty">'
   /* ⚠⚠ **「讀不到」和「確定沒有書籤」不可以講成同一句。** 這整套 `bad:1` 的出發點
      就是這件事，而 `bkFoot()` 分開了、這裡沒有：store 壞掉時主畫面會印
      「還沒有任何書籤。到任一場對話頁…就能加」——那是把使用者往「重新開始建」推，
      正是最糟的建議（原始資料還在，一動就沒了）。
      守它的是探針的 `manage_bad_store_does_not_say_empty`。 */
   +(st.bad
     ?'⚠⚠ 讀不到現有的書籤（localStorage 裡那份資料壞了）。'
      +'畫面上的空白**不代表你沒有書籤**——請先用下面的「⛑ 匯出原始資料」存檔。'
     :(st.items.length
       ?'目前的篩選條件沒有符合的書籤。'
       :'還沒有任何書籤。到任一場對話頁，'
        +'按回合標頭右邊的 ☆ 就能加。'))
   +'</li>';
 document.getElementById('bxList').innerHTML=h;
 document.getElementById('bxUndo').innerHTML=
  BK_UNDO?'<button onclick="bkUndo()">復原剛移除的那一筆</button>':'';
 document.getElementById('bxFoot').innerHTML=bkFoot(st);}

function bxEdit(id){
 var st=bkLoad(), rec=bkFindId(st,id);
 /* ⚠ 管理頁**沒有** BK_URL／BK_TITLE 可以刷新快取欄位，把記錄自己存的原樣傳回去，
    不然在這裡按一次「儲存變更」就會把 url／標題洗成空的。 */
 if(rec)bkEditRec({id:rec.id,sid:rec.sid,anchor:rec.anchor,url:rec.url||'',
                   stitle:rec.stitle||'',town:rec.town?1:0,sum:rec.sum||''});}
function bxDrop(id){bkDrop(id);}

/* core 改完資料就叫這個。⚠ 先關對話窗再重畫：訊息列 `#bkMsg` 在頁面下方的 bkFoot 裡，
   不是在對話窗裡，所以關掉窗訊息照樣看得到。 */
function bkRefresh(msg){bkClose();bxRender();bkSay(msg);}
/* ⚠ 別的分頁改的：重畫清單就好，**不可以把使用者開著的對話窗關掉**
   （`bkRefresh` 會 `bkClose()`，那是「我自己存完了」才該做的事）。 */
function bkSync(){
 var m=bkModalEl();
 if(m&&m.classList.contains('open'))return;
 bxRender();}
bxRender();
"""


# ⚠⚠ 同樣是**普通 raw 字串，不是 f-string**（單大括號）。
# 設定頁（第 3 期）：個人偏好的預設值 ＋ 類別管理。
# ⚠ 分界：**新增類別就地在對話窗做**（很頻繁），**整理才來這一頁**（改名／合併／刪除／排序，
#   一個月一次）。Will 2026-08-22 的裁決，理由見 `bookmarks-proposal.md`〈類別的裁決〉。
_SETTINGS_JS = r"""
var BS_REN=-1;        /* 正在就地改名的那一列（-1＝沒有）*/
var BS_ADD=false;     /* 「＋ 新增類別」的輸入框開著沒 */
var BS_UNDO=null;     /* 剛刪掉的類別 {cat,at,ids}，讓刪除可逆 */

function bsRender(){
 var st=bkLoad(), s=bkSet(), cats=bkCatsAll(st), i, h='';
 /* ① 預設的「提醒我複查」 */
 document.getElementById('bsSpans').innerHTML=bkSpanChips(bkDefSpan());
 document.getElementById('bsSpanNow').textContent=bkSpanLabel(bkDefSpan());
 /* ② 類別管理 */
 for(i=0;i<cats.length;i++){
  var c=cats[i], n=bkCatCount(st,c);
  var managed=false, j;
  for(j=0;j<s.cats.length;j++)if(s.cats[j]===c)managed=true;
  h+='<li class="bs-cat"><span class="bs-name">'+bkEsc(c)+'</span>'
   +'<span class="bmi-sum">'+n+' 筆'
   +(managed?'':' · 只有書籤在用')+'</span>'
   +'<span class="grow"></span>';
  if(BS_REN===i)
   h+='<input type="text" id="bsRenInp" maxlength="60" value="'+bkEsc(c)+'"'
     +' aria-label="新的類別名稱"'
     +' onkeydown="bsRenKey(event,'+i+')">'
     +'<button class="pri" onclick="bsRenCommit('+i+')">存</button>'
     +'<button onclick="bsRenCancel()">取消</button>';
  else
   h+='<button onclick="bsRen('+i+')">✏ 改名</button>'
     +'<button onclick="bsMove('+i+',-1)" aria-label="往上移">▲</button>'
     +'<button onclick="bsMove('+i+',1)" aria-label="往下移">▼</button>'
     +'<button class="dang" onclick="bsDel('+i+')">🗑 刪除</button>';
  h+='</li>';}
 if(!cats.length)
  h='<li class="bmi-sum" id="bsNoCat">還沒有任何類別。'
   +'加書籤的時候按「＋ 新類別」'
   +'就能就地建一個。</li>';
 document.getElementById('bsCats').innerHTML=h;
 document.getElementById('bsAdd').innerHTML=
  BS_ADD?('<input type="text" id="bsAddInp" maxlength="60"'
          +' aria-label="新類別名稱" onkeydown="bsAddKey(event)">'
          +'<button class="pri" onclick="bsAddCommit()">新增</button>'
          +'<button onclick="bsAddCancel()">取消</button>')
        :'<button onclick="bsAddOpen()">＋ 新增類別</button>';
 document.getElementById('bsUndo').innerHTML=
  BS_UNDO?('<button onclick="bsUndoCat()">復原類別「'
           +bkEsc(BS_UNDO.cat)+'」</button>'):'';
 document.getElementById('bsFoot').innerHTML=bkFoot(st);
 if(BS_REN>=0){var e=document.getElementById('bsRenInp');if(e){e.focus();e.select();}}
 if(BS_ADD){var a=document.getElementById('bsAddInp');if(a)a.focus();}}

/* ① 預設值：按下去就存，不要另外一顆「儲存」——一顆 chip 就是一個決定。 */
function bkPick(btn){        /* ⚠ 覆寫 core 的版本：這一頁的 chip 按下即生效 */
 var g=document.getElementById('bsSpans'), all=g.querySelectorAll('.bm-chip'), i;
 for(i=0;i<all.length;i++)all[i].setAttribute('aria-checked','false');
 btn.setAttribute('aria-checked','true');
 var s=bkSet(); s.rv=btn.getAttribute('data-v');
 /* ⚠⚠ **寫入的回傳值一定要看。** 這一頁原本改名／刪除／復原／排序／預設值
    幾乎全部忽略回傳值，然後照樣宣稱成功（跨模型 Medium#6）——
    使用者以為存好了，重新整理才發現什麼都沒變。 */
 if(!bkSaveSet(s)){bsRender();bkSay(bkWhySetFail());return;}
 bsRender();
 bkSay('已存：之後新加的書籤預設「'
       +bkSpanLabel(s.rv)+'」。已經存過的書籤不受影響。');}

/* ② 改名——⚠ **要連動**：改了名，所有用到它的書籤一起改（Will 2026-08-22 裁決）。
   不連動的話改名就只是多造一個孤兒類別。改成一個已經存在的名字＝合併，刻意允許。 */
function bsRen(i){BS_REN=i;BS_ADD=false;bsRender();}
function bsRenCancel(){BS_REN=-1;bsRender();}
function bsRenKey(ev,i){
 if(ev.key==='Enter'){ev.preventDefault();bsRenCommit(i);}
 else if(ev.key==='Escape'){ev.preventDefault();bsRenCancel();}}
function bsRenCommit(i){
 var inp=document.getElementById('bsRenInp');
 if(!inp)return;
 var st=bkLoad(), cats=bkCatsAll(st), old=cats[i];
 var nu=String(inp.value==null?'':inp.value).replace(/^\s+|\s+$/g,'').slice(0,60);
 BS_REN=-1;
 if(old===undefined||!nu||nu===old){bsRender();return;}
 if(st.bad){bsRender();bkSay(BK_BADMSG);return;}
 var n=0,j,now=Date.now(),snap=bkRawSnap();
 /* ⚠⚠ 動到書籤的 `cat` 就**一定要更新它的 `mt`**（跨模型 Medium#8）。
    不更新的話，之後匯入一份「改名前」的備份會被判成比本機新，把舊類別壓回來
    ——而畫面上沒有任何跡象。守它的是探針的 `rename_bumps_mt`。 */
 for(j=0;j<st.items.length;j++)if((st.items[j].cat||'')===old){
  st.items[j].cat=nu; st.items[j].mt=now; st.items[j].lr=bkNum(st.items[j].lr,0)+1; n++;}
 if(!bkStore(st)){bsRender();bkSay(bkWhyFail(st));return;}
 /* 設定清單裡也換掉。⚠ 舊名不在清單裡（只有書籤在用）時要把新名**補進去**，
    否則改完名字就從管理頁的篩選列順序裡掉出去。去重交給 bkCatsClean。 */
 var s=bkSet(), hit=false;
 for(j=0;j<s.cats.length;j++)if(s.cats[j]===old){s.cats[j]=nu;hit=true;}
 if(!hit)s.cats.push(nu);
 /* 建立時刻跟著搬（`bkCatTimes` 只留還在 cats 裡的鍵，舊名那筆會自己被清掉）。 */
 if(s.cta[old]!==undefined&&s.cta[nu]===undefined)s.cta[nu]=s.cta[old];
 if(!bkSaveSet(s)){
  var ok1=bkRollback(snap);
  bsRender();bkSay(ok1?bkWhySetFail():BK_PARTIALMSG);return;}
 bsRender();
 bkSay('已改名：「'+old+'」→「'+nu
       +'」，連動更新 '+n+' 筆書籤。');}

/* ③ 新增（這裡是「整理」用的入口；現場新增請用對話窗那顆「＋ 新類別」）*/
function bsAddOpen(){BS_ADD=true;BS_REN=-1;bsRender();}
function bsAddCancel(){BS_ADD=false;bsRender();}
function bsAddKey(ev){
 if(ev.key==='Enter'){ev.preventDefault();bsAddCommit();}
 else if(ev.key==='Escape'){ev.preventDefault();bsAddCancel();}}
function bsAddCommit(){
 var inp=document.getElementById('bsAddInp');
 if(!inp)return;
 var c=String(inp.value==null?'':inp.value).replace(/^\s+|\s+$/g,'').slice(0,60);
 BS_ADD=false;
 if(!c){bsRender();return;}
 var s=bkSet(), i, has=false;
 for(i=0;i<s.cats.length;i++)if(s.cats[i]===c)has=true;
 /* ⚠ 建立時刻要記（同 `bkCatAdd`），不然新類別在對話窗裡排不到前面。 */
 if(!has){s.cats.push(c);s.cta[c]=Date.now();
          if(!bkSaveSet(s)){bsRender();bkSay(bkWhySetFail());return;}}
 bsRender();
 bkSay(has?('「'+c+'」已經在清單裡了。')
          :('已新增類別「'+c+'」。'));}

/* ④ 刪除——⚠ 不跳 confirm()（會卡住整頁），改成刪完留一顆「復原」。
   訊息要**講清楚有幾筆書籤的類別被清空**，不要只說「已刪除」。 */
function bsDel(i){
 var st=bkLoad(), cats=bkCatsAll(st), c=cats[i];
 if(c===undefined)return;
 if(st.bad){bsRender();bkSay(BK_BADMSG);return;}
 var s=bkSet(), at=-1, j;
 for(j=0;j<s.cats.length;j++)if(s.cats[j]===c)at=j;
 /* ⚠ `wasManaged` ＝這個類別本來就在設定清單裡嗎。沒有它的話，
    復原會把一個「只有書籤在用」的類別**升格成設定管理的**，不是回到原狀。
    ⚠ `cta` 也要記著，不然復原之後它在對話窗的順序會掉到最後（＝不是回到原狀）。 */
 BS_UNDO={cat:c,at:(at<0?s.cats.length:at),wasManaged:(at>=0),
          cta:s.cta[c],ids:[]};
 var n=0,now=Date.now(),snap=bkRawSnap();
 /* ⚠ 和改名同一條規則：動到 `cat` 就要更新 `mt`（跨模型 Medium#8）。 */
 for(j=0;j<st.items.length;j++)if((st.items[j].cat||'')===c){
  BS_UNDO.ids.push(st.items[j].id); st.items[j].cat=''; st.items[j].mt=now;
  st.items[j].lr=bkNum(st.items[j].lr,0)+1; n++;}
 if(!bkStore(st)){BS_UNDO=null;bsRender();bkSay(bkWhyFail(st));return;}
 if(at>=0){s.cats.splice(at,1);
           if(!bkSaveSet(s)){
            var ok2=bkRollback(snap);
            if(ok2)BS_UNDO=null;   /* 回捲成功＝什麼都沒發生，那顆「復原」不該留著 */
            bsRender();bkSay(ok2?bkWhySetFail():BK_PARTIALMSG);return;}}
 BS_REN=-1; bsRender();
 bkSay('已刪除類別「'+c+'」'
       +(n?('，'+n+' 筆書籤的類別被清空'):'')
       +'。按「復原」可以救回來。');}
function bsUndoCat(){
 if(!BS_UNDO)return;
 var st=bkLoad(), s=bkSet(), ids=Object.create(null), j, now=Date.now();
 if(st.bad){bsRender();bkSay(BK_BADMSG);return;}
 var snap=bkRawSnap();
 for(j=0;j<BS_UNDO.ids.length;j++)ids[BS_UNDO.ids[j]]=1;
 for(j=0;j<st.items.length;j++)if(ids[st.items[j].id]){
  st.items[j].cat=BS_UNDO.cat; st.items[j].mt=now;
  st.items[j].lr=bkNum(st.items[j].lr,0)+1;}
 /* ⚠⚠ 和 `bkUndo()` 同一條：**寫入成功了才可以清掉 BS_UNDO**（跨模型 Medium#6）。
    先清的話寫入一失敗，那顆「復原」鈕就消失、而類別再也救不回來。 */
 if(!bkStore(st)){bsRender();bkSay(bkWhyFail(st));return;}
 /* ⚠ 只有本來就被管理的才放回清單——否則復原不是回到原狀，是升格。 */
 if(BS_UNDO.wasManaged){s.cats.splice(Math.min(BS_UNDO.at,s.cats.length),0,BS_UNDO.cat);
                        if(BS_UNDO.cta!==undefined)s.cta[BS_UNDO.cat]=BS_UNDO.cta;
                        if(!bkSaveSet(s)){
                         var ok3=bkRollback(snap);
                         bsRender();bkSay(ok3?bkWhySetFail():BK_PARTIALMSG);return;}}
 var c=BS_UNDO.cat; BS_UNDO=null;
 bsRender();
 bkSay('已復原類別「'+c+'」。');}

/* ⑤ 排序：這裡的順序決定**設定頁與管理頁篩選列**的順序。
   ⚠ 加書籤對話窗不看這個順序，它一律把**最近用過的**排前面（現場要快）。 */
function bsMove(i,d){
 var st=bkLoad(), cats=bkCatsAll(st), c=cats[i];
 if(c===undefined)return;
 var s=bkSet(), at=-1, j;
 for(j=0;j<s.cats.length;j++)if(s.cats[j]===c)at=j;
 /* 只有書籤在用、還沒進清單的類別：先把整份現況固定下來，才有東西可以搬。 */
 if(at<0){s.cats=cats.slice();at=i;}
 var to=at+d;
 if(to<0||to>=s.cats.length)return;
 var t=s.cats[at]; s.cats[at]=s.cats[to]; s.cats[to]=t;
 if(!bkSaveSet(s)){bsRender();bkSay(bkWhySetFail());return;}
 BS_REN=-1; bsRender();}

/* core 改完資料就叫這個。⚠ 這一頁**沒有**共用對話窗（它只整理類別、不編輯單筆書籤），
   所以不必 bkClose()——`bkModalEl()` 在這一頁本來就回 null，core 各處都擋著。 */
function bkRefresh(msg){bsRender();bkSay(msg);}
/* ⚠⚠ **別的分頁改的：正在打字就不要重畫。** `bsRender()` 是用 `innerHTML` 整段重建，
   `#bsRenInp` 的 value 會被塞回**舊的類別名**、`#bsAddInp` 會變回空字串
   ⇒ 使用者打到一半的新名字直接消失。core 那道 `bkModalEl()` 的保險在這一頁
   **結構上不可能生效**（這一頁沒有對話窗，`bkModalEl()` 恆回 null），
   所以擋在這裡才算數。
   守它的是探針的 `storage_event_keeps_rename_input`。 */
function bkSync(){
 if(BS_REN>=0||BS_ADD)return;
 bsRender();}
bsRender();
"""


def render_bookmarks_html(rows, fallback=None, sess_dir=None) -> str:
    """書籤管理頁 → `out/sessions/bookmarks.html`。

    ⚠⚠ **放在 `out/sessions/` 底下，不是 `out/` 根。** session 頁在
    `out/sessions/<source>/<account>/*.html`，管理頁放根目錄就差三層——那是全域最遠的
    一對，也是 `file://` origin 規則最可能出問題的地方。放同一棵子樹是**零成本的保險**
    （`bookmarks-proposal.md`〈B. 管理頁面怎麼生〉）。

    ⚠ 這一頁**每次執行都無條件重產**，和 `index.html` 一樣不受 manifest 版本閘管
    ——所以改這個函式**不必**升 `RENDERER_VERSION`（那個閘只管 session 頁）。
    """
    sess = {}

    def _add(r, overwrite):
        sid = r.get("session_id")
        # has_html＝這個 row 的 HTML 真的產過且與 sig 同步（`--format md` 時可能沒有）。
        # 沒有的話別把死連結烤進去——讓它走「對不到檔案」那條路，訊息才誠實。
        if not sid or not r.get("out_html") or not r.get("has_html", True):
            return
        if sid in sess and not overwrite:
            return
        # ⚠⚠ **每一筆都要確認檔案真的在。** manifest 會記著早就被清掉的輸出，
        #    而縮範圍模式不清孤兒檔，磁碟上還會留著改名前的舊檔。
        if sess_dir is not None and not (sess_dir / r["out_html"]).exists():
            return
        sess[sid] = {"u": "sessions/" + r["out_html"].replace("\\", "/"),
                     "t": r.get("title", ""), "p": r.get("proj", ""),
                     "a": r.get("account", ""), "d": r.get("date_str", "")}

    # ⚠⚠ **同一個 sid 可能在 `rows` 裡出現兩次。** 縮範圍模式下 `new_entries` 是
    #    舊 manifest 的副本，而 manifest 的鍵是**來源檔路徑**：專案夾一改名，
    #    舊路徑那筆還留著（指向改名前的檔名），新路徑那筆才是現況。
    #    重建過的那筆會被 `pop` 掉再重新塞回去 ⇒ **排在後面**，所以這裡取後到者。
    #    （守它的是 `test_bookmark_link_durability` 第 1 節：專案改名後
    #      同一個 sid 必須對到**新的**檔名，而且它先驗「檔名真的變了」才驗指向。）
    for r in rows:
        _add(r, True)
    # ⚠⚠ 再用 manifest 補上「本次沒涵蓋、但檔案還在磁碟上」的那些。
    #    升版後第一次跑 `--project X` 時 `rows` 只剩那幾場（manifest 被視為空），
    #    沒有這一段的話，管理頁會對著還在的檔案說「不在這次的輸出裡」——那是假話。
    for e in (fallback or {}).values():
        row = (e or {}).get("row")
        if row:
            _add(row, False)
    body = f"""
<div class="wrap">
  <div class="topbar">
    <span><a class="back" href="../index.html">← 回索引</a>
    <a class="back" href="settings.html">⚙ 設定</a></span>
  </div>
  <h1>🔖 書籤管理</h1>
  <div class="smeta">書籤存在這台瀏覽器的 localStorage 裡，
    <b>一次「清除瀏覽資料」就沒了</b>——真正的備份是下面那兩顆匯出鈕。</div>
  <div class="filters">
    <input id="bxQ" class="search" type="search" placeholder="🔍 搜尋備註、摘要、類別、對話標題…"
           aria-label="搜尋書籤" oninput="bxSearch(this)">
    <label class="wchk"><input type="checkbox" id="bxDue" onchange="bxToggleDue(this)">
      ⏰ 只看該複查的</label>
    <span class="bm-chips" id="bxSorts">
      <button type="button" class="bm-chip" role="radio" data-s="new" aria-checked="true"
              onclick="bxPickSort(this)">最近加入</button>
      <button type="button" class="bm-chip" role="radio" data-s="old" aria-checked="false"
              onclick="bxPickSort(this)">最早加入</button>
      <button type="button" class="bm-chip" role="radio" data-s="due" aria-checked="false"
              onclick="bxPickSort(this)">快到期的先</button>
    </span>
  </div>
  <div class="bm-chips" id="bxCats"></div>
  <div class="bxcount" id="bxCount"></div>
  <ul class="bm-list bx-list" id="bxList"></ul>
  <div class="bm-act" id="bxUndo"></div>
  <div id="bxFoot"></div>
</div>
<div class="bm-modal" id="bkModal" onclick="bkBackdrop(event)">
  <div class="bm-card" id="bkCard" role="dialog" aria-modal="true" aria-labelledby="bkTitle"></div>
</div>
<script>
function lsGet(k){{try{{return localStorage.getItem(k);}}catch(e){{return null;}}}}
function lsSet(k,v){{try{{localStorage.setItem(k,v);return true;}}catch(e){{return false;}}}}
var BK_MGR='';        /* 這一頁自己就是管理頁，footer 不畫那個連結 */
</script>
<script>{_BOOKMARK_CORE_JS}</script>
<script>{_MANAGE_JS.replace("__BX_SESS__", js_embed(sess))}</script>
<script>
/* 排序 chip 的單選外觀：和複查間隔那組共用 aria-checked 的語意，但各自一組。 */
(function(){{
 var g=document.getElementById('bxSorts');
 g.addEventListener('click',function(ev){{
  var b=ev.target.closest('.bm-chip'); if(!b)return;
  var all=g.querySelectorAll('.bm-chip');
  for(var i=0;i<all.length;i++)all[i].setAttribute('aria-checked','false');
  b.setAttribute('aria-checked','true');}});
}})();
</script>
"""
    return html_page("書籤管理", body)


def render_settings_html() -> str:
    """設定頁 → `out/sessions/settings.html`（Will 2026-08-22 裁決：**獨立一頁**）。

    ⚠ 和管理頁同一層，理由同上。一樣每次執行都無條件重產，不必升 `RENDERER_VERSION`。
    """
    body = f"""
<div class="wrap">
  <div class="topbar">
    <span><a class="back" href="bookmarks.html">← 回書籤管理</a>
    <a class="back" href="../index.html">← 回索引</a></span>
  </div>
  <h1>⚙ 設定</h1>
  <div class="smeta">設定和書籤一樣存在這台瀏覽器的 localStorage 裡；
    它<b>會一起寫進匯出的 JSON</b>，換機器時匯入就回來了。</div>

  <h2 class="bs-h">「提醒我複查」的預設值</h2>
  <div class="bm-lab">新加書籤時預設選哪一個。目前是<b id="bsSpanNow"></b>。
    <span class="bmi-sum">· 到期只會標記 ⏰，書籤永遠不會自己消失</span></div>
  <div class="bm-chips" id="bsSpans" role="radiogroup" aria-label="提醒我複查的預設值"></div>

  <h2 class="bs-h">類別管理</h2>
  <div class="bm-lab">改名、合併、刪除、排序。
    <span class="bmi-sum">· <b>要新增類別的話不用來這裡</b>：加書籤的對話窗裡按「＋ 新類別」
    就能就地建好並選起來 · 改名成一個已經存在的名字＝合併這兩個類別
    · 這裡的順序決定管理頁篩選列的順序；加書籤的對話窗一律把最近用過的排前面</span></div>
  <ul class="bm-list bs-list" id="bsCats"></ul>
  <div class="bm-act" id="bsAdd"></div>
  <div class="bm-act" id="bsUndo"></div>

  <h2 class="bs-h">備份</h2>
  <div id="bsFoot"></div>
</div>
<script>
function lsGet(k){{try{{return localStorage.getItem(k);}}catch(e){{return null;}}}}
function lsSet(k,v){{try{{localStorage.setItem(k,v);return true;}}catch(e){{return false;}}}}
var BK_MGR='bookmarks.html';
</script>
<script>{_BOOKMARK_CORE_JS}</script>
<script>{_SETTINGS_JS}</script>
"""
    return html_page("書籤設定", body)


def render_session_html(s: Session, index_href: str, memory_href: str = "") -> str:
    if not hasattr(s, "main_groups"):
        analyze(s)
    smap = getattr(s, "subagent_map", {})
    smeta = getattr(s, "subagent_meta", {})
    ai = ai_name(s.source_kind)          # 回合標頭 AI 那方的名（Claude/Codex）
    used = set()
    rendered_sub = set()
    # 主對話：換日插日期分隔線；跨過切帳號時刻插切帳號分隔線
    turns = []
    last_day = None
    amarks, acct_i = acct_marks(s), 0

    def sep_div(ts, day):
        # ⚠ 後綴**不指向「該步的成因徽章」**：單步回合根本不畫逐步分隔列（`render_turn_html`
        # 的 multi_step 閘門），後續步驟不冷啟時也沒有成因色可看——那個指標會懸空。
        # 「以下屬於另一個帳號」這半句在 HTML **是**成立的（線一定有下一則：`pending_acct`
        # 不會吐出最後一則之後的標記，而「畫不出來的回合」那條分支在主對話不可達），
        # 所以這裡保留、MD 那邊不保留。兩邊的差異是有依據的，不是分岔。
        return (f'<div class="acct-sep"><span>🔑 {esc(acct_sep_label(ts, day))}'
                ' — 以下屬於另一個帳號；是自行切換或撞到額度，這條線不做判定'
                '</span></div>')

    for g in s.main_groups:
        h = render_turn_html(g, s.tmap, used, smap, smeta, rendered_sub, ai)
        # 切帳號線依**所有回合**的時間推進，不看這一則在這種輸出畫不畫得出來：HTML 與
        # Markdown 對「畫得出來」的判定並不相同（例如 redacted_thinking 只有 HTML 收），
        # 跟著各自的結果推進，兩種輸出的分隔線條數就會不一致。
        hits, acct_i = pending_acct(amarks, acct_i, g)
        d = local_str(g.get("dt"), "%Y-%m-%d") if g.get("dt") else ""
        # 線相對於日期列的位置，取決於它屬於哪一天：
        #   同一天   → 排在日期列**之後**（排在之前會讀成「換日之前就換了帳號」）
        #   更早那天 → 排在日期列**之前**：它不屬於下面那一天，排在之後會讓時間軸倒退
        # 兩者會分開，是因為線與下一則之間可以隔好幾天（中間那些畫不出來的回合不佔位置）。
        earlier, same_day = [], []
        for ts in hits:
            (earlier if (d and epoch_str(ts, "%Y-%m-%d") not in ("", d)) else same_day).append(ts)
        if not h:
            # 這一則畫不出來，線仍要補（否則 MD 會多一條）。沒有日期列要插，但仍走同一條
            # 分流順序——這條分支目前構造不出來，真的可達時排序才不會與上面那條打架。
            turns += [sep_div(ts, d) for ts in earlier + same_day]
            continue
        turns += [sep_div(ts, d) for ts in earlier]
        if d and d != last_day:
            turns.append(f'<div class="day-sep"><span>{esc(day_label(g.get("dt")))}</span></div>')
            last_day = d
        turns += [sep_div(ts, d) for ts in same_day]
        turns.append(h)

    inline_tids = set(rendered_sub)   # 已就地接在 Task 底下的子代理

    # 只有無法對應到任何 Task 的（孤兒）子代理才退回頁尾
    side_html = ""
    orphan_tids = [tid for tid in smap if tid not in rendered_sub]
    if orphan_tids:
        blocks = []
        for tid in orphan_tids:
            rendered_sub.add(tid)
            inner = [render_turn_html(g, s.tmap, used, smap, smeta, rendered_sub, ai) for g in smap[tid]]
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
    if getattr(s, "efforts", None):
        usage += " · effort " + esc("→".join(s.efforts))
    if s.usage.get("total_in") or s.tok_out:
        cost = cost_label(s.cost, s.cost_partial)
        peak_win = getattr(s, "ctx_peak_win", None)     # 峰值那一步自己的模型視窗（見 _collect_usage）
        peak_pct = f"（{round(100 * s.ctx_peak / peak_win)}% / {fmt_tokens(peak_win)}）" if peak_win and s.ctx_peak else ""
        usage += (f" · 💲~{cost} · ⚡快取 {s.cache_pct}%"
                  f" · 脈絡峰值 {fmt_tokens(s.ctx_peak)}{peak_pct} · 產出 {fmt_tokens(s.tok_out)}")
    usage += _miss_summary_html(s)
    rc = getattr(s, "resume_ctx", 0)
    if rc:
        w = context_window(getattr(s, "resume_model", "") or (s.models[-1] if s.models else ""))
        pct = f"（≈{round(100 * rc / w)}% / {fmt_tokens(w)}）" if w else ""
        usage += f" · ↩ resume 約 ~{fmt_tokens(rc)}{pct}"
    kind_chip = (f'<span class="chip kind" title="{esc_attr(KIND_TITLES.get(s.kind, ""))}">{esc(KIND_LABELS.get(s.kind, s.kind))}</span> · '
                 if s.kind != "chat" else "")
    stats = f"{kind_chip}💬 {s.n_user} 問 / {s.n_assistant} 答 · 🔧 {s.n_tools} 次工具呼叫{usage}"
    h1 = f'<span class="named">✎ {esc(s.title)}</span>' if s.rename else esc(s.title)
    # 書籤：把「這一頁是誰」填進 JS。
    # ⚠ `s.out_html` 是相對 `out/sessions/` 的；記錄裡存**相對 `out/`** 的路徑，
    #   這樣管理頁（放在 `out/sessions/` 底下）與索引頁都算得回去，
    #   而且匯出檔裡不會出現 `C:\\Users\\<名字>\\`。
    # ⚠ `town`＝這個標題**是不是使用者自己取的**。沒改過名的標題就是第一則使用者訊息，
    #   那是對話內容，匯出「只匯書籤」時要跟摘要一起遮掉。
    # ⚠⚠ **只有 Claude 那條路算數。** Claude 的 `extract_rename` 讀的是 `/rename` 事件
    #   ——那確實是使用者動作；Codex 的 `s.rename` 來自 `session_index.jsonl` 的
    #   `thread_name`，那只是索引檔的一個欄位，**不是使用者動作的證據**。
    #   實查本機語料（2026-08-22）：唯一一筆 `thread_name` 是
    #   `Codex Companion Task: You are running a TOOLING PROBE, not a`
    #   ——明顯是從對話內容截出來的 60 字，不是誰取的名字。
    #   遮蔽是**隱私保證**，證據不足時一律從嚴：Codex 一律當成沒改過名。
    #   代價只是「只匯書籤」那份少一行標題。守它的是 `test_bookmark_codex_title_masked`。
    # ⚠ `BK_MGR` 是管理頁的相對路徑：管理頁在 `out/sessions/bookmarks.html`，
    #   而 `s.out_html` 相對 `out/sessions/` ⇒ 退回 `out/sessions/` 要 (層數-1) 個 `../`。
    #   `rel_index_href` 退的是 `out/`，比這裡多一層，**不要拿它來算**。
    bookmark_js = (_BOOKMARK_PAGE_JS
                   .replace("__BK_SID__", js_embed(s.session_id))
                   .replace("__BK_URL__",
                            js_embed("sessions/" + (getattr(s, "out_html", "") or "").replace("\\", "/")))
                   .replace("__BK_TITLE__", js_embed(s.title))
                   .replace("__BK_TOWN__",
                            "1" if (s.rename and s.source_kind == SOURCE_CLAUDE) else "0"))
    bk_mgr = js_embed("../" * (len(PureWindowsPath(getattr(s, "out_html", "") or "x.html").parts) - 1)
                      + "bookmarks.html")
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
    <button onclick="toggleAll(false)">收合全部</button>
    <button class="bkbtn" id="bkbtn" onclick="bkPanel()" title="這一頁的書籤 · 匯出／匯入">🔖 書籤</button></span>
  </div>
  <div class="meterbar">每則顯示：
    <label><input type="checkbox" id="cb_cache" onchange="tm('cache')">⚡快取%</label>
    <label><input type="checkbox" id="cb_miss" onchange="tm('miss')" title="Claude API 自報的快取失效成因（含命中率仍高、只掉一段的部分失效）">⚠失效原因</label>
    <label><input type="checkbox" id="cb_cost" onchange="tm('cost')">💲花費</label>
    <label><input type="checkbox" id="cb_in" onchange="tm('in')">新輸入</label>
    <label><input type="checkbox" id="cb_cw" onchange="tm('cw')">快取寫入</label>
    <label><input type="checkbox" id="cb_cr" onchange="tm('cr')">快取讀取</label>
    <label><input type="checkbox" id="cb_ctx" onchange="tm('ctx')" title="脈絡 tokens；知道模型視窗時附佔比 %">脈絡</label>
    <label><input type="checkbox" id="cb_out" onchange="tm('out')">產出</label>
    <label><input type="checkbox" id="cb_gap" onchange="tm('gap')" title="距上一次 API 呼叫多久（快取 TTL 以距上次使用計）">距上一步</label>
    <label><input type="checkbox" id="cb_dur" onchange="tm('dur')" title="這個回合實際耗時（送出 → 答完）">⏱耗時</label>
    <label><input type="checkbox" id="cb_eff" onchange="tm('eff')" title="該次呼叫的推理強度 effort">effort</label>
  </div>
  <h1>{h1}</h1>
  {sub}
  <div class="smeta">{meta}</div>
  <div class="smeta">{esc(when)} · {stats} · <span class="mono">{esc(s.session_id)}</span></div>
  {sub_toc}
  <div class="thread">{''.join(turns)}</div>
  {side_html}
</div>
<!-- 書籤對話窗（第 2 期）。⚠ 空殼在建置期產出，內容全部由 JS 從 localStorage 填——
     建置期不可能知道書籤內容，它們在瀏覽器裡。 -->
<div class="bm-modal" id="bkModal" onclick="bkBackdrop(event)">
  <div class="bm-card" id="bkCard" role="dialog" aria-modal="true" aria-labelledby="bkTitle"></div>
</div>
<script>
function toggleAll(o){{document.querySelectorAll('details.tool,details.think,details.sidechain-wrap').forEach(function(d){{d.open=o;}});}}
function isK(id){{                          /* 嚴格認 k<8碼日期>T<9碼時分秒毫秒>Z，後面可再接子定位符 */
 if(id.length<20||id.charAt(0)!=='k'||id.charAt(9)!=='T'||id.charAt(19)!=='Z')return false;
 if(id.length>20&&id.charAt(20)!=='-')return false;   /* 不准直接黏東西上去（k…Zjunk） */
 for(var i=1;i<19;i++){{if(i===9)continue;var c=id.charAt(i);if(c<'0'||c>'9')return false;}}
 return true;}}
/* ⚠⚠ **文法驗證器**：`isK()` 只驗前 20 字，之後只要求第一個字元是 `-`
   ⇒ `-s`、`-b`、裸 `-tb`、`-s1-b1-b2`、`|`、引號、超長垃圾**全部**會被 `openSub()`
   的 `lastIndexOf('-')` 一段段剝掉、最後框住基底回合並顯示「退化命中」
   （`bookmarks-p4-codex` Medium，六種輸入實測全中）。
   那不是靜默指錯（有橫幅、不改網址），但它把**根本不合文法**的輸入表現成一次成功的
   粗略定位——而那個回合跟輸入毫無關係。
   ⇒ 只有**完整合法**的錨點才准走退化階梯，其餘一律走未命中。
   文法：`k<8碼>T<9碼>Z` 之後，依序最多各出現一次 `-tb<數字>`／`-s<數字>`／`-b<數字>`。
   ⚠ 順序、重複、大小寫都要管：`-b1-tb2`（順序反）與 `-S1-b1`（大寫）都必須被拒。
   ⚠ 長度上限擋掉「幾百個 `-`」造成的反覆切字串。
   守它的是 `scripts/probe_anchor_js.js` 的第 ⑬ 組（含一格前置條件與一組對照組）。 */
function bmGrammarOk(id){{
 if(id.length>200)return false;
 if(!isK(id))return false;
 if(id.length===20)return true;
 /* 槽位是**有序**的：回合 tiebreak → 步 → 區塊 → 子定位符自己的 tiebreak。
    ⚠ 最後那一格不是多餘的：第 1 期的逐段退化規則就設計成「`k…-s…-tb2` 的 tb 屬於
    子定位符、要連它一起退」，而 `probe_anchor_js.js` 的第 ④ 組一直在守那個保證。
    ⚠⚠ **但它只准接在 `s` 後面**（`bookmarks-p4fix-codex` Medium）：原本只靠「槽位往後走」
    這一條，於是 `b → tb` 也被放行 ⇒ `-b1-tb2` 通過守門、退化後框住基底回合＝**安靜指錯**，
    而上面那段註解自己寫著「`-b1-tb2`（順序反）必須被拒」——**實作與註解當時是相反的**。
    差別在**前一段是誰**，不在「有沒有末尾 tb」，所以這裡要記住上一個吃掉的槽位。
    守它的是第 ⑬ 組的 `-b1-tb2`，對照組是第 ④ 組的 `-s…-tb2`（**必須仍然合法**）。 */
 var SLOT=['tb','s','b','tb'], p=id.split('-'), wi=0, last=-1, i, j;
 var digits=function(t){{
  if(!t.length)return false;
  for(var k=0;k<t.length;k++){{var c=t.charAt(k);if(c<'0'||c>'9')return false;}}
  return true;}};
 /* `-s` 兩種寫法都要收：第 4 期產的是 **epoch 毫秒**（純數字），
    而第 1 期凍結文法時寫的是**可讀式 UTC**（`20260801T051501000Z`）。
    真實輸出只會有前者，但後者是已出貨的退化規則承諾過的形狀。 */
 /* ⚠ 19 碼不是 20：`isK` 驗的 20 碼含開頭那個 `k`，這裡的 payload 沒有它。
    `20260801T051501000Z` ＝ 8＋1＋9＋1。算錯一格會讓合法的舊形狀被當成畸形。 */
 var stamp=function(t){{
  if(t.length!==19||t.charAt(8)!=='T'||t.charAt(18)!=='Z')return false;
  for(var k=0;k<18;k++){{if(k===8)continue;var c=t.charAt(k);if(c<'0'||c>'9')return false;}}
  return true;}};
 for(i=1;i<p.length;i++){{
  var seg=p[i], hit=-1;
  for(j=wi;j<SLOT.length;j++){{
   var pre=SLOT[j];
   if(seg.slice(0,pre.length)!==pre)continue;
   if(j===3&&last!==1)continue;  /* 末尾 tb 只准接在 `s` 後面，不准接在 `b` 後面 */
   var rest=seg.slice(pre.length);
   if(digits(rest)||(pre==='s'&&stamp(rest))){{hit=j;break;}}
  }}
  if(hit<0)return false;      /* 不認得的段、順序不對、重複、或 payload 不合法 */
  wi=hit+1;                   /* 槽位只能往後走 ⇒ 每一種最多出現一次 */
  last=hit;
 }}
 return true;}}
function bmTurnId(id){{                     /* 錨點文法 k<ts>[-tb<n>][-s<ts>][-b<n>] 的「回合」那一層 */
 var p=id.split('-'),tb=false;
 if(p.length>1&&p[1].slice(0,2)==='tb'&&p[1].length>2){{        /* tb 後面一定要有數字 */
  tb=true;for(var i=2;i<p[1].length;i++){{var c=p[1].charAt(i);if(c<'0'||c>'9'){{tb=false;break;}}}}}}
 return p.slice(0,tb?2:1).join('-');}}
function bmWhen(id){{                       /* 可讀式錨點的用處：失效時讀得出是哪一刻 */
 if(!isK(id))return id;                     /* 形狀不對就原樣印，不要拼出 '2026-01-01 :: UTC' */
 var mo=+id.substr(5,2),d=+id.substr(7,2),h=+id.substr(10,2),mi=+id.substr(12,2),se=+id.substr(14,2);
 /* ⚠ isK 只驗「是不是數字」，`k99999999T999999999Z` 照樣通過。範圍不合理就原樣印，
    不要自信地講一個不存在的時刻（`durable-anchor-r4` #8）。 */
 if(mo<1||mo>12||d<1||d>31||h>23||mi>59||se>59)return id;
 return id.substr(1,4)+'-'+id.substr(5,2)+'-'+id.substr(7,2)+' '
       +id.substr(10,2)+':'+id.substr(12,2)+':'+id.substr(14,2)+' UTC';}}
function bmClear(){{                        /* ⚠⚠ 命中時一定要撤掉上一次的橫幅 */
 var d=document.getElementById('bm-banner');
 if(d&&d.parentElement)d.parentElement.removeChild(d);}}
function bmBanner(msg){{                    /* ⚠ 重用同一個節點：兩次未命中不該疊兩條 */
 var d=document.getElementById('bm-banner');
 if(!d){{d=document.createElement('div');d.className='bm-miss';d.id='bm-banner';
        document.body.insertBefore(d,document.body.firstChild);}}
 d.textContent=msg;
 var x=document.createElement('button');x.className='bm-x';x.textContent='\u2715';
 x.title='關閉';x.onclick=bmClear;d.appendChild(x);
 return d;}}
function bmMiss(id){{
 if(!isK(id))return;                       /* 只對耐久錨點出聲；別的 hash 找不到是既有行為 */
 bmBanner('\u26a0 這個書籤指向的回合在這份輸出裡找不到（'+bmWhen(id)+'）。'
  +'可能這份轉錄檔被重新解析過，或這個連結屬於另一場 session。');}}
function openSub(id){{var el=document.getElementById(id);
 /* 逐段退化：第 4 期的區塊層級錨點（k…-s…-b3）丟進只認得回合的頁面時，一段一段往回退到
    「找得到那一輪」為止，而不是死掉。
    ⚠⚠ **只退子定位符（s…/b…），絕不退純數字的 tiebreak 後綴。** `-2` 是「同一毫秒的第二輪」，
    把它退掉會跳到**同毫秒的另一輪**、加上代表成功的 .hl 外框、還改寫網址列——那正是這整個
    設計要消滅的「安靜指錯」。⚠ 也只對耐久錨點做：sub-orphans 那種 id 也帶 -。 */
 /* ⚠⚠ 文法不合的一律當未命中，**不准走退化階梯**（見 `bmGrammarOk` 的說明）。
    ⚠ 判準用 `isK` 不是 `bmGrammarOk`：`isK` 為假代表「那根本不是耐久錨點」
    （子代理目錄的 `sub-…` 也帶 `-`），那是既有行為，照原樣走。
    這裡新增的只有「看起來是耐久錨點、但後綴不合文法」那一格。 */
 if(isK(id)&&!bmGrammarOk(id)){{
  document.querySelectorAll('.hl').forEach(function(x){{x.classList.remove('hl');}});
  bmMiss(id);return true;}}
 var deg=false,bt=isK(id)?bmTurnId(id):id;
 /* 一段一段退到**回合那一層**為止，不再靠「最後一段是不是數字」猜。
    `k…-tb2` 本身就是回合層 ⇒ 一步都不退（退了就會跳到同毫秒的另一輪＝安靜指錯）；
    `k…-s…-tb2` 的 tb 屬於子定位符 ⇒ 連它一起退。 */
 while(!el&&isK(id)&&id!==bt){{
  id=id.slice(0,id.lastIndexOf('-'));el=document.getElementById(id);deg=true;}}
 if(!el){{
  /* ⚠ 未命中也要清掉上一次留下的 .hl：不清的話畫面會同時有「某一輪被框起來」＋
     「找不到」橫幅，讀者讀不出到底跳了沒（`durable-anchor-r3` #1）。 */
  document.querySelectorAll('.hl').forEach(function(x){{x.classList.remove('hl');}});
  bmMiss(id);return true;}}
 /* t{{n}} 掛在標頭的零尺寸 span 上 → 往上提到所屬回合，讓 #t7 與 #k…Z 行為完全一致
    （展開折疊區塊、加 .hl 外框都只對 .turn/.compact-sep 本身生效）。 */
 if(el.classList.contains('tanchor')){{var tp=el.closest('.turn,.compact-sep');if(tp)el=tp;}}
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
 el.scrollIntoView({{behavior:'smooth',block:'start'}});
 /* ⚠ 退化命中 ≠ 精確命中：出一條軟提示，而且**不改寫網址列**——改了就把原連結蓋掉，
    使用者連「我本來存的是更細的位置」都看不出來。 */
 if(deg){{bmBanner('\u2139 原連結指向這一輪裡更細的位置，那個位置在這份輸出裡找不到，'
  +'已改為跳到該回合（'+bmWhen(id)+'）。');}}
 /* ⚠⚠ 精確命中要**主動撤掉**橫幅。沒撤的話一次 miss 之後，**每一次成功跳轉都長得像失敗**
    ——橫幅是 fixed、捲不掉，會一直說「找不到」直到重新載入（`durable-anchor-r3` #1）。 */
 else{{bmClear();history.replaceState(null,'',  '#'+id);}}
 return false;}}
function bmGo(){{if(location.hash.length>1)openSub(location.hash.slice(1));}}
setTimeout(bmGo,0);
/* ⚠⚠ **hashchange 一定要掛。** 只在 load 時跑一次的話，「點瀏覽器我的最愛、而那一頁
   已經開在同一個分頁裡」是**同文件 fragment 導覽、不會重新載入** → openSub 一次都不會
   被呼叫 → 沒有 .hl、沒有橫幅，「找不到」又變回徹底靜默。而那正是本功能的主要使用情境。
   （頁內連結走 onclick→openSub 並回 false，不觸發導覽；replaceState 也不觸發，不會重複跑。） */
/* ⚠ **已知限制**（`durable-anchor-r4` #6）：`hashchange` 只在 hash **有變**時觸發，
   所以「網址列已經是 #k…、再點一次同一個我的最愛」完全靜默。頁內連結不受影響
   （走 onclick→openSub）。這是瀏覽器行為，靜態頁擋不到；不做假的修補。 */
addEventListener('hashchange',bmGo);
var MET=['cache','miss','cost','in','cw','cr','ctx','out','gap','dur','eff'],
    DEF={{cache:1,miss:1,cost:1,in:0,cw:0,cr:0,ctx:0,out:0,gap:1,dur:0,eff:0}};
function lsGet(k){{try{{return localStorage.getItem(k);}}catch(e){{return null;}}}}
function lsSet(k,v){{try{{localStorage.setItem(k,v);return true;}}catch(e){{return false;}}}}
function applyMet(){{MET.forEach(function(k){{var v=lsGet('m_'+k);v=(v===null)?DEF[k]:(v==='1'?1:0);document.body.classList.toggle('hide-'+k,!v);var cb=document.getElementById('cb_'+k);if(cb)cb.checked=!!v;}});}}
function tm(k){{var cb=document.getElementById('cb_'+k);lsSet('m_'+k,cb.checked?'1':'0');document.body.classList.toggle('hide-'+k,!cb.checked);}}
applyMet();
var BK_MGR={bk_mgr};
{_BOOKMARK_CORE_JS}
{bookmark_js}
</script>
"""
    return html_page(s.title, body,
                     body_class="hide-in hide-cw hide-cr hide-ctx hide-out hide-dur hide-eff")


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
    pct = _pct_display(u["cache_read"], total_in)
    cold = ""
    mp, mi, mc, mcold, mmask = coldest_step(group)
    if mp is not None and mcold:
        tag = _cold_cause_label(mc, mmask)
        # ⚠ 只掛在「真失效」（過期/異常逐出）；結構性/未歸因只標成因、不當警訊
        warn = "⚠" if mc in _COLD_GENUINE else ""
        if group.get("n_steps", 0) >= 2:
            cold = f" · ❄最低 {mp}%（步驟{mi}{('·' + tag) if tag else ''}）{warn}"
        elif tag or warn:
            # (v36-fam3 F2) 單步回合：MD 一律不畫逐步列，而上面那條又要求 n_steps >= 2
            # → 成因在 MD 上完全看不到（HTML 至少還有顏色，MD 連顏色都沒有）。
            # 不寫「最低／步驟 N」——只有一步，那兩個詞沒有意義。
            # MD 沒有 title 可掛，所以成因一律攤在版面上（與多步那條同樣的理由）。
            cold = f" · ❄{tag}{warn}"
    cost = cost_label(u["cost"], u["unpriced"])
    miss = ""
    if u.get("miss"):      # API 自報失效（含部分失效）：MD 也要看得到，否則 grep 不到
        order = sorted(set(u["miss"]), key=lambda r: (-u["miss"].count(r), r))
        tok = f"·失效前綴 {fmt_tokens(u['miss_tok'])}" if u.get("miss_tok") else ""
        nall, nc = len(u["miss"]), (u.get("miss_cold") or 0)     # 同 HTML：整段／只掉一段照實拆
        tag = "" if nc == nall else ("部分失效·" if nc == 0 else f"整段{nc}·只掉一段{nall - nc}·")
        miss = f" · ⚠{tag}{'、'.join(miss_label(r) for r in order[:2])}×{len(u['miss'])}{tok}"
    win = _group_window(group)
    pctx = f"（{round(100 * u['ctx_max'] / win)}%）" if (win and u["ctx_max"]) else ""
    dur = f" · ⏱{fmt_dur(group['dur_ms'] / 1000)}" if group.get("dur_ms") else ""
    # MD 完全不畫逐步列（render_turn_md 略過 _step），所以「距上一步／effort」不論單步多步都得
    # 在回合列上給，否則 MD 永遠看不到這兩欄（README 承諾 MD 附上同樣資訊）。
    su_lead, gap_lead = _lead_step(group)
    extra = f" · 距上一步 {fmt_dur(gap_lead)}" if gap_lead is not None else ""
    if su_lead and su_lead.get("effort"):
        extra += f" · effort {su_lead['effort']}"
    return (f"  ·  ⚡{pct}%{cold}{miss} · ~{cost} · ctx {fmt_tokens(u['ctx_max'])}{pctx}"
            f" · ↑{fmt_tokens(u['output'])}{dur}{extra}")


def render_turn_md(group, tmap, ai="Claude"):
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
        elif t == "_command":
            # ⚠ 指令與其輸出都用 fence 包住：輸出是終端文字，裸著寫會被 MD 吃掉。
            # `--search` 的全文索引讀的就是這份 MD ⇒ 指令名從這一版起搜得到。
            nm = " ".join(x for x in (b.get("name"), b.get("args")) if x)
            if nm:
                parts.append(f"`{nm}`")
            if b.get("out"):
                parts.append(_md_fence(b["out"]))
            # 掛在這個區塊上的圖（見 `group_turns`）：MD 用和一般 image block 同一個記號
            parts += ["_[圖片]_"] * len(b.get("imgs") or [])
        elif t == "_interject":
            # ⚠ MD 也要有——`--search` 的全文索引讀的是這一份，插話的內容不可以搜不到。
            _q = parse_ts(b.get("queued_at")) if b.get("queued_at") else None
            # ⚠ 標籤和 HTML 那半共用同一個判準（`after_turn`），兩邊不可以各寫一句。
            _what = "這一輪結束後才讀到" if b.get("after_turn") else "中途插話"
            _lbl = f"👤 你 · {_what}" + (f"（{local_str(_q, '%H:%M:%S')} 送出）" if _q else "")
            _txt = clean_user_text(b.get("text") or "")
            # ⚠ 圖片在 MD 用和一般 image block 同一個記號 `_[圖片]_`：兩處各寫一種的話，
            #   讀 MD 的人會以為那是兩種不同的東西。
            _lines = _txt.splitlines() + ["_[圖片]_"] * len(b.get("imgs") or [])
            parts.append(f"> **{_lbl}**\n>\n" + "\n".join("> " + ln for ln in _lines))
        elif t == "_notify":
            lead = b.get("summary") or ""
            st = _NOTIFY_STATUS_LABEL.get(b.get("status") or "", b.get("status") or "")
            parts.append("_⚙ 背景任務" + (f" · {st}" if st else "")
                         + (f" · {lead}" if lead else "") + "_")
            # ⚠⚠ **原文一定要進 MD**：`--search` 的全文索引讀的是這一份。
            # v55 之前通知是普通 user 回合、整坨原文**是進 MD 的** ⇒ 只寫一行摘要
            # 等於**搜尋能力回退**（task-id、輸出檔路徑、Monitor 的 `<event>` 內文全部
            # 從此搜不到），不只是「新內容沒進去」。
            # ⚠⚠ **是 `raw or event`，不是 `event or raw`。** 寫成後者的話，帶 `<event>`
            # 的那種（Monitor 型）只會留下 event 內容，同一則的 `<task-id>` 與
            # `<output-file>` **從全文索引消失**——那是 v55 之前搜得到的東西
            # （`utf-fix-codex` Medium 實測）。原文是超集，摘要另外那一行已經給了。
            # ⚠ 和 HTML 走同一個表示式（`_strip_ansi(raw or event)`），兩邊不可以各取各的。
            _detail = _strip_ansi(b.get("raw") or b.get("event") or "")
            if _detail.strip():
                parts.append(_md_fence(_detail))
            # 掛在這個區塊上的圖（見 `group_turns`）：MD 用和一般 image block 同一個記號
            parts += ["_[圖片]_"] * len(b.get("imgs") or [])
    if not parts:
        return ""
    icon = "👤 You" if role == "user" else f"🤖 {ai}"
    side = "↳ " if group.get("side") else ""
    when = local_str(group.get("dt"), "%H:%M:%S")
    meters = turn_meters_md(group)
    # 中途插話：HTML 有徽章，MD 也要有一個對應的記號，否則同一件事只有一半的輸出看得到。
    # ⚠ 它落在 `_TURN_HEAD_RE` 的 `(?P<rest>.*)` 那一段（`·` 與 `{#tN}` 之間），
    # 和 `meters` 同一格 ⇒ **`--search` 的切回合不受影響**（那條正則靠 `{#tN}` 定位）。
    _qa_md = parse_ts(group.get("queued_at")) if group.get("queued_at") else None
    if _qa_md:
        meters = f"{meters} · ⏳ 中途插話（{local_str(_qa_md, '%H:%M:%S')} 送出）"
    # {#tN}/{#sN}＝對應 HTML 該則的錨點 id：--search 用它定位，手動 rg 到後也可接在 .html# 後跳到該則
    mark = f" {{#{group['anchor']}}}" if group.get("anchor") else ""
    # 耐久錨點也寫進 MD，維持「兩種輸出讀同一份錨點」這條既有性質：rg 到之後可以直接
    # 接在 .html# 後面跳，而且那個連結不會因為之後改解析器而指到別輪。
    # ⚠ **已知限制**：這樣一行上會有兩個 `{#…}`，而 Pandoc 的 header-attribute 語法只吃
    # **結尾那一個**——用 pandoc 算繪這份 MD 時，被當成 id 的會是耐久錨點，`{#tN}` 會變成
    # 標題裡的可見文字。本工具自己的 `_TURN_HEAD_RE` 吃得下兩者（實測 `--search` 仍正常），
    # 而 `--search` 結果頁的連結仍走 `t{n}`，所以 MD 裡的耐久錨點目前只是給人 `rg` 用。
    # 順序不可對調：`{#tN}` 必須在前，`_TURN_HEAD_RE` 靠它切回合（`durable-anchor-r2` #13）。
    if group.get("kanchor"):
        mark += f" {{#{group['kanchor']}}}"
    return f"\n### {side}{icon} · {when}{meters}{mark}\n\n" + "\n\n".join(parts) + "\n"


# MD 的切帳號分隔線。**渲染端與搜尋端共用同一份定義**：那行不屬於任何一則回合，
# `iter_turn_chunks` 要靠它辨識，兩邊各寫一份字串就會在改文案時默默分岔。
MD_ACCT_SEP_PREFIX = "**🔑 "
MD_ACCT_SEP_SUFFIX = "** — 自行切換或撞到額度都有可能，本檔不做判定"


def is_md_acct_sep(line):
    """這一行是不是渲染端插進去的切帳號分隔線。

    ⚠ **要比整行的形狀，不能只看開頭。** 使用者自己的訊息也可以用 `**🔑 ` 開頭（粗體加鑰匙
    emoji 沒有任何保留意義），只比前綴會把那一則的內文**靜默刪掉**——全文搜尋從此漏報那段話，
    而且畫面上沒有任何跡象。前後綴都是本模組自己產的字串，使用者要同時撞上兩端才會誤判。"""
    return line.startswith(MD_ACCT_SEP_PREFIX) and line.endswith(MD_ACCT_SEP_SUFFIX)


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
        (f"- effort：{'→'.join(s.efforts)}" if getattr(s, "efforts", None) else None),
        (f"- {_miss_head(s)[0]}（API 自報）" if _miss_head(s)[0] else None),
        (f"- 型態：{KIND_LABELS.get(s.kind, s.kind)}" if s.kind != "chat" else None),
        f"- Session：`{s.session_id}` · v{s.version}",
        "", "---",
    ]
    ai = ai_name(s.source_kind)          # 回合標頭 AI 那方的名（Claude/Codex）
    # 切帳號分隔線：MD 也要有（否則 grep 不到），與 HTML 共用 acct_marks／pending_acct，
    # 同一組時刻、同一條插入規則，兩邊不會分岔。
    amarks, acct_i = acct_marks(s), 0
    # ⚠ 這裡**不傳日期基準**：MD 全篇沒有日期可對照（回合標頭只有時分秒），任何基準都會有
    # 一格裸奔成沒有日期的時分。不傳＝一律帶日期，理由見 `acct_sep_label` 的 docstring。
    parts = []
    for g in s.main_groups:
        md = render_turn_md(g, s.tmap, ai)
        # 與 HTML 同一條推進規則（理由見 render_session_html），兩邊條數與位置才會一致。
        hits, acct_i = pending_acct(amarks, acct_i, g)
        for ts in hits:
            # ⚠ 後綴**不可照抄 HTML 那一條**（`sep_div`）：它說「以下屬於另一個帳號」，
            # 而 MD **不一定有「以下」**——條數與位置是跟著**所有回合**推進的（理由見
            # `render_session_html`），但有些回合只有 HTML 畫得出來（如 `redacted_thinking`）。
            # 那種回合落在最後一則時，MD 這條線就在檔尾、下面真的什麼都沒有。HTML 沒有這一格：
            # 反方向（HTML 畫不出來）的那條分支在主對話不可達，`sep_div` 的註解有說明。
            # 所以 MD 只陳述實據（換了帳號、什麼時刻）、不宣稱方向；
            # 兩種輸出的條數與位置仍然一致（不與最高不變量③衝突）。
            sep = MD_ACCT_SEP_PREFIX + acct_sep_label(ts) + MD_ACCT_SEP_SUFFIX + "\n"
            # ⚠ **這裡不補 `---`。** 那條橫線與這條分隔線一樣不屬於任何一則回合，但
            # `iter_turn_chunks` 只認得 `MD_ACCT_SEP_PREFIX` 那一行 → `---` 會落進**前**一則的
            # 搜尋內文與片段裡（而前一則屬於舊帳號）。**每多插一種裝飾，就多一個消費者得跟著
            # 學會跳過的形狀**；回合本來就有 `### ` 標頭當視覺分界，這條粗體 🔑 行自己也夠顯眼。
            parts.append("\n" + sep)
        if not md:
            continue
        parts.append(md)
    out = "\n".join(x for x in head if x is not None) + "\n" + "\n".join(parts)
    if s.side_groups:
        sparts = [render_turn_md(g, s.tmap, ai) for g in s.side_groups]
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


# ⚠⚠ 同樣是**普通 raw 字串，不是 f-string**（單大括號），理由見 `_BOOKMARK_JS`。
# 索引頁的書籤圖示：建置期看不到書籤（它們在瀏覽器的 localStorage 裡），
# 只能由頁面 JS 用每一列的 `data-sid` 對回來。
# ⚠ 這一段**不需要升 RENDERER_VERSION**：`index.html` 每次執行都無條件重產，
#   不受 manifest 版本閘管（那個閘只管 session 頁）。
_INDEX_BOOKMARK_JS = r"""
/* ── 索引頁的書籤圖示（Will 2026-08-22 要求）───────────────────────────
   ⚠ 讀的是和 session 頁**同一個** localStorage 鍵；`file://` 在 Chrome 眼裡是單一
   origin，所以索引頁讀得到 session 頁存的東西（第 1.5 期已實測過這個前提）。 */
function bkIndexMark(){
 /* ⚠ `Object.create(null)` 不是 `{}`：sid 來自匯入檔時可能叫 `constructor`，
    拿 `{}` 當表會撈到原型上的成員 ⇒ `e.n++` 打在一個函式上、整列的計數變 NaN。 */
 var by=Object.create(null);
 try{
  var raw=lsGet('asv_bm_v1');
  var o=raw?JSON.parse(raw):null;
  var items=(o&&Array.isArray(o.items))?o.items:[];
  var now=Date.now();
  for(var i=0;i<items.length;i++){
   /* ⚠ 每一筆都要驗型別：`{items:[null]}` 與 `{sid:{}}` 都是合法 JSON
      （跨模型 Medium#7 在 core 那邊的同一件事）。 */
   var x=items[i]; if(!x||typeof x!=='object'||typeof x.sid!=='string'||!x.sid)continue;
   var e=by[x.sid]||(by[x.sid]={n:0,due:0,cats:Object.create(null)});
   e.n++;
   if(x.due>0&&x.due<=now)e.due++;
   if(x.cat&&typeof x.cat==='string')e.cats[x.cat]=1;
  }
 /* 壞掉的 store 不可以讓整頁索引死掉。
    ⚠⚠ 這裡**一定要是 `Object.create(null)`**，不可以寫 `{}`：底下用
    `by[r.dataset.sid]` 查，而 sid 叫 `constructor` 時 `{}` 會撈到原型上的函式
    ⇒ 判成「這一場有書籤」⇒ `Object.keys(e.cats)` 對 undefined 取值、整頁掛掉。
    上面那個宣告本來就已經是 `Object.create(null)` 了，catch 這條路漏掉就等於沒防。 */
 }catch(e){ by=Object.create(null); }
 ROWS.forEach(function(r){
  var old=r.querySelector('.bmflag');
  if(old&&old.parentElement)old.parentElement.removeChild(old);
  var e=by[r.dataset.sid||''];
  r.dataset.bm=e?'1':'0';
  if(!e)return;
  var cats=Object.keys(e.cats).sort();
  var flag=document.createElement('span');
  flag.className='bmflag'+(e.due?' due':'');
  /* ⚠ 用 textContent 不用 innerHTML：類別是使用者輸入、也可能來自別人給的匯入檔。 */
  flag.textContent='🔖'+(e.n>1?e.n:'')+(e.due?' ⏰':'');
  flag.title=e.n+' 筆書籤'
             +(e.due?('，'+e.due+' 筆該複查'):'')
             +(cats.length?('：'+cats.join('、')):'');
  var cell=r.querySelector('td a');
  if(cell&&cell.parentElement)cell.parentElement.insertBefore(flag,cell);
 });
 /* 篩選列多一顆「只看有書籤的」——⚠ 它要參與既有的 af()，不是自己另做一套顯示邏輯，
    否則和其他篩選條件疊起來會互相打架。 */
 var fb=document.getElementById('fb');
 if(fb){
  /* ⚠⚠ 要數的是**這份索引裡真的有書籤的列**，不是 store 裡的 sid 總數
     （跨模型 Low#11）。書籤可能來自別台的匯入檔、或指向這次沒涵蓋的 session
     ⇒ 用總數決定的話，鈕會出現、勾下去卻得到一張空表，而畫面上沒有任何解釋。
     ⚠ 這一段一定要跑在上面那個 `ROWS.forEach` **之後**（`data-bm` 才填好了）。
     守它的是索引探針的 `filter_hidden_when_only_offindex`。 */
  var total=0;
  ROWS.forEach(function(r){ if(r.dataset.bm==='1')total++; });
  fb.parentElement.style.display=total?'':'none';
  fb.parentElement.title=total+' 場有書籤';
  /* ⚠⚠ **藏起來的篩選不可以還在生效。** 上一行只是把控制項藏起來，`af()` 讀的仍是
     `fb.checked` ⇒ 勾著的狀態下，別的分頁移除最後一筆書籤時：勾選框消失、篩選照跑
     ⇒ **整張表 0 列，而畫面上沒有任何東西解釋為什麼**（收斂確認輪 Medium）。
     ⚠ 這個狀態是新的 storage listener 造出來的：這個函式以前只在載入時跑一次，
     那時 `fb` 必定沒勾，所以到不了。**每加一條同步路徑，就要問它造出了哪些新狀態。**
     ⚠ 只在 `total===0`（＝控制項真的被藏起來）時取消勾選；鈕還看得見時勾著就該繼續生效
     （對照組 `visible_bm_filter_still_applies` 在守這一半）。
     守它的是索引探針的 `hidden_bm_filter_does_not_blank_table`。 */
  if(!total)fb.checked=false;
 }
}
/* ⚠ 「只看有書籤的」的還原在 `restoreF()` 裡（理由見那裡）。這一段只負責
   「這份索引一場書籤都沒有就取消勾選」，而它必須排在還原**之後**——
   順序是 **restoreF → af → bkIndexMark → af**。
   守它的是索引探針的 `reload_keeps_bm_filter`（真的重新載入一次）
   與 `restored_bm_filter_still_unchecked_when_hidden`（順序）。 */
bkIndexMark();
af();      /* 圖示會改變 data-bm，重跑一次篩選讓「只看有書籤的」立刻生效 */
/* ⚠⚠ **索引頁自己要有這一段。** core 的 `storage` listener 只寫在
   `_BOOKMARK_CORE_JS` 裡，而索引頁**不內嵌 core**（它只有這一段）
   ⇒ 「在 session 頁加了書籤，開著的索引頁跟著出現 🔖」原本根本沒有實作，
   但 core 的註解與 README〈開著好幾個分頁〉都寫成有。
   這一頁沒有對話窗、也沒有任何編輯狀態，所以直接重畫就好。
   守它的是索引探針的 `index_storage_event_redraws`。 */
addEventListener('storage',function(ev){
 if(ev&&ev.key&&ev.key!=='asv_bm_v1')return;
 try{bkIndexMark();af();}catch(e){}});
"""


def render_index_html(rows, show_account=False, cache_report=False, codex_report=False) -> str:
    rows = sorted(rows, key=lambda r: r.get("start_ts") or 0, reverse=True)
    projects = sorted({r["proj"] for r in rows})
    months = sorted({r["month"] for r in rows if r.get("month")}, reverse=True)
    accounts = sorted({r.get("account", "") for r in rows if r.get("account")})
    sources = sorted({r.get("source_kind", SOURCE_CLAUDE) for r in rows})
    show_source = len(sources) > 1
    kinds = sorted({r.get("kind") or "chat" for r in rows})
    show_kind = len(kinds) > 1          # 單一型態（如全為一般互動）時不顯示型態下拉
    proj_opts = "".join(f'<option value="{esc_attr(p)}">{esc(p)}</option>' for p in projects)
    month_opts = "".join(f'<option value="{esc_attr(m)}">{esc(m)}</option>' for m in months)
    acc_opts = "".join(f'<option value="{esc_attr(a)}">{esc(a)}</option>' for a in accounts)
    src_opts = "".join(f'<option value="{esc_attr(src)}">{esc(source_label(src))}</option>' for src in sources)
    acc_select = (f'<select id="fa" onchange="af()"><option value="">全部帳號/來源</option>{acc_opts}</select>'
                  if show_account else "")
    src_select = (f'<select id="fs" onchange="af()"><option value="">全部工具</option>{src_opts}</select>'
                  if show_source else "")
    kind_opts = "".join(f'<option value="{esc_attr(k)}">{esc(KIND_LABELS.get(k, k))}</option>' for k in kinds)
    kind_select = (f'<select id="fk" onchange="af()"><option value="">全部型態</option>{kind_opts}</select>'
                   if show_kind else "")
    # 只有真的有可避免浪費時才出現這個勾選框：沒有的話擺著只是一個永遠篩不出東西的控制項
    waste_toggle = ('<label class="wchk" title="只列出有人因可避免冷啟的 session">'
                    '<input type="checkbox" id="fw" onchange="af()"> 只看有人因浪費的</label>'
                    if any((r.get("waste_n") or 0) for r in rows) else "")
    # ⚠ 這顆一開始是隱藏的：書籤在 localStorage 裡，建置期不知道有沒有。
    #   由 bkIndexMark() 在頁面載入時決定要不要顯示（一場書籤都沒有就別擺著一個
    #   永遠篩不出東西的控制項——和上面那顆「人因浪費」同一個原則）。
    bm_toggle = ('<label class="wchk" id="fbwrap" style="display:none" '
                 'title="只列出有書籤的 session"><input type="checkbox" id="fb" '
                 'onchange="af()"> 🔖 只看有書籤的</label>')
    acc_th = "<th>帳號/來源</th>" if show_account else ""
    src_th = "<th>工具</th>" if show_source else ""
    tr = []
    for r in rows:
        acc = r.get("account", "")
        src = r.get("source_kind", SOURCE_CLAUDE)
        chip_title = (("夾 " + r.get("proj_munged", "") + (" · " + r["cwd"] if r.get("cwd") else ""))
                      if src == SOURCE_CLAUDE else r.get("cwd", ""))   # Codex 的 proj_munged 不是真夾，不標「夾」
        ai = r.get("ai_title", "")
        kind = r.get("kind") or "chat"
        blob = (source_label(src) + " " + acc + " " + r["proj"] + " "
                + r.get("cwd", "") + " " + r.get("proj_munged", "") + " "
                + r["title"] + " " + ai + " " + r.get("branch", "") + " " + kind).lower()
        acc_td = f'<td><span class="chip acc">{esc(acc)}</span></td>' if show_account else ""
        src_td = f'<td><span class="chip src">{esc(source_label(src))}</span></td>' if show_source else ""
        ns = r.get("n_subagents") or 0
        nc = r.get("n_compacts") or 0
        kind_badge = (f' <span class="chip kind" title="{esc_attr(KIND_TITLES.get(kind, ""))}">{esc(KIND_LABELS.get(kind, kind))}</span>'
                      if kind != "chat" else "")
        sub_badge = (f' <span class="chip sub" title="包含 {ns} 個子代理對話（其他 JSONL 內容）">🧩 ×{ns}</span>'
                     if ns else "")
        sub_badge += (f' <span class="chip compact" title="此 session 發生 {nc} 次壓縮（手動 /compact 或自動）">✂ ×{nc}</span>'
                      if nc else "")
        wn = r.get("waste_n") or 0
        wusd = r.get("waste_usd") or 0.0
        # 金額跟別處一樣走 `cost_label`：有人因步是未知型號時估不出錢，直接寫 $0
        # 會被讀成「沒多花錢」——浮欄、表頭、MD、②-b 四處都帶 `?`，這裡不帶就是五處對四處。
        wlbl = cost_label(wusd, r.get("waste_partial"))
        if wn:
            sub_badge += (
                f' <span class="chip waste" title="{esc_attr(f"此 session 有 {wn} 次人因可避免的冷啟（閒置過期／自行切帳號），估算多花 {wlbl}。結構性冷啟（第一句/被迫切帳號/換模型/壓縮/前綴變動）與伺服器側的提早失效都不計入——那些不是改習慣能省的。")}">'
                f'🔥 ×{wn}</span>')
        if r.get("rename"):
            title_html = f'<span class="named">✎ {esc(r["title"])}</span>{kind_badge}{sub_badge}'
            if ai and ai != r["title"]:
                title_html += f'<div class="subtitle">{esc(ai)}</div>'
        else:
            title_html = esc(r["title"]) + kind_badge + sub_badge
        mem_link = (f'<a class="memlink" href="{esc_attr(r["mem_href"])}" title="此專案 memory">🧠</a>'
                    if r.get("mem_href") else "")
        cost_cell = cost_label(r.get("cost", 0) or 0, r.get("cost_partial"))
        rc = r.get("resume_ctx") or 0
        rw = r.get("resume_window")
        rc_title = f' title="≈{round(100 * rc / rw)}% / {fmt_tokens(rw)}"' if (rc and rw) else ""
        resume_cell = (f'<td class="num" data-sort="{rc}"{rc_title}>~{fmt_tokens(rc)}</td>'
                       if rc else '<td class="num" data-sort="0"></td>')
        tr.append(
            # ⚠ `data-sid` 是索引頁那顆書籤圖示唯一的依據：書籤存在 localStorage 裡、
            #   建置期看不到，只能由頁面 JS 用 session id 對回來（見 _INDEX_BOOKMARK_JS）。
            f'<tr data-sid="{esc_attr(r.get("session_id", ""))}" '
            f'data-source="{esc_attr(src)}" data-acc="{esc_attr(acc)}" data-proj="{esc_attr(r["proj"])}" '
            f'data-month="{esc_attr(r.get("month",""))}" data-kind="{esc_attr(kind)}" '
            f'data-waste="{1 if wn else 0}" data-bm="0" data-text="{esc_attr(blob)}">'
            f'<td class="nowrap">{esc(r.get("date_str",""))}</td>'
            f'{src_td}'
            f'{acc_td}'
            f'<td><span class="chip" title="{esc_attr(chip_title)}">{esc(r["proj"])}</span></td>'
            f'<td><a href="sessions/{esc_attr(r["out_html"])}">{title_html}</a>{mem_link}</td>'
            f'<td class="num">{r["n_user"]}/{r["n_assistant"]}</td>'
            f'<td class="num">{r["n_tools"]}</td>'
            f'<td class="num" data-sort="{(r.get("cost", 0) or 0):.6f}">{cost_cell}</td>'
            f'{resume_cell}'
            f'<td class="num waste-td" data-sort="{wusd:.6f}">'
            + (esc(cost_label(wusd, r.get("waste_partial"))) if wn else "—")
            + '</td>'
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
    mem_js = js_embed(mem_map)      # 跳脫 < > 與 U+2028/9 的理由見 js_embed
    grand = cost_summary(rows)
    # ⚠ 管理頁的連結**無條件出現**，不像報告那樣看有沒有資料：書籤在 localStorage 裡，
    #   建置期永遠不知道有沒有。有書籤才顯示的話，第一次要找管理頁的人就永遠找不到。
    rlinks = ['<a href="sessions/bookmarks.html">🔖 書籤管理 →</a>']
    if cache_report:
        rlinks += ['<a href="cache-report.html">⚡ 快取分析報告 →</a>',
                   '<a href="cache-hypotheses.html">🧪 快取假說檢定 →</a>']
    if codex_report:
        rlinks.append('<a href="cache-codex.html">📈 Codex 快取存活 →</a>')
    report_links_div = f'<div class="smeta">{"　".join(rlinks)}</div>' if rlinks else ''
    body = f"""
<div class="wrap">
  <h1>AI 對話紀錄</h1>
  <div class="smeta">{len(rows)} 個 session · {len(sources) or 1} 種工具 · {len(accounts) or 1} 個帳號 · {len(projects)} 個專案{grand} · 產生於 {esc(local_str(datetime.now()))}</div>
  {report_links_div}
  <div class="filters">
    <input id="q" class="search" placeholder="🔍 搜尋標題 / 專案 / 分支…" oninput="af()">
    {src_select}
    {acc_select}
    {kind_select}
    <select id="fp" onchange="af()"><option value="">全部專案</option>{proj_opts}</select>
    <select id="fm" onchange="af()"><option value="">全部月份</option>{month_opts}</select>
    {waste_toggle}
    {bm_toggle}
    <button id="clr" class="clr" type="button" onclick="clearF()">清除</button>
    <span id="cnt" class="cnt"></span>
  </div>
  <div id="memstrip" class="memstrip" style="display:none"></div>
  <table id="tbl">
    <thead><tr>
      <th>日期 ▾</th>{src_th}{acc_th}<th>專案</th><th>標題</th>
      <th class="num">問/答</th><th class="num">工具</th><th class="num">估算$</th><th class="num" title="resume 後約載入的 context（最後一輪脈絡）">resume</th><th class="num" title="人因可避免的快取浪費：閒置過期／自行切帳號造成的重暖成本（結構性冷啟與伺服器側提早失效都不計）">浪費</th><th>分支</th><th>時長</th>
    </tr></thead>
    <tbody>{''.join(tr)}</tbody>
  </table>
</div>
<script>
var Q=document.getElementById('q'),FS=document.getElementById('fs'),FP=document.getElementById('fp'),FM=document.getElementById('fm'),FA=document.getElementById('fa'),FK=document.getElementById('fk'),FW=document.getElementById('fw'),FB=document.getElementById('fb'),CNT=document.getElementById('cnt');
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
function lsSet(k,v){{try{{localStorage.setItem(k,v);return true;}}catch(e){{return false;}}}}
var FKEY='idx_filter_v1';
/* ⚠ `b` ＝「只看有書籤的」。它和其他七個條件一樣要被記住：實際用法是**勾著它來來回回**
   （索引 → 某一場 → 回索引 → 下一場），而每次回索引都是一次重新載入。
   ⚠ 還原就在 `restoreF()` 裡（往下十行，那裡有完整的理由）。
   ⚠⚠ **這裡原本寫著「還原不在 `restoreF()` 裡，在 `bkIndexRestoreFilter()`」——那是 2026-08-23
   修好之前的舊說法，而且 `bkIndexRestoreFilter` 這個名字整份 repo 裡不存在。**
   留著它會讓下一個人照錯的那條改，直接重演教訓 39。
   ⇒ 一般化：**改了承重的機制，就要去找「解釋舊機制」的那幾段註解**，
   它們不會跟著程式一起變紅。 */
function saveF(){{lsSet(FKEY,JSON.stringify({{q:Q.value,s:FS?FS.value:'',p:FP.value,m:FM.value,a:FA?FA.value:'',k:FK?FK.value:'',w:(FW&&FW.checked)?1:0,b:(FB&&FB.checked)?1:0}}));}}
function setSel(el,v){{if(!el||!v)return;for(var i=0;i<el.options.length;i++){{if(el.options[i].value===v){{el.value=v;return;}}}}}}
/* ⚠⚠ **`b`（只看有書籤的）一定要在這裡還原，不可以拖到 `bkIndexMark()` 那邊。**
   下一行就是 `restoreF();af();`，而 **`af()` 尾端會 `saveF()`** ⇒ 還原晚一步的話，
   那一次 `af()` 會拿「此刻還沒勾」的狀態把 `b` 覆寫成 0，於是永遠記不住。
   2026-08-23 真的這樣出貨過一次：探針全綠、實際使用無效——因為探針是「存完馬上手動還原」，
   中間**沒有那一次 `af()`**，恆綠（教訓 39）。
   ⚠ 這裡勾起來時每一列的 `data-bm` 還沒填 ⇒ 第一次 `af()` 會把整張表濾成空的。
   **那不會被看到**：整段（含 `_INDEX_BOOKMARK_JS`）是同一個同步 `<script>`，
   `bkIndexMark();af();` 在第一次繪製之前就跑完了。
   ⚠ 「這份索引一場書籤都沒有就取消勾選」那條規則仍然有效（在 `bkIndexMark()` 尾端），
   它排在這之後，所以藏起來的篩選不會被還原回來。 */
function restoreF(){{var raw=lsGet(FKEY);if(!raw)return;try{{var f=JSON.parse(raw);if(f.q)Q.value=f.q;setSel(FS,f.s);setSel(FP,f.p);setSel(FM,f.m);setSel(FA,f.a);setSel(FK,f.k);if(FW)FW.checked=!!f.w;if(FB)FB.checked=!!f.b;}}catch(e){{}}}}
function clearF(){{Q.value='';if(FS)FS.value='';FP.value='';FM.value='';if(FA)FA.value='';if(FK)FK.value='';if(FW)FW.checked=false;if(FB)FB.checked=false;af();}}
function af(){{var q=Q.value.toLowerCase(),s=FS?FS.value:'',p=FP.value,m=FM.value,a=FA?FA.value:'',k=FK?FK.value:'',w=(FW&&FW.checked),bm=(FB&&FB.checked),n=0;
 ROWS.forEach(function(r){{var ok=(!q||r.dataset.text.indexOf(q)>=0)&&(!s||r.dataset.source===s)&&(!p||r.dataset.proj===p)&&(!m||r.dataset.month===m)&&(!a||r.dataset.acc===a)&&(!k||r.dataset.kind===k)&&(!w||r.dataset.waste==='1')&&(!bm||r.dataset.bm==='1');
  r.style.display=ok?'':'none';if(ok)n++;}});
 CNT.textContent=n+' / '+ROWS.length;updMem(p,s,a);saveF();}}
restoreF();af();
document.querySelectorAll('#tbl th').forEach(function(th,i){{th.onclick=function(){{
 var tb=document.querySelector('#tbl tbody'),rs=[].slice.call(tb.rows);
 th._d=!th._d;rs.sort(function(a,b){{var ca=a.cells[i],cb=b.cells[i];
  var x=ca.dataset.sort!==undefined?ca.dataset.sort:ca.innerText,y=cb.dataset.sort!==undefined?cb.dataset.sort:cb.innerText;
  var nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)){{return th._d?nx-ny:ny-nx;}}
  return th._d?String(x).localeCompare(y):String(y).localeCompare(x);}});rs.forEach(function(r){{tb.appendChild(r);}});}};}});
{_INDEX_BOOKMARK_JS}
</script>
"""
    return html_page("Claude Code 對話紀錄", body)


def render_index_md(rows, show_account=False, cache_report=False, codex_report=False) -> str:
    by_source_proj = {}
    for r in rows:
        by_source_proj.setdefault((r.get("source_kind", SOURCE_CLAUDE), r["proj"]), []).append(r)
    sources = {r.get("source_kind", SOURCE_CLAUDE) for r in rows}
    n_acc = len({r.get("account", "") for r in rows if r.get("account")}) or 1
    out = ["# AI 對話紀錄", "",
           f"{len(rows)} 個 session · {len(sources) or 1} 種工具 · {n_acc} 個帳號 · {len({p for _, p in by_source_proj})} 個專案{cost_summary(rows)}", ""]
    links = []
    if cache_report:
        links += ["⚡ [快取分析報告](cache-report.md)", "🧪 [快取假說檢定](cache-hypotheses.md)"]
    if codex_report:
        links.append("📈 [Codex 快取存活](cache-codex.md)")
    if links:
        out += [" · ".join(links), ""]
    for src, proj in sorted(by_source_proj, key=lambda x: (source_label(x[0]), x[1])):
        out.append(f"## {source_label(src)} / {proj}")
        out.append("")
        for r in sorted(by_source_proj[(src, proj)], key=lambda x: x.get("start_ts") or 0, reverse=True):
            acc = f"`{r.get('account','')}` · " if (show_account and r.get("account")) else ""
            mark = "✎ " if r.get("rename") else ""
            cost = cost_label(r.get("cost", 0) or 0, r.get("cost_partial"))
            kind = r.get("kind") or "chat"
            wn = r.get("waste_n") or 0
            # 金額一律走 `cost_label`（td／MD／tooltip 三處同一個口徑）：估不出來時它給 `?`，
            # 而 `fmt_money(0)+"+?"` 會給 `$0+?`——同一列上下文出現兩種寫法最難察覺。
            waste = (f" · 🔥人因浪費 "
                     f"{cost_label(r.get('waste_usd') or 0, r.get('waste_partial'))}"
                     f"（{wn} 次）") if wn else ""
            out.append(f"- {acc}[{mark}{r['title']}](sessions/{r['out_md']}) — {r.get('date_str','?')} · "
                       f"~{cost} · {r['n_user']}問/{r['n_assistant']}答 · 🔧{r['n_tools']}"
                       + (f" · 型態:{KIND_LABELS.get(kind, kind)}" if kind != "chat" else "")
                       + waste
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
# ② 段的敘述把冷啟拆成四桶。**四桶必須剛好蓋滿 REPORT_CAUSES 的每一個鍵**，否則「共 N 次冷啟」
# 會大於列出來的各桶合計，那幾次在敘述層等於憑空消失（`test_smoke` 有一條測試釘住這個分割）。
# 新增成因時：想清楚它屬於哪一桶並加進去，不要只加進 REPORT_CAUSES。
_UNAVOIDABLE_CAUSES = ("first", "switch", "model", "compact", "unavail")
_API_PREFIX_CAUSES = ("tools", "system", "msgs")   # API 自報的前綴變動（習慣可避免）
_UNKNOWN_CAUSES = ("unknown",)      # 自報了、但本工具還不認得的型別：自成一桶，不硬塞進別桶
_AVOIDABLE_CAUSES = ("expiry", "evict", "acct")   # 「可避免/異常」：非結構性、人因或閒置造成（③ 實色）。
# 索引「人因浪費」徽章／欄位只認**使用者自己造成**的那兩種，刻意**不含 evict**：
# evict 是「1h TTL 內卻冷啟」的伺服器側異常，把它算進「人因」等於要人為自己控制不了的事負責，
# 與本工具紅/灰配色的既有立場（只有真的自己弄丟才醒目）自相矛盾。
# 這個集合也讓索引合計＝報告的「可避免浪費」KPI（該 KPI 同樣只累加 expiry＋acct）。
_HUMAN_CAUSES = ("expiry", "acct")
# 註：API 自報的前綴變動（tools/system/msgs）刻意不列此——它們「改習慣可避免」但「早點回來不可避免」，
# 與本 bar 實色語意（閒置行為）不同，故歸淡色；③ 的 tooltip／圖例必須把它們列進去，否則與 ② 打架。


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


def build_cache_report(rows, stratify=True, acct_health=None):
    """從各 session 的 cache_steps／cache_events 彙整快取分析（v2）。回傳 dict（has_data=False 代表沒資料）。
    四條主線：
      (A) 成因分解──每個冷啟恰好歸一因，優先序為 **API 自報 > 邊界事件(switch/model/compact) > 間隔推論**
          （REPORT_CAUSES 只是顯示順序，不代表優先序）；
          結構性成因（第一句/切帳號/換模型/壓縮）的相鄰步 pair 不進 TTL 估計（競爭風險：它們死於
          別的原因，混入會污染 TTL 曲線）。**API 自報的前綴變動**（工具定義/系統提示/前文/換模型）
          同樣整對移出 TTL 與帶內樣本，但理由不同：那些步的命中率分母含被迫重寫的量、已被污染，
          冷熱都移（只挑冷的移會把存活率灌高）；其中「本來會落在應命中帶」的對數另計 band_excluded，
          恆等於帶內樣本 n 的減少量（假說頁引用此值，全母體的 excluded 不可拿來對）。
      (B) TTL 存活──分桶命中率＋Wilson CI；cohort 依「最近一次有寫入的步」之 TTL 分
          「1h／5m／未知(舊資料無細分)」——cur 讀的快取是那次寫入存的。
          KPI「TTL 遵約率」＝1h cohort 在應命中帶 [REPORT_INTRA_SEC, REPORT_TTL_SAFE_SEC) 的命中率，
          其補數＝「提早失效率」（1h 快取理論上帶內必命中；TTL 每次使用會刷新，gap＝距上次使用）。
      (C) 重暖作息──各帳號時間軸閒置 ≥ REPORT_BREAK_SEC 後的冷啟，本地時段直方圖
          （平日/假日 × 可避免/異常 vs 非閒置造成，正規化為 次/活躍日）。
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
    masked_total = {k: 0 for k in cause_keys}   # 實據勝出、但同一步仍成立的人因條件
    masked_steps = 0
    # API 自報成因（diagnostics.cache_miss_reason）：冷啟／部分失效分開記，並估算失效前綴長度與多付的錢。
    api_cold = {}                                   # reason -> 次數（冷啟步）
    api_partial = {}                                # reason -> 次數（命中率仍 ≥ 門檻、只掉一段）
    api_tok = {"cold": 0, "partial": 0}
    api_usd = {"cold": 0.0, "partial": 0.0}
    api_usd_partial = False                         # 有前綴變動步是未知模型、金額估不出（比照 avoid_partial 標 +?）
    api_excluded = 0                                # 因「前綴變動」被排除的相鄰步對（全母體）
    band_excluded = 0                               # 其中「本來會落在應命中帶」的對數（假說頁引用此值）
    srv_excluded = 0                                # 因「伺服器側自報不可用」而整對移出的對數
    # (v36-fam5 #1) 標成 `acct`（人因、記錢）但同場稍早出現過 limit/auth 的步數——那次切換
    # 可能是被迫的。**只揭露，不改成因也不改金額**；顯示端在 `_cold_cause_label` 並列標示。
    acct_limit_earlier = 0
    cf_comply_n = 0                                 # 反事實帶內 n：假裝「API 自報」這條規則不存在時的樣本數。
    #   band_excluded ＝ cf_comply_n − comply_n。不能只數「被直接排除的那一對」——前綴變動步若沒有新寫入，
    #   lineage 會失效，**下游**的對也會從 1h cohort 掉進 unknown、一併離開帶內；只數直接排除會少報，
    #   而假說頁與 KPI 副標都拿這個數字當「另有 N 對移出」的揭露。
    band_api = {}                                   # 應命中帶內失效時 API 說了什麼（"-"＝沒說）
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
        # (v36-fam5 #1) 與 classify_cache_causes 同一份（兩處是刻意的雙胞胎）：整條時間軸上的
        # limit/auth 時刻，判「這次 acct 之前同場有沒有撞過 limit」。不走會前進的事件游標。
        # ⚠⚠ **名字不可以叫 `limit_ts`**（v36-fam6 #1，Blocker）：迴圈外 `:4100` 有一個同名的
        #   **跨 session 累加器**（收折疊後的 429，餵報告 ④「什麼時候撞到 limit」）。在迴圈內
        #   重新繫結那個名字會把累加器整個蓋掉：`limits_n` 從 100 掉到 **0**，而 `if d["limits_n"]:`
        #   是整段 ④ 的開關 → **那一章連同時段表一起靜默消失**，沒有任何提示。
        #   本檔的 per-row 區域變數一律加 `row_` 前綴，避免再撞到迴圈外的累加器。
        row_limit_ts = sorted(float(t) for t, k in events if k in ("limit", "auth"))
        for st in raw:
            tok_read += st[1]
            tok_in += st[2]
            mode_n["1h" if st[5] > 0 else ("5m" if st[4] > 0 else "unknown")] += 1
        steps = [(i, st) for i, st in enumerate(raw) if st[2] >= REPORT_MIN_CTX]
        # API 自報成因盤點（獨立於成因判定：連「命中率看起來正常、但 API 說有一段被重寫」的
        # 部分失效也算進來——那是逐步徽章之外唯一看得到它的地方）。
        # 母體用 raw、不用 ctx 過濾後的 steps：ctx < REPORT_MIN_CTX 的過濾是為了「由命中率推論成因」
        # 才存在的（暖機步的命中率無參考意義），但 API 自報是**事實**、不是推論，對它不適用。
        # 用 steps 會少算次數／失效前綴長度／金額，還可能把未知的新型別整個吞掉（違反不變量⑤），
        # 而 ②-b 文案又宣稱母體是「所有有自報成因的呼叫」——數字與宣稱不符。
        for st in raw:
            reason, mtok = _step_miss(st)
            if not reason:
                continue
            bucket = "cold" if _step_cold(st) else "partial"
            (api_cold if bucket == "cold" else api_partial)[reason] = \
                (api_cold if bucket == "cold" else api_partial).get(reason, 0) + 1
            api_tok[bucket] += mtok
            mi = st[6]
            w = rewrite_waste_usd(models[mi] if 0 <= mi < len(models) else "", mtok,
                                  st[4], st[5], wrote=st[3])
            # 注意：這裡對**任何**帶 cache_missed_input_tokens 的成因累加。今天實資料只有四種前綴變動類
            # 會附長度（previous_message_not_found／unavailable 恆為 0），但那是資料性質、非程式保證 →
            # ②-b 文案只能說「被迫重算的代價」，不可斷言「是你改前綴的代價」。（若哪天 CLI 也替
            # unavailable 附長度，那是伺服器側失效，且 pmnf 的金額會開始與 KPI avoid_usd 重疊。）
            if w:
                api_usd[bucket] += w
            elif mtok > 0 and st[3] > 0:
                api_usd_partial = True              # 未知模型：有重算量卻算不出錢 → 標記，渲染時附 +?
        last_lim = None
        for t, kind in events:
            if kind == "limit" and (last_lim is None or t - last_lim > 600):
                limit_ts.append(t)
                last_lim = t
        cause_by_t = {}          # 鍵＝步驟在 raw 裡的索引（不是 epoch，見下方 timelines 那段）
        if steps and steps[0][0] == 0 and _step_cold(steps[0][1]):   # 原始第 0 步＝session 第一句
            t0 = steps[0][1][0]
            # 同 classify_cache_causes：首步有 API 自報就以自報為準——快取跨 session 共用，第一句仍
            # 可能命中上一場留下的前綴，此時 tools/system/msgs 才是真成因；標 first 會把「習慣可避免」
            # 的成因記進「不可避免」桶，也讓 ② 與 ②-b／逐步徽章對同一步說法不一。
            # 同 classify_cache_causes：直接證據兩個 dict 都要查（兩處是刻意的雙胞胎）。
            fc = (API_MISS_CAUSE.get(_step_miss(steps[0][1])[0])
                  or API_OTHER_CAUSE.get(_step_miss(steps[0][1])[0]))
            if fc == "msgs" and any(k == "compact" and t <= t0 for t, k in events):
                fc = "compact"
            fc = fc or "first"
            causes_total[fc] += 1
            cold_events.append((t0, fc))
            cause_by_t[steps[0][0]] = fc
        ei, n_ev = 0, len(events)
        cf_last_write = None   # 反事實 lineage：與 last_write 同步更新，但**不因前綴變動重置**（只因邊界）
        last_write = None      # 最近一次有寫入的可分析步之 TTL（"1h"/"5m"）——cur 讀的快取以此為準；
        for (ip, prev), (ic, cur) in zip(steps, steps[1:]):   # 舊資料無細分則一路 None → cohort=unknown
            if prev[5] > 0:
                last_write = "1h"
                cf_last_write = "1h"
            elif prev[4] > 0:
                last_write = "5m"
                cf_last_write = "5m"
            gap = cur[0] - prev[0]
            if gap < 0:
                neg_gaps += 1
                continue
            n_pairs += 1
            while ei < n_ev and events[ei][0] <= prev[0]:
                ei += 1
            kinds = set()
            acct_ts = []
            j = ei
            # 同 classify_cache_causes：上界用截到整秒的事件時刻比（兩處是刻意的雙胞胎）。
            while j < n_ev and int(events[j][0]) <= cur[0]:
                kinds.add(events[j][1])
                if events[j][1] == "acct":
                    acct_ts.append(events[j][0])     # 排除判準要看落點，不只是有沒有發生
                j += 1
            ei = j          # 同 classify_cache_causes：消費過就推過去，一次性（兩處雙胞胎）
            cold = _step_cold(cur)
            boundary = ("switch" if ("limit" in kinds or "auth" in kinds) else
                        "model" if (prev[6] >= 0 and cur[6] >= 0 and prev[6] != cur[6]) else
                        "compact" if "compact" in kinds else None)
            # 前綴變動（工具/系統提示/前文/換模型）是 client 造成、必然重寫，不是快取沒撐住 →
            # 與結構性邊界同樣視為競爭風險：定成因用它，且該相鄰步對不進 TTL 存活／應命中帶統計。
            api_cause = API_MISS_CAUSE.get(_step_miss(cur)[0])
            # 同 classify：**不向前借**。但這一側**會**整對移出樣本（下面的 `srv_excluded`）——
            # 兩側在「成因」上一致、在「樣本」上只有這一側有動作，那是刻意的分工。
            srv_cause = API_OTHER_CAUSE.get(_step_miss(cur)[0])
            if not api_cause:
                # 同 classify_cache_causes：夾在中間、被 ctx 門檻濾掉的前綴變動步照樣污染這一對。
                # 範圍限制 SCOPE-FILTERED-STEP-CAUSE 同樣適用於此（兩處是刻意的雙胞胎）。
                api_cause = next((c for r in raw[ip + 1:ic]
                                  if (c := API_MISS_CAUSE.get(_step_miss(r)[0]))), None)
            if api_cause == "msgs" and boundary == "compact":
                api_cause = "compact"      # 同 classify_cache_causes：壓縮邊界比通稱 msgs 更準
            cohort = last_write or "unknown"
            ttl_bound = REPORT_TTL_SAFE_SEC if cohort == "1h" else 5 * 60
            # 切帳號落在 TTL 邊界**之前**＝它可能在快取還活著時就把整段前綴丟掉（判準的完整說明
            # 在下方排除那一段）。這裡先算出來，反事實基準與實際排除要用**同一個**值。
            acct_kills_live = any(t < prev[0] + ttl_bound for t in acct_ts)
            # 反事實帶內計數（「API 自報」規則不存在的世界）。
            # ⚠ 反事實與實際**只准差 API 自報那一條規則**：其餘排除（結構性邊界、切帳號）兩邊都要
            # 照樣生效，lineage 重置也要同步。少扣一項，被那一項排掉的對就會落進
            # `cf_comply_n − comply_n` 的差額裡，而那個差額對外揭露成「因前綴變動移出」。
            # ⚠ `srv_cause` 兩個世界都要扣：`band_excluded` 揭露的是「**API 自報前綴變動**這條
            # 規則造成的樣本減少」，而伺服器側不可用是另一條規則、在反事實裡照樣成立。
            # 只扣實際那側的話，這些對會落進差額、被講成「因前綴變動移出」。
            if (not boundary and not acct_kills_live and not srv_cause
                    and (cf_last_write or "unknown") == "1h"
                    and REPORT_INTRA_SEC <= gap < REPORT_TTL_SAFE_SEC):
                cf_comply_n += 1
            # 反事實 lineage 與實際共用 `_resets_lineage`，**只**把 `api_cause` 那一格關掉——
            # 這個世界裡「API 自報前綴變動」那條規則不存在，其餘三個入口照樣成立。
            # 寫成同一支之後，「兩邊只准差這一條規則」從註解的承諾變成看得見的參數差。
            if _resets_lineage(None, srv_cause, boundary, bool(acct_ts)):
                cf_last_write = None
            if cold:
                if api_cause:
                    cause = api_cause
                elif boundary:
                    cause = boundary
                elif srv_cause:
                    cause = srv_cause     # 同 classify_cache_causes 的順序（邊界之後、推論之前）
                elif gap >= ttl_bound:
                    cause = "expiry"
                elif "acct" in kinds:
                    # 與 classify_cache_causes 同規則（兩邊由 test_smoke 的一致性測試釘住）：
                    # 直接的帳號邊界證據勝過由間隔推論的 intra／evict，不分間隔長短。
                    cause = "acct"
                    # (v36-fam5 #1) 這一步之前同場出現過 limit/auth ⇒ 這次切換可能是被迫的。
                    # ⚠ **只計數、只揭露，不動成因也不動金額**（Will 2026-08-21 裁決）；
                    #   顯示端的並列標示在 `classify_cache_causes` 那側，兩邊同一個判準。
                    if row_limit_ts and row_limit_ts[0] <= prev[0]:
                        acct_limit_earlier += 1
                elif gap >= REPORT_INTRA_SEC:
                    cause = "evict"
                else:
                    cause = "intra"
                causes_total[cause] += 1
                cold_events.append((cur[0], cause))
                # 與 classify_cache_causes 同規則（兩邊是刻意的雙胞胎、共用 `_masked_human`）：
                # 實據勝出時同一步仍成立的人因條件要數得出來，否則它只活在逐步徽章、報告看不見。
                also = _masked_human(cause, (api_cause, srv_cause, boundary),
                                     kinds, gap, ttl_bound)
                if also:
                    masked_steps += 1
                    for c in also:
                        masked_total[c] += 1
                cause_by_t[ic] = cause
                if cause in ("expiry", "acct"):
                    # 可避免的重暖成本 ≈ 本步實際寫入成本 −（若命中）同量讀取成本
                    mi = cur[6]
                    price = model_price(models[mi]) if 0 <= mi < len(models) else None
                    if price:            # 寫入依本步自己的 TTL 細分計價（同 call_cost 規則）
                        legacy = max(cur[3] - cur[4] - cur[5], 0)
                        write = (legacy + cur[4]) * CACHE_WRITE_MULT + cur[5] * CACHE_WRITE_MULT_1H
                        avoid_usd += (write - cur[3] * CACHE_READ_MULT) * price[0] / 1_000_000
                    else:
                        avoid_partial = True
            # ── lineage 重置：四個入口一次算完，與 classify_cache_causes 共用 `_resets_lineage` ──
            # ⚠ **不可以**再散回下面每個 continue 站點各寫一次：散寫正是 `v36-fam3` F1 那條分岔的
            # 來源（`srv_cause` 當時只有這一側寫了，classify 那側沒有 → 同一步在報告端算 expiry
            # 記人因錢、在逐步徽章端算 evict 不記錢）。
            # 位置：`cohort` 在上面已經取過值，所以這裡改不會動到本對自己的分桶。
            if _resets_lineage(api_cause, srv_cause, boundary, bool(acct_ts)):
                last_write = None
            if api_cause:
                # API 說前綴被改掉：這步的命中/未命中被「客戶端改了前綴」污染（分母含被迫重寫的量），
                # 不反映快取有沒有撐住 → 整對移出風險集（competing risk）。**冷、熱都移**：
                # 只挑冷的移除等於只刪失敗樣本，會把存活率灌高，比不處理更糟。
                # 只在「API 自報是唯一排除理由」時計數：同時有邊界（切帳號/換模型/壓縮）的對，
                # 在這條規則出現以前就已被 boundary 擋掉，算成「因自報而排除」是錯誤歸因。
                # （model_changed 幾乎必然伴隨 model 邊界，不擋就會系統性灌水。）
                if not boundary:
                    api_excluded += 1         # 全母體排除數；帶內減少量改由 cf_comply_n 反事實差額算
                continue                      # lineage 已在上面統一重置
            # 切帳號落在 TTL 邊界**之前** → 它可能在快取還活著時就把整段前綴丟掉，這一對量到的
            # 就不是「快取撐了多久」。落在邊界**之後** → 快取在切換發生前已自然過期，這一對是
            # 有效的存活樣本，排掉只會讓母體平白變小。
            # ⚠ 判準必須是「切換時刻相對邊界的位置」，**不能**改用「最後選中的成因」：成因那條
            # 判定是 `elif gap >= ttl_bound: "expiry"`，只看間隔長短、不看切換落在間隔的哪裡。
            # 拿成因當判準的話，「呼叫完 10 秒就切換、下一次呼叫隔了一小時才來」會因為間隔夠長
            # 而被判成 expiry、進而被收進存活樣本——那一對其實 10 秒就死了，證明不了任何存活。
            # （`acct_kills_live` 在上面 `ttl_bound` 之後就算好了，反事實基準用的是同一個值。）
            if srv_cause:
                # 伺服器側自報不可用：那一次沒中是**伺服器的事**，不是快取沒撐住 → 與前綴變動
                # 同樣視為競爭風險，整對移出存活／帶內樣本（命中與未命中都移；只挑未命中移會
                # 把存活率灌高）。不移的話，KPI 磚的「提早失效」會把它算進去，而 ② 成因表已經
                # 把它分到「伺服器不可用」——同一頁上同一個詞會變成兩個數字。
                # ⚠ **只影響統計，不影響標示**：成因與並列標籤在上面 `if cold:` 那段就算完了，
                # 逐步徽章仍會標「閒置過期＋伺服器不可用」，讓人看得出「就算伺服器沒掛，
                # 這一步的快取也還是會失效」。
                # (v36-fam3 F6) 與 `api_excluded` 同一個判準：**只在「伺服器自報是唯一排除理由」
                # 時計數**。`if srv_cause:` 排在 `if boundary or acct_kills_live:` 之前，所以同時有
                # 邊界（切帳號/換模型/壓縮）的那一對會先落到這裡——那種對在這條規則出現以前就
                # 已被 boundary 擋掉，算成「因自報而排除」是錯誤歸因。兩個計數器都對外揭露
                # （`_srv_excluded_note` / 帶內那句），口徑不一樣讀者無從對帳。
                # ⚠ 只影響揭露數字，不影響任何統計母體（兩條路都是整對移出）。
                if not boundary:
                    srv_excluded += 1
                continue                      # lineage 已在上面統一重置
            if boundary or acct_kills_live:
                # 結構性成因：不進 TTL 存活／帶內統計（競爭風險）。lineage 的重置已在上面統一做掉
                # ——切帳號/換模型/壓縮後是全新前綴，邊界前的寫入 TTL 不再適用；
                # 新 lineage 的 TTL 由邊界後第一個有寫入的步重新建立（其前皆視為 unknown）。
                # ⚠ `acct`（自願切帳號）在**成因**上刻意只搶 evict 那一格（見上方判定），但在
                # **統計**上它與 limit/auth 切帳號是同一件事：快取按組織／workspace 隔離、被整段
                # 丟掉，不是
                # 「沒撐過 TTL」。不在這裡一起排除的話，它會留在 comply_n／evict_form／UTC 尖峰
                # 樣本裡被當成一次「提早失效」——正是本工具要消滅的那種污染，只是換個地方發生。
                continue                      # lineage 已在上面統一重置
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
                    # 帶內失效時 API 怎麼說。前綴變動類與伺服器側不可用**都**已在上面整對排除，
                    # 所以這裡收得到的只會是 previous_message_not_found、未知碼，或「沒說」。
                    r_api = _step_miss(cur)[0]
                    band_api[r_api or "-"] = band_api.get(r_api or "-", 0) + 1
            # ⚠ 這一對即使被計入（切換晚於 TTL 邊界、快取在切換前就自然過期了），lineage 照樣
            # 已在上面被重置——`_resets_lineage` 的 acct 入口問的是「窗裡有沒有切帳號」，
            # 不是 `acct_kills_live`。切換之後是全新前綴，這與它算不算存活樣本是兩個問題。
        acct = r.get("account", "")
        tl = timelines.setdefault(acct, [])
        # ⚠ 鍵用**步驟在 raw 裡的索引**，不是 epoch：同一秒的兩步會用同一把 epoch 鍵互相覆蓋，
        # ③ 重暖作息的著色就跟著錯。索引是唯一的，不必再另外消歧義。
        for _i, st in steps:
            tl.append((st[0], _step_cold(st), cause_by_t.get(_i)))
        all_ts.extend(st[0] for _, st in steps)

    # ── (C) 重暖作息：各帳號時間軸上 ≥ REPORT_BREAK_SEC 的閒置後、確實冷啟的第一步 ──
    resumes = []
    for tl in timelines.values():
        # 只用 (時刻, 冷熱) 當鍵：第三欄是成因，未歸因時是 None，同時刻同冷熱的兩步會讓 tuple
        # 比較走到它身上而丟 TypeError，整個建置中止。排序本來也只需要時間先後。
        tl.sort(key=lambda x: (x[0], x[1]))
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

    # 型態分層（review vs 一般）：同一套判定分別跑在兩個子母體上，供報告做「分開統計」比較。
    # exec 是 Codex 專屬，Claude 只會有 review/chat；stratify=False 的子呼叫不再往下分層（防遞迴）。
    by_kind = None
    if stratify:
        present = [kk for kk in ("review", "chat") if any(r.get("kind") == kk for r in claude)]
        if len(present) >= 2:
            by_kind = {kk: build_cache_report([r for r in claude if r.get("kind") == kk], stratify=False)
                       for kk in present}

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

    # 「因前綴變動而移出應命中帶」的完整樣本減少量＝反事實帶內 n − 實際帶內 n。
    # 涵蓋兩種來源：① 該對被直接排除；② 前綴變動步沒有新寫入 → lineage 失效 → 下游的對從 1h
    # 掉進 unknown cohort、也離開帶內。只數 ① 會少報，而假說頁與 KPI 副標拿它當「另有 N 對移出」揭露。
    band_excluded = max(cf_comply_n - comply_n, 0)

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
            # 人因（閒置過期＋自行切帳號）；用常數求和而非寫死鍵名，日後再加成因不會靜默漏算
            "avoid_n": sum(causes_total[c] for c in _HUMAN_CAUSES),
            "avoid_week": sum(causes_total[c] for c in _HUMAN_CAUSES) / span_weeks,
        },
        # (B) 存活
        "cohort_bins": cohort_bins,
        "n_pairs": n_pairs,
        "warm_max": warm_max,
        "top_warm": sorted(top_warm, reverse=True)[:6],
        # (A) 成因
        "causes_total": causes_total,
        "masked_human": {"steps": masked_steps,
                         "causes": {k: v for k, v in masked_total.items() if v}},
        "cause_periods": periods,
        # (A2) API 自報成因（實據）：冷啟／部分失效各自的次數、失效前綴長度與估算多付的錢
        "api": {"cold": api_cold, "partial": api_partial,
                "cold_tok": api_tok["cold"], "partial_tok": api_tok["partial"],
                "cold_usd": api_usd["cold"], "partial_usd": api_usd["partial"],
                "usd_partial": api_usd_partial,
                "excluded": api_excluded, "band_excluded": band_excluded, "band": band_api,
                "srv_excluded": srv_excluded, "acct_limit_earlier": acct_limit_earlier,
                "n": sum(api_cold.values()) + sum(api_partial.values())},
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
        "by_kind": by_kind,       # {kind: 子報告 dict}；型態分層比較用（None＝母體只有單一型態，不比較）
        # 切帳號偵測的涵蓋率（load_account_switches 的 health）：報告要據此說明「acct 的 0」
        # 是哪一種 0。{} ＝ 這次建置沒有量到（例如 --no-claude），文案要跟「量到 0」分開講。
        "acct_health": dict(acct_health or {}),
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
    """時段圖（每列一小時）：bar 分兩段＝可避免/異常（實色；_AVOIDABLE_CAUSES＝人因兩種＋提早失效）
    ＋非閒置造成（同色調淡；含 API 自報的前綴變動），
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
            f'<span class="hseg a" style="width:{wa}px" '
            f'title="可避免/異常（人因：閒置過期／自行切帳號＋伺服器側：提早失效）：{a} 次"></span>'
            f'<span class="hseg u" style="width:{wu}px" title="非閒置造成（第一句/切帳號/換模型/壓縮，或 API 自報的前綴變動）：{u} 次"></span>'
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


def _api_miss_rows(d):
    """API 自報成因的彙總列：[(成因鍵, 冷啟次數, 部分失效次數)]，總次數多的在前。"""
    api = d.get("api") or {}
    cold, partial = api.get("cold") or {}, api.get("partial") or {}
    keys = sorted(set(cold) | set(partial),
                  key=lambda r: (-(cold.get(r, 0) + partial.get(r, 0)), r))
    return [(r, cold.get(r, 0), partial.get(r, 0)) for r in keys]


def _api_miss_html(d):
    """②-b：Claude 自報的失效成因全貌，含「命中率看起來正常、卻有一段被重寫」的部分失效。
    部分失效在 ⚡% 上完全看不出來（分母同步變大），只有這裡與逐步徽章看得到。"""
    api = d.get("api") or {}
    rows = _api_miss_rows(d)
    parts = ["<h3>②-b Claude 自報的失效成因（含部分失效）</h3>"]
    if not rows:
        parts.append('<div class="lead">這批資料沒有 API 自報成因——2026-05 之前的 Claude Code 版本'
                     "還沒有 <code>diagnostics.cache_miss_reason</code> 這個欄位，"
                     "或期間內每次呼叫都完整命中。上面的成因全部來自本工具推論。</div>")
        return "".join(parts)
    n_cold = sum(c for _, c, _ in rows)
    n_part = sum(p for _, _, p in rows)
    tok = api.get("partial_tok", 0) + api.get("cold_tok", 0)
    usd = api.get("partial_usd", 0.0) + api.get("cold_usd", 0.0)
    # 用 cost_label 而非 fmt_money：全部步驟都是未知模型時 usd＝0，仍要顯示 ?／+? 提示金額算不出來，
    # 不能整句消失（那會讓讀者以為「沒有多付」）。
    money = (f"、比直接命中多付 <b>~{esc(cost_label(usd, api.get('usd_partial')))}</b>"
             if (usd or api.get("usd_partial")) else "")
    parts.append(f'<div class="insight">Claude 明講失效成因的呼叫共 <b>{n_cold + n_part}</b> 次：'
                 f'冷啟 {n_cold} 次、<b>部分失效 {n_part}</b> 次'
                 f'（命中率仍在門檻之上、但有一段被迫重算——在 ⚡% 上看不出來）。'
                 f'API 有附「失效前綴長度」的合計 <b>{fmt_tokens(tok)}</b> tokens{money}'
                 "（失效前綴＝從多久以前開始對不上，不等於重寫量；金額以各步實際寫入量為上限估算，"
                 "算的是「實際被重寫的那部分 token 比直接命中多付多少」，不等於「本來全都省得下來」）。"
                 "本節母體＝所有有自報成因的呼叫（含 ② 歸為「session 第一句」的首步、及 ② 逐對統計略過的步），"
                 "故各成因的「冷啟」數不必等於 ② 對應列。</div>")
    body = "".join(f"<tr><td>{esc(miss_label(r))}</td><td class='num'>{c}</td><td class='num'>{p}</td>"
                   f"<td class='hn'>{esc(API_MISS_NOTES.get(r, ''))}</td></tr>"
                   for r, c, p in rows)
    parts.append('<table class="rep"><thead><tr><th>API 自報成因</th><th class="num">冷啟</th>'
                 '<th class="num">部分失效</th><th>意思</th></tr></thead>'
                 f"<tbody>{body}</tbody></table>")
    if api.get("excluded"):
        parts.append(f'<div class="lead">另有 <b>{api["excluded"]}</b> 對相鄰步因「前綴被改掉」'
                     "（工具定義／系統提示／前文／換模型）排除在 TTL 存活與應命中帶統計之外："
                     "這些步的命中率分母含被迫重寫的量，已被污染，不反映快取有沒有撐住——"
                     "所以整對移出風險集（冷、熱都移；只挑冷的移會把存活率灌高）。</div>")
    return "".join(parts)


def _api_miss_md(d):
    """②-b 的 Markdown 版（與 _api_miss_html 同一批數字）。"""
    api = d.get("api") or {}
    rows = _api_miss_rows(d)
    out = ["", "### ②-b Claude 自報的失效成因（含部分失效）", ""]
    if not rows:
        out.append("這批資料沒有 API 自報成因（2026-05 前的 Claude Code 版本沒有 "
                   "`diagnostics.cache_miss_reason`，或期間內每次呼叫都完整命中）；上面的成因全為本工具推論。")
        return out
    n_cold = sum(c for _, c, _ in rows)
    n_part = sum(p for _, _, p in rows)
    tok = api.get("partial_tok", 0) + api.get("cold_tok", 0)
    usd = api.get("partial_usd", 0.0) + api.get("cold_usd", 0.0)
    out.append(f"Claude 明講成因的呼叫共 {n_cold + n_part} 次：冷啟 {n_cold}、"
               f"**部分失效 {n_part}**（命中率仍在門檻之上、但有一段被迫重算，⚡% 看不出來）；"
               f"有附「失效前綴長度」的合計 {fmt_tokens(tok)} tokens"
               + (f"、比直接命中多付 ~{cost_label(usd, api.get('usd_partial'))}"
                  if (usd or api.get("usd_partial")) else "")
               + "（失效前綴不等於重寫量；金額以各步實際寫入量為上限估算，算的是「實際被重寫的那部分 "
                 "token 比直接命中多付多少」，不等於「本來全都省得下來」）。"
                 "本節母體＝所有有自報成因的呼叫（含 ② 的「session 第一句」首步、及逐對統計略過的步），"
                 "故各成因「冷啟」數不必等於 ② 對應列。")
    out += ["", "| API 自報成因 | 冷啟 | 部分失效 | 意思 |", "|---|---:|---:|---|"]
    for r, c, p in rows:
        out.append(f"| {miss_label(r)} | {c} | {p} | {API_MISS_NOTES.get(r, '')} |")
    if api.get("excluded"):
        out += ["", f"另有 {api['excluded']} 對相鄰步因「前綴被改掉」排除在 TTL 存活與應命中帶統計外："
                    "這些步的命中率分母含被迫重寫的量、已被污染，整對移出風險集"
                    "（冷、熱都移；只挑冷的移會把存活率灌高）。"]
    return out


def _srv_excluded_note(d, html=True) -> str:
    """「伺服器不可用」那些相鄰步對被移出統計的揭露。

    兩種報告共用同一份文字（理由同 `_acct_scope_note`）。
    ⚠ **這一句不可省**：移出樣本會讓存活率與遵約率看起來比較好看，而移出的理由
    （伺服器故障不是快取沒撐住）只有寫出來讀者才判斷得了。同時要講明**逐步徽章仍會標**，
    否則讀者會以為那些冷啟被整個抹掉了。"""
    nsrv = (d.get("api") or {}).get("srv_excluded") or 0
    if not nsrv:
        return ""
    b = "<b>{}</b>" if html else "**{}**"
    return ("另有 " + b.format(f"{nsrv} 對") + "相鄰步因 API 自報「伺服器不可用」整對移出存活與"
            "帶內樣本（命中與未命中都移）——伺服器故障不是「快取沒撐住」，留著會污染 TTL 的量測。"
            + b.format("那些冷啟仍會出現在成因統計與逐步徽章上") +
            "：徽章會把同一步仍成立的條件一起標出來（例如「閒置過期＋伺服器不可用」），"
            "好讓人看得出就算伺服器沒掛、那一步的快取也還是會失效。")


def _band_api_note(d):
    """假說頁共用句：帶內「提早失效」樣本裡，API 各說了什麼。

    ⚠ 括號那句要講**兩類**都排除掉了：前綴變動類（`api_excluded`）與伺服器側不可用
    （`srv_excluded`）都在取樣時整對移出，所以 `band_api` 裡永遠不會出現這兩類的自報碼。
    只寫前綴變動類的話，讀者會以為 `unavailable` 還留在帶內樣本裡（它不在）。"""
    api = d.get("api") or {}
    band = api.get("band") or {}
    note = ""
    excl = []
    if api.get("band_excluded"):     # 只算「本來會落在這條帶內」的，別拿全母體數字唬人
        excl.append(f"另有 {api['band_excluded']} 對本來會落在帶內的相鄰步，因 API 自報「前綴被改掉」"
                    "──工具定義／系統提示／前文／換模型──整對移出樣本：命中與未命中都移，"
                    "只挑未命中移會把存活率灌高")
    if api.get("srv_excluded"):
        # (v36-fam3 F4) `unavailable` 也永遠進不了 band_api——只講前綴變動類的話，讀者會以為
        # 它還留在帶內樣本裡。⚠ 這裡刻意**不給對數**：`srv_excluded` 是全母體計數，不是
        # 「本來會落在帶內」的數，拿它當帶內數字就是用全母體唬人（正是上一行要避免的事）。
        excl.append("API 自報「伺服器不可用」的相鄰步同樣整對移出，所以下面的成因分佈裡"
                    "不會出現「快取暫時不可用」——那是伺服器的事，不是快取沒撐住")
    if excl:
        note = "（" + "；此外，".join(excl) + "）"
    if not band:                     # 帶內一個失效樣本都沒留下時，排除揭露更要講——遵約率看起來完美，
        if not note:                 # 正是因為失效樣本全被排除掉了
            return ""
        return (f"帶內沒有失效樣本{note}。" if (d.get("kpi") or {}).get("comply_n")
                else f"帶內樣本全數被移出{note}。")   # 連樣本都不剩＝「沒有失效」會誤導
    named = [(r, n) for r, n in sorted(band.items(), key=lambda kv: -kv[1]) if r != "-"]
    silent = band.get("-", 0)
    bits = "、".join(f"{miss_label(r)} {n} 次" for r, n in named)
    if not named:
        return f"這批帶內失效 API 都沒給成因{note}。"
    tail = ("「前文不在快取」正是「該在的前綴不見了」的形態，仍當失效候選；"
            if any(r == "previous_message_not_found" for r, _ in named) else "")
    return (f"帶內失效裡 API 有給成因的：{bits}；其餘 {silent} 次 API 沒說。"
            f"{tail}能明確歸給客戶端改動的都已排除{note}。")


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


def _kind_compare_data(d):
    """型態分層比較（review vs 一般）的表格資料：回傳 (欄標題 list, [(指標名, [各欄值 str]), …])。
    無分層（母體只有單一型態）回 None。HTML 與 MD 共用，確保兩邊數字一致。"""
    bk = d.get("by_kind")
    if not bk:
        return None
    cols = [("全部", d)] + [(KIND_LABELS.get(kk, kk), sub) for kk, sub in bk.items()]

    def _hit(x):
        return _pct(x["kpi"]["hit"]) if x["kpi"]["tok_in"] else "—"

    def _comply(x):
        n, h = x["kpi"]["comply_n"], x["kpi"]["comply_hit"]
        return f"{_pct(h / n)}（n={n}）" if n else "—"

    metrics = [
        ("session 數", lambda x: str(x["n_sessions"])),
        ("可分析步樣本", lambda x: str(x["n_pairs"])),
        ("整體命中率", _hit),
        ("TTL 遵約率", _comply),
        ("冷啟總數", lambda x: str(sum(x["causes_total"].values()))),
        ("　├ 提早失效（evict）", lambda x: str(x["causes_total"]["evict"])),
        ("　└ 閒置過期（expiry）", lambda x: str(x["causes_total"]["expiry"])),
    ]
    headers = [name for name, _ in cols]
    rows = [(label, [fn(sub) for _, sub in cols]) for label, fn in metrics]
    return headers, rows


def _acct_scope_note(d, html=True) -> str:
    """切帳號偵測的範圍與實際涵蓋率。

    兩種報告共用同一份文字：分成兩份寫，遲早只有一邊會被改到，那時就有一種輸出在隱瞞。
    ⚠ 一律輸出、不看次數是不是 0——0 有三種意思（真的沒切過／這台只有一個帳號／
    history 讀不到或列格式變了），涵蓋率就是分辨它們的那份資料。
    ⚠ (v36-fam3 F7) **兩個方向都要講。** 原本只揭露漏報（「顯示 0 不代表沒發生過」），
    讀者會以為這個數字只會少不會多；而誤報那一格是**會記到錢**的（多算一次人因浪費）。
    範圍限制本身見 planning/scope-limits.md 的 SCOPE-CFG-PATH-IDENTITY。"""
    b = "<b>{}</b>" if html else "**{}**"
    note = ("「自行切帳號」偵測自各帳號的 history.jsonl，"
            + b.format("只在多帳號的本機資料上成立")
            + "（跨機同步來的紀錄看不到另一台的切換）——"
            + b.format("顯示 0 不代表沒發生過") + "。反過來，"
            + b.format("同一個登入被用在兩個 config 目錄時會多算一次")
            + "（連同它的成本）：history 一列只有 sessionId 與時刻、沒有帳號身分欄位，"
              "那種佈局在資料上與真的換帳號完全同形。兩份內容仍相同的情形已經擋掉"
              "（同一時刻同時出現在兩個 config 的 sessionId 整組不歸屬），"
              "擋不掉的是「兩份曾經相同、之後各自被用過」。")
    # (v36-fam5 #1) 第三個誤報方向，同樣是**會記到錢**的那一種：「被迫/自願」只看同一對相鄰步
    # 之間的事件，429 與切換中間夾了任何一次 API 呼叫，那次被迫切換就落成「自行切帳號」。
    n_le = ((d.get("api") or {}).get("acct_limit_earlier") or 0)
    if n_le:
        note += ("另外，" + b.format(f"其中 {n_le} 次的同一場對話稍早出現過 429/401")
                 + "——「被迫/自願」只看同一對相鄰步之間的事件，中間夾了任何一次呼叫就分不出來，"
                 "那幾次可能是被迫的卻仍被算進人因浪費（逐步徽章會把兩個條件並列標出）。")
    h = d.get("acct_health") or {}
    if not h.get("dirs"):
        return note + "本次建置沒有量到偵測涵蓋率。"
    return note + (f"本次掃了 {h['dirs']} 個 config 目錄、讀到 {h.get('files', 0)} 份 history 共 "
                   f"{h.get('rows', 0)} 列（認不得 {h.get('bad_rows', 0)} 列"
                   + (f"、讀取失敗 {h['read_errors']} 個" if h.get("read_errors") else "")
                   + f"），辨識出 {h.get('accounts', 0)} 個帳號。")


def _masked_human_note(d, html=True) -> str:
    """實據成因勝出、但同一步仍成立人因條件的次數。

    兩種報告共用同一份文字（理由同 `_acct_scope_note`）。
    ⚠ 這些步的成因欄只記實據那一個，所以上面的人因次數**不含**它們。不揭露的話，
    「那一步其實也換了帳號」就只活在逐步徽章裡，報告這一層完全看不見。"""
    m = d.get("masked_human") or {}
    by_cause = m.get("causes") or {}
    if not m.get("steps") or not by_cause:
        return ""
    b = "<b>{}</b>" if html else "**{}**"
    items = "、".join(f"{_COLD_CAUSE_LABEL.get(c, c)} {v} 次" for c, v in sorted(by_cause.items()))
    return ("另有 " + b.format(f"{m['steps']} 次") + "冷啟由同一步的直接證據（API 自報或結構性邊界）"
            "定成因，但同一步仍成立"
            "人因條件（" + items + "）。成因欄只記實據那一個，"
            + b.format("上面的人因次數不含它們") + "；逐步徽章會兩個都標。")


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
        bx = (d.get("api") or {}).get("band_excluded") or 0
        tiles.append(("TTL 遵約率", _pct(p),
                      f"1h 快取、閒置 2–55 分內回來仍命中；CI {_pct(lo)}–{_pct(hi)}，n={k['comply_n']}"
                      # headline 數字自己也要帶這個限定——它因排除而整整跳了 1.75pp
                      + (f"（另有 {bx} 對因前綴變動移出）" if bx else "")))
        tiles.append(("提早失效", f"{k['comply_n'] - k['comply_hit']} 次",
                      f"應命中帶內卻冷啟＝{_pct(1 - p)}（1h TTL 下理論上應為 0）"))
    ct0 = d["causes_total"]
    tiles.append(("人因冷啟", f"{k['avoid_n']} 次",
                  f"閒置過期 {ct0['expiry']}＋自行切帳號 {ct0['acct']}；約 {k['avoid_week']:.1f} 次/週"))
    # (v36-fam4 #1) 金額一律走 `cost_label`，與索引 td／tooltip／MD 同一個口徑——副標題就寫著
    # 「與索引同口徑」，而 `fmt_money(0)+"+?"` 給的 `$0+?` 會被讀成「沒多花錢」（索引那側給 `?`）。
    tiles.append(("人因浪費", cost_label(k["avoid_usd"], k["avoid_partial"]),
                  "重暖的估算額外成本（重寫 −0.1× 讀取）；與索引「浪費」欄同口徑"))
    parts.append('<div class="kpis">' + "".join(
        f'<div class="kpi"><div class="kv2">{esc(v)}</div><div class="kl">{esc(t)}</div>'
        f'<div class="ks">{esc(s)}</div></div>' for t, v, s in tiles) + "</div>")

    # ── 型態分層：review vs 一般 ──
    cmp_data = _kind_compare_data(d)
    if cmp_data:
        headers, crows = cmp_data
        parts.append("<h2>型態分層：review vs 一般</h2>")
        parts.append('<div class="lead">同一套判定分別跑在兩個子母體上，看快取行為是否因型態而異'
                     '（review＝首句是 reviewer 角色/review prompt 的 cross-model review；一般＝其餘互動；'
                     'exec 是 Codex 專屬、不在此頁）。「全部」為兩者合計。</div>')
        th = "".join(f'<th class="num">{esc(h)}</th>' for h in headers)
        tb = "".join("<tr><td>" + esc(lbl) + "</td>"
                     + "".join(f'<td class="num">{esc(v)}</td>' for v in vals) + "</tr>"
                     for lbl, vals in crows)
        parts.append(f'<table class="rep"><thead><tr><th>指標</th>{th}</tr></thead><tbody>{tb}</tbody></table>')

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
        parts.append('<div class="lead">結構性冷啟（session 第一句／切帳號／換模型／壓縮後，以及 API 自報的'
                     '前綴變動——工具定義／系統提示／前文，以及 API 自報「伺服器不可用」）'
                     '已從曲線樣本排除'
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
    parts.append('<div class="lead">成因有兩種來源：<b>API 自報</b>（Claude 在 '
                 "<code>diagnostics.cache_miss_reason</code> 直接寫明為什麼沒命中——實據）與"
                 "<b>本工具推論</b>（沒有自報時，依邊界事件與間隔判定）。有自報就以自報為準。</div>")
    total_cold = sum(d["causes_total"].values())
    # (v36-fam5 #6) 三段揭露**在 `if total_cold:` 之外算好**：它們講的是「偵測範圍／被實據遮蔽
    # 的人因／被移出樣本的對數」，三件事在零冷啟時照樣成立且照樣有話要說——實測本機有 289 個
    # 「有 cache_steps、零冷啟成因」的 session，只用它們建報告時 `_srv_excluded_note` 仍有
    # 75 對要揭露，而舊寫法一句都不出。三個函式各自守門（沒東西講就回空字串），所以無條件串接
    # 不會吐出空段落。
    scope_notes_html = (_acct_scope_note(d, html=True) + _masked_human_note(d, html=True)
                        + _srv_excluded_note(d, html=True))
    if total_cold:
        ct = d["causes_total"]
        avoid = sum(ct[c] for c in _AVOIDABLE_CAUSES)
        api_keys = _API_PREFIX_CAUSES
        api_n = sum(ct[k] for k in api_keys)
        unavoid = sum(ct[c] for c in _UNAVOIDABLE_CAUSES)
        unk_n = sum(ct[c] for c in _UNKNOWN_CAUSES)
        ins2 = (f"共 <b>{total_cold}</b> 次冷啟：不可避免 <b>{unavoid}</b>"
                "（第一句/被迫切帳號/換模型/壓縮/伺服器不可用）、"
                + (f"前綴變動（習慣可避免）<b>{api_n}</b>、" if api_n else "")
                + (f"未知的自報成因 <b>{unk_n}</b>、" if unk_n else "")
                + f"可避免/異常 <b>{avoid}</b>（閒置過期 {ct['expiry']}"
                + (f"＋自行切帳號 {ct['acct']}" if ct["acct"] else "")
                + f"＋提早失效 {ct['evict']}）、"
                f"回合內雜訊 {ct['intra']}。")
        if ct["acct"]:
            ins2 += "「自行切帳號」是沒撞 limit 就換帳號——快取按組織／workspace 隔離，等於自己把前綴丟掉。"
        # 偵測範圍**一律揭露，不看數字是不是 0**：0 有三種完全不同的意思（真的沒切過／這台
        # 只有一個帳號／history 讀不到或列格式變了）。只在 >0 時才附註，等於讓最需要保留懷疑
        # 的那個數字裸奔——讀者會把「沒偵測到」讀成「沒發生」。
        # ⚠ 這句話成立的前提是它**兩個分支都出**（見上面 `scope_notes_html`）：
        #   (v36-fam5 #6) 之前它只長在這一支裡，於是「一律揭露」在零冷啟那一支是假的。
        ins2 += scope_notes_html
        if api_n:
            ins2 += (f"「前綴變動」是 API 自報的（工具定義 {ct['tools']}／系統提示 {ct['system']}／"
                     f"前文 {ct['msgs']}）——不是快取沒撐住，是這次請求的前綴跟上次不一樣了、"
                     "多為中途載工具或改 CLAUDE.md 造成（改掉習慣就能省），已排除在 TTL 統計外。")
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
        parts.append('<div class="insight">期間內沒有冷啟。' + scope_notes_html + "</div>")
    parts.append(_api_miss_html(d))

    # ── ③ 重暖作息 ──
    parts.append("<h2>③ 你都什麼時段重新暖機（冷啟作息）</h2>")
    n_res = d["n_resumes"]
    parts.append(f'<div class="lead">各帳號活動軸上「閒置 ≥ {fmt_dur(REPORT_BREAK_SEC)} 後、確實冷啟的第一步」'
                 f"＝一次重新開工，共 <b>{n_res}</b> 次（閒置夠久卻仍命中 1h 快取者不算）。"
                 "bar 分兩段：<b>實色＝可避免/異常</b>（<b>人因</b>：閒置過期——早點回來就能省、"
                 "自行切帳號——沒撞 limit 就別換；<b>加上伺服器側異常</b>：提早失效＝1h TTL 內卻冷啟，"
                 "<b>那一種不是你能控制的</b>，所以索引的「浪費」欄與②的人因 KPI 都不計它）、"
                 "淡色＝不是閒置造成的（session 第一句/被迫切帳號/換模型/壓縮/伺服器不可用，以及 API 自報的前綴變動——"
                 "那類要改習慣才省得到，不是「早點回來」能解決的）。已正規化為「次/活躍日」，平日假日可直接比。</div>")
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
        bx = (d.get("api") or {}).get("band_excluded") or 0
        out.append(f"- TTL 遵約率（1h 快取、閒置 2–55 分仍命中）：**{_pct(p)}**"
                   f"（CI {_pct(lo)}–{_pct(hi)}，n={k['comply_n']}"
                   + (f"；另有 {bx} 對因前綴變動移出" if bx else "")
                   + f"）；提早失效 {k['comply_n'] - k['comply_hit']} 次")
    out.append(f"- 人因冷啟（閒置過期 {d['causes_total']['expiry']}＋自行切帳號 "
               f"{d['causes_total']['acct']}）：**{k['avoid_n']} 次**（約 {k['avoid_week']:.1f} 次/週）"
               f"；估算人因浪費 **{cost_label(k['avoid_usd'], k['avoid_partial'])}**")   # 同 HTML：走 cost_label

    cmp_data = _kind_compare_data(d)
    if cmp_data:
        headers, crows = cmp_data
        out += ["", "## 型態分層：review vs 一般", "",
                "同一套判定分別跑在兩個子母體上（review＝首句是 reviewer 角色/review prompt；一般＝其餘；exec 是 Codex 專屬、不在此）。",
                "", "| 指標 | " + " | ".join(headers) + " |",
                "|---|" + "---:|" * len(headers)]
        for lbl, vals in crows:
            out.append(f"| {lbl} | " + " | ".join(vals) + " |")

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
        out.append("> 結構性冷啟（第一句/切帳號/換模型/壓縮，以及 API 自報的前綴變動：工具定義/系統提示/前文）"
                   "已排除；「< 1 分」桶多為回合內快取斷點雜訊。"
                   "TTL 每次使用會刷新，間隔＝距上一次使用。")
    if d["top_warm"]:
        out += ["", "異常存活（>1 小時仍命中；理論上不該發生，供對照）："]
        for g, ts in d["top_warm"]:
            out.append(f"- {fmt_dur(g)} 後仍命中 · {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}")

    out += ["", "## ② 冷啟成因分解", "",
            "成因兩種來源：**API 自報**（Claude 在 `diagnostics.cache_miss_reason` 直接寫明——實據）與"
            "**本工具推論**（沒有自報時依邊界事件與間隔判定）；有自報就以自報為準。"]
    total_cold = sum(d["causes_total"].values())
    # (v36-fam5 #6) 同 HTML：三段揭露在 `if` 之外算好，兩個分支都要出。
    scope_notes_md = (_acct_scope_note(d, html=False) + _masked_human_note(d, html=False)
                      + _srv_excluded_note(d, html=False))
    if total_cold:
        ct = d["causes_total"]
        api_n = sum(ct[c] for c in _API_PREFIX_CAUSES)
        unavoid = sum(ct[c] for c in _UNAVOIDABLE_CAUSES)
        unk_n = sum(ct[c] for c in _UNKNOWN_CAUSES)
        out.append("")
        out.append(f"共 {total_cold} 次冷啟；不可避免 {unavoid}"
                   "（第一句/被迫切帳號/換模型/壓縮/伺服器不可用）、"
                   + (f"前綴變動（習慣可避免）{api_n}、" if api_n else "")
                   + (f"未知的自報成因 {unk_n}、" if unk_n else "")
                   + f"可避免/異常 {sum(ct[c] for c in _AVOIDABLE_CAUSES)}"
                   + (f"（含自行切帳號 {ct['acct']}）" if ct["acct"] else "")
                   + f"、回合內雜訊 {ct['intra']}。")
        # 與 HTML 共用同一份揭露（_acct_scope_note）：兩種報告口徑必須一致。
        out.append(scope_notes_md)
        if api_n:
            out.append(f"「前綴變動」是 API 自報的（工具定義 {ct['tools']}／系統提示 {ct['system']}／"
                       f"前文 {ct['msgs']}）——不是快取沒撐住，多為中途載工具或改 CLAUDE.md 造成"
                       "（改掉習慣就能省），已排除在 TTL 統計外。")
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
        out.append(scope_notes_md)
    out += _api_miss_md(d)

    out += ["", "## ③ 重暖作息（冷啟時段）", "",
            f"閒置 ≥ {fmt_dur(REPORT_BREAK_SEC)} 後、確實冷啟的第一步＝重新開工，共 {d['n_resumes']} 次"
            "（可避免/異常＝人因的閒置過期／自行切帳號，加上伺服器側的提早失效——最後那種不是你能"
            "控制的，索引「浪費」欄與②的人因 KPI 都不計它；非閒置造成＝第一句/被迫切帳號/換模型/壓縮，"
            "以及 API 自報的前綴變動——工具定義/系統提示/前文，那類要改習慣才省得到，"
            "不是「早點回來」能解決的）。"]
    wd_tot = [c["a"] + c["u"] for c in d["clock_wd"]]
    we_tot = [c["a"] + c["u"] for c in d["clock_we"]]
    if sum(wd_tot):
        out.append(f"- 平日 {sum(wd_tot)} 次／{d['wd_days']} 個活躍日，集中 {_hours_label(_top_hours(wd_tot))}")
    if sum(we_tot):
        out.append(f"- 假日 {sum(we_tot)} 次／{d['we_days']} 個活躍日，集中 {_hours_label(_top_hours(we_tot))}")
    out += ["", "| 時 | 平日可避免/異常 | 平日非閒置 | 假日可避免/異常 | 假日非閒置 |",
            "|---|---:|---:|---:|---:|"]
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
        '（結構性冷啟已排除、比率附 Wilson 95% CI），拿自己的 JSONL 跑 viewer 就會得到同一組檢定，可直接對照。'
        '2026-05 起的紀錄 Claude 會<b>自報</b>未命中成因，能歸給「前綴被改掉」（工具定義／系統提示／前文／'
        '換模型）的已從樣本剔除——這頁追的是<b>剩下不能歸給客戶端改動</b>的那些'
        '（其中不少 API 仍給了成因，如「前文不在快取」——那正是快取真的不在了的形態，故仍當失效候選）。</div>')
    parts.append(f'<div class="lead">涵蓋範圍：{esc(" · ".join(_cover_items(d)))}</div>')
    if k["comply_n"]:
        ev = k["comply_n"] - k["comply_hit"]
        parts.append(f'<div class="insight">共用樣本：應命中帶相鄰步 <b>n={k["comply_n"]}</b>，'
                     f'其中提早失效 <b>{ev}</b> 次（{_ci_str(ev, k["comply_n"])}）。</div>')
    elif (d.get("api") or {}).get("band_excluded"):
        # 樣本不是不存在，是被排除掉了——不可講成「資料不足、再累積就有」。但排除理由不只自報：
        # 邊界（切帳號/換模型/壓縮）排掉的帶內對**不**計進 band_excluded，不可一併算在 API 頭上。
        parts.append('<div class="insight">應命中帶目前沒有可檢定樣本——<b>不是</b>資料不足，'
                     '而是原本會落在帶內的相鄰步都已排除（其中 '
                     f'<b>{d["api"]["band_excluded"]}</b> 對因 API 自報「前綴被改掉」，'
                     '其餘為切帳號／換模型／壓縮邊界）。</div>')
    else:
        parts.append('<div class="insight">尚無應命中帶樣本（需要 1h 快取寫入、閒置 2–55 分的相鄰步）；'
                     '累積資料後這頁才有東西可檢定。</div>')
    note = _band_api_note(d)         # 移出 guard：帶內樣本被排除到 0 時，這句揭露才最需要講
    if note:
        parts.append(f'<div class="lead">{esc(note)}</div>')

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
           "拿自己的 JSONL 跑 viewer 就會得到同一組檢定，可直接對照。"
           "2026-05 起的紀錄 Claude 會自報未命中成因，能歸給「前綴被改掉」（工具定義／系統提示／前文／換模型）"
           "的已從樣本剔除——這頁追的是剩下不能歸給客戶端改動的那些"
           "（其中不少 API 仍給了成因，如「前文不在快取」——那正是快取真的不在了的形態，仍當失效候選）。", ""]
    out.append("涵蓋範圍：" + " · ".join(_cover_items(d)))
    if k["comply_n"]:
        ev = k["comply_n"] - k["comply_hit"]
        out += ["", f"共用樣本：應命中帶相鄰步 n={k['comply_n']}，其中提早失效 {ev} 次"
                    f"（{_ci_str(ev, k['comply_n'])}）。"]
    elif (d.get("api") or {}).get("band_excluded"):
        out += ["", "應命中帶目前沒有可檢定樣本——**不是**資料不足，而是原本會落在帶內的相鄰步都已排除"
                    f"（其中 {d['api']['band_excluded']} 對因 API 自報「前綴被改掉」，"
                    "其餘為切帳號／換模型／壓縮邊界）。"]
    else:
        out += ["", "尚無應命中帶樣本（需要 1h 快取寫入、閒置 2–55 分的相鄰步）。"]
    note = _band_api_note(d)         # 移出 guard（同 HTML）：樣本被排除到 0 時最需要這句
    if note:
        out += ["", note]

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
# Codex 快取存活統計（cache-codex.html / cache-codex.md）
# =========================================================================
_CODEX_KINDS = ("review", "exec", "chat")
_CODEX_KIND_CSS = {"review": "co-1h", "exec": "co-5m", "chat": "co-unk"}   # 沿用曲線三色


def build_codex_survival(rows):
    """Codex 快取存活統計：同一 session 內相鄰呼叫的「閒置間隔 → 是否冷啟」，依型態分層。
    方法與 Claude 健檢頁刻意不同：OpenAI 自動快取沒有寫入 TTL 標記，無法分 TTL cohort、
    也沒有成因分解可做；這裡只做通用存活觀察＋型態分層對照。
    「命中」＝該步 cache_read/脈絡 ≥ CACHE_COLD_PCT%（與徽章同門檻）；樣本＝前後步脈絡皆
    ≥ REPORT_MIN_CTX 的相鄰呼叫。OpenAI 為前綴部分命中且快取跨 session（組織內）共享，
    長閒置後的「命中」可能只是共享前綴殘餘或其他 session 刷新——頁面明示此限制。
    回傳 dict（has_data=False 代表沒資料）。"""
    bins = {k: [[0, 0] for _ in REPORT_GAP_BUCKETS] for k in _CODEX_KINDS}   # [n, hit]
    bins_all = [[0, 0] for _ in REPORT_GAP_BUCKETS]
    med_all = [[] for _ in REPORT_GAP_BUCKETS]      # 各桶命中率%清單 → 中位數（部分命中的誠實呈現）
    kind_counts = {k: 0 for k in _CODEX_KINDS}
    n_sessions = n_calls = 0
    tok_in = tok_read = 0
    first_ts = last_ts = None
    warm_max = 0
    short_n = short_cold = 0                        # gap < REPORT_INTRA_SEC 的樣本：斷點雜訊率
    top_warm = []                                   # (gap, epoch)：閒置 ≥ 1 小時仍 ≥ 門檻（觀察用）
    for r in rows:
        if r.get("source_kind") != SOURCE_CODEX:
            continue
        steps = r.get("codex_steps") or []
        if not steps:
            continue
        kind = r.get("kind") or "chat"
        if kind not in bins:
            kind = "chat"
        n_sessions += 1
        kind_counts[kind] += 1
        n_calls += len(steps)
        prev = None
        for st in steps:
            tok_in += st[2]
            tok_read += st[1]
            first_ts = st[0] if first_ts is None else min(first_ts, st[0])
            last_ts = st[0] if last_ts is None else max(last_ts, st[0])
            if prev is not None and st[2] >= REPORT_MIN_CTX and prev[2] >= REPORT_MIN_CTX:
                gap = st[0] - prev[0]
                if gap >= 0:
                    b = _gap_bucket_index(gap)
                    cold = _step_cold(st)
                    bins[kind][b][0] += 1
                    bins_all[b][0] += 1
                    if not cold:
                        bins[kind][b][1] += 1
                        bins_all[b][1] += 1
                    med_all[b].append(round(100 * st[1] / st[2]))
                    if gap < REPORT_INTRA_SEC:
                        short_n += 1
                        short_cold += 1 if cold else 0
                    if not cold:
                        warm_max = max(warm_max, gap)
                        if gap >= 3600:
                            top_warm.append((gap, st[0]))
            prev = st
    top_warm.sort(reverse=True)
    span = ""
    if first_ts and last_ts:
        span = (datetime.fromtimestamp(first_ts).strftime("%Y-%m-%d") + " – "
                + datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d"))
    n_pairs = sum(n for n, _ in bins_all)
    return {
        "has_data": n_pairs > 0,
        "n_sessions": n_sessions, "n_calls": n_calls, "n_pairs": n_pairs,
        "kind_counts": kind_counts, "span": span,
        "tok_in": tok_in, "tok_read": tok_read,
        "bins": bins, "bins_all": bins_all,
        "med_all": [sorted(x) for x in med_all],
        "short_n": short_n, "short_cold": short_cold,
        "warm_max": warm_max, "top_warm": top_warm[:8],
        "claude_report": False,      # main() 依當次建置是否有 Claude 報告覆寫（互連連結用）
    }


def _codex_cover(d):
    kc = d["kind_counts"]
    kinds_txt = "、".join(f"{KIND_LABELS[k]} {kc[k]}" for k in _CODEX_KINDS if kc[k])
    cover = [f"{d['n_sessions']} 個 Codex session（{kinds_txt}）",
             f"{d['n_calls']} 次呼叫", f"{d['n_pairs']} 個相鄰呼叫樣本"]
    if d["span"]:
        cover.append(d["span"])
    return cover


def _codex_survival_cohorts(d):
    return [{"label": KIND_LABELS[k], "css": _CODEX_KIND_CSS[k],
             "bins": [(n, hit) for n, hit in d["bins"][k]]}
            for k in _CODEX_KINDS if any(n for n, _ in d["bins"][k])]


def render_codex_survival_html(d) -> str:
    parts = ['<div class="report">']
    back2 = ('<span><a class="back" href="cache-report.html">⚡ Claude 快取分析 →</a></span>'
             if d.get("claude_report") else "")
    parts.append(f'<div class="topbar"><span><a class="back" href="index.html">← 回索引</a></span>{back2}</div>')
    parts.append("<h1>📈 Codex 快取存活統計</h1>")
    parts.append(
        '<div class="lead">同一 session 內相鄰兩次 API 呼叫，看「閒置多久之後回來，快取還在不在」。'
        f'「命中」＝該步 cache_read ÷ 脈絡 ≥ {CACHE_COLD_PCT}%（與各頁徽章同門檻）；'
        f'樣本＝前後步脈絡皆 ≥ {REPORT_MIN_CTX} tokens 的相鄰呼叫；依 session 型態'
        '（review／exec／一般）分層——三個母體的使用節奏不同，混看會失真。'
        '所有比率附 Wilson 95% CI。</div>')
    claude_ref = ('<a href="cache-report.html">Claude 健檢頁</a>' if d.get("claude_report")
                  else "Claude 健檢頁")     # 無 Claude 報告時不出連結（避免斷鏈）
    parts.append(
        f'<div class="lead">⚠ 方法限制（與 {claude_ref}的差異）：'
        'OpenAI 是<b>自動快取</b>，usage 沒有寫入量與 TTL 標記，無法像 Claude 那樣分 TTL cohort、'
        '做成因分解或遵約率；且快取是<b>前綴部分命中、組織內跨 session 共享</b>——長閒置後仍「命中」'
        '可能只是共享前綴殘餘或其他 session 恰好刷新，未必是本 session 的快取存活。'
        '本頁是<b>觀察</b>，不是 TTL 量測。</div>')
    parts.append(f'<div class="lead">涵蓋範圍：{esc(" · ".join(_codex_cover(d)))}</div>')

    hit_frac = d["tok_read"] / d["tok_in"] if d["tok_in"] else 0.0
    tiles = [("整體命中率", _pct(hit_frac) if d["tok_in"] else "—", "token 加權：讀取 ÷ 全部脈絡"),
             ("相鄰呼叫樣本", str(d["n_pairs"]), f"脈絡 ≥ {REPORT_MIN_CTX} tokens 的前後步")]
    if d["short_n"]:
        p, lo, hi = _wilson(d["short_cold"], d["short_n"])
        tiles.append((f"短間隔（<{fmt_dur(REPORT_INTRA_SEC)}）冷啟", f"{d['short_cold']}/{d['short_n']}",
                      f"回合內斷點雜訊，非閒置造成；比率 {_pct(p)}（CI {_pct(lo)}–{_pct(hi)}）"))
    if d["warm_max"]:
        tiles.append(("最長閒置仍命中", fmt_dur(d["warm_max"]),
                      "含共享前綴殘餘效應，見上方方法限制"))
    parts.append('<div class="kpis">' + "".join(
        f'<div class="kpi"><div class="kv2">{esc(v)}</div><div class="kl">{esc(t)}</div>'
        f'<div class="ks">{esc(s)}</div></div>' for t, v, s in tiles) + "</div>")

    parts.append("<h2>存活曲線（依 session 型態分層）</h2>")
    cohorts = _codex_survival_cohorts(d)
    if cohorts:
        parts.append(_svg_survival(cohorts))
        if len(cohorts) > 1:
            parts.append('<div class="legend">' + "".join(
                f'<span><span class="sw {c["css"]}"></span>{esc(c["label"])}</span>' for c in cohorts) + "</div>")
        parts.append('<div class="lead">「&lt; 1 分」桶的 miss 是回合內快取斷點雜訊（與閒置無關）；'
                     '長間隔桶的「命中」含部分命中（≥ 門檻即算）——對照下表「中位命中%」看衰減幅度。</div>')
        head = "".join(f'<th colspan="2">{esc(c["label"])}</th>' for c in cohorts)
        sub = ('<th class="num">樣本</th><th>命中率（CI）</th><th class="num">中位命中%</th>'
               + "".join('<th class="num">樣本</th><th>命中率（CI）</th>' for _ in cohorts))
        body_rows = []
        for i, (_, lbl) in enumerate(REPORT_GAP_BUCKETS):
            n_all, hit_all = d["bins_all"][i]
            if not n_all:
                continue
            meds = d["med_all"][i]
            med = f"{meds[len(meds) // 2]}%" if meds else "—"
            cells = (f'<td class="num">{n_all}</td><td>{_ci_str(hit_all, n_all)}</td>'
                     f'<td class="num">{med}</td>')
            for c in cohorts:
                n, hit = c["bins"][i]
                cells += f'<td class="num">{n or "—"}</td><td>{_ci_str(hit, n) if n else "—"}</td>'
            body_rows.append(f"<tr><td>{esc(lbl)}</td>{cells}</tr>")
        parts.append(f'<table class="rep"><thead><tr><th rowspan="2">閒置間隔</th><th colspan="3">全部</th>{head}</tr>'
                     f"<tr>{sub}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>")
    if d["top_warm"]:
        items = "".join(
            f'<div class="kv"><b>{fmt_dur(g)}</b> 後仍 ≥ {CACHE_COLD_PCT}%　'
            f'<span class="hn">{esc(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"))}</span></div>'
            for g, ts in d["top_warm"])
        parts.append(f"<h3>長閒置仍命中（≥ 1 小時，觀察）</h3>"
                     '<div class="lead">OpenAI 快取離峰保留可達 1 小時，且共享前綴可能被其他 session 刷新'
                     "——這些不是異常，列出供對照。</div>" + items)
    parts.append(f'<div class="lead">※ 全為觀察值：命中率取自各次呼叫 usage；冷啟門檻＝命中率 &lt; {CACHE_COLD_PCT}%；'
                 '時間為本機時區。Claude 的 TTL 量測（cohort／成因／遵約率）在'
                 ' <a href="cache-report.html">快取分析報告</a>。</div>' if d.get("claude_report") else
                 f'<div class="lead">※ 全為觀察值：命中率取自各次呼叫 usage；冷啟門檻＝命中率 &lt; {CACHE_COLD_PCT}%；'
                 '時間為本機時區。</div>')
    parts.append("</div>")
    return html_page("Codex 快取存活統計", "".join(parts), "report")


def render_codex_survival_md(d) -> str:
    out = ["# 📈 Codex 快取存活統計", "",
           f"同一 session 內相鄰呼叫的「閒置間隔 → 命中率」觀察，依型態分層。命中門檻 {CACHE_COLD_PCT}%。",
           "⚠ OpenAI 自動快取無 TTL 標記，且前綴部分命中、組織內跨 session 共享——本頁是觀察，非 TTL 量測。", "",
           "涵蓋範圍：" + " · ".join(_codex_cover(d)), ""]
    hit_frac = d["tok_read"] / d["tok_in"] if d["tok_in"] else 0.0
    out.append(f"- 整體命中率（token 加權）：**{_pct(hit_frac)}**")
    out.append(f"- 相鄰呼叫樣本：**{d['n_pairs']}**（脈絡 ≥ {REPORT_MIN_CTX} tokens）")
    if d["short_n"]:
        out.append(f"- 短間隔（<{fmt_dur(REPORT_INTRA_SEC)}）斷點冷啟：**{d['short_cold']}/{d['short_n']}**（{_ci_str(d['short_cold'], d['short_n'])}）")
    if d["warm_max"]:
        out.append(f"- 最長閒置仍命中：**{fmt_dur(d['warm_max'])}**（含共享前綴殘餘效應）")
    cohorts = _codex_survival_cohorts(d)
    if cohorts:
        head = " | ".join(f"{c['label']}：樣本 | 命中率（CI）" for c in cohorts)
        out += ["", f"| 閒置間隔 | 全部：樣本 | 命中率（CI） | 中位命中% | {head} |",
                "|---" * (4 + 2 * len(cohorts)) + "|"]
        for i, (_, lbl) in enumerate(REPORT_GAP_BUCKETS):
            n_all, hit_all = d["bins_all"][i]
            if not n_all:
                continue
            meds = d["med_all"][i]
            med = f"{meds[len(meds) // 2]}%" if meds else "—"
            cells = " | ".join(f"{n or '—'} | {_ci_str(hit, n) if n else '—'}"
                               for n, hit in (c["bins"][i] for c in cohorts))
            out.append(f"| {lbl} | {n_all} | {_ci_str(hit_all, n_all)} | {med} | {cells} |")
    if d["top_warm"]:
        out += ["", "長閒置仍命中（≥ 1 小時；OpenAI 離峰保留可達 1 小時＋共享前綴刷新，非異常）："]
        for g, ts in d["top_warm"]:
            out.append(f"- {fmt_dur(g)} 後仍 ≥ {CACHE_COLD_PCT}% · {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}")
    out += ["", f"※ 全為觀察值；冷啟門檻＝命中率 < {CACHE_COLD_PCT}%；時間為本機時區。"
                "Claude 的 TTL 量測另見 [快取分析報告](cache-report.md)。" if d.get("claude_report") else
                f"※ 全為觀察值；冷啟門檻＝命中率 < {CACHE_COLD_PCT}%；時間為本機時區。"]
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
.smeta .warn{color:#d29922}
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
/* flex-wrap 是必要的：.meter 各自 white-space:nowrap、彼此間又沒有空白可斷行，勾滿補充徽章時
   單行塞不下就會把靠 margin-left:auto 推到右邊的時間戳擠出畫面。換行後時間跟著落到最後一行右端。 */
.turn .head{display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:7px 12px;
border-bottom:1px solid var(--border);font-size:13px;background:rgba(127,127,127,.06)}
.turn .who{font-weight:600}.turn .when{margin-left:auto;color:var(--muted);font-size:12px;cursor:help;text-decoration:underline dotted transparent}
.turn .when:hover{text-decoration-color:var(--muted)}
.badge{background:var(--border);border-radius:10px;padding:1px 8px;font-size:11px;color:var(--muted)}
.meter{font-size:11px;color:var(--muted);background:rgba(127,127,127,.12);border-radius:8px;padding:1px 6px;margin-left:6px;white-space:nowrap}
.meter.m-cache{color:var(--assistant)}.meter.m-cost{color:#d29922}
.meter.m-cr{color:var(--assistant)}.meter.m-cw{color:#d29922}
.meter.cold{background:var(--err);color:#fff;font-weight:600}
.meter.coldx{background:rgba(127,127,127,.22);color:var(--muted);font-weight:600;text-decoration:underline dotted}
.meter.m-miss{background:rgba(210,153,34,.18);color:#d29922;font-weight:600}
.meter.m-miss.part{background:rgba(127,127,127,.14);color:var(--muted);font-weight:500}
.meter.m-gap,.meter.m-dur,.meter.m-eff{color:var(--muted)}
body.hide-cache .m-cache,body.hide-miss .m-miss,body.hide-cost .m-cost,body.hide-in .m-in,body.hide-cw .m-cw,body.hide-cr .m-cr,body.hide-ctx .m-ctx,body.hide-out .m-out,body.hide-gap .m-gap,body.hide-dur .m-dur,body.hide-eff .m-eff{display:none}
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
/* 指令列 / 背景任務通知：使用者確實送出、或系統確實注入，但都**不是發言**
   ⇒ 不套對話框、不給書籤鈕，只給一列窄的橫列（見 render_turn_html 的 `_cmd_only` 分支）*/
/* ⚠ 這一列**沿用 `.turn`**：`openSub()` 靠 `.turn` 找跳轉目標並加 `.hl` 外框，
   `#`／`☆` 的顯形也綁在 `.turn:hover`。所以是把 `.turn` 的框與底色**改掉**，不是不用它。 */
.turn.metarow{display:flex;align-items:flex-start;gap:8px;margin:6px 0;padding:2px 0;
font-size:13px;border:none;border-radius:0;background:none;overflow:visible}
.turn.metarow>.when{margin-left:0;color:var(--muted);font-size:11.5px;
font-family:ui-monospace,Consolas,monospace;padding-top:3px;white-space:nowrap}
.metabody{flex:1;min-width:0}
.cmdmark{color:var(--muted)}
.cmdline code{background:rgba(127,127,127,.14);border-radius:6px;padding:1px 7px;
font-family:ui-monospace,Consolas,monospace;font-size:12.5px;color:var(--text)}
pre.cmdout{margin:4px 0 0;padding:6px 10px;background:var(--panel2);border:1px solid var(--border);
border-radius:6px;color:var(--muted);font-size:12px;white-space:pre-wrap;word-break:break-word;
max-height:16em;overflow:auto}
details.notify{border:1px solid var(--border);border-radius:8px;background:var(--panel2)}
details.notify>summary{color:var(--muted);font-size:12.5px;padding:5px 10px}
/* 中途插話（排隊送出）：⚠ 給琥珀色，和 `.compact-sep`／`.chip.sub` 同一組語意
   ——「這裡發生了一件會改變後續走向的事」。**不給紅**：紅在本專案專指真的快取失效。 */
.badge.queued{background:rgba(210,150,60,.22);color:#d2963c;cursor:help}
/* 中途插話：**畫在該輪內部**，因為實測 98.4% 的插話就是在同一輪之內送達的。
   用使用者那條藍（`--user`）標身分，和外層助手回合分得開。 */
.ijbox{border:1px solid var(--user);border-left:3px solid var(--user);border-radius:8px;
margin:10px 0;background:var(--panel2)}
.ijhead{display:flex;align-items:center;gap:8px;padding:6px 12px;font-size:13px;
font-weight:600;color:var(--user);border-bottom:1px solid var(--border)}
.ijwhen{margin-left:auto;font-weight:400;color:var(--muted);font-size:11.5px;cursor:help}
.ijbody{padding:6px 12px 10px}
/* 「插話之後」那一段：縮排＋左側細線，表示這些是因為那句話才做的 */
.ijafter{margin:0 0 8px 10px;padding-left:12px;border-left:2px dashed var(--user);opacity:.98}
.ijafterhead{color:var(--user);font-size:11.5px;margin:4px 0 2px;opacity:.8}
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
/* 人因可避免的快取浪費：與逐步徽章的「真失效」紅同一組語意，別的冷啟一律不給紅 */
.chip.waste{background:rgba(248,81,73,.22)}
td.waste-td{color:var(--muted)}
.wchk{font-size:13px;white-space:nowrap;display:inline-flex;align-items:center;gap:4px}
/* 索引頁「這一場有書籤」的圖示。⚠ 建置期不知道有沒有——由 bkIndexMark() 在載入時插入。
   ⚠ `white-space:nowrap` ＋ `margin-right`：它插在標題連結**前面**，不可以把標題擠斷行。 */
.bmflag{display:inline-block;margin-right:6px;font-size:12px;white-space:nowrap;
        color:var(--accent);cursor:default}
.bmflag.due{color:#d29922}      /* 有 ⏰ 該複查的那幾場；⚠ 只是標記，永遠不會自動刪 */
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
.acct-sep{display:flex;align-items:center;text-align:center;color:var(--accent);font-size:12px;margin:20px 0 8px}
.acct-sep::before,.acct-sep::after{content:"";flex:1;border-top:1px dashed var(--accent);opacity:.5}
.acct-sep span{padding:0 12px}
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
.report .cz-tools{background:#6ea8fe}
.report .cz-system{background:#7fb77f}
.report .cz-msgs{background:#c9b458}
.report .cz-compact{background:#d2963c}
.report .cz-expiry{background:var(--accent)}
.report .cz-evict{background:var(--err)}
.report .cz-acct{background:#d87ba8}
.report .cz-unavail{background:rgba(139,148,158,.45)}
.report .cz-unknown{background:rgba(139,148,158,.16)}
.report .cz-intra{background:rgba(139,148,158,.28)}
/* ⚠ REPORT_CAUSES 的**每一個** key 都要在這裡有一條 .cz-<key>：圖例色塊與 (A) 成因分期堆疊圖
   都直接拿 key 當 class，少一條就是「占了寬度卻畫不出來」——看起來像圖表破洞，不像漏設定。
   `test_smoke` 有一條測試釘住這個對應。 */
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
/* 每輪標頭的 # 直達連結：滑過該輪才顯形。⚠ 錨點本身不掛在這裡（掛在 .turn 上），
   理由見 render_turn_html 的註解——.hl 外框是「跳成功了」的唯一訊號。 */
/* ⚠ 隱藏時要一併關掉命中測試：`opacity:0` 不會讓元素退出點擊，否則每個回合標頭右緣
   都多一塊看不見卻按得到的區域。⚠ 這個連結**會**佔掉 23px（`.when` 是 margin-left:auto
   靠右，新元件進同一列本來就會推它）——那是刻意加的 UI，不是版面缺陷。 */
.alink{margin-left:8px;color:var(--muted);text-decoration:none;font-weight:600;
       opacity:0;pointer-events:none;transition:opacity .12s}
/* ⚠ `:not(.blk-ctl)` 是第 4 期加的：區塊層級也用 `.alink`，沒排除的話滑過一輪就會
   同時亮起該輪**每一個區塊**的按鈕（實測 p99 有 101 個）。見下方〈區塊層級的 #／☆〉。 */
.turn:hover .alink:not(.blk-ctl),.alink:not(.blk-ctl):focus{opacity:.65;pointer-events:auto}
.alink:hover{opacity:1;color:var(--accent)}
/* ⚠ 沒有 hover 的裝置上，靠 `.turn:hover` 顯形等於這個功能的主要入口永遠看不到。 */
@media (hover:none){.alink{opacity:.55;pointer-events:auto;margin-left:6px;padding:2px 8px}}
.tanchor{position:absolute;width:0;height:0;overflow:hidden}  /* ⚠ 必須 absolute：`display:inline-block` 時它仍是 `.turn .head` 的 flex item，會吃掉一份 `gap:8px`；而 `.compact-sep span{padding:0 12px}` 是**後代選擇器**、連它也 match，加上 `box-sizing:border-box`，`width:0` 縮不掉那 24px。實測每個回合標頭偏移 8px、每條 compact 分隔線偏移 24px。 */
/* 書籤指向的回合找不到時的橫幅。⚠ 沒有它的話「找不到」是徹底靜默的
   （openSub 的第一行就 return），使用者分不出「沒跳」和「本來就在頁首」。 */
/* ⚠⚠ **一定要 fixed。** 之前是文件流裡的一塊、插在 body 最前面，而 `scrollIntoView`
   已經把使用者帶到頁面深處——實測橫幅落在視窗上方 1928px 處，精確命中與退化命中在使用者
   視窗裡**逐像素相同**。fixed 之後它永遠在視窗內，也不再造成版面位移（out of flow）。
   ⚠⚠ **但一定要放在「底部」，不能放頂端。** 第一版放 `top:12px`，而
   `scrollIntoView({block:'start'})` 會把目標輪貼到視窗頂端（y=0）——實測橫幅佔 12–79px，
   **蓋住的正好是那一輪的標頭**：`.hl` 外框上緣、時間戳、`#` 直達連結全在底下。
   「.hl 是跳成功的唯一訊號」而它被自己的提示遮掉，等於白做（`durable-anchor-r3` #2）。 */
.bm-miss{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:60;
         max-width:min(900px,calc(100vw - 24px));padding:10px 14px;
         border:1px solid var(--err);border-radius:8px;background:var(--panel);
         box-shadow:0 4px 16px rgba(0,0,0,.28);font-size:14px;line-height:1.6;
         max-height:34vh;overflow-y:auto}
/* ⚠ 關閉鈕要有可按的大小：`padding:2px 4px` 量出來只有 20×19px，低於任何合理的觸控下限
   （`durable-anchor-r4` #5，由 scripts/probe_anchor_layout.py 守著）。 */
.bm-x{margin-left:10px;border:0;background:transparent;color:inherit;cursor:pointer;
      font-size:16px;line-height:1;opacity:.6;padding:0;
      min-width:28px;min-height:28px;vertical-align:middle}
.bm-x:hover{opacity:1}
/* ⚠ 小視窗下文字會折成很多行——實測 500×400 時橫幅吃掉 44% 視窗高。收緊間距與字級。 */
@media (max-width:640px){.bm-miss{padding:8px 10px;font-size:13px;line-height:1.5;max-height:30vh}}
/* ══ 書籤（第 2 期）══════════════════════════════════════════════════════
   ⚠⚠ 這一整段是**普通字串**，大括號寫一個就好。頁面的 script 區塊才是 f-string、
   那裡要雙括號。同一個檔裡兩種規則並存，寫錯的話 CSS 會整段壞掉。
   ⚠ 註解裡**不要寫字面的角括號 script 標籤**：CSS 會原樣落進 style 元素，
   讓「整頁標籤開闔成對」這條檢查（test_bookmark_ui）永遠對不起來。 */
/* 每輪標頭的加書籤鈕：與 .alink 同一套顯形規則（滑過該輪才出現）。
   ⚠ **已加書籤的那一輪要永遠看得見**（.on）——否則使用者看不出自己存過哪幾輪，
   而「這一輪存過沒有」正是他來這一頁要問的第一個問題。
   ⚠ 和 .alink 一樣，它**會**永久佔掉標頭右側的寬度（24px + 4px margin）。
   那是刻意加的 UI，不是版面缺陷；小視窗會不會擠爆由 probe_anchor_layout.py 守。 */
.bmk{margin-left:4px;border:0;background:transparent;color:var(--muted);cursor:pointer;
     font-size:13px;line-height:1;padding:0;min-width:24px;min-height:24px;
     opacity:0;pointer-events:none;transition:opacity .12s}
/* ⚠ `:not(.blk-ctl)` 的理由同 .alink（第 4 期）。 */
.turn:hover .bmk:not(.blk-ctl),.bmk:not(.blk-ctl):focus{opacity:.65;pointer-events:auto}
.bmk:hover{opacity:1;color:var(--accent)}
.bmk.on{opacity:1;pointer-events:auto;color:var(--accent)}
/* ⚠ 沒有 hover 的裝置上靠 .turn:hover 顯形＝這個功能的入口永遠看不到（同 .alink）。 */
@media (hover:none){.bmk{opacity:.55;pointer-events:auto}}
/* ── 區塊層級的 #／☆（第 4 期）─────────────────────────────────────────
   ⚠⚠ **顯形範圍一定要縮到 `.blk`，不能沿用 `.turn:hover`。** 全語料實測一輪的可標記
   區塊數 p99＝101（Codex 129、max 412）——沿用整輪的規則，滑過任何一處就會同時亮起
   一百組按鈕。上面兩條 `.turn:hover` 規則因此加了 `:not(.blk-ctl)` 把區塊那組排除掉。
   ⚠ 用 `>` 直接子選擇器。**但這裡原本寫的理由是假的**（`bookmarks-p4-fam` Low）：
   舊版說「工具區塊裡可能巢著子代理的回合，那裡面又有 `.blk`」——**不會**：
   `render_turn_html` 把子代理區塊放在 `extra`、當成工具區塊的**兄弟**，
   實測真頁上 `.blk .blk` 恒為 0，而且探針的 `subagent_not_wrapped_in_block`
   正好在保證它永遠為 0。
   → `>` 留著是因為它**比較窄、而且未來真的巢起來時不會壞**，
   不是因為現在有巢狀結構。**理由寫錯比沒寫更貴**：下一個人會拿它去推別的事。 */
.blk{position:relative}
/* ⚠⚠ **容器一定要 `pointer-events:none` ＋ 透明背景。**（`bookmarks-p4-fam` Medium）
   第一版把 `background:var(--panel)` 放在容器上、`opacity:0` 只加在**子元素**上 ⇒
   每個區塊右上角固定有一塊 49×24px 的不透明方塊，**不管有沒有滑過**：
   蓋住第一行右端（code fence、寬表格、長工具摘要），而且那塊區域的文字**選不起來也點不到**。
   實測 `elementFromPoint(控制項內 2px)` 回的是 `SPAN.blk-ctls` 而不是底下的字。
   背景移到按鈕自己身上——按鈕平常 `opacity:0`，只有顯形時才連背景一起出現。 */
.blk-ctls{position:absolute;right:4px;top:0;z-index:2;display:inline-flex;align-items:center;
 line-height:1.6;pointer-events:none;background:transparent}
.blk-ctls .blk-ctl{opacity:0;pointer-events:none;transition:opacity .12s;
 background:var(--panel);border-radius:6px}
.blk:hover>.blk-ctls>.blk-ctl,.blk-ctls>.blk-ctl:focus{opacity:.65;pointer-events:auto}
.blk-ctls>.blk-ctl:hover{opacity:1;color:var(--accent)}
/* ⚠ 已加書籤的那一顆要**一直**看得見，否則「這一塊存過沒有」得靠滑鼠一格一格掃。
   與 `.bmk.on` 同一個理由，但這裡必須再寫一次：上面那條 `.blk-ctls .blk-ctl{opacity:0}`
   的權重（0,2,0）壓過 `.bmk.on`（0,2,0）後來居上——同權重時後寫的贏。 */
.blk-ctls>.bmk.on{opacity:1;pointer-events:auto;color:var(--accent)}
/* ⚠ 沒有 hover 的裝置：同 .alink／.bmk，不給常駐就等於這個入口不存在。 */
@media (hover:none){.blk-ctls .blk-ctl{opacity:.5;pointer-events:auto}}
/* 對話窗：fixed 覆蓋層，不進文件流（不造成版面位移）。 */
.bm-modal{position:fixed;inset:0;z-index:70;display:none;
          background:rgba(0,0,0,.45);padding:16px;overflow-y:auto}
.bm-modal.open{display:flex;align-items:flex-start;justify-content:center}
.bm-card{width:min(560px,100%);margin:auto;background:var(--panel);color:var(--text);
         border:1px solid var(--border);border-radius:12px;padding:16px 18px;
         box-shadow:0 8px 32px rgba(0,0,0,.4);font-size:14px;line-height:1.6}
.bm-card h3{margin:0 0 4px;font-size:16px}
.bm-lab{display:block;margin:12px 0 4px;color:var(--muted);font-size:12.5px}
.bm-card input[type=text],.bm-card textarea{width:100%;background:var(--panel2);
  color:var(--text);border:1px solid var(--border);border-radius:8px;padding:7px 9px;font:inherit}
.bm-card textarea{min-height:60px;resize:vertical}
.bm-prev{background:var(--panel2);border:1px solid var(--border);border-radius:8px;
         padding:7px 9px;color:var(--muted);font-size:12.5px;max-height:66px;overflow-y:auto;
         word-break:break-word}
/* ⚠ 單選用「一排 chip」而不是下拉：Will 的原話是「下拉使用上比較不方便」。
   語意仍然是單選（role=radio + aria-checked），只是長得比較好按。 */
.bm-chips{display:flex;flex-wrap:wrap;gap:6px}
.bm-chip{border:1px solid var(--border);background:var(--panel2);color:var(--text);
         border-radius:999px;padding:6px 12px;cursor:pointer;font:inherit;font-size:13px;
         min-height:32px}
.bm-chip[aria-checked=true]{border-color:var(--accent);color:var(--accent);font-weight:600}
.bm-act{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px;align-items:center}
.bm-act .grow{flex:1}
/* ⚠ 管理頁與設定頁的按鈕**不在 `.bm-card` 裡**（對話窗只是那兩頁的一小部分），
   所以這四條選擇器一定要一起涵蓋 `.bm-foot`／`.bm-act`／`.bm-item`／`.bs-cat`
   ——少一個那一區就會退回瀏覽器預設樣式，在深色底上幾乎看不見。 */
.bm-card button,.bm-foot button,.bm-act button,.bm-item button,.bs-cat button{
  border:1px solid var(--border);background:var(--panel2);color:var(--text);
  border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit;font-size:13px;min-height:32px}
.bm-card button:hover,.bm-foot button:hover,.bm-act button:hover,
.bm-item button:hover,.bs-cat button:hover{border-color:var(--accent)}
.bm-card button.pri,.bm-act button.pri,.bs-cat button.pri{
  border-color:var(--accent);color:var(--accent);font-weight:600}
.bm-card button.dang:hover,.bm-item button.dang:hover,.bs-cat button.dang:hover{
  border-color:var(--err);color:var(--err)}
/* ⚠ 匯出／匯入和存檔鈕**同一批出貨**：第一顆書籤存下去的那一刻，資料就只在
   localStorage 裡，一次「清除瀏覽資料」就沒了。真正的耐久保證是匯出的 JSON。 */
.bm-foot{margin-top:14px;padding-top:12px;border-top:1px solid var(--border);
         display:flex;flex-wrap:wrap;gap:8px;align-items:center;
         color:var(--muted);font-size:12.5px}
.bm-list{margin:8px 0 0;padding:0;list-style:none;max-height:42vh;overflow-y:auto}
.bm-item{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:6px;
         background:var(--panel2)}
.bmi-top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.bmi-cat{color:var(--accent);font-size:12px}
.bmi-due{color:#d29922;font-size:12px}      /* ⏰ 該複查了；⚠ 只是標記，永遠不會自動刪 */
.bmi-note{white-space:pre-wrap;word-break:break-word}
.bmi-sum{color:var(--muted);font-size:12px;word-break:break-word}
.bm-msg{margin-top:10px;font-size:12.5px;color:var(--accent);min-height:1.2em}
.bkbtn.has{border-color:var(--accent);color:var(--accent)}
@media (max-width:640px){.bm-card{padding:12px 13px;font-size:13.5px}
  .bm-modal{padding:8px}.bm-list{max-height:38vh}}
/* 書籤管理頁／設定頁（第 3 期）。
   ⚠ `button.bm-chip` 這一條要放在上面那組 `.bm-card button` 之後：兩者權重相同（0,1,1），
   靠**後到者勝**把 chip 拉回圓角膠囊。少了它，對話窗裡的 chip 會被當成一般按鈕。 */
button.bm-chip{border-radius:999px;padding:6px 12px}
.bm-chip.on{border-color:var(--accent);color:var(--accent);font-weight:600}
.bm-chip.add{border-style:dashed}
.bm-chip.more{color:var(--muted)}
.bm-newcat{display:inline-flex;gap:4px;align-items:center}
.bm-newcat input{width:auto;min-width:9em;background:var(--panel2);color:var(--text);
  border:1px solid var(--accent);border-radius:999px;padding:6px 12px;font:inherit;font-size:13px}
.bm-mgr{font-size:12.5px;white-space:nowrap}
.bxcount{color:var(--muted);font-size:12.5px;margin:10px 0 0}
/* ⚠ 管理頁的清單是**整頁的主體**，不可以沿用對話窗那個 42vh 的內捲高度。 */
.bx-list{max-height:none;overflow:visible}
.bx-item.due{border-color:#d29922}
.bx-item .bmi-top .grow{flex:1}
.bx-go{white-space:nowrap}
.bx-sess{font-size:12.5px;margin-top:2px;word-break:break-word}
.bs-h{font-size:15px;margin:26px 0 6px;padding-top:16px;border-top:1px solid var(--border)}
.bs-list{max-height:none;overflow:visible}
.bs-cat{display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:var(--panel2);
  border:1px solid var(--border);border-radius:8px;padding:6px 10px;margin-bottom:6px}
.bs-cat .grow{flex:1}
.bs-name{font-weight:600;word-break:break-word}
.bs-cat input[type=text],.bm-act input[type=text]{background:var(--panel);color:var(--text);
  border:1px solid var(--accent);border-radius:8px;padding:6px 10px;font:inherit;font-size:13px}
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


def session_signature(main_path: Path, marks=()) -> str:
    """主檔 + 外部子代理檔 + 同專案 memory/ 的 (檔名, mtime, size) 組合，作為變動指紋。
    把 memory 納入：memory 新增/變動/移除時，session 會重建（頁頂 🧠 連結與 row 的 mem_href 才會更新）。

    `marks` 是這場 session 的切帳號時刻（load_account_switches 的結果）。**必須進指紋**：
    那份資料來自 repo 外的 history.jsonl，transcript 完全沒變也可能改變歸因結果（新增一個帳號、
    還原備份都會）。不納入的話，增量建置會沿用舊 row，把 acct 歸因與「浪費」欄靜默停在舊值——
    使用者看到的是過期的結論而且沒有任何跡象。空值與非空值會產生不同字串，所以增減兩個方向都會觸發重建。"""
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
    if marks:
        # ⚠ 精度必須與**顯示定位用的精度**一致。截成整秒的話，同一秒內的切換時刻變動
        # （例如 .700 → .300，會跨過某個回合）不會改變指紋 → 增量建置沿用舊頁，
        # 分隔線就停在錯的位置。毫秒是 history.jsonl 的原生精度，`.3f` 穩定可重現。
        parts.append("acct:" + ",".join(f"{float(t):.3f}" for t in marks))
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
        "kind": s.kind,
        "models": [short_model(m) for m in s.models],
        "cost": s.cost,
        "cost_partial": s.cost_partial,
        "cache_pct": s.cache_pct,
        # cache-report 原料：[[epoch, cache_read, 脈絡, 寫總量, 寫5分, 寫1h, 模型idx, 自報成因碼, 重算tokens], …]
        "cache_steps": getattr(s, "cache_steps", []),
        "cache_models": getattr(s, "cache_models", []),
        # [[epoch, "limit"|"auth"|"compact"|"acct"], …]（acct 來自 load_account_switches）
        "cache_events": getattr(s, "cache_events", []),
        # 人因浪費：本場「可避免」冷啟的次數與估算金額（索引徽章/欄位/篩選用；見 session_waste）
        "waste_n": getattr(s, "waste_n", 0),
        "waste_usd": getattr(s, "waste_usd", 0.0),
        "waste_partial": getattr(s, "waste_partial", False),
        "codex_steps": getattr(s, "codex_steps", []),    # Codex 存活頁原料：[[epoch, cache_read, 脈絡], …]
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


def load_manifest_paths(out: Path) -> dict:
    """只取 `sid → row` 用來**算檔名與標題**，**忽略 renderer 版本**。

    ⚠ 和 `load_manifest` 刻意分開，兩支不可以互相取代：
    那一支在 renderer 升版時回空字典是**對的**（快取的 row 內容可能已經過時，
    不能拿來重用輸出）；但**檔名與標題不隨 renderer 改變**，
    而書籤管理頁需要的就只有那兩樣。

    ⚠⚠ 不做這件事的後果（`bookmarks-p23` Medium）：升版後第一次跑縮範圍建置
    （`--project X`）時 `rows` 只剩本次掃到的那幾場，管理頁就會對著**磁碟上還好端端
    躺著**的 session 斬釘截鐵地說「⚠ 不在這次的輸出裡」並且不給連結。
    索引頁少幾列還看得出是「這次沒涵蓋」，管理頁那句話卻是**明確的假話**。
    """
    try:
        data = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("entries", {}) or {}


def save_manifest(out: Path, entries: dict):
    (out / MANIFEST_NAME).write_text(
        json.dumps({"renderer_version": RENDERER_VERSION, "entries": entries}, ensure_ascii=False),
        encoding="utf-8")


def manifest_key(source_kind: str, path: Path) -> str:
    return f"{source_kind}:{path.resolve()}"


def _index_rows(entries: dict, include_empty: bool) -> list:
    """manifest entries → 索引頁要畫的 rows。

    ⚠⚠ **正常路徑與「零場對帳」那條路一定要用同一段。** 那兩處曾經各寫一份，
    結果零場那份**漏掉了 `empty` 的過濾**（曾用 `--include-empty` 建過的話，
    manifest 裡會留著 `empty:true` 的 row，之後一次不帶那個旗標的零場建置就會把它畫進索引）
    ——同一份輸入在兩條路上長得不一樣。`bookmarks-fix2-fam-r2` 驗收找到的。
    ⚠ 抽成函式**本身就是修法**：把條件複製一份、再叮嚀「兩邊要一致」，是這條線上
    重複失效的那個做法（`bkMerge` 與 `bkCommit` 的守門條件也是這樣分岔的）。
    ⚠ 誠實邊界：`empty` 那一格**沒有專屬的測試素材**（要造 `n_turns==0` 的語料）。
    現在它承重的理由是「兩條路共用同一段」這個結構，不是有一格斷言在守它——
    不要在這裡寫「守它的是 XXX」。
    """
    return [e["row"] for e in entries.values()
            if e.get("row") and (include_empty or not e["row"].get("empty"))]


ARCHIVE_MANIFEST = "_archived.jsonl"


def session_content_kind(s) -> str:
    """這一場 session 的內容分類：`"conversation"` / `"command_only"` / `"empty"`。

    ⚠⚠ **判準直接讀渲染管線的產物（`s.main_groups`），不另寫一套。**
    這是搬檔的依據——判錯的代價是把一場**有對話**的紀錄搬離
    `~/.claude/projects/`，而那個目錄是 **Claude Code 自己**在讀的
    （`claude --resume` 的清單）。另寫一份判準，遲早和「畫面上看得到什麼」分岔，
    而分岔的那一天沒有任何跡象。

    `"command_only"` 的定義是**畫面上除了指令列以外什麼都沒有**：
    沒有助手內容、沒有使用者發言、沒有中途插話。
    ⚠ **背景任務通知不算對話**（它是系統注入的），但**單獨只有通知**也不算
    `command_only`——那一格回 `"empty"`，本函式的呼叫端只搬 `command_only`。
    """
    has_cmd = False
    for g in getattr(s, "main_groups", []) or []:
        for b in g["blocks"]:
            t = b.get("type")
            if t == "_command":
                has_cmd = True
            elif t == "_notify":
                pass                      # 系統注入，不算對話
            elif t == "_interject":
                # 使用者真的說了話。
                # ⚠⚠ **這一格目前到不了，是刻意留的縱深，不是有效的守衛。**
                # `_interject` 只會被掛進**開著的 assistant 回合**，而那種回合必然
                # 至少有一個可呈現的 block（`group_turns` 對沒有內容的 assistant 事件
                # 直接 `continue`）⇒ 底下那條 `block_is_renderable` 一定先回
                # `"conversation"`。突變檢驗實測：把這一行改成 `pass`，**沒有任何一格會紅**。
                # ⇒ 留著是因為「插話 ＝ 對話」這條語意本身正確，
                #   但**不要把它算進「已驗證的防線」**（教訓 29）。
                #   它會變成有效守衛的條件：哪天 `_interject` 能掛進沒有可呈現內容的回合。
                return "conversation"
            elif t == "_step":
                pass                      # 只是步驟分隔，本身沒有內容
            elif block_is_renderable(b, g["role"]):
                # ⚠⚠ **user 與 assistant 走同一支判準，不可以各寫一份。**
                # 舊版 user 那條只看 `clean_user_text(b["text"])` ⇒ **使用者貼的圖片
                # （`{"type":"image"}`，沒有 `text` 欄）不算對話** ⇒ 一場「只貼了一張圖
                # ＋下過任一指令」的 session 會被判成 `command_only` **搬離
                # `~/.claude/projects/`**——而那張圖在頁面上是畫得出來的
                # （`render_image_block`）。
                # 那正是本函式 docstring 說「另寫一份判準遲早和畫面分岔」要避開的事，
                # 而它自己就分岔了（`utf-fam` High#2 實測）。
                # ⚠ `_command`／`_notify`／`_step`／`_interject` 都在前面的 elif 攔掉了，
                #   這裡不會誤收；`tool_result` 與純空白本來就不是可呈現區塊，判 False 正確。
                return "conversation"
    # 子代理有內容也算有對話（不可能只有指令，但別讓判準有洞）
    for g in getattr(s, "side_groups", []) or []:
        for b in g["blocks"]:
            if b.get("type") not in ("_step", "_command", "_notify") \
                    and block_is_renderable(b, g["role"]):
                return "conversation"
    # ⚠⚠ **最後一道保守否決：不認得的 content block 一律當對話。**
    # 放在這裡（而不是迴圈前面）是因為它比較貴——要掃原始事件；能在渲染產物就判出
    # `conversation` 的走上面那條就好。理由與實測見 `_has_unknown_content_block()`。
    if _has_unknown_content_block(s):
        return "conversation"
    return "command_only" if has_cmd else "empty"


def archive_name(cwd: str, path: Path) -> str:
    """搬到封存目錄後的檔名：`<munged cwd>__<原檔名>`。

    ⚠ 用 **munged 的 cwd**（`munge_path`，把 `:` `\\` `/` 全換成 `-`）而不是原始路徑：
    原始路徑帶分隔符，當檔名會建出一整串子目錄。munged 名同時是**可讀又可還原**的
    ——`D--Workshops-Will-proj__<sid>.jsonl` 一眼看得出它本來在哪。
    ⚠ 沒有 cwd 的（極少數壞檔）用 `_nocwd`，不要讓檔名變成 `__<sid>`。
    """
    return f"{munge_path(cwd) if cwd else '_nocwd'}__{path.name}"


def _drop_session_pages(out: Path, key: str) -> tuple:
    """刪掉某個來源檔產生的頁面（HTML／MD）。回傳 `(刪掉幾個, [失敗原因…])`。

    路徑取自 manifest 的 `out_html`／`out_md`，**不自己重組檔名**——檔名規則
    （時間前綴、專案名截斷、sid 前 8 碼）在別處，重組一份遲早分岔。

    ⚠⚠ **那兩個欄位是相對 `out/sessions/`，不是相對 `out/`。**
    少接一層 `sessions/` 的話 `unlink()` 會靜靜地什麼都刪不到——`is_file()` 為 False、
    不丟例外、回報「刪了 0 個」也不會有人看。索引頁那邊也是接 `"sessions/" + out_html`。

    ⚠⚠ **刪不掉一定要往上報，不可以 `except OSError: pass`。**
    頁面被鎖／唯讀／權限不足時，舊版把錯誤整個吞掉、來源照搬 ⇒ 磁碟上留下一個
    **索引連不到、但仍含私密對話**的孤兒頁，而輸出上完全沒有跡象
    （`utf-fix-codex` Medium 實測）。回報之後由呼叫端決定怎麼辦。
    """
    n, fails = 0, []
    entries, _ = load_manifest(out)
    row = (entries.get(key) or {}).get("row") or {}
    for f in (row.get("out_html"), row.get("out_md")):
        if not f:
            continue
        p = out / "sessions" / f
        try:
            if p.is_file():
                p.unlink()
                n += 1
            elif p.exists():
                # ⚠ manifest 說這裡有一頁，而那個路徑存在、卻不是普通檔
                # （被換成目錄、或是某種特殊項目）⇒ **清不掉**，和刪失敗同一類。
                # 靜靜跳過的話它會留在磁碟上，而來源已經搬走、不會再重產。
                fails.append(f"{p.name}：路徑存在但不是普通檔，清不掉")
        except OSError as e:
            fails.append(f"{p.name}：{type(e).__name__}: {e}")
    return n, fails


def archive_command_only(sessions, dest: Path, scanned_roots, out: Path) -> tuple:
    """把「只有指令、沒有對話」的 session 檔搬到 `dest`，並刪掉它產生的頁面。

    `sessions` ＝ [(kind, acc, proj, path, Session)]，只處理 Claude 側。
    回傳 `(搬走的清單, 略過的原因 Counter)`。

    ⚠⚠ **這是本工具唯一會動到 `out/` 以外檔案的功能。** 四道安全閘，缺一不可：

    1. **目的地不可以在來源裡面**（`~/.claude/projects/…`）——搬進去等於沒搬，
       而且下一次執行會再掃到它、再搬一次，路徑越接越長。
    2. **目的地不可以在 `out/` 裡面**——`out/` 會被整批重產與清理。
    3. **絕不覆蓋**：目的地已經有同名檔就略過並回報，不比對內容、不改名硬塞。
       ⚠ 用 `O_CREAT|O_EXCL` **原子地**取得檔名，不是 `exists()` 之後再搬。
    4. **每一筆都寫進 `_archived.jsonl`**（原始絕對路徑、新檔名、時間、判定理由），
       所以還原是機械的，不必靠人記得檔名怎麼組。
       ⚠⚠ **兩段式**：搬之前先寫一行 `state:"pending"`、搬成功後再寫一行 `state:"moved"`，
       兩行都 `fsync` 落磁。⇒ 任何時刻被中斷，紀錄裡都找得到「有一場正在從 A 搬到 B」。
       ⚠ **舊版（2026-08-25 之前）的那些行沒有 `state` 欄**——沒有 `state` 一律當
       `moved`，那批已經獨立驗過 92/92 完整。
    5. **記帳寫得進去，才可以開始搬**：manifest 一開始就打開並整趟持有，
       開不起來直接 `SystemExit`。舊版是搬完才開檔，於是「來源已經不在、
       還原依據一行都沒有」是一條真的路徑。

    ⚠ 子代理外部轉錄（`<sid>/` 目錄）跟著一起搬——只搬主檔會留下孤兒目錄。

    ⚠⚠ **產出的頁面在這裡就刪掉，不要指望 `prune_gone_sources()`**——那一支只對帳
    manifest 紀錄、**不刪磁碟上的檔**（`SCOPE-BOOKMARK-PRUNED-ORPHAN-FILES`）。
    不在這裡刪，`out/` 會留下一批指不到來源的孤兒頁，而索引已經不再連到它們
    ＝**看不見、但仍含私密對話**的檔案留在磁碟上。
    """
    # ⚠ 用 plain dict 不用 `collections.Counter`：本模組刻意沒有 import collections，
    # 為了一個計數器多一個 import 不划算（`--version` 那條線也還沒動過這裡）。
    moved, skipped = [], {}

    def _skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    dest = dest.resolve()
    for root in scanned_roots:
        try:
            rr = Path(root).resolve()
        except Exception:
            continue
        if dest == rr or rr in dest.parents:
            raise SystemExit(f"拒絕執行：封存目錄在來源目錄裡面（{dest}）")
        # ⚠ **反方向也要擋。** `--archive-command-only ~/.claude` 是很自然的手誤，
        # 那會把 JSONL 直接倒進 Claude Code 的設定目錄，和 `settings.json`、
        # `history.jsonl` 混在一起。
        if dest in rr.parents:
            raise SystemExit(f"拒絕執行：封存目錄是來源目錄的上層（{dest}）")
    out_r = out.resolve()
    if dest == out_r or out_r in dest.parents:
        raise SystemExit(f"拒絕執行：封存目錄在輸出目錄裡面（{dest}）")
    dest.mkdir(parents=True, exist_ok=True)
    man = dest / ARCHIVE_MANIFEST

    # ⚠⚠ **第 5 道閘：記帳寫得進去，才可以開始搬。**
    # 舊版是「搬完才開檔記帳」⇒ manifest 不可寫（權限／磁碟滿／它其實是個目錄）時，
    # 主檔已經離開 `~/.claude/projects/`、目的地有檔、**而還原依據一行都沒有**
    # （`utf-fix-codex` High#3 實測 `archive_rc=1 source_exists=False manifest_is_dir=True`）。
    # 先開起來、整趟持有這個 handle：寫不進去就在**任何一次搬移之前**停下來。
    try:
        man_fh = man.open("a", encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"拒絕執行：封存紀錄寫不進去（{man}）：{type(e).__name__}: {e}")

    def _journal(row):
        """寫一行紀錄並**確實落磁**（flush ＋ fsync）。

        ⚠ 沒有 fsync 的話，行程被砍／斷電時緩衝區裡的那幾行會消失，
        而檔案已經搬走了——「還原依據」必須比「已經發生的搬移」更耐久。
        """
        man_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        man_fh.flush()
        os.fsync(man_fh.fileno())

    try:
        _archive_loop(sessions, dest, out, _journal, moved, _skip)
    finally:
        man_fh.close()
    return moved, skipped


def _archive_loop(sessions, dest: Path, out: Path, _journal, moved, _skip):
    """`archive_command_only()` 的主迴圈——安全閘都過了之後的逐場處理。

    抽出來只是為了讓 manifest 的 handle 有一個乾淨的 `finally` 可以關，
    行為與判準全部在這裡，`archive_command_only()` 的 docstring 是它們的說明。
    """
    for kind, acc, proj, path, s in sessions:
        if kind != SOURCE_CLAUDE:
            continue
        if session_content_kind(s) != "command_only":
            continue
        target = dest / archive_name(getattr(s, "cwd", ""), path)
        side = path.with_suffix("")
        # ⚠⚠ **來源的絕對路徑要在搬走之前算好。** `--claude-source` 收得下相對路徑，
        # 而舊版寫進紀錄的是未 resolve 的 `str(path)` ⇒ CLI 同時印著「含原始絕對路徑」，
        # 兩者互相矛盾，而還原的人只有那份紀錄（`utf-fix-codex` High#3 實測
        # `FROM_IS_ROOTED=False`）。⚠ 搬完再 resolve 是錯的——那時來源已經不在了。
        try:
            src_abs = str(path.resolve())
        except OSError:
            src_abs = str(path.absolute())
        # ⚠ 頁面**先刪、再搬**：搬完才刪的話，中間若失敗就會留下「來源已經不在、
        # 頁面還在」的孤兒；反過來最壞只是頁面被刪、來源還在，下一次執行會重產。
        # 失敗方向要選可自癒的那一邊。
        # ⚠⚠ **順序是：原子佔名 → pending 落磁 → 刪頁 → 搬移**，而且**任何一步失敗
        # 都要把佔位檔收回來**（`utf-fix-codex-r2` 驗收）。
        # 第一版把「刪頁」排在「佔名」之前、又只有 `shutil.move` 那一段有 cleanup ⇒
        # 注入一次 `fsync` 失敗就實測到：`source=True target=True target_size=0
        # html=False md=False`——**頁面已經刪了、目的地留下 0 位元組的佔位檔，
        # 而下一次執行會撞到「目的地已有同名檔」永遠略過這一場。**
        def _unreserve(tgt=None):
            """把這一趟用 `O_EXCL` 搶到的目的地檔收回來。**每一條失敗路徑都要走它。**

            ⚠⚠ **不可以只刪 0 位元組的。** `shutil.move` 跨磁碟會退化成「複製＋刪來源」，
            複製寫到一半才失敗時目的地是**部分內容**，不是 0 位元組
            （`utf-fix-codex-r3` 實測 `target_size=7`）⇒ 只認 0 的話那個半截檔留下來，
            下一次執行永遠撞「目的地已有同名檔」，這一場再也搬不動。
            ⚠ 判準是**這個名字是我們這一趟建出來的**（`O_EXCL` 保證），
              而且**來源還在**——兩者都成立時刪掉它是安全的。

            範圍限制 SCOPE-ARCHIVE-NO-RESTART-RECONCILE：本函式只涵蓋
            **這一趟行程內收得到的中斷**（例外與 `KeyboardInterrupt`）。
            硬終止／斷電之後留下的檔沒有跨次的恢復機制，會擋住那一場的後續封存
            （來源仍安全）。詳見 planning/scope-limits.md。

            ⚠⚠ **刪不掉要講出來，不可以吞掉。** 舊版 `except OSError: pass` ⇒
            輸出只講「fsync 失敗」，完全沒提「而且佔位檔清不掉」，
            使用者不會知道下一次會撞牆（同一輪實測 `UNLINK_FAIL`）。
            回 `True` 代表收乾淨了。
            """
            tgt = tgt if tgt is not None else target
            try:
                if not tgt.exists():
                    return True
                if not path.exists():
                    # 來源已經不在 ⇒ 目的地那個檔可能就是搬成功的結果，**絕對不要刪**。
                    return False
                tgt.unlink()
                return True
            except OSError as e2:
                _skip(f"⚠ 目的地的佔位檔清不掉（下一次會撞「已有同名檔」）："
                      f"{type(e2).__name__}: {e2}")
                return False

        # ⚠ `os.open` 與 `os.close` 要包在同一個受保護區：`close` 也可能丟例外，
        #   而那時檔名已經被我們搶走了（`utf-fix-codex-r3` 指出）。
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            _skip("目的地已有同名檔")
            continue
        except OSError as e:
            _skip(f"目的地建不出來：{type(e).__name__}: {e}")
            continue
        try:
            os.close(fd)
        except OSError as e:
            _unreserve()
            _skip(f"目的地建不出來：{type(e).__name__}: {e}")
            continue

        # ⚠⚠ **搬之前先記「打算搬」。** 這一行落磁之後，就算下一步整個行程被砍，
        # 還原的人也知道「有一場正在從 A 搬到 B」——去 A 或 B 找得到它。
        # 這是兩段式的第一段；成功之後會再寫一行 `state: "moved"`。
        try:
            _journal({"state": "pending", "moved_at": datetime.now(timezone.utc).isoformat(),
                      "from": src_abs, "to": str(target),
                      "cwd": getattr(s, "cwd", ""), "account": acc, "project": proj,
                      "reason": "command_only"})
        except OSError as e:
            # 記帳寫不進去（磁碟滿、handle 壞掉）⇒ **這一場一步都不要動**。
            # ⚠ 這一條**必須排在下面的 `BaseException` 前面**，否則它是死碼。
            _unreserve()
            _skip(f"封存紀錄寫不進去：{type(e).__name__}: {e}")
            continue
        except BaseException:
            # ⚠⚠ **`Ctrl+C` 也要收佔位檔。** 只接 `OSError` 的話，使用者在
            # 「搶到名字」與「寫 pending」之間按下 Ctrl+C，就留下一個
            # **0 位元組、而且 manifest 一行紀錄都沒有**的檔——之後每一次執行
            # 都會撞「目的地已有同名檔」而略過這一場，永遠搬不動
            # （`utf-fix-codex-r4` 實測 `INTERRUPT_GAP` ＋ `INTERRUPT_RETRY`）。
            # ⚠ **收完一定要原樣拋出去**：吞掉 `KeyboardInterrupt` 會讓 Ctrl+C 失效。
            _unreserve()
            raise
        try:
            n_pages, page_fails = _drop_session_pages(out, manifest_key(SOURCE_CLAUDE, path))
        except BaseException:
            _unreserve()
            raise
        if page_fails:
            _unreserve()
            # ⚠⚠ **刪不掉就整場不搬。** 搬了的話會留下「索引連不到、但仍含私密對話」
            # 的孤兒頁，而來源已經不在、下一次執行也不會再產生它 ⇒ 沒有人會再發現它。
            # 不搬的話最壞只是這一場留在原地，下一次重跑就好。
            _skip(f"頁面刪不掉，整場不搬：{page_fails[0]}")
            continue
        # ⚠⚠ 「絕不覆蓋」是上面那道 `O_CREAT|O_EXCL`——**原子的**，不是 `exists()` 之後再搬。
        # 兩者之間有窗：另一個 viewer（或任何程序）在中間建出同名檔時，
        # `shutil.move` 跨磁碟會退化成 copy ⇒ **覆蓋掉先前封存的紀錄**
        # （`utf-fix-codex` Medium 實測）。搶到名字的只會有一個，搶輸的拿 `FileExistsError`。
        try:
            # ⚠⚠ **一定要用 `shutil.move`，不可以用 `Path.replace()`。**
            # 後者是 `os.replace`，**跨磁碟機會直接丟 OSError**（Windows WinError 17）——
            # 而這個功能的典型用法正好就是跨磁碟機：來源在 `C:\Users\…\.claude\projects`，
            # 使用者指定的封存目錄多半在別的磁碟機。
            # 實測：第一版用 `replace()`，92 場**全部**失敗，而失敗被 `_skip()` 收成一行
            # 「搬移失敗：OSError：92」——**跑起來像是「沒有東西需要搬」**。
            # `shutil.move` 跨裝置時會退化成複製＋刪除，而且目錄也吃得下。
            shutil.move(str(path), str(target))
        except KeyboardInterrupt:
            # ⚠⚠ 跨磁碟複製到一半被 Ctrl+C：目的地是**部分內容**、來源還在。
            # 不收的話那個半截檔會擋住之後每一次執行
            # （`utf-fix-codex-r4` 實測 `PARTIAL_INTERRUPT` ＋ `PARTIAL_INTERRUPT_RETRY`）。
            # ⚠ `_unreserve()` 自己會判「來源還在才刪」，所以搬成功之後才中斷不會誤刪。
            _unreserve()
            raise
        except Exception as e:
            # ⚠ 把實際訊息帶出來。只印例外類別名的話，「跨磁碟機」和「權限不足」
            # 長得一模一樣，而它們的處置完全不同。
            # ⚠⚠ **佔位檔要收掉**：上面用 `O_EXCL` 搶到的那個名字是我們的，
            # 搬失敗還留著的話，下一次執行會撞到「目的地已有同名檔」而永遠搬不動。
            # ⚠ 跨磁碟時它可能是**寫到一半的部分內容**，不是 0 位元組——見 `_unreserve()`。
            _unreserve()
            try:
                _journal({"state": "failed", "moved_at": datetime.now(timezone.utc).isoformat(),
                          "from": src_abs, "to": str(target),
                          "error": f"{type(e).__name__}: {e}"})
            except OSError:
                pass          # 記帳也壞了：上面已經把佔位檔收回，來源仍在原地
            _skip(f"搬移失敗：{type(e).__name__}: {e}")
            continue

        # ⚠⚠⚠ **主檔一離開來源目錄，下一件事就必須是「記帳」。**
        # 舊版把子代理目錄的搬移放在同一個 `try` 裡、失敗就 `continue` ⇒
        # **主檔已經不在 `~/.claude/projects/` 了，而 `_archived.jsonl` 一行都沒寫**，
        # 畫面還回報「搬走 0 場」——使用者被告知什麼都沒發生，實際上少了一場
        # `claude --resume` 入口，**而且沒有任何還原依據**
        # （`utf-fam` High#1 實測：來源主檔不在、封存目錄有、manifest 0 行）。
        # ⇒ 記帳與 `moved` 都綁在**主檔搬移成功**這一件事上，
        #   子代理目錄的成敗只是這一筆的附註，不可以讓它推翻記帳。
        side_note, side_to = "", ""
        if side.is_dir():
            side_target = dest / target.stem
            # ⚠ 子代理目錄也要有「絕不覆蓋」閘：直接 `shutil.move` 到已存在的目錄
            # 會把它塞成**巢狀子目錄**（`utf-fam` Low#2）。
            # ⚠⚠ 和主檔同一個理由改成原子的：`os.mkdir` 在目的地已存在時丟
            # `FileExistsError`，中間沒有窗。搶到之後**逐一搬子項**進去
            # ——`shutil.move` 到一個「已經存在的目錄」正是會巢狀的那個動作。
            try:
                os.mkdir(str(side_target))
            except FileExistsError:
                side_note = "子代理目錄未搬：目的地已存在"
                _skip("子代理目錄未搬（目的地已存在）")
            except OSError as e:
                side_note = f"子代理目錄未搬：{type(e).__name__}: {e}"
                _skip(f"子代理目錄未搬：{type(e).__name__}")
            else:
                try:
                    for child in sorted(side.iterdir()):
                        shutil.move(str(child), str(side_target / child.name))
                    side.rmdir()
                    side_note, side_to = "子代理目錄已搬", str(side_target)
                except Exception as e:
                    # ⚠ 半搬的狀態要講出來，不可以只說「未搬」——那會讓還原的人
                    # 去原目錄找一個已經少了幾個檔的目錄。
                    side_note = f"子代理目錄只搬了一部分：{type(e).__name__}: {e}"
                    side_to = str(side_target)
                    _skip(f"子代理目錄只搬了一部分：{type(e).__name__}")
        # 兩段式的第二段：這一行落磁之後，這一筆才算完成。
        _journal({"state": "moved", "moved_at": datetime.now(timezone.utc).isoformat(),
                  "from": src_abs, "to": str(target),
                  "cwd": getattr(s, "cwd", ""), "account": acc, "project": proj,
                  "reason": "command_only", "pages_removed": n_pages,
                  "sidechain": side_note, "sidechain_to": side_to})
        moved.append((path, target))


def prune_gone_sources(entries: dict, scanned_roots) -> tuple:
    """把「來源檔已經不在了、而且它就在本次掃過的來源根底下」的 entry 拿掉。

    回傳 `(留下來的 entries, 拿掉幾筆)`。

    ⚠⚠ **這是書籤管理頁反查表（`BX_SESS`）唯一的對帳點。** 不做的話，明確來源中
    **已刪除的 session 會永久留在表裡**（跨模型 Medium#5）：任何縮範圍旗標都讓
    `filtering=True` ⇒ 整份舊 manifest 原封不動被沿用，而縮範圍模式又不清孤兒檔
    ⇒ 舊 HTML 還躺在磁碟上 ⇒ fallback 每次都把它烤回去。實測：以明確來源建兩場、
    刪掉其中一份 JSONL、再用同一個來源重建，程式仍回報共 2 場。

    ⚠ **只對本次真的掃過的根做。** 範圍外的來源可能只是這次沒指定、或磁碟沒掛上，
    在那裡「檔案不存在」**不代表「被刪掉了」**——那正是 `filtering` 當初保守的理由，
    這裡不推翻它，只是把「我這次確實看過那個目錄」這件事用上。

    ⚠ 對不上就保留（`relative_to` 丟 ValueError、路徑解析不出來、key 形狀不認得），
    **失敗方向一律是保守的**：寧可留著一筆過期的，也不要誤刪還在用的。

    範圍限制 SCOPE-BOOKMARK-PRUNED-ORPHAN-FILES：本函式只對帳 manifest 紀錄，
    **不刪磁碟上已經產出來的檔**；縮範圍模式的孤兒清理範圍見 `main()` 尾端那段。
    詳見 planning/scope-limits.md。
    """
    roots = []
    for r in scanned_roots:
        try:
            roots.append(Path(r).resolve())
        except OSError:
            pass
    if not roots:
        return entries, 0
    kept, gone = {}, 0
    for key, ent in entries.items():
        src = key.split(":", 1)[1] if ":" in key else ""   # key＝`<來源類型>:<解析過的路徑>`
        p, under = Path(src), False
        # ⚠ 用 `relative_to` ＋ try/except，不用 `is_relative_to`（那是 3.9+）：
        #   README 只保證「Python 3 標準函式庫」，這裡不值得為一個布林值訂下版本門檻。
        for root in roots:
            try:
                p.relative_to(root)
                under = True
                break
            except ValueError:
                pass
        if under and not p.exists():
            gone += 1
            continue
        kept[key] = ent
    return kept, gone


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


def claude_source_items(claude_source_args, account_arg):
    """解析出的 [(標籤 or None, projects目錄)]，**未經 realpath 去重**。

    兩種消費方式對「同一實體被多個來源指到」的需求相反，所以拆成兩段：
    - **掃 session**（`collect_sources`）要去重：junction/symlink 指到同一實體時重複掃是白工，
      同一場還會被計兩次。
    - **掃 `history.jsonl`**（切帳號偵測）要保留全部：`projects/` 常是各帳號 junction 到同一
      實體，但 `history.jsonl` 是各帳號各一份、沒有共用。跟著去重就只讀得到其中一份，
      「同一 sessionId 出現在兩個帳號」永遠比不出來。"""
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
    return items


def collect_sources(claude_source_args, account_arg):
    """回傳 [(標籤, projects目錄)]，指到同一實體的來源只留一個。
    通用方式：--claude-source 指定一個或多個輸入目錄（可寫「標籤=路徑」）；
    便利捷徑：--account 名稱 對應 ~/.claude[-名稱]/projects；
    皆未指定時：自動偵測 ~/.claude* 下所有含 projects 的目錄。"""
    items = _dedupe_by_realpath(claude_source_items(claude_source_args, account_arg))
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
# 回合標頭：AI 那方是「🤖 <名>」（Claude／Codex…），故 who 放寬成 🤖 後接非空白名（見 ai_name/render_turn_md）。
# ⚠ 耐久錨點 `{#k…Z}` 接在 `{#tN}` **後面**，所以這條一定要吃得下它，否則整份 .md 切不出
#   任何回合、搜尋結果頁一則都不會有（`test_search` 的「命中詞高亮」就是在守這一格）。
#   它是 optional：既有的 .md 是舊 renderer 產的、沒有那一段，重建之前兩種都要認得。
_TURN_HEAD_RE = re.compile(r"^### (?P<side>↳ )?(?P<who>👤 You|🤖 \S+) ·(?P<rest>.*)\{#(?P<a>[ts]\d+)\}"
                           r"(?: \{#(?P<k>k[0-9A-Za-z-]+)\})?\s*$")
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
    # ⚠ 內文**尾端一律 rstrip**。回合之間插進來的東西（目前是切帳號分隔線）除了自己那一行，
    # 前後還會帶空行，那些會落進**前**一則的內文。與其讓這裡去認得每一種插入物的形狀
    # （認漏一種就是一個缺陷，而形狀會隨渲染端改動），不如直接不讓尾端空白有意義——
    # 命中位置不可能落在尾端空白裡，片段本來也會把空白壓平，所以砍掉不損失任何東西。
    cur, buf = None, []
    for line in md_text.splitlines():
        m = _TURN_HEAD_RE.match(line)
        if m:
            if cur:
                yield (*cur, "\n".join(buf).rstrip())
            tm = _TIME_RE.search(m.group("rest") or "")
            cur = (m.group("a"), bool(m.group("side")), m.group("who"), tm.group(0) if tm else "")
            buf = []
        elif cur is not None:
            if is_md_acct_sep(line):
                continue      # 回合**之間**的切帳號分隔線：不屬於前一則，也不屬於後一則
            buf.append(line)
    if cur:
        yield (*cur, "\n".join(buf).rstrip())


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
    # ⚠⚠ 本工具唯一會動到 `out/` 以外檔案的功能，**只有明確給了這個參數才會發生**。
    # 不給＝完全不會碰任何來源檔（連檢查都不做）。
    ap.add_argument("--archive-command-only", default=None, metavar="目錄",
                    help="把「只有指令、沒有對話」的 Claude session 檔搬到該目錄"
                         "（檔名＝cwd＋原檔名），並刪掉它產生的頁面。"
                         "⚠ 那些 session 會從 `claude --resume` 的清單消失；"
                         "每一筆都記在該目錄的 _archived.jsonl，可依它還原")
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

    # 封存「只有指令、沒有對話」的 session。⚠ **只有明確給了參數才會發生**——
    # 沒給就連判定都不做，一個來源檔都不會被讀第二次。
    if args.archive_command_only:
        _roots = [root for _, root in accounts] + [root for _, root in codex_accounts]
        _cand = []
        for kind, acc, proj, sf in files:
            if kind != SOURCE_CLAUDE:
                continue
            try:
                _s = load_session(sf, proj, acc, SOURCE_CLAUDE)
                analyze(_s)
            except Exception as e:
                print(f"  ! 封存判定跳過（讀不起來）{sf.name}: {e}", file=sys.stderr)
                continue
            _cand.append((kind, acc, proj, sf, _s))
        _moved, _skipped = archive_command_only(
            _cand, Path(args.archive_command_only), _roots, out)
        _gone = {p for p, _ in _moved}
        files = [f for f in files if f[3] not in _gone]
        print(f"封存「只有指令、沒有對話」的 session：搬走 {len(_moved)} 場 → "
              f"{Path(args.archive_command_only)}")
        for why, n in sorted(_skipped.items()):
            print(f"  (略過) {why}：{n}")
        # ⚠⚠ **「什麼都沒發生」和「一場都沒搬成」在輸出上必須長得不一樣**（教訓 57）。
        # 前者是「本來就沒有符合條件的」，後者是「全部撞牆了」——後者要能一眼看出來。
        if not _moved and _skipped:
            print(f"  ⚠ 一場都沒搬成：{sum(_skipped.values())} 場全部落在上面的略過原因裡。"
                  "這不是「沒有東西需要封存」。", file=sys.stderr)
        elif not _moved:
            print("  （沒有符合條件的 session，什麼都沒有動。）")
        if _moved:
            print(f"  紀錄在 {Path(args.archive_command_only) / ARCHIVE_MANIFEST}"
                  "（含原始絕對路徑，可依它還原）")
            print("  ⚠ 這些 session 已經不在 ~/.claude/projects/ 底下，"
                  "`claude --resume` 的清單不會再有它們。")
        # ⚠ 頁面已經由 `archive_command_only()` 逐場刪掉了（見那裡的說明：
        # `prune_gone_sources()` **只對帳 manifest、不刪磁碟上的檔**）。
    # 自願切帳號的偵測資料：各帳號 config 目錄下的 history.jsonl
    # health 一定要接住並顯示：切帳號歸因是選配資料源，「0 次切換」有三種完全不同的意思
    # （真的沒切過／這台只有一個帳號／history 讀不到或列格式變了）。函式填得出涵蓋率，
    # 呼叫端不接等於那份資料只存在函式裡面，使用者看到的仍然是一個沒有脈絡的 0。
    acct_health = {}
    # ⚠ 餵給 claude_config_dirs 的是**去重之前**的來源清單（`claude_source_items`），不是
    # `accounts`：後者經過 realpath 去重，多個 config 的 projects/ junction 到同一實體時
    # 只會剩一個，於是只讀得到一份 history.jsonl。理由見那兩個函式的 docstring。
    acct_switches = ({} if args.no_claude
                     else load_account_switches(
                         claude_config_dirs(
                             p for _, p in claude_source_items(args.claude_source, args.account)),
                         acct_health))
    print(f"Claude 帳號：{', '.join(a for a, _ in accounts) or '(無)'}")
    if acct_health.get("dirs"):
        print(f"切帳號偵測：掃 {acct_health['dirs']} 個 config／讀到 {acct_health.get('files', 0)} 份 "
              f"history（{acct_health.get('rows', 0)} 列，認不得 {acct_health.get('bad_rows', 0)}"
              + (f"，讀取失敗 {acct_health['read_errors']}" if acct_health.get("read_errors") else "")
              + f"）→ {acct_health.get('accounts', 0)} 個帳號、"
              f"{acct_health.get('switch_sessions', 0)} 場有切換")
    if codex_accounts:
        print(f"Codex 來源：{', '.join(a for a, _ in codex_accounts)}")
    if not files:
        print("沒有找到任何 session 檔。")
        # ⚠⚠ **零場也要對帳。** 舊寫法在這裡直接 `return`，於是
        #     「來源被刪到一場不剩」時整個對帳流程根本不會執行：舊 manifest、
        #     書籤管理頁的 `BX_SESS` 反查表、索引頁全部原封不動留著，
        #     連結指向已經不存在的檔。`prune_gone_sources` 位在這個 return 之後，
        #     所以它守的那件事在**最極端的那一格**反而失效。
        #     ⚠ `test_bookmark_deleted_source_pruned` 只做「兩場刪成一場」，
        #       所以這一格一直沒有素材走到（教訓 29）。
        #     守它的是 `tests/test_smoke.py::test_bookmark_all_sources_deleted`。
        scanned_roots = [root for _, root in accounts] + [root for _, root in codex_accounts]
        # ⚠ 一個來源根都沒掃到（`--no-claude --no-codex`、或來源設定錯）時**什麼都不要動**：
        #   那不是「東西被刪了」，是「這次沒有去看」。誤判的代價是清掉還在的紀錄。
        if not scanned_roots:
            return
        filtering = bool(args.project or args.claude_source or args.account or args.no_claude
                         or args.codex_source or args.no_codex)
        old_manifest, _ = load_manifest(out)
        new_entries = dict(old_manifest) if filtering else {}
        new_entries, n_gone = prune_gone_sources(new_entries, scanned_roots)
        if n_gone:
            print(f"  （來源已刪除，對掉 {n_gone} 筆舊紀錄）")
        save_manifest(out, new_entries)
        if want_html:
            # ⚠⚠ **索引頁要用對帳後的 `new_entries` 重畫，不可以寫死。**
            #     `new_entries` 在縮範圍時保留了範圍外的紀錄（正常路徑本來就會把它們
            #     一起畫出來），這裡卻曾經寫 `render_index_html([], False, False, False)`
            #     ⇒ manifest 說有、`BX_SESS` 也還指著、HTML 檔還在磁碟上，
            #     **只有索引頁說一場都沒有**。
            #     ⚠ 改之前這個分支是直接 `return`（索引不會被動到），所以那是「零場也要
            #       對帳」自己造出來的新缺陷——正是這條線上重複出現的那個樣態。
            #     ⚠⚠ **而第一次修的時候只換掉第一個參數**，第三、四個（要不要連到快取報告）
            #       還是寫死的 `False` ⇒ 報告檔還在磁碟上、索引卻不連它了。**同一個形狀，
            #       隔壁兩個參數。** 所以現在四個參數全部照正常路徑算一次。
            #     ⚠ rows 一律走 `_index_rows()`：兩條路各寫一份就會分岔（`empty` 的過濾
            #       第一次修的時候也漏了）。
            #     守它的是 `tests/test_smoke.py::test_zero_scan_keeps_out_of_scope_index`
            #     （列還在）與 `::test_zero_scan_keeps_cache_report_links`（連結還在）。
            zero_rows = _index_rows(new_entries, args.include_empty)
            zero_cache = build_cache_report(zero_rows, acct_health=acct_health)
            (out / "index.html").write_text(
                render_index_html(zero_rows,
                                  len({r.get("account", "") for r in zero_rows}) > 1,
                                  zero_cache["has_data"],
                                  build_codex_survival(zero_rows)["has_data"]),
                encoding="utf-8")
            bx_fallback, _ = prune_gone_sources(load_manifest_paths(out), scanned_roots)
            (sess_dir / "bookmarks.html").write_text(
                render_bookmarks_html([], bx_fallback, sess_dir), encoding="utf-8")
            (sess_dir / "settings.html").write_text(render_settings_html(), encoding="utf-8")
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
    # ⚠⚠ 沿用舊 manifest 不等於「舊的都還在」。本次真的掃過的來源根底下、檔案已經不在的，
    #     一律對掉——不做的話已刪除的 session 會**永久**留在書籤管理頁的反查表裡
    #     （跨模型 Medium#5）。判準與保守方向見 `prune_gone_sources` 的 docstring。
    scanned_roots = [root for _, root in accounts] + [root for _, root in codex_accounts]
    if filtering:
        new_entries, n_gone = prune_gone_sources(new_entries, scanned_roots)
        if n_gone:
            print(f"  （來源已刪除，對掉 {n_gone} 筆舊紀錄）")
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
    n_build = n_reuse = n_empty = n_broken = 0      # n_broken：壞掉被跳過的來源檔
    n_broken_kept = 0                              # 其中「沿用上一次輸出」的（見下方 except）
    for source_kind, acc_name, proj_name, sf in files:
        key = manifest_key(source_kind, sf)
        # Claude 的 transcript 檔名就是 sessionId，切帳號資料據此對應（Codex 無此資料）
        sig = session_signature(sf, acct_switches.get(sf.stem, ()))
        cached = manifest.get(key) or {}
        row = cached.get("row")
        if source_kind == SOURCE_CODEX and args.project and row and not row_matches_project(row, args.project):
            continue
        new_entries.pop(key, None)
        reusable = (
            cached.get("sig") == sig and row
            # ⚠⚠ 帳號標籤變了就**一定要重建**（跨模型 Medium#5 的 [推論] 那半）。
            #    輸出路徑是 `<來源>/<帳號>/<檔名>`，而 sig 只看檔案內容 ⇒ 同一份 JSONL
            #    換一個 `--claude-source 標籤=路徑` 重跑時，舊 row 會被沿用、頁面留在
            #    **舊的帳號目錄**下，索引與 BX_SESS 也跟著指到那裡。標籤是會改輸出位置
            #    的因素，就必須進 reusable 的判定。
            and row.get("account", "") == (acc_name or "")
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

        # ⚠⚠ **壞掉的那一場只有一個處理器。** 先前解析失敗與分析失敗各寫一份，於是
        # 「把上一次的 row 放回去」只做在分析那一半 ⇒ **解析失敗照樣會刪掉舊頁面**
        # （`qimg-fix-codex` Medium#1）。兩條路共用這一支，之後再長第三條也是。
        def _broken(kind, exc):
            nonlocal n_broken, n_broken_kept
            print(f"  ! {kind} {sf.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            n_broken += 1
            # ⚠⚠ **要先確認上一次的輸出真的還在磁碟上**。無條件回填的話，舊 row 指向的
            # 頁面若早就不見了（被清過、被手動刪過），索引就會多一條**指向不存在的檔**
            # 的連結——那和這一條原本要修的缺陷是同一種傷害，只是方向相反。
            row = (cached or {}).get("row") or {}
            if any(row.get(k) and (sess_dir / row[k]).exists() for k in ("out_html", "out_md")):
                new_entries[key] = cached
                n_broken_kept += 1

        try:
            if source_kind == SOURCE_CODEX:
                s = load_codex_session(sf, acc_name, codex_titles)
            else:
                s = load_session(sf, proj_name, acc_name, source_kind)
        except Exception as e:
            _broken("解析失敗", e)
            continue
        if source_kind == SOURCE_CODEX and not project_matches_filter(s, args.project):
            continue
        if not any(e.get("type") in ("user", "assistant") for e in s.events):
            continue
        # ⚠⚠ **一場壞掉不可以拖垮整批。** 這一層過去沒有接例外，於是任何一場分析出事
        # （例：`queued_command.prompt` 是 block 陣列時的 `AttributeError`）就整支停在
        # 那裡、**一個頁面都產不出來**——2026-09-01 同事回報時是 321 個來源檔全滅。
        # 形狀刻意和上面那段「解析失敗」同款：印一行到 stderr、跳過這一場、其餘照產。
        # ⚠ **不可以改成靜默**：這一行訊息加上收尾那個「壞掉跳過 N」是使用者唯一會知道
        #   「有東西沒被畫出來」的管道；吞掉的話就變成看不見的資料遺失。
        # ⚠ 例外類別也要印（`{type(e).__name__}`）：`AttributeError` 這種只印訊息時
        #   （「'list' object has no attribute 'strip'」）看不出是什麼錯。
        # ⚠ 這一段**包到渲染完為止**，不是只包 `analyze()`：宣稱的性質是「一場壞掉不可以
        #   拖垮整批」，而渲染層拿到的是同一份髒資料（本批新增的碼有一半在渲染層）。
        # ⚠⚠ **不放回上一次的 row 的話**：這一場不進 `rows` ⇒ `allowed` 不含它 ⇒ **孤兒清理
        # 會刪掉上一次建好的頁面**；而同一次執行寫 `bookmarks.html` 用的是磁碟上那份舊
        # manifest（`prune_gone_sources()` 只對掉「來源檔不見了」的，來源檔還在）⇒ 管理頁
        # 留下一列**指向剛剛被自己刪掉的檔案**的書籤：點下去是找不到檔案，而不是設計上
        # 要給的「⚠ 對不到檔案」。⚠ 沿用的是**上一次**的內容，所以收尾那行要講出來。
        try:
            analyze(s, acct_switches)
        except Exception as e:
            _broken("分析失敗", e)
            continue
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
        # ⚠⚠ **護欄要真的包到渲染。** 先前只包了 `analyze()`，而註解卻寫著「包到渲染完為止」
        # ——那是**只修一半、而且註解在說謊**（`qimg-fix-codex` Medium#8 讀碼抓到）。
        # 渲染層拿到的是同一份髒資料，而這條線新增的碼有一半在渲染層。
        try:
            if want_html:
                mem_link = ("../" * len(PureWindowsPath(s.out_html).parts) + s.mem_href) if s.mem_href else ""
                (sess_dir / s.out_html).write_text(
                    render_session_html(s, rel_index_href(s.out_html), mem_link), encoding="utf-8")
            if want_md:
                (sess_dir / s.out_md).write_text(render_session_md(s), encoding="utf-8")
            row = session_to_row(s)
        except Exception as e:
            _broken("渲染失敗", e)
            continue
        # has_html/has_md＝該檔與本 row 的 sig+renderer 同步。縮格式建置（--format md/html）時，
        # 另一格式若在「同一 sig」下產過且檔仍在，旗標沿用；sig 變了就不可信（過期檔）。
        prev = cached.get("row") if cached.get("sig") == sig else None
        row["has_html"] = want_html or bool(prev and prev.get("has_html")
                                            and (sess_dir / row["out_html"]).exists())
        row["has_md"] = want_md or bool(prev and prev.get("has_md")
                                        and (sess_dir / row["out_md"]).exists())
        new_entries[key] = {"sig": sig, "row": row}
        n_build += 1

    rows = _index_rows(new_entries, args.include_empty)
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
    cache_data = build_cache_report(rows, acct_health=acct_health)
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

    # Codex 快取存活統計：獨立於 Claude 報告（兩家 TTL 機制不同，資料層即分離，不混統計）
    codex_data = build_codex_survival(rows)
    codex_data["claude_report"] = has_report
    has_codex_report = codex_data["has_data"]
    if has_codex_report and want_html:
        (out / "cache-codex.html").write_text(render_codex_survival_html(codex_data), encoding="utf-8")
    if has_codex_report and want_md:
        (out / "cache-codex.md").write_text(render_codex_survival_md(codex_data), encoding="utf-8")
    if not has_codex_report:
        for stale in (out / "cache-codex.html", out / "cache-codex.md"):
            try:
                stale.unlink()
            except OSError:
                pass

    if want_html:
        (out / "index.html").write_text(render_index_html(rows, show_account, has_report, has_codex_report), encoding="utf-8")
        # 書籤管理頁與設定頁（第 3 期）。⚠ 放 `out/sessions/` **底下**，不是 `out/` 根：
        # 和寫入者（session 頁）同一棵子樹，把「目錄層級」這個變數整個消掉。
        # ⚠ 兩份和 index.html 一樣**每次都無條件重產**，不受 manifest 的版本閘管。
        # ⚠ 下面清孤兒檔那一段只掃 `rel.parts[0] in scanned_source_dirs`（來源夾），
        #   這兩個檔在 `sessions/` 的**第一層**、`parts[0]` 就是檔名本身 ⇒ 掃不到、不會被誤刪。
        #   守它的是 `test_bookmark_manage_pages` 的「重建兩次仍在」那一格。
        # ⚠⚠ fallback 讀的是**磁碟上那份舊 manifest**（`save_manifest` 這時還沒跑），
        #    所以它必須走**同一條對帳規則**——否則上面 `new_entries` 對掉的那幾筆會從
        #    這裡再被烤回反查表，同一個缺陷換一條路徑進來（跨模型 Medium#5）。
        #    守它的是 `test_bookmark_deleted_source_pruned`。
        bx_fallback, _ = prune_gone_sources(load_manifest_paths(out), scanned_roots)
        (sess_dir / "bookmarks.html").write_text(
            render_bookmarks_html(rows, bx_fallback, sess_dir), encoding="utf-8")
        (sess_dir / "settings.html").write_text(render_settings_html(), encoding="utf-8")
    elif want_md:
        # ⚠ 純 md 建置**不重產**這兩頁，留著就是一份**過期的 `BX_SESS`**：
        #   之後某場的檔名變了（專案改名）而該次是純 md 全量建置時，孤兒清理會刪掉舊
        #   `.html`、又不會產新的 ⇒ 管理頁生出一條指向**已刪檔案**的連結，
        #   而不是誠實的「⚠ 對不到檔案」。比照 `cache-report.*` 那段：不重產就刪掉。
        for stale in (sess_dir / "bookmarks.html", sess_dir / "settings.html"):
            try:
                stale.unlink()
            except OSError:
                pass
    if want_md:
        (out / "index.md").write_text(render_index_md(rows, show_account, has_report, has_codex_report), encoding="utf-8")

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
    if n_broken:
        # ⚠ 這一格是**資料遺失的公告**，不是統計：上面每一筆都印過 `!` 訊息，
        #   但那些會被幾百行輸出捲走，收尾這一行是使用者一定看得到的位置。
        # ⚠ 「沿用上一次的輸出」要講出來——那一頁的內容是舊的，而索引與書籤照樣指得到它。
        _kept = f"，其中 {n_broken_kept} 沿用上一次的輸出" if n_broken_kept else ""
        bits.append(f"壞掉跳過 {n_broken}{_kept}（見上面的 ! 訊息）")
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
        if has_codex_report:
            print(f"  Codex 快取存活： {(out / 'cache-codex.html').resolve()}")
    if args.open and want_html:
        try:
            webbrowser.open(index_path.as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
