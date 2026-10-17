"""Sistema de Lembrete de Bump do Disboard — Persistente e à Prova de Reboot."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks
from core.paths import DATA_DIR

LOGGER = logging.getLogger("baphomet.bump")

# ── Constantes ──────────────────────────────────────────────────────────────
DISBOARD_BOT_ID = 302050872383242240
BUMP_COOLDOWN = timedelta(hours=2)

# Cargo padrão mencionado nos lembretes, caso nenhum cargo seja configurado.
DEFAULT_BUMP_ROLE_ID = 1381835764505120878

# Palavras-chave que aparecem APENAS em bumps bem-sucedidos do Disboard.
# O Disboard responde em vários idiomas, então cobrimos os mais comuns.
SUCCESS_KEYWORDS = (
    "bump done",        # EN
    "bump efetuado",    # PT
    "check it out",     # EN (variante)
    "bump efectuado",   # ES
)

# Cor do embed de sucesso do Disboard (verde-água / #24b7b7 em decimal).
# Se o Disboard estiver em cooldown, o embed é vermelho/laranja.
DISBOARD_SUCCESS_COLOR = 0x24B7B7

DB_PATH = DATA_DIR / "bump_reminder.sqlite3"


# ── Frases do Baphomet ──────────────────────────────────────────────────────
# O sistema sorteia um número de 1 a 40.
# O número sorteado corresponde diretamente ao índice da frase nesta lista.
BAPHOMET_BUMP_PHRASES = [
    "🕯️ {ping}, o bump despertou no altar; alguém ouve o chamado?",
    "👁️ {ping}, a janela do bump se abriu, e o abismo está sorrindo.",
    "🩸 {ping}, o bump voltou a sangrar no relógio do destino.",
    "🎭 {ping}, o palco externo está pronto; o bump aguarda sua estrela condenada.",
    "⚰️ {ping}, o bump saiu do túmulo e pede passagem.",
    "🧵 {ping}, o fio da divulgação está solto; o bump está disponível.",
    "📜 {ping}, o grimório atualizou: bump disponível para invocação.",
    "🔔 {ping}, o sino tocou; o bump voltou ao mundo dos vivos.",
    "🕸️ {ping}, novas almas podem cair na teia; o bump está pronto.",
    "🦷 {ping}, o servidor mostrou os dentes; o bump está disponível.",
    "🪞 {ping}, o reflexo do abismo aponta para um bump disponível.",
    "🗝️ {ping}, a fechadura da divulgação abriu; o bump aguarda.",
    "🫀 {ping}, o coração do servidor bate mais alto: bump disponível.",
    "🐐 {ping}, Baphomet farejou oportunidade; o bump está livre.",
    "🌒 {ping}, a lua virou o rosto; o bump pode ser invocado.",
    "🕯️ {ping}, a chama tremeu no altar; o bump voltou a existir.",
    "👁️ {ping}, o olho abriu no escuro; o bump está pronto.",
    "🩸 {ping}, o pacto da divulgação está pingando fresco; bump disponível.",
    "🎪 {ping}, a lona subiu outra vez; o bump espera plateia.",
    "🦴 {ping}, os ossos bateram no chão; sinal claro de bump liberado.",
    "📜 {ping}, uma nova linha surgiu no grimório: bump acessível.",
    "🧿 {ping}, o presságio é simples e inconveniente: bump disponível.",
    "🕰️ {ping}, o relógio do caos completou seu ciclo; bump pronto.",
    "🔮 {ping}, a bola de cristal só mostra uma coisa: bump liberado.",
    "⚱️ {ping}, a urna da divulgação se abriu; o bump pode sair.",
    "🦇 {ping}, morcegos começaram a circular; o bump está desperto.",
    "🪦 {ping}, a lápide rachou ao meio; o bump retornou.",
    "🫧 {ping}, até o vazio borbulhou; o bump está disponível.",
    "🗡️ {ping}, a lâmina apontou para fora; o bump aguarda execução.",
    "🐍 {ping}, a serpente sussurrou no rodapé: bump disponível.",
    "🧵 {ping}, um novo fio apareceu na tapeçaria; o bump pode ser puxado.",
    "🌑 {ping}, a noite abriu espaço; o bump está livre.",
    "🪬 {ping}, o talismã vibrou sem permissão; bump pronto para uso.",
    "🕷️ {ping}, a aranha terminou a teia; o bump aguarda presa.",
    "🧛 {ping}, algo faminto acordou no caixão; o bump está disponível.",
    "🕯️ {ping}, uma vela acendeu sozinha; o bump foi liberado.",
    "👑 {ping}, o trono rangeu no escuro; o bump pede presença.",
    "🧟 {ping}, até os mortos se moveram; o bump voltou.",
    "🌀 {ping}, o caos alinhou os dentes; bump disponível.",
    "🎭 {ping}, Baphomet abriu as cortinas; o bump está em cena.",
]


# ── Camada de Persistência ──────────────────────────────────────────────────
class BumpRepository:
    """Acesso direto ao SQLite — isolado da lógica de negócio."""

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
        await self._create_tables()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("BumpRepository não conectado.")
        return self._conn

    async def _create_tables(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bump_config (
                guild_id       INTEGER PRIMARY KEY,
                channel_id     INTEGER,
                role_id        INTEGER
            );

            CREATE TABLE IF NOT EXISTS bump_pending (
                guild_id       INTEGER PRIMARY KEY,
                channel_id     INTEGER NOT NULL,
                release_at     TEXT    NOT NULL
            );
            """
        )
        await self.conn.commit()

    # ── Config ──────────────────────────────────────────────────────────────
    async def upsert_config(
        self,
        guild_id: int,
        channel_id: int | None,
        role_id: int | None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO bump_config (guild_id, channel_id, role_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                role_id    = excluded.role_id
            """,
            (guild_id, channel_id, role_id),
        )
        await self.conn.commit()

    async def get_config(self, guild_id: int) -> dict | None:
        rows = await self.conn.execute_fetchall(
            "SELECT channel_id, role_id FROM bump_config WHERE guild_id = ?",
            (guild_id,),
        )

        if not rows:
            return None

        return {
            "channel_id": rows[0]["channel_id"],
            "role_id": rows[0]["role_id"],
        }

    # ── Pending bumps ───────────────────────────────────────────────────────
    async def schedule_bump(
        self,
        guild_id: int,
        channel_id: int,
        release_at: datetime,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO bump_pending (guild_id, channel_id, release_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                release_at = excluded.release_at
            """,
            (guild_id, channel_id, release_at.isoformat()),
        )
        await self.conn.commit()

    async def get_ready_bumps(self, now: datetime) -> list[dict]:
        """Retorna todos os bumps cujo release_at já passou."""
        rows = await self.conn.execute_fetchall(
            "SELECT guild_id, channel_id, release_at FROM bump_pending WHERE release_at <= ?",
            (now.isoformat(),),
        )

        return [
            {
                "guild_id": r["guild_id"],
                "channel_id": r["channel_id"],
                "release_at": r["release_at"],
            }
            for r in rows
        ]

    async def delete_pending(self, guild_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM bump_pending WHERE guild_id = ?",
            (guild_id,),
        )
        await self.conn.commit()


# ── O Cog ───────────────────────────────────────────────────────────────────
class BumpReminderCog(commands.Cog):
    """Monitora bumps do Disboard e envia lembretes persistentes."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.repo = BumpRepository(str(DB_PATH))

    async def cog_load(self) -> None:
        await self.repo.connect()
        self.check_pending_bumps.start()
        LOGGER.info("BumpReminderCog carregado e task iniciada.")

    async def cog_unload(self) -> None:
        self.check_pending_bumps.cancel()
        await self.repo.close()

    @staticmethod
    def escolher_frase_baphomet(ping: str) -> tuple[int, str]:
        """
        Sorteia um número de 1 a 40 e retorna a frase correspondente.

        O número sorteado corresponde exatamente à posição da frase na lista:
        número 1 = primeira frase
        número 40 = última frase
        """
        numero_sorteado = secrets.SystemRandom().randint(1, len(BAPHOMET_BUMP_PHRASES))
        frase = BAPHOMET_BUMP_PHRASES[numero_sorteado - 1].format(ping=ping)
        return numero_sorteado, frase

    # ── Interceptação do Disboard ───────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # ┌─────────────────────────────────────────────────────────────────┐
        # │  FILTRO 1: Só nos interessa mensagens do bot do Disboard.      │
        # └─────────────────────────────────────────────────────────────────┘
        if message.author.id != DISBOARD_BOT_ID:
            return

        if not message.embeds:
            return

        if message.guild is None:
            return

        embed = message.embeds[0]

        # ┌─────────────────────────────────────────────────────────────────┐
        # │  FILTRO 2 (CRÍTICO): Distinguir SUCESSO vs COOLDOWN.           │
        # │                                                                 │
        # │  O Disboard usa cores distintas nos embeds:                     │
        # │    • Sucesso  → verde-água (#24B7B7 / decimal 2406327)         │
        # │    • Cooldown → vermelho/laranja                               │
        # │                                                                 │
        # │  Além da cor, verificamos palavras-chave na descrição como      │
        # │  fallback, pois o Disboard pode mudar a paleta no futuro.      │
        # └─────────────────────────────────────────────────────────────────┘
        is_success = False

        # Verificação por cor do embed
        if embed.color and embed.color.value == DISBOARD_SUCCESS_COLOR:
            is_success = True

        # Verificação por palavras-chave (fallback multilíngue)
        description = (embed.description or "").lower()
        if any(kw in description for kw in SUCCESS_KEYWORDS):
            is_success = True

        if not is_success:
            # É uma resposta de cooldown ou erro — ignorar silenciosamente.
            return

        guild_id = message.guild.id

        # ── INTERCEPTOR DE ACHIEVEMENT: Precisão Cirúrgica ───────────────
        user_id = message.interaction.user.id if message.interaction else None
        if user_id:
            old_rows = await self.repo.conn.execute_fetchall(
                "SELECT release_at FROM bump_pending WHERE guild_id = ?",
                (guild_id,)
            )
            if old_rows:
                old_release_iso = old_rows[0]["release_at"]
                try:
                    old_release_at = datetime.fromisoformat(old_release_iso)
                    delta_time = message.created_at.timestamp() - old_release_at.timestamp()
                    if abs(delta_time) <= 1.0:
                        secret_cog = self.bot.get_cog("SecretAchievementsCog")
                        if secret_cog:
                            self.bot.loop.create_task(
                                secret_cog.unlock_achievement(user_id, guild_id, "surgical_bump")
                            )
                except ValueError:
                    pass
        # ──────────────────────────────────────────────────────────────────

        # ── Bump confirmado! Agendar o próximo lembrete. ────────────────
        release_at = datetime.now(timezone.utc) + BUMP_COOLDOWN

        # Decidir o canal de destino: configuração do admin ou canal atual.
        config = await self.repo.get_config(guild_id)
        target_channel_id = (
            config["channel_id"]
            if config and config["channel_id"]
            else message.channel.id
        )

        await self.repo.schedule_bump(guild_id, target_channel_id, release_at)

        LOGGER.info(
            f"[Guild {guild_id}] Bump registrado. "
            f"Lembrete agendado para {release_at.isoformat()}."
        )

    # ── Task Loop — Agendador Imparável ─────────────────────────────────
    @tasks.loop(minutes=1)
    async def check_pending_bumps(self) -> None:
        """Consulta o banco a cada minuto e dispara lembretes vencidos."""
        now = datetime.now(timezone.utc)
        ready = await self.repo.get_ready_bumps(now)

        for entry in ready:
            guild_id = entry["guild_id"]
            channel_id = entry["channel_id"]

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                # Canal inacessível — limpar registro para não ficar preso.
                await self.repo.delete_pending(guild_id)
                LOGGER.warning(
                    f"[Guild {guild_id}] Canal {channel_id} inacessível. Registro removido."
                )
                continue

            config = await self.repo.get_config(guild_id)

            role_id = (
                config["role_id"]
                if config and config["role_id"]
                else DEFAULT_BUMP_ROLE_ID
            )

            ping = f"<@&{role_id}>"

            numero_sorteado, mensagem = self.escolher_frase_baphomet(ping)

            try:
                await channel.send(mensagem)
                LOGGER.info(
                    f"[Guild {guild_id}] Lembrete de bump enviado "
                    f"com a frase #{numero_sorteado}."
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.error(f"[Guild {guild_id}] Falha ao enviar lembrete: {exc}")

            # ── Deletar IMEDIATAMENTE para não duplicar no próximo minuto.
            await self.repo.delete_pending(guild_id)

    @check_pending_bumps.before_loop
    async def before_check(self) -> None:
        await self.bot.wait_until_ready()

    # ── Método de Configuração (Admin via /server_config) ──────────────
    async def configurar_bump(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "⚠️ Este comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return

        await self.repo.upsert_config(
            guild_id=interaction.guild.id,
            channel_id=canal.id if canal else None,
            role_id=cargo.id if cargo else None,
        )

        parts = ["✅ Configuração do Bump Reminder salva!"]

        if canal:
            parts.append(f"📍 **Canal:** {canal.mention}")
        else:
            parts.append("📍 **Canal:** Mesmo canal onde o `/bump` for dado.")

        if cargo:
            parts.append(f"🔔 **Cargo pingado:** {cargo.mention}")
        else:
            parts.append(
                f"🔔 **Cargo pingado:** Padrão <@&{DEFAULT_BUMP_ROLE_ID}>."
            )

        await interaction.response.send_message("\n".join(parts), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BumpReminderCog(bot))

