from __future__ import annotations

"""Cards Visuais Do Sistema De XP Do Baphomet (Monalisa Edition)."""

import asyncio
import io
import aiosqlite
from typing import Iterable

import discord
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from ..utils.xp_models import LeaderboardEntry, RankSnapshot
from core.logger import log_exception
from core.paths import FONTS_DIR


FONT_REGULAR = str(FONTS_DIR / "Poppins-Regular.ttf")
FONT_BOLD = str(FONTS_DIR / "Poppins-Bold.ttf")
FONT_BLACK = str(FONTS_DIR / "Montserrat-Black.ttf")


class XpCardRenderer:
    def __init__(self, font_regular_path: str = FONT_REGULAR, font_bold_path: str = FONT_BOLD, font_black_path: str = FONT_BLACK) -> None:
        self.font_regular_path = font_regular_path
        self.font_bold_path = font_bold_path
        self.font_black_path = font_black_path

    def _font(self, size: int, weight: str = "regular") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if weight == "black":
            path = self.font_black_path
        elif weight == "bold":
            path = self.font_bold_path
        else:
            path = self.font_regular_path
            
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            try:
                # Fallbacks nativos do Linux caso a fonte customizada não exista
                fallback = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if weight in ["bold", "black"] else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                return ImageFont.truetype(fallback, size=size)
            except OSError:
                return ImageFont.load_default()

    async def _read_asset(self, asset: discord.Asset | None) -> bytes | None:
        """Faz o fetch de assets de forma assíncrona (usa aiohttp do discord.py)"""
        if asset is None:
            return None
        try:
            return await asset.read()
        except Exception as exc:
            log_exception(exc)
            return None

    # --- PIL Graphics Functions (CPU Bound - Executadas em Threads) ---

    def _get_dominant_color(self, img: Image.Image) -> tuple[int, int, int]:
        """Redimensiona para 1x1 para obter a cor média."""
        img_1x1 = img.resize((1, 1), resample=Image.Resampling.LANCZOS)
        color = img_1x1.getpixel((0, 0))
        if isinstance(color, tuple) and len(color) >= 3:
            return (color[0], color[1], color[2])
        return (120, 60, 240) # Roxo Baphomet fallback

    def _draw_text(
        self, 
        draw: ImageDraw.ImageDraw, 
        pos: tuple[int, int], 
        text: str, 
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont, 
        fill: tuple, 
        shadow_color: tuple = (0, 0, 0, 180), 
        shadow_offset: tuple[int, int] = (3, 3),
        stroke_width: int = 1,
        stroke_fill: tuple = (0, 0, 0, 150)
    ) -> None:
        """Desenha texto com Shadow e Contorno (Stroke) para legibilidade impecável."""
        x, y = pos
        sx, sy = shadow_offset
        # Drop Shadow Direcional
        if shadow_color[3] > 0:
            draw.text((x + sx, y + sy), text, font=font, fill=shadow_color)
        # Texto principal com Stroke (Borda)
        draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)

    def _create_circular_avatar(self, avatar_bytes: bytes | None, size: int) -> tuple[Image.Image, tuple[int, int, int]]:
        """Recorta um avatar com Anti-Aliasing perfeito e extrai a cor dominante."""
        if avatar_bytes:
            try:
                avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            except Exception as exc:
                log_exception(exc)
                avatar = Image.new("RGBA", (size, size), (86, 64, 134, 255))
        else:
            avatar = Image.new("RGBA", (size, size), (86, 64, 134, 255))

        dominant_color = self._get_dominant_color(avatar)
        
        # Redimensionamento com altíssima qualidade
        avatar = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)

        # Máscara supersampled (3x) para anti-aliasing (bordas perfeitas e suaves)
        mask_size = size * 3
        mask = Image.new("L", (mask_size, mask_size), 0)
        draw_mask = ImageDraw.Draw(mask)
        draw_mask.ellipse((0, 0, mask_size, mask_size), fill=255)
        mask = mask.resize((size, size), Image.Resampling.LANCZOS)

        output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        output.paste(avatar, (0, 0), mask)
        
        return output, dominant_color

    def _create_badge_image(self, badge_image_bytes: bytes | None, size: int) -> Image.Image | None:
        if not badge_image_bytes:
            return None
        try:
            with Image.open(io.BytesIO(badge_image_bytes)) as image:
                image = image.convert("RGBA")
                image.thumbnail((size, size), Image.Resampling.LANCZOS)
                output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                x = (size - image.width) // 2
                y = (size - image.height) // 2
                output.alpha_composite(image, (x, y))
                return output
        except Exception as exc:
            log_exception(exc)
            return None

    def _draw_progress_bar(
        self, 
        draw: ImageDraw.ImageDraw, 
        box: tuple[int, int, int, int], 
        ratio: float, 
        start_color: tuple = (163, 112, 255, 255), 
        end_color: tuple = (0, 255, 255, 255)
    ) -> Image.Image | None:
        """Barra de XP com bordas arredondadas e Gradiente dinâmico."""
        x0, y0, x1, y1 = box
        width = x1 - x0
        height = y1 - y0
        
        # Fundo translúcido da barra
        draw.rounded_rectangle(box, radius=height//2, fill=(0, 0, 0, 140), outline=(255, 255, 255, 40), width=1)
        
        filled_width = int(width * ratio)
        if filled_width < height: # Impede que a barra deforme se estiver muito vazia
             filled_width = min(height, width) if ratio > 0 else 0

        if filled_width > 0:
            # Layer do gradiente
            bar_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            bar_draw = ImageDraw.Draw(bar_layer)
            
            # Máscara para manter os cantos arrendondados perfeitos
            mask = Image.new("L", (width, height), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle((0, 0, filled_width, height), radius=height//2, fill=255)
            
            # Criação do gradiente de cor (Linear)
            for i in range(filled_width):
                r = int(start_color[0] + (end_color[0] - start_color[0]) * (i / width))
                g = int(start_color[1] + (end_color[1] - start_color[1]) * (i / width))
                b = int(start_color[2] + (end_color[2] - start_color[2]) * (i / width))
                a = int(start_color[3] + (end_color[3] - start_color[3]) * (i / width))
                bar_draw.line([(i, 0), (i, height)], fill=(r, g, b, a))
            
            bar_layer.putalpha(mask)
            return bar_layer
        return None

    def _create_glass_background(self, width: int, height: int, image_bytes: bytes | None, fallback_color: tuple = (20, 16, 30, 255)) -> Image.Image:
        """Gera o efeito Glassmorphism imersivo no fundo."""
        bg = None
        if image_bytes:
            try:
                bg = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
            except Exception as exc:
                log_exception(exc)
                pass
        
        if not bg:
            bg = Image.new("RGBA", (width, height), fallback_color)
        else:
            bg = ImageOps.fit(bg, (width, height), method=Image.Resampling.LANCZOS)
            # Blur extremo para efeito "vidro"
            bg = bg.filter(ImageFilter.GaussianBlur(radius=35))
        
        # Overlay escuro para forçar legibilidade de elementos claros
        overlay = Image.new("RGBA", (width, height), (15, 10, 25, 170))
        return Image.alpha_composite(bg, overlay)

    def _truncate(self, value: str, max_chars: int) -> str:
        return value if len(value) <= max_chars else value[: max_chars - 1].rstrip() + "…"

    def _build_rank_card_image(
        self,
        guild_name: str,
        snapshot: RankSnapshot,
        avatar_bytes: bytes | None,
        banner_bytes: bytes | None,
        badge_image_bytes: bytes | None = None,
        bond_count: int = 0,
        bond_multiplier: float = 1.0,
    ) -> io.BytesIO:
        """Função Síncrona e bloqueante que constrói o card (Deve rodar em thread)"""
        width, height = 1040, 420
        
        # Background Glassmorphism (Usa Banner. Se não houver, usa Avatar)
        bg_source = banner_bytes if banner_bytes else avatar_bytes
        canvas = self._create_glass_background(width, height, bg_source)
        
        # Layer de Alpha Compositing
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        
        # Caixa principal fosca e elegante
        card_box = (24, 24, width - 24, height - 24)
        draw.rounded_rectangle(card_box, radius=32, fill=(0, 0, 0, 100), outline=(255, 255, 255, 45), width=2)
        
        # Avatar
        avatar_size = 190
        avatar, dom_color = self._create_circular_avatar(avatar_bytes, avatar_size)
        
        # Borda do Avatar (Sutil e dinâmica baseada na cor da imagem)
        border_size = avatar_size + 14
        draw.ellipse((60 - 7, 75 - 7, 60 + border_size - 7, 75 + border_size - 7), fill=dom_color + (255,))
        layer.paste(avatar, (60, 75), avatar)
        
        # Elementos de Texto
        guild_text = self._truncate(guild_name, 28).upper()
        user_name = self._truncate(snapshot.display_name, 20)
        rank_text = f"#{snapshot.position}" if snapshot.position is not None else " "
        progress_text = f"{snapshot.xp_into_level:,} / {snapshot.xp_for_next_level:,} XP"
        
        # Textos Refinados com Tipografia Avançada
        self._draw_text(draw, (300, 60), guild_text, font=self._font(26, weight="bold"), fill=(210, 210, 210, 255))
        self._draw_text(draw, (300, 110), user_name, font=self._font(56, weight="bold"), fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0,0,0,100))
        
        self._draw_text(draw, (300, 200), f"LVL {snapshot.level}", font=self._font(48, weight="black"), fill=dom_color + (255,), stroke_width=2, stroke_fill=(0,0,0,180))
        self._draw_text(draw, (490, 222), f"Total: {snapshot.total_xp:,} XP", font=self._font(22, weight="regular"), fill=(220, 220, 220, 255))
        
        self._draw_text(draw, (width - 340, 60), rank_text, font=self._font(36, weight="black"), fill=(255, 220, 50, 255), stroke_width=2, stroke_fill=(0,0,0,120))
        
        # Barra de Progresso
        bar_box = (300, 260, width - 60, 295)
        # Gradiente da cor primária do avatar para neon cyan
        gradient_bar = self._draw_progress_bar(draw, bar_box, snapshot.progress_ratio, start_color=dom_color+(255,), end_color=(0,255,255,255))
        if gradient_bar:
            layer.paste(gradient_bar, (bar_box[0], bar_box[1]), gradient_bar)
            
        # Texto dentro da barra matematicamente centralizado
        text_font = self._font(20, weight="bold")
        text_bbox = draw.textbbox((0, 0), progress_text, font=text_font)
        text_w = text_bbox[2] - text_bbox[0]
        text_x = bar_box[0] + (bar_box[2] - bar_box[0]) // 2 - text_w // 2
        self._draw_text(draw, (text_x, bar_box[1] + 5), progress_text, font=text_font, fill=(255, 255, 255, 255), shadow_color=(0,0,0,255), stroke_width=2, stroke_fill=(0,0,0,255))

        # Faixa extra discreta: preserva o card original e adiciona apenas os novos dados.
        extra_box = (300, 322, width - 60, 382)
        draw.rounded_rectangle(extra_box, radius=20, fill=(0, 0, 0, 85), outline=(255, 255, 255, 32), width=1)

        badge = self._create_badge_image(badge_image_bytes, 48)
        text_start_x = extra_box[0] + 22
        if badge is not None:
            badge_x = extra_box[0] + 16
            badge_y = extra_box[1] + (extra_box[3] - extra_box[1] - badge.height) // 2
            layer.paste(badge, (badge_x, badge_y), badge)
            text_start_x = badge_x + badge.width + 18

        bond_text = f"Vínculos: {max(0, int(bond_count))} ({max(0.0, float(bond_multiplier)):.1f}x)"
        self._draw_text(
            draw,
            (text_start_x, extra_box[1] + 17),
            bond_text,
            font=self._font(24, weight="bold"),
            fill=(235, 235, 235, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0, 160),
        )
        
        # Mesclar as Layers
        final_img = Image.alpha_composite(canvas, layer)
        output = io.BytesIO()
        final_img.save(output, format="PNG")
        output.seek(0)
        return output

    def _draw_achievements(self, base_image: Image.Image, asset_paths: list[str]) -> Image.Image:
        import os
        x_offset = base_image.width - 340
        y_offset = 120
        spacing = 50
        
        for path in asset_paths:
            if not os.path.exists(path):
                continue
            try:
                badge = Image.open(path).convert("RGBA")
                badge.thumbnail((40, 40), Image.Resampling.LANCZOS)
                layer = Image.new("RGBA", base_image.size, (0, 0, 0, 0))
                layer.paste(badge, (x_offset, y_offset))
                base_image = Image.alpha_composite(base_image, layer)
                x_offset += spacing
            except Exception as exc:
                continue
        return base_image

    async def render_rank_card(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member | discord.User,
        snapshot: RankSnapshot,
        badge_image_bytes: bytes | None = None,
        bond_count: int = 0,
        bond_multiplier: float = 1.0,
        db_conn: aiosqlite.Connection | None = None,
    ) -> io.BytesIO:
        """API Assíncrona. Fetch concorrente de imagens, injeção relacional SQL e execução em ThreadPool."""
        
        asset_paths = []
        if db_conn is not None:
            try:
                async with db_conn.execute(
                    """
                    SELECT a.asset_path 
                    FROM xp_db.user_achievements u
                    JOIN xp_db.achievements_def a ON u.achievement_id = a.id
                    WHERE u.guild_id = ? AND u.user_id = ?
                    """,
                    (guild.id, member.id)
                ) as cursor:
                    rows = await cursor.fetchall()
                    asset_paths = [row[0] for row in rows]
            except Exception as exc:
                log_exception(exc)
                
        # Download paralelo
        tasks = [self._read_asset(member.display_avatar)]
        
        # Tenta buscar o banner, requer um objeto User (Member não possui .banner carregado por padrão)
        if getattr(member, "banner", None) is not None:
            tasks.append(self._read_asset(member.banner))
        else:
            # Caso não tenha o banner em cache, tentamos usar a foto base
            async def null_coro(): return None
            tasks.append(null_coro())
            
        results = await asyncio.gather(*tasks)
        avatar_bytes = results[0]
        banner_bytes = results[1]
        
        # Offload para a Thread
        raw_io = await asyncio.to_thread(
            self._build_rank_card_image,
            guild.name,
            snapshot,
            avatar_bytes,
            banner_bytes,
            badge_image_bytes,
            bond_count,
            bond_multiplier,
        )
        
        if asset_paths:
            base_img = Image.open(raw_io)
            loop = asyncio.get_running_loop()
            final_img = await loop.run_in_executor(None, self._draw_achievements, base_img, asset_paths)
            out_io = io.BytesIO()
            final_img.save(out_io, format="PNG")
            out_io.seek(0)
            return out_io
            
        return raw_io

    def _build_leaderboard_image(self, guild_name: str, icon_bytes: bytes | None, entries_data: list[tuple[LeaderboardEntry, bytes | None, bytes | None]]) -> io.BytesIO:
        """Constrói o leaderboard com alinhamento matemático e espaçamento premium."""
        width = 1200
        row_height = 150
        top_padding = 220
        bottom_padding = 60
        
        # Altura dinâmica à prova de futuro
        height = top_padding + (row_height * max(1, len(entries_data))) + bottom_padding
        
        canvas = self._create_glass_background(width, height, icon_bytes)
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        
        draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=35, fill=(0, 0, 0, 110), outline=(255, 255, 255, 30), width=2)
        
        self._draw_text(draw, (60, 50), "LEADERBOARD", font=self._font(64, weight="black"), fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0,0,0,100))
        self._draw_text(draw, (65, 130), self._truncate(guild_name, 45), font=self._font(32, weight="regular"), fill=(210, 210, 210, 255))
        
        # Cores Premium para o Pódio
        medal_colors = {1: (255, 215, 0, 255), 2: (211, 211, 211, 255), 3: (205, 127, 50, 255)}
        
        if not entries_data:
            self._draw_text(draw, (60, 230), "Nenhuma lenda emergiu neste servidor ainda.", font=self._font(30, weight="regular"), fill=(180, 180, 180, 255))
        else:
            for index, (entry, avatar_bytes, badge_bytes) in enumerate(entries_data, start=1):
                y = top_padding + ((index - 1) * row_height)
                
                # Container da Linha (Maior respiro, y + 130)
                bg_alpha = 150 if index == 1 else 110 if index == 2 else 90 if index == 3 else 60
                outline_color = (255, 255, 255, 40) if index <= 3 else None
                draw.rounded_rectangle((50, y, width - 50, y + 130), radius=28, fill=(0, 0, 0, bg_alpha), outline=outline_color, width=2)
                
                # Badge de Posição Matematicamente Alinhado
                badge_x, badge_w, badge_h = 80, 90, 70
                badge_color = medal_colors.get(index, (80, 80, 80, 200))
                draw.rounded_rectangle((badge_x, y + 30, badge_x + badge_w, y + 30 + badge_h), radius=20, fill=badge_color)
                
                # Texto centralizado no badge usando textbbox real
                badge_font = self._font(36, weight="black")
                text_bbox = draw.textbbox((0,0), f"#{index}", font=badge_font)
                text_w = text_bbox[2] - text_bbox[0]
                text_h = text_bbox[3] - text_bbox[1]
                pos_x = badge_x + (badge_w - text_w)//2
                # Ajuste óptico no Y por causa do topo das fontes maiúsculas
                pos_y = y + 30 + (badge_h - text_h)//2 - 6 
                self._draw_text(draw, (pos_x, pos_y), f"#{index}", font=badge_font, fill=(0, 0, 0, 255), shadow_color=(0,0,0,0), stroke_width=0)
                
                # Avatar (Fixo no eixo X = 200)
                avatar, dom_color = self._create_circular_avatar(avatar_bytes, 90)
                # Borda Dinâmica
                draw.ellipse((196, y + 16, 294, y + 114), fill=dom_color + (255,))
                layer.paste(avatar, (200, y + 20), avatar)
                
                # Nome e Info (Fixo no eixo X = 320)
                display_name = self._truncate(entry.display_name, 18)
                name_color = badge_color if index <= 3 else (255, 255, 255, 255)
                name_font = self._font(34, weight="bold")
                self._draw_text(draw, (320, y + 30), display_name, font=name_font, fill=name_color, stroke_width=1)
                
                if badge_bytes:
                    badge_size = 40
                    badge_img = self._create_badge_image(badge_bytes, badge_size)
                    if badge_img:
                        name_bbox = draw.textbbox((0, 0), display_name, font=name_font)
                        name_w = name_bbox[2] - name_bbox[0]
                        name_h = name_bbox[3] - name_bbox[1]
                        icon_x = 320 + name_w + 12
                        icon_y = y + 30 + (name_h - badge_size) // 2 + 5
                        layer.paste(badge_img, (icon_x, icon_y), badge_img)
                
                self._draw_text(draw, (320, y + 78), f"LVL {entry.level}  •  {entry.total_xp:,} XP", font=self._font(24, weight="regular"), fill=(200, 200, 200, 255))
                
                # Progress Bar (Fixo no eixo X = 780 para não colidir com os nomes longos)
                bar_box = (780, y + 50, 1120, y + 70)
                grad_start = badge_color if index <= 3 else dom_color + (255,)
                gradient_bar = self._draw_progress_bar(draw, bar_box, entry.progress_ratio, start_color=grad_start, end_color=(0, 255, 255, 255))
                if gradient_bar:
                    layer.paste(gradient_bar, (bar_box[0], bar_box[1]), gradient_bar)
                    
                # Texto Progress Menor
                self._draw_text(draw, (780, y + 80), f"Faltam {entry.remaining_to_next:,} XP", font=self._font(18, weight="regular"), fill=(180, 180, 180, 255))
                
        final_img = Image.alpha_composite(canvas, layer)
        output = io.BytesIO()
        final_img.save(output, format="PNG")
        output.seek(0)
        return output

    async def render_leaderboard_card(
        self,
        *,
        guild: discord.Guild,
        entries: Iterable[tuple[LeaderboardEntry, discord.Member | discord.User | None, bytes | None]],
    ) -> io.BytesIO:
        """Busca todas as imagens (Icone, Avatares) e delega para thread pool."""
        entries_list = list(entries)
        
        # Download paralelo do logo do server e avatares
        tasks = [self._read_asset(guild.icon)]
        for _, member, _badge in entries_list:
            if member:
                tasks.append(self._read_asset(member.display_avatar))
            else:
                async def null_coro(): return None
                tasks.append(null_coro())
                
        results = await asyncio.gather(*tasks)
        icon_bytes = results[0]
        avatars_bytes = results[1:]
        
        # Monta payload para o Worker
        entries_data = [(entry, avatar, badge) for (entry, _, badge), avatar in zip(entries_list, avatars_bytes)]
        
        return await asyncio.to_thread(self._build_leaderboard_image, guild.name, icon_bytes, entries_data)
