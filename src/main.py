"""
main.py - Main Entry Point and Application Loop
=================================================

HOW MICROPYTHON BOOTS:
----------------------
When the Pico powers on (or reboots), MicroPython runs two files in order:

1. boot.py  - Hardware initialization (pin modes, clock speed, etc.)
              This file is optional and we don't use it in this project.
              It's for low-level setup that must happen before anything else.

2. main.py  - The main application code (THIS FILE).
              This is where your program lives. MicroPython runs this file
              from top to bottom, and when it finishes, the Pico sits idle
              (or enters the REPL if connected via USB serial).

Since we want the Pico to run forever (it's an IoT device, not a script),
our main.py sets everything up and then enters an infinite loop.

THE BOOT SEQUENCE:
------------------
1. Load configuration from flash (WiFi credentials, server URL, token)
2. Initialize the LED controller (for visual status feedback)
3. Connect to WiFi (try each stored SSID)
4. Connect to the WebSocket server
5. Authenticate with the server (send device token)
6. Enter the main loop

THE MAIN LOOP:
--------------
The main loop runs forever and does these things each iteration:
1. Feed the watchdog timer (so it doesn't reboot us)
2. Update the LED pattern (non-blocking blink control)
3. Poll the WebSocket for incoming messages
4. If a message arrived, dispatch it to the appropriate handler
5. Poll TCP relay sessions for data to forward
6. Send heartbeat if enough time has passed
7. Check WiFi connection health
8. If WiFi or WebSocket dropped, attempt reconnection

Each iteration takes about 100-200ms (mostly waiting for WebSocket recv()
timeout). This means we can respond to commands within about 200ms of
receiving them.

ABOUT THE INFINITE LOOP:
In regular Python/Django, an infinite loop would be a bug. On a microcontroller,
it's the standard pattern. The Pico has no OS, no task scheduler, no event loop
(unless you use uasyncio). Your code IS the only thing running. If main.py ends,
the Pico does nothing until it's rebooted.

ERROR HANDLING PHILOSOPHY:
We wrap nearly everything in try/except because the Pico MUST keep running.
A single unhandled exception would terminate main.py, and the Pico would go
silent until manually rebooted (or the watchdog catches it). We'd rather
log an error and try to recover than crash and go dark.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
# Standard MicroPython modules.
import time
import gc  # Garbage collector (for memory management on a tiny heap)

# Our firmware modules.
# Each of these is a .py file on the Pico's flash filesystem.
from config import Config
from wifi_manager import WiFiManager
from ws_client import WebSocketClient
from protocol import ProtocolHandler
from led_controller import LEDController, handle_identify
from wol import handle_wol
from network_scanner import handle_scan
from tcp_relay import (
    TCPRelay,
    handle_tcp_relay_open,
    handle_tcp_relay_data,
    handle_tcp_relay_close,
)
from ota_updater import handle_ota_update, handle_get_versions
from watchdog import WatchdogManager


# -------------------------------------------------------------------------
# Firmware Version
# -------------------------------------------------------------------------
# Increment this when you release a new version.
# The server can check this to know if an OTA update is needed.
FIRMWARE_VERSION = "0.2.1"


# -------------------------------------------------------------------------
# Boot Sequence
# -------------------------------------------------------------------------
def boot(reuse=None):
    """
    The boot sequence: set everything up before entering the main loop.

    Returns a dict of all initialized components, or None if boot failed
    critically (no config, no WiFi, etc.).

    Reuse fast-path:
      If `reuse` is a previous components dict and its WiFi is still
      connected, we skip Step 3 (WiFi connect, the slowest part of boot:
      5-15s) and Step 2 (LED re-init). Only the WebSocket-and-up layers
      get rebuilt. This is what main()'s reconnect path uses when only
      the WebSocket dropped, which is most reconnects -- the LAN didn't
      die, just the server connection blipped.

    WHY A FUNCTION?
    Keeping boot logic in a function (instead of top-level code) makes it:
    1. Easier to read and understand
    2. Possible to retry from main() if something fails
    3. Better for memory (local variables are freed after the function returns,
       unless we explicitly return them)
    """
    # Activate the in-RAM log ring buffer -- print() now tees to both
    # serial and a capped-size buffer so `wakemypc logs --debug` can
    # snapshot recent output even if it wasn't streaming live at the
    # time. Idempotent on partial reboots.
    try:
        from log_buffer import install as _install_log_buffer

        _install_log_buffer()
    except ImportError:
        pass

    # Skip the noisy banner on a partial reboot -- the user log already
    # shows the previous one, and a fresh one looks like a hard reset
    # (which it isn't).
    is_partial = bool(reuse and reuse.get("wifi") and reuse["wifi"].is_connected())
    if not is_partial:
        print("=" * 50)
        print("WakeMyPC Pico Firmware v" + FIRMWARE_VERSION)
        print("=" * 50)
    else:
        print("[boot] Partial reboot -- WiFi still up, only re-establishing WebSocket")

    # Run garbage collection before we start. This frees any memory left over
    # from the REPL or previous code that was loaded.
    gc.collect()
    print("[boot] Free memory:", gc.mem_free(), "bytes")

    # ------------------------------------------------------------------
    # Step 1: Load Configuration
    # ------------------------------------------------------------------
    # The config file (secrets.json) contains WiFi credentials, server URL,
    # and device authentication details. Without this, we can't do anything.
    print("\n[boot] Step 1: Loading configuration...")
    config = Config()
    has_config = config.load()

    if not has_config:
        print("[boot] WARNING: No configuration found!")
        print("[boot] The Pico needs to be provisioned with WiFi credentials")
        print("[boot] and a server URL. Use the pico_cli tool to set this up.")
        # We continue anyway -- the config has defaults, and maybe the user
        # will connect via USB serial to configure it.

    # Check for essential config values.
    server_url = config.get("server_url", "")
    ws_endpoint = config.get("ws_endpoint", "")
    wifi_networks = config.get("wifi_networks", [])
    device_token = config.get("device_token", "")
    device_id = config.get("device_id", "")

    if not wifi_networks:
        print("[boot] WARNING: No WiFi networks configured!")
    if not server_url:
        print("[boot] WARNING: No server URL configured!")

    # ------------------------------------------------------------------
    # Step 2: Initialize LED Controller
    # ------------------------------------------------------------------
    # On the reuse path we keep the existing LED instance so its state
    # (pattern, timers) doesn't reset; otherwise create a fresh one.
    if is_partial:
        led = reuse["led"]
        led.set_pattern("connecting")
    else:
        print("\n[boot] Step 2: Initializing LED...")
        led = LEDController()
        led.set_pattern("connecting")  # Slow blink = "I'm starting up"

    # ------------------------------------------------------------------
    # Step 3: Connect to WiFi (skipped if reused WiFi is still up)
    # ------------------------------------------------------------------
    # This is usually the slowest part of boot (5-15 seconds). On the
    # reuse path we skip it entirely -- if the previous main loop saw
    # only the WebSocket fail, the WiFi association is still valid.
    if is_partial:
        wifi = reuse["wifi"]
        wifi_info = wifi.get_info()
        print("[boot] Reusing WiFi:", wifi_info.get("ssid"), "@", wifi_info.get("ip"))
    else:
        print("\n[boot] Step 3: Connecting to WiFi...")
        wifi = WiFiManager()

        if not wifi_networks:
            print("[boot] Skipping WiFi (no networks configured)")
            led.set_pattern("error")
            # Return what we have -- main() will handle the missing WiFi.
            return {
                "config": config,
                "led": led,
                "wifi": wifi,
                "ws": None,
                "proto": None,
            }

        # Try to connect. This tries each SSID in order with timeouts.
        wifi_connected = wifi.connect(wifi_networks)

        if not wifi_connected:
            print("[boot] WiFi connection failed!")
            led.set_pattern("error")  # Fast blink = error
            return {
                "config": config,
                "led": led,
                "wifi": wifi,
                "ws": None,
                "proto": None,
            }

        # Print WiFi info for debugging.
        wifi_info = wifi.get_info()
        print("[boot] WiFi connected!")
        print("[boot]   SSID:", wifi_info["ssid"])
        print("[boot]   IP:  ", wifi_info["ip"])
        print("[boot]   RSSI:", wifi_info["rssi"], "dBm")

    # ------------------------------------------------------------------
    # Step 4: Connect to WebSocket Server
    # ------------------------------------------------------------------
    print("\n[boot] Step 4: Connecting to WebSocket server...")

    if not ws_endpoint:
        print("[boot] Skipping WebSocket (no endpoint URL configured)")
        led.set_pattern("error")
        return {
            "config": config,
            "led": led,
            "wifi": wifi,
            "ws": None,
            "proto": None,
        }

    ws = WebSocketClient(ws_endpoint)
    ws_connected = ws.connect()

    if not ws_connected:
        print("[boot] WebSocket connection failed!")
        led.set_pattern("error")
        return {
            "config": config,
            "led": led,
            "wifi": wifi,
            "ws": ws,
            "proto": None,
        }

    print("[boot] WebSocket connected!")

    # ------------------------------------------------------------------
    # Step 5: Set Up Protocol Handler & Register Message Handlers
    # ------------------------------------------------------------------
    print("\n[boot] Step 5: Setting up protocol handlers...")
    proto = ProtocolHandler(ws, config)

    # Register handlers for each message type the server can send.
    # This maps message type strings to handler functions.
    #
    # PATTERN: This is like Django's urlpatterns, but for WebSocket messages:
    #   urlpatterns = [path("api/wol/", wol_view)]         # Django
    #   proto.register("wol", handle_wol)                  # Pico firmware
    proto.register("wol", handle_wol)
    proto.register("scan", handle_scan)
    proto.register("identify", handle_identify)
    proto.register("tcp_relay_open", handle_tcp_relay_open)
    proto.register("tcp_relay_data", handle_tcp_relay_data)
    proto.register("tcp_relay_close", handle_tcp_relay_close)
    proto.register("ota_update", handle_ota_update)
    proto.register("get_versions", handle_get_versions)

    # Store LED and TCP relay references on proto so handlers can access them.
    proto._led = led
    proto._tcp_relay = TCPRelay(ws)

    # ------------------------------------------------------------------
    # Step 6: Authenticate with Server
    # ------------------------------------------------------------------
    print("\n[boot] Step 6: Authenticating with server...")

    # Send our device token and ID so the server knows who we are.
    # This is the first message we send after connecting.
    import machine

    try:
        hardware_id = "".join("{:02x}".format(b) for b in machine.unique_id())
    except Exception:
        hardware_id = "unknown"

    auth_message = {
        "type": "auth",
        "device_id": device_id,
        "token": device_token,
        "hardware_id": hardware_id,
        "firmware_version": FIRMWARE_VERSION,
        "ip": wifi_info["ip"] if wifi_info else "unknown",
    }
    ws.send(auth_message)
    print("[boot] Auth message sent")

    # Switch LED to solid on (connected and ready!).
    led.set_pattern("connected")

    print("\n[boot] Boot complete! Entering main loop...")
    print("=" * 50)

    return {
        "config": config,
        "led": led,
        "wifi": wifi,
        "ws": ws,
        "proto": proto,
    }


# -------------------------------------------------------------------------
# Main Loop
# -------------------------------------------------------------------------
def main():
    """
    The main application loop.

    This function runs forever (until the Pico is powered off or rebooted).
    It's structured as an outer retry loop (for reconnection) containing
    an inner message-processing loop.

    STRUCTURE:
    while True:                          # Outer loop: retry on failure
        components = boot()              # Set up everything
        watchdog.start()                 # Start crash protection
        while connected:                 # Inner loop: process messages
            watchdog.feed()              # "I'm still alive"
            led.update()                 # Update blink pattern
            msg = ws.recv()              # Check for server messages
            if msg: proto.dispatch(msg)  # Handle the message
            relay.poll_all()             # Forward TCP relay data
            heartbeat()                  # Send periodic heartbeat
            check_wifi()                 # Make sure WiFi is still up
        # If we exit the inner loop, something broke. Wait and retry.
        time.sleep(backoff_delay)
    """
    # Initialize the watchdog.
    # We create it outside the loop so it persists across reconnections.
    # (Remember: once started, the hardware WDT can't be stopped!)
    watchdog = WatchdogManager(timeout_ms=8000)

    # Reconnection backoff tracking.
    reconnect_delay = 1
    max_reconnect_delay = 60
    # Cooldown when the server has rejected our token (auth_fail). Way
    # longer than max_reconnect_delay because (a) the token is provably
    # bad and won't work until the user reprovisions, (b) the server
    # rate-limits at 5 attempts/minute so anything faster gets us 4029'd.
    # 5 minutes is short enough that a USB reprovision is picked up
    # quickly without the user having to manually power-cycle.
    auth_failed_cooldown = 300

    # Track uptime and reconnections for health reporting.
    # boot_ticks records the time.ticks_ms() value at startup so we can
    # calculate how long the Pico has been running (uptime).
    # reconnect_count tracks how many times we've had to reconnect -- a high
    # number suggests WiFi instability or server issues.
    boot_ticks = time.ticks_ms()
    reconnect_count = 0

    # `last_components` is the previous successful boot's component dict;
    # we hand it to boot() on retries so it can skip the WiFi step when
    # the LAN association is still healthy. None on first boot.
    last_components = None

    # ---- Outer Loop: Retry on Failure ----
    while True:
        try:
            # Feed the watchdog before booting (boot can take a while).
            # If the watchdog was started in a previous iteration, we need
            # to keep feeding it during boot.
            if watchdog._started:
                watchdog.feed()

            # Run the boot sequence. Pass last_components so boot() can
            # take the fast path (reuse WiFi + LED) when the previous
            # disconnect was WebSocket-only.
            components = boot(reuse=last_components)
            last_components = components
            config = components["config"]
            led = components["led"]
            wifi = components["wifi"]
            ws = components["ws"]
            proto = components["proto"]

            # Start the watchdog timer (if not already started).
            # We start it AFTER boot because boot involves slow operations
            # (WiFi connection, WebSocket handshake) that might exceed the
            # watchdog timeout.
            if not watchdog._started:
                watchdog.start()

            # If boot failed to establish a WebSocket connection, wait and retry.
            if not ws or not proto:
                print("[main] Boot incomplete, retrying in", reconnect_delay, "seconds")
                led.set_pattern("error")

                # Wait, but keep feeding the watchdog during the wait!
                wait_with_watchdog(watchdog, led, reconnect_delay)

                # Exponential backoff.
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                continue

            # Boot succeeded -- reset the backoff.
            reconnect_delay = 1

            # Track heartbeat timing.
            heartbeat_interval = config.get("heartbeat_interval", 30)
            last_heartbeat = time.ticks_ms()
            # Periodic device-status scan timing. The Pico walks
            # proto.assigned_devices, TCP-probes each, and sends a
            # `device_status` message so the dashboard's online/offline
            # dot reflects reality. Default 60s -- low enough to feel
            # responsive, high enough to not melt a Pico that's
            # monitoring 10+ devices.
            device_scan_interval = config.get("device_scan_interval", 60)
            scan_state = {"last": time.ticks_ms(), "force": False}

            def _force_scan_on_assignment(_devices):
                scan_state["force"] = True

            proto.set_on_device_assignment(_force_scan_on_assignment)

            # Lazy-import the scanner so boot stays light if no devices
            # are assigned yet (it's never imported in that case).
            _scanner = None

            # ---- Inner Loop: Process Messages ----
            while True:
                # 1. Feed the watchdog.
                #    This is the FIRST thing in the loop -- if anything below
                #    takes too long or crashes, the watchdog resets us.
                watchdog.feed()

                # 2. Update LED pattern (non-blocking).
                led.update()

                # 3. Poll WebSocket for incoming messages.
                #    recv() is non-blocking (100ms timeout). It returns None
                #    if no message is available.
                msg = ws.recv()

                if msg is not None:
                    # Dispatch to the appropriate handler. We deliberately
                    # do NOT blink the LED here -- a heartbeat-driven blink
                    # every 30s on the same pin/pattern as the "error"
                    # flash looked like the Pico was misbehaving. The LED
                    # is reserved for actual state changes (connecting,
                    # connected, error) and the "identify" command.
                    proto.dispatch(msg)

                # 4. Poll TCP relay sessions.
                #    Check if any target devices have sent data back through
                #    our relay connections, and forward it to the server.
                if hasattr(proto, "_tcp_relay"):
                    relay_data = proto._tcp_relay.poll_all()
                    for session_id, b64_data in relay_data:
                        # Send relay data back to the server.
                        ws.send(
                            {
                                "type": "tcp_relay_data",
                                "session_id": session_id,
                                "data": b64_data,
                            }
                        )

                # 5. Send heartbeat if it's time.
                #    The heartbeat now includes health data so the server can
                #    display a health dashboard (RAM usage, WiFi signal, uptime,
                #    reconnection count). This data is stored in Redis cache on
                #    the server and displayed on the transmitter detail page.
                now = time.ticks_ms()
                if time.ticks_diff(now, last_heartbeat) >= heartbeat_interval * 1000:
                    wifi_info = wifi.get_info() if wifi.is_connected() else None

                    # Collect garbage before measuring memory so we get an
                    # accurate picture of actual memory usage (not just garbage
                    # that hasn't been collected yet).
                    gc.collect()

                    # Build health metrics to include with the heartbeat.
                    # These let the server dashboard show the Pico's internal
                    # state: how much RAM is free, WiFi signal quality, how
                    # long it's been running, and how stable the connection is.
                    health = {
                        "free_ram": gc.mem_free(),
                        "total_ram": gc.mem_free() + gc.mem_alloc(),
                        "wifi_rssi": wifi.get_rssi() if hasattr(wifi, 'get_rssi') else None,
                        "uptime_seconds": time.ticks_diff(time.ticks_ms(), boot_ticks) // 1000,
                        "reconnect_count": reconnect_count,
                    }

                    print(
                        "[main] heartbeat sent | uptime=",
                        health["uptime_seconds"],
                        "s | free_ram=",
                        health["free_ram"],
                        "B | rssi=",
                        health["wifi_rssi"],
                        "| reconnects=",
                        health["reconnect_count"],
                    )
                    proto.send_heartbeat(wifi_info, health)
                    last_heartbeat = now

                # 5b. Periodic device-status scan -- original all-at-once
                #     behaviour (stagger fix temporarily removed for debug).
                due = (
                    time.ticks_diff(now, scan_state["last"])
                    >= device_scan_interval * 1000
                )
                if proto.assigned_devices and (due or scan_state["force"]):
                    n = len(proto.assigned_devices)
                    scan_start = time.ticks_ms()
                    print(
                        "[main] scan START |",
                        n,
                        "device(s) | forced=",
                        scan_state["force"],
                    )
                    if _scanner is None:
                        from network_scanner import NetworkScanner
                        _scanner = NetworkScanner(timeout=2)
                    try:
                        scan_results = _scanner.check_devices(proto.assigned_devices)
                        statuses = []
                        for r in scan_results:
                            if not r.get("public_id"):
                                continue
                            statuses.append({
                                "public_id": r["public_id"],
                                "online": r.get("online", False),
                                "ip": r.get("ip"),
                            })
                        elapsed = time.ticks_diff(time.ticks_ms(), scan_start)
                        online_count = sum(1 for s in statuses if s["online"])
                        print(
                            "[main] scan END |",
                            elapsed,
                            "ms |",
                            online_count,
                            "/",
                            n,
                            "online",
                        )
                        if statuses:
                            ws.send({"type": "device_status", "devices": statuses})
                    except Exception as scan_exc:
                        print("[main] device scan failed:", scan_exc)
                    scan_state["last"] = now
                    scan_state["force"] = False

                # 6. Check WebSocket health (ping/pong).
                if not ws.check_heartbeat():
                    print("[main] WebSocket heartbeat failed!")
                    break  # Exit inner loop to reconnect.

                # 7. Check WiFi connection.
                if not wifi.is_connected():
                    print("[main] WiFi disconnected!")
                    break  # Exit inner loop to reconnect.

                # 8. Check if WebSocket is still connected.
                if not ws.is_connected():
                    print("[main] WebSocket disconnected!")
                    break  # Exit inner loop to reconnect.

                # 9. Check if the server rejected our token. If so, exit
                # the inner loop -- main.py's outer loop will pick up the
                # auth_failed flag and switch to the long cooldown.
                if proto.auth_failed:
                    print(
                        "[main] auth_fail flagged by protocol handler --",
                        "token rejected. Reason:", proto.auth_fail_reason,
                    )
                    break

            # If we exit the inner loop, something disconnected.
            reconnect_count += 1
            print("[main] Connection lost, will reconnect... (reconnect #" + str(reconnect_count) + ")")
            # Distinct LED pattern when the disconnect is auth-related so
            # a user looking at the device can tell "wrong token" from
            # "WiFi died" without plugging in a serial cable.
            if proto and proto.auth_failed:
                led.set_pattern("auth_failed")
            else:
                led.set_pattern("error")

            # Clean up. Important: do NOT disconnect WiFi here. WiFi
            # being still associated lets boot() take its fast path on
            # the next iteration (skips the 5-15s WiFi handshake). If
            # WiFi is *actually* the cause of the disconnect, the next
            # boot()'s WiFi state check will detect that and reconnect
            # properly.
            try:
                ws.close()
            except Exception:
                pass
            if hasattr(proto, "_tcp_relay"):
                proto._tcp_relay.close_all()

        except MemoryError:
            # OUT OF MEMORY! This is serious on a microcontroller.
            # Run garbage collection immediately and hope for the best.
            print("[main] MEMORY ERROR! Running gc.collect()...")
            gc.collect()
            print("[main] Free memory after gc:", gc.mem_free(), "bytes")

        except Exception as e:
            # Catch-all for any other unexpected errors.
            # Log it and continue to the retry loop.
            print("[main] Unexpected error:", e)

        # Wait before reconnecting. Auth failures get a fixed long cooldown
        # (token won't work until reprovisioned, server rate-limits at 5
        # attempts/minute); other failures get the usual exponential
        # backoff capped at max_reconnect_delay.
        if proto and proto.auth_failed:
            print(
                "[main] Auth failed -- cooldown for",
                auth_failed_cooldown,
                "seconds. Reprovision via 'pico-cli register --rotate' or --token to recover.",
            )
            wait_with_watchdog(watchdog, led, auth_failed_cooldown)
            # Don't escalate the regular backoff while in auth-failed
            # mode; the cooldown is already long.
        else:
            print("[main] Reconnecting in", reconnect_delay, "seconds...")
            wait_with_watchdog(watchdog, led, reconnect_delay)
            # Increase backoff for next failure.
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)


def wait_with_watchdog(watchdog, led, seconds):
    """
    Wait for the specified number of seconds while keeping the watchdog
    fed and the LED updated.

    We can't just do time.sleep(seconds) because:
    1. The watchdog would expire and reboot us (8-second timeout)
    2. The LED pattern would freeze (no update() calls)

    Instead, we sleep in small 100ms chunks, feeding the watchdog and
    updating the LED between each chunk.

    Parameters:
        watchdog: The WatchdogManager instance
        led:      The LEDController instance (for continued blink patterns)
        seconds:  How many seconds to wait (float or int)
    """
    iterations = int(seconds * 10)  # 10 iterations per second (100ms each)

    for _ in range(iterations):
        watchdog.feed()
        led.update()
        time.sleep(0.1)


# -------------------------------------------------------------------------
# Entry Point
# -------------------------------------------------------------------------
# When MicroPython runs main.py, it executes top-level code.
# We call main() which enters the infinite loop.
#
# The try/except here is our absolute last line of defense. If main()
# itself crashes (which it shouldn't, because it has its own try/except),
# we print the error. The watchdog will reboot us shortly after.
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL ERROR in main():", e)
        # The watchdog (if started) will reboot us.
        # If the watchdog hasn't started yet, we just sit here. :(
        # In production, you might want to add a machine.reset() here
        # as a fallback, but be careful about boot loops.
        time.sleep(10)
        import machine

        machine.reset()
