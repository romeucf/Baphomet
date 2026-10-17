from __future__ import annotations

import asyncio
import hashlib
import html
import io
import logging
import os
import secrets
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError
from core.logger import log_exception


LOGGER = logging.getLogger("baphomet.tierlist.wikipedia")

WIKIPEDIA_SOURCE_TYPE = "wikipedia_pageimage"
DEFAULT_USER_AGENT = "BaphometTierListBot/2.0 (Discord bot; contact: not-configured) discord.py/aiohttp"
SUPPORTED_IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
RESTRICTED_LICENSE_MARKERS = (
    "conteúdo restrito",
    "conteudo restrito",
    "uso restrito",
    "restricted",
    "non-free",
    "non free",
    "fair use",
    "all rights reserved",
    "copyrighted",
)


class WikipediaUserError(Exception):
    def __init__(self, user_message: str, *, code: str = "wikipedia_error") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


class WikimediaHttpError(WikipediaUserError):
    pass


@dataclass(frozen=True)
class WikipediaPageImageCandidate:
    wiki_language: str
    pageid: int
    title: str
    fullurl: str
    description: str = ""
    pageimage: str = ""
    thumbnail_url: str = ""
    thumbnail_width: int | None = None
    thumbnail_height: int | None = None
    original_url: str = ""
    index: int = 0

    @property
    def has_image(self) -> bool:
        return bool(self.pageimage)


@dataclass(frozen=True)
class WikimediaImageMetadata:
    file_pageid: int | None = None
    commons_pageid: int | None = None
    canonicaltitle: str = ""
    descriptionurl: str = ""
    url: str = ""
    thumburl: str = ""
    size: int | None = None
    width: int | None = None
    height: int | None = None
    mime: str = ""
    mediatype: str = ""
    artist: str = ""
    credit: str = ""
    license_short_name: str = ""
    license_url: str = ""
    usage_terms: str = ""
    attribution_required: str = ""
    object_name: str = ""
    image_description: str = ""
    extmetadata_categories: str = ""
    restrictions: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WikimediaFilePageData:
    pageid: int | None = None
    imageinfo: dict[str, Any] | None = None
    categories: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WikipediaResolvedImage:
    source_type: str
    display_name: str
    caption: str
    image_url: str
    image_bytes: bytes
    image_cache_key: str
    wiki_language: str
    wikipedia_pageid: int
    wikipedia_title: str
    wikipedia_url: str
    wikimedia_file_title: str
    wikimedia_file_description_url: str
    image_mime: str
    artist: str
    credit: str
    license_short_name: str
    license_url: str
    usage_terms: str
    attribution_required: str
    metadata_source: str = "MediaWiki Action API"


@dataclass
class WikipediaResolution:
    item: WikipediaResolvedImage | None = None
    candidates: list[WikipediaPageImageCandidate] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.candidates) and self.item is None


@dataclass(frozen=True)
class WikipediaSearchResults:
    all_candidates: tuple[WikipediaPageImageCandidate, ...]
    image_candidates: tuple[WikipediaPageImageCandidate, ...]


class AsyncTTLCache:
    def __init__(self, ttl_seconds: int, *, label: str) -> None:
        self.ttl_seconds = ttl_seconds
        self.label = label
        self._values: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            item = self._values.get(key)
            if item is None:
                return None

            expires_at, value = item
            if expires_at < time.monotonic():
                self._values.pop(key, None)
                return None

            LOGGER.info("Wikimedia cache hit: %s %s.", self.label, key)
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._values[key] = (time.monotonic() + self.ttl_seconds, value)


class WikimediaCache:
    def __init__(
        self,
        *,
        search_ttl_seconds: int = 6 * 60 * 60,
        page_ttl_seconds: int = 6 * 60 * 60,
        imageinfo_ttl_seconds: int = 24 * 60 * 60,
        image_bytes_ttl_seconds: int = 6 * 60 * 60,
        processed_image_ttl_seconds: int = 6 * 60 * 60,
    ) -> None:
        self.search = AsyncTTLCache(search_ttl_seconds, label="search")
        self.page = AsyncTTLCache(page_ttl_seconds, label="page")
        self.page_categories = AsyncTTLCache(page_ttl_seconds, label="page-categories")
        self.imageinfo = AsyncTTLCache(imageinfo_ttl_seconds, label="imageinfo")
        self.image_bytes = AsyncTTLCache(image_bytes_ttl_seconds, label="image-bytes")
        self.processed_image = AsyncTTLCache(processed_image_ttl_seconds, label="processed-image")


class WikimediaHttpClient:
    def __init__(
        self,
        *,
        user_agent: str | None = None,
        max_retries: int = 3,
        timeout_seconds: int = 10,
    ) -> None:
        self.user_agent = (user_agent or os.getenv("WIKIMEDIA_USER_AGENT") or DEFAULT_USER_AGENT).strip()
        self.max_retries = max(1, max_retries)
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=3, sock_read=8)

    def api_url(self, language: str) -> str:
        safe_language = re.sub(r"[^a-z0-9-]", "", (language or "pt").casefold()) or "pt"
        if safe_language in {"commons", "wikimedia-commons"}:
            return "https://commons.wikimedia.org/w/api.php"
        if safe_language == "wikidata":
            return "https://www.wikidata.org/w/api.php"
        return f"https://{safe_language}.wikipedia.org/w/api.php"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

    @property
    def image_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "image/jpeg,image/png,image/webp,image/gif,image/*;q=0.8",
        }

    async def get_json(self, language: str, params: dict[str, Any]) -> dict[str, Any]:
        url = self.api_url(language)
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                started_at = time.monotonic()
                async with aiohttp.ClientSession(timeout=self.timeout) as http:
                    async with http.get(url, params=params, headers=self.headers, allow_redirects=True) as response:
                        if response.status == 429:
                            retry_after = self._retry_after_seconds(response.headers)
                            LOGGER.warning("Wikimedia 429 em %s; retry em %ss.", language, retry_after or "backoff")
                            await self._sleep_before_retry(attempt, retry_after)
                            continue

                        if response.status in {403, 404}:
                            raise WikimediaHttpError(
                                "Não consegui consultar a Wikipedia agora. Tente novamente em instantes.",
                                code=f"wikimedia_http_{response.status}",
                            )

                        if response.status >= 500:
                            retry_after = self._retry_after_seconds(response.headers)
                            LOGGER.warning(
                                "Wikimedia HTTP %s em %s; retry em %ss.",
                                response.status,
                                language,
                                retry_after or "backoff",
                            )
                            if attempt < self.max_retries - 1:
                                await self._sleep_before_retry(attempt, retry_after)
                                continue
                            raise WikimediaHttpError(
                                "A Wikipedia está ocupada agora. Tente novamente em instantes.",
                                code="wikimedia_unavailable",
                            )

                        if response.status != 200:
                            raise WikimediaHttpError(
                                "Não consegui consultar a Wikipedia agora. Tente novamente em instantes.",
                                code=f"wikimedia_http_{response.status}",
                            )

                        payload = await response.json(content_type=None)

                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    code = str(error.get("code") or "wikimedia_api_error")
                    if code == "maxlag":
                        retry_after = self._retry_after_seconds(error)
                        LOGGER.warning("Wikimedia maxlag em %s; retry em %ss.", language, retry_after or "backoff")
                        if attempt < self.max_retries - 1:
                            await self._sleep_before_retry(attempt, retry_after)
                            continue
                        raise WikimediaHttpError(
                            "A Wikipedia está ocupada agora. Tente novamente em instantes.",
                            code="wikimedia_maxlag",
                        )
                    raise WikimediaHttpError(
                        "Não consegui consultar a Wikipedia agora. Tente novamente em instantes.",
                        code=f"wikimedia_api_{code}",
                    )

                LOGGER.info("Wikimedia API %s respondeu em %.2fs.", language, time.monotonic() - started_at)
                return payload

            except WikimediaHttpError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                LOGGER.warning("Falha de rede/timeout Wikimedia em %s: %s.", language, exc)
                if attempt < self.max_retries - 1:
                    await self._sleep_before_retry(attempt, None)
                    continue
            except ValueError as exc:
                last_error = exc
                LOGGER.warning("JSON inválido da Wikimedia em %s: %s.", language, exc)
                break

        LOGGER.exception("Wikimedia excedeu tentativas em %s.", language, exc_info=last_error)
        raise WikimediaHttpError(
            "Não consegui consultar a Wikipedia agora. Tente novamente em instantes.",
            code="wikimedia_network",
        )

    def _retry_after_seconds(self, source: Any) -> float | None:
        retry_after: Any = None
        if isinstance(source, dict):
            retry_after = source.get("Retry-After") or source.get("retry-after") or source.get("lag")
        try:
            if retry_after is None:
                return None
            return max(1.0, min(float(retry_after), 10.0))
        except (TypeError, ValueError):
            return None

    async def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else min(8.0, 1.25 * (2 ** attempt))
        delay += secrets.SystemRandom().uniform(0.0, 0.35)
        await asyncio.sleep(delay)


class WikipediaSearchResolver:
    def __init__(self, http_client: WikimediaHttpClient, cache: WikimediaCache) -> None:
        self.http_client = http_client
        self.cache = cache

    async def search(self, term: str, *, language: str) -> WikipediaSearchResults:
        normalized_key = re.sub(r"\s+", " ", term.strip()).casefold()
        cache_key = f"{language}:{normalized_key}"
        cached = await self.cache.search.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": term,
            "gsrnamespace": "0",
            "gsrlimit": "10",
            "prop": "pageimages|pageterms|info",
            "piprop": "name|thumbnail|original",
            "pithumbsize": "800",
            "pilicense": "free",
            "wbptterms": "description",
            "inprop": "url",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
            "maxlag": "5",
        }
        started_at = time.monotonic()
        payload = await self.http_client.get_json(language, params)
        pages = payload.get("query", {}).get("pages", []) if isinstance(payload, dict) else []
        candidates = self._parse_pages(pages, language=language)
        image_candidates = tuple(candidate for candidate in candidates if candidate.has_image)
        results = WikipediaSearchResults(
            all_candidates=tuple(candidates),
            image_candidates=image_candidates,
        )

        for candidate in candidates:
            await self.cache.page.set(f"{language}:{candidate.pageid}", candidate)

        LOGGER.info(
            "Wikipedia search '%s' em %s: %d resultados, %d com imagem, %.2fs.",
            term,
            language,
            len(candidates),
            len(image_candidates),
            time.monotonic() - started_at,
        )
        await self.cache.search.set(cache_key, results)
        return results

    def _parse_pages(self, pages: Any, *, language: str) -> list[WikipediaPageImageCandidate]:
        if not isinstance(pages, list):
            return []

        candidates: list[WikipediaPageImageCandidate] = []
        for fallback_index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue

            try:
                pageid = int(page.get("pageid") or 0)
            except (TypeError, ValueError):
                pageid = 0
            if pageid <= 0:
                continue

            title = str(page.get("title") or "").strip()
            if not title:
                continue

            description = ""
            terms = page.get("terms")
            if isinstance(terms, dict):
                raw_descriptions = terms.get("description")
                if isinstance(raw_descriptions, list) and raw_descriptions:
                    description = str(raw_descriptions[0] or "").strip()

            thumbnail = page.get("thumbnail") if isinstance(page.get("thumbnail"), dict) else {}
            original = page.get("original") if isinstance(page.get("original"), dict) else {}
            try:
                index = int(page.get("index") or fallback_index)
            except (TypeError, ValueError):
                index = fallback_index

            candidates.append(
                WikipediaPageImageCandidate(
                    wiki_language=language,
                    pageid=pageid,
                    title=title,
                    fullurl=str(page.get("fullurl") or "").strip(),
                    description=description,
                    pageimage=str(page.get("pageimage") or "").strip(),
                    thumbnail_url=str(thumbnail.get("source") or "").strip(),
                    thumbnail_width=self._optional_int(thumbnail.get("width")),
                    thumbnail_height=self._optional_int(thumbnail.get("height")),
                    original_url=str(original.get("source") or "").strip(),
                    index=index,
                )
            )

        candidates.sort(key=lambda candidate: candidate.index)
        return candidates

    def _optional_int(self, value: Any) -> int | None:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None


class WikimediaCategoryResolver:
    def __init__(self, http_client: WikimediaHttpClient, cache: WikimediaCache) -> None:
        self.http_client = http_client
        self.cache = cache

    async def fetch_page_categories(self, candidate: WikipediaPageImageCandidate) -> tuple[str, ...]:
        cache_key = f"{candidate.wiki_language}:{candidate.pageid}"
        cached = await self.cache.page_categories.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "action": "query",
            "prop": "categories",
            "pageids": str(candidate.pageid),
            "cllimit": "max",
            "clprop": "hidden",
            "format": "json",
            "formatversion": "2",
            "maxlag": "5",
        }
        categories: list[str] = []
        continuation: dict[str, Any] = {}
        for _ in range(8):
            payload = await self.http_client.get_json(candidate.wiki_language, {**params, **continuation})
            pages = payload.get("query", {}).get("pages", []) if isinstance(payload, dict) else []
            page_list = pages if isinstance(pages, list) else []
            for page in page_list:
                if not isinstance(page, dict):
                    continue
                raw_categories = page.get("categories", [])
                category_list = raw_categories if isinstance(raw_categories, list) else []
                for category in category_list:
                    if isinstance(category, dict):
                        title = str(category.get("title") or "").strip()
                        if title:
                            categories.append(title)

            raw_continue = payload.get("continue") if isinstance(payload, dict) else None
            if not isinstance(raw_continue, dict):
                break
            continuation = {
                key: value
                for key, value in raw_continue.items()
                if key != "continue"
            }
            if not continuation:
                break

        result = tuple(dict.fromkeys(categories))
        await self.cache.page_categories.set(cache_key, result)
        return result


class WikimediaAttributionExtractor:
    def from_imageinfo(
        self,
        imageinfo: dict[str, Any],
        *,
        file_pageid: int | None = None,
        commons_pageid: int | None = None,
        categories: tuple[str, ...] = tuple(),
    ) -> WikimediaImageMetadata:
        extmetadata = imageinfo.get("extmetadata")
        if not isinstance(extmetadata, dict):
            extmetadata = {}

        return WikimediaImageMetadata(
            file_pageid=file_pageid,
            commons_pageid=commons_pageid,
            canonicaltitle=self._clean(imageinfo.get("canonicaltitle")),
            descriptionurl=self._clean(imageinfo.get("descriptionurl")),
            url=self._clean(imageinfo.get("url")),
            thumburl=self._clean(imageinfo.get("thumburl")),
            size=self._optional_int(imageinfo.get("size")),
            width=self._optional_int(imageinfo.get("width")),
            height=self._optional_int(imageinfo.get("height")),
            mime=self._clean(imageinfo.get("mime")),
            mediatype=self._clean(imageinfo.get("mediatype")),
            artist=self._metadata_value(extmetadata, "Artist"),
            credit=self._metadata_value(extmetadata, "Credit"),
            license_short_name=self._metadata_value(extmetadata, "LicenseShortName"),
            license_url=self._metadata_value(extmetadata, "LicenseUrl", limit=250),
            usage_terms=self._metadata_value(extmetadata, "UsageTerms"),
            attribution_required=self._metadata_value(extmetadata, "AttributionRequired", limit=20),
            object_name=self._metadata_value(extmetadata, "ObjectName"),
            image_description=self._metadata_value(extmetadata, "ImageDescription"),
            extmetadata_categories=self._metadata_value(extmetadata, "Categories", limit=300),
            restrictions=self._metadata_value(extmetadata, "Restrictions", limit=300),
            categories=categories,
        )

    def _metadata_value(self, extmetadata: dict[str, Any], key: str, *, limit: int = 180) -> str:
        item = extmetadata.get(key)
        raw_value = item.get("value") if isinstance(item, dict) else item
        return self._clean(raw_value, limit=limit)

    def _clean(self, value: Any, *, limit: int = 180) -> str:
        if value is None:
            return ""
        text = str(value)
        text = re.sub(r"(?is)<br\s*/?>", " ", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > limit:
            text = text[: max(0, limit - 3)].rstrip() + "..."
        return text

    def _optional_int(self, value: Any) -> int | None:
        try:
            parsed = int(value)
            return parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            return None


class WikipediaPageImageResolver:
    def __init__(
        self,
        http_client: WikimediaHttpClient,
        cache: WikimediaCache,
        attribution_extractor: WikimediaAttributionExtractor,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.attribution_extractor = attribution_extractor

    async def resolve_imageinfo(self, candidate: WikipediaPageImageCandidate) -> WikimediaImageMetadata:
        file_title = self.file_title(candidate.pageimage)
        if not file_title:
            raise WikipediaUserError(
                "Encontrei artigos, mas nenhum tinha imagem livre utilizável.",
                code="wiki_no_free_image",
            )

        cache_key = f"{candidate.wiki_language}:{file_title.casefold()}"
        cached = await self.cache.imageinfo.get(cache_key)
        if cached is not None:
            return cached

        page_data = await self._fetch_file_page_data(candidate.wiki_language, file_title)
        commons_data: WikimediaFilePageData | None = None
        if candidate.wiki_language != "commons":
            try:
                commons_data = await self._fetch_file_page_data("commons", file_title)
            except WikipediaUserError as exc:
                LOGGER.warning("Não consegui consultar categorias do Commons para %s: %s", file_title, exc.code)

        imageinfo = page_data.imageinfo or (commons_data.imageinfo if commons_data else None)
        if imageinfo is None:
            raise WikipediaUserError(
                "Encontrei artigos, mas nenhum tinha imagem livre utilizável.",
                code="wiki_no_imageinfo",
            )

        categories = tuple(dict.fromkeys(
            tuple(page_data.categories) + (tuple(commons_data.categories) if commons_data else tuple())
        ))
        metadata = self.attribution_extractor.from_imageinfo(
            imageinfo,
            file_pageid=page_data.pageid,
            commons_pageid=(commons_data.pageid if commons_data else page_data.pageid if candidate.wiki_language == "commons" else None),
            categories=categories,
        )
        LOGGER.info(
            "Wikipedia imageinfo: pageid=%s file=%s mime=%s license=%s categories=%d.",
            candidate.pageid,
            file_title,
            metadata.mime,
            metadata.license_short_name,
            len(categories),
        )
        await self.cache.imageinfo.set(cache_key, metadata)
        return metadata

    def file_title(self, pageimage: str) -> str:
        value = re.sub(r"\s+", " ", (pageimage or "").strip())
        if not value:
            return ""
        if value.casefold().startswith("file:"):
            return value
        return f"File:{value}"

    async def _fetch_file_page_data(self, language: str, file_title: str) -> WikimediaFilePageData:
        params = {
            "action": "query",
            "prop": "imageinfo|categories",
            "titles": file_title,
            "iiprop": "url|size|mime|mediatype|canonicaltitle|extmetadata",
            "iiurlwidth": "800",
            "iiextmetadatafilter": (
                "Artist|Credit|LicenseShortName|LicenseUrl|UsageTerms|AttributionRequired|"
                "ObjectName|ImageDescription|Categories|Restrictions"
            ),
            "iiextmetadatalanguage": language if language not in {"commons", "wikidata"} else "en",
            "cllimit": "max",
            "clprop": "hidden",
            "format": "json",
            "formatversion": "2",
            "maxlag": "5",
        }
        pageid: int | None = None
        imageinfo: dict[str, Any] | None = None
        categories: list[str] = []
        continuation: dict[str, Any] = {}

        for _ in range(8):
            payload = await self.http_client.get_json(language, {**params, **continuation})
            pages = payload.get("query", {}).get("pages", []) if isinstance(payload, dict) else []
            page = self._first_page(pages)
            if page is not None:
                if pageid is None:
                    pageid = self._optional_pageid(page.get("pageid"))
                if imageinfo is None:
                    imageinfo = self._first_imageinfo([page])
                raw_categories = page.get("categories", [])
                category_list = raw_categories if isinstance(raw_categories, list) else []
                for category in category_list:
                    if isinstance(category, dict):
                        title = str(category.get("title") or "").strip()
                        if title:
                            categories.append(title)

            raw_continue = payload.get("continue") if isinstance(payload, dict) else None
            if not isinstance(raw_continue, dict):
                break
            continuation = {
                key: value
                for key, value in raw_continue.items()
                if key != "continue"
            }
            if not continuation:
                break

        return WikimediaFilePageData(
            pageid=pageid,
            imageinfo=imageinfo,
            categories=tuple(dict.fromkeys(categories)),
        )

    def _first_page(self, pages: Any) -> dict[str, Any] | None:
        if not isinstance(pages, list):
            return None
        for page in pages:
            if isinstance(page, dict):
                return page
        return None

    def _first_imageinfo(self, pages: Any) -> dict[str, Any] | None:
        if not isinstance(pages, list):
            return None
        for page in pages:
            if not isinstance(page, dict):
                continue
            imageinfos = page.get("imageinfo")
            if isinstance(imageinfos, list) and imageinfos and isinstance(imageinfos[0], dict):
                return imageinfos[0]
        return None

    def _optional_pageid(self, value: Any) -> int | None:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None


class PillowImageValidator:
    def __init__(self, *, max_pixels: int = 25_000_000, max_dimension: int = 1000) -> None:
        self.max_pixels = max_pixels
        self.max_dimension = max_dimension

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

                width, height = raw_image.size
                if width <= 0 or height <= 0:
                    raise WikipediaUserError(
                        "A imagem encontrada não é compatível com o renderer.",
                        code="wiki_image_invalid_dimensions",
                    )
                if width * height > self.max_pixels:
                    raise WikipediaUserError(
                        "A imagem encontrada é grande demais para processar com segurança.",
                        code="wiki_image_too_many_pixels",
                    )

                converted = raw_image.convert("RGBA")

            if converted.width > self.max_dimension or converted.height > self.max_dimension:
                converted = ImageOps.contain(
                    converted,
                    (self.max_dimension, self.max_dimension),
                    method=Image.Resampling.LANCZOS,
                )

            output = io.BytesIO()
            converted.save(output, format="PNG")
            output.seek(0)
            return output.getvalue()
        except WikipediaUserError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise WikipediaUserError(
                "A imagem encontrada não é compatível com o renderer.",
                code="wiki_image_invalid",
            ) from exc


class ImageDownloadService:
    def __init__(
        self,
        *,
        http_client: WikimediaHttpClient,
        cache: WikimediaCache,
        validator: PillowImageValidator,
        max_bytes: int = 8 * 1024 * 1024,
        timeout_seconds: int = 10,
        max_retries: int = 3,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.validator = validator
        self.max_bytes = max_bytes
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=3, sock_read=8)
        self.max_retries = max(1, max_retries)
        self._inflight: dict[str, asyncio.Task[bytes]] = {}
        self._lock = asyncio.Lock()

    async def download_validated(self, image_url: str, *, cache_key: str) -> bytes:
        processed_key = f"{cache_key}:processed:{self.validator.max_dimension}"
        cached = await self.cache.processed_image.get(processed_key)
        if cached is not None:
            return cached

        async with self._lock:
            cached = await self.cache.processed_image.get(processed_key)
            if cached is not None:
                return cached

            task = self._inflight.get(processed_key)
            if task is None:
                task = asyncio.create_task(self._download_and_validate(image_url, processed_key=processed_key))
                self._inflight[processed_key] = task

        try:
            return await task
        finally:
            if task.done():
                async with self._lock:
                    if self._inflight.get(processed_key) is task:
                        self._inflight.pop(processed_key, None)

    async def _download_and_validate(self, image_url: str, *, processed_key: str) -> bytes:
        started_at = time.monotonic()
        raw = await self._download_raw(image_url)
        normalized = await asyncio.to_thread(self.validator.normalize, raw)
        await self.cache.processed_image.set(processed_key, normalized)
        LOGGER.info(
            "Wikimedia image baixada e validada: raw=%d processed=%d em %.2fs.",
            len(raw),
            len(normalized),
            time.monotonic() - started_at,
        )
        return normalized

    async def _download_raw(self, image_url: str, *, use_cache: bool = True, store_cache: bool = True) -> bytes:
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WikipediaUserError(
                "A imagem encontrada não pôde ser baixada com segurança.",
                code="wiki_image_invalid_url",
            )

        cache_key = f"url:{self._hash(image_url)}"
        if use_cache:
            cached = await self.cache.image_bytes.get(cache_key)
            if cached is not None:
                return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as http:
                    async with http.get(
                        image_url,
                        headers=self.http_client.image_headers,
                        allow_redirects=True,
                        max_redirects=5,
                    ) as response:
                        if response.status == 429:
                            last_error = WikipediaUserError(
                                "A imagem encontrada não pôde ser baixada com segurança.",
                                code="wiki_image_rate_limited",
                            )
                            if attempt < self.max_retries - 1:
                                retry_after = self._retry_after_seconds(response.headers)
                                LOGGER.warning(
                                    "Download Wikimedia 429 para %s; retry em %ss.",
                                    image_url,
                                    retry_after or "backoff",
                                )
                                await self._sleep_before_retry(attempt, retry_after)
                                continue
                            raise last_error

                        if response.status >= 500:
                            last_error = WikipediaUserError(
                                "A imagem encontrada não pôde ser baixada com segurança.",
                                code="wiki_image_http_error",
                            )
                            if attempt < self.max_retries - 1:
                                retry_after = self._retry_after_seconds(response.headers)
                                LOGGER.warning(
                                    "Download Wikimedia HTTP %s para %s; retry em %ss.",
                                    response.status,
                                    image_url,
                                    retry_after or "backoff",
                                )
                                await self._sleep_before_retry(attempt, retry_after)
                                continue
                            raise last_error

                        if response.status != 200:
                            LOGGER.warning("Download Wikimedia falhou: HTTP %s para %s.", response.status, image_url)
                            raise WikipediaUserError(
                                "A imagem encontrada não pôde ser baixada com segurança.",
                                code="wiki_image_http_error",
                            )

                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        if content_type == "image/svg+xml":
                            raise WikipediaUserError(
                                "A imagem encontrada não é compatível com o renderer.",
                                code="wiki_image_svg",
                            )
                        if content_type and content_type not in SUPPORTED_IMAGE_MIMES:
                            LOGGER.warning("Imagem Wikimedia recusada por Content-Type: %s.", content_type)
                            raise WikipediaUserError(
                                "A imagem encontrada não pôde ser baixada com segurança.",
                                code="wiki_image_content_type",
                            )

                        content_length = response.headers.get("Content-Length")
                        try:
                            if content_length and int(content_length) > self.max_bytes:
                                raise WikipediaUserError(
                                    "A imagem encontrada não pôde ser baixada com segurança.",
                                    code="wiki_image_too_large",
                                )
                        except ValueError:
                            pass

                        data = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            data.extend(chunk)
                            if len(data) > self.max_bytes:
                                raise WikipediaUserError(
                                    "A imagem encontrada não pôde ser baixada com segurança.",
                                    code="wiki_image_too_large",
                                )

                if not data:
                    raise WikipediaUserError(
                        "A imagem encontrada não pôde ser baixada com segurança.",
                        code="wiki_image_empty",
                    )

                raw_bytes = bytes(data)
                if store_cache:
                    await self.cache.image_bytes.set(cache_key, raw_bytes)
                return raw_bytes

            except WikipediaUserError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                LOGGER.warning("Falha de rede/timeout ao baixar imagem Wikimedia: %s.", exc)
                if attempt < self.max_retries - 1:
                    await self._sleep_before_retry(attempt, None)
                    continue

        LOGGER.exception("Download Wikimedia excedeu tentativas.", exc_info=last_error)
        raise WikipediaUserError(
            "A imagem encontrada não pôde ser baixada com segurança.",
            code="wiki_image_network",
        ) from last_error

    def _retry_after_seconds(self, source: Any) -> float | None:
        retry_after = None
        if isinstance(source, dict):
            retry_after = source.get("Retry-After") or source.get("retry-after")
        try:
            if retry_after is None:
                return None
            return max(1.0, min(float(retry_after), 10.0))
        except (TypeError, ValueError):
            return None

    async def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else min(8.0, 1.25 * (2 ** attempt))
        delay += secrets.SystemRandom().uniform(0.0, 0.35)
        await asyncio.sleep(delay)

    def _hash(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


class WikipediaImageService:
    def __init__(
        self,
        *,
        default_language: str | None = None,
        fallback_language: str | None = None,
        user_agent: str | None = None,
        max_image_bytes: int | None = None,
    ) -> None:
        self.default_language = self._clean_language(default_language or os.getenv("WIKI_DEFAULT_LANG") or "pt")
        self.fallback_language = self._clean_language(fallback_language or os.getenv("WIKI_FALLBACK_LANG") or "en")
        self.cache = WikimediaCache()
        self.http_client = WikimediaHttpClient(user_agent=user_agent)
        self.search_resolver = WikipediaSearchResolver(self.http_client, self.cache)
        self.category_resolver = WikimediaCategoryResolver(self.http_client, self.cache)
        self.attribution_extractor = WikimediaAttributionExtractor()
        self.page_image_resolver = WikipediaPageImageResolver(
            self.http_client,
            self.cache,
            self.attribution_extractor,
        )
        self.validator = PillowImageValidator()
        configured_max_bytes = max_image_bytes or self._env_int("WIKI_MAX_IMAGE_BYTES", 8 * 1024 * 1024)
        self.image_downloader = ImageDownloadService(
            http_client=self.http_client,
            cache=self.cache,
            validator=self.validator,
            max_bytes=configured_max_bytes,
        )

    async def resolve(
        self,
        raw_term: str,
        *,
        allow_ambiguous: bool = True,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> WikipediaResolution:
        term = self.normalize_search_term(raw_term)
        started_at = time.monotonic()
        LOGGER.info("Wikipedia termo recebido: %r; normalizado: %r.", raw_term, term)

        search_results = await self._search_with_fallback(term)
        candidates = list(search_results.image_candidates)
        if not candidates:
            if search_results.all_candidates:
                raise WikipediaUserError(
                    "Encontrei artigos, mas nenhum tinha imagem livre utilizável para a tierlist.",
                    code="wiki_no_free_image",
                )
            raise WikipediaUserError(
                "Não encontrei nenhum artigo na Wikipedia para esse termo.",
                code="wiki_no_results",
            )

        if len(candidates) > 1 and allow_ambiguous:
            LOGGER.info("Wikipedia termo ambíguo '%s': %d candidatos.", term, len(candidates))
            return WikipediaResolution(candidates=candidates[:25])

        item = await self._resolve_first_usable_candidate(
            candidates,
            guild_id=guild_id,
            user_id=user_id,
            term=term,
        )
        LOGGER.info(
            "Wikipedia resolvida em %.2fs: %s:%s %s.",
            time.monotonic() - started_at,
            item.wiki_language,
            item.wikipedia_pageid,
            item.wikipedia_title,
        )
        return WikipediaResolution(item=item)

    async def resolve_candidate(
        self,
        candidate: WikipediaPageImageCandidate,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
        term: str = "",
    ) -> WikipediaResolvedImage:
        started_at = time.monotonic()
        metadata = await self.page_image_resolver.resolve_imageinfo(candidate)
        if self._metadata_indicates_restricted_license(metadata):
            LOGGER.warning(
                "Imagem Wikipedia rejeitada por licença restrita: pageid=%s file=%s license=%s usage=%s.",
                candidate.pageid,
                metadata.canonicaltitle or candidate.pageimage,
                metadata.license_short_name,
                metadata.usage_terms,
            )
            raise WikipediaUserError(
                "Encontrei artigos, mas nenhum tinha imagem livre utilizável para a tierlist.",
                code="wiki_non_free_image",
            )

        download_url = self._select_download_url(candidate, metadata)
        if not download_url:
            raise WikipediaUserError(
                "Encontrei artigos, mas nenhum tinha imagem livre utilizável para a tierlist.",
                code="wiki_no_download_url",
            )

        file_title = metadata.canonicaltitle or self.page_image_resolver.file_title(candidate.pageimage)
        cache_key = self._image_cache_key(candidate, file_title, download_url)
        image_bytes = await self.image_downloader.download_validated(download_url, cache_key=cache_key)
        caption = candidate.title.strip() or metadata.object_name or "Wikipedia"

        LOGGER.info(
            "Wikipedia item pronto: lang=%s pageid=%s title=%s file=%s mime=%s license=%s url=%s em %.2fs.",
            candidate.wiki_language,
            candidate.pageid,
            candidate.title,
            file_title,
            metadata.mime,
            metadata.license_short_name,
            download_url,
            time.monotonic() - started_at,
        )
        return WikipediaResolvedImage(
            source_type=WIKIPEDIA_SOURCE_TYPE,
            display_name=caption,
            caption=caption,
            image_url=download_url,
            image_bytes=image_bytes,
            image_cache_key=cache_key,
            wiki_language=candidate.wiki_language,
            wikipedia_pageid=candidate.pageid,
            wikipedia_title=candidate.title,
            wikipedia_url=candidate.fullurl,
            wikimedia_file_title=file_title,
            wikimedia_file_description_url=metadata.descriptionurl,
            image_mime=metadata.mime,
            artist=metadata.artist,
            credit=metadata.credit,
            license_short_name=metadata.license_short_name,
            license_url=metadata.license_url,
            usage_terms=metadata.usage_terms,
            attribution_required=metadata.attribution_required,
        )

    async def _resolve_first_usable_candidate(
        self,
        candidates: list[WikipediaPageImageCandidate],
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
        term: str = "",
    ) -> WikipediaResolvedImage:
        last_error: WikipediaUserError | None = None
        retryable_codes = {
            "wiki_no_free_image",
            "wiki_no_imageinfo",
            "wiki_no_download_url",
            "wiki_non_free_image",
            "wiki_image_svg",
            "wiki_image_content_type",
            "wiki_image_invalid",
            "wiki_image_invalid_dimensions",
            "wiki_image_too_many_pixels",
            "wiki_image_empty",
            "wiki_image_http_error",
        }

        for candidate in candidates:
            try:
                return await self.resolve_candidate(
                    candidate,
                    guild_id=guild_id,
                    user_id=user_id,
                    term=term,
                )
            except WikipediaUserError as exc:
                last_error = exc
                LOGGER.warning(
                    "Candidato Wikipedia ignorado: lang=%s pageid=%s title=%s code=%s.",
                    candidate.wiki_language,
                    candidate.pageid,
                    candidate.title,
                    exc.code,
                )
                if exc.code not in retryable_codes:
                    raise

        if last_error is not None:
            raise last_error

        raise WikipediaUserError(
            "Encontrei artigos, mas nenhum tinha imagem livre utilizável para a tierlist.",
            code="wiki_no_free_image",
        )

    def normalize_search_term(self, raw_term: str) -> str:
        value = re.sub(r"\s+", " ", (raw_term or "").strip())
        value = self._title_from_wikipedia_url(value) or value
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            raise WikipediaUserError("Informe um termo para pesquisar na Wikipedia.", code="wiki_empty")
        if len(value) > 100:
            raise WikipediaUserError("Use no máximo 100 caracteres no termo da Wikipedia.", code="wiki_too_long")
        if not any(char.isalnum() for char in value):
            raise WikipediaUserError("Informe um termo com letras ou números para pesquisar na Wikipedia.", code="wiki_invalid")
        return value

    async def _search_with_fallback(self, term: str) -> WikipediaSearchResults:
        languages = [self.default_language]
        if self.fallback_language and self.fallback_language not in languages:
            languages.append(self.fallback_language)

        first_with_results: WikipediaSearchResults | None = None
        last_results: WikipediaSearchResults | None = None
        last_error: WikipediaUserError | None = None
        for language in languages:
            try:
                results = await self.search_resolver.search(term, language=language)
            except WikipediaUserError as exc:
                last_error = exc
                LOGGER.warning("Busca Wikipedia falhou em %s; tentando fallback se existir: %s.", language, exc.code)
                continue

            last_results = results
            if results.all_candidates and first_with_results is None:
                first_with_results = results
            if results.image_candidates:
                return results

        if first_with_results is None and last_results is None and last_error is not None:
            raise last_error

        return first_with_results or last_results or WikipediaSearchResults(tuple(), tuple())

    def _select_download_url(
        self,
        candidate: WikipediaPageImageCandidate,
        metadata: WikimediaImageMetadata,
    ) -> str:
        for url in (candidate.thumbnail_url, metadata.thumburl):
            if self._is_usable_thumbnail_url(url):
                return url

        if self._is_small_raster_original(candidate.original_url, metadata):
            return candidate.original_url

        return ""

    def _is_usable_thumbnail_url(self, url: str) -> bool:
        value = (url or "").strip()
        if not value:
            return False
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.path.lower().endswith(".svg")

    def _is_small_raster_original(
        self,
        url: str,
        metadata: WikimediaImageMetadata,
    ) -> bool:
        if not self._is_usable_thumbnail_url(url):
            return False
        if (metadata.mime or "").lower() == "image/svg+xml":
            return False
        if metadata.mime and metadata.mime.lower() not in SUPPORTED_IMAGE_MIMES:
            return False
        if metadata.size is not None and metadata.size > 2 * 1024 * 1024:
            return False
        width = metadata.width or 0
        height = metadata.height or 0
        if width and height and width * height > 4_000_000:
            return False
        return True

    def _metadata_indicates_restricted_license(self, metadata: WikimediaImageMetadata) -> bool:
        combined = " ".join(
            part
            for part in (
                metadata.license_short_name,
                metadata.usage_terms,
                metadata.credit,
            )
            if part
        ).casefold()
        return any(marker in combined for marker in RESTRICTED_LICENSE_MARKERS)

    def _image_cache_key(self, candidate: WikipediaPageImageCandidate, file_title: str, image_url: str) -> str:
        normalized_file = re.sub(r"\s+", "_", file_title.strip())
        url_digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:16]
        return f"wiki:{candidate.wiki_language}:{candidate.pageid}:{normalized_file}:800:{url_digest}"

    def _title_from_wikipedia_url(self, value: str) -> str:
        parsed = urlparse(value)
        host = (parsed.netloc or "").casefold()
        if not host.endswith("wikipedia.org"):
            return ""
        path = parsed.path or ""
        if not path.startswith("/wiki/"):
            return ""
        title = unquote(path.removeprefix("/wiki/")).replace("_", " ").strip()
        return title

    def _clean_language(self, value: str) -> str:
        return re.sub(r"[^a-z0-9-]", "", (value or "").casefold()) or "pt"

    def _env_int(self, name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, "") or default)
            return value if value > 0 else default
        except (TypeError, ValueError):
            return default
