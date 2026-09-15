"""Model-facing prompt text.

The *shape* follows ``LightGUIAgent/lightguiagent/claude_client.py:18-164`` (system
rules + per-action JSON examples + language matching + explain/summary fields), which
is a good structure and worth keeping. Everything app-specific in that prompt was
rewritten: it hardcodes phone packages (美团/淘宝/微信/支付宝, ``claude_client.py:160-163``)
that are not on a CEA1.3 head unit, and its recovery advice is "look for back in grid
A1-B3", which is wrong on a landscape dash.

Reflection follows ``MobileAgent/Mobile-Agent-v2/MobileAgent/prompt.py:77-129``
(before/after two-image A/B/C) but asks for a bare letter, because the original's
caller does substring matching — ``run.py:401`` ``if 'A' in reflect`` — which any
Latin letter or "ADB"/"Activity" in the model's free-form thought trips over.
"""

from __future__ import annotations

import base64
import json
import re

from autogui.actions import ACTION_NAMES, SCROLL_DIRECTIONS

LANGUAGE_RULE = (
    "explain / progress / entities 的取值必须与用户任务同语言（中文任务用中文说明）。"
)

OUTPUT_RULE = (
    "只输出一个 JSON 对象。不要 markdown 代码块，不要解释文字，不要输出多个对象。"
)

DECIDE_SYSTEM = (
    "你是车机（Android Automotive 中控横屏）GUI Agent 的决策器。"
    "每一轮你会看到任务、历史、已确认信息，以及当前屏幕，你要给出**一个**下一步动作。\n\n"
    "可用动作（type 字段取值）：" + ", ".join(ACTION_NAMES) + "\n"
    "SCROLL 的 direction 只能是 " + "/".join(SCROLL_DIRECTIONS) + "。\n\n"
    "规则：\n"
    "1. 一次只做一个动作，不要规划多步；下一轮你会重新观察。\n"
    "2. 只在信息足够时判定 COMPLETE；确认不了就继续操作，绝不臆测成功。\n"
    "3. 反复无效时用 BACK / HOME / LAUNCH 换路径，不要重复同一个动作。\n"
    "4. 需要弹隐私协议/广告弹窗时，优先点“同意/以后再说/关闭/我知道了”这类按钮。\n"
    "5. 涉及金额、下单、发送消息等不可逆操作前，先确认界面已经到位。\n"
    "6. " + LANGUAGE_RULE + "\n"
    "7. " + OUTPUT_RULE
)

DECIDE_SCHEMA_TEXT = (
    '{"type":"CLICK","node_ref":7,"explain":"...","progress":"...","entities":{"时间":"19:00"}}'
)


def decide_user_text(context_text: str) -> list[dict]:
    """Tree route — **no image**. This is the token-economy path."""
    return [
        {"type": "text", "text": context_text},
        {
            "type": "text",
            "text": (
                "上面是**当前屏幕的可点击候选列表**。请从中选一个完成下一步的候选，"
                "回复其 node_ref（整数）。若列表里没有所需控件，可改用 SCROLL / BACK / HOME / LAUNCH，"
                "或回复 type=\"grid\" 给出网格位置。\n"
                f"输出示例：{DECIDE_SCHEMA_TEXT}\n现在输出："
            ),
        },
    ]


def decide_user_vlm(context_text: str, jpeg_b64: str, grid_spec: str) -> list[dict]:
    """Fallback route — one annotated grid frame."""
    return [
        {"type": "text", "text": context_text},
        {
            "type": "text",
            "text": (
                f"当前屏幕截图已叠加 {grid_spec} 网格（列 A-… 行 1-…）。"
                "控件树不可用，请用网格位置回答，字段名 grid，例如 \"J5\"。"
            ),
        },
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}},
        {"type": "text", "text": "输出示例：" + DECIDE_SCHEMA_TEXT.replace('"node_ref":7', '"grid":"J5"') + "\n现在输出："},
    ]


def build_decide_messages(context_text: str, jpeg_b64: str | None, grid_spec: str) -> list[dict]:
    msgs = [{"role": "system", "content": DECIDE_SYSTEM}]
    if jpeg_b64:
        msgs.append({"role": "user", "content": decide_user_vlm(context_text, jpeg_b64, grid_spec)})
    else:
        msgs.append({"role": "user", "content": decide_user_text(context_text)})
    return msgs


# ── reflection ───────────────────────────────────────────────────────────────────

REFLECT_SYSTEM = (
    "你是操作效果审计器。给你同一台车机在**一个动作之前和之后**的两张截图，以及那个动作。"
    "判断动作是否产生了预期效果。\n"
    "只回答一个大写字母，不要任何其它文字：\n"
    "A = 动作生效，页面按预期变化了。\n"
    "B = 动作生效但进了错误页面（需要后退）。\n"
    "C = 动作没有产生任何可见变化。"
)


def build_reflect_messages(action_desc: str, goal: str, before_b64: str, after_b64: str) -> list[dict]:
    return [
        {"role": "system", "content": REFLECT_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"任务目标：{goal}\n刚执行的动作：{action_desc}\n第一张=动作前，第二张=动作后。\n回答："},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{before_b64}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{after_b64}"}},
            ],
        },
    ]


# ── parsing ──────────────────────────────────────────────────────────────────────


def strip_code_fence(text: str) -> str:
    """Remove a ```json fence if the model wrapped its answer anyway.

    Follows ``claude_client.py:356-360``; ours also handles the fence appearing on
    only one side, and trailing prose after the object.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def first_json_object(text: str) -> dict:
    """Extract the first complete JSON object, tolerating surrounding prose."""
    t = strip_code_fence(text)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                obj = json.loads(t[start : i + 1])
                if not isinstance(obj, dict):
                    raise ValueError("JSON value was not an object")
                return obj
    raise ValueError(f"unbalanced JSON object in response: {text[:200]!r}")


def parse_reflection(text: str) -> str:
    """Strict A/B/C. Returns 'A', 'B', 'C', or 'UNKNOWN'.

    Never substring-match (see module docstring on the v2 bug): the answer must be
    the reply itself — a bare letter, a letter leading the reply ("A. ..."), or an
    explicit "回答：A" / "ANSWER: A" label. Free-form reasoning that merely contains
    an 'A' word ('ADB', 'App', 'Activity') stays UNKNOWN.
    """
    t = (text or "").strip().upper()
    if not t:
        return "UNKNOWN"
    # Explicit "回答：C" / "ANSWER: C" — take the letter right after the label.
    m = re.match(r"^(?:回答|ANSWER)\s*[:：]?\s*([ABC])(?![A-Z])", t)
    if m:
        return m.group(1)
    # The very first word is the answer, optionally followed by punctuation ("A.").
    first = re.split(r"\s+", t, maxsplit=1)[0].strip(".,;:!?。、，；：！？")
    return first if first in ("A", "B", "C") else "UNKNOWN"


def encode_png_base64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode("ascii")
