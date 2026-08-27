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
