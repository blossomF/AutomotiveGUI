"""The action space — single source of truth for prompt, parser and executor.

Wider than ``LightGUIAgent/lightguiagent/config.py:150-156``'s five actions, which
lack BACK and HOME entirely: there, "recover" means the prompt telling the model that
a back button is *usually* in the top-left, around grid A1-B3
(``claude_client.py:151-156``). On a landscape head unit with a persistent dock that
hint is wrong, and the model burns steps hunting for a button. See 技术方案评估.md
§2 C-10.

``FAIL`` matters as well: without an explicit give-up action a stuck agent has no way
to end a task early except ``COMPLETE`` (wrong) or exhausting ``max_steps`` (slow and
expensive).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionSpec:
    name: str
    args: tuple[str, ...]
    meaning: str


ACTIONS: tuple[ActionSpec, ...] = (
    ActionSpec("CLICK", ("node_ref | grid",), "Tap one on-screen candidate"),
    ActionSpec("LONG_PRESS", ("node_ref | grid",), "Hold ~0.8s — context menus, drag handles"),
    ActionSpec("TYPE", ("text", "clear_first?"), "Type into the focused field"),
    ActionSpec("SCROLL", ("direction",), "Scroll down|up (or right|left on landscape lists)"),
    ActionSpec("BACK", (), "System back — leaves a wrong page or dismisses a dialog"),
    ActionSpec("HOME", (), "System home — escape hatch when a page is unrecoverable"),
    ActionSpec("LAUNCH", ("app",), "Start an app by package name"),
    ActionSpec("SKILL", ("name", "params?"), "Run a declarative recipe instead of stepping"),
    ActionSpec("WAIT", ("seconds?",), "Let an animation or network load settle"),
    ActionSpec("COMPLETE", ("result?",), "Task done — say what was achieved"),
    ActionSpec("FAIL", ("reason",), "Task cannot be completed — stop and explain"),
)

ACTION_NAMES: tuple[str, ...] = tuple(a.name for a in ACTIONS)
TERMINAL: frozenset[str] = frozenset({"COMPLETE", "FAIL"})

# Actions whose effect we can check with a screen-signature change and no VLM call.
MUTATING: frozenset[str] = frozenset({"CLICK", "LONG_PRESS", "TYPE", "SCROLL", "BACK", "HOME", "LAUNCH", "SKILL"})

SCROLL_DIRECTIONS = ("down", "up", "left", "right")


def parse_action(raw: dict) -> dict:
    """Validate one model-emitted action dict into a normalised form.

    Raises ``ValueError`` for anything unusable; ``brain.glm_client`` retries once
    with the complaint and the graph degrades rather than crashing if it fails again.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"action must be an object, got {type(raw).__name__}")
    name = str(raw.get("action", "")).strip().upper()
    if name not in ACTION_NAMES:
        raise ValueError(f"unknown action {name!r}; valid: {', '.join(ACTION_NAMES)}")

    out: dict = {"type": name, "explain": str(raw.get("explain", ""))[:200]}

    if name in ("CLICK", "LONG_PRESS"):
        if "node_ref" in raw and raw["node_ref"] is not None:
            try:
                out["node_ref"] = int(str(raw["node_ref"]).lstrip("#"))
            except (TypeError, ValueError):
                raise ValueError(f"node_ref must be an integer, got {raw['node_ref']!r}")
        elif raw.get("grid"):
            out["grid"] = str(raw["grid"]).strip().upper()
        else:
            raise ValueError(f"{name} needs node_ref or grid")
    elif name == "TYPE":
        if "text" not in raw:
            raise ValueError("TYPE needs text")
        out["text"] = str(raw["text"])
        out["clear_first"] = bool(raw.get("clear_first", False))
    elif name == "SCROLL":
        d = str(raw.get("direction", "down")).strip().lower()
        if d not in SCROLL_DIRECTIONS:
            raise ValueError(f"SCROLL direction must be one of {SCROLL_DIRECTIONS}, got {d!r}")
        out["direction"] = d
    elif name == "LAUNCH":
        out["app"] = str(raw.get("app", "")).strip()
        if not out["app"]:
            raise ValueError("LAUNCH needs app (package name)")
    elif name == "SKILL":
        out["skill"] = str(raw.get("name", "")).strip()
        out["params"] = raw.get("params") or {}
        if not out["skill"]:
            raise ValueError("SKILL needs name")
    elif name == "WAIT":
        try:
            out["seconds"] = min(float(raw.get("seconds", 1.5)), 5.0)
        except (TypeError, ValueError):
            out["seconds"] = 1.5
    elif name == "COMPLETE":
        out["result"] = str(raw.get("result", ""))[:300]
    elif name == "FAIL":
        out["reason"] = str(raw.get("reason", raw.get("explain", "unknown")))[:300]

    return out
