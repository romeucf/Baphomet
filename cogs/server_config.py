import discord
from discord import app_commands
from discord.ext import commands


ROLE_STAFF_ID = 1393546293418397866


class BotaoAbrirDenuncia(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir denúncia", emoji="🚨", custom_id="denuncia:abrir")
    async def abrir(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        membro = interaction.user
        role_staff = guild.get_role(ROLE_STAFF_ID)

        nome_canal = f"denuncia-{membro.name}".lower().replace(" ", "-")[:40]
        canal_existente = discord.utils.get(guild.text_channels, name=nome_canal)
        if canal_existente:
            await interaction.response.send_message(
                f"❌ Você já tem um ticket aberto em {canal_existente.mention}.",
                ephemeral=True,
            )
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            membro: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }
        if role_staff:
            overwrites[role_staff] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            )

        canal = await guild.create_text_channel(
            name=nome_canal,
            overwrites=overwrites,
            category=interaction.channel.category,
            reason=f"Ticket de denúncia aberto por {membro}",
        )
        await canal.send(content=f"Denúncia aberta <@&{ROLE_STAFF_ID}>", view=BotaoFecharDenuncia())
        await interaction.response.send_message(
            f"✅ Seu ticket foi aberto em {canal.mention}.",
            ephemeral=True,
        )


class BotaoFecharDenuncia(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fechar denúncia",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        custom_id="denuncia:fechar",
    )
    async def fechar(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_staff = interaction.guild.get_role(ROLE_STAFF_ID)
        eh_staff = role_staff in interaction.user.roles if role_staff else False
        eh_admin = interaction.user.guild_permissions.administrator

        if not (eh_staff or eh_admin):
            await interaction.response.send_message(
                "❌ Apenas a staff pode fechar denúncias.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("🔒 Fechando o ticket...", ephemeral=True)
        await interaction.channel.delete(reason=f"Ticket fechado por {interaction.user}")


@app_commands.default_permissions(administrator=True)
class ServerConfig(app_commands.Group):
    welcome_group = app_commands.Group(
        name="welcome",
        description="Gerencia as mensagens de boas-vindas do servidor.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    starboard_group = app_commands.Group(
        name="starboard",
        description="Customização do sistema de Mural de Artes.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    antispam_group = app_commands.Group(
        name="antispam",
        description="Controles centrais do firewall anti-spam.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    aotd_config_group = app_commands.Group(
        name="aotd_config",
        description="Configuração administrativa do Álbum do Dia.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    motd_config_group = app_commands.Group(
        name="motd_config",
        description="Configuração e operação do Filme do Dia.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    vinculo_config_group = app_commands.Group(
        name="vinculo_config",
        description="Configura os interesses e parâmetros do altar de vínculos.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    whitelist_inativos_group = app_commands.Group(
        name="whitelist_inativos",
        description="Gerencia a whitelist do prune de inativos.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    convites_group = app_commands.Group(
        name="convites",
        description="Centraliza a proteção e configuração de convites.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    canais_group = app_commands.Group(
        name="canais",
        description="Centraliza bloqueios e canais estruturais do servidor.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    bump_group = app_commands.Group(
        name="bump",
        description="Centraliza a configuração do sistema de bump.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    limpeza_group = app_commands.Group(
        name="limpeza",
        description="Centraliza as configurações de limpeza automática.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    inativos_group = app_commands.Group(
        name="inativos",
        description="Centraliza as ferramentas de prune de inatividade.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    logs_group = app_commands.Group(
        name="logs",
        description="Centraliza os rituais e status de logs.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    denuncias_group = app_commands.Group(
        name="denuncias",
        description="Centraliza o sistema de denúncias do servidor.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    def __init__(self):
        super().__init__(
            name="server_config",
            description="Administração obscura: molde as engrenagens do servidor aos seus desígnios.",
        )

    async def _send_module_unavailable(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)
        else:
            await interaction.followup.send("Módulo indisponível.", ephemeral=True)

    async def _call_cog(
        self,
        interaction: discord.Interaction,
        cog_name: str,
        method_name: str,
        *args,
        **kwargs,
    ) -> None:
        cog = interaction.client.get_cog(cog_name)
        if cog is None:
            await self._send_module_unavailable(interaction)
            return

        method = getattr(cog, method_name, None)
        if method is None:
            await self._send_module_unavailable(interaction)
            return

        try:
            if isinstance(method, app_commands.Command):
                callback_binding = method.binding or cog
                await method.callback(
                    callback_binding,
                    interaction,
                    *args,
                    **kwargs,
                )
            else:
                await method(interaction, *args, **kwargs)
        except Exception as exc:
            aotd_logger = getattr(cog, "log_aotd_exception", None)
            if callable(aotd_logger):
                aotd_logger(
                    exc,
                    interaction=interaction,
                    phase="server_config_proxy",
                    proxy_cog=cog_name,
                    proxy_method=method_name,
                    positional_arg_types=[type(arg).__name__ for arg in args],
                    keyword_arg_names=sorted(kwargs),
                )
            raise

    async def _call_whitelist_group(
        self,
        interaction: discord.Interaction,
        method_name: str,
        *args,
        **kwargs,
    ) -> None:
        from cogs.inactivity_prune import WhitelistGroup

        cog = interaction.client.get_cog("InactivityPruneCog")
        if cog is None:
            await self._send_module_unavailable(interaction)
            return

        group = WhitelistGroup(cog)
        method = getattr(group, method_name, None)
        if method is None:
            await self._send_module_unavailable(interaction)
            return

        if isinstance(method, app_commands.Command):
            await method.callback(group, interaction, *args, **kwargs)
        else:
            await method(interaction, *args, **kwargs)

    @welcome_group.command(name="setup", description="[Admin] Configura o canal e o embed de boas-vindas.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_welcome_setup(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "Welcome", "cmd_setup")

    @welcome_group.command(name="preview", description="[Admin] Mostra um preview do embed de boas-vindas.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_welcome_preview(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "Welcome", "cmd_preview")

    @starboard_group.command(name="setup", description="Define o canal do Mural de Artes e o mínimo de reações.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_starboard_setup(
        self,
        interaction: discord.Interaction,
        mural: discord.TextChannel,
        min_estrelas: int = 5,
    ) -> None:
        await self._call_cog(interaction, "StarboardCog", "setup_mural", mural, min_estrelas)

    @starboard_group.command(name="add_canal", description="Adiciona um canal para ser monitorado pelo bot.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_starboard_add_channel(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ) -> None:
        await self._call_cog(interaction, "StarboardCog", "add_channel", canal)

    @starboard_group.command(name="remover_canal", description="Para de monitorar um canal específico.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_starboard_remove_channel(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ) -> None:
        await self._call_cog(interaction, "StarboardCog", "remove_channel", canal)

    @starboard_group.command(name="status", description="Mostra as configurações atuais do Mural.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_starboard_status(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "StarboardCog", "starboard_status")

    @antispam_group.command(name="toggle", description="Ativa ou desativa a vigilância do anti-spam.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_antispam_toggle(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "ModerationCog", "antispam_toggle")

    @antispam_group.command(name="status", description="Mostra o estado atual do anti-spam.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_antispam_status(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "ModerationCog", "antispam_status")

    @logs_group.command(name="setup", description="define o canal e ativa/desativa os logs de punições")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_setup_logs(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        status: bool,
    ) -> None:
        await self._call_cog(interaction, "PunishmentLogs", "setup_logs", channel, status)

    @bump_group.command(
        name="configurar_bump",
        description="Define o canal e cargo para o lembrete do /bump 🔔",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_configurar_bump(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        await self._call_cog(interaction, "BumpReminderCog", "configurar_bump", canal, cargo)

    @inativos_group.command(
        name="expulsar_inativos",
        description="Expulsa membros que não enviam mensagens há X dias ⚠️",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_expulsar_inativos(
        self,
        interaction: discord.Interaction,
        dias: app_commands.Range[int, 1, 365],
    ) -> None:
        await self._call_cog(interaction, "InactivityPruneCog", "expulsar_inativos", dias)

    @whitelist_inativos_group.command(name="adicionar", description="Adiciona um membro ou cargo à whitelist 🛡️")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_whitelist_adicionar(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        await self._call_whitelist_group(interaction, "adicionar", usuario, cargo)

    @whitelist_inativos_group.command(name="remover", description="Remove um membro ou cargo da whitelist 🗑️")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_whitelist_remover(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member | None = None,
        cargo: discord.Role | None = None,
    ) -> None:
        await self._call_whitelist_group(interaction, "remover", usuario, cargo)

    @whitelist_inativos_group.command(name="listar", description="Mostra quem está imune ao prune 📋")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_whitelist_listar(self, interaction: discord.Interaction) -> None:
        await self._call_whitelist_group(interaction, "listar")

    @aotd_config_group.command(name="canal", description="Define o canal onde o Álbum do Dia será enviado automaticamente.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel | None = None,
    ) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "configurar_canal", canal)

    @aotd_config_group.command(name="cargo", description="Define o cargo marcado no post do Álbum do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_cargo(
        self,
        interaction: discord.Interaction,
        cargo: discord.Role | None = None,
    ) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "configurar_cargo", cargo)

    @aotd_config_group.command(name="horario", description="Define o horário diário do Álbum do Dia no tempo de Brasília.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_horario(self, interaction: discord.Interaction, horario: str) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "configurar_horario", horario)

    @aotd_config_group.command(name="staff-canal", description="Define o canal interno onde a staff será avisada sobre novas sugestões.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_staff_canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel | None = None,
    ) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "configurar_staff_canal", canal)

    @aotd_config_group.command(name="staff-cargo", description="Define o cargo de staff marcado quando alguém sugere um álbum.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_staff_cargo(
        self,
        interaction: discord.Interaction,
        cargo: discord.Role | None = None,
    ) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "configurar_staff_cargo", cargo)

    @aotd_config_group.command(name="ativar", description="Ativa o envio automático diário do Álbum do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_ativar(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "ativar_auto")

    @aotd_config_group.command(name="desativar", description="Desativa o envio automático diário do Álbum do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_desativar(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "desativar_auto")

    @aotd_config_group.command(name="shuffle", description="Embaralha aleatoriamente a ordem dos álbuns/EPs na fila.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_shuffle(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "shuffle_fila")

    @aotd_config_group.command(name="status", description="Mostra a configuração atual do Álbum do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_status(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "status")

    @aotd_config_group.command(name="lista", description="Lista os álbuns/EPs agendados na fila.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_lista(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "listar_fila")

    @aotd_config_group.command(name="remover", description="Remove um álbum/EP da fila pelo índice.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_remover(
        self,
        interaction: discord.Interaction,
        indice: int,
    ) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "remover", indice)

    @aotd_config_group.command(name="testar-album", description="Força o envio do próximo álbum/EP agora.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_testar_album(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "testar_album")

    @aotd_config_group.command(name="test_recap", description="Força o envio do Recap Semanal do Álbum do Dia agora.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_test_recap(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "test_recap")

    @aotd_config_group.command(name="toggle_recap", description="Ativa ou desativa o envio automático do Recap Semanal de álbuns.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_toggle_recap(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "toggle_recap")

    @aotd_config_group.command(name="recap_time", description="Define o horário do Recap Semanal do Álbum do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_aotd_recap_time(self, interaction: discord.Interaction, horario: str) -> None:
        await self._call_cog(interaction, "AlbumDoDia", "recap_time", horario)

    @motd_config_group.command(name="toggle", description="Ativa ou desativa o envio automático diário do Filme do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_toggle(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "toggle")

    @motd_config_group.command(name="horario", description="Define o horário diário do Filme do Dia no formato HH:MM.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_horario(self, interaction: discord.Interaction, horario: str) -> None:
        await self._call_cog(interaction, "MovieCog", "horario", horario)

    @motd_config_group.command(name="canal", description="Define o canal onde o Filme do Dia será publicado.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel,
    ) -> None:
        await self._call_cog(interaction, "MovieCog", "canal", canal)

    @motd_config_group.command(name="cargo", description="Define o cargo mencionado nas publicações do Filme do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_cargo(
        self,
        interaction: discord.Interaction,
        cargo: discord.Role,
    ) -> None:
        await self._call_cog(interaction, "MovieCog", "cargo", cargo)

    @motd_config_group.command(name="emojis", description="Define os emojis das reações do Filme do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_emojis(
        self,
        interaction: discord.Interaction,
        like: str,
        dislike: str,
        never_watched: str,
    ) -> None:
        await self._call_cog(interaction, "MovieCog", "emojis", like, dislike, never_watched)

    @motd_config_group.command(name="resetar_emojis", description="Restaura os emojis padrão das reações do Filme do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_resetar_emojis(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "resetar_emojis")

    @motd_config_group.command(name="status", description="Mostra todas as configurações atuais do Filme do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_status(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "status")

    @motd_config_group.command(name="remover_blacklist", description="Remove um filme da blacklist pelo ID do TMDB.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_remover_blacklist(
        self,
        interaction: discord.Interaction,
        id: int,
    ) -> None:
        await self._call_cog(interaction, "MovieCog", "remover_blacklist", id)

    @motd_config_group.command(name="blacklist", description="Lista os filmes registrados na blacklist deste servidor.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_blacklist(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "blacklist")

    @motd_config_group.command(name="testar", description="Publica um Filme do Dia de teste sem gravar na blacklist.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_testar(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "testar")

    @motd_config_group.command(name="fila", description="Lista cronologicamente a fila de sugestões do Filme do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_fila(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "fila")

    @motd_config_group.command(name="toggle_recap", description="Ativa ou desativa o envio automático do Recap Semanal de filmes.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_toggle_recap(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "toggle_recap")

    @motd_config_group.command(name="recap_time", description="Define o horário do Recap Semanal do Filme do Dia.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_recap_time(self, interaction: discord.Interaction, horario: str) -> None:
        await self._call_cog(interaction, "MovieCog", "recap_time", horario)

    @motd_config_group.command(name="test_recap", description="Testa o envio do Recap Semanal do Filme do Dia agora.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_motd_test_recap(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "MovieCog", "test_recap")

    @vinculo_config_group.command(name="adicionar", description="Registra um cargo como interesse de vínculo.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_adicionar(
        self,
        interaction: discord.Interaction,
        cargo: discord.Role,
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_adicionar", cargo)

    @vinculo_config_group.command(name="remover", description="Remove um cargo da lista de interesses.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_remover(
        self,
        interaction: discord.Interaction,
        cargo: discord.Role,
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_remover", cargo)

    @vinculo_config_group.command(name="listar", description="Lista os cargos configurados como interesses.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_listar(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_listar")

    @vinculo_config_group.command(name="limpar", description="Remove todos os cargos configurados como interesses.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_limpar(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_limpar")

    @vinculo_config_group.command(name="canal-fofoca", description="Define o canal público dos anúncios de vínculos.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_canal_fofoca(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel | None = None,
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_canal_fofoca", canal)

    @vinculo_config_group.command(name="testar-card", description="Gera uma prévia do card visual de vínculo.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_testar_card(
        self,
        interaction: discord.Interaction,
        solicitante: discord.Member,
        aceitante: discord.Member,
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_testar_card", solicitante, aceitante)

    @vinculo_config_group.command(name="afinidade", description="Configura os dias necessários para afinidade 2 e 3.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_afinidade(
        self,
        interaction: discord.Interaction,
        nivel_2_dias: app_commands.Range[int, 1, 3650],
        nivel_3_dias: app_commands.Range[int, 2, 3650],
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_afinidade", nivel_2_dias, nivel_3_dias)

    @vinculo_config_group.command(name="maldicao", description="Configura a maldição de ruptura.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_maldicao(
        self,
        interaction: discord.Interaction,
        penalidade_percent: app_commands.Range[int, 0, 95],
        horas: app_commands.Range[int, 1, 720],
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_maldicao", penalidade_percent, horas)

    @vinculo_config_group.command(name="doacao", description="Configura a taxa das doações de XP entre parceiros.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_doacao(
        self,
        interaction: discord.Interaction,
        taxa_percent: app_commands.Range[int, 0, 95],
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_doacao", taxa_percent)

    @vinculo_config_group.command(name="ressonancia", description="Configura presença recente e bônus de ressonância.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_ressonancia(
        self,
        interaction: discord.Interaction,
        janela_minutos: app_commands.Range[int, 1, 1440],
        bonus_percent: app_commands.Range[int, 0, 95],
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_ressonancia", janela_minutos, bonus_percent)

    @vinculo_config_group.command(name="limite", description="Define o limite diário de solicitações de vínculo por usuário.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_limite(
        self,
        interaction: discord.Interaction,
        quantidade: app_commands.Range[int, 0, 100],
    ) -> None:
        await self._call_cog(interaction, "VinculosCog", "config_limite", quantidade)

    @vinculo_config_group.command(name="relatorio", description="Mostra o relatório administrativo do altar de vínculos.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_config_vinculo_relatorio(self, interaction: discord.Interaction) -> None:
        await self._call_cog(interaction, "VinculosCog", "vinculo_relatorio")

    @convites_group.command(
        name="anti_convites",
        description="Sela o servidor contra convites hereges. Bloqueia links de outros reinos.",
    )
    async def anti_convites(self, interaction: discord.Interaction, estado: bool) -> None:
        cog = interaction.client.get_cog("AntiInviteCog")
        if cog:
            await cog.anti_invite_toggle(interaction, estado)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @convites_group.command(
        name="status_anticonvites",
        description="Revela se o selo contra convites profanos está ativo ou adormecido.",
    )
    async def status_anticonvites(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("AntiInviteCog")
        if cog:
            await cog.anti_invite_status(interaction)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @canais_group.command(
        name="trancar_canal",
        description="Tranca os portões deste canal. Apenas os escolhidos poderão ecoar suas vozes.",
    )
    async def trancar_canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel = None,
    ) -> None:
        cog = interaction.client.get_cog("BlacklistCog")
        if cog:
            ctx = await interaction.client.get_context(interaction)
            await cog.bloquear_canal(ctx, canal)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @canais_group.command(
        name="destrancar_canal",
        description="Quebra os selos deste canal. Permite que o caos das vozes mortais retorne.",
    )
    async def destrancar_canal(
        self,
        interaction: discord.Interaction,
        canal: discord.TextChannel = None,
    ) -> None:
        cog = interaction.client.get_cog("BlacklistCog")
        if cog:
            ctx = await interaction.client.get_context(interaction)
            await cog.desbloquear_canal(ctx, canal)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @bump_group.command(
        name="canal_bump",
        description="Elege o altar onde os ritos de ascensão (bump) serão celebrados.",
    )
    async def canal_bump(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        cog = interaction.client.get_cog("BumpReminderCog")
        if cog:
            await cog.configurar_bump(interaction, canal)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @limpeza_group.command(
        name="configurar_limpeza_saida",
        description="Configura a foice: expurga os rastros deixados por almas que abandonaram o servidor.",
    )
    async def limpeza_saida(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        cog = interaction.client.get_cog("GhostCleanupCog")
        if cog:
            await cog.configurar_limpeza_saida(interaction, canal)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @logs_group.command(
        name="canal_logs",
        description="Define o pergaminho sombrio onde os segredos e ações do servidor serão eternizados.",
    )
    async def canal_logs(
        self,
        interaction: discord.Interaction,
        canal_destino_id: str,
        servidor_origem_id: str = None,
    ) -> None:
        cog = interaction.client.get_cog("DailyLoggerCog")
        if cog:
            await cog.configurar_logs(interaction, canal_destino_id, servidor_origem_id)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @denuncias_group.command(
        name="canal_denuncias",
        description="Cria um confessionário oculto para que os mortais relatem as heresias ao conselho.",
    )
    async def canal_denuncias(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Denuncia")
        if cog:
            await cog.denuncia_chat(interaction)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @logs_group.command(
        name="painel_logs",
        description="Abre o grimório de registros. Um painel para observar tudo o que rasteja nas sombras.",
    )
    async def painel_logs(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("PunishmentLogs")
        if cog:
            await cog.setup_logs(interaction)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @convites_group.command(
        name="canal_convites",
        description="Demarca o território onde novos adeptos podem ser convocados livremente.",
    )
    async def canal_convites(
        self,
        interaction: discord.Interaction,
        max_age: app_commands.Range[int, 0, 720],
        max_uses: app_commands.Range[int, 0, 1000],
    ) -> None:
        cog = interaction.client.get_cog("ModerationCog")
        if cog:
            await cog.set_convite(interaction, max_age, max_uses)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @canais_group.command(
        name="canal_geral",
        description="Sagra o salão principal onde a cacofonia das almas recém-chegadas irá ecoar.",
    )
    async def canal_geral(self, interaction: discord.Interaction, canal: discord.TextChannel) -> None:
        cog = interaction.client.get_cog("ModerationCog")
        if cog:
            await cog.set_geral(interaction, canal)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @canais_group.command(
        name="canal_permanencia",
        description="Define o abismo onde a presença contínua dos membros será monitorada e julgada.",
    )
    async def canal_permanencia(
        self,
        interaction: discord.Interaction,
        minutos: app_commands.Range[int, 1, 10080],
    ) -> None:
        cog = interaction.client.get_cog("ModerationCog")
        if cog:
            await cog.set_permanencia(interaction, minutos)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @limpeza_group.command(
        name="status_limpeza_saida",
        description="Sussurra o estado atual da foice que limpa as cinzas dos que partiram.",
    )
    async def status_limpeza(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("GhostCleanupCog")
        if cog:
            await cog.status_limpeza_saida(interaction)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @logs_group.command(
        name="status_logs",
        description="Expõe se o olho que tudo vê (logs) está observando ou vendado no momento.",
    )
    async def status_logs(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("DailyLoggerCog")
        if cog:
            await cog.status_logs(interaction)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @canais_group.command(
        name="status-canais",
        description="Mapeia as artérias do servidor, revelando a função profana de cada canal consagrado.",
    )
    async def status_canais(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("BlacklistCog")
        if cog:
            ctx = await interaction.client.get_context(interaction)
            await cog.status_canais(ctx)
        else:
            await interaction.response.send_message("Módulo indisponível.", ephemeral=True)

    @denuncias_group.command(name="denuncia_chat", description="Posta a mensagem de abertura de denúncias no canal.")
    async def denuncia_chat(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📋 Sistema de Denúncias",
            description=(
                "Presenciou algo que viola as regras do servidor?\n\n"
                "Clique no botão de abrir denúncia para abrir um ticket de denúncia **privado** "
                "com a staff."
            ),
            color=discord.Color.from_rgb(220, 50, 50),
        )
        await interaction.channel.send(embed=embed, view=BotaoAbrirDenuncia())
        await interaction.response.send_message("✅ Mensagem postada.", ephemeral=True)

    @denuncias_group.command(name="fechar_denuncia", description="Fecha (deleta) o canal de ticket atual.")
    async def fechar_denuncia(self, interaction: discord.Interaction) -> None:
        if not interaction.channel.name.startswith("denuncia-"):
            await interaction.response.send_message(
                "❌ Este comando só pode ser usado dentro de um canal de ticket.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("🔒 Fechando o ticket...", ephemeral=True)
        await interaction.channel.delete(reason=f"Ticket fechado por {interaction.user} via comando")

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        msg = "Ocorreu uma falha no sistema interno."
        if isinstance(error, app_commands.MissingPermissions):
            msg = "⛔ Acesso negado: Credenciais insuficientes."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = (
                "🔧 Erro de Configuração: O Bot precisa da permissão "
                f"{', '.join(error.missing_permissions)} para fazer isso."
            )
        else:
            import logging

            logging.getLogger(__name__).exception(
                "Crash não tratado no grupo de configuração",
                exc_info=error,
            )

        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(ServerConfig())
    bot.add_view(BotaoAbrirDenuncia())
    bot.add_view(BotaoFecharDenuncia())
