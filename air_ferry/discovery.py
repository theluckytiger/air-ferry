"""mDNS advertise / browse for air-ferry peers."""

from __future__ import annotations

import asyncio
import socket
import threading
from dataclasses import asdict, dataclass

from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

from air_ferry.netutil import local_ip
from air_ferry.utils import SERVICE_TYPE

__all__ = ["Advertiser", "Peer", "PeerWatcher", "discover", "local_ip"]


@dataclass(frozen=True)
class Peer:
    name: str
    host: str
    port: int

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def as_dict(self) -> dict:
        return {**asdict(self), "http_url": self.http_url, "id": f"{self.host}:{self.port}"}


class Advertiser:
    def __init__(self, port: int, name: str | None = None) -> None:
        self.port = port
        self.instance = name or f"air-ferry-{socket.gethostname()}"
        self._zc: Zeroconf | None = None
        self._info: ServiceInfo | None = None

    def start(self) -> None:
        host = local_ip()
        self._zc = Zeroconf()
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.instance}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(host)],
            port=self.port,
            properties={"path": "/", "ui": "1"},
            server=f"{self.instance}.local.",
        )
        self._zc.register_service(self._info, allow_name_change=True)

    def stop(self) -> None:
        if self._zc and self._info:
            try:
                self._zc.unregister_service(self._info)
            except Exception:
                pass
        if self._zc:
            self._zc.close()
        self._zc = None
        self._info = None


class PeerWatcher:
    def __init__(self) -> None:
        self._peers: dict[str, Peer] = {}
        self._lock = threading.Lock()
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None

    def start(self) -> None:
        if self._zc is not None:
            return
        self._zc = Zeroconf()
        self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, handlers=[self._on_change])

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._zc is not None:
            self._zc.close()
            self._zc = None
        with self._lock:
            self._peers.clear()

    def _on_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
        **_kwargs: object,
    ) -> None:
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                self._peers.pop(name, None)
            return
        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if not info or not info.addresses:
            return
        host = socket.inet_ntoa(info.addresses[0])
        label = (
            name[: -len(f".{SERVICE_TYPE}")]
            if name.endswith(f".{SERVICE_TYPE}")
            else name
        )
        with self._lock:
            self._peers[name] = Peer(name=label, host=host, port=info.port)

    def snapshot(self, exclude: tuple[str, int] | None = None) -> list[Peer]:
        with self._lock:
            peers = list(self._peers.values())
        if exclude is not None:
            ex_host, ex_port = exclude
            peers = [
                p
                for p in peers
                if not (p.port == ex_port and p.host in {ex_host, "127.0.0.1", local_ip()})
            ]
        return sorted(peers, key=lambda p: p.name.lower())


async def discover(timeout: float = 3.0) -> list[Peer]:
    watcher = PeerWatcher()
    watcher.start()
    try:
        await asyncio.sleep(timeout)
        return watcher.snapshot()
    finally:
        watcher.stop()
