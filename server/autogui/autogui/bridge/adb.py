"""ADB primitives for one head unit.

Ported from ``android-agent/gitd/bots/common/adb.py`` — that file depends only on the
standard library, which is why it was worth extracting. See 技术方案评估.md §2 for the
four deliberate deviations from the original:

  A-1  Droidrun Portal fast path (original :357-420) dropped entirely — it needs a
       portal APK on the device, and the contest rules forbid installing anything.
  A-4  ``input_text_arg`` (original :71-82) kept: text we type is LLM-generated, and
       ``adb shell`` re-parses argv through the *device* shell, so an injected
       ``$(reboot)`` would otherwise execute on the head unit.
  C-8  ``exec-out`` capture (original :48-50) + ``strip_to_png_signature`` (original
       :36-45) kept. The signature trim is not an optimisation here — head units are
       multi-display, and ``screencap`` prefixes a warning line that corrupts the
       stream.
  C-11 every call carries ``-s <serial>``; the original's device probing ran bare
       ``adb shell wm size`` and would latch onto the wrong device.

XML parsing helpers are module-level functions rather than ``Device`` methods as in
the original, so ``perception/tree.py`` and the tests can use them without a device.
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
    """adb itself failed: missing binary, timeout, or nonzero exit.

    Subclasses ``RuntimeError`` so callers that already catch broadly keep working.
    The point of the type is the distinction the original makes at :85-101: a
    swallowed failure that returns ``""`` reads as an empty success downstream, which
    is much worse than an exception.
    """

    def __init__(self, args, returncode: int, stderr: str = "", stdout: str = ""):
        self.cmd_args = tuple(args)
        self.returncode = returncode
        self.stderr = stderr or ""
        self.stdout = stdout or ""
        detail = self.stderr.strip() or self.stdout.strip() or "no output"
        cmd = "adb " + " ".join(str(a) for a in args)
        super().__init__(f"{cmd} failed (exit {returncode}): {detail}")


class ADBResult(NamedTuple):
    """Result of :meth:`Device.adb_soft` — a nonzero exit is data, not an error."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# ── pure helpers (no device needed) ──────────────────────────────────────────────


def strip_to_png_signature(raw: bytes) -> bytes:
    """Trim everything before the PNG magic bytes.

    Multi-display devices prefix ``screencap -p`` stdout with a
    "[Warning] Multiple displays were found..." line. Head units are exactly that
    case, so a naive read yields a corrupt image. (original adb.py:39-45)
    """
    idx = raw.find(PNG_SIGNATURE)
    return raw[idx:] if idx > 0 else raw


def png_size(raw: bytes) -> tuple[int, int] | None:
    """Read (width, height) from a PNG IHDR chunk.

    Authoritative for what the VLM actually sees. Prefer this over ``wm size``,
    which on multi-display head units can report a logical size that differs from
    the captured framebuffer.
    """
    if len(raw) < 24 or not raw.startswith(PNG_SIGNATURE):
        return None
    if raw[12:16] != b"IHDR":
        return None
    w, h = struct.unpack_from(">II", raw, 16)
    return int(w), int(h)


def normalize_keycode(key: str) -> str:
    """Validate an Android keycode, adding the ``KEYCODE_`` prefix if absent.

    ``adb shell input keyevent <key>`` re-parses through the device shell, so an
    agent-controlled value like ``KEYCODE_HOME; reboot`` would run ``reboot`` on
    the head unit. Real keycodes are always bare ``KEYCODE_[A-Z0-9_]+``.
    (original adb.py:56-68)
    """
    if not key.startswith("KEYCODE_"):
        key = "KEYCODE_" + key.upper()
    if not _KEYCODE_RE.match(key):
        raise ValueError(f"invalid keycode {key!r}: expected KEYCODE_[A-Z0-9_]+")
    return key


def input_text_arg(text: str) -> str:
    """Shell-quote text for ``adb shell input text`` so it is typed literally.

    Spaces become ``%s`` (input's own space escape) inside the quotes. Callers
    should pass already-ASCII text via :func:`ascii_typeable`. (original adb.py:71-82)
    """
    return shlex.quote(text.replace(" ", "%s"))


def ascii_typeable(text: str) -> str:
    """Fold text to the closest ASCII that ``input text`` can send.

    ``adb shell input text`` is ASCII-only — one non-ASCII character fails the
    whole command and leaves the field empty. NFKD-decompose and drop combining
    marks ("café" -> "cafe"). For CJK this yields an empty string, which is the
    caller's signal to try the pinyin fallback instead. (original adb.py:20-33)
    """
    if text.isascii():
        return text
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return folded


# ── XML tree parsing (pure, operates on ``uiautomator dump`` output) ─────────────

_NODE_RE = re.compile(r"<node\b[^>]*?/?>", re.S)
_BOUNDS_RE = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')


def _attr(node: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', node)
    return html.unescape(m.group(1)).strip() if m else ""


def nodes(xml: str) -> list[str]:
    """Split a uiautomator dump into individual ``<node ...>`` strings."""
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
    """``'[x1,y1][x2,y2]'`` -> cell centre. (original adb.py:467-470)"""
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
    """Filter nodes; text/desc match case-insensitively and as substrings."""
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
    """First match trying ``text`` then ``desc`` then ``rid``, in that priority.

    Skill hints resolve through here. Returns ``None`` when nothing matches so the
    caller can raise ``SkillMiss`` and degrade to the generic channel rather than
    failing the whole task.
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
            found = find_nodes(xml, **kwargs)  # retry ignoring clickable: some widgets are not marked
        if found:
            return found[0]
    return None


# ── device ───────────────────────────────────────────────────────────────────────


class Device:
    """ADB + XML primitives for exactly one connected head unit."""

    def __init__(self, serial: str, *, default_delay: float = 0.4, display: int | None = None):
        self.serial = serial
        self.default_delay = default_delay
        # Android 9+ supports `screencap --display <id>`. On a head unit the default
        # capture can otherwise come from the instrument cluster rather than the
        # centre console, which the rules explicitly put out of scope (rule 4.2).
        self.display = display

    # ── invocation ───────────────────────────────────────────────────────────

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
        """Run adb, returning decoded stripped stdout. Raises ``ADBError`` on nonzero exit."""
        r = self._run(args, timeout)
        if r.returncode != 0:
            raise ADBError(args, r.returncode, _dec(r.stderr), _dec(r.stdout))
        return _dec(r.stdout).strip()

    def adb_bytes(self, *args, timeout: float = 30) -> bytes:
        """Run adb returning raw stdout — for ``exec-out`` binary streams."""
        r = self._run(args, timeout)
        if r.returncode != 0:
            raise ADBError(args, r.returncode, _dec(r.stderr), "")
        return r.stdout

    def adb_soft(self, *args, timeout: float = 30) -> ADBResult:
        """Run adb without raising on nonzero exit.

        For probes where failure is a normal answer (is this IME installed?).
        A missing adb binary or a timeout still raises — never a normal outcome.
        """
        r = self._run(args, timeout)
        return ADBResult(r.returncode, _dec(r.stdout).strip(), _dec(r.stderr).strip())

    # ── capture ──────────────────────────────────────────────────────────────

    def screenshot(self, timeout: float = 15) -> bytes:
        """Grab the framebuffer as PNG bytes in one round trip.

        Two round trips (``screencap -p /sdcard/x.png`` then ``pull``, as in
        ``LightGUIAgent/lightguiagent/agent.py:117-135``) cost an extra file transfer
        on every step, which is on the critical path of the performance score.

        Always via ``exec-out``, never ``shell``: the latter allocates a PTY that
        rewrites ``\\n`` to ``\\r\\n`` and corrupts the PNG stream.
        """
        args = ["exec-out", "screencap", "-p"]
        if self.display is not None:
            # Android 9+ only; older builds ignore the flag or error out, and the
            # caller sees a plain ADBError naming the command.
            args += ["--display", str(int(self.display))]
        raw = self.adb_bytes(*args, timeout=timeout)
        return strip_to_png_signature(raw)

    def list_displays(self, timeout: float = 8) -> list[str]:
        """Display ids as reported by SurfaceFlinger. For choosing ``adb.display``."""
        out = self.adb_soft("shell", "dumpsys", "SurfaceFlinger", "--display-id", timeout=timeout).stdout
        return [m for m in re.findall(r"Display ID:\s*(\d+)", out)]

    def dump_xml(self, remote: str = "/sdcard/autogui_ui.xml", timeout: float = 15) -> str:
        """UI hierarchy as uiautomator XML.

        ``uiautomator dump`` refuses to overwrite by default, so remove first.
        Reading the file back through one ``exec-out cat`` avoids a second adb
        transfer. (original adb.py:456-463, minus the portal fast path — see A-1)
        """
        self.adb_soft("shell", "rm", "-f", remote, timeout=timeout)
        r = self.adb_soft("shell", "uiautomator", "dump", remote, timeout=timeout)
        if not r.ok and "dump failed" in (r.stdout + r.stderr).lower():
            return ""
        try:
            return self.adb("exec-out", "cat", remote, timeout=timeout)
        except ADBError:
            return ""

    # ── input ────────────────────────────────────────────────────────────────

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
        """A zero-distance swipe held for ``ms`` — the only long-press ``input`` offers."""
        self.swipe(x, y, x, y, ms=ms, delay=delay)

    def keyevent(self, key: str, delay: float | None = None) -> None:
        self.adb("shell", "input", "keyevent", normalize_keycode(key))
        time.sleep(delay if delay is not None else self.default_delay)

    def back(self, delay: float | None = None) -> None:
        self.keyevent("KEYCODE_BACK", delay)

    def home(self, delay: float | None = None) -> None:
        self.keyevent("KEYCODE_HOME", delay)

    def type_ascii(self, text: str, delay: float | None = None) -> None:
        """Type via ``input text``. ASCII only; see :func:`ascii_typeable`."""
        self.adb("shell", "input", "text", input_text_arg(ascii_typeable(text)))
        time.sleep(delay if delay is not None else self.default_delay)

    # ── apps ─────────────────────────────────────────────────────────────────

    def launch_app(self, package: str, activity: str | None = None, delay: float = 2.0) -> str:
        """Start an app — the programmatic equivalent of tapping its icon.

        Prefers the LAUNCHER intent (what a real tap does) because an explicit
        ``-n`` component sometimes starts in the background on some vendors.
        """
        if not re.fullmatch(r"[A-Za-z0-9._]+(/[A-Za-z0-9._]+)?", package):
            raise ValueError(f"invalid package name: {package!r}")
        if activity:
            r = self.adb_soft("shell", "am", "start", "-n", activity, timeout=10)
            if r.ok and "Error" not in r.stdout:
                time.sleep(delay)
                return "am"
        self.adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1", timeout=10)
        time.sleep(delay)
        return "monkey"

    def stop_app(self, package: str, delay: float = 0.6) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._]+", package):
            raise ValueError(f"invalid package name: {package!r}")
        self.adb("shell", "am", "force-stop", package)
        time.sleep(delay)

    def current_app(self, timeout: float = 8) -> dict:
        """Best-effort foreground activity. Returns ``{package, activity}`` (possibly empty).

        Parsing differs across Android versions, hence the two patterns and the
        permissive ``default``/``mResumedActivity`` keys. Used for the decision log
        ("当前 Activity 已切换至 …") more than for control flow.
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

    # ── device info ──────────────────────────────────────────────────────────

    def get_prop(self, key: str, timeout: float = 5) -> str:
        if not re.fullmatch(r"[a-z0-9._]+", key):
            raise ValueError(f"invalid prop name: {key!r}")
        return self.adb("shell", "getprop", key, timeout=timeout)

    def screen_size(self, timeout: float = 15) -> tuple[int, int]:
        """(width, height) of the captured framebuffer.

        Decodes the screenshot rather than trusting ``wm size``: on a multi-display
        head unit the two can disagree, and the screenshot is what the VLM sees.
        Falls back to ``wm size`` (with ``-s``, unlike the original at settings.py:137)
        only if the PNG header is unparseable.
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
        """Poll the UI tree until ``text`` appears; return that XML. Raises ``TimeoutError``.

        Accepts a pre-fetched ``xml`` so a caller that just dumped the tree does not
        pay for it twice.
        """
        deadline = time.time() + timeout
        while True:
            current = xml if xml is not None else self.dump_xml()
            if text and text in current:
                return current
            if time.time() >= deadline:
                raise TimeoutError(f"timed out after {timeout}s waiting for {text!r}")
            time.sleep(interval)
            xml = None


def _dec(b) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return b or ""


def list_connected() -> list[str]:
    """Serials of devices in ``device`` state."""
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
    """Resolve one device; auto-selects when exactly one is attached."""
    if not serial or serial == "auto":
        connected = list_connected()
        if len(connected) == 1:
            return Device(connected[0], display=display)
        if not connected:
            raise RuntimeError("no ADB device connected (check `adb devices` and USB debugging)")
        raise RuntimeError(f"multiple devices connected — specify serial. Found: {connected}")
    return Device(serial, display=display)
