"""GLM 5.3 client over the OpenAI-compatible protocol.

Replaces ``LightGUIAgent/lightguiagent/claude_client.py``, which is bound to the
``anthropic`` SDK (``from anthropic import Anthropic`` at :11, ``client.messages.create``
at :270). The swap is not just a constructor change — three concrete differences:

  * image payload: Anthropic's ``{type:"image", source:{media_type, data}}`` becomes
    OpenAI's ``{type:"image_url", image_url:{url:"data:image/...;base64,..."}}``.
    ``prompts.py`` already emits the OpenAI shape.
  * usage fields: ``usage.input_tokens``/``output_tokens`` -> ``prompt_tokens``/
    ``completion_tokens``.
  * pricing: ``config.py:162-165`` hardcodes $5/$25 per million and reports USD.
    GLM runs on a committee-issued quota, so a dollar figure is meaningless here —
    we report token counts and call counts only.

We use ``openai`` directly rather than ``langchain-openai``: one fewer runtime
dependency, and we need no chain composition (LangGraph owns the control flow).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from autogui.actions import parse_action
from autogui.brain import prompts
from autogui.config import GLMConfig


class GLMError(RuntimeError):
    """The model could not be reached or never produced a usable answer."""


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
    """Thin, JSON-strict chat wrapper with usage accounting."""

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

    # ── raw chat ─────────────────────────────────────────────────────────────

    def chat(self, messages: list[dict]) -> tuple[str, Usage]:
        """One chat completion, retried on transport errors. Returns (text, usage).

        ``usage`` covers **this** logical call including its retries, and is mirrored
        onto ``self.last_call``. Returning the cumulative ``self.usage`` here instead
        would make the per-step numbers in the decision log grow every step and read
        as nonsense to whoever is debugging.
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
            except Exception as e:  # transport / auth / schema drift
                last = e
                if attempt < self.cfg.max_retries:
                    time.sleep(1.0 + attempt)
        self.last_call = call
        raise GLMError(f"GLM call failed after {self.cfg.max_retries + 1} attempts: {last}")

    def chat_json(self, messages: list[dict]) -> dict:
        """Chat then parse as JSON; on a parse failure, re-ask once with the complaint.

        The extra round trip is cheaper than a wasted step downstream, and it keeps
        malformed-output handling out of the graph. ``last_call`` accumulates both
        attempts so the decision log's per-step token figure is the true cost.
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

    # ── high-level: decide ───────────────────────────────────────────────────

    def decide(
        self,
        context_text: str,
        *,
        jpeg_b64: str | None = None,
        grid_spec: str = "20x10",
    ) -> tuple[dict, Usage]:
        """Ask for the next action. Returns a normalised action dict.

        Raises ``ValueError`` if even after the JSON retry the action is unusable —
        the caller (graph) treats that as a decision failure and degrades route
        tree->vlm or counts a no-progress step, never crashes the run.
        """
        messages = prompts.build_decide_messages(context_text, jpeg_b64, grid_spec)
        raw = self.chat_json(messages)
        action = parse_action(raw)
        progress = str(raw.get("progress", ""))[:300]
        entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
        action["_progress"] = progress
        action["_entities"] = _clean_entities(entities)
        return action, self.last_call

    # ── high-level: reflect ──────────────────────────────────────────────────

    def reflect(self, action_desc: str, goal: str, before_b64: str, after_b64: str) -> str:
        """A / B / C / UNKNOWN. See ``prompts.parse_reflection`` for why UNKNOWN exists."""
        text, _ = self.chat(prompts.build_reflect_messages(action_desc, goal, before_b64, after_b64))
        return prompts.parse_reflection(text)


def _clean_entities(raw: dict) -> dict:
    """Keep entities small and stringly — they live in every future prompt."""
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
