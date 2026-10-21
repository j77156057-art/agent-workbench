"""把 Agent 工作台 自己的工具经 MCP 暴露出去，让同一仓库里的其他 agent 也能用。

harness 原本只有 MCP client：Agent 工作台 有 90 多个工具，但并肩干活的其他 AI 一个都调不到，
只能靠 markdown 公告表互相转述。这个 server 把它们直接递到对面。

默认【只观察、不改代码】：检索、引用查找、历史、诊断、以及 lane 认领表。改代码的工具
（apply_edit / create_file / dev_apply_edits / dev_patch / dev_move）必须显式
`--allow-writes` 才注册；而且即便注册，仍然走各工具原有的路径沙箱、越界拒绝与审批门——
MCP 不是绕过护栏的后门，只是换一个入口。

用法：

    python -m agent_runtime.mcp_server --project D:/WorkBuddy/rag-agent [--allow-writes]
    python -m agent_runtime.mcp_server --list          # 看会暴露哪些工具

stdio 传输，本机进程级；不开网络端口，因此不存在远程可达面。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Callable

#: 默认暴露：只读观察 + 协作登记（dev_lanes 只写自己的状态文件，不动代码）
READ_ONLY_TOOLS = (
    "search_code",
    "grep",
    "read_file",
    "list_dir",
    "dev_glob",
    "dev_git_diff",
    "dev_git_log",
    "dev_find_references",
    "dev_diagnostics",
    "self_verify",
    "search_knowledge",
    "dev_lanes",
)
#: 需要 `--allow-writes` 才注册
WRITE_TOOLS = (
    "apply_edit",
    "create_file",
    "dev_apply_edits",
    "dev_patch",
    "dev_move",
    # 会落夹具文件、起监听端口并启动真实浏览器，属于有副作用的一档
    "dev_media",
    # 真的会点、会填：页面背后的服务可能随之改项目或发请求
    "dev_page_action",
    # 会建/删临时 detached worktree（写 .git 元数据）并执行提交里的代码，不是纯读
    "dev_headcheck",
    # 真的把项目测试跑起来（执行项目代码），只是不接受任意命令与任意参数
    "dev_testrun",
    # 会编译 .vue、起临时静态服务并启动真实浏览器执行组件代码
    "dev_vue",
    # 会起真后端进程（uvicorn api:app）、开真浏览器整页跑一遍
    "dev_apptest",
)
#: 无论如何都不暴露：任意命令执行、越界文件操作、以及会二次放大权限的连接器跳转
NEVER_EXPOSED = (
    "run_command",
    "python_exec",
    "dev_mcp_call",
    "dev_route_connector",
    "read_external_file",
    "create_external_file",
    "edit_external_file",
    "delete_external_file",
    "install_tool",
    "dev_commit",
    "dev_commit_all",
    "dev_rollback_changeset",
    "start_workflow",
)
DESTRUCTIVE = {"dev_patch", "dev_move", "apply_edit", "create_file", "dev_apply_edits"}

INSTRUCTIONS = (
    "Agent 工作台 harness 工具面。约定：每个工具只有一个字符串入参 input，格式与 Agent 工作台"
    "的 Action Input 完全相同（`键: 值` 多行，或直接给路径/符号名）。开工前先用 dev_lanes "
    "action: status 看别的 lane 在动哪些文件，动某个文件前 action: check 问归属；改完用 "
    "dev_diagnostics + dev_git_diff 自查。默认只暴露观察类工具，写代码的工具需要服务端 "
    "--allow-writes。"
)


def _wrap(name: str, func: Callable[[str], Any], description: str) -> Callable[..., str]:
    """把 `func(input: str) -> str` 包成带类型标注与文档的 MCP 工具函数。"""

    def tool(input: str = "") -> str:  # noqa: A002 - 与工作台 Action Input 同名
        try:
            out = func(input)
        except Exception as exc:  # noqa: BLE001 - 工具异常不能打断 server
            return f"工具 {name} 执行失败：{type(exc).__name__}: {str(exc)[:300]}"
        return out if isinstance(out, str) else str(out)

    tool.__name__ = name
    tool.__doc__ = description or f"Agent Workbench builtin tool {name}."
    return tool


def selected_tools(allow_writes: bool = False, only: str = "") -> tuple[str, ...]:
    """这次要暴露的工具名；`only` 只能进一步收窄，不能放大。"""
    names = list(READ_ONLY_TOOLS) + (list(WRITE_TOOLS) if allow_writes else [])
    wanted = [item.strip() for item in (only or "").replace(",", " ").split() if item.strip()]
    if wanted:
        names = [name for name in names if name in wanted]
    return tuple(name for name in names if name not in NEVER_EXPOSED)


def build_server(project: str = "", *, allow_writes: bool = False, only: str = "",
                 name: str = "docmind-harness"):
    """构造 MCP server。`project` 会设成 code_root，所有路径沙箱都以它为界。"""
    import config
    import tools
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    root = os.path.normpath(os.path.abspath(project or config.get_runtime("code_root")
                                            or tools.CODE_ROOT or ""))
    if root:
        config.set_runtime("code_root", root)
    server = MCPServer(name=name, instructions=INSTRUCTIONS,
                       description="Agent 工作台 harness 工具面（检索/引用/历史/诊断/lane 协作）")
    for tool_name in selected_tools(allow_writes, only):
        meta = tools.TOOLS.get(tool_name) or {}
        func = meta.get("func")
        if not callable(func):
            continue
        server.add_tool(
            _wrap(tool_name, func, str(meta.get("description") or "")),
            name=tool_name,
            title=tool_name.replace("_", " ").title(),
            description=str(meta.get("description") or "")[:6000],
            annotations=ToolAnnotations(
                readOnlyHint=tool_name in READ_ONLY_TOOLS,
                destructiveHint=tool_name in DESTRUCTIVE,
                idempotentHint=tool_name in READ_ONLY_TOOLS,
                openWorldHint=False,
            ),
        )
    return server, root


def _run_stdio(server: Any) -> int:
    asyncio.run(server.run_stdio_async())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docmind-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--project", default="", help="作为 code_root 的项目目录（路径沙箱边界）")
    parser.add_argument("--allow-writes", action="store_true",
                        help="额外暴露改代码的工具（默认只读）")
    parser.add_argument("--tools", default="", help="进一步收窄到指定工具名，逗号分隔")
    parser.add_argument("--list", action="store_true", help="只打印会暴露的工具名并退出")
    args = parser.parse_args(argv)
    if args.list:
        for tool_name in selected_tools(args.allow_writes, args.tools):
            print(tool_name)
        return 0
    try:
        server, root = build_server(args.project, allow_writes=args.allow_writes,
                                    only=args.tools)
    except ModuleNotFoundError as exc:  # mcp 没装
        print(f"需要 MCP SDK：python -m pip install 'mcp>=2,<3'（{exc}）", file=sys.stderr)
        return 2
    if root:
        print(f"docmind-mcp 已就绪，项目={root}，写操作={'开' if args.allow_writes else '关'}",
              file=sys.stderr)
    else:
        print("警告：没有 --project 也没有配置的 code_root，检索类工具会报「未配置代码库根目录」。",
              file=sys.stderr)
    return _run_stdio(server)


if __name__ == "__main__":
    raise SystemExit(main())
