"""UI 树 -> 紧凑编号候选列表（B 路径的决策面）。

本模块是 Context 得分大多取决于成败的地方。这些 App 的原始
``uiautomator dump`` 有 40-120 KB；把它直接喂给模型会在第一步就
击穿 token 预算。因此我们：

  1. 只保留带人类可见标签（text / content-desc / 有意义的
     resource-id 尾部）的节点；
  2. 丢弃巨大的容器节点（包裹整个屏幕的 ``android.widget.FrameLayout``
     不是能点击的东西）；
  3. 优先*可点击*节点，重复项中保留最紧凑的框；
  4. 按阅读顺序截断列表；
  5. 每个候选渲染成一行短文本。

模型用整数 ``node_ref`` 回答，从不复制坐标，从而彻底消除
非法坐标这一类错误。

``screen_signature`` 是 ``core/guard.py`` 背后的廉价变化检测器，
也是 verify 节点“大多数时候 0 次 VLM”行为的依据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autogui.bridge.adb import node_bounds, node_class, node_clickable, node_desc, node_rid, node_text, nodes

_MAX_LABEL = 28
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

    def as_dict(self) -> dict:
        return {
            "ref": self.ref, "label": self.label, "xy": (self.cx, self.cy),
            "bounds": self.bounds, "clickable": self.clickable, "rid": self.rid, "class": self.cls,
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


def extract(
    xml: str,
    *,
    screen: tuple[int, int] | None = None,
    limit: int = 40,
    include_non_clickable: bool = True,
) -> list[Candidate]:
    """把 uiautomator dump 解析为最多 ``limit`` 个可寻址候选。"""
    if not xml:
        return []
    sw, sh = screen or (1920, 1080)

    raw: list[tuple] = []
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

        text = _clip(node_text(n))
        desc = _clip(node_desc(n))
        rid = _rid_tail(node_rid(n))
        clickable = node_clickable(n)

        label = text or desc or rid
        if not label:
            continue

        w, h = x2 - x1, y2 - y1
        if w >= sw * _CONTAINER_W and h >= sh * _CONTAINER_H:
            continue  # 全屏容器

        raw.append((clickable, (x1 + x2) // 2, (y1 + y2) // 2, b, label, clickable, rid, node_class(n)))

    if not raw:
        return []

    # 按阅读顺序排序，然后合并重复中心点，长标签胜出
    # （标签为"播放"的节点胜过标签为"btn"的外层包装）。
    raw.sort(key=lambda t: (t[2] // 40, t[1]))
    if not include_non_clickable:
        raw = [t for t in raw if t[0]]
    if len(raw) > limit:
        clickable_first = [t for t in raw if t[0]]
        pool = clickable_first[:limit] if len(clickable_first) >= limit else raw[:limit]
    else:
        pool = raw

    seen: dict[tuple[int, int], int] = {}
    out: list[Candidate] = []
    for _, cx, cy, b, label, clickable, rid, cls in pool:
        key = (cx, cy)
        if key in seen:
            i = seen[key]
            if len(label) > len(out[i].label):
                out[i].label = label
            continue
        seen[key] = len(out)
        out.append(Candidate(ref=len(out) + 1, label=label, cx=cx, cy=cy, bounds=b,
                             clickable=clickable, rid=rid, cls=cls))
        if len(out) >= limit:
            break

    # 去重重排之后 ref 必须重新连续编号。
    for i, c in enumerate(out, start=1):
        c.ref = i
    return out


def candidates_from_xml(xml: str, limit: int = 40, screen: tuple[int, int] | None = None) -> list[Candidate]:
    return extract(xml, limit=limit, screen=screen)


def render_candidates(cands: list[Candidate]) -> str:
    """每个候选一行：``#7 搜索 [960,86]``。可点击的会标记。

    坐标为模型的空间推理而打印，但模型*不会*回传坐标——只回传 ``#ref``。
    """
    lines = []
    for c in cands:
        mark = "" if c.clickable else " (n)"
        lines.append(f"#{c.ref} {c.label} [{c.cx},{c.cy}]{mark}")
    return "\n".join(lines)


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
    ``ScrollView`` / ``RecyclerView`` / ``ListView`` 判竖向（车机主列表 RV
    虽宽大于高但仍是竖向，故不能只看宽高比）；其余才按宽高比兜底。
    返回 ``{"horizontal": [(bounds, class_tail)...], "vertical": [...]}``。
    """
    groups: dict[str, list[tuple[tuple[int, int, int, int], str]]] = {
        "horizontal": [], "vertical": []}
    if not xml:
        return groups
    sw, sh = screen or (1920, 1080)
    for n in nodes(xml):
        if 'scrollable="true"' not in n:
            continue
        b = node_bounds(n)
        if not b:
            continue
        x1, y1, x2, y2 = b
        cw, ch = x2 - x1, y2 - y1
        if cw <= 0 or ch <= 0:
            continue
        # 越界容器（多屏 dump 的另一块面板）会把向量带出屏，直接丢弃。
        if x1 < 0 or y1 < 0 or x2 > sw or y2 > sh:
            continue
        tail = (node_class(n) or "").rsplit(".", 1)[-1]
        if tail == "HorizontalScrollView" or "ViewPager" in tail:
            axis = "horizontal"
        elif tail in ("ScrollView", "RecyclerView", "ListView"):
            axis = "vertical"
        else:
            axis = "horizontal" if cw >= ch else "vertical"
        groups[axis].append((b, tail))
    return groups


def screen_signature(xml: str) -> str:
    """树中所有可见标签的顺序敏感摘要。

    供 ``perception/capture.py`` 构建无进展指纹使用。仅用文本会漏掉
    纯视觉变化（视频开始播放），所以 capture 还会折叠进一个降采样
    图像哈希。
    """
    parts = []
    for n in nodes(xml):
        t = node_text(n) or node_desc(n)
        if t:
            parts.append(re.sub(r"\s+", " ", t).strip())
    return "|".join(parts)
