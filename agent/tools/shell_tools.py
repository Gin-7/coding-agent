"""命令执行工具 run_command：subprocess 执行 cmd，超时 + 进程树终止 + 输出截断。

Windows 关键点：
- shell=True 走 cmd.exe
- CREATE_NEW_PROCESS_GROUP + taskkill /T /F 保证超时能杀掉整棵进程树
- 输出按 UTF-8 → GBK fallback 解码，避免中文乱码
- 注册 on_output 回调时启用读线程，实现实时输出（最终结果仍截断回传）
"""
import re
import subprocess
import threading
from typing import Optional

from .paths import resolve_workspace_path
from .registry import ToolRejected, register

MAX_OUTPUT_CHARS = 3000
MAX_COMMAND_CHARS = 7000
DEFAULT_TIMEOUT = 120
CREATE_NEW_PROCESS_GROUP = 0x00000200

# (正则, 拦截原因)：危险命令黑名单，命中即拒绝执行（权限模型：工作区内自动 + 黑名单）
BLACKLIST = [
    (r"^\s*(del|erase|rmdir|rd)\b", "危险删除命令"),
    (r"^\s*format\b", "磁盘格式化"),
    (r"^\s*rm\s+-rf", "危险删除命令"),
    (r"^\s*shutdown\b", "关机/重启"),
]


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def _check_blacklist(command: str) -> Optional[str]:
    for pat, why in BLACKLIST:
        if re.search(pat, command, re.IGNORECASE):
            return why
    return None


def _kill_tree(proc) -> None:
    try:
        subprocess.run(
            ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def tool_run_command(tool_ctx, command: str, timeout: int = DEFAULT_TIMEOUT, workdir: str = None) -> str:
    command = (command or "").strip()
    if not command:
        return "命令为空"
    if len(command) > MAX_COMMAND_CHARS:
        return f"命令过长（>{MAX_COMMAND_CHARS} 字符）：请把长操作写成脚本文件后执行"
    why = _check_blacklist(command)
    if why:
        raise ToolRejected(f"已拦截危险命令（{why}）：{command}")
    timeout = max(1, min(int(timeout), 600))
    if workdir:
        cwd = resolve_workspace_path(tool_ctx.workspace, workdir)
        cwd = cwd if cwd.is_dir() else tool_ctx.workspace
    else:
        cwd = tool_ctx.workspace
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        raise ToolRejected(f"启动命令失败: {e}")

    # 实时输出模式：读线程逐行解码并回调（UTF-8/GBK 多字节字符不会跨 \n 拆断）
    if getattr(tool_ctx, "on_output", None):
        chunks: list = []

        def _reader():
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                chunk = _decode(line)
                chunks.append(chunk)
                try:
                    tool_ctx.on_output(chunk)
                except Exception:
                    pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            _kill_tree(proc)
            t.join(timeout=5)
            raise ToolRejected(f"命令超时（>{timeout}s），进程树已终止")
        proc.wait()
        text = "".join(chunks)
        try:
            proc.stdout.close()
        except Exception:
            pass
        code = proc.returncode
    else:
        try:
            out_bytes, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise ToolRejected(f"命令超时（>{timeout}s），进程树已终止")
        text = _decode(out_bytes or b"")
        code = proc.returncode

    truncated = len(text) > MAX_OUTPUT_CHARS
    shown = text[:MAX_OUTPUT_CHARS]
    return f"退出码 {code}；输出 {len(text)} 字符{'（已截断）' if truncated else ''}\n{shown}"


def register_shell_tools() -> None:
    register(
        "run_command",
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "在 Windows cmd 中执行命令（默认在工作区根目录）。带超时（默认 120s），超时自动终止进程树；危险命令会被拦截；输出超过 3000 字符会截断。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的命令"},
                        "timeout": {"type": "integer", "description": "超时秒数，1-600（默认 120）"},
                        "workdir": {"type": "string", "description": "执行目录（相对工作区根目录，默认工作区根目录）"},
                    },
                    "required": ["command"],
                },
            },
        },
        tool_run_command,
    )
