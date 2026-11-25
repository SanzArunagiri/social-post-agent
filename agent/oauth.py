"""OAuth sign-in with your own developer apps and automatic token refresh.

GET /connect/{platform} -> the provider's consent screen -> GET /callback/{provider}.
Facebook and Instagram share one Meta sign-in (provider "facebook"): every Page you pick is stored,
plus the Instagram professional account linked to each Page.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

from sqlalchemy import delete, insert, select

from . import accounts, crypto, db
from .config import settings
from .net import PlatformError, client, ok

THREADS = "https://graph.threads.net/v1.0"
X_API = "https://api.x.com/2"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
PROVIDER = {"facebook": "facebook", "instagram": "facebook", "meta": "facebook",
            "threads": "threads", "x": "x", "twitter": "x", "youtube": "youtube", "google": "youtube"}
PROVIDERS = ("facebook", "threads", "x", "youtube")

META_SCOPES = ("pages_show_list,pages_read_engagement,pages_manage_posts,business_management,read_insights,"
               "instagram_basic,instagram_content_publish,instagram_manage_insights")
THREADS_SCOPES = "threads_basic,threads_content_publish,threads_manage_insights"
X_SCOPES = "tweet.read tweet.write users.read offline.access media.write"
YOUTUBE_SCOPES = ("https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
                  "https://www.googleapis.com/auth/youtube.force-ssl")


def graph() -> str:
    return f"https://graph.facebook.com/{settings.meta_graph_version}"


def provider_of(platform: str) -> str:
    p = PROVIDER.get((platform or "").lower())
    if not p:
        raise ValueError(f"Unknown platform '{platform}' - use facebook, instagram, threads, x or youtube")
    return p


def redirect_uri(provider: str) -> str:
    return f"{settings.public_url}/callback/{provider}"


# ------------------------------------------------------------------ short-lived tokens (state, connect links)

async def put_token(kind: str, data: dict, ttl: float) -> str:
    tok = crypto.new_token()
    now = time.time()
    async with db.engine().begin() as c:
        await c.execute(delete(db.tokens).where(db.tokens.c.expires < now))
        await c.execute(insert(db.tokens).values(id=crypto.token_hash(tok), kind=kind,
                                                 data=crypto.encrypt_json(data), expires=now + ttl))
    return tok


async def take_token(kind: str, tok: str) -> dict | None:
    """Single use: the token is deleted when read."""
    if not tok:
        return None
    h = crypto.token_hash(tok)
    async with db.engine().begin() as c:
        r = (await c.execute(select(db.tokens).where(db.tokens.c.id == h, db.tokens.c.kind == kind))).first()
        if not r:
            return None
        # the delete decides who wins if two requests race with the same token
        if not (await c.execute(delete(db.tokens).where(db.tokens.c.id == h))).rowcount:
            return None
    if r.expires < time.time():
        return None
    return crypto.decrypt_json(r.data)


async def connect_link(platform: str) -> dict:
    """A one-time link (15 min) that starts the sign-in in a browser, so the API key never goes in a URL."""
    provider = provider_of(platform)
    _require(provider)
    tok = await put_token("connect", {"provider": provider}, 900)
    return {"platform": platform, "provider": provider, "url": f"{settings.public_url}/connect/{platform}?t={tok}",
            "expires_in_minutes": 15, "redirect_uri": redirect_uri(provider)}


def _require(provider: str) -> None:
    conf = settings.configured()
    if not conf.get(provider):
        need = {"facebook": "META_APP_ID and META_APP_SECRET", "threads": "THREADS_APP_ID and THREADS_APP_SECRET",
                "x": "X_CLIENT_ID", "youtube": "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"}[provider]
        raise ValueError(f"Set {need} in .env first (see README)")
    if not settings.public_url:
        raise ValueError("Set PUBLIC_URL in .env first - the platforms redirect back to it")


async def authorize_url(platform: str) -> str:
    """The provider's consent-screen URL (state stored for 30 minutes)."""
    provider = provider_of(platform)
    _require(provider)
    data: dict = {"provider": provider}
    if provider == "x":
        data["verifier"] = secrets.token_urlsafe(48)
    state = await put_token("state", data, 1800)
    ru = redirect_uri(provider)
    if provider == "facebook":
        return f"https://www.facebook.com/{settings.meta_graph_version}/dialog/oauth?" + urlencode(
            {"client_id": settings.meta_app_id, "redirect_uri": ru, "state": state, "scope": META_SCOPES,
             "response_type": "code"})
    if provider == "threads":
        return "https://threads.net/oauth/authorize?" + urlencode(
            {"client_id": settings.threads_app_id, "redirect_uri": ru, "state": state, "response_type": "code",
             "scope": THREADS_SCOPES})
    if provider == "x":
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data["verifier"].encode()).digest()).decode().rstrip("=")
        return "https://x.com/i/oauth2/authorize?" + urlencode(
            {"response_type": "code", "client_id": settings.x_client_id, "redirect_uri": ru, "state": state,
             "scope": X_SCOPES, "code_challenge": challenge, "code_challenge_method": "S256"})
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {"client_id": settings.google_client_id, "redirect_uri": ru, "state": state, "response_type": "code",
         "scope": YOUTUBE_SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"})


def _x_auth() -> dict:
    """Confidential X clients authenticate the token call with Basic auth; public clients send client_id only."""
    return {"auth": (settings.x_client_id, settings.x_client_secret)} if settings.x_client_secret else {}


async def finish(provider: str, code: str, state: str) -> list[str]:
    """OAuth callback: exchange the code, store the account(s). Returns what was connected."""
    st = await take_token("state", state)
    if not st or st.get("provider") != provider:
        raise ValueError("This sign-in link expired or was already used - start again with connect_account")
    if not code:
        raise ValueError("The provider didn't return a code")
    ru = redirect_uri(provider)
    async with client() as c:
        if provider == "facebook":
            g = graph()
            short = ok(await c.get(f"{g}/oauth/access_token", params={
                "client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret,
                "redirect_uri": ru, "code": code}))["access_token"]
            user_token = ok(await c.get(f"{g}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret, "fb_exchange_token": short}))["access_token"]
            # Page tokens obtained from a long-lived user token do not expire
            pages = ok(await c.get(f"{g}/me/accounts", params={
                "access_token": user_token, "limit": 100,
                "fields": "id,name,access_token,instagram_business_account{id,username}"})).get("data", [])
            names = []
            for p in pages:
                await accounts.save("facebook", p["id"], p["name"], p["access_token"], meta={"page_id": p["id"]})
                names.append(f"Facebook: {p['name']}")
                ig = p.get("instagram_business_account")
                if ig:
                    await accounts.save("instagram", ig["id"], ig.get("username") or p["name"], p["access_token"],
                                        meta={"page_id": p["id"]})
                    names.append(f"Instagram: @{ig.get('username', ig['id'])}")
            if not names:
                raise ValueError("No Facebook Pages were shared - pick your Page(s) on Meta's consent screen")
            return names
        if provider == "threads":
            d = ok(await c.post("https://graph.threads.net/oauth/access_token", data={
                "client_id": settings.threads_app_id, "client_secret": settings.threads_app_secret, "code": code,
                "grant_type": "authorization_code", "redirect_uri": ru}))
            ll = ok(await c.get("https://graph.threads.net/access_token", params={
                "grant_type": "th_exchange_token", "client_secret": settings.threads_app_secret,
                "access_token": d["access_token"]}))
            me = ok(await c.get(f"{THREADS}/me", params={"fields": "id,username", "access_token": ll["access_token"]}))
            await accounts.save("threads", me["id"], me.get("username") or me["id"], ll["access_token"],
                                expires_at=time.time() + int(ll.get("expires_in") or 5184000))
            return [f"Threads: @{me.get('username')}"]
        if provider == "x":
            d = ok(await c.post(f"{X_API}/oauth2/token", data={
                "code": code, "grant_type": "authorization_code", "redirect_uri": ru,
                "code_verifier": st.get("verifier", ""), "client_id": settings.x_client_id}, **_x_auth()))
            me = ok(await c.get(f"{X_API}/users/me", headers={"Authorization": f"Bearer {d['access_token']}"}))["data"]
            await accounts.save("x", me["id"], me.get("username") or me["id"], d["access_token"], d.get("refresh_token"),
                                time.time() + int(d.get("expires_in") or 7200))
            return [f"X: @{me.get('username')}"]
        if provider == "youtube":
            d = ok(await c.post(GOOGLE_TOKEN, data={
                "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "redirect_uri": ru, "grant_type": "authorization_code"}))
            items = ok(await c.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                                   headers={"Authorization": f"Bearer {d['access_token']}"})).get("items") or []
            if not items:
                raise ValueError("That Google account has no YouTube channel")
            ch = items[0]
            await accounts.save("youtube", ch["id"], ch["snippet"]["title"], d["access_token"], d.get("refresh_token"),
                                time.time() + int(d.get("expires_in") or 3600))
            return [f"YouTube: {ch['snippet']['title']}"]
    raise ValueError(f"Unknown provider {provider}")


async def access_token(acc: dict) -> str:
    """A valid access token for the account, refreshed (and stored) when it is close to expiring.
    X tokens live 2 h, Google 1 h (both have refresh tokens); Threads long-lived tokens live 60 days and
    are refreshed in their last week; Facebook / Instagram Page tokens don't expire."""
    exp, p = acc.get("expires_at") or 0, acc["platform"]
    try:
        if p in ("x", "youtube") and exp and exp - time.time() < 300:
            if not acc.get("refresh_token"):
                raise ValueError(f"The {p} sign-in expired - connect the account again")
            async with client() as c:
                if p == "x":
                    r = await c.post(f"{X_API}/oauth2/token", data={
                        "grant_type": "refresh_token", "client_id": settings.x_client_id,
                        "refresh_token": acc["refresh_token"]}, **_x_auth())
                else:
                    r = await c.post(GOOGLE_TOKEN, data={
                        "grant_type": "refresh_token", "client_id": settings.google_client_id,
                        "client_secret": settings.google_client_secret, "refresh_token": acc["refresh_token"]})
                d = ok(r)
            acc.update(token=d["access_token"], refresh_token=d.get("refresh_token") or acc["refresh_token"],
                       expires_at=time.time() + int(d.get("expires_in") or 3600))
            await accounts.update_tokens(acc["id"], acc["token"], acc["refresh_token"], acc["expires_at"])
        elif p == "threads" and exp and exp - time.time() < 7 * 86400:
            async with client() as c:
                d = ok(await c.get("https://graph.threads.net/refresh_access_token",
                                   params={"grant_type": "th_refresh_token", "access_token": acc["token"]}))
            acc.update(token=d["access_token"], expires_at=time.time() + int(d.get("expires_in") or 5184000))
            await accounts.update_tokens(acc["id"], acc["token"], None, acc["expires_at"])
    except (PlatformError, ValueError) as exc:
        await accounts.set_error(acc["id"], f"token refresh failed: {exc}")
        raise
    return acc["token"]


async def refresh_due() -> list[str]:
    """Background: Threads long-lived tokens die after 60 days unless refreshed - keep them alive even when
    nothing is posted. (X and Google refresh on demand: their refresh tokens don't lapse that way.)"""
    done = []
    for a in await accounts.list_accounts():
        exp = a.get("token_expires_at") or 0
        if a["platform"] == "threads" and exp and exp - time.time() < 7 * 86400:
            try:
                await access_token(await accounts.get(a["id"]))
                done.append(a["id"])
            except Exception:
                pass
    return done


# ------------------------------------------------------------------ Meta uninstall / data-deletion callbacks

def parse_signed_request(signed_request: str, secret: str) -> dict:
    """Meta's signed_request (HMAC-SHA256 with the app secret) -> payload, or ValueError."""
    try:
        sig_b64, payload_b64 = (signed_request or "").split(".", 1)
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        payload = base64.urlsafe_b64decode(pad(payload_b64))
    except Exception as exc:
        raise ValueError("Malformed signed_request") from exc
    if not secret or not hmac.compare_digest(sig, hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()):
        raise ValueError("Bad signature")
    return json.loads(payload)


async def meta_callback(provider: str, signed_request: str) -> str:
    """A person removed the app (or asked for their data to be deleted): forget their Threads account.
    (For Facebook the payload carries an app-scoped user id that doesn't map to Pages - Page tokens simply
    stop working and the next publish reports it.) Returns a confirmation code."""
    secret = settings.threads_app_secret if provider == "threads" else settings.meta_app_secret
    data = parse_signed_request(signed_request, secret)
    uid = str(data.get("user_id") or "")
    if provider == "threads" and uid:
        await accounts.disconnect(f"threads:{uid}")
    return hashlib.sha256(f"{provider}:{uid}".encode()).hexdigest()[:16]
