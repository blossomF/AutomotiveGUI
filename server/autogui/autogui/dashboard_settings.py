"""本地面板的模型和网格设置；不把 API Key 返回给浏览器。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from autogui.config import Config, GLMConfig, GridConfig
from autogui.perception.grid import auto_grid_dimensions, grid_dimensions


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


def _grid_fields(payload: dict, current: Config) -> GridConfig:
    raw = payload.get("grid") or {}
    if not isinstance(raw, dict):
        raise ValueError("grid 必须是对象")
    return GridConfig(mode=raw.get("mode", current.grid.mode),
                      cols=raw.get("cols", current.grid.cols),
                      rows=raw.get("rows", current.grid.rows))


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
                  "api_key_configured": bool(cfg.glm.api_key)},
        "grid": {"mode": cfg.grid.mode, "cols": cfg.grid.cols, "rows": cfg.grid.rows},
        "voice": {
            "appid": str(voice_raw.get("appid") or ""),
            "api_key": str(voice_raw.get("api_key") or ""),
            "api_secret": _plain(str(voice_raw.get("api_secret") or "")),
            "iat_url": str(voice_raw.get("iat_url") or ""),
        },
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


def save_settings(config_path: Path, payload: dict) -> dict:
    """校验后原子保存。api_key 和 api_secret 加密存储，其余明文。"""
    current = Config.load(config_path)
    url, name, key = _model_fields(payload, current)
    grid = _grid_fields(payload, current)
    model_updates: dict[str, object] = {"base_url": url, "model": name}
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
    updates = {
        "glm": model_updates,
        "grid": {"mode": grid.mode, "cols": grid.cols, "rows": grid.rows},
    }
    if voice_updates:
        updates["voice"] = voice_updates
    updated = _replace_fields(config_path.read_text(encoding="utf-8"), updates)
    # 再用正式配置解析器验证一次，写盘采用同目录原子替换。
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


def grid_preview(config_path: Path, screen_size: tuple[int, int] | None = None) -> dict:
    """按实际截图预览；设备离线时由调用方显示不可用。"""
    cfg = Config.load(config_path)
    if screen_size is None:
        from autogui.bridge.adb import get_device, png_size
        device = get_device(cfg.device_serial, display=cfg.display_id)
        screen_size = png_size(device.screenshot())
    if not screen_size:
        raise RuntimeError("无法从截图读取屏幕尺寸")
    width, height = screen_size
    auto_cols, auto_rows = auto_grid_dimensions(width, height)
    cols, rows = grid_dimensions(width, height, mode=cfg.grid.mode,
                                 cols=cfg.grid.cols, rows=cfg.grid.rows)
    return {"width": width, "height": height, "auto": {"cols": auto_cols, "rows": auto_rows},
            "active": {"mode": cfg.grid.mode, "cols": cols, "rows": rows,
                       "cell_width": round(width / cols, 1), "cell_height": round(height / rows, 1)}}
