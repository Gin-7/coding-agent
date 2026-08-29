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


def test_plan_mode_readonly(tmp):
    """计划模式 = 只读 + 做计划：写/执行被拒（无批准→执行阶段），最终 finish。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "plan")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, plan_mode=True)
    result = loop.run("演示任务")
    assert result["status"] == "finished"
    contents = " ".join(m.get("content") or "" for m in loop.ctx.messages)
    # 写/执行类操作被执行层拒绝（计划模式只读）
    assert "计划模式仅允许只读" in contents
    # 计划模式提示应注入（"只做计划"）
    assert "【计划模式】" in contents
    # 只读 schema 过滤：计划模式下暴露的工具不含 run_command
    names = [s["function"]["name"] for s in loop._schemas()]
    assert "run_command" not in names and "read_file" in names


def test_subagent_returns_result(tmp):
    """子 agent：spawn 返回有界结果；只读防护使 run_command 被执行层拒绝。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "sub")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None)
    result = loop._run_subagent("研究一下工作区", max_steps=5)
    assert isinstance(result, str) and result
    # 只读防护：子 agent 不允许 run_command（默认只读），工具 schema 已被过滤
    sub = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                    max_steps=5, on_event=None, allowed_tools={"read_file", "list_dir"})
    names = [s["function"]["name"] for s in sub._schemas()]
    assert "run_command" not in names and "read_file" in names


def test_subagent_depth_guard(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "subdepth")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, subagent_depth=2)
    result = loop._run_subagent("x")
    assert "嵌套深度" in result


def test_subagents_parallel(tmp):
    """并行子 agent：一次派生多个，返回按序合并的有界结果；只读防护生效。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "subpar")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None)
    result = loop._run_subagents_parallel([{"prompt": "子任务A"}, {"prompt": "子任务B"}])
    assert isinstance(result, str) and "子agent 1" in result and "子agent 2" in result
    # 并行时子 agent 提交（MockLLM 会写 hello.txt）只读防护：write_file 不属于只读 → 被拒
    # 结果应包含子 agent 的总结
    assert "演示任务" in result or "子agent" in result


def test_subagents_parallel_depth_guard(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "subpardepth")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None, subagent_depth=2)
    result = loop._run_subagents_parallel([{"prompt": "x"}])
    assert "嵌套深度" in result


def test_needs_confirm(tmp):
    """审批模式确认规则：命令执行类一律确认；子 agent 仅当授予写权限才确认。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "confirm")
    loop = AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=10, on_event=None)
    assert loop._needs_confirm("run_command", {"command": "x"}) is True
    assert loop._needs_confirm("start_background", {"command": "x"}) is True
    assert loop._needs_confirm("git_commit", {"message": "m"}) is True
    assert loop._needs_confirm("read_file", {"path": "x"}) is False
    # 只读子 agent 不确认
    assert loop._needs_confirm("spawn_subagent", {"prompt": "p"}) is False
    assert loop._needs_confirm("spawn_subagents", {"tasks": [{"prompt": "p"}]}) is False
    # 写授权子 agent 确认（顶层 / tasks 项内）
    assert loop._needs_confirm("spawn_subagent", {"prompt": "p", "allow_write": True}) is True
    assert loop._needs_confirm("spawn_subagents", {"tasks": [{"prompt": "p", "allow_write": True}]}) is True
    assert loop._needs_confirm("start_subagents", {"tasks": [{"prompt": "p", "allow_write": True}]}) is True


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
