import shlex
from types import SimpleNamespace

import pytest

from autogui.brain import prompts
from autogui.actions import parse_action
from autogui.bridge.input import TextInput, UnicodeInputUnavailable
from autogui.config import Config
from autogui.core.graph import Runner
from autogui.perception.capture import Observation
from autogui.perception.tree import candidates_from_xml


class FakeDevice:
    def __init__(self):
        self.calls = []

    def adb(self, *args, **kwargs):
        self.calls.append(args)

    def adb_soft(self, *args, **kwargs):
        self.calls.append(args)
        return SimpleNamespace(ok=True)


def test_adbkeyboard_is_preferred_for_chinese_input():
    class Device:
        def adb_soft(self, *args, **kwargs):
            assert args == ("shell", "ime", "list", "-a")
            return SimpleNamespace(ok=True, stdout="com.android.adbkeyboard/.AdbIME")

    assert Config().input_chain[0] == "adbkeyboard"
    assert TextInput(Device()).probe() == "adbkeyboard"


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


def test_adbkeyboard_keeps_chinese_with_spaces_in_one_device_shell_argument():
    class Device:
        current_ime = "com.example/.Ime"
        broadcast_args = None

        def adb_soft(self, *args, **kwargs):
            if args[:4] == ("shell", "settings", "get", "secure"):
                return SimpleNamespace(ok=True, stdout=self.current_ime)
            if args[:3] == ("shell", "ime", "set"):
                self.current_ime = args[3]
            return SimpleNamespace(ok=True, stdout="")

        def adb(self, *args, **kwargs):
            self.broadcast_args = args
            return "Broadcasting: Intent { act=ADB_INPUT_TEXT (has extras) }\nBroadcast completed: result=0"

    device = Device()
    sender = TextInput(device)
    sender.strategy = "adbkeyboard"
    sender._probed = True
    result = sender.type("王菲 红豆", delay=0)
    assert shlex.split(device.broadcast_args[-1]) == ["王菲 红豆"]
    assert result.payload_exact
    assert device.current_ime == "com.example/.Ime"


def test_adbkeyboard_does_not_report_split_broadcast_as_success():
    class Device:
        current_ime = "com.example/.Ime"

        def adb_soft(self, *args, **kwargs):
            if args[:4] == ("shell", "settings", "get", "secure"):
                return SimpleNamespace(ok=True, stdout=self.current_ime)
            if args[:3] == ("shell", "ime", "set"):
                self.current_ime = args[3]
            return SimpleNamespace(ok=True, stdout="")

        def adb(self, *args, **kwargs):
            return "Broadcasting: Intent { act=ADB_INPUT_TEXT pkg=红豆 (has extras) }"

    device = Device()
    sender = TextInput(device)
    sender.strategy = "adbkeyboard"
    sender._probed = True
    with pytest.raises(UnicodeInputUnavailable, match="参数被拆分"):
        sender.type("王菲 红豆", delay=0)
    assert device.current_ime == "com.example/.Ime"


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
    assert "看图" in prompts.render("decide_vlm_screen", grid_spec="10x20",
                                image_width=1080, image_height=2400)


def test_unicode_submission_requires_exact_edittext_readback(monkeypatch):
    runner = Runner(cfg=Config(), task="搜索会议")
    calls = []
    runner.executor = SimpleNamespace(call=lambda name, **kw: calls.append(name) or "OK")
    runner.log = SimpleNamespace(phase=lambda *args, **kw: None)
    fake_xml = ('<hierarchy><node class="android.widget.EditText" text="hy" '
                'resource-id="calendar:id/title" /></hierarchy>')
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: SimpleNamespace(dump_xml=lambda: fake_xml))
    runner._dispatch({"type": "TYPE", "text": "会议", "submit": True})
    assert calls == ["input_text"]  # No ENTER on an unverified value.
    assert runner.pending_unicode == ""  # A failed readback is not a failed input channel.
    assert runner.pending_unicode_readback == "会议"


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
    assert not runner.unicode_readback_unconfirmed_this_step


def test_qq_music_autocomplete_is_focused_before_exact_chinese_type(monkeypatch):
    """Replays the search field and action shape from the 王菲 failure log."""
    xml_before = ('<hierarchy><node class="android.widget.AutoCompleteTextView" '
                  'resource-id="android:id/search_src_text" text="" focused="false" '
                  'enabled="true" clickable="true" bounds="[929,128][2192,164]" /></hierarchy>')
    xml_after = xml_before.replace('text="" focused="false"', 'text="王菲" focused="true"')
    current = {"xml": xml_before}
    calls = []

    def call(name, **kwargs):
        calls.append((name, kwargs))
        if name == "input_text":
            current["xml"] = xml_after
            return "OK input(adbkeyboard) sent='王菲'"
        return "OK"

    runner = Runner(cfg=Config(), task="用QQ音乐播放王菲的歌")
    runner.executor = SimpleNamespace(call=call)
    runner.log = SimpleNamespace(phase=lambda *args, **kwargs: None)
    runner.obs = Observation(b"", xml_before, 2240, 1260, "sig",
                             candidates_from_xml(xml_before, screen=(2240, 1260)))
    monkeypatch.setattr("autogui.core.graph.tools.dev",
                        lambda: SimpleNamespace(dump_xml=lambda: current["xml"]))
    monkeypatch.setattr("autogui.core.graph.tools.last_input_result",
                        lambda: SimpleNamespace(typed="王菲"))
    monkeypatch.setattr("autogui.core.graph.time.sleep", lambda seconds: None)

    action = parse_action({"type": "TYPE", "text": "王菲", "node_ref": 1, "submit": True})
    assert action["node_ref"] == 1
    result = runner._dispatch(action)
    assert [name for name, _ in calls] == ["tap", "input_text", "keyevent"]
    assert calls[0][1] == {"x": 1560, "y": 146}
    assert calls[1][1]["text"] == "王菲"
    assert "ENTER(submit)" in result
    assert runner.pending_unicode == runner.pending_unicode_readback == ""


def test_unconfirmed_autocomplete_readback_keeps_adb_chinese_path(monkeypatch):
    xml = ('<hierarchy><node class="android.widget.AutoCompleteTextView" '
           'resource-id="android:id/search_src_text" text="" focused="true" '
           'enabled="true" clickable="true" bounds="[929,128][2192,164]" /></hierarchy>')
    calls = []
    runner = Runner(cfg=Config(), task="搜索王菲")
    runner.obs = Observation(b"", xml, 2240, 1260, "sig",
                             candidates_from_xml(xml, screen=(2240, 1260)))
    runner.executor = SimpleNamespace(call=lambda name, **kwargs: calls.append(name) or "OK")
    runner.log = SimpleNamespace(phase=lambda *args, **kwargs: None, debug=False)
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: SimpleNamespace(dump_xml=lambda: xml))
    monkeypatch.setattr("autogui.core.graph.time.sleep", lambda seconds: None)

    action = parse_action({"type": "TYPE", "text": "王菲", "node_ref": 1, "submit": True})
    runner._dispatch(action)
    assert calls == ["input_text"]  # Already focused; no tap and no premature ENTER.
    assert runner.pending_unicode == ""
    assert runner.pending_unicode_readback == "王菲"
    assert "再用 TYPE 输入原文" in runner.context.input_guide
    assert runner.route({})["route"] == "vlm"
    assert runner.verify({"last_action": action}) == {"recover_depth": 0, "no_progress": 0}


def test_visual_completion_is_allowed_when_unicode_readback_is_unavailable(monkeypatch):
    runner = Runner(cfg=Config(), task="搜索晴天")
    runner.pending_unicode = "晴天"
    runner.context.input_guide = "等待视觉确认"
    import io
    from PIL import Image
    image = io.BytesIO()
    Image.new("RGB", (1080, 2400), "white").save(image, format="PNG")
    runner.obs = Observation(image.getvalue(), "", 1080, 2400, "sig", [])
    runner.screen = SimpleNamespace(width=1080, height=2400)
    runner.log = SimpleNamespace(phase=lambda *args, **kw: None, debug=False)
    seen = []
    def decide(*args, **kw):
        import base64
        sent = Image.open(io.BytesIO(base64.b64decode(kw["jpeg_b64"])))
        seen.append(sent.getpixel((sent.width // 2, sent.height // 2)))
        return ({"type": "COMPLETE", "result": "已显示晴天搜索结果"},
                SimpleNamespace(calls=1, prompt_tokens=10, completion_tokens=10))
    runner.glm = SimpleNamespace(decide=decide)

    result = runner.decide({"step": 4, "route": "vlm"})
    assert result["action"]["type"] == "COMPLETE"
    assert min(seen[0]) > 245  # 原始白图，没有红色格线或标签覆盖
    assert runner.pending_unicode == ""
    assert runner.context.input_guide == ""
    acted = runner.act({"action": result["action"]})
    settled = runner.verify({"last_action": acted["last_action"]})
    assert settled["success"] is True and settled["status"] == "complete"


def test_app_cache_is_not_described_as_installed_app_catalog():
    assert "不是设备应用全集" in prompts.DECIDE_SYSTEM
    assert "HOME" in prompts.DECIDE_SYSTEM
