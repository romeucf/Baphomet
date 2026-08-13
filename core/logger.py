import traceback
import discord
from discord.ext import commands
from datetime import datetime

class Colors:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BG_RED = "\033[41m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

def _clean_traceback(exc: Exception) -> str:
    """Extrai e limpa o traceback, reduzindo ruído de bibliotecas internas."""
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    clean_tb = []
    
    # Vamos manter as linhas principais, mas se quisermos podemos filtrar "discord/ext", "asyncio" para dar menos ruído.
    # No entanto, a exigência é "TRACEBACK COMPLETO, destacando a linha exata do código do bot".
    # Vamos destacar os caminhos que contém "Baphomet" ou o arquivo atual.
    
    for line in tb_lines:
        normalized_line = line.replace("\\", "/")
        if "/discord/" in normalized_line or "/asyncio/" in normalized_line:
            clean_tb.append(f"{Colors.DIM}{line}{Colors.RESET}")
        elif "Baphomet" in normalized_line or "cogs/" in normalized_line or "main.py" in normalized_line:
            # Highlight this part
            clean_tb.append(f"{Colors.GREEN}{Colors.BOLD}{line}{Colors.RESET}")
        else:
            clean_tb.append(line)
            
    return "".join(clean_tb)

def _safe_attr(value, attribute: str, default=None):
    """Lê metadados de objetos do Discord sem deixar o próprio logger falhar."""
    try:
        return getattr(value, attribute, default)
    except Exception:
        return default


def log_exception(exc: Exception, context: str = None, interaction: discord.Interaction = None, ctx: commands.Context = None, args: tuple = None, kwargs: dict = None):
    """
    Loga uma exceção de forma cirúrgica e rica.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. Timestamp e Cabeçalho
    print(f"\n{Colors.BG_RED}{Colors.WHITE}{Colors.BOLD} === EXCEÇÃO CAPTURADA === {Colors.RESET}")
    print(f"{Colors.CYAN}{Colors.BOLD}[TIMESTAMP]{Colors.RESET} {now}")
    
    # 2. Localização do Erro
    # Se houver um traceback, pegamos o último frame que faz parte do bot
    tb = exc.__traceback__
    location = "Desconhecido"
    while tb is not None:
        frame = tb.tb_frame
        code = frame.f_code
        normalized_filename = code.co_filename.replace("\\", "/")
        if "Baphomet" in normalized_filename or "cogs/" in normalized_filename or "main.py" in normalized_filename:
            location = f"Arquivo: {code.co_filename}, Linha: {tb.tb_lineno}, Função: {code.co_name}"
        tb = tb.tb_next
        
    print(f"{Colors.CYAN}{Colors.BOLD}[LOCALIZAÇÃO DO ERRO]{Colors.RESET} {location}")
    if context:
        print(f"{Colors.CYAN}{Colors.BOLD}[CONTEXTO ADICIONAL]{Colors.RESET} {context}")
        
    # 3. Tipo da Exceção
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__
    print(f"{Colors.CYAN}{Colors.BOLD}[TIPO DA EXCEÇÃO]{Colors.RESET} {exc_module}.{exc_type}")
    
    # 4. Mensagem do Erro
    print(f"{Colors.CYAN}{Colors.BOLD}[MENSAGEM DO ERRO]{Colors.RESET} {Colors.RED}{str(exc)}{Colors.RESET}")
    
    # 5. Contexto de Interação
    if interaction:
        user = _safe_attr(interaction, "user")
        guild = _safe_attr(interaction, "guild")
        channel = _safe_attr(interaction, "channel")
        command = _safe_attr(interaction, "command")
        response = _safe_attr(interaction, "response")
        cmd_name = _safe_attr(command, "qualified_name", None) or _safe_attr(command, "name", "Unknown Command")
        try:
            response_done = response.is_done() if response is not None else None
        except Exception:
            response_done = "indisponível"
        
        print(f"{Colors.CYAN}{Colors.BOLD}[CONTEXTO DE INTERAÇÃO]{Colors.RESET}")
        print(f"  {Colors.YELLOW}Interaction ID:{Colors.RESET} {_safe_attr(interaction, 'id', 'None')}")
        print(f"  {Colors.YELLOW}Usuário:{Colors.RESET} {_safe_attr(user, 'name', 'None')} (ID: {_safe_attr(user, 'id', 'None')})")
        print(f"  {Colors.YELLOW}Guilda:{Colors.RESET} {_safe_attr(guild, 'name', 'None')} (ID: {_safe_attr(guild, 'id', 'None')})")
        print(f"  {Colors.YELLOW}Canal:{Colors.RESET} {_safe_attr(channel, 'name', 'None')} (ID: {_safe_attr(channel, 'id', 'None')})")
        print(f"  {Colors.YELLOW}Comando/Interação:{Colors.RESET} /{cmd_name}")
        print(f"  {Colors.YELLOW}Resposta reconhecida:{Colors.RESET} {response_done}")
        print(f"  {Colors.YELLOW}Comando marcado como falho:{Colors.RESET} {_safe_attr(interaction, 'command_failed', 'None')}")
        
    elif ctx:
        user = _safe_attr(ctx, "author")
        guild = _safe_attr(ctx, "guild")
        channel = _safe_attr(ctx, "channel")
        print(f"{Colors.CYAN}{Colors.BOLD}[CONTEXTO DE INTERAÇÃO]{Colors.RESET}")
        print(f"  {Colors.YELLOW}Usuário:{Colors.RESET} {_safe_attr(user, 'name', 'None')} (ID: {_safe_attr(user, 'id', 'None')})")
        print(f"  {Colors.YELLOW}Guilda:{Colors.RESET} {_safe_attr(guild, 'name', 'None')} (ID: {_safe_attr(guild, 'id', 'None')})")
        print(f"  {Colors.YELLOW}Canal:{Colors.RESET} {_safe_attr(channel, 'name', 'None')} (ID: {_safe_attr(channel, 'id', 'None')})")
        message = _safe_attr(ctx, "message")
        print(f"  {Colors.YELLOW}Comando:{Colors.RESET} {_safe_attr(message, 'content', 'None')}")
        
    if args or kwargs:
        print(f"{Colors.CYAN}{Colors.BOLD}[ARGUMENTOS]{Colors.RESET} Args: {args} | Kwargs: {kwargs}")

    # 6. Traceback Completo
    print(f"{Colors.CYAN}{Colors.BOLD}[TRACEBACK COMPLETO]{Colors.RESET}")
    clean_tb = _clean_traceback(exc)
    print(clean_tb, end="")
    print(f"{Colors.BG_RED}{Colors.WHITE}{Colors.BOLD} ========================= {Colors.RESET}\n")

def task_error_handler(task_name: str):
    """
    Decorador para tratar exceções em tasks.loop, ou simplesmente uma função helper.
    Como tasks.loop usa um método @loop.error, vamos fornecer um gerador de handler.
    """
    async def handler(exc):
        log_exception(exc, context=f"Background Task Loop: {task_name}")
    return handler
