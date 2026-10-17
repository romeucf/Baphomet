from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

from modules.iceberg.models import IcebergProject, IcebergStatus, utc_now_iso
from core.logger import log_exception



LOGGER = logging.getLogger("baphomet.iceberg.repository")
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS iceberg_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS iceberg_projects (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    guild_id INTEGER NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    project_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT NULL,
    CHECK (status IN ('DRAFT', 'FINALIZED', 'DELETED'))
);

CREATE INDEX IF NOT EXISTS idx_iceberg_projects_owner_id
    ON iceberg_projects(owner_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_iceberg_projects_guild_id
    ON iceberg_projects(guild_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_iceberg_projects_status
    ON iceberg_projects(status);
"""


class IcebergDatabaseManager:
    def __init__(self, db_path: str | pathlib.Path = "data/icebergs.sqlite3") -> None:
        self.db_path = pathlib.Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("IcebergDatabaseManager ainda não foi conectado.")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self.apply_pragmas()
        await self.run_migrations()

    async def apply_pragmas(self) -> None:
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")

    async def run_migrations(self) -> None:
        await self.conn.executescript(SCHEMA_SQL)
        rows = await self.conn.execute_fetchall("SELECT version FROM iceberg_schema_migrations")
        applied = {int(row[0]) for row in rows}
        if SCHEMA_VERSION not in applied:
            await self.conn.execute(
                "INSERT INTO iceberg_schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, utc_now_iso()),
            )
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @staticmethod
    def _is_busy_error(exc: BaseException) -> bool:
        message = str(exc).casefold()
        return any(
            phrase in message
            for phrase in (
                "database is locked",
                "database is busy",
                "database table is locked",
                "database schema is locked",
            )
        )

    async def _begin_immediate_with_retry(self) -> None:
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                return
            except aiosqlite.OperationalError as exc:
                if not self._is_busy_error(exc) or attempt >= attempts:
                    if self._is_busy_error(exc):
                        LOGGER.error("sqlite_busy_failure db_path=%s attempts=%s error=%s", self.db_path, attempt, exc)
                    raise
                delay = 0.08 * attempt
                LOGGER.warning("sqlite_busy_retry db_path=%s attempt=%s next_delay_seconds=%.2f", self.db_path, attempt, delay)
                await asyncio.sleep(delay)

    @asynccontextmanager
    async def immediate_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            await self._begin_immediate_with_retry()
            try:
                yield self.conn
            except Exception as exc:
                log_exception(exc)
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()


class IcebergRepository:
    def __init__(self, db: IcebergDatabaseManager) -> None:
        self.db = db

    async def save_project(self, project: IcebergProject) -> IcebergProject:
        payload = project.to_json(pretty=False)
        async with self.db.immediate_transaction() as conn:
            row = await self._fetch_one(conn, "SELECT id FROM iceberg_projects WHERE id = ?", (project.id,))
            if row is None:
                await conn.execute(
                    """
                    INSERT INTO iceberg_projects(
                        id, owner_id, guild_id, name, status, project_json,
                        created_at, updated_at, deleted_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        project.id,
                        int(project.owner_id),
                        project.guild_id,
                        project.name,
                        project.status.value,
                        payload,
                        project.created_at,
                        project.updated_at,
                    ),
                )
            else:
                await conn.execute(
                    """
                    UPDATE iceberg_projects
                    SET owner_id = ?, guild_id = ?, name = ?, status = ?,
                        project_json = ?, updated_at = ?, deleted_at = NULL
                    WHERE id = ?
                    """,
                    (
                        int(project.owner_id),
                        project.guild_id,
                        project.name,
                        project.status.value,
                        payload,
                        project.updated_at,
                        project.id,
                    ),
                )
        saved = await self.get_project(project.id, owner_id=project.owner_id)
        if saved is None:
            raise RuntimeError("Projeto de iceberg salvo não pôde ser recuperado.")
        return saved

    async def get_project(
        self,
        project_id: str,
        *,
        owner_id: int | None = None,
        include_deleted: bool = False,
    ) -> IcebergProject | None:
        query = "SELECT * FROM iceberg_projects WHERE id = ?"
        params: list[Any] = [project_id]
        if owner_id is not None:
            query += " AND owner_id = ?"
            params.append(int(owner_id))
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        row = await self._fetch_one(self.db.conn, query, tuple(params))
        return self._project_from_row(row)

    async def list_projects_for_user(
        self,
        owner_id: int,
        *,
        guild_id: int | None = None,
        limit: int = 10,
        include_deleted: bool = False,
    ) -> list[IcebergProject]:
        query = "SELECT * FROM iceberg_projects WHERE owner_id = ?"
        params: list[Any] = [int(owner_id)]
        if guild_id is not None:
            query += " AND (guild_id = ? OR guild_id IS NULL)"
            params.append(int(guild_id))
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        rows = await self.db.conn.execute_fetchall(query, tuple(params))
        return [project for row in rows if (project := self._project_from_row(row)) is not None]

    async def mark_deleted(self, project_id: str, *, owner_id: int) -> None:
        now = utc_now_iso()
        async with self.db.immediate_transaction() as conn:
            await conn.execute(
                """
                UPDATE iceberg_projects
                SET status = ?, updated_at = ?, deleted_at = ?
                WHERE id = ? AND owner_id = ? AND deleted_at IS NULL
                """,
                (IcebergStatus.DELETED.value, now, now, project_id, int(owner_id)),
            )

    async def _fetch_one(self, conn: aiosqlite.Connection, query: str, params: tuple[Any, ...]) -> Any | None:
        cursor = await conn.execute(query, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    def _project_from_row(self, row: Any | None) -> IcebergProject | None:
        if row is None:
            return None
        try:
            payload = json.loads(row["project_json"])
        except json.JSONDecodeError:
            LOGGER.error("iceberg_project_json_invalid project_id=%s", row["id"])
            return None
        project = IcebergProject.from_dict(payload)
        project.id = row["id"]
        project.owner_id = int(row["owner_id"])
        project.guild_id = row["guild_id"]
        project.name = row["name"]
        project.status = IcebergStatus(row["status"])
        project.created_at = row["created_at"]
        project.updated_at = row["updated_at"]
        return project
