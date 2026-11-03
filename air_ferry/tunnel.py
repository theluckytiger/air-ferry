"""Opt-in Cloudflare quick tunnel when the phone cannot reach the PC on LAN."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)
_DIR = Path.home() / "AppData" / "Local" / "air-ferry"
_BIN = _DIR / "cloudflared.exe"
_LOG = _DIR / "cloudflared.log"
_DOWNLOAD = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    "cloudflared-windows-amd64.exe"
)


def _binary() -> Path | None:
    if sys.platform != "win32":
        return None
    if _BIN.is_file() and _BIN.stat().st_size > 1_000_000:
        return _BIN
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = _BIN.with_suffix(".download")
    try:
        with urllib.request.urlopen(_DOWNLOAD, timeout=180) as resp:
            tmp.write_bytes(resp.read())
        tmp.replace(_BIN)
        return _BIN
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


def _wait_reachable(url: str, *, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= getattr(resp, "status", 200) < 500:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


class QuickTunnel:
    """Runs cloudflared; call start() once, stop() on shutdown."""

    def __init__(self) -> None:
        self.url: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._drain: threading.Thread | None = None

    def start(self, port: int, *, timeout: float = 60.0) -> str | None:
        exe = _binary()
        if exe is None:
            return None
        _DIR.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [str(exe), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        assert self._proc.stdout is not None
        deadline = time.time() + timeout
        buf: list[str] = []
        while time.time() < deadline and self._proc.poll() is None:
            line = self._proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            buf.append(line)
            match = _URL_RE.search(line)
            if match:
                self.url = match.group(0).rstrip("/")
                # Keep draining logs or the pipe fills and cloudflared freezes.
                self._drain = threading.Thread(
                    target=self._pump_logs,
                    args=(self._proc,),
                    daemon=True,
                )
                self._drain.start()
                _wait_reachable(self.url, timeout=15.0)
                return self.url

        try:
            _LOG.write_text("".join(buf[-200:]), encoding="utf-8")
        except Exception:
            pass
        self.stop()
        return None

    @staticmethod
    def _pump_logs(proc: subprocess.Popen[str]) -> None:
        try:
            assert proc.stdout is not None
            with _LOG.open("a", encoding="utf-8") as fh:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                        continue
                    fh.write(line)
                    fh.flush()
        except Exception:
            pass

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.url = None
