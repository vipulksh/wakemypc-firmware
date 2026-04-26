## v0.3.3 — survive warm reboot (post-OTA WiFi recovery)

**Critical fix.** After v0.3.2 successfully OTA'd a Pico, the device
would reboot into v0.3.2 and then fail to reconnect to WiFi -- stuck
in a loop of `Timeout connecting to <SSID>` followed by an 8s WDT
reset, until the user power-cycled it manually. v0.3.3 fixes this.

**Root cause.** The CYW43 WiFi chip on the Pico W lives on a separate
power domain from the RP2040. `machine.reset()` (used by the OTA
pipeline to load new code) only resets the RP2040; the CYW43 keeps
its previous session state. The next boot's `wlan.connect()` finds
the chip still convinced it owns the old SSID, refuses to scan or
re-associate cleanly, and times out. Power-cycling fixes it because
that drops chip power; soft reset doesn't.

**Fix.** [wifi_manager.connect()](src/wifi_manager.py) now toggles
`active(False) -> sleep(0.5) -> active(True)` at the start of every
attempt. That drives the chip's power-management pin and forces a
clean re-init -- the equivalent of toggling WiFi off/on in an OS
settings panel. Cheap on cold boot (radio was already off), essential
on warm boot.

**Apply path.**

If you're already on a working v0.3.1 or v0.3.2, OTA to v0.3.3 should
work end-to-end now (the WDT-during-OTA fix landed in v0.3.2 already).

If your Pico is currently *stuck* in the post-v0.3.2 reboot loop, OTA
won't recover it -- the WS connection never establishes. USB-flash to
escape:

```
wakemypc upload --firmware-dir ./pico_firmware/src/
```

After USB recovery, future updates flow over OTA normally.

```
docker compose -f docker-compose.local.yml exec django \
  python manage.py import_firmware_manifest 0.3.3 --mark-latest
```
