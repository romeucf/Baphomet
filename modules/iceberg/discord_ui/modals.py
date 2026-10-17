from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from modules.iceberg.constants import ICEBERG_TITLE_MAX_LENGTH
from modules.iceberg.models import ItemSourceType, PlacementConfig
from modules.iceberg.sources.providers import IcebergUserError
from core.logger import log_exception


if TYPE_CHECKING:
    from cogs.iceberg import IcebergCog


LOGGER = logging.getLogger("baphomet.iceberg.modals")


class IcebergGeneralModal(discord.ui.Modal, title="Editar Título"):
    def __init__(self, cog: IcebergCog, *, project_id: str, owner_id: int, current_name: str, panel_message: discord.Message | None) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.project_id = project_id
        self.owner_id = owner_id
        self.panel_message = panel_message
        self.name_input = discord.ui.TextInput(
            label="Título do iceberg",
            default=self._title_default(current_name),
            min_length=1,
            max_length=ICEBERG_TITLE_MAX_LENGTH,
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            project = await self.cog.service.update_general(
                self.project_id,
                owner_id=self.owner_id,
                name=str(self.name_input.value),
            )
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_general_update_failed project_id=%s", self.project_id)
            await interaction.followup.send("❌ Não consegui atualizar esse iceberg. O erro foi registrado.", ephemeral=True)
            return
        await self.cog.refresh_panel_message(self.panel_message, project)
        await interaction.followup.send("✅ Título atualizado.", ephemeral=True)

    def _title_default(self, value: str) -> str:
        title = re.sub(r"\s+", " ", (value or "").strip()) or "Iceberg"
        if len(title) <= ICEBERG_TITLE_MAX_LENGTH:
            return title
        return title[: ICEBERG_TITLE_MAX_LENGTH - 3].rstrip() + "..."


class IcebergAddItemModal(discord.ui.Modal):
    def __init__(self, cog: IcebergCog, *, project_id: str, owner_id: int, source_type: ItemSourceType, panel_message: discord.Message | None) -> None:
        title = {
            ItemSourceType.TEXT: "Adicionar Texto",
            ItemSourceType.IMAGE_URL: "Adicionar Imagem por URL",
            ItemSourceType.DISCORD_AVATAR: "Adicionar Avatar",
            ItemSourceType.WIKIPEDIA: "Adicionar Wikipedia",
        }.get(source_type, "Adicionar Item")
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.project_id = project_id
        self.owner_id = owner_id
        self.source_type = source_type
        self.panel_message = panel_message
        self.layer_input = discord.ui.TextInput(
            label="Camada",
            placeholder="Número ou nome da camada. Ex: 1, Raso, Abismo",
            min_length=1,
            max_length=48,
            required=True,
        )
        self.title_input = discord.ui.TextInput(
            label="Nome/legenda",
            placeholder="Opcional para imagens; obrigatório para texto",
            min_length=0,
            max_length=90,
            required=False,
        )
        self.source_input = discord.ui.TextInput(
            label=self._source_label(source_type),
            placeholder=self._source_placeholder(source_type),
            min_length=1 if source_type is not ItemSourceType.TEXT else 0,
            max_length=500,
            required=source_type is not ItemSourceType.TEXT,
        )
        self.add_item(self.layer_input)
        self.add_item(self.title_input)
        self.add_item(self.source_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_title = str(self.title_input.value or "")
        raw_value = str(self.source_input.value or "")
        if self.source_type is ItemSourceType.TEXT and not raw_value.strip():
            raw_value = raw_title

        if self.source_type is ItemSourceType.WIKIPEDIA:
            try:
                candidates = await self.cog.service.source_registry.providers[ItemSourceType.WIKIPEDIA].search_candidates(raw_value)
                if not candidates:
                    await interaction.followup.send("⚠️ Nenhum resultado encontrado na Wikipedia para essa busca.", ephemeral=True)
                    return

                from modules.iceberg.discord_ui.views import IcebergWikipediaCandidateView
                view = IcebergWikipediaCandidateView(
                    cog=self.cog,
                    project_id=self.project_id,
                    owner_id=self.owner_id,
                    layer_ref=str(self.layer_input.value),
                    title=raw_title,
                    value=raw_value,
                    candidates=candidates,
                    panel_message=self.panel_message,
                )
                await interaction.followup.send("Selecione o candidato correto:", view=view, ephemeral=True)
                return
            except Exception as exc:
                log_exception(exc)
                LOGGER.exception("iceberg_wiki_search_failed project_id=%s", self.project_id)
                await interaction.followup.send("❌ Erro ao buscar na Wikipedia. O erro foi registrado.", ephemeral=True)
                return

        try:
            project = await self.cog.service.add_item(
                self.project_id,
                owner_id=self.owner_id,
                layer_ref=str(self.layer_input.value),
                source_type=self.source_type,
                title=raw_title,
                value=raw_value,
                interaction=interaction,
            )
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_item_add_failed project_id=%s source_type=%s", self.project_id, self.source_type)
            await interaction.followup.send("❌ Não consegui adicionar esse item. O erro foi registrado.", ephemeral=True)
            return
        await self.cog.refresh_panel_message(self.panel_message, project)
        await interaction.followup.send("✅ Item adicionado ao iceberg.", ephemeral=True)

    def _source_label(self, source_type: ItemSourceType) -> str:
        return {
            ItemSourceType.TEXT: "Texto",
            ItemSourceType.IMAGE_URL: "URL da imagem",
            ItemSourceType.DISCORD_AVATAR: "ID ou menção do usuário",
            ItemSourceType.WIKIPEDIA: "Termo da Wikipedia",
        }.get(source_type, "Fonte")

    def _source_placeholder(self, source_type: ItemSourceType) -> str:
        return {
            ItemSourceType.TEXT: "Se vazio, uso o campo Nome/legenda",
            ItemSourceType.IMAGE_URL: "https://exemplo.com/imagem.png",
            ItemSourceType.DISCORD_AVATAR: "123456789012345678",
            ItemSourceType.WIKIPEDIA: "Ex: Duna, Backrooms, Atlantis",
        }.get(source_type, "")


class IcebergEditItemModal(discord.ui.Modal, title="Editar Item"):
    def __init__(self, cog: IcebergCog, *, project_id: str, owner_id: int, item_id: str, current_title: str, current_layer: str, current_placement: PlacementConfig, panel_message: discord.Message | None) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.project_id = project_id
        self.owner_id = owner_id
        self.item_id = item_id
        self.panel_message = panel_message
        placement_default = self._placement_to_text(current_placement)
        self.title_input = discord.ui.TextInput(
            label="Nome/legenda",
            default=current_title,
            min_length=1,
            max_length=90,
            required=True,
        )
        self.layer_input = discord.ui.TextInput(
            label="Camada",
            default=current_layer,
            min_length=1,
            max_length=48,
            required=True,
        )
        self.placement_input = discord.ui.TextInput(
            label="Posição opcional",
            default=placement_default,
            placeholder="auto ou x,y,escala. Ex: 0.5,0.2,1.2",
            min_length=0,
            max_length=40,
            required=False,
        )
        self.add_item(self.title_input)
        self.add_item(self.layer_input)
        self.add_item(self.placement_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            placement = self._parse_placement(str(self.placement_input.value or ""))
            project = await self.cog.service.edit_item(
                self.project_id,
                owner_id=self.owner_id,
                item_id=self.item_id,
                title=str(self.title_input.value),
                layer_ref=str(self.layer_input.value),
                placement=placement,
            )
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_item_edit_failed project_id=%s item_id=%s", self.project_id, self.item_id)
            await interaction.followup.send("❌ Não consegui editar esse item. O erro foi registrado.", ephemeral=True)
            return
        await self.cog.refresh_panel_message(self.panel_message, project)
        await interaction.followup.send("✅ Item atualizado.", ephemeral=True)

    def _placement_to_text(self, placement: PlacementConfig) -> str:
        if placement.x is None or placement.y is None:
            return "auto"
        return f"{placement.x:.2f},{placement.y:.2f},{placement.scale:.2f}"

    def _parse_placement(self, raw: str) -> PlacementConfig:
        value = raw.strip().casefold()
        if not value or value == "auto":
            return PlacementConfig()
        parts = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
        try:
            x = float(parts[0])
            y = float(parts[1])
            scale = float(parts[2]) if len(parts) >= 3 else 1.0
        except (IndexError, ValueError) as exc:
            raise IcebergUserError("⚠️ Posição inválida. Use `auto` ou `x,y,escala`.", code="placement_invalid") from exc
        return PlacementConfig.from_dict({"x": x, "y": y, "scale": scale})
