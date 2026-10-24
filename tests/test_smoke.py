#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_session_viewer 的煙霧測試（不含任何真實對話）。

跑法：
    python tests/test_smoke.py          # 直接跑，成功印 OK
    pytest tests/                        # 或用 pytest

它在暫存目錄造一個假的 projects 結構，跑轉換器，檢查輸出涵蓋：
表格算繪、工具摺疊、圖片內嵌、/rename 標題、模型/token、不安全連結被移除、
回合錨點（HTML id ↔ MD {#tN} 標記）與 --search 全文搜尋結果頁。
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ai_session_viewer.py"

# ⚠⚠ **整套測試一律在假的家目錄下跑。** 工具就算給了明確的 `--claude-source`，切帳號偵測照樣會掃
#    `~/.claude*/history.jsonl`（`claude_config_dirs()`——那是刻意的：多帳號機器上各帳號的
#    `projects/` 常是同一個 junction，history 卻各一份）。不隔離的話，測試會讀到跑測試那台機器上
#    使用者**真的** history，結果還會隨機器而變。
#    指到一個不存在的目錄就夠了：工具對家目錄只讀不寫，glob 一個不存在的目錄就是零筆。
#    需要家目錄素材的那幾格自己另設 `HOME`／`USERPROFILE`。
os.environ["HOME"] = os.environ["USERPROFILE"] = str(
    Path(tempfile.gettempdir()) / f"asv-test-no-home-{os.getpid()}")


def new_tmp(tmp_path=None):
    """這一支測試要用的暫存目錄。給了 `tmp_path` 就用它，否則自己開一個。

    ⚠⚠ **為什麼不直接 `tempfile.mkdtemp()`**：在**沙箱化的 code review 環境**下
    （codex `workspace-write`），`mkdtemp()` 建出來的目錄**連根目錄都寫不進去**，
    更別說在裡面再建一層——而這個測試套件每一支都要建 `projects/<專案>/`。
    ⇒ 整套測試**一行斷言都跑不到**就 `PermissionError` 死掉，於是 reviewer
    **靜默降級成純讀碼、卻照樣寫出漂亮的 verdict**。

    ⭐ **成因（2026-08-23 實測）**：
    | 位置 | 建目錄 | 寫檔 |
    |---|---|---|
    | `mkdtemp()` 的根 | — | **✗ 存取被拒** |
    | `mkdtemp()` 底下再一層 | **✗** | ✗ |
    | runner 預建的 `%TMP%\\work\\` 底下（`Path.mkdir`）| **✓** | **✓**（連巢狀都可以）|

    ⚠⚠ **上面那張表是對的，但「事前就存在的目錄可以用」這個推論不成立**：
    `mkdtemp(dir=<那個目錄>)` **換了地點，沒換機制**，沙箱裡照樣整套死在
    `WinError 5`，只是改死在 `work\\tmpXXXX\\projects`。

    真正的規則是：**不可以是 `mkdtemp()` 建的目錄，不管它建在哪。**
    `mkdtemp()` 會鎖權限（Windows 上是受限的 DACL），沙箱的受限 token 進不去它建的那一層；
    表格第三列之所以成功，是因為那一列用的是 `Path.mkdir()`，不是 `mkdtemp()`。

    所以：設 `ASV_TEST_TMP` 指到一個**已經存在且可寫**的目錄，本函式就用 `Path.mkdir()`
    在它底下開一個唯一子目錄。沙箱環境下的 charter 只要加一行：

        $env:ASV_TEST_TMP = "$env:TMP\\work"

    ⚠ 沒設就照舊走 `mkdtemp()`——本機開發完全不受影響。

    ⚠⚠ **這件事在本機無法被否證**（沒有沙箱，兩條路都寫得進去），所以
    `test_tmp_base_env` 是用「**把 `mkdtemp` 換成會爆的東西，看它還活不活得下去**」
    在守，而不是用「建得出來嗎」在守。後者在本機不管結論對不對都是綠的。
    """
    if tmp_path:
        return Path(tmp_path)
    base = os.environ.get("ASV_TEST_TMP")
    if base:
        b = Path(base)
        # ⚠ 只在**它已經存在**時才用：不存在就代表使用者指錯了，
        # 這時候自己 mkdir 出來的目錄在沙箱下照樣寫不進去（那正是要避開的情況），
        # 而且會把「路徑打錯」變成一個更難查的權限錯誤。
        if b.is_dir():
            # ⚠ **不可以用 `mkdtemp(dir=b)`**——理由見上方 docstring。
            # `exist_ok=False` 是刻意的：撞號要當場看得見，不要靜靜共用同一個目錄
            # （兩支測試共用暫存目錄的症狀會表現成「另一支的產物污染我的斷言」）。
            for _ in range(8):
                cand = b / ("asv-" + uuid.uuid4().hex[:16])
                try:
                    cand.mkdir(parents=False, exist_ok=False)
                    return cand
                except FileExistsError:
                    continue
            raise RuntimeError("在 ASV_TEST_TMP 底下連續 8 次都撞號：" + str(b))
    return Path(tempfile.mkdtemp())


def _session_pages(out):
    """`out/sessions/` 底下的**對話頁**（不含管理頁／設定頁）。

    ⚠ 書籤第 3 期起，`out/sessions/` 的**第一層**多了兩個不是對話頁的檔：
    `bookmarks.html`（書籤管理頁）與 `settings.html`（設定頁）。
    直接用 `rglob("*.html")[0]` 會隨機挑到它們——形狀斷言就對著一個空殼在跑，
    而且**錯得很安靜**（那兩頁沒有 `.turn`，所有 `in html` 斷言一起變假）。
    對話頁一律落在 `<source>/<account>/` 底下 ⇒ 相對路徑至少三段。
    排序是為了讓 `[0]` 在多頁時是決定性的。
    """
    return sorted(p for p in (out / "sessions").rglob("*.html")
                  if len(p.relative_to(out / "sessions").parts) >= 3)


def _page_body(html):
    """把 `script`／`style` 的內容整段拿掉，只留真正的標記與可見文字。

    ⚠ **凡是「某個字串在頁面上的位置」這類斷言，都要先過這一層。**
    書籤第 2 期起，session 標題會**再出現一次**——內嵌成頁尾 JS 的 `BK_TITLE`。
    於是 `html.rindex(<標題裡的字>)` 會指到整頁最後面，
    `test_acct_separator_and_step_time` 那條「最後一則 INCRONE 在分隔線之前」就此永遠假。
    那不是產品缺陷，是**斷言掃到了不該掃的區域**。

    `script`／`style` 在 HTML 裡是 raw-text 元素，內容本來就不是標記。"""
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", html, flags=re.S | re.I)


def _page_dup_ids(html):
    """整頁重複的 HTML `id`（**最高不變量**：重複的話 getElementById 會靜默取第一個）。

    ⚠ **只掃標籤開頭上的 `id=`，不能對整份 HTML 做純文字 `findall`**
    ：渲染器把訊息內文的 `<`/`>` 跳脫成 `&lt;`/`&gt;`，
    但**雙引號原封不動**，於是使用者訊息裡的 `id="x"` 會以字面留在 HTML 裡。實測：
    一則內文含兩個 `id="FAKEDUP"` 的訊息，純文字掃描判定重複，
    瀏覽器 `querySelectorAll('[id=FAKEDUP]')` 卻是 0 個——**假陽性**。
    `[^<>]*?` 保證只在同一個標籤開頭之內配對。

    ⚠ **第二類假陽性：`script`／`style` 是 raw-text 元素。** 頁面 JS 會用字串組 HTML
    （書籤對話窗整塊都是），於是原始碼裡的 `'<h3 id="bkTitle">'` 長得就像一個標籤開頭，
    而它在 `bkOpen` 與 `bkPanel` 各出現一次 ⇒ 被判成重複的 id。**那兩處根本不是標籤**，
    掃之前要先把這兩種元素的內容整段拿掉。

    兩類都各有素材在守：純文字那類是 `test_durable_anchor` 的 `id="IDSCANFAKE"`，
    raw-text 那類是 `test_bookmark_ui`（那一頁的 JS 真的組了兩次 `id="bkTitle"`）。
    **少了任何一類的處理，對應那一支就會紅。**
    """
    ids = re.findall(r'<[a-zA-Z][^<>]*?\sid="([^"]*)"', _page_body(html))
    return sorted({i for i in ids if ids.count(i) > 1})


SID = "00000000-0000-4000-8000-000000000001"
# 1x1 透明 PNG
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

EVENTS = [
    {"type": "user", "uuid": "u1", "parentUuid": None,
     "timestamp": "2026-06-01T01:00:00.000Z", "cwd": "/home/demo/Demo/Proj",
     "gitBranch": "main", "version": "2.1.150", "sessionId": SID, "isSidechain": False,
     "message": {"role": "user", "content": "請做一個比較表格，然後讀取 README。"}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
     "timestamp": "2026-06-01T01:00:05.000Z", "sessionId": SID, "isSidechain": False,
     "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "msg_demo_1",
                 "usage": {"input_tokens": 1200, "output_tokens": 300},
                 "content": [
                     {"type": "thinking", "thinking": "先想表格內容。"},
                     {"type": "text", "text": "比較如下：\n\n| 項目 | A | B |\n|---|---|---|\n| 速度 | 快 | 慢 |\n\n我來讀 README。"},
                     {"type": "tool_use", "id": "tool_1", "name": "Read",
                      "input": {"file_path": "/home/demo/Demo/Proj/README.md"}}]}},
    {"type": "user", "uuid": "u2", "parentUuid": "a1",
     "timestamp": "2026-06-01T01:00:06.000Z", "sessionId": SID, "isSidechain": False,
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "tool_1", "content": "# Demo\n內容第一行"}]}},
    {"type": "assistant", "uuid": "a2", "parentUuid": "u2",
     "timestamp": "2026-06-01T01:00:10.000Z", "sessionId": SID, "isSidechain": False,
     "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "msg_demo_2",
                 "usage": {"input_tokens": 1500, "output_tokens": 120},
                 "content": [{"type": "text",
                              "text": "完成 ✅ 安全連結 [Anthropic](https://www.anthropic.com)，"
                                      "不安全 [x](javascript:alert(1)) 應被移除。"}]}},
    {"type": "user", "uuid": "u3", "parentUuid": "a2",
     "timestamp": "2026-06-01T01:00:12.000Z", "sessionId": SID, "isSidechain": False,
     "message": {"role": "user", "content": [
         {"type": "text", "text": "看這張圖："},
         {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}]}},
    {"type": "system", "subtype": "local_command", "uuid": "s1", "parentUuid": "u3",
     "timestamp": "2026-06-01T01:00:13.000Z", "sessionId": SID, "isSidechain": False,
     "content": "<command-name>/rename</command-name>\n<command-args>\"煙霧測試\"</command-args>"},
    {"type": "system", "subtype": "local_command", "uuid": "s2", "parentUuid": "s1",
     "timestamp": "2026-06-01T01:00:13.500Z", "sessionId": SID, "isSidechain": False,
     "content": "<local-command-stdout>Session renamed to: \"煙霧測試\"</local-command-stdout>"},
    {"type": "ai-title", "uuid": "t1", "timestamp": "2026-06-01T01:00:14.000Z",
     "sessionId": SID, "aiTitle": "Demo auto title"},
]


def _build_fixture(base: Path) -> Path:
    proj = base / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{SID}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in EVENTS), encoding="utf-8")
    return base / "projects"


def test_smoke(tmp_path=None):
    tmp = new_tmp(tmp_path)
    projects = _build_fixture(tmp)
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={projects}", "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出 {r.returncode}\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "煙霧測試" in index, "索引應顯示 /rename 名稱"
    assert "✎" in index, "renamed 應有 ✎ 標記"
    assert "idx_filter_v1" in index and "restoreF" in index, "索引應有篩選狀態記憶（localStorage 還原）"
    assert 'onclick="clearF()"' in index, "索引應有清除篩選按鈕"
    # 回索引時停在剛剛點的那一列：行為由 `python scripts/probe_bookmarks.py --back`（真 Chrome）驗；
    # 這裡只守「那一段有被寫進頁面、而且接在篩選上」。
    assert "idx_back_v1" in index and 'id="backmsg"' in index, "索引應有「回索引時停在那一列」"
    assert "saveF();backAf();}" in index, "每次篩完都要讓「被藏起來的那一列」有機會捲過去"

    htmls = list(_session_pages(out))
    assert htmls, "應產生 session HTML"
    assert htmls[0].relative_to(out / "sessions").parts[:2] == ("claude-code", "demo")
    html = htmls[0].read_text(encoding="utf-8")
    checks = {
        '<table class="md"': "表格未算繪",
        'details class="tool"': "工具未做成可摺疊",
        "data:image/png;base64": "圖片未內嵌",
        "opus-4-7": "未顯示模型",
        "快取": "未顯示快取/成本資訊",
        "煙霧測試": "session 頁標題應為 /rename 名稱",
        "Demo auto title": "應顯示自動標題作輔助",
        "closest('.sidechain-wrap')": "跳轉錨點應展開回合內收合區（略過子代理框）",
        "nextElementSibling": "跳轉 compact 分隔線應展開壓縮摘要",
    }
    for needle, msg in checks.items():
        assert needle in html, msg
    assert 'href="../../../index.html"' in html, "session 回索引路徑應配合工具/帳號 namespace"
    assert "↩ resume 約" in html and "% / 1.0M" in html, "session 頁應顯示 resume 量與 % / 視窗"
    assert ">resume</th>" in index, "索引應有 resume 欄"
    assert "sessions/claude-code/demo/" in index, "索引連結應包含工具/帳號 namespace"
    assert "javascript:alert" not in html, "不安全連結未被移除"

    # 回合錨點：HTML 每則有 id、MD 標頭有對應 {#tN} 標記
    assert 'id="t1"' in html, "HTML 第一則應有錨點 id=t1"
    md = htmls[0].with_suffix(".md").read_text(encoding="utf-8")
    assert "{#t1}" in md, "MD 回合標頭應有 {#t1} 錨點標記"
    print("OK: smoke test passed")


def test_search(tmp_path=None):
    # --search：搜既有輸出，結果頁含高亮與跳轉錨點；多詞 = 同一則內 AND
    tmp = new_tmp(tmp_path)
    projects = _build_fixture(tmp)
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={projects}", "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"轉換失敗\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    # 「比較表格」與「README」同在第一則使用者訊息 → 命中 t1
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), "--search", "比較表格 README"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"搜尋失敗\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    pages = sorted((out / "search").glob("*.html"))
    assert len(pages) == 1, f"應產生 1 個結果頁，實得 {len(pages)}"
    page = pages[0].read_text(encoding="utf-8")
    assert "<mark>" in page, "結果頁應有命中詞高亮"
    assert "#t1" in page, "結果應連到命中那一則的錨點"
    assert 'target="_blank"' in page and "../sessions/" in page, "結果應以相對路徑另開分頁"
    assert 'id="q"' in page, "結果頁應有頁內再過濾框"

    # 「比較表格」「移除」分屬不同則 → AND 不成立，0 命中（仍寫出結果頁、正常退出）
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), "--search", "比較表格 移除"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"零命中搜尋不應失敗\nSTDERR:{r.stderr}"
    assert "命中 0 則" in r.stdout, f"應回報 0 命中，實得：{r.stdout}"
    assert len(list((out / "search").glob("*.html"))) == 2, "零命中也應寫出結果頁"

    def _s(query, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--out", str(out), "--search", query, *extra],
            capture_output=True, text=True, encoding="utf-8")

    # 片語（引號、逐字）：連續文字命中；片語內空白不是 AND
    r = _s('"做一個比較表格"')
    assert r.returncode == 0 and "命中 0 則" not in r.stdout, f"片語應命中\n{r.stdout}{r.stderr}"
    r = _s('"表格 README"')          # 兩詞同一則但不相鄰 → 片語不命中（AND 的話會中）
    assert "命中 0 則" in r.stdout, "片語不應退化成 AND"
    r = _s('"比較如下： | 項目"')     # 原文中間隔著換行 → 片語內空白應可跨換行
    assert "命中 0 則" not in r.stdout, "片語內空白應比對跨換行"
    # OR：獨立大寫是運算子，任一命中即可
    r = _s("zzznope OR README")
    assert "命中 0 則" not in r.stdout, "OR 任一命中應成立"
    r = _s("zzznope OR zzznope2")
    assert "命中 0 則" in r.stdout, "OR 兩者皆無應 0 命中"
    r = _s("OR")                     # 只有懸空運算子 → 視同沒關鍵字
    assert r.returncode != 0 and "關鍵字" in r.stderr, "純 OR 應報缺關鍵字"
    # match-case：fixture 只有大寫 README
    r = _s("readme")
    assert "命中 0 則" not in r.stdout, "預設不分大小寫應命中"
    r = _s("readme", "--match-case")
    assert "命中 0 則" in r.stdout, "--match-case 應區分大小寫而不命中"

    # md-only 建置（--format md）：結果頁應退化連到 .md 並標示，不可連到不存在的 .html
    out2 = tmp / "out_mdonly"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={projects}", "--no-codex",
         "--out", str(out2), "--format", "md"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"md-only 轉換失敗\nSTDERR:{r.stderr}"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out2), "--search", "比較表格"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "命中 0 則" not in r.stdout, f"md-only 搜尋應命中\n{r.stdout}{r.stderr}"
    page2 = sorted((out2 / "search").glob("*.html"))[0].read_text(encoding="utf-8")
    assert re.search(r'href="\.\./sessions/[^"]+\.md"', page2), "md-only 應連到 .md"
    assert ".html#" not in page2, "md-only 不應連到不存在的 .html"
    assert "僅 .md" in page2 and "個無 .html" in page2, "md-only 應有標示與頁頂警告"

    # 過期輸出防護：來源變更後只重建其中一種格式，磁碟上另一種格式的「舊檔」不可再被信任
    def _append_event(text_mark, uuid, ts):
        with (projects / "demo-proj" / f"{SID}.jsonl").open("a", encoding="utf-8") as f:
            f.write("\n" + json.dumps(
                {"type": "user", "uuid": uuid, "parentUuid": "u3", "timestamp": ts,
                 "sessionId": SID, "isSidechain": False,
                 "message": {"role": "user", "content": text_mark}}, ensure_ascii=False))

    def _conv(dst, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"demo={projects}", "--no-codex",
             "--out", str(dst), *extra], capture_output=True, text=True, encoding="utf-8")

    # (a) both 建置 → 來源變更 → 只重建 md：舊 .html 雖存在但過期，搜尋應退化連 .md
    out3 = tmp / "out_stale_html"
    assert _conv(out3).returncode == 0
    _append_event("追加訊息MARKA。", "u8", "2026-06-01T01:00:20.000Z")
    assert _conv(out3, "--format", "md").returncode == 0
    assert list(_session_pages(out3)), "過期 .html 應仍在磁碟上（前提）"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out3), "--search", "比較表格"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "命中 0 則" not in r.stdout
    page3 = sorted((out3 / "search").glob("*.html"))[0].read_text(encoding="utf-8")
    assert ".html#" not in page3 and "僅 .md" in page3, "過期 .html 不應被連結，應退化連 .md"

    # (b) both 建置 → 來源變更 → 只重建 html：.md 語料過期，該 session 應跳過並警告
    out4 = tmp / "out_stale_md"
    assert _conv(out4).returncode == 0
    _append_event("追加訊息MARKB。", "u7", "2026-06-01T01:00:21.000Z")
    assert _conv(out4, "--format", "html").returncode == 0
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out4), "--search", "比較表格"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "命中 0 則" in r.stdout, "過期 .md 不可當語料，應 0 命中"
    assert "缺或過時" in r.stderr, "應警告有 session 因缺/過時 .md 未納入"

    # 沒建置紀錄時應明確報錯（非 0 退出）
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(tmp / "nowhere"), "--search", "x"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode != 0 and "先跑一次轉換" in r.stderr, "缺 manifest 應報錯提示先轉換"

    # 空白關鍵字應報錯，不可掉進正常轉換流程
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), "--search", "   "],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode != 0 and "關鍵字" in r.stderr, "空白 --search 應報錯而非執行轉換"
    assert "掃描到" not in r.stdout, "空白 --search 不應觸發轉換"
    print("OK: search test passed")


# 子代理就地呈現（A+B）：子代理對話應接在派出它的 Task 呼叫底下，且不另列索引
SID2 = "00000000-0000-4000-8000-000000000002"
TOOLU = "toolu_subagentdemo01"
MAIN_EVENTS = [
    {"type": "user", "uuid": "mu1", "parentUuid": None,
     "timestamp": "2026-06-02T01:00:00.000Z", "cwd": "/home/demo/Demo/Proj",
     "gitBranch": "main", "version": "2.1.150", "sessionId": SID2, "isSidechain": False,
     "message": {"role": "user", "content": "請派一個子代理去盤點。"}},
    {"type": "assistant", "uuid": "ma1", "parentUuid": "mu1",
     "timestamp": "2026-06-02T01:00:05.000Z", "sessionId": SID2, "isSidechain": False,
     "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "msg_main_1",
                 "usage": {"input_tokens": 800, "output_tokens": 60},
                 "content": [{"type": "text", "text": "我派一個子代理。"},
                             {"type": "tool_use", "id": TOOLU, "name": "Task",
                              "input": {"subagent_type": "Explore", "description": "盤點任務",
                                        "prompt": "你是盤點執行者。"}}]}},
    {"type": "user", "uuid": "mu2", "parentUuid": "ma1",
     "timestamp": "2026-06-02T01:00:30.000Z", "sessionId": SID2, "isSidechain": False,
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": TOOLU, "content": "子代理回報：完成盤點。"}]}},
    {"type": "assistant", "uuid": "ma2", "parentUuid": "mu2",
     "timestamp": "2026-06-02T01:00:35.000Z", "sessionId": SID2, "isSidechain": False,
     "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "msg_main_2",
                 "usage": {"input_tokens": 900, "output_tokens": 40},
                 "content": [{"type": "text", "text": "子代理已完成。"}]}},
]
SUB_EVENTS = [
    {"type": "user", "uuid": "su1", "parentUuid": None, "agentId": "demoagent01",
     "timestamp": "2026-06-02T01:00:10.000Z", "sessionId": SID2, "isSidechain": True,
     "message": {"role": "user", "content": "你是盤點執行者。"}},
    {"type": "assistant", "uuid": "sa1", "parentUuid": "su1", "agentId": "demoagent01",
     "timestamp": "2026-06-02T01:00:20.000Z", "sessionId": SID2, "isSidechain": True,
     "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "msg_sub_1",
                 "usage": {"input_tokens": 500, "output_tokens": 80},
                 "content": [{"type": "text", "text": "子代理思考中SUBAGENTMARKER完成。"}]}},
]


def test_subagent_inline(tmp_path=None):
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    sub = proj / SID2 / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (proj / f"{SID2}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in MAIN_EVENTS), encoding="utf-8")
    (sub / "agent-demoagent01.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in SUB_EVENTS), encoding="utf-8")
    (sub / "agent-demoagent01.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "盤點任務", "toolUseId": TOOLU}), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    html = [p for p in _session_pages(out)][0].read_text(encoding="utf-8")
    assert "SUBAGENTMARKER" in html, "子代理對話內容應出現在父 session 頁"
    assert 'class="sidechain-wrap"' in html, "應有子代理摺疊區"
    assert "子代理 Explore" in html and "盤點任務" in html, "摺疊標題應含子代理類型與描述"
    # A+B：子代理框應在 Task 呼叫之後、且在後續主對話之前（就地，不堆頁尾）
    pos_task = html.find("子代理回報")            # Task 呼叫的 tool_result 內文
    pos_sub = html.find('class="sidechain-wrap"')
    pos_final = html.find("子代理已完成")          # Task 之後的主對話
    assert 0 <= pos_task < pos_sub, "子代理應接在 Task 呼叫之後"
    assert pos_sub < pos_final, "子代理應就地呈現（在後續主對話之前），而非堆到頁尾"

    assert 'class="sub-toc"' in html, "session 頁應有頂部子代理跳轉清單"
    assert 'href="#sub-' in html and "openSub(" in html, "清單應有跳轉錨點與展開函式"
    assert f'id="{ "sub-" + TOOLU }"' in html, "就地子代理框應有對應錨點 id"

    index = (out / "index.html").read_text(encoding="utf-8")
    # ⚠ 用 `data-sid=` 當計數依據，不要綁「`<tr` 後面第一個屬性是什麼」——
    #   `<tr>` 最前面是 `data-sid`，寫 `'<tr data-source'` 的計數會是 0（實測）。
    #   每一列都有 `data-sid`，而它只出現在資料列上。
    rows = index.count("<tr data-sid=")
    assert rows == 1, f"子代理不應另列索引，應只有 1 列，實得 {rows}"
    assert "🧩 ×1" in index, "索引標題後應有子代理數量標示 🧩 ×1"

    # 子代理回合也有錨點（HTML id="sN" ↔ MD {#sN}），--search 命中子代理內容可直接跳
    assert re.search(r'id="s\d+"', html), "子代理回合應有 s 系錨點 id"
    md = [p for p in (out / "sessions").rglob("*.md")][0].read_text(encoding="utf-8")
    assert "{#s" in md, "MD 子代理回合標頭應有 {#sN} 錨點標記"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), "--search", "SUBAGENTMARKER"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"子代理搜尋失敗\nSTDERR:{r.stderr}"
    page = sorted((out / "search").glob("*.html"))[0].read_text(encoding="utf-8")
    assert re.search(r"#s\d+", page), "命中子代理內容應連到 s 系錨點"
    assert "子代理" in page, "結果頁應標示該筆來自子代理"
    print("OK: subagent inline test passed")


def test_day_divider(tmp_path=None):
    # 跨兩天的對話：應出現換日分隔線（含星期），且只在換日時出現
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-000000000003"
    evs = [
        {"type": "user", "uuid": "d1", "parentUuid": None, "timestamp": "2026-05-20T10:00:00.000Z",
         "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "第一天的訊息DAY1MARK。"}},
        {"type": "assistant", "uuid": "d2", "parentUuid": "d1", "timestamp": "2026-05-21T11:00:00.000Z",
         "sessionId": sid, "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "md3",
                                       "usage": {"input_tokens": 100, "output_tokens": 20},
                                       "content": [{"type": "text", "text": "第二天的回覆DAY2MARK。"}]}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = [p for p in _session_pages(out)][0].read_text(encoding="utf-8")
    assert 'class="day-sep"' in html, "跨日應有日期分隔線"
    # 兩天 -> 至少兩條分隔線（起始日 + 換日）
    assert html.count('class="day-sep"') >= 2, "起始日與換日各應有一條分隔線"
    assert "週" in html, "分隔線應含星期"
    # 每則時間 hover 顯示完整日期（title）
    assert 'class="when" title="2026-05-20' in html and 'class="when" title="2026-05-21' in html, \
        "每則時間應有完整日期 tooltip"
    print("OK: day divider test passed")


def test_compact_marker(tmp_path=None):
    # 含 /compact 摘要的 session：應插入壓縮分隔線，摘要收進摺疊（不直接攤在版面）
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-000000000004"
    evs = [
        {"type": "user", "uuid": "c1", "parentUuid": None, "timestamp": "2026-05-20T10:00:00.000Z",
         "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "壓縮前的提問BEFOREMARK。"}},
        {"type": "assistant", "uuid": "c2", "parentUuid": "c1", "timestamp": "2026-05-20T10:00:05.000Z",
         "sessionId": sid, "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mc2",
                                       "usage": {"input_tokens": 100, "output_tokens": 20},
                                       "content": [{"type": "text", "text": "壓縮前回覆。"}]}},
        {"type": "system", "uuid": "cb1", "parentUuid": None, "subtype": "compact_boundary",
         "timestamp": "2026-05-20T10:30:00.000Z", "sessionId": sid, "content": "Conversation compacted",
         "compactMetadata": {"trigger": "manual", "preTokens": 900000, "postTokens": 5000}},
        {"type": "user", "uuid": "c3", "parentUuid": "cb1", "timestamp": "2026-05-20T10:30:00.000Z",
         "isCompactSummary": True, "isVisibleInTranscriptOnly": True, "sessionId": sid,
         "message": {"role": "user", "content": "This session is being continued… 摘要內容SUMMARYMARK。"}},
        {"type": "user", "uuid": "c4", "parentUuid": "c3", "timestamp": "2026-05-20T10:31:00.000Z",
         "sessionId": sid, "message": {"role": "user", "content": "壓縮後的新提問AFTERMARK。"}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = [p for p in _session_pages(out)][0].read_text(encoding="utf-8")
    assert 'class="compact-sep"' in html, "應在 compact 點插入分隔線"
    assert "手動 /compact" in html, "分隔線應標示手動/自動（此例為手動）"
    assert "→" in html and "tokens" in html, "分隔線應顯示壓縮前→後 token"
    assert 'class="think compact-sum"' in html, "摘要應收進摺疊"
    # 壓縮點前後的真實對話照常呈現
    assert "BEFOREMARK" in html and "AFTERMARK" in html, "壓縮前後對話應照常呈現"
    assert "SUMMARYMARK" in html, "摘要內容應保留（在摺疊內）"
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "✂ ×1" in index, "索引標題後應有壓縮次數標示 ✂ ×1"

    # --- MD：壓縮摘要不畫成「👤 You」，標頭仍切得出一則（`--search` 靠它）---
    md = next((out / "sessions").rglob("*.md")).read_text(encoding="utf-8")
    heads = [ln for ln in md.splitlines() if ln.startswith("### ")]
    summ_head = [ln for ln in heads if "📋 壓縮摘要" in ln]
    assert len(summ_head) == 1 and "手動 /compact" in summ_head[0], f"MD 的摘要標頭不對：{heads}"
    assert sum("👤 You" in ln for ln in heads) == 2, f"真的使用者回合只有兩則：{heads}"
    sys.path.insert(0, str(SCRIPT.parent))
    import ai_session_viewer as _v
    chunks = list(_v.iter_turn_chunks(md))
    who = [c[2] for c in chunks if "SUMMARYMARK" in c[4]]
    assert who == ["📋 壓縮摘要"], f"搜尋切回合要把摘要切成自己的一則：{[(c[2], c[4][:20]) for c in chunks]}"
    print("OK: compact marker test passed")


def test_ai_md(tmp_path=None):
    """`--ai-md`：只處理一場，產給 AI 讀的精簡版＋完整版到 out/ai/，stdout 只印一行絕對路徑。

    精簡版的承諾：雙方講的話全留；**每一次工具呼叫恰好一行 `- 🔧 `**（略去的東西在原處標出來）；
    思考與計量不放；目錄的行號指得到那一則。"""
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "a1d00000-0000-4000-8000-0000000000a1"
    n = [0]

    def ev(role, content, sec, **kw):
        n[0] += 1
        e = {"type": role, "uuid": f"u{n[0]}", "timestamp": f"2026-06-10T01:{sec // 60:02d}:{sec % 60:02d}.000Z",
             "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.200", "sessionId": sid,
             "message": {"role": role, "content": content}}
        if role == "assistant":
            e["message"].update({"model": "claude-opus-4-8", "id": f"m{n[0]}",
                                 "usage": {"input_tokens": 10, "output_tokens": 5}})
        e.update(kw)
        return e

    def tool(sec, tid, name, inp):
        return ev("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": inp}], sec)

    def res(sec, tid, content, err=False):
        return ev("user", [{"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": err}], sec)

    read_body = "\n".join(f"READBODY{i}" for i in range(100))
    out50 = "\n".join(f"OUTLINE{i:02d}" for i in range(50))
    err60 = "\n".join(f"ERRLINE{i:02d}" for i in range(60))
    LS, PS, NEL = chr(0x2028), chr(0x2029), chr(0x85)      # `splitlines()` 也算斷行的字元
    evs = [
        ev("user", "第一個問題QFIRST。\n\n### 使用者自己的標題\n\n```bash\n# fence 裡的註解\n### 👤 You · 假的回合\n```\n"
                   "- 🔧 假的工具行" + LS + "- 🔧 LS假的工具行" + LS + "### LS假的回合" + NEL + "- 🔧 NEL假的工具行"
                   # 不是 fence 的開頭（` 的資訊字串帶 `）：不可以替它「補關」——補的那一行才會開出真的 fence
                   "\n```bad`info", 0,
           gitBranch="main\n```\nBRANCHFENCE"),           # 檔頭的分支取第一則的：原文開出的 fence 不可以吞掉整份
        ev("assistant", [{"type": "thinking", "thinking": "THINKSECRET"},
                         {"type": "text", "text": "好，我來看。"}], 2),
        tool(3, "t1", "Read", {"file_path": "/x/Proj/a.py", "extra\n- 🔧 KEY_FAKE": "x"}), res(4, "t1", read_body),
        tool(5, "t2", "Write", {"file_path": "/x/Proj/b.py", "content": "\n".join(["WRITEBODY"] * 50)}),
        res(6, "t2", "ok"),
        tool(7, "t3", "MultiEdit", {"file_path": "/x/Proj/a.py", "edits": [
            {"old_string": "OLDLINE", "new_string": "NEWLINE", "replace_all": True}]}),
        res(8, "t3", "ok"),
        tool(9, "t4", "Bash", {"command": "pytest -q", "description": "跑測試DESCMARK", "timeout": 600}),
        res(10, "t4", out50 + "\n- 🔧 輸出裡假的工具行\r- 🔧 CR假的工具行\r### 👤 You · CR假的回合"),
        tool(11, "t5", "Bash", {"command": "make build"}), res(12, "t5", err60, err=True),
        tool(13, "t6", "Grep", {"pattern": "foo"}), res(14, "t6", "a\nb\nc"),
        tool(15, "t7", "Task", {"subagent_type": "Explore", "description": "找東西\nDESCLINE2",
                                "prompt": "PROMPTBODY " * 20}),
        res(16, "t7", [{"type": "text", "text": "子代理的報告REPORTMARK\n- 🔧 報告裡假的工具行\r- 🔧 報告CR假的工具行\n"
                                                # 沒關的 fence 用 ~~~~：後面的 ``` 與待辦裡的 ~~~ 都關不掉它，不包起來就一路吞到檔尾
                                                + PS + "- 🔧 輸出PS假的工具行\n~~~~python\n沒關的 fence"}]),
        tool(17, "t8", "AskUserQuestion", {"questions": [{"question": "要哪一個QASK？"}]}),
        res(18, "t8", "使用者選了ANSWERMARK" + "答" * 9000 + "ANSWEREND"),
        tool(19, "t9", "TodoWrite", {"todos": [
            # 項目帶 ~~~（不包起來就一路吞到檔尾）與 ```（包起來的 fence 要比它長）
            {"content": "做完TODOMARK\n~~~\n待辦的第二行\n```", "status": "in_progress", "activeForm": "正在做ACTIVEFORM"},
            {"content": "取消的TODO", "status": "cancelled"}, "RAWTODO",
            {"content": "保留  兩個空白EXACT", "status": "custom  status"}]}),
        res(20, "t9", "ok"),
        tool(21, "t10", "Bash", {"command": "sleep 999"}),            # 沒有結果：中斷
        tool(22, "t11", "Bash", {"command": "printf '%s' 'a  b'", "cmd": "CMDALIAS"}), res(23, "t11", "a  b"),
        tool(24, "t12", "Edit", {"file_path": "/x/Proj/c.py", "edits": [{"old_string": "E_OLD", "new_string": "E_NEW"}],
                                 "old_string": "TOPOLD", "new_string": "TOPNEW"}), res(25, "t12", "done  twice"),
        tool(26, "t13", "NotebookRead", "STRINPUT_NB"), res(27, "t13", "nb"),
        tool(28, "t14", "Grep", {"pattern": "a  b"}), res(29, "t14", "x"),
        tool(30, "t15", "NotebookEdit", {"notebook_path": "/x/n.ipynb", "new_source": "NB1\nNB2\nNB3"}),
        res(31, "t15", "ok"),
        tool(32, "t16", "TaskCreate", {"subject": "SUBJ  TWO", "description": "做某事DESC"}), res(33, "t16", "ok"),
        {"type": "system", "uuid": "cb1", "parentUuid": None, "subtype": "compact_boundary",
         "timestamp": "2026-06-10T01:30:00.000Z", "sessionId": sid, "content": "Conversation compacted",
         "compactMetadata": {"trigger": "auto", "preTokens": 900000, "postTokens": 5000}},
        ev("user", "This session is being continued… SUMMARYMARK", 1800, isCompactSummary=True,
           isVisibleInTranscriptOnly=True),
        ev("user", "<command-name>/effort</command-name>\n<command-message>effort</command-message>\n"
                   "<command-args>high\n- 🔧 指令參數假的工具行</command-args>", 1805),
        # 沒關的 fence；最後那一行後面接了字，不是結尾 ⇒ 仍然沒關，要補關
        ev("user", "壓縮後的問題QAFTER。\r- 🔧 CR訊息假的工具行\n```text\n沒關的訊息 fence\n``` 不是結尾", 1810),
        ev("assistant", [{"type": "text", "text": "## 助手的標題\n收到。"}], 1812),
    ]
    n_tools = 16
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")

    def run(*extra):
        return subprocess.run([sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
                               "--no-codex", *extra], capture_output=True, text=True, encoding="utf-8")

    # --- 1. 只印一行路徑；只寫 out/ai/，不碰建置的紀錄 ---
    out = tmp / "out"
    r = run("--ai-md", sid[:8], "--out", str(out))
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    lines = r.stdout.splitlines()
    assert len(lines) == 1, f"stdout 只能有一行路徑，實得 {lines}"
    ai_path = Path(lines[0])
    assert ai_path.is_absolute() and ai_path.name.endswith(".ai.md") and ai_path.exists(), lines[0]
    full_path = ai_path.with_name(ai_path.name[:-len(".ai.md")] + ".md")
    assert full_path.exists(), "預設兩者都產：完整版要在精簡版旁邊"
    assert ai_path.parent.parent.parent == out.resolve() / "ai", f"要放在 out/ai/<來源>/<帳號>/，實得 {ai_path}"
    assert not (out / "sessions").exists() and not (out / "index.html").exists() \
        and not (out / ".build-manifest.json").exists(), "不可以碰增量建置的輸出與紀錄"
    md = ai_path.read_text(encoding="utf-8")
    full = full_path.read_text(encoding="utf-8")

    # --- 2. 每一次工具呼叫恰好一行，略去的都有標記 ---
    tool_lines = [ln for ln in md.splitlines() if ln.startswith("- 🔧 ")]
    assert len(tool_lines) == n_tools, f"工具行數要等於工具呼叫數 {n_tools}，實得 {len(tool_lines)}：{tool_lines}"
    assert f"共 {n_tools} 次" in md, "檔頭要講工具操作共幾次"
    assert "READBODY" not in md and "內容略（100 行）" in md, "讀檔內容不留、要標出略了幾行"
    assert "WRITEBODY" not in md and "寫入 50 行，內容略" in md, "寫檔內容不留、要標出來"
    assert "- OLDLINE" in md and "+ NEWLINE" in md, "改檔要留 diff"
    assert "OUTLINE49" in md and "OUTLINE37" not in md and "OUTLINE38" in md, "指令輸出只留最後 15 行"
    assert "共 53 行，只留最後 15 行" in md, "截掉的輸出要標出來"
    assert "ERRLINE59" in md and "ERRLINE20" in md and "ERRLINE19" not in md, "出錯的留最後 40 行"
    assert "結果 3 行，略" in md, "搜尋結果略去要標出來"
    assert "REPORTMARK" in md and "ANSWERMARK" in md and "QASK" in md, "子代理報告、提問與回答要全留"
    assert "沒有結果：中斷或未完成" in md, "沒有結果的呼叫要講出來"
    # --- 3. 不放的東西 ---
    assert "THINKSECRET" not in md and "THINKSECRET" in full, "思考區塊只在完整版"
    assert "⚡" not in md and "~$" not in md, "計量不放"
    # --- 4. 標題降級（fence 裡的不動）、壓縮摘要、待辦清單 ---
    assert "\\### 使用者自己的標題" in md and "\\## 助手的標題" in md, "訊息裡的標題要降級"
    assert "# fence 裡的註解" in md and "\\# fence 裡的註解" not in md, "fence 裡的 # 不動"
    assert "SUMMARYMARK" in md and "### 📋 前情摘要 · " in md, "壓縮摘要要在、畫成前情摘要"
    assert "TODOMARK" in md.split("## 目錄")[0], "最後一份待辦清單放進檔頭"
    assert str(full_path) in md, "檔頭要有完整版的路徑"
    assert "QFIRST" in md and "QAFTER" in md and "好，我來看。" in md, "雙方的話全留"
    # --- 5. 目錄的行號指到那一則；「從第 N 行讀起」指到最後一則前情摘要 ---
    body = md.splitlines()
    toc = re.findall(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) · 第 (\d+) 行 · (\S+)", md, re.M)
    assert len(toc) == 4, f"目錄要有三則使用者回合（含一則指令列）＋一則前情摘要，實得 {toc}"
    for when, ln, icon in toc:
        target = body[int(ln) - 1]
        want = "### 📋 前情摘要 · " if icon == "📋" else "### 👤 You · "
        assert target.startswith(want) and when in target, f"第 {ln} 行應是 {want}{when}，實得 {target!r}"
    m = re.search(r"從第 (\d+) 行（最後一則前情摘要）", md)
    assert m and body[int(m.group(1)) - 1].startswith("### 📋 前情摘要 · "), "接續的起點要指到前情摘要"
    heads = [ln for ln in body if ln.startswith("### ")]
    assert len(heads) == 6, f"`### ` 開頭的只有回合標頭（6 則），實得 {heads}"

    # --- 5b. 原文關不住的 fence 不可以吞掉後面的回合；工具那一行與回合標頭不可以被原文偽造 ---
    def scan_fences(lines):
        """照 CommonMark 認 fence（開頭：0–3 空白、3 個以上 ` 或 ~，` 的資訊字串不可以有 `；結尾：同一種字元、
        不短於開頭、後面只有空白）。回傳 (fence 外的行, 結尾時還開著的 fence)。"""
        outs, fence = [], None
        for ln in lines:
            if fence is None:
                m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", ln)
                if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                    fence = m.group(1)
                    continue
                outs.append(ln)
            else:
                m = re.match(r"^ {0,3}(`{3,}|~{3,})[ \t]*$", ln)
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                    fence = None
        return outs, fence
    outs, left = scan_fences(body)
    outside = [ln for ln in outs if ln.startswith("### ")]
    assert outside == heads and left is None, (
        f"每一個回合標頭都要在 fence 外面（原文沒關的 fence 不可以吞掉後面）：{outside}，最後還開著 {left!r}")
    assert "\\- 🔧 輸出裡假的工具行" in md and "\\- 🔧 報告裡假的工具行" in md and "\\- 🔧 假的工具行" in md, \
        "原文裡 `- 🔧 ` 開頭的行要加反斜線"
    assert "\\### 👤 You · 假的回合" in md, "fence 裡像回合標頭的行也要加反斜線"
    # --- 5c. 沒畫出來的輸入與結果也要標：Edit／Write 的結果、Task 的指示 ---
    line_of = {ln.split(" · ")[0][len("- 🔧 "):]: ln for ln in tool_lines}
    assert "結果：ok" in line_of["MultiEdit"] and "結果：ok" in line_of["Write"], f"短的結果照列：{line_of}"
    assert "edits[0].replace_all=true" in line_of["MultiEdit"], f"diff 畫不出的巢狀欄位要標：{line_of['MultiEdit']}"
    assert "description 略（" in line_of["Task"], f"只畫了第一行的欄位要標出來：{line_of['Task']}"
    assert "KEY_FAKE" in md, "欄位名也照列（但不可以造出一行工具行，見工具行數）"
    assert "prompt 略（" in line_of["Task"] and "PROMPTBODY" not in md, f"沒畫的輸入要標出來：{line_of['Task']}"
    assert any("description=跑測試DESCMARK" in ln and "timeout=600" in ln for ln in tool_lines), (
        f"指令那一支只畫了 command，其餘參數要接在那一行：{tool_lines}")
    assert ("\\- 🔧 CR假的工具行" in md and "\\### 👤 You · CR假的回合" in md
            and "\\- 🔧 CR訊息假的工具行" in md and "\\- 🔧 報告CR假的工具行" in md), "原文裡用 CR 斷行的也要擋（先統一換行再擋）"
    assert "指令參數假的工具行" in md and not any(ln.startswith("- 🔧 指令參數") for ln in body), (
        "指令列的參數也要擋保留的行首")
    # --- 5d. 本檔的斷行只有 `\n`：U+2028／U+2029／U+0085 也要先換成 `\n` 再擋（5 節的行號是用 splitlines 驗的）---
    assert ("\\- 🔧 LS假的工具行" in md and "\\### LS假的回合" in md and "\\- 🔧 NEL假的工具行" in md
            and "\\- 🔧 輸出PS假的工具行" in md), "其他斷行字元也要先換成 \\n 再擋保留的行首"
    assert not any(ch in md for ch in (LS, PS, NEL, "\r")), "本檔的斷行只有 \\n"

    def tl(prefix):
        return next(ln for ln in tool_lines if ln.startswith("- 🔧 " + prefix))
    # --- 5e. 算「畫出來了」的值要經得起那一行的空白壓縮；同一件事有兩個欄位時只算用到的那一個 ---
    assert "printf '%s' 'a  b'" in md, "空白被壓縮改掉的指令要原樣留在「輸入」那一段"
    assert "cmd=CMDALIAS" in tl("Bash · printf"), f"command 與 cmd 都有時，沒用到的那一個要標：{tl('Bash · printf')}"
    ed = tl("Edit · /x/Proj/c.py")
    assert "- E_OLD" in md and "old_string=TOPOLD" in ed and "new_string=TOPNEW" in ed, f"有 edits 時頂層的新舊字串要標：{ed}"
    assert "結果 1 行，略" in ed, f"短的結果經不起空白壓縮就算略：{ed}"
    assert "輸入=STRINPUT_NB" in tl("NotebookRead"), f"輸入不是物件也要交代：{tl('NotebookRead')}"
    assert "pattern 略（4 字）" in tl("Grep · a b"), f"空白被壓縮改掉的欄位不算畫了：{tl('Grep · a b')}"
    assert "寫入 3 行，內容略" in tl("NotebookEdit"), f"寫入的行數照用到的那個欄位算：{tl('NotebookEdit')}"
    # --- 5f. 待辦清單：照原值進 fence、沒畫的欄位彙總標、不是物件的項目照列；提問與回答全留；檔頭的原文一行、字不少 ---
    assert "todos[*].activeForm 略（1 項）" in line_of["TodoWrite"], f"待辦項目裡沒畫的欄位要標：{line_of['TodoWrite']}"
    assert "- [cancelled] 取消的TODO" in md and "- [?] RAWTODO" in md, "不認得的狀態照原值寫、不是物件的項目也要列"
    assert "- [custom  status] 保留  兩個空白EXACT" in md, "待辦的文字與狀態照原值（空白一個都不動）"
    tc = tl("TaskCreate")
    assert "subject=SUBJ  TWO" in md and "description=做某事DESC" in tc, f"TaskCreate 的欄位照原值、沒畫的要標：{tc}"
    assert "ANSWEREND" in md, "使用者的回答全留（不截）"
    assert json.dumps("main\n```\nBRANCHFENCE", ensure_ascii=False) in md.split("## 目錄")[0], (
        "檔頭的分支有斷行就寫成 JSON 字串（一行、字一個不少）")

    # --- 6. 只產一種 ---
    r2 = run("--ai-md", sid, "--ai-md-kind", "full", "--out", str(tmp / "out2"))
    p2 = r2.stdout.strip()
    assert r2.returncode == 0 and p2.endswith(".md") and not p2.endswith(".ai.md"), r2.stdout + r2.stderr
    assert not list((tmp / "out2").rglob("*.ai.md")), "只要完整版就不產精簡版"
    r3 = run("--ai-md", sid, "--ai-md-kind", "compact", "--out", str(tmp / "out3"))
    assert r3.returncode == 0 and r3.stdout.strip().endswith(".ai.md"), r3.stdout + r3.stderr
    assert len(list((tmp / "out3").rglob("*.md"))) == 1, "只要精簡版就只產一份"
    assert "這一次沒有產生完整版" in Path(r3.stdout.strip()).read_text(encoding="utf-8")

    # --- 7. 找那一場：id 開頭、Codex 的結尾 8 碼、last 跳過正在寫的、太短與不唯一 ---
    sys.path.insert(0, str(SCRIPT.parent))
    import ai_session_viewer as v
    d = tmp / "pick"
    d.mkdir(exist_ok=True)
    fa, fb = d / "aaaa1111-0000-4000-8000-000000000001.jsonl", d / "aaaa2222-0000-4000-8000-000000000002.jsonl"
    fc = d / "rollout-2026-06-10T01-00-00-019f0003-0000-7000-8000-0000c0dec0de.jsonl"
    for f in (fa, fb, fc):
        f.write_text("{}", encoding="utf-8")
    files = [(v.SOURCE_CLAUDE, "demo", "p", fa), (v.SOURCE_CLAUDE, "demo", "p", fb),
             (v.SOURCE_CODEX, "demo", "codex", fc)]
    now = 2_000_000_000
    os.utime(fa, (now - 100, now - 100))
    os.utime(fb, (now - 5, now - 5))
    os.utime(fc, (now - 50, now - 50))
    # Codex 的子代理執行緒（`session_meta.source` 是 dict）比 fc 新，`last` 也不可以挑它
    fs = d / "rollout-2026-06-10T01-00-30-019f0004-0000-7000-8000-00000000005b.jsonl"
    fs.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "019f0004-0000-7000-8000-00000000005b", "cwd": "/x/Other", "source": {"subagent": {}}}}), encoding="utf-8")
    os.utime(fs, (now - 40, now - 40))
    files.append((v.SOURCE_CODEX, "demo", "codex", fs))
    # `--project` 對 Codex：看 session_meta 的 cwd
    fc.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "019f0003-0000-7000-8000-0000c0dec0de", "cwd": "/x/Wanted"}}), encoding="utf-8")
    os.utime(fc, (now - 50, now - 50))
    assert v.codex_file_matches_project(fc, "wanted") and not v.codex_file_matches_project(fs, "wanted"), \
        "Codex 的 --project 要照 session_meta 的 cwd 篩"
    def pick(ident):
        try:
            return v.pick_ai_md_target(files, ident)[0][3]
        except SystemExit as e:        # 找不到／不唯一是 SystemExit：轉成斷言才看得到是哪一格
            return f"SystemExit: {e}"
    assert pick("aaaa1111") == fa, "認 id 的開頭"
    assert pick("c0dec0de") == fc, "Codex 要認輸出檔名裡那 8 碼（id 的結尾）"
    assert pick("019f0003") == fc, "Codex 也認 id 的開頭"
    # --- 7b. Codex 認的是 session_meta 的 id（載入器與輸出檔名用的就是它），不是檔名 ---
    fr = d / "renamed-by-hand.jsonl"
    fr.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "019f0008-0000-7000-8000-0000000000d8", "cwd": "/x/R"}}), encoding="utf-8")
    fw = d / "rollout-2026-06-10T01-00-00-019f0009-0000-7000-8000-0000000000e9.jsonl"
    fw.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "019f000a-0000-7000-8000-0000000000fa", "cwd": "/x/W"}}), encoding="utf-8")
    # 檔名本身就是另一個 id（整個檔名相等）：也不可以繞過內容的 id
    fx = d / "019fcccc-0000-7000-8000-0000000000aa.jsonl"
    fx.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "019fdddd-0000-7000-8000-0000000000bb", "cwd": "/x/X"}}), encoding="utf-8")
    files2 = files + [(v.SOURCE_CODEX, "demo", "codex", fr), (v.SOURCE_CODEX, "demo", "codex", fw),
                      (v.SOURCE_CODEX, "demo", "codex", fx)]

    def pick2(ident):
        try:
            return v.pick_ai_md_target(files2, ident)[0][3]
        except SystemExit as e:
            return f"SystemExit: {e}"
    assert pick2("019f0008") == fr and pick2("000000d8") == fr, "改過名的檔要照 session_meta 的 id 找得到"
    assert pick2("019f000a") == fw and str(pick2("019f0009")).startswith("SystemExit"), "檔名上的 id 不算數"
    assert pick2("019fdddd") == fx and str(pick2(fx.stem)).startswith("SystemExit"), (
        "整個檔名相等也不可以繞過內容的 id")
    # --- 7c. fence 外的原文（背景通知的摘要、指令列的參數）開不出 fence；插話整則一起降級、沒關的 fence 補關 ---
    def open_fence(text):
        return scan_fences(text.split("\n"))[1]
    for blk, mark in (({"type": "_notify", "status": "completed", "summary": "通知\n```\nNOTIFYFENCE"}, "NOTIFYFENCE"),
                      ({"type": "_command", "name": "/x", "args": "第一行\n```\nCMDFENCE"}, "CMDFENCE")):
        txt = "\n\n".join(v._ai_turn_blocks({"role": "user", "blocks": [blk]}, {}))
        assert mark in txt and open_fence(txt) is None, f"{blk['type']} 的原文不可以開出 fence：{txt!r}"
    ij = "\n\n".join(v._ai_turn_blocks({"role": "user", "blocks": [
        {"type": "_interject", "text": "插話\n```\n# 註解INTERJ"}]}, {}))
    assert "> # 註解INTERJ" in ij and ij.splitlines()[-1] == "> ```", f"插話整則一起降級、沒關的 fence 要補關：{ij!r}"
    # --- 7d. MultiEdit 的 edits 裡不是物件的那一筆（完整版 MD 的渲染器不收這種形狀，所以不放進上面的端到端）---
    me = v.render_tool_ai_md({"name": "MultiEdit", "id": "me1", "input": {"file_path": "/p", "edits": [
        {"old_string": "a", "new_string": "b"}, "BADEDIT"]}}, {}).split("\n")[0]
    assert "edits[1]=BADEDIT" in me, f"不是物件的那一筆也要標：{me}"
    # 單行、但超過摘要截斷長度的 description：摘要只畫了前段，不算畫了
    tk = v.render_tool_ai_md({"name": "Task", "id": "tk1", "input": {"subagent_type": "E", "description": "長" * 80}},
                             {}).split("\n")[0]
    assert "description 略（80 字）" in tk, f"只畫第一行而且被截到的欄位要標：{tk}"
    # content 與 new_source 都在：只算用到的那一個，另一個要標
    wa = v.render_tool_ai_md({"name": "Write", "id": "w1", "input": {"file_path": "/p/w", "content": "",
                                                                     "new_source": "A\nB"}}, {}).split("\n")[0]
    assert "寫入 2 行" in wa and "content=" in wa, f"兩個別名都在時，沒用到的那一個要標：{wa}"
    # --- 7e. 檔頭的完整版路徑（使用者給的 --out，POSIX 上可以帶換行與 fence）：一行、字不少，開不出 fence ---
    s7 = v.load_session(proj / f"{sid}.jsonl", "demo-proj", "demo")
    v.analyze(s7)
    hostile = "/tmp/out\n```\n換行的路徑/x.md"
    md7 = v.render_session_ai_md(s7, hostile)
    b7 = md7.splitlines()
    for _, ln in re.findall(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) · 第 (\d+) 行 · ", md7, re.M):
        assert b7[int(ln) - 1].startswith("### "), f"第 {ln} 行應是回合標頭，實得 {b7[int(ln) - 1]!r}"
    outs7, left7 = scan_fences(b7)
    assert json.dumps(hostile, ensure_ascii=False) in md7 and left7 is None and (
        [x for x in outs7 if x.startswith("### ")] == [x for x in b7 if x.startswith("### ")]), (
        "檔頭的路徑有斷行就寫成 JSON 字串，不可以開出 fence")
    # `json.dumps` 不跳脫 U+2028／U+2029／U+0085，而整份檔之後會把它們換成 \n ⇒ 那一行要自己寫成 \uXXXX
    for sep in (LS, PS, NEL):
        hostile_u = f"/tmp/out{sep}```{sep}換行的路徑/x.md"
        md7u = v.render_session_ai_md(s7, hostile_u)
        b7u = md7u.splitlines()
        outs7u, left7u = scan_fences(b7u)
        line7u = [x for x in b7u if x.startswith("> 完整版：")]
        assert (left7u is None and len(line7u) == 1 and f"\\u{ord(sep):04x}" in line7u[0]
                and json.loads(line7u[0][len("> 完整版：`"):-1]) == hostile_u
                and [x for x in outs7u if x.startswith("### ")] == [x for x in b7u if x.startswith("### ")]), (
            f"JSON 字串裡的 U+{ord(sep):04X} 也要跳脫，不可以斷行、開出 fence：{line7u}")
    # --- 7f. 標題切在 code span 中間：H1 補上同長度的反引號；結尾講明紀錄到這裡為止 ---
    s7.title = "請修" + "字" * 80 + " ``abc`de"
    h1 = v.render_session_ai_md(s7).split("\n")[0]
    assert h1 == "# " + s7.title + "``", f"沒關的 code span 要補上同長度的反引號：{h1!r}"
    for raw, want in (("a `b", "a `b`"), ("a `b`", "a `b`"), ("a \\`b", "a \\`b"), ("a `b\\", "a `b\\`"),
                      ("a ``", "a `` ``"), ("`a` ``b", "`a` ``b``")):
        assert v._ai_close_code_span(raw) == want, f"{raw!r} ⇒ {v._ai_close_code_span(raw)!r}，應為 {want!r}"
    assert md.rstrip("\n").split("\n")[-1].startswith("> **紀錄到這裡為止**（最後一筆 "), (
        f"精簡版的結尾要講明紀錄到這裡為止：{md.rstrip().splitlines()[-1]!r}")
    got, notes = v.pick_ai_md_target(files, "last", now=now)
    assert got[3] == fc and any("aaaa2222" in x for x in notes), f"last 要跳過 30 秒內還在寫的：{got[3].name} {notes}"
    def broken(it):
        if it[3] == fc:
            raise ValueError("BROKENMARK")
        return True
    got_b, notes_b = v.pick_ai_md_target(files, "last", now=now, eligible=broken)
    assert got_b[3] == fa and any("BROKENMARK" in x for x in notes_b), (
        f"讀不起來的那一場不可以安靜地被跳過：{got_b[3].name} {notes_b}")
    try:
        v.pick_ai_md_target(files, "c0dec0de", eligible=broken)
    except SystemExit as e:
        assert "BROKENMARK" in str(e) and "讀不起來" in str(e), f"指定 id 時要講是讀不起來：{e}"
    else:
        raise AssertionError("指定的那一場讀不起來要報錯")
    for bad in ("aaaa", "aa", "zzzz9999"):
        try:
            v.pick_ai_md_target(files, bad)
        except SystemExit as e:
            assert str(e), "錯誤要有訊息"
        else:
            raise AssertionError(f"{bad!r} 應該失敗（不唯一、太短或找不到）")

    # --- 8. `--project` 對 Codex 在挑那一場之前就要篩（建置是載入後才篩，這條路要自己篩）---
    cx = tmp / "cx" / "2026" / "06" / "10"
    cx.mkdir(parents=True, exist_ok=True)

    def rollout(sid, cwd, mt, later_cwd=None):
        f = cx / f"rollout-2026-06-10T01-00-00-{sid}.jsonl"
        f.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
            {"timestamp": "2026-06-10T01:00:00.000Z", "type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
            {"timestamp": "2026-06-10T01:00:00.500Z", "type": "turn_context",
             "payload": {"cwd": later_cwd or cwd, "model": "gpt-5.5"}},
            {"timestamp": "2026-06-10T01:00:01.000Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": f"在 {cwd}"}},
            {"timestamp": "2026-06-10T01:00:02.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好"}]}}]),
            encoding="utf-8")
        os.utime(f, (mt, mt))
    t_now = time.time()
    rollout("019f0005-0000-7000-8000-0000000000a5", "/x/Wanted", t_now - 300)
    rollout("019f0006-0000-7000-8000-0000000000b6", "/x/Other", t_now - 100)
    # 開頭那一筆說 Wanted、後面的 turn_context 換成 Other：建置把它算成 Other，這裡也要
    rollout("019f0007-0000-7000-8000-0000000000c7", "/x/Wanted", t_now - 60, later_cwd="/x/Other")
    r8 = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'cx'}", "--no-claude",
                         "--project", "Wanted", "--ai-md", "last", "--out", str(tmp / "out8")],
                        capture_output=True, text=True, encoding="utf-8")
    assert r8.returncode == 0, r8.stderr
    assert "__Wanted__" in r8.stdout and "0000a5" in r8.stdout, f"--project 篩掉的那一場不可以被挑中：{r8.stdout}"
    # --- 8b. 檔裡不只一筆 session_meta、後面那筆換了 id：挑的時候只看開頭，載入後對不上就要停，不可以寫出別場 ---
    fm2 = cx / "rollout-2026-06-10T01-00-00-019f000b-0000-7000-8000-0000000000bb.jsonl"
    fm2.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        {"timestamp": "2026-06-10T01:00:00.000Z", "type": "session_meta",
         "payload": {"id": "019f000b-0000-7000-8000-0000000000bb", "cwd": "/x/M"}},
        {"timestamp": "2026-06-10T01:00:00.100Z", "type": "session_meta",
         "payload": {"id": "019f000c-0000-7000-8000-0000000000cc", "cwd": "/x/M"}},
        {"timestamp": "2026-06-10T01:00:00.500Z", "type": "turn_context", "payload": {"cwd": "/x/M", "model": "gpt-5.5"}},
        {"timestamp": "2026-06-10T01:00:01.000Z", "type": "event_msg", "payload": {"type": "user_message", "message": "嗨"}},
        {"timestamp": "2026-06-10T01:00:02.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好"}]}}]),
        encoding="utf-8")
    os.utime(fm2, (t_now - 1000, t_now - 1000))
    r9 = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'cx'}", "--no-claude",
                         "--ai-md", "019f000b", "--out", str(tmp / "out9")],
                        capture_output=True, text=True, encoding="utf-8")
    assert r9.returncode != 0 and "對不上" in r9.stderr and not list((tmp / "out9").rglob("*.md")), (
        f"載入後的 session id 對不上指定的 id 要停：{r9.stdout}{r9.stderr}")
    # 同一種檔，檔名就是開頭那一筆的 id、用整個檔名指定：載入後的核對也不可以拿檔名放行
    id_d = "019f000d-0000-7000-8000-0000000000dd"
    fm3 = cx / f"{id_d}.jsonl"
    fm3.write_text(fm2.read_text(encoding="utf-8").replace("019f000b-0000-7000-8000-0000000000bb", id_d)
                   .replace("019f000c-0000-7000-8000-0000000000cc", "019f000e-0000-7000-8000-0000000000ee"),
                   encoding="utf-8")
    os.utime(fm3, (t_now - 1000, t_now - 1000))
    r10 = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'cx'}", "--no-claude",
                          "--ai-md", id_d, "--out", str(tmp / "out10")],
                         capture_output=True, text=True, encoding="utf-8")
    assert r10.returncode != 0 and "對不上" in r10.stderr and not list((tmp / "out10").rglob("*.md")), (
        f"整個檔名相等也不可以放過載入後對不上的 id：{r10.stdout}{r10.stderr}")
    print("OK: ai md test passed")


def test_cache_report(tmp_path=None):
    # 快取分析報告 v2：TTL 細分計價（1h=2×）、成因分解（first/switch/expiry/evict）、
    # 429 邊界自動偵測（切帳號不污染 TTL 統計）、TTL 遵約率、SVG 圖表與 md twin。
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-000000000005"

    def astep(uuid, parent, ts, mid, inp, cc5, cc1h, cr):
        return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
                "sessionId": sid, "isSidechain": False,
                "message": {"role": "assistant", "model": "claude-opus-4-7", "id": mid,
                            "usage": {"input_tokens": inp, "output_tokens": 0,
                                      "cache_creation_input_tokens": cc5 + cc1h,
                                      "cache_read_input_tokens": cr,
                                      "cache_creation": {"ephemeral_5m_input_tokens": cc5,
                                                         "ephemeral_1h_input_tokens": cc1h}},
                            "content": [{"type": "text", "text": "ok"}]}}

    evs = [
        {"type": "user", "uuid": "q1", "parentUuid": None, "timestamp": "2026-06-03T00:59:00.000Z",
         "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "快取測試CACHEMARK。"}},
        # A：session 第一句（cold，成因 first）；1h 寫入 100 萬 → 成本 2×＝$10
        astep("a1", "q1", "2026-06-03T01:00:00.000Z", "mm1", 0, 0, 1_000_000, 0),
        # B：+10 分，warm（帶內命中 → 遵約樣本）
        astep("a2", "a1", "2026-06-03T01:10:00.000Z", "mm2", 100, 0, 10_000, 1_000_000),
        # 429 limit（撞牆 → 其後第一步歸因「切帳號」，且不進 TTL 統計）
        {"type": "assistant", "uuid": "e1", "parentUuid": "a2", "timestamp": "2026-06-03T01:20:00.000Z",
         "sessionId": sid, "isSidechain": False, "isApiErrorMessage": True, "apiErrorStatus": 429,
         "message": {"role": "assistant", "model": "<synthetic>", "id": "mmE",
                     "content": [{"type": "text",
                                  "text": "You've hit your session limit · resets 6pm (Asia/Taipei)"}]}},
        # C：limit 後 +5 分（cold，成因 switch）
        astep("a3", "e1", "2026-06-03T01:25:00.000Z", "mm3", 0, 0, 1_000_000, 0),
        # D：+2 小時（cold，成因 expiry＝可避免；亦為閒置 ≥30 分的重暖事件）
        astep("a4", "a3", "2026-06-03T03:25:00.000Z", "mm4", 0, 0, 500_000, 0),
        # E：+10 分（cold，帶內 → 成因 evict＝提早失效；遵約樣本 miss）
        astep("a5", "a4", "2026-06-03T03:35:00.000Z", "mm5", 0, 0, 1_000_000, 1_000),
        # F：+90 秒，warm、寫 1h TTL——邊界前最後一次寫入刻意用 1h：若 boundary 未重置 lineage，
        # G→H 會沿用 stale 1h cohort 而把 9 分 cold 誤判 evict＋誤進遵約樣本（下方三個斷言同時卡住）
        astep("a6", "a5", "2026-06-03T03:36:30.000Z", "mm6", 0, 0, 10_000, 1_000_000),
        # system 形態 401（重試記錄帶 error.status → auth 邊界；其後第一步歸因 switch）
        {"type": "system", "subtype": "api_error", "uuid": "e2", "parentUuid": "a6",
         "timestamp": "2026-06-03T03:36:35.000Z", "sessionId": sid, "isSidechain": False,
         "level": "error", "retryAttempt": 1, "maxRetries": 10, "retryInMs": 1000,
         "error": {"status": 401, "headers": {}}},
        # G：401 後 +90 秒（cold → switch，不是回合內雜訊；本步無寫入）
        astep("a7", "e2", "2026-06-03T03:38:00.000Z", "mm7", 200_000, 0, 0, 0),
        # H：+9 分（cold；401 邊界已重置快取 lineage、G 又無寫入 → cohort=unknown（按 5 分下界）
        #    → 9 分＝expiry，不是 evict、也不進 1h 遵約樣本——cohort 後備＋lineage 重置回歸）
        astep("a8", "a7", "2026-06-03T03:47:00.000Z", "mm8", 0, 10_000, 0, 0),
        # I：+2 分，warm（H 寫過 5m → 同 lineage 內的 5m cohort 樣本；存活表出現 5m 欄，
        #    H 的 5m 寫入亦覆蓋成本 1.25× 路徑）
        astep("a9", "a8", "2026-06-03T03:49:00.000Z", "mm9", 0, 0, 0, 1_000_000),
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "快取分析報告" in index, "索引應連到快取分析報告"
    assert "cache-hypotheses.html" in index, "索引應連到快取假說檢定頁"
    # 成本按 TTL 細分計價（1h=2×、5m=1.25×）：A$10 + B$0.6005 + C$10 + D$5 + E$10.0005
    # + F$0.60 + G$1 + H$0.0625 + I$0.50 ＝ $37.7635 ≈ $37.76（全按 5 分假設會低估）
    assert "$37.76" in index, "成本應按 TTL 細分計價（找不到 $37.76）"

    rep = (out / "cache-report.html").read_text(encoding="utf-8")
    checks = {
        "TTL 遵約率": "KPI 應有 TTL 遵約率",
        ">50%<": "遵約率應為 1/2＝50%（A→B 命中、D→E 失效）",
        "limit/切帳號": "成因表應含 limit/切帳號",
        "閒置過期": "成因表應含閒置過期",
        "提早失效": "應含提早失效（evict）",
        'svg class="viz"': "應有 SVG 圖表（存活曲線/dot plot）",
        "cz-switch": "成因堆疊圖應有切帳號段",
        "撞到 limit": "應有撞牆時刻小節",
        "CI ": "比率應附 Wilson CI",
    }
    for needle, msg in checks.items():
        assert needle in rep, f"{msg}（找不到 {needle!r}）"
    assert "429" in rep and "污染" not in rep[:200], "撞牆說明應提及 429"
    # 拆頁：假說檢定（時段/脈絡）移到 cache-hypotheses，健檢頁只留連結
    assert "尖峰" not in rep, "時段假說應已移出健檢頁"
    assert "cache-hypotheses.html" in rep, "健檢頁應連到假說頁"
    hyp = (out / "cache-hypotheses.html").read_text(encoding="utf-8")
    hchecks = {
        "伺服器時段": "假說①（尖峰時段）應在假說頁",
        "累積脈絡": "假說②（context 大小）應在假說頁",
        "cache-report.html": "假說頁應連回健檢頁",
        "樣本不足": "小樣本應走「樣本不足」路徑而非硬出 verdict",
        "附帶觀察": "應有失效形態機制線索（帶內有 evict）",
        'svg class="viz"': "假說頁應有 dot plot",
        "🔺": "UTC 尖峰參考帶標示應在假說頁",
    }
    for needle, msg in hchecks.items():
        assert needle in hyp, f"{msg}（找不到 {needle!r}）"

    md = (out / "cache-report.md").read_text(encoding="utf-8")
    assert "| limit/切帳號 | 2 |" in md, "md 成因表 switch 應為 2（429 assistant 形態＋401 system 形態）"
    assert "| 閒置過期 | 2 |" in md, "md 成因表 expiry 應為 2（D＝1h 過期、H＝邊界後 unknown lineage 過期）"
    assert "| 提早失效 | 1 |" in md, "md 成因表 evict 應為 1（H 不得因沿用邊界前 1h cohort 而誤算）"
    assert "| session 第一句 | 1 |" in md, "md 成因表 first 應為 1"
    assert "| 回合內雜訊 | 0 |" in md, "md 成因表 intra 應為 0（G 屬 401 邊界，非雜訊）"
    assert "TTL 遵約率" in md and "50%" in md, "md 應有遵約率 50%（H 不得進 1h 遵約樣本）"
    assert "5 分寫入" in md, "md 存活表應出現 5m cohort 欄"
    hmd = (out / "cache-hypotheses.md").read_text(encoding="utf-8")
    assert "## ① 伺服器時段" in hmd and "## ② 累積脈絡" in hmd, "md 假說頁應含兩個假說小節"
    assert "| ≥ 500k | 2 | 50%（CI 9–91%） |" in hmd, \
        "ctx 分箱：A→B 與 D→E 的前步脈絡皆落 ≥500k、失效 1/2（Wilson CI 9–91%）"
    assert "殘餘命中中位數 1.0k" in hmd, "機制線索：E 失效時殘餘 cache_read=1000"
    assert "| UTC 時 |" not in md and "尖峰" not in md, "UTC 表應已移出健檢 md"
    # 直接卡 lineage 重置：G→H（540 秒）必須落在「TTL 未知」cohort 的 5–10 分桶
    # （欄序＝1h、5m、未知各兩欄；沿用 stale 1h 或 5m cohort 都會使此列不符）
    assert "| 5–10 分 | — | — | — | — | 1 |" in md, "H 應落在 unknown lineage 的 5–10 分桶"
    print("OK: cache report test passed")


def test_codex_step_badges(tmp_path=None):
    # Codex 逐步快取徽章：token_count 掛在該次呼叫的第一筆事件＝步驟起點；
    # 冷啟步驟有紅標與 ❄最低；重播的 token_count 去重；孤兒 token_count（無任何事件前）安全丟棄。
    tmp = new_tmp(tmp_path)
    sess_dir = tmp / "sessions" / "2026" / "06" / "05"
    sess_dir.mkdir(parents=True, exist_ok=True)
    csid = "019f0000-0000-7000-8000-000000000001"

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-06-05T01:00:{sec:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    def tc(sec, inp, cached, out):
        return line(sec, "event_msg", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                                  "reasoning_output_tokens": 0, "total_tokens": 0},
            "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                 "output_tokens": out, "reasoning_output_tokens": 0,
                                 "total_tokens": inp + out},
            "model_context_window": 272000}})

    rows = [
        line(0, "session_meta", {"id": csid, "cwd": "/x/CodexProj",
                                 "cli_version": "0.29.0", "git": {"branch": "main"}}),
        line(1, "turn_context", {"cwd": "/x/CodexProj", "model": "gpt-5.5"}),
        tc(2, 500, 0, 0),                                   # 孤兒：無任何事件可掛 → 丟棄不計
        line(3, "event_msg", {"type": "user_message", "message": "幫我改一個東西CODEXQMARK。"}),
        line(4, "response_item", {"type": "reasoning",
                                  "summary": [{"type": "summary_text", "text": "想想怎麼做CODEXTHINK。"}]}),
        line(5, "response_item", {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "我先看檔案。"}]}),
        tc(6, 2000, 0, 10),                                 # 步驟1：0%（冷啟，掛在 reasoning 事件）
        line(7, "response_item", {"type": "function_call", "name": "shell_command",
                                  "arguments": "{\"command\":[\"ls\"]}", "call_id": "call_demo_1"}),
        tc(8, 2200, 2000, 20),                              # 步驟2：91%
        line(9, "response_item", {"type": "function_call_output", "call_id": "call_demo_1",
                                  "output": "{\"output\":\"README.md\"}"}),
        line(10, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "完成CODEXDONE。"}]}),
        tc(11, 2500, 2400, 30),                             # 步驟3：96%
        tc(12, 2500, 2400, 30),                             # 重播（同簽章、其間無事件）→ 去重不重計
    ]
    (sess_dir / f"rollout-2026-06-05T01-00-00-{csid}.jsonl").write_text(
        "\n".join(rows), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'sessions'}",
         "--no-claude", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    htmls = list(_session_pages(out))
    assert htmls, "應產生 Codex session HTML"
    html = htmls[0].read_text(encoding="utf-8")
    assert "CODEXQMARK" in html and "CODEXDONE" in html, "對話內容應照常呈現"
    # 三步都應有步驟分隔列（含時刻）；步驟1 冷啟紅標；❄最低徽章指向步驟1
    STEP_T = "步驟 %d · [0-9]{2}:[0-9]{2}:[0-9]{2}</span>"
    assert re.search(STEP_T % 1, html) and re.search(STEP_T % 3, html), (
        "多步回合應有逐步分隔列（含時刻）")
    assert re.search(STEP_T % 1 + '<span class="meter m-cache cold"', html), (
        "冷啟步驟應紅標（m-cache cold）")
    assert "❄最低 0%·步驟1" in html, "回合徽章應標出最低步驟"
    # 總帳：孤兒丟棄、重播去重 → total_in=6700、cache_read=4400 → 66%
    #（孤兒未丟會成 61%；重播重計會成 74%）
    assert "⚡快取 66%" in html, "session 頁命中率應為 66%（孤兒丟棄＋重播去重）"
    assert "gpt-5.5" in html, "應顯示模型"
    print("OK: codex step badges test passed")


def test_codex_same_minute_outputs(tmp_path=None):
    """Codex 同一分鐘、同一專案開的幾場各自一個輸出檔（檔名的 id 段取 sid 最後 8 碼）。

    Codex 的 id 是 UUIDv7：前 8 碼是時間戳，同一分鐘開的幾場前 8 碼相同。取前 8 碼的話三樣
    （時間到分鐘、專案、id 段）全同 ⇒ 後寫的蓋掉先寫的，索引兩列連到同一頁、書籤反查表也是。
    另一半：真的撞到時（最後 8 碼也相同）要出聲，不可以安靜地只留一場。
    """
    tmp = new_tmp(tmp_path)
    sess_dir = tmp / "sessions" / "2026" / "06" / "07"
    sess_dir.mkdir(parents=True, exist_ok=True)

    def write(sid, sec, mark):
        def line(s, typ, payload):
            return json.dumps({"timestamp": f"2026-06-07T02:00:{s:02d}.000Z", "type": typ,
                               "payload": payload}, ensure_ascii=False)
        (sess_dir / f"rollout-2026-06-07T02-00-{sec:02d}-{sid}.jsonl").write_text("\n".join([
            line(sec, "session_meta", {"id": sid, "cwd": "/x/Same", "cli_version": "0.29.0"}),
            line(sec + 1, "turn_context", {"cwd": "/x/Same", "model": "gpt-5.5"}),
            line(sec + 2, "event_msg", {"type": "user_message", "message": f"問題{mark}。"}),
            line(sec + 3, "response_item", {"type": "message", "role": "assistant",
                                            "content": [{"type": "output_text", "text": "好。"}]}),
        ]), encoding="utf-8")

    # 前 8 碼相同（同一個毫秒時間戳的高位）、最後 8 碼不同
    sid_a = "019f0002-0000-7000-8000-00000a1a1a1a"
    sid_b = "019f0002-0001-7000-8000-00000b2b2b2b"
    write(sid_a, 0, "AAAMARK")
    write(sid_b, 10, "BBBMARK")

    def build(out):
        r = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'sessions'}",
                            "--no-claude", "--out", str(out)],
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
        return r

    # --- 1. 前 8 碼相同、最後 8 碼不同：兩場各一個檔，索引與書籤反查表各指各的 ---
    out = tmp / "out"
    r = build(out)
    pages = _session_pages(out)
    assert len(pages) == 2, f"兩場應有兩個對話頁，實得 {[p.name for p in pages]}"
    by_sid = {sid: [p for p in pages if p.name.endswith(f"__{sid[-8:]}.html")] for sid in (sid_a, sid_b)}
    assert all(len(v) == 1 for v in by_sid.values()), (
        f"Codex 檔名的 id 段應是 sid 最後 8 碼，實得 {[p.name for p in pages]}")
    assert "AAAMARK" in by_sid[sid_a][0].read_text(encoding="utf-8"), "A 的頁面應是 A 的內容"
    assert "BBBMARK" in by_sid[sid_b][0].read_text(encoding="utf-8"), "B 的頁面應是 B 的內容"
    mds = sorted(p.name for p in (out / "sessions").rglob("*.md"))
    assert len(mds) == 2, f"兩場應有兩個 .md，實得 {mds}"
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    sess_map = json.loads(re.search(r"var BX_SESS=(\{.*?\});\s*$", mgr, re.M).group(1)
                          .replace("\\u003c", "<").replace("\\u003e", ">"))
    assert sess_map[sid_a]["u"] != sess_map[sid_b]["u"], "書籤反查表的兩場應指到不同的檔"
    for sid, mark in ((sid_a, "AAAMARK"), (sid_b, "BBBMARK")):
        assert mark in (out / sess_map[sid]["u"]).read_text(encoding="utf-8"), (
            f"書籤反查表的 {sid[-8:]} 應指到它自己的頁面")
    assert "輸出檔名相同" not in r.stderr + r.stdout, f"沒撞名卻出聲：{r.stderr[-400:]}"

    # --- 2. 最後 8 碼也相同 ⇒ 真的撞名：要在 stderr 與收尾那一行講出來 ---
    sid_c = "019f0002-0002-7000-8000-ffff0a1a1a1a"
    assert sid_c[-8:] == sid_a[-8:] and sid_c[:8] == sid_a[:8]
    write(sid_c, 20, "CCCMARK")
    r2 = build(tmp / "out2")
    assert "輸出檔名相同" in r2.stderr and sid_c[-8:] in r2.stderr, (
        f"撞名要在 stderr 講出來\nSTDERR:{r2.stderr[-600:]}")
    assert "輸出檔名相同 1 個" in r2.stdout, f"收尾那一行要講撞名的個數\nSTDOUT:{r2.stdout[-400:]}"

    # --- 3. Claude 的檔名不變：id 段仍是 sessionId 前 8 碼（既有的連結與書籤快取都靠它）---
    assert SID[:8] != SID[-8:]
    out3 = tmp / "out3"
    r3 = subprocess.run([sys.executable, str(SCRIPT), "--claude-source", f"demo={_build_fixture(tmp / 'cl')}",
                         "--no-codex", "--out", str(out3)], capture_output=True, text=True, encoding="utf-8")
    assert r3.returncode == 0, f"非零退出\nSTDOUT:{r3.stdout}\nSTDERR:{r3.stderr}"
    names3 = [p.name for p in _session_pages(out3)]
    assert names3 and all(nm.endswith(f"__{SID[:8]}.html") for nm in names3), (
        f"Claude 檔名的 id 段應是 sessionId 前 8 碼，實得 {names3}")

    # --- 4. 只差大小寫的兩個路徑在 Windows 上是同一個檔：照樣要出聲 ---
    case_dir = tmp / "case" / "2026" / "06" / "07"
    case_dir.mkdir(parents=True, exist_ok=True)
    for f in sess_dir.glob("*.jsonl"):
        f.unlink()
    sess_dir = case_dir
    write("019f0002-0003-7000-8000-0000abcdef12", 0, "LOWERMARK")
    write("019f0002-0004-7000-8000-0000ABCDEF12", 10, "UPPERMARK")
    r4 = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'case'}",
                         "--no-claude", "--out", str(tmp / "out4")], capture_output=True, text=True, encoding="utf-8")
    assert r4.returncode == 0, r4.stderr
    assert "輸出檔名相同" in r4.stderr and "輸出檔名相同 1 個" in r4.stdout, (
        f"只差大小寫也要出聲\nSTDOUT:{r4.stdout[-300:]}\nSTDERR:{r4.stderr[-300:]}")

    # --- 5. 這一次一場都沒掃到（零場那條路）時，沿用下來的列撞名也要出聲 ---
    empty_src = tmp / "empty-src"
    empty_src.mkdir(exist_ok=True)
    r5 = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"other={empty_src}",
                         "--no-claude", "--out", str(tmp / "out4")], capture_output=True, text=True, encoding="utf-8")
    assert r5.returncode == 0, r5.stderr
    assert "輸出檔名相同" in r5.stderr and "輸出檔名相同 1 個" in r5.stdout, (
        f"零場那條路也要檢查沿用的列\nSTDOUT:{r5.stdout[-300:]}\nSTDERR:{r5.stderr[-300:]}")

    # --- 6. 只產一種格式的完整建置不刪另一種格式的檔（另一種的索引還連著它們）---
    home = tmp / "home6"
    cx = home / ".codex" / "sessions" / "2026" / "06" / "07"
    cx.mkdir(parents=True, exist_ok=True)
    sess_dir = cx
    write(sid_a, 0, "AAAMARK")
    env6 = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    out6 = tmp / "out6"

    def full(*extra):
        r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out6), *extra],
                           capture_output=True, text=True, encoding="utf-8", env=env6)
        assert r.returncode == 0, r.stderr
        return r
    full()
    page = next(p for p in _session_pages(out6))
    old_html, old_md = page.with_name("20260607-1000__Same__019f0002.html"), page.with_name("20260607-1000__Same__019f0002.md")
    old_html.write_text("舊檔名的頁面", encoding="utf-8")       # 換檔名之前留下的那一份
    old_md.write_text("舊檔名的頁面", encoding="utf-8")
    full("--format", "md")
    assert old_html.exists(), "只產 md 的建置不可以刪 html（index.html 沒重畫、還連著它）"
    assert not old_md.exists(), "只產 md 的建置照樣清 md 的孤兒檔"
    full("--format", "html")
    assert not old_html.exists(), "產 html 的完整建置要清掉舊的 html"
    assert page.exists() and page.with_suffix(".md").exists(), "現在的頁面不可以被清掉"
    print("OK: codex same-minute outputs test passed")


def test_session_kind_tags(tmp_path=None):
    # session 型態自動分類：review（首句 # Review）／exec（codex_exec 無頭）／一般；
    # 索引有型態下拉與標題徽章、row 帶 data-kind、md twin 有型態標記、session 頁有 chip。
    tmp = new_tmp(tmp_path)
    sess_dir = tmp / "sessions" / "2026" / "06" / "06"
    sess_dir.mkdir(parents=True, exist_ok=True)

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-06-06T02:00:{sec:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    def tc(sec, inp, cached, out):
        return line(sec, "event_msg", {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                 "output_tokens": out, "reasoning_output_tokens": 0,
                                 "total_tokens": inp + out}}})

    sid_a = "019f0000-0000-7000-8000-00000000000a"   # exec ＋ 首句 review → 型態 review
    (sess_dir / f"rollout-2026-06-06T02-00-00-{sid_a}.jsonl").write_text("\n".join([
        line(0, "session_meta", {"id": sid_a, "cwd": "/x/P", "cli_version": "0.29.0",
                                 "originator": "codex_exec", "source": "exec"}),
        line(1, "turn_context", {"cwd": "/x/P", "model": "gpt-5.5"}),
        line(2, "event_msg", {"type": "user_message",
                              "message": "# Review: demo-r1 — 2026-06-06 — P\n請唯讀審查REVIEWKINDMARK。"}),
        line(3, "response_item", {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "CLEAN"}]}),
        tc(4, 3000, 2000, 10),
    ]), encoding="utf-8")

    sid_b = "019f0001-0000-7000-8000-00000000000b"   # 無 originator、一般首句 → 型態 chat（無徽章）
    # （最後 8 碼須與 sid_a 不同：Codex 輸出檔名的 id 段是 sid 最後 8 碼，同分鐘＋同專案＋同 id 段會互蓋）
    (sess_dir / f"rollout-2026-06-06T02-10-00-{sid_b}.jsonl").write_text("\n".join([
        line(10, "session_meta", {"id": sid_b, "cwd": "/x/P", "cli_version": "0.29.0"}),
        line(11, "turn_context", {"cwd": "/x/P", "model": "gpt-5.5"}),
        line(12, "event_msg", {"type": "user_message", "message": "一般對話CHATKINDMARK。"}),
        line(13, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "好的。"}]}),
        tc(14, 3000, 2500, 10),
    ]), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'sessions'}",
         "--no-claude", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'id="fk"' in index, "索引應有型態下拉（兩種型態並存）"
    assert ">review</option>" in index and ">一般</option>" in index, "下拉應列出 review 與 一般"
    assert 'data-kind="review"' in index and 'data-kind="chat"' in index, "row 應帶 data-kind"
    assert 'class="chip kind"' in index and ">review</span>" in index, "review 標題旁應有型態徽章"
    assert "r.dataset.kind===k" in index and "k:FK?FK.value:''" in index, "篩選 JS 應納入型態並記憶狀態"

    htmls = {p.name: p.read_text(encoding="utf-8") for p in _session_pages(out)}
    page_a = next(v for v in htmls.values() if "REVIEWKINDMARK" in v)
    page_b = next(v for v in htmls.values() if "CHATKINDMARK" in v)
    assert 'class="chip kind"' in page_a and ">review</span>" in page_a, "review session 頁應有型態 chip"
    assert 'class="chip kind"' not in page_b, "一般 session 頁不應有型態 chip"

    imd = (out / "index.md").read_text(encoding="utf-8")
    assert "型態:review" in imd, "md 索引應標 review 型態"
    assert "型態:一般" not in imd, "一般型態不標（避免噪音）"
    md_a = next(p.read_text(encoding="utf-8") for p in (out / "sessions").rglob("*.md")
                if "REVIEWKINDMARK" in p.read_text(encoding="utf-8"))
    assert "- 型態：review" in md_a, "session md 表頭應有型態列"
    print("OK: session kind tags test passed")


def test_codex_survival_report(tmp_path=None):
    # Codex 快取存活統計獨立頁：相鄰呼叫 gap→命中，依型態分層；與 Claude 報告互相獨立
    #（無 Claude 資料時 cache-report 不產生、cache-codex 照樣產生）。
    tmp = new_tmp(tmp_path)
    sess_dir = tmp / "sessions" / "2026" / "06" / "07"
    sess_dir.mkdir(parents=True, exist_ok=True)

    def mkline(base_h, n, typ, payload):
        ts = f"2026-06-07T{base_h + n // 3600:02d}:{(n % 3600) // 60:02d}:{n % 60:02d}.000Z"
        return json.dumps({"timestamp": ts, "type": typ, "payload": payload}, ensure_ascii=False)

    def call(base_h, n, text, inp, cached, out_t):
        # 一次 API 呼叫＝assistant message（步驟起點，usage 掛此事件、epoch 取此事件時間）＋token_count
        return [
            mkline(base_h, n, "response_item", {"type": "message", "role": "assistant",
                                                "content": [{"type": "output_text", "text": text}]}),
            mkline(base_h, n + 1, "event_msg", {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                     "output_tokens": out_t, "reasoning_output_tokens": 0,
                                     "total_tokens": inp + out_t}}}),
        ]

    # A（exec＋首句 review → 型態 review）：30s 暖 → 480s 冷 → 5400s 暖（跨 1 小時仍 ≥25%）
    sid_a = "019f0002-0000-7000-8000-00000000000a"
    rows_a = [
        mkline(1, 0, "session_meta", {"id": sid_a, "cwd": "/x/P", "cli_version": "0.29.0",
                                      "originator": "codex_exec", "source": "exec"}),
        mkline(1, 1, "turn_context", {"cwd": "/x/P", "model": "gpt-5.5"}),
        mkline(1, 2, "event_msg", {"type": "user_message", "message": "# Review: surv-r1 — 審查SURVAMARK。"}),
    ]
    rows_a += call(1, 100, "步1", 2000, 1800, 10)
    rows_a += call(1, 130, "步2", 2100, 1900, 20)        # gap 30s → <1 分，90% 暖
    rows_a += call(1, 610, "步3", 2200, 100, 30)         # gap 480s → 5–10 分，5% 冷
    rows_a += call(1, 6010, "步4", 2300, 800, 40)        # gap 5400s → 1–2 時，35% 暖（top_warm）
    (sess_dir / f"rollout-2026-06-07T01-00-00-{sid_a}.jsonl").write_text(
        "\n".join(rows_a), encoding="utf-8")

    # B（一般互動）：40s 暖 → 1200s 冷
    sid_b = "019f0003-0000-7000-8000-00000000000b"
    rows_b = [
        mkline(4, 0, "session_meta", {"id": sid_b, "cwd": "/x/P", "cli_version": "0.29.0"}),
        mkline(4, 1, "turn_context", {"cwd": "/x/P", "model": "gpt-5.5"}),
        mkline(4, 2, "event_msg", {"type": "user_message", "message": "一般對話SURVBMARK。"}),
    ]
    rows_b += call(4, 200, "步1", 2000, 1900, 10)
    rows_b += call(4, 240, "步2", 2000, 1800, 20)        # gap 40s → <1 分，90% 暖
    rows_b += call(4, 1440, "步3", 2500, 200, 30)        # gap 1200s → 15–30 分，8% 冷
    (sess_dir / f"rollout-2026-06-07T04-00-00-{sid_b}.jsonl").write_text(
        "\n".join(rows_b), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'sessions'}",
         "--no-claude", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    # 獨立性：無 Claude 資料 → 健檢頁不產生，Codex 頁照樣產生
    assert (out / "cache-codex.html").exists() and (out / "cache-codex.md").exists(), "應產生 Codex 存活頁"
    assert not (out / "cache-report.html").exists(), "無 Claude 資料不應產生健檢頁"

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "cache-codex.html" in index, "索引應連到 Codex 存活頁"
    assert "cache-report.html" not in index, "索引不應連到不存在的健檢頁"
    imd = (out / "index.md").read_text(encoding="utf-8")
    assert "[Codex 快取存活](cache-codex.md)" in imd, "md 索引應連到 Codex 存活頁"

    md = (out / "cache-codex.md").read_text(encoding="utf-8")
    assert "整體命中率（token 加權）：**56%**" in md, "token 加權命中率應為 8500/15100=56%"
    assert "相鄰呼叫樣本：**5**" in md, "相鄰樣本應為 5（A 3 對＋B 2 對）"
    assert "**0/2**" in md, "短間隔（<2 分）斷點冷啟應為 0/2"
    assert "review：樣本" in md and "一般：樣本" in md, "分層欄應含 review 與 一般"
    assert "exec：樣本" not in md, "無 exec 樣本不應出現該欄"
    assert "| < 1 分 | 2 | 100%" in md and "| 90% |" in md, "<1 分桶：2 樣本全命中、中位 90%"
    assert "| 5–10 分 | 1 | 0%" in md and "| 5% |" in md, "5–10 分桶：1 樣本冷啟、中位 5%"
    assert "| 15–30 分 | 1 | 0%" in md, "15–30 分桶：1 樣本冷啟"
    assert "| 1–2 時 | 1 | 100%" in md and "| 35% |" in md, "1–2 時桶：1 樣本命中、中位 35%"
    assert "後仍 ≥ 25%" in md, "應列出 ≥1 小時仍命中的觀察"
    assert "非 TTL 量測" in md, "md 應明示方法限制"

    html = (out / "cache-codex.html").read_text(encoding="utf-8")
    assert "Codex 快取存活統計" in html and 'svg class="viz"' in html, "應有標題與存活曲線 SVG"
    assert 'class="sw co-1h"' in html, "legend 應有 sw 色塊"
    assert ">review<" in html and ">一般<" in html, "legend/表頭應含 review 與 一般"
    assert 'class="rep"' in html, "應有統計表"
    assert "觀察" in html and "不是 TTL 量測" in html, "頁面應明示方法限制"
    assert "cache-report.html" not in html, "無 Claude 報告時 Codex 頁不得殘留健檢頁連結（斷鏈）"
    print("OK: codex survival report test passed")


def test_review_detection():
    # review 型態偵測放寬：認 reviewer 角色/格式（含手寫 prompt），但不認裸 "review"/"審查"，
    # 也不誤收 Worker 跑 pr-review。
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    is_rev = lambda t: bool(v.REVIEW_RE.search(t))
    for t in [
        "# Review: demo-r1 — 2026 — P\n請唯讀審查",
        "# PROPOSAL-080 Fable Implementation-Stage Review Prompt 你是 Reviewer session，scope",
        "你是 Reviewer session，scope 是 review PROPOSAL-080 在兩次 fresh R7 gate 失敗後的最小 corrective",
        "You are a Reviewer session with fresh context — you did not author any of this",
        "You are Reviewer session, scope is reviewing PROPOSAL-026",
        "你是一位資深 Python 程式碼審查者。請做 R1 review：只審查目前未提交的這次改動",
    ]:
        assert is_rev(t), f"應判 review：{t[:40]!r}"
    for t in [
        "你是 Worker session, return-only，scope 是使用本專案既有 Dflow `/dflow:pr-review` 審查目前 feature/x",
        "幫我 review 一下這段程式碼",
        "請審查目前的改動並回報",
        "實作一個快取分析報告",
    ]:
        assert not is_rev(t), f"不應判 review：{t[:40]!r}"
    print("OK: review detection test passed")


def test_cache_report_by_kind():
    # Claude 報告「review vs 一般 分開統計」：build_cache_report 產出 by_kind 子報告、防遞迴、
    # 母體只有單一型態時不分層；HTML/MD 有比較表。
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    def steps():   # [epoch, cache_read, ctx, w_total, w5, w1h, midx]；step0 冷(first)、step1 命中(1h cohort、gap 300s)
        return [[1000, 0, 2000, 2000, 0, 2000, 0],
                [1300, 1900, 2000, 100, 0, 100, 0]]

    def row(kind):
        return {"source_kind": v.SOURCE_CLAUDE, "kind": kind, "account": "a",
                "cache_steps": steps(), "cache_models": ["m0"], "cache_events": []}

    d = v.build_cache_report([row("review"), row("chat")])
    assert d["by_kind"] is not None and set(d["by_kind"]) == {"review", "chat"}, "應分層 review/chat"
    assert d["by_kind"]["review"]["n_sessions"] == 1 and d["by_kind"]["chat"]["n_sessions"] == 1, "各型態 1 session"
    assert d["by_kind"]["review"].get("by_kind") is None, "子報告不得再往下分層（防遞迴）"
    assert d["n_sessions"] == 2, "全部＝兩型態合計"
    # 全部 first 冷啟＝2（兩 session 各一）＝子母體加總，確認分層與合計一致
    assert d["causes_total"]["first"] == 2
    assert (d["by_kind"]["review"]["causes_total"]["first"]
            + d["by_kind"]["chat"]["causes_total"]["first"]) == 2

    html = v.render_cache_report_html(d)
    assert "型態分層" in html and "review" in html and "一般" in html, "HTML 應有型態分層比較表"
    md = v.render_cache_report_md(d)
    assert "## 型態分層" in md, "MD 應有型態分層小節"

    # 母體只有單一型態 → 不分層（沒有可比對象）
    assert v.build_cache_report([row("chat")])["by_kind"] is None, "單一型態不分層"
    assert "型態分層" not in v.render_cache_report_html(v.build_cache_report([row("chat")])), \
        "單一型態的報告不應出現分層表"
    print("OK: cache report by-kind test passed")


def test_classify_cache_causes():
    # 逐步冷啟成因分類（供徽章著色）：first/switch/model/compact/expiry/evict/intra 各命中一次，
    # 並與 build_cache_report 的 causes_total 交叉比對，確保「報告」與「逐步徽章」用同一套判定、不漂移。
    import importlib
    from collections import Counter
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    # cache_steps 一筆＝[epoch, cache_read, ctx, w_total, w5, w1h, midx]；ctx=2000（≥ REPORT_MIN_CTX）
    def cold(t, midx=0):   # 冷啟步（read=0）；帶 1h 寫入以延續 cohort
        return [t, 0, 2000, 2000, 0, 2000, midx]
    def warm(t, midx=0):   # 命中步（read=1900/2000=95%）
        return [t, 1900, 2000, 2000, 0, 2000, midx]
    raw = [
        cold(0, 0),         # first：原始第 0 步冷啟
        cold(60, 0),        # intra：gap 60 < 120s
        cold(260, 0),       # evict：gap 200 ∈ [120,3300)，1h cohort
        cold(4260, 0),      # expiry：gap 4000 ≥ 3300
        cold(4360, 1),      # model：換 midx 0→1（優先於 gap）
        cold(4460, 1),      # compact：其間有 compact 事件
        cold(4560, 1),      # switch：其間有 limit 事件
        warm(4660, 1),      # 命中 → 無成因
    ]
    models = ["m0", "m1"]
    events = [[4400, "compact"], [4500, "limit"]]
    got = v.classify_cache_causes(raw, models, events)
    want = {0: "first", 60: "intra", 260: "evict", 4260: "expiry",
            4360: "model", 4460: "compact", 4560: "switch"}
    assert got == want, f"逐步成因不符：{got}"

    rows = [{"source_kind": v.SOURCE_CLAUDE, "account": "", "cache_steps": raw,
             "cache_models": models, "cache_events": events}]
    rep = v.build_cache_report(rows)
    agg = Counter(want.values())
    for k, n in rep["causes_total"].items():
        assert n == agg.get(k, 0), f"報告 causes_total[{k}]={n} 與分類器聚合 {agg.get(k,0)} 不一致（分類漂移）"
    print("OK: classify cache causes test passed")


def test_cold_cause_badges(tmp_path=None):
    # Claude 逐步冷啟徽章依成因著色：結構性（首呼叫）→ 中性灰 coldx；真失效（提早逐出）→ 醒目紅 cold。
    # 另驗 token 細分徽章（新輸入/快取寫入/快取讀取）與其勾選開關、預設隱藏 class。
    tmp = new_tmp(tmp_path)
    csid = "00000000-0000-4000-8000-0000000000c1"
    proj = tmp / "projects" / "cold-proj"
    proj.mkdir(parents=True, exist_ok=True)

    def a(t, mid, inp, cw, cr, w1h):    # 一次 assistant 呼叫（＝一步）；w1h→1h 寫入細分
        u = {"input_tokens": inp, "cache_creation_input_tokens": cw,
             "cache_read_input_tokens": cr, "output_tokens": 40,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": w1h}}
        return {"type": "assistant", "uuid": f"a{t}", "timestamp": t, "sessionId": csid,
                "isSidechain": False,
                "message": {"role": "assistant", "model": "claude-opus-4-8", "id": mid,
                            "usage": u, "content": [{"type": "text", "text": f"答{mid}"}]}}
    ev = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-06-10T01:00:00.000Z",
         "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.150", "sessionId": csid,
         "isSidechain": False, "message": {"role": "user", "content": "問題一COLDFIRST"}},
        a("2026-06-10T01:00:05.000Z", "m_c1", 2000, 2000, 0, 2000),      # 首呼叫 0% → 結構性 coldx
        {"type": "user", "uuid": "u2", "timestamp": "2026-06-10T01:10:00.000Z",
         "sessionId": csid, "isSidechain": False,
         "message": {"role": "user", "content": "問題二COLDEVICT"}},
        a("2026-06-10T01:10:05.000Z", "m_c2", 3000, 0, 100, 0),          # gap 600s、1h cohort、3% → evict cold
    ]
    (proj / f"{csid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={tmp / 'projects'}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = list(_session_pages(out))[0].read_text(encoding="utf-8")

    assert 'class="meter m-cache coldx"' in html, "首呼叫冷啟應為中性灰 coldx（非快取失效）"
    assert 'class="meter m-cache cold"' in html, "提早逐出冷啟應為醒目紅 cold（真失效）"
    assert "結構性/預期內" in html, "結構性冷啟的 title 應說明非失效"
    assert "含真正的快取失效" in html, "真失效冷啟的 title 應點名快取失效"
    # token 細分：三個新徽章 + 勾選開關 + 預設隱藏 class + coldx 樣式
    for needle, msg in {
        'class="meter m-in"': "應有『新輸入』細分徽章",
        'class="meter m-cw"': "應有『快取寫入』細分徽章",
        'class="meter m-cr"': "應有『快取讀取』細分徽章",
        'id="cb_in"': "應有新輸入勾選框", 'id="cb_cw"': "應有快取寫入勾選框",
        'id="cb_cr"': "應有快取讀取勾選框",
        "hide-in hide-cw hide-cr": "token 細分應預設隱藏",
        ".meter.coldx": "應有中性冷啟樣式",
    }.items():
        assert needle in html, f"{msg}（找不到 {needle!r}）"

    # 成因 join 必須以 message.id 對應，不可用「同秒第幾次」的序號：兩端母體不同——
    # 生產端（classify_cache_causes）只數得出 usage 的呼叫，消費端走的是**所有** `_step`，
    # 而沒有 usage 的呼叫照樣會產生 `_step`。同一秒裡夾一個沒有 usage 的步，序號就整批錯位，
    # 成因掛到錯的步驟上，而徽章看起來一切正常。
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    osid = "00000000-0000-4000-8000-0000000000c2"
    oproj = tmp / "projects" / "ord-proj"
    oproj.mkdir(parents=True, exist_ok=True)
    same_sec = "2026-06-10T01:10:05.000Z"
    nousage = {"type": "assistant", "uuid": "ax", "timestamp": same_sec, "sessionId": osid,
               "isSidechain": False,
               "message": {"role": "assistant", "model": "claude-opus-4-8", "id": "m_x",
                           "content": [{"type": "text", "text": "無 usage 但看得見的一步"}]}}
    oev = [ev[0] | {"sessionId": osid}, a("2026-06-10T01:00:05.000Z", "m_o1", 2000, 2000, 0, 2000),
           ev[2] | {"sessionId": osid}, nousage,
           a(same_sec, "m_o2", 3000, 0, 100, 0)]      # 與 nousage 同秒的真冷啟
    opath = oproj / f"{osid}.jsonl"
    opath.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in oev), encoding="utf-8")
    os_ = v.load_session(opath, "ord-proj")
    v.analyze(os_)
    by_mid = {b.get("mid"): b.get("cause")
              for g in os_.main_groups for b in g.get("blocks", []) if b.get("type") == "_step"}
    assert by_mid.get("m_o2") == "evict", \
        f"真冷啟那步應拿到自己的成因，實得 {by_mid}"
    assert not by_mid.get("m_x"), \
        f"沒有 usage、不在 cache_steps 裡的步不得被掛上別人的成因，實得 {by_mid}"
    # **單步回合**：不畫逐步分隔列，也不符合 ❄ 的 `n_steps >= 2` → 不另外處理的話，成因與並列標籤
    #   在頁面上一個字都看不到，而 `_masked_human_note`／`_srv_excluded_note` 對讀者寫的正是
    #   「逐步徽章會兩個都標」。規則要與多步回合一致：成因進 title、並列條件攤到版面上；
    #   MD 沒有 title 可掛，所以成因一律攤在版面上。
    def _one(cause, masked, n_steps=1):
        su = {"input": 3000, "cache_create": 0, "cache_read": 100, "total_in": 3100,
              "output": 40, "miss": "", "miss_tok": 0, "miss_usd": None,
              "miss_usd_partial": False, "win": None, "effort": None}
        gu = v._new_turn_usage()
        gu.update({"input": 3000, "cache_create": 0, "cache_read": 100, "output": 40,
                   "ctx_max": 3100, "cost": 0.01})
        return {"role": "assistant", "u": gu, "n_steps": n_steps,
                "blocks": [{"type": "_step", "idx": 1, "u": su, "cause": cause,
                            "cause_masked": masked, "t": 1780000000, "gap": None}]}

    g1 = _one("unavail", ["expiry"])          # 實據勝出、但同一步仍成立人因條件
    h1, m1 = v.render_turn_meters(g1), v.turn_meters_md(g1)
    both = v._cold_cause_label("unavail", ["expiry"])       # 「閒置過期＋伺服器不可用」
    assert both in h1, f"單步回合的整段徽章要把並列成因攤到版面上，實得 {h1}"
    assert both in m1, f"MD 的單步回合同樣要看得到並列成因，實得 {m1}"
    assert "只算一次" in h1, "並列的是顯示不是記帳：title 要講明金額記在哪一邊"
    # 並列時 tooltip **不得自相矛盾**：前半句若由 turn_cold_class 決定
    #   （只看勝出成因 → unavail 判成「非快取失效」），後半句卻在說「閒置過期（可避免）」。
    for bad in ("非快取失效", "含真正的快取失效"):
        assert bad not in h1, (
            f"並列成因時前半句不得預判「是不是失效」（與後半句打架），實得 {h1[:300]}")
    g2 = _one("expiry", None)                 # 沒有並列條件：成因進 title（與多步的 ❄ 同規則）
    h2, m2 = v.render_turn_meters(g2), v.turn_meters_md(g2)
    assert v._COLD_CAUSE_NOTE["expiry"] in h2, f"單步回合的 title 要講得出成因，實得 {h2}"
    assert v._cold_cause_label("expiry", None) in m2, f"MD 沒有 title，成因一律上版面：{m2}"
    # ⚠ 既有的「是不是真失效」判讀不得被換掉——換掉等於把原本有的資訊換走（那是回歸不是修正）
    assert "非快取失效" in v.render_turn_meters(_one("first", None)),         "結構性冷啟的單步回合仍要說明非失效"
    print("OK: cold cause badges test passed")


def test_api_miss_reason(tmp_path=None):
    # Claude API 自報的快取失效成因（message.diagnostics.cache_miss_reason）：
    #  (1) 有自報就以自報為準——帶內（gap 600s）本該被推論成 evict 的冷啟，自報 tools_changed → 成因 tools、
    #      著色轉中性灰、且排除在 TTL/帶內樣本外（不得算成「提早失效」）；
    #  (2) 命中率仍高的「部分失效」也要看得見（⚡% 完全看不出來）；
    #  (3) 附帶的新可勾選欄位：距上一步 / effort / ⏱回合耗時 / ctx 佔視窗 %。
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    # ── 分類器：同一組步驟，只差有沒有自報成因 ──
    def step(t, cr, code=0, mtok=0):     # [epoch, read, ctx, 寫總, 寫5m, 寫1h, midx, 自報碼, 重算量]
        return [t, cr, 2000, 2000, 0, 2000, 0, code, mtok]
    tools_code = v.API_MISS_CODES.index("tools_changed")
    raw_plain = [step(0, 0), step(600, 0)]
    raw_api = [step(0, 0), step(600, 0, tools_code, 20000)]
    assert v.classify_cache_causes(raw_plain, ["m0"], [])[600] == "evict", "沒有自報時應推論為提早失效"
    assert v.classify_cache_causes(raw_api, ["m0"], [])[600] == "tools", "有自報時應以 API 成因為準"

    def rep(raw):
        return v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                      "cache_steps": raw, "cache_models": ["claude-opus-4-8"],
                                      "cache_events": []}])
    d0, d1 = rep(raw_plain), rep(raw_api)
    assert d0["causes_total"]["evict"] == 1 and d0["kpi"]["comply_n"] == 1, "對照組：帶內樣本＋提早失效各 1"
    assert d1["causes_total"]["evict"] == 0 and d1["causes_total"]["tools"] == 1, "自報前綴變動不得算提早失效"
    assert d1["kpi"]["comply_n"] == 0 and d1["api"]["excluded"] == 1, "前綴變動的相鄰步對應排除在帶內樣本外"
    assert d1["api"]["cold"]["tools_changed"] == 1 and d1["api"]["cold_tok"] == 20000, "應記錄自報成因與重算量"
    assert d1["api"]["cold_usd"] > 0, "已知模型應能估出被迫重算多花多少"
    # 部分失效：命中率 95%（非冷啟）卻有自報成因 → 只會在 api.partial 出現，不進成因分解
    d2 = rep([step(0, 0), step(60, 1900, v.API_MISS_CODES.index("unavailable"))])
    assert d2["api"]["partial"]["unavailable"] == 1, "命中率仍高的部分失效也要被記錄"
    assert sum(d2["api"]["cold"].values()) == 0, "部分失效不是冷啟"

    # 排除計數要分兩種：全母體 excluded vs「本來會落在應命中帶」的 band_excluded——
    # 假說頁引用的是後者，用前者會誇大好幾倍（同一批資料裡兩者相差很大）。
    def st2(t, cr, code=0, w1h=2000):
        return [t, cr, 2000, 2000, 0, w1h, 0, code, 0]
    # 兩者必須不相等，斷言才有鑑別力：一對帶外（gap 30s）＋一對帶內（gap 970s）都因前綴變動排除，
    # 再加一次帶內、無自報成因的真失效讓 _band_api_note 有東西可講。
    d4 = rep([st2(0, 0), st2(30, 0, tools_code),        # 帶外（< 2 分）
              st2(1000, 0, tools_code),                 # 帶內 → 只有這對算 band_excluded
              st2(2000, 0)])                            # 帶內、無自報 → 真失效
    assert d4["api"]["excluded"] == 2, "兩對都因前綴變動被排除（全母體）"
    assert d4["api"]["band_excluded"] == 1, "只有一對本來會落在應命中帶"
    note = v._band_api_note(d4)
    assert "1 對" in note, f"假說頁應引用帶內排除數（1）：{note}"
    assert "2 對" not in note, f"不得把全母體排除數（2）當成帶內排除數：{note}"
    # 同時有邊界（429）的前綴變動對，本來就會被 boundary 擋掉 → 不得記進 band_excluded
    d5 = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                "cache_steps": [st2(0, 0), st2(600, 0, tools_code)],
                                "cache_models": ["claude-opus-4-8"], "cache_events": [[300, "limit"]]}])
    assert d5["api"]["excluded"] == 0 and d5["api"]["band_excluded"] == 0, \
        "邊界同時成立時不得歸因為『因自報而排除』（兩個計數都不算）"

    # 金額上限：cache_missed_input_tokens 是「失效前綴有多長」，不是這次寫了多少。
    # missed 遠大於本步實際寫入時，多付的錢不可能超過「真的寫進去的量」→ 以寫入量為上限。
    big = v.rewrite_waste_usd("claude-opus-4-8", 344317, 0, 8194, wrote=8194)
    assert abs(big - 8194 * 1.9 * 5 / 1e6) < 1e-9, f"missed 超過寫入量時應以寫入量計價，得 {big}"
    assert abs(v.rewrite_waste_usd("claude-opus-4-8", 5000, 0, 9000, wrote=9000)
               - 5000 * 1.9 * 5 / 1e6) < 1e-9, "missed 小於寫入量時照 missed 計"
    assert v.rewrite_waste_usd("claude-opus-4-8", 344317, 0, 0, wrote=0) is None, "沒寫入就沒有多付"

    # 未知的新成因：字串與長度都要留著（否則新 type 一出現就靜默少算）
    assert v._miss_reason({"diagnostics": {"cache_miss_reason":
                                           {"type": "future_reason", "cache_missed_input_tokens": 999}}}) \
        == ("future_reason", 999), "未知成因的長度不得被丟掉"

    # 未知的新成因也要能穿透「報告」路徑（不只逐步徽章）：cache_steps 用哨兵碼保住次數與失效前綴長度。
    # 哨兵碼刻意是固定高值、與 API_MISS_CODES 長度解耦（append 新 type 不得位移舊 row 的解讀）
    sentinel = v.API_MISS_UNKNOWN_CODE
    assert sentinel > len(v.API_MISS_CODES), "哨兵碼必須在索引範圍之外，否則新增 type 會撞號"
    d_unknown = rep([step(0, 0), step(600, 0, sentinel, 12345)])
    assert d_unknown["api"]["n"] == 1, "報告端不得把未知的新成因整個丟掉（次數要在）"
    assert d_unknown["api"]["cold_tok"] == 12345, "未知新成因的失效前綴長度也要保住"
    assert d_unknown["api"]["excluded"] == 0, "未知型別不知是否為前綴變動 → 不得排除，退回推論（安全預設）"

    # 同一步既有 API 自報成因、又落在邊界（429/壓縮）後：兩邊都必須重置 TTL lineage，
    # 否則徽章判 expiry、報告判 evict（實際踩過的分歧）。
    def st3(t, cr, code=0, w=0):
        return [t, cr, 2000, w, 0, w, 0, code, 0]
    raw_b = [st3(0, 0, 0, 2000), st3(60, 1900, 0, 2000),
             st3(200, 0, tools_code, 0),      # 自報成因＋邊界，且本步沒有寫入 → lineage 必須重置
             st3(600, 0, 0, 2000)]
    ev_b = [[150, "limit"]]
    got = v.classify_cache_causes(raw_b, ["claude-opus-4-8"], ev_b)
    rep_b = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                   "cache_steps": raw_b, "cache_models": ["claude-opus-4-8"],
                                   "cache_events": ev_b}])
    from collections import Counter
    assert Counter(got.values()) == Counter({k: n for k, n in rep_b["causes_total"].items() if n}), \
        f"徽章與報告的成因判定不一致（lineage 漂移）：{got} vs {rep_b['causes_total']}"
    html = v.render_cache_report_html(d1)
    assert "②-b" in html and "工具定義變動" in html, "報告應有 API 自報成因小節"
    assert "部分失效" in html, "報告應點出部分失效"
    md = v.render_cache_report_md(d1)
    assert "②-b" in md and "工具定義變動" in md, "MD 報告應有同一小節"

    # ── 端對端：session 頁徽章 ──
    tmp = new_tmp(tmp_path)
    msid = "00000000-0000-4000-8000-0000000000m1".replace("m", "a")
    proj = tmp / "projects" / "miss-proj"
    proj.mkdir(parents=True, exist_ok=True)

    def a(t, mid, inp, cw, cr, uuid, diag=None, empty=False):
        u = {"input_tokens": inp, "cache_creation_input_tokens": cw,
             "cache_read_input_tokens": cr, "output_tokens": 40,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": cw}}
        msg = {"role": "assistant", "model": "claude-opus-4-8", "id": mid, "usage": u,
               "content": [] if empty else [{"type": "text", "text": f"答{mid}"}]}
        msg["diagnostics"] = {"cache_miss_reason": diag} if diag else None
        return {"type": "assistant", "uuid": uuid, "timestamp": t, "sessionId": msid,
                "isSidechain": False, "effort": "max", "message": msg}
    ev = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-06-20T01:00:00.000Z",
         "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.218", "sessionId": msid,
         "isSidechain": False, "message": {"role": "user", "content": "問題一MISSQ"}},
        a("2026-06-20T01:00:05.000Z", "m_m1", 3000, 3000, 0, "a1"),
        {"type": "user", "uuid": "u2", "timestamp": "2026-06-20T01:10:00.000Z",
         "sessionId": msid, "isSidechain": False,
         "message": {"role": "user", "content": "問題二MISSQ"}},
        # 冷啟＋自報 tools_changed（gap 600s：沒有自報就會被判成 evict）
        a("2026-06-20T01:10:05.000Z", "m_m2", 2, 20000, 3000, "a2",
          {"type": "tools_changed", "cache_missed_input_tokens": 20000}),
        # 部分失效：命中率高，但 API 說有一段沒命中
        a("2026-06-20T01:10:20.000Z", "m_m3", 2, 500, 40000, "a3", {"type": "unavailable"}),
        # 門檻邊界：命中 24.6%（四捨五入是 25%）→ 必須算冷啟，不得標成「部分失效」
        a("2026-06-20T01:10:30.000Z", "m_m4", 2, 7538, 2460, "a4", {"type": "system_changed"}),
        # 同一個顯示回合裡有兩個真實回合的耗時（中間 user 是純 tool_result）→ 要累加不是覆寫
        {"type": "system", "subtype": "turn_duration", "uuid": "sd1", "parentUuid": "a3",
         "timestamp": "2026-06-20T01:10:21.000Z", "sessionId": msid, "isSidechain": False,
         "isMeta": True, "durationMs": 21000, "messageCount": 4},
        {"type": "system", "subtype": "turn_duration", "uuid": "sd2", "parentUuid": "a4",
         "timestamp": "2026-06-20T01:10:31.000Z", "sessionId": msid, "isSidechain": False,
         "isMeta": True, "durationMs": 5000, "messageCount": 5},
        # 同一次呼叫（m_m5）的**首事件沒有可呈現內容**（實測近六成呼叫如此，例如只有 thinking 簽章）。
        # cache_steps 用首事件時間（01:13:30）當鍵，但 `_step` 標記會落到 7 分後那筆（01:20:30）→
        # 若不正規化，成因 join 對不上（這步的 evict 會被畫成中性灰）、「距上一步」也會多算 7 分。
        a("2026-06-20T01:15:30.000Z", "m_m5", 2, 9000, 1000, "a5x", empty=True),
        a("2026-06-20T01:21:30.000Z", "m_m5", 2, 9000, 1000, "a5"),
        # turn_duration 掛在「沒有可呈現內容」的 assistant 事件上（a5x）：group_turns 會跳過該事件，
        # 若不在跳過前先收下耗時，這段 wall-clock 就靜默消失、⏱ 偏低（實測真有 1 筆 223 秒被丟掉）。
        {"type": "system", "subtype": "turn_duration", "uuid": "sd3", "parentUuid": "a5x",
         "timestamp": "2026-06-20T01:15:31.000Z", "sessionId": msid, "isSidechain": False,
         "isMeta": True, "durationMs": 10000, "messageCount": 6},
    ]
    (proj / f"{msid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={tmp / 'projects'}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = list(_session_pages(out))[0].read_text(encoding="utf-8")
    mdtxt = list((out / "sessions").rglob("*.md"))[0].read_text(encoding="utf-8")
    for needle, msg in {
        'class="meter m-miss"': "應有 API 自報失效徽章",
        "⚠工具定義變動": "應標出自報成因",
        "失效前綴 20.0k": "應標出 API 回報的失效前綴長度（不是重寫量）",
        "⚠部分失效·快取暫時不可用": "命中率高的只掉一段，要標成中性的「部分失效」而非整段失效",
        'class="meter m-miss part"': "部分失效應用中性樣式，不與整段失效同色",
        'id="cb_miss"': "應有失效原因勾選框",
        'id="cb_gap"': "應有『距上一步』勾選框",
        'id="cb_eff"': "應有 effort 勾選框",
        'id="cb_dur"': "應有耗時勾選框",
        "距上一步": "步驟徽章應顯示距上一次呼叫多久",
        "effort max": "步驟徽章應顯示 effort",
        "⏱36秒": "多段耗時要累加（21s＋5s＋掛在無內容事件上的 10s），不是只留最後一段、也不得漏掉後者",
        "⚠ 快取被打斷 3 次": "session 表頭應彙總被打斷次數",
        "hide-dur hide-ttl hide-eff": "耗時、寫入 TTL 與 effort 應預設隱藏",
        "%)": "脈絡徽章應附佔 context 視窗的 %",
    }.items():
        assert needle in html, f"{msg}（找不到 {needle!r}）"
    # 「距上一步」預設開啟：DEF 與 body class 必須同步，否則載入時會先隱藏再由 JS 補顯示，
    # 關掉 JS 更是完全不成立。
    bodycls = re.search(r'<body class="([^"]*)"', html)
    assert bodycls and "hide-gap" not in bodycls.group(1),         "「距上一步」預設開啟，不應同時被 body class 隱藏：" + (bodycls.group(1) if bodycls else "(無)")
    m_def = re.search(r"DEF=\{([^}]*)\}", html)
    assert m_def and "gap:1" in m_def.group(1), (
        "「距上一步」的 DEF 應為 1；⚠ 要對準 DEF 那一段——CSS 是每頁共用的常數，"
        "裡面的 gap:12px／gap:14px 會讓整頁搜尋「gap:1」永遠命中："
        + (m_def.group(1) if m_def else "(找不到 DEF)"))
    assert "工具定義變動" in mdtxt and "⚠" in mdtxt, "MD 也要看得到自報成因"
    # 24.6% 那步：⚡ 要著色成冷啟，緊接的 ⚠ 必須是整段失效樣式（非 m-miss part）；
    # 且**顯示也要寫 24.6%**——寫成 25% 會與報告的「門檻＝命中率 < 25%」打架。
    m = re.search(r'>⚡24\.6%</span><span class="meter (m-miss[^"]*)"[^>]*>⚠(系統提示變動)', html)
    assert m and m.group(1) == "m-miss", \
        f"命中率 24.6% 應判冷啟（整段失效）且顯示 24.6%，不得四捨五入成 25%：{m and m.group(1)}"
    assert 'class="meter m-cache coldx" title="此步驟未命中快取' in html, "該步的 ⚡ 也要著上冷啟色"
    assert 'class="meter m-cache coldx"' in html, "自報前綴變動的冷啟＝結構性，應為中性灰而非紅"
    # 首事件無可呈現內容的呼叫：成因 join 與 gap 都要用「呼叫起點」而非「第一筆有內容的事件」。
    # m_m5 是 1h cohort、間隔 180s 的冷啟 → 應判 evict（真失效＝紅標 cold），不得因 join 失敗變成灰的未歸因。
    assert 'class="meter m-cache cold"' in html, \
        "首事件無內容的呼叫，其真失效（evict）成因必須 join 得上並標紅，不得顯示成未歸因的灰"
    # 呼叫起點 01:15:30、首個有內容事件 01:21:30，前一步 01:10:30 → 正確 300s，錯誤會是 660s
    assert f"距上一步 {v.fmt_dur(300)}" in html, "「距上一步」須為真正的呼叫間隔（300s）"
    assert f"距上一步 {v.fmt_dur(660)}" not in html, "不得用「上次吐出內容→這次吐出內容」當間隔（660s）"
    report = (out / "cache-report.html").read_text(encoding="utf-8")
    assert "②-b" in report and "工具定義變動" in report, "報告頁應含 API 自報小節"

    # 子代理步驟（cause=None）的冷啟著色，與 ②-b 金額混到未知模型時的標記。
    # (#1) 子代理步驟（cause=None）若 API 自報前綴變動，⚡ 要中性灰（coldx）、不得塗紅與旁邊 ⚠ 自打；
    #      但 previous_message_not_found 這種「真失效候選」cause=None 仍應紅標。
    su_pref = {"total_in": 10000, "cache_read": 0, "input": 2, "cache_create": 9998, "output": 5,
               "miss": "tools_changed", "miss_tok": 5000, "miss_usd": 0.1}
    hp = v.render_step_meters(1, su_pref, cause=None)
    assert 'class="meter m-cache coldx"' in hp, "子代理前綴變動冷啟應中性灰（coldx），不得塗紅"
    hf = v.render_step_meters(1, dict(su_pref, miss="previous_message_not_found"), cause=None)
    assert 'class="meter m-cache coldx"' not in hf and 'class="meter m-cache cold"' in hf, \
        "真失效候選（前文不在快取）cause=None 仍應紅標 cold"
    # (#4) ②-b 金額：混到未知模型時，能估的照估、估不出的標 usd_partial 並在畫面附 +?（比照 ① 的 +?）。
    d_mix = v.build_cache_report([
        {"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
         "cache_steps": [step(0, 0), step(600, 0, tools_code, 5000)],
         "cache_models": ["claude-opus-4-8"], "cache_events": []},
        {"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
         "cache_steps": [step(0, 0), step(600, 0, tools_code, 5000)],
         "cache_models": ["claude-zzznew-9"], "cache_events": []}])
    assert d_mix["api"]["usd_partial"] is True and d_mix["api"]["cold_usd"] > 0, \
        "混到未知模型：能估的照估、估不出的標 usd_partial"
    assert "+?" in v.render_cache_report_html(d_mix), "②-b 金額應附 +? 提示有步驟估不出"
    d_known = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                     "cache_steps": [step(0, 0), step(600, 0, tools_code, 5000)],
                                     "cache_models": ["claude-opus-4-8"], "cache_events": []}])
    assert d_known["api"]["usd_partial"] is False, "全已知模型不該標記 usd_partial"

    # `cause or …` 會把主對話「已分析、非失效」的 cause=""（無自報）洗成 None → 誤塗紅，
    # 動到無 diagnostics 的舊資料（違反不變量①）。cause="" 且無自報必須維持中性灰。
    su_ana = {"total_in": 10000, "cache_read": 0, "input": 2, "cache_create": 9998, "output": 5, "miss": ""}
    assert 'class="meter m-cache coldx"' in v.render_step_meters(1, su_ana, cause=""), \
        'cause=""（已分析非失效）無自報時應維持 coldx，不得塗紅'
    g_ana = {"n_steps": 2, "u": {"input": 2, "cache_create": 9998, "cache_read": 0},
             "blocks": [{"type": "_step", "idx": 1, "cause": "",
                         "u": {"total_in": 10000, "cache_read": 0, "miss": ""}}]}
    assert v.turn_cold_class(g_ana) == "coldx", 'cause="" 的已分析冷啟回合應中性灰（不變量①）'

    # rewrite_waste_usd 混合 TTL 要逐段拆（同 avoid_usd），不得整段按 1h 高估。
    mix = v.rewrite_waste_usd("claude-opus-4-8", 50000, 9000, 1000, wrote=10000)   # 9k@5m + 1k@1h
    assert abs(mix - 10000 * (1.325 - 0.10) * 5 / 1e6) < 1e-9, f"混合 TTL 應逐段拆（blended 1.325），得 {mix}"
    assert abs(mix - (9000 * 1.25 + 1000 * 2.0 - 10000 * 0.10) * 5 / 1e6) < 1e-9, "missed==wrote 應化簡成 avoid_usd"
    assert abs(v.rewrite_waste_usd("claude-opus-4-8", 5000, 0, 8000, wrote=8000)
               - 5000 * 1.9 * 5 / 1e6) < 1e-9, "純 1h 寫入仍為 2.0（不回歸）"
    # fmt_money 的 "<$0.01" 在 ②-b HTML 要逃逸成 &lt;$0.01，不得吐裸 <（無效標記）。
    d_tiny = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                    "cache_steps": [step(0, 0), step(600, 0, tools_code, 1)],
                                    "cache_models": ["claude-opus-4-8"], "cache_events": []}])
    h_tiny = v.render_cache_report_html(d_tiny)
    assert "&lt;$0.01" in h_tiny and "~<$0.01" not in h_tiny, "②-b 的 <$0.01 金額應逃逸，不得吐裸 <"

    # 全部都是未知模型時 usd＝0，但仍要顯示「?」提示算不出來——整句消失會讓人以為沒多付。
    d_unk = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                   "cache_steps": [step(0, 0), step(600, 0, tools_code, 5000)],
                                   "cache_models": ["claude-zzznew-9"], "cache_events": []}])
    assert d_unk["api"]["usd_partial"] is True and d_unk["api"]["cold_usd"] == 0
    assert "多付 <b>~?</b>" in v.render_cache_report_html(d_unk), "全未知模型時金額應顯示 ?，不得整句消失"
    assert "多付 ~?" in "\n".join(v._api_miss_md(d_unk)), "MD 版同樣要顯示 ?"

    # 帶內失效樣本全被排除時（band 空），排除揭露更要講——否則遵約率看起來完美卻沒說原因。
    d_allx = rep([st2(0, 0), st2(1000, 0, tools_code)])
    assert d_allx["api"]["band_excluded"] == 1 and not d_allx["api"]["band"]
    note_x = v._band_api_note(d_allx)
    assert "整對移出樣本" in note_x and "1 對" in note_x, f"band 空時仍須揭露排除：{note_x!r}"

    # 帶內樣本被排除到 0 時，假說頁不得謊稱「尚無應命中帶樣本…累積資料後才有東西可檢定」
    # （樣本存在、是被排除掉的），且排除揭露必須照樣出現（不可被 if comply_n 的 guard 擋掉）。
    d_zero = rep([st2(0, 0), st2(1000, 0, tools_code), st2(2000, 0, tools_code)])
    assert d_zero["kpi"]["comply_n"] == 0 and d_zero["api"]["band_excluded"] >= 1, "前提：帶內樣本全被排除"
    for txt, what in ((v.render_cache_hypotheses_html(d_zero), "HTML"),
                      (v.render_cache_hypotheses_md(d_zero), "MD")):
        assert "不是" in txt and "對因 API 自報" in txt, f"{what}：應說明不是資料不足而是被排除"
        # 歸因不得把邊界（切帳號/換模型/壓縮）排掉的帶內對也算成「API 自報」——它們不計進 band_excluded
        assert "全部因 API 自報" not in txt, f"{what}：不得把所有排除都歸給 API 自報"
        assert "其餘為切帳號" in txt, f"{what}：應點出其餘排除來自邊界"
        assert "累積資料後這頁才有東西可檢定" not in txt, f"{what}：不得謊稱資料不足"
        assert "整對移出樣本" in txt, f"{what}：排除揭露必須出現（最需要它的時候）"
    assert "帶內樣本全數被移出" in v._band_api_note(d_zero), "連樣本都不剩時不可說成「沒有失效樣本」"

    # 回合徽章的整段／只掉一段要照實拆，不得因「有一步整段冷啟」就把整個 ×N 講成整段失效
    g_mix = {"role": "assistant", "n_steps": 3, "blocks": [],
             "u": {"input": 2, "cache_create": 9000, "cache_read": 30000, "ctx_max": 39002,
                   "output": 9, "cost": 0.1, "unpriced": False, "miss_tok": 1000, "miss_usd": 0.2,
                   "miss": ["tools_changed", "unavailable", "unavailable"], "miss_cold": 1}}
    h_mix = v.render_turn_meters(g_mix)
    assert "整段1·只掉一段2·" in h_mix, f"混合時應拆成「整段1·只掉一段2」：{h_mix[:220]}"
    md_mix = v.turn_meters_md(g_mix)
    assert "整段1·只掉一段2·" in md_mix, f"MD 同樣要拆：{md_mix}"
    # 全部整段 → 無標籤；全部只掉一段 → 「部分失效·」（兩端不得回歸）
    h_all = v.render_turn_meters(dict(g_mix, u=dict(g_mix["u"], miss_cold=3)))
    assert "整段3·" not in h_all and "部分失效·" not in h_all, f"全整段時不該有拆分標籤：{h_all[:200]}"
    assert "部分失效·" in v.render_turn_meters(dict(g_mix, u=dict(g_mix["u"], miss_cold=0)))

    # 回合／表頭金額也要有 +?：未知模型不得靜默貢獻 0，讓畫面顯示偏低的「多付」。
    acc = v._new_turn_usage()
    v._acc_turn_usage(acc, {"id": "mu1", "model": "claude-zzznew-9",
                            "diagnostics": {"cache_miss_reason":
                                            {"type": "tools_changed", "cache_missed_input_tokens": 9000}},
                            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 9000,
                                      "cache_read_input_tokens": 0, "output_tokens": 5,
                                      "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                         "ephemeral_1h_input_tokens": 9000}}})
    assert acc["miss_usd_partial"] is True and acc["miss_usd"] == 0.0, "未知模型應標 miss_usd_partial"
    html_tp = v.render_turn_meters({"role": "assistant", "n_steps": 1, "blocks": [],
                                    "u": dict(acc, ctx_max=9002, cost=0.0, unpriced=True)})
    assert "~?" in html_tp, f"回合徽章金額應顯示 ?（算不出來），不得整段消失：{html_tp[:200]}"
    class _S:      # 表頭同理（_miss_head 走 cost_label）
        miss_kinds = {"tools_changed": 1}
        miss_tok, miss_usd, miss_usd_partial, miss_cold = 9000, 0.0, True, 1
    assert "多付 ~?" in v._miss_head(_S())[0], "session 表頭金額也要標 ?"

    # 逐步徽章同理——回合／表頭／②-b 三層都標 ? 了，只有步驟層靜默省略金額＝同一事實兩處
    # 說法不一，且「失效前綴 9.0k」不附金額會被讀成「沒多付」。三種情形都要釘：
    def _mk(model, c1, c2):
        return {"id": "su", "model": model,
                "diagnostics": {"cache_miss_reason": {"type": "tools_changed",
                                                      "cache_missed_input_tokens": 9000}},
                "usage": {"input_tokens": 2, "cache_creation_input_tokens": c1,
                          "cache_read_input_tokens": c2, "output_tokens": 5}}
    su_unk = v._step_usage(_mk("claude-zzznew-9", 9000, 0))          # 未知模型 → 標 ?
    assert su_unk["miss_usd"] is None and su_unk["miss_usd_partial"] is True, \
        f"未知模型的步驟應標 miss_usd_partial：{su_unk}"
    assert "多付 ~?" in v.render_step_meters(1, su_unk), \
        f"逐步徽章金額算不出時要標 ?：{v.render_step_meters(1, su_unk)}"
    su_known = v._step_usage(_mk("claude-opus-4-20250514", 9000, 0))  # 已知模型 → 照常顯示金額
    assert su_known["miss_usd_partial"] is False and "多付 ~$" in v.render_step_meters(1, su_known), \
        "已知模型不得回歸成 ?"
    su_zero = v._step_usage(_mk("claude-opus-4-20250514", 0, 90000))  # 寫入 0＝真的沒多付，不可誤標 ?
    assert su_zero["miss_usd"] is None and su_zero["miss_usd_partial"] is False, \
        "被 wrote 上限壓成 0 是『真的沒多付』，與『算不出來』不同，不得標 ?"
    assert "多付" not in v.render_step_meters(1, su_zero), "沒多付的步驟不該出現金額字樣"
    # 不變量①：沒有 diagnostics 的舊資料完全不受影響（不得冒出徽章或旗標）
    su_old = v._step_usage({"id": "so", "model": "claude-opus-4-20250514",
                            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100,
                                      "cache_read_input_tokens": 900, "output_tokens": 5}})
    assert su_old["miss"] == "" and su_old["miss_usd_partial"] is False, "舊資料不得被本修正動到"
    assert "失效前綴" not in v.render_step_meters(1, su_old), "舊資料不該出現失效徽章"

    # ③ 的 MD 版不得還寫「不可避免＝第一句/切帳號/換模型/壓縮」（HTML 與 MD 要同一種說法）
    md_rep = v.render_cache_report_md(d1)
    assert "不可避免＝第一句" not in md_rep, "③ MD 仍用舊標籤（前綴變動也落在該桶，說法會與 ② 打架）"
    assert "非閒置造成" in md_rep, "③ MD 應改用「非閒置造成」並列出前綴變動"

    # 壓縮邊界後的冷啟會自報 messages_changed：不得被通稱 msgs 蓋掉更準的 compact 實據，
    # 否則「壓縮後」那欄歸零，還被挪進「習慣可避免」（自動壓縮不是改習慣能省的）。
    msgs_code = v.API_MISS_CODES.index("messages_changed")
    raw_c = [st2(0, 0), st2(600, 0, msgs_code)]
    ev_c = [[300, "compact"]]
    got_c = v.classify_cache_causes(raw_c, ["claude-opus-4-8"], ev_c)
    assert got_c[600] == "compact", f"壓縮邊界應勝過通稱 msgs：{got_c}"
    rep_c = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                   "cache_steps": raw_c, "cache_models": ["claude-opus-4-8"],
                                   "cache_events": ev_c}])
    assert rep_c["causes_total"]["compact"] == 1 and rep_c["causes_total"]["msgs"] == 0, \
        f"報告端也要歸「壓縮後」：{ {k: n for k, n in rep_c['causes_total'].items() if n} }"
    assert Counter(got_c.values()) == Counter({k: n for k, n in rep_c["causes_total"].items() if n}), \
        "徽章與報告在 compact＋messages_changed 上不得漂移"
    # 且帳目不變：同時有邊界的對本來就被 boundary 擋掉，不得計入「因自報而排除」
    assert rep_c["api"]["excluded"] == 0 and rep_c["api"]["band_excluded"] == 0, \
        "compact 邊界的對不算『因自報而排除』（帳目須與純邊界路徑相同）"
    # 沒有壓縮邊界時，messages_changed 仍應歸 msgs（不得反向誤判）
    got_n = v.classify_cache_causes(raw_c, ["claude-opus-4-8"], [])
    assert got_n[600] == "msgs", f"無壓縮邊界時仍應為 msgs：{got_n}"

    # 首步也要「有自報就以自報為準」。快取跨 session 共用 → 新 session 第一句仍可能命中上一場
    # 的前綴，此時 tools/system/msgs 才是真成因；標 first 會把「習慣可避免」記進「不可避免」桶，
    # 也讓 ② 與 ②-b／逐步徽章對同一步說法不一。
    raw_f1 = [st2(0, 0, tools_code)]
    assert v.classify_cache_causes(raw_f1, ["claude-opus-4-8"], []) == {0: "tools"}, \
        "首步自報 tools_changed 不得被 first 蓋掉"
    d_f1 = rep(raw_f1)
    assert d_f1["causes_total"]["tools"] == 1 and d_f1["causes_total"]["first"] == 0, \
        f"報告端同理：{ {k: n for k, n in d_f1['causes_total'].items() if n} }"
    assert v.classify_cache_causes([st2(0, 0)], ["claude-opus-4-8"], []) == {0: "first"}, \
        "沒有自報時仍須是 first（不得反向誤判）"

    # 前綴變動步若沒有新寫入，舊 TTL lineage 必須失效——否則下一個冷啟沿用舊 1h cohort、
    # 被判成假的「1h 內卻冷啟」(evict)，正是本工具要消滅的東西。
    def stw(t, cr, wrote, w1h, code=0):
        return [t, cr, 2000, wrote, 0, w1h, 0, code, 0]
    raw_f2 = [stw(0, 0, 2000, 2000), stw(60, 2000, 0, 0),
              stw(200, 1800, 0, 0, tools_code), stw(600, 0, 2000, 2000)]
    got_f2 = v.classify_cache_causes(raw_f2, ["claude-opus-4-8"], [])
    assert got_f2.get(600) == "expiry", f"前綴變動後應落到 unknown lineage，不得是假 evict：{got_f2}"
    assert rep(raw_f2)["causes_total"]["evict"] == 0, "報告端同樣不得產生假 evict"
    # 反面：前綴變動步自己有寫入時 lineage 應由它重建（修正不得矯枉過正、把真 evict 也吃掉）
    raw_f2b = [stw(0, 0, 2000, 2000), stw(60, 2000, 0, 0),
               stw(200, 1800, 2000, 2000, tools_code), stw(600, 0, 2000, 2000)]
    assert v.classify_cache_causes(raw_f2b, ["claude-opus-4-8"], []).get(600) == "evict", \
        "前綴變動步有寫入時，lineage 應重建成 1h"

    # band_excluded ＝「帶內樣本的**完整**減少量」（反事實差額），不是只數被直接排除的那一對：
    # lineage 失效會讓下游的對也從 1h 掉進 unknown、一併離開帶內。此 fixture 減少 2 對、
    # 只數直接排除會 report 1，故有鑑別力。
    raw_f2c = [[600, 1900, 2000, 2000, 0, 2000, 0, 0, 0],
               [1200, 0, 2000, 2000, 0, 0, 0, tools_code, 0],
               [2700, 0, 2000, 0, 0, 2000, 0, 0, 0]]
    on_c, off_c = rep(raw_f2c), rep([r[:7] + [0, 0] for r in raw_f2c])
    drop_c = off_c["kpi"]["comply_n"] - on_c["kpi"]["comply_n"]
    assert drop_c == 2, f"前提：這組反事實應減少 2 對（否則斷言沒鑑別力），實得 {drop_c}"
    assert on_c["api"]["band_excluded"] == drop_c, \
        f"band_excluded 須恆等於帶內樣本減少量：報 {on_c['api']['band_excluded']}、實減 {drop_c}"

    # ②-b 母體＝所有有自報成因的呼叫。ctx < REPORT_MIN_CTX 的過濾只服務「由命中率推論成因」，
    # 對 API 明講的事實不適用；過濾掉會少算次數／長度／金額，還可能吞掉未知的新型別。
    d_f3 = rep([[0, 0, 400, 400, 0, 400, 0, tools_code, 321]])
    assert d_f3["api"]["cold"].get("tools_changed") == 1 and d_f3["api"]["cold_tok"] == 321, \
        f"ctx < {v.REPORT_MIN_CTX} 的自報呼叫不得被靜默吞掉：{d_f3['api']}"

    # 被 REPORT_MIN_CTX 濾掉的前綴變動步，照樣污染它所夾的那一對相鄰步。
    # 不看的話結構性實據被分析門檻吃掉，下游冷啟被誤判成假 evict——實測 ctx 差 1 個
    # token（400 vs 500）結論就整個翻轉，那是分析門檻不該有的權力。
    def stc(t, cr, ctx, wrote, w1h, code=0, missed=0):
        return [t, cr, ctx, wrote, 0, w1h, 0, code, missed]
    for ctx_lo in (400, v.REPORT_MIN_CTX):        # 門檻上下都要得到同一個結論
        raw_h2 = [stc(0, 0, 2000, 2000, 2000),
                  stc(600, 0, ctx_lo, 0, 0, tools_code, 900),   # 低 ctx 的前綴變動
                  stc(1200, 0, 2000, 2000, 2000)]
        d_h2 = rep(raw_h2)
        assert d_h2["causes_total"]["evict"] == 0, \
            f"ctx={ctx_lo}：夾在中間的前綴變動不得讓下游冷啟變成假 evict：" \
            f"{ {k: n for k, n in d_h2['causes_total'].items() if n} }"
        assert d_h2["api"]["excluded"] >= 1 and d_h2["kpi"]["comply_n"] == 0, \
            f"ctx={ctx_lo}：被污染的對必須移出應命中帶：{d_h2['api']} / {d_h2['kpi']['comply_n']}"
        assert v.classify_cache_causes(raw_h2, ["claude-opus-4-8"], []).get(1200) != "evict", \
            f"ctx={ctx_lo}：徽章端同樣不得有假 evict"

    # 同一秒的兩次呼叫不得共用 join 鍵。撞號時後者會蓋掉前者；首步也採用 API 自報成因，
    # 所以後果不只是「推論成因貼錯」，還會是「沒有 diagnostics 的那一步被標成
    # API 自報」＝把推論講成實據，違反最高不變量①。
    def stk(t, code=0):
        return [t, 0, 2000, 2000, 0, 2000, 0, code, 900 if code else 0]
    raw_h1 = [stk(1000), stk(1000, tools_code)]      # 同秒：第 0 步無自報、第 1 步自報 tools
    c_h1 = v.classify_cache_causes(raw_h1, ["claude-opus-4-8"], [])
    assert len(c_h1) == 2, f"同秒兩步必須各有自己的鍵，不得互相覆蓋：{c_h1}"
    seen_h1, got_h1 = {}, []
    for st in raw_h1:                                 # 比照 analyze()：依序數、共用 _cause_key
        t = st[0]
        n = seen_h1.get(t, 0)
        seen_h1[t] = n + 1
        got_h1.append(c_h1.get(v._cause_key(t, n), ""))
    assert got_h1 == ["first", "tools"], \
        f"無 diagnostics 的首步不得被標成 API 自報成因（撞號前是 ['tools','tools']）：{got_h1}"
    assert Counter(got_h1) == Counter({k: n for k, n in rep(raw_h1)["causes_total"].items() if n}), \
        "撞秒時徽章與報告仍須一致"
    # 沒撞秒時鍵必須維持裸 epoch（保證舊資料行為完全不變）
    assert v._cause_key(1000, 0) == 1000 and v._cause_key(1000, 1) == (1000, 1)

    print("OK: api miss reason test passed")


def test_write_ttl_badges(tmp_path=None):
    """寫入 TTL 的呈現（RENDERER_VERSION 61）。

    守三件事，三件都是**安靜**的失敗方向：
    ① 徽章只描述本步寫入——沒有寫入的步不得標（否則會被讀成「這一步的快取只剩 5 分鐘」）；
    ② 1h→5m 的切換要標得出來，而且**只在切換那一步**（每步都標＝噪音，等於看不見切換點）；
    ③ 表頭摘要只計主對話——子代理固定寫 5m，混進來會讓每一場都掛上這行，
       而且會把主對話真正的切換洗掉。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    # ── 單元：檔次判定 ──
    assert v.ttl_tier({"cc5": 0, "cc1h": 900}) == "1h"
    assert v.ttl_tier({"cc5": 900, "cc1h": 0}) == "5m"
    assert v.ttl_tier({"cc5": 9, "cc1h": 9}) == "both"
    assert v.ttl_tier({"cc5": 0, "cc1h": 0}) == "", "純讀取的步沒有 TTL 可言，不得猜一個"
    assert v.ttl_tier({}) == "", "舊資料（無 cache_creation 細分）不得被當成 5m"

    tmp = new_tmp(tmp_path)
    sid = "11110000-0000-4000-8000-0000000011ff"     # 主對話 1h → 5m（中途切換）
    sid2 = "22220000-0000-4000-8000-0000000022ff"    # 全程 1h（表頭不該出現這一行）
    proj = tmp / "projects" / "ttl-proj"
    proj.mkdir(parents=True, exist_ok=True)

    def a(sess, t, mid, cw, cr, uuid, ttl="1h", side=False):
        cc = ({"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": cw} if ttl == "1h"
              else {"ephemeral_5m_input_tokens": cw, "ephemeral_1h_input_tokens": 0})
        u = {"input_tokens": 2, "cache_creation_input_tokens": cw,
             "cache_read_input_tokens": cr, "output_tokens": 30, "cache_creation": cc}
        return {"type": "assistant", "uuid": uuid, "timestamp": t, "sessionId": sess,
                "isSidechain": side, "message": {
                    "role": "assistant", "model": "claude-opus-4-8", "id": mid, "usage": u,
                    "content": [{"type": "text", "text": f"答{mid}"}]}}

    def q(sess, t, uuid, text, side=False):
        return {"type": "user", "uuid": uuid, "timestamp": t, "cwd": "/x/Proj",
                "gitBranch": "main", "version": "2.1.278", "sessionId": sess, "isSidechain": side,
                "message": {"role": "user", "content": text}}

    ev = [
        q(sid, "2026-09-22T01:00:00.000Z", "u1", "問題一TTLQ"),
        a(sid, "2026-09-22T01:00:05.000Z", "t_1", 5000, 0, "a1"),
        a(sid, "2026-09-22T01:00:09.000Z", "t_2", 900, 5000, "a2"),
        q(sid, "2026-09-22T01:20:00.000Z", "u2", "問題二TTLQ"),
        # 純讀取的步夾在兩次寫入之間：它沒有 TTL 可言，**不可以**重設切換的基準——
        # 否則下一步的 1h→5m 會因為「上一步沒有檔次」而標不出來
        a(sid, "2026-09-22T01:20:03.000Z", "t_2r", 0, 5900, "a2r"),
        # 進入 overage：同一條主對話軸上，寫入從 1h 變成 5m（這一步要標 1h→5m）
        a(sid, "2026-09-22T01:20:05.000Z", "t_3", 800, 5900, "a3", ttl="5m"),
        # 之後仍是 5m，但**不是**切換點 → 只標「寫入 TTL 5m」
        a(sid, "2026-09-22T01:20:09.000Z", "t_4", 700, 6700, "a4", ttl="5m"),
        # 子代理：固定 5m，而且不得影響主對話的摘要與切換判定
        q(sid, "2026-09-22T01:21:00.000Z", "s0", "子代理任務", side=True),
        a(sid, "2026-09-22T01:21:05.000Z", "t_s1", 4000, 0, "s1", ttl="5m", side=True),
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev), encoding="utf-8")
    ev2 = [
        q(sid2, "2026-09-22T02:00:00.000Z", "v1", "全程一小時TTLQ"),
        a(sid2, "2026-09-22T02:00:05.000Z", "z_1", 5000, 0, "b1"),
        a(sid2, "2026-09-22T02:00:09.000Z", "z_2", 900, 5000, "b2"),
    ]
    (proj / f"{sid2}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev2), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={tmp / 'projects'}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    pages = {p.name: p.read_text(encoding="utf-8")
             for p in list(_session_pages(out)) + list((out / "sessions").rglob("*.md"))}
    html = next(t for n, t in pages.items() if sid[:8] in n and n.endswith(".html"))
    plain = next(t for n, t in pages.items() if sid2[:8] in n and n.endswith(".html"))
    mdtxt = next(t for n, t in pages.items() if sid[:8] in n and n.endswith(".md"))

    assert 'id="cb_ttl"' in html, "應有『寫入 TTL』勾選框"
    assert "寫入 TTL 1h</span>" in html, "1h 寫入的步應標 1h"
    assert 'class="meter m-ttl sw t5"' in html and "寫入 TTL 1h→5m" in html, \
        "1h→5m 的切換步應標出來，而且落點是 5m ⇒ 要帶 .t5"
    # ⚠⚠ 5m 一律紅底白字、且**不受勾選開關控制**：
    # 子代理可用 CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h 改掉 ⇒ 5m 不是中性事實。
    # 只驗 class 不夠——隱藏規則把它藏起來，紅色一樣沒人看得到，所以三格一起守。
    assert 'class="meter m-ttl t5"' in html, "純 5m 的步要帶 .t5（紅底白字）"
    assert ".meter.m-ttl.t5{background:var(--err);color:#fff" in html, "5m 應沿用 .cold 的紅底白字"
    assert "body.hide-ttl .m-ttl:not(.t5)" in html, (
        "隱藏規則必須排除 .t5，否則預設關的開關會把紅色 5m 一起藏掉")
    assert html.count("寫入 TTL 1h→5m") == 1, \
        f"切換只發生一次，卻標了 {html.count('寫入 TTL 1h→5m')} 次——每步都標等於看不見切換點"
    # 表頭：只計主對話（1h 2 步、5m 2 步；子代理那一步不算）
    assert "寫入 TTL：1h 2 步／5m 2 步" in html, "表頭應彙總主對話各檔次步數（不含子代理）"
    assert "起 1h→5m" in html, "表頭應標出切換時刻"
    assert "寫入 TTL" in mdtxt and "1h→5m" in mdtxt, "MD 表頭也要看得到"
    # 全程 1h 的那一場：表頭不出現這一行（最常見的情形不佔版面），但逐步徽章仍在
    assert "寫入 TTL：" not in plain, "全程 1h 的 session 表頭不應出現寫入 TTL 摘要"
    assert "寫入 TTL 1h</span>" in plain, "逐步徽章與表頭摘要是兩件事，前者照常要有"
    assert "m-ttl t5" not in plain, "全程 1h 的 session 不該出現紅色 5m 徽章"
    # 子代理那一步也是 5m ⇒ 同樣要紅（那是可以改掉的預設，不是理所當然）
    assert html.count("m-ttl t5") >= 2, "子代理的 5m 也要標紅，不得當成中性事實"

    # ── 表頭與逐步徽章必須講同一件事（兩處若各算一次，行序≠時間序時就會分岔）──
    # 檔案行序刻意打亂：時間上是 1h → 5m → 5m，行序卻是 5m、1h、5m。另夾一筆**沒有可呈現內容**的
    # 5m 呼叫（不會有 `_step`、也就沒有徽章）——表頭不可以把它算成一步。
    sid3 = "44440000-0000-4000-8000-0000000044ff"
    ev3 = [
        q(sid3, "2026-09-22T04:00:00.000Z", "w1", "亂序TTLQ"),
        a(sid3, "2026-09-22T04:00:20.000Z", "o_3", 700, 5900, "c3", ttl="5m"),
        a(sid3, "2026-09-22T04:00:05.000Z", "o_1", 5000, 0, "c1"),
        a(sid3, "2026-09-22T04:00:10.000Z", "o_2", 800, 5000, "c2", ttl="5m"),
    ]
    blank = a(sid3, "2026-09-22T04:00:15.000Z", "o_x", 600, 5800, "cx", ttl="5m")
    blank["message"]["content"] = []            # 有 usage、沒有任何可呈現的內容
    ev3.insert(2, blank)
    (proj / f"{sid3}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev3), encoding="utf-8")
    s3 = v.load_session(proj / f"{sid3}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s3)
    steps3 = [b for g in s3.main_groups for b in g.get("blocks", []) if b.get("type") == "_step"]
    badge_tiers = [v.ttl_tier(b.get("u")) for b in steps3]
    badge_sw = [(b["u"].get("ttl_prev"), v.ttl_tier(b["u"])) for b in steps3 if b["u"].get("ttl_prev")]
    assert badge_tiers == ["1h", "5m", "5m"], f"逐步徽章應照時間序：{badge_tiers}"
    assert badge_sw == [("1h", "5m")], f"徽章只該有一次 1h→5m：{badge_sw}"
    assert s3.ttl_n == {"1h": 1, "5m": 2, "both": 0}, \
        f"表頭步數要與徽章同一串步驟（沒有 `_step` 的呼叫不算）：{s3.ttl_n}"
    assert [(a_, b_) for _, a_, b_ in s3.ttl_switches] == badge_sw, \
        f"表頭的切換要與徽章一致，不可以照檔案行序另算出 5m→1h：{s3.ttl_switches}"
    # 沒有 `_step` 的那次 5m 呼叫：不算步，但**要另列**——寫入照樣發生，只是畫不出來
    assert s3.ttl_hidden == {"1h": 0, "5m": 1, "both": 0}, f"隱形呼叫的寫入要另計：{s3.ttl_hidden}"
    assert "另有 1 次沒有可呈現內容的呼叫（5m ×1）" in v._ttl_head(s3)[0], v._ttl_head(s3)[0]

    # 畫得出來的步全是 1h、唯一的 5m 寫入在隱形呼叫裡：表頭**不可以**因為「全程 1h」而整行不出現
    sid5 = "77770000-0000-4000-8000-0000000077ff"
    blank5 = a(sid5, "2026-09-22T06:00:10.000Z", "h_x", 600, 5000, "hx", ttl="5m")
    blank5["message"]["content"] = []
    (proj / f"{sid5}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid5, "2026-09-22T06:00:00.000Z", "y1", "隱形五分TTLQ"),
        a(sid5, "2026-09-22T06:00:05.000Z", "h_1", 5000, 0, "h1"),
        blank5,
        a(sid5, "2026-09-22T06:00:20.000Z", "h_2", 700, 5600, "h2"),
    ]), encoding="utf-8")
    s5 = v.load_session(proj / f"{sid5}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s5)
    head5 = v._ttl_summary_html(s5)
    assert "另有 1 次沒有可呈現內容的呼叫（5m ×1）" in head5 and 'class="warn"' in head5, \
        f"隱形的 5m 寫入要讓表頭出現並上警示色：{head5!r}"

    # 隱形呼叫的兩種邊角：①沒有 message.id（每一筆各自是一次呼叫）；②同一個 id 的第一筆沒有
    # usage、第二筆才有——usage 要取第一筆**帶 usage 的**，不是第一筆事件
    sid7 = "99990000-0000-4000-8000-0000000099ff"
    anon = a(sid7, "2026-09-22T08:00:10.000Z", "unused", 600, 5000, "an1", ttl="5m")
    anon["message"].pop("id")
    anon["message"]["content"] = []
    split_a = a(sid7, "2026-09-22T08:00:12.000Z", "g_s", 0, 0, "sp1")
    split_a["message"]["content"] = []
    split_a["message"].pop("usage")
    split_b = a(sid7, "2026-09-22T08:00:13.000Z", "g_s", 500, 5000, "sp2", ttl="5m")
    split_b["message"]["content"] = []
    (proj / f"{sid7}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid7, "2026-09-22T08:00:00.000Z", "w7", "隱形邊角TTLQ"),
        a(sid7, "2026-09-22T08:00:05.000Z", "g_1", 5000, 0, "g1"),
        anon, split_a, split_b,
        a(sid7, "2026-09-22T08:00:20.000Z", "g_2", 700, 5600, "g2"),
    ]), encoding="utf-8")
    s7 = v.load_session(proj / f"{sid7}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s7)
    assert s7.ttl_hidden == {"1h": 0, "5m": 2, "both": 0}, (
        "沒有 id 的隱形呼叫、以及「第一筆沒有 usage」的隱形呼叫都要算進去："
        f"{s7.ttl_hidden}")

    # ── 沒有 TTL 細分的寫入（舊資料）：有其他檔次時要另列，否則「不顯示」會被讀成「全程 1h」──
    def legacy_a(sess, t, mid, cw, uuid, blank=False):
        e = a(sess, t, mid, cw, 0, uuid)
        e["message"]["usage"].pop("cache_creation")          # 只剩 cache_creation_input_tokens
        if blank:
            e["message"]["content"] = []
        return e
    sid8 = "aaaa0000-0000-4000-8000-00000000aaff"
    (proj / f"{sid8}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid8, "2026-09-22T09:00:00.000Z", "l1", "舊資料混用TTLQ"),
        a(sid8, "2026-09-22T09:00:05.000Z", "l_1", 5000, 0, "la1"),
        legacy_a(sid8, "2026-09-22T09:00:10.000Z", "l_2", 600, "la2"),              # 畫得出來的舊資料寫入
        legacy_a(sid8, "2026-09-22T09:00:12.000Z", "l_3", 600, "la3", blank=True),  # 隱形的舊資料寫入
    ]), encoding="utf-8")
    s8 = v.load_session(proj / f"{sid8}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s8)
    head8 = v._ttl_head(s8)[0]
    assert "TTL 未知 1 步" in head8 and "TTL 未知 ×1" in head8, (
        f"有 1h 又有沒有細分的寫入時，表頭不可以因為「全程 1h」而不顯示：{head8!r}")
    # 整場都是舊資料：沒有任何檔次可講 ⇒ 照樣不顯示（花費組成表另有一列）
    sid9 = "bbbb0000-0000-4000-8000-00000000bbff"
    (proj / f"{sid9}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid9, "2026-09-22T10:00:00.000Z", "m1", "全是舊資料TTLQ"),
        legacy_a(sid9, "2026-09-22T10:00:05.000Z", "o_1", 5000, "oa1"),
    ]), encoding="utf-8")
    s9 = v.load_session(proj / f"{sid9}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s9)
    assert v._ttl_head(s9)[0] == "", f"整場都是舊資料時不顯示這一行：{v._ttl_head(s9)[0]!r}"

    # ── 提示文字只講觀察到的寫入：不替資料推斷成因，也不保證未來一定重寫 ──
    tip = v._ttl_head(s3)[1] + v._TTL_TIP
    assert "資料本身不帶成因" in tip and "多半是帳號進入" not in tip, f"提示不可以推斷成因：{tip!r}"
    assert "回來時要重寫" not in tip, f"提示不可以保證未來一定重寫：{tip!r}"

    # ── 「距上一步」：中間夾一步沒有時間戳的，下一步仍量到**上一個有時間的步** ──
    sid10 = "cccc0000-0000-4000-8000-00000000ccff"
    notime = a(sid10, "2026-09-22T11:00:07.000Z", "n_2", 0, 5000, "nb2")
    notime.pop("timestamp")
    (proj / f"{sid10}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid10, "2026-09-22T11:00:00.000Z", "p1", "缺時間戳TTLQ"),
        a(sid10, "2026-09-22T11:00:05.000Z", "n_1", 5000, 0, "nb1"),
        notime,
        a(sid10, "2026-09-22T11:00:15.000Z", "n_3", 0, 5000, "nb3"),
    ]), encoding="utf-8")
    s10 = v.load_session(proj / f"{sid10}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s10)
    st10 = {b.get("mid"): b for g in s10.main_groups for b in g.get("blocks", []) if b.get("type") == "_step"}
    assert st10["n_3"].get("gap") == 10, (
        f"缺時間戳的步不可以打斷「距上一步」：n_3 應距 n_1 10 秒，實得 {st10['n_3'].get('gap')!r}")
    assert st10["n_2"].get("gap") is None, "沒有時間戳的那一步自己不該有「距上一步」"

    # ── 同一個檔裡的兩個子代理（檔內 sidechain、各帶 agentId、對不回父 Task）──
    sid6 = "88880000-0000-4000-8000-0000000088ff"
    ev6 = [q(sid6, "2026-09-22T07:00:00.000Z", "z1", "檔內兩代理TTLQ"),
           a(sid6, "2026-09-22T07:00:05.000Z", "i_1", 5000, 0, "i1")]
    for name, t0, ttl_, mid in (("agent-P", "07:01", "1h", "ip_1"), ("agent-Q", "07:03", "5m", "iq_1")):
        for e in (q(sid6, f"2026-09-22T{t0}:00.000Z", f"{name}-u", f"任務{name}", side=True),
                  a(sid6, f"2026-09-22T{t0}:05.000Z", mid, 3000, 0, f"{name}-a", ttl=ttl_, side=True)):
            e["agentId"] = name
            ev6.append(e)
    (proj / f"{sid6}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev6), encoding="utf-8")
    s6 = v.load_session(proj / f"{sid6}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s6)
    side6 = [b for g in s6.subagent_map.get("", []) for b in g.get("blocks", []) if b.get("type") == "_step"]
    q_step = next(b for b in side6 if b.get("mid") == "iq_1")
    assert not q_step["u"].get("ttl_prev") and q_step.get("gap") is None, (
        "同一個檔裡的兩個代理也要各自一條軸（身分靠 agentId）："
        f"ttl_prev={q_step['u'].get('ttl_prev')!r} gap={q_step.get('gap')!r}")

    # ── 子代理的軸以代理身分分：父 Task 對不上的兩個子代理會落進同一組，不可以串成一條軸 ──
    sid4 = "55550000-0000-4000-8000-0000000055ff"
    (proj / f"{sid4}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid4, "2026-09-22T05:00:00.000Z", "x1", "兩個子代理TTLQ"),
        a(sid4, "2026-09-22T05:00:05.000Z", "p_1", 5000, 0, "d1"),
    ]), encoding="utf-8")
    subdir = proj / sid4 / "subagents"
    subdir.mkdir(parents=True, exist_ok=True)
    for name, t0, ttl_, mid in (("agent-A", "05:01", "1h", "sa_1"), ("agent-B", "05:03", "5m", "sb_1")):
        side_ev = [q(sid4, f"2026-09-22T{t0}:00.000Z", f"{name}-u", f"任務{name}", side=True),
                   a(sid4, f"2026-09-22T{t0}:05.000Z", mid, 3000, 0, f"{name}-a", ttl=ttl_, side=True)]
        for e in side_ev:
            e["agentId"] = name
        # 刻意不放 `<name>.meta.json` ⇒ 對不回父 Task，兩個代理落進同一組
        (subdir / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in side_ev), encoding="utf-8")
    s4 = v.load_session(proj / f"{sid4}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s4)
    assert list(s4.subagent_map) == [""], f"前提：兩個代理都落進同一組，實得 {list(s4.subagent_map)}"
    side_steps = [b for g in s4.subagent_map[""] for b in g.get("blocks", []) if b.get("type") == "_step"]
    b_step = next(b for b in side_steps if b.get("mid") == "sb_1")
    assert not b_step["u"].get("ttl_prev"), \
        f"代理 B 的第一次寫入不是切換，不可以接著代理 A 的 1h 標成 1h→5m：{b_step['u'].get('ttl_prev')!r}"
    assert b_step.get("gap") is None, \
        f"代理 B 的第一步沒有「上一步」，不可以量到代理 A 的最後一步：{b_step.get('gap')!r}"

    # --- 同一次呼叫被使用者回合切成兩段：畫成兩個步，表頭只能算一次------------
    # 花費組成表依 message.id 去重、只算一次；表頭若算兩次，就是同一場兩處講出兩個數字。
    sid11 = "dddd0000-0000-4000-8000-00000000ddff"
    (proj / f"{sid11}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid11, "2026-09-22T12:00:00.000Z", "d1", "切段TTLQ"),
        a(sid11, "2026-09-22T12:00:05.000Z", "m_1", 5000, 0, "dm1"),
        a(sid11, "2026-09-22T12:00:08.000Z", "m_2", 700, 5000, "dm2a", ttl="5m"),
        q(sid11, "2026-09-22T12:00:09.000Z", "d2", "中間插進來的使用者回合TTLQ"),
        a(sid11, "2026-09-22T12:00:10.000Z", "m_2", 700, 5000, "dm2b", ttl="5m"),
    ]), encoding="utf-8")
    s11 = v.load_session(proj / f"{sid11}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s11)
    _mids11 = [b.get("mid") for g in s11.main_groups for b in g.get("blocks", []) if b.get("type") == "_step"]
    assert _mids11.count("m_2") == 2, f"前提：同一個 id 要真的畫成兩個步：{_mids11}"
    assert s11.ttl_n == {"1h": 1, "5m": 1, "both": 0}, (
        f"⚠⚠ 同一次呼叫的寫入算了兩次（花費組成表只算一次）：{s11.ttl_n}")
    assert "5m 1 步" in v._ttl_head(s11)[0], v._ttl_head(s11)[0]

    # --- 沒有時間戳的步不參與切換判定（檔案是 1h→5m，表頭卻講 5m→1h）------------
    # 缺時間戳的事件在時間序裡排最前面（那是假的位置），拿它當基準會把方向講反。
    sid12 = "eeee0000-0000-4000-8000-00000000eeff"
    _nt12 = a(sid12, "2026-09-22T13:00:09.000Z", "k_2", 700, 5000, "kb2", ttl="5m")
    _nt12.pop("timestamp")
    (proj / f"{sid12}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        q(sid12, "2026-09-22T13:00:00.000Z", "k0", "缺時間戳切換TTLQ"),
        a(sid12, "2026-09-22T13:00:05.000Z", "k_1", 5000, 0, "kb1"),
        _nt12,
    ]), encoding="utf-8")
    s12 = v.load_session(proj / f"{sid12}.jsonl", "ttl-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s12)
    assert s12.ttl_switches == [], (
        f"⚠⚠ 缺時間戳的步被拿來當切換基準，方向講反了：{s12.ttl_switches}")
    assert not any((b.get("u") or {}).get("ttl_prev") for g in s12.main_groups
                   for b in g.get("blocks", []) if b.get("type") == "_step"), "徽章也不可以標出那個假切換"
    assert s12.ttl_n == {"1h": 1, "5m": 1, "both": 0}, f"缺時間戳的步，寫入照樣要算進步數：{s12.ttl_n}"

    # --- 提示文字：不可以有 Markdown 記號、不可以帶作者機器上的觀察、不可以說「子代理另計」----
    # tooltip 是純文字 title 屬性，`**`／反引號會原樣露出；「本機實測…overage」讀的人會以為是他的機器；
    # 子代理的寫入沒有在任何地方彙總，「另計」是不存在的東西。
    _tips = v._TTL_TIP + v._ttl_head(s3)[1]
    assert "**" not in _tips and "`" not in _tips, f"提示文字裡有 Markdown 記號：{_tips!r}"
    assert "本機實測" not in _tips and "overage" not in _tips, f"提示文字帶著作者機器上的觀察：{_tips!r}"
    assert "另計" not in v._ttl_head(s3)[1], f"子代理的寫入沒有另外彙總，不可以寫「另計」：{v._ttl_head(s3)[1]!r}"
    _titles = re.findall(r'class="meter m-ttl[^"]*" title="([^"]*)"', html)
    assert _titles and not any("**" in t or "`" in t or "&#x60;" in t for t in _titles), (
        "渲染出來的徽章 title 也要乾淨（不是只驗常數）")

    # --- MD 的回合列要帶寫入 TTL：切換那一則標切換、5m 的標 5m、純 1h 不標----------
    # MD 不畫逐步列；只有表頭說「有切換」的話，每一則都找不到是哪一則。
    _md_turns = [l for l in mdtxt.splitlines() if "⚡" in l and "寫入 TTL：" not in l]
    assert sum("寫入 TTL 1h→5m" in l for l in _md_turns) == 1, (
        f"MD 裡切換那一則要標「寫入 TTL 1h→5m」，而且只有那一則：{[l[-60:] for l in _md_turns]}")
    assert any(re.search(r"寫入 TTL 5m(?![+→])", l) for l in _md_turns), "子代理那一則（5m）要標出來"
    assert not any(re.search(r"寫入 TTL 1h(?![→+])", l) for l in _md_turns), "純 1h 是常態，MD 不標"

    # --- `5m+1h` 混合步維持中性：只有「落點是純 5m」才紅（m08）---------------------
    _mixed = v._ttl_meter({"cc5": 9, "cc1h": 9})
    _mixed_sw = v._ttl_meter({"cc5": 9, "cc1h": 9, "ttl_prev": "1h"})
    assert "5m+1h" in _mixed and "t5" not in _mixed, f"混合步不是純 5m，紅色會誇大：{_mixed}"
    assert "1h→5m+1h" in _mixed_sw and "t5" not in _mixed_sw, f"切到混合也不是純 5m：{_mixed_sw}"
    assert " t5" in v._ttl_meter({"cc5": 9, "cc1h": 0}), "對照：純 5m 要紅"

    print("OK: write TTL badges test passed")


def test_cost_mix_table(tmp_path=None):
    """表頭的「花費組成」表（RENDERER_VERSION 61）。

    守兩件事：
    ① **組成的加總要等於表頭那個總額**——兩處各算一套是這種表最典型的安靜錯誤，
       畫面上看起來都很合理，但讀者拿它做決策（「寫入佔多少、值不值得改 TTL」）會被誤導；
    ② 0 的列要留著。長對話「新輸入 0.0%」本身就是資訊，省略會讓人以為漏算。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    # 單元：拆解的加總 == call_cost
    args = ("claude-opus-4-8", 100, 5000, 400000, 800, 1200, 3800)
    parts = v.call_cost_parts(*args)
    assert parts is not None
    assert abs(sum(parts.values()) - v.call_cost(*args)) < 1e-12, \
        "組成的加總必須等於 call_cost，否則表與總額會各說各話"
    assert v.call_cost_parts("claude-zzznew-9", 1, 1, 1, 1) is None, "未知模型要回 None"
    # 舊資料（沒有 TTL 細分）的寫入量：按 5m 計價（與 call_cost 口徑一致），但**另放一格**——
    # 記成「快取寫入 5m」就是在講資料沒說的事（那個 5m 是估價口徑，不是觀察到的 TTL）
    legacy = v.call_cost_parts("claude-opus-4-8", 0, 1000, 0, 0)
    assert legacy["wx"] > 0 and legacy["w5"] == 0 and legacy["w1h"] == 0, \
        f"無細分的寫入要放進 TTL 未知那一格：{legacy}"
    assert abs(legacy["wx"] - v.call_cost_parts("claude-opus-4-8", 0, 1000, 0, 0, 1000, 0)["w5"]) < 1e-15, \
        "TTL 未知那一格按 5m 計價，金額要與真的 5m 寫入相同"
    # ⚠⚠ 總額要與**最初那一條算式**逐位元相同：先把各項加起來、最後才除一次。
    #   「各項各除一次再加」（不論用 `sum()` 或逐項 `+`）在大約四分之一的輸入上差最後一位，
    #   恰好落在半分邊界時就顯示成不同的分——下面每一組都是實際會分岔的數字。
    def _v60(model, inp, cc, cr, o, c5, c1h):
        pin, pout = v.model_price(model)
        legacy = max(cc - c5 - c1h, 0)
        write = (legacy + c5) * v.CACHE_WRITE_MULT + c1h * v.CACHE_WRITE_MULT_1H
        return (inp * pin + write * pin + cr * pin * v.CACHE_READ_MULT + o * pout) / 1_000_000
    _m = "claude-sonnet-4-6"
    assert v.model_price(_m), "前提：這個型號要有單價"
    _pin, _pout = v.model_price(_m)
    for _args in ((_m, 0, 4000, 0, 0, 2109, 0),                                # 部分細分的寫入
                  ("claude-opus-4-8", 7, 4000, 12345, 321, 2501, 0),            # 「各項加總」會分岔
                  ("claude-haiku-4-5-20251001", 10, 24718, 0, 145, 0, 24718),  # 「sum()」與逐項 + 會分岔
                  ("claude-haiku-4-5", 1006, 7657, 5715, 690, 3800, 535)):     # 各除再加：$0.01 → $0.02
        assert v.model_price(_args[0]), f"前提：{_args[0]} 要有單價"
        assert v.call_cost(*_args) == _v60(*_args), (
            f"總額要與最初的算式逐位元相同：{_args} → {v.call_cost(*_args)!r} vs {_v60(*_args)!r}")
    assert v.cost_label(v.call_cost("claude-haiku-4-5", 1006, 7657, 5715, 690, 3800, 535), False) \
        == v.cost_label(_v60("claude-haiku-4-5", 1006, 7657, 5715, 690, 3800, 535), False), "顯示的分也要相同"
    # 「5m 那一列」要直接由已知的 5m 寫入量算：用「合併項減掉無細分項」會多出浮點尾數，
    # 恰好在半分邊界時顯示成不同的分（4000 個已知 5m ＋ 4 個無細分：$0.01 → $0.02）
    _p5 = v.call_cost_parts(_m, 0, 4004, 0, 0, 4000, 0)
    assert _p5["w5"] == 4000 * v.CACHE_WRITE_MULT * _pin / 1_000_000, \
        f"已知的 5m 寫入金額要直接算，不可以用減的：{_p5['w5']!r}"

    tmp = new_tmp(tmp_path)
    sid = "33330000-0000-4000-8000-0000000033ff"
    proj = tmp / "projects" / "mix-proj"
    proj.mkdir(parents=True, exist_ok=True)
    ev = [
        {"type": "user", "uuid": "m1", "timestamp": "2026-09-22T03:00:00.000Z", "cwd": "/x/Proj",
         "gitBranch": "main", "version": "2.1.278", "sessionId": sid, "isSidechain": False,
         "message": {"role": "user", "content": "問題COSTMIXQ"}},
        {"type": "assistant", "uuid": "m2", "timestamp": "2026-09-22T03:00:05.000Z",
         "sessionId": sid, "isSidechain": False, "message": {
             "role": "assistant", "model": "claude-opus-4-8", "id": "x_1",
             "usage": {"input_tokens": 10, "cache_creation_input_tokens": 4000,
                       "cache_read_input_tokens": 500000, "output_tokens": 900,
                       "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                          "ephemeral_1h_input_tokens": 4000}},
             "content": [{"type": "text", "text": "答COSTMIX"}]}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={tmp / 'projects'}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = list(_session_pages(out))[0].read_text(encoding="utf-8")
    mdtxt = list((out / "sessions").rglob("*.md"))[0].read_text(encoding="utf-8")

    assert 'class="costmix"' in html and "花費組成" in html, "表頭應有花費組成表"
    for lbl in ("新輸入", "快取寫入 5m", "快取寫入 1h", "快取讀取", "產出"):
        assert lbl in html, f"組成表應有「{lbl}」這一列（0 的列也要留）"
    # 佔比加總要落在 100%（容忍逐列四捨五入）
    pcts = [float(x) for x in re.findall(r'<td class="mono">([0-9.]+)%</td>', html)]
    assert len(pcts) == 5, f"應有五列佔比，實際 {len(pcts)}"
    assert abs(sum(pcts) - 100.0) < 0.3, f"佔比加總應為 100%，實際 {sum(pcts)}"
    # 這筆素材：讀取 50 萬 × 0.1× 遠大於其他項 ⇒ 讀取必須是最大的一列
    assert pcts[3] == max(pcts), f"讀取應佔最大宗，實際各列 {pcts}"
    assert "花費組成（估算）：" in mdtxt and "快取讀取" in mdtxt, "MD 也要有同一份組成"
    assert "TTL 未知" not in html, "有 TTL 細分的資料不該出現 TTL 未知那一列"

    # ⚠ 佔比對「每一列同乘一個倍數」是不變的，只驗佔比等於沒驗金額。
    # 金額要直接對表頭的總額（同一個 `s.cost`，未四捨五入）。
    s = v.load_session(proj / f"{sid}.jsonl", "mix-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s)
    assert s.cost > 0 and abs(sum(s.cost_mix.values()) - s.cost) < 1e-12, \
        f"組成表各列金額加總必須等於表頭總額：{sum(s.cost_mix.values())!r} vs {s.cost!r}"

    # 舊資料（沒有 TTL 細分）的一場：出現 TTL 未知那一列，而且「快取寫入 5m」那一列是 0
    sid_l = "66660000-0000-4000-8000-0000000066ff"
    ev_l = [dict(ev[0], sessionId=sid_l, uuid="L1"),
            dict(ev[1], sessionId=sid_l, uuid="L2", message=dict(ev[1]["message"], id="x_L", usage={
                "input_tokens": 10, "cache_creation_input_tokens": 4000,
                "cache_read_input_tokens": 500000, "output_tokens": 900}))]
    (proj / f"{sid_l}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev_l), encoding="utf-8")
    s_l = v.load_session(proj / f"{sid_l}.jsonl", "mix-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s_l)
    rows = {lbl: usd for lbl, usd, _ in v.cost_mix_rows(s_l)}
    assert rows.get("快取寫入（TTL 未知，按 5m 計）", 0) > 0, f"舊資料要出現 TTL 未知那一列：{rows}"
    assert rows.get("快取寫入 5m") == 0, f"舊資料的寫入不可記成 5m：{rows}"
    assert abs(sum(s_l.cost_mix.values()) - s_l.cost) < 1e-12, "舊資料同樣要與表頭總額一致"
    assert "TTL 未知" in v._cost_mix_html(s_l), "HTML 的組成表也要有那一列"

    # --- ⚠⚠ **場次總額**要逐位元等於「每次呼叫的 `call_cost` 依序相加」（m01）--------
    # 上面只釘了 `call_cost` 這個函式本身；`_collect_usage` 若改成累加組成表的各項，整場的總額會在
    # 很多輸入上差最後一位（合成語料 151 場裡 89 場），而整套測試照樣綠。
    # （這三筆是用隨機搜尋找到的「逐次 call_cost 相加」與「各項加總」會分岔的形狀：0.75826874999… vs 0.75826875）
    _calls = [("claude-opus-4-8", 1335, 22358, 582423, 1976, 16985, 4347),
              ("claude-haiku-4-5", 434, 28941, 260565, 783, 18359, 930),
              ("claude-sonnet-4-6", 1134, 1382, 474140, 2300, 200, 1039)]
    sid_t = "99990000-0000-4000-8000-0000000099ee"
    _ev_t = [dict(ev[0], sessionId=sid_t, uuid="T0")]
    for _n, (_mdl, _i, _cc, _cr, _o, _c5, _c1h) in enumerate(_calls):
        _ev_t.append({"type": "assistant", "uuid": f"T{_n + 1}",
                      "timestamp": f"2026-09-22T03:0{_n + 1}:00.000Z", "sessionId": sid_t, "isSidechain": False,
                      "message": {"role": "assistant", "model": _mdl, "id": f"tot_{_n}",
                                  "usage": {"input_tokens": _i, "cache_creation_input_tokens": _cc,
                                            "cache_read_input_tokens": _cr, "output_tokens": _o,
                                            "cache_creation": {"ephemeral_5m_input_tokens": _c5,
                                                               "ephemeral_1h_input_tokens": _c1h}},
                                  "content": [{"type": "text", "text": f"答TOTAL{_n}"}]}})
    (proj / f"{sid_t}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in _ev_t), encoding="utf-8")
    s_t = v.load_session(proj / f"{sid_t}.jsonl", "mix-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s_t)
    _exp, _alt = 0.0, 0.0
    for _c in _calls:
        _exp += v.call_cost(*_c)
        _alt += sum(v.call_cost_parts(*_c).values())
    assert _exp != _alt, "前提：這組呼叫要是「各項加總」與總額算式會分岔的形狀，否則這一格驗不到東西"
    assert s_t.cost == _exp, (
        f"⚠⚠ 場次總額不是逐次 `call_cost` 相加：{s_t.cost!r} vs {_exp!r}（各項加總會是 {_alt!r}）")

    print("OK: cost mix table test passed")


def test_split_call_usage_last_event(tmp_path=None):
    """同一次呼叫（同一個 `message.id`）拆成好幾筆事件時，usage 取**最後一筆帶 usage 的那筆**（RENDERER_VERSION 64）。

    各筆的輸入與快取欄位相同，但 `output_tokens` 是寫到那一刻為止的產出（前面幾筆常是個位數，
    最後一筆才是整次呼叫的總產出）。取第一筆會讓總額、花費組成、回合與逐步的「↑產出」一起少算。
    守三件事：
    ① 總額／產出／花費組成用的是最後一筆，而且彼此一致；
    ② 回合與逐步徽章同一個口徑（各自依 id 去重、取先遇到的那筆——統一要在那之前做）；
    ③ 第一筆沒有 usage、後面才有的呼叫，那一步照樣有徽章、總額照樣算到。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    tmp = new_tmp(tmp_path)
    sid = "64640000-0000-4000-8000-00000000640a"
    proj = tmp / "projects" / "split-proj"
    proj.mkdir(parents=True, exist_ok=True)
    mdl = "claude-opus-4-8"

    def _u(o):
        return {"input_tokens": 12, "cache_creation_input_tokens": 3000,
                "cache_read_input_tokens": 200000, "output_tokens": o,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 3000}}

    def _a(uuid, ts, mid, content, usage):
        m = {"role": "assistant", "model": mdl, "id": mid, "content": content}
        if usage is not None:
            m["usage"] = usage
        return {"type": "assistant", "uuid": uuid, "timestamp": ts, "sessionId": sid,
                "isSidechain": False, "message": m}

    ev = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-10-07T03:00:00.000Z", "cwd": "/x/Proj",
         "gitBranch": "main", "version": "2.1.300", "sessionId": sid, "isSidechain": False,
         "message": {"role": "user", "content": "問題SPLITQ"}},
        # 呼叫 A：三筆，產出 3 → 7 → 1234（最後一筆才是總產出）
        _a("a1", "2026-10-07T03:00:02.000Z", "call_A", [{"type": "thinking", "thinking": "想SPLITA"}], _u(3)),
        _a("a2", "2026-10-07T03:00:03.000Z", "call_A", [{"type": "text", "text": "答SPLITA"}], _u(7)),
        _a("a3", "2026-10-07T03:00:04.000Z", "call_A",
           [{"type": "tool_use", "id": "tu_A", "name": "Bash", "input": {"command": "echo hi"}}], _u(1234)),
        {"type": "user", "uuid": "u2", "timestamp": "2026-10-07T03:00:05.000Z", "sessionId": sid,
         "isSidechain": False, "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_A", "content": "hi"}]}},
        # 呼叫 B：第一筆沒有 usage、第二筆才有
        _a("b1", "2026-10-07T03:00:06.000Z", "call_B", [{"type": "text", "text": "答SPLITB"}], None),
        _a("b2", "2026-10-07T03:00:07.000Z", "call_B", [{"type": "text", "text": "續SPLITB"}], _u(2222)),
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in ev), encoding="utf-8")
    # 子代理轉錄檔裡的呼叫 C：同樣拆兩筆，產出 5 → 3333（總額含子代理，口徑要一樣）
    side = proj / sid / "subagents"
    side.mkdir(parents=True, exist_ok=True)
    (side / "agent-split.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        _a("c1", "2026-10-07T03:00:08.000Z", "call_C", [{"type": "text", "text": "子SPLITC"}], _u(5)),
        _a("c2", "2026-10-07T03:00:09.000Z", "call_C", [{"type": "text", "text": "子續SPLITC"}], _u(3333)),
    ]), encoding="utf-8")

    s = v.load_session(proj / f"{sid}.jsonl", "split-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s)
    exp_cost = (v.call_cost(mdl, 12, 3000, 200000, 1234, 0, 3000) + v.call_cost(mdl, 12, 3000, 200000, 2222, 0, 3000)
                + v.call_cost(mdl, 12, 3000, 200000, 3333, 0, 3000))
    assert s.tok_out == 1234 + 2222 + 3333, (
        f"產出要取每次呼叫最後一筆（含子代理轉錄檔）：{s.tok_out}"
        f"（取第一筆會是 8：A 只算到 3、B 整次漏掉、子代理 C 只算到 5）")
    assert s.cost == exp_cost, f"總額要用最後一筆的 usage：{s.cost!r} vs {exp_cost!r}"
    assert abs(sum(s.cost_mix.values()) - s.cost) < 1e-12, "花費組成要與總額同一口徑"
    assert s.usage["cache_create"] == 9000 and s.usage["input"] == 36, \
        f"輸入與快取欄位不可因為統一而重複或遺漏：{s.usage}"

    # 回合與逐步：同一個口徑
    a_turns = [t for t in s.main_groups if t["role"] == "assistant"]
    assert sum(t["u"]["output"] for t in a_turns) == 1234 + 2222, \
        f"回合的 ↑產出 要與表頭一致：{[t['u']['output'] for t in a_turns]}"
    steps = [b for t in a_turns for b in t["blocks"] if b.get("type") == "_step"]
    by_mid = {b["mid"]: b["u"] for b in steps}
    assert by_mid.get("call_A") and by_mid["call_A"]["output"] == 1234, \
        f"逐步徽章的 ↑產出 要取最後一筆：{by_mid.get('call_A')}"
    assert by_mid.get("call_B") is not None and by_mid["call_B"]["output"] == 2222, \
        f"第一筆沒有 usage 的呼叫，那一步照樣要有徽章：{by_mid.get('call_B')}"
    assert by_mid["call_B"]["cc1h"] == 3000, "那一步的寫入 TTL 也要在（切換偵測靠它）"

    # 頁面：逐步徽章畫出最後一筆的產出
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={tmp / 'projects'}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = list(_session_pages(out))[0].read_text(encoding="utf-8")
    assert "↑1.2k</span>" in html and "↑2.2k</span>" in html, "逐步徽章要畫出最後一筆的產出"
    assert ">↑3</span>" not in html, "不可再畫出第一筆的產出"

    # 鍵要帶來源檔：主檔與子代理檔出現**同一個** message.id 時各取各的最後一筆
    # （不帶的話，主檔那一步會拿到子代理檔的 usage——子代理的事件排在主檔之後）
    sid2 = "64640000-0000-4000-8000-00000000640b"
    ev2 = [dict(ev[0], sessionId=sid2, uuid="v1"),
           dict(_a("x1", "2026-10-07T04:00:02.000Z", "dup_1", [{"type": "text", "text": "主DUPA"}], _u(3)), sessionId=sid2),
           dict(_a("x2", "2026-10-07T04:00:03.000Z", "dup_1", [{"type": "text", "text": "主續DUPA"}], _u(800)), sessionId=sid2)]
    (proj / f"{sid2}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in ev2), encoding="utf-8")
    side2 = proj / sid2 / "subagents"
    side2.mkdir(parents=True, exist_ok=True)
    (side2 / "agent-dup.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        dict(_a("y1", "2026-10-07T04:00:04.000Z", "dup_1", [{"type": "text", "text": "子DUPA"}], _u(2)), sessionId=sid2),
        dict(_a("y2", "2026-10-07T04:00:05.000Z", "dup_1", [{"type": "text", "text": "子續DUPA"}], _u(77)), sessionId=sid2),
    ]), encoding="utf-8")
    s2 = v.load_session(proj / f"{sid2}.jsonl", "split-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s2)
    main_steps = [b for t in s2.main_groups if t["role"] == "assistant" for b in t["blocks"] if b.get("type") == "_step"]
    assert [b["u"]["output"] for b in main_steps if b.get("mid") == "dup_1"] == [800], \
        f"主檔那一步要取主檔自己的最後一筆（800），不可拿到子代理檔的：{[b['u'] for b in main_steps]}"

    print("OK: split call usage test passed")


def test_ctx_window_and_coldest(tmp_path=None):
    # 先守兩件事：context 視窗的前綴判定，與冷熱門檻的判定（下面 (1)(2)）。
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    # (1) context 視窗改用裸前綴（同 PRICE_PER_M）：新版 opus/sonnet 不得靜默失去「佔視窗 %」。
    assert v.context_window("claude-opus-5") == 1_000_000, "新版 opus 也要對得到視窗（裸前綴 fail-open）"
    assert v.context_window("claude-opus-4-8") == 1_000_000
    assert v.context_window("claude-sonnet-5-20260101") == 1_000_000
    assert v.context_window("claude-3-5-sonnet-20241022") == 200_000, "3.x 世代仍落 200k fallback"
    assert v.context_window("gpt-5.5") is None, "非 Claude 查不到就回 None（只顯示絕對值）"
    # 1M 是**分版本**的，不能用裸前綴一律當 1M：opus/sonnet 要 4.6 以上才是 1M。
    # 一律當 1M 的話，sonnet-4-5 的 100k 脈絡會被算成 10%（實際 50%），差 5 倍。
    for m in ("claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
              "claude-sonnet-4-6", "claude-sonnet-5", "claude-fable-5", "claude-opus-6"):
        assert v.context_window(m) == 1_000_000, f"{m} 應為 1M，實得 {v.context_window(m)}"
    for m in ("claude-opus-4-5", "claude-opus-4-5-20251101", "claude-opus-4-1",
              "claude-opus-4-20250514", "claude-sonnet-4-5-20250929",
              "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"):
        assert v.context_window(m) == 200_000, f"{m} 應為 200k，實得 {v.context_window(m)}"
    # 日期尾段不可被當成次版號（否則 opus-4-20250514 會被讀成「很新」而誤判 1M）
    assert v._model_version("4-20250514") == (4, 0) and v._model_version("4-8") == (4, 8)
    # (2) coldest_step 冷熱判定用整數交叉相乘：24.6% 的最低步要算冷、不得因 round→25% 漏掉 ❄。
    g = {"n_steps": 2, "blocks": [
        {"type": "_step", "idx": 1, "u": {"cache_read": 9000, "total_in": 10000, "output": 0}},
        {"type": "_step", "idx": 2, "u": {"cache_read": 2460, "total_in": 10000, "output": 0}}]}
    mp, mi, mc, mcold, _mm = v.coldest_step(g)
    assert (mp, mcold) == ("24.6", True), f"24.6% 最低步應判冷，且顯示 24.6 不四捨五入：{(mp, mi, mcold)}"
    g2 = {"n_steps": 2, "blocks": [
        {"type": "_step", "idx": 1, "u": {"cache_read": 9000, "total_in": 10000, "output": 0}},
        {"type": "_step", "idx": 2, "u": {"cache_read": 2600, "total_in": 10000, "output": 0}}]}
    assert v.coldest_step(g2)[3] is False, "26% 不應判冷（確保沒有反向誤判）"

    # 同一顯示回合可能併了不同模型的呼叫（opus 1M 視窗 → haiku 200k 視窗）。回合的
    # 「佔視窗 %」必須用 **ctx_max 那一步** 的視窗，不是第一步的——否則回合徽章與逐步徽章
    # 對同一事實給出兩個百分比（實測 19% vs 95%）。HTML 與 MD 都要對。
    def _m(mid, model, cr):
        return {"id": mid, "model": model,
                "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": cr, "output_tokens": 5}}
    acc = v._new_turn_usage()
    v._acc_turn_usage(acc, _m("m1", "claude-opus-4-8", 100_000))             # 1M 視窗、非 ctx_max
    v._acc_turn_usage(acc, _m("m2", "claude-haiku-4-5-20251001", 190_000))   # 200k 視窗、ctx_max 來自這步
    assert acc["ctx_max"] == 190_000 and acc["ctx_win"] == 200_000, \
        f"ctx_win 應跟著 ctx_max 那一步走：{acc['ctx_max']}/{acc['ctx_win']}"
    gw = {"role": "assistant", "n_steps": 2, "blocks": [], "u": acc}
    assert "(95%)" in v.render_turn_meters(gw), f"回合佔比應為 95%：{v.render_turn_meters(gw)[:200]}"
    assert "（95%）" in v.turn_meters_md(gw), f"MD 版同樣要 95%：{v.turn_meters_md(gw)}"
    # 反向：ctx_max 來自大視窗那步時，不得被後來的小視窗汙染
    acc2 = v._new_turn_usage()
    v._acc_turn_usage(acc2, _m("n1", "claude-opus-4-8", 400_000))
    v._acc_turn_usage(acc2, _m("n2", "claude-haiku-4-5-20251001", 50_000))
    assert acc2["ctx_win"] == 1_000_000 and "(40%)" in v.render_turn_meters(
        {"role": "assistant", "n_steps": 2, "blocks": [], "u": acc2}), "反向情境不得誤用小視窗"

    # 「距上一步／effort」若只掛在逐步分隔列上，而單步回合刻意不畫那一列
    # → 單次呼叫的回合（多數）這兩欄完全看不到；MD 更是完全不畫逐步列、單步多步都沒有。
    su_x3 = {"win": 1_000_000, "cache_read": 900, "total_in": 1000, "input": 0, "cache_create": 100,
             "output": 5, "miss": "", "miss_tok": 0, "miss_usd": None, "miss_usd_partial": False,
             "model": "claude-opus-4-8", "effort": "max"}
    tu_x3 = {"input": 0, "cache_create": 100, "cache_read": 900, "output": 5, "ctx_max": 1000,
             "ctx_win": 1_000_000, "cost": 0.0, "unpriced": False, "miss": [], "miss_tok": 0,
             "miss_usd": 0.0, "miss_cold": 0, "miss_usd_partial": False}
    g_solo = {"role": "assistant", "n_steps": 1, "anchor": "", "u": tu_x3,
              "blocks": [{"type": "_step", "idx": 1, "u": su_x3, "gap": 600},
                         {"type": "text", "text": "hi"}]}
    h_solo = v.render_turn_html(g_solo, {}, set())
    assert "距上一步" in h_solo and "effort" in h_solo, f"單步回合也要看得到 gap/effort：{h_solo[:300]}"
    assert "距上一步" in v.turn_meters_md(g_solo) and "effort" in v.turn_meters_md(g_solo), \
        f"MD 版同樣要有：{v.turn_meters_md(g_solo)}"
    # 多步回合走逐步列，回合列不得再補一份（否則同一資訊出現兩次）
    h_multi = v.render_turn_html(dict(g_solo, n_steps=2), {}, set())
    assert h_multi.count("距上一步") == 1, f"多步回合不得重複顯示：出現 {h_multi.count('距上一步')} 次"

    # turn_duration 若在「還沒有開著的 assistant 回合」時抵達（緊接可見 user、同一次呼叫的
    # 內容後到），不可以直接丟掉（那則的 ⏱ 會永遠消失）：要按 message.id 暫存再掛回。
    from datetime import datetime, timezone

    def _ev(role, mid, blocks, ms=None):
        e = {"type": role, "_dt": datetime(2026, 7, 26, tzinfo=timezone.utc),
             "message": {"id": mid, "model": "claude-opus-4-8", "content": blocks,
                         "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 0, "output_tokens": 1}}}
        if ms:
            e["_turn_ms"] = ms
        return e
    turns_x3 = v.group_turns([_ev("user", "u1", [{"type": "text", "text": "q"}]),
                              _ev("assistant", "a1", [], ms=223000),      # 無內容＋耗時，cur 還是 user
                              _ev("assistant", "a1", [{"type": "text", "text": "ans"}])])
    a_x3 = [t for t in turns_x3 if t["role"] == "assistant"]
    assert a_x3 and a_x3[0].get("dur_ms") == 223000, \
        f"同一次呼叫的耗時必須掛回來，不得靜默丟失：{[t.get('dur_ms') for t in a_x3]}"
    # 選「最冷步」也要用精確比值：25.0% 與 24.6% 同 round 成 25 時，要選真正最冷（步2）並標冷。
    g3 = {"n_steps": 2, "blocks": [
        {"type": "_step", "idx": 1, "u": {"cache_read": 2500, "total_in": 10000, "output": 0}},
        {"type": "_step", "idx": 2, "u": {"cache_read": 2460, "total_in": 10000, "output": 0}}]}
    r3 = v.coldest_step(g3)
    assert (r3[1], r3[3]) == (2, True), f"同 round(25%) 時要選真正最冷步並標冷：{r3}"
    # `[1m]` 後綴本身就是視窗大小：走版本表會把它判成 200k，ctx 佔比一口氣差 5 倍
    # （100 萬 ctx 顯示成 500%）。
    assert v.context_window("claude-sonnet-4-5-20250929[1m]") == 1_000_000, \
        "[1m] 後綴要直接決定視窗，不可再走版本表"
    assert v.context_window("claude-sonnet-4-5-20250929") == 200_000, \
        "對照組：沒有後綴的舊版本仍是 200k"

    # 門檻邊界的「顯示」也要與判定一致：24.6% 不得寫成 25%（報告說門檻是「< 25%」）。
    assert v._pct_display(2460, 10000) == "24.6", "剛好進位到門檻時要多給一位小數"
    assert v._pct_display(2500, 10000) == "25", "正好 25% 照常取整（它不是冷啟）"
    # 一位小數也會進位：24.95〜24.99% 不得印成 25.0（否則同一個矛盾只是換個位數重現）
    for cr, tot, want in [(2495, 10000, "24.9"), (2499, 10000, "24.9"), (24999, 100000, "24.9")]:
        assert v._pct_display(cr, tot) == want, \
            f"{100*cr/tot:.4f}% 應顯示 {want}（向下取），得 {v._pct_display(cr, tot)}"
        assert cr * 100 < v.CACHE_COLD_PCT * tot, "前提：這些都該是冷啟"
    assert v._pct_display(9970, 10000) == "100" and v._pct_display(0, 10000) == "0", "其餘照常取整"
    assert r3[0] == "24.6", f"❄最低 也要顯示 24.6 而非 25：{r3[0]!r}"
    # turn_cold_class 的冷熱判定同樣不得先四捨五入（否則整段不著色、❄ 與逐步卻著色）
    g4 = {"n_steps": 2, "u": {"input": 40, "cache_create": 7500, "cache_read": 2460},
          "blocks": [{"type": "_step", "idx": 1, "cause": "expiry",
                      "u": {"total_in": 10000, "cache_read": 2460, "miss": ""}}]}
    assert v.turn_cold_class(g4) == "cold", "彙總 24.6% 應判冷啟（含真失效步→紅），不得因 round 成 25 而放過"
    print("OK: ctx window & coldest step test passed")


def test_codex_ai_label(tmp_path=None):
    # Codex session 的 AI 那方應標「Codex」而非寫死「Claude」；且 MD 標頭放寬後仍能被搜尋正則切出。
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    m = v._TURN_HEAD_RE.match("### 🤖 Codex · 12:00:00  ·  ⚡50% {#t2}")
    assert m and m.group("who") == "🤖 Codex", "搜尋正則應能切出 Codex 回合標頭"

    tmp = new_tmp(tmp_path)
    sess_dir = tmp / "sessions" / "2026" / "06" / "07"
    sess_dir.mkdir(parents=True, exist_ok=True)
    csid = "019f0000-0000-7000-8000-0000000000a1"

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-06-07T01:00:{sec:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)
    rows = [
        line(0, "session_meta", {"id": csid, "cwd": "/x/CodexProj",
                                 "cli_version": "0.29.0", "git": {"branch": "main"}}),
        line(1, "turn_context", {"cwd": "/x/CodexProj", "model": "gpt-5.5"}),
        line(2, "event_msg", {"type": "user_message", "message": "幫我看一下LABELQ。"}),
        line(3, "response_item", {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "好的LABELANS。"}]}),
    ]
    (sess_dir / f"rollout-2026-06-07T01-00-00-{csid}.jsonl").write_text(
        "\n".join(rows), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'sessions'}",
         "--no-claude", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = list(_session_pages(out))[0].read_text(encoding="utf-8")
    md = list((out / "sessions").rglob("*.md"))[0].read_text(encoding="utf-8")
    assert "🤖 Codex" in html and "🤖 Claude" not in html, "Codex session HTML 的 AI 應標 Codex 而非 Claude"
    assert "### 🤖 Codex ·" in md, "Codex session MD 回合標頭應標 Codex"
    assert "🤖 Claude" not in md, "Codex session MD 不應寫死 Claude"

    # 搜尋 Codex 助手回合內容 → 應命中並錨到助手那一則（證明放寬後的 _TURN_HEAD_RE 有把 Codex 回合切出）
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), "--search", "LABELANS"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "命中 0 則" not in r.stdout, f"應搜到 Codex 助手內容\n{r.stdout}{r.stderr}"
    print("OK: codex ai label test passed")


def test_codex_item_completed_user(tmp_path=None):
    # Codex 0.147 互動模式（codex-tui）不再發 event_msg:user_message，改發
    # item_completed(item.type == "UserMessage")；exec 路徑仍發舊格式。兩種都要收得到
    # 使用者 prompt 與標題，且某版兩種都發時只能收一次（不得重複呈現）。
    tmp = new_tmp(tmp_path)
    sess_dir = tmp / "sessions" / "2026" / "06" / "08"
    sess_dir.mkdir(parents=True, exist_ok=True)

    def line(sec, typ, payload):
        # sec 會超過 59（各 fixture 各用一段），要進位到分，否則會產生 03:00:60 這種無效時間
        return json.dumps({"timestamp": f"2026-06-08T03:{sec // 60:02d}:{sec % 60:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    def usermsg_new(sec, text, iid):
        return line(sec, "event_msg", {"type": "item_completed", "item": {
            "type": "UserMessage", "id": iid,
            "content": [{"type": "text", "text": text, "text_elements": []}]}})

    def started(sec):
        return line(sec, "event_msg", {"type": "task_started", "turn_id": f"turn-{sec}"})

    def tc(sec, inp, cached, out_tok):
        return line(sec, "event_msg", {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                 "output_tokens": out_tok, "reasoning_output_tokens": 0,
                                 "total_tokens": inp + out_tok},
            "model_context_window": 272000}})

    # A：只有新格式 → prompt 與標題都要出得來
    sid_a = "019f0004-0000-7000-8000-00000000000a"
    (sess_dir / f"rollout-2026-06-08T03-00-00-{sid_a}.jsonl").write_text("\n".join([
        line(0, "session_meta", {"id": sid_a, "cwd": "/x/NewFmt", "cli_version": "0.147.0",
                                 "originator": "codex-tui"}),
        line(1, "turn_context", {"cwd": "/x/NewFmt", "model": "gpt-5.6"}),
        started(2),
        usermsg_new(3, "新格式問句NEWFMTQ。", "item-1"),
        # 這種 response_item role=user 是注入的脈絡，不可當成使用者 turn
        line(4, "response_item", {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": "注入脈絡INJECTED。"}]}),
        line(5, "response_item", {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "新格式回答NEWFMTA。"}]}),
        # item_completed 的其他 item 型別不得被當成使用者回合，也不得炸掉
        line(6, "event_msg", {"type": "item_completed",
                              "item": {"type": "Reasoning", "id": "r-1", "content": []}}),
        tc(7, 2000, 1800, 10),
    ]), encoding="utf-8")

    # B：**同一檔前半舊格式、後半新格式**（resume 跨版本升級的真實形狀：rollout 是追加的，
    #    格式跟著當下執行模式走）。三則 prompt 落在三個不同回合窗 → 三則都必須收到。
    #    這是「整檔擇一」會靜默吃掉兩則的那個情境。
    sid_b = "019f0005-0000-7000-8000-00000000000b"
    (sess_dir / f"rollout-2026-06-08T03-10-00-{sid_b}.jsonl").write_text("\n".join([
        line(10, "session_meta", {"id": sid_b, "cwd": "/x/Mixed", "cli_version": "0.146.0"}),
        line(11, "turn_context", {"cwd": "/x/Mixed", "model": "gpt-5.6"}),
        started(12),
        line(13, "event_msg", {"type": "user_message", "message": "舊格式問句MIXOLD。"}),
        line(14, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答一MIXA1。"}]}),
        started(15),
        usermsg_new(16, "新格式問句MIXNEW1。", "item-b1"),
        line(17, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答二MIXA2。"}]}),
        started(18),
        usermsg_new(19, "新格式問句MIXNEW2。", "item-b2"),
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答三MIXA3。"}]}),
    ]), encoding="utf-8")

    # C：同一個回合窗內新舊格式發出同一段文字。**兩則都要留**——這個形狀有兩種可能來源
    #    （同一則的雙表示／使用者連送兩次剛好被記成不同格式），兩種格式都沒有可互相關聯的
    #    身分欄位，分不出來；歧義下刪資料就是靜默丟掉一則真實發問。只出聲，不刪。
    sid_c = "019f0006-0000-7000-8000-00000000000c"
    (sess_dir / f"rollout-2026-06-08T03-20-00-{sid_c}.jsonl").write_text("\n".join([
        line(30, "session_meta", {"id": sid_c, "cwd": "/x/Dupe", "cli_version": "0.148.0"}),
        line(31, "turn_context", {"cwd": "/x/Dupe", "model": "gpt-5.6"}),
        started(32),
        line(33, "event_msg", {"type": "user_message", "message": "重複問句DUPEQ。"}),
        usermsg_new(34, "重複問句DUPEQ。", "item-c1"),
        line(35, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "好的DUPEA。"}]}),
        started(36),
        line(37, "event_msg", {"type": "user_message", "message": "重複問句DUPEQ。"}),
        line(38, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "再一次DUPEA2。"}]}),
    ]), encoding="utf-8")

    # D：item.type 改名 ＋ content element type 改名 → 收不到回合，但**必須出聲**（不得靜默）
    sid_d = "019f0007-0000-7000-8000-00000000000d"
    (sess_dir / f"rollout-2026-06-08T03-30-00-{sid_d}.jsonl").write_text("\n".join([
        line(40, "session_meta", {"id": sid_d, "cwd": "/x/Drift", "cli_version": "0.999.0"}),
        line(41, "turn_context", {"cwd": "/x/Drift", "model": "gpt-5.6"}),
        started(42),
        line(43, "event_msg", {"type": "item_completed", "item": {
            "type": "UserMessageItem", "id": "item-d1",       # 型別改名 → 認不得
            "content": [{"type": "text", "text": "漂移問句DRIFTQ。"}]}}),
        line(44, "response_item", {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": "注入脈絡DRIFTCTX。"}]}),
        line(45, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "漂移回答DRIFTA。"}]}),
    ]), encoding="utf-8")

    # E：UserMessage 取不出文字（純圖片 prompt／element type 改名）→ 不得收成空回合，且必須出聲
    sid_e = "019f0008-0000-7000-8000-00000000000e"
    (sess_dir / f"rollout-2026-06-08T03-40-00-{sid_e}.jsonl").write_text("\n".join([
        line(50, "session_meta", {"id": sid_e, "cwd": "/x/Empty", "cli_version": "0.999.0"}),
        line(51, "turn_context", {"cwd": "/x/Empty", "model": "gpt-5.6"}),
        started(52),
        line(53, "event_msg", {"type": "item_completed", "item": {
            "type": "UserMessage", "id": "item-e1",
            "content": [{"type": "image", "image_url": "data:image/png;base64,AA"}]}}),
        started(54),
        usermsg_new(55, "文字問句EMPTYOK。", "item-e2"),
        line(56, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答EMPTYA。"}]}),
    ]), encoding="utf-8")

    # F：同一回合窗內真的重打同一句話（實測本機有 window 含多達 7 則 user 記錄）。
    #    中間隔著助手回答 → 是兩則真回合，判重不得把它刪成一則。
    sid_f = "019f0009-0000-7000-8000-00000000000f"
    (sess_dir / f"rollout-2026-06-08T03-50-00-{sid_f}.jsonl").write_text("\n".join([
        line(60, "session_meta", {"id": sid_f, "cwd": "/x/Repeat", "cli_version": "0.148.0"}),
        line(61, "turn_context", {"cwd": "/x/Repeat", "model": "gpt-5.6"}),
        started(62),
        line(63, "event_msg", {"type": "user_message", "message": "重打同一句REPEATQ。"}),
        line(64, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答一REPEATA1。"}]}),
        usermsg_new(65, "重打同一句REPEATQ。", "item-f1"),   # 同一 window、同文字，但中間有回答
        line(66, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答二REPEATA2。"}]}),
    ]), encoding="utf-8")

    # G：item.type 改成通用名 ＋ role="user"（只比對型別名會整個穿過去而無聲）
    sid_g = "019f000a-0000-7000-8000-00000000000a"
    (sess_dir / f"rollout-2026-06-08T04-00-00-{sid_g}.jsonl").write_text("\n".join([
        line(70, "session_meta", {"id": sid_g, "cwd": "/x/Role", "cli_version": "0.999.0"}),
        line(71, "turn_context", {"cwd": "/x/Role", "model": "gpt-5.6"}),
        started(72),
        line(73, "event_msg", {"type": "item_completed", "item": {
            "type": "Message", "role": "user", "id": "item-g1",
            "content": [{"type": "text", "text": "通用型別問句ROLEQ。"}]}}),
        line(74, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答ROLEA。"}]}),
    ]), encoding="utf-8")

    # H：**反序**（新格式先、舊格式後）。與 C 同一條規則、順序相反：形狀對稱，處置也必須對稱
    #    ——兩則都留、一樣出聲。少了這格，同一種歧義會因為順序不同而得到兩種處置。
    sid_h = "019f000b-0000-7000-8000-00000000000b"
    (sess_dir / f"rollout-2026-06-08T04-10-00-{sid_h}.jsonl").write_text("\n".join([
        line(80, "session_meta", {"id": sid_h, "cwd": "/x/Rev", "cli_version": "0.148.0"}),
        line(81, "turn_context", {"cwd": "/x/Rev", "model": "gpt-5.6"}),
        started(82),
        usermsg_new(83, "反序雙表示REVQ。", "item-h1"),
        line(84, "event_msg", {"type": "user_message", "message": "反序雙表示REVQ。"}),
        line(85, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答REVA。"}]}),
    ]), encoding="utf-8")

    # I：**同格式**連續同文、中間沒有任何內容事件（佇列一次送出兩句一樣的話，實測 window 內
    #    最多有 7 則 user 記錄）。判重只准認「跨格式」的雙表示——同格式連續同文是真的重打兩次，
    #    誤刪就是靜默丟掉一則真實發問。新格式 ×2 與舊格式 ×2 兩邊都要守住。
    sid_i = "019f000c-0000-7000-8000-00000000000c"
    (sess_dir / f"rollout-2026-06-08T04-20-00-{sid_i}.jsonl").write_text("\n".join([
        line(90, "session_meta", {"id": sid_i, "cwd": "/x/Queue", "cli_version": "0.148.0"}),
        line(91, "turn_context", {"cwd": "/x/Queue", "model": "gpt-5.6"}),
        started(92),
        usermsg_new(93, "排隊重送QUEUEQ。", "item-i1"),
        usermsg_new(94, "排隊重送QUEUEQ。", "item-i2"),      # 同格式、緊鄰、同文 → 兩則都要留
        line(95, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答QUEUEA。"}]}),
        started(96),
        line(97, "event_msg", {"type": "user_message", "message": "排隊重送OLDQ。"}),
        line(98, "event_msg", {"type": "user_message", "message": "排隊重送OLDQ。"}),   # 舊格式同理
        line(99, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "答OLDA。"}]}),
    ]), encoding="utf-8")

    # J：漂移到 **event_msg 這一層**（payload.type 本身換名，不再是 user_message／item_completed）。
    #    item 層的哨兵接不到這種，漏掉就又是「整場 prompt 一則不剩卻毫無聲音」。
    sid_j = "019f000d-0000-7000-8000-00000000000d"
    (sess_dir / f"rollout-2026-06-08T04-30-00-{sid_j}.jsonl").write_text("\n".join([
        line(100, "session_meta", {"id": sid_j, "cwd": "/x/EvtDrift", "cli_version": "0.999.0"}),
        line(101, "turn_context", {"cwd": "/x/EvtDrift", "model": "gpt-5.6"}),
        started(102),
        line(103, "event_msg", {"type": "user_prompt", "message": "事件層漂移LOSTQ。"}),
        line(104, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答EVTDRIFTA。"}]}),
        started(105),
        # role=user 的未知型別（型別名不帶 user）同樣要被接住
        line(106, "event_msg", {"type": "turn_input", "role": "user", "message": "角色欄漂移ROLELOST。"}),
        line(107, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答EVTDRIFTB。"}]}),
    ]), encoding="utf-8")

    # K：`user_message` 的**型別還在**，但承載文字的欄位換了位置（message → content）。
    #    型別白名單擋不住這種：它進得了已知分支、卻取不出文字。沒有哨兵就整則靜默消失，
    #    而頁面看起來完整——與 0.147 那次同一種失效樣態，只是漂移點更深一層。
    sid_k = "019f000e-0000-7000-8000-00000000000e"
    (sess_dir / f"rollout-2026-06-08T04-40-00-{sid_k}.jsonl").write_text("\n".join([
        line(110, "session_meta", {"id": sid_k, "cwd": "/x/FieldMove", "cli_version": "0.999.0"}),
        line(111, "turn_context", {"cwd": "/x/FieldMove", "model": "gpt-5.6"}),
        started(112),
        line(113, "event_msg", {"type": "user_message", "content": "欄位搬家FIELDMOVEQ。"}),
        line(114, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答FIELDMOVEA。"}]}),
    ]), encoding="utf-8")

    # L：`item_completed` 的型別還在，但 role/content 從 `item` 那一層**整組搬到 payload 這一層**。
    #    item 層取不到型別也取不到 role → item 層哨兵接不到；payload.type 又是已知的
    #    item_completed → event 層哨兵也進不去。哨兵必須**逐層**都有，否則中間這一層是個洞。
    sid_l = "019f000f-0000-7000-8000-00000000000f"
    (sess_dir / f"rollout-2026-06-08T04-50-00-{sid_l}.jsonl").write_text("\n".join([
        line(120, "session_meta", {"id": sid_l, "cwd": "/x/LayerMove", "cli_version": "0.999.0"}),
        line(121, "turn_context", {"cwd": "/x/LayerMove", "model": "gpt-5.6"}),
        started(122),
        line(123, "event_msg", {"type": "item_completed", "role": "user",
                                "content": [{"type": "text", "text": "層級搬家LAYERMOVEQ。"}]}),
        line(124, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答LAYERMOVEA。"}]}),
    ]), encoding="utf-8")

    # M：**最外層**的型別漂移——事件外殼從 `event_msg` 換成別的名字，payload 仍明確是使用者發言。
    #    內兩層的哨兵都掛在 `event_msg` 那一支底下，根本進不來。哨兵要蓋滿每一層，
    #    少一層就是一個洞，而每個洞的症狀都一樣：頁面看起來完整、prompt 一則不剩。
    sid_m = "019f0010-0000-7000-8000-000000000010"
    (sess_dir / f"rollout-2026-06-08T05-00-00-{sid_m}.jsonl").write_text("\n".join([
        line(130, "session_meta", {"id": sid_m, "cwd": "/x/OuterDrift", "cli_version": "0.999.0"}),
        line(131, "turn_context", {"cwd": "/x/OuterDrift", "model": "gpt-5.6"}),
        started(132),
        line(133, "user_event", {"type": "user_message", "message": "外殼漂移OUTERQ。"}),
        line(134, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答OUTERA。"}]}),
    ]), encoding="utf-8")

    # N：`item` **容器本身**漂了——不是 dict 而是包著 dict 的 list。若直接換成 {}，
    #    item 層每一個哨兵都看不到東西；payload 這一層又不帶 role（實測 485 個 rollout 皆然），
    #    event 層哨兵也接不到 → 整則 prompt 靜默消失、沒有任何一個計數會動。
    sid_n = "019f0011-0000-7000-8000-000000000011"
    (sess_dir / f"rollout-2026-06-08T05-10-00-{sid_n}.jsonl").write_text("\n".join([
        line(140, "session_meta", {"id": sid_n, "cwd": "/x/ItemList", "cli_version": "0.999.0"}),
        line(141, "turn_context", {"cwd": "/x/ItemList", "model": "gpt-5.6"}),
        started(142),
        line(143, "event_msg", {"type": "item_completed", "item": [
            {"type": "UserMessage", "id": "item-n1",
             "content": [{"type": "text", "text": "容器漂移CONTAINERQ。"}]}]}),
        line(144, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答CONTAINERA。"}]}),
    ]), encoding="utf-8")

    # O：**混合內容**的 prompt（文字＋圖片）。抽得到文字 → 不會觸發「取不出文字」那個哨兵，
    #    但圖片那個 block 被靜默丟掉。頁面看起來完整、內容卻少了一半，正是要擋的形狀。
    sid_o = "019f0012-0000-7000-8000-000000000012"
    (sess_dir / f"rollout-2026-06-08T05-20-00-{sid_o}.jsonl").write_text("\n".join([
        line(150, "session_meta", {"id": sid_o, "cwd": "/x/Mixed", "cli_version": "0.148.0"}),
        line(151, "turn_context", {"cwd": "/x/Mixed", "model": "gpt-5.6"}),
        started(152),
        line(153, "event_msg", {"type": "item_completed", "item": {
            "type": "UserMessage", "id": "item-o1",
            "content": [{"type": "text", "text": "混合內容MIXEDQ。"},
                        {"type": "image", "image_url": "data:image/png;base64,AA"}]}}),
        line(154, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答MIXEDA。"}]}),
    ]), encoding="utf-8")

    # P：**`payload` 容器本身**不是 dict（list 包 dict）。清成 {} 之後 ptype 是 None
    #    → 進不了 user_message／item_completed 任何一支，`_codex_event_user_like({}, None)`
    #    也恆為 False → 整則 prompt 靜默消失、每一個計數都停在 0。哨兵要蓋滿每一層容器。
    sid_p = "019f0013-0000-7000-8000-000000000013"
    (sess_dir / f"rollout-2026-06-08T05-30-00-{sid_p}.jsonl").write_text("\n".join([
        line(160, "session_meta", {"id": sid_p, "cwd": "/x/PayloadList", "cli_version": "0.999.0"}),
        line(161, "turn_context", {"cwd": "/x/PayloadList", "model": "gpt-5.6"}),
        started(162),
        line(163, "event_msg", [{"type": "user_message", "message": "payload容器漂移PAYQ。"}]),
        line(164, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答PAYA。"}]}),
    ]), encoding="utf-8")

    # Q：`item` 被**序列化成 JSON 字串**。型別名還在字串裡，但 isinstance(raw_item, dict) 是
    #    False → 換成 {} 之後與 N 同一種失效：整則消失且無聲。只認 list 容器接不到這一格。
    sid_q = "019f0014-0000-7000-8000-000000000014"
    (sess_dir / f"rollout-2026-06-08T05-40-00-{sid_q}.jsonl").write_text("\n".join([
        line(170, "session_meta", {"id": sid_q, "cwd": "/x/ItemStr", "cli_version": "0.999.0"}),
        line(171, "turn_context", {"cwd": "/x/ItemStr", "model": "gpt-5.6"}),
        started(172),
        line(173, "event_msg", {"type": "item_completed", "item": json.dumps(
            {"type": "UserMessage", "id": "item-q1",
             "content": [{"type": "text", "text": "字串容器STRQ。"}]}, ensure_ascii=False)}),
        line(174, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答STRA。"}]}),
    ]), encoding="utf-8")

    # R：`response_item` 的 payload 容器漂移。`response_item` 是**認得**的外層型別，而它的
    #    role=user 本來就是注入的脈絡（AGENTS.md 全文之類）、不是使用者打的字 → 不可歸到
    #    「最外層型別不認得」，那既指錯層也會誤報。
    sid_r = "019f0015-0000-7000-8000-000000000015"
    (sess_dir / f"rollout-2026-06-08T05-50-00-{sid_r}.jsonl").write_text("\n".join([
        line(180, "session_meta", {"id": sid_r, "cwd": "/x/RespItem", "cli_version": "0.999.0"}),
        line(181, "turn_context", {"cwd": "/x/RespItem", "model": "gpt-5.6"}),
        started(182),
        line(183, "event_msg", {"type": "user_message", "message": "正常問句RESPQ。"}),
        line(184, "response_item", [{"type": "message", "role": "user",
                                     "content": [{"type": "input_text", "text": "注入脈絡"}]}]),
        line(185, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答RESPA。"}]}),
    ]), encoding="utf-8")

    # S：型別名**不含 user**、也不帶 role（`Prompt`）。五個既有哨兵的判準同源，這種漂移會讓它們
    #    **同時**是 0——正是 0.147 那次「頁面完整、prompt 一則不剩、stderr 全靜」的樣態。
    #    形狀無關的那一條（有助理回合、卻零使用者回合）必須接住。
    sid_s = "019f0016-0000-7000-8000-000000000016"
    (sess_dir / f"rollout-2026-06-08T06-00-00-{sid_s}.jsonl").write_text("\n".join([
        line(190, "session_meta", {"id": sid_s, "cwd": "/x/Shapeless", "cli_version": "0.999.0"}),
        line(191, "turn_context", {"cwd": "/x/Shapeless", "model": "gpt-5.6"}),
        started(192),
        line(193, "event_msg", {"type": "item_completed", "item": {
            "type": "Prompt", "id": "item-s1",
            "content": [{"type": "text", "text": "無痕漂移SHAPEQ。"}]}}),
        line(194, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答SHAPEA。"}]}),
    ]), encoding="utf-8")

    # T：與 S 同形，但這是**子代理執行緒**（session_meta.source.subagent）——本來就沒有人打字，
    #    形狀無關那條哨兵不得對它出聲，否則每個子代理都會誤報一次。
    sid_t = "019f0017-0000-7000-8000-000000000017"
    (sess_dir / f"rollout-2026-06-08T06-10-00-{sid_t}.jsonl").write_text("\n".join([
        line(200, "session_meta", {"id": sid_t, "cwd": "/x/Sub", "cli_version": "0.999.0",
                                   "source": {"subagent": {"thread_spawn": {
                                       "parent_thread_id": "019fef69-a1b5-7061-a9c6-a9e6652edf15",
                                       "depth": 1}}}}),
        line(201, "turn_context", {"cwd": "/x/Sub", "model": "gpt-5.6"}),
        started(202),
        line(203, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答SUBA。"}]}),
    ]), encoding="utf-8")

    # U：漂移的 prompt ＋**工具結果**。工具結果也是以 `type=="user"` 存的，所以「數 user 事件」
    #    那種寫法在這裡會看到 1、以為沒事——但真實的使用者 prompt 一則都沒收到。
    sid_u = "019f0018-0000-7000-8000-000000000018"
    (sess_dir / f"rollout-2026-06-08T06-20-00-{sid_u}.jsonl").write_text("\n".join([
        line(210, "session_meta", {"id": sid_u, "cwd": "/x/ToolOnly", "cli_version": "0.999.0"}),
        line(211, "turn_context", {"cwd": "/x/ToolOnly", "model": "gpt-5.6"}),
        started(212),
        line(213, "event_msg", {"type": "item_completed", "item": {
            "type": "Prompt", "id": "item-u1",
            "content": [{"type": "text", "text": "漂移問句TOOLQ。"}]}}),
        line(214, "response_item", {"type": "function_call_output", "call_id": "call-u1",
                                    "output": "工具輸出TOOLOUT"}),
        line(215, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答TOOLA。"}]}),
    ]), encoding="utf-8")

    # W：**一場中途才漂移**：前半舊格式收得到、後半漂成未知型別全丟。只看「有沒有收到任何
    #    一則」的哨兵會完全穿過去（收到 1 則就閉嘴），但實際上少了一則真實發問。
    sid_w = "019f0019-0000-7000-8000-000000000019"
    (sess_dir / f"rollout-2026-06-08T06-30-00-{sid_w}.jsonl").write_text("\n".join([
        line(220, "session_meta", {"id": sid_w, "cwd": "/x/MidDrift", "cli_version": "0.999.0"}),
        line(221, "turn_context", {"cwd": "/x/MidDrift", "model": "gpt-5.6"}),
        started(222),
        line(223, "event_msg", {"type": "user_message", "message": "前半收得到MIDQ1。"}),
        line(224, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答MIDA1。"}]}),
        started(225),
        line(226, "event_msg", {"type": "item_completed", "item": {
            "type": "HumanTurn", "id": "item-w1",
            "content": [{"type": "text", "text": "後半全丟MIDQ2。"}]}}),
        line(227, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答MIDA2。"}]}),
    ]), encoding="utf-8")

    # X：**被中止的回合**沒有 prompt 是正常的（真實語料上有三份），不得誤報。
    sid_x = "019f001a-0000-7000-8000-00000000001a"
    (sess_dir / f"rollout-2026-06-08T06-40-00-{sid_x}.jsonl").write_text("\n".join([
        line(230, "session_meta", {"id": sid_x, "cwd": "/x/Aborted", "cli_version": "0.999.0"}),
        line(231, "turn_context", {"cwd": "/x/Aborted", "model": "gpt-5.6"}),
        started(232),
        line(233, "event_msg", {"type": "user_message", "message": "有收到ABORTQ。"}),
        line(234, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答ABORTA。"}]}),
        started(235),
        line(236, "event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
    ]), encoding="utf-8")

    # Y：**一個回合連送多則 prompt**（真實語料上有 38 回合／47 則的 session）。整檔拿
    #    「回合總數 vs prompt 總數」相減的話，這種 session 的漏收會全部沉在門檻底下——
    #    第二個回合整窗空了也不會出聲。逐回合窗記帳才抓得到。
    sid_y = "019f001b-0000-7000-8000-00000000001b"
    (sess_dir / f"rollout-2026-06-08T06-50-00-{sid_y}.jsonl").write_text("\n".join([
        line(240, "session_meta", {"id": sid_y, "cwd": "/x/Queued", "cli_version": "0.999.0"}),
        line(241, "turn_context", {"cwd": "/x/Queued", "model": "gpt-5.6"}),
        started(242),
        line(243, "event_msg", {"type": "user_message", "message": "連送一QUEUE1。"}),
        line(244, "event_msg", {"type": "user_message", "message": "連送二QUEUE2。"}),
        line(245, "event_msg", {"type": "user_message", "message": "連送三QUEUE3。"}),
        line(246, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答QUEUEA。"}]}),
        started(247),
        line(248, "event_msg", {"type": "item_completed", "item": {
            "type": "Prompt", "id": "item-y1",
            "content": [{"type": "text", "text": "整窗空QUEUELOST。"}]}}),
        line(249, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答QUEUEB。"}]}),
    ]), encoding="utf-8")

    # Z：**舊格式** user_message 的 `message` 從字串漂成 content block 陣列。
    #    若只做 `str(payload.get("message") or "")` → Python 的 repr 整包會被當成 prompt 收下，
    #    而三層哨兵全靜：repr 非空（不觸發「取不出文字」）、型別名還在（不觸發型別哨兵）、
    #    不走 item 分支（不數 dropped block）。圖片 block 無聲消失、頁面看起來完整。
    #    新格式那條路徑早就有 `_codex_content_text` ＋ unconsumed 兩層，舊格式這側要對稱。
    sid_z = "019f001c-0000-7000-8000-00000000001c"
    (sess_dir / f"rollout-2026-06-08T07-00-00-{sid_z}.jsonl").write_text("\n".join([
        line(250, "session_meta", {"id": sid_z, "cwd": "/x/LegacyDrift", "cli_version": "0.999.0"}),
        line(251, "turn_context", {"cwd": "/x/LegacyDrift", "model": "gpt-5.6"}),
        started(252),
        line(253, "event_msg", {"type": "user_message", "message": [
            {"type": "text", "text": "舊格式漂移LEGACYDRIFTQ。"},
            {"type": "image", "image_url": "data:image/png;base64,AA"}]}),
        line(254, "response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "答LEGACYDRIFTA。"}]}),
    ]), encoding="utf-8")

    # ZR：`response_item` 的**容器**漂了（list 包 dict）。助理訊息、reasoning 與
    #    工具呼叫全走這一支 → 整場回答消失、頁面是「一則提問、零則回答」。而既有哨兵一個都
    #    接不到：容器層那條只在「看起來像使用者發言」時計數，收尾那條形狀無關的又被
    #    `n_ai_turns` 擋住（助理事件正好是 0）。
    sid_zr = "019f001d-0000-7000-8000-00000000001d"
    (sess_dir / f"rollout-2026-06-08T07-10-00-{sid_zr}.jsonl").write_text("\n".join([
        line(260, "session_meta", {"id": sid_zr, "cwd": "/x/RespDrift", "cli_version": "0.999.0"}),
        line(261, "turn_context", {"cwd": "/x/RespDrift", "model": "gpt-5.6"}),
        started(262),
        line(263, "event_msg", {"type": "user_message", "message": "回答會消失RESPDRIFTQ。"}),
        line(264, "response_item", [{"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": "答RESPDRIFTA。"}]}]),
    ]), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'sessions'}",
         "--no-claude", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    pages = {p.name: p.read_text(encoding="utf-8") for p in _session_pages(out)}
    mds = {p.name: p.read_text(encoding="utf-8") for p in (out / "sessions").rglob("*.md")}

    # 照 manifest 找每一場的頁面，不靠檔名猜：Codex 檔名的 id 段是 sid 最後 8 碼，
    # 這裡好幾場的最後 8 碼相同（只是專案不同、檔名不撞），拿片段去比對檔名會挑錯場
    _rows = {e["row"]["session_id"]: e["row"] for e in json.loads(
        (out / ".build-manifest.json").read_text(encoding="utf-8"))["entries"].values() if e.get("row")}

    def md_of(sid):
        return (out / "sessions" / _rows[sid]["out_md"]).read_text(encoding="utf-8")

    a = (out / "sessions" / _rows[sid_a]["out_html"]).read_text(encoding="utf-8")
    assert a in pages.values()
    assert "NEWFMTQ" in a, "新格式的使用者 prompt 應呈現在 session 頁"
    assert "NEWFMTA" in a, "助手回答應照常呈現"
    assert "INJECTED" not in a, "response_item role=user 的注入脈絡不可被當成使用者 turn"
    assert md_of(sid_a).count("### 👤 You") == 1, "新格式應恰好收出一則使用者回合"

    # B：混合檔三則全在（整檔擇一的話會只剩一則）
    b_md = md_of(sid_b)
    assert b_md.count("### 👤 You") == 3, \
        f"混合格式檔應收出 3 則使用者回合，實得 {b_md.count('### 👤 You')}"
    for mark in ("MIXOLD", "MIXNEW1", "MIXNEW2"):
        assert mark in b_md, f"混合格式檔遺失 prompt：{mark}"

    # C：跨格式緊鄰同文兩則都留（歧義不刪）＋跨窗同文兩則真回合 → 共 3 則
    c_md = md_of(sid_c)
    assert c_md.count("### 👤 You") == 3, \
        f"跨格式緊鄰同文應全部保留，共 3 則使用者回合，實得 {c_md.count('### 👤 You')}"
    assert "緊鄰的跨格式同文" in r.stderr, \
        f"跨格式緊鄰同文應對 stderr 出聲（保留但要讓人看得見），實得 stderr：{r.stderr}"

    # D/E：漂移必須出聲（stderr），不得靜默產出空頁
    assert "型別像使用者訊息卻不認得" in r.stderr, \
        f"item.type 漂移應對 stderr 出聲，實得 stderr：{r.stderr}"
    assert "取不出文字" in r.stderr, f"UserMessage 取不出文字應對 stderr 出聲，實得 stderr：{r.stderr}"
    e_md = md_of(sid_e)
    assert e_md.count("### 👤 You") == 1, "取不出文字的那則不得收成空回合（只剩文字那則）"

    # F：同窗重打同一句（中間隔著回答）→ 是兩則真回合，判重不得誤刪
    f_md = md_of(sid_f)
    assert f_md.count("### 👤 You") == 2, \
        f"同一回合窗內隔著回答重打同一句應收出 2 則，實得 {f_md.count('### 👤 You')}"

    # H：反序也一樣兩則都留（處置必須與 C 對稱）
    h_md = md_of(sid_h)
    assert h_md.count("### 👤 You") == 2, \
        f"新格式先、舊格式後的緊鄰同文應兩則都留，實得 {h_md.count('### 👤 You')}"
    assert "REVQ" in h_md, "prompt 內容仍應呈現"

    # G：通用型別 ＋ role=user → 收不到回合，但必須出聲（只比對型別名會靜默穿過）
    g_md = md_of(sid_g)
    assert g_md.count("### 👤 You") == 0, "認不得的型別不應硬收成回合"
    assert r.stderr.count("型別像使用者訊息卻不認得") >= 2, \
        f"item.type 改名與 role=user 兩種漂移都要出聲，實得 stderr：{r.stderr}"

    # I：同格式緊鄰同文＝真的重打（佇列送兩次），判重只准認跨格式的雙表示 → 4 則全留
    i_md = md_of(sid_i)
    assert i_md.count("### 👤 You") == 4, \
        f"同格式連續同文（新×2＋舊×2）應全部保留共 4 則，實得 {i_md.count('### 👤 You')}"
    # 一則都沒被判重才算真的守住——判重計數器出聲就代表又把真實發問吃掉了
    assert "QUEUEQ" in i_md and "OLDQ" in i_md, "同格式重送的兩句都應呈現"

    # J：event_msg 這一層的漂移（payload.type 換名／改帶 role=user）必須出聲，不得靜默丟光
    j_md = md_of(sid_j)
    assert j_md.count("### 👤 You") == 0, "認不得的事件型別不應硬收成回合"
    assert r.stderr.count("型別像使用者發言卻不認得") >= 1, \
        f"event_msg 層漂移應對 stderr 出聲，實得 stderr：{r.stderr}"
    assert "LOSTQ" not in j_md and "ROLELOST" not in j_md, "沒收下的內容不應憑空出現"

    # K：型別還在、文字欄位搬家 → 收不到回合，但必須出聲（否則整則靜默消失）
    k_md = md_of(sid_k)
    assert k_md.count("### 👤 You") == 0, "取不出文字的那則不得硬收成空回合"
    assert "FIELDMOVEQ" not in k_md, "沒收下的內容不應憑空出現"
    assert r.stderr.count("取不出文字") >= 2, \
        f"user_message 與 UserMessage 兩種欄位漂移都要出聲，實得 stderr：{r.stderr}"

    # L：item 那一層整組搬到 payload 層 → 收不到回合，但哨兵必須接住
    l_md = md_of(sid_l)
    assert l_md.count("### 👤 You") == 0, "認不得的形狀不應硬收成回合"
    assert "LAYERMOVEQ" not in l_md, "沒收下的內容不應憑空出現"
    assert r.stderr.count("型別像使用者訊息卻不認得") >= 3, \
        f"item 層改名、role=user、以及欄位搬到 payload 層三種都要出聲，實得 stderr：{r.stderr}"

    # M：最外層事件型別漂移 → 收不到回合，但哨兵必須接住（內兩層都進不來）
    m_md = md_of(sid_m)
    assert m_md.count("### 👤 You") == 0, "認不得的外層形狀不應硬收成回合"
    assert "OUTERQ" not in m_md, "沒收下的內容不應憑空出現"
    assert "最外層型別不認得" in r.stderr, \
        f"最外層事件型別漂移應對 stderr 出聲，實得 stderr：{r.stderr}"

    # N：item 容器不是 dict → 收不到回合，但**必須**出聲（若直接換成 {}，三層哨兵全都接不到）
    n_md = md_of(sid_n)
    assert n_md.count("### 👤 You") == 0, "認不得的容器形狀不應硬收成回合"
    assert "CONTAINERQ" not in n_md, "沒收下的內容不應憑空出現"
    assert r.stderr.count("型別像使用者訊息卻不認得") >= 4, \
        f"item 容器漂移（非 dict）也要進同一個哨兵，實得 stderr：{r.stderr}"

    # O：混合內容 → 文字照收，但被丟掉的 block 要有人數得出來
    o_md = md_of(sid_o)
    assert "MIXEDQ" in o_md, ("混合內容的文字部分仍要收得到；實得檔案清單："
                              + repr(sorted(mds)) + "\n這一份的內容：" + o_md[:400])
    assert "block 沒被收進來" in r.stderr, \
        f"混合內容有 block 被丟掉時必須出聲，實得 stderr：{r.stderr}"
    # ⚠ 這一格與 E（純圖片、取不出文字）是**不同**的哨兵：E 那條靠「抽不到任何文字」觸發，
    #    混合內容抽得到文字，所以只有 E 那條的話這裡完全無聲。
    assert "MIXEDQ" in o_md and o_md.count("### 👤 You") == 1, "混合內容仍是一則正常回合"

    # Z：舊格式的型別漂移要走與新格式同一組檢查——文字照收、被丟掉的 block
    #    要數得出來，而 Python 的 repr **不得**被當成 prompt 收下。
    z_md = md_of(sid_z)
    assert "舊格式漂移LEGACYDRIFTQ" in z_md, \
        f"舊格式 content block 陣列的文字部分仍要收得到：{z_md[:400]}"
    assert "'type': 'text'" not in z_md and "image_url" not in z_md, \
        f"Python 的 repr 不得被當成 prompt 收下（三層哨兵都認不出來，只能靠這裡擋）：{z_md[:400]}"

    # ZR：助理內容整批消失時必須出聲——這是「看起來完整、少了東西」的另一半，
    #    而它沒有哨兵就完全無聲（頁面：一則提問、零則回答）。
    zr_md = md_of(sid_zr)
    assert "回答會消失RESPDRIFTQ" in zr_md, f"同一場的 prompt 仍要收得到：{zr_md[:300]}"
    assert "答RESPDRIFTA" not in zr_md, "對照：容器漂掉的助理內容本來就解析不出來"
    assert "response_item 的容器形狀不認得" in r.stderr, (
        f"response_item 容器漂移必須出聲，實得 stderr：{r.stderr}")

    # P：payload 容器漂移 → 收不到回合，但哨兵必須接住（item 層與最外層哨兵都進不來）
    p_md = md_of(sid_p)
    assert p_md.count("### 👤 You") == 0, "認不得的容器形狀不應硬收成回合"
    assert "PAYQ" not in p_md, "沒收下的內容不應憑空出現"
    assert r.stderr.count("型別像使用者發言卻不認得") >= 2, (
        f"payload 容器漂移（非 dict）也要進 event 層哨兵，實得 stderr：{r.stderr}")

    # Q：item 被序列化成 JSON 字串 → 與 N 同一個哨兵（只是容器換一種形態）
    q_md = md_of(sid_q)
    assert q_md.count("### 👤 You") == 0, "認不得的容器形狀不應硬收成回合"
    assert "STRQ" not in q_md, "沒收下的內容不應憑空出現"
    assert r.stderr.count("型別像使用者訊息卻不認得") >= 5, (
        f"item 被序列化成字串也要進同一個哨兵，實得 stderr：{r.stderr}")

    # R：`response_item` 是認得的外層型別 → 不得多報一次「最外層型別不認得」（M 那格才是）
    assert r.stderr.count("最外層型別不認得") == 1, (
        f"只有真的認不得的外層型別該出這條；response_item 的注入脈絡不算，實得 stderr：{r.stderr}")
    assert "正常問句RESPQ" in md_of(sid_r), "同一場的正常 prompt 仍要收得到"

    # E（純圖片）不該同時觸發兩個哨兵：取不出文字那條已經報過，再數一次 dropped block
    # 會讓同一則訊息出兩行警告，看起來像兩個獨立問題。
    # 該出這條的只有兩格：O（新格式混合內容）與 Z（舊格式漂成 content block 陣列）——
    # 兩側對稱，所以計數是 2 不是 1。
    assert r.stderr.count("block 沒被收進來") == 2, (
        f"新舊兩種格式的混合內容各該報一次 dropped block，實得 stderr：{r.stderr}")

    # S：五個型別哨兵同時失明時，形狀無關的那一條要接住
    shapeless = [ln for ln in r.stderr.splitlines() if "個回合開始了卻沒收到" in ln]
    assert any(sid_s[:8] in ln for ln in shapeless), (
        f"型別名不含 user、也不帶 role 的漂移必須被形狀無關的哨兵接住，實得：{shapeless}")
    # T：子代理執行緒本來就沒有人打字，不得誤報
    assert not any(sid_t[:8] in ln for ln in shapeless), (
        f"子代理執行緒不得觸發這條哨兵，實得：{shapeless}")
    # U：工具結果也是以 type=="user" 存的——拿它當「有收到 prompt」的證據會讓哨兵閉嘴
    assert any(sid_u[:8] in ln for ln in shapeless), (
        f"只有工具結果、沒有真實 prompt 時哨兵必須出聲，實得：{shapeless}")
    assert "TOOLQ" not in md_of(sid_u), "沒收下的內容不應憑空出現"
    # W：一場中途才漂移（收到 1 則、實際 2 則）——只看「有沒有收到任何一則」會整個穿過去
    assert any(sid_w[:8] in ln for ln in shapeless), (
        f"部分遺失（前半收得到、後半全丟）同樣要出聲，實得：{shapeless}")
    assert "前半收得到MIDQ1" in md_of(sid_w) and "MIDQ2" not in md_of(sid_w), (
        "收得到的那一則仍要呈現，收不到的不得憑空出現")
    # X：被中止的回合沒有 prompt 是正常的，不得誤報
    assert not any(sid_x[:8] in ln for ln in shapeless), (
        f"turn_aborted 的回合本來就沒有 prompt，不得誤報，實得：{shapeless}")
    # Y：一個回合連送多則時，整檔總數相減會被淹掉——逐窗記帳才抓得到那個空掉的回合
    assert any(sid_y[:8] in ln for ln in shapeless), (
        f"一個回合連送多則的 session，漏收同樣要出聲，實得：{shapeless}")
    assert "連送一QUEUE1" in md_of(sid_y) and "QUEUELOST" not in md_of(sid_y), (
        "收得到的三則仍要呈現，收不到的不得憑空出現")

    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "新格式問句NEWFMTQ" in idx, "索引標題應取到新格式的首句，而非 fallback 成 (無對話)"
    assert f"(無對話) {sid_a[:8]}" not in idx, "有 prompt 的 session 不應被標成 (無對話)"
    print("OK: codex item_completed user message test passed")


def test_account_switch_cause(tmp_path=None):
    # 自願切帳號（沒撞 limit）：transcript 裡沒有 429/401，只能靠各帳號自己的 history.jsonl 認出。
    # 這種步在修正前會被歸成 evict（伺服器側異常），實為人因可避免 → 應歸 acct 並進索引的人因浪費。
    import ai_session_viewer as v

    sid = "019f0100-0000-7000-8000-000000000abc"
    cfg_a = new_tmp(tmp_path) / "cfgA"
    cfg_b = cfg_a.parent / "cfgB"
    for c in (cfg_a, cfg_b):
        c.mkdir(parents=True, exist_ok=True)
    base = 1780000000
    (cfg_a / "history.jsonl").write_text(json.dumps(
        {"display": "先問一句", "timestamp": base * 1000, "sessionId": sid}) + "\n", encoding="utf-8")
    (cfg_b / "history.jsonl").write_text(json.dumps(
        {"display": "換帳號後再問", "timestamp": (base + 600) * 1000, "sessionId": sid}) + "\n",
        encoding="utf-8")

    sw = v.load_account_switches([cfg_a, cfg_b])
    assert sid in sw, f"同一 sessionId 出現在兩個帳號的 history 應判定為切帳號，實得 {sw}"
    # config 目錄必須獨立列舉：各帳號的 projects/ 常是指向同一實體的 junction，會被來源清單的
    # realpath 去重收斂成一個，但 history.jsonl 各帳號各一份 —— 跟著收斂就永遠偵測不到切帳號。
    (cfg_a / "projects").mkdir(exist_ok=True)
    (cfg_b / "projects").mkdir(exist_ok=True)
    found = v.claude_config_dirs([cfg_a / "projects", cfg_b / "projects"])
    assert cfg_a in found and cfg_b in found, \
        f"兩個帳號的 config 目錄都要列出（不得被 projects/ 的去重收斂掉），實得 {found}"

    # 上面那段是直接餵函式；**組裝那一段也要測**——函式對、組裝錯的話這條路徑照樣靜默失效。
    # 自訂佈局（--claude-source）下兩個 config 的 projects/ junction 到同一實體時：掃 session
    # 要去重（重複掃是白工、同一場會被計兩次），掃 history 不能去重（history.jsonl 各帳號各一
    # 份、沒有共用）。兩者必須走不同的來源清單。
    cfg_j = cfg_a.parent / "cfgJ"
    cfg_j.mkdir(parents=True, exist_ok=True)
    try:
        (cfg_j / "projects").symlink_to(cfg_a / "projects", target_is_directory=True)
    except (OSError, NotImplementedError):
        pass                                  # 建不出 symlink 的平台略過這一格
    else:
        cs_args = [f"a={cfg_a / 'projects'}", f"j={cfg_j / 'projects'}"]
        assert len(v.collect_sources(cs_args, "")) == 1, (
            "掃 session 的來源仍要按 realpath 去重（同一實體重複掃是白工、同一場會被計兩次）")
        asm = v.claude_config_dirs(pp for _, pp in v.claude_source_items(cs_args, ""))
        assert cfg_a in asm and cfg_j in asm, (
            f"掃 history 的 config 清單必須保留去重前的兩個來源，實得 {asm}")

    # 同一列被複製到另一個 config（手動跨機同步/備份還原）不是換帳號：同 timestamp 出現在
    # 多個帳號 → 無法歸屬 → 整組排除，不得憑空生出一次 acct 而誣賴使用者。
    cfg_c = cfg_a.parent / "cfgC"
    cfg_d = cfg_a.parent / "cfgD"
    dup = json.dumps({"display": "同一列", "timestamp": base * 1000, "sessionId": "dup-sid"})
    for c in (cfg_c, cfg_d):
        c.mkdir(parents=True, exist_ok=True)
        (c / "history.jsonl").write_text(dup + "\n", encoding="utf-8")
    assert v.load_account_switches([cfg_c, cfg_d]) == {}, \
        "完全相同的 history 列出現在兩個帳號＝檔案被複製，不得判成切帳號"
    # 單帳號時必須什麼都不報（安全退化，公開使用者多半只有一個帳號）
    assert v.load_account_switches([cfg_a]) == {}, "單一帳號不應偵測出任何切換"

    # 兩步：第二步間隔 10 分（<55 分 TTL）且冷啟 → 無 acct 資料時是 evict、有的話是 acct
    steps = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
             [base + 600, 0, 100000, 100000, 0, 100000, 0, 0, 0]]
    models = ["claude-opus-4-5"]
    c_no = v.classify_cache_causes(steps, models, [])
    c_yes = v.classify_cache_causes(steps, models, [[base + 600, "acct"]])
    assert "evict" in c_no.values(), f"沒有切帳號資料時應判 evict，實得 {c_no}"
    assert "acct" in c_yes.values(), f"有切帳號資料時應判 acct，實得 {c_yes}"
    assert "evict" not in c_yes.values(), "acct 應取代該步的 evict，不得兩者並存"

    # acct 只搶 evict 那一格：間隔超過 TTL 時快取本來就會死 → 仍是 expiry
    far = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
           [base + 4000, 0, 100000, 100000, 0, 100000, 0, 0, 0]]
    c_far = v.classify_cache_causes(far, models, [[base + 4000, "acct"]])
    assert "expiry" in c_far.values(), f"間隔超過 TTL 應維持 expiry，實得 {c_far}"

    # 索引的人因浪費：只認 expiry/acct，不含 evict（evict 是伺服器側異常，不該算在人頭上）
    class _S:
        pass
    s = _S()
    s.cache_steps, s.cache_models, s.cache_events = steps, models, [[base + 600, "acct"]]
    n, usd, _p = v.session_waste(s)
    assert n == 1 and usd > 0, f"切帳號那步應計入人因浪費，實得 n={n} usd={usd}"
    s.cache_events = []
    n_ev, _u, _p = v.session_waste(s)
    assert n_ev == 0, f"evict 不得算進人因浪費，實得 {n_ev}"

    # acct 在**統計**上必須與 limit/auth 同等對待：快取按組織隔離、被整段丟掉，不是「沒撐過
    # TTL」。留在 TTL 風險集裡會被算成一次「提早失效」——正是本功能要消滅的污染換個地方發生。
    def _row(ev):
        return {"cache_steps": steps, "cache_models": models, "cache_events": ev,
                "source_kind": "claude-code", "kind": "chat", "account": "a",
                "start_ts": float(base)}
    d_acct = v.build_cache_report([_row([[base + 600, "acct"]])])
    d_limit = v.build_cache_report([_row([[base + 600, "limit"]])])
    d_none = v.build_cache_report([_row([])])
    assert d_acct["kpi"]["comply_n"] == 0, \
        f"acct 應排除在 TTL 帶內樣本外（同 limit），實得 comply_n={d_acct['kpi']['comply_n']}"
    assert d_limit["kpi"]["comply_n"] == 0, "limit 本來就該排除（對照組）"
    assert d_none["kpi"]["comply_n"] == 1, \
        "沒有帳號邊界的 evict 仍要留在風險集裡，否則是排除過頭"
    assert d_acct["causes_total"]["acct"] == 1, "排除統計不得影響成因計數"

    # acct 也要勝過 **<2 分的 intra**：帳號邊界是 history.jsonl 記下的直接證據，intra 是由間隔
    # 推論出來的；快取此時本來還活著，冷啟的原因就是換了組織。少了這一格，切完帳號兩分鐘內
    # 接著做事的那筆人因浪費會在索引與報告上「一致地」消失——兩邊數字仍然相等，但一致地錯。
    near = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
            [base + 60, 0, 100000, 100000, 0, 100000, 0, 0, 0]]
    assert "intra" in v.classify_cache_causes(near, models, []).values(), \
        "沒有帳號資料時 <2 分冷啟仍該是 intra（對照組）"
    c_near = v.classify_cache_causes(near, models, [[base + 60, "acct"]])
    assert "acct" in c_near.values(), f"<2 分內的帳號邊界應判 acct，實得 {c_near}"
    assert "intra" not in c_near.values(), "直接證據應取代推論，不得兩者並存"
    d_near = v.build_cache_report([dict(_row([[base + 60, "acct"]]), cache_steps=near)])
    assert d_near["causes_total"]["acct"] == 1 and d_near["causes_total"]["intra"] == 0, \
        f"報告端必須與 classify 同規則，實得 {dict(d_near['causes_total'])}"
    s.cache_steps, s.cache_events = near, [[base + 60, "acct"]]
    n_near, usd_near, _p = v.session_waste(s)
    assert n_near == 1 and usd_near > 0, f"<2 分內的切帳號應計入索引的人因浪費，實得 n={n_near}"
    assert n_near == d_near["kpi"]["avoid_n"], \
        f"索引與報告的人因次數必須同口徑，實得 {n_near} vs {d_near['kpi']['avoid_n']}"
    s.cache_steps, s.cache_events = steps, [[base + 600, "acct"]]   # 還原給後面的索引呈現測試

    # history.jsonl 的健康狀態：回空 dict 有三種完全不同的意思，不可混為一談。
    # ① 數字被序列化成**字串**時仍要收得到（只認 int/float 的話整份 history 靜默歸零，
    #    而呼叫端看到的是「這台沒切過帳號」）。
    cfg_s1 = cfg_a.parent / "cfgS1"
    cfg_s2 = cfg_a.parent / "cfgS2"
    for c, t in ((cfg_s1, base), (cfg_s2, base + 600)):
        c.mkdir(parents=True, exist_ok=True)
        (c / "history.jsonl").write_text(json.dumps(
            {"display": "字串時間", "timestamp": str(t * 1000), "sessionId": "str-sid"}) + "\n",
            encoding="utf-8")
    assert "str-sid" in v.load_account_switches([cfg_s1, cfg_s2]), \
        "timestamp 寫成數字字串時仍須認得，否則整份 history 靜默歸零"
    # ①-a 一列**大到轉不成 float 的整數**（JSON 允許任意位數）只能算壞列，不可以中止整個建置
    assert v._history_epoch_ms(10 ** 400) is None, "溢位的時間戳要當壞列，不可以丟例外"
    cfg_o1 = cfg_a.parent / "cfgO1"
    cfg_o1.mkdir(parents=True, exist_ok=True)
    (cfg_o1 / "history.jsonl").write_text(
        '{"display": "x", "timestamp": ' + "1" + "0" * 400 + ', "sessionId": "ovf-sid"}\n'
        + json.dumps({"display": "y", "timestamp": base * 1000, "sessionId": "str-sid"}) + "\n",
        encoding="utf-8")
    assert "str-sid" in v.load_account_switches([cfg_o1, cfg_s2]), \
        "一列溢位的時間戳不可以拖垮整份 history（其餘的列照收）"
    # ①-b 單位漂移：這一欄是 epoch **毫秒**。上游若改成秒，值仍轉得成 float、除以 1000 之後
    #     也還是合法浮點數 → 切帳號時刻靜默變成 1970 年，而壞列數是 0、完全沒有跡象。
    cfg_u1 = cfg_a.parent / "cfgU1"
    cfg_u2 = cfg_a.parent / "cfgU2"
    for c, t in ((cfg_u1, base), (cfg_u2, base + 600)):     # 秒，不是毫秒
        c.mkdir(parents=True, exist_ok=True)
        (c / "history.jsonl").write_text(json.dumps(
            {"display": "單位漂移", "timestamp": t, "sessionId": "unit-sid"}) + "\n",
            encoding="utf-8")
    h_unit = {}
    assert v.load_account_switches([cfg_u1, cfg_u2], h_unit) == {}, \
        "時間量級整個錯掉時不得產出切帳號時刻（那會是 1970 年的假資料）"
    # ①-c **部分**複製（不是整份）：兩份 history 曾經相同、之後其中一邊被裁切或輪替，
    #     同一個 sid 的列就會乾淨地分成「舊的只在 A、新的只在 B」——與真正切帳號完全同形。
    #     判準要拉到 sessionId 層級：只要有任何一列撞在兩個帳號上，整個 sid 都不可歸屬。
    cfg_p1 = cfg_a.parent / "cfgP1"
    cfg_p2 = cfg_a.parent / "cfgP2"
    shared = {"display": "共有的一列", "timestamp": base * 1000, "sessionId": "part-sid"}
    only_a = {"display": "只在A", "timestamp": (base + 100) * 1000, "sessionId": "part-sid"}
    only_b = {"display": "只在B", "timestamp": (base + 700) * 1000, "sessionId": "part-sid"}
    for c, extra in ((cfg_p1, only_a), (cfg_p2, only_b)):
        c.mkdir(parents=True, exist_ok=True)
        (c / "history.jsonl").write_text(
            json.dumps(shared) + "\n" + json.dumps(extra) + "\n", encoding="utf-8")
    assert v.load_account_switches([cfg_p1, cfg_p2]) == {}, \
        "有任何一列撞在兩個帳號上＝這兩份 history 之間有複製關係，整個 sid 都不可歸屬"
    assert h_unit["bad_rows"] == 2, \
        f"量級不合理的列要計為壞列，才看得出「讀到了但認不得」，實得 {h_unit}"
    # ② 帳號身分是**實體路徑**、不是目錄名：不同位置的 config 很容易同名（都叫 .claude），
    #    用目錄名當身分會把兩個帳號併成一個，它們之間的切換就永遠偵測不到。
    same_name = cfg_a.parent / "boxX" / ".claude", cfg_a.parent / "boxY" / ".claude"
    for c, t in zip(same_name, (base, base + 600)):
        c.mkdir(parents=True, exist_ok=True)
        (c / "history.jsonl").write_text(json.dumps(
            {"display": "同名目錄", "timestamp": t * 1000, "sessionId": "samename-sid"}) + "\n",
            encoding="utf-8")
    assert "samename-sid" in v.load_account_switches(list(same_name)), \
        "兩個同名但不同路徑的 config 目錄是兩個帳號，不得被併成一個"
    # ③ 涵蓋率要能被呼叫端讀到，報告才有辦法揭露「0 次」是哪一種 0
    health = {}
    v.load_account_switches([cfg_s1, cfg_s2], health)
    assert health["files"] == 2 and health["rows"] == 2 and health["bad_rows"] == 0, \
        f"health 應回報掃描涵蓋率，實得 {health}"
    bad = cfg_a.parent / "cfgBad"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "history.jsonl").write_text('{"sessionId": "x", "timestamp": {"nested": 1}}\n',
                                       encoding="utf-8")
    h_bad = {}
    assert v.load_account_switches([bad], h_bad) == {}
    assert h_bad["bad_rows"] == 1, f"認不得的列要被數出來，實得 {h_bad}"
    # ⑤ 非有限值要被當成壞列擋下。inf／nan 過得了 float()，卻會在後面轉 int() 時丟
    #    OverflowError——**一列壞資料中止整個建置**。這是選配資料源，壞列只能被計為壞列。
    #    裸 Infinity/NaN（JSON 非標準但 Python 解得出來）與字串形式都要擋。
    for i, form in enumerate(("Infinity", "-Infinity", "NaN", '"Infinity"', '"nan"')):
        i1, i2 = cfg_a.parent / f"cfgInf{i}a", cfg_a.parent / f"cfgInf{i}b"
        for d, t in ((i1, "1780000000000"), (i2, form)):
            d.mkdir(parents=True, exist_ok=True)
            (d / "history.jsonl").write_text(
                '{"sessionId":"inf-sid","timestamp":%s}\n' % t, encoding="utf-8")
        h_inf = {}
        got_inf = v.load_account_switches([i1, i2], h_inf)      # 這一行本身不得丟例外
        assert got_inf == {}, f"非有限 timestamp 不得產生切換標記（{form}），實得 {got_inf}"
        assert h_inf["bad_rows"] == 1, f"非有限 timestamp 應計為壞列（{form}），實得 {h_inf}"
    # ④ 偵測範圍要**在兩種報告上、且不論次數是不是 0** 都揭露：0 有三種意思（沒切過／單帳號／
    #    讀不到），只在 >0 時附註等於讓最需要保留懷疑的那個數字裸奔。少一種輸出＝那一種在隱瞞。
    for d_case, label in ((d_none, "acct=0"), (d_acct, "acct=1")):
        for render in (v.render_cache_report_html, v.render_cache_report_md):
            txt = render(d_case)
            assert "只在多帳號的本機資料上成立" in txt, f"{label} 的 {render.__name__} 應揭露偵測範圍"
            assert "顯示 0 不代表沒發生過" in txt, f"{label} 的 {render.__name__} 應說明 0 的歧義"
    # ⑥ 涵蓋率要真的走到報告上——只把 health 填好、呼叫端不接，使用者看到的仍是沒有脈絡的 0。
    d_h = v.build_cache_report([_row([])],
                               acct_health={"dirs": 4, "files": 4, "rows": 2464,
                                            "bad_rows": 0, "read_errors": 0, "accounts": 4})
    assert d_h["acct_health"]["accounts"] == 4, "報告資料應帶著偵測涵蓋率"
    for render in (v.render_cache_report_html, v.render_cache_report_md):
        txt = render(d_h)
        assert "掃了 4 個 config 目錄" in txt and "2464 列" in txt and "4 個帳號" in txt, \
            f"{render.__name__} 應把實際涵蓋率寫出來"
    for render in (v.render_cache_report_html, v.render_cache_report_md):
        assert "沒有量到偵測涵蓋率" in render(d_none), \
            f"{render.__name__} 在沒量到涵蓋率時要跟『量到 0』分開講"

    # 切帳號資料來自 repo 外的 history.jsonl：transcript 沒變也可能改變歸因 → 必須進指紋，
    # 否則增量建置會沿用舊 row，把 acct 歸因與「浪費」欄靜默停在舊值。
    probe = Path(v.__file__)
    assert v.session_signature(probe) != v.session_signature(probe, [base + 600]), \
        "切帳號時刻必須影響 session 指紋（否則增量建置不會重算歸因）"

    # 索引呈現：紅徽章、浪費欄、篩選勾選框、data-waste 標記
    row = {"session_id": sid, "source_kind": "claude-code", "account": "a", "proj": "P",
           "proj_munged": "P", "cwd": "/x/P", "title": "切帳號那場", "rename": "", "ai_title": "",
           "kind": "chat", "models": ["opus"], "cost": 1.0, "cost_partial": False,
           "out_html": "x.html", "date_str": "2026-08-01 10:00", "month": "2026-08",
           "dur": "10分", "branch": "main", "n_user": 2, "n_assistant": 2, "n_tools": 0,
           "waste_n": n, "waste_usd": usd, "waste_partial": False}
    html = v.render_index_html([row])
    assert 'class="chip waste"' in html, "有人因浪費的 session 應有紅徽章"
    assert f"🔥 ×{n}" in html, "徽章應標出次數"
    assert 'data-waste="1"' in html, "row 應帶 data-waste 供篩選"
    assert 'id="fw"' in html, "應有『只看有人因浪費的』勾選框"
    assert "浪費</th>" in html, "應有浪費欄位表頭"
    # 金額估不出來（人因步是未知型號）時，🔥 的說明也要帶 `?`：浮欄、表頭、MD、②-b 四處
    # 都走 cost_label，這裡若直接寫 $0 會被讀成「沒多花錢」——五處對四處的不一致最難發現。
    part_row = dict(row, waste_usd=0.0, waste_partial=True)
    part_html = v.render_index_html([part_row])
    tip = part_html[part_html.index("chip waste"):][:400]
    assert v.cost_label(0.0, True) in tip.split("估算多花")[1][:8], (
        f"金額估不出來時 tooltip 也要帶 ?，實得：{tip.split('估算多花')[1][:40]}")
    # 同一筆金額，浮欄／MD／tooltip 三處必須是**同一種寫法**：`cost_label(0, True)` 是 `?`，
    # 而 `fmt_money(0)+"+?"` 是 `$0+?`——數字相同、標籤不同，出現在同一列上下文最難察覺。
    assert "$0+?" not in part_html, f"浪費欄要與 tooltip 同口徑（不得出現 $0+?）"
    part_md = v.render_index_md([dict(part_row, out_md="x.md")])
    assert "$0+?" not in part_md and "人因浪費 ?" in part_md, (
        f"MD 也要同口徑，實得：{[l for l in part_md.splitlines() if '人因浪費' in l]}")

    # **報告那兩處也要同口徑**。KPI 磚的副標題就寫著「與索引『浪費』欄同口徑」
    #   ——報告寫 $0+?、索引寫 ? 時那句是假的。
    base = 1780000000
    st = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
          [base + 4000, 0, 100000, 100000, 0, 100000, 0, 0, 0]]   # gap > TTL → expiry（人因）
    d_unk = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                   "cache_steps": st, "cache_models": ["模型不在價目表"],
                                   "cache_events": [], "start_ts": float(base)}])
    assert d_unk["kpi"]["avoid_n"] == 1 and d_unk["kpi"]["avoid_partial"], (
        f"前置條件：要有人因冷啟且金額估不出來，實得 {d_unk['kpi']}")
    rep_html, rep_md = v.render_cache_report_html(d_unk), v.render_cache_report_md(d_unk)
    for name, text in (("HTML KPI 磚", rep_html), ("MD ②", rep_md)):
        assert "$0+?" not in text, (
            f"{name} 的人因浪費不得寫成 $0+?（會被讀成「沒多花錢」）；索引那側給的是 "
            f"{v.cost_label(0.0, True)!r}")
    assert "人因浪費 **?**" in rep_md, f"MD 應與索引同口徑，實得：" + repr(
        [l for l in rep_md.splitlines() if "人因浪費" in l])

    # 沒有任何浪費時不該擺一個永遠篩不出東西的勾選框
    row0 = dict(row, waste_n=0, waste_usd=0.0)
    assert 'id="fw"' not in v.render_index_html([row0]), "全無浪費時不應出現篩選勾選框"
    print("OK: account switch cause test passed")


def test_report_partition_and_labels():
    """② 的各桶要蓋滿每個成因、每個成因要有配色、反事實基準只准差 API 自報那條規則。"""
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    # ① 四個敘述桶必須是 REPORT_CAUSES 的一個**分割**（不重疊、不遺漏）。漏一個鍵的後果是
    #    「共 N 次冷啟」大於列出來的各桶合計，那幾次在敘述層憑空消失。
    keys = [k for k, _l, _n in v.REPORT_CAUSES]
    buckets = (list(v._UNAVOIDABLE_CAUSES) + list(v._API_PREFIX_CAUSES)
               + list(v._UNKNOWN_CAUSES) + list(v._AVOIDABLE_CAUSES) + ["intra"])
    assert sorted(buckets) == sorted(keys), (
        f"② 的各桶必須剛好蓋滿 REPORT_CAUSES：漏了 {sorted(set(keys) - set(buckets))}、"
        f"多了 {sorted(set(buckets) - set(keys))}")
    assert len(buckets) == len(set(buckets)), "同一個成因不得同時屬於兩桶（會被重複計）"

    # ② 每個成因鍵都要有配色：圖例色塊與成因分期堆疊圖都直接拿 key 當 class，
    #    少一條 CSS 就是「占了寬度卻畫不出來」——看起來像圖表破洞。
    viewer_src = (ROOT / "ai_session_viewer.py").read_text(encoding="utf-8")
    missing = [k for k in keys if (".cz-" + k + "{") not in viewer_src]
    assert not missing, f"這些成因沒有 .cz-<key> 配色，圖例會是空白方格：{missing}"

    # ③ 反事實基準（band_excluded）只准反映「API 自報」那一條規則。被切帳號排掉的相鄰步對
    #    若沒有同步扣掉，就會落進差額裡，而那個差額對外揭露成「因前綴變動移出」。
    base = 1780000000
    models = ["claude-opus-4-5"]

    def warm_pair(t0, gap):        # 一對落在應命中帶（1h cohort、2～55 分）且命中的步
        return [[t0, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
                [t0 + gap, 95000, 100000, 100000, 0, 100000, 0, 0, 0]]

    def row(steps, events, acc):
        return {"cache_steps": steps, "cache_models": models, "cache_events": events,
                "source_kind": v.SOURCE_CLAUDE, "kind": "chat", "account": acc,
                "start_ts": float(base)}

    clean = row(warm_pair(base, 600), [], "a")
    killed = row(warm_pair(base, 600), [[base + 10, "acct"]], "b")   # 切換在 TTL 邊界前 → 排除
    d = v.build_cache_report([clean, killed])
    assert d["api"]["excluded"] == 0, "這批資料完全沒有 API 自報的前綴變動（前提檢查）"
    assert d["api"]["band_excluded"] == 0, (
        f"零個前綴變動步卻報出 {d['api']['band_excluded']} 對「因前綴變動移出」"
        "——被切帳號排掉的對不可掛在 API 頭上")

    # ③-b 反事實的 **lineage 重置**也要同步。切帳號排掉那一對之後，實際那側 `last_write` 歸零、
    #     下游的對掉進 unknown cohort 而離開帶內；反事實若沒跟著歸零，那一對只在反事實側被算到，
    #     差額同樣會被講成「因前綴變動移出」。用「中間那步沒有新寫入」把 lineage 的差別逼出來。
    no_write = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],          # 有 1h 寫入 → 建 lineage
                [base + 600, 95000, 100000, 0, 0, 0, 0, 0, 0],              # 沒有寫入 → 不刷新 lineage
                [base + 1200, 95000, 100000, 0, 0, 0, 0, 0, 0]]
    d_lin = v.build_cache_report([row(no_write, [[base + 10, "acct"]], "c")])
    assert d_lin["api"]["excluded"] == 0, "前提檢查：這批沒有任何 API 自報的前綴變動"
    assert d_lin["api"]["band_excluded"] == 0, (
        f"切帳號排掉那一對之後，反事實的 lineage 也要跟著重置，"
        f"否則下游的對會只在反事實側被算到；實得 {d_lin['api']['band_excluded']}")

    # ④ 時間軸排序不可比到成因欄：同時刻、同冷熱、成因一個是字串一個是 None 會丟 TypeError，
    #    整個建置中止。同一帳號下才會相遇，所以兩個 row 要放同一個 account。
    #    ⚠ 成因為 None 要**可達**才測得到：第一個可分析步若是原始第 0 步會被標成 first。
    #    前面墊一個 ctx 低於 REPORT_MIN_CTX 的步把它濾掉，剩下那步就既不是 first、也不是任何
    #    一對的後項 → cause_by_t 沒有它。
    cold_first = [[base + 4900, 0, 499, 499, 0, 499, 0, 0, 0],               # ctx < 500 → 被濾掉
                  [base + 5000, 0, 100000, 100000, 0, 100000, 0, 0, 0]]      # 冷、且無從歸因
    with_cause = [[base + 5000, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
                  [base + 5000, 0, 100000, 100000, 0, 100000, 0, 0, 0]]      # 冷、有成因
    d_sort = v.build_cache_report([row(cold_first, [], "same"), row(with_cause, [], "same")])
    assert d_sort["has_data"], "前提檢查：這兩個 row 要真的被分析到"

    # ⑤ 事件消費要**一次性**：兩條界線精度不同時，落在步驟整秒上的切換會同時滿足「這一對的
    #    上界」與「下一對的下界」，一次切換被記成兩次人因浪費、金額翻倍。
    three = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
             [base + 60, 0, 100000, 100000, 0, 100000, 0, 0, 0],
             [base + 120, 0, 100000, 100000, 0, 100000, 0, 0, 0]]
    c_once = v.classify_cache_causes(three, models, [[base + 60.6, "acct"]])
    assert list(c_once.values()).count("acct") == 1, (
        f"一次切換只能算一次，實得 {c_once}")
    d_once = v.build_cache_report([row(three, [[base + 60.6, "acct"]], "once")])
    assert d_once["causes_total"]["acct"] == 1 and d_once["kpi"]["avoid_n"] == 1, (
        f"報告端同規則（兩處是刻意的雙胞胎），實得 {dict(d_once['causes_total'])}")

    # ⑥ 逐步成因的鍵不可用裸 epoch：同一秒的兩步會互相覆蓋，③ 重暖作息的「可避免／非閒置」
    #    著色就跟著錯。用步驟索引當鍵才不會撞。
    brk = v.REPORT_BREAK_SEC + 60
    tools_code = v.API_MISS_CODES.index("tools_changed")
    same_sec = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],          # 暖，起點
                [base + brk, 0, 100000, 100000, 0, 100000, 0, 0, 0],        # 冷 → expiry（可避免）
                [base + brk, 0, 100000, 100000, 0, 100000, 0, tools_code, 9]]  # 同秒、冷 → tools
    d_key = v.build_cache_report([row(same_sec, [], "key")])
    avoid_n_clock = sum(c["a"] for c in d_key["clock_wd"]) + sum(c["a"] for c in d_key["clock_we"])
    unavoid_clock = sum(c["u"] for c in d_key["clock_wd"]) + sum(c["u"] for c in d_key["clock_we"])
    assert (avoid_n_clock, unavoid_clock) == (1, 0), (
        f"重暖那一步的成因是 expiry（可避免），不得被同秒的另一步覆蓋成 tools，"
        f"實得 可避免={avoid_n_clock} 非閒置={unavoid_clock}")

    print("OK: report partition & labels test passed")


def test_acct_precision_and_server_cause():
    """切帳號標記的次秒精度、TTL 存活樣本的排除判準、伺服器側成因不落人因桶。"""
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    base = 1780000000
    models = ["claude-opus-4-5"]

    def steps_of(gap, read=0, miss_code=0, miss_tok=0):
        # [epoch, cache_read, 脈絡, 寫入總量, 寫入5分, 寫入1h, 模型idx, 自報成因碼, 重算tokens]
        return [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
                [base + gap, read, 100000, 100000, 0, 100000, 0, miss_code, miss_tok]]

    def row_of(steps, events):
        return {"cache_steps": steps, "cache_models": models, "cache_events": events,
                "source_kind": v.SOURCE_CLAUDE, "kind": "chat", "account": "a",
                "start_ts": float(base)}

    # ① 切帳號標記要**保留次秒精度**。步驟時刻是整秒，而消費事件的規則是「不晚於前一步就跳過」
    #    → 截成整秒的話，「與前一步同秒、實際上更晚」的切換會整組被丟掉、該步的成因跟著掉。
    class _FakeSession:
        source_kind = v.SOURCE_CLAUDE
        session_id = "sub-sec-sid"
        events = []

    _st, _md, evs, _mids = v.collect_cache_steps(_FakeSession(), {"sub-sec-sid": [base + 0.6]})
    assert evs and evs[0][1] == "acct", f"切帳號事件應被帶進來，實得 {evs}"
    assert evs[0][0] != int(evs[0][0]), (
        f"切帳號事件不得被截成整秒（history 的原生精度是毫秒），實得 {evs[0][0]!r}")

    near = steps_of(60)                       # gap 60 < REPORT_INTRA_SEC → 沒有帳號資料時是 intra
    c_sub = v.classify_cache_causes(near, models, [[base + 0.6, "acct"]])
    assert "acct" in c_sub.values(), (
        f"與前一步同秒、但更晚的切換仍要算進這一步，實得 {c_sub}")
    c_trunc = v.classify_cache_causes(near, models, [[float(base), "acct"]])
    assert "intra" in c_trunc.values(), (
        f"對照組：切換不晚於前一步時本來就該跳過，實得 {c_trunc}")

    # ② TTL 存活樣本的排除判準＝**切換落在 TTL 邊界的哪一側**，不是「有沒有發生切換」。
    #    邊界之前 → 快取還活著就被整段丟掉，量不到存活；邊界之後 → 快取已自然過期，樣本有效。
    far = steps_of(4000)                      # gap 4000 ≥ REPORT_TTL_SAFE_SEC
    def bins_n(events):
        d = v.build_cache_report([row_of(far, events)])
        return sum(b["n"] for b in d["cohort_bins"]["1h"])

    assert bins_n([]) == 1, "沒有帳號邊界的一對本來就該進存活樣本（對照組）"
    assert bins_n([[base + v.REPORT_TTL_SAFE_SEC + 100, "acct"]]) == 1, (
        "切換晚於 TTL 邊界＝快取在切換前就自然過期了，這一對是有效樣本，不該被排掉")
    assert bins_n([[base + 10, "acct"]]) == 0, (
        "切換早於 TTL 邊界＝可能在快取還活著時就丟掉前綴，這一對不能拿來量存活")

    # ③ 伺服器自報 unavailable 是**同一步的直接證據**，不得再往下推論成人因桶：
    #    否則同一次呼叫會在 API 區被寫成伺服器不可用、在索引與 KPI 又被算成使用者造成的浪費。
    code = v.API_MISS_CODES.index("unavailable")
    srv = steps_of(600, miss_code=code, miss_tok=5000)
    ev_acct = [[base + 600, "acct"]]
    c_srv = v.classify_cache_causes(srv, models, ev_acct)
    assert "unavail" in c_srv.values(), f"自報 unavailable 應有自己的成因桶，實得 {c_srv}"
    assert "acct" not in c_srv.values(), f"伺服器側成因不得同時落進人因桶，實得 {c_srv}"
    d_srv = v.build_cache_report([row_of(srv, ev_acct)])
    assert d_srv["causes_total"]["unavail"] == 1 and d_srv["causes_total"]["acct"] == 0, (
        f"報告端必須與 classify 同規則，實得 {dict(d_srv['causes_total'])}")
    assert d_srv["kpi"]["avoid_n"] == 0, (
        f"伺服器不可用不算人因浪費，實得 avoid_n={d_srv['kpi']['avoid_n']}")

    # ⑤ 同時有結構性邊界的那一對，不得再算成「因伺服器自報而排除」——
    #    `if srv_cause:` 排在 `if boundary:` 之前，所以那種對會先落進 srv 分支。
    #    判準要與 `api_excluded` 一致（「自報是唯一排除理由」才計數），否則兩個對外揭露的
    #    數字口徑不同、讀者無從對帳。本機重疊 0 對，所以只有這條測試守得住。
    d_srv_only = v.build_cache_report([row_of(srv, [])])
    assert d_srv_only["api"]["srv_excluded"] == 1, (
        f"只有伺服器自報時要計數（對照組），實得 {d_srv_only['api']['srv_excluded']}")
    d_srv_bd = v.build_cache_report([row_of(srv, [[base + 300, "limit"]])])
    assert d_srv_bd["api"]["srv_excluded"] == 0, (
        f"同時有邊界的對已被 boundary 擋掉，不得再算成「因自報而排除」，"
        f"實得 {d_srv_bd['api']['srv_excluded']}")
    assert d_srv_bd["causes_total"]["switch"] == 1, (
        f"對照：那一對的成因仍該是結構性邊界，實得 "
        f"{ {k: n for k, n in d_srv_bd['causes_total'].items() if n} }")

    # ④ **三步以上**才量得到 lineage。上面 ③ 的 srv fixture 全是兩步，量不到
    #    「下一個冷啟用哪個 cohort」——而報告端與徽章端必須在這一格一致：報告端遇到 srv 會重置
    #    lineage、classify 端若不會（重置條件漏了 srv_cause），同一步就一邊算 expiry（人因、記錢）、
    #    一邊算 evict（非人因、不記錢），同一頁上兩個地方對同一步講相反的話。
    #    兩邊共用 `_resets_lineage`，這條測試就是釘住它的那根釘子。
    #    ⚠ srv 那一步**必須沒有新寫入**：有寫入的話迴圈頂端會立刻把 lineage 立回來，分岔看不出來
    #    （本機語料 153 個 srv 步全都有寫入，所以實跑觸發 0 次——這是可達性低、不是不存在）。
    srv3 = [[base, 90000, 100000, 100000, 0, 100000, 0, 0, 0],        # 1h 寫入 → cohort = 1h
            [base + 600, 0, 100000, 0, 0, 0, 0, code, 5000],          # srv 自報 unavailable、**零寫入**
            [base + 1600, 0, 100000, 100000, 0, 100000, 0, 0, 0]]     # 冷啟：cohort 應已掉成 unknown
    from collections import Counter
    c3 = v.classify_cache_causes(srv3, models, [])
    d3 = v.build_cache_report([row_of(srv3, [])])
    # gap = 1000 秒：unknown cohort 的界線是 5 分 → expiry；沿用舊 1h cohort（界線 55 分）→ evict。
    assert c3[base + 1600] == "expiry", (
        f"srv 那步沒寫入 → 舊 1h lineage 必須失效，否則下一個冷啟被誤判成 evict，實得 {c3}")
    assert Counter(c3.values()) == Counter({k: n for k, n in d3["causes_total"].items() if n}), (
        f"三步 lineage 上逐步徽章與報告不得漂移：{dict(c3)} vs "
        f"{ {k: n for k, n in d3['causes_total'].items() if n} }")
    # 記帳也要跟著對：expiry 是人因桶，報告端必須數到 1 次（分岔時這裡會是 0）。
    assert d3["kpi"]["avoid_n"] == 1, (
        f"expiry 是人因浪費，報告端應記 1 次，實得 avoid_n={d3['kpi']['avoid_n']}")

    class _S:
        pass

    s = _S()
    s.cache_steps, s.cache_models, s.cache_events = srv, models, ev_acct
    n_srv, usd_srv, _p = v.session_waste(s)
    assert n_srv == 0 and usd_srv == 0, (
        f"索引與報告必須同口徑：伺服器不可用那步不得計入浪費，實得 n={n_srv} usd={usd_srv}")

    # 對照組：拿掉自報碼，同一步就回到 acct，而且兩邊都算得出那筆人因浪費
    plain = steps_of(600)
    d_plain = v.build_cache_report([row_of(plain, ev_acct)])
    assert d_plain["causes_total"]["acct"] == 1 and d_plain["kpi"]["avoid_n"] == 1, (
        f"對照組：沒有自報碼時仍該判 acct 並計入人因，實得 {dict(d_plain['causes_total'])}")
    s.cache_steps = plain
    n_plain, _u, _p = v.session_waste(s)
    assert n_plain == 1, f"對照組：索引端同樣要算得到，實得 {n_plain}"

    # 伺服器側自報不可用的相鄰步對要**整對移出**存活／帶內樣本：那一次沒中是伺服器的事，
    # 不是快取沒撐住，留著會污染 TTL 的量測，也會讓 KPI 磚與 ② 成因表對「提早失效」給兩個數字。
    warm_srv = steps_of(600, read=95000, miss_code=code, miss_tok=5000)
    d_warm = v.build_cache_report([row_of(warm_srv, [])])
    assert sum(b["n"] for b in d_warm["cohort_bins"]["1h"]) == 0, (
        "自報 unavailable 的相鄰步對要整對移出存活樣本（命中的也要移，只挑未命中移會灌高存活率）")
    assert d_warm["api"]["srv_excluded"] == 1, (
        f"移出幾對要數得出來，報告才揭露得了，實得 {d_warm['api']}")
    assert d_warm["api"]["band_excluded"] == 0, (
        "伺服器側排除在反事實裡也成立 → 不可落進差額、被講成「因前綴變動移出」")

    # KPI 磚的「提早失效」與 ② 成因表的「提早失效」——**兩者不是同一個母體**，別把巧合當不變量。
    #   磚是 `comply_n - comply_hit`：只數 **1h cohort 且落在應命中帶**的冷啟。
    #   ② 的 `evict` 不分 cohort、也不看帶。所以磚**恆為 ② 的子集**，相等只在
    #   「樣本剛好全是 1h 帶內」時成立。
    # ⚠ 不可以直接斷言相等：fixture 兩對都是 1h cohort → 必然相等、
    #   抓不到任何東西（本機語料兩邊也剛好都是 14，實跑同樣蓋不到）。所以斷言真正成立的
    #   關係，並補一組非 1h 的 evict 證明這條測試分得出來。
    mixed = [row_of(steps_of(600, miss_code=code, miss_tok=5000), []),   # 伺服器不可用（冷）
             row_of(steps_of(600), [])]                                  # 真的提早失效（冷）
    d_mix = v.build_cache_report(mixed)
    kpi_evict = d_mix["kpi"]["comply_n"] - d_mix["kpi"]["comply_hit"]
    assert kpi_evict == d_mix["causes_total"]["evict"] == 1, (
        f"全部樣本都是 1h 帶內時兩個數字要一致（伺服器那對已整對移出，不得留在磚裡）："
        f"KPI {kpi_evict} vs ② {d_mix['causes_total']['evict']}")
    # 非 1h cohort 的 evict：前一步沒有寫入 → cohort unknown（界線 5 分），gap 200 秒落在
    # [REPORT_INTRA_SEC, 300) → 成因是 evict，但磚只收 1h 帶內 → 磚必須是 0。
    unk = [[base, 0, 100000, 0, 0, 0, 0, 0, 0],                      # 零寫入 → lineage unknown
           [base + 200, 0, 100000, 100000, 0, 100000, 0, 0, 0]]      # 冷啟、gap 200
    d_unk = v.build_cache_report([row_of(unk, [])])
    kpi_unk = d_unk["kpi"]["comply_n"] - d_unk["kpi"]["comply_hit"]
    assert d_unk["causes_total"]["evict"] == 1, (
        f"前置條件：這一對的成因要是 evict，實得 "
        f"{ {k: n for k, n in d_unk['causes_total'].items() if n} }")
    assert kpi_unk == 0, (
        f"非 1h cohort 的 evict 進不了 KPI 磚（磚只數帶內 1h）——兩個數字本來就不該相等，"
        f"實得 KPI {kpi_unk}")
    assert kpi_unk <= d_unk["causes_total"]["evict"], "磚恆為 ② 的子集（真正成立的那個關係）"

    # ⚠ 排除**只影響統計，不影響標示**：那一步的成因與並列標籤仍要在，讓人看得出
    #   「就算伺服器沒掛，這一步的快取也還是會失效」。
    far_srv = steps_of(4000, miss_code=code, miss_tok=5000)      # 間隔超過 TTL ＋ 伺服器不可用
    d_far = v.build_cache_report([row_of(far_srv, [])])
    assert d_far["causes_total"]["unavail"] == 1, "移出樣本不得把那次冷啟從成因統計裡一起抹掉"
    assert d_far["masked_human"]["causes"] == {"expiry": 1}, (
        f"同一步仍成立的閒置過期要照樣標出來，實得 {d_far['masked_human']}")
    m_far = {}
    v.classify_cache_causes(far_srv, models, [], m_far)
    assert v._cold_cause_label("unavail", list(m_far.values())[0]) == "閒置過期＋伺服器不可用", (
        "逐步徽章要並列兩個條件")

    # ⚠ 移出樣本一定要在報告上說明（會讓存活率看起來變好，理由只有寫出來讀者才判斷得了）
    for as_html in (True, False):
        note = v._srv_excluded_note(d_warm, html=as_html)
        assert "伺服器不可用" in note and "1 對" in note, f"揭露要講清楚移出幾對（html={as_html}）"
        assert "逐步徽章" in note, "也要講明那些冷啟仍會出現在徽章上，否則讀者以為被整個抹掉了"
    assert v._srv_excluded_note(v.build_cache_report([row_of(steps_of(600), [])])) == "", (
        "沒有移出任何一對時不該憑空生出一段揭露")

    # 順序：伺服器側自報排在結構性邊界之後、間隔推論之前
    c_bnd = v.classify_cache_causes(srv, models, [[base + 600, "limit"]])
    assert "switch" in c_bnd.values(), f"結構性邊界仍優先於伺服器側自報，實得 {c_bnd}"

    # ④ 實據勝出時，同一步仍成立的人因條件**不可以消失**：記帳只算一次（成因欄是實據那個、
    #    不進人因 KPI），但畫面上兩個都要標。少了這一半，「那一步其實也換了帳號」就被藏掉了。
    masked = {}
    v.classify_cache_causes(srv, models, ev_acct, masked)
    assert masked and list(masked.values())[0] == ["acct"], (
        f"被實據蓋過的切帳號要記下來，實得 {masked}")
    label = dict((k, lab) for k, lab, _ in v.REPORT_CAUSES)["acct"]
    srv_label = dict((k, lab) for k, lab, _ in v.REPORT_CAUSES)["unavail"]
    # 並列成一個標籤（人因在前、記帳的那個在後），不是各自散在別處
    assert v._cold_cause_label("unavail", ["acct"]) == label + "＋" + srv_label, (
        f"兩個條件要並列成一個標籤，實得 {v._cold_cause_label('unavail', ['acct'])}")
    assert v._cold_cause_label("unavail") == srv_label, "沒有同時成立的條件時就是原本那一個"

    d_mask = v.build_cache_report([row_of(srv, ev_acct)])
    mh = d_mask["masked_human"]
    assert mh["steps"] == 1 and mh["causes"] == {"acct": 1}, (
        f"報告端要與 classify 同口徑地數出來，實得 {mh}")
    assert d_mask["kpi"]["avoid_n"] == 0, "數出來歸數出來，人因 KPI 仍不得把它算進去"
    for as_html in (True, False):
        note = v._masked_human_note(d_mask, html=as_html)
        assert label in note and "不含它們" in note, (
            f"兩種報告都要揭露這件事（html={as_html}），實得：{note}")
    assert v._masked_human_note(v.build_cache_report([row_of(plain, ev_acct)])) == "", (
        "沒有被蓋過的情形時不應憑空生出一段揭露")

    # 結構性邊界（撞 limit 換帳號／換模型／壓縮）同樣是直接證據、同樣會蓋過人因條件。
    # 少了這個入口，同一件事就從那裡溜過去：不算人因（對），但畫面上也看不到（不對）。
    far_lim = steps_of(4000)
    m_bnd = {}
    c_bnd2 = v.classify_cache_causes(far_lim, models, [[base + 4000, "limit"]], m_bnd)
    assert "switch" in c_bnd2.values(), f"前提檢查：limit 邊界應判 switch，實得 {c_bnd2}"
    assert m_bnd and list(m_bnd.values())[0] == ["expiry"], (
        f"邊界勝出時，同一步仍成立的閒置過期也要記下來，實得 {m_bnd}")
    d_bnd = v.build_cache_report([row_of(far_lim, [[base + 4000, "limit"]])])
    assert d_bnd["masked_human"]["causes"] == {"expiry": 1}, (
        f"報告端同口徑，實得 {d_bnd['masked_human']}")

    # 逐步徽章：說明文字要同時出現「伺服器不可用」與「自行切帳號」
    u = {"cache_read": 0, "total_in": 100000, "input": 100000, "cache_create": 0,
         "output": 100, "win": 200000}
    tip = v.render_step_meters(2, u, "unavail", 600, base + 600, ["acct"])
    assert label + "＋" + srv_label in tip, f"冷啟徽章要標並列標籤，實得：{tip}"
    for c in ("unavail", "acct"):
        assert v._COLD_CAUSE_NOTE[c] in tip, f"兩個條件的解釋都要在說明裡（{c}），實得：{tip}"
    assert f"記在「{srv_label}」" in tip, f"說明要講清楚錢記在哪一邊，實得：{tip}"
    assert label not in v.render_step_meters(2, u, "unavail", 600, base + 600), (
        "對照組：沒有被蓋過的人因條件時不得憑空標上去")

    # ⑤ **未知的**自報成因也是同一步的直接證據：不映射的話那一步會繼續往下推論，最後落進
    #    人因桶——等於把「我們還看不懂的東西」算到使用者頭上。與 unavailable 是同一類。
    unk = steps_of(600, miss_code=v.API_MISS_UNKNOWN_CODE, miss_tok=5000)
    c_unk = v.classify_cache_causes(unk, models, ev_acct)
    assert "unknown" in c_unk.values(), f"未知自報碼要有自己的成因桶，實得 {c_unk}"
    assert "acct" not in c_unk.values(), f"未知自報碼不得被推論成人因，實得 {c_unk}"
    d_unk = v.build_cache_report([row_of(unk, ev_acct)])
    assert d_unk["kpi"]["avoid_n"] == 0, (
        f"未知自報碼不得計入人因浪費，實得 avoid_n={d_unk['kpi']['avoid_n']}")

    # ⑥ **撞 limit 被迫換帳號**不得被標成「自行切帳號」——`acct` 在本檔是專有名詞，
    #    意思就是「沒撞 limit 就換」。標錯方向與「算到使用者頭上」同等嚴重。
    m_forced = {}
    c_forced = v.classify_cache_causes(steps_of(600), models,
                                       [[base + 600, "limit"], [base + 600, "acct"]], m_forced)
    assert "switch" in c_forced.values(), f"前提檢查：limit 邊界應判 switch，實得 {c_forced}"
    assert not any("acct" in a for a in m_forced.values()), (
        f"同一區間有 limit 時，那次換帳號是被迫的，不得標成自行切帳號，實得 {m_forced}")

    # ⑦ 切換落在**當前這一步的同一秒**時要算進這一對，不可被推到下一對去
    m_same = {}
    c_same = v.classify_cache_causes(near, models, [[base + 60.6, "acct"]], m_same)
    assert "acct" in c_same.values(), (
        f"與當前步同秒的切換要算進這一對（步驟時刻是整秒，上界要同精度比），實得 {c_same}")
    d_same = v.build_cache_report([row_of(near, [[base + 60.6, "acct"]])])
    assert d_same["causes_total"]["acct"] == 1, (
        f"報告端同規則（兩處是刻意的雙胞胎），實得 {dict(d_same['causes_total'])}")

    # ⑧ 首步的直接證據不得被 `first` 蓋掉：成因表寫「session 第一句」、API 自報表寫
    #    「伺服器不可用」，同一步兩種說法。
    first_srv = [[base, 0, 100000, 100000, 0, 100000, 0, code, 5000]]
    c_first = v.classify_cache_causes(first_srv, models, [])
    assert "unavail" in c_first.values(), (
        f"首步也要讓已知的直接證據優先，first 只是沒有證據時的 fallback，實得 {c_first}")
    d_first = v.build_cache_report([row_of(first_srv, [])])
    assert d_first["causes_total"]["unavail"] == 1 and d_first["causes_total"]["first"] == 0, (
        f"報告端同規則（兩處是刻意的雙胞胎），實得 {dict(d_first['causes_total'])}")

    print("OK: acct precision & server cause test passed")


def test_acct_separator_and_step_time(tmp_path=None):
    # 涵蓋 session 頁顯示層三件事：逐步分隔列帶時刻、「距上一步」的預設值，
    # 以及主對話跨過切帳號時刻時插入的分隔線（時刻取自 cache_events 的 acct 標記）。
    import ai_session_viewer as v
    from datetime import datetime, timedelta, timezone

    root = new_tmp(tmp_path)

    def ep(iso):
        # 保留小數：切換時刻的次秒精度是被測行為之一
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    def hhmmss(iso):
        # 期望值用與產品碼同一條轉換算出來，斷言才不綁死在某個時區
        return datetime.fromtimestamp(ep(iso)).strftime("%H:%M:%S")

    def build(tag, evs, rows_a, rows_b):
        """一場 session ＋ 兩個帳號的 history，跑一次建置，回傳 (html, md)。
        rows_a／rows_b 是各帳號 history 的時刻（ISO）；同一 sid 出現在兩邊＝中途換過帳號。"""
        tmp = root / tag
        cfg_a, cfg_b = tmp / "cfgA", tmp / "cfgB"
        proj = cfg_a / "projects" / "demo-proj"
        proj.mkdir(parents=True, exist_ok=True)
        (cfg_b / "projects").mkdir(parents=True, exist_ok=True)
        sid = evs[0]["sessionId"]
        (proj / (sid + ".jsonl")).write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
        for cfg, rows in ((cfg_a, rows_a), (cfg_b, rows_b)):
            (cfg / "history.jsonl").write_text("\n".join(
                json.dumps({"display": "x", "timestamp": int(ep(x) * 1000), "sessionId": sid})
                for x in rows) + "\n", encoding="utf-8")
        out = tmp / "out"
        # ⚠ HOME 要指到空目錄：切帳號偵測會無條件列舉 `~/.claude*`，不隔離的話這個單元測試
        # 會去讀這台機器上真實、且正在被寫入的 history.jsonl（結果隨環境浮動）。
        iso = tmp / "home"; iso.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, HOME=str(iso), USERPROFILE=str(iso))
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--claude-source", "A=" + str(cfg_a / "projects"),
             "--claude-source", "B=" + str(cfg_b / "projects"),
             "--no-codex", "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8", env=env)
        assert r.returncode == 0, "非零退出\nSTDOUT:" + r.stdout + "\nSTDERR:" + r.stderr
        html = [p for p in _session_pages(out)][0].read_text(encoding="utf-8")
        md = [p for p in (out / "sessions").rglob("*.md")][0].read_text(encoding="utf-8")
        return html, md

    def user(uid, parent, iso, sid, text):
        return {"type": "user", "uuid": uid, "parentUuid": parent, "timestamp": iso,
                "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
                "message": {"role": "user", "content": text}}

    def asst(uid, parent, iso, sid, mid, blocks):
        return {"type": "assistant", "uuid": uid, "parentUuid": parent, "timestamp": iso,
                "sessionId": sid,
                "message": {"role": "assistant", "model": "claude-opus-4-7", "id": mid,
                            "usage": {"input_tokens": 100, "output_tokens": 20},
                            "content": blocks}}

    txt = lambda s: [{"type": "text", "text": s}]

    # ── 情境 1：一次切換 —— 位置要夾在前後兩則之間，HTML 與 MD 條數要相等 ──────────
    sid1 = "019f0200-0000-7000-8000-0000000005a1"
    t0, t1 = "2026-05-28T10:00:00.000Z", "2026-05-28T10:20:00.000Z"
    html, md = build("one", [
        user("a1", None, t0, sid1, "換帳號前的訊息BEFORESWITCH。"),
        asst("a2", "a1", "2026-05-28T10:00:05.000Z", sid1, "m1", txt("前半段的回覆。")),
        user("a3", "a2", t1, sid1, "換帳號後的訊息AFTERSWITCH。"),
        asst("a4", "a3", "2026-05-28T10:20:05.000Z", sid1, "m2", txt("後半段的回覆。")),
    ], [t0], [t1])
    assert html.count('class="acct-sep"') == 1, "只換一次帳號就只該有一條線"
    assert "換了登入帳號" in html, "分隔線應標出換過登入帳號"
    # ⚠ 顯示層不得使用成因層的判定詞：「自行切帳號」＝沒撞 limit（人因可避免）、
    # 「limit/切帳號」＝撞了 429/401（不可避免）。畫線不看有沒有 limit 邊界，貼上任一個
    # 都會有一部分的線與正下方那一步的成因徽章講相反的話。
    for verdict_word in ("自行切帳號", "limit/切帳號"):
        assert verdict_word not in html and verdict_word not in md, (
            "分隔線不得使用成因層的判定詞：" + verdict_word)
    assert html.index("BEFORESWITCH") < html.index('class="acct-sep"') < html.index("AFTERSWITCH"), \
        "分隔線要夾在換帳號的前後兩則之間"
    assert md.count("換了登入帳號") == html.count('class="acct-sep"'), "HTML 與 MD 條數必須相等"
    assert md.index("BEFORESWITCH") < md.index("換了登入帳號") < md.index("AFTERSWITCH"), \
        "MD 的分隔線位置要與 HTML 一致"
    # MD 全篇沒有日期可對照（回合標頭只有時分秒）→ 切帳號線一律帶日期；
    # HTML 上方有日期分隔線，同一天就不必重複印。
    want_d1 = datetime.fromtimestamp(ep(t1)).strftime("%m-%d")
    i_md1 = md.index("換了登入帳號")
    assert want_d1 in md[i_md1:i_md1 + 40], "MD 的切帳號線要帶日期：" + md[i_md1:i_md1 + 40]
    i_h1 = html.index('class="acct-sep"')
    assert want_d1 not in html[i_h1:i_h1 + 60], (
        "HTML 同一天不必重複印日期：" + html[i_h1:i_h1 + 60])

    # 分隔線不屬於任何一則：--search 切塊不得把它併進**前**一則（那一則屬於舊帳號，
    # 會變成錨點指錯、片段全是樣板的固定命中，每個切過帳號的 session 都多一筆）
    chunk_bodies = [c[-1] for c in v.iter_turn_chunks(md)]
    assert chunk_bodies, "應切得出回合"
    assert not any("換了登入帳號" in b for b in chunk_bodies), (
        "切帳號分隔線不得落進任何一則的搜尋內文")
    # ⚠ 只比「有沒有那幾個字」釘不住：這條線**配套插進去的裝飾**一樣
    # 不屬於任何一則，卻照樣會落進前一則。所以比對「同一場但沒切帳號」的切塊結果——
    # 有沒有切帳號，都不該改動任何一則的搜尋內文。
    _, md_nosw = build("one_nosw", [
        user("a1", None, t0, sid1, "換帳號前的訊息BEFORESWITCH。"),
        asst("a2", "a1", "2026-05-28T10:00:05.000Z", sid1, "m1", txt("前半段的回覆。")),
        user("a3", "a2", t1, sid1, "換帳號後的訊息AFTERSWITCH。"),
        asst("a4", "a3", "2026-05-28T10:20:05.000Z", sid1, "m2", txt("後半段的回覆。")),
    ], [t0], [])
    assert "換了登入帳號" not in md_nosw, "對照組不該有切帳號線，否則下面那條比對沒有意義"
    nosw_bodies = [c[-1] for c in v.iter_turn_chunks(md_nosw)]
    assert nosw_bodies == chunk_bodies, (
        "切帳號分隔線不得改動任何一則的搜尋內文（含它自己插的裝飾與空行）：\n"
        "有切帳號：" + repr(chunk_bodies) + "\n沒切帳號：" + repr(nosw_bodies))
    # ⚠ 反過來也要釘：使用者自己的訊息可以用 `**🔑 ` 開頭（粗體加鑰匙 emoji 沒有保留意義）。
    # 切塊端只比前綴的話，會把那一則的內文**靜默刪掉**——全文搜尋從此漏報，畫面上毫無跡象。
    fake = "### 👤 You · 12:00:00 {#t1}\n\n**🔑 KEEPMESEARCHABLE** 這是使用者自己寫的\n"
    fake_body = list(v.iter_turn_chunks(fake))[0][-1]
    assert "KEEPMESEARCHABLE" in fake_body, \
        "以 **🔑 開頭的使用者內容不得被切塊端當成分隔線刪掉，得到：" + repr(fake_body)
    # 真的分隔線仍要被跳過。期望值用**渲染端自己的常數**組，兩邊不會在改文案時分岔。
    real = ("### 👤 You · 12:00:00 {#t1}\n\n"
            + v.MD_ACCT_SEP_PREFIX + v.acct_sep_label(ep(t1)) + v.MD_ACCT_SEP_SUFFIX + "\n")
    real_body = list(v.iter_turn_chunks(real))[0][-1]
    assert "換了登入帳號" not in real_body, \
        "真的切帳號分隔線仍要被跳過，得到：" + repr(real_body)

    # 顯示層只陳述實據，不得替快取結果背書（畫線這條路徑沒有任何命中率證據要求）
    assert "前綴不共用" not in html and "前綴不共用" not in md, \
        "分隔線不得宣稱快取後果——那是成因層的判斷"
    m_def = re.search(r"DEF=\{([^}]*)\}", html)
    assert m_def and "gap:1" in m_def.group(1), (
        "「距上一步」應預設開啟（DEF 的 gap）；要對準 DEF 那一段，別對整頁搜尋："
        + (m_def.group(1) if m_def else "(找不到 DEF)"))
    bodycls = re.search(r'<body class="([^"]*)"', html)
    assert bodycls and "hide-gap" not in bodycls.group(1),         "預設開啟的度量不應同時被 body class 隱藏：" + (bodycls.group(1) if bodycls else "(無)")

    # ── 情境 2：切換落在最後一則之後 —— 兩種輸出都是 0 條（不得只有一邊丟掉）────────
    sid2 = "019f0200-0000-7000-8000-0000000005a2"
    html, md = build("after", [
        user("b1", None, "2026-05-28T10:00:00.000Z", sid2, "只有這一則ONLYTURN。"),
        asst("b2", "b1", "2026-05-28T10:00:05.000Z", sid2, "m1", txt("回覆。")),
    ], ["2026-05-28T10:00:00.000Z"], ["2026-05-28T11:00:00.000Z"])
    assert html.count('class="acct-sep"') == 0 and md.count("換了登入帳號") == 0, \
        "落在最後一則之後的切換不畫，且兩種輸出要一致"

    # ── 情境 3：只有一種輸出畫得出來的回合 —— 條數仍須相等 ───────────────────────
    # redacted_thinking 只有 HTML 收、MD 不收；若切帳號線跟著各自的渲染結果推進，
    # 這一則又是最後一則，MD 那條線就會被丟掉而 HTML 留著。
    sid3 = "019f0200-0000-7000-8000-0000000005a3"
    html, md = build("skew", [
        user("c1", None, "2026-05-28T10:00:00.000Z", sid3, "第一則FIRSTTURN。"),
        asst("c2", "c1", "2026-05-28T10:00:05.000Z", sid3, "m1", txt("回覆。")),
        user("c3", "c2", "2026-05-28T10:00:50.000Z", sid3, "第二則SECONDTURN。"),
        asst("c4", "c3", "2026-05-28T10:02:00.000Z", sid3, "m2",
             [{"type": "redacted_thinking", "data": "zzz"}]),
    ], ["2026-05-28T10:00:00.000Z"], ["2026-05-28T10:01:40.000Z"])
    assert html.count('class="acct-sep"') == md.count("換了登入帳號") == 1, \
        "某一則只有一種輸出畫得出來時，兩邊的分隔線條數仍須相等"
    # ↑ 條數相等的代價：這一條在 MD 落在**檔尾**（最後一則只有 HTML 畫得出來）。
    #   所以 MD 的文案不得宣稱「以下」有東西，也不得叫人去看 MD 根本沒有的逐步徽章。
    tail = md[md.index(v.MD_ACCT_SEP_PREFIX):]
    assert tail.strip().count("\n") == 0, \
        "這個 fixture 要保證 MD 的分隔線真的在檔尾（下面沒有內容），否則下面幾條斷言白做：" + tail
    assert "換了登入帳號" in tail, "上面那句話要對的是切帳號那一行，抓錯行了：" + tail
    for bad in ("以下屬於另一個帳號", "徽章"):
        assert bad not in tail, \
            "MD 的切帳號分隔線不得出現「" + bad + "」（檔尾沒有「以下」、MD 也沒有徽章）：" + tail

    # ── 情境 3c：最前端的過濾與 pending_acct 必須是**同一條規則** ─────────────────────
    # 兩邊各寫一份的話，「時刻完全相等」這一格會分岔：`ts > 第一則時刻` 一律丟掉，但
    # pending_acct 的收線規則是「`< gt` 或（`== gt` 且該則是 user）」。首則是 assistant 時
    # 那個標記該畫在第二則之前（上面有第一則可隔），不該被丟掉。
    class _FakeSession:                      # acct_marks 只用這兩個屬性
        pass

    def marks_and_positions(roles, group_ts, mark_ts):
        st = _FakeSession()
        base = datetime(2026, 5, 28, 10, 0, 0, tzinfo=timezone.utc)
        st.main_groups = [{"role": r, "dt": base + timedelta(seconds=x)}
                          for r, x in zip(roles, group_ts)]
        st.acct_times = [(base + timedelta(seconds=x)).timestamp() for x in mark_ts]
        kept, i, drawn = v.acct_marks(st), 0, []
        for gi, g in enumerate(st.main_groups):
            hits, i = v.pending_acct(kept, i, g)
            drawn += [(gi, round(ts - base.timestamp())) for ts in hits]
        return drawn

    # 首則是 assistant、標記與它同刻 → 兩個標記都畫在第二則之前
    assert marks_and_positions(("assistant", "assistant"), (0, 5), (0, 3)) == [(1, 0), (1, 3)], \
        "首則是 assistant 時，同刻的標記不得被最前端過濾吃掉——它該畫在第二則之前"
    # 首則是 user、標記與它同刻 → 該收在第一則之前，而上面空無一物 → 丟掉
    assert marks_and_positions(("user", "assistant"), (0, 5), (0,)) == [], \
        "首則是 user 時，同刻的標記收在第一則之前，而那上面空無一物 → 不畫"
    # 早於第一則的一律丟掉（不分角色）
    assert marks_and_positions(("assistant", "user"), (10, 20), (5,)) == [], \
        "早於第一則的標記上方空無一物 → 不畫"

    # ── 情境 3b：負的小數 epoch —— `int()` 往零截會把 1969 顯示成 1970 ────────────────
    # 直接測 `epoch_str` 採不到這個：錯在「怎麼把事件時間變成 `_step` 的 `t`」那一步。
    neg = datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=timezone.utc)
    assert v._epoch({"_dt": neg}) == -1, \
        "負的小數 epoch 要往下取整（包含該時刻的那一秒），得到：" + str(v._epoch({"_dt": neg}))
    pos = datetime(2026, 5, 28, 10, 0, 0, 500000, tzinfo=timezone.utc)
    assert v._epoch({"_dt": pos}) == int(pos.timestamp()), \
        "正的 epoch 行為不得改變（真實資料全落在這一側）"

    # ── 情境 4：多步回合 —— 逐步時刻必須是「各步自己的時刻」，不是回合時間 ──────────
    sid4 = "019f0200-0000-7000-8000-0000000005a4"
    s1, s2, s3 = ("2026-05-28T10:00:05.000Z", "2026-05-28T10:00:20.000Z",
                  "2026-05-28T10:00:45.000Z")
    html, md = build("steps", [
        user("d1", None, "2026-05-28T10:00:00.000Z", sid4, "多步回合MULTISTEP。"),
        asst("d2", "d1", s1, sid4, "n1", txt("第一步。")),
        asst("d3", "d2", s2, sid4, "n2", txt("第二步。")),
        asst("d4", "d3", s3, sid4, "n3", txt("第三步。")),
    ], ["2026-05-28T10:00:00.000Z"], [])
    for i, iso in enumerate((s1, s2, s3), start=1):
        want = "步驟 " + str(i) + " · " + hhmmss(iso) + "</span>"
        assert want in html, "逐步時刻應為該步自己的時刻，缺少：" + want
    # 三步時刻互異 → 若誤用回合時間，上面三條至少有兩條會落空
    assert len({hhmmss(x) for x in (s1, s2, s3)}) == 3, "fixture 自身要保證三步時刻互異"

    # ── 情境 4b：呼叫起點早於「第一筆可呈現事件」—— 步驟列要印呼叫起點 ────────────────
    # ⚠ 上面那個 fixture 每一筆都有可呈現內容，`analyze` 的正規化在那裡是 **no-op**（整段刪掉
    # 也照樣過），所以「呼叫起點 vs 第一筆可呈現事件」這一格等於零覆蓋。實測近六成呼叫的首事件
    # 沒有可呈現內容（純 thinking 簽章等），兩者可以差 1〜30+ 秒——那正是不變量①要釘的東西。
    sid4b = "019f0200-0000-7000-8000-0000000005a6"
    c0 = "2026-05-28T10:00:05.000Z"        # 呼叫起點：只有空白 thinking，畫不出來
    c1 = "2026-05-28T10:00:38.000Z"        # 同一次呼叫（同 message.id）的第一筆可呈現內容
    html, md = build("callstart", [
        user("f1", None, "2026-05-28T10:00:00.000Z", sid4b, "呼叫起點CALLSTART。"),
        asst("f2", "f1", c0, sid4b, "p1", [{"type": "thinking", "thinking": "   "}]),
        asst("f3", "f2", c1, sid4b, "p1", txt("第一步的內容。")),
        asst("f4", "f3", "2026-05-28T10:01:10.000Z", sid4b, "p2", txt("第二步。")),
    ], ["2026-05-28T10:00:00.000Z"], [])
    assert "步驟 1 · " + hhmmss(c0) + "</span>" in html, \
        "步驟列要印**呼叫起點**：首事件雖然畫不出來，那次呼叫就是在那時開始的。缺少 " + hhmmss(c0)
    assert "步驟 1 · " + hhmmss(c1) + "</span>" not in html, \
        "步驟 1 不得改印「第一筆可呈現事件」的時刻（" + hhmmss(c1) + "）"
    assert hhmmss(c0) != hhmmss(c1), "fixture 自身要保證兩個時刻不同，否則上面兩條斷言沒有意義"

    # ── 情境 5：切換與相鄰回合落在**同一秒**內 —— 次秒先後不得被截掉 ─────────────
    # history 的時刻本來就有毫秒。若切換時刻被截成整秒，同一秒內「比切換更早」的那一則
    # 會被判成在切換之後，分隔線就畫早了。
    sid5 = "019f0200-0000-7000-8000-0000000005a5"
    html, md = build("subsec", [
        user("e1", None, "2026-05-28T10:00:00.000Z", sid5, "同秒第一則FIRSTMARK。"),
        asst("e2", "e1", "2026-05-28T10:00:00.100Z", sid5, "m1", txt("同秒第二則SECONDMARK。")),
        user("e3", "e2", "2026-05-28T10:00:00.900Z", sid5, "換帳號後THIRDMARK。"),
        asst("e4", "e3", "2026-05-28T10:00:01.500Z", sid5, "m2", txt("之後的回覆。")),
    ], ["2026-05-28T09:59:00.000Z"], ["2026-05-28T10:00:00.900Z"])
    assert html.count('class="acct-sep"') == 1, "同秒情境仍應只有一條分隔線"
    assert html.index("SECONDMARK") < html.index('class="acct-sep"') < html.index("THIRDMARK"),         "切換時刻的次秒精度被截掉時，分隔線會畫在同一秒內較早的那一則之前"
    assert md.index("SECONDMARK") < md.index("換了登入帳號") < md.index("THIRDMARK"),         "MD 同上"

    # ── 情境 6：跨午夜 —— 切帳號線要排在它所屬那天的日期分隔線之後 ─────────────
    # 排在日期線之前會被讀成「換日之前就換了帳號」。
    sid6 = "019f0200-0000-7000-8000-0000000005a6"
    html, md = build("midnight", [
        user("f1", None, "2026-05-28T15:59:30.000Z", sid6, "換日前DAYONE。"),
        asst("f2", "f1", "2026-05-28T15:59:35.000Z", sid6, "m1", txt("前一天的回覆。")),
        user("f3", "f2", "2026-05-28T16:01:00.000Z", sid6, "換日後DAYTWO。"),
        asst("f4", "f3", "2026-05-28T16:01:05.000Z", sid6, "m2", txt("後一天的回覆。")),
    ], ["2026-05-28T15:59:30.000Z"], ["2026-05-28T16:01:00.000Z"])
    day_seps = [m.start() for m in re.finditer(r'class="day-sep"', html)]
    assert len(day_seps) == 2, "跨日應有兩條日期分隔線（起始日＋換日），實得 %d" % len(day_seps)
    i_acct = html.index('class="acct-sep"')
    assert day_seps[1] < i_acct, "切帳號線要排在它所屬那天的日期分隔線之後"
    assert html.index("DAYONE") < day_seps[1] < i_acct < html.index("DAYTWO"), \
        "順序應為：前一天的回合 → 日期線 → 切帳號線 → 後一天的回合"
    assert md.count("換了登入帳號") == 1, "MD 也應恰有一條"
    # MD 沒有日期分隔線：跨日之後的切帳線要自己帶日期，否則只印時分會被讀成起始日的時刻。
    # HTML 那邊有日期線可讀，維持精簡（不重複印日期）。
    want6 = datetime.fromtimestamp(ep("2026-05-28T16:01:00.000Z")).strftime("%m-%d")
    i_md6 = md.index("換了登入帳號")
    assert want6 in md[i_md6:i_md6 + 40], "MD 跨日後的切帳線要帶日期：" + md[i_md6:i_md6 + 40]
    i_h6 = html.index('class="acct-sep"')
    assert want6 not in html[i_h6:i_h6 + 60], (
        "HTML 已有日期線，切帳線不必再印一次日期：" + html[i_h6:i_h6 + 60])

    # 逐步時刻要帶完整日期的 tooltip：跨午夜時光看 00:01:00 無從判斷是哪一天
    step_html = v.render_step_meters(2, None, t=int(datetime(2026, 5, 29, 0, 1, 0).timestamp()))
    assert 'title="2026-05-29 00:01:00"' in step_html, \
        "逐步分隔列應把完整日期掛在 title：" + step_html

    # ── 指紋精度：必須與「顯示定位」同精度 ───────────────────────────────────
    # 同一秒內的切換時刻變動（.700 → .300）會跨過某個回合而改變分隔線位置；指紋若截成整秒，
    # 增量建置就會沿用畫錯位置的舊頁。
    sigp = root / "sig.jsonl"
    sigp.write_text("{}", encoding="utf-8")
    assert v.session_signature(sigp, [1780000000.700]) != v.session_signature(sigp, [1780000000.300]), \
        "同一秒內的切換時刻變動必須改變 session 指紋"

    # ── 情境 7：切換早於第一則 —— 與「晚於最後一則不畫」對稱，兩端都不畫 ───────────
    # 線的作用是把前後隔開；上方空無一物的線會暗示「這份 transcript 裡看得到一次切換」。
    sid7 = "019f0200-0000-7000-8000-0000000005a7"
    html, md = build("before", [
        user("g1", None, "2026-05-28T10:00:00.000Z", sid7, "整場都屬於新帳號MARKFIRST。"),
        asst("g2", "g1", "2026-05-28T10:00:05.000Z", sid7, "m1", txt("回覆。")),
    ], ["2026-05-28T09:00:00.000Z"], ["2026-05-28T09:30:00.000Z"])
    assert html.count('class="acct-sep"') == 0 and md.count("換了登入帳號") == 0, (
        "切換早於第一則時不畫（與晚於最後一則同一條規則）")

    # ── 情境 8：開頭那幾則沒有時間 —— 線的上方不是空的，仍要畫 ────────────────────
    # 「早於第一個**有時間**的回合」與「上面沒有回合」不是同一件事：開頭沒有時間戳時，
    # 拿前者當判準會把落在它們之後的切換整條吃掉（HTML 與 MD 都不見）。
    sid8 = "019f0200-0000-7000-8000-0000000005a8"
    html, md = build("undated", [
        user("h1", None, None, sid8, "沒有時間的第一則NOSTAMP。"),
        user("h2", "h1", "2026-05-28T10:10:00.000Z", sid8, "換帳號後AFTERSTAMP。"),
        asst("h3", "h2", "2026-05-28T10:10:05.000Z", sid8, "m1", txt("回覆。")),
    ], ["2026-05-28T10:00:00.000Z"], ["2026-05-28T10:05:00.000Z"])
    assert html.count('class="acct-sep"') == md.count("換了登入帳號") == 1, (
        "開頭的回合沒有時間時，落在它之後的切換仍要畫，且兩種輸出一致")
    # ⚠ 頁面／檔頭標題取自第一個**有時間**的回合，也含 AFTERSTAMP；位置比較要看主對話裡
    # 那一次（rindex），拿 index 會比到標題上去。
    assert html.index("NOSTAMP") < html.index('class="acct-sep"') < html.rindex("AFTERSTAMP"), (
        "線要落在沒有時間的那一則與第一個有時間的回合之間")
    assert md.index("NOSTAMP") < md.index("換了登入帳號") < md.rindex("AFTERSTAMP"), "MD 同上"

    # ── 情境 9：切換時刻與前一則**完全相等** —— 收在新帳號的 user 之前 ──────────────
    # 切換時刻取自新帳號的第一個 prompt；同刻的 assistant 是切換前那一則，
    # 線收在它前面會把前後兩邊畫反（情境 5 的次秒版本擋不到完全相等這一格）。
    sid9 = "019f0200-0000-7000-8000-0000000005a9"
    teq = "2026-05-28T10:10:00.000Z"
    html, md = build("tie", [
        user("i1", None, "2026-05-28T10:09:00.000Z", sid9, "切換前TIEBEFORE。"),
        asst("i2", "i1", teq, sid9, "m1", txt("同刻的回覆TIEASST。")),
        user("i3", "i2", teq, sid9, "換帳號後TIEAFTER。"),
        asst("i4", "i3", "2026-05-28T10:10:30.000Z", sid9, "m2", txt("之後的回覆。")),
    ], ["2026-05-28T10:09:00.000Z"], [teq])
    assert html.count('class="acct-sep"') == md.count("換了登入帳號") == 1, "同刻情境仍只有一條線"
    assert html.index("TIEASST") < html.index('class="acct-sep"') < html.index("TIEAFTER"), (
        "時刻完全相等時，線要收在新帳號的 user 之前，不是同刻 assistant 之前")
    assert md.index("TIEASST") < md.index("換了登入帳號") < md.index("TIEAFTER"), "MD 同上"

    # ── 情境 10：切換不屬於它插進去的那一天 —— 標籤要自己帶日期 ─────────────────────
    # 切換與下一個可見回合相隔一天以上（中間那些畫不出來）時，線會落在後者的日期底下；
    # 標籤只印時分就會被讀成那一天的時刻。兩端相隔 24h 以上，任何時區下本地日期都不同。
    sid10 = "019f0200-0000-7000-8000-0000000005aa"
    mark10 = "2026-05-29T10:00:00.000Z"
    html, md = build("crossday", [
        user("j1", None, "2026-05-28T10:00:00.000Z", sid10, "切換前CROSSBEFORE。"),
        asst("j2", "j1", "2026-05-28T10:00:05.000Z", sid10, "m1", txt("回覆。")),
        user("j3", "j2", "2026-05-30T10:00:00.000Z", sid10, "兩天後CROSSAFTER。"),
        asst("j4", "j3", "2026-05-30T10:00:05.000Z", sid10, "m2", txt("後來的回覆。")),
    ], ["2026-05-28T10:00:00.000Z"], [mark10])
    want_day = datetime.fromtimestamp(ep(mark10)).strftime("%m-%d")
    i_sep = html.index('class="acct-sep"')
    assert want_day in html[i_sep:i_sep + 200], (
        "線所屬的日期與插入處不同時，標籤要帶日期（缺 %s）：%s" % (want_day, html[i_sep:i_sep + 160]))
    i_sep_md = md.index("換了登入帳號")
    assert want_day in md[i_sep_md - 40:i_sep_md + 60], "MD 同上：" + md[i_sep_md - 40:i_sep_md + 60]
    # 位置也要對：線屬於更早的那一天，排在後一天的日期列**之後**會讓畫面上的時間軸倒退
    # （日期列寫 05-30、它下面第一條線卻寫 05-29）。
    day_seps10 = [m.start() for m in re.finditer(r'class="day-sep"', html)]
    assert len(day_seps10) == 2, "跨兩天應有兩條日期線，實得 %d" % len(day_seps10)
    assert day_seps10[0] < i_sep < day_seps10[1], (
        "屬於更早那一天的切帳號線，要排在後一天的日期列之前")

    # 同一天就不要多印日期（不是「一律加上去」，那會讓常見情形變吵）
    ts10 = ep(mark10)
    same_day = datetime.fromtimestamp(ts10).strftime("%Y-%m-%d")
    assert v.acct_sep_label(ts10, same_day) == (
        "換了登入帳號 · " + datetime.fromtimestamp(ts10).strftime("%H:%M")), (
        "所屬日期相同時只印時分：" + v.acct_sep_label(ts10, same_day))
    assert want_day in v.acct_sep_label(ts10, "2999-01-01"), "日期不同時要補上日期"
    assert v.acct_sep_label(ts10) == v.acct_sep_label(ts10, ""), "沒給日期就維持原樣"

    # ── 情境 11：增量建置 —— 切換時刻在同一秒內移動，沿用的頁面必須跟著重建 ─────────────
    # 那份時刻來自 repo 外的 history.jsonl：transcript 一個 byte 都沒動，分隔線位置卻會變。
    # 只比對指紋字串看不出這件事——要真的建置兩次，看沿用那條路徑上的線有沒有跟著動。
    sid11 = "019f0200-0000-7000-8000-0000000005ab"
    tmp11 = root / "incr"
    cfg_a11, cfg_b11 = tmp11 / "cfgA", tmp11 / "cfgB"
    proj11 = cfg_a11 / "projects" / "demo-proj"
    proj11.mkdir(parents=True, exist_ok=True)
    (cfg_b11 / "projects").mkdir(parents=True, exist_ok=True)
    (proj11 / (sid11 + ".jsonl")).write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        user("k1", None, "2026-05-28T10:00:00.000Z", sid11, "第一則INCRONE。"),
        asst("k2", "k1", "2026-05-28T10:00:00.500Z", sid11, "m1", txt("同秒的回覆INCRTWO。")),
        user("k3", "k2", "2026-05-28T10:00:00.900Z", sid11, "第三則INCRTHREE。"),
        asst("k4", "k3", "2026-05-28T10:00:02.000Z", sid11, "m2", txt("最後的回覆。")),
    ]), encoding="utf-8")
    out11 = tmp11 / "out"
    home11 = tmp11 / "home"
    home11.mkdir(parents=True, exist_ok=True)
    env11 = dict(os.environ, HOME=str(home11), USERPROFILE=str(home11))

    def build11(mark_iso):
        """只改 history 的切換時刻（transcript 不動），重跑一次建置。回傳 (輸出訊息, html)。"""
        for cfg, rows in ((cfg_a11, ["2026-05-28T09:59:00.000Z"]), (cfg_b11, [mark_iso])):
            (cfg / "history.jsonl").write_text("\n".join(
                json.dumps({"display": "x", "timestamp": int(ep(x) * 1000), "sessionId": sid11})
                for x in rows) + "\n", encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--claude-source", "A=" + str(cfg_a11 / "projects"),
             "--claude-source", "B=" + str(cfg_b11 / "projects"),
             "--no-codex", "--out", str(out11)],
            capture_output=True, text=True, encoding="utf-8", env=env11)
        assert r.returncode == 0, "非零退出\nSTDOUT:" + r.stdout + "\nSTDERR:" + r.stderr
        page = [q for q in _session_pages(out11)][0].read_text(encoding="utf-8")
        # ⚠ 一定要去掉 script／style 再做位置比較：session 標題（＝第一則使用者訊息，
        #   本測試的素材就是「第一則INCRONE。」）自書籤第 2 期起會**再出現一次**在頁尾的
        #   `BK_TITLE`，`rindex` 會指到那裡，下面三條就永遠是假的。見 `_page_body`。
        return (r.stdout + r.stderr), _page_body(page)

    _, h11a = build11("2026-05-28T10:00:00.700Z")
    assert h11a.index("INCRTWO") < h11a.index('class="acct-sep"') < h11a.index("INCRTHREE"), (
        ".700 的切換要落在同一秒內的前後兩則之間")
    log11b, h11b = build11("2026-05-28T10:00:00.300Z")
    assert "新建/更新 1" in log11b, (
        "切換時刻在同一秒內變動（transcript 沒動）必須觸發重建，實得：" + log11b.strip()[-200:])
    assert h11b.rindex("INCRONE") < h11b.index('class="acct-sep"') < h11b.index("INCRTWO"), (
        "重建後分隔線要跟著移到 .300 該在的位置")
    log11c, h11c = build11("2026-05-28T10:00:00.300Z")
    assert "沿用 1" in log11c, "什麼都沒變就該沿用，實得：" + log11c.strip()[-200:]
    assert h11c.rindex("INCRONE") < h11c.index('class="acct-sep"') < h11c.index("INCRTWO"), (
        "沿用的頁面仍須保有位置正確的分隔線")

    # ── 毫秒正規化：指紋用 %.3f，切換時刻的精度就不得比毫秒更細 ────────────────────
    # 更細的話，同一秒內相差次毫秒、卻落在同一回合**兩側**的兩個標記會產生相同的指紋字串
    # → 顯示定位看得出差異、指紋看不出來 → 增量建置沿用畫錯位置的舊頁。
    cfg_m1, cfg_m2 = root / "ms" / "cfgA", root / "ms" / "cfgB"
    for c in (cfg_m1, cfg_m2):
        c.mkdir(parents=True, exist_ok=True)
    sid_ms = "019f0200-0000-7000-8000-0000000005ac"
    (cfg_m1 / "history.jsonl").write_text(json.dumps(
        {"display": "x", "timestamp": 1780000000000, "sessionId": sid_ms}) + "\n", encoding="utf-8")
    (cfg_m2 / "history.jsonl").write_text(json.dumps(
        {"display": "x", "timestamp": 1780000600000.4001, "sessionId": sid_ms}) + "\n", encoding="utf-8")
    mk = v.load_account_switches([cfg_m1, cfg_m2]).get(sid_ms) or []
    assert mk, "同一 sessionId 出現在兩個帳號的 history，應偵測得到切換"
    assert all(float("%.3f" % t) == t for t in mk), (
        "切換時刻必須正規化到毫秒，否則會與 session_signature 的 %%.3f 精度不一致：" + repr(mk))
    assert v.session_signature(sigp, mk) == v.session_signature(sigp, [round(x, 3) for x in mk]), (
        "指紋不得因次毫秒殘留而漂移")

    # ── 邊界：沒有時間的回合不推進索引；取不出來的時刻不顯示也不炸 ─────────────────
    marks = [100, 200]
    assert v.pending_acct(marks, 0, {"dt": None}) == ([], 0), "沒有時間的回合不得吃掉切換點"
    assert v.pending_acct(marks, 0, {"dt": datetime.fromtimestamp(150, timezone.utc)}) == ([100], 1), \
        "只補畫已經跨過的那些"
    # 完全相等：同刻的 assistant 不收線（它在切換之前），同刻的 user 才收
    at100 = datetime.fromtimestamp(100, timezone.utc)
    assert v.pending_acct(marks, 0, {"dt": at100, "role": "assistant"}) == ([], 0), (
        "同刻的 assistant 是切換前那一則，不得在它之前收線")
    assert v.pending_acct(marks, 0, {"dt": at100, "role": "user"}) == ([100], 1), (
        "同刻的 user 是新帳號的第一則，線要收在它之前")

    # acct_marks 最前端的判準：看「上面還有沒有回合」，不是「早於第一個有時間的回合」
    class _Fake:
        pass
    at200 = datetime.fromtimestamp(200, timezone.utc)
    f1 = _Fake(); f1.acct_times = [150.0]; f1.main_groups = [{"dt": None}, {"dt": at200}]
    assert v.acct_marks(f1) == [150.0], "第一個有時間的回合上面還有回合 → 不得擋掉"
    f2 = _Fake(); f2.acct_times = [150.0]; f2.main_groups = [{"dt": at200}]
    assert v.acct_marks(f2) == [], "上面真的沒有回合 → 照舊不畫"
    f3 = _Fake(); f3.acct_times = [150.0]; f3.main_groups = [{"dt": None}]
    assert v.pending_acct(v.acct_marks(f3), 0, {"dt": None}) == ([], 0), (
        "整場都沒有時間的回合：留著標記也畫不出來，不得因此炸掉")
    # epoch_str 的契約：取不出來就回空字串，絕不讓建置失敗。失敗路徑用「一定超出範圍」的值測
    # ——負值在各平台行為不同（Windows 丟 OSError、POSIX 直接給 1969），不能拿來當跨平台契約。
    BAD = 10 ** 18
    assert v.epoch_str(None) == "", "None 應回空字串"
    assert v.epoch_str(BAD) == "", "超出範圍的時刻應回空字串，不得拋例外"
    assert v.epoch_str(0) != "", "0 是合法時刻（1970-01-01），不得與「取不出來」同形"
    assert v.render_step_meters(1, None, t=BAD).startswith("<div"), "取不出時刻仍要畫得出分隔列"
    assert "步驟 1</span>" in v.render_step_meters(1, None, t=BAD), "取不出時刻就整個不印，不留半截"
    assert v.acct_sep_label(BAD) == "換了登入帳號", "取不出時刻就只留標籤，不留半截"
    print("OK: acct separator and step time test passed")



def test_scope_notes_without_cold():
    """零冷啟時三段揭露仍要出現在 ②——它們講的是偵測範圍與樣本排除，與有沒有冷啟無關。

    三段若包在 `if total_cold:` 裡，「一律揭露，不看數字是不是 0」那句註解在
    零冷啟那一支就是假的：最需要保留懷疑的那一格反而什麼都不說。實測本機有 289 個
    「有 cache_steps、零冷啟成因」的 session，只用它們建報告時 `srv_excluded` 仍有 75 對
    要揭露——包在 `if` 裡就會一句都不出。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    base = 1780000000
    unavail = v.API_MISS_CODES.index("unavailable")

    def hit(t, code=0):
        # cache_read/脈絡 = 90% → 遠高於冷啟門檻，這一步不是冷啟；自報成因走 st[7]。
        return [t, 90000, 100000, 100000, 0, 100000, 0, code, 0]

    row = {"cache_steps": [hit(base), hit(base + 600, unavail)],
           "cache_models": ["claude-opus-4-5"], "cache_events": [],
           "source_kind": v.SOURCE_CLAUDE, "kind": "chat", "account": "a",
           "start_ts": float(base)}
    d = v.build_cache_report([row])

    # 前提：這個 fixture 必須真的零冷啟，否則走的是另一支、測不到本條。
    assert sum(d["causes_total"].values()) == 0, "fixture 應為零冷啟"
    assert d["api"]["srv_excluded"] == 1, "unavailable 的那一對應被記進 srv_excluded"

    for tag, text, is_html in (("HTML", v.render_cache_report_html(d), True),
                               ("MD", v.render_cache_report_md(d), False)):
        assert "期間內沒有冷啟" in text, f"{tag}：應走零冷啟那一支"
        # 比對整段揭露本文（不是關鍵字）：改了措辭而忘了兩支都出時，這裡才會紅。
        scope = v._acct_scope_note(d, html=is_html)
        srv = v._srv_excluded_note(d, html=is_html)
        assert scope and scope in text, f"{tag}：零冷啟時仍要揭露切帳號偵測範圍"
        assert srv and srv in text, f"{tag}：零冷啟時仍要揭露因伺服器不可用移出的對數"

    print("OK: scope notes without cold test passed")



def test_prompt_loss_backstop(tmp_path=None):
    """prompt 事件與 `task_started` 一起漂掉時，仍要有一條哨兵出聲。

    上面那條「回合開了卻零則 prompt」雖然改看結果，卻仍要先有 `event_msg:task_started`
    才數得出窗；上游若在同一版把兩者一起改名／拿掉，`turn_open` 永遠是 False，
    連同五個形狀哨兵**六個一起歸零**——正是它們要擋的樣態。
    ⚠ 這條**不可以**改用 `s.events` 裡 type=="user" 的則數：工具結果也是以 user 存的。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    tmp = new_tmp(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-06-09T04:{sec // 60:02d}:{sec % 60:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    def body(prompt_type, with_started):
        out = [line(0, "session_meta", {"id": "019f0009-0000-7000-8000-00000000000f",
                                        "cwd": "/x/Backstop", "cli_version": "0.147.0"}),
               line(1, "turn_context", {"cwd": "/x/Backstop", "model": "gpt-5.6"})]
        if with_started:
            out.append(line(2, "event_msg", {"type": "task_started", "turn_id": "t-1"}))
        out.append(line(3, "event_msg", {"type": prompt_type, "message": "使用者問句BACKSTOPQ。"}))
        # 工具結果也是以 user 存進 s.events —— 有它在，用 s.events 數 prompt 的寫法會漏報
        out += [line(4, "response_item", {"type": "function_call", "name": "sh",
                                          "call_id": "c1", "arguments": "{}"}),
                line(5, "response_item", {"type": "function_call_output", "call_id": "c1",
                                          "output": "工具結果TOOLOUT。"}),
                line(6, "response_item", {"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text",
                                                       "text": "助理回答BACKSTOPA。"}]})]
        return "\n".join(out)

    def run(prompt_type, with_started, name):
        f = tmp / f"rollout-2026-06-09T04-00-00-{name}.jsonl"
        f.write_text(body(prompt_type, with_started), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            s = v.load_codex_session(f)
        return s, buf.getvalue()

    # ① 正常資料不得出聲（誤報會讓所有警告很快沒人讀）
    ok, err = run("user_message", True, "ok")
    assert not err.strip(), f"正常資料不該有警告，實得：{err!r}"

    # ② prompt 改名但留著 task_started → 舊那條（回合窗）接得住
    _, err_win = run("Prompt", True, "win")
    assert "回合開始了卻沒收到任何使用者 prompt" in err_win, f"回合窗哨兵應出聲：{err_win!r}"

    # ③ prompt 改名且沒有 task_started → 只剩底線哨兵接得住（改動前這裡完全無聲）
    drift, err_bs = run("Prompt", False, "bs")
    assert "整場零則使用者 prompt" in err_bs, (
        f"prompt 與 task_started 一起漂掉時仍要出聲，實得：{err_bs!r}")

    # ④ 底線哨兵不可以靠 s.events 的 user 則數：工具結果就是以 user 存的，
    #    這個 fixture 裡 prompt 全丟了但 s.events 仍有 user 事件。
    assert any(e.get("type") == "user" for e in drift.events), (
        "fixture 應含以 user 存的工具結果，否則測不到「不可用 s.events 數 prompt」那一半")

    # ⑤ 同一個根因不得出兩行警告
    assert err_win.count("!") == 1, f"同一根因只該出一行警告，實得：{err_win!r}"

    print("OK: prompt loss backstop test passed")



def test_response_item_type_drift(tmp_path=None):
    """`response_item` 的型別改名／content 元素改名，都要出聲。

    容器那條哨兵只在 payload **不是 dict** 時計數；歷史上真正發生過的漂移（0.147）卻是
    **型別改名**，容器好端端的。改名之後助理訊息／reasoning／工具呼叫整批解析不出來，
    頁面變成「N 則提問、零則回答」，而 `n_empty_turns` 那條被 `n_ai_turns` 擋住
    （助理事件正好是 0）→ 六個哨兵全靜。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    tmp = new_tmp(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-06-10T05:{sec // 60:02d}:{sec % 60:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    def body(msg_type, elem_type):
        return "\n".join([
            line(0, "session_meta", {"id": "019f000a-0000-7000-8000-000000000010",
                                     "cwd": "/x/Drift", "cli_version": "0.147.0"}),
            line(1, "turn_context", {"cwd": "/x/Drift", "model": "gpt-5.6"}),
            line(2, "event_msg", {"type": "task_started", "turn_id": "t-1"}),
            line(3, "event_msg", {"type": "user_message", "message": "使用者問句DRIFTQ。"}),
            line(4, "response_item", {"type": msg_type, "role": "assistant",
                                      "content": [{"type": elem_type, "text": "助理回答DRIFTA。"}]}),
        ])

    def run(msg_type, elem_type, name):
        f = tmp / f"rollout-2026-06-10T05-00-00-{name}.jsonl"
        f.write_text(body(msg_type, elem_type), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            s = v.load_codex_session(f)
        n_ai = sum(1 for e in s.events if e.get("type") == "assistant")
        return n_ai, buf.getvalue()

    # ① 正常資料：收得到助理內容，且不得出聲
    n_ai, err = run("message", "output_text", "ok")
    assert n_ai == 1, f"正常資料應收到 1 則助理內容，實得 {n_ai}"
    assert not err.strip(), f"正常資料不該有警告，實得：{err!r}"

    # ② payload.type 改名 → 助理內容整批消失，必須出聲
    n_ai, err = run("Message", "output_text", "typedrift")
    assert n_ai == 0, "前提：改名後助理內容應該解析不出來（否則測的不是這件事）"
    assert "payload.type 不認得" in err, f"型別改名時要出聲，實得：{err!r}"

    # ③ content 元素型別改名 → 型別白名單接不到，只剩這一條看得見
    n_ai, err = run("message", "text_out", "elemdrift")
    assert n_ai == 0, "前提：元素改名後應該抽不出文字"
    assert "抽不出文字" in err, f"content 元素改名時要出聲，實得：{err!r}"

    # ④ 白名單要蓋住現有語料裡出現過、但本工具刻意不呈現的型別，否則哨兵會對真實資料狂叫。
    for known in ("web_search_call", "tool_search_call", "tool_search_output", "agent_message"):
        assert known in v._CODEX_HANDLED_RESPONSE_ITEMS, (
            f"{known} 在真實語料裡出現過，不列進白名單會變成誤報來源")

    print("OK: response_item type drift test passed")



def test_forced_switch_parallel_label():
    """撞過 limit 之後才換的帳號：並列標示，但**記帳不變**。

    `boundary` 的「被迫/自願」只看同一對相鄰步之間的事件，429 與切換中間夾了任何一次呼叫，
    那次被迫切換就落成 `acct`（人因、記錢、紅色徽章）。全語料實測 10 個 acct 標籤裡有 3 個
    是這種（間隔 5005／4942／11584 秒、$9.05 ＝ 人因浪費的 3.03%）。
    規則：並列顯示 ＋ 報告揭露，**不動成因也不動金額**。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    base = 1780000000
    models = ["claude-opus-4-5"]

    def cold(t):
        # cache_read/脈絡 = 0 → 冷啟
        return [t, 0, 100000, 100000, 0, 100000, 0, 0, 0]

    def row(events):
        return {"cache_steps": [cold(base), cold(base + 60), cold(base + 120)],
                "cache_models": models, "cache_events": events,
                "source_kind": v.SOURCE_CLAUDE, "kind": "chat", "account": "a",
                "start_ts": float(base)}

    # 429 在第一對、切換在第二對 → 兩者被一次呼叫隔開
    sep = [[base + 30, "limit"], [base + 90.5, "acct"]]
    masked = {}
    causes = v.classify_cache_causes(row(sep)["cache_steps"], models, sep, masked)
    key = v._cause_key(base + 120, 0)
    assert causes.get(key) == "acct", f"前提：隔開之後這一步仍被判成 acct，實得 {causes.get(key)!r}"
    assert masked.get(key) == ["limit_earlier"], (
        f"同場稍早撞過 limit 要並列標出，實得 {masked.get(key)!r}")

    # 記帳不得改變：成因仍是 acct，金額照算
    d = v.build_cache_report([row(sep)])
    assert d["causes_total"]["acct"] == 1, "成因不得因為並列而改變"
    # 這個 fixture 的成因是 first / switch / acct，`_HUMAN_CAUSES` 只認 expiry+acct → 1。
    assert d["kpi"]["avoid_n"] == 1, "人因次數不得因為並列而改變（switch 不是人因）"
    assert d["kpi"]["avoid_usd"] > 0, "並列不得把金額洗掉——記帳仍要照 acct 算"
    assert d["api"]["acct_limit_earlier"] == 1, "報告要數得出這種步，才揭露得了"
    assert "稍早出現過 429/401" in v._acct_scope_note(d, html=False), (
        "統計頁要說明這個誤報方向——它是會記到錢的那一種")

    # 對照組：429 與切換落在同一對之間 → 本來就判得出被迫，不得多標
    same = [[base + 70, "limit"], [base + 90.5, "acct"]]
    masked2 = {}
    causes2 = v.classify_cache_causes(row(same)["cache_steps"], models, same, masked2)
    assert causes2.get(key) == "switch", f"同一對內有 limit 應判成 switch，實得 {causes2.get(key)!r}"
    assert "limit_earlier" not in (masked2.get(key) or []), "同一對內就判得出來的不該再並列"

    # tooltip 不得自相矛盾：修正語要排在成因說明**之後**
    title = v._cold_cause_title("acct", ["limit_earlier"], "這一步的成因")
    assert title.index("沒撞 limit」只對這一對") > title.index("這場中途換了帳號（沒撞 limit）"), (
        "補述要排在成因說明之後才是修正，排在前面就是兩句話互相否定")
    assert "**" not in title, "tooltip 是純文字 title 屬性，不可留 markdown 星號"

    # 顯示層專用鍵不得混進成因母體，否則成因表會多一列永遠是 0 的假成因
    assert "limit_earlier" not in v._COLD_CAUSE_LABEL, "並列條件不是成因，不可進 REPORT_CAUSES"

    print("OK: forced switch parallel label test passed")



def test_limits_section_accumulator():
    """撞牆時刻是**跨 session 累加**的，不可被逐 row 的區域變數蓋掉。

    在逐 row 迴圈內新建一個同名的 `limit_ts` 會把迴圈外那個累加器整個覆寫，
    於是 `limits_n` 只剩最後一個 row 的殘留、而且混進了 auth。實資料上 100 → 0，
    而 `if d["limits_n"]:` 是報告 ④「什麼時候撞到 limit」整段的開關
    → **那一章連同時段表一起靜默消失**，測試全綠、沒有任何提示。

    這條測試釘三件事：跨 row 累加、折疊規則、以及「最後一個 row 沒有 429 也不能歸零」。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    base = 1780000000

    def row(events, t0):
        # 兩步、命中，成因不是本條的重點；只要 row 進得了 Claude 那條路徑即可
        return {"cache_steps": [[t0, 90000, 100000, 100000, 0, 100000, 0, 0, 0],
                                [t0 + 600, 90000, 100000, 100000, 0, 100000, 0, 0, 0]],
                "cache_models": ["claude-opus-4-5"], "cache_events": events,
                "source_kind": v.SOURCE_CLAUDE, "kind": "chat", "account": "a",
                "start_ts": float(t0)}

    a = row([[base + 60, "limit"]], base)                       # 1 次
    b = row([[base + 86400 + 60, "limit"]], base + 86400)       # 1 次（另一場）
    c = row([[base + 172800 + 60, "auth"]], base + 172800)      # auth 不算撞牆
    d_ = row([[base + 259200 + 60, "limit"],
              [base + 259200 + 120, "limit"]], base + 259200)   # 10 分內折疊 → 1 次

    # ① 跨 row 累加：三個 row 各一次 429（c 是 auth，不算）
    assert v.build_cache_report([a, b, c])["limits_n"] == 2, "429 要跨 session 累加，且 auth 不算"

    # ② 最後一個 row 沒有 429 時，前面幾個 row 的不可以跟著消失
    #    （逐 row 覆寫的寫法在這裡會回 0）
    assert v.build_cache_report([a, b, c])["limits_n"] == 2, "最後一個 row 無 429 不得讓累計歸零"

    # ③ 同一場 10 分鐘內折疊為一次，且單一 row 不得被重複計
    assert v.build_cache_report([a])["limits_n"] == 1, "單一 row 的一次 429 只能算一次"
    assert v.build_cache_report([d_])["limits_n"] == 1, "同場 10 分內的兩次 429 要折疊成一次"

    # ④ 時段分布要跟著有值，否則 ④ 段的表是空的
    rep = v.build_cache_report([a, b, d_])
    assert rep["limits_n"] == 3
    assert sum(rep["limit_hours"]) == rep["limits_n"], "時段直方圖的總數要等於撞牆次數"
    assert rep["limits_wd"] + rep["limits_we"] == rep["limits_n"], "平日＋假日要等於總數"

    print("OK: limits section accumulator test passed")



def test_message_role_and_content_drift(tmp_path=None):
    """`role` 改名／缺欄、`content` 欄改名——與型別改名同症狀，但既有哨兵接不到。

    助理訊息是靠 `role == "assistant"` 認出來的，role 一改名就被 `continue` 直接吞掉；
    而 `n_empty_assistant_items` 的守門要求 content 是**非空 list**，content 欄改名後
    取到 None、守門不成立，那一格也不出聲。兩種都是「N 則提問、零則回答」＋ stderr 全靜。
    實測 516 份 rollout：role 只有 developer/user/assistant、content 恆為非空 list → 零誤報。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    tmp = new_tmp(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-06-11T06:{sec // 60:02d}:{sec % 60:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    def run(msg_payload, name):
        f = tmp / f"rollout-2026-06-11T06-00-00-{name}.jsonl"
        f.write_text("\n".join([
            line(0, "session_meta", {"id": "019f000b-0000-7000-8000-000000000011",
                                     "cwd": "/x/RoleDrift", "cli_version": "0.147.0"}),
            line(1, "turn_context", {"cwd": "/x/RoleDrift", "model": "gpt-5.6"}),
            line(2, "event_msg", {"type": "task_started", "turn_id": "t-1"}),
            line(3, "event_msg", {"type": "user_message", "message": "使用者問句ROLEQ。"}),
            line(4, "response_item", msg_payload),
        ]), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            s = v.load_codex_session(f)
        return sum(1 for e in s.events if e.get("type") == "assistant"), buf.getvalue()

    good = {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "助理回答ROLEA。"}]}

    # ① 正常資料：收得到、不出聲
    n_ai, err = run(dict(good), "ok")
    assert n_ai == 1 and not err.strip(), f"正常資料不該有警告：{err!r}"

    # ② role 改名 → 助理內容整批消失，必須出聲
    bad_role = dict(good, role="model")
    n_ai, err = run(bad_role, "role")
    assert n_ai == 0, "前提：role 改名後助理內容應消失"
    assert "role 不認得" in err, f"role 改名要出聲：{err!r}"

    # ③ role 缺欄（不是改名，是整個沒有）→ 同樣要出聲
    no_role = {k: xv for k, xv in good.items() if k != "role"}
    n_ai, err = run(no_role, "norole")
    assert n_ai == 0 and "role 不認得" in err, f"role 缺欄要出聲：{err!r}"

    # ④ content 欄改名 → 型別白名單與 role 白名單都接不到，只剩這一條
    bad_content = {"type": "message", "role": "assistant",
                   "body": [{"type": "output_text", "text": "助理回答ROLEA。"}]}
    n_ai, err = run(bad_content, "content")
    assert n_ai == 0, "前提：content 欄改名後應抽不出內容"
    assert "content 不是非空清單" in err, f"content 欄改名要出聲：{err!r}"

    # ⑤ 已知 role 三種都不得誤報（developer/user 是真實語料裡就有的）
    for r in ("user", "developer"):
        _n, err = run(dict(good, role=r), "role_" + r)
        assert "role 不認得" not in err, f"{r} 是真實語料裡就有的 role，不得誤報：{err!r}"

    print("OK: message role & content drift test passed")



def test_json_string_content(tmp_path=None):
    """使用者 prompt 的**字串**一律原樣收下，看起來像 JSON 也不解析。

    從字串內容分不出「上游把 content 序列化了」還是「使用者真的把一段 JSON 貼進來當問題」
    （後者甚至可以長得和 content block 陣列一模一樣）。解析它就可能把使用者的原文換成
    抽出來的片段——那是竄改，比顯示得難看更糟。所以：
    ① 舊格式 `user_message.message`：字串是正常形狀 → 原樣、不出聲；
    ② 新格式 `UserMessage.content`：正常形狀是 block 陣列，字串＝上游換了形狀 →
       **原樣收下，但要出聲**（不管字串看起來像不像 JSON）。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    tmp = new_tmp(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)
    blocks = [{"type": "text", "text": "真正的問句JSONQ"}, {"type": "image", "url": "x"}]

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-06-12T08:00:{sec:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    def run(user_event, name):
        f = tmp / f"rollout-2026-06-12T08-00-00-{name}.jsonl"
        f.write_text("\n".join([
            line(0, "session_meta", {"id": "019f000c-0000-7000-8000-000000000012",
                                     "cwd": "/x/Json", "cli_version": "0.147.0"}),
            line(1, "turn_context", {"cwd": "/x/Json", "model": "gpt-5.6"}),
            line(2, "event_msg", {"type": "task_started", "turn_id": "t-1"}),
            user_event,
            line(5, "response_item", {"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "回答JSONA。"}]}),
        ]), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            s = v.load_codex_session(f)
        texts = []
        for e in s.events:
            if e.get("type") != "user":
                continue
            c = (e.get("message") or {}).get("content")
            if isinstance(c, str):
                texts.append(c)
            else:
                texts += [b.get("text", "") for b in (c or []) if isinstance(b, dict)]
        return texts, buf.getvalue()

    typed = json.dumps(blocks, ensure_ascii=False)     # 長得和 content block 陣列一模一樣的字串

    # ① 新格式：item.content 是字串（看起來像 JSON）→ 原文一個字都不能動，但要出聲
    t, err = run(line(3, "event_msg", {"type": "item_completed", "item": {
        "type": "UserMessage", "id": "i1", "content": typed}}), "new")
    assert t == [typed], f"新格式的字串 content 要原樣收下、不可解析，實得 {t!r}"
    assert "content 是字串" in err, f"新格式的 content 是字串＝上游換了形狀，要出聲：{err!r}"

    # ② 新格式：一般字串 → 同樣原樣收下、同樣出聲（形狀不對，和像不像 JSON 無關）
    t, err = run(line(3, "event_msg", {"type": "item_completed", "item": {
        "type": "UserMessage", "id": "i2", "content": "新格式一般問句NEWSTR"}}), "newplain")
    assert t == ["新格式一般問句NEWSTR"], f"實得 {t!r}"
    assert "content 是字串" in err, f"形狀不對就要出聲：{err!r}"

    # ③ ⚠ 舊格式：字串本身是一段**含圖片 block** 的 JSON 陣列 → 原文一個字都不能動；
    #   但若那真是上游序列化的內容，圖片就沒被畫出來——要出聲（講「可能」，不講「一定」）
    t, err = run(line(3, "event_msg", {"type": "user_message", "message": typed}), "legacy")
    assert t == [typed], f"使用者貼的 JSON 不可被解析掉（那是竄改），實得 {t!r}"
    assert "非文字 block" in err, f"含非文字 block 的 JSON 字串要出聲（否則是看不見的遺失）：{err!r}"
    # ③-b 只有文字 block 的 JSON 陣列：就算真是序列化的，也沒有東西沒畫出來 → 不出聲
    text_only = json.dumps([{"type": "text", "text": "只有文字JSONT"}], ensure_ascii=False)
    t, err = run(line(3, "event_msg", {"type": "user_message", "message": text_only}), "legacytext")
    assert t == [text_only] and not err.strip(), f"只有文字的 JSON 字串不該出聲：{t!r} {err!r}"
    # ③-c 新格式的空字串：實際上什麼都沒收下 ⇒ 不可以說「已原樣收下」
    t, err = run(line(3, "event_msg", {"type": "item_completed", "item": {
        "type": "UserMessage", "id": "i3", "content": ""}}), "newempty")
    assert "content 是字串" not in err, f"空字串沒有被收下，警告不可以說「已原樣收下」：{err!r}"
    assert "取不出文字" in err, f"空的 prompt 仍要由「取不出文字」那條出聲：{err!r}"
    # 只有空白的字串同理（實際上同樣什麼都沒收下）
    t, err = run(line(3, "event_msg", {"type": "item_completed", "item": {
        "type": "UserMessage", "id": "i4", "content": "   "}}), "newblank")
    assert "content 是字串" not in err, f"只有空白的字串沒有被收下，不可以說「已原樣收下」：{err!r}"

    # ④ 舊格式：其他 JSON 與一般字串同樣不受影響
    raw = "[1, 2, 3]"
    t, err = run(line(3, "event_msg", {"type": "user_message", "message": raw}), "userjson")
    assert t == [raw] and not err.strip(), f"實得 {t!r} {err!r}"
    t, err = run(line(3, "event_msg", {"type": "user_message", "message": "一般問句PLAIN"}), "plain")
    assert t == ["一般問句PLAIN"] and not err.strip(), f"一般字串不得受影響：{t!r} {err!r}"

    print("OK: json string content test passed")



def test_history_partial_blank_sentinel(tmp_path=None):
    """**部分** history 認不得時也要出聲，不能只在全部都認不得時才說。

    切帳號是靠**跨帳號比對**成立的：少掉一邊，這條路徑就實質失效，而回傳值與
    「真的沒切過」完全同形。條件若寫成 `h["files"] and not by_sid`（全部都認不得），
    只要還有一份讀得出來就一聲不吭。
    順帶釘住 `files` 只算**開得起來**的那幾份（在 open 之前就 +1 的話，開檔失敗時
    `files` 與 `read_errors` 同時加一，揭露句的「讀到 N 份」高估涵蓋率）。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")

    tmp = new_tmp(tmp_path)

    def cfg(name, rows):
        c = tmp / name
        c.mkdir(parents=True, exist_ok=True)
        (c / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return c

    good = [{"sessionId": f"s{i}", "timestamp": 1780000000000 + i * 1000} for i in range(5)]
    drift = [{"sid": f"s{i}", "ts": 1780000000000 + i * 1000} for i in range(5)]   # 欄位改名

    # ① 一份好的 ＋ 兩份漂掉 → 「全部都認不得」不成立（by_sid 非空），哨兵仍必須出聲
    h = {}
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        v.load_account_switches([cfg("a", good), cfg("b", drift), cfg("c", drift)], h)
    assert h["files_blank"] == 2, f"應數出 2 份有列卻認不得，實得 {h.get('files_blank')!r}"
    assert "份有列、卻一列都認不得" in buf.getvalue(), f"部分漂掉要出聲：{buf.getvalue()!r}"

    # ② 全部都好 → 不得誤報
    h2 = {}
    buf2 = io.StringIO()
    with contextlib.redirect_stderr(buf2):
        v.load_account_switches([cfg("d", good), cfg("e", good)], h2)
    assert h2["files_blank"] == 0 and not buf2.getvalue().strip(), (
        f"正常資料不得出聲：{buf2.getvalue()!r}")

    # ③ `files` 只算開得起來的：不存在的 config 目錄不得讓 files 加一
    h3 = {}
    with contextlib.redirect_stderr(io.StringIO()):
        v.load_account_switches([cfg("f", good), tmp / "does-not-exist"], h3)
    assert h3["files"] == 1, f"files 只該算真的讀到的那幾份，實得 {h3.get('files')!r}"

    print("OK: history partial blank sentinel test passed")


def test_durable_anchor(tmp_path=None):
    """耐久回合錨點：格式、UTC、撞號 tiebreak、沒有時間的回合，以及 t{n} 不受影響。

    ⚠⚠ **為什麼要用合成輸入**：撞號在真實語料只有 1/9047、`dt is None` 是 0。
    **「掃了全部語料 0 誤報」不能當成「這條程式碼跑得起來」的證據**——零誤報的路徑
    正是沒被執行的路徑。這裡把兩條路各強制走一次。
    """
    sys.path.insert(0, str(ROOT))   # 全檔 17 個測試裡唯一漏掉的一個
    import ai_session_viewer as v
    from datetime import datetime, timedelta, timezone as _tz

    # --- 1. 格式與 UTC 換算 -------------------------------------------------
    dt = datetime(2026, 8, 1, 2, 52, 33, 123456, tzinfo=_tz.utc)
    assert v.durable_anchor(dt) == "k20260801T025233123Z", v.durable_anchor(dt)
    # 同一時刻換個時區表示，錨點必須一模一樣（否則換時區＝書籤全滅）
    dt_local = dt.astimezone(_tz(timedelta(hours=8)))
    assert v.durable_anchor(dt_local) == v.durable_anchor(dt), "同一時刻不同時區應產生同一錨點"
    # 毫秒不可截掉：只差 1 毫秒也要是不同的錨點（截到整秒會讓撞號從 1 變 31）
    assert v.durable_anchor(dt) != v.durable_anchor(dt + timedelta(milliseconds=1))
    assert v.durable_anchor(None) == "", "沒有時間就沒有耐久錨點"
    # ⚠ naive datetime（沒有 tzinfo）必須被擋掉，不可以走 astimezone()——那會默默假設
    # **本機時區**，於是換一台機器就換一批錨點，正是這個函式存在要避開的失效模式。
    # 本 codebase 的 dt 全部來自 parse_ts（已正規化成 aware），實測 9166 輪 naive 0 個，
    # 但擋一行比日後靜默漂掉便宜。寧可沒有錨點，也不要給一個會隨機器漂的。
    assert v.durable_anchor(datetime(2026, 8, 1, 2, 52, 33, 123456)) == "",         "naive datetime 必須被拒絕，不可以套用本機時區"
    # ⚠ 這裡不再另外驗「開頭是 k、不含冒號」——那個變數上面幾行才被字面值釘死成
    # "k20260801T025233123Z"，再驗一次是**不可能紅**的恆真斷言。

    # --- 2. 撞號 tiebreak 與 dt is None（合成 session，強制走那兩條路）-------
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000da"
    same = "2026-07-23T15:14:11.497Z"          # 兩則 user 回合共用同一毫秒
    evs = [
        {"type": "user", "uuid": "d1", "parentUuid": None, "timestamp": same,
         "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "撞號第一則DUPFIRST。"}},
        {"type": "user", "uuid": "d2", "parentUuid": "d1", "timestamp": same,
         "sessionId": sid,
         "message": {"role": "user", "content": "撞號第二則DUPSECOND。"}},
        {"type": "assistant", "uuid": "d3", "parentUuid": "d2",
         "timestamp": "2026-07-23T15:14:20.250Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "md3",
                     "usage": {"input_tokens": 100, "output_tokens": 20},
                     "content": [{"type": "text", "text":
                         # ⚠ 這兩個假 id 是 `_page_dup_ids` 的守衛，**不要「順手清掉」**：
                         # 內文的 `<`/`>` 會被跳脫、雙引號不會，所以純文字掃描會把它們
                         # 算成重複的頁面 id。
                         '正常回覆NORMALTURN。<div id="IDSCANFAKE">x</div>'
                         '<span id="IDSCANFAKE">y</span>'}]}},
        # ⚠ 沒有 timestamp：`parse_ts(None)` → `_dt` 是 None → 該回合沒有耐久錨點
        {"type": "user", "uuid": "d4", "parentUuid": "d3", "sessionId": sid,
         "message": {"role": "user", "content": "沒有時間的一則NOTIME。"}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out), "--format", "both"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = [p for p in _session_pages(out)][0].read_text(encoding="utf-8")
    md = [p for p in (out / "sessions").rglob("*.md")][0].read_text(encoding="utf-8")

    base = "k20260723T151411497Z"
    # 撞號：**第一個不加後綴**，第二個才是 -2。
    assert f'id="{base}"' in html, f"撞號的第一則應拿到無後綴的 {base}"
    assert f'id="{base}-tb2"' in html, "撞號的第二則應拿到 -tb2 後綴"

    # ⚠⚠ **要斷言「誰」拿到哪一個，不能只斷言「有 base、有 -2」。**
    # 後者對任何指派順序都成立——實測：把排序整條刪掉、
    # 或改成 reverse=True，「有 base、有 -2」的斷言**全綠**。
    # 現在的 tiebreak 是來源檔行序（`src_i`），所以先出現在 JSONL 裡的那一則拿無後綴。
    def _turn_id_of(mark):
        """含 `mark` 的那個 .turn **開標籤上**的 id（沒有 id 屬性就回 ""）。

        ⚠ 只看開標籤，不能看整段：標頭裡有個 `<span class="tanchor" id="tN">`，
        看整段會把它的 id 當成回合的 id——那樣「沒有耐久錨點的回合不該有 id」
        這條就永遠驗不出來。
        ⚠ 也不能用 mark 的第一次出現：session 標題取自第一則使用者訊息，
        所以 mark 常常先出現在 `<title>` 裡，那之前沒有任何 .turn。
        """
        pos = -1
        while True:
            pos = html.find(mark, pos + 1)
            if pos < 0:
                raise AssertionError(f"{mark} 沒有出現在任何 .turn 區塊裡")
            j = html.rfind('<div class="turn', 0, pos)
            if j < 0:
                continue
            tag = html[j:html.index(">", j)]
            m = re.search(r'\sid="([^"]*)"', tag)
            return m.group(1) if m else ""

    assert _turn_id_of("DUPFIRST") == base, \
        f"來源檔行序在前的那一則要拿無後綴錨點，實得 {_turn_id_of('DUPFIRST')!r}"
    assert _turn_id_of("DUPSECOND") == f"{base}-tb2", \
        f"行序在後的那一則要拿 -tb2，實得 {_turn_id_of('DUPSECOND')!r}"

    # 耐久錨點必須掛在 .turn 本身（不是掛在標頭那個 opacity:0 的連結上）：
    # openSub 只對 .turn 展開內部折疊、也只在它身上加 .hl 外框，而那圈外框是
    # 「跳成功了」的唯一視覺訊號。掛錯地方，成功與失敗會長得一模一樣。
    assert re.search(r'<div class="turn user"[^>]*\sid="%s"' % re.escape(base), html), \
        "耐久錨點必須是 .turn 的 id"
    # t{n} 退成零尺寸 span，但**必須still在**：全文搜尋與 MD 的 {#tN} 都靠它
    assert '<span class="tanchor" id="t1">' in html, "t{n} 應保留成 tanchor span"
    assert '<span class="tanchor" id="t3">' in html, "t{n} 應逐輪都在"

    # 沒有時間的那一則：不給耐久錨點，也不給 # 連結（⚠ 不要退回 t{n} 充數）
    assert "NOTIME" in html, "沒有時間的回合仍應正常呈現"
    # ⚠ 只驗 `not startswith("k")` 擋不住「退回 t{n}」這個明文禁止的作法
    # ——把 kanchor 設成 anchor 的突變下，那條照樣綠。
    # 要驗的是：**那個 .turn 根本沒有 id 屬性**。
    assert _turn_id_of("NOTIME") == "", \
        f"沒有時間的回合的 .turn 不該有 id（更不可以退回 t{{n}}），實得 {_turn_id_of('NOTIME')!r}"

    # ⚠ **最高不變量：同一頁的 id 必須唯一。** 重複的話 getElementById 會靜默取第一個。
    dup = _page_dup_ids(html)
    assert not dup, f"同一頁出現重複的 id：{dup}"

    # # 直達連結：指向耐久錨點、title 帶本地時間、走 openSub
    assert f'href="#{base}"' in html, "# 連結應指向耐久錨點"
    assert 'class="alink"' in html and "這一輪的直達連結" in html, "# 連結與 title 應存在"

    # ⚠ 下面這幾條**只是煙霧檢查**（那幾段 JS 有沒有被整段刪掉），
    # **不是行為測試**：對產生出來的 JS 做字串比對，只有改名字會紅、改行為不會紅
    # 。真正驗行為的是 `node scripts/probe_anchor_js.js <某頁>.html`
    # ——那支用假 DOM 實際跑這些函式，涵蓋退化、橫幅、上提、同分頁導覽與關閉鈕共 13 組。
    # ⚠ `tests/` 會投影到公開 dist repo，所以不在這裡引入 node 依賴。
    for _frag, _why in (("function bmMiss(", "找不到錨點時的橫幅"),
                        ("bm-miss", "橫幅樣式"),
                        ("addEventListener('hashchange'", "同分頁導覽的進入點"),
                        ("classList.contains('tanchor')", "tanchor 上提")):
        assert _frag in html, f"{_why}那段 JS/CSS 不見了（行為請用 scripts/probe_anchor_js.js 驗）"

    # MD 側：兩個錨點並存，且 {#tN} 在**同一行的前面**（_TURN_HEAD_RE 靠它切回合）
    # ⚠ 只驗 `md.index("{#t1}") < md.index("{#k…}")` 的話，驗的是**行序不是行內序**：
    # 兩個標記分屬不同行時照樣成立，就算 render_turn_md 改成
    # 先印 k 錨點（那會直接打死 _TURN_HEAD_RE）也不會紅。要驗就驗同一行上的相對位置。
    assert "{#t1}" in md and f"{{#{base}}}" in md, "MD 應同時帶兩個錨點"
    both = [ln for ln in md.splitlines() if "{#t" in ln and f"{{#{base}}}" in ln]
    assert both, f"應有一行同時帶 {{#tN}} 與 {{#{base}}}"
    for ln in both:
        assert ln.index("{#t") < ln.index(f"{{#{base}}}"), \
            f"同一行上 {{#tN}} 必須在耐久錨點之前（_TURN_HEAD_RE 靠它）：{ln!r}"

    # 全文搜尋沒有被新錨點打壞
    r2 = subprocess.run(
        [sys.executable, str(SCRIPT), "--search", "NORMALTURN", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r2.returncode == 0, f"搜尋非零退出\nSTDOUT:{r2.stdout}\nSTDERR:{r2.stderr}"
    spages = sorted((out / "search").glob("*.html"))
    assert spages, "搜尋應產生結果頁"
    spage = spages[-1].read_text(encoding="utf-8")
    assert "<mark>" in spage, "加了耐久錨點之後，搜尋結果頁仍要切得出回合並高亮"
    print("OK: durable anchor test passed")


def test_anchor_tiebreak_main_vs_side(tmp_path=None):
    """主回合與**子代理回合**撞同一毫秒時，誰拿到無後綴的耐久錨點。

    ⚠⚠ **這條保證只能講到測試守得住的範圍**：註解宣稱了某個保證、卻沒有任何測試在守它，
    這條保證就會被寫錯。
    這條測試存在的意義就是：**寫錯時，它會紅。**

    為什麼是「主回合該贏」：子代理的轉錄檔是 `<sid>/**/*.jsonl`，它在主回合**之後**才落地
    （子代理是被主對話的 Task 呼叫起來的）。所以真實會發生的情境是
    「主回合原本獨佔某毫秒、拿了無後綴錨點、使用者存成書籤 → 子代理檔之後才出現」。
    ⚠ 若讓子代理排在前面，那個既有書籤就會**安靜地指到子代理那一輪**
    （有 `.hl`、會改寫網址列、不出橫幅）＝「跳到了，但跳到錯的地方」。

    ⚠ **反方向不保證**（子代理先、主回合後出現）——那是已登記的殘餘風險
    `SCOPE-BOOKMARK-TIEBREAK-INSERT`，不是這條測試的守備範圍。
    """
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000tb"
    hit = "2026-08-05T03:00:00.500Z"          # 主回合與子代理回合共用這一毫秒

    # 主檔：前面墊幾則，讓那個主回合的行序**大於 0**（子代理檔的行序從 0 起算）
    main_evs = [
        {"type": "user", "uuid": "m1", "parentUuid": None, "timestamp": "2026-08-05T02:00:00.000Z",
         "cwd": "/x/Proj", "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "墊第一則。"}},
        {"type": "assistant", "uuid": "m2", "parentUuid": "m1", "timestamp": "2026-08-05T02:00:05.000Z",
         "sessionId": sid, "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mm2",
                                       "usage": {"input_tokens": 50, "output_tokens": 10},
                                       "content": [{"type": "text", "text": "墊第二則。"}]}},
        {"type": "user", "uuid": "m3", "parentUuid": "m2", "timestamp": "2026-08-05T02:00:10.000Z",
         "sessionId": sid, "message": {"role": "user", "content": "墊第三則。"}},
        {"type": "user", "uuid": "m4", "parentUuid": "m3", "timestamp": hit,
         "sessionId": sid, "message": {"role": "user", "content": "主回合MAINCOLLIDE。"}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in main_evs), encoding="utf-8")

    # 子代理轉錄：<sid>/**/*.jsonl，**第 0 行**就撞同一毫秒
    side_dir = proj / sid
    side_dir.mkdir(parents=True, exist_ok=True)
    side_evs = [
        {"type": "user", "uuid": "s1", "parentUuid": None, "timestamp": hit,
         "sessionId": sid, "message": {"role": "user", "content": "子代理SIDECOLLIDE。"}},
        {"type": "assistant", "uuid": "s2", "parentUuid": "s1", "timestamp": "2026-08-05T03:00:02.000Z",
         "sessionId": sid, "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "ms2",
                                       "usage": {"input_tokens": 30, "output_tokens": 5},
                                       "content": [{"type": "text", "text": "子代理回覆。"}]}},
    ]
    (side_dir / "agent1.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in side_evs), encoding="utf-8")

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = [p for p in _session_pages(out)][0].read_text(encoding="utf-8")

    def _turn_tag_id(mark):
        pos = -1
        while True:
            pos = html.find(mark, pos + 1)
            if pos < 0:
                raise AssertionError(f"{mark} 沒有出現在任何 .turn 區塊裡")
            j = html.rfind('<div class="turn', 0, pos)
            if j < 0:
                continue
            m = re.search(r'\sid="([^"]*)"', html[j:html.index(">", j)])
            return m.group(1) if m else ""

    base = "k20260805T030000500Z"
    got_main = _turn_tag_id("MAINCOLLIDE")
    got_side = _turn_tag_id("SIDECOLLIDE")
    # 前置：兩者真的撞在同一毫秒（fixture 沒寫壞）
    assert got_main.startswith(base) and got_side.startswith(base), \
        f"fixture 沒撞號：main={got_main!r} side={got_side!r}"
    assert got_main == base, \
        (f"主回合要拿無後綴的 {base}，實得 {got_main!r}——"
         "子代理檔之後才落地時，這會讓既有書籤安靜指到子代理那一輪")
    assert got_side == f"{base}-tb2", f"子代理回合要拿 -tb2，實得 {got_side!r}"

    # 同一頁 id 唯一（撞號處理不可產生重複）
    dup = _page_dup_ids(html)
    assert not dup, f"同一頁出現重複的 id：{dup}"
    print("OK: anchor tiebreak (main vs side) test passed")


def test_anchor_tiebreak_sort_is_load_bearing(tmp_path=None):
    """撞號組的**排序本身**是否承重：把它拿掉、或把鍵廢成常數，這條必須紅。

    ⚠⚠ **為什麼要有第二條 tiebreak 測試**：
    前兩條（`test_durable_anchor` 的撞號段、`test_anchor_tiebreak_main_vs_side`）用的素材，
    **list 位置順序剛好等於 `(src_f, src_i)` 順序**——前者是連續兩則主對話 user 事件，
    後者是「主檔 vs 子代理檔」而 `s.main_groups + s.side_groups` 本來就主在前。
    於是「有排序」與「完全不排序」輸出一樣，實測下列突變**全綠**：
    刪掉 `_grp.sort(...)`／`_tiebreak_key` 恆回 `0`／恆回 `-1`／換回上一版被否決的
    `(bool(side), role)`。兩條測試都只對**反序**敏感，對**沒有排序**不敏感。

    **這條的素材刻意讓兩者相反**：兩個子代理轉錄檔撞同一毫秒，
    `by_parent` 的插入序（＝各自**最早**事件的時間序）與**檔名序**相反——
    `z_agent.jsonl` 開場早所以 list 位置在前，但 `a_agent.jsonl` 檔名排前所以排序後該由它拿
    無後綴錨點。**不排序就會換人**，上述五個突變因此全部會紅（實測，2026-08-22）。

    ⚠ 這裡不重複驗「主 vs 子」那條保證，那是
    `test_anchor_tiebreak_main_vs_side` 的守備範圍；本條只驗**排序有沒有在做事**。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000sf"
    hit = "2026-08-05T03:00:00.500Z"          # 兩個子代理回合共用這一毫秒

    # 主檔只墊一則（session 要有個標題與起訖時間）
    (proj / f"{sid}.jsonl").write_text(json.dumps(
        {"type": "user", "uuid": "m1", "parentUuid": None,
         "timestamp": "2026-08-05T01:00:00.000Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "主檔墊一則MAINPAD。"}},
        ensure_ascii=False), encoding="utf-8")

    side_dir = proj / sid
    side_dir.mkdir(parents=True, exist_ok=True)

    def _agent(fname, tool_use_id, first_ts, mark):
        """一個子代理轉錄檔：開場一則（時間決定 `by_parent` 插入序）＋撞號一則。

        ⚠ `.meta.json` 的 `toolUseId` 不可省：沒有它兩個檔會併進同一個 `by_parent` 桶
        （key 都是 `""`），side 事件本來就依時間排過，撞號時穩定排序退回**檔案載入序**
        ＝檔名序，於是又變成「不排序也對」，這條測試就白寫了。
        """
        evs = [
            {"type": "user", "uuid": fname + "u0", "parentUuid": None, "timestamp": first_ts,
             "sessionId": sid, "message": {"role": "user", "content": f"{mark}開場。"}},
            {"type": "user", "uuid": fname + "u1", "parentUuid": fname + "u0", "timestamp": hit,
             "sessionId": sid, "message": {"role": "user", "content": f"{mark}撞號。"}},
        ]
        (side_dir / f"{fname}.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
        (side_dir / f"{fname}.meta.json").write_text(
            json.dumps({"toolUseId": tool_use_id, "agentType": "x", "description": mark}),
            encoding="utf-8")

    # ⚠ 這兩行的**時間**與**檔名**刻意相反，別「順手」調成一致——調了這條就失去鑑別力。
    _agent("a_agent", "tid_a", "2026-08-05T02:30:00.000Z", "AAGENT")   # 檔名前、開場晚
    _agent("z_agent", "tid_z", "2026-08-05T02:00:00.000Z", "ZAGENT")   # 檔名後、開場早

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    html = [p for p in _session_pages(out)][0].read_text(encoding="utf-8")

    def _turn_tag_id(mark):
        pos = -1
        while True:
            pos = html.find(mark, pos + 1)
            if pos < 0:
                raise AssertionError(f"{mark} 沒有出現在任何 .turn 區塊裡")
            j = html.rfind('<div class="turn', 0, pos)
            if j < 0:
                continue
            m = re.search(r'\sid="([^"]*)"', html[j:html.index(">", j)])
            return m.group(1) if m else ""

    base = "k20260805T030000500Z"
    got_a = _turn_tag_id("AAGENT撞號")
    got_z = _turn_tag_id("ZAGENT撞號")
    # 前置：fixture 真的撞號（改壞了要在這裡就講清楚，不要讓下面兩條的訊息誤導人）
    assert got_a.startswith(base) and got_z.startswith(base),         f"fixture 沒撞號：a={got_a!r} z={got_z!r}"
    # ⚠ list 位置是 z 在前（開場早）。排序若沒在做事，拿到無後綴的就會是 z。
    assert got_a == base,         (f"檔名序在前的 a_agent 要拿無後綴的 {base}，實得 {got_a!r}——"
         "撞號組的排序沒在做事（被刪掉／鍵成了常數），錨點退回 list 位置")
    assert got_z == f"{base}-tb2", f"檔名序在後的 z_agent 要拿 -tb2，實得 {got_z!r}"
    dup = _page_dup_ids(html)
    assert not dup, f"同一頁出現重複的 id：{dup}"
    print("OK: anchor tiebreak sort-is-load-bearing test passed")


def test_bookmark_ui(tmp_path=None):
    """書籤（第 2 期）在**產出的頁面上**的形狀，以及內嵌資料的跳脫。

    ⚠ **這一支只驗形狀，不驗行為。** 真正的行為（存／讀／匯出遮蔽／匯入合併／⏰ 篩選）
    要用 `python scripts/probe_bookmarks.py --page <某頁>.html` ——那支跑**真 Chrome**，
    因為書籤這塊踩滿了假 DOM 造不出來的東西（`innerHTML`、`localStorage`、`Blob`、
    `FileReader`）。⚠ `tests/` 會投影到公開 dist repo，所以這裡不引入 node／Chrome 依賴。

    ⚠⚠ 這裡最重要的一格是 **`</script>` 破出**。標題要內嵌進 `<script>` 當 `BK_TITLE`，
    不跳脫 `<` 的話一句 `</script>` 就能把整段 JS 截斷——底下所有書籤功能連同錨點跳轉
    一起死掉，而且**畫面上完全看不出來**。守它的是 `js_embed()`。

    ⚠ **素材必須用 `/rename`，不能用第一則使用者訊息。** 自動標題那條路
    `first_user_text()` 有一行 `re.sub(r"<[^>]+>", "", txt)` 會先把標籤剝掉，
    拿它當素材的話 `js_embed()` 根本沒被執行到——**測試會綠，但綠得毫無意義**。
    `extract_rename()` **不剝標籤**，那才是真正沒有上游防護的路徑。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000bk"
    hostile = '破出測試</script><script>window.PWNED=1;</script>'
    evs = [
        {"type": "user", "uuid": "b1", "parentUuid": None,
         "timestamp": "2026-08-09T04:05:06.700Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "第一則BKFIRST。"}},
        # ⚠ /rename 的名稱**不經過剝標籤**（`extract_rename` 只 strip 引號與空白），
        #   所以它是真正會把敵意字串送進 <script> 的那條路。
        {"type": "system", "subtype": "local_command", "uuid": "b0r", "parentUuid": "b1",
         "timestamp": "2026-08-09T04:05:07.000Z", "sessionId": sid,
         "content": f'Session renamed to: {hostile}'},
        {"type": "assistant", "uuid": "b2", "parentUuid": "b1",
         "timestamp": "2026-08-09T04:05:10.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mb2",
                     "usage": {"input_tokens": 80, "output_tokens": 12},
                     "content": [{"type": "text", "text": "回覆BKSECOND。"}]}},
        # ⚠ 沒有 timestamp ⇒ 沒有耐久錨點 ⇒ **不該有加書籤鈕**（給了也存不回來）
        {"type": "user", "uuid": "b3", "parentUuid": "b2", "sessionId": sid,
         "message": {"role": "user", "content": "沒有時間的一則BKNOTIME。"}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    page = [p for p in _session_pages(out)][0]
    html = page.read_text(encoding="utf-8")

    # --- 1. `</script>` 不可以破出 -----------------------------------------
    # 內嵌處必須是跳脫過的 \u003c/script\u003e，而不是真的 `</script>`。
    m = re.search(r"BK_TITLE=(.*?), BK_TOWN=", html, re.S)
    assert m, "頁面裡找不到 BK_TITLE"
    assert "</script>" not in m.group(1), \
        f"標題把 </script> 原樣寫進 <script> 了（js_embed 沒生效）：{m.group(1)[:120]}"
    assert "\\u003c" in m.group(1), f"BK_TITLE 應把 < 跳成 \\u003c，實得 {m.group(1)[:120]}"
    # 前置：素材真的把敵意字串送到了 BK_TITLE（否則下面兩條在守空氣）
    assert "PWNED" in m.group(1),         f"素材沒送進 BK_TITLE，這一格在守空氣——實得 {m.group(1)[:120]}"
    # ⚠ 不能斷言「整頁沒有 window.PWNED」——標題也會被渲染進 <h1>／<title>，
    #   那裡是 HTML-escape 過的**文字**（`&lt;/script&gt;`），出現是正常的。
    #   要驗的是那串**未跳脫**的序列在整頁任何地方都不存在。
    assert "</script><script>window.PWNED" not in html,         "敵意序列以未跳脫的原樣出現在頁面上（＝真的破出了）"
    # 整份頁面的 <script> 開闔要成對——破出會多出一個關閉標籤
    assert html.count("<script>") == html.count("</script>"), \
        f"<script> 開闔不成對：{html.count('<script>')} vs {html.count('</script>')}"

    # --- 2. 每輪標頭的加書籤鈕 ---------------------------------------------
    def _head_of(mark):
        pos = html.index(mark)
        j = html.rfind('<div class="turn', 0, pos)
        assert j >= 0, f"{mark} 不在任何 .turn 裡"
        return html[j:html.index('<div class="body"', j)]

    for mark in ("BKFIRST", "BKSECOND"):
        head = _head_of(mark)
        assert 'class="bmk"' in head, f"{mark} 這一輪的標頭應有加書籤鈕"
        assert "data-k=\"k" in head, f"{mark} 的加書籤鈕要帶 data-k（耐久錨點）"
        assert "bkOpen(this.getAttribute(" in head, f"{mark} 的加書籤鈕要呼叫 bkOpen"
    # ⚠ 沒有耐久錨點的那一輪**不可以**有加書籤鈕：身分是 sid+錨點，沒錨點就存不回來。
    #   給了一顆按不出結果的鈕，比沒有鈕更糟。
    assert 'class="bmk"' not in _head_of("BKNOTIME"), \
        "沒有耐久錨點的回合不該有加書籤鈕（存了也找不回來）"
    assert 'class="alink"' not in _head_of("BKNOTIME"), "同上：# 連結也不該有（既有規則）"

    # --- 3. 對話窗骨架與內嵌身分 -------------------------------------------
    for frag, why in (
        ('<div class="bm-modal" id="bkModal"', "書籤對話窗外層"),
        ('id="bkCard"', "對話窗內容容器（內容由 JS 填，建置期不可能知道書籤）"),
        ('id="bkbtn"', "頂列的書籤鈕"),
        ("var BK_KEY='asv_bm_v1'", "localStorage 的鍵"),
        ("function bkExport(", "匯出（**必須和存檔鈕同一批出貨**）"),
        ("function bkImport(", "匯入"),
        ("function bkAddMonths(", "複查日期（月份不可溢位）"),
        (".bmk{", "加書籤鈕的樣式"),
        (".bm-chip{", "單選 chip 的樣式（⚠ 不是下拉）"),
    ):
        assert frag in html, f"缺少{why}：`{frag}`"
    assert f'var BK_SID="{sid}"' in html, "BK_SID 應填成這一場的 session id"
    assert "var BK_FALLBACK='6m'" in html, "提醒複查的預設值應為半年"
    # ⚠ 書籤記錄裡的 url 是**相對 out/ 的**：檔名含本地時間＋專案名，絕對路徑一旦寫進
    #   匯出的 JSON 就會帶著 `C:\\Users\\<名字>\\` 出門。
    mu = re.search(r'var BK_SID=.*?, BK_URL="([^"]*)"', html)
    assert mu, "頁面裡找不到 BK_URL"
    assert mu.group(1).startswith("sessions/"), f"BK_URL 應相對 out/，實得 {mu.group(1)!r}"
    assert ":" not in mu.group(1) and not mu.group(1).startswith("/"), \
        f"BK_URL 不可以是絕對路徑，實得 {mu.group(1)!r}"

    # --- 3b. 索引頁的書籤圖示 ------------------------
    # ⚠ 一樣**只驗形狀**。行為（有書籤才亮、⏰、篩選、不可注入）用
    #   `python scripts/probe_bookmarks.py --index out/index.html`（真 Chrome，6 組 17 項）。
    # ⚠ 這一段**不需要升 RENDERER_VERSION**：index.html 每次執行都無條件重產，
    #   不受 manifest 版本閘管（那個閘只管 session 頁）。
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert f'data-sid="{sid}"' in idx, "索引列要帶 session id（圖示唯一的依據）"
    assert 'data-bm="0"' in idx, "索引列要有 data-bm 供「只看有書籤的」篩選用"
    for frag, why in (
        ("function bkIndexMark(", "索引頁的書籤標記函式"),
        ('id="fb"', "「只看有書籤的」勾選框"),
        ('id="fbwrap"', "那顆勾選框的外層（一場書籤都沒有時要藏起來）"),
        (".bmflag{", "圖示的樣式"),
        ("r.dataset.bm==='1'", "篩選要接進既有的 af()，不可以另做一套顯示邏輯"),
    ):
        assert frag in idx, f"索引頁缺少{why}：`{frag}`"
    assert _page_dup_ids(idx) == [], f"索引頁出現重複的 id：{_page_dup_ids(idx)}"

    # --- 4. 不可以打壞第 0＋1 期既有的東西（回歸）--------------------------
    assert '<span class="tanchor" id="t1">' in html, "t{n} 錨點仍要在（全文搜尋靠它）"
    assert 'class="alink"' in html and "這一輪的直達連結" in html, "# 直達連結仍要在"
    dup = _page_dup_ids(html)
    assert not dup, f"同一頁出現重複的 id：{dup}"
    print("OK: bookmark UI (phase 2) shape test passed")

def test_bookmark_block_anchors(tmp_path=None):
    """書籤第 4 期：**區塊層級錨點**的文法、序號作用域，以及每個可標記區塊的 ☆。

    錨點文法（`bookmarks-proposal.md`〈現在該預留什麼〉定死的那一條）：

        k<回合時戳>[-tb<n>]              ← 回合（第 0＋1 期，已出貨）
        k<回合時戳>[-tb<n>]-s<步epoch>-b<n>  ← 屬於某一步的區塊，n **以步為作用域**
        k<回合時戳>[-tb<n>]-b<n>            ← 落在第一個 `_step` 之前的區塊，n 以回合為作用域

    ⚠⚠ **為什麼 n 要以「步」為作用域，而不是整輪**：全語料實測（`probe_turn_identity.py
    blocks`）一輪的可標記區塊數 Claude p99=101／max=286、Codex p99=129／max=412，
    **12.6%／34.4% 的回合超過 20 個區塊**——用輪內序號的話，「爆炸半徑關在一輪之內」
    這個理由就名存實亡（一輪就是整段對話）。同一份實測另外量到：`_step.t` 缺漏
    **0/67876**、同輪內步時戳撞號 **0** ⇒ 用步時戳當作用域是免費的。
    改用步作用域後，一步底下的區塊數 p90 只有 2〜4。

    ⚠ **`_step` 沒有時戳時，那一步的區塊一律不給錨點**（實測 0 筆，但那條路要能走）。
    理由與 `durable_anchor()` 拒絕 naive datetime 同一條：**寧可沒有錨點，也不要給一個
    會撞號的**——若退回輪內序號，就會和「第一個 `_step` 之前」那些區塊撞在同一個
    `-b<n>` 命名空間裡，然後安靜指錯。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000b4"
    # 一則 user（沒有 `_step` ⇒ 走輪內序號那條路）＋ 一個含**兩步**的 assistant 回合。
    # ⚠ 兩步必須是不同的 `message.id`：`group_turns(per_step=True)` 是按 id 起新的一步。
    evs = [
        {"type": "user", "uuid": "p1", "parentUuid": None,
         "timestamp": "2026-08-09T04:05:06.700Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "使用者這一則P4USER。"}},
        # 第一步：文字 ＋ 工具呼叫（兩個可標記區塊）
        {"type": "assistant", "uuid": "p2", "parentUuid": "p1",
         "timestamp": "2026-08-09T04:05:10.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mstepA",
                     "usage": {"input_tokens": 100, "output_tokens": 20},
                     "content": [{"type": "text", "text": "第一步的說明P4TXTA。"},
                                 {"type": "tool_use", "id": "tu_a", "name": "Bash",
                                  "input": {"command": "echo P4TOOLA"}}]}},
        # 第二步：又一段文字 ＋ 又一個工具呼叫
        {"type": "assistant", "uuid": "p3", "parentUuid": "p2",
         "timestamp": "2026-08-09T04:05:40.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mstepB",
                     "usage": {"input_tokens": 120, "output_tokens": 25},
                     "content": [{"type": "text", "text": "第二步的說明P4TXTB。"},
                                 {"type": "tool_use", "id": "tu_b", "name": "Read",
                                  "input": {"file_path": "/x/P4TOOLB.py"}}]}},
        # ⚠ 沒有 timestamp ⇒ 整輪沒有耐久錨點 ⇒ **裡面的區塊也不該有 id 或 ☆**
        {"type": "user", "uuid": "p4", "parentUuid": "p3", "sessionId": sid,
         "message": {"role": "user", "content": "沒有時間的一則P4NOTIME。"}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    page = [p for p in _session_pages(out)][0]
    html = page.read_text(encoding="utf-8")

    # ⚠⚠ **不可以用第一次或最後一次出現。** session 標題取自第一則使用者訊息，於是
    # 「P4USER」會出現三次：`<title>`／`<h1>`（在所有 `.turn` **之前**）、對話本體，
    # 以及頁尾 `<script>` 裡的 `BK_TITLE`（在所有 `.turn` **之後**）。
    # 用 `rindex` 撈到的是頁尾那一個，往前找到的會是**整頁最後一個** `.blk`
    # ——斷言照跑，只是驗錯了對象。
    _body = html.index('<div class="turn')

    def _blk_of(mark):
        """含 `mark` 的那個 `.blk` 包裝元素的**開標籤**（找不到就回 ""）。

        ⚠ 只回開標籤：包裝裡面還有巢狀內容（子代理、工具結果），看整段會把裡層的
        id 誤認成這一塊的 id。
        """
        pos = html.index(mark, _body)
        j = html.rfind('<div class="blk"', 0, pos)
        if j < 0:
            return ""
        return html[j:html.index(">", j) + 1]

    # --- 1. 步作用域：每一步的第一個區塊都是 -b1，不是接著上一步往下數 -------
    a_txt, a_tool = _blk_of("P4TXTA"), _blk_of("P4TOOLA")
    b_txt, b_tool = _blk_of("P4TXTB"), _blk_of("P4TOOLB")
    for name, tag in (("P4TXTA", a_txt), ("P4TOOLA", a_tool),
                      ("P4TXTB", b_txt), ("P4TOOLB", b_tool)):
        assert tag, f"{name} 沒有被 .blk 包起來"
        assert ' id="k' in tag, f"{name} 的 .blk 沒有耐久錨點：{tag}"

    ids = {}
    for name, tag in (("P4TXTA", a_txt), ("P4TOOLA", a_tool),
                      ("P4TXTB", b_txt), ("P4TOOLB", b_tool)):
        m = re.search(r' id="([^"]+)"', tag)
        assert m, f"{name} 抓不到 id：{tag}"
        ids[name] = m.group(1)

    # 文法：兩步的區塊都要帶 `-s<epoch>-b<n>`
    for name in ("P4TXTA", "P4TOOLA", "P4TXTB", "P4TOOLB"):
        assert re.match(r"^k\d{8}T\d{9}Z-s\d+-b\d+$", ids[name]), \
            f"{name} 的錨點文法不對：{ids[name]}"

    # ⚠⚠ **這一格才是「步作用域」的承重斷言。** 只驗「有 -s 有 -b」的話，
    # 把序號改成整輪連號（b1..b4）照樣全綠——那正是這一期要換掉的行為。
    assert ids["P4TXTA"].endswith("-b1"), f"第一步的第一個區塊應是 -b1：{ids['P4TXTA']}"
    assert ids["P4TOOLA"].endswith("-b2"), f"第一步的第二個區塊應是 -b2：{ids['P4TOOLA']}"
    assert ids["P4TXTB"].endswith("-b1"), \
        f"⚠ 第二步的第一個區塊必須重新從 -b1 起算（序號作用域＝步，不是整輪）：{ids['P4TXTB']}"
    assert ids["P4TOOLB"].endswith("-b2"), f"第二步的第二個區塊應是 -b2：{ids['P4TOOLB']}"

    # 兩步的 `-s<epoch>` 必須不同，否則作用域根本沒分開（四個 id 會撞成兩對）
    sa = ids["P4TXTA"].split("-s")[1].split("-b")[0]
    sb = ids["P4TXTB"].split("-s")[1].split("-b")[0]
    assert sa != sb, f"兩步的步時戳相同（{sa}），序號作用域沒有真的分開"
    assert len(set(ids.values())) == 4, f"四個區塊的錨點不是互異的：{ids}"

    # --- 2. 回合作用域：user 回合沒有 `_step`，走 `-b<n>`（不帶 -s）---------
    u = _blk_of("P4USER")
    assert u, "使用者那一則的文字區塊沒有被 .blk 包起來"
    mu = re.search(r' id="([^"]+)"', u)
    assert mu, f"使用者區塊抓不到 id：{u}"
    assert re.match(r"^k\d{8}T\d{9}Z-b\d+$", mu.group(1)), \
        f"沒有步的回合，其區塊應走輪內序號（不帶 -s）：{mu.group(1)}"

    # --- 3. 每個可標記區塊都要有自己的 ☆（文字段落也要）--
    def _blk_full(mark):
        pos = html.index(mark, _body)
        j = html.rfind('<div class="blk"', 0, pos)
        assert j >= 0, f"{mark} 不在任何 .blk 裡"
        return html[j:pos]

    for mark in ("P4TXTA", "P4TOOLA", "P4TXTB", "P4TOOLB", "P4USER"):
        seg = _blk_full(mark)
        assert 'class="bmk blk-ctl"' in seg, f"{mark} 這一塊少了加書籤鈕"
        assert 'class="alink blk-ctl"' in seg, f"{mark} 這一塊少了 # 直達連結"

    # --- 4. 沒有耐久錨點的回合，區塊也不給（給了也存不回來）-----------------
    pos = html.rindex("P4NOTIME")
    j = html.rfind('<div class="turn', 0, pos)
    assert j >= 0, "P4NOTIME 不在任何 .turn 裡"
    seg = html[j:pos]
    assert '<div class="blk"' not in seg, \
        "沒有耐久錨點的回合不該有 .blk 包裝（那一塊的書籤存不回來）"
    assert 'class="bmk' not in seg, "沒有耐久錨點的回合不該有任何加書籤鈕"

    # --- 5. 步本身也要有錨點（退化階梯的中間那一階）------------------------
    # 區塊找不到 → 退到 `k…-s<epoch>` → 再退到 `k…`。沒有中間這階的話，
    # 一個 286 個區塊的回合裡，任何一次區塊漂移都會直接彈回整輪的最上面。
    # ⚠ 要驗**完整的那一個 id**。寫成 `'id="k' in html and f'-s{ep}"' in html` 的話，
    # 前半對任何頁面都成立、後半也被區塊自己的 `-s…-b1` 滿足 ⇒ 就算完全不掛步錨點也全綠。
    for name in ("P4TXTA", "P4TXTB"):
        step_id = ids[name].split("-b")[0]          # k…-s<epoch>
        assert f'<div class="step-sep" id="{step_id}">' in html, \
            f"步 {step_id} 的分隔列沒有掛上錨點（退化階梯少了中間一階）"
    assert html.count('<div class="step-sep" id="k') == 2, \
        "兩步就該有兩個掛了錨點的步驟列"

    # --- 6. 整頁 id 不可重複（區塊 id 一多，撞號就從理論變實務）------------
    dup = _page_dup_ids(html)
    assert not dup, f"頁面上有重複的 id：{sorted(dup)[:8]}"

    # --- 7. `block_anchor()` 的四條契約（**直接對純函式下斷言**）------------
    # ⚠⚠ **為什麼不走整條管線**：第三條（步有、時戳沒有）在真實與合成語料上都造不出來
    # ——沒有 timestamp 的 assistant 事件會排到整個檔案的最前面，因而**自成一輪**、
    # 那一輪連 kanchor 都沒有，走的是「整輪沒錨點」那條既有的路（§4 已經在守）。
    # 走整條管線的話，M2／M9 兩個突變會**全部漏掉**：斷言看起來很像在守這件事，
    # 其實素材根本到不了那個狀態。
    # ⚠ 這裡**不是**把它降級成「沒有素材所以不驗」——契約照驗，
    # 只是驗在唯一到得了那個狀態的地方：函式本身。
    import ai_session_viewer as v4
    assert v4.block_anchor("kX", None, 3, False) == "kX-b3", \
        "第一個 `_step` 之前的區塊：序號以回合為作用域，不帶 -s"
    assert v4.block_anchor("kX", 1786248310, 2, True) == "kX-s1786248310-b2", \
        "步裡的區塊：帶 -s<epoch>，序號以步為作用域"
    assert v4.block_anchor("kX", None, 2, True) == "", \
        ("步裡的區塊但那一步沒有時戳 ⇒ **什麼都不給**。退回 `kX-b2` 會和"
         "「第一個 `_step` 之前」那些區塊撞進同一個 -b<n> 命名空間，然後安靜指錯")
    assert v4.block_anchor("", 1786248310, 1, True) == "", \
        "整輪沒有耐久錨點時，區塊也不給（給了也存不回來）"
    # 第五條：**負的步 epoch 一律不給錨點**。
    # ⚠⚠ 理由不是「1970 年前不會發生」，是**分隔符撞上字母表**：`-` 就是段落分隔符，
    # 所以 `kX-s-500-b1` 對**每一個**用 `-` 切字串的消費者都是畸形——
    # `bmGrammarOk()` 判它不合法（於是合法的錨點被宣告未命中），而 `openSub()` 的
    # `lastIndexOf('-')` 退化階梯也會把它切在錯的地方。
    # ⇒ 修的是**產出端**，不是驗證端：讓文法接受帶號數字，等於要求下游每一個
    # 消費者都自己處理負號，那是把一個洞換成三個。
    # 這和上面第三條是同一條規則：**寧可沒有錨點，也不要給一個會指錯的。**
    assert v4.block_anchor("kX", -500, 1, True) == "", \
        "負的步 epoch ⇒ 什麼都不給（`-` 是分隔符，帶號數字會讓每個消費者都切錯）"
    assert v4.block_anchor("kX", 0, 1, True) == "kX-s0-b1", \
        "⚠ 對照組：0 是合法的（別把「負的不給」寫成「非正的不給」）"

    # --- 7b. ⚠⚠ **第二個產出端**：步驟列的 id 也不可以吐出帶號的 `-s`---
    # 上面那幾條只對 `block_anchor()` 這個純函式下斷言，而 `-s<ep>` 有**兩個**產出端：
    # 區塊錨點與**步驟列的 id**。只修前者的話 ⇒ 同一個 group 裡區塊錨點正確消失，
    # 步驟列卻照樣輸出 `k…-s-900`——那正是 `bmGrammarOk()` 會拒絕的形狀。
    # ⚠ **這一格必須走 renderer**，不可以對純函式下斷言：純函式那一層永遠看不到
    # 第二個產出端，而「同一條規則寫在兩個地方」本來就是兩邊容易分岔的形狀。
    neg_group = {
        "kanchor": "k19691231T235959100Z",
        "role": "assistant",
        "n_steps": 2,
        "blocks": [
            {"type": "_step", "tms": -900, "t": -1, "idx": 1},
            {"type": "text", "text": "第一步的文字"},
            {"type": "_step", "tms": -500, "t": -1, "idx": 2},
            {"type": "text", "text": "第二步的文字"},
        ],
    }
    neg_html = v4.render_turn_html(neg_group, {}, set())
    signed = re.findall(r'id="(k[^"]*-s-[^"]*)"', neg_html)
    assert not signed, \
        f"步驟列吐出了帶號的 -s（bmGrammarOk 會拒絕這種字串，等於安靜壞掉）：{signed[:4]}"
    # ⚠ 對照組：正的照樣要給，否則「整個不給」也會讓上面那格變綠
    pos_group = dict(neg_group, kanchor="k20260801T000000000Z", blocks=[
        {"type": "_step", "tms": 1786248310000, "t": 1786248310, "idx": 1},
        {"type": "text", "text": "第一步的文字"},
        {"type": "_step", "tms": 1786248311000, "t": 1786248311, "idx": 2},
        {"type": "text", "text": "第二步的文字"},
    ])
    pos_html = v4.render_turn_html(pos_group, {}, set())
    assert re.search(r'id="k20260801T000000000Z-s1786248310000"', pos_html), \
        "⚠ 對照組：正的步 epoch 仍必須畫出步驟列的錨點（別把「負的不給」做成「都不給」）"

    # --- 8. 顯形範圍必須縮到 .blk（否則滑過一輪會亮起上百組按鈕）-----------
    # ⚠⚠ **這一格只守「規則寫對了」，不守「滑鼠滑過去真的只亮一組」。**
    # headless 造不出 `:hover`，所以真正的視覺行為靠人眼；但少了 `:not(.blk-ctl)`
    # 這個機制就一定壞，所以它值得一格會紅的。
    for sel in ('.turn:hover .alink:not(.blk-ctl)', '.turn:hover .bmk:not(.blk-ctl)'):
        assert sel in html, \
            f"少了 `{sel}`：滑過一輪會把該輪每一個區塊的按鈕一起點亮（實測 p99 有 101 個）"
    assert '.blk:hover>.blk-ctls>.blk-ctl' in html, \
        "少了 .blk 自己的顯形規則，區塊的按鈕會永遠看不見"


def test_bookmark_block_anchor_same_second(tmp_path=None):
    """區塊錨點的步時戳**必須有毫秒**，而且同毫秒時整步不給錨點。

    ⚠⚠ **為什麼會有這一支**：`-s<步epoch>` 若只有**秒**
    （`_epoch()` 是 `math.floor`；回合錨點則保留毫秒、真撞號時還會加 `-tb<n>`），
    同一輪裡兩次 API 呼叫落在同一秒 ⇒
      ① 兩個 `<div class="step-sep">` 共用一個 id（最高不變量：同頁 id 必須唯一）
      ② 兩步底下的區塊拿到**完全相同**的 `-s<ep>-b<n>`
      ③ `getElementById` 取第一個 ⇒ 在第二塊按 ☆ 存下來的是**第一塊**的摘要；
         跳回去時 `openSub()` 回 `false`（＝精確命中）、`.hl` 框在第一塊、
         **沒有橫幅、還改寫網址列** ＝ 這整套設計要消滅的「安靜指錯」。

    ⚠⚠ **量到 0 次撞號不是保證**：全語料量到「同輪內步時戳撞號 0/67876」只代表
    「還沒發生過」，不代表「不會發生」。`durable_anchor()` 的註解隔壁就寫著
    「只截到整秒，撞號會從 1 變 31」，同一份道理也適用於步這一層。

    ⚠ **毫秒也不是保證**（回合層實測 9047 輪撞 1 次），所以第二節那條底線也要有人守：
    同一輪內 `tms` 重複的那些步，**整步的區塊一律不給錨點**——走 `block_anchor()`
    已經有的「什麼都不給」那條路。**寧可沒有錨點，也不要給一個會指錯的。**
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)

    def _build(sid, ts_a, ts_b, sub):
        proj = tmp / sub / "demo-proj"
        proj.mkdir(parents=True, exist_ok=True)
        evs = [
            {"type": "user", "uuid": "s1", "parentUuid": None,
             "timestamp": "2026-08-09T04:05:06.700Z", "cwd": "/x/Proj", "gitBranch": "main",
             "version": "2.1.150", "sessionId": sid,
             "message": {"role": "user", "content": "問一句SSUSER。"}},
            {"type": "assistant", "uuid": "s2", "parentUuid": "s1",
             "timestamp": ts_a, "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "msecA",
                         "usage": {"input_tokens": 100, "output_tokens": 20},
                         "content": [{"type": "text", "text": "第一步SSTEXTA。"}]}},
            {"type": "assistant", "uuid": "s3", "parentUuid": "s2",
             "timestamp": ts_b, "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "msecB",
                         "usage": {"input_tokens": 120, "output_tokens": 25},
                         "content": [{"type": "text", "text": "第二步SSTEXTB。"}]}},
        ]
        (proj / f"{sid}.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
        out = tmp / f"out-{sub}"
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"{sub}={proj.parent}",
             "--no-codex", "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
        return [p for p in _session_pages(out)][0].read_text(encoding="utf-8")

    def _anchor_of(html, mark):
        body = html.index('<div class="turn')
        pos = html.index(mark, body)
        j = html.rfind('<div class="blk"', 0, pos)
        if j < 0 or j < html.rfind('<div class="turn', 0, pos):
            return ""          # 沒有被包成 .blk ＝ 沒有給錨點
        m = re.search(r' id="([^"]+)"', html[j:html.index(">", j) + 1])
        return m.group(1) if m else ""

    # --- 1. 同一秒、不同毫秒 ⇒ 兩步必須拿到**不同**的錨點 -------------------
    h = _build("00000000-0000-4000-8000-0000000000s1",
               "2026-08-09T04:05:10.100Z", "2026-08-09T04:05:10.900Z", "sec1")
    a, b = _anchor_of(h, "SSTEXTA"), _anchor_of(h, "SSTEXTB")
    assert a and b, f"同一秒的兩步都該有錨點，實得 a={a!r} b={b!r}"
    assert a != b, \
        (f"⚠ 同一秒內的兩步拿到同一個錨點（{a}）——步時戳必須帶毫秒。"
         "在第二塊按 ☆ 會存下第一塊的摘要，而且跳回去時看起來像精確命中")
    # 前置：兩者真的都是步層錨點（不然上一格可能只是因為別的原因不同）
    for name, v in (("SSTEXTA", a), ("SSTEXTB", b)):
        assert re.match(r"^k\d{8}T\d{9}Z-s\d+-b\d+$", v), f"{name} 的文法不對：{v}"
    # 承重：`-s` 那一段本身必須不同（只有 `-b` 不同的話還是同一步，作用域沒分開）
    assert a.split("-b")[0] != b.split("-b")[0], \
        f"兩步的 -s 段相同 ⇒ 步時戳沒有毫秒解析度：{a} vs {b}"
    dup = _page_dup_ids(h)
    assert not dup, f"同一秒兩步造成頁面 id 重複：{sorted(dup)[:6]}"

    # --- 2. 完全同毫秒 ⇒ 那兩步的區塊**一律不給錨點**（底線）---------------
    h2 = _build("00000000-0000-4000-8000-0000000000s2",
                "2026-08-09T04:05:10.100Z", "2026-08-09T04:05:10.100Z", "sec2")
    a2, b2 = _anchor_of(h2, "SSTEXTA"), _anchor_of(h2, "SSTEXTB")
    assert a2 == "" and b2 == "", \
        ("同毫秒的兩步必須整步不給錨點（寧可沒有，也不要給一個會指錯的），"
         f"實得 a={a2!r} b={b2!r}")
    dup2 = _page_dup_ids(h2)
    assert not dup2, f"同毫秒兩步仍造成頁面 id 重複：{sorted(dup2)[:6]}"
    # ⚠ 前置：那一輪本身還是有耐久錨點（否則上面兩格會因為「整輪都沒錨點」而恆真）
    assert re.search(r'<div class="turn assistant" id="k\d{8}T\d{9}Z"', h2), \
        "這一輪應該仍有回合層的耐久錨點，只是步那一層不給"


def test_tmp_base_env(tmp_path=None):
    """`ASV_TEST_TMP` 要真的把暫存目錄挪到指定的基底底下。

    ⚠⚠ **這一格守的是「跨模型 review 輪能不能跑測試」**，不是產品行為。
    在 codex 的 `workspace-write` 沙箱下，`tempfile.mkdtemp()` 建出來的目錄
    **連根目錄都寫不進去**（2026-08-23 實測），而本套件每一支都要建 `projects/<專案>/`
    ⇒ 整套一行斷言都跑不到就死掉，reviewer 於是**靜默降級成純讀碼**。
    有了這個環境變數，charter 只要加一行就能讓它真的跑測試。

    ⚠ 所以這一格壞掉的後果是**看不見的**：本機永遠全綠（沒設那個變數），
    只有在沙箱裡才會退回 `mkdtemp()` 然後整套死掉——而那時候沒有人在看這一格。
    **這正是它需要被明確守住的理由。**
    """
    sys.path.insert(0, str(ROOT))
    base = new_tmp(tmp_path) / "asv-base"
    base.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("ASV_TEST_TMP")
    try:
        os.environ["ASV_TEST_TMP"] = str(base)
        got = new_tmp()
        assert got.parent == base, \
            f"設了 ASV_TEST_TMP 卻沒開在它底下：{got}（期望父目錄 {base}）"
        assert got.is_dir(), f"回傳的路徑不存在：{got}"
        # 真的寫得進去（沙箱下這一格才是重點——建得出來不等於寫得進去）
        (got / "projects" / "p").mkdir(parents=True)
        (got / "projects" / "p" / "x.jsonl").write_text("{}", encoding="utf-8")

        # ⚠⚠ **這一格才是真正守著沙箱那件事的**。
        #
        # 上面那幾行在**本機**永遠會過，不管 `new_tmp()` 內部走的是 `mkdtemp()` 還是
        # `mkdir()`——因為本機沒有沙箱，兩條路都寫得進去。於是「已經解掉了」這個結論
        # 在本機**無法被否證**：`new_tmp()` 若仍是 `mkdtemp(dir=base)`，沙箱裡照樣整套
        # 死在 `WinError 5`，只是死在 `work\tmpXXXX\projects` 而不是
        # `mkdtemp()` 的預設位置。**換了地點，沒換機制。**
        #
        # 成因：`mkdtemp()` 建目錄時會鎖權限（Windows 上是受限的 DACL），
        # 沙箱的受限 token 因此進不去它建的那一層。**跟目錄在哪無關。**
        # ⇒ 唯一能在本機驗到的形狀是「**把 `mkdtemp` 弄成不能用，看它還活不活得下去**」。
        import tempfile as _tf
        _real = _tf.mkdtemp

        def _boom(*a, **k):
            raise AssertionError("設了 ASV_TEST_TMP 就不可以再經過 mkdtemp()")

        os.environ["ASV_TEST_TMP"] = str(base)
        _tf.mkdtemp = _boom
        try:
            sandboxed = new_tmp()
        finally:
            _tf.mkdtemp = _real
        assert sandboxed.parent == base, f"沒開在基底底下：{sandboxed}"
        # 建得出來不等於寫得進去；沙箱下差別就在這一步，所以這裡要真的再建一層
        (sandboxed / "projects" / "p").mkdir(parents=True)
        (sandboxed / "projects" / "p" / "x.jsonl").write_text("{}", encoding="utf-8")
        # 同一個基底底下要能連開兩個而不撞號（`mkdtemp` 本來免費提供的那一半）
        _tf.mkdtemp = _boom
        try:
            second = new_tmp()
        finally:
            _tf.mkdtemp = _real
        assert second != sandboxed, f"連開兩次拿到同一個目錄：{second}"

        # ⚠ 明確給 `tmp_path` 時，環境變數**不可以**蓋掉它
        explicit = new_tmp(base / "explicit")
        assert explicit == base / "explicit", \
            f"明確給的 tmp_path 被環境變數蓋掉了：{explicit}"

        # ⚠ 指到不存在的目錄要**安靜退回** `mkdtemp()`，不可以炸、也不可以自己 mkdir
        #   （自己建出來的在沙箱下照樣寫不進去，那會把「路徑打錯」變成更難查的權限錯誤）
        missing = base / "no-such-dir"
        os.environ["ASV_TEST_TMP"] = str(missing)
        fb = new_tmp()
        assert fb.is_dir(), "退回路徑不存在"
        assert not str(fb).startswith(str(missing)), \
            f"指到不存在的基底時不該自己建出來用：{fb}"
    finally:
        if old is None:
            os.environ.pop("ASV_TEST_TMP", None)
        else:
            os.environ["ASV_TEST_TMP"] = old


def test_bookmark_manage_pages(tmp_path=None):
    """書籤管理頁與設定頁（第 3 期）的**形狀**，以及三頁共用核心的契約。

    ⚠ **這一支只驗形狀，不驗行為。** 行為（篩選／搜尋／排序／改名連動／刪除復原／
    以 sid 重算檔名）要用真 Chrome：
        python scripts/probe_bookmarks.py --manage out/sessions/bookmarks.html
        python scripts/probe_bookmarks.py --settings out/sessions/settings.html
    ⚠ `tests/` 會投影到公開 dist repo，所以這裡不引入 node／Chrome 依賴。

    ⚠⚠ 這裡最重要的兩格：
    1. **`</script>` 破出**——管理頁把**每一場的標題**烤進 `BX_SESS`。標題是對話內容，
       一句 `</script>` 就能截斷整段 JS，而畫面上完全看不出來。守它的是 `js_embed()`。
       （素材必須用 `/rename`：自動標題那條路 `first_user_text()` 會先剝標籤，
       拿它當素材等於沒測到。）
    2. **管理頁的相對路徑真的走得到**——`BK_MGR` 的層數算錯是無聲的：
       頁面照樣產得出來，只是那個連結 404。這裡用 `Path.resolve()` 實際解一次，
       不是比字串。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000m3"
    hostile = '管理頁破出</script><script>window.PWNED3=1;</script>'
    evs = [
        {"type": "user", "uuid": "m1", "parentUuid": None,
         "timestamp": "2026-08-09T04:05:06.700Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "第一則MGFIRST。"}},
        # ⚠ /rename 的名稱不經過剝標籤，是真正會把敵意字串送進 <script> 的那條路。
        {"type": "system", "subtype": "local_command", "uuid": "m0r", "parentUuid": "m1",
         "timestamp": "2026-08-09T04:05:07.000Z", "sessionId": sid,
         "content": f'Session renamed to: {hostile}'},
        {"type": "assistant", "uuid": "m2", "parentUuid": "m1",
         "timestamp": "2026-08-09T04:05:10.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mm2",
                     "usage": {"input_tokens": 80, "output_tokens": 12},
                     "content": [{"type": "text", "text": "回覆MGSECOND。"}]}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    argv = [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
            "--no-codex", "--out", str(out)]
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    mgr_p = out / "sessions" / "bookmarks.html"
    set_p = out / "sessions" / "settings.html"

    # --- 1. 位置：⚠ 一定要在 out/sessions/ 底下，不是 out/ 根 -----------------
    # session 頁在 out/sessions/<source>/<account>/*.html，管理頁放根目錄就差三層
    # ——那是全域最遠的一對，也是 file:// origin 規則最可能出問題的地方。
    assert mgr_p.exists(), f"書籤管理頁應產在 {mgr_p}"
    assert set_p.exists(), f"設定頁應產在 {set_p}"
    assert not (out / "bookmarks.html").exists(), "⚠ 管理頁不可以放 out/ 根"
    assert not (out / "settings.html").exists(), "⚠ 設定頁不可以放 out/ 根"
    mgr = mgr_p.read_text(encoding="utf-8")
    st = set_p.read_text(encoding="utf-8")
    page_p = _session_pages(out)[0]
    page = page_p.read_text(encoding="utf-8")
    idx = (out / "index.html").read_text(encoding="utf-8")

    # --- 2. 三頁共用核心的契約（缺一項那一頁就會壞，而且壞得很安靜）---------
    # core 用到 lsGet/lsSet、BK_MGR，並要求各頁自己定義 bkRefresh。
    for name, html, need_modal in (("session 頁", page, True), ("管理頁", mgr, True),
                                   ("設定頁", st, False)):
        assert "function lsGet(" in html and "function lsSet(" in html, \
            f"{name}缺 lsGet/lsSet——core 全部的存取都靠它們"
        assert "var BK_MGR=" in html, f"{name}缺 BK_MGR（bkFoot 的管理頁連結靠它）"
        assert "function bkRefresh(" in html, f"{name}缺 bkRefresh——core 改完資料沒有人重畫"
        assert "var BK_KEY='asv_bm_v1'" in html, f"{name}沒有內嵌書籤核心"
        assert 'id="bkMsg"' in html, f"{name}缺訊息列（bkFoot 產的，bkSay 寫在那裡）"
        # ⚠ 設定頁**刻意沒有**對話窗：它只整理類別、不編輯單筆書籤。
        assert (('id="bkModal"' in html) is need_modal), \
            f"{name}的對話窗有無不符預期（need_modal={need_modal}）"
        assert _page_dup_ids(html) == [], f"{name}出現重複的 id：{_page_dup_ids(html)}"

    # --- 3. ⚠⚠ `</script>` 不可以破出（管理頁把每一場的標題烤進 BX_SESS）-----
    m = re.search(r"var BX_SESS=(.*)", mgr)
    assert m, "管理頁裡找不到 BX_SESS"
    baked = m.group(1)
    assert "PWNED3" in baked, f"素材沒送進 BX_SESS，這一格在守空氣——{baked[:120]}"
    assert "</script>" not in baked, f"標題把 </script> 原樣寫進 <script> 了：{baked[:160]}"
    assert "\\u003c" in baked, f"BX_SESS 應把 < 跳成 \\u003c，實得 {baked[:160]}"
    assert "</script><script>window.PWNED3" not in mgr, "敵意序列以未跳脫的原樣出現（＝真的破出了）"
    assert mgr.count("<script>") == mgr.count("</script>"), \
        f"管理頁 <script> 開闔不成對：{mgr.count('<script>')} vs {mgr.count('</script>')}"
    assert st.count("<script>") == st.count("</script>"), "設定頁 <script> 開闔不成對"

    # --- 4. 烤進去的反查表：相對 out/、不可以有絕對路徑 ----------------------
    # ⚠ 這一頁**沒有** fetch 可用（file:// 下 Chrome 擋 XHR），所以反查表只能在建置期烤進來。
    sess_map = json.loads(re.search(r"var BX_SESS=(\{.*?\});\s*$", mgr, re.M).group(1)
                          .replace("\\u003c", "<").replace("\\u003e", ">"))
    assert sid in sess_map, f"BX_SESS 應含這一場的 session id，實得 {list(sess_map)[:3]}"
    u = sess_map[sid]["u"]
    assert u.startswith("sessions/"), f"BX_SESS 的路徑應相對 out/，實得 {u!r}"
    assert ":" not in u and not u.startswith("/"), f"不可以是絕對路徑，實得 {u!r}"
    assert (out / u).exists(), f"BX_SESS 指的檔案要真的在：{out / u}"
    assert "C:\\" not in mgr and "C:/" not in mgr, "管理頁不可以出現絕對路徑"

    # --- 5. ⚠ 相對路徑真的走得到（層數算錯是無聲的：頁面照產，連結 404）------
    mgr_href = re.search(r'var BK_MGR="([^"]*)"', page).group(1)
    assert (page_p.parent / mgr_href).resolve() == mgr_p.resolve(), \
        f"session 頁的 BK_MGR({mgr_href!r}) 解不到管理頁：{(page_p.parent / mgr_href).resolve()}"
    assert (mgr_p.parent / "settings.html").resolve() == set_p.resolve()
    assert 'href="../index.html"' in mgr, "管理頁要能回索引"
    assert 'href="bookmarks.html"' in st, "設定頁要能回管理頁"
    # 索引頁的入口：⚠ 無條件出現。書籤在 localStorage 裡，建置期不知道有沒有；
    # 「有書籤才顯示」的話，第一次要找管理頁的人就永遠找不到。
    assert 'href="sessions/bookmarks.html"' in idx, "索引頁要有書籤管理頁的入口"

    # --- 6. 管理頁與設定頁的骨架 -------------------------------------------
    for frag, why in (
        ('id="bxList"', "書籤清單（內容由 JS 填，建置期不可能知道書籤）"),
        ('id="bxDue"', "⚠ 「⏰ 只看該複查的」篩選（一定要有）"),
        ('id="bxCats"', "類別篩選 chip"),
        ('id="bxQ"', "搜尋框"),
        ('id="bxSorts"', "排序"),
        ("function bxRender(", "重畫整份清單"),
        ("function bkSafeRel(", "⚠ 匯入檔的怪路徑不可以變成連結"),
    ):
        assert frag in mgr, f"管理頁缺少{why}：`{frag}`"
    for frag, why in (
        ('id="bsSpans"', "「提醒我複查」的預設值（chip）"),
        ('id="bsCats"', "類別管理清單"),
        ("function bsRenCommit(", "⚠ 改名要連動所有用到它的書籤"),
        ("function bsDel(", "刪除"),
        ("function bsUndoCat(", "⚠ 刪除要可復原（不跳 confirm）"),
        ("function bsMove(", "排序"),
        ("function bkExport(", "⚠ 設定也要進匯出（只在 localStorage 裡＝清一次就沒了）"),
    ):
        assert frag in st, f"設定頁缺少{why}：`{frag}`"
    assert "<select" not in mgr and "<select" not in st, \
        "⚠ 不要下拉（使用上比較不方便）——單選一律用 chip"

    # --- 7. session 頁的類別改成 chip（不可以還留著 datalist）---------------
    assert 'id="bkCats"' in page, "加書籤對話窗的類別 chip 容器"
    assert "bkCatNew(" in page and "＋ 新類別" in page.replace("\\uff0b", "＋"), \
        "⚠ 要有「＋ 新類別」就地新增（不可以退化成只能從設定頁選）"
    assert "list=\"bkCats\"" not in page and "<datalist" not in page, \
        "⚠ datalist 要整個拿掉，不可以兩套控制項並存"

    # --- 8. 再建一次：兩頁要還在，且既有 session 頁要沿用 -------------------
    # ⚠⚠ **這一格證明的是「重建兩次之後兩頁還在」，不是「清孤兒檔那段不會吃掉它們」。**
    #    清孤兒檔在 `if not filtering:` 裡面，而 `filtering` 只要有 `--claude-source`
    #    或 `--no-codex` 任一個就是 True ⇒ **這裡它一次都不會執行**。
    #    那個推論改由 `test_bookmark_link_durability` 第 3 節直接驗述詞。
    #    （不寫清楚的話，下一個人會以為這一格涵蓋了孤兒清理——那是空心的。）
    r2 = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    assert r2.returncode == 0, f"第二次建置非零退出\n{r2.stdout}\n{r2.stderr}"
    assert mgr_p.exists() and set_p.exists(), "⚠ 重建之後管理頁／設定頁被清孤兒檔那一段吃掉了"
    assert "沿用 1" in r2.stdout, f"第二次應沿用既有 session 頁：{r2.stdout}"

    # --- 9. `--format md` 不該產這兩頁（它們是 HTML 專屬的）-----------------
    out2 = tmp / "out-md"
    r3 = subprocess.run(argv[:-1] + [str(out2), "--format", "md"],
                        capture_output=True, text=True, encoding="utf-8")
    assert r3.returncode == 0, f"md 模式非零退出\n{r3.stdout}\n{r3.stderr}"
    assert not (out2 / "sessions" / "bookmarks.html").exists(), "--format md 不該產管理頁"
    assert not (out2 / "sessions" / "settings.html").exists(), "--format md 不該產設定頁"
    # --- 9b. ⚠ 既有目錄切成 md：**留著的兩頁會帶著過期的 BX_SESS** ------------
    # 之後某場檔名變了而該次是純 md 全量建置時，孤兒清理會刪掉舊 .html、又不會產新的
    # ⇒ 管理頁生出一條指向**已刪檔案**的連結，而不是誠實的「⚠ 對不到檔案」。
    # 比照 cache-report.* 那段：不重產就刪掉。
    assert mgr_p.exists() and set_p.exists(), "前提：這個 out 目錄本來有這兩頁"
    r4 = subprocess.run(argv + ["--format", "md"],
                        capture_output=True, text=True, encoding="utf-8")
    assert r4.returncode == 0, f"md 模式非零退出\n{r4.stdout}\n{r4.stderr}"
    assert not mgr_p.exists(), "⚠ 切成 md 之後不可以留著一份過期的管理頁"
    assert not set_p.exists(), "⚠ 切成 md 之後不可以留著一份過期的設定頁"
    print("OK: bookmark manage/settings pages (phase 3) shape test passed")


def test_bookmark_link_durability(tmp_path=None):
    """書籤管理頁**算得出檔名**的三個保證（整條線的核心主張）。

    書籤的身分是 `session_id + 錨點`，記錄裡的 `url` 只是快取——因為輸出檔名是
    **本地時間＋專案名＋帳號名**組出來的，專案改名或機器換時區就全變。
    管理頁因此每次都拿 sid 重算檔名。這一支守的就是那個「重算」在三種情況下都還成立：

    1. **改了專案名** → 同一個 sid 對到新檔名（沒有這一格，整套設計的理由就沒人在守）
    2. **renderer 升版後跑縮範圍建置** → 不可以把沒涵蓋到的那些講成「不在這次的輸出裡」
    3. **清孤兒檔那段掃不到這兩頁**（直接驗述詞，見下面的說明）
    """
    sys.path.insert(0, str(ROOT))
    import ai_session_viewer as asv
    tmp = new_tmp(tmp_path)
    root = tmp / "projects"
    out = tmp / "out"
    ids = {"Alpha": "00000000-0000-4000-8000-0000000aaaa1",
           "Beta": "00000000-0000-4000-8000-0000000bbbb1"}

    def mk(proj, sid, day):
        p = root / proj
        p.mkdir(parents=True, exist_ok=True)
        evs = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": f"2026-08-0{day}T04:05:06.700Z", "cwd": f"/x/{proj}",
             "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
             "message": {"role": "user", "content": f"{proj} 的訊息內容夠長。" * 3}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": f"2026-08-0{day}T04:05:10.000Z", "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                         "usage": {"input_tokens": 80, "output_tokens": 12},
                         "content": [{"type": "text", "text": "回覆。"}]}}]
        (p / f"{sid}.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")

    def build(*extra):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"demo={root}",
             "--no-codex", "--out", str(out)] + list(extra),
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"非零退出\n{r.stdout}\n{r.stderr}"
        return r

    def bx():
        """讀管理頁烤進去的 sid → 檔名反查表。"""
        mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
        m = re.search(r"var BX_SESS=(\{.*?\});\s*$", mgr, re.M)
        assert m, "管理頁裡找不到 BX_SESS"
        return json.loads(m.group(1).replace("\\u003c", "<").replace("\\u003e", ">"))

    mk("Alpha", ids["Alpha"], 1)
    mk("Beta", ids["Beta"], 2)
    build()
    first = bx()
    assert set(first) == set(ids.values()), f"兩場都要在表裡，實得 {list(first)}"

    # --- 1. ⭐ 改了專案名，同一個 sid 要對到**新的**檔名 --------------------
    # 這是整條線最核心的那句話：「檔名會變，所以身分不綁檔名」。
    # ⚠ 真正的「改專案名」是**夾名與 cwd 一起變**——顯示名是從 `cwd` 推出來的
    #   （`build_project_names`），只改夾名的話檔名根本不會動，這一格就會在守空氣。
    (root / "Alpha").rename(root / "Alpha-renamed")
    jf = root / "Alpha-renamed" / f"{ids['Alpha']}.jsonl"
    jf.write_text(jf.read_text(encoding="utf-8").replace('"/x/Alpha"', '"/x/Alpha-renamed"'),
                  encoding="utf-8")
    build()
    after = bx()
    a = ids["Alpha"]
    assert a in after, "改名之後那一場應該還在表裡（sid 沒變）"
    # ⚠ 前提：檔名**真的**變了。不驗這一格的話，下一格可能是在守空氣。
    assert after[a]["u"] != first[a]["u"], \
        f"專案改名後檔名應該不同，兩次都是 {after[a]['u']}——這一格在守空氣"
    assert "Alpha-renamed" in after[a]["u"], f"新檔名應含新專案名，實得 {after[a]['u']}"
    assert (out / after[a]["u"]).exists(), f"新檔名要指到真的存在的檔：{after[a]['u']}"

    # --- 2. ⚠⚠ renderer 升版 ＋ 縮範圍建置，不可以把還在的檔講成「不在輸出裡」---
    # `load_manifest` 在版本不符時回空字典（那是對的：快取的 row 內容可能過時），
    # 於是 `rows` 只剩本次掃到的那幾場。但**檔名與標題不隨 renderer 改變**，
    # 所以管理頁另外走 `load_manifest_paths()` 把它們補回來。
    mf = out / asv.MANIFEST_NAME
    d = json.loads(mf.read_text(encoding="utf-8"))
    d["renderer_version"] = -1                       # 模擬升版：manifest 一律被視為過期
    mf.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    build("--project", "Beta")                       # 只涵蓋 Beta
    scoped = bx()
    assert set(scoped) == set(ids.values()), \
        f"⚠ 縮範圍建置不可以把沒涵蓋到的 session 從表裡砍掉，實得 {list(scoped)}"
    for sid, v in scoped.items():
        assert (out / v["u"]).exists(), f"表裡的每一筆都要指到真的存在的檔：{v['u']}"

    # --- 2b. 但檔案真的不見了就**不可以**還留在表裡（補進來的那批要對帳）------
    gone = out / scoped[a]["u"]
    gone.unlink()
    build("--project", "Beta")
    assert a not in bx(), "⚠ manifest 有紀錄但檔案已不在時，不可以把死連結烤進表裡"

    # --- 3. 清孤兒檔那段掃不到這兩頁（**直接驗述詞**）------------------------
    # ⚠⚠ 端到端測不到這件事：清孤兒檔在 `if not filtering:` 裡面，而 `filtering`
    #    只要有 `--claude-source` / `--no-codex` 任一個就是 True ⇒ 測試裡它**一次都不會執行**
    #    （要讓它跑就得不給任何來源旗標，那會去掃使用者真正的 ~/.claude，測試絕不可以）。
    #    所以這裡驗的是那段程式的**述詞**：守衛是 `rel.parts[0] in scanned_source_dirs`，
    #    而 `scanned_source_dirs` 只會是 `safe_name(<source_kind>, 24)`。
    for name in ("bookmarks.html", "settings.html"):
        p = out / "sessions" / name
        assert p.exists(), f"{name} 應該在 out/sessions/ 底下"
        parts = p.relative_to(out / "sessions").parts
        assert len(parts) == 1, f"{name} 不在 sessions/ 第一層，parts={parts}"
        for kind in (asv.SOURCE_CLAUDE, asv.SOURCE_CODEX):
            assert parts[0] != asv.safe_name(kind, 24), \
                f"⚠ {name} 和來源夾名撞名（{kind}）⇒ 會被清孤兒檔那段砍掉"
    print("OK: bookmark link durability (rename / stale manifest / orphan sweep) test passed")


def test_bookmark_codex_title_masked(tmp_path=None):
    """⚠ **Codex 的標題一律當成「不是使用者取的」**（`BK_TOWN=0`）。

    「只匯書籤」那份的遮蔽規則是：**使用者自己取的名字才留著**，其餘一律視為對話內容。
    Claude 那條路的 `extract_rename` 讀的是 `/rename` 事件，那確實是使用者動作；
    Codex 這邊 `s.rename` 來自 `session_index.jsonl` 的 `thread_name`——
    那只是索引檔的一個欄位，**不是使用者動作的證據**。

    實查本機語料（2026-08-22）：唯一一筆 `thread_name` 是
    `Codex Companion Task: You are running a TOOLING PROBE, not a`
    ——明顯是從對話內容截出來的 60 字。**遮蔽是隱私保證，證據不足時從嚴。**

    ⚠ 這一支只驗 `BK_TOWN`（遮蔽用的旗標），**不驗 `s.rename`**：
    後者還管畫面上的 ✎ 標記與索引頁，那部分行為刻意不動。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    sroot = tmp / "codex" / "sessions" / "2026" / "08" / "01"
    sroot.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-00000000cdx1"
    # Codex 的 thread_name：形狀比照本機實查到的那一筆（從對話內容截出來的）
    (tmp / "codex" / "session_index.jsonl").write_text(
        json.dumps({"id": sid, "thread_name": "Codex Companion Task: 這是從對話內容截出來的"},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    evs = [
        {"timestamp": "2026-08-01T04:05:06.700Z",
         "type": "session_meta",
         "payload": {"id": sid, "cwd": "/x/Proj", "originator": "codex_cli_rs"}},
        {"timestamp": "2026-08-01T04:05:07.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "CDXFIRST 第一則。"}},
        {"timestamp": "2026-08-01T04:05:10.000Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "CDXSECOND 回覆。"}},
    ]
    (sroot / f"rollout-2026-08-01T04-05-06-{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-source", f"cx={tmp / 'codex' / 'sessions'}",
         "--no-claude", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\n{r.stdout}\n{r.stderr}"
    pages = _session_pages(out)
    assert pages, f"沒產出 Codex session 頁\n{r.stdout}\n{r.stderr}"
    html = pages[0].read_text(encoding="utf-8")
    # 前提：這一場真的被 thread_name 命名了（否則下面那格在守空氣）
    assert "Codex Companion Task" in html, \
        "素材沒讓 thread_name 生效，這一格在守空氣"
    m = re.search(r"BK_TOWN=(\d)", html)
    assert m, "頁面裡找不到 BK_TOWN"
    assert m.group(1) == "0", \
        f"⚠ Codex 的標題不可以被當成「使用者自己取的」（BK_TOWN={m.group(1)}）——" \
        "「只匯書籤」那份會因此帶著由對話內容生成的標題出門"
    print("OK: codex title is masked in redacted export test passed")


def test_bookmark_deleted_source_pruned(tmp_path=None):
    """⚠⚠ **明確來源裡被刪掉的 session，不可以永遠留在書籤管理頁的反查表裡。**

    任何縮範圍旗標（這裡是 `--claude-source`）都會讓 `filtering=True`：整份舊 manifest
    原封不動沿用、孤兒檔也不清 ⇒ 舊 HTML 還躺在磁碟上 ⇒ 管理頁的 fallback 每次都把
    那一場再烤回 `BX_SESS`，對著一場**已經刪掉的對話**說「在這次的輸出裡」並給連結。

    ⚠ 這一支要驗三件事，缺一件就會變成空心：
      ①**素材真的生效過**（第一次建置時兩場都在表裡）——否則第二次「不在」毫無意義；
      ②刪掉來源檔、用**同一個來源**重建之後，那一場從 `BX_SESS` 消失；
      ③**沒被刪的那一場還在**（不然「整張表都清掉」也會通過）。
    ⚠ 還要驗**範圍外的來源不受影響**：`prune_gone_sources` 只對本次掃過的根動手，
      這是它保守的那一半，沒有測就等於沒有那個限制。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)

    def _mk(sid, mark, ts):
        evs = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": ts, "cwd": "/x/Proj", "gitBranch": "main",
             "version": "2.1.150", "sessionId": sid,
             "message": {"role": "user", "content": f"{mark} 第一則。"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": ts, "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                         "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": f"{mark} 回覆。"}]}},
        ]
        f = proj / f"{sid}.jsonl"
        f.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs),
                     encoding="utf-8")
        return f

    sid_a = "00000000-0000-4000-8000-0000000000a1"
    sid_b = "00000000-0000-4000-8000-0000000000b1"
    fa = _mk(sid_a, "ALPHA", "2026-08-01T01:00:00.000Z")
    _mk(sid_b, "BETA", "2026-08-01T02:00:00.000Z")
    out = tmp / "out"
    cmd = [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
           "--no-codex", "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"第一次建置非零退出\n{r.stdout}\n{r.stderr}"
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    # ① 前置：兩場都真的進了反查表（不然下面在守空氣）
    assert sid_a in mgr and sid_b in mgr, \
        f"素材沒生效：第一次建置的 BX_SESS 就沒有兩場，這一格在守空氣"

    fa.unlink()                       # 來源被刪掉（使用者清掉了那場對話）
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"第二次建置非零退出\n{r.stdout}\n{r.stderr}"
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    # ② 被刪掉的那一場不可以還在表裡
    assert sid_a not in mgr, \
        "⚠ 來源已刪除的 session 仍留在 BX_SESS——管理頁會對著一場不存在的對話給連結"
    # ③ 沒被刪的那一場還在（否則「整張表清空」也會通過）
    assert sid_b in mgr, "把還在的那一場也清掉了——對帳對過頭了"

    # ④ 範圍外的來源不受影響：換一個**沒有被掃到**的來源根重建，
    #    manifest 裡屬於 demo 那個根的紀錄要原封不動留著。
    # ⚠⚠ **這一格若寫成下面那樣會是空心的**（突變檢驗會抓到）：
    #    `sid_b` 的 JSONL 本來就還在磁碟上，所以就算 `prune_gone_sources` 完全不管
    #    `roots`（＝只要檔案不在就對掉），它照樣留得下來 ⇒ 把那個保守限制整個拿掉
    #    測試仍然全綠。要讓它承重，範圍外那一場的來源檔**必須也是不在的**——
    #    這樣「留著」就只可能來自「不在本次掃過的根底下」這條規則。
    fb_path = proj / f"{sid_b}.jsonl"
    fb_path.unlink()                  # 模擬那顆磁碟沒掛上／那個來源這次不可見
    other = tmp / "other" / "projects" / "p"
    other.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"oth={other.parent}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"第三次建置非零退出\n{r.stdout}\n{r.stderr}"
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    assert sid_b in mgr, \
        "⚠ 掃了別的來源根，卻把範圍外那一場對掉了——`prune` 只該碰本次掃過的根"
    print("OK: deleted source pruned from BX_SESS test passed")


def test_bookmark_account_label_rebuild(tmp_path=None):
    """⚠⚠ **帳號標籤變了就一定要重建**——`BX_SESS` 不可以繼續指到舊帳號目錄。

    輸出路徑是 `<來源>/<帳號>/<檔名>`，而 `reusable` 用的 `sig` **只看檔案內容**。
    同一份 JSONL 換一個 `--claude-source 標籤=路徑` 重跑時，若 `reusable` 不比帳號標籤，
    舊 row 會被整個沿用 ⇒ 頁面留在**舊的帳號目錄**下，書籤管理頁的反查表與索引頁
    也跟著指到那裡。

    ⚠ 這一支的存在理由：`reusable` 裡
    `row.get("account","") == (acc_name or "")` **真的在做事**（拿掉它 `BX_SESS` 會指到
    舊帳號目錄），而沒有這一支時，**整份 `test_smoke.py` 拿掉它仍然全綠**——那是一條確定存在的空心。

    ⚠ 兩件都要驗，缺一件就沒有承重：
      ①**素材真的生效過**（第一次建置時反查表指的是 `demo/`）；
      ②換標籤重建後指到 `other/`。只驗②的話，「反查表永遠指到最後一次的標籤」
        這種假象（例如整份重建）也會通過，但那不是這裡要守的東西——
        所以①要明確寫出「第一次是 demo」。
    ⚠ **舊的 `demo/` 那一頁還留在磁碟上**（縮範圍建置不清孤兒檔，
      `SCOPE-BOOKMARK-PRUNED-ORPHAN-FILES`）⇒ 不可以用「檔案在不在」當斷言，
      那一格恆真。要看的是 `BX_SESS` 裡的 `u`。
    """
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000d1"
    evs = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "timestamp": "2026-08-01T01:00:00.000Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "DELTA 第一則。"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "timestamp": "2026-08-01T01:00:00.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "text", "text": "DELTA 回覆。"}]}},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"

    def _u(label):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"{label}={proj.parent}",
             "--no-codex", "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"`{label}` 建置非零退出\n{r.stdout}\n{r.stderr}"
        mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
        m = re.search(r"var BX_SESS=(\{.*?\});", mgr, re.S)
        assert m, "管理頁裡讀不到 BX_SESS（渲染器的形狀變了？）"
        table = json.loads(m.group(1))
        assert sid in table, f"素材沒生效：`{label}` 建置後 BX_SESS 裡沒有這一場"
        return table[sid]["u"]

    # ① 素材真的生效過：第一次的反查表指的是 demo/
    u1 = _u("demo")
    assert "/demo/" in u1, f"素材沒生效：第一次建置就不在 demo/ 底下（u={u1}）"
    # ② 換標籤重建 → 指到 other/
    u2 = _u("other")
    assert "/other/" in u2, \
        ("⚠ 換了 `--claude-source` 的帳號標籤，BX_SESS 還指著舊帳號目錄"
         f"（u={u2}）——`reusable` 沒有把帳號標籤算進去")
    print("OK: account label change rebuilds and BX_SESS follows test passed")


def test_bookmark_all_sources_deleted(tmp_path=None):
    """⚠⚠ **來源被刪到「一場都不剩」時也要對帳。**

    `main()` 裡的 `if not files: … return` 位在 `prune_gone_sources()` **之前**，
    所以最極端的那一格反而不會執行：舊 manifest、書籤管理頁的 `BX_SESS`、索引頁
    全部原封不動留著，連結指向已經不存在的檔。
    （`test_bookmark_deleted_source_pruned` 只做
    「兩場刪成一場」，2→0 那一格沒有素材走到。）

    ⚠ 同時要驗**反過來的保守面**：一個來源根都沒掃到時（`--no-claude --no-codex`）
    **什麼都不要動**——那不是「東西被刪了」，是「這次沒有去看」。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000c1"
    evs = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "timestamp": "2026-08-01T01:00:00.000Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.150", "sessionId": sid,
         "message": {"role": "user", "content": "GAMMA 第一則。"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "timestamp": "2026-08-01T01:00:00.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "text", "text": "GAMMA 回覆。"}]}},
    ]
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    cmd = [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
           "--no-codex", "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"第一次建置非零退出\n{r.stdout}\n{r.stderr}"
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    # ① 素材真的生效過
    assert sid in mgr, "素材沒生效：第一次建置就沒進 BX_SESS，這一支在守空氣"

    # ② 刪到一場不剩，用**同一個來源**重建 → 反查表要清乾淨
    f.unlink()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"零場建置非零退出\n{r.stdout}\n{r.stderr}"
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    assert sid not in mgr, \
        "⚠ 來源掉到零場時 BX_SESS 沒有對帳——管理頁會對著一場不存在的對話給連結"

    # ③ 保守面：一個來源根都沒掃到時什麼都不要動
    f.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    assert sid in mgr, "重建之後那一場應該回來了"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--no-claude", "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"no-source 建置非零退出\n{r.stdout}\n{r.stderr}"
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    assert sid in mgr, \
        "⚠ 一個來源根都沒掃到就把紀錄清掉了——那不是『東西被刪了』，是『這次沒有去看』"
    print("OK: all sources deleted reconciles BX_SESS test passed")


def test_zero_scan_keeps_out_of_scope_index(tmp_path=None):
    """⚠⚠ **零場對帳那條路不可以把 `index.html` 寫成空表。**

    「零場也要對帳」那個分支把 manifest 對帳做對了（範圍外的 row 留著、`BX_SESS`
    的 fallback 也留著），但索引頁寫的是 `render_index_html([], …)`——**寫死的空 rows**。
    正常路徑用的是 `new_entries` 算出來的 rows，縮範圍時本來就會把範圍外那些一起畫。
    兩條路不一致的結果是：manifest 說有一場、`BX_SESS` 還指著它、HTML 檔還在磁碟上，
    **只有索引頁說一場都沒有**。

    ⚠ 三件都要驗：
      ①**素材真的生效過**（A＋B 兩場都在索引裡）；
      ②B 的來源刪光、用**同一個 B**重建（走零場那條路）之後，A 還在索引裡；
      ③B 不在了（不然「整份原封不動」也會通過）。
    """
    tmp = new_tmp(tmp_path)

    def _mk(root, sid, mark, ts):
        root.mkdir(parents=True, exist_ok=True)
        evs = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": ts, "cwd": "/x/Proj", "gitBranch": "main",
             "version": "2.1.150", "sessionId": sid,
             "message": {"role": "user", "content": f"{mark} 第一則。"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": ts, "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                         "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": f"{mark} 回覆。"}]}},
        ]
        f = root / f"{sid}.jsonl"
        f.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs),
                     encoding="utf-8")
        return f

    proj_a = tmp / "srcA" / "projects" / "pa"
    proj_b = tmp / "srcB" / "projects" / "pb"
    _mk(proj_a, "00000000-0000-4000-8000-0000000000e1", "EPSILON", "2026-08-01T01:00:00.000Z")
    fb = _mk(proj_b, "00000000-0000-4000-8000-0000000000e2", "ZETA", "2026-08-01T02:00:00.000Z")
    out = tmp / "out"

    def _build(label, root):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"{label}={root}",
             "--no-codex", "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"`{label}` 建置非零退出\n{r.stdout}\n{r.stderr}"
        return r

    _build("a", proj_a.parent)
    _build("b", proj_b.parent)
    idx = (out / "index.html").read_text(encoding="utf-8")
    # ① 素材真的生效過
    assert "EPSILON" in idx and "ZETA" in idx, \
        "素材沒生效：兩場建完索引裡就沒有兩場，這一支在守空氣"

    fb.unlink()                       # B 的來源刪光 → 下一次 B 的建置會走「零場」那條路
    r = _build("b", proj_b.parent)
    assert "沒有找到任何 session 檔" in r.stdout, \
        f"沒有走到零場那條路，這一支測到的不是要測的東西\n{r.stdout}"
    idx = (out / "index.html").read_text(encoding="utf-8")
    # ② 範圍外那一場還在索引裡
    assert "EPSILON" in idx, \
        ("⚠ 零場建置把索引寫成空表——範圍外、檔案還在、manifest 還記著的 session "
         "從索引消失了（manifest 與 BX_SESS 都還指著它）")
    # ③ 被刪掉的那一場不在（否則「整份原封不動」也會通過）
    assert "ZETA" not in idx, "來源已刪除的那一場還留在索引裡"
    print("OK: zero-scan build keeps out-of-scope rows in index test passed")


def test_zero_scan_keeps_cache_report_links(tmp_path=None):
    """⚠⚠ **零場對帳那條路也不可以寫死 `cache_report=False, codex_report=False`。**

    上一條（`test_zero_scan_keeps_out_of_scope_index`）守的是同一個呼叫的**第一個**參數
    （rows 從 `new_entries` 算）；第三、四個參數若寫死成 `False, False`
    ⇒ 零場建置之後 `cache-report.html` / `cache-hypotheses.html` **檔案都還在磁碟上**，
    索引頁卻不再連到它們。**和上一條是同一個形狀**（用寫死的值取代對帳出來的狀態），
    只是換到隔壁兩個參數。

    ⚠ 這一支只守「連結還在」這一件事（一格只守一件事）。
    ⚠ 前置要驗：素材真的產得出報告，否則兩次都是 False 也會通過。
    """
    tmp = new_tmp(tmp_path)
    projects = _build_fixture(tmp / "srcA")          # 這份假語料會產出快取報告
    proj_b = tmp / "srcB" / "projects" / "pb"
    proj_b.mkdir(parents=True, exist_ok=True)
    sid_b = "00000000-0000-4000-8000-0000000000f2"
    evs = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "timestamp": "2026-08-01T02:00:00.000Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.150", "sessionId": sid_b,
         "message": {"role": "user", "content": "ETA 第一則。"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "timestamp": "2026-08-01T02:00:00.000Z", "sessionId": sid_b,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "text", "text": "ETA 回覆。"}]}},
    ]
    fb = proj_b / f"{sid_b}.jsonl"
    fb.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"

    def _build(label, root):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"{label}={root}",
             "--no-codex", "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"`{label}` 建置非零退出\n{r.stdout}\n{r.stderr}"
        return r

    _build("a", projects)
    _build("b", proj_b.parent)
    idx = (out / "index.html").read_text(encoding="utf-8")
    # 前置：素材真的產得出報告，而且索引真的連著它
    assert (out / "cache-report.html").exists(), "素材沒生效：這份假語料沒產出快取報告"
    assert "cache-report.html" in idx, "素材沒生效：兩場建完索引就沒連到報告，這一支在守空氣"

    fb.unlink()
    r = _build("b", proj_b.parent)
    assert "沒有找到任何 session 檔" in r.stdout, \
        f"沒有走到零場那條路，這一支測到的不是要測的東西\n{r.stdout}"
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert (out / "cache-report.html").exists(), \
        "報告檔被零場建置刪掉了（這一支要驗的是連結，不是檔案；檔案沒了代表另有問題）"
    assert "cache-report.html" in idx, \
        ("⚠ 零場建置把快取報告的連結拿掉了——檔案還在磁碟上，索引頁卻不連它了"
         "（`render_index_html` 的第三、四個參數被寫死成 False）")
    print("OK: zero-scan build keeps cache-report links test passed")


def test_no_uppercase_unicode_escape_in_js(tmp_path=None):
    """⚠⚠ **JS 沒有大寫 U 的跳脫語法**——寫了會原樣顯示在畫面上。

    那四大段書籤 JS 是 Python 的 `r'''...'''` 常數，裡面寫的東西**原樣進 `<script>`**。
    Python 的 `chr(92)+"U0001f516"` 在非 raw 字串裡是 🔖，在 raw 字串裡卻是十個字元；
    而 JS 看到它會把反斜線丟掉，於是頁面上出現的是那串**字面**，不是圖示。
    受影響的有：管理頁連結、頂列書籤鈕、設定頁刪除鈕、索引頁書籤圖示，**四處全中**，
    而且四支形狀測試與四個模式的探針**沒有一個會紅**（它們比對的是 id 與類別名）。

    ⚠ 這一支掃的是**產出來的頁面**，不是原始碼：真正會被使用者看到的是前者。
    ⚠ 掃描器要找的字面不可以出現在它自己掃的內容裡 ⇒ 這裡用 `chr(92)` 組出 pattern，
      而那幾段 JS 的註解也一律寫「大寫 U 的跳脫」、不寫那個字面。
    """
    tmp = new_tmp(tmp_path)
    projects = _build_fixture(tmp)
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={projects}",
         "--no-codex", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\n{r.stdout}\n{r.stderr}"
    pat = re.compile(chr(92) * 2 + "U[0-9a-fA-F]{8}")
    pages = _session_pages(out) + [out / "index.html",
                                   out / "sessions" / "bookmarks.html",
                                   out / "sessions" / "settings.html"]
    # 前置：這幾頁真的都產出來了（少一頁就等於少掃一頁，而那很安靜）
    assert all(p.exists() for p in pages), \
        f"少了頁面，掃描範圍不完整：{[str(p) for p in pages if not p.exists()]}"
    bad = []
    for p in pages:
        html = p.read_text(encoding="utf-8")
        for block in re.findall(r"<script>(.*?)</script>", html, re.S):
            for hit in pat.findall(block):
                bad.append(f"{p.name}: {hit}")
    assert not bad, \
        "⚠ <script> 裡出現了 JS 不認得的大寫 U 跳脫，畫面上會顯示成那串字面：" + str(bad[:6])
    # 對照組：那四個圖示要真的以圖示的樣子出現（不然「整段刪掉」也會通過）
    idx = (out / "index.html").read_text(encoding="utf-8")
    mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
    assert "\U0001f516" in idx or chr(0x1f516) in idx, "索引頁的書籤圖示不見了"
    assert chr(0x1f516) in mgr, "管理頁的書籤圖示不見了"
    print("OK: no uppercase-U escapes inside generated <script> test passed")


def test_user_turn_fidelity(tmp_path=None):
    """使用者回合忠實度（RENDERER_VERSION 55）：排隊提示詞、背景任務通知、指令列。

    ⚠⚠ **為什麼要合成輸入**：這三件在真實語料裡都有（探針量得到），但真實語料
    **驗不到反例**——「撤回後沒送出的提示詞不可以畫出來」在語料裡只有 14 次，
    而且沒有任何一次會讓斷言變紅。反例要自己造。

    ⚠ 這一格守的是 `ai_session_viewer.py` 註解裡三句保證：
      ① `popAll` 是撤回，不可以畫（否則多一句幽靈重複句）
      ② 指令列**不發耐久錨點**、通知列**照發**（既有書籤逐字不變的唯一機械保證）
      ③ 指令的結果會併回指令那一列，不另起一輪（不然多一個同時間戳的回合 ⇒ 撞號）
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000fd"
    SENT = "這一句是排隊送出的QUEUEDSENT。"        # 助手還在跑 ⇒ 掛進該輪內部
    SENT2 = "這一句排隊時沒有輪在跑QUEUEDCROSS。"   # 沒有開著的回合 ⇒ 退回一般 user 回合
    LATE = "這一句是那一輪跑完才被讀到QUEUEDLATE。"  # remove 落在 turn_duration 之後
    MID2 = "這一句是下一輪跑到一半插的QUEUEDMID2。"  # turn_duration 之後又有新的 assistant
    SENT_TAIL = "這一句插在那一輪最後QUEUEDTAIL。"   # 後面沒有步驟 ⇒ 走回合結尾那道關閉
    DROPPED = "這一句被我撤回了QUEUEDDROPPED。"
    # ⚠ 拼接：這個字串是本測試要驗「不可以照原樣畫出來」的東西，
    #    寫成字面會讓掃描類的斷言掃到測試自己。
    NTAG = "task-" + "notification"
    NOTIFY = (f"<{NTAG}>\n<task-id>bq7x</task-id>\n"
              "<summary>Background command NOTIFYSUMMARY completed</summary>\n"
              "<status>completed</status>\n"
              "<output-file>C:\\tmp\\bq7x.output</output-file>\n"
              f"</{NTAG}>")
    # 系統注入、也走佇列的通知：**沒有 `origin`**，這就是它與人打的提示詞唯一的差別
    PHANTOM = (f"<{NTAG}>\n<task-id>bz9q</task-id>\n"
               "<summary>PHANTOMNOTIFY should never become a user turn</summary>\n"
               f"<status>completed</status>\n</{NTAG}>")
    # ⚠⚠ 以下兩筆素材各自**只會走到一條**至今沒有素材走到的路：
    #  ①「指令與結果寫在同一則」→ 驗 `parse_command()` **先比 name 再比 out** 的順序
    #  ②「通知文字裡剛好含 `<command-name>`」→ 驗 `user_special_blocks()` **先判通知**
    # （第三筆「第一則就是指令」直接放在 `evs` 開頭，驗 `first_user_text()` 那道守衛。）
    SAME = ("<command-name>/statusline</command-name>\n"
            "<command-args>ARGSAME</command-args>\n"
            "<local-command-stdout>OUTSAME</local-command-stdout>")
    NTAG_IN = (f"<{NTAG}>\n<task-id>bq9z</task-id>\n"
               "<summary>NOTIFYWINS &lt;command-name&gt;/fake&lt;/command-name&gt;</summary>\n"
               f"<status>completed</status>\n</{NTAG}>")
    T_CMD = "2026-07-24T03:10:00.100Z"      # 指令與其結果**同一時刻**（實測就是如此）
    # ⚠ 一定要是**真的解得開**的 base64：解不開會畫成「資料無效」佔位，
    #   而下面那些斷言只看有沒有 `<img>`，會靜靜地驗到另一件事。
    PNG1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
              "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    evs = [
        # ③ **第一則就是指令**：`first_user_text()` 必須跳過它去找真正的提問
        {"type": "user", "uuid": "f0", "parentUuid": None,
         "timestamp": "2026-07-24T02:59:50.000Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.240", "sessionId": sid,
         "message": {"role": "user", "content":
                     "<command-name>/context</command-name>\n<command-args></command-args>"}},
        {"type": "user", "uuid": "f1", "parentUuid": None,
         "timestamp": "2026-07-24T03:00:00.000Z", "cwd": "/x/Proj", "gitBranch": "main",
         "version": "2.1.240", "sessionId": sid,
         "message": {"role": "user", "content": "一開始的正常提問FIRSTASK。"}},
        # ① 指令與結果**寫在同一則**（`parse_command` 先比 name 才對）
        {"type": "user", "uuid": "f1b", "parentUuid": "f1",
         "timestamp": "2026-07-24T03:00:02.000Z", "sessionId": sid,
         "message": {"role": "user", "content": SAME}},
        # ② 通知文字裡剛好含 `<command-name>`（`user_special_blocks` 先判通知才對）
        {"type": "user", "uuid": "f1c", "parentUuid": "f1b",
         "timestamp": "2026-07-24T03:00:03.000Z", "sessionId": sid,
         "message": {"role": "user", "content": NTAG_IN}},
        {"type": "assistant", "uuid": "f2", "parentUuid": "f1",
         "timestamp": "2026-07-24T03:00:05.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf2",
                     "usage": {"input_tokens": 100, "output_tokens": 20},
                     "content": [{"type": "text", "text": "正常回覆NORMALREPLY。"}]}},
        # --- ① 排隊送出：真的被 remove 取走 ⇒ 必須畫出來 -----------------------
        # ⚠⚠ 這一則**帶圖片**（排隊時貼上的），所以 `prompt` 是 block 陣列——真實資料
        # 就是這個形狀（CLI 只在那則帶得出圖片 block 時才從字串換成陣列）。
        # ⚠ `queue-operation` 那半仍是字串：CLI 只在佇列項的值是字串時才寫 `content`。
        # ⚠⚠ 選它當帶圖的那一則是刻意的：**下面「插話沒有推移其他區塊錨點」那一格
        #    比對的就是這一輪**，圖片走錯路（當成獨立 block）會在那裡變紅。
        {"type": "attachment", "uuid": "f3", "timestamp": "2026-07-24T03:00:07.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command",
                        "prompt": [{"type": "text", "text": SENT},
                                   {"type": "image", "source": {
                                       "type": "base64", "media_type": "image/png",
                                       "data": PNG1X1}}],
                        "imagePasteIds": ["paste-a"], "commandMode": "prompt",
                        "origin": {"kind": "human"},
                        "timestamp": "2026-07-24T03:00:07.000Z"}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-24T03:00:07.000Z", "sessionId": sid, "content": SENT},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-24T03:00:09.000Z", "sessionId": sid, "content": SENT},
        # 插話之後**同一輪內還有兩個步驟**（不同的 message.id ＝兩次 API 呼叫）。
        # ⚠⚠ 沒有這兩筆，本測試就**分不出 B（只縮第一段）和 A（縮到那一輪結束）**——
        # 插話後只有 0～1 個步驟時兩種畫法的產出一模一樣。
        {"type": "assistant", "uuid": "f2b", "parentUuid": "f2",
         "timestamp": "2026-07-24T03:00:15.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf2b",
                     "usage": {"input_tokens": 120, "output_tokens": 15},
                     "content": [{"type": "text", "text": "回應插話的第一段IJSTEPONE。"}]}},
        {"type": "assistant", "uuid": "f2c", "parentUuid": "f2b",
         "timestamp": "2026-07-24T03:00:25.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf2c",
                     "usage": {"input_tokens": 130, "output_tokens": 15},
                     "content": [{"type": "text", "text": "再下一步IJSTEPTWO。"}]}},
        # 第二次插話，而且**它後面沒有步驟了**（下一則是通知，會關掉這一輪）。
        # ⚠⚠ 沒有這一則，`render_turn_html` 結尾那道「還開著就收」的關閉標籤是**死碼**
        # ——縮排框永遠在下一個步驟就收掉了，漏收 `</div>` 不會有任何斷言變紅。
        {"type": "attachment", "uuid": "f2d", "timestamp": "2026-07-24T03:00:30.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command", "prompt": SENT_TAIL,
                        "commandMode": "prompt", "origin": {"kind": "human"},
                        "timestamp": "2026-07-24T03:00:30.000Z"}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-24T03:00:30.000Z", "sessionId": sid, "content": SENT_TAIL},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-24T03:00:35.000Z", "sessionId": sid, "content": SENT_TAIL},
        # --- ① 反例：撤回（popAll）⇒ **絕不可以**畫出來 ------------------------
        {"type": "attachment", "uuid": "f4", "timestamp": "2026-07-24T03:00:11.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command", "prompt": DROPPED, "commandMode": "prompt",
                        "origin": {"kind": "human"},
                        "timestamp": "2026-07-24T03:00:11.000Z"}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-24T03:00:11.000Z", "sessionId": sid, "content": DROPPED},
        {"type": "queue-operation", "operation": "popAll",
         "timestamp": "2026-07-24T03:00:12.000Z", "sessionId": sid, "content": DROPPED},
        # --- ① 反例其二：**系統注入的**通知也走 queued_command ＋ remove -------
        # ⚠⚠ 它和人打的提示詞唯一的差別就是 `origin.kind`（它沒有）。
        # 不看那一欄的話，全語料會多冒出 143 列幻影通知，而**上面每一條斷言都照樣綠**。
        {"type": "attachment", "uuid": "f4b", "timestamp": "2026-07-24T03:00:13.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command", "commandMode": "task-notification",
                        "prompt": PHANTOM, "timestamp": "2026-07-24T03:00:13.000Z"}},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-24T03:00:14.000Z", "sessionId": sid, "content": PHANTOM},
        # ⚠⚠ **`isMeta` 的通知**：v54 是雜訊、整則被丟掉、**不切輪也沒有錨點**。
        # 真實語料裡背景任務通知**幾乎全是這一種**（抽驗某檔 9 則全是）。
        # 沒有這一筆素材，「掛進回合內部、不佔序號」那條路一格都驗不到。
        {"type": "user", "uuid": "f2e", "parentUuid": "f2c", "isMeta": True,
         "timestamp": "2026-07-24T03:00:28.000Z", "sessionId": sid,
         "message": {"role": "user", "content":
                     f"<{NTAG}>\n<task-id>meta1</task-id>\n"
                     "<summary>INLINENOTIFY done</summary>\n"
                     f"<status>completed</status>\n</{NTAG}>"}},
        # --- ② 背景任務通知（系統以 user 身分注入）----------------------------
        {"type": "user", "uuid": "f5", "parentUuid": "f2",
         "timestamp": "2026-07-24T03:05:00.000Z", "sessionId": sid,
         "message": {"role": "user", "content": NOTIFY}},
        # --- ③ 指令＋其結果（下一則、同一時刻）；結果帶 ANSI 控制碼 -----------
        {"type": "user", "uuid": "f6", "parentUuid": "f5", "timestamp": T_CMD,
         "sessionId": sid,
         "message": {"role": "user", "content":
                     "<command-name>/effort</command-name>\n"
                     "<command-message>effort</command-message>\n"
                     "<command-args>high</command-args>"}},
        {"type": "user", "uuid": "f7", "parentUuid": "f6", "timestamp": T_CMD,
         "sessionId": sid,
         "message": {"role": "user", "content":
                     "<local-command-stdout>Set effort level to "
                     "\x1b[1mhigh\x1b[22m CMDOUTMARK</local-command-stdout>"}},
        # ⚠⚠ 三筆補洞素材（突變檢驗逐條點名「這條路沒有素材」）：
        # ⑥b 終端控制碼**不只有 CSI**：OSC（設視窗標題）與裸控制字元也要剝
        {"type": "user", "uuid": "f7b", "parentUuid": "f7",
         "timestamp": "2026-07-24T03:10:30.000Z", "sessionId": sid,
         "message": {"role": "user", "content":
                     "<command-name>/status</command-name>\n<command-args></command-args>"}},
        {"type": "user", "uuid": "f7c", "parentUuid": "f7b",
         "timestamp": "2026-07-24T03:10:30.000Z", "sessionId": sid,
         "message": {"role": "user", "content":
                     "<local-command-stdout>\x1b]0;視窗標題\x07OSCMARK"
                     "\x00\x08NULMARK"
                     # ⚠⚠ DCS（`ESC P … ESC \`）：只剝引導字元的話 **payload 會留下來**。
                     "\x1bPDCSPAYLOAD\x1b\\DCSMARK"
                     # ⚠⚠ **裸的 C1**（`\x80`–`\x9f`）：`_CTRL_RE` 只到 `\x7f` 的話剝不掉。
                     "\x80\x91\x99C1MARK"
                     "</local-command-stdout>"}},
        {"type": "assistant", "uuid": "f8", "parentUuid": "f7",
         "timestamp": "2026-07-24T03:11:00.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf8",
                     "usage": {"input_tokens": 90, "output_tokens": 10},
                     "content": [{"type": "text", "text": "第二段回覆SECONDREPLY。"}]}},
        # --- ①-b 反例：排隊，但**沒有開著的 assistant 回合**（真的跨輪那 2/127）------
        # 前一則是使用者自己的回合 ⇒ `cur` 是 None ⇒ 掛不進去 ⇒ 退回一般 user 回合。
        # ⚠ 這條路徑在真實語料只有 2 則，**靠語料驗不到**，只能自己造。
        {"type": "user", "uuid": "f9", "parentUuid": "f8",
         "timestamp": "2026-07-24T03:12:00.000Z", "sessionId": sid,
         "message": {"role": "user", "content": "第二個正常提問SECONDASK。"}},
        {"type": "attachment", "uuid": "f9b", "timestamp": "2026-07-24T03:12:10.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command", "prompt": SENT2, "commandMode": "prompt",
                        "origin": {"kind": "human"},
                        "timestamp": "2026-07-24T03:12:10.000Z"}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-24T03:12:10.000Z", "sessionId": sid, "content": SENT2},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-24T03:12:20.000Z", "sessionId": sid, "content": SENT2},
        # --- ③-b 指令的**第二種存法**：`type:system, subtype:local_command` ---------
        # ⚠⚠ 兩種存法**並存**（不是版本遷移）：全語料 `type:user` 575 則、
        # `system/local_command` 345 則（`/context`／`/status`／`/rename`／**`/rc`**…）。
        # 只做前者＝只做到 62.5%。
        {"type": "system", "subtype": "local_command", "uuid": "f9c",
         "timestamp": "2026-07-24T03:12:40.000Z", "sessionId": sid, "isMeta": False,
         "content": "<command-name>/remote-control</command-name>\n"
                    "            <command-message>remote-control</command-message>\n"
                    "            <command-args></command-args>"},
        # ⚠ 沒有輸出的指令會另寫一則**空的**結果事件。它畫不出東西，
        #   但如果不在解析端就丟掉，它會**佔掉一個回合**（`t{n}` 序號往後推）。
        {"type": "system", "subtype": "local_command", "uuid": "f9d",
         "timestamp": "2026-07-24T03:12:40.000Z", "sessionId": sid, "isMeta": False,
         "content": "<local-command-stdout></local-command-stdout>"},
        {"type": "assistant", "uuid": "f10", "parentUuid": "f9",
         "timestamp": "2026-07-24T03:13:00.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf10",
                     "usage": {"input_tokens": 80, "output_tokens": 10},
                     "content": [{"type": "text", "text": "第三段回覆THIRDREPLY。"}]}},

        # --- ③-c ⚠⚠ 「指令列 → 助手發言 → 指令輸出（**同時戳**）」----------------
        # ⚠ 前面一定要有一則**真的 user 發言**：它會關掉開著的 assistant 回合，
        #   下一則指令才會自成一列（`turns[-1]`）。少了它，指令會掛進開著的回合內部，
        #   於是 `turns[-1]` 不是那一列指令 ⇒ 突變改不動任何東西 ⇒ 這一格白造。
        {"type": "user", "uuid": "f10b", "timestamp": "2026-07-24T03:13:30.000Z",
         "sessionId": sid, "isMeta": False,
         "message": {"role": "user", "content": "再問一句CLOSER。"}},
        # 這一段素材**只會走到**「併回上一列」那條路上的 `cur is None` 那一格。
        # 沒有它的時候那一格被標成「已知到不了」而略過——實測**到得了**：
        # 同時戳的 assistant 事件可以依行序排在指令與它的輸出中間。
        # 少了 `cur is None`，輸出會被併回**前面那一列指令**，等於跨過中間的發言，
        # 畫面順序與時間順序不符。
        {"type": "user", "uuid": "f11", "timestamp": "2026-07-24T03:14:00.000Z",
         "sessionId": sid, "isMeta": False,
         "message": {"role": "user", "content":
                     "<command-name>/usage-credits</command-name>\n"
                     "<command-message>usage-credits</command-message>\n"
                     "<command-args></command-args>"}},
        {"type": "assistant", "uuid": "f12", "parentUuid": "f11",
         "timestamp": "2026-07-24T03:14:00.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf12",
                     "usage": {"input_tokens": 20, "output_tokens": 5},
                     "content": [{"type": "text", "text": "夾在中間的發言BETWEEN。"}]}},
        {"type": "user", "uuid": "f13", "timestamp": "2026-07-24T03:14:00.000Z",
         "sessionId": sid, "isMeta": False,
         "message": {"role": "user", "content":
                     "<local-command-stdout>CREDITSOUT 還有 3 塊</local-command-stdout>"}},

        # --- ①-c ⚠⚠ 排隊句落在該輪 `turn_duration` **之後**才被讀到 --------------
        # 那一輪已經結束了，所以它**不是**「中途插話」——但它仍然掛在那一輪內部
        # （改成獨立回合會切輪、位移區塊錨點，實測會弄丟 54 個舊錨點）。
        # ⇒ 能改的只有標籤。這一段素材**只會走到** `after_turn` 那一格。
        {"type": "assistant", "uuid": "f14", "parentUuid": "f13",
         "timestamp": "2026-07-24T03:15:00.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf14",
                     "usage": {"input_tokens": 30, "output_tokens": 8},
                     "content": [{"type": "text", "text": "這一輪的回覆TURNEND。"}]}},
        {"type": "system", "subtype": "turn_duration", "uuid": "f14d", "parentUuid": "f14",
         "timestamp": "2026-07-24T03:15:05.000Z", "sessionId": sid, "isSidechain": False,
         "isMeta": True, "durationMs": 5000, "messageCount": 2},
        {"type": "attachment", "uuid": "f15", "timestamp": "2026-07-24T03:15:20.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command", "prompt": LATE, "commandMode": "prompt",
                        "origin": {"kind": "human"},
                        "timestamp": "2026-07-24T03:15:20.000Z"}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-24T03:15:20.000Z", "sessionId": sid, "content": LATE},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-24T03:15:30.000Z", "sessionId": sid, "content": LATE},
        {"type": "assistant", "uuid": "f16", "parentUuid": "f14",
         "timestamp": "2026-07-24T03:15:40.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf16",
                     "usage": {"input_tokens": 35, "output_tokens": 9},
                     "content": [{"type": "text", "text": "回它那一句AFTERLATE。"}]}},

        # --- ①-d ⚠⚠ **下一輪又開始跑了**：這一句才是真正的「中途插話」-------------
        # 素材：`turn_duration`（①-c）→ **新的 assistant 內容**（f16 已經是了）→
        # 又一句排隊。`_turn_done` 若只設不清，這一句會被誤標成「上一輪結束後才讀到」，
        # 而它其實是在第二輪跑到一半插進來的（實測）。
        {"type": "attachment", "uuid": "f17", "timestamp": "2026-07-24T03:16:00.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command", "prompt": MID2, "commandMode": "prompt",
                        "origin": {"kind": "human"},
                        "timestamp": "2026-07-24T03:16:00.000Z"}},
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": "2026-07-24T03:16:00.000Z", "sessionId": sid, "content": MID2},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-24T03:16:05.000Z", "sessionId": sid, "content": MID2},
        {"type": "assistant", "uuid": "f18", "parentUuid": "f16",
         "timestamp": "2026-07-24T03:16:20.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mf18",
                     "usage": {"input_tokens": 40, "output_tokens": 10},
                     "content": [{"type": "text", "text": "回第二輪那一句AFTERMID2。"}]}},

        # --- ①-g ⚠⚠ **只貼了圖、一個字都沒打**的排隊句 -------------------------
        # 文字是空字串 ⇒ 若被兩道守衛各擋一次（`if p:` 與
        # `if not c:`），這一則就**整則不出現** ⇒ 使用者真的送出去的東西在頁面上完全沒有痕跡。
        # CLI 寫的 `prompt` 是「空的 text block ＋ 圖片」，`remove.content` 是**空字串**
        # （⚠ 不是整欄不見——那是另一回事，配對規則見產品碼註解）。
        # ⚠⚠ **位置刻意排在整場最後**：放進「插話 → 第一段 → 再下一段」那段素材中間，
        #   會讓突變 ⑱（B 改成 A）兩種畫法同形 ⇒ 那一格從「抓到」變「漏掉」。
        #   素材決定走到哪。
        {"type": "attachment", "uuid": "f19", "timestamp": "2026-07-24T03:16:30.000Z",
         "sessionId": sid,
         "attachment": {"type": "queued_command",
                        "prompt": [{"type": "text", "text": ""},
                                   {"type": "image", "source": {
                                       "type": "base64", "media_type": "image/png",
                                       "data": PNG1X1}}],
                        "imagePasteIds": ["paste-b"], "commandMode": "prompt",
                        "origin": {"kind": "human"},
                        "timestamp": "2026-07-24T03:16:30.000Z"}},
        {"type": "queue-operation", "operation": "remove",
         "timestamp": "2026-07-24T03:16:35.000Z", "sessionId": sid, "content": ""},
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out), "--format", "both"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    # ⚠⚠ **不可以直接 `[0]`。** 分析這一場時丟了例外的話，現在的行為是「印一行、跳過
    # 這一場」（退出碼仍是 0）⇒ 一個頁面都沒有 ⇒ 死在 `IndexError`，看不出發生什麼事。
    # 那種紅**不算守衛**——紅的理由是產品崩了，
    # 不是它宣稱守的那件事。把 stderr 帶進訊息裡，紅因才看得出來。
    _pages = list(_session_pages(out))
    assert _pages, ("這一場沒有產出任何頁面——多半是分析時丟了例外、那一場被跳過了。\n"
                    f"STDOUT:{r.stdout}\nSTDERR:{r.stderr}")
    page = _pages[0]
    html = page.read_text(encoding="utf-8")
    md = [p for p in (out / "sessions").rglob("*.md")][0].read_text(encoding="utf-8")

    # --- ① 排隊送出的那一句 -------------------------------------------------
    assert SENT in html, "被 remove 取走的排隊提示詞必須畫出來（否則答案在、問題不在）"
    assert SENT in md, "MD 也要有——`--search` 的全文索引讀的是這一份"
    assert DROPPED not in html, "popAll ＝ 撤回，畫出來就是一句使用者沒送出的幽靈重複句"
    assert DROPPED not in md, "撤回的提示詞也不可以進 MD 索引"
    # ⚠⚠ **系統注入的通知走同一條佇列路徑**，只差在沒有 `origin.kind`。
    # 不看那一欄就會多冒出一批幻影回合（全語料 143 則），而其他每一條斷言都照樣綠。
    assert "PHANTOMNOTIFY" not in html, (
        "沒有 origin.kind 的 queued_command 是**系統注入**的，不是使用者打的字，"
        "不可以補成 user 回合")
    assert "中途插話" in html, "要看得出來這是助手工作到一半時插進來的"
    # ⚠⚠ MD 有**兩條各自獨立的路**，要分開驗——只驗「md 裡有沒有『中途插話』」的話，
    # 拿掉任何一條，另一條都會讓斷言照樣綠：
    #   ① 回合內部的插話 → `> **👤 你 · 中途插話…**` 引用塊
    #   ② 真跨輪的那一種 → `###` 標頭上的 `· ⏳ 中途插話（… 送出）`
    _mdl = md.splitlines()
    assert any(ln.startswith("> **👤 你 · 中途插話") for ln in _mdl), (
        "回合內部的插話要進 MD——`--search` 的全文索引讀的是這一份，搜不到就等於沒有")
    assert any(ln.startswith("###") and "中途插話" in ln for ln in _mdl), (
        "跨輪那一種的 MD 記號在回合標頭上，那是另一條路，要分開守")

    # --- ①-a 助手還在跑的時候插話：**不可以切輪**，要畫成該輪內部的子框 ----
    # ⚠⚠ 依據是量測不是偏好：全語料 125/127（98.4%）的插話是在同一輪之內送達的。
    # 當成獨立 user 回合＝把一輪切成兩輪，畫面上變成「助手講完 → 使用者說話 →
    # 助手開新的一輪」，那是假的。
    assert 'class="ijbox"' in html, "同一輪之內的插話要畫成子框"
    assert 'class="ijafter"' in html, "插話後的第一段回應要縮排（B 畫法）"
    import ai_session_viewer as v
    s = v.load_session(proj / f"{sid}.jsonl", "demo-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s)
    host = [g for g in s.main_groups
            if any(b.get("type") == "_interject" for b in g["blocks"])]
    # ⚠ 兩則：①同一輪之內插進來的（SENT）、①-c 落在 `turn_duration` 之後才讀到的（LATE）。
    #   **兩者都掛在回合內部**——後者只是標籤不同，位置一樣（切輪會位移區塊錨點）。
    assert len(host) == 2, f"應該有兩則插話掛在回合內部，實得 {len(host)}"
    assert all(g["role"] == "assistant" for g in host), (
        "插話必須掛在**開著的那個 assistant 回合**裡面——掛成獨立回合就是把一輪切成兩輪")
    assert not any(g["role"] == "user" and any(SENT in (b.get("text") or "")
                                               for b in g["blocks"])
                   for g in s.main_groups), "插話不可以同時又變成一個獨立的 user 回合"
    # ⚠⚠ **空的「↳ 回應這句」框不可以存在。** 插話是那一輪最後一個區塊時
    # （他打完字、助手沒有再回任何東西），照樣開框的話 ⇒ 框頭在說謊。
    # ⚠ 這一格和「div 平衡」是**兩件事**：撤掉開啟標籤與補上關閉標籤，div 數都會平衡，
    #   所以只驗平衡的話，把 `_close_ij()` 的 `parts.pop()` 關掉不會有任何一格變紅。
    assert '<div class="ijafter"><div class="ijafterhead">↳ 回應這句</div></div>' not in html, (
        "頁面上出現了空的「↳ 回應這句」框——插話後面沒有任何回應時，那整個框要撤掉")
    # ⚠ `.ijafter` 是**手寫開合**的巢狀 div（不是包在單一 f-string 裡）⇒ 漏收就整頁走版。
    assert html.count("<div") == html.count("</div>"), (
        f"頁面 div 沒有平衡（<div {html.count('<div')} / </div> {html.count('</div>')}）"
        "——`.ijafter` 的關閉標籤漏了")

    # ⚠⚠ **範圍必須是「只縮第一段回應」**（B，不是 A）。
    # 掃 div 巢狀深度找出 `.ijafter` 真正的結束位置，再看誰在裡面、誰在外面。
    # 少了這一格，B 和 A 的產出在「插話後只有 0～1 個步驟」時完全同形，
    # 這個測試就會對「改成縮到那一輪結束」毫無反應。
    _s = html.index('<div class="ijafter">')
    _d, _i, _end = 0, _s, None
    while _i < len(html):
        _o, _c = html.find("<div", _i), html.find("</div>", _i)
        if _c < 0:
            break
        if 0 <= _o < _c:
            _d += 1
            _i = _o + 4
        else:
            _d -= 1
            _i = _c + 6
            if _d == 0:
                _end = _i
                break
    assert _end, "`.ijafter` 沒有對應的關閉標籤"
    _inside = html[_s:_end]
    assert "IJSTEPONE" in _inside, "插話後的第一段回應要在縮排框**裡面**"
    assert "IJSTEPTWO" not in _inside, (
        "再下一步要在縮排框**外面**——只縮第一段（B），不是縮到那一輪結束（A）")

    # --- ⚠⚠ 插話**不可以佔區塊序號**（既有區塊書籤逐字不變的唯一機械保證）------
    # 它被插進 blocks 中間；一旦算成可標記區塊，**同一步內它後面每一個區塊的序號 +1**
    # ⇒ 既有的 `-b3` 不會失效、不會退化，**它會指到別的內容**（最壞的失敗方向）。
    _bl = host[0]["blocks"]
    _anc = v.turn_block_anchors(host[0])
    _ij_i = next(i for i, b in enumerate(_bl) if b.get("type") == "_interject")
    assert not _anc[_ij_i], (
        "插話不可以拿到區塊錨點——它會把同一步內後面所有區塊的 -b<n> 往後推，"
        "既有的區塊書籤就安靜地指到別的內容了")

    # ⚠⚠ 上面那一格只驗「插話沒拿到」，**驗不到「別人有沒有被推移」**。
    # 這裡把同一份輸入拿掉插話再算一次，逐一比對其餘區塊的錨點。
    # 少了這一格，把 `_interject` 改回可標記區塊時**不會有任何一格變紅**
    # （實測：測試只驗它拿得到，沒有比對過別人）。
    _no_ij = dict(host[0])
    _no_ij["blocks"] = [b for b in _bl if b.get("type") != "_interject"]
    _anc_no = v.turn_block_anchors(_no_ij)
    _with = [a for b, a in zip(_bl, _anc)
             if a and b.get("type") not in ("_step", "_interject")]
    _without = [a for b, a in zip(_no_ij["blocks"], _anc_no)
                if a and b.get("type") != "_step"]
    assert _with == _without, (
        "插話讓其他區塊的錨點位移了——既有區塊書籤會指到別的內容\n"
        f"  有插話: {_with}\n  無插話: {_without}")

    # ⚠⚠ 上面兩格**都驗不到圖片那條路**：拿掉的只有 `_interject` 本身，圖片若被當成
    # 獨立區塊塞進同一輪，它在「有插話」與「無插話」兩邊都在 ⇒ 兩份錨點照樣相等。
    # 所以這裡直接查來源：**排隊句貼的圖片不可以出現在這一輪的 `blocks` 裡**，
    # 它必須待在 `_interject` 自己身上（`imgs`）。變成獨立區塊的話，同一步之後
    # 每一個 `-b<n>` 都會 +1，既有的區塊書籤安靜指到別的內容。
    assert not any(b.get("type") == "image" for b in _bl), (
        "排隊送出時貼的圖片變成了這一輪的獨立區塊——"
        "它會把同一步之後每個區塊的 -b<n> 往後推")
    assert any((b.get("imgs") or []) for b in _bl if b.get("type") == "_interject"), (
        "沒有任何一個插話子框帶著圖片——素材沒建起來，上面那一格什麼都沒驗到")

    # --- ①-f/①-g 端到端：**排隊時貼的圖真的畫出來了**，而且畫在子框裡面 -------
    # ⚠ 兩張：一張跟著有文字的那一句（f3），一張是只貼圖沒打字的那一則（f3b）。
    assert html.count('img class="msg-img"') == 2, (
        "排隊送出時貼的圖片沒有畫出來（或畫了不只兩張）——"
        f"實得 {html.count('img class=' + chr(34) + 'msg-img' + chr(34))} 張。"
        "若看到「資料無效／過大／格式不支援」佔位，那是素材的 base64 有問題")
    # ⚠ 比對的是 `class="img-ph"` 不是 `img-ph`：頁面的 CSS 裡本來就有 `.img-ph` 規則，
    #   只找後者的話這一格**永遠是紅的**。
    assert 'class="img-ph"' not in html, (
        "圖片畫成了佔位（資料無效／過大／格式不支援），不是真的內嵌——"
        "這一格若鬆掉，上面那條 `<img>` 計數會驗到另一件事")
    # ⚠⚠ **圖片必須在 `.ijbody` 之內**：跑到子框外面就代表它變成了獨立的可標記區塊，
    # 那正是會把同一步後面每個 `-b<n>` 往後推的做法。
    _ijb = html.find('class="ijbody"')
    assert _ijb > 0 and 'img class="msg-img"' in html[_ijb:_ijb + 4000], (
        "圖片沒有畫在插話子框裡面")
    # MD 那半：`--search` 讀的是它，圖片至少要留下記號
    assert md.count("_[圖片]_") >= 2, (
        f"MD 沒有留下圖片記號（實得 {md.count('_[圖片]_')} 個）——"
        "搜尋與 MD 匯出會看不出這一句帶了圖")

    # --- ①-b 真的跨輪的那一種：沒有開著的回合可掛 ⇒ 退回一般 user 回合＋徽章 --
    assert SENT2 in html, "跨輪送達的排隊提示詞一樣要畫出來"
    assert 'class="badge queued"' in html, "跨輪的那一種要有徽章（它沒有可以掛進去的回合）"
    assert html.count('class="badge queued"') == 1, (
        f"只有跨輪的那一則該有徽章，實得 {html.count('class=\"badge queued\"')} 個")
    assert "排隊送出於" in html, "按下 Enter 的時刻必須留在游標提示裡，時間軸才沒有說謊"
    assert any(g["role"] == "user" and g.get("queued_at")
               and any(SENT2 in (b.get("text") or "") for b in g["blocks"])
               for g in s.main_groups), "跨輪那一則應該是一個獨立的 user 回合"

    # --- ② 背景任務通知 -----------------------------------------------------
    assert 'details class="notify"' in html, "通知要收成一列可展開的摘要"
    assert "NOTIFYSUMMARY" in html, "摘要文字取自 <summary> 欄"
    # ⚠ 它**不是使用者的發言**：不可以套「👤 你」那個對話框
    nrow = re.search(r'<div class="turn metarow"[^>]*>(?:(?!</div>\s*<div class="turn).)*?'
                     r'details class="notify".*?(?=<div class="turn|</div></div></body>)',
                     html, re.DOTALL)
    assert nrow, "通知應該畫成 metarow（窄橫列），不是 turn user 對話框"
    assert "👤" not in nrow.group(0), "通知列不可以掛「👤 你」——那不是使用者說的話"

    # --- ③ 指令列 -----------------------------------------------------------
    assert "/effort" in html, "使用者下過的指令必須看得到"
    assert "CMDOUTMARK" in html, "指令的結果也要看得到"
    assert "\x1b" not in html and "[1m" not in html, "終端控制碼要剝掉，否則畫出來是亂碼"
    # ⚠⚠ **控制碼不只有 CSI。** 上一行的素材只有 `\x1b[1m`，所以它**驗不到**
    # OSC（`ESC ] … BEL`，`/status` 這類設視窗標題會用）與裸控制字元。
    # 「素材涵蓋到哪裡就是驗證能力的上限」——這兩格是補那個上限的。
    assert "OSCMARK" in html, "OSC 那一則沒有畫出來，下面兩格就沒有素材"
    assert "視窗標題" not in html, (
        "OSC 序列（ESC ] … BEL）沒有被剝掉——它會把終端的設定指令原樣寫進頁面")
    assert "\x00" not in html and "\x08" not in html, "裸控制字元要濾掉"
    # ⚠⚠ 控制字串的 **payload** 也要跟著不見（只剝引導字元＝把 payload 留在頁面上）
    assert "DCSMARK" in html, "DCS 那一則沒有畫出來，下面那格就沒有素材"
    assert "DCSPAYLOAD" not in html, (
        "DCS（ESC P … ESC \\）的 payload 留在頁面上了——控制字串要連內容一起吃掉")
    # ⚠⚠ **裸的 C1**（`\x80`–`\x9f`）：素材只有 OSC／NUL／BS 的話，`_CTRL_RE` 的
    #   範圍只寫到 `\x7f` 也全綠。突變 ⑥b 是把整條規則關掉，抓不到這種「只漏幾個」。
    assert "C1MARK" in html, "C1 那一則沒有畫出來，下面那格就沒有素材"
    _c1 = [hex(ord(ch)) for ch in html if 0x80 <= ord(ch) <= 0x9f]
    assert not _c1, f"裸的 C1 控制字元原樣進了頁面：{_c1[:8]}"
    assert "NULMARK" in html, "濾控制字元時把周圍的文字一起吃掉了"

    # ⚠ 「指令輸出跨過中間的助手發言併回上一列」**結構上到不了**：
    # 事件依 `(時刻, 檔案行序)` 排序，而指令輸出的時戳**恰好等於**指令
    # ⇒ 它永遠緊接在指令後面排，更晚的助手事件插不進中間。
    # 產品碼那道守衛（只認「上一個區塊就是它」＋時戳相同）留著當縱深，
    # 但**不要為它寫一條永遠不會綠的斷言**——素材造不出來（突變標「已知到不了」）。
    assert "/effort" in md, "指令名要進 MD，`--search` 才搜得到"
    # 結果**併回**指令那一列：同一個時刻只產生一列，不是兩列
    # --- ①-b ⚠⚠ 中間隔著助手發言的同時戳輸出**不可以**併回前面那一列指令 --------
    # 素材：`/usage-credits`（03:14:00）→ 助手「BETWEEN」（03:14:00）→ 輸出（03:14:00）。
    # 三者同時戳，所以「時間戳相同就併」會跨過中間的發言。
    # ⚠ 這一格守的是 `併回` 那段的 `cur is None`——它**不是結構上不可達**，
    #   走不到只是**素材沒走到**。
    # ⚠⚠ **必須排在下面那條「幾列指令」的斷言前面**：兩者都會因為同一個突變變紅，
    #   而先紅的那一條決定了突變檢驗看到的紅因。數量那條講不出「錯在哪」。
    _credits = [b for g in s.main_groups for b in g["blocks"]
                if b.get("type") == "_command" and b.get("name") == "/usage-credits"]
    assert len(_credits) == 1, f"`/usage-credits` 那一列指令不見了，實得 {len(_credits)}"
    assert "CREDITSOUT" not in (_credits[0].get("out") or ""), (
        "⚠⚠ 指令輸出被併回**跨過助手發言**的那一列指令了——畫面順序與時間順序不符。"
        "併回的條件必須是「上一個區塊就是它」，不是「時間戳一樣」")
    assert "CREDITSOUT" in html, "那一段指令輸出整個不見了（併不回去時要自成一列）"
    assert "BETWEEN" in html, "夾在中間的助手發言不見了——素材沒建起來，這一格等於沒驗"

    # ⚠ 七列：`/context`（開頭）、`/statusline`（指令＋結果同一則）、
    #   `/effort`（結果在下一則、**必須併回**）、`/status`（OSC 素材）、
    #   `/remote-control`（system 存法）、`/usage-credits`（③-c）、
    #   以及 ③-c 那一則**併不回去**的輸出（中間隔著助手發言 ⇒ 自成一列，那是對的）。
    #   ⚠ `/effort` 與 `/status` 的結果各自併回自己那一列，所以那兩個不另計。
    assert html.count('<span class="cmdmark">⬚</span>') == 7, (
        f"應該恰好七列指令，實得 {html.count(chr(0x2B1A))} 列。"
        "指令與其結果必須併成一列——多一列就多一個同時間戳的回合，"
        "撞號組成員一變就可能換人拿無後綴的耐久錨點")

    # --- ② vs ③：錨點政策（既有書籤逐字不變的唯一機械保證）------------------
    cmd_rows = [g for g in s.main_groups
                if any(b.get("type") == "_command" for b in g["blocks"])]
    # ⚠ 只算**自成一列**的通知：`isMeta` 那種掛在 assistant 回合內部（`inline`），
    # 不加這個條件的話，宿主那一輪也會被算成「一列通知」。
    nfy_rows = [g for g in s.main_groups
                if any(b.get("type") == "_notify" and not b.get("inline")
                       for b in g["blocks"])]
    # ⚠⚠ 指令有**兩種存法**：`type:user`（`/effort`）與 `system/local_command`（`/remote-control`）。
    # 只驗一種的話，另一種整批消失也不會有任何一格變紅。
    assert "/remote-control" in html, (
        "存在 `system/local_command` 的指令也要畫出來——那是指令的另一半存法，"
        "`/context`／`/status`／`/rename`／`/rc` 都走這條")
    assert "/remote-control" in md, "第二種存法的指令也要進 MD"
    assert len(cmd_rows) == 7, (
        f"七個帶指令的回合：六列自成一列的（/context、/statusline、/effort、/status、"
        f"/remote-control、/usage-credits），＋一個 assistant 回合內部掛著 ③-c 那一則"
        f"**併不回去**的輸出。實得 {len(cmd_rows)}")
    # ⚠ ③-c 那一則輸出落在**開著的 assistant 回合內部**，不是自成一輪——
    # 「不併回去」與「不多一個回合」兩件事可以同時成立，撞號池因此不變。
    _ijrow = [g for g in cmd_rows if g["role"] == "assistant"]
    assert len(_ijrow) == 1 and len([b for b in _ijrow[0]["blocks"]
                                     if b.get("type") == "_command"]) == 1, (
        "③-c 併不回去的那一則輸出應該掛在開著的 assistant 回合內部，"
        f"實得 {[(g['role'], sum(1 for b in g['blocks'] if b.get('type') == '_command')) for g in cmd_rows]}")
    # ⚠ 這裡不放「不可以有 blocks 為空的 user 回合」這條——那是**恆真**的：
    # `group_turns` 建 user 回合的兩條路徑，一條要求 `special` 非空、另一條要求
    # `blocks` 非空，所以那種回合**不可能存在**。它名義上要守的事其實是被下面
    # 兩條守住的（M1 突變確實紅在 `len(cmd_rows)`）。恆真的斷言＝裝飾品。
    assert all(any(b.get("name") or b.get("args") or b.get("out")
                   for b in g["blocks"] if b.get("type") == "_command")
               for g in cmd_rows), "三格全空的指令列不該存在（它畫不出東西卻佔一個回合）"
    assert len(nfy_rows) == 2, (
        f"兩列通知（一般的＋內文含 `<command-name>` 的那一則），實得 {len(nfy_rows)}")
    assert not cmd_rows[0].get("kanchor"), (
        "指令列**不可以**拿耐久錨點：它是這一版才出現的回合，發了錨點就等於"
        "往撞號池裡塞新成員，既有書籤可能安靜改指到別輪")
    # ⚠⚠ **通知列的「區塊」錨點也要保住**（v55 之前它是普通 user 回合的 text 區塊，
    # `wrap_block()` 會給它 id 與 ☆）。只驗回合錨點的話，區塊錨點整批消失不會有人發現。
    _nb = [(b, a) for g in nfy_rows for b, a in zip(g["blocks"], v.turn_block_anchors(g))
           if b.get("type") == "_notify"]
    assert _nb and all(a for _, a in _nb), (
        f"自成一列的通知要有區塊錨點，實得 {[a for _, a in _nb]}")
    # 反方向：掛在回合內部的通知（v55 之前是雜訊、**根本沒有錨點**）不可以佔序號
    _inline_nb = [a for g in s.main_groups
                  for b, a in zip(g["blocks"], v.turn_block_anchors(g))
                  if b.get("type") == "_notify" and b.get("inline")]
    assert not any(_inline_nb), (
        "掛在回合內部的通知拿到了區塊錨點——它會把同一步後面的區塊往後推，"
        f"實得 {_inline_nb}")
    assert nfy_rows[0].get("kanchor"), (
        "通知列**必須**保留耐久錨點：它在這一版之前就是普通 user 回合、本來就標得到書籤，"
        "拿掉會讓標在通知上的既有書籤斷掉（實測全語料會少 872 個）")

    # --- 標題：指令與通知都不可以變成 session 標題 ---------------------------
    # ⚠ 素材的**第一則就是 `/context`** ⇒ 這一格真的走到 `first_user_text()` 那道守衛。
    # （第一則若是真提問，標題本來就對，那道守衛就是死斷言、突變不會紅。）
    assert "FIRSTASK" in s.title, f"標題應取第一句真正的提問，實得 {s.title!r}"
    assert "/context" not in s.title and "/effort" not in s.title \
        and "task-id" not in s.title, "指令與通知都不是使用者在講的事，不可以當標題"

    # --- ① 指令與結果寫在同一則：`parse_command()` 一定要先比 name --------------
    _same = [b for g in s.main_groups for b in g["blocks"]
             if b.get("type") == "_command" and b.get("name") == "/statusline"]
    assert len(_same) == 1, f"同一則裡的指令要解析成一個 _command，實得 {len(_same)}"
    assert _same[0].get("args") == "ARGSAME" and _same[0].get("out") == "OUTSAME", (
        "指令名／參數／結果寫在同一則時三格都要拿到——先比 `out` 會把指令名丟掉，"
        f"實得 {_same[0]!r}")

    # --- ①-c ⚠⚠ `turn_duration` 之後才讀到的排隊句：標籤不可以說「中途插話」-------
    _late = [b for g in s.main_groups for b in g["blocks"]
             if b.get("type") == "_interject" and "QUEUEDLATE" in (b.get("text") or "")]
    assert len(_late) == 1, (
        f"落在 turn_duration 之後的排隊句應該仍然掛在那一輪內部（不切輪），實得 {len(_late)}")
    assert _late[0].get("after_turn") is True, (
        "⚠⚠ 這一句是在那一輪 `turn_duration` **之後**才被讀到的，"
        "`after_turn` 必須為真——`cur` 在 turn_duration 之後仍然開著，"
        "不標記的話它會被寫成「中途插話」，而助手當時早就停了")
    assert "這一輪結束後才讀到" in html, (
        "`after_turn` 的那一種要改口叫「這一輪結束後才讀到」，不可以照寫「中途插話」")
    assert "這一輪結束後才讀到" in md, "MD 那半也要改口（兩邊共用同一個判準）"
    _mid = [b for g in s.main_groups for b in g["blocks"]
            if b.get("type") == "_interject" and "QUEUEDSENT" in (b.get("text") or "")]
    assert _mid and not _mid[0].get("after_turn"), (
        "真的在跑到一半插進來的那一句 `after_turn` 必須是假的——"
        "兩種都標成真就等於這個欄位沒有資訊")
    # ⚠⚠ ①-d：`turn_duration` **之後又有新的 assistant 內容** ⇒ 下一輪已經在跑了，
    # 這一句是真正的中途插話。`_turn_done` 只設不清的話它會被誤標。
    _mid2 = [b for g in s.main_groups for b in g["blocks"]
             if b.get("type") == "_interject" and "QUEUEDMID2" in (b.get("text") or "")]
    assert len(_mid2) == 1, f"①-d 那一句插話不見了，實得 {len(_mid2)}——素材沒建起來"
    assert not _mid2[0].get("after_turn"), (
        "⚠⚠ `turn_duration` 之後又有新的 assistant 內容 ⇒ **下一輪已經在跑**，"
        "這一句是真的中途插話，不可以標成「上一輪結束後才讀到」。"
        "`_turn_done` 要在新的 assistant 內容抵達時清掉，不是只設不清")

    # --- ①-e ⚠⚠ 排隊句配對的兩個最小反例（**函式層**，只驗 `synth_queued_user_events`）
    # 兩者都要有一格在守：
    # **沒有測試守的話**，產品碼對這兩個形狀處理錯了，測試照樣全綠。
    # ⚠ 走函式層是刻意的：這兩個形狀要控制 `_srcf` 與事件行序，用整個 CLI 造很囉唆，
    #   而要守的東西就在這一支函式裡。
    def _qev(i, srcf, **kw):
        e = {"_i": i, "_srcf": srcf, "_dt": v.parse_ts(kw.get("timestamp"))}
        e.update(kw)
        return e

    _T = "同一句話QREPEAT。"
    _reuse = [
        _qev(0, "", type="attachment", timestamp="2026-08-25T10:00:01.000Z",
             attachment={"type": "queued_command", "prompt": _T, "commandMode": "prompt",
                         "origin": {"kind": "human"}, "timestamp": "2026-08-25T10:00:01.000Z"}),
        _qev(1, "", type="queue-operation", operation="remove", content=_T,
             timestamp="2026-08-25T10:00:02.000Z"),
        # 第一次由**真的 user 事件**代表（撤回後重送那一種）⇒ 不合成
        _qev(2, "", type="user", uuid="qr1", timestamp="2026-08-25T10:00:03.000Z",
             message={"role": "user", "content": _T}),
        # 第二次又排隊送出同一句：**要合成，而且要配自己那一次的按 Enter 時刻**
        _qev(3, "", type="attachment", timestamp="2026-08-25T10:00:10.000Z",
             attachment={"type": "queued_command", "prompt": _T, "commandMode": "prompt",
                         "origin": {"kind": "human"}, "timestamp": "2026-08-25T10:00:10.000Z"}),
        _qev(4, "", type="queue-operation", operation="remove", content=_T,
             timestamp="2026-08-25T10:00:11.000Z"),
    ]
    _got = v.synth_queued_user_events(_reuse)
    assert len(_got) == 1, (
        f"第一次由真 user 事件代表、第二次要合成，實得 {len(_got)} 則")
    assert _got[0]["_queued_at"] == "2026-08-25T10:00:10.000Z", (
        "⚠⚠ 第二次送出拿到了**第一次**的按 Enter 時刻——時間軸在說謊。"
        "每個對得上的 remove 都要消耗一個 attachment 時刻，不論最後由誰呈現。"
        f"實得 {_got[0]['_queued_at']!r}")
    _cross = [
        # 附件在**子代理的轉錄檔**裡
        _qev(0, "sub/agent.jsonl", type="attachment", timestamp="2026-08-25T11:00:00.000Z",
             attachment={"type": "queued_command", "prompt": "跨來源QXSRC。",
                         "commandMode": "prompt", "origin": {"kind": "human"},
                         "timestamp": "2026-08-25T11:00:00.000Z"}),
        # `remove` 在**主檔**裡 ⇒ 兩者不是同一件事，不可以配對
        _qev(1, "", type="queue-operation", operation="remove", content="跨來源QXSRC。",
             timestamp="2026-08-25T11:00:05.000Z"),
    ]
    assert v.synth_queued_user_events(_cross) == [], (
        "⚠⚠ 附件在子代理檔、remove 在主檔，卻在主對話合成出一句話——"
        "配對的鍵一定要帶來源檔")

    # --- ①-f ⚠⚠ **`prompt` 是 block 陣列**：排隊送出時**貼了圖片**的那一種。
    # 處理不好的後果不是「畫錯」，是**整支工具當場崩掉**
    # （`AttributeError: 'list' object has no attribute 'strip'`，
    #  321 個 session 檔一個頁面都沒產出）。本機語料 588 則全是字串 ⇒ **只有合成素材守得到**。
    # ⚠ `queue-operation` 那半**仍然是字串**：CLI 只在佇列項的值是字串時才記 `content`，
    #   而貼上的圖片是另一欄帶的 ⇒ 文字對得上、這一句補得回來。
    _IMG = "貼了圖片的排隊句QIMGBLOCK。"
    _B64 = "iVBORw0KGgoAAAANSUhEUg"
    _blocks = [
        _qev(0, "", type="attachment", timestamp="2026-08-25T12:00:00.000Z",
             attachment={"type": "queued_command",
                         "prompt": [{"type": "text", "text": _IMG},
                                    {"type": "image",
                                     "source": {"type": "base64",
                                                "media_type": "image/png", "data": _B64}}],
                         "commandMode": "prompt", "origin": {"kind": "human"},
                         "imagePasteIds": ["pasteid1"],
                         "timestamp": "2026-08-25T12:00:00.000Z"}),
        _qev(1, "", type="queue-operation", operation="remove", content=_IMG,
             timestamp="2026-08-25T12:00:04.000Z"),
    ]
    _gotb = v.synth_queued_user_events(_blocks)
    assert len(_gotb) == 1, (
        "⚠⚠ `prompt` 是 block 陣列的排隊句沒有被補回來（實得 "
        f"{len(_gotb)} 則）——處理不好時這裡是 AttributeError，整支工具崩在這一行")
    # ⚠ 補出來的內容＝「文字 block ＋ 圖片 block」，和真的 user 事件貼圖時同形。
    _cb = _gotb[0]["message"]["content"]
    assert isinstance(_cb, list) and [b.get("type") for b in _cb] == ["text", "image"], (
        f"帶圖的排隊句應該補成 [文字, 圖片]，實得 {_cb!r}")
    assert _cb[0]["text"] == _IMG, f"文字那半不對：{_cb[0]!r}"
    assert _cb[1]["source"]["data"] == _B64, "貼上的圖片沒有被帶進來"
    assert _gotb[0]["_queued_at"] == "2026-08-25T12:00:00.000Z", (
        "block 陣列那一則的按 Enter 時刻沒有被記到——`human` 裡根本沒收到它")
    # ⚠⚠ 取**文字**時只收 `type:"text"` 的 block：把非文字 block 也吐出來的話，
    # 一坨 base64 會被當成使用者說的話拿去和 `remove.content` 比對（配不到）。
    assert v.content_text(_blocks[0]["attachment"]["prompt"]) == _IMG, (
        "⚠⚠ `content_text()` 把非文字 block 也吐出來了——"
        f"實得 {v.content_text(_blocks[0]['attachment']['prompt'])[:60]!r}")
    assert _B64 not in v.content_text(_gotb[0]["message"]["content"]), (
        "圖片的 base64 被當成文字了")

    # --- ①-g ⚠⚠ **只貼圖、一個字都沒打**：文字是空字串，`remove.content` 也是空字串。
    # 若被 `if p:` 擋掉、`if not c:` 又擋掉一次，這一則**整則不出現**。
    _only = [
        _qev(0, "", type="attachment", timestamp="2026-08-25T13:00:00.000Z",
             attachment={"type": "queued_command",
                         "prompt": [{"type": "text", "text": ""},
                                    {"type": "image",
                                     "source": {"type": "base64",
                                                "media_type": "image/png", "data": _B64}}],
                         "commandMode": "prompt", "origin": {"kind": "human"},
                         "imagePasteIds": ["pasteid2"],
                         "timestamp": "2026-08-25T13:00:00.000Z"}),
        _qev(1, "", type="queue-operation", operation="remove", content="",
             timestamp="2026-08-25T13:00:03.000Z"),
    ]
    _goto = v.synth_queued_user_events(_only)
    assert len(_goto) == 1 and [b.get("type") for b in _goto[0]["message"]["content"]] == ["image"], (
        f"只貼圖沒打字的排隊句沒有被補回來（實得 {_goto!r}）——空文字不代表沒送出東西")

    # ⚠⚠ **`content` 整欄不存在的 `remove` 不可以配上去。** 本機 574 個 `remove` 有
    # 49 個是這樣（佇列項的值不是字串，CLI 整欄不寫），它們都不是人打的排隊句；
    # 把「沒有 content」和「content 是空字串」當成同一件事，就會拿沒關係的 remove
    # 去配一則帶圖的排隊句，憑空畫出一張圖。
    _nocontent = [
        _only[0],
        _qev(1, "", type="queue-operation", operation="remove",
             timestamp="2026-08-25T13:00:03.000Z"),
    ]
    assert v.synth_queued_user_events(_nocontent) == [], (
        "⚠⚠ `remove` 連 `content` 欄都沒有，卻拿它配了一則排隊句——"
        "「整欄不存在」和「空字串」是兩件事")

    # --- ①-h ⚠⚠ **崩潰不可以被換成靜默**：`prompt` 形狀認不得時要出聲 ---------
    # 只把 `AttributeError` 擋掉的話，`prompt` 漂成 dict／數字／list of str 時三條路都回空
    # ⇒ 這一則排隊句連 `human` 都進不去、頁面上又變回「答案在、問題不在」。
    # ⚠ Codex 那一側有 13 條這種漂移哨兵，Claude 這一側也要有。
    import contextlib as _ctx
    for _bad_prompt, _tag in (({"text": "hi"}, "dict"), (42, "int"), (["hi"], "list of str"),
                              # ⚠ `type` 對得上、但 `text` 欄搬走了
                              ([{"type": "text", "content": "lost"}], "text 欄搬走"),
                              # `text` 在、但空著，字在別欄／包進巢狀——取不出文字又不出聲的同一種
                              ([{"type": "text", "text": "", "content": "hidden prompt"}], "text 空著、字在別欄"),
                              ([{"type": "text", "text": "",
                                 "content": [{"type": "text", "text": "hidden"}]}], "字包進巢狀"),
                              ([{"type": "text", "text": "", "content": ["hidden"]}], "字放在字串清單")):
        _ev = [
            _qev(0, "", type="attachment", timestamp="2026-08-25T14:00:00.000Z",
                 attachment={"type": "queued_command", "prompt": _bad_prompt,
                             "commandMode": "prompt", "origin": {"kind": "human"},
                             "timestamp": "2026-08-25T14:00:00.000Z"}),
            _qev(1, "", type="queue-operation", operation="remove", content="隨便",
                 timestamp="2026-08-25T14:00:03.000Z"),
        ]
        _buf = io.StringIO()
        with _ctx.redirect_stderr(_buf):
            v.synth_queued_user_events(_ev, "fake.jsonl")
        assert "形狀認不得" in _buf.getvalue(), (
            f"`prompt` 是 {_tag} 時整則被靜默丟掉、沒有任何哨兵出聲——"
            f"崩潰換成靜默是更糟的失效。實得：{_buf.getvalue()!r}")
    # 對照：只貼了圖、文字欄是空的——那是正常形狀，不可以出聲
    _ok = [
        _qev(0, "", type="attachment", timestamp="2026-08-25T14:00:00.000Z",
             attachment={"type": "queued_command", "commandMode": "prompt", "origin": {"kind": "human"},
                         "prompt": [{"type": "text", "text": ""},
                                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                 "data": "iVBORw0KGgo="}}],
                         "timestamp": "2026-08-25T14:00:00.000Z"}),
    ]
    _buf = io.StringIO()
    with _ctx.redirect_stderr(_buf):
        v.synth_queued_user_events(_ok, "fake.jsonl")
    assert "形狀認不得" not in _buf.getvalue(), f"只貼圖的正常形狀不該出聲：{_buf.getvalue()!r}"

    # --- ①-i ⚠⚠ `remove` 比 attachment 多時：時刻可以沿用，**圖片不可以** ------
    # 沿用是為了「不要整句不畫」；但那一筆帶著圖，照抄的話同一張使用者只貼過一次的圖
    # 會被畫第二次——性質從「時間軸不準」變成「多畫了他沒送出的東西」。
    _T2 = "沿用時刻QREUSE。"
    _reuse2 = [
        _qev(0, "", type="attachment", timestamp="2026-08-25T15:00:00.000Z",
             attachment={"type": "queued_command",
                         "prompt": [{"type": "text", "text": _T2},
                                    {"type": "image",
                                     "source": {"type": "base64",
                                                "media_type": "image/png", "data": _B64}}],
                         "commandMode": "prompt", "origin": {"kind": "human"},
                         "timestamp": "2026-08-25T15:00:00.000Z"}),
        _qev(1, "", type="queue-operation", operation="remove", content=_T2,
             timestamp="2026-08-25T15:00:02.000Z"),
        _qev(2, "", type="queue-operation", operation="remove", content=_T2,
             timestamp="2026-08-25T15:00:04.000Z"),
    ]
    _got2 = v.synth_queued_user_events(_reuse2)
    assert len(_got2) == 2, f"兩個 remove 應該各補一則，實得 {len(_got2)}"
    assert isinstance(_got2[0]["message"]["content"], list), "第一則要帶圖"
    assert _got2[1]["message"]["content"] == _T2, (
        "⚠⚠ 第二個 `remove` 沿用了最後一筆的**圖片** ⇒ 同一張只貼過一次的圖被畫了兩次。"
        f"時刻可以沿用，圖片不行。實得：{_got2[1]['message']['content']!r}")

    # --- ①-j ⚠⚠ 指令包裝 ＋ 貼圖：**不可以因為那張圖就切輪** ------------------
    # 實測：圖片進了 `rest` ⇒ `not rest` 失效 ⇒ 自成一輪 ⇒ 那一輪被切成
    # 兩半、同一步後面的區塊錨點整批改指。可達性目前 0，但失敗方向是最壞的那一種。
    def _cmdimg_groups(with_img):
        _blocks = [{"type": "text", "text": "<command-name>/effort</command-name>\n"
                                            "<command-args>high</command-args>"}]
        if with_img:
            _blocks.append({"type": "image", "source": {"type": "base64",
                                                        "media_type": "image/png", "data": _B64}})
        _evs = [
            _qev(0, "", type="assistant", timestamp="2026-08-25T16:00:00.000Z",
                 message={"role": "assistant", "model": "m", "id": "mm1",
                          "content": [{"type": "text", "text": "第一段CMDIMGA。"}]}),
            _qev(1, "", type="user", uuid="ci1", timestamp="2026-08-25T16:00:05.000Z",
                 message={"role": "user", "content": _blocks}),
            _qev(2, "", type="assistant", timestamp="2026-08-25T16:00:10.000Z",
                 message={"role": "assistant", "model": "m", "id": "mm2",
                          "content": [{"type": "text", "text": "第二段CMDIMGB。"}]}),
        ]
        return v.group_turns(_evs, per_step=True)

    _g0, _g1 = _cmdimg_groups(False), _cmdimg_groups(True)
    assert len(_g0) == len(_g1), (
        "⚠⚠ 指令包裝那一則**多了一張圖就多切了一輪** ⇒ 那一輪被切成兩半、"
        f"同一步後面的區塊錨點整批改指。無圖 {len(_g0)} 輪 vs 有圖 {len(_g1)} 輪")

    # --- ② 通知文字裡含 `<command-name>`：一定要判成通知，不是指令 --------------
    assert "NOTIFYWINS" in html, "這一則通知沒有被畫出來，下面的斷言就沒有素材"
    _wrong = [b for g in s.main_groups for b in g["blocks"]
              if b.get("type") == "_command" and "fake" in (b.get("name") or "")]
    assert not _wrong, (
        "通知的內文裡出現 `<command-name>` 時被判成指令了——"
        "`user_special_blocks()` 必須先判通知")

    print("OK: user turn fidelity (queued / notify / command) test passed")


def test_codex_sentinel_false_alarms(tmp_path=None):
    """兩個哨兵**不可以對完全正常的資料出聲**，但真的漏收仍然要出聲。

    下面兩種哨兵訊息對完全正常的資料都是**誤報**
    （工具跑完了、頁面也產出了，stderr 卻吵起來）：

    | 哨兵 | 它以為 | 實際上 |
    |---|---|---|
    | 「N 則助理訊息有 content 卻抽不出文字」 | 承載文字的元素型別改名了 | `[{"type":"output_text","text":""}]`——**型別沒改名、欄位也在，就是那一則沒有話講**（`phase=commentary`／`final_answer`）|
    | 「N 個回合開始了卻沒收到任何使用者 prompt」 | 使用者那一側有東西沒解析出來 | 兩個窗都是**系統自己起的**：一個自動壓縮（外層 `compacted` ＋ `context_compacted`）、一個壓縮失敗（只留一個 `error`）|

    ⚠⚠ **這兩格在本機語料上永遠是 0**（565 份 rollout、729 個回合窗裡零個空窗），
    所以「實測 0 誤報」那句話在這台機器上是恆真的，什麼都沒證明。

    ⚠ 誤報比漏報更貴：這幾條哨兵的全部價值在「它一出聲就是真的」，
    對正常資料吵一次，下次真的漂移時就沒有人會讀那一行。
    ⇒ 所以這一支同時驗**兩個方向**：正常資料要安靜，真的漏收要出聲。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    tmp = new_tmp(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-09-01T06:{sec // 60:02d}:{sec % 60:02d}.000Z",
                           "type": typ, "payload": payload}, ensure_ascii=False)

    HEAD = [line(0, "session_meta", {"id": "019f0090-0000-7000-8000-0000000000a1",
                                     "cwd": "/x/Sentinel", "cli_version": "0.147.0"}),
            line(1, "turn_context", {"cwd": "/x/Sentinel", "model": "gpt-5.6"})]

    def run(rows, name):
        f = tmp / f"rollout-2026-09-01T06-00-00-{name}.jsonl"
        f.write_text("\n".join(HEAD + rows), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            s = v.load_codex_session(f)
        n_ai = sum(1 for e in s.events if e.get("type") == "assistant")
        return n_ai, buf.getvalue()

    def turn(sec, prompt=True, text="助理回答SENTA。"):
        rows = [line(sec, "event_msg", {"type": "task_started", "turn_id": f"t-{sec}"})]
        if prompt:
            rows.append(line(sec + 1, "event_msg",
                             {"type": "user_message", "message": "使用者問句SENTQ。"}))
        if text is not None:
            rows.append(line(sec + 2, "response_item",
                             {"type": "message", "role": "assistant", "phase": "final_answer",
                              "content": [{"type": "output_text", "text": text}]}))
        rows.append(line(sec + 3, "event_msg", {"type": "task_complete"}))
        return rows

    # --- ① 真的空的助理訊息：**不是漂移**，不可以出聲 -----------------------
    empty_msg = HEAD and turn(10) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "phase": "commentary",
                                   "content": [{"type": "output_text", "text": ""}]}),
    ]
    n_ai, err = run(empty_msg, "emptymsg")
    assert n_ai == 1, f"前提：有話講的那一則要收得到（實得 {n_ai} 則助理內容）"
    assert "抽不出文字" not in err, (
        "⚠⚠ `[{\"type\":\"output_text\",\"text\":\"\"}]` 是**真的沒有話講**，不是型別改名——"
        f"對正常資料出聲會讓這條哨兵失去全部價值。實得：{err!r}")

    # --- ②（反向）元素型別真的改名時，**照樣要出聲** ------------------------
    # ⚠ 少了這一格，上面那條可以用「整條哨兵拿掉」來滿足，而那不是修好，是關掉。
    renamed = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "text_out", "text": "改名了SENTR。"}]}),
    ]
    _n, err = run(renamed, "renamed")
    assert "抽不出文字" in err, (
        f"content 元素型別改名時哨兵沒出聲——那正是它唯一看得見的地方。實得：{err!r}")

    # --- ③ 自動壓縮那一輪：系統自己起的，沒有 prompt 是正常的 ---------------
    # ⚠ 外層 `compacted` 的 payload **沒有 `type`**（是 `{message, replacement_history}`），
    #   所以只能認外層那個名字。
    # ⚠⚠ **這個窗刻意排在中間**（後面還有一個正常回合）：空窗有兩處計數——
    #   下一個 `task_started` 到來時那一次（這裡走的）與收尾那一次（④ 走的）。
    #   兩處都要有素材，只顧一邊的話另一邊改壞了不會有任何一格變紅。
    compact_mid = turn(10) + [
        line(30, "event_msg", {"type": "task_started", "turn_id": "t-30"}),
        json.dumps({"timestamp": "2026-09-01T06:00:31.000Z", "type": "compacted",
                    "payload": {"message": "（壓縮摘要）", "replacement_history": []}},
                   ensure_ascii=False),
        line(32, "event_msg", {"type": "context_compacted"}),
        line(33, "event_msg", {"type": "task_complete"}),
    ] + turn(40)
    _n, err = run(compact_mid, "compact")
    assert "回合開始了卻沒收到" not in err, (
        f"自動壓縮那一輪是系統自己起的，沒有 prompt 是正常的。實得：{err!r}")
    # ⚠ 同一件事的**收尾那次計數**：壓縮窗排在檔尾。兩處各改各的，只驗中間那格會漏一半
    #   （沒有檔尾這一格，突變 ㉜b 就抓不到）。
    _n, err = run(compact_mid[:-4], "compacttail")
    assert "回合開始了卻沒收到" not in err, (
        f"壓縮窗排在檔尾時，收尾那一次計數也要認得它。實得：{err!r}")

    # --- ④ 壓縮失敗：只留下一個 `error`，同樣不可以吵 -----------------------
    err_tail = turn(10) + [
        line(30, "event_msg", {"type": "task_started", "turn_id": "t-30"}),
        line(31, "event_msg", {"type": "error", "message": "Error running remote compact task"}),
        line(32, "event_msg", {"type": "task_complete"}),
    ]
    _n, err = run(err_tail, "errturn")
    assert "回合開始了卻沒收到" not in err, (
        f"那一輪失敗了（`error`），和被中止的回合同一類。實得：{err!r}")

    # --- ⑥ 沒有 `type`、只有空白文字：抽取器**支援這個形狀且真的抽到了** ------
    # ⚠ `_codex_content_text()` 有一支 `not item.get("type") and item.get("text")`，
    #   `[{"text":"   "}]` 走的就是它 ⇒ 說「抽不出文字（型別可能改名了）」是假的。
    typeless = turn(10) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"text": "   "}]}),
    ]
    _n, err = run(typeless, "typeless")
    assert "抽不出文字" not in err, (
        "⚠ 沒有 `type`、只有空白文字的 block 是抽取器**明確支援**的形狀，"
        f"哨兵不該說它「型別可能改名了」。實得：{err!r}")

    # --- ⑦（反向）內容被搬到別的欄位：型別沒改、`text` 還是字串 ⇒ **要出聲** --
    # ⚠⚠ 這是「會被新判準吞掉」的形狀。只看型別的話它完全靜默，
    #    而那一則的文字**整批消失**。
    moved = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "",
                                                "content": "真正的回答SENTMOVED。"}]}),
    ]
    _n, err = run(moved, "moved")
    assert "抽不出文字" in err, (
        "⚠⚠ 內容被搬到別的欄位（`text` 空著、真文字在別欄）⇒ 那一則會整批消失，"
        f"哨兵一定要出聲。實得：{err!r}")

    # --- ⑧（反向）`error` 那一窗**有助理內容** ⇒ 是真的漏收，不可以豁免 -------
    # ⚠⚠ 實測的形狀：prompt 事件改名（沒收到）→ 助理照常回答 → `error`。
    #    把任何 `error` 都當成「系統自己起的」的話，這種**真的漏收**會完全靜默。
    err_with_ai = turn(10) + [
        line(30, "event_msg", {"type": "task_started", "turn_id": "t-30"}),
        line(31, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text",
                                                "text": "沒人問就答SENTERRAI。"}]}),
        line(32, "event_msg", {"type": "error", "message": "stream disconnected"}),
        line(33, "event_msg", {"type": "task_complete"}),
    ]
    _n, err = run(err_with_ai + turn(40), "errai")      # 空窗排在中間 ⇒ 迴圈裡那次計數
    assert "回合開始了卻沒收到" in err, (
        "⚠⚠ 這個窗有助理內容、卻零則 prompt ⇒ 是真的漏收（不是壓縮失敗），"
        f"不可以被 `error` 豁免掉。實得：{err!r}")
    # ⚠ 同一件事的**收尾那次計數**：空窗排在檔尾。兩處各改各的，只驗一邊等於漏一半。
    _n, err = run(err_with_ai, "erraitail")
    assert "回合開始了卻沒收到" in err, (
        "⚠⚠ 同上，但這個窗**排在檔尾**——收尾那一次計數也要認得它。"
        f"實得：{err!r}")

    # --- ⑨ 空文字 ＋ **結構性 metadata**（`index: 0`）：不是內容搬家，不可以吵 --
    # ⚠ 判準若寫成「任何非空欄位」，`0` 這種 metadata 會被當成「文字搬到別欄位」
    #   。承載文字的欄位一定是字串。
    meta_only = turn(10) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "",
                                                "index": 0, "annotations": []}]}),
    ]
    _n, err = run(meta_only, "metaonly")
    assert "抽不出文字" not in err, (
        f"結構性 metadata（`index`/`annotations`）不是內容搬家，不可以誤報。實得：{err!r}")

    # --- ⑩（反向）`error` 那一窗只有**工具呼叫**（沒有助理文字）⇒ 仍是真的漏收 --
    # ⚠⚠ `turn_ai` 若只認 `message`，這個窗會被 `error` 錯誤豁免
    #    。
    err_tool = turn(10) + [
        line(30, "event_msg", {"type": "task_started", "turn_id": "t-30"}),
        line(31, "response_item", {"type": "function_call", "name": "sh",
                                   "call_id": "c9", "arguments": "{}"}),
        line(32, "event_msg", {"type": "error", "message": "stream disconnected"}),
        line(33, "event_msg", {"type": "task_complete"}),
    ]
    _n, err = run(err_tool, "errtool")
    assert "回合開始了卻沒收到" in err, (
        "⚠⚠ 這個窗有**工具呼叫**（助理內容的一種）、卻零則 prompt ⇒ 是真的漏收，"
        f"不可以被 `error` 豁免。實得：{err!r}")

    # --- ⑤（反向）真的漏收 prompt 的空窗，**照樣要出聲** --------------------
    # ⚠⚠ 少了這一格，③④ 可以用「整條哨兵關掉」來滿足。
    # ⚠ 這個空窗**不是最後一個**（後面還有一個窗），走的是迴圈裡那一次計數。
    real_loss = turn(10) + [
        line(30, "event_msg", {"type": "task_started", "turn_id": "t-30"}),
        line(31, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "沒人問就答SENTX。"}]}),
        line(32, "event_msg", {"type": "task_complete"}),
    ] + turn(40)
    _n, err = run(real_loss, "realloss")
    assert "回合開始了卻沒收到" in err, (
        f"⚠⚠ 真的有一個窗零則 prompt，哨兵卻沒出聲——這條哨兵等於被關掉了。實得：{err!r}")

    # --- ⑩（反向）字被包進**巢狀結構**：`text` 空著、真文字在 `content:[{"type":"text",…}]` --
    # 只看頂層字串欄位的話，這種整則靜默消失（零則助理事件、零警告）。
    nested = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "",
                                                "content": [{"type": "text",
                                                             "text": "包在裡面的回答SENTNEST。"}]}]}),
    ]
    _n, err = run(nested, "nested")
    assert "抽不出文字" in err, (
        f"⚠⚠ 字被包進巢狀結構時那一則整批消失，哨兵一定要出聲。實得：{err!r}")
    # 對照：巢狀的**結構性 metadata**（引用的 url／title）不是被搬走的內容，不可以出聲
    annotated = turn(10) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "",
                                                "annotations": [{"type": "url_citation",
                                                                 "url": "https://example.invalid/x",
                                                                 "title": "某個標題"}]}]}),
    ]
    _n, err = run(annotated, "annotated")
    assert "抽不出文字" not in err, (
        f"巢狀的引用 metadata 不是被搬走的內容，不該出聲。實得：{err!r}")

    # --- ⑪（反向）只有**加密的 reasoning**、沒有 prompt，然後 `error` ⇒ 仍是真的漏收 --
    # 真實 rollout 的 reasoning 幾乎全是 `summary: []` ＋ `encrypted_content`：只在「有摘要」時才算
    # 助理內容的話，這一窗會被 `error` 錯誤豁免，而這一格在真實資料上就等於從不成立。
    enc = turn(10) + [
        line(30, "event_msg", {"type": "task_started", "turn_id": "t-30"}),
        line(31, "response_item", {"type": "reasoning", "summary": [],
                                   "encrypted_content": "gAAAAB-synthetic-ciphertext"}),
        line(32, "event_msg", {"type": "error", "message": "stream disconnected"}),
        line(33, "event_msg", {"type": "task_complete"}),
    ] + turn(40)
    _n, err = run(enc, "encreason")
    assert "回合開始了卻沒收到" in err, (
        f"⚠⚠ 有助理產出（加密的 reasoning）卻沒有 prompt 的失敗回合被豁免了。實得：{err!r}")
    # 同一件事、但**整場只有這一個回合**：助理事件一個都沒有（加密 reasoning 不產生事件），
    # 收尾那道「有助理內容才出聲」的門檻不可以把它擋掉
    enc_only = [
        line(30, "event_msg", {"type": "task_started", "turn_id": "t-30"}),
        line(31, "response_item", {"type": "reasoning", "summary": [],
                                   "encrypted_content": "gAAAAB-synthetic-ciphertext"}),
        line(32, "event_msg", {"type": "error", "message": "stream disconnected"}),
        line(33, "event_msg", {"type": "task_complete"}),
    ]
    _n, err = run(enc_only, "encreasononly")
    assert "回合開始了卻沒收到" in err, (
        f"⚠⚠ 整場只有一個「加密 reasoning ＋失敗、沒有 prompt」的回合時，哨兵被收尾門檻擋掉了。實得：{err!r}")

    # --- ⑬（反向）**抽得到文字**的助理訊息裡，其餘帶著內容的 block 被略過 ⇒ 要出聲 ----------
    # 「取不出文字」那條只在一個字都抽不到時才看；文字＋圖片的那一則會只留文字、圖片安靜消失。
    mixed = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "有文字SENTMIX。"},
                                               {"type": "image", "url": "synthetic-image"}]}),
    ]
    _n, err = run(mixed, "mixed")
    assert "沒被收下" in err, f"文字以外的 block 被略過時要出聲。實得：{err!r}"
    # 對照：旁邊只是一個真的沒有話講的空文字 block ⇒ 不可以出聲
    mixed_ok = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "有文字SENTOK。"},
                                               {"type": "output_text", "text": ""}]}),
    ]
    _n, err = run(mixed_ok, "mixedok")
    assert "沒被收下" not in err, f"空文字 block 不是被丟掉的內容，不該出聲。實得：{err!r}"
    # `text` 不是字串（物件）⇒ 不可以把 Python 的 repr 當成回答收下，而且要出聲
    objtext = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": {"value": "SENTOBJ"}}]}),
    ]
    _n, err = run(objtext, "objtext")
    assert _n == 0 and "抽不出文字" in err, (
        f"`text` 是物件時不可以把 repr 當回答（實得 {_n} 則助理內容），而且要出聲：{err!r}")

    # --- ⑭（反向）reasoning 摘要換了形狀（不是 list）⇒ 要出聲；正常的空摘要不出聲 --------------
    badsum = turn(10) + [
        line(20, "response_item", {"type": "reasoning",
                                   "summary": {"type": "summary_text", "text": "摘要SENTSUM"}}),
    ]
    _n, err = run(badsum, "badsum")
    assert "摘要形狀認不得" in err, f"reasoning 摘要不是 list 時那一則摘要會安靜消失，要出聲：{err!r}"
    oksum = turn(10) + [
        line(20, "response_item", {"type": "reasoning", "summary": [],
                                   "encrypted_content": "gAAAAB-synthetic"}),
    ]
    _n, err = run(oksum, "oksum")
    assert "摘要形狀認不得" not in err, f"加密、沒有摘要的 reasoning 是正常形狀，不該出聲：{err!r}"

    # --- ⑫（反向）字放在內容容器的**字串清單**裡：`content: ["…"]` ⇒ 一樣要出聲 ------------
    strlist = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "",
                                                "content": ["字串清單裡的回答SENTLIST。"]}]}),
    ]
    _n, err = run(strlist, "strlist")
    assert "抽不出文字" in err, f"字放在 content 的字串清單裡時那一則整批消失，要出聲。實得：{err!r}"

    # --- ⑮（反向）推理**原文**放在 reasoning 的 `content` ⇒ 要出聲---------------
    # 本工具只畫摘要；摘要是空的 list 時，摘要那條哨兵完全安靜，原文就無聲消失。
    rcontent = turn(10) + [
        line(20, "response_item", {"type": "reasoning", "summary": [],
                                   "content": [{"type": "reasoning_text", "text": "推理原文SENTRC。"}]}),
    ]
    _n, err = run(rcontent, "rcontent")
    assert "推理原文" in err, f"reasoning 的 content 帶著文字時要出聲：{err!r}"
    # 對照：加密的 reasoning（content 是 null）是正常形狀
    rnull = turn(10) + [
        line(20, "response_item", {"type": "reasoning", "summary": [], "content": None,
                                   "encrypted_content": "gAAAAB-synthetic"}),
    ]
    _n, err = run(rnull, "rnull")
    assert "推理原文" not in err and "摘要形狀認不得" not in err, f"加密的 reasoning 不該出聲：{err!r}"

    # --- ⑯（反向）摘要**是 list**、裡面有帶著內容卻沒被收下的元素 ⇒ 要出聲（m09）-----
    # ⑭ 只驗了「不是 list」那一支；list 那一支拿掉也照樣綠。
    listsum = turn(10) + [
        line(20, "response_item", {"type": "reasoning",
                                   "summary": [{"type": "summary_text", "text": "摘要SENTLS。"},
                                               {"type": "summary_image", "url": "synthetic-image"}]}),
    ]
    _n, err = run(listsum, "listsum")
    assert "摘要形狀認不得" in err, f"摘要 list 裡有沒被收下、帶著內容的元素時要出聲：{err!r}"

    # --- ⑰（反向）`text` 收下了、**別的欄位還帶著內容** ⇒ 要出聲---------------------
    # 只收 `text` 會把同一個 block 裡的圖片連結（或搬過去的內容）整個丟掉，而「有文字」讓其他哨兵全靜。
    ride = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "See chart SENTRIDE:",
                                                "image_url": "https://example.invalid/chart.png"}]}),
    ]
    _n, err = run(ride, "ride")
    assert "沒被收下" in err, f"同一個 block 的其他欄位帶著內容時要出聲：{err!r}"
    wsmoved = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "有文字SENTWS。"},
                                               {"type": "output_text", "text": " ",
                                                "value": "搬到別欄位的內容SENTWSV"}]}),
    ]
    _n, err = run(wsmoved, "wsmoved")
    assert "沒被收下" in err, f"`text` 只剩空白、內容搬到別欄位時要出聲：{err!r}"
    # 對照：`annotations`（引用的 url／title）是 metadata，不是被丟掉的內容
    annot = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "有引用SENTANN。",
                                                "annotations": [{"type": "url_citation",
                                                                 "url": "https://example.invalid/",
                                                                 "title": "Example", "start_index": 0,
                                                                 "end_index": 3}]}]}),
    ]
    _n, err = run(annot, "annot")
    assert "沒被收下" not in err, f"引用的 metadata 不是被丟掉的內容，不該出聲：{err!r}"
    # 使用者那一側同一個形狀：收下了 `text`、同一個 block 還帶著圖片連結
    uride = [line(10, "event_msg", {"type": "task_started", "turn_id": "t-ur"}),
             line(11, "event_msg", {"type": "item_completed", "item": {
                 "type": "UserMessage",
                 "content": [{"type": "text", "text": "看這張SENTUR",
                              "image_url": "https://example.invalid/u.png"}]}}),
             line(12, "response_item", {"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text", "text": "好SENTURA。"}]}),
             line(13, "event_msg", {"type": "task_complete"})]
    _n, err = run(uride, "uride")
    assert "沒被收進來" in err, f"使用者訊息的 block 收下了文字、還帶著圖片時要出聲：{err!r}"

    # --- ⑱（反向）內容藏在**不叫 `text` 的鍵**底下一層 ⇒ 要出聲---------------------
    nested = turn(10, text=None) + [
        line(20, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"text": "", "output": {"value": "藏在別層SENTNEST。"}}]}),
    ]
    _n, err = run(nested, "nested")
    assert "抽不出文字" in err, f"內容換一層、換個鍵名時整則消失，要出聲：{err!r}"

    # --- ⑲ 使用者貼了一段 Slack Block Kit JSON：那是他的原文，不是序列化的 content ⇒ 不可以出聲 -------
    # 反向：真的序列化的 content（文字＋圖片）照樣要出聲。
    def _legacy(prompt, name):
        rows = [line(10, "event_msg", {"type": "task_started", "turn_id": f"t-{name}"}),
                line(11, "event_msg", {"type": "user_message", "message": prompt}),
                line(12, "response_item", {"type": "message", "role": "assistant",
                                           "content": [{"type": "output_text", "text": "回答SENTLG。"}]}),
                line(13, "event_msg", {"type": "task_complete"})]
        return run(rows, name)[1]

    err = _legacy(json.dumps([{"type": "section", "text": {"type": "mrkdwn", "text": "Hello SENTSLACK"}},
                              {"type": "divider"}]), "slack")
    assert "含非文字 block" not in err, f"使用者貼的 Slack Block Kit 不是序列化的 content，不該出聲：{err!r}"
    err = _legacy(json.dumps([{"type": "input_text", "text": "看圖SENTSER"},
                              {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]), "serial")
    assert "含非文字 block" in err, f"真的序列化的 content（文字＋圖片）照樣要出聲：{err!r}"

    print("OK: codex sentinel false alarms test passed")


def test_one_bad_session_does_not_kill_batch(tmp_path=None):
    """⚠⚠ **一場壞掉不可以拖垮整批**：那一場跳過、其餘照產，而且要出聲。

    例如 `queued_command.prompt` 是 block 陣列時 `synth_queued_user_events()` 丟
    `AttributeError`，而 `main()` 那一層若**沒有接**
    ⇒ **321 個來源檔一個頁面都沒產出**、退出碼非 0。
    那個具體的崩潰由 `test_user_turn_fidelity` ①-f 守，
    **這一格守的是下一個還沒被發現的崩潰**。

    ⚠⚠ **刻意用「注入例外」而不是找一筆真的會崩的素材**：真素材一旦被修好，
    這一格就會安靜地不再驗到任何東西、而測試照樣全綠——那是假綠的形狀。
    注入的例外不會被修掉。

    三件事一起驗（少一件就不算守住）：
    ① 退出碼 0、好的那場**真的有產出**；② 壞的那場**沒有**留下半截頁面；
    ③ **有出聲**——stderr 一行 `! 分析失敗`，收尾一格「壞掉跳過 1」。
    ⚠ ③ 不是裝飾：把例外整個吞掉也會讓 ①② 全綠，那時使用者拿到的是一份
      **安靜少一場**的輸出，而他不會知道。
    """
    tmp = new_tmp(tmp_path)
    projects = tmp / "projects" / "demo-proj"
    projects.mkdir(parents=True, exist_ok=True)
    out = tmp / "out"

    # ⚠ 兩個 sid 的**前 8 碼必須不同**：頁面檔名只帶前 8 碼，同碼就分不出誰產出了
    GOOD = "9a11ffff-1111-4111-8111-111111111111"
    BAD = "9b22ffff-2222-4222-8222-222222222222"

    def write(sid, marker):
        evs = [
            {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid,
             "timestamp": "2026-09-01T05:00:00.000Z", "cwd": "/x/Proj",
             "gitBranch": "main", "version": "2.1.251",
             "message": {"role": "user", "content": f"提問{marker}。"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid,
             "timestamp": "2026-09-01T05:00:05.000Z",
             "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                         "usage": {"input_tokens": 100, "output_tokens": 10},
                         "content": [{"type": "text", "text": f"回覆{marker}。"}]}},
        ]
        (projects / f"{sid}.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")

    write(GOOD, "GOODSESSION")
    write(BAD, "BADSESSION")

    # 在子行程裡把 `analyze` 換成「遇到那一場就丟例外」，其餘照原本走。
    # ⚠⚠ **三個注入點各驗一次。** 壞掉的那一場有三條路會走到同一個處理器
    #   （解析／分析／渲染），三條都要守；只守分析那一條的話，「回填舊 row」就只做到三分之一。
    def code_of(outdir, inject="analyze", extra=()):
        head = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "sys.argv = ['ai_session_viewer.py', '--claude-source', "
            f"'demo=' + {str(projects.parent)!r}, '--no-codex', '--out', {str(outdir)!r}]"
            f" + {list(extra)!r}\n"
            "import ai_session_viewer as v\n")
        BOOM = ("        raise AttributeError(\"'list' object has no attribute 'strip'\")\n")
        if inject == "analyze":
            boom = ("_orig = v.analyze\n"
                    "def _boom(s, acct=None):\n"
                    f"    if getattr(s, 'session_id', '') == {BAD!r}:\n"
                    + BOOM +
                    "    return _orig(s, acct)\n"
                    "v.analyze = _boom\n")
        elif inject == "load":
            boom = ("_orig = v.load_session\n"
                    "def _boom(path, *a, **k):\n"
                    f"    if {BAD!r} in path.name:\n"
                    + BOOM +
                    "    return _orig(path, *a, **k)\n"
                    "v.load_session = _boom\n")
        elif inject == "render":
            boom = ("_orig = v.render_session_html\n"
                    "def _boom(s, *a, **k):\n"
                    f"    if getattr(s, 'session_id', '') == {BAD!r}:\n"
                    + BOOM +
                    "    return _orig(s, *a, **k)\n"
                    "v.render_session_html = _boom\n")
        elif inject == "replace_md":
            # HTML 換上了、MD 換不上（Windows 上目標檔被開著或唯讀時就是這樣）
            boom = ("_orig_rep = v.os.replace\n"
                    "def _rep(a, b):\n"
                    f"    if str(b).endswith('.md') and {BAD[:8]!r} in str(b):\n"
                    "        raise PermissionError('injected: target is open')\n"
                    "    return _orig_rep(a, b)\n"
                    "v.os.replace = _rep\n")
        elif inject == "render_md":
            # HTML 已經渲染完、MD 才失敗：直接寫正式檔的話，HTML 這時已經被換成新內容了
            boom = ("_orig = v.render_session_md\n"
                    "def _boom(s, *a, **k):\n"
                    f"    if getattr(s, 'session_id', '') == {BAD!r}:\n"
                    + BOOM +
                    "    return _orig(s, *a, **k)\n"
                    "v.render_session_md = _boom\n")
        else:
            boom = ""
        return head + boom + "v.main()\n"

    code = code_of(out, inject="analyze")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")

    # ① 整批不可以停下來，好的那一場要真的產出
    assert r.returncode == 0, (
        "⚠⚠ 一場分析失敗把整支工具帶走了（退出碼 "
        f"{r.returncode}）。\n{r.stderr[-800:]}")
    pages = list((out / "sessions").rglob("*.html"))
    good = [p for p in pages if GOOD[:8] in p.name]
    bad = [p for p in pages if BAD[:8] in p.name]
    assert good and "GOODSESSION" in good[0].read_text(encoding="utf-8"), (
        f"好的那一場沒有產出（實得頁面：{[p.name for p in pages]}）")
    # ② 壞的那一場不可以留下半截頁面
    assert not bad, f"壞掉的那一場留下了頁面：{[p.name for p in bad]}"

    # ③ ⚠⚠ 一定要出聲——這一格才是「靜默少一場」與「跳過並告知」的分界
    assert "分析失敗" in r.stderr and BAD[:8] in r.stderr, (
        f"壞掉的那一場沒有在 stderr 出聲，實得：{r.stderr[-500:]!r}")
    assert "壞掉跳過 1" in r.stdout, (
        "收尾那行沒有公告「壞掉跳過 N」——`!` 訊息會被幾百行輸出捲走，"
        f"那一格是使用者一定看得到的位置。實得：{r.stdout[-500:]!r}")

    # --- ② ⚠⚠ **這一場先前建過**：分析失敗不可以把上一次的頁面刪掉 ------------
    # 實測的形狀：`new_entries.pop()` 之後那一場不進 `rows`
    # ⇒ 孤兒清理刪掉上一次建好的頁面；但同一次寫 `bookmarks.html` 讀的是磁碟上的**舊**
    # manifest（`prune_gone_sources()` 只對掉「來源檔不見了」的，來源檔還在）⇒ 管理頁
    # 留著一列**指向剛剛被自己刪掉的檔案**的書籤：點下去是找不到檔案，
    # 而不是設計上要給的「⚠ 對不到檔案」。
    out2 = tmp / "out2"
    r1 = subprocess.run([sys.executable, "-c", code_of(out2, inject=None)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r1.returncode == 0, f"前提：第一次建置要成功\n{r1.stderr[-500:]}"
    pages1 = {p.name for p in (out2 / "sessions").rglob("*.html")}
    assert any(BAD[:8] in n for n in pages1), (
        f"前提：第一次建置要產出壞掉那一場的頁面（實得 {sorted(pages1)}）")

    # 來源檔改一個位元組 ⇒ 指紋變了 ⇒ 不 reusable ⇒ 這一次真的會走進 analyze
    src_bad = projects / f"{BAD}.jsonl"
    src_bad.write_text(src_bad.read_text(encoding="utf-8").replace(
        "提問BADSESSION。", "提問BADSESSION！"), encoding="utf-8")

    r2 = subprocess.run([sys.executable, "-c", code_of(out2, inject="analyze")],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r2.returncode == 0, f"第二次建置非零退出\n{r2.stderr[-500:]}"
    # ⚠⚠ **主要的守衛在 manifest 這一層**，不是檔案那一層：孤兒清理只在
    #   `filtering=False`（完全不帶來源旗標的全量建置）時才跑，而測試一定要帶
    #   `--claude-source` 指到假語料 ⇒ **這個測試環境永遠到不了那條刪檔路徑**。
    #   根因是「那一場沒有留在 manifest 裡」，刪檔與書籤指錯都是它的下游。
    _man = json.loads((out2 / ".build-manifest.json").read_text(encoding="utf-8"))
    assert any(BAD in k for k in _man.get("entries", {})), (
        "⚠⚠ 分析失敗的那一場沒有被放回 manifest ⇒ 它不進 `rows` ⇒ 全量建置時"
        "**孤兒清理會刪掉上一次建好的頁面**，而書籤管理頁那一列還在（讀的是磁碟上的舊 manifest）"
        f"⇒ 使用者點下去找不到檔案。manifest 現有 {len(_man.get('entries', {}))} 筆")
    pages2 = {p.name for p in (out2 / "sessions").rglob("*.html")}
    assert any(BAD[:8] in n for n in pages2), (
        "⚠⚠ 分析失敗把**上一次建好的頁面**刪掉了——而書籤管理頁那一列還在，"
        f"使用者點下去會找不到檔案。實得頁面：{sorted(pages2)}")
    assert "沿用上一次的輸出" in r2.stdout, (
        "收尾那行沒有講出「沿用上一次的輸出」——那一頁的內容是舊的，"
        f"不講的話使用者會以為它是這一次產出的。實得：{r2.stdout[-400:]!r}")
    bx = (out2 / "bookmarks.html")
    if bx.exists():
        assert BAD[:8] in bx.read_text(encoding="utf-8"), (
            "書籤管理頁把這一場對掉了，但頁面還在 ⇒ 兩邊不一致（另一個方向的同一個缺陷）")

    # --- ③ 另外兩個注入點：**解析失敗**與**渲染失敗**走同一支處理器 ------------
    for _stage, _msg in (("load", "解析失敗"), ("render", "渲染失敗")):
        _out = tmp / f"out_{_stage}"
        _r1 = subprocess.run([sys.executable, "-c", code_of(_out, inject=None)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert _r1.returncode == 0, f"前提：{_stage} 那一組的第一次建置要成功"
        src_bad2 = projects / f"{BAD}.jsonl"
        src_bad2.write_text(src_bad2.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        _r2 = subprocess.run([sys.executable, "-c", code_of(_out, inject=_stage)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert _r2.returncode == 0, (
            f"⚠⚠ {_msg}把整支工具帶走了（退出碼 {_r2.returncode}）"
            f"——護欄只包住其中一條路\n{_r2.stderr[-500:]}")
        assert _msg in _r2.stderr, f"{_msg}沒有在 stderr 出聲：{_r2.stderr[-300:]!r}"
        _man2 = json.loads((_out / ".build-manifest.json").read_text(encoding="utf-8"))
        assert any(BAD in k for k in _man2.get("entries", {})), (
            f"⚠⚠ {_msg}時沒有把上一次的 row 放回 manifest ⇒ 全量建置會刪掉上一次的頁面、"
            "而書籤那一列還在（同一個缺陷，只是換一條路進來）")

    # --- ④ ⚠⚠ **舊輸出已經不在磁碟上時，不可以回填** ---------------------------
    # 反方向的同一種傷害：無條件回填會讓索引多一條**指向不存在的檔**的連結
    out3 = tmp / "out3"
    _r1 = subprocess.run([sys.executable, "-c", code_of(out3, inject=None)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r1.returncode == 0, "前提：out3 的第一次建置要成功"
    for _p in (out3 / "sessions").rglob("*"):
        if _p.is_file() and BAD[:8] in _p.name:
            _p.unlink()                      # 模擬「頁面被清掉／手動刪掉」
    src_bad3 = projects / f"{BAD}.jsonl"
    src_bad3.write_text(src_bad3.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _r2 = subprocess.run([sys.executable, "-c", code_of(out3, inject="analyze")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r2.returncode == 0, "第二次建置不該非零退出"
    _man3 = json.loads((out3 / ".build-manifest.json").read_text(encoding="utf-8"))
    assert not any(BAD in k for k in _man3.get("entries", {})), (
        "⚠⚠ 舊輸出已經不在磁碟上了，卻還是把那一列放回 manifest ⇒ 索引會多一條"
        "**指向不存在的檔**的連結。回填之前一定要確認那份輸出還在")

    # --- ⑤ ⚠⚠ **渲染到一半失敗，不可以留下 manifest 仍然信任的新頁面** --------------
    # HTML 渲染完、MD 才失敗：直接寫正式檔的話 HTML 已經換成新內容，而 manifest 放回的是
    # **上一次**的 row（舊指紋）——頁面與紀錄對不上，寫壞的頁面還會被下一次當成「沿用」。
    # 要求：正式檔要嘛是完整的舊版、要嘛是完整的新版；失敗時就是舊版，而且不留暫存檔。
    out4 = tmp / "out4"
    _r1 = subprocess.run([sys.executable, "-c", code_of(out4, inject=None)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r1.returncode == 0, "前提：out4 的第一次建置要成功"
    _before = {p.name: p.read_bytes() for p in (out4 / "sessions").rglob("*")
               if p.is_file() and BAD[:8] in p.name}
    assert any(n.endswith(".html") for n in _before), f"前提：要有舊的 HTML：{sorted(_before)}"
    src_bad4 = projects / f"{BAD}.jsonl"
    src_bad4.write_text(src_bad4.read_text(encoding="utf-8").replace(
        "BADSESSION", "BADSESSION-NEWER"), encoding="utf-8")
    _r2 = subprocess.run([sys.executable, "-c", code_of(out4, inject="render_md")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r2.returncode == 0 and "渲染失敗" in _r2.stderr, f"前提：MD 渲染要失敗並出聲\n{_r2.stderr[-400:]}"
    _after = {p.name: p.read_bytes() for p in (out4 / "sessions").rglob("*")
              if p.is_file() and BAD[:8] in p.name}
    assert _after == _before, (
        "⚠⚠ 渲染到一半失敗，正式檔卻被換掉了（或留下暫存檔）——manifest 放回的是上一次的 row，"
        f"頁面與紀錄對不上。前：{sorted(_before)}；後：{sorted(_after)}")

    # --- ⑥ ⚠⚠ 上一次只建了 MD，這一次渲染又失敗 ⇒ 索引不可以連到不存在的 HTML -------
    out5 = tmp / "out5"
    _r1 = subprocess.run([sys.executable, "-c", code_of(out5, inject=None, extra=("--format", "md"))],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r1.returncode == 0, "前提：out5 的第一次（只建 MD）建置要成功"
    src_bad5 = projects / f"{BAD}.jsonl"
    src_bad5.write_text(src_bad5.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _r2 = subprocess.run([sys.executable, "-c", code_of(out5, inject="render")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r2.returncode == 0 and "渲染失敗" in _r2.stderr, f"前提：HTML 渲染要失敗\n{_r2.stderr[-400:]}"
    _idx = (out5 / "index.html").read_text(encoding="utf-8")
    _hrefs = re.findall(r'href="sessions/([^"]+)"', _idx)
    _bad_links = [h for h in _hrefs if BAD[:8] in h]
    assert _bad_links and all((out5 / "sessions" / h).exists() for h in _bad_links), (
        "⚠⚠ 索引連到不存在的頁面：上一次只有 MD、這一次 HTML 渲染失敗，"
        f"那一列要改連 MD。實得連結：{_bad_links}")
    assert "僅 .md" in _idx, "改連 MD 的那一列要標示「僅 .md」"

    # --- ⑥-b 舊紀錄說「有 HTML」、但那個檔已經不在：沿用時旗標要照磁碟實況改寫 --------
    out6 = tmp / "out6"
    _r1 = subprocess.run([sys.executable, "-c", code_of(out6, inject=None)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r1.returncode == 0, "前提：out6 的第一次建置要成功"
    for _p in (out6 / "sessions").rglob("*.html"):
        if BAD[:8] in _p.name:
            _p.unlink()                      # HTML 被清掉了，MD 還在
    src_bad6 = projects / f"{BAD}.jsonl"
    src_bad6.write_text(src_bad6.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _r2 = subprocess.run([sys.executable, "-c", code_of(out6, inject="render")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r2.returncode == 0 and "渲染失敗" in _r2.stderr, "前提：HTML 渲染要失敗"
    _idx6 = (out6 / "index.html").read_text(encoding="utf-8")
    _l6 = [h for h in re.findall(r'href="sessions/([^"]+)"', _idx6) if BAD[:8] in h]
    assert _l6 and all((out6 / "sessions" / h).exists() for h in _l6), (
        "⚠⚠ 沿用的舊紀錄仍說「有 HTML」，索引就連到了已經不在的檔。實得：" + repr(_l6))

    # --- ⑦ ⚠⚠ HTML 換上了、MD 換不上：manifest 要照磁碟實況記，不可以新舊混在一起卻說全是舊的 ----
    out7 = tmp / "out7"
    _r1 = subprocess.run([sys.executable, "-c", code_of(out7, inject=None)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r1.returncode == 0, "前提：out7 的第一次建置要成功"
    src_bad7 = projects / f"{BAD}.jsonl"
    src_bad7.write_text(src_bad7.read_text(encoding="utf-8").replace(
        "BADSESSION", "BADSESSION-PUB7"), encoding="utf-8")
    _r2 = subprocess.run([sys.executable, "-c", code_of(out7, inject="replace_md")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r2.returncode == 0 and "寫出失敗" in _r2.stderr, f"前提：MD 要換不上並出聲\n{_r2.stderr[-400:]}"
    _man7 = json.loads((out7 / ".build-manifest.json").read_text(encoding="utf-8"))
    _row7 = next(e["row"] for k, e in _man7["entries"].items() if BAD in k)
    _html7 = next(p for p in (out7 / "sessions").rglob("*.html") if BAD[:8] in p.name)
    assert "BADSESSION-PUB7" in _html7.read_text(encoding="utf-8"), "前提：HTML 已經換成新版"
    assert _row7.get("has_html") is True and _row7.get("has_md") is False, (
        "⚠⚠ HTML 是新的、MD 是舊的：manifest 要照實記（HTML 同步、MD 過期），"
        f"不可以整列說成舊的或新的。實得 has_html={_row7.get('has_html')} has_md={_row7.get('has_md')}")
    _idx7 = (out7 / "index.md").read_text(encoding="utf-8")
    _bad_line = next(l for l in _idx7.splitlines() if BAD[:8] in l)
    assert ".html)" in _bad_line and "僅 HTML" in _bad_line, (
        f"MD 過期時，MD 索引要改連 HTML 並標示：{_bad_line}")

    # --- ⑦-b 「下一次建置會修好」要真的發生：MD 標成過期的那一場，下一次正常建置一定要重寫 ----------
    # （沿用判定不看 `has_md` 時，過期的 MD 連續兩次建置都沒被重寫，整套照樣綠）
    _r3 = subprocess.run([sys.executable, "-c", code_of(out7, inject=None)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r3.returncode == 0, f"第三次（正常）建置非零退出\n{_r3.stderr[-400:]}"
    _md7 = next(p for p in (out7 / "sessions").rglob("*.md") if BAD[:8] in p.name)
    assert "BADSESSION-PUB7" in _md7.read_text(encoding="utf-8"), (
        "⚠⚠ MD 標成過期的那一場，下一次建置沒有重寫——沿用了舊 MD（`--search` 讀的就是它）")
    _row7b = next(e["row"] for k, e in json.loads(
        (out7 / ".build-manifest.json").read_text(encoding="utf-8"))["entries"].items() if BAD in k)
    assert _row7b.get("has_md") is True, f"重寫之後 MD 要標回同步：{_row7b.get('has_md')!r}"

    # --- ⑧ 沿用時兩種格式都不可用（磁碟上那份 MD 原本就被標成過期）⇒ 不回填、索引不給連結 ----------
    out8 = tmp / "out8"
    _r1 = subprocess.run([sys.executable, "-c", code_of(out8, inject=None)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r1.returncode == 0, "前提：out8 的第一次建置要成功（兩種格式都有）"
    src_bad8 = projects / f"{BAD}.jsonl"
    src_bad8.write_text(src_bad8.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _r2 = subprocess.run([sys.executable, "-c", code_of(out8, inject=None, extra=("--format", "html"))],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r2.returncode == 0, "前提：第二次只建 HTML（MD 留著但標成過期）"
    for _p in (out8 / "sessions").rglob("*.html"):
        if BAD[:8] in _p.name:
            _p.unlink()
    src_bad8.write_text(src_bad8.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _r3 = subprocess.run([sys.executable, "-c", code_of(out8, inject="render")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert _r3.returncode == 0 and "渲染失敗" in _r3.stderr, "前提：第三次渲染要失敗"
    _man8 = json.loads((out8 / ".build-manifest.json").read_text(encoding="utf-8"))
    assert not any(BAD in k for k in _man8.get("entries", {})), (
        "⚠⚠ 兩種格式都不可用（HTML 不在、MD 過期），卻還是把那一列放回 manifest")
    for _ix in ("index.html", "index.md"):
        _t8 = (out8 / _ix).read_text(encoding="utf-8")
        assert not any(BAD[:8] in h and not (out8 / "sessions" / h).exists()
                       for h in re.findall(r'sessions/([^")\s]+)', _t8)), (
            f"{_ix} 連到了不存在的檔")
    # 索引本身的防線：一列兩種格式都標成不可用時（例如縮範圍建置沿用的舊紀錄），兩份索引都不給連結
    import importlib
    sys.path.insert(0, str(ROOT))
    _v8 = importlib.import_module("ai_session_viewer")
    _good = dict(next(e["row"] for k, e in _man8["entries"].items() if GOOD in k),
                 has_html=False, has_md=False)
    assert "sessions/" not in _v8.render_index_html([_good]).split("<tbody", 1)[-1], (
        "HTML 索引替「兩種格式都不可用」的列給了連結")
    assert "](sessions/" not in _v8.render_index_md([_good]), "MD 索引替「兩種格式都不可用」的列給了連結"

    print("OK: one bad session does not kill the batch test passed")


def test_archive_command_only(tmp_path=None):
    """`--archive-command-only`：把「只有指令、沒有對話」的 session 搬走。

    ⚠⚠ **這是本工具唯一會動到 `out/` 以外檔案的功能**，而且動的是
    `~/.claude/projects/`——**Claude Code 自己**在讀的目錄。
    所以這一格最重要的不是「有沒有搬」，是**「不該搬的絕對不能被搬」**：
    分類器判錯一次，使用者就永久失去一場對話在 `claude --resume` 裡的入口。

    ⚠ 沒給參數時**一個來源檔都不可以被碰** —— 這一格也在守。
    """
    sys.path.insert(0, str(ROOT))
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "demo-proj"
    proj.mkdir(parents=True, exist_ok=True)

    # ⚠ 四個 sid 的**前 8 碼必須互不相同**：頁面檔名只帶 sid 前 8 碼
    # （`<時間>__<專案>__<sid[:8]>.html`），相同的話「誰的頁面被刪了」根本分不出來，
    # 而那正是這一格要驗的事。
    # ⚠⚠ 素材的 mtime 一律撥回一天前：封存不碰最近 `ARCHIVE_MIN_IDLE_SEC` 內有寫入的 session
    #    （可能還開著）。不撥的話每一場都被那道閘擋下，下面的斷言全部對著「什麼都沒搬」在跑。
    #    那道閘本身由「最近有寫入的不搬」那一格單獨驗（它刻意不撥）。
    _OLD = time.time() - 86400

    def w(sid, evs):
        p = proj / f"{sid}.jsonl"
        p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
        os.utime(p, (_OLD, _OLD))

    base = dict(cwd="/x/Proj", gitBranch="main", version="2.1.240")
    CMD_U = ("<command-name>/effort</command-name>\n"
             "<command-message>effort</command-message>\n"
             "<command-args>high</command-args>")
    CMD_S = ("<command-name>/remote-control</command-name>\n"
             "<command-args></command-args>")
    NTAG = "task-" + "notification"

    # ① 只有指令（兩種存法都有）⇒ **該搬**
    sid_cmd = "ac010000-0000-4000-8000-00000000ac01"
    w(sid_cmd, [
        dict(type="user", uuid="a1", timestamp="2026-07-25T01:00:00.000Z",
             sessionId=sid_cmd, message={"role": "user", "content": CMD_U}, **base),
        dict(type="system", subtype="local_command", uuid="a2", isMeta=False,
             timestamp="2026-07-25T01:00:05.000Z", sessionId=sid_cmd, content=CMD_S, **base),
    ])
    # ② 指令 ＋ **真的對話** ⇒ **絕對不可以搬**
    sid_talk = "ac020000-0000-4000-8000-00000000ac02"
    w(sid_talk, [
        dict(type="user", uuid="b1", timestamp="2026-07-25T02:00:00.000Z",
             sessionId=sid_talk, message={"role": "user", "content": CMD_U}, **base),
        dict(type="user", uuid="b2", timestamp="2026-07-25T02:00:10.000Z",
             sessionId=sid_talk,
             message={"role": "user", "content": "這是真的提問KEEPME。"}, **base),
        dict(type="assistant", uuid="b3", timestamp="2026-07-25T02:00:20.000Z",
             sessionId=sid_talk,
             message={"role": "assistant", "model": "claude-opus-4-7", "id": "mb3",
                      "usage": {"input_tokens": 10, "output_tokens": 5},
                      "content": [{"type": "text", "text": "回覆KEEPMEREPLY。"}]}, **base),
    ])
    # ③ 指令 ＋ **只有背景任務通知** ⇒ 通知不算對話，仍**該搬**
    sid_ntfy = "ac030000-0000-4000-8000-00000000ac03"
    w(sid_ntfy, [
        dict(type="user", uuid="c1", timestamp="2026-07-25T03:00:00.000Z",
             sessionId=sid_ntfy, message={"role": "user", "content": CMD_U}, **base),
        dict(type="user", uuid="c2", timestamp="2026-07-25T03:00:10.000Z",
             sessionId=sid_ntfy,
             message={"role": "user", "content":
                      f"<{NTAG}>\n<task-id>z1</task-id>\n"
                      f"<summary>NTFYONLY done</summary>\n<status>completed</status>\n"
                      f"</{NTAG}>"}, **base),
    ])
    # ④ 指令 ＋ **助手只用了工具、沒有文字** ⇒ 那也是對話，**不可以搬**
    sid_tool = "ac040000-0000-4000-8000-00000000ac04"
    w(sid_tool, [
        dict(type="user", uuid="d1", timestamp="2026-07-25T04:00:00.000Z",
             sessionId=sid_tool, message={"role": "user", "content": CMD_U}, **base),
        dict(type="assistant", uuid="d2", timestamp="2026-07-25T04:00:10.000Z",
             sessionId=sid_tool,
             message={"role": "assistant", "model": "claude-opus-4-7", "id": "md2",
                      "usage": {"input_tokens": 10, "output_tokens": 5},
                      "content": [{"type": "tool_use", "id": "tu1", "name": "Read",
                                   "input": {"file_path": "/x/a.txt"}}]}, **base),
    ])

    # ⑤ 指令 ＋ **只有使用者發言、助手一個字都沒回** ⇒ 那也是對話，**不可以搬**
    # ⚠⚠ 沒有這一場，「使用者發言」那條分支**一格都驗不到**：②那場同時有助手回覆，
    # 所以把使用者那條關掉，助手那條照樣接住 ⇒ 突變不會紅（實測漏過一次）。
    # **要驗一條分支，就要有一筆素材「只會走到它」**。
    sid_user = "ac050000-0000-4000-8000-00000000ac05"
    w(sid_user, [
        dict(type="user", uuid="e1", timestamp="2026-07-25T05:00:00.000Z",
             sessionId=sid_user, message={"role": "user", "content": CMD_U}, **base),
        dict(type="user", uuid="e2", timestamp="2026-07-25T05:00:10.000Z",
             sessionId=sid_user,
             message={"role": "user", "content": "問了但沒人回USERONLY。"}, **base),
    ])

    # ⑥ 指令 ＋ **這支程式不認得的 content block** ⇒ 破壞性分類不可以 fail-open
    # ⚠⚠ 這一場的素材只會走到 `_has_unknown_content_block()` 那條保守否決：
    # 未知型別在 `group_turns()` 就被丟掉了，渲染產物裡**只剩一列指令**
    # ⇒ 只看 `main_groups` 的話它長得跟①一模一樣（實測搬走了）。
    sid_unk = "ac060000-0000-4000-8000-00000000ac06"
    w(sid_unk, [
        dict(type="user", uuid="f1", timestamp="2026-07-25T06:00:00.000Z",
             sessionId=sid_unk, message={"role": "user", "content": CMD_U}, **base),
        dict(type="user", uuid="f2", timestamp="2026-07-25T06:00:10.000Z",
             sessionId=sid_unk,
             message={"role": "user", "content": [
                 {"type": "document", "source": {"type": "base64", "data": "UEsDBA=="}}]},
             **base),
    ])
    # ⑦ 指令 ＋ **使用者自己在講那個通知標籤**（前後都有他真的打的字）
    # ⇒ substring 判準會把整則當成通知（通知不算對話）⇒ 誤搬
    sid_lit = "ac070000-0000-4000-8000-00000000ac07"
    w(sid_lit, [
        dict(type="user", uuid="g1", timestamp="2026-07-25T07:00:00.000Z",
             sessionId=sid_lit, message={"role": "user", "content": CMD_U}, **base),
        dict(type="user", uuid="g2", timestamp="2026-07-25T07:00:10.000Z",
             sessionId=sid_lit,
             message={"role": "user", "content":
                      f"請解釋 LITERALASK 這個 <{NTAG}> 標籤是什麼意思？"
                      f"</{NTAG}> 我在頁面上看到它。"}, **base),
    ])

    # ⑧ 指令 ＋ `content` 是**單一個 dict**（不是 list）⇒ 也是「看不懂」
    # ⚠⚠ 守衛若寫成「不是 list 就是純字串，認得」，這一場照樣被搬走
    #    （實測 `single_dict_block: has_unknown=False kind=command_only`）。
    sid_dict = "ac0a0000-0000-4000-8000-00000000ac0a"
    w(sid_dict, [
        dict(type="user", uuid="j1", timestamp="2026-07-25T10:00:00.000Z",
             sessionId=sid_dict, message={"role": "user", "content": CMD_U}, **base),
        dict(type="user", uuid="j2", timestamp="2026-07-25T10:00:10.000Z",
             sessionId=sid_dict,
             message={"role": "user", "content": {"type": "document", "id": "d1"}}, **base),
    ])
    # ⑨ 指令 ＋ list 裡混了**非 dict 的非空元素**
    sid_scalar = "ac0b0000-0000-4000-8000-00000000ac0b"
    w(sid_scalar, [
        dict(type="user", uuid="k1", timestamp="2026-07-25T11:00:00.000Z",
             sessionId=sid_scalar, message={"role": "user", "content": CMD_U}, **base),
        dict(type="assistant", uuid="k2", timestamp="2026-07-25T11:00:10.000Z",
             sessionId=sid_scalar,
             message={"role": "assistant", "model": "claude-opus-4-7", "id": "mk2",
                      "usage": {"input_tokens": 10, "output_tokens": 5},
                      "content": ["某種還沒見過的純量內容"]}, **base),
    ])

    out = tmp / "out"
    arc = tmp / "archive"

    def run(*extra):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
             "--no-codex", "--out", str(out), "--format", "both", *extra],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
        return r

    # --- 沒給參數：一個來源檔都不可以被碰 -----------------------------------
    run()
    for sid in (sid_cmd, sid_talk, sid_ntfy, sid_tool, sid_user, sid_unk, sid_lit,
                sid_dict, sid_scalar):
        assert (proj / f"{sid}.jsonl").is_file(), (
            f"沒給 --archive-command-only 時 {sid} 不該被動到")
    assert not arc.exists(), "沒給參數時不該建出封存目錄"

    # --- 給了參數：只搬該搬的 -----------------------------------------------
    # ⚠⚠ **先在封存目錄放一個同名檔**：「絕不覆蓋」那道閘要有素材才驗得到。
    # 沒有它的話，把 `if target.exists()` 關掉也不會有任何一格變紅（實測漏過一次）。
    arc.mkdir(parents=True, exist_ok=True)
    clash = arc / f"-x-Proj__{sid_cmd}.jsonl"
    clash.write_text("先前封存的內容DONOTCLOBBER", encoding="utf-8")
    r = run("--archive-command-only", str(arc))
    assert clash.read_text(encoding="utf-8") == "先前封存的內容DONOTCLOBBER", (
        "⚠⚠ 覆蓋掉了封存目錄裡已經存在的同名檔——那是先前封存的紀錄，覆蓋＝資料消失")
    assert (proj / f"{sid_cmd}.jsonl").is_file(), (
        "撞名時該**略過並保留原檔**，不是搬走之後才發現搬不進去")
    assert "目的地已有同名檔" in (r.stdout + r.stderr), "撞名要回報，不可以靜默略過"
    # 清掉這顆地雷，讓下面的斷言看的是正常情況
    clash.unlink()
    r = run("--archive-command-only", str(arc))
    assert (proj / f"{sid_user}.jsonl").is_file(), (
        "⚠⚠ 只有使用者發言、助手沒回的 session 也是對話，不可以搬")
    assert (proj / f"{sid_talk}.jsonl").is_file(), (
        "⚠⚠ 有真對話的 session 被搬走了——那是永久失去 `claude --resume` 入口")
    assert (proj / f"{sid_tool}.jsonl").is_file(), (
        "⚠⚠ 助手只用工具沒講話**也是對話**，不可以搬")
    assert not (proj / f"{sid_cmd}.jsonl").is_file(), "只有指令的該被搬走"
    assert not (proj / f"{sid_ntfy}.jsonl").is_file(), "只有指令＋通知的該被搬走（通知不算對話）"
    assert (proj / f"{sid_unk}.jsonl").is_file(), (
        "⚠⚠ 有不認得的 content block 就不可以搬——破壞性分類不可以對未知 schema fail-open")
    assert (proj / f"{sid_lit}.jsonl").is_file(), (
        "⚠⚠ 使用者自己在講那個通知標籤，那是他的發言，不是系統注入的通知")
    assert (proj / f"{sid_dict}.jsonl").is_file(), (
        "⚠⚠ `content` 是單一個 dict（不是 list）也是「看不懂」——不可以搬。"
        "「不是 list 就是純字串」那句話不成立")
    assert (proj / f"{sid_scalar}.jsonl").is_file(), (
        "⚠⚠ list 裡混了非 dict 的非空元素也是「看不懂」——不可以搬")

    # 檔名＝cwd（munged）＋原檔名，且原始絕對路徑記在 manifest 裡（可還原）
    names = sorted(p.name for p in arc.glob("*.jsonl") if p.name != "_archived.jsonl")
    assert names == sorted([f"-x-Proj__{sid_cmd}.jsonl", f"-x-Proj__{sid_ntfy}.jsonl"]), names
    man = [json.loads(x) for x in
           (arc / "_archived.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    # ⚠⚠ **兩段式紀錄**：每一場搬之前先寫 `pending`、搬成功後再寫 `moved`。
    # 沒有第一段的話，「主檔已經離開、紀錄一行都沒寫」是一條真的路徑
    # （manifest 不可寫、行程被砍），而還原的人只有這份紀錄。
    pend = [m for m in man if m.get("state") == "pending"]
    done = [m for m in man if m.get("state") == "moved"]
    assert len(done) == 2, f"應該有兩場搬成功的紀錄：{man}"
    assert len(pend) == 2, f"搬之前的 pending 紀錄漏了：{man}"
    assert {m["to"] for m in pend} == {m["to"] for m in done}, (
        "pending 與 moved 對不起來——還原的人會不知道哪一場搬完了")
    assert all(Path(m["from"]).name.endswith(".jsonl") and m["reason"] == "command_only"
               for m in done), man
    # ⚠⚠ `from` 一定要是**絕對路徑**：CLI 印的是「含原始絕對路徑，可依它還原」，
    # 而 `--claude-source` 收得下相對路徑。寫成未 resolve 的 `str(path)` 的話 ⇒ 那句話是假的。
    assert all(Path(m["from"]).is_absolute() for m in man), (
        f"⚠ 紀錄裡的來源路徑不是絕對路徑，還原時找不到原位：{[m['from'] for m in man]}")

    # 產出的頁面要跟著消失（⚠ `prune_gone_sources` 只對帳 manifest、不刪檔，
    # 不在封存那裡刪的話會留下看不見但仍含對話內容的孤兒頁）
    left = {p.name for p in _session_pages(out)}
    assert not any(sid_cmd[:8] in n or sid_ntfy[:8] in n for n in left), (
        f"被搬走的 session 頁面還在：{left}")
    assert any(sid_talk[:8] in n for n in left), "有對話的那場頁面不該被刪"

    # --- ⚠⚠ 搬移必須撐得住「來源與目的地不同磁碟機」-------------------------
    # 這正是這個功能的**典型用法**：來源在 `C:\Users\…\.claude\projects`，
    # 使用者指定的封存目錄多半在別的磁碟機。
    # 只用 `Path.replace()`（＝`os.replace`）的話，跨磁碟機直接丟 OSError ⇒
    # 92 場**全部**失敗，而失敗被收成一行「搬移失敗：OSError：92」，
    # **跑起來像是「沒有東西需要搬」**——暫存目錄和來源在同一顆碟時，這一格照樣是綠的。
    # 這裡不能真的造出第二顆碟，所以**把「搬進封存目錄」的 `os.replace` 換成跨磁碟錯誤**
    # （`EXDEV`），驗它真的退回「凍結→複製→比對→刪」那條路而且搬得成。
    # ⚠ 不能用 grep 原始碼裡有沒有 `shutil.move(...)` 來驗——那只證明 API 名字對，
    #   證明不了行為（`shutil.move` 在佔位檔前面**每一次**都退化成複製＋刪來源）。
    import ai_session_viewer as _v
    import errno as _errno
    _real_replace, _real_copyfile, _real_move = _v.os.replace, _v.shutil.copyfile, _v.shutil.move

    def _one_cmd_session(sid, t):
        w(sid, [dict(type="user", uuid=f"x{sid[:4]}", timestamp=t, sessionId=sid,
                     message={"role": "user", "content": CMD_U}, **base)])
        run()
        _s = _v.load_session(proj / f"{sid}.jsonl", "demo-proj", "demo", _v.SOURCE_CLAUDE)
        _v.analyze(_s)
        return _s

    def _into(dst, arc_dir):
        return Path(dst).resolve().parent == Path(arc_dir).resolve()

    @contextlib.contextmanager
    def _cross_device(arc_dir):
        """「搬進 `arc_dir`」的 `os.replace` 一律丟跨磁碟錯誤（造不出第二顆碟，改造它的錯誤碼）。"""
        def _fake(src, dst, *a, **k):
            if _into(dst, arc_dir):
                raise OSError(_errno.EXDEV, "simulated cross-device move")
            return _real_replace(src, dst, *a, **k)
        _v.os.replace = _fake
        try:
            yield
        finally:
            _v.os.replace = _real_replace

    def _arc_one(sid, s, arc_dir, *cls):
        return _v.archive_command_only(
            [(_v.SOURCE_CLAUDE, "demo", "demo-proj", proj / f"{sid}.jsonl", s, *cls)],
            arc_dir, [proj.parent], out)

    def _journal_rows(arc_dir):
        f = arc_dir / "_archived.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] \
            if f.is_file() else []

    arc_x = tmp / "archive-xdev"
    sid_x = "ac180000-0000-4000-8000-00000000ac18"
    _sx = _one_cmd_session(sid_x, "2026-07-25T16:20:00.000Z")
    _body_x = (proj / f"{sid_x}.jsonl").read_bytes()
    with _cross_device(arc_x):
        _mx, _skx = _arc_one(sid_x, _sx, arc_x)
    assert len(_mx) == 1, f"跨磁碟時要退回「凍結→複製→比對→刪」照樣搬成功：{_mx} {_skx}"
    assert (arc_x / f"-x-Proj__{sid_x}.jsonl").read_bytes() == _body_x, "跨磁碟搬過去的內容要逐位元相同"
    assert not (proj / f"{sid_x}.jsonl").exists(), "搬成功之後來源不該還在"
    assert not list(proj.glob("*.archiving")), "跨磁碟搬完要刪掉凍結檔"

    # --- ⚠⚠ 同一顆碟：搬移要是**原子改名**，不可以是「複製＋刪來源」 ---------------
    # 用 `shutil.move` 的話，遇到我們自己的 `O_EXCL` 佔位檔，內部的 rename 失敗（Windows 的 rename
    # 不覆蓋）⇒ **每一次**都退化成複製＋刪來源；複製完到刪來源之間寫進來的那一行會被一起刪掉。
    # 改名的話檔案身分（inode／NTFS file id）不變——複製一定會換一個。
    arc_r = tmp / "archive-rename"
    sid_r = "ac190000-0000-4000-8000-00000000ac19"
    _sr = _one_cmd_session(sid_r, "2026-07-25T16:30:00.000Z")
    _ino_r = (proj / f"{sid_r}.jsonl").stat().st_ino
    _mr, _skr = _arc_one(sid_r, _sr, arc_r)
    assert _mr, f"同碟搬移失敗：{_skr}"
    assert (arc_r / f"-x-Proj__{sid_r}.jsonl").stat().st_ino == _ino_r, (
        "⚠⚠ 同一顆碟的搬移不是原子改名（檔案身分換了 ⇒ 走的是複製＋刪來源）："
        "複製完到刪來源之間 Claude Code 寫進來的那一行會被一起刪掉")

    # --- ⚠⚠ 跨磁碟：複製途中 Claude Code **依路徑**往來源追加一行 ⇒ 那一行不可以被刪掉 ------
    # 先改名凍結再複製：之後依路徑寫入的東西落在原處的新檔，不會跟著凍結檔被刪。
    _LATE = (json.dumps({"type": "user", "uuid": "late1", "timestamp": "2026-07-25T16:41:00.000Z",
                         "message": {"role": "user", "content": "LATEWRITE"}}) + "\n").encode()
    arc_lw = tmp / "archive-latewrite"
    sid_lw = "ac1a0000-0000-4000-8000-00000000ac1a"
    _slw = _one_cmd_session(sid_lw, "2026-07-25T16:40:00.000Z")
    _src_lw = proj / f"{sid_lw}.jsonl"

    def _copy_then_append_path(src, dst, *a, **k):
        r = _real_copyfile(src, dst, *a, **k)
        with open(_src_lw, "ab") as fh:                 # 依路徑追加（複製完、刪來源之前）
            fh.write(_LATE)
        return r

    _v.shutil.copyfile = _copy_then_append_path
    try:
        with _cross_device(arc_lw):
            _arc_one(sid_lw, _slw, arc_lw)
    finally:
        _v.shutil.copyfile = _real_copyfile
    _hold = [p.name for p in (_src_lw, arc_lw / f"-x-Proj__{sid_lw}.jsonl")
             if p.is_file() and _LATE in p.read_bytes()]
    assert _hold, ("⚠⚠ 搬移途中依路徑寫進來源的那一行被刪掉了——不在來源原處、也不在封存檔裡"
                   "（紀錄還寫 moved、stderr 一個字都沒有）")
    assert not list(proj.glob("*.archiving")), "凍結檔要收掉"

    # --- ⚠⚠ 跨磁碟：開著的 handle 往**凍結檔**追加 ⇒ 內容對不上，不可以刪 ----------------
    # 改名擋不住「已經開著的 handle」：那種寫入會落進凍結檔。複製完要比兩邊內容都等於搬移前那份，
    # 對不上就把凍結檔改名回原處、這一場不搬。
    arc_hw = tmp / "archive-handlewrite"
    sid_hw = "ac1b0000-0000-4000-8000-00000000ac1b"
    _shw = _one_cmd_session(sid_hw, "2026-07-25T16:50:00.000Z")
    _src_hw = proj / f"{sid_hw}.jsonl"
    _orig_hw = _src_hw.read_bytes()

    def _copy_then_append_src(src, dst, *a, **k):
        r = _real_copyfile(src, dst, *a, **k)
        with open(src, "ab") as fh:                     # 寫進「被複製的那個檔」
            fh.write(_LATE)
        return r

    _v.shutil.copyfile = _copy_then_append_src
    try:
        with _cross_device(arc_hw):
            _mhw, _skhw = _arc_one(sid_hw, _shw, arc_hw)
    finally:
        _v.shutil.copyfile = _real_copyfile
    assert not _mhw, f"凍結檔在複製後被寫過，不可以算搬成功：{_mhw}"
    assert _src_hw.is_file() and _src_hw.read_bytes() == _orig_hw + _LATE, (
        "⚠⚠ 寫進凍結檔的那一行不見了——凍結檔要改名回原處，內容一個位元組都不可以少")
    assert not [p for p in arc_hw.glob("*.jsonl") if p.name != "_archived.jsonl"], (
        "來源原處完整保有內容時，目的地那份複本是多餘的，要收掉（否則下一次撞「已有同名檔」）")
    assert not list(proj.glob("*.archiving")), "凍結檔要改名回原處，不可以留在旁邊"

    # --- ⚠⚠ 跨磁碟：**比對完、刪凍結檔之前**依路徑追加 ⇒ 那一行要落在原處的新檔 -------------------
    # 「複製完比對兩邊內容」擋得住複製途中的寫入，擋不住比對完到刪掉之間那一刻；先改名凍結的用處就在這裡：
    # 那一刻依路徑寫入的東西落在原處一個新建的檔，不會跟著被刪的那個檔一起消失。
    # （上一格的注入落在複製途中，比對會先擋下來——拿掉凍結也照樣綠，那一格驗不到這件事。）
    arc_ul = tmp / "archive-late-unlink"
    sid_ul = "ac250000-0000-4000-8000-00000000ac25"
    _sul = _one_cmd_session(sid_ul, "2026-07-25T18:40:00.000Z")
    _src_ul = proj / f"{sid_ul}.jsonl"
    _real_unlink, _ul_hits = _v.os.unlink, []

    def _append_then_unlink(path, *a, **k):
        if not _ul_hits and Path(path).name.startswith(sid_ul):
            _ul_hits.append(Path(path).name)
            with open(_src_ul, "ab") as fh:             # 依路徑追加（比對完、刪掉之前）
                fh.write(_LATE)
        return _real_unlink(path, *a, **k)

    _v.os.unlink = _append_then_unlink
    try:
        with _cross_device(arc_ul):
            _mul, _skul = _arc_one(sid_ul, _sul, arc_ul)
    finally:
        _v.os.unlink = _real_unlink
    assert _ul_hits, "前提：注入的寫入要真的落在刪掉來源（凍結檔）之前"
    assert _mul, f"前提：這一場要搬成功：{_skul}"
    _hold_ul = [p.name for p in (_src_ul, arc_ul / f"-x-Proj__{sid_ul}.jsonl")
                if p.is_file() and _LATE in p.read_bytes()]
    assert _hold_ul, ("⚠⚠ 比對完、刪掉之前依路徑寫進來源的那一行被刪掉了——沒有先改名凍結的話，"
                      "那一刻的寫入會跟著被刪的檔一起消失")

    # --- ⚠⚠ 跨磁碟：搬移失敗時原處**已被重建** ⇒ 凍結檔改名回去不可以覆蓋它 ------------------------
    # 失敗或中斷要把凍結檔改名回原處；那一刻原處若已經有 Claude Code 重建的新檔（剛寫進一行），
    # 覆蓋上去就是把那一行刪掉。要留著凍結檔、當場講出它在哪。
    arc_rs = tmp / "archive-restore-recreated"
    sid_rs = "ac260000-0000-4000-8000-00000000ac26"
    _srs = _one_cmd_session(sid_rs, "2026-07-25T18:50:00.000Z")
    _src_rs = proj / f"{sid_rs}.jsonl"
    _orig_rs = _src_rs.read_bytes()

    def _recreate_then_fail(src, dst, *a, **k):
        with open(_src_rs, "ab") as fh:                 # 原處被重建（依路徑寫入，凍結之後）
            fh.write(_LATE)
        raise OSError("simulated copy failure after the source path was recreated")

    _err_rs = io.StringIO()
    _v.shutil.copyfile = _recreate_then_fail
    try:
        with _cross_device(arc_rs), contextlib.redirect_stderr(_err_rs):
            _mrs, _skrs = _arc_one(sid_rs, _srs, arc_rs)
    finally:
        _v.shutil.copyfile = _real_copyfile
    _stg_rs = proj / f"{sid_rs}.jsonl.archiving"
    assert not _mrs, f"複製失敗不可以算搬成功：{_mrs}"
    assert _src_rs.read_bytes() == _LATE, (
        "⚠⚠ 原處被重建的新檔（剛寫進來的那一行）被凍結檔蓋掉了——改名回原處不可以覆蓋")
    assert _stg_rs.is_file() and _stg_rs.read_bytes() == _orig_rs, "搬移前的完整內容要留在凍結檔"
    assert str(_stg_rs) in _err_rs.getvalue(), f"凍結檔留在哪要當場講出來：{_err_rs.getvalue()!r}"
    assert not [p for p in arc_rs.glob("*.jsonl") if p.name != "_archived.jsonl"], "空佔位檔要收掉"
    _stg_rs.unlink()                                    # 之後的格子不要被它干擾

    # --- ⚠⚠ 唯讀的來源——同碟與跨碟都要搬得成、不可以留下擋路的檔 ------
    # 複製會把唯讀屬性一起帶到目的地、刪來源失敗 ⇒ 清理若刪不掉那份唯讀的完整副本
    # （訊息還叫它「佔位檔」），下一次起永遠撞「目的地已有同名檔」。
    for _xd, _sid_ro, _t_ro in ((False, "ac1c0000-0000-4000-8000-00000000ac1c", "2026-07-25T17:00:00.000Z"),
                                (True, "ac1d0000-0000-4000-8000-00000000ac1d", "2026-07-25T17:10:00.000Z")):
        _arc_ro = tmp / f"archive-readonly-{'xdev' if _xd else 'same'}"
        _sro = _one_cmd_session(_sid_ro, _t_ro)
        _src_ro = proj / f"{_sid_ro}.jsonl"
        _body_ro = _src_ro.read_bytes()
        os.chmod(_src_ro, stat.S_IREAD)
        try:
            with (_cross_device(_arc_ro) if _xd else contextlib.nullcontext()):
                _mro, _skro = _arc_one(_sid_ro, _sro, _arc_ro)
        finally:
            for _p in (_src_ro, _arc_ro / f"-x-Proj__{_sid_ro}.jsonl"):
                if _p.exists():
                    os.chmod(_p, stat.S_IREAD | stat.S_IWRITE)
        assert _mro, f"唯讀的來源搬不動（{'跨碟' if _xd else '同碟'}）：{_skro}"
        assert (_arc_ro / f"-x-Proj__{_sid_ro}.jsonl").read_bytes() == _body_ro
        assert not _src_ro.exists() and not list(proj.glob("*.archiving")), (
            f"唯讀的來源搬完之後原處與凍結檔都不該還在（{'跨碟' if _xd else '同碟'}）")

    # --- 安全閘：封存目錄不可以在來源或 out/ 裡面 ---------------------------
    for bad in (proj.parent / "inside", out / "inside"):
        rr = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
             "--no-codex", "--out", str(out), "--archive-command-only", str(bad)],
            capture_output=True, text=True, encoding="utf-8")
        assert rr.returncode != 0, f"封存目錄在 {bad} 底下時應該拒絕執行"
        assert "拒絕執行" in (rr.stdout + rr.stderr), (rr.stdout + rr.stderr)[-300:]

    # --- ⚠⚠ 記帳寫不進去 ⇒ 必須在**任何一次搬移之前**停下來 ------------------
    # 「搬完才開檔記帳」的話：manifest 不可寫時，主檔已經離開 `~/.claude/projects/`、
    # 目的地有檔、**而還原依據一行都沒有**（實測）。
    # 這裡用「把 `_archived.jsonl` 做成一個目錄」來造出「開不起來」，
    # 那是跨平台都成立的做法（權限位元在 Windows 上不可靠）。
    arc2 = tmp / "archive-nowrite"
    arc2.mkdir(parents=True, exist_ok=True)
    (arc2 / "_archived.jsonl").mkdir(exist_ok=True)
    keep = sorted(p.name for p in proj.glob("*.jsonl"))
    rr = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out), "--archive-command-only", str(arc2)],
        capture_output=True, text=True, encoding="utf-8")
    assert rr.returncode != 0, "記帳寫不進去時應該拒絕執行"
    assert "拒絕執行" in (rr.stdout + rr.stderr), (rr.stdout + rr.stderr)[-300:]
    assert sorted(p.name for p in proj.glob("*.jsonl")) == keep, (
        "⚠⚠ 記帳寫不進去卻已經搬了檔——那是「來源不在、還原依據沒有」的狀態")
    assert not any(p.suffix == ".jsonl" and p.is_file() for p in arc2.iterdir()), (
        "拒絕執行時不該在封存目錄留下任何檔")

    # --- ⚠⚠ 相對來源路徑：紀錄裡的 `from` 仍然必須是絕對路徑 -------------------
    # CLI 印的是「含原始絕對路徑，可依它還原」，而 `--claude-source` 收得下相對路徑。
    # ⚠ 這一段素材**只會走到** `path.resolve()` 那一格：其他每一個案例傳的都是絕對路徑，
    #   所以把 `resolve()` 拿掉也不會有任何一格變紅（先補素材，不是先加斷言）。
    arc4 = tmp / "archive-rel"
    sid_rel = "ac090000-0000-4000-8000-00000000ac09"
    w(sid_rel, [
        dict(type="user", uuid="i1", timestamp="2026-07-25T09:00:00.000Z",
             sessionId=sid_rel, message={"role": "user", "content": CMD_U}, **base),
    ])
    rr = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", "demo=projects",
         "--no-codex", "--out", str(out), "--archive-command-only", str(arc4)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(tmp))
    assert rr.returncode == 0, f"非零退出\nSTDOUT:{rr.stdout}\nSTDERR:{rr.stderr}"
    man4 = [json.loads(x) for x in
            (arc4 / "_archived.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert man4, "相對來源那一場沒有被搬——素材沒建起來，這一格等於沒驗"
    assert all(Path(m["from"]).is_absolute() for m in man4), (
        f"⚠ 用相對 --claude-source 時，紀錄裡的來源路徑不是絕對路徑，"
        f"還原時找不到原位：{[m['from'] for m in man4]}")

    # --- ⚠⚠ 記帳「已經打開、但寫到一半才失敗」------------------------------
    # 上面那一格驗的是「一開始就開不起來」。**中途失敗是另一條路**：
    # 只有 `shutil.move` 那一段有 cleanup 的話 ⇒ 注入一次 `fsync` 失敗實測到
    # `source=True target=True target_size=0 html=False md=False`
    # ——頁面已刪、目的地留下 0 位元組佔位檔，下一次執行永遠撞「已有同名檔」。
    # ⚠ 這一格只能在函式層驗（子行程注入不了例外）。
    arc5 = tmp / "archive-fsyncfail"
    sid_fs = "ac0c0000-0000-4000-8000-00000000ac0c"
    w(sid_fs, [
        dict(type="user", uuid="l1", timestamp="2026-07-25T12:00:00.000Z",
             sessionId=sid_fs, message={"role": "user", "content": CMD_U}, **base),
    ])
    run()                                        # 讓它有頁面可刪
    _s = _v.load_session(proj / f"{sid_fs}.jsonl", "demo-proj", "demo", _v.SOURCE_CLAUDE)
    _v.analyze(_s)
    _orig_fsync = _v.os.fsync
    _v.os.fsync = lambda fd: (_ for _ in ()).throw(OSError("injected fsync failure"))
    try:
        _moved, _sk = _v.archive_command_only(
            [(_v.SOURCE_CLAUDE, "demo", "demo-proj", proj / f"{sid_fs}.jsonl", _s)],
            arc5, [proj.parent], out)
    finally:
        _v.os.fsync = _orig_fsync
    assert not _moved, f"記帳寫到一半失敗時不該算搬成功：{_moved}"
    assert (proj / f"{sid_fs}.jsonl").is_file(), (
        "⚠⚠ 記帳中途失敗，來源必須留在原地")
    _stray = [p for p in arc5.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _stray, (
        f"⚠⚠ 失敗路徑留下了 `O_EXCL` 的 0 位元組佔位檔：{[p.name for p in _stray]}"
        "——下一次執行會撞到「目的地已有同名檔」而永遠搬不動這一場")
    assert any("紀錄寫不進去" in k for k in _sk), (
        f"記帳中途失敗要回報，不可以靜默略過：{_sk}")

    # --- ⚠⚠ 搬到一半失敗：目的地是**部分內容**，不是 0 位元組 -----------------
    # 跨磁碟的搬移是「複製＋刪來源」，複製寫到一半才失敗時目的地有半截檔。
    # cleanup 若只刪 0 位元組的 ⇒ 那個半截檔留下來，下一次永遠撞「已有同名檔」
    # （實測 `target_size=7`）。⚠ 這一格只能在函式層造。
    arc6 = tmp / "archive-partial"
    sid_pt = "ac0d0000-0000-4000-8000-00000000ac0d"
    _s2 = _one_cmd_session(sid_pt, "2026-07-25T13:00:00.000Z")
    _orig_pt = (proj / f"{sid_pt}.jsonl").read_bytes()

    def _partial_copy(src, dst, *a, **k):
        Path(dst).write_text("PARTIAL", encoding="utf-8")   # 複製寫了一半…
        raise OSError("injected after partial destination write")

    _v.shutil.copyfile = _partial_copy
    try:
        with _cross_device(arc6):
            _moved2, _sk2 = _arc_one(sid_pt, _s2, arc6)
    finally:
        _v.shutil.copyfile = _real_copyfile
    assert not _moved2, f"搬到一半失敗不該算成功：{_moved2}"
    assert (proj / f"{sid_pt}.jsonl").read_bytes() == _orig_pt, (
        "搬到一半失敗，來源必須原封不動留在原地（凍結檔要改名回去）")
    _left = [p for p in arc6.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _left, (
        f"⚠⚠ 目的地留下寫到一半的檔：{[(p.name, p.stat().st_size) for p in _left]}"
        "——下一次執行會撞「已有同名檔」而永遠搬不動這一場。"
        "cleanup 不可以只認 0 位元組")
    assert not list(proj.glob("*.archiving")), "搬到一半失敗，凍結檔要改名回原處"

    # --- ⚠⚠ Ctrl+C 落在「搶到名字」與「寫 pending」之間 ---------------------
    # 只接 `OSError` 的話，那一刻中斷會留下一個 **0 位元組、而且 manifest 一行都沒有**
    # 的檔，之後每一次執行都撞「目的地已有同名檔」而略過這一場，**永遠搬不動**
    # （實測 `INTERRUPT_GAP` ＋ `INTERRUPT_RETRY`）。
    # ⚠ 這一格同時驗兩件：①佔位檔收掉了 ②`KeyboardInterrupt` **原樣傳出去**
    #   （吞掉它會讓 Ctrl+C 失效，那是另一個缺陷）。
    arc8 = tmp / "archive-interrupt"
    sid_ki = "ac0f0000-0000-4000-8000-00000000ac0f"
    _s4 = _one_cmd_session(sid_ki, "2026-07-25T15:00:00.000Z")
    _orig_fsync3 = _v.os.fsync
    _v.os.fsync = lambda fd: (_ for _ in ()).throw(KeyboardInterrupt("injected Ctrl+C"))
    _propagated = False
    try:
        _arc_one(sid_ki, _s4, arc8)
    except KeyboardInterrupt:
        _propagated = True
    finally:
        _v.os.fsync = _orig_fsync3
    assert _propagated, "⚠⚠ `KeyboardInterrupt` 被吞掉了——Ctrl+C 會失效"
    assert (proj / f"{sid_ki}.jsonl").is_file(), "中斷時來源必須留在原地"
    _stuck = [p for p in arc8.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _stuck, (
        f"⚠⚠ Ctrl+C 之後留下了佔位檔：{[p.name for p in _stuck]}"
        "——它沒有任何 manifest 紀錄，卻會讓之後每一次執行都撞「已有同名檔」")

    # --- ⚠⚠ Ctrl+C 落在「關閉佔位檔」那一刻 ---------------------------------
    # 只接 OSError 的話，這一刻中斷同樣留下 0 位元組的佔位檔、之後每一次都撞「已有同名檔」。
    # ⚠⚠ 注入的 close **先丟 Ctrl+C、還沒真的關**（第二次呼叫才關）：這樣才驗得到
    #   「先確保 handle 關了、再收佔位檔」那一段。注入成「先關、再丟」的話，handle 早就關了，
    #   產品碼重關的那一行拿掉也照樣綠（實測存活）。
    arc9 = tmp / "archive-interrupt-close"
    sid_cl = "ac110000-0000-4000-8000-00000000ac11"
    _s9 = _one_cmd_session(sid_cl, "2026-07-25T15:10:00.000Z")
    _real_close, _hits = _v.os.close, []

    def _ki_before_close(fd):
        if not _hits:
            _hits.append(fd)
            raise KeyboardInterrupt("injected Ctrl+C at close (handle still open)")
        _real_close(fd)

    _v.os.close = _ki_before_close
    _propagated9 = False
    try:
        _arc_one(sid_cl, _s9, arc9)
    except KeyboardInterrupt:
        _propagated9 = True
    finally:
        _v.os.close = _real_close
    assert _hits, "前提：注入的 close 要真的被呼叫到"
    try:
        os.fstat(_hits[0])
        _still_open = True
    except OSError:
        _still_open = False
    if _still_open:                          # 測試自己收掉，免得暫存目錄刪不掉
        try:
            _real_close(_hits[0])
        except OSError:
            pass
    assert _propagated9, "⚠⚠ 關檔時的 Ctrl+C 被吞掉了——Ctrl+C 會失效"
    assert not _still_open, "⚠⚠ Ctrl+C 落在關檔那一刻之後，佔位檔的 handle 沒有被關掉"
    assert (proj / f"{sid_cl}.jsonl").is_file(), "中斷時來源必須留在原地"
    _stuck9 = [p for p in arc9.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _stuck9, (
        f"⚠⚠ 關檔時 Ctrl+C 留下了佔位檔：{[p.name for p in _stuck9]}"
        "——之後每一次執行都會撞「已有同名檔」")

    # --- ⚠⚠ 搬移其實完成了、來源路徑又被重建，然後才 Ctrl+C ---------------------
    # 只看「來源還在」會把目的地刪掉——而那是原本那份的**唯一完整副本**
    # （例如 Claude Code 又往同一場寫了一行，重建出一個只有新內容的檔）。
    arc10 = tmp / "archive-recreated-source"
    sid_rc = "ac120000-0000-4000-8000-00000000ac12"
    _s10 = _one_cmd_session(sid_rc, "2026-07-25T15:20:00.000Z")
    _orig_body = (proj / f"{sid_rc}.jsonl").read_text(encoding="utf-8")

    def _move_then_recreate(src, dst, *a, **k):
        r = _real_replace(src, dst, *a, **k)                     # 搬移真的完成（同碟：原子改名）
        if _into(dst, arc10):
            Path(src).write_text("RECREATED-BY-ANOTHER-WRITER\n", encoding="utf-8")
            raise KeyboardInterrupt("injected Ctrl+C after the move completed")
        return r

    _v.os.replace = _move_then_recreate
    _propagated10, _err10 = False, io.StringIO()
    try:
        with contextlib.redirect_stderr(_err10):
            _arc_one(sid_rc, _s10, arc10)
    except KeyboardInterrupt:
        _propagated10 = True
    finally:
        _v.os.replace = _real_replace
    assert _propagated10, "⚠⚠ Ctrl+C 被吞掉了"
    _kept = [p for p in arc10.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert _kept and _kept[0].read_text(encoding="utf-8") == _orig_body, (
        "⚠⚠ 搬移已完成、來源路徑被重建之後的 Ctrl+C 把目的地刪掉了——"
        f"那是原本那份的唯一完整副本。目的地現況：{[(p.name, p.stat().st_size) for p in _kept]}")
    assert "完整副本" in _err10.getvalue(), (
        f"保留下來的目的地是完整的，訊息要照實講出來：{_err10.getvalue()!r}")

    # --- 對照組：複製到一半就 Ctrl+C（來源沒被動過）⇒ 半截檔照樣要收掉 ----------
    # 身分檢查不可以變成「什麼都不敢刪」：來源還是搬之前那一個時，目的地只是半截檔。
    arc11 = tmp / "archive-partial-interrupt"
    sid_pk = "ac130000-0000-4000-8000-00000000ac13"
    _s11 = _one_cmd_session(sid_pk, "2026-07-25T15:30:00.000Z")
    _orig_pk = (proj / f"{sid_pk}.jsonl").read_bytes()

    def _partial_then_ki(src, dst, *a, **k):
        Path(dst).write_text("PARTIAL", encoding="utf-8")
        raise KeyboardInterrupt("injected Ctrl+C mid-copy")

    _v.shutil.copyfile = _partial_then_ki
    try:
        with _cross_device(arc11):
            try:
                _arc_one(sid_pk, _s11, arc11)
            except KeyboardInterrupt:
                pass
    finally:
        _v.shutil.copyfile = _real_copyfile
    assert (proj / f"{sid_pk}.jsonl").read_bytes() == _orig_pk, "複製到一半中斷，來源必須原封不動留在原地"
    _half = [p for p in arc11.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _half, (
        f"複製到一半的半截檔沒有收掉：{[(p.name, p.stat().st_size) for p in _half]}"
        "——身分檢查不可以把該刪的也擋掉")
    assert not list(proj.glob("*.archiving")), "中斷時凍結檔要改名回原處"

    # --- ⚠⚠ 凍結檔被**同長度改寫、mtime 又被設回去**：metadata 全一樣，內容卻不一樣 ----------
    # 只比 (裝置, inode, 大小, mtime) 的話會被騙過而刪掉目的地——那是原本那份的唯一完整副本。
    arc12 = tmp / "archive-sameshape-rewrite"
    sid_sw = "ac140000-0000-4000-8000-00000000ac14"
    _s12 = _one_cmd_session(sid_sw, "2026-07-25T15:40:00.000Z")
    _src12 = proj / f"{sid_sw}.jsonl"
    _orig12 = _src12.read_bytes()

    def _copy_then_forge(src, dst, *a, **k):
        _real_copyfile(src, dst, *a, **k)                         # 完整複製到目的地
        _st = os.stat(src)
        _forged = bytes(b ^ 0x01 if 97 <= b <= 122 else b for b in _orig12)   # 同長度、不同內容
        Path(src).write_bytes(_forged)                            # 開著的 handle 把被複製的那個檔改寫
        os.utime(src, ns=(_st.st_atime_ns, _st.st_mtime_ns))    # mtime 設回去
        raise KeyboardInterrupt("injected Ctrl+C after a same-shape rewrite")

    _v.shutil.copyfile = _copy_then_forge
    _err12 = io.StringIO()
    try:
        with _cross_device(arc12), contextlib.redirect_stderr(_err12):
            try:
                _arc_one(sid_sw, _s12, arc12)
            except KeyboardInterrupt:
                pass
    finally:
        _v.shutil.copyfile = _real_copyfile
    _kept12 = [p for p in arc12.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert _kept12 and _kept12[0].read_bytes() == _orig12, (
        "⚠⚠ 來源被同長度改寫、mtime 設回之後，清理把目的地刪了——那是原本內容唯一的副本")
    assert "目的地保留" in _err12.getvalue(), (
        "保留目的地時要**當場**印出來（Ctrl+C 會直接往外拋，排隊的訊息來不及印）："
        f"{_err12.getvalue()!r}")

    # --- 對照：只動了 mtime、內容沒變（半截目的地）⇒ 內容一樣就可以刪，不可以留下擋路的檔 ------
    arc13 = tmp / "archive-touch-only"
    sid_to = "ac150000-0000-4000-8000-00000000ac15"
    _s13 = _one_cmd_session(sid_to, "2026-07-25T15:50:00.000Z")

    def _partial_touch_ki(src, dst, *a, **k):
        Path(dst).write_text("PARTIAL", encoding="utf-8")
        os.utime(src, None)                                       # 只動 mtime
        raise KeyboardInterrupt("injected Ctrl+C after a metadata-only change")

    _v.shutil.copyfile = _partial_touch_ki
    try:
        with _cross_device(arc13):
            try:
                _arc_one(sid_to, _s13, arc13)
            except KeyboardInterrupt:
                pass
    finally:
        _v.shutil.copyfile = _real_copyfile
    _left13 = [p for p in arc13.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _left13, (
        "來源內容沒變（只動了 mtime）時，半截目的地要收掉——留著會擋住之後每一次執行："
        f"{[(p.name, p.stat().st_size) for p in _left13]}")

    # --- 判定時讀得到、搬之前讀不到來源內容（沒辦法確認）⇒ 不搬，空佔位檔照樣要收 ----
    # 「沒辦法確認就保留目的地」只適用於有內容的目的地；0 位元組的佔位檔不可能是任何東西的副本。
    arc14 = tmp / "archive-unreadable-source"
    sid_ur = "ac160000-0000-4000-8000-00000000ac16"
    _s14 = _one_cmd_session(sid_ur, "2026-07-25T16:00:00.000Z")
    _cls14 = _v._file_ident(proj / f"{sid_ur}.jsonl")
    _orig_ident = _v._file_ident
    _v._file_ident = lambda p: None
    try:
        _m14, _sk14 = _arc_one(sid_ur, _s14, arc14, _cls14)
    finally:
        _v._file_ident = _orig_ident
    assert not _m14, f"搬之前讀不到來源，不可以搬：{_m14}"
    assert (proj / f"{sid_ur}.jsonl").is_file(), "來源必須留在原地"
    _left14 = [p for p in arc14.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _left14, (
        "沒搬的時候，空的佔位檔要收掉（它不可能是任何內容的副本）："
        f"{[(p.name, p.stat().st_size) for p in _left14]}")

    # --- Ctrl+C 落在「量來源內容」那一刻：名字已經搶到、pending 也寫了 ⇒ 佔位檔要收掉 -----------
    arc15 = tmp / "archive-interrupt-hash"
    sid_hk = "ac170000-0000-4000-8000-00000000ac17"
    _s15 = _one_cmd_session(sid_hk, "2026-07-25T16:10:00.000Z")
    _cls15 = _v._file_ident(proj / f"{sid_hk}.jsonl")

    def _hash_ki(p):
        raise KeyboardInterrupt("injected Ctrl+C while hashing the source")

    _v._file_ident = _hash_ki
    _prop15 = False
    try:
        _arc_one(sid_hk, _s15, arc15, _cls15)
    except KeyboardInterrupt:
        _prop15 = True
    finally:
        _v._file_ident = _orig_ident
    assert _prop15, "算雜湊時的 Ctrl+C 被吞掉了"
    assert (proj / f"{sid_hk}.jsonl").is_file(), "中斷時來源必須留在原地"
    assert any(r.get("state") == "pending" for r in _journal_rows(arc15)), (
        "前提：這一格要真的走到「pending 已寫、搬之前量內容」那一刻，否則等於沒驗")
    _left15 = [p for p in arc15.glob("*.jsonl") if p.name != "_archived.jsonl"]
    assert not _left15, (
        f"算雜湊時 Ctrl+C 留下了佔位檔：{[p.name for p in _left15]}——之後每一次都會撞「已有同名檔」")

    # --- ⚠⚠ 判定之後、搬移之前，那一場多了一則真的問答 ⇒ 不可以搬----------
    # 判定與搬移之間隔著整批的讀取；這段時間 Claude Code 往同一場寫了真的對話，
    # 照樣把它搬走的話，`claude --resume` 的清單就少了一場**有對話**的 session。
    _TALK = [
        {"type": "user", "uuid": "rt1", "timestamp": "2026-07-25T17:30:10.000Z",
         "message": {"role": "user", "content": "這是判定之後才寫進來的提問REALTALK。"}, **base},
        {"type": "assistant", "uuid": "rt2", "timestamp": "2026-07-25T17:30:20.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mrt2",
                     "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "text", "text": "回覆REALTALK。"}]}, **base},
    ]
    _TALK_LINES = "\n" + "\n".join(json.dumps(e, ensure_ascii=False) for e in _TALK)
    arc16 = tmp / "archive-changed-after-classify"
    sid_cc = "ac1e0000-0000-4000-8000-00000000ac1e"
    _scc = _one_cmd_session(sid_cc, "2026-07-25T17:30:00.000Z")
    _src_cc = proj / f"{sid_cc}.jsonl"
    _cls_cc = _v._file_ident(_src_cc)                 # 判定時那一份
    with open(_src_cc, "a", encoding="utf-8") as fh:
        fh.write(_TALK_LINES)
    os.utime(_src_cc, (_OLD, _OLD))      # 不讓「最近有寫入」那道閘先擋下：這一格驗的是內容比對
    _m16, _sk16 = _arc_one(sid_cc, _scc, arc16, _cls_cc)
    assert not _m16, f"⚠⚠ 判定之後才補上對話的那一場被搬走了：{_m16}"
    assert "REALTALK" in _src_cc.read_text(encoding="utf-8"), "來源要原封不動留在原地"
    assert not [p for p in arc16.glob("*.jsonl") if p.name != "_archived.jsonl"], "不搬就不可以留下佔位檔"
    assert any("判定之後" in k for k in _sk16), f"不搬的理由要講出來：{_sk16}"

    # --- ⚠⚠ 同一件事走 CLI：`main()` 判定時量的內容身分要真的傳進去 --------------------
    # 沒傳的話迴圈會當場重量，而那時檔案已經是新內容了——比對永遠相等，這道閘形同虛設。
    arc17 = tmp / "archive-changed-after-classify-cli"
    sid_cm = "ac1f0000-0000-4000-8000-00000000ac1f"
    w(sid_cm, [dict(type="user", uuid="cm1", timestamp="2026-07-25T17:40:00.000Z", sessionId=sid_cm,
                    message={"role": "user", "content": CMD_U}, **base)])
    run()
    _src_cm = proj / f"{sid_cm}.jsonl"
    _code17 = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import ai_session_viewer as v\n"
        "_real = v.archive_command_only\n"
        "def _wrapped(sessions, *a, **k):\n"
        f"    with open({str(_src_cm)!r}, 'a', encoding='utf-8') as fh:\n"
        f"        fh.write({_TALK_LINES.replace('REALTALK', 'CLITALK')!r})\n"
        f"    os.utime({str(_src_cm)!r}, ({_OLD!r}, {_OLD!r}))\n"
        "    return _real(sessions, *a, **k)\n"
        "v.archive_command_only = _wrapped\n"
        f"sys.argv = ['ai_session_viewer.py', '--claude-source', {('demo=' + str(proj.parent))!r},"
        f" '--no-codex', '--out', {str(out)!r}, '--format', 'both', '--archive-command-only', {str(arc17)!r}]\n"
        "v.main()\n")
    rr = subprocess.run([sys.executable, "-c", _code17], capture_output=True, text=True,
                        encoding="utf-8", cwd=str(tmp))
    assert rr.returncode == 0, f"非零退出\nSTDOUT:{rr.stdout}\nSTDERR:{rr.stderr}"
    assert _src_cm.is_file() and "CLITALK" in _src_cm.read_text(encoding="utf-8"), (
        "⚠⚠ 走 CLI 時，判定之後才補上對話的那一場被搬走了——`main()` 量的內容身分沒有傳進封存")
    assert not (arc17 / f"-x-Proj__{sid_cm}.jsonl").exists()

    # --- ⚠⚠ 判定**期間**（讀檔途中）就被寫過 ⇒ 不拿那一刻的內容當基準、這次不搬 ---------------------
    # 讀檔與量身分之間有窗：讀的時候只有指令、量的時候已經多了一則真的問答 ⇒ 量到的是新內容，
    # 搬之前再量一次當然相等，有對話的那場就被搬走了。`main()` 讀前讀後各 stat 一次，對不上就不搬。
    # （上一格的寫入落在判定**之後**，量到的是舊內容——拿掉讀前讀後那道比對也照樣綠，驗不到這件事。）
    arc22 = tmp / "archive-changed-during-classify"
    sid_dc = "ac270000-0000-4000-8000-00000000ac27"
    w(sid_dc, [dict(type="user", uuid="dc1", timestamp="2026-07-25T19:00:00.000Z", sessionId=sid_dc,
                    message={"role": "user", "content": CMD_U}, **base)])
    run()
    _src_dc = proj / f"{sid_dc}.jsonl"
    _code22 = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import ai_session_viewer as v\n"
        "_real, _done = v.load_session, []\n"
        "def _wrapped(path, *a, **k):\n"
        "    s = _real(path, *a, **k)\n"
        f"    if not _done and v.Path(path).name == {_src_dc.name!r}:\n"
        "        _done.append(1)\n"
        f"        with open({str(_src_dc)!r}, 'a', encoding='utf-8') as fh:\n"
        f"            fh.write({_TALK_LINES.replace('REALTALK', 'DURINGTALK')!r})\n"
        f"        os.utime({str(_src_dc)!r}, ({_OLD!r}, {_OLD!r}))\n"
        "    return s\n"
        "v.load_session = _wrapped\n"
        f"sys.argv = ['ai_session_viewer.py', '--claude-source', {('demo=' + str(proj.parent))!r},"
        f" '--no-codex', '--out', {str(out)!r}, '--format', 'both', '--archive-command-only', {str(arc22)!r}]\n"
        "v.main()\n")
    rr = subprocess.run([sys.executable, "-c", _code22], capture_output=True, text=True,
                        encoding="utf-8", cwd=str(tmp))
    assert rr.returncode == 0, f"非零退出\nSTDOUT:{rr.stdout}\nSTDERR:{rr.stderr}"
    assert _src_dc.is_file() and "DURINGTALK" in _src_dc.read_text(encoding="utf-8"), (
        "⚠⚠ 判定期間（讀檔途中）才補上對話的那一場被搬走了——讀前讀後的 stat 對不上，就不可以拿那一刻量到的內容當基準")
    assert not (arc22 / f"-x-Proj__{sid_dc}.jsonl").exists()
    assert "判定期間" in (rr.stdout + rr.stderr), f"不搬的理由要講出來：{(rr.stdout + rr.stderr)[-400:]}"

    # --- ⚠⚠ 最近還有寫入的 session 不搬（可能還開著）------------------------------
    # 用 `!` 下封存指令的那一場：判定時只有一行 `<bash-input>`（指令還在跑）⇒ 被判成只有指令。
    # 搬走一場還開著的檔，Claude Code 下一次寫入會在原路徑重建新檔，對話被切成兩半。
    arc18 = tmp / "archive-recent"
    sid_rec = "ac200000-0000-4000-8000-00000000ac20"
    _p_rec = proj / f"{sid_rec}.jsonl"
    _p_rec.write_text(json.dumps(dict(type="user", uuid="rc1", timestamp="2026-07-25T17:50:00.000Z",
                                      sessionId=sid_rec, message={"role": "user", "content": CMD_U},
                                      **base), ensure_ascii=False), encoding="utf-8")   # ⚠ 刻意不撥 mtime
    r18 = run("--archive-command-only", str(arc18))
    assert _p_rec.is_file(), "⚠⚠ 剛寫過的 session（可能還開著）被搬走了"
    assert "可能還開著" in (r18.stdout + r18.stderr), "不搬的理由要講出來"
    os.utime(_p_rec, (_OLD, _OLD))                    # 之後的格子不要被它干擾

    # --- ⚠⚠ 主檔已經搬到目的地之後才 Ctrl+C ⇒ 一定要記帳、一定要講出來--------
    # 紀錄若停在 `pending`、stderr 一個字都沒有——使用者看到的是「什麼都沒發生」，
    # 實際上那一場已經不在 `~/.claude/projects/`。
    arc19 = tmp / "archive-cut-after-move"
    sid_cut = "ac210000-0000-4000-8000-00000000ac21"
    _scut = _one_cmd_session(sid_cut, "2026-07-25T18:00:00.000Z")
    _orig_cut = (proj / f"{sid_cut}.jsonl").read_bytes()

    def _replace_then_ki(src, dst, *a, **k):
        r = _real_replace(src, dst, *a, **k)
        if _into(dst, arc19):
            raise KeyboardInterrupt("injected Ctrl+C right after the move landed")
        return r

    def _move_then_ki(src, dst, *a, **k):        # 搬移走 shutil.move 的版本：同一個注入點
        r = _real_move(src, dst, *a, **k)
        if _into(dst, arc19):
            raise KeyboardInterrupt("injected Ctrl+C right after the move landed")
        return r

    _v.os.replace, _v.shutil.move = _replace_then_ki, _move_then_ki
    _err19, _prop19 = io.StringIO(), False
    try:
        with contextlib.redirect_stderr(_err19):
            _arc_one(sid_cut, _scut, arc19)
    except KeyboardInterrupt:
        _prop19 = True
    finally:
        _v.os.replace, _v.shutil.move = _real_replace, _real_move
    assert _prop19, "Ctrl+C 被吞掉了"
    _t19 = arc19 / f"-x-Proj__{sid_cut}.jsonl"
    assert _t19.is_file() and _t19.read_bytes() == _orig_cut, "前提：主檔已經在目的地"
    assert any(r.get("state") == "moved" and r.get("to", "").endswith(_t19.name)
               for r in _journal_rows(arc19)), (
        "⚠⚠ 主檔已經搬到目的地之後的 Ctrl+C，紀錄卻停在 pending——還原的人不知道它搬完了沒")
    assert "封存中斷" in _err19.getvalue() and _t19.name in _err19.getvalue(), (
        f"主檔已搬走卻被中斷，要當場講出它在哪裡：{_err19.getvalue()!r}")

    # --- ⚠⚠ 子代理目錄搬到一半 Ctrl+C ⇒ 記帳寫兩處位置、當場講出來----------
    arc20 = tmp / "archive-cut-sidechain"
    sid_sc = "ac220000-0000-4000-8000-00000000ac22"
    _ssc = _one_cmd_session(sid_sc, "2026-07-25T18:10:00.000Z")
    _side = proj / sid_sc
    (_side / "subagents").mkdir(parents=True)
    (_side / "subagents" / "agent-a1.jsonl").write_text("{}", encoding="utf-8")
    (_side / "x1.json").write_text("{}", encoding="utf-8")
    (_side / "x2.json").write_text("{}", encoding="utf-8")
    _n_side = []

    def _move_side_then_ki(src, dst, *a, **k):
        if Path(src).resolve().parent == _side.resolve():
            _n_side.append(src)
            if len(_n_side) == 2:
                raise KeyboardInterrupt("injected Ctrl+C mid sub-agent move")
        return _real_move(src, dst, *a, **k)

    _v.shutil.move = _move_side_then_ki
    _err20, _prop20 = io.StringIO(), False
    try:
        with contextlib.redirect_stderr(_err20):
            _arc_one(sid_sc, _ssc, arc20)
    except KeyboardInterrupt:
        _prop20 = True
    finally:
        _v.shutil.move = _real_move
    assert _prop20 and len(_n_side) == 2, "前提：Ctrl+C 要真的落在子代理目錄搬到一半"
    _mv20 = [r for r in _journal_rows(arc20) if r.get("state") == "moved"]
    assert _mv20, "⚠⚠ 子代理目錄搬到一半被中斷：主檔已經搬走了，紀錄卻停在 pending"
    assert ("中斷" in _mv20[-1].get("sidechain", "") and _mv20[-1].get("sidechain_from")
            and _mv20[-1].get("sidechain_to")), (
        f"子代理目錄一半在來源、一半在封存目錄：紀錄要寫明中斷與兩處位置：{_mv20[-1]}")
    assert "封存中斷" in _err20.getvalue() and str(_side) in _err20.getvalue(), (
        f"子代理目錄被切成兩半，要當場講出兩處位置：{_err20.getvalue()!r}")

    # --- ⚠⚠ 量完內容之後來源又被**追加**、複製到一半 Ctrl+C ⇒ 來源仍完整，半截檔要收、不可以亂講 ----
    # 保留 7 位元組的半截檔、還說它「可能是原本那份的唯一完整副本」是錯的——
    # 其實來源完整（只是多了一行），而那個半截檔會擋住之後每一次執行。
    _APP = ("\n" + json.dumps({"type": "user", "uuid": "ap1", "timestamp": "2026-07-25T18:21:00.000Z",
                               "message": {"role": "user", "content": CMD_U}, **base})).encode()
    def _cut_after_touch(_kind, _sid4, _t4):
        _arc4b = tmp / f"archive-{_kind}-then-cut"
        _s4b = _one_cmd_session(_sid4, _t4)
        _src4b = proj / f"{_sid4}.jsonl"
        _orig4b = _src4b.read_bytes()

        def _touch_src(p, _kind=_kind):
            if _kind == "append":
                with open(p, "ab") as fh:
                    fh.write(_APP)
            else:                                         # 不是追加：開頭就不一樣了
                Path(p).write_bytes(b'{"type":"user","uuid":"rw1"}\n')

        def _replace_touch_xdev(src, dst, *a, _arc=_arc4b, **k):
            if _into(dst, _arc):
                _touch_src(src)                           # 量完內容之後、搬之前
                raise OSError(_errno.EXDEV, "simulated cross-device move")
            return _real_replace(src, dst, *a, **k)

        def _partial_ki(src, dst, *a, **k):
            Path(dst).write_bytes(b"PARTIAL")
            raise KeyboardInterrupt("injected Ctrl+C mid-copy")

        def _move_touch_partial_ki(src, dst, *a, _arc=_arc4b, **k):    # 搬移走 shutil.move 的版本
            if _into(dst, _arc):
                _touch_src(src)
                Path(dst).write_bytes(b"PARTIAL")
                raise KeyboardInterrupt("injected Ctrl+C mid-copy")
            return _real_move(src, dst, *a, **k)

        _v.os.replace, _v.shutil.copyfile, _v.shutil.move = (
            _replace_touch_xdev, _partial_ki, _move_touch_partial_ki)
        _err4b = io.StringIO()
        try:
            with contextlib.redirect_stderr(_err4b):
                try:
                    _arc_one(_sid4, _s4b, _arc4b)
                except KeyboardInterrupt:
                    pass
        finally:
            _v.os.replace, _v.shutil.copyfile, _v.shutil.move = _real_replace, _real_copyfile, _real_move
        _left4b = [p for p in _arc4b.glob("*.jsonl") if p.name != "_archived.jsonl"]
        if _kind == "append":
            assert _src4b.read_bytes() == _orig4b + _APP, "來源（含追加的那一行）要完整留在原地"
            assert not _left4b, (
                "來源開頭仍完整保有搬移前那份內容時，半截目的地是多餘的、要收掉（否則永遠撞「已有同名檔」）："
                f"{[(p.name, p.stat().st_size) for p in _left4b]}")
            assert "完整副本" not in _err4b.getvalue(), (
                f"⚠⚠ 不可以把半截檔說成完整副本：{_err4b.getvalue()!r}")
        else:
            assert _left4b, "來源被改寫（不是追加）時，目的地可能是那份內容唯一的殘留，不可以刪"
            assert "可能不完整" in _err4b.getvalue(), (
                f"⚠⚠ 保留的目的地是半截檔，訊息要照實講「可能不完整」，不可以說是完整副本：{_err4b.getvalue()!r}")

    _cut_after_touch("append", "ac230000-0000-4000-8000-00000000ac23", "2026-07-25T18:20:00.000Z")

    # --- ⚠⚠ 量完內容之後來源被**改寫**（不是追加）、複製到一半 Ctrl+C ⇒ 保留目的地，但要講「可能不完整」 ----
    _cut_after_touch("rewrite", "ac240000-0000-4000-8000-00000000ac24", "2026-07-25T18:30:00.000Z")

    # --- ⚠⚠ cleanup **自己**失敗時要講出來，不可以吞掉 ----------------------
    # 可攜的做法：不靠 Windows 檔案鎖，**monkeypatch `Path.unlink` 丟 `PermissionError`**。
    # ⚠ 這一格守的是「輸出會不會告訴你下一次會撞牆」，不是「有沒有刪掉」。
    arc7 = tmp / "archive-unlinkfail"
    sid_uf = "ac0e0000-0000-4000-8000-00000000ac0e"
    w(sid_uf, [
        dict(type="user", uuid="n1", timestamp="2026-07-25T14:00:00.000Z",
             sessionId=sid_uf, message={"role": "user", "content": CMD_U}, **base),
    ])
    run()
    _s3 = _v.load_session(proj / f"{sid_uf}.jsonl", "demo-proj", "demo", _v.SOURCE_CLAUDE)
    _v.analyze(_s3)
    _orig_fsync2, _orig_unlink = _v.os.fsync, Path.unlink
    _v.os.fsync = lambda fd: (_ for _ in ()).throw(OSError("injected fsync failure"))
    Path.unlink = lambda self, *a, **k: (_ for _ in ()).throw(
        PermissionError("injected unlink failure"))
    try:
        _m3, _sk3 = _v.archive_command_only(
            [(_v.SOURCE_CLAUDE, "demo", "demo-proj", proj / f"{sid_uf}.jsonl", _s3)],
            arc7, [proj.parent], out)
    finally:
        _v.os.fsync, Path.unlink = _orig_fsync2, _orig_unlink
    assert not _m3, f"記帳失敗不該算搬成功：{_m3}"
    assert (proj / f"{sid_uf}.jsonl").is_file(), "來源必須留在原地"
    assert any("佔位檔清不掉" in k for k in _sk3), (
        f"⚠⚠ cleanup 自己失敗被吞掉了——使用者不會知道下一次會撞「已有同名檔」：{_sk3}")

    # --- ⚠⚠ 頁面刪不掉 ⇒ 整場不搬（否則留下看不見、仍含對話的孤兒頁）--------
    # 造法：把那一場的 HTML 換成**目錄**，`unlink()` 就會丟 OSError。
    arc3 = tmp / "archive-lockedpage"
    # ⚠ 需要一場**還在、而且該搬**的 session：①③ 在上面已經被搬走了，
    # ⑥⑦ 又刻意不該搬 ⇒ 這條路沒有素材。現造一場。
    sid_lock = "ac080000-0000-4000-8000-00000000ac08"
    w(sid_lock, [
        dict(type="user", uuid="h1", timestamp="2026-07-25T08:00:00.000Z",
             sessionId=sid_lock, message={"role": "user", "content": CMD_U}, **base),
    ])
    run()                                        # 產出它的頁面
    victim = None
    for p in _session_pages(out):
        if p.suffix == ".html" and sid_lock[:8] in p.name:
            victim = p
            break
    assert victim is not None, "找不到要鎖住的那一頁——素材沒建起來，這一格等於沒驗"
    victim.unlink()
    victim.mkdir()
    rr = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={proj.parent}",
         "--no-codex", "--out", str(out), "--archive-command-only", str(arc3)],
        capture_output=True, text=True, encoding="utf-8")
    assert (proj / f"{sid_lock}.jsonl").is_file(), (
        "⚠⚠ 頁面刪不掉卻照搬——磁碟上會留下索引連不到、但仍含私密對話的孤兒頁")
    assert "頁面刪不掉" in (rr.stdout + rr.stderr), (
        f"頁面刪不掉要回報，不可以靜默吞掉：{(rr.stdout + rr.stderr)[-400:]}")
    victim.rmdir()

    print("OK: archive command-only sessions test passed")


def test_build_interrupt_residue(tmp_path=None):
    """建置被中斷（Ctrl+C／硬終止）之後**不可以留下半截或擋路的檔**。

    ① 硬終止留下的頁面暫存檔（`*.html.tmp`）：下一次建置開頭要清掉——指定來源的建置會略過孤兒清理、
       那一場又被沿用，半截暫存檔因此會**永遠**留著。不是本工具命名的檔不碰。
    ② Ctrl+C 落在寫頁面暫存檔途中：暫存檔當場收掉（不可以只接 `Exception`）。
    ③ 索引頁與 manifest 每次都重寫：中斷時正式檔要嘛完整的舊版、要嘛完整的新版
       （直接寫正式檔的話，中斷就留下截斷的索引）。
    """
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "res-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "f0f00000-0000-4000-8000-00000000f0ff"
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        {"type": "user", "uuid": "r1", "timestamp": "2026-09-30T01:00:00.000Z", "cwd": "/x/Res",
         "sessionId": sid, "message": {"role": "user", "content": "提問RESIDUEQ。"}},
        {"type": "assistant", "uuid": "r2", "timestamp": "2026-09-30T01:00:05.000Z", "sessionId": sid,
         "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "mr2",
                     "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "text", "text": "回覆RESIDUEA。"}]}},
    ]), encoding="utf-8")
    out = tmp / "out"

    def build(*extra, patch=""):
        code = ("import sys, pathlib\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import ai_session_viewer as v\n" + patch +
                f"sys.argv = ['ai_session_viewer.py', '--claude-source', {('demo=' + str(tmp / 'projects'))!r},"
                f" '--no-codex', '--out', {str(out)!r}, *{list(extra)!r}]\n"
                "v.main()\n")
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def ki_on(names):
        """`Path.write_text` 寫到這些檔名時，寫一半就丟 Ctrl+C（中斷落在寫檔途中）。`*x` ＝ 結尾是 x。"""
        return ("_orig_wt = pathlib.Path.write_text\n"
                f"_PATS = {list(names)!r}\n"
                "def _wt(self, data, *a, **k):\n"
                "    if any(self.name.endswith(p[1:]) if p.startswith('*') else self.name == p for p in _PATS):\n"
                "        _orig_wt(self, data[:len(data) // 2], *a, **k)\n"
                "        raise KeyboardInterrupt('injected Ctrl+C mid write')\n"
                "    return _orig_wt(self, data, *a, **k)\n"
                "pathlib.Path.write_text = _wt\n")

    r = build()
    assert r.returncode == 0, f"前提：第一次建置要成功\n{r.stderr[-500:]}"
    page = next(p for p in _session_pages(out) if sid[:8] in p.name)
    page_md = page.with_suffix(".md")
    assert page_md.is_file(), "前提：MD 也要建出來"

    # --- ① 硬終止留下的半截暫存檔：下一次建置開頭要清掉；不是本工具命名的不碰 ------------------
    strays = [page.with_name(page.name + ".tmp"), page_md.with_name(page_md.name + ".tmp"),
              out / "index.html.tmp", out / ".build-manifest.json.tmp"]
    for p in strays:
        p.write_text("<half-written>", encoding="utf-8")
    keep = out / "notes.tmp"
    keep.write_text("not ours", encoding="utf-8")
    r = build()
    assert r.returncode == 0, f"第二次建置非零退出\n{r.stderr[-500:]}"
    left = [p.name for p in strays if p.exists()]
    assert not left, (
        f"⚠⚠ 上一次中斷留下的暫存檔還在：{left}——指定來源的建置會略過孤兒清理、那一場又被沿用，"
        "它們會永遠留著")
    assert keep.exists(), "不是本工具命名的 .tmp 不可以碰（`--out` 可能指到別的東西也在的目錄）"

    # --- ② Ctrl+C 落在寫頁面暫存檔途中 ⇒ 暫存檔要當場收掉 ------------------------------------
    r = build("--force", patch=ki_on(["*.html.tmp"]))
    assert r.returncode != 0 and "KeyboardInterrupt" in r.stderr, f"前提：要真的中斷\n{r.stderr[-300:]}"
    left2 = [p.name for p in (out / "sessions").rglob("*.tmp")]
    assert not left2, f"⚠⚠ Ctrl+C 之後留下了頁面暫存檔：{left2}"
    assert "RESIDUEA" in page.read_text(encoding="utf-8"), "正式頁面要是完整的（舊版）"

    # --- ②-b Ctrl+C 落在**換上**頁面途中（HTML 換上了、換 MD 那一刻）⇒ 暫存檔一樣要當場收掉 -----
    # ② 的中斷落在寫暫存檔途中；換上（`os.replace`）那一段是另一個 `try`，只接 `OSError` 的話
    # MD 的暫存檔留在原地——下一次指定來源的建置又會沿用這一場（② 那一格驗不到這一段）。
    def ki_on_replace(suffix):
        return ("import os as _os\n"
                "_orig_rp = _os.replace\n"
                "def _rp(src, dst, *a, **k):\n"
                f"    if str(src).endswith({suffix!r}):\n"
                "        raise KeyboardInterrupt('injected Ctrl+C at replace')\n"
                "    return _orig_rp(src, dst, *a, **k)\n"
                "_os.replace = _rp\n")

    r = build("--force", patch=ki_on_replace(".md.tmp"))
    assert r.returncode != 0 and "KeyboardInterrupt" in r.stderr, f"前提：要真的在換上 MD 時中斷\n{r.stderr[-300:]}"
    left2b = [p.name for p in (out / "sessions").rglob("*.tmp")]
    assert not left2b, f"⚠⚠ 換上頁面途中 Ctrl+C，留下了暫存檔：{left2b}"
    assert "RESIDUEA" in page.read_text(encoding="utf-8"), "HTML 要是完整的（新換上的或舊的都可以）"

    # --- ③ 索引頁／manifest 寫到一半被中斷 ⇒ 正式檔原封不動（完整的舊版），不留暫存檔 -----------
    for name in ("index.html", ".build-manifest.json", ".cwd-cache.json"):
        before = (out / name).read_bytes()
        r = build("--force", patch=ki_on([name, name + ".tmp"]))
        assert r.returncode != 0 and "KeyboardInterrupt" in r.stderr, f"前提：{name} 要真的在寫的時候中斷"
        assert (out / name).read_bytes() == before, (
            f"⚠⚠ {name} 寫到一半被中斷，正式檔被截斷了——要先寫暫存檔、完整了才換上")
        assert not (out / (name + ".tmp")).exists(), f"{name} 的暫存檔要收掉"
    json.loads((out / ".build-manifest.json").read_text(encoding="utf-8"))   # 還讀得起來

    print("OK: build interrupt residue test passed")


def test_cwd_cache(tmp_path=None):
    """專案名要用的 cwd 跨建置快取（`peek_cwd_cached`／`.cwd-cache.json`）。

    函式層：
    ① 主檔沒變 ⇒ **不開檔**，直接用快取。
    ② 只有 mtime 變 ⇒ 重讀。
    ③ 開頭原本沒有 cwd 的檔長出 cwd 之後要更新（只有 size 變也要；快取裡的空字串不可以被一直沿用）。
    ④ 先 stat 再讀：檔案在兩步之間長大時，記下的是**較小**的 size（下一次才會重讀）。
    ⑤ 版本不符、檔壞掉 ⇒ 整份不用；主檔不見了 ⇒ 不留紀錄；stat 得到卻開不了 ⇒ 不記（⑤b）。
    建置層：
    ⑥ 建置真的走快取（把快取裡的 cwd 改掉，專案名跟著變）；`--force` 不用它；主檔一動就讀回真的 cwd。
    ⑦ 縮範圍建置：範圍外的紀錄檔還在就留、不在就丟。
    ⑧ 硬終止留下的快取暫存檔，下一次建置開頭要清掉。
    ⑨ 快取寫不進去 ⇒ 只警告、建置照常。
    ⑩ cwd 帶著孤立的代理字元 ⇒ 快取照樣寫得進去，建置不中止。
    ⑪ 完整建置（無縮範圍旗標）只留本次掃到的。
    """
    sys.path.insert(0, str(ROOT))
    import ai_session_viewer as asv
    tmp = new_tmp(tmp_path)
    root = tmp / "projects"
    out = tmp / "out"

    def ev(i, cwd=None):
        e = {"type": "user", "uuid": f"u{i}", "timestamp": f"2026-10-01T01:00:{i:02d}.000Z",
             "sessionId": "s", "message": {"role": "user", "content": f"第 {i} 則訊息內容。"}}
        if cwd:
            e["cwd"] = cwd
        return json.dumps(e, ensure_ascii=False) + "\n"

    # ---- 函式層 -------------------------------------------------------------------------
    d = root / "Fn"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "fn.jsonl"
    f.write_text(ev(1, "/x/Fn"), encoding="utf-8")
    real_peek = asv._peek_cwd_raw
    calls = []

    def counting_peek(p, *a, **k):
        calls.append(p)
        return real_peek(p, *a, **k)

    try:
        asv._peek_cwd_raw = counting_peek
        c1 = {}
        assert asv.peek_cwd_cached(f, {}, c1) == "/x/Fn" and len(calls) == 1
        st = f.stat()
        assert c1[str(f)] == [st.st_size, st.st_mtime_ns, "/x/Fn"], f"快取紀錄形狀不對：{c1}"

        # ① 沒變 ⇒ 不開檔
        c2 = {}
        assert asv.peek_cwd_cached(f, c1, c2) == "/x/Fn"
        assert len(calls) == 1, "⚠ 主檔沒變卻又讀了一次——快取沒有作用"
        assert c2 == c1, "命中時要把紀錄原樣帶進下一份快取（不帶的話下一次又全部重讀）"

        # ② 只有 mtime 變（內容換成等長的 cwd，size 不變）⇒ 也要重讀
        f.write_text(ev(1, "/x/Fm"), encoding="utf-8")
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        assert f.stat().st_size == st.st_size, "前提：size 要一樣，這一格才是在守 mtime"
        c3 = {}
        assert asv.peek_cwd_cached(f, c1, c3) == "/x/Fm" and len(calls) == 2, (
            "⚠ mtime 變了卻沿用快取——鍵裡要有 mtime")

        # ③ 開頭沒有 cwd 的檔，長出 cwd 之後要更新——mtime 刻意還原，只剩 size 不同
        #   （mtime 解析度粗的檔案系統上，追加之後 mtime 可能不變）
        g = d / "grow.jsonl"
        g.write_text(ev(1), encoding="utf-8")
        c4 = {}
        assert asv.peek_cwd_cached(g, {}, c4) == "", "前提：這份檔開頭真的讀不到 cwd"
        gst = g.stat()
        with g.open("a", encoding="utf-8") as fh:
            fh.write(ev(2, "/x/Grown"))
        os.utime(g, ns=(gst.st_atime_ns, gst.st_mtime_ns))
        assert g.stat().st_mtime_ns == gst.st_mtime_ns, "前提：mtime 要一樣，這一格才是在守 size"
        c5 = {}
        assert asv.peek_cwd_cached(g, c4, c5) == "/x/Grown", (
            "⚠⚠ 開頭原本沒有 cwd 的檔長出 cwd 之後，還在沿用快取裡的空字串")

        # ④ 先 stat 再讀：讀的當下檔案長大，記下的要是長大**前**的 size
        h = d / "race.jsonl"
        h.write_text(ev(1), encoding="utf-8")
        size_before = h.stat().st_size

        def growing_peek(p, *a, **k):
            with p.open("a", encoding="utf-8") as fh:
                fh.write(ev(2, "/x/Race"))
            return real_peek(p, *a, **k)

        asv._peek_cwd_raw = growing_peek
        c6 = {}
        asv.peek_cwd_cached(h, {}, c6)
        assert c6[str(h)][0] == size_before, (
            f"⚠⚠ 記下的 size 是讀完之後的（{c6[str(h)][0]}，讀之前是 {size_before}）——"
            "要先 stat 再讀，否則讀的當下長出來的內容會被當成「已經看過」")
        asv._peek_cwd_raw = counting_peek

        # ⑤ 主檔不見了 ⇒ 不留紀錄
        gone = d / "gone.jsonl"
        c7 = {}
        assert asv.peek_cwd_cached(gone, {str(gone): [1, 1, "/x/Old"]}, c7) == "" and c7 == {}

        # ⑤b stat 得到、卻開不了（被別的程式鎖住、離線佔位檔）⇒ 這次當成沒有 cwd，但**不記**
        #   用目錄當素材：stat 成功、open 一定丟 OSError，不必模擬鎖檔。
        locked = d / "locked.jsonl"
        locked.mkdir()
        c8 = {}
        assert asv.peek_cwd_cached(locked, {}, c8) == ""
        assert c8 == {}, (
            f"⚠⚠ 讀不到的檔被記成「沒有 cwd」——檔案沒變就一直沿用，暫時的讀取失敗變成永久的錯誤專案名：{c8}")
    finally:
        asv._peek_cwd_raw = real_peek

    # ⑤ 版本不符、壞掉 ⇒ 整份不用
    out.mkdir(parents=True, exist_ok=True)
    cache = out / asv.CWD_CACHE_NAME
    cache.write_text(json.dumps({"v": asv.CWD_CACHE_VERSION + 1, "files": {"a": [1, 1, "/x"]}}),
                     encoding="utf-8")
    assert asv.load_cwd_cache(out) == {}, "版本不符的快取不可以用"
    cache.write_text("{壞掉", encoding="utf-8")
    assert asv.load_cwd_cache(out) == {}
    shutil.rmtree(out)                                 # 建置層從全新的輸出目錄開始（第一次建置也要寫得進去）

    # ---- 建置層 -------------------------------------------------------------------------
    sids = {"Alpha": "00000000-0000-4000-8000-0000000cccc1",
            "Beta": "00000000-0000-4000-8000-0000000cccc2"}

    def mk(proj):
        p = root / proj
        p.mkdir(parents=True, exist_ok=True)
        sid = sids[proj]
        evs = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": "2026-10-01T04:05:06.700Z", "cwd": f"/x/{proj}",
             "gitBranch": "main", "version": "2.1.150", "sessionId": sid,
             "message": {"role": "user", "content": f"{proj} 的訊息內容夠長。" * 3}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": "2026-10-01T04:05:10.000Z", "sessionId": sid,
             "message": {"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                         "usage": {"input_tokens": 80, "output_tokens": 12},
                         "content": [{"type": "text", "text": "回覆。"}]}}]
        jf = p / f"{sid}.jsonl"
        jf.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
        return jf

    def build(*extra):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--claude-source", f"demo={root}",
             "--no-codex", "--out", str(out)] + list(extra),
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"非零退出\n{r.stdout}\n{r.stderr}"
        assert "cwd 快取寫不進去" not in r.stderr, r.stderr

    def projs():
        m = json.loads((out / asv.MANIFEST_NAME).read_text(encoding="utf-8"))
        return sorted(e["row"]["proj"] for e in m["entries"].values())

    def cached():
        return json.loads(cache.read_text(encoding="utf-8"))["files"]

    shutil.rmtree(d)                                   # 函式層的素材不進建置
    ja, jb = mk("Alpha"), mk("Beta")
    build()
    c = cached()
    assert set(c) == {str(ja), str(jb)}, f"兩個主檔都要進快取，實得 {list(c)}"
    assert projs() == ["Alpha", "Beta"]

    # ⑥ 建置真的走快取：把 Alpha 的 cwd 在快取裡改掉，專案名要跟著變
    def tamper():
        data = json.loads(cache.read_text(encoding="utf-8"))
        data["files"][str(ja)][2] = "/y/Tampered"
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    tamper()
    build()
    assert "Tampered" in projs(), (
        f"⚠ 改了快取裡的 cwd，專案名卻沒變——建置沒有走快取（實得 {projs()}）")
    # `--force`（忽略快取）不用它
    build("--force")
    assert projs() == ["Alpha", "Beta"], f"⚠ --force 還在用 cwd 快取：{projs()}"
    # 主檔一動（size 變了）就讀回真的 cwd
    tamper()
    build()
    assert "Tampered" in projs(), f"前提：快取又被改掉了（實得 {projs()}）"
    with ja.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    build()
    assert projs() == ["Alpha", "Beta"], f"主檔變了卻沒有重讀：{projs()}"

    # ⑦ 縮範圍建置：範圍外的紀錄檔還在就留、不在就丟
    #   （這裡每次都帶 `--claude-source`，所以每次都是縮範圍建置——那正是會累積的那條路）
    build("--project", "Beta")
    assert set(cached()) == {str(ja), str(jb)}, (
        f"⚠ 縮範圍建置把範圍外的紀錄丟了（下一次完整建置會全部重讀），實得 {list(cached())}")
    shutil.rmtree(root / "Alpha")
    build("--project", "Beta")
    assert set(cached()) == {str(jb)}, (
        f"⚠ 來源刪掉之後還留著它的紀錄——帶 --claude-source 的使用者，快取會一直長：{list(cached())}")

    # ⑧ 硬終止留下的快取暫存檔，下一次建置開頭要清掉。
    #   ⚠ 要用**零場**建置驗：有掃到 session 的建置本來就會重寫快取，殘留的 `.tmp` 會被正常的寫入
    #     覆蓋後換上，清理那一格有沒有在做都看不出來。零場那條路不寫快取，只有清理會碰到它。
    stray = out / (asv.CWD_CACHE_NAME + ".tmp")
    stray.write_text("<half-written>", encoding="utf-8")
    empty = tmp / "empty-projects"
    empty.mkdir()
    r = subprocess.run([sys.executable, str(SCRIPT), "--claude-source", f"demo={empty}",
                        "--no-codex", "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "沒有找到任何 session 檔" in r.stdout, f"前提：要走零場那條路\n{r.stdout}\n{r.stderr}"
    assert not stray.exists(), "⚠ 上一次中斷留下的 cwd 快取暫存檔還在"

    def run(*args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              encoding="utf-8", env=env)

    # ⑨ 快取寫不進去（那個位置是目錄）⇒ 只印一行警告，建置照常
    out9 = tmp / "out9"
    (out9 / asv.CWD_CACHE_NAME).mkdir(parents=True)
    r = run("--claude-source", f"demo={root}", "--no-codex", "--out", str(out9))
    assert r.returncode == 0, f"⚠ 快取寫不進去，整次建置就失敗了\n{r.stdout}\n{r.stderr}"
    assert "cwd 快取寫不進去" in r.stderr, f"前提：寫入真的失敗了\n{r.stderr}"
    assert list((out9 / "sessions").rglob("*.html")), "頁面照樣要產出來"

    # ⑩ cwd 帶著孤立的代理字元（JSONL 裡合法的跳脫）⇒ 快取照樣寫得進去，建置不中止
    root10 = tmp / "projects10"
    (root10 / "Odd").mkdir(parents=True)
    odd_sid = "00000000-0000-4000-8000-0000000cccc3"
    odd_cwd = "/x/Odd" + chr(0xDC80)
    (root10 / "Odd" / f"{odd_sid}.jsonl").write_text(json.dumps(
        {"type": "user", "uuid": "u1", "timestamp": "2026-10-01T04:05:06.700Z", "cwd": odd_cwd,
         "sessionId": odd_sid, "message": {"role": "user", "content": "內容夠長的一則訊息。" * 3}},
        ensure_ascii=True) + "\n", encoding="utf-8")
    out10 = tmp / "out10"
    r = run("--claude-source", f"demo={root10}", "--no-codex", "--out", str(out10))
    assert r.returncode == 0, f"⚠⚠ cwd 有孤立代理字元，整次建置中止了\n{r.stdout}\n{r.stderr}"
    assert "cwd 快取寫不進去" not in r.stderr, r.stderr
    assert [v[2] for v in asv.load_cwd_cache(out10).values()] == [odd_cwd], "快取要原樣存回那個 cwd"

    # ⑪ 完整建置（不帶任何縮範圍旗標）只留本次掃到的：範圍外、檔案還在的舊紀錄也要丟
    #   ⚠ 上面每一次建置都帶 `--claude-source`＝縮範圍；這一格是唯一走完整建置的。
    #   來源走自動偵測 ⇒ 家目錄要指到假的，而且裡面只有這一場。
    home = tmp / "home11"
    (home / ".claude" / "projects" / "Gamma").mkdir(parents=True)
    gamma = home / ".claude" / "projects" / "Gamma" / f"{sids['Beta']}.jsonl"
    gamma.write_text(jb.read_text(encoding="utf-8").replace('"/x/Beta"', '"/x/Gamma"'), encoding="utf-8")
    out11 = tmp / "out11"
    out11.mkdir()
    (out11 / asv.CWD_CACHE_NAME).write_text(json.dumps(
        {"v": asv.CWD_CACHE_VERSION, "files": {str(jb): [1, 1, "/x/Beta"]}}), encoding="utf-8")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    r = run("--out", str(out11), env=env)
    assert r.returncode == 0, f"非零退出\n{r.stdout}\n{r.stderr}"
    got = asv.load_cwd_cache(out11)
    assert any(v[2] == "/x/Gamma" for v in got.values()), f"前提：本次掃到的那一場要進快取：{got}"
    assert str(jb) not in got, (
        f"⚠ 完整建置留著本次沒掃到的舊紀錄（檔還在也不該留）——刪掉的 session 會一直累積：{list(got)}")

    print("OK: cwd cache test passed")


def test_codex_tool_output_items(tmp_path=None):
    """Codex 工具結果是 content item 陣列時（`input_text`／`input_image`），照文字與圖片畫（RENDERER_VERSION 66 起；只認已知欄位是 67 起）。

    舊行為：整格 `json.dumps` ⇒ html 與 md 都是一整行跳脫過的 JSON（換行全變成 `\\n`）、圖片是一長串 base64。
    反向兩格：認不得的型別、`text` 不是字串，都要照原樣印出來，不可以藏掉。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    tmp = new_tmp(tmp_path)
    tmp.mkdir(parents=True, exist_ok=True)
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

    def line(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-10-10T03:00:{sec:02d}.000Z", "type": typ, "payload": payload},
                          ensure_ascii=False)

    def call(sec, cid, output):
        return [line(sec, "response_item", {"type": "custom_tool_call", "call_id": cid, "name": "exec",
                                            "input": "print('x')"}),
                line(sec + 1, "response_item", {"type": "custom_tool_call_output", "call_id": cid,
                                                "output": output})]

    rows = [line(0, "session_meta", {"id": "019f00a0-0000-7000-8000-0000000000c1",
                                     "cwd": "/x/ExecOut", "cli_version": "0.157.1"}),
            line(1, "turn_context", {"cwd": "/x/ExecOut", "model": "gpt-5.6"}),
            line(2, "event_msg", {"type": "task_started", "turn_id": "t1"}),
            line(3, "event_msg", {"type": "user_message", "message": "跑一下EXECQ。"})]
    rows += call(4, "c-text", [{"type": "input_text", "text": "Script completed\nWall time: 0.1s\n"},
                               {"type": "input_text", "text": "第一行OUTA <b>\n第二行OUTB"}])
    rows += call(6, "c-img", [{"type": "input_text", "text": "Script completed\n"},
                              {"type": "input_image", "image_url": f"data:image/png;base64,{png}",
                               "detail": "high"}])
    rows += call(8, "c-remote", [{"type": "input_image", "image_url": "https://img.example/REMOTEIMG.png"}])
    rows += call(10, "c-svg", [{"type": "input_image", "image_url": "data:image/svg+xml;base64,PHN2Zz4="}])
    rows += call(12, "c-odd", [{"type": "output_audio", "data": "ODDTYPE"},
                               {"type": "input_text", "text": {"value": "NONSTR"}}])
    rows += call(14, "c-out", [{"type": "output_text", "text": "OUTTEXT1\nOUTTEXT2"}])
    png2 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    rows += call(16, "c-extra", [{"type": "input_text", "text": "EXTRATEXT", "annotations": []},
                                 {"type": "input_image", "image_url": f"data:image/png;base64,{png2}",
                                  "detail": "high", "file_id": "EXTRAIMG"}])
    rows += call(18, "c-empty", [{"type": "input_text", "text": ""}])
    # 指令輸出裡長得跟回合標頭一模一樣的行（例：`cat` 另一份本工具產的 .md）：
    # 一行指到不存在的回合；一行帶著這一場也有的 `{#t3}`、又比真的那一行先出現，但耐久錨點對不上。
    # 兩行前面都照抄了對應的 html 標籤：頁面上它們是跳脫過的內文，不可以被當成真的錨點
    rows += call(20, "c-forge", [{"type": "input_text", "text":
                                  "前FORGEDMARK\n" '<span class="tanchor" id="t999"></span>\n'
                                  "### 🤖 Codex · 03:00:30 {#t999}\n後FORGEDTAIL\n"
                                  '<div class="turn user" id="k2222222222222">\n'
                                  "### 👤 You · 03:00:40 {#t3} {#k2222222222222}\nSTEALMARK\n"
                                  "### 👤 You · 03:00:41 {#t3}\nNOKMARK"}])
    rows += [line(22, "response_item", {"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text", "text": "好了EXECA。"}]}),
             line(23, "event_msg", {"type": "task_complete"}),
             line(24, "event_msg", {"type": "task_started", "turn_id": "t2"}),
             line(25, "event_msg", {"type": "user_message", "message": "第二題SECONDQ。"}),
             line(26, "response_item", {"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text", "text": "答SECONDA。"}]}),
             line(27, "event_msg", {"type": "task_complete"})]
    f = tmp / "rollout-2026-10-10T03-00-00-019f00a0-0000-7000-8000-0000000000c1.jsonl"
    f.write_text("\n".join(rows), encoding="utf-8")
    s = v.load_codex_session(f)
    v.analyze(s)
    s.out_html, s.out_md, s.mem_href = "codex/demo/x.html", "codex/demo/x.md", ""
    html = v.render_session_html(s, "../../../index.html", "")
    md = v.render_session_md(s)
    assert "好了EXECA" in html and "跑一下EXECQ" in md, "前提：這一場要畫得出來"

    # --- ① 文字：換行留著、不是 JSON ------------------------------------------
    assert "第一行OUTA <b>\n第二行OUTB" in md, (
        "md 裡的指令輸出要保留換行（舊行為是一整行 JSON，`\\n` 被跳脫）")
    assert "<pre>第一行OUTA &lt;b&gt;\n第二行OUTB</pre>" in html, (
        "html 裡的指令輸出要是一格保留換行、跳脫過的 <pre>")
    assert "Wall time: 0.1s" in md and "<pre>Script completed\nWall time: 0.1s\n</pre>" in html, (
        "標頭那一格也要照文字畫")
    for page, name in ((md, "md"), (html, "html")):
        # 能出現的只有刻意照原樣印的兩格：⑥（text 不是字串）與 ⑧（多了欄位），各由那兩段守
        assert page.count("input_text") == 2, (
            f"{name} 看得到 {page.count('input_text')} 次 `input_text`——有文字格被當成 JSON 印了，或該印的沒印")
        assert "第一行OUTA <b>\\n" not in page, f"{name} 還是跳脫過的一整行"
    assert "OUTTEXT1\nOUTTEXT2" in md and "<pre>OUTTEXT1\nOUTTEXT2</pre>" in html, (
        "`output_text` 格也要照文字畫")

    # --- ② data: 網址的圖片：html 內嵌、md 佔位 -------------------------------
    assert f'src="data:image/png;base64,{png}"' in html, "data: 網址的圖片要內嵌成 <img>"
    assert png not in md and "[圖片]" in md, "md 不放 base64，只放 [圖片]"

    # --- ③ 外連網址不抓、④ 格式白名單沿用 ------------------------------------
    assert "REMOTEIMG" not in html, "http 網址的圖片不可以變成 <img>（離線頁面不替對話外連）"
    assert "image/svg+xml，格式不支援內嵌" in html and "PHN2Zz4=" not in html, "svg 不在白名單，只放佔位"

    # --- ⑤（反向）認不得的型別、⑥ text 不是字串：照原樣印 JSON ---------------
    assert '"type": "output_audio"' in md and "ODDTYPE" in html, (
        "認不得的型別要照原樣印出來——藏掉的話上游再改形狀時沒有人看得到")
    assert '{"value": "NONSTR"}' in md and "{'value'" not in md + html, (
        "`text` 不是字串時要印 JSON，不可以 str() 成 Python 的 repr")

    # --- ⑦ Claude 的兩個分支照舊 ----------------------------------------------
    assert v.normalize_result([{"type": "text", "text": None}, {"type": "image"}]) == "\n[圖片]"

    # --- ⑧（反向）多了欄位的格不認：照原樣印 JSON，多出來的欄位看得到 -----------
    assert '"annotations": []' in md and '"annotations": []' in html, (
        "文字格多了欄位要照原樣印——認了的話多出來的欄位在頁面上不會以任何形式出現")
    assert '"file_id": "EXTRAIMG"' in md and '"file_id": "EXTRAIMG"' in html, "圖片格多了欄位也一樣"
    assert png2 in md and 'src="data:image/png;base64,' + png2 not in html, (
        "多了欄位的圖片格不可以內嵌，要照原樣印 JSON（md 裡看得到它的資料）")

    # --- ⑨ 空字串的文字格：畫成空框，不是 JSON --------------------------------
    assert '"text": ""' not in md + html and "<pre></pre>" in html, (
        "空字串照認（輸出本來就是空的）：html 是一格空的 <pre>，不印 JSON")

    # --- ⑩ 搜尋：指令輸出裡長得像回合標頭的行不切回合 -------------------------
    sdir = tmp / "cli" / "sessions" / "2026" / "10" / "10"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f.name).write_text("\n".join(rows), encoding="utf-8")
    out = tmp / "cli" / "out"
    r = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'cli' / 'sessions'}",
                        "--no-claude", "--out", str(out)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"轉換失敗\n{r.stdout}\n{r.stderr}"
    page_md = next(out.glob("sessions/codex/**/*.md")).read_text(encoding="utf-8")
    assert "\n### 🤖 Codex · 03:00:30 {#t999}\n" in page_md, "前提：照抄的標頭行原樣進了 .md"
    assert re.search(r"^### 👤 You · .*\{#t3\}", page_md.split("STEALMARK")[1], re.M), (
        "前提：真的 t3 標頭在照抄那一行之後")

    r = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'cli' / 'sessions'}",
                        "--no-claude", "--out", str(out)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "沿用 1" in r.stdout, f"前提：第二次建置沿用上一次的輸出\n{r.stdout}\n{r.stderr}"

    def links(q):
        r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out), "--search", q],
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0 and "命中 0 則" not in r.stdout, f"搜尋「{q}」應命中\n{r.stdout}{r.stderr}"
        res = max((out / "search").glob("*.html"), key=lambda p: p.stat().st_mtime_ns)
        return set(re.findall(r'href="[^"]*\.html#([ts]\d+)"', res.read_text(encoding="utf-8")))

    assert links("FORGEDTAIL") == {"t2"}, "指到不存在回合的照抄標頭不可以切回合（命中要連回真的那一則）"
    assert links("STEALMARK") == {"t2"}, (
        "耐久錨點對不上的照抄標頭不可以搶走真的 t3（內文裡照抄的 id=\"k…\" 也不算）")
    assert links("NOKMARK") == {"t2"}, "真的 t3 帶著耐久錨點：照抄來、沒帶的那一行不算（要成對比）"
    assert links("SECONDQ") == {"t3"}, "真的 t3 標頭照常切（沿用上一次輸出的建置也一樣）"
    # md-only 建置：沒有 html 也要擋（同一則裡照抄標頭前後的兩個詞，AND 要命中）
    out_md = tmp / "cli" / "out-md"
    r = subprocess.run([sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'cli' / 'sessions'}",
                        "--no-claude", "--out", str(out_md), "--format", "md"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"md-only 轉換失敗\n{r.stderr}"
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out_md), "--search", "FORGEDMARK FORGEDTAIL"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "命中 1 則" in r.stdout, (
        f"md-only 建置也不可以被照抄的標頭切開（同一則的兩個詞要 AND 命中）\n{r.stdout}{r.stderr}")
    # 比位置（序號）：同一場照抄自己前面的標頭、錨點配別則的耐久錨點、跟後面某一則**完全相同**的照抄，都當內文
    syn = ("### 👤 You · 00:00:01 {#t1} {#k1}\nA\n### 👤 You · 00:00:01 {#t1} {#k1}\nDUPMARK\n"
           "### 🤖 Codex · 00:00:02 {#t3} {#k2}\nCROSSMARK\n### 🤖 Codex · 00:00:02 {#t2} {#k2}\nEXACTMARK\n"
           "### 🤖 Codex · 00:00:02 {#t2} {#k2}\nB\n### 👤 You · 00:00:03 {#t3} {#k3}\nC\n"
           "### 👤 You · 00:00:04 {#t4}\nD")
    syn_turns = [["t1", "k1", 0], ["t2", "k2", 4], ["t3", "k3", 5], ["t4", None, 6]]
    chunks = list(v.iter_turn_chunks(syn, syn_turns))
    assert [c[0] for c in chunks] == ["t1", "t2", "t3", "t4"], f"真的四則都要切出來：{[c[0] for c in chunks]}"
    assert "DUPMARK" in chunks[0][-1], "同一個錨點第二次出現要當內文"
    assert "CROSSMARK" in chunks[0][-1], "錨點配別則的耐久錨點要當內文"
    assert "EXACTMARK" in chunks[0][-1] and "EXACTMARK" not in chunks[1][-1], (
        "跟後面某一則完全相同的照抄、比真的那一行先出現：位置不對就當內文")
    assert list(v.iter_turn_chunks("### 👤 You · 00:00:04 {#t4} {#k9}\nE", [["t4", None, 0]])) == [], (
        "表上沒有耐久錨點的那一則，帶著耐久錨點的那一行不算")
    # manifest 的表跟磁碟上的 .md 對不上：兩筆來源寫到同一個輸出路徑時要挑對得上的那一張；都對不上才退回只看正則
    mpath = out / ".build-manifest.json"
    man = json.loads(mpath.read_text(encoding="utf-8"))
    key, ent = next((k, e) for k, e in man["entries"].items() if e.get("turns"))
    good = ent["turns"]
    man["entries"][key + "#other-source"] = dict(ent, turns=[[a, "k1", n] for a, _, n in good])  # 排在後面、對不上
    mpath.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    assert links("FORGEDTAIL") == {"t2"}, "同一個路徑有好幾張表時，要挑跟這份 .md 對得上的那一張"
    for e in man["entries"].values():
        if e.get("turns"):
            e["turns"] = [[a, "k1", n] for a, _, n in good]
    mpath.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    assert links("SECONDQ") == {"t3"}, "表都對不上這份 .md 時要退回只看正則（真的 t3 照常切）"

    # --- ⑪ 子代理回合也要記進表；沒有時間戳的子代理（沒有耐久錨點）照抄不走 -------
    sub_proj = tmp / "side" / "projects" / "side-proj"
    sub_proj.mkdir(parents=True, exist_ok=True)
    side_ev = [dict(e) for e in MAIN_EVENTS]
    side_ev[2] = dict(side_ev[2], message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": TOOLU,
         "content": "前SIDEFORGE\n### ↳ 🤖 Claude · 00:00:00 {#s1}\n後SIDETAIL"}]})
    side_ev += [{k: v_ for k, v_ in e.items() if k != "timestamp"} for e in SUB_EVENTS]
    sfile = sub_proj / f"{SID2}.jsonl"
    sfile.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in side_ev), encoding="utf-8")
    ss = v.load_session(sfile, "side-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(ss)
    ss.out_html, ss.out_md, ss.mem_href = "claude-code/demo/side.html", "claude-code/demo/side.md", ""
    smd = v.render_session_md(ss)
    side_turns = [t for t in ss.md_turns if t[0].startswith("s")]
    assert side_turns and side_turns[0][1] is None, f"前提：子代理回合記進表、而且沒有耐久錨點：{ss.md_turns}"
    assert "\n### ↳ 🤖 Claude · 00:00:00 {#s1}\n" in smd, "前提：照抄的子代理標頭原樣進了 .md"
    schunks = list(v.iter_turn_chunks(smd, ss.md_turns))
    assert [c[0] for c in schunks] == [t[0] for t in ss.md_turns], (
        f"切出來的要剛好是表上那幾則：{[c[0] for c in schunks]} vs {ss.md_turns}")
    owner = [c[0] for c in schunks if "後SIDETAIL" in c[-1]]
    assert owner and not owner[0].startswith("s"), f"主對話工具輸出裡照抄的子代理標頭不可以搶走真的子代理回合：{owner}"

    print("OK: codex tool output items test passed")


def test_sync_conflict_copies(tmp_path=None):
    """跨機同步工具另存的副本（`<sid>.synced-<主機>-<UTC 時間>.jsonl`）自己一列、自己一頁，標「同步副本」。

    副本與原檔的開始時間、專案、前 8 碼都相同 ⇒ 輸出檔名的 id 段不另帶一段的話，兩份寫到同一個輸出檔、後寫的蓋掉
    先寫的。逐段走：①內容一樣的兩份 ②分岔的兩份 ③`--ai-md` 選場 ④檔名的形狀 ⑤旁邊沒有原檔的副本
    ⑥同一場的幾份副本各自一頁 ⑦上一次建置兩份寫在同一個檔、升版後只建一部分時的書籤反查表。
    """
    import importlib
    from datetime import datetime, timezone
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    tmp = new_tmp(tmp_path)
    sid = "5c5c0000-0000-4000-8000-00000000c0de"
    cname = f"{sid}.synced-HOSTX-20260801T010203Z"
    # 副本的說明：主機與存的時間（UTC 換成本地時間，同索引的日期欄）
    label = "同步副本 HOSTX " + datetime(2026, 8, 1, 1, 2, 3, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    def ev(i, role, content, sec):
        e = {"type": role, "uuid": f"{role[0]}{i}", "timestamp": f"2026-07-30T02:00:{sec:02d}.000Z",
             "cwd": "/x/Sync", "sessionId": sid, "isSidechain": False}
        if role == "user":
            e["message"] = {"role": "user", "content": content}
        else:
            e["message"] = {"role": "assistant", "model": "claude-opus-4-8", "id": f"msg_s{i}",
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                            "content": [{"type": "text", "text": content}]}
        return e

    title = {"type": "ai-title", "aiTitle": "原檔的AI標題SYNCTITLE", "sessionId": sid}
    head = [ev(1, "user", "第一個問題SYNCFIRST。", 0), ev(1, "assistant", "第一個回答。", 5)]

    def write(path, evs, sort=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False, sort_keys=sort) for e in evs) + "\n",
                        encoding="utf-8")

    def build(out, src, *extra):
        r = subprocess.run([sys.executable, str(SCRIPT), "--claude-source", f"demo={src}", "--no-codex",
                            "--out", str(out), *extra], capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
        assert "輸出檔名相同" not in r.stdout + r.stderr, f"不應撞名\n{r.stdout[-400:]}\n{r.stderr[-400:]}"
        return r

    def index_rows(out):
        idx = (out / "index.html").read_text(encoding="utf-8")
        rows = re.findall(r'<tr data-sid="([^"]*)"(.*?)</tr>', idx, re.S)
        return [s for s, _ in rows], dict(rows)

    def bx(out):
        mgr = (out / "sessions" / "bookmarks.html").read_text(encoding="utf-8")
        return json.loads(re.search(r"var BX_SESS=(\{.*?\});\s*$", mgr, re.M).group(1)
                          .replace("\\u003c", "<").replace("\\u003e", ">"))

    def own_pages(out, tags):
        pages = [p.read_text(encoding="utf-8") for p in _session_pages(out)]
        assert len(pages) == len(tags), f"每份各一頁，實得 {[p.name for p in _session_pages(out)]}"
        for tag in tags:
            assert sum(tag in h for h in pages) == 1, f"{tag} 要剛好在一頁裡（沒被別份蓋掉）"

    # --- 1. 內容一樣的兩份（副本重新序列化、少了標題列）：兩列、各自一頁，副本那一列與那一頁標「同步副本」---
    p1 = tmp / "s1" / "sync-proj"
    write(p1 / f"{sid}.jsonl", head + [title])
    write(p1 / f"{cname}.jsonl", head, sort=True)
    out1 = tmp / "out1"
    build(out1, p1.parent)
    cid = v.claude_out_id(cname)
    assert cid != sid[:8] and cid.startswith(sid[:8]), cid
    pages = _session_pages(out1)
    po = [p for p in pages if p.name.endswith(f"__{sid[:8]}.html")]
    pc = [p for p in pages if p.name.endswith(f"__{cid}.html")]
    assert len(pages) == 2 and len(po) == 1 and len(pc) == 1, f"兩份各有一頁、檔名不同，實得 {[p.name for p in pages]}"
    sids, rows = index_rows(out1)
    assert sorted(sids) == sorted([sid, cname]), f"兩份各一列、各用自己的檔名當 id，實得 {sids}"
    assert label in rows[cname] and "同步副本" not in rows[sid], "索引上只有副本那一列標同步副本"
    assert "SYNCTITLE" in rows[sid], "原檔那一列是原檔的標題"
    ho, hc = po[0].read_text(encoding="utf-8"), pc[0].read_text(encoding="utf-8")
    assert label in _page_body(hc) and "同步副本" not in _page_body(ho), "副本那一頁的頁首要標同步副本"
    md_idx = (out1 / "index.md").read_text(encoding="utf-8")
    assert md_idx.count(f"〔{label}〕") == 1, f"MD 索引的副本那一列要標同步副本\n{md_idx}"
    mdo, mdc = po[0].with_suffix(".md").read_text(encoding="utf-8"), pc[0].with_suffix(".md").read_text(encoding="utf-8")
    assert label in mdc and "同步副本" not in mdo, "MD 頁首也要標"
    m1 = bx(out1)
    assert m1[sid]["u"].endswith(po[0].name) and m1[cname]["u"].endswith(pc[0].name), f"書籤反查表各指各的頁，實得 {m1}"
    r1 = build(out1, p1.parent)
    assert "沿用 2" in r1.stdout, f"沒有變的兩份都沿用（不互相蓋掉、不重建）\n{r1.stdout[-300:]}"

    # --- 2. 分岔（兩台各自接著聊）：一樣兩列，各畫各的內容 ---
    p2 = tmp / "s2" / "sync-proj"
    write(p2 / f"{sid}.jsonl", head + [title, ev(5, "user", "原檔這邊OMARK。", 40)])
    write(p2 / f"{cname}.jsonl", head + [ev(6, "user", "副本那邊CMARK。", 40)])
    out2 = tmp / "out2"
    build(out2, p2.parent)
    cid = v.claude_out_id(cname)
    pages = _session_pages(out2)
    ho = [p for p in pages if p.name.endswith(f"__{sid[:8]}.html")][0].read_text(encoding="utf-8")
    hc = [p for p in pages if p.name.endswith(f"__{cid}.html")][0].read_text(encoding="utf-8")
    assert "OMARK" in ho and "CMARK" not in ho and "CMARK" in hc and "OMARK" not in hc, "各畫各的內容"

    # --- 3. `--ai-md` 選場：前 8 碼兩份都對得到；完整 id 剛好是另一份的開頭時以完全相同的為準；認副本的 id 與輸出檔名那一段 ---
    def aimd(key):
        return subprocess.run([sys.executable, str(SCRIPT), "--claude-source", f"demo={p2.parent}", "--no-codex",
                               "--ai-md", key, "--out", str(tmp / "ai")], capture_output=True, text=True,
                              encoding="utf-8")
    ra = aimd(sid[:8])
    assert ra.returncode != 0 and "對到 2 場" in ra.stderr, f"前 8 碼兩份都對得到，要請人給長一點的 id\n{ra.stderr[-300:]}"
    rb = aimd(sid)
    assert rb.returncode == 0 and "OMARK" in Path(rb.stdout.strip()).read_text(encoding="utf-8"), (
        f"原檔的完整 id 要指得到原檔（不可以被副本的 id 開頭擋住）\n{rb.stderr[-300:]}")
    for key in (cname, cid):
        rc = aimd(key)
        assert rc.returncode == 0, f"要認得副本的 id 與輸出檔名裡那一段（{key}）\n{rc.stderr[-300:]}"
        got = Path(rc.stdout.strip())
        assert "CMARK" in got.read_text(encoding="utf-8") and got.name.endswith(f"__{cid}.ai.md"), (key, got.name)

    # --- 4. 檔名的形狀：只認同步工具產生的那一種 ---
    for stem, base in [(cname, sid), (cname + ".1", sid), (cname + ".synced-OTHER-20260802T000000Z", sid),
                       (cname + ".2.synced-B-C-20260802T000000Z.1", sid),
                       (f"{sid}.synced-{'H' * 25}-20260801T010203Z", None),     # 主機名超過 24 字
                       (f"{sid}.synced-HOSTX", None), (f"{sid}.synced-HOSTX-20260801T0102Z", None),
                       (f"{sid}.synced-HOSTX-20260801T010203Z.x", None),
                       (f"{sid} (1)", None), (".synced-HOSTX-20260801T010203Z", None)]:
        assert v.sync_copy_base(stem) == (stem if base is None else base), (
            f"檔名的形狀只認同步工具產生的那一種：{stem} → {v.sync_copy_base(stem)}")
        assert bool(v.sync_copy_label(stem)) == (base is not None), f"說明與判讀要一致：{stem}"
        assert (v.claude_out_id(stem) == stem[:8]) == (base is None), f"只有副本的輸出檔名另帶一段：{stem}"
    assert v.sync_copy_label(cname) == label, f"副本的說明是主機與本地時間，實得 {v.sync_copy_label(cname)!r}"

    # --- 5. 旁邊沒有原檔的副本：照樣自己一列、標同步副本 ---
    p5 = tmp / "s5" / "sync-proj"
    write(p5 / f"{cname}.jsonl", head)
    out5 = tmp / "out5"
    build(out5, p5.parent)
    sids, rows = index_rows(out5)
    assert sids == [cname] and label in rows[cname], f"只有副本也照樣標，實得 {sids}"

    # --- 6. 同一場的兩份副本只差在 `.<n>`：雜湊取得太短就湊得出撞成同一段的一對 ⇒ 各自一頁、不撞名 ---
    pair = [f"{cname}.4243", f"{cname}.4502"]
    for i, stem in enumerate(pair):
        write(tmp / "s6" / "pair" / f"{stem}.jsonl", head + [ev(7 + i, "user", f"第{i}份PAIR{i}MARK。", 40)])
    build(tmp / "out6", tmp / "s6")
    own_pages(tmp / "out6", ("PAIR0MARK", "PAIR1MARK"))
    assert v.claude_out_id(pair[0]) != v.claude_out_id(pair[1]), f"輸出檔名的 id 段要分得開：{pair}"

    # --- 6b. 主機名只差大小寫的兩份副本：不分大小寫的檔案系統（Windows、macOS 預設）上，直接接副本檔名的後段
    #         會是同一個檔 ⇒ id 段不分大小寫也要分得開 ---
    case = [f"{sid}.synced-HOSTX-20260801T010203Z", f"{sid}.synced-hostx-20260801T010203Z"]
    for i, stem in enumerate(case):          # 只差大小寫的兩個檔名在 Windows 放不進同一個資料夾 ⇒ 各放一個（同一個 cwd）
        write(tmp / "s6b" / f"case{i}" / f"{stem}.jsonl", head + [ev(9 + i, "user", f"第{i}份CASE{i}MARK。", 40)])
    build(tmp / "out6b", tmp / "s6b")
    own_pages(tmp / "out6b", ("CASE0MARK", "CASE1MARK"))
    assert v.claude_out_id(case[0]).casefold() != v.claude_out_id(case[1]).casefold(), f"輸出檔名的 id 段要分得開：{case}"

    # --- 7. 上一次建置時原檔和副本寫在同一個檔（輸出檔名的規則跟著版本換過），升版後只建一部分：書籤管理頁
    #        不可以把那個共用的檔給錯的那一場。那個檔這一次有別場重建過 ⇒ 是那一場的；沒有 ⇒ 裡面是上一次後寫的那一場、
    #        分不出是誰。兩種都照實講對不到（不在反查表裡）---
    other = "7e7e0000-0000-4000-8000-0000000000aa"
    s7 = tmp / "s7"
    write(s7 / "A-orig" / f"{sid}.jsonl", head + [title, ev(5, "user", "原檔這邊OMARK。", 40)])
    write(s7 / "B-copy" / f"{cname}.jsonl", head + [ev(6, "user", "副本那邊CMARK。", 40)])
    write(s7 / "C-other" / f"{other}.jsonl", [dict(e, sessionId=other, cwd="/x/Other") for e in head])
    full = tmp / "out7"
    build(full, s7)
    m7 = bx(full)
    assert {sid, cname, other} <= set(m7), f"完整建置三場都在反查表裡，實得 {list(m7)}"
    copy_page = (full / m7[cname]["u"]).read_text(encoding="utf-8")

    def old_build(name, copy_wrote_last=False, orig_row=True, copy_case=False):
        """複製一份完整建置，改成「上一次建置」的樣子：manifest 版本不符；副本那一列指著原檔那一頁的路徑
        （`copy_case`：只差大小寫的寫法），副本自己那一頁不在；`copy_wrote_last` 時共用的那個檔裝的是副本的頁；
        `orig_row=False` 時上一次的 manifest 裡沒有原檔那一列。"""
        dst = tmp / name
        shutil.copytree(full, dst)
        mf = dst / v.MANIFEST_NAME
        d = json.loads(mf.read_text(encoding="utf-8"))
        d["renderer_version"] = -1
        ents = d["entries"]
        okey = next(k for k, e in ents.items() if e["row"]["session_id"] == sid)
        crow = next(e["row"] for e in ents.values() if e["row"]["session_id"] == cname)
        shared, shared_md = ents[okey]["row"]["out_html"], ents[okey]["row"]["out_md"]
        (dst / "sessions" / crow["out_html"]).unlink()
        (dst / "sessions" / crow["out_md"]).unlink()
        crow["out_html"], crow["out_md"] = (shared.upper(), shared_md.upper()) if copy_case else (shared, shared_md)
        if not orig_row:
            del ents[okey]
        mf.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        if copy_wrote_last:
            (dst / "sessions" / shared).write_text(copy_page, encoding="utf-8")
        return dst, "sessions/" + shared

    def page_of(out, m, key):
        return (out / m[key]["u"]).read_text(encoding="utf-8")

    # --- 7a. 只建原檔那個夾：原檔在共用的那個檔重建，副本不可以指過去 ---
    o7, x = old_build("out7a")
    build(o7, s7, "--project", "A-orig")
    m = bx(o7)
    assert m[sid]["u"] == x and "OMARK" in page_of(o7, m, sid), f"原檔指著它重建的那一頁，實得 {m.get(sid)}"
    assert cname not in m, f"那個檔現在是原檔的，副本不可以指過去，實得 {m.get(cname)}"
    # --- 7b. 只建副本那個夾（共用的那個檔裝的是副本、副本那一列寫成只差大小寫）：副本指它自己的新頁，原檔不可以指過去 ---
    o7, x = old_build("out7b", copy_wrote_last=True, copy_case=True)
    build(o7, s7, "--project", "B-copy")
    m = bx(o7)
    assert m[cname]["u"] != x and "CMARK" in page_of(o7, m, cname), f"副本指著它自己的新頁，實得 {m.get(cname)}"
    assert sid not in m, f"共用的那個檔裝的是副本，原檔不可以指過去，實得 {m.get(sid)}"
    # --- 7c. 兩邊都不建：兩場都不指過去，別場照舊 ---
    o7, x = old_build("out7c", copy_wrote_last=True)
    build(o7, s7, "--project", "C-other")
    m = bx(o7)
    assert sid not in m and cname not in m and other in m, f"分不出是誰的那個檔兩場都不給，實得 {list(m)}"
    # --- 7d. 上一次的 manifest 裡只有副本那一列：原檔這一次在那裡重建，副本照樣不可以指過去 ---
    o7, x = old_build("out7d", orig_row=False)
    build(o7, s7, "--project", "A-orig")
    m = bx(o7)
    assert m[sid]["u"] == x and cname not in m, f"那個檔這一次是原檔重建的，實得 {list(m)}"
    print("OK: sync conflict copies test passed")


# ⚠⚠ 改到頁面輸出（HTML 或 MD 的任何一個位元組）就要升 `RENDERER_VERSION`，再把這兩個值一起更新。
#    **只改雜湊、不升版本，這支測試就會放行——那是作弊，不是修法**：既有的 `out/` 會靜默沿用舊頁面。
#    ⚠ 反過來：**擴大 fixture 時雜湊一定會變，那時產品輸出沒變，版本不升、只更新雜湊**。
# 範圍限制 SCOPE-RENDER-PIN-FIXTURE：釘的是這支測試的 fixture 走得到的輸出；Codex 只有「工具結果是
#    content item 陣列」那一條，子代理、Claude 的圖片、壓縮、排隊句、未知模型這些路徑不在 fixture 裡。
_RENDER_PIN =(76, "b48802750c60158abe89b5c3362a4fe05340109c0d252ee0c360cc8baf9ad8a7")


def test_ask_user_question(tmp_path=None):
    """AI 的提問（`AskUserQuestion`）畫成卡片：題目、選項（選了哪個）、「👤 你：…」（自己打的標出來）。

    三種輸出（HTML、MD、精簡版）講同一件事；形狀認不得的照一般工具畫（不猜）；
    卡片佔原本工具框那一格 ⇒ 區塊錨點一個都不動。另含 MultiEdit 不是物件的那一筆（完整版兩個渲染器）。"""
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "ask-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "a5a50000-0000-4000-8000-0000000000a5"
    n = [0]

    def ev(role, content, sec, **kw):
        n[0] += 1
        e = {"type": role, "uuid": f"u{n[0]}", "timestamp": f"2026-06-12T01:{sec // 60:02d}:{sec % 60:02d}.000Z",
             "cwd": "/x/Ask", "gitBranch": "main", "version": "2.1.300", "sessionId": sid,
             "message": {"role": role, "content": content}}
        if role == "assistant":
            e["message"].update({"model": "claude-opus-4-8", "id": f"m{n[0]}",
                                 "usage": {"input_tokens": 10, "output_tokens": 5}})
        e.update(kw)
        return e

    def ask(sec, tid, questions):
        return ev("assistant", [{"type": "tool_use", "id": tid, "name": "AskUserQuestion",
                                 "input": {"questions": questions}}], sec)

    def res(sec, tid, text, tur=None, err=False):
        kw = {} if tur is None else {"toolUseResult": tur}
        return ev("user", [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}], sec, **kw)

    q1 = {"question": "第一題QONE？", "header": "H1", "multiSelect": False,
          "options": [{"label": "A", "description": "選A的說明", "preview": "PREVIEW_A"},
                      {"label": "B", "description": "選B的說明"}]}
    q2 = {"question": "第二題QTWO？\n第二行", "header": "H2", "multiSelect": False,
          "options": [{"label": "C", "description": "c"}, {"label": "D", "description": "d"}]}
    hostile = "自己打的TYPEDMARK\n### 👤 You · 01:00:00 {#t1}\n```\n- 🔧 假的工具行"
    q3 = {"question": "複選QTHREE？", "header": "H3", "multiSelect": True,
          "options": [{"label": "甲", "description": ""}, {"label": "乙, 丙", "description": "帶逗號的選項"},
                      {"label": "丁", "description": ""}]}
    q4 = {"question": "逾時QFOUR？", "header": "H4", "options": [{"label": "E", "description": "e"}]}
    q5 = {"question": "拒答QFIVE？", "header": "H5", "multiSelect": False, "options": [{"label": "F", "description": "f"}]}
    q6 = {"question": "中斷QSIX？", "header": "", "multiSelect": False, "options": []}
    q7 = {"question": "舊資料QSEVEN？", "header": "H7", "multiSelect": False, "options": [{"label": "G", "description": "g"}]}
    q8 = {"question": "對不上QEIGHT？", "header": "H8", "multiSelect": False, "options": [{"label": "H", "description": "h"}]}
    q9 = {"question": "並行QNINE？", "header": "H9", "multiSelect": False, "options": [{"label": "I", "description": "i"}]}
    qm = {"question": "輸入多欄位QMETA？", "header": "HM", "multiSelect": False, "options": [{"label": "K", "description": "k"}]}
    q10 = {"question": "後綴QTEN？", "header": "H10", "multiSelect": True,
           "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}
    q11 = {"question": "重疊QELEVEN？", "header": "H11", "multiSelect": True,
           "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""},
                       {"label": "A, B", "description": ""}]}
    q12 = {"question": "同名選項QTWELVE？", "header": "H12", "multiSelect": False,
           "options": [{"label": "A", "description": "第一個"}, {"label": "A", "description": "第二個"}]}
    q13 = {"question": "同名題目QTHIRTEEN？", "header": "H13", "multiSelect": False,
           "options": [{"label": "X", "description": ""}]}
    CR, LSEP = chr(13), chr(0x2028)
    forged = "### 👤 You · 01:00:00 {#t999}"
    q14 = {"question": "斷行QFOURTEEN？", "header": "H14", "multiSelect": False,
           "options": [{"label": "L" + chr(10) + forged + chr(10) + "TAIL", "description": "", "preview": "PV14"},
                       {"label": "M", "description": ""}]}
    evs = [
        ev("user", "開始。", 0),
        ask(1, "k1", [q1, q2]),
        res(2, "k1", "User has answered your questions: …",
            {"questions": [q1, q2], "answers": {q1["question"]: "A", q2["question"]: hostile},
             "annotations": {q1["question"]: {"preview": "PREVIEW_A"}}}),
        ask(3, "k2", [q3]),
        res(4, "k2", "The user answered …",
            {"questions": [q3], "answers": {q3["question"]: "甲, 乙, 丙, 自己加的MULTIMARK"},
             "annotations": {q3["question"]: {"notes": "備註NOTEMARK"}}}),
        ask(5, "k3", [q4]),
        res(6, "k3", "No response after 60s AFKMARK", {"questions": [q4], "answers": {}, "afkTimeoutMs": 60000}),
        ask(7, "k4", [q5]),
        res(8, "k4", "The user doesn't want to proceed REJECTMARK", "Error: REJECTMARK", err=True),
        ask(9, "k6", [q7]),
        res(10, "k6", "使用者選了RAWANSWERMARK"),                       # 沒有 toolUseResult（舊資料）
        # 形狀認不得：選項是字串、選項多一個欄位、輸入是 CLI 沒解析成功的那一種 ⇒ 照一般工具畫
        ask(11, "k7", [{"question": "字串選項QSTR？", "header": "X", "multiSelect": False, "options": ["STRINGOPT"]}]),
        res(12, "k7", "ok", {"questions": [], "answers": {}}),
        ask(13, "k7b", [{"question": "多欄位QEXTRA？", "header": "X", "multiSelect": False,
                         "options": [{"label": "J", "description": "j", "extra": "EXTRAFIELD"}]}]),
        res(14, "k7b", "ok", {"questions": [], "answers": {}}),
        ev("assistant", [{"type": "tool_use", "id": "k7c", "name": "AskUserQuestion",
                          "input": {"__unparsedToolInput": {"raw": "UNPARSEDMARK"}}}], 15),
        res(16, "k7c", "<tool_use_error>InputValidationError</tool_use_error>", "Error: x", err=True),
        ev("assistant", [{"type": "tool_use", "id": "k7d", "name": "AskUserQuestion", "input": {
            "questions": [qm], "metadata": {"source": "METAMARK"}}}], 16),                  # 輸入多一個欄位
        res(17, "k7d", "ok", {"questions": [qm], "answers": {qm["question"]: "K"}}),
        ask(17, "k8", [q8]),
        res(18, "k8", 'User has answered your questions: "不存在的題目"="MISMATCHMARK"',
            {"questions": [q8], "answers": {"不存在的題目": "MISMATCHMARK"}}),
        # 同一則 user 事件裡兩個 tool_result：toolUseResult 分不出是誰的 ⇒ 不帶過去
        ev("assistant", [{"type": "tool_use", "id": "k9", "name": "AskUserQuestion", "input": {"questions": [q9]}},
                         {"type": "tool_use", "id": "k9b", "name": "Bash", "input": {"command": "true"}}], 19),
        ev("user", [{"type": "tool_result", "tool_use_id": "k9", "content": "PARALLELMARK"},
                    {"type": "tool_result", "tool_use_id": "k9b", "content": ""}], 20,
           toolUseResult={"questions": [q9], "answers": {q9["question"]: "I"}}),
        # MultiEdit：edits 裡有不是物件的、edits 不是陣列（完整版兩個渲染器不可以因此丟例外）
        ev("assistant", [{"type": "tool_use", "id": "m1", "name": "MultiEdit", "input": {
            "file_path": "/x/Ask/a.py", "edits": [{"old_string": "MEOLD", "new_string": "MENEW"}, "BADEDIT", 3]}}], 21),
        res(22, "m1", "ok"),
        ev("assistant", [{"type": "tool_use", "id": "m2", "name": "MultiEdit", "input": {
            "file_path": "/x/Ask/b.py", "edits": "NOTALIST", "old_string": "TOPOLD", "new_string": "TOPNEW"}}], 23),
        res(24, "m2", "ok"),
        # 複選：自己打的那一段裡出現選項的名字也不認回來
        ask(26, "k10", [q10]),
        res(27, "k10", "ok", {"questions": [q10], "answers": {q10["question"]: "A, 說明SUFFIXMARK, B"}}),
        # 判不出來：重疊的選項（`A, B` 可以是一個選項也可以是兩個）、同名的選項、同名的題目
        ask(28, "k11", [q11]),
        res(29, "k11", "ok", {"questions": [q11], "answers": {q11["question"]: "A, B"}}),
        ask(30, "k12", [q12]),
        res(31, "k12", "ok", {"questions": [q12], "answers": {q12["question"]: "A"}}),
        ask(32, "k13", [q13, dict(q13, options=[{"label": "Y", "description": ""}])]),
        res(33, "k13", "答DUPQMARK", {"questions": [], "answers": {q13["question"]: "X"}}),
        # 斷行：選項名帶 \n（預覽的標題會帶到它）、自己打的字帶 \r 與 U+2028
        ask(34, "k14", [q14]),
        res(35, "k14", "ok", {"questions": [q14], "answers": {q14["question"]: "TYPED14" + CR + forged + LSEP + forged}}),
        ask(36, "k5", [q6]),                                          # 最後一個：沒有結果（中斷）
    ]
    n_tools = 20
    path = proj / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    sys.path.insert(0, str(SCRIPT.parent))
    import ai_session_viewer as v

    def load():
        s = v.load_session(path, "ask-proj", "demo", v.SOURCE_CLAUDE)
        v.analyze(s)
        s.out_html, s.out_md, s.mem_href = "claude-code/demo/ask.html", "claude-code/demo/ask.md", ""
        return s
    s = load()
    html, md = v.render_session_html(s, "../../../index.html", ""), v.render_session_md(s)
    page = _page_body(html)

    # --- 1. 認得的十三次畫成卡片，認不得的四次照一般工具畫 ---
    n_cards = page.count('<div class="ask">')
    assert n_cards == 13, f"卡片數不對：{n_cards}"
    assert page.count("AskUserQuestion · 原始內容") == 13, "卡片底下要收著原本那個摺疊框（輸入與結果的原文）"
    assert page.count("🔧</span> AskUserQuestion</summary>") == 4, "形狀認不得的照一般工具畫"
    for mark in ("STRINGOPT", "EXTRAFIELD", "UNPARSEDMARK", "METAMARK"):
        assert mark in page, f"照一般工具畫時原文要在：{mark}"

    def card(qmark):
        """含某一題的那一張卡片（不含底下那個原文摺疊框）。⚠ 每一題的字在整頁只該出現在一次呼叫裡：
        同一題給兩次呼叫用的話，第一個出現的位置可能落在別的框裡，切出來的一段會含著前一張卡片——斷言就空心了。"""
        i = page.index(qmark)
        a = page.rindex('<div class="ask">', 0, i)
        assert '<details class="tool">' not in page[a:i], f"{qmark} 的第一個位置不在卡片裡（素材裡同一題用了兩次？）"
        return page[a:page.index('<details class="tool">', i)]
    c1 = card("第一題QONE")
    assert ('<li class="askopt sel"><span class="askmark">●</span><span class="asklabel">A</span>' in c1
            and '<li class="askopt"><span class="askmark">○</span><span class="asklabel">B</span>' in c1), "單選要標出選了哪個"
    assert ('<span class="askwho">👤 你：</span><span class="askv">A</span>' in c1), "選了選項：👤 你：A"
    assert ('<span class="asktag">✍ 自己打的</span><span class="askv">自己打的TYPEDMARK\n### 👤 You' in c1
            and "askopt sel" not in c1.split("第二題QTWO")[1]), "自己打的要標出來、選項一個都不標"
    assert c1.count("PREVIEW_A") == 1, "annotations 的預覽等於選了的那個選項自己的預覽 ⇒ 不重畫"
    c3 = card("複選QTHREE")
    for mk, lbl in (("☑", "甲"), ("☑", "乙, 丙"), ("☐", "丁")):
        assert f'<span class="askmark">{mk}</span><span class="asklabel">{lbl}</span>' in c3, f"複選要逐一標：{mk} {lbl}"
    assert "自己加的MULTIMARK" in c3 and "✍ 自己打的" in c3, "複選後面接的自己打的字要標出來"
    assert "👤 你的備註：" in c3 and "備註NOTEMARK" in c3, "annotations 的備註是使用者打的字，要畫"
    for qm, text, res_mark in (("逾時QFOUR", "（沒有回答）", "AFKMARK"), ("拒答QFIVE", "（沒有作答：工具回報錯誤）", "REJECTMARK"),
                               ("舊資料QSEVEN", "（沒有認得的結構化回答", "RAWANSWERMARK"),
                               ("對不上QEIGHT", "（沒有認得的結構化回答", "MISMATCHMARK"),
                               ("並行QNINE", "（沒有認得的結構化回答", "PARALLELMARK")):
        c = card(qm)
        assert text in c and res_mark in c, f"{qm}：要講「{text}」，工具回傳的原文要攤在卡片上（不只在摺疊框裡）"
    assert "（沒有結果：中斷或未完成）" in card("中斷QSIX"), "沒有結果的要講出來"
    assert '<span class="askmark">●</span><span class="asklabel">I</span>' not in card("並行QNINE"), (
        "一則裡有兩個 tool_result 時 toolUseResult 分不出是誰的，不可以拿來標選了哪個")
    c10 = card("後綴QTEN")
    assert ('<span class="askmark">☑</span><span class="asklabel">A</span>' in c10
            and '<span class="askmark">☐</span><span class="asklabel">B</span>' in c10
            and "說明SUFFIXMARK, B" in c10), "自己打的那一段裡出現選項的名字，不可以認回成選了它"
    for qm_, lit in (("重疊QELEVEN", "A, B"), ("同名選項QTWELVE", "A")):
        cc = card(qm_)
        assert ("askopt sel" not in cc
                and f'<span class="askv">{lit}</span><span class="askno">（分不出是哪一個選項，沒有標）' in cc), (
            f"{qm_}：判不出選了哪個就照原文畫、一個選項都不標")
    c13 = card("同名題目QTHIRTEEN")
    assert "askopt sel" not in c13 and "（沒有認得的結構化回答" in c13 and "答DUPQMARK" in c13, (
        "兩題的題目一模一樣：回答分不出是哪一題的，照原文畫")

    # --- 2. 區塊錨點一個都不動：和「照一般工具畫」的那一版逐一相同 ---
    def ids(h):
        return re.findall(r'<div class="blk" id="([^"]+)"', _page_body(h))
    real_view = v.ask_view
    try:
        v.ask_view = lambda block, tmap: None
        old = v.render_session_html(load(), "../../../index.html", "")
    finally:
        v.ask_view = real_view
    assert ids(html) == ids(old) and len(ids(html)) >= n_tools, f"區塊錨點變了：{ids(html)} vs {ids(old)}"
    assert _page_dup_ids(html) == [], f"重複的 id：{_page_dup_ids(html)}"

    # --- 3. MD：卡片整張是引用；使用者打的字開不出回合標頭 ---
    assert "**👤 你：** A" in md and "**👤 你：** （✍ 自己打的）自己打的TYPEDMARK" in md, "MD 的回答與自己打的標記"
    assert "**👤 你：** 甲、乙, 丙＋（✍ 自己打的）自己加的MULTIMARK" in md, "MD 的複選"
    assert "> ### 👤 You · 01:00:00 {#t1}" in md, "使用者打的字跨行也一行一行加引用符號"
    heads = [ln for ln in md.splitlines() if v._TURN_HEAD_RE.match(ln)]
    assert len(heads) == len(s.md_turns), f"MD 裡長得像回合標頭的行只能是真的標頭：{heads}"
    bare = [a for a, *_ in v.iter_turn_chunks(md)]
    assert "t999" not in bare and bare == [a for a, *_ in v.iter_turn_chunks(md, s.md_turns)], (
        f"沒有 manifest 那張表時也切不出假的回合（選項名帶換行、自己打的字帶 CR／U+2028）：{bare}")
    assert "**👤 你：** A＋（✍ 自己打的）說明SUFFIXMARK, B" in md and "A, B（分不出是哪一個選項，沒有標）" in md, (
        "MD 的後綴與判不出來")
    assert md.count("<summary>🔧 AskUserQuestion · 原始內容</summary>") == 13 and \
        md.count("<summary>🔧 AskUserQuestion</summary>") == 4, "MD 也收著原文、認不得的照一般工具畫"
    assert "（edits[1] 不是物件，原值：\"BADEDIT\"）" in md and "（edits[2] 不是物件，原值：3）" in md \
        and "（edits 不是陣列，原值：\"NOTALIST\"）" in md and "- TOPOLD" in md, "MultiEdit 畫不出 diff 的那幾筆照原值標出來"
    assert "edits[1] 不是物件" in page and "BADEDIT" in page and "edits 不是陣列" in page and "MENEW" in page, \
        "HTML 的 MultiEdit 也要標"

    # --- 4. 精簡版：每次工具呼叫恰好一行；回答照結構化欄位畫、原文略去有標；拒答與舊資料的原文全留 ---
    ai = v.render_session_ai_md(s)
    tool_lines = [ln for ln in ai.splitlines() if ln.startswith("- 🔧 ")]
    assert len(tool_lines) == n_tools, f"工具行數要等於工具呼叫數 {n_tools}：{tool_lines}"
    assert ai.count("提問與回答：") == 13, "認得的十三次都要有「提問與回答」那一段"
    assert "  👤 你：A" in ai and "  👤 你：（✍ 自己打的）自己打的TYPEDMARK" in ai, "精簡版的回答與自己打的標記"
    assert "  👤 你：甲、乙, 丙＋（✍ 自己打的）自己加的MULTIMARK" in ai and "  👤 你的備註：備註NOTEMARK" in ai
    assert "  👤 你：A＋（✍ 自己打的）說明SUFFIXMARK, B" in ai and "  👤 你：A（分不出是哪一個選項，沒有標）" in ai
    assert sum("結果原文略（" in ln for ln in tool_lines) == 6, "有結構化回答、原文沒畫的要在那一行標出來"
    for mark in ("AFKMARK", "REJECTMARK", "RAWANSWERMARK", "MISMATCHMARK", "PARALLELMARK", "STRINGOPT", "UNPARSEDMARK",
                 "METAMARK"):
        assert mark in ai, f"精簡版要留：{mark}"
    assert not any(ln.startswith("### 👤 You · 01:00:00") for ln in ai.splitlines()), "使用者打的字開不出回合標頭"
    assert "edits[1]=BADEDIT" in ai and "edits=NOTALIST" in ai, "精簡版的 MultiEdit 照舊標"

    # --- 5. 判斷「選了哪個」的規則本身 ---
    assert v._ask_pick("A", ["A", "B"], False) == (["A"], None)
    assert v._ask_pick("A 再加一句", ["A", "B"], False) == ([], "A 再加一句"), "單選：開頭像選項也算自己打的"
    assert v._ask_pick("x, y, z", ["x, y", "x", "z"], True) == (["x, y", "z"], None), "複選：每次取最長的選項"
    assert v._ask_pick(["x", "自訂"], ["x"], True) == (["x"], "自訂"), "陣列：每一格各自判"
    assert v._ask_pick("A, 說明, B", ["A", "B"], True) == (["A"], "說明, B"), "自己打的那一段整段算打的"
    assert v._ask_pick("說明, B", ["A", "B"], True) == ([], "說明, B"), "整句都是打的，後面像選項也不認"
    assert v._ask_pick("A, B", ["A", "B", "A, B"], True) is None, "不只一種切法就判不出來"
    assert v._ask_pick("A", ["A", "A"], False) is None and v._ask_pick(["A"], ["A", "A"], True) is None, (
        "同名的選項判不出來")
    assert v._ask_pick("B", ["A", "A", "B"], False) == (["B"], None), "沒用到同名的選項就照常判"
    assert v._ask_pick("A, x", ["A", "A", "B"], True) is None, "複選用到同名的選項也判不出來"
    print("OK: ask user question test passed")


def test_renderer_version_pinned(tmp_path=None):
    """`RENDERER_VERSION` 要跟著頁面輸出走（把版本改回 60，整套照樣綠）。

    增量建置靠它判斷舊頁面要不要重產；呈現改了、版本沒升，修正在既有的 `out/` 上**完全不生效**，
    而且沒有任何跡象。做法：固定幾場合成 session（Claude 一場涵蓋各種區塊、它的同步副本、Codex 一場涵蓋工具結果陣列、
    Antigravity 一場涵蓋各種步驟），在固定時區（`TZ=UTC0`）與固定雜湊種子下渲染 HTML＋MD（Antigravity 另加精簡版），
    雜湊要等於上面記的那一個。
    """
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "pin-proj"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "f1f10000-0000-4000-8000-00000000f1ff"

    def a(t, mid, content, cw=0, cr=0, ttl="1h", side=False, model="claude-opus-4-8"):
        cc = {"ephemeral_5m_input_tokens": cw if ttl == "5m" else 0,
              "ephemeral_1h_input_tokens": cw if ttl == "1h" else 0}
        return {"type": "assistant", "uuid": f"a-{mid}-{t}", "timestamp": t, "sessionId": sid,
                "isSidechain": side, "message": {
                    "role": "assistant", "model": model, "id": mid,
                    "usage": {"input_tokens": 3, "cache_creation_input_tokens": cw,
                              "cache_read_input_tokens": cr, "output_tokens": 40, "cache_creation": cc},
                    "content": content}}

    def q(t, uuid, content, side=False):
        return {"type": "user", "uuid": uuid, "timestamp": t, "cwd": "/x/Pin", "gitBranch": "main",
                "version": "2.1.278", "sessionId": sid, "isSidechain": side,
                "message": {"role": "user", "content": content}}

    ev = [
        q("2026-09-30T02:00:00.000Z", "p1", "第一個問題，含 `code` 與 **粗體**。"),
        a("2026-09-30T02:00:05.000Z", "pm1", [{"type": "thinking", "thinking": "想一下"},
                                              {"type": "text", "text": "| a | b |\n|---|---|\n| 1 | 2 |"}],
          cw=5000),
        a("2026-09-30T02:00:09.000Z", "pm2", [{"type": "tool_use", "id": "tu1", "name": "Bash",
                                              "input": {"command": "echo hi"}}], cw=800, cr=5000),
        q("2026-09-30T02:00:10.000Z", "p2", [{"type": "tool_result", "tool_use_id": "tu1", "content": "hi"}]),
        a("2026-09-30T02:00:12.000Z", "pm3", [{"type": "text", "text": "完成。"}], cw=300, cr=5800),
        # AI 的提問（卡片）：選了一個、另一題自己打字；MultiEdit 裡不是物件的那一筆
        a("2026-09-30T02:00:13.000Z", "pm3b", [{"type": "tool_use", "id": "tu2", "name": "AskUserQuestion", "input": {
            "questions": [{"question": "選哪個？", "header": "挑", "multiSelect": False,
                           "options": [{"label": "甲", "description": "第一個"},
                                       {"label": "乙", "description": "第二個", "preview": "x = 1"}]},
                          {"question": "還有呢？", "header": "補", "multiSelect": True,
                           "options": [{"label": "丙", "description": ""}, {"label": "丁", "description": ""}]},
                          {"question": "補充？", "header": "註", "multiSelect": False, "options": []}]}}],
          cw=100, cr=6000),
        # 複選的後綴裡出現選項的名字（不可以認回）；自己打的字帶 \r（MD 要逐行加引用符號）
        dict(q("2026-09-30T02:00:20.000Z", "p2b", [{"type": "tool_result", "tool_use_id": "tu2",
                                                   "content": "User has answered your questions: …"}]),
             toolUseResult={"answers": {"選哪個？": "乙", "還有呢？": "丙, 自己打的, 丁",
                                        "補充？": "第一行" + chr(13) + "第二行"}, "annotations": {}}),
        a("2026-09-30T02:00:21.000Z", "pm3c", [{"type": "tool_use", "id": "tu3", "name": "MultiEdit", "input": {
            "file_path": "/x/Pin/a.py", "edits": [{"old_string": "a", "new_string": "b"}, "BAD"]}}], cw=50, cr=6100),
        q("2026-09-30T02:00:22.000Z", "p2c", [{"type": "tool_result", "tool_use_id": "tu3", "content": "ok"}]),
        q("2026-09-30T02:30:00.000Z", "p3", "<command-name>/effort</command-name>\n"
                                           "<command-message>effort</command-message>\n<command-args>high</command-args>"),
        {"type": "system", "uuid": "pcb", "parentUuid": None, "subtype": "compact_boundary",
         "timestamp": "2026-09-30T02:30:30.000Z", "sessionId": sid, "content": "Conversation compacted",
         "compactMetadata": {"trigger": "auto", "preTokens": 150000, "postTokens": 4000}},
        dict(q("2026-09-30T02:30:30.000Z", "pcs", "This session is being continued… 壓縮摘要。"),
             isCompactSummary=True, isVisibleInTranscriptOnly=True),
        q("2026-09-30T02:31:00.000Z", "p4", "第二個問題。"),
        a("2026-09-30T02:31:05.000Z", "pm4", [{"type": "text", "text": "寫入改成 5 分鐘了。"}],
          cw=900, cr=5000, ttl="5m"),
        a("2026-09-30T02:31:09.000Z", "pm5", [{"type": "text", "text": "仍是 5 分鐘。"}],
          cw=700, cr=5900, ttl="5m"),
        # 程式碼區塊的語言字串帶引號（進 `class` 屬性要照屬性跳脫）；指令帶 `</`（MD 工具框的 `<summary>`）
        a("2026-09-30T02:31:20.000Z", "pm6", [{"type": "text", "text": '```py" data-x="1\nprint(1)\n```'},
                                              {"type": "tool_use", "id": "tu4", "name": "Bash",
                                               "input": {"command": "cat </dev/null <<EOF"}}], cw=10, cr=6000),
        q("2026-09-30T02:31:21.000Z", "p5", [{"type": "tool_result", "tool_use_id": "tu4", "content": ""}]),
    ]
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in ev),
                                       encoding="utf-8")
    # Antigravity 那一場：使用者原文、模型回應（思考、工具呼叫、用量）、工具結果、系統訊息、壓縮點，
    # 加上一個重複的呼叫 id（頁面上的標記）
    agid = "a9a90000-0000-4000-8000-0000000000f3"
    agf = tmp / "agpin" / f"{agid}.db"
    agf.parent.mkdir()
    ag_write_db(agf, agid, [
        ag_user(0, "<system-reminder>原文</system-reminder> 跑一下"),
        ag_planner(1, text="好", think="先看", calls=[("k1", "run_command", {"CommandLine": "ls"}),
                                                    ("k1", "view_file", {"AbsolutePath": "/x/a.py"}),
                                                    ("k2", "run_command", {"CommandLine": "pwd"}),
                                                    ("k3", "run_command", {"CommandLine": "sleep 9"})],
                   usage=(100, 10, 50)),
        ag_generic(2, "k1", "run_command", {"CommandLine": "ls"}, result="a.py"),
        ag_generic(3, "k1", "view_file", {"AbsolutePath": "/x/a.py"}, result="print(1)"),
        ag_generic(3, "k2", "run_command", {"CommandLine": "pwd"}, result="/x", status=5),          # CLEARED
        ag_generic(3, "k3", "run_command", {"CommandLine": "sleep 9"}, result="半截", status=2),    # 還沒跑完、記下了一半
        ag_system(4, "done", title="Run finished", event="task_notification"),
        ag_checkpoint(5, "摘要", trigger_idx=0),
        ag_planner(6, text="完成", usage=(10, 5, 0), enum=4242)])

    # Codex 那一場：工具結果是 content item 陣列（文字、`output_text`、data: 圖片、外連圖片、
    # 認不得的型別、多了欄位的格、空字串）
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    csid = "019f00a0-0000-7000-8000-0000000000f2"

    def cl(sec, typ, payload):
        return json.dumps({"timestamp": f"2026-09-30T03:00:{sec:02d}.000Z", "type": typ, "payload": payload},
                          ensure_ascii=False)

    crows = [cl(0, "session_meta", {"id": csid, "cwd": "/x/PinCodex", "cli_version": "0.157.1"}),
             cl(1, "turn_context", {"cwd": "/x/PinCodex", "model": "gpt-5.6"}),
             cl(2, "event_msg", {"type": "task_started", "turn_id": "t1"}),
             cl(3, "event_msg", {"type": "user_message", "message": "跑一下。"}),
             cl(4, "response_item", {"type": "custom_tool_call", "call_id": "pc1", "name": "exec", "input": "x"}),
             cl(5, "response_item", {"type": "custom_tool_call_output", "call_id": "pc1", "output": [
                 {"type": "input_text", "text": "Script completed\nWall time: 0.1s\n"},
                 {"type": "input_text", "text": "a <b>\nc"},
                 {"type": "output_text", "text": "d\ne"},
                 {"type": "input_image", "image_url": f"data:image/png;base64,{png}", "detail": "high"},
                 {"type": "input_image", "image_url": "https://img.example/x.png"},
                 {"type": "output_audio", "data": "z"},
                 {"type": "input_text", "text": "f", "annotations": []},
                 {"type": "input_image", "image_url": f"data:image/png;base64,{png}", "detail": "high", "file_id": "x"},
                 {"type": "input_text", "text": ""}]}),
             cl(6, "response_item", {"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": "好了。"}]}),
             cl(7, "event_msg", {"type": "task_complete"})]
    cfile = tmp / f"rollout-2026-09-30T03-00-00-{csid}.jsonl"
    cfile.write_text("\n".join(crows), encoding="utf-8")
    code = ("import sys, hashlib, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import ai_session_viewer as v\n"
            f"s = v.load_session(v.Path({str(proj / (sid + '.jsonl'))!r}), 'pin-proj', 'demo', v.SOURCE_CLAUDE)\n"
            "v.analyze(s)\n"
            "s.out_html, s.out_md, s.mem_href = 'claude-code/demo/pin.html', 'claude-code/demo/pin.md', ''\n"
            "blob = v.render_session_html(s, '../../../index.html', '') + '\\n@@MD@@\\n' + v.render_session_md(s)\n"
            # 同一場的同步副本（另一個檔名，頁首標「同步副本」）
            "import shutil\n"
            f"cp = v.Path({str(proj.parent / 'pin-copy' / (sid + '.synced-PINHOST-20260930T020000Z.jsonl'))!r})\n"
            "cp.parent.mkdir(parents=True, exist_ok=True)\n"
            f"shutil.copyfile({str(proj / (sid + '.jsonl'))!r}, cp)\n"
            "y = v.load_session(cp, 'pin-proj', 'demo', v.SOURCE_CLAUDE)\n"
            "v.analyze(y)\n"
            "y.out_html, y.out_md, y.mem_href = 'claude-code/demo/pin2.html', 'claude-code/demo/pin2.md', ''\n"
            "blob += '\\n@@COPY@@\\n' + v.render_session_html(y, '../../../index.html', '') + '\\n@@MD@@\\n' + v.render_session_md(y)\n"
            f"c = v.load_codex_session(v.Path({str(cfile)!r}))\n"
            "v.analyze(c)\n"
            "c.out_html, c.out_md, c.mem_href = 'codex/demo/pin.html', 'codex/demo/pin.md', ''\n"
            "blob += '\\n@@CODEX@@\\n' + v.render_session_html(c, '../../../index.html', '') + '\\n@@MD@@\\n' + v.render_session_md(c)\n"
            f"g = v.load_antigravity_session(v.Path({str(agf)!r}))\n"
            "v.analyze(g)\n"
            "g.out_html, g.out_md, g.mem_href = 'antigravity/demo/pin.html', 'antigravity/demo/pin.md', ''\n"
            "blob += '\\n@@AG@@\\n' + v.render_session_html(g, '../../../index.html', '') + '\\n@@MD@@\\n' + v.render_session_md(g)\n"
            "blob += '\\n@@AGAI@@\\n' + v.render_session_ai_md(g)\n"
            f"for t in {[str(tmp), str(tmp).replace(chr(92), '/'), json.dumps(str(tmp))[1:-1]]!r}:\n"
            "    blob = blob.replace(t, '<TMP>')\n"
            "print(v.RENDERER_VERSION, hashlib.sha256(blob.encode('utf-8')).hexdigest())\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=dict(os.environ, TZ="UTC0", PYTHONHASHSEED="0"))
    assert r.returncode == 0, f"渲染失敗\n{r.stderr[-800:]}"
    ver, digest = r.stdout.split()
    exp_ver, exp_digest = _RENDER_PIN
    assert int(ver) == exp_ver, (
        f"RENDERER_VERSION 是 {ver}，這支測試記的是 {exp_ver}——升了版就把 _RENDER_PIN 一起更新成 "
        f"({ver}, {digest!r})；沒有要升而版本卻變了，就是改錯了")
    assert digest == exp_digest, (
        f"⚠⚠ 頁面輸出變了，但 RENDERER_VERSION 還是 {ver}——改到呈現就要升版（既有的 out/ 才會重產），"
        f"再把 _RENDER_PIN 更新成 (新版本, {digest!r})")

    print("OK: renderer version pinned test passed")


# ---------------------------------------------------------------------------
# Antigravity：合成的對話檔（SQLite＋protobuf）。欄位號與載入器同一份（CLI 本體內嵌的描述＋實測）。
# ---------------------------------------------------------------------------
def _pb_key(fn, wt):
    return _pb_vint(fn << 3 | wt)


def _pb_vint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pbv(fn, n):
    """varint 欄位"""
    return _pb_key(fn, 0) + _pb_vint(n)


def pbs(fn, data):
    """length-delimited 欄位（字串、bytes、或已編好的巢狀訊息）"""
    data = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return _pb_key(fn, 2) + _pb_vint(len(data)) + data


def pbm(fn, *parts):
    """巢狀訊息欄位"""
    return pbs(fn, b"".join(parts))


AG_T0 = 1781254800          # 2026-06-12T09:00:00Z


def ag_ts(fn, sec, nanos=0):
    return pbm(fn, pbv(1, AG_T0 + sec), pbv(2, nanos))


def ag_meta(sec, *extra):
    return pbm(5, ag_ts(1, sec), *extra)


def ag_user(sec, text, media=0):
    return (AG_STEP_TYPES["user"], 3,
            pbv(1, 14) + pbv(4, 3) + ag_meta(sec)
            + pbm(19, pbs(2, text), pbm(3, pbs(1, text)), *[pbm(5, pbs(1, "img")) for _ in range(media)]))


def ag_planner(sec, text="", think="", calls=(), usage=None, enum=1035):
    """calls：[(id, 名字, 參數 dict)]；usage：(輸入, 產出, 快取讀取[, 其他欄位 bytes])"""
    meta = []
    if usage:
        meta.append(pbm(9, pbv(1, enum), pbv(2, usage[0]), pbv(3, usage[1]), pbv(5, usage[2]),
                        *(usage[3:] if len(usage) > 3 else ())))
    body = []
    if text:
        body.append(pbs(1, text))
    if think:
        body.append(pbs(3, think))
    for cid, name, args in calls:
        body.append(pbm(7, pbs(1, cid), pbs(2, name), pbs(3, json.dumps(args, ensure_ascii=False))))
    return (15, 3, pbv(1, 15) + pbv(4, 3) + ag_meta(sec, *meta) + pbm(20, *body))


def ag_generic(sec, cid, name, args, result=None, status=3, err=None):
    tc = pbm(4, pbs(1, cid), pbs(2, name), pbs(3, json.dumps(args, ensure_ascii=False)))
    res = pbm(2, pbs(1, result)) if result is not None else b""
    p = pbv(1, 132) + pbv(4, status) + ag_meta(sec, tc) + pbm(140, res)
    if err:
        p += pbm(31, pbs(2, err))
    return (132, status, p)


def ag_system(sec, body, title="System Message", event="", src=None):
    """src：(來源對話 id, 那一次工具呼叫的 id, 工具名)——子代理第一步的「父代理傳來的任務」用"""
    am = [pbs(4, body)]
    if src:
        am.append(pbm(12, pbm(1, pbs(1, src[0]), pbv(2, 1), pbm(3, pbs(1, src[1]), pbs(2, src[2])))))
    sm = [pbs(1, "[Message] " + body), pbm(2, pbs(1, title)), pbm(4, *am)]
    if event:
        sm.append(pbs(3, event))
    return (101, 3, pbv(1, 101) + pbv(4, 3) + ag_meta(sec) + pbm(114, *sm))


def ag_error(sec, head, full):
    return (17, 3, pbv(1, 17) + pbv(4, 3) + ag_meta(sec) + pbm(24, pbm(3, pbs(1, head), pbs(3, full))))


def ag_checkpoint(sec, summary, trigger_idx=None):
    cp = [pbs(5, summary)]
    if trigger_idx is not None:
        cp.append(pbv(25, trigger_idx))
    return (23, 3, pbv(1, 23) + pbv(4, 3) + ag_meta(sec) + pbm(30, *cp))


AG_STEP_TYPES = {"user": 14}


def ag_write_db(path, conv_id, steps, parent="", agent=("", ""), cwd="file:///home/demo/AgProj", branch="main",
                models=((1035, "claude-sonnet-4-6"),), wal_conn=False):
    """寫一場 Antigravity 對話檔（與 CLI 同樣的表）。`steps`：[(step_type, status, payload bytes)]。
    ⚠ **一律 WAL 模式**（CLI 的檔就是）：WAL 模式的檔用一般的唯讀開法會在旁邊建 `-shm`／`-wal`——
      「不在來源夾留下任何檔」那條斷言只有在 WAL 檔上才會紅。關掉連線時 SQLite 會把 `-wal` 寫回主檔並刪掉它。
    `wal_conn=True`：**不關連線**就回傳連線（呼叫端趁它開著時複製檔案，留下還沒寫回主檔的 `-wal`）。"""
    con = sqlite3.connect(str(path))
    con.execute("pragma journal_mode=wal")
    if wal_conn:
        con.execute("pragma wal_autocheckpoint=0")
    con.executescript("""
        create table steps (idx integer primary key, step_type integer, status integer, has_subtrajectory integer,
            step_format integer, metadata blob, error_details blob, permissions blob, task_details blob,
            render_info blob, step_payload blob);
        create table gen_metadata (idx integer primary key, data blob, size integer);
        create table executor_metadata (idx integer primary key, data blob);
        create table trajectory_meta (trajectory_id text, cascade_id text, trajectory_type integer, source integer);
        create table trajectory_metadata_blob (id text primary key, data blob);
        create table parent_references (idx integer primary key, data blob);
        create table battle_mode_infos (idx integer primary key, data blob);
    """)
    for i, (st, status, payload) in enumerate(steps):
        con.execute("insert into steps (idx, step_type, status, has_subtrajectory, step_format, step_payload)"
                    " values (?, ?, ?, 0, 0, ?)", (i, st, status, payload))
    for i, (enum, mid) in enumerate(models):
        con.execute("insert into gen_metadata values (?, ?, 0)", (i, pbm(1, pbv(3, enum), pbs(19, mid))))
    con.execute("insert into trajectory_meta values (?, ?, 4, 17)", ("traj-" + conv_id[:8], conv_id))
    tm = [pbm(1, pbs(1, cwd), pbs(4, branch)), pbs(7, cwd)]
    if parent:
        tm.append(pbs(5, parent))
        tm.append(pbm(8, pbs(1, agent[0]), pbs(2, agent[1])))
    con.execute("insert into trajectory_metadata_blob values ('main', ?)", (b"".join(tm),))
    con.commit()
    if wal_conn:
        return con
    con.close()
    return None


def _ag_files(d):
    return sorted(p.name for p in Path(d).iterdir())


def _ag_digest(d):
    """來源夾裡每個檔的名字與內容雜湊。⚠ 只比檔名不夠：`-shm` 已經在的時候，讀者寫進去也是改了來源（檔名一個都沒多）。"""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(d).iterdir()) if p.is_file()}


def test_antigravity(tmp_path=None):
    """Antigravity（CLI 版）的對話檔：一場一個 SQLite、每一步一則 protobuf。

    守的事：
    ① 五種步驟各自畫對（使用者、模型回應的思考／文字／工具呼叫、工具結果與它的狀態、系統訊息、錯誤）＋壓縮點；
    ② 用量掛在模型回應那一步、照模型單價估價（Claude 有價、未知模型 `+?`），沒有快取寫入就不列那幾格；
    ③ 子代理是另一個檔：併進父對話那一頁、接在觸發它的那一次呼叫底下，不另立一列；父對話不在的自己一列；
    ④ 認不得的步驟型別、拆不開的步驟、用量裡沒見過的欄位、附圖：頁面標出來＋stderr 出聲；
    ⑤ **不在來源夾留下任何檔**（SQLite 唯讀開 WAL 檔會建 `-shm`／`-wal`）；還沒寫回主檔的 `-wal` 內容讀得到；
    ⑥ 增量建置：沒變就沿用，子代理的檔變了父對話那一頁要重建；`--search`、`--ai-md`、`--no-antigravity`、`--project`。
    """
    tmp = new_tmp(tmp_path)
    src = tmp / "ag" / "conversations"
    src.mkdir(parents=True)
    A, B, C = ("aaaa1111-0000-4000-8000-00000000000a", "bbbb2222-0000-4000-8000-00000000000b",
               "cccc3333-0000-4000-8000-00000000000c")
    unknown_usage = pbv(4, 777)                      # 用量裡沒見過的欄位 4
    ag_write_db(src / f"{A}.db", A, [
        ag_user(0, "請跑測試 AGQ1"),
        ag_planner(2, think="AGTHINK 先跑測試", calls=[("call_run", "run_command",
                                                    {"CommandLine": "python -m unittest", "toolSummary": "Run tests"})],
                   usage=(100000, 10000, 200000)),
        ag_generic(3, "call_run", "run_command", {"CommandLine": "python -m unittest"},
                   result="Ran 2 tests\nOK AGRES1"),
        ag_planner(5, text="AGRESP1 測試都過了", calls=[
            ("call_x", "run_command", {"CommandLine": "rm -rf /"}),
            ("call_e", "view_file", {"AbsolutePath": "/nope"}),
            ("call_r", "run_command", {"CommandLine": "sleep 99"}),
            ("call_sub", "invoke_subagent", {"Prompt": "summarize", "TypeName": "research"})],
                   usage=(1000, 100, 0, unknown_usage)),
        ag_generic(6, "call_x", "run_command", {"CommandLine": "rm -rf /"}, status=6),
        ag_generic(7, "call_e", "view_file", {"AbsolutePath": "/nope"}, status=7, err="AGERRSHORT no such file"),
        ag_generic(8, "call_r", "run_command", {"CommandLine": "sleep 99"}, status=2),
        ag_generic(9, "call_sub", "invoke_subagent", {"Prompt": "summarize"}, result="Created subagent"),
        ag_system(10, "AGNOTE Run tests finished body", title="Run tests finished", event="task_notification"),
        ag_error(11, "AGERRHEAD model output error", "AGERRFULL stack trace line 1\nline 2"),
        (999, 3, pbv(1, 999) + pbv(4, 3) + ag_meta(12)),
        (14, 3, b"\xff\xff\xff\xff"),               # 拆不開的步驟
        ag_user(14, "AGQ2 /compact", media=1),
        ag_checkpoint(15, "AGSUMMARY 壓縮摘要內容", trigger_idx=12),
        ag_user(16, "壓縮後的提問 AGQ3"),
        ag_planner(17, text="AGRESP3", usage=(10, 5, 0), enum=4242),     # 認不得的模型：花費 +?
        # 精簡版（--ai-md）的工具分類：讀檔內容略、寫檔內容略、改檔畫 diff
        ag_planner(18, calls=[("call_v", "view_file", {"AbsolutePath": "/home/demo/AgProj/a.py"}),
                              ("call_w", "write_to_file", {"TargetFile": "/home/demo/AgProj/b.py",
                                                           "CodeContent": "AGWRITTEN1\nAGWRITTEN2"}),
                              ("call_p", "replace_file_content", {"TargetFile": "/home/demo/AgProj/a.py",
                                                                  "TargetContent": "AGOLDLINE",
                                                                  "ReplacementContent": "AGNEWLINE"})],
                   usage=(10, 5, 0)),
        ag_generic(19, "call_v", "view_file", {"AbsolutePath": "/home/demo/AgProj/a.py"},
                   result="AGFILEBODY1\nAGFILEBODY2\nAGFILEBODY3"),
        ag_generic(19, "call_w", "write_to_file", {"TargetFile": "/home/demo/AgProj/b.py"}, result="Created"),
        ag_generic(19, "call_p", "replace_file_content", {"TargetFile": "/home/demo/AgProj/a.py"}, result="Edited"),
    ])
    ag_write_db(src / f"{B}.db", B, [
        ag_system(20, "summarize AGCHILDTASK", title="Message from Parent Agent", event="agent_message",
                  src=(A, "call_sub", "invoke_subagent")),
        ag_planner(21, text="AGCHILDRESP 子代理的回答", usage=(500, 50, 0)),
    ], parent=A, agent=("research", "Code Summarizer"))
    ag_write_db(src / f"{C}.db", C, [                 # 父對話不在：自己一列
        ag_system(30, "orphan AGORPHANTASK", event="agent_message",
                  src=("dddd4444-0000-4000-8000-00000000000d", "call_z", "invoke_subagent")),
        ag_planner(31, text="AGORPHANRESP", usage=(10, 5, 0)),
    ], parent="dddd4444-0000-4000-8000-00000000000d", agent=("research", "Lost"))
    # 標題（CLI 自動產生的）在上一層的 conversation_summaries.db
    scon = sqlite3.connect(str(src.parent / "conversation_summaries.db"))
    scon.execute("pragma journal_mode=wal")
    scon.execute("create table conversation_summaries (conversation_id text primary key, title text,"
                 " parent_conversation_id text, agent_name text)")
    scon.execute("insert into conversation_summaries values (?, 'AG Auto Title', '', '')", (A,))
    scon.commit()
    scon.close()
    before, before_up = _ag_digest(src), _ag_digest(src.parent)
    out = tmp / "out"

    def build(*extra):
        r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex",
                            "--antigravity-source", f"demo={src}", "--out", str(out), *extra],
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
        return r

    r = build()
    # ③ 子代理不另立一列：A 與孤兒 C 兩列
    assert "共 2 個 session" in r.stdout, "子代理不另立一列：" + r.stdout
    pages = _session_pages(out)
    assert [p.relative_to(out / "sessions").parts[:2] for p in pages] == [("antigravity", "demo")] * 2, pages
    pa = next((p for p in pages if p.name.endswith("__aaaa1111.html")), None)
    assert pa is not None, f"輸出檔名的 id 段取對話 id 的前 8 碼：{[p.name for p in pages]}"
    html = pa.read_text(encoding="utf-8")
    md = pa.with_suffix(".md").read_text(encoding="utf-8")
    body = _page_body(html)
    # ① 各種步驟
    assert "AG Auto Title" in body, "標題取 conversation_summaries.db 的自動標題"
    assert "AGQ1" in body and "🤖 Antigravity" in body, "使用者發言與 AI 回合"
    assert "AGTHINK" in body and "💭 思考" in body, "思考區塊"
    assert "run_command · python -m unittest" in body and "AGRES1" in body, "工具呼叫（摘要帶指令）與它的結果配成一組"
    assert "沒有執行：被取消，或權限沒有核准" in body, "CANCELED 的工具步驟"
    assert "AGERRSHORT" in body, "ERROR 的工具步驟帶 Step.error 的內容"
    assert body.count("（無結果 / 已中斷）") == 1, "紀錄停在 RUNNING 的那一次呼叫沒有結果"
    assert "背景任務 · Run tests finished" in body and "AGNOTE" in body, "背景任務完成畫成摺疊列"
    assert "⚠ 錯誤 · AGERRHEAD" in body and "AGERRFULL" in body, "錯誤：標題一行、完整內容收在摺疊裡"
    assert "⚠ 認不得的步驟" in body and "型別 999" in body, "認不得的步驟要在頁面上標出來"
    assert "⚠ 拆不開的步驟" in body, "拆不開的步驟要在頁面上標出來"
    assert body.index("⚠ 拆不開的步驟") > body.index("AGQ1"), "拆不開的步驟留在原來的位置（頁面）"
    assert "🖼️ 圖片" in body, "使用者輸入附的圖片畫佔位"
    assert 'class="compact-sep"' in html and "AGSUMMARY" in body and "手動 /compact" in body, "壓縮點"
    # ③ 子代理接在 invoke_subagent 底下（不是頁尾的孤兒區）
    assert f'id="sub-{A}-call_sub-' in html and "子代理 research · Code Summarizer" in body,         "子代理接在 invoke_subagent 底下"
    assert "AGCHILDTASK" in body and "AGCHILDRESP" in body, "子代理的內容在父對話那一頁"
    assert "sub-orphans" not in body, "子代理接在 invoke_subagent 底下（不是頁尾的孤兒區）"
    # ② 計量：Claude 單價（sonnet $3／$15）＋快取讀取 0.1×；未知模型 ⇒ +?
    #   (100000×3 + 200000×0.3 + 10000×15 + 1000×3 + 100×15 + 500×3 + 50×15) / 1e6 = 0.51675
    assert "💲~$0.52+?" in body, f"估價：{re.findall(r'💲~[^ <]*', body)}"
    assert "（不含快取寫入）" in body and "估價說明：" in md, "估價說明"
    assert "快取寫入 5m" not in md, "沒有快取寫入量就不列那幾格（不是 0，是不知道）"
    assert "sonnet-4-6" in md and "antigravity-model-4242" in md, "模型名取 gen_metadata 的模型 id"
    _rows = [e["row"] for e in json.loads((out / ".build-manifest.json").read_text(encoding="utf-8"))
             ["entries"].values()]
    assert _rows and all(not r.get("cache_steps") and not r.get("waste_n") for r in _rows),         "Antigravity 不進 Claude 的快取成因分析（沒有快取寫入量與 TTL）"
    assert not _page_dup_ids(html), _page_dup_ids(html)
    # ④ 出聲
    for needle in ("認不得的步驟型別 999", "步驟拆不開", "用量裡有沒見過的欄位 [4]", "使用者輸入附的圖片"):
        assert needle in r.stderr, f"要出聲：{needle}｜{r.stderr}"
    assert "⚠ 用量裡有沒見過的欄位" in body and "欄位 [4]" in body, "用量裡沒見過的欄位：頁面上也要標"
    # 孤兒子代理：自己一列，第一步（父代理傳來的任務）當成它的第一則發言、也當標題
    pc = next(p for p in pages if p.name.endswith("__cccc3333.html"))
    assert "orphan AGORPHANTASK" in _page_body(pc.read_text(encoding="utf-8")), "孤兒子代理自己一頁"
    idx_md = (out / "index.md").read_text(encoding="utf-8")
    assert "orphan AGORPHANTASK" in idx_md and "## Antigravity / AgProj" in idx_md, "孤兒子代理的標題：" + idx_md
    # ⑤ 來源夾一個檔都沒多、也沒有一個檔被改
    assert _ag_digest(src) == before, f"來源夾的檔多了或變了：{sorted(set(_ag_digest(src).items()) ^ set(before.items()))}"
    assert _ag_digest(src.parent) == before_up, "conversation_summaries.db 那一層的檔多了或變了"

    # ⑥ 增量建置：沒變就沿用；子代理的檔變了，父對話那一頁重建
    r = build()
    assert "沿用 2" in r.stdout, "沒變就沿用：" + r.stdout
    time.sleep(0.05)
    con = sqlite3.connect(str(src / f"{B}.db"))
    con.execute("insert into steps (idx, step_type, status, has_subtrajectory, step_format, step_payload)"
                " values (2, 15, 3, 0, 0, ?)", (ag_planner(22, text="AGCHILDMORE")[2],))
    con.commit()
    con.close()
    before = _ag_digest(src)
    r = build()
    assert "新建/更新 1" in r.stdout and "沿用 1" in r.stdout, "子代理的檔變了，父對話那一頁要重建：" + r.stdout
    assert "AGCHILDMORE" in pa.read_text(encoding="utf-8"), "子代理的檔變了，父對話那一頁要重建"
    # --search 搜得到 Antigravity 的內容；--no-antigravity 時不搜它
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out), "--search", "AGRES1"],
                       capture_output=True, text=True, encoding="utf-8")
    assert "命中 1 則" in r.stdout, "--search 搜得到 Antigravity：" + r.stdout
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out), "--search", "AGRES1", "--no-antigravity"],
                       capture_output=True, text=True, encoding="utf-8")
    assert "命中 0 則" in r.stdout, "--no-antigravity 不搜它：" + r.stdout
    # --ai-md：指定 id 的開頭（精簡版本來就不放子代理內部的來回）
    r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex", "--antigravity-source",
                        f"demo={src}", "--out", str(out), "--ai-md", "aaaa1111"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    ai = Path(r.stdout.strip()).read_text(encoding="utf-8")
    assert "AGRESP1" in ai and "來源：Antigravity" in ai and "Antigravity 自己接續" in ai, ai[:2000]
    assert "AGFILEBODY" not in ai and "view_file · /home/demo/AgProj/a.py — 內容略（3 行）" in ai,         "精簡版：讀檔內容略（Antigravity 的 view_file）"
    assert "AGWRITTEN1" not in ai and "write_to_file · /home/demo/AgProj/b.py — 寫入 2 行，內容略" in ai,         "精簡版：寫檔內容略（Antigravity 的 write_to_file）"
    assert "- AGOLDLINE" in ai and "+ AGNEWLINE" in ai, "精簡版：改檔畫 diff（Antigravity 的 replace_file_content）"
    assert "run_command · python -m unittest" in ai, "精簡版：指令那一行帶著指令"
    # 拆不開的步驟留在原來的位置（沿用上一步的時刻），不會跑到整場最前面
    # ⚠ 只比正文：檔頭的目錄就有 AGQ1，拿整份檔比的話不管順序都過
    ai_body = ai[ai.index("\n---\n"):]
    assert ai_body.index("拆不開的步驟") > ai_body.index("AGQ1"), "拆不開的步驟留在原來的位置：" + ai_body[:1500]
    # --project 篩 Antigravity：工作目錄的名字
    r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex", "--antigravity-source",
                        f"demo={src}", "--out", str(tmp / "out-p"), "--project", "nomatchzz"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "共 0 個 session" in r.stdout, "--project 篩 Antigravity：" + r.stdout
    assert _ag_digest(src) == before, f"來源夾的檔多了或變了：{sorted(set(_ag_digest(src).items()) ^ set(before.items()))}"

    # ⑤ WAL：還沒寫回主檔的內容（`-wal`）讀得到，而且不在來源夾留下任何檔
    for with_shm in (True, False):
        wsrc = tmp / f"wal-{int(with_shm)}"
        wsrc.mkdir()
        D = f"eeee555{int(with_shm)}-0000-4000-8000-00000000000e"
        live = tmp / f"live-{int(with_shm)}"
        live.mkdir()
        wcon = ag_write_db(live / f"{D}.db", D, [ag_user(40, "AGWALQ"), ag_planner(41, text="AGWALRESP")],
                           wal_conn=True)
        try:
            assert (live / f"{D}.db-wal").stat().st_size > 0, "素材：連線開著時 -wal 要有內容"
            for suf in ("", "-wal") + (("-shm",) if with_shm else ()):
                shutil.copyfile(live / f"{D}.db{suf}", wsrc / f"{D}.db{suf}")
        finally:
            wcon.close()
        wbefore = _ag_digest(wsrc)
        wout = tmp / f"wout-{int(with_shm)}"
        r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex", "--antigravity-source",
                            f"w={wsrc}", "--out", str(wout)], capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        wpages = _session_pages(wout)
        assert len(wpages) == 1 and "AGWALRESP" in wpages[0].read_text(encoding="utf-8"),             f"還沒寫回主檔的內容要讀得到（-shm {'在' if with_shm else '不在'}）：{r.stdout}{r.stderr}"
        assert _ag_digest(wsrc) == wbefore, (
            f"來源夾的檔多了或變了（-shm {'在' if with_shm else '不在'}）："
            f"{sorted(set(_ag_digest(wsrc).items()) ^ set(wbefore.items()))}")
    print("OK: antigravity test passed")


def test_antigravity_tree_cycle():
    """父子關係成環（資料壞掉）：每一場都還在，不無限遞迴；自己是自己的父對話也一樣。"""
    sys.path.insert(0, str(ROOT))
    import ai_session_viewer as v
    tmp = new_tmp()
    X, Y, Z = ("1111aaaa-0000-4000-8000-000000000001", "2222bbbb-0000-4000-8000-000000000002",
               "3333cccc-0000-4000-8000-000000000003")
    ag_write_db(tmp / f"{X}.db", X, [ag_user(0, "x")], parent=Y, agent=("a", "b"))
    ag_write_db(tmp / f"{Y}.db", Y, [ag_user(1, "y")], parent=X, agent=("a", "b"))
    ag_write_db(tmp / f"{Z}.db", Z, [ag_user(2, "z")], parent=Z, agent=("a", "b"))
    roots, tree = v.antigravity_tree(sorted(tmp.glob("*.db")))
    ids = sorted(i["id"] for i in roots)
    shown = set(ids) | {d["id"] for r in roots for d in tree[str(r["path"])]}
    assert shown == {X, Y, Z}, f"父子成環：每一場都要還在（{ids}／{shown}）"
    print("OK: antigravity tree cycle test passed")


def _ag_build(src, out, *extra):
    """只讀 `src` 這一個 Antigravity 來源建一次（`--antigravity-source` 算縮範圍建置：沿用舊紀錄）。"""
    r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex",
                        "--antigravity-source", f"demo={src}", "--out", str(out), *extra],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    return r


def _manifest_rows(out):
    return [e["row"] for e in json.loads((Path(out) / ".build-manifest.json").read_text(encoding="utf-8"))
            ["entries"].values()]


def test_antigravity_tool_pairing(tmp_path=None):
    """工具呼叫配它自己的結果，不配別次呼叫的：

    ① 同一個呼叫 id 在模型回應裡出現兩次：兩個結果各配各的（依出現順序），頁面與 stderr 都講「呼叫 id 重複」；
    ② 沒有呼叫 id：照工具名與參數配到模型回應裡同樣沒有 id 的那一次（那個呼叫只畫一次，不另補一個）；
       配不到的才用工具步驟自己帶的呼叫補一個；頁面上標出來；
    ③ 父對話與子代理用了同一個呼叫 id：併進同一頁之後各配各的，兩邊的結果都在；
    ④ 原始 id 本身長得像另編出來的內部 id：各配各的；
    ⑤ 拆到一半的步驟（模型回應或工具結果）不動配對表：後面的結果不會配給一個沒有畫出來的呼叫、跟著消失。"""
    tmp = new_tmp(tmp_path)
    src = tmp / "conversations"
    src.mkdir()
    P, K = "1234abcd-0000-4000-8000-0000000000a1", "5678abcd-0000-4000-8000-0000000000b2"
    ag_write_db(src / f"{P}.db", P, [
        ag_user(0, "AGPAIRQ"),
        ag_planner(1, calls=[("dup", "run_command", {"CommandLine": "echo ONE"}),
                             ("dup", "run_command", {"CommandLine": "echo TWO"}),
                             ("", "view_file", {"AbsolutePath": "/home/demo/NOID"}),
                             ("same", "invoke_subagent", {"TypeName": "research", "Role": "r"})],
                   usage=(10, 5, 0)),
        ag_generic(2, "dup", "run_command", {"CommandLine": "echo ONE"}, result="AGOUT1"),
        ag_generic(3, "dup", "run_command", {"CommandLine": "echo TWO"}, result="AGOUT2"),
        ag_generic(4, "", "view_file", {"AbsolutePath": "/home/demo/NOID"}, result="AGNOIDRES"),
        ag_generic(5, "", "view_file", {"AbsolutePath": "/home/demo/LONE"}, result="AGLONERES"),
        ag_generic(9, "same", "invoke_subagent", {"TypeName": "research"}, result="AGPARENTRES"),
        ag_planner(10, calls=[("x", "run_command", {"CommandLine": "echo XA"}),
                              ("x", "run_command", {"CommandLine": "echo XB"}),
                              ("x#2", "run_command", {"CommandLine": "echo XC"})], usage=(10, 5, 0)),
        ag_generic(11, "x", "run_command", {"CommandLine": "echo XA"}, result="AGXRES1"),
        ag_generic(12, "x", "run_command", {"CommandLine": "echo XB"}, result="AGXRES2"),
        ag_generic(13, "x#2", "run_command", {"CommandLine": "echo XC"}, result="AGXRES3"),
    ])
    ag_write_db(src / f"{K}.db", K, [
        ag_system(6, "child task AGKIDTASK", event="agent_message", src=(P, "same", "invoke_subagent")),
        ag_planner(7, calls=[("same", "run_command", {"CommandLine": "echo KID"})], usage=(10, 5, 0)),
        ag_generic(8, "same", "run_command", {"CommandLine": "echo KID"}, result="AGKIDRES"),
    ], parent=P, agent=("research", "Kid"))
    out = tmp / "out"
    r = _ag_build(src, out)
    pages = _session_pages(out)
    assert len(pages) == 1, [p.name for p in pages]
    body = _page_body(pages[0].read_text(encoding="utf-8"))
    # --- ① 兩個結果都在、各在自己的呼叫底下（第一個呼叫的框裡是 AGOUT1，不是被第二個蓋掉）
    assert body.count("AGOUT1") == 1 and body.count("AGOUT2") == 1, "同一個 id 的兩個結果都要在、各一次"
    assert (body.index("echo ONE") < body.index("AGOUT1") < body.index("echo TWO")
            < body.index("AGOUT2")), "結果依出現順序各配各的呼叫"
    assert "⚠ 工具呼叫 id 重複" in body and "id 重複" in r.stderr, "呼叫 id 重複：頁面與 stderr 都要講"
    # --- ② 沒有 id：配到同樣沒有 id 的那一次（那個呼叫只畫一次），配不到的補一個
    assert body.count("view_file · /home/demo/NOID") == 1 and "AGNOIDRES" in body, \
        "沒有 id 的照工具名與參數配，不另補一個呼叫"
    assert "view_file · /home/demo/LONE" in body and "AGLONERES" in body, "配不到的用工具步驟自己帶的呼叫補一個"
    assert "⚠ 工具步驟沒有呼叫 id" in body and "⚠ 工具呼叫沒有 id" in body, "沒有呼叫 id：頁面上要標"
    # --- ③ 父對話與子代理同一個 id：兩個結果都在
    assert "AGPARENTRES" in body and "AGKIDRES" in body, "父對話與子代理同一個呼叫 id：兩邊的結果都要在"
    assert "AGKIDTASK" in body and "sub-orphans" not in body, "子代理照樣接在觸發它的那一次呼叫底下"
    assert "（無結果 / 已中斷）" not in body, "每一個呼叫都配到了結果"
    # --- ④ 原始 id 本身長得像另編出來的內部 id（`x`、`x`、`x#2`）：三個結果各在自己的呼叫底下，一個都不少
    assert [body.count(f"AGXRES{i}") for i in (1, 2, 3)] == [1, 1, 1], \
        f"另編的內部 id 不可以撞到原始 id：{[body.count(f'AGXRES{i}') for i in (1, 2, 3)]}"
    assert (body.index("echo XA") < body.index("AGXRES1") < body.index("echo XB") < body.index("AGXRES2")
            < body.index("echo XC") < body.index("AGXRES3")), "每個結果在自己的呼叫底下"
    # --- ⑤ 拆到一半的步驟不動配對表（自己一場：和上面那幾段的頁面分開）：模型回應那一支——結果用它自己帶的呼叫補一個
    half = tmp / "half"
    half.mkdir()
    H = "2468abcd-0000-4000-8000-0000000000c3"
    ag_write_db(half / f"{H}.db", H, [
        ag_user(0, "AGHALFQ"),
        # 模型回應拆到一半（用量的欄位 wire type 不對）：它的呼叫沒有畫出來，也不可以留在配對表裡
        (15, 3, pbv(1, 15) + pbv(4, 3) + ag_meta(1, pbm(9, pbv(1, 1035), _pb_key(2, 5) + b"ABCD"))
         + pbm(20, pbm(7, pbs(1, "zz"), pbs(2, "run_command"), pbs(3, json.dumps({"CommandLine": "echo ZZ"}))))),
        ag_generic(2, "zz", "run_command", {"CommandLine": "echo ZZ"}, result="AGZZRES"),
        # 工具結果拆到一半（結果文字的 wire type 不對）：它不可以先把呼叫配掉，下一個同 id 的結果才配得回去
        ag_planner(3, calls=[("yy", "run_command", {"CommandLine": "echo YY"})], usage=(10, 5, 0)),
        (132, 3, pbv(1, 132) + pbv(4, 3)
         + ag_meta(4, pbm(4, pbs(1, "yy"), pbs(2, "run_command"), pbs(3, json.dumps({"CommandLine": "echo YY"}))))
         + pbm(140, pbm(2, _pb_key(1, 5) + b"WXYZ"))),
        ag_generic(5, "yy", "run_command", {"CommandLine": "echo YY"}, result="AGYYRES"),
    ])
    _ag_build(half, tmp / "out-half")
    body = _page_body(_session_pages(tmp / "out-half")[0].read_text(encoding="utf-8"))
    assert body.count("⚠ 拆不開的步驟") == 2, "拆到一半的兩步照樣標「拆不開的步驟」"
    zz, yy = "run_command · echo ZZ", "run_command · echo YY"
    assert body.count("AGZZRES") == 1 and body.count(zz) == 1, \
        f"拆到一半的模型回應不可以留下配對：結果要在、呼叫補一個（{body.count('AGZZRES')}, {body.count(zz)}）"
    # --- ⑤b 工具結果那一支——拆不開的那一步沒有把呼叫配掉，下一個結果配回原本的呼叫（不另補一個）
    assert body.count("AGYYRES") == 1 and body.count(yy) == 1, \
        f"拆到一半的工具結果不可以先配掉呼叫：{body.count('AGYYRES')}, {body.count(yy)}"
    assert body.index("echo YY") < body.index("AGYYRES")
    assert "（無結果 / 已中斷）" not in body, "原本的呼叫配到了結果"
    print("OK: antigravity tool pairing test passed")


def test_antigravity_identity(tmp_path=None):
    """一場 Antigravity 對話的身分：

    ① 同一個對話 id 兩個檔（同步工具的衝突副本、手動備份）：各自一列、各自一頁（不互相蓋掉），
       檔名不是 `<對話 id>.db` 的那份在頁首與索引標「副本 <檔名>」；
    ② `--ai-md` 用檔裡的對話 id 認人：檔名改過也找得到（給 id 的開頭）；
    ③ 孤兒子代理（父對話不在）自己一列；父對話出現之後併進父對話那一頁，舊的那一列要消失——
       帶 `--antigravity-source` 的建置算縮範圍、沿用舊紀錄，舊的那一列最容易留下來；
    ④ `conversation_summaries.db` 給的子代理型別變了（子代理的檔沒變）：父對話那一頁要重建；
    ⑤ 父對話的檔讀不起來：讀得起來的子代理不跟著它消失（自己一列）；父對話讀到一半才壞的，stderr 講出連帶的子代理；
    ⑥ 兩個呼叫 id 清洗之後一樣：子代理的頁內錨點不撞號；
    ⑦ 孤兒子代理（對不到觸發它的那一次呼叫）自己又派了子代理：孫子代理只畫一次（接在孤兒的那一次呼叫底下），
       錨點不重複，頁頂目錄的連結指到它實際所在的框。"""
    tmp = new_tmp(tmp_path)
    # --- ① 同一個對話 id 兩個檔（父對話與子代理各有一份副本；副本的檔名排在本人前面）
    src = tmp / "dup"
    src.mkdir()
    P, KD = "9999aaaa-0000-4000-8000-0000000000c1", "9998aaaa-0000-4000-8000-0000000000c2"
    steps = [ag_user(0, "AGDUPQ"),
             ag_planner(1, text="AGDUPRESP", calls=[("cd", "invoke_subagent", {"TypeName": "research"})],
                        usage=(10, 5, 0)),
             ag_generic(4, "cd", "invoke_subagent", {"TypeName": "research"}, result="ok")]
    ag_write_db(src / f"{P}.db", P, steps)
    ag_write_db(src / f"{P} (1).db", P, steps + [ag_user(5, "AGFORKQ")])
    kid = [ag_system(2, "kid AGDUPKID", event="agent_message", src=(P, "cd", "invoke_subagent")),
           ag_planner(3, text="AGDUPKIDRESP", usage=(1, 1, 0))]
    ag_write_db(src / f"{KD}.db", KD, kid, parent=P, agent=("research", "Kid"))
    # 子代理的副本自己接著跑過一步（分岔）：和本人一模一樣的那幾步在同一頁會被去重，只有分岔的那一步看得出它有沒有被併進來
    ag_write_db(src / f"{KD} (1).db", KD, kid + [ag_planner(3, text="AGKIDFORK", usage=(1, 1, 0))],
                parent=P, agent=("research", "Kid"))
    out = tmp / "out-dup"
    r = _ag_build(src, out)
    assert "共 3 個 session" in r.stdout and "輸出檔名相同" not in r.stderr, \
        "本人（併著子代理）、本人的副本、子代理的副本各一列：" + r.stdout + r.stderr
    pages = _session_pages(out)
    assert len(pages) == 3, f"同一個對話 id 的兩個檔：各自一頁、不互相蓋掉：{[p.name for p in pages]}"
    htmls = [p.read_text(encoding="utf-8") for p in pages]
    bodies = [_page_body(h) for h in htmls]
    assert sum("AGFORKQ" in b for b in bodies) == 1 and sum("AGDUPRESP" in b for b in bodies) == 2, "兩頁各畫各的檔"
    assert sum(f"副本 {P} (1).db" in b for b in bodies) == 1, "檔名不是對話 id 的那份標副本（頁首）"
    assert all("同一場 Antigravity 對話的另一個檔" in h for h, b in zip(htmls, bodies) if "副本 " in b), \
        "副本標記的說明講的是 Antigravity 的副本（不是 Claude 的跨機同步工具）"
    orig = next(b for b in bodies if "AGDUPRESP" in b and "副本 " not in b)
    assert orig.count("AGDUPKIDRESP") == 1, "子代理接到本人（不是排序在前的副本），而且只接一份"
    assert "AGKIDFORK" not in orig, "子代理的副本不併進父對話（它自己一列）"
    assert sum("AGKIDFORK" in b for b in bodies) == 1, "子代理的副本自己那一頁畫它自己的內容"
    assert sum(f"副本 {KD} (1).db" in b for b in bodies) == 1, "子代理的副本自己一列"
    assert f"副本 {P} (1).db" in (out / "index.html").read_text(encoding="utf-8"), "索引也標副本"
    # --- ② --ai-md：改過名的檔
    ren = tmp / "ren"
    ren.mkdir()
    R = "abcd1234-0000-4000-8000-0000000000d1"
    ag_write_db(ren / "renamed.db", R, [ag_user(0, "AGRENQ"), ag_planner(1, text="AGRENRESP", usage=(1, 1, 0))])
    r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex", "--antigravity-source",
                        f"demo={ren}", "--out", str(tmp / "out-ren"), "--ai-md", "abcd1234"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"--ai-md 用檔裡的對話 id 認人（檔名改過）：{r.stderr}"
    assert "AGRENRESP" in Path(r.stdout.strip()).read_text(encoding="utf-8")
    # --- ③ 孤兒子代理 → 父對話出現之後併進去
    orph = tmp / "orph"
    orph.mkdir()
    PA, KI = "7777aaaa-0000-4000-8000-0000000000e1", "8888bbbb-0000-4000-8000-0000000000e2"
    ag_write_db(orph / f"{KI}.db", KI, [
        ag_system(2, "kid task AGLATEKID", event="agent_message", src=(PA, "c1", "invoke_subagent")),
        ag_planner(3, text="AGLATEKIDRESP", usage=(100, 10, 0))], parent=PA, agent=("research", "Late"))
    oout = tmp / "out-orph"
    r = _ag_build(orph, oout)
    assert "共 1 個 session" in r.stdout and len(_manifest_rows(oout)) == 1, r.stdout
    ag_write_db(orph / f"{PA}.db", PA, [
        ag_user(0, "AGLATEPARENTQ"),
        ag_planner(1, calls=[("c1", "invoke_subagent", {"TypeName": "research"})], usage=(10, 5, 0)),
        ag_generic(4, "c1", "invoke_subagent", {"TypeName": "research"}, result="ok")])
    r = _ag_build(orph, oout)
    rows = _manifest_rows(oout)
    assert len(rows) == 1, f"子代理併進父對話之後，它自己那一列要消失：{[x.get('title') for x in rows]}"
    assert sum(x.get("tok_out", 0) for x in rows) == 15, \
        f"產出只算一次（父 5＋子 10）：{[x.get('tok_out') for x in rows]}"
    idx_md = (oout / "index.md").read_text(encoding="utf-8")
    assert "AGLATEPARENTQ" in idx_md and "AGLATEKID" not in idx_md, idx_md
    # --- ④ conversation_summaries.db 給的子代理型別
    sm = tmp / "sm" / "conversations"
    sm.mkdir(parents=True)
    SP, SK = "3333dddd-0000-4000-8000-0000000000f1", "4444eeee-0000-4000-8000-0000000000f2"
    ag_write_db(sm / f"{SP}.db", SP, [
        ag_user(0, "AGSMQ"),
        ag_planner(1, calls=[("cs", "invoke_subagent", {"TypeName": "x"})], usage=(1, 1, 0)),
        ag_generic(4, "cs", "invoke_subagent", {"TypeName": "x"}, result="ok")])
    ag_write_db(sm / f"{SK}.db", SK, [
        ag_system(2, "t AGSMKID", event="agent_message", src=(SP, "cs", "invoke_subagent")),
        ag_planner(3, text="AGSMKIDRESP", usage=(1, 1, 0))], parent=SP, agent=("", ""))
    scon = sqlite3.connect(str(sm.parent / "conversation_summaries.db"))
    scon.execute("create table conversation_summaries (conversation_id text primary key, title text,"
                 " parent_conversation_id text, agent_name text)")
    scon.execute("insert into conversation_summaries values (?, '', ?, 'AGENT_ONE')", (SK, SP))
    scon.commit()
    scon.close()
    sout = tmp / "out-sm"
    _ag_build(sm, sout)
    page = _session_pages(sout)[0]
    assert "子代理 AGENT_ONE" in _page_body(page.read_text(encoding="utf-8")), "子代理型別取 conversation_summaries.db"
    scon = sqlite3.connect(str(sm.parent / "conversation_summaries.db"))
    scon.execute("update conversation_summaries set agent_name = 'AGENT_TWO'")
    scon.commit()
    scon.close()
    r = _ag_build(sm, sout)
    assert "子代理 AGENT_TWO" in _page_body(page.read_text(encoding="utf-8")), \
        "conversation_summaries.db 給的子代理型別變了，父對話那一頁要重建：" + r.stdout
    # --- ⑤ 父對話的檔讀不起來：子代理不接到它（不然跟著一起讀不起來），自己一列
    bp = tmp / "badparent"
    bp.mkdir()
    BPA, BKI = "1212aaaa-0000-4000-8000-000000000301", "3434bbbb-0000-4000-8000-000000000302"
    (bp / f"{BPA}.db").write_bytes(b"not a database at all " * 64)
    ag_write_db(bp / f"{BKI}.db", BKI, [
        ag_system(2, "kid task AGBPKID", event="agent_message", src=(BPA, "c1", "invoke_subagent")),
        ag_planner(3, text="AGBPKIDRESP", usage=(1, 1, 0))], parent=BPA, agent=("research", "Kid"))
    r = _ag_build(bp, tmp / "out-bp")
    pages = _session_pages(tmp / "out-bp")
    assert len(pages) == 1 and "AGBPKIDRESP" in _page_body(pages[0].read_text(encoding="utf-8")), \
        f"父對話讀不起來，子代理照樣自己一頁：{r.stdout}{r.stderr}"
    assert "壞掉跳過 1" in r.stdout and f"{BPA}.db" in r.stderr, "讀不起來的父對話照樣講出來"
    # --- ⑤b 父對話的身分讀得起來、步驟表才壞：子代理跟著它算壞掉（範圍限制），stderr 那一行要講出來
    lp = tmp / "lateparent"
    lp.mkdir()
    LPA, LKI = "5656aaaa-0000-4000-8000-000000000303", "7878bbbb-0000-4000-8000-000000000304"
    ag_write_db(lp / f"{LPA}.db", LPA, [
        ag_user(0, "AGLPQ"), ag_planner(1, calls=[("c1", "invoke_subagent", {"TypeName": "x"})], usage=(1, 1, 0))])
    con = sqlite3.connect(str(lp / f"{LPA}.db"))
    con.execute("alter table steps drop column status")
    con.commit()
    con.close()
    ag_write_db(lp / f"{LKI}.db", LKI, [
        ag_system(2, "kid task AGLPKID", event="agent_message", src=(LPA, "c1", "invoke_subagent")),
        ag_planner(3, text="AGLPKIDRESP", usage=(1, 1, 0))], parent=LPA, agent=("research", "Kid"))
    r = _ag_build(lp, tmp / "out-lp")
    assert "1 個子代理也沒有畫出來" in r.stderr, f"父對話讀到一半壞掉：stderr 要講出連帶的子代理：{r.stderr}"
    # --- ⑥ 兩個呼叫 id 清洗之後一樣（`x.y`、`x-y`）：兩個子代理的頁內錨點不撞號，目錄的連結各指到自己那一個
    an = tmp / "anchors"
    an.mkdir()
    AP, AK1, AK2 = ("9090aaaa-0000-4000-8000-000000000305", "9191bbbb-0000-4000-8000-000000000306",
                    "9292cccc-0000-4000-8000-000000000307")
    ag_write_db(an / f"{AP}.db", AP, [
        ag_user(0, "AGANQ"),
        ag_planner(1, calls=[("x.y", "invoke_subagent", {"TypeName": "a"}),
                             ("x-y", "invoke_subagent", {"TypeName": "b"})], usage=(1, 1, 0)),
        ag_generic(6, "x.y", "invoke_subagent", {"TypeName": "a"}, result="ok"),
        ag_generic(7, "x-y", "invoke_subagent", {"TypeName": "b"}, result="ok")])
    for kid_id, call, word, t0 in ((AK1, "x.y", "AGANKID1", 2), (AK2, "x-y", "AGANKID2", 4)):
        ag_write_db(an / f"{kid_id}.db", kid_id, [
            ag_system(t0, f"task {word}", event="agent_message", src=(AP, call, "invoke_subagent")),
            ag_planner(t0 + 1, text=word + "RESP", usage=(1, 1, 0))], parent=AP, agent=("research", word))
    _ag_build(an, tmp / "out-an")
    html = _session_pages(tmp / "out-an")[0].read_text(encoding="utf-8")
    sub_ids = re.findall(r'<details id="(sub-[^"]+)"', html)
    assert len(sub_ids) == 2 and len(set(sub_ids)) == 2, f"兩個子代理的錨點不撞號：{sub_ids}"
    links = re.findall(r"openSub\('(sub-[^']+)'\)", html)
    assert sorted(links) == sorted(sub_ids), f"目錄的連結各指到自己那一個子代理：{links} vs {sub_ids}"
    for sid_, word in zip(sub_ids, ("AGANKID1", "AGANKID2")):
        block = html[html.index(f'<details id="{sid_}"'):]
        assert word + "RESP" in block[:block.index("</details>")], f"{sid_} 那一框是它自己的子代理"
    # --- ⑦ 孤兒子代理自己派了子代理
    oc = tmp / "orphchain"
    oc.mkdir()
    OP, OK_, OL = ("9393aaaa-0000-4000-8000-000000000308", "9494bbbb-0000-4000-8000-000000000309",
                   "9595cccc-0000-4000-8000-000000000310")
    ag_write_db(oc / f"{OP}.db", OP, [ag_user(0, "AGOCQ"), ag_planner(1, text="AGOCROOT", usage=(1, 1, 0))])
    ag_write_db(oc / f"{OK_}.db", OK_, [
        ag_system(2, "task AGOCKID", event="agent_message", src=(OP, "nosuchcall", "invoke_subagent")),
        ag_planner(3, calls=[("leafcall", "invoke_subagent", {"TypeName": "leaf"})], usage=(1, 1, 0))],
        parent=OP, agent=("research", "Kid"))
    ag_write_db(oc / f"{OL}.db", OL, [
        ag_system(4, "task AGOCLEAF", event="agent_message", src=(OK_, "leafcall", "invoke_subagent")),
        ag_planner(5, text="AGOCLEAFRESP", usage=(1, 7, 0))], parent=OK_, agent=("research", "Leaf"))
    _ag_build(oc, tmp / "out-oc")
    html = _session_pages(tmp / "out-oc")[0].read_text(encoding="utf-8")
    assert _page_body(html).count("AGOCLEAFRESP") == 1, "孫子代理只畫一次"
    assert not _page_dup_ids(html), f"錨點不重複：{_page_dup_ids(html)}"
    links = re.findall(r"openSub\('(sub-[^']+)'\)", html)
    leaf_box = [x for x in re.findall(r'<details id="(sub-[^"]+)"', html) if x != "sub-orphans"]
    assert len(leaf_box) == 1 and sorted(links) == sorted(["sub-orphans", leaf_box[0]]), \
        f"目錄：孤兒指到頁尾總區，孫子代理指到它自己的框：{links} vs {leaf_box}"
    assert "AGOCLEAFRESP" in html[html.index(f'<details id="{leaf_box[0]}"'):], "那一框是孫子代理"
    print("OK: antigravity identity test passed")


def test_antigravity_page_marks(tmp_path=None):
    """紀錄裡不對勁的地方頁面上看得到（不只 stderr）；使用者原文照原樣；不畫紀錄裡沒有的量。

    ① 認不得的步驟狀態、步驟型別欄與內容不一致、沒有時刻、取不出文字的使用者輸入、子代理讀不起來：頁面上各標一列；
    ② 工具結果附的圖片／檔案畫佔位（「🖼️ 圖片」），不是只在 stderr；
    ③ 使用者原文裡的 `<system-reminder>…</system-reminder>`、`<command-name>…</command-name>` 是他打的字：
       頁面、MD、精簡版都照原樣（不剝、不畫成指令列），也照原樣當標題；
    ④ 字串欄位放的是 fixed32（wire type 對不上）：算拆不開，不當成正常的發言；
    ⑤ 回合與逐步的 token 細分不畫「寫入」（紀錄裡沒有快取寫入量：那一格不是 0，是不知道）；
    ⑥ 完整版 MD 工具框的 `<summary>`：原文（檔案路徑）破不出那個元素；
    ⑦ 沒有文字、思考或工具呼叫的模型回應：標出來、用量照算；
    ⑧ 模型回應的狀態不是完成（還沒跑完、被取消、出錯、被打斷）：那一則的最後標出來，不畫得和完成的一樣；
       別種步驟（使用者輸入、系統訊息、錯誤、壓縮點）的狀態不是完成：那一步之後標一列、也出聲；
    ⑨ 工具結果還沒跑完、但記下了文字或附件：照畫；它與 CLEARED 的結果都在那一步之後標「狀態不是完成」；
       stderr 說畫了幾個附件佔位，頁面上就有幾個。"""
    tmp = new_tmp(tmp_path)
    src = tmp / "conversations"
    src.mkdir()
    M, BAD = "5555aaaa-0000-4000-8000-000000000101", "6666bbbb-0000-4000-8000-000000000102"
    lit = "<system-reminder>AGLITERAL</system-reminder> 與 <command-name>/AGFOO</command-name>"
    hostile = "</summary><img src=x onerror=alert(1)><summary>"
    ag_write_db(src / f"{M}.db", M, [
        ag_user(0, lit),
        ag_planner(1, text="AGRESP0", calls=[("cimg", "view_file", {"AbsolutePath": "/home/demo/pic.png"}),
                                             ("chost", "view_file", {"AbsolutePath": hostile}),
                                             ("cnl", "view_file", {"AbsolutePath": "/x" + chr(10) * 2 + "## AGBROKE"}),
                                             ("cbad", "invoke_subagent", {"TypeName": "research"})],
                   usage=(1000, 100, 2000)),
        (132, 3, pbv(1, 132) + pbv(4, 3)
         + ag_meta(2, pbm(4, pbs(1, "cimg"), pbs(2, "view_file"), pbs(3, json.dumps({"AbsolutePath": "/home/demo/pic.png"}))))
         + pbm(140, pbm(2, pbs(1, "AGIMGRES"), pbm(4, pbs(1, "png"))))),
        ag_generic(3, "chost", "view_file", {"AbsolutePath": hostile}, result="AGHOSTRES"),
        ag_generic(3, "cnl", "view_file", {"AbsolutePath": "/x" + chr(10) * 2 + "## AGBROKE"}, result="AGNLRES"),
        ag_generic(4, "cbad", "invoke_subagent", {"TypeName": "research"}, result="ok"),
        (15, 99, pbv(1, 15) + pbv(4, 99) + ag_meta(5) + pbm(20, pbs(1, "AGSTATUS99"))),     # 認不得的狀態
        (15, 3, ag_user(6, "AGMISMATCH")[2]),                                                 # 型別欄 15、內容是 14
        (15, 3, pbv(1, 15) + pbv(4, 3) + pbm(5) + pbm(20, pbs(1, "AGNOTIME"))),               # 沒有時刻
        (14, 3, pbv(1, 14) + pbv(4, 3) + ag_meta(8) + pbm(19, pbv(6, 1))),                    # 取不出文字
        (14, 3, pbv(1, 14) + pbv(4, 3) + ag_meta(9) + pbm(19, _pb_key(2, 5) + b"Q7Z9")),     # 字串欄位放 fixed32
        ag_planner(10, text="AGRESP1", usage=(10, 5, 0)),
        ag_user(11, "<system-reminder>AGONLYWRAP</system-reminder>"),                         # 整則只有那個標籤
        ag_planner(12, text="AGRESP2", usage=(10, 5, 0)),
        (15, 3, pbv(1, 15) + pbv(4, 3)                                                          # 沒有內容、有用量
         + ag_meta(13, pbm(9, pbv(1, 1035), pbv(2, 100), pbv(3, 7), pbv(5, 50))) + pbm(20)),
        ag_planner(14, text="AGAFTEREMPTY", usage=(10, 3, 0)),
        # ⑧ 模型回應的狀態不是完成：還沒跑完（RUNNING）、被取消、出錯、被打斷
        *[(15, st, pbv(1, 15) + pbv(4, st) + ag_meta(15 + i) + pbm(20, pbs(1, f"AGST{st}")))
          for i, st in enumerate((2, 6, 7, 12))],
        # ⑧ 別種步驟（這裡是使用者輸入）的狀態不是完成
        (14, 6, pbv(1, 14) + pbv(4, 6) + ag_meta(19) + pbm(19, pbs(2, "AGUSERCANCEL"))),
        # ⑨ 工具結果的狀態：還沒跑完（RUNNING）但記下了文字與附件、CLEARED、還沒跑完而什麼都沒記下
        ag_planner(20, calls=[("crun", "view_file", {"AbsolutePath": "/home/demo/run.png"}),
                              ("cclr", "run_command", {"CommandLine": "echo CLR"}),
                              ("cnone", "run_command", {"CommandLine": "echo AGNONE"})], usage=(10, 5, 0)),
        (132, 2, pbv(1, 132) + pbv(4, 2)
         + ag_meta(21, pbm(4, pbs(1, "crun"), pbs(2, "view_file"), pbs(3, json.dumps({"AbsolutePath": "/home/demo/run.png"}))))
         + pbm(140, pbm(2, pbs(1, "AGPARTIALRES"), pbm(4, pbs(1, "png"))))),
        ag_generic(22, "cclr", "run_command", {"CommandLine": "echo CLR"}, result="AGCLEAREDRES", status=5),
        ag_generic(23, "cnone", "run_command", {"CommandLine": "echo AGNONE"}, status=2),
    ])
    # 讀不起來的子代理：認得出父子（第一步與後設資料都在），步驟表少一欄
    ag_write_db(src / f"{BAD}.db", BAD, [
        ag_system(3, "bad kid", event="agent_message", src=(M, "cbad", "invoke_subagent"))],
        parent=M, agent=("research", "Broken"))
    con = sqlite3.connect(str(src / f"{BAD}.db"))
    con.execute("alter table steps drop column status")
    con.commit()
    con.close()
    out = tmp / "out"
    r = _ag_build(src, out)
    page = _session_pages(out)[0]
    html = page.read_text(encoding="utf-8")
    body, md = _page_body(html), page.with_suffix(".md").read_text(encoding="utf-8")
    # --- ①
    for label in ("⚠ 認不得的步驟狀態", "⚠ 步驟型別不一致", "⚠ 步驟沒有時刻", "⚠ 取不出文字的使用者輸入", "⚠ 子代理讀不起來"):
        assert label in body and label in md, f"頁面（與 MD）要標：{label}"
    for needle in ("認不得的步驟狀態 99", "型別不一致", "沒有時刻", "取不出文字", "子代理"):
        assert needle in r.stderr, f"要出聲：{needle}｜{r.stderr}"
    assert body.index("AGSTATUS99") < body.index("⚠ 認不得的步驟狀態"), "標在那一步的位置（畫完那一步之後）"
    # --- ②
    assert "🖼️ 圖片" in body and "工具結果附的圖片" in r.stderr, "工具結果附的圖片：頁面上畫佔位"
    # --- ③（⚠ 這一則也是標題：頁首、MD 與精簡版的檔頭都有這段字，斷言只看回合的內文）
    lit_html = "&lt;system-reminder&gt;AGLITERAL&lt;/system-reminder&gt; 與 &lt;command-name&gt;/AGFOO&lt;/command-name&gt;"
    assert f"<p>{lit_html}</p>" in body, "使用者原文照原樣（頁面）：不剝、不畫成指令列"
    assert f"\n\n{lit}\n" in md, "使用者原文照原樣（MD）"
    assert "<p>&lt;system-reminder&gt;AGONLYWRAP&lt;/system-reminder&gt;</p>" in body, \
        "整則只有那個標籤的發言也照原樣畫（不當雜訊丟掉）"
    rows = _manifest_rows(out)
    assert rows and rows[0]["title"].startswith("<system-reminder>AGLITERAL"), f"標題照原樣：{rows[0]['title']}"
    r2 = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex", "--antigravity-source",
                         f"demo={src}", "--out", str(out), "--ai-md", "5555aaaa"],
                        capture_output=True, text=True, encoding="utf-8")
    assert r2.returncode == 0, r2.stderr
    ai = Path(r2.stdout.strip()).read_text(encoding="utf-8")
    assert lit in ai[ai.index("\n---\n"):], "使用者原文照原樣（精簡版）"
    # --- ④
    assert "Q7Z9" not in body and "Q7Z9" not in md, "wire type 對不上的字串欄位不當成發言"
    assert "⚠ 拆不開的步驟" in body
    # --- ⑤
    assert 'class="meter m-cw"' not in html, "Antigravity 的頁面不畫「寫入」徽章（紀錄裡沒有這個量）"
    # --- ⑥
    # （工具框裡的輸入原文是在 fence 裡，照原樣是對的；要擋的是 `<summary>` 那一行）
    exp = "<details><summary>🔧 view_file · &lt;/summary>&lt;img src=x onerror=alert(1)>&lt;summary></summary>"
    assert exp in md.splitlines(), \
        f"MD 工具框的 <summary> 破不出去：{[ln for ln in md.splitlines() if ln.startswith('<details><summary>🔧')]}"
    assert "AGHOSTRES" in md
    assert "<details><summary>🔧 view_file · /x ## AGBROKE</summary>" in md.splitlines(), \
        "<summary> 裡的斷行壓成空格（空行會讓後面的字跑出那個元素）"
    # --- ⑦ 沒有文字、思考或工具呼叫的模型回應：頁面上那一步標出來；它的用量照算，整場的產出等於各回合產出的和
    assert "⚠ 沒有內容的模型回應" in body and "⚠ 沒有內容的模型回應" in md, "沒有內容的模型回應：頁面上要標"
    assert "模型回應沒有內容" in r.stderr, "沒有內容的模型回應：要出聲"
    sys.path.insert(0, str(ROOT))
    import ai_session_viewer as v
    sess = v.load_antigravity_session(src / f"{M}.db")
    v.analyze(sess)
    turn_out = sum(g["u"]["output"] for g in sess.main_groups if g.get("u"))
    assert sess.tok_out == turn_out, f"整場的產出（{sess.tok_out}）要等於各回合的和（{turn_out}）"
    # --- ⑧ 狀態不是完成的模型回應：那一則的最後標出來（HTML、MD、精簡版）；完成的不標
    mark = "⚠ 模型回應的狀態不是完成"
    assert body.count(mark) == 4 and md.count(mark) == 4, f"四則狀態不是完成的各標一次：{body.count(mark)}, {md.count(mark)}"
    for st, why in ((2, "還沒跑完"), (6, "被取消"), (7, "出錯"), (12, "被打斷")):
        at = body.index(f"AGST{st}")
        assert at < body.index(mark, at) < body.index(why, at), f"狀態 {st}：標在那一則之後、講出是哪一種"
    assert ai.count(mark) == 4, "精簡版也標"
    assert mark not in body[:body.index("AGST2")], "完成的模型回應不標"
    # --- ⑧b 別種步驟（使用者輸入）的狀態不是完成：那一步之後標一列、也出聲
    smark = "⚠ 步驟的狀態不是完成"
    assert body.count(smark) == 1 and smark in md and body.index("AGUSERCANCEL") < body.index(smark), \
        "別種步驟的狀態不是完成：那一步之後標出來"
    assert "步驟的狀態不是完成（被取消" in r.stderr, "別種步驟的狀態不是完成：要出聲"
    # --- ⑨ 工具結果的狀態：還沒跑完而記下了內容的照畫（文字與附件的佔位）、CLEARED 照畫，兩者都在那一步之後標出來
    #        （畫出來的不是錯誤框，看起來和完成的一樣）；什麼都沒記下的畫「無結果」、不另標
    tmark = "⚠ 工具結果的狀態不是完成"
    assert body.count(tmark) == 2 and md.count(tmark) == 2, f"兩個工具結果各標一次：{body.count(tmark)}, {md.count(tmark)}"
    for res in ("AGPARTIALRES", "AGCLEAREDRES"):
        assert res in body and res in md, f"記下的結果文字照畫：{res}"
        assert body.index(res) < body.index(tmark, body.index(res)), f"{res}：標在那一步之後"
    assert "CLEARED（被清除" in body and "工具結果的狀態不是完成（CLEARED" in r.stderr, "CLEARED：頁面與 stderr 都講"
    assert body.count("紀錄停在這一步、還沒跑完") == 2 and "工具結果的狀態不是完成（紀錄停在這一步" in r.stderr, \
        "還沒跑完的工具結果：頁面與 stderr 都講（另一次是 ⑧ 的模型回應）"
    assert body.count("🖼️ 圖片") == 2 and "2 次：工具結果附的圖片" in r.stderr, \
        f"附件：頁面畫了幾個佔位，stderr 就講幾個（{body.count('🖼️ 圖片')}｜{r.stderr}）"
    assert body.count("（無結果 / 已中斷）") == 1 and "echo AGNONE" in body, "還沒跑完、什麼都沒記下的：畫「無結果」"
    print("OK: antigravity page marks test passed")


def test_antigravity_tools_scoped_to_source(tmp_path=None):
    """Antigravity 的工具名表只對 Antigravity 的呼叫生效，其他來源照舊：

    ① Claude 的紀錄裡剛好有同名的工具（MCP 伺服器常見的 `read_file`、`run_command`、`write_file`）：
       摘要只畫工具名，精簡版照一般工具畫（輸入與輸出都留），不套 Antigravity 的參數名；
    ② Claude 事件帶著 `_special` 欄位：不畫成通知列（那是 Antigravity 載入器的欄位，不是 Claude 紀錄裡的東西）；
    ③ 共用的 `md_to_html`：程式碼區塊的語言字串照屬性跳脫，破不出 `class` 屬性。"""
    sys.path.insert(0, str(ROOT))
    import ai_session_viewer as v
    tmp = new_tmp(tmp_path)
    proj = tmp / "projects" / "scope-proj"
    proj.mkdir(parents=True)
    sid = "5c0e0000-0000-4000-8000-0000000005c0"

    def q(t, uuid_, content, **kw):
        return dict({"type": "user", "uuid": uuid_, "timestamp": t, "cwd": "/x/Scope", "sessionId": sid,
                     "message": {"role": "user", "content": content}}, **kw)

    def a(t, mid, content):
        return {"type": "assistant", "uuid": f"a-{mid}", "timestamp": t, "sessionId": sid,
                "message": {"role": "assistant", "model": "claude-opus-4-8", "id": mid, "content": content,
                            "usage": {"input_tokens": 3, "output_tokens": 4}}}

    ev = [q("2026-10-01T01:00:00.000Z", "u1", "CLAUDEQ", _special={"type": "_notify", "label": "X",
                                                                   "summary": "INJECTED_NOTIFY"}),
          a("2026-10-01T01:00:01.000Z", "m1", [
              {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"Path": "/demo/a"}},
              {"type": "tool_use", "id": "t2", "name": "run_command", "input": {"CommandLine": "ls -la"}},
              {"type": "tool_use", "id": "t3", "name": "write_file", "input": {"Path": "/demo/b", "Content": "x"}}]),
          q("2026-10-01T01:00:02.000Z", "u2", [
              {"type": "tool_result", "tool_use_id": "t1", "content": "CLAUDEBODY1"},
              {"type": "tool_result", "tool_use_id": "t2", "content": "CLAUDEBODY2"},
              {"type": "tool_result", "tool_use_id": "t3", "content": "CLAUDEBODY3"}]),
          q("2026-10-01T01:00:03.000Z", "u3", [{"type": "_notify", "label": "SPOOF", "summary": "NOTICE",
                                               "raw": "BODY"}]),
          q("2026-10-01T01:00:04.000Z", "u4", [{"type": "_notify", "label": "SPOOFP", "raw": "B2"}])]
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in ev), encoding="utf-8")
    s = v.load_session(f, "scope-proj", "demo", v.SOURCE_CLAUDE)
    v.analyze(s)
    s.out_html, s.out_md, s.mem_href = "claude-code/demo/x.html", "claude-code/demo/x.md", ""
    body = _page_body(v.render_session_html(s, "../../../index.html", ""))
    md = v.render_session_md(s)
    ai = v.render_session_ai_md(s)
    # --- ①
    for name, shown in (("read_file", "/demo/a"), ("run_command", "ls -la"), ("write_file", "/demo/b")):
        assert f"{name} · {shown}" not in body and f"{name} · {shown}" not in md, f"Claude 的 {name}：摘要只畫工具名"
        assert f"{name} · {shown}" not in ai, f"Claude 的 {name}：精簡版也只畫工具名"
    for body_txt in ("CLAUDEBODY1", "CLAUDEBODY2", "CLAUDEBODY3"):
        assert body_txt in ai, f"Claude 的同名工具在精簡版照一般工具畫（輸出要留）：{body_txt}"
    assert "內容略" not in ai and "寫入 1 行" not in ai, "Claude 的同名工具不套 Antigravity 的讀檔／寫檔分類"
    assert v.tool_summary("read_file", {"Path": "/demo/a"}) == "read_file"
    assert v.tool_summary("read_file", {"Path": "/demo/a"}, ag=True) == "read_file · /demo/a"
    # --- ②
    assert "INJECTED_NOTIFY" not in body and "CLAUDEQ" in body, "Claude 事件的 _special 欄位不畫成通知列"
    # --- ③
    h = v.md_to_html('```x" onmouseover="alert(1)\nA\n```')
    # （③ 的另一半）Claude 事件的 `_notify` 區塊帶著 `label`：標籤照舊是「背景任務」（label 是 Antigravity 載入器的欄位）
    assert "SPOOF" not in body and "SPOOF" not in md and "SPOOF" not in ai, "Claude 事件的 _notify 區塊不認 label"
    assert "_⚙ 背景任務 · NOTICE_" in md, "Claude 的通知列照舊是「背景任務」"
    assert 'onmouseover="' not in h and "language-x&quot; onmouseover=&quot;alert(1)" in h, h
    print("OK: antigravity tools scoped to source test passed")


def test_antigravity_tree_deep_and_tmp_mode(tmp_path=None):
    """① 父子鏈很深（資料壞掉或刻意造的）：排樹不撞 Python 的遞迴上限——那個例外發生在每一場的例外處理之外，
       整批建置會停下來；
    ② `_ag_connect` 複製用的暫存目錄：Windows 上不用 0o700 建（Python 3.13 起那會換成一份不繼承的權限，
       沙箱化的行程連自己開的目錄都寫不進去）；POSIX 上要 0o700（`/tmp` 是大家共用的）；
    ③ 別人給的檔裡 `steps` 是跑不完的檢視表：只有那一場讀不起來，整批不卡住；
    ④ 執行預算用完就中止查詢。"""
    sys.path.insert(0, str(ROOT))
    import ai_session_viewer as v
    tmp = new_tmp(tmp_path)
    # --- ① 不讀檔：換掉讀身分的那一支，直接給一條很深的父子鏈
    n = sys.getrecursionlimit() + 500
    infos = {f"c{i}": {"id": f"c{i}", "path": Path(f"c{i}.db"), "parent": f"c{i - 1}" if i else "", "call": "",
                       "agent": "", "agent_name": "", "cwd": "", "branch": ""} for i in range(n)}
    real_info = v.antigravity_conv_info
    v.antigravity_conv_info = lambda p: dict(infos[Path(p).stem])
    try:
        roots, tree = v.antigravity_tree([Path(f"c{i}.db") for i in range(n)])
    finally:
        v.antigravity_conv_info = real_info
    assert len(roots) == 1 and len(tree[str(roots[0]["path"])]) == n - 1, "很深的父子鏈：每一場都在"
    # --- ② 暫存目錄的權限
    live, wsrc = tmp / "live", tmp / "wsrc"
    live.mkdir()
    wsrc.mkdir()
    D = "0d0d0d0d-0000-4000-8000-000000000201"
    wcon = ag_write_db(live / f"{D}.db", D, [ag_user(0, "AGMODEQ")], wal_conn=True)
    try:
        for suf in ("", "-wal"):
            shutil.copyfile(live / f"{D}.db{suf}", wsrc / f"{D}.db{suf}")
    finally:
        wcon.close()
    modes = []
    real_mkdir = Path.mkdir

    def spy(self, mode=0o777, parents=False, exist_ok=False):
        modes.append(mode)
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    Path.mkdir = spy
    try:
        con, tdir = v._ag_connect(wsrc / f"{D}.db")
    finally:
        Path.mkdir = real_mkdir
    try:
        assert tdir is not None and con.execute("select count(*) from steps").fetchone()[0] == 1
    finally:
        v._ag_close(con, tdir)
    assert modes == [0o777 if os.name == "nt" else 0o700], f"暫存目錄的權限：{[oct(m) for m in modes]}"
    # --- ③ 別人給的檔：`steps` 是一段永遠跑不完的遞迴查詢（檢視表）——這一場讀不起來，整批照樣建完、不卡住
    hv = tmp / "hostile"
    hv.mkdir()
    HV, GD = "4545aaaa-0000-4000-8000-000000000401", "4646bbbb-0000-4000-8000-000000000402"
    ag_write_db(hv / f"{HV}.db", HV, [ag_user(0, "AGHVQ")])
    con = sqlite3.connect(str(hv / f"{HV}.db"))
    con.executescript("drop table steps; create view steps as with recursive n(x) as (values(1) union all"
                      " select x + 1 from n) select x as idx, 14 as step_type, 3 as status, null as step_payload from n;")
    con.close()
    ag_write_db(hv / f"{GD}.db", GD, [ag_user(0, "AGGOODQ"), ag_planner(1, text="AGGOODRESP", usage=(1, 1, 0))])
    try:
        r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex", "--antigravity-source",
                            f"demo={hv}", "--out", str(tmp / "out-hv")],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
    except subprocess.TimeoutExpired:
        raise AssertionError("一個檔的 steps 是跑不完的查詢：整批建置卡住了")
    assert r.returncode == 0 and "壞掉跳過 1" in r.stdout, r.stdout + r.stderr
    assert "不是資料表：steps（view）" in r.stderr, "讀不起來的原因要講出是哪一個名字不是資料表：" + r.stderr
    assert any("AGGOODRESP" in p.read_text(encoding="utf-8") for p in _session_pages(tmp / "out-hv")), \
        "旁邊那一場照樣建出來"
    # --- ④ 執行預算：一個連線跑超過上限就中止當下那一句（真實的檔遠碰不到上限；這裡把上限調到 0 看它真的會擋）
    real_budget = getattr(v, "AG_SQL_BUDGET", 0)
    v.AG_SQL_BUDGET = 0
    try:
        BG = "4747aaaa-0000-4000-8000-000000000403"
        ag_write_db(tmp / f"{BG}.db", BG, [ag_user(i, f"q{i}") for i in range(3000)])
        try:
            v._ag_events(tmp / f"{BG}.db", BG, "", v.collections.Counter())
        except sqlite3.OperationalError as e:
            assert "interrupted" in str(e), e
        else:
            raise AssertionError("執行預算用完了，查詢要被中止")
    finally:
        v.AG_SQL_BUDGET = real_budget
    print("OK: antigravity tree deep and tmp mode test passed")


def test_antigravity_incremental_edges(tmp_path=None):
    """Antigravity 的來源與增量建置：
    ① 同一個檔出現在兩個 `--antigravity-source`（資料夾，和它裡面父對話那一個檔；兩種順序）：子代理不消失——
       只畫在一頁裡、產出只算一次；後面那個來源講出「已經在前面的來源裡」；
    ② 孤兒子代理自己一列之後，父對話出現了、但這一次的 `--project` 把父對話篩掉：子代理那一列不可以先被拿掉
       （兩場都還在、索引卻一列都沒有）；之後涵蓋父對話的建置照常併進去，那一列才消失；
    ③ 對話檔旁邊多了一個空的 `-wal`（或它的時刻變了）：內容沒變，頁面沿用、不重建。"""
    tmp = new_tmp(tmp_path)
    # --- ① 重疊的來源
    src = tmp / "overlap"
    src.mkdir()
    P, K = "a1a1aaaa-0000-4000-8000-000000000401", "b2b2bbbb-0000-4000-8000-000000000402"
    ag_write_db(src / f"{P}.db", P, [ag_user(0, "AGOVQ"),
                                     ag_planner(1, calls=[("c1", "invoke_subagent", {"TypeName": "x"})], usage=(1, 1, 0)),
                                     ag_generic(4, "c1", "invoke_subagent", {"TypeName": "x"}, result="ok")])
    ag_write_db(src / f"{K}.db", K, [
        ag_system(2, "task AGOVKID", event="agent_message", src=(P, "c1", "invoke_subagent")),
        ag_planner(3, text="AGOVKIDRESP", usage=(10, 8, 0))], parent=P, agent=("research", "Kid"))
    for n, (order, sources) in enumerate((("資料夾在前", (f"dir={src}", f"one={src / (P + '.db')}")),
                                          ("單一檔在前", (f"one={src / (P + '.db')}", f"dir={src}")))):
        out = tmp / f"out-overlap{n}"
        args = [a for s_ in sources for a in ("--antigravity-source", s_)]
        r = subprocess.run([sys.executable, str(SCRIPT), "--no-claude", "--no-codex", *args, "--out", str(out)],
                           capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr
        bodies = [_page_body(p.read_text(encoding="utf-8")) for p in _session_pages(out)]
        assert sum(b.count("AGOVKIDRESP") for b in bodies) == 1, \
            f"{order}：子代理只畫在一頁裡、不消失（{[b.count('AGOVKIDRESP') for b in bodies]}）{r.stderr}"
        assert sum(x["tok_out"] for x in _manifest_rows(out)) == 9, \
            f"{order}：產出只算一次（父 1＋子 8）：{[x['tok_out'] for x in _manifest_rows(out)]}"
        assert "1 個檔已經在前面的來源裡" in r.stderr, f"{order}：後面那個來源要講出來：{r.stderr}"
    # --- ② 縮範圍建置把父對話篩掉
    fl = tmp / "filtered"
    fl.mkdir()
    FP, FK = "c3c3aaaa-0000-4000-8000-000000000403", "d4d4bbbb-0000-4000-8000-000000000404"
    ag_write_db(fl / f"{FK}.db", FK, [
        ag_system(2, "task AGFLKID", event="agent_message", src=(FP, "c1", "invoke_subagent")),
        ag_planner(3, text="AGFLKIDRESP", usage=(10, 8, 0))], parent=FP, agent=("research", "Kid"),
        cwd="file:///home/demo/KidProj")
    fout = tmp / "out-filtered"
    _ag_build(fl, fout)
    assert len(_manifest_rows(fout)) == 1
    ag_write_db(fl / f"{FP}.db", FP, [ag_user(0, "AGFLQ"),
                                      ag_planner(1, calls=[("c1", "invoke_subagent", {"TypeName": "x"})], usage=(1, 1, 0)),
                                      ag_generic(4, "c1", "invoke_subagent", {"TypeName": "x"}, result="ok")],
                cwd="file:///home/demo/ParentProj")
    r = _ag_build(fl, fout, "--project", "UnrelatedProj")
    rows = _manifest_rows(fout)
    assert len(rows) == 1 and rows[0]["tok_out"] == 8, \
        f"父對話被範圍篩掉：子代理那一列要留著：{[x.get('title') for x in rows]}｜{r.stdout}"
    assert (fout / "sessions" / rows[0]["out_html"]).exists(), "子代理那一頁也還在"
    r = _ag_build(fl, fout)
    rows = _manifest_rows(fout)
    assert len(rows) == 1 and rows[0]["tok_out"] == 9, \
        f"涵蓋父對話的建置：子代理併進去、它自己那一列消失：{[(x.get('title'), x['tok_out']) for x in rows]}"
    # --- ③ 空的 -wal
    ew = tmp / "emptywal"
    ew.mkdir()
    E = "e5e5aaaa-0000-4000-8000-000000000405"
    ag_write_db(ew / f"{E}.db", E, [ag_user(0, "AGEWQ"), ag_planner(1, text="AGEWRESP", usage=(1, 1, 0))])
    eout = tmp / "out-emptywal"
    _ag_build(ew, eout)
    wal = ew / f"{E}.db-wal"
    wal.write_bytes(b"")
    r = _ag_build(ew, eout)
    assert "新建/更新 0，沿用 1" in r.stdout, f"空的 -wal 出現：頁面沿用：{r.stdout}"
    os.utime(wal, (time.time() + 60, time.time() + 60))
    r = _ag_build(ew, eout)
    assert "新建/更新 0，沿用 1" in r.stdout, f"空的 -wal 時刻變了：頁面沿用：{r.stdout}"
    print("OK: antigravity incremental edges test passed")


if __name__ == "__main__":
    # ⚠⚠ **這一格必須第一個跑**：它驗的是
    # `new_tmp()` 本身，而下面每一支測試都靠 `new_tmp()` 開工。排在後面的話，
    # helper 壞掉時整套會在**第一支測試**就 `PermissionError` 死掉，
    # **永遠到不了這一格**——於是「守著它的那格」在最需要它的時候完全沒有聲音。
    test_tmp_base_env()
    test_smoke()
    test_search()
    test_subagent_inline()
    test_day_divider()
    test_compact_marker()
    test_ai_md()
    test_codex_step_badges()
    test_codex_same_minute_outputs()
    test_session_kind_tags()
    test_codex_survival_report()
    test_cache_report()
    test_review_detection()
    test_cache_report_by_kind()
    test_classify_cache_causes()
    test_cold_cause_badges()
    test_api_miss_reason()
    test_write_ttl_badges()
    test_cost_mix_table()
    test_split_call_usage_last_event()
    test_ctx_window_and_coldest()
    test_codex_ai_label()
    test_codex_item_completed_user()
    test_account_switch_cause()
    test_report_partition_and_labels()
    test_acct_precision_and_server_cause()
    test_acct_separator_and_step_time()
    test_scope_notes_without_cold()
    test_prompt_loss_backstop()
    test_response_item_type_drift()
    test_forced_switch_parallel_label()
    test_limits_section_accumulator()
    test_message_role_and_content_drift()
    test_json_string_content()
    test_history_partial_blank_sentinel()
    test_durable_anchor()
    test_anchor_tiebreak_main_vs_side()
    test_anchor_tiebreak_sort_is_load_bearing()
    test_bookmark_ui()
    test_bookmark_block_anchors()
    test_bookmark_block_anchor_same_second()
    test_bookmark_manage_pages()
    test_bookmark_link_durability()
    test_bookmark_codex_title_masked()
    test_bookmark_deleted_source_pruned()
    test_bookmark_account_label_rebuild()
    test_bookmark_all_sources_deleted()
    test_zero_scan_keeps_out_of_scope_index()
    test_zero_scan_keeps_cache_report_links()
    test_no_uppercase_unicode_escape_in_js()
    test_user_turn_fidelity()
    test_codex_sentinel_false_alarms()
    test_one_bad_session_does_not_kill_batch()
    test_archive_command_only()
    test_build_interrupt_residue()
    test_cwd_cache()
    test_codex_tool_output_items()
    test_ask_user_question()
    test_sync_conflict_copies()
    test_renderer_version_pinned()
    test_antigravity()
    test_antigravity_tree_cycle()
    test_antigravity_tool_pairing()
    test_antigravity_identity()
    test_antigravity_page_marks()
    test_antigravity_tools_scoped_to_source()
    test_antigravity_tree_deep_and_tmp_mode()
    test_antigravity_incremental_edges()
