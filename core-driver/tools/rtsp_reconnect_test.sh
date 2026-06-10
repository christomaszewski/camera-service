#!/usr/bin/env bash
# Deterministic RTSP reconnect/recovery test (no real camera). Streams from the local fake RTSP
# server, KILLS it mid-run to induce a stall (the exact failure a real camera causes when it ACKs
# PLAY but streams no media), and checks the data-starvation watchdog detects it and REOPENS the
# stream -- then restarts the server and confirms frames resume into the SAME recording.
#
# Pass: log shows "reports disconnect -> reconnecting" + "reconnected ... resuming capture", and the
# sidecar CSV has a big inter-frame gap (the stall) with frames BOTH before AND after it (recovery).
#
# Run from repo root:  CAM_DEV_IMAGE=cam-dev-jp7 ./core-driver/tools/rtsp_reconnect_test.sh
set -euo pipefail
IMG="${CAM_DEV_IMAGE:-cam-dev-jp7}"
echo "### image=$IMG ###"

docker run --rm -v "$PWD/core-driver:/app" --entrypoint bash "$IMG" -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  serve() { python3 tools/fake_rtsp_server.py 512 512 25 >>/tmp/srv.log 2>&1 & echo $!; }
  echo "=== start fake RTSP server (MJPEG 512x512@25) ==="
  SRV=$(serve); sleep 2; tail -1 /tmp/srv.log
  echo "=== start service (rtsp-fake.yaml; reconnect on, timeout 5s / first-frame grace 12s) ==="
  python3 main.py -c config/rtsp-fake.yaml >/tmp/core.log 2>&1 & CORE=$!
  sleep 8;  echo "[t=8]  KILL fake server -> stream stalls"; kill "$SRV" 2>/dev/null || true; wait "$SRV" 2>/dev/null || true
  sleep 9;  echo "[t=17] RESTART fake server"; SRV2=$(serve)
  sleep 26; echo "[t=43] stop service"; kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true
  kill "$SRV2" 2>/dev/null || true

  echo "=== reconnect timeline (core.log) ==="
  grep -nE "pipeline: running|reports disconnect|reconnecting|reconnect attempt|reconnected|resuming capture|stop requested" /tmp/core.log || true
  grep -q "reports disconnect" /tmp/core.log || { echo "FAIL: watchdog never detected the stall"; exit 1; }
  grep -q "resuming capture" /tmp/core.log || { echo "FAIL: source never reconnected"; exit 1; }
  echo "=== sidecar analysis (stall gap + frames on both sides) ==="
  python3 - <<PY
import csv, glob, sys
paths = glob.glob("/data/recordings/rtspmjpeg-*.csv")
if not paths:
    sys.exit("FAIL: no sidecar CSV written")
rows=list(csv.DictReader(open(paths[0])))
print("total recorded rows:", len(rows))
ts=[int(r["timestamp_ns"]) for r in rows]
if len(ts)>2:
    gaps=[(ts[i+1]-ts[i])/1e9 for i in range(len(ts)-1)]
    mx=max(gaps); idx=gaps.index(mx)
    print("max inter-frame gap: %.1fs  (rows %d before / %d after the gap)" % (mx, idx+1, len(rows)-idx-1))
    ok = mx>5 and (len(rows)-idx-1)>10
    print("VERDICT:", "RECOVERED" if ok else "NOT RECOVERED")
    sys.exit(0 if ok else 1)
print("VERDICT: NOT RECOVERED (too few rows)")
sys.exit(1)
PY
'
echo "PASS: rtsp reconnect/recovery"
