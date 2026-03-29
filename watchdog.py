"""
watchdog.py - Hardware Watchdog Timer for Crash Recovery
=========================================================

WHAT IS A WATCHDOG TIMER?
-------------------------
A watchdog timer (WDT) is a hardware timer that automatically reboots the
microcontroller if the software stops responding. Think of it as a dead man's
switch: you must periodically "feed" the watchdog (reset its timer) to prove
your code is still running. If you stop feeding it (because your code crashed,
hung, or entered an infinite loop), the watchdog's timer expires and it
hard-resets the entire chip.

WHY DO WE NEED THIS?
Microcontrollers run 24/7 in unattended environments. If the firmware crashes
at 3 AM, nobody is there to press the reset button. The watchdog ensures the
Pico always recovers from crashes automatically. Without it, a single bug
could make the device permanently unresponsive until someone physically
power-cycles it.

Common scenarios the watchdog handles:
1. Unhandled exception crashes the main loop
2. Network code gets stuck waiting for a response that never comes
3. Memory leak causes an out-of-memory crash
4. Hardware glitch causes the processor to hang

HOW THE PICO'S WATCHDOG WORKS:
-------------------------------
The RP2350 chip (in the Pico W 2) has a hardware watchdog timer built into
the silicon. It's completely independent of the main processor -- even if the
CPU locks up completely, the watchdog still runs and can reset it.

In MicroPython:
    from machine import WDT
    wdt = WDT(timeout=8000)   # 8-second timeout (in milliseconds)
    wdt.feed()                 # Reset the timer (call this regularly!)

The WDT constructor starts the watchdog immediately. From that point on,
you MUST call wdt.feed() at least once every `timeout` milliseconds,
or the Pico reboots.

IMPORTANT: Once started, the hardware WDT CANNOT be stopped! There's no
wdt.stop() method. This is by design -- a buggy program shouldn't be able
to disable its own safety mechanism. The only way to stop the watchdog is
to reboot (which it will do automatically if you don't feed it).

CHOOSING THE TIMEOUT:
- Too short (e.g., 1 second): Normal operations like WiFi reconnection
  might take longer than this, causing unnecessary reboots.
- Too long (e.g., 60 seconds): After a crash, the device sits unresponsive
  for up to 60 seconds before recovering.
- 8 seconds: A good balance. Most operations complete within a few seconds,
  and 8 seconds of downtime after a crash is acceptable.

FEEDING STRATEGY:
We feed the watchdog at the top of each main loop iteration. If any step in
the loop takes more than 8 seconds (like scanning many devices), we feed it
during that operation too. The key rule: never let 8 seconds pass without
feeding.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
# `machine` provides hardware-level functions.
# WDT = Watchdog Timer class.
from machine import WDT

import time


# -------------------------------------------------------------------------
# Watchdog Manager
# -------------------------------------------------------------------------
class WatchdogManager:
    """
    Manages the hardware watchdog timer.

    This wrapper provides:
    - Safe initialization (with error handling)
    - Feed tracking (for debugging -- when was the last feed?)
    - Optional software-only mode for development (no actual hardware WDT)

    Usage:
        wd = WatchdogManager(timeout_ms=8000)
        wd.start()

        # In main loop:
        while True:
            wd.feed()       # MUST be called every iteration!
            # ... do stuff ...

    ABOUT THE hardware=False OPTION:
    During development on your computer (not on a real Pico), you might want
    to test the firmware logic without the hardware watchdog. Set hardware=False
    to use a software-only mock that logs feeds but doesn't actually reboot.
    """

    def __init__(self, timeout_ms=8000, hardware=True):
        """
        Parameters:
            timeout_ms: Watchdog timeout in milliseconds.
                        If feed() isn't called within this time, the Pico reboots.
                        Range: 1 to 8388 ms (about 8.3 seconds max on RP2350).
                        Default: 8000 ms (8 seconds).

            hardware:   If True, use the real hardware WDT.
                        If False, use a software-only mock (for development).

        NOTE: The timeout range is limited by the hardware. The RP2350's
        watchdog timer uses a 24-bit counter at 1 MHz, giving a maximum
        timeout of about 8.3 seconds (2^23 microseconds).
        """
        self._timeout_ms = timeout_ms
        self._use_hardware = hardware
        self._wdt = None
        self._started = False

        # Tracking for debugging.
        self._feed_count = 0
        self._last_feed_time = 0
        self._start_time = 0

    def start(self):
        """
        Start the watchdog timer.

        WARNING: Once started, the hardware WDT cannot be stopped!
        You MUST call feed() regularly from this point on, or the Pico
        will reboot. Make sure your main loop is set up before calling this.

        Returns True if the watchdog was started successfully.
        """
        if self._started:
            print("[watchdog] Already started")
            return True

        try:
            if self._use_hardware:
                # Create the hardware WDT.
                # This call STARTS the countdown immediately!
                #
                # machine.WDT(timeout=N) where N is milliseconds.
                # The Pico's hardware watchdog has a max timeout of ~8300 ms.
                self._wdt = WDT(timeout=self._timeout_ms)
                print(
                    "[watchdog] Hardware WDT started, timeout:", self._timeout_ms, "ms"
                )
            else:
                print("[watchdog] Software-only mode (no hardware WDT)")

            self._started = True
            self._start_time = time.ticks_ms()
            self._last_feed_time = time.ticks_ms()

            # Feed immediately to start the countdown fresh.
            self.feed()
            return True

        except Exception as e:
            # WDT initialization can fail on non-Pico platforms or simulators.
            print("[watchdog] Failed to start:", e)
            print("[watchdog] Falling back to software-only mode")
            self._use_hardware = False
            self._started = True
            return True

    def feed(self):
        """
        Feed (reset) the watchdog timer.

        This tells the watchdog "I'm still alive, don't reboot me."
        The countdown resets to the full timeout value.

        Call this at least once per loop iteration. If you're doing something
        slow (like scanning many devices), call it during that operation too.

        ANALOGY:
        Imagine a bomb with an 8-second fuse. Every time you call feed(),
        you replace the fuse with a fresh 8-second one. If you forget to
        replace it, BOOM (reboot).

        PERFORMANCE:
        wdt.feed() is extremely fast (a single register write to the hardware).
        Calling it 1000 times per second would have zero measurable impact.
        Don't hesitate to feed it liberally.
        """
        if self._wdt:
            self._wdt.feed()

        self._feed_count += 1
        self._last_feed_time = time.ticks_ms()

    def get_info(self):
        """
        Get watchdog status information (for debugging and heartbeat data).

        Returns a dict with:
            started:       Whether the watchdog is running
            hardware:      Whether the hardware WDT is active
            timeout_ms:    The configured timeout
            feed_count:    How many times feed() has been called
            last_feed_ms:  Time since last feed (in ms)
            uptime_ms:     Time since the watchdog was started
        """
        now = time.ticks_ms()
        return {
            "started": self._started,
            "hardware": self._use_hardware and self._wdt is not None,
            "timeout_ms": self._timeout_ms,
            "feed_count": self._feed_count,
            "last_feed_ms": time.ticks_diff(now, self._last_feed_time)
            if self._started
            else 0,
            "uptime_ms": time.ticks_diff(now, self._start_time) if self._started else 0,
        }
