"""Minimal in-process rate limiter for auth endpoints.

A sliding window keyed by client IP. The api container runs a single
uvicorn worker, so in-memory state is authoritative; behind the Cloudflare
tunnel the client IP comes from CF-Connecting-IP (set by Cloudflare, not
the client). This is defense in depth against password/2FA brute force —
not a substitute for a strong password.
"""

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

_HITS: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


def rate_limit(bucket: str, limit: int, window_seconds: int):
    """FastAPI dependency: at most `limit` requests per window per IP."""

    async def _dep(request: Request) -> None:
        key = f"{bucket}:{_client_ip(request)}"
        now = time.monotonic()
        hits = [t for t in _HITS[key] if now - t < window_seconds]
        if len(hits) >= limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many attempts — wait a minute and try again.",
            )
        hits.append(now)
        _HITS[key] = hits
        # Opportunistic cleanup so the dict can't grow unbounded.
        if len(_HITS) > 10000:
            for k in [k for k, v in _HITS.items() if not v or now - v[-1] > 3600]:
                _HITS.pop(k, None)

    return _dep
