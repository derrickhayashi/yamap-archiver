from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .errors import YamapError


def read_json_list(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YamapError(f"Could not read {label} {path}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise YamapError(f"{label.capitalize()} {path} must contain a JSON list of objects")
    return data


def unique_by(items: Iterable[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        value = str(item.get(key) or "").strip()
        if not value:
            raise YamapError(f"{label} entry is missing {key}")
        if value in result:
            raise YamapError(f"Duplicate {key} {value} in {label}")
        result[value] = item
    return result


def _write_atomically(path: Path, writer: Any, *, overwrite: bool) -> None:
    path = path.expanduser()
    if path.exists() and not overwrite:
        raise YamapError(f"Refusing to overwrite {path}; pass --overwrite if that is intentional")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        writer(handle)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def write_json_safely(path: Path, data: Any, *, overwrite: bool) -> None:
    def render(handle: Any) -> None:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    _write_atomically(path, render, overwrite=overwrite)


def write_geojson_safely(path: Path, data: dict[str, Any], *, overwrite: bool) -> None:
    def render(handle: Any) -> None:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    _write_atomically(path, render, overwrite=overwrite)
