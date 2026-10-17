"""Protocolo de Limpeza de Ghost Joiners (Membros Fantasmas) — Anti-Brechas."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from core.paths import DATA_DIR

LOGGER = logging.getLogger("baphomet.ghost_cleanup")

DB_PATH = DATA_DIR / "ghost_cleanup.sqlite3"


# ── Camada de Persistência ──────────────────────────────────────────────────
class GhostCleanupRepository:
    """Armazena configurações de tempo e o rastreio da mensagem de boas-vindas."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS ghost_config (
                guild_id INTEGER PRIMARY KEY,
                minutes  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS welcome_tracking (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
        """)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("GhostCleanupRepository não conectado.")
        return self._conn

    # ── Configurações ──
    async def upsert_config(self, guild_id: int, minutes: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO ghost_config (guild_id, minutes)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET minutes = excluded.minutes
            """,
            (guild_id, minutes),
        )
        await self.conn.commit()

    async def get_config(self, guild_id: int) -> int | None:
        rows = await self.conn.execute_fetchall(
            "SELECT minutes FROM ghost_config WHERE guild_id = ?", (guild_id,)
        )
        if not rows:
            return None
        return rows[0]["minutes"]

    # ── Rastreio da Mensagem ──
    async def save_welcome_message(self, guild_id: int, user_id: int, message_id: int, channel_id: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO welcome_tracking (guild_id, user_id, message_id, channel_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                message_id = excluded.message_id,
                channel_id = excluded.channel_id
            """,
            (guild_id, user_id, message_id, channel_id),
        )
        await self.conn.commit()

    async def get_welcome_message(self, guild_id: int, user_id: int) -> dict | None:
        rows = await self.conn.execute_fetchall(
            "SELECT message_id, channel_id FROM welcome_tracking WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if not rows:
            return None
        return {"message_id": rows[0]["message_id"], "channel_id": rows[0]["channel_id"]}

    async def delete_tracking(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM welcome_tracking WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()


# ── O Cog ───────────────────────────────────────────────────────────────────
class GhostCleanupCog(commands.Cog):
    """Protocolo de limpeza total para membros que entram e saem rapidamente."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.repo = GhostCleanupRepository(str(DB_PATH))

    async def cog_load(self) -> None:
        await self.repo.connect()
        LOGGER.info("GhostCleanupCog carregado.")

    async def cog_unload(self) -> None:
        await self.repo.close()

    # ── Rastreio via Evento Customizado ─────────────────────────────────────
    # Este evento é disparado pelo cog de entrada_mensagem.py
    # `self.bot.dispatch("welcome_message_sent", member, msg)`
    @commands.Cog.listener()
    async def on_welcome_message_sent(self, member: discord.Member, message: discord.Message) -> None:
        """Salva a referência da mensagem de boas-vindas do membro."""
        await self.repo.save_welcome_message(member.guild.id, member.id, message.id, message.channel.id)

    # ── Gatilho de Limpeza ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild_id = member.guild.id
        
        # 1. Verifica se o servidor configurou a limpeza
        minutes_threshold = await self.repo.get_config(guild_id)
        if minutes_threshold is None:
            return

        # 2. Calcula o tempo de permanência
        if member.joined_at is None:
            return  # Sem data de entrada, não tem como calcular
        
        joined_at = member.joined_at.replace(tzinfo=timezone.utc) if member.joined_at.tzinfo is None else member.joined_at
        now = datetime.now(timezone.utc)
        duration_minutes = (now - joined_at).total_seconds() / 60.0

        # 3. Verifica se é um ghost joiner
        if duration_minutes <= minutes_threshold:
            LOGGER.info(f"[Ghost Cleanup] {member} saiu após {duration_minutes:.1f} minutos (Limite: {minutes_threshold}). Iniciando protocolo.")
            
            # Dispara a limpeza em background para não bloquear o event loop
            asyncio.create_task(self._execute_cleanup_protocol(member.guild, member.id))

    async def _execute_cleanup_protocol(self, guild: discord.Guild, user_id: int) -> None:
        """Executa a limpeza total das mensagens do ghost joiner."""
        
        # ── PARTE A: Exclusão da Mensagem de Boas-Vindas e Respostas ──
        tracking_data = await self.repo.get_welcome_message(guild.id, user_id)
        
        if tracking_data:
            channel_id = tracking_data["channel_id"]
            welcome_msg_id = tracking_data["message_id"]
            channel = guild.get_channel(channel_id)
            
            if isinstance(channel, discord.TextChannel):
                try:
                    welcome_msg = await channel.fetch_message(welcome_msg_id)
                    messages_to_delete = [welcome_msg]
                    
                    # Busca mensagens recentes no canal para encontrar respostas
                    # ┌─────────────────────────────────────────────────────────────────┐
                    # │ EXCLUSÃO DE RESPOSTAS (O DETALHE MAIS COMPLEXO)                 │
                    # │ Aqui iteramos as últimas 100 mensagens do canal.                │
                    # │ Verificamos se `msg.reference` não é None e se ela aponta       │
                    # │ EXATAMENTE para o ID da mensagem de boas-vindas do fantasma.    │
                    # └─────────────────────────────────────────────────────────────────┘
                    async for msg in channel.history(limit=100, after=welcome_msg.created_at):
                        if msg.reference is not None and msg.reference.message_id == welcome_msg_id:
                            messages_to_delete.append(msg)
                    
                    # Bulk delete é muito mais eficiente e seguro contra rate limits
                    if len(messages_to_delete) == 1:
                        await messages_to_delete[0].delete()
                    else:
                        await channel.delete_messages(messages_to_delete)
                        
                    LOGGER.info(f"Deletada a msg de boas-vindas e {len(messages_to_delete)-1} respostas para user {user_id}.")
                except discord.NotFound:
                    LOGGER.debug(f"Msg de boas vindas {welcome_msg_id} já havia sido deletada.")
                except discord.HTTPException as exc:
                    LOGGER.warning(f"Falha ao apagar msg de boas-vindas no canal {channel_id}: {exc}")

        # ── PARTE B: Exclusão das Mensagens do Usuário (Purge) ──
        for channel in guild.text_channels:
            # Verifica permissões básicas antes de tentar dar purge
            perms = channel.permissions_for(guild.me)
            if not perms.manage_messages or not perms.read_message_history:
                continue
                
            try:
                # Purge localiza e exclui apenas mensagens do autor alvo nas últimas 50
                deleted = await channel.purge(limit=50, check=lambda m: m.author.id == user_id)
                if deleted:
                    LOGGER.info(f"Purge deletou {len(deleted)} mensagens de {user_id} em #{channel.name}.")
            except discord.HTTPException as exc:
                LOGGER.warning(f"Erro no purge do canal {channel.id}: {exc}")
            
            # Anti Rate-Limit: Pequena pausa entre cada canal
            await asyncio.sleep(1.0)

        # ── PARTE C: Limpeza do Cache ──
        await self.repo.delete_tracking(guild.id, user_id)
        LOGGER.info(f"Protocolo de limpeza para user {user_id} finalizado.")

    # ── Comando de Configuração (Admin) ─────────────────────────────────────
    async def configurar_limpeza_saida(
        self, interaction: discord.Interaction, minutos: app_commands.Range[int, 0, 1440]
    ) -> None:
        if minutos == 0:
            await self.repo.conn.execute("DELETE FROM ghost_config WHERE guild_id = ?", (interaction.guild.id,))
            await self.repo.conn.commit()
            await interaction.response.send_message("⛔ **Protocolo Ghost Cleanup Desativado.**", ephemeral=True)
            return

        await self.repo.upsert_config(interaction.guild.id, minutos)
        await interaction.response.send_message(
            f"✅ **Protocolo Ativado!**\nSe um membro sair antes de completar **{minutos} minuto(s)** no servidor, apagarei todas as mensagens dele, as boas-vindas dele e qualquer resposta que as pessoas tenham dado para a mensagem de boas-vindas.",
            ephemeral=True,
        )

    async def status_limpeza_saida(self, interaction: discord.Interaction) -> None:
        minutes = await self.repo.get_config(interaction.guild.id)
        
        embed = discord.Embed(title="👻 Status — Limpeza de Ghost Joiners", color=discord.Color.dark_gray())
        
        if minutes is None:
            embed.description = "⚠️ Nenhuma configuração definida para este módulo."
            embed.color = discord.Color.red()
        else:
            embed.description = f"🟢 **Ativo**\nMembros que saírem antes de completar **{minutes} minuto(s)** terão seus rastros completamente apagados do servidor."
            embed.color = discord.Color.green()
            
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GhostCleanupCog(bot))
