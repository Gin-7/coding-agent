"""CLI 双入口：交互式 REPL / 一次性任务（--task）。

支持 --resume 会话恢复、--permission 审批模式、--mock 演示、--tools 工具列表等。
"""
import argparse
import os
import sys
from pathlib import Path

from . import __version__

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
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--resume", action="store_true",
                   help="从最近一次会话恢复历史继续（续接上一轮对话）")
    p.add_argument("--resume-file", default=None, metavar="PATH",
                   help="从指定会话 JSONL 文件恢复历史继续")
    p.add_argument("--permission", choices=("auto", "ask"), default="auto",
                   help="执行权限：auto 自动执行（危险命令黑名单拦截）；ask 命令/提交/后台启动/写授权子agent 需确认（默认 auto）")
    p.add_argument("--plan", action="store_true",
                   help="计划模式：只读探索并制定计划，不执行修改（等价于 --permission plan）")
    p.add_argument("--web", action="store_true", help="启动 Web UI（本地网页界面，SSE 事件流）")
    p.add_argument("--port", type=int, default=8080, help="Web UI 端口（默认 8080）")
    return p.parse_args(argv)


def _confirm_interactive(name: str, desc: str) -> bool:
    if name == "plan":
        # 计划模式交出计划：批准后转入执行阶段（全量工具）
        print(f"\n[计划]\n{desc}\n")
        prompt = "批准执行该计划？[y/N] "
    else:
        prompt = f"允许执行 {name} {desc}？[y/N] "
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "是")


def _build_real(workspace: Path, args, on_event, confirm):
    from .config import Config
    from .context import Context
    from .llm import LLMClient
    from .loop import AgentLoop
    from .models import context_window_for
    from .prompts import make_system_prompt
    from .tools import ToolContext, register_all

    register_all()
    cfg = Config(workspace, model=args.model, base_url=args.base_url, api_key=args.api_key,
                 max_steps=args.max_steps, max_context_tokens=args.budget)
    if not cfg.api_key:
        raise SystemExit(
            "未找到 API key：请设置环境变量 AGENT_API_KEY / DEEPSEEK_API_KEY，"
            "或在工作区 .coding-agent/.env 中提供（不会入库）。"
        )
    llm = LLMClient(
        base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
        temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.timeout,
    )
    # 上下文预算：AGENT_MAX_CONTEXT_TOKENS > 0 作硬上限（直接用）；
    # = 0（默认）表示"用满模型窗口"，按模型真实窗口推导并留 10% 安全余量。
    # 注意：绝不能把 0 直接当预算传进去——那会让 needs_trim() 恒成立，
    # 主循环每一步都去做压缩/裁剪，历史被无谓压掉。
    cap = cfg.max_context_tokens
    if cap and cap > 0:
        effective = cap
    else:
        effective = context_window_for(cfg.model) or 56000
        effective = max(1024, int(effective * 0.90))
    ctx = Context(make_system_prompt(cfg.workspace), effective,
                  budget_resolver=lambda: effective, window_resolver=lambda: effective)
    return AgentLoop(llm, ctx, ToolContext(cfg.workspace), max_steps=cfg.max_steps,
                     on_event=on_event, confirm=confirm, plan_mode=args.plan)


def _build_mock(workspace: Path, max_steps, budget, on_event, confirm, plan_mode=False):
    from .context import Context
    from .loop import AgentLoop
    from .mock import MockLLM
    from .prompts import make_system_prompt
    from .tools import ToolContext, register_all

    register_all()
    ctx = Context(make_system_prompt(workspace), budget or 56000)
    return AgentLoop(MockLLM(), ctx, ToolContext(workspace), max_steps=max_steps or 50,
                     on_event=on_event, confirm=confirm, plan_mode=plan_mode)


def _show_status(result: dict) -> None:
    s = result.get("status")
    if s == "finished":
        print(f"\n[完成] {result.get('summary', '')}")
    elif s == "timeout":
        print(f"\n[超时] {result.get('message')}")
    elif s == "stopped":
        print(f"\n[中止] {result.get('message')}")
    elif s == "cancelled":
        print(f"\n[已取消] {result.get('message')}")
    elif s == "error":
        print(f"\n[错误] {result.get('message')}")
    if result.get("steps") is not None:
        u = result.get("usage") or {}
        print(f"[统计] 步骤 {result['steps']} | 本轮输入 {u.get('prompt', 0)} / 输出 {u.get('completion', 0)} tokens")


def _handle_slash(loop, cmd: str) -> None:
    c = cmd.strip().lower()
    if c == "/stats":
        u = loop.ctx.real_usage
        print(f"[统计] 历史消息 {len(loop.ctx.messages)} 条 | 预估 {loop.ctx.estimated_tokens()} tokens | "
              f"累计输入 {u['prompt']} / 输出 {u['completion']} tokens")
    elif c == "/tools":
        from .tools import TOOLS
        for name in sorted(TOOLS):
            desc = TOOLS[name]["schema"]["function"]["description"]
            print(f"  {name}: {desc[:60]}")
    elif c == "/clear":
        n = len(loop.ctx.messages) - 1
        loop.ctx.messages = loop.ctx.messages[:1]
        print(f"[已清空] 丢弃 {n} 条历史消息")
    elif c in ("/help", "/h"):
        print("命令：/stats 统计 | /tools 工具列表 | /clear 清空历史 | /exit 退出 | 其余输入视为任务")
    else:
        print(f"未知命令: {cmd}（/help 查看）")


def _repl(loop, renderer, log_turn) -> None:
    print(BANNER)
    print("输入任务，agent 将自主完成；/help 查看命令；exit 退出。\n")
    while True:
        try:
            task = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not task:
            continue
        low = task.lower()
        if low in ("exit", "quit", "退出", "/exit"):
            break
        if task.startswith("/"):
            _handle_slash(loop, task)
            continue
        try:
            result = loop.run(task)
            log_turn(result)
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

    if args.web:
        from .web import run_server
        return run_server(workspace, args, port=args.port)

    from .events import event_to_dict
    from .config import prepare_state_dir, state_dir
    from .renderer import CliRenderer
    from .session import Session

    renderer = CliRenderer()
    # 规划模式强制交互确认计划；权限 ask 时命令级确认
    confirm = _confirm_interactive if (args.permission == "ask" or args.plan) else None

    # agent 状态目录：迁移旧版散落布局 + 预置 .env，会话/记忆/备份统一放 .coding-agent/
    prepare_state_dir(workspace)
    sessions_dir = state_dir(workspace) / "sessions"

    with Session(sessions_dir) as session:
        def on_event(ev):
            renderer.emit(ev)
            session.log(event_to_dict(ev))

        if args.mock:
            # plan_mode 必须透传：否则 `--plan --mock`（演示计划模式）会静默退化成普通模式
            loop = _build_mock(workspace, args.max_steps, args.budget, on_event, confirm,
                               plan_mode=args.plan)
        else:
            loop = _build_real(workspace, args, on_event, confirm)

        # 会话恢复：从历史 JSONL 载入消息
        if args.resume or args.resume_file:
            from .session import latest_session, load_messages
            from .prompts import make_system_prompt
            resume_path = Path(args.resume_file) if args.resume_file else latest_session(sessions_dir)
            if not resume_path or not resume_path.exists():
                print(f"未找到可恢复的会话: {args.resume_file or '(最近一次)'}")
                return 1
            msgs = load_messages(resume_path)
            if msgs:
                if msgs[0].get("role") == "system":
                    msgs[0]["content"] = make_system_prompt(workspace)
                loop.ctx.messages = msgs
                print(f"[已恢复会话] {resume_path.name}（{len(msgs)} 条历史消息）")
            else:
                print(f"[警告] 会话中没有可恢复的历史: {resume_path.name}")

        # 工作区记忆：会话开始注入 .coding-agent/.agent-memory.md 内容（恢复会话时避免重复注入）
        memory_file = state_dir(workspace) / ".agent-memory.md"
        if memory_file.exists() and not any(
                "[工作区记忆]" in (m.get("content") or "") for m in loop.ctx.messages):
            content = memory_file.read_text(encoding="utf-8", errors="replace")[:4000]
            loop.ctx.add({"role": "user", "content": "[工作区记忆]\n" + content})

        def log_turn(result: dict):
            session.log({"type": "RunResult", "status": result.get("status"),
                         "message": result.get("message") or result.get("summary"),
                         "steps": result.get("steps")})
            session.log({"type": "MessagesDump", "messages": loop.ctx.messages})

        if args.task:
            try:
                result = loop.run(args.task)
                log_turn(result)
                _show_status(result)
            except KeyboardInterrupt:
                print("\n[已中断]")
                return 130
        else:
            _repl(loop, renderer, log_turn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
