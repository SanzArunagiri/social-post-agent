"""Media files: stored in the data volume and served at an unguessable public URL, because Instagram,
Threads and Facebook photo posts fetch media by URL.

Ways in: multipart upload (REST), base64 in one go or in chunks (MCP clients can't stream files),
an https link (downloaded with an SSRF guard), or a one-time upload link a person opens in a browser.
A file is deleted only when every post that uses it is finished on every platform (see deletable()).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
import time
from pathlib import Path

import httpx
from sqlalchemy import delete, insert, select, update

from . import crypto, db
from .config import settings
from .net import client, drive_direct, pinned_request

log = logging.getLogger("agent.media")
ID_RX = re.compile(r"^[0-9a-f]{32}$")
ACTIVE = ("draft", "scheduled", "posting", "partial", "failed")       # posts that may still need their files


def max_bytes() -> int:
    return settings.media_max_mb * 1024 * 1024


def media_dir() -> Path:
    d = settings.media_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def sniff(head: bytes) -> str:
    """Real type from the first bytes (Instagram only takes JPEG, so a .jpg that is really a PNG matters)."""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return "image/heic"
        return "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    return ""


ALLOWED = {"image/jpeg", "image/png", "image/gif", "image/webp", "video/mp4", "video/quicktime", "video/webm"}
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/heic": ".heic",
       "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}


def kind(mime: str) -> str:
    return "video" if (mime or "").startswith("video/") else "image"


def public_url(row: dict) -> str:
    return f"{settings.public_url}/media/{row['id']}{Path(row.get('path') or '').suffix}"


def out(row: dict) -> dict:
    return {"id": row["id"], "filename": row["filename"], "mime": row["mime"], "size": row["size"],
            "kind": kind(row["mime"]), "url": public_url(row)}


async def _register(path: Path, mid: str, filename: str, mime: str, size: int) -> dict:
    row = {"id": mid, "created": time.time(), "filename": (filename or path.name)[:255], "mime": mime,
           "size": size, "path": str(path)}
    async with db.engine().begin() as c:
        await c.execute(insert(db.media).values(**row))
    log.info("stored media %s (%s, %d bytes)", mid, mime, size)
    return out(row)


def _finalise_type(tmp: Path, filename: str, mime: str) -> tuple[str, str]:
    with open(tmp, "rb") as f:
        head = f.read(32)
    real = sniff(head)
    # The type always comes from the bytes, never from the name or the declared type: files are served
    # publicly from this origin, so e.g. an SVG or HTML file must never get in.
    if real == "image/heic":
        raise ValueError("HEIC images aren't accepted by the platforms - convert to JPEG")
    if real not in ALLOWED:
        raise ValueError("Only JPEG, PNG, GIF, WebP images and MP4, MOV, WebM videos can be uploaded")
    return real, EXT[real]


async def save_file(tmp: Path, filename: str, mime: str = "") -> dict:
    """Adopt a fully written temp file (in the media dir) as a stored media file."""
    try:
        size = tmp.stat().st_size
        if size == 0:
            raise ValueError("The file is empty")
        if size > max_bytes():
            raise ValueError(f"File is larger than MEDIA_MAX_MB ({settings.media_max_mb} MB)")
        mime, ext = _finalise_type(tmp, filename, mime)
        mid = secrets.token_hex(16)
        path = media_dir() / f"{mid}{ext}"
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return await _register(path, mid, filename or f"media{ext}", mime, size)


def temp_path() -> Path:
    return media_dir() / f".part-{secrets.token_hex(12)}"


async def save_bytes(data: bytes, filename: str, mime: str = "") -> dict:
    if len(data) > max_bytes():
        raise ValueError(f"File is larger than MEDIA_MAX_MB ({settings.media_max_mb} MB)")
    tmp = temp_path()
    await asyncio.to_thread(tmp.write_bytes, data)
    return await save_file(tmp, filename, mime)


async def save_upload(upload) -> dict:
    """FastAPI UploadFile, written to disk in 1 MB pieces with the size cap enforced while reading."""
    tmp, size = temp_path(), 0
    try:
        with open(tmp, "wb") as f:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes():
                    raise ValueError(f"File is larger than MEDIA_MAX_MB ({settings.media_max_mb} MB)")
                f.write(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return await save_file(tmp, upload.filename or "upload", upload.content_type or "")


async def import_url(url: str) -> dict:
    """Download a public https link (Google Drive / Dropbox share links work too)."""
    url = drive_direct((url or "").strip())
    if not url.startswith("https://"):
        raise ValueError("Give an https:// link to an image or video")
    tmp, size = temp_path(), 0
    try:
        async with client(300, follow_redirects=False) as c:
            for _ in range(6):                               # follow redirects ourselves, checking each hop
                target, headers, ext = await pinned_request(url)
                async with c.stream("GET", target, headers=headers, extensions=ext) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = str(httpx.URL(url).join(r.headers["location"]))
                        continue
                    if r.status_code >= 300:
                        raise ValueError(f"Couldn't download the media ({r.status_code} from {r.url.host})")
                    ctype = r.headers.get("content-type", "").split(";")[0]
                    if ctype.startswith("text/html"):
                        raise ValueError("That link is a web page, not an image or video file (share it publicly, "
                                         "or use a direct file link)")
                    if int(r.headers.get("content-length") or 0) > max_bytes():
                        raise ValueError(f"File is larger than MEDIA_MAX_MB ({settings.media_max_mb} MB)")
                    with open(tmp, "wb") as f:
                        async for chunk in r.aiter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > max_bytes():
                                raise ValueError(f"File is larger than MEDIA_MAX_MB ({settings.media_max_mb} MB)")
                            f.write(chunk)
                    name = _name_from(r, url)
                    break
            else:
                raise ValueError("Too many redirects")
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"Couldn't download the media: {exc.__class__.__name__}") from exc
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return await save_file(tmp, name, ctype)


def _name_from(r: httpx.Response, url: str) -> str:
    cd = r.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    if m:
        return Path(m.group(1)).name[:200]
    return (url.split("?")[0].rstrip("/").split("/")[-1] or "media")[:200]


# ------------------------------------------------------------------ base64 / chunked upload (MCP)

_chunks: dict[str, dict] = {}


async def upload_base64(filename: str, data_base64: str, upload_id: str = "", done: bool = True, mime: str = "") -> dict:
    """One call with the whole file, or several: the first returns an upload_id, pass it (and done=false)
    until the last part. Parts are appended to a temp file, so memory stays small."""
    payload = re.sub(r"^data:[^;,]+;base64,", "", (data_base64 or "").strip())
    try:
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=False)
    except Exception as exc:
        raise ValueError("data_base64 is not valid base64") from exc
    now = time.time()
    for k in [k for k, v in _chunks.items() if now - v["ts"] > 7200]:
        _chunks.pop(k)["path"].unlink(missing_ok=True)
    if not upload_id:
        upload_id = secrets.token_hex(12)
        _chunks[upload_id] = {"name": filename or "upload", "path": temp_path(), "size": 0, "ts": now, "mime": mime}
    st = _chunks.get(upload_id)
    if not st:
        raise ValueError("Unknown or expired upload_id - start again without one")
    st["size"] += len(raw)
    st["ts"] = now
    if st["size"] > max_bytes():
        _chunks.pop(upload_id, None)
        st["path"].unlink(missing_ok=True)
        raise ValueError(f"File is larger than MEDIA_MAX_MB ({settings.media_max_mb} MB)")
    await asyncio.to_thread(_append, st["path"], raw)
    if not done:
        return {"upload_id": upload_id, "received_bytes": st["size"], "done": False}
    _chunks.pop(upload_id, None)
    return await save_file(st["path"], filename or st["name"], mime or st.get("mime") or "")


def _append(path: Path, raw: bytes) -> None:
    with open(path, "ab") as f:
        f.write(raw)


# ------------------------------------------------------------------ one-time upload links

async def create_link(note: str = "") -> dict:
    tok = crypto.new_token(18)
    now = time.time()
    exp = now + settings.upload_link_minutes * 60
    async with db.engine().begin() as c:
        await c.execute(delete(db.upload_links).where(db.upload_links.c.expires < now - 86400))
        await c.execute(insert(db.upload_links).values(id=crypto.token_hash(tok), created=now, expires=exp,
                                                       note=(note or "")[:200], media=[]))
    return {"token": tok, "url": f"{settings.public_url}/u/{tok}", "expires_in_minutes": settings.upload_link_minutes,
            "max_files": settings.upload_link_max_files,
            "next": "Open the link in a browser and drop the files, then call get_uploaded_media with this token"}


async def get_link(tok: str, allow_expired: bool = False) -> dict | None:
    if not tok or len(tok) > 100:
        return None
    async with db.engine().connect() as c:
        r = (await c.execute(select(db.upload_links).where(db.upload_links.c.id == crypto.token_hash(tok)))).first()
    if not r or (not allow_expired and r.expires < time.time()):
        return None
    return dict(r._mapping)


async def link_add(tok: str, item: dict) -> None:
    link = await get_link(tok)
    if not link:
        raise ValueError("This upload link expired")
    async with db.engine().begin() as c:
        await c.execute(update(db.upload_links).where(db.upload_links.c.id == link["id"]).values(
            media=[*(link["media"] or []), item]))


async def link_media(tok: str) -> dict:
    link = await get_link(tok, allow_expired=True)
    if not link:
        raise ValueError("Unknown upload link")
    rows = {r["id"] for r in await rows_for([m["id"] for m in link["media"] or []])}
    return {"expired": link["expires"] < time.time(), "note": link["note"],
            "media": [m for m in link["media"] or [] if m["id"] in rows]}


# ------------------------------------------------------------------ lookup

def clean_id(ref: str) -> str:
    """'<id>' or '<id>.jpg' or a /media/ URL of this server -> id ('' if it isn't one)."""
    ref = (ref or "").strip()
    if settings.public_url and ref.startswith(f"{settings.public_url}/media/"):
        ref = ref.rsplit("/", 1)[-1]
    ref = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", ref)
    return ref if ID_RX.match(ref) else ""


async def resolve(refs: list[str] | None) -> list[str]:
    """Media ids or https links (downloaded now) -> media ids, in order."""
    ids = []
    for ref in refs or []:
        ref = str(ref or "").strip()
        if not ref:
            continue
        mid = clean_id(ref)
        if mid:
            ids.append(mid)
        elif ref.startswith(("https://", "http://")):
            ids.append((await import_url(ref))["id"])
        else:
            raise ValueError(f"'{ref[:60]}' is neither a media id nor an https link")
    return ids


async def rows_for(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    async with db.engine().connect() as c:
        rows = {r.id: dict(r._mapping) for r in (await c.execute(select(db.media).where(db.media.c.id.in_(ids)))).fetchall()}
    return [rows[i] for i in ids if i in rows]


async def require(ids: list[str]) -> list[dict]:
    """Rows for every id, each still on disk - a post must never go out without its picture / video."""
    rows = await rows_for(ids)
    known = {r["id"] for r in rows}
    gone = [i for i in ids if i not in known]
    if gone:
        raise ValueError(f"Media {', '.join(gone)} doesn't exist (deleted or never uploaded) - upload it again")
    lost = [r["id"] for r in rows if not Path(r["path"]).exists()]
    if lost:
        raise ValueError(f"Media file(s) {', '.join(lost)} vanished from {settings.media_dir} - is /data a "
                         "persistent volume? Upload again")
    return rows


async def file_for(name: str) -> tuple[Path, str] | None:
    mid = clean_id(name)
    if not mid:
        return None
    rows = await rows_for([mid])
    if not rows or not Path(rows[0]["path"]).exists():
        return None
    return Path(rows[0]["path"]), rows[0]["mime"]


# ------------------------------------------------------------------ cleanup

def post_media_ids(p) -> list[str]:
    m = p._mapping if hasattr(p, "_mapping") else p
    ids = list(m["media"] or [])
    th = (m["options"] or {}).get("thumbnail")
    if th:
        ids.append(th)
    return ids


async def delete_media(ids: list[str], force: bool = False) -> dict:
    """Delete files now. Files still needed by an unfinished post are kept unless force=True."""
    busy: set[str] = set()
    if not force:
        async with db.engine().connect() as c:
            for r in (await c.execute(select(db.posts.c.media, db.posts.c.options).where(db.posts.c.status.in_(ACTIVE)))).fetchall():
                busy |= set(post_media_ids(r))
    deleted, kept = [], []
    for m in await rows_for(ids):
        if m["id"] in busy:
            kept.append(m["id"])
            continue
        Path(m["path"]).unlink(missing_ok=True)
        async with db.engine().begin() as c:
            await c.execute(delete(db.media).where(db.media.c.id == m["id"]))
        deleted.append(m["id"])
    if deleted:
        log.info("deleted %d media file(s)", len(deleted))
    return {"deleted": deleted, "kept_in_use": kept}


async def deletable(now: float | None = None) -> list[str]:
    """Files that are safe to delete:
    - used by posts that are ALL finished ("posted": every target succeeded, or "cancelled"), at least one
      posted, and the last finished MEDIA_DELETE_AFTER_MINUTES ago (-1 = never delete posted media);
    - used by no post, or only by cancelled ones: MEDIA_KEEP_UNUSED_DAYS after the last use / upload.
    Drafts, scheduled (incl. natively scheduled), posting, partial and failed posts keep their files."""
    now = now or time.time()
    keep_unused = now - settings.media_keep_unused_days * 86400
    async with db.engine().connect() as c:
        posts = (await c.execute(select(db.posts.c.media, db.posts.c.options, db.posts.c.status, db.posts.c.done_at,
                                        db.posts.c.created, db.posts.c.targets, db.posts.c.results))).fetchall()
        files = (await c.execute(select(db.media.c.id, db.media.c.created))).fetchall()
    uses: dict[str, list] = {}
    for r in posts:
        for m in post_media_ids(r):
            uses.setdefault(m, []).append(r)
    found = []
    for f in files:
        us = uses.get(f.id, [])
        if not us:
            if (f.created or 0) < keep_unused:
                found.append(f.id)
            continue
        if any(u.status not in ("posted", "cancelled") for u in us):
            continue
        if any(u.status == "posted" and not all(((u.results or {}).get(t) or {}).get("ok") for t in u.targets or [])
               for u in us):
            continue
        last = max((u.done_at or u.created or 0) for u in us)
        if any(u.status == "posted" for u in us):
            wait = settings.media_delete_after_minutes
            if wait >= 0 and last <= now - wait * 60:
                found.append(f.id)
        elif last < keep_unused:
            found.append(f.id)
    return found


async def cleanup() -> list[str]:
    gone = (await delete_media(await deletable()))["deleted"]
    # stale chunk files from abandoned uploads
    for p in media_dir().glob(".part-*"):
        try:
            if time.time() - p.stat().st_mtime > 7200:
                p.unlink(missing_ok=True)
        except OSError:
            pass
    return gone
