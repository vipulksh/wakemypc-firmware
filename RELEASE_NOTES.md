## v0.2.1 — Debug build

A diagnostic-focused release built specifically to trace what happens
when a Pico is asked to monitor several devices at once.

**Reverted (intentionally):**
- The round-robin scan stagger from v0.2.0 is **temporarily reverted**
  in this build. The all-at-once scan is back so the overload bug can
  be reproduced and observed live in `wakemypc logs`.

**New debug instrumentation:**
- `config.py` prints the loaded config on boot (device_id, server_url,
  ws_endpoint, configured SSIDs). Secrets are masked: device_token is
  printed as `***masked***`, WiFi passwords are never logged.
- `main.py` heartbeat tick prints uptime / free RAM / WiFi RSSI /
  reconnect count.
- `main.py` scan loop prints `[main] scan START | N device(s) | forced=…`
  and `[main] scan END | <ms> ms | <online>/<total> online` so a slow
  scan is visible as a long gap between START and END.
- `network_scanner.check_devices` prints per-device timing
  (`[scanner] <name> -> ONLINE | port= 80 | rt= 7 ms | took= 12 ms`).
- `ws_client.send` prints message type + size on every outbound frame.

**OTA:**
- The Pico now fetches `MANIFEST.json` directly from the GitHub
  Release referenced by `manifest_url` in the `ota_update` message
  (instead of the server embedding the file list inline). Single
  source of truth across server + Pico.
- Post-install hook support: a manifest entry with
  `"post_install": true` runs after the file swap; `"delete_after": true`
  removes the hook from flash. For `secrets.json` migrations on
  breaking releases.

**CLI:**
- `wakemypc logs` filters high-frequency lines by default (heartbeat
  metrics, per-probe timing, per-message dispatch). Pass `--debug` for
  the full firehose.
- `wakemypc upload --firmware-dir <path>` auto-falls-through to a
  `src/` subdirectory so passing the parent dir works without knowing
  the internal layout.
