from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.paths import FONTS_DIR
from modules.media_processing.whitetext.errors import FontNotFoundError, InvalidTextError, LayoutComputationError, TextTooLongError


FONT_FILENAME = "FuturaCEB.otf"
DEFAULT_FONT_PATH = FONTS_DIR / FONT_FILENAME


@dataclass(frozen=True)
class CaptionStyle:
    """Configurable visual rules for computing a white text caption layout."""

    font_path: Path | None = None
    text_color: tuple[int, int, int] = (0, 0, 0)
    background_color: tuple[int, int, int] = (255, 255, 255)
    min_font_size: int | None = 16
    max_font_size: int | None = 160
    base_font_ratio: float = 0.105
    horizontal_padding_ratio: float = 0.06
    vertical_padding_ratio: float = 0.45
    min_horizontal_padding: int = 12
    max_horizontal_padding: int = 96
    min_vertical_padding: int = 8
    max_vertical_padding: int = 96
    line_spacing_ratio: float = 0.14
    max_chars: int = 800
    max_lines_soft: int | None = None


@dataclass(frozen=True)
class TextLine:
    """A measured line and the exact draw coordinates for its text origin."""

    text: str
    width: int
    height: int
    x: int
    y: int


@dataclass(frozen=True)
class CaptionLayout:
    """Complete caption-band layout for a fixed media width."""

    media_width: int
    caption_height: int
    font_size: int
    horizontal_padding: int
    vertical_padding: int
    line_spacing: int
    lines: list[TextLine]
    text_block_width: int
    text_block_height: int

    @property
    def max_text_width(self) -> int:
        return self.media_width - (2 * self.horizontal_padding)

    @property
    def available_width(self) -> int:
        return self.max_text_width

    @property
    def bar_height(self) -> int:
        return self.caption_height

    @property
    def padding_x(self) -> int:
        return self.horizontal_padding

    @property
    def padding_y(self) -> int:
        return self.vertical_padding


@dataclass(frozen=True)
class _MeasuredLine:
    text: str
    width: int
    height: int
    bbox: tuple[int, int, int, int]


class WhitetextLayout:
    """Small reusable facade around the pure caption layout functions."""

    def __init__(self, *, style: CaptionStyle | None = None, font_path: str | Path | None = None) -> None:
        base_style = style or CaptionStyle()
        if font_path is not None:
            base_style = replace(base_style, font_path=Path(font_path))
        self.style = base_style
        self.font_path = resolve_font_path(base_style.font_path)
        self._font_cache: dict[int, ImageFont.FreeTypeFont] = {}
        self._measure_image = Image.new("RGB", (1, 1), self.style.background_color)
        self._draw = ImageDraw.Draw(self._measure_image)

    def build_caption_layout(self, text: str, media_width: int) -> CaptionLayout:
        """Compute a complete caption layout using this instance's style."""

        return compute_caption_layout(text, media_width, self.style)

    def load_font(self, size: int) -> ImageFont.FreeTypeFont:
        """Load and cache the required font at a given size."""

        cached = self._font_cache.get(size)
        if cached is not None:
            return cached
        font = load_font(self.font_path, size)
        self._font_cache[size] = font
        return font


def normalize_caption_text(text: str, *, max_chars: int = CaptionStyle().max_chars) -> str:
    """Normalize user caption text while preserving intentional line breaks.

    The function trims surrounding whitespace, normalizes CRLF/CR line breaks to
    LF, strips each manual line's edges, preserves accents, punctuation and
    emoji, and rejects empty or overlong captions.
    """

    if text is None:
        raise InvalidTextError("Informe um texto para colocar na faixa branca.", code="text_missing")

    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise InvalidTextError("Informe um texto para colocar na faixa branca.", code="text_empty")
    if len(normalized) > max_chars:
        raise TextTooLongError(f"O texto pode ter no maximo {max_chars} caracteres.", code="text_too_long")

    lines = [line.strip() for line in normalized.split("\n")]
    normalized = "\n".join(line for line in lines if line)
    if not normalized:
        raise InvalidTextError("Informe um texto para colocar na faixa branca.", code="text_empty")
    return normalized


def resolve_font_path(font_path: str | Path | None) -> Path:
    """Resolve the required FuturaCEB.otf font path or raise FontNotFoundError."""

    candidates: list[Path] = []
    if font_path is not None:
        candidates.append(Path(font_path))

    candidates.append(DEFAULT_FONT_PATH)

    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        candidates.append(parent / "assets" / "fonts" / FONT_FILENAME)

    seen: set[Path] = set()
    attempted: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        attempted.append(resolved)
        if resolved.is_file():
            return resolved

    raise FontNotFoundError(
        "A fonte obrigatoria FuturaCEB.otf nao foi encontrada. Coloque o arquivo em assets/fonts/FuturaCEB.otf.",
        code="font_missing",
        detail="Font candidates: " + ", ".join(str(path) for path in attempted),
    )


def load_font(font_path: Path, size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType/OpenType font with Pillow without silent fallback."""

    if size <= 0:
        raise LayoutComputationError("O tamanho da fonte precisa ser positivo.", code="font_size_invalid")
    try:
        return ImageFont.truetype(str(font_path), size=size)
    except OSError as exc:
        raise FontNotFoundError(
            "A fonte obrigatoria FuturaCEB.otf nao pode ser carregada. Verifique assets/fonts/FuturaCEB.otf.",
            code="font_load_failed",
            detail=str(exc),
        ) from exc


def measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """Measure text using textbbox and return its real pixel width and height."""

    bbox = _measure_text_bbox(draw, text, font)
    return max(0, bbox[2] - bbox[0]), max(0, bbox[3] - bbox[1])


def wrap_text_by_pixels(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    """Wrap text by real rendered pixel width, preserving manual line breaks."""

    if max_width <= 0:
        raise LayoutComputationError("A largura disponivel para texto e invalida.", code="text_width_invalid")

    wrapped: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            continue

        current = ""
        for word in words:
            if measure_text(draw, word, font)[0] > max_width:
                if current:
                    wrapped.append(current)
                    current = ""
                pieces = _break_word_by_pixels(word, font, max_width, draw)
                wrapped.extend(pieces[:-1])
                current = pieces[-1]
                continue

            candidate = word if not current else f"{current} {word}"
            if measure_text(draw, candidate, font)[0] <= max_width:
                current = candidate
                continue

            if current:
                wrapped.append(current)
            current = word

        if current:
            wrapped.append(current)

    if not wrapped:
        raise LayoutComputationError("Nao foi possivel quebrar o texto em linhas.", code="wrap_empty")
    return wrapped


def compute_responsive_font_size(media_width: int, style: CaptionStyle) -> int:
    """Compute a clamped font size proportional to the media width."""

    if media_width <= 0:
        raise LayoutComputationError("A largura da midia precisa ser positiva.", code="media_width_invalid")

    min_font_size = style.min_font_size if style.min_font_size is not None else 16
    max_font_size = style.max_font_size if style.max_font_size is not None else 160
    if min_font_size <= 0 or max_font_size <= 0 or min_font_size > max_font_size:
        raise LayoutComputationError("Configuracao invalida de tamanho de fonte.", code="font_size_config_invalid")

    return _clamp(round(media_width * style.base_font_ratio), min_font_size, max_font_size)


def compute_caption_layout(text: str, media_width: int, style: CaptionStyle | None = None) -> CaptionLayout:
    """Compute every value needed to render a white caption band above media."""

    caption_style = style or CaptionStyle()
    if media_width <= 0:
        raise LayoutComputationError("A largura da midia precisa ser positiva.", code="media_width_invalid")

    normalized = normalize_caption_text(text, max_chars=caption_style.max_chars)
    font_path = resolve_font_path(caption_style.font_path)
    font_size = compute_responsive_font_size(media_width, caption_style)
    font = load_font(font_path, font_size)

    horizontal_padding = _clamp(
        round(media_width * caption_style.horizontal_padding_ratio),
        caption_style.min_horizontal_padding,
        caption_style.max_horizontal_padding,
    )
    max_text_width = media_width - (2 * horizontal_padding)
    if max_text_width <= 0:
        raise LayoutComputationError("A midia e estreita demais para receber texto.", code="text_width_invalid")

    measure_image = Image.new("RGB", (1, 1), caption_style.background_color)
    draw = ImageDraw.Draw(measure_image)
    lines_text = wrap_text_by_pixels(normalized, font, max_text_width, draw)
    if caption_style.max_lines_soft is not None and len(lines_text) > caption_style.max_lines_soft:
        raise LayoutComputationError("Esse texto geraria linhas demais para a faixa branca.", code="line_soft_limit")

    measured_lines = [_measure_line(draw, line, font) for line in lines_text]
    line_spacing = max(0, round(font_size * caption_style.line_spacing_ratio))
    text_block_width = max(line.width for line in measured_lines)
    text_block_height = sum(line.height for line in measured_lines) + line_spacing * (len(measured_lines) - 1)
    vertical_padding = _clamp(
        round(font_size * caption_style.vertical_padding_ratio),
        caption_style.min_vertical_padding,
        caption_style.max_vertical_padding,
    )
    caption_height = text_block_height + (2 * vertical_padding)

    current_top = round((caption_height - text_block_height) / 2)
    text_lines: list[TextLine] = []
    for measured in measured_lines:
        x = round((media_width - measured.width) / 2) - measured.bbox[0]
        y = current_top - measured.bbox[1]
        if measured.width > max_text_width:
            raise LayoutComputationError("Uma linha ultrapassou a largura segura.", code="line_width_overflow")
        text_lines.append(TextLine(text=measured.text, width=measured.width, height=measured.height, x=x, y=y))
        current_top += measured.height + line_spacing

    return CaptionLayout(
        media_width=media_width,
        caption_height=caption_height,
        font_size=font_size,
        horizontal_padding=horizontal_padding,
        vertical_padding=vertical_padding,
        line_spacing=line_spacing,
        lines=text_lines,
        text_block_width=text_block_width,
        text_block_height=text_block_height,
    )


def _measure_line(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> _MeasuredLine:
    bbox = _measure_text_bbox(draw, text, font)
    return _MeasuredLine(text=text, width=max(0, bbox[2] - bbox[0]), height=max(0, bbox[3] - bbox[1]), bbox=bbox)


def _measure_text_bbox(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, int, int]:
    if not text:
        return (0, 0, 0, 0)
    return draw.textbbox((0, 0), text, font=font)


def _break_word_by_pixels(
    word: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in word:
        if measure_text(draw, char, font)[0] > max_width:
            raise LayoutComputationError(
                "A largura disponivel e pequena demais para caber um unico caractere.",
                code="single_character_too_wide",
            )

        candidate = f"{current}{char}"
        if current and measure_text(draw, candidate, font)[0] > max_width:
            pieces.append(current)
            current = char
        else:
            current = candidate

    if current:
        pieces.append(current)
    if not pieces:
        raise LayoutComputationError("Nao foi possivel quebrar uma palavra longa.", code="word_wrap_empty")
    return pieces


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))

