#!/usr/bin/env bash
set -euo pipefail

BRAND_NAME="${BRAND_NAME:-GameMonitor}"
ADMIN_PORT="${ADMIN_PORT:-8080}"

echo "[${BRAND_NAME}-GameMonitor] starting"
echo "  KUMA_URL=${KUMA_URL:-}"
echo "  PEL_URL=${PEL_URL:-}"
echo "  KUMA_NAME_PREFIX=${KUMA_NAME_PREFIX:-AUTO}"
echo "  KUMA_STALE_DAYS=${KUMA_STALE_DAYS:-7}"
echo "  KUMA_INTERVAL=${KUMA_INTERVAL:-60}"
echo "  STATUS_PAGE_SLUG=${STATUS_PAGE_SLUG:-gamersaloon}"
echo "  ADMIN_PORT=${ADMIN_PORT}"
if [ -z "${ADMIN_PASS:-}" ]; then
  echo "  WARNING: admin UI auth is DISABLED (set ADMIN_PASS to enable; ADMIN_USER defaults to 'admin')"
fi

# Run both the admin web UI and the monitor cron. If EITHER exits, tear down the
# other and exit non-zero so the container's restart policy recovers it (rather
# than silently running degraded — e.g. admin dead but cron still up).
gunicorn --chdir /app/app -b "0.0.0.0:${ADMIN_PORT}" --workers 2 --access-logfile - admin:app &
GUNICORN_PID=$!

/usr/local/bin/supercronic /etc/supercronic/crontab &
CRON_PID=$!

# Forward shutdown signals (docker stop) to both children so they exit gracefully
# instead of being SIGKILLed after the grace period (which can truncate writes).
shutdown() {
  echo "[${BRAND_NAME}-GameMonitor] signal received; stopping children"
  kill -TERM "${GUNICORN_PID}" "${CRON_PID}" 2>/dev/null || true
}
trap shutdown TERM INT

# `|| true` so `set -e` doesn't abort before the cleanup below when a child exits
# non-zero. `wait -n` returns when the first child exits.
wait -n || true
echo "[${BRAND_NAME}-GameMonitor] a child process exited; shutting down for restart"
kill -TERM "${GUNICORN_PID}" "${CRON_PID}" 2>/dev/null || true
wait || true
exit 1
