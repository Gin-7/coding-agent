"""Web 服务器测试：静态页、工具、工作区/会话、运行任务、SSE、文件夹浏览、设置持久化、
工作区重命名/移除与会话重命名/置顶/归档。"""
import json as _json
import sys
import threading
import time
import urllib.error
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

        # 运行时权限/计划设置应作用于 loop（_apply_permission_plan）
        req = urllib.request.Request(base + "/api/settings", data=_json.dumps(
            {"permission": "ask", "plan": False}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            s = _json.loads(r.read())
        assert s.get("permission") == "ask" and web.loop.confirm is not None
        req = urllib.request.Request(base + "/api/settings", data=_json.dumps(
            {"permission": "auto", "plan": False}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            _json.loads(r.read())
        assert web.loop.confirm is None
        req = urllib.request.Request(base + "/api/settings", data=_json.dumps(
            {"permission": "plan"}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            _json.loads(r.read())
        # 计划模式也要挂 confirm：计划交出后靠它弹窗征求批准，批准后 loop 才关闭
        # plan_mode 转入执行阶段（旧行为是 confirm=None，批准后执行无法实现）
        assert web.loop.plan_mode is True and web.loop.confirm is not None
        req = urllib.request.Request(base + "/api/settings", data=_json.dumps(
            {"permission": "auto"}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            _json.loads(r.read())

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


def test_workspace_session_management(tmp):
    """工作区重命名/移除 + 会话重命名/置顶/归档：HTTP 路由 + SessionMeta 快照持久化 + 排序 + 运行中守卫。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    from agent.web import create_server
    register_all()

    ws = make_ws(tmp, "manage")

    def factory(on_event, hub, web):
        return AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                         max_steps=5, on_event=on_event)

    httpd, web = create_server(ws, factory, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post_raw(path, body):
        req = urllib.request.Request(base + path, data=_json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read())

    def workspaces():
        with urllib.request.urlopen(base + "/api/workspaces", timeout=10) as r:
            return _json.loads(r.read())

    def find_ws(root):
        return [w for w in workspaces() if Path(w["root"]) == Path(root).resolve()][0]

    try:
        # ---------- 工作区重命名：显示名别名，不动磁盘目录 ----------
        code, d = post_raw("/api/workspace/rename", {"path": str(ws), "name": "我的项目"})
        assert code == 200 and d["ok"] and d["name"] == "我的项目"
        assert find_ws(ws)["name"] == "我的项目"
        reg = _json.loads((ws / ".agent-workspaces.json").read_text(encoding="utf-8"))
        assert reg["names"] and list(reg["names"].values()) == ["我的项目"]
        # 改回文件夹名 = 清除别名
        code, d = post_raw("/api/workspace/rename", {"path": str(ws), "name": ws.name})
        assert code == 200 and d["ok"] and d["name"] == ws.name
        reg = _json.loads((ws / ".agent-workspaces.json").read_text(encoding="utf-8"))
        assert reg["names"] == {}
        # 空名称拒绝
        code, d = post_raw("/api/workspace/rename", {"path": str(ws), "name": "  "})
        assert code == 400 and not d["ok"]

        # ---------- 工作区移除：先注册子工作区再移除；默认工作区拒绝移除 ----------
        sub = ws / "subws"
        sub.mkdir()
        code, d = post_raw("/api/workspace/session/new", {"path": str(sub)})
        assert code == 200 and d["ok"]
        code, d = post_raw("/api/workspace/delete", {"path": str(sub)})
        assert code == 200 and d["ok"] and d["switched"] is True  # 移除的是当前工作区 → 切回默认
        assert all(Path(w["root"]) != sub.resolve() for w in workspaces())
        code, d = post_raw("/api/workspace/delete", {"path": str(ws)})
        assert code == 400 and not d["ok"]

        # ---------- 会话重命名：API 生效 + SessionMeta 快照落盘 + 重启读回 ----------
        active = web.workspace.active_filename
        code, d = post_raw("/api/session/rename",
                           {"root": str(ws), "filename": active, "name": "重构登录模块"})
        assert code == 200 and d["ok"] and d["name"] == "重构登录模块"
        assert find_ws(ws)["sessions"][0]["name"] == "重构登录模块"
        code, d = post_raw("/api/session/rename", {"root": str(ws), "filename": active, "name": " "})
        assert code == 400 and not d["ok"]
        from agent.web import Workspace as Ws
        ws2 = Ws(ws)  # 模拟服务重启
        assert ws2.session_map[active].name == "重构登录模块" and ws2.session_map[active].renamed

        # ---------- 置顶：老会话置顶后排在更新的会话之前 ----------
        time.sleep(1.1)  # 会话文件名精确到秒，避免同名覆盖
        code, d = post_raw("/api/session/new", {})
        assert code == 200 and d["ok"]
        second = web.workspace.active_filename
        code, d = post_raw("/api/session/pin", {"root": str(ws), "filename": active, "pinned": True})
        assert code == 200 and d["ok"]
        names = [s["filename"] for s in find_ws(ws)["sessions"]]
        assert names[0] == active and second in names
        sess = [s for s in find_ws(ws)["sessions"] if s["filename"] == active][0]
        assert sess["pinned"] is True
        ws3 = Ws(ws)
        assert ws3.session_map[active].pinned is True

        # ---------- 归档：置顶复位 + 排到末尾 + 取消归档可恢复 ----------
        code, d = post_raw("/api/session/archive", {"root": str(ws), "filename": active, "archived": True})
        assert code == 200 and d["ok"]
        sess = [s for s in find_ws(ws)["sessions"] if s["filename"] == active][0]
        assert sess["archived"] is True and sess["pinned"] is False
        names = [s["filename"] for s in find_ws(ws)["sessions"]]
        assert names[-1] == active
        code, d = post_raw("/api/session/archive", {"root": str(ws), "filename": active, "archived": False})
        assert code == 200 and d["ok"]
        sess = [s for s in find_ws(ws)["sessions"] if s["filename"] == active][0]
        assert sess["archived"] is False and sess["pinned"] is False  # 取消归档回到常规列表（置顶不复原，可手动再置顶）

        # ---------- 运行中守卫：活跃会话在任务执行期间禁止改名/置顶/归档 ----------
        web.run_thread = threading.Thread(target=lambda: time.sleep(0.4), daemon=True)
        web.run_thread.start()
        code, d = post_raw("/api/session/rename", {"root": str(ws), "filename": second, "name": "x"})
        assert code == 400 and not d["ok"]
        code, d = post_raw("/api/session/pin", {"root": str(ws), "filename": second, "pinned": True})
        assert code == 400 and not d["ok"]
        web.run_thread.join()
        web.run_thread = None
        # 非活跃会话不受守卫影响
        code, d = post_raw("/api/session/rename", {"root": str(ws), "filename": active, "name": "改名不受限"})
        assert code == 200 and d["ok"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_session_robustness(tmp):
    """回归三连：空 path 拒绝（旧 bug：静默在当前工作区建会话）、同秒连建不撞名、
    会话激活不再立即落盘（惰性写入，首次运行才有文件）。"""
    from agent.context import Context
    from agent.loop import AgentLoop
    from agent.mock import MockLLM
    from agent.prompts import make_system_prompt
    from agent.tools import ToolContext, register_all
    from agent.web import create_server
    register_all()

    ws = make_ws(tmp, "robust")

    def factory(on_event, hub, web):
        return AgentLoop(MockLLM(), Context(make_system_prompt(str(ws)), 56000), ToolContext(ws),
                         max_steps=3, on_event=on_event)

    httpd, web = create_server(ws, factory, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post_raw(path, body):
        req = urllib.request.Request(base + path, data=_json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read())

    def session_path(filename):
        return ws / "sessions" / filename

    try:
        # 启动时的默认会话：已激活但未落盘
        first = web.workspace.active_filename
        assert not session_path(first).exists()

        # 空 / 缺失 path 一律 400，且不新建、不切换
        before = set(web.workspace.session_map)
        code, d = post_raw("/api/workspace/session/new", {})
        assert code == 400 and not d["ok"] and "路径" in d.get("message", "")
        code, d = post_raw("/api/workspace/session/new", {"path": ""})
        assert code == 400 and not d["ok"]
        assert set(web.workspace.session_map) == before
        assert web.workspace.active_filename == first

        # 同一秒内连建两个会话：文件名必须不同（旧 bug：同秒撞名，两份历史交错）
        code, _ = post_raw("/api/session/new", {})
        assert code == 200
        code, _ = post_raw("/api/session/new", {})
        assert code == 200
        names = list(web.workspace.session_map)
        assert len(names) == len(set(names)) == len(before) + 2
        newest = web.workspace.active_filename
        assert newest != first and not session_path(newest).exists()

        # 首次运行才创建文件并写入内容
        req = urllib.request.Request(base + "/api/run", data=_json.dumps(
            {"task": "看一下目录"}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert _json.loads(r.read())["ok"]
        start = time.time()
        while web.is_running() and time.time() - start < 15:
            time.sleep(0.1)
        assert not web.is_running()
        assert session_path(newest).exists()
        assert "UserMessage" in session_path(newest).read_text(encoding="utf-8")
    finally:
        httpd.shutdown()
        httpd.server_close()


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
