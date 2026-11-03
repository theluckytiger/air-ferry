"""Entry: tray `air-ferry`, optional terminal `air-ferry-tui`."""

from __future__ import annotations

import io
import socket
import webbrowser
from pathlib import Path
from typing import Optional

import qrcode
import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from air_ferry import __version__
from air_ferry.launcher import main as gui_main
from air_ferry.netutil import lan_ips, lan_urls, local_ip, prepare_windows_firewall
from air_ferry.server import create_app
from air_ferry.utils import DEFAULT_PORT, default_download_dir

console = Console()

tui_app = typer.Typer(
    name="air-ferry-tui",
    help="Optional terminal launcher.",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=False,
)


def main() -> None:
    gui_main()


def _qr_ascii(url: str) -> str:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue().rstrip()


@tui_app.callback()
def tui_main(
    ctx: typer.Context,
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p"),
    host: str = typer.Option("0.0.0.0", "--host"),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    dest: Optional[Path] = typer.Option(None, "--dest", "-d"),
    open_browser: bool = typer.Option(True, "--open-browser/--no-browser"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    firewall = prepare_windows_firewall(port, allow_elevate=True)
    instance = name or f"air-ferry-{socket.gethostname()}"
    dest_dir = (dest or default_download_dir()).expanduser().resolve()
    lan_ip = local_ip()
    urls = lan_urls(port) or [f"http://{lan_ip}:{port}"]

    app = create_app(port=port, dest_dir=dest_dir, instance_name=instance)
    app.state.hub.firewall = firewall
    app.state.hub.addresses = lan_ips() or [lan_ip]
    app.state.hub.host_ip = lan_ip

    console.print(
        Panel.fit(
            Text.from_markup(
                f"[bold]air-ferry[/]  [dim]v{__version__}[/]\n"
                f"[cyan]{urls[0]}[/]\n"
                f"[dim]{firewall.get('detail', '')}[/]"
            ),
            border_style="cyan",
            title="ready",
        )
    )
    console.print(_qr_ascii(urls[0]))
    console.print()

    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}")

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="on")
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


@tui_app.command("version")
def version() -> None:
    console.print(__version__)
