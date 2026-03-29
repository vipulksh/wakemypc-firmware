"""
wifi_manager.py - WiFi Connection Manager for Pico W 2
=======================================================

HOW WIFI WORKS ON THE PICO W:
------------------------------
The Pico W has a CYW43439 WiFi chip made by Infineon (formerly Cypress).
This chip handles all the low-level radio communication -- the Pico's main
processor just sends it commands over an SPI bus (a type of internal wiring
protocol between chips).

MicroPython exposes WiFi through the `network` module, which gives you a
WLAN (Wireless LAN) object. There are two modes:

1. **STA_IF (Station Interface)**: The Pico acts as a WiFi CLIENT -- it
   connects TO an existing WiFi network (like your home router). This is
   what we use. Think of "station" like "radio station listener."

2. **AP_IF (Access Point Interface)**: The Pico creates its OWN WiFi
   network that other devices can connect to. Useful for initial setup
   (creating a config portal), but we don't use it for normal operation.

CONNECTING TO WIFI - THE PROCESS:
---------------------------------
1. Activate the WiFi interface (turn on the radio)
2. Scan for available networks (optional but useful for debugging)
3. Call wlan.connect(ssid, password)
4. Wait for the connection to establish (takes 2-10 seconds typically)
5. Once connected, the Pico gets an IP address via DHCP (just like your laptop)
6. Now we can make TCP/UDP connections to other devices!

SIGNAL STRENGTH (RSSI):
-----------------------
RSSI = Received Signal Strength Indicator, measured in dBm (decibels
relative to 1 milliwatt). It's always a negative number:
  -30 dBm = Excellent (very close to router)
  -50 dBm = Good
  -70 dBm = Fair
  -80 dBm = Poor
  -90 dBm = Barely usable

MULTI-SSID SUPPORT:
-------------------
We store multiple WiFi networks in config and try each one in order.
This is useful if:
  - You move the Pico between locations (home, office)
  - You have a primary network and a fallback hotspot
  - Your network has a 2.4GHz and 5GHz SSID (Pico only supports 2.4GHz!)

NOTE: The Pico W only supports 2.4 GHz WiFi (no 5 GHz). Make sure your
router has a 2.4 GHz network enabled. Most routers broadcast both, but
some "smart" routers merge them into one SSID and might cause issues.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
# `network` is a MicroPython-specific module for networking hardware.
# It provides the WLAN class for WiFi operations.
import network

# `time` works similarly to Python's time module, but with fewer functions.
# time.sleep(seconds) -- pause execution (blocking! nothing else runs)
# time.ticks_ms() -- returns a monotonic millisecond counter (wraps around)
# time.ticks_diff(a, b) -- safe way to subtract tick values (handles wrapping)
import time


# -------------------------------------------------------------------------
# WiFi Manager Class
# -------------------------------------------------------------------------
class WiFiManager:
    """
    Manages WiFi connectivity for the Pico W.

    This handles:
    - Connecting to WiFi networks (with multi-SSID support)
    - Monitoring connection status
    - Reconnecting if the connection drops
    - Scanning for available networks

    Usage:
        wifi = WiFiManager()
        connected = wifi.connect([
            {"ssid": "MyHomeWiFi", "password": "secret123"},
            {"ssid": "BackupHotspot", "password": "backup456"},
        ])
        if connected:
            print("IP:", wifi.get_info()["ip"])
    """

    def __init__(self):
        # Create the WLAN interface object in Station (client) mode.
        #
        # WHAT IS network.STA_IF?
        # STA_IF stands for "Station Interface." In WiFi terminology, a
        # "station" is any device that connects to an access point (router).
        # Your phone, laptop, and now this Pico are all "stations."
        #
        # The alternative is AP_IF (Access Point Interface), where the Pico
        # would CREATE a WiFi network for others to join.
        self._wlan = network.WLAN(network.STA_IF)

        # Track which SSID we're connected to (None if not connected).
        self._current_ssid = None

        # Connection timeout in seconds. If we can't connect to a network
        # within this time, we move on to the next one.
        self._timeout = 15

    def connect(self, wifi_networks):
        """
        Try to connect to WiFi using the provided list of networks.

        Parameters:
            wifi_networks: list of dicts, each with "ssid" and "password" keys.
                           Tried in order; first successful connection wins.

        Returns:
            True if connected to any network, False if all failed.

        WHAT HAPPENS INTERNALLY:
        1. We activate the WiFi radio (wlan.active(True))
        2. For each network in the list:
           a. Call wlan.connect(ssid, password)
           b. Poll wlan.isconnected() in a loop with a timeout
           c. If connected, return True
           d. If timeout, disconnect and try the next network
        3. If no network worked, return False
        """
        if not wifi_networks:
            print("[wifi] No WiFi networks configured!")
            return False

        # Step 1: Activate the WiFi radio.
        # The radio is OFF by default to save power. We need to turn it on.
        # This is like flipping the WiFi switch on a laptop.
        self._wlan.active(True)

        # Small delay to let the radio initialize. The CYW43 chip needs a
        # moment to power up and calibrate.
        time.sleep(1)

        # Step 2: Try each network in order.
        for net in wifi_networks:
            ssid = net.get("ssid", "")
            password = net.get("password", "")

            if not ssid:
                continue

            print("[wifi] Trying to connect to:", ssid)

            # Disconnect from any previous attempt (clean slate).
            # Without this, the driver might still be trying to connect
            # to the previous SSID.
            try:
                self._wlan.disconnect()
            except Exception:
                pass

            # Small delay after disconnect.
            time.sleep(0.5)

            try:
                # wlan.connect() starts the connection process.
                # It does NOT block until connected -- it just initiates
                # the WiFi handshake (authentication, association, DHCP).
                # We need to poll isconnected() to know when it's done.
                self._wlan.connect(ssid, password)
            except Exception as e:
                print("[wifi] Connect error:", e)
                continue

            # Step 2b: Wait for connection with timeout.
            # We poll in a loop, checking every 500ms.
            #
            # ABOUT time.ticks_ms():
            # This returns the number of milliseconds since the Pico booted.
            # It wraps around after about 12 days (2^30 ms), so we use
            # time.ticks_diff() for safe subtraction that handles the wrap.
            start = time.ticks_ms()
            timeout_ms = self._timeout * 1000

            while not self._wlan.isconnected():
                # Check if we've exceeded the timeout.
                elapsed = time.ticks_diff(time.ticks_ms(), start)
                if elapsed > timeout_ms:
                    print("[wifi] Timeout connecting to", ssid)
                    break

                # wlan.status() returns a numeric code indicating the current
                # state of the connection attempt:
                #   0 = CYW43_LINK_DOWN (not connected, idle)
                #   1 = CYW43_LINK_JOIN (connecting / joining network)
                #   2 = CYW43_LINK_NOIP (associated but no IP yet)
                #   3 = CYW43_LINK_UP (connected with IP -- success!)
                #  -1 = CYW43_LINK_FAIL (connection failed)
                #  -2 = CYW43_LINK_NONET (SSID not found)
                #  -3 = CYW43_LINK_BADAUTH (wrong password)
                status = self._wlan.status()
                if status < 0:
                    # Negative status = definite failure.
                    status_names = {
                        -1: "LINK_FAIL",
                        -2: "SSID_NOT_FOUND",
                        -3: "WRONG_PASSWORD",
                    }
                    reason = status_names.get(status, "UNKNOWN_ERROR")
                    print("[wifi] Failed:", reason, "(code:", status, ")")
                    break

                # Sleep 500ms before checking again.
                # In MicroPython, time.sleep() accepts floats for sub-second.
                time.sleep(0.5)

            # Check if we successfully connected.
            if self._wlan.isconnected():
                self._current_ssid = ssid

                # wlan.ifconfig() returns a tuple of 4 strings:
                # (ip_address, subnet_mask, gateway, dns_server)
                # Example: ('192.168.1.42', '255.255.255.0', '192.168.1.1', '8.8.8.8')
                ip_info = self._wlan.ifconfig()
                print("[wifi] Connected to", ssid)
                print("[wifi] IP address:", ip_info[0])
                print("[wifi] Subnet mask:", ip_info[1])
                print("[wifi] Gateway:", ip_info[2])
                print("[wifi] DNS:", ip_info[3])
                return True

        # If we get here, none of the networks worked.
        print("[wifi] Could not connect to any configured network")
        self._wlan.active(False)  # Turn off radio to save power.
        return False

    def disconnect(self):
        """
        Disconnect from the current WiFi network and turn off the radio.

        You might call this before connecting to a different network,
        or when putting the Pico into a low-power sleep mode.
        """
        try:
            self._wlan.disconnect()
        except Exception:
            pass
        self._wlan.active(False)
        self._current_ssid = None
        print("[wifi] Disconnected")

    def is_connected(self):
        """
        Check if the Pico is currently connected to a WiFi network.

        Returns True/False.

        NOTE: This checks the current state of the connection. WiFi can
        drop at any time (router reboot, out of range, interference),
        so you should check this periodically in your main loop.
        """
        try:
            return self._wlan.isconnected()
        except Exception:
            return False

    def get_info(self):
        """
        Get current WiFi connection information.

        Returns a dict with:
            ssid:     The name of the connected network
            ip:       The Pico's IP address on the network
            subnet:   The subnet mask (defines the network range)
            gateway:  The router's IP address
            dns:      The DNS server's IP address
            rssi:     Signal strength in dBm (negative number, closer to 0 = better)

        Returns None if not connected.

        ABOUT RSSI:
        RSSI (Received Signal Strength Indicator) tells you how strong the
        WiFi signal is. It's measured in dBm (decibels relative to milliwatt):
          -30 dBm = Amazing (right next to the router)
          -50 dBm = Good
          -70 dBm = Okay, might have occasional drops
          -80 dBm = Poor, expect frequent issues
          -90 dBm = Almost unusable
        """
        if not self.is_connected():
            return None

        ip_info = self._wlan.ifconfig()

        # Get RSSI (signal strength).
        # On the Pico W, wlan.status('rssi') returns the signal strength.
        try:
            rssi = self._wlan.status("rssi")
        except Exception:
            rssi = 0  # Some firmware versions don't support this.

        return {
            "ssid": self._current_ssid,
            "ip": ip_info[0],
            "subnet": ip_info[1],
            "gateway": ip_info[2],
            "dns": ip_info[3],
            "rssi": rssi,
        }

    def scan_networks(self):
        """
        Scan for available WiFi networks in range.

        Returns a list of dicts, each with:
            ssid:      Network name (can be empty for hidden networks)
            bssid:     MAC address of the access point (unique hardware ID)
            channel:   WiFi channel number (1-13 for 2.4GHz)
            rssi:      Signal strength in dBm
            security:  Security type (0=open, 1=WEP, 2=WPA-PSK, 3=WPA2-PSK, 4=WPA/WPA2)
            hidden:    Whether the network is hidden (doesn't broadcast its name)

        HOW WIFI SCANNING WORKS:
        The Pico's radio briefly listens on each WiFi channel (1-13) for
        "beacon frames" -- periodic announcements that access points broadcast
        to advertise their existence. This takes about 2-5 seconds.

        NOTE: The WiFi interface must be active (turned on) to scan.
        We activate it if it isn't already.
        """
        # Make sure the radio is on.
        was_active = self._wlan.active()
        if not was_active:
            self._wlan.active(True)
            time.sleep(1)  # Let the radio warm up.

        results = []
        try:
            # wlan.scan() returns a list of tuples:
            # (ssid_bytes, bssid_bytes, channel, rssi, security, hidden)
            #
            # Note: SSID and BSSID come as bytes objects, not strings.
            # We decode them to strings for easier handling.
            raw_results = self._wlan.scan()

            for entry in raw_results:
                ssid_bytes, bssid_bytes, channel, rssi, security, hidden = entry

                # Decode SSID from bytes to string.
                # Some SSIDs might contain non-UTF8 bytes, so we use 'replace'
                # to handle that gracefully.
                ssid = ssid_bytes.decode("utf-8", "replace")

                # Format BSSID (MAC address) as a human-readable string.
                # BSSID is 6 bytes, displayed as XX:XX:XX:XX:XX:XX
                bssid = ":".join("{:02x}".format(b) for b in bssid_bytes)

                # Map security number to a human-readable name.
                security_names = {
                    0: "OPEN",
                    1: "WEP",
                    2: "WPA-PSK",
                    3: "WPA2-PSK",
                    4: "WPA/WPA2-PSK",
                }

                results.append(
                    {
                        "ssid": ssid,
                        "bssid": bssid,
                        "channel": channel,
                        "rssi": rssi,
                        "security": security_names.get(security, "UNKNOWN"),
                        "hidden": bool(hidden),
                    }
                )

        except Exception as e:
            print("[wifi] Scan error:", e)

        # Sort by signal strength (strongest first).
        # In Python, sorted() with a key function works the same in MicroPython.
        # RSSI is negative, so we sort in descending order (less negative = stronger).
        results.sort(key=lambda x: x["rssi"], reverse=True)

        # Turn radio back off if it wasn't active before.
        if not was_active:
            self._wlan.active(False)

        return results
