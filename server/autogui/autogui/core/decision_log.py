"""The decision-chain log (contest rule 4.7 — a hard requirement, not a nicety).

Rule 4.7 asks for the reasoning chain to be printed live on the PC screen. It is
also the evidence base for three scored items at once: the judges read it to grade
Harness design, its timing column is the proof for the performance points, and its
token column is the proof for Context. So it is built as a first-class component
rather than ``print`` calls scattered through the graph.

Structure follows ``LightGUIAgent/lightguiagent/logger.py``'s JSONL approach, with
fields added that our architecture specifically needs: ``route`` (which of
skill/tree/vlm served this step), ``sig`` (screen fingerprint), ``vlm_calls`` and
``tokens``.

Two outputs, always in sync:
  * human line on stdout, with the phase column padded so the chain scans vertically
  * one JSONL record per event under ``artifacts/logs/<task>-<ts>.jsonl``
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_PHASE_COLOR = {
    "perceive": "36",   # cyan
    "route": "35",      # magenta
    "decide": "33",     # yellow
    "act": "32",        # green
    "verify": "34",     # blue
    "recovery": "31",   # red
    "guard": "90",      # grey
    "finish": "1",
    "skill": "32",
    "error": "41",
}


class DecisionLog:
    def __init__(self, task: str, artifacts_dir: Path | str, *, verbose: bool = True):
        self.task = task
        self.started = time.time()
        self.verbose = verbose
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in task[:24] if ch.isalnum() or ch in "-_ ") or "task"
        self.dir = Path(artifacts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{safe}-{stamp}.jsonl"
        self.events: list[dict] = []
        self.step = 0
        self._fh = None
        try:
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError:  # read-only artifacts dir; keep running, we still print
            self._fh = None

    # ── output ───────────────────────────────────────────────────────────────

    def phase(self, phase: str, message: str, *, step: int | None = None, **metrics) -> None:
        """Emit one line: ``[Step 3] [decide] 点击搜索框 (glms=1 in=3120 out=96)``."""
        self.events.append({"t": round(time.time() - self.started, 3), "step": step if step is not None else self.step,
                            "phase": phase, "msg": message, **metrics})
        self._write_jsonl()
        if not self.verbose:
            return
        n = self.step if step is None else step
        tail = "".join(f" {k}={v}" for k, v in metrics.items() if v is not None and v != "")
        label = _c(f"{phase:<9}", _PHASE_COLOR.get(phase, "0"))
        print(f"[Step {n}] {label} {message}{tail}")

    def step_begin(self, step: int) -> None:
        self.step = step

    def banner(self, text: str) -> None:
        if self.verbose:
            print(_c(f"\n{'=' * 68}\n{text}\n{'=' * 68}", "1"))

    def kv(self, key: str, value: str) -> None:
        if self.verbose:
            print(f"  {key}: {value}")

    def warn(self, message: str) -> None:
        self.phase("error", message)

    # ── persistence ──────────────────────────────────────────────────────────

    def _write_jsonl(self) -> None:
        if not self._fh:
            return
        try:
            self._fh.write(json.dumps(self.events[-1], ensure_ascii=False) + "\n")
            self._fh.flush()  # flush per event: a crash mid-demo must not lose the chain
        except OSError:
            pass

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    # ── closing summary ──────────────────────────────────────────────────────

    def summary(self, *, success: bool, status: str, steps: int, usage: dict, route_counts: dict,
                vlm_reflections: int, elapsed: float) -> str:
        """The table the judges and our own acceptance doc both read.

        ``avg_vlm_per_step`` is the single most quotable number in the run: the
        design target is <=1 (Mobile-Agent-v2 spends ~4 per step), and this is where
        we show it.
        """
        avg = f"{elapsed / steps:.1f}" if steps else "-"
        vlm_per_step = f"{(usage.get('calls', 0) / steps):.2f}" if steps else "-"
        lines = [
            "",
            _c("─" * 68, "1"),
            _c(f"结果 {'✅ 成功' if success else '❌ 未成功'}   status={status}", "1"),
            _c("─" * 68, "1"),
            f"  步数            {steps}",
            f"  总耗时          {elapsed:.1f}s   (平均 {avg}s/步)",
            f"  GLM 调用次数    {usage.get('calls', 0)}   (反思额外 {vlm_reflections} 次)",
            f"  平均每步 VLM    {vlm_per_step}    ← 目标 ≤ 1（Mobile-Agent-v2 ≈ 4）",
            f"  token  in/out   {usage.get('prompt_tokens', 0):,} / {usage.get('completion_tokens', 0):,}"
            f"   合计 {usage.get('total_tokens', 0):,}",
            f"  JSON 重试        {usage.get('retries', 0)}",
            f"  路由分布        " + "  ".join(f"{k}={v}" for k, v in sorted(route_counts.items())) if route_counts else "  路由分布        -",
            f"  决策链日志      {self.path}",
            _c("─" * 68, "1"),
        ]
        text = "\n".join(lines)
        if self.verbose:
            print(text)
        self.events.append({"t": round(elapsed, 3), "phase": "finish", "msg": status, "success": success,
                            "steps": steps, "usage": usage, "routes": route_counts})
        self._write_jsonl()
        self.close()
        return text
