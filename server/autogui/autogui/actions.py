"""动作空间 —— prompt、解析器、执行器三方共享的唯一事实来源。

比 ``LightGUIAgent/lightguiagent/config.py:150-156`` 的 5 个动作更宽——对方
完全没有 BACK 和 HOME：在那套代码里，“恢复”意味着提示词告诉模型返回键
*通常*在左上角、网格 A1-B3 附近（``claude_client.py:151-156``）。在带常驻
dock 的横屏车机上这个提示是错的，模型会白白烧掉步数找按钮。见
技术方案评估.md §2 C-10。

``FAIL`` 同样重要：没有显式的“放弃”动作，卡住的 agent 除了
``COMPLETE``（错的）或耗尽 ``max_steps``（慢且贵）之外，没有提前结束任务的途径。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionSpec:
    name: str
    args: tuple[str, ...]
    meaning: str


ACTIONS: tuple[ActionSpec, ...] = (
    ActionSpec("CLICK", ("node_ref | grid",), "Tap one on-screen candidate"),
    ActionSpec("LONG_PRESS", ("node_ref | grid",), "Hold ~0.8s — context menus, drag handles"),
    ActionSpec("TYPE", ("text", "clear_first?"), "Type into the focused field"),
    ActionSpec("SCROLL", ("direction",), "Scroll down|up (or right|left on landscape lists)"),
    ActionSpec("BACK", (), "System back — leaves a wrong page or dismisses a dialog"),
    ActionSpec("HOME", (), "System home — escape hatch when a page is unrecoverable"),
    ActionSpec("LAUNCH", ("app",), "Start an app by package name"),
    ActionSpec("SKILL", ("name", "params?"), "Run a declarative recipe instead of stepping"),
    ActionSpec("WAIT", ("seconds?",), "Let an animation or network load settle"),
    ActionSpec("COMPLETE", ("result?",), "Task done — say what was achieved"),
    ActionSpec("FAIL", ("reason",), "Task cannot be completed — stop and explain"),
)

ACTION_NAMES: tuple[str, ...] = tuple(a.name for a in ACTIONS)
TERMINAL: frozenset[str] = frozenset({"COMPLETE", "FAIL"})

# 可以用“屏幕指纹变化”来校验效果、而不必花一次 VLM 调用的动作。
MUTATING: frozenset[str] = frozenset({"CLICK", "LONG_PRESS", "TYPE", "SCROLL", "BACK", "HOME", "LAUNCH", "SKILL"})

SCROLL_DIRECTIONS = ("down", "up", "left", "right")


def parse_action(raw: dict) -> dict:
    """把模型输出的动作 dict 校验并归一化。

    遇到任何不可用的输入抛 ``ValueError``；``brain.glm_client`` 会带着
    失败原因重试一次，再失败则图降级处理而不是崩溃。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"action must be an object, got {type(raw).__name__}")
    name = str(raw.get("type") or raw.get("action") or "").strip().upper()
    if name not in ACTION_NAMES:
        raise ValueError(f"unknown action {name!r}; valid: {', '.join(ACTION_NAMES)}")

    out: dict = {"type": name, "explain": str(raw.get("explain", ""))[:200]}

    if name in ("CLICK", "LONG_PRESS"):
        if "node_ref" in raw and raw["node_ref"] is not None:
            try:
                out["node_ref"] = int(str(raw["node_ref"]).lstrip("#"))
            except (TypeError, ValueError):
                raise ValueError(f"node_ref must be an integer, got {raw['node_ref']!r}")
        elif raw.get("grid"):
            out["grid"] = str(raw["grid"]).strip().upper()
        else:
            raise ValueError(f"{name} needs node_ref or grid")
    elif name == "TYPE":
        if "text" not in raw:
            raise ValueError("TYPE needs text")
        out["text"] = str(raw["text"])
        out["clear_first"] = bool(raw.get("clear_first", False))
    elif name == "SCROLL":
        d = str(raw.get("direction", "down")).strip().lower()
        if d not in SCROLL_DIRECTIONS:
            raise ValueError(f"SCROLL direction must be one of {SCROLL_DIRECTIONS}, got {d!r}")
        out["direction"] = d
    elif name == "LAUNCH":
        out["app"] = str(raw.get("app", "")).strip()
        if not out["app"]:
            raise ValueError("LAUNCH needs app (package name)")
    elif name == "SKILL":
        out["skill"] = str(raw.get("name", "")).strip()
        out["params"] = raw.get("params") or {}
        if not out["skill"]:
            raise ValueError("SKILL needs name")
    elif name == "WAIT":
        try:
            out["seconds"] = min(float(raw.get("seconds", 1.5)), 5.0)
        except (TypeError, ValueError):
            out["seconds"] = 1.5
    elif name == "COMPLETE":
        out["result"] = str(raw.get("result", ""))[:300]
    elif name == "FAIL":
        out["reason"] = str(raw.get("reason", raw.get("explain", "unknown")))[:300]

    return out
