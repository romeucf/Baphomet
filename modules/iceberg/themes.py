from __future__ import annotations

from copy import deepcopy

from modules.iceberg.constants import ICEBERG_DEFAULT_LAYERS, ICEBERG_MAX_LAYERS, ICEBERG_MIN_LAYERS
from modules.iceberg.models import LayerConfig, ThemeConfig


DEFAULT_THEME_ID = "classic_iceberg"
DEFAULT_LAYER_NAMES = (
    "Ponta",
    "Raso",
    "Submerso",
    "Profundo",
    "Abismo",
    "Zona Hadal",
    "Subglacial",
    "Núcleo do Iceberg",
    "Abismo Azul",
    "Fundo do Iceberg",
)


def default_layer_name(index: int) -> str:
    if 0 <= index < len(DEFAULT_LAYER_NAMES):
        return DEFAULT_LAYER_NAMES[index]
    return f"Camada {index + 1}"


def classic_iceberg_theme() -> ThemeConfig:
    return ThemeConfig(
        id=DEFAULT_THEME_ID,
        name="Iceberg classico",
        canvas_width=1200,
        canvas_height=1600,
        padding_x=96,
        title_top=42,
        title_height=118,
        iceberg_top_y=210,
        iceberg_bottom_y=1450,
        iceberg_center_x=600,
        iceberg_boundary_width_ratios=(0.18, 0.34, 0.52, 0.70, 0.92, 0.84, 0.68),
        sky_color=(164, 218, 241, 255),
        ocean_color=(56, 149, 196, 255),
        deep_ocean_color=(14, 47, 91, 255),
        horizon_color=(233, 249, 255, 185),
        iceberg_top_color=(250, 254, 255, 255),
        iceberg_bottom_color=(117, 205, 232, 255),
        iceberg_edge_color=(29, 99, 145, 220),
        layer_line_color=(31, 98, 143, 145),
        layer_label_color=(8, 34, 55, 230),
        title_color=(7, 31, 50, 255),
        item_fill_color=(249, 253, 255, 238),
        item_outline_color=(37, 92, 130, 170),
        item_text_color=(10, 32, 49, 255),
        item_muted_text_color=(42, 74, 97, 242),
        font_regular="Poppins-Regular.ttf",
        font_bold="Poppins-Bold.ttf",
        title_font_size=58,
        layer_label_font_size=25,
        item_font_size=22,
        item_caption_font_size=18,
        item_card_width=138,
        item_card_height=164,
        item_chip_min_width=132,
        item_chip_height=58,
        item_gap_x=18,
        item_gap_y=16,
        item_padding_x=18,
        item_padding_y=10,
        item_radius=14,
        layer_inner_padding_x=46,
        layer_inner_padding_y=24,
        footer_height=70,
        item_stroke_width=2,
        item_stroke_color=(255, 255, 255, 200),
    )


def classic_blue_theme() -> ThemeConfig:
    return ThemeConfig(
        id="classic_blue",
        name="Iceberg Noturno (Classic Blue)",
        canvas_width=1200,
        canvas_height=1600,
        padding_x=96,
        title_top=42,
        title_height=118,
        iceberg_top_y=210,
        iceberg_bottom_y=1450,
        iceberg_center_x=600,
        iceberg_boundary_width_ratios=(0.18, 0.34, 0.52, 0.70, 0.92, 0.84, 0.68),
        sky_color=(12, 19, 43, 255),
        ocean_color=(26, 45, 99, 255),
        deep_ocean_color=(9, 14, 30, 255),
        horizon_color=(68, 104, 191, 185),
        iceberg_top_color=(204, 218, 255, 255),
        iceberg_bottom_color=(79, 127, 240, 255),
        iceberg_edge_color=(36, 62, 128, 220),
        layer_line_color=(45, 78, 161, 145),
        layer_label_color=(200, 220, 255, 230),
        title_color=(220, 235, 255, 255),
        item_fill_color=(17, 32, 69, 238),
        item_outline_color=(68, 104, 191, 170),
        item_text_color=(220, 235, 255, 255),
        item_muted_text_color=(140, 165, 210, 242),
        font_regular="Poppins-Regular.ttf",
        font_bold="Poppins-Bold.ttf",
        title_font_size=58,
        layer_label_font_size=25,
        item_font_size=22,
        item_caption_font_size=18,
        item_card_width=138,
        item_card_height=164,
        item_chip_min_width=132,
        item_chip_height=58,
        item_gap_x=18,
        item_gap_y=16,
        item_padding_x=18,
        item_padding_y=10,
        item_radius=14,
        layer_inner_padding_x=46,
        layer_inner_padding_y=24,
        footer_height=70,
        item_stroke_width=2,
        item_stroke_color=(0, 0, 0, 200),
    )


THEME_PRESETS: dict[str, ThemeConfig] = {
    DEFAULT_THEME_ID: classic_iceberg_theme(),
    "classic_blue": classic_blue_theme(),
}


def get_theme(theme_id: str | None = None) -> ThemeConfig:
    key = (theme_id or DEFAULT_THEME_ID).strip().casefold()
    theme = THEME_PRESETS.get(key) or THEME_PRESETS[DEFAULT_THEME_ID]
    return deepcopy(theme)


def default_layers(count: int = ICEBERG_DEFAULT_LAYERS) -> list[LayerConfig]:
    safe_count = max(ICEBERG_MIN_LAYERS, min(ICEBERG_MAX_LAYERS, int(count or ICEBERG_DEFAULT_LAYERS)))
    layers: list[LayerConfig] = []
    for index in range(safe_count):
        layers.append(
            LayerConfig(
                id=f"layer-{index + 1}",
                name=default_layer_name(index),
                order=index,
                height_weight=1.0,
            )
        )
    return layers
