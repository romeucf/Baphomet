from __future__ import annotations

import json
import re
import uuid
from pathlib import PurePosixPath
from typing import Any

def dumps_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def new_uuid() -> str:
    return str(uuid.uuid4())


def load_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    if isinstance(value, dict):
        return value
    return {"value": value}


def metadata_to_json(metadata: dict[str, Any] | None) -> str:
    return dumps_json(metadata or {})


def normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def validate_relative_storage_path(storage_path: str) -> None:
    path = PurePosixPath(storage_path)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("storage_path deve ser relativo e não pode conter '..'.")
    if not str(path) or str(path) == ".":
        raise ValueError("storage_path não pode ser vazio.")


def validate_asset_hash(asset_hash: str) -> None:
    if not re.fullmatch(r"[a-fA-F0-9]{64}", asset_hash):
        raise ValueError("asset_hash deve ser um SHA-256 hexadecimal de 64 caracteres.")


async def fetch_one(conn: Any, query: str, params: tuple[Any, ...] = ()) -> Any | None:
    rows = await conn.execute_fetchall(query, params)
    return rows[0] if rows else None
