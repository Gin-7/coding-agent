"""元工具：finish —— 完成任务标记（主循环终止条件之一）。"""
from .registry import register


def tool_finish(tool_ctx, summary: str) -> str:
    return f"任务完成：{summary}"


def register_meta_tools() -> None:
    register(
        "finish",
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "任务已完成，结束 agent 执行并给出总结。必须在你确认任务真正完成时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "任务完成总结（做了什么、结果如何）"},
                    },
                    "required": ["summary"],
                },
            },
        },
        tool_finish,
    )
