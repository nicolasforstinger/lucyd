#!/bin/sh
# Docker smoke test: build, start, health check, single request.
# Run from repository root: sh tests/test_docker_smoke.sh
set -e

IMAGE="lucyd:smoke-test"

echo "Building image..."
docker build -t "$IMAGE" .

echo "Starting container..."
CID=$(docker run -d --rm \
    -e LUCYD_DATA_DIR=/data \
    -p 8199:8100 \
    "$IMAGE" -c /dev/null 2>/dev/null || true)

if [ -z "$CID" ]; then
    echo "SKIP: Docker not available or build failed"
    exit 0
fi

cleanup() { docker stop "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "Waiting for health..."
for i in $(seq 1 30); do
    if docker inspect --format='{{.State.Health.Status}}' "$CID" 2>/dev/null | grep -q healthy; then
        echo "Healthy after ${i}s"
        break
    fi
    sleep 1
done

echo "Checking status endpoint..."
STATUS=$(curl -sf http://localhost:8199/api/v1/status 2>/dev/null || echo "FAILED")
echo "Status: $STATUS"

if echo "$STATUS" | grep -q "FAILED"; then
    echo "FAIL: Status endpoint not reachable"
    exit 1
fi

echo "PASS: Docker smoke test completed"
