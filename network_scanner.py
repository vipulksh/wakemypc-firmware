"""
network_scanner.py - Device Status Checker via TCP Probes
==========================================================

HOW DO WE CHECK IF A DEVICE IS ONLINE?
---------------------------------------
On a regular computer, you'd use `ping` (ICMP Echo Request). But MicroPython
on the Pico CANNOT send ICMP packets because:

1. ICMP requires "raw sockets" -- low-level sockets that let you craft custom
   network packets. MicroPython's socket implementation doesn't support these.
2. Raw sockets typically require root/admin privileges, and the concept of
   "privileges" doesn't exist on a microcontroller.
3. ICMP is a separate protocol from TCP/UDP, and the Pico's network stack
   (lwIP) only exposes TCP and UDP to MicroPython.

ALTERNATIVE: TCP PORT PROBING
------------------------------
Instead of ICMP ping, we try to connect to well-known TCP ports on the target
device. If we can establish a TCP connection (or get a "connection refused"
response), the device is ONLINE. If we get a timeout (no response at all),
the device is likely OFFLINE.

WHAT IS A TCP PORT?
A port is a 16-bit number (0-65535) that identifies a specific service on a
device. Think of the IP address as a building's street address, and the port
as the apartment number. Common ports:

    Port 22   = SSH (Secure Shell, remote terminal)
    Port 80   = HTTP (web server)
    Port 443  = HTTPS (secure web server)
    Port 445  = SMB (Windows file sharing)
    Port 3389 = RDP (Windows Remote Desktop)
    Port 548  = AFP (Apple file sharing)

If a device has ANY of these services running, connecting to that port will
succeed (or at least respond with "connection refused" if the port is closed
but the machine is on). Either way, we know the device is alive.

WHY CONNECTION REFUSED = ONLINE:
When you try to connect to a closed port on a RUNNING machine:
  - The machine's OS responds with a TCP RST (Reset) packet
  - Our socket.connect() raises a "connection refused" error
  - But we know the machine IS online because it responded!

When the machine is OFF or unreachable:
  - No response at all
  - Our socket.connect() times out after our timeout period
  - This tells us the machine is offline

ABOUT ARP (Address Resolution Protocol):
ARP maps IP addresses to MAC addresses on a local network. In theory, you could
detect online devices by sending ARP requests -- every device MUST respond to ARP
for its IP. However, MicroPython doesn't expose ARP directly. The Pico's network
stack (lwIP) handles ARP internally and doesn't give us an API to send custom ARP
packets or read the ARP cache. So TCP probing is our best option.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
import socket
import time


# -------------------------------------------------------------------------
# Default ports to probe
# -------------------------------------------------------------------------
# These are the most common ports found on home/office devices.
# We try them in order; if any one connects, the device is online.
DEFAULT_PROBE_PORTS = [
    # (port_number, service_name, description)
    (22, "SSH", "Secure Shell -- remote terminal access"),
    (80, "HTTP", "Web server -- most devices have a web interface"),
    (443, "HTTPS", "Secure web server"),
    (445, "SMB", "Windows/Samba file sharing"),
    (3389, "RDP", "Windows Remote Desktop Protocol"),
    (548, "AFP", "Apple Filing Protocol"),
    (8080, "HTTP-ALT", "Alternative HTTP port -- common for apps/APIs"),
    (3000, "DEV", "Common development server port"),
]


# -------------------------------------------------------------------------
# Network Scanner
# -------------------------------------------------------------------------
class NetworkScanner:
    """
    Checks whether devices on the LAN are online by probing TCP ports.

    This is a lightweight alternative to ICMP ping that works within
    MicroPython's limitations.

    Usage:
        scanner = NetworkScanner()
        result = scanner.check_device("192.168.1.10")
        print(result)
        # {"ip": "192.168.1.10", "online": True, "port": 22, "service": "SSH", ...}

        results = scanner.check_devices([
            {"ip": "192.168.1.10", "name": "Desktop"},
            {"ip": "192.168.1.20", "name": "NAS"},
        ])
    """

    def __init__(self, timeout=2, ports=None):
        """
        Parameters:
            timeout: Seconds to wait for each TCP connection attempt.
                     2 seconds is a good balance between speed and reliability.
                     On a local network, responses are usually <50ms, so 2s
                     gives plenty of margin.

            ports:   List of (port, name, description) tuples to probe.
                     If None, uses DEFAULT_PROBE_PORTS.

        ABOUT TIMEOUTS:
        Setting the right timeout is important:
        - Too short (e.g., 0.1s): might miss slow devices, false negatives
        - Too long (e.g., 10s): scanning becomes painfully slow
        - 2 seconds: good for local network (devices respond in <100ms usually)
        """
        self._timeout = timeout
        self._ports = ports or DEFAULT_PROBE_PORTS

    def check_device(self, ip, ports=None):
        """
        Check if a single device is online by probing its TCP ports.

        Parameters:
            ip:    The IP address to check (e.g., "192.168.1.10")
            ports: Optional list of (port, name, desc) tuples to override defaults

        Returns a dict:
            {
                "ip": "192.168.1.10",
                "online": True/False,
                "response_time_ms": 45,        # Only if online
                "port": 22,                     # Port that responded
                "service": "SSH",               # Name of the service
                "open_ports": [22, 80],         # All ports that responded
            }

        HOW TCP PROBING WORKS (step by step):
        1. Create a TCP socket (SOCK_STREAM)
        2. Set a timeout (so we don't wait forever)
        3. Try to connect to ip:port
        4. If connect() succeeds -> port is OPEN -> device is ONLINE
        5. If connect() raises "connection refused" -> port is CLOSED but device is ONLINE
        6. If connect() times out -> port is unreachable -> try next port
        7. If all ports time out -> device is probably OFFLINE
        """
        probe_ports = ports or self._ports
        result = {
            "ip": ip,
            "online": False,
            "response_time_ms": None,
            "port": None,
            "service": None,
            "open_ports": [],
        }

        for port_num, service_name, _ in probe_ports:
            start_time = time.ticks_ms()

            try:
                # Create a new TCP socket for each probe.
                # We can't reuse sockets between different hosts/ports.
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

                # Set the connection timeout.
                # If connect() doesn't complete within this time, it raises OSError.
                sock.settimeout(self._timeout)

                try:
                    # Attempt to connect.
                    # socket.connect() performs the TCP 3-way handshake:
                    #   Client -> Server: SYN (synchronize)
                    #   Server -> Client: SYN-ACK (acknowledge)
                    #   Client -> Server: ACK
                    # If the handshake completes, the port is OPEN.
                    sock.connect((ip, port_num))

                    # Connection succeeded -- port is open, device is online!
                    elapsed = time.ticks_diff(time.ticks_ms(), start_time)
                    result["online"] = True
                    result["open_ports"].append(port_num)

                    # Record the first responding port.
                    if result["port"] is None:
                        result["port"] = port_num
                        result["service"] = service_name
                        result["response_time_ms"] = elapsed

                except OSError as e:
                    # Check the error type.
                    # The error message/number varies by MicroPython version,
                    # so we check for common patterns.
                    error_str = str(e)
                    elapsed = time.ticks_diff(time.ticks_ms(), start_time)

                    # "ECONNREFUSED" or errno 111 = Connection Refused.
                    # This means the device IS online but this specific port
                    # is closed (no service listening). The device's OS sent
                    # back a TCP RST packet.
                    if "ECONNREFUSED" in error_str or "111" in error_str:
                        result["online"] = True
                        if result["response_time_ms"] is None:
                            result["response_time_ms"] = elapsed
                            result["port"] = port_num
                            result["service"] = service_name + " (refused)"
                        # Don't add to open_ports since it was refused.

                    # "ETIMEDOUT" or similar = No response at all.
                    # The device is either off, unreachable, or behind a firewall
                    # that silently drops the packet.
                    # We just move on to the next port.

                finally:
                    sock.close()

            except Exception as e:
                # Socket creation failed or other unexpected error.
                print("[scanner] Error probing", ip, ":", port_num, "-", e)
                continue

        return result

    def check_devices(self, targets):
        """
        Check multiple devices and return their status.

        Parameters:
            targets: List of dicts, each with at least "ip" key.
                     Optional keys: "name", "mac", "ports"

                     Example:
                     [
                         {"ip": "192.168.1.10", "name": "Gaming PC", "mac": "AA:BB:CC:DD:EE:FF"},
                         {"ip": "192.168.1.20", "name": "NAS"},
                     ]

        Returns:
            List of result dicts (same format as check_device() output,
            plus the "name" and "mac" from the input).

        NOTE ON PERFORMANCE:
        MicroPython is single-threaded, so we check devices one at a time.
        With 2-second timeouts and 8 ports, checking one offline device
        takes up to 16 seconds. To keep this manageable, we stop probing
        a device as soon as we confirm it's online (any open port suffices).
        For offline devices, we probe all ports (since we need to wait for
        all timeouts before declaring "offline").

        We also use a reduced port list for batch scans to keep total time
        reasonable.
        """
        results = []

        # For batch scans, use a smaller set of ports to save time.
        # We pick the most commonly open ports.
        quick_ports = [
            (80, "HTTP", "Web server"),
            (22, "SSH", "Secure Shell"),
            (445, "SMB", "File sharing"),
            (3389, "RDP", "Remote Desktop"),
        ]

        for target in targets:
            ip = target.get("ip")
            if not ip:
                continue

            print("[scanner] Checking", target.get("name", ip), "(", ip, ")")

            # Check the device.
            result = self.check_device(ip, ports=quick_ports)

            # Forward all input metadata to the result. We previously only
            # copied "name" and "mac"; that dropped public_id, which the
            # server-side _handle_device_status keys off. Without public_id
            # the server doesn't know which Device row to update and the
            # dashboard's online/offline dot stays stuck.
            result["name"] = target.get("name", "")
            result["mac"] = target.get("mac", "")
            result["public_id"] = target.get("public_id", "")

            results.append(result)

            # Brief pause between devices to avoid overwhelming the network.
            time.sleep(0.1)

        return results


def handle_scan(message, proto):
    """
    Protocol handler for device scan commands from the server.

    Expected message format:
        {
            "type": "scan",
            "targets": [
                {"ip": "192.168.1.10", "name": "Desktop", "mac": "AA:BB:..."},
                {"ip": "192.168.1.20", "name": "NAS"},
            ]
        }

    Response:
        {
            "type": "scan_result",
            "devices": [
                {"ip": "192.168.1.10", "name": "Desktop", "online": true, ...},
                {"ip": "192.168.1.20", "name": "NAS", "online": false, ...},
            ]
        }
    """
    targets = message.get("targets", [])
    if not targets:
        proto.send_response(
            "scan_result",
            {
                "devices": [],
                "message": "No targets specified",
            },
        )
        return

    scanner = NetworkScanner(timeout=2)
    results = scanner.check_devices(targets)

    # Summary for the log.
    online = sum(1 for r in results if r["online"])
    print("[scanner] Results:", online, "/", len(results), "online")

    proto.send_response(
        "scan_result",
        {
            "devices": results,
            "total": len(results),
            "online": online,
            "offline": len(results) - online,
        },
    )
