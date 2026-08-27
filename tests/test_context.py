"""上下文管理与压缩测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_common import run_tests


def test_estimate_tokens(tmp):
    from agent.context import estimate_tokens
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("你好世界" * 100) > estimate_tokens("你好世界")


def test_trim_rounds_keeps_pairing(tmp):
    from agent.context import Context
    ctx = Context("sys", 1_000_000)
    ctx.add({"role": "user", "content": "task"})
    for r in range(2):
        ctx.add({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"a{r}_1", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": f"a{r}_2", "type": "function", "function": {"name": "y", "arguments": "{}"}},
        ]})
        ctx.add({"role": "tool", "tool_call_id": f"a{r}_1", "content": "out1"})
        ctx.add({"role": "tool", "tool_call_id": f"a{r}_2", "content": "out2"})
    removed = ctx.trim_old_tool_rounds()
    assert removed == 1
    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    assert len(ctx.messages[2]["tool_calls"]) == 2
    assert ctx.messages[3]["tool_call_id"] == "a1_1"


def test_trim_to_budget_returns_count(tmp):
    from agent.context import Context
    ctx = Context("sys", 100)
    ctx.add({"role": "user", "content": "task"})
    for r in range(3):
        ctx.add({"role": "assistant", "content": "x" * 200, "tool_calls": [
            {"id": f"a{r}_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
        ctx.add({"role": "tool", "tool_call_id": f"a{r}_1", "content": "y" * 200})
    removed = ctx.trim_to_budget()
    assert removed >= 1
    assert ctx.estimated_tokens() <= 100 or removed >= 2


def test_hard_truncate(tmp):
    from agent.context import Context
    ctx = Context("sys", 1_000_000)
    ctx.add({"role": "user", "content": "task"})
    for r in range(3):
        ctx.add({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{r}", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
        ctx.add({"role": "tool", "tool_call_id": f"c{r}", "content": "out"})
    removed = ctx.hard_truncate(keep_recent_rounds=1)
    assert removed == 4
    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "user", "user", "assistant", "tool"]


class _StubLLM:
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
    assert removed == 4
    roles = [m["role"] for m in ctx.messages]
    assert roles == ["system", "user", "user", "assistant", "tool"]
    assert "摘要内容" in ctx.messages[2]["content"]
    assert ctx.messages[3]["tool_calls"][0]["id"] == "c2"
    assert len(stub.calls) == 1


def test_compaction_chunked_and_merged(tmp):
    from agent.compaction import compact_history
    ctx = _ctx_with_rounds(20, result_size=5000)
    stub = _StubLLM(content="chunk summary")
    removed = compact_history(ctx, stub, 2000, keep_recent_rounds=1, chunk_ratio=0.4, min_region_tokens=0)
    assert removed == 38
    assert len(stub.calls) >= 3
    assert "【早期对话摘要】" in ctx.messages[2]["content"]
    assert ctx.messages[-2]["tool_calls"][0]["id"] == "c19"


def test_compaction_failure_falls_back(tmp):
    from agent.compaction import compact_history

    class _FailLLM:
        def chat(self, messages, tools=None):
            raise RuntimeError("api down")

    ctx = _ctx_with_rounds(3)
    assert compact_history(ctx, _FailLLM(), 100_000, keep_recent_rounds=1, min_region_tokens=0) == 0
    removed = ctx.hard_truncate(keep_recent_rounds=1)
    assert removed == 4


def test_compaction_small_region_skipped(tmp):
    from agent.compaction import compact_history
    ctx = _ctx_with_rounds(3)
    stub = _StubLLM()
    removed = compact_history(ctx, stub, 100_000, keep_recent_rounds=1, min_region_tokens=10_000)
    assert removed == 0
    assert len(stub.calls) == 0


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
