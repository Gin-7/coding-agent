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


def ensure_safe_file(workspace: Path, p: Path, path: str) -> None:
    """敏感路径保护：.env 系列（凭据）不允许读写；agent 状态目录仅允许写工作区记忆文件。

    状态目录判断以工作区自身为界（相对工作区的第一段）——沙箱等工作区可能恰好
    位于某个名为 .coding-agent 的祖先目录之下，不能把祖先误判为状态目录
    （回归：曾导致沙箱内一切读写被拒，agent 被逼用命令行转义写文件）。
    """
    if p.name.startswith(".env"):
        raise PathError(f"凭据文件受保护，不允许访问：{path}")
    try:
        rel = p.relative_to(Path(workspace).resolve())
    except ValueError:
        return  # 越界由 resolve_workspace_path 负责
    if rel.parts and rel.parts[0] == STATE_DIR_NAME:
        if not (len(rel.parts) == 2 and rel.parts[1] == ".agent-memory.md"):
            raise PathError(f"agent 状态目录受保护，不允许访问：{path}")
