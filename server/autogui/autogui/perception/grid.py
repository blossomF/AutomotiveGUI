"""Landscape grid overlay and label<->pixel mapping (the path-A fallback surface).

Reuses the *idea* of ``LightGUIAgent/lightguiagent/grid_converter.py`` (single-letter
columns + 1-based rows, so the model answers "E5" instead of a pixel pair, which
measurably reduces coordinate hallucination) but is implemented fresh for two
reasons documented in 技术方案评估.md §2 C-11:

  * that project's auto-density clamps rows to ``max(15, ...)``
    (``settings.py:159-171``), tuned for a portrait phone; on a 1920x1080 landscape
    head unit it yields ~128x72px cells. We take cols/rows from config verbatim.
  * its ``GridStyle`` schema forbids ``rows < 10``, so a landscape grid is barely
    expressible at all.

A 20x10 landscape grid gives ~96x108px cells at 1920x1080 — roughly one touch target
per cell, which is what makes the label unambiguous to a VLM.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass
class Grid:
    width: int
    height: int
    cols: int = 20
    rows: int = 10

    def __post_init__(self) -> None:
        if not (1 <= self.cols <= 26):
            raise ValueError("cols must be within 1..26 for single-letter labels")
        if self.cols > len(_ALPHABET):
            raise ValueError("cols exceeds available letters")
        self.cell_w = self.width / self.cols
        self.cell_h = self.height / self.rows

    # ── conversion ───────────────────────────────────────────────────────────

    def to_pixel(self, label: str) -> tuple[int, int]:
        """``"E5"`` -> centre of that cell. Raises ``ValueError`` on malformed input."""
        s = (label or "").strip().upper()
        if len(s) < 2 or not s[0].isalpha() or not s[1:].isdigit():
            raise ValueError(f"invalid grid label {label!r}, expected like 'E5'")
        col = _ALPHABET.index(s[0])
        row = int(s[1:])
        if col >= self.cols:
            raise ValueError(f"column {s[0]} out of range A-{_ALPHABET[self.cols - 1]}")
        if not (1 <= row <= self.rows):
            raise ValueError(f"row {row} out of range 1..{self.rows}")
        return (int((col + 0.5) * self.cell_w), int((row - 0.5) * self.cell_h))

    def to_label(self, x: int, y: int) -> str:
        col = min(max(int(x / self.cell_w), 0), self.cols - 1)
        row = min(max(int(y / self.cell_h), 0), self.rows - 1)
        return f"{_ALPHABET[col]}{row + 1}"

    def in_bounds(self, label: str) -> bool:
        try:
            self.to_pixel(label)
            return True
        except ValueError:
            return False

    # ── rendering ────────────────────────────────────────────────────────────

    def annotate(self, png_bytes: bytes, style=None) -> Image.Image:
        """Draw the grid and cell labels onto a screenshot, return the PIL image."""
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        if img.size != (self.width, self.height):
            # Keep geometry honest: a resized capture would desync labels from pixels.
            self.width, self.height = img.size
            self.cell_w = self.width / self.cols
            self.cell_h = self.height / self.rows

        draw = ImageDraw.Draw(img, "RGBA")
        font = _load_font(getattr(style, "label_size", 22) if style else 22)
        line = getattr(style, "line_color", (255, 0, 0)) if style else (255, 0, 0)
        lw = getattr(style, "line_width", 3) if style else 3
        lcol = getattr(style, "label_color", (255, 255, 0)) if style else (255, 255, 0)
        lbg = getattr(style, "label_bg", (0, 0, 0, 180)) if style else (0, 0, 0, 180)

        for c in range(1, self.cols):
            x = int(c * self.cell_w)
            draw.line([(x, 0), (x, self.height)], fill=(*line, 150), width=lw)
        for r in range(1, self.rows):
            y = int(r * self.cell_h)
            draw.line([(0, y), (self.width, y)], fill=(*line, 150), width=lw)

        for r in range(self.rows):
            for c in range(self.cols):
                label = f"{_ALPHABET[c]}{r + 1}"
                cx, cy = int((c + 0.5) * self.cell_w), int((r + 0.5) * self.cell_h)
                tw = draw.textlength(label, font=font)
                th = label_height(font)
                draw.rectangle(
                    [cx - tw / 2 - 4, cy - th / 2 - 2, cx + tw / 2 + 4, cy + th / 2 + 2],
                    fill=lbg,
                )
                _draw_centred(draw, (cx, cy), label, font, lcol)
        return img

    def mark_action(self, img: Image.Image, x: int, y: int, radius: int = 34) -> Image.Image:
        """Crosshair the point we tapped — for the before/after reflection image."""
        marked = img.convert("RGB").copy()
        draw = ImageDraw.Draw(marked, "RGBA")
        draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                     outline=(0, 255, 0, 255), width=5)
        draw.line([(x - radius - 12, y), (x + radius + 12, y)], fill=(0, 255, 0, 255), width=3)
        draw.line([(x, y - radius - 12), (x, y + radius + 12)], fill=(0, 255, 0, 255), width=3)
        return marked


_font_cache: dict[int, object] = {}


def _draw_centred(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill) -> None:
    """Draw text centred on ``xy``.

    ``anchor="mm"`` is only supported by TrueType fonts, and ``_load_font`` falls back
    to Pillow's default bitmap font when none is installed — which is plausible on a
    bare Windows test VM. Computing the offset from the measured size works either way.
    """
    x, y = xy
    try:
        draw.text((x, y), text, fill=fill, font=font, anchor="mm")
    except (ValueError, AttributeError):
        tw = draw.textlength(text, font=font)
        th = label_height(font)
        draw.text((x - tw / 2, y - th / 2), text, fill=fill, font=font)


def _load_font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def label_height(font) -> int:
    try:
        return int(font.getbbox("Ag")[3])
    except Exception:  # pragma: no cover - default font lacks getbbox in some Pillow
        return 16


def encode_jpeg(img: Image.Image, quality: int = 85, max_side: int = 1568) -> bytes:
    """Downscale then JPEG-encode. ``max_side`` replaces LightGUIAgent's hardcoded
    ``target_size: 1568 # Claude optimal`` (config.py:91) with a tunable — the
    optimum for GLM-V is an open question for the 9/19 A/B test."""
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
