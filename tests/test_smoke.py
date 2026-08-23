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
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ai_session_viewer.py"


def new_tmp(tmp_path=None):
    """這一支測試要用的暫存目錄。給了 `tmp_path` 就用它，否則自己開一個。

    ⚠⚠ **為什麼不直接 `tempfile.mkdtemp()`**：在**沙箱化的 code review 環境**下
    （codex `workspace-write`），`mkdtemp()` 建出來的目錄**連根目錄都寫不進去**，
    更別說在裡面再建一層——而這個測試套件每一支都要建 `projects/<專案>/`。
    ⇒ 整套測試**一行斷言都跑不到**就 `PermissionError` 死掉，於是 reviewer
    **靜默降級成純讀碼、卻照樣寫出漂亮的 verdict**。這個 repo 為此賠過兩輪額度。

    ⭐ **2026-08-23 實測把成因釘死了**（`bookmarks-p4-codex` 的能力回報）：
    | 位置 | 建目錄 | 寫檔 |
    |---|---|---|
    | `mkdtemp()` 的根 | — | **✗ 存取被拒** |
    | `mkdtemp()` 底下再一層 | **✗** | ✗ |
    | runner 預建的 `%TMP%\\work\\` 底下（`Path.mkdir`）| **✓** | **✓**（連巢狀都可以）|

    ⚠⚠ **2026-08-23 晚間訂正（`bookmarks-p4fix-codex` Medium）：上面那張表是對的，
    但從它推出來的結論錯了。** 當時的結論是「事前就存在的目錄可以用」，於是本函式改成
    `mkdtemp(dir=<那個目錄>)`——**換了地點，沒換機制**。跨模型輪照樣整套死在
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
    在守，而不是用「建得出來嗎」在守。後者當時是綠的，而結論是錯的。
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
            # ⚠ **不可以用 `mkdtemp(dir=b)`**——理由見上方訂正。
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
    舊寫法 `rglob("*.html")[0]` 會隨機挑到它們——形狀斷言就對著一個空殼在跑，
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
    （`durable-anchor-r4` #3 順帶項）：渲染器把訊息內文的 `<`/`>` 跳脫成 `&lt;`/`&gt;`，
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
    **換回任何一個舊版，對應那一支就會紅。**
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
    #   書籤第 2 期在 `<tr>` 最前面插了 `data-sid`，原本寫 `'<tr data-source'` 的計數
    #   當場變成 0（實測）。每一列都有 `data-sid`，而它只出現在資料列上。
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
    print("OK: compact marker test passed")


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
    # （sid 前 8 碼須與 sid_a 不同：輸出檔名含 sid[:8]，同分鐘＋同專案＋同前綴會互蓋）
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
    # (v36-fam3 F2) **單步回合**：不畫逐步分隔列，也不符合 ❄ 的 `n_steps >= 2` → 成因與並列標籤
    #   原本在頁面上一個字都看不到，而 `_masked_human_note`／`_srv_excluded_note` 對讀者寫的正是
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
    # (v36-fam4 #2) 並列時 tooltip **不得自相矛盾**：前半句原本由 turn_cold_class 決定
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
        "hide-dur hide-eff": "耗時與 effort 應預設隱藏",
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
    # 且**顯示也要寫 24.6%**——寫成 25% 會與報告的「門檻＝命中率 < 25%」打架（G10 F2）。
    m = re.search(r'>⚡24\.6%</span><span class="meter (m-miss[^"]*)"[^>]*>⚠(系統提示變動)', html)
    assert m and m.group(1) == "m-miss", \
        f"命中率 24.6% 應判冷啟（整段失效）且顯示 24.6%，不得四捨五入成 25%：{m and m.group(1)}"
    assert 'class="meter m-cache coldx" title="此步驟未命中快取' in html, "該步的 ⚡ 也要著上冷啟色"
    assert 'class="meter m-cache coldx"' in html, "自報前綴變動的冷啟＝結構性，應為中性灰而非紅"
    # (G13) 首事件無可呈現內容的呼叫：成因 join 與 gap 都要用「呼叫起點」而非「第一筆有內容的事件」。
    # m_m5 是 1h cohort、間隔 180s 的冷啟 → 應判 evict（真失效＝紅標 cold），不得因 join 失敗變成灰的未歸因。
    assert 'class="meter m-cache cold"' in html, \
        "首事件無內容的呼叫，其真失效（evict）成因必須 join 得上並標紅，不得顯示成未歸因的灰"
    # 呼叫起點 01:15:30、首個有內容事件 01:21:30，前一步 01:10:30 → 正確 300s，錯誤會是 660s
    assert f"距上一步 {v.fmt_dur(300)}" in html, "「距上一步」須為真正的呼叫間隔（300s）"
    assert f"距上一步 {v.fmt_dur(660)}" not in html, "不得用「上次吐出內容→這次吐出內容」當間隔（660s）"
    report = (out / "cache-report.html").read_text(encoding="utf-8")
    assert "②-b" in report and "工具定義變動" in report, "報告頁應含 API 自報小節"

    # G4 gate 修正的回歸釘子。
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

    # (G5) 修 G4 的回歸：`cause or …` 會把主對話「已分析、非失效」的 cause=""（無自報）洗成 None → 誤塗紅，
    #      動到無 diagnostics 的舊資料（違反不變量①）。cause="" 且無自報必須維持中性灰。
    su_ana = {"total_in": 10000, "cache_read": 0, "input": 2, "cache_create": 9998, "output": 5, "miss": ""}
    assert 'class="meter m-cache coldx"' in v.render_step_meters(1, su_ana, cause=""), \
        'cause=""（已分析非失效）無自報時應維持 coldx，不得塗紅'
    g_ana = {"n_steps": 2, "u": {"input": 2, "cache_create": 9998, "cache_read": 0},
             "blocks": [{"type": "_step", "idx": 1, "cause": "",
                         "u": {"total_in": 10000, "cache_read": 0, "miss": ""}}]}
    assert v.turn_cold_class(g_ana) == "coldx", 'cause="" 的已分析冷啟回合應中性灰（不變量①）'

    # (G6 #1) rewrite_waste_usd 混合 TTL 要逐段拆（同 avoid_usd），不得整段按 1h 高估。
    mix = v.rewrite_waste_usd("claude-opus-4-8", 50000, 9000, 1000, wrote=10000)   # 9k@5m + 1k@1h
    assert abs(mix - 10000 * (1.325 - 0.10) * 5 / 1e6) < 1e-9, f"混合 TTL 應逐段拆（blended 1.325），得 {mix}"
    assert abs(mix - (9000 * 1.25 + 1000 * 2.0 - 10000 * 0.10) * 5 / 1e6) < 1e-9, "missed==wrote 應化簡成 avoid_usd"
    assert abs(v.rewrite_waste_usd("claude-opus-4-8", 5000, 0, 8000, wrote=8000)
               - 5000 * 1.9 * 5 / 1e6) < 1e-9, "純 1h 寫入仍為 2.0（不回歸）"
    # (G6 #2) fmt_money 的 "<$0.01" 在 ②-b HTML 要逃逸成 &lt;$0.01，不得吐裸 <（無效標記）。
    d_tiny = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                    "cache_steps": [step(0, 0), step(600, 0, tools_code, 1)],
                                    "cache_models": ["claude-opus-4-8"], "cache_events": []}])
    h_tiny = v.render_cache_report_html(d_tiny)
    assert "&lt;$0.01" in h_tiny and "~<$0.01" not in h_tiny, "②-b 的 <$0.01 金額應逃逸，不得吐裸 <"

    # (G8 #2) 全部都是未知模型時 usd＝0，但仍要顯示「?」提示算不出來——整句消失會讓人以為沒多付。
    d_unk = v.build_cache_report([{"source_kind": v.SOURCE_CLAUDE, "account": "", "kind": "chat",
                                   "cache_steps": [step(0, 0), step(600, 0, tools_code, 5000)],
                                   "cache_models": ["claude-zzznew-9"], "cache_events": []}])
    assert d_unk["api"]["usd_partial"] is True and d_unk["api"]["cold_usd"] == 0
    assert "多付 <b>~?</b>" in v.render_cache_report_html(d_unk), "全未知模型時金額應顯示 ?，不得整句消失"
    assert "多付 ~?" in "\n".join(v._api_miss_md(d_unk)), "MD 版同樣要顯示 ?"

    # (G8 #3) 帶內失效樣本全被排除時（band 空），排除揭露更要講——否則遵約率看起來完美卻沒說原因。
    d_allx = rep([st2(0, 0), st2(1000, 0, tools_code)])
    assert d_allx["api"]["band_excluded"] == 1 and not d_allx["api"]["band"]
    note_x = v._band_api_note(d_allx)
    assert "整對移出樣本" in note_x and "1 對" in note_x, f"band 空時仍須揭露排除：{note_x!r}"

    # (G12 #1) 帶內樣本被排除到 0 時，假說頁不得謊稱「尚無應命中帶樣本…累積資料後才有東西可檢定」
    #          （樣本存在、是被排除掉的），且排除揭露必須照樣出現——先前被 if comply_n 的 guard 擋掉。
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

    # (G12 #2) 回合徽章的整段／只掉一段要照實拆，不得因「有一步整段冷啟」就把整個 ×N 講成整段失效
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

    # (G10 F3) 回合／表頭金額也要有 +?：未知模型不得靜默貢獻 0，讓畫面顯示偏低的「多付」。
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

    # (X1 F2) 逐步徽章同理——回合／表頭／②-b 三層都標 ? 了，只有步驟層靜默省略金額＝同一事實兩處
    #         說法不一，且「失效前綴 9.0k」不附金額會被讀成「沒多付」。三種情形都要釘：
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

    # (G8 #1) ③ 的 MD 版不得還寫「不可避免＝第一句/切帳號/換模型/壓縮」（HTML 已改，MD 漏改＝兩處說法不一）
    md_rep = v.render_cache_report_md(d1)
    assert "不可避免＝第一句" not in md_rep, "③ MD 仍用舊標籤（前綴變動也落在該桶，說法會與 ② 打架）"
    assert "非閒置造成" in md_rep, "③ MD 應改用「非閒置造成」並列出前綴變動"

    # (G9 #1) 壓縮邊界後的冷啟會自報 messages_changed：不得被通稱 msgs 蓋掉更準的 compact 實據，
    #         否則「壓縮後」那欄歸零，還被挪進「習慣可避免」（自動壓縮不是改習慣能省的）。
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

    # (X2 F1) 首步也要「有自報就以自報為準」。快取跨 session 共用 → 新 session 第一句仍可能命中上一場
    #         的前綴，此時 tools/system/msgs 才是真成因；標 first 會把「習慣可避免」記進「不可避免」桶，
    #         也讓 ② 與 ②-b／逐步徽章對同一步說法不一。
    raw_f1 = [st2(0, 0, tools_code)]
    assert v.classify_cache_causes(raw_f1, ["claude-opus-4-8"], []) == {0: "tools"}, \
        "首步自報 tools_changed 不得被 first 蓋掉"
    d_f1 = rep(raw_f1)
    assert d_f1["causes_total"]["tools"] == 1 and d_f1["causes_total"]["first"] == 0, \
        f"報告端同理：{ {k: n for k, n in d_f1['causes_total'].items() if n} }"
    assert v.classify_cache_causes([st2(0, 0)], ["claude-opus-4-8"], []) == {0: "first"}, \
        "沒有自報時仍須是 first（不得反向誤判）"

    # (X2 F2) 前綴變動步若沒有新寫入，舊 TTL lineage 必須失效——否則下一個冷啟沿用舊 1h cohort、
    #         被判成假的「1h 內卻冷啟」(evict)，正是本工具要消滅的東西。
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

    # (X2 F2b) band_excluded ＝「帶內樣本的**完整**減少量」（反事實差額），不是只數被直接排除的那一對：
    #          lineage 失效會讓下游的對也從 1h 掉進 unknown、一併離開帶內。此 fixture 減少 2 對、
    #          只數直接排除會report 1，故有鑑別力。
    raw_f2c = [[600, 1900, 2000, 2000, 0, 2000, 0, 0, 0],
               [1200, 0, 2000, 2000, 0, 0, 0, tools_code, 0],
               [2700, 0, 2000, 0, 0, 2000, 0, 0, 0]]
    on_c, off_c = rep(raw_f2c), rep([r[:7] + [0, 0] for r in raw_f2c])
    drop_c = off_c["kpi"]["comply_n"] - on_c["kpi"]["comply_n"]
    assert drop_c == 2, f"前提：這組反事實應減少 2 對（否則斷言沒鑑別力），實得 {drop_c}"
    assert on_c["api"]["band_excluded"] == drop_c, \
        f"band_excluded 須恆等於帶內樣本減少量：報 {on_c['api']['band_excluded']}、實減 {drop_c}"

    # (X2 F3) ②-b 母體＝所有有自報成因的呼叫。ctx < REPORT_MIN_CTX 的過濾只服務「由命中率推論成因」，
    #         對 API 明講的事實不適用；過濾掉會少算次數／長度／金額，還可能吞掉未知的新型別。
    d_f3 = rep([[0, 0, 400, 400, 0, 400, 0, tools_code, 321]])
    assert d_f3["api"]["cold"].get("tools_changed") == 1 and d_f3["api"]["cold_tok"] == 321, \
        f"ctx < {v.REPORT_MIN_CTX} 的自報呼叫不得被靜默吞掉：{d_f3['api']}"

    # (X4 H2) 被 REPORT_MIN_CTX 濾掉的前綴變動步，照樣污染它所夾的那一對相鄰步。
    #         不看的話結構性實據被分析門檻吃掉，下游冷啟被誤判成假 evict——實測 ctx 差 1 個
    #         token（400 vs 500）結論就整個翻轉，那是分析門檻不該有的權力。
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

    # (X4 H1) 同一秒的兩次呼叫不得共用 join 鍵。撞號時後者會蓋掉前者，而自從首步也採用 API
    #         自報成因之後，後果從「推論成因貼錯」升級成「沒有 diagnostics 的那一步被標成
    #         API 自報」＝把推論講成實據，違反最高不變量①。
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


def test_ctx_window_and_coldest(tmp_path=None):
    # G3 gate 修正的回歸釘子：兩個「門檻／前綴一致性」bug。
    import importlib
    sys.path.insert(0, str(ROOT))
    v = importlib.import_module("ai_session_viewer")
    # (1) context 視窗改用裸前綴（同 PRICE_PER_M）：新版 opus/sonnet 不得靜默失去「佔視窗 %」。
    assert v.context_window("claude-opus-5") == 1_000_000, "新版 opus 也要對得到視窗（裸前綴 fail-open）"
    assert v.context_window("claude-opus-4-8") == 1_000_000
    assert v.context_window("claude-sonnet-5-20260101") == 1_000_000
    assert v.context_window("claude-3-5-sonnet-20241022") == 200_000, "3.x 世代仍落 200k fallback"
    assert v.context_window("gpt-5.5") is None, "非 Claude 查不到就回 None（只顯示絕對值）"
    # (X4 M6) 1M 是**分版本**的，不能用裸前綴一律當 1M：opus/sonnet 要 4.6 以上才是 1M。
    #         先前 sonnet-4-5 的 100k 脈絡會被算成 10%（實際 50%），差 5 倍。
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

    # (X2 F4) 同一顯示回合可能併了不同模型的呼叫（opus 1M 視窗 → haiku 200k 視窗）。回合的
    #         「佔視窗 %」必須用 **ctx_max 那一步** 的視窗，不是第一步的——否則回合徽章與逐步徽章
    #         對同一事實給出兩個百分比（實測 19% vs 95%）。HTML 與 MD 都要對。
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

    # (X3 M1) 「距上一步／effort」原本只掛在逐步分隔列上，而單步回合刻意不畫那一列
    #         → 單次呼叫的回合（多數）這兩欄完全看不到；MD 更是完全不畫逐步列、單步多步都沒有。
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

    # (X3 M2) turn_duration 若在「還沒有開著的 assistant 回合」時抵達（緊接可見 user、同一次呼叫的
    #         內容後到），原本直接丟掉 → 那則的 ⏱ 永遠消失。改成按 message.id 暫存再掛回。
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
    # (G7) 選「最冷步」也要用精確比值：25.0% 與 24.6% 同 round 成 25 時，要選真正最冷（步2）並標冷。
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

    # (G10) 門檻邊界的「顯示」也要與判定一致：24.6% 不得寫成 25%（報告說門檻是「< 25%」）。
    assert v._pct_display(2460, 10000) == "24.6", "剛好進位到門檻時要多給一位小數"
    assert v._pct_display(2500, 10000) == "25", "正好 25% 照常取整（它不是冷啟）"
    # 一位小數也會進位：24.95〜24.99% 不得印成 25.0（否則同一個矛盾只是換個位數重現）
    for cr, tot, want in [(2495, 10000, "24.9"), (2499, 10000, "24.9"), (24999, 100000, "24.9")]:
        assert v._pct_display(cr, tot) == want, \
            f"{100*cr/tot:.4f}% 應顯示 {want}（向下取），得 {v._pct_display(cr, tot)}"
        assert cr * 100 < v.CACHE_COLD_PCT * tot, "前提：這些都該是冷啟"
    assert v._pct_display(9970, 10000) == "100" and v._pct_display(0, 10000) == "0", "其餘照常取整"
    assert r3[0] == "24.6", f"❄最低 也要顯示 24.6 而非 25：{r3[0]!r}"
    # (G10 F1) turn_cold_class 的冷熱判定同樣不得先四捨五入（否則整段不著色、❄ 與逐步卻著色）
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

    # N：`item` **容器本身**漂了——不是 dict 而是包著 dict 的 list。舊寫法直接換成 {}，
    #    於是 item 層每一個哨兵都看不到東西；payload 這一層又不帶 role（實測 485 個 rollout 皆然），
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

    # Z：(v36-fam3 F3) **舊格式** user_message 的 `message` 從字串漂成 content block 陣列。
    #    原本是 `str(payload.get("message") or "")` → Python 的 repr 整包被當成 prompt 收下，
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

    # ZR：(v36-fam4 #4) `response_item` 的**容器**漂了（list 包 dict）。助理訊息、reasoning 與
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

    def md_of(sid):
        return next(v for k, v in mds.items() if sid[:8] in k)

    a = next(v for k, v in pages.items() if sid_a[:8] in k)
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

    # N：item 容器不是 dict → 收不到回合，但**必須**出聲（舊寫法換成 {} 之後三層哨兵全都接不到）
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

    # Z：(v36-fam3 F3) 舊格式的型別漂移要走與新格式同一組檢查——文字照收、被丟掉的 block
    #    要數得出來，而 Python 的 repr **不得**被當成 prompt 收下。
    z_md = md_of(sid_z)
    assert "舊格式漂移LEGACYDRIFTQ" in z_md, \
        f"舊格式 content block 陣列的文字部分仍要收得到：{z_md[:400]}"
    assert "'type': 'text'" not in z_md and "image_url" not in z_md, \
        f"Python 的 repr 不得被當成 prompt 收下（三層哨兵都認不出來，只能靠這裡擋）：{z_md[:400]}"

    # ZR：(v36-fam4 #4) 助理內容整批消失時必須出聲——這是「看起來完整、少了東西」的另一半，
    #    而它原本完全無聲（頁面：一則提問、零則回答）。
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

    # (v36-fam4 #1) **報告那兩處也要同口徑**。上一批只修了索引三處、只釘了索引側的斷言，
    #   而 KPI 磚的副標題就寫著「與索引『浪費』欄同口徑」——報告寫 $0+?、索引寫 ? 時那句是假的。
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

    # ⑤ (v36-fam3 F6) 同時有結構性邊界的那一對，不得再算成「因伺服器自報而排除」——
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

    # ④ (v36-fam3 F1) **三步以上**才量得到 lineage。上面 ③ 的 srv fixture 全是兩步，量不到
    #    「下一個冷啟用哪個 cohort」——而報告端與徽章端當時正是在這一格分岔：報告端遇到 srv 會重置
    #    lineage、classify 端不會（重置條件漏了 srv_cause）。同一步於是一邊算 expiry（人因、記錢）、
    #    一邊算 evict（非人因、不記錢），同一頁上兩個地方對同一步講相反的話。
    #    兩邊現已共用 `_resets_lineage`，這條測試就是釘住它的那根釘子。
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
    # ⚠ (v36-fam4 #7) 這裡原本直接斷言相等，而 fixture 兩對都是 1h cohort → 必然相等、
    #   抓不到任何東西（本機語料兩邊也剛好都是 14，實跑同樣蓋不到）。改成斷言真正成立的
    #   關係，並補一組非 1h 的 evict 證明這條測試現在分得出來。
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
    # ⚠ 只比「有沒有那幾個字」釘不住：這條線**配套插進去的裝飾**（曾經有一條 `---`）一樣
    # 不屬於任何一則，卻照樣會落進前一則。改成比對「同一場但沒切帳號」的切塊結果——
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
    """(v36-fam5 #6) 零冷啟時三段揭露仍要出現在 ②——它們講的是偵測範圍與樣本排除，與有沒有冷啟無關。

    舊寫法把三段包在 `if total_cold:` 裡，於是「一律揭露，不看數字是不是 0」那句註解在
    零冷啟那一支是假的：最需要保留懷疑的那一格反而什麼都不說。實測本機有 289 個
    「有 cache_steps、零冷啟成因」的 session，只用它們建報告時 `srv_excluded` 仍有 75 對
    要揭露卻一句都不出。
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
    """(v36-fam5 #3) prompt 事件與 `task_started` 一起漂掉時，仍要有一條哨兵出聲。

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
    """(v36-fam5 #2) `response_item` 的型別改名／content 元素改名，都要出聲。

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
    """(v36-fam5 #1) 撞過 limit 之後才換的帳號：並列標示，但**記帳不變**。

    `boundary` 的「被迫/自願」只看同一對相鄰步之間的事件，429 與切換中間夾了任何一次呼叫，
    那次被迫切換就落成 `acct`（人因、記錢、紅色徽章）。全語料實測 10 個 acct 標籤裡有 3 個
    是這種（間隔 5005／4942／11584 秒、$9.05 ＝ 人因浪費的 3.03%）。
    Will 2026-08-21 裁決：並列顯示 ＋ 報告揭露，**不動成因也不動金額**。
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

    # tooltip 不得自相矛盾：修正語要排在成因說明**之後**（v36-fam4 #2 那個坑）
    title = v._cold_cause_title("acct", ["limit_earlier"], "這一步的成因")
    assert title.index("沒撞 limit」只對這一對") > title.index("這場中途換了帳號（沒撞 limit）"), (
        "補述要排在成因說明之後才是修正，排在前面就是兩句話互相否定")
    assert "**" not in title, "tooltip 是純文字 title 屬性，不可留 markdown 星號"

    # 顯示層專用鍵不得混進成因母體，否則成因表會多一列永遠是 0 的假成因
    assert "limit_earlier" not in v._COLD_CAUSE_LABEL, "並列條件不是成因，不可進 REPORT_CAUSES"

    print("OK: forced switch parallel label test passed")



def test_limits_section_accumulator():
    """(v36-fam6 #1，Blocker) 撞牆時刻是**跨 session 累加**的，不可被逐 row 的區域變數蓋掉。

    v36-fam5 #1 在逐 row 迴圈內新建了一個同名的 `limit_ts`，把迴圈外那個累加器整個覆寫，
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
    #    （逐 row 覆寫的寫法在這裡會回 0——這正是 v36-fam5 #1 的症狀）
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
    """(v36-fam6 #2) `role` 改名／缺欄、`content` 欄改名——與型別改名同症狀，但既有哨兵接不到。

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
    """(v36-fam6 #3) content 被序列化成 JSON 字串時，不可把整包原文當成 prompt 收下。

    與 v36-fam3 F3 修掉的「`str()` 的 repr 被當 prompt」同一類，只是漂移點再往內一層，
    F3 的修法沒涵蓋到，而且新舊兩種格式**都**中。這個形態不是憑空假設——
    `_codex_container_user_like` 本來就特地涵蓋「被序列化成 JSON 字串的 dict」。

    ⚠ 反方向同樣要釘住：使用者**真的把一段 JSON 貼進來當問題**時，不可以被解析掉——
    那是比漏報更糟的竄改。
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

    # ① 新格式：item.content 是 JSON 字串 → 要還原成真正的問句，並出聲
    t, err = run(line(3, "event_msg", {"type": "item_completed", "item": {
        "type": "UserMessage", "id": "i1",
        "content": json.dumps(blocks, ensure_ascii=False)}}), "new")
    assert any("真正的問句JSONQ" in x for x in t), f"應還原成真正的問句，實得 {t!r}"
    assert not any(x.strip().startswith("[{") for x in t), f"不可把整包 JSON 原文當 prompt：{t!r}"
    assert "序列化成 JSON 字串" in err, f"還原了也要出聲（上游換了形狀）：{err!r}"

    # ② 舊格式：message 是 JSON 字串 → 兩側對稱
    t, err = run(line(3, "event_msg", {"type": "user_message",
                                       "message": json.dumps(blocks, ensure_ascii=False)}), "legacy")
    assert any("真正的問句JSONQ" in x for x in t), f"舊格式也要還原，實得 {t!r}"
    assert "序列化成 JSON 字串" in err, f"舊格式也要出聲：{err!r}"

    # ③ ⚠ 反方向：使用者真的貼 JSON 陣列當問題 → 原文一個字都不能動
    raw = "[1, 2, 3]"
    t, err = run(line(3, "event_msg", {"type": "user_message", "message": raw}), "userjson")
    assert t == [raw], f"使用者真的貼的 JSON 不可被解析掉（那是竄改），實得 {t!r}"
    assert "序列化成 JSON 字串" not in err, f"這不是漂移，不該出聲：{err!r}"

    # ④ 一般字串不受影響
    t, err = run(line(3, "event_msg", {"type": "user_message", "message": "一般問句PLAIN"}), "plain")
    assert t == ["一般問句PLAIN"] and not err.strip(), f"一般字串不得受影響：{t!r} {err!r}"

    print("OK: json string content test passed")



def test_history_partial_blank_sentinel(tmp_path=None):
    """(v36-fam6 #5) **部分** history 認不得時也要出聲，不能只在全部都認不得時才說。

    切帳號是靠**跨帳號比對**成立的：少掉一邊，這條路徑就實質失效，而回傳值與
    「真的沒切過」完全同形。舊條件是 `h["files"] and not by_sid`（全部都認不得），
    只要還有一份讀得出來就一聲不吭。
    順帶釘住 `files` 只算**開得起來**的那幾份（舊寫法在 open 之前就 +1，開檔失敗時
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

    # ① 一份好的 ＋ 兩份漂掉 → 舊條件不成立（by_sid 非空），新哨兵必須出聲
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
    sys.path.insert(0, str(ROOT))   # 全檔 17 個測試裡唯一漏掉的一個（durable-anchor-r4 #3 順帶項）
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
    # "k20260801T025233123Z"，再驗一次是**不可能紅**的恆真斷言（durable-anchor-r3 #5）。

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
                         # 算成重複的頁面 id（durable-anchor-r4 #3 順帶項）。
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
    # 後者對任何指派順序都成立——durable-anchor-r2 #6 實測示範過：把排序整條刪掉、
    # 或改成 reverse=True，舊版的三條斷言**全綠**。
    # 現在的 tiebreak 是來源檔行序（`src_i`），所以先出現在 JSONL 裡的那一則拿無後綴。
    def _turn_id_of(mark):
        """含 `mark` 的那個 .turn **開標籤上**的 id（沒有 id 屬性就回 ""）。

        ⚠ 只看開標籤，不能看整段：標頭裡有個 `<span class="tanchor" id="tN">`，
        看整段會把它的 id 當成回合的 id——那樣「沒有耐久錨點的回合不該有 id」
        這條就永遠驗不出來（第一版就是這樣，被自己的新斷言抓到）。
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
    # ⚠ 舊版寫 `not startswith("k")`，擋不住「退回 t{n}」這個明文禁止的作法
    # ——把 kanchor 設成 anchor 的突變下，那條照樣綠（durable-anchor-r3 #5）。
    # 要驗的是：**那個 .turn 根本沒有 id 屬性**。
    assert _turn_id_of("NOTIME") == "", \
        f"沒有時間的回合的 .turn 不該有 id（更不可以退回 t{{n}}），實得 {_turn_id_of('NOTIME')!r}"

    # ⚠ **最高不變量：同一頁的 id 必須唯一。** 重複的話 getElementById 會靜默取第一個。
    # 在這條加進來之前，這個不變量在整個 tests/ 裡是**零自動化守衛**（durable-anchor-r3 #5）。
    dup = _page_dup_ids(html)
    assert not dup, f"同一頁出現重複的 id：{dup}"

    # # 直達連結：指向耐久錨點、title 帶本地時間、走 openSub
    assert f'href="#{base}"' in html, "# 連結應指向耐久錨點"
    assert 'class="alink"' in html and "這一輪的直達連結" in html, "# 連結與 title 應存在"

    # ⚠ 下面這幾條**只是煙霧檢查**（那幾段 JS 有沒有被整段刪掉），
    # **不是行為測試**：對產生出來的 JS 做字串比對，只有改名字會紅、改行為不會紅
    # （durable-anchor-r2 #8）。真正驗行為的是 `node scripts/probe_anchor_js.js <某頁>.html`
    # ——那支用假 DOM 實際跑這些函式，涵蓋退化、橫幅、上提、同分頁導覽與關閉鈕共 13 組。
    # ⚠ `tests/` 會投影到公開 dist repo，所以不在這裡引入 node 依賴。
    for _frag, _why in (("function bmMiss(", "找不到錨點時的橫幅"),
                        ("bm-miss", "橫幅樣式"),
                        ("addEventListener('hashchange'", "同分頁導覽的進入點"),
                        ("classList.contains('tanchor')", "tanchor 上提")):
        assert _frag in html, f"{_why}那段 JS/CSS 不見了（行為請用 scripts/probe_anchor_js.js 驗）"

    # MD 側：兩個錨點並存，且 {#tN} 在**同一行的前面**（_TURN_HEAD_RE 靠它切回合）
    # ⚠ 舊版寫 `md.index("{#t1}") < md.index("{#k…}")`，那驗的是**行序不是行內序**
    # （durable-anchor-r2 #8）：兩個標記分屬不同行時照樣成立，就算 render_turn_md 改成
    # 先印 k 錨點（那會直接打死 _TURN_HEAD_RE）也不會紅。要驗就驗同一行上的相對位置。
    assert "{#t1}" in md and f"{{#{base}}}" in md, "MD 應同時帶兩個錨點"
    both = [ln for ln in md.splitlines() if "{#t" in ln and f"{{#{base}}}" in ln]
    assert both, f"應有一行同時帶 {{#tN}} 與 {{#{base}}}"
    for ln in both:
        assert ln.index("{#t") < ln.index(f"{{#{base}}}"), \
            f"同一行上 {{#tN}} 必須在耐久錨點之前（_TURN_HEAD_RE 靠它）：{ln!r}"

    # 全文搜尋沒有被新錨點打壞（這一格擋過一次真的回歸）
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

    ⚠⚠ **這條保證在這個 repo 被寫錯過三次**（`durable-anchor-y1` #2 →
    `r2` #3 → `r4` #1），三次都是因為註解宣稱了某個保證、卻沒有任何測試在守它。
    這條測試存在的意義就是：**下次再寫錯，它會紅。**

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

    ⚠⚠ **為什麼要有第二條 tiebreak 測試**（`durable-anchor-r4` #3）：
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
    拿它當素材的話 `js_embed()` 根本沒被執行到——**測試會綠，但綠得毫無意義**
    （第一版就是這樣寫的）。`extract_rename()` **不剝標籤**，那才是真正沒有上游防護的路徑。
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
        (".bm-chip{", "單選 chip 的樣式（⚠ 不是下拉：Will 2026-08-22）"),
    ):
        assert frag in html, f"缺少{why}：`{frag}`"
    assert f'var BK_SID="{sid}"' in html, "BK_SID 應填成這一場的 session id"
    assert "var BK_FALLBACK='6m'" in html, "提醒複查的預設值應為半年（Will 2026-08-22 裁決）"
    # ⚠ 書籤記錄裡的 url 是**相對 out/ 的**：檔名含本地時間＋專案名，絕對路徑一旦寫進
    #   匯出的 JSON 就會帶著 `C:\\Users\\<名字>\\` 出門。
    mu = re.search(r'var BK_SID=.*?, BK_URL="([^"]*)"', html)
    assert mu, "頁面裡找不到 BK_URL"
    assert mu.group(1).startswith("sessions/"), f"BK_URL 應相對 out/，實得 {mu.group(1)!r}"
    assert ":" not in mu.group(1) and not mu.group(1).startswith("/"), \
        f"BK_URL 不可以是絕對路徑，實得 {mu.group(1)!r}"

    # --- 3b. 索引頁的書籤圖示（Will 2026-08-22 追加）------------------------
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
    # 第一版用 `rindex` 撈到的是頁尾那一個，於是往前找到的是**整頁最後一個** `.blk`
    # ——斷言照跑，只是驗錯了對象（測試自己先示範了一次「安靜指錯」）。
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

    # --- 3. 每個可標記區塊都要有自己的 ☆（Will 2026-08-23 裁決：文字段落也要）--
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
    # ⚠ 要驗**完整的那一個 id**。第一版寫成 `'id="k' in html and f'-s{ep}"' in html`，
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
    # 第一版就是這樣寫的，結果 M2／M9 兩個突變**全部漏掉**：斷言看起來很像在守這件事，
    # 其實素材根本到不了那個狀態。
    # ⚠ 這裡**不是**把它降級成「沒有素材所以不驗」（教訓 34）——契約照驗，
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
    # 第五條（`bookmarks-p4fix-codex` Medium）：**負的步 epoch 一律不給錨點**。
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

    # --- 7b. ⚠⚠ **第二個產出端**：步驟列的 id 也不可以吐出帶號的 `-s`（p4fix-r2 Low）---
    # 上面那幾條只對 `block_anchor()` 這個純函式下斷言，而 `-s<ep>` 有**兩個**產出端：
    # 區塊錨點與**步驟列的 id**。第一版只修了前者 ⇒ 同一個 group 裡區塊錨點正確消失，
    # 步驟列卻照樣輸出 `k…-s-900`——那正是 `bmGrammarOk()` 會拒絕的形狀。
    # ⚠ **這一格必須走 renderer**，不可以再對純函式下斷言：純函式那一層永遠看不到
    # 第二個產出端，而「同一條規則寫在兩個地方」正是這個 repo 反覆吃虧的形狀。
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

    ⚠⚠ **為什麼會有這一支**（`bookmarks-p4-fam` High）：第一版的 `-s<步epoch>` 只有**秒**
    （`_epoch()` 是 `math.floor`），而回合錨點保留毫秒、真撞號時還會加 `-tb<n>`。
    同一輪裡兩次 API 呼叫落在同一秒 ⇒
      ① 兩個 `<div class="step-sep">` 共用一個 id（最高不變量：同頁 id 必須唯一）
      ② 兩步底下的區塊拿到**完全相同**的 `-s<ep>-b<n>`
      ③ `getElementById` 取第一個 ⇒ 在第二塊按 ☆ 存下來的是**第一塊**的摘要；
         跳回去時 `openSub()` 回 `false`（＝精確命中）、`.hl` 框在第一塊、
         **沒有橫幅、還改寫網址列** ＝ 這整套設計要消滅的「安靜指錯」。

    ⚠⚠ **根因是把量測讀錯**：全語料量到「同輪內步時戳撞號 0/67876」就當成保證——
    那是「還沒發生過」，不是「不會發生」。`durable_anchor()` 的註解隔壁就寫著
    「只截到整秒，撞號會從 1 變 31」，同一份道理沒有套到步這一層。

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
    這個 repo 為此賠過兩輪額度。有了這個環境變數，charter 只要加一行就能讓它真的跑測試。

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

        # ⚠⚠ **這一格才是真正守著沙箱那件事的**（`bookmarks-p4fix-codex` Medium）。
        #
        # 上面那幾行在**本機**永遠會過，不管 `new_tmp()` 內部走的是 `mkdtemp()` 還是
        # `mkdir()`——因為本機沒有沙箱，兩條路都寫得進去。於是「已經解掉了」這個結論
        # 在本機**無法被否證**，而實際上沒解：`new_tmp()` 當時仍是 `mkdtemp(dir=base)`，
        # 跨模型輪照樣整套死在 `WinError 5`，只是死在 `work\tmpXXXX\projects` 而不是
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
       拿它當素材等於沒測到——第 2 期就是這樣寫錯過一次。）
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
    assert not (out / "bookmarks.html").exists(), "⚠ 管理頁不可以放 out/ 根（見提案〈B〉）"
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
        ('id="bxDue"', "⚠ 「⏰ 只看該複查的」篩選（Will 明講一定要有）"),
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
        "⚠ 不要下拉（Will：「下拉使用上比較不方便」）——單選一律用 chip"

    # --- 7. session 頁的類別改成 chip（不可以還留著 datalist）---------------
    assert 'id="bkCats"' in page, "加書籤對話窗的類別 chip 容器"
    assert "bkCatNew(" in page and "＋ 新類別" in page.replace("\\uff0b", "＋"), \
        "⚠ 要有「＋ 新類別」就地新增（不可以退化成只能從設定頁選）"
    assert "list=\"bkCats\"" not in page and "<datalist" not in page, \
        "⚠ 第 2 期的 datalist 要整個拿掉，不可以兩套控制項並存"

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
    #   第一版就是那樣寫的，被下面那條「檔名真的變了嗎」擋下來。
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
    # ⚠⚠ **這一格原本是空心的**（`bookmarks-fix1-fam` 突變檢驗抓到）：
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
    也跟著指到那裡（跨模型 `bookmarks-codex` Medium#5 的 [推論] 那半）。

    ⚠ 這一支的存在理由：`bookmarks-fix1-fam` 那一輪證明過 `reusable` 裡
    `row.get("account","") == (acc_name or "")` **真的在做事**（拿掉它 `BX_SESS` 會指到
    舊帳號目錄），但當時**整份 `test_smoke.py` 拿掉它仍然全綠**——那是一條確定存在的空心。

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
    （`bookmarks-fix1` Medium#6；`test_bookmark_deleted_source_pruned` 只做
    「兩場刪成一場」，2→0 那一格一直沒有素材走到 ⇒ 教訓 29。）

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
    **只有索引頁說一場都沒有**（收斂確認輪 High）。
    ⚠ 改之前這裡是直接 `return`、索引不會被動到，所以這是「零場也要對帳」那批**新造出來的**。

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

    上一條（`test_zero_scan_keeps_out_of_scope_index`）修好的是同一個呼叫的**第一個**參數
    （rows 改從 `new_entries` 算）；第三、四個參數當時還是寫死的 `False, False`
    ⇒ 零場建置之後 `cache-report.html` / `cache-hypotheses.html` **檔案都還在磁碟上**，
    索引頁卻不再連到它們。**和上一條是同一個形狀**（用寫死的值取代對帳出來的狀態），
    只是換到隔壁兩個參數（`bookmarks-fix2-fam-r2` 驗收找到的）。

    ⚠ 這一支只守「連結還在」這一件事（教訓 37：一格只守一件事）。
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
    實際踩到：管理頁連結、頂列書籤鈕、設定頁刪除鈕、索引頁書籤圖示，**四處全中**，
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


if __name__ == "__main__":
    # ⚠⚠ **這一格必須第一個跑**（`bookmarks-p4fix-codex` Medium）：它驗的是
    # `new_tmp()` 本身，而下面每一支測試都靠 `new_tmp()` 開工。排在後面的話，
    # helper 壞掉時整套會在**第一支測試**就 `PermissionError` 死掉，
    # **永遠到不了這一格**——於是「守著它的那格」在最需要它的時候完全沒有聲音。
    test_tmp_base_env()
    test_smoke()
    test_search()
    test_subagent_inline()
    test_day_divider()
    test_compact_marker()
    test_codex_step_badges()
    test_session_kind_tags()
    test_codex_survival_report()
    test_cache_report()
    test_review_detection()
    test_cache_report_by_kind()
    test_classify_cache_causes()
    test_cold_cause_badges()
    test_api_miss_reason()
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
