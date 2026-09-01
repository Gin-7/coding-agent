# Coding Agent（编程智能体 · Web 版）

个人实现的编程智能体：通过与大语言模型交互，自主读写文件、执行命令，完成编程任务。**核心逻辑（对话历史与上下文管理、工具定义与本地执行、模型输出解析、循环终止、错误处理）不依赖任何 agent 框架，仅基于 `requests` 与 Python 标准库实现。**

界面以 **Web UI 为主**：浏览器渲染事件流（SSE 实时推送），文件树、对话、工具调用、后台任务与子 agent 全部可视可控。同时也保留命令行 REPL 作为轻量入口。

## 快速开始

```bash
pip install -r requirements.txt

# 推荐：启动 Web UI（默认 http://127.0.0.1:8080）
python -m agent --web

# Web UI 模拟模式（无需 API key，演示完整流程）
python -m agent --web --mock
python -m agent --web --mock --port 8090

# 命令行模式（可选，无界面，适合管道 / 远程）
python -m agent --mock "演示任务"
python -m agent "写一个 hello.py 并运行"
python -m agent                  # 交互式 REPL，多轮对话
```

## Web UI（主要功能）

后端为标准库 `http.server` + SSE，前端为纯 HTML/CSS/JS（可切换深色 / 浅色），**零额外前端依赖**。浏览器打开后即：

- **左侧边栏 —— 工作区与会话树**
  - 多工作区：随时用文件夹选择器切换本地目录作为工作区
  - 多会话：每个工作区可建多个会话，会话名由首轮对话自动决定；点击会话即可在该会话内继续
  - 底部"设置"入口
- **中间对话区**
  - 模型回复流式显示；工具调用以卡片呈现，点击展开查看参数与结果；命令输出实时滚动
  - 上下文裁剪 / 压缩透明可见；切换会话直接定位到最新消息
  - 审批请求以**非阻塞浮层**呈现（不遮罩全屏，可继续浏览 / 滚动 / 收起为小条）
  - 输入栏"发送"按钮发送后切换为"中断"
- **右侧边栏 —— 文件树 + 任务区**
  - 文件树：浏览 / 预览工作区文件，目录可逐级进入（含 `..` 上移）
  - 文件树下方两个可收起 / 展开区：**后台任务** 与 **子 agent**
  - 点击列表中的任务 / 子 agent，在右侧预览列查看详情：
    - 后台任务：命令、状态徽章、实时输出、停止按钮
    - 子 agent：对话式详情（prompt + 工具调用 + 输出，与主 agent 一致）
  - 当前选中的文件 / 任务项会高亮
- **权限模式**（输入区下拉）：`自动编辑` / `变更前确认` / `计划模式`（先只读探索 → 交出计划 → **批准后执行**）
- **设置弹窗**：明暗主题、模型、工具列表、关于；主题与布局服务端持久化（`.coding-agent/.agent-settings.json`），重启 / 换浏览器不丢

## 命令行模式（REPL）

```bash
python -m agent --resume "继续上一个任务：..."        # 从最近会话恢复
python -m agent --resume-file .coding-agent/sessions/session-xxx.jsonl "..."   # 指定会话恢复
python -m agent --permission ask "任务"               # 审批模式：命令 / 提交需确认
python -m agent --plan "任务"                         # 规划模式：只读探索 → 输出计划 → 终端确认批准后执行
python -m agent --version / --tools                  # 版本 / 列工具
```

REPL 内斜杠命令：`/stats`（token 统计）、`/tools`、`/clear`、`/help`、`/exit`。

## 配置

复制 `.env.example` 为 `.coding-agent/.env` 并填入 API key（或设置环境变量）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_API_KEY` | — | API key（也支持 `DEEPSEEK_API_KEY`） |
| `AGENT_MODEL` | `deepseek-chat` | 模型名 |
| `AGENT_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容网关地址 |
| `AGENT_MAX_CONTEXT_TOKENS` | `0` | 上下文上限（`0` = 用满模型真实窗口并留 10% 余量；`>0` 作硬上限） |
| `AGENT_MAX_STEPS` | `50` | 单次任务最大迭代步数（子 agent 单独上限 20；Web 设置面板可调） |

Web 模式默认监听 `127.0.0.1:8080`，可用 `--port` 修改；`--mock` 无需 key 即可演示。
工作区默认取启动目录；在源码仓库内启动时首次进入 `.coding-agent/default-workspace/` 干净沙箱（源码仓库可手动添加为工作区），也可用 `--workspace` 指定。

## 内置工具

| 工具 | 说明 |
|---|---|
| `read_file` | 读取文件（带行号，分页） |
| `write_file` | 创建 / 整体覆盖写入文件（UTF-8） |
| `edit_file` | 精确搜索-替换，保留原文件行尾风格 |
| `undo_file` | 撤销最近一次修改（自动备份于 `.coding-agent/.agent-backups/`） |
| `list_dir` | 目录浏览（文件 / 子目录 / 大小） |
| `search` | 递归搜索文件内容（子串 / 正则） |
| `glob` | 按 glob 模式查找文件（支持 `**` 递归） |
| `run_command` | 执行命令（超时 + 进程树终止 + 危险命令黑名单 + 实时输出） |
| `start_background` | 后台启动长命令（dev server / 构建 / 安装），不阻塞主循环 |
| `poll_background` / `stop_background` / `list_background` | 后台任务：查状态与输出 / 停止（杀进程树）/ 列出 |
| `git_status` / `git_diff` | 查看工作区改动 |
| `git_commit` / `git_log` | 提交 / 查看提交历史 |
| `spawn_subagent` / `spawn_subagents` | 派生子 agent：单个（同步）/ 多个并行（同步等待合并）；默认只读硬约束 |
| `start_subagents` / `wait_subagents` / `list_subagent_batches` | 异步子 agent 批次：派出去不阻塞 → 稍后收结果 → 查状态 |
| `finish` | 完成任务标记 |

凭据文件（`.env` 系列）与 agent 状态目录 `.coding-agent/`（记忆文件除外）对 agent 不可读写、不可搜索。

## 架构要点

- **事件驱动**：主循环是事件源，只 `_emit()` 事件；CLI 渲染器 / Web SSE / JSONL 会话日志都是订阅者，互不耦合
- **上下文管理**：双轨 token 计量 + 三层压缩策略（摘要压缩 → 整轮裁剪 → 硬截断）
- **子 agent**：独立 ToolContext、共享工作区与后台管理；运行时以 `SubagentStarted/SubagentEvent/SubagentStatus` 事件流式上行

## 测试

```bash
python tests/run_all.py            # 全量（纯标准库，无需 pytest）
python tests/test_tools.py         # 单模块
```

工作区记忆：`.coding-agent/.agent-memory.md`（不入库）记录跨会话约定，会话开始自动注入上下文。

设计文档见 `DESIGN.md`；精简提交版说明见 `README.txt`。
