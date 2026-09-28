"""设置页的配置保存与模型验证不依赖真实设备或网络。"""

import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autogui.config import Config
from autogui.dashboard_settings import grid_preview, public_settings, save_settings, test_model as verify_model
from autogui.perception.grid import auto_grid_dimensions, grid_dimensions


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "grid:\n  cols: 20 # manual fallback\n  rows: 10\n"
        "glm:\n  model: old-model\n  base_url: https://old.example/v1\n  api_key: ''\n"
        "voice:\n  appid: ''\n  api_key: ''\n  api_secret: ''\n"
        "debug: true\n", encoding="utf-8",
    )
    return path


def test_auto_grid_uses_actual_screenshot_dimensions():
    assert auto_grid_dimensions(1080, 2400) == (11, 22)
    assert auto_grid_dimensions(1920, 1080) == (19, 10)
    assert grid_dimensions(1080, 2400, mode="manual", cols=20, rows=10) == (20, 10)
    assert grid_dimensions(1080, 2400, mode="auto", cols=20, rows=10) == (11, 22)


def test_settings_save_preserves_unrelated_config_and_manual_precedence(config_path):
    data = save_settings(config_path, {
        "model": {"url": "https://new.example/v1", "name": "new-model", "api_key": ""},
        "grid": {"mode": "manual", "cols": 12, "rows": 24},
    })
    source = config_path.read_text(encoding="utf-8")
    assert "debug: true" in source
    assert "# manual fallback" in source
    assert data["model"]["url"] == "https://new.example/v1"
    assert data["grid"] == {"mode": "manual", "cols": 12, "rows": 24}
    assert grid_preview(config_path, (1080, 2400))["active"]["cols"] == 12
    assert grid_preview(config_path, (1080, 2400))["auto"] == {"cols": 11, "rows": 22}


def test_key_is_encrypted_and_never_returned(config_path, monkeypatch):
    import autogui.security as security

    monkeypatch.setattr(security, "encrypt", lambda key: "ENC(fake)" if key == "secret-value" else "")
    monkeypatch.setattr(security, "resolve_secret", lambda value: "secret-value" if value == "ENC(fake)" else "")
    saved = save_settings(config_path, {"model": {"api_key": "secret-value"}})
    assert "secret-value" not in config_path.read_text(encoding="utf-8")
    assert "secret-value" not in str(saved)
    assert saved["model"]["api_key_configured"] is True
    assert saved["model"]["api_key_state"] == "saved"
    assert "api_key" not in public_settings(config_path)["model"]


def test_saved_settings_reappear_after_reload_without_revealing_secrets(config_path, monkeypatch):
    import autogui.security as security

    monkeypatch.setattr(security, "encrypt", lambda value: f"ENC({value[::-1]})")
    monkeypatch.setattr(security, "resolve_secret", lambda value: value[4:-1][::-1])
    save_settings(config_path, {
        "model": {"url": "https://saved.example/v1", "name": "saved-model", "api_key": "model-secret"},
        "voice": {"appid": "app-id", "api_key": "voice-key", "api_secret": "voice-secret"},
        "grid": {"mode": "manual", "cols": 12, "rows": 24},
    })
    reloaded = public_settings(config_path)
    assert reloaded["model"] == {
        "url": "https://saved.example/v1", "name": "saved-model",
        "api_key_configured": True, "api_key_state": "saved",
    }
    assert reloaded["grid"] == {"mode": "manual", "cols": 12, "rows": 24}
    assert reloaded["voice"]["configured"] is True
    assert reloaded["voice"]["fields"] == {
        "appid": "saved", "api_key": "saved", "api_secret": "saved",
    }
    assert "model-secret" not in str(reloaded)
    assert "voice-secret" not in str(reloaded)


def test_unreadable_ciphertext_is_reported_explicitly(config_path, monkeypatch):
    import autogui.security as security

    monkeypatch.setattr(security, "resolve_secret", lambda _: (_ for _ in ()).throw(OSError("unavailable")))
    source = config_path.read_text(encoding="utf-8")
    config_path.write_text(source.replace("api_key: ''", "api_key: ENC(other-user)"), encoding="utf-8")
    status = public_settings(config_path)["model"]
    assert status["api_key_configured"] is False
    assert status["api_key_state"] == "unavailable"


def test_model_verification_uses_unsaved_form_and_does_not_modify_config(config_path):
    before = config_path.read_bytes()
    seen = []

    class FakeClient:
        def chat(self, messages, *, kind):
            assert kind == "settings_test"
            return "OK", None

    def factory(cfg):
        seen.append(cfg)
        return FakeClient()

    result = verify_model(config_path, {"model": {
        "url": "https://trial.example/v1", "name": "trial-model", "api_key": "trial-secret",
    }}, client_factory=factory)
    assert result["ok"] is True
    assert seen[0].base_url == "https://trial.example/v1"
    assert seen[0].model == "trial-model"
    assert seen[0].max_tokens == Config.load(config_path).glm.max_tokens
    assert config_path.read_bytes() == before
    assert "trial-secret" not in str(result)


def test_bad_model_url_cannot_be_saved(config_path):
    with pytest.raises(ValueError, match="HTTPS"):
        save_settings(config_path, {"model": {"url": "http://remote.example/v1"}})
    assert Config.load(config_path).glm.base_url == "https://old.example/v1"


def test_dashboard_settings_page_and_api_work_without_device(config_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    module_path = root / "dist" / "dashboard_server.py"
    spec = importlib.util.spec_from_file_location("dashboard_settings_test_server", module_path)
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)
    monkeypatch.setattr(dashboard, "CONFIG_PATH", config_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/settings") as response:
            html = response.read().decode("utf-8")
        assert "验证连接" in html and "gridManual" in html
        with urllib.request.urlopen(base + "/api/settings") as response:
            settings = json.load(response)
        assert settings["grid"]["mode"] == "auto"
        assert "api_key" not in settings["model"]
        body = json.dumps({"grid": {"mode": "manual", "cols": 12, "rows": 24}}).encode()
        request = urllib.request.Request(base + "/api/settings", body,
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request) as response:
            saved = json.load(response)
        assert saved["ok"] and saved["grid"]["mode"] == "manual"
        assert Config.load(config_path).grid.cols == 12
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
