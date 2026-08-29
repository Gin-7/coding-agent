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
class CommandOutput:
    """命令执行的实时输出增量（流式显示，最终结果仍由 ToolResultEvent 汇总）。"""
    text: str


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


@dataclass
class CompactedEvent:
    """上下文管理：早期消息被压缩为摘要（summarized=True）或兜底丢弃（False）。"""
    messages_removed: int
    summarized: bool = True


@dataclass
class ContextUsageEvent:
    """上下文窗口使用情况（UI 环形指示器用）：当前估算 token 数与当前预算（窗口）。"""
    tokens: int
    budget: int


@dataclass
class BackgroundStarted:
    """后台任务启动（长命令不阻塞主循环）。"""
    task_id: str
    command: str
    pid: int


@dataclass
class BackgroundOutput:
    """后台任务实时输出。"""
    task_id: str
    text: str


@dataclass
class BackgroundStatus:
    """后台任务状态变化（done / stopped）。"""
    task_id: str
    status: str
    exit_code: int = None


@dataclass
class SubagentStarted:
    """子 agent 启动（运行态：开始在后台/并行执行）。"""
    subagent_id: str
    batch_id: str
    name: str
    prompt: str


@dataclass
class SubagentEvent:
    """子 agent 运行期间的逐事件上行（文本增量 / 工具调用 / 结果等）。

    event 为被包装的内部事件 dict（含 type 字段），前端按明细类型渲染成
    与主 agent 一致的对话视图。
    """
    subagent_id: str
    batch_id: str
    event: dict


@dataclass
class SubagentStatus:
    """子 agent 状态变化（done / error / interrupted）。"""
    subagent_id: str
    batch_id: str
    status: str
    summary: str


@dataclass
class SubagentResult:
    """子 agent 完成汇总（兼容旧路径；新链路改走 Started/Event/Status）。"""
    task_id: str
    status: str
    summary: str


def event_to_dict(ev: Any) -> dict:
    """事件 → JSON 安全 dict（供会话日志与回放）。"""
    d = asdict(ev)
    d["type"] = type(ev).__name__
    return d
