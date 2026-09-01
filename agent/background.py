"""后台任务管理：启动长命令不阻塞主循环，输出实时泄到事件流，可轮询/停止。

线程安全：每任务一个读线程 + 锁保护的环形缓冲（有界，防爆内存）。
任务在会话内存活（各轮之间可查/可停）；由调用方在停止时清理。
"""
import subprocess
import threading
import uuid
from collections import deque

from .tools.shell_tools import CREATE_NEW_PROCESS_GROUP, _decode

MAX_BUFFER_CHARS = 4000


class BackgroundManager:
    def __init__(self):
        self._tasks = {}
        self._lock = threading.Lock()
        self._counter = 0
        # 每次运行（每个 AgentLoop）独立的前缀：后台任务 id 在「同一次运行」内
        # 递增，但跨运行（同一会话文件拼接多轮时）不再唯一。加 per-manager 前缀，
        # 保证全会话/全进程内 id 不会重复，避免回放时 Map 键覆盖与孤儿进程歧义。
        self._uid = uuid.uuid4().hex[:6]
        # 由主循环注入的回调
        self.on_output = None  # Callable(task_id, text) 逐行实时输出
        self.emit = None       # Callable(dict) 状态事件

    def start(self, command: str, cwd, timeout: int = None):
        """启动后台命令，立即返回 (结果dict, err)。"""
        with self._lock:
            self._counter += 1
            task_id = f"bg-{self._uid}-{self._counter}"
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(cwd),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NEW_PROCESS_GROUP,
            )
        except Exception as e:
            return {"error": f"启动失败: {e}"}, str(e)
        task = {"proc": proc, "buffer": deque(maxlen=MAX_BUFFER_CHARS), "status": "running",
                "exit": None, "command": command, "pid": proc.pid}
        with self._lock:
            self._tasks[task_id] = task
        threading.Thread(target=self._read, args=(task_id, task), daemon=True).start()
        if self.emit:
            try:
                self.emit({"type": "BackgroundStarted", "task_id": task_id,
                           "command": command, "pid": proc.pid})
            except Exception:
                pass
        return {"task_id": task_id, "pid": proc.pid}, None

    def _read(self, task_id: str, task: dict) -> None:
        proc = task["proc"]
        code = None
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                text = _decode(line)
                with self._lock:
                    task["buffer"].append(text)
                if self.on_output:
                    try:
                        self.on_output(task_id, text)
                    except Exception:
                        pass
            proc.wait()
            code = proc.returncode
        except Exception:
            code = None
        with self._lock:
            task["status"] = "done"
            task["exit"] = code
        if self.emit:
            try:
                self.emit({"type": "BackgroundStatus", "task_id": task_id,
                           "status": "done", "exit_code": code})
            except Exception:
                pass

    def poll(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {"error": "任务不存在"}
            buf = "".join(task["buffer"])
            return {"task_id": task_id, "status": task["status"],
                    "exit_code": task["exit"], "output": buf}

    def stop(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {"error": "任务不存在"}
        try:
            subprocess.run(["taskkill", "/pid", str(task["proc"].pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        with self._lock:
            task["status"] = "stopped"
        if self.emit:
            try:
                self.emit({"type": "BackgroundStatus", "task_id": task_id,
                           "status": "stopped", "exit_code": None})
            except Exception:
                pass
        return {"task_id": task_id, "status": "stopped"}

    def list_tasks(self):
        with self._lock:
            return [{"task_id": tid, "status": t["status"], "exit_code": t["exit"],
                     "command": t["command"], "pid": t["pid"],
                     "output": "".join(t["buffer"])}
                    for tid, t in self._tasks.items()]

    def close(self):
        """停止所有后台任务（server 退出 / 会话清理时调用）。"""
        with self._lock:
            ids = list(self._tasks.keys())
        for tid in ids:
            try:
                self.stop(tid)
            except Exception:
                pass
