#!/usr/bin/env bash
# Deterministic USB hotplug/hot-swap test on the Orin. Simulates UNPLUG/REPLUG of a real UVC camera
# via sysfs unbind/bind (no physical access) and checks the data-starvation watchdog detects the loss
# and REOPENS the device when it returns -- recording into the SAME file across the gap.
#
# Runs the core NATIVELY on the host (NOT a container): Docker's --device mapping doesn't track a host
# device disappearing/reappearing, but a native process sees /dev/v4l/by-id/... vanish + come back live.
# Needs: host gstreamer+python deps (JP7 has them) and passwordless sudo for unbind/bind.
#
# Pass: log shows "liveness: stream stalled ... -> reopening" + "v4l2 device ... not present" (backoff)
# + "source reconnected ... resuming capture", and the sidecar CSV has frames BOTH sides of the gap.
#
# Override BYID/USBDEV for a different camera (find USBDEV via: basename $(readlink -f \
#   /sys/class/video4linux/video0/device) | cut -d: -f1).
set -u
REPO="${REPO:-/home/uxv/ws/gige-vision-service/core-driver}"
BYID="${BYID:-/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._NexiGo_HD_Webcam_SN0001-video-index0}"
USBDEV="${USBDEV:-1-4.1}"
FMT="${FMT:-MJPEG}"; W="${W:-1280}"; H="${H:-720}"; FPS="${FPS:-30}"
WORK=/tmp/usbhotplug; REC="$WORK/rec"
mkdir -p "$REC" /tmp/cam

# safety: always leave the camera BOUND, even if we bail early
trap 'echo "$USBDEV" | sudo tee /sys/bus/usb/drivers/usb/bind >/dev/null 2>&1 || true' EXIT

cat > "$WORK/cam.yaml" <<YAML
camera:
  type: usb
  frame_rate: $FPS
  reconnect: true
  reconnect_timeout_s: 5
usb:
  device: $BYID
  pixel_format: $FMT
  width: $W
  height: $H
recording:
  enabled: true
  encoder: auto
  output_dir: $REC
  name_prefix: usbrec
  segment_seconds: 60
preview:
  enabled: false
transport:
  plugin_endpoint:
    enabled: true
    socket_path: /tmp/cam/frames
plugins: []
YAML

rm -f "$REC"/*
cd "$REPO"
python3 main.py -c "$WORK/cam.yaml" >"$WORK/core.log" 2>&1 &
CORE=$!
for _ in $(seq 1 30); do grep -q "pipeline: running" "$WORK/core.log" && break; sleep 1; done
sleep 6
echo "[t+6]  UNPLUG  (usb unbind $USBDEV)"; echo "$USBDEV" | sudo tee /sys/bus/usb/drivers/usb/unbind >/dev/null
sleep 10
echo "[t+16] REPLUG  (usb bind $USBDEV)";   echo "$USBDEV" | sudo tee /sys/bus/usb/drivers/usb/bind   >/dev/null
sleep 22
echo "[t+38] stop";   kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true

echo "=== timeline (core.log) ==="
grep -nE "pipeline: running|usb source|liveness:|reconnect attempt|not present|reconnected|resuming capture|stop requested" "$WORK/core.log" || true
echo "=== sidecar: frames either side of the unplug gap ==="
python3 - "$REC" <<'PY'
import csv, glob, sys
f = sorted(glob.glob(sys.argv[1] + "/*.csv"))
if not f:
    print("NO CSV (no frames recorded at all)"); raise SystemExit
rows = list(csv.DictReader(open(f[0])))
print("recorded rows:", len(rows))
ts = [int(r["timestamp_ns"]) for r in rows]
if len(ts) > 2:
    gaps = [(ts[i+1]-ts[i])/1e9 for i in range(len(ts)-1)]
    mx = max(gaps); idx = gaps.index(mx)
    print("max inter-frame gap: %.1fs  (rows %d before / %d after the gap)" % (mx, idx+1, len(rows)-idx-1))
    print("VERDICT:", "RECOVERED" if (mx > 3 and len(rows)-idx-1 > 10) else "NOT RECOVERED")
else:
    print("VERDICT: NOT RECOVERED (too few rows)")
PY
