## v0.1.0 — Initial release

First public release of the WakeMyPC Pico W firmware.

**Features:**
- WebSocket auth with the wakemypc.com server.
- Wake-on-LAN: send magic packets to managed devices on the LAN.
- Device monitoring: TCP-port-probe assigned devices, report online/offline.
- WiFi configuration over the dashboard (credentials stay on the Pico).
- SSH-over-WebSocket TCP relay for remote shell access via the Pico.
- OTA scaffold: download firmware files, sha256-verify, atomic swap.
- Watchdog + LED status patterns (connecting, connected, error).

**Source-available** under PolyForm Noncommercial 1.0.0.
