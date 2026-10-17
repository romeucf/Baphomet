from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from modules.media_assets.asset_repository import TierAssetRepository
from modules.media_assets.assets import TierTemplateAssetStore
from modules.media_assets.database import DatabaseManager as TierAssetDatabaseManager
from modules.media_assets.downloads import SafeImageDownloader
from modules.integrations.wikipedia import WikipediaImageService

from modules.iceberg.constants import ICEBERG_DEFAULT_LAYERS, ICEBERG_MAX_LAYERS, ICEBERG_MIN_LAYERS, ICEBERG_TITLE_MAX_LENGTH
from modules.iceberg.models import IcebergProject, ItemSourceType
from modules.iceberg.repository import IcebergDatabaseManager, IcebergRepository
from modules.iceberg.renderer import IcebergRenderer
from modules.iceberg.service import IcebergService
from modules.iceberg.sources.providers import IcebergSourceProviderRegistry, IcebergUserError
from modules.iceberg.themes import DEFAULT_THEME_ID, default_layer_name
from core.logger import log_exception



LOGGER = logging.getLogger("baphomet.iceberg.commands")


class IcebergCog(commands.Cog):
    iceberg = app_commands.Group(name="iceberg", description="Crie e renderize icebergs customizáveis.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = IcebergDatabaseManager()
        self.repository = IcebergRepository(self.db)
        self.asset_db = TierAssetDatabaseManager()
        self.asset_repository = TierAssetRepository(self.asset_db)
        self.asset_store = TierTemplateAssetStore(repository=self.asset_repository)
        self.downloader = SafeImageDownloader()
        self.wikipedia_service = WikipediaImageService()
        self.source_registry = IcebergSourceProviderRegistry(
            downloader=self.downloader,
            asset_store=self.asset_store,
            wikipedia_service=self.wikipedia_service,
        )
        self.service = IcebergService(
            repository=self.repository,
            asset_repository=self.asset_repository,
            asset_store=self.asset_store,
            source_registry=self.source_registry,
            renderer=IcebergRenderer(),
        )

    async def start(self) -> None:
        await self.db.connect()
        await self.asset_db.connect()
        LOGGER.info("Iceberg cog inicializado com repository, assets compartilhados e Wikipedia.")

    def cog_unload(self) -> None:
        asyncio.create_task(self.db.close())
        asyncio.create_task(self.asset_db.close())

    @iceberg.command(name="criar", description="Cria um draft de iceberg.")
    @app_commands.guild_only()
    @app_commands.describe(nome="Nome do iceberg", camadas="Quantidade inicial de camadas")
    async def criar(
        self,
        interaction: discord.Interaction,
        nome: app_commands.Range[str, 1, ICEBERG_TITLE_MAX_LENGTH],
        camadas: app_commands.Range[int, ICEBERG_MIN_LAYERS, ICEBERG_MAX_LAYERS] = ICEBERG_DEFAULT_LAYERS,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            project = await self.service.create_project(
                owner_id=interaction.user.id,
                guild_id=interaction.guild_id,
                name=str(nome),
                layer_count=int(camadas),
                theme_id=DEFAULT_THEME_ID,
            )
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_create_failed user_id=%s guild_id=%s", interaction.user.id, interaction.guild_id)
            await interaction.followup.send("❌ Não consegui criar esse iceberg agora. O erro foi registrado.", ephemeral=True)
            return
        await self.send_editor_panel(interaction, project=project, content="✅ Draft criado. Use o painel para editar e finalizar.")

    @iceberg.command(name="abrir", description="Abre o painel de edição de um iceberg seu.")
    @app_commands.guild_only()
    @app_commands.describe(projeto="ID do projeto")
    async def abrir(self, interaction: discord.Interaction, projeto: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            project = await self.service.get_project_for_user(projeto, owner_id=interaction.user.id)
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        await self.send_editor_panel(interaction, project=project)

    @abrir.autocomplete("projeto")
    async def abrir_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.project_choices(interaction, current)

    @iceberg.command(name="meus", description="Lista seus icebergs recentes.")
    @app_commands.guild_only()
    async def meus(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        projects = await self.service.list_projects(owner_id=interaction.user.id, guild_id=interaction.guild_id, limit=10)
        embed = discord.Embed(title="Seus icebergs", color=discord.Color.teal())
        if not projects:
            embed.description = "Você ainda não tem drafts. Crie um com `/iceberg criar`."
        for project in projects:
            embed.add_field(
                name=project.name[:256],
                value=f"`{project.id}`\n{len(project.layers)} camadas • {len(project.items)} itens",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @iceberg.command(name="anexar", description="Adiciona um item usando uma imagem enviada como attachment.")
    @app_commands.guild_only()
    @app_commands.describe(projeto="ID do projeto", camada="Número ou nome da camada", arquivo="Imagem anexada", nome="Legenda opcional")
    async def anexar(
        self,
        interaction: discord.Interaction,
        projeto: str,
        camada: str,
        arquivo: discord.Attachment,
        nome: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            project = await self.service.add_item(
                projeto,
                owner_id=interaction.user.id,
                layer_ref=camada,
                source_type=ItemSourceType.ATTACHMENT,
                title=nome,
                attachment=arquivo,
                interaction=interaction,
            )
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_attachment_add_failed project_id=%s user_id=%s", projeto, interaction.user.id)
            await interaction.followup.send("❌ Não consegui adicionar esse attachment. O erro foi registrado.", ephemeral=True)
            return
        await self.send_editor_panel(interaction, project=project, content="✅ Attachment adicionado como item.")

    @anexar.autocomplete("projeto")
    async def anexar_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.project_choices(interaction, current)

    @iceberg.command(name="renderizar", description="Renderiza a imagem final de um iceberg.")
    @app_commands.guild_only()
    @app_commands.describe(projeto="ID do projeto")
    async def renderizar(self, interaction: discord.Interaction, projeto: str) -> None:
        await self.send_rendered_iceberg(interaction, project_id=projeto)

    @renderizar.autocomplete("projeto")
    async def renderizar_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.project_choices(interaction, current)

    @iceberg.command(name="importar", description="Importa um iceberg a partir de um JSON.")
    @app_commands.guild_only()
    @app_commands.describe(arquivo="Arquivo .json exportado do iceberg")
    async def importar(self, interaction: discord.Interaction, arquivo: discord.Attachment) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if arquivo.size > 256 * 1024:
                raise IcebergUserError("⚠️ Esse JSON é grande demais para importar.", code="import_too_large")
            raw = await arquivo.read(use_cached=True)
            try:
                raw_json = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IcebergUserError("⚠️ Envie um arquivo JSON em UTF-8.", code="import_encoding_invalid") from exc
            project = await self.service.import_project_json(
                raw_json,
                owner_id=interaction.user.id,
                guild_id=interaction.guild_id,
                force_new_id=True,
            )
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_import_failed user_id=%s guild_id=%s", interaction.user.id, interaction.guild_id)
            await interaction.followup.send("❌ Não consegui importar esse iceberg. O erro foi registrado.", ephemeral=True)
            return
        await self.send_editor_panel(interaction, project=project, content="✅ Iceberg importado. Revise o painel antes de finalizar.")

    async def project_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        query = (current or "").casefold().strip()
        projects = await self.service.list_projects(owner_id=interaction.user.id, guild_id=interaction.guild_id, limit=25)
        choices: list[app_commands.Choice[str]] = []
        for project in projects:
            if query and query not in project.name.casefold() and query not in project.id:
                continue
            label = f"{project.name[:60]} ({project.id[:8]})"
            choices.append(app_commands.Choice(name=label[:100], value=project.id))
            if len(choices) >= 25:
                break
        return choices

    async def send_editor_panel(
        self,
        interaction: discord.Interaction,
        *,
        project: IcebergProject,
        content: str | None = None,
    ) -> None:
        from modules.iceberg.discord_ui.views import IcebergEditorView

        await interaction.followup.send(
            content=content,
            embed=self.build_project_embed(project),
            view=IcebergEditorView(cog=self, project=project),
            ephemeral=True,
        )

    async def refresh_panel_message(self, message: discord.Message | None, project: IcebergProject) -> None:
        if message is None:
            return
        from modules.iceberg.discord_ui.views import IcebergEditorView

        try:
            await message.edit(embed=self.build_project_embed(project), view=IcebergEditorView(cog=self, project=project))
        except discord.HTTPException:
            LOGGER.warning("iceberg_panel_refresh_failed project_id=%s message_id=%s", project.id, getattr(message, "id", None))

    def build_project_embed(self, project: IcebergProject) -> discord.Embed:
        embed = discord.Embed(
            title="🧊 Painel De Criação De Iceberg",
            description=(
                f"**Título:** {discord.utils.escape_markdown(project.name)}\n"
                f"**Projeto:** `{project.id}`\n"
                f"**Camadas:** {len(project.layers)}\n"
                f"**Itens:** {len(project.items)}\n\n"
                "Use os botões abaixo para editar título, adicionar itens e finalizar."
            ),
            color=discord.Color.from_rgb(56, 149, 196),
        )
        for index, layer in enumerate(project.ordered_layers()):
            items = project.ordered_items_for_layer(layer.id)
            item_names = []
            for item in items[:6]:
                display = item.title.strip() if item.title else ""
                if not display:
                    display = item.source.metadata.get("auto_title") or item.source.type.value
                item_names.append(display)
            item_summary = ", ".join(item_names)
            if len(items) > 6:
                item_summary += f" +{len(items) - 6}"
            embed.add_field(
                name=f"{index + 1}. {default_layer_name(index)}",
                value=discord.utils.escape_markdown(item_summary) if item_summary else "Sem itens",
                inline=False,
            )
        embed.set_footer(text="Attachment por imagem: use /iceberg anexar, porque modal do Discord não recebe upload.")
        return embed

    async def send_rendered_iceberg(self, interaction: discord.Interaction, *, project_id: str) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
        try:
            project, buffer = await self.service.render_project(project_id, owner_id=interaction.user.id)
        except IcebergUserError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception as exc:
            log_exception(exc)
            LOGGER.exception("iceberg_render_failed project_id=%s user_id=%s", project_id, interaction.user.id)
            await interaction.followup.send("❌ Não consegui renderizar esse iceberg. O erro foi registrado.", ephemeral=True)
            return
        filename = f"iceberg-{project.id[:8]}.png"
        await interaction.followup.send(
            content=f"🧊 **{discord.utils.escape_markdown(project.name)}**",
            file=discord.File(buffer, filename=filename),
        )


async def setup(bot: commands.Bot) -> None:
    cog = IcebergCog(bot)
    await cog.start()
    await bot.add_cog(cog)
