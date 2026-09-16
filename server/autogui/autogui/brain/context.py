"""上下文组装与 token 经济。

四层，见 技术方案评估.md §5.1：

  permanent   任务 + 声明路径 + 动作 schema + 技能索引            ~600 tok
  sliding     最近 N 步纯文本，含失败步                          ~500 tok
  entities    跨步骤关键事实（时间 / 地点 / 歌曲 / 商品）         ~150 tok
  observation 候选列表（文本）或网格图（二选一，绝不都要）        1-2k / 图

最大的杠杆是 **tree 模式从不发截图**。一张 1920x1080 带网格标注的帧
每步约耗 1.5k 视觉 token；40 行候选列表只花几百。这就是“每个 token
都挣到”的分数来源，而且 tree 模式也是*更快*的路径，所以 Context 分
和性能分方向一致。

``estimate_tokens`` 是启发式，不是真 tokenizer。只用来决定发送前裁掉
什么；对外报告的是 provider 返回的真实用量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from autogui.actions import ACTION_NAMES
from autogui.perception import tree as tree_mod

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")


def estimate_tokens(text: str) -> int:
    """GLM 家族的粗略估计：CJK ~1 token/字，ASCII ~1 token/4 字符。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


@dataclass
class Context:
    """模型为一次决策看到的全部内容（已裁剪）。"""

    task: str
    declared_path: str
    history: list[dict] = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    progress: str = ""
    skill_index: str = ""

    history_window: int = 5
    token_budget: int = 8000
    candidate_limit: int = 40

    # ── 永久块 ───────────────────────────────────────────────────────────────

    def permanent_block(self) -> str:
        lines = [
            f"任务：{self.task}",
            f"定位路径：{self.declared_path}（tree=控件树优先，vlm=网格兜底）",
            f"动作类型：{', '.join(ACTION_NAMES)}",
        ]
        if self.skill_index:
            lines.append("可用技能：\n" + self.skill_index)
        return "\n".join(lines)

    # ── 滑动块 ───────────────────────────────────────────────────────────────

    def history_block(self) -> str:
        """最近 ``history_window`` 步，每步一行。

        失败步保留可见。``Mobile-Agent-v2/run.py:401-424`` 只在反思回答
        A 时才追加历史，失败尝试会从记录中消失，B/C 之后
        ``completed_requirements`` 就会过期——模型随后会复用一个不再
        描述现实的计划。我们保留该步并打上标记。
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
        # 排序使序列化稳定，对 prompt 前缀缓存友好。
        body = "\n".join(f"{k}={self.entities[k]}" for k in sorted(self.entities))
        return "已确认的关键信息（后续步骤直接使用，不要重新推断）：\n" + body

    def observation_text(self, cands: list[tree_mod.Candidate]) -> str:
        return tree_mod.render_candidates(cands) if cands else "TREE_EMPTY"

    # ── 预算约束 ─────────────────────────────────────────────────────────────

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
        """返回 (context_text, 可能被截短的候选, token 计数)。

        超预算时的牺牲顺序：先最旧的历史行，再尾部候选。
        实体和永久块永不丢弃——丢了任务或记住的会议时间，
        比丢一行菜单严重。
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

    # ── 更新 ─────────────────────────────────────────────────────────────────

    def record(self, step: int, route: str, action: str, explain: str, detail: str = "", failed: bool = False) -> None:
        self.history.append(
            {"step": step, "route": route, "action": action, "explain": explain[:120], "detail": detail[:80], "failed": failed}
        )

    def merge_entities(self, new: dict) -> None:
        """模型提出的实体只做合并，不允许悄悄删除已有项。"""
        for k, v in (new or {}).items():
            if v in (None, "", "None"):
                continue
            self.entities[str(k)[:24]] = str(v)[:60]
