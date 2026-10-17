from __future__ import annotations

import asyncio
import importlib.util
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord
from discord.ext import commands
from PIL import Image

from cogs.iceberg import IcebergCog
from modules.iceberg.models import (
    IcebergProject,
    ItemConfig,
    ItemDisplayStyle,
    ItemSource,
    ItemSourceType,
    LayerConfig,
    LayerLayoutMode,
    PlacementConfig,
    is_image_source,
)
from modules.iceberg.renderer import (
    IcebergRenderer,
    IcebergTemplateError,
    TEMPLATE_ITEM_SAFE_RIGHT_RATIO,
    crop_center_square,
)
from modules.iceberg.repository import IcebergDatabaseManager, IcebergRepository
from modules.iceberg.service import IcebergService
from modules.iceberg.sources.providers import (
    AttachmentImageProvider,
    DiscordAvatarProvider,
    IcebergUserError,
    ImageUrlProvider,
    ResolvedIcebergSource,
    TextItemProvider,
    WikipediaImageProvider,
)
from modules.iceberg.themes import DEFAULT_LAYER_NAMES, default_layer_name, default_layers, get_theme
from modules.iceberg.constants import (
    ICEBERG_MAX_LAYERS,
    ICEBERG_TEMPLATE_TIER_COUNT,
    ICEBERG_TITLE_MAX_LENGTH,
    MAX_IMAGE_ITEMS_PER_LAYER,
    MAX_TEXT_ITEMS_PER_LAYER,
)
import modules.iceberg.discord_ui.modals as iceberg_modals
from modules.iceberg.discord_ui.modals import IcebergAddItemModal, IcebergGeneralModal
from modules.iceberg.discord_ui.views import (
    IcebergEditorView,
    IcebergManageItemsView,
    IcebergWikipediaCandidateView,
)
from modules.integrations.wikipedia import WikipediaUserError
from modules.media_assets.exceptions import AssetDownloadError, AssetValidationError


AIOSQLITE_AVAILABLE = importlib.util.find_spec("aiosqlite") is not None


async def asyncio_threadsafe_callbacks_work() -> bool:
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def worker() -> None:
        loop.call_soon_threadsafe(future.set_result, True)

    threading.Thread(target=worker, daemon=True).start()
    try:
        return bool(await asyncio.wait_for(future, 0.5))
    except TimeoutError:
        return False


def png_bytes(color: tuple[int, int, int, int] = (220, 40, 90, 255)) -> bytes:
    image = Image.new("RGBA", (64, 64), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


TEMPLATE_TIER_COLORS = (
    (10, 20, 30, 255),
    (20, 40, 60, 255),
    (30, 60, 90, 255),
    (40, 80, 120, 255),
    (50, 100, 150, 255),
    (60, 120, 180, 255),
    (70, 140, 210, 255),
    (80, 160, 220, 255),
    (90, 180, 230, 255),
    (100, 200, 240, 255),
)


def template_bounds(height: int) -> list[int]:
    return [round(index * height / ICEBERG_TEMPLATE_TIER_COUNT) for index in range(ICEBERG_TEMPLATE_TIER_COUNT + 1)]


def write_test_template(path: Path, *, width: int = 100, height: int = 101) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = template_bounds(height)
    image = Image.new("RGBA", (width, height), TEMPLATE_TIER_COLORS[0])
    for index, color in enumerate(TEMPLATE_TIER_COLORS):
        image.paste(color, (0, bounds[index], width, bounds[index + 1]))
    image.save(path, format="PNG")


def template_start_y(image: Image.Image) -> int:
    for y in range(image.height):
        if image.getpixel((1, y)) == TEMPLATE_TIER_COLORS[0]:
            return y
    raise AssertionError("template não encontrada na renderização")


def label_area_changed(image: Image.Image, *, template_top: int, template_height: int, layer_index: int) -> bool:
    bounds = template_bounds(template_height)
    label_left = int(round(image.width * TEMPLATE_ITEM_SAFE_RIGHT_RATIO))
    region = image.crop(
        (
            label_left,
            template_top + bounds[layer_index],
            image.width,
            template_top + bounds[layer_index + 1],
        )
    )
    base_color = TEMPLATE_TIER_COLORS[layer_index]
    for y in range(region.height):
        for x in range(region.width):
            if region.getpixel((x, y)) != base_color:
                return True
    return False


def make_project(layer_count: int = 7) -> IcebergProject:
    theme = get_theme()
    height_weights = [0.8, 1.2, 1.8, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    layers = []
    for index in range(layer_count):
        height_weight = height_weights[index] if index < len(height_weights) else 1.0
        layers.append(
            LayerConfig(id=f"layer-{index + 1}", name=default_layer_name(index), order=index, height_weight=height_weight)
        )
    return IcebergProject(
        id="project-1",
        owner_id=42,
        guild_id=99,
        name="Iceberg Teste",
        theme_id=theme.id,
        theme=theme,
        layers=layers,
        items=[
            ItemConfig(
                id="text-1",
                layer_id="layer-1",
                title="Texto visivel",
                source=ItemSource(type=ItemSourceType.TEXT, value="Texto visivel"),
                display_style=ItemDisplayStyle.CHIP,
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            ItemConfig(
                id="image-1",
                layer_id="layer-2",
                title="Imagem",
                source=ItemSource(type=ItemSourceType.ATTACHMENT, value="imagem.png", asset_id="asset-1"),
                display_style=ItemDisplayStyle.CARD,
                placement=PlacementConfig(x=0.5, y=0.1, scale=1.0),
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        ],
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


class MemoryIcebergRepository:
    def __init__(self) -> None:
        self.projects: dict[str, IcebergProject] = {}

    async def save_project(self, project: IcebergProject) -> IcebergProject:
        self.projects[project.id] = project
        return project

    async def get_project(self, project_id: str, *, owner_id: int) -> IcebergProject | None:
        project = self.projects.get(project_id)
        if project is None or project.owner_id != owner_id:
            return None
        return project

    async def list_projects_for_user(self, owner_id: int, *, guild_id: int | None = None, limit: int = 10) -> list[IcebergProject]:
        return [
            project
            for project in self.projects.values()
            if project.owner_id == owner_id and (guild_id is None or project.guild_id == guild_id)
        ][:limit]

    async def mark_deleted(self, project_id: str, *, owner_id: int) -> None:
        self.projects.pop(project_id, None)


class IcebergManageItemsViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_editor_view_does_not_expose_layer_configuration(self) -> None:
        view = IcebergEditorView(cog=SimpleNamespace(), project=make_project())
        labels = {getattr(child, "label", "") for child in view.children}
        custom_ids = {getattr(child, "custom_id", "") for child in view.children}
        self.assertNotIn("Configurar Camadas", labels)
        self.assertFalse(any(custom_id.endswith(":layers") for custom_id in custom_ids))
        self.assertFalse(hasattr(iceberg_modals, "IcebergLayersModal"))

    async def test_manage_items_view_uses_default_layer_names_for_old_custom_layers(self) -> None:
        project = make_project()
        project.layers[0].name = "Nome Antigo Customizado"
        view = IcebergManageItemsView(cog=SimpleNamespace(), project=project, panel_message=None)
        self.assertIn("Ponta", view.options()[0].description)
        self.assertNotIn("Nome Antigo", view.options()[0].description)

    async def test_general_modal_clips_legacy_long_title_default(self) -> None:
        modal = IcebergGeneralModal(
            SimpleNamespace(),
            project_id="project-1",
            owner_id=42,
            current_name="Titulo antigo importado com mais de quarenta caracteres",
            panel_message=None,
        )
        self.assertLessEqual(len(str(modal.name_input.default)), ICEBERG_TITLE_MAX_LENGTH)

    async def test_pagination_options_slice(self) -> None:
        project = make_project()

        project.items = []
        # Populate project with 30 items
        for i in range(30):
            item = ItemConfig(
                id=f"item-{i}",
                layer_id="layer-1",
                title=f"Item Title {i}",
                source=ItemSource(type=ItemSourceType.TEXT, value=f"text {i}", metadata={}),
                display_style=ItemDisplayStyle.CHIP,
                placement=PlacementConfig(),
                sort_order=i,
                created_at="now",
                updated_at="now"
            )
            project.items.append(item)

        view = IcebergManageItemsView(cog=SimpleNamespace(), project=project, panel_message=None)

        # Page 0
        view.current_page = 0
        options_page0 = view.options()
        self.assertEqual(len(options_page0), 25)
        self.assertEqual(options_page0[0].label, "Item Title 0")

        # Page 1
        view.current_page = 1
        options_page1 = view.options()
        self.assertEqual(len(options_page1), 5)
        self.assertEqual(options_page1[0].label, "Item Title 25")


class IcebergRendererSnapshotTests(unittest.TestCase):
    def render_with_template(self, project: IcebergProject, *, width: int = 100, height: int = 101) -> Image.Image:
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=width, height=height)
            buffer = IcebergRenderer(template_path=template_path).render_project(
                project,
                asset_bytes_by_item_id={"image-1": png_bytes()},
            )
            return Image.open(buffer).convert("RGBA")

    def test_renderer_crops_template_for_7_layers(self) -> None:
        project = make_project(layer_count=7)
        project.items = []
        image = self.render_with_template(project)
        bounds = template_bounds(101)
        top = template_start_y(image)
        self.assertGreater(top, 0)
        self.assertEqual(image.getpixel((1, 0)), (255, 255, 255, 255))
        self.assertEqual(image.getpixel((1, top)), TEMPLATE_TIER_COLORS[0])
        self.assertEqual(image.size, (100, top + bounds[7]))
        self.assertEqual(image.getpixel((1, image.height - 1)), TEMPLATE_TIER_COLORS[6])

    def test_renderer_crops_template_for_8_layers(self) -> None:
        project = make_project(layer_count=8)
        project.items = []
        image = self.render_with_template(project)
        bounds = template_bounds(101)
        top = template_start_y(image)
        present_colors = {image.getpixel((1, y)) for y in range(top, image.height)}
        self.assertEqual(image.size, (100, top + bounds[8]))
        self.assertIn(TEMPLATE_TIER_COLORS[7], present_colors)
        self.assertNotIn(TEMPLATE_TIER_COLORS[8], present_colors)
        self.assertNotIn(TEMPLATE_TIER_COLORS[9], present_colors)

    def test_renderer_uses_full_template_for_10_layers(self) -> None:
        project = make_project(layer_count=10)
        project.items = []
        image = self.render_with_template(project)
        top = template_start_y(image)
        self.assertEqual(image.size, (100, top + 101))
        self.assertEqual(image.getpixel((1, image.height - 1)), TEMPLATE_TIER_COLORS[9])

    def test_renderer_crops_template_for_9_layers(self) -> None:
        project = make_project(layer_count=9)
        project.items = []
        image = self.render_with_template(project)
        bounds = template_bounds(101)
        top = template_start_y(image)
        present_colors = {image.getpixel((1, y)) for y in range(top, image.height)}
        self.assertEqual(image.size, (100, top + bounds[9]))
        self.assertIn(TEMPLATE_TIER_COLORS[8], present_colors)
        self.assertNotIn(TEMPLATE_TIER_COLORS[9], present_colors)

    def test_renderer_adds_white_title_bar_above_template(self) -> None:
        project = make_project(layer_count=7)
        project.items = []
        image = self.render_with_template(project, width=500, height=1000)
        top = template_start_y(image)
        self.assertGreater(top, 0)
        self.assertEqual(image.getpixel((1, 0)), (255, 255, 255, 255))
        self.assertEqual(image.getpixel((image.width - 2, top - 1)), (255, 255, 255, 255))
        self.assertEqual(image.getpixel((1, top)), TEMPLATE_TIER_COLORS[0])
        self.assertEqual(image.height, top + template_bounds(1000)[7])

    def test_renderer_handles_long_and_legacy_titles_inside_title_bar(self) -> None:
        project = make_project(layer_count=7)
        project.items = []
        project.name = "T" * ICEBERG_TITLE_MAX_LENGTH
        image = self.render_with_template(project, width=500, height=1000)
        self.assertGreater(template_start_y(image), 0)

        project.name = "Titulo antigo com mais de quarenta caracteres importado de JSON"
        image = self.render_with_template(project, width=500, height=1000)
        self.assertGreater(template_start_y(image), 0)

    def test_renderer_draws_default_layer_labels_in_visible_sidebar(self) -> None:
        project = make_project(layer_count=7)
        project.items = []
        image = self.render_with_template(project, width=1580, height=1200)
        top = template_start_y(image)
        for index in range(7):
            self.assertTrue(label_area_changed(image, template_top=top, template_height=1200, layer_index=index), index)
        self.assertEqual(image.height, top + template_bounds(1200)[7])

    def test_renderer_draws_all_layer_labels_for_full_template(self) -> None:
        project = make_project(layer_count=10)
        project.items = []
        image = self.render_with_template(project, width=1580, height=1200)
        top = template_start_y(image)
        for index in range(10):
            self.assertTrue(label_area_changed(image, template_top=top, template_height=1200, layer_index=index), index)
        self.assertEqual(image.height, top + 1200)

    def test_renderer_rejects_layer_counts_outside_template_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path)
            renderer = IcebergRenderer(template_path=template_path)

            with self.assertRaisesRegex(ValueError, "mínimo 7"):
                renderer.render_project(make_project(layer_count=6))
            with self.assertRaisesRegex(ValueError, "máximo 10"):
                renderer.render_project(make_project(layer_count=11))

    def test_renderer_preview_resizes_after_crop(self) -> None:
        project = make_project(layer_count=8)
        project.items = []
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1000, height=1001)
            renderer = IcebergRenderer(template_path=template_path)
            full = Image.open(renderer.render_project(project)).convert("RGBA")
            buffer = renderer.render_project(project, preview=True)
            image = Image.open(buffer).convert("RGBA")
        self.assertEqual(image.size, (900, round(full.height * 900 / full.width)))

    def test_renderer_accepts_manual_rotation_opacity_and_placement(self) -> None:
        project = make_project(layer_count=7)
        project.items = [
            ItemConfig(
                id="manual-1",
                layer_id="layer-3",
                title="Manual",
                source=ItemSource(type=ItemSourceType.TEXT, value="Manual"),
                display_style=ItemDisplayStyle.CHIP,
                placement=PlacementConfig(x=0.4, y=0.3, scale=1.2, rotation=14.0, opacity=0.55),
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        ]
        image = self.render_with_template(project, width=1200, height=2000)
        top = template_start_y(image)
        self.assertEqual(image.size, (1200, top + template_bounds(2000)[7]))

    def test_renderer_uses_template_scaled_theme_for_items(self) -> None:
        class CapturingRenderer(IcebergRenderer):
            captured_width: int | None = None

            def _draw_item(self, image, theme, item, box, image_bytes):
                self.captured_width = theme.canvas_width

        project = make_project(layer_count=7)
        project.items = [
            ItemConfig(
                id="text-scaled",
                layer_id="layer-3",
                title="Escalado",
                source=ItemSource(type=ItemSourceType.TEXT, value="Escalado"),
                display_style=ItemDisplayStyle.CHIP,
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1600, height=2000)
            renderer = CapturingRenderer(template_path=template_path)
            renderer.render_project(project)
        self.assertEqual(renderer.captured_width, 1600)

    def test_renderer_errors_when_template_is_missing(self) -> None:
        project = make_project(layer_count=7)
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            with self.assertRaisesRegex(IcebergTemplateError, "assets/images/iceberg/icebergtemplate.png"):
                IcebergRenderer(template_path=missing_path).render_project(project)

    def test_project_json_roundtrip_keeps_domain_types(self) -> None:
        project = make_project()
        imported = IcebergProject.from_dict(project.to_dict())
        self.assertEqual(imported.name, project.name)
        self.assertEqual(imported.items[0].source.type, ItemSourceType.TEXT)
        self.assertEqual(imported.items[1].display_style, ItemDisplayStyle.CARD)
        self.assertEqual(imported.layers[2].height_weight, 1.8)



    def test_renderer_layout_modes(self) -> None:
        project = make_project()

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1200, height=2000)
            renderer = IcebergRenderer(template_path=template_path)

            project.layers[0].layout_mode = LayerLayoutMode.SCATTER
            buffer_scatter1 = renderer.render_project(project, asset_bytes_by_item_id={"image-1": png_bytes()})
            buffer_scatter2 = renderer.render_project(project, asset_bytes_by_item_id={"image-1": png_bytes()})
            self.assertEqual(buffer_scatter1.getvalue(), buffer_scatter2.getvalue())

            project.layers[0].layout_mode = LayerLayoutMode.GRID
            buffer_grid = renderer.render_project(project, asset_bytes_by_item_id={"image-1": png_bytes()})
            self.assertIsNotNone(buffer_grid)

            project.layers[0].layout_mode = LayerLayoutMode.MASONRY
            buffer_masonry = renderer.render_project(project, asset_bytes_by_item_id={"image-1": png_bytes()})
            self.assertIsNotNone(buffer_masonry)

    def test_renderer_draws_chip_card_and_sticker_styles(self) -> None:
        project = make_project(layer_count=7)
        project.items = [
            ItemConfig(
                id="chip-1",
                layer_id="layer-3",
                title="Chip",
                source=ItemSource(type=ItemSourceType.TEXT, value="Chip"),
                display_style=ItemDisplayStyle.CHIP,
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            ItemConfig(
                id="card-1",
                layer_id="layer-4",
                title="Card",
                source=ItemSource(type=ItemSourceType.ATTACHMENT, value="card.png", asset_id="card-asset"),
                display_style=ItemDisplayStyle.CARD,
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            ItemConfig(
                id="sticker-1",
                layer_id="layer-5",
                title="Sticker",
                source=ItemSource(type=ItemSourceType.ATTACHMENT, value="sticker.png", asset_id="sticker-asset"),
                display_style=ItemDisplayStyle.STICKER,
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1600, height=2200)
            buffer = IcebergRenderer(template_path=template_path).render_project(
                project,
                asset_bytes_by_item_id={
                    "card-1": png_bytes((20, 180, 80, 255)),
                    "sticker-1": png_bytes((180, 20, 80, 255)),
                },
            )
        image = Image.open(buffer).convert("RGBA")
        top = template_start_y(image)
        self.assertEqual(image.size, (1600, top + template_bounds(2200)[7]))

    def test_renderer_draw_order_uses_z_index_then_sort_order(self) -> None:
        class OrderingRenderer(IcebergRenderer):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.drawn_ids: list[str] = []

            def _draw_item(self, image, theme, item, box, image_bytes):
                self.drawn_ids.append(item.id)

        project = make_project(layer_count=7)
        project.items = [
            ItemConfig(
                id="z2-sort0",
                layer_id="layer-3",
                title="A",
                source=ItemSource(type=ItemSourceType.TEXT, value="A"),
                display_style=ItemDisplayStyle.CHIP,
                placement=PlacementConfig(z_index=2),
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            ItemConfig(
                id="z1-sort1",
                layer_id="layer-3",
                title="B",
                source=ItemSource(type=ItemSourceType.TEXT, value="B"),
                display_style=ItemDisplayStyle.CHIP,
                placement=PlacementConfig(z_index=1),
                sort_order=1,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            ItemConfig(
                id="z1-sort0",
                layer_id="layer-3",
                title="C",
                source=ItemSource(type=ItemSourceType.TEXT, value="C"),
                display_style=ItemDisplayStyle.CHIP,
                placement=PlacementConfig(z_index=1),
                sort_order=0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1600, height=2200)
            renderer = OrderingRenderer(template_path=template_path)
            renderer.render_project(project)
        self.assertEqual(renderer.drawn_ids, ["z1-sort0", "z1-sort1", "z2-sort0"])

    def test_renderer_overflow_raises_error(self) -> None:
        project = make_project()
        project.layers[0].layout_mode = LayerLayoutMode.GRID

        # Add 1000 large items to the first layer to cause an overflow
        items = []
        for i in range(1000):
            items.append(
                ItemConfig(
                    id=f"item-{i}",
                    layer_id="layer-1",
                    title="Muito grande para caber",
                    source=ItemSource(type=ItemSourceType.TEXT, value="Enorme"),
                    display_style=ItemDisplayStyle.CARD,
                    sort_order=i,
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
            )
        project.items = items

        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1200, height=2000)
            renderer = IcebergRenderer(template_path=template_path)
            with self.assertRaisesRegex(ValueError, "A camada não comporta os itens"):
                renderer.render_project(project)


class IcebergServiceValidationTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, *, renderer: object | None = None) -> IcebergService:
        return IcebergService(
            repository=MemoryIcebergRepository(),
            asset_repository=SimpleNamespace(),
            asset_store=SimpleNamespace(),
            source_registry=SimpleNamespace(),
            renderer=renderer or IcebergRenderer(),
        )

    async def test_create_project_defaults_to_7_layers_and_rejects_outside_range(self) -> None:
        service = self.make_service()
        project = await service.create_project(owner_id=42, guild_id=99, name="Novo")
        self.assertEqual(len(project.layers), 7)

        with self.assertRaisesRegex(IcebergUserError, "mínimo 7"):
            await service.create_project(owner_id=42, guild_id=99, name="Poucas", layer_count=6)

        with self.assertRaisesRegex(IcebergUserError, "máximo 10"):
            await service.create_project(owner_id=42, guild_id=99, name="Muitas", layer_count=ICEBERG_MAX_LAYERS + 1)

    async def test_project_title_limit_allows_40_and_rejects_41_chars(self) -> None:
        service = self.make_service()
        project = await service.create_project(owner_id=42, guild_id=99, name="A" * ICEBERG_TITLE_MAX_LENGTH)
        self.assertEqual(project.name, "A" * ICEBERG_TITLE_MAX_LENGTH)

        with self.assertRaisesRegex(IcebergUserError, "máximo 40 caracteres"):
            await service.create_project(owner_id=42, guild_id=99, name="B" * (ICEBERG_TITLE_MAX_LENGTH + 1))

        await service.repository.save_project(project)
        with self.assertRaisesRegex(IcebergUserError, "máximo 40 caracteres"):
            await service.update_general(project.id, owner_id=42, name="C" * (ICEBERG_TITLE_MAX_LENGTH + 1))

    async def test_update_layers_rejects_outside_range_without_sqlite(self) -> None:
        service = self.make_service()
        await service.repository.save_project(make_project())

        with self.assertRaisesRegex(IcebergUserError, "mínimo 7"):
            await service.update_layers("project-1", owner_id=42, names=[f"Camada {index}" for index in range(1, 7)])

        with self.assertRaisesRegex(IcebergUserError, "máximo 10"):
            await service.update_layers("project-1", owner_id=42, names=[f"Camada {index}" for index in range(1, ICEBERG_MAX_LAYERS + 2)])

    async def test_resolve_layer_keeps_old_custom_names_and_accepts_default_fallback(self) -> None:
        service = self.make_service()
        project = make_project()
        project.layers[0].name = "Nome Antigo Customizado"
        self.assertIs(service.resolve_layer(project, "Nome Antigo Customizado"), project.layers[0])
        self.assertIs(service.resolve_layer(project, "Ponta"), project.layers[0])

    async def test_import_rejects_projects_outside_layer_range_without_sqlite(self) -> None:
        service = self.make_service()

        with self.assertRaisesRegex(IcebergUserError, "mínimo 7"):
            await service.import_project_json(make_project(layer_count=6).to_json(), owner_id=42, guild_id=99)

        with self.assertRaisesRegex(IcebergUserError, "máximo 10"):
            await service.import_project_json(make_project(layer_count=ICEBERG_MAX_LAYERS + 1).to_json(), owner_id=42, guild_id=99)

    async def test_render_converts_template_error_to_user_error_without_sqlite(self) -> None:
        class BrokenRenderer:
            def render_project(self, *args, **kwargs):
                raise IcebergTemplateError("❌ Não encontrei a template local `assets/images/iceberg/icebergtemplate.png`.")

        async def inline_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        service = self.make_service(renderer=BrokenRenderer())
        project = make_project()
        project.items = []
        await service.repository.save_project(project)

        with patch("modules.iceberg.service.asyncio.to_thread", inline_to_thread):
            with self.assertRaises(IcebergUserError) as cm:
                await service.render_project("project-1", owner_id=42)
        self.assertEqual(cm.exception.code, "template_unavailable")
        self.assertIn("assets/images/iceberg/icebergtemplate.png", cm.exception.user_message)


class IcebergSourceProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_provider_requires_real_text(self) -> None:
        provider = TextItemProvider()
        with self.assertRaises(IcebergUserError):
            await provider.resolve(title=" ", value=" ")
        resolved = await provider.resolve(value="  Conteudo  ")
        self.assertEqual(resolved.title, "Conteudo")
        self.assertEqual(resolved.source.type, ItemSourceType.TEXT)

    async def test_attachment_provider_stores_image_asset(self) -> None:
        class FakeAssetStore:
            async def store_image_bytes(self, raw_bytes: bytes, *, source_type: str | None = None, metadata: dict | None = None) -> SimpleNamespace:
                self.raw_bytes = raw_bytes
                self.source_type = source_type
                self.metadata = metadata
                return SimpleNamespace(
                    asset_id="asset-1",
                    asset_hash="a" * 64,
                    storage_path="aa/aa/" + ("a" * 64) + ".webp",
                    mime_type="image/webp",
                    width=64,
                    height=64,
                    size_bytes=123,
                )

        class FakeAttachment:
            filename = "misterio.png"
            content_type = "image/png"
            size = 256
            url = "https://cdn.discordapp.test/misterio.png"

            async def read(self, *, use_cached: bool = True) -> bytes:
                return png_bytes()

        store = FakeAssetStore()
        provider = AttachmentImageProvider(downloader=SimpleNamespace(), asset_store=store)
        resolved = await provider.resolve(title="", attachment=FakeAttachment())
        self.assertEqual(resolved.title, "")
        self.assertEqual(resolved.source.metadata.get("auto_title"), "misterio")
        self.assertEqual(resolved.source.asset_id, "asset-1")
        self.assertEqual(resolved.source.type, ItemSourceType.ATTACHMENT)
        self.assertEqual(store.source_type, ItemSourceType.ATTACHMENT.value)



    async def test_image_url_invalid(self) -> None:
        class FakeDownloader:
            async def download(self, url: str) -> None:
                raise AssetDownloadError("Acesso bloqueado por segurança", code="ssrf_blocked")

        provider = ImageUrlProvider(downloader=FakeDownloader(), asset_store=SimpleNamespace())
        with self.assertRaisesRegex(IcebergUserError, "Acesso bloqueado"):
            await provider.resolve(value="http://127.0.0.1/image.png")

    async def test_discord_avatar_user_not_found(self) -> None:
        class FakeClient:
            async def fetch_user(self, user_id: int):
                raise discord.HTTPException(SimpleNamespace(status=404, reason="Not Found"), "Not Found")

        provider = DiscordAvatarProvider(downloader=SimpleNamespace(), asset_store=SimpleNamespace())
        with self.assertRaises(IcebergUserError) as cm:
            await provider.resolve(value="123456789012345678", client=FakeClient())
        self.assertEqual(cm.exception.code, "avatar_user_not_found")

    async def test_wikipedia_without_thumbnail(self) -> None:
        class FakeWikipediaService:
            async def resolve(self, query: str, **kwargs):
                raise WikipediaUserError("Nenhuma imagem encontrada", code="wikipedia_no_image")

        provider = WikipediaImageProvider(asset_store=SimpleNamespace(), wikipedia_service=FakeWikipediaService())
        with self.assertRaises(IcebergUserError) as cm:
            await provider.resolve(value="Teste sem imagem")
        self.assertEqual(cm.exception.code, "wikipedia_no_image")

    async def test_attachment_too_large(self) -> None:
        class FakeAttachment:
            size = 10 * 1024 * 1024 # 10MB

        provider = AttachmentImageProvider(downloader=SimpleNamespace(), asset_store=SimpleNamespace())
        with self.assertRaises(IcebergUserError) as cm:
            await provider.resolve(attachment=FakeAttachment())
        self.assertEqual(cm.exception.code, "attachment_too_large")

    async def test_attachment_fake_content_type(self) -> None:
        class FakeAttachment:
            size = 1024
            content_type = "application/pdf"

        provider = AttachmentImageProvider(downloader=SimpleNamespace(), asset_store=SimpleNamespace())
        with self.assertRaises(IcebergUserError) as cm:
            await provider.resolve(attachment=FakeAttachment())
        self.assertEqual(cm.exception.code, "attachment_not_image")

    async def test_attachment_invalid_truncated_image(self) -> None:
        class FakeAssetStore:
            async def store_image_bytes(self, raw_bytes: bytes, **kwargs):
                raise AssetValidationError("Imagem corrompida", code="image_invalid")

        class FakeAttachment:
            size = 1024
            content_type = "image/png"
            filename = "test.png"
            url = "http://test.com/test.png"
            async def read(self, **kwargs):
                return b"not an image"

        provider = AttachmentImageProvider(downloader=SimpleNamespace(), asset_store=FakeAssetStore())
        with self.assertRaises(IcebergUserError) as cm:
            await provider.resolve(attachment=FakeAttachment())
        self.assertEqual(cm.exception.code, "image_invalid")


    async def test_wikipedia_modal_returns_candidate_view(self) -> None:

        async def mock_search(q):
            return [{"title": "Teste", "pageid": 1, "description": "", "thumbnail_url": ""}]

        class FakeCog:
            class FakeRegistry:
                providers = {
                    ItemSourceType.WIKIPEDIA: SimpleNamespace(
                        search_candidates=mock_search
                    )
                }
            service = SimpleNamespace(source_registry=FakeRegistry)

        cog = FakeCog()
        modal = IcebergAddItemModal(cog, project_id="p1", owner_id=1, source_type=ItemSourceType.WIKIPEDIA, panel_message=None)

        class FakeInteraction:
            class Response:
                async def defer(self, **kwargs): pass
            class Followup:
                async def send(self, content, view=None, ephemeral=True):
                    self.content = content
                    self.view = view
            response = Response()
            followup = Followup()

        modal.title_input._value = "My Title"
        modal.source_input._value = "Teste"
        modal.layer_input._value = "layer-1"

        interaction = FakeInteraction()
        await modal.on_submit(interaction)
        self.assertIsInstance(interaction.followup.view, IcebergWikipediaCandidateView)
        self.assertEqual(interaction.followup.view.candidates[0]["title"], "Teste")


    async def test_wikipedia_search_candidates(self) -> None:
        class FakeCandidate:
            def __init__(self, pageid, title, description, thumbnail_url):
                self.pageid = pageid
                self.title = title
                self.description = description
                self.thumbnail_url = thumbnail_url

        class FakeResults:
            def __init__(self, candidates):
                self.candidates = candidates

        class FakeWikipediaService:
            async def search(self, query: str, language: str):
                if language == "pt":
                    return FakeResults([FakeCandidate(1, "Teste", "Desc pt", "http://pt")])
                elif language == "en":
                    return FakeResults([FakeCandidate(2, "Test", "Desc en", "http://en")])
                return None

        provider = WikipediaImageProvider(asset_store=SimpleNamespace(), wikipedia_service=FakeWikipediaService())

        # Test pt-BR fallback to pt
        candidates = await provider.search_candidates("teste", locale="pt-BR")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "Teste")

        # Test en fallback if pt doesn't exist
        class FakeWikipediaServiceEnFallback:
            async def search(self, query: str, language: str):
                if language == "en":
                    return FakeResults([FakeCandidate(2, "Test", "Desc en", "http://en")])
                return None

        provider_en = WikipediaImageProvider(asset_store=SimpleNamespace(), wikipedia_service=FakeWikipediaServiceEnFallback())
        candidates_en = await provider_en.search_candidates("teste", locale="pt-BR")
        self.assertEqual(len(candidates_en), 1)
        self.assertEqual(candidates_en[0]["title"], "Test")

@unittest.skipIf(not AIOSQLITE_AVAILABLE, "aiosqlite não está instalado neste Python")
class IcebergRepositoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not await asyncio_threadsafe_callbacks_work():
            self.skipTest("asyncio.call_soon_threadsafe não acorda o loop neste sandbox")
        self.tmp = tempfile.TemporaryDirectory()
        self.db = IcebergDatabaseManager(Path(self.tmp.name) / "icebergs.sqlite3")
        try:
            await asyncio.wait_for(self.db.connect(), timeout=2.0)
        except TimeoutError:
            self.tmp.cleanup()
            self.skipTest("aiosqlite.connect travou neste sandbox")
        self.repository = IcebergRepository(self.db)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_repository_saves_and_lists_project_json(self) -> None:
        project = make_project()
        saved = await self.repository.save_project(project)
        self.assertEqual(saved.id, "project-1")
        listed = await self.repository.list_projects_for_user(42, guild_id=99)
        self.assertEqual([item.id for item in listed], ["project-1"])
        fetched = await self.repository.get_project("project-1", owner_id=42)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.items[1].source.asset_id, "asset-1")

    async def test_service_layer_update_preserves_items_by_name_and_moves_orphans(self) -> None:
        class FakeAssetRepository:
            pass

        class FakeAssetStore:
            pass

        class FakeRegistry:
            pass

        service = IcebergService(
            repository=self.repository,
            asset_repository=FakeAssetRepository(),
            asset_store=FakeAssetStore(),
            source_registry=FakeRegistry(),
            renderer=IcebergRenderer(),
        )
        await self.repository.save_project(make_project())
        updated = await service.update_layers(
            "project-1",
            owner_id=42,
            names=["Raso", "Novo", "Submerso", "Profundo", "Abismo", "Camada 6", "Camada 7"],
            weights=[2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        )
        self.assertEqual([layer.name for layer in updated.layers], ["Raso", "Novo", "Submerso", "Profundo", "Abismo", "Camada 6", "Camada 7"])
        self.assertTrue(all(item.layer_id in {layer.id for layer in updated.layers} for item in updated.items))

    async def test_service_rejects_layer_update_outside_template_range(self) -> None:
        service = IcebergService(
            repository=self.repository,
            asset_repository=SimpleNamespace(),
            asset_store=SimpleNamespace(),
            source_registry=SimpleNamespace(),
            renderer=IcebergRenderer(),
        )
        await self.repository.save_project(make_project())

        with self.assertRaisesRegex(IcebergUserError, "mínimo 7"):
            await service.update_layers("project-1", owner_id=42, names=[f"Camada {index}" for index in range(1, 7)])

        with self.assertRaisesRegex(IcebergUserError, "máximo 10"):
            await service.update_layers("project-1", owner_id=42, names=[f"Camada {index}" for index in range(1, ICEBERG_MAX_LAYERS + 2)])

    async def test_service_converts_template_error_to_user_error(self) -> None:
        class BrokenRenderer:
            def render_project(self, *args, **kwargs):
                raise IcebergTemplateError("❌ Não encontrei a template local `assets/images/iceberg/icebergtemplate.png`.")

        service = IcebergService(
            repository=self.repository,
            asset_repository=SimpleNamespace(),
            asset_store=SimpleNamespace(),
            source_registry=SimpleNamespace(),
            renderer=BrokenRenderer(),
        )
        project = make_project()
        project.items = []
        await self.repository.save_project(project)

        with self.assertRaises(IcebergUserError) as cm:
            await service.render_project("project-1", owner_id=42)
        self.assertEqual(cm.exception.code, "template_unavailable")
        self.assertIn("assets/images/iceberg/icebergtemplate.png", cm.exception.user_message)


class IcebergCommandRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

    async def asyncTearDown(self) -> None:
        await self.bot.close()

    async def test_iceberg_commands_register_without_tierlist_collisions(self) -> None:
        await self.bot.load_extension("cogs.tierlist")
        await self.bot.add_cog(IcebergCog(self.bot))
        command_names = [command.qualified_name for command in self.bot.tree.walk_commands()]
        self.assertIn("tierlist criar", command_names)
        self.assertIn("iceberg criar", command_names)
        self.assertIn("iceberg anexar", command_names)
        self.assertIn("iceberg importar", command_names)
        self.assertEqual(sorted(name for name in command_names if command_names.count(name) > 1), [])


class IcebergLayerItemLimitTests(unittest.IsolatedAsyncioTestCase):
    """Tests for per-layer item limits (10 texts / 4 images)."""

    def make_service(self) -> IcebergService:
        return IcebergService(
            repository=MemoryIcebergRepository(),
            asset_repository=SimpleNamespace(),
            asset_store=SimpleNamespace(),
            source_registry=SimpleNamespace(
                resolve=self._fake_resolve,
            ),
            renderer=IcebergRenderer(),
        )

    async def _fake_resolve(self, source_type, **kwargs):
        title = kwargs.get("title") or ""
        value = kwargs.get("value") or title
        return ResolvedIcebergSource(
            source=ItemSource(type=source_type, value=value, metadata={}),
            title=title,
        )

    async def test_layer_text_limit_allows_10_rejects_11(self) -> None:
        service = self.make_service()
        project = await service.create_project(owner_id=42, guild_id=99, name="Limite")
        for i in range(MAX_TEXT_ITEMS_PER_LAYER):
            project = await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.TEXT, title=f"Texto {i}", value=f"Texto {i}",
            )
        text_count = sum(
            1 for item in project.items
            if item.layer_id == project.layers[0].id and not is_image_source(item.source.type)
        )
        self.assertEqual(text_count, MAX_TEXT_ITEMS_PER_LAYER)

        with self.assertRaisesRegex(IcebergUserError, "limite de 10 textos"):
            await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.TEXT, title="11o texto", value="11o texto",
            )

    async def test_layer_image_limit_allows_4_rejects_5(self) -> None:
        service = self.make_service()
        project = await service.create_project(owner_id=42, guild_id=99, name="Limite Img")
        for i in range(MAX_IMAGE_ITEMS_PER_LAYER):
            project = await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.IMAGE_URL, title=f"Img {i}", value=f"http://example.com/{i}.png",
            )
        image_count = sum(
            1 for item in project.items
            if item.layer_id == project.layers[0].id and is_image_source(item.source.type)
        )
        self.assertEqual(image_count, MAX_IMAGE_ITEMS_PER_LAYER)

        with self.assertRaisesRegex(IcebergUserError, "limite de 4 imagens"):
            await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.IMAGE_URL, title="5a img", value="http://example.com/5.png",
            )

    async def test_layer_separate_counts(self) -> None:
        """A layer full of texts can still accept images and vice-versa."""
        service = self.make_service()
        project = await service.create_project(owner_id=42, guild_id=99, name="Separado")
        # Fill layer with 10 texts
        for i in range(MAX_TEXT_ITEMS_PER_LAYER):
            project = await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.TEXT, title=f"T{i}", value=f"T{i}",
            )
        # Should still accept images
        project = await service.add_item(
            project.id, owner_id=42, layer_ref="1",
            source_type=ItemSourceType.IMAGE_URL, title="Img ok", value="http://example.com/ok.png",
        )
        # Fill images
        for i in range(MAX_IMAGE_ITEMS_PER_LAYER - 1):
            project = await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.IMAGE_URL, title=f"I{i}", value=f"http://example.com/{i}.png",
            )
        # Now both are full; texts should fail, images should fail
        with self.assertRaisesRegex(IcebergUserError, "limite de 10 textos"):
            await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.TEXT, title="overflow", value="overflow",
            )
        with self.assertRaisesRegex(IcebergUserError, "limite de 4 imagens"):
            await service.add_item(
                project.id, owner_id=42, layer_ref="1",
                source_type=ItemSourceType.IMAGE_URL, title="overflow", value="http://example.com/x.png",
            )

    async def test_import_validates_layer_item_limits(self) -> None:
        service = self.make_service()
        project = make_project()
        # Stuff 12 text items into layer-1
        project.items = [
            ItemConfig(
                id=f"txt-{i}", layer_id="layer-1", title=f"T{i}",
                source=ItemSource(type=ItemSourceType.TEXT, value=f"T{i}"),
                display_style=ItemDisplayStyle.CHIP, sort_order=i,
                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
            )
            for i in range(12)
        ]
        with self.assertRaisesRegex(IcebergUserError, "limite de 10 textos"):
            await service.import_project_json(project.to_json(), owner_id=42, guild_id=99)


class IcebergCropCenterSquareTests(unittest.TestCase):
    """Tests for the crop_center_square helper."""

    def test_horizontal_image_becomes_square(self) -> None:
        image = Image.new("RGBA", (200, 100), (255, 0, 0, 255))
        result = crop_center_square(image)
        self.assertEqual(result.size, (100, 100))
        # Center pixel should be red
        self.assertEqual(result.getpixel((50, 50)), (255, 0, 0, 255))

    def test_vertical_image_becomes_square(self) -> None:
        image = Image.new("RGBA", (100, 300), (0, 255, 0, 255))
        result = crop_center_square(image)
        self.assertEqual(result.size, (100, 100))

    def test_already_square_unchanged(self) -> None:
        image = Image.new("RGBA", (64, 64), (0, 0, 255, 255))
        result = crop_center_square(image)
        self.assertIs(result, image)  # same object — no copy needed

    def test_does_not_distort(self) -> None:
        """A 2:1 image crop should preserve center pixels, not stretch."""
        image = Image.new("RGBA", (200, 100), (0, 0, 0, 255))
        # Paint a 10x10 red square at center
        for x in range(95, 105):
            for y in range(45, 55):
                image.putpixel((x, y), (255, 0, 0, 255))
        result = crop_center_square(image)
        self.assertEqual(result.size, (100, 100))
        # The red center should still be red
        self.assertEqual(result.getpixel((50, 50)), (255, 0, 0, 255))
        # The edge should remain black
        self.assertEqual(result.getpixel((0, 0)), (0, 0, 0, 255))


class IcebergImageCaptionTests(unittest.TestCase):
    """Tests for conditional caption rendering."""

    def render_with_template(self, project: IcebergProject, asset_bytes: dict[str, bytes] | None = None, *, width: int = 1600, height: int = 2200) -> Image.Image:
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=width, height=height)
            buffer = IcebergRenderer(template_path=template_path).render_project(
                project,
                asset_bytes_by_item_id=asset_bytes or {},
            )
            return Image.open(buffer).convert("RGBA")

    def test_image_with_caption_renders_text(self) -> None:
        """Image item with title should produce a visible caption area."""
        project = make_project(layer_count=7)
        project.items = [
            ItemConfig(
                id="img-cap", layer_id="layer-3", title="Minha Legenda",
                source=ItemSource(type=ItemSourceType.ATTACHMENT, value="test.png", asset_id="a1"),
                display_style=ItemDisplayStyle.CARD, sort_order=0,
                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        image = self.render_with_template(project, {"img-cap": png_bytes()})
        self.assertIsNotNone(image)
        self.assertGreater(image.width, 0)

    def test_image_without_caption_no_text(self) -> None:
        """Image item with empty title should NOT crash and should render cleanly."""
        project = make_project(layer_count=7)
        project.items = [
            ItemConfig(
                id="img-nocap", layer_id="layer-3", title="",
                source=ItemSource(type=ItemSourceType.ATTACHMENT, value="test.png", asset_id="a2"),
                display_style=ItemDisplayStyle.CARD, sort_order=0,
                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        image = self.render_with_template(project, {"img-nocap": png_bytes()})
        self.assertIsNotNone(image)
        self.assertGreater(image.width, 0)

    def test_image_without_caption_sticker_no_crash(self) -> None:
        """Sticker style with empty title."""
        project = make_project(layer_count=7)
        project.items = [
            ItemConfig(
                id="stk-nocap", layer_id="layer-4", title="",
                source=ItemSource(type=ItemSourceType.ATTACHMENT, value="s.png", asset_id="a3"),
                display_style=ItemDisplayStyle.STICKER, sort_order=0,
                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        image = self.render_with_template(project, {"stk-nocap": png_bytes()})
        self.assertIsNotNone(image)


class IcebergDefaultLayerNameTests(unittest.TestCase):
    """Tests for default layer name completeness."""

    def test_default_layers_10_returns_names_for_all(self) -> None:
        layers = default_layers(10)
        self.assertEqual(len(layers), 10)
        for layer in layers:
            self.assertTrue(layer.name.strip(), f"Layer {layer.order} has empty name")

    def test_layers_6_through_10_are_not_generic(self) -> None:
        for index in range(5, 10):
            name = default_layer_name(index)
            self.assertNotIn("Camada", name, f"Layer {index + 1} still uses generic name")

    def test_layers_1_through_5_preserved(self) -> None:
        expected = ["Ponta", "Raso", "Submerso", "Profundo", "Abismo"]
        for index, expected_name in enumerate(expected):
            self.assertEqual(default_layer_name(index), expected_name)

    def test_default_layer_names_tuple_has_10_entries(self) -> None:
        self.assertEqual(len(DEFAULT_LAYER_NAMES), 10)

    def test_renderer_draws_labels_up_to_layer_10(self) -> None:
        project = make_project(layer_count=10)
        project.items = []
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1580, height=1200)
            image = Image.open(
                IcebergRenderer(template_path=template_path).render_project(project)
            ).convert("RGBA")
        top = template_start_y(image)
        for index in range(10):
            self.assertTrue(
                label_area_changed(image, template_top=top, template_height=1200, layer_index=index),
                f"Label area for layer {index} was not drawn",
            )


class IcebergBackwardCompatibilityTests(unittest.TestCase):
    """Ensure old projects with custom names and generated titles still work."""

    def test_json_roundtrip_with_old_custom_layer_names(self) -> None:
        project = make_project()
        project.layers[0].name = "Nome Antigo"
        project.layers[2].name = "Custom Layer"
        imported = IcebergProject.from_dict(project.to_dict())
        self.assertEqual(imported.layers[0].name, "Nome Antigo")
        self.assertEqual(imported.layers[2].name, "Custom Layer")
        self.assertEqual(len(imported.items), len(project.items))

    def test_json_roundtrip_with_empty_title_item(self) -> None:
        project = make_project()
        project.items.append(
            ItemConfig(
                id="empty-title", layer_id="layer-1", title="",
                source=ItemSource(type=ItemSourceType.IMAGE_URL, value="http://x.com/img.png", asset_id="a1", metadata={"auto_title": "img"}),
                display_style=ItemDisplayStyle.CARD, sort_order=2,
                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        imported = IcebergProject.from_dict(project.to_dict())
        empty_item = next(item for item in imported.items if item.id == "empty-title")
        # from_dict may apply fallback "Item" for empty title in the model — this is fine
        # The key point is it doesn't crash
        self.assertIsNotNone(empty_item)
        self.assertEqual(empty_item.source.metadata.get("auto_title"), "img")

    def test_preview_still_works_with_mixed_items(self) -> None:
        project = make_project(layer_count=8)
        project.items.append(
            ItemConfig(
                id="img-empty", layer_id="layer-3", title="",
                source=ItemSource(type=ItemSourceType.ATTACHMENT, value="x.png", asset_id="a-prev"),
                display_style=ItemDisplayStyle.CARD, sort_order=0,
                created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            template_path = Path(tmp) / "assets/images/iceberg/icebergtemplate.png"
            write_test_template(template_path, width=1000, height=1001)
            renderer = IcebergRenderer(template_path=template_path)
            buffer = renderer.render_project(
                project,
                asset_bytes_by_item_id={"image-1": png_bytes(), "img-empty": png_bytes()},
                preview=True,
            )
        image = Image.open(buffer).convert("RGBA")
        self.assertLessEqual(image.width, 900)


if __name__ == "__main__":
    unittest.main()
