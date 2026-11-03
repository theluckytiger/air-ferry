"""FastAPI app: web UI, mDNS, WebSocket room transfers."""

from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import qrcode
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from qrcode.image.svg import SvgPathImage

from air_ferry import __version__
from air_ferry.discovery import Advertiser, PeerWatcher, local_ip
from air_ferry.netutil import lan_ips, lan_urls
from air_ferry.tunnel import QuickTunnel
from air_ferry.utils import (
    CHUNK_SIZE,
    decode_message,
    default_download_dir,
    encode_message,
    format_bytes,
    safe_filename,
    unique_destination,
)

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_SHARED = 40


class TransferError(Exception):
    pass


@dataclass
class RoomClient:
    id: str
    label: str
    ws: WebSocket


def _ephemeral_dir() -> Path:
    root = Path(tempfile.gettempdir()) / "air-ferry-ephemeral"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@dataclass
class SharedFile:
    id: str
    filename: str
    size: int
    sha256: str
    path: Path
    from_id: str
    from_label: str
    persisted: bool = True
    created: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "from_id": self.from_id,
            "from_label": self.from_label,
            "created": self.created,
            "download_url": f"/api/files/{self.id}",
            "size_label": format_bytes(self.size),
            "persisted": self.persisted,
            # Only for phone→PC keeps in Downloads/air-ferry (host uploads stay ephemeral).
            "saved_path": str(self.path.resolve()) if self.persisted else "",
        }


class Hub:
    def __init__(
        self,
        *,
        port: int,
        dest_dir: Path,
        instance_name: str,
        enable_mdns: bool = True,
    ) -> None:
        self.port = port
        self.dest_dir = dest_dir
        self.instance_name = instance_name
        self.enable_mdns = enable_mdns
        self.host_ip = local_ip()
        self.addresses = lan_ips() or [self.host_ip]
        self.firewall: dict[str, Any] = {"ok": True, "detail": ""}
        self.tunnel_url: str | None = None
        self._tunnel: QuickTunnel | None = None
        self._tunnel_lock = asyncio.Lock()
        self.advertiser = Advertiser(port=port, name=instance_name)
        self.watcher = PeerWatcher()
        self.clients: dict[str, RoomClient] = {}
        self.files: dict[str, SharedFile] = {}
        self._file_order: list[str] = []
        self._peer_task: asyncio.Task | None = None

    def info(self) -> dict[str, Any]:
        # Refresh each call so USB tether / Wi-Fi changes update the QR.
        self.addresses = lan_ips() or [self.host_ip]
        self.host_ip = local_ip()
        lan = lan_urls(self.port, self.addresses) or [
            f"http://{self.host_ip}:{self.port}"
        ]
        # One share link: tunnel after opt-in, otherwise best LAN/tether address.
        url = self.tunnel_url or lan[0]
        return {
            "name": self.instance_name,
            "version": __version__,
            "host": self.host_ip,
            "port": self.port,
            "url": url,
            "tunnel": self.tunnel_url,
            "addresses": list(self.addresses),
            "dest": str(self.dest_dir),
            "firewall": self.firewall,
        }

    async def enable_tunnel(self) -> dict[str, Any]:
        """Start an internet relay. LAN blocks cannot be detected from this PC alone."""
        async with self._tunnel_lock:
            if self.tunnel_url:
                return {"ok": True, "url": self.tunnel_url, "info": self.info()}
            tunnel = QuickTunnel()
            url = await asyncio.to_thread(tunnel.start, self.port)
            if not url:
                tunnel.stop()
                return {
                    "ok": False,
                    "detail": "Could not open tunnel (needs outbound internet).",
                    "info": self.info(),
                }
            self._tunnel = tunnel
            self.tunnel_url = url
            payload = {"ok": True, "url": url, "info": self.info()}
        await self.broadcast({"type": "info", "info": self.info()})
        return payload

    async def disable_tunnel(self) -> dict[str, Any]:
        async with self._tunnel_lock:
            tunnel = self._tunnel
            self._tunnel = None
            self.tunnel_url = None
        if tunnel is not None:
            await asyncio.to_thread(tunnel.stop)
        info = self.info()
        await self.broadcast({"type": "info", "info": info})
        return {"ok": True, "info": info}

    def clients_payload(self) -> list[dict[str, str]]:
        return [{"id": c.id, "label": c.label} for c in self.clients.values()]

    def files_payload(self) -> list[dict[str, Any]]:
        return [
            self.files[fid].as_dict()
            for fid in reversed(self._file_order)
            if fid in self.files
        ]

    async def broadcast(self, payload: dict[str, Any], *, exclude: WebSocket | None = None) -> None:
        message = encode_message(payload)
        dead: list[str] = []
        for cid, client in self.clients.items():
            if exclude is not None and client.ws is exclude:
                continue
            try:
                await client.ws.send_text(message)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self.clients.pop(cid, None)

    async def send(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        await ws.send_text(encode_message(payload))

    def remember_file(self, shared: SharedFile) -> None:
        self.files[shared.id] = shared
        self._file_order.append(shared.id)
        while len(self._file_order) > MAX_SHARED:
            old_id = self._file_order.pop(0)
            old = self.files.pop(old_id, None)
            if old is not None and not old.persisted:
                _unlink_quiet(old.path)

    async def _presence_loop(self) -> None:
        last_host: str | None = None
        while True:
            await self.broadcast({"type": "presence", "clients": self.clients_payload()})
            # USB tether / Wi-Fi can appear after launch; push a fresh QR when the IP changes.
            info = self.info()
            key = info["url"] if info.get("tunnel") else info["host"]
            if key != last_host:
                last_host = key
                await self.broadcast({"type": "info", "info": info})
            await asyncio.sleep(3.0)

    async def start(self) -> None:
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        if self.enable_mdns:
            await asyncio.to_thread(self.advertiser.start)
            await asyncio.to_thread(self.watcher.start)
        self._peer_task = asyncio.create_task(self._presence_loop())

    async def stop(self) -> None:
        # Tunnel first so quit / crash paths never leave cloudflared running.
        tunnel = self._tunnel
        self._tunnel = None
        self.tunnel_url = None
        if tunnel is not None:
            try:
                await asyncio.to_thread(tunnel.stop)
            except Exception:
                pass
        for shared in list(self.files.values()):
            if not shared.persisted:
                _unlink_quiet(shared.path)
        self.files.clear()
        self._file_order.clear()
        if self._peer_task is not None:
            self._peer_task.cancel()
            try:
                await self._peer_task
            except (asyncio.CancelledError, RuntimeError):
                pass
            self._peer_task = None
        if self.enable_mdns:
            await asyncio.to_thread(self.watcher.stop)
            await asyncio.to_thread(self.advertiser.stop)


def create_app(
    *,
    port: int = 8000,
    dest_dir: Path | None = None,
    instance_name: str | None = None,
    enable_mdns: bool = True,
) -> FastAPI:
    import socket

    dest = (dest_dir or default_download_dir()).expanduser().resolve()
    name = instance_name or f"air-ferry-{socket.gethostname()}"
    hub = Hub(port=port, dest_dir=dest, instance_name=name, enable_mdns=enable_mdns)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await hub.start()
        yield
        await hub.stop()

    app = FastAPI(title="air-ferry", version=__version__, lifespan=lifespan)
    app.state.hub = hub

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        return hub.info()

    @app.post("/api/tunnel")
    async def start_tunnel() -> dict[str, Any]:
        return await hub.enable_tunnel()

    @app.delete("/api/tunnel")
    async def stop_tunnel() -> dict[str, Any]:
        return await hub.disable_tunnel()

    @app.get("/api/qr.svg")
    async def qr_svg(u: str | None = None) -> Response:
        url = (u or "").strip() or hub.info()["url"]
        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")

    @app.get("/api/files")
    async def list_files() -> dict[str, Any]:
        return {"files": hub.files_payload()}

    @app.get("/api/files/{file_id}")
    async def download_file(file_id: str) -> FileResponse:
        shared = hub.files.get(file_id)
        if shared is None or not shared.path.is_file():
            raise HTTPException(status_code=404, detail="file gone")
        return FileResponse(
            path=shared.path,
            filename=shared.filename,
            media_type="application/octet-stream",
            content_disposition_type="attachment",
        )

    @app.websocket("/ws/ui")
    async def ui_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        client_id = uuid.uuid4().hex[:10]
        client = RoomClient(id=client_id, label="Device", ws=websocket)
        hub.clients[client_id] = client

        await hub.send(
            websocket,
            {
                "type": "hello",
                "client_id": client_id,
                "info": hub.info(),
                "clients": hub.clients_payload(),
                "files": hub.files_payload(),
            },
        )
        await hub.broadcast(
            {"type": "presence", "clients": hub.clients_payload()},
            exclude=websocket,
        )

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("text") is None:
                    continue
                data = decode_message(message["text"])
                msg_type = data.get("type")
                if msg_type == "hello":
                    label = str(data.get("label") or "Device").strip()[:40] or "Device"
                    client.label = label
                    await hub.broadcast({"type": "presence", "clients": hub.clients_payload()})
                    continue
                if msg_type == "share":
                    await _handle_share(hub, client, data)
                    continue
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.pop(client_id, None)
            await hub.broadcast({"type": "presence", "clients": hub.clients_payload()})

    @app.get("/")
    async def index() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__ASSET_V__", __version__)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store"},
        )

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app


async def _handle_share(hub: Hub, client: RoomClient, meta: dict) -> None:
    websocket = client.ws
    try:
        filename = safe_filename(str(meta["filename"]))
        size = int(meta["size"])
    except (KeyError, TypeError, ValueError) as exc:
        await hub.send(websocket, {"type": "error", "reason": f"bad share meta: {exc}"})
        return

    if size < 0:
        await hub.send(websocket, {"type": "error", "reason": "negative size"})
        return

    # Host PC shares: keep only an ephemeral copy for peers (do not clutter Downloads).
    # Phone / remote shares: persist under Downloads/air-ferry.
    persist = bool(meta.get("persist", True))
    transfer_id = str(meta.get("id") or uuid.uuid4().hex)
    t0 = time.perf_counter()
    digest = hashlib.sha256()
    received = 0
    staging = hub.dest_dir if persist else _ephemeral_dir()
    staging.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None

    await hub.send(websocket, {"type": "accept", "id": transfer_id, "filename": filename})
    await hub.broadcast(
        {
            "type": "progress",
            "id": transfer_id,
            "filename": filename,
            "received": 0,
            "total": size,
            "speed_mbps": 0.0,
            "from_id": client.id,
            "from_label": client.label,
        }
    )

    try:
        with tempfile.NamedTemporaryFile(
            dir=staging,
            prefix=".air-ferry-up-",
            suffix=".part",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            saw_done = size == 0
            while received < size or not saw_done:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    raise TransferError("uploader disconnected")
                data = message.get("bytes")
                if data is not None:
                    if saw_done or received >= size:
                        raise TransferError("unexpected binary after transfer finished")
                    if len(data) > CHUNK_SIZE:
                        raise TransferError("chunk exceeds 64KB limit")
                    if received + len(data) > size:
                        raise TransferError("more bytes than declared")
                    tmp.write(data)
                    digest.update(data)
                    received += len(data)
                    elapsed = max(time.perf_counter() - t0, 1e-6)
                    await hub.broadcast(
                        {
                            "type": "progress",
                            "id": transfer_id,
                            "filename": filename,
                            "received": received,
                            "total": size,
                            "speed_mbps": round((received / elapsed) / (1024 * 1024), 2),
                            "from_id": client.id,
                            "from_label": client.label,
                        }
                    )
                    continue

                text = message.get("text")
                if not text:
                    raise TransferError("expected binary chunk or done")
                control = decode_message(text)
                ctype = control.get("type")
                if ctype == "abort":
                    raise TransferError("share aborted")
                if ctype in {"done", "complete"}:
                    if received != size:
                        raise TransferError("done before all bytes received")
                    saw_done = True
                    continue
                raise TransferError(f"unexpected control: {ctype}")

        sha = digest.hexdigest()
        final = unique_destination(staging, filename)
        assert tmp_path is not None
        tmp_path.replace(final)
        tmp_path = None

        shared = SharedFile(
            id=transfer_id,
            filename=final.name,
            size=size,
            sha256=sha,
            path=final,
            from_id=client.id,
            from_label=client.label,
            persisted=persist,
        )
        hub.remember_file(shared)
        await hub.send(websocket, {"type": "complete", "id": transfer_id, **shared.as_dict()})
        await hub.broadcast({"type": "file", "file": shared.as_dict()})
    except Exception as exc:
        err = {"type": "error", "id": transfer_id, "reason": str(exc), "filename": filename}
        try:
            await hub.send(websocket, err)
        except Exception:
            pass
        await hub.broadcast(err)
    finally:
        if tmp_path is not None:
            _unlink_quiet(tmp_path)
