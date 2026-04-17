#!/bin/sh
set -e

# ── Load environment ─────────────────────────────────────────────
# Source /config/.env if mounted, so env vars don't depend on the
# caller passing --env-file to docker run.
if [ -f /config/.env ]; then
    set -a
    . /config/.env
    set +a
fi

# ── Resolve paths ─────────────────────────────────────────────────
CONFIG="${LUCYD_CONFIG:-/data/lucyd.toml}"
CRON_USER="${LUCYD_CRON_USER:-root}"
STATE_DIR="${LUCYD_STATE_DIR:-/data}"

# ── Ensure data dirs exist ────────────────────────────────────────
DATA_DIR="${LUCYD_DATA_DIR:-/data}"
mkdir -p "${DATA_DIR}/sessions" "${DATA_DIR}/downloads" "${DATA_DIR}/logs"

# ── Build cron environment ────────────────────────────────────────
# Cron jobs hit the daemon's HTTP API directly (curl).  Only the
# auth token needs to be forwarded — everything else (DB, provider
# keys) is resolved by the daemon process itself, not the cron job.
CRON_TOKEN="${LUCYD_HTTP_TOKEN:-}"
CRON_PORT="${LUCYD_HTTP_PORT:-8100}"

AUTH_HEADER=""
if [ -n "$CRON_TOKEN" ]; then
    AUTH_HEADER="-H \"Authorization: Bearer ${CRON_TOKEN}\""
fi
API="http://localhost:${CRON_PORT}/api/v1"

cat > /etc/cron.d/lucyd <<CRONTAB
SHELL=/bin/sh
PATH=/usr/local/bin:/usr/bin:/bin

10 * * * * ${CRON_USER} curl -sf -X POST ${AUTH_HEADER} ${API}/index >> ${STATE_DIR}/lucyd-index.log 2>&1
15 * * * * ${CRON_USER} curl -sf -X POST ${AUTH_HEADER} ${API}/consolidate >> ${STATE_DIR}/lucyd-consolidate.log 2>&1
50 3 * * * ${CRON_USER} curl -sf -X POST ${AUTH_HEADER} ${API}/compact >> ${STATE_DIR}/lucyd-compact.log 2>&1
5  4 * * * ${CRON_USER} curl -sf -X POST ${AUTH_HEADER} ${API}/maintain >> ${STATE_DIR}/lucyd-consolidate.log 2>&1
20 4 * * * ${CRON_USER} curl -sf -X POST ${AUTH_HEADER} ${API}/evolve >> ${STATE_DIR}/lucyd-evolve.log 2>&1
CRONTAB
chmod 644 /etc/cron.d/lucyd

# Start cron and atd in background.
# atd failure is non-fatal — deferred tasks (at/batch) won't work but
# the daemon, bridges, and cron jobs all run without it.
cron
atd || echo "WARNING: atd failed to start (exit $?) — 'at' scheduling unavailable" >&2

# ── Parse arguments ──────────────────────────────────────────────
DAEMON_ARGS=""
BRIDGES=""
for arg in "$@"; do
    case "$arg" in
        --with-telegram) BRIDGES="${BRIDGES} telegram" ;;
        --with-email)    BRIDGES="${BRIDGES} email" ;;
        *)               DAEMON_ARGS="${DAEMON_ARGS:+$DAEMON_ARGS }$arg" ;;
    esac
done

# ── Start daemon ─────────────────────────────────────────────────
# Run in background so we can wait for the API before launching
# bridges.  Signal trap ensures SIGTERM still reaches the daemon.
python lucyd.py $DAEMON_ARGS &
DAEMON_PID=$!
trap "kill -TERM $DAEMON_PID" TERM INT

# ── Wait for API ─────────────────────────────────────────────────
# Poll the status endpoint until the daemon is accepting requests.
# 30 attempts × 1s = 30s — well within Docker's healthcheck start
# period.  If it never comes up, bridges just don't launch and the
# container fails the healthcheck on its own.
if [ -n "$BRIDGES" ]; then
    READY=0
    for _ in $(seq 1 30); do
        if curl -sf http://localhost:8100/api/v1/status > /dev/null 2>&1; then
            READY=1
            break
        fi
        sleep 1
    done
    if [ "$READY" = "1" ]; then
        for bridge in $BRIDGES; do
            python -P "channels/${bridge}.py" &
        done
    else
        echo "WARNING: daemon API not ready after 30s — bridges not launched" >&2
    fi
fi

# ── Wait for daemon ──────────────────────────────────────────────
# wait exits with the daemon's exit code; the trap above ensures
# SIGTERM from Docker is forwarded.
wait $DAEMON_PID
