from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

from core.paths import FONTS_DIR


class FontAssetError(RuntimeError):
    """Erro claro para execucoes que exigem fontes locais estritas."""


class FontManager:
    """Carrega fontes locais do projeto com cache e fallback previsivel do Pillow."""

    _FONT_FILES = {
        "regular": "Poppins-Regular.ttf",
        "bold": "Poppins-Bold.ttf",
        "display": "Poppins-Bold.ttf",
        "futura": "FuturaCEB.otf",
        "montserrat": "Montserrat-Black.ttf",
    }

    def __init__(self, *, strict: bool = False, font_dirs: tuple[Path, ...] | None = None) -> None:
        self.strict = strict
        self.font_dirs = font_dirs or (FONTS_DIR,)
        self._cache: dict[tuple[str, int], ImageFont.ImageFont] = {}

    def font(self, size: int, weight: str = "regular") -> ImageFont.ImageFont:
        """Retorna uma fonte local pelo peso logico: regular, bold ou display."""

        normalized_weight = weight if weight in self._FONT_FILES else "regular"
        cache_key = (normalized_weight, size)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        path = self._resolve(normalized_weight)
        if path is not None:
            loaded = ImageFont.truetype(str(path), size=size)
        elif self.strict:
            expected = ", ".join(str(path) for path in self.expected_paths(normalized_weight))
            raise FontAssetError(f"Fonte local ausente para '{normalized_weight}'. Esperado em: {expected}")
        else:
            loaded = self._fallback(size)

        self._cache[cache_key] = loaded
        return loaded

    def expected_paths(self, weight: str) -> tuple[Path, ...]:
        filename = self._FONT_FILES.get(weight, self._FONT_FILES["regular"])
        return tuple(font_dir / filename for font_dir in self.font_dirs)

    def _resolve(self, weight: str) -> Path | None:
        for path in self.expected_paths(weight):
            if path.exists():
                return path
        return None

    @staticmethod
    def _fallback(size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()
