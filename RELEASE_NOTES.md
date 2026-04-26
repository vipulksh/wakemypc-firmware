## v0.3.0 — ICMP ping + scan stagger restored

**Performance:**
- **3-device overload fixed** (the bug deliberately left in v0.2.1 for
  reproduction). The round-robin scan stagger from v0.2.0 is back: one
  device probed per tick, ticks evenly spread across `device_scan_interval`.
  With 3 devices on a 60s interval that's one probe every ~20s -- and
  no single tick blocks for more than ~2s.
- **ICMP Echo pre-flight** for online detection. The scanner now sends
  one ICMP packet first; on reply (typical home/IoT/printer behaviour),
  it short-circuits the multi-port TCP walk entirely. Single round-trip,
  usually <10ms on a LAN.

**New module:**
- `ping.py` — raw-socket ICMP Echo (RFC 792) with proper Internet
  checksum (RFC 1071) and defensive IP-header parsing on reply. Build-
  compat guard: if `socket.SOCK_RAW` isn't exposed (stripped MicroPython
  builds), the scanner latches ICMP off after the first attempt and
  falls back to TCP probing. You'll see `[scanner] ICMP unavailable,
  TCP-only: <error>` once and never again.

**Logs:**
- Scanner now prints which path answered:
  `[scanner] <name> -> online (icmp, 7 ms )`
  `[scanner] <name> -> online (tcp:80)` / `(tcp-refused:80)`
  `[scanner] <name> -> offline`

**Reflash and re-import the manifest:**
```
wakemypc upload --firmware-dir ./pico_firmware/src/
docker compose -f docker-compose.local.yml exec django \
  python manage.py import_firmware_manifest 0.3.0 --mark-latest
```
