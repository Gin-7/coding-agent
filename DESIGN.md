# 编程智能体（Coding Agent）设计方案

> 构建编程智能体（coding agent）的设计总纲：记录架构决策、模块规格与开发里程碑。

---

## 1. 项目概述

个人独立设计并实现一个**编程智能体（coding agent）**：通过与 LLM 交互，自主读写文件、执行命令，完成用户交代的编程任务。目标形态类似简化的 Claude Code / Codex / OpenCode / DeepSeek Harness。

### 1.1 设计约束

| 类型 | 内容 |
|---|---|
| 禁止 | 在现成 agent 产品上封装界面；使用任何 agent 框架/SDK（LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等） |
| 禁止 | 依赖 API 服务端托管的代码执行或文件工具（Code Interpreter、Files API） |
| 允许 | 模型厂商 API 客户端库、OpenAI 兼容网关、模型原生 tool calling 接口 |
| 必须自研 | 对话历史与上下文管理、工具的定义与本地执行、模型输出的解析、循环终止条件、错误处理 |
| 其他 | 模型不限、语言不限、平台不限；API key 走环境变量或未入库配置 |

### 1.2 仓库与交付

1. **Git 仓库**：公开仓库，完整提交历史，不改写已推送历史
2. **使用文档**：README 说明如何运行与特色功能
3. **演示**：录制 agent 完成真实编程任务的过程，并讲解实现

---

## 2. 技术选型（已确认）

| 维度 | 决策 | 理由 |
|---|---|---|
| 语言 | Python 3.10+ | 标准库丰富，开发效率高 |
| 平台 | Windows（v1 仅 Windows） | 用户要求；shell 走 cmd.exe |
| LLM 客户端 | 自写 `requests` 版 OpenAI 兼容客户端 + SSE 流式 | 可讲清 wire protocol，协议层完全可控；避免引入厚重依赖 |
| 运行时模型 | DeepSeek（base_url/model 可配置） | 便宜、tool calling 强、支持流式；换模型零成本。**已实测 Qwen（DashScope OpenAI 兼容网关）跑通**，零改动 |
| 权限模型 | 工作区内自动执行 + 危险命令黑名单 | 演示流畅，安全设计有层次 |
| 上下文管理 | 双轨 token 计量 + compaction → 裁剪 → 硬截断 | 核心逻辑自研、可扩展 |
| 前端形态 | 终端 CLI（事件流驱动渲染） | 无额外依赖，演示自然；为 Web UI 预留接口 |
| 会话持久化 | JSONL 事件日志 | 可回放、可做回归测试、演示素材 |
| 第三方依赖 | 仅 `requests` | 其余全标准库 |

---

## 3. 总体架构

### 3.1 分层视图

```
用户输入任务（交互式 REPL / --task 一次性模式）
        │
        ▼
┌───────────────────────────────────────────────┐
│                 CLI 层 cli.py                 │
│   双入口、流式打印、ANSI 彩色、Ctrl+C 优雅中断  │
└───────────────────┬───────────────────────────┘
                    │ 订阅事件流
                    ▼
┌───────────────────────────────────────────────┐
│             主循环 loop.py（事件源）          │
│                                               │
│  while 未终止:                                │
│    ① 组装上下文 → 调 LLM（SSE 流式）         │
│    ② 解析输出（原生 tool_calls / 文本协议）   │
│    ③ 校验并分派工具 → 本地执行               │
│    ④ 结果写回历史 → 检查终止条件             │
└───────┬─────────────┬──────────────┬──────────┘
        │             │              │
   ┌────▼────┐   ┌─────▼──────┐  ┌───▼────────┐
   │ llm.py  │   │context.py  │  │  tools/    │
   │ OpenAI  │   │ 历史管理    │  │ 文件/命令  │
   │ 兼容客户端│  │ token预算   │  │ /搜索/完成 │
   │ 自写+流式 │  │ 压缩compaction│  │ (注册表)   │
   └─────────┘   └────────────┘  └────────────┘
```

### 3.2 事件流驱动（核心架构决策）

主循环**不直接碰 UI**，而是产生统一事件流；CLI 渲染器 / Web UI 只是事件的订阅者：

```
事件:  text_delta │ tool_call │ tool_result │ error │ finish │ ask_permission │ command_output
```

- 核心与 UI 完全解耦：CLI 是终端渲染器，Web UI 是浏览器渲染器（SSE 推送），主循环零改动
- 事件流同时写入 JSONL 会话日志（session.py），天然支持回放与调试
- 设计要点："agent 是事件驱动的，UI 只是订阅者"

### 3.2.1 Web UI（`agent/web.py` + `agent/web_ui/`）

本地网页界面，浏览器作为渲染器（零额外依赖：后端 `http.server` + SSE，前端纯 HTML/CSS/JS）：

- `python -m agent --web [--port N]` 启动；`GET /api/events` 为 SSE 事件流长连接
- **多工作区**：`POST /api/workspace` 选择本地文件夹（`GET /api/fs/browse` 浏览服务器文件系统）
- **多会话**：每工作区多会话，会话名由首轮对话决定；`POST /api/session/new`/`select` 新建/切换，`GET /api/session/messages` 取历史展示
- `POST /api/run` 提交任务（工作线程运行，事件广播到所有订阅客户端）
- 审批（plan/ask 模式）通过浏览器弹窗 `POST /api/confirm` 回传；`POST /api/interrupt` 在步骤边界中断
- 前端：顶栏（工作区/会话/主题/设置/状态）、居中对话流（流式回复、可展开工具卡片、实时命令输出）、右侧文件面板、设置抽屉（菜单栏：明暗主题/工具/关于）

### 3.3 目录结构

```
coding-agent/
├── agent/
│   ├── __init__.py
│   ├── cli.py          # 交互式 REPL + 一次性模式（双入口）
│   ├── events.py       # 事件流数据结构定义
│   ├── renderer.py     # CLI 渲染器（ANSI 颜色、折叠、进度）
│   ├── loop.py         # 主循环：迭代、终止、防空转
│   ├── llm.py          # 自写 OpenAI 兼容客户端 + SSE 流式 + 重试
│   ├── context.py      # 历史管理 + 双轨 token 计量 + 裁剪/compaction/截断
│   ├── parser.py       # tool_calls 校验 + 文本协议兜底
│   ├── session.py      # JSONL 会话持久化
│   ├── config.py       # 环境变量 / .env 配置加载
│   ├── prompts.py      # system prompt 模板
│   └── tools/
│       ├── __init__.py # 注册表：{name: (fn, json_schema)}
│       ├── file_tools.py   # read_file / write_file / edit_file
│       ├── search_tools.py # list_dir / search
│       ├── shell_tools.py  # run_command
│       └── meta_tools.py   # finish
├── tests/              # 关键路径测试（parser / context / tools）
├── DESIGN.md           # 本文档
├── README.md           # 仓库说明与使用文档
└── requirements.txt    # requests
```

---

## 4. 六大核心模块规格

> 重要逻辑全部自研：历史与上下文、工具定义与执行、输出解析、循环终止、错误处理。

### 4.1 LLM 客户端 `llm.py`

- 用 `requests` 实现 OpenAI 兼容 `POST {base_url}/chat/completions`
- **SSE 流式解析**：逐行读取 `data:` 增量，实时产出 `text_delta`
- 支持原生 `tools` 参数（tool calling）
- 配置项：`base_url` / `model` / `api_key`（环境变量）/ `temperature` / `max_tokens` / 超时
- **重试策略**：429/5xx 指数退避（如 1s→2s→4s，最多 3 次）；网络错误同样重试；重试耗尽后抛出让主循环优雅退出
- 每次响应记录真实 `usage`（prompt/completion tokens），交给 context.py 校准预算

### 4.2 上下文管理 `context.py`（核心模块）

**消息结构**（完整保留，不丢信息）：
- `system`（固定 + 每轮可注入动态提示）
- `user`（任务描述、用户追加指令）
- `assistant`（含 `tool_calls`）
- `tool`（工具执行结果，按 tool_call_id 对应）

**双轨 token 计量**：
1. **真实计量**：API 返回的 `usage` 累计进历史，作为权威依据
2. **启发式预检**：发送前估算（CJK ≈ 1.5 token/字，ASCII ≈ 4 字符/token），超预算提前处理，避免请求被拒

**超预算三层策略**（优先保留语义）：
1. **compaction（优先）**：把最近 K 轮（默认 2）之前的早期消息用一次独立 LLM 调用压成结构化摘要，替换进历史（Claude Code 同款思路）；区域过大（超预算 40%）时分块压缩再合并，控制单次调用规模；区域过小（<512 tokens）时跳过，避免摘要被反复再摘要
2. **裁剪（兜底，免费）**：compaction 不可用/不足时，以"轮"为单位整轮丢弃最老的工具调用（保护最新一轮，保证 tool 消息与 tool_calls 严格配对）
3. **硬截断（最后手段）**：仅保留最近 K 轮，更早的直接丢弃并留提示

### 4.3 工具系统 `tools/`（注册表模式）

**注册表**：`{name: (callable, json_schema)}`，schema 直接作为 API `tools` 参数，工具即数据。

**v1 工具集**：

| 工具 | 说明 | 关键参数 |
|---|---|---|
| `read_file` | 读取文件，行号显示 | `path`, `offset?`, `limit?` |
| `write_file` | 全覆盖写，UTF-8 显式编码 | `path`, `content` |
| `edit_file` | 精确搜索-替换（省 token，改大文件） | `path`, `old`, `new` |
| `undo_file` | 撤销最近一次修改（自动备份于 `.agent-backups/`） | `path` |
| `list_dir` | 目录/文件浏览 | `path` |
| `search` | 文本/正则搜索 | `pattern`, `path`, `regex?` |
| `glob` | 按 glob 模式找文件（`**` 递归） | `pattern`, `path?` |
| `run_command` | subprocess 执行 cmd，带超时 | `command`, `timeout?`, `workdir?` |
| `git_status` / `git_diff` | 查看工作区改动 | `path?`, `staged?` |
| `git_commit` / `git_log` | 提交 / 查看提交历史 | `message` / `n?` |
| `finish` | 完成任务标记（携带总结） | `summary` |

**本地执行原则**：
- 工具 = 普通 Python 函数直接调 OS API；`run_command` 用 `subprocess`（`CREATE_NEW_PROCESS_GROUP`）
- 工具输出**截断回传**（如 3000 字符），防止命令输出撑爆上下文
- 任何异常不崩溃：捕获 → 转成 tool 结果回喂模型（见 4.6）

### 4.4 输出解析 `parser.py`

**主路径 · 原生 tool_calls**：API 已返回结构化 JSON，但校验层自写：
- 工具名存在于注册表
- 参数 JSON 可解析、必填字段齐全
- 非法调用 → 构造错误 tool 结果回喂模型，让它自己修正

**兜底路径 · 文本协议（特色功能）**：
- 模型不支持 tool calling 时，从回复文本提取 `<tool_call>{"name": "...", "args": {...}}</tool_call>` 块
- 正则提取 + JSON 校验，与原生路径统一为内部 `Action` 结构，主循环无感知
- 价值：同一套 agent 可接任意模型；演示"为什么需要两条路径"

### 4.5 循环终止 `loop.py`

终止条件（任一命中即停）：
1. 模型调用 `finish` 工具 → 正常成功终止
2. 迭代数达上限（默认 30，可配）
3. 用户 Ctrl+C → 优雅中断（把中断信息回喂模型，让它收敛后 finish）
4. API 重试耗尽 → 报错退出

**防空转**：迭代过半仍未 finish → 注入提示"若任务已完成请调用 finish"，防止模型无限空转。

### 4.6 错误处理（agent 能"自主"的关键）

- **工具异常 → 回喂模型**：捕获后把错误信息作为 tool 结果返回，模型据此自我修复（如命令失败后换参数重试）——agent 自主性的核心闭环
- 解析失败 → 返回错误信息让模型重试（给 1 次机会，避免死循环）
- 命令超时 → `taskkill /T /F` 杀进程树（Windows 用 `CREATE_NEW_PROCESS_GROUP` + taskkill）
- 编码：读文件先试 UTF-8 再 fallback GBK；cmd 输出 `errors='replace'` 解码

---

## 5. 前端设计（CLI）

### 5.1 双入口

```
python -m agent                          # 交互式 REPL：输入任务，多轮对话
python -m agent "任务描述"               # 一次性模式（演示/自动化使用）
```

### 5.2 渲染规范

| 事件 | 显示 | 颜色 |
|---|---|---|
| 模型思考文本 | 流式打字机效果 | 默认 |
| `tool_call` | `🔧 run_command  cd src && python main.py` | 青色 |
| `tool_result` | 折叠，超长显示前 200 字符 + `…(截断)` | 灰色 |
| 错误 | 附重试信息 | 红色 |
| 迭代进度 | 前缀 `step 3/30` | 黄色 |
| `finish` | 总结框 | 绿色 |

### 5.3 Windows 终端三坑（提前设计）

1. **ANSI 颜色**：Windows 10+ 终端默认不启用 VT 转义，需 `os.system('')` 或 `ctypes` 调 `SetConsoleMode` 开启，否则彩色输出变乱码
2. **stdout 编码**：重定向时 Python 默认 GBK，中文/emoji 会崩 → 入口 `sys.stdout.reconfigure(encoding='utf-8')`
3. **Ctrl+C**：交互模式要优雅中断当前迭代（信息回喂模型），而非整个程序崩溃

### 5.4 会话持久化 `session.py`

- 每轮会话以 JSONL 追加记录全部事件流
- 用途：回放调试、回归测试输入、演示 agent 执行全过程

---

## 6. 安全设计

| 层次 | 机制 |
|---|---|
| 路径锁 | 文件工具默认限定工作区根目录内，越界返回错误 |
| 命令黑名单 | 拦截危险操作（`del /f /s`、`rd /s /q`、`format` 等） |
| 超时 | `run_command` 强制 timeout（默认 120s），防挂死 |
| 凭据 | API key 仅从环境变量 / 未入库 `.env` 读取；`.gitignore` 排除 `.env` |
| 凭据保护 | agent 工具不可读写/搜索 `.env` 系列文件（凭据不进入模型上下文） |
| 输出截断 | 命令输出截断回传，防上下文被撑爆 |

---

## 7. 开发里程碑（约 6 天）

| 日 | 内容 | 验收标准 |
|---|---|---|
| D1 | 仓库初始化 + `llm.py` + `events.py` + 最小主循环（read_file / write_file / run_command / finish 四工具） | ✅ 完成：agent 自主跑通真实小任务（mock + 真实 Qwen API 双验证） |
| D2 | `context.py` 预算 + 裁剪 + `loop.py` 错误处理完善 | ✅ 完成：错误自我修复闭环实测通过；裁剪在真实 API 下配对完整（budget=400 触发验证）；修复 finish 调用后历史配对缺失问题 |
| D3 | 补齐 edit_file / list_dir / search + 流式输出 + CLI 打磨 + session.py | ✅ 完成：三个新工具实测通过（真实 API 综合任务验证）；凭据文件保护；--tools 列出工具 |
| D4 | compaction + 文本协议兜底（特色功能） | ✅ 完成：compaction（分块压缩+合并）实测触发并正常收尾；三层策略 compaction → 裁剪 → 硬截断；文本协议兜底 D1 已就位 |
| D5 | 测试（parser / context / tools）+ 使用文档 + 演示准备 | 全流程稳定可用，演示素材齐备 |
| D6 | 缓冲：真实任务演练、修 bug、文档打磨 | 全流程彩排 |
| 增强 | git 工具集、glob、会话恢复 --resume、审批模式 --permission ask、命令实时输出、usage 统计与 REPL 斜杠命令、参数类型校验 | ✅ 完成：29 项测试全绿，真实 API 验证 git/glob/恢复/流式输出 |
| 增强2 | undo 编辑备份、规划模式 --plan（计划轮仅只读工具，批准后执行）、工作区记忆 .agent-memory.md | ✅ 完成：32 项测试全绿，真实 API 验证 undo/plan/记忆注入 |
| 增强3 | Web UI（SSE 事件流 + 浏览器渲染器）、Web 端审批与中断 | ✅ 完成：33 项测试全绿，冒烟验证页面/接口/运行链路 |

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 长任务 token 爆掉 | compaction + 截断（D4 完成） |
| Windows 编码/进程坑 | 提前设计（5.3 / 4.6），D1 即验证 cmd 执行 |
| API 流式错误被静默吞掉 | llm.py 显式识别错误块并抛出，回显给用户 |
