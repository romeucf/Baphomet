from __future__ import annotations

import io
import re
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence, UnidentifiedImageError

from modules.media_processing.whitetext.errors import (
    ImageTooLargeError,
    InvalidImageError,
    LayoutComputationError,
    OutputTooLargeError,
    UnsupportedMediaError,
    WhitetextProcessingError,
    WhitetextUnsupportedMediaError,
)
from modules.media_processing.whitetext.layout import CaptionLayout, CaptionStyle, WhitetextLayout, compute_caption_layout, load_font, normalize_caption_text, resolve_font_path


MAX_TEXT_LENGTH = CaptionStyle().max_chars
MAX_IMAGE_INPUT_BYTES = 25 * 1024 * 1024
MAX_VIDEO_INPUT_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_GIF_FRAMES = 250
MAX_GIF_TOTAL_PIXELS = 80_000_000

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class MediaKind(Enum):
    STATIC_IMAGE = "static_image"
    GIF = "gif"
    VIDEO = "video"


@dataclass(frozen=True)
class WhiteTextProcessorConfig:
    max_input_bytes: int = 25 * 1024 * 1024
    max_output_bytes: int = 8 * 1024 * 1024
    max_static_pixels: int = 16_000_000
    min_width: int = 80
    min_height: int = 20
    max_width: int = 4000
    max_height: int = 4000
    allow_transparency: bool = True
    output_format_static: str = "PNG"
    max_gif_frames: int = MAX_GIF_FRAMES
    max_gif_total_pixels: int = MAX_GIF_TOTAL_PIXELS
    default_frame_duration_ms: int = 100
    preserve_loop: bool = True
    gif_optimize: bool = True
    gif_disposal: int = 2


@dataclass(frozen=True)
class ProcessedMedia:
    buffer: io.BytesIO
    filename: str
    content_type: str

    @property
    def size_bytes(self) -> int:
        return self.buffer.getbuffer().nbytes


def process_media(
    raw: bytes,
    *,
    filename: str,
    content_type: str | None,
    text: str,
    layout: WhitetextLayout | None = None,
) -> ProcessedMedia:
    normalized_text = _validate_text(text)
    if not raw:
        raise WhitetextProcessingError("O anexo enviado esta vazio.", code="empty_attachment")

    kind = detect_media_kind(raw, filename=filename, content_type=content_type)
    if kind == MediaKind.VIDEO:
        from modules.media_processing.whitetext.video import VideoProcessingConfig, process_video_bytes_to_captioned_gif

        style = layout.style if layout is not None else None
        output, _ = process_video_bytes_to_captioned_gif(
            raw,
            normalized_text,
            video_config=VideoProcessingConfig(max_video_input_bytes=MAX_VIDEO_INPUT_BYTES),
            processor_config=WhiteTextProcessorConfig(max_input_bytes=MAX_IMAGE_INPUT_BYTES),
            style=style,
        )
        return ProcessedMedia(buffer=output, filename="video-whitetext.gif", content_type="image/gif")

    if len(raw) > MAX_IMAGE_INPUT_BYTES:
        limit_mb = MAX_IMAGE_INPUT_BYTES // (1024 * 1024)
        raise WhitetextProcessingError(f"Essa midia e grande demais. Limite: {limit_mb} MB.", code="media_too_large")
    if kind == MediaKind.GIF:
        return _process_gif_bytes(raw, filename=filename, text=normalized_text, layout=layout)
    return _process_static_image(raw, filename=filename, text=normalized_text, layout=layout)


def detect_media_kind(raw: bytes, *, filename: str, content_type: str | None) -> MediaKind:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image_format = (image.format or "").upper()
            if image_format in {"PNG", "JPEG", "JPG"}:
                return MediaKind.STATIC_IMAGE
            if image_format == "WEBP":
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
                    raise WhitetextUnsupportedMediaError(
                        "WEBP animado ainda nao e suportado pelo /whitetext. Envie PNG, JPG, GIF ou video curto.",
                        code="animated_webp_unsupported",
                    )
                return MediaKind.STATIC_IMAGE
            if image_format == "GIF":
                return MediaKind.GIF
    except WhitetextUnsupportedMediaError:
        raise
    except (UnidentifiedImageError, OSError, ValueError):
        pass

    if _looks_like_video(raw, filename=filename, content_type=content_type):
        return MediaKind.VIDEO
    raise WhitetextUnsupportedMediaError(
        "Formato nao suportado. Envie PNG, JPG, WEBP estatico, GIF, MP4, MOV ou WEBM.",
        code="unsupported_media_type",
    )


def detect_image_format(data: bytes) -> str:
    """Detect the real image format with Pillow instead of trusting filenames."""

    if not data:
        raise InvalidImageError("A imagem enviada esta vazia.", code="image_empty")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                image.verify()
                if not image_format:
                    raise InvalidImageError("Nao consegui identificar o formato da imagem.", code="image_format_unknown")
                return "JPEG" if image_format == "JPG" else image_format
    except InvalidImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageTooLargeError(
            "Essa imagem tem resolucao grande demais para processar com seguranca.",
            code="image_decompression_bomb",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("O arquivo enviado nao parece ser uma imagem valida.", code="image_invalid") from exc


def open_static_image(data: bytes) -> Image.Image:
    """Open, verify, orient, and normalize a static image from bytes."""

    return _open_static_image(data, WhiteTextProcessorConfig())


def validate_static_dimensions(image: Image.Image, config: WhiteTextProcessorConfig) -> None:
    """Validate static image dimensions against configurable safety limits."""

    width, height = image.size
    if width < config.min_width:
        raise ImageTooLargeError(
            f"A imagem e estreita demais para receber texto com seguranca. Minimo: {config.min_width}px.",
            code="image_width_too_small",
        )
    if height < config.min_height:
        raise ImageTooLargeError(
            f"A imagem e baixa demais para receber texto com seguranca. Minimo: {config.min_height}px.",
            code="image_height_too_small",
        )
    if width > config.max_width:
        raise ImageTooLargeError(f"A imagem e larga demais. Maximo: {config.max_width}px.", code="image_width_too_large")
    if height > config.max_height:
        raise ImageTooLargeError(f"A imagem e alta demais. Maximo: {config.max_height}px.", code="image_height_too_large")
    if width * height > config.max_static_pixels:
        raise ImageTooLargeError(
            "Essa imagem tem pixels demais para processar com seguranca.",
            code="image_pixel_limit",
        )


def render_caption_canvas(image: Image.Image, text: str, style: CaptionStyle | None = None) -> Image.Image:
    """Render a white caption band above a validated static image."""

    caption_style = style or CaptionStyle()
    layout = compute_caption_layout(text, image.width, caption_style)
    has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
    rendered = process_single_gif_frame(image, layout, caption_style)
    return rendered if has_alpha else rendered.convert("RGB")


def process_static_image_bytes(
    data: bytes,
    text: str,
    config: WhiteTextProcessorConfig | None = None,
    style: CaptionStyle | None = None,
) -> tuple[io.BytesIO, str]:
    """Process static image bytes and return a PNG buffer ready for discord.File."""

    processor_config = config or WhiteTextProcessorConfig()
    if not data:
        raise InvalidImageError("A imagem enviada esta vazia.", code="image_empty")
    if len(data) > processor_config.max_input_bytes:
        raise ImageTooLargeError(
            f"A imagem enviada e grande demais. Limite: {processor_config.max_input_bytes // (1024 * 1024)} MB.",
            code="image_input_too_large",
        )

    image = _open_static_image(data, processor_config)
    validate_static_dimensions(image, processor_config)
    rendered = render_caption_canvas(image, text, style)
    if rendered.width != image.width or rendered.height <= image.height:
        raise LayoutComputationError("O canvas final ficou inconsistente.", code="output_dimensions_invalid")

    output = io.BytesIO()
    rendered.save(output, format=processor_config.output_format_static, optimize=True)
    output.seek(0)
    if output.getbuffer().nbytes > processor_config.max_output_bytes:
        raise OutputTooLargeError(
            "O resultado ficou grande demais para enviar.",
            code="output_too_large",
        )
    return output, "whitetext.png"


def is_animated_gif(data: bytes) -> bool:
    """Return True when bytes are a valid animated GIF."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (image.format or "").upper() != "GIF":
                    return False
                return bool(getattr(image, "is_animated", False)) and int(getattr(image, "n_frames", 1)) > 1
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageTooLargeError(
            "Esse GIF tem resolucao grande demais para processar com seguranca.",
            code="gif_decompression_bomb",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("O arquivo enviado nao parece ser uma imagem valida.", code="image_invalid") from exc


def extract_gif_metadata(image: Image.Image) -> dict:
    """Extract core GIF metadata, including per-frame durations."""

    return _extract_gif_metadata(image, WhiteTextProcessorConfig())


def validate_gif(image: Image.Image, config: WhiteTextProcessorConfig) -> None:
    """Validate animated GIF dimensions and frame count before processing."""

    if (image.format or "").upper() != "GIF":
        raise UnsupportedMediaError("O arquivo enviado nao e um GIF.", code="gif_invalid_format")

    width, height = image.size
    if width < config.min_width:
        raise ImageTooLargeError(
            f"O GIF e estreito demais para receber texto com seguranca. Minimo: {config.min_width}px.",
            code="gif_width_too_small",
        )
    if height < config.min_height:
        raise ImageTooLargeError(
            f"O GIF e baixo demais para receber texto com seguranca. Minimo: {config.min_height}px.",
            code="gif_height_too_small",
        )
    if width > config.max_width:
        raise ImageTooLargeError(f"O GIF e largo demais. Maximo: {config.max_width}px.", code="gif_width_too_large")
    if height > config.max_height:
        raise ImageTooLargeError(f"O GIF e alto demais. Maximo: {config.max_height}px.", code="gif_height_too_large")

    frame_count = int(getattr(image, "n_frames", 1))
    if frame_count <= 0:
        raise InvalidImageError("Esse GIF nao possui frames validos.", code="gif_empty")
    if frame_count > config.max_gif_frames:
        raise ImageTooLargeError(
            f"Esse GIF tem frames demais. Limite: {config.max_gif_frames}.",
            code="gif_too_many_frames",
        )
    if width * height * frame_count > config.max_gif_total_pixels:
        raise ImageTooLargeError(
            "Esse GIF tem pixels demais ao considerar todos os frames.",
            code="gif_total_pixel_limit",
        )

    for index in range(frame_count):
        try:
            image.seek(index)
            image.load()
        except (EOFError, OSError, ValueError) as exc:
            raise InvalidImageError("Esse GIF parece estar corrompido.", code="gif_corrupt_frame") from exc
    try:
        image.seek(0)
    except EOFError:
        pass


def process_gif_bytes(
    data: bytes,
    text: str,
    config: WhiteTextProcessorConfig | None = None,
    style: CaptionStyle | None = None,
) -> tuple[io.BytesIO, str]:
    """Process GIF bytes and return an animated GIF buffer ready for discord.File."""

    processor_config = config or WhiteTextProcessorConfig()
    if not data:
        raise InvalidImageError("O GIF enviado esta vazio.", code="gif_empty_payload")
    if len(data) > processor_config.max_input_bytes:
        raise ImageTooLargeError(
            f"O GIF enviado e grande demais. Limite: {processor_config.max_input_bytes // (1024 * 1024)} MB.",
            code="gif_input_too_large",
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                validate_gif(image, processor_config)
                metadata = _extract_gif_metadata(image, processor_config)
                caption_style = style or CaptionStyle()
                layout = compute_caption_layout(text, metadata["width"], caption_style)
                font = load_font(resolve_font_path(caption_style.font_path), layout.font_size)

                frames: list[Image.Image] = []
                for frame in ImageSequence.Iterator(image):
                    frame.load()
                    frames.append(_render_caption_frame(frame.copy(), layout, caption_style, font))

                if not frames:
                    raise InvalidImageError("Esse GIF nao possui frames validos.", code="gif_empty")
    except (UnsupportedMediaError, InvalidImageError, ImageTooLargeError, LayoutComputationError):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageTooLargeError(
            "Esse GIF tem resolucao grande demais para processar com seguranca.",
            code="gif_decompression_bomb",
        ) from exc
    except (EOFError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("Nao consegui abrir esse GIF.", code="gif_open_failed") from exc

    output = io.BytesIO()
    save_kwargs = {
        "format": "GIF",
        "save_all": True,
        "append_images": frames[1:],
        "duration": metadata["durations"],
        "optimize": processor_config.gif_optimize,
        "disposal": processor_config.gif_disposal,
    }
    if processor_config.preserve_loop:
        save_kwargs["loop"] = metadata["loop"]
    frames[0].save(output, **save_kwargs)
    output.seek(0)
    if output.getbuffer().nbytes > processor_config.max_output_bytes:
        raise OutputTooLargeError("O GIF final ficou grande demais para enviar.", code="gif_output_too_large")
    return output, "whitetext.gif"


def process_single_gif_frame(frame: Image.Image, layout: CaptionLayout, style: CaptionStyle) -> Image.Image:
    """Render one GIF frame below a precomputed caption layout."""

    font = load_font(resolve_font_path(style.font_path), layout.font_size)
    return _render_caption_frame(frame, layout, style, font)


def _render_caption_frame(
    frame: Image.Image,
    layout: CaptionLayout,
    style: CaptionStyle,
    font: ImageFont.FreeTypeFont,
) -> Image.Image:
    """Render one image/frame below a caption layout using an already loaded font."""

    if frame.width != layout.media_width:
        raise LayoutComputationError("O frame do GIF nao tem a largura esperada.", code="gif_frame_width_mismatch")

    source = frame.convert("RGBA")
    output = Image.new("RGBA", (layout.media_width, source.height + layout.caption_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(output)
    background = _rgba(style.background_color)
    text_color = _rgba(style.text_color)

    draw.rectangle((0, 0, layout.media_width - 1, layout.caption_height - 1), fill=background)
    output.alpha_composite(source, (0, layout.caption_height))

    for line in layout.lines:
        bbox = draw.textbbox((line.x, line.y), line.text, font=font)
        if bbox[0] < 0 or bbox[1] < 0 or bbox[2] > layout.media_width or bbox[3] > layout.caption_height:
            raise LayoutComputationError("O texto calculado sairia da faixa branca.", code="text_bounds_overflow")
        draw.text((line.x, line.y), line.text, font=font, fill=text_color)
    return output


def _process_static_image(
    raw: bytes,
    *,
    filename: str,
    text: str,
    layout: WhitetextLayout | None = None,
) -> ProcessedMedia:
    style = layout.style if layout is not None else None
    output, _ = process_static_image_bytes(
        raw,
        text,
        config=WhiteTextProcessorConfig(max_input_bytes=MAX_IMAGE_INPUT_BYTES, max_static_pixels=MAX_IMAGE_PIXELS),
        style=style,
    )
    return ProcessedMedia(
        buffer=output,
        filename=f"{_safe_stem(filename)}-whitetext.png",
        content_type="image/png",
    )


def _process_gif_bytes(
    raw: bytes,
    *,
    filename: str,
    text: str,
    layout: WhitetextLayout | None = None,
    converted_from_video: bool = False,
) -> ProcessedMedia:
    style = layout.style if layout is not None else None
    output, _ = process_gif_bytes(
        raw,
        text,
        config=WhiteTextProcessorConfig(max_input_bytes=MAX_IMAGE_INPUT_BYTES),
        style=style,
    )
    output_name = "video-whitetext.gif" if converted_from_video else f"{_safe_stem(filename)}-whitetext.gif"
    return ProcessedMedia(buffer=output, filename=output_name, content_type="image/gif")


def _validate_text(text: str) -> str:
    return normalize_caption_text(text, max_chars=MAX_TEXT_LENGTH)


def _extract_gif_metadata(image: Image.Image, config: WhiteTextProcessorConfig) -> dict:
    if (image.format or "").upper() != "GIF":
        raise UnsupportedMediaError("O arquivo enviado nao e um GIF.", code="gif_invalid_format")

    frame_count = int(getattr(image, "n_frames", 1))
    durations: list[int] = []
    for index in range(frame_count):
        try:
            image.seek(index)
            image.load()
        except (EOFError, OSError, ValueError) as exc:
            raise InvalidImageError("Esse GIF parece estar corrompido.", code="gif_corrupt_frame") from exc
        duration = int(image.info.get("duration", config.default_frame_duration_ms) or config.default_frame_duration_ms)
        durations.append(max(1, duration))

    try:
        image.seek(0)
    except EOFError:
        pass

    return {
        "width": image.width,
        "height": image.height,
        "n_frames": frame_count,
        "loop": int(image.info.get("loop", 0) or 0),
        "durations": durations,
        "mode": image.mode,
        "transparency": image.info.get("transparency"),
    }


def _open_static_image(data: bytes, config: WhiteTextProcessorConfig) -> Image.Image:
    image_format = detect_image_format(data)
    if image_format == "GIF":
        raise UnsupportedMediaError("GIF sera processado pelo fluxo animado, nao pelo fluxo de imagem estatica.", code="gif_not_static")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if getattr(image, "is_animated", False) or int(getattr(image, "n_frames", 1)) > 1:
                    raise UnsupportedMediaError(
                        "Imagens animadas devem usar o fluxo de GIF/video.",
                        code="animated_image_not_static",
                    )
                image = ImageOps.exif_transpose(image)
                image.load()
                has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
                if has_alpha and config.allow_transparency:
                    normalized = image.convert("RGBA")
                elif has_alpha:
                    normalized = Image.new("RGB", image.size, (255, 255, 255))
                    normalized.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
                else:
                    normalized = image.convert("RGB")
                return normalized.copy()
    except UnsupportedMediaError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageTooLargeError(
            "Essa imagem tem resolucao grande demais para processar com seguranca.",
            code="image_decompression_bomb",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("Nao consegui abrir essa imagem.", code="image_open_failed") from exc


def _rgba(color: tuple[int, ...]) -> tuple[int, int, int, int]:
    if len(color) == 4:
        return (int(color[0]), int(color[1]), int(color[2]), int(color[3]))
    return (int(color[0]), int(color[1]), int(color[2]), 255)


def _safe_stem(filename: str) -> str:
    stem = Path(filename or "media").stem.strip().lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip(".-")
    return (stem or "media")[:48]


def _looks_like_video(raw: bytes, *, filename: str, content_type: str | None) -> bool:
    has_mp4_magic = len(raw) >= 12 and raw[4:8] == b"ftyp"
    has_webm_magic = raw.startswith(b"\x1a\x45\xdf\xa3")
    return has_mp4_magic or has_webm_magic
