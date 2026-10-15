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
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
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
if str(AUTOGUI_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOGUI_DIR))
from autogui.log_timeline import LogCache, process_key
_LOG_CACHE = LogCache()
_LIST_CACHE = {}
_LIST_LOCK = threading.RLock()

_SETTINGS_LOCK = threading.Lock()


def task_xml_artifact(task_dir: str, filename: str) -> Path | None:
    """只允许读取已有任务 debug 目录下的单个 XML 文件。"""
    if not task_dir or not filename or Path(task_dir).name != task_dir or Path(filename).name != filename:
        return None
    if Path(filename).suffix.lower() != ".xml" or not (LOGS_ROOT / f"{task_dir}.jsonl").is_file():
        return None
    task_path = (LOGS_ROOT / task_dir).resolve()
    if task_path.parent != LOGS_ROOT.resolve():
        return None
    artifact = (task_path / filename).resolve()
    return artifact if artifact.parent == task_path and artifact.is_file() else None

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
def task_label(session_id: str, task: str) -> str:
    """以启动时间和原始指令显示任务，旧日志回退到文件名中的任务片段。"""
    match = re.search(r"-(\d{8})-(\d{6})$", session_id)
    if not match:
        return task
    try:
        stamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return task
    return f"{stamp:%m-%d %H:%M:%S} {task}"


def _task_path(task_dir):
    if not task_dir or Path(task_dir).name != task_dir or "/" in task_dir or "\\" in task_dir:
        return None
    path = (LOGS_ROOT / f"{task_dir}.jsonl").resolve()
    return path if path.parent == LOGS_ROOT.resolve() and path.is_file() else None


def _run_state(task_dir):
    path = _task_path(task_dir)
    if not path:
        return {}
    try:
        return json.loads(path.with_suffix(".run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _apply_runtime(detail, first, state):
    detail = {**detail, "stats": dict(detail["stats"])}
    detail["run_log"] = state.get("run_log") or first.get("run_log") or ""
    if detail["complete"]:
        return detail
    pid = first.get("pid") or state.get("pid")
    identity = first.get("process_key") or state.get("process_key")
    started = first.get("started_at") or state.get("started_at")
    if state.get("state") in ("cancelled", "error", "interrupted"):
        detail["status"] = state["state"]
        detail["reason"] = state.get("reason", "进程已退出，未写入任务总结")
        if state.get("ended_at") and started:
            detail["stats"]["total_time"] = round(max(0, state["ended_at"] - started), 1)
    elif pid and identity:
        observed = process_key(pid)
        if observed == identity:
            detail["status"] = "running"
            if started:
                detail["stats"]["total_time"] = round(max(0, time.time() - started), 1)
        elif observed is None and os.name == "nt":
            # Access denied and a dead process are indistinguishable without a handle.
            detail["status"] = "unknown"
            detail["reason"] = "无法核实进程状态；尚无结束记录"
        else:
            detail["status"] = "interrupted"
            detail["reason"] = "执行进程已退出，未写入任务总结；显示最后已记录的耗时和用量"
    else:
        detail["reason"] = "缺少结束记录和进程信息，无法确认任务是否仍在运行"
    detail["stats"]["avg_step"] = round(detail["stats"]["total_time"] / detail["stats"]["total_steps"], 2) if detail["stats"]["total_steps"] else None
    return detail


def list_tasks() -> list:
    """Newest session first. Cache projections; don't read old logs every poll."""
    if not LOGS_ROOT.exists():
        return []
    out = []
    with _LIST_LOCK:
        paths = list(LOGS_ROOT.glob("*.jsonl"))
        existing = set()
        for path in paths:
            key = str(path.resolve())
            existing.add(key)
            stat = path.stat()
            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            cached = _LIST_CACHE.get(key)
            if not cached or cached[0] != signature:
                base, events = _LOG_CACHE.read(path)
                base = {k: v for k, v in base.items() if k != "steps"}
                cached = (signature, base, events[0] if events else {})
                _LIST_CACHE[key] = cached
            detail = _apply_runtime(cached[1], cached[2], _run_state(path.stem))
            stats = detail["stats"]
            match = re.search(r"-(\d{8})-(\d{6})$", path.stem)
            stamp = "".join(match.groups()) if match else datetime.fromtimestamp(stat.st_mtime).strftime("%Y%m%d%H%M%S")
            out.append({**{k: detail[k] for k in ("task_dir", "session_id", "task", "title", "status", "reason", "event_count")},
                        **stats, "total_cost": None, "_sort": (stamp, path.stem)})
        for key in set(_LIST_CACHE) - existing:
            del _LIST_CACHE[key]
    out.sort(key=lambda item: item["_sort"], reverse=True)
    for item in out:
        del item["_sort"]
    return out


def get_task_detail(task_dir: str) -> dict:
    path = _task_path(task_dir)
    if not path:
        return None
    detail, events = _LOG_CACHE.read(path)
    return _apply_runtime(detail, events[0] if events else {}, _run_state(task_dir))


def get_live(task_dir: str, after_idx: int | None = None) -> dict:
    detail = get_task_detail(task_dir)
    if not detail:
        return {"ok": False, "error": "task not found"}
    after = -1 if after_idx is None else after_idx
    reset = after > detail["cursor"]
    if reset:
        after = -1
    updates = detail["steps"][max(0, after + 1):max(0, after + 1) + 200]
    cursor = updates[-1]["idx"] if updates else after
    return {**{k: v for k, v in detail.items() if k != "steps"},
            "ok": True, "updates": updates, "last": detail["steps"][-1] if detail["steps"] else None,
            "cursor": cursor, "reset": reset, "has_more": cursor < detail["cursor"]}


def task_run_log(task_dir):
    detail = get_task_detail(task_dir)
    name = detail.get("run_log") if detail else None
    if not name or Path(name).name != name or not re.fullmatch(r"dashboard_run_[\w-]+\.log", name):
        return None
    path = (LOGS_ROOT / name).resolve()
    return path if path.parent == LOGS_ROOT.resolve() and path.is_file() else None


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
        LOGS_ROOT.mkdir(parents=True, exist_ok=True)
        before = {p.stem for p in LOGS_ROOT.glob("*.jsonl")}
        ts = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1000000000:09d}"
        log_path = LOGS_ROOT / f"dashboard_run_{ts}.log"
        try:
            logf = open(log_path, "w", encoding="utf-8", buffering=1)
        except OSError as exc:
            return None, f"无法创建任务输出日志：{exc}"
        task_env = {**os.environ, "AUTOGUI_RUN_LOG": log_path.name, "PYTHONUNBUFFERED": "1"}
        started_at = time.time()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", "main.py", task],
                env=task_env,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except Exception as e:  # noqa: BLE001
            logf.close()
            return None, f"启动失败：{e}"
        _RUNNING_PROC = proc
        proc.dashboard_cancelled = False
        def copy_output():
            try:
                for line in proc.stdout:
                    logf.write(line)
                    logf.flush()
                    try:
                        print(line, end="", flush=True)
                    except (OSError, UnicodeError):
                        pass
            finally:
                logf.close()
                proc.stdout.close()
        threading.Thread(target=copy_output, daemon=True).start()
    # 轮询等待新任务日志出现。冷启动在这台重依赖车机上实测可达 60s+（import langgraph
    # 等重模块 + 首帧慢），旧窗口 25s 会把"还在启动"误判成失败、但 main.py 其实在正常跑。
    # 窗口放到 120s；同时若 main.py 提前退出（真报错）立即失败并带上日志尾部，不干等。
    deadline_ts = time.time() + 120.0
    while time.time() < deadline_ts:
        now = sorted(p.stem for p in LOGS_ROOT.glob("*.jsonl"))
        new = [n for n in now if n not in before]
        for task_dir in new:
            path = _task_path(task_dir)
            if not path:
                continue
            _, events = _LOG_CACHE.read(path)
            if not events or events[0].get("run_log") != log_path.name:
                continue
            state = {"pid": proc.pid, "process_key": process_key(proc.pid),
                     "started_at": started_at, "run_log": log_path.name, "state": "running"}
            state_path = path.with_suffix(".run.json")
            state_path.write_text(json.dumps(state), encoding="utf-8")
            def watch(state_path=state_path, state=state, proc=proc):
                code = proc.wait()
                state = {**state, "ended_at": time.time(), "exit_code": code,
                         "state": "cancelled" if proc.dashboard_cancelled else "error" if code else "interrupted",
                         "reason": "用户停止任务" if proc.dashboard_cancelled else f"进程退出（退出码 {code}），未写入任务总结"}
                temp = state_path.with_suffix(".tmp")
                temp.write_text(json.dumps(state), encoding="utf-8")
                temp.replace(state_path)
            threading.Thread(target=watch, daemon=True).start()
            return task_dir, None
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
        proc.dashboard_cancelled = True
        try:
            if os.name == "nt":
                result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode and proc.poll() is None:
                    proc.dashboard_cancelled = False
                    return False
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        except OSError:
            if proc.poll() is None:
                proc.dashboard_cancelled = False
                return False
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
            if path == "/log_view.js":
                self._send(200, Path(__file__).with_name("log_view.js").read_bytes(), "text/javascript; charset=utf-8")
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif path == "/api/run-log":
                artifact = task_run_log(qs.get("task", [""])[0])
                if artifact is None:
                    self._send(404, "该任务没有关联的进程输出日志".encode("utf-8"), "text/plain; charset=utf-8")
                else:
                    with artifact.open("rb") as fh:
                        fh.seek(max(0, artifact.stat().st_size - 65536))
                        body = fh.read()
                    self._send(200, body, "text/plain; charset=utf-8")
            elif path in ("/", "/index.html"):
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
            elif path == "/api/tasks":
                self._send_json(list_tasks())
            elif path.startswith("/api/task/"):
                task_dir = urllib.parse.unquote(path[len("/api/task/"):])
                detail = get_task_detail(task_dir)
                if detail is None:
                    self._send_json({"error": "task not found"}, 404)
                else:
                    self._send_json(detail)
            elif path == "/api/artifact":
                task_dir = qs.get("task", [""])[0]
                filename = qs.get("file", [""])[0]
                artifact = task_xml_artifact(task_dir, filename)
                if artifact is None:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                else:
                    self._send(200, artifact.read_bytes(), "application/xml; charset=utf-8")
            elif path == "/api/live":
                task_dir = qs.get("task", [""])[0]
                try:
                    after_idx = int(qs["after"][0]) if "after" in qs else None
                except (ValueError, IndexError):
                    after_idx = None
                self._send_json(get_live(task_dir, after_idx))
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
        global _LAST_SEEN
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
                # 流式语音：建立讯飞 IAT 会话（浏览器随后边录边发）
                _voice_stream_cleanup()
                try:
                    sid = _voice_stream_create(_load_cfg())
                    self._send_json({"ok": True, "session": sid})
                except Exception as e:  # noqa: BLE001
                    self._send_json({"ok": False, "error": str(e)}, 400)
            elif path in ("/api/voice/stream/chunk", "/api/voice/stream/stop", "/api/voice/stream/abort"):
                _voice_stream_cleanup()
                item = _voice_stream_get(payload.get("sid", ""))
                if item is None:
                    self._send_json({"ok": False, "error": "流式会话不存在或已过期"}, 404)
                    return
                sess = item["session"]
                if path == "/api/voice/stream/chunk":
                    try:
                        pcm = base64.b64decode(payload.get("audio", ""))
                    except Exception:
                        pcm = b""
                    if len(pcm) > 32768:
                        self._send_json({"ok": False, "error": "音频帧过大"}, 413)
                        return
                    item["frames"] += 1
                    item["bytes"] += len(pcm)
                    if item["frames"] == 1:
                        print(f"   首帧到达 {payload.get('sid','')}：{len(pcm)} 字节")
                    sess.send_audio(pcm)
                    self._send_json({"ok": True, "partial": sess.partial})
                elif path == "/api/voice/stream/stop":
                    try:
                        text = sess.finish(timeout=10.0)
                    except Exception as e:  # noqa: BLE001
                        self._send_json({"ok": False, "error": str(e)}, 400)
                        return
                    finally:
                        with _VOICE_STREAMS_LOCK:
                            _VOICE_STREAMS.pop(payload.get("sid", ""), None)
                    print(f"   ✓ 流式识别完成：{item['frames']} 帧/{item['bytes']} 字节 | {len(text)} 字 | {text[:80]}")
                    self._send_json({"ok": True, "text": text})
                else:  # abort
                    sess.close()
                    with _VOICE_STREAMS_LOCK:
                        _VOICE_STREAMS.pop(payload.get("sid", ""), None)
                    print(f"   ✗ 流式会话中止：{item['frames']} 帧/{item['bytes']} 字节")
                    self._send_json({"ok": True})
            elif path == "/api/settings":
                try:
                    with _SETTINGS_LOCK:
                        saved = _settings_api().save_settings(CONFIG_PATH, payload)
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
  .live-item { display:block; padding:9px 8px;
               border-radius:9px; border-left:3px solid transparent; }
  .phase-group { margin:14px 0 7px; padding:8px 10px; border-radius:8px;
                 background:#eaf2ff; color:#174ea6; font-weight:700; font-size:14px; }
  .timeline .phase-group { margin-top:18px; }
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
  .a-perceive { background:#0369a1; } .a-route { background:#6d28d9; }
  .a-decide { background:#1d4ed8; } .a-act { background:#047857; }
  .a-verify { background:#0f766e; } .a-recovery { background:#c2410c; }
  .a-guard { background:#334155; } .a-finish { background:#15803d; }
  .a-skill { background:#7e22ce; } .a-error { background:#b91c1c; } .a-log { background:#475569; }
  .a-setup { background:#475569; } .a-xml { background:#0e7490; }
  .a-glm_request { background:#be185d; } .a-glm_response { background:#db2777; }
  .a-input { background:#a16207; } .a-ad { background:#b45309; }
  .card.p-setup { border-left:4px solid #475569; } .card.p-xml { border-left:4px solid #0e7490; }
  .card.p-glm_request { border-left:4px solid #be185d; } .card.p-glm_response { border-left:4px solid #db2777; }
  .card.p-perceive { border-left:4px solid #0369a1; } .card.p-route { border-left:4px solid #6d28d9; }
  .card.p-decide { border-left:4px solid #1d4ed8; } .card.p-act { border-left:4px solid #047857; }
  .card.p-verify { border-left:4px solid #0f766e; } .card.p-recovery { border-left:4px solid #c2410c; }
  .card.p-guard { border-left:4px solid #334155; } .card.p-finish { border-left:4px solid #15803d; }
  .card.p-skill { border-left:4px solid #7e22ce; } .card.p-ad { border-left:4px solid #b45309; }
  .card.p-input { border-left:4px solid #a16207; } .card.p-error { border-left:4px solid #b91c1c; }
  .card.p-log { border-left:4px solid #475569; }
  .step-no { color:#7b8794; font-weight:600; }
  .meta { margin:8px 0; font-size:13px; color:#3e4c59; line-height:1.6; word-break:break-all; }
  .meta b { color:#1f2933; }
  [hidden] { display:none !important; }
  header { flex-wrap:wrap; }
  .live-top { flex-wrap:wrap; }
  .badge.unknown,.badge.interrupted,.badge.error,.badge.timeout,.badge.cancelled { background:#fef3c7; color:#92400e; }
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

const eventSummary = LogView.summary;
const eventTechnical = LogView.technical;
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

let curDir = null, pollTimer = null, pollBusy = false, pollEnabled = false;
let liveSteps = [], liveCursor = -1, liveStatus = 'running', liveDebug = false;
let stickBottom = true, liveLimit = 200;
function setLive(t){ const el=$('#live'); if(el) el.textContent=t||''; }
function setLogLink(dir){
  $('#logLink').href=dir?'/logs?task='+encodeURIComponent(dir):'/logs';
  const a=$('#runLog'); if(a){a.href='/api/run-log?task='+encodeURIComponent(dir||'');a.hidden=true;}
}
function stopPoll(){pollEnabled=false;if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=null;}
function startPoll(){pollEnabled=true;if(pollTimer===null&&!pollBusy)pollTimer=setTimeout(()=>{pollTimer=null;tick();},2000);}
function track(dir){stopPoll();curDir=dir;liveCursor=-1;liveSteps=[];liveLimit=200;
  const l=$('#liveList');if(l)l.replaceChildren();
  if(dir)localStorage.setItem('lga_task_dir',dir);else localStorage.removeItem('lga_task_dir');setLogLink(dir);
}
function showCard(on){$('#livePanel').classList.toggle('hidden',!on);}
function setBadge(state,text){const b=$('#liveBadge');b.className='badge '+state;b.textContent=text;}
function renderRunRows(rebuild=false,updates=[]){
 const list=$('#liveList');const selected=liveSteps.filter(s=>liveDebug||!LogView.diagnostic(s));
 if(rebuild){list.innerHTML=selected.slice(-liveLimit).map(s=>LogView.card(s,curDir,true)).join('');}
 else{
  const fragment=document.createDocumentFragment();
  for(const step of updates){if(!liveDebug&&LogView.diagnostic(step))continue;
   const box=document.createElement('div');box.innerHTML=LogView.card(step,curDir,true);fragment.appendChild(box.firstElementChild);}
  list.appendChild(fragment);while(list.childElementCount>liveLimit)list.firstElementChild.remove();
 }
 const primary=liveSteps.filter(s=>!LogView.diagnostic(s)).length;
 $('#liveCount').textContent=primary+' 条进度 · '+(liveSteps.length-primary)+' 条调试';
 $('#runOlder').hidden=selected.length<=liveLimit;
 if(stickBottom)list.scrollTop=list.scrollHeight;
}
function renderLive(d){
 if(d.reset){liveSteps=[];liveCursor=-1;$('#liveList').replaceChildren();}
 const updates=(d.updates||[]).filter(s=>s.idx>liveCursor);
 liveSteps.push(...updates);liveCursor=d.cursor??liveCursor;liveStatus=d.status;
 setBadge(d.status,LogView.statusNames[d.status]||d.status);$('#liveTask').textContent=d.task||'';
 renderRunRows(false,updates);showCard(true);
 $('#runLog').hidden=!d.run_log;
}
async function fetchLive(){
 const dir=curDir;if(!dir)return null;
 let d;
 do{
  d=await LogView.fetchJSON('/api/live?task='+encodeURIComponent(dir)+'&after='+liveCursor);
  if(curDir!==dir)return null;renderLive(d);
 }while(d.has_more);
 return d;
}
async function loadLiveHistory(){return fetchLive();}
async function tick(){
 if(!curDir||pollBusy)return;
 pollBusy=true;const dir=curDir;
 try{
  const d=await fetchLive();if(!d||curDir!==dir)return;
  if(d.status==='running'){
   setMascot('thinking');const silent=Math.max(0,Math.round(d.stats.total_time-(d.last?.at_time||0)));
   setLive('运行中 · 第 '+d.stats.total_steps+' 步 · '+d.stats.total_time+'s'+
    (d.last?' · '+LogView.summary(d.last.action):'')+(silent>=30?' · '+silent+' 秒无新日志，继续等待并刷新':''));
  }else{
   setMascot(d.status==='done'?'happy':'idle');
   setLive((LogView.statusNames[d.status]||d.status)+' · '+d.stats.total_time+'s'+(d.reason?' · '+d.reason:''));stopPoll();
  }
 }catch(error){if(curDir===dir){setLive('日志连接暂不可用，正在重试：'+error.message);pollEnabled=true;}}
 finally{pollBusy=false;if(pollEnabled)startPoll();}
}
$('#runDebug').onchange=e=>{liveDebug=e.target.checked;renderRunRows(true);};
$('#runOlder').onclick=()=>{liveLimit+=200;renderRunRows(true);};

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
  try {
    await LogView.fetchJSON('/api/stop',{method:'POST'});
    setLive('停止请求已发送，正在获取最终状态');
    startPoll(); await tick();
  } catch(error){setLive('停止失败：'+error.message);}
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
let voiceStream = null;   // {sid, stream, ctx, node, rec, startedAt, silentSince}

function f32to16(buf){
  const out = new Int16Array(buf.length);
  for(let i = 0; i < buf.length; i++){
    const s = Math.max(-1, Math.min(1, buf[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}
function resampleTo16k(buf, fromRate){
  if(fromRate === 16000) return buf;
  const n = Math.round(buf.length * 16000 / fromRate);
  const out = new Float32Array(n);
  for(let i = 0; i < n; i++){
    const pos = i * (buf.length - 1) / Math.max(1, n - 1);
    const i0 = Math.floor(pos), i1 = Math.min(buf.length - 1, i0 + 1), f = pos - i0;
    out[i] = buf[i0] * (1 - f) + buf[i1] * f;
  }
  return out;
}
function fillTaskTextLive(text){
  const inp = $('#taskInput'); if(!inp) return;
  if(document.activeElement !== inp) inp.value = text || '';
}
function releaseMedia(vs){
  try { vs.node && vs.node.disconnect(); } catch(e){}
  try { vs.stream && vs.stream.getTracks().forEach(t => t.stop()); } catch(e){}
  try { vs.ctx && vs.ctx.close(); } catch(e){}
  const btn = $('#voiceBtn'); if(btn) btn.classList.remove('recording');
}
async function endVoiceStream(final){
  const vs = voiceStream; voiceStream = null;
  if(!vs) return;
  releaseMedia(vs);
  if(!final){
    setMascot('idle');
    try { await fetch('/api/voice/stream/abort', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sid: vs.sid})}); } catch(e){}
    return;
  }
  setMascot('thinking');
  toast('🎤 说话结束，识别收尾中…');
  try {
    const r = await fetch('/api/voice/stream/stop', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sid: vs.sid})});
    const j = await r.json();
    if(j.ok){ fillTaskText(j.text || ''); toast(j.text ? '✓ 已识别，确认后启动' : '✗ 未识别到内容'); }
    else toast('✗ ' + (j.error || '识别失败'));
    setMascot('idle');
  } catch(e){
    toast('✗ 请求出错：' + e); setMascot('idle');
  }
}
async function startVoiceInput(){
  if(voiceStream){ endVoiceStream(true); return; }   // 录音中再点一次麦克风 = 结束
  const btn = $('#voiceBtn'); if(!btn) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
  } catch(e){
    toast('✗ 无法访问麦克风：' + e); setMascot('idle'); return;
  }
  btn.classList.add('recording');
  setMascot('thinking');
  toast('🎤 录音中，实时出字…');
  try {
    const r0 = await fetch('/api/voice/stream/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'});
    const j0 = await r0.json();
    if(!j0.ok){
      toast('✗ ' + (j0.error || '启动识别失败'));
      stream.getTracks().forEach(t => t.stop());
      btn.classList.remove('recording'); setMascot('idle');
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC({sampleRate: 16000});
    if(ctx.state === 'suspended'){ await ctx.resume(); }
    const src = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(1024, 1, 1);   // ~64ms/帧 @16k
    const vs = {sid: j0.session, stream, ctx, node, rec: true, startedAt: Date.now(), silentSince: 0, started: false};
    voiceStream = vs;
    node.onaudioprocess = async (ev) => {
      if(!vs.rec) return;
      let f32 = resampleTo16k(ev.inputBuffer.getChannelData(0), ctx.sampleRate);
      if(!f32.length) return;
      let sum = 0;
      for(let i = 0; i < f32.length; i++) sum += f32[i] * f32[i];
      const rms = Math.sqrt(sum / f32.length);
      const now = Date.now();
      if(rms >= 0.003){ vs.started = true; vs.silentSince = 0; }
      else if(vs.started && !vs.silentSince){ vs.silentSince = now; }
      if((vs.started && vs.silentSince && now - vs.silentSince > 800) ||
         now - vs.startedAt > 15000){
        vs.rec = false;
        endVoiceStream(true);
        return;
      }
      try {
        const pcm = new Uint8Array(f32to16(f32).buffer);
        let b64 = '';
        for(let i = 0; i < pcm.length; i += 8192){
          b64 += btoa(String.fromCharCode.apply(null, pcm.subarray(i, i + 8192)));
        }
        const r = await fetch('/api/voice/stream/chunk', {method:'POST', headers:{'Content-Type':'application/json'},
                                                           body: JSON.stringify({sid: vs.sid, audio: b64})});
        const j = await r.json();
        if(j.partial) fillTaskTextLive(j.partial);
      } catch(e){ /* 单帧失败不打断录音 */ }
    };
    const mute = ctx.createGain(); mute.gain.value = 0;   // 防回授
    src.connect(node); node.connect(mute); mute.connect(ctx.destination);
  } catch(e){
    toast('✗ ' + e);
    stream.getTracks().forEach(t => t.stop());
    const cur = voiceStream; voiceStream = null;
    if(cur){
      releaseMedia(cur);
      try { await fetch('/api/voice/stream/abort', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sid: cur.sid})}); } catch(_){}
    }
    btn.classList.remove('recording'); setMascot('idle');
  }
}
$('#voiceBtn').onclick = startVoiceInput;

// 列表滚动粘性：用户手动上滚看历史时，别把他拽回底部
const _ll = $('#liveList');
if(_ll) _ll.addEventListener('scroll', () => {
  stickBottom = (_ll.scrollHeight - _ll.scrollTop - _ll.clientHeight) < 24;
});

// 刷新页面后继续跟踪本面板启动的那个任务（先用完整轨迹补回历史，再进入 2 秒轮询）
const saved = localStorage.getItem('lga_task_dir');
if(saved){track(saved);pollEnabled=true;tick();}
else { setMascot('idle'); setLive(''); }
"""

LOGS_JS = r"""
// 给本标签命名，下指令页的「任务日志」会复用这个标签（切换），而不是新开一个
try { window.name = 'lg_logs'; } catch(e){}

let currentDir=null,timer=null,logsBusy=false,autoRefresh=true,logCursor=-1;
let logSteps=[],logDebug=false,logLimit=200,lastDetail=null;
function stopLogTimer(){if(timer!==null)clearTimeout(timer);timer=null;}
function renderLogRows(rebuild=false,updates=[]){
 const tl=$('#timeline'),main=$('.main');
 const atBottom=main.scrollHeight-main.scrollTop-main.clientHeight<48;
 const selected=logSteps.filter(s=>logDebug||!LogView.diagnostic(s));
 if(rebuild)tl.innerHTML=selected.slice(-logLimit).map(s=>LogView.card(s,currentDir)).join('');
 else{
  const fragment=document.createDocumentFragment();
  for(const step of updates){if(!logDebug&&LogView.diagnostic(step))continue;
   const box=document.createElement('div');box.innerHTML=LogView.card(step,currentDir);fragment.appendChild(box.firstElementChild);}
  tl.appendChild(fragment);while(tl.childElementCount>logLimit)tl.firstElementChild.remove();
 }
 $('#logsOlder').hidden=selected.length<=logLimit;
 if(atBottom&&updates.length)requestAnimationFrame(()=>{main.scrollTop=main.scrollHeight;});
}
async function loadTasks(prefer){
 try{
  const list=await LogView.fetchJSON('/api/tasks');const sel=$('#taskSel');sel.replaceChildren();
  for(const item of list){const option=document.createElement('option');option.value=item.task_dir;option.textContent=item.title||item.task;sel.appendChild(option);}
  const dir=list.find(x=>x.task_dir===prefer)?.task_dir||list[0]?.task_dir;
  if(dir){sel.value=dir;await loadTask(dir,true);}
 }catch(error){$('#timeline').textContent='读取任务失败：'+error.message;}
}
async function loadTask(dir,reset=false){
 if(!dir)return;
 if(currentDir!==dir||reset){currentDir=dir;logCursor=-1;logSteps=[];lastDetail=null;logLimit=200;stopLogTimer();$('#timeline').replaceChildren();}
 if(logsBusy)return;
 logsBusy=true;const requestDir=dir;
 try{
  let d;
  do{
   d=await LogView.fetchJSON('/api/live?task='+encodeURIComponent(dir)+'&after='+logCursor);
   if(currentDir!==requestDir)return;
   if(d.reset){logSteps=[];logCursor=-1;$('#timeline').replaceChildren();}
   const updates=d.updates.filter(s=>s.idx>logCursor);logSteps.push(...updates);logCursor=d.cursor;
   lastDetail=d;$('#stats').innerHTML=LogView.stats(d);renderLogRows(false,updates);
   $('#logsRunLog').hidden=!d.run_log;$('#logsRunLog').href='/api/run-log?task='+encodeURIComponent(dir);
  }while(d.has_more);
  $('#logError').textContent='';
 }catch(error){if(currentDir===requestDir)$('#logError').textContent='日志连接暂不可用：'+error.message+'；可点击加载重试';}
 finally{
  logsBusy=false;
  if(currentDir!==requestDir){loadTask(currentDir);return;}
  const active=autoRefresh&&(!lastDetail||lastDetail.status==='running');
  $('#autoBtn').dataset.on=active?'1':'0';$('#autoBtn').textContent='自动刷新：'+(active?'开':'关');
  if(active){
   stopLogTimer();timer=setTimeout(()=>{timer=null;loadTask(currentDir);},2000);
  }
 }
}
$('#taskSel').onchange=()=>loadTask($('#taskSel').value);
$('#loadBtn').onclick=()=>loadTask($('#taskSel').value);
$('#autoBtn').onclick=()=>{autoRefresh=$('#autoBtn').dataset.on!=='1';stopLogTimer();$('#autoBtn').dataset.on=autoRefresh?'1':'0';$('#autoBtn').textContent='自动刷新：'+(autoRefresh?'开':'关');if(autoRefresh)loadTask(currentDir);};
$('#logsDebug').onchange=e=>{logDebug=e.target.checked;renderLogRows(true);};
$('#logsOlder').onclick=()=>{logLimit+=200;renderLogRows(true);};
loadTasks(new URLSearchParams(location.search).get('task'));
"""

SETTINGS_JS = r"""
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
            silence_threshold:numOrNull('#voiceSilenceThreshold')},
    apps: appRows(), device_serial: currentDeviceSerial
  };
}
function status(id, message, good){
  const node = $(id); node.textContent = message;
  node.className = 'settings-message ' + (good ? 'good' : 'error');
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
    $('#voiceAppid').value = data.voice.appid || '';
    $('#voiceApiKey').value = data.voice.api_key || '';
    $('#voiceApiSecret').value = data.voice.api_secret || '';
    $('#voiceIatUrl').value = data.voice.iat_url || '';
    $('#voiceMaxSeconds').value = data.voice.max_seconds;
    $('#voiceSilenceSeconds').value = data.voice.silence_seconds;
    $('#voiceSilenceThreshold').value = data.voice.silence_threshold;
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
  } catch(error){ status('#saveStatus', error.message, false); }
}
async function postSettings(path, body){
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                                     body:JSON.stringify(body)});
  const data = await response.json();
  if(!response.ok) throw new Error(data.error || data.message || '请求失败');
  return data;
}
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
    renderAppRows(data.apps || []);
    status('#saveStatus', currentDeviceSerial
      ? '设置已保存；目标应用包名已针对当前设备核对。'
      : '设置已保存；应用包名尚未针对当前设备核对，请点「重新加载」后再直启。', true);
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
readSettings();
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
      <button id="voiceBtn" type="button" title="语音录入" aria-label="语音录入">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="9" y="2.5" width="6" height="11" rx="3"/>
          <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/>
          <path d="M12 18.2v3.3"/>
        </svg>
      </button>
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
        <label class="mini"><input type="checkbox" id="runDebug"> 调试详情</label>
        <button class="mini" id="runOlder" hidden>显示更早记录</button>
        <a class="mini" id="runLog" hidden target="_blank" rel="noopener">进程输出</a>
      </div>
      <div class="live-list" id="liveList"></div>
    </div>
  </div>
</div>
<script src="/log_view.js"></script>
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
  <button id="autoBtn" class="mini" data-on="1">自动刷新：开</button>
  <label class="mini"><input type="checkbox" id="logsDebug"> 调试详情</label>
  <button class="mini" id="logsOlder" hidden>显示更早记录</button>
  <a class="mini" id="logsRunLog" hidden target="_blank" rel="noopener">进程输出</a>
  <a class="mini" href="/settings">⚙ 设置</a>
  <a class="mini" href="/" target="lg_run">← 去下指令</a>
</header>
<div class="main">
  <div id="logError" class="meta" role="status"></div>
  <div id="stats" class="stats"></div>
  <div id="timeline" class="timeline"><div class="empty">请在上方选择历史任务加载回放…</div></div>
</div>
<script src="/log_view.js"></script>
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
        <label class="field">最长录音（秒）<input id="voiceMaxSeconds" type="number" step="1" min="3" max="120" placeholder="15"></label>
        <label class="field">静音停止（秒）<input id="voiceSilenceSeconds" type="number" step="0.1" min="0.2" max="5" placeholder="0.8"></label>
        <label class="field">静音阈值 RMS<input id="voiceSilenceThreshold" type="number" step="1" min="50" max="3000" placeholder="120"></label>
      </div>
      <div class="settings-actions"><button id="asrVerifyBtn" type="button">验证连接</button></div>
      <div id="asrVerifyStatus" class="settings-message" role="status" aria-live="polite"></div>
    </div>
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
<script src="/log_view.js"></script>
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
