## v0.1.1 — Server protocol alignment

**Fixed:**
- **`firmware_update_available` handler.** When the server told a Pico
  on outdated firmware that an update was available, the Pico bounced
  back an "Unknown message type" error -- on every auth, every
  reconnect. Out-of-date Picos no longer spam the server.
- **Immediate device scan on assignment change.** When the dashboard
  added or reassigned a managed device, the Pico used to wait up to
  60 seconds (next periodic scan) before reporting the new device's
  status. Now the assignment message triggers an immediate scan, so
  newly-assigned devices light up in seconds.
