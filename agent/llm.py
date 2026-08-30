"""自写 OpenAI 兼容客户端：POST {base_url}/chat/completions，支持 SSE 流式与原生 tool calling。

不依赖 openai SDK —— 我们清楚 wire protocol 长什么样，出问题可完全掌控。
允许使用模型厂商 API 客户端库，这里选择自写以展示对协议的理解。
"""
import json
import time
from typing import Any, Iterator, Optional

import requests


class LLMError(Exception):
    """不可重试的最终错误。"""


class LLMRetryableError(LLMError):
    """可重试的临时错误（429 / 5xx / 网络抖动）。"""


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 120,
        max_retries: int = 3,
        model_resolver: Optional[callable] = None,
        base_url_resolver: Optional[callable] = None,
        api_key_resolver: Optional[callable] = None,
        max_tokens_resolver: Optional[callable] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        # 热切换：resolver 在每次请求时实时解析最新值（CLI 进程/无面板时留空即退化为固定值）
        self.model_resolver = model_resolver
        self.base_url_resolver = base_url_resolver
        self.api_key_resolver = api_key_resolver
        self.max_tokens_resolver = max_tokens_resolver

    # ---------- 热切换解析 ----------

    def _cur_model(self) -> str:
        if self.model_resolver is not None:
            v = self.model_resolver()
            if v:
                return v
        return self.model

    def _cur_base_url(self) -> str:
        if self.base_url_resolver is not None:
            v = self.base_url_resolver()
            if v:
                return v.rstrip("/")
        return self.base_url

    def _cur_api_key(self) -> str:
        if self.api_key_resolver is not None:
            v = self.api_key_resolver()
            if v:
                return v
        return self.api_key

    def _cur_max_tokens(self) -> int:
        if self.max_tokens_resolver is not None:
            v = self.max_tokens_resolver()
            if v and v > 0:
                return v
        return self.max_tokens

    # ---------- 内部 ----------

    def _payload(self, messages: list, tools: Optional[list], stream: bool) -> dict:
        payload: dict[str, Any] = {
            "model": self._cur_model(),
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self._cur_max_tokens(),
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        if stream:
            # 请求流式响应中的 usage（部分网关不支持时忽略即可）
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _post(self, payload: dict) -> requests.Response:
        url = f"{self._cur_base_url()}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._cur_api_key()}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                url, headers=headers, json=payload,
                timeout=self.timeout, stream=payload.get("stream", False),
            )
        except requests.RequestException as e:
            raise LLMRetryableError(f"网络错误: {e}") from e
        if resp.status_code >= 400:
            body = resp.text[:500]
            resp.close()
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                raise LLMRetryableError(f"HTTP {resp.status_code}: {body}")
            raise LLMError(f"HTTP {resp.status_code}: {body}")
        return resp

    def _with_retry(self, fn, desc: str):
        """指数退避重试：1s → 2s → 4s。"""
        last: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return fn()
            except LLMRetryableError as e:
                last = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        raise LLMError(f"{desc} 重试 {self.max_retries} 次后仍失败: {last}")

    @staticmethod
    def _extract(data: dict) -> dict:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append({
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "{}",
            })
        return {
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
        }

    def _stream_iter(self, resp: requests.Response) -> Iterator[dict]:
        """解析 SSE 流：delta.content 为文本增量；delta.tool_calls 按 index 累积。"""
        tool_slots: dict[int, dict] = {}
        finish_reason = None
        text_parts: list[str] = []
        usage = None
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("error"):
                # 部分网关会在流中下发错误块（如 DashScope 的 "tool call aborted"），
                # 必须显式抛出，否则会被静默吞掉导致循环空转
                err = chunk["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise LLMError(f"API 流错误: {msg or '未知错误'}")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
            delta = ch.get("delta") or {}
            content = delta.get("content")
            if content:
                text_parts.append(content)
                yield {"type": "text", "text": content}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_slots.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
        tool_calls = [
            {"id": s["id"] or f"call_{i}", "name": s["name"], "arguments": s["arguments"]}
            for i, s in sorted(tool_slots.items())
        ]
        yield {
            "type": "done",
            "content": "".join(text_parts),
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "usage": usage,
        }

    # ---------- 对外 ----------

    def chat(self, messages: list, tools: Optional[list] = None) -> dict:
        """非流式调用（供 compaction 等内部用途）。"""
        def do():
            resp = self._post(self._payload(messages, tools, stream=False))
            data = resp.json()
            resp.close()
            return data
        data = self._with_retry(do, "chat")
        return self._extract(data)

    def chat_stream(self, messages: list, tools: Optional[list] = None) -> Iterator[dict]:
        """流式调用：逐 token 产出 {"type": "text", ...}，最后产出 {"type": "done", ...}。"""
        def do():
            return self._post(self._payload(messages, tools, stream=True))
        resp = self._with_retry(do, "chat(stream)")
        try:
            yield from self._stream_iter(resp)
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"流式读取中断: {e}") from e
        finally:
            resp.close()
