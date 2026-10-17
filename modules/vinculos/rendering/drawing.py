from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw
from core.logger import log_exception



@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def size(self) -> tuple[int, int]:
        return (self.w, self.h)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    scale = 3
    mask = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0] * scale, size[1] * scale), radius=radius * scale, fill=255)
    return mask.resize(size, Image.Resampling.LANCZOS)


def load_rgba_from_bytes(image_bytes: bytes | None) -> Image.Image | None:
    if not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return image.convert("RGBA")
    except Exception as exc:
        log_exception(exc)
        return None
