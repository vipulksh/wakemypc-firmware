"""
led_controller.py - Onboard LED Status Indicator
==================================================

THE PICO'S ONBOARD LED:
-----------------------
The Pico W 2 has a single LED built into the board. Unlike the original Pico
(where the LED is connected to GPIO pin 25), the Pico W's LED is connected
to the WiFi chip (CYW43439), not directly to a GPIO pin. This is because the
WiFi chip uses many of the GPIO pins internally.

To control it in MicroPython, you use:
    from machine import Pin
    led = Pin("LED", Pin.OUT)     # "LED" is a special name for the onboard LED
    led.on()                       # Turn it on (value = 1)
    led.off()                      # Turn it off (value = 0)
    led.toggle()                   # Flip the current state

WHAT IS GPIO?
GPIO = General Purpose Input/Output. These are the physical pins on the edge
of the Pico board. Each pin can be configured as:
  - INPUT:  Read a voltage (is a button pressed? is a sensor detecting something?)
  - OUTPUT: Set a voltage (turn on an LED, activate a motor, send a signal)

The Pico has 26 GPIO pins (GP0 through GP25). The onboard LED on the Pico W
isn't on a numbered GPIO -- it's controlled through the WiFi chip's driver.

WHAT IS Pin.OUT?
Pin.OUT means we're configuring this pin as an OUTPUT -- we want to SEND a
signal (high voltage = LED on, low voltage = LED off). The alternative is
Pin.IN (reading a signal from a sensor, button, etc.).

LED BLINK PATTERNS:
-------------------
Since we only have ONE LED (not an RGB LED, not a screen), we use different
blink patterns to communicate status, like Morse code:

    SOLID ON:         Connected to server, everything healthy
    SLOW BLINK (1s):  Connecting to WiFi (please wait...)
    FAST BLINK (200ms): Error condition (WiFi failed, server unreachable)
    TRIPLE FLASH:     Command received and acknowledged
    RAPID STROBE (50ms): Identify mode (for physically finding the device)
    OFF:              Deep sleep / not running

This is a common pattern in IoT devices. Think of the LED on your router:
solid green = connected, blinking = activity, red = error.

HOW WE IMPLEMENT PATTERNS (STATE MACHINE):
We use a simple state machine with a timer. The main loop calls update()
frequently (every iteration). update() checks if it's time to toggle the
LED based on the current pattern's timing. This is NON-BLOCKING -- unlike
time.sleep(), it doesn't pause the program.

WHY NOT USE time.sleep() FOR BLINKING?
If we wrote:
    while True:
        led.on()
        time.sleep(0.5)
        led.off()
        time.sleep(0.5)

This would BLOCK the entire program -- it couldn't do anything else while
sleeping (no WebSocket messages, no heartbeats, no watchdog feeding).
Instead, we check elapsed time and only toggle when needed.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
# `machine` is THE core MicroPython module for hardware control.
# It provides access to:
#   Pin     - GPIO pins (digital input/output)
#   PWM     - Pulse Width Modulation (analog-ish output, for dimming LEDs etc.)
#   Timer   - Hardware timers (periodic/one-shot callbacks)
#   WDT     - Watchdog Timer (auto-reboot on crash)
#   I2C     - Inter-Integrated Circuit bus (for sensors, displays)
#   SPI     - Serial Peripheral Interface (for SD cards, displays)
#   UART    - Serial communication (for GPS modules, other microcontrollers)
#   ADC     - Analog to Digital Converter (read analog voltages)
from machine import Pin

import time


# -------------------------------------------------------------------------
# LED Pattern Definitions
# -------------------------------------------------------------------------
# Each pattern is a tuple of (on_ms, off_ms, repeat_count).
# repeat_count = -1 means repeat forever.
# repeat_count = N means do N cycles then stop (LED stays off).

PATTERNS = {
    # Connected to server, everything working.
    # LED stays on continuously -- the user can see at a glance that it's healthy.
    "connected": {
        "sequence": [(1, 0)],  # ON forever (1ms on, 0ms off = always on)
        "repeat": -1,
    },
    # Connecting to WiFi. Slow pulse so the user knows it's trying.
    "connecting": {
        "sequence": [(500, 500)],  # 0.5s on, 0.5s off
        "repeat": -1,
    },
    # Error state. Fast blink = something is wrong.
    "error": {
        "sequence": [(100, 100)],  # 0.1s on, 0.1s off (fast and urgent)
        "repeat": -1,
    },
    # Command acknowledged. Three quick flashes then off.
    # This gives visual feedback when the server sends a command.
    "ack": {
        "sequence": [(100, 100), (100, 100), (100, 500)],  # flash, flash, flash, pause
        "repeat": 1,  # Do it once
    },
    # Identify mode. Rapid strobe so you can physically find the device
    # among many Picos. "Which one is pico-living-room? Let me trigger identify..."
    "identify": {
        "sequence": [(50, 50)],  # Very fast strobe
        "repeat": 100,  # ~10 seconds of strobing (100 * 100ms = 10s)
    },
    # Auth-fail mode. Distinctive double-blink with a long pause so it's
    # visually different from a generic "error" fast blink. This means the
    # device_token is wrong / rate-limited; the user needs to reprovision
    # via `pico-cli register --rotate` or `--token`. Documented in the
    # firmware README's troubleshooting section.
    "auth_failed": {
        "sequence": [(150, 150), (150, 1500)],  # blink, blink, long pause
        "repeat": -1,
    },
    # Off / idle. LED stays off.
    "off": {
        "sequence": [(0, 1)],  # 0ms on, 1ms off = always off
        "repeat": -1,
    },
}


# -------------------------------------------------------------------------
# LED Controller
# -------------------------------------------------------------------------
class LEDController:
    """
    Controls the onboard LED with different blink patterns.

    This uses a non-blocking approach: you call update() in your main loop,
    and it toggles the LED based on elapsed time. It never sleeps or blocks.

    Usage:
        led = LEDController()
        led.set_pattern("connecting")   # Start slow blink

        # In main loop:
        while True:
            led.update()                # Must be called frequently!
            # ... do other stuff ...

        led.set_pattern("connected")    # Switch to solid on
        led.set_pattern("ack")          # Triple flash (auto-reverts to previous)
    """

    def __init__(self):
        # Initialize the onboard LED pin.
        #
        # Pin("LED", Pin.OUT):
        #   "LED" = the special identifier for the Pico W's onboard LED
        #   Pin.OUT = configure as output (we're driving the LED, not reading it)
        #
        # On the original Pico (non-W), you'd use Pin(25, Pin.OUT) instead.
        # The W version routes the LED through the WiFi chip, hence the
        # different identifier.
        try:
            self._led = Pin("LED", Pin.OUT)
        except Exception:
            # Fallback for non-W Pico or simulator.
            try:
                self._led = Pin(25, Pin.OUT)
            except Exception:
                self._led = None
                print("[led] WARNING: Could not initialize LED pin")

        # Current pattern state.
        self._current_pattern = "off"
        self._previous_pattern = "off"  # For reverting after one-shot patterns.

        # Pattern playback state.
        self._sequence = [(0, 1)]  # Current sequence steps.
        self._step_index = 0  # Which step we're on.
        self._is_on = False  # Current LED state (on/off).
        self._last_toggle = 0  # When we last toggled (ticks_ms).
        self._repeat_count = -1  # How many full cycles remain (-1 = infinite).
        self._cycle_count = 0  # How many full cycles we've completed.

    def set_pattern(self, pattern_name):
        """
        Switch to a different LED pattern.

        Parameters:
            pattern_name: One of the keys in PATTERNS dict:
                          "connected", "connecting", "error", "ack", "identify", "off"

        For one-shot patterns (like "ack"), the LED automatically reverts to
        the previous pattern after the sequence completes. This means you can
        do:
            led.set_pattern("connected")  # Solid on
            led.set_pattern("ack")        # Triple flash, then back to solid on
        """
        if pattern_name not in PATTERNS:
            print("[led] Unknown pattern:", pattern_name)
            return

        pattern = PATTERNS[pattern_name]

        # If this is a one-shot pattern, save the current pattern for reverting.
        if pattern["repeat"] > 0:
            self._previous_pattern = self._current_pattern

        self._current_pattern = pattern_name
        self._sequence = pattern["sequence"]
        self._repeat_count = pattern["repeat"]
        self._step_index = 0
        self._cycle_count = 0
        self._is_on = False
        self._last_toggle = time.ticks_ms()

        # Start with LED off at the beginning of the new pattern.
        self._set_led(False)

    def update(self):
        """
        Update the LED state based on the current pattern.

        MUST be called frequently in the main loop (at least every 50ms
        for smooth patterns). This is the "tick" function of our LED
        state machine.

        HOW IT WORKS:
        1. Look at the current step in the sequence (on_ms, off_ms).
        2. Check how long we've been in the current state (on or off).
        3. If enough time has passed, toggle to the next state.
        4. When we finish a sequence step, move to the next step.
        5. When we finish the whole sequence, either repeat or revert.
        """
        if not self._led:
            return

        now = time.ticks_ms()
        elapsed = time.ticks_diff(now, self._last_toggle)

        # Get the current step's timing.
        step = self._sequence[self._step_index]
        on_ms, off_ms = step

        # Special case: always on (on_ms > 0, off_ms == 0).
        if on_ms > 0 and off_ms == 0:
            self._set_led(True)
            return

        # Special case: always off (on_ms == 0).
        if on_ms == 0:
            self._set_led(False)
            return

        # Determine how long to stay in the current state.
        if self._is_on:
            target_ms = on_ms
        else:
            target_ms = off_ms

        # Check if it's time to toggle.
        if elapsed >= target_ms:
            if self._is_on:
                # We were ON, now turn OFF.
                self._set_led(False)
                self._last_toggle = now
            else:
                # We were OFF, now turn ON.
                # But first, advance to the next step in the sequence.
                self._step_index += 1

                if self._step_index >= len(self._sequence):
                    # We've completed one full cycle of the sequence.
                    self._step_index = 0
                    self._cycle_count += 1

                    # Check if we've done enough cycles.
                    if (
                        self._repeat_count > 0
                        and self._cycle_count >= self._repeat_count
                    ):
                        # One-shot pattern completed. Revert to previous.
                        self._set_led(False)
                        self.set_pattern(self._previous_pattern)
                        return

                self._set_led(True)
                self._last_toggle = now

    def _set_led(self, on):
        """
        Set the LED to on or off.

        Parameters:
            on: True = LED on, False = LED off

        WHY A SEPARATE METHOD?
        1. It tracks the current state in self._is_on
        2. It handles the case where the LED pin wasn't initialized
        3. Single point of control makes debugging easier
        """
        self._is_on = on
        if self._led:
            # Pin.value(1) = high voltage = LED on
            # Pin.value(0) = low voltage = LED off
            # This is the most portable way to set a pin's output.
            self._led.value(1 if on else 0)

    def get_pattern(self):
        """Return the name of the current pattern."""
        return self._current_pattern

    def flash_ack(self):
        """
        Convenience method: trigger the acknowledgment flash pattern.
        Automatically reverts to the previous pattern when done.
        """
        self.set_pattern("ack")

    def identify(self):
        """
        Convenience method: trigger the identify strobe pattern.
        Used when the server sends an "identify" command so the user
        can physically locate this specific Pico among many.
        """
        self.set_pattern("identify")


def handle_identify(message, proto):
    """
    Protocol handler for the "identify" command.

    The server sends this when the user clicks "Identify" in the dashboard.
    The Pico's LED strobes rapidly for ~10 seconds so the user can
    physically find it (e.g., which Pico is in the living room?).

    Expected message: {"type": "identify"}
    Response: {"type": "identify_ack"}
    """
    # Access the LED controller from the proto's context.
    # We store it there during boot (in main.py).
    if hasattr(proto, "_led"):
        proto._led.identify()

    proto.send_response(
        "identify_ack",
        {
            "message": "LED identify pattern activated",
        },
    )
