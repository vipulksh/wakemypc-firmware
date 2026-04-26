## v0.3.1 — OTA redirect fix + partial-mask boot log

**Critical fix:**
- **OTA over GitHub Releases is now functional.** Previously
  `http_download` treated any non-200 response as a hard error. GitHub
  release URLs (`github.com/.../releases/download/...`) always reply
  302 with a `Location:` pointing at the asset CDN, so OTA against
  v0.3.0 would have silently failed at the very first byte. The
  downloader now follows up to 5 redirect hops, refuses
  HTTPS-to-HTTP downgrades, and refuses non-http(s) Location schemes.

**More debug logs throughout the OTA path:**
- `[ota.http] hop= 0 GET https://...` for every connection attempt
- `[ota.http]   host= ... port= ... ssl= ...` and the response status
- `[ota.http]   -> redirect to ...` on every 3xx hop
- `[ota.http]   downloaded N bytes to <path>` on success
- `[ota] fetch_manifest:` + manifest version / file count on parse
- `[ota] update complete: success= ... updated= ...` after the swap

**WS sanity log:**
- `[ws] Connecting to ...` now also prints `| tls=True/False`. A
  plaintext `ws://` connection logs a `WARNING: connecting over
  plaintext ws://` line so a misprovisioned production Pico is
  noticeable.

**Boot log: partial mask for secrets.**
The config-load banner now shows the first four characters and the
length of masked values rather than fully redacting them:

```
[config]   wifi   = ssid= Buffalo-1CD0 | password= myW1*** (12c) | order= 0
[config]   token  = 4f3a*** (40c)
```

Lets you confirm the right token / password is loaded from a shared
log without enough material to reconstruct the full secret.

**Reflash + reimport manifest** (this is the version that finally
applies via OTA cleanly, but the v0.3.0 -> v0.3.1 hop itself still
needs a USB flash because v0.3.0 has the broken downloader):

```
wakemypc upload --firmware-dir ./pico_firmware/src/
docker compose -f docker-compose.local.yml exec django \
  python manage.py import_firmware_manifest 0.3.1 --mark-latest
```
