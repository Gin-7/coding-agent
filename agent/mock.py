"""MockLLM：无 API key 时的确定性演示驱动（也用于测试）。

脚本：查看环境 → 写文件 → 完成任务，验证主循环/解析/工具/日志全链路。
与 LLMClient 同接口（chat_stream），主循环无需感知差异。
"""
import json


class MockLLM:
    def chat_stream(self, messages: list, tools: list = None):
        last = messages[-1] if messages else {}
        role = last.get("role")
        if role == "user":
            content = last.get("content") or ""
            if any(k in content for k in ("解析失败", "请继续", "提示：")):
                yield {"type": "text", "text": "（模拟模式）收到提示，任务已确认完成。"}
                yield self._done([("mock_f", "finish", {"summary": "演示任务完成"})])
            else:
                yield {"type": "text", "text": "（模拟模式）我将演示：查看环境 → 写文件 → 完成任务。"}
                yield self._done([("mock_1", "run_command", {"command": "python --version"})])
        elif role == "tool":
            content = (last.get("content") or "").lower()
            if "python" in content:
                yield {"type": "text", "text": "\n环境正常，现在写一个示例文件。"}
                yield self._done([("mock_2", "write_file", {"path": "hello.txt", "content": "Hello from mock agent!\n"})])
            else:
                yield {"type": "text", "text": "\n文件已写入，任务完成。"}
                yield self._done([("mock_3", "finish", {"summary": "演示任务完成（已生成 hello.txt）"})])
        else:
            yield self._done([])

    @staticmethod
    def _done(tool_calls):
        return {
            "type": "done",
            "content": "...",
            "tool_calls": [
                {"id": cid, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}
                for cid, name, args in tool_calls
            ],
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "usage": None,
        }
