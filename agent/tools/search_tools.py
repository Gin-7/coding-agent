"""目录浏览与搜索工具：list_dir / search。"""
import os
import re
from pathlib import Path

from .paths import PathError, resolve_workspace_path
from .registry import register

from ..config import STATE_DIR_NAME

MAX_LIST_ENTRIES = 200
MAX_SEARCH_MATCHES = 100
MAX_GLOB_RESULTS = 200
# .coding-agent 是 agent 自身状态（会话/记忆/备份），不进搜索与列表，避免污染上下文
SEARCH_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                    ".coding-agent", ".test-tmp", ".idea", ".vscode"}
SEARCH_SKIP_FILES = {".env", ".gitignore"}


def tool_list_dir(tool_ctx, path: str = ".") -> str:
    p = resolve_workspace_path(tool_ctx.workspace, path or ".")
    if STATE_DIR_NAME in p.parts:
        raise PathError(f"agent 状态目录受保护，不允许访问：{path}")
    if not p.is_dir():
        return f"目录不存在: {path}"
    entries = sorted((e for e in p.iterdir() if e.name != STATE_DIR_NAME),
                     key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = []
    shown = 0
    for e in entries:
        if shown >= MAX_LIST_ENTRIES:
            break
        shown += 1
        if e.is_dir():
            lines.append(f"[目录] {e.name}/")
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = -1
            lines.append(f"[文件] {e.name} ({size} B)")
    total_dirs = sum(1 for e in entries if e.is_dir())
    total_files = len(entries) - total_dirs
    head = f"目录 {path}：{total_dirs} 个子目录，{total_files} 个文件"
    if len(entries) > MAX_LIST_ENTRIES:
        head += f"（仅显示前 {MAX_LIST_ENTRIES} 项）"
    return "\n".join([head] + lines)


def tool_search(tool_ctx, pattern: str, path: str = ".", regex: bool = False) -> str:
    if not pattern:
        return "pattern 不能为空"
    root = resolve_workspace_path(tool_ctx.workspace, path or ".")
    if not root.is_dir():
        return f"目录不存在: {path}"
    if regex:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"正则表达式无效: {e}"
        match = lambda line: bool(rx.search(line))
    else:
        pat = pattern.lower()
        match = lambda line: pat in line.lower()

    results = []
    total = 0
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS]
        for fn in sorted(filenames):
            if fn in SEARCH_SKIP_FILES or fn.startswith(".env"):
                continue
            fp = Path(dirpath) / fn
            rel = str(fp.relative_to(root)).replace("\\", "/")
            try:
                data = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue  # 二进制文件跳过
            text = data.decode("utf-8", errors="replace")
            scanned += 1
            for i, line in enumerate(text.splitlines(), 1):
                if match(line):
                    total += 1
                    if len(results) < MAX_SEARCH_MATCHES:
                        shown = line.strip()[:120]
                        results.append(f"{rel}:{i}: {shown}")
    if not results:
        return f"未找到匹配（扫描 {scanned} 个文件）"
    head = f"找到 {total} 处匹配（扫描 {scanned} 个文件）"
    if total > MAX_SEARCH_MATCHES:
        head += f"，仅显示前 {MAX_SEARCH_MATCHES} 处"
    return "\n".join([head] + results)


def tool_glob(tool_ctx, pattern: str, path: str = ".") -> str:
    """按 glob 模式在工作区内找文件（支持 ** 递归）。"""
    if not pattern:
        return "pattern 不能为空"
    root = resolve_workspace_path(tool_ctx.workspace, path or ".")
    if not root.is_dir():
        return f"目录不存在: {path}"
    matches = []
    try:
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            if any(seg in SEARCH_SKIP_DIRS for seg in rel.split("/")):
                continue
            matches.append(rel)
    except Exception as e:
        return f"glob 模式解析失败: {e}"
    matches.sort()
    if not matches:
        return f"未找到匹配 {pattern}"
    head = f"找到 {len(matches)} 个匹配"
    if len(matches) > MAX_GLOB_RESULTS:
        head += f"，仅显示前 {MAX_GLOB_RESULTS} 个"
    return "\n".join([head] + matches[:MAX_GLOB_RESULTS])


def register_search_tools() -> None:
    register(
        "list_dir",
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "列出目录下的子目录与文件（含大小）。路径相对工作区根目录，默认工作区根目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录路径（相对工作区根目录，默认工作区根目录）"},
                    },
                    "required": [],
                },
            },
        },
        tool_list_dir,
    )
    register(
        "search",
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "在工作区内递归搜索文件内容（默认忽略 .git/__pycache__ 等目录）。返回 文件:行号:内容。regex=false 时按不区分大小写的子串匹配，regex=true 时按正则匹配。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "要搜索的内容或正则表达式"},
                        "path": {"type": "string", "description": "搜索起始目录（相对工作区根目录，默认工作区根目录）"},
                        "regex": {"type": "boolean", "description": "是否按正则匹配（默认 false）"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        tool_search,
    )
    register(
        "glob",
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "按 glob 模式在工作区内查找文件（支持 ** 递归，如 '**/*.py'、'tests/*.txt'）。返回匹配的相对路径列表。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "glob 模式（相对工作区根目录）"},
                        "path": {"type": "string", "description": "查找起始目录（相对工作区根目录，默认工作区根目录）"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        tool_glob,
    )
