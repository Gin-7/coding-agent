"""文件工具（D1）：read_file / write_file。edit_file / list_dir / search 在 D3 补齐。"""
from pathlib import Path

from .paths import resolve_workspace_path
from .registry import register

MAX_READ_LINES = 2000


def _read_text(p: Path) -> str:
    """读文件：UTF-8 优先，fallback GBK / UTF-16（Windows 下常见编码陷阱）。"""
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def tool_read_file(tool_ctx, path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    if offset < 1:
        return "offset 必须 >= 1"
    limit = max(1, min(int(limit), MAX_READ_LINES))
    p = resolve_workspace_path(tool_ctx.workspace, path)
    if not p.is_file():
        return f"文件不存在: {path}"
    text = _read_text(p)
    lines = text.splitlines()
    if offset > len(lines):
        return f"文件 {path} 只有 {len(lines)} 行，offset 超出范围"
    end = min(len(lines), offset + limit - 1)
    sel = lines[offset - 1:end]
    width = len(str(len(lines)))
    body = "\n".join(f"{i:>{width}} | {ln}" for i, ln in enumerate(sel, start=offset))
    return f"文件 {path}：共 {len(lines)} 行，显示第 {offset}-{end} 行\n{body}"


def tool_write_file(tool_ctx, path: str, content: str) -> str:
    p = resolve_workspace_path(tool_ctx.workspace, path)
    if p.exists() and p.is_dir():
        return f"目标是目录，无法写入: {path}"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return f"已写入 {len(content)} 字符 → {path}"


def register_file_tools() -> None:
    register(
        "read_file",
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文件内容（带行号），支持 offset/limit 分页。路径相对工作区根目录，不允许越界。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径（相对工作区根目录）"},
                        "offset": {"type": "integer", "description": "起始行号，从 1 开始（默认 1）"},
                        "limit": {"type": "integer", "description": f"最多读取行数（默认 {MAX_READ_LINES}）"},
                    },
                    "required": ["path"],
                },
            },
        },
        tool_read_file,
    )
    register(
        "write_file",
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "创建或整体覆盖写入文件（UTF-8）。注意：会清空目标文件的已有内容。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径（相对工作区根目录）"},
                        "content": {"type": "string", "description": "要写入的完整内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        tool_write_file,
    )
