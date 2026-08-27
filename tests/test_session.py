"""会话持久化测试（JSONL 写入 / 读取恢复）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_common import make_ws, run_tests


def test_resume_roundtrip(tmp):
    from agent.session import Session, load_messages
    ws = make_ws(tmp, "resume")
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "out"}]
    with Session(ws / "sessions") as s:
        s.log({"type": "MessagesDump", "messages": msgs})
    loaded = load_messages(sorted((ws / "sessions").glob("*.jsonl"))[0])
    assert loaded == msgs


def test_session_named_file(tmp):
    from agent.session import Session
    ws = make_ws(tmp, "named")
    with Session(ws / "sessions", filename="custom.jsonl") as s:
        s.log({"type": "UserMessage", "content": "hi"})
    assert (ws / "sessions" / "custom.jsonl").exists()


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
