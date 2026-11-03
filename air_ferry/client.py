"""WebSocket sender: stream a local file to a discovered receiver."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from air_ferry.utils import (
    CHUNK_SIZE,
    decode_message,
    encode_message,
    sha256_file,
)

ProgressCallback = Callable[[int, int, str], Awaitable[None] | None]


async def _maybe_await(result: object) -> None:
    if inspect.isawaitable(result):
        await result


async def send_file(
    path: Path | str,
    uri: str,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Offer and stream path to uri. Returns the receiver's ok payload."""
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"not a file: {file_path}")

    size = file_path.stat().st_size
    digest = sha256_file(file_path)
    filename = file_path.name

    async with connect(uri, max_size=CHUNK_SIZE + 1024) as ws:
        await ws.send(
            encode_message(
                {
                    "type": "offer",
                    "filename": filename,
                    "size": size,
                    "sha256": digest,
                }
            )
        )

        reply_raw = await ws.recv()
        if isinstance(reply_raw, bytes):
            raise RuntimeError("expected text accept frame")
        reply = decode_message(reply_raw)
        if reply.get("type") == "error":
            raise RuntimeError(reply.get("reason", "receiver rejected transfer"))
        if reply.get("type") != "accept":
            raise RuntimeError(f"unexpected reply: {reply.get('type')}")

        sent = 0
        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                await ws.send(chunk)
                sent += len(chunk)
                if on_progress:
                    await _maybe_await(on_progress(sent, size, filename))

        await ws.send(encode_message({"type": "done", "sha256": digest}))

        try:
            final_raw = await ws.recv()
        except ConnectionClosed as exc:
            raise RuntimeError("connection closed before confirmation") from exc

        if isinstance(final_raw, bytes):
            raise RuntimeError("expected text confirmation frame")
        final = decode_message(final_raw)
        if final.get("type") == "error":
            raise RuntimeError(final.get("reason", "transfer failed on receiver"))
        if final.get("type") != "ok":
            raise RuntimeError(f"unexpected confirmation: {final.get('type')}")
        if final.get("sha256") != digest:
            raise RuntimeError("receiver reported a different sha256")

        return final
