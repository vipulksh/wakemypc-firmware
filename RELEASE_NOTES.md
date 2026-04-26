## v0.2.0 — Staggered scans + log ring buffer

**Performance:**
- **Round-robin device scans.** The previous behaviour probed every
  managed device in the same main-loop iteration, blocking up to 8s
  per offline device (4 ports × 2s timeout). With three offline
  devices that was 24s of blocking per scan -- past the watchdog and
  heartbeat windows. Now the loop probes one device per tick at
  `scan_interval / N` spacing; each tick blocks for at most ~1-2s.
- **Single-port probe with early return** (`NetworkScanner.check_one`):
  port 80 first, port 22 fallback, 1s timeout. Stops on the first
  conclusive answer.

**Debug:**
- New **`log_buffer`** module: every `print()` is teed into both
  serial and an in-RAM 200-line ring buffer.
