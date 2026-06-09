#!/usr/bin/env bash
# Smoke-test the producer pipeline in the dev container (no Jetson HW required).
#
# Exercises: unit tests -> fake-camera capture -> timestamps -> CSV/JSON sidecar ->
# encoder availability fallback (auto -> hw-hevc-lossless, but no NVENC in this image -> ffv1,
# warned) -> software FFV1 recording -> shm transport (header round-trip via shm_probe) ->
# clean EOS shutdown -> mkv decode check.
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run from the repo root:  ./core-driver/tools/dev_test.sh
set -euo pipefail

docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== unit tests ==="
  python3 tests/test_transport.py
  python3 tests/test_config.py
  python3 tests/test_timestamps.py
  python3 tests/test_dropstats.py
  python3 tests/test_formats.py
  python3 tests/test_recorder.py

  echo "=== fake-camera producer + shm probe ==="
  python3 main.py -c config/fake-camera.yaml >/tmp/core.log 2>&1 &
  CORE=$!
  sleep 5
  python3 tools/shm_probe.py --socket /tmp/cam/frames --count 5 --timeout 5
  kill -INT "$CORE"; wait "$CORE"; echo "core exit: $?"

  echo "=== encoder fallback (auto -> hw-hevc-lossless, no NVENC here -> ffv1) ==="
  grep -q "falling back to ffv1" /tmp/core.log \
    || { echo "FAIL: expected hw-hevc-lossless -> ffv1 fallback"; tail -20 /tmp/core.log; exit 1; }
  grep -q "recorder: encoder=ffv1" /tmp/core.log \
    || { echo "FAIL: recorder did not run ffv1"; tail -20 /tmp/core.log; exit 1; }
  echo "fallback engaged (warned + recorded ffv1)"

  echo "=== outputs ==="
  ls -la /data/recordings/
  CSV=$(ls /data/recordings/fake-*.csv) || { echo "FAIL: no sidecar CSV"; exit 1; }
  ROWS=$(($(wc -l < "$CSV") - 1))
  [ "$ROWS" -gt 0 ] || { echo "FAIL: sidecar CSV has no frame rows"; exit 1; }
  echo "sidecar rows: $ROWS"
  echo "=== decode FFV1 mkv (must reach EOS, no error) ==="
  M=$(ls /data/recordings/fake-*-00000.mkv) || { echo "FAIL: no recording segment"; exit 1; }
  gst-launch-1.0 filesrc location="$M" ! matroskademux ! avdec_ffv1 ! fakesink >/tmp/decode.log 2>&1 \
    || { echo "FAIL: mkv decode errored"; tail -5 /tmp/decode.log; exit 1; }
  grep -qi "got eos" /tmp/decode.log || { echo "FAIL: decode never reached EOS"; exit 1; }
  echo "decode OK (EOS, no error)"
'
echo "PASS: dev_test"
