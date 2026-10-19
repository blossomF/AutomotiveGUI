"""图状态。

``AgentState`` 刻意保持窄——只放条件边（router）需要读的控制流事实。
大块数据（PNG 帧、XML dump、组装好的上下文、日志器）放在 ``Runner`` 上，
只被引用、不被复制。

这与 ``LightGUIAgent/lightguiagent/agent.py`` 有实质区别：后者把所有东西
都作为 ``self.*`` 实例状态、在循环里原地修改。可变实例状态对 ``for``
循环没问题，但在图里会让 checkpoint 和回放变得含糊（哪个节点改了什么）。
把"路由器读的状态"和"节点读的数据"分开，条件边才诚实，以后也能从任意
步重新运行图。

``no_progress`` 和 ``screen_sig`` 留在 state 里，因为 ``guard`` 靠它们
分支，读它们的 router 不应被迫信任某个旁路对象。
"""

from __future__ import annotations

from typing import Literal, TypedDict

Route = Literal["skill", "tree", "vlm"]


class AgentState(TypedDict, total=False):
    # 目标
    task: str

    # 循环计数与上限
    step: int
    no_progress: int  # 仅 perceive 更新；同屏每轮 +1，画面变化才清零。
    recover_depth: int

    # 观测摘要（完整数据在 Runner 上）
    screen_sig: str
    prev_sig: str
    node_count: int
    candidate_count: int

    # 路由与当前决策
    route: Route
    action: dict
    last_action: dict
    # 仅当 decide 想在另一条路由上再试一次时为 True。必须显式标记：
    # "没有产生动作"在"放弃本步"时也会出现，若把那种情况也路由回
    # decide 会空转。
    retry_decision: bool

    # 结果
    done: bool
    success: bool
    status: Literal["", "complete", "failed", "max_steps", "no_progress", "timeout", "error"]
    error: str
    result_text: str
