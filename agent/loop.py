"""主循环（核心自研逻辑之一）：迭代执行，直至终止。

流程：组装上下文 → 调 LLM（流式）→ 解析输出 → 分派工具本地执行 → 结果写回历史 → 检查终止。
终止条件：finish 工具 / 达最大步数 / 用户中断 / API 重试耗尽 / 模型连续空转。
"""
import json

from .context import Context
from .events import (ErrorEvent, FinishEvent, StepEvent, TextDelta, ToolCallEvent,
                     ToolResultEvent, TrimmedEvent)
from .llm import LLMError
from .parser import ParseError, parse_tool_calls, parse_text_protocol
from .tools import TOOLS, ToolContext, dispatch, tool_schemas


class AgentLoop:
    def __init__(self, llm, ctx: Context, tool_ctx: ToolContext, max_steps: int = 30, on_event=None):
        self.llm = llm
        self.ctx = ctx
        self.tool_ctx = tool_ctx
        self.max_steps = max_steps
        self.on_event = on_event

    def _emit(self, ev) -> None:
        if self.on_event:
            self.on_event(ev)

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

    def run(self, task: str) -> dict:
        self.ctx.add({"role": "user", "content": task})
        text_only_streak = 0

        for step in range(1, self.max_steps + 1):
            self._emit(StepEvent(step, self.max_steps))
            self._maybe_hint_finish(step)
            trimmed = self.ctx.trim_to_budget()
            if trimmed:
                self._emit(TrimmedEvent(trimmed))

            # 1. 调 LLM（流式）
            content_parts, tool_calls_raw = [], []
            try:
                for ev in self.llm.chat_stream(self.ctx.messages, tools=tool_schemas()):
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
                    return {"status": "finished", "summary": summary}
                result = dispatch(a.name, a.arguments, self.tool_ctx)
                self._emit(ToolResultEvent(a.call_id, a.name, result["ok"], result["output"]))
                self._append_tool_result(a, result, text_protocol)

        self._emit(ErrorEvent(f"达到最大步数 {self.max_steps}，任务中止"))
        return {"status": "timeout", "message": f"达到最大步数 {self.max_steps}"}
