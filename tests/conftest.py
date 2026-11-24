"""Test setup: a fresh data dir + SQLite DB per test, and every outgoing HTTP call routed to a mock."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field

import httpx
import pytest

os.environ.update(API_KEY="test-api-key-0123456789", ENCRYPTION_KEY="test-encryption-key-0123456789",
                  PUBLIC_URL="https://agent.example.com", DATA_DIR=tempfile.mkdtemp(prefix="spa-"),
                  INSIGHTS_REFRESH_HOURS="0", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")

from agent import config, db, net, publishers  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 3000


@dataclass
class Call:
    method: str
    url: str
    params: dict
    headers: dict
    body: bytes

    def form(self) -> dict:
        from urllib.parse import parse_qsl
        return dict(parse_qsl(self.body.decode(errors="replace")))

    def json(self) -> dict:
        return json.loads(self.body or b"{}")


@dataclass
class Mock:
    """Route table: (METHOD, regex on 'host/path') -> response (dict = JSON 200, httpx.Response, or callable)."""
    routes: list = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def on(self, method: str, pattern: str, response) -> "Mock":
        self.routes.insert(0, (method.upper(), re.compile(pattern), response))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        call = Call(request.method, str(request.url), dict(request.url.params), dict(request.headers), body)
        self.calls.append(call)
        target = f"{request.url.host}{request.url.path}"
        for method, rx, resp in self.routes:
            if method == request.method and rx.search(target):
                if callable(resp):
                    resp = resp(call)
                if isinstance(resp, httpx.Response):
                    return resp
                return httpx.Response(200, json=resp)
        return httpx.Response(404, json={"error": {"message": f"no mock for {request.method} {target}"}})

    def find(self, method: str, pattern: str) -> list[Call]:
        rx = re.compile(pattern)
        return [c for c in self.calls if c.method == method.upper() and rx.search(httpx.URL(c.url).host + httpx.URL(c.url).path)]


@pytest.fixture
def mock(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.reload()
    m = Mock()
    net.transport = httpx.MockTransport(m.handler)
    publishers.POLL_SECONDS = 0
    yield m
    net.transport = None


@pytest.fixture
async def env(mock):
    await db.dispose()
    await db.init()
    yield mock
    await db.dispose()


async def add_account(platform: str, account_id: str = "1", name: str = "acme", **kw) -> str:
    from agent import accounts
    meta = kw.pop("meta", {"page_id": "PAGE1"} if platform in ("facebook", "instagram") else {})
    return await accounts.save(platform, account_id, name, kw.pop("token", f"{platform}-token"),
                               kw.pop("refresh", None), kw.pop("expires_at", None), meta)


async def add_media(data: bytes = JPEG, name: str = "photo.jpg") -> dict:
    from agent import media
    return await media.save_bytes(data, name)
