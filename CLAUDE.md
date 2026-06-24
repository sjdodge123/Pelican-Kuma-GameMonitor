# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A container that bridges a Pelican/Pterodactyl panel to an Uptime Kuma instance. It runs **two processes** (started by `entrypoint.sh`):

1. **Monitor** (`app/update_kuma.py`) — run once per minute by supercronic. Reconciles Uptime Kuma push monitors against current Pelican server state: creates monitors for running servers, pushes heartbeats, tags them, deletes stale managed monitors, reconciles a dynamic status page, and sends per-game Discord notifications.
2. **Admin UI** (`app/admin.py`) — a Flask app (served by gunicorn on `ADMIN_PORT`, default 8080) for managing per-game Discord webhooks.

Shared modules: `app/store.py` (atomic JSON config/state in `/data`; also exposes `read_json`/`write_json` reused by the monitor), `app/branding.py` (config-driven branding), `app/settings.py` (admin-editable optional runtime tunables, override→env→default), `app/notify.py` (`send_discord`, imported by both the monitor and the admin UI), and `app/maintenance.py` (Pelican-schedule → Kuma-maintenance sync; `compute_off_windows` is a pure, unit-testable parser). There is no test suite, build step, or linter configured.

Imports are **flat** (`from store import ...`, `from branding import ...`), not a package — the monitor runs as a script (cwd `/app/app`) and gunicorn uses `--chdir /app/app`. Keep it this way; do not add `app/__init__.py` or package-relative imports.

## Running

The script is designed to run once per invocation (it is not a long-lived loop). Supercronic runs it every minute inside the container via `crontab`.

Run the container (see `README.md` for the full env var list):
```
docker build -t pelican-kuma-gamemonitor .
docker run --rm -e KUMA_URL=... -e KUMA_USER=... -e KUMA_PASS=... \
  -e PEL_URL=... -e PEL_APP_KEY=... -e PEL_CLIENT_KEY=... \
  -v pelican-kuma-data:/data pelican-kuma-gamemonitor
```

Run a single monitor iteration locally against live services (requires the same env vars; copy `.env.example`):
```
pip install -r requirements.txt
cd app && python -u update_kuma.py
```
Run the admin UI locally:
```
cd app && python -u admin.py   # or: gunicorn --chdir app -b 0.0.0.0:8080 admin:app
```
Set `DEBUG=1` to print per-server state and tag/push actions — essential for diagnosing behavior since the script is otherwise silent.

Releases: pushing a git tag matching `v*` triggers `.github/workflows/release-ghcr.yml`, which builds and publishes to `ghcr.io/sjdodge123/pelican-kuma-gamemonitor`.

## Architecture & key concepts

`main()` in `app/update_kuma.py` is the entire reconciliation pass, in order:

1. **Two Pelican APIs, two keys.** Server listing + node names come from the **Application API** (`PEL_APP_KEY`, `/api/application/*`). Per-server live state comes from the **Client API** (`PEL_CLIENT_KEY`, `/api/client/servers/<id>/resources`). These are distinct keys with distinct permissions — a common source of `401`/`403` errors (see README troubleshooting).

2. **Caching.** Server and node lists are cached together to `/data` (TTL-controlled) to avoid hammering the panel on every minute-by-minute run. Live server state is *not* cached — it is fetched fresh each run.

3. **Running = up.** Only servers whose `current_state` is in `RUNNING_STATES` (`running`/`starting`) get a monitor created and a heartbeat pushed. Everything else is treated as `down`.

4. **Monitor identity is the name.** Monitors are matched to servers by name (`monitor_name()`), and only Kuma monitors with a `pushToken` are considered managed by this tool. There is no stored ID mapping.

5. **Tagging.** Monitors get the managed tag (`KUMA_MANAGED_TAG`, default `managed:pelican`) and, if Application-API node read permission is granted, a `wing:<node>` tag. Tags are only applied to *newly created* monitors. The managed tag is the safety gate for deletion.

6. **Stale cleanup.** A monitor is deleted only if its last heartbeat is older than `KUMA_STALE_DAYS` **and** it carries the managed tag. `get_monitor_tags()` reads tags from the bulk `get_monitors()` payload when present and only falls back to a per-monitor `get_monitor()` call when they're absent (avoids an N+1 every run). Non-push monitors are skipped entirely. This is what prevents the tool from deleting monitors it didn't create.

7. **Discord (per-game).** Webhooks are resolved per server identifier via `store.webhook_for()` — a server's own webhook, else the global `DISCORD_WEBHOOK_URL` fallback. Discord is "enabled" if any webhook (default or per-server) is configured. Prior status per server is persisted to `DISCORD_STATE_PATH`; a notification fires only on a status *change* and only after a server has been seen once (no first-run spam). Changes are **debounced** to suppress flap noise: the `status` field is the last *confirmed* status (the notification baseline), while `pending`/`pending_count` track a not-yet-confirmed change — a new status must hold for `DISCORD_CONFIRM_RUNS` consecutive runs (default 2, ~minutes) before it's confirmed and notified; a status that reverts before then is discarded silently. Heartbeat pushes to Kuma are *not* debounced (Kuma has its own retry logic) — only Discord. Messages are embeds whose **color** is branded; the message **name/avatar** default to each webhook's own Discord-configured identity (so per-channel webhooks carry their own game's name/icon), unless `DISCORD_USE_BRAND_IDENTITY=1` overrides them with the brand username/avatar. When `STATUS_PAGE_PUBLIC_URL` is set, a "View live status" markdown link is appended to the notification body.

8. **Dynamic status page.** `reconcile_status_page()` maintains a single Kuma status page (`STATUS_PAGE_SLUG`) with all managed monitors grouped by wing/node. Wing grouping is derived in the same monitor-tag pass that does stale cleanup, bucketing each managed monitor by its `wing:<node>` tag into `status_groups` (and retro-tagging a running monitor that's missing its wing tag). The reconcile fetches the **live** page once and compares the desired managed fields (title, theme, groups, branded CSS) against it, plus the icon source fingerprint — so it both skips no-op saves (no per-minute churn) and corrects external drift. It manages title/description/theme/customCSS/icon/publicGroupList, sets `published`/`showTags` only on first creation, and preserves operator-set fields (`footerText`, `googleAnalyticsId`, `showPoweredBy`, `domainNameList`, `showCertificateExpiry`). The branded CSS (`build_status_css`) mirrors the admin palette (`STATUS_PALETTE`). Status-page failures are caught and never abort the run.

9. **Maintenance sync (opt-in, `MAINTENANCE_SYNC_ENABLED`).** `app/maintenance.py` mirrors each server's Pelican power schedules into Kuma maintenance windows so scheduled-offline reads as "maintenance", not degraded. `compute_off_windows()` is a pure parser: online = from a `power start` to the next `power stop` (events placed on a weekly minute grid, cron dow `0/7`=Sun); offline windows are the **complement** (joined across the week boundary). It returns `[]` (always-on) or `None` (ambiguous/complex cron → server left unmanaged, not guessed). `reconcile_maintenance()` is idempotent like the status page: tool-owned maintenances are tracked in `MAINTENANCE_STATE_PATH` and created/updated/deleted to match; operator-made maintenances are untouched. Crons are interpreted in, and maintenances created in, `SCHEDULE_TZ` (default UTC — Pelican evaluates crons in the panel TZ, which is UTC by default even if schedules were *labelled* in local time). Schedules are cached (`SCHEDULE_CACHE_TTL_SECONDS`).

### Two-process model & decoupling

The admin UI (`app/admin.py`) holds **no Pelican keys**. It reads the server list from the monitor's Pelican cache (`store.list_pelican_servers()` → `pelican_servers_cache.json`) and reads/writes the webhook mapping (`webhooks.json`). The monitor reads that same `webhooks.json` each run. This file-based handoff in `/data` is the only coupling between the two processes — keep it that way rather than having the admin UI call Pelican/Kuma directly. `store._write_json()` writes atomically (temp file + replace) since both processes touch `/data`.

Admin auth is HTTP basic, enforced only when **both** `ADMIN_USER` and `ADMIN_PASS` are set (otherwise open, with a startup warning). Webhook rows for servers no longer in the cache are kept and flagged "stale" so assignments aren't silently lost.

### Admin-editable settings

`app/settings.py` extends the branding override model to optional *operational* tunables: effective value per field is **admin override → env var → code default** (`settings()`), reading admin-saved values from `/data/settings.json` (`store.load/save_settings`). `SETTINGS_SCHEMA` is the single source — each entry carries `key`/`env`/`type` (`bool`/`int`/`choice`/`str`)/`default`/`group`/`label`/`help`, driving both resolution and the admin form widget. The admin **Settings** card renders fields grouped by `group`; a blank/`Default` selection = inherit env/default (so a bool is a tri-state select: Default / On / Off, where an explicit Off beats an env-On). In scope: Discord/notifications (`discord_confirm_runs`, `discord_use_brand_identity`, `status_page_public_url`), status page (`status_page_enabled`/`title`/`theme`/`ungrouped_label`), and maintenance sync (`maintenance_sync_enabled`, `schedule_tz`). **Deliberately env-only** (not in the UI): all secrets/connection vars (`KUMA_*`, `PEL_*`), file paths, auth (`ADMIN_*`), and the status-page **slug** (identity) — this preserves the no-keys-in-admin decoupling. The monitor resolves settings once at import (fresh process each minute → current per run); `notify.send_discord` and the admin header resolve per call so the long-lived gunicorn process honors live changes.

### Branding

`app/branding.py` is the single source of branding truth. Effective value per field is **admin override → env var → neutral default** (`_resolve()`): admin-saved overrides in `/data/branding.json` (edited via the admin UI, `store.load/save_branding`) win, else `BRAND_*` env vars, else code defaults (`BRAND_NAME=GameMonitor`, not GamerSaloon — that identity is applied via config, never hardcoded). The admin form shows raw overrides as input values and the effective value as the placeholder, so clearing a field falls back to env/default. `avatar_url` defaults to `logo_url`, `webhook_username` defaults to `name`. The **Discord webhook avatar** must be a public absolute URL (Discord fetches it from the internet; `send_discord` skips a non-absolute one). The **status-page icon** is different: `reconcile_status_page` embeds a local `/static/...` icon as a base64 data URI, which Kuma ingests and self-hosts — so the status-page logo renders with no public hosting. (The admin-UI header logo uses the `/static` path directly.) `_icon_source` realpath-confines local icon paths to the `static/` dir (no traversal) and size-caps them. Note `branding.py` imports `store`, not vice-versa — don't introduce the reverse import.

### Defensive-parsing conventions to preserve

The code deliberately tolerates differing API response shapes and Kuma versions; keep this style when editing:
- `extract_state()` probes many possible JSON shapes for `current_state`/`state` before falling back to `"unknown"`.
- `get_last_hb_ms()` and `monitor_has_managed_tag()` check multiple key spellings.
- Cache/state load/save helpers swallow exceptions and degrade gracefully (missing/corrupt cache → refetch).
- Missing node read permission disables wing tagging rather than failing the run.

Uptime Kuma is driven through the `uptime-kuma-api` Socket.IO client (`UptimeKumaApi`), not REST.
