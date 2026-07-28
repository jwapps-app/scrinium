# Security policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

Use GitHub's [private vulnerability reporting](https://github.com/jwapps-app/scrinium/security/advisories/new)
(Security → Report a vulnerability). That keeps the details between us until a
fix exists.

Please include what you need to make the problem reproducible: the endpoint or
file, the request or input, what you expected, and what happened. A proof of
concept helps but is not required.

I maintain this in my spare time, so expect an initial response in days rather
than hours. I will tell you what I plan to do and when, and credit you in the
release notes unless you would rather stay anonymous.

## Supported versions

Only the `main` branch and the images built from it
(`ghcr.io/jwapps-app/scrinium-api` and `-web`, tag `latest`) receive fixes.
There are no maintained release branches — update to current `main` before
reporting.

## Threat model

Understanding what this project does and does not defend against will save us
both time.

**In scope.** Anything reachable by an unauthenticated request; privilege
boundaries between accounts in the same library; tenant isolation; handling of
hostile documents (a crafted PDF, an office file, a zip import) since those
arrive from outside; the public share-link surface; the ingestion paths that
accept input from elsewhere (email attachments, the watched folder); and
availability bugs where a single request or document can stall the whole
application.

**Out of scope.**

- **Anyone with filesystem access to the host.** Blobs are stored unencrypted
  and the container currently runs as root (documented in `backend/Dockerfile`).
  Encryption at rest is the storage layer's job.
- **Anyone with database access.** The database is trusted; it holds document
  text and TOTP secrets in the clear.
- **A compromised OCR sidecar.** The Apple Vision helper is assumed to be the
  operator's own machine on their own network.
- **Denial of service by an authenticated operator against their own library.**
  Uploading a thousand huge books is a supported use, not an attack.
- **Missing rate limits on ordinary authenticated endpoints.** Auth endpoints
  are limited; the rest assume an authenticated user is not attacking themselves.

## Deployment expectations

The application assumes it sits behind a reverse proxy that terminates TLS.
Two things matter if you deviate from the shipped `docker-compose` files:

- `SECRET_KEY` must be a strong random value. Production refuses to start on a
  weak or placeholder secret.
- `TRUSTED_PROXIES` decides whose `CF-Connecting-IP` header is believed for
  rate limiting. It defaults to private ranges, which covers the bundled nginx.
  If something else fronts the API, set it accordingly — an over-broad value
  lets a caller forge its own address and defeat the auth rate limits.
