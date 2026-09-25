"""Rate-limit attribution when the tunnel peer cannot be verified."""

import uuid

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import ratelimit
from app.services.ratelimit import UNVERIFIED_SHARE, bucket_keys, client_ip, rate_limit


class _Client:
    def __init__(self, host):
        self.host = host


class _Req:
    def __init__(self, peer, headers):
        self.client = _Client(peer)
        self.headers = headers


NGINX = "172.18.0.5"
LAN = "192.168.1.50"
TUNNEL = "192.168.1.42"
CLAIMED = "203.0.113.9"


@pytest.fixture(autouse=True)
def _nginx_is_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "172.18.0.0/16")


def _bucket():
    return f"test-{uuid.uuid4().hex[:8]}"


def test_forged_header_without_cf_ray_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "tunnel_peers", "")
    req = _Req(NGINX, {"x-real-ip": LAN, "cf-connecting-ip": CLAIMED})
    assert bucket_keys(req) == [(LAN, 1)]


def test_unverified_header_charges_the_real_address_too(monkeypatch):
    monkeypatch.setattr(settings, "tunnel_peers", "")
    req = _Req(NGINX, {"x-real-ip": LAN, "cf-connecting-ip": CLAIMED, "cf-ray": "abc-SJC"})
    assert bucket_keys(req) == [(f"{LAN}|{CLAIMED}", 1), (LAN, UNVERIFIED_SHARE)]
    # The best-effort address is still the claimed one; only the limiter
    # refuses to take it on faith.
    assert client_ip(req) == CLAIMED


def test_verified_tunnel_peer_is_keyed_on_the_forwarded_address(monkeypatch):
    monkeypatch.setattr(settings, "tunnel_peers", TUNNEL)
    through_tunnel = _Req(NGINX, {"x-real-ip": TUNNEL, "cf-connecting-ip": CLAIMED, "cf-ray": "x"})
    assert bucket_keys(through_tunnel) == [(CLAIMED, 1)]
    from_lan = _Req(NGINX, {"x-real-ip": LAN, "cf-connecting-ip": CLAIMED, "cf-ray": "x"})
    assert bucket_keys(from_lan) == [(LAN, 1)]


def test_no_header_at_all_is_keyed_on_the_real_address(monkeypatch):
    monkeypatch.setattr(settings, "tunnel_peers", "")
    assert bucket_keys(_Req(NGINX, {"x-real-ip": LAN})) == [(LAN, 1)]
    # Straight to the container, not via nginx: the socket peer is all we have.
    assert bucket_keys(_Req("10.9.8.7", {"x-real-ip": LAN, "cf-connecting-ip": CLAIMED})) == [("10.9.8.7", 1)]


async def test_lan_caller_rotating_forged_headers_is_still_limited(monkeypatch):
    """A LAN caller with no tunnel peers configured forges a fresh
    CF-Connecting-IP (and a CF-Ray) on every request. Each pair bucket is
    new, so those never fill; the real address's bucket must."""
    monkeypatch.setattr(settings, "tunnel_peers", "")
    limit = 2
    dep = rate_limit(_bucket(), limit, 60)
    allowed = limit * UNVERIFIED_SHARE
    for i in range(allowed):
        await dep(_Req(NGINX, {"x-real-ip": LAN, "cf-connecting-ip": f"203.0.113.{i}", "cf-ray": f"ray-{i}"}))
    with pytest.raises(HTTPException) as exc:
        await dep(_Req(NGINX, {"x-real-ip": LAN, "cf-connecting-ip": "203.0.113.200", "cf-ray": "ray-x"}))
    assert exc.value.status_code == 429
    # And the forger has spent its own honest bucket: a plain request from
    # the same LAN address is refused as well.
    with pytest.raises(HTTPException):
        await dep(_Req(NGINX, {"x-real-ip": LAN}))
    # A different LAN address is unaffected.
    await dep(_Req(NGINX, {"x-real-ip": "192.168.1.51"}))


async def test_one_forged_address_repeated_hits_the_normal_limit(monkeypatch):
    monkeypatch.setattr(settings, "tunnel_peers", "")
    limit = 3
    dep = rate_limit(_bucket(), limit, 60)
    headers = {"x-real-ip": LAN, "cf-connecting-ip": CLAIMED, "cf-ray": "ray"}
    for _ in range(limit):
        await dep(_Req(NGINX, headers))
    with pytest.raises(HTTPException):
        await dep(_Req(NGINX, headers))


async def test_verified_tunnel_clients_do_not_share_a_bucket(monkeypatch):
    monkeypatch.setattr(settings, "tunnel_peers", TUNNEL)
    limit = 2
    dep = rate_limit(_bucket(), limit, 60)
    for _ in range(limit):
        await dep(_Req(NGINX, {"x-real-ip": TUNNEL, "cf-connecting-ip": "203.0.113.1", "cf-ray": "r"}))
    with pytest.raises(HTTPException):
        await dep(_Req(NGINX, {"x-real-ip": TUNNEL, "cf-connecting-ip": "203.0.113.1", "cf-ray": "r"}))
    # Another person through the same tunnel still gets in.
    await dep(_Req(NGINX, {"x-real-ip": TUNNEL, "cf-connecting-ip": "203.0.113.2", "cf-ray": "r"}))
    assert ratelimit._HITS  # sanity: the limiter state this test relied on exists
