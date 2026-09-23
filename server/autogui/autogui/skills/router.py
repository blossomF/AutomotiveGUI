"""任务 -> 技能路由。

两个入口，按以下顺序使用：

  1. ``match_keywords`` —— 确定性、零 VLM。每个配方在自己的 YAML 里
     声明触发词；任务包含触发词且能提取出参数时，直接运行配方，
     全程不调用模型。性能分和准确率分就来自这里：命中时约 1s/步，
     且不会定位错控件。
  2. 模型自己在任意 decide 步也可以回答 ``{"type":"SKILL","name":...,
     "params":{...}}``——它在永久提示词块里看到过 ``SkillBook.index()``。
     这覆盖关键词列表没预料到的说法。

两者都是建议性的：未命中时执行器内部抛 ``SkillMiss``，图会落回
通用 tree/vlm 路由。
"""

from __future__ import annotations

import re

from autogui.skills.base import Skill, SkillBook


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def match_keywords(task: str, book: SkillBook) -> tuple[Skill, dict] | None:
    """第一个声明的触发词出现在任务中的技能。

    引号内的片段和冒号后面的文本会被提取为第一个声明参数，
    例如"播放《七里香》"对 ``params: [song]`` 的技能产生
    ``{"song": "七里香"}``。
    """
    t = _norm(task)
    if not t:
        return None
    for skill in book.by_id.values():
        if not any(_norm(word) in t for word in skill.trigger_words() if word):
            continue
        # 半成品配方不参与关键词命中：YAML 里 app.package 仍是占位空值，说明这套
        # 技能还没用 --packages 绑定到具体 App。此时 launch 第一步必抛 SkillMiss，
        # click/type 也无法保证落在预期 App——关键词命中只会白跑（执行器还会重试
        # 再烧一轮）才降级回通用 tree/vlm。实测「播放他的第一首歌」命中空包名的
        # music.play_song 连烧 2 轮、「暂停」命中无 launch 步骤的 music.pause 也
        # 连烧 2 轮。统一在 app 包名为空时跳过整套配方，零代价落回模型；等包名
        # 配上后配方自然恢复启用。
        if not skill.package:
            continue
        return skill, _extract_params(task, skill)
    return None


_PARAM_PATTERNS = (
    re.compile(r"[《“\"'【\[](.+?)[》”\"'】\]]"),          # 括号内引用
    re.compile(r"[=:：]\s*(.+)$"),                          # key: value
    re.compile(r"(?:搜索|查找|找|播放|播放一下|来一首)\s*(.+)$"),  # 动词 + 宾语
)


def _strip_triggers(value: str, triggers: list[str]) -> str:
    """从抽出的参数里剥掉触发词。

    任务「播放哔哩哔哩钢铁侠第一季」按「动词+宾语」抽出来的是
    「哔哩哔哩钢铁侠第一季」——触发词「哔哩哔哩」回答的是*去哪个 App*，
    不是用户要搜的内容。带着它去搜索往往搜不到，配方里的 ``wait_for``
    等这整串出现就超时，整条配方降级给模型手点（实测多烧 5~6 步）。
    剥掉后只剩「钢铁侠第一季」，才是该拿去搜的词。
    """
    out = value
    # 长触发词先剥："哔哩哔哩" 先于 "哔哩"，避免留下半个词。
    for w in sorted((x for x in triggers if x), key=len, reverse=True):
        out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
    return out


def _extract_params(task: str, skill: Skill) -> dict:
    """尽力做槽位填充。参数缺失时让技能抛 SkillMiss——
    这是正确结果：宁可把这一轮交给模型，也不要用编造的参数跑配方。"""
    if not skill.params:
        return {}
    out: dict[str, str] = {}
    triggers = list(skill.trigger_words())
    for pattern in _PARAM_PATTERNS:
        m = pattern.search(task)
        if m:
            value = _strip_triggers(m.group(1), triggers).strip(" 。,.，、")
            if value:
                out[skill.params[0]] = value[:40]
                break
    return out
