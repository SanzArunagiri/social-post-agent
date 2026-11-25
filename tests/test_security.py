"""Regression tests for the security review: upload limits, stored XSS, SSRF, one-time tokens, headers."""
from __future__ import annotations

import asyncio
import re
import socket

import pytest

from agent import config, media, net, oauth

from .conftest import JPEG
from .test_api import AUTH, client  # noqa: F401  (fixture)

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


async def test_svg_and_html_are_rejected_whatever_they_claim(env):
    for data, name in ((SVG, "x.svg"), (b"<html><script>alert(1)</script>", "x.jpg")):
        with pytest.raises(ValueError, match="Only JPEG, PNG"):
            await media.save_bytes(data, name, "image/svg+xml")


def test_media_served_with_sandbox_csp_and_nosniff(client):  # noqa: F811
    mid = client.post("/api/media", headers=AUTH, files={"file": ("a.jpg", JPEG, "image/jpeg")}).json()["id"]
    r = client.get(f"/media/{mid}.jpg")
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_bad_upload_link_is_refused_before_the_body_is_read(client):  # noqa: F811
    r = client.post("/u/not-a-real-token-xxxxxxxx", content=b"garbage",
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 410


def test_oversized_requests_get_413(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config.settings, "media_max_mb", 1)
    tok = client.post("/api/create_upload_link", headers=AUTH, json={}).json()["token"]
    big = JPEG + b"\x00" * (3 * 1024 * 1024)
    assert client.post(f"/u/{tok}", files={"file": ("a.jpg", big, "image/jpeg")}).status_code == 413

    def chunks():                                            # no Content-Length: counted while streaming
        for _ in range(40):
            yield b"x" * (512 * 1024)
    r = client.post("/api/create_post_draft", headers={**AUTH, "content-type": "application/json"}, content=chunks())
    assert r.status_code == 413
    assert client.post("/callback/threads/delete", content=b"x" * 70000,
                       headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413


def test_upload_page_csp_uses_a_nonce(client):  # noqa: F811
    tok = client.post("/api/create_upload_link", headers=AUTH, json={}).json()["token"]
    r = client.get(f"/u/{tok}")
    nonce = re.search(r"'nonce-([^']+)'", r.headers["content-security-policy"]).group(1)
    assert f'<script nonce="{nonce}">' in r.text and "frame-ancestors 'none'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "100.64.1.1", "192.168.1.1",
                                "::1", "fd00::1", "::ffff:10.0.0.1", "0.0.0.0"])
def test_ssrf_guard_blocks_internal_addresses(monkeypatch, ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(fam, socket.SOCK_STREAM, 6, "", (ip, 443))])
    with pytest.raises(ValueError, match="internal address"):
        net._public_ip("evil.example", 443)


def test_ssrf_guard_pins_the_checked_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(net, "transport", None)
    url, headers, ext = asyncio.run(net.pinned_request("https://cdn.example.com/a.jpg?x=1"))
    assert url == "https://93.184.216.34/a.jpg?x=1"
    assert headers == {"Host": "cdn.example.com"} and ext == {"sni_hostname": "cdn.example.com"}


async def test_one_time_tokens_work_once_even_when_raced(env):
    tok = await oauth.put_token("connect", {"provider": "x"}, 60)
    results = await asyncio.gather(*(oauth.take_token("connect", tok) for _ in range(5)))
    assert sum(r is not None for r in results) == 1
