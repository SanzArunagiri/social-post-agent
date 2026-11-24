"""HTTP helpers shared by every platform module: one client factory (tests inject a mock transport),
uniform error parsing, an SSRF guard for user-supplied URLs, and log redaction."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

import httpx

from .config import settings

# Tests set this to httpx.MockTransport(handler); every outgoing call goes through _client().
transport: httpx.AsyncBaseTransport | None = None


class PlatformError(RuntimeError):
    """A platform API refused the call. The message is safe to show and to log."""


def client(timeout: float = 60, follow_redirects: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20), transport=transport,
                             follow_redirects=follow_redirects,
                             headers={"User-Agent": "social-post-agent/1.0 (+https://github.com/SanzArunagiri/social-post-agent)"})


def ok(r: httpx.Response) -> dict:
    """JSON body of a successful response, else PlatformError with the platform's own message."""
    try:
        d = r.json()
    except Exception:
        d = {"error": r.text[:300]}
    failed = r.status_code >= 300 or (isinstance(d, dict) and d.get("error") and not d.get("id") and not d.get("data"))
    if failed:
        msg = _message(d) if isinstance(d, dict) else str(d)
        raise PlatformError(f"{r.request.url.host} {r.status_code}: {redact(str(msg))[:400]}")
    return d if isinstance(d, dict) else {"data": d}


def _message(d: dict) -> str:
    err = d.get("error")
    if isinstance(err, dict):
        parts = [err.get("error_user_msg") or err.get("message") or "", err.get("error_user_title") or ""]
        msg = " - ".join(p for p in parts if p) or str(err)
        if err.get("code") is not None:
            msg += f" (code {err.get('code')}{'/' + str(err['error_subcode']) if err.get('error_subcode') else ''})"
        return msg
    if isinstance(d.get("errors"), list) and d["errors"]:                     # X API v2
        e = d["errors"][0]
        return str(e.get("message") or e.get("detail") or e)
    return str(d.get("detail") or d.get("title") or d.get("error_description") or err or d)


# ------------------------------------------------------------------ SSRF guard

def _check_host(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Only public http(s) links can be used")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Can't resolve {u.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped.is_private):
            raise ValueError(f"{u.hostname} points to an internal address - not allowed")


async def check_public_url(url: str) -> None:
    if transport is not None:                    # tests: no DNS
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("Only public http(s) links can be used")
        return
    await asyncio.to_thread(_check_host, url)


def drive_direct(url: str) -> str:
    """Google Drive / Dropbox share links -> direct download links (files shared as 'anyone with the link')."""
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:.*&)?id=)([A-Za-z0-9_\-]{10,})", url)
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    if "dropbox.com" in url:
        return re.sub(r"([?&])dl=0", r"\1dl=1", url) if "dl=0" in url else url + ("&" if "?" in url else "?") + "dl=1"
    return url


# ------------------------------------------------------------------ log redaction

_PATTERNS = [
    (re.compile(r"(?i)\b(access_token|refresh_token|client_secret|fb_exchange_token|input_token|code|code_verifier|"
                r"assertion|token|key|t)=([^&\s\"']+)"), r"\1=[redacted]"),
    (re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"/u/[A-Za-z0-9_\-]{12,}"), "/u/[redacted]"),
    (re.compile(r"/bot\d+:[A-Za-z0-9_\-]+"), "/bot[redacted]"),
]


def redact(text: str) -> str:
    for rx, rep in _PATTERNS:
        text = rx.sub(rep, text)
    for s in settings.secret_values():
        text = text.replace(s, "[redacted]")
    return text


class RedactingFilter(logging.Filter):
    """Redacts the message and its arguments in place (formatters such as uvicorn's access log rely on the
    args keeping their shape)."""

    @staticmethod
    def _clean(a):
        if isinstance(a, str):
            return redact(a)
        if isinstance(a, BaseException):
            return redact(str(a))
        return a

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self._clean(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._clean(v) for k, v in record.args.items()}
        except Exception:
            pass
        return True


def setup_logging() -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    f = RedactingFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "agent", "httpx", "mcp"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
    # httpx logs every request URL (Graph API calls carry access_token in the query) - keep it quiet
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
