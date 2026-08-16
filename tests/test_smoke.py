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
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ai_session_viewer.py"

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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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

    htmls = list((out / "sessions").rglob("*.html"))
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    assert list((out3 / "sessions").rglob("*.html")), "過期 .html 應仍在磁碟上（前提）"
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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

    html = [p for p in (out / "sessions").rglob("*.html")][0].read_text(encoding="utf-8")
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
    rows = index.count('<tr data-source')
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    html = [p for p in (out / "sessions").rglob("*.html")][0].read_text(encoding="utf-8")
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    html = [p for p in (out / "sessions").rglob("*.html")][0].read_text(encoding="utf-8")
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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

    htmls = list((out / "sessions").rglob("*.html"))
    assert htmls, "應產生 Codex session HTML"
    html = htmls[0].read_text(encoding="utf-8")
    assert "CODEXQMARK" in html and "CODEXDONE" in html, "對話內容應照常呈現"
    # 三步都應有步驟分隔列；步驟1 冷啟紅標；❄最低徽章指向步驟1
    assert "步驟 1</span>" in html and "步驟 3</span>" in html, "多步回合應有逐步分隔列"
    assert '步驟 1</span><span class="meter m-cache cold"' in html, "冷啟步驟應紅標（m-cache cold）"
    assert "❄最低 0%·步驟1" in html, "回合徽章應標出最低步驟"
    # 總帳：孤兒丟棄、重播去重 → total_in=6700、cache_read=4400 → 66%
    #（孤兒未丟會成 61%；重播重計會成 74%）
    assert "⚡快取 66%" in html, "session 頁命中率應為 66%（孤兒丟棄＋重播去重）"
    assert "gpt-5.5" in html, "應顯示模型"
    print("OK: codex step badges test passed")


def test_session_kind_tags(tmp_path=None):
    # session 型態自動分類：review（首句 # Review）／exec（codex_exec 無頭）／一般；
    # 索引有型態下拉與標題徽章、row 帶 data-kind、md twin 有型態標記、session 頁有 chip。
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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

    htmls = {p.name: p.read_text(encoding="utf-8") for p in (out / "sessions").rglob("*.html")}
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    html = list((out / "sessions").rglob("*.html"))[0].read_text(encoding="utf-8")

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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    html = list((out / "sessions").rglob("*.html"))[0].read_text(encoding="utf-8")
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
        "hide-gap hide-dur hide-eff": "新增欄位應預設隱藏",
        "%)": "脈絡徽章應附佔 context 視窗的 %",
    }.items():
        assert needle in html, f"{msg}（找不到 {needle!r}）"
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
    mp, mi, mc, mcold = v.coldest_step(g)
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

    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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
    html = list((out / "sessions").rglob("*.html"))[0].read_text(encoding="utf-8")
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
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
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

    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-source", f"demo={tmp / 'sessions'}",
         "--no-claude", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"非零退出\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"

    pages = {p.name: p.read_text(encoding="utf-8") for p in (out / "sessions").rglob("*.html")}
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

    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "新格式問句NEWFMTQ" in idx, "索引標題應取到新格式的首句，而非 fallback 成 (無對話)"
    assert f"(無對話) {sid_a[:8]}" not in idx, "有 prompt 的 session 不應被標成 (無對話)"
    print("OK: codex item_completed user message test passed")


def test_account_switch_cause(tmp_path=None):
    # 自願切帳號（沒撞 limit）：transcript 裡沒有 429/401，只能靠各帳號自己的 history.jsonl 認出。
    # 這種步在修正前會被歸成 evict（伺服器側異常），實為人因可避免 → 應歸 acct 並進索引的人因浪費。
    import ai_session_viewer as v

    sid = "019f0100-0000-7000-8000-000000000abc"
    cfg_a = Path(tmp_path or tempfile.mkdtemp()) / "cfgA"
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
    # 沒有任何浪費時不該擺一個永遠篩不出東西的勾選框
    row0 = dict(row, waste_n=0, waste_usd=0.0)
    assert 'id="fw"' not in v.render_index_html([row0]), "全無浪費時不應出現篩選勾選框"
    print("OK: account switch cause test passed")


if __name__ == "__main__":
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
