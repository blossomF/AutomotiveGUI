"""技能层：模型指导与显式配方执行的边界。

这些测试都不触碰设备——执行器的设备调用由真机上的
``main.py --skill-only`` 覆盖。
"""

import textwrap
from pathlib import Path

import pytest

from autogui.skills.base import DEFAULT_POPUPS, GENERIC_DISMISS, SkillBook, SkillMiss


def test_qq_music_has_one_play_song_entry():
    root = Path(__file__).resolve().parents[1] / "skills"
    book = SkillBook(root, device=None, text_input=None)  # type: ignore[arg-type]
    matches = [skill for skill in book.for_configured_app("QQ音乐")
               if skill.name == "play_song"]
    assert len(matches) == 1
    assert matches[0].app == "music"
    guide = book.guidance("music.play_song", {"song": "王菲"})
    assert "先聚焦输入框" in guide and "歌手" in guide
    assert matches[0].steps[3] == {"act": "type", "arg": "song", "clear_first": True}

MUSIC_YAML = textwrap.dedent(
    """
    name: music
    app:
      package: com.car.music
    popups:
      - { detect: "用户协议", button: "同意" }
    skills:
      - name: play_song
        summary: 搜索并播放
        params: [song]
        guide: 先搜索 {song}，核对歌曲与播放状态后才能完成。
        steps:
          - { act: launch }
          - { act: dismiss_popups }
          - { act: click, hint: { text: "搜索" } }
          - { act: type, arg: song, clear_first: true }
          - { act: complete }
      - name: pause
        summary: 暂停
        guide: 先检查当前确实正在播放，再点击暂停并复核。
        steps:
          - { act: click, hint: { desc: "暂停" } }
    """
)


@pytest.fixture
def book(tmp_path) -> SkillBook:
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "skill.yaml").write_text(MUSIC_YAML, encoding="utf-8")
    return SkillBook(tmp_path, device=None, text_input=None)   # type: ignore[arg-type]


def test_yaml_loads_into_addressable_skills(book):
    assert set(book.by_id) == {"music.play_song", "music.pause"}
    skill = book.get("music.play_song")
    assert skill.package == "com.car.music"
    assert skill.params == ["song"]
    assert len(skill.steps) == 5


def test_lookup_accepts_short_key(book):
    assert book.get("play_song") is book.get("music.play_song")
    assert book.get("nope") is None


def test_index_is_one_compact_line_per_skill_for_the_permanent_block(book):
    index = book.index()
    assert "play_song(song=...)" in index
    assert len(index.splitlines()) == 2
    # 它会进*每一轮*提示词，所以必须保持很小。
    assert len(index) < 200


def test_missing_skill_dir_is_not_an_error(tmp_path):
    empty = SkillBook(tmp_path / "absent", device=None, text_input=None)  # type: ignore[arg-type]
    assert empty.by_id == {}
    assert empty.index() == "（无）"


def test_configured_app_package_overrides_yaml_package(tmp_path):
    """包名以页面配置（config.yaml apps 段）为准：已绑定技能的配置包名覆盖 YAML。"""
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "skill.yaml").write_text(
        MUSIC_YAML + "configured_app: 音乐\n", encoding="utf-8")
    book = SkillBook(tmp_path, device=None, text_input=None,
                     app_packages={"音乐": "com.car.music2"})   # type: ignore[arg-type]
    assert book.get("music.play_song").package == "com.car.music2"


def test_yaml_package_kept_when_app_not_in_config(tmp_path):
    """绑定应用不在配置里时，回退 YAML 包名。"""
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "skill.yaml").write_text(
        MUSIC_YAML + "configured_app: 音乐\n", encoding="utf-8")
    book = SkillBook(tmp_path, device=None, text_input=None,
                     app_packages={"别的App": "com.other"})   # type: ignore[arg-type]
    assert book.get("music.play_song").package == "com.car.music"


def test_malformed_yaml_is_skipped_not_fatal(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "skill.yaml").write_text("name: [unclosed", encoding="utf-8")
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "skill.yaml").write_text(MUSIC_YAML, encoding="utf-8")
    book = SkillBook(tmp_path, device=None, text_input=None)  # type: ignore[arg-type]
    assert book.by_id  # 合法的那个仍然加载成功


# ── 模型指导 ───────────────────────────────────────────────────────────────────


def test_guidance_is_text_and_does_not_touch_device(book):
    text = book.guidance("music.play_song", {"song": "七里香"})
    assert "七里香" in text
    assert "核对歌曲与播放状态" in text
    assert "只有看到目标已达成" in text


def test_guidance_requires_params(book):
    with pytest.raises(SkillMiss, match="missing guide params"):
        book.guidance("music.play_song", {})


def test_index_only_shows_skills_with_guides(book):
    book.get("music.pause").guide = ""
    assert "music.pause" not in book.index()


# ── 降级契约 ───────────────────────────────────────────────────────────────────


def test_missing_required_param_raises_skill_miss(book):
    """宁可把这一轮交给模型，也不要用编造的槽位值跑配方。"""
    with pytest.raises(SkillMiss):
        book.run("music.play_song", {})


def test_unknown_skill_raises_skill_miss(book):
    with pytest.raises(SkillMiss):
        book.run("does.not.exist", {})


def test_step_referencing_an_unknown_param_fails_loudly(book):
    """步骤引用了技能未声明的参数属于编写 bug。必须以 SkillMiss
    暴露出来，绝不能变成静默为空的 type 步骤。"""
    from autogui.skills.base import Skill

    stray = Skill(app="music", name="stray", summary="", params=[], steps=[], package="com.car.music")
    with pytest.raises(SkillMiss, match="unknown param"):
        book._arg_or_text(stray, {"act": "type", "arg": "missing"}, {})


def test_param_substitution_in_hints(book):
    from autogui.skills.base import Skill

    song = Skill(app="music", name="x", summary="", params=["song"], steps=[], package="com.car.music")
    assert book._sub(song, "{song}", {"song": "七里香"}) == "七里香"
    assert book._sub(song, "{package}", {}) == "com.car.music"


# ── 弹窗词表必须本地化（技术方案评估.md §2 A-3） ────────────────────────────────


def test_default_popup_words_are_chinese():
    """android-agent 原版是英文/TikTok 专属的，在这里什么都匹配不到。"""
    assert all(any("一" <= ch <= "鿿" for ch in w) for w in GENERIC_DISMISS[:8])
    assert "以后再说" in GENERIC_DISMISS
    assert "not now" not in [w.lower() for w in GENERIC_DISMISS]


def test_declared_popups_have_detect_and_button_shapes():
    for popup in DEFAULT_POPUPS:
        assert "detect" in popup and ("button" in popup or popup.get("method") == "back")
