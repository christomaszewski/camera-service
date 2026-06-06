#!/usr/bin/env bash
# Smoke-test the supervisor in the dev container: it spawns the core (fake camera) +
# a lightweight in-image "probe" plugin, then `docker stop` verifies a clean teardown
# (the core finalizes its recording). No Jetson required.
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run:     ./core-driver/tools/supervisor_test.sh
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
docker rm -f cam_sensor >/dev/null 2>&1

docker run -d --init --name cam_sensor -v "$REPO/core-driver:/app" cam-dev \
  bash -c "mkdir -p /data/recordings /tmp/cam && python3 supervisor.py -c config/supervisor-fake.yaml -v" >/dev/null
sleep 10

echo "== running (supervisor spawns core + probe; probe reads frames) =="
docker logs cam_sensor 2>&1 | grep -iE "spawn|supervising|running|frame_id=" | head -12
echo "== docker stop -> clean teardown =="
docker stop -t 15 cam_sensor
docker logs cam_sensor 2>&1 | grep -iE "signal|stopping|EOS|sensor stopped" | tail -8
docker rm -f cam_sensor >/dev/null 2>&1
