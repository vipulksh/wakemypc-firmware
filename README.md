# wakemypc-firmware

The MicroPython firmware that runs on a Raspberry Pi Pico W / W 2 paired with [wakemypc.com](https://wakemypc.com).

This repo is **public on purpose** — the firmware sits on your home network and handles your machines' Wake-on-LAN traffic and (optionally) tunnels SSH connections from the server. You should be able to read every line of code on the device before you trust it.

If anything in here looks weird to you, please open an issue. Especially in [`src/tcp_relay.py`](src/tcp_relay.py) — see the dedicated section below.

## What this firmware does

A wakemypc-flashed Pico, on your home network, does four things:

1. **Wake-on-LAN.** When the dashboard tells it to wake a PC, it sends the WoL "magic packet" on the LAN. Source: [`src/wol.py`](src/wol.py).
2. **Device monitoring.** It periodically TCP-pings the IPs you've assigned and reports up/down state to the server. Source: [`src/network_scanner.py`](src/network_scanner.py).
3. **TCP relay (opt-in).** If you've configured an SSH credential on the server, the server can ask the Pico to open a TCP socket to a target IP/port on your LAN and forward bytes through the WebSocket. The server runs SSH on its end; the Pico is just a byte pipe. **Read the section below before you enable this.** Source: [`src/tcp_relay.py`](src/tcp_relay.py).
4. **Heartbeat + status.** Every 30s it reports memory, WiFi RSSI, uptime, and reconnect count. The dashboard's Health card displays this. Source: [`src/protocol.py`](src/protocol.py) (`send_heartbeat`).

It does **not** do anything else. There's no analytics, no telemetry beyond the heartbeat, no third-party network calls. The Pico talks to exactly one host: your configured WebSocket endpoint.

## What `tcp_relay.py` does, and why it exists

The dashboard's "Shutdown" feature works by SSH-ing into your PC and running an OS-specific shutdown command. But the server (running at wakemypc.com) can't reach your PC directly — your PC is behind your home router on a private subnet. So the Pico, which **is** on your LAN, acts as a TCP relay.

**The flow:**

1. You provision an SSH credential for a device through the dashboard. The private key is stored encrypted (Fernet) on the server. **The key never leaves the server.**
2. You click "Shutdown" on the device. The server sends a `tcp_relay_open` message to the Pico over WebSocket: "open a TCP connection to 192.168.1.50:22."
3. The Pico opens a raw TCP socket to that IP/port. ([`src/tcp_relay.py`](src/tcp_relay.py) `handle_tcp_relay_open`)
4. The server runs the SSH protocol (paramiko library) over the WebSocket as if it were the socket. Each direction of bytes is base64-encoded and shipped through `tcp_relay_data` messages.
5. The Pico's job is to copy bytes between the WebSocket and the TCP socket. **It does not parse, modify, or store any of them.** The bytes are encrypted SSH traffic — the Pico can't read them either.
6. SSH command runs on the PC, returns a result, the relay session closes.

**Why this is auditable:**

- The Pico has no way to read the SSH protocol. The Pico has no SSH library. It has `socket.recv` / `socket.send` and base64.
- The SSH key never reaches the Pico. Compromising the Pico does not compromise your PC's SSH access.
- The relay session is **opt-in per device**: if you don't configure an SSH credential, no `tcp_relay_open` will ever arrive.
- You can verify all of the above by reading [`src/tcp_relay.py`](src/tcp_relay.py) (~300 LOC including comments) and grepping for `import socket` to confirm there's no other use of raw sockets.

**Why the Pico is in the loop at all:**

It's the only thing with line-of-sight to your PCs. Without it, you'd need a port-forward on your router (which is worse for security) or a VPN (which is more complicated to set up). The relay is the only sane way to make "shutdown" work without you punching holes in your firewall.

If you don't trust this design or don't want SSH-based shutdown: **don't configure an SSH credential.** The wake-on-LAN side works fine on its own, and `tcp_relay_open` will never be sent.

## Flashing a Pico

You need [`wakemypc-cli`](https://github.com/<owner>/wakemypc-cli) to install MicroPython and copy this firmware to the Pico. The CLI handles BOOTSEL, file transfer, secrets provisioning, and registration.

```bash
pip install git+https://github.com/<owner>/wakemypc-cli.git
wakemypc detect                  # plug the Pico in BOOTSEL mode
wakemypc flash --uf2 <path>      # install MicroPython
wakemypc upload --firmware-dir ./src/   # copy this firmware
wakemypc provision --server-url wss://wakemypc.com --add-new-wifi --wifi-ssid <SSID> --wifi-pass <PASS>
wakemypc register --api-url https://wakemypc.com --username <you> --password <pw>
```

Or grab a release artifact and copy the .py files manually with [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
for f in src/*.py; do mpremote connect /dev/ttyACM0 cp "$f" ":$(basename $f)"; done
mpremote connect /dev/ttyACM0 reset
```

## OTA updates

When the server has a newer firmware version available and the Pico's user is on a tier that has access to it, the server sends a manifest with download URLs and sha256 checksums. The Pico:

1. Downloads each file to a `/staging/` directory.
2. Verifies the sha256 of every file.
3. Atomically renames `/staging/<f>` to `/<f>` for each file.
4. Runs any post-install hooks (e.g. `secrets.json` schema migrations).
5. `machine.reset()`.

If anything fails before the rename phase, nothing changes. If the device reboots mid-rename (unusual), `/backup/` and `/staging/` both still have intact copies.

You can verify a release's checksums against your installed files:

```bash
mpremote connect /dev/ttyACM0 cat main.py | sha256sum
# Compare with the value in the release's MANIFEST.json
```

## Releases

Tag the repo (`v1.0.1`) and push. The [release.yml](.github/workflows/release.yml) workflow builds a `MANIFEST.json` listing every `src/*.py` with its sha256 and creates a GitHub Release with the manifest + files attached. The wakemypc.com server pulls from this URL when offering OTA upgrades.

## License

**Source-available, non-commercial.** [PolyForm Noncommercial 1.0.0](LICENSE).

What that means in practice:

- **You can** read every line, run it on your own Picos for personal/hobby/research use, modify it for yourself, share patches back here, and audit it for security.
- **You cannot** sell it, ship it inside a paid product, or run a commercial competing service on it.
- Charities, schools, and government use also count as permitted (non-commercial) per the license text.

Patches and audits are warmly welcomed -- the whole point of this repo being public is that you can verify what's running on your hardware.

## Reporting a security concern

For anything that looks like a vulnerability rather than a normal bug, please email security@wakemypc.com (or open a private security advisory on GitHub) instead of a public issue. Public issues for everything else.
