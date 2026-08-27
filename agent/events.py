"""事件流数据结构：主循环是事件源，CLI 渲染器 / 会话日志是订阅者。

核心架构决策：UI 只是事件的订阅者，核心零 UI 耦合；
同一事件流同时用于终端渲染与 JSONL 持久化（可回放、可做回归测试）。
"""
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class TextDelta:
    """模型回复的文本增量（流式打字机效果）。"""
    text: str


@dataclass
class ToolCall:
    """一次工具调用（解析后的内部 Action，含校验结果）。"""
    call_id: str
    name: str
    arguments: dict


@dataclass
class ToolCallEvent:
    """主循环即将执行一次工具调用。"""
    call_id: str
    name: str
    arguments: dict


@dataclass
class ToolResultEvent:
    """一次工具调用的执行结果。"""
    call_id: str
    name: str
    ok: bool
    output: str
    truncated: bool = False


@dataclass
class StepEvent:
    """迭代进度。"""
    step: int
    max_steps: int


@dataclass
class ErrorEvent:
    """非致命错误（工具失败是反馈，不走这里；这里是循环级错误）。"""
    message: str


@dataclass
class FinishEvent:
    """任务完成。"""
    summary: str


@dataclass
class TrimmedEvent:
    """上下文管理：因预算裁剪了最老的 N 轮工具调用（让上下文管理透明可观察）。"""
    rounds: int


def event_to_dict(ev: Any) -> dict:
    """事件 → JSON 安全 dict（供会话日志与回放）。"""
    d = asdict(ev)
    d["type"] = type(ev).__name__
    return d
