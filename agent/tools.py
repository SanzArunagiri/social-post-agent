"""The tools. Each one is a plain async function; the same function is served as an MCP tool (/mcp)
and as a REST endpoint (POST /api/<name>, and GET for the read-only ones). Docstrings and Field
descriptions are what MCP clients (Claude, ...) see, so they are written for an AI caller."""
from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from pydantic import Field

from . import accounts, insights, media, notify, oauth, posts
from .config import settings

Refs = Annotated[list[str] | None, Field(description="Media ids (from upload_media / an upload link) or public https "
                                                     "links to images / videos (downloaded now; Google Drive and "
                                                     "Dropbox share links work)")]
Account = Annotated[str, Field(description="Account id from list_social_accounts (e.g. 'facebook:1234'), its name, or "
                                           "empty when only one account of this platform is connected")]
Wait = Annotated[bool, Field(description="Wait (up to ~4 min) for the result. With false, or for long videos, check "
                                         "get_post / get_publishing_history afterwards")]
When = Annotated[str | None, Field(description="ISO 8601 time, e.g. '2026-12-01T09:30' (server TIMEZONE) or "
                                               "'2026-12-01T09:30:00+08:00'; 'now' or empty = right away")]


def _post_brief(p: dict) -> dict:
    """What a caller usually needs back from a publish."""
    out = {"post_id": p["id"], "status": p["status"], "when": p["when_local"],
           "results": {t["account"]: t["result"] for t in p["targets"] if t["result"]},
           "links": p["links"], "errors": p["errors"]}
    if p["status"] == "posting" or (p["status"] == "scheduled" and (p["when"] or 0) <= time.time() + 5):
        out["note"] = "Still in progress - check get_post later"
    return out


async def _direct(platform: str, account: str, text: str, media_refs: list[str] | None, options: dict,
                  wait: bool, when: str | None = None) -> dict:
    p = await posts.create(text=text, targets=[account] if account else None, platform=platform,
                           media_refs=media_refs, options=options, source="api")
    return _post_brief(await posts.schedule(p["id"], when or "now", wait=wait))


# ======================================================================= accounts

async def list_social_accounts() -> dict:
    """Connected accounts (use their ids as targets), which platforms have developer-app credentials set,
    and the OAuth redirect URIs to register in each developer app."""
    conf = settings.configured()
    return {"accounts": await accounts.list_accounts(),
            "platforms_configured": conf,
            "redirect_uris": {p: oauth.redirect_uri(p) for p in oauth.PROVIDERS} if settings.public_url else {},
            "connect_hint": "Use connect_account(platform) to get a sign-in link for a browser"}


async def connect_account(platform: Annotated[str, Field(description="facebook (also connects the Instagram accounts "
                                                                     "linked to your Pages), instagram, threads, x or youtube")]) -> dict:
    """A one-time sign-in link (15 minutes) for a person to open in a browser. After they approve, the
    account(s) appear in list_social_accounts."""
    return await oauth.connect_link(platform)


async def disconnect_account(account: Annotated[str, Field(description="Account id from list_social_accounts")]) -> dict:
    """Forget an account and its tokens (it can be connected again later)."""
    return {"disconnected": await accounts.disconnect(account), "account": account}


# ======================================================================= posts

async def create_post_draft(
        text: Annotated[str, Field(description="Main text / caption")] = "",
        targets: Annotated[list[str] | None, Field(description="Account ids or names, or platform names "
                                                               "('instagram') when one account of it is connected")] = None,
        variants: Annotated[dict[str, str] | None, Field(description="Per-platform (or per-account-id) text that replaces "
                                                                     "the main text there, e.g. {'x': 'short version'}")] = None,
        media: Refs = None,
        options: Annotated[dict[str, Any] | None, Field(description=(
            "Platform options: thumbnail (media id / link: YouTube thumbnail, Instagram Reel cover, Facebook video "
            "thumbnail), youtube_title, youtube_description, youtube_privacy (public|unlisted|private), youtube_tags, "
            "youtube_category_id, youtube_made_for_kids, youtube_notify_subscribers, youtube_short, threads_topic, "
            "instagram_share_to_feed, facebook_link, facebook_video_title"))] = None,
        when: When = None) -> dict:
    """Save a post without publishing it. The result lists any platform-limit problems per target.
    Publish it with schedule_post(post_id, when='now' or a time)."""
    return await posts.create(text=text, targets=targets, media_refs=media, variants=variants, options=options,
                              when=when, source="draft")


async def schedule_post(
        when: When = None,
        post_id: Annotated[int | None, Field(description="An existing draft (or a failed / partial post to retry, or a "
                                                         "scheduled one to move). Omit to create the post from the fields below")] = None,
        text: str = "", targets: list[str] | None = None, variants: dict[str, str] | None = None,
        media: Refs = None, options: dict[str, Any] | None = None,
        wait: Annotated[bool, Field(description="When publishing now: wait (up to ~4 min) for the result")] = False) -> dict:
    """Publish a post at a time (or now). Facebook (10 min - 30 days ahead) and YouTube videos (15+ min ahead)
    are handed to the platform's own scheduler, so they go out even if this server is down; Instagram, Threads
    and X have no scheduling API - the built-in scheduler publishes them at the time."""
    if post_id is None:
        if not targets:
            raise ValueError("Give post_id, or text/media plus targets")
        p = await posts.create(text=text, targets=targets, media_refs=media, variants=variants, options=options,
                               when=when, source="api")
        post_id = p["id"]
    elif any([text, targets, variants, media, options]):
        raise ValueError("To change a post's content, cancel it and create a new one - schedule_post(post_id) only sets the time")
    return await posts.schedule(post_id, when, wait=wait)


async def cancel_post(post_id: int) -> dict:
    """Cancel a draft or scheduled post (copies held by Facebook / YouTube are removed). Its media is released."""
    return await posts.cancel(post_id)


async def get_post(post_id: int) -> dict:
    """One post with per-target status, links and errors."""
    return await posts.get(post_id)


async def get_scheduled_posts(limit: Annotated[int, Field(ge=1, le=200)] = 50) -> list[dict]:
    """Posts waiting for their time (soonest first). 'native' in a target's result means the platform holds it."""
    return await posts.list_posts(("scheduled",), limit=limit, order_by_when=True)


async def get_publishing_history(
        limit: Annotated[int, Field(ge=1, le=200)] = 20, offset: int = 0,
        status: Annotated[Literal["", "draft", "scheduled", "posting", "posted", "partial", "failed", "cancelled"],
                          Field(description="Only this status (empty = everything except drafts)")] = "",
        platform: Annotated[str, Field(description="Only posts that target this platform")] = "") -> list[dict]:
    """Posts, newest first, with the result on every platform: links, ids and errors."""
    statuses = (status,) if status else ("scheduled", "posting", "posted", "partial", "failed", "cancelled")
    return await posts.list_posts(statuses, limit=limit, offset=offset, platform=platform)


# ======================================================================= per-platform publishing

async def publish_facebook_post(
        text: str = "", media: Refs = None, account: Account = "",
        link: Annotated[str, Field(description="A link to attach (text-only posts)")] = "",
        video_title: str = "", thumbnail: Annotated[str, Field(description="Video thumbnail (media id / link)")] = "",
        wait: Wait = True) -> dict:
    """Post to a Facebook Page now: text (optionally with a link), one photo, several photos (up to 10)
    or one video."""
    return await _direct("facebook", account, text, media,
                         {"facebook_link": link, "facebook_video_title": video_title, "thumbnail": thumbnail}, wait)


async def publish_instagram_post(
        caption: str = "", media: Refs = None, account: Account = "",
        reel_cover: Annotated[str, Field(description="Reel cover image (JPEG media id / link)")] = "",
        share_reel_to_feed: bool = True, wait: Wait = True) -> dict:
    """Post to Instagram now: one JPEG image, a carousel (2-10 images / videos) or one video (a Reel, with an
    optional cover). Images must be JPEG. Instagram fetches the files from PUBLIC_URL."""
    return await _direct("instagram", account, caption, media,
                         {"thumbnail": reel_cover, "instagram_share_to_feed": None if share_reel_to_feed else False}, wait)


async def publish_threads_post(
        text: str = "", media: Refs = None, account: Account = "",
        topic_tag: Annotated[str, Field(description="One topic tag (1-50 chars, no '.' or '&'), without #")] = "",
        wait: Wait = True) -> dict:
    """Post to Threads now: text (500 chars), one image or video, or a carousel of 2-20 items."""
    return await _direct("threads", account, text, media, {"threads_topic": topic_tag}, wait)


async def publish_x_post(text: str = "", media: Refs = None, account: Account = "", wait: Wait = True) -> dict:
    """Post to X now: text (280 weighted chars unless X_MAX_CHARS is raised) with up to 4 images, or one video /
    GIF. Note: posting through the X API needs a paid tier with write access (or pay-per-use credits)."""
    return await _direct("x", account, text, media, {}, wait)


async def upload_youtube_video(
        video: Annotated[str, Field(description="The video: media id or https link")],
        title: Annotated[str, Field(description="Up to 100 characters")] = "",
        description: str = "",
        privacy: Literal["", "public", "unlisted", "private"] = "",
        thumbnail: Annotated[str, Field(description="Custom thumbnail (JPEG/PNG up to 2 MB; the channel must be verified)")] = "",
        publish_at: Annotated[str | None, Field(description="Publish later (ISO 8601). 15+ minutes ahead: YouTube keeps "
                                                            "it private and publishes it itself")] = None,
        tags: list[str] | None = None, category_id: str = "", made_for_kids: bool = False,
        notify_subscribers: bool = True, is_short: bool = False, account: Account = "", wait: Wait = True) -> dict:
    """Upload a video to YouTube (resumable upload) with title, description, privacy, custom thumbnail and
    optional scheduled publishAt."""
    opts = {"youtube_title": title, "youtube_privacy": privacy,
            "thumbnail": thumbnail, "youtube_tags": tags or None, "youtube_category_id": category_id,
            "youtube_made_for_kids": made_for_kids or None,
            "youtube_notify_subscribers": None if notify_subscribers else False, "youtube_short": is_short or None}
    return await _direct("youtube", account, description, [video], opts, wait, when=publish_at)


# ======================================================================= insights & notifications

async def get_social_insights(
        account: Annotated[str, Field(description="One account id / name / platform; empty = all")] = "",
        days: Annotated[int, Field(ge=1, le=400)] = 30,
        refresh: Annotated[bool, Field(description="Fetch fresh numbers now (otherwise the last refresh is used)")] = False,
        posts_limit: Annotated[int, Field(ge=0, le=100)] = 10) -> dict:
    """Followers (with 7-day and period change) and recent posts with views / likes / comments / shares per
    account, including posts made outside this agent. Refreshed every INSIGHTS_REFRESH_HOURS."""
    if refresh:
        only = (await accounts.resolve([account]))[0] if account else ""
        await insights.refresh(only)
    return await insights.overview(account, days, posts_limit)


async def send_telegram_notification(text: str, chat_id: Annotated[str, Field(description="Default: TELEGRAM_CHAT_ID")] = "",
                                     silent: bool = False) -> dict:
    """Send a Telegram message through the configured bot."""
    return await notify.send_telegram(text, chat_id, silent)


# ======================================================================= media

async def upload_media(
        filename: str,
        data_base64: Annotated[str, Field(description="File content, base64 (a data: URL prefix is fine). Keep each "
                                                      "call under ~3 MB of base64; send bigger files in parts")],
        upload_id: Annotated[str, Field(description="From the previous part's result (empty for the first part)")] = "",
        done: Annotated[bool, Field(description="false while more parts follow, true on the last (or only) part")] = True,
        mime_type: str = "") -> dict:
    """Upload an image or video as base64, in one call or in parts. Returns the media id to use in posts.
    For large files a person has locally, create_upload_link is easier."""
    return await media.upload_base64(filename, data_base64, upload_id, done, mime_type)


async def import_media_url(url: Annotated[str, Field(description="Public https link to an image or video")]) -> dict:
    """Download a file from a link into the media store and return its media id."""
    return await media.import_url(url)


async def create_upload_link(note: Annotated[str, Field(description="Shown on the upload page")] = "") -> dict:
    """A one-time web page (default 60 minutes) where a person drops images / videos from their device.
    Then call get_uploaded_media(token) for the media ids."""
    return await media.create_link(note)


async def get_uploaded_media(token: Annotated[str, Field(description="The token returned by create_upload_link")]) -> dict:
    """Files uploaded through an upload link so far (media ids, types, public URLs)."""
    return await media.link_media(token)


async def delete_media(media_ids: list[str], force: Annotated[bool, Field(description="Also delete files an "
                                                                                      "unfinished post still needs")] = False) -> dict:
    """Delete stored media files now (normally they are deleted automatically once every post using them is out)."""
    return await media.delete_media([media.clean_id(m) or m for m in media_ids], force)


READ_ONLY = {"list_social_accounts", "get_post", "get_scheduled_posts", "get_publishing_history", "get_social_insights",
             "get_uploaded_media"}
DESTRUCTIVE = {"disconnect_account", "cancel_post", "delete_media"}
TOOLS = [list_social_accounts, connect_account, disconnect_account, create_post_draft, schedule_post, cancel_post,
         get_post, get_scheduled_posts, get_publishing_history, publish_facebook_post, publish_instagram_post,
         publish_threads_post, publish_x_post, upload_youtube_video, get_social_insights, send_telegram_notification,
         upload_media, import_media_url, create_upload_link, get_uploaded_media, delete_media]
