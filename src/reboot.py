"""
reboot.py - Centralized hard-reset helper for the Pico W.
==========================================================

WHY THIS EXISTS
---------------
The Pico W boots from cold cleanly, but `machine.reset()` only resets
the RP2040 -- the CYW43 WiFi chip lives on a separate power domain
and keeps its previous association state. Most of the time that's
fine, but if anything trips the chip into a bad state (e.g., a
host-side serial shenanigan, a botched OTA, a long disconnect), the
new firmware boots into a stuck WiFi.

This module is the *single* place every reset path in the firmware
goes through, so the chip dance happens consistently.

WHAT IT DOES
------------
1. Pulls GPIO 23 (the CYW43's WL_REG_ON line on the Pico W) low for
   500ms before calling `machine.reset()`. This is the closest thing
   to a real power-cycle of the radio that we can do programmatically;
   `wlan.active(False)` was tried in v0.3.3 and proved insufficient.
2. Wraps every step in try/except so a peripheral error can never
   keep the device alive when we want it dead.
3. Falls into a tight `while True: pass` after `machine.reset()` so
   the watchdog (if armed) finishes the job in the unlikely event
   the soft reset returns control to us.

CALLERS
-------
- ota_updater.handle_ota_update (post-OTA success)
- ota_updater._rollback (post-OTA rollback)
- protocol._handle_reboot (server-pushed reboot)
- main top-level except (fatal recovery)
- boot.factory_reset (config wipe)

Each caller passes a short `reason` string that's printed to the
serial log so an operator can tell which path triggered the reset.
"""

import time
import machine


def hard_reset(reason=""):
    """Power-cycle the CYW43 chip (best-effort), then machine.reset().

    Never returns. If `machine.reset()` somehow does, busy-waits so
    the watchdog -- which should always be armed by the time we get
    here -- finishes the job.
    """
    print("[reboot] hard_reset:", reason)
    try:
        # GPIO 23 = WL_REG_ON on the Pico W. Driving it low drops
        # power to the CYW43 radio. 500ms is enough for the chip's
        # bulk decoupling to bleed off; the cyw43 driver has been
        # fully torn down by this point on every documented call site.
        machine.Pin(23, machine.Pin.OUT).value(0)
        time.sleep_ms(500)
    except Exception as exc:
        # Never let a peripheral error keep us from rebooting.
        print("[reboot] CYW43 power-cycle skipped:", exc)
    machine.reset()
    while True:
        # Unreachable in practice; safety net for the watchdog.
        pass
