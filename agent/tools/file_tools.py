"""文件工具：read_file / write_file / edit_file / undo_file。

edit_file / write_file 修改已有文件前自动备份到 .agent-backups/，
undo_file 从备份恢复最近一次修改（磁盘备份，跨会话可用）。
"""
import shutil
from pathlib import Path

from .paths import ensure_safe_file, resolve_workspace_path
from .registry import register

MAX_READ_LINES = 2000
BACKUP_ROOT = ".agent-backups"


def _backup(tool_ctx, p: Path) -> None:
    """修改已有文件前备份：.agent-backups/<相对路径>.bak（只保留最新一份）。"""
    try:
        rel = p.relative_to(tool_ctx.workspace)
    except ValueError:
        return
    if not p.exists() or not p.is_file():
        return
    dest = tool_ctx.workspace / BACKUP_ROOT / (str(rel).replace("\\", "/") + ".bak")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest)


def _read_text(p: Path) -> str:
    """读文件：UTF-8 优先，fallback GBK / UTF-16（Windows 下常见编码陷阱）。"""
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def _read_raw(p: Path) -> tuple:
    """按原始字节读取（不做通用换行规范化），返回 (文本, 主导行尾)。"""
    data = p.read_bytes()
    for enc in ("utf-8", "gbk"):
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline = "\r\n" if crlf > lf else "\n"
    return text, newline


def tool_read_file(tool_ctx, path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    if offset < 1:
        return "offset 必须 >= 1"
    limit = max(1, min(int(limit), MAX_READ_LINES))
    p = resolve_workspace_path(tool_ctx.workspace, path)
    ensure_safe_file(p, path)
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
    ensure_safe_file(p, path)
    if p.exists() and p.is_dir():
        return f"目标是目录，无法写入: {path}"
    _backup(tool_ctx, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return f"已写入 {len(content)} 字符 → {path}"


def _unique_old(text: str, old: str):
    """在（已规范化为 LF 的）文本中定位唯一的 old。返回 (匹配数)。"""
    old = old.replace("\r\n", "\n")  # 模型传 \r\n 时也归一化
    count = text.count(old)
    return count


def tool_edit_file(tool_ctx, path: str, old: str, new: str) -> str:
    p = resolve_workspace_path(tool_ctx.workspace, path)
    ensure_safe_file(p, path)
    if not p.is_file():
        return f"文件不存在: {path}"
    text, newline = _read_raw(p)
    text_lf = text.replace("\r\n", "\n")  # 统一到 LF 便于匹配
    count = _unique_old(text_lf, old)
    if count == 0:
        return "未找到要替换的内容：old 文本与文件内容不一致（注意精确匹配，含空白与换行）"
    if count > 1:
        return f"old 文本在文件中出现 {count} 处，匹配不唯一：请提供更多上下文使匹配唯一"
    old_norm = old.replace("\r\n", "\n")
    _backup(tool_ctx, p)
    new_text = text_lf.replace(old_norm, new, 1)
    if newline == "\r\n":
        new_text = new_text.replace("\n", "\r\n")  # 保留原文件的行尾风格
    p.write_bytes(new_text.encode("utf-8"))
    idx = new_text.find(new)
    before = new_text[max(0, idx - 40):idx].replace("\n", "\\n")
    after = new_text[idx + len(new):idx + len(new) + 40].replace("\n", "\\n")
    return f"已修改 {path}（精确替换 1 处）\n...{before}[{new}]{after}..."


def tool_undo_file(tool_ctx, path: str) -> str:
    """从 .agent-backups 恢复该文件最近一次修改前的版本。"""
    p = resolve_workspace_path(tool_ctx.workspace, path)
    ensure_safe_file(p, path)
    try:
        rel = p.relative_to(tool_ctx.workspace)
    except ValueError:
        return f"路径越界: {path}"
    bak = tool_ctx.workspace / BACKUP_ROOT / (str(rel).replace("\\", "/") + ".bak")
    if not bak.exists():
        return f"没有可撤销的备份: {path}"
    shutil.copy2(bak, p)
    try:
        bak.unlink()
    except OSError:
        pass  # 恢复已完成；备份删除失败（如被文件锁占用）不影响撤销结果
    return f"已撤销对 {path} 的最近一次修改（从备份恢复）"


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
    register(
        "edit_file",
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "精确搜索-替换文件中的一段文本（UTF-8）。old 必须与文件内容完全一致（含空白与换行），且只能匹配一处，否则会失败——需要更多上下文时把 old 加长。适合修改大文件的局部内容，避免整体重写。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径（相对工作区根目录）"},
                        "old": {"type": "string", "description": "要被替换的原文（必须精确匹配且唯一）"},
                        "new": {"type": "string", "description": "替换后的新文本"},
                    },
                    "required": ["path", "old", "new"],
                },
            },
        },
        tool_edit_file,
    )
    register(
        "undo_file",
        {
            "type": "function",
            "function": {
                "name": "undo_file",
                "description": "撤销对文件最近一次修改：从自动备份（.agent-backups/）恢复修改前的版本。write_file/edit_file 修改已有文件时会自动备份。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要恢复的文件路径（相对工作区根目录）"},
                    },
                    "required": ["path"],
                },
            },
        },
        tool_undo_file,
    )
