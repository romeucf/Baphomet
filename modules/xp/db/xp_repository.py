from __future__ import annotations

"""Persistência Assíncrona Do Sistema De XP."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from ..utils import (
    BondContribution,
    GuildXpConfig,
    PenaltyContribution,
    RankBadge,
    UserXpProfile,
    VINCULO_RESONANCE_WINDOW_SECONDS,
    VinculoXpContext,
    calculate_vinculo_xp_context,
    normalize_difficulty,
    parse_iso,
    utc_now_iso,
)
from .xp_migrations import run_migrations
from core.logger import log_exception



def build_default_guild_config(guild_id: int) -> GuildXpConfig:
    return GuildXpConfig(guild_id=guild_id)


DEFAULT_VINCULO_BOND_TYPE = "pacto_sangue"
DEFAULT_AFFINITY_LEVEL_2_DAYS = 7
DEFAULT_AFFINITY_LEVEL_3_DAYS = 60


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _affinity_bonus(level: int) -> float:
    return {
        1: 0.05,
        2: 0.10,
        3: 0.15,
    }.get(level, 0.05)


class XpRepository:
    CONFIG_FIELDS = {
        "difficulty",
        "cooldown_seconds",
        "min_xp_per_message",
        "max_xp_per_message",
        "min_message_length",
        "min_unique_words",
        "anti_repeat_window_seconds",
        "anti_repeat_similarity",
        "ignore_bots",
        "ignore_webhooks",
        "levelup_channel_id",
        "log_channel_id", # NEW
    }

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._tx_lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("XP Repository Ainda Não Foi Conectado")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = FULL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await run_migrations(self._conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def ensure_guild_config(self, guild_id: int) -> None:
        config = build_default_guild_config(guild_id)
        await self.connection.execute(
            """
            INSERT OR IGNORE INTO xp_guild_config(
                guild_id,
                difficulty,
                cooldown_seconds,
                min_xp_per_message,
                max_xp_per_message,
                min_message_length,
                min_unique_words,
                anti_repeat_window_seconds,
                anti_repeat_similarity,
                ignore_bots,
                ignore_webhooks,
                levelup_channel_id,
                log_channel_id,
                created_at,
                updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                config.difficulty.value,
                config.cooldown_seconds,
                config.min_xp_per_message,
                config.max_xp_per_message,
                config.min_message_length,
                config.min_unique_words,
                config.anti_repeat_window_seconds,
                config.anti_repeat_similarity,
                int(config.ignore_bots),
                int(config.ignore_webhooks),
                config.levelup_channel_id,
                config.log_channel_id,
                utc_now_iso(),
                utc_now_iso(),
            ),
        )
        await self.connection.commit()

    async def get_guild_config(self, guild_id: int) -> GuildXpConfig:
        await self.ensure_guild_config(guild_id)
        row = (await self.connection.execute_fetchall("SELECT * FROM xp_guild_config WHERE guild_id = ?", (guild_id,)))[0]
        ignored_channels = await self.connection.execute_fetchall(
            "SELECT channel_id FROM xp_ignored_channels WHERE guild_id = ?",
            (guild_id,),
        )
        ignored_categories = await self.connection.execute_fetchall(
            "SELECT category_id FROM xp_ignored_categories WHERE guild_id = ?",
            (guild_id,),
        )
        ignored_roles = await self.connection.execute_fetchall(
            "SELECT role_id FROM xp_ignored_roles WHERE guild_id = ?",
            (guild_id,),
        )
        level_roles = await self.connection.execute_fetchall(
            "SELECT level, role_id FROM xp_level_roles WHERE guild_id = ? ORDER BY level ASC",
            (guild_id,),
        )
        
        # Compatibilidade com colunas novas se falhar
        try:
            log_channel_id = int(row["log_channel_id"]) if row["log_channel_id"] is not None else None
        except (KeyError, IndexError):
            log_channel_id = None
            
        return GuildXpConfig(
            guild_id=guild_id,
            difficulty=normalize_difficulty(row["difficulty"]),
            cooldown_seconds=int(row["cooldown_seconds"]),
            min_xp_per_message=int(row["min_xp_per_message"]),
            max_xp_per_message=int(row["max_xp_per_message"]),
            min_message_length=int(row["min_message_length"]),
            min_unique_words=int(row["min_unique_words"]),
            anti_repeat_window_seconds=int(row["anti_repeat_window_seconds"]),
            anti_repeat_similarity=float(row["anti_repeat_similarity"]),
            ignore_bots=bool(row["ignore_bots"]),
            ignore_webhooks=bool(row["ignore_webhooks"]),
            levelup_channel_id=int(row["levelup_channel_id"]) if row["levelup_channel_id"] is not None else None,
            log_channel_id=log_channel_id,
            ignored_channel_ids={int(item[0]) for item in ignored_channels},
            ignored_category_ids={int(item[0]) for item in ignored_categories},
            ignored_role_ids={int(item[0]) for item in ignored_roles},
            level_roles={int(item[0]): int(item[1]) for item in level_roles},
        )

    async def update_guild_config(self, guild_id: int, **fields: Any) -> None:
        invalid = set(fields) - self.CONFIG_FIELDS
        if invalid:
            raise ValueError(f"campos de config inválidos: {sorted(invalid)}")
        if not fields:
            return
        await self.ensure_guild_config(guild_id)
        payload = dict(fields)
        if "difficulty" in payload:
            payload["difficulty"] = normalize_difficulty(payload["difficulty"]).value
        for boolean_field in ("ignore_bots", "ignore_webhooks"):
            if boolean_field in payload:
                payload[boolean_field] = int(bool(payload[boolean_field]))
        assignments = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values()) + [utc_now_iso(), guild_id]
        await self.connection.execute(
            f"UPDATE xp_guild_config SET {assignments}, updated_at = ? WHERE guild_id = ?",
            values,
        )
        await self.connection.commit()

    async def set_ignored_target(self, table: str, column: str, guild_id: int, target_id: int, enabled: bool) -> None:
        valid = {
            "xp_ignored_channels": "channel_id",
            "xp_ignored_categories": "category_id",
            "xp_ignored_roles": "role_id",
        }
        if valid.get(table) != column:
            raise ValueError("tabela de ignore inválida")
        if enabled:
            await self.connection.execute(
                f"INSERT OR IGNORE INTO {table}(guild_id, {column}) VALUES(?, ?)",
                (guild_id, target_id),
            )
        else:
            await self.connection.execute(
                f"DELETE FROM {table} WHERE guild_id = ? AND {column} = ?",
                (guild_id, target_id),
            )
        await self.connection.commit()

    async def set_level_role(self, guild_id: int, level: int, role_id: int) -> None:
        await self.connection.execute(
            "INSERT OR REPLACE INTO xp_level_roles(guild_id, level, role_id) VALUES(?, ?, ?)",
            (guild_id, level, role_id),
        )
        await self.connection.commit()

    async def remove_level_role(self, guild_id: int, level: int) -> bool:
        cur = await self.connection.execute(
            "DELETE FROM xp_level_roles WHERE guild_id = ? AND level = ?",
            (guild_id, level),
        )
        await self.connection.commit()
        return cur.rowcount > 0

    async def set_rank_badge(
        self,
        *,
        guild_id: int,
        role_id: int,
        image_path: str,
        priority: int = 0,
        label: str | None = None,
    ) -> RankBadge:
        now = utc_now_iso()
        await self.connection.execute(
            """
            INSERT INTO rank_badges(
                guild_id,
                role_id,
                image_path,
                priority,
                label,
                created_at,
                updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                image_path = excluded.image_path,
                priority = excluded.priority,
                label = excluded.label,
                updated_at = excluded.updated_at
            """,
            (guild_id, role_id, image_path, int(priority), label, now, now),
        )
        await self.connection.commit()
        return RankBadge(
            guild_id=guild_id,
            role_id=role_id,
            image_path=image_path,
            priority=int(priority),
            label=label,
            created_at=now,
            updated_at=now,
        )

    async def remove_rank_badge(self, guild_id: int, role_id: int) -> RankBadge | None:
        rows = await self.connection.execute_fetchall(
            "SELECT * FROM rank_badges WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        if not rows:
            return None
        badge = self._row_to_rank_badge(rows[0])
        await self.connection.execute(
            "DELETE FROM rank_badges WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        await self.connection.commit()
        return badge

    async def get_rank_badge(self, guild_id: int, role_id: int) -> RankBadge | None:
        rows = await self.connection.execute_fetchall(
            "SELECT * FROM rank_badges WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        return self._row_to_rank_badge(rows[0]) if rows else None

    async def list_rank_badges(self, guild_id: int) -> list[RankBadge]:
        rows = await self.connection.execute_fetchall(
            """
            SELECT *
            FROM rank_badges
            WHERE guild_id = ?
            ORDER BY priority DESC, role_id DESC
            """,
            (guild_id,),
        )
        return [self._row_to_rank_badge(row) for row in rows]

    async def get_profile(self, guild_id: int, user_id: int) -> UserXpProfile:
        rows = await self.connection.execute_fetchall(
            "SELECT * FROM xp_profiles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if not rows:
            return UserXpProfile(guild_id=guild_id, user_id=user_id)
        row = rows[0]
        return UserXpProfile(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            total_xp=int(row["total_xp"]),
            message_count=int(row["message_count"]),
            last_awarded_at=row["last_awarded_at"],
            last_message_hash=row["last_message_hash"],
            last_message_at=row["last_message_at"],
            last_known_name=row["last_known_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_vinculo_xp_context(
        self,
        *,
        guild_id: int,
        user_id: int,
        base_xp: int,
        awarded_at_iso: str,
        resonance_window_seconds: int = VINCULO_RESONANCE_WINDOW_SECONDS,
    ) -> VinculoXpContext:
        if not await self._vinculo_context_tables_ready():
            return calculate_vinculo_xp_context(base_xp=base_xp, bonds=(), penalties=(), source="none")

        awarded_at = _as_utc(parse_iso(awarded_at_iso)) or datetime.now(timezone.utc)
        cutoff = awarded_at - timedelta(seconds=max(1, int(resonance_window_seconds)))
        level_2_days, level_3_days = await self._get_vinculo_affinity_thresholds(guild_id)
        vinculo_rows = await self.connection.execute_fetchall(
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
        partner_ids = [
            int(row["user_high_id"]) if int(row["user_low_id"]) == user_id else int(row["user_low_id"])
            for row in vinculo_rows
        ]
        presence_by_user = await self._get_vinculo_presence_by_user(guild_id, partner_ids)
        bonds: list[BondContribution] = []
        for row in vinculo_rows:
            partner_id = int(row["user_high_id"]) if int(row["user_low_id"]) == user_id else int(row["user_low_id"])
            created_at = _as_utc(parse_iso(str(row["created_at"]))) or awarded_at
            affinity_level = self._affinity_level_for_created_at(
                created_at,
                awarded_at,
                level_2_days,
                level_3_days,
            )
            partner_last_seen_at = presence_by_user.get(partner_id)
            partner_seen = _as_utc(parse_iso(partner_last_seen_at)) if partner_last_seen_at else None
            resonance_active = partner_seen is not None and partner_seen >= cutoff
            bonds.append(
                BondContribution(
                    vinculo_id=int(row["id"]),
                    partner_id=partner_id,
                    bond_type=str(self._row_get(row, "bond_type", DEFAULT_VINCULO_BOND_TYPE) or DEFAULT_VINCULO_BOND_TYPE),
                    affinity_level=affinity_level,
                    bonus_rate=_affinity_bonus(affinity_level),
                    resonance_active=resonance_active,
                    partner_last_seen_at=partner_last_seen_at,
                    resonance_window_seconds=max(1, int(resonance_window_seconds)),
                )
            )

        penalties = await self._get_vinculo_penalty_contributions(guild_id, user_id, awarded_at)
        return calculate_vinculo_xp_context(
            base_xp=base_xp,
            bonds=bonds,
            penalties=penalties,
            source="sqlite_vinculos",
        )

    async def count_active_vinculos(self, guild_id: int, user_id: int) -> int:
        if not await self._table_exists("vinculos"):
            return 0
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

    async def _vinculo_context_tables_ready(self) -> bool:
        rows = await self.connection.execute_fetchall(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('vinculos', 'vinculo_presence', 'vinculo_penalties')
            """
        )
        return {str(row[0]) for row in rows} == {"vinculos", "vinculo_presence", "vinculo_penalties"}

    async def _table_exists(self, table_name: str) -> bool:
        rows = await self.connection.execute_fetchall(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return bool(rows)

    async def _get_vinculo_affinity_thresholds(self, guild_id: int) -> tuple[int, int]:
        if not await self._table_exists("vinculo_guild_settings"):
            return DEFAULT_AFFINITY_LEVEL_2_DAYS, DEFAULT_AFFINITY_LEVEL_3_DAYS
        rows = await self.connection.execute_fetchall(
            """
            SELECT affinity_level_2_days, affinity_level_3_days
            FROM vinculo_guild_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        if not rows:
            return DEFAULT_AFFINITY_LEVEL_2_DAYS, DEFAULT_AFFINITY_LEVEL_3_DAYS
        level_2_days = max(1, int(rows[0]["affinity_level_2_days"]))
        level_3_days = max(level_2_days + 1, int(rows[0]["affinity_level_3_days"]))
        return level_2_days, level_3_days

    async def _get_vinculo_presence_by_user(self, guild_id: int, user_ids: list[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        placeholders = ", ".join("?" for _ in user_ids)
        rows = await self.connection.execute_fetchall(
            f"""
            SELECT user_id, last_seen_at
            FROM vinculo_presence
            WHERE guild_id = ?
              AND user_id IN ({placeholders})
            """,
            (guild_id, *user_ids),
        )
        return {int(row["user_id"]): str(row["last_seen_at"]) for row in rows}

    async def _get_vinculo_penalty_contributions(
        self,
        guild_id: int,
        user_id: int,
        awarded_at: datetime,
    ) -> tuple[PenaltyContribution, ...]:
        rows = await self.connection.execute_fetchall(
            """
            SELECT id, multiplier_delta, reason, expires_at
            FROM vinculo_penalties
            WHERE guild_id = ?
              AND user_id = ?
              AND active = 1
            ORDER BY expires_at ASC, id ASC
            """,
            (guild_id, user_id),
        )
        penalties: list[PenaltyContribution] = []
        for row in rows:
            expires_at = str(row["expires_at"])
            expires = _as_utc(parse_iso(expires_at))
            if expires is None or expires <= awarded_at:
                continue
            multiplier_delta = float(row["multiplier_delta"])
            if multiplier_delta >= 0:
                continue
            penalties.append(
                PenaltyContribution(
                    penalty_id=int(row["id"]),
                    multiplier_delta=multiplier_delta,
                    reason=str(row["reason"]),
                    expires_at=expires_at,
                )
            )
        return self._coalesce_vinculo_penalties(tuple(penalties))

    def _coalesce_vinculo_penalties(
        self,
        penalties: tuple[PenaltyContribution, ...],
    ) -> tuple[PenaltyContribution, ...]:
        rupture_penalties = [penalty for penalty in penalties if penalty.reason == "ruptura"]
        other_penalties = [penalty for penalty in penalties if penalty.reason != "ruptura"]
        if rupture_penalties:
            strongest_rupture = min(
                rupture_penalties,
                key=lambda penalty: (penalty.multiplier_delta, penalty.expires_at, penalty.penalty_id),
            )
            other_penalties.append(strongest_rupture)
        return tuple(sorted(other_penalties, key=lambda penalty: (penalty.expires_at, penalty.penalty_id)))

    def _affinity_level_for_created_at(
        self,
        created_at: datetime,
        awarded_at: datetime,
        level_2_days: int,
        level_3_days: int,
    ) -> int:
        level_2_at = created_at + timedelta(days=max(1, level_2_days))
        level_3_at = created_at + timedelta(days=max(level_2_days + 1, level_3_days))
        if awarded_at >= level_3_at:
            return 3
        if awarded_at >= level_2_at:
            return 2
        return 1

    def _row_get(self, row: aiosqlite.Row, key: str, default: object = None) -> object:
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    async def try_add_message_xp(
        self,
        *,
        guild_id: int,
        user_id: int,
        delta_xp: int,
        last_known_name: str,
        awarded_at_iso: str,
        cooldown_cutoff_iso: str,
        message_hash: str,
        repeat_cutoff_iso: str,
        vinculo_context: VinculoXpContext | None = None,
    ) -> tuple[bool, str | None, int, int]:
        if vinculo_context is not None:
            delta_xp = vinculo_context.final_xp
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                profile = await self.get_profile(guild_id, user_id)
                old_total = profile.total_xp
                last_awarded_at = parse_iso(profile.last_awarded_at)
                cooldown_cutoff = parse_iso(cooldown_cutoff_iso)
                if last_awarded_at and cooldown_cutoff and last_awarded_at > cooldown_cutoff:
                    await conn.rollback()
                    return False, "cooldown", old_total, old_total

                last_message_at = parse_iso(profile.last_message_at)
                repeat_cutoff = parse_iso(repeat_cutoff_iso)
                if (
                    profile.last_message_hash
                    and profile.last_message_hash == message_hash
                    and last_message_at
                    and repeat_cutoff
                    and last_message_at > repeat_cutoff
                ):
                    await conn.rollback()
                    return False, "repeat_db", old_total, old_total

                await conn.execute(
                    """
                    INSERT INTO xp_profiles(
                        guild_id,
                        user_id,
                        total_xp,
                        message_count,
                        last_awarded_at,
                        last_message_hash,
                        last_message_at,
                        last_known_name,
                        created_at,
                        updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        total_xp = MAX(0, xp_profiles.total_xp + excluded.total_xp),
                        message_count = xp_profiles.message_count + excluded.message_count,
                        last_awarded_at = excluded.last_awarded_at,
                        last_message_hash = excluded.last_message_hash,
                        last_message_at = excluded.last_message_at,
                        last_known_name = excluded.last_known_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        guild_id,
                        user_id,
                        delta_xp,
                        1,
                        awarded_at_iso,
                        message_hash,
                        awarded_at_iso,
                        last_known_name,
                        awarded_at_iso,
                        awarded_at_iso,
                    ),
                )
                if vinculo_context is not None:
                    await self._insert_vinculo_bonus_history(
                        conn=conn,
                        guild_id=guild_id,
                        user_id=user_id,
                        context=vinculo_context,
                        message_hash=message_hash,
                        created_at=awarded_at_iso,
                    )
                updated = await self.get_profile(guild_id, user_id)
                await conn.commit()
                return True, None, old_total, updated.total_xp
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def _insert_vinculo_bonus_history(
        self,
        *,
        conn: aiosqlite.Connection,
        guild_id: int,
        user_id: int,
        context: VinculoXpContext,
        message_hash: str,
        created_at: str,
    ) -> None:
        if not context.bond_contributions:
            return
        for bond in context.bond_contributions:
            if not bond.resonance_active or bond.bonus_rate <= 0:
                continue
            await conn.execute(
                """
                INSERT INTO vinculo_xp_bonus_history(
                    guild_id,
                    vinculo_id,
                    user_id,
                    partner_id,
                    bond_type,
                    base_xp,
                    bonus_xp,
                    multiplier,
                    affinity_level,
                    resonance_active,
                    penalty_delta,
                    created_at,
                    message_hash,
                    final_xp,
                    positive_bonus_pool,
                    penalty_pool,
                    bonus_rate,
                    partner_last_seen_at,
                    resonance_window_seconds
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    bond.vinculo_id,
                    user_id,
                    bond.partner_id,
                    bond.bond_type,
                    context.base_xp,
                    bond.allocated_bonus_xp,
                    context.final_multiplier,
                    bond.affinity_level,
                    int(bond.resonance_active),
                    -context.penalty_rate,
                    created_at,
                    message_hash,
                    context.final_xp,
                    context.positive_bonus_pool,
                    context.penalty_pool,
                    bond.bonus_rate,
                    bond.partner_last_seen_at,
                    bond.resonance_window_seconds,
                ),
            )

    async def adjust_xp(
        self,
        *,
        guild_id: int,
        user_id: int,
        delta_xp: int,
        last_known_name: str,
        actor_user_id: int | None,
        reason: str | None,
    ) -> tuple[int, int]:
        now = utc_now_iso()
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                profile = await self.get_profile(guild_id, user_id)
                old_total = profile.total_xp
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO xp_profiles(
                        guild_id,
                        user_id,
                        total_xp,
                        message_count,
                        last_known_name,
                        created_at,
                        updated_at
                    ) VALUES(?, ?, 0, 0, ?, ?, ?)
                    """,
                    (guild_id, user_id, last_known_name, now, now),
                )
                await conn.execute(
                    """
                    UPDATE xp_profiles
                    SET total_xp = MAX(0, total_xp + ?),
                        last_known_name = ?,
                        updated_at = ?
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (delta_xp, last_known_name, now, guild_id, user_id),
                )
                await conn.execute(
                    """
                    INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, user_id, actor_user_id, delta_xp, reason, now),
                )
                updated = await self.get_profile(guild_id, user_id)
                await conn.commit()
                return old_total, updated.total_xp
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def reset_guild_xp(self, guild_id: int, actor_user_id: int) -> int:
        now = utc_now_iso()
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "DELETE FROM xp_profiles WHERE guild_id = ?",
                    (guild_id,)
                )
                deleted_count = cur.rowcount
                
                await conn.execute(
                    """
                    INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, 0, actor_user_id, 0, "RESET GLOBAL DE TEMPORADA", now),
                )
                await conn.commit()
                return deleted_count
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def reset_profile(self, guild_id: int, user_id: int, actor_user_id: int | None, reason: str | None) -> tuple[int, int]:
        now = utc_now_iso()
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                profile = await self.get_profile(guild_id, user_id)
                old_total = profile.total_xp
                await conn.execute(
                    """
                    INSERT INTO xp_profiles(
                        guild_id,
                        user_id,
                        total_xp,
                        message_count,
                        created_at,
                        updated_at
                    ) VALUES(?, ?, 0, 0, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        total_xp = 0,
                        message_count = 0,
                        last_awarded_at = NULL,
                        last_message_hash = NULL,
                        last_message_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (guild_id, user_id, now, now),
                )
                await conn.execute(
                    """
                    INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, user_id, actor_user_id, -old_total, reason or "reset", now),
                )
                await conn.commit()
                return old_total, 0
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def execute_purification(
        self,
        guild_id: int,
        target_user_id: int,
        top_user_ids: list[int],
        slice_xp: int,
        actor_user_id: int,
    ) -> int:
        now = utc_now_iso()
        async with self._tx_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                profile = await self.get_profile(guild_id, target_user_id)
                total_sacrificed = profile.total_xp

                await conn.execute(
                    """
                    UPDATE xp_profiles
                    SET total_xp = 0,
                        message_count = 0,
                        last_awarded_at = NULL,
                        last_message_hash = NULL,
                        last_message_at = NULL,
                        updated_at = ?
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (now, guild_id, target_user_id),
                )

                await conn.execute(
                    """
                    INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (guild_id, target_user_id, actor_user_id, -total_sacrificed, "PURIFICAÇÃO (ALVO)", now),
                )

                for uid in top_user_ids:
                    await conn.execute(
                        """
                        INSERT OR IGNORE INTO xp_profiles(
                            guild_id,
                            user_id,
                            total_xp,
                            message_count,
                            created_at,
                            updated_at
                        ) VALUES(?, ?, 0, 0, ?, ?)
                        """,
                        (guild_id, uid, now, now),
                    )
                    await conn.execute(
                        """
                        UPDATE xp_profiles
                        SET total_xp = total_xp + ?,
                            updated_at = ?
                        WHERE guild_id = ? AND user_id = ?
                        """,
                        (slice_xp, now, guild_id, uid),
                    )
                    await conn.execute(
                        """
                        INSERT INTO xp_adjustments(guild_id, target_user_id, actor_user_id, delta_xp, reason, created_at)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (guild_id, uid, actor_user_id, slice_xp, "PURIFICAÇÃO (BENEFICIÁRIO)", now),
                    )

                await conn.commit()
                return total_sacrificed
            except Exception as exc:
                log_exception(exc)
                await conn.rollback()
                raise

    async def count_ranked_profiles(self, guild_id: int) -> int:
        rows = await self.connection.execute_fetchall(
            "SELECT COUNT(*) AS c FROM xp_profiles WHERE guild_id = ? AND total_xp > 0",
            (guild_id,),
        )
        return int(rows[0]["c"])

    async def get_rank_position(self, guild_id: int, user_id: int, total_xp: int) -> int | None:
        if total_xp <= 0:
            return None
        rows = await self.connection.execute_fetchall(
            """
            SELECT COUNT(*) AS ahead
            FROM xp_profiles
            WHERE guild_id = ?
              AND total_xp > 0
              AND (
                total_xp > ?
                OR (total_xp = ? AND user_id < ?)
              )
            """,
            (guild_id, total_xp, total_xp, user_id),
        )
        return int(rows[0]["ahead"]) + 1

    async def get_top_profiles(self, guild_id: int, limit: int) -> list[UserXpProfile]:
        rows = await self.connection.execute_fetchall(
            """
            SELECT *
            FROM xp_profiles
            WHERE guild_id = ? AND total_xp > 0
            ORDER BY total_xp DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [self._row_to_profile(row) for row in rows]

    async def get_profiles_page(self, guild_id: int, offset: int, limit: int) -> list[UserXpProfile]:
        rows = await self.connection.execute_fetchall(
            """
            SELECT *
            FROM xp_profiles
            WHERE guild_id = ? AND total_xp > 0
            ORDER BY total_xp DESC, user_id ASC
            LIMIT ? OFFSET ?
            """,
            (guild_id, limit, offset),
        )
        return [self._row_to_profile(row) for row in rows]

    def _row_to_profile(self, row: aiosqlite.Row) -> UserXpProfile:
        return UserXpProfile(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            total_xp=int(row["total_xp"]),
            message_count=int(row["message_count"]),
            last_awarded_at=row["last_awarded_at"],
            last_message_hash=row["last_message_hash"],
            last_message_at=row["last_message_at"],
            last_known_name=row["last_known_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_rank_badge(self, row: aiosqlite.Row) -> RankBadge:
        return RankBadge(
            guild_id=int(row["guild_id"]),
            role_id=int(row["role_id"]),
            image_path=str(row["image_path"]),
            priority=int(row["priority"]),
            label=str(row["label"]) if row["label"] is not None else None,
            created_at=str(row["created_at"]) if row["created_at"] is not None else None,
            updated_at=str(row["updated_at"]) if row["updated_at"] is not None else None,
        )
