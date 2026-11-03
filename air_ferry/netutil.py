"""LAN IPs + Windows firewall / Private network profile."""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import time
from typing import Iterable

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_ips_cache: tuple[float, list[str]] | None = None


def _run(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def _usable(ip: str) -> bool:
    return bool(ip) and not ip.startswith(("127.", "169.254.", "0."))


def _is_tether(ip: str) -> bool:
    """USB / phone hotspot ranges (iPhone, Android, Windows ICS)."""
    return ip.startswith(("172.20.10.", "192.168.42.", "192.168.43.", "192.168.137."))


def _adapter_ips() -> list[str]:
    """All IPv4 addresses on this machine (getaddrinfo alone often misses USB tether)."""
    found: list[str] = []

    def add(ip: str) -> None:
        if _usable(ip) and ip not in found:
            found.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass

    if sys.platform == "win32":
        try:
            r = _run(
                [
                    "powershell",
                    "-NoProfile",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    "Get-NetIPAddress -AddressFamily IPv4 |"
                    " Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |"
                    " Select-Object -ExpandProperty IPAddress",
                ],
                timeout=5,
            )
            for line in (r.stdout or "").splitlines():
                add(line.strip())
        except Exception:
            pass

    return found


def lan_ips() -> list[str]:
    """Prefer phone USB/hotspot, then 192.168.*, then the rest. Re-call after plugging cable."""
    global _ips_cache
    now = time.monotonic()
    if _ips_cache is not None and now - _ips_cache[0] < 2.0:
        return list(_ips_cache[1])

    every = _adapter_ips()
    ips: list[str] = []

    def add(ip: str) -> None:
        if _usable(ip) and ip not in ips:
            ips.append(ip)

    for ip in every:
        if _is_tether(ip):
            add(ip)
    for ip in every:
        if ip.startswith("192.168."):
            add(ip)
    for ip in every:
        add(ip)

    result = ips or ["127.0.0.1"]
    _ips_cache = (now, result)
    return list(result)


def local_ip() -> str:
    ips = lan_ips()
    return ips[0] if ips else "127.0.0.1"


def lan_urls(port: int, ips: Iterable[str] | None = None) -> list[str]:
    return [f"http://{ip}:{port}" for ip in (ips if ips is not None else lan_ips())]


def _rule_exists(name: str) -> bool:
    return _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"]).returncode == 0


def _listener_exes() -> list[str]:
    primary = os.path.realpath(sys.executable)
    out = [primary]
    base, name = os.path.split(primary)
    twin = (
        "pythonw.exe"
        if name.lower() == "python.exe"
        else "python.exe"
        if name.lower() == "pythonw.exe"
        else None
    )
    if twin:
        path = os.path.join(base, twin)
        if os.path.isfile(path):
            out.append(path)
    return out


def ensure_windows_lan_access(port: int) -> dict[str, str | bool]:
    """
    One-time (or when rules missing) elevate:
    - allow this Python + TCP port on Profile Any (works on Public and Private)
    - also flip profiles to Private when we already need UAC
    """
    if sys.platform != "win32":
        return {"ok": True, "detail": "non-Windows"}

    import ctypes

    port_name = f"air-ferry port {port}"
    # Profile Any rules already cover Public Wi-Fi / tether; do not re-prompt only to flip Private.
    if _rule_exists("air-ferry") and _rule_exists(port_name):
        return {"ok": True, "detail": "firewall OK"}

    chunks: list[str] = []
    for i, exe in enumerate(_listener_exes()):
        exe_q = exe.replace("'", "''")
        label = "air-ferry" if i == 0 else "air-ferry-pythonw"
        chunks.append(
            f"Remove-NetFirewallRule -DisplayName '{label}' -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -DisplayName '{label}' -Direction Inbound -Action Allow "
            f"-Program '{exe_q}' -Profile Any | Out-Null; "
            f"Get-NetFirewallApplicationFilter -Program '{exe_q}' -ErrorAction SilentlyContinue "
            f"| Get-NetFirewallRule | Where-Object {{ $_.Action -eq 'Block' }} "
            f"| Remove-NetFirewallRule; "
        )
    chunks.append(
        f"Remove-NetFirewallRule -DisplayName '{port_name}' -ErrorAction SilentlyContinue; "
        f"New-NetFirewallRule -DisplayName '{port_name}' -Direction Inbound "
        f"-Action Allow -Protocol TCP -LocalPort {port} -Profile Any | Out-Null; "
        "Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -ne 'Private' } | "
        "ForEach-Object { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex "
        "-NetworkCategory Private -ErrorAction SilentlyContinue }; "
    )

    b64 = base64.b64encode("".join(chunks).encode("utf-16-le")).decode("ascii")
    rc = int(
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            "powershell.exe",
            f"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {b64}",
            None,
            0,
        )
    )
    if rc <= 32:
        return {"ok": False, "detail": "firewall NOT set: click Yes on the UAC prompt"}

    for _ in range(60):
        time.sleep(0.5)
        if _rule_exists("air-ferry") or _rule_exists(port_name):
            return {"ok": True, "detail": "firewall OK"}

    return {"ok": False, "detail": "firewall NOT set: approve UAC, then retry"}


def prepare_windows_firewall(port: int, *, allow_elevate: bool = True) -> dict[str, str | bool]:
    if not allow_elevate:
        return {"ok": True, "detail": "skipped"}
    return ensure_windows_lan_access(port)
