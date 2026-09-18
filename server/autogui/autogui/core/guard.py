"""循环的硬性上限。

三个独立上限，任一触发即结束运行：

  steps       迭代次数，廉价且直观
  no-progress 屏幕指纹连续 N 次观测不变——不管模型选了什么动作，
              都能抓住"原地打转"
  wall clock  绝对截止时刻，因为 Demo Day 一个时间槽只有 8 分钟

三者都是*确定性*代码路径，不调用模型——这正是它们值得存在的原因：
模型行为失控时它们依然会触发。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GuardLimits:
    max_steps: int = 20
    no_progress_limit: int = 3
    wall_clock_limit: float = 180.0


class GuardVerdict:
    """继续，或停止的原因。"""

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
    """顺序有讲究：先报告最具可操作性的原因。

    no-progress 优先于 max-steps，因为"屏幕连续 3 步没变化"比
    "步数用完了"给排查问题的工程师提供的信息多得多。
    """
    if elapsed >= limits.wall_clock_limit:
        return GuardVerdict("timeout", f"elapsed {elapsed:.0f}s >= {limits.wall_clock_limit:.0f}s")
    if step > limits.max_steps:
        return GuardVerdict("max_steps", f"exceeded {limits.max_steps} steps")
    if no_progress >= limits.no_progress_limit:
        return GuardVerdict("no_progress", f"screen unchanged for {no_progress} consecutive steps")
    return GO
