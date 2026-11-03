# air-ferry

Share files between your PC (Linux/Windows) and phone. Web UI, system tray, WebSockets, Local network by default, optional cloudflare tunnel for when the router blocks device to device traffic.

![air-ferry web UI](docs/screenshot.png)

Stack: Python 3.10+, FastAPI, uvicorn, websockets, zeroconf, rich, typer, pystray

## Install

**Linux** (Python 3.10+). Tray deps once, then install and run:

```bash
# Fedora
sudo dnf install python3-gobject libappindicator-gtk3 wl-clipboard

# Debian / Ubuntu
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1 wl-clipboard

# Arch
sudo pacman -S python-gobject libappindicator-gtk3 wl-clipboard
```

```bash
python3 -m pip install --user "git+https://github.com/theluckytiger/air-ferry.git" && air-ferry
```

**Windows:** download the `.exe` from [Releases](https://github.com/theluckytiger/air-ferry/releases) and run it.

## Use

1. Approve a firewall prompt if asked (Windows UAC / Linux polkit) so phones can reach the PC
2. Browser UI opens; the app stays in the tray
3. Scan the QR on your phone (same Wi-Fi)
4. If the page does not load, click **Open a tunnel** (QR switches)
5. Drop or select a file on either side

Tray: open UI, copy link, open/close tunnel, quit. Quitting always stops the tunnel.

Optional terminal mode: `air-ferry-tui`

## License

MIT