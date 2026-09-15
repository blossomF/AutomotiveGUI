"""Graph state.

``AgentState`` is deliberately narrow — only the control-flow facts the edges need.
The bulky things (PNG frames, XML dumps, the assembled context, the logger) live on
``Runner`` and are referenced, not copied.

That is a real difference from ``LightGUIAgent/lightguiagent/agent.py``, which keeps
all of it as ``self.*`` instance state mutated in place by the loop. Mutable
instance state on a single object works for a ``for`` loop, but in a graph it makes
checkpoints and replay ambiguous (which node mutated what). Separating "state the
router reads" from "data the nodes read" keeps the conditional edges honest and
lets the graph be re-runs from any step later on.

``no_progress`` and ``screen_sig`` stay in the state because ``guard`` branches on
them, and a router that reads them should not have to trust a side object.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Route = Literal["skill", "tree", "vlm"]


class AgentState(TypedDict, total=False):
    # goal
    task: str
    declared_path: str

    # loop counters and limits
    step: int
    no_progress: int
    recover_depth: int

    # observation summary (full data is on Runner)
    screen_sig: str
    prev_sig: str
    node_count: int
    candidate_count: int

    # routing and the current decision
    route: Route
    action: dict
    last_action: dict
    # True only when decide wants one more attempt on a different route. It has to be
    # explicit: "no action produced" also happens when we give up on a step, and
    # routing that back to decide would spin.
    retry_decision: bool

    # outcome
    done: bool
    success: bool
    status: Literal["", "complete", "failed", "max_steps", "no_progress", "timeout", "error"]
    error: str
    result_text: str
