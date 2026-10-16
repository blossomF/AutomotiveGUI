from dataclasses import replace
from types import SimpleNamespace

from autogui.brain.context import Context
from autogui.brain.prompts import DECIDE_SYSTEM, DECIDE_SYSTEM_VLM
from autogui.brain.scene_hints import match_scene_hints
from autogui.config import Config
from autogui.core.graph import Runner
from autogui.perception.capture import Observation
from autogui.perception.tree import Candidate


DIALOG = ('<hierarchy><node class="android.app.Dialog">'
          '<node text="通知权限"/><node text="以后再说" clickable="true"/>'
          '</node></hierarchy>')


def test_wheel_guidance_follows_current_candidates_only():
    context = Context(task="设置时间")
    wheel = Candidate(1, "19时", 50, 50, (0, 0, 100, 100), False,
                      "hour", "android.widget.NumberPicker")
    text, _, _ = context.trim([wheel])
    assert "当前有可读数值滚轮" in text
    custom = replace(wheel, cls="app.WheelView", label="小时")
    text, _, _ = context.trim([custom])
    assert "当前有自定义滚轮" in text
    assert "当前有可读数值滚轮" not in text
    text, _, _ = context.trim([])
    assert "当前有自定义滚轮" not in text
    assert "当前有可读数值滚轮" not in text
    for system in (DECIDE_SYSTEM, DECIDE_SYSTEM_VLM):
        assert "当前有" not in system
        assert "数值滚轮用 ROLL_TO" not in system
        assert "弹窗应对行为" not in system


def test_popup_requires_structure_and_action_not_keywords():
    assert match_scene_hints(DIALOG)
    assert not match_scene_hints(DIALOG.replace('class="android.app.Dialog"', 'class="android.view.View"'))
    assert not match_scene_hints(DIALOG.replace('clickable="true"', 'clickable="false"'))
    assert not match_scene_hints("broken XML")
    assert not match_scene_hints("")


def test_countdown_guidance_requires_actionable_skip():
    xml = '<hierarchy><node text="跳过 3s" clickable="true" /></hierarchy>'
    assert "倒计时" in match_scene_hints(xml)[0]
    assert not match_scene_hints(xml.replace('clickable="true"', 'clickable="false"'))
    assert not match_scene_hints(xml.replace('跳过 3s', '跳过教程'))


def test_runner_replaces_scene_guidance_each_decision_without_extra_call():
    runner = Runner(cfg=Config(), task="打开应用")
    runner.obs = Observation(b"", DIALOG, 1080, 2400, "dialog", [])
    runner.screen = SimpleNamespace(width=1080, height=2400)
    runner.log = SimpleNamespace(phase=lambda *a, **k: None, debug=False)
    seen = []

    def decide(text, *args, **kwargs):
        seen.append(text)
        return {"type": "WAIT", "seconds": 1}, SimpleNamespace(calls=1, prompt_tokens=1, completion_tokens=1, latency_ms=0)

    runner.glm = SimpleNamespace(decide=decide)
    runner.decide({"step": 1, "route": "tree"})
    runner.obs = Observation(b"", "<hierarchy/>", 1080, 2400, "page", [])
    runner.decide({"step": 2, "route": "tree"})
    assert len(seen) == 2
    assert "当前控件树包含带操作按钮的对话框" in seen[0]
    assert "当前场景操作指导" not in seen[1]
    assert runner.context.scene_hints == []
