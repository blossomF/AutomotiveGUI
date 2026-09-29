"""单台车机的 ADB 原语。

移植自 ``android-agent/gitd/bots/common/adb.py`` —— 那个文件只依赖标准库，
这正是值得整体抽出的原因。对原文的四处刻意偏离，见 技术方案评估.md §2：

  A-1  Droidrun Portal 快速通道（原文 :357-420）整体删除——它需要在设备上
        安装 portal APK，而竞赛规则禁止安装任何东西。
  A-4  保留 ``input_text_arg``（原文 :71-82）：我们输入的文字由 LLM 生成，
        而 ``adb shell`` 会用*设备端* shell 重新解析 argv，注入的
        ``$(reboot)`` 否则会在车机上直接执行。
  C-8  保留 ``exec-out`` 抓取（原文 :48-50）+ ``strip_to_png_signature``
        （原文 :36-45）。签名裁剪在这里不是优化——车机是多屏设备，
        ``screencap`` 会在流前加一行警告，把图像弄坏。
  C-11 每次调用都带 ``-s <serial>``；原文的设备探测直接跑裸
        ``adb shell wm size``，会锁到错误的设备上。

XML 解析辅助函数是模块级函数，而不是原文里的 ``Device`` 方法，
这样 ``perception/tree.py`` 和测试可以在没有设备的情况下使用。
"""

from __future__ import annotations

import html
import re
import shlex
import struct
import subprocess
import time
import unicodedata
from typing import NamedTuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KEYCODE_RE = re.compile(r"^KEYCODE_[A-Z0-9_]+$")


class ADBError(RuntimeError):
    """adb 命令本身失败：可执行文件缺失、超时或非零退出。

    继承 ``RuntimeError``，让已经做宽泛捕获的调用方继续可用。
    这个类型的意义在于原文 :85-101 做出的区分：被吞掉、返回 ``""``
    的失败在下游会被读成“空的成功”，比抛异常糟糕得多。
    """

    def __init__(self, args, returncode: int, stderr: str = "", stdout: str = ""):
        self.cmd_args = tuple(args)
        self.returncode = returncode
        self.stderr = stderr or ""
        self.stdout = stdout or ""
        parts = [part.strip() for part in (self.stderr, self.stdout) if part.strip()]
        detail = "\n".join(dict.fromkeys(parts)) or "no output"
        cmd = "adb " + " ".join(str(a) for a in args)
        super().__init__(f"{cmd} failed (exit {returncode}): {detail}")


class ADBResult(NamedTuple):
    """:meth:`Device.adb_soft` 的返回——非零退出码是数据，不是错误。"""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# ── 纯辅助函数（不需要设备） ──────────────────────────────────────────────────


def strip_to_png_signature(raw: bytes) -> bytes:
    """裁剪掉 PNG 魔数之前的一切内容。

    多屏设备上 ``screencap -p`` 的 stdout 前面会带一行
    "[Warning] Multiple displays were found..."。车机正是这种情况，
    朴素的读取会得到损坏的图像。（原文 adb.py:39-45）
    """
    idx = raw.find(PNG_SIGNATURE)
    return raw[idx:] if idx > 0 else raw


def png_size(raw: bytes) -> tuple[int, int] | None:
    """从 PNG IHDR 块读出 (宽, 高)。

    对“VLM 实际看到什么”是权威答案。优先于 ``wm size``——后者在
    多屏车机上可能报告与捕获到的帧缓冲不同的逻辑尺寸。
    """
    if len(raw) < 24 or not raw.startswith(PNG_SIGNATURE):
        return None
    if raw[12:16] != b"IHDR":
        return None
    w, h = struct.unpack_from(">II", raw, 16)
    return int(w), int(h)


def normalize_keycode(key: str) -> str:
    """校验 Android 键码，缺前缀时补上 ``KEYCODE_``。

    ``adb shell input keyevent <key>`` 会经过设备端 shell 重新解析，
    所以 agent 可控的值如 ``KEYCODE_HOME; reboot`` 会在车机上执行
    ``reboot``。真实的键码永远只是 ``KEYCODE_[A-Z0-9_]+``。
    （原文 adb.py:56-68）
    """
    if not key.startswith("KEYCODE_"):
        key = "KEYCODE_" + key.upper()
    if not _KEYCODE_RE.match(key):
        raise ValueError(f"invalid keycode {key!r}: expected KEYCODE_[A-Z0-9_]+")
    return key


def input_text_arg(text: str) -> str:
    """为 ``adb shell input text`` 做 shell 引用，保证逐字输入。

    空格在引号内变成 ``%s``（input 自己的空格转义）。调用方应先用
    :func:`ascii_typeable` 转成纯 ASCII。（原文 adb.py:71-82）
    """
    return shlex.quote(text.replace(" ", "%s"))


def ascii_typeable(text: str) -> str:
    """把文本折叠成 ``input text`` 能发送的最接近 ASCII。

    ``adb shell input text`` 只收 ASCII——一个非 ASCII 字符会让整条
    命令失败、输入框留空。先 NFKD 分解再丢掉组合符号
    （"café" -> "cafe"）。对 CJK 这会得到空字符串，提示调用方
    改走拼音兜底。（原文 adb.py:20-33）
    """
    if text.isascii():
        return text
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return folded


# ── XML 树解析（纯函数，操作 ``uiautomator dump`` 的输出） ────────────────────

_NODE_RE = re.compile(r"<node\b[^>]*?/?>", re.S)
_BOUNDS_RE = re.compile(r'bounds="\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"')


def _attr(node: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', node)
    return html.unescape(m.group(1)).strip() if m else ""


def node_attr(node: str, name: str) -> str:
    """Read one uiautomator node attribute with XML entities decoded."""
    return _attr(node, name)


def nodes(xml: str) -> list[str]:
    """把 uiautomator dump 拆成一个个 ``<node ...>`` 字符串。"""
    return _NODE_RE.findall(xml or "")


def node_text(node: str) -> str:
    return _attr(node, "text")


def node_desc(node: str) -> str:
    return _attr(node, "content-desc")


def node_rid(node: str) -> str:
    return _attr(node, "resource-id")


def node_class(node: str) -> str:
    return _attr(node, "class")


def node_clickable(node: str) -> bool:
    return _attr(node, "clickable") == "true"


def node_bounds(node: str) -> tuple[int, int, int, int] | None:
    m = _BOUNDS_RE.search(node)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def node_center(node: str) -> tuple[int, int] | None:
    b = node_bounds(node)
    if not b:
        return None
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)


def bounds_center(bounds_str: str) -> tuple[int, int]:
    """``'[x1,y1][x2,y2]'`` -> 格子中心。（原文 adb.py:467-470）"""
    n = list(map(int, re.findall(r"\d+", bounds_str)))
    return ((n[0] + n[2]) // 2, (n[1] + n[3]) // 2)


def find_nodes(
    xml: str,
    *,
    text: str | None = None,
    desc: str | None = None,
    rid: str | None = None,
    clickable_only: bool = False,
) -> list[str]:
    """过滤节点；text/desc 按子串、忽略大小写匹配。"""
    out = []
    for n in nodes(xml):
        if clickable_only and not node_clickable(n):
            continue
        if rid is not None and node_rid(n) != rid:
            continue
        if text is not None and text.lower() not in node_text(n).lower():
            continue
        if desc is not None and desc.lower() not in node_desc(n).lower():
            continue
        out.append(n)
    return out


def find_node(xml: str, *, text: str | None = None, desc: str | None = None, rid: str | None = None) -> str | None:
    """按 ``text`` → ``desc`` → ``rid`` 的优先级找第一个匹配。

    Skill 的 hint 通过这里解析。什么都没匹配到时返回 ``None``，
    让调用方抛 ``SkillMiss`` 并降级到通用通道，而不是让整个任务失败。
    """
    probes = []
    if text:
        probes.append({"text": text})
    if desc:
        probes.append({"desc": desc})
    if rid:
        probes.append({"rid": rid})
    for kwargs in probes:
        found = find_nodes(xml, clickable_only=True, **kwargs)
        if not found:
            found = find_nodes(xml, **kwargs)  # 重试一次：忽略 clickable，有些控件没标记
        if found:
            return found[0]
    return None


# ── 设备 ───────────────────────────────────────────────────────────────────────


class Device:
    """针对一台已连接车机的 ADB + XML 原语。"""

    def __init__(self, serial: str, *, default_delay: float = 0.4, display: int | None = None):
        self.serial = serial
        self.default_delay = default_delay
        self.trace_log = None  # Runner.prepare 注入；只记录当前任务的 XML dump
        # Android 9+ 支持 `screencap --display <id>`。否则车机上的默认
        # 捕获可能来自仪表屏而不是中控屏，而规则明确把后者之外的屏
        # 划出范围（规则 4.2）。
        self.display = display

    # ── 调用 ─────────────────────────────────────────────────────────────────

    def _run(self, args, timeout: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["adb", "-s", self.serial, *args],
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise ADBError(args, 127, "adb executable not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise ADBError(args, -1, f"timed out after {timeout}s") from e

    def adb(self, *args, timeout: float = 30) -> str:
        """运行 adb，返回解码并 strip 的 stdout。非零退出抛 ``ADBError``。"""
        r = self._run(args, timeout)
        if r.returncode != 0:
            raise ADBError(args, r.returncode, _dec(r.stderr), _dec(r.stdout))
        return _dec(r.stdout).strip()

    def adb_bytes(self, *args, timeout: float = 30) -> bytes:
        """运行 adb，返回原始 stdout——用于 ``exec-out`` 的二进制流。"""
        r = self._run(args, timeout)
        if r.returncode != 0:
            raise ADBError(args, r.returncode, _dec(r.stderr), "")
        return r.stdout

    def adb_soft(self, *args, timeout: float = 30) -> ADBResult:
        """运行 adb，非零退出不抛异常。

        用于“失败是正常答案”的探测（这个 IME 装了吗？）。
        adb 可执行文件缺失或超时仍然抛异常——那绝不是正常结果。
        """
        r = self._run(args, timeout)
        return ADBResult(r.returncode, _dec(r.stdout).strip(), _dec(r.stderr).strip())

    # ── 抓帧 ─────────────────────────────────────────────────────────────────

    def screenshot(self, timeout: float = 15) -> bytes:
        """一次往返把帧缓冲抓成 PNG 字节。

        两次往返（``screencap -p /sdcard/x.png`` 再 ``pull``，见
        ``LightGUIAgent/lightguiagent/agent.py:117-135``）每步多一次
        文件传输，而它正处在性能分的关键路径上。

        永远走 ``exec-out``，不走 ``shell``：后者会分配 PTY，把
        ``\\n`` 改写成 ``\\r\\n``，弄坏 PNG 流。
        """
        args = ["exec-out", "screencap", "-p"]
        if self.display is not None:
            # 仅 Android 9+；更老的构建会忽略该参数或报错，
            # 调用方看到的是带命令名的普通 ADBError。
            args += ["--display", str(int(self.display))]
        raw = self.adb_bytes(*args, timeout=timeout)
        return strip_to_png_signature(raw)

    def list_displays(self, timeout: float = 8) -> list[str]:
        """SurfaceFlinger 报告的显示器 id。用于选择 ``adb.display``。"""
        out = self.adb_soft("shell", "dumpsys", "SurfaceFlinger", "--display-id", timeout=timeout).stdout
        return [m for m in re.findall(r"Display ID:\s*(\d+)", out)]

    def dump_xml(self, remote: str = "/sdcard/autogui_ui.xml", timeout: float = 8) -> str:
        """以 uiautomator XML 形式获取 UI 层级。

        ``uiautomator dump`` 默认拒绝覆盖，所以先删。再用一次
        ``exec-out cat`` 读回文件，避免第二次 adb 传输。
        （原文 adb.py:456-463，去掉 portal 快速通道——见 A-1）

        超时取 8s：WebView 重的页面（如车机/手机 H5 搜索页）uiautomator
        可能挂起 10s+ 且最终仍返回空树（实测 11.4s/0 节点）。宁可快速
        失败返回空树，让 route 走 VLM 网格兜底。
        """
        started = time.perf_counter()
        stage = "remove_old"
        try:
            removed = self.adb_soft("shell", "rm", "-f", remote, timeout=timeout)
            if not removed.ok:
                self._trace_xml("", started, "remove_failed", removed.stderr or removed.stdout,
                                remote, remove_exit=removed.returncode)
                return ""
            stage = "dump"
            r = self.adb_soft("shell", "uiautomator", "dump", remote, timeout=timeout)
        except ADBError as e:  # 抓树失败按空树处理，走 VLM 兜底
            self._trace_xml("", started, "timeout" if e.returncode == -1 else "adb_error",
                            str(e), remote, stage=stage, adb_exit=e.returncode)
            return ""
        dump_meta = dict(dump_exit=r.returncode, dump_stdout=r.stdout,
                         dump_stderr=r.stderr)
        if not r.ok:
            xml = ""
            self._trace_xml(xml, started, "dump_failed", (r.stdout + " " + r.stderr).strip(),
                            remote, **dump_meta)
            return xml
        try:
            xml = self.adb("exec-out", "cat", remote, timeout=timeout)
        except ADBError as e:
            xml = ""
            self._trace_xml(xml, started, "read_error", str(e), remote,
                            read_exit=e.returncode, **dump_meta)
            return xml
        remote_file = ""
        if not xml:
            # An empty cat does not reveal whether dump created a zero-byte
            # file. Probe only on this rare path to keep normal capture fast.
            try:
                probe = self.adb_soft("shell", "ls", "-l", remote, timeout=timeout)
                remote_file = f"exit={probe.returncode} {(probe.stdout or probe.stderr).strip()}"
            except ADBError as e:
                remote_file = str(e)
        self._trace_xml(xml, started, "ok" if xml else "empty", "", remote,
                        remote_file=remote_file, **dump_meta)
        return xml

    def _trace_xml(self, xml: str, started: float, status: str, detail: str,
                   remote: str, **diagnostics) -> None:
        log = self.trace_log
        if log is None:
            return
        metrics = dict(status=status, adb_detail=detail[:300],
                       command=f"uiautomator dump {remote}; exec-out cat {remote}",
                       dump_ms=round((time.perf_counter() - started) * 1000, 1))
        metrics.update({key: value[:500] if isinstance(value, str) else value
                        for key, value in diagnostics.items()})
        if log.debug:
            log.trace_text("xml", xml, **metrics)
        else:
            log.phase("xml", f"XML dump {status} ({len(xml)} 字符)", **metrics)

    # ── 输入 ─────────────────────────────────────────────────────────────────

    def tap(self, x: int, y: int, delay: float | None = None) -> None:
        self.adb("shell", "input", "tap", str(int(x)), str(int(y)))
        time.sleep(delay if delay is not None else self.default_delay)

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        ms: int = 400,
        delay: float | None = None,
    ) -> None:
        self.adb(
            "shell", "input", "swipe",
            str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(ms)),
        )
        time.sleep(delay if delay is not None else self.default_delay)

    def long_press(self, x: int, y: int, ms: int = 800, delay: float | None = None) -> None:
        """按住 ``ms`` 的零距离 swipe——``input`` 提供的唯一长按形式。"""
        self.swipe(x, y, x, y, ms=ms, delay=delay)

    def keyevent(self, key: str, delay: float | None = None) -> None:
        self.adb("shell", "input", "keyevent", normalize_keycode(key))
        time.sleep(delay if delay is not None else self.default_delay)

    def back(self, delay: float | None = None) -> None:
        self.keyevent("KEYCODE_BACK", delay)

    def home(self, delay: float | None = None) -> None:
        self.keyevent("KEYCODE_HOME", delay)

    def type_ascii(self, text: str, delay: float | None = None) -> None:
        """通过 ``input text`` 输入。仅 ASCII；见 :func:`ascii_typeable`。"""
        self.adb("shell", "input", "text", input_text_arg(ascii_typeable(text)))
        time.sleep(delay if delay is not None else self.default_delay)

    # ── 应用 ─────────────────────────────────────────────────────────────────

    def launch_app(self, package: str, activity: str | None = None, delay: float = 2.0) -> str:
        """启动一个 App——程序化的“点图标”等价物。

        优先用 LAUNCHER intent（真实点击做的事），因为显式
        ``-n`` 组件在某些厂商机型上会启动到后台。
        """
        if not re.fullmatch(r"[A-Za-z0-9._]+(/[A-Za-z0-9._]+)?", package):
            raise ValueError(f"invalid package name: {package!r}")
        if activity:
            r = self.adb_soft("shell", "am", "start", "-n", activity, timeout=10)
            if r.ok and "Error" not in r.stdout:
                time.sleep(delay)
                return "am"
        output = self.adb("shell", "monkey", "-p", package,
                          "-c", "android.intent.category.LAUNCHER", "1", timeout=10)
        if "Events injected: 1" not in output:
            raise ADBError(("shell", "monkey", "-p", package), 0,
                           stdout=f"没有启动活动：{output.strip()[:300]}")
        time.sleep(delay)
        return "monkey"

    def stop_app(self, package: str, delay: float = 0.6) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._]+", package):
            raise ValueError(f"invalid package name: {package!r}")
        self.adb("shell", "am", "force-stop", package)
        time.sleep(delay)

    def current_app(self, timeout: float = 8) -> dict:
        """尽力获取前台 Activity。返回 ``{package, activity}``（可能为空）。

        不同 Android 版本的解析不同，所以有两个模式、以及宽松的
        ``default``/``mResumedActivity`` 键。主要用于决策日志
        （“当前 Activity 已切换至 …”），很少用于控制流。
        """
        out = self.adb_soft("shell", "dumpsys", "activity", "activities", timeout=timeout).stdout
        for pat in (
            r"mResumedActivity:\s*ActivityRecord\{[^}]*\s+([^\s/]+)/([^\s}]+)",
            r"ResumedActivity:\s*ActivityRecord\{[^}]*\s+([^\s/]+)/([^\s}]+)",
            r"mFocusedApp:\s*ActivityRecord\{[^}]*\s+([^\s/]+)/([^\s}]+)",
        ):
            m = re.search(pat, out)
            if m:
                return {"package": m.group(1), "activity": m.group(2)}
        return {"package": "", "activity": ""}

    def list_packages(self, timeout: float = 15) -> list[str]:
        out = self.adb("shell", "pm", "list", "packages", timeout=timeout)
        return sorted(line.split(":", 1)[1] for line in out.splitlines() if line.startswith("package:"))

    # ── 设备信息 ─────────────────────────────────────────────────────────────

    def get_prop(self, key: str, timeout: float = 5) -> str:
        if not re.fullmatch(r"[a-z0-9._]+", key):
            raise ValueError(f"invalid prop name: {key!r}")
        return self.adb("shell", "getprop", key, timeout=timeout)

    def screen_size(self, timeout: float = 15) -> tuple[int, int]:
        """捕获帧缓冲的 (宽, 高)。

        解码截图，而不是相信 ``wm size``：多屏车机上两者可能不一致，
        而截图才是 VLM 看到的。只有 PNG 头无法解析时才回退到
        ``wm size``（带 ``-s``，与原文 settings.py:137 不同）。
        """
        try:
            size = png_size(self.screenshot(timeout=timeout))
            if size:
                return size
        except ADBError:
            pass
        out = self.adb_soft("shell", "wm", "size", timeout=timeout).stdout
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        raise ADBError(("wm", "size"), 1, "could not determine screen size")

    def wait_for(
        self,
        text: str,
        timeout: float = 12,
        interval: float = 1.0,
        xml: str | None = None,
    ) -> str:
        """轮询 UI 树直到出现 ``text``；返回那份 XML。超时抛 ``TimeoutError``。

        接受预取的 ``xml``，让刚 dump 过树的调用方不用付两次成本。

        匹配是**空白不敏感**的：车机上"输入的关键词"和"界面上显示的关键词"
        空格经常对不上——控件树里是 ``钢铁侠 第一季``（或反过来），而技能参数
        是 ``钢铁侠第一季``。用裸 ``in`` 比会直接误判成"没出现"然后超时，
        实测就是这么挂的（skill 的 wait_for 10s 超时 → 配方降级 → 通用路径
        从零开始，白白烧掉一步）。所以两边都去掉全部空白再比。
        顺带把 XML 实体还原（``&amp;`` 等），否则含 ``&``/``<`` 的文本永远匹配不上。
        """
        needle = _normalize_for_match(text)
        deadline = time.time() + timeout
        while True:
            current = xml if xml is not None else self.dump_xml()
            if not needle or needle in _normalize_for_match(current):
                return current
            if time.time() >= deadline:
                raise TimeoutError(f"timed out after {timeout}s waiting for {text!r}")
            time.sleep(interval)
            xml = None


_XML_ENTITIES = {
    "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&amp;": "&",
}


def _normalize_for_match(s: str) -> str:
    """去空白 + 还原常见 XML 实体，用于 ``wait_for`` 的宽松包含判断。"""
    for ent, ch in _XML_ENTITIES.items():
        if ent in s:
            s = s.replace(ent, ch)
    return re.sub(r"\s+", "", s)


def _dec(b) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return b or ""


def list_connected() -> list[str]:
    """处于 ``device`` 状态的设备 serial 列表。"""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def get_device(serial: str | None = None, display: int | None = None) -> Device:
    """解析出唯一设备；恰好一台在线时自动选择。"""
    if not serial or serial == "auto":
        connected = list_connected()
        if len(connected) == 1:
            return Device(connected[0], display=display)
        if not connected:
            raise RuntimeError("no ADB device connected (check `adb devices` and USB debugging)")
        raise RuntimeError(f"multiple devices connected — specify serial. Found: {connected}")
    return Device(serial, display=display)
