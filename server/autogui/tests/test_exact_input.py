from types import SimpleNamespace

import pytest

from autogui.brain import prompts
from autogui.bridge.input import TextInput, UnicodeInputUnavailable
from autogui.config import Config
from autogui.core.graph import Runner
from autogui.perception.capture import Observation


class FakeDevice:
    def __init__(self):
        self.calls = []

    def adb(self, *args, **kwargs):
        self.calls.append(args)

    def adb_soft(self, *args, **kwargs):
        self.calls.append(args)
        return SimpleNamespace(ok=True)


def test_ascii_channel_refuses_chinese_before_clearing_existing_field():
    device = FakeDevice()
    text_input = TextInput(device)
    text_input.strategy = "ascii"
    text_input._probed = True
    with pytest.raises(UnicodeInputUnavailable):
        text_input.type("会议", clear_first=True, delay=0)
    assert device.calls == []


def test_ascii_channel_sends_exact_ascii():
    device = FakeDevice()
    text_input = TextInput(device)
    text_input.strategy = "ascii"
    text_input._probed = True
    result = text_input.type("Meeting", delay=0)
    assert result.typed == "Meeting" and result.payload_exact
    assert device.calls == [("shell", "input", "text", "Meeting")]


def test_failed_chinese_type_forces_keyboard_screenshot_without_repeating():
    runner = Runner(cfg=Config(), task="新建会议")
    runner.executor = SimpleNamespace(call=lambda *args, **kw: (_ for _ in ()).throw(
        UnicodeInputUnavailable("会议", "仅有 ASCII 通道")))
    runner.log = SimpleNamespace(phase=lambda *args, **kw: None)
    result = runner.act({"step": 1, "route": "tree", "action": {"type": "TYPE", "text": "会议"}})
    assert result["last_action"] == {}
    assert result["no_progress"] == 0
    assert runner.pending_unicode == "会议"
    assert "不要再次 TYPE" in runner.context.permanent_block()
    assert runner.route({})["route"] == "vlm"
    assert "屏幕键盘" in prompts.render("decide_vlm_screen", grid_spec="10x20") or "看图" in prompts.render(
        "decide_vlm_screen", grid_spec="10x20")


def test_unicode_submission_requires_exact_edittext_readback(monkeypatch):
    runner = Runner(cfg=Config(), task="搜索会议")
    calls = []
    runner.executor = SimpleNamespace(call=lambda name, **kw: calls.append(name) or "OK")
    fake_xml = ('<hierarchy><node class="android.widget.EditText" text="hy" '
                'resource-id="calendar:id/title" /></hierarchy>')
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: SimpleNamespace(dump_xml=lambda: fake_xml))
    with pytest.raises(UnicodeInputUnavailable):
        runner._dispatch({"type": "TYPE", "text": "会议", "submit": True})
    assert calls == ["input_text"]  # No ENTER on an unverified value.


def test_empty_xml_after_unicode_input_uses_next_screenshot_instead_of_failing(monkeypatch):
    runner = Runner(cfg=Config(), task="搜索晴天")
    calls = []
    runner.executor = SimpleNamespace(call=lambda name, **kw: calls.append(name) or "OK")
    runner.log = SimpleNamespace(phase=lambda *args, **kw: None, debug=False)
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: SimpleNamespace(dump_xml=lambda: ""))
    monkeypatch.setattr("autogui.core.graph.time.sleep", lambda seconds: None)

    action = {"type": "TYPE", "text": "晴天", "submit": True}
    result = runner.act({"step": 2, "route": "vlm", "action": action})
    assert result["last_action"] == action
    assert runner.pending_unicode == ""
    assert calls == ["input_text"]  # Wait for visual evidence before ENTER.
    assert runner.verify({"last_action": action}) == {"recover_depth": 0, "no_progress": 0}
    assert not runner.unicode_xml_unavailable_this_step


def test_visual_completion_is_allowed_when_unicode_readback_is_unavailable(monkeypatch):
    runner = Runner(cfg=Config(), task="搜索晴天")
    runner.pending_unicode = "晴天"
    runner.context.input_guide = "等待视觉确认"
    runner.obs = Observation(b"screenshot", "", 1080, 2400, "sig", [])
    runner.grid = SimpleNamespace(cols=10, rows=20, annotate=lambda *args: object())
    runner.log = SimpleNamespace(phase=lambda *args, **kw: None, debug=False)
    runner.glm = SimpleNamespace(decide=lambda *args, **kw: (
        {"type": "COMPLETE", "result": "已显示晴天搜索结果"},
        SimpleNamespace(calls=1, prompt_tokens=10, completion_tokens=10)))
    monkeypatch.setattr("autogui.core.graph.grid_mod.encode_jpeg", lambda *args: b"jpeg")

    result = runner.decide({"step": 4, "route": "vlm"})
    assert result["action"]["type"] == "COMPLETE"
    assert runner.pending_unicode == ""
    assert runner.context.input_guide == ""
    acted = runner.act({"action": result["action"]})
    settled = runner.verify({"last_action": acted["last_action"]})
    assert settled["success"] is True and settled["status"] == "complete"


def test_app_cache_is_not_described_as_installed_app_catalog():
    assert "不是设备应用全集" in prompts.DECIDE_SYSTEM
    assert "HOME" in prompts.DECIDE_SYSTEM
