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
from pathlib import Path
from string import Template

from autogui.actions import ACTIONS, ACTION_NAMES, SCROLL_DIRECTIONS

# 每个动作的字段清单，**从 ACTIONS 自动生成**——actions.py 自称是
# "prompt、解析器、执行器三方共享的唯一事实来源"，但 prompt 原先只取了
# ACTION_NAMES（动作名），没取 args（字段定义）。结果是模型知道有 SKILL /
# TYPE 这些动作，却不知道它们要填什么字段，实测产出：
#   {"type":"SKILL","skill":"search_video",...}   -> 解析器读 name，报 SKILL needs name
#   {"type":"TYPE","explain":"输入关键词钢铁侠"}    -> 解析器读 text，报 TYPE needs text
# 由 ACTIONS 生成而不是手写，才能保证以后加动作时不会再漂移。
def _action_fields(route: str) -> str:
    fields = []
    for action in ACTIONS:
        if not action.args:
            continue
        args = list(action.args)
        if action.name in ("CLICK", "LONG_PRESS"):
            args[0] = "node_ref | coord" if route == "tree" else "point | node_ref | coord"
        fields.append(f"{action.name}({', '.join(args)})")
    return "；".join(fields)

_PROMPT_DIR = Path(__file__).with_name("prompt_templates")
# 一条模板一个文件；启动时加载，修改后重启服务生效。
_PROMPT_TEXT = {path.stem: path.read_text(encoding="utf-8")
                for path in sorted(_PROMPT_DIR.glob("*.txt"))}


def render(name: str, **values: object) -> str:
    """按文件名取独立提示词，严格替换动态字段；缺失字段立即报错。"""
    value = _PROMPT_TEXT.get(name)
    if not isinstance(value, str):
        raise ValueError(f"missing prompt template: {name}")
    return Template(value).substitute(**values).strip()


OUTPUT_RULE = render("output_rule")
DECIDE_SCHEMA_TEXT = render("decide_schema_tree")
def _decide_system(route: str) -> str:
    drag_rule = ("DRAG 的 from/to 都是设备屏幕像素 [x,y]。" if route == "tree"
                 else "DRAG 的 from/to 都是当前发送截图的像素 [x,y]，程序会换算为设备坐标。")
    return render(
        "decide_system",
        action_names=", ".join(ACTION_NAMES),
        action_fields=_action_fields(route),
        scroll_directions="/".join(SCROLL_DIRECTIONS),
        drag_coordinate_rule=drag_rule,
        output_rule=OUTPUT_RULE,
    )


DECIDE_SYSTEM = _decide_system("tree")
DECIDE_SYSTEM_VLM = _decide_system("vlm")


def decide_user_text(context_text: str, screen: tuple[int, int] | None = None) -> str:
    """tree 路由——**不发图**。这是 token 经济的路径。

    返回**单个字符串**而不是 ``[{"type":"text",...}]`` 数组：纯文本请求本就
    没必要用多模态分段，而且部分严格的 OpenAI 兼容网关不把 ``content=数组``
    的纯文本 user 消息识别为 user 角色（实测 ts-glm-53-flash 直接 400
    "messages containing only assistant or system roles are not supported"，
    只剩 system 被识别）。只有必须内嵌图片的 vlm 路径才用数组 content。
    """
    coord_rule = render("decide_tree_coords", width=screen[0], height=screen[1]) if screen else ""
    return render(
        "decide_tree_user", context_text=context_text, coord_rule=coord_rule,
        schema_text=DECIDE_SCHEMA_TEXT,
    )


def decide_user_vlm(context_text: str, jpeg_b64: str,
                    image_size: tuple[int, int] | None = None) -> list[dict]:
    """兜底路由——一张无覆盖物的截图。"""
    return [
        {"type": "text", "text": context_text},
        {
            "type": "text",
            "text": render("decide_vlm_screen",
                           image_width=(image_size or (0, 0))[0],
                           image_height=(image_size or (0, 0))[1]),
        },
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}},
        {"type": "text", "text": render("decide_vlm_output", schema_text=render("decide_schema_vlm"))},
    ]


def build_decide_messages(
    context_text: str, jpeg_b64: str | None,
    screen: tuple[int, int] | None = None,
    image_size: tuple[int, int] | None = None,
) -> list[dict]:
    msgs = [{"role": "system", "content": DECIDE_SYSTEM_VLM if jpeg_b64 else DECIDE_SYSTEM}]
    if jpeg_b64:
        msgs.append({"role": "user", "content": decide_user_vlm(context_text, jpeg_b64,
                                                              image_size or screen)})
    else:
        msgs.append({"role": "user", "content": decide_user_text(context_text, screen)})
    return msgs


# ── 反思 ─────────────────────────────────────────────────────────────────────


REFLECT_SYSTEM = render("reflect_system")


def build_reflect_messages(action_desc: str, goal: str, before_b64: str, after_b64: str) -> list[dict]:
    return [
        {"role": "system", "content": REFLECT_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": render("reflect_user", goal=goal, action_desc=action_desc)},
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
