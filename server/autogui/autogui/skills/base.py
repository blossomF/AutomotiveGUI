"""技能目录：只向模型提供操作指导，不包含设备执行器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from autogui.brain import prompts


class SkillMiss(RuntimeError):
    """技能或指导参数不可用，交由模型继续决策。"""


@dataclass
class Skill:
    app: str
    name: str
    summary: str
    guide: str = ""
    params: list[str] = field(default_factory=list)
    package: str = ""
    configured_app: str = ""

    def signature(self) -> str:
        """一行技能签名，注入永久提示词块。

        使用 ``app.name`` 全名，与模型返回的 SKILL.name 和内部键一致。
        """
        args = ", ".join(f"{p}=..." for p in self.params)
        return f"{self.app}.{self.name}({args}) — {self.summary}"


class SkillBook:
    """加载、索引和渲染技能指导。"""

    def __init__(self, root: Path | str, app_packages: dict | None = None):
        self.root = Path(root)
        # 页面配置（config.yaml apps 段，设置页设备探测）的应用名 -> 包名。
        # 有 configured_app 绑定的技能优先用它；YAML 里的 app.package 仅作回退。
        self.app_packages = dict(app_packages or {})
        self.by_id: dict[str, Skill] = {}
        self.load()

    # ── 加载 ─────────────────────────────────────────────────────────────────

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
            for spec in doc.get("skills") or []:
                name = spec.get("name")
                if not name:
                    continue
                skill = Skill(
                    app=doc.get("name", path.parent.name),
                    name=name,
                    summary=spec.get("summary", ""),
                    guide=str(spec.get("guide") or "").strip(),
                    params=list(spec.get("params") or []),
                    package=package,
                    configured_app=str(doc.get("configured_app") or "").strip(),
                )
                # 包名以页面配置为准（设置页设备探测写入 config.yaml 的 apps 段）
                if skill.configured_app and self.app_packages.get(skill.configured_app):
                    skill.package = self.app_packages[skill.configured_app]
                self.by_id[f"{skill.app}.{skill.name}"] = skill

    def index(self) -> str:
        """只展示有模型指导内容的技能。"""
        return "\n".join(
            s.signature() for _, s in sorted(self.by_id.items(), key=lambda kv: kv[1].app) if s.guide
        ) or "（无）"

    def for_configured_app(self, app_name: str) -> list[Skill]:
        """Skills explicitly bound to a configured app for the initial model route."""
        return [s for s in self.by_id.values()
                if s.guide and s.configured_app == app_name]

    def guidance(self, key: str, params: dict | None = None) -> str:
        """返回给模型的操作指导，不执行任何设备动作。"""
        skill = self.get(key)
        if skill is None or not skill.guide:
            raise SkillMiss(f"no model guide for {key!r}")
        args = dict(params or {})
        missing = [p for p in skill.params if not str(args.get(p, "")).strip()]
        if missing:
            raise SkillMiss(f"{key} missing guide params: {', '.join(missing)}")
        guide = self._sub(skill, skill.guide, args)
        return prompts.render(
            "skill_guide_wrapper", skill_id=f"{skill.app}.{skill.name}",
            params=args, guide=guide,
        )

    def get(self, key: str) -> Skill | None:
        if key in self.by_id:
            return self.by_id[key]
        hits = [s for k, s in self.by_id.items() if k.endswith("." + key) or s.name == key]
        return hits[0] if len(hits) == 1 else None


    def _sub(self, skill: Skill, value, args: dict):
        """替换 ``{param}`` 和隐式的 ``{package}``。"""
        if not isinstance(value, str):
            return value
        out = value
        if "{package}" in out:
            out = out.replace("{package}", skill.package)
        for k, v in args.items():
            out = out.replace("{" + k + "}", str(v))
        return out
