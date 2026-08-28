"""Web 服务器测试：静态页、工具、工作区/会话、运行任务、SSE、文件夹浏览、设置持久化。"""
import json as _json
import sys
import threading
import time
import urllib.parse as _parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_common import make_ws, run_tests


def test_web_server_endpoints(tmp):
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    from agent.web import create_server
    register_all()

    ws = make_ws(tmp, "web")
    confirm_calls = []

    def factory(on_event, hub, web):
        return AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                         max_steps=10, on_event=on_event,
                         confirm=lambda n, d: (confirm_calls.append(n), True)[1])

    httpd, web = create_server(ws, factory, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            assert r.status == 200 and b"Coding Agent" in r.read()
        with urllib.request.urlopen(base + "/api/tools", timeout=10) as r:
            tools = _json.loads(r.read())
        assert "read_file" in tools and "git_commit" in tools

        with urllib.request.urlopen(base + "/api/workspace", timeout=10) as r:
            meta = _json.loads(r.read())
        assert "web" in meta["root"]
        assert len(meta["sessions"]) >= 1 and meta["active"]

        # 列出所有工作区（侧边栏树）
        with urllib.request.urlopen(base + "/api/workspaces", timeout=10) as r:
            ws_list = _json.loads(r.read())
        assert len(ws_list) >= 1 and ws_list[0]["is_active"]

        # 在指定工作区新建会话；切换工作区后工具根目录必须同步（关键修复）
        sub = ws / "subws"
        sub.mkdir()
        req = urllib.request.Request(base + "/api/workspace/session/new", data=_json.dumps(
            {"path": str(sub)}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["ok"]
        assert str(web.loop.tool_ctx.workspace).replace("\\", "/").rstrip("/") == \
            str(sub.resolve()).replace("\\", "/").rstrip("/")

        # 切回默认工作区
        req = urllib.request.Request(base + "/api/workspace", data=_json.dumps(
            {"path": str(ws)}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["ok"]
        assert str(web.loop.tool_ctx.workspace).replace("\\", "/") == str(ws.resolve()).replace("\\", "/")

        req = urllib.request.Request(base + "/api/session/new", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["ok"]

        # 先连 SSE 再运行，避免丢早期事件
        sse = urllib.request.urlopen(base + "/api/events", timeout=30)
        req = urllib.request.Request(base + "/api/run", data=_json.dumps(
            {"task": "演示任务"}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["ok"]
        events = []
        try:
            deadline = 20
            start = time.time()
            while time.time() - start < deadline:
                line = sse.readline().decode("utf-8", errors="replace").strip()
                if line.startswith("data: "):
                    ev = _json.loads(line[6:])
                    events.append(ev)
                    if ev["type"] == "RunResult":
                        break
        finally:
            sse.close()
        types = [e["type"] for e in events]
        assert "UserMessage" in types and "ToolCallEvent" in types and "RunResult" in types
        assert events[-1]["status"] == "finished"

        # 文件夹选择器：根浏览（盘符）与具体目录
        with urllib.request.urlopen(base + "/api/fs/browse", timeout=10) as r:
            b = _json.loads(r.read())
        assert b.get("isRoot") and len(b["entries"]) >= 1
        with urllib.request.urlopen(base + "/api/fs/browse?path=" + _parse.quote(str(ws)), timeout=10) as r:
            assert "entries" in _json.loads(r.read())

        # 会话切换
        req = urllib.request.Request(base + "/api/session/select", data=_json.dumps(
            {"filename": meta["active"]}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["ok"]

        with urllib.request.urlopen(base + "/api/files?path=.", timeout=10) as r:
            assert "path" in _json.loads(r.read())
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_settings_persist(tmp):
    """设置持久化：白名单校验 + 落盘 .agent-settings.json + 重启后读回。"""
    from agent.web import EventHub, SETTINGS_FILE_NAME, WebAgentServer, Workspace

    ws = make_ws(tmp, "settings")
    web = WebAgentServer(None, EventHub(), Workspace(ws))
    assert web.get_settings() == {}

    # 合法键保存，未知键忽略
    out = web.update_settings({"theme": "light", "sidebar_collapsed": True, "evil": "x"})
    assert out == {"theme": "light", "sidebar_collapsed": True}
    data = _json.loads((ws / SETTINGS_FILE_NAME).read_text(encoding="utf-8"))
    assert data["theme"] == "light" and data["sidebar_collapsed"] is True

    # 新实例（模拟服务重启）读回；非法值不覆盖
    web2 = WebAgentServer(None, EventHub(), Workspace(ws))
    assert web2.get_settings() == {"theme": "light", "sidebar_collapsed": True}
    web2.update_settings({"theme": "blue", "sidebar_collapsed": "yes"})
    assert web2.get_settings() == {"theme": "light", "sidebar_collapsed": True}

    # HTTP 端到端：POST /api/settings → GET /api/settings
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    from agent.web import create_server
    register_all()
    ws2 = make_ws(tmp, "settings-http")

    def factory(on_event, hub, web):
        return AgentLoop(MockLLM(), Context(make_system_prompt(str(ws2)), 56000),
                         ToolContext(ws2), max_steps=10, on_event=on_event)

    httpd, web = create_server(ws2, factory, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/settings", data=_json.dumps(
            {"theme": "light"}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["theme"] == "light"
        with urllib.request.urlopen(base + "/api/settings", timeout=10) as r:
            assert _json.loads(r.read())["theme"] == "light"
        assert _json.loads((ws2 / SETTINGS_FILE_NAME).read_text(encoding="utf-8"))["theme"] == "light"
    finally:
        httpd.shutdown()
        httpd.server_close()


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
