#!/usr/bin/env bash
# Headless RTSP round-trip (no real camera): a local MJPEG-over-RTSP server + the RtspSource.
# Proves the encoded dual-output for a NETWORK source -- stream-copy record + decode for consumers
# -- reusing the same GstPipelineSource machinery as USB.
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .   (now includes gst-rtsp-server)
# Run from the repo root:  ./core-driver/tools/rtsp_test.sh
set -euo pipefail

docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== start fake RTSP server (MJPEG 512x512) ==="
  python3 tools/fake_rtsp_server.py 512 512 25 >/tmp/rtsp.log 2>&1 &
  SRV=$!
  sleep 2; head -1 /tmp/rtsp.log
  echo "=== RtspSource: connect + dual-output (stream-copy record + decode I420 for consumers) ==="
  python3 main.py -c config/rtsp-fake.yaml >/tmp/core.log 2>&1 &
  CORE=$!
  sleep 6
  echo "-- consumers get DECODED I420 via the header endpoint --"
  python3 tools/shm_probe.py --socket /tmp/cam/frames --count 3 --timeout 8 || true
  kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true; kill "$SRV" 2>/dev/null || true
  grep -iE "rtsp source:|recorder: stream-copy|drop summary" /tmp/core.log | head -3
  echo "-- recording is STREAM-COPIED MJPEG (demuxes image/jpeg + decodes; NOT re-encoded) --"
  gst-launch-1.0 filesrc location=/data/recordings/rtspmjpeg-00000.mkv ! matroskademux ! jpegdec ! fakesink -v 2>&1 \
    | grep -oE "image/jpeg|Got EOS" | sort -u | head
'
