"""VLM image coordinates must map to the device frame, not be tapped raw."""

from types import SimpleNamespace

import pytest

from autogui.actions import parse_action
from autogui.config import Config
from autogui.core.graph import Runner
from autogui.perception.capture import Observation


def test_point_uses_transmitted_image_size_for_device_tap():
    runner = Runner(Config(), "查看新歌榜详情")
    runner.obs = Observation(b"", "", 2240, 1260, "sig", [])
    events = []
    runner.log = SimpleNamespace(phase=lambda *args, **kw: events.append(args))
    runner.executor = SimpleNamespace(call=lambda method, **kw: (events.append((method, kw)) or "ok"))
    action = parse_action({"type": "CLICK", "point": [990, 127]})
    action["image_size"] = (1568, 882)

    assert runner._dispatch(action) == "ok"
    assert ("tap", {"x": 1414, "y": 181}) in events


@pytest.mark.parametrize("point", [[float("nan"), 1], [1, float("inf")], [True, 2], [1]])
def test_invalid_image_point_is_rejected(point):
    with pytest.raises(ValueError, match="point"):
        parse_action({"type": "CLICK", "point": point})
