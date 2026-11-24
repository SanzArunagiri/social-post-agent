"""All settings come from environment variables (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name, "true" if default else "false").lower()
    return v in ("1", "true", "yes", "on")


class Settings:
    """Read once at import; tests call reload() after changing os.environ."""

    def __init__(self) -> None:
        self.api_key = env("API_KEY")
        self.encryption_key = env("ENCRYPTION_KEY")
        self.public_url = env("PUBLIC_URL").rstrip("/")
        self.data_dir = Path(env("DATA_DIR", "/data"))
        self.database_url = env("DATABASE_URL")
        self.timezone = env("TIMEZONE", "UTC")
        self.log_level = env("LOG_LEVEL", "INFO").upper()
        self.api_docs = env_bool("API_DOCS", False)
        # optional: also serve MCP at /mcp/<MCP_URL_KEY> for MCP clients that can't send headers
        self.mcp_url_key = env("MCP_URL_KEY")

        # Meta (Facebook Pages + Instagram professional accounts linked to them)
        self.meta_app_id = env("META_APP_ID")
        self.meta_app_secret = env("META_APP_SECRET")
        self.meta_graph_version = env("META_GRAPH_VERSION", "v23.0")
        # Threads has its own app id / secret (a "Threads" use case in a Meta app)
        self.threads_app_id = env("THREADS_APP_ID")
        self.threads_app_secret = env("THREADS_APP_SECRET")
        # X (OAuth 2.0 with PKCE). The secret is only for confidential clients.
        self.x_client_id = env("X_CLIENT_ID")
        self.x_client_secret = env("X_CLIENT_SECRET")
        # Google (YouTube Data API v3)
        self.google_client_id = env("GOOGLE_CLIENT_ID")
        self.google_client_secret = env("GOOGLE_CLIENT_SECRET")
        self.youtube_default_privacy = env("YOUTUBE_DEFAULT_PRIVACY", "public").lower()
        # Telegram
        self.telegram_bot_token = env("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = env("TELEGRAM_CHAT_ID")
        self.telegram_notify = env("TELEGRAM_NOTIFY", "all").lower()      # all | failures | off

        # media + jobs
        self.media_max_mb = env_int("MEDIA_MAX_MB", 512)
        self.media_delete_after_minutes = env_int("MEDIA_DELETE_AFTER_MINUTES", 30)
        self.media_keep_unused_days = env_int("MEDIA_KEEP_UNUSED_DAYS", 7)
        self.upload_link_minutes = env_int("UPLOAD_LINK_MINUTES", 60)
        self.upload_link_max_files = env_int("UPLOAD_LINK_MAX_FILES", 20)
        self.upload_rate_per_minute = env_int("UPLOAD_RATE_PER_MINUTE", 30)
        self.insights_refresh_hours = env_float("INSIGHTS_REFRESH_HOURS", 6)
        self.scheduler_interval_seconds = env_int("SCHEDULER_INTERVAL_SECONDS", 30)
        self.native_scheduling = env_bool("NATIVE_SCHEDULING", True)

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("UTC")

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    def configured(self) -> dict[str, bool]:
        """Which platforms have developer-app credentials."""
        meta = bool(self.meta_app_id and self.meta_app_secret)
        return {"facebook": meta, "instagram": meta,
                "threads": bool(self.threads_app_id and self.threads_app_secret),
                "x": bool(self.x_client_id),
                "youtube": bool(self.google_client_id and self.google_client_secret),
                "telegram": bool(self.telegram_bot_token and self.telegram_chat_id)}

    def secret_values(self) -> list[str]:
        """Values that must never appear in logs."""
        vals = [self.api_key, self.encryption_key, self.mcp_url_key, self.meta_app_secret, self.threads_app_secret,
                self.x_client_secret, self.google_client_secret, self.telegram_bot_token]
        return [v for v in vals if v and len(v) >= 6]

    def problems(self) -> list[str]:
        out = []
        if len(self.api_key) < 16:
            out.append("API_KEY must be set (16+ characters) - e.g. `openssl rand -hex 32`")
        if len(self.encryption_key) < 16:
            out.append("ENCRYPTION_KEY must be set (16+ characters) - e.g. `openssl rand -hex 32`")
        if self.mcp_url_key and len(self.mcp_url_key) < 24:
            out.append("MCP_URL_KEY must be 24+ characters when set (it acts as a password in the URL)")
        if self.public_url and not self.public_url.startswith(("https://", "http://")):
            out.append("PUBLIC_URL must start with https://")
        return out


settings = Settings()


def reload() -> Settings:
    global settings
    settings.__init__()
    return settings
