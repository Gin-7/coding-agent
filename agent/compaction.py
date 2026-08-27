"""上下文压缩（compaction）：把早期对话压缩为结构化摘要（核心自研逻辑之一）。

三层预算策略中的第 1 层（优先，保留语义）：把最近 K 轮之前的早期消息
用一次独立 LLM 调用压成摘要，替换进历史（Claude Code 同款思路）。
压缩区域过大时分块压缩再合并，控制单次调用规模。
压缩失败 / 区域过小时返回 0，由调用方走裁剪与硬截断兜底。
"""
from .context import estimate_tokens

COMPACT_PROMPT = """请把下面的早期 agent 执行记录压缩为简洁的结构化摘要，供继续执行任务参考。

要求：
- 用要点列出：已完成的关键操作与结果、工作区当前状态（已创建/修改的文件）、尚未完成的事项
- 保留关键数据（路径、命令、关键数字、报错原因）
- 总长度控制在 500 字以内，不要编造记录中不存在的信息

记录开始
{records}
记录结束
"""

MERGE_PROMPT = """以下是同一段执行记录的多个分块摘要，请合并为一份简洁、无重复的结构化摘要（500 字以内）。

{chunks}
"""


def _format_message(m: dict, max_content: int = 300) -> str:
    role = m.get("role", "?")
    if role == "tool":
        return f"[工具结果] {m.get('content', '')[:max_content]}"
    if role == "assistant":
        parts = [m.get("content", "")]
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            parts.append(f"[调用 {fn.get('name')}: {fn.get('arguments', '')[:max_content]}]")
        return "[助手] " + " ".join(parts)[:max_content * 2]
    return f"[{role}] {m.get('content', '')[:max_content]}"


def _serialize(messages: list) -> str:
    return "\n".join(_format_message(m) for m in messages)


def _summarize(llm, text: str) -> str:
    result = llm.chat([{"role": "user", "content": COMPACT_PROMPT.format(records=text)}])
    return (result.get("content") or "").strip()


def compact_history(ctx, llm, budget: int, keep_recent_rounds: int = 2,
                    chunk_ratio: float = 0.4, min_region_tokens: int = 512) -> int:
    """把最近窗口之前的早期消息压缩为摘要。成功返回替换掉的消息数，否则返回 0。

    任何异常（LLM 不可用 / 调用失败）都不会向上抛——由调用方走裁剪与硬截断兜底。
    """
    try:
        boundary = ctx.find_compaction_boundary(keep_recent_rounds)
        region = ctx.region_before(boundary)
        if not region:
            return 0
        region_tokens = sum(estimate_tokens(_format_message(m)) for m in region)
        if region_tokens < min_region_tokens:
            return 0  # 区域太小，不值得一次压缩调用（也防止摘要被反复再摘要）

        # 分块压缩，控制单次调用规模
        chunk_limit = max(1000, int(budget * chunk_ratio))
        chunks: list = []
        current: list = []
        current_tokens = 0
        for m in region:
            t = estimate_tokens(_format_message(m))
            if current and current_tokens + t > chunk_limit:
                chunks.append(current)
                current, current_tokens = [], 0
            current.append(m)
            current_tokens += t
        if current:
            chunks.append(current)

        summaries = [_summarize(llm, _serialize(chunk)) for chunk in chunks]
        if len(summaries) > 1:
            merged = llm.chat([{"role": "user", "content": MERGE_PROMPT.format(chunks="\n\n".join(summaries))}])
            final = (merged.get("content") or "").strip() or "\n".join(summaries)
        else:
            final = summaries[0]
        if not final:
            return 0
        return ctx.apply_compaction(boundary, final)
    except Exception:  # noqa: BLE001 —— 压缩失败不阻塞主循环，交给兜底策略
        return 0
