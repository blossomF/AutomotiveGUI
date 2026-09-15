"""Task -> skill routing.

Two entry points, used in that order:

  1. ``match_keywords`` — deterministic, zero-VLM. Each recipe declares trigger
     words in its YAML; a task containing them plus an extractable parameter runs
     the recipe without ever calling the model. This is where the performance and
     accuracy points come from: a hit is ~1s/step and cannot mislocate a widget.
  2. The model itself may answer ``{"type":"SKILL","name":...,"params":{...}}`` on
     any decide step, having seen ``SkillBook.index()`` in the permanent prompt
     block. That covers phrasings the keyword list did not anticipate.

Both are advisory: a miss raises ``SkillMiss`` inside the executor and the graph
falls through to the generic tree/vlm route.
"""

from __future__ import annotations

import re

from autogui.skills.base import Skill, SkillBook


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def match_keywords(task: str, book: SkillBook) -> tuple[Skill, dict] | None:
    """First skill with a declared trigger present in the task.

    Quoted spans and the text after a colon are lifted out as the first declared
    parameter, so "播放《七里香》" yields ``{"song": "七里香"}`` for a skill with
    ``params: [song]``.
    """
    t = _norm(task)
    if not t:
        return None
    for skill in book.by_id.values():
        if not any(_norm(word) in t for word in skill.trigger_words() if word):
            continue
        return skill, _extract_params(task, skill)
    return None


_PARAM_PATTERNS = (
    re.compile(r"[《“\"'【\[](.+?)[》”\"'】\]]"),          # 括号内引用
    re.compile(r"[=:：]\s*(.+)$"),                          # key: value
    re.compile(r"(?:搜索|查找|找|播放|播放一下|来一首)\s*(.+)$"),  # verb + object
)


def _extract_params(task: str, skill: Skill) -> dict:
    """Best-effort slot filling. Absent params leave the skill to raise SkillMiss,
    which is the correct outcome — better to hand the turn to the model than to run
    a recipe with a fabricated argument."""
    if not skill.params:
        return {}
    out: dict[str, str] = {}
    for pattern in _PARAM_PATTERNS:
        m = pattern.search(task)
        if m:
            value = m.group(1).strip(" 。,.，、")
            if value:
                out[skill.params[0]] = value[:40]
                break
    return out
