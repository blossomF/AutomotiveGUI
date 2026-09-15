"""Context assembly and token economy.

Four layers, per 技术方案评估.md §5.1:

  permanent   task + declared path + action schema + skill index      ~600 tok
  sliding     last N steps as plain text, failures included           ~500 tok
  entities    cross-step key facts (time / place / song / item)       ~150 tok
  observation candidate list (text) OR grid image (never both)        1-2k / image

The single biggest lever is that **tree mode never sends a screenshot**. An
annotated 1920x1080 frame costs on the order of 1.5k visual tokens every step; a
40-line candidate list costs a few hundred. That is where the "every token earns
its keep" score comes from, and it also makes tree mode the *faster* route, so the
Context and performance points pull in the same direction.

``estimate_tokens`` is a heuristic, not a tokenizer. We only use it to decide what
to drop before sending, and we report the provider's real count afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from autogui.actions import ACTION_NAMES
from autogui.perception import tree as tree_mod

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")


def estimate_tokens(text: str) -> int:
    """Rough GLM-family estimate: CJK ~1 token/char, ASCII ~1 token/4 chars."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


@dataclass
class Context:
    """Everything the model sees for one decision, already trimmed."""

    task: str
    declared_path: str
    history: list[dict] = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    progress: str = ""
    skill_index: str = ""

    history_window: int = 5
    token_budget: int = 8000
    candidate_limit: int = 40

    # ── permanent block ──────────────────────────────────────────────────────

    def permanent_block(self) -> str:
        lines = [
            f"任务：{self.task}",
            f"定位路径：{self.declared_path}（tree=控件树优先，vlm=网格兜底）",
            f"动作类型：{', '.join(ACTION_NAMES)}",
        ]
        if self.skill_index:
            lines.append("可用技能：\n" + self.skill_index)
        return "\n".join(lines)

    # ── sliding block ────────────────────────────────────────────────────────

    def history_block(self) -> str:
        """Last ``history_window`` steps, one line each.

        Failed steps stay visible. ``Mobile-Agent-v2/run.py:401-424`` only appends
        history on reflection answer A, so a failed attempt vanishes from the record
        and ``completed_requirements`` goes stale after a B/C — the model then reuses
        a plan that no longer describes reality. We keep the step and mark it.
        """
        recent = self.history[-self.history_window:] if self.history else []
        if not recent:
            return "（第一步，尚无历史）"
        out = []
        for h in recent:
            flag = "✗" if h.get("failed") else "✓"
            detail = h.get("detail", "")
            out.append(f"{h.get('step', '?')}.{flag} [{h.get('route', '?')}] {h.get('action', '?')} {detail} — {h.get('explain', '')}".rstrip())
        return "\n".join(out)

    def entities_block(self) -> str:
        if not self.entities:
            return ""
        # Sorted so the serialisation is stable and prompt-prefix-caching friendly.
        body = "\n".join(f"{k}={self.entities[k]}" for k in sorted(self.entities))
        return "已确认的关键信息（后续步骤直接使用，不要重新推断）：\n" + body

    def observation_text(self, cands: list[tree_mod.Candidate]) -> str:
        return tree_mod.render_candidates(cands) if cands else "TREE_EMPTY"

    # ── budget enforcement ───────────────────────────────────────────────────

    def measure(self, cands: list[tree_mod.Candidate] | None = None) -> dict:
        parts = {
            "permanent": self.permanent_block(),
            "history": self.history_block(),
            "entities": self.entities_block(),
            "observation": self.observation_text(cands or []),
        }
        counts = {k: estimate_tokens(v) for k, v in parts.items()}
        counts["total"] = sum(counts.values())
        return counts

    def trim(self, cands: list[tree_mod.Candidate]) -> tuple[str, list[tree_mod.Candidate], dict]:
        """Return (context_text, possibly-shortened candidates, token counts).

        Order of sacrifice under pressure: oldest history line, then tail candidates.
        Entities and the permanent block are never dropped — losing the task or the
        remembered meeting time is worse than losing one menu row.
        """
        kept = list(cands)
        window = self.history_window

        def build(w: int, k: int) -> tuple[str, dict]:
            c = Context(
                task=self.task, declared_path=self.declared_path, history=self.history,
                entities=self.entities, skill_index=self.skill_index,
                history_window=w, candidate_limit=k,
            )
            body = kept[:k]
            text = "\n\n".join(p for p in (
                c.permanent_block(),
                (c.entities_block() + "\n\n") if c.entities else "",
                "历史：\n" + c.history_block(),
                f"进度：{self.progress}" if self.progress else "",
                "当前屏幕可点击候选（#编号 = node_ref，回复时用 node_ref，不要复制坐标）：\n" + c.observation_text(body),
            ) if p)
            return text, estimate_tokens(text)

        text, tokens = build(window, len(kept))
        dropped_any = False
        while tokens > self.token_budget and (window > 1 or len(kept) > 6):
            dropped_any = True
            if window > 1:
                window -= 1
            else:
                kept = kept[: max(6, len(kept) - 6)]
            text, tokens = build(window, len(kept))

        if dropped_any:
            text += "\n\n（上下文因预算已裁剪：保留最近 %d 步、%d 个候选）" % (window, len(kept))
        return text, kept, {"approx_input": tokens, "trimmed": dropped_any}

    # ── updates ──────────────────────────────────────────────────────────────

    def record(self, step: int, route: str, action: str, explain: str, detail: str = "", failed: bool = False) -> None:
        self.history.append(
            {"step": step, "route": route, "action": action, "explain": explain[:120], "detail": detail[:80], "failed": failed}
        )

    def merge_entities(self, new: dict) -> None:
        """Model-proposed entities are merged, never allowed to delete silently."""
        for k, v in (new or {}).items():
            if v in (None, "", "None"):
                continue
            self.entities[str(k)[:24]] = str(v)[:60]
