import json
import logging
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
from core.paths import DATA_DIR

# =========================================================
# CONSTANTES
# =========================================================

SPAM_MESSAGE_LIMIT = 7
SPAM_WINDOW_SECONDS = 3
MAINTENANCE_INTERVAL_MINUTES = 5
SETTINGS_FILE = DATA_DIR / "guild_settings.json"

DEFAULT_GUILD_SETTINGS: Dict[str, Any] = {
    "geral": None,
    "antispam_enabled": True,
    "permanencia_minutos": 10,
    "invite_max_age": 0,    # 0 = nunca expira (em segundos)
    "invite_max_uses": 0,   # 0 = usos ilimitados
}

log = logging.getLogger(__name__)

# =========================================================
# COG E PERSISTÊNCIA ASSÍNCRONA
# =========================================================

class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        
        # Lock de segurança para escritas no arquivo de configuração
        self._config_lock = asyncio.Lock()

        # Anti-spam: chave = (guild_id, channel_id, user_id)
        self.spam_tracker: defaultdict[Tuple[int, int, int], deque[datetime]] = defaultdict(deque)

        # Permânencia: chave = guild_id -> dict de user_id: datetime
        self.join_times: defaultdict[int, Dict[int, datetime]] = defaultdict(dict)

        # Configurações
        self.guild_settings: defaultdict[int, Dict[str, Any]] = defaultdict(
            lambda: dict(DEFAULT_GUILD_SETTINGS)
        )

    async def cog_load(self) -> None:
        """Chamado pelo discord.py quando o cog for carregado."""
        await self._load_settings_async()
        self._maintenance_task.start()

    async def cog_unload(self) -> None:
        """Chamado pelo discord.py se o cog for descarregado."""
        self._maintenance_task.cancel()

    # =========================================================
    # HELPERS DE ARQUIVO E DADOS
    # =========================================================

    async def _load_settings_async(self) -> None:
        """Carrega o JSON assincronamente sem bloquear o Event Loop."""
        if not SETTINGS_FILE.exists():
            return
            
        def _read() -> Dict[str, Any]:
            try:
                with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                log.warning("Falha ao carregar %s — usando config em branco.", SETTINGS_FILE)
                return {}

        raw = await asyncio.to_thread(_read)
        
        for guild_id_str, cfg in raw.items():
            merged = dict(DEFAULT_GUILD_SETTINGS)
            merged.update(cfg)
            self.guild_settings[int(guild_id_str)] = merged

    async def _persist_async(self) -> None:
        """Salva as configs em arquivo usando uma thread separada e trava Mutex."""
        data_to_save = {str(gid): cfg for gid, cfg in self.guild_settings.items()}

        def _write() -> None:
            try:
                with SETTINGS_FILE.open("w", encoding="utf-8") as f:
                    json.dump(data_to_save, f, indent=2, ensure_ascii=False)
            except OSError:
                log.exception("Falha de I/O ao salvar %s", SETTINGS_FILE)

        async with self._config_lock:
            await asyncio.to_thread(_write)

    def _settings(self, guild_id: int) -> Dict[str, Any]:
        return self.guild_settings[guild_id]

    # =========================================================
    # HELPERS INTERNOS DO DISCORD
    # =========================================================

    def _get_me(self, guild: discord.Guild) -> discord.Member | None:
        return guild.me if guild.me else guild.get_member(self.bot.user.id) if self.bot.user else None

    async def _send_to_geral(self, guild: discord.Guild, content: str) -> None:
        channel_id = self._settings(guild.id).get("geral")
        if not channel_id:
            return
            
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
            
        me = self._get_me(guild)
        if me and not channel.permissions_for(me).send_messages:
            return
            
        try:
            await channel.send(content)
        except discord.HTTPException:
            log.exception("Falha ao enviar aviso no canal geral (Guild ID: %d)", guild.id)

    # =========================================================
    # TASK DE MANUTENÇÃO E LIMPEZA DA MEMÓRIA
    # =========================================================

    @tasks.loop(minutes=MAINTENANCE_INTERVAL_MINUTES)
    async def _maintenance_task(self) -> None:
        """Limpa entradas velhas para evitar vazamento de memória com o tempo."""
        now = datetime.now(timezone.utc)
        
        # 1. Limpeza do Anti-Spam
        spam_cutoff = now - timedelta(seconds=SPAM_WINDOW_SECONDS)
        stale_spam = [k for k, ts in self.spam_tracker.items() if not ts or ts[-1] < spam_cutoff]
        for k in stale_spam:
            self.spam_tracker.pop(k, None)
            
        # 2. Limpeza do Monitoramento de Permanência (Memory Leak Fix)
        # Se um membro entrou e não saiu dentro do tempo de análise, limpamos o cache dele.
        cleared_joins = 0
        for guild_id, join_dict in list(self.join_times.items()):
            max_minutes = self._settings(guild_id).get("permanencia_minutos", 10)
            join_cutoff = now - timedelta(minutes=max_minutes + 2) # Margem de erro de 2 min
            
            stale_users = [uid for uid, timestamp in join_dict.items() if timestamp < join_cutoff]
            for uid in stale_users:
                del join_dict[uid]
                cleared_joins += 1

        if stale_spam or cleared_joins:
            log.debug("Manutenção: %d chaves de spam removidas | %d rastreios de entrada liberados", len(stale_spam), cleared_joins)

    # =========================================================
    # EVENTOS PRINCIPAIS
    # =========================================================

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        self.join_times[member.guild.id][member.id] = datetime.now(timezone.utc)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        # Puxa o horário e já remove do dict
        joined_at = self.join_times[member.guild.id].pop(member.id, None) or member.joined_at
        if not joined_at:
            return

        time_in_guild = datetime.now(timezone.utc) - joined_at
        minutos_configurados = self._settings(member.guild.id)["permanencia_minutos"]

        if time_in_guild < timedelta(minutes=minutos_configurados):
            minutos_reais = round(time_in_guild.total_seconds() / 60, 1)
            log.info(
                "Saída antecipada detectada: ID %d ficou apenas %.1f min na guild %d.",
                member.id, minutos_reais, member.guild.id
            )
            await self._send_to_geral(
                member.guild,
                f"⚠️ **{discord.utils.escape_markdown(str(member))}** "
                f"entrou e saiu em apenas **{minutos_reais} min** "
                f"(Limite de análise local: {minutos_configurados} min)."
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Segurança base: ignora DMs, DMs de bots e mensagens de si mesmo
        if not message.guild or message.author.bot:
            return

        if self._settings(message.guild.id)["antispam_enabled"]:
            await self.check_spam_and_kick(message)
            
        # O process_commands AQUI foi removido intencionalmente (Anti-Bug de duplicação)

    # =========================================================
    # LÓGICA DE DEFESA
    # =========================================================

    async def check_spam_and_kick(self, message: discord.Message) -> None:
        now = datetime.now(timezone.utc)
        key = (message.guild.id, message.channel.id, message.author.id)
        timestamps = self.spam_tracker[key]

        timestamps.append(now)

        # Remove contagens fora da janela de verificação de 3 segundos
        while timestamps and (now - timestamps[0]).total_seconds() > SPAM_WINDOW_SECONDS:
            timestamps.popleft()

        if len(timestamps) <= SPAM_MESSAGE_LIMIT:
            return

        # Limite violado
        self.spam_tracker.pop(key, None)
        
        # Fallback de type hinting. Autor de msg em Guild é sempre Member.
        if not isinstance(message.author, discord.Member):
            return

        log.warning("Spam travado: Autor %d | Canal %d | Guild %d", message.author.id, message.channel.id, message.guild.id)

        try:
            await message.author.kick(
                reason=f"Sistema Anti-Spam (Violou limite de {SPAM_MESSAGE_LIMIT} msgs em {SPAM_WINDOW_SECONDS}s)"
            )
        except discord.Forbidden:
            await message.channel.send("⚠️ Falha ao expulsar spamer: Permissões insuficientes.")
            return
        except discord.HTTPException:
            log.exception("Erro HTTP ao kickar ID %d", message.author.id)
            return

        await message.channel.send(f"🛡️ {message.author.mention} foi expulso(a) automaticamente por flood/spam.")

        # Limpeza visual das mensagens
        try:
            spam_start = now - timedelta(seconds=SPAM_WINDOW_SECONDS + 1)
            await message.channel.purge(
                limit=SPAM_MESSAGE_LIMIT + 5,
                check=lambda m: m.author.id == message.author.id,
                after=spam_start,
                bulk=True,
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Não foi possível apagar as msgs do spammer ID %d", message.author.id)

    # =========================================================
    # SLASH COMMANDS (Configurações)
    # =========================================================

    async def set_geral(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        if not interaction.guild_id: return
        
        self._settings(interaction.guild_id)["geral"] = canal.id
        await self._persist_async()
        
        await interaction.response.send_message(f"✅ Canal de alertas de segurança configurado: {canal.mention}", ephemeral=True)

    async def set_permanencia(self, interaction: discord.Interaction, minutos: app_commands.Range[int, 1, 10080]) -> None:
        if not interaction.guild_id: return
        
        self._settings(interaction.guild_id)["permanencia_minutos"] = minutos
        await self._persist_async()
        
        await interaction.response.send_message(f"✅ Nova régua de permanência: **{minutos} minuto(s)**.", ephemeral=True)

    async def set_convite(self, interaction: discord.Interaction, max_age: app_commands.Range[int, 0, 720], max_uses: app_commands.Range[int, 0, 1000]) -> None:
        if not interaction.guild_id: return
        
        cfg = self._settings(interaction.guild_id)
        cfg["invite_max_age"] = max_age * 3600  # API discord demanda segundos
        cfg["invite_max_uses"] = max_uses
        await self._persist_async()

        age_str = f"{max_age}h" if max_age > 0 else "permanente"
        uses_str = str(max_uses) if max_uses > 0 else "ilimitado"
        await interaction.response.send_message(f"✅ Gerador de convites ajustado:\n• Expira em: **{age_str}**\n• Usos: **{uses_str}**", ephemeral=True)

    # =========================================================
    # MÉTODOS (Anti-Spam)
    # =========================================================

    async def antispam_toggle(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id: return
        
        cfg = self._settings(interaction.guild_id)
        cfg["antispam_enabled"] = not cfg["antispam_enabled"]
        await self._persist_async()

        estado = "✅ ATIVADA" if cfg["antispam_enabled"] else "🔴 SUSPENSA"
        await interaction.response.send_message(f"Vigilância de chat **{estado}**.", ephemeral=True)

    async def antispam_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild_id: return
        
        cfg = self._settings(interaction.guild_id)
        estado = "✅ Blindado (Ativo)" if cfg["antispam_enabled"] else "🔴 Vulnerável (Inativo)"
        await interaction.response.send_message(
            f"**Status da Segurança do Chat**\n{estado}\n\n-# Limite hardcoded: O bot bane quem passar de {SPAM_MESSAGE_LIMIT} envios a cada {SPAM_WINDOW_SECONDS}s.", 
            ephemeral=True
        )


# =========================================================
# FUNÇÃO DE MONTAGEM NO DISCORD
# =========================================================

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))

