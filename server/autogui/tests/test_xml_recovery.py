"""Recovery decisions for intermittent XML failures and unlabeled splash ads."""

from types import SimpleNamespace

from autogui.bridge.adb import ADBError, ADBResult, Device
from autogui.config import Config
from autogui.core.graph import Runner


def test_two_dump_timeouts_skip_one_observation_then_retry(monkeypatch):
    device = Device("test-device")
    calls = []
    traces = []
    device._trace_xml = lambda *args, **kwargs: traces.append((args[2], kwargs))

    def adb_soft(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("shell", "rm"):
            return ADBResult(0, "", "")
        raise ADBError(args, -1, "timed out after 8s")

    monkeypatch.setattr(device, "adb_soft", adb_soft)
    assert device.dump_xml() == ""
    assert device.dump_xml() == ""
    count = len(calls)
    assert device.dump_xml() == ""
    assert len(calls) == count
    assert traces[-1][0] == "skipped_after_timeouts"
    assert device.dump_xml() == ""
    assert len(calls) == count + 2


def test_dump_exit_without_stderr_has_readable_reason(monkeypatch):
    device = Device("test-device")
    traces = []
    device._trace_xml = lambda *args, **kwargs: traces.append(args)
    monkeypatch.setattr(device, "adb_soft", lambda *args, **kwargs:
                        ADBResult(0, "", "") if args[:2] == ("shell", "rm")
                        else ADBResult(137, "", ""))
    monkeypatch.setattr(device, "_diagnose_dump_137", lambda remote: {})
    assert device.dump_xml() == ""
    assert traces[-1][2] == "dump_failed"
    assert "137" in traces[-1][3]


def test_qq_unlabeled_launch_frame_reobserves_before_model(monkeypatch):
    runner = Runner(Config(), "播放我爱你中国")
    runner.context.launch_note = "QQ音乐"
    runner.launch_monotonic = 100.0
    events = []
    runner.log = SimpleNamespace(phase=lambda *args, **kw: events.append(args), debug=False)
    old = SimpleNamespace(xml="<hierarchy><node /></hierarchy>", tree_usable=False)
    refreshed = SimpleNamespace(xml="<hierarchy><node text='搜索' /></hierarchy>",
                                tree_usable=True, shot_ms=500, tree_ms=3000)
    screen = SimpleNamespace(width=1080, height=2400)
    monkeypatch.setattr("autogui.core.graph.time.monotonic", lambda: 107.0)
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: object())
    monkeypatch.setattr("autogui.core.graph.observe", lambda *args, **kw: (refreshed, screen))
    assert runner._wait_for_launch_ad(old, screen) == (refreshed, screen)
    assert any("补抓以排除无标签广告" in event[1] for event in events)
