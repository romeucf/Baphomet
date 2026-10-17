from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Final

import aiosqlite

from core.paths import DATA_DIR


LOGGER = logging.getLogger("baphomet.motd_db")

_DEFAULT_DB_PATH = DATA_DIR / "filme_do_dia.sqlite3"


DEFAULT_SCHEDULE_TIME: Final[str] = "18:00"
LIKE_EMOJI_COLUMN: Final[str] = "like_emoji"
DISLIKE_EMOJI_COLUMN: Final[str] = "dislike_emoji"
NEVER_WATCHED_EMOJI_COLUMN: Final[str] = "never_watched_emoji"
REACTION_EMOJI_COLUMNS: Final[tuple[str, str, str]] = (
    LIKE_EMOJI_COLUMN,
    DISLIKE_EMOJI_COLUMN,
    NEVER_WATCHED_EMOJI_COLUMN,
)
_UNSET: Final[object] = object()


@dataclass(frozen=True, slots=True)
class GuildConfig:
    guild_id: int
    channel_id: int | None
    role_id: int | None
    schedule_time: str = DEFAULT_SCHEDULE_TIME
    is_active: bool = True
    like_emoji: str | None = None
    dislike_emoji: str | None = None
    never_watched_emoji: str | None = None
    recap_active: bool = False
    recap_time: str = "18:00"


@dataclass(frozen=True, slots=True)
class MotdHistoryEntry:
    id: int
    guild_id: int
    movie_title: str
    poster_url: str | None
    date_added: str


@dataclass(frozen=True, slots=True)
class MotdQueueEntry:
    id: int
    guild_id: int
    tmdb_id: int
    movie_title: str
    user_id_sugestao: int
    date_added: str


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    guild_id: int
    tmdb_id: int
    movie_title: str | None
    date_added: str


class DatabaseManager:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(str(self.db_path)) as db:
            db.row_factory = aiosqlite.Row
            await self._apply_pragmas(db)
            yield db

    @staticmethod
    async def _apply_pragmas(db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA foreign_keys = ON;"):
            pass
        
        async with db.execute("PRAGMA journal_mode=WAL;") as cursor:
            await cursor.fetchone()

        async with db.execute("PRAGMA synchronous=NORMAL;"):
            pass

        async with db.execute("PRAGMA busy_timeout=5000;"):
            pass

    async def init_db(self) -> None:
        try:
            LOGGER.info(
                "[MOTD-DB] Inicializando banco de dados em: %s",
                self.db_path.resolve(),
            )

            async with self._connect() as db:
                # Registrar estado pré-init para diagnóstico
                tables_before = await self._list_tables(db)
                LOGGER.info(
                    "[MOTD-DB] Tabelas existentes antes do init: %s",
                    tables_before or "(nenhuma)",
                )

                async with db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guild_configs (
                        guild_id INTEGER PRIMARY KEY,
                        channel_id INTEGER,
                        role_id INTEGER,
                        schedule_time TEXT NOT NULL DEFAULT '18:00',
                        like_emoji TEXT,
                        dislike_emoji TEXT,
                        never_watched_emoji TEXT
                    );
                    """
                ):
                    pass

                await self._ensure_guild_config_columns(db)

                async with db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS movie_blacklist (
                        guild_id INTEGER NOT NULL,
                        tmdb_id INTEGER NOT NULL,
                        movie_title TEXT,
                        date_added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(guild_id, tmdb_id)
                    );
                    """
                ):
                    pass

                async with db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS motd_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        movie_title TEXT NOT NULL,
                        poster_url TEXT,
                        date_added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                ):
                    pass

                async with db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS motd_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        tmdb_id INTEGER NOT NULL,
                        movie_title TEXT NOT NULL,
                        user_id_sugestao INTEGER NOT NULL,
                        date_added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(guild_id, tmdb_id)
                    );
                    """
                ):
                    pass

                await db.commit()

                # Validação pós-init
                tables_after = await self._list_tables(db)
                LOGGER.info(
                    "[MOTD-DB] Tabelas após init: %s",
                    tables_after,
                )
                await self._validate_schema(db)

            LOGGER.info("[MOTD-DB] Banco de dados inicializado com sucesso.")
        except Exception:
            LOGGER.error(
                "[MOTD-DB] Falha CRITICA ao inicializar o banco de dados.",
                exc_info=True,
            )
            raise

    async def close(self) -> None:
        """Executa WAL checkpoint para garantir que todos os dados estão
        persistidos no arquivo principal do banco antes do shutdown."""
        try:
            async with self._connect() as db:
                async with db.execute("PRAGMA wal_checkpoint(TRUNCATE);") as cursor:
                    result = await cursor.fetchone()
                    LOGGER.info(
                        "[MOTD-DB] WAL checkpoint concluido: %s",
                        tuple(result) if result else "OK",
                    )
            LOGGER.info("[MOTD-DB] Banco de dados encerrado com seguranca.")
        except Exception:
            LOGGER.error(
                "[MOTD-DB] Erro ao executar checkpoint de shutdown.",
                exc_info=True,
            )

    @staticmethod
    async def _list_tables(db: aiosqlite.Connection) -> list[str]:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ) as cursor:
            rows = await cursor.fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    async def _validate_schema(db: aiosqlite.Connection) -> None:
        expected_tables = {
            "guild_configs",
            "movie_blacklist",
            "motd_history",
            "motd_queue",
        }
        actual_tables = set(await DatabaseManager._list_tables(db))
        missing_tables = expected_tables - actual_tables
        if missing_tables:
            LOGGER.error(
                "[MOTD-DB] ALERTA: Tabelas esperadas nao encontradas: %s",
                missing_tables,
            )
        else:
            LOGGER.info("[MOTD-DB] Validacao de schema OK — todas as tabelas presentes.")

        # Validar colunas de guild_configs
        async with db.execute("PRAGMA table_info(guild_configs);") as cursor:
            rows = await cursor.fetchall()
        columns = {str(row["name"]) for row in rows}
        expected_columns = {
            "guild_id", "channel_id", "role_id", "schedule_time",
            "is_active", "like_emoji", "dislike_emoji",
            "never_watched_emoji", "recap_active", "recap_time",
        }
        missing_columns = expected_columns - columns
        if missing_columns:
            LOGGER.error(
                "[MOTD-DB] ALERTA: Colunas faltantes em guild_configs: %s",
                missing_columns,
            )
        else:
            LOGGER.info(
                "[MOTD-DB] guild_configs OK — %d colunas presentes.",
                len(columns),
            )

    @staticmethod
    async def _ensure_guild_config_columns(db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(guild_configs);") as cursor:
            rows = await cursor.fetchall()

        existing_columns = {str(row["name"]) for row in rows}
        for column_name in REACTION_EMOJI_COLUMNS + ("is_active", "recap_active", "recap_time"):
            if column_name in existing_columns:
                continue

            if column_name == "is_active":
                async with db.execute(
                    "ALTER TABLE guild_configs ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;"
                ):
                    pass
            elif column_name == "recap_active":
                async with db.execute(
                    "ALTER TABLE guild_configs ADD COLUMN recap_active INTEGER NOT NULL DEFAULT 0;"
                ):
                    pass
            elif column_name == "recap_time":
                async with db.execute(
                    "ALTER TABLE guild_configs ADD COLUMN recap_time TEXT NOT NULL DEFAULT '18:00';"
                ):
                    pass
            else:
                async with db.execute(
                    f"ALTER TABLE guild_configs ADD COLUMN {column_name} TEXT;"
                ):
                    pass

    async def set_config(
        self,
        guild_id: int,
        channel_id: int | None,
        role_id: int | None,
        schedule_time: str = DEFAULT_SCHEDULE_TIME,
        *,
        is_active: bool | object = _UNSET,
        like_emoji: str | None | object = _UNSET,
        dislike_emoji: str | None | object = _UNSET,
        never_watched_emoji: str | None | object = _UNSET,
        recap_active: bool | object = _UNSET,
        recap_time: str | object = _UNSET,
    ) -> None:
        try:
            async with self._write_lock:
                async with self._connect() as db:
                    try:
                        async with db.execute("BEGIN IMMEDIATE;"):
                            pass

                        current_state: dict[str, Any] = {}
                        if (
                            is_active is _UNSET
                            or like_emoji is _UNSET
                            or dislike_emoji is _UNSET
                            or never_watched_emoji is _UNSET
                            or recap_active is _UNSET
                            or recap_time is _UNSET
                        ):
                            current_state = await self._fetch_current_state(
                                db,
                                guild_id,
                            )

                        next_is_active = (
                            bool(current_state.get("is_active", True))
                            if is_active is _UNSET
                            else bool(is_active)
                        )
                        next_like_emoji = self._resolve_emoji_value(
                            like_emoji,
                            current_state.get(LIKE_EMOJI_COLUMN),
                        )
                        next_dislike_emoji = self._resolve_emoji_value(
                            dislike_emoji,
                            current_state.get(DISLIKE_EMOJI_COLUMN),
                        )
                        next_never_watched_emoji = self._resolve_emoji_value(
                            never_watched_emoji,
                            current_state.get(NEVER_WATCHED_EMOJI_COLUMN),
                        )
                        next_recap_active = (
                            bool(current_state.get("recap_active", False))
                            if recap_active is _UNSET
                            else bool(recap_active)
                        )
                        next_recap_time = (
                            str(current_state.get("recap_time", "18:00"))
                            if recap_time is _UNSET
                            else str(recap_time)
                        )

                        async with db.execute(
                            """
                            INSERT INTO guild_configs (
                                guild_id,
                                channel_id,
                                role_id,
                                schedule_time,
                                is_active,
                                like_emoji,
                                dislike_emoji,
                                never_watched_emoji,
                                recap_active,
                                recap_time
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(guild_id) DO UPDATE SET
                                channel_id = excluded.channel_id,
                                role_id = excluded.role_id,
                                schedule_time = excluded.schedule_time,
                                is_active = excluded.is_active,
                                like_emoji = excluded.like_emoji,
                                dislike_emoji = excluded.dislike_emoji,
                                never_watched_emoji = excluded.never_watched_emoji,
                                recap_active = excluded.recap_active,
                                recap_time = excluded.recap_time;
                            """,
                            (
                                guild_id,
                                channel_id,
                                role_id,
                                schedule_time,
                                int(next_is_active),
                                next_like_emoji,
                                next_dislike_emoji,
                                next_never_watched_emoji,
                                int(next_recap_active),
                                next_recap_time,
                            ),
                        ):
                            pass

                        await db.commit()
                    except Exception:
                        await db.rollback()
                        raise
        except Exception:
            LOGGER.error(
                "Falha ao gravar configuracao da guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            raise

    @staticmethod
    async def _fetch_current_state(
        db: aiosqlite.Connection,
        guild_id: int,
    ) -> dict[str, Any]:
        async with db.execute(
            f"""
            SELECT
                is_active,
                {LIKE_EMOJI_COLUMN},
                {DISLIKE_EMOJI_COLUMN},
                {NEVER_WATCHED_EMOJI_COLUMN},
                recap_active,
                recap_time
            FROM guild_configs
            WHERE guild_id = ?;
            """,
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return {}

        return {
            "is_active": bool(row["is_active"]) if row["is_active"] is not None else True,
            LIKE_EMOJI_COLUMN: DatabaseManager._normalize_optional_text(
                row[LIKE_EMOJI_COLUMN]
            ),
            DISLIKE_EMOJI_COLUMN: DatabaseManager._normalize_optional_text(
                row[DISLIKE_EMOJI_COLUMN]
            ),
            NEVER_WATCHED_EMOJI_COLUMN: DatabaseManager._normalize_optional_text(
                row[NEVER_WATCHED_EMOJI_COLUMN]
            ),
            "recap_active": bool(row["recap_active"]) if row["recap_active"] is not None else False,
            "recap_time": str(row["recap_time"]) if row["recap_time"] is not None else "18:00",
        }

    @staticmethod
    def _resolve_emoji_value(value: object, current_value: str | None) -> str | None:
        if value is _UNSET:
            return current_value
        return DatabaseManager._normalize_optional_text(value)

    @staticmethod
    def _normalize_optional_text(value: object) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    async def get_config(self, guild_id: int) -> GuildConfig | None:
        try:
            async with self._connect() as db:
                async with db.execute(
                    """
                    SELECT
                        guild_id,
                        channel_id,
                        role_id,
                        COALESCE(schedule_time, ?) AS schedule_time,
                        is_active,
                        like_emoji,
                        dislike_emoji,
                        never_watched_emoji,
                        recap_active,
                        recap_time
                    FROM guild_configs
                    WHERE guild_id = ?;
                    """,
                    (DEFAULT_SCHEDULE_TIME, guild_id),
                ) as cursor:
                    row = await cursor.fetchone()

            if row is None:
                return None

            return GuildConfig(
                guild_id=int(row["guild_id"]),
                channel_id=(
                    int(row["channel_id"])
                    if row["channel_id"] is not None
                    else None
                ),
                role_id=int(row["role_id"]) if row["role_id"] is not None else None,
                schedule_time=str(row["schedule_time"] or DEFAULT_SCHEDULE_TIME),
                is_active=bool(row["is_active"]) if row["is_active"] is not None else True,
                like_emoji=self._normalize_optional_text(row["like_emoji"]),
                dislike_emoji=self._normalize_optional_text(row["dislike_emoji"]),
                never_watched_emoji=self._normalize_optional_text(
                    row["never_watched_emoji"]
                ),
                recap_active=bool(row["recap_active"]) if row["recap_active"] is not None else False,
                recap_time=str(row["recap_time"]) if row["recap_time"] is not None else "18:00",
            )
        except Exception:
            LOGGER.error(
                "Falha ao buscar configuracao da guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            raise

    async def add_to_blacklist(
        self,
        guild_id: int,
        tmdb_id: int,
        movie_title: str,
    ) -> bool:
        try:
            async with self._write_lock:
                async with self._connect() as db:
                    try:
                        async with db.execute("BEGIN IMMEDIATE;"):
                            pass

                        async with db.execute(
                            """
                            INSERT INTO movie_blacklist (
                                guild_id,
                                tmdb_id,
                                movie_title
                            )
                            VALUES (?, ?, ?)
                            ON CONFLICT(guild_id, tmdb_id) DO UPDATE SET
                                movie_title = excluded.movie_title;
                            """,
                            (guild_id, tmdb_id, movie_title),
                        ) as cursor:
                            changed_rows = cursor.rowcount

                        await db.commit()
                        return changed_rows > 0
                    except Exception:
                        await db.rollback()
                        raise
        except Exception:
            LOGGER.error(
                "Falha ao adicionar tmdb_id=%s na blacklist da guild_id=%s.",
                tmdb_id,
                guild_id,
                exc_info=True,
            )
            raise

    async def remove_from_blacklist(self, guild_id: int, tmdb_id: int) -> bool:
        try:
            async with self._write_lock:
                async with self._connect() as db:
                    try:
                        async with db.execute("BEGIN IMMEDIATE;"):
                            pass

                        async with db.execute(
                            """
                            DELETE FROM movie_blacklist
                            WHERE guild_id = ?
                              AND tmdb_id = ?;
                            """,
                            (guild_id, tmdb_id),
                        ) as cursor:
                            changed_rows = cursor.rowcount

                        await db.commit()
                        return changed_rows > 0
                    except Exception:
                        await db.rollback()
                        raise
        except Exception:
            LOGGER.error(
                "Falha ao remover tmdb_id=%s da blacklist da guild_id=%s.",
                tmdb_id,
                guild_id,
                exc_info=True,
            )
            raise

    async def is_blacklisted(self, guild_id: int, tmdb_id: int) -> bool:
        try:
            async with self._connect() as db:
                async with db.execute(
                    """
                    SELECT 1
                    FROM movie_blacklist
                    WHERE guild_id = ?
                      AND tmdb_id = ?
                    LIMIT 1;
                    """,
                    (guild_id, tmdb_id),
                ) as cursor:
                    row = await cursor.fetchone()

            return row is not None
        except Exception:
            LOGGER.error(
                "Falha ao validar blacklist tmdb_id=%s guild_id=%s.",
                tmdb_id,
                guild_id,
                exc_info=True,
            )
            raise

    async def get_blacklist(self, guild_id: int) -> list[BlacklistEntry]:
        try:
            async with self._connect() as db:
                async with db.execute(
                    """
                    SELECT
                        guild_id,
                        tmdb_id,
                        movie_title,
                        date_added
                    FROM movie_blacklist
                    WHERE guild_id = ?
                    ORDER BY date_added DESC, tmdb_id ASC;
                    """,
                    (guild_id,),
                ) as cursor:
                    rows = await cursor.fetchall()

            return [
                BlacklistEntry(
                    guild_id=int(row["guild_id"]),
                    tmdb_id=int(row["tmdb_id"]),
                    movie_title=(
                        str(row["movie_title"])
                        if row["movie_title"] is not None
                        else None
                    ),
                    date_added=str(row["date_added"]),
                )
                for row in rows
            ]
        except Exception:
            LOGGER.error(
                "Falha ao listar blacklist da guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            raise


    async def add_motd_history(self, guild_id: int, movie_title: str, poster_url: str | None) -> None:
        try:
            async with self._write_lock:
                async with self._connect() as db:
                    try:
                        async with db.execute("BEGIN IMMEDIATE;"):
                            pass

                        async with db.execute(
                            """
                            INSERT INTO motd_history (guild_id, movie_title, poster_url)
                            VALUES (?, ?, ?);
                            """,
                            (guild_id, movie_title, poster_url)
                        ):
                            pass
                            
                        # Limitar histórico para apenas os 7 mais recentes
                        async with db.execute(
                            """
                            DELETE FROM motd_history 
                            WHERE id NOT IN (
                                SELECT id FROM motd_history 
                                WHERE guild_id = ? 
                                ORDER BY date_added DESC, id DESC 
                                LIMIT 7
                            )
                            AND guild_id = ?;
                            """,
                            (guild_id, guild_id)
                        ):
                            pass

                        await db.commit()
                    except Exception:
                        await db.rollback()
                        raise
        except Exception:
            LOGGER.error(
                "Falha ao adicionar historico MOTD para guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            raise

    async def get_motd_history(self, guild_id: int) -> list[MotdHistoryEntry]:
        try:
            async with self._connect() as db:
                async with db.execute(
                    """
                    SELECT id, guild_id, movie_title, poster_url, date_added
                    FROM motd_history
                    WHERE guild_id = ?
                    ORDER BY date_added DESC, id DESC
                    LIMIT 7;
                    """,
                    (guild_id,)
                ) as cursor:
                    rows = await cursor.fetchall()
            
            return [
                MotdHistoryEntry(
                    id=row["id"],
                    guild_id=row["guild_id"],
                    movie_title=row["movie_title"],
                    poster_url=row["poster_url"],
                    date_added=row["date_added"]
                ) for row in rows
            ]
        except Exception:
            LOGGER.error(
                "Falha ao buscar historico MOTD para guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            raise

    async def add_to_motd_queue(
        self,
        guild_id: int,
        tmdb_id: int,
        movie_title: str,
        user_id_sugestao: int,
    ) -> bool:
        try:
            async with self._write_lock:
                async with self._connect() as db:
                    try:
                        async with db.execute("BEGIN IMMEDIATE;"):
                            pass

                        async with db.execute(
                            """
                            INSERT INTO motd_queue (
                                guild_id,
                                tmdb_id,
                                movie_title,
                                user_id_sugestao
                            )
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(guild_id, tmdb_id) DO NOTHING;
                            """,
                            (guild_id, tmdb_id, movie_title, user_id_sugestao)
                        ) as cursor:
                            changed_rows = cursor.rowcount

                        await db.commit()
                        return changed_rows > 0
                    except Exception:
                        await db.rollback()
                        raise
        except Exception:
            LOGGER.error(
                "Falha ao adicionar na fila MOTD tmdb_id=%s guild_id=%s.",
                tmdb_id,
                guild_id,
                exc_info=True,
            )
            raise

    async def get_motd_queue(self, guild_id: int) -> list[MotdQueueEntry]:
        try:
            async with self._connect() as db:
                async with db.execute(
                    """
                    SELECT
                        id,
                        guild_id,
                        tmdb_id,
                        movie_title,
                        user_id_sugestao,
                        date_added
                    FROM motd_queue
                    WHERE guild_id = ?
                    ORDER BY id ASC;
                    """,
                    (guild_id,),
                ) as cursor:
                    rows = await cursor.fetchall()

            return [
                MotdQueueEntry(
                    id=row["id"],
                    guild_id=row["guild_id"],
                    tmdb_id=row["tmdb_id"],
                    movie_title=row["movie_title"],
                    user_id_sugestao=row["user_id_sugestao"],
                    date_added=row["date_added"],
                )
                for row in rows
            ]
        except Exception:
            LOGGER.error(
                "Falha ao listar fila MOTD guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            raise

    async def pop_from_motd_queue(self, guild_id: int) -> MotdQueueEntry | None:
        try:
            async with self._write_lock:
                async with self._connect() as db:
                    try:
                        async with db.execute("BEGIN IMMEDIATE;"):
                            pass

                        async with db.execute(
                            """
                            SELECT
                                id,
                                guild_id,
                                tmdb_id,
                                movie_title,
                                user_id_sugestao,
                                date_added
                            FROM motd_queue
                            WHERE guild_id = ?
                            ORDER BY id ASC
                            LIMIT 1;
                            """,
                            (guild_id,),
                        ) as cursor:
                            row = await cursor.fetchone()

                        if not row:
                            await db.rollback()
                            return None

                        entry = MotdQueueEntry(
                            id=row["id"],
                            guild_id=row["guild_id"],
                            tmdb_id=row["tmdb_id"],
                            movie_title=row["movie_title"],
                            user_id_sugestao=row["user_id_sugestao"],
                            date_added=row["date_added"],
                        )

                        async with db.execute(
                            "DELETE FROM motd_queue WHERE id = ?;",
                            (entry.id,),
                        ):
                            pass

                        await db.commit()
                        return entry
                    except Exception:
                        await db.rollback()
                        raise
        except Exception:
            LOGGER.error(
                "Falha ao remover item da fila MOTD guild_id=%s.",
                guild_id,
                exc_info=True,
            )
            raise

    async def is_in_motd_queue(self, guild_id: int, tmdb_id: int) -> bool:
        try:
            async with self._connect() as db:
                async with db.execute(
                    """
                    SELECT 1
                    FROM motd_queue
                    WHERE guild_id = ?
                      AND tmdb_id = ?
                    LIMIT 1;
                    """,
                    (guild_id, tmdb_id),
                ) as cursor:
                    row = await cursor.fetchone()

            return row is not None
        except Exception:
            LOGGER.error(
                "Falha ao validar fila MOTD tmdb_id=%s guild_id=%s.",
                tmdb_id,
                guild_id,
                exc_info=True,
            )
            raise

__all__ = [
    "BlacklistEntry",
    "MotdHistoryEntry",
    "MotdQueueEntry",
    "DatabaseManager",
    "GuildConfig",
]
