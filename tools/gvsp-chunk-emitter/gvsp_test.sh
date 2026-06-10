#!/usr/bin/env bash
# Validate the REAL Aravis chunk-parse path with no hardware: run the patched
# chunk-emitting GV fake camera + our pipeline over GVSP (loopback), and check
# ChunkTimestamp extraction + lossless recording.
#
# Prereq:  docker build -f tools/gvsp-chunk-emitter/Dockerfile -t cam-chunks .
# Run:     ./tools/gvsp-chunk-emitter/gvsp_test.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

docker run --rm -v "$REPO/core-driver:/app" -v "$REPO/tools/gvsp-chunk-emitter:/t" cam-chunks bash -c '
  set -eo pipefail
  echo "== unit tests =="
  python3 tests/test_timestamps.py | tail -1
  python3 tests/test_transport.py  | tail -1
  python3 tests/test_config.py     | tail -1

  echo "== direct chunk read over GVSP (chunk_check) =="
  arv-fake-gv-camera-0.8 -i 127.0.0.1 -s GV01 >/tmp/emit.log 2>&1 &
  sleep 3
  python3 /t/chunk_check.py | tail -3

  echo "== full pipeline over GVSP =="
  mkdir -p /data/recordings
  python3 main.py -c config/gvsp-test.yaml >/tmp/core.log 2>&1 &
  CORE=$!
  sleep 6
  kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true
  grep -iE "chunk mode enabled|Active timestamp" /tmp/core.log | head -2 || true
  grep -qi "chunk mode enabled" /tmp/core.log || { echo "FAIL: chunk mode never engaged"; exit 1; }
  echo "-- sidecar CSV (frame_id, ..., chunk_ns, camera_ns, system_ns) --"
  CSV=$(ls /data/recordings/gvsp-*.csv) || { echo "FAIL: no sidecar CSV"; exit 1; }
  head -3 "$CSV"
  [ "$(wc -l < "$CSV")" -gt 1 ] || { echo "FAIL: sidecar CSV has no frame rows"; exit 1; }
  echo "-- ptp provenance --"; grep -E "timestamp_source|ptp_synced" /data/recordings/gvsp-*.json
  echo "-- lossless mkv decodes --"
  M=$(ls /data/recordings/gvsp-*-00000.mkv) || { echo "FAIL: no recording segment"; exit 1; }
  gst-launch-1.0 filesrc location="$M" ! matroskademux ! avdec_ffv1 ! fakesink >/tmp/decode.log 2>&1 \
    || { echo "FAIL: mkv decode errored"; tail -5 /tmp/decode.log; exit 1; }
  grep -qi "got eos" /tmp/decode.log || { echo "FAIL: decode never reached EOS"; exit 1; }
  echo "decode OK (EOS, no error)"
'
echo "PASS: gvsp chunk extraction + recording"
