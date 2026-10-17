from __future__ import annotations

import logging
from typing import Any

from modules.media_assets.database import DatabaseManager
from modules.media_assets.models import TierAsset, utc_now_iso
from modules.media_assets.repository_utils import (
    fetch_one,
    load_json_dict,
    metadata_to_json,
    new_uuid,
    normalize_optional_text,
    validate_asset_hash,
    validate_relative_storage_path,
)


LOGGER = logging.getLogger("baphomet.tierlist_templates.asset_repository")


def _asset_from_row(row: Any | None) -> TierAsset | None:
    if row is None:
        return None
    return TierAsset(
        id=row["id"],
        asset_hash=row["asset_hash"],
        storage_path=row["storage_path"],
        mime_type=row["mime_type"],
        width=int(row["width"]),
        height=int(row["height"]),
        size_bytes=int(row["size_bytes"]),
        source_type=row["source_type"],
        metadata=load_json_dict(row["metadata_json"]),
        created_at=row["created_at"],
        marked_orphan_at=row["marked_orphan_at"],
        deleted_at=row["deleted_at"],
    )


class TierAssetRepository:
    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    async def get_asset_by_hash(self, asset_hash: str, *, include_deleted: bool = False) -> TierAsset | None:
        validate_asset_hash(asset_hash)
        query = "SELECT * FROM tier_assets WHERE asset_hash = ?"
        params: tuple[Any, ...] = (asset_hash.lower(),)
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        row = await fetch_one(self.db.conn, query, params)
        return _asset_from_row(row)

    async def create_asset(
        self,
        *,
        asset_hash: str,
        storage_path: str,
        mime_type: str,
        width: int,
        height: int,
        size_bytes: int,
        source_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TierAsset:
        validate_asset_hash(asset_hash)
        validate_relative_storage_path(storage_path)
        if width <= 0 or height <= 0 or size_bytes <= 0:
            raise ValueError("width, height e size_bytes devem ser positivos.")
        clean_mime_type = normalize_optional_text(mime_type)
        if clean_mime_type is None:
            raise ValueError("mime_type não pode ser vazio.")
        asset_hash = asset_hash.lower()

        async with self.db.immediate_transaction() as conn:
            existing = await fetch_one(
                conn,
                "SELECT * FROM tier_assets WHERE asset_hash = ?",
                (asset_hash,),
            )
            now = utc_now_iso()
            if existing is not None:
                if existing["deleted_at"] is None:
                    LOGGER.info("asset_reused asset_id=%s asset_hash=%s", existing["id"], asset_hash)
                    return _asset_from_row(existing)  # type: ignore[return-value]
                await conn.execute(
                    """
                    UPDATE tier_assets
                    SET storage_path = ?, mime_type = ?, width = ?, height = ?,
                        size_bytes = ?, source_type = ?, metadata_json = ?,
                        created_at = ?, marked_orphan_at = NULL, deleted_at = NULL
                    WHERE id = ?
                    """,
                    (
                        storage_path,
                        clean_mime_type,
                        int(width),
                        int(height),
                        int(size_bytes),
                        normalize_optional_text(source_type),
                        metadata_to_json(metadata),
                        now,
                        existing["id"],
                    ),
                )
                asset = _asset_from_row(
                    await fetch_one(conn, "SELECT * FROM tier_assets WHERE id = ?", (existing["id"],))
                )
                if asset is None:
                    raise RuntimeError("Asset reativado não pôde ser recuperado.")
                LOGGER.info("asset_reused asset_id=%s asset_hash=%s reactivated=true", asset.id, asset_hash)
                return asset

            asset_id = new_uuid()
            await conn.execute(
                """
                INSERT INTO tier_assets(
                    id, asset_hash, storage_path, mime_type, width, height,
                    size_bytes, source_type, metadata_json, created_at,
                    marked_orphan_at, deleted_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    asset_id,
                    asset_hash,
                    storage_path,
                    clean_mime_type,
                    int(width),
                    int(height),
                    int(size_bytes),
                    normalize_optional_text(source_type),
                    metadata_to_json(metadata),
                    now,
                ),
            )

        asset = await self.get_asset_by_hash(asset_hash, include_deleted=True)
        if asset is None:
            raise RuntimeError("Asset criado não pôde ser recuperado.")
        LOGGER.info(
            "asset_created asset_id=%s asset_hash=%s source_type=%s size_bytes=%s",
            asset.id,
            asset.asset_hash,
            asset.source_type,
            asset.size_bytes,
        )
        return asset

    async def get_asset(self, asset_id: str, *, include_deleted: bool = False) -> TierAsset | None:
        query = "SELECT * FROM tier_assets WHERE id = ?"
        params: tuple[Any, ...] = (asset_id,)
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        row = await fetch_one(self.db.conn, query, params)
        return _asset_from_row(row)

    async def mark_orphan_candidate(self, asset_id: str, *, marked_at: str | None = None) -> TierAsset:
        now = marked_at or utc_now_iso()
        async with self.db.immediate_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE tier_assets
                SET marked_orphan_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (now, asset_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Asset não encontrado.")
        asset = await self.get_asset(asset_id)
        if asset is None:
            raise RuntimeError("Asset marcado não pôde ser recuperado.")
        return asset

    async def list_orphan_candidates(
        self,
        *,
        before: str | None = None,
        limit: int = 100,
    ) -> list[TierAsset]:
        query = """
            SELECT * FROM tier_assets
            WHERE marked_orphan_at IS NOT NULL AND deleted_at IS NULL
        """
        params: list[Any] = []
        if before is not None:
            query += " AND marked_orphan_at <= ?"
            params.append(before)
        query += " ORDER BY marked_orphan_at ASC LIMIT ?"
        params.append(int(limit))
        rows = await self.db.conn.execute_fetchall(query, tuple(params))
        return [asset for row in rows if (asset := _asset_from_row(row)) is not None]

    async def list_unreferenced_assets(self, *, limit: int = 500) -> list[TierAsset]:
        rows = await self.db.conn.execute_fetchall(
            """
            SELECT a.*
            FROM tier_assets a
            WHERE a.deleted_at IS NULL
              AND a.marked_orphan_at IS NOT NULL
            ORDER BY a.created_at ASC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [asset for row in rows if (asset := _asset_from_row(row)) is not None]

    async def soft_delete_asset(self, asset_id: str) -> TierAsset:
        now = utc_now_iso()
        async with self.db.immediate_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE tier_assets
                SET deleted_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (now, asset_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Asset não encontrado.")
        asset = await self.get_asset(asset_id, include_deleted=True)
        if asset is None:
            raise RuntimeError("Asset removido não pôde ser recuperado.")
        return asset
