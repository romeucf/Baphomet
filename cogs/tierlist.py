from __future__ import annotations

import asyncio
import io
import logging
import math
import pathlib
import re
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Literal
from typing import OrderedDict as OrderedDictType
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from modules.integrations.wikipedia import (
    WIKIPEDIA_SOURCE_TYPE,
    WikipediaImageService,
    WikipediaPageImageCandidate,
    WikipediaResolution,
    WikipediaResolvedImage,
    WikipediaUserError,
)
from modules.integrations.spotify import (    SpotifyImageDownloader,
    SpotifyImageError,
    SpotifyImageProcessor,
    SpotifyInputResolver,
    SpotifyResolution,
    SpotifyResolvedItem,
    SpotifyService,
    SpotifyUserError,
)
from core.logger import log_exception
LOGGER = logging.getLogger("baphomet.tierlist")


# Evita que imagens absurdamente gigantes tentem explodir a memória do bot.
Image.MAX_IMAGE_PIXELS = 25_000_000


def normalize_caption(value: object, *, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if text.casefold() in {"none", "null"}:
        return None
    if max_length is not None:
        text = text[:max_length].strip()
    return text or None


# ============================================================
# MODELOS DE DADOS
# ============================================================

@dataclass
class TierItem:
    """
    Representa um item da Tier List.

    name:
        Nome/legenda do item.

    image_url:
        URL opcional enviada pelo usuário.

    image_bytes:
        Bytes baixados da imagem.
        Esse campo é preenchido apenas na hora de gerar a imagem final.
        Se for None, o renderer só desenha texto quando houver render_caption.
    """

    name: str
    image_url: str | None = None
    image_bytes: bytes | None = None
    source_type: str = "text"
    caption: str | None = None
    user_caption: str | None = None
    render_caption: str | None = None
    has_visible_caption: bool = False
    internal_title: str | None = None
    source_query: str | None = None
    image_cache_key: str | None = None
    spotify_type: str | None = None
    spotify_id: str | None = None
    spotify_url: str | None = None
    spotify_name: str | None = None
    spotify_artists: tuple[str, ...] = field(default_factory=tuple)
    album_name: str | None = None
    track_name: str | None = None
    release_date: str | None = None
    attribution_text: str | None = None
    display_name: str | None = None
    image_url_used: str | None = None
    wiki_language: str | None = None
    wikipedia_pageid: int | None = None
    wikipedia_title: str | None = None
    wikipedia_url: str | None = None
    wikimedia_file_title: str | None = None
    wikimedia_file_description_url: str | None = None
    image_mime: str | None = None
    artist: str | None = None
    credit: str | None = None
    license_short_name: str | None = None
    license_url: str | None = None
    usage_terms: str | None = None
    attribution_required: str | None = None
    metadata_source: str | None = None


@dataclass
class TierListSession:
    """
    Estado temporário de uma Tier List em criação.
    Fica em RAM e é apagado ao gerar, cancelar ou expirar.
    """

    owner_id: int
    title: str
    tiers: list[str] = field(default_factory=lambda: ["S", "A", "B", "C", "D"])
    items: OrderedDictType[str, list[TierItem]] = field(
        default_factory=lambda: OrderedDict(
            {
                "S": [],
                "A": [],
                "B": [],
                "C": [],
                "D": [],
            }
        )
    )
    tier_colors: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    panel_message: discord.Message | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)


# ============================================================
# PARTE 1 — PILLOW RENDERER
# ============================================================

class TierListRenderer:
    """
    Motor visual Sleek Modern Minimalist para Tier Lists do Baphomet.

    Esta classe trata exclusivamente do canvas Pillow: layout, mascaras,
    sombras suaves e desenho final. A ingestao de imagens por URL, Discord ID
    ou Wikipedia continua fora daqui e chega como `TierItem.image_bytes`.
    """

    CANVAS_BG = (17, 17, 25, 255)       # #111119
    ROW_BG = (29, 29, 37, 255)          # #1D1D25
    ITEM_BG = (48, 55, 70, 255)         # #303746
    ITEM_OUTLINE = (74, 82, 111, 255)   # #4A526F
    TEXT = (246, 247, 250, 255)
    TEXT_DARK = (17, 17, 25, 255)
    TEXT_MUTED = (174, 183, 196, 255)
    FOOTER_LINE = (61, 62, 70, 255)

    # Cores extraidas do mock anexado.
    TIER_COLORS = [
        (255, 67, 88),
        (255, 164, 5),
        (236, 208, 101),
        (115, 229, 146),
        (107, 155, 242),
        (153, 126, 246),
        (235, 111, 184),
        (86, 190, 205),
    ]

    CANVAS_WIDTH = 800
    GRID_X = 40
    GRID_RIGHT = 760
    TITLE_TOP = 28
    ROWS_TOP = 160
    ROW_HEIGHT_BASE = 111
    ROW_GAP = 11
    ROW_RADIUS = 18
    ROW_PADDING_TOP = 21
    ROW_PADDING_BOTTOM = 20
    LABEL_WIDTH = 162
    LABEL_TO_ITEMS_GAP = 18
    MAX_ITEMS_PER_ROW = 8
    ITEM_WIDTH = 160
    ITEM_HEIGHT = 70
    ITEM_RADIUS = 12
    ITEM_GAP_X = 12
    ITEM_GAP_Y = 12
    ITEM_THUMB_SIZE = 48
    TEXT_ITEM_PADDING_X = 48
    TEXT_ITEM_PADDING_Y = 18
    TEXT_ITEM_LINE_GAP = 4
    MAX_TEXT_ITEM_WIDTH = 250
    TEXT_WRAP_CHARS = 25
    IMAGE_ITEM_SIZE = 160
    TEXT_ITEM_HEIGHT = IMAGE_ITEM_SIZE
    IMAGE_ITEM_RADIUS = 18
    IMAGE_CAPTION_HEIGHT = 46
    SPOTIFY_CAPTION_HEIGHT = 52
    SPOTIFY_ITEM_HEIGHT = IMAGE_ITEM_SIZE + SPOTIFY_CAPTION_HEIGHT
    SPOTIFY_ART_PADDING = 8
    WIKIPEDIA_CAPTION_HEIGHT = 48
    WIKIPEDIA_ITEM_HEIGHT = IMAGE_ITEM_SIZE + WIKIPEDIA_CAPTION_HEIGHT
    WIKIPEDIA_ART_PADDING = 8
    FOOTER_TOP_GAP = 50
    FOOTER_AVATAR_TOP_GAP = 18
    FOOTER_AVATAR_SIZE = 63
    FOOTER_TEXT_X = 129
    FOOTER_BOTTOM = 30

    def __init__(self, font_path: str | None = None) -> None:
        self.font_path = font_path
        self._font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        self._mask_cache: dict[tuple, Image.Image] = {}
        self._warned_font_fallback = False

    # ============================================================
    #  FONTES E MEDIDAS DE TEXTO
    # ============================================================

    def _font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """
        Carrega uma fonte sem serifa moderna via ImageFont.truetype().

        O cache evita reabrir arquivos TTF em cada card. Se as fontes do projeto
        nao existirem no host, a renderizacao continua com fallback do Pillow e
        emite um aviso unico no terminal.
        """
        cache_key = (size, bold)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached

        repo_root = pathlib.Path(__file__).resolve().parents[1]
        font_dir = repo_root / "assets" / "fonts"
        candidates: list[str] = []

        if self.font_path:
            candidates.append(self.font_path)

        if bold:
            candidates.extend([
                str(font_dir / "Poppins-Bold.ttf"),
                str(font_dir / "Montserrat-Black.ttf"),
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "arialbd.ttf",
            ])
        else:
            candidates.extend([
                str(font_dir / "Poppins-Regular.ttf"),
                str(font_dir / "Poppins-Bold.ttf"),
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "arial.ttf",
            ])

        for candidate in candidates:
            try:
                font = ImageFont.truetype(candidate, size)
                self._font_cache[cache_key] = font
                return font
            except Exception as exc:
                log_exception(exc)
                continue

        if not self._warned_font_fallback:
            print("[AVISO] Fonte premium nao encontrada. Usando fallback padrao do Pillow.")
            self._warned_font_fallback = True

        font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font

    def _text_bbox(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> tuple[int, int, int, int]:
        return draw.textbbox((0, 0), text, font=font)

    def _text_size(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> tuple[int, int]:
        left, top, right, bottom = self._text_bbox(draw, text, font)
        return right - left, bottom - top

    def _center_text_position(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> tuple[int, int]:
        """Centraliza texto compensando bearings reais medidos por textbbox()."""
        x1, y1, x2, y2 = box
        left, top, right, bottom = self._text_bbox(draw, text, font)
        text_w = right - left
        text_h = bottom - top
        x = x1 + ((x2 - x1) - text_w) / 2 - left
        y = y1 + ((y2 - y1) - text_h) / 2 - top
        return int(x), int(y)

    def _fit_font(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        *,
        start_size: int,
        max_width: int,
        min_size: int,
        bold: bool,
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Diminui a fonte ate o texto caber na largura disponivel."""
        for size in range(start_size, min_size - 1, -1):
            font = self._font(size, bold=bold)
            if self._text_size(draw, text, font)[0] <= max_width:
                return font
        return self._font(min_size, bold=bold)

    def _clip_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> str:
        """Corta textos longos com reticencias ASCII sem quebrar o chip."""
        value = re.sub(r"\s+", " ", (text or "Item").strip()) or "Item"
        if self._text_size(draw, value, font)[0] <= max_width:
            return value

        suffix = "..."
        while value and self._text_size(draw, value + suffix, font)[0] > max_width:
            value = value[:-1]
        return value + suffix if value else suffix

    def _draw_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        pos: tuple[int, int],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
        *,
        shadow_alpha: int = 80,
    ) -> None:
        """Texto flat com uma sombra minima para legibilidade em dark mode."""
        x, y = pos
        if shadow_alpha > 0:
            draw.text((x, y + 2), text, font=font, fill=(0, 0, 0, shadow_alpha))
        draw.text((x, y), text, font=font, fill=fill)

    def _create_background(self, width: int, height: int) -> Image.Image:
        """Cria o fundo solido do mock 1:1, sem gradientes ou textura."""
        return Image.new("RGBA", (width, height), self.CANVAS_BG)

    # ============================================================
    #  MASCARAS PILL-SHAPED E SOMBRAS DIFUSAS
    # ============================================================

    def _pill_mask(self, size: tuple[int, int]) -> Image.Image:
        """
        Cria mascara de pilula perfeita.

        Regra geometrica: radius = height / 2. A mascara e desenhada em 3x e
        reduzida com LANCZOS; isso deixa as laterais semicirculares sem serrilha.
        """
        width, height = size
        key = ("pill", width, height)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached.copy()

        scale = 3
        hi_w, hi_h = width * scale, height * scale
        radius = hi_h // 2
        mask_hi = Image.new("L", (hi_w, hi_h), 0)
        ImageDraw.Draw(mask_hi).rounded_rectangle(
            (0, 0, hi_w - 1, hi_h - 1),
            radius=radius,
            fill=255,
        )
        mask = mask_hi.resize((width, height), Image.Resampling.LANCZOS)
        self._mask_cache[key] = mask
        return mask.copy()

    def _circle_mask(self, diameter: int) -> Image.Image:
        """Mascara circular perfeita para avatares/imagens dentro dos chips."""
        key = ("circle", diameter, diameter)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached.copy()

        scale = 3
        hi = diameter * scale
        mask_hi = Image.new("L", (hi, hi), 0)
        ImageDraw.Draw(mask_hi).ellipse((0, 0, hi - 1, hi - 1), fill=255)
        mask = mask_hi.resize((diameter, diameter), Image.Resampling.LANCZOS)
        self._mask_cache[key] = mask
        return mask.copy()

    def _rounded_mask(self, size: tuple[int, int], radius: int) -> Image.Image:
        """Mascara anti-aliased para cards arredondados do item."""
        width, height = size
        key = ("round", width, height, radius)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached.copy()

        scale = 3
        hi_w, hi_h = width * scale, height * scale
        mask_hi = Image.new("L", (hi_w, hi_h), 0)
        ImageDraw.Draw(mask_hi).rounded_rectangle(
            (0, 0, hi_w - 1, hi_h - 1),
            radius=radius * scale,
            fill=255,
        )
        mask = mask_hi.resize((width, height), Image.Resampling.LANCZOS)
        self._mask_cache[key] = mask
        return mask.copy()

    def _left_round_mask(self, size: tuple[int, int], radius: int) -> Image.Image:
        """Mascara com apenas os cantos esquerdos arredondados, igual ao label."""
        width, height = size
        key = ("left_round", width, height, radius)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached.copy()

        scale = 3
        hi_w, hi_h = width * scale, height * scale
        hi_r = radius * scale
        mask_hi = Image.new("L", (hi_w, hi_h), 0)
        draw = ImageDraw.Draw(mask_hi)
        draw.rectangle((hi_r, 0, hi_w, hi_h), fill=255)
        draw.rectangle((0, hi_r, hi_r, hi_h - hi_r), fill=255)
        draw.pieslice((0, 0, hi_r * 2, hi_r * 2), 180, 270, fill=255)
        draw.pieslice((0, hi_h - hi_r * 2, hi_r * 2, hi_h), 90, 180, fill=255)
        mask = mask_hi.resize((width, height), Image.Resampling.LANCZOS)
        self._mask_cache[key] = mask
        return mask.copy()

    def _draw_soft_shadow(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
        *,
        radius: int,
        offset: tuple[int, int] = (0, 10),
        blur: int = 28,
        opacity: int = 54,
    ) -> None:
        """
        Sombra moderna: alta dispersao, baixa opacidade.

        A profundidade do layout vem daqui. A sombra e renderizada em layer
        propria, borrada com GaussianBlur e composta antes do elemento principal.
        """
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        local_w = width + blur * 2 + abs(offset[0])
        local_h = height + blur * 2 + abs(offset[1])
        layer = Image.new("RGBA", (local_w, local_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        sx = blur + max(0, -offset[0])
        sy = blur + max(0, -offset[1])
        draw.rounded_rectangle(
            (sx, sy, sx + width, sy + height),
            radius=radius,
            fill=(0, 0, 0, opacity),
        )
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
        base.alpha_composite(
            layer,
            (x1 + offset[0] - blur - max(0, -offset[0]), y1 + offset[1] - blur - max(0, -offset[1])),
        )

    def _draw_item_shadow(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
    ) -> None:
        """
        Sombra especifica dos itens, com offset Y positivo e blur 5.

        O item permanece visualmente elevado sobre o trilho, mas a sombra fica
        contida e organica: uma pilula preta translúcida, deslocada para baixo,
        borrada por GaussianBlur(radius=5) antes da colagem do chip real.
        """
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        blur = 5
        offset_y = 8
        pad = blur * 3
        layer = Image.new("RGBA", (width + pad * 2, height + pad * 2 + offset_y), (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(layer, "RGBA")
        shadow_draw.rounded_rectangle(
            (pad, pad + offset_y, pad + width, pad + offset_y + height),
            radius=height // 2,
            fill=(0, 0, 0, 72),
        )
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
        base.alpha_composite(layer, (x1 - pad, y1 - pad))

    def _paste_pill(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
        fill: tuple[int, int, int, int],
        *,
        outline: tuple[int, int, int, int] | None = None,
        outline_width: int = 1,
    ) -> None:
        """Desenha uma pilula flat usando mascara alpha de radius = height / 2."""
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        layer = Image.new("RGBA", (width, height), fill)
        mask = self._pill_mask((width, height))
        layer.putalpha(mask)

        if outline is not None and outline_width > 0:
            draw = ImageDraw.Draw(layer, "RGBA")
            draw.rounded_rectangle(
                (outline_width // 2, outline_width // 2, width - 1 - outline_width // 2, height - 1 - outline_width // 2),
                radius=height // 2,
                outline=outline,
                width=outline_width,
            )

        base.paste(layer, (x1, y1), mask=layer)

    def _paste_rounded_rect(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
        fill: tuple[int, int, int, int],
        *,
        radius: int,
        outline: tuple[int, int, int, int] | None = None,
        outline_width: int = 1,
    ) -> None:
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        mask = self._rounded_mask((width, height), radius)
        layer = Image.new("RGBA", (width, height), fill)
        layer.putalpha(mask)

        if outline is not None and outline_width > 0:
            draw = ImageDraw.Draw(layer, "RGBA")
            draw.rounded_rectangle(
                (0, 0, width - 1, height - 1),
                radius=radius,
                outline=outline,
                width=outline_width,
            )

        base.paste(layer, (x1, y1), mask=layer)

    def _paste_left_round_rect(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
        fill: tuple[int, int, int, int],
        *,
        radius: int,
    ) -> None:
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        mask = self._left_round_mask((width, height), radius)
        layer = Image.new("RGBA", (width, height), fill)
        layer.putalpha(mask)
        base.paste(layer, (x1, y1), mask=layer)

    # ============================================================
    #  IMAGENS E CHIPS DE ITENS
    # ============================================================

    def _safe_open_image(self, image_bytes: bytes | None, item_name: str) -> Image.Image:
        """
        Abre bytes em RGBA com tolerancia a formatos e ponteiros internos.

        BytesIO.seek(0), raw.seek(0) e convert("RGBA") preservam o conserto de
        fotos parcialmente lidas, GIF/WebP no frame errado e imagens sem alpha.
        """
        if not image_bytes:
            raise ValueError("item sem bytes de imagem")

        buffer = io.BytesIO(image_bytes)
        buffer.seek(0)
        try:
            with Image.open(buffer) as raw:
                try:
                    raw.seek(0)
                except Exception as exc:
                    log_exception(exc)
                    pass
                return raw.convert("RGBA")
        except Exception as exc:
            log_exception(exc)
            raise ValueError(f"imagem corrompida/ilegivel para {item_name}: {exc}") from exc

    def _item_has_image(self, item: "TierItem") -> bool:
        """A imagem altera o formato visual do item e entra no dry-run."""
        return bool(getattr(item, "image_bytes", None))

    def _is_spotify_item(self, item: "TierItem") -> bool:
        return getattr(item, "source_type", "") == "spotify" or bool(getattr(item, "spotify_id", None))

    def _is_wikipedia_item(self, item: "TierItem") -> bool:
        return getattr(item, "source_type", "") == WIKIPEDIA_SOURCE_TYPE

    def _item_display_name(self, item: "TierItem") -> str:
        """
        Texto visual do card.

        Metadados resolvidos de Spotify/Wikipedia ficam fora daqui de proposito:
        so texto digitado pelo usuario deve aparecer como legenda renderizada.
        """
        if self._item_has_visual_source(item):
            if not getattr(item, "has_visible_caption", False):
                return ""
            return normalize_caption(getattr(item, "render_caption", None)) or ""

        return normalize_caption(getattr(item, "render_caption", None)) or normalize_caption(getattr(item, "name", None)) or ""

    def _item_has_visible_caption(self, item: "TierItem") -> bool:
        return bool(getattr(item, "has_visible_caption", False) and self._item_display_name(item))

    def _item_has_visual_source(self, item: "TierItem") -> bool:
        return bool(
            getattr(item, "image_url", None)
            or getattr(item, "image_bytes", None)
            or self._is_spotify_item(item)
            or self._is_wikipedia_item(item)
        )

    def _image_caption_height(self, item: "TierItem") -> int:
        if not self._item_has_visible_caption(item):
            return 0
        if self._is_spotify_item(item):
            return self.SPOTIFY_CAPTION_HEIGHT
        if self._is_wikipedia_item(item):
            return self.WIKIPEDIA_CAPTION_HEIGHT
        return self.IMAGE_CAPTION_HEIGHT

    def _image_item_height(self, item: "TierItem") -> int:
        return self.IMAGE_ITEM_SIZE + self._image_caption_height(item)

    def _fit_image_cover(self, image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
        target_w, target_h = target_size
        if target_w <= 0 or target_h <= 0:
            raise ValueError("target de imagem inválido")

        source = image.convert("RGBA")
        src_w, src_h = source.size
        if src_w <= 0 or src_h <= 0:
            raise ValueError("imagem sem dimensões válidas")

        scale = max(target_w / src_w, target_h / src_h)
        resized_w = max(target_w, math.ceil(src_w * scale))
        resized_h = max(target_h, math.ceil(src_h * scale))
        resized = source.resize((resized_w, resized_h), Image.Resampling.LANCZOS)

        left = max(0, (resized_w - target_w) // 2)
        top = max(0, (resized_h - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))

    def _apply_rounded_alpha(self, image: Image.Image, radius: int) -> Image.Image:
        rounded = image.convert("RGBA")
        mask = self._rounded_mask(rounded.size, radius)
        alpha = rounded.getchannel("A")
        rounded.putalpha(ImageChops.multiply(alpha, mask))
        return rounded

    def _paste_cover_image(
        self,
        card: Image.Image,
        raw: Image.Image,
        box: tuple[int, int, int, int],
    ) -> None:
        x1, y1, x2, y2 = box
        fitted = self._fit_image_cover(raw, (x2 - x1, y2 - y1))
        card.paste(fitted, (x1, y1), mask=fitted)

    def _coerce_rgb_color(
        self,
        color: object,
        fallback: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        """
        Normaliza cores vindas do estado da sessão antes do ImageDraw.

        A UI valida hexadecimal, mas esta camada defensiva impede que um estado
        antigo/malformado gere ValueError no Pillow. Qualquer entrada que nao
        tenha tres canais inteiros no intervalo 0..255 cai para a cor padrao da
        tier, preservando a entrega da imagem final.
        """
        try:
            channels = tuple(int(channel) for channel in color[:3])  # type: ignore[index]
        except Exception as exc:
            log_exception(exc)
            return fallback

        if len(channels) != 3:
            return fallback

        if any(channel < 0 or channel > 255 for channel in channels):
            return fallback

        return channels

    def _wrap_text_lines(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        """
        Quebra texto textual por caracteres e por pixels, nessa ordem.

        `textwrap.wrap()` faz a primeira interceptacao tipografica com limite de
        caracteres, evitando frases infinitas em uma unica linha. Em seguida,
        cada linha candidata e validada com a largura real em pixels medida pelo
        Pillow; se uma palavra ou trecho ainda ultrapassar a largura util, o
        fallback quebra em caracteres para preservar 100% do conteudo dentro do
        limite visual do card.
        """
        value = re.sub(r"\s+", " ", (text or "").strip())
        if not value:
            return []

        lines: list[str] = []
        wrapped_candidates = textwrap.wrap(
            value,
            width=self.TEXT_WRAP_CHARS,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [value]

        for candidate_line in wrapped_candidates:
            words = candidate_line.split()
            current = ""

            for word in words:
                candidate = f"{current} {word}".strip()
                if self._text_size(draw, candidate, font)[0] <= max_width:
                    current = candidate
                    continue

                if current:
                    lines.append(current)
                    current = ""

                if self._text_size(draw, word, font)[0] <= max_width:
                    current = word
                    continue

                chunk = ""
                for char in word:
                    candidate = chunk + char
                    if chunk and self._text_size(draw, candidate, font)[0] > max_width:
                        lines.append(chunk)
                        chunk = char
                    else:
                        chunk = candidate
                current = chunk

            if current:
                lines.append(current)

        return lines

    def _measure_text_item(
        self,
        draw: ImageDraw.ImageDraw,
        item: "TierItem",
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        *,
        max_width: int,
    ) -> dict:
        """
        Mede um item textual antes do desenho real.

        A largura nunca ultrapassa `MAX_TEXT_ITEM_WIDTH`; o conteudo excedente
        vira texto multilinha. A altura do card nasce no tamanho de uma imagem
        para manter alinhamento com cards quadrados, mas cresce quando o bloco
        multilinha + padding vertical exige mais espaco. O raio do desenho sera
        recalculado no paint como `height // 2`, preservando a geometria de
        pilula mesmo quando a caixa fica mais alta.
        """
        display_name = self._item_display_name(item)
        if not display_name:
            return {"width": 0, "height": 0, "lines": [], "text": ""}

        box_max_w = max(
            self.TEXT_ITEM_HEIGHT,
            min(self.MAX_TEXT_ITEM_WIDTH, max_width),
        )
        text_max_w = max(1, box_max_w - self.TEXT_ITEM_PADDING_X * 2)
        lines = self._wrap_text_lines(draw, display_name, font, text_max_w) or [display_name]
        multiline_text = "\n".join(lines)
        left, top, right, bottom = draw.multiline_textbbox(
            (0, 0),
            multiline_text,
            font=font,
            spacing=self.TEXT_ITEM_LINE_GAP,
            align="center",
        )
        text_w = right - left
        text_h = bottom - top

        width = min(
            box_max_w,
            max(self.TEXT_ITEM_HEIGHT, text_w + self.TEXT_ITEM_PADDING_X * 2),
        )
        height = max(
            self.TEXT_ITEM_HEIGHT,
            text_h + self.TEXT_ITEM_PADDING_Y * 2,
        )
        return {
            "width": int(width),
            "height": int(height),
            "lines": lines,
            "text": multiline_text,
        }

    def _draw_image_inside_chip(
        self,
        chip: Image.Image,
        item: "TierItem",
        *,
        tier_color: tuple[int, int, int],
    ) -> bool:
        """Recorta imagem em card arredondado e cola dentro do item."""
        image_x = 11
        image_y = (self.ITEM_HEIGHT - self.ITEM_THUMB_SIZE) // 2

        try:
            raw = self._safe_open_image(item.image_bytes, item.name)
            fitted = self._fit_image_cover(raw, (self.ITEM_THUMB_SIZE, self.ITEM_THUMB_SIZE))
        except Exception as exc:
            log_exception(exc)
            print(f"[ERRO] Fallback visual para item '{item.name}': {exc}")
            return False

        fitted = self._apply_rounded_alpha(fitted, 9)
        chip.paste(fitted, (image_x, image_y), mask=fitted)
        return True

    def _draw_image_square_item(
        self,
        base: Image.Image,
        item: "TierItem",
        box: tuple[int, int, int, int],
        *,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        """Desenha item com imagem em dois layouts reais: image-only ou imagem + legenda."""
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        caption = self._item_display_name(item)
        footer_h = self._image_caption_height(item)
        image_h = max(1, height - footer_h)
        image_box = (0, 0, width, image_h)
        card = Image.new("RGBA", (width, height), self.ITEM_BG)
        draw = ImageDraw.Draw(card, "RGBA")

        try:
            raw = self._safe_open_image(item.image_bytes, self._item_display_name(item) or "item com imagem")
            self._paste_cover_image(card, raw, image_box)
        except Exception as exc:
            log_exception(exc)
            print(f"[ERRO] Fallback visual para item '{self._item_display_name(item)}': {exc}")

        if caption:
            caption_box = (0, image_h, width, height)
            draw.rectangle(caption_box, fill=(29, 29, 37, 255))
            caption_font = self._fit_font(
                draw,
                caption,
                start_size=getattr(font, "size", 22),
                max_width=width - 18,
                min_size=12,
                bold=True,
            )
            clipped = self._clip_text(draw, caption, caption_font, width - 18)
            tx, ty = self._center_text_position(draw, caption_box, clipped, caption_font)
            self._draw_text(draw, clipped, (tx, ty), caption_font, self.TEXT, shadow_alpha=120)

        card = self._apply_rounded_alpha(card, self.IMAGE_ITEM_RADIUS)
        draw = ImageDraw.Draw(card, "RGBA")
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=self.IMAGE_ITEM_RADIUS,
            outline=self.ITEM_OUTLINE,
            width=1,
        )
        base.paste(card, (x1, y1), mask=card)

    def _draw_spotify_square_item(
        self,
        base: Image.Image,
        item: "TierItem",
        box: tuple[int, int, int, int],
        *,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        """Desenha capa Spotify usando o mesmo layout/crop dos demais cards de imagem."""
        self._draw_image_square_item(base, item, box, font=font)

    def _draw_wikipedia_square_item(
        self,
        base: Image.Image,
        item: "TierItem",
        box: tuple[int, int, int, int],
        *,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        """Desenha imagem Wikipedia aprovada usando cover crop, sem padding/letterbox."""
        self._draw_image_square_item(base, item, box, font=font)

    def _draw_item_chip(
        self,
        base: Image.Image,
        item: "TierItem",
        box: tuple[int, int, int, int],
        *,
        tier_color: tuple[int, int, int],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        """Desenha o card de item 160x70 do mock, com borda azulada sutil."""
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1

        if self._item_has_image(item):
            if self._is_spotify_item(item):
                self._draw_spotify_square_item(base, item, box, font=font)
                return
            if self._is_wikipedia_item(item):
                self._draw_wikipedia_square_item(base, item, box, font=font)
                return
            self._draw_image_square_item(base, item, box, font=font)
            return

        display_name = self._item_display_name(item)
        if not display_name:
            return

        measure_draw = ImageDraw.Draw(base, "RGBA")
        text_max_w = max(1, width - self.TEXT_ITEM_PADDING_X * 2)
        lines = self._wrap_text_lines(measure_draw, display_name, font, text_max_w) or [display_name]
        multiline_text = "\n".join(lines)
        left, top, right, bottom = measure_draw.multiline_textbbox(
            (0, 0),
            multiline_text,
            font=font,
            spacing=self.TEXT_ITEM_LINE_GAP,
            align="center",
        )
        text_w = right - left
        text_h = bottom - top
        radius = max(1, height // 2)

        chip = Image.new("RGBA", (width, height), self.ITEM_BG)
        chip.putalpha(self._rounded_mask((width, height), radius))
        draw = ImageDraw.Draw(chip, "RGBA")
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=radius,
            outline=self.ITEM_OUTLINE,
            width=1,
        )

        tx = (width - text_w) / 2 - left
        ty = (height - text_h) / 2 - top
        draw.multiline_text(
            (tx, ty),
            multiline_text,
            font=font,
            fill=self.TEXT,
            spacing=self.TEXT_ITEM_LINE_GAP,
            align="center",
        )

        base.paste(chip, (x1, y1), mask=chip)

    # ============================================================
    #  LINHAS, LABELS E RODAPE
    # ============================================================

    def _draw_row_container(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
    ) -> None:
        """Desenha o trilho escuro com raio fixo igual ao mock."""
        self._paste_rounded_rect(base, box, self.ROW_BG, radius=self.ROW_RADIUS)

    def _draw_tier_label(
        self,
        base: Image.Image,
        row_box: tuple[int, int, int, int],
        tier_name: str,
        tier_color: tuple[int, int, int],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        """Label colorido com cantos esquerdos arredondados e direita reta."""
        row_x1, row_y1, _, row_y2 = row_box
        label_box = (row_x1, row_y1, row_x1 + self.LABEL_WIDTH, row_y2)
        self._paste_left_round_rect(base, label_box, (*tier_color, 255), radius=self.ROW_RADIUS)

        draw = ImageDraw.Draw(base, "RGBA")
        label_text = re.sub(r"\s+", "", (tier_name or "?").strip())[:3] or "?"
        label_font = font
        while self._text_size(draw, label_text, label_font)[0] > self.LABEL_WIDTH - 24 and getattr(label_font, "size", 18) > 18:
            label_font = self._font(getattr(label_font, "size", 42) - 2, bold=True)

        tx, ty = self._center_text_position(draw, label_box, label_text, label_font)
        self._draw_text(draw, label_text, (tx, ty), label_font, self.TEXT_DARK, shadow_alpha=0)

    def _draw_empty_state(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> None:
        """Tier vazia permanece visualmente limpa, sem texto placeholder."""
        return

    def _resolve_author_name(self, author: object | None, creator_name: str) -> str:
        """Extrai exatamente o nome de usuario da interacao quando disponivel."""
        raw_name = ""
        if author is not None:
            raw_name = str(getattr(author, "name", "") or "")
        if not raw_name:
            raw_name = str(creator_name or "usuario")
        raw_name = raw_name.strip().lstrip("@") or "usuario"
        return re.sub(r"\s+", "_", raw_name)[:40]

    def _draw_footer(
        self,
        base: Image.Image,
        *,
        usuario_autor: str,
        avatar_bytes: bytes | None,
    ) -> None:
        """Desenha o footer do mock: linha, avatar circular e assinatura."""
        draw = ImageDraw.Draw(base, "RGBA")
        brt_tz = timezone(timedelta(hours=-3))
        data_atual = datetime.now(brt_tz).strftime("%d/%m/%Y às %H:%M")
        footer_text = f"Gerado por Baphomet  •  Criado por @{usuario_autor}  •  {data_atual}"
        line_y = base.height - self.FOOTER_BOTTOM - self.FOOTER_AVATAR_SIZE - self.FOOTER_AVATAR_TOP_GAP
        avatar_y = line_y + self.FOOTER_AVATAR_TOP_GAP
        avatar_box = (self.GRID_X, avatar_y, self.GRID_X + self.FOOTER_AVATAR_SIZE, avatar_y + self.FOOTER_AVATAR_SIZE)

        content_right = base.width - self.GRID_X
        draw.line((self.GRID_X, line_y, content_right, line_y), fill=self.FOOTER_LINE, width=2)
        self._draw_footer_avatar(base, avatar_box, avatar_bytes=avatar_bytes, usuario_autor=usuario_autor)

        footer_box = (
            self.FOOTER_TEXT_X,
            avatar_y,
            content_right,
            avatar_y + self.FOOTER_AVATAR_SIZE,
        )
        footer_font = self._fit_font(
            draw,
            footer_text,
            start_size=18,
            max_width=footer_box[2] - footer_box[0],
            min_size=12,
            bold=True,
        )
        _, ty = self._center_text_position(draw, footer_box, footer_text, footer_font)
        tx = footer_box[0]
        self._draw_text(draw, footer_text, (tx, ty), footer_font, self.TEXT_MUTED, shadow_alpha=25)

    def _draw_footer_avatar(
        self,
        base: Image.Image,
        box: tuple[int, int, int, int],
        *,
        avatar_bytes: bytes | None,
        usuario_autor: str,
    ) -> None:
        """Usa bytes ja recebidos pela chamada ou gera fallback local, sem rede."""
        x1, y1, x2, y2 = box
        size = x2 - x1
        mask = self._circle_mask(size)

        if avatar_bytes:
            try:
                raw = self._safe_open_image(avatar_bytes, "avatar do autor")
                avatar = ImageOps.fit(raw, (size, size), method=Image.Resampling.LANCZOS)
                avatar.putalpha(mask)
                base.paste(avatar, (x1, y1), mask=avatar)
                return
            except Exception as exc:
                log_exception(exc)
                pass

        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        avatar_draw = ImageDraw.Draw(layer, "RGBA")
        avatar_draw.ellipse((0, 0, size - 1, size - 1), fill=(70, 76, 92, 255))
        avatar_draw.ellipse((4, 4, size - 5, size - 5), fill=(106, 115, 137, 255))
        initial = (usuario_autor[:1] or "B").upper()
        font = self._font(26, bold=True)
        tx, ty = self._center_text_position(avatar_draw, (0, 0, size, size), initial, font)
        self._draw_text(avatar_draw, initial, (tx, ty), font, self.TEXT, shadow_alpha=0)
        layer.putalpha(mask)
        base.paste(layer, (x1, y1), mask=layer)

    # ============================================================
    #  LAYOUT RESPONSIVO E RENDERIZACAO FINAL
    # ============================================================

    def calculate_tierlist_dimensions(
        self,
        tiers_dict: dict,
        tier_colors: dict[str, tuple[int, int, int]] | None = None,
        min_width: int = 800,
        max_width: int = 2400,
    ) -> dict:
        """
        Calcula a matriz visual antes de instanciar Image.new().

        1. Mantem largura 800px, igual ao mock anexado.
        2. Calcula a capacidade real da matriz de itens:
           floor((largura_util + gap_x) / (item_w + gap_x)).
        3. Limita a capacidade por MAX_ITEMS_PER_ROW, mantendo controle do wrap.
        4. Expande cada tier por line_count = ceil(total_itens / itens_por_linha).
        5. Acumula Y sequencialmente: row_y_atual += row_height + row_gap.
        """
        tier_colors = tier_colors or {}
        canvas_w = max(min_width, min(max_width, self.CANVAS_WIDTH))
        items_x = self.GRID_X + self.LABEL_WIDTH + self.LABEL_TO_ITEMS_GAP
        scratch = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        scratch_draw = ImageDraw.Draw(scratch, "RGBA")
        item_font = self._font(22, bold=True)

        # Primeira passada: mede cards de texto ja com Word Wrap aplicado.
        # A largura textual e limitada por MAX_TEXT_ITEM_WIDTH; portanto frases
        # longas aumentam a altura do card, nao a largura infinita do canvas.
        max_single_item_w = self.IMAGE_ITEM_SIZE
        preliminary_area_w = max(1, (canvas_w - self.GRID_X - 22) - items_x)
        for items in tiers_dict.values():
            for item in items:
                if self._item_has_image(item):
                    max_single_item_w = max(max_single_item_w, self.IMAGE_ITEM_SIZE)
                else:
                    measurement = self._measure_text_item(
                        scratch_draw,
                        item,
                        item_font,
                        max_width=preliminary_area_w,
                    )
                    max_single_item_w = max(max_single_item_w, measurement["width"])

        required_canvas_w = items_x + max(preliminary_area_w, max_single_item_w) + 22 + self.GRID_X
        canvas_w = max(min_width, min(max_width, required_canvas_w))
        grid_right = canvas_w - self.GRID_X
        items_right = grid_right - 22
        items_area_w = max(1, items_right - items_x)

        row_layouts: list[dict] = []
        current_y = self.ROWS_TOP
        for index, (tier_name, items) in enumerate(tiers_dict.items()):
            item_layouts: list[dict] = []
            line_heights: list[int] = []
            current_line = 0
            cursor_x = 0

            for item in items:
                if self._item_has_image(item):
                    item_w = self.IMAGE_ITEM_SIZE
                    item_h = self._image_item_height(item)
                else:
                    measurement = self._measure_text_item(
                        scratch_draw,
                        item,
                        item_font,
                        max_width=items_area_w,
                    )
                    item_w = measurement["width"]
                    item_h = measurement["height"]

                if item_w <= 0 or item_h <= 0:
                    continue

                # Packing horizontal responsivo:
                # - X usa o acumulador da linha atual;
                # - se o proximo card ultrapassar a area util, ocorre wrap;
                # - line_heights guarda o maior item da linha para centralizar
                #   imagens e textos multilinha no mesmo eixo vertical.
                projected_x = cursor_x + (self.ITEM_GAP_X if cursor_x else 0) + item_w
                if cursor_x and projected_x > items_area_w:
                    current_line += 1
                    cursor_x = 0

                item_x_offset = cursor_x
                if len(line_heights) <= current_line:
                    line_heights.append(item_h)
                else:
                    line_heights[current_line] = max(line_heights[current_line], item_h)

                item_layouts.append(
                    {
                        "item": item,
                        "line": current_line,
                        "x_offset": item_x_offset,
                        "width": item_w,
                        "height": item_h,
                    }
                )
                cursor_x += item_w + self.ITEM_GAP_X

            if not line_heights:
                line_heights = [self.ITEM_HEIGHT]

            line_count = len(line_heights)
            line_offsets: list[int] = []
            accumulated_y = 0
            for line_height in line_heights:
                line_offsets.append(accumulated_y)
                accumulated_y += line_height + self.ITEM_GAP_Y

            dynamic_items_h = sum(line_heights) + max(0, line_count - 1) * self.ITEM_GAP_Y

            # A tier agora responde diretamente a altura do conteudo:
            # - texto puro fica na altura base do layout;
            # - imagens quadradas removem o padding vertical artificial;
            # - multiplas linhas somam apenas conteudo + gaps reais.
            row_h = max(self.ROW_HEIGHT_BASE, dynamic_items_h)
            content_y_offset = (row_h - dynamic_items_h) // 2

            default_color = self.TIER_COLORS[index % len(self.TIER_COLORS)]
            row_color = self._coerce_rgb_color(tier_colors.get(tier_name), default_color)
            row_box = (self.GRID_X, current_y, grid_right, current_y + row_h)
            row_layouts.append({
                "tier": tier_name,
                "items": items,
                "item_layouts": item_layouts,
                "color": row_color,
                "line_count": line_count,
                "line_heights": line_heights,
                "line_offsets": line_offsets,
                "content_y_offset": content_y_offset,
                "row_height": row_h,
                "row_y": current_y,
                "row_box": row_box,
            })
            current_y += row_h + self.ROW_GAP

        rows_end = current_y - self.ROW_GAP if row_layouts else self.ROWS_TOP
        footer_line_y = rows_end + self.FOOTER_TOP_GAP
        canvas_h = footer_line_y + self.FOOTER_AVATAR_TOP_GAP + self.FOOTER_AVATAR_SIZE + self.FOOTER_BOTTOM

        return {
            "canvas_w": int(canvas_w),
            "canvas_h": int(canvas_h),
            "row_layouts": row_layouts,
            "grid_right": grid_right,
            "items_x": items_x,
            "items_right": items_right,
        }

    def generate_tierlist_image(
        self,
        title: str,
        tiers_dict: dict,
        *,
        author: object | None = None,
        creator_name: str = "",
        guild_icon_bytes: bytes | None = None,
        tier_colors: dict[str, tuple[int, int, int]] | None = None,
    ) -> io.BytesIO:
        """
        Gera a imagem final em PNG.

        `guild_icon_bytes` e usado apenas como imagem ja pronta para o avatar
        circular do footer. Nenhum download e feito aqui.
        """
        usuario_autor = self._resolve_author_name(author, creator_name)
        layout = self.calculate_tierlist_dimensions(tiers_dict, tier_colors=tier_colors)
        canvas_w = layout["canvas_w"]
        canvas_h = layout["canvas_h"]
        grid_right = layout["grid_right"]

        image = self._create_background(canvas_w, canvas_h)
        draw = ImageDraw.Draw(image, "RGBA")

        title_text = re.sub(r"\s+", " ", (title or "Tier List").strip()) or "Tier List"
        title_box = (
            self.GRID_X,
            self.TITLE_TOP,
            grid_right,
            self.ROWS_TOP - 58,
        )
        title_font = self._fit_font(
            draw,
            title_text,
            start_size=50,
            max_width=title_box[2] - title_box[0],
            min_size=28,
            bold=True,
        )
        tx, ty = self._center_text_position(draw, title_box, title_text, title_font)
        self._draw_text(draw, title_text, (tx, ty), title_font, self.TEXT, shadow_alpha=0)

        tier_font = self._font(48, bold=True)
        item_font = self._font(22, bold=True)
        empty_font = self._font(21, bold=True)

        for row in layout["row_layouts"]:
            row_box = row["row_box"]
            self._draw_row_container(image, row_box)
            self._draw_tier_label(image, row_box, row["tier"], row["color"], tier_font)

            items_x = layout["items_x"]
            items_y = row_box[1] + row["content_y_offset"]

            if not row["item_layouts"]:
                self._draw_empty_state(
                    image,
                    (items_x, row_box[1], row_box[2], row_box[3]),
                    empty_font,
                )
            else:
                for item_layout in row["item_layouts"]:
                    item = item_layout["item"]
                    line = item_layout["line"]
                    item_x = items_x + item_layout["x_offset"]
                    item_w = item_layout["width"]
                    item_h = item_layout["height"]
                    line_h = row["line_heights"][line]
                    item_y = items_y + row["line_offsets"][line] + (line_h - item_h) // 2
                    self._draw_item_chip(
                        image,
                        item,
                        (item_x, item_y, item_x + item_w, item_y + item_h),
                        tier_color=row["color"],
                        font=item_font,
                    )

        self._draw_footer(image, usuario_autor=usuario_autor, avatar_bytes=guild_icon_bytes)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

# ============================================================
# PARTE 2 — DISCORD.PY: MODALS, SELECTS, VIEWS E SESSÃO
# ============================================================

class ConfigureTiersModal(discord.ui.Modal):
    def __init__(self, cog: TierListCog, owner_id: int) -> None:
        super().__init__(title="📝 Configurar Tiers")

        self.cog = cog
        self.owner_id = owner_id

        self.tiers_input = discord.ui.TextInput(
            label="Tiers separadas por vírgula",
            placeholder="Exemplo: S, A, B, C, D",
            default="S, A, B, C, D",
            min_length=1,
            max_length=250,
            required=True,
        )

        self.add_item(self.tiers_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        parsed = self.cog.parse_tiers(str(self.tiers_input.value))

        if not parsed:
            await interaction.response.send_message(
                "⚠️ Você precisa informar pelo menos uma tier válida.",
                ephemeral=True,
            )
            return

        if len(parsed) > 25:
            await interaction.response.send_message(
                "⚠️ Use no máximo **25 tiers**, pois esse é o limite do Select Menu do Discord.",
                ephemeral=True,
            )
            return

        old_items = session.items
        new_items: OrderedDictType[str, list[TierItem]] = OrderedDict(
            (tier, []) for tier in parsed
        )

        # Preserva itens das tiers que ainda existem.
        for tier in parsed:
            if tier in old_items:
                new_items[tier].extend(old_items[tier])

        # Se uma tier antiga foi removida, os itens dela vão para a primeira tier nova.
        # Assim o usuário não perde dados silenciosamente.
        removed_items: list[TierItem] = []

        for old_tier, items in old_items.items():
            if old_tier not in new_items:
                removed_items.extend(items)

        if removed_items:
            new_items[parsed[0]].extend(removed_items)

        session.tiers = parsed
        session.items = new_items
        session.tier_colors = {
            tier: session.tier_colors[tier]
            for tier in parsed
            if tier in session.tier_colors
        }

        await self.cog.refresh_panel(session)

        await interaction.response.send_message(
            f"✅ Tiers configuradas: **{', '.join(parsed)}**",
            ephemeral=True,
        )


class AddItemModal(discord.ui.Modal):
    def __init__(self, cog: TierListCog, owner_id: int) -> None:
        super().__init__(title="➕ Adicionar Item")

        self.cog = cog
        self.owner_id = owner_id

        self.item_name = discord.ui.TextInput(
            label="Nome do item",
            placeholder="Opcional: Pizza, Minecraft, 21 Savage...",
            min_length=0,
            max_length=25,
            required=False,
        )

        self.image_url = discord.ui.TextInput(
            label="URL da imagem",
            placeholder="Opcional: https://exemplo.com/imagem.png",
            min_length=0,
            max_length=500,
            required=False,
        )

        self.user_id_input = discord.ui.TextInput(
            label="Foto de Usuário (ID)",
            placeholder="ID do usuário: 123456789012345678",
            min_length=0,
            max_length=25,
            required=False,
        )

        self.web_search_input = discord.ui.TextInput(
            label="Pesquisa de imagem na web",
            placeholder="Ex: Billie Eilish, Minecraft, Brasil, Charizard",
            min_length=0,
            max_length=100,
            required=False,
        )
        self.spotify_album_input = discord.ui.TextInput(
            label="Spotify (Álbum)",
            placeholder="Ex: https://open.spotify.com/album/... ou Brat - Charli xcx",
            min_length=0,
            max_length=200,
            required=False,
        )

        self.add_item(self.item_name)
        self.add_item(self.image_url)
        self.add_item(self.user_id_input)
        self.add_item(self.web_search_input)
        self.add_item(self.spotify_album_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Prevenção Absoluta de Timeouts: Avisa ao Discord que o processamento será longo
        # OBRIGATÓRIO DEFER ANTES DE TUDO
        await interaction.response.defer(ephemeral=True, thinking=True)

        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.followup.send(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        if not session.tiers:
            await interaction.followup.send(
                "⚠️ Configure as tiers antes de adicionar itens.",
                ephemeral=True,
            )
            return

        total_items = sum(len(items) for items in session.items.values())

        if total_items >= self.cog.MAX_ITEMS_PER_SESSION:
            await interaction.followup.send(
                f"⚠️ Limite de **{self.cog.MAX_ITEMS_PER_SESSION} itens** atingido nessa tier list.",
                ephemeral=True,
            )
            return

        raw_item_name = str(self.item_name.value).strip()
        clean_item = self.cog.clean_text(raw_item_name, max_length=25)
        render_caption = self.cog.render_caption_from_user(raw_item_name, max_length=25)
        clean_url = str(self.image_url.value).strip()
        user_id_str = str(self.user_id_input.value).strip()
        wikipedia_search_str = str(self.web_search_input.value).strip()
        spotify_album_str = str(self.spotify_album_input.value).strip()

        filled_sources = self.cog.get_filled_image_sources(
            image_url=clean_url,
            avatar_user_id=user_id_str,
            wikipedia=wikipedia_search_str,
            spotify=spotify_album_str,
        )
        if len(filled_sources) > 1:
            await interaction.followup.send(
                self.cog.conflicting_image_sources_message(filled_sources),
                ephemeral=True,
            )
            return

        if not filled_sources and not render_caption:
            await interaction.followup.send(
                self.cog.empty_item_message(),
                ephemeral=True,
            )
            return

        selected_source = filled_sources[0]["key"] if filled_sources else ""

        if selected_source == "spotify":
            try:
                resolution = await self.cog.resolve_spotify_input(spotify_album_str)
            except SpotifyUserError as exc:
                LOGGER.exception("Falha amigável ao resolver Spotify: %s", exc.code)
                await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
                return
            except Exception as exc:
                log_exception(exc)
                LOGGER.exception("Falha inesperada ao resolver entrada Spotify.")
                await interaction.followup.send(
                    "❌ Não consegui consultar o Spotify agora. Tente novamente em instantes.",
                    ephemeral=True,
                )
                return

            if resolution.is_ambiguous:
                view = SpotifyCandidateSelectView(
                    cog=self.cog,
                    owner_id=self.owner_id,
                    candidates=resolution.candidates,
                    custom_caption=render_caption or "",
                    source_query=spotify_album_str,
                )
                message = await interaction.followup.send(
                    "🎵 Encontrei mais de um resultado. Escolha o correto no menu abaixo.",
                    view=view,
                    ephemeral=True,
                    wait=True,
                )
                view.message = message
                return

            if resolution.item is None:
                await interaction.followup.send(
                    "❌ Não consegui encontrar esse álbum ou música no Spotify.",
                    ephemeral=True,
                )
                return

            try:
                item = await self.cog.build_spotify_tier_item(
                    resolution.item,
                    custom_caption=render_caption or "",
                    source_query=spotify_album_str,
                )
            except SpotifyUserError as exc:
                LOGGER.exception("Falha amigável ao preparar capa Spotify: %s", exc.code)
                await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
                return
            except Exception as exc:
                log_exception(exc)
                LOGGER.exception("Falha inesperada ao preparar item Spotify.")
                await interaction.followup.send(
                    "❌ A capa desse item não pôde ser baixada com segurança.",
                    ephemeral=True,
                )
                return

            await self.cog.send_tier_choice_for_item(
                interaction,
                owner_id=self.owner_id,
                item=item,
                tiers=session.tiers,
                extra=(
                    "\n🎵 Capa e metadados resolvidos pelo Spotify. "
                    "A legenda ficará fora da arte da capa."
                ),
            )
            return

        if selected_source == "wikipedia":
            try:
                resolution = await self.cog.resolve_wikipedia_input(
                    wikipedia_search_str,
                    guild_id=interaction.guild_id,
                    user_id=interaction.user.id,
                )
            except WikipediaUserError as exc:
                LOGGER.exception("Falha amigável ao resolver Wikipedia: %s", exc.code)
                await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
                return
            except Exception as exc:
                log_exception(exc)
                LOGGER.exception("Falha inesperada ao resolver entrada Wikipedia.")
                await interaction.followup.send(
                    "❌ Não consegui consultar a Wikipedia agora. Tente novamente em instantes.",
                    ephemeral=True,
                )
                return

            if resolution.is_ambiguous:
                view = WikipediaCandidateSelectView(
                    cog=self.cog,
                    owner_id=self.owner_id,
                    candidates=resolution.candidates,
                    custom_caption=render_caption or "",
                    source_query=wikipedia_search_str,
                )
                message = await interaction.followup.send(
                    "🌐 Esse termo é ambíguo. Escolha o artigo correto no menu.",
                    view=view,
                    ephemeral=True,
                    wait=True,
                )
                view.message = message
                return

            if resolution.item is None:
                await interaction.followup.send(
                    "❌ Não encontrei nenhum artigo na Wikipedia para esse termo.",
                    ephemeral=True,
                )
                return

            item = self.cog.build_wikipedia_tier_item(
                resolution.item,
                custom_caption=render_caption or "",
                source_query=wikipedia_search_str,
            )
            await self.cog.send_tier_choice_for_item(
                interaction,
                owner_id=self.owner_id,
                item=item,
                tiers=session.tiers,
                extra=(
                    "\n🌐 Imagem livre resolvida via Wikipedia/Wikimedia. "
                    "A legenda ficará fora da imagem."
                ),
            )
            return

        if selected_source == "image_url":
            if not self.cog.looks_like_url(clean_url):
                await interaction.followup.send("❌ A URL informada não parece válida.", ephemeral=True)
                return

            item = TierItem(
                name=render_caption or "",
                image_url=clean_url,
                image_bytes=None,
                source_type="image_url",
                caption=render_caption,
                user_caption=render_caption,
                render_caption=render_caption,
                has_visible_caption=render_caption is not None,
                internal_title="Imagem por URL",
                source_query=clean_url,
            )
            await self.cog.send_tier_choice_for_item(
                interaction,
                owner_id=self.owner_id,
                item=item,
                tiers=session.tiers,
                extra="\n🖼️ Imagem detectada com sucesso. A legenda só aparece se você preencheu o nome.",
            )
            return

        if selected_source == "avatar_user_id":
            try:
                user_id = int(user_id_str)
                user = await interaction.client.fetch_user(user_id)
                avatar_url = user.display_avatar.replace(format="png", size=256).url
            except (ValueError, discord.NotFound, discord.HTTPException):
                await interaction.followup.send("❌ Não consegui encontrar esse usuário para usar o avatar.", ephemeral=True)
                return

            item = TierItem(
                name=render_caption or "",
                image_url=avatar_url,
                image_bytes=None,
                source_type="image_url",
                caption=render_caption,
                user_caption=render_caption,
                render_caption=render_caption,
                has_visible_caption=render_caption is not None,
                internal_title=f"Avatar de usuário {user_id}",
                source_query=user_id_str,
            )
            await self.cog.send_tier_choice_for_item(
                interaction,
                owner_id=self.owner_id,
                item=item,
                tiers=session.tiers,
                extra="\n🖼️ Avatar detectado com sucesso. A legenda só aparece se você preencheu o nome.",
            )
            return

        item = TierItem(
            name=clean_item,
            image_url=None,
            image_bytes=None,
            source_type="text",
            caption=render_caption,
            user_caption=render_caption,
            render_caption=render_caption,
            has_visible_caption=render_caption is not None,
        )
        await self.cog.send_tier_choice_for_item(
            interaction,
            owner_id=self.owner_id,
            item=item,
            tiers=session.tiers,
        )


class SpotifyCandidateSelect(discord.ui.Select):
    def __init__(self, view_instance: "SpotifyCandidateSelectView") -> None:
        self.view_instance = view_instance

        options: list[discord.SelectOption] = []
        for index, candidate in enumerate(view_instance.candidates[:10]):
            artist_text = ", ".join(candidate.artists) if candidate.artists else "Artista desconhecido"
            year = (candidate.release_date or "")[:4]
            kind = "álbum" if candidate.spotify_type == "album" else "track"
            name = candidate.spotify_name
            description_parts = [artist_text, year, kind]
            description = " • ".join(part for part in description_parts if part)
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(index),
                    description=description[:100],
                    emoji="🎵",
                )
            )

        super().__init__(
            placeholder="Escolha o resultado do Spotify",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view_instance.owner_id:
            await interaction.response.send_message(
                "❌ Só quem criou a tier list pode usar esse menu.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=False)

        session = self.view_instance.cog.sessions.get(self.view_instance.owner_id)
        if session is None:
            await interaction.edit_original_response(
                content="❌ Essa sessão expirou ou foi cancelada.",
                view=None,
            )
            return

        selected_index = int(self.values[0])
        try:
            candidate = self.view_instance.candidates[selected_index]
        except IndexError:
            await interaction.edit_original_response(
                content="❌ Esse resultado não está mais disponível. Tente adicionar de novo.",
                view=None,
            )
            return

        try:
            item = await self.view_instance.cog.build_spotify_tier_item(
                candidate,
                custom_caption=self.view_instance.custom_caption,
                source_query=self.view_instance.source_query,
            )
        except SpotifyUserError as exc:
            LOGGER.exception("Falha amigável ao preparar candidato Spotify: %s", exc.code)
            await interaction.edit_original_response(content=f"❌ {exc.user_message}", view=None)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("Falha inesperada ao preparar candidato Spotify.")
            await interaction.edit_original_response(
                content="❌ A capa desse item não pôde ser baixada com segurança.",
                view=None,
            )
            return

        tier_view = ItemTierSelectView(
            cog=self.view_instance.cog,
            owner_id=self.view_instance.owner_id,
            item=item,
            tiers=session.tiers,
        )
        item_label = item.name or "item com imagem"
        await interaction.edit_original_response(
            content=(
                f"📌 Escolha em qual tier colocar **{discord.utils.escape_markdown(item_label)}**:"
                "\n🎵 Capa e metadados resolvidos pelo Spotify. A legenda ficará fora da arte da capa."
            ),
            view=tier_view,
        )
        self.view_instance.stop()


class SpotifyCandidateSelectView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "TierListCog",
        owner_id: int,
        candidates: list[SpotifyResolvedItem],
        custom_caption: str,
        source_query: str = "",
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.candidates = candidates
        self.custom_caption = custom_caption
        self.source_query = source_query
        self.message: discord.Message | None = None
        self.add_item(SpotifyCandidateSelect(self))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content="⌛ Tempo esgotado. Nenhum item do Spotify foi adicionado.",
                    view=None,
                )
            except discord.HTTPException:
                pass

        self.stop()


class WikipediaCandidateSelect(discord.ui.Select):
    def __init__(self, view_instance: "WikipediaCandidateSelectView") -> None:
        self.view_instance = view_instance

        options: list[discord.SelectOption] = []
        for index, candidate in enumerate(view_instance.candidates[:25]):
            description_parts = []
            if candidate.description:
                description_parts.append(candidate.description)
            description_parts.append(candidate.wiki_language)
            description_parts.append("imagem livre" if candidate.has_image else "sem imagem")
            options.append(
                discord.SelectOption(
                    label=candidate.title[:100],
                    value=str(index),
                    description=" • ".join(description_parts)[:100],
                    emoji="🌐",
                )
            )

        super().__init__(
            placeholder="Escolha o artigo da Wikipedia",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view_instance.owner_id:
            await interaction.response.send_message(
                "❌ Só quem criou a tier list pode usar esse menu.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=False)

        session = self.view_instance.cog.sessions.get(self.view_instance.owner_id)
        if session is None:
            await interaction.edit_original_response(
                content="❌ Essa sessão expirou ou foi cancelada.",
                view=None,
            )
            return

        selected_index = int(self.values[0])
        try:
            candidate = self.view_instance.candidates[selected_index]
        except IndexError:
            await interaction.edit_original_response(
                content="❌ Esse resultado não está mais disponível. Tente adicionar de novo.",
                view=None,
            )
            return

        try:
            resolved = await self.view_instance.cog.resolve_wikipedia_candidate(
                candidate,
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
            )
            item = self.view_instance.cog.build_wikipedia_tier_item(
                resolved,
                custom_caption=self.view_instance.custom_caption,
                source_query=self.view_instance.source_query,
            )
        except WikipediaUserError as exc:
            LOGGER.exception("Falha amigável ao preparar candidato Wikipedia: %s", exc.code)
            await interaction.edit_original_response(content=f"❌ {exc.user_message}", view=None)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("Falha inesperada ao preparar candidato Wikipedia.")
            await interaction.edit_original_response(
                content="❌ A imagem encontrada não pôde ser baixada com segurança.",
                view=None,
            )
            return

        tier_view = ItemTierSelectView(
            cog=self.view_instance.cog,
            owner_id=self.view_instance.owner_id,
            item=item,
            tiers=session.tiers,
        )
        item_label = item.name or "item Wikipedia"
        await interaction.edit_original_response(
            content=(
                f"📌 Escolha em qual tier colocar **{discord.utils.escape_markdown(item_label)}**:"
                "\n🌐 Imagem livre resolvida via Wikipedia/Wikimedia. A legenda ficará fora da imagem."
            ),
            view=tier_view,
        )
        self.view_instance.stop()


class WikipediaCandidateSelectView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "TierListCog",
        owner_id: int,
        candidates: list[WikipediaPageImageCandidate],
        custom_caption: str,
        source_query: str = "",
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.owner_id = owner_id
        self.candidates = candidates
        self.custom_caption = custom_caption
        self.source_query = source_query
        self.message: discord.Message | None = None
        self.add_item(WikipediaCandidateSelect(self))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content="⌛ A seleção expirou. O item não foi adicionado.",
                    view=None,
                )
            except discord.HTTPException:
                pass

        self.stop()



class ItemTierSelect(discord.ui.Select):
    def __init__(
        self,
        cog: TierListCog,
        owner_id: int,
        item: TierItem,
        tiers: list[str],
    ) -> None:
        self.cog = cog
        self.owner_id = owner_id
        self.item = item

        options = [
            discord.SelectOption(
                label=tier[:100],
                value=tier,
                description=f"Colocar em {tier}"[:100],
                emoji="📌",
            )
            for tier in tiers[:25]
        ]

        super().__init__(
            placeholder="Selecione a tier do item",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.edit_message(
                content="❌ Essa sessão expirou ou foi cancelada.",
                view=None,
            )
            return

        selected_tier = self.values[0]

        # Blindagem:
        # O Select foi criado com as tiers da sessão,
        # mas ainda validamos novamente no callback.
        if selected_tier not in session.tiers or selected_tier not in session.items:
            await interaction.response.edit_message(
                content="❌ Essa tier não existe mais na sessão atual. Configure novamente.",
                view=None,
            )
            return

        async with session.lock:
            total_items = sum(len(items) for items in session.items.values())
            if total_items >= self.cog.MAX_ITEMS_PER_SESSION:
                await interaction.response.edit_message(
                    content=f"⚠️ Limite de **{self.cog.MAX_ITEMS_PER_SESSION} itens** atingido nessa tier list.",
                    view=None,
                )
                return

            session.items[selected_tier].append(self.item)

        await self.cog.refresh_panel(session)

        if self.item.source_type == "spotify":
            image_badge = " com capa Spotify"
        elif self.item.source_type == WIKIPEDIA_SOURCE_TYPE:
            image_badge = " com imagem da Wikipedia"
        else:
            image_badge = " com imagem" if (self.item.image_url or self.item.image_bytes) else ""
        item_label = self.item.name or "item com imagem"

        await interaction.response.edit_message(
            content=(
                f"✅ **{discord.utils.escape_markdown(item_label)}**{image_badge} "
                f"foi adicionado em **{selected_tier}**."
            ),
            view=None,
        )


class EditTitleModal(discord.ui.Modal, title="Editar Nome da Tier List"):
    novo_titulo = discord.ui.TextInput(
        label="Novo Título",
        style=discord.TextStyle.short,
        required=True,
        min_length=1,
        max_length=100,
    )

    def __init__(self, current_title: str, view_instance: "TierListControlView") -> None:
        super().__init__()
        self.view_instance = view_instance
        self.novo_titulo.default = current_title

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Passo 1: Prevenção de Timeout
        await interaction.response.defer()

        # Passo 2: Atualização de Estado
        session = self.view_instance.cog.sessions.get(self.view_instance.owner_id)
        if not session:
            await interaction.followup.send("❌ Sessão expirada.", ephemeral=True)
            return

        old_title = session.title
        try:
            # Altera a variável na memória da sessão atual
            session.title = self.novo_titulo.value.strip()

            # Passo 3: Regeração Visual
            # Recria o Embed do painel que exibe o novo título na parte superior
            novo_embed = self.view_instance.cog.build_panel_embed(session)

            # Passo 4: Edição da Mensagem
            if session.panel_message:
                await session.panel_message.edit(embed=novo_embed, view=self.view_instance)
                
            await interaction.followup.send("✅ Título atualizado com sucesso!", ephemeral=True)
        except Exception as e:
            log_exception(e)
            # Reverte em caso de falha visual para proteger os dados da sessão
            session.title = old_title
            await interaction.followup.send(f"❌ Falha ao atualizar o título: {e}", ephemeral=True)


class ItemTierSelectView(discord.ui.View):
    def __init__(
        self,
        cog: TierListCog,
        owner_id: int,
        item: TierItem,
        tiers: list[str],
    ) -> None:
        super().__init__(timeout=120)

        self.cog = cog
        self.owner_id = owner_id

        self.add_item(
            ItemTierSelect(
                cog=cog,
                owner_id=owner_id,
                item=item,
                tiers=tiers,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Só quem criou a tier list pode usar esse menu.",
                ephemeral=True,
            )
            return False

        return True


class ItemSelectionSelect(discord.ui.Select):
    """Select paginado que transforma a escolha do usuario em editar ou remover item."""

    def __init__(
        self,
        view_instance: "ItemSelectionView",
        page_refs: list[dict],
    ) -> None:
        self.view_instance = view_instance
        self.page_refs = page_refs

        options: list[discord.SelectOption] = []
        for local_index, ref in enumerate(page_refs):
            item: TierItem = ref["item"]
            display_name = view_instance.display_item_name(item)
            options.append(
                discord.SelectOption(
                    label=display_name[:100],
                    value=str(local_index),
                    description=f"Tier {ref['tier']} • posição {ref['index'] + 1}"[:100],
                    emoji="🌐" if item.source_type == WIKIPEDIA_SOURCE_TYPE else ("🖼️" if item.image_url or item.image_bytes else "📝"),
                )
            )

        super().__init__(
            placeholder=view_instance.select_placeholder(),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_index = int(self.values[0])
        ref = self.page_refs[selected_index]

        if self.view_instance.mode == "remove":
            await self.view_instance.remove_selected_item(interaction, ref)
            return

        # Select -> Modal:
        # O usuario nunca digita o nome defeituoso para localizar o item.
        # Ele escolhe uma referencia exata de tier + indice, e o modal abre
        # com os dados atuais ja preenchidos.
        await interaction.response.send_modal(
            EditItemModal(
                main_view=self.view_instance.main_view,
                tier_name=ref["tier"],
                item_index=ref["index"],
                item=ref["item"],
            )
        )


class ItemSelectionView(discord.ui.View):
    """View efemera que lista todos os itens da sessao em paginas de ate 25."""

    PAGE_SIZE = 25

    def __init__(
        self,
        main_view: "TierListControlView",
        *,
        page: int = 0,
        mode: Literal["edit", "remove"] = "edit",
    ) -> None:
        super().__init__(timeout=120)
        self.main_view = main_view
        self.cog = main_view.cog
        self.owner_id = main_view.owner_id
        self.page = page
        self.mode = mode
        self.item_refs = self.collect_item_refs()
        self.total_pages = max(1, math.ceil(len(self.item_refs) / self.PAGE_SIZE))
        self.page = max(0, min(self.page, self.total_pages - 1))

        page_start = self.page * self.PAGE_SIZE
        page_end = page_start + self.PAGE_SIZE
        page_refs = self.item_refs[page_start:page_end]

        if page_refs:
            self.add_item(ItemSelectionSelect(self, page_refs))

        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.total_pages - 1

    def collect_item_refs(self) -> list[dict]:
        session = self.cog.sessions.get(self.owner_id)
        if not session:
            return []

        refs: list[dict] = []
        for tier in session.tiers:
            for index, item in enumerate(session.items.get(tier, [])):
                refs.append(
                    {
                        "tier": tier,
                        "index": index,
                        "item": item,
                    }
                )
        return refs

    def display_item_name(self, item: TierItem) -> str:
        name = re.sub(r"\s+", " ", (item.name or "").strip())
        if name:
            return name

        for fallback in (
            item.spotify_name,
            item.display_name,
            item.internal_title,
            item.wikipedia_title,
            item.source_query,
        ):
            fallback_name = re.sub(r"\s+", " ", (fallback or "").strip())
            if fallback_name:
                return fallback_name

        if item.image_url or item.image_bytes:
            return "item com imagem"
        return "item sem nome"

    def action_emoji(self) -> str:
        return "🗑️" if self.mode == "remove" else "✏️"

    def action_verb(self) -> str:
        return "remover" if self.mode == "remove" else "editar"

    def select_placeholder(self) -> str:
        return f"Selecione o item que deseja {self.action_verb()}"

    def page_content(self) -> str:
        return (
            f"{self.action_emoji()} Selecione o item para {self.action_verb()}. "
            f"Página {self.page + 1}/{self.total_pages}"
        )

    async def remove_selected_item(
        self,
        interaction: discord.Interaction,
        ref: dict,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        session = self.cog.sessions.get(self.owner_id)
        if session is None:
            await interaction.followup.send(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        tier_name = ref["tier"]
        item_index = ref["index"]
        original_item: TierItem = ref["item"]
        removed_item: TierItem | None = None

        async with session.lock:
            try:
                current_item = session.items[tier_name][item_index]
            except (KeyError, IndexError):
                await interaction.followup.send(
                    "❌ Não encontrei esse item na sessão atual. Abra o menu de remoção novamente.",
                    ephemeral=True,
                )
                return

            if current_item is not original_item:
                await interaction.followup.send(
                    "❌ A lista mudou desde que você abriu o menu. Abra o menu de remoção novamente.",
                    ephemeral=True,
                )
                return

            removed_item = session.items[tier_name].pop(item_index)

        try:
            await self.main_view.refresh_after_item_edit(interaction, session)
        except Exception as exc:
            log_exception(exc)
            if removed_item is not None:
                async with session.lock:
                    if tier_name not in session.items:
                        session.items[tier_name] = []
                    safe_index = max(0, min(item_index, len(session.items[tier_name])))
                    session.items[tier_name].insert(safe_index, removed_item)

            await interaction.followup.send(
                "❌ Falha ao remover o item. A alteração foi revertida para proteger a sessão.",
                ephemeral=True,
            )
            return

        removed_label = self.display_item_name(removed_item) if removed_item else "item"
        try:
            await interaction.edit_original_response(
                content=(
                    f"✅ **{discord.utils.escape_markdown(removed_label)}** "
                    f"foi removido da tier **{discord.utils.escape_markdown(tier_name)}**."
                ),
                view=None,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                f"✅ **{discord.utils.escape_markdown(removed_label)}** foi removido.",
                ephemeral=True,
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Só quem criou a tier list pode usar esse menu.",
                ephemeral=True,
            )
            return False

        if self.owner_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        previous_view = ItemSelectionView(self.main_view, page=self.page - 1, mode=self.mode)
        await interaction.response.edit_message(
            content=previous_view.page_content(),
            view=previous_view,
        )

    @discord.ui.button(label="Próxima", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        next_view = ItemSelectionView(self.main_view, page=self.page + 1, mode=self.mode)
        await interaction.response.edit_message(
            content=next_view.page_content(),
            view=next_view,
        )


class EditItemModal(discord.ui.Modal):
    """Modal preenchido com os dados atuais do item selecionado."""

    def __init__(
        self,
        *,
        main_view: "TierListControlView",
        tier_name: str,
        item_index: int,
        item: TierItem,
    ) -> None:
        super().__init__(title="Editar Item")
        self.main_view = main_view
        self.tier_name = tier_name
        self.item_index = item_index
        self.item = item

        self.item_name = discord.ui.TextInput(
            label="Nome do item",
            placeholder="Opcional",
            default=item.name or "",
            min_length=0,
            max_length=25,
            required=False,
        )
        default_url = item.image_url if item.image_url and main_view.cog.looks_like_url(item.image_url) else ""
        self.image_url = discord.ui.TextInput(
            label="URL da imagem",
            placeholder="Opcional: deixe vazio para remover/trocar por texto",
            default=default_url,
            min_length=0,
            max_length=500,
            required=False,
        )
        self.target_tier = discord.ui.TextInput(
            label="Tier",
            placeholder="Exemplo: S, A, B...",
            default=tier_name,
            min_length=1,
            max_length=20,
            required=True,
        )

        self.add_item(self.item_name)
        self.add_item(self.image_url)
        self.add_item(self.target_tier)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defer obrigatório: mutação + possível hidratação + Pillow podem passar
        # do tempo limite de resposta inicial do Discord.
        await interaction.response.defer(ephemeral=True)

        session = self.main_view.cog.sessions.get(self.main_view.owner_id)
        if not session:
            await interaction.followup.send("❌ Sessão expirada.", ephemeral=True)
            return

        old_tier = self.tier_name
        new_tier = self.main_view.cog.clean_text(str(self.target_tier.value), max_length=20)
        new_caption = self.main_view.cog.render_caption_from_user(str(self.item_name.value), max_length=25)
        new_name = new_caption or ""
        new_url = str(self.image_url.value).strip() or None

        if new_tier not in session.items:
            await interaction.followup.send("❌ Essa tier não existe na sessão atual.", ephemeral=True)
            return

        if new_url and not self.main_view.cog.looks_like_url(new_url):
            await interaction.followup.send("❌ A URL informada não parece válida.", ephemeral=True)
            return

        try:
            current_item = session.items[old_tier][self.item_index]
        except (KeyError, IndexError):
            await interaction.followup.send(
                "❌ Não encontrei esse item na sessão atual. Abra o menu de edição novamente.",
                ephemeral=True,
            )
            return

        if current_item is not self.item:
            await interaction.followup.send(
                "❌ A lista mudou desde que você abriu o menu. Abra o menu de edição novamente.",
                ephemeral=True,
            )
            return

        # Snapshot reversivel: se refresh/render falhar, o estado volta ao ponto
        # anterior e a sessão não fica parcialmente corrompida.
        old_state = replace(current_item)
        old_url = current_item.image_url
        old_valid_url = old_url if old_url and self.main_view.cog.looks_like_url(old_url) else None
        keeps_existing_image = bool(old_valid_url and new_url == old_valid_url)
        if not new_caption and not new_url and not keeps_existing_image:
            await interaction.followup.send(
                self.main_view.cog.empty_item_message(),
                ephemeral=True,
            )
            return

        moved_item: TierItem | None = None

        try:
            if current_item.source_type == "spotify" and new_url == old_valid_url:
                current_item.name = new_name
                current_item.caption = new_caption
                current_item.user_caption = new_caption
                current_item.render_caption = new_caption
                current_item.has_visible_caption = new_caption is not None
            elif current_item.source_type == WIKIPEDIA_SOURCE_TYPE and new_url == old_valid_url:
                current_item.name = new_name
                current_item.caption = new_caption
                current_item.user_caption = new_caption
                current_item.render_caption = new_caption
                current_item.has_visible_caption = new_caption is not None
            else:
                current_item.name = new_name
                current_item.caption = new_caption
                current_item.user_caption = new_caption
                current_item.render_caption = new_caption
                current_item.has_visible_caption = new_caption is not None
            current_item.image_url = new_url
            if new_url != old_valid_url:
                current_item.image_bytes = None
                self.main_view.cog.clear_spotify_metadata(current_item)
                self.main_view.cog.clear_wikipedia_metadata(current_item)
                current_item.source_type = "image_url" if new_url else "text"
                current_item.internal_title = "Imagem por URL" if new_url else None
                current_item.source_query = new_url or None

            if new_tier != old_tier:
                moved_item = session.items[old_tier].pop(self.item_index)
                session.items[new_tier].append(moved_item)

            await self.main_view.refresh_after_item_edit(interaction, session)

            await interaction.followup.send("✅ Item editado com sucesso.", ephemeral=True)

        except Exception as exc:
            log_exception(exc)
            # Rollback completo: restaura campos e recoloca o item na tier/indice
            # original caso ele tenha sido movido antes da falha.
            rollback_item = moved_item or current_item
            if moved_item is not None:
                try:
                    session.items[new_tier].remove(moved_item)
                except (KeyError, ValueError):
                    pass
                session.items[old_tier].insert(self.item_index, moved_item)

            for attr, value in vars(old_state).items():
                setattr(rollback_item, attr, value)

            await interaction.followup.send("❌ Falha ao editar o item.", ephemeral=True)


class TierColorSelect(discord.ui.Select):
    """Select de tiers que abre um modal de cor para a tier escolhida."""

    def __init__(self, view_instance: "TierColorSelectionView") -> None:
        self.view_instance = view_instance
        session = view_instance.cog.sessions.get(view_instance.owner_id)
        tiers = session.tiers if session else []

        options = [
            discord.SelectOption(
                label=tier[:100],
                value=tier,
                description="Editar cor desta tier",
                emoji="🎨",
            )
            for tier in tiers[:25]
        ]

        super().__init__(
            placeholder="Selecione a tier para editar a cor",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        tier_name = self.values[0]
        session = self.view_instance.cog.sessions.get(self.view_instance.owner_id)

        if not session or tier_name not in session.items:
            await interaction.response.edit_message(
                content="❌ Essa tier não existe mais na sessão atual.",
                view=None,
            )
            return

        current_color = session.tier_colors.get(
            tier_name,
            self.view_instance.main_view.default_color_for_tier(session, tier_name),
        )

        await interaction.response.send_modal(
            EditTierColorModal(
                main_view=self.view_instance.main_view,
                tier_name=tier_name,
                current_color=current_color,
            )
        )


class TierColorSelectionView(discord.ui.View):
    """View efemera que lista as tiers atuais para customizacao de cor."""

    def __init__(self, main_view: "TierListControlView") -> None:
        super().__init__(timeout=120)
        self.main_view = main_view
        self.cog = main_view.cog
        self.owner_id = main_view.owner_id
        self.add_item(TierColorSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Só quem criou a tier list pode usar esse menu.",
                ephemeral=True,
            )
            return False

        if self.owner_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return False

        return True


class TierConfigView(discord.ui.View):
    """Menu efemero que centraliza configuracao de tiers e cores."""

    def __init__(self, main_view: "TierListControlView") -> None:
        super().__init__(timeout=120)
        self.main_view = main_view
        self.cog = main_view.cog
        self.owner_id = main_view.owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Só quem criou a tier list pode usar esse menu.",
                ephemeral=True,
            )
            return False

        if self.owner_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Editar Tiers",
        emoji="📝",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_tiers(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            ConfigureTiersModal(
                cog=self.cog,
                owner_id=self.owner_id,
            )
        )

    @discord.ui.button(
        label="Editar Cores",
        emoji="🎨",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_colors(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.edit_message(
                content="❌ Essa sessão expirou ou foi cancelada.",
                view=None,
            )
            return

        if not session.tiers:
            await interaction.response.edit_message(
                content="⚠️ Não há tiers para editar.",
                view=None,
            )
            return

        await interaction.response.edit_message(
            content="🎨 Selecione a tier que receberá uma nova cor.",
            view=TierColorSelectionView(self.main_view),
        )


class EditTierColorModal(discord.ui.Modal):
    """Modal de validacao hexadecimal e injecao de cor no estado da sessao."""

    def __init__(
        self,
        *,
        main_view: "TierListControlView",
        tier_name: str,
        current_color: tuple[int, int, int],
    ) -> None:
        super().__init__(title=f"Editar Cor: {tier_name}")
        self.main_view = main_view
        self.tier_name = tier_name
        self.hex_color = discord.ui.TextInput(
            label="Cor hexadecimal",
            placeholder="#FF0000",
            default=main_view.cog.color_to_hex(current_color),
            min_length=6,
            max_length=7,
            required=True,
        )
        self.add_item(self.hex_color)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        session = self.main_view.cog.sessions.get(self.main_view.owner_id)
        if not session:
            await interaction.followup.send("❌ Sessão expirada.", ephemeral=True)
            return

        if self.tier_name not in session.items:
            await interaction.followup.send("❌ Essa tier não existe na sessão atual.", ephemeral=True)
            return

        parsed_color = self.main_view.cog.parse_hex_color(str(self.hex_color.value))
        old_color = session.tier_colors.get(self.tier_name)
        used_default = False

        if parsed_color is None:
            used_default = True
            parsed_color = self.main_view.default_color_for_tier(session, self.tier_name)

        try:
            if used_default:
                session.tier_colors.pop(self.tier_name, None)
            else:
                session.tier_colors[self.tier_name] = parsed_color

            await self.main_view.refresh_after_item_edit(interaction, session)
            if used_default:
                await interaction.followup.send(
                    "⚠️ Cor inválida. Mantive a cor padrão dessa tier sem quebrar a imagem.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send("✅ Cor da tier atualizada.", ephemeral=True)

        except Exception as exc:
            log_exception(exc)
            if old_color is None:
                session.tier_colors.pop(self.tier_name, None)
            else:
                session.tier_colors[self.tier_name] = old_color
            await interaction.followup.send("❌ Falha ao editar a cor da tier.", ephemeral=True)


class PostGenerationView(discord.ui.View):
    """View anexada a imagem final para reabrir a sessao sem perder estado."""

    def __init__(self, cog: "TierListCog", owner_id: int) -> None:
        super().__init__(timeout=30 * 60)
        self.cog = cog
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Essa tier list não é sua para editar.",
                ephemeral=True,
            )
            return False

        if self.owner_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ O estado dessa tier list não está mais disponível em memória.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="Editar Tier List", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def reopen_session(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)
        if not session:
            await interaction.response.send_message(
                "❌ O estado dessa tier list não está mais disponível em memória.",
                ephemeral=True,
            )
            return

        active_view = TierListControlView(self.cog, self.owner_id)
        session.panel_message = interaction.message

        await interaction.response.edit_message(
            content=None,
            embed=self.cog.build_panel_embed(session),
            attachments=[],
            view=active_view,
        )


class TierListControlView(discord.ui.View):
    def __init__(self, cog: TierListCog, owner_id: int) -> None:
        super().__init__(timeout=15 * 60)

        self.cog = cog
        self.owner_id = owner_id

    def default_color_for_tier(
        self,
        session: TierListSession,
        tier_name: str,
    ) -> tuple[int, int, int]:
        try:
            tier_index = session.tiers.index(tier_name)
        except ValueError:
            tier_index = 0
        return self.cog.renderer.TIER_COLORS[tier_index % len(self.cog.renderer.TIER_COLORS)]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Esse painel não é seu, beldade. Crie o seu com `/tierlist criar`.",
                ephemeral=True,
            )
            return False

        if self.owner_id not in self.cog.sessions:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return False

        return True

    async def on_timeout(self) -> None:
        # Remove a sessão da RAM.
        # Isso evita memory leak caso a pessoa abandone o painel.
        session = self.cog.sessions.pop(self.owner_id, None)

        for child in self.children:
            child.disabled = True

        if session and session.panel_message:
            try:
                embed = discord.Embed(
                    title="⌛ Sessão Expirada",
                    description="A criação dessa tier list ficou inativa por muito tempo e foi encerrada.",
                    color=discord.Color.dark_gray(),
                )

                await session.panel_message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

        self.stop()

    async def refresh_after_item_edit(
        self,
        interaction: discord.Interaction,
        session: TierListSession,
    ) -> None:
        """
        Atualiza a mensagem principal depois da mutacao do item.

        A fonte da verdade continua sendo `session.items`. Para a imagem, criamos
        um snapshot descartavel e hidratado; assim bytes temporarios de download
        nao poluem a sessao em memoria.
        """
        if not session.panel_message:
            return

        async with session.lock:
            tiers_snapshot = self.cog.clone_tiers_snapshot(session)

        hydrated_snapshot = await self.cog.hydrate_tier_images(tiers_snapshot)

        guild_icon_bytes = None
        if interaction.guild and interaction.guild.icon:
            try:
                guild_icon_bytes = await interaction.guild.icon.replace(format="png", size=128).read()
            except (discord.HTTPException, ValueError, TypeError):
                guild_icon_bytes = None

        image_buffer = await asyncio.to_thread(
            self.cog.renderer.generate_tierlist_image,
            session.title,
            hydrated_snapshot,
            author=interaction.user,
            guild_icon_bytes=guild_icon_bytes,
            tier_colors=session.tier_colors,
        )

        preview_file = discord.File(image_buffer, filename="tierlist_preview.png")
        await session.panel_message.edit(
            embed=self.cog.build_panel_embed(session),
            attachments=[preview_file],
            view=self,
        )

    @discord.ui.button(
        label="Editar Título",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_title(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        # Abre o modal passando o título atual para carregar como default
        await interaction.response.send_modal(
            EditTitleModal(
                current_title=session.title,
                view_instance=self,
            )
        )

    @discord.ui.button(
        label="Configurar Tiers",
        emoji="📝",
        style=discord.ButtonStyle.secondary,
    )
    async def configure_tiers(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "⚙️ O que você quer configurar nas tiers?",
            view=TierConfigView(self),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Adicionar Item",
        emoji="➕",
        style=discord.ButtonStyle.secondary,
    )
    async def add_item(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        if not session.tiers:
            await interaction.response.send_message(
                "⚠️ Configure as tiers antes de adicionar itens.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            AddItemModal(
                cog=self.cog,
                owner_id=self.owner_id,
            )
        )

    @discord.ui.button(
        label="Remover Item",
        emoji="🗑️",
        style=discord.ButtonStyle.secondary,
    )
    async def remove_item(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        total_items = sum(len(items) for items in session.items.values())
        if total_items <= 0:
            await interaction.response.send_message(
                "⚠️ Não há itens para remover.",
                ephemeral=True,
            )
            return

        selection_view = ItemSelectionView(self, mode="remove")
        await interaction.response.send_message(
            selection_view.page_content(),
            view=selection_view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Editar Item",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_item(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        total_items = sum(len(items) for items in session.items.values())
        if total_items <= 0:
            await interaction.response.send_message(
                "⚠️ Não há itens para editar.",
                ephemeral=True,
            )
            return

        selection_view = ItemSelectionView(self, mode="edit")
        await interaction.response.send_message(
            selection_view.page_content(),
            view=selection_view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Finalizar",
        emoji="🖼️",
        style=discord.ButtonStyle.success,
    )
    async def generate_image(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        session = self.cog.sessions.get(self.owner_id)

        if session is None:
            await interaction.response.send_message(
                "❌ Essa sessão expirou ou foi cancelada.",
                ephemeral=True,
            )
            return

        async with session.lock:
            total_items = sum(len(items) for items in session.items.values())

        if total_items <= 0:
            await interaction.response.send_message(
                "⚠️ Adicione pelo menos **1 item** antes de gerar a imagem.",
                ephemeral=True,
            )
            return

        # Responde imediatamente ao Discord.
        # Isso evita timeout da interação enquanto baixamos imagens e renderizamos.
        await interaction.response.defer(thinking=True)

        # Copia a sessão para evitar alteração enquanto renderiza.
        async with session.lock:
            title_snapshot = session.title
            tier_colors_snapshot = dict(session.tier_colors)
            tiers_snapshot = self.cog.clone_tiers_snapshot(session)

        # Download assíncrono das URLs. hydrate_tier_images e defensivo, mas
        # mantemos um fallback local para garantir que rede quebrada nunca mate
        # a entrega da imagem final.
        try:
            hydrated_snapshot = await self.cog.hydrate_tier_images(tiers_snapshot)
        except Exception as exc:
            log_exception(exc)
            print(f"[TIERLIST GENERATE] Hidratação falhou; renderizando snapshot sem novos downloads: {exc}")
            hydrated_snapshot = tiers_snapshot

        guild_icon_bytes = None
        if interaction.guild and interaction.guild.icon:
            try:
                guild_icon_bytes = await interaction.guild.icon.replace(format="png", size=128).read()
            except (discord.HTTPException, ValueError, TypeError):
                guild_icon_bytes = None

        try:
            # Pillow fora do event loop.
            image_buffer = await asyncio.to_thread(
                self.cog.renderer.generate_tierlist_image,
                title_snapshot,
                hydrated_snapshot,
                author=interaction.user,
                guild_icon_bytes=guild_icon_bytes,
                tier_colors=tier_colors_snapshot,
            )

        except Exception as exc:
            log_exception(exc)
            print(f"[TIERLIST GENERATE] Render principal falhou; tentando fallback textual: {exc}")
            fallback_snapshot: OrderedDictType[str, list[TierItem]] = OrderedDict(
                (
                    tier,
                    [
                        replace(
                            item,
                            name=item.render_caption or item.caption or item.name or "",
                            image_url=None,
                            image_bytes=None,
                            source_type="text",
                        )
                        for item in hydrated_snapshot.get(tier, [])
                    ],
                )
                for tier in hydrated_snapshot.keys()
            )

            try:
                image_buffer = await asyncio.to_thread(
                    self.cog.renderer.generate_tierlist_image,
                    title_snapshot,
                    fallback_snapshot,
                    author=interaction.user,
                    guild_icon_bytes=guild_icon_bytes,
                    tier_colors={},
                )
            except Exception as fallback_exc:
                log_exception(fallback_exc)
                print(f"[TIERLIST GENERATE] Fallback textual também falhou: {fallback_exc}")
                await interaction.followup.send(
                    "❌ Não consegui gerar a imagem final dessa vez.",
                    ephemeral=True,
                )
                return

        files = [discord.File(
            image_buffer,
            filename="tierlist.png",
        )]

        for child in self.children:
            child.disabled = True

        try:
            if session.panel_message:
                done_embed = discord.Embed(
                    title="✅ Tier List Gerada",
                    description="A imagem final foi criada. Você ainda pode reabrir a edição pela mensagem final.",
                    color=discord.Color.green(),
                )

                await session.panel_message.edit(embed=done_embed, view=None)
        except discord.HTTPException:
            pass

        self.stop()

        final_message = await interaction.followup.send(
            content=f"🖼️ **{discord.utils.escape_markdown(title_snapshot)}**",
            files=files,
            view=PostGenerationView(self.cog, self.owner_id),
            wait=True,
        )
        session.panel_message = final_message

    @discord.ui.button(
        label="Cancelar",
        emoji="❌",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.cog.sessions.pop(self.owner_id, None)

        for child in self.children:
            child.disabled = True

        self.stop()

        await interaction.response.defer(ephemeral=True)

        try:
            await interaction.message.delete()

            await interaction.followup.send(
                "❌ Sessão cancelada e painel removido.",
                ephemeral=True,
            )

        except discord.HTTPException:
            try:
                embed = discord.Embed(
                    title="❌ Sessão Cancelada",
                    description="Os dados temporários foram apagados.",
                    color=discord.Color.red(),
                )

                await interaction.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

            await interaction.followup.send(
                "❌ Sessão cancelada.",
                ephemeral=True,
            )


# ============================================================
# COG PRINCIPAL
# ============================================================

class TierListCog(
    commands.GroupCog,
    group_name="tierlist",
    group_description="Crie Tier Lists Interativas",
):
    MAX_TIERS = 25
    MAX_ITEMS_PER_SESSION = 150

    MAX_IMAGE_BYTES = 5 * 1024 * 1024
    IMAGE_DOWNLOAD_TIMEOUT = 5
    IMAGE_DOWNLOAD_CONCURRENCY = 8

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # Estado temporário:
        # user_id -> TierListSession
        self.sessions: dict[int, TierListSession] = {}

        self.renderer = TierListRenderer()
        self.spotify_service = SpotifyService()
        self.spotify_resolver = SpotifyInputResolver(self.spotify_service)
        self.spotify_image_downloader = SpotifyImageDownloader(
            processor=SpotifyImageProcessor(),
            max_bytes=self.MAX_IMAGE_BYTES,
            timeout_seconds=max(self.IMAGE_DOWNLOAD_TIMEOUT, 8),
        )
        self.wikipedia_service = WikipediaImageService()

    async def resolve_spotify_input(
        self,
        raw: str,
        *,
        preferred_type: str | None = None,
        allow_ambiguous: bool = True,
    ) -> SpotifyResolution:
        return await self.spotify_resolver.resolve(
            raw,
            preferred_type=preferred_type,
            allow_ambiguous=allow_ambiguous,
        )

    async def build_spotify_tier_item(
        self,
        resolved: SpotifyResolvedItem,
        *,
        custom_caption: str = "",
        source_query: str = "",
    ) -> TierItem:
        image_bytes = await self.spotify_image_downloader.download(
            resolved.image_url,
            cache_key=resolved.cache_key,
        )

        caption = self.render_caption_from_user(custom_caption, max_length=80)

        if not image_bytes:
            raise SpotifyImageError(
                "A capa desse item não pôde ser baixada com segurança.",
                code="spotify_image_empty",
            )

        LOGGER.info(
            "Item Spotify pronto: %s:%s '%s'.",
            resolved.spotify_type,
            resolved.spotify_id,
            resolved.spotify_name,
        )

        return TierItem(
            name=caption or "",
            image_url=resolved.image_url,
            image_bytes=image_bytes,
            source_type="spotify",
            caption=caption,
            user_caption=caption,
            render_caption=caption,
            has_visible_caption=caption is not None,
            internal_title=resolved.display_name,
            source_query=source_query,
            image_cache_key=resolved.cache_key,
            spotify_type=resolved.spotify_type,
            spotify_id=resolved.spotify_id,
            spotify_url=resolved.spotify_url,
            spotify_name=resolved.spotify_name,
            spotify_artists=resolved.artists,
            album_name=resolved.album_name,
            track_name=resolved.track_name,
            release_date=resolved.release_date,
            attribution_text=resolved.attribution_text,
        )

    async def resolve_wikipedia_input(
        self,
        raw: str,
        *,
        allow_ambiguous: bool = True,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> WikipediaResolution:
        return await self.wikipedia_service.resolve(
            raw,
            allow_ambiguous=allow_ambiguous,
            guild_id=guild_id,
            user_id=user_id,
        )

    async def resolve_wikipedia_candidate(
        self,
        candidate: WikipediaPageImageCandidate,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> WikipediaResolvedImage:
        return await self.wikipedia_service.resolve_candidate(
            candidate,
            guild_id=guild_id,
            user_id=user_id,
        )

    def build_wikipedia_tier_item(
        self,
        resolved: WikipediaResolvedImage,
        *,
        custom_caption: str = "",
        source_query: str = "",
    ) -> TierItem:
        caption = self.render_caption_from_user(custom_caption, max_length=80)

        return TierItem(
            name=caption or "",
            image_url=resolved.image_url,
            image_bytes=resolved.image_bytes,
            source_type=WIKIPEDIA_SOURCE_TYPE,
            caption=caption,
            user_caption=caption,
            render_caption=caption,
            has_visible_caption=caption is not None,
            internal_title=resolved.wikipedia_title or resolved.display_name,
            source_query=source_query,
            image_cache_key=resolved.image_cache_key,
            attribution_text=None,
            display_name=resolved.display_name,
            image_url_used=resolved.image_url,
            wiki_language=resolved.wiki_language,
            wikipedia_pageid=resolved.wikipedia_pageid,
            wikipedia_title=resolved.wikipedia_title,
            wikipedia_url=resolved.wikipedia_url,
            wikimedia_file_title=resolved.wikimedia_file_title,
            wikimedia_file_description_url=resolved.wikimedia_file_description_url,
            image_mime=resolved.image_mime,
            artist=resolved.artist,
            credit=resolved.credit,
            license_short_name=resolved.license_short_name,
            license_url=resolved.license_url,
            usage_terms=resolved.usage_terms,
            attribution_required=resolved.attribution_required,
            metadata_source=resolved.metadata_source,
        )

    async def send_tier_choice_for_item(
        self,
        interaction: discord.Interaction,
        *,
        owner_id: int,
        item: TierItem,
        tiers: list[str],
        extra: str = "",
    ) -> None:
        view = ItemTierSelectView(
            cog=self,
            owner_id=owner_id,
            item=item,
            tiers=tiers,
        )

        item_label = item.name or "item com imagem"
        await interaction.followup.send(
            f"📌 Escolha em qual tier colocar **{discord.utils.escape_markdown(item_label)}**:{extra}",
            view=view,
            ephemeral=True,
        )

    def clone_tier_item(self, item: TierItem) -> TierItem:
        return replace(item)

    def clone_tiers_snapshot(
        self,
        session: TierListSession,
    ) -> OrderedDictType[str, list[TierItem]]:
        return OrderedDict(
            (
                tier,
                [self.clone_tier_item(item) for item in session.items.get(tier, [])],
            )
            for tier in session.tiers
        )

    def clear_spotify_metadata(self, item: TierItem) -> None:
        item.image_cache_key = None
        item.spotify_type = None
        item.spotify_id = None
        item.spotify_url = None
        item.spotify_name = None
        item.spotify_artists = tuple()
        item.album_name = None
        item.track_name = None
        item.release_date = None
        item.attribution_text = None

    def clear_wikipedia_metadata(self, item: TierItem) -> None:
        item.display_name = None
        item.image_url_used = None
        item.wiki_language = None
        item.wikipedia_pageid = None
        item.wikipedia_title = None
        item.wikipedia_url = None
        item.wikimedia_file_title = None
        item.wikimedia_file_description_url = None
        item.image_mime = None
        item.artist = None
        item.credit = None
        item.license_short_name = None
        item.license_url = None
        item.usage_terms = None
        item.attribution_required = None
        item.metadata_source = None

    @app_commands.command(
        name="criar",
        description="Cria uma tier list interativa com texto, URLs, Spotify e Wikipedia.",
    )
    @app_commands.guild_only()
    @app_commands.describe(titulo="Título da tier list")
    async def criar(
        self,
        interaction: discord.Interaction,
        titulo: app_commands.Range[str, 1, 80],
    ) -> None:
        owner_id = interaction.user.id
        clean_title = self.clean_text(str(titulo), max_length=80)

        # Se o usuário já tinha sessão, substitui com segurança.
        old_session = self.sessions.pop(owner_id, None)

        if old_session and old_session.panel_message:
            try:
                old_embed = discord.Embed(
                    title="♻️ Sessão Substituída",
                    description="Você iniciou uma nova tier list, então essa sessão antiga foi encerrada.",
                    color=discord.Color.dark_gray(),
                )

                await old_session.panel_message.edit(embed=old_embed, view=None)
            except discord.HTTPException:
                pass

        session = TierListSession(
            owner_id=owner_id,
            title=clean_title,
        )

        self.sessions[owner_id] = session

        view = TierListControlView(
            cog=self,
            owner_id=owner_id,
        )

        embed = self.build_panel_embed(session)

        await interaction.response.send_message(
            embed=embed,
            view=view,
        )

        session.panel_message = await interaction.original_response()

    async def fetch_image_safely(
        self,
        http: aiohttp.ClientSession,
        url: str,
    ) -> bytes | None:
        """
        Baixa uma imagem sem derrubar o bot, operando com resiliência total.

        Regras de Negócio e Segurança:
        - Spoofing de User-Agent real para bypass de firewalls básicos (Erro 403).
        - Suporte a redirecionamentos (allow_redirects=True) para novos CDNs (ex: Discord).
        - Strict timeout individual de 5 segundos para não prender a queue de processamento.
        - Verificação rigorosa do 'Content-Type' -> Deve começar com 'image/'.
        - Captura de ClientError e TimeoutError isolada (Safe fallback para texto se falhar).
        """

        if not self.looks_like_url(url):
            return None

        parsed_url = urlparse(url)
        host = (parsed_url.netloc or "").casefold()
        if host.endswith("wikimedia.org") or host.endswith("wikipedia.org"):
            headers = self.wikipedia_service.http_client.image_headers
        else:
            # 1. Bypass de Firewalls Básicos (Cloudflare / CDN Blocks)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            }

        # 4. Gestão Rigorosa de Timeouts (5s limite rígido)
        timeout = aiohttp.ClientTimeout(total=5)

        try:
            # 2. allow_redirects=True garante o fetch seguro em CDN do Discord e afins
            async with http.get(url, headers=headers, allow_redirects=True, timeout=timeout) as response:
                if response.status != 200:
                    return None

                # 3. Validação de Content-Type (Evita abrir HTML como imagem no Pillow)
                content_type = response.headers.get("Content-Type", "").lower()
                if "image/" not in content_type:
                    return None

                data = bytearray()
                
                # Fetch iterativo seguro para proteção de memória
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > getattr(self, 'MAX_IMAGE_BYTES', 5 * 1024 * 1024):  # Fallback seguro para 5MB
                        return None

                if not data:
                    return None

                # Rito anti-"imagem fantasma":
                # Bytes baixados viram BytesIO, o ponteiro volta para 0 antes
                # do Image.open(), e o frame decodificado e purificado em RGBA.
                # O renderer ainda repetira essa defesa, mas salvar PNG RGBA
                # aqui impede capas de CDN (Spotify/Discord/etc.) de chegarem
                # vazias, paletizadas ou com alpha estranho na linha de chegada.
                try:
                    buffer = io.BytesIO(bytes(data))
                    buffer.seek(0)
                    with Image.open(buffer) as raw_image:
                        try:
                            raw_image.seek(0)
                        except Exception as exc:
                            log_exception(exc)
                            pass
                        purified_image = raw_image.convert("RGBA")

                    output = io.BytesIO()
                    purified_image.save(output, format="PNG")
                    output.seek(0)
                    return output.getvalue()
                except Exception as exc:
                    log_exception(exc)
                    print(f"[IMAGE FETCH] Pillow recusou imagem de '{url}': {exc}")
                    return None

        # Tratamento cirúrgico de quebras de rede (Timeout e Falha de HTTP)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        except Exception as exc:
            log_exception(exc)
            return None

    async def hydrate_tier_images(
        self,
        tiers_snapshot: OrderedDictType[str, list[TierItem]],
    ) -> OrderedDictType[str, list[TierItem]]:
        """
        Baixa todas as imagens antes do Pillow.

        Importante:
        - aiohttp roda de forma assíncrona;
        - renderização Pillow roda depois em thread separada;
        - imagem inválida vira None;
        - sem imagem, o renderer só desenha texto se houver render_caption.
        """

        timeout = aiohttp.ClientTimeout(total=self.IMAGE_DOWNLOAD_TIMEOUT)
        semaphore = asyncio.Semaphore(self.IMAGE_DOWNLOAD_CONCURRENCY)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:

                async def hydrate_one(item: TierItem) -> None:
                    if item.image_bytes:
                        return

                    if not item.image_url:
                        return

                    async with semaphore:
                        if item.source_type == "spotify" or item.spotify_id:
                            try:
                                item.image_bytes = await self.spotify_image_downloader.download(
                                    item.image_url,
                                    cache_key=item.image_cache_key or f"spotify:{item.spotify_type}:{item.spotify_id}",
                                )
                            except SpotifyUserError as exc:
                                LOGGER.warning("Falha ao hidratar capa Spotify: %s", exc.code)
                                item.image_bytes = None
                        elif item.source_type == WIKIPEDIA_SOURCE_TYPE:
                            try:
                                item.image_bytes = await self.wikipedia_service.image_downloader.download_validated(
                                    item.image_url,
                                    cache_key=item.image_cache_key or item.image_url_used or item.image_url,
                                )
                            except WikipediaUserError as exc:
                                LOGGER.warning("Falha ao hidratar imagem Wikipedia: %s", exc.code)
                                item.image_bytes = None
                        else:
                            item.image_bytes = await self.fetch_image_safely(http, item.image_url)

                tasks: list[asyncio.Task[None]] = []

                for items in tiers_snapshot.values():
                    for item in items:
                        if item.image_url:
                            tasks.append(asyncio.create_task(hydrate_one(item)))

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as exc:
            log_exception(exc)
            print(f"[IMAGE HYDRATION] Falha global na hidratação; renderizando fallback textual/imagens já salvas: {exc}")

        return tiers_snapshot

    def parse_tiers(self, raw: str) -> list[str]:
        """
        Entrada:
            S, A, B, C, D

        Saída:
            ["S", "A", "B", "C", "D"]

        Remove vazios, normaliza espaços e evita duplicatas.
        """

        parts = raw.split(",")
        tiers: list[str] = []
        seen: set[str] = set()

        for part in parts:
            clean = self.clean_text(part, max_length=20)

            if not clean:
                continue

            key = clean.casefold()

            if key in seen:
                continue

            seen.add(key)
            tiers.append(clean)

            if len(tiers) >= self.MAX_TIERS:
                break

        return tiers

    def clean_text(
        self,
        text: str,
        *,
        max_length: int,
    ) -> str:
        """
        Limpa texto do usuário:
        - remove quebras de linha;
        - comprime espaços repetidos;
        - corta no limite seguro.
        """

        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_length].strip()

    def render_caption_from_user(self, raw_caption: str, *, max_length: int = 80) -> str | None:
        return normalize_caption(raw_caption, max_length=max_length)

    def get_filled_image_sources(
        self,
        *,
        image_url: str = "",
        avatar_user_id: str = "",
        wikipedia: str = "",
        spotify: str = "",
    ) -> list[dict[str, str]]:
        source_fields = (
            ("image_url", "Link de imagem", image_url),
            ("avatar_user_id", "ID de usuário", avatar_user_id),
            ("wikipedia", "Wikipedia", wikipedia),
            ("spotify", "Spotify", spotify),
        )
        return [
            {"key": key, "label": label}
            for key, label, value in source_fields
            if str(value or "").strip()
        ]

    def format_filled_image_sources(self, sources: list[dict[str, str]]) -> str:
        labels = [source["label"] for source in sources if source.get("label")]
        if not labels:
            return ""
        if len(labels) == 1:
            return labels[0]
        return f"{', '.join(labels[:-1])} e {labels[-1]}"

    def conflicting_image_sources_message(self, sources: list[dict[str, str]]) -> str:
        filled_text = self.format_filled_image_sources(sources)
        suffix = f"\n\nFontes preenchidas: {filled_text}." if filled_text else ""
        return (
            "⚠️ Honra e proveito não cabem no mesmo saco estreito.\n\n"
            "Você preencheu mais de uma fonte de imagem ao mesmo tempo. "
            "Eu preciso saber qual imagem usar: avatar de usuário, link direto, "
            "Wikipedia, Spotify ou outra fonte, e não tudo junto no mesmo item.\n\n"
            "Escolha só uma fonte de imagem e tente de novo."
            f"{suffix}"
        )

    def empty_item_message(self) -> str:
        return (
            "⚠️ Esse item veio tão vazio que nem o abismo respondeu. "
            "Preencha um nome ou escolha uma fonte de imagem."
        )

    def parse_hex_color(self, raw: str) -> tuple[int, int, int] | None:
        """Valida e converte #RRGGBB/RRGGBB para tupla RGB."""
        value = (raw or "").strip()
        match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value)
        if not match:
            return None

        hex_value = match.group(1)
        try:
            return (
                int(hex_value[0:2], 16),
                int(hex_value[2:4], 16),
                int(hex_value[4:6], 16),
            )
        except ValueError:
            return None

    def color_to_hex(self, color: tuple[int, int, int]) -> str:
        """Formata RGB em #RRGGBB para preencher modais."""
        r, g, b = color
        return f"#{r:02X}{g:02X}{b:02X}"

    def looks_like_url(self, url: str) -> bool:
        parsed = urlparse(url)

        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
        )

    def build_panel_embed(self, session: TierListSession) -> discord.Embed:
        total_items = sum(len(items) for items in session.items.values())
        image_items = sum(
            1
            for items in session.items.values()
            for item in items
            if item.image_url
        )
        spotify_items = sum(
            1
            for items in session.items.values()
            for item in items
            if item.source_type == "spotify" or item.spotify_id
        )
        wikipedia_items = sum(
            1
            for items in session.items.values()
            for item in items
            if item.source_type == WIKIPEDIA_SOURCE_TYPE
        )

        embed = discord.Embed(
            title="🧩 Painel De Criação De Tier List",
            description=(
                f"**Título:** {discord.utils.escape_markdown(session.title)}\n"
                f"**Tiers:** {len(session.tiers)}\n"
                f"**Itens:** {total_items}/{self.MAX_ITEMS_PER_SESSION}\n"
                f"**Itens Com URL:** {image_items}\n\n"
                f"**Itens Spotify:** {spotify_items}\n"
                f"**Itens Wikipedia:** {wikipedia_items}\n\n"
                "Use os botões abaixo para configurar, adicionar itens e gerar a imagem final."
            ),
            color=discord.Color.from_rgb(155, 93, 229),
        )

        for tier in session.tiers:
            items = session.items.get(tier, [])

            preview_parts: list[str] = []

            for item in items[:8]:
                has_image = bool(item.image_url or item.image_bytes)
                if item.source_type == "spotify" or item.spotify_id:
                    icon = "🎵"
                elif item.source_type == WIKIPEDIA_SOURCE_TYPE:
                    icon = "🌐"
                else:
                    icon = "🖼️" if has_image else "📝"
                item_label = item.name or "item com imagem"
                preview_parts.append(f"{icon} {item_label}")

            preview = ", ".join(preview_parts)

            if len(items) > 8:
                preview += f" +{len(items) - 8}"

            embed.add_field(
                name=f"📌 {tier}",
                value=preview or "Sem itens ainda",
                inline=False,
            )

        embed.set_footer(
            text="A sessão expira após 15 minutos de inatividade. URLs ruins viram texto automaticamente."
        )

        return embed

    async def refresh_panel(self, session: TierListSession) -> None:
        if not session.panel_message:
            return

        try:
            await session.panel_message.edit(embed=self.build_panel_embed(session))
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TierListCog(bot))

