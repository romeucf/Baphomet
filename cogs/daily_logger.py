import asyncio
import datetime
import io
import logging
from zoneinfo import ZoneInfo

import aiosqlite
import discord
from discord.ext import commands, tasks
from core.logger import log_exception
from core.paths import DATA_DIR


# Constantes do módulo
DB_PATH = DATA_DIR / "daily_logs.sqlite3"

LOGGER = logging.getLogger("baphomet.daily_logger")

class DailyLoggerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._message_buffer: list[tuple[int, str, str, str, str]] = []

    async def cog_load(self) -> None:
        """Inicia a conexão com o BD e inicializa o loop."""
        self._db = await aiosqlite.connect(str(DB_PATH))
        self._db.row_factory = aiosqlite.Row
        
        # Otimizações de concorrência e I/O para SQLite
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        
        # Setup das Tabelas
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS logger_config (
                guild_id INTEGER PRIMARY KEY,
                target_channel_id INTEGER NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS logger_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_log_messages_guild ON logger_messages(guild_id)")
        await self._db.commit()
        
        self.flush_buffer_task.start()
        self.daily_log_task.start()
        LOGGER.info("Daily Global Logging inicializado.")

    async def cog_unload(self) -> None:
        """Encerra a conexão segura e cancela a task."""
        self.flush_buffer_task.cancel()
        self.daily_log_task.cancel()
        await self._flush_buffer()
        if self._db:
            await self._db.close()

    async def _flush_buffer(self) -> None:
        """Descarrega todas as mensagens acumuladas em memória em uma única transação SQLite."""
        if not self._db or not self._message_buffer:
            return
        async with self._lock:
            if not self._message_buffer:
                return
            batch = self._message_buffer[:]
            self._message_buffer.clear()
            try:
                await self._db.executemany(
                    "INSERT INTO logger_messages (guild_id, channel_name, author_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
                await self._db.commit()
            except Exception as e:
                log_exception(e)
                LOGGER.error(f"Erro ao gravar lote de mensagens no DB de logs: {e}")

    @tasks.loop(seconds=30)
    async def flush_buffer_task(self) -> None:
        await self._flush_buffer()

    @flush_buffer_task.before_loop
    async def before_flush_buffer_task(self) -> None:
        await self.bot.wait_until_ready()

    # =========================================================
    # COMANDO DE CONFIGURAÇÃO (Admin-Only)
    # =========================================================
    async def configurar_logs(
        self, 
        interaction: discord.Interaction, 
        canal_destino_id: str, 
        servidor_origem_id: str | None = None
    ) -> None:
        # Padrão: servidor de onde o comando foi digitado
        guild_id_str = servidor_origem_id or str(interaction.guild_id)
        
        try:
            target_guild_id = int(guild_id_str)
            target_channel_id = int(canal_destino_id)
        except ValueError:
            return await interaction.response.send_message("❌ Os IDs fornecidos devem ser apenas números.", ephemeral=True)

        if not self._db:
            return await interaction.response.send_message("❌ Banco de dados não está pronto. Tente novamente em alguns segundos.", ephemeral=True)

        # Validação Global
        canal = self.bot.get_channel(target_channel_id)
        if canal is None:
            return await interaction.response.send_message("❌ Canal de destino não encontrado. Verifique se o bot está no servidor de destino.", ephemeral=True)

        # Salva Configuração
        await self._db.execute(
            """
            INSERT INTO logger_config (guild_id, target_channel_id) 
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET target_channel_id=excluded.target_channel_id
            """,
            (target_guild_id, target_channel_id)
        )
        await self._db.commit()
        
        await interaction.response.send_message(
            f"✅ Configuração salva com sucesso!\n"
            f"**Servidor Origem ID:** `{target_guild_id}`\n"
            f"**Canal Destino:** <#{target_channel_id}>",
            ephemeral=True
        )

    async def status_logs(self, interaction: discord.Interaction) -> None:
        if not self._db:
            return await interaction.response.send_message("❌ Banco de dados não está pronto.", ephemeral=True)
            
        row = await self._db.execute_fetchall(
            "SELECT target_channel_id FROM logger_config WHERE guild_id = ?", 
            (interaction.guild.id,)
        )
        
        embed = discord.Embed(title="📊 Status — Daily Logger", color=discord.Color.blurple())
        
        if not row:
            embed.description = "⚠️ Nenhuma configuração definida para este módulo."
            embed.color = discord.Color.red()
        else:
            target_id = row[0]["target_channel_id"]
            embed.description = f"🟢 **Ativo**\nOs logs diários deste servidor estão sendo enviados para: <#{target_id}>"
            embed.color = discord.Color.green()
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # =========================================================
    # MOTOR DE CAPTURA (Assíncrono e Otimizado com Batching)
    # =========================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignora mensagens DMs, de si mesmo e de outros bots ou webhooks
        if not message.guild or message.author.bot or message.webhook_id:
            return

        if not self._db:
            return

        guild_id = message.guild.id
        channel_name = message.channel.name if hasattr(message.channel, "name") else "unknown"
        author_name = f"{message.author.name} (`{message.author.id}`)"
        content = message.content or "[Mídia / Vazio]"
        
        # Timestamp de Brasília isolado para o momento da captura
        now_brt = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M:%S")

        self._message_buffer.append((guild_id, channel_name, author_name, content, now_brt))

    # =========================================================
    # O AGENDADOR DE TAREFAS (00:00 BRT)
    # =========================================================
    @tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=ZoneInfo("America/Sao_Paulo")))
    async def daily_log_task(self) -> None:
        """Dispara pontualmente todo dia à meia-noite."""
        LOGGER.info("Iniciando rotina de Daily Global Logging...")
        if not self._db:
            return

        await self._flush_buffer()

        # Recupera todas as configurações
        configs = await self._db.execute_fetchall("SELECT guild_id, target_channel_id FROM logger_config")
        
        yesterday_date = (datetime.datetime.now(ZoneInfo("America/Sao_Paulo")) - datetime.timedelta(days=1)).strftime("%d_%m_%Y")
        
        for cfg in configs:
            guild_id = cfg["guild_id"]
            target_channel_id = cfg["target_channel_id"]
            
            # Pega mensagens desse servidor usando Lock para evitar concorrência com deleção
            async with self._lock:
                msgs = await self._db.execute_fetchall(
                    "SELECT id, channel_name, author_name, content, created_at FROM logger_messages WHERE guild_id = ? ORDER BY id ASC", 
                    (guild_id,)
                )
            
            if not msgs:
                continue

            target_channel = self.bot.get_channel(target_channel_id)
            if not target_channel or not isinstance(target_channel, discord.TextChannel):
                LOGGER.warning(f"O canal alvo ({target_channel_id}) para a guild {guild_id} não existe mais ou é inacessível.")
                continue

            # =========================================================
            # GERAÇÃO DO ARQUIVO .TXT EM MEMÓRIA (IO Stream)
            # =========================================================
            log_buffer = io.StringIO()
            log_buffer.write(f"=== BAPHOMET DAILY GLOBAL LOGS - {yesterday_date.replace('_', '/')} ===\n")
            log_buffer.write(f"=== Servidor ID: {guild_id} | Total de Mensagens: {len(msgs)} ===\n\n")

            for row in msgs:
                time_only = row["created_at"].split(" ")[1] if " " in row["created_at"] else row["created_at"]
                linha = f"[{time_only}] #{row['channel_name']} | {row['author_name']}: {row['content']}\n"
                log_buffer.write(linha)

            # Move o ponteiro do StringIO de volta para o começo e transforma em Bytes
            log_buffer.seek(0)
            file_bytes = io.BytesIO(log_buffer.read().encode('utf-8'))
            file_name = f"log_data_{yesterday_date}.txt"
            
            discord_file = discord.File(fp=file_bytes, filename=file_name)

            # Embed bonito para acompanhar
            embed = discord.Embed(
                title="📦 Fechamento de Logs",
                description=f"Relatório de Logs diário concluído com sucesso.\n\n**Total de Mensagens do Dia:** `{len(msgs):,}`",
                color=discord.Color.from_rgb(120, 60, 240),
                timestamp=datetime.datetime.now(ZoneInfo("America/Sao_Paulo"))
            )
            embed.set_footer(text=f"Servidor Origem: {guild_id}")

            try:
                # Envio do arquivo cross-server
                await target_channel.send(embed=embed, file=discord_file)
                
                # =========================================================
                # LIMPEZA DE DADOS (FLUSH)
                # =========================================================
                # Somente limpa caso o envio tenha dado certo.
                # Extrai os IDs das mensagens que acabaram de ser enviadas.
                msg_ids = [m["id"] for m in msgs]
                
                # Se houver muitas mensagens, dividimos o delete em chunks
                chunk_size = 900
                async with self._lock:
                    for i in range(0, len(msg_ids), chunk_size):
                        chunk = msg_ids[i:i + chunk_size]
                        placeholders = ",".join("?" * len(chunk))
                        await self._db.execute(f"DELETE FROM logger_messages WHERE id IN ({placeholders})", chunk)
                    await self._db.commit()
                    
                LOGGER.info(f"Logs da guild {guild_id} enviados e banco de dados limpo.")

            except discord.Forbidden:
                LOGGER.error(f"Erro: Sem permissão para enviar o arquivo de log no canal {target_channel_id}.")
            except discord.HTTPException as e:
                LOGGER.error(f"Erro HTTP ao enviar o arquivo de log: {e}")
            except Exception as e:
                log_exception(e)
                LOGGER.error(f"Erro crítico e inesperado ao processar os logs da guild {guild_id}: {e}")

    @daily_log_task.before_loop
    async def before_daily_log_task(self):
        """Espera o bot logar completamente no Discord antes de começar a loop."""
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DailyLoggerCog(bot))
