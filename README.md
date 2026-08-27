# Coding Agent（编程智能体）

个人自研的编程智能体：通过与大语言模型交互，自主读写文件、执行命令，完成编程任务。

核心逻辑（对话历史与上下文管理、工具定义与本地执行、模型输出解析、循环终止、错误处理）全部自研，仅依赖 `requests`。

## 快速开始

```bash
pip install -r requirements.txt

# 方式一：模拟模式（无需 API key，演示完整流程）
python -m agent --mock "演示任务"

# 方式二：真实模式（先配置 .env 或环境变量 AGENT_API_KEY）
python -m agent "写一个 hello.py 并运行"
python -m agent                    # 交互式 REPL，多轮对话
```

## 配置

复制 `.env.example` 为 `.env` 并填入 API key（或设置环境变量）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_API_KEY` | — | API key（也支持 `DEEPSEEK_API_KEY`） |
| `AGENT_MODEL` | `deepseek-chat` | 模型名 |
| `AGENT_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容网关地址 |
| `AGENT_MAX_CONTEXT_TOKENS` | `56000` | 上下文预算 |
| `AGENT_MAX_STEPS` | `30` | 最大迭代步数 |

## 内置工具

| 工具 | 说明 |
|---|---|
| `read_file` | 读取文件（带行号，分页） |
| `write_file` | 创建/整体覆盖写入文件（UTF-8） |
| `edit_file` | 精确搜索-替换，保留原文件行尾风格 |
| `undo_file` | 撤销最近一次修改（自动备份于 `.agent-backups/`） |
| `list_dir` | 目录浏览（文件/子目录/大小） |
| `search` | 递归搜索文件内容（子串/正则） |
| `glob` | 按 glob 模式查找文件（支持 `**` 递归） |
| `run_command` | 执行 cmd 命令（超时 + 进程树终止 + 危险命令黑名单 + 实时输出） |
| `git_status` / `git_diff` | 查看工作区改动 |
| `git_commit` / `git_log` | 提交 / 查看提交历史 |
| `finish` | 完成任务标记 |

凭据文件（`.env` 系列）对 agent 不可读写、不可搜索。

## 高级用法

```bash
python -m agent --resume "继续上一个任务：..."   # 从最近会话恢复历史继续干活
python -m agent --resume-file sessions/session-xxx.jsonl "..."  # 指定会话恢复
python -m agent --permission ask "任务"           # 审批模式：命令/提交需确认
python -m agent --plan "任务"                     # 规划模式：先计划（只读探索）批准后执行
python -m agent --web                             # 启动 Web UI（默认 http://127.0.0.1:8080）
python -m agent --web --mock --port 8090          # Web UI 模拟模式（无需 API key）
python -m agent --version                         # 版本
python -m agent --tools                           # 列出全部工具
```

## Web UI

本地网页界面（浏览器渲染事件流，SSE 实时推送）：

- **多工作区**：可随时选择本地文件夹作为工作区（文件夹挑选器），文件目录显示在右侧
- **多会话**：每个工作区可建多个会话，会话名由首轮对话决定；点击会话即可在该会话内继续对话
- **对话流**：模型回复流式显示，工具调用卡片可展开查看参数与结果；命令输出实时滚动；上下文管理（裁剪/压缩）透明可见
- **设置面板**：内置菜单栏，含明暗主题切换、工具列表、关于
- **审批弹窗**：`--permission ask` / `--plan` 模式下在浏览器内批准/拒绝；中断按钮随时停止

实现零额外依赖：后端为标准库 `http.server` + SSE，前端为纯 HTML/CSS/JS（可切换深色/浅色）。

工作区记忆：`.agent-memory.md`（不入库）记录跨会话约定，会话开始自动注入上下文；agent 会在任务中更新它。

REPL 内支持斜杠命令：`/stats`（token 统计）、`/tools`、`/clear`（清空历史）、`/help`、`/exit`。

## 测试

```bash
python tests/test_d1.py
```

设计文档见 `DESIGN.md`。
