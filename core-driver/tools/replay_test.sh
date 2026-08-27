#!/usr/bin/env bash
# Smoke-test the replay source (camera.type: replay): record a short run with a fake
# camera, replay the run through the full service, and assert the roundtrip is EXACT --
# identical sidecar CSV (same stamps, same frame ids, no drops) and bit-identical
# frames (lossless codecs) / byte-identical bitstream (stream-copy).
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run from the repo root:  ./core-driver/tools/replay_test.sh
set -euo pipefail

echo "########## RAW GRAY8: usb-fake/ffv1 run -> replay -> re-record, exact roundtrip ##########"
docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== 1. record a short GRAY8/FFV1 run ==="
  python3 main.py -c config/usb-fake.yaml >/tmp/rec.log 2>&1 &
  CORE=$!; sleep 4; kill -INT "$CORE"; wait "$CORE"
  ORIG_CSV=$(ls /data/recordings/usbfake-*.csv) || { echo "FAIL: no original CSV"; exit 1; }
  ORIG_MKV=$(ls /data/recordings/usbfake-*-00000.mkv)
  ROWS=$(($(wc -l < "$ORIG_CSV") - 1))
  [ "$ROWS" -gt 0 ] || { echo "FAIL: original run recorded no frames"; exit 1; }
  echo "original rows: $ROWS"

  echo "=== 2. replay the run through the service (finite -> exits 0 on its own) ==="
  timeout 120 python3 main.py -c config/replay-test.yaml >/tmp/replay.log 2>&1 \
    || { echo "FAIL: replay run exited non-zero"; tail -30 /tmp/replay.log; exit 1; }
  grep -q "replay source:" /tmp/replay.log || { echo "FAIL: replay source never started"; exit 1; }
  grep -q "playback finished" /tmp/replay.log || { echo "FAIL: no clean EOF finalize"; exit 1; }

  echo "=== 3. sidecar CSV must be IDENTICAL (same stamps + ids, zero drops) ==="
  RE_CSV=$(ls /data/recordings/rerec-*.csv) || { echo "FAIL: no re-recorded CSV"; exit 1; }
  RE_MKV=$(ls /data/recordings/rerec-*-00000.mkv)
  diff "$ORIG_CSV" "$RE_CSV" || { echo "FAIL: replay CSV differs from the original"; exit 1; }
  echo "CSV identical ($ROWS rows)"

  echo "=== 4. decoded frames must be BIT-IDENTICAL (ffv1 lossless roundtrip) ==="
  gst-launch-1.0 filesrc location="$ORIG_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/a.raw >/dev/null 2>&1
  gst-launch-1.0 filesrc location="$RE_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/b.raw >/dev/null 2>&1
  [ -s /tmp/a.raw ] || { echo "FAIL: original decode produced nothing"; exit 1; }
  cmp /tmp/a.raw /tmp/b.raw || { echo "FAIL: replayed frames are not bit-identical"; exit 1; }
  echo "frames bit-identical ($(stat -c %s /tmp/a.raw) bytes)"
'

echo
echo "########## RAW GRAY16 (thermal shape): 16-bit ffv1 run -> replay roundtrip ##########"
docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  sed "s/GRAY8/GRAY16_LE/; s/name_prefix: usbfake/name_prefix: usb16/" \
    config/usb-fake.yaml > /tmp/usb-fake-gray16.yaml
  python3 main.py -c /tmp/usb-fake-gray16.yaml >/tmp/rec.log 2>&1 &
  CORE=$!; sleep 4; kill -INT "$CORE"; wait "$CORE"
  ORIG_CSV=$(ls /data/recordings/usb16-*.csv) || { echo "FAIL: no original CSV"; exit 1; }
  ORIG_MKV=$(ls /data/recordings/usb16-*-00000.mkv)
  timeout 120 python3 main.py -c config/replay-test.yaml >/tmp/replay.log 2>&1 \
    || { echo "FAIL: replay run exited non-zero"; tail -30 /tmp/replay.log; exit 1; }
  RE_CSV=$(ls /data/recordings/rerec-*.csv); RE_MKV=$(ls /data/recordings/rerec-*-00000.mkv)
  diff "$ORIG_CSV" "$RE_CSV" || { echo "FAIL: replay CSV differs"; exit 1; }
  gst-launch-1.0 filesrc location="$ORIG_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/a.raw >/dev/null 2>&1
  gst-launch-1.0 filesrc location="$RE_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/b.raw >/dev/null 2>&1
  [ -s /tmp/a.raw ] && cmp /tmp/a.raw /tmp/b.raw \
    || { echo "FAIL: 16-bit replay not bit-identical"; exit 1; }
  echo "GRAY16 roundtrip bit-identical"
'

echo
echo "########## STREAM-COPY MJPEG: usb-fake-mjpeg run -> replay -> stream-copy again ##########"
docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== 1. record a short MJPEG (stream-copy) run ==="
  python3 main.py -c config/usb-fake-mjpeg.yaml >/tmp/rec.log 2>&1 &
  CORE=$!; sleep 4; kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true
  ORIG_CSV=$(ls /data/recordings/usbmjpeg-*.csv) || { echo "FAIL: no original CSV"; exit 1; }
  ORIG_MKV=$(ls /data/recordings/usbmjpeg-*-00000.mkv)

  echo "=== 2. replay: must run the stream-copy dual path and re-record the SAME bytes ==="
  timeout 120 python3 main.py -c config/replay-test.yaml >/tmp/replay.log 2>&1 \
    || { echo "FAIL: replay run exited non-zero"; tail -30 /tmp/replay.log; exit 1; }
  grep -q "stream-copy" /tmp/replay.log || { echo "FAIL: replay did not stream-copy"; exit 1; }
  RE_CSV=$(ls /data/recordings/rerec-*.csv); RE_MKV=$(ls /data/recordings/rerec-*-00000.mkv)
  diff "$ORIG_CSV" "$RE_CSV" || { echo "FAIL: replay CSV differs"; exit 1; }

  echo "=== 3. demuxed JPEG bitstream must be byte-identical (no re-encode anywhere) ==="
  gst-launch-1.0 filesrc location="$ORIG_MKV" ! matroskademux ! \
    filesink location=/tmp/a.mjpg >/dev/null 2>&1
  gst-launch-1.0 filesrc location="$RE_MKV" ! matroskademux ! \
    filesink location=/tmp/b.mjpg >/dev/null 2>&1
  [ -s /tmp/a.mjpg ] || { echo "FAIL: original demux produced nothing"; exit 1; }
  cmp /tmp/a.mjpg /tmp/b.mjpg || { echo "FAIL: stream-copy replay bitstream differs"; exit 1; }
  echo "stream-copy bitstream byte-identical ($(stat -c %s /tmp/a.mjpg) bytes)"
'
echo "PASS: replay_test"
