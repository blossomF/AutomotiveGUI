"""Bridge primitives — the parts that must be right before any device is touched.

Several of these are the security/robustness fixes called out in 技术方案评估.md §2
(A-2, A-4, C-8, C-11), so they are pinned here rather than left to device testing.
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


def _png(w: int, h: int) -> bytes:
    """A structurally valid PNG header; enough for png_size()."""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x00" * 4


# ── capture stream hygiene (C-8) ─────────────────────────────────────────────────


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


# ── shell-injection guards (A-4) ─────────────────────────────────────────────────


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
    """Text we type is model-generated, so it must survive ``adb shell`` re-parsing.

    The property that matters: after the device shell splits the command line, the
    payload still arrives as **one** argv token. If it split, the second token would
    be an executed command on the head unit.
    """
    folded = ascii_typeable(payload)
    quoted = input_text_arg(folded)
    argv = shlex.split(quoted)
    assert len(argv) == 1, f"payload split into {argv!r} — would execute on device"
    # Spaces are input's own %s escape, and stay inside the single token.
    assert " " not in argv[0] or folded == ""


def test_ascii_folding_keeps_latin_accents_but_drops_cjk():
    assert ascii_typeable("café") == "cafe"
    assert ascii_typeable("七里香") == ""      # caller must degrade to another strategy
    assert ascii_typeable("Qilixiang 123") == "Qilixiang 123"


# ── tree parsing ─────────────────────────────────────────────────────────────────


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
