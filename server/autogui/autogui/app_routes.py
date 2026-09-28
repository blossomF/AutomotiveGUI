"""首次纯文本决策：由模型选择配置应用直启，或进入屏幕观测。"""

from __future__ import annotations

from dataclasses import dataclass

from autogui.brain import prompts
from autogui.config import AppConfig


@dataclass(frozen=True)
class InitialRoute:
    app: AppConfig | None = None
    finish_after_launch: bool = False
    reason: str = ""


def choose_initial_route(task: str, apps: list[AppConfig], glm) -> InitialRoute:
    """只接受模型选择的缓存 ID；模型不能提供或猜测包名。"""
    available = [app for app in apps if app.in_scope and app.package]
    if not available:
        return InitialRoute(reason="未配置可直启应用")

    catalog = "\n".join(
        f"{i}: {app.name}" + (f"（别名：{'、'.join(app.aliases)}）" if app.aliases else "")
        for i, app in enumerate(available, 1)
    )
    messages = [
        {"role": "system", "content": prompts.render("initial_route_system")},
        {"role": "user", "content": prompts.render("initial_route_user", task=task, catalog=catalog)},
    ]
    raw = glm.chat_json(messages, kind="initial_app_route")
    if not isinstance(raw, dict):
        return InitialRoute(reason="模型首次判断格式无效")
    decision = raw.get("decision")
    if decision == "OBSERVE":
        return InitialRoute(reason="模型选择屏幕观测")
    if decision not in ("OPEN_ONLY", "OPEN_CONTINUE"):
        return InitialRoute(reason="模型首次判断动作无效")
    app_id = raw.get("app_id")
    if type(app_id) is not int or not 1 <= app_id <= len(available):
        return InitialRoute(reason="模型选择的应用 ID 不在缓存中")
    return InitialRoute(app=available[app_id - 1],
                        finish_after_launch=decision == "OPEN_ONLY",
                        reason=f"模型选择 {decision}")
