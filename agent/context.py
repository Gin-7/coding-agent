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
    """启发式 token 估算：中文为主场景，粗粒度即可（真实 usage 负责校准）。

    非中文（代码/英文）按 ~3 字符/token 估算——之前用 /4 严重低估，导致代码类内容
    真实 token 数被少算约 12×，压缩预算形同虚设。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text
              if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af")
    return int(cjk * 1.5 + (len(text) - cjk) / 3) + 1


class Context:
    def __init__(self, system_prompt: str, budget: int, budget_resolver: Optional[callable] = None,
                 window_resolver: Optional[callable] = None):
        self.messages: list = [{"role": "system", "content": system_prompt}]
        self.budget = budget
        self.budget_resolver = budget_resolver
        self.window_resolver = window_resolver
        # 两个口径的用量统计，别混用：
        #   real_usage  —— **会话累计**（跨轮累加，不重置）。REPL 的 /stats 要看这个。
        #   round_usage —— **本轮**（每次 AgentLoop.run() 开始时重置）。RunResult 上报这个。
        # 早先 RunResult 直接报 real_usage，多轮会话下会看到 prompt 一路涨到上百万，
        # 其实是累计值被当成了单轮用量。
        self.real_usage = {"prompt": 0, "completion": 0, "total": 0}
        self.round_usage = {"prompt": 0, "completion": 0, "total": 0}
        # 最近一次请求的真实 prompt token 数（API 回传的权威上下文大小）。
        self.last_prompt_tokens = 0
        # 基线开销 = 真实 prompt_tokens − 当时消息的启发式估算。
        # 即 system prompt + 工具 schema + 消息框架这些"启发式看不见"的固定开销。
        #
        # 为什么要拆开：旧实现直接把 last_prompt_tokens 当估算值返回，两者尺度不同
        # （权威值含基线、启发式只算消息），混用会同时踩两个坑——
        #   ① 追加超大工具结果后估算不涨（盲区），直到下一次 API 调用才发现超预算；
        #   ② 裁剪/压缩后估算不降，needs_trim() 恒真 → 一路裁到光再级联硬截断。
        # 拆成"基线 + 启发式(当前消息)"后，两个坑同时消失：消息一变，估算立刻跟着变。
        self._baseline_tokens = 0

    # ---------- 写入 ----------

    def add(self, message: dict) -> None:
        self.messages.append(message)

    def reset_round_usage(self) -> None:
        """开始新的一轮 run()：本轮用量清零（会话累计值不受影响）。"""
        self.round_usage = {"prompt": 0, "completion": 0, "total": 0}

    def record_usage(self, usage: Optional[dict]) -> None:
        if not usage:
            return
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(k)
            if v:
                key = k.replace("_tokens", "")
                self.real_usage[key] += v      # 会话累计
                self.round_usage[key] += v     # 本轮
        pt = usage.get("prompt_tokens")
        if pt:
            self.last_prompt_tokens = pt
            # 用权威值反推基线：真实总量 − 本次请求里那些消息的启发式估算 = 固定开销。
            # 此刻 self.messages 正是刚发出去的那批（主循环在写回 assistant 消息前调用），
            # 所以两者可直接相减。
            self._baseline_tokens = max(0, pt - self._heuristic_tokens())

    # ---------- 计量 ----------

    @staticmethod
    def _message_tokens(m: dict) -> int:
        """单条消息的启发式估算（正文 + 各 tool_call 的参数）。"""
        total = estimate_tokens(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            total += estimate_tokens(tc.get("function", {}).get("arguments") or "")
        return total

    def _heuristic_tokens(self) -> int:
        """当前全部消息的启发式估算（不含 system prompt / 工具 schema 等基线开销）。"""
        return sum(self._message_tokens(m) for m in self.messages)

    def estimated_tokens(self) -> int:
        """上下文估算 = 基线开销 + 当前消息的启发式估算。

        首次 API 调用前基线为 0（纯启发式，会略微低估，但此时也无可裁剪内容）。
        """
        return self._baseline_tokens + self._heuristic_tokens()

    def _cur_budget(self) -> int:
        """实时预算：resolver 返回正值则用当前模型窗口推导值，否则退化为构造时固定值。"""
        if self.budget_resolver is not None:
            v = self.budget_resolver()
            if v and v > 0:
                return v
        return self.budget

    def _cur_window(self) -> Optional[int]:
        """真实模型窗口（token）：resolver 返回正值则用当前模型窗口，否则 None（未知）。"""
        if self.window_resolver is not None:
            v = self.window_resolver()
            if v and v > 0:
                return v
        return None

    def needs_trim(self) -> bool:
        return self.estimated_tokens() > self._cur_budget()

    # ---------- 裁剪 ----------

    def trim_old_tool_rounds(self, max_rounds: int = 1) -> int:
        """从最老位置开始整轮裁剪（assistant tool_calls + 其全部 tool 结果），返回裁剪轮数。

        只处理第一条 user 消息之后的早期轮次；**最新一轮不裁剪**（当前任务需要，
        且 API 要求 tool 消息与其 assistant tool_calls 严格配对，半轮裁剪会破坏约束）。

        max_rounds 默认 1：一次只裁最老的一轮。**这一点很关键**——早先版本一次
        扫描就把所有可裁剪轮次全清掉，导致 trim_to_budget() 里"裁到低于预算为止"
        的循环形同虚设（第一次调用就清空、第二轮无货可裁直接 break），历史被
        无谓清空。改成每次只裁一轮后，调用方才能边裁边重算、够用即止。
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
                if removed >= max_rounds:
                    break  # 裁够就收手，把剩余部分留给下一轮判断
                continue
            tail.append((i, m))
            i += 1
        # break 提前退出时，i 之后尚未扫描的消息要原样保留
        tail.extend((j, self.messages[j]) for j in range(i, len(self.messages)))
        self.messages = head + [m for _, m in tail]
        return removed

    def trim_to_budget(self) -> int:
        """循环裁剪直到预估 token 低于预算；无可裁剪项时停止。返回裁剪轮数。

        每裁一轮就用启发式重算（估算随消息变化即时下降），因此是真正的
        "裁到够用为止"，不会像旧实现那样一路裁光再级联硬截断。
        """
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
        当前任务保护：当前任务指令 = 列表里最后一个 user 消息。压缩区不得覆盖它，
        否则正在执行的任务会被压进摘要、agent 表现为「遗忘当前任务」（多任务连跑
        场景常见）。仅当存在更早任务（last_user > first_user）时，把边界收拢到
        last_user，使当前任务（指令 + 其全部轮次）完整保留在尾部。
        """
        first_user = next((i for i, m in enumerate(self.messages) if m["role"] == "user"), 1)
        # 当前任务指令 = 列表里最后一个 user 消息
        last_user = first_user
        for i in range(len(self.messages) - 1, first_user - 1, -1):
            if self.messages[i]["role"] == "user":
                last_user = i
                break
        boundary = first_user + 1
        count = 0
        for i in range(len(self.messages) - 1, first_user, -1):
            if self.messages[i].get("tool_calls"):
                count += 1
                if count == keep_recent_rounds:
                    boundary = i
                    break
        # 多任务会话：把边界收拢到当前任务指令之前，避免把当前任务压进摘要
        if last_user > first_user:
            boundary = min(boundary, last_user)
        return boundary

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
        # 历史被替换后，上一次真实请求的读数已不再代表现状，置 0 表示"暂无权威读数"
        # （估算由 _baseline_tokens + 启发式承担，不依赖这个值；UI 的"全新会话"判定会看它）
        self.last_prompt_tokens = 0
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
        # 同 apply_compaction：历史被替换，上一次真实读数失效，置 0
        self.last_prompt_tokens = 0
        return removed
