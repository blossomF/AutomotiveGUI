"""Compress a uiautomator tree into addressable, stateful UI candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from autogui.bridge.adb import node_attr, node_bounds, node_class, node_clickable, node_desc, node_rid, node_text, nodes

_MAX_LABEL = 48
# 达到屏幕这个宽度比例（占屏比例）和这个高度的节点是容器，不是目标。
_CONTAINER_W = 0.72
_CONTAINER_H = 0.55

# 这些 resource-id 尾部是结构性噪声，即使它们带有值也一样。
# 框架 id 大小写不一致（"statusBarBackground" vs "statusbar_background"），
# 所以这里忽略大小写、针对*尾部*而不是整个 id 匹配。
_RID_NOISE_TAIL = re.compile(
    r"(?:content|rootview|background|mask|placeholder|spacer|statusbar|navbar|"
    r"navigationbar|decor|overlay|window[_-]?content)$",
    re.I,
)


@dataclass
class Candidate:
    ref: int
    label: str
    cx: int
    cy: int
    bounds: tuple[int, int, int, int]
    clickable: bool
    rid: str
    cls: str
    text: str = ""
    desc: str = ""
    scrollable: bool = False
    enabled: bool = True
    checkable: bool = False
    checked: bool = False
    selected: bool = False
    long_clickable: bool = False
    editable: bool = False
    focused: bool = False
    scroll_axis: str = ""

    def as_dict(self) -> dict:
        return {
            "ref": self.ref, "label": self.label, "xy": (self.cx, self.cy),
            "bounds": self.bounds, "clickable": self.clickable, "rid": self.rid, "class": self.cls,
            "text": self.text, "content_desc": self.desc, "scrollable": self.scrollable,
            "enabled": self.enabled, "checkable": self.checkable, "checked": self.checked,
            "selected": self.selected, "long_clickable": self.long_clickable,
            "editable": self.editable, "focused": self.focused,
            "scroll_axis": self.scroll_axis,
        }


def _clip(s: str, n: int = _MAX_LABEL) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _rid_tail(rid: str) -> str:
    """``com.foo:id/search_box`` -> ``search_box``。裸 id 或纯结构性 id 会被丢弃。"""
    if not rid or ":" not in rid:
        return ""
    tail = rid.rsplit("/", 1)[-1]
    return "" if _RID_NOISE_TAIL.search(tail) else tail


def _area(b: tuple[int, int, int, int]) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _scroll_axis(cls: str, bounds: tuple[int, int, int, int]) -> str:
    tail = cls.rsplit(".", 1)[-1]
    if tail == "HorizontalScrollView" or "ViewPager" in tail:
        return "horizontal"
    if tail in ("ScrollView", "RecyclerView", "ListView", "NumberPicker"):
        return "vertical"
    return "horizontal" if bounds[2] - bounds[0] >= bounds[3] - bounds[1] else "vertical"


def candidate_priority(c: Candidate) -> int:
    """Keep actionable nodes before plain labels when the list must be cut."""
    score = (100 if c.scrollable else 0) + (80 if c.editable else 0)
    score += (70 if c.clickable else 0) + (60 if c.checkable else 0)
    score += (40 if c.long_clickable else 0)
    score += (20 if c.text else 0) + (15 if c.desc else 0) + (5 if c.rid else 0)
    if not c.enabled:
        score -= 40
    return score


def is_actionable(c: Candidate) -> bool:
    return c.clickable or c.scrollable or c.editable or c.checkable or c.long_clickable


def _reading_order(c: Candidate) -> tuple[int, int]:
    return c.cy // 40, c.cx


def select_candidates(cands: list[Candidate], limit: int, screen_height: int) -> list[Candidate]:
    """Rank after dedup; reserve coverage across the whole screen."""
    if len(cands) <= limit:
        return sorted(cands, key=_reading_order)

    def take_spread(pool: list[Candidate], slots: int) -> list[Candidate]:
        bands: list[list[Candidate]] = [[], [], [], []]
        for c in pool:
            band = min(3, max(0, c.cy * 4 // max(1, screen_height)))
            bands[band].append(c)
        for band in bands:
            band.sort(key=lambda c: (-candidate_priority(c), *_reading_order(c)))
        taken: list[Candidate] = []
        while len(taken) < slots and any(bands):
            for band in bands:
                if band and len(taken) < slots:
                    taken.append(band.pop(0))
        return taken

    actionable = [c for c in cands if is_actionable(c)]
    passive = [c for c in cands if not is_actionable(c)]
    chosen = take_spread(actionable, limit)
    if len(chosen) < limit:
        chosen.extend(take_spread(passive, limit - len(chosen)))
    return sorted(chosen, key=_reading_order)


def extract(
    xml: str,
    *,
    screen: tuple[int, int] | None = None,
    limit: int = 40,
    include_non_clickable: bool = True,
    stats: dict[str, int] | None = None,
) -> list[Candidate]:
    """把 uiautomator dump 解析为最多 ``limit`` 个可寻址候选。"""
    if not xml:
        if stats is not None:
            stats.update(eligible=0, unique=0, selected=0, omitted_actionable=0)
        return []
    sw, sh = screen or (1920, 1080)

    raw: list[Candidate] = []
    for n in nodes(xml):
        b = node_bounds(n)
        if not b:
            continue
        x1, y1, x2, y2 = b
        if x2 <= x1 or y2 <= y1:
            continue
        # 忽略屏幕外的残留（多屏 dump 会包含另一块面板）。
        if x2 <= 0 or y2 <= 0 or x1 >= sw or y1 >= sh:
            continue
        # Partly visible controls remain usable, but their action coordinates
        # must refer to the visible screen, not the off-screen XML rectangle.
        b = (max(0, x1), max(0, y1), min(sw, x2), min(sh, y2))
        x1, y1, x2, y2 = b

        text = _clip(node_text(n))
        desc = _clip(node_desc(n))
        rid = _rid_tail(node_rid(n))
        clickable = node_clickable(n)
        scrollable = node_attr(n, "scrollable") == "true"
        cls = node_class(n)
        tail = cls.rsplit(".", 1)[-1]
        editable = tail in ("EditText", "AutoCompleteTextView")
        checkable = node_attr(n, "checkable") == "true"
        long_clickable = node_attr(n, "long-clickable") == "true"

        label = text or desc or rid or (tail if scrollable or editable or checkable or tail in ("SeekBar", "Slider") else "")
        if not label:
            continue

        w, h = x2 - x1, y2 - y1
        if w >= sw * _CONTAINER_W and h >= sh * _CONTAINER_H and not (
            scrollable or clickable or editable or checkable or long_clickable
        ):
            continue  # noninteractive full-screen wrapper

        raw.append(Candidate(
            ref=0, label=label, cx=(x1 + x2) // 2, cy=(y1 + y2) // 2,
            bounds=b, clickable=clickable, rid=rid, cls=cls, text=text, desc=desc,
            scrollable=scrollable, enabled=node_attr(n, "enabled") != "false",
            checkable=checkable,
            checked=node_attr(n, "checked") == "true",
            selected=node_attr(n, "selected") == "true",
            long_clickable=long_clickable,
            editable=editable, focused=node_attr(n, "focused") == "true",
            scroll_axis=_scroll_axis(cls, b) if scrollable else "",
        ))

    if not raw:
        if stats is not None:
            stats.update(eligible=0, unique=0, selected=0, omitted_actionable=0)
        return []

    if not include_non_clickable:
        raw = [c for c in raw if c.clickable]
    # Same bounds usually mean a wrapper and its actionable child. Preserve
    # the child's own flags/class while inheriting a useful visible label.
    by_bounds: dict[tuple[int, int, int, int], Candidate] = {}
    for c in raw:
        old = by_bounds.get(c.bounds)
        if old is None:
            by_bounds[c.bounds] = c
            continue
        best, other = (c, old) if candidate_priority(c) > candidate_priority(old) else (old, c)
        if not best.text and other.text:
            best.text = other.text
            best.label = other.text
        if not best.desc and other.desc:
            best.desc = other.desc
            if not best.text:
                best.label = other.desc
        # Wrapper and child can carry different parts of one control's state.
        best.clickable |= other.clickable
        best.scrollable |= other.scrollable
        best.scroll_axis = best.scroll_axis or other.scroll_axis
        best.checkable |= other.checkable
        best.checked |= other.checked
        best.selected |= other.selected
        best.long_clickable |= other.long_clickable
        best.editable |= other.editable
        best.focused |= other.focused
        best.enabled &= other.enabled
        by_bounds[c.bounds] = best

    out = select_candidates(list(by_bounds.values()), max(0, limit), sh)
    if stats is not None:
        selected_bounds = {c.bounds for c in out}
        stats.update(
            eligible=len(raw), unique=len(by_bounds), selected=len(out),
            omitted_actionable=sum(
                c.bounds not in selected_bounds
                for c in by_bounds.values()
                if is_actionable(c)
            ),
        )
    for i, c in enumerate(out, start=1):
        c.ref = i
    return out


def candidates_from_xml(xml: str, limit: int = 40, screen: tuple[int, int] | None = None,
                        stats: dict[str, int] | None = None) -> list[Candidate]:
    return extract(xml, limit=limit, screen=screen, stats=stats)


def render_candidates(cands: list[Candidate]) -> str:
    """Render short lines with action/state attributes needed for decisions."""
    lines = []
    for c in cands:
        flags = []
        if c.clickable:
            flags.append("可点")
        if c.long_clickable:
            flags.append("长按")
        if c.editable:
            flags.append("输入")
        if c.cls.rsplit(".", 1)[-1] in ("SeekBar", "Slider"):
            flags.append("滑块")
        wheel = is_value_wheel(c)
        if c.scrollable or wheel:
            flags.append("横滚" if (c.scroll_axis or _scroll_axis(c.cls, c.bounds)) == "horizontal" else "纵滚")
        if c.checkable:
            flags.append("已勾" if c.checked else "未勾")
        if c.selected:
            flags.append("选中")
        if c.focused:
            flags.append("焦点")
        if not c.enabled:
            flags.append("禁用")
        if not flags:
            flags.append("静态")
        detail = "" if not c.desc or c.desc == c.label else f" 描述:{c.desc}"
        ident = f" id:{c.rid}" if c.rid and c.rid != c.label and (c.clickable or c.scrollable or c.editable) else ""
        bounds = f" 范围:{c.bounds}" if c.scrollable or c.cls.rsplit(".", 1)[-1] in ("SeekBar", "Slider") else ""
        lines.append(f"#{c.ref} {c.label} [{c.cx},{c.cy}] ({','.join(flags)}){' [滚轮]' if wheel else ''}{detail}{ident}{bounds}")
    return "\n".join(lines)


def is_value_wheel(candidate: Candidate) -> bool:
    """Only mark wheels whose selected value can be read from the UI tree."""
    return candidate.cls.rsplit(".", 1)[-1] == "NumberPicker" and any(
        re.fullmatch(r"[+-]?\d+\s*[^\d]*", value.strip())
        for value in (candidate.desc, candidate.text, candidate.label) if value
    )


def by_ref(cands: list[Candidate], ref: int) -> Candidate | None:
    """解析模型返回的 ``node_ref``。越界返回 ``None``，让调用方
    降级 tree->vlm 而不是抛异常（技术方案评估.md §5.2）。"""
    for c in cands:
        if c.ref == ref:
            return c
    return None


def scroll_targets(
    xml: str, *, screen: tuple[int, int] | None = None
) -> dict[str, list[tuple[tuple[int, int, int, int], str]]]:
    """收集屏内 ``scrollable=true`` 容器，按滚动轴向分组返回。

    SCROLL 手势必须落在可滚动容器内部：起/终点一旦滑到相邻固定栏或外层
    竖向列表，手势会被别的视图吞掉（实测 QQ音乐推荐页用屏幕中心全屏横滑
    翻不动顶部 ``HorizontalScrollView`` 横条，而容器内部短滑正常）。

    轴向判定：类名明确为 ``HorizontalScrollView`` / 含 ``ViewPager`` 判横向；
    ``ScrollView`` / ``RecyclerView`` / ``ListView`` / ``NumberPicker`` 判竖向
    （车机主列表 RV 虽宽大于高但仍是竖向）；其余按宽高比兜底。
    部分出屏的容器裁到当前屏幕内，与候选坐标保持一致。
    返回 ``{"horizontal": [(bounds, class_tail)...], "vertical": [...]}``。
    """
    groups: dict[str, list[tuple[tuple[int, int, int, int], str]]] = {
        "horizontal": [], "vertical": []}
    if not xml:
        return groups
    sw, sh = screen or (1920, 1080)
    for n in nodes(xml):
        if node_attr(n, "scrollable") != "true":
            continue
        b = node_bounds(n)
        if not b:
            continue
        x1, y1, x2, y2 = b
        cw, ch = x2 - x1, y2 - y1
        if cw <= 0 or ch <= 0:
            continue
        if x2 <= 0 or y2 <= 0 or x1 >= sw or y1 >= sh:
            continue
        b = (max(0, x1), max(0, y1), min(sw, x2), min(sh, y2))
        cls = node_class(n) or ""
        groups[_scroll_axis(cls, b)].append((b, cls.rsplit(".", 1)[-1]))
    return groups


def screen_signature(xml: str) -> str:
    """树中可见标签和状态的顺序敏感摘要。

    供 ``perception/capture.py`` 构建无进展指纹使用。仅用文本会漏掉
    纯视觉变化（视频开始播放），所以 capture 还会折叠进一个降采样
    图像哈希。
    """
    parts = []
    for n in nodes(xml):
        t = node_text(n) or node_desc(n) or _rid_tail(node_rid(n))
        if t:
            state = "".join(
                f"|{attr}={node_attr(n, attr)}"
                for attr in ("checked", "selected", "enabled")
                if node_attr(n, attr) in ("true", "false")
            )
            parts.append(re.sub(r"\s+", " ", t).strip() + state)
    return "|".join(parts)
