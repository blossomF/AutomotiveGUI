"""首次模型选应用：启动前不观测，未命中才进入原图。"""

import time
from types import SimpleNamespace

import main

from autogui.app_routes import choose_initial_route
from autogui.config import AppConfig, Config
from autogui.core.graph import Runner


def _config():
    return Config(apps=[
        AppConfig(name="美团", package="com.example.meituan", aliases=["美团外卖"]),
        AppConfig(name="QQ音乐", package="com.example.music"),
    ])


class FakeGLM:
    def __init__(self, answer, events):
        self.answer = answer
        self.events = events
        self.last_call = SimpleNamespace(latency_ms=12, total_tokens=30)
        self.usage = SimpleNamespace(as_dict=lambda: {"calls": 1, "prompt_tokens": 25,
                                                      "completion_tokens": 5, "total_tokens": 30})

    def chat_json(self, messages, *, kind):
        self.events.append("model")
        assert kind == "initial_app_route"
        assert "美团外卖" in messages[1]["content"]
        assert "com.example.meituan" not in messages[1]["content"]
        return self.answer


class FakeLog:
    path = "test.jsonl"
    events = []

    def phase(self, *args, **kwargs):
        pass

    def summary(self, **kwargs):
        return kwargs["status"]


def _runner(monkeypatch, task, answer, events, launch_result="OK launch"):
    class Executor:
        def call(self, action, **kwargs):
            events.append((action, kwargs))
            return launch_result

    runner = Runner(cfg=_config(), task=task, glm=FakeGLM(answer, events), executor=Executor())
    runner.log = FakeLog()
    monkeypatch.setattr(runner, "prepare", lambda: setattr(runner, "started", time.time()))
    return runner


def test_model_selects_configured_id_without_regex_or_package_guess():
    cfg = _config()
    events = []
    glm = FakeGLM({"decision": "OPEN_CONTINUE", "app_id": 1}, events)
    route = choose_initial_route("打开美团外卖 App，搜索兰州拉面", cfg.apps, glm)
    assert route.app.package == "com.example.meituan"
    assert not route.finish_after_launch
    assert choose_initial_route("打开未知应用", cfg.apps,
                                FakeGLM({"decision": "OBSERVE", "app_id": None}, [] )).app is None
    assert choose_initial_route("打开未知应用", cfg.apps,
                                FakeGLM({"decision": "OPEN_ONLY", "app_id": 99}, [])).app is None


def test_pure_open_model_then_adb_no_screen_or_graph(monkeypatch):
    events = []
    runner = _runner(monkeypatch, "打开美团",
                     {"decision": "OPEN_ONLY", "app_id": 1}, events)
    monkeypatch.setattr(runner, "graph", lambda: (_ for _ in ()).throw(AssertionError("screen")))
    result = runner.execute()
    assert result["success"] is True
    assert events == ["model", ("launch_app", {"package": "com.example.meituan"})]
    assert runner.context.entities["已打开应用"] == "美团"


def test_composite_opens_before_original_observation(monkeypatch):
    events = []
    runner = _runner(monkeypatch, "打开美团外卖 App，搜索兰州拉面，把第一家店的招牌菜加入购物车",
                     {"decision": "OPEN_CONTINUE", "app_id": 1}, events)

    class Graph:
        def invoke(self, state, *, config):
            events.append("perceive")
            assert runner.context.entities["已打开应用"] == "美团"
            return {"success": True, "status": "complete", "step": 2}

    monkeypatch.setattr(runner, "graph", lambda: Graph())
    assert runner.execute()["success"] is True
    assert events == ["model", ("launch_app", {"package": "com.example.meituan"}), "perceive"]


def test_uncached_or_failed_launch_falls_back_to_observation(monkeypatch):
    for answer, result in [({"decision": "OBSERVE", "app_id": None}, "OK"),
                           ({"decision": "OPEN_ONLY", "app_id": 1}, "TOOL_ERROR missing app")]:
        events = []
        runner = _runner(monkeypatch, "打开未知应用", answer, events, result)

        class Graph:
            def invoke(self, state, *, config):
                events.append("perceive")
                return {"success": False, "status": "stopped", "step": 1}

        monkeypatch.setattr(runner, "graph", lambda: Graph())
        runner.execute()
        assert events[0] == "model"
        assert events[-1] == "perceive"


def test_empty_cache_skips_initial_model_call(monkeypatch):
    events = []
    runner = _runner(monkeypatch, "打开未知应用", {"decision": "OBSERVE"}, events)
    runner.cfg.apps = []

    class Graph:
        def invoke(self, state, *, config):
            events.append("perceive")
            return {"success": False, "status": "stopped", "step": 1}

    monkeypatch.setattr(runner, "graph", lambda: Graph())
    runner.execute()
    assert events == ["perceive"]
    assert "initial_model" not in runner.route_counts


def test_cmd_run_always_constructs_model_for_initial_decision(monkeypatch):
    cfg = _config()
    seen = []
    monkeypatch.setattr(main, "_bootstrap", lambda _: object())
    import autogui.brain.glm_client as glm_mod
    import autogui.core.graph as graph_mod

    monkeypatch.setattr(glm_mod, "GLMClient", lambda _: "model")

    class FakeRunner:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def execute(self):
            return {"success": True}

    monkeypatch.setattr(graph_mod, "Runner", FakeRunner)
    assert main.cmd_run(SimpleNamespace(task="打开美团", skill_only=False), cfg) == 0
    assert seen[0]["glm"] == "model"
