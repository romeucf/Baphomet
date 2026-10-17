from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from modules.media_processing.whitetext.errors import FontNotFoundError, InvalidTextError, LayoutComputationError, TextTooLongError
from modules.media_processing.whitetext.layout import (
    CaptionStyle,
    compute_caption_layout,
    compute_responsive_font_size,
    load_font,
    measure_text,
    normalize_caption_text,
    resolve_font_path,
    wrap_text_by_pixels,
)


class WhitetextLayoutTests(unittest.TestCase):
    def test_short_caption_on_400px_media_wraps_by_pixels(self) -> None:
        layout = compute_caption_layout("talitinha tá no andar de cima eu:", 400)

        self.assertGreaterEqual(len(layout.lines), 2)
        self.assertTrue(all(line.width <= layout.max_text_width for line in layout.lines))
        self.assertEqual(layout.caption_height, layout.text_block_height + 2 * layout.vertical_padding)

    def test_very_short_text_is_centered(self) -> None:
        layout = compute_caption_layout("eu:", 400)
        line = layout.lines[0]

        self.assertEqual(len(layout.lines), 1)
        self.assertLessEqual(abs((line.x + line.width / 2) - 200), 3)
        self.assertGreaterEqual(line.y, 0)

    def test_giant_word_breaks_by_character_without_overflow(self) -> None:
        layout = compute_caption_layout("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 180)

        self.assertGreater(len(layout.lines), 1)
        self.assertTrue(all(line.width <= layout.max_text_width for line in layout.lines))

    def test_manual_line_breaks_are_preserved(self) -> None:
        layout = compute_caption_layout("linha 1\nlinha 2", 400)

        self.assertEqual([line.text for line in layout.lines], ["linha 1", "linha 2"])

    def test_empty_text_raises_invalid_text(self) -> None:
        with self.assertRaises(InvalidTextError):
            compute_caption_layout("     ", 400)

    def test_very_long_text_raises_text_too_long(self) -> None:
        with self.assertRaises(TextTooLongError):
            compute_caption_layout("a" * 3000, 400)

    def test_tiny_media_width_raises_layout_error(self) -> None:
        with self.assertRaises(LayoutComputationError):
            compute_caption_layout("texto", 10)

    def test_large_media_width_respects_max_font_size(self) -> None:
        style = CaptionStyle(max_font_size=120)
        layout = compute_caption_layout("texto grande", 2000, style)

        self.assertEqual(layout.font_size, 120)
        self.assertEqual(compute_responsive_font_size(2000, style), 120)

    def test_normalize_preserves_accents_punctuation_emoji_and_manual_breaks(self) -> None:
        self.assertEqual(normalize_caption_text("  olá!!! 😭\r\nlinha 2  "), "olá!!! 😭\nlinha 2")

    def test_resolve_font_path_uses_required_font(self) -> None:
        path = resolve_font_path(None)

        self.assertEqual(path.name, "FuturaCEB.otf")

    def test_missing_font_raises_font_not_found(self) -> None:
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(FontNotFoundError):
                resolve_font_path(None)

    def test_wrap_text_by_pixels_rejects_width_that_cannot_fit_one_character(self) -> None:
        font_path = resolve_font_path(None)
        font = load_font(font_path, 42)
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        with self.assertRaises(LayoutComputationError):
            wrap_text_by_pixels("A", font, 1, draw)

    def test_measure_text_uses_real_bbox(self) -> None:
        font_path = resolve_font_path(None)
        font = load_font(font_path, 42)
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        width, height = measure_text(draw, "Ág", font)

        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
