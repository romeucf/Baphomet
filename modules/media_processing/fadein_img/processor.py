from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from modules.media_processing.fadein_img.errors import (
    AnimatedImageNotSupportedError,
    ImageTooLargeError,
    InvalidImageError,
    OutputTooLargeError,
    UnsupportedMediaError,
)


SUPPORTED_IMAGE_FORMATS = {"PNG", "JPEG", "JPG", "WEBP", "GIF"}


@dataclass(frozen=True)
class FadeInImageConfig:
    max_input_bytes: int = 15 * 1024 * 1024
    max_output_bytes: int = 8 * 1024 * 1024
    min_width: int = 32
    min_height: int = 32
    max_width: int = 2000
    max_height: int = 2000
    max_pixels: int = 4_000_000
    fps: int = 20
    duration_seconds: float = 1.0
    output_filename: str = "fadein.gif"
    gif_optimize: bool = True
    enable_compression: bool = True
    compression_palette_colors: tuple[int, ...] = (256, 224, 192, 160, 128, 96, 64)
    compression_fps_values: tuple[int, ...] = (20, 18, 16, 15, 12, 10)
    compression_scale_factors: tuple[float, ...] = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5)


@dataclass(frozen=True)
class GifCompressionAttempt:
    fps: int
    scale: float
    colors: int | None


def validate_input_size(data: bytes, config: FadeInImageConfig) -> None:
    """Reject empty inputs or payloads above the configured byte limit."""

    if not data:
        raise InvalidImageError("A imagem enviada esta vazia.", code="image_empty")
    if len(data) > config.max_input_bytes:
        raise ImageTooLargeError(
            "Essa imagem e grande demais para processar com seguranca.",
            code="image_input_too_large",
        )


def open_and_validate_static_image(data: bytes, config: FadeInImageConfig) -> Image.Image:
    """Open, verify, orient, validate, and normalize an image to RGB."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = _normalized_format(image)
                if image_format not in SUPPORTED_IMAGE_FORMATS:
                    raise UnsupportedMediaError(
                        "Formato de imagem nao suportado.",
                        code="unsupported_image_format",
                    )
                if _is_animated(image):
                    raise AnimatedImageNotSupportedError(
                        "Imagem animada nao e suportada por este comando.",
                        code="animated_image_not_supported",
                    )
                image.verify()
    except (UnsupportedMediaError, AnimatedImageNotSupportedError):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageTooLargeError(
            "Essa imagem tem resolucao grande demais para processar com seguranca.",
            code="image_decompression_bomb",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError(
            "Nao consegui abrir essa imagem. Talvez o arquivo esteja corrompido.",
            code="image_invalid",
        ) from exc

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = _normalized_format(image)
                if image_format not in SUPPORTED_IMAGE_FORMATS:
                    raise UnsupportedMediaError(
                        "Formato de imagem nao suportado.",
                        code="unsupported_image_format",
                    )
                if _is_animated(image):
                    raise AnimatedImageNotSupportedError(
                        "Imagem animada nao e suportada por este comando.",
                        code="animated_image_not_supported",
                    )

                oriented = ImageOps.exif_transpose(image)
                oriented.load()
                validate_dimensions(oriented, config)
                return _flatten_transparency_to_black(oriented)
    except (UnsupportedMediaError, AnimatedImageNotSupportedError, ImageTooLargeError):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageTooLargeError(
            "Essa imagem tem resolucao grande demais para processar com seguranca.",
            code="image_decompression_bomb",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError(
            "Nao consegui abrir essa imagem. Talvez o arquivo esteja corrompido.",
            code="image_invalid",
        ) from exc


def validate_dimensions(image: Image.Image, config: FadeInImageConfig) -> None:
    """Validate image dimensions against configurable safety limits."""

    width, height = image.size
    if width < config.min_width:
        raise ImageTooLargeError(
            f"A imagem e estreita demais. Minimo: {config.min_width}px.",
            code="image_width_too_small",
        )
    if height < config.min_height:
        raise ImageTooLargeError(
            f"A imagem e baixa demais. Minimo: {config.min_height}px.",
            code="image_height_too_small",
        )
    if width > config.max_width:
        raise ImageTooLargeError(
            f"A imagem e larga demais. Maximo: {config.max_width}px.",
            code="image_width_too_large",
        )
    if height > config.max_height:
        raise ImageTooLargeError(
            f"A imagem e alta demais. Maximo: {config.max_height}px.",
            code="image_height_too_large",
        )
    if width * height > config.max_pixels:
        raise ImageTooLargeError(
            "Essa imagem tem pixels demais para processar com seguranca.",
            code="image_pixel_limit",
        )


def build_fadein_frames(image: Image.Image, config: FadeInImageConfig) -> list[Image.Image]:
    """Build linear fade-in frames from black to the original RGB image."""

    if image.mode != "RGB":
        image = image.convert("RGB")

    total_frames = _total_frames(config)
    black_frame = Image.new("RGB", image.size, (0, 0, 0))
    frames: list[Image.Image] = []

    for index in range(total_frames):
        if index == 0:
            frame = black_frame.copy()
        elif index == total_frames - 1:
            frame = image.copy()
        else:
            progress = index / (total_frames - 1)
            frame = Image.blend(black_frame, image, progress)
        frames.append(frame)

    return frames


def save_frames_as_gif(frames: list[Image.Image], config: FadeInImageConfig) -> io.BytesIO:
    """Save frames as a looping GIF, compressing only as much as needed."""

    if not frames:
        raise InvalidImageError("Nenhum frame foi gerado para o GIF.", code="gif_no_frames")

    attempts = [GifCompressionAttempt(fps=config.fps, scale=1.0, colors=None)]
    if config.enable_compression:
        attempts.extend(_compression_attempts(config))

    smallest_size: int | None = None
    for attempt in attempts:
        candidate_frames = _prepare_frames_for_attempt(frames, config, attempt)
        if not candidate_frames:
            continue
        output = _encode_gif(candidate_frames, config, attempt.colors)
        output_size = output.getbuffer().nbytes
        smallest_size = output_size if smallest_size is None else min(smallest_size, output_size)
        if output_size <= config.max_output_bytes:
            output.seek(0)
            return output

    detail = f"smallest={smallest_size} limit={config.max_output_bytes}"
    raise OutputTooLargeError(
        "O GIF final ficou grande demais para enviar mesmo depois da compressao.",
        code="output_too_large_after_compression",
        detail=detail,
    )


def _encode_gif(frames: list[Image.Image], config: FadeInImageConfig, colors: int | None) -> io.BytesIO:
    encoded_frames = _quantize_frames(frames, colors) if colors is not None else frames
    durations = _frame_durations_ms(config, len(encoded_frames))
    output = io.BytesIO()
    encoded_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=encoded_frames[1:],
        duration=durations,
        loop=0,
        optimize=config.gif_optimize,
        disposal=2,
    )
    output.seek(0)
    return output


def process_fadein_image_bytes(
    data: bytes,
    config: FadeInImageConfig | None = None,
) -> tuple[io.BytesIO, str]:
    """Process static image bytes and return a GIF buffer ready for discord.File."""

    processor_config = config or FadeInImageConfig()
    validate_input_size(data, processor_config)
    image = open_and_validate_static_image(data, processor_config)
    frames = build_fadein_frames(image, processor_config)
    output = save_frames_as_gif(frames, processor_config)
    return output, processor_config.output_filename


def _normalized_format(image: Image.Image) -> str:
    image_format = (image.format or "").upper()
    return "JPEG" if image_format == "JPG" else image_format


def _is_animated(image: Image.Image) -> bool:
    return bool(getattr(image, "is_animated", False)) or int(getattr(image, "n_frames", 1)) > 1


def _has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _flatten_transparency_to_black(image: Image.Image) -> Image.Image:
    if _has_alpha(image):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        composed = Image.alpha_composite(background, rgba)
        return composed.convert("RGB")
    return image.convert("RGB")


def _total_frames(config: FadeInImageConfig) -> int:
    if config.fps <= 0:
        raise ValueError("fps must be greater than zero")
    if config.duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    return max(2, int(round(config.fps * config.duration_seconds)))


def _frame_durations_ms(config: FadeInImageConfig, frame_count: int) -> list[int]:
    if frame_count <= 0:
        return []
    if frame_count == 1:
        return [int(round(config.duration_seconds * 1000))]

    total_duration_cs = max(1, int(round(config.duration_seconds * 100)))
    base_duration_cs = max(1, total_duration_cs // frame_count)
    remainder_cs = max(0, total_duration_cs - (base_duration_cs * frame_count))

    durations = [
        (base_duration_cs + (1 if index < remainder_cs else 0)) * 10
        for index in range(frame_count)
    ]
    return durations


def _compression_attempts(config: FadeInImageConfig) -> list[GifCompressionAttempt]:
    attempts: list[GifCompressionAttempt] = []
    seen: set[tuple[int, float, int | None]] = {(config.fps, 1.0, None)}

    def add(fps: int, scale: float, colors: int | None) -> None:
        if fps <= 0 or fps > config.fps:
            return
        if scale <= 0 or scale > 1:
            return
        if colors is not None and not 2 <= colors <= 256:
            return
        key = (fps, scale, colors)
        if key not in seen:
            seen.add(key)
            attempts.append(GifCompressionAttempt(fps=fps, scale=scale, colors=colors))

    palette_values = _palette_values(config)
    palette_set = set(palette_values)
    fps_values = _fps_values(config)
    scale_values = _scale_values(config)

    def preferred_colors(values: tuple[int, ...]) -> tuple[int, ...]:
        selected = tuple(value for value in values if value in palette_set)
        return selected or (min(palette_values),)

    for colors in palette_values:
        add(config.fps, 1.0, colors)

    for fps in fps_values:
        if fps != config.fps:
            add(fps, 1.0, 256)

    for fps in fps_values:
        if fps != config.fps:
            for colors in preferred_colors((192, 128, 96, 64)):
                add(fps, 1.0, colors)

    for scale in scale_values:
        if scale != 1.0:
            for colors in (256, *preferred_colors((192, 128))):
                add(config.fps, scale, colors)

    for scale in (0.75, 0.7, 0.65):
        if scale in scale_values:
            for fps in (15, 12, 10):
                if fps not in fps_values:
                    continue
                for colors in preferred_colors((128, 96, 64)):
                    add(fps, scale, colors)

    for scale in (0.6, 0.55, 0.5):
        if scale in scale_values:
            for fps in (12, 10):
                if fps not in fps_values:
                    continue
                for colors in preferred_colors((96, 64)):
                    add(fps, scale, colors)

    return attempts


def _palette_values(config: FadeInImageConfig) -> tuple[int, ...]:
    values = tuple(color for color in config.compression_palette_colors if 2 <= color <= 256)
    return values or (256,)


def _fps_values(config: FadeInImageConfig) -> tuple[int, ...]:
    values = {fps for fps in config.compression_fps_values if 2 <= fps <= config.fps}
    values.add(config.fps)
    return tuple(sorted(values, reverse=True))


def _scale_values(config: FadeInImageConfig) -> tuple[float, ...]:
    values = {scale for scale in config.compression_scale_factors if 0 < scale <= 1}
    values.add(1.0)
    return tuple(sorted(values, reverse=True))


def _prepare_frames_for_attempt(
    frames: list[Image.Image],
    config: FadeInImageConfig,
    attempt: GifCompressionAttempt,
) -> list[Image.Image]:
    target_frame_count = max(2, int(round(attempt.fps * config.duration_seconds)))
    selected = _select_evenly_spaced_frames(frames, min(target_frame_count, len(frames)))
    if attempt.scale == 1.0:
        return [frame.copy() for frame in selected]
    return _resize_frames(selected, attempt.scale, config)


def _select_evenly_spaced_frames(frames: list[Image.Image], target_count: int) -> list[Image.Image]:
    if target_count >= len(frames):
        return frames
    if target_count <= 2:
        return [frames[0], frames[-1]]

    last_index = len(frames) - 1
    indexes = [round(index * last_index / (target_count - 1)) for index in range(target_count)]
    indexes[0] = 0
    indexes[-1] = last_index
    return [frames[index] for index in indexes]


def _resize_frames(frames: list[Image.Image], scale: float, config: FadeInImageConfig) -> list[Image.Image]:
    if not frames:
        return []

    width, height = frames[0].size
    new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    if new_size[0] < config.min_width or new_size[1] < config.min_height:
        return []

    return [frame.resize(new_size, Image.Resampling.LANCZOS) for frame in frames]


def _quantize_frames(frames: list[Image.Image], colors: int) -> list[Image.Image]:
    if colors >= 256:
        return [frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=256) for frame in frames]
    return [
        frame.convert("RGB").quantize(
            colors=colors,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.FLOYDSTEINBERG,
        )
        for frame in frames
    ]
