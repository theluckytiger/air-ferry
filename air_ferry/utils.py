"""Shared helpers for framing, hashing, and path safety."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

CHUNK_SIZE = 64 * 1024
SERVICE_TYPE = "_air-ferry._tcp.local."
DEFAULT_PORT = 8765

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str) -> str:
    """Return a basename that cannot escape the destination directory."""
    cleaned = Path(name).name.strip().strip(".")
    cleaned = _UNSAFE_NAME.sub("_", cleaned)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unsafe filename: {name!r}")
    return cleaned


def sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def encode_message(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def decode_message(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("control message must be a JSON object")
    return data


def format_bytes(n: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{n} B"


def unique_destination(directory: Path, filename: str) -> Path:
    """Pick a non-colliding path under directory for filename."""
    target = directory / filename
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    index = 1
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1
