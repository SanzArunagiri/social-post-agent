"""Platform limits, checked before anything is sent so a post fails early with a clear reason.

Sources: each platform's publishing docs (Graph API / Threads / X API v2 / YouTube Data API v3).
Limits change - if a platform relaxes one, update the numbers here.
"""
from __future__ import annotations

import re

from .config import env_int

MB = 1024 * 1024
URL_RX = re.compile(r"https?://\S+", re.I)


def x_max_chars() -> int:
    return env_int("X_MAX_CHARS", 280)            # X Premium accounts can post longer - raise it in .env


LIMITS = {
    "facebook": {"text": 63206, "images": 10, "image_mb": 10, "video_mb": 1024},
    "instagram": {"text": 2200, "hashtags": 30, "mentions": 20, "items": 10, "image_mb": 8, "reel_mb": 300,
                  "carousel_video_mb": 100},
    "threads": {"text": 500, "items": 20, "image_mb": 8, "video_mb": 1024, "topic": 50},
    "x": {"images": 4, "image_mb": 5, "gif_mb": 15, "video_mb": 512},
    "youtube": {"title": 100, "description": 5000, "thumb_mb": 2, "images": 10, "tags_chars": 500},
}


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0xFE00 <= o <= 0xFE0F or o == 0x200D


def threads_length(text: str) -> int:
    """Threads counts emojis as their UTF-8 byte length."""
    return sum(len(ch.encode()) if _is_emoji(ch) else 1 for ch in text or "")


def x_length(text: str) -> int:
    """X's weighted length: links count 23, Latin-ish characters 1, others (CJK, emoji) 2."""
    text = text or ""
    n = 0
    for u in URL_RX.findall(text):
        n += 23
        text = text.replace(u, "", 1)
    for ch in text:
        o = ord(ch)
        n += 1 if (o <= 4351 or 8192 <= o <= 8205 or 8208 <= o <= 8223 or 8242 <= o <= 8247) else 2
    return n


def _mb(f: dict) -> float:
    return (f.get("size") or 0) / MB


def _vid(f: dict) -> bool:
    return (f.get("mime") or "").startswith("video/")


def check(platform: str, text: str, files: list[dict], thumb: dict | None = None, options: dict | None = None) -> list[str]:
    """Problems that would stop this platform from posting ([] = fine)."""
    o, t, out = options or {}, text or "", []
    vids = [f for f in files if _vid(f)]
    imgs = [f for f in files if not _vid(f)]
    L = LIMITS.get(platform, {})
    if not t.strip() and not files:
        return ["Nothing to post - add text or media"]
    if thumb and not vids:
        out.append("A thumbnail / cover only applies to a video post")
    if thumb and _vid(thumb):
        out.append("The thumbnail must be an image")

    if platform == "facebook":
        if len(t) > L["text"]:
            out.append(f"Facebook allows {L['text']} characters (this is {len(t)})")
        if vids and (len(vids) > 1 or imgs):
            out.append("Facebook: post one video, or up to 10 images - not both")
        if len(imgs) > L["images"]:
            out.append(f"Facebook: at most {L['images']} images per post")
        out += [f"Facebook: {f['filename']} is over {L['image_mb']} MB" for f in imgs if _mb(f) > L["image_mb"]]
        out += [f"Facebook: {f['filename']} is over 1 GB" for f in vids if _mb(f) > L["video_mb"]]

    elif platform == "instagram":
        if len(t) > L["text"]:
            out.append(f"Instagram captions allow {L['text']} characters (this is {len(t)})")
        if len(re.findall(r"(?<!\w)#\w", t)) > L["hashtags"]:
            out.append(f"Instagram allows {L['hashtags']} hashtags")
        if len(re.findall(r"(?<!\w)@\w", t)) > L["mentions"]:
            out.append(f"Instagram allows {L['mentions']} @mentions")
        if not files:
            out.append("Instagram needs an image or a video")
        if len(files) > L["items"]:
            out.append(f"Instagram carousels take at most {L['items']} items")
        out += [f"Instagram only accepts JPEG images ({f['filename']} is {f['mime']}) - convert it to .jpg"
                for f in imgs if f.get("mime") != "image/jpeg"]
        out += [f"Instagram: {f['filename']} is over {L['image_mb']} MB" for f in imgs if _mb(f) > L["image_mb"]]
        cap = L["reel_mb"] if len(files) == 1 else L["carousel_video_mb"]
        out += [f"Instagram: {f['filename']} is over {cap} MB" for f in vids if _mb(f) > cap]
        out += [f"Instagram: {f['filename']} must be MP4 or MOV" for f in vids
                if f.get("mime") not in ("video/mp4", "video/quicktime")]
        if thumb and (thumb.get("mime") != "image/jpeg" or _mb(thumb) > 8):
            out.append("Instagram Reel covers must be JPEG, up to 8 MB")
        if thumb and len(files) > 1:
            out.append("Instagram: a cover image only applies to a single Reel")

    elif platform == "threads":
        n = threads_length(t)
        if n > L["text"]:
            out.append(f"Threads allows {L['text']} characters (this is {n}; emojis count as several)")
        if len(files) > L["items"]:
            out.append(f"Threads carousels take at most {L['items']} items")
        out += [f"Threads only accepts JPEG or PNG images ({f['filename']} is {f['mime']})" for f in imgs
                if f.get("mime") not in ("image/jpeg", "image/png")]
        out += [f"Threads: {f['filename']} is over {L['image_mb']} MB" for f in imgs if _mb(f) > L["image_mb"]]
        out += [f"Threads: {f['filename']} is over 1 GB" for f in vids if _mb(f) > L["video_mb"]]
        out += [f"Threads: {f['filename']} must be MP4 or MOV" for f in vids
                if f.get("mime") not in ("video/mp4", "video/quicktime")]
        topic = str(o.get("threads_topic") or "").lstrip("#")
        if topic and (len(topic) > L["topic"] or re.search(r"[.&]", topic)):
            out.append("Threads topic tags are 1-50 characters without '.' or '&'")
        if thumb:
            out.append("Threads doesn't take a custom video cover")

    elif platform == "x":
        n, cap = x_length(t), x_max_chars()
        if n > cap:
            out.append(f"X allows {cap} characters (this is {n}; links count 23) - add a shorter 'x' variant")
        gifs = [f for f in imgs if f.get("mime") == "image/gif"]
        if (vids or gifs) and len(files) > 1:
            out.append("X takes one video or GIF, or up to 4 images")
        if len(imgs) > L["images"]:
            out.append(f"X takes at most {L['images']} images")
        out += [f"X: {f['filename']} is over {L['image_mb']} MB" for f in imgs if f not in gifs and _mb(f) > L["image_mb"]]
        out += [f"X: {f['filename']} is over {L['gif_mb']} MB" for f in gifs if _mb(f) > L["gif_mb"]]
        out += [f"X: {f['filename']} is over {L['video_mb']} MB" for f in vids if _mb(f) > L["video_mb"]]
        if thumb:
            out.append("X doesn't take a custom video thumbnail")

    elif platform == "youtube":
        if vids:
            if len(files) != 1:
                out.append("YouTube: upload one video per post (images can't be mixed in - use the thumbnail option)")
            title = str(o.get("youtube_title") or (t.split("\n")[0] if t.strip() else vids[0]["filename"]))
            if len(title) > L["title"] and o.get("youtube_title"):
                out.append(f"YouTube titles allow {L['title']} characters (this is {len(title)})")
            if re.search(r"[<>]", title + t):
                out.append("YouTube titles and descriptions can't contain < or >")
            if len(t.encode()) > L["description"]:
                out.append(f"YouTube descriptions allow {L['description']} bytes (this is {len(t.encode())})")
            privacy = str(o.get("youtube_privacy") or "").lower()
            if privacy and privacy not in ("public", "unlisted", "private"):
                out.append("youtube_privacy must be public, unlisted or private")
            if thumb and (thumb.get("mime") not in ("image/jpeg", "image/png") or _mb(thumb) > L["thumb_mb"]):
                out.append("YouTube thumbnails must be JPEG or PNG, up to 2 MB")
            tags = o.get("youtube_tags") or []
            if isinstance(tags, str):
                tags = [x.strip() for x in tags.split(",") if x.strip()]
            if sum(len(x) + 1 for x in tags) > L["tags_chars"]:
                out.append("YouTube tags allow 500 characters in total")
        else:
            if len(imgs) > L["images"]:
                out.append(f"A YouTube image post takes at most {L['images']} images")

    return out
