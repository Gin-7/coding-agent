"""主循环测试（mock 端到端 / 配对 / 审批 / 规划模式）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_common import make_ws, run_tests


def test_mock_loop_end_to_end(tmp):
    from agent.context import Context
    from agent.events import FinishEvent, event_to_dict
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.session import Session
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "loop")
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
    """历史中所有 assistant tool_calls 都须有对应 tool 结果（否则下一轮被 API 拒）。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "pairing")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None)
    assert loop.run("演示任务")["status"] == "finished"
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


def test_permission_deny(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "perm")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, confirm=lambda name, desc: False)
    result = loop.run("演示任务")
    assert result["status"] == "finished"
    assert any("用户拒绝" in (m.get("content") or "") for m in loop.ctx.messages)


def test_plan_mode_approved(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "plan_ok")
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
    ws = make_ws(tmp, "plan_no")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, confirm=lambda name, desc: False, plan_mode=True)
    result = loop.run("演示任务")
    assert result["status"] == "cancelled"


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
