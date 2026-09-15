"""Shared fixtures: a landscape head-unit UI dump with no device and no network.

The XML mirrors what ``uiautomator dump`` actually emits on these builds — a deep
nesting of unlabelled FrameLayout containers around a minority of genuinely
addressable nodes, which is exactly the case ``perception/tree.py`` has to filter.
"""

import pytest

SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][1920,1080]">
    <node index="0" text="" resource-id="com.car.music:id/root" class="android.widget.LinearLayout" content-desc="" clickable="false" bounds="[0,0][1920,1080]">
      <node index="0" text="" resource-id="android:id/statusBarBackground" class="android.view.View" content-desc="" clickable="false" bounds="[0,0][1920,72]" />
      <node index="1" text="搜索音乐" resource-id="com.car.music:id/search_box" class="android.widget.EditText" content-desc="" clickable="true" bounds="[640,36][1280,108]" />
      <node index="2" text="" resource-id="" class="android.widget.ImageButton" content-desc="播放" clickable="true" bounds="[80,920][200,1040]" />
      <node index="3" text="七里香 - 周杰伦" resource-id="com.car.music:id/title" class="android.widget.TextView" content-desc="" clickable="false" bounds="[220,930][700,975]" />
      <node index="4" text="" resource-id="" class="android.widget.LinearLayout" content-desc="" clickable="false" bounds="[0,108][1920,900]">
        <node index="0" text="告白气球" resource-id="com.car.music:id/item" class="android.widget.TextView" content-desc="" clickable="true" bounds="[0,120][1920,220]" />
        <node index="1" text="晴天" resource-id="com.car.music:id/item" class="android.widget.TextView" content-desc="" clickable="true" bounds="[0,220][1920,320]" />
        <node index="2" text="夜曲" resource-id="com.car.music:id/item" class="android.widget.TextView" content-desc="" clickable="true" bounds="[0,320][1920,420]" />
      </node>
      <node index="5" text="隐私协议" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" bounds="[400,1050][700,1075]" />
      <node index="6" text="同意" resource-id="com.car.music:id/ok" class="android.widget.Button" content-desc="" clickable="true" bounds="[1500,1000][1700,1060]" />
      <node index="7" text="以后再说" resource-id="com.car.music:id/later" class="android.widget.Button" content-desc="" clickable="true" bounds="[1720,1000][1900,1060]" />
    </node>
  </node>
</hierarchy>"""

# What a self-drawn / SurfaceView app produces: a big tree with nothing addressable.
EMPTY_TREE_XML = """<?xml version='1.0'?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][1920,1080]" />
  <node index="1" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][1920,1080]" />
</hierarchy>"""


@pytest.fixture
def sample_xml() -> str:
    return SAMPLE_XML


@pytest.fixture
def empty_xml() -> str:
    return EMPTY_TREE_XML


@pytest.fixture
def landscape() -> tuple[int, int]:
    return (1920, 1080)
