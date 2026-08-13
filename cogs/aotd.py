from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import secrets
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from core.logger import log_exception


LOGGER = logging.getLogger("Baphomet.AOTD")
AOTD_GENERIC_ERROR = "❌ Ocorreu um erro ao executar esse comando."


# ============================================================
#  ÁLBUM DO DIA — COG ÚNICO
#  Armazenamento em SQLite.
#  Sem Pillow. Sem renderer. Apenas embed.
#
#  Requisitos:
#  - discord.py 2.x
#  - spotipy
#
#  Variáveis de ambiente esperadas:
#  - SPOTIFY_ID
#  - SPOTIFY_SECRET
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_FILE = DATA_DIR / "aotd.sqlite3"

OLD_QUEUE_FILES = [
    DATA_DIR / "data/fila_albuns.json",
    BASE_DIR / "data/fila_albuns.json",
    Path.cwd() / "data/fila_albuns.json",
]

OLD_CONFIG_FILES = [
    DATA_DIR / "aotd_config.json",
    BASE_DIR / "aotd_config.json",
    Path.cwd() / "aotd_config.json",
]

SPOTIPY_CLIENT_ID = os.getenv("SPOTIFY_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIFY_SECRET")

BR_TZ = dt.timezone(dt.timedelta(hours=-3), name="BRT")

EMOJI_GOSTO = "<:aotd_gosto:1495227456851017758>"
EMOJI_NAO_GOSTO = "<:aotd_naogosto:1495227731791708250>"
EMOJI_NUNCA_OUVI = "<:aotd_nuncaouvi:1495227758224085043>"

DEFAULT_ROLE_ID = 1495234087772754011

DEFAULT_CONFIG: dict[str, Any] = {
    "auto_enabled": True,
    "album_channel_id": None,
    "album_role_id": DEFAULT_ROLE_ID,

    # Horário diário em Brasília
    "daily_hour": 12,
    "daily_minute": 0,
    "last_auto_sent_date": None,

    # Notificações internas para staff/admins quando alguém sugere álbum
    "staff_notify_channel_id": None,
    "staff_notify_role_id": None,
    
    # Recap Semanal
    "recap_enabled": False,
    "recap_hour": 18,
    "recap_minute": 0,
}


class AOTDQueueListView(discord.ui.View):
    """
    View paginada do /aotd_config lista.

    Segurança:
    - A mensagem é ephemeral.
    - Apenas o admin que executou o comando consegue navegar.
    - Mesmo assim, a View checa novamente se o usuário ainda tem permissão de admin.
    """

    def __init__(
        self,
        *,
        cog: "AlbumDoDia",
        interaction: discord.Interaction,
        fila: list[dict[str, Any]],
        config: dict[str, Any],
        per_page: int = 8,
    ) -> None:
        super().__init__(timeout=180)

        self.cog = cog
        self.owner_id = interaction.user.id
        self.fila = fila
        self.config = config
        self.per_page = max(1, per_page)
        self.current_page = 0
        self.message: Optional[discord.Message] = None

        self._update_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.fila) + self.per_page - 1) // self.per_page)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🚫 Essa lista não é sua, mortal. Use `/aotd_config lista` pra abrir a sua própria.",
                ephemeral=True,
            )
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "🚫 Esse comando só pode ser usado dentro de um servidor.",
                ephemeral=True,
            )
            return False

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "🚫 Você precisa ser administrador para navegar nessa lista.",
                ephemeral=True,
            )
            return False

        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound) as exc:
                self.cog.log_aotd_exception(
                    exc,
                    phase="queue_pagination_timeout_disable",
                    message_id=getattr(self.message, "id", None),
                    page=self.current_page + 1,
                )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        self.cog.log_aotd_exception(
            error,
            interaction=interaction,
            phase="queue_pagination",
            item_custom_id=getattr(item, "custom_id", None),
            page=self.current_page + 1,
            total_pages=self.total_pages,
            queue_size=len(self.fila),
        )

        try:
            await self.cog.send_error_response(
                interaction,
                "❌ Não consegui atualizar a página da fila. O erro foi registrado no console.",
            )
        except discord.HTTPException as response_error:
            self.cog.log_aotd_exception(
                response_error,
                interaction=interaction,
                phase="queue_pagination_error_response",
            )

    def _update_buttons(self) -> None:
        total_pages = self.total_pages

        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue

            if item.custom_id == "aotd_lista_primeira":
                item.disabled = self.current_page <= 0

            elif item.custom_id == "aotd_lista_anterior":
                item.disabled = self.current_page <= 0

            elif item.custom_id == "aotd_lista_proxima":
                item.disabled = self.current_page >= total_pages - 1

            elif item.custom_id == "aotd_lista_ultima":
                item.disabled = self.current_page >= total_pages - 1

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self.current_page = max(0, min(self.current_page, self.total_pages - 1))
        self._update_buttons()

        embed = self.cog.criar_embed_lista_paginada(
            fila=self.fila,
            config=self.config,
            page=self.current_page,
            per_page=self.per_page,
            guild=interaction.guild,
        )

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Primeira",
        emoji="⏮️",
        style=discord.ButtonStyle.secondary,
        custom_id="aotd_lista_primeira",
    )
    async def primeira_pagina(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.current_page = 0
        await self._refresh(interaction)

    @discord.ui.button(
        label="Anterior",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        custom_id="aotd_lista_anterior",
    )
    async def pagina_anterior(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.current_page -= 1
        await self._refresh(interaction)

    @discord.ui.button(
        label="Próxima",
        emoji="▶️",
        style=discord.ButtonStyle.primary,
        custom_id="aotd_lista_proxima",
    )
    async def proxima_pagina(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.current_page += 1
        await self._refresh(interaction)

    @discord.ui.button(
        label="Última",
        emoji="⏭️",
        style=discord.ButtonStyle.secondary,
        custom_id="aotd_lista_ultima",
    )
    async def ultima_pagina(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.current_page = self.total_pages - 1
        await self._refresh(interaction)


class AlbumDoDia(commands.Cog):
    aotd = app_commands.Group(
        name="aotd",
        description="Comandos públicos do Álbum do Dia.",
        guild_only=True,
    )
    aotd_config = app_commands.Group(
        name="aotd_config",
        description="Configuração administrativa do Álbum do Dia.",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._spotify: Optional[spotipy.Spotify] = None

        self.inicializar_banco()
        self.migrar_json_antigo_para_sqlite()

        print(f"[AOTD] SQLite ativo em: {DB_FILE.resolve()}")

        self.enviar_album_automatico.start()
        self.enviar_recap_automatico.start()

    def cog_unload(self) -> None:
        self.enviar_album_automatico.cancel()
        self.enviar_recap_automatico.cancel()

    @staticmethod
    def _safe_interaction_value(interaction: discord.Interaction, attribute: str) -> Any:
        try:
            return getattr(interaction, attribute, None)
        except Exception:
            return None

    @classmethod
    def log_aotd_exception(
        cls,
        error: BaseException,
        *,
        interaction: Optional[discord.Interaction] = None,
        phase: str,
        **details: Any,
    ) -> str:
        """Registra uma falha do AOTD com dados úteis e um ID correlacionável."""
        error_id = uuid.uuid4().hex[:12]
        original = getattr(error, "original", error)
        command = cls._safe_interaction_value(interaction, "command") if interaction else None
        user = cls._safe_interaction_value(interaction, "user") if interaction else None
        guild = cls._safe_interaction_value(interaction, "guild") if interaction else None
        channel = cls._safe_interaction_value(interaction, "channel") if interaction else None
        response = cls._safe_interaction_value(interaction, "response") if interaction else None

        try:
            response_done = response.is_done() if response is not None else None
        except Exception:
            response_done = "indisponível"

        diagnostics = {
            "error_id": error_id,
            "phase": phase,
            "exception_type": f"{type(original).__module__}.{type(original).__name__}",
            "interaction_id": cls._safe_interaction_value(interaction, "id") if interaction else None,
            "command": getattr(command, "qualified_name", None) or getattr(command, "name", None),
            "user_id": getattr(user, "id", None),
            "guild_id": getattr(guild, "id", None),
            "channel_id": getattr(channel, "id", None),
            "response_done": response_done,
            "command_failed": cls._safe_interaction_value(interaction, "command_failed") if interaction else None,
            "database": str(DB_FILE.resolve()),
            **details,
        }
        context = "AOTD failure | " + " | ".join(
            f"{key}={value!r}" for key, value in diagnostics.items()
        )

        if isinstance(original, Exception):
            log_exception(original, context=context, interaction=interaction)
            LOGGER.error(
                context,
                exc_info=(type(original), original, original.__traceback__),
            )
        else:
            LOGGER.error("%s | exception=%r", context, original)

        return error_id

    @staticmethod
    async def send_error_response(
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # ========================================================
    #  Banco de Dados
    # ========================================================

    def conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def inicializar_banco(self) -> None:
        with self.conectar() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS album_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_position INTEGER,
                    nome TEXT NOT NULL,
                    artista TEXT NOT NULL,
                    url TEXT,
                    imagem TEXT,
                    sugerido_por TEXT,
                    sugerido_por_id INTEGER,
                    generos_json TEXT NOT NULL DEFAULT '[]',
                    total_faixas INTEGER,
                    duracao_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aotd_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL,
                    artista TEXT NOT NULL,
                    imagem TEXT,
                    date_added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aotd_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            self.garantir_coluna_queue_position(conn)

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_album_queue_position
                ON album_queue (queue_position ASC, id ASC)
                """
            )

            for key, value in DEFAULT_CONFIG.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO aotd_config (key, value)
                    VALUES (?, ?)
                    """,
                    (key, json.dumps(value, ensure_ascii=False)),
                )

            conn.commit()

    @staticmethod
    def garantir_coluna_queue_position(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(album_queue)").fetchall()
        column_names = {column["name"] for column in columns}

        if "queue_position" not in column_names:
            conn.execute(
                """
                ALTER TABLE album_queue
                ADD COLUMN queue_position INTEGER
                """
            )

        conn.execute(
            """
            UPDATE album_queue
            SET queue_position = id
            WHERE queue_position IS NULL
            """
        )

    def migrar_json_antigo_para_sqlite(self) -> None:
        with self.conectar() as conn:
            queue_count = conn.execute(
                "SELECT COUNT(*) FROM album_queue"
            ).fetchone()[0]

            if queue_count == 0:
                queue_file = self.encontrar_primeiro_arquivo_existente(OLD_QUEUE_FILES)

                if queue_file:
                    try:
                        with queue_file.open("r", encoding="utf-8") as f:
                            fila_antiga = json.load(f)

                        if isinstance(fila_antiga, list):
                            for item in fila_antiga:
                                if isinstance(item, dict):
                                    self.inserir_album_sync(conn, item)

                            print(f"[AOTD] Fila migrada do JSON: {queue_file.resolve()}")

                    except Exception as exc:
                        self.log_aotd_exception(
                            exc,
                            phase="legacy_queue_migration",
                            source_file=str(queue_file.resolve()),
                        )
                        print(f"[AOTD] Não consegui migrar a fila antiga: {exc}")

            config_file = self.encontrar_primeiro_arquivo_existente(OLD_CONFIG_FILES)

            if config_file:
                try:
                    with config_file.open("r", encoding="utf-8") as f:
                        config_antiga = json.load(f)

                    if isinstance(config_antiga, dict):
                        for key in DEFAULT_CONFIG:
                            if key in config_antiga:
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO aotd_config (key, value)
                                    VALUES (?, ?)
                                    """,
                                    (
                                        key,
                                        json.dumps(
                                            config_antiga[key],
                                            ensure_ascii=False,
                                        ),
                                    ),
                                )

                        print(f"[AOTD] Config migrada do JSON: {config_file.resolve()}")

                except Exception as exc:
                    self.log_aotd_exception(
                        exc,
                        phase="legacy_config_migration",
                        source_file=str(config_file.resolve()),
                    )
                    print(f"[AOTD] Não consegui migrar a config antiga: {exc}")

            conn.commit()

    @staticmethod
    def encontrar_primeiro_arquivo_existente(paths: list[Path]) -> Optional[Path]:
        vistos: set[Path] = set()

        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path

            if resolved in vistos:
                continue

            vistos.add(resolved)

            if path.exists() and path.is_file():
                return path

        return None

    # ========================================================
    #  Config em SQLite
    # ========================================================

    def carregar_config_sync(self) -> dict[str, Any]:
        with self.conectar() as conn:
            rows = conn.execute("SELECT key, value FROM aotd_config").fetchall()

        config = DEFAULT_CONFIG.copy()

        for row in rows:
            key = row["key"]

            try:
                value = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                value = row["value"]

            config[key] = value

        hour, minute = self.get_daily_time(config)
        config["daily_hour"] = hour
        config["daily_minute"] = minute

        return config

    def salvar_config_sync(self, config: dict[str, Any]) -> None:
        clean = DEFAULT_CONFIG.copy()
        clean.update(config)

        hour, minute = self.get_daily_time(clean)
        clean["daily_hour"] = hour
        clean["daily_minute"] = minute

        with self.conectar() as conn:
            for key, value in clean.items():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO aotd_config (key, value)
                    VALUES (?, ?)
                    """,
                    (key, json.dumps(value, ensure_ascii=False)),
                )

            conn.commit()

    def carregar_config(self) -> dict[str, Any]:
        return self.carregar_config_sync()

    def salvar_config(self, config: dict[str, Any]) -> None:
        self.salvar_config_sync(config)

    # ========================================================
    #  Fila em SQLite
    # ========================================================

    @staticmethod
    def row_para_album(row: sqlite3.Row) -> dict[str, Any]:
        try:
            generos = json.loads(row["generos_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            generos = []

        if not isinstance(generos, list):
            generos = []

        return {
            "_queue_id": row["id"],
            "_queue_position": row["queue_position"],
            "nome": row["nome"],
            "artista": row["artista"],
            "url": row["url"],
            "imagem": row["imagem"],
            "sugerido_por": row["sugerido_por"],
            "sugerido_por_id": row["sugerido_por_id"],
            "generos": generos,
            "total_faixas": row["total_faixas"],
            "duracao_ms": row["duracao_ms"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def proxima_posicao_sync(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(queue_position), 0) + 1
            FROM album_queue
            """
        ).fetchone()

        return int(row[0] or 1)

    @staticmethod
    def inserir_album_sync(conn: sqlite3.Connection, album: dict[str, Any]) -> int:
        generos = album.get("generos") or []

        if not isinstance(generos, list):
            generos = []

        queue_position = AlbumDoDia.proxima_posicao_sync(conn)

        try:
            duracao_ms = max(0, int(album.get("duracao_ms") or 0))
        except (TypeError, ValueError, OverflowError):
            duracao_ms = 0

        cursor = conn.execute(
            """
            INSERT INTO album_queue (
                queue_position,
                nome,
                artista,
                url,
                imagem,
                sugerido_por,
                sugerido_por_id,
                generos_json,
                total_faixas,
                duracao_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                queue_position,
                str(album.get("nome") or "Álbum desconhecido"),
                str(album.get("artista") or "Artista desconhecido"),
                album.get("url"),
                album.get("imagem"),
                album.get("sugerido_por"),
                album.get("sugerido_por_id"),
                json.dumps(generos, ensure_ascii=False),
                album.get("total_faixas"),
                duracao_ms,
            ),
        )

        return int(cursor.lastrowid)

    def carregar_fila(self) -> list[dict[str, Any]]:
        with self.conectar() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM album_queue
                ORDER BY queue_position ASC, id ASC
                """
            ).fetchall()

        return [self.row_para_album(row) for row in rows]

    def contar_fila(self) -> int:
        with self.conectar() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM album_queue").fetchone()[0]
            )

    def album_ja_na_fila(self, url: Optional[str]) -> bool:
        if not url:
            return False

        with self.conectar() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM album_queue
                WHERE url = ?
                LIMIT 1
                """,
                (url,),
            ).fetchone()

        return row is not None

    def adicionar_album_na_fila(self, album: dict[str, Any]) -> int:
        with self.conectar() as conn:
            self.inserir_album_sync(conn, album)

            posicao = int(
                conn.execute("SELECT COUNT(*) FROM album_queue").fetchone()[0]
            )

            conn.commit()

        return posicao

    def primeiro_album_da_fila(self) -> Optional[dict[str, Any]]:
        with self.conectar() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM album_queue
                ORDER BY queue_position ASC, id ASC
                LIMIT 1
                """
            ).fetchone()

        return self.row_para_album(row) if row else None

    def remover_album_por_id(self, queue_id: int) -> None:
        with self.conectar() as conn:
            conn.execute(
                """
                DELETE FROM album_queue
                WHERE id = ?
                """,
                (queue_id,),
            )

            conn.commit()

    def remover_album_por_indice(self, indice: int) -> Optional[dict[str, Any]]:
        offset = indice - 1

        with self.conectar() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM album_queue
                ORDER BY queue_position ASC, id ASC
                LIMIT 1 OFFSET ?
                """,
                (offset,),
            ).fetchone()

            if not row:
                return None

            album = self.row_para_album(row)

            conn.execute(
                """
                DELETE FROM album_queue
                WHERE id = ?
                """,
                (album["_queue_id"],),
            )

            conn.commit()

        return album

    def embaralhar_fila(self) -> int:
        with self.conectar() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM album_queue
                ORDER BY queue_position ASC, id ASC
                """
            ).fetchall()

            ids = [int(row["id"]) for row in rows]

            if len(ids) < 2:
                return len(ids)

            ordem_original = ids.copy()
            secrets.SystemRandom().shuffle(ids)

            if ids == ordem_original:
                ids = ids[1:] + ids[:1]

            for position, queue_id in enumerate(ids, start=1):
                conn.execute(
                    """
                    UPDATE album_queue
                    SET queue_position = ?
                    WHERE id = ?
                    """,
                    (position, queue_id),
                )

            conn.commit()

        return len(ids)

    def inserir_aotd_history(self, album: dict[str, Any]) -> None:
        nome = self.limitar_texto(album.get("nome") or "Álbum desconhecido", 200)
        artista = self.limitar_texto(album.get("artista") or "Artista desconhecido", 200)
        imagem = album.get("imagem") if self.is_valid_url(album.get("imagem")) else None
        
        with self.conectar() as conn:
            conn.execute(
                """
                INSERT INTO aotd_history (nome, artista, imagem)
                VALUES (?, ?, ?)
                """,
                (nome, artista, imagem),
            )
            
            conn.execute(
                """
                DELETE FROM aotd_history
                WHERE id NOT IN (
                    SELECT id FROM aotd_history
                    ORDER BY date_added DESC, id DESC
                    LIMIT 7
                )
                """
            )
            conn.commit()

    def get_aotd_history(self) -> list[dict[str, Any]]:
        with self.conectar() as conn:
            rows = conn.execute(
                """
                SELECT id, nome, artista, imagem, date_added
                FROM aotd_history
                ORDER BY date_added DESC, id DESC
                LIMIT 7
                """
            ).fetchall()
            
        history = []
        for row in rows:
            item = dict(row)
            item["nome"] = self.limitar_texto(item.get("nome") or "Álbum desconhecido", 200)
            item["artista"] = self.limitar_texto(item.get("artista") or "Artista desconhecido", 200)
            if not self.is_valid_url(item.get("imagem")):
                item["imagem"] = None
            history.append(item)

        return history

    # ========================================================
    #  Spotify
    # ========================================================

    def get_spotify(self) -> spotipy.Spotify:
        if not SPOTIPY_CLIENT_ID or not SPOTIPY_CLIENT_SECRET:
            raise RuntimeError(
                "As variáveis SPOTIFY_ID e SPOTIFY_SECRET não estão configuradas."
            )

        if self._spotify is None:
            self._spotify = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=SPOTIPY_CLIENT_ID,
                    client_secret=SPOTIPY_CLIENT_SECRET,
                ),
                requests_timeout=12,
                retries=2,
            )

        return self._spotify

    @staticmethod
    def classificar_lancamento_spotify(
        *,
        album_type: Optional[str],
        total_tracks: Any,
        nome: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        O Spotify não possui album_type='ep'.
        Ele costuma agrupar singles e EPs em album_type='single'.

        Regra prática do bot:
        - album_type='album' => permitido como Álbum.
        - album_type='compilation' => bloqueado.
        - album_type='single' com "EP" no nome => permitido como EP.
        - album_type='single' com 4+ faixas => permitido como EP.
        - album_type='single' com 1 a 3 faixas sem "EP" no nome => bloqueado como Single.
        """
        tipo_spotify = (album_type or "").strip().lower()

        try:
            faixas = int(total_tracks or 0)
        except (TypeError, ValueError):
            faixas = 0

        nome_lower = (nome or "").lower()
        tem_ep_no_nome = bool(re.search(r"(^|[\s\-\(\[\:])ep($|[\s\-\)\]\:])", nome_lower))

        if tipo_spotify == "album":
            return {
                "permitido": True,
                "tipo": "Álbum",
                "motivo": None,
            }

        if tipo_spotify == "compilation":
            return {
                "permitido": False,
                "tipo": "Coletânea",
                "motivo": (
                    "Esse lançamento parece ser uma **coletânea/compilação**. "
                    "A fila aceita apenas **álbuns** ou **EPs**."
                ),
            }

        if tipo_spotify == "single":
            if tem_ep_no_nome or faixas >= 4:
                return {
                    "permitido": True,
                    "tipo": "EP",
                    "motivo": None,
                }

            return {
                "permitido": False,
                "tipo": "Single",
                "motivo": (
                    "Esse lançamento parece ser um **single**. "
                    "A fila aceita apenas **álbuns** ou **EPs**."
                ),
            }

        if faixas <= 3 and not tem_ep_no_nome:
            return {
                "permitido": False,
                "tipo": "Single",
                "motivo": (
                    "Esse lançamento parece ser um **single**. "
                    "A fila aceita apenas **álbuns** ou **EPs**."
                ),
            }

        if tem_ep_no_nome or 4 <= faixas <= 6:
            return {
                "permitido": True,
                "tipo": "EP",
                "motivo": None,
            }

        return {
            "permitido": True,
            "tipo": "Álbum",
            "motivo": None,
        }

    @staticmethod
    def extrair_spotify_album_id(valor: str) -> Optional[str]:
        texto = str(valor or "").strip()
        spotify_id_pattern = r"[A-Za-z0-9]{22}"

        uri_match = re.fullmatch(
            rf"spotify:album:({spotify_id_pattern})",
            texto,
            flags=re.IGNORECASE,
        )
        if uri_match:
            return uri_match.group(1)

        if texto.lower().startswith(("open.spotify.com/", "www.open.spotify.com/")):
            texto = f"https://{texto}"

        try:
            parsed = urlparse(texto)
        except ValueError:
            return None

        if parsed.scheme.lower() not in {"http", "https"}:
            return None

        if (parsed.hostname or "").lower() not in {
            "open.spotify.com",
            "www.open.spotify.com",
        }:
            return None

        partes = [parte for parte in parsed.path.split("/") if parte]
        if len(partes) == 2 and partes[0].lower() == "album":
            album_id = partes[1]
        elif (
            len(partes) == 3
            and partes[0].lower().startswith("intl-")
            and partes[1].lower() == "album"
        ):
            album_id = partes[2]
        else:
            return None

        if re.fullmatch(spotify_id_pattern, album_id):
            return album_id

        return None

    @staticmethod
    def parece_localizador_spotify(valor: str) -> bool:
        texto = str(valor or "").strip()
        texto_lower = texto.lower()

        if texto_lower.startswith("spotify:"):
            return True

        if texto_lower.startswith(("open.spotify.com/", "www.open.spotify.com/")):
            texto = f"https://{texto}"

        try:
            parsed = urlparse(texto)
        except ValueError:
            return False

        return (parsed.hostname or "").lower() in {
            "open.spotify.com",
            "www.open.spotify.com",
        }

    def buscar_album_spotify_sync(self, query: str) -> Optional[dict[str, Any]]:
        sp = self.get_spotify()
        spotify_album_id = self.extrair_spotify_album_id(query)

        if spotify_album_id:
            album = sp.album(spotify_album_id)
        else:
            if self.parece_localizador_spotify(query):
                raise RuntimeError(
                    "O link do Spotify precisa apontar diretamente para um álbum ou EP válido."
                )

            resultados = sp.search(q=query, type="album", limit=1)
            albuns_encontrados = resultados.get("albums", {}).get("items", [])

            if not albuns_encontrados:
                return None

            album = albuns_encontrados[0]
        artista = album["artists"][0]

        total_tracks = album.get("total_tracks", "?")
        spotify_album_type = album.get("album_type")

        classificacao = self.classificar_lancamento_spotify(
            album_type=spotify_album_type,
            total_tracks=total_tracks,
            nome=album.get("name"),
        )

        try:
            artist_info = sp.artist(artista["id"])
            generos_raw = artist_info.get("genres", []) or []
        except Exception as exc:
            self.log_aotd_exception(
                exc,
                phase="spotify_artist_metadata",
                spotify_artist_id=artista.get("id"),
                spotify_album_id=album.get("id"),
            )
            generos_raw = []

        generos = generos_raw[:4]

        duracao_ms = 0

        try:
            tracks_page = sp.album_tracks(album["id"], limit=50)

            while tracks_page:
                duracao_ms += sum(
                    track.get("duration_ms", 0)
                    for track in tracks_page.get("items", [])
                )

                tracks_page = sp.next(tracks_page) if tracks_page.get("next") else None

        except Exception as exc:
            self.log_aotd_exception(
                exc,
                phase="spotify_album_tracks",
                spotify_album_id=album.get("id"),
            )
            duracao_ms = 0

        return {
            "nome": album.get("name", "Álbum desconhecido"),
            "artista": artista.get("name", "Artista desconhecido"),
            "url": album.get("external_urls", {}).get("spotify", ""),
            "imagem": album.get("images", [{}])[0].get("url")
            if album.get("images")
            else None,
            "generos": generos,
            "total_faixas": total_tracks,
            "duracao_ms": duracao_ms,
            "spotify_album_type": spotify_album_type,
            "tipo_lancamento": classificacao["tipo"],
            "lancamento_permitido": classificacao["permitido"],
            "motivo_bloqueio": classificacao["motivo"],
        }

    async def buscar_album_spotify(self, query: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self.buscar_album_spotify_sync, query)

    # ========================================================
    #  Utilidades
    # ========================================================

    @staticmethod
    def now_br() -> dt.datetime:
        return dt.datetime.now(BR_TZ)

    @staticmethod
    def fmt_bool(value: bool) -> str:
        return "Ativado" if value else "Desativado"

    @staticmethod
    def channel_mention(channel_id: Optional[int]) -> str:
        normalized_channel_id = AlbumDoDia.normalize_snowflake(channel_id)
        return f"<#{normalized_channel_id}>" if normalized_channel_id else "Não configurado"

    @staticmethod
    def build_role_ping(role_id: Optional[int]) -> Optional[str]:
        normalized_role_id = AlbumDoDia.normalize_snowflake(role_id)

        if normalized_role_id is None:
            return None

        return f"<@&{normalized_role_id}>"

    @staticmethod
    def parse_br_time(horario: str) -> Optional[tuple[int, int]]:
        if not horario:
            return None

        text = horario.strip().lower().replace(" ", "")

        patterns = (
            r"^(\d{1,2}):(\d{2})$",
            r"^(\d{1,2})h(\d{2})$",
            r"^(\d{1,2})h$",
            r"^(\d{1,2})$",
        )

        for pattern in patterns:
            match = re.fullmatch(pattern, text)

            if not match:
                continue

            hour = int(match.group(1))
            minute = 0

            if len(match.groups()) >= 2 and match.group(2):
                minute = int(match.group(2))

            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute

        return None

    @staticmethod
    def get_daily_time(config: dict[str, Any]) -> tuple[int, int]:
        try:
            hour = int(config.get("daily_hour", 12))
            minute = int(config.get("daily_minute", 0))
        except (TypeError, ValueError):
            return 12, 0

        if not 0 <= hour <= 23:
            hour = 12

        if not 0 <= minute <= 59:
            minute = 0

        return hour, minute

    @classmethod
    def format_daily_time(cls, config: dict[str, Any]) -> str:
        hour, minute = cls.get_daily_time(config)
        return f"{hour:02d}:{minute:02d} BRT"

    @staticmethod
    def limitar_texto(texto: Any, limite: int) -> str:
        texto = str(texto or "").strip()

        if len(texto) <= limite:
            return texto

        if limite <= 1:
            return texto[:limite]

        return texto[: limite - 1].rstrip() + "…"

    @staticmethod
    def is_valid_url(url: Any) -> bool:
        if not isinstance(url, str):
            return False

        try:
            parsed = urlparse(url.strip())
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        except ValueError:
            return False

    @staticmethod
    def normalize_snowflake(value: Any) -> Optional[int]:
        try:
            snowflake = int(value)
        except (TypeError, ValueError, OverflowError):
            return None

        return snowflake if snowflake > 0 else None

    @staticmethod
    def normalize_nonnegative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def next_daily_run(self, config: Optional[dict[str, Any]] = None) -> dt.datetime:
        config = config or self.carregar_config()

        hour, minute = self.get_daily_time(config)
        now = self.now_br()

        scheduled_today = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        if now < scheduled_today:
            return scheduled_today

        return scheduled_today + dt.timedelta(days=1)

    def queue_until_date(
        self,
        queue_size: int,
        config: Optional[dict[str, Any]] = None,
    ) -> Optional[dt.date]:
        if queue_size <= 0:
            return None

        return self.next_daily_run(config).date() + dt.timedelta(days=queue_size - 1)

    def role_display(self, guild: Optional[discord.Guild], role_id: Optional[int]) -> str:
        normalized_role_id = self.normalize_snowflake(role_id)

        if normalized_role_id is None:
            return "Não configurado"

        role = guild.get_role(normalized_role_id) if guild else None

        return role.mention if role else f"<@&{normalized_role_id}>"

    async def resolve_text_channel(
        self,
        channel_id: Optional[int],
    ) -> Optional[discord.abc.Messageable]:
        normalized_channel_id = self.normalize_snowflake(channel_id)

        if normalized_channel_id is None:
            return None

        channel = self.bot.get_channel(normalized_channel_id)

        if channel is not None and hasattr(channel, "send"):
            return channel

        try:
            fetched = await self.bot.fetch_channel(normalized_channel_id)

            if hasattr(fetched, "send"):
                return fetched

        except (discord.HTTPException, discord.Forbidden, discord.NotFound) as exc:
            self.log_aotd_exception(
                exc,
                phase="channel_resolution",
                configured_channel_id=normalized_channel_id,
            )
            return None

        return None

    async def resolve_album_channel(
        self,
        channel_id: Optional[int],
    ) -> Optional[discord.abc.Messageable]:
        return await self.resolve_text_channel(channel_id)

    async def send_destination(
        self,
        destino: discord.Interaction | discord.abc.Messageable,
        *,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        ephemeral: bool = False,
        allowed_mentions: Optional[discord.AllowedMentions] = None,
    ) -> Optional[discord.Message]:
        if isinstance(destino, discord.Interaction):
            if destino.response.is_done():
                return await destino.followup.send(
                    content=content,
                    embed=embed,
                    ephemeral=ephemeral,
                    allowed_mentions=allowed_mentions,
                    wait=True,
                )

            await destino.response.send_message(
                content=content,
                embed=embed,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )

            return await destino.original_response()

        return await destino.send(
            content=content,
            embed=embed,
            allowed_mentions=allowed_mentions,
        )

    # ========================================================
    #  Embed do Álbum
    # ========================================================

    def criar_embed_album(self, album: dict[str, Any]) -> discord.Embed:
        nome = self.limitar_texto(album.get("nome") or "Álbum desconhecido", 200)
        artista = self.limitar_texto(album.get("artista") or "Artista desconhecido", 200)
        url = album.get("url") if self.is_valid_url(album.get("url")) else None
        imagem = album.get("imagem")

        duracao_ms = self.normalize_nonnegative_int(album.get("duracao_ms"))
        minutos = duracao_ms // 60000
        segundos = (duracao_ms % 60000) // 1000

        generos = album.get("generos") or []
        if not isinstance(generos, list):
            generos = []
        total_faixas = album.get("total_faixas", "?")

        footer_parts = [
            f"🎵 {total_faixas} faixas · {minutos}min {segundos:02d}s"
        ]

        if generos:
            footer_parts.append("🎸 " + self.limitar_texto(", ".join(str(g) for g in generos[:4]), 500))

        embed = discord.Embed(
            title=f"🎧 {nome}",
            description=(
                f"O álbum de hoje é **{nome}** de **{artista}**.\n\n"
                f"E aí, já ouviu? O que achou?\n\n"
                f"{EMOJI_GOSTO} — Eu gosto desse álbum\n"
                f"{EMOJI_NAO_GOSTO} — Não gosto desse álbum\n"
                f"{EMOJI_NUNCA_OUVI} — Nunca ouvi esse álbum"
            ),
            color=discord.Color.blurple(),
            url=url,
        )

        if self.is_valid_url(imagem):
            embed.set_image(url=imagem)

        embed.set_footer(text=" | ".join(footer_parts))

        return embed

    def criar_embed_lista_paginada(
        self,
        *,
        fila: list[dict[str, Any]],
        config: dict[str, Any],
        page: int,
        per_page: int,
        guild: Optional[discord.Guild] = None,
    ) -> discord.Embed:
        total = len(fila)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))

        start = page * per_page
        end = min(start + per_page, total)
        pagina_atual = fila[start:end]

        data_final = self.queue_until_date(total, config)

        description_lines = [
            f"**{total}** álbuns/EPs na fila.",
            f"Mostrando **{start + 1}–{end}** de **{total}**.",
            "",
            f"⏰ Horário diário: **{self.format_daily_time(config)}**",
        ]

        if data_final:
            description_lines.append(
                f"🗓️ Cobertura estimada: até **{data_final.strftime('%d/%m/%Y')}**"
            )

        description_lines.append("")
        description_lines.append(
            "Use os botões abaixo para navegar sem floodar o chat. Finalmente civilização."
        )

        embed = discord.Embed(
            title="🗂️ Fila do Álbum do Dia",
            description="\n".join(description_lines),
            color=discord.Color.blurple(),
            timestamp=self.now_br(),
        )

        if guild and guild.icon:
            embed.set_author(name=guild.name, icon_url=guild.icon.url)

        for index, album in enumerate(pagina_atual, start=start + 1):
            nome = self.limitar_texto(album.get("nome", "Sem nome"), 110)
            artista = self.limitar_texto(album.get("artista", "Sem artista"), 80)
            sugerido_por = self.limitar_texto(
                album.get("sugerido_por") or "Comunidade",
                70,
            )

            field_name = self.limitar_texto(
                f"#{index:02d} · {nome} — {artista}",
                256,
            )

            embed.add_field(
                name=field_name,
                value=f"👤 Enviado por: **{sugerido_por}**",
                inline=False,
            )

        primeira_imagem = next(
            (
                album.get("imagem")
                for album in pagina_atual
                if self.is_valid_url(album.get("imagem"))
            ),
            None,
        )

        if primeira_imagem:
            embed.set_thumbnail(url=primeira_imagem)

        embed.set_footer(
            text=(
                f"Página {page + 1}/{total_pages} • "
                "Para remover: /aotd_config remover índice"
            )
        )

        return embed

    def criar_embed_notificacao_staff(
        self,
        *,
        album: dict[str, Any],
        interaction: discord.Interaction,
        posicao_fila: int,
    ) -> discord.Embed:
        nome = self.limitar_texto(album.get("nome") or "Álbum desconhecido", 900)
        artista = self.limitar_texto(album.get("artista") or "Artista desconhecido", 900)
        url = album.get("url") if self.is_valid_url(album.get("url")) else None
        imagem = album.get("imagem")

        duracao_ms = self.normalize_nonnegative_int(album.get("duracao_ms"))
        minutos = duracao_ms // 60000
        segundos = (duracao_ms % 60000) // 1000

        generos = album.get("generos") or []
        if not isinstance(generos, list):
            generos = []
        generos_txt = (
            self.limitar_texto(", ".join(str(g) for g in generos[:4]), 1000)
            if generos
            else "Sem gêneros encontrados"
        )

        tipo = album.get("tipo_lancamento") or "Álbum/EP"

        embed = discord.Embed(
            title="🛎️ Novo álbum/EP sugerido",
            description=(
                f"{interaction.user.mention} adicionou um novo lançamento na fila do "
                f"**Álbum do Dia**."
            ),
            color=discord.Color.gold(),
            url=url,
            timestamp=self.now_br(),
        )

        embed.add_field(name="Lançamento", value=f"**{nome}**", inline=True)
        embed.add_field(name="Artista", value=f"**{artista}**", inline=True)
        embed.add_field(name="Tipo", value=f"**{tipo}**", inline=True)
        embed.add_field(name="Posição na fila", value=f"`#{posicao_fila}`", inline=True)
        embed.add_field(name="Duração", value=f"{minutos}min {segundos:02d}s", inline=True)
        embed.add_field(name="Faixas", value=str(album.get("total_faixas", "?")), inline=True)
        embed.add_field(name="Gêneros", value=generos_txt, inline=False)

        if self.is_valid_url(imagem):
            embed.set_thumbnail(url=imagem)

        embed.set_footer(
            text=f"Sugerido por {interaction.user} • ID: {interaction.user.id}"
        )

        return embed

    def criar_embed_single_bloqueado(self, album: dict[str, Any]) -> discord.Embed:
        nome = self.limitar_texto(album.get("nome") or "Lançamento desconhecido", 900)
        artista = self.limitar_texto(album.get("artista") or "Artista desconhecido", 900)
        imagem = album.get("imagem")
        url = album.get("url") if self.is_valid_url(album.get("url")) else None
        motivo = album.get("motivo_bloqueio") or (
            "Esse lançamento não pode entrar na fila."
        )

        embed = discord.Embed(
            title="🚫 Single não permitido",
            description=(
                f"Encontrei **{nome}** — **{artista}**, mas ele não foi adicionado.\n\n"
                f"{motivo}\n\n"
                f"Procure pelo nome de um **álbum** ou **EP** e tente de novo."
            ),
            color=discord.Color.red(),
            url=url,
        )

        embed.add_field(
            name="Tipo detectado",
            value=f"**{album.get('tipo_lancamento', 'Single')}**",
            inline=True,
        )

        embed.add_field(
            name="Faixas",
            value=str(album.get("total_faixas", "?")),
            inline=True,
        )

        if self.is_valid_url(imagem):
            embed.set_thumbnail(url=imagem)

        return embed

    # ========================================================
    #  Notificação interna da Staff
    # ========================================================

    async def notificar_staff_nova_sugestao(
        self,
        *,
        interaction: discord.Interaction,
        album: dict[str, Any],
        posicao_fila: int,
    ) -> None:
        config = self.carregar_config()

        staff_channel_id = config.get("staff_notify_channel_id")
        staff_role_id = config.get("staff_notify_role_id")

        if not staff_channel_id:
            return

        channel = await self.resolve_text_channel(staff_channel_id)

        if channel is None:
            return

        role_ping = self.build_role_ping(staff_role_id)
        embed = self.criar_embed_notificacao_staff(
            album=album,
            interaction=interaction,
            posicao_fila=posicao_fila,
        )

        try:
            await channel.send(
                content=role_ping,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )

        except (discord.HTTPException, discord.Forbidden) as exc:
            self.log_aotd_exception(
                exc,
                interaction=interaction,
                phase="staff_notification_send",
                staff_channel_id=staff_channel_id,
                staff_role_id=staff_role_id,
                queue_position=posicao_fila,
                album_url=album.get("url"),
            )
            return

    # ========================================================
    #  Envio do Álbum
    # ========================================================

    async def despachar_album(
        self,
        destino: discord.Interaction | discord.abc.Messageable,
    ) -> bool:
        album_de_hoje = self.primeiro_album_da_fila()

        if not album_de_hoje:
            await self.send_destination(
                destino,
                content="⚠️ A fila está vazia. Adicionem mais álbuns/EPs com `/aotd sugerir`.",
                ephemeral=isinstance(destino, discord.Interaction),
            )
            return False

        config = self.carregar_config()
        role_ping = self.build_role_ping(config.get("album_role_id"))

        embed = self.criar_embed_album(album_de_hoje)

        msg = await self.send_destination(
            destino,
            content=role_ping,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

        if msg is None:
            return False

        self.remover_album_por_id(int(album_de_hoje["_queue_id"]))

        for emoji in (EMOJI_GOSTO, EMOJI_NAO_GOSTO, EMOJI_NUNCA_OUVI):
            try:
                await msg.add_reaction(emoji)
            except (discord.HTTPException, discord.Forbidden, TypeError) as exc:
                self.log_aotd_exception(
                    exc,
                    interaction=destino if isinstance(destino, discord.Interaction) else None,
                    phase="album_reaction_add",
                    message_id=getattr(msg, "id", None),
                    emoji=emoji,
                    queue_id=album_de_hoje.get("_queue_id"),
                )
                continue

        try:
            thread = await msg.create_thread(
                name=str(album_de_hoje.get("nome") or "Álbum do Dia")[:100],
                auto_archive_duration=10080,
            )

            sugerido_por_id = album_de_hoje.get("sugerido_por_id")

            if sugerido_por_id:
                await thread.send(f"✨ Sugerido por: <@{sugerido_por_id}>")
            else:
                await thread.send(
                    f"✨ Sugerido por: {album_de_hoje.get('sugerido_por', 'Comunidade')}"
                )

        except (discord.HTTPException, discord.Forbidden) as exc:
            self.log_aotd_exception(
                exc,
                interaction=destino if isinstance(destino, discord.Interaction) else None,
                phase="album_thread_create_or_send",
                message_id=getattr(msg, "id", None),
                queue_id=album_de_hoje.get("_queue_id"),
            )

        try:
            self.inserir_aotd_history(album_de_hoje)
        except Exception as exc:
            # O álbum já foi publicado e removido da fila. Falhar o comando neste
            # ponto induziria uma repetição enganosa e não recuperaria o histórico.
            self.log_aotd_exception(
                exc,
                interaction=destino if isinstance(destino, discord.Interaction) else None,
                phase="album_history_after_successful_dispatch",
                message_id=getattr(msg, "id", None),
                queue_id=album_de_hoje.get("_queue_id"),
            )

        return True

    # ========================================================
    #  Loop Automático
    # ========================================================

    @tasks.loop(minutes=1)
    async def enviar_album_automatico(self) -> None:
        await self.bot.wait_until_ready()

        config = self.carregar_config()

        if not config.get("auto_enabled", True):
            return

        hour, minute = self.get_daily_time(config)
        now = self.now_br()

        if now.hour != hour or now.minute != minute:
            return

        today_key = now.date().isoformat()

        if config.get("last_auto_sent_date") == today_key:
            return

        channel = await self.resolve_album_channel(config.get("album_channel_id"))

        if channel is None:
            return

        try:
            enviado = await self.despachar_album(channel)
        except Exception as exc:
            self.log_aotd_exception(
                exc,
                phase="automatic_album_dispatch",
                album_channel_id=config.get("album_channel_id"),
                scheduled_date=today_key,
            )
            return

        if enviado:
            config = self.carregar_config()
            config["last_auto_sent_date"] = today_key
            self.salvar_config(config)

    @enviar_album_automatico.before_loop
    async def before_enviar_album_automatico(self) -> None:
        await self.bot.wait_until_ready()

    @enviar_album_automatico.error
    async def enviar_album_automatico_error(self, error: BaseException) -> None:
        self.log_aotd_exception(error, phase="automatic_album_loop")

    async def send_aotd_recap(self, is_test: bool = False) -> bool:
        config = self.carregar_config()
        if not config.get("recap_enabled", False) and not is_test:
            return False

        channel = await self.resolve_album_channel(config.get("album_channel_id"))
        if channel is None:
            return False

        history = self.get_aotd_history()
        if not history:
            if is_test:
                await channel.send("Não há álbuns registrados nesta semana para o recap.")
            return False

        from modules.movies.recap_logic import generate_recap_collage, build_recap_embed
        import discord
        
        urls = [
            item["imagem"]
            for item in history
            if self.is_valid_url(item.get("imagem"))
        ]
        buffer = await generate_recap_collage(urls)
        
        embed, emojis = build_recap_embed(history, system_type="AOTD")

        kwargs = {"embed": embed}
        if buffer:
            file = discord.File(fp=buffer, filename="recap.jpg")
            embed.set_image(url="attachment://recap.jpg")
            kwargs["file"] = file
            
        try:
            msg = await channel.send(**kwargs)
            for emoji in emojis:
                try:
                    await msg.add_reaction(emoji)
                except discord.HTTPException as exc:
                    self.log_aotd_exception(
                        exc,
                        phase="recap_reaction_add",
                        message_id=getattr(msg, "id", None),
                        emoji=emoji,
                        is_test=is_test,
                    )
            return True
        except discord.Forbidden as exc:
            self.log_aotd_exception(
                exc,
                phase="recap_send_forbidden",
                album_channel_id=config.get("album_channel_id"),
                is_test=is_test,
                history_size=len(history),
            )
            return False

    @tasks.loop(minutes=1)
    async def enviar_recap_automatico(self) -> None:
        await self.bot.wait_until_ready()
        now = self.now_br()

        # Recap só roda na sexta-feira (weekday() == 4)
        if now.weekday() != 4:
            return

        config = self.carregar_config()
        if not config.get("recap_enabled", False):
            return

        r_hour = int(config.get("recap_hour", 18))
        r_minute = int(config.get("recap_minute", 0))

        if now.hour != r_hour or now.minute != r_minute:
            return

        today_key = f"recap_{now.date().isoformat()}"
        if config.get("last_recap_sent_date") == today_key:
            return

        enviado = await self.send_aotd_recap()
        if enviado:
            config = self.carregar_config()
            config["last_recap_sent_date"] = today_key
            self.salvar_config(config)

    @enviar_recap_automatico.before_loop
    async def before_enviar_recap_automatico(self) -> None:
        await self.bot.wait_until_ready()

    @enviar_recap_automatico.error
    async def enviar_recap_automatico_error(self, error: BaseException) -> None:
        self.log_aotd_exception(error, phase="automatic_recap_loop")

    # ========================================================
    #  Slash Commands — Usuários
    # ========================================================

    @aotd.command(
        name="sugerir",
        description="Sugere um álbum ou EP para a fila do Álbum do Dia.",
    )
    @app_commands.describe(
        nome_album="Nome ou link exato do álbum/EP no Spotify.",
    )
    async def sugerir(
        self,
        interaction: discord.Interaction,
        nome_album: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            album_info = await self.buscar_album_spotify(nome_album)

        except RuntimeError as exc:
            self.log_aotd_exception(
                exc,
                interaction=interaction,
                phase="spotify_album_lookup",
                query=nome_album,
            )
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        except Exception as exc:
            self.log_aotd_exception(
                exc,
                interaction=interaction,
                phase="spotify_album_search",
                query=nome_album,
            )
            await interaction.followup.send(
                "❌ Não consegui consultar o Spotify agora. Tenta de novo daqui a pouco.",
                ephemeral=True,
            )
            return

        if not album_info:
            await interaction.followup.send(
                f"❌ Não encontrei **{nome_album}** no Spotify.",
                ephemeral=True,
            )
            return

        if not album_info.get("lancamento_permitido", True):
            embed = self.criar_embed_single_bloqueado(album_info)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        album_info["sugerido_por"] = interaction.user.name
        album_info["sugerido_por_id"] = interaction.user.id

        album_url = album_info.get("url")

        if self.album_ja_na_fila(album_url):
            await interaction.followup.send(
                "⚠️ Esse álbum/EP já está na fila.",
                ephemeral=True,
            )
            return

        posicao_fila = self.adicionar_album_na_fila(album_info)

        tipo = self.limitar_texto(album_info.get("tipo_lancamento") or "Álbum/EP", 100)
        nome = self.limitar_texto(album_info.get("nome") or "Álbum desconhecido", 900)
        artista = self.limitar_texto(album_info.get("artista") or "Artista desconhecido", 900)
        album_url = album_info.get("url") if self.is_valid_url(album_info.get("url")) else None

        embed = discord.Embed(
            title="🎵 Álbum adicionado à fila!",
            description=(
                f"**{nome}** — {artista}\n\n"
                f"🏷️ Formato: **{tipo}**\n"
                f"📌 Posição na fila: **#{posicao_fila}**\n"
                f"Use `/aotd fila` para ver o tamanho da fila."
            ),
            color=discord.Color.green(),
            url=album_url,
        )

        if self.is_valid_url(album_info.get("imagem")):
            embed.set_thumbnail(url=album_info["imagem"])

        await interaction.followup.send(embed=embed, ephemeral=True)

        try:
            await self.notificar_staff_nova_sugestao(
                interaction=interaction,
                album=album_info,
                posicao_fila=posicao_fila,
            )
        except Exception as exc:
            # A sugestão e a confirmação ao usuário já foram concluídas. A
            # notificação interna é auxiliar e não deve transformar sucesso em erro.
            self.log_aotd_exception(
                exc,
                interaction=interaction,
                phase="staff_notification_after_successful_suggestion",
                queue_position=posicao_fila,
                album_url=album_info.get("url"),
            )

    @aotd.command(
        name="fila",
        description="Mostra quantos álbuns/EPs existem na fila.",
    )
    async def ver_fila(self, interaction: discord.Interaction) -> None:
        config = self.carregar_config()
        qtd = self.contar_fila()

        if qtd == 0:
            await interaction.response.send_message("📭 A fila está vazia.")
            return

        data_final = self.queue_until_date(qtd, config)

        await interaction.response.send_message(
            f"📚 Temos **{qtd}** álbuns/eps na fila.\n"
            f"⏰ Horário configurado: **{self.format_daily_time(config)}**.\n"
            f"🗓️ Teremos Álbuns do Dia até o dia "
            f"**{data_final.strftime('%d/%m/%Y')}**."
        )

    # ========================================================
    #  Slash Commands — Admin
    # ========================================================

    @aotd_config.command(
        name="canal",
        description="Define o canal onde o Álbum do Dia será enviado automaticamente.",
    )
    @app_commands.describe(
        canal="Canal de texto do Álbum do Dia. Deixe vazio para remover o canal configurado.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def configurar_canal(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
    ) -> None:
        config = self.carregar_config()
        config["album_channel_id"] = canal.id if canal else None
        self.salvar_config(config)

        if canal:
            await interaction.response.send_message(
                f"✅ Canal do Álbum do Dia configurado para {canal.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "✅ Canal do Álbum do Dia removido da configuração.",
                ephemeral=True,
            )

    @aotd_config.command(
        name="cargo",
        description="Define o cargo marcado no post do Álbum do Dia.",
    )
    @app_commands.describe(
        cargo="Cargo que será marcado. Deixe vazio para desativar o ping de cargo.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def configurar_cargo(
        self,
        interaction: discord.Interaction,
        cargo: Optional[discord.Role] = None,
    ) -> None:
        config = self.carregar_config()
        config["album_role_id"] = cargo.id if cargo else None
        self.salvar_config(config)

        if cargo:
            await interaction.response.send_message(
                f"✅ Cargo do Álbum do Dia configurado para {cargo.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "✅ Ping de cargo do Álbum do Dia desativado.",
                ephemeral=True,
            )

    @aotd_config.command(
        name="horario",
        description="Define o horário diário do Álbum do Dia no tempo de Brasília.",
    )
    @app_commands.describe(
        horario="Horário em Brasília. Exemplos: 12:00, 20:30, 7:05, 22h ou 8.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def configurar_horario(
        self,
        interaction: discord.Interaction,
        horario: str,
    ) -> None:
        parsed = self.parse_br_time(horario)

        if parsed is None:
            await interaction.response.send_message(
                "❌ Horário inválido. Use algo como `12:00`, `20:30`, `7:05`, `22h` ou `8`.",
                ephemeral=True,
            )
            return

        hour, minute = parsed

        config = self.carregar_config()
        config["daily_hour"] = hour
        config["daily_minute"] = minute
        self.salvar_config(config)

        next_run = self.next_daily_run(config)

        await interaction.response.send_message(
            f"✅ Horário automático do Álbum do Dia configurado para **{hour:02d}:{minute:02d} BRT**.\n"
            f"🗓️ Próximo envio previsto: **{next_run.strftime('%d/%m/%Y às %H:%M')} BRT**.",
            ephemeral=True,
        )

    @aotd_config.command(
        name="staff-canal",
        description="Define o canal interno onde a staff será avisada sobre novas sugestões.",
    )
    @app_commands.describe(
        canal="Canal exclusivo de admins/staff. Deixe vazio para desativar as notificações internas.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def configurar_staff_canal(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
    ) -> None:
        config = self.carregar_config()
        config["staff_notify_channel_id"] = canal.id if canal else None
        self.salvar_config(config)

        if canal:
            await interaction.response.send_message(
                f"✅ Canal de notificação da staff configurado para {canal.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "✅ Canal de notificação da staff removido. As notificações internas foram desativadas.",
                ephemeral=True,
            )

    @aotd_config.command(
        name="staff-cargo",
        description="Define o cargo de staff marcado quando alguém sugere um álbum.",
    )
    @app_commands.describe(
        cargo="Cargo de staff que será marcado. Deixe vazio para não marcar cargo nas notificações internas.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def configurar_staff_cargo(
        self,
        interaction: discord.Interaction,
        cargo: Optional[discord.Role] = None,
    ) -> None:
        config = self.carregar_config()
        config["staff_notify_role_id"] = cargo.id if cargo else None
        self.salvar_config(config)

        if cargo:
            await interaction.response.send_message(
                f"✅ Cargo de staff configurado para {cargo.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "✅ Cargo de staff removido. As notificações internas continuarão sem ping de cargo.",
                ephemeral=True,
            )

    @aotd_config.command(
        name="ativar",
        description="Ativa o envio automático diário do Álbum do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ativar_auto(self, interaction: discord.Interaction) -> None:
        config = self.carregar_config()
        config["auto_enabled"] = True
        self.salvar_config(config)

        aviso = ""

        if not config.get("album_channel_id"):
            aviso = (
                "\n⚠️ Configure um canal com `/aotd_config canal`, "
                "senão o envio automático não terá onde postar."
            )

        await interaction.response.send_message(
            f"✅ Envio automático ativado para todos os dias às **{self.format_daily_time(config)}**.\n"
            f"📌 Canal atual: {self.channel_mention(config.get('album_channel_id'))}"
            f"{aviso}",
            ephemeral=True,
        )

    @aotd_config.command(
        name="desativar",
        description="Desativa o envio automático diário do Álbum do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def desativar_auto(self, interaction: discord.Interaction) -> None:
        config = self.carregar_config()
        config["auto_enabled"] = False
        self.salvar_config(config)

        await interaction.response.send_message(
            "🛑 Envio automático do Álbum do Dia desativado.",
            ephemeral=True,
        )

    @aotd_config.command(
        name="shuffle",
        description="Embaralha aleatoriamente a ordem dos álbuns/EPs na fila. Admin.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def shuffle_fila(self, interaction: discord.Interaction) -> None:
        qtd = self.contar_fila()

        if qtd == 0:
            await interaction.response.send_message(
                "📭 A fila está vazia, então não tem o que embaralhar.",
                ephemeral=True,
            )
            return

        if qtd == 1:
            await interaction.response.send_message(
                "🎧 Só tem **1 álbum/EP** na fila. O shuffle ia só fingir trabalho.",
                ephemeral=True,
            )
            return

        total = self.embaralhar_fila()

        await interaction.response.send_message(
            f"🔀 Fila embaralhada com sucesso!\n"
            f"🎵 **{total}** álbuns/EPs foram reorganizados aleatoriamente.",
            ephemeral=True,
        )

    @aotd_config.command(
        name="status",
        description="Mostra a configuração atual do Álbum do Dia.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction) -> None:
        config = self.carregar_config()
        fila = self.carregar_fila()

        qtd = len(fila)
        data_final = self.queue_until_date(qtd, config)
        proximo = fila[0] if fila else None

        spotify_ok = bool(SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET)
        next_run = self.next_daily_run(config)

        embed = discord.Embed(
            title="📊 Status do Álbum do Dia",
            color=discord.Color.blurple(),
            timestamp=self.now_br(),
        )

        embed.add_field(
            name="Automático",
            value=self.fmt_bool(bool(config.get("auto_enabled", True))),
            inline=True,
        )

        embed.add_field(
            name="Horário",
            value=f"**{self.format_daily_time(config)}**",
            inline=True,
        )

        embed.add_field(
            name="Próximo envio",
            value=next_run.strftime("**%d/%m/%Y às %H:%M BRT**"),
            inline=True,
        )

        embed.add_field(
            name="Spotify",
            value="Configurado" if spotify_ok else "Sem credenciais",
            inline=True,
        )

        embed.add_field(
            name="Canal do álbum",
            value=self.channel_mention(config.get("album_channel_id")),
            inline=True,
        )

        embed.add_field(
            name="Cargo do álbum",
            value=self.role_display(interaction.guild, config.get("album_role_id")),
            inline=True,
        )

        embed.add_field(
            name="Fila",
            value=f"{qtd} Álbuns",
            inline=True,
        )

        embed.add_field(
            name="Banco",
            value=f"`{DB_FILE.name}`",
            inline=True,
        )

        embed.add_field(
            name="Canal da staff",
            value=self.channel_mention(config.get("staff_notify_channel_id")),
            inline=True,
        )

        embed.add_field(
            name="Cargo da staff",
            value=self.role_display(interaction.guild, config.get("staff_notify_role_id")),
            inline=True,
        )

        embed.add_field(
            name="Notificação da staff",
            value="Ativada" if config.get("staff_notify_channel_id") else "Desativada",
            inline=True,
        )

        if proximo:
            embed.add_field(
                name="Próximo lançamento",
                value="Oculto no status público da fila. Use `/aotd_config lista` se precisar auditar.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Próximo lançamento",
                value="Nenhum lançamento na fila.",
                inline=False,
            )

        if data_final:
            embed.add_field(
                name="Cobertura estimada",
                value=f"Até **{data_final.strftime('%d/%m/%Y')}**",
                inline=False,
            )

        dicas = []

        if not config.get("album_channel_id"):
            dicas.append("Use /aotd_config canal para definir onde o post automático vai cair.")

        if not config.get("staff_notify_channel_id"):
            dicas.append("Use /aotd_config staff-canal para ativar avisos internos de sugestões.")

        dicas.append("Use /aotd_config horario para mudar o horário diário em Brasília.")
        dicas.append("Use /aotd_config shuffle para embaralhar a fila.")

        embed.set_footer(text=" ".join(dicas))

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @aotd_config.command(
        name="lista",
        description="Lista os álbuns/EPs agendados na fila. Admin.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def listar_fila(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        fila = self.carregar_fila()

        if not fila:
            await interaction.followup.send(
                "📭 A fila está vazia.",
                ephemeral=True,
            )
            return

        config = self.carregar_config()

        view = AOTDQueueListView(
            cog=self,
            interaction=interaction,
            fila=fila,
            config=config,
            per_page=8,
        )

        embed = self.criar_embed_lista_paginada(
            fila=fila,
            config=config,
            page=0,
            per_page=view.per_page,
            guild=interaction.guild,
        )

        view.message = await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            wait=True,
        )

    @aotd_config.command(
        name="remover",
        description="Remove um álbum/EP da fila pelo índice. Admin.",
    )
    @app_commands.describe(
        indice="Número do lançamento na /aotd_config lista.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def remover(
        self,
        interaction: discord.Interaction,
        indice: int,
    ) -> None:
        if indice < 1:
            await interaction.response.send_message(
                "❌ Índice inválido.",
                ephemeral=True,
            )
            return

        removido = self.remover_album_por_indice(indice)

        if not removido:
            await interaction.response.send_message(
                "❌ Índice inválido.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🗑️ **{removido.get('nome', 'Lançamento')}** removido da fila.",
            ephemeral=True,
        )

    @aotd_config.command(
        name="testar-album",
        description="Força o envio do próximo álbum/EP agora. Admin.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def testar_album(self, interaction: discord.Interaction) -> None:
        await self.despachar_album(interaction)

    @aotd_config.command(
        name="test_recap",
        description="Força o envio do Recap Semanal do Álbum do Dia agora. Admin.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def test_recap(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        enviado = await self.send_aotd_recap(is_test=True)
        if not enviado:
            await interaction.followup.send(
                "Não foi possível enviar o Recap (Verifique o canal, permissões ou se o recap está vazio).", 
                ephemeral=True
            )

    @aotd_config.command(
        name="toggle_recap",
        description="Ativa ou desativa o envio automático do Recap Semanal de álbuns (Sexta-feira).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def toggle_recap(self, interaction: discord.Interaction) -> None:
        config = self.carregar_config()
        estado = not config.get("recap_enabled", False)
        config["recap_enabled"] = estado
        self.salvar_config(config)

        status_text = "Ligado" if estado else "Desligado"
        await interaction.response.send_message(
            f"O Recap Semanal do Álbum do Dia foi **{status_text}**.",
            ephemeral=True,
        )

    @aotd_config.command(
        name="recap_time",
        description="Define o horário do Recap Semanal do Álbum do Dia (Sextas) no formato HH:MM.",
    )
    @app_commands.describe(horario="Horário em formato HH:MM, usando America/Sao_Paulo.")
    @app_commands.checks.has_permissions(administrator=True)
    async def recap_time(self, interaction: discord.Interaction, horario: str) -> None:
        import re
        TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        if TIME_PATTERN.fullmatch(horario) is None:
            await interaction.response.send_message(
                "Formato inválido. Use `HH:MM`, por exemplo `18:00`.",
                ephemeral=True,
            )
            return

        hour_str, min_str = horario.split(":")
        config = self.carregar_config()
        config["recap_hour"] = int(hour_str)
        config["recap_minute"] = int(min_str)
        self.salvar_config(config)

        await interaction.response.send_message(
            f"Horário do Recap Semanal configurado para `{horario}`.",
            ephemeral=True,
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.MissingPermissions):
            msg = "🚫 Você precisa ser administrador para usar esse comando."
        elif isinstance(error, app_commands.BotMissingPermissions):
            missing = ", ".join(error.missing_permissions)
            msg = f"🔧 Estou sem as permissões necessárias: {missing}."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Aguarde {error.retry_after:.1f}s antes de usar esse comando novamente."
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = "🚫 Esse comando só pode ser usado dentro de um servidor."
        else:
            error_id = self.log_aotd_exception(
                original,
                interaction=interaction,
                phase="app_command_error",
                wrapper_type=f"{type(error).__module__}.{type(error).__name__}",
            )
            msg = f"{AOTD_GENERIC_ERROR} O erro foi registrado no console (ID `{error_id}`)."

        try:
            await self.send_error_response(interaction, msg)
        except discord.HTTPException as response_error:
            self.log_aotd_exception(
                response_error,
                interaction=interaction,
                phase="app_command_error_response",
                original_exception_type=f"{type(original).__module__}.{type(original).__name__}",
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AlbumDoDia(bot))
    bot.tree.remove_command("aotd_config")
