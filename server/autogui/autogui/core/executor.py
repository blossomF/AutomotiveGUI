"""Executor: one call surface, two transports.

``exec_mode: local`` calls the plain functions in ``bridge/tools.py``.
``exec_mode: mcp``   calls the *same* functions through a real MCP session against
``mcp_server.py``, so the protocol, the generated schemas and the tool descriptions
are all genuinely exercised — not decorative.

Why this exists at all (技术方案评估.md §3 constraint 1): MCP is worth points on the
Skills/MCP axis, but wiring a subprocess + stdio framing into a 16-day project is a
way to lose a week to plumbing. Because the tool bodies are ordinary functions, the
two modes cannot diverge, and if the MCP route misbehaves the flip is a config value
rather than a refactor. The contest explicitly rewards having a degrade path.

The in-memory transport (``mcp.shared.memory``) speaks the actual JSON-RPC protocol
over an object pair — no subprocess, no Windows-only pipe quirks on the Demo Day
machine. An external client (Claude Code, another team's agent) connects over stdio
to ``python -m autogui.mcp_server`` instead; both reach the identical tool list.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class LocalExecutor:
    """Direct in-process calls. No MCP involved."""

    name = "local"

    def __init__(self):
        from autogui.bridge import tools

        self._tools = tools

    def call(self, tool: str, **kwargs: Any) -> str:
        fn = getattr(self._tools, tool, None)
        if fn is None:
            raise AttributeError(f"no such tool: {tool}")
        return _to_text(fn(**kwargs))

    def close(self) -> None:
        return None


class MCPExecutor:
    """Synchronous facade over an async MCP client session pinned to a private loop."""

    name = "mcp"

    def __init__(self, server, *, timeout: float = 30.0):
        import contextlib

        self._timeout = timeout
        self._stack = contextlib.AsyncExitStack()
        self._session = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._bootstrap, args=(server,), daemon=True, name="mcp-loop")
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError("MCP session did not come up within 20s")
        if self._failure:
            raise self._failure

    def _bootstrap(self, server) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._enter(server))
            self._ready.set()
            self._loop.run_forever()
        except BaseException as e:  # surfaced to the constructor
            self._failure = e
            self._ready.set()
        finally:
            self._loop.run_until_complete(self._stack.aclose())

    async def _enter(self, server) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        # ``create_connected_server_and_client_session`` wants the low-level Server;
        # FastMCP has exposed it under different attribute names across versions, so
        # try the known ones before falling back to the object itself. A wrong guess
        # here is cheap: make_executor catches it and degrades to local.
        target = server
        for attr in ("_mcp_server", "server", "_server"):
            inner = getattr(server, attr, None)
            if inner is not None and hasattr(inner, "list_tools"):
                target = inner
                break

        self._session = await self._stack.enter_async_context(
            create_connected_server_and_client_session(target)
        )
        await self._session.initialize()

    def call(self, tool: str, **kwargs: Any) -> str:
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, kwargs), self._loop
        )
        result = fut.result(timeout=self._timeout)
        return _result_to_text(result)

    def list_tools(self) -> list[str]:
        fut = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
        return [t.name for t in fut.result(timeout=self._timeout).tools]

    def close(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "OK"
    return str(value)


def _result_to_text(result: Any) -> str:
    """Flatten an MCP ``CallToolResult`` to text; a tool error must not read as success."""
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else repr(block))
    out = "\n".join(p for p in parts if p)
    if getattr(result, "isError", False):
        return f"TOOL_ERROR {out}".strip()
    return out or "OK"


def make_executor(mode: str, *, server=None):
    """Build the executor for ``exec_mode``, degrading to local if MCP will not come up.

    Returning a working local executor here is deliberate: an MCP handshake failure
    should cost us a log line, not the run. ``bridge.tools`` is already bound by
    ``main.py`` in either mode — binding is a property of the device, not of the
    transport.
    """
    from autogui.bridge import tools

    if not getattr(tools, "_device", None):
        raise RuntimeError("device not bound — main.py must call tools.bind_device() first")

    if mode == "mcp":
        try:
            if server is None:
                from autogui.mcp_server import build_server

                server, _ = build_server()
            return MCPExecutor(server), None
        except Exception as e:
            return LocalExecutor(), f"exec_mode=mcp unavailable ({e}); degraded to local"
    return LocalExecutor(), None
