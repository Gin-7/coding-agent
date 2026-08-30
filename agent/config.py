"""配置加载：环境变量优先，.env 文件兜底（.env 不入库，凭据绝不进代码）。

agent 自身的状态（会话日志、记忆、编辑备份、面板设置、工作区注册表）统一放在
工作区的 .coding-agent/ 目录下，不与用户文件混放。
"""
import os
from pathlib import Path
from typing import Optional

STATE_DIR_NAME = ".coding-agent"

DEFAULTS = {
    "AGENT_MODEL": "deepseek-chat",
    "AGENT_BASE_URL": "https://api.deepseek.com",
    "AGENT_TEMPERATURE": "0.0",
    "AGENT_MAX_TOKENS": "4096",
    "AGENT_MAX_CONTEXT_TOKENS": "0",
    "AGENT_MAX_STEPS": "50",
    "AGENT_TIMEOUT": "120",
}


def state_dir(workspace: Path) -> Path:
    """工作区的 agent 状态目录（<ws>/.coding-agent/）。"""
    return Path(workspace) / STATE_DIR_NAME


def is_source_repo(workspace: Path) -> bool:
    """哨兵检测：该目录是否为 agent 自身的源码仓库。"""
    ws = Path(workspace)
    return (ws / "agent" / "web.py").is_file() and (ws / "agent" / "__main__.py").is_file()


def first_launch_workspace(workspace: Path, explicit: bool = False) -> Path:
    """Web 首启工作区解析：在源码仓库内启动时指向干净沙箱，仓库本身不再作为默认工作区。

    理由：README 场景在仓库内启动，CWD 约定会把 agent 源码当成默认工作区
    （auto-edit 下有误改自身的风险）。重定向目标固定为
    <仓库>/.coding-agent/default-workspace（gitignored），跨次启动稳定；
    全局设置/注册表随之放在仓库的 .coding-agent/ 下。用户自己的项目目录
    不含哨兵文件，行为不变；显式传入 --workspace 时完全尊重。
    """
    ws = Path(workspace).resolve()
    if explicit or not is_source_repo(ws):
        return ws
    sandbox = state_dir(ws) / "default-workspace"
    sandbox.mkdir(parents=True, exist_ok=True)
    return sandbox


def prepare_state_dir(workspace: Path) -> Path:
    """确保状态目录存在；新工作区立即预置空 .env（面板写入不会越界到上级）。

    同时把旧版散落在工作区根目录的状态一次性迁入（目标已存在则不覆盖）：
    sessions/、.env、.agent-backups/、.agent-memory.md。
    """
    state = state_dir(workspace)
    state.mkdir(exist_ok=True)

    def move_file(name: str, force_over_empty: bool = False) -> None:
        legacy = workspace / name
        target = state / name
        if not legacy.is_file() or target.exists():
            return
        if force_over_empty and target.exists() and target.stat().st_size > 0:
            return
        legacy.replace(target)

    def move_dir(name: str) -> None:
        legacy = workspace / name
        target = state / name
        if not legacy.is_dir() or target.exists():
            return
        legacy.replace(target)

    move_file(".env", force_over_empty=True)  # 根目录旧 .env 优先于预置的空文件
    if not (state / ".env").exists():
        (state / ".env").touch()
    move_dir("sessions")
    move_dir(".agent-backups")
    move_file(".agent-memory.md")
    move_file(".agent-settings.json")
    move_file(".agent-workspaces.json")
    return state


def load_dotenv(path: Path) -> None:
    """极简 .env 解析（不引第三方依赖）。已存在的环境变量优先，不覆盖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def find_env_files(start: Path) -> list:
    """就近优先的 .env 候选链：每级目录先 .coding-agent/.env 再 .env，逐级向上。

    链式而非首个命中：子目录工作区预置了自己的空 .env 后，仍能从上级继承
    API key 等配置（setdefault 语义保证近处优先）。
    """
    chain, seen = [], set()
    p = Path(start).resolve()
    while True:
        for cand in (p / STATE_DIR_NAME / ".env", p / ".env"):
            if cand.exists() and str(cand) not in seen:
                seen.add(str(cand))
                chain.append(cand)
        if p.parent == p:
            break
        p = p.parent
    if not chain:
        chain.append(Path(start).resolve() / STATE_DIR_NAME / ".env")
    return chain


def find_env_file(start: Path) -> Path:
    """返回就近的一个 .env 路径（存在或应创建的位置），供写入方使用。"""
    return find_env_files(start)[0]


class Config:
    def __init__(self, workspace: Path, env_file: Optional[Path] = None, **overrides):
        if env_file is not None:
            load_dotenv(env_file)
        else:
            for f in find_env_files(workspace):
                load_dotenv(f)
        self.workspace = workspace.resolve()
        self.model = os.environ.get("AGENT_MODEL", DEFAULTS["AGENT_MODEL"])
        self.base_url = os.environ.get("AGENT_BASE_URL", DEFAULTS["AGENT_BASE_URL"])
        self.api_key = os.environ.get("AGENT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
        self.temperature = float(os.environ.get("AGENT_TEMPERATURE", DEFAULTS["AGENT_TEMPERATURE"]))
        self.max_tokens = int(os.environ.get("AGENT_MAX_TOKENS", DEFAULTS["AGENT_MAX_TOKENS"]))
        self.max_context_tokens = int(os.environ.get("AGENT_MAX_CONTEXT_TOKENS", DEFAULTS["AGENT_MAX_CONTEXT_TOKENS"]))
        self.max_steps = int(os.environ.get("AGENT_MAX_STEPS", DEFAULTS["AGENT_MAX_STEPS"]))
        self.timeout = int(os.environ.get("AGENT_TIMEOUT", DEFAULTS["AGENT_TIMEOUT"]))
        # CLI 参数覆盖（None 表示未指定，不覆盖）
        for k, v in overrides.items():
            if v is not None:
                setattr(self, k, v)
