"""
╔══════════════════════════════════════════════════════════════╗
║                  WELCOME COG — Discord Bot                   ║
║  Boas-vindas automáticas com embed configurável via Modal    ║
╚══════════════════════════════════════════════════════════════╝

INSTALAÇÃO:
  1. Coloque este arquivo em: cogs/welcome.py
  2. No seu bot principal (main.py), carregue com:
       await bot.load_extension("cogs.welcome")
  3. As configurações são salvas em: data/welcome_config.json
     (criado automaticamente)

COMANDOS (todos exclusivos para Administradores):
  /welcome setup   — Seleciona canal e abre o Modal de configuração
  /welcome preview — Exibe um preview do embed atual

VARIÁVEIS DISPONÍVEIS NOS CAMPOS DO EMBED:
  {user}          → Menção do membro          (@João)
  {user_name}     → Nome de exibição          (João)
  {user_tag}      → Tag completa              (João#0001)
  {server}        → Nome do servidor
  {member_count}  → Número total de membros
"""

import discord
from discord import app_commands
from discord.ext import commands

import json
import os
import logging
import asyncio
import io
from PIL import Image
from datetime import datetime, timezone
from typing import Optional
from core.logger import log_exception
from core.paths import DATA_DIR


# ─── Logger ────────────────────────────────────────────────────────────────────
log = logging.getLogger("welcome_cog")

# ─── Arquivo de persistência ───────────────────────────────────────────────────
CONFIG_PATH = DATA_DIR / "welcome_config.json"


# ══════════════════════════════════════════════════════════════════════════════
#  Persistência (JSON)
# ══════════════════════════════════════════════════════════════════════════════

def _load_all() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Erro ao ler %s: %s", CONFIG_PATH, exc)
        return {}


def _save_all(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.error("Erro ao salvar %s: %s", CONFIG_PATH, exc)


def config_get(guild_id: int) -> Optional[dict]:
    return _load_all().get(str(guild_id))


def config_set(guild_id: int, cfg: dict) -> None:
    all_cfg = _load_all()
    all_cfg[str(guild_id)] = cfg
    _save_all(all_cfg)


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers de cor e embed
# ══════════════════════════════════════════════════════════════════════════════

_COLOR_NAMES: dict[str, int] = {
    "red":     0xED4245,
    "green":   0x57F287,
    "blue":    0x5865F2,
    "yellow":  0xFEE75C,
    "orange":  0xE67E22,
    "purple":  0x9B59B6,
    "pink":    0xFF69B4,
    "cyan":    0x1ABC9C,
    "white":   0xFFFFFF,
    "black":   0x23272A,
    "gold":    0xF1C40F,
    "blurple": 0x5865F2,
    "fuchsia": 0xEB459E,
}


def resolve_color(value: str) -> int:
    """Converte nome ou código hex para inteiro. Retorna blurple se inválido."""
    val = value.strip().lower()
    if val in _COLOR_NAMES:
        return _COLOR_NAMES[val]
    val = val.lstrip("#").lstrip("0x")
    try:
        result = int(val, 16)
        # Garante que o valor está no intervalo válido para Discord
        return max(0, min(result, 0xFFFFFF))
    except ValueError:
        return 0x5865F2


def _apply_vars(text: str, member: discord.Member) -> str:
    """Substitui variáveis pelo dados reais do membro."""
    return (
        text
        .replace("{user}",         member.mention)
        .replace("{user_name}",    member.display_name)
        .replace("{user_tag}",     str(member))
        .replace("{server}",       member.guild.name)
        .replace("{member_count}", str(member.guild.member_count))
    )


async def build_embed(cfg: dict, member: discord.Member) -> discord.Embed:
    """Constrói o embed real substituindo variáveis pelo membro que entrou."""
    
    try:
        avatar_bytes = await member.display_avatar.read()
        def extract_color():
            with Image.open(io.BytesIO(avatar_bytes)) as img:
                img = img.convert("RGBA")
                img_1x1 = img.resize((1, 1), resample=Image.Resampling.LANCZOS)
                color = img_1x1.getpixel((0, 0))
                if isinstance(color, tuple) and len(color) >= 3:
                    return (color[0] << 16) + (color[1] << 8) + color[2]
                return resolve_color(cfg.get("color", "blurple"))
                
        loop = asyncio.get_running_loop()
        color_val = await loop.run_in_executor(None, extract_color)
    except Exception as e:
        log_exception(e)
        log.warning("Falha ao extrair cor do avatar: %s", e)
        color_val = resolve_color(cfg.get("color", "blurple"))

    embed = discord.Embed(
        title=_apply_vars(cfg.get("title", "Bem-vindo(a)!"), member),
        description=_apply_vars(cfg.get("description", ""), member),
        color=color_val,
        timestamp=datetime.now(timezone.utc),
    )

    # Thumbnail: URL personalizada ou avatar do membro
    thumb = cfg.get("thumbnail_url", "").strip()
    embed.set_thumbnail(url=thumb if thumb else member.display_avatar.url)

    # Banner
    banner = cfg.get("image_url", "").strip()
    if banner:
        embed.set_image(url=banner)

    # Footer
    footer = _apply_vars(cfg.get("footer", "{server}"), member)
    icon = member.guild.icon.url if member.guild.icon else None
    embed.set_footer(text=footer, icon_url=icon)

    return embed


def build_preview_embed(cfg: dict, guild: discord.Guild) -> discord.Embed:
    """Constrói o embed com placeholders legíveis (sem membro real)."""

    def _preview(text: str) -> str:
        return (
            text
            .replace("{user}",         "**@NovoMembro**")
            .replace("{user_name}",    "NovoMembro")
            .replace("{user_tag}",     "NovoMembro#0000")
            .replace("{server}",       guild.name)
            .replace("{member_count}", str(guild.member_count))
        )

    embed = discord.Embed(
        title=_preview(cfg.get("title", "Bem-vindo(a)!")),
        description=_preview(cfg.get("description", "")),
        color=resolve_color(cfg.get("color", "blurple")),
        timestamp=datetime.now(timezone.utc),
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    banner = cfg.get("image_url", "").strip()
    if banner:
        embed.set_image(url=banner)

    footer = _preview(cfg.get("footer", "{server}"))
    icon = guild.icon.url if guild.icon else None
    embed.set_footer(text=footer, icon_url=icon)

    return embed


# ══════════════════════════════════════════════════════════════════════════════
#  Modal de configuração
# ══════════════════════════════════════════════════════════════════════════════

class WelcomeModal(discord.ui.Modal, title="⚙️ Configurar Boas-Vindas"):

    embed_title = discord.ui.TextInput(
        label="Título",
        placeholder="Ex: 🎉 Bem-vindo(a), {user_name}!",
        default="🎉 Bem-vindo(a), {user_name}!",
        max_length=256,
        required=True,
    )

    embed_description = discord.ui.TextInput(
        label="Descrição",
        style=discord.TextStyle.paragraph,
        placeholder="Use {user}, {user_name}, {server}, {member_count}...",
        default=(
            "Olá, {user}! 👋\n\n"
            "Seja muito bem-vindo(a) ao **{server}**!\n"
            "Você é nosso(a) membro de número **{member_count}**. "
            "Esperamos que aproveite bastante!"
        ),
        max_length=4000,
        required=True,
    )

    embed_color = discord.ui.TextInput(
        label="Cor (nome ou hex)",
        placeholder="Ex: blurple | gold | #FF5733 | red",
        default="blurple",
        max_length=30,
        required=False,
    )

    embed_footer = discord.ui.TextInput(
        label="Rodapé (Footer)",
        placeholder="Ex: {server} • Boas-vindas! 🚀",
        default="{server} • Seja bem-vindo(a)!",
        max_length=2048,
        required=False,
    )

    embed_banner = discord.ui.TextInput(
        label="URL da imagem de banner (opcional)",
        placeholder="https://i.imgur.com/exemplo.png  (deixe vazio para não usar)",
        max_length=512,
        required=False,
    )

    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=300)
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        cfg: dict = {
            "channel_id":    self.channel_id,
            "title":         self.embed_title.value.strip(),
            "description":   self.embed_description.value.strip(),
            "color":         self.embed_color.value.strip() or "blurple",
            "footer":        self.embed_footer.value.strip(),
            "image_url":     self.embed_banner.value.strip(),
            "thumbnail_url": "",   # usa avatar do membro por padrão
            "enabled":       True,
            "configured_by": interaction.user.id,
            "configured_at": datetime.now(timezone.utc).isoformat(),
        }

        config_set(interaction.guild_id, cfg)

        channel = interaction.guild.get_channel(self.channel_id)
        preview = build_preview_embed(cfg, interaction.guild)

        info = discord.Embed(
            title="✅ Boas-vindas configuradas!",
            description=(
                f"**Canal:** {channel.mention if channel else f'<#{self.channel_id}>'}\n"
                f"**Cor:** `{cfg['color']}`\n\n"
                "Veja abaixo como ficará o embed quando um novo membro entrar:"
            ),
            color=0x57F287,
        )
        await interaction.followup.send(embeds=[info, preview], ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Erro dentro do WelcomeModal: %s", error)
        msg = "❌ Ocorreu um erro ao salvar. Tente novamente."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  View de seleção de canal
# ══════════════════════════════════════════════════════════════════════════════

class ChannelSelectView(discord.ui.View):

    def __init__(self) -> None:
        super().__init__(timeout=120)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="📢 Selecione o canal de boas-vindas...",
        channel_types=[discord.ChannelType.text],
        min_values=1,
        max_values=1,
    )
    async def channel_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.ChannelSelect,
    ) -> None:
        # ChannelSelect retorna AppCommandChannel (objeto parcial).
        # Resolvemos para o objeto real via guild para ter acesso a permissions_for().
        channel: discord.TextChannel = interaction.guild.get_channel(select.values[0].id)

        if channel is None:
            await interaction.response.send_message(
                "❌ Canal não encontrado. Tente novamente.",
                ephemeral=True,
            )
            return

        # Verifica se o bot tem permissões necessárias no canal
        me = interaction.guild.me
        perms = channel.permissions_for(me)
        if not perms.send_messages or not perms.embed_links:
            missing = []
            if not perms.send_messages:
                missing.append("`Enviar Mensagens`")
            if not perms.embed_links:
                missing.append("`Inserir Links`")
            await interaction.response.send_message(
                f"❌ Não tenho as permissões necessárias em {channel.mention}:\n"
                + "\n".join(f"  • {p}" for p in missing),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(WelcomeModal(channel_id=channel.id))
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════════════════════
#  Cog principal
# ══════════════════════════════════════════════════════════════════════════════

class WelcomeCog(commands.Cog, name="Welcome"):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        log.info("WelcomeCog carregado com sucesso.")

    # ── Métodos de configuração (expostos via /server_config) ──────────────

    async def cmd_setup(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="⚙️ Configuração de Boas-Vindas",
            description=(
                "Selecione abaixo o **canal de texto** onde as mensagens serão enviadas.\n"
                "Após selecionar, um formulário de configuração abrirá.\n\n"
                "**Variáveis disponíveis no embed:**\n"
                "> `{user}` — Menção do membro\n"
                "> `{user_name}` — Nome de exibição\n"
                "> `{user_tag}` — Tag completa (nome#0000)\n"
                "> `{server}` — Nome do servidor\n"
                "> `{member_count}` — Total de membros"
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="Esta mensagem expira em 2 minutos.")
        await interaction.response.send_message(embed=embed, view=ChannelSelectView(), ephemeral=True)

    async def cmd_preview(self, interaction: discord.Interaction) -> None:
        cfg = config_get(interaction.guild_id)
        if not cfg:
            await interaction.response.send_message(
                "❌ Nenhuma configuração encontrada. Use `/server_config boasvindas setup` primeiro.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(cfg["channel_id"])
        status = "✅ Ativo" if cfg.get("enabled", True) else "⛔ Desativado"

        channel_str = channel.mention if channel else f"`ID {cfg['channel_id']}` (não encontrado)"
        info = discord.Embed(
            title="👁️ Preview — Embed de Boas-Vindas",
            description=(
                f"**Canal:** {channel_str}\n"
                f"**Status:** {status}"
            ),
            color=0x5865F2,
        )
        preview = build_preview_embed(cfg, interaction.guild)
        await interaction.response.send_message(embeds=[info, preview], ephemeral=True)

    # ── Tratamento de erros do grupo ────────────────────────────────────────

    async def on_welcome_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ Você precisa ser **administrador** para usar este comando."
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = "❌ Este comando só pode ser usado dentro de um servidor."
        else:
            log.exception("Erro inesperado em comando /welcome: %s", error)
            msg = f"❌ Ocorreu um erro inesperado: `{error}`"

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ── Evento on_member_join ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = config_get(member.guild.id)
        if not cfg or not cfg.get("enabled", True):
            return

        channel: Optional[discord.TextChannel] = member.guild.get_channel(cfg["channel_id"])
        if channel is None:
            log.warning(
                "[Guild %s] Canal de boas-vindas ID=%s não encontrado.",
                member.guild.id,
                cfg["channel_id"],
            )
            return

        perms = channel.permissions_for(member.guild.me)
        if not perms.send_messages or not perms.embed_links:
            log.warning(
                "[Guild %s] Sem permissão para enviar embed em #%s.",
                member.guild.id,
                channel.name,
            )
            return

        try:
            embed = await build_embed(cfg, member)
            sent_msg = await channel.send(embed=embed)
            self.bot.dispatch("welcome_message_sent", member, sent_msg)
            log.info(
                "Boas-vindas enviadas → %s em '%s' (#%s)",
                member, member.guild.name, channel.name,
            )
        except discord.HTTPException as exc:
            log.error("HTTPException ao enviar boas-vindas: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Setup — obrigatório para load_extension
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))

