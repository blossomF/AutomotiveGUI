"""一次观测轮次：并行抓取帧和 UI 树。

这里有两件事是参照项目做得更差的：

  * **并行抓取。** ``screencap`` 和 ``uiautomator dump`` 是相互独立的设备端操作，
    而 ``dump`` 是慢的那一个（在这些车机上要好几秒）。按顺序执行——就像
    ``LightGUIAgent/lightguiagent/agent.py:117-135`` 对截图所做的那样——
    会让每步延迟变成两者的*和*。并行执行则让延迟变成两者的最大值。
    这个差距正处在 10 分性能得分的关键路径上。
  * **屏幕指纹。** 可见文本摘要 + 大幅降采样的图像哈希，构成*程序化的*
    无进展检测器。LightGUIAgent 的卡死处理只是在提示词里追加一句话
    （``claude_client.py:322-338``），所以没有任何东西能真正拦住一个
    决定连续点同一个格子 20 次的模型。见 技术方案评估.md §2 C-9。
"""

from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image

from autogui.bridge.adb import Device
from autogui.perception import tree as tree_mod
from autogui.perception.grid import Grid, grid_dimensions


@dataclass
class Observation:
    png: bytes
    xml: str
    width: int
    height: int
    signature: str
    candidates: list[tree_mod.Candidate] = field(default_factory=list)
    tree_ms: float = 0.0
    shot_ms: float = 0.0

    @property
    def node_count(self) -> int:
        return len(tree_mod.nodes(self.xml))

    @property
    def tree_usable(self) -> bool:
        """可寻址且有标签的节点足够多，可以走 B 路径。

        dump 可以很大却没用——一个全是 FrameLayout、没有任何文本的树
        会产生零个候选，这就是回退到网格的信号。
        """
        return len(self.candidates) >= 3


def _downsample_hash(png: bytes) -> str:
    """粗粒度像素摘要，让纯视觉变化也能被登记。

    16x16 灰度是刻意有损的：我们要的是“屏幕到底有没有变化”，
    而不是“是否精确变化”。压缩噪声和时钟跳动会让个别桶变化，
    严格的整图哈希对此会过度反应。
    """
    try:
        img = Image.open(io.BytesIO(png)).convert("L").resize((16, 16), Image.BILINEAR)
    except Exception:  # pragma: no cover - 损坏的截图
        return "img?"
    return hashlib.md5(img.tobytes()).hexdigest()[:8]


def compute_signature(xml: str, png: bytes) -> str:
    return f"{hashlib.md5(tree_mod.screen_signature(xml).encode('utf-8')).hexdigest()[:8]}:{_downsample_hash(png)}"


def observe(
    device: Device,
    *,
    grid_cols: int = 20,
    grid_rows: int = 10,
    grid_mode: str = "manual",
    candidate_limit: int = 40,
    last_size: tuple[int, int] | None = None,
) -> tuple[Observation, Grid]:
    """并行抓取帧 + 树，并为当前屏幕构建网格。"""
    import time

    timings: dict[str, float] = {}

    def shot() -> tuple[bytes, float]:
        t = time.perf_counter()
        b = device.screenshot()
        return b, (time.perf_counter() - t) * 1000

    def dump() -> tuple[str, float]:
        t = time.perf_counter()
        x = device.dump_xml()
        return x, (time.perf_counter() - t) * 1000

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_shot = pool.submit(shot)
        f_dump = pool.submit(dump)
        png, timings["shot"] = f_shot.result()
        xml, timings["dump"] = f_dump.result()

    width, height = _resolve_size(png, last_size or device.screen_size())
    cands = tree_mod.candidates_from_xml(xml, limit=candidate_limit, screen=(width, height))
    cols, rows = grid_dimensions(width, height, mode=grid_mode, cols=grid_cols, rows=grid_rows)
    grid = Grid(width=width, height=height, cols=cols, rows=rows)

    return (
        Observation(
            png=png,
            xml=xml,
            width=width,
            height=height,
            signature=compute_signature(xml, png),
            candidates=cands,
            shot_ms=round(timings["shot"], 1),
            tree_ms=round(timings["dump"], 1),
        ),
        grid,
    )


def _resolve_size(png: bytes, fallback: tuple[int, int]) -> tuple[int, int]:
    """信任解码出的帧而不是 ``wm size``——多屏设备上两者会不一致。"""
    from autogui.bridge.adb import png_size

    return png_size(png) or fallback
