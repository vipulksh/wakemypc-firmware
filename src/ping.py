"""
ping.py - ICMP Echo (Ping) via Raw Sockets in MicroPython
==========================================================

CONTEXT: REVISITING THE "RAW SOCKETS NOT SUPPORTED" CLAIM
---------------------------------------------------------
network_scanner.py says raw sockets are unavailable on the Pico, so it
falls back to TCP probing. That comment is overly pessimistic. In the
stock MicroPython rp2 build for Pico W, the underlying lwIP stack is
compiled with LWIP_RAW=1, and MicroPython's socket module exposes
SOCK_RAW. So we CAN open an ICMP socket -- with caveats:

  - It only works when the build defines SOCK_RAW (most rp2 PicoW
    builds do; some minimal stripped builds don't). We probe at runtime
    and raise a clear error if not.
  - There is no concept of "root" on the Pico, so no privilege check.
  - The lwIP raw API is simpler than Linux's: it gives us the full IP
    packet on receive (IP header + ICMP), not just the ICMP payload.
  - We have to assemble the ICMP header ourselves and compute the
    Internet checksum -- the kernel does none of that for us.

ICMP ECHO PACKET FORMAT (RFC 792)
---------------------------------
An ICMP Echo Request looks like this (header is 8 bytes, then payload):

    0      1      2          4          6           8
    +------+------+----------+----------+-----------+--- ... ---+
    | Type | Code | Checksum | Ident.   | Sequence  |  Payload  |
    | (8)  | (0)  | (16-bit) | (16-bit) | (16-bit)  |  (any)    |
    +------+------+----------+----------+-----------+--- ... ---+

  - Type = 8 for Echo Request, 0 for Echo Reply
  - Code = 0
  - Checksum = 16-bit one's complement of the entire ICMP message
               (header + payload), with the checksum field set to 0
               during computation
  - Identifier + Sequence are echoed back unchanged by the responder
    so we can match replies to requests

THE INTERNET CHECKSUM (RFC 1071)
--------------------------------
Sum every 16-bit word as a 32-bit accumulator, fold the carry bits back
in, then one's-complement the result. If the data length is odd, the
last byte is padded with a zero byte (only for the checksum -- not on
the wire). Same algorithm as IP, UDP, and TCP checksums.

WHAT YOU GET / WHAT YOU DON'T
-----------------------------
This module gives you a working ping(host) function that returns the
round-trip time in milliseconds, or None on timeout/error. It is NOT
a drop-in replacement for the TCP probing in network_scanner -- ICMP
adds value because:

  - Many devices (printers, IoT, NAS appliances) reply to ICMP but have
    every TCP port firewalled, so TCP probing reports them offline.
  - ICMP is faster: a single round-trip, often <5ms on a LAN.
  - It works across L3 (routers won't forward arbitrary TCP, but they
    pass ICMP).

But ICMP also has trade-offs:

  - Some hosts (Windows by default, hardened servers) drop incoming
    ICMP Echo, so a "no reply" result is ambiguous.
  - We can't distinguish "host down" from "ICMP filtered." A combined
    strategy (try ICMP, fall back to TCP probe) is what production
    scanners do.

Usage:
    from ping import ping
    rtt_ms = ping("192.168.1.1", timeout=1)
    if rtt_ms is not None:
        print("alive,", rtt_ms, "ms")
    else:
        print("no reply")
"""

import socket
import struct
import time
import random


# -------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------
# IANA protocol number for ICMP. Some MicroPython builds don't define
# socket.IPPROTO_ICMP, so we hardcode the value (1) for portability.
_IPPROTO_ICMP = 1

# Echo Request type/code (RFC 792)
_ICMP_ECHO_REQUEST = 8
_ICMP_ECHO_REPLY = 0

# Default ICMP payload. 32 bytes is what `ping` uses on Windows; Linux
# uses 56. The exact size doesn't matter for liveness -- it just has to
# round-trip intact.
_DEFAULT_PAYLOAD = b"micropython-ping-payload-32byte!"


# -------------------------------------------------------------------------
# Internet checksum (RFC 1071)
# -------------------------------------------------------------------------
def _checksum(data):
    """
    Compute the 16-bit Internet checksum over an arbitrary byte string.

    Algorithm:
      1. Treat data as a sequence of 16-bit big-endian words.
      2. Sum them in a wide accumulator.
      3. Fold any carry-out bits (above bit 16) back into the low 16 bits.
      4. Return the one's complement of the result.

    If the length is odd, pad with one zero byte (this padding is only
    for the math; the on-wire packet is unchanged).
    """
    if len(data) % 2:
        data = data + b"\x00"

    s = 0
    # struct.unpack on the whole thing is faster than a Python loop, but
    # MicroPython's struct doesn't accept a count prefix without a fixed
    # length, so we iterate. For an ~40-byte ICMP message this is fine.
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]

    # Fold the high bits back in until only 16 bits remain.
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)

    return (~s) & 0xFFFF


# -------------------------------------------------------------------------
# Build an ICMP Echo Request packet
# -------------------------------------------------------------------------
def _build_echo_request(ident, seq, payload):
    """
    Construct a complete ICMP Echo Request as a bytes object.

    The checksum is computed over the header (with checksum=0) plus
    payload, then patched back into the header.
    """
    # First pass: build header with checksum=0 so we can compute the
    # real checksum over the full message.
    header = struct.pack(
        "!BBHHH",
        _ICMP_ECHO_REQUEST,  # type
        0,                   # code
        0,                   # checksum placeholder
        ident & 0xFFFF,      # identifier
        seq & 0xFFFF,        # sequence number
    )
    chksum = _checksum(header + payload)

    # Second pass: rebuild the header with the real checksum.
    header = struct.pack(
        "!BBHHH",
        _ICMP_ECHO_REQUEST,
        0,
        chksum,
        ident & 0xFFFF,
        seq & 0xFFFF,
    )
    return header + payload


# -------------------------------------------------------------------------
# Parse an ICMP Echo Reply
# -------------------------------------------------------------------------
def _parse_reply(buf, expect_ident, expect_seq):
    """
    Given the bytes received on the raw socket, return True if they
    contain a matching Echo Reply for our (ident, seq), else False.

    On lwIP-backed MicroPython, recv() on a SOCK_RAW with IPPROTO_ICMP
    returns the full IP datagram: 20 bytes of IPv4 header (assuming no
    IP options) followed by the 8-byte ICMP header and payload. We
    strip the IP header by reading its IHL (header length) field.

    We defensively also handle the case where the platform strips the
    IP header for us (some embedded stacks do), by checking whether
    byte 0 looks like an ICMP type rather than an IP version nibble.
    """
    if len(buf) < 8:
        return False

    # IPv4 header starts with version (4 bits) and IHL (4 bits). For
    # IPv4 with no options, IHL=5 -> first byte = 0x45. If we see 0x45
    # (or any 0x4X) we have the IP header; otherwise assume stripped.
    first = buf[0]
    if (first >> 4) == 4:
        ihl = (first & 0x0F) * 4  # IHL is in 4-byte words
        icmp = buf[ihl:]
    else:
        icmp = buf

    if len(icmp) < 8:
        return False

    icmp_type, icmp_code, _chk, ident, seq = struct.unpack("!BBHHH", icmp[:8])
    return (
        icmp_type == _ICMP_ECHO_REPLY
        and icmp_code == 0
        and ident == (expect_ident & 0xFFFF)
        and seq == (expect_seq & 0xFFFF)
    )


# -------------------------------------------------------------------------
# Public ping() function
# -------------------------------------------------------------------------
def ping(host, timeout=1, payload=None, ident=None, seq=1):
    """
    Send a single ICMP Echo Request to `host` and wait for the reply.

    Parameters:
        host:    Hostname or dotted-quad IP. We resolve via getaddrinfo.
        timeout: Seconds to wait for a reply before giving up.
        payload: Optional bytes to put in the Echo payload. Defaults to
                 a fixed 32-byte string.
        ident:   16-bit identifier. Random if not given. Useful if you
                 want to multiplex multiple pings.
        seq:     16-bit sequence number. Caller increments across calls
                 if running a sustained ping.

    Returns:
        Round-trip time in milliseconds (int) on a matching Echo Reply,
        or None on timeout / error / non-matching reply.

    Raises:
        OSError if the build doesn't support SOCK_RAW or if the call
        fails for a reason other than timeout (e.g., unresolvable host).
    """
    if payload is None:
        payload = _DEFAULT_PAYLOAD
    if ident is None:
        # 16-bit random ident so concurrent pingers don't collide.
        ident = random.getrandbits(16)

    # Resolve host. getaddrinfo returns a list of 5-tuples; the last
    # element is a sockaddr (ip, port). Port is irrelevant for ICMP
    # but the API requires one.
    addr_info = socket.getaddrinfo(host, 0)
    sockaddr = addr_info[0][-1]

    # Open a raw ICMP socket. SOCK_RAW may be missing on stripped
    # builds; surface a clear message instead of an opaque AttributeError.
    try:
        sock_raw = socket.SOCK_RAW
    except AttributeError:
        raise OSError(
            "This MicroPython build does not expose socket.SOCK_RAW; "
            "raw ICMP is unavailable. Use TCP probing instead."
        )

    sock = socket.socket(socket.AF_INET, sock_raw, _IPPROTO_ICMP)
    try:
        sock.settimeout(timeout)

        packet = _build_echo_request(ident, seq, payload)
        t_send = time.ticks_ms()
        sock.sendto(packet, sockaddr)

        # Read replies until we get one matching our (ident, seq) or
        # we time out. We may receive replies meant for OTHER pingers
        # on the system (rare on a Pico, but the kernel duplicates
        # incoming ICMP to every raw ICMP socket).
        deadline = time.ticks_add(t_send, int(timeout * 1000))
        while True:
            try:
                # 1500 = typical Ethernet MTU; oversized is harmless.
                buf = sock.recv(1500)
            except OSError:
                # Treat any socket error during recv (timeout, EAGAIN)
                # as "no reply within budget."
                return None

            t_recv = time.ticks_ms()
            if _parse_reply(buf, ident, seq):
                return time.ticks_diff(t_recv, t_send)

            # Not ours -- keep waiting if there's still time.
            if time.ticks_diff(deadline, t_recv) <= 0:
                return None
    finally:
        sock.close()


# -------------------------------------------------------------------------
# CLI entry point for quick manual testing on the Pico REPL:
#   >>> import ping
#   >>> ping.main("192.168.1.1")
# -------------------------------------------------------------------------
def main(host, count=4, timeout=1):
    """Send `count` pings, print results, mimic the system `ping` tool."""
    print("PING", host)
    sent = 0
    received = 0
    for i in range(1, count + 1):
        sent += 1
        try:
            rtt = ping(host, timeout=timeout, seq=i)
        except OSError as e:
            print("  error:", e)
            return
        if rtt is None:
            print("  seq=", i, "timeout")
        else:
            received += 1
            print("  seq=", i, "rtt=", rtt, "ms")
        time.sleep(1)
    loss = 100 * (sent - received) // sent if sent else 0
    print("---", host, "ping statistics ---")
    print(sent, "sent,", received, "received,", loss, "% loss")
