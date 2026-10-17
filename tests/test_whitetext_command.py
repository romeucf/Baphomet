from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

import cogs.whitetext as whitetext_commands
from cogs.whitetext import (
    MAX_OUTPUT_BYTES,
    WhiteTextCog,
    classify_attachment,
    process_media_bytes,
)
from modules.media_processing.whitetext.errors import (
    FFmpegNotFoundError,
    FontNotFoundError,
    ImageTooLargeError,
    InvalidImageError,
    InvalidTextError,
    LayoutComputationError,
    OutputTooLargeError,
    TextTooLongError,
    UnsupportedMediaError,
    VideoProcessingError,
    VideoTooLargeError,
    VideoTooLongError,
)
from modules.media_processing.whitetext.layout import CaptionStyle
from modules.media_processing.whitetext.processor import MediaKind, WhiteTextProcessorConfig
from modules.media_processing.whitetext.video import VideoProcessingConfig


def image_bytes(format_name: str = "PNG", *, size: tuple[int, int] = (120, 80)) -> bytes:
    image = Image.new("RGB", size, (40, 100, 160))
    buffer = io.BytesIO()
    image.save(buffer, format=format_name)
    return buffer.getvalue()


class WhiteTextCommandHelpersTests(unittest.TestCase):
    def test_classify_attachment_uses_content_type(self) -> None:
        self.assertEqual(
            classify_attachment(SimpleNamespace(content_type="image/gif", filename="x.bin"), b"not checked"),
            MediaKind.GIF,
        )
        self.assertEqual(
            classify_attachment(SimpleNamespace(content_type="video/mp4", filename="x.bin"), b"not checked"),
            MediaKind.VIDEO,
        )
        self.assertEqual(
            classify_attachment(SimpleNamespace(content_type="image/png", filename="x.bin"), b"not checked"),
            MediaKind.STATIC_IMAGE,
        )

    def test_classify_attachment_uses_extension_fallback(self) -> None:
        self.assertEqual(
            classify_attachment(SimpleNamespace(content_type=None, filename="meme.webp"), b"not checked"),
            MediaKind.STATIC_IMAGE,
        )
        self.assertEqual(
            classify_attachment(SimpleNamespace(content_type=None, filename="meme.mov"), b"not checked"),
            MediaKind.VIDEO,
        )

    def test_classify_attachment_falls_back_to_real_bytes(self) -> None:
        media_kind = classify_attachment(SimpleNamespace(content_type=None, filename="meme.bin"), image_bytes("PNG"))

        self.assertEqual(media_kind.value, MediaKind.STATIC_IMAGE.value)

    def test_process_media_bytes_static(self) -> None:
        buffer, filename = process_media_bytes(
            image_bytes("PNG"),
            "texto",
            MediaKind.STATIC_IMAGE,
            WhiteTextProcessorConfig(max_output_bytes=MAX_OUTPUT_BYTES),
            VideoProcessingConfig(),
            CaptionStyle(),
        )

        rendered = Image.open(buffer)
        self.assertEqual(filename, "whitetext.png")
        self.assertEqual(rendered.width, 120)
        self.assertGreater(rendered.height, 80)

    def test_process_media_bytes_video_delegates(self) -> None:
        fake_buffer = io.BytesIO(b"GIF89a")
        mocked = Mock(return_value=(fake_buffer, "whitetext.gif"))
        with patch.object(whitetext_commands, "process_video_bytes_to_captioned_gif", mocked):
            buffer, filename = process_media_bytes(
                b"video",
                "texto",
                MediaKind.VIDEO,
                WhiteTextProcessorConfig(),
                VideoProcessingConfig(),
                CaptionStyle(),
            )

        self.assertIs(buffer, fake_buffer)
        self.assertEqual(filename, "whitetext.gif")
        mocked.assert_called_once()

    def test_process_media_bytes_rejects_unknown_kind(self) -> None:
        with self.assertRaises(UnsupportedMediaError):
            process_media_bytes(
                b"x",
                "texto",
                object(),  # type: ignore[arg-type]
                WhiteTextProcessorConfig(),
                VideoProcessingConfig(),
                CaptionStyle(),
            )


class WhiteTextCogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

    async def asyncTearDown(self) -> None:
        await self.bot.close()

    async def test_extension_registers_whitetext_slash_command(self) -> None:
        await self.bot.load_extension("cogs.whitetext")

        command_names = [command.qualified_name for command in self.bot.tree.walk_commands()]
        self.assertIn("whitetext", command_names)

    def test_initial_validation(self) -> None:
        cog = WhiteTextCog(self.bot)

        with self.assertRaises(UnsupportedMediaError):
            cog._validate_initial_input(None, "texto")
        with self.assertRaises(InvalidTextError):
            cog._validate_initial_input(SimpleNamespace(size=1), "  ")
        with self.assertRaises(TextTooLongError):
            cog._validate_initial_input(SimpleNamespace(size=1), "x" * 1000)
        with self.assertRaises(ImageTooLargeError):
            cog._validate_initial_input(SimpleNamespace(size=26 * 1024 * 1024), "texto")

    def test_friendly_error_messages_are_portuguese_and_specific(self) -> None:
        cases = [
            (FontNotFoundError("x"), "FuturaCEB"),
            (InvalidTextError("x"), "texto valido"),
            (TextTooLongError("x"), "grande demais"),
            (UnsupportedMediaError("x"), "formato nao e suportado"),
            (InvalidImageError("x"), "Nao consegui abrir essa imagem"),
            (ImageTooLargeError("x"), "grande demais"),
            (OutputTooLargeError("x"), "resultado ficou grande demais"),
            (FFmpegNotFoundError("x"), "FFmpeg"),
            (VideoTooLongError("x"), "longo demais"),
            (VideoTooLargeError("x"), "video e grande demais"),
            (VideoProcessingError("x"), "converter esse video"),
            (LayoutComputationError("x"), "encaixar esse texto"),
        ]

        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertIn(expected, WhiteTextCog._friendly_error_message(error))

    async def test_cooldown_error_is_answered_friendly(self) -> None:
        cog = WhiteTextCog(self.bot)
        interaction = Mock()
        cooldown = app_commands.Cooldown(1, 20.0)

        with patch.object(cog, "_send_error", new=AsyncMock()) as send_error:
            await cog.cog_app_command_error(interaction, app_commands.CommandOnCooldown(cooldown, 3.2))

        send_error.assert_awaited_once()
        self.assertIn("4s", send_error.await_args.args[1])
