import discord
from discord.ext import commands
import json
import os
from core.paths import DATA_DIR

# Arquivo para salvar as configurações e garantir que não sejam perdidas ao reiniciar (Potência)
CONFIG_FILE = DATA_DIR / "starboard_config.json"

class StarboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = self.load_config()

    def load_config(self):
        """Carrega as configurações do JSON."""
        if not os.path.exists(CONFIG_FILE):
            return {}
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_config(self):
        """Salva as configurações no JSON."""
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4)

    def get_guild_config(self, guild_id):
        """Retorna ou cria a configuração padrão para o servidor."""
        guild_id_str = str(guild_id)
        if guild_id_str not in self.config:
            self.config[guild_id_str] = {
                "mural_channel": None,
                "monitored_channels": [],
                "threshold": 5,
                "starred_messages": [] # Evita que a mesma mensagem vá pro mural duas vezes
            }
            self.save_config()
        return self.config[guild_id_str]

    # =====================================================================
    # MÉTODOS DE CONFIGURAÇÃO (Exclusivos para Admins via /server_config)
    # =====================================================================

    async def setup_mural(self, interaction: discord.Interaction, mural: discord.TextChannel, min_estrelas: int = 5):
        config = self.get_guild_config(interaction.guild_id)
        config["mural_channel"] = mural.id
        config["threshold"] = min_estrelas
        self.save_config()
        
        await interaction.response.send_message(
            f"✅ **Mural configurado com sucesso!**\n"
            f"📍 **Canal:** {mural.mention}\n"
            f"⭐ **Mínimo de reações:** {min_estrelas}",
            ephemeral=True
        )

    async def add_channel(self, interaction: discord.Interaction, canal: discord.TextChannel):
        config = self.get_guild_config(interaction.guild_id)
        if canal.id in config["monitored_channels"]:
            return await interaction.response.send_message(f"⚠️ O canal {canal.mention} já está sendo monitorado.", ephemeral=True)
        
        config["monitored_channels"].append(canal.id)
        self.save_config()
        await interaction.response.send_message(f"👁️‍🗨️ **Canal adicionado!** Agora estou monitorando {canal.mention} em busca de estrelas.", ephemeral=True)

    async def remove_channel(self, interaction: discord.Interaction, canal: discord.TextChannel):
        config = self.get_guild_config(interaction.guild_id)
        if canal.id in config["monitored_channels"]:
            config["monitored_channels"].remove(canal.id)
            self.save_config()
            await interaction.response.send_message(f"❌ **Canal removido!** Não vou mais monitorar {canal.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ O canal {canal.mention} não estava na lista de monitoramento.", ephemeral=True)

    async def starboard_status(self, interaction: discord.Interaction):
        config = self.get_guild_config(interaction.guild_id)
        mural = f"<#{config['mural_channel']}>" if config['mural_channel'] else "Não definido"
        canais = ", ".join([f"<#{c}>" for c in config['monitored_channels']]) if config['monitored_channels'] else "Nenhum canal monitorado"
        
        embed = discord.Embed(title="⚙️ Status do Starboard", color=discord.Color.gold())
        embed.add_field(name="Mural de Artes", value=mural, inline=False)
        embed.add_field(name="Canais Monitorados", value=canais, inline=False)
        embed.add_field(name="Mínimo de ⭐", value=f"{config['threshold']} estrelas", inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # =====================================================================
    # EVENTO PRINCIPAL: O MOTOR DO STARBOARD (Potência)
    # =====================================================================

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Ignora reações em mensagens diretas (DMs)
        if not payload.guild_id:
            return

        # Verifica se a reação é uma estrela
        if str(payload.emoji) != "⭐":
            return

        config = self.get_guild_config(payload.guild_id)

        # 1. Verifica se o sistema está configurado
        if not config["mural_channel"]:
            return

        # 2. Verifica se o canal está na lista de monitorados
        if payload.channel_id not in config["monitored_channels"]:
            return

        # 3. Verifica se a mensagem já foi pro mural
        if payload.message_id in config["starred_messages"]:
            return

        # Pega os objetos do servidor, canal e mensagem
        guild = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id)
        mural_channel = guild.get_channel(config["mural_channel"])

        if not channel or not mural_channel:
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return # Mensagem foi apagada antes de podermos checar

        # 4. Conta quantas estrelas reais a mensagem tem
        star_count = 0
        for reaction in message.reactions:
            if str(reaction.emoji) == "⭐":
                star_count = reaction.count
                break

        # 5. Se atingiu a meta, envia para o Mural!
        if star_count >= config["threshold"]:
            await self.send_to_mural(message, mural_channel, star_count)
            
            # Registra no JSON que essa mensagem já foi enviada
            config["starred_messages"].append(message.id)
            self.save_config()

    async def send_to_mural(self, message: discord.Message, mural_channel: discord.TextChannel, star_count: int):
        """Monta um Embed luxuoso e envia para o canal do mural."""
        
        embed = discord.Embed(
            description=message.content, 
            color=discord.Color.gold(),
            timestamp=message.created_at
        )
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.set_footer(text=f"ID: {message.id}")

        # Se houver imagens anexadas (crucial para um mural de artes), coloca no embed!
        if message.attachments:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith('image/'):
                    embed.set_image(url=att.url)
                    break # Coloca apenas a primeira imagem no corpo principal

        # Botão interativo nativo do Discord para ir direto para a mensagem original
        view = discord.ui.View()
        button = discord.ui.Button(label="Ir para a Arte Original", style=discord.ButtonStyle.link, url=message.jump_url)
        view.add_item(button)

        content = f"⭐ **{star_count}** | {message.channel.mention}"
        
        await mural_channel.send(content=content, embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(StarboardCog(bot))

