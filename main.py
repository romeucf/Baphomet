import asyncio
import logging
import os
import platform
import sys
import time
import traceback
import uuid

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.db_transaction import BaphometTransactionManager
from core.redis_state import AbyssalRedisManager, RedisConnectionError
from core.logger import log_exception

# ==============================================================================
# 1. CORES E ESTILOS PARA TERMINAL (ANSI)
# ==============================================================================
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"

# ==============================================================================
# 2. FUNÇÕES AUXILIARES DE LOGGING (BEAUTIFUL LOGS)
# ==============================================================================
def print_separator() -> None:
    print(f"{Colors.BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.RESET}")

def log_info(msg: str) -> None:
    print(f"🔹 {Colors.CYAN}[INFO]{Colors.RESET} {msg}")

def log_success(msg: str) -> None:
    print(f"✅ {Colors.GREEN}[OK]{Colors.RESET} {msg}")

def log_warn(msg: str) -> None:
    print(f"⚠️ {Colors.YELLOW}[WARN]{Colors.RESET} {msg}")

def log_error(msg: str) -> None:
    print(f"❌ {Colors.RED}[ERROR]{Colors.RESET} {msg}")

def log_cog(msg: str, success: bool = True) -> None:
    tag = f"✅ {Colors.GREEN}[COG]{Colors.RESET}" if success else f"❌ {Colors.RED}[COG]{Colors.RESET}"
    print(f"{tag} {msg}")

def log_sync(msg: str) -> None:
    print(f"⚡ {Colors.BLUE}[SYNC]{Colors.RESET} {msg}")

def log_discord(msg: str) -> None:
    print(f"🟣 {Colors.MAGENTA}[DISCORD]{Colors.RESET} {msg}")

# ==============================================================================
# 3. VALIDAÇÃO DE AMBIENTE (.ENV E TOKEN)
# ==============================================================================
def get_token() -> str:
    log_info("Carregando variáveis de ambiente...")
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")

    if not token:
        print_separator()
        log_error("Nenhum token encontrado! Verifique se o arquivo .env existe e possui a chave DISCORD_TOKEN.")
        sys.exit(1)

    masked_token = f"{token[:4]}****************{token[-4:]}" if len(token) > 8 else "********"
    log_success(f"Token detectado com segurança: {masked_token}")
    return token

# ==============================================================================
# 4. CLASSES DO BOT (CORE)
# ==============================================================================
class MyBot(commands.Bot):
    def __init__(self) -> None:
        self.start_time = time.time()

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents
        )
        self.tx_manager = BaphometTransactionManager(db_path="data/baphomet_transactions.sqlite3", pool_size=5)
        self.redis_manager = AbyssalRedisManager()

    async def load_all_extensions(self) -> tuple[int, int]:
        """Varre o diretório /cogs para buscar extensões."""
        log_info("Iniciando o carregamento de extensões (Cogs)...")
        loaded, failed = 0, 0

        if not os.path.exists("./cogs"):
            return 0, 0

        # Regra 1: Arquivos .py soltos em cogs/ (ex: cogs/aotd.py)
        # Regra 2: Arquivos chamados commands.py dentro de subdiretórios imediatos (ex: cogs/iceberg/commands.py)
        extensions_to_load = []
        
        for item in os.listdir("./cogs"):
            item_path = os.path.join("./cogs", item)
            
            if os.path.isfile(item_path):
                if item.endswith(".py") and not item.startswith("__"):
                    extensions_to_load.append(f"cogs.{item[:-3]}")
                    
            elif os.path.isdir(item_path) and not item.startswith("__"):
                commands_file = os.path.join(item_path, "commands.py")
                if os.path.isfile(commands_file):
                    extensions_to_load.append(f"cogs.{item}.commands")

        for extension in extensions_to_load:
            try:
                await self.load_extension(extension)
                log_cog(f"{extension} carregado com sucesso.")
                loaded += 1
            except commands.NoEntryPointError:
                pass
            except Exception as exc:
                log_cog(f"{extension} falhou: {type(exc).__name__}: {exc}", success=False)
                failed += 1

        return loaded, failed

    async def setup_hook(self) -> None:
        """Chamado pelo discord.py antes de logar o bot."""
        # Inicializa o pool mestre do Redis e injeta no Transaction Manager
        await self.redis_manager.connect()
        self.tx_manager.inject_redis(self.redis_manager)

        # Inicializa o pool de conexões do tx_manager ANTES das cogs
        await self.tx_manager.initialize()

        loaded, failed = await self.load_all_extensions()
        
        await self.load_achievements_cache()

        print_separator()
        log_info(f"Resumo de Cogs: {loaded} carregados com sucesso | {failed} falharam.")
        print_separator()

        log_sync("Sincronizando slash commands globalmente...")
        try:
            synced = await self.tree.sync()
            log_sync(f"{len(synced)} comandos sincronizados com sucesso!")
        except Exception as exc:
            log_error(f"Falha ao sincronizar comandos: {exc}")

    async def load_achievements_cache(self) -> None:
        """Sincroniza o subsistema de Secret Achievements do SQLite (XP DB) para o Redis L1."""
        log_info("Iniciando cache estrito de Secret Achievements...")
        if not self.redis_manager or not self.redis_manager._is_connected:
            log_error("Redis L1 indisponível. Bypass do cache de Achievements ativado para prevenir falhas sistêmicas.")
            return

        try:
            async with self.tx_manager.acquire() as conn:
                try:
                    async with conn.execute(
                        """
                        SELECT u.guild_id, u.user_id, a.internal_code 
                        FROM xp_db.user_achievements u
                        INNER JOIN xp_db.achievements_def a ON u.achievement_id = a.id
                        """
                    ) as cursor:
                        rows = await cursor.fetchall()
                except Exception as db_err:
                    # Captura falha se a tabela não existir (ex: migração falhou)
                    logging.getLogger("Baphomet.Achievements").warning(
                        "Tabela de conquistas não encontrada ou falha no DDL. Bypass de cache ativado. Erro: %s", db_err
                    )
                    log_warn(f"Cache ignorado por ausência estrutural no DB: {db_err}")
                    return

            pipe = self.redis_manager._pool.pipeline()
            cache_count = 0
            for row in rows:
                guild_id = row["guild_id"]
                user_id = row["user_id"]
                internal_code = row["internal_code"]
                key = f"baphomet:achievements:unlocked:{guild_id}:{user_id}"
                pipe.sadd(key, internal_code)
                cache_count += 1
            
            if cache_count > 0:
                await pipe.execute()
                logging.getLogger("Baphomet.Achievements").info(f"Foram cacheados in-memory com sucesso {cache_count} Secret Achievements na camada L1 (Redis).")
                log_success(f"Cache populado: {cache_count} Secret Achievements carregados (O(1) lookups).")
            else:
                logging.getLogger("Baphomet.Achievements").info("Nenhum Secret Achievement encontrado no database relacional para cacheamento.")
                log_info("Tabela de Secret Achievements vazia. Cache não modificado.")

        except RedisConnectionError as e:
            logging.getLogger("Baphomet.Achievements").error("Partição de rede detectada com o container Redis durante a transição do cache de Achievements.", exc_info=True)
            log_exception(e, context="load_achievements_cache Redis Connection Error")
            log_error("Falha de rede com o Redis L1 ao carregar conquistas.")
        except Exception as e:
            logging.getLogger("Baphomet.Achievements").warning("Falha inesperada no boot do cache de Achievements: %s", e)
            log_warn(f"Falha amigável de boot L1 cache. O bot continuará online. Erro: {e}")


    async def close(self) -> None:
        """Desligamento gracioso do Bot."""
        log_warn("Iniciando processo de desligamento seguro...")

        runtime = getattr(self, "xp_runtime", None)
        if runtime is not None:
            try:
                log_info("Fechando conexão segura do repositório de XP...")
                await runtime.repository.close()
                log_success("Repositório XP encerrado com sucesso.")
            except Exception as exc:
                log_error(f"Erro ao fechar repositório XP: {exc}")

        vinculos_runtime = getattr(self, "vinculos_runtime", None)
        if vinculos_runtime is not None:
            try:
                log_info("Fechando conexão segura do repositório de vínculos...")
                await vinculos_runtime.repository.close()
                log_success("Repositório de vínculos encerrado com sucesso.")
            except Exception as exc:
                log_error(f"Erro ao fechar repositório de vínculos: {exc}")

        motd_db = getattr(self, "motd_db_manager", None)
        if motd_db is not None:
            try:
                log_info("Executando checkpoint do banco de dados MOTD...")
                await motd_db.close()
                log_success("Banco MOTD encerrado com sucesso.")
            except Exception as exc:
                log_error(f"Erro ao fechar banco MOTD: {exc}")

        try:
            log_info("Encerrando Transaction Manager (tx_manager)...")
            await self.tx_manager.close()
            log_success("Transaction Manager encerrado com sucesso.")
        except Exception as exc:
            log_error(f"Erro ao fechar Transaction Manager: {exc}")

        try:
            log_info("Encerrando Redis Manager (redis_manager)...")
            await self.redis_manager.close()
            log_success("Redis Manager encerrado com sucesso.")
        except Exception as exc:
            log_error(f"Erro ao fechar Redis Manager: {exc}")

        log_discord("Bot desconectado da API.")
        await super().close()

bot = MyBot()

# ==============================================================================
# 5. EVENTOS DO DISCORD
# ==============================================================================
@bot.event
async def on_error(event_method: str, *args, **kwargs) -> None:
    """
    Handler global para erros não capturados em Event Listeners (on_message, etc).
    """
    exc_type, exc_value, exc_traceback = sys.exc_info()
    if exc_value:
        log_exception(exc_value, context=f"Event Listener Error: {event_method}", args=args, kwargs=kwargs)
    else:
        log_error(f"Erro em evento {event_method}, mas nenhuma exceção fornecida.")

@bot.event
async def on_ready() -> None:
    boot_time = round(time.time() - bot.start_time, 2)
    users_count = sum(guild.member_count for guild in bot.guilds if guild.member_count)

    print_separator()
    print(f"{Colors.GREEN}{Colors.BOLD}🟢 BOT ONLINE E OPERACIONAL{Colors.RESET}")
    print(f"🤖 Usuário:           {bot.user} (ID: {bot.user.id})")
    print(f"🏠 Servidores:        {len(bot.guilds)}")
    print(f"👥 Usuários Visíveis: ~{users_count}")
    print(f"⏱️ Tempo de Boot:     {boot_time}s")
    print_separator()
    log_info("Aguardando interações dos usuários...")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    """Trata erros de prefix commands sem poluir o console."""
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.NotOwner):
        log_warn(f"O usuário {ctx.author} tentou usar comando restrito (!{ctx.command}).")
        return

    original_error = getattr(error, 'original', error)
    log_exception(original_error, context="Prefix Command Error", ctx=ctx)
    try:
        await ctx.send(f"❌ Ocorreu um erro interno: `{type(original_error).__name__}: {original_error}`")
    except discord.HTTPException:
        pass


@bot.tree.error
async def on_tree_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """
    Handler global para erros de slash commands e grupos de comandos (app_commands.Group).
    Cobre casos que os handlers .error das Cogs não alcançam, como o grupo /antispam.
    """
    # Erros já tratados pelas Cogs chegam aqui com AppCommandError genérico —
    # evitamos duplicar respostas checando se a interação já foi respondida.
    msg: str

    if isinstance(error, app_commands.MissingPermissions):
        msg = "Você precisa ser administrador(a) para usar esse comando."
    elif isinstance(error, app_commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        msg = f"Estou sem as permissões necessárias: {missing}."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Aguarde {error.retry_after:.1f}s antes de usar esse comando novamente."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "Esse comando só pode ser usado dentro de um servidor."
    else:
        # Desempacota o erro original se existir na árvore de comandos do Discord
        original_error = getattr(error, 'original', error)
        error_id = uuid.uuid4().hex[:12]
        msg = (
            "Falha Cataclísmica: O sistema não pôde computar sua requisição. "
            f"ID do erro: `{error_id}`."
        )
        log_exception(
            original_error,
            context=(
                "Global Slash Command Error | "
                f"error_id={error_id!r} | "
                f"wrapper_type={type(error).__module__}.{type(error).__name__}"
            ),
            interaction=interaction,
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException as response_error:
        log_exception(
            response_error,
            context=(
                "Global Slash Command Error Response | "
                f"original_type={type(getattr(error, 'original', error)).__name__}"
            ),
            interaction=interaction,
        )
        log_warn("Não foi possível enviar resposta de erro ao usuário (interação expirada?).")

# ==============================================================================
# 6. COMANDOS GLOBAIS (PREFIXO)
# ==============================================================================
@bot.command(name="sync")
@commands.is_owner()
async def sync_commands(ctx: commands.Context) -> None:
    """Força a sincronização dos slash commands globalmente. Uso: !sync (Restrito ao dono)"""
    log_sync(f"Sincronização manual solicitada por {ctx.author}...")
    try:
        synced = await bot.tree.sync()
        await ctx.send(f"✅ **{len(synced)}** comandos sincronizados globalmente com sucesso!")
        log_sync(f"{len(synced)} comandos sincronizados!")
    except Exception as e:
        await ctx.send(f"❌ Falha crítica ao sincronizar: {e}")
        log_error(f"Sincronização manual falhou: {e}")

# ==============================================================================
# 7. FUNÇÃO ASSÍNCRONA PRINCIPAL
# ==============================================================================

async def main() -> None:
    print_separator()
    print(f"{Colors.BOLD}{Colors.MAGENTA}🖤 BAPHOMET — DISCORD BOT{Colors.RESET}")
    print_separator()
    print(f"🚀 Inicializando o ecossistema...")
    print(f"🐍 Python:       {platform.python_version()}")
    print(f"📦 discord.py:   {discord.__version__}")
    print(f"🖥️ Sistema:      {platform.system()} {platform.release()}")
    print_separator()

    discord.utils.setup_logging(level=logging.WARNING)

    token = get_token()

    log_discord("Conectando aos servidores Gateway do Discord...")
    try:
        await bot.start(token)
    except discord.LoginFailure:
        log_error("Falha no login: O token fornecido no .env é inválido ou foi revogado.")
    except Exception as e:
        log_error(f"Erro fatal inesperado durante o tempo de execução: {e}")
    finally:
        if not bot.is_closed():
            await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_separator()
        log_warn("Sinal de interrupção recebido. O Bot foi encerrado com segurança.")
        print_separator()
