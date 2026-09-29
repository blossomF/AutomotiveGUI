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

import re
from dataclasses import dataclass

# tree（纯文本）模式没有网格图，模型无法回答 E5 这类格号；当它需要点中一个
# 长条控件的特定比例位置（典型：把播放进度条跳到约 70%——node_ref 只能点中
# 控件中心）时，会直接给屏幕像素坐标。实测 GLM 写出 grid='[880, 885]'，旧代码
# 只认字母格号，动作直接 ValueError 报废。下面两个小工具把这种"显式像素坐标"
# 识别出来；字母格号（"E5"/"J10"）仍走 grid 标签路径。
_GRID_LABEL_RE = re.compile(r"^[A-Za-z]\d+$")
_COORD_NUM_RE = re.compile(r"-?\d+")


def parse_coord(value) -> tuple[int, int] | None:
    """把显式像素坐标 CLICK 目标解析成 ``(x, y)``；不是坐标对时返回 ``None``。

    接受 ``[880, 885]`` / ``"(880,885)"`` / ``"880 885"`` 字符串，以及两个
    数字的 list/tuple。字母格号或取不出恰好两个整数的一律返回 ``None``。
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if _GRID_LABEL_RE.match(s):
        return None
    nums = _COORD_NUM_RE.findall(s)
    if len(nums) == 2:
        return int(nums[0]), int(nums[1])
    return None


@dataclass(frozen=True)
class ActionSpec:
    name: str
    args: tuple[str, ...]
    meaning: str


ACTIONS: tuple[ActionSpec, ...] = (
    ActionSpec("CLICK", ("node_ref | grid",), "Tap one on-screen candidate"),
    ActionSpec("LONG_PRESS", ("node_ref | grid",), "Hold ~0.8s — context menus, drag handles"),
    ActionSpec("TYPE", ("text", "clear_first?", "submit?"),
               "Type into the focused field; submit=true presses ENTER after typing (search boxes)"),
    ActionSpec("SCROLL", ("direction", "node_ref?", "amount?"),
               "Scroll a specific list by a fraction of its viewport"),
    ActionSpec("ROLL_TO", ("node_ref", "target", "delta_units?"),
               "Adjust a readable value wheel to the target, checking each movement"),
    ActionSpec("DRAG", ("from", "to", "duration?"),
               "Press-and-drag between two pixel points [x,y] — sliders/seek bars IGNORE taps; "
               "drag the thumb from its current position to the target ratio"),
    ActionSpec("BACK", (), "System back — leaves a wrong page or dismisses a dialog"),
    ActionSpec("HOME", (), "System home — escape hatch when a page is unrecoverable"),
    ActionSpec("LAUNCH", ("app",), "Start an app by package name"),
    ActionSpec("SKILL", ("name", "params?"), "Load task guidance; no device action; next decision remains with the model"),
    ActionSpec("WAIT", ("seconds?",), "Let an animation or network load settle"),
    ActionSpec("COMPLETE", ("result?",), "Task done — say what was achieved"),
    ActionSpec("FAIL", ("reason",), "Task cannot be completed — stop and explain"),
)

ACTION_NAMES: tuple[str, ...] = tuple(a.name for a in ACTIONS)
TERMINAL: frozenset[str] = frozenset({"COMPLETE", "FAIL"})

# 可以用“屏幕指纹变化”来校验效果、而不必花一次 VLM 调用的动作。
MUTATING: frozenset[str] = frozenset({"CLICK", "LONG_PRESS", "TYPE", "SCROLL", "ROLL_TO", "DRAG", "BACK", "HOME", "LAUNCH"})

SCROLL_DIRECTIONS = ("down", "up", "left", "right")


def parse_action(raw: dict) -> dict:
    """把模型输出的动作 dict 校验并归一化。

    遇到任何不可用的输入抛 ``ValueError``；``brain.glm_client`` 会带着
    失败原因重试一次，再失败则图降级处理而不是崩溃。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"action must be an object, got {type(raw).__name__}")
    name = str(raw.get("type") or raw.get("action") or "").strip().upper()
    # 容错：旧版 prompt 曾示范 {"type":"grid",...}，模型偶发沿用。GRID 不是
    # 独立动作，实质就是 CLICK 一个 grid 标签/像素坐标目标，归一到 CLICK。
    if name == "GRID":
        name = "CLICK"
    if name not in ACTION_NAMES:
        raise ValueError(f"unknown action {name!r}; valid: {', '.join(ACTION_NAMES)}")

    out: dict = {"type": name, "explain": str(raw.get("explain", ""))[:200]}

    if name in ("CLICK", "LONG_PRESS"):
        if "node_ref" in raw and raw["node_ref"] is not None:
            try:
                out["node_ref"] = int(str(raw["node_ref"]).lstrip("#"))
            except (TypeError, ValueError):
                raise ValueError(f"node_ref must be an integer, got {raw['node_ref']!r}")
        elif raw.get("grid") is not None:
            coord = parse_coord(raw["grid"])
            if coord is not None:
                # tree 模式给的显式像素坐标；越界夹紧在执行侧（那里才知道屏幕尺寸）。
                out["coord"] = coord
            else:
                g = str(raw["grid"]).strip().upper()
                if not g:
                    raise ValueError(f"{name} needs node_ref or grid")
                out["grid"] = g
        else:
            raise ValueError(f"{name} needs node_ref or grid")
    elif name == "TYPE":
        if "text" not in raw:
            raise ValueError("TYPE needs text")
        out["text"] = str(raw["text"])
        out["clear_first"] = bool(raw.get("clear_first", False))
        # submit=true：输入完成后补发一次 ENTER。仅用于搜索框等"回车即提交"
        # 的单行输入框（实测车机 QQ音乐：键盘弹起遮挡结果，ENTER 同时触发搜索
        # 并收起键盘）。普通表单/多行输入不要带，避免误提交。
        out["submit"] = bool(raw.get("submit", False))
    elif name == "SCROLL":
        d = str(raw.get("direction", "down")).strip().lower()
        if d not in SCROLL_DIRECTIONS:
            raise ValueError(f"SCROLL direction must be one of {SCROLL_DIRECTIONS}, got {d!r}")
        out["direction"] = d
        if raw.get("node_ref") is not None:
            try:
                out["node_ref"] = int(str(raw["node_ref"]).lstrip("#"))
            except (TypeError, ValueError):
                raise ValueError("SCROLL node_ref must be an integer")
        try:
            amount = float(raw.get("amount", 0.6))
        except (TypeError, ValueError):
            raise ValueError("SCROLL amount must be a fraction from 0.1 to 0.9")
        if not 0.1 <= amount <= 0.9:
            raise ValueError("SCROLL amount must be a fraction from 0.1 to 0.9")
        out["amount"] = amount
    elif name == "ROLL_TO":
        try:
            out["node_ref"] = int(str(raw["node_ref"]).lstrip("#"))
        except (KeyError, TypeError, ValueError):
            raise ValueError("ROLL_TO needs an integer node_ref")
        out["target"] = str(raw.get("target", "")).strip()
        if not out["target"]:
            raise ValueError("ROLL_TO needs target, e.g. 19时")
        if raw.get("delta_units") is not None:
            try:
                out["delta_units"] = int(raw["delta_units"])
            except (TypeError, ValueError):
                raise ValueError("ROLL_TO delta_units must be an integer")
    elif name == "DRAG":
        # 进度条/滑块专用：from/to 都是显式像素 [x,y]。起拖点必须是 thumb 当前
        # 位置（模型按 当前时间/总时长 × 轨道 bounds 算），不是轨道起点——实测
        # 车机 QQ音乐 sb_seekBar 对轨道空白处的 tap/swipe 都不响应，只有按住
        # thumb 拖动才 seek。
        p_from = parse_coord(raw.get("from"))
        p_to = parse_coord(raw.get("to"))
        if p_from is None or p_to is None:
            raise ValueError("DRAG needs from and to as pixel pairs, e.g. \"from\":[228,885],\"to\":[952,885]")
        out["from"] = p_from
        out["to"] = p_to
        try:
            out["duration"] = max(100, min(int(raw.get("duration", 400)), 2000))
        except (TypeError, ValueError):
            out["duration"] = 400
    elif name == "LAUNCH":
        out["app"] = str(raw.get("app", "")).strip()
        if not out["app"]:
            raise ValueError("LAUNCH needs app (package name)")
    elif name == "SKILL":
        # 字段名容错：type 已经声明了 SKILL，模型再写 "name" 并不自然。
        # 实测 GLM 会写 {"type":"SKILL","skill":"search_video","params":{...}}
        # 或把参数放在 "args"。两种写法语义完全一致，都接受。
        out["skill"] = str(raw.get("name") or raw.get("skill") or "").strip()
        out["params"] = raw.get("params") or raw.get("args") or {}
        if not out["skill"]:
            raise ValueError(f"SKILL needs name (or skill); got keys {sorted(raw)}")
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
