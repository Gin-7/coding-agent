"""子 agent 工具：spawn_subagent —— 主 agent 把子任务交给一个独立的子 agent 处理。"""
from .registry import register


def tool_spawn_subagent(tool_ctx, prompt, max_steps=8, tools=None):
    prompt = (prompt or "").strip()
    if not prompt:
        return "任务描述不能为空"
    fn = getattr(tool_ctx, "subagent", None)
    if fn is None:
        return "子agent不可用"
    try:
        return fn(prompt, max_steps, tools)
    except Exception as e:  # noqa: BLE001 —— 子 agent 失败不崩父循环
        return f"子agent执行失败: {type(e).__name__}: {e}"


def register_subagent_tools() -> None:
    register("spawn_subagent", {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "把一个子任务交给一个独立的子 agent 处理并返回其结果（同步等待）。子 agent 上下文独立、默认只读（read/list/search/glob/git 查看），步数受限。适合把大任务拆成可独立探索的子任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "交给子 agent 的子任务描述（要做什么、关注什么）"},
                    "max_steps": {"type": "integer", "description": "子 agent 最大步数（默认 8，上限 20）"},
                    "tools": {"type": "array", "items": {"type": "string"},
                              "description": "可选：允许子 agent 使用的工具名列表（默认只读工具）"},
                },
                "required": ["prompt"],
            },
        },
    }, tool_spawn_subagent)
