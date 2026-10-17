from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from modules.media_processing.whitetext.errors import FFmpegNotFoundError, InvalidVideoError, OutputTooLargeError, VideoProcessingError, VideoTooLargeError
from modules.media_processing.whitetext.processor import WhiteTextProcessorConfig
from modules.media_processing.whitetext.processor import process_media
from modules.media_processing.whitetext.video import (
    VideoMetadata,
    VideoProcessingConfig,
    convert_video_to_gif_bytes,
    ensure_ffmpeg_available,
    probe_video,
    process_video_bytes_to_captioned_gif,
    validate_video_metadata,
)


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def make_test_video(*, width: int = 120, height: int = 80, duration: float = 1.0) -> bytes:
    if not FFMPEG:
        raise unittest.SkipTest("FFmpeg not available")
    with tempfile.TemporaryDirectory() as tmp:
        video_path = Path(tmp) / "input.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=red:s={width}x{height}:d={duration}",
                "-vf",
                "format=yuv420p",
                str(video_path),
            ],
            check=True,
            capture_output=True,
        )
        return video_path.read_bytes()


class WhitetextVideoUnitTests(unittest.TestCase):
    def test_missing_ffmpeg_is_custom_error(self) -> None:
        with patch("modules.media_processing.whitetext.video.shutil.which", return_value=None):
            with self.assertRaises(FFmpegNotFoundError):
                ensure_ffmpeg_available()

    def test_validate_video_metadata_rejects_invalid_duration(self) -> None:
        metadata = VideoMetadata(duration=0, width=120, height=80, fps=12, format_name="mov,mp4")

        with self.assertRaises(InvalidVideoError):
            validate_video_metadata(metadata, VideoProcessingConfig())

    def test_validate_video_metadata_rejects_absurd_resolution(self) -> None:
        metadata = VideoMetadata(duration=1, width=9000, height=5000, fps=30, format_name="mov,mp4")

        with self.assertRaises(VideoTooLargeError):
            validate_video_metadata(metadata, VideoProcessingConfig())

    def test_convert_video_rejects_large_input_before_tempfile(self) -> None:
        config = VideoProcessingConfig(max_video_input_bytes=4)

        with self.assertRaises(VideoTooLargeError):
            convert_video_to_gif_bytes(b"12345", config)

    def test_timeout_is_custom_error(self) -> None:
        with patch("modules.media_processing.whitetext.video.subprocess.run", side_effect=subprocess.TimeoutExpired(["ffmpeg"], 1)):
            with self.assertRaises(VideoProcessingError):
                convert_video_to_gif_bytes(b"not empty", VideoProcessingConfig())


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/ffprobe not available")
class WhitetextVideoTests(unittest.TestCase):
    def test_probe_video_reads_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "input.mp4"
            video_path.write_bytes(make_test_video(width=128, height=72, duration=1))

            metadata = probe_video(video_path, VideoProcessingConfig())

        self.assertGreater(metadata.duration, 0)
        self.assertEqual(metadata.width, 128)
        self.assertEqual(metadata.height, 72)
        self.assertIsNotNone(metadata.format_name)

    def test_probe_video_rejects_non_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.bin"
            path.write_bytes(b"not a video")

            with self.assertRaises(InvalidVideoError):
                probe_video(path, VideoProcessingConfig())

    def test_convert_video_to_gif_bytes_limits_width_and_returns_gif(self) -> None:
        gif_bytes = convert_video_to_gif_bytes(
            make_test_video(width=240, height=120, duration=1),
            VideoProcessingConfig(max_output_width=120, output_fps=8),
        )
        output = Image.open(io.BytesIO(gif_bytes))

        self.assertEqual(output.format, "GIF")
        self.assertEqual(output.width, 120)

    def test_convert_video_to_gif_bytes_rejects_intermediate_too_large(self) -> None:
        with self.assertRaises(OutputTooLargeError):
            convert_video_to_gif_bytes(
                make_test_video(width=120, height=80, duration=1),
                VideoProcessingConfig(max_intermediate_gif_bytes=1),
            )

    def test_process_video_bytes_to_captioned_gif(self) -> None:
        buffer, filename = process_video_bytes_to_captioned_gif(
            make_test_video(width=120, height=80, duration=1),
            "video",
            video_config=VideoProcessingConfig(max_output_width=120, output_fps=8),
            processor_config=WhiteTextProcessorConfig(max_output_bytes=8 * 1024 * 1024),
        )
        output = Image.open(buffer)

        self.assertEqual(filename, "whitetext.gif")
        self.assertEqual(output.width, 120)
        self.assertGreater(output.height, 80)

    def test_short_mp4_converts_to_captioned_gif(self) -> None:
        result = process_media(make_test_video(width=120, height=80, duration=1), filename="clip.mp4", content_type="video/mp4", text="video")
        output = Image.open(io.BytesIO(result.buffer.getvalue()))

        self.assertEqual(result.content_type, "image/gif")
        self.assertEqual(result.filename, "video-whitetext.gif")
        self.assertEqual(output.width, 120)
        self.assertGreater(output.height, 80)
