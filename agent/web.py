"""Web UI：本地 HTTP 服务器 + SSE 事件流，浏览器作为渲染器。

支持：多工作区（选择本地文件夹）、每工作区多会话（会话名由首轮对话决定、点击切换续接）、
设置面板（明暗主题、工具列表）、右侧文件目录。浏览器作为渲染器，零额外依赖。

事件驱动架构的直接受益者：AgentLoop 保持不变，web 层把 on_event 接到 SSE 广播；
审批 / 中断通过 HTTP 回传；工作区 / 会话通过 HTTP 切换。
"""
import json
import os
import queue
import secrets
import string
import threading
import time
import urllib.parse
import datetime as _dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .config import find_env_file, prepare_state_dir, state_dir
from .events import ContextUsageEvent

WEB_UI_DIR = Path(__file__).resolve().parent / "web_ui"
MAX_SESSION_NAME = 24
MAX_SESSIONS_SHOWN = 100
SETTINGS_FILE_NAME = ".agent-settings.json"


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
                        q.get_nowait()
                        q.put_nowait(event)
                    except Exception:
                        pass


class SessionRecord:
    """工作区内的一个会话：文件名、名字（首轮决定或手动重命名）、消息历史、写入句柄。"""

    def __init__(self, filename: str):
        self.filename = filename
        self.name = "新会话"
        self.pinned = False
        self.archived = False
        self.renamed = False  # 用户手动改过名后，首轮对话不再自动覆盖会话名
        self.messages = None  # None 表示尚未从文件加载
        self.writer = None
        self.mtime = 0.0

    def path(self, sessions_dir: Path) -> Path:
        return sessions_dir / self.filename


class Workspace:
    def __init__(self, root: Path, display_name: str = None):
        self.root = Path(root).resolve()
        self.display_name = display_name
        # agent 状态（会话/配置/记忆/备份）统一放 .coding-agent/，并迁移旧版散落布局
        state = prepare_state_dir(self.root)
        self.sessions_dir = state / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_map: dict = {}
        self.active_filename = None
        self._scan()

    def _scan(self):
        for f in sorted(self.sessions_dir.glob("session-*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            if f.name not in self.session_map:
                rec = SessionRecord(f.name)
                rec.mtime = f.stat().st_mtime
                _load_session_meta(rec, f)
                self.session_map[f.name] = rec
        if not self.session_map:
            self.new_session()
        elif self.active_filename is None or self.active_filename not in self.session_map:
            # 已有会话但未设活跃：取最新一个
            self.active_filename = next(iter(sorted(
                self.session_map, key=lambda k: self.session_map[k].mtime, reverse=True)))

    def new_session(self) -> SessionRecord:
        base = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"session-{base}.jsonl"
        # 同秒重名保护：文件名只精确到秒，快速连建两个会话会撞名（两份历史交错进同一文件），
        # 已存在时追加 4 位随机后缀；glob 仍为 session-*.jsonl，对扫描/恢复透明
        while filename in self.session_map or (self.sessions_dir / filename).exists():
            filename = f"session-{base}-{secrets.token_hex(2)}.jsonl"
        rec = SessionRecord(filename)
        rec.mtime = time.time()
        self.session_map[filename] = rec
        self.active_filename = filename
        return rec

    def get(self, filename: str) -> SessionRecord:
        return self.session_map.get(filename)

    def get_active(self) -> SessionRecord:
        return self.session_map.get(self.active_filename)

    def list_sessions(self):
        """置顶优先、归档垫底，其余按最近活动倒序（侧边栏展示顺序）。"""
        recs = list(self.session_map.values())
        recs.sort(key=lambda r: (r.archived, not r.pinned, -r.mtime))
        return recs


def _load_session_meta(rec: SessionRecord, path: Path) -> None:
    """从会话文件恢复元数据：最后一条 SessionMeta 生效（重命名/置顶/归档为追加快照），
    无 SessionMeta 时回退第一条 UserMessage 作为名字。"""
    first_user = None
    meta = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "SessionMeta" and (obj.get("name") or "pinned" in obj or "archived" in obj):
                meta = obj
            elif t == "UserMessage" and first_user is None:
                first_user = obj.get("content")
    except OSError:
        pass
    if meta is not None:
        if meta.get("name"):
            rec.name = meta["name"]
            rec.renamed = True
        rec.pinned = bool(meta.get("pinned"))
        rec.archived = bool(meta.get("archived"))
    elif first_user:
        rec.name = first_user[:MAX_SESSION_NAME]


def load_messages_for_session(path: Path):
    """读取会话文件最后一条 MessagesDump（消息历史），无则返回 None。"""
    from .session import load_messages
    return load_messages(path)


class WebAgentServer:
    def __init__(self, loop, hub: EventHub, workspace: Workspace):
        self.loop = loop
        self.hub = hub
        self.workspace = workspace
        self.default_root = workspace.root
        self.system_prompt = ""
        self.budget = 56000
        self.budget_resolver = None
        self.window_resolver = None
        self.workspaces = {str(workspace.root): workspace}
        self._workspace_names = {}
        self.run_thread = None
        self.interrupt_event = threading.Event()
        self._pending_confirm = None
        self._lock = threading.Lock()
        # 面板设置/工作区注册表收进 .coding-agent/（旧版根目录文件一次性迁入），先于注册表加载
        state = prepare_state_dir(self.default_root)
        self._load_registry()
        self.settings_path = state / SETTINGS_FILE_NAME
        self.settings = self._load_settings()

    # ---------- 工作区注册表（左侧边栏展示所有工作区） ----------

    def _registry_path(self):
        return state_dir(self.default_root) / ".agent-workspaces.json"

    def _load_registry(self):
        p = self._registry_path()
        paths, names = [], {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                paths = data.get("workspaces", [])
                names = data.get("names", {}) or {}
            except (json.JSONDecodeError, OSError):
                paths = []
        self._workspace_names = {str(Path(k).resolve()): v for k, v in (names or {}).items()}
        if str(self.default_root) not in paths:
            paths.insert(0, str(self.default_root))
        seen = set()
        for raw in paths:
            key = str(Path(raw).resolve())
            if key in seen:
                continue
            seen.add(key)
            if key in self.workspaces:
                continue
            if Path(raw).is_dir():
                try:
                    self.workspaces[key] = Workspace(Path(raw), display_name=names.get(key))
                except OSError:
                    pass

    def _save_registry(self):
        root = str(self.default_root)
        paths = [root] + [str(w.root) for w in self.workspaces.values() if str(w.root) != root]
        seen, out = set(), []
        for x in paths:
            if x not in seen:
                seen.add(x)
                out.append(x)
        # 显示名别名：仅记录与文件夹名不同的（文件夹名本身不入库，目录改名后自动跟随）
        names = {str(w.root): w.display_name for w in self.workspaces.values()
                 if w.display_name and w.display_name != (w.root.name or str(w.root))}
        try:
            self._registry_path().write_text(
                json.dumps({"workspaces": out, "names": names}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    # ---------- 设置持久化（主题 / 侧栏布局；与工作区注册表同款本地 JSON） ----------

    def _load_settings(self) -> dict:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {k: data[k] for k in ("theme", "sidebar_collapsed", "right_collapsed",
                                     "model", "model_url", "model_key", "permission",
                                     "max_context_tokens", "max_tokens", "max_steps") if k in data}

    def _save_settings(self) -> None:
        try:
            self.settings_path.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def get_settings(self) -> dict:
        return dict(self.settings)

    def update_settings(self, patch: dict) -> dict:
        """白名单键 + 值校验后合并保存；未知键 / 非法值直接忽略。"""
        if isinstance(patch, dict):
            if patch.get("theme") in ("dark", "light"):
                self.settings["theme"] = patch["theme"]
            for k in ("sidebar_collapsed", "right_collapsed"):
                if isinstance(patch.get(k), bool):
                    self.settings[k] = patch[k]
            if isinstance(patch.get("model"), str) and patch["model"].strip():
                self.settings["model"] = patch["model"].strip()
            if isinstance(patch.get("model_url"), str):
                self.settings["model_url"] = patch["model_url"].strip()
            if isinstance(patch.get("model_key"), str):
                self.settings["model_key"] = patch["model_key"].strip()
            if "max_context_tokens" in patch:
                try:
                    self.settings["max_context_tokens"] = max(0, int(patch["max_context_tokens"]))
                except (TypeError, ValueError):
                    pass
            if "max_tokens" in patch:
                try:
                    # 单次生成上限：限制合理区间 [256, 65536]，避免把非法/过小值写进 .env 导致启动异常
                    self.settings["max_tokens"] = max(256, min(65536, int(patch["max_tokens"])))
                except (TypeError, ValueError):
                    pass
            if patch.get("permission") in ("auto", "ask", "plan"):
                self.settings["permission"] = patch["permission"]
            if "max_steps" in patch:
                # 单次任务最大迭代步数：限制合理区间 [1, 500]，非法/非正数忽略
                try:
                    iv = int(patch["max_steps"])
                except (TypeError, ValueError):
                    iv = 0
                if iv > 0:
                    self.settings["max_steps"] = min(500, iv)
            self._save_settings()
            # 模型相关设置写回 .env，使面板改动与 .env 配置保持一致（.env 已被 gitignore）
            self._sync_env(patch)
            self._apply_permission_plan()
            # 步数上限热生效：下一次 run() 即按新值执行（对正在运行的任务无影响）
            loop = getattr(self, "loop", None)
            if loop is not None and "max_steps" in self.settings:
                loop.max_steps = self.settings["max_steps"]
            # 设置变更（尤其上下文窗口上限 / 模型）后主动广播一次上下文使用率，
            # 使前端环形指示器的分母（预算/窗口）无需等到下条消息才刷新。
            # 全新会话同样归零显示（仅刷新分母），避免把系统提示词估算当成"已使用"
            loop = getattr(self, "loop", None)
            if loop is not None:
                if len(loop.ctx.messages) <= 1 and loop.ctx.last_prompt_tokens == 0:
                    loop._emit(ContextUsageEvent(tokens=0, budget=loop.ctx._cur_budget(),
                                                 window=loop.ctx._cur_window() or 0))
                else:
                    loop._emit_context_usage()
        return self.get_settings()

    def _sync_env(self, patch: dict) -> None:
        """把面板改动的模型/BaseURL/Key/上下文上限/步数 同步写回 .env（保留其余行/注释）。"""
        mapping = {"model": "AGENT_MODEL", "model_url": "AGENT_BASE_URL", "model_key": "AGENT_API_KEY",
                   "max_context_tokens": "AGENT_MAX_CONTEXT_TOKENS", "max_tokens": "AGENT_MAX_TOKENS",
                   "max_steps": "AGENT_MAX_STEPS"}
        wants = {}
        for k, env_key in mapping.items():
            if k not in patch:
                continue
            val = patch[k]
            if k in ("max_context_tokens", "max_tokens", "max_steps"):
                # 仅接受合法整数并夹到安全区间，避免把非法值写进 .env 导致下次启动 Config 崩溃
                try:
                    ival = int(val)
                except (TypeError, ValueError):
                    continue
                if k == "max_context_tokens":
                    if ival < 0:
                        continue
                elif k == "max_tokens":  # 单次生成上限，限制 [256, 65536]
                    ival = max(256, min(65536, ival))
                else:  # max_steps：单次任务步数上限，限制 [1, 500]
                    ival = max(1, min(500, ival))
                wants[env_key] = str(ival)
            else:
                if val == "":
                    continue
                wants[env_key] = val
        if not wants:
            return
        env_path = find_env_file(self.workspace.root)
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        touched = set()
        out = []
        for line in lines:
            stripped = line.strip()
            hit = False
            for env_key, val in wants.items():
                if stripped.startswith(env_key + "="):
                    out.append(f"{env_key}={val}")
                    touched.add(env_key)
                    hit = True
                    break
            if not hit:
                out.append(line)
        for env_key, val in wants.items():
            if env_key not in touched:
                out.append(f"{env_key}={val}")
        try:
            env_path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
        except OSError:
            pass

    def _apply_permission_plan(self) -> None:
        """把当前 permission（auto/ask/plan）应用到运行中的 loop（运行时切换授权/计划模式）。

        plan 模式下 confirm 也要挂上：计划交出来之后要靠它弹窗征求批准，
        批准后 loop 才关掉 plan_mode 转入执行阶段。
        """
        if self.loop is None:
            return
        permission = self.settings.get("permission", "auto")
        self.loop.confirm = self.confirm if permission in ("ask", "plan") else None
        self.loop.plan_mode = permission == "plan"

    def list_workspaces(self) -> list:
        result = []
        for key, ws in self.workspaces.items():
            result.append({
                "root": str(ws.root),
                "name": ws.display_name or ws.root.name or str(ws.root),
                "is_active": ws is self.workspace,
                "active": ws.active_filename,
                "sessions": self._session_dicts(ws),
            })
        return result

    def bind_loop(self, loop):
        """绑定 loop 并从其 ctx 派生 system_prompt / budget，激活默认会话。"""
        self.loop = loop
        self.system_prompt = loop.ctx.messages[0]["content"]
        self.budget = loop.ctx.budget
        self.budget_resolver = getattr(loop.ctx, "budget_resolver", None)
        self.window_resolver = getattr(loop.ctx, "window_resolver", None)
        self._activate(self.workspace.get_active())

    # ---------- 工作区 ----------

    def select_workspace(self, path: str):
        if not (path or "").strip():
            return False, "路径不能为空"
        p = Path(path).resolve()
        if not p.is_dir():
            return False, "不是有效目录"
        ws = self.workspaces.get(str(p))
        if ws is None:
            ws = Workspace(p)
            self.workspaces[str(p)] = ws
        self.workspace = ws
        self._activate(ws.get_active())
        self._save_registry()
        return True, str(p)

    def _session_dicts(self, ws: Workspace) -> list:
        return [
            {"filename": r.filename, "name": r.name, "mtime": r.mtime,
             "pinned": r.pinned, "archived": r.archived}
            for r in ws.list_sessions()
        ]

    def workspace_meta(self) -> dict:
        ws = self.workspace
        return {
            "root": str(ws.root),
            "sessions": self._session_dicts(ws),
            "active": ws.active_filename,
        }

    def rename_workspace(self, path: str, name: str):
        """改工作区显示名（别名持久化到注册表；不动磁盘上的真实文件夹）。"""
        p = Path(path).resolve()
        ws = self.workspaces.get(str(p))
        if ws is None:
            return False, "工作区不存在"
        name = (name or "").strip()
        if not name:
            return False, "名称不能为空"
        name = name[:40]
        if name == (ws.root.name or str(ws.root)):
            self._workspace_names.pop(str(p), None)  # 改回文件夹名 = 清除别名
        else:
            self._workspace_names[str(p)] = name
        ws.display_name = None if name == (ws.root.name or str(ws.root)) else name
        self._save_registry()
        return True, ws.display_name or ws.root.name or str(ws.root)

    def remove_workspace(self, path: str):
        """把工作区从侧边栏移除（仅移出注册表，不删除磁盘文件）。"""
        p = Path(path).resolve()
        key = str(p)
        if key == str(self.default_root):
            return False, "服务启动目录所在的工作区无法移除"
        ws = self.workspaces.pop(key, None)
        if ws is None:
            return False, "工作区不存在"
        self._workspace_names.pop(key, None)
        switched = False
        if ws is self.workspace:
            # 移除的是当前工作区：切回启动目录的最近会话
            self.workspace = self.workspaces[str(self.default_root)]
            self._activate(self.workspace.get_active())
            switched = True
        self._save_registry()
        return True, switched

    # ---------- 会话 ----------

    def _activate(self, rec: SessionRecord):
        """把 loop.ctx 切换到指定会话（保存旧会话消息，加载新会话消息）。"""
        prev = self.workspace.get_active()
        if prev is not None and prev is not rec and prev.messages is not None:
            prev.messages = list(self.loop.ctx.messages)
            if prev.writer:
                try:
                    prev.writer.close()
                except Exception:
                    pass
                prev.writer = None
        # 加载新会话消息
        from .context import Context
        if rec.messages is None:
            path = rec.path(self.workspace.sessions_dir)
            msgs = load_messages_for_session(path) if path.exists() else None
            rec.messages = msgs or self._fresh_messages()
        # 写入句柄惰性打开：激活即建文件会让从未使用的会话留下永久 0 字节 JSONL
        ctx = Context(self.system_prompt, self.budget, budget_resolver=self.budget_resolver,
                      window_resolver=self.window_resolver)
        ctx.messages = list(rec.messages)
        self.loop.ctx = ctx
        # 关键：工具根目录同步到当前工作区（否则 agent 仍操作旧工作区）
        self.loop.tool_ctx.workspace = self.workspace.root
        self.workspace.active_filename = rec.filename
        # 会话切换/新建后主动广播一次上下文使用率，使前端环形指示器无需等到下条消息才刷新
        loop = getattr(self, "loop", None)
        if loop is not None:
            # 全新会话（仅含系统提示词、尚无任何对话、且本次进程未发过请求）→ 环形归零显示，
            # 避免把系统提示词的启发式估算(~1K)当成"已使用"展示；首次请求后由 run() 内的广播接手计数
            if len(loop.ctx.messages) <= 1 and loop.ctx.last_prompt_tokens == 0:
                loop._emit(ContextUsageEvent(tokens=0, budget=loop.ctx._cur_budget(),
                                             window=loop.ctx._cur_window() or 0))
            else:
                loop._emit_context_usage()

    def _fresh_messages(self) -> list:
        return [{"role": "system", "content": self.system_prompt}]

    def _open_writer(self, rec: SessionRecord):
        from .session import Session
        return Session(self.workspace.sessions_dir, filename=rec.filename)

    def new_session(self):
        rec = self.workspace.new_session()
        self._activate(rec)
        return rec

    def select_session(self, filename: str):
        rec = self.workspace.get(filename)
        if rec is None:
            return False, "会话不存在"
        if self.is_running():
            return False, "任务执行中无法切换会话"
        self._activate(rec)
        return True, rec.name

    # ---------- 会话元数据（重命名 / 置顶 / 归档，追加 SessionMeta 快照持久化） ----------

    def _find_session(self, root: str, filename: str):
        """按工作区路径定位会话；root 为空时用当前工作区。返回 (workspace, record)。"""
        if root:
            ws = self.workspaces.get(str(Path(root).resolve()))
            if ws is None:
                return None, None
        else:
            ws = self.workspace
        return ws, ws.get(filename or "")

    def _persist_session_meta(self, ws: Workspace, rec: SessionRecord) -> None:
        """把会话元数据以最后一条 SessionMeta 快照的形式追加进 JSONL（O_APPEND 与运行中的写入互不冲突）。"""
        path = rec.path(ws.sessions_dir)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "SessionMeta", "name": rec.name,
                                    "pinned": rec.pinned, "archived": rec.archived},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _guard_active_session(self, ws, rec) -> str:
        """运行中禁止改动正在执行的活跃会话（其写入句柄正被逐行使用）。"""
        if self.is_running() and ws is self.workspace and rec.filename == ws.active_filename:
            return "任务执行中无法修改该会话"
        return ""

    def rename_session(self, root: str, filename: str, name: str):
        ws, rec = self._find_session(root, filename)
        if ws is None or rec is None:
            return False, "会话不存在"
        name = (name or "").strip()
        if not name:
            return False, "名称不能为空"
        guard = self._guard_active_session(ws, rec)
        if guard:
            return False, guard
        rec.name = name[:60]
        rec.renamed = True
        self._persist_session_meta(ws, rec)
        self.hub.broadcast({"type": "SessionsChanged"})
        return True, rec.name

    def set_session_flags(self, root: str, filename: str, pinned: bool = None, archived: bool = None):
        ws, rec = self._find_session(root, filename)
        if ws is None or rec is None:
            return False, "会话不存在"
        guard = self._guard_active_session(ws, rec)
        if guard:
            return False, guard
        if pinned is not None:
            rec.pinned = bool(pinned)
        if archived is not None:
            rec.archived = bool(archived)
        if rec.archived:
            rec.pinned = False  # 归档会话不参与置顶排序
        self._persist_session_meta(ws, rec)
        self.hub.broadcast({"type": "SessionsChanged"})
        return True, ""

    def new_session_in(self, path: str):
        """在指定工作区新建会话（必要时先切换工作区）。"""
        if self.is_running():
            return False, "任务执行中无法新建会话"
        if not (path or "").strip():
            return False, "路径不能为空"  # 空 path 会 resolve 成 CWD，静默在当前工作区建会话
        cur = str(self.workspace.root).replace("\\", "/").rstrip("/")
        target = str(Path(path).resolve()).replace("\\", "/").rstrip("/")
        if cur != target:
            ok, msg = self.select_workspace(path)
            if not ok:
                return False, msg
        rec = self.workspace.new_session()
        self._activate(rec)
        return True, rec.filename

    def session_messages(self, filename: str):
        rec = self.workspace.get(filename)
        if rec is None:
            return {"error": "会话不存在"}, 404
        path = rec.path(self.workspace.sessions_dir)
        msgs = load_messages_for_session(path) if path.exists() else None
        return {"filename": filename, "messages": msgs or []}, 200

    def session_events(self, filename: str):
        """读取会话 JSONL 全量事件日志，供前端回放（与实时渲染完全一致）。"""
        rec = self.workspace.get(filename)
        if rec is None:
            return {"error": "会话不存在"}, 404
        path = rec.path(self.workspace.sessions_dir)
        events = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(obj)
        return {"filename": filename, "events": events}, 200

    # ---------- 运行 ----------

    def is_running(self) -> bool:
        return self.run_thread is not None and self.run_thread.is_alive()

    def start_run(self, task: str):
        task = (task or "").strip()
        if not task:
            return False, "任务不能为空"
        if self.is_running():
            return False, "已有任务在执行中，请先等待完成或中断"
        ws = self.workspace
        rec = ws.get_active()
        if rec is None:
            return False, "请先新建或选择一个会话"
        if rec.writer is None:
            rec.writer = self._open_writer(rec)
        # 首轮对话决定会话名（用户手动改过名的会话不覆盖）
        if not rec.renamed and (rec.name == "新会话" or (rec.messages and len(rec.messages) <= 1)):
            rec.name = task[:MAX_SESSION_NAME]
            if rec.writer:
                rec.writer.log({"type": "SessionMeta", "name": rec.name,
                                "pinned": rec.pinned, "archived": rec.archived})
        if rec.writer:
            rec.writer.log({"type": "UserMessage", "content": task})
        rec.mtime = time.time()
        self.hub.broadcast({"type": "SessionsChanged"})
        self.interrupt_event.clear()
        self.hub.broadcast({"type": "UserMessage", "content": task})
        self.run_thread = threading.Thread(target=self._run_worker, args=(task,), daemon=True)
        self.run_thread.start()
        return True, ""

    def _run_worker(self, task: str):
        try:
            result = self.loop.run(task)
        except Exception as e:  # noqa: BLE001
            result = {"status": "error", "message": f"{type(e).__name__}: {e}"}
        # usage 直接用 result 里的（run() 已上报"本轮"用量）。这里**不要**再拿
        # ctx.real_usage 覆盖——那是会话累计值，多轮下会一路累加，看起来像单轮破百万。
        self.hub.broadcast({"type": "RunResult", **result})
        self._persist_active(result)

    def _persist_active(self, result: dict):
        rec = self.workspace.get_active()
        if rec is None or rec.writer is None:
            return
        rec.writer.log({"type": "RunResult", "status": result.get("status"),
                        "message": result.get("message") or result.get("summary"),
                        "steps": result.get("steps"),
                        # 落盘"本轮"用量（result 里已带）；会话累计值 real_usage 不入库，
                        # 免得回放时把跨轮总账误读成单轮消耗
                        "usage": result.get("usage") or dict(self.loop.ctx.round_usage)})
        rec.writer.log({"type": "MessagesDump", "messages": self.loop.ctx.messages})
        rec.messages = list(self.loop.ctx.messages)
        rec.mtime = time.time()
        rec.writer.close()
        rec.writer = None

    def interrupt(self):
        self.interrupt_event.set()
        self.hub.broadcast({"type": "Notice", "message": "已请求中断，将在当前步骤结束后停止"})

    # ---------- 审批 ----------

    def confirm(self, name: str, desc: str) -> bool:
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

    # ---------- 工作区文件浏览 ----------

    def workspace_files(self, rel: str):
        root = (self.workspace.root / rel).resolve()
        try:
            root.relative_to(self.workspace.root)
        except ValueError:
            return {"error": "路径越界"}, 403
        if not root.is_dir():
            return {"error": "目录不存在"}, 404
        entries = []
        for e in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name == ".coding-agent":
                continue  # agent 自身状态目录，不作为用户文件展示
            try:
                size = e.stat().st_size if e.is_file() else None
            except OSError:
                size = None
            entries.append({"name": e.name, "dir": e.is_dir(), "size": size})
        rel_str = str(root.relative_to(self.workspace.root)).replace("\\", "/") or "."
        return {"path": rel_str, "entries": entries}, 200

    def read_file_content(self, rel: str):
        """读取工作区内某个文件的内容，供 Web UI 右侧预览。

        做越界 / 非文件 / 超大 / 二进制 拦截；UTF-8 失败兜底 GBK→latin-1；
        超 4000 行截断，避免一次性把超大文件灌进浏览器。
        """
        root = (self.workspace.root / rel).resolve()
        try:
            root.relative_to(self.workspace.root)
        except ValueError:
            return {"error": "路径越界"}, 403
        if not root.is_file():
            return {"error": "不是文件"}, 404
        try:
            size = root.stat().st_size
        except OSError:
            return {"error": "无法访问该文件"}, 403
        # 超过 512KB 直接拦，避免卡顿
        if size > 512 * 1024:
            return {"error": "文件过大（>512KB），请在编辑器中打开", "size": size}, 413
        data = root.read_bytes()
        # 前 4KB 含空字节 → 视为二进制，不预览
        if b"\x00" in data[:4096]:
            return {"error": "二进制文件，无法预览"}, 415
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("gbk", errors="replace")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="replace")
        MAX_LINES = 4000
        lines = text.split("\n")
        truncated = len(lines) > MAX_LINES
        if truncated:
            text = "\n".join(lines[:MAX_LINES])
        rel_str = str(root.relative_to(self.workspace.root)).replace("\\", "/")
        return {"path": rel_str, "name": root.name, "content": text,
                "truncated": truncated, "size": size}, 200


def fs_browse(path: str):
    """本地文件夹选择器：浏览服务器文件系统（path 为空时列出盘符）。"""
    if not path:
        drives = []
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            try:
                if os.path.exists(d):
                    drives.append({"name": d, "dir": True, "path": d})
            except OSError:
                continue  # 无介质的驱动器可能访问报错，跳过
        return {"path": "", "entries": drives, "isRoot": True}, 200
    p = Path(path)
    if not p.is_dir():
        return {"error": "目录不存在"}, 404
    entries = []
    try:
        itr = sorted(p.iterdir(), key=lambda x: x.name.lower())
    except OSError:
        return {"error": "无法访问该目录"}, 403
    for d in itr:
        if d.is_dir() and not d.name.startswith((".", "$")):
            entries.append({"name": d.name, "dir": True,
                            "path": str(d).replace("\\", "/")})
    return {"path": str(p).replace("\\", "/"), "entries": entries}, 200


def build_handler(server: WebAgentServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentWeb/0.2"

        def log_message(self, fmt, *args):
            pass

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
            ctype = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                     "js": "application/javascript; charset=utf-8"}.get(
                p.suffix.lstrip("."), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # 禁用缓存：静态资源每次都重新拉取，避免浏览器沿用旧的 index/app.js/style.css
            # （否则前端改动即使加了 ?v 戳也可能因 HTML 本身被缓存而加载不到）
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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
                rel = (query.get("path") or [""])[0]
                data, code = server.workspace_files(rel)
                self._json(data, code)
            elif path == "/api/file":
                rel = (query.get("path") or [""])[0]
                data, code = server.read_file_content(rel)
                self._json(data, code)
            elif path == "/api/fs/browse":
                p = (query.get("path") or [""])[0]
                data, code = fs_browse(p)
                self._json(data, code)
            elif path == "/api/workspace":
                self._json(server.workspace_meta())
            elif path == "/api/workspaces":
                self._json(server.list_workspaces())
            elif path == "/api/session/messages":
                fn = (query.get("filename") or [""])[0]
                data, code = server.session_messages(fn)
                self._json(data, code)
            elif path == "/api/session/events":
                fn = (query.get("filename") or [""])[0]
                data, code = server.session_events(fn)
                self._json(data, code)
            elif path == "/api/settings":
                self._json(server.get_settings())
            elif path == "/api/status":
                self._json({"running": server.is_running(), "root": str(server.workspace.root)})
            elif path == "/api/background/tasks":
                loop = server.loop
                tasks = (loop.background.list_tasks()
                         if loop is not None and getattr(loop, "background", None) is not None
                         else [])
                self._json({"tasks": tasks})
            elif path == "/api/subagents":
                loop = server.loop
                subs = loop.list_subagents() if loop is not None and hasattr(loop, "list_subagents") else []
                self._json({"subagents": subs})
            else:
                self.send_error(404)

        def _sse(self):
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
            elif path == "/api/background/stop":
                tid = body.get("task_id", "")
                loop = server.loop
                if loop is not None and getattr(loop, "background", None) is not None and tid:
                    loop.background.stop(tid)
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "message": "no active loop or task"}, 400)
            elif path == "/api/settings":
                self._json(server.update_settings(body))
            elif path == "/api/workspace":
                ok, msg = server.select_workspace(body.get("path", ""))
                if ok:
                    self._json({"ok": True, "workspace": server.workspace_meta()})
                else:
                    self._json({"ok": False, "message": msg}, 400)
            elif path == "/api/session/new":
                server.new_session()
                self._json({"ok": True, "workspace": server.workspace_meta()})
            elif path == "/api/workspace/session/new":
                ok, msg = server.new_session_in(body.get("path", ""))
                if ok:
                    self._json({"ok": True, "workspace": server.workspace_meta()})
                else:
                    self._json({"ok": False, "message": msg}, 400)
            elif path == "/api/session/select":
                ok, msg = server.select_session(body.get("filename", ""))
                if ok:
                    self._json({"ok": True, "workspace": server.workspace_meta()})
                else:
                    self._json({"ok": False, "message": msg}, 400)
            elif path == "/api/workspace/rename":
                ok, msg = server.rename_workspace(body.get("path", ""), body.get("name", ""))
                self._json({"ok": ok, "name": msg} if ok else {"ok": False, "message": msg},
                           200 if ok else 400)
            elif path == "/api/workspace/delete":
                ok, msg = server.remove_workspace(body.get("path", ""))
                self._json({"ok": ok, "switched": msg} if ok else {"ok": False, "message": msg},
                           200 if ok else 400)
            elif path == "/api/session/rename":
                ok, msg = server.rename_session(body.get("root", ""), body.get("filename", ""),
                                                body.get("name", ""))
                self._json({"ok": ok, "name": msg} if ok else {"ok": False, "message": msg},
                           200 if ok else 400)
            elif path == "/api/session/pin":
                ok, msg = server.set_session_flags(body.get("root", ""), body.get("filename", ""),
                                                   pinned=bool(body.get("pinned")))
                self._json({"ok": ok} if ok else {"ok": False, "message": msg}, 200 if ok else 400)
            elif path == "/api/session/archive":
                ok, msg = server.set_session_flags(body.get("root", ""), body.get("filename", ""),
                                                   archived=bool(body.get("archived")))
                self._json({"ok": ok} if ok else {"ok": False, "message": msg}, 200 if ok else 400)
            else:
                self.send_error(404)

    return Handler


def create_server(workspace: Path, loop_factory, port: int = 0):
    """创建并启动 Web 服务器（不阻塞）。返回 (httpd, web_server)。

    loop_factory(on_event, hub, web) -> AgentLoop。
    web 用于取 confirm / interrupt_event。启动即初始化默认工作区与会话。
    """
    from .events import event_to_dict

    hub = EventHub()
    ws = Workspace(workspace)
    web = WebAgentServer(None, hub, ws)

    def on_event(ev):
        d = event_to_dict(ev)
        hub.broadcast(d)
        rec = web.workspace.get_active()
        if rec is not None and rec.writer is not None:
            try:
                rec.writer.log(d)
            except Exception:
                pass

    loop = loop_factory(on_event, hub, web)
    web.bind_loop(loop)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), build_handler(web))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, web


def _build_loop(workspace: Path, args, on_event, hub, web):
    """真实 / 模拟模式的 loop 构造：审批走 Web 弹窗，支持中断。"""
    from .context import Context
    from .loop import AgentLoop
    from .prompts import make_system_prompt
    from .tools import ToolContext, register_all

    register_all()
    # 统一加载配置（读 .env），mock/real 均尊重 AGENT_MAX_CONTEXT_TOKENS 上限
    from .config import Config
    cfg = Config(workspace, model=args.model, base_url=args.base_url, api_key=args.api_key,
                 max_steps=args.max_steps, max_context_tokens=args.budget)
    budget_resolver = None
    window_resolver = None
    if args.mock:
        from .mock import MockLLM
        llm = MockLLM()
    else:
        from .llm import LLMClient
        from .models import context_window_for
        # 固定兜底：CLI 参数 > .env/default（resolver 在 CLI 缺省时再实时读 Web 面板）
        fallback_model = args.model or cfg.model
        fallback_base_url = args.base_url or cfg.base_url
        fallback_api_key = args.api_key or cfg.api_key
        # 热切换 resolver：每次请求实时读 Web 设置面板，改完即对新请求生效，无需重启
        model_resolver = lambda: args.model or web.settings.get("model") or fallback_model
        base_url_resolver = lambda: args.base_url or web.settings.get("model_url") or fallback_base_url
        api_key_resolver = lambda: args.api_key or web.settings.get("model_key") or fallback_api_key
        def max_tokens_resolver():
            # 热切换：每次请求实时读 Web 设置面板，改完即对新请求生效，无需重启
            raw = web.settings.get("max_tokens")
            if raw in (None, ""):
                raw = cfg.max_tokens
            try:
                v = int(raw)
            except (TypeError, ValueError):
                v = 0
            return v if v and v > 0 else cfg.max_tokens
        if not api_key_resolver():
            raise SystemExit(
                "未找到 API key：请设置环境变量 AGENT_API_KEY / DEEPSEEK_API_KEY，"
                "或在工作区 .env 中提供，或在 Web 设置面板填写 API Key"
                "（.env 已被 .gitignore 排除，不会入库）。")
        llm = LLMClient(base_url=fallback_base_url, api_key=fallback_api_key,
                        model=fallback_model,
                        temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.timeout,
                        model_resolver=model_resolver, base_url_resolver=base_url_resolver,
                        api_key_resolver=api_key_resolver, max_tokens_resolver=max_tokens_resolver)
    # 有效上限：用户设置的硬上限优先，否则模型真实窗口（mock/未知回退 56000）。
    # 预算 = 有效上限 * 0.90，即保留 10% 安全余量、压缩触发点落在 90%（设上限或满窗口统一）。
    # 优先读 Web 设置面板（可热切换，改完即对新请求生效），回退 .env/default。
    def _resolve_cap():
        raw = web.settings.get("max_context_tokens")
        if raw in (None, ""):
            raw = cfg.max_context_tokens
        try:
            v = int(raw)
        except (TypeError, ValueError):
            v = 0
        return v if v and v > 0 else None
    def _resolve_effective_window():
        """有效上限（token）：cap 优先；否则取模型真实窗口（mock/未知回退 56000）。
        同时作为预算分母与 UI 显示上限；cap 不超过真实窗口以防越界 400。"""
        cap = _resolve_cap()
        real = context_window_for(model_resolver()) if not args.mock else None
        if cap:
            return min(cap, real) if real else cap
        return real or 56000
    def budget_resolver():
        eff = _resolve_effective_window()
        # 保留 10% 安全余量：压缩触发点 = 90% 有效窗口（cap 或真实窗口）
        return max(1024, int(eff * 0.90))
    def window_resolver():
        # 有效上限（cap 或真实窗口），前端悬停以此做分母，统一显示"设置值/完整窗口"
        return _resolve_effective_window()
    budget_cap = _resolve_cap()
    budget = max(1024, int((budget_cap or 56000) * 0.90))  # 固定兜底（resolver 缺省时）
    # 授权/计划：Web UI 运行时设置 > CLI 参数（--permission ask / --plan）> 默认 auto
    permission = web.settings.get("permission")
    if not permission:
        permission = "plan" if args.plan else ("ask" if args.permission == "ask" else "auto")
    plan = permission == "plan"
    # plan 模式也要挂 confirm：计划交出后靠它弹窗征求批准，批准后才转入执行阶段
    confirm = web.confirm if permission in ("ask", "plan") else None
    ctx = Context(make_system_prompt(workspace), budget, budget_resolver=budget_resolver,
                  window_resolver=window_resolver)
    # 步数上限：CLI 参数 > 面板设置 > .env/默认（面板改动会同步 .env 并热生效，见 update_settings）
    max_steps = args.max_steps
    if not max_steps:
        try:
            max_steps = int(web.settings.get("max_steps") or 0)
        except (TypeError, ValueError):
            max_steps = 0
        max_steps = max_steps if max_steps > 0 else cfg.max_steps
    return AgentLoop(llm, ctx, ToolContext(workspace), max_steps=max_steps,
                     on_event=on_event, confirm=confirm, plan_mode=plan,
                     interrupt_event=web.interrupt_event)


def run_server(workspace: Path, args, port: int = 8080, open_browser: bool = True) -> None:
    """启动 Web 服务器并阻塞（cli 的 --web 入口）。"""
    def factory(on_event, hub, web):
        # 用「当前激活工作区」读 .env（含 AGENT_MAX_CONTEXT_TOKENS 上限），而非 server 启动目录，
        # 否则在 coding-agent 启动、UI 切到 test 时仍读 coding-agent/.env，压窗设置不生效。
        return _build_loop(web.workspace.root, args, on_event, hub, web)

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
