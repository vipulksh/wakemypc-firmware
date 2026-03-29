"""
tcp_relay.py - TCP Relay for SSH Tunneling Through WebSocket
=============================================================

THE PROBLEM THIS SOLVES:
------------------------
Your Django server needs to SSH into computers on the Pico's local network
(e.g., to run commands, manage the machine). But the server is on the internet
and the target computers are behind a home router with NAT (Network Address
Translation). The server CAN'T reach them directly.

             INTERNET                    HOME NETWORK
    [Django Server] ---X--->  [Router/NAT]  [Desktop PC]
                                             [NAS]
                                             [Pico W] <-- connected via WiFi

The server CAN talk to the Pico (via WebSocket, because the Pico initiated
the connection outward). And the Pico CAN talk to the Desktop (they're on
the same local network). So the Pico acts as a RELAY:

    [Server] <--WebSocket--> [Pico] <--TCP--> [Desktop:22 (SSH)]

HOW THE RELAY WORKS:
--------------------
1. Server sends a "tcp_relay_open" command with the target IP and port.
2. Pico opens a raw TCP socket to the target (e.g., 192.168.1.10:22).
3. The server sends data (base64-encoded) through the WebSocket.
4. Pico decodes the data and forwards it to the TCP socket.
5. Pico reads data from the TCP socket, base64-encodes it, sends it back.
6. The server decodes it -- it's like the server has a direct TCP connection!

WHY BASE64?
WebSocket text frames are UTF-8 encoded. SSH traffic is raw binary that may
contain bytes that aren't valid UTF-8. Base64 encodes binary data as ASCII
text (using only A-Z, a-z, 0-9, +, /), making it safe to send through
text-based protocols. The downside is ~33% size overhead.

WHAT IS BASE64?
Base64 takes groups of 3 bytes (24 bits) and splits them into 4 groups of
6 bits. Each 6-bit value (0-63) maps to one of 64 safe ASCII characters.
"Hello" in bytes -> base64 -> "SGVsbG8="

THE PICO DOESN'T UNDERSTAND SSH:
The Pico never looks at or interprets the SSH data. It just forwards raw bytes
back and forth like a dumb pipe. The SSH protocol (authentication, encryption,
commands) happens between the server and the target PC. The Pico is just the
middleman. This is similar to how a VPN or SSH tunnel works.

ABOUT RAW TCP SOCKETS:
A "raw" TCP socket in this context means we're not using any application
protocol on top of TCP (no HTTP, no WebSocket, no TLS wrapping). We just
open a TCP connection and send/receive bytes. This is the lowest level of
network communication available to us.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
import socket

# base64 encoding for binary data through the text WebSocket.
try:
    import ubinascii as binascii
except ImportError:
    import binascii

try:
    import ujson as json
except ImportError:
    pass


# -------------------------------------------------------------------------
# TCP Relay
# -------------------------------------------------------------------------
class TCPRelay:
    """
    Manages TCP relay sessions between the WebSocket and local network devices.

    Each relay session has:
    - A session_id (to distinguish multiple simultaneous relays)
    - A TCP socket connected to the target device
    - A reference to the WebSocket client (for sending data back to the server)

    MULTIPLE SESSIONS:
    The server might want to SSH into multiple devices simultaneously, or
    have multiple SSH sessions to the same device. Each gets its own
    session_id and TCP socket.

    MEMORY CONSIDERATIONS:
    Each TCP socket and its buffers consume RAM. The Pico has ~250KB of
    usable RAM, so we limit the number of concurrent sessions and buffer sizes.

    Usage:
        relay = TCPRelay(ws_client)
        relay.open_session("sess-1", "192.168.1.10", 22)  # Open connection
        relay.send_data("sess-1", base64_encoded_data)      # Forward to target
        data = relay.poll_data("sess-1")                     # Read from target
        relay.close_session("sess-1")                        # Clean up
    """

    # Maximum number of concurrent relay sessions.
    # Limited by RAM -- each session uses ~2KB for buffers.
    MAX_SESSIONS = 4

    # Read buffer size for TCP socket reads.
    # SSH packets are typically small (a few hundred bytes), but file
    # transfers can be larger. 1024 bytes is a good balance.
    READ_BUFFER_SIZE = 1024

    def __init__(self, ws_client):
        """
        Parameters:
            ws_client: The WebSocketClient instance for sending data to the server.
        """
        self._ws = ws_client

        # Active sessions: {session_id: socket_object}
        self._sessions = {}

    def open_session(self, session_id, host, port, timeout=5):
        """
        Open a new TCP relay session to a target device.

        Parameters:
            session_id: Unique identifier for this session (string)
            host:       Target IP address (e.g., "192.168.1.10")
            port:       Target TCP port (e.g., 22 for SSH)
            timeout:    Connection timeout in seconds

        Returns True if the connection was established, False otherwise.

        WHAT HAPPENS:
        1. We create a TCP socket
        2. Connect it to the target host:port
        3. Set a short timeout for non-blocking reads
        4. Store the socket for future send/receive operations
        """
        # Check session limit.
        if len(self._sessions) >= self.MAX_SESSIONS:
            print("[relay] Max sessions reached (", self.MAX_SESSIONS, ")")
            return False

        # Check for duplicate session ID.
        if session_id in self._sessions:
            print("[relay] Session already exists:", session_id)
            return False

        try:
            print("[relay] Opening session", session_id, "to", host, ":", port)

            # Create and connect the TCP socket.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))

            # Set a short timeout for non-blocking reads in the poll loop.
            # 0.05 seconds (50ms) means poll_data() returns quickly even
            # if no data is available, so the main loop isn't blocked.
            sock.settimeout(0.05)

            # Store the session.
            self._sessions[session_id] = sock

            print("[relay] Session", session_id, "connected")
            return True

        except Exception as e:
            print("[relay] Failed to open session", session_id, ":", e)
            return False

    def close_session(self, session_id):
        """
        Close a TCP relay session and clean up.

        Parameters:
            session_id: The session to close.

        Always call this when the server is done with a relay, or when
        an error occurs. Leaving sockets open wastes resources and can
        cause "address already in use" errors.
        """
        sock = self._sessions.pop(session_id, None)
        if sock:
            try:
                sock.close()
            except Exception:
                pass
            print("[relay] Session", session_id, "closed")

    def close_all(self):
        """Close all active relay sessions."""
        for session_id in list(self._sessions.keys()):
            self.close_session(session_id)

    def send_data(self, session_id, b64_data):
        """
        Forward base64-encoded data from the server to the target device.

        Parameters:
            session_id: Which relay session to send through
            b64_data:   Base64-encoded string of the data to send

        Returns True if sent successfully, False on error.

        DATA FLOW:
        Server -> [base64 encode] -> WebSocket -> Pico -> [base64 decode] -> TCP -> Target

        HOW BASE64 DECODING WORKS:
        binascii.a2b_base64() converts base64 text back to raw bytes.
        "SGVsbG8=" -> b"Hello"
        """
        sock = self._sessions.get(session_id)
        if not sock:
            print("[relay] Unknown session:", session_id)
            return False

        try:
            # Decode from base64 to raw bytes.
            raw_data = binascii.a2b_base64(b64_data)

            # Send the raw bytes through the TCP socket to the target device.
            # socket.send() might not send all bytes at once (especially for
            # large data). sendall() would be better but isn't always available
            # in MicroPython, so we loop.
            total_sent = 0
            while total_sent < len(raw_data):
                sent = sock.send(raw_data[total_sent:])
                if sent == 0:
                    raise OSError("Connection closed")
                total_sent += sent

            return True

        except Exception as e:
            print("[relay] Send error on session", session_id, ":", e)
            # Close the broken session.
            self.close_session(session_id)
            return False

    def poll_data(self, session_id):
        """
        Check if the target device has sent any data, and forward it to the server.

        Parameters:
            session_id: Which relay session to poll

        Returns the data as a base64 string, or None if no data available.

        DATA FLOW:
        Target -> TCP -> Pico -> [base64 encode] -> WebSocket -> Server

        This is NON-BLOCKING: if no data is available, it returns None
        immediately (thanks to the short socket timeout we set).
        """
        sock = self._sessions.get(session_id)
        if not sock:
            return None

        try:
            # Try to read data from the TCP socket.
            # With a 50ms timeout, this returns quickly if no data is available.
            data = sock.recv(self.READ_BUFFER_SIZE)

            if not data:
                # Empty read = connection closed by the target.
                print("[relay] Target closed connection for session", session_id)
                self.close_session(session_id)
                return None

            # Encode as base64 for safe transport through the WebSocket.
            # binascii.b2a_base64() adds a trailing newline; strip() removes it.
            b64_data = binascii.b2a_base64(data).strip()
            return b64_data.decode("ascii")

        except OSError:
            # Timeout = no data available. This is normal and expected.
            return None
        except Exception as e:
            print("[relay] Poll error on session", session_id, ":", e)
            self.close_session(session_id)
            return None

    def poll_all(self):
        """
        Poll all active sessions for incoming data.

        Returns a list of (session_id, b64_data) tuples for sessions
        that have data available.

        Call this in the main loop to check all relay sessions.
        """
        results = []
        # Iterate over a copy of keys since poll_data might close sessions.
        for session_id in list(self._sessions.keys()):
            data = self.poll_data(session_id)
            if data:
                results.append((session_id, data))
        return results

    def get_active_sessions(self):
        """Return a list of active session IDs."""
        return list(self._sessions.keys())


# -------------------------------------------------------------------------
# Protocol Handlers
# -------------------------------------------------------------------------


def handle_tcp_relay_open(message, proto):
    """
    Handle request to open a new TCP relay session.

    Expected message:
        {
            "type": "tcp_relay_open",
            "session_id": "sess-abc123",
            "host": "192.168.1.10",
            "port": 22
        }

    Response:
        {
            "type": "tcp_relay_opened",
            "session_id": "sess-abc123",
            "success": true/false
        }
    """
    session_id = message.get("session_id")
    host = message.get("host")
    port = message.get("port", 22)

    if not session_id or not host:
        proto.send_response(
            "tcp_relay_opened",
            {
                "success": False,
                "message": "Missing session_id or host",
            },
        )
        return

    # Get or create the relay instance.
    # We store it on the proto object so it persists across messages.
    if not hasattr(proto, "_tcp_relay"):
        proto._tcp_relay = TCPRelay(proto._ws)

    success = proto._tcp_relay.open_session(session_id, host, port)
    proto.send_response(
        "tcp_relay_opened",
        {
            "session_id": session_id,
            "success": success,
            "host": host,
            "port": port,
        },
    )


def handle_tcp_relay_data(message, proto):
    """
    Handle data forwarding through a TCP relay session.

    Expected message:
        {
            "type": "tcp_relay_data",
            "session_id": "sess-abc123",
            "data": "base64encodeddata..."
        }

    No response is sent for data messages (to reduce overhead).
    Errors will trigger a tcp_relay_closed message.
    """
    session_id = message.get("session_id")
    data = message.get("data")

    if not hasattr(proto, "_tcp_relay"):
        return

    if not session_id or not data:
        return

    success = proto._tcp_relay.send_data(session_id, data)
    if not success:
        proto.send_response(
            "tcp_relay_closed",
            {
                "session_id": session_id,
                "reason": "send_failed",
            },
        )


def handle_tcp_relay_close(message, proto):
    """
    Handle request to close a TCP relay session.

    Expected message:
        {"type": "tcp_relay_close", "session_id": "sess-abc123"}

    Response:
        {"type": "tcp_relay_closed", "session_id": "sess-abc123"}
    """
    session_id = message.get("session_id")

    if hasattr(proto, "_tcp_relay") and session_id:
        proto._tcp_relay.close_session(session_id)

    proto.send_response(
        "tcp_relay_closed",
        {
            "session_id": session_id,
            "reason": "requested",
        },
    )
