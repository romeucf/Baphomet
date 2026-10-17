from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from modules.media_processing.whitetext.errors import FFmpegNotFoundError, InvalidVideoError, OutputTooLargeError, VideoProcessingError, VideoTooLargeError
from modules.media_processing.whitetext.layout import CaptionStyle
from modules.media_processing.whitetext.processor import WhiteTextProcessorConfig


LOGGER = logging.getLogger("baphomet.whitetext.video")


@dataclass(frozen=True)
class VideoProcessingConfig:
    max_video_input_bytes: int = 25 * 1024 * 1024
    max_video_duration_seconds: float = 8.0
    output_fps: int = 12
    max_output_width: int = 640
    ffmpeg_timeout_seconds: int = 45
    ffprobe_timeout_seconds: int = 10
    video_temp_prefix: str = "baphomet-whitetext-video-"
    gif_palette_quality: str = "bayer:bayer_scale=5"
    max_intermediate_gif_bytes: int = 20 * 1024 * 1024


@dataclass(frozen=True)
class VideoMetadata:
    duration: float
    width: int
    height: int
    fps: float | None
    format_name: str | None


def ensure_ffmpeg_available() -> None:
    """Ensure both ffmpeg and ffprobe are available in PATH."""

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise FFmpegNotFoundError(
            "FFmpeg/ffprobe nao esta disponivel neste ambiente para processar video.",
            code="ffmpeg_missing",
        )


def probe_video(input_path: Path, config: VideoProcessingConfig) -> VideoMetadata:
    """Probe video metadata with ffprobe JSON output."""

    ensure_ffmpeg_available()
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate",
        "-show_entries",
        "format=duration,format_name",
        "-of",
        "json",
        str(input_path),
    ]
    try:
        completed = _run_subprocess(command, timeout=config.ffprobe_timeout_seconds, error_code="ffprobe_failed")
    except VideoProcessingError as exc:
        if exc.code == "video_timeout":
            raise
        raise InvalidVideoError(
            "Nao consegui validar esse video. Talvez o arquivo esteja corrompido ou nao seja um video de verdade.",
            code="ffprobe_failed",
            detail=exc.detail,
        ) from exc
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise InvalidVideoError("Nao consegui ler os metadados desse video.", code="ffprobe_json_invalid") from exc

    streams = payload.get("streams") or []
    if not streams:
        raise InvalidVideoError("Esse arquivo nao possui stream de video.", code="video_stream_missing")
    stream = streams[0]
    format_info = payload.get("format") or {}

    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidVideoError("Nao consegui ler as dimensoes desse video.", code="video_dimensions_missing") from exc

    duration_raw = format_info.get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise InvalidVideoError("Nao consegui ler a duracao desse video.", code="video_duration_missing") from exc

    fps = _parse_fps(stream.get("r_frame_rate"))
    return VideoMetadata(
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        format_name=format_info.get("format_name"),
    )


def validate_video_metadata(metadata: VideoMetadata, config: VideoProcessingConfig) -> None:
    """Validate probed video metadata before conversion.

    Long videos are accepted here and truncated by FFmpeg to
    config.max_video_duration_seconds during conversion.
    """

    if metadata.duration <= 0:
        raise InvalidVideoError("Esse video parece nao ter duracao valida.", code="video_duration_invalid")
    if metadata.width <= 0 or metadata.height <= 0:
        raise InvalidVideoError("Esse video tem dimensoes invalidas.", code="video_dimensions_invalid")
    if metadata.width > 8192 or metadata.height > 8192 or metadata.width * metadata.height > 33_177_600:
        raise VideoTooLargeError("Esse video tem resolucao grande demais para processar com seguranca.", code="video_resolution_too_large")


def convert_video_to_gif_bytes(video_data: bytes, config: VideoProcessingConfig | None = None) -> bytes:
    """Convert video bytes to a bounded intermediate GIF using FFmpeg."""

    video_config = config or VideoProcessingConfig()
    if not video_data:
        raise InvalidVideoError("O video enviado esta vazio.", code="video_empty")
    if len(video_data) > video_config.max_video_input_bytes:
        raise VideoTooLargeError(
            f"Esse video e grande demais. Limite: {video_config.max_video_input_bytes // (1024 * 1024)} MB.",
            code="video_input_too_large",
        )

    ensure_ffmpeg_available()
    with tempfile.TemporaryDirectory(prefix=video_config.video_temp_prefix) as tmp:
        temp_dir = Path(tmp)
        input_path = temp_dir / "input.media"
        output_path = temp_dir / "intermediate.gif"
        input_path.write_bytes(video_data)

        metadata = probe_video(input_path, video_config)
        validate_video_metadata(metadata, video_config)

        filter_graph = (
            f"fps={video_config.output_fps},"
            f"scale='min({video_config.max_output_width},iw)':-1:flags=lanczos,"
            f"split[s0][s1];"
            f"[s0]palettegen[p];"
            f"[s1][p]paletteuse=dither={video_config.gif_palette_quality}"
        )
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-t",
            f"{video_config.max_video_duration_seconds:.3f}",
            "-i",
            str(input_path),
            "-an",
            "-vf",
            filter_graph,
            "-f",
            "gif",
            str(output_path),
        ]
        _run_subprocess(command, timeout=video_config.ffmpeg_timeout_seconds, error_code="ffmpeg_convert_failed")
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise VideoProcessingError("FFmpeg nao gerou um GIF valido.", code="video_conversion_empty")
        if output_path.stat().st_size > video_config.max_intermediate_gif_bytes:
            raise OutputTooLargeError(
                "Esse video gerou um GIF intermediario grande demais.",
                code="intermediate_gif_too_large",
            )
        return output_path.read_bytes()


def process_video_bytes_to_captioned_gif(
    video_data: bytes,
    text: str,
    video_config: VideoProcessingConfig | None = None,
    processor_config: WhiteTextProcessorConfig | None = None,
    style: CaptionStyle | None = None,
) -> tuple[io.BytesIO, str]:
    """Convert video bytes to GIF, then apply the whitetext GIF pipeline."""

    from modules.media_processing.whitetext.processor import process_gif_bytes

    intermediate = convert_video_to_gif_bytes(video_data, video_config)
    gif_config = processor_config if isinstance(processor_config, WhiteTextProcessorConfig) else WhiteTextProcessorConfig()
    return process_gif_bytes(intermediate, text, config=gif_config, style=style)


# Backward-compatible wrapper used by earlier internal code/tests.
def convert_video_to_gif(raw: bytes, *, filename: str = "input.media", limits: object | None = None, **_: object) -> bytes:
    return convert_video_to_gif_bytes(raw)


def _run_subprocess(command: list[str], *, timeout: int | float, error_code: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoProcessingError("O processamento do video demorou demais.", code="video_timeout") from exc
    except FileNotFoundError as exc:
        raise FFmpegNotFoundError("FFmpeg/ffprobe nao foi encontrado.", code="ffmpeg_missing") from exc
    except OSError as exc:
        raise VideoProcessingError("Nao consegui executar FFmpeg para processar esse video.", code=error_code) from exc

    if completed.returncode != 0:
        detail = _summarize_process_output(completed.stderr or completed.stdout)
        if error_code == "ffprobe_failed":
            LOGGER.debug("video_probe_failed detail=%s", detail)
        else:
            LOGGER.warning("video_command_failed code=%s detail=%s command=%s", error_code, detail, command[0])
        raise VideoProcessingError("Nao consegui processar esse video com FFmpeg.", code=error_code, detail=detail)
    return completed


def _parse_fps(value: object) -> float | None:
    if not value:
        return None
    text = str(value)
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return float(numerator) / denominator_value
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _summarize_process_output(output: str | None, *, limit: int = 700) -> str:
    clean = " ".join((output or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."
