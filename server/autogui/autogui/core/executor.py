"""执行器：一个调用面，两种传输。

``exec_mode: local`` 直接调用 ``bridge/tools.py`` 里的普通函数。
``exec_mode: mcp``   通过一个真实的 MCP 会话调用*同样的*函数（对端
``mcp_server.py``），协议、生成的 schema 和工具描述都被真实执行——
不是摆设。

为什么要有这一层（技术方案评估.md §3 约束 1）：MCP 在 Skills/MCP 轴上
值分，但把一个 16 天的项目接上 subprocess + stdio 帧处理，等于用一周
时间做管道工程。因为工具本体就是普通函数，两种模式不可能漂移；MCP
链路出问题时，切换只是一个配置值而不是重构。竞赛明确奖励有降级路径。

内存传输（``mcp.shared.memory``）在对象对上讲真实的 JSON-RPC 协议——
无 subprocess，也没有 Demo Day 机器上 Windows 专属的管道怪癖。外部
客户端（Claude Code、其他团队的 agent）则通过 stdio 连接
``python -m autogui.mcp_server``；两者到达完全相同的工具列表。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class LocalExecutor:
    """进程内直接调用。不经过 MCP。"""

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
    """异步 MCP 客户端会话之上的同步门面，固定在一个私有事件循环上。"""

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
        except BaseException as e:  # 暴露给构造函数
            self._failure = e
            self._ready.set()
        finally:
            self._loop.run_until_complete(self._stack.aclose())

    async def _enter(self, server) -> None:
        from mcp.shared.memory import create_connected_server_and_client_session

        # ``create_connected_server_and_client_session`` 要的是底层 Server；
        # FastMCP 在不同版本里用不同属性名暴露它，所以先试已知的几个，
        # 最后回退到对象本身。这里猜错的代价很小：make_executor 会捕获
        # 并降级到 local。
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
    """把 MCP ``CallToolResult`` 摊平成文本；工具错误不能被读成成功。"""
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else repr(block))
    out = "\n".join(p for p in parts if p)
    if getattr(result, "isError", False):
        return f"TOOL_ERROR {out}".strip()
    return out or "OK"


def make_executor(mode: str, *, server=None):
    """按 ``exec_mode`` 构建执行器；MCP 起不来时降级到 local。

    这里刻意返回可用的 local 执行器：MCP 握手失败只应让我们损失一行
    日志，而不是整个运行。两种模式下 ``bridge.tools`` 都已被
    ``main.py`` 绑定——绑定是设备的属性，不是传输的属性。
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
