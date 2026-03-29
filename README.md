# Pico Firmware - NetMonitor IoT Agent

## What is a Raspberry Pi Pico W 2?

The Raspberry Pi Pico W 2 is a tiny microcontroller board that costs about $6.
Unlike a full Raspberry Pi (which runs Linux), the Pico is a **microcontroller** --
it has no operating system, no desktop, no file manager. It runs ONE program in a
loop, forever, the moment you plug it in.

Key specs:
- **RP2350** dual-core ARM Cortex-M33 processor at 150 MHz
- **520 KB** of RAM (yes, kilobytes -- your phone has millions of times more)
- **4 MB** of flash storage (where your code lives)
- **CYW43** WiFi + Bluetooth chip (this is the "W" in Pico W)
- 26 GPIO pins (General Purpose Input/Output -- for connecting sensors, LEDs, etc.)
- Powered via USB-C (5V) or external power (1.8V-5.5V)

Think of it as a tiny, cheap computer that is perfect for IoT (Internet of Things)
tasks: reading sensors, controlling devices, and communicating over WiFi.

## What is MicroPython?

MicroPython is a lean implementation of Python 3 designed to run on microcontrollers.
It is NOT full CPython -- many standard library modules are missing or renamed (e.g.,
`json` becomes `ujson`, `socket` becomes `usocket`). But the core language (variables,
functions, classes, loops, etc.) works the same way you already know from Django.

Key differences from regular Python:
- No `pip install` -- libraries must be copied as .py files to the board
- Limited RAM means you must be careful with large data structures
- `time.sleep()` works but blocks everything (there is no threading)
- Hardware access via the `machine` module (pins, timers, I2C, SPI, etc.)
- File I/O works but the filesystem is tiny (a few MB of flash)

## What This Firmware Does

This firmware turns the Pico W 2 into a **network agent** that sits on your local
network and takes commands from your Django server via WebSocket:

1. **Wake-on-LAN** -- Sends magic packets to wake sleeping computers
2. **Device Monitoring** -- Checks if devices on the LAN are online via TCP probes
3. **TCP Relay** -- Forwards SSH connections from the server to LAN devices
4. **OTA Updates** -- Can update its own code over WiFi
5. **LED Status** -- Blinks the onboard LED to show connection status

## How to Flash It

Use the standalone `pico_cli` tool (a separate Python package, NOT part of the Django server).
The server never touches the Pico directly -- end users flash and configure their own Picos.

```bash
# Install the CLI tool on your local machine (not the server)
pip install -e ./pico_cli

# Flash and provision a Pico
pico-cli detect                          # List connected Picos
pico-cli flash                           # Flash MicroPython firmware
pico-cli upload                          # Upload this firmware code
pico-cli provision --server-url wss://vipul.app/ws/pico/  # Configure WiFi + server
pico-cli register --api-url https://vipul.app  # Register on server, get token
```

Or manually:
1. Install MicroPython on the Pico (hold BOOTSEL button, plug in USB, drag .uf2 file)
2. Connect to the Pico's REPL via serial (e.g., `screen /dev/ttyACM0 115200`)
3. Copy all .py files to the Pico using `mpremote` or Thonny IDE

## File Structure

```
pico_firmware/
  config.py          - Configuration management (reads/writes secrets.json)
  wifi_manager.py    - WiFi connection with multi-SSID support
  ws_client.py       - WebSocket client for server communication
  protocol.py        - Message routing (dispatches commands to handlers)
  wol.py             - Wake-on-LAN magic packet sender
  network_scanner.py - TCP-based device online/offline checker
  tcp_relay.py       - TCP relay for SSH tunneling through WebSocket
  led_controller.py  - Onboard LED blink patterns for status indication
  ota_updater.py     - Over-the-air firmware updates
  watchdog.py        - Hardware watchdog timer for crash recovery
  main.py            - Entry point: boot sequence and main loop
```
