"""路径安全：文件工具默认锁死在工作区根目录内，越界一律拒绝。"""
from pathlib import Path


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
    """凭据保护：.env 系列文件（含 .env.local 等）不允许 agent 读写。"""
    if p.name.startswith(".env"):
        raise PathError(f"凭据文件受保护，不允许访问：{path}")
