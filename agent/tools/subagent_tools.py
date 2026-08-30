"""子 agent 工具：spawn_subagent —— 主 agent 把子任务交给一个独立的子 agent 处理。"""
from .registry import register


def tool_spawn_subagent(tool_ctx, prompt, max_steps=12, tools=None, allow_write=False):
    prompt = (prompt or "").strip()
    if not prompt:
        return "任务描述不能为空"
    fn = getattr(tool_ctx, "subagent", None)
    if fn is None:
        return "子agent不可用"
    try:
        return fn(prompt, max_steps, tools, bool(allow_write))
    except Exception as e:  # noqa: BLE001 —— 子 agent 失败不崩父循环
        return f"子agent执行失败: {type(e).__name__}: {e}"


def tool_spawn_subagents(tool_ctx, tasks):
    """并行运行多个子 agent（同步等待）。tasks: 数组，每项 {prompt, max_steps?, tools?, allow_write?}。"""
    if not tasks:
        return "任务列表为空"
    fn = getattr(tool_ctx, "subagents", None)
    if fn is None:
        return "子agent不可用"
    try:
        return fn(tasks)
    except Exception as e:  # noqa: BLE001
        return f"子agent并行执行失败: {type(e).__name__}: {e}"


def tool_start_subagents(tool_ctx, tasks):
    """异步启动并行子 agent（不阻塞），返回批次 id。"""
    if not tasks:
        return "任务列表为空"
    fn = getattr(tool_ctx, "start_subagents", None)
    if fn is None:
        return "子agent不可用"
    try:
        batch = fn(tasks)
        return f"后台子agent批次已启动: {batch}" if isinstance(batch, str) else "启动失败"
    except Exception as e:  # noqa: BLE001
        return f"子agent启动失败: {type(e).__name__}: {e}"


def tool_wait_subagents(tool_ctx, batch_id):
    fn = getattr(tool_ctx, "wait_subagents", None)
    if fn is None:
        return "子agent不可用"
    try:
        return fn(batch_id)
    except Exception as e:  # noqa: BLE001
        return f"等待子agent失败: {type(e).__name__}: {e}"


def tool_list_subagent_batches(tool_ctx):
    fn = getattr(tool_ctx, "list_subagent_batches", None)
    if fn is None:
        return "子agent不可用"
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return f"查询失败: {type(e).__name__}: {e}"


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
                    "max_steps": {"type": "integer", "description": "子 agent 最大步数（默认 12，上限 30）"},
                    "tools": {"type": "array", "items": {"type": "string"},
                              "description": "可选：允许子 agent 使用的工具名列表（默认只读；allow_write=true 时才可放开写/执行）"},
                    "allow_write": {"type": "boolean", "description": "是否允许子 agent 使用写/执行类工具（默认 false=只读安全；设为 true 才可写文件/执行命令）"},
                },
                "required": ["prompt"],
            },
        },
    }, tool_spawn_subagent)
    register("spawn_subagents", {
        "type": "function",
        "function": {
            "name": "spawn_subagents",
            "description": "并行运行多个子 agent（线程并行，同步等待全部完成返回合并结果）。tasks 为数组，每项 {prompt, max_steps?, tools?, allow_write?}。默认只读；allow_write=true 才放开写/执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "子任务列表（最多 4 个）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string", "description": "子任务描述"},
                                "max_steps": {"type": "integer", "description": "子 agent 最大步数"},
                                "tools": {"type": "array", "items": {"type": "string"},
                                          "description": "允许的工具名列表（默认只读）"},
                                "allow_write": {"type": "boolean", "description": "是否允许写/执行（默认 false）"},
                            },
                            "required": ["prompt"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    }, tool_spawn_subagents)
    register("start_subagents", {
        "type": "function",
        "function": {
            "name": "start_subagents",
            "description": "异步启动一组并行子 agent（不阻塞主 agent，可先去做别的事），返回批次 id（如 sub-batch-1）。稍后用 wait_subagents 收结果、list_subagent_batches 查状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "description": "子任务列表（最多 4 个）",
                              "items": {"type": "object",
                                        "properties": {"prompt": {"type": "string", "description": "子任务描述"},
                                                       "max_steps": {"type": "integer"},
                                                       "tools": {"type": "array", "items": {"type": "string"}},
                                                       "allow_write": {"type": "boolean"}},
                                        "required": ["prompt"]}},
                },
                "required": ["tasks"],
            },
        },
    }, tool_start_subagents)
    register("wait_subagents", {
        "type": "function",
        "function": {
            "name": "wait_subagents",
            "description": "等待一个后台子 agent 批次完成并返回其合并结果（阻塞直到全部完成）。",
            "parameters": {
                "type": "object",
                "properties": {"batch_id": {"type": "string", "description": "批次 id（来自 start_subagents）"}},
                "required": ["batch_id"],
            },
        },
    }, tool_wait_subagents)
    register("list_subagent_batches", {
        "type": "function",
        "function": {
            "name": "list_subagent_batches",
            "description": "列出所有后台子 agent 批次及其运行状态。",
            "parameters": {"type": "object", "properties": {}},
        },
    }, tool_list_subagent_batches)
