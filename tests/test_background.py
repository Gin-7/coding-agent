"""后台任务测试（真实子进程）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_common import make_ws, run_tests


def _mgr_ctx(tmp, name):
    from agent.background import BackgroundManager
    from agent.tools import ToolContext
    ws = make_ws(tmp, name)
    ctx = ToolContext(ws)
    ctx.background = BackgroundManager()
    return ws, ctx


def test_background_run_and_poll(tmp):
    from agent.tools import dispatch, register_all
    register_all()
    ws, ctx = _mgr_ctx(tmp, "bg")
    r = dispatch("start_background", {"command": "echo bg-hello"}, ctx)
    assert r["ok"] and "bg-1" in r["output"]
    p = ""
    for _ in range(30):
        p = dispatch("poll_background", {"task_id": "bg-1"}, ctx)
        if "done" in p["output"]:
            break
        time.sleep(0.2)
    assert "bg-hello" in p["output"] and "done" in p["output"]


def test_background_stop(tmp):
    from agent.tools import dispatch, register_all
    register_all()
    ws, ctx = _mgr_ctx(tmp, "bgstop")
    r = dispatch("start_background", {"command": "ping -n 30 127.0.0.1"}, ctx)
    assert r["ok"]
    p = dispatch("stop_background", {"task_id": "bg-1"}, ctx)
    assert "已停止" in p["output"]
    p2 = dispatch("poll_background", {"task_id": "bg-1"}, ctx)
    assert "stopped" in p2["output"]


def test_background_events(tmp):
    from agent.tools import dispatch, register_all
    register_all()
    ws, ctx = _mgr_ctx(tmp, "bgev")
    events = []
    ctx.background.emit = events.append
    r = dispatch("start_background", {"command": "echo hi"}, ctx)
    assert r["ok"], r["output"]
    # 轮询等待而非固定 sleep：机器繁忙时进程启动 + 读线程收尾可能超过 0.4s（曾造成偶发假阴性）
    for _ in range(50):
        if any(e.get("type") == "BackgroundStatus" for e in events):
            break
        time.sleep(0.1)
    assert any(e.get("type") == "BackgroundStarted" for e in events)
    assert any(e.get("type") == "BackgroundStatus" for e in events)


def test_background_live_output(tmp):
    from agent.tools import dispatch, register_all
    register_all()
    ws, ctx = _mgr_ctx(tmp, "bglive")
    out = []
    ctx.background.on_output = lambda tid, text: out.append((tid, text))
    dispatch("start_background", {"command": "echo live-marker"}, ctx)
    for _ in range(30):
        if any("live-marker" in t for _, t in out):
            break
        time.sleep(0.2)
    assert any("live-marker" in t for _, t in out)


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
