#!/usr/bin/env bash
# Smoke-test the USB source (fake videotestsrc) through the shared pipeline -- no /dev/video,
# no Jetson. Proves a SECOND source type works end to end over the step-2 seam:
#   usb Source -> appsink feeder -> on_frame -> appsrc -> tee -> FFV1 + shm (CAMF header).
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run from the repo root:  ./core-driver/tools/usb_test.sh
set -euo pipefail

docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== fake USB (videotestsrc) producer + shm probe ==="
  python3 main.py -c config/usb-fake.yaml >/tmp/core.log 2>&1 &
  CORE=$!
  sleep 5
  python3 tools/shm_probe.py --socket /tmp/cam/frames --count 5 --timeout 5
  kill -INT "$CORE"; wait "$CORE"; echo "core exit: $?"

  echo "=== outputs ==="
  ls -la /data/recordings/
  echo "=== decode FFV1 mkv (must reach EOS, no error) ==="
  gst-launch-1.0 filesrc location=/data/recordings/usbfake-00000.mkv ! matroskademux ! avdec_ffv1 ! fakesink 2>&1 \
    | grep -iE "error|got eos" | head -4
'

echo
echo "########## COLOR: fake USB I420 -> ffv1 (lossless, no chroma resample) ##########"
docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== fake USB COLOR (videotestsrc I420) producer (auto encoder should pick ffv1) ==="
  timeout -s INT 6 python3 main.py -c config/usb-fake-color.yaml 2>&1 | grep -iE "recorder: encoder|drop summary" | head -3 || true
  echo "=== recording stores I420 natively (NOT NV24/GRAY8 => no resample) + decodes ==="
  gst-launch-1.0 filesrc location=/data/recordings/usbcolor-00000.mkv ! matroskademux ! avdec_ffv1 ! fakesink -v 2>&1 \
    | grep -oE "format=\(string\)[A-Za-z0-9_]+|Got EOS" | sort -u | head
'
