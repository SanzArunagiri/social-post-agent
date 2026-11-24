"""Each publisher against a mocked platform API (httpx.MockTransport)."""
from __future__ import annotations

import httpx
import pytest

from agent import accounts, media, publishers
from agent.net import PlatformError

from .conftest import JPEG, MP4, PNG, add_account, add_media


async def rows(*items) -> list[dict]:
    return await media.rows_for([m["id"] for m in items])


# ------------------------------------------------------------------ Facebook

async def test_facebook_text_post_with_link(env):
    env.on("POST", r"graph.facebook.com/v[\d.]+/PAGE1/feed$", {"id": "PAGE1_99"})
    acc = await accounts.get(await add_account("facebook", "PAGE1"))
    r = await publishers.facebook(acc, "Hello", [], {"facebook_link": "https://example.com"})
    assert r == {"id": "PAGE1_99", "url": "https://www.facebook.com/PAGE1_99"}
    form = env.find("POST", "/feed$")[0].form()
    assert form["message"] == "Hello" and form["link"] == "https://example.com"
    assert form["access_token"] == "facebook-token"


async def test_facebook_single_photo_uses_public_media_url(env):
    env.on("POST", r"/PAGE1/photos$", {"id": "PHOTO1", "post_id": "PAGE1_5"})
    acc = await accounts.get(await add_account("facebook", "PAGE1"))
    m = await add_media()
    r = await publishers.facebook(acc, "cap", await rows(m), {})
    assert r["id"] == "PAGE1_5"
    form = env.find("POST", "/photos$")[0].form()
    assert form["url"] == f"https://agent.example.com/media/{m['id']}.jpg"
    assert form["caption"] == "cap"


async def test_facebook_multi_photo_scheduled(env):
    n = {"i": 0}

    def photo(_c):
        n["i"] += 1
        return {"id": f"P{n['i']}"}
    env.on("POST", r"/PAGE1/photos$", photo).on("POST", r"/PAGE1/feed$", {"id": "PAGE1_7"})
    acc = await accounts.get(await add_account("facebook", "PAGE1"))
    files = await rows(await add_media(), await add_media())
    r = await publishers.facebook(acc, "two", files, {"_schedule_at": 1900000000})
    assert r["scheduled"] is True
    photos = [c.form() for c in env.find("POST", "/photos$")]
    assert all(p["published"] == "false" and p["temporary"] == "true" for p in photos)
    feed = env.find("POST", "/feed$")[0].form()
    assert feed["attached_media"] == '[{"media_fbid": "P1"}, {"media_fbid": "P2"}]'
    assert feed["scheduled_publish_time"] == "1900000000" and feed["published"] == "false"
    assert feed["unpublished_content_type"] == "SCHEDULED"


async def test_facebook_video_sends_bytes_and_thumbnail(env):
    env.on("POST", r"graph-video.facebook.com/v[\d.]+/PAGE1/videos$", {"id": "VID1"})
    acc = await accounts.get(await add_account("facebook", "PAGE1"))
    v, th = await add_media(MP4, "clip.mp4"), await add_media(JPEG, "cover.jpg")
    r = await publishers.facebook(acc, "watch", await rows(v), {"_thumb": (await rows(th))[0]})
    assert r["id"] == "VID1"
    body = env.find("POST", "/videos$")[0].body
    assert b'name="source"' in body and b'name="thumb"' in body and b"ftypmp42" in body


async def test_facebook_error_message_is_surfaced(env):
    env.on("POST", r"/feed$", httpx.Response(400, json={"error": {"message": "(#200) No permission", "code": 200}}))
    acc = await accounts.get(await add_account("facebook", "PAGE1"))
    with pytest.raises(PlatformError, match=r"No permission \(code 200\)"):
        await publishers.facebook(acc, "x", [], {})


# ------------------------------------------------------------------ Instagram

async def test_instagram_single_image(env):
    env.on("POST", r"/IG1/media$", {"id": "C1"}).on("GET", r"/C1$", {"status_code": "FINISHED"})
    env.on("POST", r"/IG1/media_publish$", {"id": "M1"}).on("GET", r"/M1$", {"permalink": "https://instagram.com/p/abc"})
    acc = await accounts.get(await add_account("instagram", "IG1"))
    m = await add_media()
    r = await publishers.instagram(acc, "caption", await rows(m), {})
    assert r == {"id": "M1", "url": "https://instagram.com/p/abc", "kind": "image"}
    form = env.find("POST", r"/IG1/media$")[0].form()
    assert form["image_url"].endswith(f"/media/{m['id']}.jpg") and form["caption"] == "caption"


async def test_instagram_carousel(env):
    n = {"i": 0}

    def child(c):
        n["i"] += 1
        return {"id": "CAR" if c.form().get("media_type") == "CAROUSEL" else f"K{n['i']}"}
    env.on("POST", r"/IG1/media$", child).on("GET", r"/CAR$", {"status_code": "FINISHED"})
    env.on("GET", r"/K\d$", {"status_code": "FINISHED"})
    env.on("POST", r"/media_publish$", {"id": "M2"}).on("GET", r"/M2$", {"permalink": "https://instagram.com/p/c"})
    acc = await accounts.get(await add_account("instagram", "IG1"))
    files = await rows(await add_media(), await add_media(MP4, "b.mp4"))
    r = await publishers.instagram(acc, "multi", files, {})
    forms = [c.form() for c in env.find("POST", r"/IG1/media$")]
    assert forms[0]["is_carousel_item"] == "true" and "image_url" in forms[0]
    assert forms[1]["media_type"] == "VIDEO" and forms[1]["is_carousel_item"] == "true"
    assert forms[2]["media_type"] == "CAROUSEL" and forms[2]["children"] == "K1,K2" and forms[2]["caption"] == "multi"
    assert r["kind"] == "carousel"


async def test_instagram_reel_with_cover_waits_for_processing(env):
    states = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    env.on("POST", r"/IG1/media$", {"id": "R1"}).on("GET", r"/R1$", lambda c: {"status_code": next(states)})
    env.on("POST", r"/media_publish$", {"id": "M3"}).on("GET", r"/M3$", {"permalink": "https://instagram.com/reel/r"})
    acc = await accounts.get(await add_account("instagram", "IG1"))
    v, th = await add_media(MP4, "reel.mp4"), await add_media(JPEG, "cover.jpg")
    r = await publishers.instagram(acc, "reel", await rows(v), {"_thumb": (await rows(th))[0],
                                                                "instagram_share_to_feed": False})
    form = env.find("POST", r"/IG1/media$")[0].form()
    assert form["media_type"] == "REELS" and form["cover_url"].endswith(f"{th['id']}.jpg") and form["share_to_feed"] == "false"
    assert len(env.find("GET", r"/R1$")) == 3 and r["kind"] == "reel"


async def test_instagram_processing_error(env):
    env.on("POST", r"/IG1/media$", {"id": "R1"}).on("GET", r"/R1$", {"status_code": "ERROR"})
    acc = await accounts.get(await add_account("instagram", "IG1"))
    with pytest.raises(PlatformError, match="couldn't process"):
        await publishers.instagram(acc, "reel", await rows(await add_media(MP4, "r.mp4")), {})


# ------------------------------------------------------------------ Threads

async def test_threads_text_with_topic(env):
    env.on("POST", r"graph.threads.net/v1.0/T1/threads$", {"id": "TC1"})
    env.on("POST", r"/T1/threads_publish$", {"id": "TP1"}).on("GET", r"/TP1$", {"permalink": "https://threads.net/@a/post/1"})
    acc = await accounts.get(await add_account("threads", "T1", "acme"))
    r = await publishers.threads(acc, "hello threads", [], {"threads_topic": "#Dev.Ops&"})
    form = env.find("POST", r"/T1/threads$")[0].form()
    assert form == {"access_token": "threads-token", "media_type": "TEXT", "text": "hello threads", "topic_tag": "DevOps"}
    assert r["url"] == "https://threads.net/@a/post/1"


async def test_threads_carousel(env):
    n = {"i": 0}

    def cont(c):
        n["i"] += 1
        return {"id": "CAR" if c.form().get("media_type") == "CAROUSEL" else f"K{n['i']}"}
    env.on("POST", r"/T1/threads$", cont).on("GET", r"/(CAR|K\d)$", {"status": "FINISHED"})
    env.on("POST", r"/threads_publish$", {"id": "TP2"}).on("GET", r"/TP2$", {"permalink": "p"})
    acc = await accounts.get(await add_account("threads", "T1"))
    await publishers.threads(acc, "pics", await rows(await add_media(), await add_media(PNG, "b.png")), {})
    forms = [c.form() for c in env.find("POST", r"/T1/threads$")]
    assert [f["media_type"] for f in forms] == ["IMAGE", "IMAGE", "CAROUSEL"]
    assert forms[2]["children"] == "K1,K2" and forms[2]["text"] == "pics"
    assert env.find("POST", "/threads_publish$")[0].form()["creation_id"] == "CAR"


# ------------------------------------------------------------------ X

async def test_x_text_and_image(env):
    env.on("POST", r"api.x.com/2/media/upload$", {"data": {"id": "MED1"}})
    env.on("POST", r"api.x.com/2/tweets$", {"data": {"id": "555", "text": "hi"}})
    acc = await accounts.get(await add_account("x", "U1", "acme", expires_at=9e12))
    r = await publishers.x(acc, "hi", await rows(await add_media()), {})
    assert r == {"id": "555", "url": "https://x.com/acme/status/555"}
    tweet = env.find("POST", "/2/tweets$")[0]
    assert tweet.json() == {"text": "hi", "media": {"media_ids": ["MED1"]}}
    assert tweet.headers["authorization"] == "Bearer x-token"


async def test_x_video_chunked_upload(env, monkeypatch):
    monkeypatch.setattr(publishers, "X_CHUNK", 1000)
    env.on("POST", r"/2/media/upload/initialize$", {"data": {"id": "V1"}})
    env.on("POST", r"/2/media/upload/V1/append$", {})
    env.on("POST", r"/2/media/upload/V1/finalize$", {"data": {"id": "V1", "processing_info": {"state": "pending"}}})
    env.on("GET", r"/2/media/upload$", {"data": {"processing_info": {"state": "succeeded"}}})
    env.on("POST", r"/2/tweets$", {"data": {"id": "777"}})
    acc = await accounts.get(await add_account("x", "U1", expires_at=9e12))
    await publishers.x(acc, "vid", await rows(await add_media(MP4, "v.mp4")), {})
    init = env.find("POST", "/initialize$")[0].json()
    assert init["media_category"] == "tweet_video" and init["total_bytes"] == len(MP4)
    appends = env.find("POST", "/append$")
    assert len(appends) == 4 and b'name="segment_index"\r\n\r\n3' in appends[-1].body


async def test_x_refreshes_expired_token(env):
    env.on("POST", r"api.x.com/2/oauth2/token$", {"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 7200})
    env.on("POST", r"/2/tweets$", {"data": {"id": "1"}})
    aid = await add_account("x", "U1", expires_at=1.0, refresh="old-refresh")
    await publishers.x(await accounts.get(aid), "hi", [], {})
    assert env.find("POST", "/oauth2/token$")[0].form()["refresh_token"] == "old-refresh"
    assert env.find("POST", "/2/tweets$")[0].headers["authorization"] == "Bearer new-token"
    stored = await accounts.get(aid)
    assert stored["token"] == "new-token" and stored["refresh_token"] == "new-refresh"


# ------------------------------------------------------------------ YouTube

async def test_youtube_resumable_upload_with_thumbnail_and_schedule(env, monkeypatch):
    monkeypatch.setattr(publishers, "YT_CHUNK", 1024)
    loc = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=abc"
    env.on("POST", r"googleapis.com/upload/youtube/v3/videos$",
           httpx.Response(200, headers={"location": loc}))

    def put(c):
        rng = c.headers.get("content-range", "")
        end = int(rng.split("-")[1].split("/")[0])
        if end + 1 < len(MP4):
            return httpx.Response(308, headers={"range": f"bytes=0-{end}"})
        return httpx.Response(200, json={"id": "YT1", "status": {"privacyStatus": "private"}})
    env.on("PUT", r"googleapis.com/upload/youtube/v3/videos$", put)
    env.on("POST", r"/thumbnails/set$", {"items": []})
    acc = await accounts.get(await add_account("youtube", "CH1", "chan", expires_at=9e12))
    v, th = await add_media(MP4, "my-video.mp4"), await add_media(JPEG, "t.jpg")
    r = await publishers.youtube(acc, "Description #shorts", await rows(v),
                                 {"youtube_title": "Title", "_thumb": (await rows(th))[0], "_schedule_at": 1900000000})
    start = env.find("POST", r"/upload/youtube/v3/videos$")[0]
    meta = start.json()
    assert meta["snippet"]["title"] == "Title" and meta["snippet"]["tags"] == ["shorts"]
    assert meta["status"]["privacyStatus"] == "private" and meta["status"]["publishAt"] == "2030-03-17T17:46:40Z"
    assert start.params["uploadType"] == "resumable"
    puts = env.find("PUT", r"/videos$")
    assert len(puts) == 3 and puts[0].headers["content-range"] == f"bytes 0-1023/{len(MP4)}"
    assert r["id"] == "YT1" and r["thumbnail"] == "set" and r["url"] == "https://youtube.com/shorts/YT1"


async def test_youtube_image_post_pilot_refusal(env):
    env.on("POST", r"/upload/youtube/v3/posts/image$",
           httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}}))
    acc = await accounts.get(await add_account("youtube", "CH1", expires_at=9e12))
    with pytest.raises(PlatformError, match="pilot"):
        await publishers.youtube(acc, "pic", await rows(await add_media()), {})
