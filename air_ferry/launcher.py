"""Tray launcher: FastAPI/uvicorn + browser + system tray (no extra window)."""

from __future__ import annotations

import argparse
import atexit
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from air_ferry.utils import DEFAULT_PORT, app_state_dir, default_download_dir

LOG_PATH = app_state_dir() / "air-ferry.log"


def _log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _hide_console() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def _pick_port(start: int) -> int:
    for port in range(start, start + 30):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.close()
            return port
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
    return start


def _wait_listening(port: int, timeout: float = 10.0) -> bool:
    waited = 0.0
    while waited < timeout:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.05)
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            pass
        finally:
            sock.close()
        threading.Event().wait(0.05)
        waited += 0.05
    return False


def _tray_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=(15, 143, 120, 255))
    draw.rectangle((18, 28, 46, 38), fill=(255, 255, 255, 255))
    draw.polygon([(32, 16), (46, 30), (18, 30)], fill=(255, 255, 255, 255))
    return img


def _copy_text(text: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            user32.OpenClipboard(0)
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = kernel32.GlobalAlloc(0x0042, len(data))
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(13, handle)
            user32.CloseClipboard()
        except Exception as exc:
            _log(f"clipboard failed: {exc}")
        return

    # Linux / macOS: prefer Wayland, then X11 helpers, then pbcopy
    try:
        if shutil.which("wl-copy"):
            subprocess.run(
                ["wl-copy"],
                check=False,
                timeout=5,
                input=text,
                text=True,
            )
            return
        if shutil.which("xclip"):
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                check=False,
                timeout=5,
                input=text,
                text=True,
            )
            return
        if shutil.which("xsel"):
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                check=False,
                timeout=5,
                input=text,
                text=True,
            )
            return
        if sys.platform == "darwin" and shutil.which("pbcopy"):
            subprocess.run(
                ["pbcopy"],
                check=False,
                timeout=5,
                input=text,
                text=True,
            )
            return
        _log("clipboard: no wl-copy/xclip/xsel/pbcopy found")
    except Exception as exc:
        _log(f"clipboard failed: {exc}")


def _show_fatal(message: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "air-ferry", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "-u", "critical", "air-ferry", message[:200]],
                check=False,
                timeout=5,
            )
        except Exception:
            pass


def run(
    *,
    port: int = DEFAULT_PORT,
    host: str = "0.0.0.0",
    name: str | None = None,
    dest: Path | None = None,
    open_browser: bool = True,
) -> None:
    import uvicorn
    from pystray import Icon, Menu, MenuItem

    from air_ferry.netutil import lan_ips, lan_urls, local_ip, prepare_firewall
    from air_ferry.server import create_app

    _log(f"--- start exe={sys.executable!r} platform={sys.platform!r}")

    chosen = _pick_port(port)
    firewall = prepare_firewall(chosen, allow_elevate=True)
    _log(f"firewall: {firewall}")

    instance = name or f"air-ferry-{socket.gethostname()}"
    dest_dir = (dest or default_download_dir()).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    lan = local_ip()
    lan_url = (lan_urls(chosen) or [f"http://{lan}:{chosen}"])[0]
    local_url = f"http://127.0.0.1:{chosen}"
    share_url = lan_url

    app = create_app(port=chosen, dest_dir=dest_dir, instance_name=instance)
    app.state.hub.firewall = firewall
    app.state.hub.addresses = lan_ips() or [lan]
    app.state.hub.host_ip = lan

    config = uvicorn.Config(
        app,
        host=host,
        port=chosen,
        log_level="warning",
        lifespan="on",
        access_log=False,
        # PyInstaller/frozen: uvicorn's default dictConfig formatters fail to load.
        log_config=None,
    )
    server = uvicorn.Server(config)

    def serve() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(server.serve())
        except Exception:
            _log("server crashed:\n" + traceback.format_exc())

    threading.Thread(target=serve, daemon=True).start()

    if not _wait_listening(chosen):
        _log("server failed to listen")
        _show_fatal(f"Could not start on port {chosen}.\nLog: {LOG_PATH}")
        return

    _log(f"listening {local_url} / {lan_url}")

    if open_browser:
        webbrowser.open(local_url)

    _hide_console()

    state: dict = {"icon": None, "share_url": share_url}

    def open_ui(icon=None, item=None) -> None:
        webbrowser.open(local_url)

    def copy_link(icon=None, item=None) -> None:
        _copy_text(state["share_url"])

    def _request(method: str, path: str) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{chosen}{path}",
            method=method,
            data=b"" if method == "POST" else None,
        )
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def phone_link(icon=None, item=None) -> None:
        open_now = not bool(getattr(app.state.hub, "tunnel_url", None))
        try:
            data = _request("POST" if open_now else "DELETE", "/api/tunnel")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            _log(f"tunnel request failed: {exc}")
            _show_fatal("Tunnel action failed.\nCheck internet on this PC.")
            return
        if not data.get("ok"):
            _show_fatal(data.get("detail") or "Tunnel action failed.")
            return
        info = data.get("info") or {}
        url = info.get("url") or lan_url
        state["share_url"] = url
        if open_now and data.get("url"):
            _copy_text(data["url"])
        _log(f"tunnel: {'open ' + data.get('url', '') if open_now else 'closed'}")
        if state["icon"] is not None:
            state["icon"].title = f"air-ferry - {url}"

    def _stop_tunnel_now() -> None:
        hub = app.state.hub
        tunnel = getattr(hub, "_tunnel", None)
        hub._tunnel = None
        hub.tunnel_url = None
        if tunnel is not None:
            try:
                tunnel.stop()
            except Exception as exc:
                _log(f"tunnel stop failed: {exc}")

    atexit.register(_stop_tunnel_now)

    def quit_app(icon=None, item=None) -> None:
        _stop_tunnel_now()
        server.should_exit = True
        if state["icon"] is not None:
            state["icon"].stop()

    menu = Menu(
        MenuItem("Open air-ferry", open_ui, default=True),
        MenuItem("Copy link", copy_link),
        MenuItem("Open / close tunnel", phone_link),
        MenuItem("Quit", quit_app),
    )
    icon = Icon("air-ferry", _tray_image(), f"air-ferry - {share_url}", menu)
    state["icon"] = icon
    try:
        icon.run()
    finally:
        _stop_tunnel_now()
        server.should_exit = True


def main(argv: list[str] | None = None) -> None:
    try:
        parser = argparse.ArgumentParser(prog="air-ferry")
        parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("-n", "--name", default=None)
        parser.add_argument("-d", "--dest", type=Path, default=None)
        parser.add_argument("--no-browser", action="store_true")
        args = parser.parse_args(argv)
        run(
            port=args.port,
            host=args.host,
            name=args.name,
            dest=args.dest,
            open_browser=not args.no_browser,
        )
    except SystemExit:
        raise
    except Exception:
        err = traceback.format_exc()
        _log(err)
        _hide_console()
        _show_fatal(f"air-ferry failed:\n\n{err[-700:]}\n\nLog: {LOG_PATH}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
