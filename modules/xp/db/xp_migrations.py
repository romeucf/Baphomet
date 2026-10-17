from __future__ import annotations

"""Migrações Do Sistema De XP."""

import aiosqlite

from ..utils import utc_now_iso

SCHEMA_VERSION = 5

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS xp_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS xp_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS xp_guild_config (
    guild_id INTEGER PRIMARY KEY,
    difficulty TEXT NOT NULL DEFAULT 'normal',
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    min_xp_per_message INTEGER NOT NULL DEFAULT 15,
    max_xp_per_message INTEGER NOT NULL DEFAULT 25,
    min_message_length INTEGER NOT NULL DEFAULT 8,
    min_unique_words INTEGER NOT NULL DEFAULT 2,
    anti_repeat_window_seconds INTEGER NOT NULL DEFAULT 180,
    anti_repeat_similarity REAL NOT NULL DEFAULT 0.92,
    ignore_bots INTEGER NOT NULL DEFAULT 1,
    ignore_webhooks INTEGER NOT NULL DEFAULT 1,
    levelup_channel_id INTEGER NULL,
    log_channel_id INTEGER NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS xp_profiles (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    total_xp INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_awarded_at TEXT NULL,
    last_message_hash TEXT NULL,
    last_message_at TEXT NULL,
    last_known_name TEXT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS xp_ignored_channels (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS xp_ignored_categories (
    guild_id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, category_id)
);

CREATE TABLE IF NOT EXISTS xp_ignored_roles (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS xp_level_roles (
    guild_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, level)
);

CREATE TABLE IF NOT EXISTS rank_badges (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    label TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS xp_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    target_user_id INTEGER NOT NULL,
    actor_user_id INTEGER NULL,
    delta_xp INTEGER NOT NULL,
    reason TEXT NULL,
    created_at TEXT NOT NULL
);

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
    message_hash TEXT NULL,
    final_xp INTEGER NOT NULL DEFAULT 0,
    positive_bonus_pool INTEGER NOT NULL DEFAULT 0,
    penalty_pool INTEGER NOT NULL DEFAULT 0,
    bonus_rate REAL NOT NULL DEFAULT 0,
    partner_last_seen_at TEXT NULL,
    resonance_window_seconds INTEGER NOT NULL DEFAULT 86400,
    CHECK (resonance_active IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_xp_profiles_guild_total_xp
    ON xp_profiles (guild_id, total_xp DESC, user_id ASC);

CREATE INDEX IF NOT EXISTS idx_xp_profiles_guild_updated_at
    ON xp_profiles (guild_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_xp_adjustments_guild_target_created
    ON xp_adjustments (guild_id, target_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_rank_badges_guild_priority
    ON rank_badges (guild_id, priority DESC, role_id DESC);

CREATE INDEX IF NOT EXISTS idx_vinculo_bonus_history_user
    ON vinculo_xp_bonus_history(guild_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_vinculo_bonus_history_vinculo
    ON vinculo_xp_bonus_history(vinculo_id, created_at DESC);

CREATE TABLE IF NOT EXISTS achievements_def (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    asset_path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_achievements_internal_code ON achievements_def(internal_code);

CREATE TABLE IF NOT EXISTS user_achievements (
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    achievement_id INTEGER NOT NULL,
    unlocked_at INTEGER DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
    PRIMARY KEY (user_id, guild_id, achievement_id),
    FOREIGN KEY(achievement_id) REFERENCES achievements_def(id)
);

CREATE INDEX IF NOT EXISTS idx_user_achievements_guild_user ON user_achievements(guild_id, user_id);
"""


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    rows = await conn.execute_fetchall(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in rows)


async def _ensure_column(conn: aiosqlite.Connection, table: str, definition: str, column: str) -> None:
    if not await _column_exists(conn, table, column):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


async def run_migrations(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA_SQL)
    await _ensure_column(conn, "xp_guild_config", "levelup_channel_id INTEGER NULL", "levelup_channel_id")
    await _ensure_column(conn, "xp_guild_config", "log_channel_id INTEGER NULL", "log_channel_id")
    await _ensure_column(conn, "xp_profiles", "last_message_hash TEXT NULL", "last_message_hash")
    await _ensure_column(conn, "xp_profiles", "last_message_at TEXT NULL", "last_message_at")
    await _ensure_column(conn, "xp_profiles", "last_known_name TEXT NULL", "last_known_name")
    await _ensure_column(conn, "vinculo_xp_bonus_history", "message_hash TEXT NULL", "message_hash")
    await _ensure_column(conn, "vinculo_xp_bonus_history", "final_xp INTEGER NOT NULL DEFAULT 0", "final_xp")
    await _ensure_column(
        conn,
        "vinculo_xp_bonus_history",
        "positive_bonus_pool INTEGER NOT NULL DEFAULT 0",
        "positive_bonus_pool",
    )
    await _ensure_column(conn, "vinculo_xp_bonus_history", "penalty_pool INTEGER NOT NULL DEFAULT 0", "penalty_pool")
    await _ensure_column(conn, "vinculo_xp_bonus_history", "bonus_rate REAL NOT NULL DEFAULT 0", "bonus_rate")
    await _ensure_column(
        conn,
        "vinculo_xp_bonus_history",
        "partner_last_seen_at TEXT NULL",
        "partner_last_seen_at",
    )
    await _ensure_column(
        conn,
        "vinculo_xp_bonus_history",
        "resonance_window_seconds INTEGER NOT NULL DEFAULT 86400",
        "resonance_window_seconds",
    )
    await _ensure_column(conn, "xp_profiles", "curse_expires_at INTEGER NOT NULL DEFAULT 0", "curse_expires_at")

    applied_rows = await conn.execute_fetchall("SELECT version FROM xp_schema_migrations")
    applied = {int(row[0]) for row in applied_rows}
    if SCHEMA_VERSION not in applied:
        await conn.execute(
            "INSERT INTO xp_schema_migrations(version, applied_at) VALUES(?, ?)",
            (SCHEMA_VERSION, utc_now_iso()),
        )

    await conn.commit()

