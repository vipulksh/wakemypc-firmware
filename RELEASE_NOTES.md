## v0.3.4 — hard-reset the CYW43 chip before post-OTA reboot

**The actual fix for v0.3.2's stuck-after-OTA loop.** v0.3.3 tried to
solve the warm-reboot WiFi issue by toggling `wlan.active(False)` ->
`active(True)` at the start of `connect()`. That was not deep enough --
on the rp2 MicroPython build, `active(False)` doesn't fully drop power
to the CYW43 chip. The chip's state machine survived, the warm-boot
WiFi failure persisted, and the watchdog still fired ~8s into the
retry boot.

v0.3.4 yanks the chip's power-enable pin (GPIO 23, wired to
`WL_REG_ON` on the Pico W) low for 500ms right before
`machine.reset()`. This is a true hardware reset of the WiFi chip --
the same effect as a USB power cycle, just for the radio. When the
new firmware boots, the chip is freshly powered and re-initializes
clean, so `wlan.connect()` succeeds in the normal sub-second window.

**Where the fix lives:** [ota_updater.handle_ota_update](src/ota_updater.py),
right before the `machine.reset()` call. Only fires on the post-OTA
reboot path -- normal cold boots are unaffected, since they don't
have the warm-chip problem to begin with.

**Apply path:**

If you're already on a working v0.3.1 or v0.3.2, OTA to v0.3.4 should
work end-to-end. The OTA itself is the same as before; the new code
just runs in the final two lines before reset.

If your Pico is *currently* stuck in the post-v0.3.2 reboot loop,
USB-flash to recover -- OTA can't reach a Pico that never associates
with WiFi.

```
wakemypc upload --firmware-dir ./pico_firmware/src/
```

```
docker compose -f docker-compose.local.yml exec django \
  python manage.py import_firmware_manifest 0.3.4 --mark-latest
```
