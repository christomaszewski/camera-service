#!/usr/bin/env bash
# Smoke-test the supervisor in the dev container: it spawns the core (fake camera) +
# a lightweight in-image "probe" plugin, then `docker stop` verifies a clean teardown
# (the core finalizes its recording). No Jetson required.
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run:     ./core-driver/tools/supervisor_test.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$(mktemp /tmp/supervisor_test.XXXXXX.log)"

# NB: assertions grep a captured log FILE, not `docker logs | grep -q` -- grep -q's early exit
# would SIGPIPE docker logs and trip pipefail even on a match.
snapshot() { docker logs cam_sensor > "$LOG" 2>&1; }
fail() {
  echo "FAIL: $*" >&2
  tail -20 "$LOG" || true
  docker rm -f cam_sensor >/dev/null 2>&1 || true
  rm -f "$LOG"
  exit 1
}

docker rm -f cam_sensor >/dev/null 2>&1 || true

docker run -d --init --name cam_sensor -v "$REPO/core-driver:/app" cam-dev \
  bash -c "mkdir -p /data/recordings /tmp/cam && python3 supervisor.py -c config/supervisor-fake.yaml -v" >/dev/null
sleep 10

echo "== running (supervisor spawns core + probe; probe reads frames) =="
snapshot
grep -iE "spawn|supervising|running|frame_id=" "$LOG" | head -12 || true
grep -q "supervising 2 service" "$LOG" \
  || fail "supervisor never reached 'supervising 2 service(s)'"
grep -q "frame_id=" "$LOG" \
  || fail "probe plugin read no frames off the transport endpoint"

echo "== docker stop -> clean teardown =="
docker stop -t 15 cam_sensor >/dev/null
snapshot
grep -iE "signal|stopping|EOS|sensor stopped" "$LOG" | tail -8 || true
grep -q "stop requested: stopping acquisition" "$LOG" \
  || fail "core never began a clean stop (recording would not be finalized)"
grep -q "sensor stopped" "$LOG" \
  || fail "supervisor teardown did not complete (no 'sensor stopped')"

docker rm -f cam_sensor >/dev/null 2>&1 || true
rm -f "$LOG"
echo "PASS: supervisor spawn / manage / clean teardown"
