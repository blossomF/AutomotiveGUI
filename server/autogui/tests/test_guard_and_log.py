"""Harness：三个硬性上限 + 决策链日志。"""

import json

import pytest

from autogui.core import guard
from autogui.core.decision_log import DecisionLog

LIMITS = guard.GuardLimits(max_steps=20, no_progress_limit=3, wall_clock_limit=180)


# ── 守卫（LightGUIAgent 一个都没有；见 core/guard.py docstring） ────────────────


def test_running_under_all_limits_continues():
    assert not guard.check(step=5, no_progress=1, elapsed=30, limits=LIMITS).stop


def test_step_ceiling():
    v = guard.check(step=21, no_progress=0, elapsed=10, limits=LIMITS)
    assert v.stop and v.stop_reason == "max_steps"


def test_no_progress_ceiling_fires_before_step_ceiling():
    """原地打转必须在第 4/20 步就停下来。"""
    v = guard.check(step=4, no_progress=3, elapsed=12, limits=LIMITS)
    assert v.stop and v.stop_reason == "no_progress"


def test_wall_clock_is_the_last_resort():
    v = guard.check(step=2, no_progress=0, elapsed=200, limits=LIMITS)
    assert v.stop and v.stop_reason == "timeout"


def test_timeout_reported_ahead_of_max_steps():
    """先报告最具可操作性的原因：烧完时钟才解释得了为什么步数耗尽。"""
    v = guard.check(step=25, no_progress=5, elapsed=999, limits=LIMITS)
    assert v.stop_reason == "timeout"


def test_no_progress_exactly_at_limit():
    assert guard.check(1, 2, 5, LIMITS).stop is False
    assert guard.check(1, 3, 5, LIMITS).stop is True


# ── 决策日志（竞赛规则 4.7） ────────────────────────────────────────────────────


@pytest.fixture
def log(tmp_path) -> DecisionLog:
    return DecisionLog("在音乐App播放七里香", tmp_path, verbose=False)


def test_log_writes_one_json_object_per_line(log, tmp_path):
    log.step_begin(1)
    log.phase("perceive", "1920x1080", shot_ms=210, dump_ms=900, sig="abc123")
    log.phase("route", "tree")
    log.close()
    lines = log.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["phase"] == "perceive" and first["sig"] == "abc123" and first["step"] == 1
    assert json.loads(lines[1])["phase"] == "route"


def test_log_flushes_per_event_so_a_crash_keeps_the_chain(log):
    """进程在演示中途死掉时，文件也必须可读。"""
    log.step_begin(1)
    log.phase("act", "tap(960,86)")
    text = log.path.read_text(encoding="utf-8")   # 尚未调用 close()
    assert text.strip() and json.loads(text.strip().splitlines()[-1])["phase"] == "act"


def test_summary_reports_model_calls_per_step(log):
    out = log.summary(
        success=True, status="complete", steps=6,
        usage={"calls": 5, "prompt_tokens": 9000, "completion_tokens": 400, "total_tokens": 9400, "retries": 1},
        route_counts={"tree": 4, "vlm": 1, "skill": 1},
        vlm_reflections=1, elapsed=42.0,
    )
    assert "0.83" in out          # 5 次调用 / 6 步——展示 ≤1 目标
    assert "9,400" in out
    assert "tree=4" in out and "vlm=1" in out
    assert "平均每步模型调用" in out
    assert "平均每步 VLM" not in out


def test_summary_survives_zero_steps(log):
    out = log.summary(success=False, status="max_steps", steps=0, usage={}, route_counts={},
                      vlm_reflections=0, elapsed=1.0)
    assert "步数" in out
