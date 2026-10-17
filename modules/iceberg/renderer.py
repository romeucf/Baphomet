from __future__ import annotations

import io
import logging
import pathlib
import re
import random
import textwrap
from dataclasses import dataclass, replace
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from modules.iceberg.constants import (
    ICEBERG_MAX_LAYERS,
    ICEBERG_MIN_LAYERS,
    ICEBERG_TEMPLATE_FILENAME,
    ICEBERG_TEMPLATE_TIER_COUNT,
    ICEBERG_TITLE_MAX_LENGTH,
)
from modules.iceberg.models import IcebergProject, ItemConfig, ItemDisplayStyle, ItemSourceType, LayerConfig, LayerLayoutMode, ThemeConfig
from modules.iceberg.themes import default_layer_name


LOGGER = logging.getLogger("baphomet.iceberg.renderer")
MASK_SCALE = 3
MAX_TEXT_LINES = 3
PREVIEW_MAX_WIDTH = 900
TEMPLATE_ITEM_SAFE_RIGHT_RATIO = 1288 / 1580
TITLE_BAR_BACKGROUND = (255, 255, 255, 255)
TITLE_BAR_TEXT_COLOR = (14, 18, 24, 255)
TITLE_HORIZONTAL_PADDING_RATIO = 0.045
TITLE_VERTICAL_PADDING_RATIO = 0.018
TITLE_FONT_SIZE_RATIO = 0.045
TITLE_MIN_FONT_SIZE_RATIO = 0.022
Image.MAX_IMAGE_PIXELS = 25_000_000


class IcebergTemplateError(RuntimeError):
    """Erro user-facing para problemas com a template fixa do iceberg."""


def crop_center_square(image: Image.Image) -> Image.Image:
    """Center-crop an image to 1:1 aspect ratio without distortion.

    - If wider than tall, trims the sides.
    - If taller than wide, trims top/bottom.
    - If already square, returns unchanged.
    """
    w, h = image.size
    if w == h:
        return image
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return image.crop((left, top, left + side, top + side))


def _has_caption(item: ItemConfig) -> bool:
    """Return True only if the item has a user-provided visible caption."""
    return bool(item.title and item.title.strip())


@dataclass(frozen=True)
class LayerRenderBox:
    layer: LayerConfig
    index: int
    top_y: int
    bottom_y: int
    top_width: int
    bottom_width: int
    polygon: tuple[tuple[int, int], ...]

    @property
    def height(self) -> int:
        return max(1, self.bottom_y - self.top_y)

    @property
    def min_left(self) -> int:
        return min(point[0] for point in self.polygon)

    @property
    def max_right(self) -> int:
        return max(point[0] for point in self.polygon)


class IcebergRenderer:
    def __init__(
        self,
        *,
        font_dir: str | pathlib.Path | None = None,
        template_path: str | pathlib.Path | None = None,
    ) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        self.font_dir = pathlib.Path(font_dir) if font_dir else repo_root / "assets" / "fonts"
        self.template_path = pathlib.Path(template_path) if template_path else repo_root / ICEBERG_TEMPLATE_FILENAME
        self._font_cache: dict[tuple[str, int], ImageFont.ImageFont] = {}
        self._mask_cache: dict[tuple[int, int, int], Image.Image] = {}
        self._template_cache: Image.Image | None = None

    def render_project(
        self,
        project: IcebergProject,
        *,
        asset_bytes_by_item_id: dict[str, bytes] | None = None,
        preview: bool = False,
    ) -> io.BytesIO:
        asset_bytes_by_item_id = asset_bytes_by_item_id or {}
        layer_count = self._validate_layer_count(len(project.layers))
        template = self._load_template()
        bounds = self._template_bounds(template.height)
        crop_height = bounds[layer_count]
        iceberg_image = template.crop((0, 0, template.width, crop_height)).convert("RGBA")
        theme = self._theme_for_canvas(project.theme, iceberg_image.size)
        render_project = replace(project, theme=theme)
        item_safe_box = self._item_safe_box(iceberg_image.width)

        layer_boxes = self._build_layer_boxes(render_project, bounds=bounds, item_safe_box=item_safe_box)
        for layer_box in layer_boxes:
            items = render_project.ordered_items_for_layer(layer_box.layer.id)
            self._draw_items_for_layer(iceberg_image, render_project, layer_box, items, asset_bytes_by_item_id)
        self._draw_layer_labels(iceberg_image, layer_boxes, theme)

        image = self._compose_with_title_bar(iceberg_image, render_project, theme)
        if preview:
            image = self._resize_preview(image)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    def _load_template(self) -> Image.Image:
        if self._template_cache is not None:
            return self._template_cache.copy()
        try:
            with Image.open(self.template_path) as raw:
                raw.load()
                template = raw.convert("RGBA")
        except FileNotFoundError as exc:
            raise IcebergTemplateError(
                f"❌ Não encontrei a template local `{ICEBERG_TEMPLATE_FILENAME}` em `{self.template_path}`."
            ) from exc
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            raise IcebergTemplateError(
                f"❌ Não consegui carregar a template local `{ICEBERG_TEMPLATE_FILENAME}`. Verifique se o PNG não está corrompido."
            ) from exc
        self._template_cache = template
        return template.copy()

    def _template_bounds(self, template_height: int) -> list[int]:
        return [
            round(index * template_height / ICEBERG_TEMPLATE_TIER_COUNT)
            for index in range(ICEBERG_TEMPLATE_TIER_COUNT + 1)
        ]

    def _validate_layer_count(self, layer_count: int) -> int:
        if layer_count < ICEBERG_MIN_LAYERS:
            raise ValueError(f"O iceberg precisa ter no mínimo {ICEBERG_MIN_LAYERS} camadas para renderizar.")
        if layer_count > ICEBERG_MAX_LAYERS:
            raise ValueError(f"O iceberg precisa ter no máximo {ICEBERG_MAX_LAYERS} camadas para renderizar.")
        return layer_count

    def _theme_for_canvas(self, theme: ThemeConfig, size: tuple[int, int]) -> ThemeConfig:
        width, height = size
        scale = width / max(1, theme.canvas_width)

        def scaled(value: int, minimum: int = 1) -> int:
            return max(minimum, int(round(value * scale)))

        return replace(
            theme,
            canvas_width=width,
            canvas_height=height,
            padding_x=scaled(theme.padding_x),
            title_top=scaled(theme.title_top, 0),
            title_height=scaled(theme.title_height),
            iceberg_top_y=0,
            iceberg_bottom_y=height,
            iceberg_center_x=width // 2,
            title_font_size=scaled(theme.title_font_size, 8),
            layer_label_font_size=scaled(theme.layer_label_font_size, 8),
            item_font_size=scaled(theme.item_font_size, 8),
            item_caption_font_size=scaled(theme.item_caption_font_size, 8),
            item_card_width=scaled(theme.item_card_width, 24),
            item_card_height=scaled(theme.item_card_height, 28),
            item_chip_min_width=scaled(theme.item_chip_min_width, 24),
            item_chip_height=scaled(theme.item_chip_height, 18),
            item_gap_x=scaled(theme.item_gap_x),
            item_gap_y=scaled(theme.item_gap_y),
            item_padding_x=scaled(theme.item_padding_x),
            item_padding_y=scaled(theme.item_padding_y),
            item_radius=scaled(theme.item_radius),
            layer_inner_padding_x=scaled(theme.layer_inner_padding_x),
            layer_inner_padding_y=scaled(theme.layer_inner_padding_y),
            item_stroke_width=scaled(theme.item_stroke_width, 0),
        )

    def _item_safe_box(self, canvas_width: int) -> tuple[int, int]:
        safe_right = int(round(canvas_width * TEMPLATE_ITEM_SAFE_RIGHT_RATIO))
        safe_right = max(1, min(canvas_width, safe_right))
        return 0, safe_right

    def _compose_with_title_bar(
        self,
        iceberg_image: Image.Image,
        project: IcebergProject,
        theme: ThemeConfig,
    ) -> Image.Image:
        title_bar = self._build_title_bar(project, theme, iceberg_image.width)
        final_image = Image.new(
            "RGBA",
            (iceberg_image.width, title_bar.height + iceberg_image.height),
            TITLE_BAR_BACKGROUND,
        )
        final_image.alpha_composite(title_bar, (0, 0))
        final_image.alpha_composite(iceberg_image, (0, title_bar.height))
        return final_image

    def _build_title_bar(self, project: IcebergProject, theme: ThemeConfig, width: int) -> Image.Image:
        measure = Image.new("RGBA", (width, 1), TITLE_BAR_BACKGROUND)
        draw = ImageDraw.Draw(measure, "RGBA")
        title = self._title_text(project.name)
        horizontal_padding = min(
            max(12, int(width * TITLE_HORIZONTAL_PADDING_RATIO)),
            max(12, width // 4),
        )
        vertical_padding = max(18, int(width * TITLE_VERTICAL_PADDING_RATIO))
        max_width = max(1, width - horizontal_padding * 2)
        start_size = max(24, int(width * TITLE_FONT_SIZE_RATIO))
        min_size = max(12, int(width * TITLE_MIN_FONT_SIZE_RATIO))
        font = self._fit_font(
            draw,
            title,
            theme,
            start_size=max(start_size, min_size),
            max_width=max_width,
            min_size=min_size,
            bold=True,
        )
        if draw.textbbox((0, 0), title, font=font)[2] <= max_width:
            lines = [title]
        else:
            font = self._font(theme, min_size, bold=True)
            lines = self._wrap_lines(draw, title, font, max_width, max_lines=2)
        spacing = max(4, int(getattr(font, "size", min_size) * 0.18))
        text = "\n".join(lines)
        left, top, right, bottom = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
        text_height = max(1, bottom - top)
        min_height = max(48, int(width * 0.08))
        bar_height = max(min_height, text_height + vertical_padding * 2)
        title_bar = Image.new("RGBA", (width, bar_height), TITLE_BAR_BACKGROUND)
        title_draw = ImageDraw.Draw(title_bar, "RGBA")
        self._draw_multiline_centered(
            title_draw,
            (horizontal_padding, vertical_padding, width - horizontal_padding, bar_height - vertical_padding),
            lines,
            font,
            TITLE_BAR_TEXT_COLOR,
            spacing=spacing,
        )
        return title_bar

    def _title_text(self, value: str) -> str:
        title = re.sub(r"\s+", " ", (value or "").strip()) or "Iceberg"
        if len(title) <= ICEBERG_TITLE_MAX_LENGTH:
            return title
        return title[: ICEBERG_TITLE_MAX_LENGTH - 3].rstrip() + "..."

    def _draw_layer_labels(self, image: Image.Image, layer_boxes: list[LayerRenderBox], theme: ThemeConfig) -> None:
        draw = ImageDraw.Draw(image, "RGBA")
        label_left = self._item_safe_box(image.width)[1]
        label_width = max(1, image.width - label_left)
        padding_x = max(6, int(label_width * 0.08))
        for index, layer_box in enumerate(layer_boxes):
            top_y = max(0, layer_box.top_y)
            bottom_y = min(image.height, layer_box.bottom_y)
            if bottom_y <= top_y:
                continue
            padding_y = max(4, min(24, int((bottom_y - top_y) * 0.12)))
            box = (
                label_left + padding_x,
                top_y + padding_y,
                image.width - padding_x,
                bottom_y - padding_y,
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            label = default_layer_name(index)
            max_width = max(1, box[2] - box[0])
            max_height = max(1, box[3] - box[1])
            start_size = max(10, min(theme.layer_label_font_size, int(max_height * 0.45), int(label_width * 0.22)))
            min_size = max(8, min(16, start_size))
            font, lines, spacing = self._fit_multiline_text(
                draw,
                label,
                theme,
                start_size=start_size,
                max_width=max_width,
                max_height=max_height,
                min_size=min_size,
                max_lines=2,
            )
            fill, stroke = self._label_text_colors(image.crop((label_left, top_y, image.width, bottom_y)))
            self._draw_multiline_centered(
                draw,
                box,
                lines,
                font,
                fill,
                spacing=spacing,
                stroke_width=2,
                stroke_fill=stroke,
            )

    def _label_text_colors(self, region: Image.Image) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        sample = region.resize((1, 1), Image.Resampling.BOX).convert("RGBA").getpixel((0, 0))
        r, g, b, _ = sample
        luminance = (0.299 * r) + (0.587 * g) + (0.114 * b)
        if luminance < 150:
            return (255, 255, 255, 255), (0, 0, 0, 175)
        return (14, 18, 24, 255), (255, 255, 255, 190)

    def _fit_multiline_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        theme: ThemeConfig,
        *,
        start_size: int,
        max_width: int,
        max_height: int,
        min_size: int,
        max_lines: int,
    ) -> tuple[ImageFont.ImageFont, list[str], int]:
        for size in range(start_size, min_size - 1, -2):
            font = self._font(theme, size, bold=True)
            lines = self._wrap_lines(draw, text, font, max_width, max_lines=max_lines)
            spacing = max(2, int(getattr(font, "size", size) * 0.12))
            left, top, right, bottom = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font, spacing=spacing, align="center")
            if right - left <= max_width and bottom - top <= max_height:
                return font, lines, spacing
        font = self._font(theme, min_size, bold=True)
        spacing = max(2, int(getattr(font, "size", min_size) * 0.12))
        return font, [self._clip_text(draw, text, font, max_width)], spacing

    def _resize_preview(self, image: Image.Image) -> Image.Image:
        if image.width <= PREVIEW_MAX_WIDTH:
            return image
        ratio = PREVIEW_MAX_WIDTH / image.width
        preview_height = max(1, int(round(image.height * ratio)))
        return image.resize((PREVIEW_MAX_WIDTH, preview_height), Image.Resampling.LANCZOS)

    def _draw_items_for_layer(
        self,
        image: Image.Image,
        project: IcebergProject,
        layer_box: LayerRenderBox,
        items: list[ItemConfig],
        asset_bytes_by_item_id: dict[str, bytes],
    ) -> None:
        if not items:
            return

        # Sort items by z_index first, then by sort_order before calculating layout
        sorted_items = sorted(items, key=lambda i: (i.placement.z_index, i.sort_order))

        auto_positions = self._auto_positions(project, layer_box, sorted_items)

        for item in sorted_items:
            box = auto_positions.get(item.id)
            if item.placement.x is not None and item.placement.y is not None:
                box = self._manual_item_box(project.theme, layer_box, item)
            if box is None:
                continue
            if item.placement.scale != 1.0:
                box = self._scale_box(box, item.placement.scale, layer_box)
            self._draw_item(image, project.theme, item, box, asset_bytes_by_item_id.get(item.id))

    def _auto_positions(
        self,
        project: IcebergProject,
        layer_box: LayerRenderBox,
        items: list[ItemConfig],
    ) -> dict[str, tuple[int, int, int, int]]:
        scales = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3]
        layout_mode = getattr(layer_box.layer, "layout_mode", LayerLayoutMode.SCATTER)

        for scale in scales:
            if layout_mode == LayerLayoutMode.GRID:
                result = self._grid_layout(project.theme, layer_box, items, scale)
            elif layout_mode == LayerLayoutMode.MASONRY:
                result = self._masonry_layout(project.theme, layer_box, items, scale)
            else:
                result = self._scatter_layout(project.id, project.theme, layer_box, items, scale)

            if result is not None:
                return result

        raise ValueError("A camada não comporta os itens. Diminua a quantidade ou mude o layout.")

    def _grid_layout(
        self,
        theme: ThemeConfig,
        layer_box: LayerRenderBox,
        items: list[ItemConfig],
        scale: float,
    ) -> dict[str, tuple[int, int, int, int]] | None:
        content_left = layer_box.min_left + theme.layer_inner_padding_x
        content_right = layer_box.max_right - theme.layer_inner_padding_x
        content_top = layer_box.top_y + theme.layer_inner_padding_y
        content_bottom = layer_box.bottom_y - theme.layer_inner_padding_y
        if content_right <= content_left or content_bottom <= content_top:
            return None

        result: dict[str, tuple[int, int, int, int]] = {}
        cursor_x = content_left
        cursor_y = content_top
        line_height = 0

        for item in items:
            orig_w, orig_h = self._item_size(theme, item)
            item_w = max(10, int(orig_w * scale))
            item_h = max(10, int(orig_h * scale))

            if cursor_x > content_left and cursor_x + item_w > content_right:
                cursor_x = content_left
                cursor_y += line_height + int(theme.item_gap_y * scale)
                line_height = 0

            if cursor_y + item_h > content_bottom:
                return None

            result[item.id] = (cursor_x, cursor_y, cursor_x + item_w, cursor_y + item_h)
            cursor_x += item_w + int(theme.item_gap_x * scale)
            line_height = max(line_height, item_h)

        return result

    def _masonry_layout(
        self,
        theme: ThemeConfig,
        layer_box: LayerRenderBox,
        items: list[ItemConfig],
        scale: float,
    ) -> dict[str, tuple[int, int, int, int]] | None:
        content_left = layer_box.min_left + theme.layer_inner_padding_x
        content_right = layer_box.max_right - theme.layer_inner_padding_x
        content_top = layer_box.top_y + theme.layer_inner_padding_y
        content_bottom = layer_box.bottom_y - theme.layer_inner_padding_y
        available_width = content_right - content_left
        if available_width <= 0 or content_bottom <= content_top:
            return None

        # Determine number of columns based on average item width
        avg_w = sum(max(10, int(self._item_size(theme, i)[0] * scale)) for i in items) / len(items) if items else 100
        cols = max(1, available_width // (int(avg_w) + int(theme.item_gap_x * scale)))

        col_heights = [content_top] * cols
        col_widths = available_width // cols
        gap_x = int(theme.item_gap_x * scale)
        gap_y = int(theme.item_gap_y * scale)

        result: dict[str, tuple[int, int, int, int]] = {}
        for item in items:
            orig_w, orig_h = self._item_size(theme, item)
            item_w = max(10, int(orig_w * scale))
            item_h = max(10, int(orig_h * scale))

            # Find shortest column
            min_col_idx = col_heights.index(min(col_heights))
            x1 = content_left + min_col_idx * col_widths + (col_widths - item_w) // 2
            y1 = col_heights[min_col_idx]

            # Constrain to box
            x1 = max(content_left, min(content_right - item_w, x1))
            if y1 + item_h > content_bottom:
                return None

            result[item.id] = (x1, y1, x1 + item_w, y1 + item_h)
            col_heights[min_col_idx] = y1 + item_h + gap_y

        return result

    def _scatter_layout(
        self,
        project_id: str,
        theme: ThemeConfig,
        layer_box: LayerRenderBox,
        items: list[ItemConfig],
        scale: float,
    ) -> dict[str, tuple[int, int, int, int]] | None:
        content_left = layer_box.min_left + theme.layer_inner_padding_x
        content_right = layer_box.max_right - theme.layer_inner_padding_x
        content_top = layer_box.top_y + theme.layer_inner_padding_y
        content_bottom = layer_box.bottom_y - theme.layer_inner_padding_y
        available_width = content_right - content_left
        available_height = content_bottom - content_top

        if available_width <= 0 or available_height <= 0:
            return None

        result: dict[str, tuple[int, int, int, int]] = {}
        placed_boxes: list[tuple[int, int, int, int]] = []

        for item in items:
            orig_w, orig_h = self._item_size(theme, item)
            item_w = max(10, int(orig_w * scale))
            item_h = max(10, int(orig_h * scale))

            if item_w > available_width or item_h > available_height:
                return None

            rng = random.Random(f"{project_id}-{layer_box.layer.id}-{item.id}")
            placed = False
            for _ in range(100):  # max attempts per item
                x1 = rng.randint(content_left, content_right - item_w)
                y1 = rng.randint(content_top, content_bottom - item_h)
                x2 = x1 + item_w
                y2 = y1 + item_h

                # Check collision with already placed boxes
                collision = False
                for bx1, by1, bx2, by2 in placed_boxes:
                    if not (x2 <= bx1 or x1 >= bx2 or y2 <= by1 or y1 >= by2):
                        collision = True
                        break

                if not collision:
                    placed_boxes.append((x1, y1, x2, y2))
                    result[item.id] = (x1, y1, x2, y2)
                    placed = True
                    break

            if not placed:
                return None

        return result

    def _manual_item_box(self, theme: ThemeConfig, layer_box: LayerRenderBox, item: ItemConfig) -> tuple[int, int, int, int]:
        item_w, item_h = self._item_size(theme, item)
        content_left = layer_box.min_left + theme.layer_inner_padding_x
        content_right = layer_box.max_right - theme.layer_inner_padding_x
        content_top = layer_box.top_y + theme.layer_inner_padding_y
        content_bottom = layer_box.bottom_y - theme.layer_inner_padding_y
        available_w = max(1, content_right - content_left - item_w)
        available_h = max(1, content_bottom - content_top - item_h)
        x = content_left + int((item.placement.x or 0.5) * available_w)
        y = content_top + int((item.placement.y or 0.5) * available_h)
        return x, y, x + item_w, y + item_h

    def _draw_item(
        self,
        image: Image.Image,
        theme: ThemeConfig,
        item: ItemConfig,
        box: tuple[int, int, int, int],
        image_bytes: bytes | None,
    ) -> None:
        if item.display_style is ItemDisplayStyle.CHIP or item.source.type is ItemSourceType.TEXT or not image_bytes:
            self._draw_chip_item(image, theme, item, box, image_bytes)
            return
        if item.display_style is ItemDisplayStyle.STICKER:
            self._draw_sticker_item(image, theme, item, box, image_bytes)
            return
        self._draw_card_item(image, theme, item, box, image_bytes)

    def _draw_card_item(
        self,
        image: Image.Image,
        theme: ThemeConfig,
        item: ItemConfig,
        box: tuple[int, int, int, int],
        image_bytes: bytes,
    ) -> None:
        x1, y1, x2, y2 = box
        width, height = x2 - x1, y2 - y1
        show_caption = _has_caption(item)
        caption_h = 42 if show_caption else 0
        card = Image.new("RGBA", (width, height), theme.item_fill_color)
        draw = ImageDraw.Draw(card, "RGBA")
        try:
            raw = crop_center_square(self._open_image(image_bytes))
            fitted = ImageOps.fit(raw, (width, max(1, height - caption_h)), method=Image.Resampling.LANCZOS)
            card.paste(fitted, (0, 0), fitted if fitted.mode == "RGBA" else None)
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            LOGGER.warning("iceberg_item_image_fallback item_id=%s source_type=%s error=%s", item.id, item.source.type, exc)
            self._draw_missing_image(draw, (0, 0, width, max(1, height - caption_h)), theme)
        if show_caption:
            draw.rectangle((0, height - caption_h, width, height), fill=theme.item_fill_color)
            font = self._fit_font(draw, item.title, theme, start_size=theme.item_caption_font_size, max_width=width - theme.item_padding_x, min_size=11, bold=True)
            clipped = self._clip_text(draw, item.title, font, width - theme.item_padding_x)
            self._draw_centered_text(draw, (0, height - caption_h, width, height), clipped, font, theme.item_text_color)
        card = self._rounded(card, theme.item_radius)
        border = ImageDraw.Draw(card, "RGBA")
        border.rounded_rectangle((0, 0, width - 1, height - 1), radius=theme.item_radius, outline=theme.item_outline_color, width=2)
        self._paste_transformed(image, card, item.placement, (x1, y1))

    def _draw_sticker_item(
        self,
        image: Image.Image,
        theme: ThemeConfig,
        item: ItemConfig,
        box: tuple[int, int, int, int],
        image_bytes: bytes,
    ) -> None:
        x1, y1, x2, y2 = box
        width, height = x2 - x1, y2 - y1
        show_caption = _has_caption(item)
        caption_h = 28 if show_caption else 0
        try:
            raw = crop_center_square(self._open_image(image_bytes))
            sticker = ImageOps.fit(raw, (width, max(1, height - caption_h)), method=Image.Resampling.LANCZOS)
            sticker = self._rounded(sticker, theme.item_radius)

            if show_caption:
                # If there's a caption, we need to compose the sticker and text together first
                composed = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                composed.paste(sticker, (0, 0), sticker)
                draw = ImageDraw.Draw(composed, "RGBA")
                font = self._fit_font(draw, item.title, theme, start_size=theme.item_caption_font_size, max_width=width, min_size=11, bold=True)
                clipped = self._clip_text(draw, item.title, font, width)
                self._draw_centered_text(draw, (0, height - caption_h, width, height), clipped, font, theme.item_text_color, stroke_width=theme.item_stroke_width, stroke_fill=theme.item_stroke_color)
                self._paste_transformed(image, composed, item.placement, (x1, y1))
            else:
                self._paste_transformed(image, sticker, item.placement, (x1, y1))

        except (OSError, ValueError, UnidentifiedImageError) as exc:
            LOGGER.warning("iceberg_sticker_fallback item_id=%s error=%s", item.id, exc)
            self._draw_chip_item(image, theme, item, box, None)
            return
    def _draw_chip_item(
        self,
        image: Image.Image,
        theme: ThemeConfig,
        item: ItemConfig,
        box: tuple[int, int, int, int],
        image_bytes: bytes | None,
    ) -> None:
        x1, y1, x2, y2 = box
        width, height = x2 - x1, y2 - y1
        chip = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(chip, "RGBA")
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=min(theme.item_radius, height // 2), fill=theme.item_fill_color, outline=theme.item_outline_color, width=2)
        text_left = theme.item_padding_x
        if image_bytes:
            thumb_size = max(34, height - theme.item_padding_y * 2)
            try:
                raw = crop_center_square(self._open_image(image_bytes))
                thumb = ImageOps.fit(raw, (thumb_size, thumb_size), method=Image.Resampling.LANCZOS)
                thumb = self._rounded(thumb, max(8, theme.item_radius - 4))
                chip.paste(thumb, (theme.item_padding_x, (height - thumb_size) // 2), thumb)
                text_left += thumb_size + theme.item_padding_x
            except (OSError, ValueError, UnidentifiedImageError):
                pass
        font = self._fit_font(draw, item.title, theme, start_size=theme.item_font_size, max_width=max(20, width - text_left - theme.item_padding_x), min_size=11, bold=True)
        lines = self._wrap_lines(draw, item.title, font, max(20, width - text_left - theme.item_padding_x), max_lines=2)
        text = "\n".join(lines)
        _, top, _, bottom = draw.multiline_textbbox((0, 0), text, font=font, spacing=3)
        draw.multiline_text((text_left, (height - (bottom - top)) / 2 - top), text, font=font, fill=theme.item_text_color, spacing=3, align="left", stroke_width=theme.item_stroke_width, stroke_fill=theme.item_stroke_color)
        self._paste_transformed(image, chip, item.placement, (x1, y1))

    def _paste_transformed(self, target: Image.Image, source: Image.Image, placement: Any, position: tuple[int, int]) -> None:
        result = source
        if placement.rotation != 0.0:
            result = result.rotate(-placement.rotation, resample=Image.Resampling.BICUBIC, expand=True)
            # Adjust position so it rotates around its center
            offset_x = (result.width - source.width) // 2
            offset_y = (result.height - source.height) // 2
            position = (position[0] - offset_x, position[1] - offset_y)

        if placement.opacity < 1.0:
            alpha = result.getchannel("A")
            alpha = alpha.point(lambda p: int(p * placement.opacity))
            result.putalpha(alpha)

        target.paste(result, position, result)

    def _draw_missing_image(self, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], theme: ThemeConfig) -> None:
        draw.rectangle(box, fill=(205, 231, 242, 255))
        x1, y1, x2, y2 = box
        draw.line((x1, y1, x2, y2), fill=theme.item_outline_color, width=3)
        draw.line((x2, y1, x1, y2), fill=theme.item_outline_color, width=3)

    def _build_layer_boxes(
        self,
        project: IcebergProject,
        *,
        bounds: list[int],
        item_safe_box: tuple[int, int],
    ) -> list[LayerRenderBox]:
        layers = project.ordered_layers()
        safe_left, safe_right = item_safe_box
        safe_width = max(1, safe_right - safe_left)
        boxes: list[LayerRenderBox] = []
        for index, layer in enumerate(layers):
            top_y = bounds[index]
            bottom_y = bounds[index + 1]
            boxes.append(
                LayerRenderBox(
                    layer=layer,
                    index=index,
                    top_y=top_y,
                    bottom_y=bottom_y,
                    top_width=safe_width,
                    bottom_width=safe_width,
                    polygon=((safe_left, top_y), (safe_right, top_y), (safe_right, bottom_y), (safe_left, bottom_y)),
                )
            )
        return boxes

    def _item_size(self, theme: ThemeConfig, item: ItemConfig) -> tuple[int, int]:
        if item.display_style is ItemDisplayStyle.CHIP or item.source.type is ItemSourceType.TEXT:
            estimated_chars = max(1, len(item.title))
            width = min(theme.item_card_width * 2, max(theme.item_chip_min_width, estimated_chars * 11 + theme.item_padding_x * 2))
            return width, theme.item_chip_height
        return theme.item_card_width, theme.item_card_height

    def _scale_box(self, box: tuple[int, int, int, int], scale: float, layer_box: LayerRenderBox) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        scaled_w = max(40, int(width * scale))
        scaled_h = max(32, int(height * scale))
        return (
            max(layer_box.min_left, center_x - scaled_w // 2),
            max(layer_box.top_y, center_y - scaled_h // 2),
            min(layer_box.max_right, center_x + scaled_w // 2),
            min(layer_box.bottom_y, center_y + scaled_h // 2),
        )

    def _open_image(self, image_bytes: bytes) -> Image.Image:
        if not image_bytes:
            raise ValueError("imagem vazia")
        with Image.open(io.BytesIO(image_bytes)) as raw:
            try:
                raw.seek(0)
            except EOFError:
                pass
            return raw.convert("RGBA")

    def _rounded(self, source: Image.Image, radius: int) -> Image.Image:
        rounded = source.convert("RGBA")
        mask = self._rounded_mask(rounded.size, radius)
        alpha = rounded.getchannel("A")
        rounded.putalpha(ImageChops.multiply(alpha, mask))
        return rounded

    def _rounded_mask(self, size: tuple[int, int], radius: int) -> Image.Image:
        key = (size[0], size[1], radius)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached.copy()
        hi_size = (size[0] * MASK_SCALE, size[1] * MASK_SCALE)
        mask = Image.new("L", hi_size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, hi_size[0] - 1, hi_size[1] - 1), radius=radius * MASK_SCALE, fill=255)
        mask = mask.resize(size, Image.Resampling.LANCZOS)
        self._mask_cache[key] = mask
        return mask.copy()

    def _font(self, theme: ThemeConfig, size: int, *, bold: bool) -> ImageFont.ImageFont:
        filename = theme.font_bold if bold else theme.font_regular
        cache_key = (filename, size)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached
        candidates = [
            self.font_dir / filename,
            self.font_dir / "Poppins-Bold.ttf",
            self.font_dir / "Poppins-Regular.ttf",
            pathlib.Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
        for candidate in candidates:
            try:
                font = ImageFont.truetype(str(candidate), size)
                self._font_cache[cache_key] = font
                return font
            except (OSError, ValueError):
                continue
        font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font

    def _fit_font(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        theme: ThemeConfig,
        *,
        start_size: int,
        max_width: int,
        min_size: int,
        bold: bool,
    ) -> ImageFont.ImageFont:
        for size in range(start_size, min_size - 1, -2):
            font = self._font(theme, size, bold=bold)
            if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
                return font
        return self._font(theme, min_size, bold=bold)

    def _draw_centered_text(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        text: str,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
        stroke_width: int = 0,
        stroke_fill: tuple[int, int, int, int] | None = None,
    ) -> None:
        x, y = self._centered_text_pos(draw, box, text, font)
        kwargs = {"font": font, "fill": fill}
        if stroke_width > 0 and stroke_fill:
            kwargs["stroke_width"] = stroke_width
            kwargs["stroke_fill"] = stroke_fill
        draw.text((x, y), text, **kwargs)

    def _draw_multiline_centered(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        lines: list[str],
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int, int],
        *,
        spacing: int,
        stroke_width: int = 0,
        stroke_fill: tuple[int, int, int, int] | None = None,
    ) -> None:
        text = "\n".join(lines)
        left, top, right, bottom = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
        text_w = right - left
        text_h = bottom - top
        x1, y1, x2, y2 = box
        x = int(x1 + (x2 - x1 - text_w) / 2 - left)
        y = int(y1 + (y2 - y1 - text_h) / 2 - top)
        kwargs = {"font": font, "fill": fill, "spacing": spacing, "align": "center"}
        if stroke_width > 0 and stroke_fill:
            kwargs["stroke_width"] = stroke_width
            kwargs["stroke_fill"] = stroke_fill
        draw.multiline_text((x, y), text, **kwargs)

    def _centered_text_pos(
        self,
        draw: ImageDraw.ImageDraw,
        box: tuple[int, int, int, int],
        text: str,
        font: ImageFont.ImageFont,
    ) -> tuple[int, int]:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        text_w = right - left
        text_h = bottom - top
        x1, y1, x2, y2 = box
        return int(x1 + (x2 - x1 - text_w) / 2 - left), int(y1 + (y2 - y1 - text_h) / 2 - top)

    def _wrap_lines(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        max_width: int,
        *,
        max_lines: int = MAX_TEXT_LINES,
    ) -> list[str]:
        words = textwrap.wrap(re.sub(r"\s+", " ", text).strip(), width=22, break_long_words=False, break_on_hyphens=False) or [text]
        lines: list[str] = []
        for candidate in words:
            line = candidate
            while line and draw.textbbox((0, 0), line, font=font)[2] > max_width:
                line = line[:-1].rstrip()
            if line:
                lines.append(line if line == candidate else f"{line}...")
            if len(lines) >= max_lines:
                break
        return lines or [text[:18]]

    def _clip_text(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
        value = re.sub(r"\s+", " ", text).strip()
        if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
            return value
        suffix = "..."
        while value and draw.textbbox((0, 0), value + suffix, font=font)[2] > max_width:
            value = value[:-1]
        return value + suffix if value else suffix
