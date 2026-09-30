"""Encode unannotated screenshots for the visual decision and reflection paths."""

from __future__ import annotations

import io

from PIL import Image


def encode_jpeg(img: Image.Image, quality: int = 85, max_side: int = 1568) -> bytes:
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
