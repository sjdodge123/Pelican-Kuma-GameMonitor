#!/usr/bin/env bash
set -euo pipefail

echo "[Pelican-Kuma-GameMonitor] starting"
echo "  KUMA_URL=${KUMA_URL:-}"
echo "  PEL_URL=${PEL_URL:-}"
echo "  KUMA_NAME_PREFIX=${KUMA_NAME_PREFIX:-AUTO}"
echo "  KUMA_STALE_DAYS=${KUMA_STALE_DAYS:-7}"
echo "  KUMA_INTERVAL=${KUMA_INTERVAL:-60}"

exec /usr/local/bin/supercronic /etc/supercronic/crontab