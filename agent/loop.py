"""主循环（核心自研逻辑之一）：迭代执行，直至终止。

流程：组装上下文 → 调 LLM（流式）→ 解析输出 → 分派工具本地执行 → 结果写回历史 → 检查终止。
终止条件：finish 工具 / 达最大步数 / 用户中断 / API 重试耗尽 / 模型连续空转。
"""
import json
import re
import threading

from .background import BackgroundManager
from .compaction import compact_history
from .context import Context
from .events import (BackgroundOutput, BackgroundStarted, BackgroundStatus, CommandOutput,
                     CompactedEvent, ContextUsageEvent, ErrorEvent, FinishEvent, StepEvent,
                     SubagentEvent, SubagentResult, SubagentStarted, SubagentStatus, TextDelta,
                     ToolCallEvent, ToolResultEvent, TrimmedEvent)
from .events import event_to_dict
from .llm import LLMError
from .parser import ParseError, parse_tool_calls, parse_text_protocol
from .tools import TOOLS, ToolContext, dispatch, tool_schemas
from .tools.shell_tools import is_destructive

# 需要用户确认（审批模式）的工具：命令执行类一律确认
GUARDED_TOOLS = ("run_command", "git_commit", "start_background")

# 子 agent 派生类工具：仅当授予写权限（allow_write=true）时才需确认
SUBAGENT_SPAWN_TOOLS = ("spawn_subagent", "spawn_subagents", "start_subagents")

# 规划阶段 / 子 agent 默认只开放只读工具
READONLY_TOOLS = ("read_file", "list_dir", "search", "glob", "git_status", "git_diff")
PLAN_TOOLS = READONLY_TOOLS
MAX_SUBAGENT_DEPTH = 2     # 子 agent 嵌套深度上限（不能再 spawn 的防线）
MAX_SUBAGENT_STEPS = 30    # 子 agent 步数上限（审查/分析类任务需要读完大文件再产出，太紧会半途而废）
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
        self.plan_mode = plan_mode  # 计划模式：全程只读 + 做计划（无批准→执行）
        self.interrupt_event = interrupt_event  # 可选：threading.Event，Web UI 中断用
        self.allowed_tools = allowed_tools  # 可选：set/序列，仅允许这些工具（子 agent 只读）
        self.subagent_depth = subagent_depth  # 嵌套深度（防止子 agent 无限递归）
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
        self._sub_progress = {}  # 子 agent 运行态：subagent_id -> {batch_id,name,prompt,status,summary,events:[]}
        # 并行子 agent 线程会写 _sub_progress，而 list_subagents() 在 HTTP 线程遍历它，
        # 不加锁会撞上 "dictionary changed size during iteration"
        self._sub_lock = threading.Lock()
        self._finish_hinted = False  # 防空转提示只注入一次（见 _maybe_hint_finish）
        self._converge_hinted = False  # 步数 75% 处的收敛强提示（同上，每次 run 一次）
        self._cmd_fail = {}    # run_command 失败计数：同一条命令失败多次 → 注入换方案提示
        self._cmd_hinted = set()
        self._last_action_sig = None  # 上一轮工具调用签名（重复调用检测用）
        self._repeat_streak = 0

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

    def _run_subagent_inner(self, sub_id: str, batch_id: str, prompt: str,
                             max_steps: int, tools, allow_write: bool = False):
        """运行单个子 agent（同步）。返回 (status, summary[:2000])。

        每个子 agent 用**自己的 ToolContext**（共享工作区/后台管理），使各回调各归各、并行安全。
        allow_write=False（默认）：硬只读 —— 忽略 tools 里的写/执行工具，最多只收窄只读集合；
        allow_write=True：才允许 tools 指定的（或全部）工具。
        运行期间通过 on_event 包装器把逐事件上浮为 SubagentEvent，并在启动/结束时广播
        SubagentStarted / SubagentStatus（供 Web UI 实时展示“运行中”与对话式详情）。
        """
        steps = max(1, min(int(max_steps or 12), MAX_SUBAGENT_STEPS))
        if allow_write:
            allowed = set(tools) if tools else None   # None = 全部工具
        else:
            allowed = set(READONLY_TOOLS)             # 硬只读
            if tools:
                allowed = allowed & set(tools)        # tools 只能再收窄
        # 注册运行态 + 广播启动（加锁：并行子 agent 线程写、HTTP 线程读同一字典）
        with self._sub_lock:
            self._sub_progress[sub_id] = {"batch_id": batch_id, "name": prompt[:40],
                                          "prompt": prompt, "status": "running",
                                          "summary": "", "events": []}
        self._emit(SubagentStarted(sub_id, batch_id, prompt[:40], prompt))
        from .prompts import make_system_prompt
        sub_tool_ctx = ToolContext(self.tool_ctx.workspace)
        sub_tool_ctx.background = self.tool_ctx.background  # 共享后台管理
        sub_ctx = Context(make_system_prompt(self.tool_ctx.workspace), self.ctx.budget,
                          budget_resolver=getattr(self.ctx, "budget_resolver", None),
                          window_resolver=getattr(self.ctx, "window_resolver", None))
        sub_loop = AgentLoop(self.llm, sub_ctx, sub_tool_ctx, max_steps=steps,
                             on_event=self._make_sub_on_event(sub_id, batch_id), confirm=None,
                             plan_mode=False, interrupt_event=self.interrupt_event,
                             allowed_tools=allowed, subagent_depth=self.subagent_depth + 1)
        try:
            result = sub_loop.run(prompt)
        except Exception as e:  # noqa: BLE001 —— 子 agent 异常转为结果，不崩父循环
            result = {"status": "error", "message": f"{type(e).__name__}: {e}"}
        status = result.get("status")
        summary = result.get("summary") or result.get("message") or self._last_subagent_output(sub_ctx)
        summary = (summary or "")[:2000]
        with self._sub_lock:
            self._sub_progress[sub_id]["status"] = status
            self._sub_progress[sub_id]["summary"] = summary
        self._emit(SubagentStatus(sub_id, batch_id, status, summary))
        return status, summary

    def _make_sub_on_event(self, sub_id: str, batch_id: str):
        """包装子 agent 的逐事件：存入进度日志 + 上浮为 SubagentEvent。"""
        def cb(ev):
            d = ev if isinstance(ev, dict) else event_to_dict(ev)
            with self._sub_lock:
                entry = self._sub_progress.get(sub_id)
                if entry is not None and entry["status"] == "running":
                    entry["events"].append(d)
            self._emit(SubagentEvent(sub_id, batch_id, d))
        return cb

    def _run_subagent(self, prompt: str, max_steps: int = 12, tools=None, allow_write: bool = False) -> str:
        """运行一个独立的子 agent（同步），返回有界结果字符串。"""
        if self.subagent_depth >= self.MAX_SUBAGENT_DEPTH:
            return "达子 agent 嵌套深度上限，不再派生"
        sub_id = _next_subagent_id()
        status, summary = self._run_subagent_inner(sub_id, sub_id, prompt, max_steps, tools, allow_write)
        return summary

    def _run_subagents_parallel(self, tasks) -> str:
        """并行运行多个子 agent，返回合并的有界结果。tasks: [{prompt, max_steps?, tools?, allow_write?}]"""
        if self.subagent_depth >= self.MAX_SUBAGENT_DEPTH:
            return "达子 agent 嵌套深度上限，不再派生"
        if not tasks:
            return "任务列表为空"
        tasks = tasks[:MAX_PARALLEL_SUBAGENTS]
        results = [None] * len(tasks)
        self._batch_counter += 1
        batch_id = f"spawn-{self._batch_counter}"

        def worker(i, t):
            try:
                sub_id = _next_subagent_id()
                status, summary = self._run_subagent_inner(
                    sub_id, batch_id, t.get("prompt", ""), t.get("max_steps", 12),
                    t.get("tools"), bool(t.get("allow_write")))
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
                sub_id = _next_subagent_id()
                status, summary = self._run_subagent_inner(
                    sub_id, batch_id, t.get("prompt", ""), t.get("max_steps", 12),
                    t.get("tools"), bool(t.get("allow_write")))
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

    def list_subagents(self) -> list:
        """Web UI 用的子 agent 运行态快照（含逐事件明细，供详情面板回放对话）。

        先加锁取快照再遍历：并行子 agent 线程会往字典里插入新条目，直接遍历
        .items() 会撞 RuntimeError: dictionary changed size during iteration。
        """
        with self._sub_lock:
            items = list(self._sub_progress.items())
        return [
            {"subagent_id": sid, "batch_id": e["batch_id"], "name": e["name"],
             "prompt": e["prompt"], "status": e["status"], "summary": e["summary"],
             "events": e["events"]}
            for sid, e in items
        ]

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
                    "steps": step, "usage": dict(self.ctx.round_usage)}
        return None

    def _emit(self, ev) -> None:
        if self.on_event:
            self.on_event(ev)

    def _emit_context_usage(self) -> None:
        """广播当前上下文窗口使用率（环形指示器数据）；子 agent 也调用，其事件被包裹为
        SubagentEvent，不会污染主 agent 的环形指示器。"""
        self._emit(ContextUsageEvent(tokens=self.ctx.estimated_tokens(),
                                     budget=self.ctx._cur_budget(),
                                     window=self.ctx._cur_window() or 0))

    def _manage_context(self) -> None:
        """预算管理三层策略：compaction（保留语义）→ 裁剪（免费兜底）→ 硬截断（最后手段）。"""
        if not self.ctx.needs_trim():
            return
        # 1) compaction：把最近窗口之前的早期消息压缩为摘要（优先，保留语义）
        removed = compact_history(self.ctx, self.llm, self.ctx._cur_budget(), self.KEEP_RECENT_ROUNDS)
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
        """防空转：过半仍未 finish 时注入提示（每次 run 只注入一次）。

        判据用实例标记而非"看最后一条消息"：后者在模型继续调用工具后就失效了
        （末尾变成 tool 消息），会导致过半之后**每一步**都重复注入同一句提示，
        既刷屏又白烧 token。
        """
        if self._finish_hinted and self._converge_hinted:
            return
        if step > self.max_steps // 2 and not self._finish_hinted:
            self._finish_hinted = True
            self.ctx.add({"role": "user", "content": "（提示：若任务已完成，请调用 finish 工具结束任务，不要做多余操作。）"})
        if step >= int(self.max_steps * 0.75) and not self._converge_hinted:
            # 步数即将耗尽：推一把收敛。实测子 agent 会死在"反复修补自己的辅助脚本"上，
            # 到这一步还差得远就该止损总结，而不是继续开新调试。
            self._converge_hinted = True
            remain = self.max_steps - step
            self.ctx.add({"role": "user", "content":
                f"（收敛提示：剩余步数仅约 {remain} 步。请立即收敛：完成当前核心目标后调用 finish；"
                "尚未完成的子项在总结中说明，不要再展开新的调试、新的文件或新的方案；"
                "如果某个辅助脚本/检查难以修好，直接放弃它并在总结中注明。）"})

    def _append_tool_result(self, call, result: dict, text_protocol: bool) -> None:
        if text_protocol:
            # 文本协议路径：模型不认识 tool_call_id，用 user 消息回传结果（兼容任意模型）
            self.ctx.add({"role": "user", "content": "【工具执行结果】\n" + result["output"]})
        else:
            self.ctx.add({"role": "tool", "tool_call_id": call.call_id, "content": result["output"]})
        # 方案A：工具结果追加后立即查一次压缩，避免"读大文件"等单步爆冲要等到下一步开头才压
        self._manage_context()
        self._emit_context_usage()

    def _plan_pending(self) -> bool:
        """计划模式：全程只读 + 做计划（无批准→执行阶段）。"""
        return self.plan_mode

    def _needs_confirm(self, name: str, args: dict) -> bool:
        """审批模式下是否需要用户确认该工具调用。

        规则：命令执行类（GUARDED_TOOLS）一律确认；子 agent 派生类仅当授予写权限
        （allow_write=true，可能出现在顶层或 tasks 项里）时才确认——只读派发不必打扰。
        """
        if name in GUARDED_TOOLS:
            return True
        if name in SUBAGENT_SPAWN_TOOLS:
            if args.get("allow_write"):
                return True
            for t in args.get("tasks") or []:
                if t.get("allow_write"):
                    return True
        return False

    def run(self, task: str) -> dict:
        # 新的一轮：本轮用量清零（REPL/Web 复用同一个 ctx，会话累计值 real_usage 不动），
        # 这样 RunResult 报的是"这一轮花了多少"，而不是跨轮累加的总账。
        self.ctx.reset_round_usage()
        self.ctx.add({"role": "user", "content": task})
        self._repeat_streak = 0
        self._cmd_fail = {}
        self._cmd_hinted = set()
        if self.plan_mode:
            self.ctx.add({"role": "user",
                          "content": "【计划模式】请用只读工具探索现状，制定一份可执行的计划；本模式只做计划、不执行任何修改，完成后调用 finish 并把计划作为总结。"})
        text_only_streak = 0
        parse_fail_streak = 0  # 连续工具调用解析失败计数（C：防止模型反复生成超大/非法工具调用死循环）
        self._finish_hinted = False  # 多轮 REPL / Web 复用同一 loop，提示按轮重置
        self._last_action_sig = None
        self._repeat_streak = 0

        for step in range(1, self.max_steps + 1):
            r = self._interrupted(step)
            if r:
                return r
            self._emit(StepEvent(step, self.max_steps))
            self._maybe_hint_finish(step)
            self._manage_context()
            self._emit_context_usage()

            # 1. 调 LLM（流式）；按 allowed_tools / 规划阶段过滤暴露的工具
            content_parts, tool_calls_raw = [], []
            schemas = self._schemas()
            stop_streaming = False
            try:
                for ev in self.llm.chat_stream(self.ctx.messages, tools=schemas):
                    if ev["type"] == "text":
                        content_parts.append(ev["text"])
                        self._emit(TextDelta(ev["text"]))
                    elif ev["type"] == "done":
                        tool_calls_raw = ev.get("tool_calls") or []
                        if ev.get("usage"):
                            self.ctx.record_usage(ev["usage"])
                    # 流式过程中也检查中断：长回复时用户不必干等到这一步跑完才停。
                    # 此时本轮的 assistant 消息还没写回历史，直接返回即可，历史保持一致。
                    if self.interrupt_event is not None and self.interrupt_event.is_set():
                        stop_streaming = True
                        break
            except LLMError as e:
                self._emit(ErrorEvent(str(e)))
                return {"status": "error", "message": str(e),
                        "steps": step, "usage": dict(self.ctx.round_usage)}
            if stop_streaming:
                self._emit(ErrorEvent("执行被用户中断"))
                return {"status": "interrupted", "message": "用户中断",
                        "steps": step, "usage": dict(self.ctx.round_usage)}

            content = "".join(content_parts)

            # 2. 解析输出（原生 tool_calls / 文本协议兜底）
            text_protocol = False
            actions = []
            if tool_calls_raw:
                try:
                    actions = parse_tool_calls(tool_calls_raw, TOOLS)
                except ParseError as e:
                    parse_fail_streak += 1
                    if getattr(e, "truncated", False):
                        # 参数在生成上限处被截断：模型需要知道"残骸长什么样"和明确的拆分策略，
                        # 否则会原样重试同样的大调用，直到触发熔断（实测 qwen 商城生成两步即中止）
                        tail = (getattr(e, "raw_args", "") or "")[-160:].replace("\n", "\\n")
                        self.ctx.add({"role": "user", "content":
                            f"工具调用解析失败：参数在单次生成上限处被截断（{e}）。\n"
                            f"被截断参数的结尾：…{tail}\n"
                            "这不是 JSON 格式问题，而是单次工具调用的内容太长。请拆分："
                            "本次先 write_file 只写文件骨架（如 HTML 结构与 CSS，控制在 150 行内），"
                            "其余内容用 edit_file 以小段替换逐次追加，单次追加不超过 100 行。"})
                        self._emit(ErrorEvent(f"工具调用参数超限被截断；已要求拆分为骨架+分段追加重试"))
                        continue
                    if parse_fail_streak >= 2:
                        # 连续两次工具调用解析失败：模型大概率在尝试把整个大文件塞进单个
                        # 工具调用参数（被 max_tokens 截断 / JSON 非法）。主动纠正并终止，
                        # 避免无意义的死循环持续消耗 token（防空转不覆盖"有调用但解析失败"的情况）。
                        self.ctx.add({"role": "user", "content":
                            f"工具调用解析失败：{e}。你连续多次生成的工具调用参数过大或格式非法"
                            f"（很可能试图一次性整体重写大文件）。请改用 edit_file 做小段精确替换，"
                            f"或分段多次写入；单次工具调用的参数不要过大。若仍无法生成合法调用，请调用 finish 结束任务。"})
                        self._emit(ErrorEvent("模型连续多次工具调用解析失败，任务中止"))
                        return {"status": "stopped", "message": "模型工具调用持续解析失败",
                                "steps": step, "usage": dict(self.ctx.round_usage)}
                    self.ctx.add({"role": "user", "content":
                        f"工具调用解析失败：{e}。请改用 edit_file 做小段精确替换（不要整体重写大文件），"
                        f"单次工具调用的参数不要过大，并以合法 JSON 重新调用工具。"})
                    # 上屏：否则用户只看到 agent 卡住，不知道是模型输出非法、正在让它重试
                    self._emit(ErrorEvent(f"工具调用解析失败：{e}；已要求模型以合法 JSON 重试"))
                    continue
            else:
                parse_fail_streak = 0  # 文本路径（无原生工具调用）→ 非解析失败，重置计数
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
                    return {"status": "stopped", "message": "模型未继续行动",
                            "steps": step, "usage": dict(self.ctx.round_usage)}
                self.ctx.add({"role": "user", "content": "请继续：要么调用工具完成剩余步骤，要么调用 finish 结束任务。"})
                continue

            text_only_streak = 0
            parse_fail_streak = 0  # 成功产出工具调用/文本协议调用，解析失败计数归零

            # 4.5 重复调用检测：现有防空转只覆盖"完全不调工具"和"解析失败"，
            #     但"同一工具+同样参数反复失败"这种最常见的死循环没人管，只能刷到 max_steps。
            sig = tuple((a.name, json.dumps(a.arguments, sort_keys=True, ensure_ascii=False))
                        for a in actions)
            if sig == self._last_action_sig:
                self._repeat_streak += 1
            else:
                self._repeat_streak = 0
            self._last_action_sig = sig

            # 5. 分派执行。注意：finish 也按普通工具执行并回写结果——
            #    若直接 return，历史中会留下无 tool 结果配对的 assistant tool_calls，
            #    下一轮（REPL 多轮对话）请求会被 API 以配对错误拒绝。
            for a in actions:
                self._emit(ToolCallEvent(a.call_id, a.name, a.arguments))
                if a.name == "finish":
                    summary = a.arguments.get("summary", "任务完成")
                    if self._plan_pending():
                        # 计划模式：finish = 交出计划，等用户批准。
                        # 无 confirm 回调时（程序化调用 / 无 UI）退化为"只出计划"，行为同前。
                        approved = bool(self.confirm("plan", summary)) if self.confirm else False
                        if not approved:
                            result = {"ok": True, "output": "计划已提交（用户未批准执行）"}
                            self._emit(ToolResultEvent(a.call_id, a.name, True, result["output"]))
                            self._append_tool_result(a, result, text_protocol)
                            self._emit(FinishEvent(summary))
                            return {"status": "finished", "summary": summary,
                                    "steps": step, "usage": dict(self.ctx.round_usage)}
                        # 批准 → 关掉计划模式，下一轮起全量工具放开，按批准的计划执行
                        self.plan_mode = False
                        self._finish_hinted = False  # 执行阶段重新给一次 finish 提示的机会
                        result = {"ok": True, "output": "计划已批准，开始执行"}
                        self._emit(ToolResultEvent(a.call_id, a.name, True, result["output"]))
                        self._append_tool_result(a, result, text_protocol)
                        self.ctx.add({"role": "user", "content":
                            "【计划已批准】请严格按上面批准的计划执行；现在可以使用全部工具（含写入与命令执行）。"})
                        break
                    result = {"ok": True, "output": "任务完成"}
                    self._emit(ToolResultEvent(a.call_id, a.name, True, result["output"]))
                    self._append_tool_result(a, result, text_protocol)
                    self._emit(FinishEvent(summary))
                    return {"status": "finished", "summary": summary,
                            "steps": step, "usage": dict(self.ctx.round_usage)}
                # 计划模式必须先判：它禁止一切写/执行。若排在审批之后，会先弹一次
                # "允许执行 run_command？" 让用户白批准一次，然后才被计划模式拒掉。
                if self._plan_pending() and a.name not in PLAN_TOOLS:
                    result = {"ok": False,
                              "output": "计划模式仅允许只读工具（read_file/list_dir/search/glob/git_status/git_diff），只做计划、不执行修改"}
                    self._emit(ToolResultEvent(a.call_id, a.name, False, result["output"]))
                    self._append_tool_result(a, result, text_protocol)
                    continue
                if self.confirm and self._needs_confirm(a.name, a.arguments):
                    desc = json.dumps(a.arguments, ensure_ascii=False)
                    if not self.confirm(a.name, desc):
                        result = {"ok": False, "output": f"用户拒绝了该操作（{a.name}）"}
                        self._emit(ToolResultEvent(a.call_id, a.name, False, result["output"]))
                        self._append_tool_result(a, result, text_protocol)
                        continue
                # 破坏性删除命令：auto 模式无人可批准 → 拒绝并提示切批准模式；
                # ask 模式的 run_command 已在上方 confirm 网关经用户确认；plan 模式更早已被拦截
                if a.name == "run_command" and is_destructive(a.arguments.get("command", "")) \
                        and self.confirm is None:
                    result = {"ok": False,
                              "output": "安全策略：破坏性删除命令需要人工批准，请在设置面板将权限切换为“批准模式(ask)”后再执行"}
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
                if a.name == "run_command":
                    # 失败命令守卫：同一命令反复失败（典型：反复跑自己写的辅助脚本），
                    # 每次"修脚本"参数都不同，连续重复检测覆盖不到，需单独计数提示。
                    # 注意 run_command 对非零退出码也返回 ok=True（命令执行了≠成功），
                    # 失败要从输出头的"退出码 N"识别（异常时 ok=False 同样算失败）
                    out = result.get("output") or ""
                    m_rc = re.search(r"退出码 (\d+)", out[:40])
                    failed = (not result.get("ok", True)) or bool(m_rc and int(m_rc.group(1)) != 0)
                    if failed:
                        cmd = (a.arguments.get("command") or "").strip()
                        self._cmd_fail[cmd] = self._cmd_fail.get(cmd, 0) + 1
                        if self._cmd_fail[cmd] >= 3 and cmd not in self._cmd_hinted:
                            self._cmd_hinted.add(cmd)
                            self.ctx.add({"role": "user", "content":
                                f"（提示：同一条命令已失败 {self._cmd_fail[cmd]} 次。停止继续重试或修补它——"
                                "改用 read_file 直接查看相关文件定位根因，或换其他工具/方案完成任务；"
                                "若它是辅助脚本，可放弃并在 finish 总结中说明。）"})
                            self._emit(ErrorEvent(f"命令已连续失败 {self._cmd_fail[cmd]} 次，已注入换方案提示"))
                r = self._interrupted(step)
                if r:
                    return r

            # 连续两轮完全相同的调用 → 多半在死循环重试，主动纠偏（不终止，给模型改过的机会）
            if self._repeat_streak >= 2:
                self._repeat_streak = 0  # 提示一次后重新计数，避免刷屏
                self._emit(ErrorEvent("检测到连续重复的工具调用，已要求模型换方案"))
                self.ctx.add({"role": "user", "content":
                    "（提示：你已连续多次用完全相同的参数调用同一组工具，这通常说明该做法走不通。"
                    "请换一种方案（换命令、换路径、或先读取/搜索确认现状）；若任务已完成请调用 finish 结束。"
                    "特别地：若你在向文件写/追加内容，必须改用 write_file / edit_file 工具，"
                    "禁止继续用命令行转义写文件——cmd 会破坏 && | < > 等特殊字符。）"})

        self._emit(ErrorEvent(f"达到最大步数 {self.max_steps}，任务中止"))
        return {"status": "timeout", "message": f"达到最大步数 {self.max_steps}",
                "steps": self.max_steps, "usage": dict(self.ctx.round_usage)}
