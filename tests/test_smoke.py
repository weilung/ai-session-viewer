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
    print("OK: cold cause badges test passed")


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
    test_codex_ai_label()
