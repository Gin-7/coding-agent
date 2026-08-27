"""git 工具集：git_status / git_diff / git_commit / git_log。

coding agent 标配能力（Claude Code / Codex 均有 git 集成）。
用参数列表方式调用 git（不经 shell），避免注入；输出截断回传。
"""
import subprocess
from typing import Optional

from .paths import resolve_workspace_path
from .registry import ToolRejected, register
from .shell_tools import _decode

MAX_OUTPUT_CHARS = 3000
GIT_TIMEOUT = 30


def _git(args: list, cwd, timeout: int = GIT_TIMEOUT):
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolRejected(f"git 命令超时（>{timeout}s）: git {' '.join(args)}")
    except Exception as e:
        raise ToolRejected(f"git 命令启动失败: {e}")
    out = _decode(proc.stdout or b"")
    err = _decode(proc.stderr or b"")
    return proc.returncode, out, err


def _fmt(code: int, out: str, err: str = "") -> str:
    text = out if not err else (out + "\n" + err if out else err)
    truncated = len(text) > MAX_OUTPUT_CHARS
    shown = text[:MAX_OUTPUT_CHARS]
    return f"git 退出码 {code}；输出 {len(text)} 字符{'（已截断）' if truncated else ''}\n{shown}"


def _workspace_cwd(tool_ctx, path: Optional[str]):
    if path:
        p = resolve_workspace_path(tool_ctx.workspace, path)
        cwd = p if p.is_dir() else p.parent
    else:
        cwd = tool_ctx.workspace
    return cwd


def tool_git_status(tool_ctx, path: str = None) -> str:
    cwd = _workspace_cwd(tool_ctx, path)
    code, out, err = _git(["status", "--short", "--branch"], cwd)
    if code != 0:
        raise ToolRejected(_fmt(code, out, err))
    return _fmt(code, out, err)


def tool_git_diff(tool_ctx, path: str = None, staged: bool = False) -> str:
    cwd = _workspace_cwd(tool_ctx, path)
    args = ["diff"]
    if staged:
        args.append("--staged")
    if path:
        args.append(path)
    code, out, err = _git(args, cwd)
    if code != 0:
        raise ToolRejected(_fmt(code, out, err))
    return _fmt(code, out, err)


def tool_git_commit(tool_ctx, message: str) -> str:
    if not (message or "").strip():
        return "commit message 不能为空"
    cwd = tool_ctx.workspace
    code, out, err = _git(["add", "-A"], cwd)
    if code != 0:
        raise ToolRejected(_fmt(code, out, err))
    code, out, err = _git(["commit", "-m", message], cwd)
    if code != 0:
        if "nothing to commit" in (out + err).lower():
            return "没有需要提交的更改"
        raise ToolRejected(_fmt(code, out, err))
    return _fmt(code, out, err)


def tool_git_log(tool_ctx, n: int = 5) -> str:
    n = max(1, min(int(n), 50))
    cwd = tool_ctx.workspace
    code, out, err = _git(["log", "--oneline", "-n", str(n)], cwd)
    if code != 0:
        raise ToolRejected(_fmt(code, out, err))
    return _fmt(code, out, err)


def register_git_tools() -> None:
    register(
        "git_status",
        {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "查看工作区 git 状态（当前分支 + 未提交改动列表，git status --short）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "可选：查看指定目录/文件的 git 状态"},
                    },
                    "required": [],
                },
            },
        },
        tool_git_status,
    )
    register(
        "git_diff",
        {
            "type": "function",
            "function": {
                "name": "git_diff",
                "description": "查看未提交改动的内容（git diff，可指定文件或只看暂存区）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "可选：只查看该文件的改动"},
                        "staged": {"type": "boolean", "description": "是否查看暂存区（--staged）改动，默认 false"},
                    },
                    "required": [],
                },
            },
        },
        tool_git_diff,
    )
    register(
        "git_commit",
        {
            "type": "function",
            "function": {
                "name": "git_commit",
                "description": "暂存全部改动并提交（git add -A + git commit）。用于把当前工作保存为一次提交。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "提交信息"},
                    },
                    "required": ["message"],
                },
            },
        },
        tool_git_commit,
    )
    register(
        "git_log",
        {
            "type": "function",
            "function": {
                "name": "git_log",
                "description": "查看最近提交历史（git log --oneline）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "description": "查看条数，1-50（默认 5）"},
                    },
                    "required": [],
                },
            },
        },
        tool_git_log,
    )
