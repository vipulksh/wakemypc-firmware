"""
ws_client.py - WebSocket Client for MicroPython
=================================================

WHAT IS A WEBSOCKET?
--------------------
HTTP is request-response: the client asks, the server answers, connection done.
WebSocket is a persistent, bidirectional connection: both sides can send messages
at any time, without the other side asking first. It's like a phone call vs.
sending letters.

WebSocket starts as an HTTP request (the "upgrade" handshake), then "upgrades"
to a raw TCP connection with a thin framing protocol on top.

WHY WEBSOCKET FOR THE PICO?
----------------------------
Our server needs to send commands to the Pico at any time (e.g., "wake up that
PC now"). With plain HTTP, the Pico would have to constantly poll the server
("any commands? any commands? any commands?"), wasting bandwidth and battery.
With WebSocket, the server just sends a message whenever it needs to.

WEBSOCKET IN MICROPYTHON vs CPYTHON:
-------------------------------------
In regular Python, you'd use the `websockets` library (pip install websockets).
In MicroPython, there's no pip and limited library support. We implement a
minimal WebSocket client using raw sockets.

The WebSocket protocol is actually quite simple:
1. Open a TCP connection to the server
2. Send an HTTP upgrade request with specific headers
3. Server responds with "101 Switching Protocols"
4. Now both sides send "frames" -- small packets with a header and payload

FRAME FORMAT (simplified):
    Byte 0: [FIN bit][RSV bits][Opcode (4 bits)]
    Byte 1: [MASK bit][Payload length (7 bits)]
    If length == 126: next 2 bytes are the real length
    If length == 127: next 8 bytes are the real length
    If MASK bit set: next 4 bytes are the masking key
    Then: the payload data

Opcodes:
    0x1 = Text frame (what we use for JSON messages)
    0x2 = Binary frame
    0x8 = Close
    0x9 = Ping (server asks "are you alive?")
    0xA = Pong (response to ping)

CLIENT-TO-SERVER MASKING:
All frames from client to server MUST be masked (XOR'd with a random 4-byte key).
This is a security requirement of the WebSocket spec to prevent cache poisoning
attacks on proxies. Server-to-client frames are NOT masked.

TLS (wss://):
"wss://" means WebSocket Secure -- encrypted with TLS (same as HTTPS).
MicroPython supports TLS via the `ssl` (or `ussl`) module, but with limitations:
- Certificate verification is often disabled (the Pico has no CA certificate store)
- Only TLS 1.2 is typically supported
- Some cipher suites may not be available
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
import errno
import socket  # Raw TCP socket operations (MicroPython's usocket)
import time  # For delays and timeouts

try:
    import uselect as select
except ImportError:
    import select

# `ssl` (or `ussl` in MicroPython) wraps a plain TCP socket in TLS encryption.
# This is how "wss://" works -- it's a WebSocket running inside a TLS tunnel.
try:
    import ussl as ssl
except ImportError:
    import ssl

# We need json for encoding/decoding messages.
try:
    import ujson as json
except ImportError:
    import json

# `struct` is for packing/unpacking binary data (used in WebSocket frame headers).
# It works the same as Python's struct module.
import struct

# We need random bytes for the WebSocket masking key and handshake key.
# MicroPython's `os.urandom(n)` generates n cryptographically random bytes.
import os

# `binascii` provides base64 encoding, needed for the WebSocket handshake.
# In MicroPython, it's available as `ubinascii`.
try:
    import ubinascii as binascii
except ImportError:
    import binascii

# `hashlib` for SHA-1 hash used in the WebSocket handshake.
# SECURITY NOTE: SHA-1 here is NOT a security concern. It's a mandatory part of
# the WebSocket protocol (RFC 6455) used only to prove the server understands
# WebSocket during the initial HTTP upgrade handshake. Every browser uses this
# same SHA-1 handshake. Actual security comes from TLS (wss://) which encrypts
# the entire connection with modern ciphers. SHA-1 is only weak for collision
# resistance in digital signatures -- this usage is safe.
try:
    import uhashlib as hashlib
except ImportError:
    pass


# -------------------------------------------------------------------------
# WebSocket Client
# -------------------------------------------------------------------------
class WebSocketClient:
    """
    A minimal WebSocket client for MicroPython.

    This implements just enough of the WebSocket protocol (RFC 6455) to:
    - Connect to ws:// and wss:// servers
    - Send and receive text frames (JSON messages)
    - Handle ping/pong (keepalive)
    - Detect disconnections

    It does NOT support:
    - WebSocket extensions (permessage-deflate, etc.)
    - Fragmented messages (our messages are small enough to fit in one frame)
    - Binary frames (we only use text/JSON)

    Usage:
        ws = WebSocketClient("wss://example.com/ws/pico/")
        if ws.connect():
            ws.send({"type": "auth", "token": "abc123"})
            msg = ws.recv()  # Non-blocking, returns None if no message
            ws.close()
    """

    def __init__(self, url):
        """
        Initialize the WebSocket client with a URL.

        Parameters:
            url: The WebSocket URL to connect to.
                 "ws://host:port/path" for unencrypted
                 "wss://host:port/path" for TLS-encrypted
        """
        # Parse the URL into its components.
        self._url = url
        self._host, self._port, self._path, self._use_ssl = self._parse_url(url)

        # The underlying TCP socket (None when not connected).
        # For wss://, this becomes an SSLSocket after the TLS handshake.
        self._sock = None

        # Reference to the plain TCP socket before it was wrapped in TLS.
        #
        # Why we need this:
        # ssl.wrap_socket() replaces self._sock with an SSLSocket. MicroPython's
        # SSLSocket does not implement settimeout(), so after the wrap we can no
        # longer call self._sock.settimeout(). We keep self._raw_sock pointing at
        # the original TCP socket so we can still control its timeout (e.g. reset
        # it to blocking after the handshake -- see _handshake_once).
        self._raw_sock = None

        # Buffer for accumulating partial frames.
        self._recv_buf = b""

        # Connection state.
        self._connected = False

        # Heartbeat tracking.
        # We send a ping to the server periodically. If we don't get a pong
        # back within a timeout, we consider the connection dead.
        self._last_ping_time = 0
        self._last_pong_time = 0
        self._ping_interval = 30  # seconds between pings
        self._pong_timeout = 10  # seconds to wait for pong response

        # Reconnection with exponential backoff.
        # When the connection drops, we don't immediately retry -- we wait
        # a bit, then longer, then longer. This prevents hammering a server
        # that might be down.
        self._reconnect_delay = 1  # Start at 1 second
        self._max_reconnect_delay = 60  # Cap at 60 seconds
        self._reconnect_attempts = 0
        # === HTTP redirect handling on the WS upgrade ===
        # Some edges (e.g. an HTTPS-only nginx in front of Traefik) return
        # 301/302/307/308 with a Location header when a Pico is provisioned
        # with ws:// against a server that only accepts wss://. Browsers
        # don't follow redirects on a WS upgrade, but for an embedded
        # device that has no other way to discover the right URL it's
        # the friendly thing to do -- otherwise a single misprovisioned
        # secrets.json takes the device offline forever.
        #
        # We cap the chain at MAX_REDIRECTS to make any loop fail fast
        # rather than spinning the radio.
        self._max_redirects = 3

    def _parse_url(self, url):
        """
        Parse a WebSocket URL into host, port, path, and SSL flag.

        Examples:
            "ws://example.com/ws/pico/"    -> ("example.com", 80,  "/ws/pico/", False)
            "wss://example.com/ws/pico/"   -> ("example.com", 443, "/ws/pico/", True)
            "ws://192.168.1.1:8000/ws/"    -> ("192.168.1.1", 8000, "/ws/", False)

        WHY DO WE PARSE MANUALLY?
        In regular Python, you'd use `urllib.parse.urlparse()`. MicroPython
        doesn't have that module, so we parse the URL by hand.
        """
        use_ssl = url.startswith("wss://") or url.startswith("https://")
        # Strip the scheme (ws:// or wss://).
        url = url.replace("wss://", "").replace("ws://", "")
        # if the server url starts with http:// or https://, remove that as well
        url = url.replace("https://", "").replace("http://", "")
        # url used for ws:// and wss:// should not start with http:// or https://, but we handle it gracefully if it does

        # Split path from host.
        if "/" in url:
            host_port, path = url.split("/", 1)
            path = "/" + path
        else:
            host_port = url
            path = "/"

        # Split port from host.
        if ":" in host_port:
            host, port_str = host_port.split(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 443 if use_ssl else 80

        return host, port, path, use_ssl

    def connect(self):
        """
        Establish a WebSocket connection to the server.

        This performs:
        1. DNS resolution (convert hostname to IP address)
        2. TCP connection (3-way handshake)
        3. TLS handshake (if wss://, negotiate encryption)
        4. WebSocket upgrade handshake (HTTP upgrade request)
        5. If the server replies with 301/302/307/308 + Location, retarget
           and try again (up to self._max_redirects hops total).

        Returns True if connected, False if any step failed.

        THE WEBSOCKET HANDSHAKE:
        The client sends an HTTP GET request with special headers:
            GET /ws/pico/ HTTP/1.1
            Host: example.com
            Upgrade: websocket
            Connection: Upgrade
            Sec-WebSocket-Key: <random base64 string>
            Sec-WebSocket-Version: 13

        The server responds with:
            HTTP/1.1 101 Switching Protocols
            Upgrade: websocket
            Connection: Upgrade
            Sec-WebSocket-Accept: <hash of our key + magic string>

        After this, the connection is "upgraded" from HTTP to WebSocket,
        and both sides can send frames freely.
        """
        # Snapshot the originally-requested URL so a failed redirect chain
        # can restore it -- otherwise self._url would be left pointing at
        # whatever URL we last tried, which is misleading on next retry.
        original_url = self._url
        original_state = (self._host, self._port, self._path, self._use_ssl)

        for attempt in range(self._max_redirects + 1):
            result = self._handshake_once()
            if result is True:
                return True
            if isinstance(result, str):
                # Redirect: result is the new absolute URL to try.
                if attempt >= self._max_redirects:
                    print(
                        "[ws] Too many redirects (",
                        attempt + 1,
                        "); giving up",
                    )
                    break
                print(
                    "[ws] Following redirect ->",
                    result,
                    "(",
                    self._max_redirects - attempt,
                    "remaining )",
                )
                # Retarget. _parse_url already promotes http:// -> ws:// and
                # https:// -> wss:// because we strip those schemes too.
                # _normalize_redirect_target adds the explicit promotion so
                # the use_ssl flag is right when the redirect uses http(s).
                new_url = self._normalize_redirect_target(result)
                self._url = new_url
                self._host, self._port, self._path, self._use_ssl = (
                    self._parse_url(new_url)
                )
                continue
            # result is False (or anything else) -> hard failure, no retry.
            break

        # Restore original URL so callers / logs see the configured target.
        self._url = original_url
        self._host, self._port, self._path, self._use_ssl = original_state
        return False

    def _handshake_once(self):
        """
        One TCP+TLS+WebSocket-upgrade attempt against the current URL.

        Returns:
            True            -> upgrade succeeded; self._sock is a live WS.
            <str>           -> server replied with a 3xx redirect; the str
                               is the absolute URL from the Location header.
                               Caller should retarget and try again.
            False           -> hard failure (DNS, TLS, non-3xx non-101, etc).
        """
        print(
            "[ws] Connecting to",
            self._url,
            "| host=",
            self._host,
            "| port=",
            self._port,
            "| tls=",
            self._use_ssl,
        )
        if not self._use_ssl:
            # ws:// is fine for local dev / a LAN-only deployment, but
            # in production wakemypc.com always serves wss://, so a
            # plaintext connection there is almost certainly a mis-
            # provisioning. Surface it loudly.
            print("[ws] WARNING: connecting over plaintext ws:// (no TLS)")

        try:
            # Step 1: DNS resolution and TCP connection.
            #
            # WHAT IS DNS RESOLUTION?
            # Converting a hostname like "example.com" to an IP address like
            # "93.184.216.34". The Pico sends a query to the DNS server
            # (configured by DHCP when connecting to WiFi).
            #
            # socket.getaddrinfo() does this resolution and returns the info
            # needed to create a connection. The result is a list of tuples:
            # [(family, type, proto, canonname, sockaddr), ...]
            addr_info = socket.getaddrinfo(self._host, self._port)
            addr = addr_info[0][-1]  # Extract the (ip, port) tuple.

            # Create a TCP socket.
            # socket.AF_INET = IPv4
            # socket.SOCK_STREAM = TCP (as opposed to SOCK_DGRAM for UDP)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Save the raw socket now, before ssl.wrap_socket() replaces
            # self._sock with an SSLSocket. We need this reference later to
            # call settimeout() -- SSLSocket doesn't support it.
            self._raw_sock = self._sock

            # 5-second timeout for the TCP connect and TLS handshake so we
            # don't hang for the OS default (~2 minutes) on an unreachable host.
            self._raw_sock.settimeout(5)

            # Connect to the server. This performs the TCP 3-way handshake:
            # Client: SYN -> Server: SYN-ACK -> Client: ACK
            self._sock.connect(addr)

            # Step 2: TLS handshake (if using wss://).
            if self._use_ssl:
                # ssl.wrap_socket() takes a plain TCP socket and wraps it in
                # TLS encryption. All data sent/received through this socket
                # is now encrypted.
                #
                # server_hostname is needed for SNI (Server Name Indication),
                # which tells the server which certificate to use (important
                # when multiple sites share the same IP address).
                #
                # NOTE: We don't verify the server's certificate here.
                # MicroPython has limited CA certificate support. In production,
                # you might want to pin a specific certificate.
                self._sock = ssl.wrap_socket(
                    self._sock,
                    server_hostname=self._host,
                )

            # Step 3: WebSocket upgrade handshake.
            # Generate a random 16-byte key, base64-encoded.
            # This key is used to verify that the server understands WebSocket.
            ws_key = binascii.b2a_base64(os.urandom(16)).strip()

            # Build the HTTP upgrade request.
            # User-Agent is required by Cloudflare's bot detection -- without it,
            # Cloudflare silently closes non-browser WebSocket connections after
            # a short idle period even though the initial handshake succeeds.
            request = (
                "GET {path} HTTP/1.1\r\n"
                "Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "User-Agent: WakeMyPC-Pico/1.0\r\n"
                "\r\n"
            ).format(
                path=self._path,
                host=self._host,
                key=ws_key.decode(),
            )

            # Send the handshake request.
            self._sock.send(request.encode())

            # Read the server's response.
            # We read until we see "\r\n\r\n" which marks the end of HTTP headers.
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = self._sock.recv(1024)
                if not chunk:
                    print("[ws] Connection closed during handshake")
                    self._close_socket()
                    return False
                response += chunk

            # Check the server's response. The good outcome is 101 Switching
            # Protocols. Anything else is either a redirect we can chase or
            # a hard failure.
            response_line = response.split(b"\r\n")[0]
            status_code = self._parse_status_code(response_line)

            if status_code == 101:
                # Success! The connection is now a WebSocket.
                self._connected = True
                self._reconnect_delay = 1  # Reset backoff on successful connect.
                self._reconnect_attempts = 0
                self._last_pong_time = time.ticks_ms()

                # THE MBED-TLS / SETTIMEOUT BUG -- why we reset to blocking here:
                #
                # The naive approach is to call settimeout(0.1) on the raw socket
                # after the handshake so that recv() returns quickly when there is
                # no data (non-blocking style). This works on a plain TCP socket
                # but silently breaks wss:// connections on MicroPython.
                #
                # MicroPython's TLS stack (mbedTLS) reads a TLS record by making
                # multiple recv() calls on the underlying TCP socket -- one for the
                # 5-byte TLS header, then one or more for the encrypted payload.
                # If the raw socket has a short timeout and the second recv() fires
                # after that timeout has elapsed, the underlying socket returns
                # EAGAIN. mbedTLS treats that as an error, aborts the current record
                # read, and surfaces the EAGAIN to the caller.
                #
                # The TCP data for that record is now in a half-consumed state:
                # mbedTLS has read the header but not the payload. On the next call
                # to recv(), mbedTLS starts a fresh record read -- but the socket
                # still has the payload bytes from the previous record sitting in
                # the TCP buffer. mbedTLS now misinterprets that payload as the
                # start of a new TLS record header, corrupting the TLS stream for
                # all future reads. In practice this means the Pico connects,
                # sends auth, but never receives auth_ok -- or any other message --
                # because every incoming TLS record gets silently discarded.
                #
                # Fix: keep the socket fully blocking so mbedTLS can always finish
                # reading a complete TLS record in one go. Non-blocking behaviour
                # for the main loop is achieved by calling select() with a short
                # timeout BEFORE recv() -- see recv() below.
                self._raw_sock.settimeout(None)

                print("[ws] Connected!")
                return True

            if status_code in (301, 302, 307, 308):
                # 301/308 = permanent, 302/307 = temporary. We follow either
                # the same way; the only difference would be whether to
                # persist the new URL to secrets.json, which we deliberately
                # don't do here -- silently rewriting flash on the basis of
                # one server response is too easy to abuse. If you see this
                # log line repeatedly, re-provision with the new URL.
                location = self._parse_location_header(response)
                self._close_socket()
                if location:
                    print(
                        "[ws] Got",
                        status_code,
                        "redirect (",
                        "permanent" if status_code in (301, 308) else "temporary",
                        ") to:",
                        location,
                    )
                    return location
                print(
                    "[ws] Got",
                    status_code,
                    "redirect but no Location header; giving up",
                )
                return False

            print("[ws] Handshake failed:", response_line)
            self._close_socket()
            return False

        except Exception as e:
            print("[ws] Connection failed:", e)
            self._close_socket()
            return False

    @staticmethod
    def _parse_status_code(response_line):
        """
        Extract the integer status code from "HTTP/1.1 <code> <reason>".
        Returns None if the line is malformed.
        """
        try:
            parts = response_line.split(b" ", 2)
            if len(parts) < 2:
                return None
            return int(parts[1])
        except (ValueError, IndexError):
            return None

    def _parse_location_header(self, response):
        """
        Pull the value of the Location header out of a raw HTTP response.
        The header name is case-insensitive per RFC 7230, so we lowercase
        before comparing. Handles relative-path Location values by
        resolving them against the current host/scheme.
        """
        try:
            text = response.decode("utf-8", "replace")
        except Exception:
            return None
        for line in text.split("\r\n"):
            # Skip the status line and the empty line before the body.
            if not line or ":" not in line:
                continue
            name, _, value = line.partition(":")
            if name.strip().lower() == "location":
                target = value.strip()
                if not target:
                    return None
                # Absolute URL -> return as-is.
                if "://" in target:
                    return target
                # Relative URL. Build an absolute one against the current
                # connection so the next attempt has a complete target.
                scheme = "wss" if self._use_ssl else "ws"
                if target.startswith("/"):
                    return "{0}://{1}:{2}{3}".format(
                        scheme, self._host, self._port, target
                    )
                # Path-relative (rare). Reuse the directory of self._path.
                base = self._path.rsplit("/", 1)[0] + "/"
                return "{0}://{1}:{2}{3}{4}".format(
                    scheme, self._host, self._port, base, target
                )
        return None

    @staticmethod
    def _normalize_redirect_target(url):
        """
        Servers commonly redirect to http:// or https:// even though the
        original request was a WebSocket upgrade. Promote those schemes to
        ws:// / wss:// so _parse_url picks the right port and TLS flag.
        """
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url

    def send(self, data):
        """
        Send a message through the WebSocket.

        Parameters:
            data: Either a string or a dict. Dicts are JSON-encoded automatically.

        Returns True if sent successfully, False on error.

        HOW WEBSOCKET FRAMES WORK (sending):
        We construct a frame with:
        - Byte 0: 0x81 = FIN bit set (this is a complete message) + opcode 0x1 (text)
        - Byte 1: 0x80 | length = MASK bit set (client MUST mask) + payload length
        - If length > 125: additional length bytes
        - 4 bytes: random masking key
        - Payload: each byte XOR'd with the masking key

        WHY MASKING?
        The WebSocket spec requires all client-to-server frames to be masked
        with a random key. This prevents a class of attacks where a malicious
        WebSocket client could trick caching proxies into storing fake HTTP
        responses. It's a security measure, not encryption.
        """
        if not self._connected or not self._sock:
            return False

        try:
            # Convert dict to JSON string if needed.
            if isinstance(data, dict):
                data = json.dumps(data)
            if isinstance(data, str):
                data = data.encode("utf-8")

            # Build the WebSocket frame.
            # First byte: FIN + opcode.
            # 0x80 = FIN bit (this is the final/only fragment)
            # 0x01 = text frame opcode
            header = bytearray()
            header.append(0x81)  # FIN + text opcode

            # Second byte: MASK bit + payload length.
            # 0x80 = MASK bit (client frames must be masked)
            length = len(data)
            if length < 126:
                header.append(0x80 | length)
            elif length < 65536:
                header.append(0x80 | 126)
                # Pack length as 2-byte big-endian unsigned short.
                header += struct.pack("!H", length)
            else:
                header.append(0x80 | 127)
                # Pack length as 8-byte big-endian unsigned long long.
                header += struct.pack("!Q", length)

            # Generate a random 4-byte masking key.
            mask = os.urandom(4)
            header += mask

            # Apply the mask to the payload.
            # Each byte is XOR'd with mask[i % 4].
            # XOR is reversible: applying the same mask again recovers the original.
            masked_data = bytearray(len(data))
            for i in range(len(data)):
                masked_data[i] = data[i] ^ mask[i % 4]

            # Send header + masked payload.
            self._sock.send(bytes(header) + bytes(masked_data))
            msg_type = ""
            if isinstance(data, (bytes, bytearray)):
                try:
                    msg_type = json.loads(data).get("type", "?")
                except Exception:
                    pass
            print("[ws] Sent:", msg_type, "(", length, "bytes )")
            return True

        except Exception as e:
            print("[ws] Send error:", e)
            self._connected = False
            return False

    def recv(self):
        """
        Receive a message from the WebSocket (non-blocking).

        Returns:
            A decoded message (string or dict) if one is available,
            None if no message is waiting.

        NON-BLOCKING BEHAVIOR:
        Because we set a short socket timeout (0.1s), this function will
        return quickly even if no data is available. This is important
        because our main loop needs to do other things (send heartbeats,
        feed the watchdog, check LED patterns, etc.).

        In regular Python, you might use asyncio for this. MicroPython has
        `uasyncio`, but for simplicity, we use timeout-based polling.

        RECEIVING FRAMES:
        Server-to-client frames are NOT masked (per the WebSocket spec).
        We read:
        - Byte 0: FIN bit + opcode
        - Byte 1: payload length (no mask bit)
        - If length == 126: next 2 bytes are the real length
        - If length == 127: next 8 bytes are the real length
        - Then: the raw payload data
        """
        if not self._connected or not self._sock:
            return None

        try:
            # Poll for incoming data with a short timeout before blocking recv.
            #
            # The socket is kept in blocking mode (see the settimeout(None) in
            # _handshake_once for the full explanation). That means a bare
            # self._sock.recv() call would stall the main loop indefinitely
            # whenever the server has nothing to send. select() lets us ask
            # "is there data ready right now?" and bail out quickly if not,
            # keeping the main loop responsive without putting the socket into
            # non-blocking mode (which would break mbedTLS as described above).
            try:
                r, _, _ = select.select([self._sock], [], [], 0.05)
                if not r:
                    return None  # No data in 50ms -- yield back to main loop.
            except Exception:
                pass  # select unavailable; fall through to blocking recv

            # Try to read frame header (at least 2 bytes).
            header = self._sock.recv(2)
            if not header:
                # Empty bytes from recv() means the server closed the TCP connection.
                print("[ws] Connection closed by server (recv returned empty)")
                self._connected = False
                return None
            if len(header) < 2:
                return None

            # Parse the first byte.
            # Bit 7 (0x80): FIN flag (1 = final fragment, we only support this)
            # Bits 0-3 (0x0F): Opcode
            opcode = header[0] & 0x0F

            # Parse the second byte.
            # Bit 7 (0x80): Mask flag (should be 0 for server-to-client)
            # Bits 0-6 (0x7F): Payload length
            is_masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F

            # Extended length handling.
            if length == 126:
                # Next 2 bytes contain the real length (16-bit big-endian).
                ext = self._recv_exact(2)
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                # Next 8 bytes contain the real length (64-bit big-endian).
                ext = self._recv_exact(8)
                length = struct.unpack("!Q", ext)[0]

            # Read masking key if present (shouldn't be for server frames).
            mask = None
            if is_masked:
                mask = self._recv_exact(4)

            # Read the payload.
            payload = self._recv_exact(length) if length > 0 else b""

            # Unmask the payload if needed.
            if mask and payload:
                payload = bytearray(payload)
                for i in range(len(payload)):
                    payload[i] ^= mask[i % 4]
                payload = bytes(payload)

            # Handle different opcodes.
            if opcode == 0x1:
                # Text frame -- this is our JSON message.
                text = payload.decode("utf-8")
                try:
                    # Try to parse as JSON.
                    return json.loads(text)
                except (ValueError, KeyError):
                    # Not valid JSON, return as plain string.
                    return text

            elif opcode == 0x2:
                # Binary frame -- return raw bytes.
                return payload

            elif opcode == 0x8:
                # Close frame -- server is closing the connection.
                print("[ws] Received close frame")
                self._connected = False
                self._close_socket()
                return None

            elif opcode == 0x9:
                # Ping frame -- server is checking if we're alive.
                # We MUST respond with a pong containing the same payload.
                self._send_pong(payload)
                return None

            elif opcode == 0xA:
                # Pong frame -- response to our ping.
                self._last_pong_time = time.ticks_ms()
                return None

            else:
                # Unknown opcode, ignore.
                return None

        except OSError as e:
            # errno 110 = ETIMEDOUT, 11 = EAGAIN -- no data yet, normal for non-blocking.
            # Any other OSError (ECONNRESET, EPIPE, etc.) means the connection is dead.
            if e.args[0] in (errno.ETIMEDOUT, errno.EAGAIN, 11, 110):
                return None
            print("[ws] Recv error (connection lost):", e)
            self._connected = False
            return None
        except Exception as e:
            print("[ws] Recv error:", e)
            self._connected = False
            return None

    def _recv_exact(self, num_bytes):
        """
        Read exactly `num_bytes` from the socket.

        Socket.recv() might return fewer bytes than requested (this is normal
        for TCP -- data arrives in chunks). This helper keeps reading until
        we have exactly the number of bytes we need.
        """
        data = b""
        while len(data) < num_bytes:
            chunk = self._sock.recv(num_bytes - len(data))
            if not chunk:
                raise OSError("Connection closed")
            data += chunk
        return data

    def _send_pong(self, payload):
        """
        Send a pong frame in response to a server ping.

        PING/PONG:
        The WebSocket protocol has built-in keepalive. The server sends a
        "ping" frame, and we must respond with a "pong" frame containing
        the same payload. This lets the server verify the connection is alive.

        If we don't respond to pings, the server may close the connection.
        """
        try:
            # Pong frame: FIN + opcode 0xA, masked, with the same payload.
            header = bytearray([0x8A])  # FIN + pong opcode

            length = len(payload)
            if length < 126:
                header.append(0x80 | length)
            else:
                header.append(0x80 | 126)
                header += struct.pack("!H", length)

            mask = os.urandom(4)
            header += mask

            masked = bytearray(len(payload))
            for i in range(len(payload)):
                masked[i] = payload[i] ^ mask[i % 4]

            self._sock.send(bytes(header) + bytes(masked))
        except Exception:
            pass

    def send_ping(self):
        """
        Send a ping frame to the server.

        We do this periodically to:
        1. Keep the connection alive (some routers/firewalls drop idle connections)
        2. Detect dead connections (if we don't get a pong back, connection is dead)
        """
        if not self._connected or not self._sock:
            return

        try:
            # Ping frame: FIN + opcode 0x9, masked, empty payload.
            mask = os.urandom(4)
            frame = bytearray([0x89, 0x80]) + mask  # FIN + ping, masked, 0 length
            self._sock.send(bytes(frame))
            self._last_ping_time = time.ticks_ms()
        except Exception as e:
            print("[ws] Ping error:", e)
            self._connected = False

    def check_heartbeat(self):
        """
        Check if it's time to send a ping and if the last pong arrived.

        Call this in your main loop. It handles:
        1. Sending a ping every `_ping_interval` seconds
        2. Detecting if the server didn't respond to our last ping

        Returns True if connection seems healthy, False if it appears dead.

        EXPONENTIAL BACKOFF:
        When the connection dies, we don't retry immediately. Instead:
        - 1st retry: wait 1 second
        - 2nd retry: wait 2 seconds
        - 3rd retry: wait 4 seconds
        - 4th retry: wait 8 seconds
        - ...up to 60 seconds max

        This prevents overwhelming the server if it's under load or restarting.
        """
        # WS-level ping/pong is disabled: Cloudflare kills the connection when
        # it sees client-initiated ping frames (opcode 0x9). Dead connections
        # are detected by recv() returning empty bytes or send() failing on
        # the application-level heartbeat.
        return True

    def get_reconnect_delay(self):
        """
        Get the current reconnection delay (with exponential backoff).

        Each failed attempt doubles the delay, up to _max_reconnect_delay.

        Returns the number of seconds to wait before the next reconnection attempt.
        """
        delay = self._reconnect_delay
        # Increase the delay for next time (exponential backoff).
        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self._max_reconnect_delay,
        )
        self._reconnect_attempts += 1
        return delay

    def is_connected(self):
        """Check if the WebSocket connection is active."""
        return self._connected

    def close(self):
        """
        Gracefully close the WebSocket connection.

        Sends a close frame to the server before disconnecting.
        The close frame is opcode 0x8.
        """
        if self._sock:
            try:
                # Send a close frame.
                mask = os.urandom(4)
                frame = bytearray([0x88, 0x80]) + mask
                self._sock.send(bytes(frame))
            except Exception:
                pass
            self._close_socket()
        self._connected = False

    def _close_socket(self):
        """Close the underlying TCP socket."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._raw_sock = None
