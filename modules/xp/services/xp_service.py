from __future__ import annotations

"""Regra De Negócio Do Sistema De XP Do Baphomet."""

import asyncio
import difflib
import hashlib
import logging
import secrets
from collections import defaultdict, deque
from datetime import timedelta
from typing import Protocol

import discord

from ..utils import (
    GuildXpConfig,
    LeaderboardEntry,
    LevelRoleSyncResult,
    PageResult,
    RankBondSummary,
    RankSnapshot,
    VINCULO_RESONANCE_WINDOW_SECONDS,
    VinculoXpContext,
    XpChangeResult,
    build_progress_snapshot,
    calculate_vinculo_xp_context,
    normalize_message_content,
    utc_now,
)
from ..db.xp_repository import XpRepository
from core.logger import log_exception



class VinculoXpContextProvider(Protocol):
    async def get_xp_context(self, guild_id: int, user_id: int, base_xp: int) -> VinculoXpContext:
        ...


class VinculoMultiplierProvider(Protocol):
    async def get_xp_multiplier(self, guild_id: int, user_id: int) -> float:
        ...


class XpService:
    LEVEL_ROLE_POLICY = "cumulative"

    def __init__(
        self,
        repository: XpRepository,
        *,
        rng: secrets.SystemRandom | None = None,
        logger: logging.Logger | None = None,
        vinculos_provider: VinculoXpContextProvider | VinculoMultiplierProvider | None = None,
    ) -> None:
        self.repository = repository
        self.rng = rng or secrets.SystemRandom()
        self.logger = logger or logging.getLogger("baphomet.xp")
        self.vinculos_provider = vinculos_provider
        self._config_cache: dict[int, GuildXpConfig] = {}
        self._user_locks: dict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._recent_fingerprints: dict[tuple[int, int], deque[tuple[str, float]]] = defaultdict(lambda: deque(maxlen=5))

    async def get_guild_config(self, guild_id: int) -> GuildXpConfig:
        cached = self._config_cache.get(guild_id)
        if cached is not None:
            return cached
        config = await self.repository.get_guild_config(guild_id)
        self._config_cache[guild_id] = config
        return config

    async def refresh_guild_config(self, guild_id: int) -> GuildXpConfig:
        config = await self.repository.get_guild_config(guild_id)
        self._config_cache[guild_id] = config
        return config

    async def update_guild_config(self, guild_id: int, **fields: object) -> GuildXpConfig:
        await self.repository.update_guild_config(guild_id, **fields)
        return await self.refresh_guild_config(guild_id)

    async def set_ignored_channel(self, guild_id: int, channel_id: int, enabled: bool) -> GuildXpConfig:
        await self.repository.set_ignored_target("xp_ignored_channels", "channel_id", guild_id, channel_id, enabled)
        return await self.refresh_guild_config(guild_id)

    async def set_ignored_category(self, guild_id: int, category_id: int, enabled: bool) -> GuildXpConfig:
        await self.repository.set_ignored_target("xp_ignored_categories", "category_id", guild_id, category_id, enabled)
        return await self.refresh_guild_config(guild_id)

    async def set_ignored_role(self, guild_id: int, role_id: int, enabled: bool) -> GuildXpConfig:
        await self.repository.set_ignored_target("xp_ignored_roles", "role_id", guild_id, role_id, enabled)
        return await self.refresh_guild_config(guild_id)

    async def set_level_role(self, guild_id: int, level: int, role_id: int) -> GuildXpConfig:
        await self.repository.set_level_role(guild_id, level, role_id)
        return await self.refresh_guild_config(guild_id)

    async def remove_level_role(self, guild_id: int, level: int) -> tuple[GuildXpConfig, bool]:
        removed = await self.repository.remove_level_role(guild_id, level)
        return await self.refresh_guild_config(guild_id), removed

    async def get_rank_bond_summary(self, guild_id: int, user_id: int) -> RankBondSummary:
        count = 0
        counter = getattr(self.vinculos_provider, "count_active_vinculos", None) if self.vinculos_provider else None
        if callable(counter):
            try:
                count = max(0, int(await counter(guild_id, user_id)))
            except Exception as exc:
                log_exception(exc)
                self.logger.exception("falha ao contar vínculos guild_id=%s user_id=%s", guild_id, user_id)
        else:
            try:
                count = max(0, int(await self.repository.count_active_vinculos(guild_id, user_id)))
            except Exception as exc:
                log_exception(exc)
                self.logger.exception("falha ao contar vínculos no sqlite guild_id=%s user_id=%s", guild_id, user_id)

        multiplier_getter = getattr(self.vinculos_provider, "get_xp_multiplier", None) if self.vinculos_provider else None
        if callable(multiplier_getter):
            try:
                return RankBondSummary(count=count, multiplier=max(0.0, float(await multiplier_getter(guild_id, user_id))))
            except Exception as exc:
                log_exception(exc)
                self.logger.exception("falha ao obter multiplicador de vínculos guild_id=%s user_id=%s", guild_id, user_id)

        return RankBondSummary(count=count, multiplier=1.0 + (count * 0.1))

    def _target_level_role_ids(self, config: GuildXpConfig, level: int) -> set[int]:
        eligible = {
            role_id
            for required_level, role_id in config.level_roles.items()
            if level >= required_level
        }
        if self.LEVEL_ROLE_POLICY == "highest" and eligible:
            highest_level = max(required for required in config.level_roles if level >= required)
            return {config.level_roles[highest_level]}
        return eligible

    def _manageable_level_role(
        self,
        guild: discord.Guild,
        role_id: int,
    ) -> tuple[discord.Role | None, bool]:
        role = guild.get_role(role_id)
        if role is None:
            return None, False
        bot_member = guild.me
        if bot_member is None:
            return role, False
        if not bot_member.guild_permissions.manage_roles:
            return role, False
        if role.is_default() or role.managed or role >= bot_member.top_role:
            return role, False
        return role, True

    async def sync_member_level_roles(
        self,
        member: discord.Member,
        *,
        reason: str = "XP: sincronização de cargos de nível",
    ) -> LevelRoleSyncResult:
        config = await self.get_guild_config(member.guild.id)
        if not config.level_roles:
            return LevelRoleSyncResult()

        profile = await self.repository.get_profile(member.guild.id, member.id)
        progress = build_progress_snapshot(profile.total_xp, config.difficulty)
        configured_role_ids = set(config.level_roles.values())
        target_role_ids = self._target_level_role_ids(config, progress.level)
        current_role_ids = {role.id for role in member.roles}

        missing_role_ids: list[int] = []
        skipped_role_ids: list[int] = []
        roles_to_add: list[discord.Role] = []
        roles_to_remove: list[discord.Role] = []

        for role_id in sorted(target_role_ids - current_role_ids):
            role, manageable = self._manageable_level_role(member.guild, role_id)
            if role is None:
                missing_role_ids.append(role_id)
            elif manageable:
                roles_to_add.append(role)
            else:
                skipped_role_ids.append(role_id)

        for role_id in sorted((current_role_ids & configured_role_ids) - target_role_ids):
            role, manageable = self._manageable_level_role(member.guild, role_id)
            if role is None:
                missing_role_ids.append(role_id)
            elif manageable:
                roles_to_remove.append(role)
            else:
                skipped_role_ids.append(role_id)

        added_ids: tuple[int, ...] = ()
        removed_ids: tuple[int, ...] = ()
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason=reason)
                added_ids = tuple(role.id for role in roles_to_add)
            except (discord.Forbidden, discord.HTTPException):
                self.logger.warning(
                    "falha ao adicionar cargos de nível guild_id=%s user_id=%s role_ids=%s",
                    member.guild.id,
                    member.id,
                    [role.id for role in roles_to_add],
                )
                skipped_role_ids.extend(role.id for role in roles_to_add)

        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason=reason)
                removed_ids = tuple(role.id for role in roles_to_remove)
            except (discord.Forbidden, discord.HTTPException):
                self.logger.warning(
                    "falha ao remover cargos de nível guild_id=%s user_id=%s role_ids=%s",
                    member.guild.id,
                    member.id,
                    [role.id for role in roles_to_remove],
                )
                skipped_role_ids.extend(role.id for role in roles_to_remove)

        return LevelRoleSyncResult(
            added_role_ids=added_ids,
            removed_role_ids=removed_ids,
            skipped_role_ids=tuple(sorted(set(skipped_role_ids))),
            missing_role_ids=tuple(sorted(set(missing_role_ids))),
        )

    async def sync_guild_level_roles(
        self,
        guild: discord.Guild,
        *,
        reason: str = "XP: sincronização geral de cargos de nível",
    ) -> dict[str, int]:
        config = await self.get_guild_config(guild.id)
        stats = {"members": 0, "added": 0, "removed": 0, "skipped": 0, "missing": 0}
        if not config.level_roles:
            return stats
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            self.logger.warning("sync de cargos de nível ignorado por falta de permissão guild_id=%s", guild.id)
            return stats

        index = 0
        async for member in self._iter_members_for_level_role_sync(guild):
            if member.bot:
                continue
            index += 1
            try:
                result = await self.sync_member_level_roles(member, reason=reason)
            except Exception as exc:
                log_exception(exc)
                self.logger.exception("falha ao sincronizar cargos de nível guild_id=%s user_id=%s", guild.id, member.id)
                continue
            stats["members"] += 1
            stats["added"] += len(result.added_role_ids)
            stats["removed"] += len(result.removed_role_ids)
            stats["skipped"] += len(result.skipped_role_ids)
            stats["missing"] += len(result.missing_role_ids)
            if index % 25 == 0:
                await asyncio.sleep(1)
        return stats

    async def cleanup_removed_level_roles(
        self,
        guild: discord.Guild,
        role_ids: set[int],
        *,
        reason: str = "XP: limpeza de cargo de nível removido da configuração",
    ) -> dict[str, int]:
        config = await self.get_guild_config(guild.id)
        stale_role_ids = set(role_ids) - set(config.level_roles.values())
        stats = {"members": 0, "removed": 0, "skipped": 0, "missing": 0}
        for role_id in sorted(stale_role_ids):
            role, manageable = self._manageable_level_role(guild, role_id)
            if role is None:
                stats["missing"] += 1
                continue
            if not manageable:
                stats["skipped"] += 1
                continue
            index = 0
            async for member in self._iter_members_for_level_role_sync(guild):
                if member.bot or role not in member.roles:
                    continue
                index += 1
                try:
                    await member.remove_roles(role, reason=reason)
                    stats["members"] += 1
                    stats["removed"] += 1
                except (discord.Forbidden, discord.HTTPException):
                    stats["skipped"] += 1
                if index % 25 == 0:
                    await asyncio.sleep(1)
        return stats

    async def _iter_members_for_level_role_sync(self, guild: discord.Guild):
        if guild.chunked:
            for member in guild.members:
                yield member
            return
        try:
            async for member in guild.fetch_members(limit=None):
                yield member
        except (discord.Forbidden, discord.HTTPException):
            self.logger.warning("nao consegui buscar membros para sync de cargos guild_id=%s", guild.id)
            for member in guild.members:
                yield member

    def _resolve_category_id(self, message: discord.Message) -> int | None:
        category_id = getattr(message.channel, "category_id", None)
        if category_id is not None:
            return int(category_id)
        parent = getattr(message.channel, "parent", None)
        parent_category_id = getattr(parent, "category_id", None)
        if parent_category_id is not None:
            return int(parent_category_id)
        return None

    def _basic_message_checks(self, message: discord.Message, config: GuildXpConfig) -> tuple[bool, str | None, str | None]:
        if message.guild is None:
            return False, "dm", None
        if not isinstance(message.author, discord.Member):
            return False, "autor_invalido", None
        if config.ignore_bots and message.author.bot:
            return False, "bot", None
        if config.ignore_webhooks and message.webhook_id is not None:
            return False, "webhook", None
        if message.channel.id in config.ignored_channel_ids:
            return False, "canal_ignorado", None
        category_id = self._resolve_category_id(message)
        if category_id is not None and category_id in config.ignored_category_ids:
            return False, "categoria_ignorada", None
        if any(role.id in config.ignored_role_ids for role in message.author.roles):
            return False, "cargo_ignorado", None

        normalized = normalize_message_content(message.content)
        if len(normalized) < config.min_message_length:
            return False, "mensagem_curta", None
        unique_words = {token for token in normalized.split(" ") if token}
        if len(unique_words) < config.min_unique_words:
            return False, "poucas_palavras_unicas", None
        return True, None, normalized

    def _passes_local_repeat_check(
        self,
        guild_id: int,
        user_id: int,
        normalized: str,
        config: GuildXpConfig,
        now_ts: float,
    ) -> tuple[bool, str | None]:
        recent = self._recent_fingerprints[(guild_id, user_id)]
        cutoff = now_ts - float(config.anti_repeat_window_seconds)
        while recent and recent[0][1] < cutoff:
            recent.popleft()
        for previous, _ in recent:
            if previous == normalized:
                return False, "mensagem_repetida"
            similarity = difflib.SequenceMatcher(a=previous, b=normalized).ratio()
            if similarity >= config.anti_repeat_similarity:
                return False, "mensagem_muito_parecida"
        return True, None

    def _remember_fingerprint(self, guild_id: int, user_id: int, normalized: str, now_ts: float) -> None:
        self._recent_fingerprints[(guild_id, user_id)].append((normalized, now_ts))

    async def _build_base_vinculo_context(self, base_xp: int, *, source: str = "none") -> VinculoXpContext:
        return calculate_vinculo_xp_context(
            base_xp=base_xp,
            bonds=(),
            penalties=(),
            source=source,
        )

    async def _resolve_vinculo_context(
        self,
        guild_id: int,
        user_id: int,
        base_xp: int,
        awarded_at_iso: str,
    ) -> VinculoXpContext:
        rich_getter = getattr(self.vinculos_provider, "get_xp_context", None) if self.vinculos_provider else None
        if callable(rich_getter):
            try:
                rich_value = await rich_getter(guild_id, user_id, base_xp)
                if isinstance(rich_value, VinculoXpContext):
                    return rich_value
            except Exception as exc:
                log_exception(exc)
                self.logger.exception(
                    "falha ao calcular contexto rico de vínculos guild_id=%s user_id=%s",
                    guild_id,
                    user_id,
                )
                return await self._build_base_vinculo_context(base_xp, source="provider_failed")

        try:
            rich_context = await self.repository.get_vinculo_xp_context(
                guild_id=guild_id,
                user_id=user_id,
                base_xp=base_xp,
                awarded_at_iso=awarded_at_iso,
                resonance_window_seconds=VINCULO_RESONANCE_WINDOW_SECONDS,
            )
            if rich_context.source != "none":
                return rich_context
        except Exception as exc:
            log_exception(exc)
            self.logger.exception(
                "falha ao calcular contexto de vínculos guild_id=%s user_id=%s",
                guild_id,
                user_id,
            )

        if self.vinculos_provider is None:
            return await self._build_base_vinculo_context(base_xp)

        try:
            multiplier = await self.vinculos_provider.get_xp_multiplier(guild_id, user_id)
            multiplier_value = float(multiplier)
        except Exception as exc:
            log_exception(exc)
            self.logger.exception(
                "falha ao calcular multiplicador legado de vínculos guild_id=%s user_id=%s",
                guild_id,
                user_id,
            )
            return await self._build_base_vinculo_context(base_xp, source="provider_failed")

        safe_base_xp = max(0, int(base_xp))
        return VinculoXpContext(
            base_xp=safe_base_xp,
            final_xp=max(0, int(round(safe_base_xp * multiplier_value))),
            final_multiplier=multiplier_value,
            positive_bonus_rate=max(0.0, multiplier_value - 1.0),
            penalty_rate=max(0.0, 1.0 - multiplier_value),
            positive_bonus_pool=0,
            penalty_pool=0,
            source="legacy_multiplier",
        )

    async def process_message(self, message: discord.Message) -> XpChangeResult | None:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return None
        config = await self.get_guild_config(message.guild.id)
        allowed, _, normalized = self._basic_message_checks(message, config)
        if not allowed or normalized is None:
            return None

        base_delta_xp = self.rng.randint(config.min_xp_per_message, config.max_xp_per_message)
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = utc_now()
        now_iso = now.isoformat()
        cooldown_cutoff_iso = (now - timedelta(seconds=config.cooldown_seconds)).isoformat()
        repeat_cutoff_iso = (now - timedelta(seconds=config.anti_repeat_window_seconds)).isoformat()
        lock = self._user_locks[(message.guild.id, message.author.id)]

        async with lock:
            passes_repeat, _ = self._passes_local_repeat_check(
                message.guild.id,
                message.author.id,
                normalized,
                config,
                now.timestamp(),
            )
            if not passes_repeat:
                return None
            vinculo_context = await self._resolve_vinculo_context(
                message.guild.id,
                message.author.id,
                base_delta_xp,
                now_iso,
            )
            awarded, _, old_total, new_total = await self.repository.try_add_message_xp(
                guild_id=message.guild.id,
                user_id=message.author.id,
                delta_xp=vinculo_context.final_xp,
                last_known_name=message.author.display_name,
                awarded_at_iso=now_iso,
                cooldown_cutoff_iso=cooldown_cutoff_iso,
                message_hash=fingerprint,
                repeat_cutoff_iso=repeat_cutoff_iso,
                vinculo_context=vinculo_context,
            )
            if not awarded:
                return None
            self._remember_fingerprint(message.guild.id, message.author.id, normalized, now.timestamp())

        old_progress = build_progress_snapshot(old_total, config.difficulty)
        new_progress = build_progress_snapshot(new_total, config.difficulty)
        return XpChangeResult(
            awarded=True,
            reason=None,
            old_total_xp=old_total,
            new_total_xp=new_total,
            old_level=old_progress.level,
            new_level=new_progress.level,
            levels_gained=max(0, new_progress.level - old_progress.level),
            delta_xp=vinculo_context.final_xp,
        )

    async def grant_level_rewards(self, member: discord.Member, new_level: int) -> list[discord.Role]:
        result = await self.sync_member_level_roles(member, reason=f"XP: Nível {new_level} alcançado")
        return [role for role in member.guild.roles if role.id in result.added_role_ids]

    async def get_rank_snapshot(self, guild: discord.Guild, user: discord.Member | discord.User) -> RankSnapshot:
        config = await self.get_guild_config(guild.id)
        profile = await self.repository.get_profile(guild.id, user.id)
        progress = build_progress_snapshot(profile.total_xp, config.difficulty)
        position = await self.repository.get_rank_position(guild.id, user.id, profile.total_xp)
        return RankSnapshot(
            guild_id=guild.id,
            user_id=user.id,
            display_name=getattr(user, "display_name", getattr(user, "name", str(user.id))),
            total_xp=profile.total_xp,
            level=progress.level,
            xp_into_level=progress.xp_into_level,
            xp_for_next_level=progress.xp_for_next_level,
            remaining_to_next=progress.remaining_to_next,
            progress_ratio=progress.progress_ratio,
            position=position,
        )

    async def get_leaderboard_entries(self, guild: discord.Guild, limit: int) -> list[LeaderboardEntry]:
        config = await self.get_guild_config(guild.id)
        profiles = await self.repository.get_top_profiles(guild.id, limit)
        entries: list[LeaderboardEntry] = []
        for index, profile in enumerate(profiles, start=1):
            progress = build_progress_snapshot(profile.total_xp, config.difficulty)
            member = guild.get_member(profile.user_id)
            display_name = profile.last_known_name or (member.display_name if member else f"Alma {profile.user_id}")
            entries.append(
                LeaderboardEntry(
                    position=index,
                    user_id=profile.user_id,
                    display_name=display_name,
                    total_xp=profile.total_xp,
                    level=progress.level,
                    remaining_to_next=progress.remaining_to_next,
                    progress_ratio=progress.progress_ratio,
                )
            )
        return entries

    async def get_leaderboard_page(self, guild: discord.Guild, page: int, page_size: int = 10) -> PageResult:
        page = max(0, page)
        offset = page * page_size
        total_entries = await self.repository.count_ranked_profiles(guild.id)
        config = await self.get_guild_config(guild.id)
        profiles = await self.repository.get_profiles_page(guild.id, offset, page_size)
        entries: list[LeaderboardEntry] = []
        for index, profile in enumerate(profiles, start=offset + 1):
            progress = build_progress_snapshot(profile.total_xp, config.difficulty)
            entries.append(
                LeaderboardEntry(
                    position=index,
                    user_id=profile.user_id,
                    display_name=profile.last_known_name or f"Alma {profile.user_id}",
                    total_xp=profile.total_xp,
                    level=progress.level,
                    remaining_to_next=progress.remaining_to_next,
                    progress_ratio=progress.progress_ratio,
                )
            )
        return PageResult(entries=entries, total_entries=total_entries, page=page, page_size=page_size)

    async def give_xp(self, guild: discord.Guild, member: discord.Member, amount: int, actor_user_id: int | None, reason: str | None) -> XpChangeResult:
        config = await self.get_guild_config(guild.id)
        old_total, new_total = await self.repository.adjust_xp(
            guild_id=guild.id,
            user_id=member.id,
            delta_xp=abs(amount),
            last_known_name=member.display_name,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        old_progress = build_progress_snapshot(old_total, config.difficulty)
        new_progress = build_progress_snapshot(new_total, config.difficulty)
        return XpChangeResult(True, None, old_total, new_total, old_progress.level, new_progress.level, max(0, new_progress.level - old_progress.level), abs(amount))

    async def remove_xp(
        self,
        guild: discord.Guild | int,
        member: discord.Member | discord.User | int,
        amount: int,
        actor_user_id: int | None = None,
        reason: str | None = None,
    ) -> XpChangeResult:
        guild_id = guild if isinstance(guild, int) else guild.id
        user_id = member if isinstance(member, int) else member.id
        display_name = getattr(member, "display_name", str(user_id))
        config = await self.get_guild_config(guild_id)
        old_total, new_total = await self.repository.adjust_xp(
            guild_id=guild_id,
            user_id=user_id,
            delta_xp=-abs(amount),
            last_known_name=display_name,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        old_progress = build_progress_snapshot(old_total, config.difficulty)
        new_progress = build_progress_snapshot(new_total, config.difficulty)
        return XpChangeResult(
            True,
            None,
            old_total,
            new_total,
            old_progress.level,
            new_progress.level,
            new_progress.level - old_progress.level,
            new_total - old_total,
        )

    async def reset_guild_xp(self, guild: discord.Guild, actor_user_id: int) -> int:
        """Apaga os perfis de XP de toda a guild."""
        deleted_count = await self.repository.reset_guild_xp(guild.id, actor_user_id)
        self.logger.info(f"Guild {guild.id} teve o XP zerado por {actor_user_id}. {deleted_count} perfis apagados.")
        return deleted_count

    async def reset_xp(self, guild: discord.Guild, member: discord.Member, actor_user_id: int | None, reason: str | None) -> XpChangeResult:
        config = await self.get_guild_config(guild.id)
        old_total, new_total = await self.repository.reset_profile(guild.id, member.id, actor_user_id, reason)
        old_progress = build_progress_snapshot(old_total, config.difficulty)
        new_progress = build_progress_snapshot(new_total, config.difficulty)
        return XpChangeResult(True, None, old_total, new_total, old_progress.level, new_progress.level, new_progress.level - old_progress.level, -old_total)

    async def purify_member(
        self,
        guild: discord.Guild,
        target_member: discord.Member,
        actor_user_id: int,
    ) -> tuple[int, int, list[discord.Member]]:
        target_profile = await self.repository.get_profile(guild.id, target_member.id)
        total_xp = target_profile.total_xp

        if total_xp <= 0:
            raise ValueError("A alma deste mortal já está seca e não serve para o ritual.")

        top_profiles = await self.repository.get_top_profiles(guild.id, 6)
        top_users = [p for p in top_profiles if p.user_id != target_member.id][:5]

        if not top_users:
            raise ValueError("Não há lordes suficientes no abismo para receber esta oferenda.")

        slice_xp = total_xp // len(top_users)
        top_user_ids = [p.user_id for p in top_users]

        await self.repository.execute_purification(
            guild_id=guild.id,
            target_user_id=target_member.id,
            top_user_ids=top_user_ids,
            slice_xp=slice_xp,
            actor_user_id=actor_user_id,
        )

        await self.sync_member_level_roles(target_member, reason="XP: purificação (alvo)")

        blessed_members: list[discord.Member] = []
        for uid in top_user_ids:
            member = guild.get_member(uid)
            if not member:
                try:
                    member = await guild.fetch_member(uid)
                except (discord.NotFound, discord.HTTPException):
                    continue
            if member:
                blessed_members.append(member)
                await self.sync_member_level_roles(member, reason="XP: purificação (beneficiário)")

        return total_xp, slice_xp, blessed_members
