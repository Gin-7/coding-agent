"""对话历史与上下文管理（核心自研逻辑之一）。

双轨 token 计量：
1. 真实计量：每次 API 返回的 usage 累计进历史（权威依据）
2. 启发式预检：发送前估算（CJK ≈ 1.5 token/字，ASCII ≈ 4 字符/token），超预算提前处理

超预算处理（D1 先实现"整轮裁剪"，compaction 压缩在 D4 实现）：
- 以"轮"为单位裁剪最老的 tool 调用轮（assistant tool_calls + 全部 tool 结果），
  保证 API 对 tool 消息与 tool_calls 的配对约束不被破坏。
"""
from typing import Optional


def estimate_tokens(text: str) -> int:
    """启发式 token 估算：中文为主场景，粗粒度即可（真实 usage 负责校准）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text
              if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af")
    return int(cjk * 1.5 + (len(text) - cjk) / 4) + 1


class Context:
    def __init__(self, system_prompt: str, budget: int, budget_resolver: Optional[callable] = None):
        self.messages: list = [{"role": "system", "content": system_prompt}]
        self.budget = budget
        self.budget_resolver = budget_resolver
        self.real_usage = {"prompt": 0, "completion": 0, "total": 0}

    # ---------- 写入 ----------

    def add(self, message: dict) -> None:
        self.messages.append(message)

    def record_usage(self, usage: Optional[dict]) -> None:
        if not usage:
            return
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(k)
            if v:
                self.real_usage[k.replace("_tokens", "")] += v

    # ---------- 计量 ----------

    def estimated_tokens(self) -> int:
        total = 0
        for m in self.messages:
            total += estimate_tokens(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                total += estimate_tokens(tc.get("function", {}).get("arguments") or "")
        return total

    def _cur_budget(self) -> int:
        """实时预算：resolver 返回正值则用当前模型窗口推导值，否则退化为构造时固定值。"""
        if self.budget_resolver is not None:
            v = self.budget_resolver()
            if v and v > 0:
                return v
        return self.budget

    def needs_trim(self) -> bool:
        return self.estimated_tokens() > self._cur_budget()

    # ---------- 裁剪 ----------

    def trim_old_tool_rounds(self) -> int:
        """从最老位置开始整轮裁剪（assistant tool_calls + 其全部 tool 结果），返回裁剪轮数。

        只处理第一条 user 消息之后的早期轮次；**最新一轮不裁剪**（当前任务需要，
        且 API 要求 tool 消息与其 assistant tool_calls 严格配对，半轮裁剪会破坏约束）。
        """
        first_user = next((i for i, m in enumerate(self.messages) if m["role"] == "user"), 1)
        # 定位最新一轮（最后一个带 tool_calls 的 assistant 消息），该轮整体保留
        last_round_idx = None
        for i in range(len(self.messages) - 1, first_user - 1, -1):
            if self.messages[i].get("tool_calls"):
                last_round_idx = i
                break
        head = self.messages[:first_user]
        tail: list = []  # (原始下标, 消息) —— 用原始下标判断是否为最新一轮
        removed = 0
        i = first_user
        while i < len(self.messages):
            m = self.messages[i]
            # 命中"tool 结果紧跟在 assistant tool_calls 之后" → 这是一轮的开始
            if (m["role"] == "tool" and tail and tail[-1][1]["role"] == "assistant"
                    and tail[-1][1].get("tool_calls")):
                if tail[-1][0] == last_round_idx:
                    # 最新一轮：不裁剪，整体保留
                    tail.append((i, m))
                    i += 1
                    continue
                tail.pop()
                removed += 1
                while i < len(self.messages) and self.messages[i]["role"] == "tool":
                    i += 1
                continue
            tail.append((i, m))
            i += 1
        self.messages = head + [m for _, m in tail]
        return removed

    def trim_to_budget(self) -> int:
        """循环裁剪直到预估 token 低于预算；无可裁剪项时停止。返回裁剪轮数。"""
        total = 0
        while self.needs_trim():
            removed = self.trim_old_tool_rounds()
            if removed == 0:
                break
            total += removed
        return total

    # ---------- 压缩 / 截断 ----------

    def find_compaction_boundary(self, keep_recent_rounds: int = 2) -> int:
        """压缩边界：保留 [boundary, end)，压缩/丢弃 [first_user+1, boundary)。

        边界定位：从尾部数第 keep_recent_rounds 个 assistant tool_calls 消息；
        轮数不足时退到 first_user+1（无可压缩区域）。
        """
        first_user = next((i for i, m in enumerate(self.messages) if m["role"] == "user"), 1)
        count = 0
        for i in range(len(self.messages) - 1, first_user, -1):
            if self.messages[i].get("tool_calls"):
                count += 1
                if count == keep_recent_rounds:
                    return i
        return first_user + 1

    def region_before(self, boundary: int) -> list:
        """压缩区域内消息：[first_user+1, boundary)（不含任务描述本身）。"""
        first_user = next((i for i, m in enumerate(self.messages) if m["role"] == "user"), 1)
        return self.messages[first_user + 1:boundary]

    def apply_compaction(self, boundary: int, summary: str) -> int:
        """用摘要替换压缩区域，返回被替换的消息数（0 表示无可替换）。"""
        first_user = next((i for i, m in enumerate(self.messages) if m["role"] == "user"), 1)
        removed = boundary - (first_user + 1)
        if removed <= 0:
            return 0
        note = {"role": "user", "content": "【早期对话摘要】\n" + summary}
        self.messages = self.messages[:first_user + 1] + [note] + self.messages[boundary:]
        return removed

    def hard_truncate(self, keep_recent_rounds: int = 2) -> int:
        """最后手段：不生成摘要，直接丢弃压缩区域，返回丢弃消息数。"""
        first_user = next((i for i, m in enumerate(self.messages) if m["role"] == "user"), 1)
        boundary = self.find_compaction_boundary(keep_recent_rounds)
        removed = boundary - (first_user + 1)
        if removed <= 0:
            return 0
        note = {"role": "user", "content": "（早期执行记录已因上下文预算被丢弃）"}
        self.messages = self.messages[:first_user + 1] + [note] + self.messages[boundary:]
        return removed
