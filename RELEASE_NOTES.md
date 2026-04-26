## v0.3.2 — OTA actually completes now

**The fix.** v0.3.1 made OTA *attempt* to work (redirect handling), but
the rp2 8s hardware watchdog could fire mid-update during long synchronous
TLS downloads. Symptom: the Pico would reboot somewhere between
`log_buffer.py` and `main.py`, leaving the firmware in a half-installed
state until rollback kicked in. v0.3.2 plumbs `watchdog.feed()` through
the whole OTA pipeline so the WDT keeps getting fed during DNS, TCP
connect, TLS handshake, request send, and every single recv() chunk.

**What changed in OTA:**
- `http_download`, `fetch_manifest`, and `OTAUpdater` accept an optional
  `feed_watchdog` callback. `main.py` stashes `watchdog.feed` on
  `proto._feed_watchdog` after each successful boot, and the OTA
  handler reads it from there.
- The callback fires before every potentially-slow socket op:
  resolving DNS, opening the TCP connection, the TLS handshake, sending
  the GET, receiving the response headers, and each 1KB body chunk.
- Socket timeout tightened from 30s -> 15s. A stuck connection now
  fails faster than the WDT.
- New phase prints (`resolving DNS`, `connecting TCP`, `TLS handshake`)
  so the next stall narrows the suspect window before reboot rather
  than vanishing into one silent recv().

**Reset cause logging at boot.** `machine.reset_cause()` is now printed
on every boot. WDT-induced resets get a banner so they're impossible to
miss in `wakemypc logs --catch-up`:

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! Last reset: WATCHDOG (timeout)
!! Something blocked the main loop > 8s.
!! Check the last printed phase before the reset.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

**Smaller flash footprint.** The release workflow now minifies every
`src/*.py` through `python-minifier` (drops comments, docstrings, and
whitespace; renames locals; preserves global names so cross-module
imports keep working). The repo source stays human-readable; the bytes
that hit flash are the lean version. Expect ~30-50% smaller files.

**Apply path:**

OTA from v0.3.1 should work end-to-end this time. If you're on v0.3.0
or older, USB-flash first -- v0.3.0 had a broken downloader and can't
fetch its own update.

```
docker compose -f docker-compose.local.yml exec django \
  python manage.py import_firmware_manifest 0.3.2 --mark-latest
```

Or USB:

```
wakemypc upload --firmware-dir ./pico_firmware/src/
```
