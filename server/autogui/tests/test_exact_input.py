from types import SimpleNamespace

import pytest

from autogui.brain import prompts
from autogui.bridge.input import TextInput, UnicodeInputUnavailable
from autogui.config import Config
from autogui.core.graph import Runner


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


def test_app_cache_is_not_described_as_installed_app_catalog():
    assert "不是设备应用全集" in prompts.DECIDE_SYSTEM
    assert "HOME" in prompts.DECIDE_SYSTEM
