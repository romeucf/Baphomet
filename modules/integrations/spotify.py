from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

import aiohttp
from PIL import Image, UnidentifiedImageError
from core.logger import log_exception


try:
    import spotipy
    from spotipy.cache_handler import CacheHandler
    from spotipy.exceptions import SpotifyException
    from spotipy.oauth2 import SpotifyClientCredentials
    try:
        from spotipy.oauth2 import SpotifyOauthError
    except ImportError:  # pragma: no cover - older Spotipy variants
        SpotifyOauthError = None
except ImportError:  # pragma: no cover - depends on deployment env
    spotipy = None
    CacheHandler = None
    SpotifyException = None
    SpotifyClientCredentials = None
    SpotifyOauthError = None


LOGGER = logging.getLogger("baphomet.tierlist.spotify")

SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
SUPPORTED_SPOTIFY_TYPES = {"album", "track"}
UNSUPPORTED_SPOTIFY_TYPES = {
    "artist",
    "playlist",
    "episode",
    "show",
    "user",
    "audiobook",
}


@dataclass(frozen=True)
class SpotifyParsedInput:
    kind: str
    spotify_type: str | None = None
    spotify_id: str | None = None
    query: str = ""
    unsupported_type: str | None = None


@dataclass(frozen=True)
class SpotifyResolvedItem:
    source_type: str
    display_name: str
    caption: str
    image_url: str
    cache_key: str
    spotify_type: str
    spotify_id: str
    spotify_url: str
    spotify_name: str
    album_name: str
    track_name: str | None
    artists: tuple[str, ...]
    release_date: str | None
    attribution_text: str


@dataclass
class SpotifyResolution:
    item: SpotifyResolvedItem | None = None
    candidates: list[SpotifyResolvedItem] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.candidates) and self.item is None


class SpotifyUserError(Exception):
    def __init__(self, user_message: str, *, code: str = "spotify_error") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


class SpotifyNotConfiguredError(SpotifyUserError):
    def __init__(self) -> None:
        super().__init__(
            "A integração com Spotify ainda não está configurada.",
            code="spotify_not_configured",
        )


class SpotifyImageError(SpotifyUserError):
    pass


if CacheHandler is not None:
    class SpotifyNoopCacheHandler(CacheHandler):
        def get_cached_token(self) -> None:
            return None

        def save_token_to_cache(self, token_info: dict[str, Any]) -> None:
            return None
else:  # pragma: no cover - Spotipy missing in deployment env
    SpotifyNoopCacheHandler = None


class TTLCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._values: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._values.get(key)
        if item is None:
            return None

        expires_at, value = item
        if expires_at < time.monotonic():
            self._values.pop(key, None)
            return None

        return value

    def set(self, key: str, value: Any) -> None:
        self._values[key] = (time.monotonic() + self.ttl_seconds, value)


class SpotifyInputParser:
    @staticmethod
    def parse(raw: str, *, preferred_type: str | None = None) -> SpotifyParsedInput:
        value = re.sub(r"\s+", " ", (raw or "").strip())
        if not value:
            return SpotifyParsedInput(kind="empty")

        lower = value.lower()
        typed_id = re.fullmatch(r"(album|track)\s*[: ]\s*([A-Za-z0-9]{22})", value, flags=re.IGNORECASE)
        if typed_id:
            spotify_type = typed_id.group(1).lower()
            spotify_id = typed_id.group(2)
            return SpotifyParsedInput(kind="id", spotify_type=spotify_type, spotify_id=spotify_id)

        if lower.startswith("spotify:"):
            parts = value.split(":")
            if len(parts) >= 3:
                spotify_type = parts[1].lower()
                spotify_id = parts[2].strip()
                if spotify_type in UNSUPPORTED_SPOTIFY_TYPES:
                    return SpotifyParsedInput(kind="unsupported", unsupported_type=spotify_type)
                if spotify_type not in SUPPORTED_SPOTIFY_TYPES:
                    return SpotifyParsedInput(kind="unsupported", unsupported_type=spotify_type or "desconhecido")
                if SPOTIFY_ID_RE.fullmatch(spotify_id):
                    return SpotifyParsedInput(kind="id", spotify_type=spotify_type, spotify_id=spotify_id)
            return SpotifyParsedInput(kind="invalid")

        url_value = value
        if lower.startswith("open.spotify.com/"):
            url_value = f"https://{value}"

        parsed = urlparse(url_value)
        host = (parsed.netloc or "").lower()
        if host in {"open.spotify.com", "www.open.spotify.com"}:
            path_parts = [part for part in parsed.path.split("/") if part]
            spotify_type = None
            spotify_id = None
            for index, part in enumerate(path_parts):
                normalized_part = part.lower()
                if normalized_part in SUPPORTED_SPOTIFY_TYPES or normalized_part in UNSUPPORTED_SPOTIFY_TYPES:
                    spotify_type = normalized_part
                    spotify_id = path_parts[index + 1] if index + 1 < len(path_parts) else None
                    break

            if spotify_type in UNSUPPORTED_SPOTIFY_TYPES:
                return SpotifyParsedInput(kind="unsupported", unsupported_type=spotify_type)

            if spotify_type not in SUPPORTED_SPOTIFY_TYPES or not spotify_id:
                return SpotifyParsedInput(kind="invalid")

            spotify_id = spotify_id.strip().rstrip("/")
            if not SPOTIFY_ID_RE.fullmatch(spotify_id):
                return SpotifyParsedInput(kind="invalid", spotify_type=spotify_type)

            return SpotifyParsedInput(kind="id", spotify_type=spotify_type, spotify_id=spotify_id)

        if SPOTIFY_ID_RE.fullmatch(value):
            spotify_type = preferred_type if preferred_type in SUPPORTED_SPOTIFY_TYPES else None
            return SpotifyParsedInput(kind="id", spotify_type=spotify_type, spotify_id=value)

        return SpotifyParsedInput(kind="search", query=value)


class SpotifyService:
    def __init__(
        self,
        *,
        metadata_ttl_seconds: int = 6 * 60 * 60,
        max_retries: int = 3,
    ) -> None:
        self.client_id = (
            os.getenv("SPOTIFY_ID")
            or ""
        ).strip()
        self.client_secret = (
            os.getenv("SPOTIFY_SECRET")
            or ""
        ).strip()
        self.max_retries = max(1, max_retries)
        self._client: Any | None = None
        self._client_lock = asyncio.Lock()
        self._metadata_cache = TTLCache(metadata_ttl_seconds)
        self._search_cache = TTLCache(metadata_ttl_seconds)

        if spotipy is None or SpotifyClientCredentials is None:
            LOGGER.warning("Spotipy não está instalado; integração Spotify desativada.")
        elif not self.client_id or not self.client_secret:
            LOGGER.warning("Credenciais Spotify ausentes; integração Spotify ficará inativa.")
        else:
            LOGGER.info("Integração Spotify pronta para inicialização lazy.")

    @property
    def is_configured(self) -> bool:
        return bool(spotipy and SpotifyClientCredentials and self.client_id and self.client_secret)

    async def _ensure_client(self) -> Any:
        if not self.is_configured:
            raise SpotifyNotConfiguredError()

        if self._client is not None:
            return self._client

        async with self._client_lock:
            if self._client is not None:
                return self._client

            LOGGER.info("Inicializando cliente Spotipy com Client Credentials Flow.")
            cache_handler = SpotifyNoopCacheHandler() if SpotifyNoopCacheHandler is not None else None
            auth_manager = SpotifyClientCredentials(
                client_id=self.client_id,
                client_secret=self.client_secret,
                requests_timeout=8,
                cache_handler=cache_handler,
            )
            self._client = spotipy.Spotify(
                auth_manager=auth_manager,
                requests_timeout=8,
                retries=0,
                status_retries=0,
            )
            return self._client

    async def get_album(self, album_id: str) -> dict[str, Any]:
        cache_key = f"album:{album_id}"
        cached = self._metadata_cache.get(cache_key)
        if cached is not None:
            LOGGER.info("Spotify cache hit para %s.", cache_key)
            return cached

        client = await self._ensure_client()
        album = await self._call_spotify("album", client.album, album_id)
        self._metadata_cache.set(cache_key, album)
        return album

    async def get_track(self, track_id: str) -> dict[str, Any]:
        cache_key = f"track:{track_id}"
        cached = self._metadata_cache.get(cache_key)
        if cached is not None:
            LOGGER.info("Spotify cache hit para %s.", cache_key)
            return cached

        client = await self._ensure_client()
        track = await self._call_spotify("track", client.track, track_id)
        self._metadata_cache.set(cache_key, track)
        return track

    async def search_catalog(self, query: str, *, limit: int = 6) -> dict[str, Any]:
        normalized_query = re.sub(r"\s+", " ", (query or "").strip())
        cache_key = f"search:{limit}:{normalized_query.casefold()}"
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            LOGGER.info("Spotify search cache hit para '%s'.", normalized_query)
            return cached

        client = await self._ensure_client()
        payload = await self._call_spotify(
            "search",
            client.search,
            q=normalized_query,
            type="album,track",
            limit=limit,
        )
        self._search_cache.set(cache_key, payload)
        return payload

    async def _call_spotify(self, operation: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                return await asyncio.to_thread(func, *args, **kwargs)
            except Exception as exc:
                log_exception(exc)
                if SpotifyException is not None and isinstance(exc, SpotifyException):
                    status = int(getattr(exc, "http_status", 0) or 0)
                    last_error = exc

                    if status == 429 and attempt < self.max_retries - 1:
                        retry_after = self._retry_after_seconds(exc)
                        LOGGER.warning(
                            "Spotify rate limit em %s; retry em %.1fs.",
                            operation,
                            retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    raise self._map_spotify_exception(exc, operation) from exc

                if SpotifyOauthError is not None and isinstance(exc, SpotifyOauthError):
                    LOGGER.exception("Autenticação Spotify falhou em %s.", operation)
                    raise SpotifyUserError(
                        "As credenciais do Spotify parecem inválidas ou expiraram.",
                        code="spotify_unauthorized",
                    ) from exc

                LOGGER.exception("Erro inesperado na chamada Spotipy '%s'.", operation)
                raise SpotifyUserError(
                    "Não consegui consultar o Spotify agora. Tente novamente em instantes.",
                    code="spotify_unexpected",
                ) from exc

        LOGGER.exception("Spotify excedeu tentativas em %s.", operation, exc_info=last_error)
        raise SpotifyUserError(
            "O Spotify limitou as requisições agora. Tente novamente em instantes.",
            code="spotify_rate_limited",
        )

    def _retry_after_seconds(self, exc: Exception) -> float:
        headers = getattr(exc, "headers", None) or {}
        retry_after = None
        if isinstance(headers, dict):
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return max(1.0, min(float(retry_after), 10.0))
        except (TypeError, ValueError):
            return 2.0

    def _map_spotify_exception(self, exc: Exception, operation: str) -> SpotifyUserError:
        status = int(getattr(exc, "http_status", 0) or 0)
        LOGGER.exception("Spotify API falhou em %s com HTTP %s.", operation, status)

        if status == 401:
            return SpotifyUserError(
                "As credenciais do Spotify parecem inválidas ou expiraram.",
                code="spotify_unauthorized",
            )
        if status == 403:
            return SpotifyUserError(
                "O Spotify negou acesso a esse recurso.",
                code="spotify_forbidden",
            )
        if status == 404:
            return SpotifyUserError(
                "Não consegui encontrar esse álbum ou música no Spotify.",
                code="spotify_not_found",
            )
        if status == 429:
            return SpotifyUserError(
                "O Spotify limitou as requisições agora. Tente novamente em instantes.",
                code="spotify_rate_limited",
            )
        if status >= 500:
            return SpotifyUserError(
                "O Spotify está instável agora. Tente novamente em instantes.",
                code="spotify_unavailable",
            )
        return SpotifyUserError(
            "Não consegui consultar o Spotify agora. Tente novamente em instantes.",
            code="spotify_api_error",
        )


class SpotifyInputResolver:
    def __init__(self, service: SpotifyService) -> None:
        self.service = service
        self.parser = SpotifyInputParser()

    async def resolve(
        self,
        raw: str,
        *,
        preferred_type: str | None = None,
        allow_ambiguous: bool = True,
    ) -> SpotifyResolution:
        started_at = time.monotonic()
        parsed = self.parser.parse(raw, preferred_type=preferred_type)
        LOGGER.info(
            "Entrada Spotify detectada: kind=%s type=%s id=%s.",
            parsed.kind,
            parsed.spotify_type,
            parsed.spotify_id,
        )

        if parsed.kind == "empty":
            raise SpotifyUserError("Informe uma URL, URI ou busca do Spotify.", code="spotify_empty")

        if parsed.kind == "unsupported":
            raise SpotifyUserError(
                "Esse link do Spotify não é de álbum nem de música. Nesta versão só aceito álbuns e tracks.",
                code="spotify_unsupported_type",
            )

        if parsed.kind == "invalid":
            raise SpotifyUserError(
                "Esse link ou URI do Spotify não parece válido.",
                code="spotify_invalid_input",
            )

        if parsed.kind == "id" and parsed.spotify_id:
            item = await self._resolve_id(parsed.spotify_id, parsed.spotify_type)
            LOGGER.info(
                "Spotify ID resolvido em %.2fs: %s:%s -> %s.",
                time.monotonic() - started_at,
                item.spotify_type,
                item.spotify_id,
                item.spotify_name,
            )
            return SpotifyResolution(item=item)

        if parsed.kind == "search":
            candidates = await self._search(parsed.query)
            if not candidates:
                raise SpotifyUserError("Não consegui encontrar esse álbum ou música no Spotify.", code="spotify_no_results")

            top_score = self._candidate_score(parsed.query, candidates[0])
            if top_score < 0.35:
                LOGGER.info(
                    "Busca Spotify sem resultado plausível para '%s' (score %.2f).",
                    parsed.query,
                    top_score,
                )
                raise SpotifyUserError("Não consegui encontrar esse álbum ou música no Spotify.", code="spotify_no_results")

            selected = self._auto_select_candidate(parsed.query, candidates)
            if selected is not None:
                LOGGER.info(
                    "Busca Spotify auto-resolvida em %.2fs: %s:%s -> %s.",
                    time.monotonic() - started_at,
                    selected.spotify_type,
                    selected.spotify_id,
                    selected.spotify_name,
                )
                return SpotifyResolution(item=selected)

            if allow_ambiguous:
                LOGGER.info("Busca Spotify ambígua para '%s' com %d candidatos.", parsed.query, len(candidates))
                return SpotifyResolution(candidates=candidates[:10])

            return SpotifyResolution(item=candidates[0])

        raise SpotifyUserError("Não consegui entender essa entrada do Spotify.", code="spotify_invalid_input")

    async def _resolve_id(self, spotify_id: str, spotify_type: str | None) -> SpotifyResolvedItem:
        if spotify_type == "album":
            album = await self.service.get_album(spotify_id)
            return self._album_to_resolved(album)

        if spotify_type == "track":
            track = await self.service.get_track(spotify_id)
            return self._track_to_resolved(track)

        try:
            album = await self.service.get_album(spotify_id)
            return self._album_to_resolved(album)
        except SpotifyUserError as album_error:
            if album_error.code != "spotify_not_found":
                raise

        track = await self.service.get_track(spotify_id)
        return self._track_to_resolved(track)

    async def _search(self, query: str) -> list[SpotifyResolvedItem]:
        payload = await self.service.search_catalog(query, limit=6)
        candidates: list[SpotifyResolvedItem] = []
        seen: set[tuple[str, str]] = set()

        albums = payload.get("albums", {}).get("items", [])
        tracks = payload.get("tracks", {}).get("items", [])

        for album in albums:
            if not isinstance(album, dict):
                continue
            try:
                candidate = self._album_to_resolved(album)
            except SpotifyUserError as exc:
                LOGGER.warning("Resultado de álbum ignorado na busca Spotify: %s.", exc.code)
                continue
            key = (candidate.spotify_type, candidate.spotify_id)
            if key not in seen:
                candidates.append(candidate)
                seen.add(key)

        for track in tracks:
            if not isinstance(track, dict):
                continue
            try:
                candidate = self._track_to_resolved(track)
            except SpotifyUserError as exc:
                LOGGER.warning("Resultado de track ignorado na busca Spotify: %s.", exc.code)
                continue
            key = (candidate.spotify_type, candidate.spotify_id)
            if key not in seen:
                candidates.append(candidate)
                seen.add(key)

        candidates.sort(
            key=lambda candidate: self._candidate_score(query, candidate),
            reverse=True,
        )
        return candidates

    def _auto_select_candidate(
        self,
        query: str,
        candidates: list[SpotifyResolvedItem],
    ) -> SpotifyResolvedItem | None:
        if len(candidates) == 1:
            score = self._candidate_score(query, candidates[0])
            return candidates[0] if score >= 0.70 else None

        scored = [(self._candidate_score(query, candidate), candidate) for candidate in candidates[:6]]
        scored.sort(key=lambda item: item[0], reverse=True)
        top_score, top_candidate = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if top_score >= 0.90 and top_score - second_score >= 0.12:
            return top_candidate

        return None

    def _candidate_score(self, query: str, candidate: SpotifyResolvedItem) -> float:
        query_norm = self._normalize_for_match(query)
        name_norm = self._normalize_for_match(candidate.spotify_name)
        artist_norm = self._normalize_for_match(" ".join(candidate.artists))
        combined_norm = self._normalize_for_match(f"{candidate.spotify_name} {' '.join(candidate.artists)}")

        if not query_norm:
            return 0.0

        sequence_score = max(
            SequenceMatcher(None, query_norm, name_norm).ratio(),
            SequenceMatcher(None, query_norm, combined_norm).ratio(),
        )
        query_tokens = set(query_norm.split())
        combined_tokens = set(combined_norm.split())
        overlap_score = len(query_tokens & combined_tokens) / max(1, len(query_tokens))
        artist_bonus = 0.08 if artist_norm and artist_norm in query_norm else 0.0
        return min(1.0, sequence_score * 0.65 + overlap_score * 0.35 + artist_bonus)

    def _normalize_for_match(self, value: str) -> str:
        value = (value or "").casefold()
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _album_to_resolved(self, album: dict[str, Any]) -> SpotifyResolvedItem:
        spotify_id = str(album.get("id") or "").strip()
        album_name = str(album.get("name") or "Álbum sem nome").strip()
        artists = self._artists(album.get("artists", []))
        image_url = self._best_image_url(album.get("images", []))
        spotify_url = str(album.get("external_urls", {}).get("spotify") or "").strip()
        release_date = str(album.get("release_date") or "").strip() or None

        if not spotify_id or not SPOTIFY_ID_RE.fullmatch(spotify_id):
            raise SpotifyUserError("O Spotify retornou um álbum sem ID válido.", code="spotify_invalid_payload")
        if not image_url:
            raise SpotifyUserError("Esse álbum não possui capa disponível no Spotify.", code="spotify_no_image")
        if not spotify_url:
            spotify_url = f"https://open.spotify.com/album/{spotify_id}"

        artist_text = ", ".join(artists) if artists else "Artista desconhecido"
        display_name = f"{album_name} - {artist_text}"
        return SpotifyResolvedItem(
            source_type="spotify",
            display_name=display_name,
            caption=display_name,
            image_url=image_url,
            cache_key=f"spotify:album:{spotify_id}",
            spotify_type="album",
            spotify_id=spotify_id,
            spotify_url=spotify_url,
            spotify_name=album_name,
            album_name=album_name,
            track_name=None,
            artists=tuple(artists),
            release_date=release_date,
            attribution_text=f"{album_name} - {artist_text}: {spotify_url}",
        )

    def _track_to_resolved(self, track: dict[str, Any]) -> SpotifyResolvedItem:
        spotify_id = str(track.get("id") or "").strip()
        track_name = str(track.get("name") or "Música sem nome").strip()
        artists = self._artists(track.get("artists", []))
        album = track.get("album") if isinstance(track.get("album"), dict) else {}
        album_name = str(album.get("name") or "").strip() or "Álbum sem nome"
        image_url = self._best_image_url(album.get("images", []))
        spotify_url = str(track.get("external_urls", {}).get("spotify") or "").strip()
        release_date = str(album.get("release_date") or "").strip() or None

        if not spotify_id or not SPOTIFY_ID_RE.fullmatch(spotify_id):
            raise SpotifyUserError("O Spotify retornou uma track sem ID válido.", code="spotify_invalid_payload")
        if not image_url:
            raise SpotifyUserError("A capa desse item não está disponível no Spotify.", code="spotify_no_image")
        if not spotify_url:
            spotify_url = f"https://open.spotify.com/track/{spotify_id}"

        artist_text = ", ".join(artists) if artists else "Artista desconhecido"
        display_name = f"{track_name} - {artist_text}"
        return SpotifyResolvedItem(
            source_type="spotify",
            display_name=display_name,
            caption=display_name,
            image_url=image_url,
            cache_key=f"spotify:track:{spotify_id}",
            spotify_type="track",
            spotify_id=spotify_id,
            spotify_url=spotify_url,
            spotify_name=track_name,
            album_name=album_name,
            track_name=track_name,
            artists=tuple(artists),
            release_date=release_date,
            attribution_text=f"{track_name} - {artist_text}: {spotify_url}",
        )

    def _artists(self, raw_artists: Any) -> list[str]:
        if not isinstance(raw_artists, list):
            return []
        artists = []
        for artist in raw_artists:
            if not isinstance(artist, dict):
                continue
            name = str(artist.get("name") or "").strip()
            if name:
                artists.append(name)
        return artists

    def _best_image_url(self, images: Any) -> str:
        if not isinstance(images, list) or not images:
            return ""

        valid_images = [
            image
            for image in images
            if isinstance(image, dict) and str(image.get("url") or "").strip()
        ]
        if not valid_images:
            return ""

        images_with_area = []
        for image in valid_images:
            try:
                width = int(image.get("width") or 0)
                height = int(image.get("height") or 0)
            except (TypeError, ValueError):
                width = 0
                height = 0

            if width > 0 and height > 0:
                images_with_area.append((width * height, image))

        if images_with_area:
            images_with_area.sort(key=lambda item: item[0], reverse=True)
            return str(images_with_area[0][1].get("url") or "").strip()

        return str(valid_images[0].get("url") or "").strip()


class SpotifyImageProcessor:
    def normalize(self, image_bytes: bytes) -> bytes:
        try:
            buffer = io.BytesIO(image_bytes)
            buffer.seek(0)
            with Image.open(buffer) as raw_image:
                try:
                    raw_image.seek(0)
                except Exception as exc:
                    log_exception(exc)
                    pass
                normalized = raw_image.convert("RGBA")

            output = io.BytesIO()
            normalized.save(output, format="PNG")
            output.seek(0)
            return output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise SpotifyImageError(
                "A capa desse item não pôde ser baixada com segurança.",
                code="spotify_image_invalid",
            ) from exc


class SpotifyImageDownloader:
    def __init__(
        self,
        *,
        processor: SpotifyImageProcessor,
        max_bytes: int = 5 * 1024 * 1024,
        timeout_seconds: int = 8,
        cache_ttl_seconds: int = 6 * 60 * 60,
    ) -> None:
        self.processor = processor
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self._bytes_cache = TTLCache(cache_ttl_seconds)
        self._inflight: dict[str, asyncio.Task[bytes]] = {}
        self._lock = asyncio.Lock()

    async def download(self, image_url: str, *, cache_key: str | None = None) -> bytes:
        key = cache_key or self._hash_url(image_url)
        cached = self._bytes_cache.get(key)
        if cached is not None:
            LOGGER.info("Cache hit para capa Spotify %s.", key)
            return cached

        async with self._lock:
            cached = self._bytes_cache.get(key)
            if cached is not None:
                LOGGER.info("Cache hit para capa Spotify %s.", key)
                return cached

            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._download_uncached(image_url, cache_key=key))
                self._inflight[key] = task

        try:
            return await task
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _download_uncached(self, image_url: str, *, cache_key: str) -> bytes:
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SpotifyImageError(
                "A capa desse item não pôde ser baixada com segurança.",
                code="spotify_image_invalid_url",
            )

        headers = {
            "User-Agent": "BaphometTierListBot/2.0 (+Discord bot)",
            "Accept": "image/avif,image/webp,image/apng,image/png,image/jpeg,image/*;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds, connect=3)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(image_url, headers=headers, allow_redirects=True) as response:
                    if response.status != 200:
                        LOGGER.warning("Download da capa Spotify falhou: HTTP %s.", response.status)
                        raise SpotifyImageError(
                            "A capa desse item não pôde ser baixada com segurança.",
                            code="spotify_image_http_error",
                        )

                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if not content_type.startswith("image/"):
                        LOGGER.warning("Capa Spotify recusada por Content-Type: %s.", content_type)
                        raise SpotifyImageError(
                            "A capa desse item não pôde ser baixada com segurança.",
                            code="spotify_image_content_type",
                        )

                    content_length = response.headers.get("Content-Length")
                    try:
                        if content_length and int(content_length) > self.max_bytes:
                            raise SpotifyImageError(
                                "A capa desse item é grande demais para baixar com segurança.",
                                code="spotify_image_too_large",
                            )
                    except ValueError:
                        pass

                    data = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise SpotifyImageError(
                                "A capa desse item é grande demais para baixar com segurança.",
                                code="spotify_image_too_large",
                            )

            if not data:
                raise SpotifyImageError(
                    "A capa desse item não pôde ser baixada com segurança.",
                    code="spotify_image_empty",
                )

            normalized = await asyncio.to_thread(self.processor.normalize, bytes(data))
            self._bytes_cache.set(cache_key, normalized)
            LOGGER.info("Capa Spotify baixada e validada: %d bytes.", len(normalized))
            return normalized

        except SpotifyImageError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.exception("Falha de rede/timeout ao baixar capa Spotify.")
            raise SpotifyImageError(
                "A capa desse item não pôde ser baixada com segurança.",
                code="spotify_image_network",
            ) from exc

    def _hash_url(self, image_url: str) -> str:
        digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        return f"spotify:image:{digest}"
