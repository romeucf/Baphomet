import asyncio
import logging
import os
import socket
import time
import urllib.parse
from typing import Optional
from core.logger import log_exception

try:
    import redis.asyncio as redis
    from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
except ImportError:
    redis = None
    RedisConnectionError = OSError
    RedisTimeoutError = OSError

class ImproperlyConfiguredError(Exception):
    """Lançada quando variáveis de ambiente essenciais estão ausentes ou mal configuradas."""
    pass

logger = logging.getLogger("Baphomet.Redis")

class AbyssalRedisManager:
    """
    Gerenciador global de cache Level-1 via Redis.
    Opera como Singleton virtual, fornecendo pools para armazenamento in-memory
    chave-valor, provendo cross-guild isolation e auto-TTL.
    """
    def __init__(self, url: str = None):
        env_url = url or os.getenv("REDIS_URL")
        if not env_url or not env_url.strip():
            logger.critical("[Redis L1] FATAL BOOT: A variável REDIS_URL não está definida no ambiente.")
            raise ImproperlyConfiguredError("A variável REDIS_URL é obrigatória e não foi encontrada no ambiente.")

        self.is_production = os.getenv("ENV", "development").lower() in ("production", "prod")

        if "upstash.io" in env_url or self.is_production:
            if env_url.startswith("redis://"):
                env_url = env_url.replace("redis://", "rediss://", 1)

        if self.is_production and not env_url.startswith("rediss://"):
            logger.critical("[Redis L1] Configuração de Redis inválida para o ambiente de produção. O servidor Upstash exige SSL (rediss://).")
            raise ImproperlyConfiguredError("Configuração de Redis inválida para o ambiente de produção. O servidor Upstash exige SSL (rediss://).")

        self.url = env_url

        parsed_test = urllib.parse.urlparse(self.url)
        safe_boot_url = self.url.replace(parsed_test.password, "******") if parsed_test.password else self.url
        logger.info(f"[Redis L1] Iniciando conexão Redis com URL: {safe_boot_url}. Modo: {'Produção' if self.is_production else 'Desenvolvimento'}")

        self._pool: Optional[redis.Redis] = None
        self._is_connected = False
        self._network_blackhole = False

        self._failed_attempts = 0
        self._last_failure_time = 0.0
        self._circuit_open_duration = 60.0

        self._last_ping_latency = 0.0
        self._resolved_host = "Unknown"

    async def connect(self) -> bool:
        """
        Estabelece o pool de conexões assíncronas com o Redis de forma estrita.
        """
        if self._network_blackhole:
            return False

        parsed_url = urllib.parse.urlparse(self.url)
        host = parsed_url.hostname
        port = parsed_url.port or 6379

        if not host:
            raise ImproperlyConfiguredError(f"A REDIS_URL fornecida é malformada ou não possui hostname: {self.url}")

        logger.debug(f"[Redis L1] Iniciando diagnóstico de conexão para o host: {host}:{port}")

        try:
            addr_info = await asyncio.get_running_loop().getaddrinfo(host, None)
            resolved_ip = addr_info[0][4][0] if addr_info else host
            self._resolved_host = resolved_ip
            logger.debug(f"[Redis L1] Probe DNS SUCESSO: Host '{host}' foi resolvido para o IP {resolved_ip}.")
        except socket.gaierror as e:
            logger.error(f"[Redis L1] FATAL FORENSE: Falha de conexão: Host '{host}' não resolvido ou inacessível. Erro: {e}")
            self._pool = None
            self._is_connected = False
            self._network_blackhole = True
            return False

        try:
            start_ping = time.perf_counter()

            kwargs = {
                "decode_responses": True,
                "socket_timeout": 10.0,
                "retry_on_timeout": True
            }
            logger.debug(f"DEBUG: Pool Params: {kwargs}")

            self._pool = redis.from_url(self.url, **kwargs)

            await self._pool.ping()
            end_ping = time.perf_counter()
            self._last_ping_latency = (end_ping - start_ping) * 1000

            self._is_connected = True
            self._network_blackhole = False
            safe_url = self.url.replace(parsed_url.password, "******") if parsed_url.password else self.url
            logger.info(f"[Redis L1] Conexão estabelecida e PING validado ({self._last_ping_latency:.2f}ms): {safe_url}")
            return True
        except (RedisConnectionError, RedisTimeoutError, OSError) as e:
            error_str = str(e).lower()
            if "connection closed by server" in error_str:
                logger.error("[Redis L1] Falha de protocolo TLS/SSL. Verifique se o servidor (ex: Upstash) exige SSL e se a autenticação foi passada corretamente (rediss://).")
            else:
                log_exception(e, context=f"FALHA DE CONEXÃO ou AUTH: O daemon Redis recusou ou demorou em '{host}:{port}'")

            self._pool = None
            self._is_connected = False
            return False
        except Exception as e:
            log_exception(e, context="Falha atípica estrutural ao inicializar driver Redis")
            self._pool = None
            self._is_connected = False
            return False

    async def ensure_connection(self) -> bool:
        """
        Garante que a conexão Redis está ativa antes de executar a transação.
        """
        if self._is_connected:
            return True

        current_time = time.time()

        if self._failed_attempts >= 3:
            time_since_failure = current_time - self._last_failure_time
            if time_since_failure < self._circuit_open_duration:
                return False
            else:
                logger.info("[Redis CB] Tempo de resfriamento esgotado. Tentando religar infraestrutura...")
                self._failed_attempts = 0

        logger.info("[Redis CB] Realizando tentativa de reconexão ativa...")
        await self.close()
        success = await self.connect()

        if success:
            self._failed_attempts = 0
            logger.info("[Redis CB] Circuito fechado. Alta Disponibilidade restaurada.")
            return True
        else:
            self._failed_attempts += 1
            self._last_failure_time = current_time
            if self._failed_attempts >= 3:
                logger.critical("[Redis CB] Sistema em modo de espera por falha de conexão persistente. Entrando em Halted Mode por 60s.")
            return False

    async def close(self):
        """Drena a conexão durante o shutdown gracioso."""
        if self._pool:
            await self._pool.close()

