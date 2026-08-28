"""运行全部测试模块（无需 pytest）。"""
import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MODULES = ["test_context", "test_parser", "test_tools", "test_loop",
           "test_session", "test_background", "test_web"]
if "--quick" in sys.argv:
    MODULES = ["test_context", "test_parser", "test_tools", "test_loop", "test_session"]

total_fail = 0
for name in MODULES:
    print(f"\n===== {name} =====")
    mod = importlib.import_module(name)
    exit_code = mod.main()
    if exit_code != 0:
        total_fail += 1

sys.exit(1 if total_fail else 0)
