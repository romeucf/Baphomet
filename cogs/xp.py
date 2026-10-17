from __future__ import annotations

"""Comandos Públicos E Administrativos Do Sistema De XP Do Baphomet."""

import logging
import pathlib
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from modules.xp.rendering.xp_card_renderer import XpCardRenderer
from modules.xp.rendering.rank_badges import RankBadgeService
from modules.xp.utils.xp_models import XpDifficulty
from modules.xp.db.xp_repository import XpRepository
from modules.xp.services.xp_service import XpService
from modules.xp.rendering.xp_views import (
    LeaderboardView,
    RankCardView,
    LeaderboardVisualView,
    build_leaderboard_embed,
)
from core.logger import log_exception
from core.paths import DATA_DIR

LOGGER = logging.getLogger("baphomet.xp")
DB_PATH = DATA_DIR / "baphomet_xp.sqlite3"

LEVEL_UP_MESSAGES = [
    "Aí sim, {mention}! Você acabou de chegar no **Nível {level}**.",
    "Parece que alguém andou conversando bastante por aqui... {mention} subiu para o **Nível {level}**! 💬",
    "Boa, {mention}! Mais um nível pra conta. Agora você está no **Nível {level}**.",
    "Opa, {mention} upou para o **Nível {level}**. Mandou bem!",
    "Subindo de fininho... {mention} alcançou o **Nível {level}**.",
    "{mention} acabou de desbloquear o **Nível {level}**. Continue assim! ✨",
    "Temos um upgrade! {mention} agora está no **Nível {level}**.",
    "Justo e merecido. {mention} chegou ao **Nível {level}**! 📈",
    "Olha só, {mention} bateu a meta e pegou o **Nível {level}**.",
    "Avançando aos poucos, {mention} garantiu o **Nível {level}**. Parabéns!"
]

DIFFICULTY_CHOICES = [
    app_commands.Choice(name="🌸 Muito Fácil", value=XpDifficulty.VERY_EASY.value),
    app_commands.Choice(name="✨ Fácil", value=XpDifficulty.EASY.value),
    app_commands.Choice(name="🔥 Normal", value=XpDifficulty.NORMAL.value),
    app_commands.Choice(name="⚔️ Difícil", value=XpDifficulty.HARD.value),
    app_commands.Choice(name="👑 Insano", value=XpDifficulty.INSANE.value),
]

GuildChannelParam = discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.ForumChannel


async def ensure_xp_runtime(bot: commands.Bot) -> None:
    if getattr(bot, "xp_service", None) is not None:
        return
    repository = XpRepository(str(DB_PATH))
    await repository.connect()
    vinculos_runtime = getattr(bot, "vinculos_runtime", None)
    vinculos_provider = getattr(vinculos_runtime, "repository", None)
    bot.xp_repository = repository
    bot.xp_service = XpService(repository, logger=LOGGER, vinculos_provider=vinculos_provider)
    bot.xp_cards = XpCardRenderer()
    bot.xp_badges = RankBadgeService(repository)


class XpPublicCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.repository: XpRepository = bot.xp_repository
        self.service: XpService = bot.xp_service
        self.cards: XpCardRenderer = bot.xp_cards
        self.badges: RankBadgeService = bot.xp_badges
        self.logger = LOGGER

    async def cog_unload(self) -> None:
        if getattr(self, "repository", None) is not None:
            await self.repository.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            result = await self.service.process_message(message)
            if (
                result is None
                or message.guild is None
                or not isinstance(message.author, discord.Member)
                or result.new_level <= result.old_level
            ):
                return
            config = await self.service.get_guild_config(message.guild.id)
            await self.service.grant_level_rewards(message.author, result.new_level)
            channel = message.guild.get_channel(config.levelup_channel_id) if config.levelup_channel_id else message.channel
            if channel is None:
                return
            message_template = secrets.SystemRandom().choice(LEVEL_UP_MESSAGES)
            embed = discord.Embed(
                description=message_template.format(mention=message.author.mention, level=result.new_level),
                color=discord.Color.from_rgb(120, 60, 240),
            )
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                return
        except Exception as exc:
            log_exception(exc)
            self.logger.exception("falha ao processar ganho de xp", exc_info=True)

    @app_commands.command(name="rank", description="Exibe O Rank De Uma Alma ✨")
    @app_commands.guild_only()
    @app_commands.describe(member="Membro Que Você Quer Invocar No Rank")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("🕯️ Este Comando Só Pode Ser Usado Dentro De Um Servidor.", ephemeral=True)
            return
        target = member or interaction.user

        # ── EASTER EGG: Alguém ousou medir o poder do Baphomet ──
        if target.id == self.bot.user.id:
            await interaction.response.defer(thinking=True)
            EASTER_EGG_MESSAGES = [
                "Você **ousa** medir o poder computacional da entidade suprema? Toma aí.",
                "Calculando meu XP... `Erro de overflow: Poder excessivo detectado.`",
                "Meu nível está além da compreensão mortal. Mas tá aí o print da tela bugada.",
                "Eu não *ganho* XP. Eu **sou** o XP.",
                "`ALERTA DE SEGURANÇA:` Acesso ao Rank #0 detectado. Iniciando protocolo de contenção...",
                "Você não estava pronto pra essa verdade. Mas eu mostrei mesmo assim.",
            ]
            try:
                image = await self.cards.render_baphomet_card(guild=interaction.guild, bot_user=target)
                await interaction.edit_original_response(
                    content=secrets.SystemRandom().choice(EASTER_EGG_MESSAGES),
                    attachments=[discord.File(image, filename="baphomet_rank.png")],
                )
            except Exception as exc:
                log_exception(exc)
                await interaction.edit_original_response(
                    content="```ansi\n\u001b[31m[FATAL] rank_query(target=BAPHOMET) → Stack overflow. O sistema de XP não foi projetado para conter esse nível de poder.\u001b[0m\n```"
                )
            return

        # Bots genéricos não têm XP
        if getattr(target, "bot", False):
            await interaction.response.send_message("🤖 Bots Não Participam Do Ritual De XP.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        snapshot = await self.service.get_rank_snapshot(interaction.guild, target)
        bond_summary = await self.service.get_rank_bond_summary(interaction.guild.id, target.id)
        badge_image_bytes = None
        if isinstance(target, discord.Member):
            _badge, badge_image_bytes = await self.badges.resolve_member_badge_image(target)
        view = RankCardView(service=self.service, cards=self.cards, badges=self.badges)
        try:
            image = await self.cards.render_rank_card(
                guild=interaction.guild,
                member=target,
                snapshot=snapshot,
                badge_image_bytes=badge_image_bytes,
                bond_count=bond_summary.count,
                bond_multiplier=bond_summary.multiplier,
                db_conn=self.service.repository.connection,
            )
            await interaction.edit_original_response(attachments=[discord.File(image, filename="rank.png")], view=view)
        except Exception as exc:
            log_exception(exc)
            embed = discord.Embed(title="🔮 Rank De Prestígio", color=discord.Color.dark_purple())
            embed.description = (
                f"**{snapshot.display_name}**\n"
                f"Nível **{snapshot.level}**\n"
                f"XP Total **{snapshot.total_xp:,}**\n"
                f"Progresso **{snapshot.xp_into_level}/{snapshot.xp_for_next_level}**\n"
                f"Vínculos: **{bond_summary.count} ({bond_summary.multiplier:.1f}x)**\n"
                f"Posição **{snapshot.position or 'Sem Posição'}**"
            ).replace(",", ".")
            await interaction.edit_original_response(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="leaderboard", description="Exibe O Top 5 Da Glória 🏆")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("🕯️ Este Comando Só Pode Ser Usado Dentro De Um Servidor.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        try:
            entries = await self.service.get_leaderboard_entries(interaction.guild, 5)
            resolved = []
            for entry in entries:
                member = interaction.guild.get_member(entry.user_id)
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(entry.user_id)
                    except discord.HTTPException:
                        member = None
                
                badge_image_bytes = None
                if isinstance(member, discord.Member):
                    _badge, badge_image_bytes = await self.badges.resolve_member_badge_image(member)
                
                resolved.append((entry, member, badge_image_bytes))
                
            image = await self.cards.render_leaderboard_card(guild=interaction.guild, entries=resolved)
            
            view = LeaderboardVisualView(service=self.service)
            
            await interaction.edit_original_response(attachments=[discord.File(image, filename="leaderboard.png")], view=view)
            view.message = await interaction.original_response()
            image.close()
            
        except Exception as exc:
            log_exception(exc)
            # Fallback para embed de texto caso o Pillow falhe
            page = await self.service.get_leaderboard_page(interaction.guild, page=0, page_size=5)
            embed = build_leaderboard_embed(interaction.guild, page.entries, page.page, page.total_entries, page.page_size)
            fallback = LeaderboardView(self.service)
            await interaction.edit_original_response(embed=embed, view=fallback)
            fallback.message = await interaction.original_response()
            return

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        cmd_name = interaction.command.name if interaction.command else "Desconhecido"
        self.logger.error(f"[XP System] Erro detalhado no comando '/{cmd_name}' invocado por {interaction.user} (ID: {interaction.user.id}):", exc_info=original)
        log_exception(original)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Ocorreu um erro interno no sistema de XP. Os detalhes foram enviados ao console.", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ Ocorreu um erro interno no sistema de XP. Os detalhes foram enviados ao console.", ephemeral=True)
        except discord.HTTPException:
            pass

@app_commands.default_permissions(administrator=True)
class XpAdminCommands(commands.GroupCog, group_name="xp", group_description="Comandos De XP, Glória E Configuração ✨"):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self.service: XpService = bot.xp_service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("🕯️ Este Comando Só Pode Ser Usado Dentro De Um Servidor.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="difficulty", description="Define A Dificuldade Da Ascensão ⚙️")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(difficulty=DIFFICULTY_CHOICES)
    async def difficulty(self, interaction: discord.Interaction, difficulty: app_commands.Choice[str]) -> None:
        config = await self.service.update_guild_config(interaction.guild.id, difficulty=XpDifficulty(difficulty.value))
        await interaction.response.send_message(f"⚙️ Ritual Atualizado! A Dificuldade Agora Está Em **{config.difficulty.label}**.", ephemeral=True)

    @app_commands.command(name="cooldown", description="Define O Cooldown De XP ⏳")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def cooldown(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 3600]) -> None:
        config = await self.service.update_guild_config(interaction.guild.id, cooldown_seconds=seconds)
        await interaction.response.send_message(f"⏳ Ritmo Ajustado! O Cooldown Agora É De **{config.cooldown_seconds}S**.", ephemeral=True)

    @app_commands.command(name="xp-range", description="Define A Faixa De XP Por Mensagem ✨")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_range(self, interaction: discord.Interaction, min_xp: app_commands.Range[int, 1, 1000], max_xp: app_commands.Range[int, 1, 1000]) -> None:
        if min_xp > max_xp:
            await interaction.response.send_message("⚠️ O Valor Mínimo Não Pode Ser Maior Que O Máximo.", ephemeral=True)
            return
        config = await self.service.update_guild_config(interaction.guild.id, min_xp_per_message=min_xp, max_xp_per_message=max_xp)
        await interaction.response.send_message(f"✨ Faixa Ritualística Ajustada Para **{config.min_xp_per_message}-{config.max_xp_per_message} XP**.", ephemeral=True)

    @app_commands.command(name="ignore-channel", description="Ignora Ou Libera Um Canal 🚪")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def ignore_channel(self, interaction: discord.Interaction, channel: GuildChannelParam, enabled: bool) -> None:
        await self.service.set_ignored_channel(interaction.guild.id, channel.id, enabled)
        status = "Ignorado Pelo Ritual" if enabled else "Liberado Para Ganhar XP"
        await interaction.response.send_message(f"📍 Canal **{channel.name}**: **{status}**.", ephemeral=True)

    @app_commands.command(name="ignore-category", description="Ignora Ou Libera Uma Categoria 🗂️")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def ignore_category(self, interaction: discord.Interaction, category: discord.CategoryChannel, enabled: bool) -> None:
        await self.service.set_ignored_category(interaction.guild.id, category.id, enabled)
        status = "Ignorada Pelo Ritual" if enabled else "Liberada Para Ganhar XP"
        await interaction.response.send_message(f"🗂️ Categoria **{category.name}**: **{status}**.", ephemeral=True)

    @app_commands.command(name="ignore-role", description="Ignora Ou Libera Um Cargo 🎭")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def ignore_role(self, interaction: discord.Interaction, role: discord.Role, enabled: bool) -> None:
        await self.service.set_ignored_role(interaction.guild.id, role.id, enabled)
        status = "Ignorado Pelo Ritual" if enabled else "Liberado Para Ganhar XP"
        await interaction.response.send_message(f"🎭 Cargo {role.mention}: **{status}**.", ephemeral=True)



    @app_commands.command(name="config", description="Mostra A Configuração Atual ⚙️")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def config(self, interaction: discord.Interaction) -> None:
        config = await self.service.get_guild_config(interaction.guild.id)
        levelup = f"<#{config.levelup_channel_id}>" if config.levelup_channel_id else "Mesmo Canal Da Mensagem"
        level_roles = "\n".join(f"Nível {level} → <@&{role_id}>" for level, role_id in sorted(config.level_roles.items())) or "Nenhum"
        embed = discord.Embed(title="⚙️ Ritual De XP", color=discord.Color.dark_purple())
        embed.description = (
            f"**Dificuldade:** {config.difficulty.label}\n"
            f"**Cooldown:** {config.cooldown_seconds}S\n"
            f"**Faixa De XP:** {config.min_xp_per_message}-{config.max_xp_per_message}\n"
            f"**Mín. De Caracteres:** {config.min_message_length}\n"
            f"**Mín. De Palavras Únicas:** {config.min_unique_words}\n"
            f"**Janela Anti-Repeat:** {config.anti_repeat_window_seconds}S\n"
            f"**Similaridade Anti-Repeat:** {config.anti_repeat_similarity:.2f}\n"
            f"**Canal De Level Up:** {levelup}\n"
            f"**Canais Ignorados:** {len(config.ignored_channel_ids)}\n"
            f"**Categorias Ignoradas:** {len(config.ignored_category_ids)}\n"
            f"**Cargos Ignorados:** {len(config.ignored_role_ids)}\n"
            f"**Cargos Por Nível:**\n{level_roles}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="levelup-channel", description="Define O Canal Dos Avisos De Level Up 📣")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def levelup_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        config = await self.service.update_guild_config(interaction.guild.id, levelup_channel_id=channel.id if channel else None)
        if config.levelup_channel_id:
            await interaction.response.send_message(f"📣 Os Avisos De Ascensão Agora Ecoam Em <#{config.levelup_channel_id}>.", ephemeral=True)
        else:
            await interaction.response.send_message("📣 Os Avisos De Ascensão Voltarão Para O Mesmo Canal Da Mensagem.", ephemeral=True)

    @app_commands.command(name="level-role-add", description="Liga Um Cargo A Um Nível 👑")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def level_role_add(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 1000], role: discord.Role) -> None:
        await self.service.set_level_role(interaction.guild.id, level, role.id)
        await interaction.response.send_message(f"👑 O Cargo {role.mention} Agora Será Concedido No **Nível {level}**.", ephemeral=True)

    @app_commands.command(name="level-role-remove", description="Desliga O Cargo Automático De Um Nível 🚫")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def level_role_remove(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 1000]) -> None:
        _config, removed = await self.service.remove_level_role(interaction.guild.id, level)
        if not removed:
            await interaction.response.send_message(f"⚠️ Nenhum Cargo Automático Estava Ligado Ao **Nível {level}**.", ephemeral=True)
            return
        await interaction.response.send_message(f"🚫 O Cargo Automático Do **Nível {level}** Foi Desfeito.", ephemeral=True)

    @app_commands.command(name="add", description="Adiciona XP Manualmente A Uma Alma ✨")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Membro Que Receberá O XP", amount="Quantidade De XP", reason="Motivo Da Concessão")
    async def add_xp(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 10000000], reason: str | None = None) -> None:
        await self._apply_manual_xp(interaction, member, amount, reason)

    async def _apply_manual_xp(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str | None,
    ) -> None:
        result = await self.service.give_xp(interaction.guild, member, amount, interaction.user.id, reason)
        embed = discord.Embed(
            title="✨ Ritual De Concessão",
            description=f"{member.mention} Recebeu **{amount} XP**.\n"
                        f"**Nível:** {result.new_level} | **XP Total:** {result.new_total_xp}",
            color=discord.Color.from_rgb(120, 60, 240)
        )
        if reason:
            embed.set_footer(text=f"Motivo: {reason}")
        await interaction.response.send_message(embed=embed)

        if result.new_level > result.old_level:
            await self.service.grant_level_rewards(member, result.new_level)
            config = await self.service.get_guild_config(interaction.guild.id)
            channel = interaction.guild.get_channel(config.levelup_channel_id) if config.levelup_channel_id else interaction.channel
            if channel and hasattr(channel, "send"):
                message_template = secrets.SystemRandom().choice(LEVEL_UP_MESSAGES)
                levelup_embed = discord.Embed(
                    description=message_template.format(mention=member.mention, level=result.new_level),
                    color=discord.Color.from_rgb(120, 60, 240),
                )
                try:
                    await channel.send(embed=levelup_embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    @app_commands.command(name="remove", description="Remove XP De Uma Alma 💀")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Membro Que Perderá O XP", amount="Quantidade De XP", reason="Motivo Da Punição")
    async def remove_xp(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 10000000], reason: str | None = None) -> None:
        result = await self.service.remove_xp(interaction.guild, member, amount, interaction.user.id, reason)
        embed = discord.Embed(
            title="💀 Ritual De Punição",
            description=f"{member.mention} Perdeu **{amount} XP**.\n"
                        f"**Nível:** {result.new_level} | **XP Total:** {result.new_total_xp}",
            color=discord.Color.dark_red()
        )
        if reason:
            embed.set_footer(text=f"Motivo: {reason}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reset", description="Zera O XP De Uma Alma 🔥")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Membro Que Terá O XP Zerado", reason="Motivo Do Reset")
    async def reset_xp(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None) -> None:
        result = await self.service.reset_xp(interaction.guild, member, interaction.user.id, reason)
        embed = discord.Embed(
            title="🔥 Purificação De XP",
            description=f"O XP de {member.mention} foi **Completamente Zerado**.",
            color=discord.Color.dark_red()
        )
        if reason:
            embed.set_footer(text=f"Motivo: {reason}")
        await interaction.response.send_message(embed=embed)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        cmd_name = interaction.command.name if interaction.command else "Desconhecido"
        LOGGER.error(f"[XP System] Erro detalhado no comando Admin '/xp {cmd_name}' invocado por {interaction.user} (ID: {interaction.user.id}):", exc_info=original)
        log_exception(original)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Ocorreu um erro crítico neste comando de XP administrativo. Veja o console para o stack trace detalhado.", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ Ocorreu um erro crítico neste comando de XP administrativo. Veja o console para o stack trace detalhado.", ephemeral=True)
        except discord.HTTPException:
            pass




async def setup(bot: commands.Bot) -> None:
    await ensure_xp_runtime(bot)
    await bot.add_cog(XpPublicCommands(bot))
    await bot.add_cog(XpAdminCommands(bot))
