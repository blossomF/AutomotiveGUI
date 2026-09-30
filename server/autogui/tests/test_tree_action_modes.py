"""Mode-specific action guidance and clickable list-row extraction."""

import pytest

from autogui.actions import image_point_to_device, parse_action
from autogui.brain.prompts import build_decide_messages
from autogui.perception.tree import candidates_from_xml, render_candidates


def test_static_title_in_clickable_row_uses_title_as_safe_tap_target():
    xml = """<hierarchy>
      <node text="" clickable="true" bounds="[0,0][1080,2400]">
        <node text="" clickable="true" bounds="[0,1400][1080,1700]">
          <node text="夜夜夜夜" clickable="false" bounds="[44,1486][200,1538]" />
          <node text="齐秦·丝路" clickable="false" bounds="[258,1552][465,1613]" />
          <node text="添加到播放队列" clickable="true" bounds="[900,1515][983,1598]" />
        </node>
      </node>
    </hierarchy>"""
    cands = candidates_from_xml(xml, screen=(1080, 2400), focus_terms=["夜夜夜夜"])
    title = next(c for c in cands if c.label == "夜夜夜夜")
    queue = next(c for c in cands if c.label == "添加到播放队列")
    assert title.clickable and title.clickable_via_parent
    assert (title.cx, title.cy) == (122, 1512)
    assert "可点·父行" in render_candidates(cands)
    assert not queue.clickable_via_parent


def test_full_screen_clickable_wrapper_does_not_promote_static_text():
    xml = ('<hierarchy><node clickable="true" bounds="[0,0][1080,2400]">'
           '<node text="广告说明" clickable="false" bounds="[40,300][500,380]" />'
           '</node></hierarchy>')
    label = candidates_from_xml(xml, screen=(1080, 2400))[0]
    assert not label.clickable and not label.clickable_via_parent


def test_tree_and_vlm_expose_distinct_click_and_drag_coordinate_rules():
    tree = build_decide_messages("任务：播放歌曲", None, screen=(1080, 2400))
    vlm = build_decide_messages("任务：播放歌曲", "abc", screen=(1080, 2400),
                                image_size=(705, 1568))
    assert "CLICK(node_ref | coord" in tree[0]["content"]
    assert "CLICK(node_ref | point" not in tree[0]["content"]
    assert "DRAG 的 from/to 都是设备屏幕像素" in tree[0]["content"]
    assert "CLICK(point | node_ref" in vlm[0]["content"]
    assert "DRAG 的 from/to 都是当前发送截图的像素" in vlm[0]["content"]
    assert "705×1568" in vlm[1]["content"][1]["text"]


def test_vlm_drag_points_scale_from_image_to_device():
    action = parse_action({"type": "DRAG", "from": [100, 200], "to": [600, 1400]})
    assert image_point_to_device(action["from"], (705, 1568), (1080, 2400)) == (153, 306)
    assert image_point_to_device(action["to"], (705, 1568), (1080, 2400)) == (919, 2143)
    with pytest.raises(ValueError, match="outside"):
        image_point_to_device((705, 100), (705, 1568), (1080, 2400))
