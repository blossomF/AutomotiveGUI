"""Unicode text input with an IME-safe degradation chain.

Ported from ``android-agent/gitd/bots/common/adb.py:262-283`` (``type_unicode``) and
``LightGUIAgent/lightguiagent/agent.py:295-333`` (``_setup_yadb``), with the P0 fix
described in 技术方案评估.md §2 A-2.

The original enables ADBKeyboard, broadcasts the text, then restores the IME to a
*hardcoded Gboard* component. A CEA1.3 head unit has no Gboard, so that line points
``default_input_method`` at a non-existent IME: every subsequent keystroke silently
does nothing, and the agent just sees an unchanged screen and starts looping. Here we
read the current IME before switching and restore that exact value.

Degradation order (probed once at startup, then cached):
    yadb  ->  ADBKeyboard  ->  input text (ASCII)  ->  pinyin-ish ASCII
"""

from __future__ import annotations

import re
import time

from autogui.bridge.adb import ADBError, Device, ascii_typeable, input_text_arg

ADBKEYBOARD_ID = "com.android.adbkeyboard/.AdbIME"
YADB_CLASSPATH = "/data/local/tmp/yadb"
YADB_MAIN = "com.ysbing.yadb.Main"

# Order to probe; first that works becomes the active strategy.
STRATEGIES = ("yadb", "adbkeyboard", "ascii")


class InputResult:
    def __init__(self, strategy: str, typed: str, note: str = ""):
        self.strategy = strategy
        self.typed = typed        # what was actually sent (may differ from requested)
        self.note = note
        self.fallback_needed = strategy == "ascii" and typed != "" and not typed.isascii()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"InputResult(strategy={self.strategy!r}, typed={self.typed!r}, note={self.note!r})"


class TextInput:
    """Chinese/emoji-capable text entry for one head unit."""

    def __init__(self, device: Device, order: tuple[str, ...] = STRATEGIES, verbose: bool = False):
        self.device = device
        self.verbose = verbose
        self.order = order
        self.strategy: str | None = None
        self._probed = False

    # ── probing ──────────────────────────────────────────────────────────────

    def probe(self) -> str:
        """Detect the best available strategy once and remember it."""
        if self._probed:
            return self.strategy or "ascii"
        for name in self.order:
            try:
                if name == "yadb" and self._has_yadb():
                    self.strategy = "yadb"
                    break
                if name == "adbkeyboard" and self._has_adbkeyboard():
                    self.strategy = "adbkeyboard"
                    break
            except ADBError as e:
                if self.verbose:
                    print(f"  [input] probe {name} failed: {e}")
                continue
        else:
            self.strategy = "ascii"
        self._probed = True
        return self.strategy

    def _has_yadb(self) -> bool:
        """yadb is a single jar pushed to /data/local/tmp; no APK install needed."""
        r = self.device.adb_soft("shell", "ls", YADB_CLASSPATH, timeout=5)
        return r.ok

    def _has_adbkeyboard(self) -> bool:
        """ADBKeyboard *is* an APK — report honestly if it cannot be present."""
        out = self.device.adb_soft("shell", "ime", "list", "-s", timeout=5).stdout
        return "com.android.adbkeyboard" in out

    # ── the IME-safe part ────────────────────────────────────────────────────

    def _original_ime(self) -> str:
        return self.device.adb_soft(
            "shell", "settings", "get", "secure", "default_input_method", timeout=5
        ).stdout.strip()

    def type(self, text: str, clear_first: bool = False, delay: float = 0.3) -> InputResult:
        """Type ``text`` into the focused field."""
        if text is None:
            return InputResult("none", "", "nothing to type")
        self.probe()

        if clear_first:
            self.clear_field()

        if not text:
            return InputResult(self.strategy or "ascii", "", "empty text")

        try:
            if self.strategy == "yadb":
                return self._via_yadb(text, delay)
            if self.strategy == "adbkeyboard":
                return self._via_adbkeyboard(text, delay)
        except ADBError as e:
            # Hard adb failure (device dropped) — surface it, do not mask it as
            # "typing degraded".
            raise
        except Exception as e:  # a strategy blowing up mid-flight degrades to ascii
            if self.verbose:
                print(f"  [input] {self.strategy} failed ({e}), degrading to ascii")
        return self._via_ascii(text, delay)

    def _via_yadb(self, text: str, delay: float) -> InputResult:
        processed = text.replace("\n", " ").replace("\t", " ")
        self.device.adb(
            "shell", "app_process",
            "-Djava.class.path=" + YADB_CLASSPATH,
            "/data/local/tmp",
            YADB_MAIN,
            "-keyboard",
            processed,
            timeout=15,
        )
        time.sleep(delay)
        return InputResult("yadb", processed)

    def _via_adbkeyboard(self, text: str, delay: float) -> InputResult:
        """Switch in, broadcast, switch back — restoring the *device's own* IME."""
        before = self._original_ime()
        # "null" is what settings get returns when no IME is set explicitly.
        restore_to = before if before and before != "null" else None
        try:
            self.device.adb_soft("shell", "ime", "enable", ADBKEYBOARD_ID, timeout=5)
            self.device.adb_soft("shell", "ime", "set", ADBKEYBOARD_ID, timeout=5)
            time.sleep(0.3)
            self.device.adb(
                "shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", text, timeout=10
            )
            time.sleep(delay)
        finally:
            # Runs even if the broadcast raised — leaving ADBKeyboard selected would
            # change the on-screen keyboard the VLM sees on the next step.
            if restore_to:
                self.device.adb_soft("shell", "ime", "set", restore_to, timeout=5)
            self.device.adb_soft("shell", "ime", "disable", ADBKEYBOARD_ID, timeout=5)
        return InputResult("adbkeyboard", text, f"ime_restored={restore_to or 'system_default'}")

    def _via_ascii(self, text: str, delay: float) -> InputResult:
        folded = ascii_typeable(text)
        if not folded.strip():
            return InputResult("ascii", "", f"cannot type {text!r}: no ASCII form. Tap a suggestion strip instead.")
        self.device.adb("shell", "input", "text", input_text_arg(folded), timeout=10)
        time.sleep(delay)
        return InputResult("ascii", folded, f"degraded from {text!r} to {folded!r}")

    def clear_field(self, max_chars: int = 60) -> None:
        """Select-all then delete.

        ``keycombination`` needs a newer Android than some head units run, so fall
        back to move-end + repeated DEL (as ``android-agent`` does at adb.py:784-786).
        """
        r = self.device.adb_soft("shell", "input", "keycombination", "113", "29", timeout=5)  # Ctrl+A
        if r.ok:
            self.device.adb_soft("shell", "input", "keyevent", "67", timeout=5)  # DEL
        else:
            self.device.adb_soft("shell", "input", "keyevent", "KEYCODE_MOVE_END", timeout=5)
            for _ in range(min(max_chars, 40)):
                self.device.adb_soft("shell", "input", "keyevent", "KEYCODE_DEL", timeout=5)
        time.sleep(0.2)


def to_pinyin_fallback(text: str) -> str:
    """Best-effort ASCII stand-in for CJK so a search box is not left empty.

    Deliberately dumb — we do not ship a pinyin table. The point is a *hint* the
    agent can follow: type the ASCII part, then tap the IME suggestion strip.
    Returns "" when there is no ASCII at all in the string.
    """
    ascii_part = re.sub(r"[^A-Za-z0-9]", "", text)
    return ascii_part
