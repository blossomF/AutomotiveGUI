"""面向模型的提示词文本。

*结构*沿用 ``LightGUIAgent/lightguiagent/claude_client.py:18-164``（系统
规则 + 每个动作的 JSON 示例 + 语言匹配 + explain/summary 字段）——这是好
结构，值得保留。其中所有 App 相关内容全部重写：原文写死了手机端包名
（美团/淘宝/微信/支付宝，``claude_client.py:160-163``），CEA1.3 车机上
没有；其恢复建议是“在网格 A1-B3 附近找返回键”，在横屏中控上是错的。

反思沿用 ``MobileAgent/Mobile-Agent-v2/MobileAgent/prompt.py:77-129``
（动作前后两图 A/B/C），但要求只回一个大写字母——因为原文调用方做
子串匹配（``run.py:401`` ``if 'A' in reflect``），模型自由思考里出现的
任何拉丁字母或 "ADB"/"Activity" 都会误触发。
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
    """tree 路由——**不发图**。这是 token 经济的路径。"""
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
    """兜底路由——一张叠加了网格的截图。"""
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


# ── 反思 ─────────────────────────────────────────────────────────────────────


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


# ── 解析 ─────────────────────────────────────────────────────────────────────


def strip_code_fence(text: str) -> str:
    """如果模型还是把答案包在 ```json 围栏里，剥掉它。

    跟随 ``claude_client.py:356-360``；我们额外处理围栏只出现在一边、
    以及对象后面还有散文的情况。
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
    """提取第一个完整的 JSON 对象，容忍前后散文。"""
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
    """严格 A/B/C。返回 'A'、'B'、'C' 或 'UNKNOWN'。

    绝不做子串匹配（模块 docstring 里 v2 的 bug）：答案必须是回复本身——
    一个裸字母、位于回复开头的字母（"A. ..."），或显式的
    "回答：A" / "ANSWER: A" 标签。仅包含 'A' 单词（'ADB'、'App'、
    'Activity'）的自由推理仍视为 UNKNOWN。
    """
    t = (text or "").strip().upper()
    if not t:
        return "UNKNOWN"
    # 显式 "回答：C" / "ANSWER: C" —— 取标签后紧跟的字母。
    m = re.match(r"^(?:回答|ANSWER)\s*[:：]?\s*([ABC])(?![A-Z])", t)
    if m:
        return m.group(1)
    # 第一个词就是答案，可跟标点（"A."）。
    first = re.split(r"\s+", t, maxsplit=1)[0].strip(".,;:!?。、，；：！？")
    return first if first in ("A", "B", "C") else "UNKNOWN"


def encode_png_base64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode("ascii")
