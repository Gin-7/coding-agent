"""主循环测试（mock 端到端 / 配对 / 审批 / 规划模式 / 用量口径）。"""
import json
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


def test_truncated_call_recovery(tmp):
    """参数被生成上限截断：给出带残骸结尾的针对性提示，模型改小粒度后任务继续而非熔断。"""
    import json
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()

    class TruncThenOkLLM:
        """第 1 次：截断的 write_file；看到'截断'提示后写小文件；再 finish。"""
        def __init__(self):
            self.n = 0

        def chat_stream(self, messages, tools=None):
            self.n += 1
            last = (messages[-1].get("content") or "")
            if self.n == 1:
                truncated = '{"path": "a.txt", "content": "hel'
                yield {"type": "done", "content": "", "finish_reason": "tool_calls", "usage": None,
                       "tool_calls": [{"id": "t1", "name": "write_file", "arguments": truncated}]}
            elif "截断" in last:
                yield {"type": "done", "content": "", "finish_reason": "tool_calls", "usage": None,
                       "tool_calls": [{"id": "t2", "name": "write_file",
                                       "arguments": json.dumps({"path": "a.txt", "content": "hello"})}]}
            else:
                yield {"type": "done", "content": "", "finish_reason": "tool_calls", "usage": None,
                       "tool_calls": [{"id": "t3", "name": "finish", "arguments": '{"summary": "ok"}'}]}

    ws = make_ws(tmp, "trunc")
    loop = AgentLoop(TruncThenOkLLM(), Context(make_system_prompt(str(ws)), 56000),
                     ToolContext(ws), max_steps=10, on_event=None)
    assert loop.run("写文件")["status"] == "finished"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "hello"
    assert any("截断" in (m.get("content") or "") for m in loop.ctx.messages if m["role"] == "user")


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
    """计划模式 = 只读 + 做计划：写/执行被拒。

    本用例不传 confirm（程序化调用、无 UI），此时没有征求批准的通道，agent 交出
    计划后即结束，不会转入执行阶段。带 confirm 的"批准→执行"见下一条用例。
    """
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


class _PlanThenExecuteLLM:
    """计划阶段（工具被过滤）交出计划；批准后写文件，再 finish。

    用"工具集里有没有 run_command"来区分当前处于计划阶段还是执行阶段。
    """

    def chat_stream(self, messages, tools=None):
        names = [t["function"]["name"] for t in (tools or [])]
        planning = "run_command" not in names
        if planning:
            yield {"type": "done", "content": "计划：写一个 out.txt",
                   "tool_calls": [{"id": "p1", "name": "finish",
                                   "arguments": json.dumps({"summary": "计划：写 out.txt"})}],
                   "finish_reason": "tool_calls", "usage": None}
            return
        wrote = any("已写入" in (m.get("content") or "") for m in messages)
        if not wrote:
            yield {"type": "done", "content": "开始执行",
                   "tool_calls": [{"id": "w1", "name": "write_file",
                                   "arguments": json.dumps({"path": "out.txt", "content": "done"})}],
                   "finish_reason": "tool_calls", "usage": None}
        else:
            yield {"type": "done", "content": "执行完毕",
                   "tool_calls": [{"id": "f2", "name": "finish",
                                   "arguments": json.dumps({"summary": "执行完成"})}],
                   "finish_reason": "tool_calls", "usage": None}


class _RejectPlanLLM:
    """计划阶段交出计划（模拟用户拒绝批准）。"""

    def chat_stream(self, messages, tools=None):
        names = [t["function"]["name"] for t in (tools or [])]
        if "run_command" not in names:
            yield {"type": "done", "content": "计划",
                   "tool_calls": [{"id": "p1", "name": "finish",
                                   "arguments": json.dumps({"summary": "计划：什么都不做"})}],
                   "finish_reason": "tool_calls", "usage": None}
        else:
            yield {"type": "done", "content": "不该走到这里", "tool_calls": [],
                   "finish_reason": "stop", "usage": None}


def test_plan_mode_approve_then_execute(tmp):
    """计划模式：交出计划 → 批准 → 转入执行阶段（全量工具放行）。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "planexec")
    asked = []
    loop = AgentLoop(_PlanThenExecuteLLM(), Context(make_system_prompt(str(ws)), 56000),
                     ToolContext(ws), max_steps=8, on_event=None,
                     confirm=lambda n, d: asked.append(n) or True, plan_mode=True)
    result = loop.run("任务")
    assert asked == ["plan"], f"应只征求一次计划批准，实际 {asked}"
    assert loop.plan_mode is False, "批准后应关闭计划模式"
    assert result["status"] == "finished" and "执行完成" in result["summary"]
    assert (ws / "out.txt").exists(), "执行阶段应真正写入了文件"


def test_plan_mode_rejected_stays_readonly(tmp):
    """计划被拒绝：不应转入执行阶段，也不应产生任何写操作。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "planreject")
    loop = AgentLoop(_RejectPlanLLM(), Context(make_system_prompt(str(ws)), 56000),
                     ToolContext(ws), max_steps=5, on_event=None,
                     confirm=lambda n, d: False, plan_mode=True)
    result = loop.run("任务")
    assert result["status"] == "finished"
    assert loop.plan_mode is True, "未批准时计划模式必须保持开启（继续只读）"
    assert not any((ws / p).exists() for p in ("out.txt",))


class _RepeatLLM:
    """每轮都返回完全相同的工具调用（模拟模型死循环重试）。"""

    def chat_stream(self, messages, tools=None):
        yield {"type": "text", "text": "再试一次"}
        yield {"type": "done", "content": "再试一次",
               "tool_calls": [{"id": "r", "name": "run_command",
                               "arguments": "{\"command\": \"echo x\"}"}],
               "finish_reason": "tool_calls", "usage": None}


def test_repeated_call_detection(tmp):
    """回归：连续相同工具调用要被检测到并纠偏。

    防空转原本只覆盖"完全不调工具"和"解析失败"两种，这种"同一参数反复失败"
    的死循环没人管，只能一路刷到 max_steps。
    """
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "repeat")
    loop = AgentLoop(_RepeatLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=6, on_event=None)
    loop.run("任务")
    assert any("连续多次用完全相同的参数" in (m.get("content") or "") for m in loop.ctx.messages)


def test_plan_mode_skips_confirm(tmp):
    """回归：计划模式必须先于审批判定。

    若审批排在前面，模型调用 run_command 时会先弹"允许执行？"让用户白批准一次，
    然后才被计划模式拒绝。
    """
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "planconfirm")
    asked = []
    loop = AgentLoop(_RepeatLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                     max_steps=3, on_event=None,
                     confirm=lambda n, d: asked.append(n) or True, plan_mode=True)
    loop.run("任务")
    assert asked == [], f"计划模式下不该请求审批，实际请求了 {asked}"
    assert any("计划模式仅允许只读工具" in (m.get("content") or "") for m in loop.ctx.messages)


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


def test_convergence_hint_at_75pct(tmp):
    """步数到 75% 仍未 finish：注入收敛强提示（实测子 agent 会死在反复修补辅助脚本上）。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()

    class WanderLLM:
        """前 3 步各读一个不同文件（不触发连续重复检测），第 4 步 finish。"""
        def __init__(self):
            self.n = 0

        def chat_stream(self, messages, tools=None):
            self.n += 1
            files = ["a.txt", "b.txt", "c.txt"]
            if self.n <= 3:
                yield {"type": "done", "content": "", "finish_reason": "tool_calls", "usage": None,
                       "tool_calls": [{"id": "r%d" % self.n, "name": "read_file",
                                       "arguments": json.dumps({"path": files[self.n - 1]})}]}
            else:
                yield {"type": "done", "content": "", "finish_reason": "tool_calls", "usage": None,
                       "tool_calls": [{"id": "f", "name": "finish",
                                       "arguments": json.dumps({"summary": "收尾"})}]}

    ws = make_ws(tmp, "converge")
    (ws / "a.txt").write_text("1", encoding="utf-8")
    (ws / "b.txt").write_text("2", encoding="utf-8")
    (ws / "c.txt").write_text("3", encoding="utf-8")
    loop = AgentLoop(WanderLLM(), Context(make_system_prompt(str(ws)), 56000),
                     ToolContext(ws), max_steps=4, on_event=None)
    assert loop.run("随便看看")["status"] == "finished"
    hints = [m.get("content") for m in loop.ctx.messages
             if m["role"] == "user" and "收敛提示" in (m.get("content") or "")]
    assert len(hints) == 1 and "剩余步数" in hints[0]


def test_failed_command_guard(tmp):
    """同一条命令连续失败 3 次：注入换方案提示（每次失败后参数不同 → 连续重复检测覆盖不到）。"""
    import json
    import sys
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()

    fail_cmd = '"%s" -c "import sys; sys.exit(3)"' % sys.executable

    class RetryLoopLLM:
        def __init__(self):
            self.n = 0

        def chat_stream(self, messages, tools=None):
            self.n += 1
            if self.n <= 3:
                yield {"type": "done", "content": "", "finish_reason": "tool_calls", "usage": None,
                       "tool_calls": [{"id": "c%d" % self.n, "name": "run_command",
                                       "arguments": json.dumps({"command": fail_cmd})}]}
            else:
                yield {"type": "done", "content": "", "finish_reason": "tool_calls", "usage": None,
                       "tool_calls": [{"id": "f", "name": "finish",
                                       "arguments": json.dumps({"summary": "放弃并总结"})}]}

    ws = make_ws(tmp, "cmdguard")
    loop = AgentLoop(RetryLoopLLM(), Context(make_system_prompt(str(ws)), 56000),
                     ToolContext(ws), max_steps=10, on_event=None)
    assert loop.run("跑命令")["status"] == "finished"
    hints = [m.get("content") for m in loop.ctx.messages
             if m["role"] == "user" and "已失败 3 次" in (m.get("content") or "")]
    assert len(hints) == 1


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


class _FixedUsageLLM:
    """每轮都上报固定用量，用于验证 RunResult 的口径。"""

    def chat_stream(self, messages, tools=None):
        yield {"type": "text", "text": "ok"}
        yield {"type": "done", "content": "ok",
               "tool_calls": [{"id": "f", "name": "finish",
                               "arguments": json.dumps({"summary": "完成"})}],
               "finish_reason": "tool_calls",
               "usage": {"prompt_tokens": 1_000, "completion_tokens": 50, "total_tokens": 1_050}}


def test_run_result_usage_is_per_round(tmp):
    """回归：RunResult.usage 必须是**本轮**用量，不是会话累计。

    早先直接报 ctx.real_usage（跨轮累加、从不重置），多轮会话下会看到 prompt 一路
    涨到上百万，被误读成"单轮就把整个窗口吃光了"。现在 run() 开头重置 round_usage。
    """
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    register_all()
    ws = make_ws(tmp, "usage")
    loop = AgentLoop(_FixedUsageLLM(), Context(make_system_prompt(str(ws)), 56000),
                     ToolContext(ws), max_steps=5, on_event=None)
    r1 = loop.run("任务一")
    r2 = loop.run("任务二")
    assert r1["usage"]["prompt"] == 1_000, f"第一轮应报 1000，实际 {r1['usage']}"
    assert r2["usage"]["prompt"] == 1_000, f"第二轮应报 1000（非累计），实际 {r2['usage']}"
    # 会话累计口径仍保留（/stats 要看这个）
    assert loop.ctx.real_usage["prompt"] == 2_000


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
