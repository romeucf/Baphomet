from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

import discord
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from core.database import DEFAULT_SCHEDULE_TIME, DatabaseManager
from modules.movies.movie_logic import (
    DEFAULT_DISLIKE_EMOJI,
    DEFAULT_LIKE_EMOJI,
    DEFAULT_NEVER_WATCHED_EMOJI,
    post_movie_of_the_day,
    send_motd_recap,
    validate_reaction_emoji,
)
from modules.movies.tmdb_api import TMDBClient
from core.logger import log_exception



LOGGER = logging.getLogger("baphomet.movie_cog")

SCHEDULER_TIMEZONE: Final[str] = "America/Sao_Paulo"
TIME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:[01]\d|2[0-3]):[0-5]\d$"
)
BLACKLIST_PAGE_SIZE: Final[int] = 10
JOB_ID_PREFIX: Final[str] = "movie_job_"
UNSET: Final[object] = object()


@dataclass(frozen=True, slots=True)
class MovieGuildConfig:
    guild_id: int
    channel_id: int | None
    role_id: int | None
    schedule_time: str
    is_active: bool = True
    like_emoji: str | None = None
    dislike_emoji: str | None = None
    never_watched_emoji: str | None = None
    recap_active: bool = False
    recap_time: str = "18:00"


class MovieCog(commands.Cog):
    motd_config = app_commands.Group(
        name="motd_config",
        description="Configuração e operação do Filme do Dia.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_manager = DatabaseManager()
        self.tmdb_client = TMDBClient()
        self.scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)
        self._initial_reschedule_done = False

    async def cog_load(self) -> None:
        await self.db_manager.init_db()
        self.bot.motd_db_manager = self.db_manager
        if not self.scheduler.running:
            self.scheduler.start()
        LOGGER.info("MovieCog inicializado com APScheduler em %s.", SCHEDULER_TIMEZONE)

    async def cog_unload(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.tmdb_client.close()
        await self.db_manager.close()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._initial_reschedule_done:
            return

        self._initial_reschedule_done = True
        for guild in self.bot.guilds:
            config = await self.db_manager.get_config(guild.id)
            if config is None:
                continue

            try:
                self._reschedule_guild(guild.id, config)
            except ValueError:
                LOGGER.error(
                    "Horario invalido ao reagendar guild_id=%s schedule_time=%s.",
                    guild.id,
                    getattr(config, "schedule_time", DEFAULT_SCHEDULE_TIME),
                    exc_info=True,
                )

    def _reschedule_guild(self, guild_id: int, config) -> None:
        time_str = getattr(config, "schedule_time", DEFAULT_SCHEDULE_TIME) or DEFAULT_SCHEDULE_TIME
        hour, minute = self._parse_schedule_time(time_str)
        trigger = CronTrigger(hour=hour, minute=minute, timezone=SCHEDULER_TIMEZONE)
        self.scheduler.add_job(
            post_movie_of_the_day,
            trigger=trigger,
            id=f"{JOB_ID_PREFIX}{guild_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            kwargs={
                "bot": self.bot,
                "guild_id": guild_id,
                "db_manager": self.db_manager,
                "tmdb_client": self.tmdb_client,
                "is_test": False,
            },
        )
        
        if getattr(config, "recap_active", False):
            recap_time = getattr(config, "recap_time", "18:00")
            try:
                r_hour, r_minute = self._parse_schedule_time(recap_time)
                r_trigger = CronTrigger(day_of_week="fri", hour=r_hour, minute=r_minute, timezone=SCHEDULER_TIMEZONE)
                self.scheduler.add_job(
                    send_motd_recap,
                    trigger=r_trigger,
                    id=f"recap_{JOB_ID_PREFIX}{guild_id}",
                    replace_existing=True,
                    kwargs={"bot": self.bot, "guild_id": guild_id, "db_manager": self.db_manager, "is_test": False}
                )
            except Exception as exc:
                log_exception(exc)
                pass
        else:
            job_id = f"recap_{JOB_ID_PREFIX}{guild_id}"
            try:
                if self.scheduler.get_job(job_id):
                    self.scheduler.remove_job(job_id)
            except JobLookupError:
                LOGGER.warning(f"[MovieCog] O job {job_id} não existia no registro para ser removido. Procedendo com a criação de um novo agendamento de fallback.")
            except Exception as exc:
                log_exception(exc)
                pass

        LOGGER.info(
            "Agendamento do Filme do Dia atualizado guild_id=%s horario=%s.",
            guild_id,
            time_str,
        )

    @motd_config.command(
        name="toggle",
        description="Ativa ou desativa o envio automático diário do Filme do Dia neste servidor.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def toggle(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        current = await self.db_manager.get_config(guild_id)
        config = self._normalize_config(guild_id, current)
        next_state = not config.is_active

        await self._save_config(guild_id, is_active=next_state)
        
        status_text = "Ligado" if next_state else "Desligado"
        await interaction.response.send_message(
            f"O Filme do Dia foi **{status_text}** neste servidor.",
            ephemeral=True,
        )

    @motd_config.command(
        name="horario",
        description="Define o horário diário do Filme do Dia no formato HH:MM.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(horario="Horário em formato HH:MM, usando America/Sao_Paulo.")
    async def horario(self, interaction: discord.Interaction, horario: str) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        if TIME_PATTERN.fullmatch(horario) is None:
            await interaction.response.send_message(
                "Formato inválido. Use `HH:MM`, por exemplo `18:00`.",
                ephemeral=True,
            )
            return

        config = await self._save_config(guild_id, schedule_time=horario)
        self._reschedule_guild(guild_id, config)
        await interaction.response.send_message(
            f"Horário do Filme do Dia configurado para `{config.schedule_time}`.",
            ephemeral=True,
        )

    @motd_config.command(
        name="canal",
        description="Define o canal onde o Filme do Dia será publicado.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(canal="Canal textual de publicação.")
    async def canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        config = await self._save_config(guild_id, channel_id=canal.id)
        self._reschedule_guild(guild_id, config)
        await interaction.response.send_message(
            f"Canal do Filme do Dia configurado para {canal.mention}.",
            ephemeral=True,
        )

    @motd_config.command(
        name="cargo",
        description="Define o cargo mencionado nas publicações do Filme do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(cargo="Cargo mencionado junto ao Filme do Dia.")
    async def cargo(
        self,
        interaction: discord.Interaction,
        cargo: discord.Role,
    ) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        config = await self._save_config(guild_id, role_id=cargo.id)
        self._reschedule_guild(guild_id, config)
        await interaction.response.send_message(
            f"Cargo do Filme do Dia configurado para {cargo.mention}.",
            ephemeral=True,
        )

    @motd_config.command(
        name="emojis",
        description="Define os emojis das reações do Filme do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        like="Emoji usado para 'Eu gosto desse filme'.",
        dislike="Emoji usado para 'Não gosto desse filme'.",
        never_watched="Emoji usado para 'Nunca assisti esse filme'.",
    )
    async def emojis(
        self,
        interaction: discord.Interaction,
        like: str,
        dislike: str,
        never_watched: str,
    ) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        emojis = {
            "like": self._normalize_optional_text(like),
            "dislike": self._normalize_optional_text(dislike),
            "never_watched": self._normalize_optional_text(never_watched),
        }

        for name, emoji in emojis.items():
            error = validate_reaction_emoji(emoji or "")
            if error is not None:
                await interaction.response.send_message(
                    f"O emoji informado em `{name}` é inválido: {error}",
                    ephemeral=True,
                )
                return

        config = await self._save_config(
            guild_id,
            like_emoji=emojis["like"],
            dislike_emoji=emojis["dislike"],
            never_watched_emoji=emojis["never_watched"],
        )
        await interaction.response.send_message(
            "Emojis do Filme do Dia atualizados:\n"
            f"{config.like_emoji} — Eu gosto desse filme\n"
            f"{config.dislike_emoji} — Não gosto desse filme\n"
            f"{config.never_watched_emoji} — Nunca assisti esse filme",
            ephemeral=True,
        )

    @motd_config.command(
        name="resetar_emojis",
        description="Restaura os emojis padrão das reações do Filme do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def resetar_emojis(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        await self._save_config(
            guild_id,
            like_emoji=None,
            dislike_emoji=None,
            never_watched_emoji=None,
        )
        await interaction.response.send_message(
            "Emojis do Filme do Dia restaurados para os padrões:\n"
            f"{DEFAULT_LIKE_EMOJI} — Eu gosto desse filme\n"
            f"{DEFAULT_DISLIKE_EMOJI} — Não gosto desse filme\n"
            f"{DEFAULT_NEVER_WATCHED_EMOJI} — Nunca assisti esse filme",
            ephemeral=True,
        )

    @motd_config.command(
        name="status",
        description="Mostra todas as configurações atuais do Filme do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        current = await self.db_manager.get_config(guild_id)
        config = self._normalize_config(guild_id, current)
        embed = self._build_status_embed(config, has_saved_config=current is not None)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @motd_config.command(
        name="remover_blacklist",
        description="Remove um filme da blacklist pelo ID do TMDB.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(id="ID numérico do filme no TMDB.")
    async def remover_blacklist(self, interaction: discord.Interaction, id: int) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        removed = await self.db_manager.remove_from_blacklist(guild_id, id)
        if removed:
            message = f"Filme `{id}` removido da blacklist."
        else:
            message = f"Nenhum registro com TMDB ID `{id}` foi encontrado."

        await interaction.response.send_message(message, ephemeral=True)

    @motd_config.command(
        name="blacklist",
        description="Lista os filmes registrados na blacklist deste servidor.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def blacklist(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        await interaction.response.defer(ephemeral=True)
        entries = await self.db_manager.get_blacklist(guild_id)
        if not entries:
            await interaction.followup.send(
                "A blacklist deste servidor ainda está vazia.",
                ephemeral=True,
            )
            return

        total_pages = (len(entries) + BLACKLIST_PAGE_SIZE - 1) // BLACKLIST_PAGE_SIZE
        for page_index, start in enumerate(
            range(0, len(entries), BLACKLIST_PAGE_SIZE),
            start=1,
        ):
            chunk = entries[start : start + BLACKLIST_PAGE_SIZE]
            embed = self._build_blacklist_embed(chunk, page_index, total_pages)
            await interaction.followup.send(embed=embed, ephemeral=True)

    @motd_config.command(
        name="testar",
        description="Publica um Filme do Dia de teste sem gravar na blacklist.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def testar(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        await interaction.response.defer(ephemeral=True)
        try:
            posted = await post_movie_of_the_day(
                self.bot,
                guild_id,
                self.db_manager,
                self.tmdb_client,
                is_test=True,
            )
        except Exception as exc:
            log_exception(exc)
            LOGGER.error(
                "Falha ao executar /test_movie guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            await interaction.followup.send(
                "Não consegui publicar o filme de teste agora.",
                ephemeral=True,
            )
            return

        if posted:
            message = "Filme de teste publicado sem alterar a blacklist."
        else:
            message = "O filme de teste não foi publicado. Verifique a configuração e permissões."

        await interaction.followup.send(message, ephemeral=True)

    @motd_config.command(
        name="fila",
        description="Lista cronologicamente a fila de sugestões do Filme do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def fila(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        await interaction.response.defer(ephemeral=True)
        entries = await self.db_manager.get_motd_queue(guild_id)
        if not entries:
            await interaction.followup.send("A fila de sugestões está vazia.", ephemeral=True)
            return

        all_lines = []
        for i, entry in enumerate(entries, start=1):
            title = str(getattr(entry, "movie_title", "N/A"))
            user_id = getattr(entry, "user_id_sugestao", None)
            date_added = str(getattr(entry, "date_added", "N/A"))[:10]
            line = f"**{i}.** {title} (por <@{user_id}>) - *{date_added}*"
            all_lines.append(line)
        
        description = "\n".join(all_lines)
        if len(description) <= 4096:
            embed = discord.Embed(
                title="Fila de Sugestões do Filme do Dia",
                description=description,
                color=discord.Color.blue()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            PAGE_SIZE = 10
            total_pages = (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE
            for page_index, start in enumerate(range(0, len(entries), PAGE_SIZE), start=1):
                chunk_lines = all_lines[start : start + PAGE_SIZE]
                embed = discord.Embed(
                    title=f"Fila de Sugestões do Filme do Dia ({page_index}/{total_pages})",
                    description="\n".join(chunk_lines),
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="sugerir_motd",
        description="Sugira um filme para a fila do Filme do Dia!",
    )
    @app_commands.describe(filme="Nome do filme que deseja sugerir.")
    @app_commands.guild_only()
    async def sugerir_motd(self, interaction: discord.Interaction, filme: str) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        await interaction.response.defer(ephemeral=True)

        config = await self.db_manager.get_config(guild_id)
        if config is None or getattr(config, "is_active", False) is False:
            await interaction.followup.send("O Filme do Dia está desativado neste servidor.", ephemeral=True)
            return

        movie = await self.tmdb_client.search_movie(filme)
        if movie is None:
            await interaction.followup.send(f"Não encontrei nenhum filme com o nome `{filme}` no TMDB.", ephemeral=True)
            return

        if await self.db_manager.is_blacklisted(guild_id, movie.tmdb_id):
            await interaction.followup.send(f"O filme **{movie.title}** já foi exibido recentemente e está na blacklist.", ephemeral=True)
            return

        if await self.db_manager.is_in_motd_queue(guild_id, movie.tmdb_id):
            await interaction.followup.send(f"O filme **{movie.title}** já está na fila de sugestões!", ephemeral=True)
            return

        success = await self.db_manager.add_to_motd_queue(
            guild_id,
            movie.tmdb_id,
            movie.title,
            interaction.user.id
        )

        if not success:
            await interaction.followup.send("Falha ao adicionar o filme na fila. Tente novamente mais tarde.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎬 Filme adicionado à fila!",
            description=f"**{movie.title}** foi sugerido com sucesso e adicionado ao final da fila.",
            color=discord.Color.green()
        )
        if movie.poster_url and movie.poster_url != "N/A":
            embed.set_thumbnail(url=movie.poster_url)
        
        embed.add_field(name="Gêneros", value=movie.genres, inline=True)
        embed.add_field(name="Lançamento", value=movie.release_date, inline=True)
        
        avatar_url = interaction.user.display_avatar.url if interaction.user.display_avatar else None
        embed.set_footer(text=f"TMDB ID: {movie.tmdb_id} • Sugerido por {interaction.user.display_name}", icon_url=avatar_url)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @sugerir_motd.error
    async def sugerir_motd_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.NoPrivateMessage):
            await interaction.response.send_message("Esse comando só pode ser usado dentro de um servidor.", ephemeral=True)
        else:
            LOGGER.error("Erro no comando sugerir_motd.", exc_info=True)
            if interaction.response.is_done():
                await interaction.followup.send("Ocorreu um erro ao sugerir o filme.", ephemeral=True)
            else:
                await interaction.response.send_message("Ocorreu um erro ao sugerir o filme.", ephemeral=True)

    @toggle.error
    @horario.error
    @canal.error
    @cargo.error
    @emojis.error
    @resetar_emojis.error
    @status.error
    @remover_blacklist.error
    @blacklist.error
    @fila.error
    @testar.error
    async def movie_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "Você precisa ser administrador(a) para usar esse comando."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "Esse comando só pode ser usado dentro de um servidor."
        else:
            LOGGER.error(
                "Erro em comando do MovieCog command=%s.",
                interaction.command.name if interaction.command else "?",
                exc_info=True,
            )
            message = "Ocorreu um erro ao executar o comando do Filme do Dia."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @motd_config.command(
        name="toggle_recap",
        description="Ativa ou desativa o envio automático do Recap Semanal de filmes (Sexta-feira).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def toggle_recap(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        current = await self.db_manager.get_config(guild_id)
        config = self._normalize_config(guild_id, current)
        next_state = not config.recap_active

        config = await self._save_config(guild_id, recap_active=next_state)
        self._reschedule_guild(guild_id, config)
        
        status_text = "Ligado" if next_state else "Desligado"
        await interaction.response.send_message(
            f"O Recap Semanal do Filme do Dia foi **{status_text}**.",
            ephemeral=True,
        )

    @motd_config.command(
        name="recap_time",
        description="Define o horário do Recap Semanal do Filme do Dia (Sextas) no formato HH:MM.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(horario="Horário em formato HH:MM, usando America/Sao_Paulo.")
    async def recap_time(self, interaction: discord.Interaction, horario: str) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        if TIME_PATTERN.fullmatch(horario) is None:
            await interaction.response.send_message(
                "Formato inválido. Use `HH:MM`, por exemplo `18:00`.",
                ephemeral=True,
            )
            return

        config = await self._save_config(guild_id, recap_time=horario)
        self._reschedule_guild(guild_id, config)
        await interaction.response.send_message(
            f"Horário do Recap Semanal configurado para `{config.recap_time}`.",
            ephemeral=True,
        )

    @motd_config.command(
        name="test_recap",
        description="Testa o envio do Recap Semanal do Filme do Dia agora.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def test_recap(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            return

        await interaction.response.defer(ephemeral=False)
        success = await send_motd_recap(
            bot=self.bot,
            guild_id=guild_id,
            db_manager=self.db_manager,
            is_test=True,
        )
        
        if not success:
            await interaction.followup.send("Não foi possível enviar o Recap (Verifique o canal, permissões ou se o recap está vazio).", ephemeral=True)

    async def _save_config(
        self,
        guild_id: int,
        *,
        channel_id: int | None | object = UNSET,
        role_id: int | None | object = UNSET,
        schedule_time: str | object = UNSET,
        is_active: bool | object = UNSET,
        like_emoji: str | None | object = UNSET,
        dislike_emoji: str | None | object = UNSET,
        never_watched_emoji: str | None | object = UNSET,
        recap_active: bool | object = UNSET,
        recap_time: str | object = UNSET,
    ) -> MovieGuildConfig:
        current = await self.db_manager.get_config(guild_id)
        config = self._normalize_config(guild_id, current)

        next_channel_id = (
            config.channel_id if channel_id is UNSET else self._coerce_optional_int(channel_id)
        )
        next_role_id = (
            config.role_id if role_id is UNSET else self._coerce_optional_int(role_id)
        )
        next_schedule_time = (
            config.schedule_time if schedule_time is UNSET else str(schedule_time)
        )
        next_is_active = (
            config.is_active if is_active is UNSET else bool(is_active)
        )
        next_like_emoji = (
            config.like_emoji
            if like_emoji is UNSET
            else self._normalize_optional_text(like_emoji)
        )
        next_dislike_emoji = (
            config.dislike_emoji
            if dislike_emoji is UNSET
            else self._normalize_optional_text(dislike_emoji)
        )
        next_never_watched_emoji = (
            config.never_watched_emoji
            if never_watched_emoji is UNSET
            else self._normalize_optional_text(never_watched_emoji)
        )
        next_recap_active = (
            config.recap_active if recap_active is UNSET else bool(recap_active)
        )
        next_recap_time = (
            config.recap_time if recap_time is UNSET else str(recap_time)
        )

        await self.db_manager.set_config(
            guild_id,
            next_channel_id,
            next_role_id,
            next_schedule_time,
            is_active=next_is_active,
            like_emoji=next_like_emoji,
            dislike_emoji=next_dislike_emoji,
            never_watched_emoji=next_never_watched_emoji,
            recap_active=next_recap_active,
            recap_time=next_recap_time,
        )

        return MovieGuildConfig(
            guild_id=guild_id,
            channel_id=next_channel_id,
            role_id=next_role_id,
            schedule_time=next_schedule_time,
            is_active=next_is_active,
            like_emoji=next_like_emoji,
            dislike_emoji=next_dislike_emoji,
            never_watched_emoji=next_never_watched_emoji,
            recap_active=next_recap_active,
            recap_time=next_recap_time,
        )

    @staticmethod
    def _normalize_config(guild_id: int, config: object | None) -> MovieGuildConfig:
        if config is None:
            return MovieGuildConfig(
                guild_id=guild_id,
                channel_id=None,
                role_id=None,
                schedule_time=DEFAULT_SCHEDULE_TIME,
                is_active=True,
                like_emoji=None,
                dislike_emoji=None,
                never_watched_emoji=None,
                recap_active=False,
                recap_time="18:00",
            )

        return MovieGuildConfig(
            guild_id=guild_id,
            channel_id=MovieCog._coerce_optional_int(getattr(config, "channel_id", None)),
            role_id=MovieCog._coerce_optional_int(getattr(config, "role_id", None)),
            schedule_time=str(
                getattr(config, "schedule_time", DEFAULT_SCHEDULE_TIME)
                or DEFAULT_SCHEDULE_TIME
            ),
            is_active=bool(getattr(config, "is_active", True)),
            like_emoji=MovieCog._normalize_optional_text(
                getattr(config, "like_emoji", None)
            ),
            dislike_emoji=MovieCog._normalize_optional_text(
                getattr(config, "dislike_emoji", None)
            ),
            never_watched_emoji=MovieCog._normalize_optional_text(
                getattr(config, "never_watched_emoji", None)
            ),
            recap_active=bool(getattr(config, "recap_active", False)),
            recap_time=str(getattr(config, "recap_time", "18:00")),
        )

    @staticmethod
    def _parse_schedule_time(time_str: str) -> tuple[int, int]:
        if TIME_PATTERN.fullmatch(time_str) is None:
            raise ValueError(f"Horario invalido: {time_str}")

        hour_text, minute_text = time_str.split(":", maxsplit=1)
        return int(hour_text), int(minute_text)

    @staticmethod
    def _interaction_guild_id(interaction: discord.Interaction) -> int | None:
        if interaction.guild_id is None:
            return None
        return int(interaction.guild_id)

    @staticmethod
    def _coerce_optional_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None

        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

        return parsed if parsed > 0 else None

    @staticmethod
    def _normalize_optional_text(value: object) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _build_status_embed(
        config: MovieGuildConfig,
        *,
        has_saved_config: bool,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Status do Filme do Dia",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Status",
            value="🟢 Ligado" if config.is_active else "🔴 Desligado",
            inline=False,
        )

        embed.add_field(
            name="Canal de publicação",
            value=MovieCog._format_channel_status(config.channel_id),
            inline=False,
        )
        embed.add_field(
            name="Cargo mencionado",
            value=MovieCog._format_role_status(config.role_id),
            inline=False,
        )
        embed.add_field(
            name="Horário diário",
            value=f"`{config.schedule_time}` ({SCHEDULER_TIMEZONE})",
            inline=False,
        )
        embed.add_field(
            name="Emojis de reação",
            value="\n".join(
                (
                    MovieCog._format_emoji_status(
                        "Eu gosto desse filme",
                        config.like_emoji,
                        DEFAULT_LIKE_EMOJI,
                    ),
                    MovieCog._format_emoji_status(
                        "Não gosto desse filme",
                        config.dislike_emoji,
                        DEFAULT_DISLIKE_EMOJI,
                    ),
                    MovieCog._format_emoji_status(
                        "Nunca assisti esse filme",
                        config.never_watched_emoji,
                        DEFAULT_NEVER_WATCHED_EMOJI,
                    ),
                )
            ),
            inline=False,
        )

        if not has_saved_config:
            embed.add_field(
                name="Observação",
                value=(
                    "Ainda não há configuração salva para este servidor; "
                    "os valores padrão foram exibidos onde se aplicam."
                ),
                inline=False,
            )

        return embed

    @staticmethod
    def _format_channel_status(channel_id: int | None) -> str:
        if channel_id is None:
            return "Não definido"
        return f"<#{channel_id}> (`{channel_id}`)"

    @staticmethod
    def _format_role_status(role_id: int | None) -> str:
        if role_id is None:
            return "Não definido"
        return f"<@&{role_id}> (`{role_id}`)"

    @staticmethod
    def _format_emoji_status(label: str, configured: str | None, default: str) -> str:
        emoji = configured or default
        source = "configurado" if configured is not None else "padrão"
        return f"{emoji} — {label} ({source})"

    @staticmethod
    def _build_blacklist_embed(
        entries: list[object],
        page_index: int,
        total_pages: int,
    ) -> discord.Embed:
        lines = ["TMDB ID    | Título                         | Data"]
        lines.append("-" * 60)

        for entry in entries:
            tmdb_id = str(getattr(entry, "tmdb_id", "N/A"))
            title = str(getattr(entry, "movie_title", None) or "N/A")
            date_added = str(getattr(entry, "date_added", "N/A"))
            lines.append(
                f"{tmdb_id[:10]:<10} | {title[:30]:<30} | {date_added[:16]}"
            )

        embed = discord.Embed(
            title=f"Blacklist de Filmes ({page_index}/{total_pages})",
            description=f"```text\n{chr(10).join(lines)}\n```",
            color=discord.Color.dark_gold(),
        )
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MovieCog(bot))


__all__ = [
    "MovieCog",
    "setup",
]
