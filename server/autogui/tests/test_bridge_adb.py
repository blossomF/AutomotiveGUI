"""桥接原语——在触碰任何设备之前必须正确的部分。

其中多项是 技术方案评估.md §2 点名的安全/健壮性修复
（A-2、A-4、C-8、C-11），所以在这里固定为测试，而不是留给真机验证。
"""

import shlex
import struct

import pytest

from autogui.bridge.adb import (
    ADBError,
    ascii_typeable,
    bounds_center,
    find_node,
    find_nodes,
    input_text_arg,
    node_bounds,
    node_center,
    node_text,
    nodes,
    normalize_keycode,
    png_size,
    strip_to_png_signature,
)
from tests.conftest import SAMPLE_XML


def test_adb_error_keeps_stdout_when_stderr_only_lists_arguments():
    error = ADBError(("shell", "monkey", "-p", "com.example.app"), 252,
                     stderr="args: [-p, com.example.app]",
                     stdout="No activities found to run")
    assert "args: [-p, com.example.app]" in str(error)
    assert "No activities found to run" in str(error)


def _png(w: int, h: int) -> bytes:
    """结构合法的 PNG 头；对 png_size() 足够。"""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x00" * 4


# ── 抓取流卫生（C-8） ───────────────────────────────────────────────────────────


def test_multi_display_warning_line_is_trimmed():
    raw = b"[Warning] Multiple displays were found, use --display to specify.\n" + _png(1920, 1080)
    out = strip_to_png_signature(raw)
    assert out.startswith(b"\x89PNG")
    assert png_size(out) == (1920, 1080)


def test_clean_png_is_untouched():
    raw = _png(800, 480)
    assert strip_to_png_signature(raw) == raw


def test_png_size_rejects_non_png():
    assert png_size(b"not an image at all") is None
    assert png_size(b"") is None
    assert png_size(b"\x89PNG\r\n\x1a\n" + b"xx") is None


# ── shell 注入防护（A-4） ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["HOME; reboot", "KEYCODE_HOME && rm -rf /", "$(reboot)", "HOME|tee", "HOME\nreboot"],
)
def test_keycode_whitelist_rejects_shell_metacharacters(bad):
    with pytest.raises(ValueError):
        normalize_keycode(bad)


def test_keycode_accepts_bare_name_and_adds_prefix():
    assert normalize_keycode("home") == "KEYCODE_HOME"
    assert normalize_keycode("KEYCODE_BACK") == "KEYCODE_BACK"
    assert normalize_keycode("move_end") == "KEYCODE_MOVE_END"


@pytest.mark.parametrize(
    "payload",
    ["hello); reboot; #", "$(whoami)", "a; rm -rf /sdcard", "x `id`"],
)
def test_typed_text_cannot_escape_into_device_shell(payload):
    """我们输入的文字是模型生成的，必须能扛住 ``adb shell`` 的重新解析。

    关键性质：设备 shell 拆完命令行后，payload 仍作为**一个**
    argv token 到达。一旦拆开，第二个 token 就会变成车机上执行的命令。
    """
    folded = ascii_typeable(payload)
    quoted = input_text_arg(folded)
    argv = shlex.split(quoted)
    assert len(argv) == 1, f"payload split into {argv!r} — would execute on device"
    # 空格是 input 自己的 %s 转义，必须留在单个 token 内部。
    assert " " not in argv[0] or folded == ""


def test_ascii_folding_keeps_latin_accents_but_drops_cjk():
    assert ascii_typeable("café") == "cafe"
    assert ascii_typeable("七里香") == ""      # 调用方必须降级到其它策略
    assert ascii_typeable("Qilixiang 123") == "Qilixiang 123"


# ── 树解析 ─────────────────────────────────────────────────────────────────────


def test_node_accessors():
    ns = nodes(SAMPLE_XML)
    assert len(ns) > 8
    labelled = [n for n in ns if node_text(n)]
    assert any(node_text(n) == "七里香 - 周杰伦" for n in labelled)
    assert node_bounds(labelled[0]) is not None


def test_find_node_priority_and_miss():
    hit = find_node(SAMPLE_XML, text="同意")
    assert hit is not None
    assert node_center(hit) == bounds_center("[1500,1000][1700,1060]")
    assert find_node(SAMPLE_XML, text="不存在的按钮") is None


def test_find_nodes_matches_substring_case_insensitively():
    assert find_nodes(SAMPLE_XML, text="晴")
    assert not find_nodes(SAMPLE_XML, text="zzz")


def test_adb_error_carries_command_context():
    err = ADBError(("shell", "input", "tap", "1", "2"), 1, stderr="device offline")
    assert "adb shell input tap" in str(err)
    assert "device offline" in str(err)
    assert err.returncode == 1
