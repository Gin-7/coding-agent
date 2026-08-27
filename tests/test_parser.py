"""模型输出解析测试（原生 tool_calls / 文本协议 / 类型校验）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_common import run_tests


def test_parse_tool_calls_ok(tmp):
    from agent.parser import parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    calls = parse_tool_calls(
        [{"id": "c1", "name": "read_file", "arguments": json.dumps({"path": "a.txt"})}], TOOLS)
    assert calls[0].name == "read_file" and calls[0].arguments == {"path": "a.txt"}


def test_parse_tool_calls_unknown(tmp):
    from agent.parser import ParseError, parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    try:
        parse_tool_calls([{"id": "c1", "name": "nope", "arguments": "{}"}], TOOLS)
        raise AssertionError("应抛出 ParseError")
    except ParseError:
        pass


def test_parse_tool_calls_missing_required(tmp):
    from agent.parser import ParseError, parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    try:
        parse_tool_calls([{"id": "c1", "name": "run_command", "arguments": "{}"}], TOOLS)
        raise AssertionError("应抛出 ParseError")
    except ParseError:
        pass


def test_parse_tool_calls_type_error(tmp):
    from agent.parser import ParseError, parse_tool_calls
    from agent.tools import register_all, TOOLS
    register_all()
    try:
        parse_tool_calls([{"id": "c1", "name": "run_command",
                           "arguments": json.dumps({"command": 123})}], TOOLS)
        raise AssertionError("应抛出 ParseError")
    except ParseError as e:
        assert "string" in str(e)


def test_text_protocol(tmp):
    from agent.parser import parse_text_protocol
    from agent.tools import register_all, TOOLS
    register_all()
    text = '我先看看。\n<tool_call>{"name": "read_file", "arguments": {"path": "x"}}</tool_call>\n完了。'
    rest, calls = parse_text_protocol(text, TOOLS)
    assert len(calls) == 1 and calls[0].name == "read_file"
    assert "我先看看" in rest and "完了" in rest


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
