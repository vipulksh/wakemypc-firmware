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


def http_download(url, dest_path, max_size=MAX_FILE_SIZE):
    """
    Download a file from an HTTP(S) URL and save it to the filesystem.

    This is a minimal HTTP client -- we don't use the `requests` library
    because it might not be available in all MicroPython builds.

    Parameters:
        url:       The URL to download from (http:// or https://)
        dest_path: Where to save the file on the Pico's flash
        max_size:  Maximum allowed file size in bytes

    Returns (success: bool, error_message: str or None)

    HOW HTTP DOWNLOAD WORKS:
    1. Parse the URL to get host, port, and path.
    2. Open a TCP connection to the server.
    3. Optionally wrap in TLS for HTTPS.
    4. Send an HTTP GET request.
    5. Read the response headers to get the content length.
    6. Read the response body and write it to a file.
    """
    try:
        # Parse URL.
        use_ssl = url.startswith("https://")
        url_stripped = url.replace("https://", "").replace("http://", "")

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

        # Connect.
        addr = socket.getaddrinfo(host, port)[0][-1]
        sock = socket.socket()
        sock.settimeout(30)
        sock.connect(addr)

        if use_ssl:
            sock = ssl.wrap_socket(sock, server_hostname=host)

        # Send HTTP GET request.
        request = (
            "GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        ).format(path=path, host=host)
        sock.send(request.encode())

        # Read response headers.
        # HTTP/1.0 responses end headers with \r\n\r\n.
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(1024)
            if not chunk:
                sock.close()
                return False, "Connection closed during headers"
            response += chunk

        # Split headers from body.
        header_end = response.index(b"\r\n\r\n")
        headers = response[:header_end].decode("ascii", "replace")
        body_start = response[header_end + 4 :]

        # Check status code.
        status_line = headers.split("\r\n")[0]
        if "200" not in status_line:
            sock.close()
            return False, "HTTP error: " + status_line

        # Check content length if available.
        content_length = -1
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
                break

        if content_length > max_size:
            sock.close()
            return False, "File too large: {} bytes (max: {})".format(
                content_length, max_size
            )

        # Write body to file.
        total_bytes = 0
        with open(dest_path, "wb") as f:
            # Write any body data we already received with the headers.
            if body_start:
                f.write(body_start)
                total_bytes += len(body_start)

            # Read remaining body.
            while True:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_size:
                    sock.close()
                    f.close()
                    # Clean up the partial file.
                    try:
                        os.remove(dest_path)
                    except OSError:
                        pass
                    return False, "File exceeded max size during download"
                f.write(chunk)

        sock.close()
        print("[ota] Downloaded", total_bytes, "bytes to", dest_path)
        return True, None

    except Exception as e:
        return False, str(e)


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

    def __init__(self):
        # Ensure the backup directory exists.
        ensure_dir(BACKUP_DIR)

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

            success, error = http_download(url, temp_path)
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

        # PHASE 3: Back up current files and replace them.
        updated = []
        for tf in temp_files:
            filename = tf["filename"]
            temp_path = tf["temp_path"]
            backup_path = BACKUP_DIR + "/" + filename

            # Back up the current file (if it exists).
            try:
                os.rename(filename, backup_path)
                print("[ota] Backed up:", filename, "->", backup_path)
            except OSError:
                # File doesn't exist yet (new file). That's fine.
                pass

            # Move the temp file to the final location.
            try:
                os.rename(temp_path, filename)
                updated.append(filename)
                print("[ota] Installed:", filename)
            except OSError as e:
                print("[ota] ERROR installing", filename, ":", e)
                # Try to restore from backup.
                try:
                    os.rename(backup_path, filename)
                    print("[ota] Restored backup for:", filename)
                except OSError:
                    pass

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


def fetch_manifest(manifest_url):
    """Download and parse MANIFEST.json from a GitHub Release URL.

    Returns a list of {"filename", "url", "checksum"} dicts ready for
    OTAUpdater.update(), or raises an Exception on failure.

    The GitHub Actions release workflow (release.yml) builds this file
    automatically when a version tag is pushed. It is the single source
    of truth for what files ship in each release -- the server reads it,
    and now the Pico reads it directly so both sides agree without the
    server having to embed the full file list in every OTA message.
    """
    success, error = http_download(manifest_url, "/tmp_manifest.json")
    if not success:
        raise Exception("Failed to download manifest: " + str(error))

    try:
        with open("/tmp_manifest.json") as f:
            raw = json.loads(f.read())
    finally:
        try:
            os.remove("/tmp_manifest.json")
        except OSError:
            pass

    files = []
    for entry in raw.get("files", []):
        filename = entry.get("path", "")
        url = entry.get("url", "")
        checksum = entry.get("sha256", "")
        if filename and url and checksum:
            files.append({"filename": filename, "url": url, "checksum": checksum})

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

    if manifest_url:
        print("[ota] Fetching manifest from", manifest_url)
        try:
            files = fetch_manifest(manifest_url)
        except Exception as e:
            proto.send_response(
                "ota_result",
                {"success": False, "message": "Manifest fetch failed: " + str(e)},
            )
            return

    if not files:
        proto.send_response(
            "ota_result",
            {"success": False, "message": "No files in manifest"},
        )
        return

    updater = OTAUpdater()
    result = updater.update(files)

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
