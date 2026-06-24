# Pelican-Kuma-GameMonitor

A small container that uses Pelican/Pterodactyl Panel APIs + Uptime Kuma to automatically:
- create Push monitors named: `<Pelican Server Name>`
- push UP based on the panel's server state (running/starting)
- delete stale managed monitors if they have had no heartbeat for > N days
- maintain a **dynamic Uptime Kuma status page** listing all managed servers, grouped by wing/node
- sync **Pelican power schedules into Kuma maintenance windows** so scheduled-off time shows as maintenance, not degraded (opt-in)
- send **per-game Discord notifications** (a separate webhook per server) on up/down changes
- expose a small **admin web UI** to manage the per-game Discord webhooks
- apply **configurable branding** (name, colors, logo, webhook avatar) — defaults are neutral; this deployment is branded **GamerSaloon**

## How it works
- Lists servers via Pelican **Application API** (`PEL_APP_KEY`)
- Reads each server's current state via Pelican **Client API** (`PEL_CLIENT_KEY`)
- Creates monitors in Uptime Kuma using the internal Socket.IO API (requires `KUMA_USER` + `KUMA_PASS`)
- Pushes heartbeat to each monitor

## Required permissions

### Pelican

This project uses **two** Pelican API keys:

- **`PEL_APP_KEY` (Application API key)**
  - Create in the Pelican **Admin / Application API Keys** area.
  - Required permissions:
    - **Servers: Read** (lists servers + gets `name` and `identifier`)
    - **Nodes: Read** *(required for wing tagging)* (resolves the Pelican wing/node name so monitors can be tagged `wing:<node name>`)
  - If **Nodes: Read** is not granted, the container will still run, but **wing tagging will be disabled**.

- **`PEL_CLIENT_KEY` (Client API key)**
  - Create in the Pelican UI under **Profile → API Keys**.
  - Must be created from a user that can access the servers you want monitored (an admin account is easiest).
  - Used to call `GET /api/client/servers/<identifier>/resources` to read `current_state`.
  - **Allowed IPs:** leave blank to allow any IP, or include the IP of the machine running this container. If the container IP is not allowed, you will see `401 Unauthenticated`.

### Uptime Kuma

The container logs into Uptime Kuma and needs an account that can:
- create monitors (Push type)
- create tags and attach tags to monitors
- delete monitors (for stale cleanup)

### Discord (optional, per game)

This project can post **up/down state change** messages to Discord, with a **separate webhook per
game server**. Notifications are sent as branded embeds (webhook username + avatar from the branding
config). A server with no webhook of its own falls back to the optional global `DISCORD_WEBHOOK_URL`.

Required Discord permissions:
- You must have **Manage Webhooks** permission in the target channel (or server) to create the webhook.
- The webhook itself needs permission to **Send Messages** in the channel.

How to configure:
1) In Discord, create a webhook per channel: channel settings → **Integrations** → **Webhooks** → **New Webhook**, and copy each **Webhook URL**.
2) Open the **admin UI** (see below), assign each game server its webhook URL, and **Save**. Use **Test** to verify.
3) Assignments are stored in `/data/webhooks.json` and picked up on the next monitor run (no restart needed).

### Admin UI

A small web interface (Flask, served on `ADMIN_PORT`, default `8080`) for managing the per-game
Discord webhooks. It lists the game servers discovered by the monitor and lets you set/test a webhook
per server plus an optional default. Protect it by setting `ADMIN_USER` + `ADMIN_PASS` (HTTP basic
auth); if either is unset, auth is disabled and a warning is logged.

### Dynamic status page

On every run the monitor reconciles a single Uptime Kuma status page (slug `STATUS_PAGE_SLUG`,
default `gamersaloon`) containing all managed monitors, grouped by Pelican **wing/node**. Disable with
`STATUS_PAGE_ENABLED=0`.

### Maintenance sync (scheduled-off ≠ degraded)

If your servers are powered on/off by **Pelican scheduled jobs**, a server that's off during its
expected hours would otherwise show as "down"/degraded on the status page. With
`MAINTENANCE_SYNC_ENABLED=1`, the monitor reads each server's power schedules
(`/api/client/servers/<id>/schedules`), derives the expected-**offline** windows (online = from a
`power start` to the next `power stop`; offline = the complement), and mirrors them into Kuma
**maintenance windows** — so scheduled downtime shows as "maintenance" and is excluded from the
degraded calc and uptime %.

- Requires the **Client API key** to be able to read schedules.
- `SCHEDULE_TZ` must match the timezone your Pelican panel evaluates crons in (**UTC** by default);
  windows are created in that same timezone. (Pelican often evaluates crons in UTC even when you
  *labelled* the schedules in local time — check that a "stop at 2 AM" really means 2 AM UTC.)
- Servers whose schedules aren't clean `power start`/`stop` pairs (complex crons, start-only) are
  logged and left unmanaged rather than guessed.
- Tool-created maintenances are tracked in `/data/maintenance_state.json` and reconciled
  (create/update/delete); maintenances you make by hand in Kuma are never touched.

### Branding

Branding is config-driven and editable two ways: the `BRAND_*` env vars (initial defaults) and the
**Branding** section of the admin UI, which persists overrides to `/data/branding.json`. Precedence is
admin override → env var → neutral default. Code defaults are neutral (`GameMonitor`); this deployment
sets `BRAND_NAME=GamerSaloon`. The logo and webhook avatar should be **public absolute URLs** so
Discord and the Kuma status page can fetch them — see `app/static/brand/README.md`.

## Troubleshooting

- **`401 Unauthenticated` from `/api/client/servers/<id>/resources`:** your `PEL_CLIENT_KEY` is invalid, expired, not copied correctly, or blocked by Allowed IPs.
- **`403 Forbidden` calling `/api/application/nodes`:** your `PEL_APP_KEY` is missing **Nodes: Read**. Wing tagging will be disabled until that permission is granted.

## Environment Variables

### Required
**Uptime Kuma**
- `KUMA_URL=http://<INSERT IP HERE>:<INSERT PORT HERE>`
- `KUMA_USER=<INSERT USER>`
- `KUMA_PASS=<INSERT PASS>`

**Pelican Panel**
- `PEL_URL=http://<INSERT IP HERE>:<INSERT PORT HERE>`
- `PEL_APP_KEY=<INSERT APP KEY HERE>`  (Application API key, used for /api/application/*)
- `PEL_CLIENT_KEY=<INSERT CLIENT KEY HERE>` (Client key from Profile → API Keys, used for /api/client/*)

### Optional (defaults shown)
General
- `DEBUG=0` (set `1` to print state + tag actions in logs)
- `KUMA_SSL_VERIFY=1`
- `PEL_SSL_VERIFY=1`

Scheduling / retention
- `KUMA_INTERVAL=60` (push monitor interval in seconds)
- `KUMA_STALE_DAYS=7` (delete managed monitors if no heartbeat newer than this)

Caching
- `CACHE_PATH=/data/pelican_servers_cache.json`
- `CACHE_TTL_SECONDS=300`
- `PEL_NODE_CACHE_PATH=/data/pelican_nodes_cache.json`
- `PEL_NODE_CACHE_TTL_SECONDS=3600`

Tagging (used for grouping + cleanup)
- `KUMA_MANAGED_TAG=managed:pelican`
- `KUMA_WING_TAG_PREFIX=wing`
- `KUMA_TAG_COLOR=#0ea5e9`

Discord notifications (optional)
- `DISCORD_WEBHOOK_URL=` (global fallback webhook; per-server webhooks are set in the admin UI and stored in `/data/webhooks.json`)
- `DISCORD_STATE_PATH=/data/discord_state.json`
- `DISCORD_CONFIRM_RUNS=2` (a status change must persist this many consecutive runs (~minutes) before notifying; debounces transient up/down flaps. `1` = notify on every change)
- `DISCORD_USE_BRAND_IDENTITY=0` (by default each webhook's own Discord-configured name + avatar identify the message; set `1` to override with the brand username/avatar)
- `STATUS_PAGE_PUBLIC_URL=` (public status page URL, e.g. `status.gamernight.net`; when set, a "View live status" link is appended to notifications)

Admin UI
- `ADMIN_PORT=8080`
- `ADMIN_PASS=` (set it to require HTTP basic auth; auth is OFF only when empty)
- `ADMIN_USER=admin` (username; defaults to `admin`)
- `ADMIN_SECRET_KEY=` (Flask session/flash secret)

Status page
- `STATUS_PAGE_ENABLED=1`
- `STATUS_PAGE_SLUG=gamersaloon`
- `STATUS_PAGE_TITLE=` (defaults to `<BRAND_NAME> Game Servers`)
- `STATUS_PAGE_THEME=dark` (`light` or `dark`)
- `STATUS_PAGE_UNGROUPED_LABEL=Other` (group name for monitors with no wing tag)
- `STATUS_PAGE_ICON=/static/brand/icon.png` (local path → embedded data URI, or an absolute URL)
- `STATUS_PAGE_CUSTOM_CSS=` (override the branded theme) / `STATUS_PAGE_BRAND_CSS=1` (set `0` to manage CSS yourself)

Maintenance sync (scheduled-off → maintenance, not degraded)
- `MAINTENANCE_SYNC_ENABLED=0` (opt-in; set `1` to enable)
- `SCHEDULE_TZ=UTC` (timezone Pelican evaluates schedule crons in; windows created in this tz)
- `SCHEDULE_CACHE_TTL_SECONDS=3600`
- `MAINTENANCE_STATE_PATH=/data/maintenance_state.json`
- `SCHEDULE_CACHE_PATH=/data/pelican_schedules_cache.json`

Branding (defaults are neutral; set per deployment; all also editable in the admin UI)
- `BRAND_NAME=GameMonitor`
- `BRAND_LOGO_URL=` (status page + admin logo; absolute URL, or a `/static/brand/...` path served by this container)
- `BRAND_AVATAR_URL=` (Discord webhook avatar — defaults to logo)
- `BRAND_ASSET_BASE_URL=` (container's externally-reachable base; turns a `/static/brand/...` path into an absolute URL for Discord/Kuma)
- `BRAND_WEBHOOK_USERNAME=` (defaults to `BRAND_NAME`)
- `BRAND_COLOR=#0ea5e9` (hex; admin accent + embed color)
- `BRAND_URL=`

GamerSaloon assets ship in `app/static/brand/` (`logo.png`, `avatar.png`, watermark-free) and are referenced via the `/static/brand/...` paths above.

Optional naming (OFF by default)
- `KUMA_NAME_PREFIX=` (leave empty to use *only* the Pelican server name)

## Run

```
docker run -d --name pelican-kuma-gamemonitor \
  --restart unless-stopped \
  -p 8080:8080 \
  -e KUMA_URL="http://<INSERT IP HERE>:<INSERT PORT HERE>" \
  -e KUMA_USER="<INSERT USER>" \
  -e KUMA_PASS="<INSERT PASS>" \
  -e PEL_URL="http://<INSERT IP HERE>:<INSERT PORT HERE>" \
  -e PEL_APP_KEY="<INSERT APP KEY HERE>" \
  -e PEL_CLIENT_KEY="<INSERT CLIENT KEY HERE>" \
  -e BRAND_NAME="GamerSaloon" \
  -e BRAND_COLOR="#c79a3b" \
  -e BRAND_LOGO_URL="<PUBLIC LOGO URL>" \
  -e BRAND_AVATAR_URL="<PUBLIC AVATAR URL>" \
  -e ADMIN_USER="admin" \
  -e ADMIN_PASS="<CHOOSE A PASSWORD>" \
  -v pelican-kuma-gamemonitor-data:/data \
  ghcr.io/sjdodge123/pelican-kuma-gamemonitor:latest
```

The admin UI is then available at `http://<host>:8080`.
