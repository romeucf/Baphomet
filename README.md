# Baphomet

Bot Discord de alta performance construído com `discord.py 2.7+`, `asyncio`, `aiosqlite`, `redis.asyncio` e `Pillow`.

## Arquitetura do Projeto

- **`main.py`**: Ponto de entrada do bot, ciclo de vida assíncrono e carregamento dinâmico de Cogs.
- **`core/`**: Infraestrutura centralizada (paths, logger, pool Redis e conexões de banco de dados).
  - `core/paths.py`: Fonte única da verdade para caminhos (`DATA_DIR`, `FONTS_DIR`, `ASSETS_DIR`, etc.).
  - `core/redis_state.py`: Gerenciamento assíncrono de cache L1 via Redis.
  - `core/logger.py`: Formatação padronizada de logs e captura de exceções.
- **`cogs/`**: Módulos de comandos e listeners (AOTD, Blacklist, Movie, Iceberg, Tierlist, Vínculos, XP, Segurança, etc.).
- **`modules/`**: Serviços de domínio, renderização gráfica e integração (Wikipedia, Spotify, processamento de mídia).
- **`data/`**: Diretório exclusivo para persistência local (bancos SQLite e configurações JSON).
- **`docs/`**: Políticas de privacidade e termos de serviço.

## Instalação e Execução

1. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure o arquivo `.env` com suas credenciais (`DISCORD_TOKEN`, `REDIS_URL`, `SPOTIFY_ID`, `SPOTIFY_SECRET`).

3. Inicie o bot:
   ```bash
   python main.py
   ```
