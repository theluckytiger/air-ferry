# air-ferry

Direct LAN file transfer over WebSockets. Devices find each other with mDNS — no cloud relay, no manual IPs.

## Install

```bash
pip install -e .
```

## Usage

On the receiving machine:

```bash
air-ferry listen
```

On the sending machine:

```bash
air-ferry send ./report.pdf
```

## How it works

1. `listen` opens a WebSocket server and advertises `_air-ferry._tcp.local.` on the LAN.
2. `send` discovers receivers via mDNS, streams the file in 64KB chunks, and verifies a SHA-256 checksum at the end.

## Requirements

- Python 3.10+
- Same local network (multicast/mDNS must be allowed)

## License

MIT
