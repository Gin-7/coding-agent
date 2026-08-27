"""模型输出解析（核心自研逻辑之一）。

主路径：原生 tool_calls（API 返回结构化 JSON），但校验层自写：
- 工具名存在于注册表
- 参数 JSON 可解析且为对象
- 必填参数齐全

兜底路径：文本协议 —— 从回复文本提取 <tool_call>{"name": ..., "arguments": {...}}</tool_call>，
兼容不支持原生 tool calling 的模型。两条路径统一为内部 ToolCall（Action）结构，主循环无感知。
"""
import json
import re
from typing import List, Tuple

from .events import ToolCall

TEXT_TOOL_PATTERN = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


class ParseError(Exception):
    """输出解析失败（会回喂给模型让其修正）。"""


def _validate(name: str, args: dict, registry: dict) -> None:
    if name not in registry:
        raise ParseError(f"未知工具: {name!r}")
    if not isinstance(args, dict):
        raise ParseError(f"工具 {name} 的参数必须是 JSON 对象")
    schema = registry[name]["schema"]["function"]["parameters"]
    for req in schema.get("required", []):
        if req not in args or args[req] is None:
            raise ParseError(f"工具 {name} 缺少必需参数: {req}")
    # 类型校验：按 schema 声明的类型检查每个参数（bool 是 int 子类，需先判 bool）
    props = schema.get("properties", {})
    for k, v in args.items():
        t = (props.get(k) or {}).get("type")
        if not t:
            continue  # 未声明类型的参数宽容处理
        if t == "string":
            if not isinstance(v, str):
                raise ParseError(f"工具 {name} 参数 {k} 应为 string，得到 {type(v).__name__}")
        elif t == "boolean":
            if not isinstance(v, bool):
                raise ParseError(f"工具 {name} 参数 {k} 应为 boolean，得到 {type(v).__name__}")
        elif t == "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                raise ParseError(f"工具 {name} 参数 {k} 应为 integer，得到 {type(v).__name__}")
        elif t == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ParseError(f"工具 {name} 参数 {k} 应为 number，得到 {type(v).__name__}")


def parse_tool_calls(raw_tool_calls: list, registry: dict) -> List[ToolCall]:
    """原生路径：校验并转换为内部 Action。"""
    result: List[ToolCall] = []
    for tc in raw_tool_calls or []:
        name = (tc.get("name") or "").strip()
        raw_args = tc.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            raise ParseError(f"工具 {name} 的参数 JSON 解析失败: {e}") from e
        _validate(name, args, registry)
        result.append(ToolCall(call_id=tc.get("id") or f"call_{len(result)}", name=name, arguments=args))
    return result


def parse_text_protocol(text: str, registry: dict) -> Tuple[str, List[ToolCall]]:
    """文本路径：提取 <tool_call> 块，返回 (剩余正文, 工具调用列表)。

    解析失败的块保持原样（不丢弃正文），避免信息丢失。
    """
    calls: List[ToolCall] = []

    def repl(m: re.Match) -> str:
        try:
            obj = json.loads(m.group(1))
            name = (obj.get("name") or "").strip()
            args = obj.get("arguments") or obj.get("args") or {}
            _validate(name, args, registry)
            calls.append(ToolCall(call_id=f"call_{len(calls)}", name=name, arguments=args))
            return ""  # 成功提取的块从正文中移除
        except (json.JSONDecodeError, ParseError):
            return m.group(0)  # 失败保留原文，正文仍回给模型

    rest = TEXT_TOOL_PATTERN.sub(repl, text)
    return rest.strip(), calls
