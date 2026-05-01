## v0.4.0 — Major Bug fix: wss:// connections silently drop all incoming messages after auth

**Root cause.** After the WebSocket upgrade handshake succeeded, the code called
`settimeout(0.1)` on the raw TCP socket to make `recv()` non-blocking. This works
fine on plain `ws://` but silently breaks encrypted `wss://` connections.

MicroPython's TLS stack (mbedTLS) reads a TLS record in two phases: first the
5-byte record header, then the encrypted payload. Both phases call `recv()` on the
underlying TCP socket. With a 0.1 s timeout on that socket, the second `recv()`
would return `EAGAIN` if the payload hadn't arrived within the timeout window.
mbedTLS treated this as a hard error and aborted the record read — but it left the
TCP receive buffer in a half-consumed state (header consumed, payload still queued).
On the next call, mbedTLS started a fresh record read and misinterpreted the
leftover payload bytes as a new TLS record header, permanently corrupting the TLS
stream. Every subsequent server message was silently discarded from that point on.

In practice this meant the Pico connected and sent `auth` successfully, but never
received `auth_ok` or any other downstream message. The connection appeared healthy
(no error logs, no disconnect) but the device was deaf.

**Fix.**
- After the 101 Switching Protocols response, reset the raw socket to fully blocking
  mode (`settimeout(None)`) so mbedTLS can always read a complete TLS record without
  interruption.
- Replace the socket-timeout-based non-blocking behaviour in `recv()` with a
  `select()` call (`uselect` on MicroPython). `select([sock], [], [], 0.05)` waits
  up to 50 ms for data and returns immediately if the socket is readable, keeping
  the main loop responsive without touching the socket's blocking mode.

### Other changes

- **`User-Agent` header on WebSocket upgrade.** Added `User-Agent: WakeMyPC-Pico/1.0`
  to the HTTP upgrade request. Cloudflare's bot-detection layer can silently close
  WebSocket connections from clients with no User-Agent header.
- **Client-initiated WS ping disabled.** Cloudflare terminates WebSocket connections
  when it receives a client-initiated ping frame (opcode `0x9`). `check_heartbeat()`
  now skips sending pings; dead connections are detected instead by `recv()` returning
  empty bytes or `send()` raising on the application-level heartbeat.
- **Accurate free-RAM readings in tick log.** `gc.collect()` is now called before
  sampling `gc.mem_free()` in the 5-second tick log. Previously, uncollected garbage
  from `recv()`, JSON decoding, and `print()` wrappers made the reading artificially
  low, and the subsequent heartbeat's `gc.collect()` made it jump back up — giving
  the false impression of a memory leak.
- **Improved OSError handling in `recv()`.** `EAGAIN` and `ETIMEDOUT` are silently
  swallowed (normal non-blocking no-data cases). Any other `OSError`
  (`ECONNRESET`, `EPIPE`, etc.) now marks the connection dead and logs the error,
  rather than being silently ignored.

