"""后台任务工具：start_background / poll_background / stop_background / list_background。"""
from .paths import resolve_workspace_path
from .registry import ToolRejected, register
from .shell_tools import CATASTROPHIC, _check_blacklist


def _mgr(tool_ctx, name):
    mgr = getattr(tool_ctx, "background", None)
    if mgr is None:
        raise ToolRejected(f"{name} 不可用：后台管理未初始化")
    return mgr


def tool_start_background(tool_ctx, command, timeout=None, workdir=None):
    command = (command or "").strip()
    if not command:
        return "命令为空"
    why = _check_blacklist(command, CATASTROPHIC)
    if why:
        raise ToolRejected(f"已拦截危险命令（{why}）：{command}")
    mgr = _mgr(tool_ctx, "start_background")
    cwd = tool_ctx.workspace
    if workdir:
        p = resolve_workspace_path(tool_ctx.workspace, workdir)
        cwd = p if p.is_dir() else tool_ctx.workspace
    result, err = mgr.start(command, cwd, timeout)
    if err:
        return err
    return f"后台任务已启动: id={result['task_id']} pid={result['pid']} 命令: {command}"


def tool_poll_background(tool_ctx, task_id):
    mgr = _mgr(tool_ctx, "poll_background")
    r = mgr.poll(task_id)
    if "error" in r:
        return r["error"]
    return f"状态: {r['status']} 退出码: {r['exit_code']}\n{r['output']}"


def tool_stop_background(tool_ctx, task_id):
    mgr = _mgr(tool_ctx, "stop_background")
    r = mgr.stop(task_id)
    if "error" in r:
        return r["error"]
    return f"后台任务 {task_id} 已停止"


def tool_list_background(tool_ctx):
    mgr = _mgr(tool_ctx, "list_background")
    tasks = mgr.list_tasks()
    if not tasks:
        return "没有后台任务"
    return "\n".join(f"{t['task_id']} [{t['status']}] pid={t['pid']} : {t['command']}" for t in tasks)


def register_background_tools() -> None:
    register("start_background", {
        "type": "function",
        "function": {
            "name": "start_background",
            "description": "在后台启动一个长命令（不阻塞本任务，输出实时可见），立即返回任务 id。稍后用 poll_background / stop_background 管理。适合 dev server、长构建、耗时安装等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {"type": "integer", "description": "可选：超时秒数"},
                    "workdir": {"type": "string", "description": "执行目录（相对工作区根目录）"},
                },
                "required": ["command"],
            },
        },
    }, tool_start_background)
    register("poll_background", {
        "type": "function",
        "function": {
            "name": "poll_background",
            "description": "查询一个后台任务的状态与输出（status/exit_code/已产生的输出）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 id（来自 start_background）"},
                },
                "required": ["task_id"],
            },
        },
    }, tool_poll_background)
    register("stop_background", {
        "type": "function",
        "function": {
            "name": "stop_background",
            "description": "停止一个后台任务（终止其进程树）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 id"},
                },
                "required": ["task_id"],
            },
        },
    }, tool_stop_background)
    register("list_background", {
        "type": "function",
        "function": {
            "name": "list_background",
            "description": "列出全部后台任务及其状态。",
            "parameters": {"type": "object", "properties": {}},
        },
    }, tool_list_background)
