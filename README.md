# Pelican-Kuma-GameMonitor

A small container that uses Pelican/Pterodactyl Panel APIs + Uptime Kuma to automatically:
- create Push monitors named: `AUTO <Pelican Server Name>`
- push UP/DOWN based on the panel's server state
- delete stale AUTO monitors if they have had no heartbeat for > N days

## How it works
- Lists servers via Pelican **Application API** (`PEL_APP_KEY`)
- Reads each server's current state via Pelican **Client API** (`PEL_CLIENT_KEY`)
- Creates monitors in Uptime Kuma using the internal Socket.IO API (requires `KUMA_USER` + `KUMA_PASS`)
- Pushes heartbeat to each monitor

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

Optional naming (OFF by default)
- `KUMA_NAME_PREFIX=` (leave empty to use *only* the Pelican server name)

docker run -d --name pelican-kuma-gamemonitor \
  --restart unless-stopped \
  -e KUMA_URL="http://<INSERT IP HERE>:<INSERT PORT HERE>" \
  -e KUMA_USER="<INSERT USER>" \
  -e KUMA_PASS="<INSERT PASS>" \
  -e PEL_URL="http://<INSERT IP HERE>:<INSERT PORT HERE>" \
  -e PEL_APP_KEY="<INSERT APP KEY HERE>" \
  -e PEL_CLIENT_KEY="<INSERT CLIENT KEY HERE>" \
  -v pelican-kuma-gamemonitor-data:/data \
  ghcr.io/sjdodge123/pelican-kuma-gamemonitor:latest