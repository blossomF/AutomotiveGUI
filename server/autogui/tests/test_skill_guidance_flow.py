"""普通任务中的 Skill 只为模型加载指导，不能凭关键词直接操作设备。"""

from types import SimpleNamespace

import pytest

from autogui.brain.glm_client import Usage
from autogui.config import Config
from autogui.core.decision_log import DecisionLog
from autogui.core.graph import Runner
from autogui.perception.grid import Grid
from autogui.perception.tree import Candidate
from autogui.skills.base import SkillBook


def _runner(tmp_path, responses):
    skill_dir = tmp_path / "music"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        "name: music\napp:\n  package: com.car.music\nskills:\n"
        "  - name: play_song\n    summary: 播放指定歌曲\n    params: [song]\n"
        "    guide: '搜索 {song}，确认播放状态后完成。'\n"
        "    steps:\n      - { act: launch }\n      - { act: complete }\n",
        encoding="utf-8",
    )

    class TrapDevice:
        def __getattr__(self, name):
            raise AssertionError(f"Skill 不应调用设备方法 {name}")

    seen = []

    class FakeGLM:
        def decide(self, context_text, **kwargs):
            seen.append(context_text)
            return responses.pop(0), Usage(calls=1)

    runner = Runner(Config(), "在音乐 App 播放七里香", glm=FakeGLM())
    runner.log = DecisionLog("skill-test", tmp_path, verbose=False)
    runner.book = SkillBook(tmp_path, TrapDevice(), None)
    runner.context.skill_index = runner.book.index()
    candidate = Candidate(1, "搜索", 50, 50, (0, 0, 100, 100), True, "", "")
    runner.obs = SimpleNamespace(candidates=[candidate, candidate, candidate], tree_usable=True,
                                 width=100, height=100, png=b"")
    runner.grid = Grid(width=100, height=100, cols=10, rows=10)
    return runner, seen


@pytest.mark.parametrize("task", [
    "在音乐 App 播放七里香",
    "在视频应用播放七里香的现场视频",
    "搜索如何播放音乐的教程",
])
def test_keyword_alone_never_selects_or_runs_skill(tmp_path, task):
    runner, seen = _runner(tmp_path, [])
    state = {"task": task, "step": 1}

    assert runner.route(state) == {"route": "tree"}
    assert runner.pending_skill is None
    assert seen == []
    runner.log.close()


def test_route_is_always_tree_first_then_vlm(tmp_path):
    runner, _ = _runner(tmp_path, [])
    assert runner.route({"step": 1}) == {"route": "tree"}
    runner.obs.tree_usable = False
    assert runner.route({"step": 1}) == {"route": "vlm"}
    runner.log.close()


def test_model_selection_loads_guide_then_model_chooses_action(tmp_path):
    runner, seen = _runner(tmp_path, [
        {"type": "SKILL", "skill": "music.play_song", "params": {"song": "七里香"}},
        {"type": "CLICK", "node_ref": 1, "explain": "点击搜索"},
    ])
    first = runner.decide({"step": 1, "route": "tree"})
    assert first["route"] == "skill"

    loaded = runner.run_skill({"step": 1, "route": "skill"})
    assert loaded["route"] == "tree"
    assert loaded["action"] == {}
    assert "搜索 七里香" in runner.context.active_skill_guide

    second = runner.decide({"step": 1, "route": loaded["route"]})
    assert second["action"]["type"] == "CLICK"
    assert "当前已加载的技能指导" in seen[1]
    assert all(event["phase"] != "act" for event in runner.log.events)
    runner.log.close()


def test_skill_requires_all_params_and_cannot_be_reactivated(tmp_path):
    runner, _ = _runner(tmp_path, [])
    assert runner._resolve_skill_choice({"skill": "music.play_song", "params": {}})[0] is None
    runner.activated_skills.add("music.play_song")
    assert runner._resolve_skill_choice({"skill": "music.play_song", "params": {"song": "七里香"}})[0] is None
    runner.log.close()
