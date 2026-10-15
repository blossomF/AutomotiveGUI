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
    usage_complete: bool = True
    usage_reports: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def merge(self, other: "Usage") -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.latency_ms += other.latency_ms
        self.retries += other.retries
        self.usage_complete = self.usage_complete and other.usage_complete
        self.usage_reports += other.usage_reports

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 1),
            "retries": self.retries,
            "usage_complete": self.usage_complete,
            "usage_reports": self.usage_reports,
        }


class GLMClient:
    """薄的、JSON 严格的 chat 封装，带用量统计。"""

    def __init__(self, cfg: GLMConfig, client=None, log=None):
        self.cfg = cfg
        self.usage = Usage()
        self.last_call = Usage()
        self._client = client
        self._log = log  # 日志器引用
        self._call_seq = 0
        if self._client is None:
            if not cfg.api_key:
                raise GLMError(f"{cfg.api_key_env} is not set — cannot call GLM")
            from openai import OpenAI

            self._client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url,
                                  timeout=cfg.timeout, max_retries=0)

    # ── 原始 chat ────────────────────────────────────────────────────────────

    def chat(self, messages: list[dict], *, kind: str = "chat") -> tuple[str, Usage]:
        """一次 chat 补全，传输错误时重试。返回 (text, usage)。

        ``usage`` 覆盖**这一次**逻辑调用（含其重试），并镜像到
        ``self.last_call``。如果这里返回累计的 ``self.usage``，决策日志里
        的每步数字会逐步递增，对排查问题的人是误导。
        """
        call = Usage()
        last: Exception | None = None

        self._call_seq += 1
        call_id = self._call_seq
        if self._log and self._log.debug:
            # 原始 base64 已单独保存为截图；请求文件保留全部文本和图片位置，
            # 避免把数 MB 的编码重复写入 JSONL/文本，仍可逐条核对模型看到了什么。
            def readable(value):
                if isinstance(value, list):
                    return [readable(item) for item in value]
                if isinstance(value, dict):
                    return {k: readable(v) for k, v in value.items()}
                if isinstance(value, str) and value.startswith("data:image/"):
                    return f"<image data URL omitted; {len(value)} chars; see step image artifact>"
                return value

            payload = {"model": self.cfg.model, "temperature": self.cfg.temperature,
                       "max_tokens": self.cfg.max_tokens, "kind": kind,
                       "messages": readable(messages)}
            self._log.trace_text("glm_request", json.dumps(payload, ensure_ascii=False, indent=2),
                                 call_id=call_id, request_kind=kind)

        for attempt in range(self.cfg.max_retries + 1):
            started = time.perf_counter()
            delta = Usage(calls=1, retries=int(attempt > 0))
            final_usage = None
            error = None
            if self._log:
                self._log.phase("model", "正在请求模型", state="start", call_id=call_id,
                                attempt=attempt + 1, request_kind=kind)
            try:
                extra: dict = {}
                effort = getattr(self.cfg, "reasoning_effort", "")
                if effort:
                    extra["reasoning_effort"] = effort
                # Keep streaming: long generations may exceed a gateway's non-streaming idle window.
                # SDK retries are disabled; each attempt is settled exactly once below.
                resp = self._client.chat.completions.create(
                    model=self.cfg.model, messages=messages,
                    temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                    stream=True, stream_options={"include_usage": True}, extra_body=extra,
                )
                content_parts: list[str] = []
                last_progress = started
                for ev in resp:
                    if getattr(ev, "usage", None):
                        final_usage = ev.usage
                    if ev.choices:
                        piece = getattr(ev.choices[0].delta, "content", None)
                        if piece:
                            content_parts.append(piece)
                    now = time.perf_counter()
                    if self._log and now - last_progress >= 5:
                        self._log.phase("model", "模型仍在生成，请稍候", state="progress",
                                        call_id=call_id, attempt=attempt + 1, request_kind=kind,
                                        pending_ms=round((now - started) * 1000, 1))
                        last_progress = now
                content = "".join(content_parts).strip()
                if self._log and self._log.debug:
                    self._log.trace_text("glm_response", content, call_id=call_id, request_kind=kind,
                                         attempt=attempt + 1,
                                         latency_ms=round((time.perf_counter() - started) * 1000, 1),
                                         in_tok=int(getattr(final_usage, "prompt_tokens", 0) or 0),
                                         out_tok=int(getattr(final_usage, "completion_tokens", 0) or 0))
                if not content:
                    raise GLMError("empty completion")
            except (KeyboardInterrupt, SystemExit) as exc:
                error = exc
                raise
            except Exception as exc:
                last = error = exc
                detail = str(exc)
                if self.cfg.api_key:
                    detail = detail.replace(self.cfg.api_key, "<redacted-api-key>")
                if self._log and self._log.debug:
                    self._log.trace_text("glm_error", detail, call_id=call_id,
                                         request_kind=kind, attempt=attempt + 1,
                                         error_type=type(exc).__name__,
                                         status_code=getattr(exc, "status_code", None))
            finally:
                delta.latency_ms = (time.perf_counter() - started) * 1000
                delta.usage_complete = final_usage is not None
                delta.usage_reports = int(final_usage is not None)
                if final_usage:
                    delta.prompt_tokens = int(final_usage.prompt_tokens or 0)
                    delta.completion_tokens = int(final_usage.completion_tokens or 0)
                call.merge(delta)
                self.usage.merge(delta)
                self.last_call = call
                if self._log:
                    self._log.phase("model", "模型请求失败" if error else "模型请求完成",
                                    state="end", call_id=call_id, attempt=attempt + 1,
                                    request_kind=kind, status="error" if error else "ok",
                                    error_type=type(error).__name__ if error else None,
                                    latency_ms=round(delta.latency_ms, 1),
                                    tokens={"input": delta.prompt_tokens, "output": delta.completion_tokens},
                                    usage_known=final_usage is not None, usage_total=self.usage.as_dict())
            if error is None:
                return content, call
            if attempt < self.cfg.max_retries and _is_retryable(error):
                wait = min(8.0, 1.0 * (2 ** attempt))
                if self._log:
                    self._log.phase("model", f"请求失败，{wait:g} 秒后重试", state="retry",
                                    call_id=call_id, attempt=attempt + 1, request_kind=kind,
                                    retry_wait_ms=wait * 1000)
                time.sleep(wait)
                continue
            break
        self.last_call = call
        raise GLMError(f"GLM call failed after {self.cfg.max_retries + 1} attempts: {last}")

    def chat_json(self, messages: list[dict], *, kind: str = "chat") -> dict:
        """chat 后按 JSON 解析；解析失败时带着抱怨重问一次。

        多一次往返比下游浪费一步便宜，也让畸形输出处理留在图之外。
        ``last_call`` 累加两次尝试，所以决策日志的每步 token 数是真实成本。
        """
        text, first = self.chat(messages, kind=kind)
        try:
            return prompts.first_json_object(text)
        except ValueError as e:
            self.usage.retries += 1
            retry = messages + [
                {"role": "assistant", "content": text[:1500]},
                {"role": "user", "content": prompts.render(
                    "json_retry_user", error=e, output_rule=prompts.OUTPUT_RULE,
                )},
            ]
            text2, second = self.chat(retry, kind=f"{kind}_json_retry")
            merged = Usage(
                calls=first.calls + second.calls,
                prompt_tokens=first.prompt_tokens + second.prompt_tokens,
                completion_tokens=first.completion_tokens + second.completion_tokens,
                latency_ms=first.latency_ms + second.latency_ms,
                retries=first.retries + second.retries + 1,
                usage_complete=first.usage_complete and second.usage_complete,
                usage_reports=first.usage_reports + second.usage_reports,
            )
            self.last_call = merged
            return prompts.first_json_object(text2)

    # ── 高层：decide ─────────────────────────────────────────────────────────

    def decide(
        self,
        context_text: str,
        *,
        jpeg_b64: str | None = None,
        screen_size: tuple[int, int] | None = None,
        image_size: tuple[int, int] | None = None,
    ) -> tuple[dict, Usage]:
        """请求下一个动作。返回归一化后的动作 dict。

        即使 JSON 重试后动作仍不可用时抛 ``ValueError``——调用方（图）
        将其视为决策失败，降级路由 tree->vlm 或计一次无进展步，
        绝不崩溃。
        """
        messages = prompts.build_decide_messages(
            context_text, jpeg_b64, screen=screen_size, image_size=image_size
        )
        raw = self.chat_json(messages, kind="decide")
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
        text, _ = self.chat(prompts.build_reflect_messages(action_desc, goal, before_b64, after_b64),
                            kind="reflect")
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
