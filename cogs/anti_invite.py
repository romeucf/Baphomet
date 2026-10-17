import discord
from discord.ext import commands
import json
import os
import re
from core.logger import log_exception
from core.paths import DATA_DIR


class AntiInviteCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = DATA_DIR / "anti_invite_config.json"
        self.whitelisted_guild_id = 1335048033200635926
        # Regex potente para capturar discord.gg, discord.com/invite, etc.
        self.invite_regex = re.compile(
            r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discordapp\.com/invite)/([\w\d-]+)", 
            re.IGNORECASE
        )
        self.config = self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_config(self):
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4)

    # --- COMANDO DE TOGGLE (ADMIN ONLY) ---
    async def anti_invite_toggle(self, it: discord.Interaction, estado: bool):
        guild_id = str(it.guild.id)
        self.config[guild_id] = estado
        self.save_config()

        status_texto = "🔴 **ATIVADO**" if estado else "⚪ **DESATIVADO**"
        await it.response.send_message(f"O sistema de Anti-Invite agora está: {status_texto}", ephemeral=True)

    async def anti_invite_status(self, it: discord.Interaction):
        guild_id = str(it.guild.id)
        estado = self.config.get(guild_id, False)
        
        embed = discord.Embed(title="🛡️ Status — Anti-Invite", color=discord.Color.green() if estado else discord.Color.red())
        
        if estado:
            embed.description = "🟢 **Ativo**\nConvites de outros servidores serão apagados automaticamente (exceto de administradores)."
        else:
            embed.description = "⚠️ **Nenhuma configuração definida para este módulo** (ou está desativado).\nConvites estão liberados."
            
        await it.response.send_message(embed=embed, ephemeral=True)

    # --- LISTENER DE MENSAGENS ---
    @commands.Cog.listener()
    async def on_message(self, message):
        # Ignora bots e mensagens fora de servidores
        if message.author.bot or not message.guild:
            return

        # Verifica se o sistema está ligado para este servidor
        guild_id = str(message.guild.id)
        if not self.config.get(guild_id, False):
            return

        # Ignora administradores para eles poderem postar convites se necessário
        if message.author.guild_permissions.administrator:
            return

        # Procura por convites na mensagem
        invites = self.invite_regex.findall(message.content)
        
        for code in invites:
            try:
                # O bot tenta "olhar" o convite para saber de qual servidor ele é
                invite = await self.bot.fetch_invite(code)
                
                # Se o ID do servidor do convite for o whitelisted, ignoramos
                if invite.guild and invite.guild.id == self.whitelisted_guild_id:
                    continue
                
                # Se chegou aqui, é um convite de outro servidor. Apaga!
                await message.delete()
                await message.channel.send(
                    f"🚫 {message.author.mention}, não é permitido enviar convites de outros servidores aqui!",
                    delete_after=5
                )
                break # Já apagou a mensagem, não precisa checar outros códigos na mesma mensagem

            except discord.NotFound:
                # O convite é inválido ou expirou, mas por segurança, muitos preferem apagar mesmo assim
                await message.delete()
            except Exception as e:
                log_exception(e)
                print(f"Erro ao processar convite: {e}")

async def setup(bot):
    await bot.add_cog(AntiInviteCog(bot))