"""工具面——纯 Python 函数，不 import MCP、不 import LangGraph。

这个模块是整个架构悬挂其上的接缝（技术方案评估.md §3 约束 1）。
agent 能*做*的一切都在这里，是返回紧凑字符串的普通函数。
``mcp_server.py`` 只是给它们套壳；``core/graph.py`` 要么直接调用
（``exec_mode: local``），要么通过 MCP 客户端调用（``exec_mode: mcp``）。

两条刻意遵守的约定：

* **返回压缩的 ``str``，绝不返回 dict。** 工具输出直接进入模型
  上下文。外部框架会输出的 ``{"ok": true, "code": 0, ...}`` 包装
  纯属烧 token。（跟随 ``android-agent/gitd/mcp_server.py:106``，
  每个工具都返回 ``str``。）
* **没有 ``device`` 参数。** android-agent 因为驱动多台手机，把 serial
  穿过全部 62 个工具。我们面向单台车机，启动时绑定——那个参数
  会在每次调用里重复烧 token。
"""

from __future__ import annotations

import base64
import time

from autogui.bridge.adb import ADBError, Device, find_node, get_device, nodes
from autogui.bridge.input import STRATEGIES, TextInput

_device: Device | None = None
_text_input: TextInput | None = None


def bind_device(serial: str | None = "auto", *, input_chain: list[str] | None = None,
                display: int | None = None, verbose: bool = False) -> Device:
    """解析并记住唯一目标设备。启动时调用一次。"""
    global _device, _text_input
    _device = get_device(serial, display=display)
    order = tuple(input_chain) if input_chain else STRATEGIES
    _text_input = TextInput(_device, order=order, verbose=verbose)
    return _device


def dev() -> Device:
    if _device is None:
        raise RuntimeError("bridge not bound — call bind_device() first (main.py does this)")
    return _device


def text_input() -> TextInput:
    if _text_input is None:
        raise RuntimeError("bridge not bound — call bind_device() first")
    return _text_input


# ── 感知（文本面；图像字节走 perception/capture.py） ──────────────────────────


def get_screen_size() -> str:
    """屏幕像素尺寸，例如 ``1920x1080``。"""
    w, h = dev().screen_size()
    return f"{w}x{h}"


def capture_screenshot_b64() -> str:
    """主屏的原始分辨率 PNG，base64 编码。

    只有 VLM 降级路由需要经工具面拿它；tree 路由从不发图——
    token 经济就来自这里。
    """
    return base64.b64encode(dev().screenshot()).decode("ascii")


def dump_ui_tree(brief: bool = True) -> str:
    """原始 ``uiautomator`` XML 层级。

    ``brief=True`` 返回节点数加少量样例，而不是整棵树——原始 dump
    有 40-120 KB，会击穿上下文预算。模型真正看到的压缩候选列表
    由 ``perception/tree.py`` 生成。
    """
    xml = dev().dump_xml()
    if not brief:
        return xml
    total = len(nodes(xml))
    return f"nodes={total} bytes={len(xml)}"


def ui_candidates(limit: int = 40) -> str:
    """紧凑的编号候选列表——路径 B 的决策面。

    每行格式：``#12 搜索 [960,86]``。模型用 ``node_ref`` 整数回答，
    从不复制坐标，消除了一整类解析错误（这也是 tree 模式在 9/19
    实测中准确率高于网格模式的原因）。
    """
    from autogui.perception.tree import candidates_from_xml, render_candidates

    xml = dev().dump_xml()
    cands = candidates_from_xml(xml, limit=limit)
    if not cands:
        return "TREE_EMPTY"
    return render_candidates(cands)


def find_and_center(match: str) -> str:
    """找到 text/content-desc 包含 ``match`` 的第一个节点；返回 ``x,y``。"""
    xml = dev().dump_xml()
    node = find_node(xml, text=match) or find_node(xml, desc=match)
    if not node:
        return f"NOT_FOUND:{match}"
    from autogui.bridge.adb import node_center

    c = node_center(node)
    return f"{c[0]},{c[1]}" if c else f"NO_BOUNDS:{match}"


# ── 动作执行 ──────────────────────────────────────────────────────────────────


def tap(x: int, y: int) -> str:
    dev().tap(x, y)
    return f"OK tap({x},{y})"


def long_press(x: int, y: int, ms: int = 800) -> str:
    dev().long_press(x, y, ms=ms)
    return f"OK long_press({x},{y},{ms}ms)"


def swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> str:
    dev().swipe(x1, y1, x2, y2, ms=ms)
    return f"OK swipe({x1},{y1})->({x2},{y2})"


def input_text(text: str, clear_first: bool = False) -> str:
    """输入到聚焦的输入框，按 yadb -> ADBKeyboard -> ASCII 降级。"""
    result = text_input().type(text, clear_first=clear_first)
    note = f" [{result.note}]" if result.note else ""
    return f"OK input({result.strategy}) sent={result.typed!r}{note}"


def keyevent(key: str) -> str:
    dev().keyevent(key)
    return f"OK keyevent({key})"


def press_back() -> str:
    dev().back()
    return "OK back"


def press_home() -> str:
    dev().home()
    return "OK home"


# ── 应用 ─────────────────────────────────────────────────────────────────────


def launch_app(package: str) -> str:
    how = dev().launch_app(package)
    return f"OK launch({package}) via {how}"


def stop_app(package: str) -> str:
    dev().stop_app(package)
    return f"OK stop({package})"


def current_app() -> str:
    info = dev().current_app()
    if not info["package"]:
        return "UNKNOWN"
    return f"{info['package']}/{info['activity']}"


def list_apps(filter_text: str = "") -> str:
    """已安装的包，可选子串过滤。用于发现真实的 CEA1.3 包名。"""
    pkgs = dev().list_packages()
    if filter_text:
        pkgs = [p for p in pkgs if filter_text.lower() in p.lower()]
    return "\n".join(pkgs) if pkgs else "NONE"


# ── 同步 ─────────────────────────────────────────────────────────────────────


def wait_for_text(text: str, timeout: float = 12) -> str:
    """阻塞直到 ``text`` 出现在 UI 树中。截图轮询循环的廉价替代品。"""
    try:
        dev().wait_for(text, timeout=timeout)
        return f"OK found({text!r})"
    except TimeoutError:
        return f"TIMEOUT waiting for {text!r} after {timeout}s"


def sleep(seconds: float = 1.0) -> str:
    time.sleep(min(float(seconds), 5.0))
    return f"OK slept {seconds}s"


# ── 技能 ─────────────────────────────────────────────────────────────────────


def run_skill(name: str, params_json: str = "{}") -> str:
    """外部 MCP 显式调用的 YAML 配方执行器；普通 Agent 不调用它。

    延迟 import 让 ``skills`` 保持在 bridge 依赖方向之外。
    """
    import json

    from autogui.skills.base import SkillMiss, run_by_name

    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as e:
        return f"BAD_PARAMS: {e}"
    try:
        trace = run_by_name(name, params)
    except SkillMiss as e:
        # 将失败原因交给显式调用方处理。
        return f"SKILL_MISS {name}: {e}"
    except ADBError as e:
        return f"SKILL_ADB_FAIL {name}: {e}"
    return "OK skill " + name + "\n" + "\n".join(trace)


# ── 工具注册表（供 mcp_server.py 消费） ───────────────────────────────────────

TOOLS = [
    get_screen_size,
    capture_screenshot_b64,
    dump_ui_tree,
    ui_candidates,
    find_and_center,
    tap,
    long_press,
    swipe,
    input_text,
    keyevent,
    press_back,
    press_home,
    launch_app,
    stop_app,
    current_app,
    list_apps,
    wait_for_text,
    sleep,
    run_skill,
]
