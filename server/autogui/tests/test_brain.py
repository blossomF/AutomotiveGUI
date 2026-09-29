"""大脑层：动作解析、JSON 严格性、反思、上下文预算。"""

import pytest

from autogui.actions import ACTION_NAMES, parse_action
from autogui.brain.context import Context, estimate_tokens
from autogui.brain.prompts import (
    DECIDE_SYSTEM, build_decide_messages, build_reflect_messages,
    decide_user_text, first_json_object, parse_reflection, render,
)
from autogui.config import AppConfig, PROJECT_ROOT
from autogui.perception import tree as tree_mod
from autogui.skills.ui_hints import PageHintBook
from tests.conftest import SAMPLE_XML


# ── 动作空间 ─────────────────────────────────────────────────────────────────────


def test_back_and_home_exist():
    """LightGUIAgent 的动作列表两者都没有，恢复只能靠猜网格左上角的返回键。
    这里必须是两个一等动作。"""
    assert "BACK" in ACTION_NAMES and "HOME" in ACTION_NAMES
    assert "FAIL" in ACTION_NAMES      # agent 需要诚实放弃的途径
    assert parse_action({"action": "back"})["type"] == "BACK"


def test_click_accepts_node_ref_or_point():
    assert parse_action({"action": "CLICK", "node_ref": 7})["node_ref"] == 7
    assert parse_action({"action": "CLICK", "node_ref": "#12"})["node_ref"] == 12
    with pytest.raises(ValueError):
        parse_action({"action": "CLICK", "grid": "j5"})
    assert parse_action({"action": "CLICK", "point": [990, 127]})["point"] == (990.0, 127.0)
    with pytest.raises(ValueError):
        parse_action({"action": "CLICK"})


def test_rejects_unknown_action_and_bad_scroll_direction():
    with pytest.raises(ValueError):
        parse_action({"action": "EXPLODE"})
    with pytest.raises(ValueError):
        parse_action({"action": "SCROLL", "direction": "sideways"})
    assert parse_action({"action": "SCROLL", "direction": "DOWN"})["direction"] == "down"


def test_wait_seconds_is_capped():
    """模型要求等 600 秒时绝不能吃掉整个 Demo Day 时间槽。"""
    assert parse_action({"action": "WAIT", "seconds": 600})["seconds"] == 5.0
    assert parse_action({"action": "WAIT"})["seconds"] == 1.5


def test_explain_is_length_bounded():
    action = parse_action({"action": "BACK", "explain": "x" * 5000})
    assert len(action["explain"]) <= 200


# ── JSON 提取 ────────────────────────────────────────────────────────────────────


def test_json_from_plain_object():
    assert first_json_object('{"action":"BACK"}') == {"action": "BACK"}


def test_json_from_code_fence():
    assert first_json_object('```json\n{"action":"BACK"}\n```') == {"action": "BACK"}


def test_json_from_prose_and_trailing_commentary():
    raw = '我需要点击返回键。\n{"action":"BACK","explain":"返回"}\n以上。'
    assert first_json_object(raw)["action"] == "BACK"


def test_json_with_nested_braces():
    raw = '{"action":"TYPE","text":"hi","entities":{"时间":"19:00"}}'
    assert first_json_object(raw)["entities"]["时间"] == "19:00"


@pytest.mark.parametrize("bad", ["", "no json here", "{", '{"unterminated": '])
def test_unparsable_response_raises(bad):
    with pytest.raises(ValueError):
        first_json_object(bad)


# ── 反思解析（技术方案评估.md §2 B-5） ──────────────────────────────────────────


def test_reflection_is_strict_not_substring():
    """Mobile-Agent-v2 用 ``if 'A' in reflect``（run.py:401）。模型自由推理中的
    任何拉丁字母 A——'ADB'、'Activity'、'App'——都会被误读成成功。
    严格裸词匹配消除这类 bug。"""
    assert parse_reflection("A") == "A"
    assert parse_reflection("B") == "B"
    assert parse_reflection("C") == "C"
    assert parse_reflection("A. 页面变化了") == "A"
    assert parse_reflection("回答： C") == "C"
    # v2 的回归用例，固定下来：
    assert parse_reflection("The ADB activity started the App on a new Activity") == "UNKNOWN"


def test_unparsable_reflection_is_unknown_not_silently_A():
    assert parse_reflection("") == "UNKNOWN"
    assert parse_reflection("我无法判断") == "UNKNOWN"


# ── 上下文经济 ───────────────────────────────────────────────────────────────────


def _ctx(**kw) -> Context:
    base = dict(task="在音乐App里播放周杰伦的《七里香》", history_window=5, token_budget=8000)
    base.update(kw)
    return Context(**base)


def test_tree_observation_is_text_only():
    """tree 路由不发图，是 token 节省的大头。"""
    c = _ctx()
    cands = tree_mod.extract(SAMPLE_XML, screen=(1920, 1080), limit=40)
    text, kept, counts = c.trim(cands)
    assert "#1 " in text
    assert "base64" not in text.lower()
    assert "定位路径" not in text
    assert counts["approx_input"] < 8000


def test_history_window_slides():
    c = _ctx(history_window=3)
    for i in range(10):
        c.record(i, "tree", f"CLICK #{i}", f"第{i}步")
    block = c.history_block()
    lines = block.splitlines()
    assert len(lines) == 3
    assert lines[-1].startswith("9.")
    assert not any(line.startswith("6.") for line in lines)


def test_failed_steps_stay_in_history():
    """v2 把失败步从历史中丢掉（run.py:401-424）；我们保留并打标记。"""
    c = _ctx()
    c.record(1, "tree", "CLICK #4", "点击", failed=True)
    assert "✗" in c.history_block()


def test_entities_survive_and_never_delete_silently():
    c = _ctx()
    c.merge_entities({"时间": "19:00", "地址": "公司"})
    c.merge_entities({"时间": "", "新增": "值"})   # 空值不得擦除
    assert c.entities["时间"] == "19:00"
    assert c.entities["新增"] == "值"


def test_budget_pressure_trims_history_then_candidates():
    cands = [tree_mod.Candidate(ref=i, label=f"候选项目{i}号", cx=i * 10, cy=i * 20,
                                bounds=(0, 0, 10, 10), clickable=True, rid="", cls="")
             for i in range(1, 121)]
    c = _ctx(token_budget=1200)
    for i in range(30):
        c.record(i, "tree", f"CLICK #{i}", "很长的解释" * 20)
    text, kept, counts = c.trim(cands)
    assert counts["approx_input"] <= 1200
    assert len(kept) < len(cands)
    assert "裁剪" in text
    assert "任务" in text                  # 任务本身永不丢弃


def test_budget_trim_keeps_bottom_scroll_target():
    cands = [tree_mod.Candidate(i, f"说明{i}", 50, i * 20, (0, i * 20, 100, i * 20 + 10),
                                False, "", "android.widget.TextView") for i in range(1, 61)]
    target = tree_mod.Candidate(61, "列表", 500, 2200, (0, 1800, 1080, 2300),
                                False, "items", "android.widget.ScrollView", scrollable=True)
    c = _ctx(token_budget=550)
    text, kept, _ = c.trim(cands + [target])
    assert len(kept) < len(cands) + 1
    assert target in kept and "纵滚" in text


def test_task_and_entities_survive_extreme_pressure():
    c = _ctx(token_budget=200)
    c.merge_entities({"时间": "19:00"})
    text, _, _ = c.trim([tree_mod.Candidate(1, "x", 1, 1, (0, 0, 1, 1), True, "", "")])
    assert "播放" in text and "19:00" in text


def test_token_estimator_orders_cjk_and_ascii_correctly():
    assert estimate_tokens("") == 0
    assert estimate_tokens("播放七里香") == 5            # CJK 约 1/字
    assert estimate_tokens("a" * 400) == 100             # ascii 约 1/4 字符
    assert estimate_tokens("播放" * 50) < estimate_tokens("a" * 400) + 200


def _ids_xml(package: str, *markers: str) -> str:
    return "<hierarchy>" + "".join(
        f'<node resource-id="{package}:id/{marker}" bounds="[100,200][300,220]" />'
        for marker in markers
    ) + "</hierarchy>"


def test_app_page_hints_require_current_package_and_page_markers():
    book = PageHintBook(PROJECT_ROOT / "skills")
    music_package = "com.mosi.mosc.onlinemedia.qqmusic"
    apps = [AppConfig(name="QQ音乐", package=music_package)]
    player = _ids_xml(music_package, "iv_bottomCtrl1", "iv_bottomCtrl2", "sb_seekBar")
    hints = book.match(player, apps)
    assert any("收藏红心" in hint for hint in hints)
    assert any("sb_seekBar" in hint and "bounds=[100,200][300,220]" in hint for hint in hints)
    assert not any("地图视角" in hint for hint in hints)
    assert book.match(_ids_xml(music_package, "iv_bottomCtrl1"), apps) == []
    assert book.match(_ids_xml("com.other.app", "iv_bottomCtrl1", "iv_bottomCtrl2"), apps) == []
    assert book.match(player, [AppConfig(name="QQ音乐", package="com.changed.music")]) == []

    nav = _ids_xml("com.fawvw.vehicle.navi.ceaapp",
                   "main_button_car_angle_layout", "main_button_scale_gps_layout")
    assert any("地图视角" in hint for hint in book.match(nav, apps))


def test_global_prompt_has_no_app_layout_or_slider_experiment():
    assert "iv_bottomCtrl1" not in DECIDE_SYSTEM
    assert "main_button_car_angle_layout" not in DECIDE_SYSTEM
    assert "实测不响应" not in DECIDE_SYSTEM
    assert "sb_seekBar" not in decide_user_text("任务", (1920, 1080))


def test_only_current_page_hints_enter_context():
    c = _ctx(page_hints=["QQ音乐 / 主页：iv_mine 是「我的」入口"])
    text, _, _ = c.trim([])
    assert "当前页面提示" in text and "iv_mine" in text
    c.page_hints = []
    text, _, _ = c.trim([])
    assert "当前页面提示" not in text and "iv_mine" not in text


def test_external_prompt_templates_render_all_routes():
    tree = build_decide_messages("任务：播放歌曲", None, screen=(1920, 1080))
    assert "CLICK(node_ref | point | coord, target_label?)" in tree[0]["content"]
    assert "1920×1080" in tree[1]["content"]
    vlm = build_decide_messages("任务：播放歌曲", "abc", image_size=(1568, 882))
    assert vlm[1]["content"][1]["text"].startswith("当前截图尺寸为 1568×882")
    assert '"point":[990,127]' in vlm[1]["content"][3]["text"]
    reflect = build_reflect_messages("CLICK", "播放歌曲", "before", "after")
    assert "A = 动作生效" in reflect[0]["content"]
    assert "刚执行的动作：CLICK" in reflect[1]["content"][0]["text"]
    assert "打开美团" in render("initial_route_user", task="打开美团", catalog="1: 美团",
                                  skill_catalog="（无）")
