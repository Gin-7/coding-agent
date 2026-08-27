"""会话持久化：JSONL 全量事件日志。

用途：回放调试、回归测试输入、演示 agent 执行全过程。
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
