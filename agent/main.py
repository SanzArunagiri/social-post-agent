"""FastAPI app: REST API (/api/<tool>), MCP server (/mcp), OAuth connect/callback, public media URLs,
the one-time upload page, and the background loops (scheduler, insights, cleanup, token refresh)."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ValidationError, create_model

from . import __version__, crypto, db, insights, media, oauth, pages, posts, tools
from .config import settings
from .net import PlatformError, redact, setup_logging

log = logging.getLogger("agent")

# ======================================================================= MCP

def _mcp_errors(fn):
    """The MCP SDK hides the message of unexpected exceptions; ours are meant for the caller, so pass them
    on as ToolError (redacted). Anything else stays generic."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ValueError, PlatformError) as exc:
            raise ToolError(redact(str(exc))) from None
        except Exception as exc:
            log.error("tool %s failed: %s: %s", fn.__name__, exc.__class__.__name__, redact(str(exc)))
            raise ToolError(f"Internal error ({exc.__class__.__name__}) - see the server log") from None
    return wrapper


def build_mcp() -> MCPServer:
    """A fresh MCP server (its session manager can only run once, so one per app instance)."""
    server = MCPServer(
        name="social-post-agent",
        version=__version__,
        instructions=(
            "Publish and schedule social media posts (Facebook Pages, Instagram, Threads, X, YouTube) with the owner's "
            "own developer apps. Start with list_social_accounts. To attach files: pass public https links, or "
            "upload_media (base64, in parts for big files), or create_upload_link for a person to drop files in a "
            "browser. Check platform limits in create_post_draft results before publishing. Only publish when the "
            "user asked to."),
    )
    for fn in tools.TOOLS:
        name = fn.__name__
        server.add_tool(_mcp_errors(fn), name=name, description=inspect.getdoc(fn), annotations=ToolAnnotations(
            readOnlyHint=name in tools.READ_ONLY, destructiveHint=name in tools.DESTRUCTIVE,
            openWorldHint=name not in tools.READ_ONLY))
    return server


# ======================================================================= rate limiting

class RateLimiter:
    """Sliding-window counter per key (in memory: one container, one process)."""

    def __init__(self) -> None:
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 10000:              # forget idle keys
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > window][:5000]:
                self.hits.pop(k, None)
        return True


limiter = RateLimiter()


def client_ip(scope_or_request) -> str:
    scope = scope_or_request.scope if hasattr(scope_or_request, "scope") else scope_or_request
    return (scope.get("client") or ("?", 0))[0]


# ======================================================================= auth (pure ASGI, so streaming isn't buffered)

PUBLIC_PREFIXES = ("/callback/", "/media/", "/u/", "/connect/")
PUBLIC_PATHS = {"/health", "/favicon.ico"}


def _has_key(headers: dict[bytes, bytes]) -> bool:
    auth = headers.get(b"authorization", b"").decode("latin-1")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get(b"x-api-key", b"").decode("latin-1").strip()
    return crypto.same(key, settings.api_key)


class AuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if settings.mcp_url_key and path.startswith("/mcp/") and crypto.same(path[5:].rstrip("/"), settings.mcp_url_key):
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}          # secret-URL MCP access
            return await self.app(scope, receive, send)
        docs = settings.api_docs and path in ("/docs", "/openapi.json", "/docs/oauth2-redirect")
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or docs:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if _has_key(headers):
            return await self.app(scope, receive, send)
        ip = client_ip(scope)
        status, body = 401, {"error": "Missing or wrong API key - send 'Authorization: Bearer <API_KEY>'"}
        if not limiter.allow(f"authfail:{ip}", 30):
            status, body = 429, {"error": "Too many failed attempts - wait a minute"}
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b'Bearer realm="social-post-agent"')]})
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


# ======================================================================= request size limits + security headers

class _TooLarge(Exception):
    pass


def body_limit(path: str) -> int:
    if path.startswith("/u/") or path == "/api/media":
        return settings.media_max_mb * 1024 * 1024 + 1024 * 1024        # file + multipart overhead
    if path.startswith("/callback/"):
        return 64 * 1024
    return 16 * 1024 * 1024                                              # JSON (base64 parts for upload_media)


class LimitsMiddleware:
    """Caps request bodies before anything parses or spools them, and adds security headers."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, limit = scope.get("path", ""), body_limit(scope.get("path", ""))
        headers = dict(scope.get("headers") or [])
        try:
            declared = int(headers.get(b"content-length", b"0") or 0)
        except ValueError:
            declared = 0
        if declared > limit:
            return await _plain(send, 413, {"error": "Request too large"})
        seen, started = 0, False

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > limit:
                    raise _TooLarge()
            return msg

        async def send_with_headers(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                h = list(msg.get("headers") or [])
                names = {k.lower() for k, _ in h}
                for k, v in ((b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                             (b"referrer-policy", b"no-referrer")):
                    if k not in names:
                        h.append((k, v))
                if path.startswith("/media/") and b"content-security-policy" not in names:
                    h.append((b"content-security-policy", b"default-src 'none'; sandbox"))
                msg = {**msg, "headers": h}
            await send(msg)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _TooLarge:
            if not started:
                await _plain(send, 413, {"error": "Request too large"})


async def _plain(send, status: int, body: dict) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def html_page(content: str, status: int = 200, nonce: str = "") -> HTMLResponse:
    script = f"script-src 'nonce-{nonce}'; connect-src 'self'; " if nonce else ""
    csp = (f"default-src 'none'; {script}style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
           "frame-ancestors 'none'; base-uri 'none'")
    return HTMLResponse(content, status, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store",
                                                  "Referrer-Policy": "no-referrer"})


# ======================================================================= background loops

async def _every(name: str, seconds: float, fn, first_delay: float = 5) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("%s: %s", name, redact(str(exc)))
        await asyncio.sleep(seconds)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    problems = settings.problems()
    if problems:
        for p in problems:
            log.critical("config: %s", p)
        raise RuntimeError("Fix the configuration in .env: " + " | ".join(problems))
    if not settings.public_url:
        log.warning("PUBLIC_URL is not set - OAuth sign-in and Instagram / Threads / Facebook photo posts need it")
    media.media_dir()
    await db.init()
    await posts.recover()
    loops = [_every("scheduler", settings.scheduler_interval_seconds, lambda: posts.run_due(wait=False)),
             _every("media cleanup", 600, media.cleanup, 120),
             _every("token refresh", 12 * 3600, oauth.refresh_due, 60)]
    if settings.insights_refresh_hours > 0:
        loops.append(_every("insights", settings.insights_refresh_hours * 3600, insights.refresh, 90))
    tasks = [asyncio.create_task(c) for c in loops]
    conf = [p for p, ok in settings.configured().items() if ok]
    log.info("social-post-agent %s ready - database: %s, platforms configured: %s, public URL: %s",
             __version__, db.kind(), ", ".join(conf) or "none", settings.public_url or "(not set)")
    async with app.state.mcp.session_manager.run():
        yield
    for t in tasks:
        t.cancel()
    await db.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="social-post-agent", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.api_docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if settings.api_docs else None)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(LimitsMiddleware)          # outermost: size cap applies before auth and parsing


    @app.exception_handler(ValueError)
    async def _value_error(_r: Request, exc: ValueError):
        return JSONResponse({"error": redact(str(exc))}, status_code=400)


    @app.exception_handler(PlatformError)
    async def _platform_error(_r: Request, exc: PlatformError):
        return JSONResponse({"error": redact(str(exc))}, status_code=502)


    @app.exception_handler(Exception)
    async def _unexpected(_r: Request, exc: Exception):
        log.error("unexpected error: %s: %s", exc.__class__.__name__, redact(str(exc)))
        return JSONResponse({"error": f"Internal error ({exc.__class__.__name__}) - see the server log"}, status_code=500)


    # ======================================================================= public routes

    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__}


    @app.get("/connect/{platform}")
    async def connect(platform: str, request: Request, t: str = ""):
        """Start a sign-in. Needs a one-time link from connect_account (?t=...) or the API key header."""
        if not limiter.allow(f"connect:{client_ip(request)}", 20):
            raise HTTPException(429, "Too many requests")
        headers = {k.lower().encode(): v.encode() for k, v in request.headers.items()}
        if not _has_key(headers):
            link = await oauth.take_token("connect", t)
            if not link or link.get("provider") != oauth.provider_of(platform):
                return html_page(pages.result_page("Link expired", "Ask for a new link with connect_account.", False), 403)
        try:
            return RedirectResponse(await oauth.authorize_url(platform), status_code=302)
        except ValueError as exc:
            return html_page(pages.result_page("Can't connect", str(exc), False), 400)


    @app.get("/callback/{provider}")
    async def callback(provider: str, request: Request, code: str = "", state: str = "", error: str = "",
                       error_description: str = ""):
        if not limiter.allow(f"callback:{client_ip(request)}", 30):
            raise HTTPException(429, "Too many requests")
        if error:
            return html_page(pages.result_page("Not connected", (error_description or error)[:300], False), 400)
        try:
            names = await oauth.finish(provider, code, state)
        except (ValueError, PlatformError) as exc:
            log.warning("OAuth callback for %s failed: %s", provider, exc)
            return html_page(pages.result_page("Not connected", redact(str(exc)), False), 400)
        return html_page(pages.result_page("Connected", "You can close this page.", True, names))


    @app.post("/callback/{provider}/{event}")
    async def meta_deauthorize(provider: str, event: str, request: Request):
        """Meta's "Uninstall" / "Delete" callback URLs (required by the Threads use case settings)."""
        if provider not in ("facebook", "threads") or event not in ("uninstall", "delete"):
            raise HTTPException(404, "Not found")
        form = await request.form()
        try:
            code = await oauth.meta_callback(provider, str(form.get("signed_request") or ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if event == "delete":
            return {"url": f"{settings.public_url}/health", "confirmation_code": code}
        return {"ok": True}

    @app.api_route("/media/{name}", methods=["GET", "HEAD"])
    async def media_file(name: str):
        f = await media.file_for(name)
        if not f:
            raise HTTPException(404, "Not found")
        path, mime = f
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "public, max-age=3600",
                                                            "X-Robots-Tag": "noindex"})


    @app.get("/u/{tok}")
    async def upload_page(tok: str, request: Request):
        if not limiter.allow(f"u-get:{client_ip(request)}", 60):
            raise HTTPException(429, "Too many requests")
        link = await media.get_link(tok)
        if not link:
            return html_page(pages.result_page("Link expired", "Ask for a new upload link.", False), 410)
        nonce = crypto.new_token(12)
        return html_page(pages.upload_page(link.get("note") or "", settings.upload_link_max_files, nonce), nonce=nonce)


    @app.post("/u/{tok}")
    async def upload_page_post(tok: str, request: Request):
        # rate limit and token check come first - nothing is read or spooled for a bad / expired link
        ip = client_ip(request)
        if not limiter.allow(f"u-post:{ip}", settings.upload_rate_per_minute) or \
                not limiter.allow(f"u-tok:{crypto.token_hash(tok)}", settings.upload_rate_per_minute):
            return JSONResponse({"error": "Too many uploads - wait a minute"}, status_code=429)
        link = await media.get_link(tok)
        if not link:
            return JSONResponse({"error": "This upload link expired"}, status_code=410)
        if len(link["media"] or []) >= settings.upload_link_max_files:
            return JSONResponse({"error": f"{settings.upload_link_max_files} files per link"}, status_code=422)
        try:
            form = await request.form(max_files=1, max_fields=2)
        except Exception:
            return JSONResponse({"error": "Send one file as multipart form-data (field 'file')"}, status_code=400)
        try:
            file = form.get("file")
            if not hasattr(file, "read"):
                return JSONResponse({"error": "Send one file as multipart form-data (field 'file')"}, status_code=400)
            try:
                m = await media.save_upload(file)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=422)
        finally:
            await form.close()
        await media.link_add(tok, m)
        return m

    # ======================================================================= REST API (API key required)

    @app.post("/api/media")
    async def api_upload(file: UploadFile = File(...)):
        """Multipart upload (streams to disk). Returns the media id."""
        return await media.save_upload(file)


    def _rest(app: FastAPI, fn) -> None:
        sig = inspect.signature(fn, eval_str=True)
        fields: dict[str, Any] = {}
        for n, p in sig.parameters.items():
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else Any
            fields[n] = (ann, ... if p.default is inspect.Parameter.empty else p.default)
        Model = create_model(f"{fn.__name__}_args", **fields)
        list_fields = {n for n, f in Model.model_fields.items() if "list" in str(f.annotation)}

        async def call(data: dict) -> Any:
            try:
                args = Model.model_validate(data)
            except ValidationError as exc:
                return JSONResponse({"error": "Invalid arguments", "details": json.loads(exc.json(include_url=False))},
                                    status_code=422)
            return await fn(**{n: getattr(args, n) for n in fields})

        async def post_ep(request: Request):
            raw = await request.body()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return JSONResponse({"error": "Body must be JSON"}, status_code=400)
            if not isinstance(data, dict):
                return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)
            return await call(data)

        post_ep.__doc__ = inspect.getdoc(fn)
        app.add_api_route(f"/api/{fn.__name__}", post_ep, methods=["POST"], name=fn.__name__, summary=fn.__name__,
                          description=inspect.getdoc(fn), openapi_extra={"requestBody": {"content": {"application/json": {
                              "schema": Model.model_json_schema()}}}})
        if fn.__name__ in tools.READ_ONLY:
            async def get_ep(request: Request):
                q = request.query_params
                data = {k: (q.getlist(k) if k in list_fields else q[k]) for k in q.keys()}
                return await call(data)
            app.add_api_route(f"/api/{fn.__name__}", get_ep, methods=["GET"], name=fn.__name__ + "_get",
                              summary=fn.__name__, description=inspect.getdoc(fn))


    for fn in tools.TOOLS:
        _rest(app, fn)

    # MCP streamable HTTP endpoint at /mcp (its routes are added to this app so the auth middleware covers them)
    mcp = build_mcp()
    app.state.mcp = mcp
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    for route in mcp_app.routes:
        app.router.routes.append(route)
    return app


app = create_app()
