"""Posts: drafts, publishing to every target, scheduling (native where the platform supports it,
otherwise the built-in scheduler), history.

Status flow:  draft -> scheduled -> posting -> posted | partial | failed        (any unfinished -> cancelled)
A partial / failed post can be scheduled again: only the targets that failed are retried.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

from sqlalchemy import insert, select, update

from . import accounts, db, limits, media, notify, publishers
from .accounts import NAMES
from .config import settings

log = logging.getLogger("agent.posts")
FINAL = ("posted", "cancelled")
_running: set[int] = set()
_tasks: set[asyncio.Task] = set()


WAIT_SECONDS = 240


def background(coro) -> asyncio.Task:
    """Run detached from the request: a caller that disconnects must not abort a half-done publish."""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


# ------------------------------------------------------------------ time

def parse_when(when) -> float | None:
    """None / '' / 'now' -> None (right away). Epoch seconds, or ISO 8601 ('2026-12-01T09:30:00+08:00');
    a time without an offset is read in TIMEZONE."""
    if when is None:
        return None
    if isinstance(when, (int, float)):
        return float(when) if when > 0 else None
    w = str(when).strip()
    if not w or w.lower() in ("now", "asap", "immediately"):
        return None
    if re.fullmatch(r"\d{9,11}(\.\d+)?", w):
        return float(w)
    try:
        dt = datetime.fromisoformat(w.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use ISO 8601 for the time, e.g. '2026-12-01T09:30' (TIMEZONE) or '2026-12-01T09:30:00+08:00'")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=settings.tz)
    return dt.timestamp()


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, settings.tz).isoformat(timespec="minutes") if ts else None


# ------------------------------------------------------------------ views

async def view(p: dict, names: dict | None = None) -> dict:
    names = names if names is not None else {a["id"]: a for a in await accounts.list_accounts()}
    th = (p.get("options") or {}).get("thumbnail")
    files = {m["id"]: m for m in await media.rows_for(list(p.get("media") or []) + ([th] if th else []))}
    results = p.get("results") or {}
    return {
        "id": p["id"], "status": p["status"], "text": p.get("text") or "", "variants": p.get("variants") or {},
        "options": p.get("options") or {}, "when": p.get("when"), "when_local": iso(p.get("when")),
        "targets": [{"account": t, "platform": t.split(":")[0], "name": (names.get(t) or {}).get("name", t),
                     "connected": t in names, "result": results.get(t), "problems": (p.get("problems") or {}).get(t)}
                    for t in p.get("targets") or []],
        "media": [media.out(files[m]) if m in files else {"id": m, "missing": True} for m in p.get("media") or []],
        "thumbnail": (media.out(files[th]) if th in files else {"id": th, "missing": True}) if th else None,
        "links": {t: r.get("url") for t, r in results.items() if (r or {}).get("ok") and r.get("url")},
        "errors": {t: r.get("error") for t, r in results.items() if r and not r.get("ok")},
        "source": p.get("source"), "created": p.get("created"), "done_at": p.get("done_at"),
    }


async def _row(pid: int) -> dict | None:
    async with db.engine().connect() as c:
        r = (await c.execute(select(db.posts).where(db.posts.c.id == pid))).first()
    return dict(r._mapping) if r else None


async def get(pid: int) -> dict:
    p = await _row(int(pid))
    if not p:
        raise ValueError(f"No post #{pid}")
    return await view(p)


async def list_posts(statuses: tuple[str, ...] | None = None, limit: int = 50, offset: int = 0,
                     platform: str = "", order_by_when: bool = False) -> list[dict]:
    q = select(db.posts)
    if statuses:
        q = q.where(db.posts.c.status.in_(statuses))
    q = q.order_by(db.posts.c.when.asc(), db.posts.c.id.asc()) if order_by_when else q.order_by(db.posts.c.id.desc())
    async with db.engine().connect() as c:
        rows = [dict(r._mapping) for r in (await c.execute(q.limit(max(1, min(limit, 200)) * (5 if platform else 1))
                                                           .offset(max(0, offset)))).fetchall()]
    if platform:
        plat = accounts.platform_word(platform)
        rows = [r for r in rows if any(t.split(":")[0] == plat for t in r["targets"] or [])][:limit]
    names = {a["id"]: a for a in await accounts.list_accounts()}
    return [await view(r, names) for r in rows]


# ------------------------------------------------------------------ create / check

def text_for(p: dict, target: str) -> str:
    v = p.get("variants") or {}
    return str(v.get(target) or v.get(target.split(":")[0]) or p.get("text") or "")


async def _problems(p: dict) -> dict:
    files = await media.rows_for(p.get("media") or [])
    th_id = (p.get("options") or {}).get("thumbnail")
    thumb = (await media.rows_for([th_id]) or [None])[0] if th_id else None
    out = {}
    for t in p.get("targets") or []:
        errs = limits.check(t.split(":")[0], text_for(p, t), files, thumb, p.get("options") or {})
        if errs:
            out[t] = "; ".join(errs)
    return out


def _clean_variants(variants: dict | None) -> dict:
    out = {}
    for k, v in (variants or {}).items():
        if v is None or str(v).strip() == "":
            continue
        key = k if ":" in k else accounts.platform_word(k)
        out[key] = str(v)
    return out


async def create(text: str = "", targets: list[str] | str | None = None, media_refs: list[str] | None = None,
                 variants: dict | None = None, options: dict | None = None, when=None, source: str = "api",
                 platform: str = "") -> dict:
    tg = await accounts.resolve(targets, platform)
    ids = await media.resolve(media_refs)
    opts = {k: v for k, v in (options or {}).items() if v is not None and v != ""}
    if opts.get("thumbnail"):
        opts["thumbnail"] = (await media.resolve([str(opts["thumbnail"])]))[0]
    await media.require(ids + ([opts["thumbnail"]] if opts.get("thumbnail") else []))
    p = {"text": (text or "")[:70000], "targets": tg, "media": ids, "variants": _clean_variants(variants), "options": opts}
    probs = await _problems(p)
    now = time.time()
    async with db.engine().begin() as c:
        res = await c.execute(insert(db.posts).values(
            created=now, updated=now, status="draft", when=parse_when(when), results={}, problems=probs,
            source=(source or "api")[:40], **p))
        pid = res.inserted_primary_key[0]
    return await get(pid)


async def _set(pid: int, **vals) -> None:
    vals["updated"] = time.time()
    async with db.engine().begin() as c:
        await c.execute(update(db.posts).where(db.posts.c.id == pid).values(**vals))


# ------------------------------------------------------------------ schedule / publish

async def schedule(pid: int, when=None, wait: bool = False) -> dict:
    """Publish a draft (or retry a partial / failed post) now, or at `when`. Re-scheduling a scheduled
    post moves it (platform-held copies are removed and re-created)."""
    p = await _row(int(pid))
    if not p:
        raise ValueError(f"No post #{pid}")
    if p["status"] not in ("draft", "scheduled", "failed", "partial"):
        raise ValueError(f"Post #{pid} is {p['status']} - it can't be (re)scheduled")
    probs = await _problems(p)
    if probs:
        await _set(pid, problems=probs)
        raise ValueError("Fix these first: " + "; ".join(f"{t}: {e}" for t, e in probs.items()))
    await media.require(media.post_media_ids(p))
    connected = {a["id"] for a in await accounts.list_accounts()}
    missing = [t for t in p["targets"] if t not in connected]
    if missing:
        raise ValueError("Not connected any more: " + ", ".join(missing))
    ts = parse_when(when) if when is not None else p.get("when")
    now = time.time()
    if ts and ts < now - 60 and when is not None:
        raise ValueError(f"{iso(ts)} is in the past - give a future time, or 'now'")
    ts = ts if ts and ts > now + 5 else now
    if p["status"] == "scheduled":
        errs = await _unschedule(p)
        if errs:
            raise ValueError("Couldn't move the copy a platform is holding: " + "; ".join(errs))
    await _set(pid, status="scheduled", when=ts, problems={})
    task = None
    if ts <= now + 5:
        task = background(publish(pid))
    elif settings.native_scheduling:
        task = background(native_schedule(pid))
    if task and wait:
        try:
            await asyncio.wait_for(asyncio.shield(task), WAIT_SECONDS)
        except (asyncio.TimeoutError, Exception):
            pass                                       # still running, or failed - the post records it
    return await get(pid)


async def publish(pid: int) -> dict:
    """Post to every target that hasn't succeeded yet. Safe to call twice: only one run per post."""
    if pid in _running:
        return await get(pid)
    _running.add(pid)
    try:
        async with db.engine().begin() as c:          # claim it (scheduler and API may race)
            res = await c.execute(update(db.posts).where(db.posts.c.id == pid, db.posts.c.status == "scheduled")
                                  .values(status="posting", updated=time.time()))
        if not res.rowcount:
            return await get(pid)
        return await _publish(pid)
    finally:
        _running.discard(pid)


async def _publish(pid: int) -> dict:
    p = await _row(pid)
    results = dict(p.get("results") or {})
    opts = p.get("options") or {}
    try:
        files = await media.require(p.get("media") or [])
        thumb = (await media.require([opts["thumbnail"]]))[0] if opts.get("thumbnail") else None
    except ValueError as exc:                          # never post without the picture / video
        for t in p["targets"]:
            if not (results.get(t) or {}).get("ok"):
                results[t] = {"ok": False, "error": str(exc)}
        await _finish(p, results)
        return await get(pid)
    for t in p["targets"]:
        if (results.get(t) or {}).get("ok"):
            continue                                   # done earlier (retry, or held natively by the platform)
        try:
            acc = await accounts.get(t)
            res = await publishers.PUBLISHERS[acc["platform"]](acc, text_for(p, t), files, {**opts, "_thumb": thumb})
            results[t] = {"ok": True, "at": time.time(), **res}
        except Exception as exc:
            log.warning("post #%s to %s failed: %s", pid, t, exc)
            results[t] = {"ok": False, "error": str(exc)[:500], "at": time.time()}
        await _set(pid, results=results)               # progress survives a crash mid-way
    await _finish(p, results)
    return await get(pid)


async def _finish(p: dict, results: dict) -> None:
    good = [t for t in p["targets"] if (results.get(t) or {}).get("ok")]
    status = "posted" if len(good) == len(p["targets"]) else ("partial" if good else "failed")
    await _set(p["id"], status=status, results=results, done_at=time.time() if status == "posted" else None)
    names = {a["id"]: a["name"] for a in await accounts.list_accounts()}
    icon = {"posted": "✅", "partial": "⚠️", "failed": "❌"}[status]
    lines = [f"{icon} Post #{p['id']} {status}"]
    for t in p["targets"]:
        r = results.get(t) or {}
        label = f"{NAMES.get(t.split(':')[0], t)} ({names.get(t, t)})"
        detail = (r.get("url") or r.get("id") or "ok") if r.get("ok") else r.get("error", "")
        lines.append(f"{'•' if r.get('ok') else '✗'} {label}: {detail}")
    await notify.notify("\n".join(lines), failure=status != "posted")


async def native_schedule(pid: int) -> dict:
    """Hand a future post to the platforms that can hold it themselves (Facebook, YouTube videos)."""
    p = await _row(pid)
    if not p or p["status"] != "scheduled" or (p.get("when") or 0) <= time.time() + 60:
        return {}
    results = dict(p.get("results") or {})
    opts = p.get("options") or {}
    files = await media.rows_for(p.get("media") or [])
    thumb = (await media.rows_for([opts["thumbnail"]]) or [None])[0] if opts.get("thumbnail") else None
    held, failed = [], []
    for t in p["targets"]:
        plat = t.split(":")[0]
        if (results.get(t) or {}).get("ok") or not publishers.native_ok(plat, p["when"], files, time.time()):
            continue
        try:
            acc = await accounts.get(t)
            res = await publishers.PUBLISHERS[plat](acc, text_for(p, t), files,
                                                    {**opts, "_thumb": thumb, "_schedule_at": p["when"]})
            results[t] = {"ok": True, "native": True, "at": time.time(), **res}
            held.append(NAMES[plat])
        except Exception as exc:                        # the built-in scheduler posts it at the time instead
            failed.append(f"{NAMES.get(plat, plat)}: {str(exc)[:200]}")
            log.warning("native scheduling of post #%s on %s failed: %s", pid, t, exc)
    cur = await _row(pid)
    if held and cur and cur["status"] == "scheduled":
        await _set(pid, results=results)
        await notify.notify(f"📅 Post #{pid} scheduled on {', '.join(held)} for {iso(p['when'])} (held by the platform)"
                            + (f"\n⚠️ Will be posted by the agent instead: {'; '.join(failed)}" if failed else ""))
    return results


async def _unschedule(p: dict) -> list[str]:
    """Remove copies a platform is holding (post cancelled or moved). Returns problems."""
    results, errs = dict(p.get("results") or {}), []
    for t, r in list(results.items()):
        if not (r or {}).get("native"):
            continue
        try:
            await publishers.unschedule(await accounts.get(t), r)
            results.pop(t)
        except Exception as exc:
            errs.append(f"{NAMES.get(t.split(':')[0], t)}: {str(exc)[:150]} - delete it there by hand")
    await _set(p["id"], results=results)
    return errs


async def cancel(pid: int) -> dict:
    p = await _row(int(pid))
    if not p:
        raise ValueError(f"No post #{pid}")
    if p["status"] in ("posting", "posted", "cancelled"):
        raise ValueError(f"Post #{pid} is {p['status']} - it can't be cancelled")
    errs = await _unschedule(p)
    await _set(p["id"], status="cancelled", done_at=time.time())
    out = await get(pid)
    if errs:
        out["warnings"] = errs
    return out


# ------------------------------------------------------------------ background

async def run_due(wait: bool = True) -> list[int]:
    """Publish posts whose time has come. The scheduler loop passes wait=False so one slow video upload
    doesn't hold up the other due posts (publish() itself prevents a post from running twice)."""
    async with db.engine().connect() as c:
        due = [r.id for r in (await c.execute(select(db.posts.c.id).where(
            db.posts.c.status == "scheduled", db.posts.c.when <= time.time()).order_by(db.posts.c.when))).fetchall()]
    for pid in due:
        if pid in _running:
            continue
        if not wait:
            background(publish(pid))
            continue
        try:
            await publish(pid)
        except Exception as exc:
            log.error("publishing post #%s failed: %s", pid, exc)
    return due


async def recover() -> None:
    """At startup: posts left in 'posting' by a crash / restart are finished with what is known."""
    async with db.engine().connect() as c:
        rows = [dict(r._mapping) for r in (await c.execute(select(db.posts).where(db.posts.c.status == "posting"))).fetchall()]
    for p in rows:
        results = dict(p.get("results") or {})
        for t in p["targets"]:
            if not (results.get(t) or {}).get("ok"):
                results[t] = {"ok": False, "error": "interrupted by a restart - check the platform, then retry with schedule_post"}
        await _finish(p, results)
