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

from autogui.actions import ACTIONS, ACTION_NAMES, SCROLL_DIRECTIONS

# 每个动作的字段清单，**从 ACTIONS 自动生成**——actions.py 自称是
# "prompt、解析器、执行器三方共享的唯一事实来源"，但 prompt 原先只取了
# ACTION_NAMES（动作名），没取 args（字段定义）。结果是模型知道有 SKILL /
# TYPE 这些动作，却不知道它们要填什么字段，实测产出：
#   {"type":"SKILL","skill":"search_video",...}   -> 解析器读 name，报 SKILL needs name
#   {"type":"TYPE","explain":"输入关键词钢铁侠"}    -> 解析器读 text，报 TYPE needs text
# 由 ACTIONS 生成而不是手写，才能保证以后加动作时不会再漂移。
_ACTION_FIELDS = "；".join(
    f"{a.name}({', '.join(a.args)})" for a in ACTIONS if a.args
)

_ACTION_EXAMPLES = (
    '{"type":"CLICK","node_ref":7}'
    ' / {"type":"TYPE","text":"钢铁侠 第一季","submit":true}'
    ' / {"type":"SKILL","name":"bilibili.search_video","params":{"keyword":"钢铁侠 第一季"}}'
    ' / {"type":"COMPLETE","result":"已播放"}'
)

_SKILL_RULE = (
    "SKILL 只加载一份操作指导，不会自动点击或完成任务。只有任务的应用和目标都与"
    "「可用技能」摘要明确匹配，且参数能从用户任务中确定时才选 SKILL；"
    "name 原样照抄完整名字（如 bilibili.search_video），参数放 params。"
    "已加载指导后不要再次选择同一 SKILL，应依据当前屏幕给出具体动作。"
    "不匹配时直接操作控件，不要猜技能名或参数。\n"
)

LANGUAGE_RULE = (
    "explain / progress / entities 的取值必须与用户任务同语言（中文任务用中文说明）。"
)

# 无语义图标按钮（无 text/content-desc，rid 只是 iv_xxxCtrl1..N）在 tree 模式下
# 无法从名字判断功能，模型凭"通常最左是模式"这类手机端先验会点错——实测把
# iv_bottomCtrl1（收藏红心）当成播放模式。这里记录实测的车机 App 图标布局。
APP_ICON_HINTS = (
    "已知车机 App 图标布局（这些按钮无文字标签，按位置辨认，不要凭手机端习惯猜）：\n"
    "- QQ音乐全屏播放页底部控制栏从左到右：收藏红心(iv_bottomCtrl1，selected=true=已收藏)、"
    "播放模式(iv_bottomCtrl2，顺序/随机/单曲循环，点击有\"已切换为…\"toast)、上一首、播放/暂停、"
    "下一首、播放队列(iv_bottomCtrl3)、音效(iv_bottomCtrl4)。\n"
    "- QQ音乐「我的/设置」入口：主页右上角 iv_mine(content-desc「我的#个人中心」) 进我的页，"
    "我的页顶部 iv_setting(content-desc「设置」) 才是设置。播放页顶部的 iv_topCtrl1~3 是 AI/分享/音效等，"
    "不是设置，不要逐个试。找不到目标入口时先返回主页从导航重进，不要穷举同类图标。\n"
    "- 车机导航(com.fawvw.vehicle.navi.ceaapp)首页左下工具栏：视角切换是 main_button_car_angle_layout"
    "（content-desc「地图视角#调整视角」，文字在「车头向上/正北向上」间循环）；右下角的"
    " main_button_scale_gps_layout 是回车位/GPS定位锁定钮，不是视角切换。充电图层、详细播报(静音)、更多也在左下栏。\n"
)

OUTPUT_RULE = (
    "只输出一个 JSON 对象。不要 markdown 代码块，不要解释文字，不要输出多个对象。"
)

DECIDE_SYSTEM = (
    "你是车机（Android Automotive 中控横屏）GUI Agent 的决策器。"
    "每一轮你会看到任务、历史、已确认信息，以及当前屏幕，你要给出**一个**下一步动作。\n\n"
    "可用动作（type 字段取值）：" + ", ".join(ACTION_NAMES) + "\n"
    "各动作字段：" + _ACTION_FIELDS + "。\n"
    "SCROLL 的 direction 只能是 " + "/".join(SCROLL_DIRECTIONS) + "。\n"
    + _SKILL_RULE +
    "LAUNCH 的 app 填包名，取下方「可用应用」列表（如 com.bilibili.bilithings），不要填中文名。\n"
    "写法示例：" + _ACTION_EXAMPLES + "\n"
    "字段必须写在对应字段里（例如要输入的文字放 text，不要只写在 explain 里）。\n\n"
    + APP_ICON_HINTS + "\n"
    "规则：\n"
    "1. 一次只做一个动作，不要规划多步；下一轮你会重新观察。\n"
    "2. 只在信息足够时判定 COMPLETE；确认不了就继续操作，绝不臆测成功。\n"
    "3. 反复无效时用 BACK / HOME / LAUNCH 换路径，不要重复同一个动作。\n"
    "4. 需要弹隐私协议/广告弹窗时，优先点“同意/以后再说/关闭/我知道了”这类按钮。\n"
    "5. 涉及金额、下单、发送消息等不可逆操作前，先确认界面已经到位。\n"
    "6. 在搜索框输入关键词时，TYPE 带 \"submit\": true——输入完成会自动按回车提交搜索，"
    "并收起遮挡结果列表的软键盘，不要再多发一个点击动作。账号/密码/昵称/多行文本等"
    "非搜索输入不要带 submit（回车可能误提交表单）。\n"
    "7. " + LANGUAGE_RULE + "\n"
    "8. 进度条/音量等滑块不要 CLICK（实测不响应），用 DRAG 从 thumb 当前位置拖到目标位置；"
    "DRAG 的 from/to 都是屏幕像素 [x,y]（tree 模式按候选 bounds 与时间文本计算）。\n"
    "9. " + OUTPUT_RULE
)

DECIDE_SCHEMA_TEXT = (
    '{"type":"CLICK","node_ref":7,"explain":"...","progress":"...","entities":{"时间":"19:00"}}'
)


def decide_user_text(context_text: str, screen: tuple[int, int] | None = None) -> str:
    """tree 路由——**不发图**。这是 token 经济的路径。

    返回**单个字符串**而不是 ``[{"type":"text",...}]`` 数组：纯文本请求本就
    没必要用多模态分段，而且部分严格的 OpenAI 兼容网关不把 ``content=数组``
    的纯文本 user 消息识别为 user 角色（实测 ts-glm-53-flash 直接 400
    "messages containing only assistant or system roles are not supported"，
    只剩 system 被识别）。只有必须内嵌图片的 vlm 路径才用数组 content。
    """
    coord_rule = ""
    if screen:
        w, h = screen
        coord_rule = (
            f"\n本路是纯文本、没有网格图，grid 字段**不要**写 E5 这类字母格号（那是看图模式才用）。"
            f"屏幕像素尺寸为 {w}×{h}，候选行末尾的 [x,y] 就是该控件中心的真实像素坐标。"
            "绝大多数点击直接回复 node_ref 即可。\n"
            "**进度条/滑块只能拖不能点**（实测车机音乐 App 的 seekBar 对单击完全无响应，"
            "必须按住滑块 thumb 拖动）。要跳到指定比例时用 DRAG，坐标自己算：\n"
            "  1) 从候选文本读到当前时间 cur 与总时长 total（如 00:28 / 04:31），以及轨道 bounds=[x0,y0][x1,y1]；\n"
            "  2) thumb 当前像素 fx = x0 + cur/total ×(x1-x0)，y 取轨道中心；\n"
            "  3) 目标像素 tx = x0 + 目标比例×(x1-x0)；\n"
            '  4) 输出 {"type":"DRAG","from":[fx,y],"to":[tx,y],"duration":500}。\n'
            "拖动距离超过约 400px 时 duration 取 1200~1500（短距离快速滑动会因触摸采样稀疏"
            "导致 thumb 只跟到一半；实测 824px/500ms 落到 42%，824px/1500ms 精确到 75%）。\n"
            "例：00:28/04:31 跳到约 3/4，轨道 [112,873][1232,897] → "
            '{"type":"DRAG","from":[228,885],"to":[952,885],"duration":1500}。'
            "注意：未登录试听歌曲通常只允许在试听段内拖动，拖到超出部分不会跳转，"
            "观察到拖动后时间没变就不要重复拖。"
        )
    return (
        context_text + "\n\n"
        "上面是**当前屏幕的可点击候选列表**。请从中选一个完成下一步的候选，"
        "回复其 node_ref（整数）。若列表里没有所需控件，可改用 SCROLL / BACK / HOME / LAUNCH。"
        f"{coord_rule}\n"
        f"输出示例：{DECIDE_SCHEMA_TEXT}\n现在输出："
    )


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


def build_decide_messages(
    context_text: str, jpeg_b64: str | None, grid_spec: str,
    screen: tuple[int, int] | None = None,
) -> list[dict]:
    msgs = [{"role": "system", "content": DECIDE_SYSTEM}]
    if jpeg_b64:
        msgs.append({"role": "user", "content": decide_user_vlm(context_text, jpeg_b64, grid_spec)})
    else:
        msgs.append({"role": "user", "content": decide_user_text(context_text, screen)})
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
