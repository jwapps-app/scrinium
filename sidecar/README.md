# Vision OCR helper (Mac sidecar)

Native macOS executable exposing Apple Vision text recognition over HTTP so the
Linux container can use it as its OCR engine. See SCRINIUM.md ("The Apple
Vision Add-On") for the full design.

## Build

```bash
cd sidecar
swift build -c release
# binary at .build/release/scrinium-ocr-helper
```

## Run

```bash
OCR_HELPER_PORT=9876 .build/release/scrinium-ocr-helper
```

Env vars: `OCR_HELPER_PORT` (default 9876), `OCR_HELPER_LANGUAGES`
(comma-separated, e.g. `en-US,de-DE`; omit for automatic),
`OCR_HELPER_MAX_CONCURRENCY` (default 4) — how many Vision recognitions run
at once. Requests beyond the limit queue instead of exhausting threads, so
the server stays responsive under heavy concurrent load (multiple worker
lanes). Raise it only if the Mac has spare cores and the helper is the
bottleneck.

The queue is bounded too: `OCR_HELPER_MAX_WAITING` (default 2 × the
concurrency + 4) is how many `/ocr` requests may wait for a slot. Past that
the helper answers 503 straight away rather than holding more page images
in memory, and the backend treats it like any other helper failure: that
document falls back to Tesseract and shows up as upgradable. If you see 503s
under a normal bulk ingest, the workers are fanning out more pages than the
Mac can absorb — lower `WORKER_CONCURRENCY`/`OCR_JOBS` on the backend or raise
this. `OCR_HELPER_READ_TIMEOUT_SECONDS` (default 30) is how long a connection
may take to deliver a complete request before it is closed with 408.

**Reinstalling after an update:** rebuild, copy over the old binary, and
restart the launchd job:

```bash
cd sidecar && swift build -c release
sudo cp .build/release/scrinium-ocr-helper /usr/local/bin/scrinium-ocr-helper
launchctl kickstart -k gui/$(id -u)/com.example.scrinium-ocr-helper
curl http://localhost:9876/health
```

## Point the app at it

In the backend env (compose / Portainer / `.env`):

```
OCR_ENGINE=apple
APPLE_OCR_URL=http://host.docker.internal:9876
```

Restart the api + worker containers. The Settings page shows
connected/not-detected. If the helper is down, ingestion silently falls back
to Tesseract — it never blocks.

## Start on login (launchd)

Save as `~/Library/LaunchAgents/com.example.scrinium-ocr-helper.plist`, fixing the
binary path, then `launchctl load` it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.scrinium-ocr-helper</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/scrinium-ocr-helper</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OCR_HELPER_PORT</key>
        <string>9876</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

```bash
xattr -d com.apple.quarantine ~/Library/LaunchAgents/com.example.scrinium-ocr-helper.plist 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.scrinium-ocr-helper.plist
curl http://localhost:9876/health
```

The `xattr` line matters if the plist was downloaded rather than written in
place: a downloaded file carries a quarantine attribute, and from macOS 27
launchd silently skips a quarantined launch agent at login. The helper then
works until the next reboot and vanishes after it, with nothing in any log.

## If it worked and then stopped

Usually a macOS upgrade. Check whether launchd still has the agent, and
reload it:

```bash
launchctl print gui/$(id -u)/com.example.scrinium-ocr-helper >/dev/null 2>&1 && echo loaded || echo NOT LOADED
xattr -d com.apple.quarantine ~/Library/LaunchAgents/com.example.scrinium-ocr-helper.plist 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.scrinium-ocr-helper.plist
curl http://localhost:9876/health
```

Scrinium falls back to Tesseract while the helper is down and sends a push
notification the first time a document has to. Documents processed that way
show under Settings › Server-side OCR as "used Tesseract", with an Upgrade
button that re-OCRs them once the helper is back.

### Malformed requests are refused, not fatal

The helper answers `400`, `413` or `431` (and closes the connection) to a
request it will not read: a `Content-Length` that is negative, non-numeric,
missing a value, repeated with different values, or above 64 MiB; a chunked
`Transfer-Encoding`; or a header section over 16 KiB. Earlier builds trapped
on a negative length and the whole process died — every `/health` check
failed until launchd restarted it. A stream of these in the log now just
means something on the network is poking the port; the helper keeps
answering. `check.sh` sends each of them and confirms `/health` still works:

```bash
./check.sh 9876              # a running helper, limits only
./check.sh 9876 page.png     # also /ocr with a real page image
```

The idle-connection (408) and full-queue (503) checks need the helper
started with small limits; the script says which variables to set.

## Endpoints

- `GET /health` → `{"status":"ok","engine":"apple-vision"}`
- `POST /ocr` (body: PNG/JPEG bytes, `Content-Length` required, at most
  64 MiB) →
  `{"width":1700,"height":2200,"blocks":[{"text":"…","confidence":0.98,"bbox":[x0,y0,x1,y1]}]}`
  — bbox is Vision-normalized (0–1, origin bottom-left). The coordinate
  mapping to PDF space happens server-side (the known fiddly bit; matters for
  Option A only). `422` when the body does not decode as an image; `503`
  with `Retry-After` when too many requests are already waiting.
