Coding Agent（编程智能体 · Web 版）
================================

仓库：https://github.com/Gin-7/coding-agent

编程智能体：与大模型交互，自主读写 / 执行命令完成任务。核心逻辑
（上下文管理、工具执行、输出解析、循环终止、错误处理）不依赖 agent 框架，
仅依赖 requests + 标准库实现。

启动（推荐 Web）
--------------
    pip install -r requirements.txt
    python -m agent --web          # http://127.0.0.1:8080
    python -m agent --web --mock   # 模拟模式，无需 API key

Web UI
------
- 左栏：多工作区（文件夹选择器切换）+ 多会话树，底部"设置"。
- 中区：回复流式；工具卡片可展开；审批非阻塞浮层；权限：自动编辑 / 变更前确认 / 计划模式。
- 右栏：文件树 + 后台任务 / 子 agent，点列表项看详情（后台含命令/状态/输出/停止；子 agent 对话式），选中高亮。
- 明暗主题与布局服务端持久化；零前端依赖（http.server + SSE / 纯 HTML/CSS/JS）。

命令行：python -m agent "写 hello.py" / --plan "任务"（计划模式）
配置：复制 .env.example 为 .coding-agent/.env 填 AGENT_API_KEY
工具：read/write/edit/undo_file、list_dir、search、glob、run_command、
start_background（后台）、git_*、spawn_subagent(s)、start/wait_subagents、finish
（凭据文件对 agent 不可读写）
架构：事件驱动（主循环只 _emit，CLI / Web SSE / JSONL 为订阅者）；上下文三层压缩；
子 agent 独立上下文、运行时流式上行。测试：python tests/run_all.py
