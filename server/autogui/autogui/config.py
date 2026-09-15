"""Configuration loading.

Deliberately *not* modelled on ``LightGUIAgent/lightguiagent/settings.py``, which
runs ADB auto-detection inside ``Settings.__init__`` and then needs a lazy
``_ConfigProxy`` layer (``config.py:50-105``) to stop that happening at import time.
Configuration here is pure data and never touches a device; device probing is an
explicit call from ``main.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class GridConfig(BaseModel):
    """Overlay grid for the VLM fallback route.

    Landscape is stated explicitly rather than derived: LightGUIAgent's auto-density
    (settings.py:159-171) clamps rows to ``max(15, ...)``, which on a 1920x1080 head
    unit silently produces 15 rows of ~128x72px cells instead of the ~10 a landscape
    screen wants. See 技术方案评估.md §2 C-11.
    """

    cols: int = Field(default=20, ge=5, le=26)  # 26 -> column letters A..Z
    rows: int = Field(default=10, ge=3, le=40)
    line_color: tuple = (255, 0, 0)
    line_width: int = 3
    label_size: int = 22
    label_color: tuple = (255, 255, 0)
    label_bg: tuple = (0, 0, 0, 180)
    jpeg_quality: int = Field(default=85, ge=40, le=100)
    max_side: int = Field(default=1568, ge=512, le=2560)  # downscale before encode

    @field_validator("cols")
    @classmethod
    def _cols_fit_alphabet(cls, v: int) -> int:
        if v > 26:
            raise ValueError("grid.cols must be <= 26 (single-letter column labels A..Z)")
        return v


class GLMConfig(BaseModel):
    """GLM 5.3 via its OpenAI-compatible endpoint. Key comes from the environment."""

    model: str = "glm-4v-plus"  # TODO 9/15: replace with the exact id the committee issues
    base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    api_key_env: str = "GLM_API_KEY"
    temperature: float = 0.1
    max_tokens: int = 1024
    timeout: float = 30.0
    max_retries: int = 2

    @property
    def api_key(self) -> str:
        import os

        return os.getenv(self.api_key_env, "")


class AgentConfig(BaseModel):
    max_steps: int = Field(default=20, ge=1, le=60)
    wall_clock_limit: float = Field(default=180.0, ge=30, le=900)  # seconds; guard input
    no_progress_limit: int = Field(default=3, ge=1, le=10)  # consecutive unchanged screens
    delay_after_action: float = Field(default=1.0, ge=0.0, le=10.0)
    history_window: int = Field(default=5, ge=1, le=20)
    token_budget: int = Field(default=8000, ge=1000, le=128000)
    tree_candidate_limit: int = Field(default=40, ge=5, le=200)
    reflection: Literal["cheap", "always", "off"] = "cheap"


class SkillConfig(BaseModel):
    enabled: bool = True
    directory: str = "skills"


class AppConfig(BaseModel):
    """A head-unit app we are allowed to touch.

    Contest rule 4.2 restricts scenarios to the CEA1.3 preinstalled set plus
    Bilibili / Feishu / 外卖, so this is a closed list — ``main.py --check`` walks it
    to produce the path A/B/C decision table.

    ``package`` is blank until the borrowed head unit is attached: run
    ``python main.py --packages`` to see what is actually installed rather than
    guessing from the phone-oriented list in
    ``LightGUIAgent/lightguiagent/claude_client.py:160-163``.
    """

    name: str
    package: str = ""
    in_scope: bool = True


class Config(BaseModel):
    exec_mode: Literal["local", "mcp"] = "local"
    declared_path: Literal["A", "B", "C"] = "C"  # frozen at 9/19 from real dump results
    device_serial: str = "auto"
    display_id: Optional[int] = None  # set when the default capture is not the centre console
    input_chain: list[str] = Field(default_factory=lambda: ["yadb", "adbkeyboard", "ascii"])
    grid: GridConfig = Field(default_factory=GridConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    glm: GLMConfig = Field(default_factory=GLMConfig)
    skills: SkillConfig = Field(default_factory=SkillConfig)
    apps: list[AppConfig] = Field(default_factory=list)
    log_dir: str = "artifacts/logs"
    verbose: bool = True

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        data: dict = {}
        if p.exists():
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"config.yaml must contain a mapping, got {type(loaded).__name__}")
            data = loaded
        return cls(**data)

    @property
    def artifacts_dir(self) -> Path:
        return PROJECT_ROOT / self.log_dir


def load(path: str | Path | None = None) -> Config:
    return Config.load(path)
