# social-post-agent

A lightweight, UI-less social media posting agent. It publishes and schedules posts to **Facebook Pages,
Instagram, Threads, X and YouTube** with *your own* developer apps, and exposes everything as a
**REST API** and an **MCP server** — so you can drive it from Claude (or any MCP client), n8n, or a
shell script.

- One container, one volume, `docker compose up`. SQLite by default, Postgres optional.
- Official APIs only, OAuth sign-in per platform, tokens encrypted at rest and refreshed automatically.
- Platform limits are checked *before* posting, with clear errors.
- Native scheduling where the platform has it (Facebook, YouTube); a built-in scheduler for the rest.
- Follower counts and per-post metrics, refreshed on an interval.
- Telegram message on every publish result (optional).
- Media is served at unguessable URLs (Instagram / Threads fetch by URL) and deleted only once every
  platform of every post using it is done.

```
 Claude / MCP client ──► /mcp ─┐
 n8n / scripts ─────────► /api ─┼─► tools ─► publishers ─► Graph API · Threads API · X API v2 · YouTube Data API
 browser (one-time links) ──────┘        └─► scheduler · insights · media store · Telegram
```

## Contents

- [5-minute setup](#5-minute-setup)
- [Create the developer apps](#create-the-developer-apps) — Meta (Facebook + Instagram), Threads, X, Google
- [Connect your accounts](#connect-your-accounts)
- [Use it from Claude](#use-it-from-claude) · [from n8n](#use-it-from-n8n) · [with curl](#rest-api-and-curl-examples)
- [Tools](#tools)
- [Scheduling, media and notifications](#scheduling-media-and-notifications)
- [Platform limitations](#platform-limitations)
- [Security](#security) · [Configuration](#configuration) · [Development](#development)

## 5-minute setup

You need Docker and a public **https** address for the server (Meta, X and Google only redirect to
https, and Instagram / Threads download your media from it) — any reverse proxy works: Caddy, Traefik,
nginx, Dokploy, Coolify, Cloudflare Tunnel.

```bash
git clone https://github.com/SanzArunagiri/social-post-agent.git
cd social-post-agent
cp .env.example .env

# required settings
sed -i "s|^API_KEY=.*|API_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=https://social.example.com|" .env     # your address

docker compose up -d
curl https://social.example.com/health          # {"ok":true,...}
```

Then add the app id / secret of each platform you want to `.env` ([next section](#create-the-developer-apps)),
`docker compose up -d` again, and [connect your accounts](#connect-your-accounts).

Prefer plain `docker run`? Once a release image is published to GHCR:

```bash
docker run -d --name social-post-agent --env-file .env -p 8000:8000 -v social-post-agent-data:/data --restart unless-stopped ghcr.io/sanzarunagiri/social-post-agent:latest
```

The container listens on port 8000 and keeps the database and media in `/data` (the named volume).
The server refuses to start without `API_KEY` and `ENCRYPTION_KEY`; the reason is in `docker compose logs`.

## Create the developer apps

Every redirect URI is `<PUBLIC_URL>/callback/<provider>`. `list_social_accounts` (or
`GET /api/list_social_accounts`) prints the exact URIs for your `PUBLIC_URL`.

| Provider | Redirect URI | `.env` |
|---|---|---|
| Meta (Facebook Pages + Instagram) | `https://YOUR_HOST/callback/facebook` | `META_APP_ID`, `META_APP_SECRET` |
| Threads | `https://YOUR_HOST/callback/threads` | `THREADS_APP_ID`, `THREADS_APP_SECRET` |
| X | `https://YOUR_HOST/callback/x` | `X_CLIENT_ID`, `X_CLIENT_SECRET` |
| Google (YouTube) | `https://YOUR_HOST/callback/youtube` | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |

### Meta — Facebook Pages and Instagram

1. At [developers.facebook.com/apps](https://developers.facebook.com/apps) create an app (type **Business**)
   and add the **Facebook Login for Business** product.
2. Facebook Login → Settings → **Valid OAuth Redirect URIs**: `https://YOUR_HOST/callback/facebook`.
3. The agent asks for these permissions: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`,
   `business_management`, `read_insights`, `instagram_basic`, `instagram_content_publish`,
   `instagram_manage_insights`. In *Development* mode they work for people with a role on the app
   (admins, developers, testers) — enough for your own Pages. To let anyone else connect, request
   Advanced Access through App Review (needs Business Verification).
4. Instagram must be a **professional** (Business or Creator) account **linked to a Facebook Page**.
   Connecting Facebook stores every Page you pick on Meta's consent screen *and* the Instagram account
   linked to each one.
5. App settings → Basic: copy **App ID** and **App Secret** to `META_APP_ID` / `META_APP_SECRET`.

### Threads

1. In a Meta app (a new one, or the one above) add the use case **Access the Threads API**.
2. Use case → Settings: **Redirect callback URLs** `https://YOUR_HOST/callback/threads`,
   **Uninstall callback URL** `https://YOUR_HOST/callback/threads/uninstall`,
   **Delete callback URL** `https://YOUR_HOST/callback/threads/delete`.
3. Permissions: `threads_basic`, `threads_content_publish`, `threads_manage_insights`.
4. While the app is in development, add your Threads profile under App roles → **Threads Testers** and
   accept the invite in the Threads app (Settings → Account → Website permissions → Invites).
5. Copy the **Threads app ID** and **Threads app secret** (shown in the Threads use-case settings — they
   differ from the Meta app's) to `THREADS_APP_ID` / `THREADS_APP_SECRET`.

### X

1. In the [X Developer Console](https://console.x.com) create a project and an app.
2. App → **User authentication settings**: permissions **Read and write**, type **Web App, Automated App
   or Bot**, **Callback URI** `https://YOUR_HOST/callback/x`, plus your website URL.
3. Copy the **OAuth 2.0 Client ID** and **Client Secret** to `X_CLIENT_ID` / `X_CLIENT_SECRET`
   (a public client works with only the ID). Scopes requested: `tweet.read tweet.write users.read
   offline.access media.write`.
4. **The X API is paid.** New developers are on pay-per-use credits (at the time of writing about
   $0.015 per post, more for posts containing a link); buy credits and set a spending limit in the
   console, or posting fails with 402/403.

### Google — YouTube

1. In [Google Cloud Console](https://console.cloud.google.com) create a project and enable the
   **YouTube Data API v3**.
2. **OAuth consent screen**: External, add your Google account as a test user while it is in *Testing*.
   Note: in Testing, Google refresh tokens expire after 7 days — publish the consent screen (In
   production) to stay connected.
3. Credentials → **Create OAuth client ID** → *Web application* → **Authorized redirect URI**
   `https://YOUR_HOST/callback/youtube`. Copy the client ID / secret to `GOOGLE_CLIENT_ID` /
   `GOOGLE_CLIENT_SECRET`.
4. Scopes requested: `youtube.upload`, `youtube.readonly`, `youtube.force-ssl`.
5. **Unverified API projects upload as private.** Videos uploaded through `videos.insert` from projects
   that haven't passed YouTube's API compliance audit are locked to *private*; request the audit with
   the *YouTube API Services – Audit and Quota Extension* form. Default quota: 100 uploads/day plus
   10,000 units/day for other calls.

### Telegram (optional)

Create a bot with [@BotFather](https://t.me/BotFather), send it a message, open
`https://api.telegram.org/bot<TOKEN>/getUpdates` to read your `chat.id`, and set `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID`. `TELEGRAM_NOTIFY=all|failures|off` picks what is sent.

## Connect your accounts

Ask for a one-time sign-in link (valid 15 minutes, single use — your API key never goes in a URL) and
open it in a browser:

```bash
curl -s -X POST https://YOUR_HOST/api/connect_account \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"platform": "facebook"}'
# {"url": "https://YOUR_HOST/connect/facebook?t=...", ...}
```

From Claude, just say *"connect my Instagram"* — it calls `connect_account` and gives you the link.
After you approve, the accounts appear in `list_social_accounts` with ids like `facebook:1234`,
`instagram:1784…`, `threads:…`, `x:…`, `youtube:UC…`. Use an id, an account name, or simply the
platform name (when only one account of it is connected) as a target.

Tokens are stored encrypted. X and Google tokens refresh automatically; Threads tokens (60 days) are
refreshed in the background; Facebook / Instagram Page tokens don't expire.

## Use it from Claude

The MCP endpoint is `https://YOUR_HOST/mcp` (streamable HTTP). It needs the API key in a header:
`Authorization: Bearer <API_KEY>` (or `X-API-Key: <API_KEY>`).

- **Claude (web / desktop / mobile)**: Settings → Connectors → **Add custom connector** → URL
  `https://YOUR_HOST/mcp`, then under **Request headers** add `Authorization` = `Bearer <API_KEY>`.
  If your workspace doesn't offer request headers yet, set `MCP_URL_KEY` to a long random string and
  use `https://YOUR_HOST/mcp/<MCP_URL_KEY>` as the connector URL with no authentication — treat that
  URL like a password.
- **Claude Code**:
  ```bash
  claude mcp add --transport http social https://YOUR_HOST/mcp --header "Authorization: Bearer $API_KEY"
  ```

Then ask things like *"Post this photo to Instagram and Facebook tomorrow at 9am with a shorter version
for X"* or *"How did my posts do this week?"*. Files in the chat can't be streamed over MCP, so Claude
either sends them base64 (`upload_media`, in parts for big files) or gives you an upload link
(`create_upload_link`) to drop them from your device.

## Use it from n8n

- **HTTP Request node**: Method `POST`, URL `https://YOUR_HOST/api/<tool>`, Authentication → *Generic
  Credential Type* → *Header Auth* with name `Authorization` and value `Bearer <API_KEY>`, Body → JSON
  with the tool's arguments (see [Tools](#tools)). Upload a binary file with `POST /api/media` as
  *Form-Data* (field `file`) and use the returned `id`.
- **AI Agent + MCP Client Tool node**: endpoint `https://YOUR_HOST/mcp`, transport *HTTP Streamable*,
  authentication *Bearer* with the API key.

## Tools

The same tools are MCP tools and REST endpoints: `POST /api/<name>` with a JSON object of arguments
(read-only tools also accept `GET /api/<name>?arg=value`). Errors come back as
`{"error": "..."}` with 400 (bad input / limit check), 502 (the platform refused) or 422 (invalid
arguments).

| Tool | What it does |
|---|---|
| `list_social_accounts` | Connected accounts, configured platforms, redirect URIs |
| `connect_account` | One-time browser link to connect `facebook` (+ Instagram), `instagram`, `threads`, `x`, `youtube` |
| `disconnect_account` | Forget an account and its tokens |
| `create_post_draft` | Save a post: `text`, `targets`, per-platform `variants`, `media` (ids or URLs), `options`, `when` — returns limit problems per target |
| `schedule_post` | Publish a draft (or new content) at `when`, or `"now"`; retries failed targets of a partial post; moves a scheduled one |
| `cancel_post` | Cancel a draft / scheduled post (removes copies held by Facebook / YouTube) |
| `get_post` | One post with per-target results |
| `get_scheduled_posts` | Upcoming posts, soonest first |
| `get_publishing_history` | Posts with per-platform result, links, errors (filter by `status`, `platform`) |
| `publish_facebook_post` | Page post now: text (+ `link`), photo, up to 10 photos, or a video (+ `thumbnail`) |
| `publish_instagram_post` | JPEG image, carousel (2–10), or Reel (+ `reel_cover`, `share_reel_to_feed`) |
| `publish_threads_post` | Text, image, video or carousel (2–20), optional `topic_tag` |
| `publish_x_post` | Text with up to 4 images or one video / GIF (OAuth 2.0 PKCE, media upload v2) |
| `upload_youtube_video` | Resumable upload with `title`, `description`, `privacy`, `thumbnail`, `publish_at`, `tags`… |
| `get_social_insights` | Followers (+7-day / period change) and recent posts with views / likes / comments / shares |
| `send_telegram_notification` | Message through your Telegram bot |
| `upload_media` | Base64 upload, in one call or in parts (`upload_id`, `done`) |
| `import_media_url` | Download an https link (Google Drive / Dropbox share links work) into the media store |
| `create_upload_link` / `get_uploaded_media` | One-time upload page for a person; then the media ids |
| `delete_media` | Delete stored files now (files still needed are kept unless `force`) |

`publish_*` and `upload_youtube_video` wait (up to ~4 minutes) and return the result; pass `wait: false`
for big videos and check `get_post` later. A publish keeps running even if the caller disconnects.

**`options`** for `create_post_draft` / `schedule_post`: `thumbnail` (media id or link — YouTube
thumbnail, Instagram Reel cover, Facebook video thumbnail), `youtube_title`, `youtube_description`,
`youtube_privacy`, `youtube_tags`, `youtube_category_id`, `youtube_made_for_kids`,
`youtube_notify_subscribers`, `youtube_short`, `threads_topic`, `instagram_share_to_feed`,
`facebook_link`, `facebook_video_title`.

**Times** are ISO 8601: `2026-12-01T09:30:00+08:00`, or `2026-12-01T09:30` in `TIMEZONE`.

## REST API and curl examples

```bash
H=(-H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json")
BASE=https://YOUR_HOST

# accounts
curl -s "${H[@]}" $BASE/api/list_social_accounts

# upload a file (multipart, streamed to disk) -> {"id": "...", "url": ".../media/<id>.jpg"}
curl -s -H "Authorization: Bearer $API_KEY" -F "file=@photo.jpg" $BASE/api/media

# post to Facebook now
curl -s "${H[@]}" $BASE/api/publish_facebook_post -d '{"text": "Hello from the API"}'

# Instagram carousel straight from public links
curl -s "${H[@]}" $BASE/api/publish_instagram_post \
  -d '{"caption": "New collection", "media": ["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"]}'

# one post, several platforms, a shorter X version, tomorrow 09:00
curl -s "${H[@]}" $BASE/api/schedule_post -d '{
  "targets": ["facebook", "instagram", "x"],
  "text": "We just shipped v2 - faster uploads, scheduled posts and more. Details on our blog.",
  "variants": {"x": "v2 is out: faster uploads + scheduling"},
  "media": ["<media id>"],
  "when": "2026-12-01T09:00:00+08:00"}'

# YouTube upload, published by YouTube itself at a time, with a custom thumbnail
curl -s "${H[@]}" $BASE/api/upload_youtube_video -d '{
  "video": "<video media id>", "title": "Launch walkthrough", "description": "All the details...",
  "thumbnail": "<image media id>", "publish_at": "2026-12-02T18:00:00Z", "wait": false}'

# what is coming up, what happened
curl -s "${H[@]}" "$BASE/api/get_scheduled_posts"
curl -s "${H[@]}" "$BASE/api/get_publishing_history?limit=10&status=failed"

# insights (fresh)
curl -s "${H[@]}" $BASE/api/get_social_insights -d '{"refresh": true, "days": 7}'

# Telegram
curl -s "${H[@]}" $BASE/api/send_telegram_notification -d '{"text": "Deploy finished"}'
```

Set `API_DOCS=true` to get interactive OpenAPI docs at `/docs`.

## Scheduling, media and notifications

**Scheduling.** `schedule_post` with a future time:

- **Facebook** (10 minutes to 30 days ahead) and **YouTube videos** (15+ minutes ahead) are handed to
  the platform right away (`native: true` in the result) and go out even if this server is down. Moving
  or cancelling the post removes the platform's copy.
- **Instagram, Threads and X** have no scheduling API: the built-in scheduler (every
  `SCHEDULER_INTERVAL_SECONDS`) publishes them at the time, so the server must be running then.
- `NATIVE_SCHEDULING=false` makes the built-in scheduler do everything.

Post status: `draft → scheduled → posting → posted | partial | failed`, or `cancelled`. Scheduling a
`partial` / `failed` post again retries only the targets that failed. A post interrupted by a restart
is marked failed for the remaining targets (check the platform before retrying).

**Media.** Images and videos are stored under `/data/media` (the real type is read from the file's first bytes, so a PNG named `.jpg` is caught before Instagram rejects it) and served at
`<PUBLIC_URL>/media/<128-bit random id>.<ext>` (public, so platforms can fetch them; range requests
supported). A file is deleted `MEDIA_DELETE_AFTER_MINUTES` after **every post that uses it is posted on
every platform**. Drafts, scheduled, partial and failed posts keep their files (cancel a post to release
them); files no post uses are kept `MEDIA_KEEP_UNUSED_DAYS`.

**Notifications.** With Telegram set up, every publish result (and native scheduling) sends a message
with the link or the error for each platform.

## Platform limitations

| Platform | Posts | Not possible / caveats |
|---|---|---|
| Facebook Page | text (+ link), 1–10 photos, 1 video (+ thumbnail) | Pages only (no personal profiles). Native scheduling 10 min – 30 days ahead |
| Instagram | JPEG image, carousel 2–10, Reel (+ cover) | **JPEG only** for images (convert PNG/WebP), caption 2,200 chars, 30 hashtags; professional account linked to a Page; **no native scheduling**; a rolling 24 h cap on API posts (see `content_publishing_limit`); Stories not supported |
| Threads | text 500 chars, image, video (≤ 5 min), carousel 2–20 | Emojis count as several chars; one topic tag; **no native scheduling**; 250 API posts per 24 h |
| X | text 280 (weighted; links = 23), ≤ 4 images or 1 video / GIF | **The API is paid** (pay-per-use credits); **no native scheduling**; reading your own posts for insights also costs credits and may be refused |
| YouTube | video upload (resumable), custom thumbnail, `publishAt` | Unaudited Google projects upload **private only**; custom thumbnails need a verified channel; 100 uploads/day default; **image / text (community) posts are pilot-only** — most projects get 403 |

All platform limits checked before posting live in `agent/limits.py`.

## Security

- Every endpoint needs the API key (`Authorization: Bearer` or `X-API-Key`, compared in constant time)
  except: `/health`, the OAuth callbacks, `/media/<random id>`, one-time upload pages `/u/<token>` and
  one-time connect links `/connect/<platform>?t=<token>`.
- Failed authentication is rate-limited per IP (30/min); upload pages are rate-limited per IP and per
  link (`UPLOAD_RATE_PER_MINUTE`) and capped at `UPLOAD_LINK_MAX_FILES` files; links expire.
- Platform tokens are encrypted at rest (Fernet, key derived from `ENCRYPTION_KEY`) and never returned
  by the API. One-time tokens are stored as SHA-256 hashes.
- Logs never contain secrets: access tokens, `Authorization` headers, one-time tokens and every secret
  from `.env` are redacted; httpx request logging is off.
- Media links fetched for you go through an SSRF guard (public addresses only, every redirect checked).
- Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` so per-IP limits see the real client address.
- Anyone with the API key can post as you — keep it secret and rotate it if leaked.

## Configuration

Everything is configured through `.env` — see [`.env.example`](.env.example), where every setting is
commented. To use Postgres, set `DATABASE_URL=postgresql://user:pass@host:5432/db` (tables are created
on start).

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q            # publishers, scheduling, OAuth, REST + MCP, all with mocked HTTP
ruff check agent tests
API_KEY=dev-key-0123456789abcd ENCRYPTION_KEY=dev-enc-0123456789abcd DATA_DIR=./data \
  PUBLIC_URL=http://localhost:8000 uvicorn agent.main:app --reload
```

```
agent/
  main.py        FastAPI app: auth, rate limits, REST routes, MCP mount, OAuth + media + upload routes, loops
  tools.py       the tools (one definition for REST and MCP)
  posts.py       drafts, publishing, scheduling (native + built-in), history
  publishers.py  Facebook, Instagram, Threads, X, YouTube publishing
  limits.py      per-platform checks
  oauth.py       connect / callback / token refresh per provider
  accounts.py    connected accounts (encrypted tokens), target resolution
  media.py       storage, uploads (multipart, base64 chunks, URL, upload links), cleanup
  insights.py    followers and post metrics
  notify.py      Telegram
  net.py         HTTP client, error parsing, SSRF guard, log redaction
  crypto.py      encryption at rest, one-time tokens
  db.py          SQLAlchemy tables (SQLite / Postgres)
```

CI runs the tests and builds the image on every push; pushing a `v*` tag publishes
`ghcr.io/sanzarunagiri/social-post-agent` (multi-arch). The first time, make the package public under
the repository's *Packages* settings so `docker run` works without logging in.

## License

[MIT](LICENSE)
