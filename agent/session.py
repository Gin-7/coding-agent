"""会话持久化：JSONL 全量事件日志。

用途：回放调试、回归测试输入、演示 agent 执行全过程；--resume 从日志恢复对话继续干活。
事件流与渲染共用同一数据源（事件驱动架构的副产品）。
"""
import json
from datetime import datetime
from pathlib import Path


class Session:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        self._f = open(self.path, "a", encoding="utf-8")

    def log(self, event: dict) -> None:
        self._f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_messages(path: Path):
    """从会话 JSONL 读取最后一次 MessagesDump 的消息历史（--resume 用）。"""
    messages = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "MessagesDump" and obj.get("messages"):
            messages = obj["messages"]
    return messages


def latest_session(root: Path):
    """工作区 sessions 目录下最新**含可恢复历史**的会话文件（--resume 省略路径时用）。"""
    files = sorted(root.glob("session-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        if load_messages(f):
            return f
    return files[0] if files else None
