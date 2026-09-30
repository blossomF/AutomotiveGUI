"""Short launch ads are handled before the first model decision."""

from types import SimpleNamespace

from autogui.config import Config
from autogui.core.graph import Runner


AD_XML = ('<hierarchy><node text="跳过" content-desc="" />'
          '<node text="互动广告" content-desc="" /></hierarchy>')
CONSENT_XML = ('<hierarchy><node text="同意" content-desc="" />'
               '<node text="隐私政策" content-desc="" /></hierarchy>')


def test_launch_ad_requires_skip_and_ad_marker():
    assert Runner._is_launch_ad(AD_XML)
    assert not Runner._is_launch_ad(CONSENT_XML)
    assert not Runner._is_launch_ad("")


def test_launch_ad_waits_to_six_seconds_then_reobserves(monkeypatch):
    runner = Runner(Config(), "打开QQ音乐播放歌曲")
    runner.launch_monotonic = 100.0
    messages = []
    runner.log = SimpleNamespace(phase=lambda *args, **kw: messages.append(args))
    old = SimpleNamespace(xml=AD_XML)
    refreshed = SimpleNamespace(xml=CONSENT_XML)
    grid = SimpleNamespace(width=1080, height=2400)
    sleeps = []
    monkeypatch.setattr("autogui.core.graph.time.monotonic", lambda: 103.0)
    monkeypatch.setattr("autogui.core.graph.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: object())
    monkeypatch.setattr("autogui.core.graph.observe", lambda *args, **kw: (refreshed, grid))

    assert runner._wait_for_launch_ad(old, grid) == (refreshed, grid)
    assert sleeps == [3.0]
    assert any("广告已消失" in item[1] for item in messages)
