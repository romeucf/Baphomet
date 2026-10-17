from __future__ import annotations

"""Sistema de vínculos do Baphomet.

Os vínculos vivem no mesmo SQLite usado pelo XP para que o multiplicador seja
calculado a partir da fonte persistida, sem cache de bônus.
"""

import asyncio
import contextlib
import logging
import math
import pathlib
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from enum import Enum
from typing import Iterable, Literal

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules.vinculos.rendering.renderer import VINCULO_CARD_FILENAME, VinculoCardRenderer
from core.logger import log_exception
from core.paths import DATA_DIR


LOGGER = logging.getLogger("baphomet.vinculos")

DB_PATH = DATA_DIR / "baphomet_xp.sqlite3"

REQUEST_TIMEOUT_SECONDS = 120
REQUEST_COOLDOWN_SECONDS = 15
MIN_COMMON_INTEREST_ROLES = 2
DEFAULT_BOND_TYPE = "pacto_sangue"
DEFAULT_AFFINITY_LEVEL_2_DAYS = 7
DEFAULT_AFFINITY_LEVEL_3_DAYS = 60
DEFAULT_RUPTURE_PENALTY_DELTA = -0.10
DEFAULT_RUPTURE_PENALTY_HOURS = 72
DEFAULT_TRANSFER_TAX_RATE = 0.20
DEFAULT_RESONANCE_WINDOW_MINUTES = 24 * 60
DEFAULT_RESONANCE_BONUS = 0.05
XP_RESONANCE_WINDOW_MINUTES = 24 * 60
DEFAULT_DAILY_REQUEST_LIMIT = 2

VINCULO_COLOR = discord.Color.from_rgb(93, 39, 126)
VINCULO_SUCCESS_COLOR = discord.Color.from_rgb(132, 48, 79)
VINCULO_WARNING_COLOR = discord.Color.from_rgb(173, 113, 38)

RequestCreateStatus = Literal["created", "active_exists", "pending_exists"]
RequestFinishStatus = Literal["accepted", "refused", "expired", "duplicate", "missing", "forbidden"]
TransferStatus = Literal[
    "completed",
    "missing_vinculo",
    "self_transfer",
    "invalid_amount",
    "insufficient_funds",
    "xp_unavailable",
]


class VinculoType(str, Enum):
    PACTO_SANGUE = "pacto_sangue"
    FIO_RIVALIDADE = "fio_rivalidade"
    PACTO_SERVIDAO = "pacto_servidao"


@dataclass(frozen=True, slots=True)
class VinculoTypeMetadata:
    key: VinculoType
    label: str
    emoji: str
    color: int
    pending_title: str
    pending_description: str
    accepted_title: str
    accepted_description: str
    rupture_title: str
    rupture_description: str
    render_key: str
    render_accent: tuple[int, int, int]


VINCULO_TYPE_METADATA: dict[VinculoType, VinculoTypeMetadata] = {
    VinculoType.PACTO_SANGUE: VinculoTypeMetadata(
        key=VinculoType.PACTO_SANGUE,
        label="Pacto de sangue",
        emoji="🩸",
        color=0x84304F,
        pending_title="🩸 Pedido de pacto de sangue",
        pending_description=(
            "{requester} ofereceu sangue e destino a {target}.\n"
            "Se o altar aceitar a assinatura da outra alma, o fio passa a amadurecer com o tempo."
        ),
        accepted_title="🩸 Pacto de sangue selado",
        accepted_description="{requester} e {target} agora caminham sob o mesmo selo rubro.",
        rupture_title="🩸 Pacto de sangue rompido",
        rupture_description="{breaker} rasgou o selo com {target}; o altar respondeu com maldição temporária.",
        render_key="blood",
        render_accent=(132, 48, 79),
    ),
    VinculoType.FIO_RIVALIDADE: VinculoTypeMetadata(
        key=VinculoType.FIO_RIVALIDADE,
        label="Fio de rivalidade",
        emoji="⚔️",
        color=0xAD7126,
        pending_title="⚔️ Desafio de rivalidade",
        pending_description=(
            "{requester} puxou um fio de rivalidade na direção de {target}.\n"
            "Se aceito, a tensão entre as duas almas também alimenta o ganho de XP."
        ),
        accepted_title="⚔️ Rivalidade reconhecida",
        accepted_description="{requester} e {target} agora brilham melhor quando um desafia o outro.",
        rupture_title="⚔️ Rivalidade abandonada",
        rupture_description="{breaker} abandonou a disputa com {target}; a lâmina cobrou seu preço.",
        render_key="rivalry",
        render_accent=(173, 113, 38),
    ),
    VinculoType.PACTO_SERVIDAO: VinculoTypeMetadata(
        key=VinculoType.PACTO_SERVIDAO,
        label="Pacto de servidão",
        emoji="⛓️",
        color=0x5D277E,
        pending_title="⛓️ Oferta de servidão",
        pending_description=(
            "{requester} apresentou correntes cerimoniais a {target}.\n"
            "Aceitar o pacto costura obediência, presença e benefício mútuo no altar."
        ),
        accepted_title="⛓️ Servidão pactuada",
        accepted_description="{requester} e {target} aceitaram o peso ritual das correntes.",
        rupture_title="⛓️ Corrente quebrada",
        rupture_description="{breaker} quebrou a corrente com {target}; a marca da ruptura ficará por um tempo.",
        render_key="chains",
        render_accent=(93, 39, 126),
    ),
}

BOND_TYPE_CHOICES = [
    app_commands.Choice(name=f"{metadata.emoji} {metadata.label}", value=metadata.key.value)
    for metadata in VINCULO_TYPE_METADATA.values()
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _normalize_pair(user_a_id: int, user_b_id: int) -> tuple[int, int]:
    return (user_a_id, user_b_id) if user_a_id < user_b_id else (user_b_id, user_a_id)


def _clip_embed_value(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 18].rstrip()}... (cortado)"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_vinculo_type(raw: str | VinculoType | None) -> VinculoType:
    if isinstance(raw, VinculoType):
        return raw
    try:
        return VinculoType(str(raw or DEFAULT_BOND_TYPE))
    except ValueError:
        return VinculoType.PACTO_SANGUE


def _vinculo_metadata(raw: str | VinculoType | None) -> VinculoTypeMetadata:
    return VINCULO_TYPE_METADATA[_normalize_vinculo_type(raw)]


def _affinity_label(level: int) -> str:
    return {
        1: "fio fino",
        2: "fio de sangue",
        3: "laço da alma",
    }.get(level, "fio fino")


def _affinity_bonus(level: int) -> float:
    return {
        1: 0.05,
        2: 0.10,
        3: 0.15,
    }.get(level, 0.05)


def _clamp_tax_rate(value: float) -> float:
    if not math.isfinite(value):
        return DEFAULT_TRANSFER_TAX_RATE
    return max(0.0, min(0.95, value))


def _format_datetime(value: str | None) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "data desconhecida"
    timestamp = int(parsed.timestamp())
    return f"{parsed.strftime('%Y-%m-%d %H:%M UTC')} (<t:{timestamp}:R>)"


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "sem próximo nível"
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"


@dataclass(slots=True)
class VinculoRequestCreation:
    status: RequestCreateStatus
    request_id: int | None = None


@dataclass(slots=True)
class VinculosRuntime:
    repository: "VinculoRepository"


@dataclass(frozen=True, slots=True)
class VinculoGuildSettings:
    guild_id: int
    gossip_channel_id: int | None = None
    affinity_level_2_days: int = DEFAULT_AFFINITY_LEVEL_2_DAYS
    affinity_level_3_days: int = DEFAULT_AFFINITY_LEVEL_3_DAYS
    rupture_penalty_delta: float = DEFAULT_RUPTURE_PENALTY_DELTA
    rupture_penalty_hours: int = DEFAULT_RUPTURE_PENALTY_HOURS
    transfer_tax_rate: float = DEFAULT_TRANSFER_TAX_RATE
    resonance_window_minutes: int = DEFAULT_RESONANCE_WINDOW_MINUTES
    resonance_bonus: float = DEFAULT_RESONANCE_BONUS
    daily_request_limit: int = DEFAULT_DAILY_REQUEST_LIMIT
    global_essence: int = 0
    eclipse_ends_at: str | None = None


@dataclass(frozen=True, slots=True)
class AffinitySnapshot:
    level: int
    label: str
    bonus: float
    next_level: int | None
    next_level_at: datetime | None
    seconds_until_next: int | None


@dataclass(frozen=True, slots=True)
class ActiveVinculo:
    id: int
    guild_id: int
    user_low_id: int
    user_high_id: int
    bond_type: VinculoType
    created_at: str
    ended_at: str | None
    active: bool
    last_announced_affinity_level: int
    is_fused: bool = False

    def partner_id_for(self, user_id: int) -> int | None:
        if user_id == self.user_low_id:
            return self.user_high_id
        if user_id == self.user_high_id:
            return self.user_low_id
        return None


@dataclass(slots=True)
class VinculoRecoveryRecord:
    guild_id: int
    user_low_id: int
    user_high_id: int
    bond_type: VinculoType
    created_at: str
    last_announced_affinity_level: int = 1
    is_fused: bool = False


@dataclass(frozen=True, slots=True)
class VinculoRecoveryEvent:
    kind: Literal["accepted", "broken", "affinity"]
    user_low_id: int
    user_high_id: int
    bond_type: VinculoType
    occurred_at: str
    affinity_level: int | None = None


@dataclass(frozen=True, slots=True)
class PenaltySnapshot:
    id: int
    guild_id: int
    user_id: int
    vinculo_id: int | None
    multiplier_delta: float
    reason: str
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ResonanceSnapshot:
    active: bool
    partner_last_seen_at: str | None
    partner_last_channel_id: int | None
    seconds_since_partner_seen: int | None
    window_minutes: int
    bonus: float


@dataclass(frozen=True, slots=True)
class TransferResult:
    status: TransferStatus
    gross_amount: int = 0
    tax_amount: int = 0
    net_amount: int = 0
    tax_rate: float = DEFAULT_TRANSFER_TAX_RATE
    donor_balance_before: int = 0
    donor_balance_after: int = 0
    receiver_balance_before: int = 0
    receiver_balance_after: int = 0
    transfer_id: int | None = None


@dataclass(frozen=True, slots=True)
class TransferSummary:
    given_count: int
    given_gross: int
    given_net: int
    received_count: int
    received_gross: int
    received_net: int
    recent_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BreakResult:
    broken: bool
    vinculo: ActiveVinculo | None = None
    penalty: PenaltySnapshot | None = None
    settings: VinculoGuildSettings | None = None


@dataclass(frozen=True, slots=True)
class InterestRoleMatch:
    common_role_ids: tuple[int, ...]
    configured_interest_role_ids: tuple[int, ...]
    existing_interest_role_ids: tuple[int, ...]
    missing_configured_role_ids: tuple[int, ...]
    ignored_default_role_ids: tuple[int, ...]
    requester_interest_role_ids: tuple[int, ...]
    target_interest_role_ids: tuple[int, ...]
    shared_non_interest_role_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BonusHistorySummary:
    total_bonus_xp: int
    event_count: int


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    vinculo: ActiveVinculo
    settings: VinculoGuildSettings
    affinity: AffinitySnapshot
    resonance: ResonanceSnapshot
    penalties: tuple[PenaltySnapshot, ...]
    transfers: TransferSummary
    bonus_history: BonusHistorySummary


async def migrate_selo_supremo(db_connection: aiosqlite.Connection) -> None:
    """Executa de forma cirúrgica a migração do Selo Supremo.
    
    Adiciona a coluna is_fused silenciando erros de coluna já existente para prosseguir ininterruptamente.
    """
    try:
        await db_connection.execute("ALTER TABLE vinculos ADD COLUMN is_fused INTEGER NOT NULL DEFAULT 0;")
        await db_connection.commit()
    except sqlite3.OperationalError as e:
        error_msg = str(e).lower()
        if "duplicate column name" in error_msg or "already exists" in error_msg:
            LOGGER.info("[Migração] A coluna is_fused já se encontra consolidada na tabela vinculos.")
        else:
            raise


class VinculoRepository:
    def __init__(self, db_path: str | pathlib.Path) -> None:
        self.db_path = pathlib.Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._tx_lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Repositório de vínculos ainda não foi conectado")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        bot = getattr(self, "bot", None)
        if bot is not None:
            if not hasattr(bot, "_db_init_lock"):
                bot._db_init_lock = asyncio.Lock()
            async with bot._db_init_lock:
                await self._do_connect()
        else:
            await self._do_connect()

    async def _do_connect(self) -> None:
        if self.db_path.exists() and self.db_path.stat().st_size == 0:
            LOGGER.critical("ALERTA DE SOBRESCRITA VAZIA: O arquivo %s possui 0 bytes. Interrompendo boot para evitar data loss!", self.db_path)
            raise RuntimeError(f"Database file {self.db_path} is empty (0 bytes)! Corrupção evitada.")
            
        self._conn = await aiosqlite.connect(str(self.db_path), timeout=20.0)
        self._conn.row_factory = aiosqlite.Row
        await self._apply_pragmas()
        await self.run_migrations()
        await self.expire_pending_requests()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _apply_pragmas(self) -> None:
        conn = self.connection
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = FULL")
        await conn.execute("PRAGMA busy_timeout = 5000")

    async def run_migrations(self) -> None:
        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS vinculo_interest_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            );

            CREATE TABLE IF NOT EXISTS vinculos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_low_id INTEGER NOT NULL,
                user_high_id INTEGER NOT NULL,
                bond_type TEXT NOT NULL DEFAULT 'pacto_sangue',
                created_at TEXT NOT NULL,
                ended_at TEXT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_announced_affinity_level INTEGER NOT NULL DEFAULT 1,
                CHECK (user_low_id < user_high_id),
                CHECK (active IN (0, 1))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_vinculos_active_pair
                ON vinculos(guild_id, user_low_id, user_high_id)
                WHERE active = 1;

            CREATE INDEX IF NOT EXISTS idx_vinculos_active_low
                ON vinculos(guild_id, user_low_id, active);

            CREATE INDEX IF NOT EXISTS idx_vinculos_active_high
                ON vinculos(guild_id, user_high_id, active);

            CREATE TABLE IF NOT EXISTS vinculo_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                user_low_id INTEGER NOT NULL,
                user_high_id INTEGER NOT NULL,
                bond_type TEXT NOT NULL DEFAULT 'pacto_sangue',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                completed_at TEXT NULL,
                status TEXT NOT NULL,
                CHECK (user_low_id < user_high_id),
                CHECK (status IN ('pending', 'accepted', 'refused', 'expired'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_vinculo_pending_pair
                ON vinculo_requests(guild_id, user_low_id, user_high_id)
                WHERE status = 'pending';

            CREATE INDEX IF NOT EXISTS idx_vinculo_requests_guild_status
                ON vinculo_requests(guild_id, status, expires_at);

            CREATE TABLE IF NOT EXISTS vinculo_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                gossip_channel_id INTEGER NULL,
                affinity_level_2_days INTEGER NOT NULL DEFAULT 7,
                affinity_level_3_days INTEGER NOT NULL DEFAULT 60,
                rupture_penalty_delta REAL NOT NULL DEFAULT -0.10,
                rupture_penalty_hours INTEGER NOT NULL DEFAULT 72,
                transfer_tax_rate REAL NOT NULL DEFAULT 0.20,
                resonance_window_minutes INTEGER NOT NULL DEFAULT 30,
                resonance_bonus REAL NOT NULL DEFAULT 0.05,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vinculo_penalties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vinculo_id INTEGER NULL,
                multiplier_delta REAL NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                CHECK (active IN (0, 1))
            );

            CREATE INDEX IF NOT EXISTS idx_vinculo_penalties_active_user
                ON vinculo_penalties(guild_id, user_id, active, expires_at);

            CREATE TABLE IF NOT EXISTS vinculo_presence (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_message_id INTEGER NULL,
                last_channel_id INTEGER NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS vinculo_xp_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                vinculo_id INTEGER NOT NULL,
                donor_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                gross_amount INTEGER NOT NULL,
                tax_rate REAL NOT NULL,
                tax_amount INTEGER NOT NULL,
                net_amount INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_vinculo_xp_transfers_pair
                ON vinculo_xp_transfers(guild_id, donor_id, receiver_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS vinculo_xp_bonus_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                vinculo_id INTEGER NULL,
                user_id INTEGER NOT NULL,
                partner_id INTEGER NULL,
                bond_type TEXT NOT NULL DEFAULT 'pacto_sangue',
                base_xp INTEGER NOT NULL,
                bonus_xp INTEGER NOT NULL,
                multiplier REAL NOT NULL,
                affinity_level INTEGER NOT NULL,
                resonance_active INTEGER NOT NULL DEFAULT 0,
                penalty_delta REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                CHECK (resonance_active IN (0, 1))
            );

            CREATE INDEX IF NOT EXISTS idx_vinculo_bonus_history_user
                ON vinculo_xp_bonus_history(guild_id, user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS vinculo_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vinculo_request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                bond_type TEXT NOT NULL DEFAULT 'pacto_sangue',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_vinculo_request_log_user_time
                ON vinculo_request_log(guild_id, requester_id, created_at DESC);
            """
        )
        await self._ensure_column("vinculos", "bond_type TEXT NOT NULL DEFAULT 'pacto_sangue'", "bond_type")
        await self._ensure_column(
            "vinculos",
            "last_announced_affinity_level INTEGER NOT NULL DEFAULT 1",
            "last_announced_affinity_level",
        )
        await self._ensure_column("vinculo_requests", "bond_type TEXT NOT NULL DEFAULT 'pacto_sangue'", "bond_type")
        await self.connection.execute(
            "UPDATE vinculos SET bond_type = ? WHERE bond_type IS NULL OR bond_type = ''",
            (DEFAULT_BOND_TYPE,),
        )
        await self.connection.execute(
            "UPDATE vinculo_requests SET bond_type = ? WHERE bond_type IS NULL OR bond_type = ''",
            (DEFAULT_BOND_TYPE,),
        )
        await self._ensure_column(
            "vinculo_guild_settings",
            f"daily_request_limit INTEGER NOT NULL DEFAULT {DEFAULT_DAILY_REQUEST_LIMIT}",
            "daily_request_limit",
        )
        await self._ensure_column("vinculo_guild_settings", "global_essence INTEGER NOT NULL DEFAULT 0", "global_essence")
        await self._ensure_column("vinculo_guild_settings", "eclipse_ends_at TEXT NULL", "eclipse_ends_at")
        await self._backfill_announced_affinity_levels()
        await migrate_selo_supremo(self.connection)
        await self.connection.commit()

    async def _column_exists(self, table: str, column: str) -> bool:
        rows = await self.connection.execute_fetchall(f"PRAGMA table_info({table})")
        return any(str(row["name"]) == column for row in rows)

    async def _ensure_column(self, table: str, definition: str, column: str) -> None:
        if not await self._column_exists(table, column):
            await self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    async def _get_meta(self, key: str) -> str | None:
        rows = await self.connection.execute_fetchall("SELECT value FROM vinculo_meta WHERE key = ?", (key,))
        return str(rows[0]["value"]) if rows else None

    async def _set_meta(self, key: str, value: str) -> None:
        await self.connection.execute(
            "INSERT INTO vinculo_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def _backfill_announced_affinity_levels(self) -> None:
        if await self._get_meta("affinity_backfill_v1") == "1":
            return

        rows = await self.connection.execute_fetchall(
            """
            SELECT id, guild_id, created_at
            FROM vinculos
            WHERE active = 1
            """
        )
        settings_by_guild: dict[int, VinculoGuildSettings] = {}
        for row in rows:
            guild_id = int(row["guild_id"])
            settings = settings_by_guild.get(guild_id)
            if settings is None:
                settings = await self._get_guild_settings_no_insert(guild_id) or VinculoGuildSettings(guild_id=guild_id)
                settings_by_guild[guild_id] = settings
            affinity = self._affinity_for_created_at(str(row["created_at"]), settings)
            await self.connection.execute(
                "UPDATE vinculos SET last_announced_affinity_level = ? WHERE id = ?",
                (affinity.level, int(row["id"])),
            )
        await self._set_meta("affinity_backfill_v1", "1")

    async def expire_stale_requests(self) -> int:
        now_iso = _utc_now_iso()
        async with self._tx_lock:
            cur = await self.connection.execute(
                """
                UPDATE vinculo_requests
                SET status = 'expired',
                    completed_at = ?
                WHERE status = 'pending'
                  AND expires_at <= ?
                """,
                (now_iso, now_iso),
            )
            await self.connection.commit()
            return cur.rowcount

    async def expire_pending_requests(self) -> int:
        now_iso = _utc_now_iso()
        async with self._tx_lock:
            cur = await self.connection.execute(
                """
                UPDATE vinculo_requests
                SET status = 'expired',
                    completed_at = ?
                WHERE status = 'pending'
                """,
                (now_iso,),
            )
            await self.connection.commit()
            return cur.rowcount

    async def list_interest_role_ids(self, guild_id: int) -> list[int]:
        rows = await self.connection.execute_fetchall(
            """
            SELECT role_id
            FROM vinculo_interest_roles
            WHERE guild_id = ?
            ORDER BY created_at ASC, role_id ASC
            """,
            (guild_id,),
        )
        return [int(row["role_id"]) for row in rows]

    async def add_interest_role(self, guild_id: int, role_id: int) -> bool:
        async with self._tx_lock:
            cur = await self.connection.execute(
                """
                INSERT OR IGNORE INTO vinculo_interest_roles(guild_id, role_id, created_at)
                VALUES(?, ?, ?)
                """,
                (guild_id, role_id, _utc_now_iso()),
            )
            await self.connection.commit()
            return cur.rowcount > 0

    async def remove_interest_role(self, guild_id: int, role_id: int) -> bool:
        async with self._tx_lock:
            cur = await self.connection.execute(
                "DELETE FROM vinculo_interest_roles WHERE guild_id = ? AND role_id = ?",
                (guild_id, role_id),
            )
            await self.connection.commit()
            return cur.rowcount > 0

    async def clear_interest_roles(self, guild_id: int) -> int:
        async with self._tx_lock:
            cur = await self.connection.execute(
                "DELETE FROM vinculo_interest_roles WHERE guild_id = ?",
                (guild_id,),
            )
            await self.connection.commit()
            return cur.rowcount

    async def create_request(
        self,
        *,
        guild_id: int,
        requester_id: int,
        target_id: int,
        bond_type: VinculoType,
        expires_at: datetime,
    ) -> VinculoRequestCreation:
        user_low_id, user_high_id = _normalize_pair(requester_id, target_id)
        now_iso = _utc_now_iso()
        expires_iso = expires_at.astimezone(timezone.utc).isoformat()
        bond_type_value = _normalize_vinculo_type(bond_type).value

        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    UPDATE vinculo_requests
                    SET status = 'expired',
                        completed_at = ?
                    WHERE status = 'pending'
                      AND expires_at <= ?
                    """,
                    (now_iso, now_iso),
                )
                active_rows = await conn.execute_fetchall(
                    """
                    SELECT id
                    FROM vinculos
                    WHERE guild_id = ?
                      AND user_low_id = ?
                      AND user_high_id = ?
                      AND active = 1
                    LIMIT 1
                    """,
                    (guild_id, user_low_id, user_high_id),
                )
                if active_rows:
                    await conn.commit()
                    return VinculoRequestCreation(status="active_exists")

                pending_rows = await conn.execute_fetchall(
                    """
                    SELECT id
                    FROM vinculo_requests
                    WHERE guild_id = ?
                      AND user_low_id = ?
                      AND user_high_id = ?
                      AND status = 'pending'
                    LIMIT 1
                    """,
                    (guild_id, user_low_id, user_high_id),
                )
                if pending_rows:
                    await conn.commit()
                    return VinculoRequestCreation(status="pending_exists", request_id=int(pending_rows[0]["id"]))

                cur = await conn.execute(
                    """
                    INSERT INTO vinculo_requests(
                        guild_id,
                        requester_id,
                        target_id,
                        user_low_id,
                        user_high_id,
                        bond_type,
                        created_at,
                        expires_at,
                        status
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        guild_id,
                        requester_id,
                        target_id,
                        user_low_id,
                        user_high_id,
                        bond_type_value,
                        now_iso,
                        expires_iso,
                    ),
                )
                await conn.commit()
                return VinculoRequestCreation(status="created", request_id=int(cur.lastrowid))
            except aiosqlite.IntegrityError:
                await conn.rollback()
                return VinculoRequestCreation(status="pending_exists")
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def accept_request(self, request_id: int, guild_id: int, target_id: int) -> RequestFinishStatus:
        now_iso = _utc_now_iso()
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                rows = await conn.execute_fetchall(
                    "SELECT * FROM vinculo_requests WHERE id = ? AND guild_id = ?",
                    (request_id, guild_id),
                )
                if not rows:
                    await conn.rollback()
                    return "missing"

                request = rows[0]
                if int(request["target_id"]) != target_id:
                    await conn.rollback()
                    return "forbidden"
                if request["status"] != "pending":
                    await conn.rollback()
                    return str(request["status"])  # type: ignore[return-value]
                if str(request["expires_at"]) <= now_iso:
                    await conn.execute(
                        "UPDATE vinculo_requests SET status = 'expired', completed_at = ? WHERE id = ?",
                        (now_iso, request_id),
                    )
                    await conn.commit()
                    return "expired"

                guild_id = int(request["guild_id"])
                user_low_id = int(request["user_low_id"])
                user_high_id = int(request["user_high_id"])
                bond_type = _normalize_vinculo_type(request["bond_type"]).value
                active_rows = await conn.execute_fetchall(
                    """
                    SELECT id
                    FROM vinculos
                    WHERE guild_id = ?
                      AND user_low_id = ?
                      AND user_high_id = ?
                      AND active = 1
                    LIMIT 1
                    """,
                    (guild_id, user_low_id, user_high_id),
                )
                if active_rows:
                    await conn.execute(
                        "UPDATE vinculo_requests SET status = 'expired', completed_at = ? WHERE id = ?",
                        (now_iso, request_id),
                    )
                    await conn.commit()
                    return "duplicate"

                await conn.execute(
                    """
                    INSERT INTO vinculos(
                        guild_id,
                        user_low_id,
                        user_high_id,
                        bond_type,
                        created_at,
                        active,
                        last_announced_affinity_level
                    )
                    VALUES(?, ?, ?, ?, ?, 1, 1)
                    """,
                    (guild_id, user_low_id, user_high_id, bond_type, now_iso),
                )
                await conn.execute(
                    "UPDATE vinculo_requests SET status = 'accepted', completed_at = ? WHERE id = ?",
                    (now_iso, request_id),
                )
                await conn.commit()
                return "accepted"
            except aiosqlite.IntegrityError:
                await conn.rollback()
                return "duplicate"
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def refuse_request(self, request_id: int, guild_id: int, target_id: int) -> RequestFinishStatus:
        now_iso = _utc_now_iso()
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                rows = await conn.execute_fetchall(
                    "SELECT * FROM vinculo_requests WHERE id = ? AND guild_id = ?",
                    (request_id, guild_id),
                )
                if not rows:
                    await conn.rollback()
                    return "missing"

                request = rows[0]
                if int(request["target_id"]) != target_id:
                    await conn.rollback()
                    return "forbidden"
                if request["status"] != "pending":
                    await conn.rollback()
                    return str(request["status"])  # type: ignore[return-value]

                status = "expired" if str(request["expires_at"]) <= now_iso else "refused"
                await conn.execute(
                    "UPDATE vinculo_requests SET status = ?, completed_at = ? WHERE id = ?",
                    (status, now_iso, request_id),
                )
                await conn.commit()
                return status  # type: ignore[return-value]
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def expire_request(self, request_id: int) -> bool:
        now_iso = _utc_now_iso()
        async with self._tx_lock:
            cur = await self.connection.execute(
                """
                UPDATE vinculo_requests
                SET status = 'expired',
                    completed_at = ?
                WHERE id = ?
                  AND status = 'pending'
                """,
                (now_iso, request_id),
            )
            await self.connection.commit()
            return cur.rowcount > 0

    async def end_vinculo(self, guild_id: int, user_a_id: int, user_b_id: int) -> bool:
        result = await self.break_vinculo(guild_id, user_a_id, user_b_id)
        return result.broken

    async def break_vinculo(self, guild_id: int, breaker_id: int, target_id: int) -> BreakResult:
        user_low_id, user_high_id = _normalize_pair(breaker_id, target_id)
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                rows = await conn.execute_fetchall(
                    """
                    SELECT *
                    FROM vinculos
                    WHERE guild_id = ?
                      AND user_low_id = ?
                      AND user_high_id = ?
                      AND active = 1
                    LIMIT 1
                    """,
                    (guild_id, user_low_id, user_high_id),
                )
                if not rows:
                    await conn.rollback()
                    return BreakResult(broken=False)

                now_iso = _utc_now_iso()
                settings = await self._ensure_guild_settings_in_tx(conn, guild_id)
                vinculo = self._row_to_vinculo(rows[0])
                expires_at = (_utc_now() + timedelta(hours=settings.rupture_penalty_hours)).isoformat()

                await conn.execute(
                    """
                    UPDATE vinculos
                    SET active = 0,
                        ended_at = ?
                    WHERE id = ?
                      AND active = 1
                    """,
                    (now_iso, vinculo.id),
                )
                await conn.execute(
                    """
                    UPDATE vinculo_penalties
                    SET active = 0
                    WHERE guild_id = ?
                      AND user_id = ?
                      AND reason = 'ruptura'
                      AND active = 1
                    """,
                    (guild_id, breaker_id),
                )
                cur = await conn.execute(
                    """
                    INSERT INTO vinculo_penalties(
                        guild_id,
                        user_id,
                        vinculo_id,
                        multiplier_delta,
                        reason,
                        created_at,
                        expires_at,
                        active
                    )
                    VALUES(?, ?, ?, ?, 'ruptura', ?, ?, 1)
                    """,
                    (
                        guild_id,
                        breaker_id,
                        vinculo.id,
                        settings.rupture_penalty_delta,
                        now_iso,
                        expires_at,
                    ),
                )
                await conn.commit()
                penalty = PenaltySnapshot(
                    id=int(cur.lastrowid),
                    guild_id=guild_id,
                    user_id=breaker_id,
                    vinculo_id=vinculo.id,
                    multiplier_delta=settings.rupture_penalty_delta,
                    reason="ruptura",
                    created_at=now_iso,
                    expires_at=expires_at,
                )
                return BreakResult(
                    broken=True,
                    vinculo=vinculo,
                    penalty=penalty,
                    settings=settings,
                )
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def list_active_vinculos_for_user(self, guild_id: int, user_id: int) -> list[ActiveVinculo]:
        rows = await self.connection.execute_fetchall(
            """
            SELECT *
            FROM vinculos
            WHERE guild_id = ?
              AND active = 1
              AND (user_low_id = ? OR user_high_id = ?)
            ORDER BY created_at ASC, id ASC
            """,
            (guild_id, user_id, user_id),
        )
        return [self._row_to_vinculo(row) for row in rows]

    async def get_anniversary_candidates(
        self,
        current_day: int,
        is_last_day_of_month: bool,
        current_year: int,
        current_month: int,
    ) -> list[ActiveVinculo]:
        """Retorna os vínculos ativos que potencialmente celebram aniversário de meses hoje,
        filtrando diretamente no banco de dados para otimização de memória."""
        is_last_day_val = 1 if is_last_day_of_month else 0
        async with self._tx_lock:
            async with self.connection.execute(
                """
                SELECT id, guild_id, user_low_id, user_high_id, bond_type, created_at, ended_at, active, last_announced_affinity_level, is_fused
                FROM vinculos
                WHERE active = 1
                  AND (
                      CAST(strftime('%d', created_at) AS INTEGER) = ?
                      OR
                      (
                          ? = 1
                          AND CAST(strftime('%d', created_at) AS INTEGER) > ?
                      )
                  )
                  AND (
                      (? - CAST(strftime('%Y', created_at) AS INTEGER)) * 12 +
                      (? - CAST(strftime('%m', created_at) AS INTEGER)) > 0
                  )
                """,
                (
                    current_day,
                    is_last_day_val,
                    current_day,
                    current_year,
                    current_month,
                ),
            ) as cursor:
                return [self._row_to_vinculo(row) for row in await cursor.fetchall()]

    async def get_active_vinculo_between(self, guild_id: int, user_a_id: int, user_b_id: int) -> ActiveVinculo | None:
        user_low_id, user_high_id = _normalize_pair(user_a_id, user_b_id)
        rows = await self.connection.execute_fetchall(
            """
            SELECT *
            FROM vinculos
            WHERE guild_id = ?
              AND user_low_id = ?
              AND user_high_id = ?
              AND active = 1
            LIMIT 1
            """,
            (guild_id, user_low_id, user_high_id),
        )
        return self._row_to_vinculo(rows[0]) if rows else None

    async def upsert_presence(
        self,
        *,
        guild_id: int,
        user_id: int,
        message_id: int,
        channel_id: int,
        seen_at: datetime | None = None,
    ) -> None:
        now = (seen_at or _utc_now()).astimezone(timezone.utc).isoformat()
        async with self._tx_lock:
            await self.connection.execute(
                """
                INSERT INTO vinculo_presence(
                    guild_id,
                    user_id,
                    last_seen_at,
                    last_message_id,
                    last_channel_id,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_message_id = excluded.last_message_id,
                    last_channel_id = excluded.last_channel_id,
                    updated_at = excluded.updated_at
                """,
                (guild_id, user_id, now, message_id, channel_id, now),
            )
            await self.connection.commit()

    async def count_active_vinculos(self, guild_id: int, user_id: int) -> int:
        rows = await self.connection.execute_fetchall(
            """
            SELECT COUNT(*) AS total
            FROM vinculos
            WHERE guild_id = ?
              AND active = 1
              AND (user_low_id = ? OR user_high_id = ?)
            """,
            (guild_id, user_id, user_id),
        )
        return int(rows[0]["total"])

    async def get_xp_multiplier(self, guild_id: int, user_id: int) -> float:
        settings = await self.get_guild_settings(guild_id)
        vinculos = await self.list_active_vinculos_for_user(guild_id, user_id)
        multiplier = 1.0

        is_eclipse_active = False
        if settings.eclipse_ends_at:
            try:
                eclipse_end = datetime.fromisoformat(settings.eclipse_ends_at)
                if datetime.now(timezone.utc) < eclipse_end:
                    is_eclipse_active = True
            except ValueError:
                pass

        for vinculo in vinculos:
            affinity = self._affinity_for_created_at(vinculo.created_at, settings)
            partner_id = vinculo.partner_id_for(user_id)
            if partner_id is not None:
                resonance = await self._resonance_for_partner(
                    guild_id,
                    partner_id,
                    settings,
                    window_minutes=XP_RESONANCE_WINDOW_MINUTES,
                )
                if resonance.active:
                    base_bonus = affinity.bonus
                    if is_eclipse_active:
                        base_bonus *= 2.0
                    multiplier += base_bonus

        penalty_delta = await self.get_active_penalty_delta(guild_id, user_id)
        multiplier += penalty_delta
        return max(0.0, multiplier)

    async def count_active_guild_vinculos(self, guild_id: int) -> int:
        rows = await self.connection.execute_fetchall(
            "SELECT COUNT(*) AS total FROM vinculos WHERE guild_id = ? AND active = 1",
            (guild_id,),
        )
        return int(rows[0]["total"])

    async def count_pending_requests(self, guild_id: int) -> int:
        await self.expire_stale_requests()
        rows = await self.connection.execute_fetchall(
            "SELECT COUNT(*) AS total FROM vinculo_requests WHERE guild_id = ? AND status = 'pending'",
            (guild_id,),
        )
        return int(rows[0]["total"])

    # ── Rate-limit: log e contagem de solicitações ──────────────────────

    async def log_vinculo_request(
        self, guild_id: int, requester_id: int, target_id: int, bond_type: str,
    ) -> None:
        """Registra uma tentativa de solicitação de vínculo no log de rate-limit.

        Este registro é absoluto: independente do resultado do pedido
        (aceito, recusado, expirado), a tentativa é contabilizada.
        Utiliza janela rolante de 24 horas para cálculo do limite diário.
        """
        await self.connection.execute(
            """
            INSERT INTO vinculo_request_log(guild_id, requester_id, target_id, bond_type, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (guild_id, requester_id, target_id, bond_type, _utc_now_iso()),
        )
        await self.connection.commit()

    async def count_recent_requests(self, guild_id: int, requester_id: int) -> int:
        """Conta solicitações de vínculo nas últimas 24 horas (janela rolante).

        Retorna quantas solicitações o usuário fez no guild dentro da
        janela de 24h contada a partir do momento atual.
        """
        cutoff = (_utc_now() - timedelta(hours=24)).isoformat()
        rows = await self.connection.execute_fetchall(
            """
            SELECT COUNT(*) AS total
            FROM vinculo_request_log
            WHERE guild_id = ?
              AND requester_id = ?
              AND created_at > ?
            """,
            (guild_id, requester_id, cutoff),
        )
        return int(rows[0]["total"])

    async def update_daily_request_limit(
        self, guild_id: int, limit: int,
    ) -> VinculoGuildSettings:
        """Atualiza o limite diário de solicitações de vínculo para o guild.

        Args:
            guild_id: ID do servidor.
            limit: Novo limite (0 = sem limite / ilimitado).
        """
        return await self._update_settings(guild_id, daily_request_limit=max(0, int(limit)))

    async def get_guild_settings(self, guild_id: int) -> VinculoGuildSettings:
        async with self._tx_lock:
            settings = await self._ensure_guild_settings_in_tx(self.connection, guild_id)
            await self.connection.commit()
            return settings

    async def _get_guild_settings_no_insert(self, guild_id: int) -> VinculoGuildSettings | None:
        rows = await self.connection.execute_fetchall(
            "SELECT * FROM vinculo_guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        return self._row_to_settings(rows[0]) if rows else None

    async def _ensure_guild_settings_in_tx(self, conn: aiosqlite.Connection, guild_id: int) -> VinculoGuildSettings:
        rows = await conn.execute_fetchall(
            "SELECT * FROM vinculo_guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        if rows:
            return self._row_to_settings(rows[0])

        now = _utc_now_iso()
        await conn.execute(
            """
            INSERT INTO vinculo_guild_settings(
                guild_id,
                affinity_level_2_days,
                affinity_level_3_days,
                rupture_penalty_delta,
                rupture_penalty_hours,
                transfer_tax_rate,
                resonance_window_minutes,
                resonance_bonus,
                daily_request_limit,
                created_at,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                DEFAULT_AFFINITY_LEVEL_2_DAYS,
                DEFAULT_AFFINITY_LEVEL_3_DAYS,
                DEFAULT_RUPTURE_PENALTY_DELTA,
                DEFAULT_RUPTURE_PENALTY_HOURS,
                DEFAULT_TRANSFER_TAX_RATE,
                DEFAULT_RESONANCE_WINDOW_MINUTES,
                DEFAULT_RESONANCE_BONUS,
                DEFAULT_DAILY_REQUEST_LIMIT,
                now,
                now,
            ),
        )
        return VinculoGuildSettings(guild_id=guild_id)

    async def set_gossip_channel(self, guild_id: int, channel_id: int | None) -> VinculoGuildSettings:
        return await self._update_settings(guild_id, gossip_channel_id=channel_id)

    async def update_affinity_thresholds(self, guild_id: int, level_2_days: int, level_3_days: int) -> VinculoGuildSettings:
        level_2_days = max(1, int(level_2_days))
        level_3_days = max(level_2_days + 1, int(level_3_days))
        return await self._update_settings(
            guild_id,
            affinity_level_2_days=level_2_days,
            affinity_level_3_days=level_3_days,
        )

    async def update_penalty_settings(self, guild_id: int, penalty_percent: int, hours: int) -> VinculoGuildSettings:
        penalty_delta = -abs(max(0, min(95, int(penalty_percent)))) / 100
        return await self._update_settings(
            guild_id,
            rupture_penalty_delta=penalty_delta,
            rupture_penalty_hours=max(1, int(hours)),
        )

    async def update_transfer_settings(self, guild_id: int, tax_percent: int) -> VinculoGuildSettings:
        return await self._update_settings(
            guild_id,
            transfer_tax_rate=_clamp_tax_rate(max(0, min(95, int(tax_percent))) / 100),
        )

    async def update_resonance_settings(self, guild_id: int, window_minutes: int, bonus_percent: int) -> VinculoGuildSettings:
        return await self._update_settings(
            guild_id,
            resonance_window_minutes=max(1, int(window_minutes)),
            resonance_bonus=max(0, min(95, int(bonus_percent))) / 100,
        )

    async def _update_settings(self, guild_id: int, **fields: object) -> VinculoGuildSettings:
        valid_fields = {
            "gossip_channel_id",
            "affinity_level_2_days",
            "affinity_level_3_days",
            "rupture_penalty_delta",
            "rupture_penalty_hours",
            "transfer_tax_rate",
            "resonance_window_minutes",
            "resonance_bonus",
            "daily_request_limit",
        }
        invalid = set(fields) - valid_fields
        if invalid:
            raise ValueError(f"campos de settings inválidos: {sorted(invalid)}")
        if not fields:
            return await self.get_guild_settings(guild_id)
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_guild_settings_in_tx(conn, guild_id)
                payload = dict(fields)
                assignments = ", ".join(f"{key} = ?" for key in payload)
                values = list(payload.values()) + [_utc_now_iso(), guild_id]
                await conn.execute(
                    f"UPDATE vinculo_guild_settings SET {assignments}, updated_at = ? WHERE guild_id = ?",
                    values,
                )
                rows = await conn.execute_fetchall(
                    "SELECT * FROM vinculo_guild_settings WHERE guild_id = ?",
                    (guild_id,),
                )
                await conn.commit()
                return self._row_to_settings(rows[0])
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def get_active_penalties(self, guild_id: int, user_id: int) -> tuple[PenaltySnapshot, ...]:
        now_iso = _utc_now_iso()
        async with self._tx_lock:
            await self.connection.execute(
                """
                UPDATE vinculo_penalties
                SET active = 0
                WHERE guild_id = ?
                  AND user_id = ?
                  AND active = 1
                  AND expires_at <= ?
                """,
                (guild_id, user_id, now_iso),
            )
            rows = await self.connection.execute_fetchall(
                """
                SELECT *
                FROM vinculo_penalties
                WHERE guild_id = ?
                  AND user_id = ?
                  AND active = 1
                  AND expires_at > ?
                ORDER BY expires_at ASC, id ASC
                """,
                (guild_id, user_id, now_iso),
            )
            await self.connection.commit()
        return self._coalesce_active_penalties(tuple(self._row_to_penalty(row) for row in rows))

    async def get_active_penalty_delta(self, guild_id: int, user_id: int) -> float:
        penalties = await self.get_active_penalties(guild_id, user_id)
        return sum(penalty.multiplier_delta for penalty in penalties)

    def _coalesce_active_penalties(self, penalties: tuple[PenaltySnapshot, ...]) -> tuple[PenaltySnapshot, ...]:
        ruptura_penalties = [penalty for penalty in penalties if penalty.reason == "ruptura"]
        other_penalties = [penalty for penalty in penalties if penalty.reason != "ruptura"]
        if ruptura_penalties:
            strongest_rupture = min(
                ruptura_penalties,
                key=lambda penalty: (penalty.multiplier_delta, penalty.expires_at, penalty.id),
            )
            other_penalties.append(strongest_rupture)
        return tuple(sorted(other_penalties, key=lambda penalty: (penalty.expires_at, penalty.id)))

    async def get_status_snapshot(self, guild_id: int, user_id: int, partner_id: int) -> StatusSnapshot | None:
        vinculo = await self.get_active_vinculo_between(guild_id, user_id, partner_id)
        if vinculo is None:
            return None
        settings = await self.get_guild_settings(guild_id)
        affinity = self._affinity_for_created_at(vinculo.created_at, settings)
        resonance = await self._resonance_for_partner(guild_id, partner_id, settings)
        user_penalties = await self.get_active_penalties(guild_id, user_id)
        partner_penalties = await self.get_active_penalties(guild_id, partner_id)
        transfers = await self.get_transfer_summary(vinculo.id, user_id, partner_id)
        bonus_history = await self.get_bonus_history_summary(vinculo.id)
        return StatusSnapshot(
            vinculo=vinculo,
            settings=settings,
            affinity=affinity,
            resonance=resonance,
            penalties=tuple((*user_penalties, *partner_penalties)),
            transfers=transfers,
            bonus_history=bonus_history,
        )

    async def get_transfer_summary(self, vinculo_id: int, user_id: int, partner_id: int) -> TransferSummary:
        rows = await self.connection.execute_fetchall(
            """
            SELECT
                COALESCE(SUM(CASE WHEN donor_id = ? THEN 1 ELSE 0 END), 0) AS given_count,
                COALESCE(SUM(CASE WHEN donor_id = ? THEN gross_amount ELSE 0 END), 0) AS given_gross,
                COALESCE(SUM(CASE WHEN donor_id = ? THEN net_amount ELSE 0 END), 0) AS given_net,
                COALESCE(SUM(CASE WHEN receiver_id = ? THEN 1 ELSE 0 END), 0) AS received_count,
                COALESCE(SUM(CASE WHEN receiver_id = ? THEN gross_amount ELSE 0 END), 0) AS received_gross,
                COALESCE(SUM(CASE WHEN receiver_id = ? THEN net_amount ELSE 0 END), 0) AS received_net
            FROM vinculo_xp_transfers
            WHERE vinculo_id = ?
            """,
            (user_id, user_id, user_id, user_id, user_id, user_id, vinculo_id),
        )
        summary = rows[0]
        recent_rows = await self.connection.execute_fetchall(
            """
            SELECT donor_id, receiver_id, gross_amount, tax_amount, net_amount, created_at
            FROM vinculo_xp_transfers
            WHERE vinculo_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 3
            """,
            (vinculo_id,),
        )
        recent_lines = tuple(
            (
                f"{_format_datetime(row['created_at'])}: <@{int(row['donor_id'])}> → "
                f"<@{int(row['receiver_id'])}> bruto {int(row['gross_amount'])}, "
                f"taxa {int(row['tax_amount'])}, líquido {int(row['net_amount'])}"
            )
            for row in recent_rows
        )
        return TransferSummary(
            given_count=int(summary["given_count"]),
            given_gross=int(summary["given_gross"]),
            given_net=int(summary["given_net"]),
            received_count=int(summary["received_count"]),
            received_gross=int(summary["received_gross"]),
            received_net=int(summary["received_net"]),
            recent_lines=recent_lines,
        )

    async def get_bonus_history_summary(self, vinculo_id: int) -> BonusHistorySummary:
        rows = await self.connection.execute_fetchall(
            """
            SELECT COALESCE(SUM(bonus_xp), 0) AS total_bonus_xp,
                   COUNT(*) AS event_count
            FROM vinculo_xp_bonus_history
            WHERE vinculo_id = ?
            """,
            (vinculo_id,),
        )
        return BonusHistorySummary(
            total_bonus_xp=int(rows[0]["total_bonus_xp"]),
            event_count=int(rows[0]["event_count"]),
        )

    async def _announce_eclipse(self, bot, guild_id: int, channel_id: int) -> None:
        """Função auxiliar soberba que propaga sensorialmente o evento de Eclipse."""
        try:
            channel = bot.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                embed = discord.Embed(
                    title="🌑 O Eclipse de Baphomet Começou",
                    description=(
                        "A **Essência Global** do altar ultrapassou o limiar catastrófico de **50.000** almas retidas. "
                        "Os céus escurecem e o poder sombrio transborda.\n\n"
                        "> Durante o crepúsculo das próximas **3 HORAS**, toda a ressonância exibe os seus atributos multiplicados por um fator absoluto de **DOIS**.\n"
                        "> A afinidade base de cada pacto e fio será brutalmente duplicada durante as vossas interações.\n\n"
                        "Aproveitem as sombras, mortais. O tempo consome-se a si próprio."
                    ),
                    color=discord.Color.from_str("#1a0000")
                )
                await channel.send(embed=embed)
        except Exception as e:
            log_exception(e)
            LOGGER.error("Falha ao propagar o Eclipse: %s", e)

    async def transfer_xp(self, guild_id: int, donor_id: int, receiver_id: int, amount: int) -> TransferResult:
        if donor_id == receiver_id:
            return TransferResult(status="self_transfer")
        if amount <= 0:
            return TransferResult(status="invalid_amount")

        user_low_id, user_high_id = _normalize_pair(donor_id, receiver_id)
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if not await self._xp_tables_ready(conn):
                    await conn.rollback()
                    return TransferResult(status="xp_unavailable", gross_amount=amount)

                rows = await conn.execute_fetchall(
                    """
                    SELECT *
                    FROM vinculos
                    WHERE guild_id = ?
                      AND user_low_id = ?
                      AND user_high_id = ?
                      AND active = 1
                    LIMIT 1
                    """,
                    (guild_id, user_low_id, user_high_id),
                )
                if not rows:
                    await conn.rollback()
                    return TransferResult(status="missing_vinculo", gross_amount=amount)

                settings = await self._ensure_guild_settings_in_tx(conn, guild_id)
                donor_total = await self._get_xp_total_in_tx(conn, guild_id, donor_id)
                receiver_total = await self._get_xp_total_in_tx(conn, guild_id, receiver_id)
                if donor_total < amount:
                    await conn.rollback()
                    return TransferResult(
                        status="insufficient_funds",
                        gross_amount=amount,
                        donor_balance_before=donor_total,
                    )

                tax_rate = _clamp_tax_rate(settings.transfer_tax_rate)
                tax_amount = min(amount, max(0, int(amount * tax_rate)))
                net_amount = max(0, amount - tax_amount)
                now = _utc_now_iso()
                vinculo = self._row_to_vinculo(rows[0])

                if tax_amount > 0:
                    await conn.execute(
                        "UPDATE vinculo_guild_settings SET global_essence = global_essence + ? WHERE guild_id = ?",
                        (tax_amount, guild_id)
                    )

                    cur_essence_rows = await conn.execute_fetchall(
                        "SELECT global_essence, gossip_channel_id FROM vinculo_guild_settings WHERE guild_id = ?",
                        (guild_id,)
                    )
                    if cur_essence_rows:
                        current_essence = int(cur_essence_rows[0]["global_essence"])
                        gossip_channel_id = cur_essence_rows[0]["gossip_channel_id"]

                        if current_essence >= 50000:
                            eclipse_end = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
                            await conn.execute(
                                "UPDATE vinculo_guild_settings SET global_essence = global_essence - 50000, eclipse_ends_at = ? WHERE guild_id = ?",
                                (eclipse_end, guild_id)
                            )
                            bot = getattr(self, "bot", None)
                            if bot and gossip_channel_id:
                                asyncio.create_task(self._announce_eclipse(bot, guild_id, gossip_channel_id))

                await self._ensure_xp_profile_in_tx(conn, guild_id, donor_id, now)
                await self._ensure_xp_profile_in_tx(conn, guild_id, receiver_id, now)
                await conn.execute(
                    """
                    UPDATE xp_profiles
                    SET total_xp = total_xp - ?,
                        updated_at = ?
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (amount, now, guild_id, donor_id),
                )
                await conn.execute(
                    """
                    UPDATE xp_profiles
                    SET total_xp = total_xp + ?,
                        updated_at = ?
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (net_amount, now, guild_id, receiver_id),
                )
                reason_base = f"vinculo_transfer:{vinculo.id}"
                await conn.execute(
                    """
                    INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, donor_id, donor_id, -amount, f"{reason_base}:out", now),
                )
                await conn.execute(
                    """
                    INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, receiver_id, donor_id, net_amount, f"{reason_base}:in", now),
                )
                if tax_amount:
                    await conn.execute(
                        """
                        INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                        VALUES(?, 0, ?, ?, ?, ?)
                        """,
                        (guild_id, donor_id, -tax_amount, f"{reason_base}:void", now),
                    )
                cur = await conn.execute(
                    """
                    INSERT INTO vinculo_xp_transfers(
                        guild_id,
                        vinculo_id,
                        donor_id,
                        receiver_id,
                        gross_amount,
                        tax_rate,
                        tax_amount,
                        net_amount,
                        created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        vinculo.id,
                        donor_id,
                        receiver_id,
                        amount,
                        tax_rate,
                        tax_amount,
                        net_amount,
                        now,
                    ),
                )
                await conn.commit()
                return TransferResult(
                    status="completed",
                    gross_amount=amount,
                    tax_amount=tax_amount,
                    net_amount=net_amount,
                    tax_rate=tax_rate,
                    donor_balance_before=donor_total,
                    donor_balance_after=donor_total - amount,
                    receiver_balance_before=receiver_total,
                    receiver_balance_after=receiver_total + net_amount,
                    transfer_id=int(cur.lastrowid),
                )
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def mark_due_affinity_announcements(self, guild_id: int, user_id: int) -> list[tuple[ActiveVinculo, AffinitySnapshot]]:
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                settings = await self._ensure_guild_settings_in_tx(conn, guild_id)
                rows = await conn.execute_fetchall(
                    """
                    SELECT *
                    FROM vinculos
                    WHERE guild_id = ?
                      AND active = 1
                      AND (user_low_id = ? OR user_high_id = ?)
                    ORDER BY created_at ASC, id ASC
                    """,
                    (guild_id, user_id, user_id),
                )
                due: list[tuple[ActiveVinculo, AffinitySnapshot]] = []
                for row in rows:
                    vinculo = self._row_to_vinculo(row)
                    affinity = self._affinity_for_created_at(vinculo.created_at, settings)
                    if affinity.level < 2 or affinity.level <= vinculo.last_announced_affinity_level:
                        continue
                    cur = await conn.execute(
                        """
                        UPDATE vinculos
                        SET last_announced_affinity_level = ?
                        WHERE id = ?
                          AND last_announced_affinity_level < ?
                        """,
                        (affinity.level, vinculo.id, affinity.level),
                    )
                    if cur.rowcount > 0:
                        due.append((vinculo, affinity))
                await conn.commit()
                return due
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def _xp_tables_ready(self, conn: aiosqlite.Connection) -> bool:
        rows = await conn.execute_fetchall(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('xp_profiles', 'xp_adjustments')
            """
        )
        return {str(row["name"]) for row in rows} == {"xp_profiles", "xp_adjustments"}

    async def _get_xp_total_in_tx(self, conn: aiosqlite.Connection, guild_id: int, user_id: int) -> int:
        rows = await conn.execute_fetchall(
            "SELECT total_xp FROM xp_profiles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return int(rows[0]["total_xp"]) if rows else 0

    async def _ensure_xp_profile_in_tx(self, conn: aiosqlite.Connection, guild_id: int, user_id: int, now: str) -> None:
        await conn.execute(
            """
            INSERT OR IGNORE INTO xp_profiles(
                guild_id,
                user_id,
                total_xp,
                message_count,
                created_at,
                updated_at
            )
            VALUES(?, ?, 0, 0, ?, ?)
            """,
            (guild_id, user_id, now, now),
        )

    async def _resonance_for_partner(
        self,
        guild_id: int,
        partner_id: int,
        settings: VinculoGuildSettings,
        *,
        window_minutes: int | None = None,
    ) -> ResonanceSnapshot:
        effective_window = max(1, int(window_minutes or settings.resonance_window_minutes))
        rows = await self.connection.execute_fetchall(
            """
            SELECT last_seen_at, last_channel_id
            FROM vinculo_presence
            WHERE guild_id = ?
              AND user_id = ?
            """,
            (guild_id, partner_id),
        )
        if not rows:
            return ResonanceSnapshot(False, None, None, None, effective_window, 0.0)

        seen_at_raw = str(rows[0]["last_seen_at"])
        seen_at = _parse_iso(seen_at_raw)
        if seen_at is None:
            return ResonanceSnapshot(False, seen_at_raw, rows[0]["last_channel_id"], None, effective_window, 0.0)

        seconds_since = int((_utc_now() - seen_at).total_seconds())
        active = seconds_since <= effective_window * 60
        return ResonanceSnapshot(
            active=active,
            partner_last_seen_at=seen_at_raw,
            partner_last_channel_id=int(rows[0]["last_channel_id"]) if rows[0]["last_channel_id"] is not None else None,
            seconds_since_partner_seen=max(0, seconds_since),
            window_minutes=effective_window,
            bonus=settings.resonance_bonus if active else 0.0,
        )

    def _affinity_for_created_at(self, created_at: str, settings: VinculoGuildSettings) -> AffinitySnapshot:
        created = _parse_iso(created_at) or _utc_now()
        now = _utc_now()
        level_2_days = max(1, settings.affinity_level_2_days)
        level_3_days = max(level_2_days + 1, settings.affinity_level_3_days)
        level_2_at = created + timedelta(days=level_2_days)
        level_3_at = created + timedelta(days=level_3_days)

        if now >= level_3_at:
            return AffinitySnapshot(3, _affinity_label(3), _affinity_bonus(3), None, None, None)
        if now >= level_2_at:
            return AffinitySnapshot(
                2,
                _affinity_label(2),
                _affinity_bonus(2),
                3,
                level_3_at,
                int((level_3_at - now).total_seconds()),
            )
        return AffinitySnapshot(
            1,
            _affinity_label(1),
            _affinity_bonus(1),
            2,
            level_2_at,
            int((level_2_at - now).total_seconds()),
        )

    def _row_to_vinculo(self, row: aiosqlite.Row | dict[str, object]) -> ActiveVinculo:
        return ActiveVinculo(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_low_id=int(row["user_low_id"]),
            user_high_id=int(row["user_high_id"]),
            bond_type=_normalize_vinculo_type(row["bond_type"]),
            created_at=str(row["created_at"]),
            ended_at=str(row["ended_at"]) if row["ended_at"] is not None else None,
            active=bool(row["active"]),
            last_announced_affinity_level=int(row["last_announced_affinity_level"]),
            is_fused=bool(row["is_fused"]) if "is_fused" in row.keys() else False,
        )

    def _row_to_settings(self, row: aiosqlite.Row) -> VinculoGuildSettings:
        level_2_days = max(1, int(row["affinity_level_2_days"]))
        level_3_days = max(level_2_days + 1, int(row["affinity_level_3_days"]))
        return VinculoGuildSettings(
            guild_id=int(row["guild_id"]),
            gossip_channel_id=int(row["gossip_channel_id"]) if row["gossip_channel_id"] is not None else None,
            affinity_level_2_days=level_2_days,
            affinity_level_3_days=level_3_days,
            rupture_penalty_delta=float(row["rupture_penalty_delta"]),
            rupture_penalty_hours=max(1, int(row["rupture_penalty_hours"])),
            transfer_tax_rate=_clamp_tax_rate(float(row["transfer_tax_rate"])),
            resonance_window_minutes=max(1, int(row["resonance_window_minutes"])),
            resonance_bonus=max(0.0, float(row["resonance_bonus"])),
            daily_request_limit=max(0, int(row["daily_request_limit"])),
            global_essence=int(row["global_essence"]) if "global_essence" in row.keys() else 0,
            eclipse_ends_at=str(row["eclipse_ends_at"]) if "eclipse_ends_at" in row.keys() and row["eclipse_ends_at"] is not None else None,
        )

    def _row_to_penalty(self, row: aiosqlite.Row) -> PenaltySnapshot:
        return PenaltySnapshot(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            vinculo_id=int(row["vinculo_id"]) if row["vinculo_id"] is not None else None,
            multiplier_delta=float(row["multiplier_delta"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
        )


class VinculoRequestView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "VinculosCog",
        request_id: int,
        guild_id: int,
        requester_id: int,
        target_id: int,
        bond_type: VinculoType,
        common_role_ids: list[int],
        timeout: float,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.request_id = request_id
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.target_id = target_id
        self.bond_type = bond_type
        self.common_role_ids = common_role_ids
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.target_id:
            return True
        await interaction.response.send_message(
            "👁️ Marionete intrusa... você não tem permissão para interferir neste pacto.",
            ephemeral=True,
        )
        return False

    def _disable_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _edit_with_state(self, interaction: discord.Interaction, state: str) -> None:
        self._disable_buttons()
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        embed = self.cog.build_request_embed(
            guild=guild,
            requester_id=self.requester_id,
            target_id=self.target_id,
            bond_type=self.bond_type,
            common_role_ids=self.common_role_ids,
            state=state,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="Aceitar vínculo", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            status = await self.cog.repository.accept_request(self.request_id, self.guild_id, interaction.user.id)
            if status == "accepted":
                await self._edit_with_state(interaction, "accepted")
                await self.cog.announce_vinculo_accepted(
                    guild=interaction.guild or self.cog.bot.get_guild(self.guild_id),
                    requester_id=self.requester_id,
                    target_id=self.target_id,
                    bond_type=self.bond_type,
                )
                return
            if status == "duplicate":
                await interaction.response.send_message(
                    "🔗 Este fio já foi amarrado, criatura efêmera. O pacto duplicado foi consumido pelo abismo.",
                    ephemeral=True,
                )
                await self._edit_message_after_followup("duplicate")
                return
            if status == "expired":
                await self._edit_with_state(interaction, "expired")
                return
            await interaction.response.send_message(
                "⚰️ Este pacto já não está pendente. O destino se moveu sem pedir licença.",
                ephemeral=True,
            )
            await self._edit_message_after_followup("stale")
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("falha ao aceitar vínculo request_id=%s", self.request_id)
            await self.cog.send_interaction_error(interaction)

    @discord.ui.button(label="Recusar vínculo", style=discord.ButtonStyle.danger)
    async def refuse_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            status = await self.cog.repository.refuse_request(self.request_id, self.guild_id, interaction.user.id)
            if status == "refused":
                await self._edit_with_state(interaction, "refused")
                return
            if status == "expired":
                await self._edit_with_state(interaction, "expired")
                return
            await interaction.response.send_message(
                "⚰️ Este pacto já foi decidido. Nem mesmo Baphomet costura o mesmo silêncio duas vezes.",
                ephemeral=True,
            )
            await self._edit_message_after_followup("stale")
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("falha ao recusar vínculo request_id=%s", self.request_id)
            await self.cog.send_interaction_error(interaction)

    async def _edit_message_after_followup(self, state: str) -> None:
        self._disable_buttons()
        guild = self.cog.bot.get_guild(self.guild_id)
        embed = self.cog.build_request_embed(
            guild=guild,
            requester_id=self.requester_id,
            target_id=self.target_id,
            bond_type=self.bond_type,
            common_role_ids=self.common_role_ids,
            state=state,
        )
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(embed=embed, view=self)
        self.stop()

    async def on_timeout(self) -> None:
        try:
            expired = await self.cog.repository.expire_request(self.request_id)
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("falha ao expirar vínculo request_id=%s", self.request_id)
            return
        if not expired:
            return
        self._disable_buttons()
        guild = self.cog.bot.get_guild(self.guild_id)
        embed = self.cog.build_request_embed(
            guild=guild,
            requester_id=self.requester_id,
            target_id=self.target_id,
            bond_type=self.bond_type,
            common_role_ids=self.common_role_ids,
            state="expired",
        )
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(embed=embed, view=self)
        self.stop()


class VinculosCog(commands.Cog):
    vinculo = app_commands.Group(name="vinculo", description="Pactos, fios e bônus de XP entre usuários.")

    def __init__(self, bot: commands.Bot, repository: VinculoRepository) -> None:
        self.bot = bot
        self.repository = repository
        self.vinculo_card_renderer = VinculoCardRenderer()
        self._request_cooldowns: dict[tuple[int, int], datetime] = {}
        self._last_activity_write: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self.repository.bot = self.bot
        await self.repository.connect()
        self.bot.vinculos_runtime = VinculosRuntime(repository=self.repository)
        xp_service = getattr(self.bot, "xp_service", None)
        if xp_service is not None:
            xp_service.vinculos_provider = self.repository
        self.check_vinculo_anniversaries.start()

    def cog_unload(self) -> None:
        self.check_vinculo_anniversaries.cancel()
        xp_service = getattr(self.bot, "xp_service", None)
        if xp_service is not None and getattr(xp_service, "vinculos_provider", None) is self.repository:
            xp_service.vinculos_provider = None
        if getattr(self.bot, "vinculos_runtime", None) is not None:
            self.bot.vinculos_runtime = None
        self.bot.loop.create_task(self.repository.close())

    @tasks.loop(time=dt_time(hour=15, minute=0, tzinfo=timezone.utc))
    async def check_vinculo_anniversaries(self) -> None:
        """Executa todos os dias às 12:00 BRT para anunciar aniversários de meses/anos de vínculo."""
        now = datetime.now(timezone.utc)
        import calendar
        _, last_day_of_month = calendar.monthrange(now.year, now.month)
        is_last_day = (now.day == last_day_of_month)

        try:
            candidates = await self.repository.get_anniversary_candidates(
                current_day=now.day,
                is_last_day_of_month=is_last_day,
                current_year=now.year,
                current_month=now.month,
            )
        except Exception as e:
            log_exception(e)
            LOGGER.error("Erro ao buscar candidatos de aniversário de vínculos: %s", e)
            return

        for vinculo in candidates:
            try:
                created_at = datetime.fromisoformat(vinculo.created_at)
                months_diff = (now.year - created_at.year) * 12 + now.month - created_at.month
                if months_diff <= 0:
                    continue

                # Garantia adicional de que o dia é correspondente ao aniversário
                is_anniversary_day = False
                if now.day == created_at.day:
                    is_anniversary_day = True
                elif now.day == last_day_of_month and created_at.day > last_day_of_month:
                    is_anniversary_day = True

                if not is_anniversary_day:
                    continue

                guild = self.bot.get_guild(vinculo.guild_id)
                if guild is None:
                    continue

                settings = await self.repository.get_guild_settings(guild.id)
                gossip_channel_id = settings.gossip_channel_id
                if not gossip_channel_id:
                    LOGGER.info("Guilda %s não possui canal de fofocas configurado para aniversários.", guild.id)
                    continue

                channel = guild.get_channel(gossip_channel_id)
                if channel is None:
                    try:
                        channel = await guild.fetch_channel(gossip_channel_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                        LOGGER.warning(
                            "Não foi possível carregar o canal de fofocas %s da guilda %s para aniversário: %s",
                            gossip_channel_id,
                            guild.id,
                            e,
                        )
                        continue

                if not isinstance(channel, discord.TextChannel):
                    LOGGER.warning(
                        "Canal de fofocas %s da guilda %s não é um canal de texto válido.",
                        gossip_channel_id,
                        guild.id,
                    )
                    continue

                # Verificar permissões do bot no canal
                permissions = channel.permissions_for(guild.me)
                if not (permissions.send_messages and permissions.embed_links and permissions.attach_files):
                    LOGGER.warning(
                        "Bot sem permissões suficientes (enviar mensagens, links ou arquivos) no canal de fofocas %s da guilda %s",
                        gossip_channel_id,
                        guild.id,
                    )
                    continue

                # Obter membros do vínculo
                try:
                    user_a = await guild.fetch_member(vinculo.user_low_id)
                    user_b = await guild.fetch_member(vinculo.user_high_id)
                except discord.NotFound:
                    LOGGER.info(
                        "Vínculo %s ignorado no aniversário: um dos membros (low: %s, high: %s) saiu do servidor.",
                        vinculo.id,
                        vinculo.user_low_id,
                        vinculo.user_high_id,
                    )
                    continue
                except discord.HTTPException as e:
                    LOGGER.error("Erro ao buscar membros para aniversário do vínculo %s: %s", vinculo.id, e)
                    continue

                # Calcular a duração formatada de forma esteticamente agradável
                years = months_diff // 12
                months = months_diff % 12
                if years > 0 and months > 0:
                    time_text = f"{years} Ano{'s' if years > 1 else ''} e {months} Mê{'s' if months == 1 else 'ses'}!"
                elif years > 0:
                    time_text = f"{years} Ano{'s' if years > 1 else ''}!"
                else:
                    time_text = f"{months_diff} Mê{'s' if months_diff == 1 else 'ses'}!"

                metadata = _vinculo_metadata(vinculo.bond_type)

                # Gerar a imagem usando a nova renderização premium de aniversário
                fp = await self.vinculo_card_renderer.render_vinculo_anniversary_card(
                    participant_a=user_a,
                    participant_b=user_b,
                    accent=metadata.render_accent,
                    time_text=time_text,
                    fallback_name_a=user_a.display_name,
                    fallback_name_b=user_b.display_name,
                    is_fused=vinculo.is_fused,
                )

                file = discord.File(fp=fp, filename="vinculo_aniversario.png")

                embed = discord.Embed(
                    title="🎉 Aniversário de Vínculo! 🎉",
                    description=f"{user_a.mention} e {user_b.mention} estão comemorando **{time_text}** de pacto firmado!",
                    color=discord.Color(metadata.color),
                )
                embed.add_field(name="Tipo do Altar", value=f"{metadata.emoji} **{metadata.label}**", inline=True)
                embed.set_image(url="attachment://vinculo_aniversario.png")

                await channel.send(content=f"{user_a.mention} {user_b.mention}", embed=embed, file=file)

            except Exception as e:
                log_exception(e)
                LOGGER.error("Erro inesperado ao processar aniversário para vínculo %s: %s", vinculo.id, e)

    @check_vinculo_anniversaries.before_loop
    async def before_check_vinculo_anniversaries(self) -> None:
        await self.bot.wait_until_ready()

    async def send_interaction_error(self, interaction: discord.Interaction) -> None:
        message = "🩸 Algo rangeu no mecanismo do altar. Tente novamente quando o sangue assentar."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOGGER.warning("não foi possível responder erro de vínculo ao usuário")

    async def _resolve_member_for_role_check(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        fallback: discord.Member | None = None,
    ) -> discord.Member | None:
        try:
            return await guild.fetch_member(int(user_id))
        except discord.NotFound:
            LOGGER.info("membro não encontrado para contagem de interesses guild_id=%s user_id=%s", guild.id, user_id)
            return None
        except discord.Forbidden:
            LOGGER.warning(
                "sem permissão para atualizar membro antes da contagem de interesses guild_id=%s user_id=%s",
                guild.id,
                user_id,
            )
        except discord.HTTPException:
            LOGGER.warning(
                "falha ao buscar membro atualizado antes da contagem de interesses guild_id=%s user_id=%s",
                guild.id,
                user_id,
                exc_info=True,
            )

        fallback_member = fallback if fallback is not None and fallback.guild.id == guild.id else None
        cached = guild.get_member(int(user_id))
        if fallback_member is not None and cached is not None:
            fallback_role_count = len(self._member_role_ids(fallback_member))
            cached_role_count = len(self._member_role_ids(cached))
            return fallback_member if fallback_role_count >= cached_role_count else cached
        if fallback_member is not None:
            return fallback_member
        if cached is not None:
            return cached
        return None

    def _format_role_ids(self, guild: discord.Guild | None, role_ids: list[int]) -> str:
        if not role_ids:
            return "Nenhum"
        lines: list[str] = []
        for role_id in role_ids:
            role = guild.get_role(role_id) if guild is not None else None
            lines.append(role.mention if role is not None else f"Cargo ausente (`{role_id}`)")
        return _clip_embed_value("\n".join(lines))

    @staticmethod
    def _normalize_role_id_set(role_ids: Iterable[object]) -> set[int]:
        normalized: set[int] = set()
        for raw_role_id in role_ids:
            try:
                role_id = int(raw_role_id)
            except (TypeError, ValueError):
                LOGGER.warning("id de cargo de interesse inválido ignorado role_id=%r", raw_role_id)
                continue
            if role_id > 0:
                normalized.add(role_id)
        return normalized

    @staticmethod
    def _member_role_ids(member: discord.Member) -> set[int]:
        role_ids: set[int] = set()
        for raw_role_id in getattr(member, "_roles", ()):
            try:
                role_ids.add(int(raw_role_id))
            except (TypeError, ValueError):
                LOGGER.warning(
                    "id cru de cargo inválido ignorado na contagem de vínculo user_id=%s role_id=%r",
                    getattr(member, "id", None),
                    raw_role_id,
                )
        for role in getattr(member, "roles", ()):
            raw_role_id = getattr(role, "id", None)
            if raw_role_id is None:
                continue
            try:
                role_ids.add(int(raw_role_id))
            except (TypeError, ValueError):
                LOGGER.warning(
                    "id de cargo inválido ignorado na contagem de vínculo user_id=%s role_id=%r",
                    getattr(member, "id", None),
                    raw_role_id,
                )
        return role_ids

    def _partition_configured_interest_role_ids(
        self,
        guild: discord.Guild,
        configured_role_ids: Iterable[object],
        held_role_ids: set[int] | None = None,
    ) -> tuple[set[int], set[int], set[int], set[int]]:
        configured_ids = self._normalize_role_id_set(configured_role_ids)
        held_role_ids = held_role_ids or set()
        existing_interest_ids: set[int] = set()
        missing_ids: set[int] = set()
        default_ids: set[int] = set()

        for role_id in configured_ids:
            if role_id == getattr(guild, "id", None):
                default_ids.add(role_id)
                continue
            role = guild.get_role(role_id)
            if role is None:
                if role_id in held_role_ids:
                    existing_interest_ids.add(role_id)
                else:
                    missing_ids.add(role_id)
                continue
            if role.is_default():
                default_ids.add(role_id)
                continue
            existing_interest_ids.add(role_id)

        return configured_ids, existing_interest_ids, missing_ids, default_ids

    @staticmethod
    def _sorted_role_ids_for_display(guild: discord.Guild, role_ids: Iterable[int]) -> tuple[int, ...]:
        def sort_key(role_id: int) -> tuple[int, int]:
            role = guild.get_role(role_id)
            return (role.position if role is not None else -1, role_id)

        return tuple(
            sorted(
                role_ids,
                key=sort_key,
                reverse=True,
            )
        )

    def _build_interest_role_match(
        self,
        *,
        configured_role_ids: Iterable[object],
        requester: discord.Member,
        target: discord.Member,
    ) -> InterestRoleMatch:
        guild = requester.guild
        requester_role_ids = self._member_role_ids(requester)
        target_role_ids = self._member_role_ids(target)
        configured_ids, existing_interest_ids, missing_ids, default_ids = self._partition_configured_interest_role_ids(
            guild,
            configured_role_ids,
            held_role_ids=requester_role_ids | target_role_ids,
        )
        requester_interest_ids = existing_interest_ids & requester_role_ids
        target_interest_ids = existing_interest_ids & target_role_ids
        common_ids = requester_interest_ids & target_interest_ids
        shared_non_interest_ids = (requester_role_ids & target_role_ids) - existing_interest_ids

        return InterestRoleMatch(
            common_role_ids=self._sorted_role_ids_for_display(guild, common_ids),
            configured_interest_role_ids=tuple(sorted(configured_ids)),
            existing_interest_role_ids=tuple(sorted(existing_interest_ids)),
            missing_configured_role_ids=tuple(sorted(missing_ids)),
            ignored_default_role_ids=tuple(sorted(default_ids)),
            requester_interest_role_ids=tuple(sorted(requester_interest_ids)),
            target_interest_role_ids=tuple(sorted(target_interest_ids)),
            shared_non_interest_role_ids=tuple(sorted(shared_non_interest_ids)),
        )

    def _common_interest_role_ids(
        self,
        *,
        configured_role_ids: Iterable[object],
        requester: discord.Member,
        target: discord.Member,
    ) -> list[int]:
        match = self._build_interest_role_match(
            configured_role_ids=configured_role_ids,
            requester=requester,
            target=target,
        )
        return list(match.common_role_ids)

    def _log_interest_gate_diagnostics(
        self,
        *,
        guild_id: int,
        requester_id: int,
        target_id: int,
        match: InterestRoleMatch,
        blocked: bool,
    ) -> None:
        LOGGER.log(
            logging.INFO if blocked else logging.DEBUG,
            (
                "vinculo interest gate %s guild_id=%s requester_id=%s target_id=%s "
                "min_required=%s common_count=%s common_interest_role_ids=%s "
                "configured_interest_role_ids=%s existing_interest_role_ids=%s "
                "missing_configured_role_ids=%s ignored_default_role_ids=%s "
                "requester_interest_role_ids=%s target_interest_role_ids=%s "
                "shared_non_interest_role_ids=%s"
            ),
            "blocked" if blocked else "passed",
            guild_id,
            requester_id,
            target_id,
            MIN_COMMON_INTEREST_ROLES,
            len(match.common_role_ids),
            list(match.common_role_ids),
            list(match.configured_interest_role_ids),
            list(match.existing_interest_role_ids),
            list(match.missing_configured_role_ids),
            list(match.ignored_default_role_ids),
            list(match.requester_interest_role_ids),
            list(match.target_interest_role_ids),
            list(match.shared_non_interest_role_ids),
        )

    def _check_request_cooldown(self, guild_id: int, user_id: int) -> int | None:
        key = (guild_id, user_id)
        until = self._request_cooldowns.get(key)
        now = _utc_now()
        if until is None or until <= now:
            self._request_cooldowns.pop(key, None)
            return None
        return max(1, int((until - now).total_seconds()) + 1)

    def _start_request_cooldown(self, guild_id: int, user_id: int) -> None:
        self._request_cooldowns[(guild_id, user_id)] = _utc_now() + timedelta(seconds=REQUEST_COOLDOWN_SECONDS)

    def build_request_embed(
        self,
        *,
        guild: discord.Guild | None,
        requester_id: int,
        target_id: int,
        bond_type: VinculoType,
        common_role_ids: list[int],
        state: str,
    ) -> discord.Embed:
        requester = f"<@{requester_id}>"
        target = f"<@{target_id}>"
        interests = self._format_role_ids(guild, common_role_ids)
        metadata = _vinculo_metadata(bond_type)

        if state == "pending":
            embed = discord.Embed(
                title=metadata.pending_title,
                description=metadata.pending_description.format(requester=requester, target=target),
                color=discord.Color(metadata.color),
            )
            embed.add_field(name="Tipo do pacto", value=f"{metadata.emoji} **{metadata.label}**", inline=False)
            embed.add_field(name="Interesses em comum", value=interests, inline=False)
            embed.add_field(name="Expiração", value=f"{REQUEST_TIMEOUT_SECONDS} segundos", inline=True)
            embed.set_footer(text="Apenas o alvo do pacto pode aceitar ou recusar.")
            return embed

        states = {
            "accepted": (
                metadata.accepted_title,
                metadata.accepted_description.format(requester=requester, target=target),
                discord.Color(metadata.color),
            ),
            "refused": (
                "⚰️ Pacto recusado",
                "O pacto foi recusado. Nem toda alma aceita outra sombra ao lado.",
                VINCULO_WARNING_COLOR,
            ),
            "expired": (
                "⌛ Pedido expirado",
                "O fio apodreceu antes de ser amarrado. O pedido de vínculo expirou.",
                VINCULO_WARNING_COLOR,
            ),
            "duplicate": (
                "🔗 Fio já existente",
                "Este pacto tentou nascer duas vezes, mas o abismo só reconhece um fio ativo.",
                VINCULO_WARNING_COLOR,
            ),
            "stale": (
                "📜 Pacto arquivado",
                "Este pedido já foi decidido. O altar encerrou a cerimônia.",
                VINCULO_WARNING_COLOR,
            ),
        }
        title, description, color = states.get(state, states["stale"])
        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="Tipo do pacto", value=f"{metadata.emoji} **{metadata.label}**", inline=False)
        embed.add_field(name="Interesses que cruzaram os fios", value=interests, inline=False)
        return embed

    async def _send_embed(self, interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = True) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _send_text(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = True) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    @vinculo.command(name="criar", description="Oferece um vínculo a outro usuário.")
    @app_commands.guild_only()
    @app_commands.describe(usuario="Usuário que receberá o pedido de vínculo", tipo="Tipo narrativo do vínculo")
    @app_commands.choices(tipo=BOND_TYPE_CHOICES)
    async def criar(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        tipo: app_commands.Choice[str],
    ) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return

        requester_id = int(interaction.user.id)
        target = usuario
        bond_type = _normalize_vinculo_type(tipo.value)
        if requester_id == target.id:
            await self._send_text(
                interaction,
                "🎭 Herege curioso... tentar selar um pacto consigo mesmo é apenas solidão com efeitos especiais.",
            )
            return
        if target.bot:
            await self._send_text(interaction, "🩸 Bonecos de lata não assinam pactos de sangue, criatura.")
            return
        if target.guild.id != interaction.guild.id:
            await self._send_text(interaction, "👁️ Essa alma não pertence a este palco. O vínculo não pode ser iniciado.")
            return

        retry_after = self._check_request_cooldown(interaction.guild.id, requester_id)
        if retry_after is not None:
            await self._send_text(
                interaction,
                f"⌛ Respire, cultista. Aguarde **{retry_after}s** antes de oferecer outro pacto.",
            )
            return

        # ── Rate-limit diário de solicitações (janela rolante de 24h) ──
        settings = await self.repository.get_guild_settings(interaction.guild.id)
        daily_limit = settings.daily_request_limit
        if daily_limit > 0:  # 0 = sem limite
            used_today = await self.repository.count_recent_requests(
                interaction.guild.id, requester_id,
            )
            if used_today >= daily_limit:
                reset_approx = _utc_now() + timedelta(hours=1)
                reset_ts = int(reset_approx.timestamp())
                await self._send_text(
                    interaction,
                    (
                        f"🕯️ O altar se recusa a aceitar mais ofertas hoje. "
                        f"Você já fez **{used_today}/{daily_limit}** solicitação(ões) nas últimas 24 horas.\n"
                        f"Tente novamente mais tarde — o próximo slot será liberado aproximadamente <t:{reset_ts}:R>."
                    ),
                )
                return

        configured_role_ids = await self.repository.list_interest_role_ids(interaction.guild.id)
        if not configured_role_ids:
            await self._send_text(
                interaction,
                "📜 Nenhum interesse foi inscrito no grimório deste servidor. Um administrador precisa configurar os cargos primeiro.",
            )
            return

        requester = await self._resolve_member_for_role_check(
            interaction.guild,
            requester_id,
            fallback=interaction.user if isinstance(interaction.user, discord.Member) else None,
        )
        target = await self._resolve_member_for_role_check(interaction.guild, target.id, fallback=target)
        if requester is None or target is None:
            await self._send_text(
                interaction,
                "👁️ Não consegui confirmar os cargos de uma das almas. Tente novamente em alguns instantes.",
            )
            return

        interest_match = self._build_interest_role_match(
            configured_role_ids=configured_role_ids,
            requester=requester,
            target=target,
        )
        common_role_ids = list(interest_match.common_role_ids)
        if len(common_role_ids) < MIN_COMMON_INTEREST_ROLES:
            self._log_interest_gate_diagnostics(
                guild_id=interaction.guild.id,
                requester_id=requester.id,
                target_id=target.id,
                match=interest_match,
                blocked=True,
            )
            await self._send_text(
                interaction,
                (
                    "🧵 Criatura efêmera, os fios entre vocês ainda são fracos demais. "
                    f"Vocês precisam ter pelo menos **{MIN_COMMON_INTEREST_ROLES}** interesse(s) em comum para selar um vínculo."
                ),
            )
            return
        self._log_interest_gate_diagnostics(
            guild_id=interaction.guild.id,
            requester_id=requester.id,
            target_id=target.id,
            match=interest_match,
            blocked=False,
        )

        creation = await self.repository.create_request(
            guild_id=interaction.guild.id,
            requester_id=requester.id,
            target_id=target.id,
            bond_type=bond_type,
            expires_at=_utc_now() + timedelta(seconds=REQUEST_TIMEOUT_SECONDS),
        )
        if creation.status == "active_exists":
            await self._send_text(interaction, "🔗 Este vínculo já está ativo. O abismo não duplica destinos.")
            return
        if creation.status == "pending_exists" or creation.request_id is None:
            await self._send_text(
                interaction,
                "🕯️ Já existe um pedido pendente entre vocês. Aguarde o fio ser aceito, recusado ou apodrecer.",
            )
            return

        # Registra a tentativa no log de rate-limit (contabilização absoluta).
        await self.repository.log_vinculo_request(
            interaction.guild.id,
            requester.id,
            target.id,
            bond_type.value,
        )
        self._start_request_cooldown(interaction.guild.id, requester.id)
        view = VinculoRequestView(
            cog=self,
            request_id=creation.request_id,
            guild_id=interaction.guild.id,
            requester_id=requester.id,
            target_id=target.id,
            bond_type=bond_type,
            common_role_ids=common_role_ids,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        embed = self.build_request_embed(
            guild=interaction.guild,
            requester_id=requester.id,
            target_id=target.id,
            bond_type=bond_type,
            common_role_ids=common_role_ids,
            state="pending",
        )
        await interaction.response.send_message(content=target.mention, embed=embed, view=view)
        view.message = await interaction.original_response()

    @vinculo.command(name="encerrar", description="Corta um vínculo ativo com outro usuário.")
    @app_commands.guild_only()
    @app_commands.describe(usuario="Usuário cujo vínculo será encerrado")
    async def encerrar(self, interaction: discord.Interaction, usuario: discord.Member) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        if interaction.user.id == usuario.id:
            await self._send_text(interaction, "🧵 Cortar o próprio fio não encerra um vínculo, apenas faz cena.")
            return

        result = await self.repository.break_vinculo(interaction.guild.id, interaction.user.id, usuario.id)
        if not result.broken:
            await self._send_text(interaction, "👁️ Nenhum vínculo ativo foi encontrado entre vocês. O altar não corta o que não existe.")
            return

        requester_multiplier = await self.repository.get_xp_multiplier(interaction.guild.id, interaction.user.id)
        target_multiplier = await self.repository.get_xp_multiplier(interaction.guild.id, usuario.id)
        penalty = result.penalty
        penalty_text = (
            f"{penalty.multiplier_delta:.2f}x até {_format_datetime(penalty.expires_at)}"
            if penalty is not None
            else "sem maldição registrada"
        )
        embed = discord.Embed(
            title="🩸 Vínculo encerrado",
            description="O fio foi cortado. O vínculo não existe mais, e o abismo recolheu seu bônus.",
            color=VINCULO_SUCCESS_COLOR,
        )
        embed.add_field(
            name="Multiplicadores atuais",
            value=(
                f"{interaction.user.mention}: **{requester_multiplier:.1f}x**\n"
                f"{usuario.mention}: **{target_multiplier:.1f}x**"
            ),
            inline=False,
        )
        embed.add_field(name="Maldição aplicada", value=f"{interaction.user.mention}: **{penalty_text}**", inline=False)
        await self._send_embed(interaction, embed)
        await self.announce_vinculo_broken(
            guild=interaction.guild,
            breaker_id=interaction.user.id,
            target_id=usuario.id,
            result=result,
        )

    @vinculo.command(name="status", description="Mostra o estado detalhado do pacto com outro usuário.")
    @app_commands.guild_only()
    @app_commands.describe(usuario="Parceiro cujo pacto você quer inspecionar")
    async def status(self, interaction: discord.Interaction, usuario: discord.Member) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        if interaction.user.id == usuario.id:
            await self._send_text(interaction, "🪞 O altar não chama autoanálise de vínculo.")
            return

        snapshot = await self.repository.get_status_snapshot(interaction.guild.id, interaction.user.id, usuario.id)
        if snapshot is None:
            await self._send_text(
                interaction,
                "👁️ Nenhum vínculo ativo foi encontrado entre vocês. Um pedido pendente ainda precisa ser aceito para virar pacto.",
            )
            return

        metadata = _vinculo_metadata(snapshot.vinculo.bond_type)
        embed = discord.Embed(
            title=f"{metadata.emoji} Status do pacto",
            color=discord.Color(metadata.color),
        )
        embed.add_field(name="Tipo", value=f"**{metadata.label}**", inline=True)
        embed.add_field(name="Criado em", value=_format_datetime(snapshot.vinculo.created_at), inline=True)
        embed.add_field(
            name="Afinidade",
            value=(
                f"Nível **{snapshot.affinity.level}** — **{snapshot.affinity.label}**\n"
                f"Bônus: **+{snapshot.affinity.bonus:.0%}**\n"
                f"Próximo: **{_format_duration(snapshot.affinity.seconds_until_next)}**"
            ),
            inline=False,
        )

        resonance = snapshot.resonance
        channel_hint = f" em <#{resonance.partner_last_channel_id}>" if resonance.partner_last_channel_id else ""
        eligible_bonus = snapshot.affinity.bonus if resonance.active else 0.0
        if resonance.partner_last_seen_at:
            resonance_value = (
                f"{'Ativa' if resonance.active else 'Adormecida'}: parceiro visto há "
                f"**{_format_duration(resonance.seconds_since_partner_seen)}**{channel_hint}.\n"
                f"Janela: **{resonance.window_minutes}min** | Afinidade elegível: **+{eligible_bonus:.0%}**"
            )
        else:
            resonance_value = f"Sem presença recente registrada. Janela: **{resonance.window_minutes}min**."
        embed.add_field(name="Ressonância", value=resonance_value, inline=False)

        if snapshot.penalties:
            penalty_lines = [
                f"<@{penalty.user_id}>: **{penalty.multiplier_delta:.2f}x** até {_format_datetime(penalty.expires_at)}"
                for penalty in snapshot.penalties
            ]
            embed.add_field(name="Maldições ativas", value=_clip_embed_value("\n".join(penalty_lines)), inline=False)
        else:
            embed.add_field(name="Maldições ativas", value="Nenhuma.", inline=False)

        embed.add_field(
            name="XP de vínculo",
            value=(
                f"Estimativa atual deste pacto: **+{eligible_bonus:.0%}** por ganho elegível.\n"
                f"Bônus registrado: **{snapshot.bonus_history.total_bonus_xp} XP** "
                f"em **{snapshot.bonus_history.event_count}** evento(s).\n"
                "A trilha fina está preparada para integração direta com o ganho de XP."
            ),
            inline=False,
        )
        transfer_value = (
            f"Você doou: **{snapshot.transfers.given_gross} XP bruto** "
            f"(**{snapshot.transfers.given_net} XP líquido** chegaram).\n"
            f"Você recebeu: **{snapshot.transfers.received_net} XP líquido** "
            f"de **{snapshot.transfers.received_gross} XP bruto**."
        )
        if snapshot.transfers.recent_lines:
            transfer_value += "\n" + "\n".join(snapshot.transfers.recent_lines)
        embed.add_field(name="Doações", value=_clip_embed_value(transfer_value), inline=False)
        await self._send_embed(interaction, embed)

    @vinculo.command(name="doar-xp", description="Doa XP ao parceiro de vínculo com taxa ritual.")
    @app_commands.guild_only()
    @app_commands.describe(usuario="Parceiro que receberá o XP", quantidade="Quantidade bruta retirada do doador")
    async def doar_xp(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        quantidade: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        if interaction.user.id == usuario.id:
            await self._send_text(interaction, "🪙 Auto-transferência só alimenta a vaidade, não o altar.")
            return
        if usuario.bot:
            await self._send_text(interaction, "🤖 Bonecos de lata não recebem doações de XP.")
            return

        result = await self.repository.transfer_xp(
            interaction.guild.id,
            interaction.user.id,
            usuario.id,
            int(quantidade),
        )
        if result.status == "missing_vinculo":
            await self._send_text(interaction, "👁️ Só é possível doar XP para alguém com vínculo ativo com você.")
            return
        if result.status == "insufficient_funds":
            await self._send_text(
                interaction,
                f"⚠️ Saldo insuficiente. Você tem **{result.donor_balance_before} XP** disponível.",
            )
            return
        if result.status == "xp_unavailable":
            await self._send_text(interaction, "⚠️ O altar de XP não está disponível para registrar a doação agora.")
            return
        if result.status != "completed":
            await self._send_text(interaction, "⚠️ A doação não pôde ser concluída.")
            return

        embed = discord.Embed(
            title="🪙 Doação de XP concluída",
            color=VINCULO_SUCCESS_COLOR,
        )
        embed.add_field(name="Doador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Parceiro", value=usuario.mention, inline=True)
        embed.add_field(name="Bruto retirado", value=f"**{result.gross_amount} XP**", inline=True)
        embed.add_field(name="Taxa do vazio", value=f"**{result.tax_amount} XP** ({result.tax_rate:.0%})", inline=True)
        embed.add_field(name="Líquido recebido", value=f"**{result.net_amount} XP**", inline=True)
        embed.add_field(
            name="Saldos",
            value=(
                f"{interaction.user.mention}: {result.donor_balance_before} → **{result.donor_balance_after}**\n"
                f"{usuario.mention}: {result.receiver_balance_before} → **{result.receiver_balance_after}**"
            ),
            inline=False,
        )
        await self._send_embed(interaction, embed)

    async def config_adicionar(self, interaction: discord.Interaction, cargo: discord.Role) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        if cargo.is_default():
            await self._send_text(interaction, "👁️ O @everyone é o chão do palco, não um interesse do grimório.")
            return
        added = await self.repository.add_interest_role(interaction.guild.id, cargo.id)
        if not added:
            await self._send_text(interaction, "📜 Este cargo já está inscrito no grimório de interesses.")
            return
        await self._send_text(interaction, f"📜 Interesse registrado no grimório: {cargo.mention}.")

    async def config_remover(self, interaction: discord.Interaction, cargo: discord.Role) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        removed = await self.repository.remove_interest_role(interaction.guild.id, cargo.id)
        if not removed:
            await self._send_text(interaction, "⚰️ Esse cargo não estava no grimório. Não há tinta para raspar.")
            return
        await self._send_text(interaction, f"🕯️ Interesse removido do grimório: {cargo.mention}.")

    async def config_listar(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        role_ids = await self.repository.list_interest_role_ids(interaction.guild.id)
        embed = discord.Embed(title="📜 Grimório de interesses", color=VINCULO_COLOR)
        embed.description = self._format_role_ids(interaction.guild, role_ids) if role_ids else "Nenhum cargo de interesse configurado ainda."
        await self._send_embed(interaction, embed)

    async def config_limpar(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        removed = await self.repository.clear_interest_roles(interaction.guild.id)
        await self._send_text(interaction, f"⚰️ O grimório foi limpo. **{removed}** interesse(s) foram apagados deste servidor.")

    async def config_canal_fofoca(self, interaction: discord.Interaction, canal: discord.TextChannel | None = None) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        settings = await self.repository.set_gossip_channel(interaction.guild.id, canal.id if canal else None)
        if settings.gossip_channel_id is None:
            await self._send_text(interaction, "📭 O canal público do altar foi desativado.")
            return
        await self._send_text(interaction, f"📣 O altar agora sussurra em <#{settings.gossip_channel_id}>.")

    async def config_testar_card(
        self,
        interaction: discord.Interaction,
        solicitante: discord.Member,
        aceitante: discord.Member,
    ) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        if solicitante.id == aceitante.id:
            await self._send_text(interaction, "🪞 Escolha duas almas diferentes para testar o card.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            image = await self.vinculo_card_renderer.render(
                participant_a=solicitante,
                participant_b=aceitante,
                accent=(140, 140, 140),
                fallback_name_a=solicitante.display_name,
                fallback_name_b=aceitante.display_name,
            )
            image.seek(0)
            file = discord.File(image, filename=VINCULO_CARD_FILENAME)
            await interaction.followup.send(
                content=(
                    "Prévia do card de vínculo: "
                    f"**{discord.utils.escape_markdown(solicitante.display_name)}** como solicitante e "
                    f"**{discord.utils.escape_markdown(aceitante.display_name)}** como aceitante."
                ),
                file=file,
                ephemeral=True,
            )
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception(
                "falha ao gerar prévia do card de vínculo guild_id=%s solicitante_id=%s aceitante_id=%s",
                interaction.guild.id,
                solicitante.id,
                aceitante.id,
            )
            await interaction.followup.send(
                "Não consegui gerar a prévia do card agora.",
                ephemeral=True,
            )

    async def config_afinidade(
        self,
        interaction: discord.Interaction,
        nivel_2_dias: app_commands.Range[int, 1, 3650],
        nivel_3_dias: app_commands.Range[int, 2, 3650],
    ) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        settings = await self.repository.update_affinity_thresholds(interaction.guild.id, nivel_2_dias, nivel_3_dias)
        await self._send_text(
            interaction,
            (
                "🧵 Afinidade regravada: "
                f"nível 2 em **{settings.affinity_level_2_days}d**, "
                f"nível 3 em **{settings.affinity_level_3_days}d**."
            ),
        )

    async def config_maldicao(
        self,
        interaction: discord.Interaction,
        penalidade_percent: app_commands.Range[int, 0, 95],
        horas: app_commands.Range[int, 1, 720],
    ) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        settings = await self.repository.update_penalty_settings(interaction.guild.id, penalidade_percent, horas)
        await self._send_text(
            interaction,
            f"🩸 Ruptura configurada: **{settings.rupture_penalty_delta:.2f}x** por **{settings.rupture_penalty_hours}h**.",
        )

    async def config_doacao(
        self,
        interaction: discord.Interaction,
        taxa_percent: app_commands.Range[int, 0, 95],
    ) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        settings = await self.repository.update_transfer_settings(interaction.guild.id, taxa_percent)
        await self._send_text(interaction, f"🪙 Taxa de doação configurada em **{settings.transfer_tax_rate:.0%}**.")

    async def config_ressonancia(
        self,
        interaction: discord.Interaction,
        janela_minutos: app_commands.Range[int, 1, 1440],
        bonus_percent: app_commands.Range[int, 0, 95],
    ) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        settings = await self.repository.update_resonance_settings(interaction.guild.id, janela_minutos, bonus_percent)
        await self._send_text(
            interaction,
            (
                "👁️ Ressonância configurada: "
                f"janela de **{settings.resonance_window_minutes}min**, "
                f"bônus de **{settings.resonance_bonus:.0%}**."
            ),
        )

    async def config_limite(
        self,
        interaction: discord.Interaction,
        quantidade: app_commands.Range[int, 0, 100],
    ) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        settings = await self.repository.update_daily_request_limit(
            interaction.guild.id, quantidade,
        )
        if settings.daily_request_limit == 0:
            await self._send_text(
                interaction,
                "♾️ Limite de solicitações desativado. Os cultistas poderão criar pedidos sem restrição diária.",
            )
        else:
            await self._send_text(
                interaction,
                f"🩸 Limite diário de solicitações configurado para **{settings.daily_request_limit}** por usuário a cada 24 horas.",
            )

    @vinculo.command(name="ajuda", description="Mostra o grimório público da mecânica de vínculos.")
    @app_commands.guild_only()
    async def vinculo_ajuda(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return

        settings = await self.repository.get_guild_settings(interaction.guild.id)
        role_ids = await self.repository.list_interest_role_ids(interaction.guild.id)

        interest_summary = (
            f"**{len(role_ids)}** cargo(s) inscrito(s) no grimório deste servidor."
            if role_ids
            else "Nenhum cargo de interesse configurado ainda. Sem interesses, sem pacto. O altar não faz milagre sem ingrediente."
        )
        gossip_channel = f"<#{settings.gossip_channel_id}>" if settings.gossip_channel_id else "Não configurado"

        bond_types = "\n".join(
            f"{metadata.emoji} **{metadata.label}** — {metadata.pending_title.replace(metadata.emoji, '').strip().lower()}."
            for metadata in VINCULO_TYPE_METADATA.values()
        )

        embed = discord.Embed(
            title="📖 Grimório público dos vínculos",
            description=(
                "O sistema de vínculos é o altar onde duas almas "
                "conectam interesse em comum, presença, afinidade e XP."
            ),
            color=VINCULO_COLOR,
        )

        embed.add_field(
            name="🧵 Como um vínculo nasce",
            value=(
                "Use `/vinculo criar` e escolha uma pessoa e um tipo de pacto.\n"
                "O alvo recebe uma mensagem pública com botões para **aceitar** ou **recusar**.\n"
                f"O pedido expira em **{REQUEST_TIMEOUT_SECONDS}s**, e só a pessoa marcada pode decidir. "
                f"Quem pediu precisa esperar **{REQUEST_COOLDOWN_SECONDS}s** antes de oferecer outro pacto."
            ),
            inline=False,
        )

        embed.add_field(
            name="📜 Condições do altar",
            value=(
                "• Não dá para criar vínculo consigo mesmo.\n"
                "• Bots não assinam pacto; lata não sangra.\n"
                "• As duas pessoas precisam ter pelo menos **dois cargos de interesse em comum**.\n"
                "• Só pode existir **um vínculo ativo entre a mesma dupla** por vez.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="🎭 Tipos de vínculo",
            value=(
                f"{bond_types}\n\n"
                "O tipo muda a estética, o texto e a aura do pacto. A parte mecânica do XP continua sendo guiada "
                "por **afinidade**, **ressonância** e possíveis **maldições**."
            ),
            inline=False,
        )

        embed.add_field(
            name="🩸 Afinidade e bônus de XP",
            value=(
                "Todo pacto começa no **nível 1 — fio fino**, com bônus elegível de **+5%**.\n"
                f"Depois de **{settings.affinity_level_2_days}d**, vira **nível 2 — fio de sangue**, com **+10%**.\n"
                f"Depois de **{settings.affinity_level_3_days}d**, vira **nível 3 — laço da alma**, com **+15%**.\n"
                "O multiplicador parte de **1.0x** e soma os bônus elegíveis dos vínculos ativos, descontando maldições quando houver."
            ),
            inline=False,
        )

        embed.add_field(
            name="👁️ Ressonância",
            value=(
                "O vínculo não gosta de alma desaparecida. A presença recente é registrada quando alguém conversa no servidor.\n"
                f"No ganho de XP, o altar considera o parceiro visto nas últimas **{XP_RESONANCE_WINDOW_MINUTES // 60}h** "
                "para liberar o bônus de afinidade daquele pacto.\n"
                f"No `/vinculo status`, a janela exibida/configurável deste servidor está em **{settings.resonance_window_minutes}min**."
            ),
            inline=False,
        )

        embed.add_field(
            name="🪙 Doação de XP",
            value=(
                "Com `/vinculo doar-xp`, você pode transferir XP para uma pessoa com vínculo ativo.\n"
                f"A taxa ritual atual é de **{settings.transfer_tax_rate:.0%}**: essa parte some no vazio, "
                "e o restante chega para o parceiro.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="⚰️ Romper um vínculo",
            value=(
                "Use `/vinculo encerrar` para cortar um pacto ativo.\n"
                "O vínculo deixa de contar para XP imediatamente, e quem rompe recebe uma maldição temporária.\n"
                f"Configuração atual: **{settings.rupture_penalty_delta:.2f}x** por **{settings.rupture_penalty_hours}h**."
            ),
            inline=False,
        )

        embed.add_field(
            name="🔮 Comandos principais",
            value=(
                "`/vinculo criar` - oferece um pacto.\n"
                "`/vinculo status` - mostra afinidade, ressonância, maldições, doações e bônus registrado.\n"
                "`/vinculo doar-xp` - doa XP com taxa ritual.\n"
                "`/vinculo encerrar` - rompe o vínculo e aceita a consequência.\n"
                "`/vinculo ajuda` - abre este grimório público."
            ),
            inline=False,
        )

        await self._send_embed(interaction, embed, ephemeral=False)

    async def vinculo_relatorio(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return

        role_ids = await self.repository.list_interest_role_ids(interaction.guild.id)
        active_count = await self.repository.count_active_guild_vinculos(interaction.guild.id)
        pending_count = await self.repository.count_pending_requests(interaction.guild.id)
        settings = await self.repository.get_guild_settings(interaction.guild.id)
        xp_repo = getattr(self.bot, "xp_repository", None)
        xp_service = getattr(self.bot, "xp_service", None)
        xp_integrated = xp_repo is not None and xp_service is not None
        xp_status = "Integrado ao ganho de XP" if xp_integrated else "Sistema de XP ainda não inicializado"

        warnings: list[str] = []
        if not role_ids:
            warnings.append("Nenhum cargo de interesse configurado ainda.")
        if role_ids and active_count == 0:
            warnings.append("O sistema está configurado, mas ainda não existem vínculos ativos.")
        if active_count > 0:
            warnings.append("Existem vínculos ativos e o multiplicador será aplicado no ganho de XP.")
        if not xp_integrated:
            warnings.append("A integração com XP não foi detectada neste instante.")

        embed = discord.Embed(
            title="👁️ Relatório do altar de vínculos",
            color=VINCULO_COLOR,
        )
        embed.add_field(name="Interesses configurados", value=str(len(role_ids)), inline=True)
        embed.add_field(name="Vínculos ativos", value=str(active_count), inline=True)
        embed.add_field(name="Pedidos pendentes", value=str(pending_count), inline=True)
        embed.add_field(
            name="Cargos de interesse",
            value=self._format_role_ids(interaction.guild, role_ids) if role_ids else "Nenhum",
            inline=False,
        )
        embed.add_field(
            name="Cálculo do multiplicador",
            value=(
                "`1.0 + afinidade dos pactos + ressonância recente + maldições ativas`\n"
                f"Ressonância: **+{settings.resonance_bonus:.0%}** por parceiro recente "
                f"({settings.resonance_window_minutes}min)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Canal público",
            value=f"<#{settings.gossip_channel_id}>" if settings.gossip_channel_id else "Não configurado",
            inline=True,
        )
        embed.add_field(
            name="Afinidade",
            value=f"N2: {settings.affinity_level_2_days}d | N3: {settings.affinity_level_3_days}d",
            inline=True,
        )
        embed.add_field(name="Integração XP", value=xp_status, inline=False)
        embed.add_field(name="Avisos do altar", value="\n".join(f"• {warning}" for warning in warnings), inline=False)
        await self._send_embed(interaction, embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or not isinstance(message.author, discord.Member) or message.author.bot:
            return
        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        if now - self._last_activity_write.get(key, 0.0) < 60.0:
            return
        self._last_activity_write[key] = now
        try:
            await self.repository.upsert_presence(
                guild_id=message.guild.id,
                user_id=message.author.id,
                message_id=message.id,
                channel_id=message.channel.id,
                seen_at=message.created_at if message.created_at else None,
            )
            due = await self.repository.mark_due_affinity_announcements(message.guild.id, message.author.id)
            for vinculo, affinity in due:
                await self.announce_affinity_upgrade(message.guild, vinculo, affinity)
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception(
                "falha ao atualizar presença de vínculo guild_id=%s user_id=%s",
                message.guild.id,
                message.author.id,
            )

    async def _gossip_channel(self, guild: discord.Guild | None) -> discord.TextChannel | None:
        if guild is None:
            return None
        settings = await self.repository.get_guild_settings(guild.id)
        if settings.gossip_channel_id is None:
            LOGGER.info("canal de fofoca de vínculos não configurado guild_id=%s", guild.id)
            return None
        channel = guild.get_channel(settings.gossip_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await guild.fetch_channel(settings.gossip_channel_id)
        except discord.NotFound:
            LOGGER.warning(
                "canal de fofoca de vínculos ausente guild_id=%s channel_id=%s",
                guild.id,
                settings.gossip_channel_id,
            )
            return None
        except discord.Forbidden:
            LOGGER.warning(
                "sem permissão para acessar canal de fofoca de vínculos guild_id=%s channel_id=%s",
                guild.id,
                settings.gossip_channel_id,
            )
            return None
        except discord.HTTPException:
            LOGGER.warning(
                "falha ao buscar canal de fofoca de vínculos guild_id=%s channel_id=%s",
                guild.id,
                settings.gossip_channel_id,
            )
            return None
        if not isinstance(fetched, discord.TextChannel):
            LOGGER.warning(
                "canal de fofoca de vínculos incompatível guild_id=%s channel_id=%s channel_type=%s",
                guild.id,
                settings.gossip_channel_id,
                type(fetched).__name__,
            )
            return None
        return fetched

    async def _resolve_vinculo_participant(
        self,
        guild: discord.Guild | None,
        user_id: int,
    ) -> discord.Member | discord.User | None:
        if guild is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(user_id)
            except discord.NotFound:
                LOGGER.info("membro de vínculo não encontrado guild_id=%s user_id=%s", guild.id, user_id)
            except discord.Forbidden:
                LOGGER.warning("sem permissão para buscar membro de vínculo guild_id=%s user_id=%s", guild.id, user_id)
            except discord.HTTPException:
                LOGGER.warning("falha ao buscar membro de vínculo guild_id=%s user_id=%s", guild.id, user_id)

        cached_user = self.bot.get_user(user_id)
        if cached_user is not None:
            return cached_user
        try:
            return await self.bot.fetch_user(user_id)
        except discord.NotFound:
            LOGGER.info("usuário de vínculo não encontrado user_id=%s", user_id)
        except discord.HTTPException:
            LOGGER.warning("falha ao buscar usuário de vínculo user_id=%s", user_id)
        return None

    async def _build_vinculo_acceptance_file(
        self,
        *,
        guild: discord.Guild | None,
        requester_id: int,
        target_id: int,
        accent: tuple[int, int, int],
    ) -> discord.File | None:
        try:
            requester, target = await asyncio.gather(
                self._resolve_vinculo_participant(guild, requester_id),
                self._resolve_vinculo_participant(guild, target_id),
            )
            image = await self.vinculo_card_renderer.render(
                participant_a=requester,
                participant_b=target,
                accent=accent,
                fallback_name_a=f"Usuario {requester_id}",
                fallback_name_b=f"Usuario {target_id}",
            )
            image.seek(0)
            return discord.File(image, filename=VINCULO_CARD_FILENAME)
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception(
                "falha ao gerar card público de vínculo guild_id=%s requester_id=%s target_id=%s",
                getattr(guild, "id", None),
                requester_id,
                target_id,
            )
            return None

    async def announce_vinculo_accepted(
        self,
        *,
        guild: discord.Guild | None,
        requester_id: int,
        target_id: int,
        bond_type: VinculoType,
    ) -> None:
        channel = await self._gossip_channel(guild)
        if channel is None:
            return
        metadata = _vinculo_metadata(bond_type)
        embed = discord.Embed(
            title=metadata.accepted_title,
            description=metadata.accepted_description.format(requester=f"<@{requester_id}>", target=f"<@{target_id}>"),
            color=discord.Color(metadata.color),
        )
        embed.add_field(name="Tipo", value=f"{metadata.emoji} **{metadata.label}**", inline=True)
        embed.add_field(name="Afinidade inicial", value="Nível 1 — fio fino (**+5%**)", inline=True)
        file = await self._build_vinculo_acceptance_file(
            guild=guild,
            requester_id=requester_id,
            target_id=target_id,
            accent=metadata.render_accent,
        )
        if file is not None:
            embed.set_image(url=f"attachment://{VINCULO_CARD_FILENAME}")
        await self._send_public_embed(channel, embed, file=file)

    async def announce_vinculo_broken(
        self,
        *,
        guild: discord.Guild | None,
        breaker_id: int,
        target_id: int,
        result: BreakResult,
    ) -> None:
        channel = await self._gossip_channel(guild)
        if channel is None or result.vinculo is None:
            return
        metadata = _vinculo_metadata(result.vinculo.bond_type)
        penalty_text = "sem maldição registrada"
        if result.penalty is not None:
            penalty_text = f"{result.penalty.multiplier_delta:.2f}x até {_format_datetime(result.penalty.expires_at)}"
        embed = discord.Embed(
            title=metadata.rupture_title,
            description=metadata.rupture_description.format(breaker=f"<@{breaker_id}>", target=f"<@{target_id}>"),
            color=VINCULO_WARNING_COLOR,
        )
        embed.add_field(name="Tipo rompido", value=f"{metadata.emoji} **{metadata.label}**", inline=True)
        embed.add_field(name="Maldição", value=f"<@{breaker_id}>: **{penalty_text}**", inline=False)
        await self._send_public_embed(channel, embed)

    async def announce_affinity_upgrade(
        self,
        guild: discord.Guild,
        vinculo: ActiveVinculo,
        affinity: AffinitySnapshot,
    ) -> None:
        channel = await self._gossip_channel(guild)
        if channel is None:
            return
        metadata = _vinculo_metadata(vinculo.bond_type)
        embed = discord.Embed(
            title=f"{metadata.emoji} O pacto amadureceu",
            description=(
                f"<@{vinculo.user_low_id}> e <@{vinculo.user_high_id}> alcançaram "
                f"**{affinity.label}** no {metadata.label.lower()}."
            ),
            color=discord.Color(metadata.color),
        )
        embed.add_field(name="Afinidade", value=f"Nível **{affinity.level}** | bônus **+{affinity.bonus:.0%}**", inline=False)
        await self._send_public_embed(channel, embed)

    async def _send_public_embed(
        self,
        channel: discord.TextChannel,
        embed: discord.Embed,
        *,
        file: discord.File | None = None,
    ) -> None:
        try:
            if file is None:
                await channel.send(embed=embed)
            else:
                await channel.send(embed=embed, file=file)
        except discord.Forbidden:
            LOGGER.warning("sem permissão para enviar anúncio público de vínculo channel_id=%s", channel.id)
        except discord.HTTPException:
            LOGGER.warning(
                "falha ao enviar anúncio público de vínculo channel_id=%s with_file=%s",
                channel.id,
                file is not None,
            )

    def _extract_recovery_pair(self, message: discord.Message) -> tuple[int, int] | None:
        text_parts: list[str] = []
        if message.content:
            text_parts.append(message.content)
        for embed in message.embeds:
            if embed.title:
                text_parts.append(embed.title)
            if embed.description:
                text_parts.append(embed.description)
            for field in embed.fields:
                text_parts.append(field.name)
                text_parts.append(field.value)

        matches = re.findall(r"<@!?(\d+)>", " ".join(text_parts))
        if len(matches) < 2:
            return None

        try:
            user_a_id = int(matches[0])
            user_b_id = int(matches[1])
        except ValueError:
            return None

        if user_a_id == user_b_id:
            return None
        return _normalize_pair(user_a_id, user_b_id)

    def _parse_affinity_level_from_embed(self, embed: discord.Embed) -> int | None:
        for field in embed.fields:
            if field.name.strip().lower() != "afinidade":
                continue
            match = re.search(r"(\d+)", field.value)
            if match is None:
                return None
            return max(1, int(match.group(1)))
        return None

    def _classify_recovery_embed(
        self,
        embed: discord.Embed,
        *,
        pair: tuple[int, int],
        occurred_at: str,
    ) -> VinculoRecoveryEvent | None:
        title = (embed.title or "").strip()
        for metadata in VINCULO_TYPE_METADATA.values():
            if title == metadata.accepted_title:
                return VinculoRecoveryEvent(
                    kind="accepted",
                    user_low_id=pair[0],
                    user_high_id=pair[1],
                    bond_type=metadata.key,
                    occurred_at=occurred_at,
                )
            if title == metadata.rupture_title:
                return VinculoRecoveryEvent(
                    kind="broken",
                    user_low_id=pair[0],
                    user_high_id=pair[1],
                    bond_type=metadata.key,
                    occurred_at=occurred_at,
                )
            if title == f"{metadata.emoji} O pacto amadureceu":
                return VinculoRecoveryEvent(
                    kind="affinity",
                    user_low_id=pair[0],
                    user_high_id=pair[1],
                    bond_type=metadata.key,
                    occurred_at=occurred_at,
                    affinity_level=self._parse_affinity_level_from_embed(embed),
                )
        return None

    def _extract_recovery_event(self, message: discord.Message) -> VinculoRecoveryEvent | None:
        pair = self._extract_recovery_pair(message)
        if pair is None:
            return None

        created_at = getattr(message, "created_at", None)
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                occurred_at = created_at.replace(tzinfo=timezone.utc).isoformat()
            else:
                occurred_at = created_at.astimezone(timezone.utc).isoformat()
        else:
            occurred_at = _utc_now_iso()

        for embed in message.embeds:
            event = self._classify_recovery_embed(embed, pair=pair, occurred_at=occurred_at)
            if event is not None:
                return event
        return None

    def _collect_recovery_records(
        self,
        *,
        guild_id: int,
        messages: Iterable[discord.Message],
    ) -> list[VinculoRecoveryRecord]:
        bot_user = getattr(self.bot, "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        active_by_pair: dict[tuple[int, int], VinculoRecoveryRecord] = {}

        for message in messages:
            if bot_user_id is not None and getattr(message.author, "id", None) != bot_user_id:
                continue

            event = self._extract_recovery_event(message)
            if event is None:
                continue

            pair = (event.user_low_id, event.user_high_id)
            if event.kind == "accepted":
                active_by_pair[pair] = VinculoRecoveryRecord(
                    guild_id=guild_id,
                    user_low_id=event.user_low_id,
                    user_high_id=event.user_high_id,
                    bond_type=event.bond_type,
                    created_at=event.occurred_at,
                )
                continue

            if event.kind == "broken":
                active_by_pair.pop(pair, None)
                continue

            if event.kind == "affinity":
                record = active_by_pair.get(pair)
                if record is None:
                    record = VinculoRecoveryRecord(
                        guild_id=guild_id,
                        user_low_id=event.user_low_id,
                        user_high_id=event.user_high_id,
                        bond_type=event.bond_type,
                        created_at=event.occurred_at,
                    )
                    active_by_pair[pair] = record
                record.last_announced_affinity_level = max(
                    record.last_announced_affinity_level,
                    event.affinity_level or 1,
                )

        return list(active_by_pair.values())

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            await self._send_text(
                interaction,
                "👁️ Marionete ousada... este altar pertence aos que carregam as chaves do servidor.",
            )
            return
        if isinstance(original, app_commands.NoPrivateMessage):
            await self._send_text(interaction, "🕯️ Este ritual só existe dentro de um servidor.")
            return
        LOGGER.error(
            "erro em comando de vínculos: %s",
            original,
            exc_info=(type(original), original, original.__traceback__) if isinstance(original, BaseException) else None,
        )
        await self.send_interaction_error(interaction)


    async def restaurar_vinculos(
        self,
        interaction: discord.Interaction,
        canal_logs: discord.TextChannel,
        limite: int = 1000
    ) -> None:
        """Ferramenta de Disaster Recovery para reconstruir vínculos apagados (ETL de mensagens do Discord)."""
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild_id:
            await interaction.followup.send("Este comando deve ser usado em um servidor.")
            return

        mensagens_varridas = 0
        history_messages: list[discord.Message] = []

        try:
            async for message in canal_logs.history(limit=limite, oldest_first=True):
                mensagens_varridas += 1
                history_messages.append(message)

        except discord.Forbidden:
            await interaction.followup.send("❌ O bot não possui permissão para ler o histórico desse canal.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Erro HTTP durante a varredura: {e}", ephemeral=True)
            return

        recovery_records = self._collect_recovery_records(
            guild_id=interaction.guild_id,
            messages=history_messages,
        )

        restaurados = 0
        ja_existentes = 0
        if recovery_records:
            try:
                async with self.repository._tx_lock:
                    conn = self.repository.connection
                    await conn.execute("BEGIN IMMEDIATE")
                    existing_rows = await conn.execute_fetchall(
                        """
                        SELECT user_low_id, user_high_id
                        FROM vinculos
                        WHERE guild_id = ?
                          AND active = 1
                        """,
                        (interaction.guild_id,),
                    )
                    existing_pairs = {
                        (int(row["user_low_id"]), int(row["user_high_id"]))
                        for row in existing_rows
                    }

                    records_to_insert: list[VinculoRecoveryRecord] = []
                    for record in recovery_records:
                        pair = (record.user_low_id, record.user_high_id)
                        if pair in existing_pairs:
                            continue
                        existing_pairs.add(pair)
                        records_to_insert.append(record)

                    ja_existentes = len(recovery_records) - len(records_to_insert)
                    if records_to_insert:
                        await conn.executemany(
                            """
                            INSERT INTO vinculos
                            (guild_id, user_low_id, user_high_id, bond_type, created_at, active, last_announced_affinity_level, is_fused)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    record.guild_id,
                                    record.user_low_id,
                                    record.user_high_id,
                                    record.bond_type.value,
                                    record.created_at,
                                    1,
                                    record.last_announced_affinity_level,
                                    int(record.is_fused),
                                )
                                for record in records_to_insert
                            ],
                        )
                    restaurados = len(records_to_insert)
                    await conn.commit()
            except Exception as e:
                log_exception(e, context="Disaster Recovery: falha na transação batch de vínculos")
                with contextlib.suppress(Exception):
                    await self.repository.connection.rollback()
                await interaction.followup.send(f"❌ Erro crítico ao restaurar os registros no DB: {e}", ephemeral=True)
                return

        relatorio = (
            "✅ **Operação de Disaster Recovery Concluída**\n"
            f"🔍 Mensagens varridas: `{mensagens_varridas}`\n"
            f"🔗 Vínculos ativos reconstruídos: `{len(recovery_records)}`\n"
            f"🗃️ Já existentes no DB: `{ja_existentes}`\n"
            f"💾 Vínculos restaurados no DB: `{restaurados}`"
        )
        await interaction.followup.send(relatorio, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VinculosCog(bot, VinculoRepository(DB_PATH)))

