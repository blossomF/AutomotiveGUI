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
        (0, 1), (0, 2), (1, 1), (1, 2), (2, 1), (2, 2)]
    assert detail["stats"]["avg_step"] == 12.3
    assert dashboard.get_live(stem)["last"]["action"]["action"] == "finish"


def test_unfinished_legacy_task_counts_graph_steps_and_recorded_time(tmp_path):
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
        "unknown", 1, 8.0)


def test_artifact_links_open_local_files_and_reject_traversal(tmp_path):
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request

    import pytest

    dashboard = _dashboard(tmp_path)
    stem = "中文任务-20261016-160000"
    events = [{"t": 1, "step": 1, "phase": "decide", "msg": "选择下一步"}]
    (tmp_path / f"{stem}.jsonl").write_text(json.dumps(events[0]), encoding="utf-8")
    folder = tmp_path / stem
    folder.mkdir()
    files = {
        "step01_0001_xml.xml": "<hierarchy>原始 XML</hierarchy>",
        "step01_candidates.txt": "#1 搜索（裁剪结果）",
        "step01_0002_glm_request.json": '{"messages": []}',
        # Invalid JSON is still viewable as the exact original response.
        "step01_0003_glm_response.json": "原始模型响应",
    }
    for name, content in files.items():
        (folder / name).write_text(content, encoding="utf-8")
    live = dashboard.get_live(stem, -1)
    assert live["updates"][0]["action"]["candidates_artifact"] == "step01_candidates.txt"
    # Enrichment must not mutate the cached projection.
    assert "candidates_artifact" not in dashboard.get_task_detail(stem)["steps"][0]["action"]

    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/artifact?"
    try:
        for name, content in files.items():
            with urllib.request.urlopen(base + urllib.parse.urlencode({"task": stem, "file": name})) as response:
                assert response.headers.get_content_type() == "text/plain"
                assert response.read().decode("utf-8") == content
        for name in ("../config.yaml", "..\\config.json", "secret.yaml", "missing.txt", "step01_candidates.txt:other.txt"):
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + urllib.parse.urlencode({"task": stem, "file": name}))
            assert error.value.code == 404
        assert dashboard.task_artifact("../other", "step01_candidates.txt") is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
