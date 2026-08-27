"""CLI 渲染器：订阅事件流，终端彩色输出。

Windows 关键点：启用 ANSI 转义（os.system('')）+ stdout 重配 UTF-8，
否则中文/emoji/颜色在 cmd 与重定向下会乱码。
"""
import json
import os
import sys

from .events import (CommandOutput, CompactedEvent, ErrorEvent, FinishEvent, StepEvent,
                     TextDelta, ToolCallEvent, ToolResultEvent, TrimmedEvent)

if os.name == "nt":
    os.system("")  # 启用 Windows 终端 VT 转义（ANSI 颜色）


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m"


class CliRenderer:
    def __init__(self):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    def emit(self, ev) -> None:
        if isinstance(ev, StepEvent):
            print(_c("33", f"\n── step {ev.step}/{ev.max_steps} ──"))
        elif isinstance(ev, TextDelta):
            print(ev.text, end="", flush=True)
        elif isinstance(ev, ToolCallEvent):
            args = json.dumps(ev.arguments, ensure_ascii=False)
            print(_c("36", f"\n🔧 {ev.name} {args}"))
        elif isinstance(ev, ToolResultEvent):
            shown = ev.output if len(ev.output) <= 200 else ev.output[:200] + "…"
            mark = "✓" if ev.ok else "✗"
            print(_c("90", f"    ↳ [{mark}] {shown}"))
        elif isinstance(ev, CommandOutput):
            print(_c("90", ev.text), end="", flush=True)
        elif isinstance(ev, ErrorEvent):
            print(_c("31", f"\n⚠ {ev.message}"))
        elif isinstance(ev, FinishEvent):
            print(_c("32", f"\n✅ {ev.summary}"))
        elif isinstance(ev, TrimmedEvent):
            print(_c("33", f"\n[上下文] 预算紧张，已裁剪最老的 {ev.rounds} 轮工具调用"))
        elif isinstance(ev, CompactedEvent):
            if ev.summarized:
                print(_c("33", f"\n[上下文] 已把早期 {ev.messages_removed} 条消息压缩为摘要"))
            else:
                print(_c("33", f"\n[上下文] 已丢弃早期 {ev.messages_removed} 条消息"))
