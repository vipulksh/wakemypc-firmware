"""
config.py - Configuration Management for the Pico Firmware
===========================================================

HOW STORAGE WORKS ON THE PICO:
------------------------------
The Pico has 4 MB of flash memory. Think of flash memory like a tiny SSD --
it persists even when power is removed (unlike RAM, which is wiped).

MicroPython sets up a small filesystem on part of this flash. You can create,
read, and write files just like on a normal computer, using Python's built-in
open() function. The filesystem is very small (about 1.5 MB usable), so we
only store essential data.

Our configuration is stored in a file called "secrets.json" on this filesystem.
It contains:
  - wifi_networks: A list of {ssid, password} dicts (so the Pico can try
    multiple WiFi networks -- useful if you move it between home and office)
  - ws_endpoint: The WebSocket endpoint URL for this Pico to connect to the server
  - server_url: The base URL of the server (used for API calls, etc.)
  - device_token: An authentication token so the server knows this Pico is legit
  - device_id: A unique identifier for this specific Pico device

WHY JSON?
---------
MicroPython includes a built-in `json` module (also available as `ujson` --
the "u" prefix means "micro" and is a MicroPython convention). JSON is simple,
human-readable, and easy to edit if you connect to the Pico's REPL (interactive
shell) for debugging.

FILE I/O IN MICROPYTHON:
------------------------
Works almost identically to regular Python:
    with open("secrets.json", "r") as f:
        data = json.load(f)

The main difference is that file paths are relative to the Pico's root
filesystem (there's no home directory, no /usr, etc. -- just a flat root).
So "secrets.json" means "/secrets.json" on the Pico.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
# In MicroPython, `json` is available as both `json` and `ujson`.
# We try `ujson` first (the native MicroPython implementation, slightly faster)
# and fall back to `json` (which is an alias for ujson on most MicroPython builds).
try:
    import ujson as json
except ImportError:
    import json

# `os` in MicroPython is a stripped-down version of Python's os module.
# It supports basics like os.listdir(), os.remove(), os.rename(), os.stat().
# No os.path module though -- we handle paths manually.
import os


# -------------------------------------------------------------------------
# Default Configuration
# -------------------------------------------------------------------------
# This is the template used when no secrets.json exists yet (first boot).
# The Pico will load these defaults but won't be able to do much until
# you configure real WiFi credentials and a server URL.
DEFAULT_CONFIG = {
    # List of WiFi networks to try, in order of preference.
    # Each entry is a dict with "ssid" (network name) and "password".
    # The Pico will try each one until it connects successfully.
    "wifi_networks": [],
    # The Server URL of your Django server.
    # "https://" means  Secure (encrypted with TLS).
    # "ws://" means unencrypted (only use for local development).
    "server_url": "",
    # The WebSocket endpoint URL for this Pico to connect to the server.
    # Example: "wss://yourserver.com/ws/pico/"
    "ws_endpoint": "",
    # Authentication token -- the server gives you this when you register
    # a new Pico device. It proves this Pico is authorized to connect.
    "device_token": "",
    # A unique ID for this Pico. Usually set during provisioning.
    # This lets the server distinguish between multiple Pico devices.
    "device_id": "",
    # How often (in seconds) to send a heartbeat to the server.
    # The server uses this to know the Pico is still alive and connected.
    "heartbeat_interval": 30,
    # How often (in seconds) to scan for device status on the LAN.
    "scan_interval": 60,
}

# The filename where we store the configuration on the Pico's flash.
CONFIG_FILE = "secrets.json"

# A backup of the config, in case the main file gets corrupted
# (e.g., power loss during a write operation).
CONFIG_BACKUP = "secrets.json.bak"


# -------------------------------------------------------------------------
# Config Class
# -------------------------------------------------------------------------
class Config:
    """
    Manages reading and writing the Pico's configuration file.

    Usage:
        cfg = Config()
        cfg.load()                          # Read from flash
        ssid = cfg.get("wifi_networks")     # Access a value
        cfg.set("device_id", "pico-001")    # Change a value
        cfg.save()                          # Write back to flash

    IMPORTANT NOTE ABOUT FLASH WRITES:
    Flash memory has a limited number of write cycles (typically 100,000).
    This sounds like a lot, but if you wrote once per second, you'd wear out
    the flash in about 28 hours. So we only write when configuration actually
    changes -- never in the main loop.
    """

    def __init__(self):
        # _data holds the current configuration as a Python dict.
        # We start with a copy of the defaults so every key is guaranteed
        # to exist even if secrets.json is missing some fields.
        self._data = dict(DEFAULT_CONFIG)

    def load(self):
        """
        Load configuration from the flash filesystem.

        Tries to read secrets.json. If it doesn't exist or is corrupted,
        falls back to the backup file, then to defaults.

        Returns True if a config file was loaded, False if using defaults.
        """
        # Try the main config file first, then the backup.
        for filename in (CONFIG_FILE, CONFIG_BACKUP):
            try:
                # open() works the same as in regular Python.
                # "r" = read mode (text). MicroPython also supports "rb" for binary.
                with open(filename, "r") as f:
                    # json.load() reads the file and parses JSON into a Python dict.
                    loaded = json.load(f)

                # Merge loaded values into our defaults.
                # This way, if the config file is missing a key that we added
                # in a firmware update, the default value is used.
                if isinstance(loaded, dict):
                    self._data.update(loaded)
                    # Print config summary with secrets masked so the log
                    # shows what's configured without leaking credentials.
                    # Masked fields: device_token, wifi passwords.
                    wifi_ssids = [
                        n.get("ssid", "?")
                        for n in self._data.get("wifi_networks", [])
                    ]
                    print("[config] Loaded from", filename)
                    print("[config]   device_id  =", self._data.get("device_id", "(none)"))
                    print("[config]   server_url =", self._data.get("server_url", "(none)"))
                    print("[config]   ws_endpoint=", self._data.get("ws_endpoint", "(none)"))
                    print("[config]   wifi_ssids =", wifi_ssids)
                    print("[config]   token      = ***masked***")
                    return True

            except OSError:
                # OSError means the file doesn't exist.
                # In MicroPython, there's no FileNotFoundError -- it's OSError.
                print("[config]", filename, "not found")
                continue

            except (ValueError, KeyError):
                # ValueError means the JSON was malformed (corrupted file).
                # This can happen if power was lost during a write.
                print("[config]", filename, "is corrupted, trying backup")
                continue

        # If we get here, no config file was found. Use defaults.
        print("[config] No config found, using defaults")
        return False

    def save(self):
        """
        Save current configuration to the flash filesystem.

        WRITE STRATEGY:
        1. First, back up the current config file (if it exists).
        2. Then write the new config to a temporary file.
        3. Rename the temp file to the real filename.

        This "write-to-temp-then-rename" pattern protects against corruption:
        if power is lost during the write, the old file (or backup) is still intact.
        """
        # Step 1: Back up existing config (if any).
        try:
            # os.rename() moves/renames a file. If the destination exists,
            # it's overwritten (on MicroPython's filesystem).
            os.rename(CONFIG_FILE, CONFIG_BACKUP)
        except OSError:
            # No existing config to back up -- that's fine.
            pass

        # Step 2: Write new config to a temporary file.
        temp_file = CONFIG_FILE + ".tmp"
        try:
            with open(temp_file, "w") as f:
                # json.dump() serializes our dict to JSON and writes it to the file.
                # In MicroPython's ujson, there's no `indent` parameter for
                # pretty-printing. The output is compact (one line).
                json.dump(self._data, f)

            # Step 3: Rename temp file to the real config file.
            # os.rename() is atomic on most filesystems, meaning it either
            # fully succeeds or fully fails -- no half-written files.
            os.rename(temp_file, CONFIG_FILE)
            print("[config] Saved to", CONFIG_FILE)
            return True

        except OSError as e:
            # This could happen if the flash is full (very unlikely with JSON).
            print("[config] Save failed:", e)
            return False

    def get(self, key, default=None):
        """
        Get a configuration value by key.

        Works like dict.get() -- returns `default` if the key doesn't exist.

        Example:
            url = config.get("server_url", "ws://localhost:8000/ws/pico/")
        """
        return self._data.get(key, default)

    def set(self, key, value):
        """
        Set a configuration value. Does NOT automatically save to flash.
        You must call save() explicitly to persist the change.

        Example:
            config.set("device_id", "pico-living-room")
            config.save()  # Now it's persisted to flash
        """
        self._data[key] = value

    def get_all(self):
        """
        Return the entire configuration as a dict.
        Useful for debugging or sending config info to the server.
        """
        # Return a copy so the caller can't accidentally modify our internal state.
        return dict(self._data)

    def reset(self):
        """
        Reset configuration to defaults and save.
        Useful during factory reset / re-provisioning.
        """
        self._data = dict(DEFAULT_CONFIG)
        self.save()
        print("[config] Reset to defaults")

    def file_exists(self):
        """
        Check if a config file exists on the flash filesystem.

        In MicroPython, the typical way to check if a file exists is to
        try os.stat() on it. If it raises OSError, the file doesn't exist.
        (There's no os.path.exists() in MicroPython.)
        """
        try:
            os.stat(CONFIG_FILE)
            return True
        except OSError:
            return False
