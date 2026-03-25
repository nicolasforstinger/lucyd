FROM python:3.13-slim

# System deps for agent operations:
#   cron — scheduled tasks (consolidation, evolution, indexing)
#   at — agent-scheduled reminders/deferred tasks
#   curl, jq — HTTP requests and JSON processing from shell
#   git — workspace version control
#   file — attachment type detection
#   openssh-client — SSH/SCP for remote system management
#   procps — ps/top/free for resource awareness
# Uncomment ffmpeg line for local STT (whisper.cpp).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       cron at curl jq git file openssh-client procps \
    && rm -rf /var/lib/apt/lists/*
# RUN apt-get install -y --no-install-recommends ffmpeg

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy framework code
COPY *.py ./
COPY channels/ channels/
COPY providers/ providers/
COPY tools/ tools/
COPY bin/ bin/
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH="/app/bin:${PATH}"

EXPOSE 8100

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/api/v1/status')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["-c", "/config/lucyd.toml"]
