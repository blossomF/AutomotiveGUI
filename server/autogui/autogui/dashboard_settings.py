"""本地面板设置；不把 API Key 返回给浏览器。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from autogui.config import Config, GLMConfig


def _model_fields(payload: dict, current: Config) -> tuple[str, str, str]:
    model = payload.get("model") or {}
    if not isinstance(model, dict):
        raise ValueError("model 必须是对象")
    url = str(model.get("url", current.glm.base_url)).strip()
    name = str(model.get("name", current.glm.model)).strip()
    key = str(model.get("api_key") or "").strip()
    parsed = urlparse(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("模型 URL 必须是有效的 API 根地址，不能包含账号、查询或片段")
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError("模型 URL 需要 HTTPS；本机 localhost 可使用 HTTP")
    if not name:
        raise ValueError("模型名称不能为空")
    return url, name, key


def _num_field(raw: dict, key: str, fallback: object, caster) -> object:
    """读取可选数字字段；未填/留空时沿用当前值，非法值直接拒绝。"""
    if key not in raw or raw[key] in ("", None):
        return fallback
    try:
        return caster(raw[key])
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是数字")


def public_settings(config_path: Path) -> dict:
    import yaml

    from autogui.security import is_encrypted, resolve_secret

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cfg = Config.load(config_path)

    def _plain(val: str) -> str:
        if not val:
            return ""
        if is_encrypted(val):
            try:
                return resolve_secret(val)
            except Exception:
                return val
        return val

    glm_raw = raw.get("glm") or {}
    voice_raw = raw.get("voice") or {}
    return {
        "model": {"url": cfg.glm.base_url, "name": cfg.glm.model,
                  "api_key": _plain(str(glm_raw.get("api_key") or "")),
                  "api_key_configured": bool(cfg.glm.api_key),
                  "temperature": cfg.glm.temperature,
                  "max_tokens": cfg.glm.max_tokens,
                  "timeout": cfg.glm.timeout,
                  "max_retries": cfg.glm.max_retries},
        "voice": {
            "appid": str(voice_raw.get("appid") or ""),
            "api_key": str(voice_raw.get("api_key") or ""),
            "api_secret": _plain(str(voice_raw.get("api_secret") or "")),
            "iat_url": str(voice_raw.get("iat_url") or ""),
            "max_seconds": cfg.voice.max_seconds,
            "silence_seconds": cfg.voice.silence_seconds,
            "silence_threshold": cfg.voice.silence_threshold,
            "min_voice_seconds": cfg.voice.min_voice_seconds,
            "agc_target": cfg.voice.agc_target,
            "agc_max_gain": cfg.voice.agc_max_gain,
            "agc_release": cfg.voice.agc_release,
            "noise_rise": cfg.voice.noise_rise,
            "noise_factor": cfg.voice.noise_factor,
        },
        "apps": [{"name": app.name, "package": app.package,
                  "aliases": list(app.aliases or [])} for app in cfg.apps if app.in_scope],
        "app_cache_serial": cfg.app_cache_serial,
        "config_path": str(config_path.resolve()),
    }


def _comment(line: str) -> str:
    """保留配置行尾的 YAML 注释，忽略引号内的 #。"""
    single = double = False
    for i, char in enumerate(line):
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif char == "#" and not single and not double:
            return " " + line[i:].rstrip("\r\n")
    return ""


def _replace_fields(source: str, updates: dict[str, dict[str, object]]) -> str:
    """只改设置字段，保留 config.yaml 的其他行、注释和顺序。"""
    lines = source.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in source else "\n"
    for section, fields in updates.items():
        head = next((i for i, line in enumerate(lines) if line.strip() == f"{section}:"
                     and line.startswith(section)), None)
        if head is None:
            raise ValueError(f"config.yaml 缺少 {section} 段")
        tail = next((i for i in range(head + 1, len(lines))
                     if lines[i].strip() and not lines[i][0].isspace()
                     and not lines[i].lstrip().startswith("#")), len(lines))
        for key, value in fields.items():
            found = next((i for i in range(head + 1, tail)
                          if lines[i].startswith(f"  {key}:")), None)
            encoded = json.dumps(value, ensure_ascii=False)
            if found is None:
                lines.insert(head + 1, f"  {key}: {encoded}{newline}")
                tail += 1
            else:
                lines[found] = f"  {key}: {encoded}{_comment(lines[found])}{newline}"
    return "".join(lines)


_PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_SAFE_SCALAR_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff.\-+]+$")


def _yaml_scalar(value: str) -> str:
    """无特殊字符的标量裸写（与 config.yaml 现有风格一致），否则 JSON 引号。"""
    return value if _SAFE_SCALAR_RE.fullmatch(value) else json.dumps(value, ensure_ascii=False)


def _apps_fields(payload: dict) -> list | None:
    """解析并校验前端提交的 apps 列表；payload 无 apps 键时返回 None。"""
    if "apps" not in payload:
        return None
    raw = payload["apps"]
    if not isinstance(raw, list):
        raise ValueError("apps 必须是列表")
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("apps 每一项必须是对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("应用名称不能为空")
        package = str(item.get("package") or "").strip()
        if package and not _PKG_RE.fullmatch(package):
            raise ValueError(f"包名不合法：{package}（仅允许字母、数字、点、下划线）")
        aliases_raw = item.get("aliases") or []
        if isinstance(aliases_raw, str):
            aliases_raw = [a for a in re.split(r"[,，、;；\s]+", aliases_raw) if a]
        if not isinstance(aliases_raw, list) or not all(isinstance(a, str) for a in aliases_raw):
            raise ValueError(f"应用 {name} 的别名必须是字符串列表")
        aliases = [a.strip() for a in aliases_raw if a.strip()]
        for label in [name, *aliases]:
            normalized = re.sub(r"\s+", "", label).casefold()
            if normalized in seen:
                raise ValueError(f"应用名称或别名重复：{label}")
            seen.add(normalized)
        out.append({
            "name": name,
            "package": package,
            "aliases": aliases,
        })
    return out


def _replace_apps_section(source: str, apps: list) -> str:
    """整段重写 apps: 配置块，其余行与注释保留；段缺失时插入到 log_dir 之前。"""
    lines = source.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in source else "\n"
    entries = []
    for app in apps:
        entries.append(f"  - name: {_yaml_scalar(app['name'])}")
        if app["aliases"]:
            entries.append(f"    aliases: {json.dumps(app['aliases'], ensure_ascii=False)}")
        if app["package"]:
            entries.append(f"    package: {_yaml_scalar(app['package'])}")
    head = next((i for i, line in enumerate(lines) if line.startswith("apps:")), None)
    if head is None:
        pos = next((i for i, line in enumerate(lines) if line.startswith("log_dir:")), len(lines))
        block = ["apps:"] + entries if entries else ["apps: []"]
        lines[pos:pos] = [f"{l}{newline}" for l in block]
        return "".join(lines)
    if lines[head][len("apps:"):].strip():
        lines[head:head + 1] = (["apps:"] + entries) if entries else [f"apps: []{newline}"]
        return "".join(lines)
    tail = next((i for i in range(head + 1, len(lines))
                 if lines[i].strip() and not lines[i][0].isspace()
                 and not lines[i].lstrip().startswith("#")), len(lines))
    if entries:
        lines[head + 1:tail] = [f"{l}{newline}" for l in entries]
    else:
        lines[head] = f"apps: []{newline}"
        del lines[head + 1:tail]
    return "".join(lines)


def _replace_cache_serial(source: str, serial: str) -> str:
    """记录目标应用包名所属设备；无设备验证时清空标记。"""
    lines = source.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in source else "\n"
    entry = f"app_cache_serial: {json.dumps(serial)}{newline}"
    head = next((i for i, line in enumerate(lines) if line.startswith("app_cache_serial:")), None)
    if head is not None:
        lines[head] = entry
    else:
        pos = next((i for i, line in enumerate(lines) if line.startswith("apps:")), len(lines))
        lines.insert(pos, entry)
    return "".join(lines)


def _atomic_write(config_path: Path, updated: str) -> None:
    """先用正式配置解析器验证一次，再同目录原子替换写盘。"""
    import yaml

    Config(**(yaml.safe_load(updated) or {}))
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False,
                                         dir=config_path.parent, prefix=".autogui-settings-") as temp:
            temp.write(updated)
            temp_name = temp.name
        os.replace(temp_name, config_path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _verified_serial(serial: str, apps: list) -> str:
    if not serial:
        return ""
    cached = _DEVICE_APPS_CACHE
    if cached.get("serial") != serial or cached.get("all_packages") is None:
        raise ValueError("设备应用列表已变化，请先重新加载")
    missing = [app["name"] for app in apps
               if app["package"] and app["package"] not in cached["all_packages"]]
    if missing:
        raise ValueError(f"当前设备未安装这些应用的配置包名：{'、'.join(missing)}；请重新加载并修正")
    return serial


def save_apps(config_path: Path, apps: list, device_serial: str | None = None) -> dict:
    """只保存完整应用清单；不动模型和 ASR 字段。"""
    parsed = _apps_fields({"apps": apps})
    if parsed is None:
        raise ValueError("apps 必须是列表")
    updated = _replace_apps_section(config_path.read_text(encoding="utf-8"), parsed)
    if device_serial is not None:
        updated = _replace_cache_serial(updated, _verified_serial(device_serial, parsed))
    _atomic_write(config_path, updated)
    return public_settings(config_path)


_DEVICE_APPS_TTL = 120.0
_DEVICE_APPS_CACHE: dict = {"ts": 0.0, "packages": None, "all_packages": None, "serial": ""}


def device_apps(config_path: Path, force: bool = False) -> dict:
    """从当前设备拉取已安装应用包名（短缓存；与 main.py --packages 同口径过滤系统前缀）。

    供设置页「重新加载」做包名补全与核对。
    """
    cfg = Config.load(config_path)
    from autogui.bridge.adb import get_device

    device = get_device(cfg.device_serial, display=cfg.display_id)
    now = time.time()
    cached = _DEVICE_APPS_CACHE["packages"]
    if (not force and cached is not None and _DEVICE_APPS_CACHE["serial"] == device.serial
            and now - _DEVICE_APPS_CACHE["ts"] < _DEVICE_APPS_TTL):
        return {"ok": True, "packages": cached, "cached": True,
                "count": len(cached), "serial": device.serial}
    packages = device.list_packages()
    interesting = [p for p in packages
                   if not p.startswith(("com.android.", "android.", "com.google."))]
    _DEVICE_APPS_CACHE["ts"] = now
    _DEVICE_APPS_CACHE["packages"] = interesting
    _DEVICE_APPS_CACHE["all_packages"] = set(packages)
    _DEVICE_APPS_CACHE["serial"] = device.serial
    return {"ok": True, "packages": interesting, "cached": False,
            "count": len(interesting), "serial": device.serial}


def refresh_app_cache(config_path: Path, apps: list) -> dict:
    """以当前设备为准全量覆盖目标包名；旧设备的缺失包名清空。"""
    inventory = device_apps(config_path, force=True)
    parsed = _apps_fields({"apps": apps})
    installed = _DEVICE_APPS_CACHE["all_packages"]
    cleared = []
    for app in parsed:
        if app["package"] and app["package"] not in installed:
            cleared.append(app["name"])
            app["package"] = ""
    saved = save_apps(config_path, parsed, device_serial=inventory["serial"])
    return {**inventory, **saved, "ok": True, "cleared": cleared,
            "saved_count": len(saved["apps"])}


def save_settings(config_path: Path, payload: dict) -> dict:
    """校验后原子保存。api_key 和 api_secret 加密存储，其余明文。"""
    current = Config.load(config_path)
    url, name, key = _model_fields(payload, current)
    apps = _apps_fields(payload)
    model_raw = payload.get("model") or {}
    model_updates: dict[str, object] = {
        "base_url": url, "model": name,
        "temperature": _num_field(model_raw, "temperature", current.glm.temperature, float),
        "max_tokens": _num_field(model_raw, "max_tokens", current.glm.max_tokens, int),
        "timeout": _num_field(model_raw, "timeout", current.glm.timeout, float),
        "max_retries": _num_field(model_raw, "max_retries", current.glm.max_retries, int),
    }
    if key:
        from autogui.security import encrypt
        model_updates["api_key"] = encrypt(key)
    voice_raw = payload.get("voice") or {}
    voice_updates: dict[str, object] = {}
    if isinstance(voice_raw, dict):
        from autogui.security import encrypt as _enc
        appid = str(voice_raw.get("appid") or "").strip()
        api_key = str(voice_raw.get("api_key") or "").strip()
        api_secret = str(voice_raw.get("api_secret") or "").strip()
        iat_url = str(voice_raw.get("iat_url") or "").strip()
        if appid:
            voice_updates["appid"] = appid
        if api_key:
            voice_updates["api_key"] = api_key
        if api_secret:
            voice_updates["api_secret"] = _enc(api_secret)
        voice_updates["iat_url"] = iat_url
        voice_updates["max_seconds"] = _num_field(voice_raw, "max_seconds", current.voice.max_seconds, float)
        voice_updates["silence_seconds"] = _num_field(voice_raw, "silence_seconds", current.voice.silence_seconds, float)
        voice_updates["silence_threshold"] = _num_field(voice_raw, "silence_threshold", current.voice.silence_threshold, int)
        voice_updates["min_voice_seconds"] = _num_field(voice_raw, "min_voice_seconds", current.voice.min_voice_seconds, float)
        voice_updates["agc_target"] = _num_field(voice_raw, "agc_target", current.voice.agc_target, float)
        voice_updates["agc_max_gain"] = _num_field(voice_raw, "agc_max_gain", current.voice.agc_max_gain, float)
        voice_updates["agc_release"] = _num_field(voice_raw, "agc_release", current.voice.agc_release, float)
        voice_updates["noise_rise"] = _num_field(voice_raw, "noise_rise", current.voice.noise_rise, float)
        voice_updates["noise_factor"] = _num_field(voice_raw, "noise_factor", current.voice.noise_factor, float)
    updates = {
        "glm": model_updates,
    }
    if voice_updates:
        updates["voice"] = voice_updates
    updated = _replace_fields(config_path.read_text(encoding="utf-8"), updates)
    if apps is not None:
        updated = _replace_apps_section(updated, apps)
        updated = _replace_cache_serial(updated, _verified_serial(str(payload.get("device_serial") or ""), apps))
    _atomic_write(config_path, updated)
    return public_settings(config_path)


def test_model(config_path: Path, payload: dict, client_factory=None) -> dict:
    """验证表单里的未保存值；不保存、不访问设备，单次最小调用。"""
    current = Config.load(config_path)
    url, name, entered_key = _model_fields(payload, current)
    key = entered_key or current.glm.api_key
    if not key:
        return {"ok": False, "message": "请填写 API Key 或先保存本机凭证"}
    from autogui.brain.glm_client import GLMClient
    # 使用任务同样的超时和输出上限，避免推理模型因验证请求额度过低而误报失败。
    cfg = GLMConfig(base_url=url, model=name, api_key=key,
                    timeout=current.glm.timeout, max_retries=0,
                    max_tokens=current.glm.max_tokens)
    started = time.perf_counter()
    try:
        client = client_factory(cfg) if client_factory else GLMClient(cfg)
        reply, _ = client.chat([{"role": "user", "content": "请只回复 OK"}], kind="settings_test")
        if not reply.strip():
            raise ValueError("模型返回空内容")
        return {"ok": True, "message": "模型连接成功", "latency_ms": round((time.perf_counter()-started)*1000)}
    except Exception as exc:
        detail = str(exc).replace(key, "<redacted-api-key>")[:300]
        return {"ok": False, "message": detail, "error_type": type(exc).__name__,
                "latency_ms": round((time.perf_counter()-started)*1000)}


def test_asr(config_path: Path, payload: dict) -> dict:
    """用表单里的（未保存）讯飞凭证做一次 WebSocket 握手验证。

    仅做鉴权握手 + 发送首帧（status=0），不录音、不产生识别结果；
    未安装 websocket-client 时退化为对端 TCP 可达性检查。
    """
    import os
    import socket

    from autogui.voice.asr import DEFAULT_IAT_URL

    current = Config.load(config_path)
    v = payload.get("voice") or {}
    if not isinstance(v, dict):
        raise ValueError("voice 必须是对象")

    def pick(field: str, fallback: str) -> str:
        val = str(v.get(field) or "").strip()
        return val or fallback

    appid = pick("appid", current.voice.appid)
    api_key = pick("api_key", current.voice.api_key)
    api_secret = pick("api_secret", current.voice.api_secret)
    iat_url = pick("iat_url", current.voice.iat_url or os.getenv("IFLYTEK_IAT_URL") or DEFAULT_IAT_URL)

    from autogui.security import is_encrypted, resolve_secret
    if is_encrypted(api_secret):
        api_secret = resolve_secret(api_secret)

    if not (appid and api_key and api_secret):
        return {"ok": False, "message": "请先填写 AppID / API Key / API Secret"}

    parsed = urlparse(iat_url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        return {"ok": False, "message": f"请求地址需为有效的 ws:// 或 wss:// 地址：{iat_url}"}

    started = time.perf_counter()
    try:
        from autogui.voice.asr import _sign_url
        signed = _sign_url(appid, api_key, api_secret, iat_url)
    except Exception as exc:
        return {"ok": False, "message": f"签名失败：{exc}", "error_type": type(exc).__name__,
                "latency_ms": round((time.perf_counter()-started)*1000)}

    # 未装 websocket-client（语音可选依赖）时，退化为 TCP 可达性检查
    try:
        import websocket
    except Exception:
        host, port = parsed.hostname, (parsed.port or (443 if parsed.scheme == "wss" else 80))
        try:
            socket.create_connection((host, port), timeout=6).close()
            return {"ok": True,
                    "message": f"未安装 websocket-client，仅完成 {host}:{port} 网络可达性检查",
                    "latency_ms": round((time.perf_counter()-started)*1000)}
        except Exception as exc2:
            return {"ok": False, "message": f"无法连接 {host}:{port}：{exc2}",
                    "error_type": type(exc2).__name__,
                    "latency_ms": round((time.perf_counter()-started)*1000)}

    try:
        # create_connection 即完成 WebSocket 握手（鉴权在握手阶段校验，
        # 凭证错误会抛 WebSocketBadStatusException）
        ws = websocket.create_connection(signed, timeout=10)
        # 按真实识别协议发送 起始帧(status=0) + 结束帧(status=2)；
        # 讯飞收齐结束帧后才回包，只发空起始帧会 recv 阻塞超时
        ws.send(json.dumps({
            "common": {"app_id": appid},
            "business": {"language": "zh_cn", "domain": "iat", "accent": "mandarin", "dwa": "wpgs"},
            "data": {"status": 0, "format": "audio/L16;rate=16000", "encoding": "raw", "audio": ""},
        }))
        ws.send(json.dumps({
            "common": {"app_id": appid},
            "data": {"status": 2, "format": "audio/L16;rate=16000", "encoding": "raw", "audio": ""},
        }))
        code, message = 0, ""
        while True:
            resp = json.loads(ws.recv())
            code, message = resp.get("code", 0), resp.get("message", "")
            if (resp.get("data") or {}).get("status") == 2:
                break
        ws.close()
        if code == 0:
            return {"ok": True, "message": "ASR 连接成功（鉴权通过）",
                    "latency_ms": round((time.perf_counter()-started)*1000)}
        return {"ok": False, "message": f"ASR 返回错误码 {code}：{message}",
                "latency_ms": round((time.perf_counter()-started)*1000)}
    except Exception as exc:
        detail = str(exc).replace(api_secret, "<redacted-api-secret>")[:300]
        return {"ok": False, "message": detail, "error_type": type(exc).__name__,
                "latency_ms": round((time.perf_counter()-started)*1000)}
