from __future__ import annotations

import asyncio
import json
import logging

import discord

from modules.media_assets.asset_repository import TierAssetRepository
from modules.media_assets.assets import TierTemplateAssetStore

from modules.iceberg.constants import (
    ICEBERG_DEFAULT_LAYERS,
    ICEBERG_MAX_LAYERS,
    ICEBERG_MIN_LAYERS,
    ICEBERG_TITLE_MAX_LENGTH,
    MAX_IMAGE_ITEMS_PER_LAYER,
    MAX_TEXT_ITEMS_PER_LAYER,
)
from modules.iceberg.models import (
    IcebergProject,
    ItemConfig,
    ItemDisplayStyle,
    ItemSourceType,
    LayerConfig,
    PlacementConfig,
    is_image_source,
    MAX_ITEM_TITLE_LENGTH,
    MAX_LAYER_NAME_LENGTH,
    MAX_PROJECT_NAME_LENGTH,
    new_uuid,
    normalize_text,
    utc_now_iso,
)
from modules.iceberg.renderer import IcebergRenderer, IcebergTemplateError
from modules.iceberg.repository import IcebergRepository
from .sources.providers import IcebergSourceProviderRegistry, IcebergUserError
from modules.iceberg.themes import DEFAULT_THEME_ID, default_layer_name, default_layers, get_theme


LOGGER = logging.getLogger("baphomet.iceberg.service")
MAX_ITEMS_PER_PROJECT = 120


class IcebergService:
    def __init__(
        self,
        *,
        repository: IcebergRepository,
        asset_repository: TierAssetRepository,
        asset_store: TierTemplateAssetStore,
        source_registry: IcebergSourceProviderRegistry,
        renderer: IcebergRenderer | None = None,
    ) -> None:
        self.repository = repository
        self.asset_repository = asset_repository
        self.asset_store = asset_store
        self.source_registry = source_registry
        self.renderer = renderer or IcebergRenderer()

    async def create_project(
        self,
        *,
        owner_id: int,
        guild_id: int | None,
        name: str,
        layer_count: int = ICEBERG_DEFAULT_LAYERS,
        theme_id: str = DEFAULT_THEME_ID,
    ) -> IcebergProject:
        layer_count = self._validate_layer_count(layer_count, code_prefix="layers")
        theme = get_theme(theme_id)
        clean_name = self._clean_project_title(name, fallback="Iceberg")
        now = utc_now_iso()
        project = IcebergProject(
            id=new_uuid(),
            owner_id=int(owner_id),
            guild_id=guild_id,
            name=clean_name,
            theme_id=theme.id,
            theme=theme,
            layers=default_layers(layer_count),
            created_at=now,
            updated_at=now,
        )
        saved = await self.repository.save_project(project)
        LOGGER.info("iceberg_project_created project_id=%s owner_id=%s guild_id=%s", saved.id, owner_id, guild_id)
        return saved

    async def get_project_for_user(self, project_id: str, *, owner_id: int) -> IcebergProject:
        project = await self.repository.get_project(project_id, owner_id=owner_id)
        if project is None:
            raise IcebergUserError("🔎 Não encontrei esse iceberg para a sua conta.", code="project_not_found")
        return project

    async def list_projects(self, *, owner_id: int, guild_id: int | None = None, limit: int = 10) -> list[IcebergProject]:
        return await self.repository.list_projects_for_user(owner_id, guild_id=guild_id, limit=limit)

    async def update_general(
        self,
        project_id: str,
        *,
        owner_id: int,
        name: str | None = None,
    ) -> IcebergProject:
        project = await self.get_project_for_user(project_id, owner_id=owner_id)
        if name is not None:
            project.name = self._clean_project_title(name, fallback=project.name)
        project.touch()
        return await self.repository.save_project(project)

    async def update_layers(
        self,
        project_id: str,
        *,
        owner_id: int,
        names: list[str],
        weights: list[float] | None = None,
    ) -> IcebergProject:
        clean_names = [
            name
            for raw in names
            if (name := normalize_text(raw, max_length=MAX_LAYER_NAME_LENGTH))
        ]
        if not clean_names:
            raise IcebergUserError(f"⚠️ Use no mínimo {ICEBERG_MIN_LAYERS} camadas.", code="layers_too_few")
        self._validate_layer_count(len(clean_names), code_prefix="layers")
        project = await self.get_project_for_user(project_id, owner_id=owner_id)
        old_by_name = {layer.name.casefold(): layer for layer in project.layers}
        old_by_order = {layer.order: layer for layer in project.layers}
        new_layers: list[LayerConfig] = []
        used_ids: set[str] = set()
        weights = weights or []
        for index, name in enumerate(clean_names):
            old_layer = old_by_name.get(name.casefold()) or old_by_order.get(index)
            layer_id = old_layer.id if old_layer is not None else f"layer-{new_uuid()[:8]}"
            while layer_id in used_ids:
                layer_id = f"layer-{new_uuid()[:8]}"
            used_ids.add(layer_id)
            weight = weights[index] if index < len(weights) else (old_layer.height_weight if old_layer else 1.0)
            new_layers.append(
                LayerConfig(
                    id=layer_id,
                    name=name,
                    order=index,
                    height_weight=max(0.15, min(8.0, float(weight))),
                    color=old_layer.color if old_layer else None,
                )
            )
        valid_ids = {layer.id for layer in new_layers}
        fallback_layer_id = new_layers[0].id
        for item in project.items:
            if item.layer_id not in valid_ids:
                item.layer_id = fallback_layer_id
                item.updated_at = utc_now_iso()
        project.layers = new_layers
        self._normalize_item_order(project)
        project.touch()
        return await self.repository.save_project(project)

    async def add_item(
        self,
        project_id: str,
        *,
        owner_id: int,
        layer_ref: str,
        source_type: ItemSourceType,
        title: str | None = None,
        value: str | None = None,
        interaction: discord.Interaction | None = None,
        attachment: discord.Attachment | None = None,
    ) -> IcebergProject:
        project = await self.get_project_for_user(project_id, owner_id=owner_id)
        if len(project.items) >= MAX_ITEMS_PER_PROJECT:
            raise IcebergUserError(f"⚠️ Esse iceberg já atingiu o limite de {MAX_ITEMS_PER_PROJECT} itens.", code="items_limit")
        layer = self.resolve_layer(project, layer_ref)
        self._validate_layer_item_limits(project, layer.id, source_type)
        resolved = await self.source_registry.resolve(
            source_type,
            title=title,
            value=value,
            attachment=attachment,
            client=getattr(interaction, "client", None),
            guild=getattr(interaction, "guild", None),
            guild_id=getattr(interaction, "guild_id", project.guild_id),
            user_id=owner_id,
        )
        now = utc_now_iso()
        # For image items the provider returns "" when user didn't supply a title;
        # for text items, a blank title should fall back to something visible.
        if is_image_source(source_type):
            clean_title = resolved.title  # may be ""
        else:
            clean_title = normalize_text(resolved.title, max_length=MAX_ITEM_TITLE_LENGTH, fallback="Item") or "Item"
        item = ItemConfig(
            id=new_uuid(),
            layer_id=layer.id,
            title=clean_title,
            source=resolved.source,
            display_style=ItemDisplayStyle.CHIP if source_type is ItemSourceType.TEXT else project.default_item_style,
            placement=PlacementConfig(),
            sort_order=self._next_sort_order(project, layer.id),
            created_at=now,
            updated_at=now,
        )
        project.items.append(item)
        project.touch()
        saved = await self.repository.save_project(project)
        LOGGER.info("iceberg_item_added project_id=%s item_id=%s source_type=%s layer_id=%s", project.id, item.id, source_type.value, layer.id)
        return saved

    async def edit_item(
        self,
        project_id: str,
        *,
        owner_id: int,
        item_id: str,
        title: str | None = None,
        layer_ref: str | None = None,
        placement: PlacementConfig | None = None,
    ) -> IcebergProject:
        project = await self.get_project_for_user(project_id, owner_id=owner_id)
        item = self.require_item(project, item_id)
        if title is not None:
            item.title = normalize_text(title, max_length=MAX_ITEM_TITLE_LENGTH, fallback=item.title) or item.title
        if layer_ref:
            new_layer = self.resolve_layer(project, layer_ref)
            if new_layer.id != item.layer_id:
                # Validate limits on destination layer
                self._validate_layer_item_limits(project, new_layer.id, item.source.type, exclude_item_id=item.id)
                item.layer_id = new_layer.id
        if placement is not None:
            item.placement = placement
        item.updated_at = utc_now_iso()
        self._normalize_item_order(project)
        project.touch()
        return await self.repository.save_project(project)

    async def remove_item(self, project_id: str, *, owner_id: int, item_id: str) -> IcebergProject:
        project = await self.get_project_for_user(project_id, owner_id=owner_id)
        before = len(project.items)
        project.items = [item for item in project.items if item.id != item_id]
        if len(project.items) == before:
            raise IcebergUserError("⚠️ Não encontrei esse item no iceberg.", code="item_not_found")
        self._normalize_item_order(project)
        project.touch()
        return await self.repository.save_project(project)

    async def move_item(self, project_id: str, *, owner_id: int, item_id: str, direction: int) -> IcebergProject:
        project = await self.get_project_for_user(project_id, owner_id=owner_id)
        item = self.require_item(project, item_id)
        siblings = project.ordered_items_for_layer(item.layer_id)
        index = next((position for position, sibling in enumerate(siblings) if sibling.id == item_id), None)
        if index is None:
            raise IcebergUserError("⚠️ Não encontrei esse item no iceberg.", code="item_not_found")
        target = max(0, min(len(siblings) - 1, index + direction))
        if target == index:
            return project
        siblings[index], siblings[target] = siblings[target], siblings[index]
        for position, sibling in enumerate(siblings):
            sibling.sort_order = position
            sibling.updated_at = utc_now_iso()
        project.touch()
        return await self.repository.save_project(project)

    async def delete_project(self, project_id: str, *, owner_id: int) -> None:
        await self.repository.mark_deleted(project_id, owner_id=owner_id)

    async def render_project(self, project_id: str, *, owner_id: int, preview: bool = False) -> tuple[IcebergProject, Any]:
        project = await self.get_project_for_user(project_id, owner_id=owner_id)
        self._validate_project_layers(project, code_prefix="render_layers")
        asset_bytes = await self.load_asset_bytes_by_item(project)
        try:
            buffer = await asyncio.to_thread(
                self.renderer.render_project,
                project,
                asset_bytes_by_item_id=asset_bytes,
                preview=preview,
            )
        except IcebergTemplateError as exc:
            raise IcebergUserError(str(exc), code="template_unavailable") from exc
        return project, buffer

    async def load_asset_bytes_by_item(self, project: IcebergProject) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for item in project.items:
            asset_id = item.source.asset_id
            if not asset_id:
                continue
            asset = await self.asset_repository.get_asset(asset_id)
            if asset is None:
                LOGGER.warning("iceberg_asset_missing project_id=%s item_id=%s asset_id=%s", project.id, item.id, asset_id)
                continue
            try:
                result[item.id] = await self.asset_store.load_asset_bytes(asset)
            except (OSError, ValueError):
                LOGGER.exception("iceberg_asset_load_failed project_id=%s item_id=%s asset_id=%s", project.id, item.id, asset_id)
        return result

    async def import_project_json(
        self,
        raw_json: str,
        *,
        owner_id: int,
        guild_id: int | None,
        force_new_id: bool = True,
    ) -> IcebergProject:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise IcebergUserError("⚠️ Esse JSON não parece ser um projeto de iceberg válido.", code="import_json_invalid") from exc
        if not isinstance(payload, dict):
            raise IcebergUserError("⚠️ O JSON do iceberg precisa ser um objeto.", code="import_not_object")
        try:
            project = IcebergProject.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise IcebergUserError("⚠️ Esse JSON não bate com o formato esperado de um iceberg.", code="import_shape_invalid", detail=str(exc)) from exc
        self._validate_project_layers(project, code_prefix="import_layers")
        self._validate_imported_layer_item_limits(project)
        if force_new_id:
            project.id = new_uuid()
        project.owner_id = int(owner_id)
        project.guild_id = guild_id
        default_theme = get_theme(DEFAULT_THEME_ID)
        project.theme_id = default_theme.id
        project.theme = default_theme
        project.default_item_style = ItemDisplayStyle.CARD
        project.touch()
        return await self.repository.save_project(project)

    def resolve_layer(self, project: IcebergProject, layer_ref: str) -> LayerConfig:
        clean = normalize_text(layer_ref, max_length=MAX_LAYER_NAME_LENGTH)
        if clean is None:
            raise IcebergUserError("⚠️ Informe a camada do item.", code="layer_empty")
        if clean.isdigit():
            index = int(clean) - 1
            layers = project.ordered_layers()
            if 0 <= index < len(layers):
                return layers[index]
        by_id = next((layer for layer in project.layers if layer.id == clean), None)
        if by_id is not None:
            return by_id
        by_name = next((layer for layer in project.layers if layer.name.casefold() == clean.casefold()), None)
        if by_name is not None:
            return by_name
        by_default_name = next(
            (layer for index, layer in enumerate(project.ordered_layers()) if default_layer_name(index).casefold() == clean.casefold()),
            None,
        )
        if by_default_name is not None:
            return by_default_name
        raise IcebergUserError("⚠️ Não encontrei essa camada. Use o número ou o nome dela.", code="layer_not_found")

    def require_item(self, project: IcebergProject, item_id: str) -> ItemConfig:
        item = next((candidate for candidate in project.items if candidate.id == item_id), None)
        if item is None:
            raise IcebergUserError("⚠️ Não encontrei esse item no iceberg.", code="item_not_found")
        return item

    def _next_sort_order(self, project: IcebergProject, layer_id: str) -> int:
        items = project.ordered_items_for_layer(layer_id)
        return len(items)

    def _normalize_item_order(self, project: IcebergProject) -> None:
        for layer in project.layers:
            for index, item in enumerate(project.ordered_items_for_layer(layer.id)):
                item.sort_order = index

    def _validate_project_layers(self, project: IcebergProject, *, code_prefix: str) -> int:
        return self._validate_layer_count(len(project.layers), code_prefix=code_prefix)

    def _clean_project_title(self, value: str, *, fallback: str) -> str:
        clean = normalize_text(value, max_length=MAX_PROJECT_NAME_LENGTH, fallback=fallback) or fallback
        if len(clean) > ICEBERG_TITLE_MAX_LENGTH:
            raise IcebergUserError(
                f"⚠️ O título do iceberg pode ter no máximo {ICEBERG_TITLE_MAX_LENGTH} caracteres.",
                code="title_too_long",
            )
        return clean

    def _validate_layer_count(self, layer_count: int, *, code_prefix: str) -> int:
        try:
            count = int(layer_count)
        except (TypeError, ValueError):
            count = 0
        if count < ICEBERG_MIN_LAYERS:
            raise IcebergUserError(f"⚠️ Use no mínimo {ICEBERG_MIN_LAYERS} camadas.", code=f"{code_prefix}_too_few")
        if count > ICEBERG_MAX_LAYERS:
            raise IcebergUserError(f"⚠️ Use no máximo {ICEBERG_MAX_LAYERS} camadas.", code=f"{code_prefix}_too_many")
        return count

    def _validate_layer_item_limits(
        self,
        project: IcebergProject,
        layer_id: str,
        source_type: ItemSourceType,
        *,
        exclude_item_id: str | None = None,
    ) -> None:
        """Check per-layer item limits (10 texts / 4 images) BEFORE saving."""
        layer_items = [
            item for item in project.items
            if item.layer_id == layer_id and item.id != exclude_item_id
        ]
        if is_image_source(source_type):
            image_count = sum(1 for item in layer_items if is_image_source(item.source.type))
            if image_count >= MAX_IMAGE_ITEMS_PER_LAYER:
                raise IcebergUserError(
                    f"⚠️ Essa camada já atingiu o limite de {MAX_IMAGE_ITEMS_PER_LAYER} imagens.",
                    code="layer_image_limit",
                )
        else:
            text_count = sum(1 for item in layer_items if not is_image_source(item.source.type))
            if text_count >= MAX_TEXT_ITEMS_PER_LAYER:
                raise IcebergUserError(
                    f"⚠️ Essa camada já atingiu o limite de {MAX_TEXT_ITEMS_PER_LAYER} textos.",
                    code="layer_text_limit",
                )

    def _validate_imported_layer_item_limits(self, project: IcebergProject) -> None:
        """Validate all layers in an imported project respect per-layer limits."""
        for layer in project.layers:
            layer_items = [item for item in project.items if item.layer_id == layer.id]
            text_count = sum(1 for item in layer_items if not is_image_source(item.source.type))
            image_count = sum(1 for item in layer_items if is_image_source(item.source.type))
            if text_count > MAX_TEXT_ITEMS_PER_LAYER:
                raise IcebergUserError(
                    f"⚠️ A camada '{layer.name}' do JSON importado excede o limite de {MAX_TEXT_ITEMS_PER_LAYER} textos.",
                    code="import_layer_text_limit",
                )
            if image_count > MAX_IMAGE_ITEMS_PER_LAYER:
                raise IcebergUserError(
                    f"⚠️ A camada '{layer.name}' do JSON importado excede o limite de {MAX_IMAGE_ITEMS_PER_LAYER} imagens.",
                    code="import_layer_image_limit",
                )
