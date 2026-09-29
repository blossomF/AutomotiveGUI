"""设置页的配置保存与模型验证不依赖真实设备或网络。"""

import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autogui.config import Config
from autogui.dashboard_settings import (
    device_apps, public_settings, refresh_app_cache,
    save_apps, save_settings, test_model as verify_model,
)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "grid:\n  cols: 20 # manual fallback\n  rows: 10\n"
        "glm:\n  model: old-model\n  base_url: https://old.example/v1\n  api_key: ''\n"
        "voice:\n  appid: ''\n  api_key: ''\n  api_secret: ''\n"
        "apps:\n  - name: QQ音乐\n    package: com.example.music\n"
        "  - name: 飞书\n    package: com.example.feishu\n"
        "debug: true\n", encoding="utf-8",
    )
    return path


def test_legacy_grid_keeps_only_image_encoding_values(config_path):
    source = config_path.read_text(encoding="utf-8")
    config_path.write_text(source.replace("  rows: 10", "  rows: 10\n  jpeg_quality: 72\n  max_side: 1200"),
                           encoding="utf-8")
    cfg = Config.load(config_path)
    assert (cfg.image.jpeg_quality, cfg.image.max_side) == (72, 1200)
    assert "grid" not in public_settings(config_path)


def test_settings_save_ignores_legacy_grid_and_preserves_other_fields(config_path):
    data = save_settings(config_path, {
        "model": {"url": "https://new.example/v1", "name": "new-model", "api_key": ""},
        "grid": {"mode": "manual", "cols": 12, "rows": 24},
    })
    source = config_path.read_text(encoding="utf-8")
    assert "debug: true" in source
    assert "# manual fallback" in source
    assert data["model"]["url"] == "https://new.example/v1"
    assert "grid" not in data
    assert data["apps"] == [
        {"name": "QQ音乐", "package": "com.example.music", "aliases": []},
        {"name": "飞书", "package": "com.example.feishu", "aliases": []},
    ]


def test_key_is_encrypted_on_disk_and_reloaded_for_local_settings(config_path, monkeypatch):
    import autogui.security as security

    monkeypatch.setattr(security, "encrypt", lambda key: "ENC(fake)" if key == "secret-value" else "")
    monkeypatch.setattr(security, "resolve_secret", lambda value: "secret-value" if value == "ENC(fake)" else "")
    saved = save_settings(config_path, {"model": {"api_key": "secret-value"}})
    assert "secret-value" not in config_path.read_text(encoding="utf-8")
    assert saved["model"]["api_key"] == "secret-value"
    assert saved["model"]["api_key_configured"] is True
    assert public_settings(config_path)["model"]["api_key"] == "secret-value"


def test_saved_settings_reappear_after_reload(config_path, monkeypatch):
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
        "api_key": "model-secret", "api_key_configured": True,
        "temperature": 0.1, "max_tokens": 1024, "timeout": 30.0, "max_retries": 4,
    }
    assert "grid" not in reloaded
    assert reloaded["voice"]["appid"] == "app-id"
    assert reloaded["voice"]["api_key"] == "voice-key"
    assert reloaded["voice"]["api_secret"] == "voice-secret"
    assert reloaded["voice"]["max_seconds"] == 15.0
    assert reloaded["voice"]["silence_seconds"] == 0.8
    assert reloaded["voice"]["silence_threshold"] == 300


def test_save_settings_persists_glm_and_voice_numeric_fields(config_path, monkeypatch):
    import autogui.security as security

    monkeypatch.setattr(security, "encrypt", lambda value: f"ENC({value})")
    monkeypatch.setattr(security, "resolve_secret", lambda value: value[4:-1])
    data = save_settings(config_path, {
        "model": {"url": "https://new.example/v1", "name": "new-model", "api_key": "",
                  "temperature": 0.2, "max_tokens": 4096, "timeout": 90, "max_retries": 6},
        "grid": {"mode": "auto", "cols": 20, "rows": 10},
        "voice": {"appid": "app-id", "api_key": "vk", "api_secret": "vs", "iat_url": "",
                  "max_seconds": 20, "silence_seconds": 1.2, "silence_threshold": 200},
    })
    cfg = Config.load(config_path)
    assert cfg.glm.temperature == 0.2
    assert cfg.glm.max_tokens == 4096
    assert cfg.glm.timeout == 90
    assert cfg.glm.max_retries == 6
    assert cfg.voice.max_seconds == 20
    assert cfg.voice.silence_seconds == 1.2
    assert cfg.voice.silence_threshold == 200
    assert data["model"]["max_tokens"] == 4096
    assert data["voice"]["silence_threshold"] == 200
    assert "# manual fallback" in config_path.read_text(encoding="utf-8")


def test_save_settings_rejects_non_numeric_fields(config_path):
    with pytest.raises(ValueError, match="必须是数字"):
        save_settings(config_path, {"model": {"url": "https://new.example/v1", "name": "m",
                                              "api_key": "", "temperature": "fast"}})
    with pytest.raises(ValueError):
        save_settings(config_path, {"model": {"url": "https://new.example/v1", "name": "m",
                                              "api_key": ""},
                                    "voice": {"max_seconds": 999}})


def test_unreadable_ciphertext_does_not_appear_configured(config_path, monkeypatch):
    import autogui.security as security

    monkeypatch.setattr(security, "resolve_secret", lambda _: (_ for _ in ()).throw(OSError("unavailable")))
    source = config_path.read_text(encoding="utf-8")
    config_path.write_text(source.replace("api_key: ''", "api_key: ENC(other-user)"), encoding="utf-8")
    status = public_settings(config_path)["model"]
    assert status["api_key_configured"] is False
    assert status["api_key"] == "ENC(other-user)"


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
        assert "验证连接" in html and "gridManual" not in html and "appCheckList" in html
        assert "reloadAppsBtn" in html and "addAppBtn" in html and "devicePackages" in html
        with urllib.request.urlopen(base + "/api/settings") as response:
            settings = json.load(response)
        assert "grid" not in settings
        assert settings["model"]["api_key"] == ""
        assert settings["apps"][0]["aliases"] == []
        body = json.dumps({"grid": {"mode": "manual", "cols": 12, "rows": 24}}).encode()
        request = urllib.request.Request(base + "/api/settings", body,
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request) as response:
            saved = json.load(response)
        assert saved["ok"] and "grid" not in saved
        assert "# manual fallback" in config_path.read_text(encoding="utf-8")
        body = json.dumps({"apps": [
            {"name": "飞书", "package": "com.example.feishu2", "aliases": ["Lark"]},
            {"name": "新应用", "package": "com.example.new", "aliases": []},
        ]}).encode()
        request = urllib.request.Request(base + "/api/settings/apps", body,
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request) as response:
            saved = json.load(response)
        assert saved["ok"]
        assert [a["name"] for a in saved["apps"]] == ["飞书", "新应用"]
        assert [a.name for a in Config.load(config_path).apps] == ["飞书", "新应用"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_save_apps_add_remove_modify_and_preserve_comments(config_path):
    saved = save_apps(config_path, [
        {"name": "QQ音乐", "package": "com.new.music", "aliases": ["QQ Music"]},
        {"name": "美团", "package": "com.sankuai.meituan.car", "aliases": []},
        {"name": "哔哩哔哩", "package": "", "aliases": ["B站"]},
    ])
    source = config_path.read_text(encoding="utf-8")
    assert "# manual fallback" in source
    assert "debug: true" in source
    assert "com.example.feishu" not in source
    cfg = Config.load(config_path)
    assert [a.name for a in cfg.apps] == ["QQ音乐", "美团", "哔哩哔哩"]
    assert cfg.apps[0].package == "com.new.music"
    assert cfg.apps[0].aliases == ["QQ Music"]
    assert cfg.apps[2].package == ""
    assert saved["apps"][1] == {"name": "美团", "package": "com.sankuai.meituan.car",
                                "aliases": []}


def test_save_settings_with_apps_payload(config_path):
    data = save_settings(config_path, {
        "model": {"url": "https://new.example/v1", "name": "new-model", "api_key": ""},
        "grid": {"mode": "auto", "cols": 20, "rows": 10},
        "apps": [
            {"name": "飞书", "package": "com.example.feishu", "aliases": []},
            {"name": "新应用", "package": "com.example.new", "aliases": []},
        ],
    })
    cfg = Config.load(config_path)
    assert [a.name for a in cfg.apps] == ["飞书", "新应用"]
    assert data["model"]["url"] == "https://new.example/v1"


def test_save_apps_rejects_bad_input(config_path):
    before = config_path.read_bytes()
    with pytest.raises(ValueError, match="不能为空"):
        save_apps(config_path, [{"name": "  ", "package": "com.a.b"}])
    with pytest.raises(ValueError, match="重复"):
        save_apps(config_path, [{"name": "A", "package": ""}, {"name": "A", "package": ""}])
    with pytest.raises(ValueError, match="包名不合法"):
        save_apps(config_path, [{"name": "A", "package": "bad pkg"}])
    assert config_path.read_bytes() == before


def test_public_settings_returns_aliases(config_path):
    save_apps(config_path, [{"name": "哔哩哔哩", "package": "com.bilibili.bilithings",
                             "aliases": ["B站"]}])
    apps = public_settings(config_path)["apps"]
    assert apps[0]["aliases"] == ["B站"]
    assert "checked" not in apps[0]


def test_device_apps_filters_system_and_caches(config_path, monkeypatch):
    import autogui.bridge.adb as adb
    import autogui.dashboard_settings as ds

    calls = []

    class FakeDevice:
        serial = "fake"

        def list_packages(self):
            calls.append(1)
            return sorted(["com.android.systemui", "android.test", "com.google.android.gms",
                           "com.sankuai.meituan.car", "com.bilibili.bilithings",
                           "com.mosi.mosc.onlinemedia.qqmusic"])

    monkeypatch.setattr(adb, "get_device", lambda *a, **k: FakeDevice())
    ds._DEVICE_APPS_CACHE.update(ts=0.0, packages=None)
    r1 = ds.device_apps(config_path)
    assert r1["ok"] is True and r1["cached"] is False and r1["count"] == 3
    assert "com.android.systemui" not in r1["packages"]
    assert "com.sankuai.meituan.car" in r1["packages"]
    r2 = ds.device_apps(config_path)
    assert r2["cached"] is True and len(calls) == 1
    r3 = ds.device_apps(config_path, force=True)
    assert r3["cached"] is False and len(calls) == 2


def test_reload_replaces_old_device_packages_and_marks_current_device(config_path, monkeypatch):
    import autogui.bridge.adb as adb
    import autogui.dashboard_settings as ds

    class Phone:
        serial = "redmi-phone"

        def list_packages(self):
            return ["com.android.systemui", "com.example.music.phone", "com.sankuai.meituan"]

    monkeypatch.setattr(adb, "get_device", lambda *a, **k: Phone())
    ds._DEVICE_APPS_CACHE.update(ts=0.0, packages=None, all_packages=None, serial="")
    refreshed = refresh_app_cache(config_path, [
        {"name": "QQ音乐", "package": "com.example.music", "aliases": []},
        {"name": "美团", "package": "com.sankuai.meituan", "aliases": []},
    ])
    assert refreshed["cleared"] == ["QQ音乐"]
    assert refreshed["count"] == 2
    assert refreshed["saved_count"] == 2
    cfg = Config.load(config_path)
    assert cfg.app_cache_serial == "redmi-phone"
    assert [app.package for app in cfg.apps] == ["", "com.sankuai.meituan"]
    assert "com.example.music" not in config_path.read_text(encoding="utf-8")
    assert refreshed["config_path"] == str(config_path.resolve())

    with pytest.raises(ValueError, match="未安装"):
        save_apps(config_path, [{"name": "QQ音乐", "package": "com.example.music", "aliases": []}],
                  device_serial="redmi-phone")
