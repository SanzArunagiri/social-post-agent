"""Database: a single SQLite file in the data volume by default, Postgres when DATABASE_URL is set."""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Column, Float, Integer, MetaData, String, Table, Text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import settings

metadata = MetaData()
BigInt = BigInteger().with_variant(Integer, "sqlite")

accounts = Table(
    "accounts", metadata,
    Column("id", String(120), primary_key=True),          # "<platform>:<platform account id>"
    Column("platform", String(20), nullable=False),       # facebook | instagram | threads | x | youtube
    Column("account_id", String(100), nullable=False),
    Column("name", String(255)),                          # page / handle / channel name
    Column("token", Text),                                # encrypted
    Column("refresh_token", Text),                        # encrypted
    Column("expires_at", Float),
    Column("meta", JSON),                                 # page_id for Instagram, ...
    Column("connected_at", Float),
    Column("error", Text),
)

posts = Table(
    "posts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created", Float, nullable=False),
    Column("updated", Float),
    # draft | scheduled | posting | posted | partial | failed | cancelled
    Column("status", String(20), nullable=False, index=True),
    Column("text", Text),
    Column("variants", JSON),                             # {"x": "shorter text", ...}
    Column("options", JSON),                              # {"youtube_title": ..., "thumbnail": "<media id>", ...}
    Column("targets", JSON),                              # ["instagram:178...", "x:123"]
    Column("media", JSON),                                # ["<media id>", ...]
    Column("when", Float, index=True),                    # epoch; null = as soon as published
    Column("results", JSON),                              # {target: {ok, id, url, error, native}}
    Column("problems", JSON),                             # {target: "why it can't post"}
    Column("source", String(40)),
    Column("done_at", Float),
)

media = Table(
    "media", metadata,
    Column("id", String(40), primary_key=True),           # random 128-bit hex - also the public URL key
    Column("created", Float),
    Column("filename", String(255)),
    Column("mime", String(120)),
    Column("size", BigInt),
    Column("path", Text),
)

upload_links = Table(
    "upload_links", metadata,
    Column("id", String(64), primary_key=True),           # sha256 of the token (the token itself is never stored)
    Column("created", Float),
    Column("expires", Float),
    Column("note", Text),
    Column("media", JSON),
)

tokens = Table(                                           # short-lived: OAuth state, one-time connect links
    "tokens", metadata,
    Column("id", String(64), primary_key=True),           # sha256 of the token
    Column("kind", String(20)),
    Column("data", Text),                                 # encrypted JSON
    Column("expires", Float, index=True),
)

follower_stats = Table(
    "follower_stats", metadata,
    Column("id", BigInt, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("account_id", String(120), nullable=False, index=True),
    Column("followers", BigInt),
    Column("extra", JSON),
)

post_metrics = Table(
    "post_metrics", metadata,
    Column("id", String(250), primary_key=True),          # "<account id>|<platform post id>"
    Column("account_id", String(120), nullable=False, index=True),
    Column("post_id", String(120), nullable=False),
    Column("ts", Float),
    Column("text", Text),
    Column("url", Text),
    Column("kind", String(30)),
    Column("metrics", JSON),                              # {views, likes, comments, shares, ...}
    Column("updated", Float),
)

_engine: AsyncEngine | None = None


def database_url() -> str:
    url = settings.database_url
    if not url:
        return f"sqlite+aiosqlite:///{settings.data_dir / 'agent.db'}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = database_url()
        if url.startswith("sqlite"):
            settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, pool_pre_ping=not url.startswith("sqlite"))
    return _engine


def kind() -> str:
    return "postgres" if database_url().startswith("postgresql") else "sqlite"


async def init() -> None:
    async with engine().begin() as c:
        await c.run_sync(metadata.create_all)


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
