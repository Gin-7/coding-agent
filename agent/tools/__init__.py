"""工具包：注册表 + 内置工具集合。"""
from .registry import TOOLS, ToolContext, dispatch, register, tool_schemas


def register_all() -> None:
    """注册全部内置工具（幂等）。"""
    from .background_tools import register_background_tools
    from .file_tools import register_file_tools
    from .git_tools import register_git_tools
    from .meta_tools import register_meta_tools
    from .search_tools import register_search_tools
    from .shell_tools import register_shell_tools
    from .subagent_tools import register_subagent_tools

    register_file_tools()
    register_shell_tools()
    register_search_tools()
    register_git_tools()
    register_background_tools()
    register_subagent_tools()
    register_meta_tools()
