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

## 测试

```bash
python tests/test_d1.py
```

设计文档见 `DESIGN.md`。
