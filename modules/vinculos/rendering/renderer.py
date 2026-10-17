from __future__ import annotations

import asyncio
import io
import logging
import random
import unicodedata
from dataclasses import dataclass
from threading import Lock

import discord
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from modules.vinculos.rendering.drawing import (
    Rect,
    load_rgba_from_bytes,
    rounded_mask,
)
from modules.vinculos.rendering.fonts import FontManager
from core.logger import log_exception



LOGGER = logging.getLogger("baphomet.vinculos.renderer")

VINCULO_CARD_FILENAME = "vinculo_selado.png"

Color = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class VinculoParticipantRenderData:
    display_name: str
    avatar_bytes: bytes | None
    fallback_initials: str


class VinculoCardRenderer:
    """Renderiza o card publico de vinculo sem acoplar Pillow ao Cog."""

    CANVAS_SIZE = (1600, 800)
    PANEL = Rect(0, 80, 1600, 640)
    AVATAR_SIZE = 490
    AVATAR_RADIUS = 86
    AVATAR_Y = 105
    LEFT_NAME_BOX = Rect(80, 610, 640, 94)
    RIGHT_NAME_BOX = Rect(880, 610, 640, 94)
    PLUS_CENTER = (800, 400)
    PLUS_ARM = 66
    PLUS_STROKE = 22

    def __init__(self, *, fonts: FontManager | None = None) -> None:
        self.fonts = fonts or FontManager()
        self._font_lock = Lock()

    async def render(
        self,
        *,
        participant_a: discord.Member | discord.User | None,
        participant_b: discord.Member | discord.User | None,
        accent: Color,
        fallback_name_a: str = "Usuario 1",
        fallback_name_b: str = "Usuario 2",
        is_fused: bool = False,
    ) -> io.BytesIO:
        avatar_a, avatar_b = await asyncio.gather(
            self._read_avatar_bytes(participant_a),
            self._read_avatar_bytes(participant_b),
        )
        data_a = VinculoParticipantRenderData(
            display_name=self._display_name(participant_a, fallback_name_a),
            avatar_bytes=avatar_a,
            fallback_initials=self._initials(self._display_name(participant_a, fallback_name_a)),
        )
        data_b = VinculoParticipantRenderData(
            display_name=self._display_name(participant_b, fallback_name_b),
            avatar_bytes=avatar_b,
            fallback_initials=self._initials(self._display_name(participant_b, fallback_name_b)),
        )
        self._warm_fonts()
        output = await asyncio.to_thread(self._render_sync, data_a, data_b, accent)
        if is_fused:
            output = await asyncio.to_thread(self._render_fused_overlay_sync, output)
        return output

    async def render_anniversary(
        self,
        *,
        participant_a: discord.Member | discord.User | None,
        participant_b: discord.Member | discord.User | None,
        accent: Color,
        time_text: str,
        fallback_name_a: str = "Usuario 1",
        fallback_name_b: str = "Usuario 2",
        is_fused: bool = False,
    ) -> io.BytesIO:
        avatar_a, avatar_b = await asyncio.gather(
            self._read_avatar_bytes(participant_a),
            self._read_avatar_bytes(participant_b),
        )
        data_a = VinculoParticipantRenderData(
            display_name=self._display_name(participant_a, fallback_name_a),
            avatar_bytes=avatar_a,
            fallback_initials=self._initials(self._display_name(participant_a, fallback_name_a)),
        )
        data_b = VinculoParticipantRenderData(
            display_name=self._display_name(participant_b, fallback_name_b),
            avatar_bytes=avatar_b,
            fallback_initials=self._initials(self._display_name(participant_b, fallback_name_b)),
        )
        self._warm_fonts()
        output = await asyncio.to_thread(self._render_anniversary_sync, data_a, data_b, accent, time_text)
        if is_fused:
            output = await asyncio.to_thread(self._render_fused_overlay_sync, output)
        return output

    async def render_vinculo_anniversary_card(
        self,
        *,
        participant_a: discord.Member | discord.User | None,
        participant_b: discord.Member | discord.User | None,
        accent: Color,
        time_text: str,
        fallback_name_a: str = "Usuario 1",
        fallback_name_b: str = "Usuario 2",
        is_fused: bool = False,
    ) -> io.BytesIO:
        avatar_a, avatar_b = await asyncio.gather(
            self._read_avatar_bytes(participant_a),
            self._read_avatar_bytes(participant_b),
        )
        data_a = VinculoParticipantRenderData(
            display_name=self._display_name(participant_a, fallback_name_a),
            avatar_bytes=avatar_a,
            fallback_initials=self._initials(self._display_name(participant_a, fallback_name_a)),
        )
        data_b = VinculoParticipantRenderData(
            display_name=self._display_name(participant_b, fallback_name_b),
            avatar_bytes=avatar_b,
            fallback_initials=self._initials(self._display_name(participant_b, fallback_name_b)),
        )
        self._warm_fonts()
        output = await asyncio.to_thread(self._render_vinculo_anniversary_card_sync, data_a, data_b, accent, time_text)
        if is_fused:
            output = await asyncio.to_thread(self._render_fused_overlay_sync, output)
        return output

    async def _read_avatar_bytes(self, participant: discord.Member | discord.User | None) -> bytes | None:
        if participant is None:
            return None
        asset = getattr(participant, "display_avatar", None)
        if asset is None:
            return None
        try:
            return await asset.replace(format="png", size=512).read()
        except (discord.HTTPException, TypeError, ValueError, OSError):
            LOGGER.warning("falha ao ler avatar para card de vinculo user_id=%s", getattr(participant, "id", None))
            return None

    def _render_sync(
        self,
        participant_a: VinculoParticipantRenderData,
        participant_b: VinculoParticipantRenderData,
        accent: Color,
    ) -> io.BytesIO:
        width, height = self.CANVAS_SIZE
        accent = self._normalize_accent(accent)
        canvas = self._draw_background(width, height)
        self._draw_panel(canvas, self.PANEL, accent)

        draw = ImageDraw.Draw(canvas)
        self._draw_participant(canvas, participant_a, self._avatar_rect("left"), self.LEFT_NAME_BOX, accent)
        self._draw_participant(canvas, participant_b, self._avatar_rect("right"), self.RIGHT_NAME_BOX, accent)
        self._draw_center_mark(canvas, self.PLUS_CENTER)
        self._draw_panel_separators(draw, width)

        output = io.BytesIO()
        canvas.convert("RGBA").save(output, format="PNG")
        output.seek(0)
        return output

    def _render_fused_overlay_sync(self, base_output: io.BytesIO) -> io.BytesIO:
        """Applies the dark red soul fusion overlay asynchronously via thread dispatch."""
        base_output.seek(0)
        base_image = Image.open(base_output).convert("RGBA")
        width, height = base_image.size
        
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        rng = random.Random()
        
        # Draw irregular dark flame polygons along the edges
        for _ in range(120):
            x1 = rng.randint(-100, width + 100)
            y1 = rng.choice([rng.randint(-50, 150), rng.randint(height - 150, height + 50)])
            size = rng.randint(20, 100)
            x2 = x1 + rng.randint(-size, size)
            y2 = y1 + rng.randint(-size, size)
            x3 = x1 + rng.randint(-size, size)
            y3 = y1 + rng.randint(-size, size)
            shade = rng.randint(100, 180)
            alpha = rng.randint(20, 70)
            draw.polygon([(x1, y1), (x2, y2), (x3, y3)], fill=(shade, 0, 0, alpha))
            
        overlay = overlay.filter(ImageFilter.GaussianBlur(10))
        final_image = Image.alpha_composite(base_image, overlay)
        
        result = io.BytesIO()
        final_image.save(result, format="PNG")
        result.seek(0)
        return result

    def _render_anniversary_sync(
        self,
        participant_a: VinculoParticipantRenderData,
        participant_b: VinculoParticipantRenderData,
        accent: Color,
        time_text: str,
    ) -> io.BytesIO:
        width, height = self.CANVAS_SIZE
        accent = self._normalize_accent(accent)
        canvas = self._draw_background(width, height)
        self._draw_panel(canvas, self.PANEL, accent)

        draw = ImageDraw.Draw(canvas)
        self._draw_participant(canvas, participant_a, self._avatar_rect("left"), self.LEFT_NAME_BOX, accent)
        self._draw_participant(canvas, participant_b, self._avatar_rect("right"), self.RIGHT_NAME_BOX, accent)
        self._draw_center_mark(canvas, self.PLUS_CENTER)
        self._draw_panel_separators(draw, width)

        # Draw Anniversary Banner/Text at the top-center (y = 40)
        banner_y = 55
        font = self._font(64, "bold")
        text_bbox = self._text_bbox(draw, time_text, font)
        text_w = self._box_width(text_bbox)
        text_h = self._box_height(text_bbox)

        text_x = (width - text_w) // 2

        # Draw a subtle background for the text
        padding_x = 40
        padding_y = 20
        banner_rect = (text_x - padding_x, banner_y - padding_y, text_x + text_w + padding_x, banner_y + text_h + padding_y)
        draw.rounded_rectangle(banner_rect, radius=20, fill=(20, 20, 20, 240), outline=(255, 215, 0, 180), width=3)

        self._safe_draw_text(
            draw,
            (text_x, banner_y - text_bbox[1]),
            time_text,
            font=font,
            fill=(255, 215, 0, 255) # Gold text
        )

        output = io.BytesIO()
        canvas.convert("RGBA").save(output, format="PNG")
        output.seek(0)
        return output

    def _render_vinculo_anniversary_card_sync(
        self,
        participant_a: VinculoParticipantRenderData,
        participant_b: VinculoParticipantRenderData,
        accent: Color,
        time_text: str,
    ) -> io.BytesIO:
        width, height = self.CANVAS_SIZE
        accent = self._normalize_accent(accent)
        canvas = self._draw_background(width, height)
        self._draw_panel(canvas, self.PANEL, accent)

        draw = ImageDraw.Draw(canvas)
        self._draw_participant(canvas, participant_a, self._avatar_rect("left"), self.LEFT_NAME_BOX, accent)
        self._draw_participant(canvas, participant_b, self._avatar_rect("right"), self.RIGHT_NAME_BOX, accent)
        self._draw_center_mark(canvas, self.PLUS_CENTER)
        self._draw_panel_separators(draw, width)

        # Usar Montserrat-Black para um estilo marcante e premium de aniversário
        font = self._font(70, "montserrat")
        text_bbox = self._text_bbox(draw, time_text, font)
        text_w = self._box_width(text_bbox)
        text_h = self._box_height(text_bbox)

        text_x = (width - text_w) // 2
        banner_y = 52

        # Desenhar um background com borda dourada elegante (glassmorphism/glow effect)
        padding_x = 50
        padding_y = 22
        banner_rect = (
            text_x - padding_x,
            banner_y - padding_y,
            text_x + text_w + padding_x,
            banner_y + text_h + padding_y,
        )
        
        # Sombra sob o banner
        shadow_offset = 4
        shadow_rect = (
            banner_rect[0] + shadow_offset,
            banner_rect[1] + shadow_offset,
            banner_rect[2] + shadow_offset,
            banner_rect[3] + shadow_offset,
        )
        draw.rounded_rectangle(shadow_rect, radius=24, fill=(0, 0, 0, 150))
        
        # Banner de fundo semi-transparente
        draw.rounded_rectangle(
            banner_rect,
            radius=24,
            fill=(15, 15, 15, 230),
            outline=(255, 215, 0, 200),
            width=4,
        )

        # Detalhe fino de borda interna
        inner_border_rect = (
            banner_rect[0] + 6,
            banner_rect[1] + 6,
            banner_rect[2] - 6,
            banner_rect[3] - 6,
        )
        draw.rounded_rectangle(
            inner_border_rect,
            radius=18,
            outline=(218, 165, 32, 80),
            width=2,
        )

        # Texto do aniversário
        self._safe_draw_text(
            draw,
            (text_x, banner_y - text_bbox[1]),
            time_text,
            font=font,
            fill=(255, 215, 0, 255),
        )

        output = io.BytesIO()
        canvas.convert("RGBA").save(output, format="PNG")
        output.seek(0)
        return output

    def _draw_background(self, width: int, height: int) -> Image.Image:
        canvas = Image.new("RGBA", (width, height), (8, 8, 8, 255))
        self._draw_outer_band(canvas, Rect(0, 0, width, self.PANEL.y), seed=481)
        self._draw_outer_band(canvas, Rect(0, self.PANEL.bottom, width, height - self.PANEL.bottom), seed=917)
        return canvas

    def _draw_panel(self, canvas: Image.Image, panel: Rect, accent: Color) -> None:
        panel_fill = self._vertical_gradient(panel.size, (42, 42, 42, 255), (54, 54, 54, 255))
        panel_fill = Image.alpha_composite(panel_fill, self._noise_layer(panel.size, seed=113, opacity=7, scale=5))
        canvas.alpha_composite(panel_fill, (panel.x, panel.y))

        draw = ImageDraw.Draw(canvas)
        draw.rectangle(panel.box, outline=(14, 14, 14, 255), width=2)
        draw.line((panel.x, panel.y, panel.right, panel.y), fill=(2, 2, 2, 255), width=6)
        draw.line((panel.x, panel.bottom - 1, panel.right, panel.bottom - 1), fill=(2, 2, 2, 255), width=6)
        draw.line((panel.x, panel.y + 8, panel.right, panel.y + 8), fill=(*accent, 48), width=1)
        draw.line((panel.x, panel.bottom - 10, panel.right, panel.bottom - 10), fill=(255, 255, 255, 18), width=1)

    def _draw_panel_separators(self, draw: ImageDraw.ImageDraw, width: int) -> None:
        draw.line((0, self.PANEL.y, width, self.PANEL.y), fill=(0, 0, 0, 255), width=5)
        draw.line((0, self.PANEL.bottom, width, self.PANEL.bottom), fill=(0, 0, 0, 255), width=5)

    def _draw_center_mark(self, canvas: Image.Image, center: tuple[int, int]) -> None:
        cx, cy = center
        draw = ImageDraw.Draw(canvas)
        arm = self.PLUS_ARM
        stroke = self.PLUS_STROKE
        draw.line((cx, cy - arm, cx, cy + arm), fill=(235, 235, 235, 245), width=stroke)
        draw.line((cx - arm, cy, cx + arm, cy), fill=(235, 235, 235, 245), width=stroke)

    def _draw_participant(
        self,
        canvas: Image.Image,
        participant: VinculoParticipantRenderData,
        avatar_rect: Rect,
        name_box: Rect,
        accent: Color,
    ) -> None:
        draw = ImageDraw.Draw(canvas)

        self._draw_avatar_shadow(canvas, avatar_rect)
        draw.rounded_rectangle(
            (avatar_rect.x - 8, avatar_rect.y - 8, avatar_rect.right + 8, avatar_rect.bottom + 8),
            radius=self.AVATAR_RADIUS + 8,
            fill=(18, 18, 18, 255),
            outline=(3, 3, 3, 255),
            width=3,
        )

        avatar = self._avatar_image(participant, avatar_rect.w, self.AVATAR_RADIUS, accent)
        canvas.alpha_composite(avatar, (avatar_rect.x, avatar_rect.y))

        draw.rounded_rectangle(
            avatar_rect.box,
            radius=self.AVATAR_RADIUS,
            outline=(238, 238, 238, 82),
            width=4,
        )
        draw.rounded_rectangle(
            (avatar_rect.x + 5, avatar_rect.y + 5, avatar_rect.right - 5, avatar_rect.bottom - 5),
            radius=max(1, self.AVATAR_RADIUS - 6),
            outline=(*accent, 45),
            width=2,
        )

        name, font = self._fit_text(draw, participant.display_name, name_box.w, start_size=86, min_size=44, weight="bold")
        bbox = self._text_bbox(draw, name, font)
        text_x = name_box.x + (name_box.w - self._box_width(bbox)) // 2
        text_y = name_box.y + (name_box.h - self._box_height(bbox)) // 2 - bbox[1]
        self._safe_draw_text(
            draw,
            (text_x, text_y),
            name,
            font=font,
            fill=(238, 238, 238, 255),
        )

    def _avatar_image(
        self,
        participant: VinculoParticipantRenderData,
        size: int,
        radius: int,
        accent: Color,
    ) -> Image.Image:
        source = load_rgba_from_bytes(participant.avatar_bytes)
        if source is None:
            return self._avatar_placeholder(size, radius, participant.fallback_initials, accent)

        fitted = ImageOps.fit(source.convert("RGBA"), (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        avatar = fitted
        mask = rounded_mask((size, size), radius)
        avatar.putalpha(mask)
        return avatar

    def _avatar_placeholder(self, size: int, radius: int, initials: str, accent: Color) -> Image.Image:
        canvas = self._vertical_gradient((size, size), (28, 28, 28, 255), (46, 46, 46, 255))
        canvas = Image.alpha_composite(canvas, self._noise_layer((size, size), seed=37 + size, opacity=9, scale=4))
        mask = rounded_mask((size, size), radius)
        canvas.putalpha(mask)

        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            (size * 0.18, size * 0.18, size * 0.82, size * 0.82),
            radius=max(12, radius // 2),
            outline=(*accent, 95),
            width=max(3, size // 65),
        )
        text = initials[:2].upper() or "?"
        font = self._font(max(38, size // 3), "display")
        bbox = self._text_bbox(draw, text, font)
        self._safe_draw_text(
            draw,
            ((size - self._box_width(bbox)) // 2, (size - self._box_height(bbox)) // 2 - bbox[1]),
            text,
            font=font,
            fill=(238, 238, 238, 255),
        )
        return canvas

    def _fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        *,
        start_size: int,
        min_size: int,
        weight: str,
    ) -> tuple[str, ImageFont.ImageFont]:
        clean = self._clean_name(text)
        for size in range(start_size, min_size - 1, -2):
            font = self._font(size, weight)
            if self._text_width(draw, clean, font) <= max_width:
                return clean, font
        font = self._font(min_size, weight)
        return self._truncate_to_width(draw, clean, font, max_width), font

    def _truncate_to_width(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
        if self._text_width(draw, text, font) <= max_width:
            return text
        suffix = "..."
        left, right = 0, len(text)
        best = suffix
        while left <= right:
            mid = (left + right) // 2
            candidate = text[:mid].rstrip()
            if not candidate:
                right = mid - 1
                continue
            candidate = f"{candidate}{suffix}"
            if self._text_width(draw, candidate, font) <= max_width:
                best = candidate
                left = mid + 1
            else:
                right = mid - 1
        if self._text_width(draw, best, font) > max_width:
            return text[: max(1, len(text) // 2)].rstrip()
        return best

    def _font(self, size: int, weight: str = "regular") -> ImageFont.ImageFont:
        try:
            with self._font_lock:
                return self.fonts.font(size, weight)
        except Exception as exc:
            log_exception(exc)
            try:
                return ImageFont.load_default(size=size)
            except TypeError:
                return ImageFont.load_default()

    def _warm_fonts(self) -> None:
        for size in range(86, 43, -2):
            self._font(size, "bold")
        self._font(max(38, self.AVATAR_SIZE // 3), "display")

    def _avatar_rect(self, side: str) -> Rect:
        center_x = self.CANVAS_SIZE[0] // 4 if side == "left" else self.CANVAS_SIZE[0] * 3 // 4
        return Rect(center_x - self.AVATAR_SIZE // 2, self.AVATAR_Y, self.AVATAR_SIZE, self.AVATAR_SIZE)

    def _draw_avatar_shadow(self, canvas: Image.Image, rect: Rect) -> None:
        blur = 26
        spread = 14
        shadow_size = (rect.w + spread * 2 + blur * 4, rect.h + spread * 2 + blur * 4)
        shadow = Image.new("RGBA", shadow_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(shadow)
        inner = Rect(blur * 2, blur * 2, rect.w + spread * 2, rect.h + spread * 2)
        draw.rounded_rectangle(inner.box, radius=self.AVATAR_RADIUS + spread, fill=(0, 0, 0, 115))
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
        canvas.alpha_composite(shadow, (rect.x - spread - blur * 2, rect.y - spread - blur * 2 + 14))

    def _draw_outer_band(self, canvas: Image.Image, band: Rect, *, seed: int) -> None:
        if band.h <= 0:
            return
        texture = Image.new("RGBA", band.size, (8, 8, 8, 255))
        texture = Image.alpha_composite(texture, self._noise_layer(band.size, seed=seed, opacity=30, scale=3))

        scratches = Image.new("RGBA", band.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(scratches)
        rng = random.Random(seed)
        for _ in range(52):
            y = rng.randint(-10, max(1, band.h + 10))
            x = rng.randint(-80, max(1, band.w - 20))
            length = rng.randint(90, 320)
            shade = rng.randint(105, 180)
            alpha = rng.randint(10, 34)
            draw.line((x, y, x + length, y + rng.randint(-12, 12)), fill=(shade, shade, shade, alpha), width=rng.randint(1, 3))
        scratches = scratches.filter(ImageFilter.GaussianBlur(0.45))
        texture = Image.alpha_composite(texture, scratches)

        fade = Image.new("L", band.size, 0)
        fade_draw = ImageDraw.Draw(fade)
        for y in range(band.h):
            edge_distance = min(y, band.h - 1 - y)
            alpha = int(255 * min(1.0, edge_distance / max(1, band.h * 0.35)))
            fade_draw.line((0, y, band.w, y), fill=max(90, alpha))
        texture.putalpha(fade)
        canvas.alpha_composite(texture, (band.x, band.y))

    @staticmethod
    def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int, int], bottom: tuple[int, int, int, int]) -> Image.Image:
        width, height = size
        image = Image.new("RGBA", size, top)
        draw = ImageDraw.Draw(image)
        denominator = max(1, height - 1)
        for y in range(height):
            ratio = y / denominator
            color = tuple(int(top[index] + (bottom[index] - top[index]) * ratio) for index in range(4))
            draw.line((0, y, width, y), fill=color)
        return image

    @staticmethod
    def _noise_layer(size: tuple[int, int], *, seed: int, opacity: int, scale: int) -> Image.Image:
        width, height = size
        small = (max(1, width // scale), max(1, height // scale))
        rng = random.Random(seed)
        noise = Image.new("L", small)
        noise.putdata([rng.randrange(256) for _ in range(small[0] * small[1])])
        noise = noise.filter(ImageFilter.GaussianBlur(0.6)).resize(size, Image.Resampling.BICUBIC)
        alpha = noise.point(lambda value: int(abs(value - 128) * opacity / 128))
        light = Image.new("RGBA", size, (255, 255, 255, 0))
        dark = Image.new("RGBA", size, (0, 0, 0, 0))
        light.putalpha(alpha.point(lambda value: value // 2))
        dark.putalpha(alpha.point(lambda value: value // 3))
        return Image.alpha_composite(dark, light)

    @staticmethod
    def _display_name(participant: discord.Member | discord.User | None, fallback: str) -> str:
        if participant is None:
            return fallback
        value = getattr(participant, "display_name", None) or getattr(participant, "global_name", None)
        return str(value or getattr(participant, "name", None) or fallback)

    @staticmethod
    def _clean_name(value: str) -> str:
        chars: list[str] = []
        for char in unicodedata.normalize("NFC", str(value)):
            category = unicodedata.category(char)
            if category.startswith("C"):
                continue
            if category in {"So", "Sk"} and ord(char) >= 0x2600:
                continue
            chars.append(char)
        text = " ".join("".join(chars).split())
        return text or "Alma sem nome"

    @classmethod
    def _initials(cls, value: str) -> str:
        clean = cls._clean_name(value)
        pieces = ["".join(char for char in part if char.isalnum()) for part in clean.split()]
        letters = [piece[0] for piece in pieces if piece]
        if len(letters) >= 2:
            return "".join(letters[:2])
        if letters:
            return letters[0]
        return "?"

    @staticmethod
    def _normalize_accent(accent: Color) -> Color:
        if len(accent) != 3:
            return (140, 140, 140)
        red, green, blue = (max(0, min(255, int(channel))) for channel in accent)
        gray = int(red * 0.299 + green * 0.587 + blue * 0.114)
        gray = max(105, min(170, gray))
        return (gray, gray, gray)

    @staticmethod
    def _to_grayscale(image: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        gray = ImageOps.grayscale(rgba)
        gray = ImageOps.autocontrast(gray, cutoff=1)
        gray = ImageEnhance.Contrast(gray).enhance(1.08)
        return Image.merge("RGBA", (gray, gray, gray, alpha))

    @classmethod
    def _text_bbox(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
    ) -> tuple[int, int, int, int]:
        try:
            return draw.textbbox((0, 0), text, font=font)
        except UnicodeError:
            safe = cls._safe_latin_text(text)
            return draw.textbbox((0, 0), safe, font=font)

    @classmethod
    def _text_width(cls, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> float:
        bbox = cls._text_bbox(draw, text, font)
        return float(cls._box_width(bbox))

    @classmethod
    def _safe_draw_text(
        cls,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        *,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
    ) -> None:
        try:
            draw.text(xy, text, font=font, fill=fill)
        except UnicodeError:
            draw.text(xy, cls._safe_latin_text(text), font=font, fill=fill)

    @staticmethod
    def _safe_latin_text(text: str) -> str:
        return text.encode("latin-1", "ignore").decode("latin-1", "ignore") or "?"

    @staticmethod
    def _box_width(box: tuple[int, int, int, int]) -> int:
        return box[2] - box[0]

    @staticmethod
    def _box_height(box: tuple[int, int, int, int]) -> int:
        return box[3] - box[1]
