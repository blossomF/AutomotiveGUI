"""The tool surface — plain Python functions, no MCP and no LangGraph imports.

This module is the seam the whole architecture hangs on (技术方案评估.md §3 constraint
1). Everything the agent can *do* is here as an ordinary function returning a compact
string. ``mcp_server.py`` only decorates these; ``core/graph.py`` either calls them
directly (``exec_mode: local``) or through the MCP client (``exec_mode: mcp``).

Two deliberate conventions:

* **Return compressed ``str``, never dict.** Tool output goes straight into the model
  context. The ``{"ok": true, "code": 0, ...}`` wrapping an external framework would
  emit is pure burned tokens. (Follows ``android-agent/gitd/mcp_server.py:106``, which
  returns ``str`` from every tool.)
* **No ``device`` parameter.** android-agent threads a serial through all 62 tools
  because it drives many phones. We target one head unit, bound at startup — that
  parameter would be repeated tokens on every single call.
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
    """Resolve and remember the single target device. Called once at startup."""
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


# ── perception (text surfaces; image bytes go through perception/capture.py) ─────


def get_screen_size() -> str:
    """Screen pixel size, e.g. ``1920x1080``."""
    w, h = dev().screen_size()
    return f"{w}x{h}"


def capture_screenshot_b64() -> str:
    """Full-resolution PNG of the main display, base64-encoded.

    Only the VLM-degraded route needs this through the tool surface; the tree route
    never ships an image at all, which is where the token economy comes from.
    """
    return base64.b64encode(dev().screenshot()).decode("ascii")


def dump_ui_tree(brief: bool = True) -> str:
    """Raw ``uiautomator`` XML hierarchy.

    ``brief=True`` returns node count plus a few samples rather than the whole tree —
    a raw dump is 40-120 KB and would wreck the context budget. The compact candidate
    list the model actually sees is produced by ``perception/tree.py``.
    """
    xml = dev().dump_xml()
    if not brief:
        return xml
    total = len(nodes(xml))
    return f"nodes={total} bytes={len(xml)}"


def ui_candidates(limit: int = 40) -> str:
    """Compact numbered candidate list — the path-B decision surface.

    Format per line: ``#12 搜索 [960,86]``. The model answers with the ``node_ref``
    integer and never copies coordinates, which removes a whole class of parsing
    errors (and is why tree mode beats grid mode on accuracy in our 9/19 tests).
    """
    from autogui.perception.tree import candidates_from_xml, render_candidates

    xml = dev().dump_xml()
    cands = candidates_from_xml(xml, limit=limit)
    if not cands:
        return "TREE_EMPTY"
    return render_candidates(cands)


def find_and_center(match: str) -> str:
    """Locate the first node whose text/content-desc contains ``match``; return ``x,y``."""
    xml = dev().dump_xml()
    node = find_node(xml, text=match) or find_node(xml, desc=match)
    if not node:
        return f"NOT_FOUND:{match}"
    from autogui.bridge.adb import node_center

    c = node_center(node)
    return f"{c[0]},{c[1]}" if c else f"NO_BOUNDS:{match}"


# ── actuation ────────────────────────────────────────────────────────────────────


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
    """Type into the focused field, degrading yadb -> ADBKeyboard -> ASCII."""
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


# ── apps ─────────────────────────────────────────────────────────────────────────


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
    """Installed packages, optionally substring-filtered. For discovering real CEA1.3 names."""
    pkgs = dev().list_packages()
    if filter_text:
        pkgs = [p for p in pkgs if filter_text.lower() in p.lower()]
    return "\n".join(pkgs) if pkgs else "NONE"


# ── synchronisation ──────────────────────────────────────────────────────────────


def wait_for_text(text: str, timeout: float = 12) -> str:
    """Block until ``text`` shows up in the UI tree. The cheap substitute for a screenshot loop."""
    try:
        dev().wait_for(text, timeout=timeout)
        return f"OK found({text!r})"
    except TimeoutError:
        return f"TIMEOUT waiting for {text!r} after {timeout}s"


def sleep(seconds: float = 1.0) -> str:
    time.sleep(min(float(seconds), 5.0))
    return f"OK slept {seconds}s"


# ── skills ───────────────────────────────────────────────────────────────────────


def run_skill(name: str, params_json: str = "{}") -> str:
    """Execute a declarative YAML skill. Zero VLM calls.

    Late import keeps ``skills`` out of the bridge's dependency direction.
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
        # Not an error to report as failure — the caller degrades to tree/vlm.
        return f"SKILL_MISS {name}: {e}"
    except ADBError as e:
        return f"SKILL_ADB_FAIL {name}: {e}"
    return "OK skill " + name + "\n" + "\n".join(trace)


# ── registry consumed by mcp_server.py ───────────────────────────────────────────

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
