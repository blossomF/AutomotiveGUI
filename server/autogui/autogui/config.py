"""配置加载。

刻意*没有*照搬 ``LightGUIAgent/lightguiagent/settings.py``——那套代码在
``Settings.__init__`` 里就跑 ADB 自动探测，随后还需要一层懒加载的
``_ConfigProxy``（``config.py:50-105``）来阻止 import 时触发。这里的配置是
纯数据，从不触碰设备；设备探测是 ``main.py`` 里的显式调用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _decrypt_or_empty(value: str) -> str:
    """ENC() 密文尝试 DPAPI 解密；失败则返回空串以回退环境变量。

    DPAPI 密文绑定加密者的 Windows 用户：config.yaml 进 git 后，换机器/换
    用户/非 Windows 平台都解不开。此时绝不能让 Config.load 崩溃——
    打印一行提示并按"未配置"处理，由环境变量接管（队友各自 set 自己的凭证）。
    """
    from autogui.security import is_encrypted, resolve_secret

    if not is_encrypted(value):
        return value  # 明文或空值，原样返回
    try:
        return resolve_secret(value)
    except Exception as e:  # noqa: BLE001 — 任何解密失败都降级，不阻断启动
        print(f"[config] ENC 凭证解密失败（{e}），改用环境变量；"
              f"本机录入请运行 python -m autogui.security set <section.field>",
              file=sys.stderr)
        return ""


class GridConfig(BaseModel):
    """VLM 兜底路由的叠加网格。

    横屏是显式声明的，而不是推导出来的：LightGUIAgent 的自动密度
    （settings.py:159-171）把行数钳制到 ``max(15, ...)``，在 1920x1080
    车机上会悄悄得到 15 行 ~128x72px 的格子，而不是横屏应有的 ~10 行。
    见 技术方案评估.md §2 C-11。
    """

    mode: Literal["auto", "manual"] = "auto"
    cols: int = Field(default=20, ge=5, le=26)  # 只在 manual 时使用；26 -> 列字母 A..Z
    rows: int = Field(default=10, ge=3, le=40)
    line_color: tuple = (255, 0, 0)
    line_width: int = 3
    label_size: int = 22
    label_color: tuple = (255, 255, 0)
    label_bg: tuple = (0, 0, 0, 180)
    jpeg_quality: int = Field(default=85, ge=40, le=100)
    max_side: int = Field(default=1568, ge=512, le=2560)  # 编码前先降采样

    @field_validator("cols")
    @classmethod
    def _cols_fit_alphabet(cls, v: int) -> int:
        if v > 26:
            raise ValueError("grid.cols must be <= 26 (single-letter column labels A..Z)")
        return v


class GLMConfig(BaseModel):
    """GLM 5.3，走 OpenAI 兼容端点。

    api_key 支持三种写法（优先级从高到低）：
    1. config.yaml 的 ENC(...) DPAPI 密文（见 autogui.security，防明文入库扣分）；
    2. config.yaml 明文（兼容，不推荐）；
    3. 留空 → 回退环境变量 GLM_API_KEY。
    """

    model: str = "glm-4v-plus"  # TODO 9/15: 换成组委会发放 key 对应的确切型号 id
    base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    api_key_env: str = "GLM_API_KEY"
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 1024
    timeout: float = 30.0
    max_retries: int = 4

    @field_validator("api_key")
    @classmethod
    def _api_key_from_env(cls, v: str, info) -> str:
        v = _decrypt_or_empty(v)  # ENC(...) 密文 → 明文；解密失败/明文/空 → 原值或空
        if v:
            return v
        return os.getenv(info.data.get("api_key_env", "GLM_API_KEY"), "")


class AgentConfig(BaseModel):
    max_steps: int = Field(default=20, ge=1, le=60)
    wall_clock_limit: float = Field(default=180.0, ge=30, le=900)  # 秒；guard 的输入
    no_progress_limit: int = Field(default=3, ge=1, le=10)  # 屏幕指纹连续不变的步数
    delay_after_action: float = Field(default=1.0, ge=0.0, le=10.0)
    history_window: int = Field(default=5, ge=1, le=20)
    token_budget: int = Field(default=8000, ge=1000, le=128000)
    tree_candidate_limit: int = Field(default=40, ge=5, le=200)
    reflection: Literal["cheap", "always", "off"] = "cheap"


class SkillConfig(BaseModel):
    enabled: bool = True
    directory: str = "skills"


class VoiceConfig(BaseModel):
    """讯飞流式听写（章程 §4.6 加分项）：可插拔的语音输入通道，主循环不依赖它。

    三个凭证的读取优先级（与 GLMConfig.api_key 一致）：
    1. config.yaml 的 ENC(...) DPAPI 密文（见 autogui.security，防明文入库扣分）；
    2. config.yaml 明文（兼容，不推荐）；
    3. 留空 → 回退环境变量 IFLYTEK_APPID / IFLYTEK_API_KEY / IFLYTEK_API_SECRET。
    """

    appid: str = ""
    api_key: str = ""
    api_secret: str = ""
    iat_url: str = ""  # 留空 = 讯飞公网 wss://iat-api.xfyun.cn/v2/iat；组委会给其他网关时填这里
    max_seconds: float = Field(default=15.0, ge=3, le=120)  # 最长录音秒数
    silence_seconds: float = Field(default=0.8, ge=0.2, le=5.0)  # 尾部连续静音后自动停止
    silence_threshold: int = Field(default=300, ge=50, le=3000)  # 静音判定 RMS 阈值

    @field_validator("appid", "api_key", "api_secret")
    @classmethod
    def _voice_cred_from_env(cls, v: str, info) -> str:
        v = _decrypt_or_empty(v)  # ENC(...) 密文 → 明文；解密失败/明文/空 → 原值或空
        if v:
            return v
        env = {"appid": "IFLYTEK_APPID", "api_key": "IFLYTEK_API_KEY",
               "api_secret": "IFLYTEK_API_SECRET"}[info.field_name]
        return os.getenv(env, "")


class AppConfig(BaseModel):
    """一个我们被允许操作的车机 App。

    竞赛规则 4.2 把场景限定在 CEA1.3 预装集 + 哔哩哔哩 / 飞书 / 外卖，
    所以这是一个封闭列表——``main.py --check`` 遍历它来生成路径 A/B/C
    决策表。

    ``package`` 在借到车机之前留空：运行
    ``python main.py --packages`` 看实际装了什么，而不是照着
    ``LightGUIAgent/lightguiagent/claude_client.py:160-163`` 里那份
    手机向的列表猜。
    """

    name: str
    package: str = ""
    in_scope: bool = True


class Config(BaseModel):
    exec_mode: Literal["local", "mcp"] = "local"
    declared_path: Literal["A", "B", "C"] = "C"  # 9/19 依据实测 dump 结果冻结
    device_serial: str = "auto"
    display_id: Optional[int] = None  # 默认捕获不是中控屏时才设置
    input_chain: list[str] = Field(default_factory=lambda: ["yadb", "adbkeyboard", "ascii"])
    grid: GridConfig = Field(default_factory=GridConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    glm: GLMConfig = Field(default_factory=GLMConfig)
    skills: SkillConfig = Field(default_factory=SkillConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    apps: list[AppConfig] = Field(default_factory=list)
    log_dir: str = "test/log"
    verbose: bool = True
    debug: bool = False  # 细粒度日志开关（截图/XML/GLM交互落盘），生产环境保持False

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
