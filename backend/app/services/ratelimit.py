"""Minimal in-process rate limiter for auth endpoints.

Sliding windows held in memory; the api container runs a single uvicorn worker,
so that state is authoritative.

Two things are limited, deliberately:

* the **client IP**, which behind the Cloudflare tunnel comes from
  CF-Connecting-IP. That header is only trusted when the request actually
  arrived from a trusted proxy — it is a plain request header, so anything able
  to reach the container directly could otherwise mint a fresh bucket per
  request and the limit became decorative.
* the **account** being targeted. An IP-keyed limit alone is bypassable by
  anyone with a pool of addresses (an ordinary IPv6 /64 is enough, no spoofing
  required), so the per-account window is what actually bounds password and
  one-time-code guessing against a single user.
"""

import ipaddress
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from app.config import settings

_HITS: dict[str, list[float]] = defaultdict(list)

_TOO_MANY = "Too many attempts — wait a minute and try again."

# With TUNNEL_PEERS unset there is no way to check that a CF-Connecting-IP
# really came through the tunnel, so it is never believed *instead of* the
# address nginx saw: the request also counts against that real address, at
# this multiple of the limit. A LAN caller forging a fresh header per request
# still runs into its own bucket, while everyone arriving through an
# unlabelled tunnel shares one pool wide enough that a single person's ten
# bad passwords do not lock the rest out.
UNVERIFIED_SHARE = 5


def _trusted_peer(request: Request) -> bool:
    """Is the immediate peer a proxy whose forwarded-IP header we believe?"""
    peer = request.client.host if request.client else ""
    if not peer:
        return False
    for entry in settings.trusted_proxy_list:
        try:
            if ipaddress.ip_address(peer) in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _in_any(address: str, networks: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    for entry in networks:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _resolve(request: Request) -> tuple[str, str]:
    """(real, forwarded): the address nginx saw, and the Cloudflare-claimed
    client if there is one that may be believed at all — "" otherwise.

    The socket peer cannot be forged, but behind nginx it is always nginx. So
    from a trusted proxy, X-Real-IP (nginx's own view of its peer) stands in
    for it — before that, every LAN caller collapsed into one bucket keyed on
    nginx's container address, and one person's ten bad passwords locked
    everyone on the network out for a minute.

    CF-Connecting-IP is kept on top of that only when the request could have
    come through the tunnel: with TUNNEL_PEERS set, that means X-Real-IP is
    cloudflared; unset, any trusted proxy — and what the caller then does
    with it is up to the caller (see bucket_keys).
    """
    peer = request.client.host if request.client else "unknown"
    if not _trusted_peer(request):
        return peer, ""
    real = (request.headers.get("x-real-ip") or "").split(",")[0].strip() or peer
    forwarded = (request.headers.get("cf-connecting-ip") or "").split(",")[0].strip()
    if not forwarded:
        return real, ""
    tunnel = settings.tunnel_peer_list
    if tunnel and not _in_any(real, tunnel):
        return real, ""
    return real, forwarded


def client_ip(request: Request) -> str:
    """The caller's address, as honestly as it can be known."""
    real, forwarded = _resolve(request)
    return forwarded or real


def bucket_keys(request: Request) -> list[tuple[str, int]]:
    """The (key, limit multiplier) pairs a request is counted against.

    One pair in the ordinary cases. Two when a forwarded address is present
    but cannot be verified (TUNNEL_PEERS unset): the pair of real and
    forwarded address at the normal limit, and the real address alone at
    UNVERIFIED_SHARE times it. The header also has to arrive with CF-Ray,
    which cloudflared always adds and a LAN caller has to go out of its way
    to forge — a speed bump, not a proof, which is why the real address's
    bucket is still charged.
    """
    real, forwarded = _resolve(request)
    if not forwarded:
        return [(real, 1)]
    if settings.tunnel_peer_list:
        return [(forwarded, 1)]
    if not request.headers.get("cf-ray"):
        return [(real, 1)]
    return [(f"{real}|{forwarded}", 1), (real, UNVERIFIED_SHARE)]


def _consume(key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    hits = [t for t in _HITS[key] if now - t < window_seconds]
    if len(hits) >= limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _TOO_MANY)
    hits.append(now)
    _HITS[key] = hits
    # Opportunistic cleanup so the dict can't grow unbounded.
    if len(_HITS) > 10000:
        for k in [k for k, v in _HITS.items() if not v or now - v[-1] > 3600]:
            _HITS.pop(k, None)


def rate_limit(bucket: str, limit: int, window_seconds: int):
    """FastAPI dependency: at most `limit` requests per window per client."""

    async def _dep(request: Request) -> None:
        for key, share in bucket_keys(request):
            _consume(f"{bucket}:{key}", limit * share, window_seconds)

    return _dep


def limit_account(bucket: str, identifier: str, limit: int, window_seconds: int) -> None:
    """Limit attempts against one account, regardless of source address.

    Called from inside a handler (it needs the request body) — the IP-keyed
    dependency runs first and this is the backstop that a rotating source
    address cannot sidestep.
    """
    _consume(f"{bucket}:acct:{identifier.strip().lower()}", limit, window_seconds)
