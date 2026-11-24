"""Publishing to each platform's official API.

Every publisher has the same shape:  async (account, text, files, opts) -> {"id", "url", ...}
  files  media rows (id, filename, mime, size, path) to post, in order
  opts   the post's options plus "_thumb" (thumbnail / cover media row) and "_schedule_at" (epoch,
         only when the platform schedules the post itself - Facebook and YouTube)
They raise PlatformError / ValueError with a message that is safe to show.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import oauth
from .config import settings
from .media import public_url
from .net import PlatformError, client, ok
from .oauth import THREADS, X_API

POLL_SECONDS = 5.0           # wait between processing-status checks (tests set 0)
POLL_TRIES = 120             # 10 minutes


def _vid(f: dict) -> bool:
    return (f.get("mime") or "").startswith("video/")


async def _wait_container(c: httpx.AsyncClient, url: str, token: str, field: str = "status_code") -> None:
    """Instagram / Threads process uploaded media before a container can be published."""
    for _ in range(POLL_TRIES):
        st = str(ok(await c.get(url, params={"fields": field, "access_token": token})).get(field) or "").upper()
        if st in ("FINISHED", "PUBLISHED"):
            return
        if st in ("ERROR", "EXPIRED"):
            raise PlatformError(f"the platform couldn't process the media ({st}) - check the format and size")
        await asyncio.sleep(POLL_SECONDS)
    raise PlatformError("media still processing after 10 minutes")


# ======================================================================= Facebook Page

async def facebook(acc: dict, text: str, files: list[dict], opts: dict) -> dict:
    page, tok, g = acc["meta"].get("page_id") or acc["account_id"], acc["token"], oauth.graph()
    sched = {"published": "false", "scheduled_publish_time": str(int(opts["_schedule_at"]))} if opts.get("_schedule_at") else {}
    async with client(900) as c:
        if not files:
            data = {"message": text, "access_token": tok, **sched}
            if opts.get("facebook_link"):
                data["link"] = str(opts["facebook_link"])
            d = ok(await c.post(f"{g}/{page}/feed", data=data))
        elif _vid(files[0]):
            # send the bytes (Facebook fetching a file_url sometimes fails with "Unable to fetch video file")
            f, th = files[0], opts.get("_thumb")
            with open(f["path"], "rb") as fh, (open(th["path"], "rb") if th else _null()) as thf:
                up = {"source": (f["filename"], fh, f["mime"])}
                if th:
                    up["thumb"] = (th["filename"], thf, th["mime"])
                data = {"description": text, "access_token": tok, **sched}
                if opts.get("facebook_video_title"):
                    data["title"] = str(opts["facebook_video_title"])[:255]
                d = ok(await c.post(f"{g.replace('//graph.', '//graph-video.')}/{page}/videos", data=data, files=up))
            vid = d.get("id")
            return {"id": vid, "url": f"https://www.facebook.com/{page}/videos/{vid}", **({"scheduled": True} if sched else {})}
        elif len(files) == 1:
            d = ok(await c.post(f"{g}/{page}/photos", data={"url": public_url(files[0]), "caption": text,
                                                          "access_token": tok, **sched}))
            d["id"] = d.get("post_id") or d.get("id")
        else:
            ids = []
            for f in files:   # upload each photo unpublished, then one feed post with all of them
                r = ok(await c.post(f"{g}/{page}/photos", data={
                    "url": public_url(f), "published": "false", **({"temporary": "true"} if sched else {}),
                    "access_token": tok}))
                ids.append({"media_fbid": r["id"]})
            d = ok(await c.post(f"{g}/{page}/feed", data={
                "message": text, "attached_media": json.dumps(ids), "access_token": tok, **sched,
                **({"unpublished_content_type": "SCHEDULED"} if sched else {})}))
    return {"id": d.get("id"), "url": f"https://www.facebook.com/{d.get('id')}", **({"scheduled": True} if sched else {})}


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


# ======================================================================= Instagram

async def instagram(acc: dict, text: str, files: list[dict], opts: dict) -> dict:
    ig, tok, g = acc["account_id"], acc["token"], oauth.graph()
    if not files:
        raise ValueError("Instagram needs an image or a video")
    async with client(300) as c:
        async def container(f: dict, child: bool) -> str:
            data = {"access_token": tok}
            if _vid(f):
                data.update(media_type="VIDEO" if child else "REELS", video_url=public_url(f))
                if not child:
                    if opts.get("_thumb"):
                        data["cover_url"] = public_url(opts["_thumb"])
                    if opts.get("instagram_share_to_feed") is False:
                        data["share_to_feed"] = "false"
            else:
                data["image_url"] = public_url(f)
            if child:
                data["is_carousel_item"] = "true"
            else:
                data["caption"] = text
            cid = ok(await c.post(f"{g}/{ig}/media", data=data))["id"]
            if _vid(f):
                await _wait_container(c, f"{g}/{cid}", tok)
            return cid

        if len(files) == 1:
            cid = await container(files[0], False)
            if not _vid(files[0]):          # publishing before the container is ready -> "Media ID is not available"
                await _wait_container(c, f"{g}/{cid}", tok)
        else:
            kids = [await container(f, True) for f in files]
            cid = ok(await c.post(f"{g}/{ig}/media", data={"media_type": "CAROUSEL", "children": ",".join(kids),
                                                          "caption": text, "access_token": tok}))["id"]
            await _wait_container(c, f"{g}/{cid}", tok)
        mid = ok(await c.post(f"{g}/{ig}/media_publish", data={"creation_id": cid, "access_token": tok}))["id"]
        try:
            link = ok(await c.get(f"{g}/{mid}", params={"fields": "permalink", "access_token": tok})).get("permalink")
        except Exception:
            link = ""
    kind = "reel" if len(files) == 1 and _vid(files[0]) else ("carousel" if len(files) > 1 else "image")
    return {"id": mid, "url": link or f"https://www.instagram.com/{acc['name']}/", "kind": kind}


# ======================================================================= Threads

async def threads(acc: dict, text: str, files: list[dict], opts: dict) -> dict:
    uid, tok = acc["account_id"], await oauth.access_token(acc)
    topic = re.sub(r"[.&]", "", str(opts.get("threads_topic") or "").lstrip("#").strip())[:50]
    async with client(300) as c:
        async def container(f: dict | None, child: bool) -> str:
            data = {"access_token": tok}
            if f is None:
                data.update(media_type="TEXT", text=text)
            elif _vid(f):
                data.update(media_type="VIDEO", video_url=public_url(f))
            else:
                data.update(media_type="IMAGE", image_url=public_url(f))
            if child:
                data["is_carousel_item"] = "true"
            elif f is not None and text:
                data["text"] = text
            if not child and topic:
                data["topic_tag"] = topic
            cid = ok(await c.post(f"{THREADS}/{uid}/threads", data=data))["id"]
            if f is not None and _vid(f):
                await _wait_container(c, f"{THREADS}/{cid}", tok, field="status")
            return cid

        if not files:
            cid = await container(None, False)
        elif len(files) == 1:
            cid = await container(files[0], False)
        else:
            kids = [await container(f, True) for f in files]
            cid = ok(await c.post(f"{THREADS}/{uid}/threads", data={
                "media_type": "CAROUSEL", "children": ",".join(kids), "access_token": tok,
                **({"text": text} if text else {}), **({"topic_tag": topic} if topic else {})}))["id"]
        if files:      # Meta recommends letting image / carousel containers settle before publishing
            await _wait_container(c, f"{THREADS}/{cid}", tok, field="status")
        tid = ok(await c.post(f"{THREADS}/{uid}/threads_publish", data={"creation_id": cid, "access_token": tok}))["id"]
        try:
            link = ok(await c.get(f"{THREADS}/{tid}", params={"fields": "permalink", "access_token": tok})).get("permalink")
        except Exception:
            link = ""
    return {"id": tid, "url": link or f"https://www.threads.net/@{acc['name']}", **({"topic": topic} if topic else {})}


# ======================================================================= X

X_CHUNK = 4 * 1024 * 1024


async def x(acc: dict, text: str, files: list[dict], opts: dict) -> dict:
    tok = await oauth.access_token(acc)
    h = {"Authorization": f"Bearer {tok}"}
    ids = []
    async with client(600) as c:
        for f in files:
            if _vid(f) or f["mime"] == "image/gif":
                category = "tweet_video" if _vid(f) else "tweet_gif"
                init = ok(await c.post(f"{X_API}/media/upload/initialize", headers=h, json={
                    "media_type": f["mime"], "total_bytes": f["size"], "media_category": category}))
                mid = (init.get("data") or init).get("id")
                with open(f["path"], "rb") as fh:
                    i = 0
                    while chunk := fh.read(X_CHUNK):
                        ok(await c.post(f"{X_API}/media/upload/{mid}/append", headers=h,
                                        data={"segment_index": str(i)}, files={"media": ("chunk", chunk, "application/octet-stream")}))
                        i += 1
                fin = ok(await c.post(f"{X_API}/media/upload/{mid}/finalize", headers=h))
                info = (fin.get("data") or fin).get("processing_info")
                for _ in range(POLL_TRIES):
                    if not info or info.get("state") == "succeeded":
                        break
                    if info.get("state") == "failed":
                        raise PlatformError(f"X couldn't process the video: {(info.get('error') or {}).get('message', '')}")
                    await asyncio.sleep(min(int(info.get("check_after_secs") or 5), 30) if POLL_SECONDS else 0)
                    st = ok(await c.get(f"{X_API}/media/upload", headers=h, params={"command": "STATUS", "media_id": mid}))
                    info = (st.get("data") or st).get("processing_info")
                else:
                    raise PlatformError("X is still processing the video after 10 minutes")
            else:
                with open(f["path"], "rb") as fh:
                    up = ok(await c.post(f"{X_API}/media/upload", headers=h, data={"media_category": "tweet_image"},
                                         files={"media": (f["filename"], fh, f["mime"])}))
                mid = (up.get("data") or up).get("id") or up.get("media_id_string")
            ids.append(str(mid))
        body: dict = {"text": text}
        if ids:
            body["media"] = {"media_ids": ids}
        d = ok(await c.post(f"{X_API}/tweets", headers=h, json=body))["data"]
    return {"id": d["id"], "url": f"https://x.com/{acc['name']}/status/{d['id']}"}


# ======================================================================= YouTube

YT_UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
YT_API = "https://www.googleapis.com/youtube/v3"
YT_CHUNK = 8 * 1024 * 1024          # must be a multiple of 256 KiB
YT_POSTS_PILOT = ("YouTube refused the image/text post. Community posts through the API are a limited pilot "
                  "for approved Google Cloud projects - post it in YouTube Studio instead")


async def youtube(acc: dict, text: str, files: list[dict], opts: dict) -> dict:
    tok = await oauth.access_token(acc)
    if not any(_vid(f) for f in files):
        return await _youtube_post(acc, text, files, tok)
    f = files[0]
    title = str(opts.get("youtube_title") or (text.split("\n")[0].strip() if text.strip() else "") or Path(f["filename"]).stem)[:100]
    description = str(opts.get("youtube_description") or text)
    tags = opts.get("youtube_tags") or re.findall(r"#(\w+)", text)[:15]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    privacy = str(opts.get("youtube_privacy") or settings.youtube_default_privacy or "public").lower()
    meta = {"snippet": {"title": title, "description": description[:5000], "tags": tags,
                        "categoryId": str(opts.get("youtube_category_id") or "22")},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(opts.get("youtube_made_for_kids", False))}}
    publish_at = opts.get("_schedule_at")
    if publish_at:      # YouTube keeps it private and makes it public at publishAt
        meta["status"].update(privacyStatus="private", publishAt=datetime.fromtimestamp(
            publish_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    h = {"Authorization": f"Bearer {tok}"}
    notify = "false" if opts.get("youtube_notify_subscribers") is False else "true"
    async with client(900) as c:
        r = await c.post(f"{YT_UPLOAD}/videos", params={"uploadType": "resumable", "part": "snippet,status",
                                                       "notifySubscribers": notify}, json=meta,
                         headers={**h, "X-Upload-Content-Type": f["mime"], "X-Upload-Content-Length": str(f["size"])})
        if r.status_code >= 300:
            ok(r)
        loc = r.headers.get("location")
        if not loc:
            raise PlatformError("YouTube didn't return an upload URL")
        d = await _resumable_put(c, loc, f, h)
        vid = d["id"]
        extra: dict = {}
        if (th := opts.get("_thumb")):       # custom thumbnails need a verified channel
            try:
                with open(th["path"], "rb") as fh:
                    ok(await c.post(f"{YT_UPLOAD}/thumbnails/set", params={"videoId": vid}, content=fh.read(),
                                    headers={**h, "Content-Type": th["mime"]}))
                extra["thumbnail"] = "set"
            except Exception as exc:
                extra["thumbnail"] = f"not set ({str(exc)[:160]})"
    short = "#shorts" in text.lower() or str(opts.get("youtube_short") or "").lower() in ("1", "true", "yes")
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}" if short else f"https://youtu.be/{vid}",
            "privacy": (d.get("status") or {}).get("privacyStatus"), **({"scheduled": True} if publish_at else {}), **extra}


async def _resumable_put(c: httpx.AsyncClient, loc: str, f: dict, h: dict) -> dict:
    """Upload in 8 MB pieces; on a dropped connection ask YouTube how much it has and carry on."""
    total, pos, retries = f["size"], 0, 0
    with open(f["path"], "rb") as fh:
        while True:
            fh.seek(pos)
            chunk = fh.read(YT_CHUNK)
            end = pos + len(chunk) - 1
            try:
                r = await c.put(loc, content=chunk, headers={**h, "Content-Type": f["mime"],
                                                             "Content-Range": f"bytes {pos}-{end}/{total}"})
            except httpx.TransportError:
                retries += 1
                if retries > 5:
                    raise
                await asyncio.sleep(2 * retries if POLL_SECONDS else 0)
                q = await c.put(loc, headers={**h, "Content-Range": f"bytes */{total}"})
                pos = _next_byte(q, pos)
                continue
            if r.status_code == 308:            # resume incomplete
                pos = _next_byte(r, end + 1)
                continue
            if r.status_code in (500, 502, 503, 504) and retries < 5:
                retries += 1
                continue
            return ok(r)


def _next_byte(r: httpx.Response, default: int) -> int:
    m = re.match(r"bytes=0-(\d+)", r.headers.get("range", ""))
    return int(m.group(1)) + 1 if m else (0 if r.status_code == 308 and "range" not in r.headers else default)


async def _youtube_post(acc: dict, text: str, files: list[dict], tok: str) -> dict:
    """YouTube image (1-10 images) or text post. Pilot API: most projects get 403."""
    ch, h = acc["account_id"], {"Authorization": f"Bearer {tok}"}
    async with client(300) as c:
        def check(r: httpx.Response) -> dict:
            if r.status_code in (401, 403):
                try:
                    reason = ((r.json().get("error") or {}).get("errors") or [{}])[0].get("reason", "")
                except Exception:
                    reason = ""
                if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
                    raise PlatformError("Connect YouTube again - image posts need the youtube.force-ssl permission")
                raise PlatformError(YT_POSTS_PILOT)
            if r.status_code == 404:
                raise PlatformError(YT_POSTS_PILOT)
            return ok(r)
        uploads = []
        for f in files:
            with open(f["path"], "rb") as fh:
                d = check(await c.post(f"{YT_UPLOAD}/posts/image", params={"channelId": ch, "uploadType": "media"},
                                       content=fh.read(), headers={**h, "Content-Type": f["mime"]}))
            uploads.append(d["imageUploadToken"])
        body: dict = {"snippet": {"channelId": ch, **({"textDisplay": text} if text.strip() else {})}}
        if uploads:
            body["attachments"] = {"imageAttachments": [{"uploadToken": t} for t in uploads]}
        d = check(await c.post(f"{YT_API}/posts", headers=h, json=body,
                               params={"part": "id,snippet" + (",attachments" if uploads else "")}))
    return {"id": d.get("id"), "url": f"https://www.youtube.com/post/{d.get('id')}"}


PUBLISHERS = {"facebook": facebook, "instagram": instagram, "threads": threads, "x": x, "youtube": youtube}

# Platforms that hold a future post themselves (it goes out even if this server is down).
# Instagram, Threads and X have no scheduling API: the built-in scheduler posts at the time.
NATIVE = {"facebook", "youtube"}


def native_ok(platform: str, when: float, files: list[dict], now: float) -> bool:
    if not settings.native_scheduling or platform not in NATIVE:
        return False
    lead = (when or 0) - now
    if platform == "facebook":            # Facebook: 10 minutes to 30 days ahead (with margins)
        return 12 * 60 <= lead <= 29 * 86400
    return lead >= 15 * 60 and any(_vid(f) for f in files)       # YouTube: videos only


async def unschedule(acc: dict, result: dict) -> None:
    """Remove a post a platform is holding for us (the post was cancelled or edited)."""
    async with client(60) as c:
        if acc["platform"] == "facebook":
            ok(await c.delete(f"{oauth.graph()}/{result['id']}", params={"access_token": acc["token"]}))
        elif acc["platform"] == "youtube":
            r = await c.delete(f"{YT_API}/videos", params={"id": result["id"]},
                               headers={"Authorization": f"Bearer {await oauth.access_token(acc)}"})
            if r.status_code >= 300:
                ok(r)
