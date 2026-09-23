"""GLM 5.3 客户端，走 OpenAI 兼容协议。

取代 ``LightGUIAgent/lightguiagent/claude_client.py``——那套代码绑定
``anthropic`` SDK（:11 处 ``from anthropic import Anthropic``，:270 处
``client.messages.create``）。替换不只是改构造函数，有三个具体差异：

  * 图像载荷：Anthropic 的 ``{type:"image", source:{media_type, data}}`` 变为
    OpenAI 的 ``{type:"image_url", image_url:{url:"data:image/...;base64,..."}}``。
    ``prompts.py`` 已经按 OpenAI 形态输出。
  * 用量字段：``usage.input_tokens``/``output_tokens`` -> ``prompt_tokens``/
    ``completion_tokens``。
  * 计费：``config.py:162-165`` 写死 $5/$25 每百万 token 并报美元。
    GLM 走组委会发放的配额，美元数字在这里没有意义——我们只报
    token 数和调用次数。

直接用 ``openai`` 而不是 ``langchain-openai``：少一个运行时依赖，
而且不需要链式组合（控制流由 LangGraph 负责）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from autogui.actions import parse_action
from autogui.brain import prompts
from autogui.config import GLMConfig


class GLMError(RuntimeError):
    """模型无法连通，或始终没有产出可用回答。"""


def _is_retryable(exc: BaseException) -> bool:
    """区分"会自愈的上游抖动"与"应立即失败的硬错误"。

    thundersoft 网关后端实例异常时，经 litellm 把故障包装成
    ``400 messages containing only assistant or system roles``——该文案
    与请求体无关：实测同一合法请求几秒/几分钟后可成功，属可自愈抖动，
    必须重试。超时 / 5xx / 429 / 连接错误同样重试；401/403 鉴权类与
    其它确定性 400 立即失败，不空等。
    """
    if isinstance(exc, GLMError):  # 内部信号（循环内仅 empty completion），同样值得重试
        return True
    if exc.__class__.__name__ in (
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "APIConnectionError",
    ):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status >= 500 or status == 429:
            return True
        if status in (401, 403):
            return False
    msg = str(exc)
    if "only assistant or system roles" in msg or "only system roles" in msg:
        return True
    return False


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    retries: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def merge(self, other: "Usage") -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.latency_ms += other.latency_ms
        self.retries += other.retries

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 1),
            "retries": self.retries,
        }


class GLMClient:
    """薄的、JSON 严格的 chat 封装，带用量统计。"""

    def __init__(self, cfg: GLMConfig, client=None):
        self.cfg = cfg
        self.usage = Usage()
        self.last_call = Usage()
        self._client = client
        if self._client is None:
            if not cfg.api_key:
                raise GLMError(f"{cfg.api_key_env} is not set — cannot call GLM")
            from openai import OpenAI

            self._client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.timeout)

    # ── 原始 chat ────────────────────────────────────────────────────────────

    def chat(self, messages: list[dict]) -> tuple[str, Usage]:
        """一次 chat 补全，传输错误时重试。返回 (text, usage)。

        ``usage`` 覆盖**这一次**逻辑调用（含其重试），并镜像到
        ``self.last_call``。如果这里返回累计的 ``self.usage``，决策日志里
        的每步数字会逐步递增，对排查问题的人是误导。
        """
        call = Usage()
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            started = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(
                    model=self.cfg.model,
                    messages=messages,
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                )
                call.calls += 1
                call.retries += attempt
                call.latency_ms += (time.perf_counter() - started) * 1000
                if getattr(resp, "usage", None):
                    call.prompt_tokens += int(resp.usage.prompt_tokens or 0)
                    call.completion_tokens += int(resp.usage.completion_tokens or 0)
                self.usage.merge(call)
                self.last_call = call
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    raise GLMError("empty completion")
                return content, call
            except Exception as e:  # 传输 / 鉴权 / schema 漂移
                last = e
                if attempt < self.cfg.max_retries and _is_retryable(e):
                    # 指数退避 1/2/4/8s：网关坏窗常持续十几秒，多次重试以撞上健康实例
                    time.sleep(min(8.0, 1.0 * (2 ** attempt)))
                    continue
                break  # 硬错误（401/403/确定性 400）立即失败，不空等
        self.last_call = call
        raise GLMError(f"GLM call failed after {self.cfg.max_retries + 1} attempts: {last}")

    def chat_json(self, messages: list[dict]) -> dict:
        """chat 后按 JSON 解析；解析失败时带着抱怨重问一次。

        多一次往返比下游浪费一步便宜，也让畸形输出处理留在图之外。
        ``last_call`` 累加两次尝试，所以决策日志的每步 token 数是真实成本。
        """
        text, first = self.chat(messages)
        try:
            return prompts.first_json_object(text)
        except ValueError as e:
            self.usage.retries += 1
            retry = messages + [
                {"role": "assistant", "content": text[:1500]},
                {"role": "user", "content": f"上一回复无法解析为 JSON（{e}）。{prompts.OUTPUT_RULE} 重新输出："},
            ]
            text2, second = self.chat(retry)
            merged = Usage(
                calls=first.calls + second.calls,
                prompt_tokens=first.prompt_tokens + second.prompt_tokens,
                completion_tokens=first.completion_tokens + second.completion_tokens,
                latency_ms=first.latency_ms + second.latency_ms,
                retries=first.retries + second.retries + 1,
            )
            self.last_call = merged
            return prompts.first_json_object(text2)

    # ── 高层：decide ─────────────────────────────────────────────────────────

    def decide(
        self,
        context_text: str,
        *,
        jpeg_b64: str | None = None,
        grid_spec: str = "20x10",
        screen_size: tuple[int, int] | None = None,
    ) -> tuple[dict, Usage]:
        """请求下一个动作。返回归一化后的动作 dict。

        即使 JSON 重试后动作仍不可用时抛 ``ValueError``——调用方（图）
        将其视为决策失败，降级路由 tree->vlm 或计一次无进展步，
        绝不崩溃。
        """
        messages = prompts.build_decide_messages(
            context_text, jpeg_b64, grid_spec, screen=screen_size
        )
        raw = self.chat_json(messages)
        try:
            action = parse_action(raw)
        except ValueError as e:
            # 把模型原始输出带进错误信息。否则调用方只看到 "SKILL needs name",
            # 无从判断模型究竟写了什么字段（实测诊断需另写 hook 抓请求才查得出）。
            raise ValueError(
                f"{e} | 模型原始输出={json.dumps(raw, ensure_ascii=False)[:300]}"
            ) from e
        progress = str(raw.get("progress", ""))[:300]
        entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
        action["_progress"] = progress
        action["_entities"] = _clean_entities(entities)
        return action, self.last_call

    # ── 高层：reflect ────────────────────────────────────────────────────────

    def reflect(self, action_desc: str, goal: str, before_b64: str, after_b64: str) -> str:
        """A / B / C / UNKNOWN。UNKNOWN 的存在原因见 ``prompts.parse_reflection``。"""
        text, _ = self.chat(prompts.build_reflect_messages(action_desc, goal, before_b64, after_b64))
        return prompts.parse_reflection(text)


def _clean_entities(raw: dict) -> dict:
    """保持实体小而字符串化——它们会出现在之后每一轮提示词里。"""
    out: dict[str, str] = {}
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return out
    if not isinstance(items, dict):
        return out
    for k, v in list(items.items())[:12]:
        if v is None:
            continue
        sval = str(v).strip()
        if sval and sval.lower() not in ("none", "null"):
            out[str(k).strip()[:24]] = sval[:60]
    return out
