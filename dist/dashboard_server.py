#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutomotiveGUI 轻量可视化面板（原 LightGUIAgent 的 dashboard，适配进本框架）。

本地 HTTP 服务，仅依赖 Python 标准库（零额外依赖）。读取 AutomotiveGUI 的
DecisionLog（``artifacts/logs/<任务>-<时间戳>.jsonl``，每行一条
``{t,step,phase,msg,**指标}`` 决策链事件），回放「看到→决定→做→验证」的
完整链路，并支持下指令页实时轮询「正在运行」的任务。

两个页面（职责分离）：
    http://127.0.0.1:8777/       —— 下指令（简洁：大圣喵 + 输入框 + 一行状态）
    http://127.0.0.1:8777/logs   —— 任务日志（任务选择 + 统计卡 + 决策链回放，支持 ?task= 深链）

启动（GLM_API_KEY 已注入环境变量即可，脚本位置不拘——路径会自动锚定到框架的 server/autogui）：
    python dist/dashboard_server.py            # 默认 http://127.0.0.1:8777（dist 布局）
    python dist/dashboard_server.py --port 9000
    python dist/dashboard_server.py --host 0.0.0.0   # 允许同网其它设备访问

数据来源：
    artifacts/logs/<任务>-<时间戳>.jsonl   —— 决策链轨迹（perceive/route/decide/act/verify…）
注意：本框架不在每步落盘截图，故面板只回放文本决策链，不显示逐步截图。
不侵入 agent 主流程，纯只读回放。
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# —— 定位队友框架入口（含 main.py 的 server/autogui）与决策链日志目录 ——
# 本文件可能位于 server/autogui/（开发期）或 dist/（分发期，dist 在项目根下），
# 两种情况都向上查找「包含 main.py 的 server/autogui 目录」，从而把路径锚定到
# 脚本真实位置，而不是依赖运行时的工作目录（CWD）。
def _resolve_autogui() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        here,                                # 脚本就在 server/autogui
        here.parent / "server" / "autogui",  # 脚本在 dist（dist 位于项目根）
        here / "server" / "autogui",
    ]
    for c in candidates:
        if (c / "main.py").exists():
            return c
    return candidates[1]                     # 兜底：按 dist 布局推断


AUTOGUI_DIR = _resolve_autogui()
PROJECT_ROOT = AUTOGUI_DIR          # 启动 main.py 时的 CWD（= server/autogui）
LOGS_ROOT = AUTOGUI_DIR / "artifacts" / "logs"   # 决策链日志（扁平 *.jsonl）
CONFIG_PATH = AUTOGUI_DIR / "config.yaml"
_SETTINGS_LOCK = threading.Lock()

# 当前由面板拉起的运行进程（同一时刻最多一个）
_RUN_LOCK = threading.Lock()
_RUNNING_PROC = None

# —— 语音录入（讯飞 ASR，复用 autogui.voice.asr.transcribe_from_mic）——
# Config 懒加载：首次 /api/voice 时才加载（避免没配凭证时面板起不来）。
# sounddevice 不能并发录音，_VOICE_LOCK 保证同一时刻只有一个录音在跑。
_CFG = None
_CFG_LOCK = threading.Lock()
_VOICE_LOCK = threading.Lock()

# —— 流式语音（浏览器边录边发 → 转发讯飞 IAT，识别与录音并行）——
_VOICE_STREAMS = {}
_VOICE_STREAMS_LOCK = threading.Lock()
_VOICE_STREAM_TTL = 180.0   # 会话空闲超过该时长自动回收


def _voice_stream_cleanup():
    """回收超时/已结束的流式会话，防止泄漏。"""
    now = time.time()
    with _VOICE_STREAMS_LOCK:
        dead = [s for s, v in _VOICE_STREAMS.items()
                if now - v["t0"] > _VOICE_STREAM_TTL or v.get("done")]
        for sid in dead:
            item = _VOICE_STREAMS.pop(sid, None)
            if item is not None:
                try:
                    item["session"].close()
                except Exception:
                    pass


def _voice_stream_get(sid):
    with _VOICE_STREAMS_LOCK:
        return _VOICE_STREAMS.get(sid)


def _voice_stream_create(cfg) -> str:
    """建立讯飞 IAT 流式会话，返回会话 ID。"""
    from autogui.voice.asr import VoiceSession, resolve_credentials
    appid, api_key, api_secret, url = resolve_credentials(cfg)
    sess = VoiceSession(appid, api_key, api_secret, url)
    sid = f"vs{int(time.time() * 1000)}"
    with _VOICE_STREAMS_LOCK:
        _VOICE_STREAMS[sid] = {"session": sess, "t0": time.time(), "done": False,
                               "frames": 0, "bytes": 0}
    print(f"🎤 流式会话 {sid} 已建立（连接 {sess.conn_seconds:.2f}s）")
    return sid

# —— 空闲自动退出（供启动器实现「关掉网页 = 关掉服务」）——
# 页面每 3 秒发一次 /api/heartbeat；看门狗发现心跳停了就退出。
# 默认 0 = 不启用，手动 `python dashboard_server.py` 行为完全不变。
_IDLE_LIMIT = 0.0
_LAST_SEEN = time.time()


def _touch() -> None:
    """记录一次「页面还活着」的信号（任何请求都算）。"""
    global _LAST_SEEN
    _LAST_SEEN = time.time()


def _idle_monitor(server) -> None:
    """看门狗线程：心跳超时且没有任务在跑 → 关闭服务。"""
    global _LAST_SEEN
    while True:
        time.sleep(1.0)
        if _IDLE_LIMIT <= 0:
            continue
        # 有任务在跑就不自杀，免得把正在执行的任务掐死
        if _RUNNING_PROC is not None and _RUNNING_PROC.poll() is None:
            _touch()
            continue
        if time.time() - _LAST_SEEN > _IDLE_LIMIT:
            print(f"⏹ 页面已关闭超过 {_IDLE_LIMIT:.0f} 秒，服务自动退出。")
            server.shutdown()
            return


# ============================================================================
# 数据解析
# ============================================================================
def list_tasks() -> list:
    """列出所有任务日志（扁平 *.jsonl），返回概要列表（按时间倒序）。

    适配 AutomotiveGUI 的 DecisionLog 格式：每个任务一个
    ``artifacts/logs/<任务>-<时间戳>.jsonl``，每行是
    ``{"t":..,"step":..,"phase":..,"msg":..,**指标}``。
    """
    if not LOGS_ROOT.exists():
        return []
    out = []
    for jl in sorted(LOGS_ROOT.glob("*.jsonl"), reverse=True):
        sid = jl.stem
        status = "running"
        total_steps = 0
        total_tokens = 0
        total_time = 0.0
        try:
            lines = [ln for ln in jl.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for line in lines:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("phase") == "finish":
                    status = "done" if ev.get("success", True) else "aborted"
                    total_time = float(ev.get("elapsed", 0.0) or 0.0)
                    usage = ev.get("usage") or {}
                    total_tokens = int(usage.get("total_tokens", 0) or 0)
                    total_steps = int(ev.get("steps", 0) or 0)
            if status == "running":
                # 没有 finish 事件：用事件条数近似步数
                total_steps = len(lines)
        except Exception:
            pass
        out.append(
            {
                "task_dir": sid,
                "session_id": sid,
                "task": sid,
                "status": status,
                "total_steps": total_steps,
                "total_time": round(total_time, 1),
                "total_cost": 0.0,
                "total_tokens": total_tokens,
            }
        )
    return out


def get_task_detail(task_dir: str) -> dict:
    """解析单个任务的完整决策链（扁平 JSONL），聚合成前端友好的结构。

    每一条事件（perceive/route/decide/act/verify/recovery/guard/finish）
    渲染为时间线的一张卡片。AutomotiveGUI 不存截图，故 image 始终为 None。
    """
    p = LOGS_ROOT / f"{task_dir}.jsonl"
    if not p.exists():
        return None
    events = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except Exception:
                pass

    status = "running"
    finish_usage = {}
    finish_steps = 0
    steps = []
    prev_t = 0.0
    for idx, ev in enumerate(events):
        phase = ev.get("phase", "log")
        msg = ev.get("msg", "")
        step_no = ev.get("step")
        step_no = step_no if isinstance(step_no, int) else idx
        # finish 事件只用于汇总状态，不单独成卡
        if phase == "finish":
            status = "done" if ev.get("success", True) else "aborted"
            finish_usage = ev.get("usage") or {}
            # 真实步数取自 finish 的顶层 steps（与 list_tasks 口径一致）；
            # 注意 usage 里没有 steps 字段，别去 usage 里找。
            finish_steps = int(ev.get("steps", 0) or 0)
            continue
        # 事件自带的指标（路由/屏幕指纹/调用次数等），用于卡片 detail
        metrics = {k: v for k, v in ev.items()
                   if k not in ("t", "step", "phase", "msg")}
        # token 解析（兼容 int 或 {input,output}）
        tok = metrics.get("tokens")
        if isinstance(tok, dict):
            token_d = {"input": int(tok.get("input", 0) or 0),
                       "output": int(tok.get("output", 0) or 0)}
        elif isinstance(tok, (int, float)):
            token_d = {"input": int(tok), "output": 0}
        else:
            token_d = {"input": 0, "output": 0}
        t = float(ev.get("t", 0.0) or 0.0)
        dt = round(t - prev_t, 2) if prev_t else 0.0
        prev_t = t
        action = {"action": phase, "summary": msg, "explain": msg, **metrics}
        steps.append({
            "idx": idx,              # 唯一事件序号：前端用它做 key，避免同 graph step 下多事件互相覆盖
            "step": step_no,         # graph step 编号（JSONL 里的 step 字段）
            "action": action,
            "inference_time": 0,
            "execution_time": 0,
            "total_time": dt,
            "tokens": token_d,
            "evidence": None,
            "image": None,           # 队友框架不存截图
            "noop": False,
        })

    total_tokens = int(finish_usage.get("total_tokens", 0) or 0)
    if not total_tokens:
        total_tokens = sum(s["tokens"]["input"] + s["tokens"]["output"] for s in steps)
    total_time = round(float(finish_usage.get("elapsed", prev_t) or prev_t), 1)
    avg = (sum(s["total_time"] for s in steps) / len(steps)) if steps else 0.0
    return {
        "task_dir": task_dir,
        "session_id": task_dir,
        "task": task_dir,
        "status": status,
        "stats": {
            "total_steps": finish_steps or len(steps),
            "total_tokens": total_tokens,
            "avg_step": round(avg, 2),
        },
        "steps": steps,
    }


def get_live(task_dir: str) -> dict:
    """轻量版：只回「最新一步 + 计数」，供下指令页每 2 秒轮询刷实时卡片。

    为什么不直接复用 /api/task/<dir>：那个会把整条轨迹（每步 action / evidence /
    图片 URL）全序列化，步骤一多响应就是几十 KB，2 秒一次纯属白耗。这里只切最后一步。
    """
    detail = get_task_detail(task_dir)
    if not detail:
        return {"ok": False, "error": "task not found"}
    steps = detail.get("steps") or []
    last = None
    if steps:
        s = steps[-1]
        last = {k: s.get(k) for k in (
            "idx", "step", "action", "image", "total_time", "execution_time",
            "inference_time", "tokens", "noop", "evidence",
        )}
    return {
        "ok": True,
        "task_dir": detail["task_dir"],
        "task": detail["task"],
        "status": detail["status"],
        "stats": detail["stats"],
        "last": last,
    }


# ============================================================================
# 语音录入（讯飞 ASR）：录音 + 识别，返回 (text, error)。阻塞约 3~20s。
# 复用 autogui.voice.asr.transcribe_from_mic——它内部已含 sounddevice 录音
# （尾部静音自动停）+ 讯飞 IAT 流式识别。Config 懒加载，凭证缺失会抛 VoiceError。
# ============================================================================
def _load_cfg():
    """懒加载 autogui Config；首次 /api/voice 时加载。失败抛异常。"""
    global _CFG
    if _CFG is not None:
        return _CFG
    with _CFG_LOCK:
        if _CFG is None:
            if str(AUTOGUI_DIR) not in sys.path:
                sys.path.insert(0, str(AUTOGUI_DIR))
            from autogui.config import Config
            _CFG = Config.load()
    return _CFG


def _settings_api():
    if str(AUTOGUI_DIR) not in sys.path:
        sys.path.insert(0, str(AUTOGUI_DIR))
    from autogui import dashboard_settings
    return dashboard_settings


def launch_voice():
    """录音 + 讯飞 ASR，返回 (text, error)。阻塞约 3~20s（录音+识别）。"""
    cfg = _load_cfg()
    from autogui.voice.asr import VoiceError, transcribe_from_mic
    t0 = time.time()
    try:
        text = transcribe_from_mic(cfg).strip()
    except VoiceError as e:
        print(f"   ✗ 语音链路失败（{time.time() - t0:.1f}s）：{e}")
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ 语音链路异常（{time.time() - t0:.1f}s）：{type(e).__name__}: {e}")
        return None, f"语音识别失败：{e}"
    print(f"   ✓ 语音识别完成（{time.time() - t0:.1f}s，{len(text)} 字）：{text[:80]}")
    return text, None


# ============================================================================
# 任务启动 / 停止（从面板拉起 main.py 子进程）
# ============================================================================
def launch_task(task: str):
    """在 server/autogui 下启动 `main.py <task>`，返回 (新 task_dir, error)。"""
    global _RUNNING_PROC
    task = (task or "").strip()
    if not task:
        return None, "任务内容为空"
    with _RUN_LOCK:
        if _RUNNING_PROC is not None and _RUNNING_PROC.poll() is None:
            return None, "已有任务在运行中，请先停止"
        before = {p.stem for p in LOGS_ROOT.glob("*.jsonl")}
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = LOGS_ROOT / f"dashboard_run_{ts}.log"
        try:
            logf = open(log_path, "w", encoding="utf-8", buffering=1)
        except Exception:
            logf = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "main.py", task],
                cwd=str(PROJECT_ROOT),
                stdout=logf,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"启动失败：{e}"
        _RUNNING_PROC = proc
    # 轮询等待新任务日志出现。冷启动在这台重依赖车机上实测可达 60s+（import langgraph
    # 等重模块 + 首帧慢），旧窗口 25s 会把"还在启动"误判成失败、但 main.py 其实在正常跑。
    # 窗口放到 120s；同时若 main.py 提前退出（真报错）立即失败并带上日志尾部，不干等。
    deadline_ts = time.time() + 120.0
    while time.time() < deadline_ts:
        now = sorted(p.stem for p in LOGS_ROOT.glob("*.jsonl"))
        new = [n for n in now if n not in before]
        if new:
            return new[-1], None
        if proc.poll() is not None:
            tail = ""
            try:
                if log_path.exists():
                    tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-15:])
            except Exception:
                pass
            return None, (f"main.py 启动后即退出（code={proc.returncode}）。"
                          f"日志尾部：\n{tail or '(空)'}")
        time.sleep(0.5)
    return None, "启动超时：120s 内未检测到新任务日志（冷启动过慢/设备离线/main.py 报错，详见 dashboard_run 日志）"


def stop_task():
    """终止面板拉起的运行进程（含子进程树），返回是否成功停止。"""
    global _RUNNING_PROC
    with _RUN_LOCK:
        proc = _RUNNING_PROC
        if proc is None or proc.poll() is not None:
            return False
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _RUNNING_PROC = None
        return True


# ============================================================================
# HTTP Handler
# ============================================================================
class Handler(BaseHTTPRequestHandler):
    def _settings_allowed(self) -> bool:
        # 设置页可修改模型目标和凭证；即使面板以 0.0.0.0 启动，也只允许本机。
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        origin = self.headers.get("Origin")
        return not origin or urllib.parse.urlparse(origin).netloc == self.headers.get("Host")

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        _touch()                     # 有请求 = 页面还在
        try:
            if path in ("/", "/index.html"):
                self._send(200, HTML_RUN.encode("utf-8"), "text/html; charset=utf-8")
            elif path in ("/logs", "/logs.html"):
                self._send(200, HTML_LOGS.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/settings":
                if not self._settings_allowed():
                    self._send_json({"error": "设置页仅允许本机访问"}, 403)
                else:
                    self._send(200, HTML_SETTINGS.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/settings":
                if not self._settings_allowed():
                    self._send_json({"error": "设置仅允许本机访问"}, 403)
                else:
                    self._send_json(_settings_api().public_settings(CONFIG_PATH))
            elif path == "/api/settings/grid-preview":
                if not self._settings_allowed():
                    self._send_json({"error": "设置仅允许本机访问"}, 403)
                else:
                    try:
                        self._send_json({"ok": True, **_settings_api().grid_preview(CONFIG_PATH)})
                    except Exception as e:
                        self._send_json({"ok": False, "error": str(e)})
            elif path == "/api/tasks":
                self._send_json(list_tasks())
            elif path.startswith("/api/task/"):
                task_dir = urllib.parse.unquote(path[len("/api/task/"):])
                detail = get_task_detail(task_dir)
                if detail is None:
                    self._send_json({"error": "task not found"}, 404)
                else:
                    self._send_json(detail)
            elif path == "/api/live":
                task_dir = qs.get("task", [""])[0]
                self._send_json(get_live(task_dir))
            elif path == "/api/heartbeat":
                # 页面心跳：仅刷新活跃时间（_touch 已在 do_GET 开头做过）
                self._send_json({"ok": True})
            elif path == "/image":
                task_dir = qs.get("task", [""])[0]
                file = qs.get("file", [""])[0]
                p = (LOGS_ROOT / task_dir / "images" / file).resolve()
                # 防目录穿越
                if not str(p).startswith(str(LOGS_ROOT.resolve())) or not p.exists():
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                self._send(200, p.read_bytes(), "image/jpeg")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        global _LAST_SEEN, _CFG
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        _touch()                     # 有请求 = 页面还在
        try:
            if path in ("/api/settings", "/api/settings/test-model", "/api/settings/test-asr",
                        "/api/settings/apps-device", "/api/settings/apps"):
                if not self._settings_allowed():
                    self._send_json({"ok": False, "error": "设置仅允许本机访问"}, 403)
                    return
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    self._send_json({"ok": False, "error": "需要 application/json"}, 415)
                    return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > 16384:
                self._send_json({"ok": False, "error": "设置请求过大"}, 413)
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            if path == "/api/closing":
                # 页面正在关闭：把活跃时间往前拨，2 秒后即可退出。
                # 若还有别的标签页在心跳，会立刻把它刷回来，不会误杀。
                _LAST_SEEN = time.time() - max(0.0, _IDLE_LIMIT - 2.0)
                self._send_json({"ok": True})
            elif path == "/api/run":
                task_dir, err = launch_task(payload.get("task", ""))
                if err:
                    self._send_json({"ok": False, "error": err}, 400)
                else:
                    self._send_json({"ok": True, "task_dir": task_dir})
            elif path == "/api/stop":
                self._send_json({"ok": True, "stopped": stop_task()})
            elif path == "/api/voice":
                # 录音 + ASR 阻塞调用；sounddevice 不能并发，加锁防重入
                if not _VOICE_LOCK.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "已有录音在进行中，请等结束"}, 409)
                    return
                try:
                    text, err = launch_voice()
                    if err:
                        self._send_json({"ok": False, "error": err}, 400)
                    else:
                        self._send_json({"ok": True, "text": text or ""})
                finally:
                    _VOICE_LOCK.release()
            elif path == "/api/voice/stream/start":
                # 流式语音：建立讯飞 IAT 会话（浏览器随后边录边发），回传停止逻辑所需配置
                _voice_stream_cleanup()
                try:
                    cfg = _load_cfg()
                    sid = _voice_stream_create(cfg)
                    v = cfg.voice
                    self._send_json({"ok": True, "session": sid, "settings": {
                        "max_seconds": v.max_seconds,
                        "silence_seconds": v.silence_seconds,
                        "silence_threshold": v.silence_threshold,
                        "min_voice_seconds": v.min_voice_seconds,
                        "agc_target": v.agc_target,
                        "agc_max_gain": v.agc_max_gain,
                        "agc_release": v.agc_release,
                        "noise_rise": v.noise_rise,
                        "noise_factor": v.noise_factor,
                    }})
                except Exception as e:  # noqa: BLE001
                    self._send_json({"ok": False, "error": str(e)}, 400)
            elif path.startswith("/api/voice/stream/"):
                parts = path.split("/")
                if len(parts) == 6 and parts[1] == "api" and parts[4] in ("chunk", "stop", "abort"):
                    action, sid = parts[4], parts[5]
                else:
                    action, sid = "", ""
                if not sid:
                    self._send_json({"ok": False, "error": "未知语音流操作"}, 400)
                    return
                _voice_stream_cleanup()
                item = _voice_stream_get(sid)
                if item is None:
                    self._send_json({"ok": False, "error": "流式会话不存在或已过期"}, 404)
                    return
                sess = item["session"]
                if action == "chunk":
                    pcm = raw if len(raw) > 2 else b""
                    if len(pcm) > 16384:
                        self._send_json({"ok": False, "error": "音频帧过大"}, 413)
                        return
                    item["frames"] += 1
                    item["bytes"] += len(pcm)
                    if item["frames"] == 1:
                        print(f"   首帧到达 {sid}：{len(pcm)} 字节")
                    sess.send_audio(pcm)
                    st, err = sess.state()
                    self._send_json({"ok": True, "partial": sess.partial, "state": st, "error": err})
                elif action == "stop":
                    try:
                        text = sess.finish(timeout=10.0)
                    except Exception as e:  # noqa: BLE001
                        self._send_json({"ok": False, "error": str(e)}, 400)
                        return
                    finally:
                        with _VOICE_STREAMS_LOCK:
                            _VOICE_STREAMS.pop(sid, None)
                    print(f"   ✓ 流式识别完成：{item['frames']} 帧/{item['bytes']} 字节 | {len(text)} 字 | {text[:80]}")
                    self._send_json({"ok": True, "text": text})
                else:  # abort（body 可为空：浏览器 sendBeacon 使用）
                    sess.close()
                    with _VOICE_STREAMS_LOCK:
                        _VOICE_STREAMS.pop(sid, None)
                    print(f"   ✗ 流式会话中止：{item['frames']} 帧/{item['bytes']} 字节")
                    self._send_json({"ok": True})
            elif path == "/api/settings":
                try:
                    with _SETTINGS_LOCK:
                        saved = _settings_api().save_settings(CONFIG_PATH, payload)
                    with _CFG_LOCK:
                        _CFG = None   # 保存后丢弃缓存，语音等路径立即用新值
                    self._send_json({"ok": True, **saved})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, 400)
            elif path == "/api/settings/test-model":
                try:
                    self._send_json(_settings_api().test_model(CONFIG_PATH, payload))
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, 400)
            elif path == "/api/settings/test-asr":
                try:
                    self._send_json(_settings_api().test_asr(CONFIG_PATH, payload))
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, 400)
            elif path == "/api/settings/apps-device":
                try:
                    if "apps" in payload:
                        with _SETTINGS_LOCK:
                            result = _settings_api().refresh_app_cache(CONFIG_PATH, payload["apps"])
                    else:
                        result = _settings_api().device_apps(CONFIG_PATH, force=bool(payload.get("force")))
                    self._send_json(result)
                except Exception as e:
                    self._send_json({"ok": False, "error": f"获取车机应用列表失败：{e}"}, 500)
            elif path == "/api/settings/apps":
                try:
                    with _SETTINGS_LOCK:
                        saved = _settings_api().save_apps(
                            CONFIG_PATH, payload.get("apps") or [],
                            device_serial=str(payload.get("device_serial") or ""),
                        )
                    self._send_json({"ok": True, **saved})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, 400)
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    # 静默日志（避免每请求刷屏）
    def log_message(self, fmt, *args):
        pass


# ============================================================================
# 前端页面（内嵌，无外部 CDN 依赖）
# ============================================================================
# ============================================================================
# 前端页面（内嵌，无外部 CDN 依赖；下指令、日志、设置共用 CSS）
#   /      下指令 —— 只留大圣喵 + 输入框，保持简洁
#   /logs  任务日志 —— 任务选择 + 统计卡 + 决策链回放
# ============================================================================
BASE_CSS = r"""
  * { box-sizing: border-box; }
  html, body { height:100%; }
  body { margin:0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background:#f4f6f8; color:#1f2933; display:flex; flex-direction:column; height:100vh; }
  /* 顶部细条 */
  header { background:#1f2933; color:#fff; padding:10px 16px; display:flex; align-items:center;
           gap:10px; flex:0 0 auto; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header .sub { font-size:12px; color:#9aa5b1; }
  header .spacer { flex:1; }
  header select { font-size:13px; padding:5px 8px; border-radius:7px; border:1px solid #4b5563;
                  background:#374151; color:#fff; max-width:320px; }
  header .mini { font-size:13px; padding:5px 10px; border-radius:7px; border:none;
                 background:#374151; color:#fff; cursor:pointer; text-decoration:none;
                 display:inline-flex; align-items:center; }
  header .mini:hover { background:#4b5563; }
  button { font-family:inherit; }
  /* 注意：必须带 .dock-inner 前缀压过 .dock-inner button 的白底，否则白字白底看不见 */
  .dock-inner button.primary { background:#2563eb; color:#fff; border-color:#2563eb; }
  .dock-inner button.primary:disabled { opacity:.6; cursor:default; }
  /* 语音录入键：入口先留着，按下走 startVoiceInput()（ASR 待接） */
  #voiceBtn { width:44px; padding:10px 0; color:#52606d;
              display:inline-flex; align-items:center; justify-content:center; }
  #voiceBtn svg { display:block; }
  #voiceBtn:hover { border-color:#2563eb; color:#2563eb; background:#eff6ff; }
  #voiceBtn:active { transform:scale(.94); }
  /* 语音电平条：录音时在麦克风键旁显示 */
  .voice-level { width:5px; height:26px; background:#e5e7eb; border-radius:3px;
                 overflow:hidden; align-self:center; display:none; }
  .voice-level.show { display:flex; flex-direction:column; justify-content:flex-end; }
  #voiceLevelFill { width:100%; height:0%; background:#22c55e; border-radius:3px;
                    transition:height 80ms linear; }
  #voiceBtn.recording { background:#fee2e2; border-color:#ef4444; color:#dc2626;
                        animation: voicePulse 1.2s ease-in-out infinite; }
  @keyframes voicePulse { 0%,100% { box-shadow:0 0 0 0 rgba(239,68,68,.4); }
                          50%    { box-shadow:0 0 0 8px rgba(239,68,68,0); } }
  /* 轻提示气泡（贴在输入坞上方） */
  #toast { position:absolute; left:50%; bottom:calc(100% + 16px);
           transform:translateX(-50%) translateY(6px);
           background:rgba(31,41,51,.94); color:#fff; font-size:13px; padding:9px 14px;
           border-radius:10px; white-space:nowrap; opacity:0; pointer-events:none;
           transition:opacity .22s, transform .22s; z-index:5; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  /* ---- 输入坞（大圣喵趴在输入框上沿）---- */
  .dock-inner { position:relative; width:100%; background:#fff; border:1px solid #e4e7eb;
                border-radius:18px; padding:12px 14px; display:flex; align-items:center; gap:10px;
                box-shadow:0 10px 30px rgba(0,0,0,.14); }
  .dock-inner input { flex:1; min-width:0; font-size:15px; padding:11px 14px 11px 92px;
                      border-radius:12px; border:1px solid #d2d8de; background:#f8fafc; outline:none; }
  .dock-inner input:focus { border-color:#2563eb; background:#fff; }
  .dock-inner button { font-size:14px; padding:10px 14px; border-radius:11px; border:1px solid #cbd2d9;
                       background:#fff; cursor:pointer; white-space:nowrap; }
  /* 大圣喵 */
  #beanDock { position:absolute; left:14px; bottom:100%; margin-bottom:-9px; width:68px; height:59.5px;
              z-index:3; filter:drop-shadow(0 3px 5px rgba(0,0,0,.18)); pointer-events:none; }
  #bean { transform-origin:50% 86%; width:100%; height:100%; }
  #think { opacity:0; transition:opacity .3s; }
  #happyEyes { display:none; }
  #bean.state-happy #happyEyes { display:inline; }
  #bean.state-happy #eyeGroup { display:none; }
  #bean.state-thinking #eyeGroup { transform:translateY(-2.5px); }
  @keyframes beanBreathe {0%,100%{transform:scale(1) translateY(0)}50%{transform:scale(1.05) translateY(-2px)}}
  @keyframes beanBounce {0%,100%{transform:translateY(0)}30%{transform:translateY(-14px)}60%{transform:translateY(0)}80%{transform:translateY(-6px)}}
  @keyframes beanSway {0%,100%{transform:rotate(-8deg)}50%{transform:rotate(8deg)}}
  #bean.state-idle { animation: beanBreathe 3s ease-in-out infinite; }
  #bean.state-receiving { animation: beanBounce .6s ease; }
  #bean.state-thinking { animation: beanSway 1.1s ease-in-out infinite; }
  #bean.state-thinking #think { opacity:1; }
  #bean.state-happy { animation: beanBounce .55s ease 2; }
  /* ---- 下指令页（整页顶对齐：输入坞 → 状态行 → 步骤列表）----
     原先用 align-items:flex-end 把卡片钉在容器底部，只有一个输入框时上方的留白是刻意效果；
     加了实时步骤列表后，那片留白就变成了一大块空白，故改为顶部对齐、内容自上而下排。
     padding-top 68px 是留给大圣喵的：它绝对定位在输入坞上沿、向上冒出约 50px，
     不留这块位置它会直接盖到顶部栏上。 */
  .run-wrap { flex:1 1 auto; display:flex; flex-direction:column; align-items:center;
              padding:68px 16px 32px; overflow-y:auto; }
  .run-card { width:100%; max-width:1080px; }
  .run-status { margin-top:11px; padding-left:8px; font-size:13px; color:#52606d; min-height:19px; }
  /* ---- 下指令页：运行中的「实时步骤列表」（前端累积，后端只回最新一步）---- */
  .live-panel { background:#fff; border:1px solid #e4e7eb; border-radius:14px;
                padding:12px 14px 8px; margin-top:14px;         /* 挂在状态行下方 */
                box-shadow:0 8px 22px rgba(16,24,40,.07); }
  .live-panel.hidden { display:none; }
  .live-top { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
              padding-bottom:9px; border-bottom:1px solid #f0f2f5; }
  .live-task { font-weight:700; font-size:14px; color:#1f2933;
               overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:52%; }
  .live-count { margin-left:auto; font-size:12px; color:#7b8794; }
  /* 列表吃满剩余视口高度：既然整页顶对齐了，剩下的空间就拿来显示步骤，而不是留成空白。
     330px 是除列表外全部固定元素的合计高度（顶栏47+上留白68+下留白32+输入坞68+状态行30
     +面板间距14+面板内边距20+面板头约30+余量21）。视口过矮时 min-height 兜底，整页滚动。 */
  .live-list { display:flex; flex-direction:column; overflow-y:auto;
               max-height:calc(100vh - 330px); min-height:150px;
               padding:3px 2px 0; overscroll-behavior:contain; }
  .live-list::-webkit-scrollbar { width:8px; }
  .live-list::-webkit-scrollbar-thumb { background:#d8dce1; border-radius:4px; }
  .live-list::-webkit-scrollbar-thumb:hover { background:#c2c8cf; }
  .live-list::-webkit-scrollbar-track { background:transparent; }
  .live-item { display:flex; gap:11px; align-items:flex-start; padding:9px 8px;
               border-radius:9px; border-left:3px solid transparent; }
  /* 最后一行就是「正在执行/最新一步」，用 CSS 自动高亮，不用 JS 维护 */
  .live-item:last-child { background:#f5f9ff; border-left-color:#2563eb; }
  /* 固定 126×80 的行高盒子：截图懒加载完会改变行高，把已滚到底的列表顶离底部，
     所以先占住位置；顺带让缩略图与预启动的占位框高度一致（原先 img 按比例只有 64px，
     比占位框矮，行高不齐） */
  .live-item .mini-shot { width:126px; flex:0 0 126px; height:80px; border-radius:7px;
                          border:1px solid #e4e7eb; background:#111;
                          object-fit:contain; }
  /* 预启动步没有截图，给个占位，免得左列塌成空白、和别的行对不齐 */
  .live-item div.mini-shot { background:#f5f6f8; border:1px dashed #d8dce1;
                             height:80px; display:flex; align-items:center; justify-content:center;
                             font-size:11px; color:#9aa5b1; }
  .live-item .mini-body { flex:1; min-width:0; }
  .live-item .mini-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .live-item .mini-sum { font-weight:700; font-size:14px; color:#1f2933; }
  .live-item .mini-explain { margin:4px 0 0; font-size:12px; color:#52606d; line-height:1.5; }
  .live-item .mini-meta { margin-top:4px; font-size:11px; color:#7b8794; }
  .live-item .atype { font-size:12px; padding:2px 7px; border-radius:6px; }
  .pre-tag { font-size:11px; font-weight:600; padding:2px 7px; border-radius:6px;
             background:#fde68a; color:#78350f; }
  /* ---- 日志页 ---- */
  .main { flex:1 1 auto; overflow-y:auto; width:100%; max-width:1080px; margin:0 auto;
          padding:16px 16px 40px; }
  .stats { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:18px; }
  .stat { background:#fff; border:1px solid #e4e7eb; border-radius:10px; padding:12px 16px;
          min-width:120px; flex:1; }
  .stat .k { font-size:12px; color:#7b8794; }
  .stat .v { font-size:18px; font-weight:700; margin-top:4px; }
  .badge { font-size:12px; font-weight:600; padding:3px 9px; border-radius:999px;
           background:#e5e7eb; color:#374151; }
  .badge.done { background:#d1fae5; color:#065f46; }
  .badge.running { background:#fef3c7; color:#92400e; }
  .badge.aborted { background:#fee2e2; color:#991b1b; }
  .badge.stopped { background:#e2e8f0; color:#475569; }
  .timeline { display:flex; flex-direction:column; gap:14px; }
  .card { background:#fff; border:1px solid #e4e7eb; border-radius:12px; display:flex;
          gap:16px; padding:14px; align-items:flex-start; }
  .card .shot { width:240px; flex:0 0 240px; border-radius:8px; border:1px solid #e4e7eb;
                background:#111; object-fit:contain; max-height:300px; }
  .card .body { flex:1; min-width:0; }
  .row1 { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .atype { font-weight:700; font-size:15px; padding:3px 9px; border-radius:7px; color:#fff; background:#475569; }
  .a-CLICK { background:#2563eb; } .a-TYPE { background:#7c3aed; }
  .a-SCROLL { background:#0891b2; } .a-AWAKE { background:#ca8a04; }
  .a-COMPLETE { background:#059669; }
  /* AutomotiveGUI 决策链 phase 配色（无截图，靠颜色区分阶段） */
  .a-perceive { background:#0891b2; } .a-route { background:#7c3aed; }
  .a-decide { background:#2563eb; } .a-act { background:#059669; }
  .a-verify { background:#0891b2; } .a-recovery { background:#ca8a04; }
  .a-guard { background:#64748b; } .a-finish { background:#059669; }
  .a-skill { background:#7c3aed; } .a-error { background:#dc2626; } .a-log { background:#475569; }
  .step-no { color:#7b8794; font-weight:600; }
  .meta { margin:8px 0; font-size:13px; color:#3e4c59; line-height:1.6; word-break:break-all; }
  .meta b { color:#1f2933; }
  .timing { font-size:12px; color:#7b8794; }
  .noop { color:#b91c1c; font-weight:600; font-size:12px; margin-top:6px; }
  .evidence { background:#ecfdf5; border-left:3px solid #059669; padding:6px 10px;
              font-size:12px; color:#065f46; margin-top:6px; border-radius:4px; }
  .empty { color:#9aa5b1; text-align:center; padding:40px; }
  /* ---- 设置页 ---- */
  .settings-main { width:100%; max-width:760px; margin:0 auto; padding:24px 16px 48px; overflow-y:auto; }
  .settings-card { background:#fff; border:1px solid #e4e7eb; border-radius:14px;
                   padding:20px; margin-bottom:16px; box-shadow:0 5px 18px rgba(16,24,40,.04); }
  .settings-card h2 { font-size:17px; margin:0 0 14px; }
  .settings-card label.field { display:block; font-size:13px; font-weight:600; margin:12px 0; }
  .settings-card input[type=text], .settings-card input[type=url],
  .settings-card input[type=password], .settings-card input[type=number] {
    display:block; width:100%; margin-top:6px; padding:10px 12px; font:inherit; font-weight:400;
    border:1px solid #cbd2d9; border-radius:9px; background:#fff; }
  .settings-card input:disabled { background:#eef1f4; color:#7b8794; }
  .settings-hint { font-size:12px; line-height:1.6; color:#52606d; margin:8px 0; }
  .settings-row { display:flex; gap:12px; flex-wrap:wrap; align-items:end; }
  .settings-row .field { flex:1 1 140px; }
  .settings-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:18px; }
  .settings-actions button { border:1px solid #cbd2d9; border-radius:9px; background:#fff;
                             padding:9px 14px; cursor:pointer; font-size:14px; }
  .settings-actions button.primary { background:#2563eb; color:#fff; border-color:#2563eb; }
  .settings-actions button:disabled { opacity:.6; cursor:default; }
  .settings-message { min-height:22px; font-size:13px; margin-top:10px; white-space:pre-wrap; }
  .settings-message.error { color:#b91c1c; }
  .settings-message.good { color:#047857; }
  .app-check-list { display:grid; gap:8px; }
  .app-row { display:flex; align-items:center; gap:8px; padding:8px 10px;
             border:1px solid #e4e7eb; border-radius:8px; }
  .app-row input[type=text] { font:inherit; font-size:13px; padding:7px 9px;
    border:1px solid #cbd2d9; border-radius:7px; background:#fff; min-width:0; }
  .app-row input[type=text]:focus { border-color:#2563eb; }
  .app-row .app-name { flex:1 1 110px; }
  .app-row .app-pkg { flex:2 1 220px; }
  .app-row .app-alias { flex:1 1 90px; }
  .app-row.filled .app-pkg { border-color:#059669; background:#ecfdf5; }
  .app-row .app-del { flex:0 0 auto; border:1px solid #fca5a5; background:#fff;
    color:#dc2626; border-radius:7px; padding:6px 10px; cursor:pointer; font-size:13px; }
  .app-row .app-del:hover { background:#fee2e2; }
  .eye-wrap { position:relative; display:flex; align-items:center; }
  .eye-wrap input { flex:1; padding-right:38px; }
  .eye-btn { position:absolute; right:8px; top:50%; transform:translateY(-50%);
             background:none; border:none; cursor:pointer; font-size:14px;
             color:#7b8794; padding:2px 4px; border-radius:4px; }
  .eye-btn:hover { background:#e4e7eb; color:#1f2933; }
"""

MASCOT_SVG = r"""<svg id="bean" class="state-idle" viewBox="0 0 64 56">
        <!-- 思考气泡 -->
        <g id="think">
          <ellipse cx="32" cy="6" rx="9" ry="5.6" fill="#fff" stroke="#cbd2d9" stroke-width="1.2"/>
          <circle cx="24.2" cy="6" r="1.4" fill="#9aa5b1"/>
          <circle cx="29.8" cy="6" r="1.4" fill="#9aa5b1"/>
          <circle cx="35.4" cy="6" r="1.4" fill="#9aa5b1"/>
        </g>
        <!-- 猫耳 -->
        <path d="M16 23 L18.5 9 L30.5 17 Z" fill="#ffc26b" stroke="#e8a44e" stroke-width="1.2" stroke-linejoin="round"/>
        <path d="M48 23 L45.5 9 L33.5 17 Z" fill="#ffc26b" stroke="#e8a44e" stroke-width="1.2" stroke-linejoin="round"/>
        <path d="M19.8 20.5 L21 13.8 L26.3 17.8 Z" fill="#ffb0a6"/>
        <path d="M44.2 20.5 L43 13.8 L37.7 17.8 Z" fill="#ffb0a6"/>
        <!-- 尾巴（从耳后绕上来） -->
        <path d="M45.5 44 Q57.6 42.6 56.4 33.5 Q55.6 27 49.8 27.6" stroke="#ffc26b" stroke-width="3.6"
              fill="none" stroke-linecap="round"/>
        <!-- 头 -->
        <circle cx="32" cy="31" r="17.5" fill="#ffc26b" stroke="#e8a44e" stroke-width="1.2"/>
        <!-- 桃心脸底 -->
        <path d="M32 16.5 C41.5 16.5 46 23 46 30.5 C46 39.5 40 46 32 46 C24 46 18 39.5 18 30.5 C18 23 22.5 16.5 32 16.5 Z" fill="#fff6e3"/>
        <!-- 大圣红纹 + 额头桃心 -->
        <path d="M20.4 27.6 Q25 23.2 29.4 26.6" stroke="#e8443a" stroke-width="2.4" fill="none" stroke-linecap="round"/>
        <path d="M43.6 27.6 Q39 23.2 34.6 26.6" stroke="#e8443a" stroke-width="2.4" fill="none" stroke-linecap="round"/>
        <path d="M32 22.4 c1.5 -2.1 3.9 -0.5 2.5 1.4 l-2.5 2.5 -2.5 -2.5 c-1.4 -1.9 1 -3.5 2.5 -1.4 Z" fill="#e8443a"/>
        <!-- 紧箍 -->
        <path d="M16 26 Q32 16 48 26" stroke="#f0b429" stroke-width="3.2" fill="none" stroke-linecap="round"/>
        <circle cx="32" cy="20.4" r="2.1" fill="#ffd23f" stroke="#e0a800" stroke-width="0.9"/>
        <!-- 腮红 -->
        <ellipse cx="21.5" cy="38" rx="4" ry="2.4" fill="#ffb0a6" opacity="0.5"/>
        <ellipse cx="42.5" cy="38" rx="4" ry="2.4" fill="#ffb0a6" opacity="0.5"/>
        <!-- 眼睛 -->
        <g id="eyeGroup">
          <circle cx="25.5" cy="32" r="3" fill="#2b2b2b"/>
          <circle cx="38.5" cy="32" r="3" fill="#2b2b2b"/>
          <circle cx="26.6" cy="30.8" r="1" fill="#fff"/>
          <circle cx="39.6" cy="30.8" r="1" fill="#fff"/>
        </g>
        <path id="happyEyes" d="M21.5 33.5 Q25.5 28.8 29.5 33.5 M34.5 33.5 Q38.5 28.8 42.5 33.5"
              stroke="#2b2b2b" stroke-width="2.3" fill="none" stroke-linecap="round"/>
        <!-- 鼻子 + 嘴 -->
        <path d="M29.5 36.2 L34.5 36.2 L32 38.7 Z" fill="#ff8a80"/>
        <path id="mouth" d="M27.5 40.5 Q32 43.6 36.5 40.5" stroke="#2b2b2b" stroke-width="2.2" fill="none" stroke-linecap="round"/>
        <!-- 胡须 -->
        <path d="M14.5 33.5 L5.5 31.5 M14.5 36.5 L5.5 37.5" stroke="#e8a44e" stroke-width="1.3" fill="none" stroke-linecap="round"/>
        <path d="M49.5 33.5 L58.5 31.5 M49.5 36.5 L58.5 37.5" stroke="#e8a44e" stroke-width="1.3" fill="none" stroke-linecap="round"/>
        <!-- 前爪（抓住输入框上沿） -->
        <ellipse cx="23" cy="50.5" rx="6.4" ry="4.7" fill="#ffc26b" stroke="#e8a44e" stroke-width="1.2"/>
        <ellipse cx="41" cy="50.5" rx="6.4" ry="4.7" fill="#ffc26b" stroke="#e8a44e" stroke-width="1.2"/>
        <path d="M20.5 52.4 L25.5 52.4 M38.5 52.4 L43.5 52.4" stroke="#e8a44e" stroke-width="1" stroke-linecap="round"/>
      </svg>"""

COMMON_JS = r"""
const $ = (s) => document.querySelector(s);
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

// 大圣喵状态机：idle 呼吸 / receiving 弹一下 / thinking 摇尾思考 / happy 眯眼大笑
const MOUTH = {
  'idle':'M27.5 40.5 Q32 43.6 36.5 40.5', 'receiving':'M27.5 40.5 Q32 43.6 36.5 40.5',
  'thinking':'M28 41 Q32 39.6 36 41', 'happy':'M26.5 39 Q32 47.5 37.5 39'
};
function setMascot(state){
  const bean = $('#bean'); if(!bean) return;
  bean.className = 'state-' + state;
  const m = $('#mouth'); if(m) m.setAttribute('d', MOUTH[state] || MOUTH['idle']);
}

// 心跳：页面开着就每 3 秒报到一次；页面全关后心跳停，服务端（若以
// --auto-exit-seconds 启动）会在超时后自动退出 —— 这就是「关网页即关服务」。
// 手动常驻启动时后端不启用该逻辑，心跳只是几个空请求，无副作用。
(function(){
  const beat = () => { try { fetch('/api/heartbeat', {cache:'no-store'}).catch(()=>{}); } catch(e){} };
  beat();
  setInterval(beat, 3000);
  // 关闭/跳转时立刻通知一声，让退出更快（多标签页时会被其他页的心跳刷回来）
  window.addEventListener('pagehide', function(){
    try { navigator.sendBeacon('/api/closing'); } catch(e){}
  });
})();
"""

RUN_JS = r"""
// 给本标签命名：日志页的「← 去下指令」会复用这个标签（切换），而不是再开一个
try { window.name = 'lg_run'; } catch(e){}

let curDir = null, pollTimer = null, lastSteps = -1, staleTicks = 0;
// 实时步骤列表：后端 /api/live 每 2 秒只回「最新一步」（省流量），历史由前端攒
let liveSteps = [];        // 已渲染的步骤，按 step 升序
let liveStatus = 'running';
let stickBottom = true;    // 用户没手动上滚时，自动贴到最新一步

function setLive(t){ const el = $('#live'); if(el) el.textContent = t || ''; }
function setLogLink(dir){
  const a = $('#logLink'); if(!a) return;
  a.href = dir ? ('/logs?task=' + encodeURIComponent(dir)) : '/logs';
}
function stopPoll(){ if(pollTimer){ clearInterval(pollTimer); pollTimer = null; } }
function startPoll(){ if(!pollTimer) pollTimer = setInterval(tick, 2000); }
function track(dir){
  curDir = dir; lastSteps = -1; staleTicks = 0;
  clearLive();                          // 换任务就把上一条的步骤列表清干净
  if(dir) localStorage.setItem('lga_task_dir', dir); else localStorage.removeItem('lga_task_dir');
  setLogLink(dir);
}

// ---- 实时步骤列表：累积每一步（后端 /api/live 只回最新一步以省流量，历史在前端攒）----
function showCard(on){ const p = $('#livePanel'); if(p) p.classList.toggle('hidden', !on); }
// 统一改徽标：避免出现「徽标还写着运行中、内容却已被清空」的错位状态
function setBadge(state, text){
  const b = $('#liveBadge'); if(!b) return;
  b.className = 'badge ' + state; b.textContent = text;
}

function clearLive(){
  liveSteps = []; liveStatus = 'running'; stickBottom = true;
  const l = $('#liveList'); if(l) l.innerHTML = '';
  const c = $('#liveCount'); if(c) c.textContent = '';
  const t = $('#liveTask'); if(t) t.textContent = '';
}

function stepKey(s){ return (s && s.idx != null) ? String(s.idx) : (s && s.step != null ? String(s.step) : ''); }

function liveItemHTML(s){
  const a = s.action || {};
  const phase = a.action || 'log';
  const msg = a.summary || a.explain || '';
  const tk = s.tokens || {}, tok = (tk.input || 0) + (tk.output || 0);
  const extra = Object.entries(a).filter(([k]) => !['action','summary','explain'].includes(k))
    .map(([k,v]) => k + '=' + (typeof v === 'object' ? JSON.stringify(v) : v)).join(' · ');
  const meta = 't+' + (s.total_time || 0) + 's' + (tok ? ' · ' + tok + ' tok' : '');
  return '<div class="live-item" data-step="' + esc(stepKey(s)) + '">'
    + '<div class="mini-body">'
    +   '<div class="mini-head">'
    +     '<span class="idx-no">#' + esc(s.idx != null ? s.idx : '') + '</span>'
    +     '<span class="atype a-' + esc(phase) + '">' + esc(phase) + '</span>'
    +     '<span class="mini-msg">' + esc(msg) + '</span>'
    +   '</div>'
    +   (extra ? '<div class="mini-explain">' + esc(extra) + '</div>' : '')
    +   '<div class="mini-meta">' + esc(meta) + ' · graph step ' + esc(s.step != null ? s.step : '') + '</div>'
    + '</div></div>';
}

function appendStep(s){
  const list = $('#liveList'); if(!list) return;
  const box = document.createElement('div');
  box.innerHTML = liveItemHTML(s);
  list.appendChild(box.firstElementChild);
  if(stickBottom) list.scrollTop = list.scrollHeight;
}

function replaceStep(s){
  const list = $('#liveList'); if(!list) return;
  const row = list.querySelector('[data-step="' + stepKey(s) + '"]');
  if(!row) return;
  const box = document.createElement('div');
  box.innerHTML = liveItemHTML(s);
  row.replaceWith(box.firstElementChild);
}

function renderLive(d){
  const panel = $('#livePanel'); if(!panel) return;    // 面板不在页面上就直接跳过
  if(!d || !d.ok) return;
  liveStatus = d.status || 'running';
  // 三种结局分开显示：被熔断中止 ≠ 完成（否则会把"打转收手"误报成成功）
  const BADGE = { done: ['done', '已完成'], running: ['running', '运行中'],
                  aborted: ['aborted', '已中止'] };
  const b = BADGE[liveStatus] || BADGE.running;
  setBadge(b[0], b[1]);
  $('#liveTask').textContent = d.task || '';
  const s = d.last;
  if(s){
    const key = stepKey(s), i = liveSteps.findIndex(x => stepKey(x) === key);
    if(i < 0){ liveSteps.push(s); appendStep(s); }       // 新的一步：往列表尾部追加
    else if(JSON.stringify(liveSteps[i]) !== JSON.stringify(s)){
      liveSteps[i] = s; replaceStep(s);                  // 同一步内容更新（截图后到等）
    }
    $('#liveCount').textContent = '共 ' + liveSteps.length + ' 步';
  }
  showCard(true);
}

// 刷新页面/切任务后：用完整轨迹把已走过的步骤补回来（只调一次，之后靠增量）
async function loadLiveHistory(dir){
  if(!dir) return;
  try {
    const r = await fetch('/api/task/' + encodeURIComponent(dir));
    const d = await r.json();
    if(!d || d.error || !Array.isArray(d.steps) || !d.steps.length) return;
    liveSteps = d.steps.slice();
    liveStatus = d.status || 'running';
    const list = $('#liveList'); if(!list) return;
    list.innerHTML = liveSteps.map(liveItemHTML).join('');
    const c = $('#liveCount'); if(c) c.textContent = '共 ' + liveSteps.length + ' 步';
    const t = $('#liveTask'); if(t) t.textContent = d.task || '';
    showCard(true);                       // 必须先显示：面板带 hidden 时是 display:none，
    list.scrollTop = list.scrollHeight;   // scrollHeight 为 0，此时赋 scrollTop 是空操作，
                                          // 结果刷新后列表停在顶部第 1 步而不是最新一步
  } catch(e){}
}

async function fetchLive(){
  if(!curDir) return;
  try {
    const r = await fetch('/api/live?task=' + encodeURIComponent(curDir));
    renderLive(await r.json());
  } catch(e){}
}

async function tick(){
  if(!curDir) return;                    // 没在跟踪任何任务就保持安静
  let list = [];
  try { const r = await fetch('/api/tasks'); list = await r.json(); } catch(e){ return; }
  if(!Array.isArray(list)) return;
  const me = list.find(x => x.task_dir === curDir);
  if(!me){ setMascot('thinking'); setLive('⏳ 启动中…'); return; }   // 日志目录刚建、还没落盘
  if(me.status === 'running'){
    setMascot('thinking');
    // 先刷列表再写文案：反过来的话 cur 取到的是上一轮的末尾步，底部会比列表慢一拍
    fetchLive().then(() => {
      // 事件条数 = 已记录的子步骤总数（graph step 下可能有多条）
      const cur = liveSteps.length ? liveSteps[liveSteps.length - 1] : null;
      const lastPhase = cur && cur.action ? cur.action.action : '';
      const lastGraphStep = cur ? cur.step : null;
      setLive('🔴 运行中 · 已记录 ' + liveSteps.length + ' 条'
              + (cur ? ' · 最新 ' + lastPhase + (lastGraphStep != null ? '（graph step ' + lastGraphStep + '）' : '') : ''));
    });
    staleTicks = (me.total_steps === lastSteps) ? staleTicks + 1 : 0;
    lastSteps = me.total_steps;
    if(staleTicks >= 15){                // 30s 步数没动：不假装还在跑
      setMascot('idle');
      setLive('⏸ 30 秒无新动作，任务可能已中断（去任务日志确认）');
      stopPoll();
    } else { startPoll(); }
  } else if(me.status === 'aborted'){
    // 被熔断中止：明确说"中止"而不是"完成"，并引导去看原因
    setMascot('idle');
    setLive('⛔ 已中止 · ' + me.total_steps + ' 步 · ' + me.total_time + 's'
            + '（判定为打转/未完成，去任务日志看原因）');
    fetchLive();
    stopPoll();
  } else {
    setMascot('happy');
    setLive('✅ 已完成 · ' + me.total_steps + ' 步 · ' + me.total_time + 's · ' + me.total_tokens + ' tok');
    fetchLive();                         // 完成后停在最后一步
    stopPoll();
  }
}

$('#runBtn').onclick = async () => {
  const task = $('#taskInput').value.trim();
  if(!task){ alert('请输入任务内容'); return; }
  const btn = $('#runBtn');
  btn.disabled = true; setMascot('receiving'); setLive('⏳ 正在启动…');
  try {
    const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
                                      body: JSON.stringify({task})});
    const j = await r.json();
    if(!j.ok){ alert('启动失败：' + (j.error || '未知错误')); setMascot('idle'); setLive(''); return; }
    $('#taskInput').value = '';          // 启动成功后清空输入框
    track(j.task_dir);                   // track 内部已 clearLive()
    showCard(false);                     // 先藏起列表，等新任务第一步落盘再展开
    setMascot('thinking'); setLive('🔴 运行中 · 已提交');
    startPoll();
  } catch(e){ alert('请求出错：' + e); setMascot('idle'); setLive(''); }
  finally { btn.disabled = false; }
};

$('#stopBtn').onclick = async () => {
  try { await fetch('/api/stop', {method:'POST'}); } catch(e){}
  track(null);                          // 清列表 + 取消「刷新后继续跟踪」
  showCard(false);                      // 收起卡片：内容已被清空，留着会变成"运行中"空卡片
  setBadge('stopped', '已停止');
  setMascot('idle'); setLive('⏹ 已停止');
};

$('#taskInput').addEventListener('keydown', e => { if(e.key === 'Enter') $('#runBtn').click(); });

// ================= 语音录入（入口已就位，ASR 后面接）=================
let toastTimer = null;
function toast(msg){
  const el = $('#toast'); if(!el) return;
  el.textContent = msg; el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}
// 语音接入点：拿到识别文本后调用 fillTaskText(text)，autoRun=true 则直接启动
function fillTaskText(text, autoRun){
  const inp = $('#taskInput'); if(!inp) return;
  inp.value = text || '';
  inp.focus();
  if(autoRun && inp.value) $('#runBtn').click();
}
// —— 流式语音：浏览器麦克风边录边发 → 服务端转发讯飞 IAT → 实时出字 ——
// 停止参数全部读 /settings voice 段配置（静音停止/最长录音/静音阈值/最少语音）
const VOICE_FRAME_SAMPLES = 1280;   // 80ms @16kHz，讯飞推荐帧长
// AudioWorklet：AGC（快攻慢放）+ 16k 线性重采样（必要时）+ 80ms 帧打包
function buildVoiceWorkletCode(S){
  return `
class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor(){
    super();
    this.resample = (sampleRate !== 16000);
    this.step = 16000 / sampleRate;
    this.pos = 0; this.prev = 0;
    this.peak = 1e-4;
    this.buf = new Int16Array(1280);
    this.fill = 0;
  }
  emit16(x){
    const s = Math.max(-1, Math.min(1, x));
    this.buf[this.fill++] = Math.round(s * 32767);
    if(this.fill >= 1280){
      const out = this.buf; this.buf = new Int16Array(1280); this.fill = 0;
      this.port.postMessage({type:'pcm', buf: out.buffer}, [out.buffer]);
    }
  }
  process(inputs){
    const ch = (inputs[0] && inputs[0][0]) ? inputs[0][0] : null;
    if(!ch || !ch.length) return true;
    let sum = 0;
    for(let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
    const rms = Math.sqrt(sum / ch.length);
    this.port.postMessage({type:'rms', value: rms});
    // AGC：峰值快攻慢放（参数来自设置页）
    if(rms > this.peak) this.peak = rms; else this.peak *= ${S.agc_release};
    const g = Math.min(${S.agc_target} / this.peak, ${S.agc_max_gain});
    for(let i = 0; i < ch.length; i++){
      const x = ch[i] * g;
      if(this.resample){
        this.pos += this.step;
        while(this.pos >= 1){
          this.pos -= 1;
          this.emit16(this.prev + (x - this.prev) * this.pos);
        }
        this.prev = x;
      } else {
        this.emit16(x);
      }
    }
    return true;
  }
}
registerProcessor('voice-capture', VoiceCaptureProcessor);
  `;
}
let voiceStream = null;   // {sid, stream, ctx, node, blobUrl, rec, ...}

function updateVoiceMeter(rms){
  const f = $('#voiceLevelFill'); if(!f) return;
  f.style.height = Math.min(100, Math.sqrt(Math.max(0, rms)) * 100) + '%';
}
function showVoiceMeter(){ const m = $('#voiceLevel'); if(m) m.classList.add('show'); }
function hideVoiceMeter(){
  const m = $('#voiceLevel'); if(m) m.classList.remove('show');
  const f = $('#voiceLevelFill'); if(f) f.style.height = '0%';
}
function fillTaskTextLive(text){
  const inp = $('#taskInput'); if(!inp) return;
  if(document.activeElement !== inp) inp.value = text || '';
}
function releaseMedia(vs){
  try { if(vs.node) vs.node.disconnect(); } catch(e){}
  try { if(vs.stream) vs.stream.getTracks().forEach(t => t.stop()); } catch(e){}
  try { if(vs.ctx) vs.ctx.close(); } catch(e){}
  if(vs.blobUrl) URL.revokeObjectURL(vs.blobUrl);
  const btn = $('#voiceBtn'); if(btn) btn.classList.remove('recording');
  hideVoiceMeter();
}
// 端点判定：自适应噪底 + 最少语音时长 + 尾部静音（参数来自设置页配置）
function endpointDecision(vs, rms, nowMs){
  updateVoiceMeter(rms);
  if(!vs.rec) return;
  const th = Math.max(vs.baseTh, vs.noise * vs.noiseFactor);
  if(rms < th){   // 安静段：快速跟随降、慢速升；说话段不更新噪底
    if(rms < vs.noise) vs.noise = rms; else vs.noise += (rms - vs.noise) * vs.noiseRise;
  }
  if(rms >= th){ vs.started = true; vs.silentSince = 0; vs.voiceMs += vs.rmsStepMs; }
  else if(vs.started && !vs.silentSince){ vs.silentSince = nowMs; }
  if((vs.started && vs.silentSince && vs.voiceMs >= vs.minVoiceMs && nowMs - vs.silentSince > vs.silenceMs) ||
     nowMs - vs.startedAt > vs.capMs){
    vs.rec = false;
    endVoiceStream(true);
  }
}
// 发一帧裸 PCM；顺带检查服务端返回的会话状态（dead 时立即中断并提示）
function sendChunk(vs, buf){
  if(!vs.rec) return;
  fetch('/api/voice/stream/chunk/' + vs.sid, {
    method:'POST', headers:{'Content-Type':'application/octet-stream'}, body: buf
  })
    .then(r => r.json())
    .then(j => {
      if(!vs.rec) return;
      if(j.state === 'dead'){
        vs.rec = false;
        toast('✗ 识别中断：' + (j.error || 'ASR 连接断开'));
        releaseMedia(vs);
        fetch('/api/voice/stream/abort/' + vs.sid, {method:'POST'}).catch(()=>{});
        setMascot('idle');
      } else if(j.partial){
        fillTaskTextLive(j.partial);
      }
    })
    .catch(()=>{});
}
async function endVoiceStream(final){
  const vs = voiceStream; voiceStream = null;
  if(!vs) return;
  vs.rec = false;
  releaseMedia(vs);
  if(!final){
    if(vs.sid) fetch('/api/voice/stream/abort/' + vs.sid, {method:'POST'}).catch(()=>{});
    setMascot('idle');
    return;
  }
  setMascot('thinking');
  toast('🎤 说话结束，识别收尾中…');
  try {
    const r = await fetch('/api/voice/stream/stop/' + vs.sid, {method:'POST'});
    const j = await r.json();
    if(j.ok){ fillTaskText(j.text || ''); toast(j.text ? '✓ 已识别，确认后启动' : '✗ 未识别到内容'); }
    else toast('✗ ' + (j.error || '识别失败'));
    setMascot('idle');
  } catch(e){
    toast('✗ 请求出错：' + e); setMascot('idle');
  }
}
async function startVoiceInput(){
  if(voiceStream){ endVoiceStream(true); return; }   // 录音中再点麦克风 = 结束
  const btn = $('#voiceBtn'); if(!btn) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true, noiseSuppression:true}});
  } catch(e){
    toast('✗ 无法访问麦克风：' + e); setMascot('idle'); return;
  }
  btn.classList.add('recording');
  setMascot('thinking');
  toast('🎤 录音中，实时出字…');
  showVoiceMeter();
  const vs = {
    sid: null, stream, ctx: null, node: null, blobUrl: null, rec: true,
    startedAt: Date.now(), silentSince: 0, started: false, voiceMs: 0,
    baseTh: 120 / 32768, noise: 0.01, rmsStepMs: 8,
    noiseRise: 0.01, noiseFactor: 3, agcTarget: 0.8, agcMaxGain: 16, agcRelease: 0.995,
    silenceMs: 800, minVoiceMs: 1500, capMs: 15000,
  };
  voiceStream = vs;
  try {
    const r0 = await fetch('/api/voice/stream/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'});
    const j0 = await r0.json();
    if(!j0.ok){
      toast('✗ ' + (j0.error || '启动识别失败'));
      endVoiceStream(false);
      return;
    }
    vs.sid = j0.session;
    const S = j0.settings || {max_seconds:15, silence_seconds:0.8, silence_threshold:120, min_voice_seconds:1.5,
                              agc_target:0.8, agc_max_gain:16, agc_release:0.995, noise_rise:0.01, noise_factor:3};
    vs.baseTh = (S.silence_threshold || 120) / 32768;
    vs.silenceMs = S.silence_seconds * 1000;
    vs.capMs = S.max_seconds * 1000;
    vs.minVoiceMs = S.min_voice_seconds * 1000;
    vs.noiseRise = S.noise_rise || 0.01;
    vs.noiseFactor = S.noise_factor || 3;
    vs.agcTarget = S.agc_target || 0.8;
    vs.agcMaxGain = S.agc_max_gain || 16;
    vs.agcRelease = S.agc_release || 0.995;
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC({sampleRate: 16000});
    vs.ctx = ctx;
    if(ctx.state === 'suspended'){ await ctx.resume(); }
    const src = ctx.createMediaStreamSource(stream);
    if(ctx.audioWorklet){
      vs.blobUrl = URL.createObjectURL(new Blob([buildVoiceWorkletCode(S)], {type:'application/javascript'}));
      await ctx.audioWorklet.addModule(vs.blobUrl);
      const node = new AudioWorkletNode(ctx, 'voice-capture');
      vs.node = node;
      src.connect(node);
      vs.rmsStepMs = 128 / ctx.sampleRate * 1000;   // worklet 128 采样/回调
      node.port.onmessage = (e) => {
        const d = e.data;
        if(d.type === 'rms') endpointDecision(vs, d.value, Date.now());
        else if(d.type === 'pcm') sendChunk(vs, d.buf);
      };
    } else {
      // 兜底：ScriptProcessor（旧浏览器），1024 采样/次，同样带 AGC + 重采样
      const node = ctx.createScriptProcessor(1024, 1, 1);
      vs.node = node;
      const mute = ctx.createGain(); mute.gain.value = 0;   // 防回授
      src.connect(node); node.connect(mute); mute.connect(ctx.destination);
      vs.rmsStepMs = 1024 / ctx.sampleRate * 1000;
      let acc = new Int16Array(VOICE_FRAME_SAMPLES), accN = 0, peak = 1e-4;
      let pos = 0, prev = 0; const step = 16000 / ctx.sampleRate;
      node.onaudioprocess = (ev) => {
        if(!vs.rec) return;
        const ch = ev.inputBuffer.getChannelData(0);
        let sum = 0;
        for(let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
        const rms = Math.sqrt(sum / ch.length);
        endpointDecision(vs, rms, Date.now());
        let fp = 0;
        for(let i = 0; i < ch.length; i++){ const a = Math.abs(ch[i]); if(a > fp) fp = a; }
        if(fp > peak) peak = fp; else peak *= vs.agcRelease;
        const g = Math.min(vs.agcTarget / peak, vs.agcMaxGain);
        for(let i = 0; i < ch.length; i++){
          const x = ch[i] * g;
          pos += step;
          while(pos >= 1){
            pos -= 1;
            const y = prev + (x - prev) * pos;
            acc[accN++] = Math.round(Math.max(-1, Math.min(1, y)) * 32767);
            if(accN >= VOICE_FRAME_SAMPLES){
              const out = acc; acc = new Int16Array(VOICE_FRAME_SAMPLES); accN = 0;
              sendChunk(vs, out.buffer);
            }
          }
          prev = x;
        }
      };
    }
  } catch(e){
    toast('✗ ' + e);
    endVoiceStream(false);
  }
}
// 关页/刷新时主动终止会话（sendBeacon 空 body，不等 180s TTL）
window.addEventListener('pagehide', function(){
  try { if(voiceStream && voiceStream.sid) navigator.sendBeacon('/api/voice/stream/abort/' + voiceStream.sid); } catch(e){}
});
$('#voiceBtn').onclick = startVoiceInput;

// 列表滚动粘性：用户手动上滚看历史时，别把他拽回底部
const _ll = $('#liveList');
if(_ll) _ll.addEventListener('scroll', () => {
  stickBottom = (_ll.scrollHeight - _ll.scrollTop - _ll.clientHeight) < 24;
});

// 刷新页面后继续跟踪本面板启动的那个任务（先用完整轨迹补回历史，再进入 2 秒轮询）
const saved = localStorage.getItem('lga_task_dir');
if(saved){ track(saved); loadLiveHistory(saved).then(() => { tick(); startPoll(); }); }
else { setMascot('idle'); setLive(''); }
"""

LOGS_JS = r"""
// 给本标签命名，下指令页的「任务日志」会复用这个标签（切换），而不是新开一个
try { window.name = 'lg_logs'; } catch(e){}

let currentDir = null, timer = null;

async function loadTasks(prefer){
  const r = await fetch('/api/tasks'); const list = await r.json();
  const sel = $('#taskSel'); sel.innerHTML = '';
  list.forEach(t => {
    const o = document.createElement('option');
    o.value = t.task_dir;
    o.textContent = `${t.task_dir} | ${t.task||'(无)'} | ${t.status==='done'?'✅':'⏳'} ${t.total_steps}步`;
    sel.appendChild(o);
  });
  const pick = (prefer && list.find(t => t.task_dir === prefer)) ? prefer
             : (list.length ? list[0].task_dir : null);
  if(pick){ sel.value = pick; loadTask(pick, true); }
}

async function loadTask(dir, animate){
  currentDir = dir;
  const r = await fetch('/api/task/' + encodeURIComponent(dir));
  const d = await r.json();
  if(d.error){ $('#timeline').innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
  // 统计卡
  $('#stats').innerHTML = `
    <div class="stat"><div class="k">任务</div><div class="v" style="font-size:14px">${esc(d.task)}</div></div>
    <div class="stat"><div class="k">状态</div><div class="v"><span class="badge ${d.status}">${d.status==='done'?'已完成':(d.status==='aborted'?'已中止':'运行中')}</span></div></div>
    <div class="stat"><div class="k">步数</div><div class="v">${d.stats.total_steps}</div></div>
    <div class="stat"><div class="k">平均步耗时</div><div class="v">${d.stats.avg_step}s</div></div>
    <div class="stat"><div class="k">总 Token</div><div class="v">${d.stats.total_tokens}</div></div>
  `;
  // 时间线
  const tl = $('#timeline'); tl.innerHTML = '';
  if(!d.steps.length){ tl.innerHTML = '<div class="empty">暂无步骤数据</div>'; }
  d.steps.forEach(s => {
    const a = s.action || {};
    const explain = a.explain ? `<div class="meta"><b>说明：</b>${esc(a.explain)}</div>` : '';
    const detail = Object.entries(a).filter(([k]) => !['action','summary','explain','decision_source','_tool_call_id','_from_tool','chinese_resolved','typed_value'].includes(k))
      .map(([k,v]) => `<b>${esc(k)}:</b> ${esc(v)}`).join(' · ');
    const ev = s.evidence ? `<div class="evidence">✅ 完成证据：${esc(s.evidence)}</div>` : '';
    const noop = s.noop ? `<div class="noop">⚠️ 上一步点击后界面无变化（控件树+画面均未变）</div>` : '';
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="body">
        <div class="row1">
          <span class="step-no">#${s.idx}</span>
          <span class="atype a-${esc(a.action||'')}">${esc(a.action||'?')}</span>
          ${a.decision_source?`<span class="badge" style="background:#e0e7ff;color:#3730a3">${esc(a.decision_source)}</span>`:''}
        </div>
        ${detail?`<div class="meta">${detail}</div>`:''}
        ${explain}
        <div class="timing">推理 ${s.inference_time}s · 执行 ${s.execution_time}s · 总 ${s.total_time}s · token ${ (s.tokens.input||0)+(s.tokens.output||0) }</div>
        ${ev}${noop}
      </div>`;
    tl.appendChild(card);
  });
  // 运行中：自动开刷新，跑完自动停
  const ab = $('#autoBtn');
  if(d.status === 'running'){
    ab.dataset.on = '1'; ab.textContent = '自动刷新：开';
    if(!timer) timer = setInterval(() => loadTask(currentDir), 2000);
  } else if(timer){ clearInterval(timer); timer = null; }
}

$('#loadBtn').onclick = () => loadTask($('#taskSel').value);
$('#autoBtn').onclick = () => {
  const b = $('#autoBtn'); const on = b.dataset.on === '1';
  b.dataset.on = on ? '0' : '1'; b.textContent = '自动刷新：' + (on ? '关' : '开');
  if(timer){ clearInterval(timer); timer = null; }
  if(!on && currentDir) loadTask(currentDir);
};
const qsTask = new URLSearchParams(location.search).get('task');
loadTasks(qsTask);
"""

SETTINGS_JS = r"""
let screenPreview = null;
let devicePackages = [];
let currentDeviceSerial = '';
function appRows(){
  const rows=[];
  document.querySelectorAll('#appCheckList .app-row').forEach(row=>{
    rows.push({
      name:row.querySelector('.app-name').value.trim(),
      package:row.querySelector('.app-pkg').value.trim(),
      aliases:row.querySelector('.app-alias').value.split(/[,，、;；]/).map(s=>s.trim()).filter(Boolean)
    });
  });
  return rows;
}
function markDirty(row){ row.dataset.dirty='1'; }
function buildAppRow(app){
  app=app||{};
  const row=document.createElement('div'); row.className='app-row';
  const name=document.createElement('input'); name.type='text'; name.className='app-name';
  name.placeholder='应用名称（如 美团）'; name.value=app.name||'';
  const pkg=document.createElement('input'); pkg.type='text'; pkg.className='app-pkg';
  pkg.list='devicePackages'; pkg.placeholder='包名（如 com.xxx.yyy，可自动补全）'; pkg.value=app.package||'';
  const alias=document.createElement('input'); alias.type='text'; alias.className='app-alias';
  alias.placeholder='别名（逗号分隔，可选）'; alias.value=(app.aliases||[]).join(', ');
  const del=document.createElement('button'); del.type='button'; del.className='app-del';
  del.textContent='删除'; del.onclick=()=>{ row.remove(); };
  [name,pkg,alias].forEach(el=>el.addEventListener('input',()=>markDirty(row)));
  row.append(name,pkg,alias,del);
  return row;
}
function renderAppRows(apps){
  const container=$('#appCheckList');
  container.replaceChildren();
  if(!apps.length){
    const hint=document.createElement('p'); hint.className='settings-hint';
    hint.textContent='config.yaml 尚未配置应用。点击下方「+ 添加应用」。';
    container.append(hint);
    return;
  }
  for(const app of apps) container.append(buildAppRow(app));
}
function normToken(s){ return String(s).toLowerCase().replace(/[^a-z0-9]/g,''); }
function autoFillEmpty(){
  let filled=0;
  document.querySelectorAll('#appCheckList .app-row').forEach(row=>{
    const pkgInput=row.querySelector('.app-pkg');
    if(pkgInput.value.trim()) return;
    const tokens=[row.querySelector('.app-name').value.trim(),
      ...row.querySelector('.app-alias').value.split(/[,，、;；]/).map(s=>s.trim()).filter(Boolean)];

    let bestMatch=null;
    let bestScore=0;

    for(const token of tokens){
      if(!token) continue;
      const n=normToken(token);
      if(n.length<2) continue;

      for(const p of devicePackages){
        const parts=p.toLowerCase().split('.');

        // 优先：精确匹配某个部分
        for(const part of parts){
          if(normToken(part)===n){
            bestMatch=p;
            bestScore=1000; // 最高优先级
            break;
          }
        }
        if(bestScore===1000) break;

        // 否则：计算相似度（部分包含）
        for(const part of parts){
          const pn=normToken(part);
          let score=0;
          if(pn.includes(n)) score=n.length*10; // 别名被包含
          else if(n.includes(pn) && pn.length>=2) score=pn.length*5; // 部分被别名包含

          if(score>bestScore){
            bestScore=score;
            bestMatch=p;
          }
        }
      }
      if(bestScore===1000) break; // 已找到精确匹配，停止
    }

    if(bestMatch && bestScore>=20){ // 至少2个字符匹配才认为有效
      pkgInput.value=bestMatch;
      row.classList.add('filled');
      filled++;
    }
  });
  return filled;
}
function numOrNull(id){ const v=$(id).value.trim(); return v==='' ? undefined : Number(v); }
function settingsPayload(){
  return {
    model: {url:$('#modelUrl').value.trim(), name:$('#modelName').value.trim(),
            api_key:$('#apiKey').value.trim(),
            temperature:numOrNull('#modelTemperature'), max_tokens:numOrNull('#modelMaxTokens'),
            timeout:numOrNull('#modelTimeout'), max_retries:numOrNull('#modelMaxRetries')},
    voice: {appid:$('#voiceAppid').value.trim(),
            api_key:$('#voiceApiKey').value.trim(),
            api_secret:$('#voiceApiSecret').value.trim(),
            iat_url:$('#voiceIatUrl').value.trim(),
            max_seconds:numOrNull('#voiceMaxSeconds'), silence_seconds:numOrNull('#voiceSilenceSeconds'),
            silence_threshold:numOrNull('#voiceSilenceThreshold'),
            min_voice_seconds:numOrNull('#voiceMinVoiceSeconds'),
            agc_target:numOrNull('#voiceAgcTarget'), agc_max_gain:numOrNull('#voiceAgcMaxGain'),
            agc_release:numOrNull('#voiceAgcRelease'), noise_rise:numOrNull('#voiceNoiseRise'),
            noise_factor:numOrNull('#voiceNoiseFactor')},
    grid: {mode:$('#gridManual').checked ? 'manual' : 'auto',
           cols:Number($('#gridCols').value), rows:Number($('#gridRows').value)},
    apps: appRows(), device_serial: currentDeviceSerial
  };
}
function status(id, message, good){
  const node = $(id); node.textContent = message;
  node.className = 'settings-message ' + (good ? 'good' : 'error');
}
function updateGrid(){
  const manual = $('#gridManual').checked;
  $('#gridCols').disabled = !manual;
  $('#gridRows').disabled = !manual;
  if(!screenPreview){
    $('#gridPreview').textContent = '设备未连接或截图不可用；自动网格将在运行时按实际截图计算。';
    return;
  }
  const w=screenPreview.width, h=screenPreview.height;
  const c=manual ? Number($('#gridCols').value) : screenPreview.auto.cols;
  const r=manual ? Number($('#gridRows').value) : screenPreview.auto.rows;
  $('#gridPreview').textContent = `${w}×${h} 截图；自动建议 ${screenPreview.auto.cols}×${screenPreview.auto.rows}；`
    + `当前表单将使用 ${c}×${r}（单格约 ${Math.round(w/c)}×${Math.round(h/r)}px）。保存后对新任务生效。`;
}
async function readSettings(){
  try {
    const response = await fetch('/api/settings', {cache:'no-store'});
    const data = await response.json();
    if(!response.ok) throw new Error(data.error || '设置读取失败');
    $('#modelUrl').value = data.model.url;
    $('#modelName').value = data.model.name;
    $('#apiKey').value = data.model.api_key || '';
    $('#modelTemperature').value = data.model.temperature;
    $('#modelMaxTokens').value = data.model.max_tokens;
    $('#modelTimeout').value = data.model.timeout;
    $('#modelMaxRetries').value = data.model.max_retries;
    $('#gridManual').checked = data.grid.mode === 'manual';
    $('#gridCols').value = data.grid.cols;
    $('#gridRows').value = data.grid.rows;
    $('#voiceAppid').value = data.voice.appid || '';
    $('#voiceApiKey').value = data.voice.api_key || '';
    $('#voiceApiSecret').value = data.voice.api_secret || '';
    $('#voiceIatUrl').value = data.voice.iat_url || '';
    $('#voiceMaxSeconds').value = data.voice.max_seconds;
    $('#voiceSilenceSeconds').value = data.voice.silence_seconds;
    $('#voiceSilenceThreshold').value = data.voice.silence_threshold;
    $('#voiceMinVoiceSeconds').value = data.voice.min_voice_seconds;
    $('#voiceAgcTarget').value = data.voice.agc_target;
    $('#voiceAgcMaxGain').value = data.voice.agc_max_gain;
    $('#voiceAgcRelease').value = data.voice.agc_release;
    $('#voiceNoiseRise').value = data.voice.noise_rise;
    $('#voiceNoiseFactor').value = data.voice.noise_factor;
    renderAppRows(data.apps || []);
    $('#appsDeviceStatus').textContent = `配置文件：${data.config_path}；上次验证设备：${data.app_cache_serial || '未验证'}。`;
    try {
      const inventory = await postSettings('/api/settings/apps-device', {});
      currentDeviceSerial = inventory.serial || '';
      devicePackages = inventory.packages || [];
      const dl = $('#devicePackages'); dl.replaceChildren();
      for(const p of devicePackages){ const o=document.createElement('option'); o.value=p; dl.append(o); }
      if(currentDeviceSerial !== data.app_cache_serial){
        status('#appsDeviceStatus', `当前设备 ${currentDeviceSerial} 与缓存设备不一致；请点「重新加载」清除旧包名。配置文件：${data.config_path}`, false);
      }
    } catch(error){
      currentDeviceSerial = '';
      status('#appsDeviceStatus', `暂时无法核对设备：${error.message}。配置文件：${data.config_path}`, false);
    }
    updateGrid();
  } catch(error){ status('#saveStatus', error.message, false); }
}
async function readPreview(){
  try {
    const response = await fetch('/api/settings/grid-preview', {cache:'no-store'});
    const data = await response.json();
    screenPreview = data.ok ? data : null;
    if(!data.ok) $('#gridPreview').textContent = '无法预览设备：' + (data.error || '未知错误');
    else updateGrid();
  } catch(error){ screenPreview=null; updateGrid(); }
}
async function postSettings(path, body){
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                                     body:JSON.stringify(body)});
  const data = await response.json();
  if(!response.ok) throw new Error(data.error || data.message || '请求失败');
  return data;
}
$('#gridManual').addEventListener('change', updateGrid);
$('#gridCols').addEventListener('input', updateGrid);
$('#gridRows').addEventListener('input', updateGrid);
$('#voiceEnabled').addEventListener('change', () => {
  $('#voiceFields').style.display = $('#voiceEnabled').checked ? '' : 'none';
});
$('#verifyBtn').onclick = async () => {
  const button=$('#verifyBtn'); button.disabled=true;
  status('#verifyStatus', '正在验证模型（会产生一次最小 API 调用）…', true);
  try {
    const data=await postSettings('/api/settings/test-model', {model:settingsPayload().model});
    status('#verifyStatus', data.ok ? `${data.message} · ${data.latency_ms}ms`
                                   : `${data.error_type||'验证失败'}：${data.message}`, data.ok);
  } catch(error){ status('#verifyStatus', error.message, false); }
  finally { button.disabled=false; }
};
$('#asrVerifyBtn').onclick = async () => {
  const button=$('#asrVerifyBtn'); button.disabled=true;
  status('#asrVerifyStatus', '正在验证 ASR 连接（会产生一次握手/最小请求）…', true);
  try {
    const data=await postSettings('/api/settings/test-asr', {voice:settingsPayload().voice});
    status('#asrVerifyStatus', data.ok ? `${data.message} · ${data.latency_ms}ms`
                                       : `${data.error_type||'验证失败'}：${data.message}`, data.ok);
  } catch(error){ status('#asrVerifyStatus', error.message, false); }
  finally { button.disabled=false; }
};
$('#saveBtn').onclick = async () => {
  const button=$('#saveBtn'); button.disabled=true;
  try {
    const data=await postSettings('/api/settings', settingsPayload());
    $('#apiKey').value = data.model.api_key || '';
    $('#voiceAppid').value = data.voice.appid || '';
    $('#voiceApiKey').value = data.voice.api_key || '';
    $('#voiceApiSecret').value = data.voice.api_secret || '';
    $('#voiceIatUrl').value = data.voice.iat_url || '';
    $('#voiceMaxSeconds').value = data.voice.max_seconds;
    $('#voiceSilenceSeconds').value = data.voice.silence_seconds;
    $('#voiceSilenceThreshold').value = data.voice.silence_threshold;
    $('#voiceMinVoiceSeconds').value = data.voice.min_voice_seconds;
    $('#voiceAgcTarget').value = data.voice.agc_target;
    $('#voiceAgcMaxGain').value = data.voice.agc_max_gain;
    $('#voiceAgcRelease').value = data.voice.agc_release;
    $('#voiceNoiseRise').value = data.voice.noise_rise;
    $('#voiceNoiseFactor').value = data.voice.noise_factor;
    renderAppRows(data.apps || []);
    status('#saveStatus', currentDeviceSerial
      ? '设置已保存；目标应用包名已针对当前设备核对。'
      : '设置已保存；应用包名尚未针对当前设备核对，请点「重新加载」后再直启。', true);
    updateGrid();
  } catch(error){ status('#saveStatus', error.message, false); }
  finally { button.disabled=false; };
};
// Eye toggle: switch input type between password and text
document.querySelectorAll('.eye-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = document.getElementById(btn.dataset.target);
    if(!target) return;
    if(target.type === 'password'){
      target.type = 'text';
      btn.textContent = 'hide';
    } else {
      target.type = 'password';
      btn.textContent = 'eye';
    }
  });
});
$('#addAppBtn').onclick = () => {
  const row = buildAppRow({});
  markDirty(row);
  $('#appCheckList').append(row);
  row.querySelector('.app-name').focus();
};
$('#reloadAppsBtn').onclick = async () => {
  const button = $('#reloadAppsBtn'); button.disabled = true;
  const st = $('#appsDeviceStatus');
  st.className = 'settings-message good';
  st.textContent = '正在从车机获取已安装应用列表…';
  try {
    const data = await postSettings('/api/settings/apps-device', {apps: appRows()});
    if(!data.ok) throw new Error(data.error || '获取应用列表失败');
    devicePackages = data.packages || [];
    currentDeviceSerial = data.serial || '';
    const dl = $('#devicePackages'); dl.replaceChildren();
    for(const p of devicePackages){ const o=document.createElement('option'); o.value=p; dl.append(o); }
    renderAppRows(data.apps || []);
    const filled = autoFillEmpty();
    let saved = data;
    if(filled){
      saved = await postSettings('/api/settings/apps', {apps: appRows(), device_serial: currentDeviceSerial});
      renderAppRows(saved.apps || []);
    }
    const ready = (saved.apps || []).filter(app=>app.package).length;
    const cleared = data.cleared || [];
    st.className = 'settings-message ' + (cleared.length ? 'error' : 'good');
    st.textContent = `当前设备 ${currentDeviceSerial}：获取 ${data.count} 个包名，仅用作候选；`
      + `配置已保存 ${saved.apps.length} 个目标应用，其中 ${ready} 个包名已在设备上核对。`
      + (cleared.length ? `已清空当前设备未安装的旧包名：${cleared.join('、')}；请从包名列表选当前设备的包名后点「保存」。` : '')
      + ` 配置文件：${saved.config_path}`;
  } catch(error){
    st.className = 'settings-message error';
    st.textContent = '重新加载失败：' + error.message;
  } finally { button.disabled = false; }
};
readSettings(); readPreview();
"""

# 下指令页：只有大圣喵 + 输入坞 + 一行状态，日志入口丢到新标签
PAGE_RUN_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutomotiveGUI · 下指令</title>
<style>/*__BASE_CSS__*/</style>
</head>
<body>
<header>
  <h1>🐱 AutomotiveGUI</h1><span class="sub">下指令</span>
  <div class="spacer"></div>
  <a class="mini" href="/settings">⚙ 设置</a>
  <a class="mini" id="logLink" href="/logs" target="lg_logs">📋 任务日志 →</a>
</header>
<div class="run-wrap">
  <div class="run-card">
    <div class="dock-inner">
      <div id="beanDock">/*__MASCOT__*/</div>
      <input id="taskInput" placeholder="输入任务，如：播放哔哩哔哩西部世界第一季第一集" autofocus />
      <button id="voiceBtn" type="button" title="语音录入（边说边识别，再点一次结束）" aria-label="语音录入">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="9" y="2.5" width="6" height="11" rx="3"/>
          <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/>
          <path d="M12 18.2v3.3"/>
        </svg>
      </button>
      <div id="voiceLevel" class="voice-level" title="麦克风电平"><div id="voiceLevelFill"></div></div>
      <button id="runBtn" class="primary">▶ 启动</button>
      <button id="stopBtn">■ 停止</button>
      <div id="toast" role="status" aria-live="polite"></div>
    </div>
    <div class="run-status"><span id="live"></span></div>
    <div id="livePanel" class="live-panel hidden">
      <div class="live-top">
        <span class="badge running" id="liveBadge">运行中</span>
        <span class="live-task" id="liveTask"></span>
        <span class="live-count" id="liveCount"></span>
      </div>
      <div class="live-list" id="liveList"></div>
    </div>
  </div>
</div>
<script>/*__COMMON_JS__*//*__RUN_JS__*/</script>
</body>
</html>
"""

# 日志页：任务选择 + 统计 + 决策链回放
PAGE_LOGS_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutomotiveGUI · 任务日志</title>
<style>/*__BASE_CSS__*/</style>
</head>
<body>
<header>
  <h1>🐱 AutomotiveGUI</h1><span class="sub">任务日志</span>
  <div class="spacer"></div>
  <select id="taskSel"></select>
  <button id="loadBtn" class="mini">加载</button>
  <button id="autoBtn" class="mini" data-on="0">自动刷新：关</button>
  <a class="mini" href="/settings">⚙ 设置</a>
  <a class="mini" href="/" target="lg_run">← 去下指令</a>
</header>
<div class="main">
  <div id="stats" class="stats"></div>
  <div id="timeline" class="timeline"><div class="empty">请在上方选择历史任务加载回放…</div></div>
</div>
<script>/*__COMMON_JS__*//*__LOGS_JS__*/</script>
</body>
</html>
"""

PAGE_SETTINGS_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutomotiveGUI · 设置</title>
<style>/*__BASE_CSS__*/</style>
</head>
<body>
<header>
  <h1>🐱 AutomotiveGUI</h1><span class="sub">设置</span>
  <div class="spacer"></div>
  <a class="mini" href="/">下指令</a>
  <a class="mini" href="/logs">任务日志</a>
</header>
<main class="settings-main">
  <section class="settings-card">
    <h2>模型 API</h2>
    <label class="field">URL<input id="modelUrl" type="url" placeholder="https://example.com/v1" required></label>
    <label class="field">模型名称<input id="modelName" type="text" placeholder="模型 ID" required></label>
    <label class="field">API Key
      <div class="eye-wrap">
        <input id="apiKey" type="password" placeholder="未配置">
        <button type="button" class="eye-btn" data-target="apiKey" title="显示/隐藏">eye</button>
      </div>
    </label>
    <div class="settings-row">
      <label class="field">Temperature<input id="modelTemperature" type="number" step="0.1" min="0" max="2" placeholder="0.1"></label>
      <label class="field">Max Tokens<input id="modelMaxTokens" type="number" step="1" min="1" placeholder="2048"></label>
      <label class="field">Timeout（秒）<input id="modelTimeout" type="number" step="1" min="1" placeholder="60"></label>
      <label class="field">Max Retries<input id="modelMaxRetries" type="number" step="1" min="0" max="10" placeholder="4"></label>
    </div>
    <div class="settings-actions"><button id="verifyBtn" type="button">验证连接</button></div>
    <div id="verifyStatus" class="settings-message" role="status" aria-live="polite"></div>
  </section>
  <section class="settings-card">
    <h2>ASR API</h2>
    <label><input id="voiceEnabled" type="checkbox" checked> 开启语音指令</label>
    <div id="voiceFields">
      <label class="field">AppID<input id="voiceAppid" type="text" placeholder="讯飞 AppID"></label>
      <label class="field">API Key<input id="voiceApiKey" type="text" placeholder="讯飞 API Key"></label>
      <label class="field">API Secret
        <div class="eye-wrap">
          <input id="voiceApiSecret" type="password" placeholder="未配置">
          <button type="button" class="eye-btn" data-target="voiceApiSecret" title="显示/隐藏">eye</button>
        </div>
      </label>
      <label class="field">请求地址<input id="voiceIatUrl" type="text" placeholder="留空 = 讯飞公网 wss://iat-api.xfyun.cn/v2/iat"></label>
      <div class="settings-row">
        <label class="field">最长录音（秒）<input id="voiceMaxSeconds" type="number" step="1" min="3" max="120" placeholder="15" title="单次录音硬性时间上限，超过强制停止"></label>
        <label class="field">静音停止（秒）<input id="voiceSilenceSeconds" type="number" step="0.1" min="0.2" max="5" placeholder="0.8" title="说完后连续静音达到此时长自动停止录音（推荐 0.8）"></label>
        <label class="field">静音阈值 RMS<input id="voiceSilenceThreshold" type="number" step="1" min="50" max="3000" placeholder="120" title="RMS 低于该值视为静音；越小越灵敏（推荐 120）"></label>
        <label class="field">最少语音（秒）<input id="voiceMinVoiceSeconds" type="number" step="0.1" min="0" max="30" placeholder="1.5" title="语音总时长不足该值前不允许静音自动停，防止点击/噪音误停（推荐 1.5）"></label>
      </div>
      <p class="settings-hint">停止逻辑：说话后连续静音超过「静音停止」（RMS 低于「静音阈值」）自动结束；「最少语音」未达到前不会停，防开头误停；「最长录音」为硬性上限。</p>
      <div class="settings-row">
        <label class="field">AGC 目标峰值<input id="voiceAgcTarget" type="number" step="0.01" min="0.01" max="1" placeholder="0.8" title="自动增益放大后的目标音量峰值（推荐 0.8）"></label>
        <label class="field">AGC 最大增益<input id="voiceAgcMaxGain" type="number" step="1" min="1" max="100" placeholder="16" title="AGC 放大倍数上限，小声说话靠它提亮（推荐 16）"></label>
        <label class="field">AGC 释放系数<input id="voiceAgcRelease" type="number" step="0.001" min="0.9" max="0.9999" placeholder="0.995" title="峰值回落衰减系数，越小音量恢复越快（推荐 0.995）"></label>
        <label class="field">噪底回升系数<input id="voiceNoiseRise" type="number" step="0.001" min="0.0001" max="0.5" placeholder="0.01" title="环境变吵时噪底基线追赶速度，越大适应越快（推荐 0.01）"></label>
        <label class="field">噪底阈值倍数<input id="voiceNoiseFactor" type="number" step="0.1" min="1" max="20" placeholder="3" title="说话判定阈值 = max(静音阈值, 该倍数×噪底)；越大越不易把环境声误判为说话（推荐 3）"></label>
      </div>
      <p class="settings-hint">AGC = 自动音量调节，小声说话也能识别清楚；噪底参数用于嘈杂环境自适应。不确定就保持默认值。</p>
      <div class="settings-actions"><button id="asrVerifyBtn" type="button">验证连接</button></div>
      <div id="asrVerifyStatus" class="settings-message" role="status" aria-live="polite"></div>
    </div>
  </section>
  <section class="settings-card">
    <h2>VLM 网格</h2>
    <label><input id="gridManual" type="checkbox"> 手动设置网格（优先于自动计算）</label>
    <div class="settings-row">
      <label class="field">列数<input id="gridCols" type="number" min="5" max="26" value="20" disabled></label>
      <label class="field">行数<input id="gridRows" type="number" min="3" max="40" value="10" disabled></label>
    </div>
    <p id="gridPreview" class="settings-hint">正在读取屏幕尺寸…</p>
    <p class="settings-hint">默认按实际截图尺寸自动计算；手动覆盖只对保存后的新任务生效。</p>
  </section>

  <section class="settings-card">
    <h2>应用配置</h2>
    <p class="settings-hint">此处只维护目标应用。点击「重新加载」会以当前设备为准核对并保存目标应用的包名；另一设备上不存在的旧包名会清空，之后请从包名列表选正确包名再保存。获取到的全部包名只作为候选，不会全部写入配置。</p>
    <div id="appCheckList" class="app-check-list">正在读取应用配置…</div>
    <div class="settings-actions">
      <button id="addAppBtn" type="button">+ 添加应用</button>
      <button id="reloadAppsBtn" type="button">重新加载</button>
    </div>
    <div id="appsDeviceStatus" class="settings-message" role="status" aria-live="polite"></div>
    <datalist id="devicePackages"></datalist>
  </section>

  <div class="settings-actions"><button id="saveBtn" class="primary" type="button">保存设置</button></div>
  <div id="saveStatus" class="settings-message" role="status" aria-live="polite"></div>
</main>
<script>/*__COMMON_JS__*//*__SETTINGS_JS__*/</script>
</body>
</html>
"""


def _render(tpl: str) -> str:
    return (
        tpl.replace("/*__BASE_CSS__*/", BASE_CSS)
        .replace("/*__COMMON_JS__*/", COMMON_JS)
        .replace("/*__RUN_JS__*/", RUN_JS)
        .replace("/*__LOGS_JS__*/", LOGS_JS)
        .replace("/*__SETTINGS_JS__*/", SETTINGS_JS)
        .replace("/*__MASCOT__*/", MASCOT_SVG)
    )


HTML_RUN = _render(PAGE_RUN_TPL)
HTML_LOGS = _render(PAGE_LOGS_TPL)
HTML_SETTINGS = _render(PAGE_SETTINGS_TPL)


def main():
    global _IDLE_LIMIT
    ap = argparse.ArgumentParser(description="AutomotiveGUI 可视化面板（源自 LightGUIAgent dashboard）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--auto-exit-seconds", type=float, default=0.0,
                    help="所有页面关闭后多少秒自动退出（0=常驻，默认）。"
                         "启动器用它实现「关掉网页就关掉服务」。")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    _IDLE_LIMIT = float(args.auto_exit_seconds or 0.0)
    if _IDLE_LIMIT > 0:
        _touch()
        threading.Thread(target=_idle_monitor, args=(server,), daemon=True).start()
    print(f"📊 AutomotiveGUI 面板已启动：http://{args.host}:{args.port}")
    print(f"   数据源：{LOGS_ROOT.resolve()}")
    if _IDLE_LIMIT > 0:
        print(f"   页面关闭 {_IDLE_LIMIT:.0f} 秒后自动退出（有任务在跑时会等它跑完）")
    print("   按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
