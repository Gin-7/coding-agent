"""路径安全：文件工具默认锁死在工作区根目录内，越界一律拒绝。"""
from pathlib import Path

from ..config import STATE_DIR_NAME


class PathError(Exception):
    pass


def resolve_workspace_path(workspace: Path, path: str) -> Path:
    """把工具传入的路径解析为工作区内的绝对路径；越界抛 PathError。

    相对路径按工作区根目录解析（与进程 CWD 无关，保证可复现）。
    """
    ws = workspace.resolve()
    p = Path(path)
    if not p.is_absolute():
        p = ws / p
    p = p.resolve()
    try:
        p.relative_to(ws)
    except ValueError:
        raise PathError(f"路径越界（仅允许工作区内）：{path}")
    return p


def ensure_safe_file(p: Path, path: str) -> None:
    """敏感路径保护：.env 系列（凭据）与 .coding-agent/（agent 自身状态）不允许读写。

    唯一例外：.coding-agent/.agent-memory.md 是跨会话工作区记忆，agent 可写。
    """
    if p.name.startswith(".env"):
        raise PathError(f"凭据文件受保护，不允许访问：{path}")
    if STATE_DIR_NAME in p.parts:
        i = p.parts.index(STATE_DIR_NAME)
        if not (len(p.parts) == i + 2 and p.parts[i + 1] == ".agent-memory.md"):
            raise PathError(f"agent 状态目录受保护，不允许访问：{path}")
