#!/usr/bin/env bash
# Smoke-test the producer pipeline in the dev container (no Jetson HW required).
#
# Exercises: unit tests -> fake-camera capture -> timestamps -> CSV/JSON sidecar ->
# software FFV1 recording -> shm transport (header round-trip via shm_probe) ->
# clean EOS shutdown -> mkv decode check.
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t gige-dev .
# Run from the repo root:  ./core-driver/tools/dev_test.sh
set -euo pipefail

docker run --rm -v "$PWD/core-driver:/app" gige-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/gige
  echo "=== unit tests ==="
  python3 tests/test_transport.py
  python3 tests/test_config.py
  python3 tests/test_timestamps.py

  echo "=== fake-camera producer + shm probe ==="
  python3 main.py -c config/fake-camera.yaml >/tmp/core.log 2>&1 &
  CORE=$!
  sleep 5
  python3 tools/shm_probe.py --socket /tmp/gige/frames --count 5 --timeout 5
  kill -INT "$CORE"; wait "$CORE"; echo "core exit: $?"

  echo "=== outputs ==="
  ls -la /data/recordings/
  echo "=== decode FFV1 mkv (must reach EOS, no error) ==="
  gst-launch-1.0 filesrc location=/data/recordings/fake-00000.mkv ! matroskademux ! avdec_ffv1 ! fakesink 2>&1 \
    | grep -iE "error|got eos" | head -4
'
