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
| `list_dir` | 目录浏览（文件/子目录/大小） |
| `search` | 递归搜索文件内容（子串/正则） |
| `run_command` | 执行 cmd 命令（超时 + 进程树终止 + 危险命令黑名单） |
| `finish` | 完成任务标记 |

凭据文件（`.env` 系列）对 agent 不可读写、不可搜索。

## 测试

```bash
python tests/test_d1.py
```

设计文档见 `DESIGN.md`。
