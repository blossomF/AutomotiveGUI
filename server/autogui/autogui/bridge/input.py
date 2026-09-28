"""带 IME 安全降级链的 Unicode 文本输入。

移植自 ``android-agent/gitd/bots/common/adb.py:262-283``（``type_unicode``）
和 ``LightGUIAgent/lightguiagent/agent.py:295-333``（``_setup_yadb``），
并包含 技术方案评估.md §2 A-2 所述的 P0 修复。

原文先启用 ADBKeyboard、广播文本，然后把 IME 还原到*写死的 Gboard*
组件。CEA1.3 车机上没有 Gboard，于是那行代码把 ``default_input_method``
指向一个不存在的 IME：之后每次击键都静默无效，agent 只会看到不变的
屏幕并开始打转。这里在切换前先读当前 IME，并精确还原那个值。

降级顺序（启动时探测一次，然后缓存）：
    yadb  ->  ADBKeyboard  ->  input text (ASCII)  ->  拼音味 ASCII
"""

from __future__ import annotations

import re
import time

from autogui.bridge.adb import ADBError, Device, ascii_typeable, input_text_arg

ADBKEYBOARD_ID = "com.android.adbkeyboard/.AdbIME"
YADB_CLASSPATH = "/data/local/tmp/yadb"
YADB_MAIN = "com.ysbing.yadb.Main"

# 探测顺序；第一个可用的成为激活策略。
STRATEGIES = ("yadb", "adbkeyboard", "ascii")


class InputResult:
    def __init__(self, strategy: str, typed: str, note: str = ""):
        self.strategy = strategy
        self.typed = typed        # 实际发出去的内容（可能与请求不同）
        self.note = note
        self.fallback_needed = strategy == "ascii" and typed != "" and not typed.isascii()
        self.pinyin_fallback = False  # 拼音首字母回退标志：调用方应跳过 ENTER，等 agent 点联想词

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"InputResult(strategy={self.strategy!r}, typed={self.typed!r}, note={self.note!r})"


class TextInput:
    """单台车机的中文/表情文本输入。"""

    def __init__(self, device: Device, order: tuple[str, ...] = STRATEGIES, verbose: bool = False):
        self.device = device
        self.verbose = verbose
        self.order = order
        self.strategy: str | None = None
        self._probed = False
        self._sdk_level: int | None = None

    # ── 探测 ─────────────────────────────────────────────────────────────────

    def probe(self) -> str:
        """探测一次当前最优可用策略，并记住。"""
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

    def _sdk(self) -> int:
        """缓存设备 SDK 等级；拿不到按 0 处理（当作老设备）。"""
        if self._sdk_level is None:
            try:
                self._sdk_level = int((self.device.get_prop("ro.build.version.sdk") or "").strip() or 0)
            except (ADBError, TypeError, ValueError):
                self._sdk_level = 0
        return self._sdk_level

    def _has_yadb(self) -> bool:
        """yadb 是推送到 /data/local/tmp 的一个 jar；无需安装 APK。

        ⚠️ Android 10（SDK 29）及以上**必然失效**：写剪贴板被系统策略拦截，
        但 ``-keyboard`` 仍打印**假的** ``Copy text: true``，输入框实际不变。
        实测这台 SDK 34 车机：命令返回成功、``typed`` 字段也填了，屏幕上却
        只有一句「已将剪贴板中的内容粘贴到Shell」，搜索框仍是占位符。

        所以不能只查文件是否还在——那会让整条输入链停在一个永远"成功"的
        假策略上，真正能用的策略永远轮不到（原本的 adbkeyboard 就在它后面）。
        """
        if self._sdk() > 28:
            return False
        r = self.device.adb_soft("shell", "ls", YADB_CLASSPATH, timeout=5)
        return r.ok

    def _has_adbkeyboard(self) -> bool:
        """ADBKeyboard *本身*是 APK——不能存在时如实上报。

        用 ``ime list -a`` 而非 ``-s``：``-s`` 只列**已启用**的 IME，而
        ``_via_adbkeyboard`` 用完会 ``ime disable`` 自己（见那里），于是
        "用过一次之后再也检测不到" —— probe 会永久退化到 ascii，中文再也
        输不进去。``-a`` 列出已安装的全部 IME，与 *_via_adbkeyboard 里
        自己会 ``ime enable``* 的前提自洽。
        """
        out = self.device.adb_soft("shell", "ime", "list", "-a", timeout=5).stdout
        return "com.android.adbkeyboard" in out

    # ── IME 安全部分 ─────────────────────────────────────────────────────────

    def _original_ime(self) -> str:
        return self.device.adb_soft(
            "shell", "settings", "get", "secure", "default_input_method", timeout=5
        ).stdout.strip()

    def type(self, text: str, clear_first: bool = False, delay: float = 0.3) -> InputResult:
        """把 ``text`` 输入到当前聚焦的输入框。"""
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
            # 硬性的 adb 失败（设备掉线）——直接暴露，不要伪装成
            # “输入降级”。
            raise
        except Exception as e:  # 某个策略中途炸掉则降级到 ascii
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
        """切进来、广播、切回去——还原的是*设备自己的* IME。"""
        before = self._original_ime()
        # 未显式设置 IME 时 settings get 返回 "null"。
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
            # 即使广播抛异常也要执行——把 ADBKeyboard 留在*选中*状态
            # 会改变 VLM 下一步看到的屏幕键盘。`set` 回原 IME 就够了。
            #
            # 刻意**不再** `ime disable`：那会让 AdbIME 从 `ime list -s` 里
            # 消失，而能力探测正是查这个列表 —— 结果是"用过一次就永久失效"，
            # 后续 probe 退到 ascii、中文再也输不进去（实测踩到）。保持
            # enabled、只是不选中，对设备行为没有任何影响。
            if restore_to:
                self.device.adb_soft("shell", "ime", "set", restore_to, timeout=5)
        return InputResult("adbkeyboard", text, f"ime_restored={restore_to or 'system_default'}")

    def _via_ascii(self, text: str, delay: float) -> InputResult:
        folded = ascii_typeable(text)
        if not folded.strip():
            # 纯中文（无 ASCII 形式）：输入拼音首字母简写，靠 App 联想候选，
            # 由 agent 下一轮点选；不发 ENTER（提交会搜拼音原词，且挡住联想条）。
            initials = to_pinyin_initials(text)
            if initials:
                self.device.adb("shell", "input", "text", input_text_arg(initials), timeout=10)
                time.sleep(delay)
                r = InputResult("ascii", initials,
                                f"pinyin-initials fallback from {text!r} to {initials!r}; "
                                f"ENTER skipped — click the matching app suggestion")
                r.pinyin_fallback = True
                return r
            return InputResult("ascii", "", f"cannot type {text!r}: no ASCII form. Tap a suggestion strip instead.")
        self.device.adb("shell", "input", "text", input_text_arg(folded), timeout=10)
        time.sleep(delay)
        return InputResult("ascii", folded, f"degraded from {text!r} to {folded!r}")

    def clear_field(self, max_chars: int = 60) -> None:
        """全选后删除。

        ``keycombination`` 需要比部分车机更新版本的 Android，所以回退
        到 move-end + 连续 DEL（同 ``android-agent`` adb.py:784-786 的做法）。
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
    """CJK 的尽力 ASCII 替身，避免搜索框被留空。

    刻意做得简单——我们不带拼音表。重点是给 agent 一个可以跟随的
    *提示*：先输入 ASCII 部分，再点 IME 候选条。字符串完全没有
    ASCII 时返回 ""。
    """
    ascii_part = re.sub(r"[^A-Za-z0-9]", "", text)
    return ascii_part


def to_pinyin_initials(text: str) -> str:
    """中文转拼音首字母简写（如“兰州拉面” -> "lzl m" -> "lzlm"）。
    
    仅当 pypinyin 可用且文本包含 CJK 字符时返回结果，否则返回空字符串。
    用于 ascii 降级时的搜索框兜底输入。
    """
    cjk_chars = [ch for ch in text if '\u4e00' <= ch <= '\u9fff']
    if not cjk_chars:
        return ""
    try:
        from pypinyin import lazy_pinyin, Style
    except ImportError:
        return ""
    initials = []
    for ch in cjk_chars:
        py = lazy_pinyin(ch, style=Style.FIRST_LETTER)
        if py:
            initials.append(py[0])
    return "".join(initials).lower()
