"""Typer entrypoints: air-ferry listen / air-ferry send."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Prompt
from rich.table import Table

from air_ferry import __version__
from air_ferry.client import send_file
from air_ferry.discovery import Advertiser, discover
from air_ferry.server import run_server
from air_ferry.utils import DEFAULT_PORT, format_bytes

app = typer.Typer(
    name="air-ferry",
    help="LAN file transfer over WebSockets with mDNS discovery.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console(stderr=True)


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


@app.callback()
def main_callback() -> None:
    """air-ferry — send files across the LAN without a cloud relay."""


@app.command("listen")
def listen(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="TCP port to bind."),
    dest: Path = typer.Option(
        Path("."),
        "--dest",
        "-d",
        help="Directory where received files are written.",
        file_okay=False,
    ),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        "-n",
        help="mDNS instance name (defaults to air-ferry-<hostname>).",
    ),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address."),
) -> None:
    """Advertise on the LAN and accept incoming transfers."""
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    instance = name or f"air-ferry-{socket.gethostname()}"

    console.print(
        f"[bold]air-ferry[/] listen  [dim]v{__version__}[/]\n"
        f"  saving to  [cyan]{dest}[/]\n"
        f"  advertising [cyan]{instance}[/] on port [cyan]{port}[/]"
    )

    async def _run() -> None:
        advertiser = Advertiser(port=port, name=instance)
        advertiser.start()
        console.print("[green]waiting for senders…[/]  (Ctrl+C to stop)\n")

        task_ids: dict[str, int] = {}

        with _progress() as progress:

            async def on_progress(received: int, total: int, filename: str) -> None:
                if filename not in task_ids:
                    task_ids[filename] = progress.add_task(
                        f"receiving {filename}", total=total
                    )
                progress.update(task_ids[filename], completed=received)

            async def on_complete(path: Path) -> None:
                console.print(
                    f"[green]✓[/] saved [bold]{path.name}[/] "
                    f"([dim]{format_bytes(path.stat().st_size)}[/]) → {path}"
                )
                task_ids.pop(path.name, None)

            async def on_error(exc: Exception) -> None:
                console.print(f"[red]transfer failed:[/] {exc}")

            try:
                await run_server(
                    host=host,
                    port=port,
                    dest_dir=dest,
                    on_progress=on_progress,
                    on_complete=on_complete,
                    on_error=on_error,
                )
            finally:
                advertiser.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


@app.command("send")
def send(
    file: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False),
    host: Optional[str] = typer.Option(
        None,
        "--host",
        "-H",
        help="Receiver host (skips mDNS discovery).",
    ),
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Receiver port."),
    timeout: float = typer.Option(
        3.0,
        "--timeout",
        "-t",
        help="Seconds to browse for mDNS peers.",
    ),
) -> None:
    """Discover a receiver and send a file."""
    file = file.expanduser().resolve()

    async def _run() -> None:
        if host:
            uri = f"ws://{host}:{port}"
            console.print(f"sending [bold]{file.name}[/] → [cyan]{uri}[/]")
        else:
            console.print(f"[dim]looking for receivers ({timeout:.0f}s)…[/]")
            peers = await discover(timeout=timeout)
            if not peers:
                console.print(
                    "[red]no air-ferry receivers found on the LAN.[/]\n"
                    "start one with [bold]air-ferry listen[/], or pass [bold]--host[/]."
                )
                raise typer.Exit(code=1)

            if len(peers) == 1:
                peer = peers[0]
            else:
                table = Table(title="Receivers", show_header=True, header_style="bold")
                table.add_column("#", style="dim", width=3)
                table.add_column("Name")
                table.add_column("Address")
                for i, peer in enumerate(peers, start=1):
                    table.add_row(str(i), peer.name, f"{peer.host}:{peer.port}")
                console.print(table)
                choice = Prompt.ask(
                    "select receiver",
                    choices=[str(i) for i in range(1, len(peers) + 1)],
                    default="1",
                )
                peer = peers[int(choice) - 1]

            uri = peer.endpoint
            console.print(
                f"sending [bold]{file.name}[/] "
                f"([dim]{format_bytes(file.stat().st_size)}[/]) → "
                f"[cyan]{peer.name}[/] ({peer.host}:{peer.port})"
            )

        with _progress() as progress:
            task_id = progress.add_task(
                f"sending {file.name}", total=file.stat().st_size
            )

            async def on_progress(sent: int, total: int, _name: str) -> None:
                progress.update(task_id, completed=sent)

            result = await send_file(file, uri, on_progress=on_progress)

        console.print(
            f"[green]✓[/] delivered as [bold]{result.get('filename', file.name)}[/] "
            f"[dim]sha256={result.get('sha256', '')[:12]}…[/]"
        )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/]")
        raise typer.Exit(code=130) from None
    except Exception as exc:
        console.print(f"[red]failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command("version")
def version() -> None:
    """Print the package version."""
    console.print(__version__)
