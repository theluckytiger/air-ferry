"""WebSocket receiver: accept a file stream and verify integrity."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from air_ferry.utils import (
    CHUNK_SIZE,
    decode_message,
    encode_message,
    safe_filename,
    unique_destination,
)

ProgressCallback = Callable[[int, int, str], Awaitable[None] | None]


class TransferError(Exception):
    pass


async def _maybe_await(result: object) -> None:
    if inspect.isawaitable(result):
        await result


async def _handle_transfer(
    websocket: ServerConnection,
    dest_dir: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    raw = await websocket.recv()
    if isinstance(raw, bytes):
        raise TransferError("expected offer as text frame")

    offer = decode_message(raw)
    if offer.get("type") != "offer":
        raise TransferError("first message must be an offer")

    try:
        filename = safe_filename(str(offer["filename"]))
        size = int(offer["size"])
        expected_sha = str(offer["sha256"]).lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise TransferError(f"malformed offer: {exc}") from exc

    if size < 0:
        raise TransferError("negative file size")
    if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        raise TransferError("invalid sha256 in offer")

    await websocket.send(encode_message({"type": "accept", "filename": filename}))

    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = unique_destination(dest_dir, filename)
    digest = hashlib.sha256()
    received = 0

    # Write to a temp file in the same directory, then rename on success.
    with tempfile.NamedTemporaryFile(
        dir=dest_dir,
        prefix=".air-ferry-",
        suffix=".part",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            while received < size:
                message = await websocket.recv()
                if isinstance(message, str):
                    control = decode_message(message)
                    if control.get("type") == "abort":
                        raise TransferError(control.get("reason", "sender aborted"))
                    raise TransferError(f"unexpected control mid-transfer: {control.get('type')}")

                if len(message) > CHUNK_SIZE:
                    raise TransferError("chunk exceeds 64KB limit")
                if received + len(message) > size:
                    raise TransferError("received more bytes than offered")

                tmp.write(message)
                digest.update(message)
                received += len(message)

                if on_progress:
                    await _maybe_await(on_progress(received, size, filename))

            # Optional done frame from sender.
            try:
                trailing = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                if isinstance(trailing, str):
                    done = decode_message(trailing)
                    if done.get("type") not in {"done", "complete"}:
                        raise TransferError(f"unexpected trailing message: {done.get('type')}")
            except (asyncio.TimeoutError, ConnectionClosed):
                pass

            if received != size:
                raise TransferError(f"size mismatch: got {received}, expected {size}")

            actual_sha = digest.hexdigest()
            if actual_sha != expected_sha:
                raise TransferError("sha256 mismatch")

            tmp.flush()
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    tmp_path.replace(final_path)
    await websocket.send(
        encode_message({"type": "ok", "filename": final_path.name, "sha256": actual_sha})
    )
    return final_path


async def run_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    dest_dir: Path | str = ".",
    on_progress: ProgressCallback | None = None,
    on_complete: Callable[[Path], Awaitable[None] | None] | None = None,
    on_error: Callable[[Exception], Awaitable[None] | None] | None = None,
) -> None:
    """Serve forever; each connection may deliver one file."""
    dest = Path(dest_dir).expanduser().resolve()

    async def handler(websocket: ServerConnection) -> None:
        try:
            path = await _handle_transfer(websocket, dest, on_progress)
            if on_complete:
                await _maybe_await(on_complete(path))
        except Exception as exc:
            try:
                await websocket.send(
                    encode_message({"type": "error", "reason": str(exc)})
                )
            except Exception:
                pass
            if on_error:
                await _maybe_await(on_error(exc))

    async with serve(handler, host, port, max_size=CHUNK_SIZE + 1024):
        await asyncio.Future()
