"""UI tree -> compact numbered candidate list (the path-B decision surface).

This module is where the Context score is mostly won or lost. A raw
``uiautomator dump`` on these apps runs 40-120 KB; feeding that to the model would
blow the token budget on step one. Instead we:

  1. keep only nodes that carry a human-visible label (text / content-desc / a
     meaningful resource-id tail);
  2. drop giant container nodes (``android.widget.FrameLayout`` wrapping the whole
     screen is not a thing you click);
  3. prefer *clickable* nodes, and among duplicates keep the tightest box;
  4. cap the list, in reading order;
  5. render one short line per candidate.

The model answers with an integer ``node_ref``. It never copies a coordinate, which
eliminates the malformed-bounds class of errors entirely.

``screen_signature`` is the cheap change-detector behind ``core/guard.py`` and the
verify node's "0 VLM most of the time" behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autogui.bridge.adb import node_bounds, node_center, node_class, node_clickable, node_desc, node_rid, node_text, nodes

_MAX_LABEL = 28
# A node this wide (fraction of screen) and this tall is a container, not a target.
_CONTAINER_W = 0.72
_CONTAINER_H = 0.55

# resource-id tails that are structural noise even though they carry a value.
# Framework ids are inconsistently cased ("statusBarBackground" vs "statusbar_background"),
# so this matches case-insensitively and against the *tail*, not the whole id.
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
    """``com.foo:id/search_box`` -> ``search_box``. Bare or purely structural ids drop out."""
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
    """Parse a uiautomator dump into at most ``limit`` addressable candidates."""
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
        # Ignore off-screen leftovers (multi-display dumps include the other panel).
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
            continue  # full-screen container

        raw.append((clickable, (x1 + x2) // 2, (y1 + y2) // 2, b, label, clickable, rid, node_class(n)))

    if not raw:
        return []

    # Sort into reading order, then collapse duplicate centres, longest-label wins
    # (a node saying "播放" beats its wrapper saying "btn").
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

    # Refs must be contiguous after dedup reordering.
    for i, c in enumerate(out, start=1):
        c.ref = i
    return out


def candidates_from_xml(xml: str, limit: int = 40, screen: tuple[int, int] | None = None) -> list[Candidate]:
    return extract(xml, limit=limit, screen=screen)


def render_candidates(cands: list[Candidate]) -> str:
    """One line per candidate: ``#7 搜索 [960,86]``. Clickable ones are flagged.

    Coordinates are printed for the model's spatial reasoning but are *not* echoed
    back by it — only the ``#ref`` is.
    """
    lines = []
    for c in cands:
        mark = "" if c.clickable else " (n)"
        lines.append(f"#{c.ref} {c.label} [{c.cx},{c.cy}]{mark}")
    return "\n".join(lines)


def by_ref(cands: list[Candidate], ref: int) -> Candidate | None:
    """Resolve a model-returned ``node_ref``. Out-of-range returns ``None`` so the
    caller can degrade tree->vlm instead of raising (技术方案评估.md §5.2)."""
    for c in cands:
        if c.ref == ref:
            return c
    return None


def screen_signature(xml: str) -> str:
    """Order-sensitive digest of every visible label in the tree.

    Used by ``perception/capture.py`` to build the no-progress fingerprint. Text
    alone misses purely-visual changes (a video starting to play), which is why
    capture folds in a downsampled image hash as well.
    """
    parts = []
    for n in nodes(xml):
        t = node_text(n) or node_desc(n)
        if t:
            parts.append(re.sub(r"\s+", " ", t).strip())
    return "|".join(parts)
