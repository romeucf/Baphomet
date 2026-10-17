from __future__ import annotations

import asyncio
import io
import logging
import math
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from modules.media_processing.whitetext.errors import (
    FFmpegNotFoundError,
    FontNotFoundError,
    ImageTooLargeError,
    InvalidImageError,
    InvalidTextError,
    InvalidVideoError,
    LayoutComputationError,
    OutputTooLargeError,
    TextTooLongError,
    UnsupportedMediaError,
    VideoProcessingError,
    VideoTooLargeError,
    VideoTooLongError,
    WhiteTextError,
)
from modules.media_processing.whitetext.layout import CaptionStyle
from modules.media_processing.whitetext.processor import MediaKind, WhiteTextProcessorConfig, process_gif_bytes, process_static_image_bytes
from modules.media_processing.whitetext.video import VideoProcessingConfig, process_video_bytes_to_captioned_gif
from core.logger import log_exception



LOGGER = logging.getLogger("baphomet.whitetext")

MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
PROCESSING_TIMEOUT_SECONDS = 75
MAX_CONCURRENT_PROCESSES = 2
MAX_TEXT_CHARS = CaptionStyle().max_chars
MAX_VIDEO_SECONDS = 8.0
VIDEO_OUTPUT_FPS = 12
VIDEO_MAX_WIDTH = 640
COOLDOWN_SECONDS = 20.0

STATIC_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
GIF_CONTENT_TYPES = {"image/gif"}
VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "video/x-msvideo"}
STATIC_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
GIF_EXTENSIONS = {".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}


def classify_attachment(attachment: discord.Attachment, data: bytes) -> MediaKind:
    """Classify an attachment using content type, extension, then byte-level validation fallback."""

    content_type = (getattr(attachment, "content_type", None) or "").split(";", 1)[0].strip().lower()
    suffix = Path(getattr(attachment, "filename", "") or "").suffix.lower()

    if content_type in GIF_CONTENT_TYPES:
        return MediaKind.GIF
    if content_type in STATIC_CONTENT_TYPES:
        return MediaKind.STATIC_IMAGE
    if content_type in VIDEO_CONTENT_TYPES:
        return MediaKind.VIDEO

    if suffix in GIF_EXTENSIONS:
        return MediaKind.GIF
    if suffix in STATIC_EXTENSIONS:
        return MediaKind.STATIC_IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO

    from modules.media_processing.whitetext.processor import detect_media_kind

    return detect_media_kind(
        data,
        filename=getattr(attachment, "filename", "") or "",
        content_type=getattr(attachment, "content_type", None),
    )


def process_media_bytes(
    data: bytes,
    text: str,
    media_kind: MediaKind,
    static_config: WhiteTextProcessorConfig,
    video_config: VideoProcessingConfig,
    style: CaptionStyle,
) -> tuple[io.BytesIO, str]:
    """Synchronous processing entrypoint intended for asyncio.to_thread."""

    media_value = getattr(media_kind, "value", str(media_kind))
    if media_value == MediaKind.STATIC_IMAGE.value:
        return process_static_image_bytes(data, text, config=static_config, style=style)
    if media_value == MediaKind.GIF.value:
        return process_gif_bytes(data, text, config=static_config, style=style)
    if media_value == MediaKind.VIDEO.value:
        return process_video_bytes_to_captioned_gif(
            data,
            text,
            video_config=video_config,
            processor_config=static_config,
            style=style,
        )
    raise UnsupportedMediaError(
        "Esse formato nao e suportado. Envie PNG, JPG, WEBP, GIF ou video MP4/MOV/WEBM.",
        code="unsupported_media_type",
    )


class WhiteTextCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._whitetext_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROCESSES)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Handle app-command checks that fail before the command body runs."""

        if isinstance(error, app_commands.CommandOnCooldown):
            seconds = max(1, math.ceil(error.retry_after))
            await self._send_error(interaction, f"Calma um pouquinho: tente de novo em {seconds}s.")
            return

        LOGGER.error(
            "whitetext_app_command_error interaction_id=%s",
            getattr(interaction, "id", None),
            exc_info=error,
        )
        await self._send_error(
            interaction,
            "Algo deu errado enquanto eu gerava o meme. O erro foi registrado para analise.",
        )

    @app_commands.command(
        name="whitetext",
        description="Adiciona uma legenda branca estilo meme no topo de uma imagem, GIF ou video.",
    )
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS, key=lambda interaction: interaction.user.id)
    @app_commands.describe(
        anexo="Imagem, GIF ou video para aplicar a legenda.",
        texto="Texto que aparecera na barra branca.",
    )
    async def whitetext(
        self,
        interaction: discord.Interaction,
        anexo: discord.Attachment,
        texto: app_commands.Range[str, 1, MAX_TEXT_CHARS],
    ) -> None:
        try:
            self._validate_permissions(interaction)
            self._validate_initial_input(anexo, str(texto))
            await interaction.response.defer(thinking=True)

            try:
                data = await anexo.read(use_cached=True)
            except discord.HTTPException as exc:
                raise WhiteTextError("Nao consegui baixar o anexo enviado.", code="attachment_read_failed") from exc

            if not data:
                raise UnsupportedMediaError("O anexo enviado esta vazio.", code="attachment_empty")

            media_kind = classify_attachment(anexo, data)
            upload_limit = self._upload_limit(interaction)
            static_config = WhiteTextProcessorConfig(
                max_input_bytes=MAX_INPUT_BYTES,
                max_output_bytes=min(upload_limit, MAX_OUTPUT_BYTES),
            )
            video_config = VideoProcessingConfig(
                max_video_input_bytes=MAX_INPUT_BYTES,
                max_video_duration_seconds=MAX_VIDEO_SECONDS,
                output_fps=VIDEO_OUTPUT_FPS,
                max_output_width=VIDEO_MAX_WIDTH,
            )
            style = CaptionStyle(max_chars=MAX_TEXT_CHARS)

            async with self._whitetext_semaphore:
                result_buffer, filename = await asyncio.wait_for(
                    asyncio.to_thread(
                        process_media_bytes,
                        data,
                        str(texto),
                        media_kind,
                        static_config,
                        video_config,
                        style,
                    ),
                    timeout=PROCESSING_TIMEOUT_SECONDS,
                )

            result_buffer.seek(0)
            if result_buffer.getbuffer().nbytes > upload_limit:
                await interaction.followup.send(
                    "O resultado ficou grande demais para enviar no Discord. Tente uma midia menor ou um texto menor.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(content="Prontinho.", file=discord.File(result_buffer, filename=filename))
        except asyncio.TimeoutError:
            await self._send_error(
                interaction,
                "O processamento demorou demais e foi cancelado. Tente uma midia menor.",
            )
        except discord.Forbidden:
            await self._send_error(interaction, "Nao tenho permissao para enviar arquivos neste canal.")
        except WhiteTextError as exc:
            await self._send_error(interaction, self._friendly_error_message(exc))
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception(
                "whitetext_unexpected_failed user_id=%s guild_id=%s attachment=%s",
                getattr(interaction.user, "id", None),
                interaction.guild_id,
                getattr(anexo, "filename", None),
            )
            await self._send_error(
                interaction,
                "Algo deu errado enquanto eu gerava o meme. O erro foi registrado para analise.",
            )

    @staticmethod
    def _validate_initial_input(attachment: discord.Attachment | None, text: str) -> None:
        if attachment is None:
            raise UnsupportedMediaError("Envie uma imagem, GIF ou video junto com o comando.", code="attachment_missing")
        if not text or not text.strip():
            raise InvalidTextError("Informe um texto para colocar na barrinha branca.", code="text_empty")
        if len(text.strip()) > MAX_TEXT_CHARS:
            raise TextTooLongError(f"O texto pode ter no maximo {MAX_TEXT_CHARS} caracteres.", code="text_too_long")
        if attachment.size and attachment.size > MAX_INPUT_BYTES:
            raise ImageTooLargeError("Essa midia e grande demais para processar com seguranca.", code="attachment_too_large")

    @staticmethod
    def _validate_permissions(interaction: discord.Interaction) -> None:
        permissions = getattr(interaction, "app_permissions", None)
        if permissions is not None:
            if not permissions.send_messages:
                raise WhiteTextError("Estou sem permissao de enviar mensagens neste canal.", code="bot_missing_send_messages")
            if not permissions.attach_files:
                raise WhiteTextError("Estou sem permissao de anexar arquivos neste canal.", code="bot_missing_attach_files")
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
            raise WhiteTextError("Estou sem permissao de enviar mensagens neste canal.", code="bot_missing_send_messages")
        if not channel_permissions.attach_files:
            raise WhiteTextError("Estou sem permissao de anexar arquivos neste canal.", code="bot_missing_attach_files")

    async def _send_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOGGER.warning("whitetext_error_response_failed interaction_id=%s", getattr(interaction, "id", None))

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
    def _friendly_error_message(error: WhiteTextError) -> str:
        if isinstance(error, FontNotFoundError):
            return "A fonte FuturaCEB.otf nao foi encontrada em ../assets/fonts/. Coloque o arquivo la e tente de novo."
        if isinstance(error, InvalidTextError):
            return "Voce precisa escrever um texto valido para colocar na barrinha branca."
        if isinstance(error, TextTooLongError):
            return "Esse texto esta grande demais para virar meme. Tenta reduzir um pouco."
        if isinstance(error, UnsupportedMediaError):
            return "Esse formato nao e suportado. Envie PNG, JPG, WEBP, GIF ou video MP4/MOV/WEBM."
        if isinstance(error, InvalidImageError):
            return "Nao consegui abrir essa imagem. Talvez o arquivo esteja corrompido ou nao seja uma imagem de verdade."
        if isinstance(error, ImageTooLargeError):
            return "Essa midia e grande demais para processar com seguranca. Tente uma menor."
        if isinstance(error, OutputTooLargeError):
            return "O resultado ficou grande demais para enviar no Discord. Tente uma midia menor, um GIF mais curto ou um video mais curto."
        if isinstance(error, FFmpegNotFoundError):
            return "O servidor onde o bot esta rodando nao tem FFmpeg instalado, entao ainda nao consigo processar videos."
        if isinstance(error, VideoTooLongError):
            return "Esse video e longo demais para converter em GIF. Tente um video mais curto."
        if isinstance(error, VideoTooLargeError):
            return "Esse video e grande demais para converter em GIF. Tente um video menor."
        if isinstance(error, InvalidVideoError):
            return "Nao consegui abrir esse video. Talvez o arquivo esteja corrompido ou nao tenha uma faixa de video."
        if isinstance(error, VideoProcessingError):
            return "Nao consegui converter esse video em GIF. Tente outro arquivo."
        if isinstance(error, LayoutComputationError):
            return "Nao consegui encaixar esse texto nessa midia. Tente uma midia maior ou um texto menor."
        return "Algo deu errado enquanto eu gerava o meme. O erro foi registrado para analise."


# Backward-compatible alias for any tests/imports that use the previous spelling.
WhitetextCog = WhiteTextCog


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WhiteTextCog(bot))
