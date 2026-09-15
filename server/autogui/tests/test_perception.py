"""Perception: tree compaction, landscape grid, screen fingerprint."""

import io

from PIL import Image

from autogui.perception import tree as tree_mod
from autogui.perception.capture import compute_signature
from autogui.perception.grid import Grid, encode_jpeg
from tests.conftest import EMPTY_TREE_XML, SAMPLE_XML


# ── tree compaction ──────────────────────────────────────────────────────────────


def test_containers_are_dropped_and_targets_kept(landscape):
    cands = tree_mod.extract(SAMPLE_XML, screen=landscape, limit=40)
    labels = [c.label for c in cands]
    assert "搜索音乐" in labels
    assert "同意" in labels
    assert "以后再说" in labels
    # The two full-screen FrameLayouts must not be offered as click targets.
    assert all(c.bounds != (0, 0, 1920, 1080) for c in cands)
    # A list row spanning full width but only 100px tall is still a real target.
    assert "告白气球" in labels


def test_refs_are_contiguous_and_resolvable(landscape):
    cands = tree_mod.extract(SAMPLE_XML, screen=landscape, limit=40)
    assert [c.ref for c in cands] == list(range(1, len(cands) + 1))
    target = next(c for c in cands if c.label == "同意")
    assert tree_mod.by_ref(cands, target.ref) is target
    assert tree_mod.by_ref(cands, 999) is None      # -> caller degrades tree to vlm


def test_render_is_one_short_line_per_candidate(landscape):
    cands = tree_mod.extract(SAMPLE_XML, screen=landscape, limit=40)
    text = tree_mod.render_candidates(cands)
    assert len(text.splitlines()) == len(cands)
    assert text.splitlines()[0].startswith("#1 ")
    assert "[" in text and "]" in text
    # Compaction is the whole point: far smaller than the raw dump it came from.
    assert len(text) < len(SAMPLE_XML) / 3


def test_self_drawn_app_yields_nothing_and_is_reported_unusable(landscape):
    cands = tree_mod.extract(EMPTY_TREE_XML, screen=landscape, limit=40)
    assert cands == []
    assert tree_mod.render_candidates(cands) == ""


def test_structural_resource_ids_are_not_offered_as_targets(landscape):
    """``android:id/statusBarBackground`` carries a value but is never something to tap.

    Case-insensitive tail matching matters here: framework ids vary their casing
    between builds ("statusBarBackground" vs "statusbar_background").
    """
    labels = [c.label for c in tree_mod.extract(SAMPLE_XML, screen=landscape, limit=40)]
    assert "statusBarBackground" not in labels
    assert not any("background" in l.lower() for l in labels)


def test_limit_is_respected(landscape):
    assert len(tree_mod.extract(SAMPLE_XML, screen=landscape, limit=2)) <= 2


def test_empty_or_garbage_xml_does_not_raise():
    assert tree_mod.extract("", limit=10) == []
    assert tree_mod.extract("<<<not xml", limit=10) == []
    assert tree_mod.extract("<hierarchy></hierarchy>", limit=10) == []


# ── landscape grid ───────────────────────────────────────────────────────────────


def test_grid_label_and_pixel_roundtrip():
    g = Grid(width=1920, height=1080, cols=20, rows=10)
    for label in ("A1", "T10", "J5", "E10"):
        x, y = g.to_pixel(label)
        assert 0 <= x < 1920 and 0 <= y < 1080
        assert g.to_label(x, y) == label


def test_landscape_cells_are_squareish():
    """The LightGUIAgent auto-density produced ~128x72px cells here; we want parity."""
    g = Grid(width=1920, height=1080, cols=20, rows=10)
    ratio = g.cell_w / g.cell_h
    assert 0.6 < ratio < 1.7


def test_grid_rejects_out_of_range_labels():
    g = Grid(width=1920, height=1080, cols=20, rows=10)
    for bad in ("U5", "A11", "A0", "5A", "", "#3"):
        assert not g.in_bounds(bad)


def test_annotate_resyncs_to_actual_image_size():
    """A capture smaller than the cached size must not desync labels from pixels."""
    buf = io.BytesIO()
    Image.new("RGB", (1280, 720), "navy").save(buf, format="PNG")
    g = Grid(width=1920, height=1080, cols=20, rows=10)  # stale size on purpose
    img = g.annotate(buf.getvalue())
    assert (g.width, g.height) == (1280, 720)
    assert img.size == (1280, 720)


def test_encode_jpeg_downscales_to_max_side():
    buf = io.BytesIO()
    Image.new("RGB", (3840, 2160), "gray").save(buf, format="PNG")
    out = encode_jpeg(Image.open(io.BytesIO(buf.getvalue())), quality=80, max_side=1568)
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 1568


# ── screen fingerprint ───────────────────────────────────────────────────────────


def _solid_png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1920, 1080), color).save(buf, format="PNG")
    return buf.getvalue()


def test_signature_is_stable_and_changes_with_text():
    png = _solid_png("black")
    assert compute_signature(SAMPLE_XML, png) == compute_signature(SAMPLE_XML, png)
    assert compute_signature(SAMPLE_XML, png) != compute_signature(EMPTY_TREE_XML, png)


def test_signature_catches_purely_visual_change():
    """No text change, only pixels — a video starting to play. Text alone would miss it."""
    same_xml = SAMPLE_XML
    assert compute_signature(same_xml, _solid_png("black")) != compute_signature(same_xml, _solid_png("white"))


def test_screen_size_prefers_the_frame_over_wm_size():
    from autogui.bridge.adb import png_size as decode

    assert decode(_solid_png("red")) == (1920, 1080)
