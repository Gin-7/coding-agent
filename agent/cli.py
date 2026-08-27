"""CLI 双入口：交互式 REPL / 一次性任务（--task）。"""
import argparse
import os
import sys
from pathlib import Path

BANNER = r"""
   ___          _             _   _
  / _ \__ _  __| | __ _ _ __ (_)_ __ __ _
 / /_\/ _` |/ _` |/ _` | '_ \| | '_ ` _ \
/ /_\\ (_| | (_| | (_| | | | | | | | | | |
\____/\__,_|\__,_|\__,_|_| |_|_|_| |_| |_|
   自研编程智能体
"""


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="agent", description="自研编程智能体")
    p.add_argument("task", nargs="?", help="一次性任务描述；省略则进入交互式 REPL")
    p.add_argument("--mock", action="store_true", help="模拟模式：无需 API key，用于演示与测试")
    p.add_argument("--model", default=None, help="模型名（默认读环境变量 AGENT_MODEL）")
    p.add_argument("--base-url", default=None, help="OpenAI 兼容网关地址（默认读 AGENT_BASE_URL）")
    p.add_argument("--api-key", default=None, help="API key（推荐用环境变量，不要写进代码/仓库）")
    p.add_argument("--workspace", default=None, help="工作区根目录（默认当前目录）")
    p.add_argument("--max-steps", type=int, default=None, help="最大迭代步数")
    p.add_argument("--budget", type=int, default=None, help="上下文 token 预算（覆盖 AGENT_MAX_CONTEXT_TOKENS）")
    p.add_argument("--tools", action="store_true", help="列出已注册的工具并退出")
    return p.parse_args(argv)


def _build_real(workspace: Path, args, on_event):
    from .config import Config
    from .context import Context
    from .llm import LLMClient
    from .loop import AgentLoop
    from .prompts import make_system_prompt
    from .tools import ToolContext, register_all

    register_all()
    cfg = Config(workspace, model=args.model, base_url=args.base_url, api_key=args.api_key,
                 max_steps=args.max_steps, max_context_tokens=args.budget)
    if not cfg.api_key:
        raise SystemExit(
            "未找到 API key：请设置环境变量 AGENT_API_KEY / DEEPSEEK_API_KEY，"
            "或在工作区 .env 中提供（.env 已被 .gitignore 排除，不会入库）。"
        )
    llm = LLMClient(
        base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
        temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.timeout,
    )
    ctx = Context(make_system_prompt(cfg.workspace), cfg.max_context_tokens)
    return AgentLoop(llm, ctx, ToolContext(cfg.workspace), max_steps=cfg.max_steps, on_event=on_event)


def _build_mock(workspace: Path, max_steps, budget, on_event):
    from .context import Context
    from .loop import AgentLoop
    from .mock import MockLLM
    from .prompts import make_system_prompt
    from .tools import ToolContext, register_all

    register_all()
    ctx = Context(make_system_prompt(workspace), budget or 56000)
    return AgentLoop(MockLLM(), ctx, ToolContext(workspace), max_steps=max_steps or 30, on_event=on_event)


def _show_status(result: dict) -> None:
    s = result.get("status")
    if s == "finished":
        print(f"\n[完成] {result.get('summary', '')}")
    elif s == "timeout":
        print(f"\n[超时] {result.get('message')}")
    elif s == "stopped":
        print(f"\n[中止] {result.get('message')}")
    elif s == "error":
        print(f"\n[错误] {result.get('message')}")


def _repl(loop, renderer, log_result) -> None:
    print(BANNER)
    print("输入任务，agent 将自主完成；输入 exit/quit 退出。\n")
    while True:
        try:
            task = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not task:
            continue
        low = task.lower()
        if low in ("exit", "quit", "退出"):
            break
        try:
            result = loop.run(task)
            log_result(result)
            _show_status(result)
        except KeyboardInterrupt:
            print("\n[已中断] 可继续输入新任务，或 exit 退出")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = _parse_args(argv)
    if args.tools:
        from .tools import TOOLS, register_all
        register_all()
        for name in sorted(TOOLS):
            desc = TOOLS[name]["schema"]["function"]["description"]
            print(f"{name}: {desc}")
        return 0
    workspace = Path(args.workspace or os.getcwd()).resolve()

    from .events import event_to_dict
    from .renderer import CliRenderer
    from .session import Session

    renderer = CliRenderer()
    with Session(workspace / "sessions") as session:
        def on_event(ev):
            renderer.emit(ev)
            session.log(event_to_dict(ev))

        def log_result(result: dict):
            session.log({"type": "RunResult", "status": result.get("status"),
                         "message": result.get("message") or result.get("summary")})

        if args.mock:
            loop = _build_mock(workspace, args.max_steps, args.budget, on_event)
        else:
            loop = _build_real(workspace, args, on_event)

        if args.task:
            try:
                result = loop.run(args.task)
                log_result(result)
                _show_status(result)
            except KeyboardInterrupt:
                print("\n[已中断]")
                return 130
        else:
            _repl(loop, renderer, log_result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
