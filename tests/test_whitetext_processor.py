from __future__ import annotations

import io
import unittest

from PIL import Image

from modules.media_processing.whitetext.errors import ImageTooLargeError, InvalidImageError, InvalidTextError, LayoutComputationError, OutputTooLargeError, WhitetextUnsupportedMediaError
from modules.media_processing.whitetext.layout import CaptionStyle, compute_caption_layout
from modules.media_processing.whitetext.processor import (
    MediaKind,
    WhiteTextProcessorConfig,
    detect_image_format,
    detect_media_kind,
    extract_gif_metadata,
    is_animated_gif,
    open_static_image,
    process_gif_bytes,
    process_media,
    process_single_gif_frame,
    process_static_image_bytes,
    render_caption_canvas,
    validate_gif,
)


def image_bytes(format_name: str, *, size: tuple[int, int] = (80, 50), color: tuple[int, int, int, int] = (20, 130, 220, 255)) -> bytes:
    image = Image.new("RGBA", size, color)
    if format_name.upper() in {"JPEG", "JPG"}:
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format=format_name)
    return buffer.getvalue()


def animated_gif_bytes(
    *,
    size: tuple[int, int] = (120, 80),
    durations: list[int] | None = None,
    loop: int = 0,
    frame_count: int = 2,
) -> bytes:
    durations = durations or [80, 180]
    frames = []
    for index in range(frame_count):
        color = (255, 0, 0, 255) if index % 2 == 0 else (0, 0, 255, 255)
        frames.append(Image.new("RGBA", size, color))
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations[:frame_count],
        loop=loop,
        disposal=2,
    )
    return buffer.getvalue()


def single_frame_gif_bytes() -> bytes:
    image = Image.new("RGBA", (120, 80), (10, 20, 30, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="GIF")
    return buffer.getvalue()


class WhitetextStaticProcessorTests(unittest.TestCase):
    def test_detect_image_format_uses_real_payload(self) -> None:
        self.assertEqual(detect_image_format(image_bytes("PNG")), "PNG")
        self.assertEqual(detect_image_format(image_bytes("JPEG")), "JPEG")

    def test_detect_image_format_rejects_fake_png(self) -> None:
        with self.assertRaises(InvalidImageError):
            detect_image_format(b"MZ fake executable renamed to png")

    def test_open_static_image_normalizes_jpeg_to_rgb(self) -> None:
        image = open_static_image(image_bytes("JPEG", size=(120, 80)))

        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (120, 80))

    def test_process_static_png_places_original_below_caption(self) -> None:
        raw = image_bytes("PNG", size=(400, 359), color=(10, 200, 40, 255))
        caption = compute_caption_layout("talitinha tá no andar de cima eu:", 400)

        buffer, filename = process_static_image_bytes(raw, "talitinha tá no andar de cima eu:")
        rendered = Image.open(buffer).convert("RGBA")

        self.assertEqual(filename, "whitetext.png")
        self.assertEqual(rendered.width, 400)
        self.assertEqual(rendered.height, 359 + caption.caption_height)
        self.assertEqual(rendered.getpixel((0, 0)), (255, 255, 255, 255))
        self.assertEqual(rendered.getpixel((20, caption.caption_height + 20)), (10, 200, 40, 255))

    def test_process_static_jpeg_and_webp(self) -> None:
        for format_name in ("JPEG", "WEBP"):
            with self.subTest(format_name=format_name):
                buffer, _ = process_static_image_bytes(image_bytes(format_name, size=(160, 90)), "eu:")
                rendered = Image.open(buffer)
                self.assertEqual(rendered.format, "PNG")
                self.assertEqual(rendered.width, 160)
                self.assertGreater(rendered.height, 90)

    def test_png_transparency_stays_rgba_with_opaque_caption(self) -> None:
        image = Image.new("RGBA", (120, 80), (200, 10, 10, 0))
        image.putpixel((30, 30), (20, 40, 60, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        output, _ = process_static_image_bytes(buffer.getvalue(), "alpha")
        rendered = Image.open(output).convert("RGBA")
        caption_height = rendered.height - 80

        self.assertEqual(rendered.getpixel((0, 0)), (255, 255, 255, 255))
        self.assertEqual(rendered.getpixel((30, caption_height + 30)), (20, 40, 60, 255))
        self.assertEqual(rendered.getpixel((10, caption_height))[3], 0)
        self.assertEqual(rendered.getpixel((10, caption_height + 10))[3], 0)

    def test_text_short_long_and_giant_word_work_within_limit(self) -> None:
        raw = image_bytes("PNG", size=(240, 120))
        for text in ("eu:", "isso é um texto longo dentro do limite " * 8, "A" * 48):
            with self.subTest(text=text[:10]):
                buffer, _ = process_static_image_bytes(raw, text)
                rendered = Image.open(buffer)
                self.assertEqual(rendered.width, 240)
                self.assertGreater(rendered.height, 120)

    def test_small_or_thin_images_fail_with_custom_error(self) -> None:
        with self.assertRaises(ImageTooLargeError):
            process_static_image_bytes(image_bytes("PNG", size=(10, 2000)), "x")
        with self.assertRaises(ImageTooLargeError):
            process_static_image_bytes(image_bytes("PNG", size=(2000, 10)), "x")

    def test_corrupt_image_fails_friendly(self) -> None:
        with self.assertRaises(InvalidImageError):
            process_static_image_bytes(b"not really an image", "x")

    def test_output_limit_is_enforced(self) -> None:
        config = WhiteTextProcessorConfig(max_output_bytes=32)
        with self.assertRaises(OutputTooLargeError):
            process_static_image_bytes(image_bytes("PNG", size=(120, 80)), "x", config=config)

    def test_render_caption_canvas_preserves_width_and_offsets(self) -> None:
        image = Image.new("RGB", (160, 90), (3, 4, 5))
        rendered = render_caption_canvas(image, "caption")
        caption_height = rendered.height - image.height

        self.assertEqual(rendered.width, image.width)
        self.assertGreater(caption_height, 0)
        self.assertEqual(rendered.getpixel((8, caption_height + 8)), (3, 4, 5))


class WhitetextProcessorCompatTests(unittest.TestCase):
    def test_static_image_returns_png_with_original_below_caption(self) -> None:
        raw = image_bytes("PNG", size=(120, 80), color=(10, 200, 40, 255))

        result = process_media(raw, filename="meme.png", content_type="image/png", text="top text")
        rendered = Image.open(result.buffer).convert("RGBA")
        bar_height = rendered.height - 80

        self.assertEqual(result.content_type, "image/png")
        self.assertTrue(result.filename.endswith("-whitetext.png"))
        self.assertEqual(rendered.width, 120)
        self.assertGreater(bar_height, 0)
        self.assertEqual(rendered.getpixel((3, 3)), (255, 255, 255, 255))
        self.assertEqual(rendered.getpixel((8, bar_height + 8)), (10, 200, 40, 255))

    def test_static_thin_images_are_rejected(self) -> None:
        with self.assertRaises(ImageTooLargeError):
            process_media(image_bytes("PNG", size=(40, 2000)), filename="tall.png", content_type="image/png", text="A")
        with self.assertRaises(ImageTooLargeError):
            process_media(image_bytes("PNG", size=(2000, 10)), filename="wide.png", content_type="image/png", text="A")

    def test_gif_preserves_frame_count_and_durations(self) -> None:
        result = process_media(animated_gif_bytes(), filename="anim.gif", content_type="image/gif", text="gif text")
        output = Image.open(result.buffer)

        self.assertEqual(result.content_type, "image/gif")
        self.assertEqual(getattr(output, "n_frames", 1), 2)
        output.seek(0)
        self.assertEqual(output.info.get("duration"), 80)
        output.seek(1)
        self.assertEqual(output.info.get("duration"), 180)
        self.assertEqual(output.width, 120)
        self.assertGreater(output.height, 80)

    def test_false_image_extension_with_invalid_bytes_is_rejected(self) -> None:
        with self.assertRaises(WhitetextUnsupportedMediaError):
            process_media(b"not really an image", filename="fake.png", content_type="image/png", text="x")

    def test_empty_text_is_rejected(self) -> None:
        with self.assertRaises(InvalidTextError):
            process_media(image_bytes("PNG"), filename="meme.png", content_type="image/png", text="   ")

    def test_mp4_magic_is_detected_even_with_false_extension(self) -> None:
        fake_mp4 = b"\x00\x00\x00\x18ftypisom" + (b"\x00" * 32)

        self.assertEqual(detect_media_kind(fake_mp4, filename="fake.png", content_type="image/png"), MediaKind.VIDEO)


class WhitetextGifProcessorTests(unittest.TestCase):
    def test_is_animated_gif(self) -> None:
        self.assertTrue(is_animated_gif(animated_gif_bytes()))
        self.assertFalse(is_animated_gif(single_frame_gif_bytes()))

    def test_is_animated_gif_rejects_corrupt_image(self) -> None:
        with self.assertRaises(InvalidImageError):
            is_animated_gif(b"not a gif")

    def test_extract_gif_metadata_collects_loop_durations_and_transparency(self) -> None:
        raw = animated_gif_bytes(durations=[70, 190], loop=3)
        with Image.open(io.BytesIO(raw)) as image:
            metadata = extract_gif_metadata(image)

        self.assertEqual(metadata["width"], 120)
        self.assertEqual(metadata["height"], 80)
        self.assertEqual(metadata["n_frames"], 2)
        self.assertEqual(metadata["loop"], 3)
        self.assertEqual(metadata["durations"], [70, 190])
        self.assertIn("mode", metadata)
        self.assertIn("transparency", metadata)

    def test_extract_gif_metadata_uses_default_duration_when_missing(self) -> None:
        raw = single_frame_gif_bytes()
        with Image.open(io.BytesIO(raw)) as image:
            metadata = extract_gif_metadata(image)

        self.assertEqual(metadata["durations"], [100])

    def test_validate_gif_rejects_too_many_frames(self) -> None:
        raw = animated_gif_bytes(frame_count=5, durations=[50] * 5)
        config = WhiteTextProcessorConfig(max_gif_frames=4)

        with Image.open(io.BytesIO(raw)) as image:
            with self.assertRaises(ImageTooLargeError):
                validate_gif(image, config)

    def test_validate_gif_rejects_total_pixels(self) -> None:
        raw = animated_gif_bytes(size=(120, 80), frame_count=3, durations=[50, 50, 50])
        config = WhiteTextProcessorConfig(max_gif_total_pixels=120 * 80 * 2)

        with Image.open(io.BytesIO(raw)) as image:
            with self.assertRaises(ImageTooLargeError):
                validate_gif(image, config)

    def test_validate_gif_rejects_too_narrow_gif(self) -> None:
        raw = animated_gif_bytes(size=(40, 80))

        with Image.open(io.BytesIO(raw)) as image:
            with self.assertRaises(ImageTooLargeError):
                validate_gif(image, WhiteTextProcessorConfig())

    def test_process_gif_bytes_preserves_animation_dimensions_durations_and_loop(self) -> None:
        raw = animated_gif_bytes(durations=[80, 180], loop=4)
        caption = compute_caption_layout("gif text", 120)

        buffer, filename = process_gif_bytes(raw, "gif text")
        output = Image.open(buffer)

        self.assertEqual(filename, "whitetext.gif")
        self.assertEqual(getattr(output, "n_frames", 1), 2)
        self.assertEqual(output.info.get("loop"), 4)
        self.assertEqual(output.width, 120)
        self.assertEqual(output.height, 80 + caption.caption_height)
        output.seek(0)
        self.assertEqual(output.info.get("duration"), 80)
        output.seek(1)
        self.assertEqual(output.info.get("duration"), 180)

    def test_process_gif_bytes_handles_single_frame_gif_as_one_frame_gif(self) -> None:
        buffer, filename = process_gif_bytes(single_frame_gif_bytes(), "one")
        output = Image.open(buffer)

        self.assertEqual(filename, "whitetext.gif")
        self.assertEqual(getattr(output, "n_frames", 1), 1)
        self.assertEqual(output.width, 120)
        self.assertGreater(output.height, 80)

    def test_process_gif_bytes_rejects_output_too_large(self) -> None:
        config = WhiteTextProcessorConfig(max_output_bytes=32)

        with self.assertRaises(OutputTooLargeError):
            process_gif_bytes(animated_gif_bytes(), "gif text", config=config)

    def test_process_gif_bytes_rejects_corrupt_gif(self) -> None:
        with self.assertRaises(InvalidImageError):
            process_gif_bytes(b"GIF89a broken", "gif text")

    def test_process_single_gif_frame_uses_precomputed_layout(self) -> None:
        layout = compute_caption_layout("same", 120)
        frame = Image.new("RGBA", (120, 80), (10, 40, 70, 255))

        rendered = process_single_gif_frame(frame, layout, CaptionStyle())

        self.assertEqual(rendered.width, 120)
        self.assertEqual(rendered.height, 80 + layout.caption_height)
        self.assertEqual(rendered.getpixel((12, layout.caption_height + 12)), (10, 40, 70, 255))

    def test_process_single_gif_frame_rejects_wrong_width(self) -> None:
        layout = compute_caption_layout("same", 120)
        frame = Image.new("RGBA", (80, 80), (10, 40, 70, 255))

        with self.assertRaises(LayoutComputationError):
            process_single_gif_frame(frame, layout, CaptionStyle())
