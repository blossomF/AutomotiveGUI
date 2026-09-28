"""薄 MCP server：把车机工具面以 stdio 暴露出去。

模式借鉴自 ``android-agent/gitd/mcp_server.py:16,105`` —— ``FastMCP``
加上普通函数上的 ``@mcp.tool()``，docstring 成为工具描述、类型注解
成为输入 schema。我们*不*照搬的：那个文件的 iOS 路由、平台支持矩阵
和 ``device`` 参数透传（``mcp_server.py:21-33``）——那些存在是因为它
要驱动许多异构设备。我们只驱动一台。

因为 ``bridge/tools.py`` 的函数本来就是普通 Python，注册是一个循环
而不是 19 个复制粘贴的装饰器，而且同一批函数同时服务
``exec_mode: local`` 路径。这种“同一性”正是关键：MCP 面和进程内面
不可能漂移。

独立运行:  python -m autogui.mcp_server
临时检查:    python -m autogui.mcp_server --list-tools
"""

from __future__ import annotations

import sys

from autogui.bridge import tools
from autogui.config import load


def build_server():
    from mcp.server.fastmcp import FastMCP

    cfg = load()
    if getattr(tools, "_device", None) is None:
        tools.bind_device(cfg.device_serial, input_chain=cfg.input_chain,
                          display=cfg.display_id, verbose=cfg.verbose)

    mcp = FastMCP("autogui-hu")

    for fn in tools.TOOLS:
        mcp.tool()(fn)

    @mcp.resource("autogui://info")
    def _info() -> str:  # pragma: no cover - 平凡函数
        return (
            f"Automotive GUI Agent bridge. device={tools.dev().serial} "
            f"screen={tools.get_screen_size()} "
            f"tools={len(tools.TOOLS)}"
        )

    return mcp, cfg


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mcp, _cfg = build_server()

    if "--list-tools" in argv:
        import asyncio

        for t in asyncio.run(mcp.list_tools()):
            required = list((t.inputSchema or {}).get("properties", {}).keys())
            print(f"{t.name:24s} {required}\n    {(t.description or '').strip().splitlines()[0]}")
        return 0

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
