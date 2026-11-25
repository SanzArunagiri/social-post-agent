"""Drafts, limit checks, publishing to several targets, retries, scheduling, cancel, media cleanup."""
from __future__ import annotations

import time

import httpx
import pytest
from sqlalchemy import select

from agent import config, db, limits, media, posts, tools

from .conftest import JPEG, MP4, PNG, add_account, add_media


def ok_fb(env):
    env.on("POST", r"/PAGE1/feed$", {"id": "PAGE1_1"}).on("POST", r"/PAGE1/photos$", {"id": "P", "post_id": "PAGE1_2"})


def ok_x(env):
    env.on("POST", r"/2/tweets$", {"data": {"id": "9"}}).on("POST", r"/2/media/upload$", {"data": {"id": "M"}})


# ------------------------------------------------------------------ limits

def test_limits():
    assert limits.x_length("hi https://example.com/very/long/link") == 3 + 23
    assert limits.x_length("日本") == 4
    assert limits.threads_length("ok 😀") == 3 + 4
    jpg = {"filename": "a.jpg", "mime": "image/jpeg", "size": 100}
    png = {"filename": "b.png", "mime": "image/png", "size": 100}
    vid = {"filename": "v.mp4", "mime": "video/mp4", "size": 100}
    assert any("JPEG" in p for p in limits.check("instagram", "x", [png]))
    assert limits.check("instagram", "x", [jpg]) == []
    assert any("needs an image" in p for p in limits.check("instagram", "text only", []))
    assert any("at most 10" in p for p in limits.check("instagram", "", [jpg] * 11))
    assert any("280" in p for p in limits.check("x", "a" * 281, []))
    assert any("one video" in p for p in limits.check("x", "", [vid, jpg]))
    assert any("at most 4" in p for p in limits.check("x", "", [jpg] * 5))
    assert any("500" in p for p in limits.check("threads", "a" * 501, []))
    assert limits.check("threads", "", [png] * 20) == []
    assert any("at most 20" in p for p in limits.check("threads", "", [png] * 21))
    assert any("one video" in p for p in limits.check("youtube", "", [vid, vid]))
    assert any("< or >" in p for p in limits.check("youtube", "a <b>", [vid]))
    assert any("2 MB" in p for p in limits.check("youtube", "", [vid], {**png, "size": 3 * 1024 * 1024}))
    assert any("only applies to a video" in p for p in limits.check("facebook", "x", [jpg], jpg))
    assert limits.check("facebook", "", []) == ["Nothing to post - add text or media"]


def test_x_max_chars_override(monkeypatch):
    monkeypatch.setenv("X_MAX_CHARS", "4000")
    assert limits.check("x", "a" * 1000, []) == []


def test_parse_when(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Asia/Singapore")
    config.reload()
    assert posts.parse_when("now") is None and posts.parse_when("") is None
    assert posts.parse_when("2030-01-01T08:00") == posts.parse_when("2030-01-01T00:00:00Z")
    assert posts.parse_when("2030-01-01T08:00:00+08:00") == 1893456000.0
    assert posts.parse_when(1893456000) == 1893456000.0
    with pytest.raises(ValueError):
        posts.parse_when("tomorrow morning")


# ------------------------------------------------------------------ drafts and publishing

async def test_draft_records_problems_and_blocks_publishing(env):
    await add_account("instagram", "IG1")
    m = await add_media(PNG, "a.png")
    p = await posts.create(text="hello", targets=["instagram"], media_refs=[m["id"]])
    assert p["status"] == "draft"
    assert "JPEG" in p["targets"][0]["problems"]
    with pytest.raises(ValueError, match="Fix these first"):
        await posts.schedule(p["id"], "now")


async def test_unknown_and_ambiguous_targets(env):
    await add_account("facebook", "PAGE1", "One")
    await add_account("facebook", "PAGE2", "Two")
    with pytest.raises(ValueError, match="2 Facebook Page accounts"):
        await posts.create(text="x", targets=["facebook"])
    with pytest.raises(ValueError, match="Not a connected account: tiktok"):
        await posts.create(text="x", targets=["tiktok"])
    p = await posts.create(text="x", targets=["Two"])
    assert [t["account"] for t in p["targets"]] == ["facebook:PAGE2"]


async def test_publish_now_with_variants_and_partial_retry(env):
    ok_fb(env)
    env.on("POST", r"/2/tweets$", httpx.Response(403, json={"title": "Forbidden", "detail": "You are not permitted"}))
    fb, x = await add_account("facebook", "PAGE1"), await add_account("x", "U1", expires_at=9e12)
    p = await posts.create(text="long main text", targets=[fb, x], variants={"twitter": "short"})
    out = await posts.schedule(p["id"], "now", wait=True)
    assert out["status"] == "partial"
    assert out["links"] == {fb: "https://www.facebook.com/PAGE1_1"}
    assert "not permitted" in out["errors"][x]
    assert env.find("POST", "/feed$")[0].form()["message"] == "long main text"

    ok_x(env)                                   # X fixed: retry only posts to X
    out = await posts.schedule(p["id"], "now", wait=True)
    assert out["status"] == "posted"
    assert len(env.find("POST", "/feed$")) == 1
    assert env.find("POST", "/2/tweets$")[-1].json() == {"text": "short"}


async def test_media_must_exist_on_disk(env):
    ok_fb(env)
    fb = await add_account("facebook", "PAGE1")
    m = await add_media()
    p = await posts.create(text="pic", targets=[fb], media_refs=[m["id"]])
    async with db.engine().connect() as c:
        path = (await c.execute(select(db.media.c.path))).scalar()
    import os
    os.unlink(path)
    with pytest.raises(ValueError, match="vanished"):
        await posts.schedule(p["id"], "now")


async def test_scheduler_publishes_when_due(env, monkeypatch):
    monkeypatch.setattr(config.settings, "native_scheduling", False)
    ok_x(env)
    x = await add_account("x", "U1", expires_at=9e12)
    p = await posts.create(text="later", targets=[x])
    when = time.time() + 3600
    out = await posts.schedule(p["id"], when)
    assert out["status"] == "scheduled"
    assert await posts.run_due() == []
    async with db.engine().begin() as c:                  # time passes
        await c.execute(db.posts.update().values(when=time.time() - 1))
    assert await posts.run_due() == [p["id"]]
    assert (await posts.get(p["id"]))["status"] == "posted"
    assert [s["id"] for s in await tools.get_scheduled_posts()] == []


async def test_native_scheduling_and_cancel(env):
    env.on("POST", r"/PAGE1/feed$", {"id": "PAGE1_77"}).on("DELETE", r"/PAGE1_77$", {"success": True})
    ok_x(env)
    fb, x = await add_account("facebook", "PAGE1"), await add_account("x", "U1", expires_at=9e12)
    p = await posts.create(text="tomorrow", targets=[fb, x])
    when = time.time() + 86400
    out = await posts.schedule(p["id"], when, wait=True)
    fb_result = next(t for t in out["targets"] if t["account"] == fb)["result"]
    assert fb_result["native"] and fb_result["ok"]
    form = env.find("POST", "/feed$")[0].form()
    assert form["published"] == "false" and int(form["scheduled_publish_time"]) == int(when)
    assert env.find("POST", "/2/tweets$") == []           # X: built-in scheduler, later

    out = await posts.cancel(p["id"])
    assert out["status"] == "cancelled"
    assert len(env.find("DELETE", "/PAGE1_77$")) == 1


async def test_native_held_post_counts_as_done_at_time(env):
    env.on("POST", r"/PAGE1/feed$", {"id": "PAGE1_78"})
    fb = await add_account("facebook", "PAGE1")
    p = await posts.create(text="t", targets=[fb])
    await posts.schedule(p["id"], time.time() + 86400, wait=True)
    async with db.engine().begin() as c:
        await c.execute(db.posts.update().values(when=time.time() - 1))
    await posts.run_due()
    out = await posts.get(p["id"])
    assert out["status"] == "posted" and len(env.find("POST", "/feed$")) == 1      # not posted twice


async def test_recover_marks_interrupted_posts(env):
    fb = await add_account("facebook", "PAGE1")
    p = await posts.create(text="t", targets=[fb])
    async with db.engine().begin() as c:
        await c.execute(db.posts.update().values(status="posting"))
    await posts.recover()
    out = await posts.get(p["id"])
    assert out["status"] == "failed" and "interrupted" in out["errors"][fb]


# ------------------------------------------------------------------ direct tools

async def test_publish_instagram_tool(env):
    env.on("POST", r"/IG1/media$", {"id": "C1"}).on("GET", r"/C1$", {"status_code": "FINISHED"})
    env.on("POST", r"/media_publish$", {"id": "M1"}).on("GET", r"/M1$", {"permalink": "https://instagram.com/p/1"})
    await add_account("instagram", "IG1")
    m = await add_media()
    out = await tools.publish_instagram_post(caption="hi", media=[m["id"]])
    assert out["status"] == "posted" and out["links"] == {"instagram:IG1": "https://instagram.com/p/1"}
    hist = await tools.get_publishing_history(platform="instagram")
    assert hist[0]["id"] == out["post_id"]


async def test_upload_youtube_video_tool_rejects_bad_thumbnail(env):
    await add_account("youtube", "CH1", expires_at=9e12)
    v = await add_media(MP4, "v.mp4")
    big = await add_media(JPEG + b"\x00" * (3 * 1024 * 1024), "big.jpg")
    with pytest.raises(ValueError, match="2 MB"):
        await tools.upload_youtube_video(video=v["id"], title="t", thumbnail=big["id"])


async def test_import_media_from_url(env):
    env.on("GET", r"cdn.example.com/pic.jpg$", httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
    m = await tools.import_media_url("https://cdn.example.com/pic.jpg")
    assert m["kind"] == "image" and m["mime"] == "image/jpeg"
    env.on("GET", r"example.com/page$", httpx.Response(200, text="<html>", headers={"content-type": "text/html"}))
    with pytest.raises(ValueError, match="web page"):
        await media.import_url("https://example.com/page")


async def test_chunked_base64_upload(env):
    import base64
    data = MP4
    a = await tools.upload_media("v.mp4", base64.b64encode(data[:1500]).decode(), done=False)
    b = await tools.upload_media("v.mp4", base64.b64encode(data[1500:]).decode(), upload_id=a["upload_id"])
    assert b["kind"] == "video" and b["size"] == len(data) and b["mime"] == "video/mp4"


async def test_rejects_non_media(env):
    with pytest.raises(ValueError, match="Only JPEG, PNG"):
        await media.save_bytes(b"%PDF-1.4 hello", "doc.pdf")


# ------------------------------------------------------------------ media cleanup

async def test_media_deleted_only_after_every_platform_is_done(env, monkeypatch):
    monkeypatch.setattr(config.settings, "media_delete_after_minutes", 0)
    ok_fb(env)
    env.on("POST", r"/2/tweets$", httpx.Response(500, json={"title": "boom"}))
    env.on("POST", r"/2/media/upload$", {"data": {"id": "M"}})
    fb, x = await add_account("facebook", "PAGE1"), await add_account("x", "U1", expires_at=9e12)
    m = await add_media()
    unused = await add_media(PNG, "spare.png")
    p = await posts.create(text="pic", targets=[fb, x], media_refs=[m["id"]])
    assert m["id"] not in await media.deletable()          # draft keeps it
    await posts.schedule(p["id"], "now", wait=True)
    assert (await posts.get(p["id"]))["status"] == "partial"
    assert m["id"] not in await media.deletable()          # X still needs it
    ok_x(env)
    await posts.schedule(p["id"], "now", wait=True)
    assert (await posts.get(p["id"]))["status"] == "posted"
    gone = await media.cleanup()
    assert m["id"] in gone and unused["id"] not in gone    # unused kept MEDIA_KEEP_UNUSED_DAYS
    assert await media.rows_for([m["id"]]) == []


async def test_delete_media_keeps_files_in_use(env):
    fb = await add_account("facebook", "PAGE1")
    m = await add_media()
    await posts.create(text="pic", targets=[fb], media_refs=[m["id"]])
    assert (await tools.delete_media([m["id"]]))["kept_in_use"] == [m["id"]]
    assert (await tools.delete_media([m["id"]], force=True))["deleted"] == [m["id"]]
