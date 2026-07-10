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
(comma-separated, e.g. `en-US,de-DE`; omit for automatic).

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
launchctl load ~/Library/LaunchAgents/com.example.scrinium-ocr-helper.plist
curl http://localhost:9876/health
```

## Endpoints

- `GET /health` → `{"status":"ok","engine":"apple-vision"}`
- `POST /ocr` (body: PNG/JPEG bytes) →
  `{"width":1700,"height":2200,"blocks":[{"text":"…","confidence":0.98,"bbox":[x0,y0,x1,y1]}]}`
  — bbox is Vision-normalized (0–1, origin bottom-left). The coordinate
  mapping to PDF space happens server-side (the known fiddly bit; matters for
  Option A only).
