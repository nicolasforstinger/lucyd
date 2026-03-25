#!/bin/sh
set -e

# ── Resolve paths ─────────────────────────────────────────────────
CONFIG="${LUCYD_CONFIG:-/data/lucyd.toml}"
CRON_USER="${LUCYD_CRON_USER:-root}"
STATE_DIR="${LUCYD_STATE_DIR:-/data}"

# ── Ensure data dirs exist ────────────────────────────────────────
DATA_DIR="${LUCYD_DATA_DIR:-/data}"
mkdir -p "${DATA_DIR}/sessions" "${DATA_DIR}/downloads" "${DATA_DIR}/logs"

# ── Build cron environment ────────────────────────────────────────
# Cron does NOT inherit the container's environment.  Forward all
# LUCYD_* vars plus API keys so cron jobs (lucyd-index, lucydctl,
# lucyd-consolidate) can resolve paths and authenticate.
CRON_ENV="SHELL=/bin/sh
PATH=/usr/local/bin:/usr/bin:/bin
LUCYD_DATA_DIR=${DATA_DIR}
LUCYD_STATE_DIR=${STATE_DIR}
LUCYD_CONFIG=${CONFIG}"

# Forward API key env vars (set by compose env_file) into cron
for var in $(env | grep -E '^(LUCYD_|OPENAI_|ANTHROPIC_|BRAVE_)' | cut -d= -f1); do
    # Skip vars already written above
    case "$var" in LUCYD_STATE_DIR|LUCYD_CONFIG) continue ;; esac
    eval "val=\$$var"
    CRON_ENV="${CRON_ENV}
${var}=${val}"
done

cat > /etc/cron.d/lucyd <<CRONTAB
${CRON_ENV}

10 * * * * ${CRON_USER} python /app/bin/lucyd-index >> ${STATE_DIR}/lucyd-index.log 2>&1
15 * * * * ${CRON_USER} python /app/bin/lucyd-consolidate -c ${CONFIG} >> ${STATE_DIR}/lucyd-consolidate.log 2>&1
50 3 * * * ${CRON_USER} python /app/bin/lucydctl --compact >> ${STATE_DIR}/lucyd-compact.log 2>&1
5  4 * * * ${CRON_USER} python /app/bin/lucyd-consolidate -c ${CONFIG} --maintain >> ${STATE_DIR}/lucyd-consolidate.log 2>&1
20 4 * * * ${CRON_USER} python /app/bin/lucydctl --evolve >> ${STATE_DIR}/lucyd-evolve.log 2>&1
CRONTAB
chmod 644 /etc/cron.d/lucyd

# Start cron and atd in background
cron
atd

# Exec into the daemon — PID 1, receives SIGTERM
exec python lucyd.py "$@"
