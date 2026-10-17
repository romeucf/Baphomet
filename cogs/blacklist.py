from __future__ import annotations

import contextlib
import inspect
import logging
from pathlib import Path
from typing import Callable, Awaitable, Any

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from core.paths import DATA_DIR


LOGGER = logging.getLogger("baphomet.blacklist")


class BlacklistCog(commands.Cog):
    """Bloqueia o uso de comandos do bot em canais específicos."""

    MANAGEMENT_COMMANDS = {
        "bloquear",
        "desbloquear",
        "status-canais",
        "status_canais",
        "listanegra",
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_file: Path = DATA_DIR / "blacklist.sqlite3"
        self._blacklisted_channels: set[int] = set()
        self.blacklisted_channels = self._blacklisted_channels

        self._prefix_check = self._global_prefix_check
        self._original_tree_interaction_check: Callable[..., Any] | None = None
        self._patched_tree_interaction_check: Callable[..., Awaitable[bool]] | None = None

    async def cog_load(self) -> None:
        await self._init_db()
        self.bot.add_check(self._prefix_check)
        self._patch_app_command_check()

    def cog_unload(self) -> None:
        self.bot.remove_check(self._prefix_check)
        self._restore_app_command_check()

    async def _init_db(self) -> None:
        """Inicializa o banco de forma assíncrona e carrega os canais bloqueados para o cache."""
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(self.db_file), timeout=5) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS blacklist (
                    channel_id INTEGER PRIMARY KEY
                )
                """
            )
            async with db.execute("SELECT channel_id FROM blacklist") as cursor:
                rows = await cursor.fetchall()
            self._blacklisted_channels.clear()
            self._blacklisted_channels.update(int(row[0]) for row in rows)
            await db.commit()

    async def _adicionar_ao_banco(self, channel_id: int) -> None:
        async with aiosqlite.connect(str(self.db_file), timeout=5) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            await db.execute(
                "INSERT OR IGNORE INTO blacklist (channel_id) VALUES (?)",
                (channel_id,),
            )
            await db.commit()

    async def _remover_do_banco(self, channel_id: int) -> None:
        async with aiosqlite.connect(str(self.db_file), timeout=5) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            await db.execute(
                "DELETE FROM blacklist WHERE channel_id = ?",
                (channel_id,),
            )
            await db.commit()

    def _is_management_prefix_command(self, ctx: commands.Context) -> bool:
        if ctx.command is None:
            return False

        names = {ctx.command.name, ctx.command.qualified_name}
        names.update(getattr(ctx.command, "aliases", []) or [])

        return any(name in self.MANAGEMENT_COMMANDS for name in names)

    def _is_management_app_command(self, interaction: discord.Interaction) -> bool:
        command = getattr(interaction, "command", None)
        if command is not None:
            qualified_name = getattr(command, "qualified_name", None)
            name = getattr(command, "name", None)

            candidates = {value for value in (qualified_name, name) if value}
            for candidate in candidates:
                root_name = str(candidate).split(" ", maxsplit=1)[0]
                if candidate in self.MANAGEMENT_COMMANDS or root_name in self.MANAGEMENT_COMMANDS:
                    return True

        data = interaction.data if isinstance(interaction.data, dict) else {}
        raw_name = data.get("name")
        if isinstance(raw_name, str):
            root_name = raw_name.split(" ", maxsplit=1)[0]
            return raw_name in self.MANAGEMENT_COMMANDS or root_name in self.MANAGEMENT_COMMANDS

        return False

    async def _global_prefix_check(self, ctx: commands.Context) -> bool:
        """Bloqueia prefix commands em canais blacklistados."""
        channel_id = getattr(ctx.channel, "id", None)
        if channel_id is None:
            return True

        if int(channel_id) not in self.blacklisted_channels:
            return True

        if self._is_management_prefix_command(ctx):
            return True

        with contextlib.suppress(discord.HTTPException, discord.Forbidden):
            await ctx.reply(
                "🚫 Este canal está selado. Comandos do bot não funcionam aqui.",
                mention_author=False,
            )

        return False

    def _patch_app_command_check(self) -> None:
        """Aplica uma trava global para slash commands também."""
        if self._patched_tree_interaction_check is not None:
            return

        self._original_tree_interaction_check = self.bot.tree.interaction_check

        async def patched_tree_interaction_check(interaction: discord.Interaction) -> bool:
            if self._original_tree_interaction_check is not None:
                original_result = self._original_tree_interaction_check(interaction)
                if inspect.isawaitable(original_result):
                    original_result = await original_result

                if not original_result:
                    return False

            return await self._global_app_check(interaction)

        self._patched_tree_interaction_check = patched_tree_interaction_check
        self.bot.tree.interaction_check = patched_tree_interaction_check  # type: ignore[method-assign]

    def _restore_app_command_check(self) -> None:
        if (
            self._original_tree_interaction_check is not None
            and self._patched_tree_interaction_check is not None
            and self.bot.tree.interaction_check is self._patched_tree_interaction_check
        ):
            self.bot.tree.interaction_check = self._original_tree_interaction_check  # type: ignore[method-assign]

        self._original_tree_interaction_check = None
        self._patched_tree_interaction_check = None

    async def _global_app_check(self, interaction: discord.Interaction) -> bool:
        """Bloqueia slash commands em canais blacklistados."""
        channel_id = getattr(interaction.channel, "id", None)
        if channel_id is None:
            channel_id = getattr(interaction, "channel_id", None)

        if channel_id is None:
            return True

        if int(channel_id) not in self.blacklisted_channels:
            return True

        if self._is_management_app_command(interaction):
            return True

        message = "🚫 Este canal está selado. Comandos do bot não funcionam aqui."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden):
            LOGGER.warning("não foi possível avisar bloqueio de slash command channel_id=%s", channel_id)

        return False

    async def _send_ctx(
        self,
        ctx: commands.Context,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
    ) -> None:
        if ctx.interaction is not None:
            await ctx.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await ctx.send(content=content, embed=embed)

    async def bloquear_canal(self, ctx: commands.Context, canal: discord.TextChannel | None = None) -> None:
        canal = canal or ctx.channel

        if not isinstance(canal, discord.TextChannel):
            await self._send_ctx(ctx, "❌ Escolha um canal de texto válido.", ephemeral=True)
            return

        if canal.id in self.blacklisted_channels:
            await self._send_ctx(
                ctx,
                f"👁️ O canal {canal.mention} já está selado. O silêncio já mora ali.",
                ephemeral=True,
            )
            return

        self.blacklisted_channels.add(canal.id)
        await self._adicionar_ao_banco(canal.id)

        await self._send_ctx(
            ctx,
            f"✅ O canal {canal.mention} foi adicionado à lista negra. Comandos não funcionarão mais lá.",
        )

    async def desbloquear_canal(self, ctx: commands.Context, canal: discord.TextChannel | None = None) -> None:
        canal = canal or ctx.channel

        if not isinstance(canal, discord.TextChannel):
            await self._send_ctx(ctx, "❌ Escolha um canal de texto válido.", ephemeral=True)
            return

        if canal.id not in self.blacklisted_channels:
            await self._send_ctx(
                ctx,
                f"📜 O canal {canal.mention} não está na lista negra.",
                ephemeral=True,
            )
            return

        self.blacklisted_channels.remove(canal.id)
        await self._remover_do_banco(canal.id)

        await self._send_ctx(
            ctx,
            f"✅ O canal {canal.mention} foi removido da lista negra. Comandos liberados.",
        )

    async def status_canais(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await self._send_ctx(ctx, "❌ Este comando só funciona dentro de um servidor.", ephemeral=True)
            return

        canais_mencionados: list[str] = []

        for channel_id in sorted(self.blacklisted_channels):
            canal_obj = guild.get_channel(channel_id)

            if canal_obj is None:
                global_channel = self.bot.get_channel(channel_id)
                if global_channel is not None and getattr(global_channel, "guild", None) != guild:
                    continue

                canais_mencionados.append(f"Canal deletado/desconhecido (`{channel_id}`)")
                continue

            canais_mencionados.append(canal_obj.mention)

        if not canais_mencionados:
            await self._send_ctx(
                ctx,
                "📜 Nenhum canal deste servidor está na lista negra de comandos no momento.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🚫 Canais na lista negra",
            description="Os comandos do bot estão desativados nos seguintes canais:\n\n" + "\n".join(canais_mencionados),
            color=discord.Color.red(),
        )
        embed.set_footer(text="Use /desbloquear para liberar um canal novamente.")

        await self._send_ctx(ctx, embed=embed)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        error = getattr(error, "original", error)

        if isinstance(error, commands.MissingPermissions):
            await self._send_ctx(
                ctx,
                "❌ Você precisa ser administrador para usar este comando.",
                ephemeral=True,
            )
            return

        if isinstance(error, commands.NoPrivateMessage):
            await self._send_ctx(
                ctx,
                "❌ Este comando só funciona dentro de um servidor.",
                ephemeral=True,
            )
            return

        if isinstance(error, commands.BadArgument):
            await self._send_ctx(
                ctx,
                "❌ Não consegui encontrar esse canal. Tenta mencionar o canal direitinho.",
                ephemeral=True,
            )
            return

        LOGGER.exception("erro em comando de blacklist", exc_info=error)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(original, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            message = "❌ Você precisa ser administrador para usar este comando."
        elif isinstance(original, app_commands.NoPrivateMessage):
            message = "❌ Este comando só funciona dentro de um servidor."
        else:
            LOGGER.exception("erro em slash command de blacklist", exc_info=original)
            message = "❌ Algo deu errado ao mexer na lista negra."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOGGER.warning("não foi possível responder erro de blacklist")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BlacklistCog(bot))