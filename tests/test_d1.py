"""D1 冒烟测试：python tests/test_d1.py（无需 pytest，纯标准库断言）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试临时目录放在项目内（系统 Temp 在沙箱下不可写），已加入 .gitignore
TMP_ROOT = ROOT / ".test-tmp"


def _ws(tmp: str, name: str) -> Path:
    p = Path(tmp) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------- context ----------

def test_estimate_tokens(tmp):
    from agent.context import estimate_tokens
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("你好世界" * 100) > estimate_tokens("你好世界")


def test_trim_rounds_keeps_pairing(tmp):
    from agent.context import Context
    ctx = Context("sys", 1_000_000)
    ctx.add({"role": "user", "content": "task"})
    # 两轮工具调用，每轮 assistant tool_calls + 2 个 tool 结果
    for r in range(2):
        ctx.add({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"a{r}_1", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": f"a{r}_2", "type": "function", "function": {"name": "y", "arguments": "{}"}},
        ]})
        ctx.add({"role": "tool", "tool_call_id": f"a{r}_1", "content": "out1"})
        ctx.add({"role": "tool", "tool_call_id": f"a{r}_2", "content": "out2"})
    removed = ctx.trim_old_tool_rounds()
    assert removed == 1
    # 剩余消息中：任何 assistant 带 tool_calls 的消息，其 tool_calls 数量必须与后续 tool 消息一一配对
    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    asst = ctx.messages[2]
    assert len(asst["tool_calls"]) == 2
    assert ctx.messages[3]["tool_call_id"] == "a1_1"  # 保留的是第二轮


def test_trim_to_budget_returns_count(tmp):
    from agent.context import Context
    ctx = Context("sys", 100)  # 极小预算
    ctx.add({"role": "user", "content": "task"})
    for r in range(3):
        ctx.add({"role": "assistant", "content": "x" * 200, "tool_calls": [
            {"id": f"a{r}_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
        ctx.add({"role": "tool", "tool_call_id": f"a{r}_1", "content": "y" * 200})
    removed = ctx.trim_to_budget()
    assert removed >= 1
    assert ctx.estimated_tokens() <= 100 or removed >= 2


# ---------- parser ----------

def test_parse_tool_calls_ok(tmp):
    from agent.parser import parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    calls = parse_tool_calls(
        [{"id": "c1", "name": "read_file", "arguments": json.dumps({"path": "a.txt"})}], TOOLS)
    assert calls[0].name == "read_file" and calls[0].arguments == {"path": "a.txt"}


def test_parse_tool_calls_unknown(tmp):
    from agent.parser import ParseError, parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    try:
        parse_tool_calls([{"id": "c1", "name": "nope", "arguments": "{}"}], TOOLS)
        raise AssertionError("应抛出 ParseError")
    except ParseError:
        pass


def test_parse_tool_calls_missing_required(tmp):
    from agent.parser import ParseError, parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    try:
        parse_tool_calls([{"id": "c1", "name": "run_command", "arguments": "{}"}], TOOLS)
        raise AssertionError("应抛出 ParseError")
    except ParseError:
        pass


def test_text_protocol(tmp):
    from agent.parser import parse_text_protocol
    from agent.tools import register_all, TOOLS
    register_all()
    text = '我先看看。\n<tool_call>{"name": "read_file", "arguments": {"path": "x"}}</tool_call>\n完了。'
    rest, calls = parse_text_protocol(text, TOOLS)
    assert len(calls) == 1 and calls[0].name == "read_file"
    assert "我先看看" in rest and "完了" in rest


# ---------- tools ----------

def test_file_roundtrip(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "file")
    ctx = ToolContext(ws)
    r = dispatch("write_file", {"path": "hello.txt", "content": "hi\n"}, ctx)
    assert r["ok"]
    r = dispatch("read_file", {"path": "hello.txt"}, ctx)
    assert r["ok"] and "hi" in r["output"]
    # 分页：offset=40, limit=10 → 显示第 40-49 行（L39..L48）
    r = dispatch("write_file", {"path": "many.txt", "content": "\n".join(f"L{i}" for i in range(50))}, ctx)
    assert r["ok"]
    r = dispatch("read_file", {"path": "many.txt", "offset": 40, "limit": 10}, ctx)
    assert r["ok"] and "L48" in r["output"] and "L38" not in r["output"]


def test_path_escape_blocked(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "escape")
    ctx = ToolContext(ws)
    r = dispatch("write_file", {"path": "../evil.txt", "content": "x"}, ctx)
    assert not r["ok"] and "越界" in r["output"]


def test_blacklist(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "blacklist")
    ctx = ToolContext(ws)
    r = dispatch("run_command", {"command": "del /f /s q.txt"}, ctx)
    assert not r["ok"] and "拦截" in r["output"]


def test_run_command_ok(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "cmd")
    ctx = ToolContext(ws)
    r = dispatch("run_command", {"command": "echo hello-agent"}, ctx)
    assert r["ok"] and "hello-agent" in r["output"]


# ---------- 主循环端到端（mock） ----------

def test_mock_loop_end_to_end(tmp):
    from agent.context import Context
    from agent.events import FinishEvent, event_to_dict
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.session import Session
    from agent.tools import ToolContext, register_all
    register_all()

    ws = _ws(tmp, "loop")
    events = []
    with Session(ws / "sessions") as s:
        loop = AgentLoop(
            MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
            max_steps=10, on_event=lambda ev: (events.append(ev), s.log(event_to_dict(ev))),
        )
        result = loop.run("演示任务")

    assert result["status"] == "finished"
    assert (ws / "hello.txt").exists()
    assert any(isinstance(ev, FinishEvent) for ev in events)
    logs = list((ws / "sessions").glob("*.jsonl"))
    assert logs and logs[0].stat().st_size > 0


def test_finish_preserves_pairing(tmp):
    """mock 循环跑完后，历史中所有 assistant tool_calls 都必须有对应 tool 结果，
    否则 REPL 下一轮请求会被 API 以配对错误拒绝（曾因 finish 直接 return 触发）。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = _ws(tmp, "pairing")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None)
    result = loop.run("演示任务")
    assert result["status"] == "finished"
    msgs = loop.ctx.messages
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("tool_calls"):
            n = len(m["tool_calls"])
            following = msgs[i + 1:i + 1 + n]
            assert len(following) == n, "tool_calls 后缺少配对 tool 结果"
            for j, f in enumerate(following):
                assert f["role"] == "tool" and f.get("tool_call_id") == m["tool_calls"][j]["id"]
            i += 1 + n
        else:
            i += 1


def test_edit_file_roundtrip(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "edit")
    ctx = ToolContext(ws)
    r = dispatch("write_file", {"path": "e.txt", "content": "hello world\nhello agent\n"}, ctx)
    assert r["ok"]
    r = dispatch("edit_file", {"path": "e.txt", "old": "hello world", "new": "hello python"}, ctx)
    assert r["ok"] and "已修改" in r["output"]
    r = dispatch("read_file", {"path": "e.txt"}, ctx)
    assert r["ok"] and "hello python" in r["output"] and "hello agent" in r["output"]


def test_edit_file_ambiguous_and_missing(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "edit2")
    ctx = ToolContext(ws)
    r = dispatch("write_file", {"path": "e.txt", "content": "x\nx\ny\n"}, ctx)
    assert r["ok"]
    # 出现 2 次 → 拒绝
    r = dispatch("edit_file", {"path": "e.txt", "old": "x", "new": "z"}, ctx)
    assert "不唯一" in r["output"]
    # 不存在 → 提示
    r = dispatch("edit_file", {"path": "e.txt", "old": "nope", "new": "z"}, ctx)
    assert "未找到" in r["output"]


def test_edit_file_crlf_compat(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "edit3")
    ctx = ToolContext(ws)
    (ws / "crlf.txt").write_bytes(b"line1\r\nline2\r\n")
    r = dispatch("edit_file", {"path": "crlf.txt", "old": "line1", "new": "changed"}, ctx)
    assert r["ok"] and "已修改" in r["output"]
    assert (ws / "crlf.txt").read_bytes() == b"changed\r\nline2\r\n"


def test_list_dir(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "listdir")
    (ws / "sub").mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    (ws / "b.py").write_text("y", encoding="utf-8")
    ctx = ToolContext(ws)
    r = dispatch("list_dir", {"path": "."}, ctx)
    assert r["ok"] and "a.txt" in r["output"] and "sub/" in r["output"]


def test_search(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "search")
    (ws / "one.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    (ws / "two.txt").write_text("HELLO world\n", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "junk.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    ctx = ToolContext(ws)
    r = dispatch("search", {"pattern": "hello"}, ctx)
    assert r["ok"] and "one.py:1" in r["output"] and "two.txt:1" in r["output"]
    # 默认跳过 __pycache__（大小写不敏感匹配 HELLO → two.txt 命中）
    assert "junk.py" not in r["output"]
    assert "2 处匹配" in r["output"]
    # 正则模式
    r = dispatch("search", {"pattern": r"def \w+\(", "regex": True}, ctx)
    assert r["ok"] and "one.py:1" in r["output"]
    # 无匹配
    r = dispatch("search", {"pattern": "notexist"}, ctx)
    assert r["ok"] and "未找到" in r["output"]


def test_credentials_protected(tmp):
    """.env 系列凭据文件：read/write/edit 全部拒绝，search 跳过。"""
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "creds")
    (ws / ".env").write_text("SECRET_KEY=abc123\n", encoding="utf-8")
    ctx = ToolContext(ws)
    r = dispatch("read_file", {"path": ".env"}, ctx)
    assert not r["ok"] and "保护" in r["output"]
    r = dispatch("write_file", {"path": ".env", "content": "x"}, ctx)
    assert not r["ok"] and "保护" in r["output"]
    r = dispatch("edit_file", {"path": ".env", "old": "x", "new": "y"}, ctx)
    assert not r["ok"] and "保护" in r["output"]
    r = dispatch("search", {"pattern": "SECRET_KEY"}, ctx)
    assert r["ok"] and "未找到" in r["output"]


class _StubLLM:
    """非流式 LLM 桩：记录调用，返回固定内容。"""

    def __init__(self, content="压缩后的摘要内容"):
        self.content = content
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        return {"content": self.content, "tool_calls": [], "finish_reason": "stop", "usage": None}


def _ctx_with_rounds(n: int, result_size: int = 10):
    from agent.context import Context
    ctx = Context("sys", 1_000_000)
    ctx.add({"role": "user", "content": "task"})
    for r in range(n):
        ctx.add({"role": "assistant", "content": f"round {r}", "tool_calls": [
            {"id": f"c{r}", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
        ctx.add({"role": "tool", "tool_call_id": f"c{r}", "content": "r" * result_size})
    return ctx


def test_compaction_single_chunk(tmp):
    from agent.compaction import compact_history
    ctx = _ctx_with_rounds(3)
    stub = _StubLLM(content="摘要内容")
    removed = compact_history(ctx, stub, 100_000, keep_recent_rounds=1, min_region_tokens=0)
    assert removed == 4  # 2 轮（4 条消息）被替换
    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "user", "user", "assistant", "tool"]
    assert "摘要内容" in ctx.messages[2]["content"]
    assert ctx.messages[3]["tool_calls"][0]["id"] == "c2"  # 最近 1 轮保留
    assert len(stub.calls) == 1  # 单块只需一次压缩调用


def test_compaction_chunked_and_merged(tmp):
    from agent.compaction import compact_history
    # 20 轮（格式化后每轮约 86 tokens）→ 区域远超块上限 1000 → 必然分块 + 合并
    ctx = _ctx_with_rounds(20, result_size=5000)
    stub = _StubLLM(content="chunk summary")
    removed = compact_history(ctx, stub, 2000, keep_recent_rounds=1, chunk_ratio=0.4, min_region_tokens=0)
    assert removed == 38  # 19 轮（38 条消息）被替换
    assert len(stub.calls) >= 3  # 至少 2 次块摘要 + 1 次合并
    assert "【早期对话摘要】" in ctx.messages[2]["content"]
    assert ctx.messages[-2]["tool_calls"][0]["id"] == "c19"  # 最近 1 轮保留


def test_compaction_failure_falls_back(tmp):
    from agent.compaction import compact_history

    class _FailLLM:
        def chat(self, messages, tools=None):
            raise RuntimeError("api down")

    ctx = _ctx_with_rounds(3)
    assert compact_history(ctx, _FailLLM(), 100_000, keep_recent_rounds=1, min_region_tokens=0) == 0
    # 调用方兜底：硬截断仍可工作
    removed = ctx.hard_truncate(keep_recent_rounds=1)
    assert removed == 4
    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "user", "user", "assistant", "tool"]


def test_compaction_small_region_skipped(tmp):
    from agent.compaction import compact_history
    ctx = _ctx_with_rounds(3)
    stub = _StubLLM()
    removed = compact_history(ctx, stub, 100_000, keep_recent_rounds=1, min_region_tokens=10_000)
    assert removed == 0  # 区域太小，跳过压缩（不浪费调用）
    assert len(stub.calls) == 0


def test_parse_tool_calls_type_error(tmp):
    from agent.parser import ParseError, parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    try:
        parse_tool_calls([{"id": "c1", "name": "run_command",
                           "arguments": json.dumps({"command": 123})}], TOOLS)
        raise AssertionError("应抛出 ParseError")
    except ParseError as e:
        assert "string" in str(e)


def test_glob_tool(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "glob")
    (ws / "one.py").write_text("x", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "two.py").write_text("y", encoding="utf-8")
    (ws / "sub" / "note.txt").write_text("z", encoding="utf-8")
    ctx = ToolContext(ws)
    r = dispatch("glob", {"pattern": "**/*.py"}, ctx)
    assert r["ok"] and "one.py" in r["output"] and "sub/two.py" in r["output"]
    r = dispatch("glob", {"pattern": "**/*.txt"}, ctx)
    assert r["ok"] and "sub/note.txt" in r["output"]


def test_git_tools(tmp):
    import subprocess
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "git")
    subprocess.run(["git", "init", "-q"], cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=True)
    ctx = ToolContext(ws)
    r = dispatch("git_status", {}, ctx)
    assert r["ok"] and "No commits" in r["output"]
    (ws / "a.txt").write_text("hi", encoding="utf-8")
    r = dispatch("git_status", {}, ctx)
    assert r["ok"] and "a.txt" in r["output"]
    r = dispatch("git_commit", {"message": "init"}, ctx)
    assert r["ok"] and "1 file changed" in r["output"]
    r = dispatch("git_log", {"n": 5}, ctx)
    assert r["ok"] and "init" in r["output"]


def test_run_command_streaming(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "stream")
    collected = []
    ctx = ToolContext(ws, on_output=lambda text: collected.append(text))
    r = dispatch("run_command", {"command": "echo stream-hello"}, ctx)
    assert r["ok"] and "stream-hello" in r["output"]
    assert any("stream-hello" in c for c in collected)


def test_permission_deny(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = _ws(tmp, "perm")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, confirm=lambda name, desc: False)
    result = loop.run("演示任务")
    # 用户拒绝 run_command 后，mock 未收到 python 输出 → 直接 finish
    assert result["status"] == "finished"
    denied = [m for m in loop.ctx.messages if "用户拒绝" in (m.get("content") or "")]
    assert denied, "应存在被拒绝的工具结果"


def test_resume_roundtrip(tmp):
    from agent.session import Session, load_messages
    ws = _ws(tmp, "resume")
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "out"}]
    with Session(ws / "sessions") as s:
        s.log({"type": "MessagesDump", "messages": msgs})
    loaded = load_messages(list((ws / "sessions").glob("*.jsonl"))[0])
    assert loaded == msgs


def test_undo_file(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = _ws(tmp, "undo")
    ctx = ToolContext(ws)
    r = dispatch("write_file", {"path": "u.txt", "content": "version 1\n"}, ctx)
    assert r["ok"]
    r = dispatch("edit_file", {"path": "u.txt", "old": "version 1", "new": "version 2"}, ctx)
    assert r["ok"]
    r = dispatch("read_file", {"path": "u.txt"}, ctx)
    assert r["ok"] and "version 2" in r["output"]
    # 撤销 → 回到 version 1
    r = dispatch("undo_file", {"path": "u.txt"}, ctx)
    assert r["ok"] and "已撤销" in r["output"]
    r = dispatch("read_file", {"path": "u.txt"}, ctx)
    assert r["ok"] and "version 1" in r["output"]
    # 再撤销 → 没有备份了
    r = dispatch("undo_file", {"path": "u.txt"}, ctx)
    assert "没有可撤销" in r["output"]


def test_plan_mode_approved(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = _ws(tmp, "plan_ok")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, confirm=lambda name, desc: True, plan_mode=True)
    result = loop.run("演示任务")
    assert result["status"] == "finished"
    contents = " ".join(m.get("content") or "" for m in loop.ctx.messages)
    assert "计划已批准" in contents


def test_plan_mode_rejected(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = _ws(tmp, "plan_no")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, confirm=lambda name, desc: False, plan_mode=True)
    result = loop.run("演示任务")
    assert result["status"] == "cancelled"


def test_web_server_endpoints(tmp):
    """Web 服务器：静态页、工具列表、运行任务、SSE 事件流、审批。"""
    import json as _json
    import threading
    import urllib.request

    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    from agent.web import create_server, WebAgentServer
    register_all()

    ws = _ws(tmp, "web")
    confirm_calls = []

    def factory(on_event, hub, web):
        loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                         max_steps=10, on_event=on_event, confirm=lambda n, d: (confirm_calls.append(n), True)[1])
        return loop

    httpd, web = create_server(ws, factory, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # 静态页
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            assert r.status == 200 and b"Coding Agent" in r.read()
        # 工具列表
        with urllib.request.urlopen(base + "/api/tools", timeout=10) as r:
            tools = _json.loads(r.read())
        assert "read_file" in tools and "git_commit" in tools
        # 先连 SSE（订阅事件流），再运行任务，确保不丢早期事件
        sse = urllib.request.urlopen(base + "/api/events", timeout=30)
        import time
        req = urllib.request.Request(base + "/api/run", data=_json.dumps(
            {"task": "演示任务"}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["ok"]
        events = []
        try:
            deadline = 20
            start = time.time()
            while time.time() - start < deadline:
                line = sse.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("data: "):
                    ev = _json.loads(line[6:])
                    events.append(ev)
                    if ev["type"] == "RunResult":
                        break
        finally:
            sse.close()
        types = [e["type"] for e in events]
        assert "UserMessage" in types and "ToolCallEvent" in types and "RunResult" in types
        result = events[-1]
        assert result["status"] == "finished"
        # 文件列表
        with urllib.request.urlopen(base + "/api/files?path=.", timeout=10) as r:
            files = _json.loads(r.read())
        assert "path" in files
    finally:
        httpd.shutdown()
        httpd.server_close()
        web.session.close()


def main() -> int:
    import shutil
    tmp = TMP_ROOT / "run"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn(tmp)
            passed += 1
            print(f"PASS {name}")
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
