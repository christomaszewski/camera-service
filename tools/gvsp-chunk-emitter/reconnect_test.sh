#!/usr/bin/env bash
# Validate camera reconnect/backoff with NO hardware. Run our pipeline against the GVSP fake
# camera, KILL the emitter mid-stream (a simulated link drop), then RESTART it, and check the
# core: (a) detects the disconnect + backs off, (b) reconnects + resumes, (c) never dies,
# (d) finalizes a non-corrupt lossless recording.
#   Prereq:  docker build -f tools/gvsp-chunk-emitter/Dockerfile -t gige-chunks .
#   Run:     ./tools/gvsp-chunk-emitter/reconnect_test.sh
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

docker run --rm -v "$REPO/core-driver:/app" gige-chunks bash -c '
  set -u
  mkdir -p /data/recordings
  emit() { arv-fake-gv-camera-0.8 -i 127.0.0.1 -s GV01 >>/tmp/emit.log 2>&1 & echo $!; }

  EMIT=$(emit); sleep 3
  python3 main.py -c config/gvsp-reconnect.yaml -v >/tmp/core.log 2>&1 &
  CORE=$!
  sleep 6

  echo "== KILL emitter (simulated disconnect) =="
  kill -9 "$EMIT" 2>/dev/null || true
  sleep 7
  if kill -0 "$CORE" 2>/dev/null; then echo "PASS: core stayed alive through the outage"
  else echo "FAIL: core died during the outage"; fi

  echo "== RESTART emitter =="
  sleep 1; EMIT=$(emit)
  sleep 10
  if kill -0 "$CORE" 2>/dev/null; then echo "PASS: core alive after the reconnect window"
  else echo "FAIL: core died around reconnect"; fi

  kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true
  kill -9 "$EMIT" 2>/dev/null || true

  echo "== evidence (core log) =="
  grep -iE "control channel lost|-> reconnecting|reconnect attempt|reconnected after|resuming capture|rebased" /tmp/core.log | head -12
  STARTS=$(grep -cE "acquisition started" /tmp/core.log)
  echo "acquisition starts = $STARTS  (expect >= 2: initial + after reconnect)"
  echo "frames recorded (sidecar rows) = $(( $(wc -l < /data/recordings/gvspr.csv) - 1 ))"

  echo "== recording finalized + decodes losslessly =="
  M=$(ls /data/recordings/gvspr-*.mkv 2>/dev/null | head -1)
  ls -l "$M" 2>/dev/null
  gst-launch-1.0 filesrc location="$M" ! matroskademux ! avdec_ffv1 ! fakesink 2>&1 | grep -iE "error|got eos" | head -1
'
