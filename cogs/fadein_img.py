from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from modules.media_processing.fadein_img.errors import (
    AnimatedImageNotSupportedError,
    FadeInImageError,
    ImageTooLargeError,
    InvalidImageError,
    OutputTooLargeError,
    UnsupportedMediaError,
)
from modules.media_processing.fadein_img.processor import FadeInImageConfig, process_fadein_image_bytes
from core.logger import log_exception



LOGGER = logging.getLogger("baphomet.fadein_img")

DEFAULT_CONFIG = FadeInImageConfig()
MAX_INPUT_BYTES = DEFAULT_CONFIG.max_input_bytes
MAX_OUTPUT_BYTES = DEFAULT_CONFIG.max_output_bytes
PROCESSING_TIMEOUT_SECONDS = 20
MAX_CONCURRENT_FADEIN = 2

SUPPORTED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def is_supported_static_image_attachment(attachment: discord.Attachment) -> bool:
    """Pre-validate Discord attachment metadata before downloading bytes."""

    content_type = (getattr(attachment, "content_type", None) or "").split(";", 1)[0].strip().lower()
    suffix = Path(getattr(attachment, "filename", "") or "").suffix.lower()

    if content_type in SUPPORTED_CONTENT_TYPES:
        return True
    if content_type.startswith(("video/", "audio/")):
        return False
    if content_type.startswith("image/"):
        return False
    return suffix in SUPPORTED_EXTENSIONS


class FadeInImageCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._fadein_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FADEIN)

    @app_commands.command(
        name="fadein_img",
        description="Transforma uma imagem em um GIF com fade-in de preto.",
    )
    @app_commands.guild_only()
    @app_commands.describe(anexo="Imagem PNG, JPG, JPEG, WEBP ou GIF estatico.")
    async def fadein_img(
        self,
        interaction: discord.Interaction,
        anexo: discord.Attachment,
    ) -> None:
        try:
            self._validate_permissions(interaction)
            await interaction.response.defer(thinking=True)
            self._validate_initial_input(anexo)

            try:
                data = await anexo.read()
            except discord.HTTPException as exc:
                raise InvalidImageError(
                    "Nao consegui baixar o anexo enviado.",
                    code="attachment_read_failed",
                ) from exc

            upload_limit = self._upload_limit(interaction)
            config = replace(DEFAULT_CONFIG, max_output_bytes=min(upload_limit, MAX_OUTPUT_BYTES))

            async with self._fadein_semaphore:
                buffer, filename = await asyncio.wait_for(
                    asyncio.to_thread(process_fadein_image_bytes, data, config),
                    timeout=PROCESSING_TIMEOUT_SECONDS,
                )

            buffer.seek(0)
            if buffer.getbuffer().nbytes > upload_limit:
                raise OutputTooLargeError(
                    "O GIF final ficou grande demais para enviar no Discord.",
                    code="discord_upload_limit_exceeded",
                )

            await interaction.followup.send(file=discord.File(buffer, filename=filename))
        except asyncio.TimeoutError:
            await self._send_error(
                interaction,
                "Essa imagem demorou demais para processar. Tenta uma menor.",
            )
        except discord.Forbidden:
            await self._send_error(interaction, "Nao tenho permissao para enviar arquivos neste canal.")
        except FadeInImageError as exc:
            await self._send_error(interaction, self._friendly_error_message(exc))
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception(
                "fadein_img_unexpected_failed user_id=%s guild_id=%s attachment=%s",
                getattr(interaction.user, "id", None),
                interaction.guild_id,
                getattr(anexo, "filename", None),
            )
            await self._send_error(
                interaction,
                "Algo deu errado enquanto eu gerava o fade-in. O erro foi registrado para analise.",
            )

    @staticmethod
    def _validate_initial_input(attachment: discord.Attachment | None) -> None:
        if attachment is None:
            raise UnsupportedMediaError(
                "Envie uma imagem PNG, JPG, JPEG ou WEBP junto com o comando.",
                code="attachment_missing",
            )
        if getattr(attachment, "size", None) == 0:
            raise InvalidImageError("O anexo enviado esta vazio.", code="attachment_empty")
        if getattr(attachment, "size", 0) and attachment.size > MAX_INPUT_BYTES:
            raise ImageTooLargeError(
                "Essa imagem e grande demais para processar com seguranca.",
                code="attachment_too_large",
            )
        if not is_supported_static_image_attachment(attachment):
            raise UnsupportedMediaError(
                "Formato de anexo nao suportado.",
                code="unsupported_attachment_type",
            )

    @staticmethod
    def _validate_permissions(interaction: discord.Interaction) -> None:
        permissions = getattr(interaction, "app_permissions", None)
        if permissions is not None:
            if not permissions.send_messages:
                raise FadeInImageError(
                    "Nao tenho permissao para enviar mensagens neste canal.",
                    code="bot_missing_send_messages",
                )
            if not permissions.attach_files:
                raise FadeInImageError(
                    "Nao tenho permissao para enviar arquivos neste canal.",
                    code="bot_missing_attach_files",
                )
            return

        guild = getattr(interaction, "guild", None)
        channel = getattr(interaction, "channel", None)
        me = getattr(guild, "me", None) if guild is not None else None
        if me is None or channel is None or not hasattr(channel, "permissions_for"):
            return
        try:
            channel_permissions = channel.permissions_for(me)
        except (AttributeError, TypeError):
            return
        if not channel_permissions.send_messages:
            raise FadeInImageError(
                "Nao tenho permissao para enviar mensagens neste canal.",
                code="bot_missing_send_messages",
            )
        if not channel_permissions.attach_files:
            raise FadeInImageError(
                "Nao tenho permissao para enviar arquivos neste canal.",
                code="bot_missing_attach_files",
            )

    async def _send_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOGGER.warning("fadein_img_error_response_failed interaction_id=%s", getattr(interaction, "id", None))

    @staticmethod
    def _upload_limit(interaction: discord.Interaction) -> int:
        direct_limit = getattr(interaction, "filesize_limit", None)
        if direct_limit:
            return int(direct_limit)
        guild = getattr(interaction, "guild", None)
        guild_limit = getattr(guild, "filesize_limit", None)
        if guild_limit:
            return int(guild_limit)
        return MAX_OUTPUT_BYTES

    @staticmethod
    def _friendly_error_message(error: FadeInImageError) -> str:
        if error.code in {"bot_missing_send_messages", "bot_missing_attach_files"}:
            return error.user_message
        if isinstance(error, AnimatedImageNotSupportedError):
            return "Esse comando e so para imagem estatica. Se quiser, manda um PNG, JPG ou WEBP."
        if isinstance(error, UnsupportedMediaError):
            return "Esse formato nao e suportado. Envia uma imagem PNG, JPG, JPEG ou WEBP."
        if isinstance(error, InvalidImageError):
            return "Nao consegui abrir essa imagem. Talvez o arquivo esteja corrompido ou nao seja uma imagem de verdade."
        if isinstance(error, ImageTooLargeError):
            return "Essa imagem e grande demais para processar com seguranca. Tenta uma menor."
        if isinstance(error, OutputTooLargeError):
            return "Mesmo comprimindo, o GIF final ficou grande demais para enviar no Discord. Tenta uma imagem menor."
        return "Algo deu errado enquanto eu gerava o fade-in. O erro foi registrado para analise."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FadeInImageCog(bot))
