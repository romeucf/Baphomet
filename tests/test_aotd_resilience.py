from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from discord import app_commands

from cogs.aotd import AlbumDoDia
from cogs.server_config import ServerConfig


def make_cog() -> AlbumDoDia:
    return object.__new__(AlbumDoDia)


class AOTDDataResilienceTests(unittest.IsolatedAsyncioTestCase):
    def test_public_aotd_commands_remain_registered(self) -> None:
        command_names = {
            command.qualified_name for command in AlbumDoDia.aotd.walk_commands()
        }

        self.assertEqual(command_names, {"aotd sugerir", "aotd fila"})

    def test_malformed_legacy_album_still_builds_embed(self) -> None:
        cog = make_cog()
        album = {
            "nome": "Álbum",
            "artista": "Artista",
            "url": "https://[endereco-invalido",
            "imagem": "arquivo-local.png",
            "duracao_ms": "não é número",
            "generos": "não é lista",
        }

        embed = cog.criar_embed_album(album)

        self.assertIsNone(embed.url)
        self.assertIsNone(embed.image.url)
        self.assertIn("0min 00s", embed.footer.text)

    async def test_invalid_configured_channel_id_is_treated_as_unconfigured(self) -> None:
        cog = make_cog()
        cog.bot = Mock()

        channel = await cog.resolve_text_channel("id-corrompido")

        self.assertIsNone(channel)
        cog.bot.get_channel.assert_not_called()

    def test_aotd_error_log_contains_correlation_and_interaction_context(self) -> None:
        response = Mock()
        response.is_done.return_value = True
        interaction = SimpleNamespace(
            id=123,
            command=SimpleNamespace(qualified_name="aotd sugerir"),
            user=SimpleNamespace(id=456),
            guild=SimpleNamespace(id=789),
            channel=SimpleNamespace(id=321),
            response=response,
            command_failed=True,
        )
        error = RuntimeError("spotify indisponível")

        with (
            patch("cogs.aotd.log_exception") as rich_log,
            patch("cogs.aotd.LOGGER.error") as standard_log,
        ):
            error_id = AlbumDoDia.log_aotd_exception(
                error,
                interaction=interaction,
                phase="test_phase",
                album_url="https://open.spotify.com/album/123",
            )

        self.assertEqual(len(error_id), 12)
        context = rich_log.call_args.kwargs["context"]
        self.assertIn(f"error_id='{error_id}'", context)
        self.assertIn("phase='test_phase'", context)
        self.assertIn("command='aotd sugerir'", context)
        self.assertIn("user_id=456", context)
        self.assertIn("guild_id=789", context)
        self.assertIn("channel_id=321", context)
        rich_log.assert_called_once_with(error, context=context, interaction=interaction)
        standard_log.assert_called_once()


class AOTDCommandResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_config_proxy_invokes_real_aotd_command_callback(self) -> None:
        group = object.__new__(ServerConfig)
        aotd = make_cog()
        aotd.carregar_fila = Mock(return_value=[])

        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            defer=AsyncMock(),
        )
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            client=SimpleNamespace(get_cog=Mock(return_value=aotd)),
            response=response,
            followup=followup,
        )

        await group._call_cog(interaction, "AlbumDoDia", "listar_fila")

        response.defer.assert_awaited_once_with(ephemeral=True)
        followup.send.assert_awaited_once_with(
            "📭 A fila está vazia.",
            ephemeral=True,
        )

    async def test_unexpected_command_error_is_logged_before_response(self) -> None:
        cog = make_cog()
        cog.log_aotd_exception = Mock(return_value="abc123def456")
        cog.send_error_response = AsyncMock()
        interaction = Mock()
        error = app_commands.CommandInvokeError(Mock(), RuntimeError("banco bloqueado"))

        await cog.cog_app_command_error(interaction, error)

        cog.log_aotd_exception.assert_called_once()
        cog.send_error_response.assert_awaited_once()
        self.assertIn("abc123def456", cog.send_error_response.await_args.args[1])

    async def test_staff_notification_failure_does_not_turn_suggestion_into_failure(self) -> None:
        cog = make_cog()
        album = {
            "nome": "Nome",
            "artista": "Artista",
            "url": "https://open.spotify.com/album/123",
            "imagem": "https://i.scdn.co/image/123",
            "tipo_lancamento": "Álbum",
            "lancamento_permitido": True,
        }
        cog.buscar_album_spotify = AsyncMock(return_value=album)
        cog.album_ja_na_fila = Mock(return_value=False)
        cog.adicionar_album_na_fila = Mock(return_value=4)
        cog.notificar_staff_nova_sugestao = AsyncMock(
            side_effect=RuntimeError("canal da staff indisponível")
        )
        cog.log_aotd_exception = Mock(return_value="abc123def456")

        interaction = SimpleNamespace(
            user=SimpleNamespace(name="tester", id=42),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await AlbumDoDia.sugerir.callback(cog, interaction, "Nome")

        interaction.followup.send.assert_awaited_once()
        cog.log_aotd_exception.assert_called_once()
        self.assertEqual(
            cog.log_aotd_exception.call_args.kwargs["phase"],
            "staff_notification_after_successful_suggestion",
        )

    async def test_server_config_proxy_logs_aotd_error_before_reraising(self) -> None:
        group = object.__new__(ServerConfig)
        aotd = SimpleNamespace(
            listar_fila=AsyncMock(side_effect=RuntimeError("sqlite locked")),
            log_aotd_exception=Mock(),
        )
        interaction = SimpleNamespace(
            client=SimpleNamespace(get_cog=Mock(return_value=aotd))
        )

        with self.assertRaisesRegex(RuntimeError, "sqlite locked"):
            await group._call_cog(interaction, "AlbumDoDia", "listar_fila")

        aotd.log_aotd_exception.assert_called_once()
        self.assertEqual(
            aotd.log_aotd_exception.call_args.kwargs["phase"],
            "server_config_proxy",
        )


if __name__ == "__main__":
    unittest.main()
