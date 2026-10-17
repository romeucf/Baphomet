from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from modules.iceberg.models import IcebergProject, ItemConfig, ItemSourceType
from modules.iceberg.sources.providers import IcebergUserError
from modules.iceberg.themes import default_layer_name
from core.logger import log_exception


if TYPE_CHECKING:
    from cogs.iceberg import IcebergCog


LOGGER = logging.getLogger("baphomet.iceberg.views")
NO_ITEM_VALUE = "__no_item__"


def iceberg_component_id(project_id: str, action: str) -> str:
    return f"ice:{project_id}:{action}"


class IcebergEditorView(discord.ui.View):
    def __init__(self, *, cog: IcebergCog, project: IcebergProject) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.project_id = project.id
        self.owner_id = project.owner_id
        self._apply_custom_ids()

    def _apply_custom_ids(self) -> None:
        self.general_button.custom_id = iceberg_component_id(self.project_id, "general")
        self.add_item_button.custom_id = iceberg_component_id(self.project_id, "add")
        self.manage_items_button.custom_id = iceberg_component_id(self.project_id, "items")
        self.render_button.custom_id = iceberg_component_id(self.project_id, "render")
        self.close_button.custom_id = iceberg_component_id(self.project_id, "close")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("⚠️ Esse painel de iceberg não é seu.", ephemeral=True)
        return False

    @discord.ui.button(label="Editar Título", emoji="✏️", style=discord.ButtonStyle.secondary, row=0, custom_id="iceberg:general")
    async def general_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from modules.iceberg.discord_ui.modals import IcebergGeneralModal

        project = await self.cog.service.get_project_for_user(self.project_id, owner_id=self.owner_id)
        await interaction.response.send_modal(
            IcebergGeneralModal(
                self.cog,
                project_id=project.id,
                owner_id=project.owner_id,
                current_name=project.name,
                panel_message=interaction.message,
            )
        )

    @discord.ui.button(label="Adicionar Item", emoji="➕", style=discord.ButtonStyle.secondary, row=0, custom_id="iceberg:add")
    async def add_item_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = IcebergAddItemView(cog=self.cog, project_id=self.project_id, owner_id=self.owner_id, panel_message=interaction.message)
        await interaction.response.send_message("Escolha a fonte do item.", view=view, ephemeral=True)

    @discord.ui.button(label="Editar Item", emoji="✏️", style=discord.ButtonStyle.secondary, row=0, custom_id="iceberg:items")
    async def manage_items_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        project = await self.cog.service.get_project_for_user(self.project_id, owner_id=self.owner_id)
        view = IcebergManageItemsView(cog=self.cog, project=project, panel_message=interaction.message)
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Finalizar", emoji="🖼️", style=discord.ButtonStyle.success, row=1, custom_id="iceberg:render")
    async def render_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.send_rendered_iceberg(interaction, project_id=self.project_id)

    @discord.ui.button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.danger, row=1, custom_id="iceberg:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


class IcebergAddItemView(discord.ui.View):
    def __init__(self, *, cog: IcebergCog, project_id: str, owner_id: int, panel_message: discord.Message | None) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.project_id = project_id
        self.owner_id = owner_id
        self.panel_message = panel_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("⚠️ Esse seletor não é seu.", ephemeral=True)
        return False

    async def _send_modal(self, interaction: discord.Interaction, source_type: ItemSourceType) -> None:
        from modules.iceberg.discord_ui.modals import IcebergAddItemModal

        await interaction.response.send_modal(
            IcebergAddItemModal(
                self.cog,
                project_id=self.project_id,
                owner_id=self.owner_id,
                source_type=source_type,
                panel_message=self.panel_message,
            )
        )

    @discord.ui.button(label="Texto", emoji="📝", style=discord.ButtonStyle.primary)
    async def text_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._send_modal(interaction, ItemSourceType.TEXT)

    @discord.ui.button(label="URL", emoji="🖼️", style=discord.ButtonStyle.secondary)
    async def url_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._send_modal(interaction, ItemSourceType.IMAGE_URL)

    @discord.ui.button(label="Avatar", emoji="👤", style=discord.ButtonStyle.secondary)
    async def avatar_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._send_modal(interaction, ItemSourceType.DISCORD_AVATAR)

    @discord.ui.button(label="Wikipedia", emoji="🌐", style=discord.ButtonStyle.secondary)
    async def wiki_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._send_modal(interaction, ItemSourceType.WIKIPEDIA)


class IcebergItemSelect(discord.ui.Select):
    def __init__(self, parent: "IcebergManageItemsView") -> None:
        options = parent.options()
        super().__init__(
            placeholder="Escolha um item",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not parent.project.items,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == NO_ITEM_VALUE:
            await interaction.response.send_message("⚠️ Não há itens nesse iceberg.", ephemeral=True)
            return
        self.parent_view.selected_item_id = self.values[0]
        embed = self.parent_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class IcebergWikipediaCandidateSelect(discord.ui.Select):
    def __init__(self, parent: "IcebergWikipediaCandidateView") -> None:
        options = []
        for index, candidate in enumerate(parent.candidates[:25]):
            desc = candidate.get("description", "")
            if len(desc) > 90:
                desc = desc[:87] + "..."
            options.append(
                discord.SelectOption(
                    label=candidate["title"][:100],
                    value=str(index),
                    description=desc or "Sem descrição",
                )
            )
        super().__init__(
            placeholder="Escolha uma página da Wikipedia",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            index = int(self.values[0])
            candidate = self.parent_view.candidates[index]
        except (ValueError, IndexError):
            await interaction.followup.send("⚠️ Candidato inválido.", ephemeral=True)
            return

        try:
            project = await self.parent_view.cog.service.add_item(
                self.parent_view.project_id,
                owner_id=self.parent_view.owner_id,
                layer_ref=self.parent_view.layer_ref,
                source_type=ItemSourceType.WIKIPEDIA,
                title=self.parent_view.title,
                value=candidate["title"],
                interaction=interaction,
            )
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_item_add_failed project_id=%s source_type=WIKIPEDIA", self.parent_view.project_id)
            await interaction.followup.send("❌ Não consegui adicionar esse item. O erro foi registrado.", ephemeral=True)
            return

        await self.parent_view.cog.refresh_panel_message(self.parent_view.panel_message, project)
        await interaction.followup.send("✅ Item da Wikipedia adicionado ao iceberg.", ephemeral=True)


class IcebergWikipediaCandidateView(discord.ui.View):
    def __init__(self, *, cog: IcebergCog, project_id: str, owner_id: int, layer_ref: str, title: str, value: str, candidates: list[dict[str, Any]], panel_message: discord.Message | None) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.project_id = project_id
        self.owner_id = owner_id
        self.layer_ref = layer_ref
        self.title = title
        self.value = value
        self.candidates = candidates
        self.panel_message = panel_message
        self.add_item(IcebergWikipediaCandidateSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("⚠️ Esse seletor não é seu.", ephemeral=True)
        return False


class IcebergManageItemsView(discord.ui.View):
    def __init__(self, *, cog: IcebergCog, project: IcebergProject, panel_message: discord.Message | None) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.project = project
        self.project_id = project.id
        self.owner_id = project.owner_id
        self.panel_message = panel_message
        self.selected_item_id: str | None = None
        self.current_page = 0
        self.add_item(IcebergItemSelect(self))
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("⚠️ Esse gerenciador não é seu.", ephemeral=True)
        return False

    def options(self) -> list[discord.SelectOption]:
        if not self.project.items:
            return [discord.SelectOption(label="Nenhum item", value=NO_ITEM_VALUE)]
        layer_names = {
            layer.id: default_layer_name(index)
            for index, layer in enumerate(self.project.ordered_layers())
        }
        options: list[discord.SelectOption] = []
        ordered = sorted(self.project.items, key=lambda item: (self.project.layer_by_id(item.layer_id).order if self.project.layer_by_id(item.layer_id) else 999, item.sort_order))

        start_idx = self.current_page * 25
        end_idx = start_idx + 25

        for item in ordered[start_idx:end_idx]:
            display_title = item.title.strip() if item.title else ""
            if not display_title:
                display_title = item.source.metadata.get("auto_title") or item.source.type.value
            options.append(
                discord.SelectOption(
                    label=display_title[:100],
                    value=item.id,
                    description=f"{layer_names.get(item.layer_id, 'Camada')} • {item.source.type.value}"[:100],
                    default=item.id == self.selected_item_id,
                )
            )
        return options

    def _layer_display_name(self, layer_id: str) -> str:
        for index, layer in enumerate(self.project.ordered_layers()):
            if layer.id == layer_id:
                return default_layer_name(index)
        return layer_id

    def build_embed(self) -> discord.Embed:
        selected = self._selected_item()
        total_pages = max(1, (len(self.project.items) + 24) // 25)
        embed = discord.Embed(
            title="Itens do iceberg",
            description=f"{len(self.project.items)} itens no projeto. Página {self.current_page + 1}/{total_pages}",
            color=discord.Color.blurple(),
        )
        if selected:
            display_title = selected.title.strip() if selected.title else ""
            if not display_title:
                display_title = selected.source.metadata.get("auto_title") or selected.source.type.value
            embed.add_field(
                name="Selecionado",
                value=f"**{discord.utils.escape_markdown(display_title)}**\nCamada: {self._layer_display_name(selected.layer_id)}",
                inline=False,
            )
        else:
            embed.add_field(name="Selecionado", value="Escolha um item no menu.", inline=False)
        return embed

    def _sync_buttons(self) -> None:
        disabled = self.selected_item_id is None
        self.edit_button.disabled = disabled
        self.remove_button.disabled = disabled
        self.up_button.disabled = disabled
        self.down_button.disabled = disabled

        total_pages = max(1, (len(self.project.items) + 24) // 25)
        self.prev_button.disabled = self.current_page <= 0
        self.next_button.disabled = self.current_page >= total_pages - 1

    def _selected_item(self) -> ItemConfig | None:
        if self.selected_item_id is None:
            return None
        return next((item for item in self.project.items if item.id == self.selected_item_id), None)

    async def _reload(self) -> None:
        self.project = await self.cog.service.get_project_for_user(self.project_id, owner_id=self.owner_id)
        total_pages = max(1, (len(self.project.items) + 24) // 25)
        if self.current_page >= total_pages:
            self.current_page = max(0, total_pages - 1)

        self.clear_items()
        self.add_item(IcebergItemSelect(self))
        self._sync_buttons()
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self.add_item(self.edit_button)
        self.add_item(self.remove_button)
        self.add_item(self.up_button)
        self.add_item(self.down_button)

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.current_page > 0:
            self.current_page -= 1
        await self._reload()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Próximo", emoji="➡️", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        total_pages = max(1, (len(self.project.items) + 24) // 25)
        if self.current_page < total_pages - 1:
            self.current_page += 1
        await self._reload()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Editar Item", emoji="✏️", style=discord.ButtonStyle.secondary, row=2)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from modules.iceberg.discord_ui.modals import IcebergEditItemModal

        selected = self._selected_item()
        if selected is None:
            await interaction.response.send_message("⚠️ Escolha um item primeiro.", ephemeral=True)
            return
        await interaction.response.send_modal(
            IcebergEditItemModal(
                self.cog,
                project_id=self.project_id,
                owner_id=self.owner_id,
                item_id=selected.id,
                current_title=selected.title,
                current_layer=self._layer_display_name(selected.layer_id),
                current_placement=selected.placement,
                panel_message=self.panel_message,
            )
        )

    @discord.ui.button(label="Remover Item", emoji="🗑️", style=discord.ButtonStyle.secondary, row=2)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected_item_id is None:
            await interaction.response.send_message("⚠️ Escolha um item primeiro.", ephemeral=True)
            return
        try:
            project = await self.cog.service.remove_item(self.project_id, owner_id=self.owner_id, item_id=self.selected_item_id)
        except IcebergUserError as exc:
            await interaction.response.send_message(exc.user_message, ephemeral=True)
            return
        self.selected_item_id = None
        await self.cog.refresh_panel_message(self.panel_message, project)
        self.project = project
        await self._reload()
        await interaction.response.edit_message(content="🗑️ Item removido.", embed=self.build_embed(), view=self)

    @discord.ui.button(label="Subir", emoji="⬆️", style=discord.ButtonStyle.secondary, row=2)
    async def up_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._move(interaction, -1)

    @discord.ui.button(label="Descer", emoji="⬇️", style=discord.ButtonStyle.secondary, row=2)
    async def down_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._move(interaction, 1)

    async def _move(self, interaction: discord.Interaction, direction: int) -> None:
        if self.selected_item_id is None:
            await interaction.response.send_message("⚠️ Escolha um item primeiro.", ephemeral=True)
            return
        try:
            project = await self.cog.service.move_item(self.project_id, owner_id=self.owner_id, item_id=self.selected_item_id, direction=direction)
        except IcebergUserError as exc:
            await interaction.response.send_message(exc.user_message, ephemeral=True)
            return
        await self.cog.refresh_panel_message(self.panel_message, project)
        self.project = project
        await self._reload()
        await interaction.response.edit_message(content="🔃 Ordem atualizada.", embed=self.build_embed(), view=self)
