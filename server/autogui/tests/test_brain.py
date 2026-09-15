"""Brain layer: action parsing, JSON strictness, reflection, context budget."""

import pytest

from autogui.actions import ACTION_NAMES, parse_action
from autogui.brain.context import Context, estimate_tokens
from autogui.brain.prompts import first_json_object, parse_reflection
from autogui.perception import tree as tree_mod
from tests.conftest import SAMPLE_XML


# ── action space ─────────────────────────────────────────────────────────────────


def test_back_and_home_exist():
    """LightGUIAgent's action list has neither, so recovery meant guessing at a
    top-left back button in the grid. Both must be first-class here."""
    assert "BACK" in ACTION_NAMES and "HOME" in ACTION_NAMES
    assert "FAIL" in ACTION_NAMES      # an agent needs a way to give up honestly
    assert parse_action({"action": "back"})["type"] == "BACK"


def test_click_accepts_node_ref_or_grid():
    assert parse_action({"action": "CLICK", "node_ref": 7})["node_ref"] == 7
    assert parse_action({"action": "CLICK", "node_ref": "#12"})["node_ref"] == 12
    assert parse_action({"action": "CLICK", "grid": "j5"})["grid"] == "J5"
    with pytest.raises(ValueError):
        parse_action({"action": "CLICK"})


def test_rejects_unknown_action_and_bad_scroll_direction():
    with pytest.raises(ValueError):
        parse_action({"action": "EXPLODE"})
    with pytest.raises(ValueError):
        parse_action({"action": "SCROLL", "direction": "sideways"})
    assert parse_action({"action": "SCROLL", "direction": "DOWN"})["direction"] == "down"


def test_wait_seconds_is_capped():
    """A model asking for a 600s wait must not eat the whole Demo Day slot."""
    assert parse_action({"action": "WAIT", "seconds": 600})["seconds"] == 5.0
    assert parse_action({"action": "WAIT"})["seconds"] == 1.5


def test_explain_is_length_bounded():
    action = parse_action({"action": "BACK", "explain": "x" * 5000})
    assert len(action["explain"]) <= 200


# ── JSON extraction ──────────────────────────────────────────────────────────────


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


# ── reflection parsing (技术方案评估.md §2 B-5) ──────────────────────────────────


def test_reflection_is_strict_not_substring():
    """Mobile-Agent-v2 does ``if 'A' in reflect`` (run.py:401). Any Latin letter A in
    the model's free-form reasoning — 'ADB', 'Activity', 'App' — silently misreads as
    success. Bare-token matching removes that class of bug."""
    assert parse_reflection("A") == "A"
    assert parse_reflection("B") == "B"
    assert parse_reflection("C") == "C"
    assert parse_reflection("A. 页面变化了") == "A"
    assert parse_reflection("回答： C") == "C"
    # The v2 regression, pinned:
    assert parse_reflection("The ADB activity started the App on a new Activity") == "UNKNOWN"


def test_unparsable_reflection_is_unknown_not_silently_A():
    assert parse_reflection("") == "UNKNOWN"
    assert parse_reflection("我无法判断") == "UNKNOWN"


# ── context economy ──────────────────────────────────────────────────────────────


def _ctx(**kw) -> Context:
    base = dict(task="在音乐App里播放周杰伦的《七里香》", declared_path="C", history_window=5, token_budget=8000)
    base.update(kw)
    return Context(**base)


def test_tree_observation_is_text_only():
    """No image on the tree route is where most of the token saving comes from."""
    c = _ctx()
    cands = tree_mod.extract(SAMPLE_XML, screen=(1920, 1080), limit=40)
    text, kept, counts = c.trim(cands)
    assert "#1 " in text
    assert "base64" not in text.lower()
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
    """v2 drops failed steps from history (run.py:401-424); we keep and mark them."""
    c = _ctx()
    c.record(1, "tree", "CLICK #4", "点击", failed=True)
    assert "✗" in c.history_block()


def test_entities_survive_and_never_delete_silently():
    c = _ctx()
    c.merge_entities({"时间": "19:00", "地址": "公司"})
    c.merge_entities({"时间": "", "新增": "值"})   # empty must not erase
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
    assert "任务" in text                  # the task itself is never dropped


def test_task_and_entities_survive_extreme_pressure():
    c = _ctx(token_budget=200)
    c.merge_entities({"时间": "19:00"})
    text, _, _ = c.trim([tree_mod.Candidate(1, "x", 1, 1, (0, 0, 1, 1), True, "", "")])
    assert "播放" in text and "19:00" in text


def test_token_estimator_orders_cjk_and_ascii_correctly():
    assert estimate_tokens("") == 0
    assert estimate_tokens("播放七里香") == 5            # ~1 per CJK char
    assert estimate_tokens("a" * 400) == 100             # ~1 per 4 ascii chars
    assert estimate_tokens("播放" * 50) < estimate_tokens("a" * 400) + 200
