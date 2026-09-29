"""Closed-loop wheel control, using fake XML/ADB instead of a connected phone."""

from types import SimpleNamespace

import pytest

from autogui.actions import parse_action
from autogui.brain.context import Context
from autogui.core.picker import PickerError, roll_to
from autogui.core.graph import Runner
from autogui.config import Config
from autogui.perception.capture import Observation
from autogui.perception.tree import Candidate, is_value_wheel


BOUNDS = (591, 1388, 778, 1857)


def _wheel(value: int, unit: str = "时") -> Candidate:
    return Candidate(7, f"{value:02d}{unit}", 684, 1622, BOUNDS, False,
                     "hour", "miuix.pickerwidget.widget.NumberPicker")


class FakeWheel:
    def __init__(self, value=11, unit="时", pixels_per_unit=160, increasing_finger=-1):
        self.value = value
        self.unit = unit
        self.pixels_per_unit = pixels_per_unit
        self.increasing_finger = increasing_finger
        self.vectors = []
        self.visible = True

    def dump(self):
        if not self.visible:
            return "<hierarchy/>"
        return (
            '<hierarchy><node class="miuix.pickerwidget.widget.NumberPicker" '
            f'resource-id="com.android.calendar:id/hour" content-desc="{self.value:02d}{self.unit}" '
            'bounds="[591,1388][778,1857]" scrollable="true" /></hierarchy>'
        )

    def swipe(self, x1, y1, x2, y2):
        assert 591 < x1 == x2 < 778
        assert 1388 < y1 < 1857 and 1388 < y2 < 1857
        self.vectors.append((x1, y1, x2, y2))
        movement = round(abs(y2 - y1) / self.pixels_per_unit)
        finger = 1 if y2 > y1 else -1
        self.value += movement * (1 if finger == self.increasing_finger else -1)
        return "OK"


def test_roll_to_reaches_target_with_control_feedback():
    wheel = FakeWheel()
    events = []
    result = roll_to(_wheel(11), "19时", dump_xml=wheel.dump, swipe=wheel.swipe,
                     delta_hint=8, report=lambda message, **metrics: events.append((message, metrics)))
    assert wheel.value == 19
    assert 1 < len(wheel.vectors) <= 16
    assert "已核验" in result
    assert events[-1][1]["target"] == "19时"


def test_roll_to_adapts_to_reversed_picker_direction():
    wheel = FakeWheel(value=13, increasing_finger=1)
    result = roll_to(_wheel(13), "19时", dump_xml=wheel.dump, swipe=wheel.swipe)
    assert wheel.value == 19 and "已核验" in result


def test_roll_to_handles_large_minute_change():
    wheel = FakeWheel(value=30, unit="分")
    result = roll_to(_wheel(30, "分"), "00分", dump_xml=wheel.dump, swipe=wheel.swipe)
    assert wheel.value == 0 and "已核验" in result


def test_roll_to_stops_when_value_does_not_change():
    wheel = FakeWheel(pixels_per_unit=10000)
    with pytest.raises(PickerError, match="连续两次无变化"):
        roll_to(_wheel(11), "19时", dump_xml=wheel.dump, swipe=wheel.swipe)
    assert len(wheel.vectors) == 2


def test_roll_to_stops_if_dialog_disappears():
    wheel = FakeWheel()

    def close_after_swipe(*vector):
        wheel.swipe(*vector)
        wheel.visible = False
        return "OK"

    with pytest.raises(PickerError, match="已消失"):
        roll_to(_wheel(11), "19时", dump_xml=wheel.dump, swipe=close_after_swipe)
    assert len(wheel.vectors) == 1


def test_roll_to_is_numeric_only_and_model_hint_is_conditional():
    assert is_value_wheel(_wheel(11))
    assert not is_value_wheel(Candidate(7, "9月29日 周二", 684, 1622, BOUNDS,
                                        False, "day", "miuix.pickerwidget.widget.NumberPicker"))
    context = Context(task="设置时间")
    text, _, _ = context.trim([_wheel(11)])
    assert "ROLL_TO" in text and "[滚轮]" in text
    text, _, _ = context.trim([])
    assert "delta_units=目标值-当前值" not in text


def test_scroll_target_and_roll_action_parse():
    assert parse_action({"type": "SCROLL", "direction": "down", "node_ref": 8,
                         "amount": 0.4})["amount"] == 0.4
    assert parse_action({"type": "ROLL_TO", "node_ref": 7, "target": "19时",
                         "delta_units": 8})["delta_units"] == 8
    with pytest.raises(ValueError):
        parse_action({"type": "SCROLL", "direction": "down", "amount": 5})


def test_targeted_scroll_uses_selected_container_bounds():
    hour = _wheel(11)
    minute = Candidate(8, "30分", 871, 1622, (778, 1388, 965, 1857), False,
                       "minute", "miuix.pickerwidget.widget.NumberPicker")
    xml = (
        '<hierarchy><node class="miuix.pickerwidget.widget.NumberPicker" '
        'resource-id="x:id/hour" bounds="[591,1388][778,1857]" scrollable="true" />'
        '<node class="miuix.pickerwidget.widget.NumberPicker" '
        'resource-id="x:id/minute" bounds="[778,1388][965,1857]" scrollable="true" /></hierarchy>'
    )
    runner = Runner(cfg=Config(), task="设置时间")
    runner.obs = Observation(b"", xml, 1080, 2400, "", [hour, minute])
    x1, y1, x2, y2 = runner._target_scroll_vector("down", 8, 0.5)
    assert x1 == x2 == 871
    assert 1388 < y2 < y1 < 1857
    with pytest.raises(ValueError, match="当前候选"):
        runner._target_scroll_vector("down", 99, 0.5)
    with pytest.raises(ValueError, match="多个滚轮"):
        runner._scroll("down")


def test_wide_date_wheel_is_still_vertical_scroll_target():
    date = Candidate(3, "9月29日 周二", 312, 1622, (34, 1388, 591, 1857), False,
                     "day", "miuix.pickerwidget.widget.NumberPicker")
    xml = (
        '<hierarchy><node class="miuix.pickerwidget.widget.NumberPicker" '
        'resource-id="x:id/day" bounds="[34,1388][591,1857]" '
        'scrollable="true" /></hierarchy>'
    )
    runner = Runner(cfg=Config(), task="设置日期")
    runner.obs = Observation(b"", xml, 1080, 2400, "", [date])
    x1, y1, x2, y2 = runner._target_scroll_vector("down", 3, 0.4)
    assert x1 == x2 == 312 and 1388 < y2 < y1 < 1857


def test_runner_dispatches_roll_to_through_existing_swipe_tool(monkeypatch):
    wheel = FakeWheel()
    runner = Runner(cfg=Config(), task="设置19时")
    runner.obs = Observation(b"", wheel.dump(), 1080, 2400, "", [_wheel(11)])
    calls = []

    def call(name, **kw):
        calls.append(name)
        return wheel.swipe(kw["x1"], kw["y1"], kw["x2"], kw["y2"])

    runner.executor = SimpleNamespace(call=call)
    runner.log = SimpleNamespace(phase=lambda *args, **kwargs: None)
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: SimpleNamespace(dump_xml=wheel.dump))
    result = runner._dispatch(parse_action({"type": "ROLL_TO", "node_ref": 7,
                                            "target": "19时", "delta_units": 8}))
    assert wheel.value == 19 and "已核验" in result
    assert calls and set(calls) == {"swipe"}
