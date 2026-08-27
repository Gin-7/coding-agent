"""测试共享助手：工作区构造 + 测试发现运行（纯标准库，无需 pytest）。"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试临时目录放在项目内（系统 Temp 在沙箱下不可写），已加入 .gitignore
TMP_ROOT = ROOT / ".test-tmp"


def make_ws(tmp, name: str) -> Path:
    p = Path(tmp) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_tests(g: dict) -> int:
    """发现当前模块中以 test_ 开头的函数，逐个运行，返回退出码。"""
    name = g.get("__name__", "test").replace(".", "_")
    tmp = TMP_ROOT / name
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    tests = [(n, fn) for n, fn in sorted(g.items()) if n.startswith("test_") and callable(fn)]
    passed = 0
    for n, fn in tests:
        try:
            fn(tmp)
            passed += 1
            print(f"PASS {n}")
        except Exception as e:
            print(f"FAIL {n}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1
