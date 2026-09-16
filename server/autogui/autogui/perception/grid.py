"""横屏网格叠加与 标签<->像素 映射（A 路径的兜底面）。

借鉴 ``LightGUIAgent/lightguiagent/grid_converter.py`` 的*思路*（单字母
列 + 从 1 开始的行，让模型回答 "E5" 而不是一对像素坐标，这被证实能
减少坐标幻觉），但重新实现，原因有二，记录于 技术方案评估.md §2 C-11：

  * 那个项目的自动密度把行数钳制到 ``max(15, ...)``
    （``settings.py:159-171``），是按竖屏手机调优的；在 1920x1080
    横屏车机上会得到 ~128x72px 的格子。我们原样从配置取 cols/rows。
  * 它的 ``GridStyle`` schema 禁止 ``rows < 10``，导致横屏网格几乎
    无法表达。

20x10 的横屏网格在 1920x1080 下得到 ~96x108px 的格子——大约每格
一个触摸目标，这正是标签对 VLM 无歧义的原因。
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

    # ── 转换 ─────────────────────────────────────────────────────────────────

    def to_pixel(self, label: str) -> tuple[int, int]:
        """``"E5"`` -> 该格子中心。输入非法时抛 ``ValueError``。"""
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

    # ── 渲染 ─────────────────────────────────────────────────────────────────

    def annotate(self, png_bytes: bytes, style=None) -> Image.Image:
        """在截图上画网格和格子标签，返回 PIL 图像。"""
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        if img.size != (self.width, self.height):
            # 保持几何一致性：缩放过的截图会让标签与像素错位。
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
        """给点过的点画十字线——用于反思用的前后对比图。"""
        marked = img.convert("RGB").copy()
        draw = ImageDraw.Draw(marked, "RGBA")
        draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                     outline=(0, 255, 0, 255), width=5)
        draw.line([(x - radius - 12, y), (x + radius + 12, y)], fill=(0, 255, 0, 255), width=3)
        draw.line([(x, y - radius - 12), (x, y + radius + 12)], fill=(0, 255, 0, 255), width=3)
        return marked


_font_cache: dict[int, object] = {}


def _draw_centred(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill) -> None:
    """以 ``xy`` 为中心绘制文字。

    ``anchor="mm"`` 只有 TrueType 字体支持，而 ``_load_font`` 在没有安装
    字体时会回退到 Pillow 默认位图字体——在光秃秃的 Windows 测试机上
    是可能的。用实测尺寸计算偏移，两种方式都有效。
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
    except Exception:  # pragma: no cover - 部分 Pillow 的默认字体没有 getbbox
        return 16


def encode_jpeg(img: Image.Image, quality: int = 85, max_side: int = 1568) -> bytes:
    """先降采样再 JPEG 编码。``max_side`` 取代 LightGUIAgent 写死的
    ``target_size: 1568 # Claude optimal``（config.py:91），改为可配置——
    GLM-V 的最优值是 9/19 A/B 测试要回答的开放问题。"""
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
