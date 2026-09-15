"""One observation round: grab the frame and the UI tree concurrently.

Two things happen here that the reference projects do worse:

  * **Parallel capture.** ``screencap`` and ``uiautomator dump`` are independent
    device-side operations, and ``dump`` is the slow one (seconds on these head
    units). Sequencing them — as ``LightGUIAgent/lightguiagent/agent.py:117-135`` does
    for the frame alone — makes per-step latency the *sum*. Running them together
    makes it the max. That difference is on the critical path of the 10-point
    performance score.
  * **Screen fingerprint.** A digest of visible text plus a heavily downsampled
    image hash gives a *programmatic* no-progress detector. LightGUIAgent's stuck
    handling is only a sentence appended to the prompt
    (``claude_client.py:322-338``), so nothing actually stops a model that decides to
    tap the same cell 20 times. See 技术方案评估.md §2 C-9.
"""

from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image

from autogui.bridge.adb import Device
from autogui.perception import tree as tree_mod
from autogui.perception.grid import Grid


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
        """Enough labelled, addressable nodes to try the path-B route.

        A dump can be large yet useless — an all-FrameLayout tree with no text
        yields zero candidates, which is the signal to fall back to the grid.
        """
        return len(self.candidates) >= 3


def _downsample_hash(png: bytes) -> str:
    """Coarse pixel digest so purely-visual changes register.

    16x16 grayscale is deliberately lossy: we want "did the screen change at all",
    not "did it change exactly". Compression noise and clock ticks change a few
    buckets, which a strict full-image hash would over-react to.
    """
    try:
        img = Image.open(io.BytesIO(png)).convert("L").resize((16, 16), Image.BILINEAR)
    except Exception:  # pragma: no cover - corrupt capture
        return "img?"
    return hashlib.md5(img.tobytes()).hexdigest()[:8]


def compute_signature(xml: str, png: bytes) -> str:
    return f"{hashlib.md5(tree_mod.screen_signature(xml).encode('utf-8')).hexdigest()[:8]}:{_downsample_hash(png)}"


def observe(
    device: Device,
    *,
    grid_cols: int = 20,
    grid_rows: int = 10,
    candidate_limit: int = 40,
    last_size: tuple[int, int] | None = None,
) -> tuple[Observation, Grid]:
    """Capture frame + tree in parallel and build the grid for this screen."""
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
    grid = Grid(width=width, height=height, cols=grid_cols, rows=grid_rows)

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
    """Trust the decoded frame over ``wm size`` — they disagree on multi-display units."""
    from autogui.bridge.adb import png_size

    return png_size(png) or fallback
