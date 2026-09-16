"""感知层：树压缩、横屏网格、屏幕指纹。"""

import io

from PIL import Image

from autogui.perception import tree as tree_mod
from autogui.perception.capture import compute_signature
from autogui.perception.grid import Grid, encode_jpeg
from tests.conftest import EMPTY_TREE_XML, SAMPLE_XML


# ── 树压缩 ─────────────────────────────────────────────────────────────────────


def test_containers_are_dropped_and_targets_kept(landscape):
    cands = tree_mod.extract(SAMPLE_XML, screen=landscape, limit=40)
    labels = [c.label for c in cands]
    assert "搜索音乐" in labels
    assert "同意" in labels
    assert "以后再说" in labels
    # 两个全屏 FrameLayout 绝不能被当成点击目标。
    assert all(c.bounds != (0, 0, 1920, 1080) for c in cands)
    # 通栏但只有 100px 高的列表行仍是真实目标。
    assert "告白气球" in labels


def test_refs_are_contiguous_and_resolvable(landscape):
    cands = tree_mod.extract(SAMPLE_XML, screen=landscape, limit=40)
    assert [c.ref for c in cands] == list(range(1, len(cands) + 1))
    target = next(c for c in cands if c.label == "同意")
    assert tree_mod.by_ref(cands, target.ref) is target
    assert tree_mod.by_ref(cands, 999) is None      # -> 调用方把 tree 降级到 vlm


def test_render_is_one_short_line_per_candidate(landscape):
    cands = tree_mod.extract(SAMPLE_XML, screen=landscape, limit=40)
    text = tree_mod.render_candidates(cands)
    assert len(text.splitlines()) == len(cands)
    assert text.splitlines()[0].startswith("#1 ")
    assert "[" in text and "]" in text
    # 压缩本身就是目的：远小于原始 dump。
    assert len(text) < len(SAMPLE_XML) / 3


def test_self_drawn_app_yields_nothing_and_is_reported_unusable(landscape):
    cands = tree_mod.extract(EMPTY_TREE_XML, screen=landscape, limit=40)
    assert cands == []
    assert tree_mod.render_candidates(cands) == ""


def test_structural_resource_ids_are_not_offered_as_targets(landscape):
    """``android:id/statusBarBackground`` 带有值但永远不是可点对象。

    这里大小写不敏感的尾部匹配很关键：框架 id 在不同构建间大小写
    不一致（"statusBarBackground" vs "statusbar_background"）。
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


# ── 横屏网格 ───────────────────────────────────────────────────────────────────


def test_grid_label_and_pixel_roundtrip():
    g = Grid(width=1920, height=1080, cols=20, rows=10)
    for label in ("A1", "T10", "J5", "E10"):
        x, y = g.to_pixel(label)
        assert 0 <= x < 1920 and 0 <= y < 1080
        assert g.to_label(x, y) == label


def test_landscape_cells_are_squareish():
    """LightGUIAgent 的自动密度在这里产生 ~128x72px 的格子；我们要接近正方形。"""
    g = Grid(width=1920, height=1080, cols=20, rows=10)
    ratio = g.cell_w / g.cell_h
    assert 0.6 < ratio < 1.7


def test_grid_rejects_out_of_range_labels():
    g = Grid(width=1920, height=1080, cols=20, rows=10)
    for bad in ("U5", "A11", "A0", "5A", "", "#3"):
        assert not g.in_bounds(bad)


def test_annotate_resyncs_to_actual_image_size():
    """比缓存尺寸小的截图不能让标签与像素错位。"""
    buf = io.BytesIO()
    Image.new("RGB", (1280, 720), "navy").save(buf, format="PNG")
    g = Grid(width=1920, height=1080, cols=20, rows=10)  # 故意用过期尺寸
    img = g.annotate(buf.getvalue())
    assert (g.width, g.height) == (1280, 720)
    assert img.size == (1280, 720)


def test_encode_jpeg_downscales_to_max_side():
    buf = io.BytesIO()
    Image.new("RGB", (3840, 2160), "gray").save(buf, format="PNG")
    out = encode_jpeg(Image.open(io.BytesIO(buf.getvalue())), quality=80, max_side=1568)
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 1568


# ── 屏幕指纹 ───────────────────────────────────────────────────────────────────


def _solid_png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1920, 1080), color).save(buf, format="PNG")
    return buf.getvalue()


def test_signature_is_stable_and_changes_with_text():
    png = _solid_png("black")
    assert compute_signature(SAMPLE_XML, png) == compute_signature(SAMPLE_XML, png)
    assert compute_signature(SAMPLE_XML, png) != compute_signature(EMPTY_TREE_XML, png)


def test_signature_catches_purely_visual_change():
    """文本不变、只有像素变——比如视频开始播放。只用文本会漏掉。"""
    same_xml = SAMPLE_XML
    assert compute_signature(same_xml, _solid_png("black")) != compute_signature(same_xml, _solid_png("white"))


def test_screen_size_prefers_the_frame_over_wm_size():
    from autogui.bridge.adb import png_size as decode

    assert decode(_solid_png("red")) == (1920, 1080)
