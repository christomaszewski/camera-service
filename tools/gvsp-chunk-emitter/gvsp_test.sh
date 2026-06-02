#!/usr/bin/env bash
# Validate the REAL Aravis chunk-parse path with no hardware: run the patched
# chunk-emitting GV fake camera + our pipeline over GVSP (loopback), and check
# ChunkTimestamp extraction + lossless recording.
#
# Prereq:  docker build -f tools/gvsp-chunk-emitter/Dockerfile -t gige-chunks .
# Run:     ./tools/gvsp-chunk-emitter/gvsp_test.sh
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

docker run --rm -v "$REPO/core-driver:/app" -v "$REPO/tools/gvsp-chunk-emitter:/t" gige-chunks bash -c '
  set -e
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
  grep -iE "chunk mode enabled|Active timestamp" /tmp/core.log | head -2
  echo "-- sidecar CSV (frame_id, ..., chunk_ns, camera_ns, system_ns) --"
  head -3 /data/recordings/gvsp.csv
  echo "-- ptp provenance --"; grep -E "timestamp_source|ptp_synced" /data/recordings/gvsp.json
  echo "-- lossless mkv decodes --"
  gst-launch-1.0 filesrc location=$(ls /data/recordings/gvsp-*.mkv | head -1) ! \
    matroskademux ! avdec_ffv1 ! fakesink 2>&1 | grep -iE "error|got eos" | head -1
'
