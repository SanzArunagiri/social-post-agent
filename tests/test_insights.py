"""Insights: followers + per-post metrics; one platform failing doesn't stop the others."""
from __future__ import annotations

import httpx

from agent import insights, oauth, tools

from .conftest import add_account


async def test_insights_refresh_and_overview(env):
    env.on("GET", r"graph.facebook.com/v[\d.]+/PAGE1$", {"followers_count": 1200, "fan_count": 1100})
    env.on("GET", r"/PAGE1/posts$", {"data": [{"id": "PAGE1_1", "message": "hello", "created_time": "2099-01-01T00:00:00+0000",
                                               "permalink_url": "https://fb.com/1", "shares": {"count": 3},
                                               "likes": {"summary": {"total_count": 10}},
                                               "comments": {"summary": {"total_count": 2}}}]})
    env.on("GET", r"api.x.com/2/users/me$", {"data": {"id": "U1", "public_metrics": {"followers_count": 50, "tweet_count": 9}}})
    env.on("GET", r"api.x.com/2/users/U1/tweets$", httpx.Response(429, json={"title": "Too Many Requests"}))
    env.on("GET", r"googleapis.com/youtube/v3/channels$", httpx.Response(401, json={"error": {"message": "bad token"}}))
    await add_account("facebook", "PAGE1", "Acme")
    await add_account("x", "U1", "acme", expires_at=9e12)
    await add_account("youtube", "CH1", "Chan", expires_at=9e12)

    out = await tools.get_social_insights(refresh=True, days=36500)
    by = {a["account"]: a for a in out["accounts"]}
    fb = by["facebook:PAGE1"]
    assert fb["followers"] == 1200 and fb["posts_in_period"] == 1 and fb["engagement"] == 15
    assert fb["posts"][0]["metrics"] == {"likes": 10, "comments": 2, "shares": 3}
    assert by["x:U1"]["followers"] == 50 and "Too Many Requests" in by["x:U1"]["warnings"]["posts"]
    assert "bad token" in by["youtube:CH1"]["error"] and by["youtube:CH1"]["followers"] is None

    one = await insights.overview("x", 30)
    assert [a["account"] for a in one["accounts"]] == ["x:U1"]


async def test_threads_token_kept_alive(env):
    import time
    env.on("GET", r"graph.threads.net/refresh_access_token$", {"access_token": "fresh", "expires_in": 5184000})
    aid = await add_account("threads", "T1", expires_at=time.time() + 86400)
    assert await oauth.refresh_due() == [aid]
    from agent import accounts
    assert (await accounts.get(aid))["token"] == "fresh"
