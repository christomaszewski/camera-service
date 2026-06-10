#!/usr/bin/env bash
# Validate camera reconnect/backoff with NO hardware. Run our pipeline against the GVSP fake
# camera, KILL the emitter mid-stream (a simulated link drop), then RESTART it, and check the
# core: (a) detects the disconnect + backs off, (b) reconnects + resumes, (c) never dies,
# (d) finalizes a non-corrupt lossless recording.
#   Prereq:  docker build -f tools/gvsp-chunk-emitter/Dockerfile -t cam-chunks .
#   Run:     ./tools/gvsp-chunk-emitter/reconnect_test.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

docker run --rm -v "$REPO/core-driver:/app" cam-chunks bash -c '
  set -u
  FAILED=0
  fail() { echo "FAIL: $*"; FAILED=1; }
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
  else fail "core died during the outage"; fi

  echo "== RESTART emitter =="
  sleep 1; EMIT=$(emit)
  sleep 10
  if kill -0 "$CORE" 2>/dev/null; then echo "PASS: core alive after the reconnect window"
  else fail "core died around reconnect"; fi

  kill -INT "$CORE" 2>/dev/null || true; wait "$CORE" 2>/dev/null || true
  kill -9 "$EMIT" 2>/dev/null || true

  echo "== evidence (core log) =="
  grep -iE "control channel lost|-> reconnecting|reconnect attempt|reconnected after|resuming capture|rebased" /tmp/core.log | head -12
  STARTS=$(grep -cE "acquisition started" /tmp/core.log || true)
  echo "acquisition starts = $STARTS  (expect >= 2: initial + after reconnect)"
  [ "${STARTS:-0}" -ge 2 ] || fail "no re-acquisition after the outage (starts=$STARTS)"
  grep -q "resuming capture" /tmp/core.log || fail "core never logged a successful reconnect"

  CSV=$(ls /data/recordings/gvspr-*.csv 2>/dev/null | head -1)
  if [ -n "$CSV" ]; then
    ROWS=$(( $(wc -l < "$CSV") - 1 ))
    echo "frames recorded (sidecar rows) = $ROWS"
    [ "$ROWS" -gt 0 ] || fail "sidecar CSV has no frame rows"
  else
    fail "no sidecar CSV written"
  fi

  echo "== recording finalized + decodes losslessly =="
  M=$(ls /data/recordings/gvspr-*-00000.mkv 2>/dev/null | head -1)
  if [ -n "$M" ]; then
    ls -l "$M"
    if gst-launch-1.0 filesrc location="$M" ! matroskademux ! avdec_ffv1 ! fakesink >/tmp/decode.log 2>&1 \
       && grep -qi "got eos" /tmp/decode.log; then
      echo "decode OK (EOS, no error)"
    else
      tail -5 /tmp/decode.log; fail "recording does not decode cleanly"
    fi
  else
    fail "no recording segment written"
  fi

  exit "$FAILED"
'
echo "PASS: reconnect/backoff + finalized recording"
