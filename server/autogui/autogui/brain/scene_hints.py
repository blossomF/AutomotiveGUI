"""从本次 XML 的结构识别场景；只提供指导，不执行设备动作。"""

import re
import xml.etree.ElementTree as ET

from autogui.brain import prompts


def match_scene_hints(xml: str) -> list[str]:
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError):
        return []

    def actionable(node):
        return (node.get("clickable") == "true"
                and node.get("enabled", "true") == "true"
                and node.get("visible-to-user", "true") == "true")

    hints = []
    # 结构标记和同一容器内的按钮必须同时存在，普通页面的词语不触发。
    for node in root.iter():
        cls = node.get("class", "").rsplit(".", 1)[-1]
        rid = node.get("resource-id", "").rsplit("/", 1)[-1]
        if (cls in {"Dialog", "AlertDialog"} or rid in {"parentPanel", "dialog_container"}):
            if any(actionable(child) for child in node.iter()):
                hints.append(prompts.render("popup_guide"))
                break
    for node in root.iter():
        label = node.get("text", "") + " " + node.get("content-desc", "")
        if actionable(node) and "跳过" in label and re.search(r"\d+\s*(?:秒|s\b)", label, re.I):
            hints.append(prompts.render("countdown_ad_guide"))
            break
    return hints
