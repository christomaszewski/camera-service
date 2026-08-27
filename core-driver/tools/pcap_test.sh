#!/usr/bin/env bash
# Smoke-test the pcap source (camera.type: pcap): synthesize a usbmon capture of a fake
# Y16 thermal camera (known ramp frames + noise traffic + one ERR frame + one truncated
# URB), run the full service from it, and assert the FFV1 recording decodes BIT-EXACT to
# the ramp frames with the capture's own timestamps in the sidecar CSV.
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run from the repo root:  ./core-driver/tools/pcap_test.sh
set -euo pipefail

docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== 1. synthesize the capture (100 frames 64x48 Y16 @60fps + noise/ERR/truncation) ==="
  python3 - <<'\''PY'\''
import sys
sys.path.insert(0, "tests")
from uvcpcap_fixture import build_y16_capture
blob, expected = build_y16_capture(frames=100, width=64, height=48, fmt="pcapng",
                                   err_frame=7, truncated_frame=13)
open("/tmp/test.pcap", "wb").write(blob)
with open("/tmp/expected_ts.txt", "w") as f:
    f.writelines(f"{ts}\n" for ts, _ in expected)
with open("/tmp/expected.raw", "wb") as f:
    for _, frame in expected:
        f.write(frame)
print(f"capture: {len(blob)} bytes, {len(expected)} expected frames")
PY

  echo "=== 2. run the service from the capture (finite -> exits 0 on its own) ==="
  timeout 120 python3 main.py -c config/pcap-test.yaml >/tmp/core.log 2>&1 \
    || { echo "FAIL: pcap run exited non-zero"; tail -30 /tmp/core.log; exit 1; }
  grep -q "pcap source:" /tmp/core.log || { echo "FAIL: pcap source never started"; exit 1; }
  grep -q "playback finished" /tmp/core.log || { echo "FAIL: no clean EOF finalize"; exit 1; }

  echo "=== 3. sidecar CSV: every frame, with the CAPTURE timestamps ==="
  CSV=$(ls /data/recordings/pcaptest-*.csv) || { echo "FAIL: no sidecar CSV"; exit 1; }
  tail -n +2 "$CSV" | cut -d, -f3 > /tmp/got_ts.txt
  diff /tmp/expected_ts.txt /tmp/got_ts.txt \
    || { echo "FAIL: recorded timestamps differ from the capture"; exit 1; }
  echo "timestamps exact ($(wc -l < /tmp/got_ts.txt) frames)"

  echo "=== 4. FFV1 recording decodes BIT-EXACT to the capture frames ==="
  M=$(ls /data/recordings/pcaptest-*-00000.mkv) || { echo "FAIL: no recording"; exit 1; }
  gst-launch-1.0 filesrc location="$M" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/got.raw >/dev/null 2>&1
  cmp /tmp/got.raw /tmp/expected.raw \
    || { echo "FAIL: recorded frames are not bit-exact to the capture"; exit 1; }
  echo "frames bit-exact ($(stat -c %s /tmp/got.raw) bytes)"
'

echo
echo "########## MJPEG pcap: dual-output -> stream-copy record, byte-exact + clean drain ##########"
docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  python3 - <<'\''PY'\''
import sys; sys.path.insert(0, "tests")
from uvcpcap_fixture import build_mjpeg_capture
blob, expected = build_mjpeg_capture(frames=40)
open("/tmp/mj.pcap", "wb").write(blob)
open("/tmp/expected.mjpg", "wb").write(b"".join(f for _, f in expected))
print(len(expected), "expected frames")
PY
  cat > /tmp/pcap-mjpeg.yaml <<EOF
camera: {type: pcap}
pcap: {path: /tmp/mj.pcap, pixel_format: MJPEG, width: 32, height: 24, speed: 0}
recording: {enabled: true, encoder: auto, name_prefix: pcapmj}
transport: {plugin_endpoint: {enabled: true, socket_path: /tmp/cam/frames}}
plugins: []
EOF
  timeout 60 python3 main.py -c /tmp/pcap-mjpeg.yaml >/tmp/core.log 2>&1 \
    || { echo "FAIL: exited non-zero"; tail -25 /tmp/core.log; exit 1; }
  grep -q "mini-pipeline drained" /tmp/core.log || { echo "FAIL: no clean EOS drain"; exit 1; }
  CSV=$(ls /data/recordings/pcapmj-*.csv); ROWS=$(($(wc -l < "$CSV") - 1))
  [ "$ROWS" -eq 40 ] || { echo "FAIL: expected 40 recorded frames, got $ROWS (tail truncated?)"; exit 1; }
  M=$(ls /data/recordings/pcapmj-*-00000.mkv)
  gst-launch-1.0 filesrc location="$M" ! matroskademux ! filesink location=/tmp/got.mjpg >/dev/null 2>&1
  cmp /tmp/got.mjpg /tmp/expected.mjpg || { echo "FAIL: stream-copied JPEGs differ from the capture"; exit 1; }
  echo "MJPEG pcap OK: 40/40 frames, stream-copy byte-exact, clean EOS drain"
'
echo "PASS: pcap_test"
