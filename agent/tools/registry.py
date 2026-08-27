"""工具注册表：{name: {"fn": callable, "schema": dict}}。

schema 直接作为 API 的 tools 参数 —— 工具即数据，新增工具 = 注册 + 实现，可扩展。
"""
from pathlib import Path
from typing import Callable, Optional

TOOLS: dict = {}


class ToolContext:
    """工具执行上下文：工作区根目录、实时输出回调等。"""

    def __init__(self, workspace: Path, on_output=None):
        self.workspace = workspace.resolve()
        self.on_output = on_output  # 可选：Callable[[str], None]，命令实时输出回调


class ToolRejected(Exception):
    """工具拒绝执行（安全拦截、超时终止等）：结果标记为失败（ok=False）。"""


def register(name: str, schema: dict, fn: Callable) -> None:
    if name in TOOLS:
        return  # 幂等：重复注册直接跳过（测试 / 热加载安全）
    TOOLS[name] = {"fn": fn, "schema": schema}


def tool_schemas() -> list:
    return [t["schema"] for t in TOOLS.values()]


def dispatch(name: str, args: dict, tool_ctx: ToolContext) -> dict:
    """本地执行工具；任何异常都转成结构化结果，绝不向上抛（错误即信息）。

    工具失败是任务环境的正常反馈，主循环会把它作为 tool 结果回喂给模型自我修复。
    """
    t = TOOLS.get(name)
    if not t:
        return {"ok": False, "output": f"未知工具: {name}"}
    try:
        output = t["fn"](tool_ctx=tool_ctx, **args)
        return {"ok": True, "output": str(output)}
    except ToolRejected as e:
        return {"ok": False, "output": str(e)}
    except Exception as e:  # noqa: BLE001 —— 有意兜底所有异常
        return {"ok": False, "output": f"{type(e).__name__}: {e}"}
