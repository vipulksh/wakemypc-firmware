"""
ota_updater.py - Over-the-Air Firmware Updates
================================================

WHAT IS OTA (Over-the-Air) UPDATE?
-----------------------------------
OTA means updating the device's software remotely, without physically
connecting it to a computer. Instead of unplugging the Pico, connecting
it via USB, and manually copying new .py files, we download them over WiFi.

This is how your phone updates apps and your smart home devices get patches.
It's essential for IoT devices because:
1. They might be in hard-to-reach places (behind furniture, in ceilings)
2. You might have dozens of them (updating each by USB would be tedious)
3. Security patches need to be deployed quickly

HOW OUR OTA WORKS:
------------------
1. Server sends an "ota_update" message with a list of files and their URLs.
2. Pico downloads each file from the server via HTTP(S).
3. Pico verifies the download using a checksum (to detect corruption).
4. Pico backs up the current version of each file.
5. Pico replaces the old files with the new ones.
6. Pico reboots to load the new code.

WHAT IS A CHECKSUM?
A checksum is a fixed-size "fingerprint" of a file's contents. If even one byte
changes, the checksum is completely different. We use SHA-256, which produces a
64-character hex string. The server sends the expected checksum, and we verify
that our downloaded file matches. If it doesn't, the download was corrupted
(maybe a network glitch dropped some bytes) and we abort.

BACKUP STRATEGY:
Before replacing any file, we copy the current version to a /backup/ folder.
If the update fails partway through (power loss, corrupted download, etc.),
the backup files are still intact. On the next boot, if the new code crashes
immediately, the watchdog timer reboots the Pico, and we could theoretically
detect the crash loop and restore from backup (though this basic implementation
doesn't do automatic rollback -- that would require boot.py changes).

FLASH FILESYSTEM LIMITATIONS:
The Pico has about 1.5 MB of usable flash. Our firmware is typically well
under 100 KB total, so there's plenty of room for backups. But we still
need to be careful about:
- Not writing huge files (would fill the flash)
- Not writing too frequently (flash has ~100K write cycles)
- Handling power loss during writes (use temp files + rename)

WHY REBOOT AFTER UPDATE?
MicroPython loads .py files into RAM when they're imported. Changing the file
on flash doesn't affect the already-loaded code in RAM. We need to reboot so
MicroPython re-reads and re-imports the updated files.
"""

# -------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------
import os
import time

try:
    import ujson as json
except ImportError:
    pass

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

# For HTTP downloads. MicroPython has `urequests` on some builds,
# but we'll use raw sockets for maximum compatibility.
import socket

try:
    import ussl as ssl
except ImportError:
    import ssl

try:
    import ubinascii as binascii
except ImportError:
    import binascii


# -------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------
# Directory for backup files.
BACKUP_DIR = "/backup"

# Maximum file size we'll accept (128 KB). Prevents accidentally filling the flash.
MAX_FILE_SIZE = 128 * 1024

# Files that should NOT be updated via OTA (security-sensitive).
PROTECTED_FILES = {"secrets.json", "secrets.json.bak"}


# -------------------------------------------------------------------------
# Helper Functions
# -------------------------------------------------------------------------


def ensure_dir(path):
    """
    Create a directory if it doesn't exist.

    MicroPython's os.mkdir() raises OSError if the directory already exists,
    and there's no os.makedirs(). So we check first.

    In regular Python, you'd use os.makedirs(path, exist_ok=True).
    In MicroPython, we do it manually.
    """
    try:
        os.stat(path)
    except OSError:
        try:
            os.mkdir(path)
            print("[ota] Created directory:", path)
        except OSError as e:
            print("[ota] Failed to create directory:", path, e)


def file_sha256(filepath):
    """
    Calculate the SHA-256 checksum of a file.

    WHAT IS SHA-256?
    SHA-256 is a cryptographic hash function. It takes any input (a file, a
    string, whatever) and produces a fixed-size output: 32 bytes, typically
    displayed as 64 hexadecimal characters. Example:
        "Hello" -> "185f8db32271fe25f561a6fc938b2e264306ec304eda518007d1764826381969"

    Key properties:
    1. Deterministic: same input always gives the same output.
    2. One-way: you can't reverse-engineer the input from the output.
    3. Collision-resistant: it's practically impossible to find two different
       inputs that produce the same output.
    4. Avalanche effect: changing one bit of input completely changes the output.

    We use it to verify that a downloaded file matches what the server intended
    to send. If even one byte was corrupted during transmission, the SHA-256
    will be completely different.

    Returns the hex string of the SHA-256 hash.
    """
    h = hashlib.sha256()

    try:
        with open(filepath, "rb") as f:
            while True:
                # Read in chunks to handle files larger than available RAM.
                chunk = f.read(512)
                if not chunk:
                    break
                # Feed each chunk into the hash function.
                h.update(chunk)

        # digest() returns raw bytes, hexlify() converts to hex string.
        return binascii.hexlify(h.digest()).decode("ascii")

    except OSError:
        return None


def http_download(url, dest_path, max_size=MAX_FILE_SIZE, max_redirects=5,
                  feed_watchdog=None):
    """
    Download a file from an HTTP(S) URL and save it to the filesystem,
    following redirects.

    Parameters:
        url:           starting URL (http:// or https://)
        dest_path:     where to save the file on the Pico's flash
        max_size:      maximum allowed file size in bytes
        max_redirects: how many 3xx redirects to follow before giving up
        feed_watchdog: optional zero-arg callable that the caller wires
                       to its WDT.feed(). Called before every potentially
                       slow socket op (DNS, connect, TLS handshake, recv).
                       Without this, multi-file OTAs over TLS would blow
                       the rp2 8s hardware watchdog -- the main-loop's
                       feed only runs between WS messages, not during a
                       synchronous handler.

    Returns (success: bool, error_message: str or None)
    """
    def _feed():
        if feed_watchdog is not None:
            try:
                feed_watchdog()
            except Exception:
                pass

    current_url = url
    for hop in range(max_redirects + 1):
        print("[ota.http] hop=", hop, "GET", current_url)
        try:
            use_ssl = current_url.startswith("https://")
            url_stripped = current_url.replace("https://", "").replace("http://", "")

            if "/" in url_stripped:
                host_port, path = url_stripped.split("/", 1)
                path = "/" + path
            else:
                host_port = url_stripped
                path = "/"

            if ":" in host_port:
                host, port = host_port.split(":", 1)
                port = int(port)
            else:
                host = host_port
                port = 443 if use_ssl else 80

            print("[ota.http]   host=", host, "port=", port, "ssl=", use_ssl)
            _feed()
            print("[ota.http]   resolving DNS...")
            addr = socket.getaddrinfo(host, port)[0][-1]
            _feed()
            print("[ota.http]   connecting TCP...")
            sock = socket.socket()
            sock.settimeout(15)
            sock.connect(addr)
            _feed()

            if use_ssl:
                print("[ota.http]   TLS handshake...")
                sock = ssl.wrap_socket(sock, server_hostname=host)
                _feed()

            request = (
                "GET {path} HTTP/1.0\r\nHost: {host}\r\n"
                "User-Agent: wakemypc-pico-ota/1\r\n"
                "Connection: close\r\n\r\n"
            ).format(path=path, host=host)
            sock.send(request.encode())
            _feed()

            response = b""
            while b"\r\n\r\n" not in response:
                _feed()
                chunk = sock.recv(1024)
                if not chunk:
                    sock.close()
                    return False, "Connection closed during headers"
                response += chunk

            header_end = response.index(b"\r\n\r\n")
            headers = response[:header_end].decode("ascii", "replace")
            body_start = response[header_end + 4 :]

            status_line = headers.split("\r\n")[0]
            print("[ota.http]   status=", status_line)

            # Parse status code out of the line ("HTTP/1.1 302 Found").
            try:
                status_code = int(status_line.split(" ")[1])
            except (IndexError, ValueError):
                sock.close()
                return False, "Malformed status line: " + status_line

            # Follow redirects (301 permanent, 302/303 found, 307/308 PRD).
            if status_code in (301, 302, 303, 307, 308):
                location = None
                for line in headers.split("\r\n"):
                    if line.lower().startswith("location:"):
                        location = line.split(":", 1)[1].strip()
                        break
                sock.close()
                if not location:
                    return False, "{} redirect with no Location header".format(
                        status_code
                    )
                # Resolve relative redirects against the current host
                # (preserves the scheme of the current request).
                if location.startswith("/"):
                    scheme = "https://" if use_ssl else "http://"
                    location = scheme + host + location
                # Refuse HTTPS -> HTTP downgrades. A MITM could craft a
                # redirect to plaintext and harvest the body. Firmware
                # downloads must stay on TLS once started on TLS.
                if use_ssl and location.startswith("http://"):
                    return False, "Refusing HTTPS->HTTP downgrade redirect to " + location
                if not (location.startswith("https://") or location.startswith("http://")):
                    return False, "Refusing redirect to non-http(s) scheme: " + location
                print("[ota.http]   -> redirect to", location)
                current_url = location
                continue  # next hop

            if status_code != 200:
                sock.close()
                return False, "HTTP error: " + status_line

            content_length = -1
            for line in headers.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":")[1].strip())
                    break
            print("[ota.http]   content_length=", content_length)

            if content_length > max_size:
                sock.close()
                return False, "File too large: {} bytes (max: {})".format(
                    content_length, max_size
                )

            total_bytes = 0
            with open(dest_path, "wb") as f:
                if body_start:
                    f.write(body_start)
                    total_bytes += len(body_start)

                while True:
                    _feed()
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_size:
                        sock.close()
                        f.close()
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass
                        return False, "File exceeded max size during download"
                    f.write(chunk)

            sock.close()
            print("[ota.http]   downloaded", total_bytes, "bytes to", dest_path)
            return True, None

        except Exception as e:
            print("[ota.http]   error:", e)
            return False, str(e)

    return False, "Too many redirects (max {})".format(max_redirects)


# -------------------------------------------------------------------------
# OTA Updater
# -------------------------------------------------------------------------
class OTAUpdater:
    """
    Handles over-the-air firmware updates.

    The update process:
    1. Receive file list from server (with URLs and checksums)
    2. Download each file to a temp location
    3. Verify checksums
    4. Back up current files
    5. Replace current files with new ones
    6. Reboot

    Usage:
        updater = OTAUpdater()
        result = updater.update(file_list)
        if result["success"]:
            machine.reset()  # Reboot to load new code
    """

    def __init__(self, feed_watchdog=None):
        # Ensure the backup directory exists.
        ensure_dir(BACKUP_DIR)
        # Optional zero-arg callback that pulses the hardware WDT. Wired
        # through main.py so the WDT keeps getting fed during the long
        # synchronous OTA pipeline (downloads, TLS handshakes, hashing).
        # Without it the rp2 8s WDT fires somewhere mid-OTA -- exactly
        # the crash we saw between log_buffer.py and main.py downloads.
        self._feed_watchdog = feed_watchdog

    def _feed(self):
        if self._feed_watchdog is not None:
            try:
                self._feed_watchdog()
            except Exception:
                pass

    def update(self, files):
        """
        Perform an OTA update with the given file list.

        Parameters:
            files: List of dicts, each with:
                   - "filename": Name of the file (e.g., "main.py")
                   - "url": URL to download from
                   - "checksum": Expected SHA-256 hex string

        Returns:
            dict with "success" (bool), "message" (str), "updated" (list of filenames)

        THE UPDATE IS ALL-OR-NOTHING:
        We download and verify ALL files before replacing ANY of them.
        If any file fails to download or verify, we abort the entire update.
        This prevents ending up with a mix of old and new code that might be
        incompatible.
        """
        if not files:
            return {"success": False, "message": "No files to update", "updated": []}

        # Filter out protected files.
        safe_files = []
        for f in files:
            filename = f.get("filename", "")
            if filename in PROTECTED_FILES:
                print("[ota] Skipping protected file:", filename)
                continue
            safe_files.append(f)

        if not safe_files:
            return {"success": False, "message": "No updatable files", "updated": []}

        print("[ota] Starting update,", len(safe_files), "files to process")

        # PHASE 1: Download all files to temp locations.
        # We use a ".ota" suffix for temp files. If something goes wrong,
        # these can be cleaned up without affecting the running code.
        temp_files = []

        for file_info in safe_files:
            filename = file_info["filename"]
            url = file_info["url"]
            expected_checksum = file_info.get("checksum", "")

            temp_path = filename + ".ota"
            print("[ota] Downloading:", filename)

            success, error = http_download(
                url, temp_path, feed_watchdog=self._feed_watchdog
            )
            if not success:
                # Clean up temp files and abort.
                self._cleanup_temp_files(temp_files)
                return {
                    "success": False,
                    "message": "Download failed for {}: {}".format(filename, error),
                    "updated": [],
                }

            temp_files.append({"filename": filename, "temp_path": temp_path})

            # PHASE 2: Verify checksum.
            if expected_checksum:
                actual_checksum = file_sha256(temp_path)
                if actual_checksum != expected_checksum:
                    print("[ota] Checksum mismatch for", filename)
                    print("[ota]   Expected:", expected_checksum)
                    print("[ota]   Got:     ", actual_checksum)
                    self._cleanup_temp_files(temp_files)
                    return {
                        "success": False,
                        "message": "Checksum mismatch for " + filename,
                        "updated": [],
                    }
                print("[ota] Checksum verified:", filename)

        # PHASE 3a: Sweep stale backups from the previous successful
        # update. Without this, /backup/ keeps growing release after
        # release. We keep one revision back -- the version we are
        # about to upgrade FROM -- which is always the freshest set of
        # backups. Older sets are tossed.
        self._sweep_old_backups()

        # PHASE 3b: Back up current files and replace them.
        # Track installed_so_far so we can undo on partial failure --
        # the previous version of this loop only restored the file
        # that failed, leaving any earlier successfully-installed
        # files in place. That left the firmware in a half-updated
        # state: some files at the new version, some at the old, and
        # nothing on the next reboot to recover.
        updated = []
        installed_so_far = []  # [(filename, backup_existed: bool), ...]
        for tf in temp_files:
            filename = tf["filename"]
            temp_path = tf["temp_path"]
            backup_path = BACKUP_DIR + "/" + filename

            # Back up the current file (if it exists).
            backup_existed = False
            try:
                os.rename(filename, backup_path)
                print("[ota] Backed up:", filename, "->", backup_path)
                backup_existed = True
            except OSError:
                # File doesn't exist yet (new file in this release).
                # Logged so an operator scanning the OTA trace can tell
                # 'no backup' from 'forgot to back up'.
                print("[ota] No prior version, skipping backup:", filename)

            # Move the temp file to the final location.
            try:
                os.rename(temp_path, filename)
                updated.append(filename)
                installed_so_far.append((filename, backup_existed))
                print("[ota] Installed:", filename)
            except OSError as e:
                print("[ota] ERROR installing", filename, ":", e)
                # Restore EVERYTHING we replaced this run -- not just
                # the failed file. Otherwise the firmware ends up at a
                # mix of old + new modules and probably fails on next
                # boot.
                self._rollback(filename, installed_so_far)
                self._cleanup_temp_files(temp_files)
                return {
                    "success": False,
                    "message": "Install failed for {}: {} (rolled back)".format(
                        filename, e
                    ),
                    "updated": [],
                }

        print("[ota] Update complete.", len(updated), "files updated")

        # PHASE 4: Run post-install hooks.
        # A hook is any file in the manifest with "post_install": true.
        # Use case: secrets.json migrations when a release renames or adds
        # required config keys. The hook reads secrets.json, transforms it,
        # writes it back. If "delete_after" is also true, the file is removed
        # from flash after it runs so it doesn't waste space or re-run.
        for file_info in safe_files:
            if not file_info.get("post_install"):
                continue
            hook_file = file_info["filename"]
            print("[ota] Running post-install hook:", hook_file)
            try:
                # Import by stripping .py, run its run() function.
                module_name = hook_file.replace(".py", "").replace("/", "_")
                hook_mod = __import__(module_name)
                if hasattr(hook_mod, "run"):
                    hook_mod.run()
                print("[ota] Hook completed:", hook_file)
            except Exception as e:
                print("[ota] Hook error:", hook_file, ":", e)
            if file_info.get("delete_after"):
                try:
                    os.remove(hook_file)
                    print("[ota] Deleted hook:", hook_file)
                except OSError:
                    pass

        return {
            "success": len(updated) == len(safe_files),
            "message": "Updated {} of {} files".format(len(updated), len(safe_files)),
            "updated": updated,
        }

    def _cleanup_temp_files(self, temp_files):
        """Remove temporary download files after a failed update."""
        for tf in temp_files:
            try:
                os.remove(tf["temp_path"])
            except OSError:
                pass

    def _rollback(self, failed_filename, installed_so_far):
        """Restore every file that was successfully installed in this
        update run, then delete the failed-file's leftover. Called when
        a single os.rename fails mid-install.

        installed_so_far is a list of (filename, backup_existed) tuples
        in install order. We undo in install order (rather than reverse
        order) because each rename is independent -- the order doesn't
        matter for correctness, only for log readability.
        """
        print("[ota] rollback: restoring", len(installed_so_far), "file(s)")
        for filename, backup_existed in installed_so_far:
            backup_path = BACKUP_DIR + "/" + filename
            # First remove the new file we just installed.
            try:
                os.remove(filename)
                print("[ota]   removed installed:", filename)
            except OSError:
                pass
            # Then restore the backup if there was one.
            if backup_existed:
                try:
                    os.rename(backup_path, filename)
                    print("[ota]   restored:", filename)
                except OSError as e:
                    print("[ota]   FAILED to restore", filename, ":", e)
            else:
                print("[ota]   was a new file, no backup to restore:", filename)
        # The failed file itself: remove any leftover .ota or partial.
        try:
            os.remove(failed_filename + ".ota")
        except OSError:
            pass

    def _sweep_old_backups(self):
        """Clear /backup/ before a new update writes fresh ones.

        We keep AT MOST one revision back: the version you are upgrading
        FROM. Without this sweep, /backup/ accumulates files release
        after release and eats flash. The cost of clearing here is
        that a successful update no longer leaves N versions of
        history -- the prior /backup/ becomes inaccessible. That's
        fine: by the time you're triggering a new update, you've
        already validated the running version.
        """
        try:
            entries = os.listdir(BACKUP_DIR)
        except OSError:
            return
        for entry in entries:
            try:
                os.remove(BACKUP_DIR + "/" + entry)
            except OSError:
                pass
        if entries:
            print("[ota] swept", len(entries), "stale backup(s)")

    def get_file_versions(self):
        """
        Get a dict of current file checksums (useful for the server to know
        which files need updating).

        Returns: {"main.py": "abc123...", "wol.py": "def456...", ...}
        """
        versions = {}
        for entry in os.listdir("/"):
            if entry.endswith(".py"):
                checksum = file_sha256(entry)
                if checksum:
                    versions[entry] = checksum
        return versions


def fetch_manifest(manifest_url, feed_watchdog=None):
    """Download and parse MANIFEST.json from a GitHub Release URL.

    Returns a list of {"filename", "url", "checksum"} dicts ready for
    OTAUpdater.update(), or raises an Exception on failure.

    The GitHub Actions release workflow (release.yml) builds this file
    automatically when a version tag is pushed. It is the single source
    of truth for what files ship in each release.
    """
    print("[ota] fetch_manifest:", manifest_url)
    success, error = http_download(
        manifest_url, "/tmp_manifest.json", feed_watchdog=feed_watchdog
    )
    if not success:
        raise Exception("Failed to download manifest: " + str(error))

    try:
        with open("/tmp_manifest.json") as f:
            raw_text = f.read()
        print("[ota]   manifest size:", len(raw_text), "bytes")
        raw = json.loads(raw_text)
    finally:
        try:
            os.remove("/tmp_manifest.json")
        except OSError:
            pass

    print(
        "[ota]   manifest version=",
        raw.get("version"),
        "min_compat=",
        raw.get("min_compat_version"),
        "tier=",
        raw.get("tier_required"),
        "files=",
        len(raw.get("files", [])),
    )

    files = []
    for entry in raw.get("files", []):
        filename = entry.get("path", "")
        url = entry.get("url", "")
        checksum = entry.get("sha256", "")
        if filename and url and checksum:
            files.append({"filename": filename, "url": url, "checksum": checksum})
        else:
            print("[ota]   skipping incomplete entry:", entry)

    return files


def handle_ota_update(message, proto):
    """Protocol handler for OTA update commands.

    The server can send the update in two ways:

    Preferred -- manifest_url (server points at GitHub Release):
        {
            "type": "ota_update",
            "version": "0.2.1",
            "manifest_url": "https://github.com/.../releases/download/v0.2.1/MANIFEST.json"
        }
        The Pico fetches MANIFEST.json from GitHub, verifies each file's
        sha256, and swaps them in. The manifest is the single source of
        truth -- both the server and the Pico read from the same file so
        there is no risk of the server embedding a stale or mismatched list.

    Legacy -- inline files list (still accepted for backward compat):
        {
            "type": "ota_update",
            "files": [{"filename": "main.py", "url": "...", "checksum": "..."}, ...]
        }

    Response:
        {"type": "ota_result", "success": true/false, "message": "...",
         "updated": ["main.py", ...]}
    """
    manifest_url = message.get("manifest_url")
    files = message.get("files", [])
    version = message.get("version", "?")

    # main.py stashes watchdog.feed on proto so we can keep the rp2 8s
    # WDT alive across the long synchronous OTA pipeline. getattr keeps
    # the handler usable in tests / older firmware where it's absent.
    feed_wdt = getattr(proto, "_feed_watchdog", None)

    print("[ota] handle_ota_update: version=", version,
          "manifest_url=", "yes" if manifest_url else "no",
          "inline_files=", len(files))

    if manifest_url:
        try:
            files = fetch_manifest(manifest_url, feed_watchdog=feed_wdt)
        except Exception as e:
            print("[ota] manifest fetch failed:", e)
            proto.send_response(
                "ota_result",
                {
                    "success": False,
                    "version": version,
                    "message": "Manifest fetch failed: " + str(e),
                },
            )
            return

    if not files:
        print("[ota] no files to update")
        proto.send_response(
            "ota_result",
            {
                "success": False,
                "version": version,
                "message": "No files in manifest",
            },
        )
        return

    print("[ota] beginning update of", len(files), "file(s) for v" + str(version))
    updater = OTAUpdater(feed_watchdog=feed_wdt)
    result = updater.update(files)
    result["version"] = version
    print(
        "[ota] update complete: success=",
        result.get("success"),
        "updated=",
        result.get("updated"),
        "message=",
        result.get("message"),
    )

    proto.send_response("ota_result", result)

    if result["success"]:
        import machine

        print("[ota] Rebooting to apply update...")
        time.sleep(2)
        machine.reset()


def handle_get_versions(message, proto):
    """
    Protocol handler for file version queries.

    Server sends:  {"type": "get_versions"}
    Response:      {"type": "versions", "files": {"main.py": "sha256...", ...}}

    The server uses this to determine which files need updating.
    """
    updater = OTAUpdater()
    versions = updater.get_file_versions()

    proto.send_response(
        "versions",
        {
            "files": versions,
        },
    )
