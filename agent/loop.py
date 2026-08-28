"""主循环（核心自研逻辑之一）：迭代执行，直至终止。

流程：组装上下文 → 调 LLM（流式）→ 解析输出 → 分派工具本地执行 → 结果写回历史 → 检查终止。
终止条件：finish 工具 / 达最大步数 / 用户中断 / API 重试耗尽 / 模型连续空转。
"""
import json
import threading

from .background import BackgroundManager
from .compaction import compact_history
from .context import Context
from .events import (BackgroundOutput, BackgroundStarted, BackgroundStatus, CommandOutput,
                     CompactedEvent, ErrorEvent, FinishEvent, StepEvent, SubagentResult,
                     TextDelta, ToolCallEvent, ToolResultEvent, TrimmedEvent)
from .llm import LLMError
from .parser import ParseError, parse_tool_calls, parse_text_protocol
from .tools import TOOLS, ToolContext, dispatch, tool_schemas

# 需要用户确认（审批模式）的工具
GUARDED_TOOLS = ("run_command", "git_commit")

# 规划阶段 / 子 agent 默认只开放只读工具
READONLY_TOOLS = ("read_file", "list_dir", "search", "glob", "git_status", "git_diff")
PLAN_TOOLS = READONLY_TOOLS
MAX_SUBAGENT_DEPTH = 2     # 子 agent 嵌套深度上限（不能再 spawn 的防线）
MAX_SUBAGENT_STEPS = 20    # 子 agent 步数上限
MAX_PARALLEL_SUBAGENTS = 4  # 并行子 agent 上限

_subagent_counter = 0
_subagent_lock = threading.Lock()


def _next_subagent_id() -> str:
    global _subagent_counter
    with _subagent_lock:
        _subagent_counter += 1
        return f"sub-{_subagent_counter}"


class AgentLoop:
    KEEP_RECENT_ROUNDS = 2  # 预算管理时保留的最近工具调用轮数
    MAX_SUBAGENT_DEPTH = MAX_SUBAGENT_DEPTH

    def __init__(self, llm, ctx: Context, tool_ctx: ToolContext, max_steps: int = 30,
                 on_event=None, confirm=None, plan_mode: bool = False, interrupt_event=None,
                 allowed_tools=None, subagent_depth: int = 0):
        self.llm = llm
        self.ctx = ctx
        self.tool_ctx = tool_ctx
        self.max_steps = max_steps
        self.on_event = on_event
        self.confirm = confirm  # 可选：Callable[[tool_name, args_desc], bool]，审批模式回调
        self.plan_mode = plan_mode  # 先计划后行动：第一轮后展示计划征求批准
        self.interrupt_event = interrupt_event  # 可选：threading.Event，Web UI 中断用
        self.allowed_tools = allowed_tools  # 可选：set/序列，仅允许这些工具（子 agent 只读）
        self.subagent_depth = subagent_depth  # 嵌套深度（防止子 agent 无限递归）
        self._plan_approved = False
        self.tool_ctx.on_output = lambda text: self._emit(CommandOutput(text))
        # 后台任务管理器（挂到工具上下文，供后台工具访问）
        self.background = BackgroundManager()
        self.background.on_output = lambda tid, text: self._emit(BackgroundOutput(tid, text))
        self.background.emit = self._bg_emit
        self.tool_ctx.background = self.background
        self.tool_ctx.subagent = self._run_subagent
        self.tool_ctx.subagents = self._run_subagents_parallel
        self.tool_ctx.start_subagents = self._start_subagents
        self.tool_ctx.wait_subagents = self._wait_subagents
        self.tool_ctx.list_subagent_batches = self._list_subagent_batches
        self._sub_batches = {}   # 后台子 agent 批次：batch_id -> {threads, results, tasks}
        self._batch_counter = 0

    def _bg_emit(self, ev: dict):
        t = ev.get("type")
        if t == "BackgroundStarted":
            self._emit(BackgroundStarted(ev["task_id"], ev["command"], ev["pid"]))
        elif t == "BackgroundStatus":
            self._emit(BackgroundStatus(ev["task_id"], ev["status"], ev.get("exit_code")))

    def _schemas(self):
        """当前调用应暴露给模型的工具 schema。"""
        base = tool_schemas()
        if self.allowed_tools is not None:
            allowed = set(self.allowed_tools)
            base = [s for s in base if s["function"]["name"] in allowed]
        if self._plan_pending():
            base = [s for s in base if s["function"]["name"] in PLAN_TOOLS]
        return base

    def _run_subagent_inner(self, prompt: str, max_steps: int, tools, allow_write: bool = False):
        """运行单个子 agent（同步）。返回 (status, summary[:2000])。

        每个子 agent 用**自己的 ToolContext**（共享工作区/后台管理），使各回调各归各、并行安全。
        allow_write=False（默认）：硬只读 —— 忽略 tools 里的写/执行工具，最多只收窄只读集合；
        allow_write=True：才允许 tools 指定的（或全部）工具。
        """
        steps = max(1, min(int(max_steps or 8), MAX_SUBAGENT_STEPS))
        if allow_write:
            allowed = set(tools) if tools else None   # None = 全部工具
        else:
            allowed = set(READONLY_TOOLS)             # 硬只读
            if tools:
                allowed = allowed & set(tools)        # tools 只能再收窄
        from .prompts import make_system_prompt
        sub_tool_ctx = ToolContext(self.tool_ctx.workspace)
        sub_tool_ctx.background = self.tool_ctx.background  # 共享后台管理
        sub_ctx = Context(make_system_prompt(self.tool_ctx.workspace), self.ctx.budget)
        sub_loop = AgentLoop(self.llm, sub_ctx, sub_tool_ctx, max_steps=steps,
                             on_event=None, confirm=None, plan_mode=False,
                             interrupt_event=self.interrupt_event,
                             allowed_tools=allowed, subagent_depth=self.subagent_depth + 1)
        try:
            result = sub_loop.run(prompt)
        except Exception as e:  # noqa: BLE001 —— 子 agent 异常转为结果，不崩父循环
            result = {"status": "error", "message": f"{type(e).__name__}: {e}"}
        status = result.get("status")
        summary = result.get("summary") or result.get("message") or self._last_subagent_output(sub_ctx)
        return status, (summary or "")[:2000]

    def _run_subagent(self, prompt: str, max_steps: int = 8, tools=None, allow_write: bool = False) -> str:
        """运行一个独立的子 agent（同步），返回有界结果字符串。"""
        if self.subagent_depth >= self.MAX_SUBAGENT_DEPTH:
            return "达子 agent 嵌套深度上限，不再派生"
        status, summary = self._run_subagent_inner(prompt, max_steps, tools, allow_write)
        self._emit(SubagentResult(_next_subagent_id(), status, summary))
        return summary

    def _run_subagents_parallel(self, tasks) -> str:
        """并行运行多个子 agent，返回合并的有界结果。tasks: [{prompt, max_steps?, tools?, allow_write?}]"""
        if self.subagent_depth >= self.MAX_SUBAGENT_DEPTH:
            return "达子 agent 嵌套深度上限，不再派生"
        if not tasks:
            return "任务列表为空"
        tasks = tasks[:MAX_PARALLEL_SUBAGENTS]
        results = [None] * len(tasks)

        def worker(i, t):
            try:
                status, summary = self._run_subagent_inner(
                    t.get("prompt", ""), t.get("max_steps", 8), t.get("tools"), bool(t.get("allow_write")))
                self._emit(SubagentResult(_next_subagent_id(), status, summary))
                results[i] = summary
            except Exception as e:  # noqa: BLE001
                results[i] = f"子agent#{i + 1} 失败: {type(e).__name__}: {e}"

        threads = []
        for i, t in enumerate(tasks):
            th = threading.Thread(target=worker, args=(i, t), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        parts = [f"--- 子agent {i + 1} ---\n{results[i]}" for i in range(len(tasks)) if results[i]]
        if not parts:
            return "所有子agent均无结果"
        return "\n".join(parts)[:4000]

    # ---------- 后台子 agent（异步批次：主 agent 可先去做别的，稍后 wait 收结果） ----------

    def _start_subagents(self, tasks) -> str:
        """异步启动一组并行子 agent，返回批次 id（不阻塞主 agent）。"""
        if self.subagent_depth >= self.MAX_SUBAGENT_DEPTH:
            return "达子 agent 嵌套深度上限，不再派生"
        if not tasks:
            return "任务列表为空"
        tasks = tasks[:MAX_PARALLEL_SUBAGENTS]
        self._batch_counter += 1
        batch_id = f"sub-batch-{self._batch_counter}"
        results = [None] * len(tasks)
        threads = []

        def worker(i, t):
            try:
                status, summary = self._run_subagent_inner(
                    t.get("prompt", ""), t.get("max_steps", 8), t.get("tools"), bool(t.get("allow_write")))
                self._emit(SubagentResult(_next_subagent_id(), status, summary))
                results[i] = summary
            except Exception as e:  # noqa: BLE001
                results[i] = f"子agent#{i + 1} 失败: {type(e).__name__}: {e}"

        for i, t in enumerate(tasks):
            th = threading.Thread(target=worker, args=(i, t), daemon=True)
            th.start()
            threads.append(th)
        self._sub_batches[batch_id] = {"tasks": tasks, "results": results, "threads": threads}
        return batch_id

    def _wait_subagents(self, batch_id: str) -> str:
        """等待某后台批次完成并返回合并结果（阻塞）。"""
        batch = self._sub_batches.get(batch_id)
        if batch is None:
            return "子agent批次不存在"
        for th in batch["threads"]:
            th.join()
        parts = [f"--- 子agent {i + 1} ---\n{batch['results'][i]}"
                 for i in range(len(batch["tasks"])) if batch["results"][i]]
        return "\n".join(parts)[:4000] if parts else "该批次无结果"

    def _list_subagent_batches(self) -> str:
        if not self._sub_batches:
            return "没有进行中的子agent批次"
        lines = []
        for bid, b in self._sub_batches.items():
            running = sum(1 for th in b["threads"] if th.is_alive())
            lines.append(f"{bid} [{running}/{len(b['tasks'])} 运行中]")
        return "\n".join(lines)

    def _last_subagent_output(self, ctx: Context) -> str:
        """子 agent 未正常 finish 时，取最后一条 assistant 内容兜底。"""
        for m in reversed(ctx.messages):
            if m.get("role") == "assistant" and m.get("content"):
                return m["content"]
        return "子agent未产出结果"

    def _interrupted(self, step: int):
        """中断检查：Web UI 请求中断时在步骤边界生效。"""
        if self.interrupt_event is not None and self.interrupt_event.is_set():
            self._emit(ErrorEvent("执行被用户中断"))
            return {"status": "interrupted", "message": "用户中断",
                    "steps": step, "usage": dict(self.ctx.real_usage)}
        return None

    def _emit(self, ev) -> None:
        if self.on_event:
            self.on_event(ev)

    def _manage_context(self) -> None:
        """预算管理三层策略：compaction（保留语义）→ 裁剪（免费兜底）→ 硬截断（最后手段）。"""
        if not self.ctx.needs_trim():
            return
        # 1) compaction：把最近窗口之前的早期消息压缩为摘要（优先，保留语义）
        removed = compact_history(self.ctx, self.llm, self.ctx.budget, self.KEEP_RECENT_ROUNDS)
        if removed:
            self._emit(CompactedEvent(removed, summarized=True))
            if not self.ctx.needs_trim():
                return
        # 2) 裁剪兜底：整轮丢弃最老工具调用（免费，不消耗 LLM 调用）
        trimmed = self.ctx.trim_to_budget()
        if trimmed:
            self._emit(TrimmedEvent(trimmed))
            if not self.ctx.needs_trim():
                return
        # 3) 硬截断兜底：仅保留最近窗口
        removed = self.ctx.hard_truncate(self.KEEP_RECENT_ROUNDS)
        if removed:
            self._emit(CompactedEvent(removed, summarized=False))

    def _maybe_hint_finish(self, step: int) -> None:
        """防空转：过半仍未 finish 时注入提示（不重复注入）。"""
        if step <= self.max_steps // 2:
            return
        last = self.ctx.messages[-1] if self.ctx.messages else None
        if last and last.get("role") == "user" and "finish" in (last.get("content") or ""):
            return
        self.ctx.add({"role": "user", "content": "（提示：若任务已完成，请调用 finish 工具结束任务，不要做多余操作。）"})

    def _append_tool_result(self, call, result: dict, text_protocol: bool) -> None:
        if text_protocol:
            # 文本协议路径：模型不认识 tool_call_id，用 user 消息回传结果（兼容任意模型）
            self.ctx.add({"role": "user", "content": "【工具执行结果】\n" + result["output"]})
        else:
            self.ctx.add({"role": "tool", "tool_call_id": call.call_id, "content": result["output"]})

    def _plan_pending(self) -> bool:
        return self.plan_mode and not self._plan_approved

    def run(self, task: str) -> dict:
        self.ctx.add({"role": "user", "content": task})
        if self.plan_mode:
            self.ctx.add({"role": "user",
                          "content": "【规划阶段】请先了解现状并制定执行计划：输出计划文本（本阶段仅开放只读工具用于探索），不要执行写/命令类操作，也不要调用 finish。"})
        text_only_streak = 0

        for step in range(1, self.max_steps + 1):
            r = self._interrupted(step)
            if r:
                return r
            self._emit(StepEvent(step, self.max_steps))
            self._maybe_hint_finish(step)
            self._manage_context()

            # 1. 调 LLM（流式）；按 allowed_tools / 规划阶段过滤暴露的工具
            content_parts, tool_calls_raw = [], []
            schemas = self._schemas()
            try:
                for ev in self.llm.chat_stream(self.ctx.messages, tools=schemas):
                    if ev["type"] == "text":
                        content_parts.append(ev["text"])
                        self._emit(TextDelta(ev["text"]))
                    elif ev["type"] == "done":
                        tool_calls_raw = ev.get("tool_calls") or []
                        if ev.get("usage"):
                            self.ctx.record_usage(ev["usage"])
            except LLMError as e:
                self._emit(ErrorEvent(str(e)))
                return {"status": "error", "message": str(e)}

            content = "".join(content_parts)

            # 2. 解析输出（原生 tool_calls / 文本协议兜底）
            text_protocol = False
            actions = []
            if tool_calls_raw:
                try:
                    actions = parse_tool_calls(tool_calls_raw, TOOLS)
                except ParseError as e:
                    self.ctx.add({"role": "user", "content": f"工具调用解析失败：{e}。请重新以正确的格式调用工具。"})
                    continue
            else:
                rest, calls = parse_text_protocol(content, TOOLS)
                content = rest
                actions = calls
                text_protocol = bool(actions)

            # 3. 记录 assistant 消息（原生路径必须带 tool_calls 以配对 tool 结果）
            if actions and not text_protocol:
                self.ctx.add({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {"id": a.call_id, "type": "function",
                         "function": {"name": a.name, "arguments": json.dumps(a.arguments, ensure_ascii=False)}}
                        for a in actions
                    ],
                })
            else:
                self.ctx.add({"role": "assistant", "content": content})

            # 4. 无任何行动 → 空转保护
            if not actions:
                text_only_streak += 1
                if text_only_streak >= 2:
                    self._emit(ErrorEvent("模型连续两轮未调用工具，任务中止"))
                    return {"status": "stopped", "message": "模型未继续行动"}
                self.ctx.add({"role": "user", "content": "请继续：要么调用工具完成剩余步骤，要么调用 finish 结束任务。"})
                continue

            text_only_streak = 0

            # 5. 分派执行。注意：finish 也按普通工具执行并回写结果——
            #    若直接 return，历史中会留下无 tool 结果配对的 assistant tool_calls，
            #    下一轮（REPL 多轮对话）请求会被 API 以配对错误拒绝。
            for a in actions:
                self._emit(ToolCallEvent(a.call_id, a.name, a.arguments))
                if a.name == "finish":
                    result = {"ok": True, "output": "任务完成"}
                    self._emit(ToolResultEvent(a.call_id, a.name, True, result["output"]))
                    self._append_tool_result(a, result, text_protocol)
                    summary = a.arguments.get("summary", "任务完成")
                    self._emit(FinishEvent(summary))
                    return {"status": "finished", "summary": summary,
                            "steps": step, "usage": dict(self.ctx.real_usage)}
                if self.confirm and a.name in GUARDED_TOOLS:
                    desc = json.dumps(a.arguments, ensure_ascii=False)
                    if not self.confirm(a.name, desc):
                        result = {"ok": False, "output": f"用户拒绝了该操作（{a.name}）"}
                        self._emit(ToolResultEvent(a.call_id, a.name, False, result["output"]))
                        self._append_tool_result(a, result, text_protocol)
                        continue
                if self._plan_pending() and a.name not in PLAN_TOOLS:
                    # 规划阶段拒绝一切写/执行类操作（计划批准前只允许只读探索）
                    result = {"ok": False,
                              "output": "规划阶段仅允许只读工具（read_file/list_dir/search/glob/git_status/git_diff），请等待计划批准后再执行"}
                    self._emit(ToolResultEvent(a.call_id, a.name, False, result["output"]))
                    self._append_tool_result(a, result, text_protocol)
                    continue
                if self.allowed_tools is not None and a.name not in self.allowed_tools:
                    # 子 agent 只读防护：即便模型调用受限工具，也拒绝执行
                    result = {"ok": False, "output": f"当前上下文仅允许工具：{sorted(self.allowed_tools)}"}
                    self._emit(ToolResultEvent(a.call_id, a.name, False, result["output"]))
                    self._append_tool_result(a, result, text_protocol)
                    continue
                result = dispatch(a.name, a.arguments, self.tool_ctx)
                self._emit(ToolResultEvent(a.call_id, a.name, result["ok"], result["output"]))
                self._append_tool_result(a, result, text_protocol)
                r = self._interrupted(step)
                if r:
                    return r

            # 规划模式：第一轮后暂停，展示计划并征求用户批准
            if self.plan_mode and step == 1 and not self._plan_approved:
                plan_text = content.strip() or "（模型未输出计划文本）"
                ok = self.confirm("plan", plan_text) if self.confirm else True
                if not ok:
                    self.ctx.add({"role": "user", "content": "用户拒绝了计划，任务取消。"})
                    self._emit(ErrorEvent("计划未获批准，任务取消"))
                    return {"status": "cancelled", "message": "计划未获批准",
                            "steps": step, "usage": dict(self.ctx.real_usage)}
                self._plan_approved = True
                self.ctx.add({"role": "user", "content": "计划已批准，请按计划执行；完成后调用 finish。"})

        self._emit(ErrorEvent(f"达到最大步数 {self.max_steps}，任务中止"))
        return {"status": "timeout", "message": f"达到最大步数 {self.max_steps}", "steps": self.max_steps}
