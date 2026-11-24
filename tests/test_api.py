"""HTTP surface: auth, REST tools, MCP over streamable HTTP, OAuth connect flow, media URLs, upload links."""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from agent import config, crypto, db, main, net

from .conftest import JPEG

KEY = "test-api-key-0123456789"
AUTH = {"Authorization": f"Bearer {KEY}"}
MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client(mock, monkeypatch):
    monkeypatch.setattr(config.settings, "meta_app_id", "app-id")
    monkeypatch.setattr(config.settings, "meta_app_secret", "app-secret-value")
    monkeypatch.setattr(config.settings, "x_client_id", "x-client")
    main.limiter.hits.clear()
    import asyncio
    asyncio.run(db.dispose())
    with TestClient(main.create_app(), base_url="https://agent.example.com") as c:
        yield c
    asyncio.run(db.dispose())


def test_auth_required_everywhere_except_public_paths(client):
    assert client.get("/health").json()["ok"] is True
    assert client.post("/api/list_social_accounts").status_code == 401
    assert client.post("/api/list_social_accounts", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    r = client.post("/api/list_social_accounts", headers={"X-API-Key": KEY})
    assert r.status_code == 200 and r.json()["accounts"] == []
    assert r.json()["redirect_uris"]["facebook"] == "https://agent.example.com/callback/facebook"
    assert client.get("/docs").status_code == 401                # docs off unless API_DOCS=true


def test_failed_auth_is_rate_limited(client):
    codes = [client.get("/api/get_scheduled_posts", headers={"Authorization": "Bearer nope"}).status_code for _ in range(32)]
    assert codes[0] == 401 and codes[-1] == 429


def test_rest_validation_and_errors(client):
    r = client.post("/api/get_publishing_history", headers=AUTH, json={"limit": 0})
    assert r.status_code == 422
    r = client.post("/api/create_post_draft", headers=AUTH, json={"text": "x", "targets": ["facebook"]})
    assert r.status_code == 400 and "Not a connected account" in r.json()["error"]
    r = client.get("/api/get_publishing_history?limit=5&status=posted", headers=AUTH)
    assert r.status_code == 200 and r.json() == []


def test_mcp_lists_and_calls_tools(client):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = client.post("/mcp", headers=MCP_HEADERS, json=init)
    assert r.status_code == 200, r.text
    r = client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"list_social_accounts", "create_post_draft", "publish_facebook_post", "publish_instagram_post",
            "publish_threads_post", "publish_x_post", "upload_youtube_video", "schedule_post", "get_scheduled_posts",
            "get_publishing_history", "get_social_insights", "send_telegram_notification", "upload_media",
            "create_upload_link"} <= names
    r = client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                       "params": {"name": "list_social_accounts", "arguments": {}}})
    assert json.loads(r.json()["result"]["content"][0]["text"])["accounts"] == []


def test_mcp_secret_url(client, monkeypatch):
    monkeypatch.setattr(config.settings, "mcp_url_key", "s" * 32)
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    h = {k: v for k, v in MCP_HEADERS.items() if k != "Authorization"}
    assert client.post("/mcp/" + "s" * 32, headers=h, json=body).status_code == 200
    assert client.post("/mcp/" + "x" * 32, headers=h, json=body).status_code == 401


def test_oauth_connect_flow_facebook(client, mock):
    link = client.post("/api/connect_account", headers=AUTH, json={"platform": "instagram"}).json()
    assert link["provider"] == "facebook" and "/connect/instagram?t=" in link["url"]
    path = link["url"].replace("https://agent.example.com", "")
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == ["https://agent.example.com/callback/facebook"]
    assert "instagram_content_publish" in q["scope"][0]
    assert client.get(path, follow_redirects=False).status_code == 403          # one-time link

    mock.on("GET", r"/oauth/access_token$", lambda c: {"access_token": "short" if "code" in c.params else "long-user"})
    mock.on("GET", r"/me/accounts$", {"data": [{"id": "PAGE1", "name": "Acme", "access_token": "page-token-secret",
                                                "instagram_business_account": {"id": "IG1", "username": "acme"}}]})
    r = client.get(f"/callback/facebook?code=abc&state={q['state'][0]}")
    assert r.status_code == 200 and "Instagram: @acme" in r.text
    assert client.get(f"/callback/facebook?code=abc&state={q['state'][0]}").status_code == 400      # state used up

    accs = client.get("/api/list_social_accounts", headers=AUTH).json()["accounts"]
    assert {a["id"] for a in accs} == {"facebook:PAGE1", "instagram:IG1"}
    assert "page-token-secret" not in json.dumps(accs)

    import asyncio

    from sqlalchemy import select

    async def raw():
        async with db.engine().connect() as c:
            return (await c.execute(select(db.accounts.c.token))).scalars().all()
    stored = asyncio.run(raw())
    asyncio.run(db.dispose())
    assert all(t != "page-token-secret" and crypto.decrypt(t) == "page-token-secret" for t in stored)


def test_x_authorize_uses_pkce(client):
    link = client.post("/api/connect_account", headers=AUTH, json={"platform": "x"}).json()
    r = client.get(link["url"].replace("https://agent.example.com", ""), follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) == 43
    assert "offline.access" in q["scope"][0]


def test_upload_link_and_public_media(client):
    link = client.post("/api/create_upload_link", headers=AUTH, json={"note": "<b>for the launch</b>"}).json()
    page = client.get(f"/u/{link['token']}")
    assert page.status_code == 200 and "<b>for the launch" not in page.text
    r = client.post(f"/u/{link['token']}", files={"file": ("a.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    got = client.get("/api/get_uploaded_media", params={"token": link["token"]}, headers=AUTH).json()
    assert [m["id"] for m in got["media"]] == [mid]

    f = client.get(f"/media/{mid}.jpg")                       # public, no key
    assert f.status_code == 200 and f.content == JPEG and f.headers["content-type"] == "image/jpeg"
    assert client.get("/media/" + "0" * 32 + ".jpg").status_code == 404
    assert client.get("/media/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.post(f"/u/{link['token']}", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 422
    assert client.get("/u/not-a-real-token-123456").status_code == 410


def test_upload_link_rate_limit(client, monkeypatch):
    monkeypatch.setattr(config.settings, "upload_rate_per_minute", 3)
    tok = client.post("/api/create_upload_link", headers=AUTH, json={}).json()["token"]
    codes = [client.post(f"/u/{tok}", files={"file": ("a.jpg", JPEG, "image/jpeg")}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_multipart_upload_and_size_limit(client, monkeypatch):
    r = client.post("/api/media", headers=AUTH, files={"file": ("a.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 200 and r.json()["url"].startswith("https://agent.example.com/media/")
    monkeypatch.setattr(config.settings, "media_max_mb", 0)
    r = client.post("/api/media", headers=AUTH, files={"file": ("a.jpg", JPEG, "image/jpeg")})
    assert r.status_code == 400 and "MEDIA_MAX_MB" in r.json()["error"]


def test_telegram_notification(client, mock, monkeypatch):
    monkeypatch.setattr(config.settings, "telegram_bot_token", "123456:ABCdefSECRET")
    monkeypatch.setattr(config.settings, "telegram_chat_id", "42")
    mock.on("POST", r"api.telegram.org/bot.*/sendMessage$", {"ok": True, "result": {"message_id": 7}})
    r = client.post("/api/send_telegram_notification", headers=AUTH, json={"text": "hi"})
    assert r.json() == {"sent": True, "message_id": 7}
    assert mock.find("POST", "sendMessage$")[0].json()["chat_id"] == "42"


def test_secrets_are_redacted_from_logs(monkeypatch, caplog):
    monkeypatch.setattr(config.settings, "meta_app_secret", "super-secret-app-value")
    f = net.RedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, "", 0, "GET https://graph.facebook.com/x?access_token=EAAB123&"
                            "client_secret=super-secret-app-value Authorization: Bearer abcdefghijkl /u/abcdefghijklmnop", (), None)
    f.filter(rec)
    msg = rec.getMessage()
    assert "EAAB123" not in msg and "super-secret-app-value" not in msg and "abcdefghijkl" not in msg
    assert "count=5" in net.redact("count=5")


def test_threads_uninstall_callback_forgets_account(client, monkeypatch):
    import base64
    import hashlib
    import hmac
    monkeypatch.setattr(config.settings, "threads_app_secret", "threads-secret-value")
    import asyncio

    from .conftest import add_account
    asyncio.run(add_account("threads", "777", "acme"))
    asyncio.run(db.dispose())
    payload = base64.urlsafe_b64encode(json.dumps({"user_id": "777", "algorithm": "HMAC-SHA256"}).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(b"threads-secret-value", payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    assert client.post("/callback/threads/uninstall", data={"signed_request": f"{sig}x.{payload}"}).status_code == 400
    r = client.post("/callback/threads/delete", data={"signed_request": f"{sig}.{payload}"})
    assert r.status_code == 200 and r.json()["confirmation_code"]
    assert client.get("/api/list_social_accounts", headers=AUTH).json()["accounts"] == []
