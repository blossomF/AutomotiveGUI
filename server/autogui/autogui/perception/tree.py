"""Compress a uiautomator tree into addressable, stateful UI candidates."""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
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
    search_text: str = ""  # full XML text for ranking; never sent to the model
    clickable_via_parent: bool = False

    def as_dict(self) -> dict:
        return {
            "ref": self.ref, "label": self.label, "xy": (self.cx, self.cy),
            "bounds": self.bounds, "clickable": self.clickable, "rid": self.rid, "class": self.cls,
            "text": self.text, "content_desc": self.desc, "scrollable": self.scrollable,
            "enabled": self.enabled, "checkable": self.checkable, "checked": self.checked,
            "selected": self.selected, "long_clickable": self.long_clickable,
            "editable": self.editable, "focused": self.focused,
            "scroll_axis": self.scroll_axis,
            "clickable_via_parent": self.clickable_via_parent,
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
    if tail in ("ScrollView", "RecyclerView", "ListView", "NumberPicker", "WheelView"):
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


def focus_terms_from_task(task: str, entities: dict | None = None,
                          skill_params: dict | None = None) -> list[str]:
    """Extract ranking hints, never an action or a claim that a result exists."""
    values = [str(v) for v in (skill_params or {}).values() if v]
    for key, value in (entities or {}).items():
        if any(marker in str(key) for marker in ("目标", "歌曲", "歌名", "关键词", "商品", "联系人", "时间")):
            values.append(str(value))
    values += re.findall(r"[《「“\"]([^》」”\"]{2,40})[》」”\"]", task)
    verbs = list(re.finditer(r"播放|搜索|查找|查看|打开|选择|设置", task))
    if verbs:
        tail = task[verbs[-1].end():]
        tail = re.split(r"[\s，。；,;！？!?]|(?:然后|并且|并|把|加入|点击)", tail, maxsplit=1)[0]
        values.append(tail.strip("《》「」“”\" App应用"))
    out: list[str] = []
    for value in values:
        value = value.strip()
        if 2 <= len(value) <= 40 and value.casefold() not in {v.casefold() for v in out}:
            out.append(value)
    return out[:6]


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def target_score(c: Candidate, focus_terms: list[str] | None) -> int:
    """Exact result titles outrank query fields and incidental prose mentions."""
    if not focus_terms or c.editable:
        return 0
    label = _normalized(c.label)
    source = _normalized(c.search_text or c.label)
    best = 0
    for term in focus_terms:
        query = _normalized(term)
        if len(query) < 2:
            continue
        if label == query:
            best = max(best, 100)
        elif label.startswith(query + "(") or label.startswith(query + "（"):
            best = max(best, 90)
        elif label.startswith(query):
            best = max(best, 70)
        elif query in label:
            best = max(best, 35)
        elif query in source:
            best = max(best, 15)
        elif len(label) >= 2 and label in query:
            best = max(best, 20)
    return best


def _estimated_tokens(value: str) -> int:
    cjk = len(re.findall(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]", value))
    return cjk + (len(value) - cjk + 3) // 4


def _clickable_row_for_labels(xml: str, screen: tuple[int, int]) -> list[bool]:
    """Identify labels inside bounded clickable items, excluding page wrappers.

    The existing flat ``nodes()`` parser remains the source for candidates.
    If hierarchy parsing fails or its order differs, no label is promoted.
    """
    sw, sh = screen
    found: list[bool] = []

    def walk(element: ET.Element, ancestors: tuple[ET.Element, ...]) -> None:
        if element.tag == "node":
            own = element.attrib
            bounds = node_bounds(f'<node bounds="{own.get("bounds", "")}" />')
            eligible = False
            if bounds and own.get("clickable") != "true" and own.get("enabled") != "false":
                for parent in reversed(ancestors):
                    attrs = parent.attrib
                    if attrs.get("clickable") != "true" or attrs.get("enabled") == "false":
                        continue
                    parent_bounds = node_bounds(f'<node bounds="{attrs.get("bounds", "")}" />')
                    if not parent_bounds:
                        continue
                    px1, py1, px2, py2 = parent_bounds
                    x1, y1, x2, y2 = bounds
                    width, height = px2 - px1, py2 - py1
                    if (px1 <= x1 < x2 <= px2 and py1 <= y1 < y2 <= py2
                            and height <= sh * 0.25 and width * height <= sw * sh * 0.20):
                        eligible = True
                        break
            found.append(eligible)
            ancestors += (element,)
        for child in element:
            walk(child, ancestors)

    try:
        walk(ET.fromstring(xml), ())
    except ET.ParseError:
        return []
    return found if len(found) == len(nodes(xml)) else []


def select_candidates(cands: list[Candidate], limit: int, screen_height: int,
                      focus_terms: list[str] | None = None) -> list[Candidate]:
    """Keep task evidence, its nearby controls and navigational anchors, then spread the rest."""
    if limit <= 0:
        return []
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

    chosen: list[Candidate] = []
    picked: set[tuple[int, int, int, int]] = set()

    def add(c: Candidate) -> None:
        if len(chosen) < limit and c.bounds not in picked:
            chosen.append(c)
            picked.add(c.bounds)

    # Input fields are frequent next actions and may have no visible text.
    # Reserve slots before task-title matches can consume the small limit.
    inputs = sorted((c for c in cands if c.editable),
                    key=lambda c: (not c.focused, not c.enabled, *_reading_order(c)))
    for c in inputs[:min(4, max(1, limit // 8))]:
        add(c)

    targets = sorted((c for c in cands if target_score(c, focus_terms) >= 20),
                     key=lambda c: (-target_score(c, focus_terms),
                                    c.editable, c.cy, c.cx))
    for c in targets[:max(1, min(8, limit // 4))]:
        add(c)

    # Keep the controls and metadata on the same visible result row. XML often
    # marks the song title static and only its adjacent buttons actionable.
    for target in targets[:3]:
        companions = [c for c in cands if c.bounds not in picked
                      and abs(c.cy - target.cy) <= max(70, screen_height // 24)
                      and (is_actionable(c) or bool(c.text))]
        companions.sort(key=lambda c: (abs(c.cy - target.cy),
                                       -candidate_priority(c), c.cx))
        nearby_controls = [c for c in companions if is_actionable(c)]
        nearby_text = [c for c in companions if not is_actionable(c) and c.text
                       and not re.fullmatch(r"[\d.,]+[kKmMwW+]*", c.text)]
        for c in nearby_controls[:2] + nearby_text[:1]:
            add(c)

    anchor_words = ("返回", "搜索", "确定", "确认", "同意", "关闭", "跳过", "取消", "下一步")
    anchors = [c for c in cands if c.bounds not in picked and
               (c.focused or c.editable or c.selected or
                (is_actionable(c) and any(word in c.label for word in anchor_words)))]
    anchors.sort(key=lambda c: (-candidate_priority(c), *_reading_order(c)))
    for c in anchors[:min(8, max(1, limit // 5))]:
        add(c)

    remaining = limit - len(chosen)
    actionable = [c for c in cands if c.bounds not in picked and is_actionable(c)]
    passive = [c for c in cands if c.bounds not in picked and not is_actionable(c)]
    for c in take_spread(actionable, min(len(actionable), (remaining * 3 + 3) // 4)):
        add(c)
    for c in take_spread(passive, limit - len(chosen)):
        add(c)
    for c in take_spread(actionable, limit - len(chosen)):
        add(c)
    return sorted(chosen, key=_reading_order)


def extract(
    xml: str,
    *,
    screen: tuple[int, int] | None = None,
    limit: int = 40,
    include_non_clickable: bool = True,
    stats: dict[str, int] | None = None,
    focus_terms: list[str] | None = None,
    token_budget: int | None = None,
) -> list[Candidate]:
    """把 uiautomator dump 解析为最多 ``limit`` 个可寻址候选。"""
    if not xml:
        if stats is not None:
            stats.update(eligible=0, unique=0, selected=0, omitted_actionable=0)
        return []
    sw, sh = screen or (1920, 1080)
    parent_rows = _clickable_row_for_labels(xml, (sw, sh))

    raw: list[Candidate] = []
    for node_index, n in enumerate(nodes(xml)):
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
        clickable_via_parent = bool(parent_rows and parent_rows[node_index] and not clickable)
        scrollable = node_attr(n, "scrollable") == "true"
        cls = node_class(n)
        tail = cls.rsplit(".", 1)[-1]
        editable = tail in ("EditText", "AutoCompleteTextView")
        checkable = node_attr(n, "checkable") == "true"
        long_clickable = node_attr(n, "long-clickable") == "true"

        label = text or desc or rid or (tail if scrollable or editable or checkable or tail in ("SeekBar", "Slider") else "")
        if not label:
            continue
        # Tap at the visible label, inside its clickable row, rather than at
        # the row centre where an independent trailing button may sit.
        if clickable_via_parent and (text or desc) and not (scrollable or editable):
            clickable = True
        else:
            clickable_via_parent = False

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
            search_text=f"{node_text(n)} {node_desc(n)}",
            clickable_via_parent=clickable_via_parent,
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
        best.clickable_via_parent |= other.clickable_via_parent
        best.scrollable |= other.scrollable
        best.scroll_axis = best.scroll_axis or other.scroll_axis
        best.checkable |= other.checkable
        best.checked |= other.checked
        best.selected |= other.selected
        best.long_clickable |= other.long_clickable
        best.editable |= other.editable
        best.focused |= other.focused
        best.enabled &= other.enabled
        if len(other.search_text) > len(best.search_text):
            best.search_text = other.search_text
        by_bounds[c.bounds] = best

    unique = list(by_bounds.values())
    out = select_candidates(unique, max(0, limit), sh, focus_terms)
    before_budget = len(out)
    if token_budget and token_budget > 0:
        while len(out) > 6 and _estimated_tokens(render_candidates(out)) > token_budget:
            out = select_candidates(unique, max(6, len(out) - 5), sh, focus_terms)
    if stats is not None:
        selected_bounds = {c.bounds for c in out}
        stats.update(
            eligible=len(raw), unique=len(by_bounds), selected=len(out),
            omitted_actionable=sum(
                c.bounds not in selected_bounds
                for c in by_bounds.values()
                if is_actionable(c)
            ),
            omitted_passive=sum(c.bounds not in selected_bounds for c in unique
                                if not is_actionable(c)),
            target_in_raw=sum(target_score(c, focus_terms) >= 20 for c in unique),
            target_selected=sum(target_score(c, focus_terms) >= 20 for c in out),
            budget_reduced=before_budget - len(out),
            candidate_tokens=_estimated_tokens(render_candidates(out)),
        )
        stats["target_lost"] = int(stats["target_in_raw"] > 0 and stats["target_selected"] == 0)
    for i, c in enumerate(out, start=1):
        c.ref = i
    return out


def candidates_from_xml(xml: str, limit: int = 40, screen: tuple[int, int] | None = None,
                        stats: dict[str, int] | None = None,
                        focus_terms: list[str] | None = None,
                        token_budget: int | None = None) -> list[Candidate]:
    return extract(xml, limit=limit, screen=screen, stats=stats,
                   focus_terms=focus_terms, token_budget=token_budget)


def render_candidates(cands: list[Candidate]) -> str:
    """Render short lines with action/state attributes needed for decisions."""
    lines = []
    for c in cands:
        flags = []
        if c.clickable:
            flags.append("可点·父行" if c.clickable_via_parent else "可点")
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
        custom_wheel = c.cls.rsplit(".", 1)[-1] == "WheelView"
        wheel_hint = " [自定义滚轮·读关联字段或截图；不用 ROLL_TO]" if custom_wheel else (" [滚轮]" if wheel else "")
        lines.append(f"#{c.ref} {c.label} [{c.cx},{c.cy}] ({','.join(flags)}){wheel_hint}{detail}{ident}{bounds}")
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
