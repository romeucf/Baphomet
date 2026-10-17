from __future__ import annotations

import asyncio
import io
import pathlib
from urllib.parse import urlparse

import aiohttp
import discord
from PIL import Image, UnidentifiedImageError

from ..db.xp_repository import XpRepository
from ..utils import RankBadge
from core.paths import DATA_DIR


BADGE_SIZE = 64
MAX_BADGE_BYTES = 5 * 1024 * 1024
MAX_BADGE_PIXELS = 4096 * 4096
VALID_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}


class RankBadgeImageError(ValueError):
    pass


class RankBadgeService:
    def __init__(
        self,
        repository: XpRepository,
        *,
        storage_root: str | pathlib.Path = DATA_DIR / "rank_badges",
    ) -> None:
        self.repository = repository
        self.storage_root = pathlib.Path(storage_root)

    async def set_badge_from_attachment(
        self,
        *,
        guild_id: int,
        role_id: int,
        attachment: discord.Attachment,
        priority: int = 0,
        label: str | None = None,
    ) -> RankBadge:
        content_type = (attachment.content_type or "").lower()
        if content_type and content_type not in VALID_IMAGE_CONTENT_TYPES:
            raise RankBadgeImageError("O anexo enviado nao parece ser uma imagem valida.")
        if attachment.size and attachment.size > MAX_BADGE_BYTES:
            raise RankBadgeImageError("A imagem enviada e grande demais. Limite: 5 MB.")
        try:
            raw = await attachment.read()
        except discord.HTTPException as exc:
            raise RankBadgeImageError("Nao consegui baixar o anexo enviado.") from exc
        return await self._save_badge(guild_id=guild_id, role_id=role_id, raw=raw, priority=priority, label=label)

    async def set_badge_from_url(
        self,
        *,
        guild_id: int,
        role_id: int,
        url: str,
        priority: int = 0,
        label: str | None = None,
    ) -> RankBadge:
        raw = await self._download_url(url)
        return await self._save_badge(guild_id=guild_id, role_id=role_id, raw=raw, priority=priority, label=label)

    async def remove_badge(self, guild_id: int, role_id: int) -> RankBadge | None:
        badge = await self.repository.remove_rank_badge(guild_id, role_id)
        if badge is None:
            return None
        path = pathlib.Path(badge.image_path)
        if self._is_inside_storage(path) and path.exists() and path.is_file():
            try:
                await asyncio.to_thread(path.unlink)
            except OSError:
                pass
        return badge

    async def list_badges(self, guild_id: int) -> list[RankBadge]:
        return await self.repository.list_rank_badges(guild_id)

    async def get_badge(self, guild_id: int, role_id: int) -> RankBadge | None:
        return await self.repository.get_rank_badge(guild_id, role_id)

    async def resolve_member_badge(self, member: discord.Member) -> RankBadge | None:
        badges = await self.repository.list_rank_badges(member.guild.id)
        if not badges:
            return None
        roles_by_id = {role.id: role for role in member.roles}
        candidates: list[tuple[int, int, int, RankBadge]] = []
        for badge in badges:
            role = roles_by_id.get(badge.role_id)
            if role is None:
                continue
            candidates.append((badge.priority, role.position, role.id, badge))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[:3])[3]

    async def resolve_member_badge_image(self, member: discord.Member) -> tuple[RankBadge | None, bytes | None]:
        badge = await self.resolve_member_badge(member)
        if badge is None:
            return None, None
        path = pathlib.Path(badge.image_path)
        if not path.exists() or not path.is_file():
            return badge, None
        try:
            return badge, await asyncio.to_thread(path.read_bytes)
        except OSError:
            return badge, None

    async def _save_badge(
        self,
        *,
        guild_id: int,
        role_id: int,
        raw: bytes,
        priority: int,
        label: str | None,
    ) -> RankBadge:
        if not raw:
            raise RankBadgeImageError("A imagem enviada esta vazia.")
        if len(raw) > MAX_BADGE_BYTES:
            raise RankBadgeImageError("A imagem enviada e grande demais. Limite: 5 MB.")

        output_path = self.storage_root / str(guild_id) / f"{role_id}.png"
        await asyncio.to_thread(self._process_and_write_png, raw, output_path)
        return await self.repository.set_rank_badge(
            guild_id=guild_id,
            role_id=role_id,
            image_path=str(output_path),
            priority=priority,
            label=label,
        )

    async def _download_url(self, url: str) -> bytes:
        parsed = urlparse(str(url).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RankBadgeImageError("A URL precisa comecar com http:// ou https://.")

        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise RankBadgeImageError(f"A URL respondeu com status HTTP {response.status}.")
                    content_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
                    if content_type and content_type not in VALID_IMAGE_CONTENT_TYPES:
                        raise RankBadgeImageError("A URL nao parece apontar para uma imagem valida.")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > MAX_BADGE_BYTES:
                            raise RankBadgeImageError("A imagem da URL e grande demais. Limite: 5 MB.")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except RankBadgeImageError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RankBadgeImageError("Nao consegui baixar a imagem dessa URL.") from exc

    def _is_inside_storage(self, path: pathlib.Path) -> bool:
        try:
            path.resolve().relative_to(self.storage_root.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _process_and_write_png(raw: bytes, output_path: pathlib.Path) -> None:
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                if image.width <= 0 or image.height <= 0:
                    raise RankBadgeImageError("A imagem enviada nao possui dimensoes validas.")
                if image.width * image.height > MAX_BADGE_PIXELS:
                    raise RankBadgeImageError("A imagem enviada tem pixels demais para ser processada com seguranca.")
                image = image.convert("RGBA")
                image.thumbnail((BADGE_SIZE, BADGE_SIZE), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (BADGE_SIZE, BADGE_SIZE), (0, 0, 0, 0))
                x = (BADGE_SIZE - image.width) // 2
                y = (BADGE_SIZE - image.height) // 2
                canvas.alpha_composite(image, (x, y))
                output_path.parent.mkdir(parents=True, exist_ok=True)
                canvas.save(output_path, format="PNG")
        except RankBadgeImageError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise RankBadgeImageError("A imagem enviada nao pode ser aberta pelo Pillow.") from exc
