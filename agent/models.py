"""主流模型上下文窗口注册表（2026 年最新，供预算热切换查询）。

- Web 设置面板切换模型时，按 model id 查表得到真实上下文窗口，再扣除输出上限
  与安全余量得到实际预算（budget）；未命中则回退到配置固定值。
- 数据来源：各厂商官方文档 / 主流比价站（2026-06 ~ 2026-08 核实）。
- 仅依赖标准库；被 web.py / context.py 在请求时实时查，不阻塞主循环。
"""
import re as _re
from typing import Optional

# 快照日期后缀（如 qwen3.7-max-2026-06-08 / claude-opus-4-8-20260601）：去掉后回退精确匹配
_SNAP_RE = _re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")

# 模型上下文窗口（单位：token）。优先精确 id，未命中走 family_fallback 兜底。
MODEL_CONTEXT_WINDOWS = {
    # ---- DeepSeek ----
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-flash-vision-exp": 1_000_000,
    # ---- 阿里通义千问 Qwen（DashScope）----
    "qwen-max": 128_000,
    "qwen-plus": 128_000,
    "qwen-turbo": 128_000,
    "qwen3-max": 128_000,
    "qwen3-coder-plus": 128_000,
    "qwen3.6-flash": 128_000,
    "qwen3.5-plus": 1_000_000,
    "qwen3.7-max": 1_000_000,
    "qwen3.8-max": 1_000_000,
    "qwen-long": 1_000_000,
    # ---- OpenAI ----
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 128_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.5": 1_000_000,
    "gpt-5.6": 1_000_000,
    # ---- Anthropic Claude ----
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-5": 1_000_000,
    # ---- Google Gemini ----
    "gemini-2.5-pro": 1_000_000,
    "gemini-3.1-pro": 1_000_000,
    "gemini-3.1-flash": 1_000_000,
    "gemini-3.5-flash": 1_000_000,
    "gemini-3.5-flash-lite": 1_000_000,
    # ---- Z.AI GLM ----
    "glm-5.3-flash": 1_000_000,
    "glm-5.3": 1_000_000,
    "glm-5.2": 1_000_000,
    "glm-5.1": 200_000,
    # ---- Moonshot Kimi ----
    "kimi-k2.5": 256_000,
    "kimi-k2.6": 256_000,
    "kimi-k3": 1_000_000,
    # ---- MiniMax ----
    "minimax-m3": 1_000_000,
    # ---- xAI Grok ----
    "grok-4": 500_000,
    "grok-4.2": 1_000_000,
    # ---- Mistral ----
    "mistral-large-2": 128_000,
    "mistral-medium-3.5": 256_000,
    # ---- Meta Llama ----
    "llama-4-maverick": 256_000,
    "llama-4-scout": 10_000_000,
}


def family_fallback(model: str) -> Optional[int]:
    """未精确命中时，按模型家族前缀兜底（尽量保守，避免高估窗口导致 400）。"""
    m = (model or "").lower()
    if m.startswith("deepseek"):
        return 128_000
    if m.startswith("qwen"):
        return 128_000  # 大多数 qwen 标准版 128K；长上下文版已在表中精确列出
    if m.startswith("gpt-5"):
        return 1_000_000
    if m.startswith("gpt-4o"):
        return 128_000
    if m.startswith("claude"):
        return 200_000  # 保守：旧版 200K，新版已在表中精确列出
    if m.startswith("gemini"):
        return 1_000_000
    if m.startswith("glm-5"):
        return 1_000_000
    if m.startswith("glm-4"):
        return 200_000
    if m.startswith("kimi"):
        return 256_000
    if m.startswith("minimax"):
        return 1_000_000
    if m.startswith("grok"):
        return 500_000
    return None


def context_window_for(model: str) -> Optional[int]:
    """返回模型上下文窗口（token）；未知模型返回 None（调用方回退固定预算）。

    匹配顺序：① 原始 id 精确命中；② 去掉快照日期后缀（如
    qwen3.7-max-2026-06-08 -> qwen3.7-max）再精确/家族匹配；③ 原始 id 家族兜底。
    这样带日期的快照模型也能拿到真实窗口，而非被家族兜底给保守的 128K。
    """
    if not model:
        return None
    # ① 精确命中
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]
    # ② 去快照日期后缀再匹配
    base = _SNAP_RE.sub("", model)
    if base and base != model:
        win = MODEL_CONTEXT_WINDOWS.get(base) or family_fallback(base)
        if win:
            return win
    # ③ 原始 id 家族兜底（保守）
    return family_fallback(model)


def budget_for_window(window: int, max_tokens: int) -> int:
    """由真实窗口推导实际预算：扣除输出上限 + 安全余量（防 heuristic 低估导致 400）。"""
    reserve = max(2048, int(window * 0.04))
    return max(4096, window - max_tokens - reserve)
