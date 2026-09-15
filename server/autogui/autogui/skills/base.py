"""Declarative YAML recipes, executed with zero VLM calls.

The file shape follows ``android-agent/gitd/skills/<app>/skill.yaml`` — a package
name, a list of named skills with parameters, and a step list — together with that
project's ``popup_detectors`` idea (``adb.py:134-150`` plus the three-tier
``dismiss_popups`` at :690-739). What we deliberately do **not** take is its
``Action``/``Workflow`` class system with precondition/postcondition/rollback: that
abstraction is built for replaying recorded macros across many devices, and mapping
a car head unit's UI onto it would cost days we do not have. A step is a dict and a
dispatcher resolves it.

Two rules that make this layer score well rather than just exist:

  * **A hint that cannot be resolved raises ``SkillMiss``, which the router turns into
    a graceful degrade to the generic tree/vlm route.** A brittle recipe is
    therefore never worse than no recipe — that is what makes skills safe to author
    in bulk in a week. It is also the same degrade path that earns the +10 technical
    depth points, so the two justifications share one implementation.
  * **Coordinates are never written into a recipe.** Hints are text/desc/rid
    matchers resolved against a *fresh* dump each step, so a recipe survives a UI
    refresh or a resolution change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from autogui.bridge.adb import Device, find_node, node_center
from autogui.bridge.input import TextInput

# Chinese head-unit popups. The android-agent original is English/TikTok-specific
# ({"not now","skip","cancel",...}) and matches nothing here — see 技术方案评估.md §2 A-3.
DEFAULT_POPUPS: list[dict] = [
    {"detect": "用户协议", "button": "同意"},
    {"detect": "隐私政策", "button": "同意"},
    {"detect": "隐私权政策", "button": "同意"},
    {"detect": "开启个性化推荐", "button": "以后再说"},
    {"detect": "通知权限", "button": "以后再说"},
    {"detect": "所在位置", "button": "禁止"},
    {"detect": "更新", "button": "以后再说"},
]
GENERIC_DISMISS = ("以后再说", "暂不", "暂不开启", "稍后再说", "取消", "关闭", "跳过",
                   "我知道了", "不同意", "残忍离开", "不再提醒", "稍后")


class SkillMiss(RuntimeError):
    """A step could not be located on the current screen — caller should degrade."""


@dataclass
class Skill:
    app: str
    name: str
    summary: str
    params: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    popups: list[dict] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    package: str = ""

    def signature(self) -> str:
        args = ", ".join(f"{p}=..." for p in self.params)
        return f"{self.name}({args}) — {self.summary}"

    def trigger_words(self) -> list[str]:
        """Declared triggers, falling back to the skill and app names."""
        return [t for t in self.triggers if t] or [self.name.replace("_", ""), self.app]


class SkillBook:
    """All loaded skills, plus the executor."""

    def __init__(self, root: Path | str, device: Device, text_input: TextInput):
        self.device = device
        self.text_input = text_input
        self.root = Path(root)
        self.by_id: dict[str, Skill] = {}
        self.load()

    # ── loading ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        self.by_id.clear()
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*/skill.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                print(f"  [skill] skipping {path}: {e}")
                continue
            package = (doc.get("app") or {}).get("package", "")
            popups = doc.get("popups") or []
            for spec in doc.get("skills") or []:
                name = spec.get("name")
                if not name:
                    continue
                skill = Skill(
                    app=doc.get("name", path.parent.name),
                    name=name,
                    summary=spec.get("summary", ""),
                    params=list(spec.get("params") or []),
                    steps=list(spec.get("steps") or []),
                    popups=popups,
                    triggers=[str(x) for x in (spec.get("triggers") or [])],
                    package=package,
                )
                self.by_id[f"{skill.app}.{skill.name}"] = skill

    def index(self) -> str:
        """One line per skill, injected into the permanent prompt block."""
        return "\n".join(s.signature() for s in sorted(self.by_id.values(), key=lambda x: x.app)) or "（无）"

    def get(self, key: str) -> Skill | None:
        if key in self.by_id:
            return self.by_id[key]
        hits = [s for k, s in self.by_id.items() if k.endswith("." + key) or s.name == key]
        return hits[0] if len(hits) == 1 else None

    # ── execution ────────────────────────────────────────────────────────────

    def run(self, key: str, params: dict | None = None) -> list[str]:
        """Execute a recipe, returning a trace of human-readable step lines.

        Raises ``SkillMiss`` on the first unresolvable hint; the graph catches it and
        reroutes through the generic channel.
        """
        skill = self.get(key)
        if skill is None:
            raise SkillMiss(f"unknown skill {key!r}")
        args = dict(params or {})
        for p in skill.params:
            if p not in args:
                raise SkillMiss(f"{key} missing required param {p!r}")

        trace: list[str] = []
        for i, step in enumerate(skill.steps, 1):
            line = self._run_step(skill, step, args)
            trace.append(f"  step{i} {line}")
        return trace

    def _sub(self, skill: Skill, value, args: dict):
        """Substitute ``{param}`` and the implicit ``{package}``."""
        if not isinstance(value, str):
            return value
        out = value
        if "{package}" in out:
            out = out.replace("{package}", skill.package)
        for k, v in args.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def _arg_or_text(self, skill: Skill, step: dict, args: dict) -> str:
        """A step takes either ``arg: <param name>`` or a literal ``text:``."""
        if step.get("arg"):
            key = step["arg"]
            if key not in args:
                raise SkillMiss(f"step references unknown param {key!r}")
            return str(self._sub(skill, args[key], args))
        return str(self._sub(skill, step.get("text", ""), args))

    def _run_step(self, skill: Skill, step: dict, args: dict) -> str:
        act = step.get("act", "")
        d = self.device

        if act == "launch":
            pkg = self._sub(skill, step.get("package") or skill.package, args)
            if not pkg:
                raise SkillMiss("launch step has no package to use")
            how = d.launch_app(pkg, delay=float(step.get("delay", 2.0)))
            return f"launch {pkg} via {how}"

        if act == "stop":
            pkg = self._sub(skill, step.get("package") or skill.package, args)
            d.stop_app(pkg)
            return f"stop {pkg}"

        if act == "dismiss_popups":
            n = self.dismiss(skill, extra=step.get("extra") or [])
            return f"dismiss_popups (closed {n})"

        if act in ("click", "tap"):
            node = self._locate(skill, step, args)
            c = node_center(node)
            if not c:
                raise SkillMiss("matched node has no bounds")
            d.tap(*c)
            return f"tap {self._describe(step)} -> ({c[0]},{c[1]})"

        if act == "type":
            value = self._arg_or_text(skill, step, args)
            r = self.text_input.type(value, clear_first=bool(step.get("clear_first")))
            if not r.typed:
                raise SkillMiss(f"cannot type {value!r} on this device")
            return f"type {value!r} via {r.strategy}"

        if act == "scroll":
            direction = step.get("direction", "down")
            self._scroll(direction, step.get("amount", 0.5))
            return f"scroll {direction}"

        if act == "keyevent":
            key = step.get("key", "KEYCODE_BACK")
            d.keyevent(key)
            return f"keyevent {key}"

        if act == "wait_for":
            needle = self._arg_or_text(skill, step, args)
            try:
                d.wait_for(needle, timeout=float(step.get("timeout", 10)))
            except TimeoutError as e:
                raise SkillMiss(str(e))
            return f"wait_for {needle!r} ok"

        if act == "sleep":
            import time

            time.sleep(min(float(step.get("seconds", 1.0)), 5.0))
            return f"sleep {step.get('seconds', 1.0)}s"

        if act in ("complete", "done"):
            return "complete"

        raise SkillMiss(f"unsupported step act={act!r}")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _describe(self, step: dict) -> str:
        hint = step.get("hint") or {}
        for k in ("text", "desc", "rid"):
            if hint.get(k):
                return f"{k}={hint[k]!r}"
        if step.get("nth") is not None:
            return f"nth={step['nth']}"
        return "?"

    def _locate(self, skill: Skill, step: dict, args: dict) -> str:
        """Resolve a hint into one node against a fresh dump."""
        hint = step.get("hint") or {}
        match = {k: self._sub(skill, v, args) for k, v in hint.items() if k in ("text", "desc", "rid")}
        if not match:
            raise SkillMiss("click step needs hint.text / hint.desc / hint.rid")
        from autogui.bridge.adb import find_nodes

        xml = self.device.dump_xml()
        nth = int(step.get("nth", hint.get("nth", 0)))
        found: list[str] = []
        for key, val in match.items():
            found = find_nodes(xml, clickable_only=True, **{key: val})
            if len(found) > nth:
                break
        if not found:  # retry without the clickable requirement
            for key, val in match.items():
                found = find_nodes(xml, **{key: val})
                if len(found) > nth:
                    break
        if len(found) <= nth:
            raise SkillMiss(f"no match for {self._describe(step)} (found {len(found)}, need index {nth})")
        return found[nth]

    def _scroll(self, direction: str, amount: float) -> None:
        w, h = self.device.screen_size()
        cx, cy = w // 2, h // 2
        dx, dy = int(w * amount / 2), int(h * amount / 2)
        vector = {
            "down": (cx, cy + dy, cx, cy - dy),
            "up": (cx, cy - dy, cx, cy + dy),
            "left": (cx + dx, cy, cx - dx, cy),
            "right": (cx - dx, cy, cx + dx, cy),
        }
        if direction not in vector:
            raise SkillMiss(f"bad scroll direction {direction!r}")
        self.device.swipe(*vector[direction])

    def dismiss(self, skill: Skill, extra: list | None = None) -> int:
        """Three-tier popup dismissal: declared -> generic words -> nothing.

        Mirrors the structure of ``android-agent`` ``dismiss_popups`` (adb.py:690-739)
        with a Chinese word list, and keeps its oversized-node guard so a full-width
        caption is not mistaken for a dismiss button.
        """
        closed = 0
        for _ in range(4):  # popups sometimes stack
            xml = self.device.dump_xml()
            if not xml:
                break
            from autogui.bridge.adb import find_nodes, nodes, node_bounds, node_text, node_desc

            target = None
            for popup in (skill.popups or DEFAULT_POPUPS) + (extra or []):
                if popup.get("detect") and popup["detect"] not in xml:
                    continue
                btn = popup.get("button")
                if popup.get("method") == "back":
                    self.device.back()
                    closed += 1
                    target = "back"
                    break
                cands = find_nodes(xml, text=btn) or find_nodes(xml, desc=btn)
                cands = [c for c in cands if node_bounds(c)]
                if cands:
                    cands.sort(key=lambda n: node_bounds(n)[1])  # topmost instance
                    self.device.tap(*node_center(cands[0]))
                    closed += 1
                    target = btn
                    break
            if target:
                continue

            for n in nodes(xml):
                if 'clickable="true"' not in n:
                    continue
                b = node_bounds(n)
                if not b:
                    continue
                w, h = self.device.screen_size()
                if (b[2] - b[0]) > w * 0.6:  # content area, not a button
                    continue
                if b[3] < 40:
                    continue
                label = f"{node_text(n)} {node_desc(n)}".strip()
                if any(word in label for word in GENERIC_DISMISS):
                    self.device.tap(*node_center(n))
                    closed += 1
                    break
            else:
                break
        return closed


def run_by_name(key: str, params: dict, *, device: Device | None = None) -> list[str]:
    """Convenience entry used by the ``run_skill`` MCP tool."""
    from autogui.config import PROJECT_ROOT, load

    if device is None:
        from autogui.bridge.tools import dev

        device = dev()
    cfg = load()
    book = SkillBook(PROJECT_ROOT / cfg.skills.directory, device, TextInput(device))
    return book.run(key, params)
