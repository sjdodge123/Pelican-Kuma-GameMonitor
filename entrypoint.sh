#!/usr/bin/env bash
set -euo pipefail

echo "[pelican-kuma-autodiscover] starting"
echo "  KUMA_URL=${KUMA_URL:-}"
echo "  PEL_URL=${PEL_URL:-}"
echo "  NAME_PREFIX=${KUMA_NAME_PREFIX:-AUTO}"
echo "  STALE_DAYS=${KUMA_STALE_DAYS:-7}"

exec /usr/local/bin/supercronic /etc/supercronic/crontab