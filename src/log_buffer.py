"""log_buffer.py -- in-RAM ring buffer that captures stdout for later retrieval.

The Pico has no persistent log; before this module, the only way to see
what the firmware was doing was to physically attach a USB serial cable
and run `wakemypc logs`. That works at the user's desk, but not when the
Pico is wedged behind a TV three rooms away. This module lets the server
(via WebSocket) pull the last N lines of output on demand, surfaced to
the operator through `wakemypc logs --debug`.

Design:
  - A capped list (max ~200 entries, ~20KB cap) tracks recent lines.
  - install() replaces the builtin `print` with a wrapper that writes to
    both stdout (so live serial still works) and the ring buffer. We
    deliberately don't redirect sys.stdout via os.dupterm: that swallows
    REPL prompts and breaks USB-attached debugging.
  - get_dump() returns a copy of the buffer for the WS handler to ship.

Memory note: each line carries an int (uptime ms) + str. With 200 lines
of ~80 chars average we burn ~24KB of heap, which is the main reason
for the 200-line cap. If the buffer ever needs to grow we should move
to a fixed-size circular bytearray.
"""

import builtins
import time

_MAX_LINES = 200
_buffer = []
_orig_print = builtins.print
_installed = False


def _capture(*args, **kwargs):
    """Wrapped print: tees to stdout + ring buffer. Best-effort -- any
    exception caught so a buggy log call can't crash the firmware.
    """
    _orig_print(*args, **kwargs)
    try:
        sep = kwargs.get("sep", " ")
        line = sep.join(str(a) for a in args)
        _buffer.append((time.ticks_ms(), line))
        if len(_buffer) > _MAX_LINES:
            del _buffer[0 : len(_buffer) - _MAX_LINES]
    except Exception:
        pass


def install():
    """Activate the log buffer. Idempotent. Call once during boot."""
    global _installed
    if _installed:
        return
    builtins.print = _capture
    _installed = True


def get_dump(limit=None):
    """Return up to `limit` most recent lines as a list of dicts:
    [{"t": uptime_ms, "msg": "..."}, ...]. Default returns the whole
    buffer (which is itself capped at _MAX_LINES).
    """
    snap = _buffer[-limit:] if limit else list(_buffer)
    return [{"t": t, "msg": m} for t, m in snap]


def clear():
    _buffer.clear()
