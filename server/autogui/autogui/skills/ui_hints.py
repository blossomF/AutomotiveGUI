"""按当前 XML 页面加载 App 局部知识；不选择 Skill，也不执行设备动作。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from autogui.bridge.adb import node_bounds, node_rid, nodes


@dataclass(frozen=True)
class PageHint:
    app: str
    package: str
    name: str
    markers: tuple[str, ...]
    text: str
    configured_app: str = ""
    bounds_marker: str = ""


class PageHintBook:
    def __init__(self, root: Path):
        self.hints: list[PageHint] = []
        for path in sorted(root.glob("*/ui_hints.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            package = str(doc.get("package") or "").strip()
            if not package:
                continue
            for page in doc.get("pages") or []:
                markers = tuple(str(m) for m in page.get("markers") or [])
                hint = str(page.get("hint") or "").strip()
                if markers and hint:
                    self.hints.append(PageHint(
                        app=str(doc.get("app") or path.parent.name),
                        package=package,
                        name=str(page.get("name") or "page"),
                        markers=markers,
                        text=hint,
                        configured_app=str(doc.get("configured_app") or ""),
                        bounds_marker=str(page.get("bounds_marker") or ""),
                    ))

    def match(self, xml: str, configured_apps: list | None = None) -> list[str]:
        """必须由当前包名下的全部页面标记佐证，避免跨 App 套用经验。"""
        if not xml:
            return []
        by_id = {node_rid(node): node for node in nodes(xml)}
        packages = {app.name: app.package for app in (configured_apps or [])}
        result = []
        for hint in self.hints:
            if hint.configured_app and packages.get(hint.configured_app) != hint.package:
                continue
            if all(f"{hint.package}:id/{marker}" in by_id for marker in hint.markers):
                detail = hint.text
                if hint.bounds_marker:
                    node = by_id.get(f"{hint.package}:id/{hint.bounds_marker}")
                    bounds = node_bounds(node) if node else None
                    if bounds is None:
                        continue
                    x0, y0, x1, y1 = bounds
                    detail += f" 当前轨道 bounds=[{x0},{y0}][{x1},{y1}]。"
                result.append(f"{hint.app} / {hint.name}：{detail}")
        return result
