"""mDNS advertise / browse for air-ferry receivers on the LAN."""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

from air_ferry.utils import SERVICE_TYPE


@dataclass(frozen=True)
class Peer:
    name: str
    host: str
    port: int

    @property
    def endpoint(self) -> str:
        return f"ws://{self.host}:{self.port}"


def _local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class Advertiser:
    """Publish an air-ferry receiver via Zeroconf."""

    def __init__(self, port: int, name: str | None = None) -> None:
        self.port = port
        self.instance = name or f"air-ferry-{socket.gethostname()}"
        self._zc: Zeroconf | None = None
        self._info: ServiceInfo | None = None

    def start(self) -> None:
        host = _local_ip()
        self._zc = Zeroconf()
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.instance}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(host)],
            port=self.port,
            properties={"path": "/"},
            server=f"{self.instance}.local.",
        )
        self._zc.register_service(self._info)

    def stop(self) -> None:
        if self._zc and self._info:
            self._zc.unregister_service(self._info)
        if self._zc:
            self._zc.close()
        self._zc = None
        self._info = None

    def __enter__(self) -> Advertiser:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class _Collector:
    def __init__(self) -> None:
        self.peers: dict[str, Peer] = {}
        self._zc: Zeroconf | None = None

    def on_change(
        self,
        zc: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is ServiceStateChange.Removed:
            self.peers.pop(name, None)
            return

        info = zc.get_service_info(service_type, name, timeout=2000)
        if not info or not info.addresses:
            return

        host = socket.inet_ntoa(info.addresses[0])
        label = name[: -len(f".{SERVICE_TYPE}")] if name.endswith(f".{SERVICE_TYPE}") else name
        self.peers[name] = Peer(name=label, host=host, port=info.port)


async def discover(timeout: float = 3.0) -> list[Peer]:
    """Browse the LAN for air-ferry receivers."""
    collector = _Collector()
    zc = Zeroconf()
    collector._zc = zc
    browser = ServiceBrowser(zc, SERVICE_TYPE, handlers=[collector.on_change])
    try:
        await asyncio.sleep(timeout)
    finally:
        browser.cancel()
        zc.close()

    return sorted(collector.peers.values(), key=lambda p: p.name.lower())


def discover_sync(timeout: float = 3.0) -> list[Peer]:
    """Blocking browse used when an event loop is not yet running."""
    collector = _Collector()
    zc = Zeroconf()
    browser = ServiceBrowser(zc, SERVICE_TYPE, handlers=[collector.on_change])
    try:
        time.sleep(timeout)
    finally:
        browser.cancel()
        zc.close()
    return sorted(collector.peers.values(), key=lambda p: p.name.lower())
