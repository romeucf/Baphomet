from __future__ import annotations

import logging
import zoneinfo
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from main import MyBot

LOGGER = logging.getLogger("baphomet.achievements")


class SecretAchievementsCog(commands.Cog):
    """Cog responsável pelo rastreamento heurístico de Payloads e Interceptadores de Achievements Secretos."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot: MyBot = bot  # type: ignore

    async def unlock_achievement(self, user_id: int, guild_id: int, internal_code: str) -> None:
        """
        Rotina Transacional de Liberação de Conquistas.
        Executa validação atômica via SADD no Redis. 
        O(1) para verificar duplicação e evitar I/O desnecessário.
        """
        redis_manager = self.bot.redis_manager
        if not redis_manager or not redis_manager._is_connected:
            LOGGER.warning("Redis inativo. Falha ao computar conquista em L1 cache.")
            return

        key = f"baphomet:achievements:unlocked:{guild_id}:{user_id}"
        
        # Operação Atômica SADD: retorna 1 se foi inserido (novo), 0 se já existia.
        added = await redis_manager._pool.sadd(key, internal_code)

        if added == 1:
            LOGGER.info(f"[Achievements] Usuário {user_id} desbloqueou a conquista secreta '{internal_code}'. Gravando...")
            xp_repo = getattr(self.bot, "xp_repository", None)
            if xp_repo is None:
                LOGGER.warning("Repositório de XP indisponível ao gravar conquista.")
                await redis_manager._pool.srem(key, internal_code)
                return
            try:
                async with xp_repo._tx_lock:
                    conn = xp_repo.connection
                    async with conn.execute(
                        "SELECT id FROM achievements_def WHERE internal_code = ?",
                        (internal_code,)
                    ) as cursor:
                        row = await cursor.fetchone()

                    if row:
                        achievement_id = row["id"]
                        await conn.execute(
                            """
                            INSERT OR IGNORE INTO user_achievements (user_id, guild_id, achievement_id)
                            VALUES (?, ?, ?)
                            """,
                            (user_id, guild_id, achievement_id)
                        )
                        await conn.commit()
                    else:
                        LOGGER.error(f"Tentativa de desbloqueio falhou: Conquista com código '{internal_code}' não existe no dicionário.")
                        await redis_manager._pool.srem(key, internal_code)

            except Exception:
                LOGGER.error(f"Erro transacional ao injetar conquista {internal_code} no banco de dados para {user_id}", exc_info=True)
                await redis_manager._pool.srem(key, internal_code)

    @commands.Cog.listener("on_message")
    async def interceptor_on_message(self, message: discord.Message) -> None:
        """Audição global de conversas para mapeamento heurístico."""
        if message.author.bot or message.guild is None:
            return

        # =========================================================================
        # GATILHO: Hora Bruxa
        # =========================================================================
        # Extrai o momento puramente em UTC e converte com fuso-horário exato.
        br_time = message.created_at.astimezone(zoneinfo.ZoneInfo("America/Sao_Paulo"))
        
        if br_time.hour == 3 and br_time.minute == 33:
            # Invoca rotina transacional sem bloquear a thread
            self.bot.loop.create_task(
                self.unlock_achievement(message.author.id, message.guild.id, "witching_hour")
            )

        # =========================================================================
        # GATILHO: Inimigo do Silêncio (Sliding Window Rate Limit Algorithm)
        # =========================================================================
        redis_manager = self.bot.redis_manager
        if redis_manager and redis_manager._is_connected:
            now_ts = message.created_at.timestamp()
            # Escopo restrito do limitador (Membro + Servidor)
            ratelimit_key = f"baphomet:silence_breaker:{message.guild.id}:{message.author.id}"
            
            # Execução de comandos Pipeline estritos
            pipe = redis_manager._pool.pipeline()
            # 1. ZREMRANGEBYSCORE: Exclui registros fora da janela de 24h (86400s)
            pipe.zremrangebyscore(ratelimit_key, "-inf", now_ts - 86400)
            # 2. ZADD: Adiciona o snowflake da mensagem com o timestamp como score
            pipe.zadd(ratelimit_key, {str(message.id): now_ts})
            # 3. ZCARD: Calcula o total de mensagens enviadas nas últimas 24h
            pipe.zcard(ratelimit_key)
            # Extensão de TTL para limpeza passiva da janela de memória
            pipe.expire(ratelimit_key, 86400)
            
            results = await pipe.execute()
            count = results[2]  # Resultado do ZCARD

            if count >= 500:
                self.bot.loop.create_task(
                    self.unlock_achievement(message.author.id, message.guild.id, "silence_breaker")
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SecretAchievementsCog(bot))
