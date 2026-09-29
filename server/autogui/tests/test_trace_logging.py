"""诊断日志必须足够重建一次 XML 与 GLM 往返，且不复制图像 base64。"""

import json
from types import SimpleNamespace

from autogui.brain.glm_client import GLMClient
from autogui.bridge.adb import ADBResult, Device
from autogui.config import Config, GLMConfig
from autogui.core.decision_log import DecisionLog
from autogui.core.graph import Runner


def test_glm_trace_keeps_prompt_response_and_call_id_without_base64(tmp_path):
    log = DecisionLog("trace", tmp_path, verbose=False, debug=True)
    log.step_begin(2)

    response = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='{"type":"BACK"}'))],
                        usage=None),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4)),
    ]

    class Completions:
        def create(self, **kwargs):
            assert kwargs["messages"][1]["content"][1]["image_url"]["url"].endswith("abc123")
            return response

    fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    glm = GLMClient(GLMConfig(api_key="unused"), client=fake, log=log)
    raw, _ = glm.chat([
        {"role": "system", "content": "完整系统规则"},
        {"role": "user", "content": [
            {"type": "text", "text": "当前候选 #1 搜索"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc123"}},
        ]},
    ], kind="decide")

    assert raw == '{"type":"BACK"}'
    request_event, response_event = [e for e in log.events if e["phase"].startswith("glm_")]
    assert request_event["call_id"] == response_event["call_id"] == 1
    request = (log.debug_dir / request_event["artifact"]).read_text(encoding="utf-8")
    saved = json.loads(request)
    assert saved["messages"][0]["content"] == "完整系统规则"
    assert saved["messages"][1]["content"][0]["text"] == "当前候选 #1 搜索"
    assert "abc123" not in request
    assert "image data URL omitted" in request
    assert (log.debug_dir / response_event["artifact"]).read_text(encoding="utf-8") == raw
    log.close()


def test_every_device_xml_dump_has_own_artifact(tmp_path, monkeypatch):
    log = DecisionLog("xml", tmp_path, verbose=False, debug=True)
    log.step_begin(3)
    device = Device("fake")
    device.trace_log = log
    monkeypatch.setattr(device, "adb_soft", lambda *args, **kwargs: ADBResult(0, "", ""))
    answers = iter(["<hierarchy>first</hierarchy>", "<hierarchy>second</hierarchy>"])
    monkeypatch.setattr(device, "adb", lambda *args, **kwargs: next(answers))

    device.dump_xml()
    device.dump_xml()

    events = [e for e in log.events if e["phase"] == "xml"]
    assert len(events) == 2
    assert events[0]["artifact"] != events[1]["artifact"]
    assert (log.debug_dir / events[0]["artifact"]).read_text(encoding="utf-8") == "<hierarchy>first</hierarchy>"
    assert (log.debug_dir / events[1]["artifact"]).read_text(encoding="utf-8") == "<hierarchy>second</hierarchy>"
    log.close()


def test_runner_prepare_wires_trace_after_log_creation(tmp_path, monkeypatch):
    device = Device("fake")
    monkeypatch.setattr("autogui.core.graph.tools.dev", lambda: device)
    cfg = Config(log_dir=str(tmp_path), debug=True, verbose=False,
                 skills={"enabled": False})
    glm = GLMClient(GLMConfig(api_key="unused"), client=object())
    runner = Runner(cfg=cfg, task="trace", glm=glm,
                    executor=SimpleNamespace(name="fake"))

    runner.prepare()

    assert runner.log is glm._log is device.trace_log
    runner.log.close()
