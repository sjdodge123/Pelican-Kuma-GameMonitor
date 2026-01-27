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

## Configuration (environment variables)
See `.env.example`.

### Required
- `KUMA_URL`, `KUMA_USER`, `KUMA_PASS`
- `PEL_URL`, `PEL_APP_KEY`, `PEL_CLIENT_KEY`

## Run (docker)
```bash
cp .env.example .env
# edit .env with <INSERT ...>

docker run -d --name pelican-kuma-gamemonitor \
  --restart unless-stopped \
  --env-file .env \
  -v pelican-kuma-gamemonitor-data:/data \
  ghcr.io/sjdodge123/pelican-kuma-gamemonitor:latest