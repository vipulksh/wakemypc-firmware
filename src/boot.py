"""
boot.py - Hardware initialization (runs BEFORE main.py)

=== MICROPYTHON BOOT SEQUENCE ===

When the Pico powers on, MicroPython executes files in this order:
  1. _boot.py  (internal, sets up filesystem - DO NOT MODIFY)
  2. boot.py   (THIS FILE - hardware init, WiFi, settings)
  3. main.py   (application code - WebSocket client, command handling)

boot.py runs ONCE at power-on. Its job is to prepare the hardware
so main.py can focus on application logic.

If boot.py crashes, main.py never runs. Keep it minimal and safe.

=== WHY SEPARATE boot.py FROM main.py? ===

Separation of concerns:
  - boot.py: ONE-TIME hardware setup (LED, WiFi, CPU frequency).
    Think of it like the BIOS on a PC -- it runs before the OS.
  - main.py: APPLICATION LOGIC that runs in a loop (WebSocket client,
    command processing, device monitoring).

This means you can update main.py (the application) without risking
changes to the boot sequence. If main.py crashes, the Pico still
booted correctly and you can debug over USB serial.

=== HOW TO CUSTOMIZE ===

Common customizations:
  - Change CPU frequency for power saving vs performance
  - Add additional WiFi networks for fallback
  - Disable factory reset check if BOOTSEL is used for something else
  - Add custom hardware init (external sensors, displays, etc.)
"""

import machine
import gc
import time

# === LED INDICATOR ===
# The onboard LED signals what stage of boot we're in.
# Pico W 2's LED is connected to the WiFi chip, not a regular GPIO pin.
# That's why we use "LED" as the pin name instead of a GPIO number.
# On a regular Pico (non-W), you would use machine.Pin(25, machine.Pin.OUT).
led = machine.Pin("LED", machine.Pin.OUT)
led.on()  # LED on = boot started

# === GARBAGE COLLECTION ===
# MicroPython has very limited RAM (~264KB on Pico W 2).
# gc.collect() forces Python to free memory used by objects that are
# no longer referenced. Running it early ensures maximum RAM is
# available for the application code in main.py.
# In CPython (desktop Python), the garbage collector runs automatically
# and you rarely think about it. In MicroPython, manual gc.collect()
# calls are essential to avoid MemoryError crashes.
gc.collect()

# === PRINT BOOT INFO ===
# This prints to the serial console (visible via USB if connected).
# Useful for debugging - you can see this output in pico-cli or Thonny.
# machine.unique_id() returns a bytes object with the Pico's hardware ID.
# This ID is burned into the chip at the factory and cannot be changed.
# We use it as the device's identity when registering with the server.
print("=" * 40)
print("Pico W 2 - WakeMyPC IoT Agent")
print(f"Unique ID: {machine.unique_id().hex()}")
print(f"CPU Freq:  {machine.freq() // 1_000_000} MHz")
print(f"Free RAM:  {gc.mem_free()} bytes")
print("=" * 40)

# === EARLY WIFI ATTEMPT ===
# Try to connect to WiFi during boot so main.py doesn't have to wait.
# If this fails, main.py will retry with full error handling.
#
# Why do it here instead of in main.py?
# - WiFi connection takes 3-10 seconds. Starting early means main.py
#   can begin sending data sooner.
# - If WiFi fails here, it's non-fatal. main.py has robust retry logic.
# - The try/except ensures boot.py NEVER crashes from a WiFi error.
try:
    from config import Config
    from wifi_manager import WiFiManager

    config = Config()
    wifi = WiFiManager(config)

    networks = config.get("wifi_networks", [])
    if networks:
        print(f"Attempting WiFi connection ({len(networks)} networks configured)...")
        # Quick attempt with short timeout -- don't block boot for too long.
        # main.py will retry with longer timeouts and exponential backoff.
        connected = wifi.connect(timeout=10)
        if connected:
            print(f"WiFi connected: {wifi.get_info()}")
            led.off()  # LED off = WiFi connected (main.py will set its own pattern)
        else:
            print("WiFi not connected (main.py will retry)")
    else:
        print("No WiFi networks configured - run pico-cli provision")
except Exception as e:
    # Catch ALL exceptions to prevent boot failure.
    # Common errors: config file missing, WiFi module not ready, etc.
    print(f"Boot WiFi error (non-fatal): {e}")

# === FACTORY RESET CHECK ===
# Hold the BOOTSEL button during power-on to trigger factory reset.
# This deletes secrets.json (which contains WiFi credentials and the
# device token) and reboots, allowing the Pico to be re-provisioned
# from scratch using pico-cli.
#
# BOOTSEL button is readable via rp2.bootsel_button() on Pico W 2.
# On older Pico models, this function may not exist (hence the try/except).
#
# Why is this useful?
# - If you move the Pico to a new network, you need new WiFi credentials.
# - If the device token is compromised, you can reset and re-register.
# - If the Pico is stuck in a boot loop, factory reset clears bad config.
# TODO Check what this does exactly, seems like it also stops the Pico from booting until you re-flash it? That would be a problem if we want to use BOOTSEL for something else in the future.
try:
    import rp2
    if rp2.bootsel_button():
        print("!!! BOOTSEL held - FACTORY RESET !!!")
        import os
        try:
            os.remove("secrets.json")
            print("secrets.json deleted")
        except OSError:
            print("No secrets.json to delete")
        try:
            os.remove("secrets_backup.json")
            print("secrets_backup.json deleted")
        except OSError:
            pass
        # Blink LED 3 times to visually confirm factory reset
        print("Rebooting in 3 seconds...")
        led.on()
        time.sleep(1)
        led.off()
        time.sleep(1)
        led.on()
        time.sleep(1)
        # machine.reset() is a hard reboot -- equivalent to unplugging and
        # re-plugging the Pico. After this, boot.py runs again from scratch.
        machine.reset()
except Exception as e:
    # rp2.bootsel_button() may not exist on all boards.
    # Non-fatal: if we can't check, just skip the factory reset feature.
    print(f"Factory reset check error (non-fatal): {e}")

# === FINAL CLEANUP ===
# Free any memory used by the WiFi/config imports before main.py starts.
gc.collect()
print(f"Boot complete. Free RAM: {gc.mem_free()} bytes")
print("Starting main.py...")
