## v0.3.5 — Real fix for post-OTA stuck-WiFi (it was the host CLI all along)

**The honest version of the v0.3.2 -> v0.3.5 chain.** The "Pico stuck
after OTA" bug we cut three releases against was not a CYW43 warm-boot
bug — it was `wakemypc logs` (the host-side CLI for streaming the
serial console) interrupting the firmware before `main.py` could run.
Specifically:

- `serial.Serial(...)` was opened with pyserial's defaults, which
  assert DTR and RTS on open and pulse the rp2 USB CDC stack hard
  enough to interrupt MicroPython during boot.
- The CLI's `_recover_log_buffer_after_reconnect()` path explicitly
  wrote a literal Ctrl+D (`\x04`) to serial after the post-OTA
  re-enumeration — that's MicroPython's REPL "soft reset and reload
  main.py" sequence, which deliberately *doesn't* reset the radio.
  The warm CYW43 then refused to associate.

Both are fixed in **wakemypc-cli v1.0.3**. This firmware release pairs
with that and adds defenses that help any older CLI client still in
the wild.

**Firmware changes in v0.3.5:**

- New module [`reboot.py`](src/reboot.py) with `hard_reset(reason="")`
  helper. Pulls GPIO 23 (CYW43 `WL_REG_ON`) low for 500ms, prints the
  reason to serial, then calls `machine.reset()`. Single chokepoint
  for the chip-power dance so it never gets forgotten on a new reset
  path.
- Every reset call site now goes through `hard_reset()`:
  - [ota_updater.handle_ota_update](src/ota_updater.py) (post-OTA success)
  - [protocol._handle_reboot](src/protocol.py) (server-pushed reboots — previously bare `machine.reset()`, the regression v0.3.4 didn't cover)
  - [main.py top-level except](src/main.py) (fatal recovery)
  - [boot.py factory reset](src/boot.py) (`bootsel` long-press wipe)
- Post-`HARD_RESET` settling delay added to the very top of `main()`:
  if the Pico boots and `machine.reset_cause() == HARD_RESET`, sleep
  1 second before printing or initialising the WDT. Lets any host
  serial reader (any version) re-enumerate cleanly before USB CDC
  writes start. Cold boots (PWRON_RESET) skip the wait.
- The v0.3.3 `wlan.active(False) -> active(True)` dance in
  `wifi_manager.connect()` and the v0.3.4 inline GPIO 23 toggle are
  retained as defense-in-depth. They cost ~1.5s on cold boot and 500ms
  on OTA reset, which we accept in exchange for tolerating any future
  reset path that forgets to use `hard_reset()`.

**Apply path:**

If you're already on a working v0.3.1 / v0.3.2 / v0.3.3 / v0.3.4, OTA
to v0.3.5 from the server. The OTA flow itself is unchanged.

If you've also been running `wakemypc logs` while OTAing, **upgrade
the CLI to v1.0.3 first** (`pip install --upgrade wakemypc`). v0.3.5
firmware tolerates an old broken CLI most of the time, but the proper
fix for a Pico in the field is upgrading both ends.

```
docker compose -f docker-compose.local.yml exec django \
  python manage.py import_firmware_manifest 0.3.5 --mark-latest
```
