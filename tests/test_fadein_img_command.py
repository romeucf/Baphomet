from __future__ import annotations

import unittest
from types import SimpleNamespace

import discord
from discord.ext import commands

from cogs.fadein_img import FadeInImageCog, is_supported_static_image_attachment
from modules.media_processing.fadein_img.errors import (
    AnimatedImageNotSupportedError,
    ImageTooLargeError,
    InvalidImageError,
    OutputTooLargeError,
    UnsupportedMediaError,
)


class FadeInImageCommandHelpersTests(unittest.TestCase):
    def test_supported_attachment_uses_content_type(self) -> None:
        self.assertTrue(is_supported_static_image_attachment(SimpleNamespace(content_type="image/png", filename="x.bin")))
        self.assertTrue(is_supported_static_image_attachment(SimpleNamespace(content_type="image/jpeg", filename="x.bin")))
        self.assertTrue(is_supported_static_image_attachment(SimpleNamespace(content_type="image/webp", filename="x.bin")))

    def test_supported_attachment_uses_extension_fallback(self) -> None:
        self.assertTrue(is_supported_static_image_attachment(SimpleNamespace(content_type=None, filename="FOTO.PNG")))
        self.assertTrue(is_supported_static_image_attachment(SimpleNamespace(content_type=None, filename="foto.jpeg")))
        self.assertTrue(is_supported_static_image_attachment(SimpleNamespace(content_type="application/octet-stream", filename="foto.webp")))

    def test_unsupported_attachment_rejects_video_and_unknown_image(self) -> None:
        self.assertFalse(is_supported_static_image_attachment(SimpleNamespace(content_type="video/mp4", filename="foto.png")))
        self.assertFalse(is_supported_static_image_attachment(SimpleNamespace(content_type="image/bmp", filename="foto.bmp")))


class FadeInImageCogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

    async def asyncTearDown(self) -> None:
        await self.bot.close()

    async def test_extension_registers_fadein_img_slash_command(self) -> None:
        await self.bot.load_extension("cogs.fadein_img")

        command_names = [command.qualified_name for command in self.bot.tree.walk_commands()]
        self.assertIn("fadein_img", command_names)

    def test_initial_validation(self) -> None:
        cog = FadeInImageCog(self.bot)

        with self.assertRaises(UnsupportedMediaError):
            cog._validate_initial_input(None)
        with self.assertRaises(InvalidImageError):
            cog._validate_initial_input(SimpleNamespace(size=0, content_type="image/png", filename="x.png"))
        with self.assertRaises(ImageTooLargeError):
            cog._validate_initial_input(SimpleNamespace(size=16 * 1024 * 1024, content_type="image/png", filename="x.png"))
        with self.assertRaises(UnsupportedMediaError):
            cog._validate_initial_input(SimpleNamespace(size=1, content_type="video/mp4", filename="x.mp4"))

    def test_permission_validation_uses_app_permissions(self) -> None:
        cog = FadeInImageCog(self.bot)

        with self.assertRaisesRegex(Exception, "mensagens"):
            cog._validate_permissions(SimpleNamespace(app_permissions=SimpleNamespace(send_messages=False, attach_files=True)))
        with self.assertRaisesRegex(Exception, "arquivos"):
            cog._validate_permissions(SimpleNamespace(app_permissions=SimpleNamespace(send_messages=True, attach_files=False)))

    def test_friendly_error_messages_are_portuguese_and_specific(self) -> None:
        cases = [
            (UnsupportedMediaError("x"), "formato nao e suportado"),
            (InvalidImageError("x"), "Nao consegui abrir essa imagem"),
            (AnimatedImageNotSupportedError("x"), "imagem estatica"),
            (ImageTooLargeError("x"), "grande demais"),
            (OutputTooLargeError("x"), "Mesmo comprimindo"),
        ]

        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertIn(expected, FadeInImageCog._friendly_error_message(error))
