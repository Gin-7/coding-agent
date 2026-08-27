"""Web UI：本地 HTTP 服务器 + SSE 事件流，浏览器作为渲染器。

事件驱动架构的直接受益者：AgentLoop 保持不变，web 层把 on_event 接到 SSE 广播；
审批（plan/ask 模式）与中断通过 HTTP 回传。零额外依赖（仅标准库 + requests）。
"""
import json
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_UI_DIR = Path(__file__).resolve().parent / "web_ui"


class EventHub:
    """SSE 广播：多客户端订阅，事件推送到各自队列（慢客户端丢最旧）。"""

    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=20000)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def broadcast(self, event: dict):
        with self._lock:
            for q in list(self._clients):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    try:
                        q.get_nowait()  # 慢客户端丢最旧
                        q.put_nowait(event)
                    except Exception:
                        pass


class WebAgentServer:
    """持有一个 AgentLoop 的长驻服务：运行任务、审批、中断、恢复会话。"""

    def __init__(self, loop, session, hub: EventHub, workspace: Path):
        self.loop = loop
        self.session = session
        self.hub = hub
        self.workspace = workspace
        self.run_thread = None
        self.interrupt_event = threading.Event()
        self._pending_confirm = None  # (holder_dict, threading.Event)
        self._lock = threading.Lock()

    # ---------- 运行 ----------

    def is_running(self) -> bool:
        return self.run_thread is not None and self.run_thread.is_alive()

    def start_run(self, task: str):
        task = (task or "").strip()
        if not task:
            return False, "任务不能为空"
        if self.is_running():
            return False, "已有任务在执行中，请先等待完成或中断"
        self.interrupt_event.clear()
        self.hub.broadcast({"type": "UserMessage", "content": task})
        self.run_thread = threading.Thread(target=self._run_worker, args=(task,), daemon=True)
        self.run_thread.start()
        return True, ""

    def _run_worker(self, task: str):
        try:
            result = self.loop.run(task)
        except Exception as e:  # noqa: BLE001 —— 工作线程兜底，异常转为结果上报
            result = {"status": "error", "message": f"{type(e).__name__}: {e}"}
        self.hub.broadcast({"type": "RunResult", **result})
        self.session.log({"type": "RunResult", "status": result.get("status"),
                          "message": result.get("message") or result.get("summary"),
                          "steps": result.get("steps")})
        self.session.log({"type": "MessagesDump", "messages": self.loop.ctx.messages})

    def interrupt(self):
        self.interrupt_event.set()
        self.hub.broadcast({"type": "Notice", "message": "已请求中断，将在当前步骤结束后停止"})

    # ---------- 审批（plan / ask 模式） ----------

    def confirm(self, name: str, desc: str) -> bool:
        """阻塞等待浏览器响应 /api/confirm（超时视为拒绝）。"""
        holder = {}
        ev = threading.Event()
        with self._lock:
            self._pending_confirm = (holder, ev)
        self.hub.broadcast({"type": "AskConfirm", "name": name, "desc": desc})
        ev.wait(timeout=600)
        return holder.get("approved") is True

    def answer_confirm(self, approved: bool):
        with self._lock:
            pending = self._pending_confirm
            self._pending_confirm = None
        if pending:
            holder, ev = pending
            holder["approved"] = approved
            ev.set()

    # ---------- 会话恢复 ----------

    def resume(self, session_name: str):
        from .session import load_messages
        from .prompts import make_system_prompt
        path = self.workspace / "sessions" / session_name
        if not path.exists():
            return False, "会话文件不存在"
        msgs = load_messages(path)
        if not msgs:
            return False, "会话中没有可恢复的历史"
        if msgs[0].get("role") == "system":
            msgs[0]["content"] = make_system_prompt(self.workspace)
        self.loop.ctx.messages = msgs
        self.hub.broadcast({"type": "Notice", "message": f"已恢复会话 {session_name}（{len(msgs)} 条历史）"})
        return True, ""


def build_handler(server: WebAgentServer, workspace: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentWeb/0.1"

        def log_message(self, fmt, *args):
            pass  # 静默访问日志

        # ---------- 基础 ----------

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def _serve_static(self, name: str):
            p = WEB_UI_DIR / name
            if not p.exists():
                self.send_error(404)
                return
            data = p.read_bytes()
            ctype = {"html": "text/html; charset=utf-8",
                     "css": "text/css; charset=utf-8",
                     "js": "application/javascript; charset=utf-8"}.get(
                p.suffix.lstrip("."), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---------- GET ----------

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path == "/style.css":
                self._serve_static("style.css")
            elif path == "/app.js":
                self._serve_static("app.js")
            elif path == "/api/events":
                self._sse()
            elif path == "/api/tools":
                from .tools import TOOLS
                self._json({name: TOOLS[name]["schema"]["function"]["description"]
                            for name in sorted(TOOLS)})
            elif path == "/api/files":
                self._files(query)
            elif path == "/api/sessions":
                self._sessions()
            elif path == "/api/status":
                self._json({"running": server.is_running()})
            else:
                self.send_error(404)

        def _files(self, query):
            rel = (query.get("path") or [""])[0]
            root = (workspace / rel).resolve()
            try:
                root.relative_to(workspace)
            except ValueError:
                self._json({"error": "路径越界"}, 403)
                return
            if not root.is_dir():
                self._json({"error": "目录不存在"}, 404)
                return
            entries = []
            for e in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                try:
                    size = e.stat().st_size if e.is_file() else None
                except OSError:
                    size = None
                entries.append({"name": e.name, "dir": e.is_dir(), "size": size})
            rel_str = str(root.relative_to(workspace)).replace("\\", "/") or "."
            self._json({"path": rel_str, "entries": entries})

        def _sessions(self):
            items = []
            for f in sorted((workspace / "sessions").glob("session-*.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                items.append({"name": f.name, "size": f.stat().st_size})
            self._json(items)

        def _sse(self):
            """SSE 长连接：HTTP/1.0 无 Content-Length，连接保持到断开。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = server.hub.subscribe()
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        line = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                        self.wfile.write(line)
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                server.hub.unsubscribe(q)

        # ---------- POST ----------

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            body = self._read_body()
            if path == "/api/run":
                ok, msg = server.start_run(body.get("task", ""))
                self._json({"ok": ok, "message": msg}, 200 if ok else 409)
            elif path == "/api/interrupt":
                server.interrupt()
                self._json({"ok": True})
            elif path == "/api/confirm":
                server.answer_confirm(bool(body.get("approved")))
                self._json({"ok": True})
            elif path == "/api/resume":
                ok, msg = server.resume(body.get("session", ""))
                self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            else:
                self.send_error(404)

    return Handler


def create_server(workspace: Path, loop_factory, port: int = 0):
    """创建并启动 Web 服务器（不阻塞）。返回 (httpd, web_server)。

    loop_factory(on_event, hub, web_server) -> AgentLoop
    """
    from .events import event_to_dict
    from .session import Session

    hub = EventHub()
    session = Session(workspace / "sessions")
    web = WebAgentServer(None, session, hub, workspace)

    def on_event(ev):
        hub.broadcast(event_to_dict(ev))
        session.log(event_to_dict(ev))

    web.loop = loop_factory(on_event, hub, web)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), build_handler(web, workspace))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, web


def _build_loop(workspace: Path, args, on_event, web: WebAgentServer):
    """真实 / 模拟模式的 loop 构造：审批走 Web 弹窗，支持中断。"""
    from .context import Context
    from .loop import AgentLoop
    from .prompts import make_system_prompt
    from .tools import ToolContext, register_all

    register_all()
    if args.mock:
        from .mock import MockLLM
        llm = MockLLM()
        budget = 56000
    else:
        from .config import Config
        from .llm import LLMClient
        cfg = Config(workspace, model=args.model, base_url=args.base_url, api_key=args.api_key,
                     max_steps=args.max_steps, max_context_tokens=args.budget)
        if not cfg.api_key:
            raise SystemExit(
                "未找到 API key：请设置环境变量 AGENT_API_KEY / DEEPSEEK_API_KEY，"
                "或在工作区 .env 中提供（.env 已被 .gitignore 排除，不会入库）。")
        llm = LLMClient(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
                        temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.timeout)
        budget = cfg.max_context_tokens
    ctx = Context(make_system_prompt(workspace), budget)
    # 审批（Web 弹窗）仅在 ask / plan 模式启用，与 CLI 行为一致
    confirm = web.confirm if (args.permission == "ask" or args.plan) else None
    return AgentLoop(llm, ctx, ToolContext(workspace), max_steps=args.max_steps or 30,
                     on_event=on_event, confirm=confirm, plan_mode=args.plan,
                     interrupt_event=web.interrupt_event)


def run_server(workspace: Path, args, port: int = 8080, open_browser: bool = True) -> None:
    """启动 Web 服务器并阻塞（cli 的 --web 入口）。"""
    def factory(on_event, hub, web):
        return _build_loop(workspace, args, on_event, web)

    httpd, web = create_server(workspace, factory, port)
    addr = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"[Web UI] {addr}（Ctrl+C 退出）")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(addr)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        web.session.close()
