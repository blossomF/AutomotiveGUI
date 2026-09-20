"""讯飞流式语音听写（WebSocket IAT）——可插拔的语音任务入口。

章程 §4.6：语音只作为任务入口（说话 → 文字 → Agent 执行），
禁止 TTS 输出、多轮语音对话、语音打断；Agent 大脑不依赖本模块。

用法：
    python main.py --voice-test                # 麦克风 + ASR，只打印识别文本（不需要车机、不需要 GLM）
    python main.py --voice                     # 麦克风 + ASR，把识别文本作为任务执行
    python main.py --voice --voice-seconds 20  # 自定义最长录音时长

凭证：
    优先读取 config.yaml 的 voice: 段（appid / api_key / api_secret）；
    留空时回退环境变量 IFLYTEK_APPID / IFLYTEK_API_KEY / IFLYTEK_API_SECRET。
接口：
    默认讯飞公开流式听写 wss://iat-api.xfyun.cn/v2/iat；
    若组委会 demo 使用其他网关地址，config.yaml 的 voice.iat_url 或环境变量 IFLYTEK_IAT_URL 覆盖。
音频：
    16kHz / 单声道 / 16-bit PCM；1280 字节（80ms）一帧、约 80ms 间隔流式发送；
    录音在检测到 0.8s 连续静音（尾部）或到达时长上限时自动停止。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from array import array
from email.utils import formatdate
from urllib.parse import quote, urlparse

DEFAULT_IAT_URL = "wss://iat-api.xfyun.cn/v2/iat"
SAMPLE_RATE = 16000
CHUNK = 1280  # 字节 = 80ms @16kHz/16bit


class VoiceError(RuntimeError):
    """语音通道错误（凭证缺失 / 麦克风失败 / ASR 接口错误）。"""


# ── 凭证与签名 ────────────────────────────────────────────────────────────────


def resolve_credentials(cfg) -> tuple[str, str, str, str]:
    """从 Config 取 (appid, api_key, api_secret, iat_url)；缺失时抛 VoiceError。"""
    v = cfg.voice
    if not (v.appid and v.api_key and v.api_secret):
        raise VoiceError(
            "讯飞 ASR 凭证未配置：请在 config.yaml 的 voice: 段填写 appid/api_key/api_secret，"
            "或设置环境变量 IFLYTEK_APPID / IFLYTEK_API_KEY / IFLYTEK_API_SECRET"
        )
    url = v.iat_url or os.getenv("IFLYTEK_IAT_URL") or DEFAULT_IAT_URL
    return v.appid, v.api_key, v.api_secret, url


def _sign_url(appid: str, api_key: str, api_secret: str, url: str) -> str:
    """讯飞 WebSocket 鉴权：base64(HMAC-SHA256(APISecret, host\ndate\nGET path))。"""
    host = urlparse(url).netloc
    path = urlparse(url).path or "/v2/iat"
    date = formatdate(timeval=None, usegmt=True)  # RFC1123 GMT 时间
    signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    signature_digest = hmac.new(
        api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(signature_digest).decode("utf-8")
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    return (
        f"{url}?authorization={quote(authorization)}"
        f"&date={quote(date)}&host={quote(host)}&appid={appid}"
    )


# ── 麦克风录音 ────────────────────────────────────────────────────────────────


def _rms(chunk: bytes) -> int:
    """16-bit 小端 PCM 的均方根幅度，用于静音判定。"""
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0
    return int((sum(s * s for s in samples) / len(samples)) ** 0.5)


def _record_until_silence(
    max_seconds: float = 15.0,
    silence_seconds: float = 0.8,
    silence_threshold: int = 300,
    device: int | None = None,
) -> bytes:
    """录音直到尾部连续静音或时长上限；开头静音不纳入返回数据。"""
    import sounddevice as sd

    block = SAMPLE_RATE // 10  # 100ms/块
    chunks: list[bytes] = []
    started = False
    silence_start: float | None = None
    deadline = time.time() + max_seconds

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=block, device=device
    ) as stream:
        while time.time() < deadline:
            data, _overflowed = stream.read(block)
            chunk = bytes(data)
            now = time.time()
            if _rms(chunk) >= silence_threshold:
                started = True
                silence_start = None
            elif started:
                if silence_start is None:
                    silence_start = now
                elif now - silence_start >= silence_seconds:
                    break
            if started:
                chunks.append(chunk)

    if not chunks:
        raise VoiceError(
            "未检测到语音（全程静音）。请靠近麦克风重试；"
            "可用 `python -m sounddevice` 查看麦克风列表，并用 --voice-device 指定"
        )
    return b"".join(chunks)


# ── ASR 识别 ─────────────────────────────────────────────────────────────────


def _frame_text(data: dict) -> str:
    """从一帧响应里取出当前分片的文字（逐帧拼接即整句）。"""
    parts: list[str] = []
    for ws in (((data.get("data") or {}).get("result")) or {}).get("ws", []):
        for cw in ws.get("cw", []):
            parts.append(cw.get("w", ""))
    return "".join(parts)


def recognize(audio: bytes, appid: str, api_key: str, api_secret: str, iat_url: str = DEFAULT_IAT_URL) -> str:
    """把 16kHz/mono/int16 PCM 音频流经讯飞 IAT，返回识别文本。"""
    import websocket

    ws = websocket.create_connection(_sign_url(appid, api_key, api_secret, iat_url), timeout=15)

    try:
        business = {"language": "zh_cn", "domain": "iat", "accent": "mandarin"}
        first = audio[:CHUNK]
        ws.send(json.dumps({
            "common": {"app_id": appid},
            "business": business,
            "data": {
                "status": 0,
                "format": f"audio/L16;rate={SAMPLE_RATE}",
                "encoding": "raw",
                "audio": base64.b64encode(first).decode("ascii"),
            },
        }))
        rest = audio[CHUNK:]
        for i in range(0, len(rest), CHUNK):
            ws.send(json.dumps({
                "common": {"app_id": appid},
                "data": {
                    "status": 1,
                    "format": f"audio/L16;rate={SAMPLE_RATE}",
                    "encoding": "raw",
                    "audio": base64.b64encode(rest[i : i + CHUNK]).decode("ascii"),
                },
            }))
            time.sleep(0.08)
        ws.send(json.dumps({
            "common": {"app_id": appid},
            "data": {"status": 2, "format": f"audio/L16;rate={SAMPLE_RATE}", "encoding": "raw", "audio": ""},
        }))

        text = ""
        while True:
            data = json.loads(ws.recv())
            if data.get("code") != 0:
                raise VoiceError(f"ASR 接口错误 code={data.get('code')} sid={data.get('sid')} message={data.get('message')}")
            text += _frame_text(data)
            if (data.get("data") or {}).get("status") == 2:
                break
        return text
    finally:
        ws.close()


# ── 对外入口 ─────────────────────────────────────────────────────────────────


def transcribe_from_mic(
    cfg,
    max_seconds: float | None = None,
    device: int | None = None,
) -> str:
    """麦克风录音 → 讯飞 ASR → 返回识别文本。失败抛 VoiceError。

    时长/静音参数默认取 config.yaml 的 voice: 段；max_seconds 显式传参优先。
    """
    v = cfg.voice
    max_seconds = max_seconds or v.max_seconds
    resolve_credentials(cfg)  # 快速失败：凭证缺失不要等录完才发现
    print(f"🎤 开始录音（说完停顿约 {v.silence_seconds:.1f}s 自动停止，上限 {max_seconds:.0f}s）…")
    audio = _record_until_silence(max_seconds, v.silence_seconds, v.silence_threshold, device)
    print(f"   录音 {len(audio) / 2 / SAMPLE_RATE:.1f}s，正在识别…")
    appid, api_key, api_secret, url = resolve_credentials(cfg)
    return recognize(audio, appid, api_key, api_secret, url)
