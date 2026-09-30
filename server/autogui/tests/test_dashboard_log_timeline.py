"""Dashboard summaries use graph steps and elapsed time from DecisionLog JSONL."""

import importlib.util
import json
from pathlib import Path


def _dashboard(tmp_path):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "dashboard_log_timeline_test", root / "dist" / "dashboard_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LOGS_ROOT = tmp_path
    return module


def test_old_finish_event_uses_t_and_groups_events_by_graph_step(tmp_path):
    dashboard = _dashboard(tmp_path)
    stem = "播放歌曲-20260929-203722"
    events = [
        {"t": 1.0, "step": 0, "phase": "glm_request", "msg": "initial"},
        {"t": 5.0, "step": 0, "phase": "glm_response", "msg": "route"},
        {"t": 9.0, "step": 1, "phase": "xml", "msg": "dump"},
        {"t": 14.0, "step": 1, "phase": "decide", "msg": "click"},
        {"t": 22.4, "step": 2, "phase": "verify", "msg": "done"},
        {"t": 24.6, "phase": "finish", "msg": "complete", "success": True,
         "steps": 2, "usage": {"total_tokens": 123}},
    ]
    (tmp_path / f"{stem}.jsonl").write_text(
        "\n".join(json.dumps(item) for item in events), encoding="utf-8")

    summary = dashboard.list_tasks()[0]
    detail = dashboard.get_task_detail(stem)
    assert summary["total_time"] == detail["stats"]["total_time"] == 24.6
    assert summary["total_steps"] == detail["stats"]["total_steps"] == 2
    assert [(s["step"], s["substep"]) for s in detail["steps"]] == [
        (0, 1), (0, 2), (1, 1), (1, 2), (2, 1)]
    assert detail["stats"]["avg_step"] == 12.3
    assert dashboard.get_live(stem)["last"]["substep"] == 1


def test_running_task_counts_graph_steps_and_elapsed_time(tmp_path):
    dashboard = _dashboard(tmp_path)
    stem = "进行中-20260929-203722"
    events = [
        {"t": 1, "step": 0, "phase": "setup", "msg": "ready"},
        {"t": 3, "step": 1, "phase": "xml", "msg": "dump"},
        {"t": 8, "step": 1, "phase": "decide", "msg": "click"},
    ]
    (tmp_path / f"{stem}.jsonl").write_text(
        "\n".join(json.dumps(item) for item in events), encoding="utf-8")
    summary = dashboard.list_tasks()[0]
    assert (summary["status"], summary["total_steps"], summary["total_time"]) == (
        "running", 1, 8.0)
