"""Bounded feedback control for readable Android value wheels.

The model chooses a wheel and a target value. Pixel travel is calibrated from
the current wheel on the current device; no app-specific pixels/unit are used.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from autogui.bridge.adb import node_bounds, node_class, node_desc, node_rid, node_text, nodes
from autogui.perception.tree import Candidate, is_value_wheel


class PickerError(ValueError):
    """The wheel cannot safely be driven to the requested value."""


_VALUE = re.compile(r"^\s*([+-]?\d+)\s*([^\d]*)\s*$")


@dataclass(frozen=True)
class PickerReading:
    value: int
    unit: str
    bounds: tuple[int, int, int, int]


def _parse_value(text: str) -> tuple[int, str]:
    match = _VALUE.fullmatch(text)
    if not match:
        raise PickerError(f"滚轮值不是单个可计算数字：{text!r}")
    return int(match[1]), match[2].strip()


def _difference(a: int, b: int, unit: str) -> int:
    """Signed shortest distance on a minute wheel; other wheels stay linear."""
    raw = a - b
    if unit == "分" and 0 <= a < 60 and 0 <= b < 60:
        return (raw + 30) % 60 - 30
    return raw


def read_picker(xml: str, candidate: Candidate) -> PickerReading:
    """Find the same wheel after a fresh dump, even when its value changes."""
    if not is_value_wheel(candidate):
        raise PickerError(f"#{candidate.ref} 不是可读的数值滚轮")
    matches = []
    for node in nodes(xml):
        if node_class(node).rsplit(".", 1)[-1] != "NumberPicker":
            continue
        bounds = node_bounds(node)
        if not bounds:
            continue
        rid = node_rid(node).rsplit("/", 1)[-1]
        if candidate.rid and rid != candidate.rid:
            continue
        cx, cy = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        distance = abs(cx - candidate.cx) + abs(cy - candidate.cy)
        matches.append((distance, node, bounds))
    if not matches:
        raise PickerError("滚轮已消失或控件树无法读取；停止操作")
    distance, node, bounds = min(matches, key=lambda item: item[0])
    if distance > max(bounds[2] - bounds[0], bounds[3] - bounds[1]):
        raise PickerError("滚轮位置已改变；停止操作")
    value, unit = _parse_value(node_desc(node) or node_text(node))
    return PickerReading(value, unit, bounds)


def _gesture(bounds: tuple[int, int, int, int], signed_pixels: int) -> tuple[int, int, int, int]:
    """Return a gesture whose *both* endpoints stay inside the wheel."""
    x1, y1, x2, y2 = bounds
    height = y2 - y1
    if x2 - x1 < 20 or height < 80:
        raise PickerError("滚轮区域过小，无法安全滑动")
    margin = max(8, round(height * 0.1))
    inner_top, inner_bottom = y1 + margin, y2 - margin
    travel = min(abs(signed_pixels), round((inner_bottom - inner_top) * 0.8))
    if travel < 12:
        raise PickerError("滚轮可滑动距离不足")
    center = (inner_top + inner_bottom) // 2
    start_y = center - travel // 2 if signed_pixels > 0 else center + travel // 2
    end_y = start_y + travel if signed_pixels > 0 else start_y - travel
    x = (x1 + x2) // 2
    return x, start_y, x, end_y


def _visible_bounds(
    bounds: tuple[int, int, int, int], screen: tuple[int, int] | None
) -> tuple[int, int, int, int]:
    if screen is None:
        return bounds
    width, height = screen
    return (max(0, bounds[0]), max(0, bounds[1]),
            min(width, bounds[2]), min(height, bounds[3]))


def roll_to(
    candidate: Candidate,
    target: str,
    *,
    dump_xml: Callable[[], str],
    swipe: Callable[[int, int, int, int], str],
    fast_swipe: Callable[[int, int, int, int], str] | None = None,
    report: Callable[..., None] | None = None,
    delta_hint: int | None = None,
    screen: tuple[int, int] | None = None,
    max_swipes: int = 16,
    max_seconds: float = 70.0,
) -> str:
    """Move one numeric wheel to its exact target with bounded observations."""
    wanted, unit = _parse_value(target)
    started = time.monotonic()
    reading = read_picker(dump_xml(), candidate)
    if unit and reading.unit and unit != reading.unit:
        raise PickerError(f"目标单位 {unit!r} 与滚轮单位 {reading.unit!r} 不一致")
    initial_delta = _difference(wanted, reading.value, reading.unit)
    if delta_hint is not None and delta_hint != initial_delta and report:
        report("模型差值与实时读数不符，以实时读数为准", model_delta=delta_hint,
               actual_delta=initial_delta)
    if report:
        report(f"滚轮 #{candidate.ref} 开始调整", current=f"{reading.value}{reading.unit}",
               target=f"{wanted}{reading.unit}", delta_units=initial_delta,
               model_delta=delta_hint, bounds=reading.bounds)
    if reading.value == wanted:
        return f"ROLL_TO #{candidate.ref} 已是 {wanted}{reading.unit}（0 次滑动）"

    # The initial direction is only a probe. Subsequent gestures use the
    # observed value change, since picker implementations may invert direction.
    finger_for_increase = -1  # -1 = finger up, +1 = finger down
    pixels_per_unit: float | None = None
    fast_works = fast_swipe is not None
    unchanged = 0
    for attempt in range(1, max_swipes + 1):
        if time.monotonic() - started >= max_seconds:
            raise PickerError(f"滚轮调整超过 {max_seconds:g}s；当前 {reading.value}{reading.unit}")
        remaining = _difference(wanted, reading.value, reading.unit)
        use_fast = fast_works and abs(remaining) >= 10
        bounds = _visible_bounds(reading.bounds, screen)
        height = bounds[3] - bounds[1]
        inner_span = height - 2 * max(8, round(height * 0.1))
        max_travel = round(inner_span * 0.8)
        if use_fast:
            distance = max_travel
        elif pixels_per_unit is None:
            distance = min(max_travel, max(40, round(inner_span * (0.6 if unchanged else 0.4))))
        else:
            distance = min(max_travel, max(36, round(min(abs(remaining), 4) * pixels_per_unit)))
        direction = finger_for_increase * (1 if remaining > 0 else -1)
        vector = _gesture(bounds, direction * distance)
        before = reading.value
        (fast_swipe if use_fast else swipe)(*vector)
        reading = read_picker(dump_xml(), candidate)
        change = _difference(reading.value, before, reading.unit)
        if report:
            report(f"滚轮 #{candidate.ref}: {before}{reading.unit} → {reading.value}{reading.unit}",
                   target=f"{wanted}{reading.unit}", swipe=vector, attempt=attempt,
                   delta=change, gesture="fast" if use_fast else "precise",
                   observed_px_per_unit=(round(abs(vector[3] - vector[1]) / abs(change), 1)
                                         if change else None))
        if reading.value == wanted:
            return f"ROLL_TO #{candidate.ref} {before}{reading.unit}→{wanted}{reading.unit}，{attempt} 次滑动已核验"
        if change == 0:
            fast_works = False
            unchanged += 1
            if unchanged >= 2:
                raise PickerError(f"滚轮连续两次无变化；当前 {reading.value}{reading.unit}")
            continue
        unchanged = 0
        # Finger sign times value-change sign tells us which finger direction
        # increases the selected value on this specific wheel.
        finger_for_increase = (1 if direction > 0 else -1) * (1 if change > 0 else -1)
        if use_fast:
            # Velocity determines fling travel; this is not a pixels/unit calibration.
            # A picker that ignores the fast gesture falls back to precise swipes.
            fast_works = abs(change) >= 3 and abs(_difference(wanted, reading.value, reading.unit)) >= 10
            pixels_per_unit = None
        else:
            pixels_per_unit = abs(vector[3] - vector[1]) / abs(change)
    raise PickerError(f"滚轮达到 {max_swipes} 次滑动上限；当前 {reading.value}{reading.unit}，目标 {wanted}{reading.unit}")
