"""决策链日志（竞赛规则 4.7——硬性要求，不是加分装饰）。

规则 4.7 要求推理链实时打印在 PC 屏幕上。它同时也是三项得分的
证据基础：评委读它给 Harness 设计打分，其时间列是性能分的证明，
其 token 列是 Context 分的证明。所以它被建成一等组件，而不是
散落在图各处的 ``print`` 调用。

结构沿用 ``LightGUIAgent/lightguiagent/logger.py`` 的 JSONL 方式，
并增加我们架构特别需要的字段：``route``（本步由 skill/tree/vlm
哪条路由服务）、``sig``（屏幕指纹）、``vlm_calls`` 和 ``tokens``。

两个输出，始终同步：
  * stdout 上的人类可读行，阶段列做补宽，便于纵向扫读决策链
  * 每个事件一条 JSONL 记录，写入 ``artifacts/logs/<task>-<ts>.jsonl``
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
    "perceive": "36",   # 青
    "route": "35",      # 紫红
    "decide": "33",     # 黄
    "act": "32",        # 绿
    "verify": "34",     # 蓝
    "recovery": "31",   # 红
    "guard": "90",      # 灰
    "finish": "1",
    "skill": "32",
    "error": "41",
}


class DecisionLog:
    def __init__(self, task: str, artifacts_dir: Path | str, *, verbose: bool = True, debug: bool = False):
        self.task = task
        self.started = time.time()
        self.verbose = verbose
        self.debug = debug
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in task[:24] if ch.isalnum() or ch in "-_ ") or "task"
        self.dir = Path(artifacts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

        # debug模式下创建任务子目录
        if self.debug:
            self.debug_dir = self.dir / f"{safe}-{stamp}"
            self.debug_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.debug_dir = None

        self.path = self.dir / f"{safe}-{stamp}.jsonl"
        self.events: list[dict] = []
        self.step = 0
        self._artifact_seq = 0
        self._fh = None
        try:
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError:  # artifacts 目录只读；继续运行，仍打印到控制台
            self._fh = None

    # ── 输出 ─────────────────────────────────────────────────────────────────

    def phase(self, phase: str, message: str, *, step: int | None = None, **metrics) -> None:
        """输出一行：``[Step 3] [decide] 点击搜索框 (glms=1 in=3120 out=96)``。"""
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

    # ── 持久化 ───────────────────────────────────────────────────────────────

    def _write_jsonl(self) -> None:
        if not self._fh:
            return
        try:
            self._fh.write(json.dumps(self.events[-1], ensure_ascii=False) + "\n")
            self._fh.flush()  # 每事件一次 flush：演示中途崩溃不能丢决策链
        except OSError:
            pass

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    # ── Debug落盘 ─────────────────────────────────────────────────────────────

    def save_image(self, name: str, data: bytes, *, extension: str = "png") -> Path | None:
        """落盘图像（PNG/JPEG），仅debug模式。返回文件路径。"""
        if not self.debug or not hasattr(self, 'debug_dir') or not self.debug_dir or not data:
            return None
        try:
            path = self.debug_dir / f"{name}.{extension}"
            path.write_bytes(data)
            return path
        except OSError:
            return None

    def save_text(self, name: str, content: str) -> Path | None:
        """落盘文本文件，仅debug模式。返回文件路径。"""
        if not self.debug or not hasattr(self, 'debug_dir') or not self.debug_dir:
            return None
        try:
            path = self.debug_dir / f"{name}.txt"
            path.write_text(content, encoding='utf-8')
            return path
        except OSError:
            return None

    def trace_text(self, kind: str, content: str, **metrics) -> Path | None:
        """保存一次完整文本交互，并在 JSONL 中记录可关联的文件名。"""
        if not self.debug:
            return None
        self._artifact_seq += 1
        name = f"step{self.step:02d}_{self._artifact_seq:04d}_{kind}"
        path = self.save_text(name, content)
        self.phase(kind, f"{kind} → {path.name if path else '保存失败'}",
                   artifact=path.name if path else "", chars=len(content), **metrics)
        return path

    def detail(self, phase: str, message: str) -> None:
        """细粒度日志（仅debug模式输出到控制台，不写JSONL）。"""
        if not self.debug or not self.verbose:
            return
        print(f"  [DEBUG {phase}] {message}")

    # ── 收尾汇总 ─────────────────────────────────────────────────────────────

    def summary(self, *, success: bool, status: str, steps: int, usage: dict, route_counts: dict,
                vlm_reflections: int, elapsed: float) -> str:
        """评委和验收文档共同阅读的汇总表。

        ``avg_vlm_per_step`` 是整场运行中最值得引用的数字：设计目标
        <=1（Mobile-Agent-v2 每步约 4 次），就在这里展示它。
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
