from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
import discord
from discord.ext import commands
from core.logger import log_exception
from core.paths import DATA_DIR


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GuildLogConfig:
    guild_id: int
    enabled: bool = False
    channel_id: Optional[int] = None


@dataclass(slots=True)
class AuditLookupResult:
    entry: Optional[discord.AuditLogEntry] = None
    permission_error: bool = False


class PunishmentLogs(commands.Cog):
    """
    cog de logs de punições com persistência em sqlite.

    monitora:
    - banimentos
    - expulsões (kick)
    - timeouts

    persistência:
    - enabled
    - channel_id

    observação importante:
    `on_member_remove` dispara tanto para saída voluntária quanto para kick.
    para diferenciar os dois casos, este cog consulta o audit log de kick:
    se houver uma entrada recente cujo alvo seja o mesmo usuário removido,
    trata como kick; se não houver, assume que foi saída voluntária e não loga.
    """

    def __init__(self, bot: commands.Bot, db_path: str | Path = DATA_DIR / "punishment_logs.sqlite3") -> None:
        self.bot = bot
        self.db_path = Path(db_path)
        self.db_lock = asyncio.Lock()
        self.config_cache: dict[int, GuildLogConfig] = {}

        # evita duplicar logs caso o mesmo audit entry seja encontrado
        self._recent_audit_entry_ids: list[int] = []
        self._recent_audit_entry_id_set: set[int] = set()
        self._recent_audit_entry_limit = 256

    async def cog_load(self) -> None:
        await self._init_database()
        await self._load_cache()

    # =========================
    # banco de dados / cache
    # =========================

    async def _init_database(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS punishment_log_settings (
                    guild_id   INTEGER PRIMARY KEY,
                    enabled    INTEGER NOT NULL DEFAULT 0,
                    channel_id INTEGER
                )
                """
            )
            await db.commit()

    async def _load_cache(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT guild_id, enabled, channel_id FROM punishment_log_settings"
            ) as cursor:
                rows = await cursor.fetchall()

        self.config_cache.clear()
        for guild_id, enabled, channel_id in rows:
            self.config_cache[guild_id] = GuildLogConfig(
                guild_id=guild_id,
                enabled=bool(enabled),
                channel_id=channel_id,
            )

    async def _save_config(self, guild_id: int, enabled: bool, channel_id: Optional[int]) -> None:
        async with self.db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO punishment_log_settings (guild_id, enabled, channel_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id)
                    DO UPDATE SET
                        enabled = excluded.enabled,
                        channel_id = excluded.channel_id
                    """,
                    (guild_id, int(enabled), channel_id),
                )
                await db.commit()

        self.config_cache[guild_id] = GuildLogConfig(
            guild_id=guild_id,
            enabled=enabled,
            channel_id=channel_id,
        )

    def _get_config(self, guild_id: int) -> GuildLogConfig:
        return self.config_cache.get(guild_id, GuildLogConfig(guild_id=guild_id))

    def _is_logging_enabled(self, guild_id: int) -> bool:
        cfg = self._get_config(guild_id)
        return cfg.enabled and cfg.channel_id is not None

    def _get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cfg = self._get_config(guild.id)
        if not cfg.enabled or cfg.channel_id is None:
            return None

        channel = guild.get_channel(cfg.channel_id)
        if channel is None:
            logger.warning(
                "punishment logs: canal %s não encontrado na guild %s",
                cfg.channel_id,
                guild.id,
            )
            return None

        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "punishment logs: canal %s da guild %s não é um TextChannel",
                cfg.channel_id,
                guild.id,
            )
            return None

        return channel

    # =========================
    # método /server_config logs setup
    # =========================

    async def setup_logs(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        status: bool,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "esse comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        me = interaction.guild.me
        if me is None:
            await interaction.response.send_message(
                "não consegui identificar o membro do bot neste servidor para validar permissões.",
                ephemeral=True,
            )
            return

        perms = channel.permissions_for(me)
        missing = []

        if not perms.view_channel:
            missing.append("view_channel")
        if not perms.send_messages:
            missing.append("send_messages")
        if not perms.embed_links:
            missing.append("embed_links")

        if missing:
            await interaction.response.send_message(
                "não posso usar esse canal porque estou sem as permissões: "
                f"`{', '.join(missing)}`",
                ephemeral=True,
            )
            return

        await self._save_config(
            guild_id=interaction.guild.id,
            enabled=status,
            channel_id=channel.id,
        )

        state = "ativados" if status else "desativados"
        await interaction.response.send_message(
            f"logs de punições **{state}** em {channel.mention}.",
            ephemeral=True,
        )

    async def status_logs(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message("esse comando só pode ser usado dentro de um servidor.", ephemeral=True)

        cfg = self._get_config(interaction.guild.id)
        
        embed = discord.Embed(title="⚖️ Status — Logs de Punições")
        
        if not cfg.enabled or cfg.channel_id is None:
            embed.description = "⚠️ Nenhuma configuração definida para este módulo (ou está desativado)."
            embed.color = discord.Color.red()
        else:
            embed.description = f"🟢 **Ativo**\nLogs de banimentos, expulsões e timeouts serão enviados para: <#{cfg.channel_id}>"
            embed.color = discord.Color.green()
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # =========================
    # listeners
    # =========================

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self._is_logging_enabled(guild.id):
            return

        audit_result = await self._fetch_matching_audit_entry(
            guild=guild,
            action=discord.AuditLogAction.ban,
            target_id=user.id,
            delay=1.0,
        )

        if audit_result.entry and self._was_audit_entry_processed(audit_result.entry.id):
            return

        if audit_result.entry:
            self._remember_audit_entry(audit_result.entry.id)

        await self._send_punishment_log(
            guild=guild,
            punishment_type="ban",
            punished_user=user,
            audit_result=audit_result,
            timeout_until=None,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """
        como o discord não distingue diretamente no evento se foi leave ou kick,
        buscamos no audit log uma entrada recente de kick para o mesmo alvo.

        se não existir essa entrada compatível:
        -> tratamos como saída voluntária
        -> não enviamos log

        se existir:
        -> tratamos como kick
        -> enviamos log
        """
        guild = member.guild

        if not self._is_logging_enabled(guild.id):
            return

        audit_result = await self._fetch_matching_audit_entry(
            guild=guild,
            action=discord.AuditLogAction.kick,
            target_id=member.id,
            delay=1.25,
        )

        # sem entrada correspondente = provavelmente leave voluntário
        if audit_result.entry is None:
            if audit_result.permission_error:
                logger.warning(
                    "punishment logs: sem permissão para audit logs na guild %s; "
                    "não é possível diferenciar leave de kick com segurança.",
                    guild.id,
                )
            return

        if self._was_audit_entry_processed(audit_result.entry.id):
            return

        self._remember_audit_entry(audit_result.entry.id)

        await self._send_punishment_log(
            guild=guild,
            punishment_type="kick",
            punished_user=member,
            audit_result=audit_result,
            timeout_until=None,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        try:
            if not self._is_logging_enabled(after.guild.id):
                return

            # Uso defensivo de getattr para suportar tanto discord.py (timed_out_until)
            # quanto antigas versões/forks (communication_disabled_until) de forma resiliente.
            before_timeout = getattr(before, "timed_out_until", getattr(before, "communication_disabled_until", None))
            after_timeout = getattr(after, "timed_out_until", getattr(after, "communication_disabled_until", None))

            if before_timeout == after_timeout:
                return

            now = discord.utils.utcnow()

            # Determina o tipo de evento relacionado a timeout
            is_new_timeout = before_timeout is None and after_timeout is not None and after_timeout > now
            is_removed_timeout = before_timeout is not None and (after_timeout is None or after_timeout <= now)
            is_modified_timeout = before_timeout is not None and after_timeout is not None and after_timeout > now and before_timeout != after_timeout

            # Se não for nenhum dos três (ex: timeout expirou naturalmente sem update direto), ignorar
            if not (is_new_timeout or is_removed_timeout or is_modified_timeout):
                return
            
            # Loga apenas adições ou modificações de timeout.
            # O escopo original do bot foca na *punição*. Mas se desejar logar a remoção, basta adaptar o `if` abaixo.
            if is_removed_timeout:
                # Caso deseje logar remoções de timeout no futuro, adicione a lógica aqui.
                return

            audit_result = await self._fetch_matching_audit_entry(
                guild=after.guild,
                action=discord.AuditLogAction.member_update,
                target_id=after.id,
                delay=1.0,
            )

            if audit_result.entry and self._was_audit_entry_processed(audit_result.entry.id):
                return

            if audit_result.entry:
                self._remember_audit_entry(audit_result.entry.id)

            await self._send_punishment_log(
                guild=after.guild,
                punishment_type="timeout",
                punished_user=after,
                audit_result=audit_result,
                timeout_until=after_timeout,
            )

        except Exception as e:
            log_exception(e)
            logger.exception("Erro inesperado em on_member_update ao processar timeout do membro %s: %s", after.id, e)

    # =========================
    # audit logs
    # =========================

    async def _fetch_matching_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int,
        *,
        delay: float = 1.0,
        max_age_seconds: int = 20,
    ) -> AuditLookupResult:
        """
        busca uma entrada de audit log correspondente ao evento.

        estratégia:
        1) espera um pouco para dar tempo do audit log propagar
        2) consulta limit=1, como pedido
        3) se a entrada mais recente não bater por alvo/tempo, faz um fallback
           curto com limit=5 para reduzir falso negativo em servidores movimentados
        """
        await asyncio.sleep(delay)

        try:
            # tentativa principal: exatamente como solicitado
            async for entry in guild.audit_logs(limit=1, action=action):
                if self._audit_entry_matches(entry, target_id, max_age_seconds):
                    return AuditLookupResult(entry=entry)

            # fallback opcional para robustez extra
            async for entry in guild.audit_logs(limit=5, action=action):
                if self._audit_entry_matches(entry, target_id, max_age_seconds):
                    return AuditLookupResult(entry=entry)

        except discord.Forbidden:
            logger.warning(
                "punishment logs: sem permissão para ler audit logs na guild %s",
                guild.id,
            )
            return AuditLookupResult(entry=None, permission_error=True)

        except discord.HTTPException as exc:
            logger.exception(
                "punishment logs: falha ao consultar audit logs na guild %s: %s",
                guild.id,
                exc,
            )
            return AuditLookupResult(entry=None, permission_error=False)

        return AuditLookupResult(entry=None, permission_error=False)

    def _audit_entry_matches(
        self,
        entry: discord.AuditLogEntry,
        target_id: int,
        max_age_seconds: int,
    ) -> bool:
        target = entry.target
        if target is None or getattr(target, "id", None) != target_id:
            return False

        now = discord.utils.utcnow()
        age = abs((now - entry.created_at).total_seconds())
        return age <= max_age_seconds

    # =========================
    # embed / envio
    # =========================

    async def _send_punishment_log(
        self,
        guild: discord.Guild,
        punishment_type: str,
        punished_user: discord.abc.User,
        audit_result: AuditLookupResult,
        timeout_until: Optional[discord.utils.utcnow] = None,
    ) -> None:
        channel = self._get_log_channel(guild)
        if channel is None:
            return

        entry = audit_result.entry
        moderator = entry.user if entry else None
        reason = entry.reason if entry and entry.reason else "não informado"
        occurred_at = discord.utils.utcnow()

        if entry is not None:
            infraction_id = str(entry.id)
        else:
            infraction_id = self._build_fallback_infraction_id(
                guild_id=guild.id,
                user_id=punished_user.id,
                punishment_type=punishment_type,
                occurred_at=occurred_at,
            )

        embed = self._build_embed(
            punishment_type=punishment_type,
            punished_user=punished_user,
            moderator=moderator,
            reason=reason,
            occurred_at=occurred_at,
            infraction_id=infraction_id,
            timeout_until=timeout_until,
            audit_permission_error=audit_result.permission_error,
        )

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning(
                "punishment logs: sem permissão para enviar embed no canal %s da guild %s",
                channel.id,
                guild.id,
            )
        except discord.HTTPException as exc:
            logger.exception(
                "punishment logs: falha ao enviar log no canal %s da guild %s: %s",
                channel.id,
                guild.id,
                exc,
            )

    def _build_embed(
        self,
        punishment_type: str,
        punished_user: discord.abc.User,
        moderator: Optional[discord.abc.User],
        reason: str,
        occurred_at,
        infraction_id: str,
        timeout_until,
        audit_permission_error: bool,
    ) -> discord.Embed:
        now = discord.utils.utcnow()

        if punishment_type == "ban":
            title = "🔴 banimento"
            colour = discord.Colour.red()
        elif punishment_type == "kick":
            title = "🟠 expulsão"
            colour = discord.Colour.orange()
        else:
            title = "🟡 castigo"
            colour = discord.Colour.gold()

        embed = discord.Embed(title=title, colour=colour, timestamp=occurred_at)

        embed.add_field(
            name="usuário punido",
            value=self._format_punished_user(punished_user),
            inline=False,
        )

        embed.add_field(
            name="id do usuário",
            value=f"`{punished_user.id}`",
            inline=True,
        )

        if moderator is not None:
            moderator_value = f"{moderator.mention}\n`{moderator}`"
        elif audit_permission_error:
            moderator_value = "não foi possível identificar\n`sem acesso aos audit logs`"
        else:
            moderator_value = "não identificado"

        embed.add_field(
            name="moderador responsável",
            value=moderator_value,
            inline=True,
        )

        embed.add_field(
            name="motivo",
            value=reason or "não informado",
            inline=False,
        )

        if punishment_type == "timeout" and timeout_until is not None:
            remaining = timeout_until - now
            if remaining.total_seconds() < 0:
                remaining = timedelta(seconds=0)

            embed.add_field(
                name="duração",
                value=(
                    f"até {discord.utils.format_dt(timeout_until, style='F')}\n"
                    f"restante: {self._humanize_timedelta(remaining)}"
                ),
                inline=False,
            )

        account_age = now - punished_user.created_at
        embed.add_field(
            name="data da conta",
            value=(
                f"{discord.utils.format_dt(punished_user.created_at, style='F')}\n"
                f"{self._humanize_timedelta(account_age)} atrás"
            ),
            inline=False,
        )

        embed.set_thumbnail(url=punished_user.display_avatar.url)
        embed.set_footer(text=f"id da infração: {infraction_id}")

        return embed

    def _format_punished_user(self, user: discord.abc.User) -> str:
        text = f"{user.mention}\n`{user}`"

        if isinstance(user, discord.Member) and user.display_name != user.name:
            text += f"\napelido: `{user.display_name}`"

        return text

    def _build_fallback_infraction_id(
        self,
        guild_id: int,
        user_id: int,
        punishment_type: str,
        occurred_at,
    ) -> str:
        prefix = {
            "ban": "BAN",
            "kick": "KICK",
            "timeout": "TIMEOUT",
        }.get(punishment_type, "PUNISH")
        return f"{prefix}-{guild_id}-{user_id}-{int(occurred_at.timestamp())}"

    # =========================
    # deduplicação
    # =========================

    def _was_audit_entry_processed(self, audit_entry_id: int) -> bool:
        return audit_entry_id in self._recent_audit_entry_id_set

    def _remember_audit_entry(self, audit_entry_id: int) -> None:
        if audit_entry_id in self._recent_audit_entry_id_set:
            return

        self._recent_audit_entry_ids.append(audit_entry_id)
        self._recent_audit_entry_id_set.add(audit_entry_id)

        while len(self._recent_audit_entry_ids) > self._recent_audit_entry_limit:
            oldest = self._recent_audit_entry_ids.pop(0)
            self._recent_audit_entry_id_set.discard(oldest)

    # =========================
    # utilidades
    # =========================

    def _humanize_timedelta(self, delta: timedelta) -> str:
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            total_seconds = 0

        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts: list[str] = []

        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds and not parts:
            # só mostra segundos quando não há unidade maior
            parts.append(f"{seconds}s")

        return " ".join(parts) if parts else "0s"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PunishmentLogs(bot))

