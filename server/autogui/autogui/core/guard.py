"""Hard limits on the loop.

LightGUIAgent has no equivalent. Its only stop conditions are ``for step in range(1,
max_steps+1)`` (``agent.py:389``) and the model saying COMPLETE (``agent.py:491``);
its "stuck detection" appends a warning string to the prompt and hopes
(``claude_client.py:322-338``). A model that keeps emitting the same click spends
all 20 steps and all the quota doing it. See 技术方案评估.md §2 C-9.

Three independent ceilings, any one of which ends the run:

  steps       iteration count, cheap and obvious
  no-progress N consecutive observations with an unchanged screen signature —
              catches spinning-in-place regardless of which action the model chose
  wall clock  an absolute deadline, because a Demo Day slot is 8 minutes

All three are *deterministic* code paths with no model call, which is what makes
them worth having: they still fire when the model is misbehaving.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GuardLimits:
    max_steps: int = 20
    no_progress_limit: int = 3
    wall_clock_limit: float = 180.0


class GuardVerdict:
    """Continue, or the reason we are stopping."""

    def __init__(self, stop_reason: str = "", detail: str = ""):
        self.stop_reason = stop_reason
        self.detail = detail

    @property
    def stop(self) -> bool:
        return bool(self.stop_reason)

    def __repr__(self) -> str:  # pragma: no cover
        return f"GuardVerdict(stop_reason={self.stop_reason!r}, detail={self.detail!r})"


GO = GuardVerdict()


def check(step: int, no_progress: int, elapsed: float, limits: GuardLimits) -> GuardVerdict:
    """Order matters: report the most actionable cause first.

    No-progress before max-steps, because "the screen has not changed for 3 steps"
    tells a debugging engineer far more than "we ran out of steps".
    """
    if elapsed >= limits.wall_clock_limit:
        return GuardVerdict("timeout", f"elapsed {elapsed:.0f}s >= {limits.wall_clock_limit:.0f}s")
    if step > limits.max_steps:
        return GuardVerdict("max_steps", f"exceeded {limits.max_steps} steps")
    if no_progress >= limits.no_progress_limit:
        return GuardVerdict("no_progress", f"screen unchanged for {no_progress} consecutive steps")
    return GO
