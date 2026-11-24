"""Connected social accounts. Tokens are encrypted at rest and never leave this module in API output."""
from __future__ import annotations

import re
import time

from sqlalchemy import delete, insert, select, update

from . import crypto, db

PLATFORMS = ("facebook", "instagram", "threads", "x", "youtube")
NAMES = {"facebook": "Facebook Page", "instagram": "Instagram", "threads": "Threads", "x": "X", "youtube": "YouTube"}
ALIASES = {"fb": "facebook", "facebook page": "facebook", "ig": "instagram", "insta": "instagram",
           "twitter": "x", "yt": "youtube", "shorts": "youtube"}


async def save(platform: str, account_id: str, name: str, token: str, refresh: str | None = None,
               expires_at: float | None = None, meta: dict | None = None) -> str:
    aid = f"{platform}:{account_id}"
    row = dict(platform=platform, account_id=str(account_id), name=name, token=crypto.encrypt(token),
               refresh_token=crypto.encrypt(refresh), expires_at=expires_at, meta=meta or {},
               connected_at=time.time(), error=None)
    async with db.engine().begin() as c:
        await c.execute(delete(db.accounts).where(db.accounts.c.id == aid))
        await c.execute(insert(db.accounts).values(id=aid, **row))
    return aid


def public(r) -> dict:
    m = r._mapping if hasattr(r, "_mapping") else r
    return {"id": m["id"], "platform": m["platform"], "name": m["name"], "account_id": m["account_id"],
            "connected_at": m["connected_at"], "token_expires_at": m["expires_at"], "error": m["error"]}


async def list_accounts() -> list[dict]:
    async with db.engine().connect() as c:
        rows = (await c.execute(select(db.accounts).order_by(db.accounts.c.platform, db.accounts.c.name))).fetchall()
    return [public(r) for r in rows]


async def get(aid: str) -> dict:
    """Account with decrypted tokens - internal use only."""
    async with db.engine().connect() as c:
        r = (await c.execute(select(db.accounts).where(db.accounts.c.id == aid))).first()
    if not r:
        raise ValueError(f"Account {aid} is not connected")
    acc = dict(r._mapping)
    acc["token"] = crypto.decrypt(acc["token"])
    acc["refresh_token"] = crypto.decrypt(acc["refresh_token"])
    acc["meta"] = acc.get("meta") or {}
    return acc


async def update_tokens(aid: str, token: str, refresh: str | None, expires_at: float | None) -> None:
    async with db.engine().begin() as c:
        await c.execute(update(db.accounts).where(db.accounts.c.id == aid).values(
            token=crypto.encrypt(token), refresh_token=crypto.encrypt(refresh), expires_at=expires_at, error=None))


async def set_error(aid: str, error: str | None) -> None:
    async with db.engine().begin() as c:
        await c.execute(update(db.accounts).where(db.accounts.c.id == aid).values(error=(error or None) and error[:500]))


async def disconnect(aid: str) -> bool:
    async with db.engine().begin() as c:
        res = await c.execute(delete(db.accounts).where(db.accounts.c.id == aid))
    return bool(res.rowcount)


def platform_word(word: str) -> str:
    w = (word or "").strip().lower()
    return ALIASES.get(w, w)


async def resolve(targets: list[str] | str | None, platform: str = "") -> list[str]:
    """Account ids for what the caller named: an account id ("x:123"), a name / @handle, or a platform
    ("instagram" - only when exactly one such account is connected). Raises with the choices otherwise."""
    if isinstance(targets, str):
        targets = [t for t in re.split(r"[,\s]+", targets) if t]
    accs = await list_accounts()
    if platform:
        accs = [a for a in accs if a["platform"] == platform]
        targets = targets or [platform]
    if not targets:
        raise ValueError("Name at least one account or platform (see list_social_accounts)")
    out, unknown = [], []
    for raw in targets:
        t = (raw or "").strip()
        low = t.lower().lstrip("@")
        hit = [a["id"] for a in accs if a["id"].lower() == t.lower()] or \
              [a["id"] for a in accs if (a["name"] or "").lower().lstrip("@") == low]
        if not hit:
            plat = platform_word(low)
            cand = [a for a in accs if a["platform"] == plat]
            if len(cand) > 1:
                raise ValueError(f"{len(cand)} {NAMES.get(plat, plat)} accounts are connected - name one: "
                                 + ", ".join(f"{a['id']} ({a['name']})" for a in cand))
            hit = [a["id"] for a in cand]
        if hit:
            out += hit
        else:
            unknown.append(t)
    if unknown:
        have = ", ".join(f"{a['id']} ({a['name']})" for a in accs) or "none - connect one with connect_account"
        raise ValueError(f"Not a connected account: {', '.join(unknown)}. Connected: {have}")
    return list(dict.fromkeys(out))
