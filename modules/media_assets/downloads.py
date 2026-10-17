from __future__ import annotations

import asyncio
import email.utils
import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:  # pragma: no cover - depende do ambiente de deploy/teste
    aiohttp = None  # type: ignore[assignment]

from modules.media_assets.exceptions import AssetDownloadError, UnsupportedImageTypeError


LOGGER = logging.getLogger("baphomet.tierlist_templates.downloads")

DEFAULT_USER_AGENT = "BaphometTierTemplateAssetResolver/1.0 (+Discord bot; aiohttp)"
SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}


@dataclass(frozen=True)
class DownloadedImage:
    url: str
    final_url: str
    content_type: str
    data: bytes


class SafeImageDownloader:
    def __init__(
        self,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        total_timeout_seconds: float = 12.0,
        connect_timeout_seconds: float = 3.0,
        sock_read_timeout_seconds: float = 8.0,
        max_attempts: int = 3,
        retry_after_cap_seconds: float = 5.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.max_bytes = int(max_bytes)
        self.max_attempts = max(1, int(max_attempts))
        self.retry_after_cap_seconds = max(0.0, float(retry_after_cap_seconds))
        self.user_agent = user_agent
        self.timeout = None
        if aiohttp is not None:
            self.timeout = aiohttp.ClientTimeout(
                total=total_timeout_seconds,
                connect=connect_timeout_seconds,
                sock_read=sock_read_timeout_seconds,
            )

    async def download(self, url: str) -> DownloadedImage:
        if aiohttp is None:
            raise AssetDownloadError(
                "Download de imagem indisponível neste ambiente.",
                detail="aiohttp não está instalado.",
                code="aiohttp_missing",
            )
        self._validate_url(url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8",
        }
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as http:
                    async with http.get(url, headers=headers, allow_redirects=True) as response:
                        if response.status == 429:
                            retry_after = self._retry_after_seconds(response.headers)
                            LOGGER.warning("Download recebeu 429 para %s; tentativa %s.", url, attempt + 1)
                            if attempt < self.max_attempts - 1:
                                await self._sleep_before_retry(attempt, retry_after)
                                continue
                            raise AssetDownloadError(
                                "A imagem está temporariamente indisponível por limite de requisições.",
                                detail=f"HTTP 429 ao baixar {url}",
                                code="image_http_429",
                            )

                        if response.status in {403, 404}:
                            raise AssetDownloadError(
                                "Não consegui baixar essa imagem. Verifique se o link está público e tente de novo.",
                                detail=f"HTTP {response.status} ao baixar {url}",
                                code=f"image_http_{response.status}",
                            )

                        if response.status >= 500:
                            last_error = AssetDownloadError(
                                "O servidor da imagem está instável agora. Tente novamente em instantes.",
                                detail=f"HTTP {response.status} ao baixar {url}",
                                code="image_http_5xx",
                            )
                            if attempt < self.max_attempts - 1:
                                await self._sleep_before_retry(attempt, self._retry_after_seconds(response.headers))
                                continue
                            raise last_error

                        if response.status != 200:
                            raise AssetDownloadError(
                                "Não consegui baixar essa imagem. Verifique o link e tente de novo.",
                                detail=f"HTTP {response.status} ao baixar {url}",
                                code=f"image_http_{response.status}",
                            )

                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        self._validate_content_type(content_type)
                        self._validate_content_length(response.headers.get("Content-Length"), url)

                        data = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            data.extend(chunk)
                            if len(data) > self.max_bytes:
                                raise AssetDownloadError(
                                    "Essa imagem é grande demais para ser usada com segurança.",
                                    detail=f"Download excedeu {self.max_bytes} bytes: {url}",
                                    code="image_too_large",
                                )

                        if not data:
                            raise AssetDownloadError(
                                "O link não retornou dados de imagem.",
                                detail=f"Resposta vazia ao baixar {url}",
                                code="image_empty",
                            )

                        return DownloadedImage(
                            url=url,
                            final_url=str(response.url),
                            content_type=content_type,
                            data=bytes(data),
                        )

            except (AssetDownloadError, UnsupportedImageTypeError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                LOGGER.warning("Falha de rede/timeout ao baixar imagem %s: %s", url, exc)
                if attempt < self.max_attempts - 1:
                    await self._sleep_before_retry(attempt, None)
                    continue

        raise AssetDownloadError(
            "Não consegui baixar essa imagem agora. Tente novamente em instantes.",
            detail=f"Download falhou para {url}: {last_error!r}",
            code="image_network_error",
        )

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AssetDownloadError(
                "A URL informada não parece ser um link de imagem válido.",
                detail=f"URL inválida: {url!r}",
                code="image_invalid_url",
            )
        host = (parsed.hostname or "").casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            raise AssetDownloadError(
                "Não posso baixar imagens de endereços locais.",
                detail=f"Host local recusado: {host}",
                code="image_local_host_blocked",
            )
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise AssetDownloadError(
                "Não posso baixar imagens de endereços de rede privada/local.",
                detail=f"IP privado/local recusado: {host}",
                code="image_private_ip_blocked",
            )

    def _validate_content_type(self, content_type: str) -> None:
        if content_type == "image/svg+xml":
            raise UnsupportedImageTypeError(
                "SVG não é aceito como imagem de template nesta etapa.",
                detail="Content-Type image/svg+xml recusado por não haver rasterização segura.",
                code="image_svg_unsupported",
            )
        if not content_type.startswith("image/"):
            raise UnsupportedImageTypeError(
                "O link informado não retornou uma imagem.",
                detail=f"Content-Type não-imagem: {content_type!r}",
                code="image_content_type_invalid",
            )
        if content_type not in SUPPORTED_IMAGE_CONTENT_TYPES:
            raise UnsupportedImageTypeError(
                "Esse tipo de imagem ainda não é aceito. Use PNG, JPEG, WEBP ou GIF.",
                detail=f"Content-Type de imagem não suportado: {content_type!r}",
                code="image_content_type_unsupported",
            )

    def _validate_content_length(self, content_length: str | None, url: str) -> None:
        if not content_length:
            return
        try:
            declared_size = int(content_length)
        except ValueError:
            return
        if declared_size > self.max_bytes:
            raise AssetDownloadError(
                "Essa imagem é grande demais para ser usada com segurança.",
                detail=f"Content-Length excede {self.max_bytes} bytes em {url}: {declared_size}",
                code="image_too_large",
            )

    async def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        fallback = min(0.5 * (2 ** attempt), self.retry_after_cap_seconds)
        delay = retry_after if retry_after is not None else fallback
        delay = min(max(delay, 0.0), self.retry_after_cap_seconds)
        if delay > 0:
            await asyncio.sleep(delay)

    def _retry_after_seconds(self, headers: Any) -> float | None:
        value = None
        try:
            value = headers.get("Retry-After")
        except AttributeError:
            return None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
