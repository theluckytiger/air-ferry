"""Opt-in Cloudflare quick tunnel when the phone cannot reach the PC on LAN."""

from __future__ import annotations

import os
import platform
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from air_ferry.utils import app_data_dir

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)
_DIR = app_data_dir()
_LOG = _DIR / "cloudflared.log"

_ASSET = {
    ("win32", "AMD64"): ("cloudflared-windows-amd64.exe", "cloudflared.exe"),
    ("win32", "x86_64"): ("cloudflared-windows-amd64.exe", "cloudflared.exe"),
    ("linux", "x86_64"): ("cloudflared-linux-amd64", "cloudflared"),
    ("linux", "AMD64"): ("cloudflared-linux-amd64", "cloudflared"),
    ("linux", "aarch64"): ("cloudflared-linux-arm64", "cloudflared"),
    ("linux", "arm64"): ("cloudflared-linux-arm64", "cloudflared"),
    ("darwin", "x86_64"): ("cloudflared-darwin-amd64", "cloudflared"),
    ("darwin", "arm64"): ("cloudflared-darwin-arm64", "cloudflared"),
}


def _asset_names() -> tuple[str, str] | None:
    key = (sys.platform, platform.machine())
    if key in _ASSET:
        return _ASSET[key]
    # Normalize common aliases
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "cloudflared-linux-amd64", "cloudflared"
        if machine in {"aarch64", "arm64"}:
            return "cloudflared-linux-arm64", "cloudflared"
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return "cloudflared-windows-amd64.exe", "cloudflared.exe"
    if sys.platform == "darwin":
        if machine in {"x86_64", "amd64"}:
            return "cloudflared-darwin-amd64", "cloudflared"
        if machine in {"arm64", "aarch64"}:
            return "cloudflared-darwin-arm64", "cloudflared"
    return None


def _binary() -> Path | None:
    names = _asset_names()
    if names is None:
        return None
    remote, local_name = names
    dest = _DIR / local_name
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        return dest
    _DIR.mkdir(parents=True, exist_ok=True)
    url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        + remote
    )
    tmp = dest.with_suffix(dest.suffix + ".download")
    try:
        with urllib.request.urlopen(url, timeout=180) as resp:
            tmp.write_bytes(resp.read())
        tmp.replace(dest)
        if sys.platform != "win32":
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return dest
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


def _popen(args: list[str]) -> subprocess.Popen[str]:
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        # Own process group so stop() can kill cloudflared + children cleanly.
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


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
        self._proc = _popen(
            [str(exe), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"]
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
        self.url = None
        if proc is None:
            return
        if proc.poll() is not None:
            return

        def _signal(sig: int) -> None:
            try:
                if sys.platform != "win32" and proc.pid:
                    os.killpg(proc.pid, sig)
                else:
                    proc.send_signal(sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    if sig == signal.SIGKILL or sys.platform == "win32":
                        proc.kill()
                    else:
                        proc.terminate()
                except Exception:
                    pass

        try:
            _signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                _signal(signal.SIGKILL if sys.platform != "win32" else signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
