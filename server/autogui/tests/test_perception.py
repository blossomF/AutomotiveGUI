"""感知层：树压缩、屏幕尺寸和屏幕指纹。"""

import io

from PIL import Image

from autogui.perception import tree as tree_mod
from autogui.perception.capture import compute_signature, observe
from autogui.perception.image import encode_jpeg
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


def _ui_node(rid: str, bounds: str, *, text: str = "", desc: str = "",
             cls: str = "android.view.View", clickable: bool = False,
             scrollable: bool = False, extra: str = "") -> str:
    return (f'<node text="{text}" content-desc="{desc}" resource-id="app:id/{rid}" '
            f'class="{cls}" clickable="{str(clickable).lower()}" '
            f'scrollable="{str(scrollable).lower()}" bounds="{bounds}" {extra} />')


def test_large_scroll_container_and_same_bounds_pager_keep_scroll_semantics():
    xml = "<hierarchy>" + "".join((
        _ui_node("root", "[0,0][1080,2400]", cls="android.widget.FrameLayout"),
        _ui_node("scroll_view", "[0,609][1080,2270]", cls="android.widget.ScrollView", scrollable=True),
        _ui_node("month_container", "[0,100][1080,1200]"),
        _ui_node("view_pager", "[0,100][1080,1200]", cls="androidx.viewpager.widget.ViewPager", scrollable=True),
    )) + "</hierarchy>"
    cands = tree_mod.extract(xml, screen=(1080, 2400))
    assert len(cands) == 2
    assert {c.rid for c in cands} == {"scroll_view", "view_pager"}
    assert all(c.scrollable for c in cands)
    rendered = tree_mod.render_candidates(cands)
    assert "纵滚" in rendered and "横滚" in rendered
    assert "范围:(0, 609, 1080, 2270)" in rendered


def test_dedup_and_priority_happen_before_limit():
    duplicates = [_ui_node(f"wrapper{i}", "[0,100][100,200]") for i in range(45)]
    passive = [_ui_node(f"label{i}", f"[100,{200+i*30}][200,{225+i*30}]", text=f"标签{i}")
               for i in range(45)]
    target = _ui_node("timezone_button_row", "[100,2000][500,2100]", text="时区", clickable=True)
    xml = "<hierarchy>" + "".join(duplicates + passive + [target]) + "</hierarchy>"
    stats = {}
    cands = tree_mod.extract(xml, screen=(1080, 2400), limit=40, stats=stats)
    assert len(cands) == 40
    assert all(stats[key] == value for key, value in {
        "eligible": 91, "unique": 47, "selected": 40, "omitted_actionable": 0,
        "omitted_passive": 7, "target_lost": 0,
    }.items())
    assert len([c for c in cands if c.bounds == (0, 100, 100, 200)]) == 1
    assert next(c for c in cands if c.rid == "timezone_button_row").clickable


def test_task_target_survives_candidate_and_token_budget():
    noise = [_ui_node(f"button{i}", f"[0,{i*30}][90,{i*30+25}]", clickable=True)
             for i in range(65)]
    title = _ui_node("song_title", "[200,1600][540,1640]", text="Simon")
    artist = _ui_node("song_artist", "[200,1650][540,1680]", text="丁世光")
    queue = _ui_node("queue_button", "[600,1600][700,1640]", text="添加到播放队列", clickable=True)
    xml = "<hierarchy>" + "".join(noise + [title, artist, queue]) + "</hierarchy>"
    stats = {}
    cands = tree_mod.extract(xml, screen=(1080, 2400), limit=40, stats=stats,
                             focus_terms=["Simon"], token_budget=900)
    labels = {c.label for c in cands}
    assert {"Simon", "丁世光", "添加到播放队列"} <= labels
    assert stats["target_lost"] == 0
    assert stats["candidate_tokens"] <= 900


def test_render_preserves_action_and_state_attributes():
    xml = "<hierarchy>" + "".join((
        _ui_node("remember", "[10,10][200,80]", text="记住", desc="保存登录状态",
                 clickable=True, extra='checkable="true" checked="true" selected="true"'),
        _ui_node("query", "[10,100][400,180]", text="搜索", clickable=True,
                 cls="android.widget.EditText", extra='focused="true"'),
        _ui_node("later", "[10,200][200,280]", text="稍后", clickable=True,
                 extra='enabled="false" long-clickable="true"'),
    )) + "</hierarchy>"
    cands = tree_mod.extract(xml, screen=(1080, 2400))
    rendered = tree_mod.render_candidates(cands)
    for marker in ("可点", "已勾", "选中", "描述:保存登录状态", "输入", "焦点", "禁用", "长按"):
        assert marker in rendered
    assert next(c for c in cands if c.rid == "remember").as_dict()["checked"] is True


def test_same_bounds_wrapper_merges_child_state_without_losing_click():
    bounds = "[10,10][200,80]"
    xml = "<hierarchy>" + "".join((
        _ui_node("row", bounds, clickable=True),
        _ui_node("check", bounds, text="提醒", extra='checkable="true" checked="true"'),
    )) + "</hierarchy>"
    cands = tree_mod.extract(xml, screen=(1080, 2400))
    assert len(cands) == 1
    assert cands[0].label == "提醒"
    assert cands[0].clickable and cands[0].checkable and cands[0].checked


def test_partly_visible_scroll_target_uses_visible_bounds():
    xml = "<hierarchy>" + _ui_node(
        "items", "[-20,100][1100,500]", cls="android.widget.ScrollView", scrollable=True,
    ) + "</hierarchy>"
    cands = tree_mod.extract(xml, screen=(1080, 2400))
    assert cands[0].bounds == (0, 100, 1080, 500)
    assert tree_mod.scroll_targets(xml, screen=(1080, 2400))["vertical"][0][0] == cands[0].bounds


def test_number_picker_uses_numeric_description_when_text_is_a_title():
    xml = "<hierarchy>" + _ui_node(
        "hour", "[100,100][300,500]", text="小时", desc="19时",
        cls="android.widget.NumberPicker", scrollable=True,
    ) + "</hierarchy>"
    cands = tree_mod.extract(xml, screen=(1080, 2400))
    assert tree_mod.is_value_wheel(cands[0])
    assert "描述:19时" in tree_mod.render_candidates(cands)


def test_observe_screen_geometry_follows_current_frame():
    buf = io.BytesIO()
    Image.new("RGB", (1080, 2400), "navy").save(buf, format="PNG")

    class Device:
        def screenshot(self):
            return buf.getvalue()

        def dump_xml(self):
            return EMPTY_TREE_XML

        def screen_size(self):
            return 1920, 1080  # 过期缓存不能影响实际截图尺寸

    observation, screen = observe(Device())
    assert (observation.width, observation.height) == (1080, 2400)
    assert (screen.width, screen.height) == (1080, 2400)


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


def test_signature_catches_checked_change_without_text_change():
    before = "<hierarchy>" + _ui_node("setting", "[10,10][100,90]", text="提醒",
                                      extra='checkable="true" checked="false"') + "</hierarchy>"
    after = before.replace('checked="false"', 'checked="true"')
    assert compute_signature(before, _solid_png("black")) != compute_signature(after, _solid_png("black"))


def test_screen_size_prefers_the_frame_over_wm_size():
    from autogui.bridge.adb import png_size as decode

    assert decode(_solid_png("red")) == (1920, 1080)
