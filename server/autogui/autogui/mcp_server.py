"""Thin MCP server exposing the head-unit tool surface over stdio.

The pattern is borrowed from ``android-agent/gitd/mcp_server.py:16,105`` — ``FastMCP``
plus ``@mcp.tool()`` on plain functions, docstring becomes the tool description and
type hints become the input schema. What we do *not* take from it: that file's iOS
routing, platform-support matrix and ``device`` parameter threading
(``mcp_server.py:21-33``), all of which exist because it drives many heterogeneous
devices. We drive one.

Because ``bridge/tools.py`` functions are already ordinary Python, registration is a
loop rather than 19 copy-pasted decorators, and the exact same functions serve the
``exec_mode: local`` path. That identity is the point: the MCP surface and the
in-process surface cannot drift apart.

Run standalone:  python -m autogui.mcp_server
Ad-hoc check:    python -m autogui.mcp_server --list-tools
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
    def _info() -> str:  # pragma: no cover - trivial
        return (
            f"Automotive GUI Agent bridge. device={tools.dev().serial} "
            f"screen={tools.get_screen_size()} declared_path={cfg.declared_path} "
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
