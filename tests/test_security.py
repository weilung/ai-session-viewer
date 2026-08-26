#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全與增量建置回歸測試；可直接用 python tests/test_security.py 執行。"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_session_viewer as viewer  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
# ⚠ 共用 `test_smoke.new_tmp`，**不拄第二份**——拄一份的代價已經付過一次了
# （探針拄了 `block_is_renderable` 而且拄漏兩件事）。那支的 docstring 寫著為什麼不能直接用 mkdtemp。
from test_smoke import new_tmp  # noqa: E402

SCRIPT = ROOT / "ai_session_viewer.py"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _write_session(projects: Path, project: str, sid: str, title: str):
    proj = projects / project
    proj.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "user",
        "uuid": sid + "-u",
        "timestamp": "2026-06-01T01:00:00.000Z",
        "cwd": f"/tmp/{project}",
        "sessionId": sid,
        "message": {"role": "user", "content": title},
    }
    (proj / f"{sid}.jsonl").write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")


def _write_codex_session(path: Path):
    events = [
        {"timestamp": "2026-06-02T01:00:00.000Z", "type": "session_meta",
         "payload": {"id": "codex-session-1", "timestamp": "2026-06-02T01:00:00.000Z",
                     "cwd": "/tmp/codex-proj", "cli_version": "0.139.0",
                     "git": {"branch": "main"}}},
        {"timestamp": "2026-06-02T01:00:01.000Z", "type": "turn_context",
         "payload": {"turn_id": "turn-1", "cwd": "/tmp/codex-proj", "model": "gpt-5"}},
        {"timestamp": "2026-06-02T01:00:02.000Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "Codex hello"}},
        {"timestamp": "2026-06-02T01:00:03.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Codex answer"}]}},
        {"timestamp": "2026-06-02T01:00:04.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell_command",
                     "arguments": "{\"command\":\"pwd\"}", "call_id": "call-1"}},
        {"timestamp": "2026-06-02T01:00:05.000Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call-1", "output": "/tmp/codex-proj"}},
        {"timestamp": "2026-06-02T01:00:05.500Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "input": "*** Begin Patch\n*** End Patch", "call_id": "call-2"}},
        {"timestamp": "2026-06-02T01:00:05.800Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "call-2", "output": "Success"}},
        {"timestamp": "2026-06-02T01:00:06.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                  "output_tokens": 20, "reasoning_output_tokens": 5,
                                  "total_tokens": 120}}}},
        {"timestamp": "2026-06-02T01:00:06.500Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                  "output_tokens": 20, "reasoning_output_tokens": 5,
                                  "total_tokens": 120}}}},
        {"timestamp": "2026-06-02T01:00:06.800Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 30, "cached_input_tokens": 10,
                                  "output_tokens": 5, "reasoning_output_tokens": 1,
                                  "total_tokens": 35}}}},
        {"timestamp": "2026-06-02T01:00:07.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                  "output_tokens": 20, "reasoning_output_tokens": 5,
                                  "total_tokens": 120}}}},
        {"timestamp": "2026-06-02T01:00:08.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Second Codex answer"}]}},
        {"timestamp": "2026-06-02T01:00:09.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                  "output_tokens": 20, "reasoning_output_tokens": 5,
                                  "total_tokens": 120}}}},
    ]
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")


def test_md_link_href_is_attribute_escaped():
    rendered = viewer.md_to_html('[x](https://example.test"onclick="alert)')
    assert 'href="https://example.test&quot;onclick=&quot;alert"' in rendered
    assert 'href="https://example.test"onclick' not in rendered


def test_image_data_uri_is_validated():
    valid = viewer.render_image_block({
        "source": {"type": "base64", "media_type": "image/png", "data": PNG}
    })
    assert '<img class="msg-img"' in valid
    assert "data:image/png;base64" in valid

    injected = viewer.render_image_block({
        "source": {"type": "base64", "media_type": "image/png", "data": 'abc" onerror="alert(1)'}
    })
    assert "<img" not in injected
    assert "onerror" not in injected

    svg = base64.b64encode(b"<svg></svg>").decode("ascii")
    unsupported = viewer.render_image_block({
        "source": {"type": "base64", "media_type": "image/svg+xml", "data": svg}
    })
    assert "<img" not in unsupported
    assert "格式不支援" in unsupported


def test_missing_text_fields_do_not_crash_grouping():
    assert viewer.extract_user_text(
        {"message": {"content": [{"type": "text", "text": 123}]}}
    ) == "123"
    turns = viewer.group_turns([
        {"type": "user", "message": {"content": [{"type": "text", "text": None}]}, "_dt": None, "_i": 0},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": None}]}, "_dt": None, "_i": 1},
    ])
    assert turns == []


def test_usage_counts_cache_tokens_once_per_message_id():
    s = viewer.Session(Path("demo.jsonl"), "demo")
    s.events = [
        {"type": "assistant", "message": {"id": "m1", "model": "claude-opus",
         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                   "cache_read_input_tokens": 30, "output_tokens": 5}}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-opus",
         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                   "cache_read_input_tokens": 30, "output_tokens": 5}}},
        {"type": "assistant", "message": {"id": "m2", "model": "claude-sonnet",
         "usage": {"inputTokens": "7", "cacheReadInputTokens": "8", "outputTokens": "9"}}},
    ]
    viewer._collect_usage(s)
    assert s.models == ["claude-opus", "claude-sonnet"]
    assert s.usage == {"input": 17, "cache_create": 20, "cache_read": 38, "output": 14, "total_in": 75}
    assert s.tok_out == 14
    assert s.ctx_peak == 60          # 單輪最大 input+cache = 10+20+30
    assert s.cache_pct == 51         # round(100*38/75)
    assert s.cost_partial is False
    # opus:(10*5 + 20*5*1.25 + 30*5*0.1 + 5*25); sonnet:(7*3 + 8*3*0.1 + 9*15)，再除以 1e6
    assert abs(s.cost - (315 + 158.4) / 1_000_000) < 1e-12


def test_unknown_model_cost_is_partial():
    s = viewer.Session(Path("demo.jsonl"), "demo")
    s.events = [
        {"type": "assistant", "message": {"id": "x1", "model": "some-other-llm",
         "usage": {"input_tokens": 100, "output_tokens": 50}}},
    ]
    viewer._collect_usage(s)
    assert s.cost == 0.0
    assert s.cost_partial is True
    assert viewer.model_price("claude-opus-4-8") == (5.0, 25.0)   # 前綴 fallback
    assert viewer.model_price("gpt-5") is None


def _isolated_home_env(tmp):
    """子行程用的環境：HOME／USERPROFILE 指到空目錄。

    掃描起點取自 HOME（`~/.claude*`／`~/.codex`），不隔離的話測試讀到的是這台機器上
    真實且正在變動的資料，斷言就不再只取決於 fixture。"""
    home = Path(tmp) / "_home"
    home.mkdir(parents=True, exist_ok=True)
    return dict(os.environ, HOME=str(home), USERPROFILE=str(home))


def test_force_project_keeps_unfiltered_manifest_rows():
    tmp = new_tmp()
    projects = tmp / "projects"
    out = tmp / "out"
    _write_session(projects, "proj-a", "aaaaaaaa", "Alpha request")
    _write_session(projects, "proj-b", "bbbbbbbb", "Beta request")

    # ⚠ HOME 一律隔離：本工具會無條件列舉 ~/.claude* 與 ~/.codex，不隔離的話這裡會去掃
    # 這台機器上真實、且正在被寫入的資料——結果隨環境浮動（實測曾因此偶發失敗）。
    env = _isolated_home_env(tmp)
    subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={projects}", "--out", str(out)],
        check=True, capture_output=True, text=True, encoding="utf-8", env=env)
    subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"demo={projects}", "--out", str(out),
         "--project", "proj-a", "--force"],
        check=True, capture_output=True, text=True, encoding="utf-8", env=env)

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Alpha request" in index
    assert "Beta request" in index
    manifest = json.loads((out / viewer.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert len(manifest["entries"]) == 2
    assert all(k.startswith("claude-code:") for k in manifest["entries"])
    rows = [e["row"] for e in manifest["entries"].values()]
    assert all(r["source_kind"] == "claude-code" for r in rows)
    assert all(r["out_html"].startswith("claude-code/demo/") for r in rows)


def test_codex_source_renders_in_own_namespace():
    tmp = new_tmp()
    codex_file = tmp / "rollout-test.jsonl"
    out = tmp / "out"
    _write_codex_session(codex_file)

    subprocess.run(
        [sys.executable, str(SCRIPT), "--no-claude", "--codex-source", f"demo={codex_file}",
         "--out", str(out)],
        check=True, capture_output=True, text=True, encoding="utf-8",
        env=_isolated_home_env(tmp))

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Codex hello" in index
    assert "sessions/codex/demo/" in index
    htmls = list((out / "sessions" / "codex" / "demo").glob("*.html"))
    assert htmls
    html = htmls[0].read_text(encoding="utf-8")
    assert "Codex answer" in html
    assert "Second Codex answer" in html
    assert "Bash" in html
    assert "Patch" in html
    assert "*** Begin Patch\n*** End Patch" in html
    assert "*** Begin Patch\\n*** End Patch" not in html
    assert "快取 39%" in html
    assert "產出 45" in html
    assert "產出 65" not in html
    manifest = json.loads((out / viewer.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert all(k.startswith("codex:") for k in manifest["entries"])
    row = next(iter(manifest["entries"].values()))["row"]
    assert row["source_kind"] == "codex"
    assert row["proj"] == "codex-proj"


def test_no_codex_run_preserves_codex_outputs():
    tmp = new_tmp()
    home = tmp / "home"
    claude_projects = home / ".claude" / "projects"
    codex_dir = home / ".codex" / "sessions"
    out = tmp / "out"
    _write_session(claude_projects, "proj-a", "aaaaaaaa", "Alpha request")
    codex_dir.mkdir(parents=True, exist_ok=True)
    _write_codex_session(codex_dir / "rollout-test.jsonl")
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)

    # 預設 = 全自動：Claude + Codex 都會轉
    subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out)],
        check=True, capture_output=True, text=True, encoding="utf-8", env=env)
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "sessions/claude-code/default/" in index
    assert "sessions/codex/default/" in index
    codex_htmls = list((out / "sessions" / "codex").rglob("*.html"))
    assert codex_htmls

    # 只跑 Claude（--no-codex）屬「縮範圍」，不得誤刪既有 Codex 輸出
    subprocess.run(
        [sys.executable, str(SCRIPT), "--no-codex", "--out", str(out)],
        check=True, capture_output=True, text=True, encoding="utf-8", env=env)
    assert all(p.exists() for p in codex_htmls)
    manifest = json.loads((out / viewer.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert any(k.startswith("codex:") for k in manifest["entries"])


def test_collect_sources_dedupes_same_realpath():
    tmp = new_tmp()
    proj = tmp / "projects"
    proj.mkdir(parents=True, exist_ok=True)
    # 兩個來源指到同一真實路徑 → 應只留一個（涵蓋 junction/symlink 造成的重複內容）
    assert len(viewer.collect_sources([f"a={proj}", f"b={proj}"], "")) == 1
    # 進一步：symlink 指到同一真實資料夾也要被收斂（無法建 symlink 的平台則略過）
    link = tmp / "link_projects"
    try:
        link.symlink_to(proj, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    assert len(viewer.collect_sources([f"real={proj}", f"sym={link}"], "")) == 1


def test_command_and_notify_are_escaped():
    """指令輸出／通知原文／指令名／插話——這批新開的四個注入面都要逸出。

    ⚠⚠ 這一格是 `utf-fam` Medium 補的：逸出**當時就是對的**，
    但**沒有任何斷言在守**。「現在是對的」和「壞了會被抓到」是兩件事。

    ⚠ 這四個來源都不是使用者打的字，很容易被當成「自己人」而漏掉逸出：
      · 指令輸出 ＝ 終端文字（`/status`、`/context` 的 stdout）
      · 通知原文 ＝ 系統注入的 XML
      · 指令名／參數 ＝ CLI 寫的
      · 插話 ＝ 走佇列、不是一般 user 事件
    """
    XSS = '</script><script>alert(1)</script><img src=x onerror=alert(2)>"'
    NTAG = "task-" + "notification"
    tmp = new_tmp()
    proj = tmp / "projects" / "p"
    proj.mkdir(parents=True, exist_ok=True)
    sid = "00000000-0000-4000-8000-0000000000xs".replace("x", "e")
    base = dict(cwd="/x/P", gitBranch="main", version="2.1.240", sessionId=sid)
    evs = [
        dict(type="user", uuid="u1", timestamp="2026-07-26T01:00:00.000Z",
             message={"role": "user", "content": "正常提問"}, **base),
        dict(type="assistant", uuid="a1", timestamp="2026-07-26T01:00:05.000Z",
             message={"role": "assistant", "model": "claude-opus-4-7", "id": "m1",
                      "usage": {"input_tokens": 5, "output_tokens": 5},
                      "content": [{"type": "text", "text": "回覆"}]}, **base),
        # ① 指令名／參數
        dict(type="user", uuid="u2", timestamp="2026-07-26T01:00:10.000Z",
             message={"role": "user", "content":
                      f"<command-name>/{XSS}</command-name>\n"
                      f"<command-args>{XSS}</command-args>"}, **base),
        # ② 指令輸出（同一時刻的下一則）
        dict(type="user", uuid="u3", timestamp="2026-07-26T01:00:10.000Z",
             message={"role": "user", "content":
                      f"<local-command-stdout>{XSS}</local-command-stdout>"}, **base),
        # ③ 通知原文（裸的，會自成一列）
        dict(type="user", uuid="u4", timestamp="2026-07-26T01:00:20.000Z",
             message={"role": "user", "content":
                      f"<{NTAG}>\n<task-id>{XSS}</task-id>\n"
                      f"<summary>{XSS}</summary>\n<status>completed</status>\n"
                      f"</{NTAG}>"}, **base),
        # ④ 中途插話（走佇列）
        dict(type="attachment", uuid="u5", timestamp="2026-07-26T01:00:06.000Z",
             attachment={"type": "queued_command", "prompt": XSS,
                         "commandMode": "prompt", "origin": {"kind": "human"},
                         "timestamp": "2026-07-26T01:00:06.000Z"}, **base),
        dict(type="queue-operation", operation="enqueue",
             timestamp="2026-07-26T01:00:06.000Z", sessionId=sid, content=XSS),
        dict(type="queue-operation", operation="remove",
             timestamp="2026-07-26T01:00:07.000Z", sessionId=sid, content=XSS),
    ]
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
    out = tmp / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--claude-source", f"d={proj.parent}",
         "--no-codex", "--out", str(out), "--format", "html"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    page = [p for p in (out / "sessions").rglob("*.html")
            if p.parent.name != "sessions"][0]
    html = page.read_text(encoding="utf-8")
    # 這四個注入面都要在頁面上出現（不然這一格什麼都沒驗到），但一律是逸出後的形式
    # ⚠ 涵蓋率先驗：素材真的走到頁面上了嗎？沒有的話下面每一格都是空的。
    assert html.count("alert(1)") >= 4, (
        f"四個注入面沒有都走到頁面上（只出現 {html.count('alert(1)')} 次）"
        "——這一格什麼都沒驗到")
    # ⚠⚠ **要驗「危險的形狀」，不是「危險的字面」。**
    # 逸出之後 `onerror=alert(2)` 這幾個字**照樣會出現**（`&lt;img src=x onerror=alert(2)&gt;`），
    # 那是無害的文字。第一版斷言寫成「字面不可以出現」，於是**逸出正確也會紅**。
    assert "<script>alert(1)</script>" not in html, "裸的 <script> 進到頁面了"
    assert "<img src=x onerror=" not in html, "裸的 <img onerror= 進到頁面了"
    assert "</script><script>" not in html, "可以截斷內嵌 <script> 區段"
    # 反面：逸出後的形式必須在（證明上面三格不是因為素材消失才通過）
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, (
        "逸出後的形式不見了——素材被整段丟掉的話，上面三格會是假綠")
    print("OK: command/notify/interject escaping test passed")


def main():
    test_command_and_notify_are_escaped()
    test_md_link_href_is_attribute_escaped()
    test_image_data_uri_is_validated()
    test_missing_text_fields_do_not_crash_grouping()
    test_usage_counts_cache_tokens_once_per_message_id()
    test_unknown_model_cost_is_partial()
    test_force_project_keeps_unfiltered_manifest_rows()
    test_codex_source_renders_in_own_namespace()
    test_no_codex_run_preserves_codex_outputs()
    test_collect_sources_dedupes_same_realpath()
    print("OK: security test passed")


if __name__ == "__main__":
    main()
