from types import SimpleNamespace

from autogui.actions import parse_action
from autogui.brain.context import Context
from autogui.config import AppConfig, Config
from autogui.core.graph import Runner
from autogui.perception.capture import Observation
from autogui.perception.tree import Candidate


OLD = Candidate(7, "跳过 5s", 980, 80, (920, 40, 1040, 120), True,
                "skip", "android.widget.Button")


def _runner(monkeypatch, fresh_xml: str):
    runner = Runner(cfg=Config(), task="QQ音乐播放歌曲")
    runner.obs = Observation(b"", "<hierarchy/>", 1080, 2400, "old", [OLD])
    calls = []
    runner.executor = SimpleNamespace(call=lambda name, **kw: calls.append((name, kw)) or "OK")
    runner.log = SimpleNamespace(phase=lambda *args, **kw: None, debug=False)
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: SimpleNamespace(dump_xml=lambda: fresh_xml))
    monkeypatch.setattr("autogui.core.graph.time.sleep", lambda seconds: None)
    return runner, calls


def _xml(text: str, bounds="[920,40][1040,120]") -> str:
    return (f'<hierarchy><node class="android.widget.Button" '
            f'resource-id="qq:id/skip" text="{text}" clickable="true" '
            f'enabled="true" bounds="{bounds}" /></hierarchy>')


def test_skip_disappeared_does_not_tap_underlying_page(monkeypatch):
    runner, calls = _runner(monkeypatch, "<hierarchy/>")
    result = runner.act({"step": 1, "route": "tree", "action": {"type": "CLICK", "node_ref": 7}})
    assert result["last_action"] == {}
    assert calls == []


def test_skip_uses_fresh_xml_position(monkeypatch):
    runner, calls = _runner(monkeypatch, _xml("跳过", "[820,50][940,130]"))
    result = runner.act({"step": 1, "route": "tree", "action": {"type": "CLICK", "node_ref": 7}})
    # The previous observation had a countdown. Even though this fresh label
    # lost the number, treat it as the same short-lived ad and wait.
    assert result["last_action"] == {}
    assert calls == []

    runner.obs.candidates[0].label = "跳过"
    result = runner.act({"step": 2, "route": "tree", "action": {"type": "CLICK", "node_ref": 7}})
    assert result["last_action"]["coord"] == (880, 90)
    assert calls == [("tap", {"x": 880, "y": 90})]


def test_countdown_skip_waits_instead_of_tapping(monkeypatch):
    runner, calls = _runner(monkeypatch, _xml("跳过 2s"))
    waits = []
    monkeypatch.setattr("autogui.core.graph.time.sleep", waits.append)
    result = runner.act({"step": 1, "route": "tree", "action": {"type": "CLICK", "node_ref": 7}})
    assert result["last_action"] == {}
    assert waits == [2.3]
    assert calls == []


def test_visual_skip_waits_without_stale_point_tap(monkeypatch):
    runner, calls = _runner(monkeypatch, "<hierarchy/>")
    waits = []
    monkeypatch.setattr("autogui.core.graph.time.sleep", waits.append)
    action = parse_action({"type": "CLICK", "point": [80, 90], "target_label": "跳过"})
    result = runner.act({"step": 1, "route": "vlm", "action": action})
    assert result["last_action"] == {}
    assert waits == [2.0]
    assert calls == []


def test_regular_click_does_not_recheck_or_wait(monkeypatch):
    runner, calls = _runner(monkeypatch, "<hierarchy/>")
    action = {"type": "CLICK", "point": [80, 90], "image_size": (1080, 2400)}
    result = runner.act({"step": 1, "route": "vlm", "action": action})
    assert result["last_action"] == action
    assert calls == [("tap", {"x": 80, "y": 90})]


def test_launch_note_is_in_trimmed_context_and_expires_after_two_decisions(monkeypatch):
    runner, _ = _runner(monkeypatch, "<hierarchy/>")
    runner.screen = SimpleNamespace(width=1080, height=2400)
    runner.glm = SimpleNamespace(decide=lambda *args, **kw: (
        {"type": "WAIT", "seconds": 1}, SimpleNamespace(calls=1, prompt_tokens=1, completion_tokens=1)))
    runner._note_launch("QQ音乐")
    text, _, _ = runner.context.trim([])
    assert "倒计时广告" in text and "QQ音乐" in text
    runner.decide({"step": 1, "route": "tree"})
    assert runner.context.launch_note == "QQ音乐"
    runner.decide({"step": 1, "route": "tree"})
    assert runner.context.launch_note == ""


def test_launch_note_not_added_to_unrelated_context():
    assert "应用刚启动" not in Context(task="普通任务").permanent_block()


def test_all_three_launch_paths_refresh_short_lived_note(monkeypatch):
    runner, calls = _runner(monkeypatch, "<hierarchy/>")
    assert runner._launch_selected_app(AppConfig(name="QQ音乐", package="com.qq.music"))
    assert runner.context.launch_note == "QQ音乐"

    runner.context.launch_note = ""
    runner._dispatch({"type": "LAUNCH", "app": "com.qq.music"})
    assert runner.context.launch_note == "com.qq.music"

    runner.context.launch_note = ""
    monkeypatch.setattr(runner, "_foreground_package", lambda: "com.qq.music")
    runner.recovery({"step": 2, "recover_depth": 3})
    assert runner.context.launch_note == "com.qq.music"
    assert [name for name, _ in calls] == ["launch_app"] * 3
