"""
protocol.py - Message Dispatch / Protocol Handler
===================================================

HOW THE PICO COMMUNICATES WITH THE SERVER:
------------------------------------------
All communication between the Pico and the Django server happens over WebSocket
using JSON messages. Every message has a "type" field that identifies what kind
of message it is. Think of it like Django URL routing, but for WebSocket messages:

    URL routing:     /api/devices/  ->  views.device_list
    Message routing: {"type": "wol"} ->  handle_wol()

MESSAGE FLOW:
-------------
Server -> Pico (commands):
    {"type": "wol", "mac": "AA:BB:CC:DD:EE:FF"}          # Wake a computer
    {"type": "scan", "targets": [...]}                     # Check device status
    {"type": "tcp_relay_open", "host": "192.168.1.10", ...}  # Open TCP relay
    {"type": "identify"}                                    # Blink LED rapidly
    {"type": "ota_update", "files": [...]}                  # Update firmware
    {"type": "ping"}                                        # Keepalive
    {"type": "reboot"}                                      # Reboot the Pico
    {"type": "config_update", "config": {...}}              # Update configuration

Pico -> Server (responses):
    {"type": "auth", "token": "...", "device_id": "..."}   # Authentication
    {"type": "heartbeat", "uptime": 12345, ...}            # Periodic heartbeat
    {"type": "wol_result", "success": true, ...}           # Command result
    {"type": "scan_result", "devices": [...]}              # Scan results
    {"type": "error", "message": "..."}                    # Error report

THE DISPATCHER PATTERN:
-----------------------
Instead of a big if/elif chain:
    if msg_type == "wol": ...
    elif msg_type == "scan": ...
    elif msg_type == "identify": ...

We use a dict that maps message types to handler functions:
    handlers = {"wol": handle_wol, "scan": handle_scan, ...}
    handlers[msg_type](message)

This is cleaner, easier to extend, and is the same pattern used by Django's
URL dispatcher and DRF's ViewSet routing.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
import time

# We need json for building response messages.
try:
    import ujson as json
except ImportError:
    pass

# `gc` is the garbage collector module. MicroPython has very limited RAM
# (about 250KB usable on the Pico), so we sometimes need to manually
# trigger garbage collection to free memory. In regular Python, you almost
# never need to do this because RAM is plentiful.
import gc


# -------------------------------------------------------------------------
# Protocol Handler
# -------------------------------------------------------------------------
class ProtocolHandler:
    """
    Routes incoming WebSocket messages to the appropriate handler function.

    This is the "brain" of the Pico firmware -- it receives messages from
    the server and decides what to do with them.

    Usage:
        proto = ProtocolHandler(ws_client, config)
        proto.register("wol", wol_handler.handle)
        proto.register("scan", scanner.handle)

        # In main loop:
        msg = ws.recv()
        if msg:
            proto.dispatch(msg)

    DESIGN PHILOSOPHY:
    Each handler function receives the full message dict and the ProtocolHandler
    instance (so it can send responses). Handlers are registered by the main.py
    boot sequence, keeping this module generic and reusable.
    """

    def __init__(self, ws_client, config):
        """
        Parameters:
            ws_client: The WebSocketClient instance (for sending responses)
            config:    The Config instance (handlers may need config values)
        """
        self._ws = ws_client
        self._config = config

        # The handler registry: maps message type strings to handler functions.
        # A handler function signature is: handler(message_dict, protocol_handler)
        self._handlers = {}

        # Track the last message time (for debugging and heartbeat info).
        self._last_message_time = 0

        # Message counter (for debugging -- how many messages have we processed?).
        self._message_count = 0

        # Server-assigned identity + monitoring list, populated by the auth_ok
        # message right after the WebSocket handshake. Other modules (e.g. a
        # device pinger) can read these once auth completes. They stay on the
        # ProtocolHandler so reconnect logic can re-fetch them cleanly.
        self.pico_id = None
        self.assigned_devices = []
        # Optional callback fired the moment auth_ok lands. Lets other modules
        # react (start the device-status loop, light an LED green, etc.)
        # without coupling them to this class.
        self._on_auth_ok = None

        # Register built-in handlers that don't depend on external modules.
        self._register_builtins()

    def _register_builtins(self):
        """
        Register handlers for basic message types that are handled internally.

        These don't require external modules (wol, scanner, etc.) -- they're
        simple enough to handle right here.
        """
        # "ping" -- server is checking if we're alive.
        # Different from WebSocket-level ping/pong: this is an application-level
        # ping that goes through our JSON message protocol.
        self._handlers["ping"] = self._handle_ping

        # "config_update" -- server is pushing new configuration.
        self._handlers["config_update"] = self._handle_config_update

        # "reboot" -- server is asking us to restart.
        self._handlers["reboot"] = self._handle_reboot

        # "get_status" -- server wants our current status.
        self._handlers["get_status"] = self._handle_get_status

        # "auth_ok" -- server confirms the WebSocket handshake succeeded and
        # tells us our pico_id + which devices we should monitor. Stored so
        # other modules can read self.pico_id / self.assigned_devices.
        self._handlers["auth_ok"] = self._handle_auth_ok

        # "pong" -- server's reply to our heartbeat. We don't need to act on
        # it -- the timestamp already got bumped in dispatch() before we got
        # here -- but we still need a registered handler so dispatch() doesn't
        # bounce an "error: unknown type" message back to the server every
        # 30 seconds.
        self._handlers["pong"] = self._handle_pong

    def register(self, message_type, handler_func):
        """
        Register a handler function for a message type.

        Parameters:
            message_type: String, e.g., "wol", "scan", "tcp_relay_open"
            handler_func: A callable that takes (message_dict, protocol_handler)

        Example:
            # In wol.py:
            def handle_wol(message, proto):
                mac = message.get("mac")
                success = send_magic_packet(mac)
                proto.send_response("wol_result", {"success": success, "mac": mac})

            # In main.py:
            proto.register("wol", handle_wol)

        WHY PASS proto TO HANDLERS?
        Handlers need to send responses back to the server. Rather than giving
        them direct access to the WebSocket client, we pass `proto` which has
        a send_response() helper. This keeps the interface clean and lets us
        add logging, error handling, etc. in one place.
        """
        self._handlers[message_type] = handler_func
        print("[proto] Registered handler for:", message_type)

    def dispatch(self, message):
        """
        Route an incoming message to its handler.

        Parameters:
            message: A dict (parsed JSON) with at least a "type" field.

        Returns True if the message was handled, False if not.

        ERROR HANDLING:
        If a handler raises an exception, we catch it and send an error
        response to the server. This prevents one bad message from crashing
        the entire firmware. Defense in depth!
        """
        if not isinstance(message, dict):
            print("[proto] Ignoring non-dict message:", type(message))
            return False

        msg_type = message.get("type")
        if not msg_type:
            print("[proto] Message has no 'type' field:", message)
            return False

        self._last_message_time = time.ticks_ms()
        self._message_count += 1

        # Look up the handler.
        handler = self._handlers.get(msg_type)
        if not handler:
            print("[proto] No handler for message type:", msg_type)
            self.send_response(
                "error",
                {
                    "message": "Unknown message type: " + msg_type,
                    "original_type": msg_type,
                },
            )
            return False

        # Call the handler, catching any exceptions.
        try:
            print("[proto] Handling:", msg_type)
            handler(message, self)

            # After handling a message, run garbage collection.
            # MicroPython's heap is tiny (~250KB), and message handling
            # creates temporary objects. gc.collect() frees unreferenced memory.
            #
            # ABOUT GARBAGE COLLECTION:
            # Python uses reference counting + a cycle collector for memory
            # management. MicroPython is the same, but with very limited heap.
            # gc.collect() forces the cycle collector to run NOW instead of
            # waiting. gc.mem_free() tells you how much heap is available.
            gc.collect()
            return True

        except Exception as e:
            # Something went wrong in the handler.
            # Log it, send an error response, and keep running.
            print("[proto] Error handling", msg_type, ":", e)
            self.send_response(
                "error",
                {
                    "message": str(e),
                    "original_type": msg_type,
                },
            )
            return False

    def send_response(self, msg_type, data=None):
        """
        Send a response message to the server.

        Parameters:
            msg_type: The "type" field for the response (e.g., "wol_result")
            data:     Optional dict of additional fields to include

        The message is JSON-encoded and sent through the WebSocket.

        Every response includes:
        - type: The response type
        - device_id: So the server knows which Pico sent this
        - timestamp: When the response was generated (monotonic ticks)
        - Plus any additional fields from `data`
        """
        message = {
            "type": msg_type,
            "device_id": self._config.get("device_id", "unknown"),
            "timestamp": time.ticks_ms(),
        }

        # Merge in additional data.
        if data and isinstance(data, dict):
            message.update(data)

        success = self._ws.send(message)
        if not success:
            print("[proto] Failed to send response:", msg_type)
        return success

    def send_heartbeat(self, wifi_info=None, health=None):
        """
        Send a periodic heartbeat to the server.

        The heartbeat tells the server:
        - The Pico is alive and running
        - How long it's been running (uptime)
        - WiFi signal strength
        - Memory usage
        - How many messages it's processed
        - Health metrics (RAM, RSSI, uptime, reconnect count)

        The server uses this to:
        - Mark the device as "online" in the dashboard
        - Monitor health metrics on the transmitter detail page
        - Detect if the Pico has rebooted (uptime resets)

        Parameters:
            wifi_info: Dict from WiFiManager.get_info() (SSID, IP, RSSI, etc.)
            health:    Dict of health metrics from main.py (free_ram, total_ram,
                       wifi_rssi, uptime_seconds, reconnect_count). These are
                       stored in Redis cache on the server and displayed on the
                       transmitter health dashboard.
        """
        # gc.mem_free() returns the number of free bytes on the heap.
        # gc.mem_alloc() returns the number of allocated bytes.
        # Together they tell us total heap size and usage percentage.
        mem_free = gc.mem_free()
        mem_alloc = gc.mem_alloc()

        heartbeat_data = {
            "uptime_ms": time.ticks_ms(),
            "mem_free": mem_free,
            "mem_alloc": mem_alloc,
            "mem_total": mem_free + mem_alloc,
            "messages_handled": self._message_count,
        }

        # Include WiFi info if provided.
        if wifi_info:
            heartbeat_data["wifi"] = wifi_info

        # Include health metrics if provided.
        # The server consumer stores these in Redis cache so the frontend
        # can display a health dashboard with RAM usage, WiFi signal quality,
        # uptime, and reconnection count.
        if health:
            heartbeat_data["health"] = health

        self.send_response("heartbeat", heartbeat_data)

    # =====================================================================
    # Built-in Handlers
    # =====================================================================

    def _handle_ping(self, message, proto):
        """
        Handle application-level ping.

        Server sends: {"type": "ping"}
        We respond:   {"type": "pong"}

        This is different from WebSocket-level ping/pong frames. This is
        an application-layer ping that goes through our JSON protocol.
        Some servers use this for latency measurement.
        """
        proto.send_response("pong")

    def _handle_auth_ok(self, message, proto):
        """
        Server confirms successful WebSocket auth and hands us our identity
        and monitoring list.

        Server sends:
            {
              "type": "auth_ok",
              "pico_id": "qTgwH",            # our public_id on the server
              "assigned_devices": [...]      # list of {public_id, mac, ip} dicts
                                             # this Pico is responsible for pinging
            }

        We:
          - Stash both on self so other modules (a device pinger, an LED
            controller, etc.) can read them.
          - Fire the optional on_auth_ok callback registered by main.py.
          - Print a friendly log line for serial-console debugging.

        We don't reply -- the server isn't waiting for an ack.
        """
        self.pico_id = message.get("pico_id")
        devices = message.get("assigned_devices") or []
        self.assigned_devices = devices

        print(
            "[proto] auth_ok received -- pico_id=",
            self.pico_id,
            " devices=",
            len(devices),
            sep="",
        )

        if self._on_auth_ok is not None:
            try:
                self._on_auth_ok(self.pico_id, devices)
            except Exception as exc:
                # Don't let a buggy callback crash the firmware; just log.
                print("[proto] on_auth_ok callback raised:", exc)

    def _handle_pong(self, message, proto):
        """
        Server's reply to one of our heartbeats. Nothing to do -- the
        liveness timestamp was already bumped in dispatch() before we got
        called. Existence of this handler matters only because it stops
        dispatch() from bouncing back an "error: unknown type" message
        every 30 seconds.
        """
        # No-op. Intentional.
        pass

    def set_on_auth_ok(self, callback):
        """
        Register a callable invoked the moment auth_ok arrives.

        Signature: callback(pico_id: str, assigned_devices: list[dict])

        Use case: main.py can wire up a device-pinger module here so it
        only starts polling once the server has told us what to monitor.
        """
        self._on_auth_ok = callback

    def _handle_config_update(self, message, proto):
        """
        Handle remote configuration update.

        Server sends: {"type": "config_update", "config": {"key": "value", ...}}
        We update our local config and save to flash.

        This lets the server remotely change settings like:
        - WiFi networks
        - Heartbeat interval
        - Scan interval
        - etc.

        SECURITY NOTE: We don't allow changing the device_token or device_id
        remotely -- those are set during provisioning only.
        """
        new_config = message.get("config", {})

        if not isinstance(new_config, dict):
            proto.send_response("error", {"message": "Invalid config format"})
            return

        # Protected fields that can't be changed remotely.
        protected = {"device_token", "device_id"}

        updated_keys = []
        for key, value in new_config.items():
            if key in protected:
                print("[proto] Ignoring protected config key:", key)
                continue
            self._config.set(key, value)
            updated_keys.append(key)

        if updated_keys:
            self._config.save()
            print("[proto] Updated config keys:", updated_keys)

        proto.send_response(
            "config_update_result",
            {
                "success": True,
                "updated_keys": updated_keys,
            },
        )

    def _handle_reboot(self, message, proto):
        """
        Handle reboot request.

        Server sends: {"type": "reboot"}
        We acknowledge, then reboot the Pico.

        ABOUT machine.reset():
        This performs a hard reset of the Pico -- equivalent to unplugging
        and re-plugging it. All RAM is cleared, all connections are dropped,
        and boot.py + main.py run again from scratch.
        """
        import machine

        # Send acknowledgment BEFORE rebooting.
        proto.send_response("reboot_ack", {"message": "Rebooting now"})

        # Small delay to ensure the response is sent.
        time.sleep(1)

        # Reboot!
        # machine.reset() never returns -- the Pico restarts immediately.
        print("[proto] Rebooting...")
        machine.reset()

    def _handle_get_status(self, message, proto):
        """
        Handle status request.

        Server sends: {"type": "get_status"}
        We respond with detailed device information.

        Useful for the server dashboard to show device details.
        """
        mem_free = gc.mem_free()
        mem_alloc = gc.mem_alloc()

        # Get the Pico's unique hardware ID.
        # Every Pico has a globally unique ID burned into the chip at the factory.
        # This is like a MAC address but for the processor itself.
        import machine

        try:
            unique_id = "".join("{:02x}".format(b) for b in machine.unique_id())
        except Exception:
            unique_id = "unknown"

        proto.send_response(
            "status",
            {
                "device_id": self._config.get("device_id", "unknown"),
                "hardware_id": unique_id,
                "uptime_ms": time.ticks_ms(),
                "mem_free": mem_free,
                "mem_alloc": mem_alloc,
                "mem_pct_used": round(mem_alloc / (mem_free + mem_alloc) * 100, 1),
                "messages_handled": self._message_count,
                "firmware_version": "1.0.0",
            },
        )
