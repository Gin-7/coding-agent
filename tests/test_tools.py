"""工具系统测试（文件/编辑/搜索/git/命令/凭据保护）。"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_common import make_ws, run_tests


def test_file_roundtrip(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "file")
    ctx = ToolContext(ws)
    r = dispatch("write_file", {"path": "hello.txt", "content": "hi\n"}, ctx)
    assert r["ok"]
    r = dispatch("read_file", {"path": "hello.txt"}, ctx)
    assert r["ok"] and "hi" in r["output"]
    r = dispatch("write_file", {"path": "many.txt", "content": "\n".join(f"L{i}" for i in range(50))}, ctx)
    assert r["ok"]
    r = dispatch("read_file", {"path": "many.txt", "offset": 40, "limit": 10}, ctx)
    assert r["ok"] and "L48" in r["output"] and "L38" not in r["output"]


def test_read_file_char_cap(tmp):
    """回归：read_file 必须有字符上限。

    只限行数不够——压缩过的 JS / 生成的 SQL 几千行就上 MB，实测单次可回传
    100 万字符（≈33 万 token），顶穿模型窗口被 API 400 打回；而这恰好是"最后一条
    工具结果"，三层降级策略（压缩/裁剪都保护最近轮次）也救不回来。
    """
    from agent.tools import ToolContext, dispatch, register_all
    from agent.tools.file_tools import MAX_READ_CHARS
    register_all()
    ws = make_ws(tmp, "readcap")
    ctx = ToolContext(ws)
    (ws / "big.js").write_text("\n".join("var x%d = \"%s\";" % (i, "a" * 480) for i in range(2000)),
                               encoding="utf-8")
    r = dispatch("read_file", {"path": "big.js"}, ctx)
    assert r["ok"]
    # 留一点余量给头部说明与截断提示
    assert len(r["output"]) < MAX_READ_CHARS * 1.1, f"未被截断：{len(r['output'])} 字符"
    assert "内容已截断" in r["output"]
    # 小文件不受影响
    (ws / "small.py").write_text("print(1)\n" * 5, encoding="utf-8")
    r2 = dispatch("read_file", {"path": "small.py"}, ctx)
    assert r2["ok"] and "截断" not in r2["output"] and "print(1)" in r2["output"]


def test_path_escape_blocked(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ctx = ToolContext(make_ws(tmp, "escape"))
    r = dispatch("write_file", {"path": "../evil.txt", "content": "x"}, ctx)
    assert not r["ok"] and "越界" in r["output"]


def test_edit_file_roundtrip(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "edit")
    ctx = ToolContext(ws)
    dispatch("write_file", {"path": "e.txt", "content": "hello world\nhello agent\n"}, ctx)
    r = dispatch("edit_file", {"path": "e.txt", "old": "hello world", "new": "hello python"}, ctx)
    assert r["ok"] and "已修改" in r["output"]
    r = dispatch("read_file", {"path": "e.txt"}, ctx)
    assert r["ok"] and "hello python" in r["output"] and "hello agent" in r["output"]


def test_edit_file_ambiguous_and_missing(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ctx = ToolContext(make_ws(tmp, "edit2"))
    dispatch("write_file", {"path": "e.txt", "content": "x\nx\ny\n"}, ctx)
    r = dispatch("edit_file", {"path": "e.txt", "old": "x", "new": "z"}, ctx)
    assert "不唯一" in r["output"]
    r = dispatch("edit_file", {"path": "e.txt", "old": "nope", "new": "z"}, ctx)
    assert "未找到" in r["output"]


def test_edit_file_crlf_compat(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "edit3")
    ctx = ToolContext(ws)
    (ws / "crlf.txt").write_bytes(b"line1\r\nline2\r\n")
    r = dispatch("edit_file", {"path": "crlf.txt", "old": "line1", "new": "changed"}, ctx)
    assert r["ok"] and "已修改" in r["output"]
    assert (ws / "crlf.txt").read_bytes() == b"changed\r\nline2\r\n"


def test_undo_file(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "undo")
    ctx = ToolContext(ws)
    dispatch("write_file", {"path": "u.txt", "content": "version 1\n"}, ctx)
    dispatch("edit_file", {"path": "u.txt", "old": "version 1", "new": "version 2"}, ctx)
    r = dispatch("undo_file", {"path": "u.txt"}, ctx)
    assert r["ok"] and "已撤销" in r["output"]
    r = dispatch("read_file", {"path": "u.txt"}, ctx)
    assert r["ok"] and "version 1" in r["output"]
    r = dispatch("undo_file", {"path": "u.txt"}, ctx)
    assert "没有可撤销" in r["output"]


def test_list_dir(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "listdir")
    (ws / "sub").mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    ctx = ToolContext(ws)
    r = dispatch("list_dir", {"path": "."}, ctx)
    assert r["ok"] and "a.txt" in r["output"] and "sub/" in r["output"]


def test_search(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "search")
    (ws / "one.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    (ws / "two.txt").write_text("HELLO world\n", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "junk.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    ctx = ToolContext(ws)
    r = dispatch("search", {"pattern": "hello"}, ctx)
    assert r["ok"] and "one.py:1" in r["output"] and "two.txt:1" in r["output"]
    assert "junk.py" not in r["output"]
    assert "2 处匹配" in r["output"]
    r = dispatch("search", {"pattern": r"def \w+\(", "regex": True}, ctx)
    assert r["ok"] and "one.py:1" in r["output"]
    r = dispatch("search", {"pattern": "notexist"}, ctx)
    assert r["ok"] and "未找到" in r["output"]


def test_glob_tool(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "glob")
    (ws / "one.py").write_text("x", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "two.py").write_text("y", encoding="utf-8")
    (ws / "sub" / "note.txt").write_text("z", encoding="utf-8")
    ctx = ToolContext(ws)
    r = dispatch("glob", {"pattern": "**/*.py"}, ctx)
    assert r["ok"] and "one.py" in r["output"] and "sub/two.py" in r["output"]
    r = dispatch("glob", {"pattern": "**/*.txt"}, ctx)
    assert r["ok"] and "sub/note.txt" in r["output"]


def test_blacklist(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ctx = ToolContext(make_ws(tmp, "blacklist"))
    r = dispatch("run_command", {"command": "del /f /s q.txt"}, ctx)
    assert not r["ok"] and "拦截" in r["output"]


def test_run_command_ok(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ctx = ToolContext(make_ws(tmp, "cmd"))
    r = dispatch("run_command", {"command": "echo hello-agent"}, ctx)
    assert r["ok"] and "hello-agent" in r["output"]


def test_run_command_streaming(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "stream")
    collected = []
    ctx = ToolContext(ws, on_output=lambda text: collected.append(text))
    r = dispatch("run_command", {"command": "echo stream-hello"}, ctx)
    assert r["ok"] and "stream-hello" in r["output"]
    assert any("stream-hello" in c for c in collected)


def test_credentials_protected(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "creds")
    (ws / ".env").write_text("SECRET_KEY=abc123\n", encoding="utf-8")
    ctx = ToolContext(ws)
    assert not dispatch("read_file", {"path": ".env"}, ctx)["ok"]
    assert not dispatch("write_file", {"path": ".env", "content": "x"}, ctx)["ok"]
    assert not dispatch("edit_file", {"path": ".env", "old": "x", "new": "y"}, ctx)["ok"]
    assert "未找到" in dispatch("search", {"pattern": "SECRET_KEY"}, ctx)["output"]


def test_git_tools(tmp):
    from agent.tools import ToolContext, dispatch, register_all
    register_all()
    ws = make_ws(tmp, "git")
    subprocess.run(["git", "init", "-q"], cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(ws), check=True)
    ctx = ToolContext(ws)
    r = dispatch("git_status", {}, ctx)
    assert r["ok"] and "No commits" in r["output"]
    (ws / "a.txt").write_text("hi", encoding="utf-8")
    r = dispatch("git_status", {}, ctx)
    assert r["ok"] and "a.txt" in r["output"]
    r = dispatch("git_commit", {"message": "init"}, ctx)
    assert r["ok"] and "1 file changed" in r["output"]
    r = dispatch("git_log", {"n": 5}, ctx)
    assert r["ok"] and "init" in r["output"]


def main() -> int:
    return run_tests(globals())


if __name__ == "__main__":
    sys.exit(main())
