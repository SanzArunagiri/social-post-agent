"""Followers and per-post metrics for every connected account, refreshed every INSIGHTS_REFRESH_HOURS.

Best-effort per platform: one that refuses (missing permission, X API read limits) records its error and
the rest carry on. Posts made outside this agent are included too.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from sqlalchemy import delete, insert, select, update

from . import accounts, db, oauth
from .net import client, ok
from .oauth import THREADS, X_API

log = logging.getLogger("agent.insights")
status: dict = {"running": False, "last": 0.0, "errors": {}}
_lock = asyncio.Lock()


def _n(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+0000", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _metric(x: dict) -> int:
    return _n((x.get("values") or [{}])[0].get("value") if x.get("values") else (x.get("total_value") or {}).get("value"))


# --------------------------------------------------------------- per platform -> (followers, extra, posts)

async def _facebook(acc: dict):
    page, tok, g = acc["meta"].get("page_id") or acc["account_id"], acc["token"], oauth.graph()
    async with client() as c:
        info = ok(await c.get(f"{g}/{page}", params={"fields": "followers_count,fan_count", "access_token": tok}))
        d = ok(await c.get(f"{g}/{page}/posts", params={
            "fields": "id,message,created_time,permalink_url,full_picture,shares,"
                      "likes.summary(true).limit(0),comments.summary(true).limit(0)", "limit": 25, "access_token": tok}))
    posts = [{"post_id": p["id"], "ts": _ts(p.get("created_time")), "text": p.get("message") or "", "url": p.get("permalink_url"),
              "kind": "image" if p.get("full_picture") else "text",
              "metrics": {"likes": _n(((p.get("likes") or {}).get("summary") or {}).get("total_count")),
                          "comments": _n(((p.get("comments") or {}).get("summary") or {}).get("total_count")),
                          "shares": _n((p.get("shares") or {}).get("count"))}} for p in d.get("data", [])]
    return _n(info.get("followers_count") or info.get("fan_count")), {"page_likes": _n(info.get("fan_count"))}, posts


async def _instagram(acc: dict):
    ig, tok, g = acc["account_id"], acc["token"], oauth.graph()
    async with client() as c:
        info = ok(await c.get(f"{g}/{ig}", params={"fields": "followers_count,media_count", "access_token": tok}))
        d = ok(await c.get(f"{g}/{ig}/media", params={
            "fields": "id,caption,timestamp,permalink,media_type,like_count,comments_count", "limit": 25, "access_token": tok}))
        posts = []
        for m in d.get("data", []):
            met = {"likes": _n(m.get("like_count")), "comments": _n(m.get("comments_count"))}
            try:   # views / reach / saves / shares need instagram_manage_insights
                ins = ok(await c.get(f"{g}/{m['id']}/insights", params={"metric": "views,reach,saved,shares", "access_token": tok}))
                for x in ins.get("data", []):
                    met[{"saved": "saves"}.get(x["name"], x["name"])] = _metric(x)
            except Exception:
                pass
            posts.append({"post_id": m["id"], "ts": _ts(m.get("timestamp")), "text": m.get("caption") or "",
                          "url": m.get("permalink"), "metrics": met,
                          "kind": {"VIDEO": "reel", "CAROUSEL_ALBUM": "carousel"}.get(m.get("media_type"), "image")})
    return _n(info.get("followers_count")), {"media": _n(info.get("media_count"))}, posts


async def _threads(acc: dict):
    uid, tok = acc["account_id"], await oauth.access_token(acc)
    async with client() as c:
        followers = 0
        try:
            ins = ok(await c.get(f"{THREADS}/{uid}/threads_insights", params={"metric": "followers_count", "access_token": tok}))
            for x in ins.get("data", []):
                if x["name"] == "followers_count":
                    followers = _metric(x)
        except Exception as exc:
            status["errors"][acc["id"] + ":followers"] = str(exc)[:200]
        d = ok(await c.get(f"{THREADS}/{uid}/threads", params={"fields": "id,text,timestamp,permalink,media_type",
                                                               "limit": 25, "access_token": tok}))
        posts = []
        for m in d.get("data", []):
            met = {}
            try:
                ins = ok(await c.get(f"{THREADS}/{m['id']}/insights", params={
                    "metric": "views,likes,replies,reposts,quotes", "access_token": tok}))
                for x in ins.get("data", []):
                    met[{"replies": "comments", "reposts": "shares"}.get(x["name"], x["name"])] = _metric(x)
            except Exception:
                pass
            posts.append({"post_id": m["id"], "ts": _ts(m.get("timestamp")), "text": m.get("text") or "",
                          "url": m.get("permalink"), "metrics": met,
                          "kind": (m.get("media_type") or "TEXT").lower().replace("_post", "")})
    return followers, {}, posts


async def _x(acc: dict):
    tok = await oauth.access_token(acc)
    h = {"Authorization": f"Bearer {tok}"}
    async with client() as c:
        me = ok(await c.get(f"{X_API}/users/me", headers=h, params={"user.fields": "public_metrics"}))["data"]
        pm = me.get("public_metrics") or {}
        posts = []
        try:   # reads are limited / paid on X - tolerate refusals
            d = ok(await c.get(f"{X_API}/users/{me['id']}/tweets", headers=h,
                               params={"max_results": 10, "tweet.fields": "created_at,public_metrics", "exclude": "replies"}))
            for t in d.get("data") or []:
                m = t.get("public_metrics") or {}
                posts.append({"post_id": t["id"], "ts": _ts(t.get("created_at")), "text": t.get("text") or "",
                              "url": f"https://x.com/{acc['name']}/status/{t['id']}", "kind": "post",
                              "metrics": {"views": _n(m.get("impression_count")), "likes": _n(m.get("like_count")),
                                          "comments": _n(m.get("reply_count")),
                                          "shares": _n(m.get("retweet_count")) + _n(m.get("quote_count"))}})
        except Exception as exc:
            status["errors"][acc["id"] + ":posts"] = str(exc)[:200]
    return _n(pm.get("followers_count")), {"posts": _n(pm.get("tweet_count"))}, posts


async def _youtube(acc: dict):
    tok = await oauth.access_token(acc)
    h = {"Authorization": f"Bearer {tok}"}
    api = "https://www.googleapis.com/youtube/v3"
    async with client() as c:
        ch = (ok(await c.get(f"{api}/channels", headers=h, params={"part": "statistics,contentDetails", "mine": "true"}))
              .get("items") or [{}])[0]
        st = ch.get("statistics") or {}
        uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        posts = []
        if uploads:
            items = ok(await c.get(f"{api}/playlistItems", headers=h, params={
                "part": "snippet,contentDetails", "playlistId": uploads, "maxResults": 25})).get("items") or []
            ids = [i["contentDetails"]["videoId"] for i in items]
            stats = {}
            if ids:
                for v in ok(await c.get(f"{api}/videos", headers=h, params={
                        "part": "statistics,contentDetails", "id": ",".join(ids)})).get("items") or []:
                    stats[v["id"]] = v
            for i in items:
                vid = i["contentDetails"]["videoId"]
                v = stats.get(vid) or {}
                s = v.get("statistics") or {}
                dur = (v.get("contentDetails") or {}).get("duration", "")
                short = dur.startswith("PT") and "H" not in dur and ("M" not in dur or dur.startswith(("PT1M", "PT2M", "PT3M")))
                posts.append({"post_id": vid, "ts": _ts(i["snippet"].get("publishedAt")), "text": i["snippet"].get("title") or "",
                              "url": f"https://youtube.com/shorts/{vid}" if short else f"https://youtu.be/{vid}",
                              "kind": "short" if short else "video",
                              "metrics": {"views": _n(s.get("viewCount")), "likes": _n(s.get("likeCount")),
                                          "comments": _n(s.get("commentCount"))}})
    return _n(st.get("subscriberCount")), {"views": _n(st.get("viewCount")), "videos": _n(st.get("videoCount"))}, posts


FETCH = {"facebook": _facebook, "instagram": _instagram, "threads": _threads, "x": _x, "youtube": _youtube}


async def refresh(only: str = "") -> dict:
    if _lock.locked():
        return {"running": True}
    async with _lock:
        status["running"] = True
        done = []
        try:
            for a in await accounts.list_accounts():
                if only and a["id"] != only:
                    continue
                try:
                    acc = await accounts.get(a["id"])
                    followers, extra, posts = await FETCH[acc["platform"]](acc)
                    await _store(acc["id"], followers, extra, posts)
                    status["errors"].pop(acc["id"], None)
                    done.append(acc["id"])
                except Exception as exc:
                    status["errors"][a["id"]] = str(exc)[:300]
                    log.warning("insights for %s failed: %s", a["id"], exc)
        finally:
            status.update(running=False, last=time.time())
        return {"refreshed": done, "errors": dict(status["errors"])}


async def _store(aid: str, followers: int, extra: dict, posts: list[dict]) -> None:
    now = time.time()
    async with db.engine().begin() as c:
        await c.execute(insert(db.follower_stats).values(ts=now, account_id=aid, followers=followers, extra=extra))
        for p in posts:
            key = f"{aid}|{p['post_id']}"[:250]
            row = dict(account_id=aid, post_id=str(p["post_id"])[:120], ts=p.get("ts") or None,
                       text=(p.get("text") or "")[:2000], url=p.get("url"), kind=p.get("kind"),
                       metrics=p.get("metrics") or {}, updated=now)
            if (await c.execute(select(db.post_metrics.c.id).where(db.post_metrics.c.id == key))).first():
                await c.execute(update(db.post_metrics).where(db.post_metrics.c.id == key).values(**row))
            else:
                await c.execute(insert(db.post_metrics).values(id=key, **row))
        await c.execute(delete(db.follower_stats).where(db.follower_stats.c.ts < now - 400 * 86400))


def _eng(m: dict) -> int:
    return sum(_n(m.get(k)) for k in ("likes", "comments", "shares", "saves", "quotes"))


async def overview(account: str = "", days: int = 30, posts_limit: int = 20) -> dict:
    accs = await accounts.list_accounts()
    if account:
        ids = set(await accounts.resolve([account]))
        accs = [a for a in accs if a["id"] in ids]
    aids = [a["id"] for a in accs]
    since = time.time() - days * 86400
    async with db.engine().connect() as c:
        stats = (await c.execute(select(db.follower_stats).where(db.follower_stats.c.account_id.in_(aids))
                                 .order_by(db.follower_stats.c.ts))).fetchall() if aids else []
        feed = (await c.execute(select(db.post_metrics).where(db.post_metrics.c.account_id.in_(aids))
                                .order_by(db.post_metrics.c.ts.desc()))).fetchall() if aids else []
    series: dict[str, list] = {}
    extra: dict[str, dict] = {}
    for r in stats:
        series.setdefault(r.account_id, []).append((r.ts, r.followers or 0))
        extra[r.account_id] = r.extra or {}
    out = []
    for a in accs:
        s = series.get(a["id"]) or []
        cur = s[-1][1] if s else None
        week = next((v for t, v in s if t >= time.time() - 7 * 86400), None)
        start = next((v for t, v in s if t >= since), None)
        mine = [dict(p._mapping) for p in feed if p.account_id == a["id"] and (p.ts or 0) >= since]
        eng = sum(_eng(p["metrics"] or {}) for p in mine)
        out.append({
            "account": a["id"], "platform": a["platform"], "name": a["name"], "followers": cur, "extra": extra.get(a["id"], {}),
            "followers_change_7d": (cur - week) if cur is not None and week is not None else None,
            "followers_change_period": (cur - start) if cur is not None and start is not None else None,
            "posts_in_period": len(mine), "engagement": eng,
            "views": sum(_n((p["metrics"] or {}).get("views")) for p in mine),
            "avg_engagement_per_post": round(eng / len(mine), 1) if mine else 0,
            "error": status["errors"].get(a["id"]),
            "warnings": {k.split(":")[-1]: v for k, v in status["errors"].items() if k.startswith(a["id"] + ":")},
            "posts": [{"post_id": p["post_id"], "published": p["ts"], "kind": p["kind"], "url": p["url"],
                       "text": (p["text"] or "")[:200], "metrics": p["metrics"] or {}, "engagement": _eng(p["metrics"] or {})}
                      for p in mine[:posts_limit]],
        })
    return {"days": days, "accounts": out, "last_refreshed": status["last"] or None, "refreshing": status["running"]}
