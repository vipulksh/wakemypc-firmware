"""
wol.py - Wake-on-LAN Magic Packet Sender
==========================================

WHAT IS WAKE-ON-LAN (WOL)?
---------------------------
Wake-on-LAN is a networking standard (from 1995!) that lets you turn on a
computer remotely by sending a special network packet called a "magic packet."

HOW IS THIS POSSIBLE?
Even when a computer is "off" (shut down or sleeping), its network card (NIC)
is still powered -- it draws a tiny bit of power from the motherboard's standby
power rail (the +5V_SB line from the power supply). The NIC listens for magic
packets on the network. When it sees one addressed to its MAC address, it
signals the motherboard to power on. It's like having a doorbell that works
even when the house is "off."

REQUIREMENTS FOR WOL TO WORK:
1. The target computer's BIOS/UEFI must have WOL enabled
2. The operating system must not have disabled WOL on shutdown
3. The NIC must support WOL (virtually all modern NICs do)
4. The sender (our Pico) must be on the same local network (or the router
   must forward broadcasts)

THE MAGIC PACKET FORMAT:
------------------------
The magic packet is a specific byte sequence:
    [6 bytes of 0xFF] + [target MAC address repeated 16 times]

So for MAC address AA:BB:CC:DD:EE:FF, the packet is:
    FF FF FF FF FF FF          (6 bytes of 0xFF -- the "sync stream")
    AA BB CC DD EE FF          (MAC address, repetition 1)
    AA BB CC DD EE FF          (MAC address, repetition 2)
    AA BB CC DD EE FF          (MAC address, repetition 3)
    ...                        (13 more repetitions)
    AA BB CC DD EE FF          (MAC address, repetition 16)

Total size: 6 + (6 * 16) = 102 bytes

WHY 16 TIMES?
The magic packet was designed to be extremely unlikely to appear by accident
in normal network traffic. Having the MAC address repeated 16 times after
the sync stream makes false positives virtually impossible.

HOW IT'S SENT:
The magic packet is sent as a UDP broadcast on port 9 (or sometimes port 7).

WHAT IS A BROADCAST?
A broadcast sends a packet to ALL devices on the local network simultaneously.
The broadcast address is typically 255.255.255.255 (or the subnet's broadcast
address, like 192.168.1.255 for a /24 network). Every device on the network
receives the packet, but only the NIC with the matching MAC address acts on it.

WHAT IS UDP?
UDP (User Datagram Protocol) is a simple, connectionless protocol. Unlike TCP
(which has handshakes, acknowledgments, and retransmission), UDP just fires
a packet and hopes it arrives. This is perfect for WOL because:
1. The target computer is "off" and can't do TCP handshakes
2. We just need to blast the packet out; reliability isn't critical
3. We can always retry if it doesn't work

WHAT IS A MAC ADDRESS?
A MAC (Media Access Control) address is a unique 6-byte hardware identifier
assigned to every network interface at the factory. It looks like:
    AA:BB:CC:DD:EE:FF (or AA-BB-CC-DD-EE-FF)
Each byte is a hexadecimal number (00-FF = 0-255). Unlike IP addresses,
MAC addresses don't change (though some software can fake them).
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
# `socket` (or `usocket` in MicroPython) provides low-level network operations.
# We use it to create UDP sockets and send broadcast packets.
import socket

# `time` for timestamps in response messages.
import time


def parse_mac(mac_string):
    """
    Parse a MAC address string into 6 bytes.

    Accepts common formats:
        "AA:BB:CC:DD:EE:FF"  (colon-separated, most common)
        "AA-BB-CC-DD-EE-FF"  (dash-separated, Windows style)
        "AABBCCDDEEFF"       (no separator)

    Returns a bytes object of length 6.
    Raises ValueError if the format is invalid.

    HOW THIS WORKS:
    1. Strip any separators (colons, dashes)
    2. Parse the remaining string as a hex number
    3. Convert to 6 bytes

    Example:
        parse_mac("AA:BB:CC:DD:EE:FF")
        -> b'\\xaa\\xbb\\xcc\\xdd\\xee\\xff'
    """
    # Remove common separators.
    mac_clean = mac_string.replace(":", "").replace("-", "").strip()

    # Validate length. 6 bytes = 12 hex characters.
    if len(mac_clean) != 12:
        raise ValueError("Invalid MAC address length: " + mac_string)

    # Parse each pair of hex characters into a byte.
    # "AA" -> 0xAA = 170, "BB" -> 0xBB = 187, etc.
    try:
        mac_bytes = bytes(int(mac_clean[i : i + 2], 16) for i in range(0, 12, 2))
    except ValueError:
        raise ValueError("Invalid MAC address characters: " + mac_string)

    return mac_bytes


def build_magic_packet(mac_bytes):
    """
    Build a Wake-on-LAN magic packet for the given MAC address.

    Parameters:
        mac_bytes: 6 bytes representing the target MAC address

    Returns:
        bytes: The 102-byte magic packet

    THE MAGIC PACKET STRUCTURE:
    - Bytes 0-5:   Six 0xFF bytes (the "sync stream" / "header")
    - Bytes 6-101: The target MAC address repeated 16 times

    The sync stream (FF FF FF FF FF FF) tells the NIC "this is a magic packet."
    The 16 repetitions of the MAC address tell it WHO to wake up.

    WHY bytes IS USED:
    In MicroPython (and Python 3), network data is always bytes, not strings.
    Strings are for text (UTF-8 encoded). Bytes are for raw binary data.
    When you do b'\\xff', that's one byte with value 255.
    """
    if len(mac_bytes) != 6:
        raise ValueError("MAC address must be exactly 6 bytes")

    # Build the packet:
    # b'\\xff' * 6 creates 6 bytes of 0xFF (the sync stream)
    # mac_bytes * 16 repeats the MAC address 16 times
    magic_packet = b"\xff" * 6 + mac_bytes * 16

    # Sanity check: the packet should be exactly 102 bytes.
    # 6 (sync) + 6 * 16 (MAC * 16) = 6 + 96 = 102
    assert len(magic_packet) == 102

    return magic_packet


def send_magic_packet(mac_string, broadcast_addr="255.255.255.255", port=9):
    """
    Send a Wake-on-LAN magic packet to wake up a computer.

    Parameters:
        mac_string:     MAC address of the target computer (e.g., "AA:BB:CC:DD:EE:FF")
        broadcast_addr: Broadcast address (default: 255.255.255.255)
        port:           UDP port to send on (default: 9, sometimes 7 is used)

    Returns:
        dict with "success" (bool) and "message" (str)

    ABOUT UDP SOCKETS:
    A socket is an endpoint for network communication -- like a phone.
    - socket.SOCK_DGRAM = UDP (datagram, connectionless, fire-and-forget)
    - socket.SOCK_STREAM = TCP (stream, connection-oriented, reliable)

    We use UDP because:
    1. The target is "off" and can't participate in TCP's handshake
    2. Broadcast only works with UDP
    3. It's simpler and uses less memory

    ABOUT SO_BROADCAST:
    By default, sockets don't allow sending to broadcast addresses (as a
    safety measure). We must explicitly enable it with setsockopt(SO_BROADCAST).
    This is like flipping a switch that says "yes, I really want to broadcast."
    """
    try:
        # Step 1: Parse the MAC address.
        mac_bytes = parse_mac(mac_string)

        # Step 2: Build the magic packet.
        packet = build_magic_packet(mac_bytes)

        # Step 3: Create a UDP socket.
        #
        # socket.AF_INET = IPv4 (Internet Protocol version 4)
        # socket.SOCK_DGRAM = UDP (User Datagram Protocol)
        #
        # WHAT IS AF_INET?
        # AF = Address Family. INET = Internet (IPv4).
        # Other options: AF_INET6 (IPv6), AF_UNIX (local sockets).
        # The Pico only supports AF_INET (IPv4).
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        try:
            # Step 4: Enable broadcast sending.
            #
            # setsockopt(level, option, value) sets a socket option.
            # SOL_SOCKET = "socket level" (as opposed to protocol-specific options)
            # SO_BROADCAST = "allow sending to broadcast addresses"
            # 1 = True (enable it)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            # Step 5: Send the magic packet!
            #
            # sendto(data, address_tuple) sends data to a specific address.
            # The address is (ip_string, port_number).
            #
            # "255.255.255.255" is the limited broadcast address -- it reaches
            # all devices on the local network regardless of subnet. The router
            # does NOT forward this to other networks (that's the point of broadcast).
            sock.sendto(packet, (broadcast_addr, port))

            print("[wol] Magic packet sent to", mac_string)
            return {
                "success": True,
                "message": "Magic packet sent to " + mac_string,
                "mac": mac_string,
            }

        finally:
            # Always close the socket when done. Sockets use limited resources
            # (the Pico can only have a handful open at once).
            sock.close()

    except Exception as e:
        print("[wol] Error:", e)
        return {
            "success": False,
            "message": str(e),
            "mac": mac_string,
        }


def handle_wol(message, proto):
    """
    Protocol handler for WOL commands from the server.

    Expected message format:
        {"type": "wol", "mac": "AA:BB:CC:DD:EE:FF"}

    Optional fields:
        "broadcast": "192.168.1.255"  (custom broadcast address)
        "count": 3                     (send the packet multiple times for reliability)

    Response:
        {"type": "wol_result", "success": true/false, "mac": "...", "message": "..."}

    WHY SEND MULTIPLE TIMES?
    WOL uses UDP, which is unreliable -- packets can be lost. Sending the
    magic packet 2-3 times increases the chance of success. We add a small
    delay between sends to avoid congestion.
    """
    mac = message.get("mac")
    if not mac:
        proto.send_response(
            "wol_result",
            {
                "success": False,
                "message": "No MAC address provided",
            },
        )
        return

    # Optional: custom broadcast address.
    broadcast = message.get("broadcast", "255.255.255.255")

    # Optional: number of times to send the packet (default 3 for reliability).
    count = message.get("count", 3)
    count = min(count, 10)  # Cap at 10 to prevent abuse.

    # Send the magic packet multiple times.
    result = None
    for i in range(count):
        result = send_magic_packet(mac, broadcast_addr=broadcast)
        if not result["success"]:
            break  # Stop if there's an error (e.g., invalid MAC).
        if i < count - 1:
            # Small delay between sends (100ms).
            # time.sleep_ms() is a MicroPython-specific function that sleeps
            # for the given number of milliseconds.
            try:
                time.sleep_ms(100)
            except AttributeError:
                time.sleep(0.1)

    if result:
        result["packets_sent"] = count
        proto.send_response("wol_result", result)
