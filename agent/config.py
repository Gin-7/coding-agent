"""配置加载：环境变量优先，.env 文件兜底（.env 不入库，凭据绝不进代码）。"""
import os
from pathlib import Path
from typing import Optional

DEFAULTS = {
    "AGENT_MODEL": "deepseek-chat",
    "AGENT_BASE_URL": "https://api.deepseek.com",
    "AGENT_TEMPERATURE": "0.0",
    "AGENT_MAX_TOKENS": "4096",
    "AGENT_MAX_CONTEXT_TOKENS": "56000",
    "AGENT_MAX_STEPS": "30",
    "AGENT_TIMEOUT": "120",
}


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


def find_env_file(start: Path) -> Path:
    """从 start 向上逐级找 .env（工作区或上级目录均可）。"""
    p = start
    while True:
        cand = p / ".env"
        if cand.exists():
            return cand
        if p.parent == p:
            break
        p = p.parent
    return start / ".env"


class Config:
    def __init__(self, workspace: Path, env_file: Optional[Path] = None, **overrides):
        load_dotenv(env_file or find_env_file(workspace))
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
