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


def _fresh_dir(base: Path) -> Path:
    """取一个干净的临时目录。

    先尝试原地重建；若 rmtree 被 Windows 文件锁 / 安全删除拦截而残留，
    则退化为带序号的新目录——否则各用例里的 mkdir() 会因目录已存在抛
    FileExistsError，整批测试假阴性（这是测试设施问题，不是业务 bug）。
    """
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        return base
    for i in range(2, 200):
        cand = base.parent / f"{base.name}-{i}"
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    raise RuntimeError(f"无法为测试准备干净目录: {base}")


def run_tests(g: dict) -> int:
    """发现当前模块中以 test_ 开头的函数，逐个运行，返回退出码。"""
    name = g.get("__name__", "test").replace(".", "_")
    tmp = _fresh_dir(TMP_ROOT / name)
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
