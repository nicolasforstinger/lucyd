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
# Cron does NOT inherit the container's environment.  Forward the
# subset of vars that cron jobs need: paths, DB URL, provider keys.
CRON_ENV="SHELL=/bin/sh
PATH=/usr/local/bin:/usr/bin:/bin
LUCYD_DATA_DIR=${DATA_DIR}
LUCYD_STATE_DIR=${STATE_DIR}
LUCYD_CONFIG=${CONFIG}"

# Forward env vars that cron jobs actually need.  Cron runs lucydctl
# (index, consolidate, compact, maintain, evolve) which needs DB access
# and LLM provider keys.  Channel tokens, HTTP auth, plugin keys, and
# search API keys are runtime-only — keep them out of the cron file.
CRON_EXCLUDE="LUCYD_TELEGRAM_TOKEN|LUCYD_EMAIL_PASSWORD|LUCYD_HTTP_TOKEN|LUCYD_ELEVENLABS_KEY|LUCYD_BRAVE_KEY|BRAVE_API_KEY"

for var in $(env | grep -E '^(LUCYD_|OPENAI_|ANTHROPIC_)' | grep -vE "^(${CRON_EXCLUDE})=" | cut -d= -f1); do
    # Skip vars already written above
    case "$var" in LUCYD_DATA_DIR|LUCYD_STATE_DIR|LUCYD_CONFIG) continue ;; esac
    eval "val=\$$var"
    CRON_ENV="${CRON_ENV}
${var}=${val}"
done

cat > /etc/cron.d/lucyd <<CRONTAB
${CRON_ENV}

10 * * * * ${CRON_USER} python /app/bin/lucydctl --index >> ${STATE_DIR}/lucyd-index.log 2>&1
15 * * * * ${CRON_USER} python /app/bin/lucydctl --consolidate >> ${STATE_DIR}/lucyd-consolidate.log 2>&1
50 3 * * * ${CRON_USER} python /app/bin/lucydctl --compact >> ${STATE_DIR}/lucyd-compact.log 2>&1
5  4 * * * ${CRON_USER} python /app/bin/lucydctl --maintain >> ${STATE_DIR}/lucyd-consolidate.log 2>&1
20 4 * * * ${CRON_USER} python /app/bin/lucydctl --evolve >> ${STATE_DIR}/lucyd-evolve.log 2>&1
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
