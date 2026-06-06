#!/usr/bin/env bash
# Headless RTSP round-trip (no real camera): a local MJPEG-over-RTSP server + the RtspSource.
# Proves the encoded dual-output for a NETWORK source -- stream-copy record + decode for consumers
# -- AND the RTCP->NTP timestamp provenance (per-frame, via the sidecar CSV: transport-agnostic).
#
# Image selects the userspace generation (the NTP meta is gst>=1.24 / JP7):
#   CAM_DEV_IMAGE=cam-dev      (default) gst 1.20 / JP6  -> arrival timestamps  (source=system)
#   CAM_DEV_IMAGE=cam-dev-jp7            gst 1.24 / JP7  -> camera wall-clock    (source=rtp_ntp)
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
#          docker build -f core-driver/Dockerfile.dev --build-arg BASE=ubuntu:24.04 -t cam-dev-jp7 .
# Run from the repo root:  ./core-driver/tools/rtsp_test.sh        (or CAM_DEV_IMAGE=cam-dev-jp7 ...)
set -euo pipefail
IMG="${CAM_DEV_IMAGE:-cam-dev}"
echo "### image=$IMG ###"

docker run --rm -v "$PWD/core-driver:/app" "$IMG" bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== gst $(gst-inspect-1.0 --version | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1); start fake RTSP server (MJPEG 512x512) ==="
  python3 tools/fake_rtsp_server.py 512 512 25 >/tmp/rtsp.log 2>&1 &
  SRV=$!; sleep 2; head -1 /tmp/rtsp.log
  echo "=== RtspSource: dual-output (stream-copy record + decode for consumers) + RTCP->NTP provenance ==="
  python3 main.py -c config/rtsp-fake.yaml >/tmp/core.log 2>&1 &
  CORE=$!; sleep 9
  kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true; kill "$SRV" 2>/dev/null || true
  grep -iE "rtsp decode branch|rtsp: RTCP|rtsp: no add-reference|transport endpoint|recorder: stream-copy" /tmp/core.log | head -5
  echo "-- per-frame timestamp provenance (sidecar CSV; works on either transport) --"
  python3 - <<PY
import csv, collections
rows=list(csv.DictReader(open("/data/recordings/rtspmjpeg.csv")))
dist=collections.Counter(r["source"] for r in rows)
print("   frames=%d  provenance=%s" % (len(rows), dict(dist)))
ntp=[int(r["timestamp_ns"]) for r in rows if r["source"]=="rtp_ntp"]
if ntp:
    d=[(ntp[i+1]-ntp[i])/1e6 for i in range(len(ntp)-1)]
    unix_ok = 1.6e18 < ntp[0] < 2.0e18   # plausible Unix-epoch ns (~2020..2033) => NTP->Unix conv correct
    print("   rtp_ntp: %d frames, inter-frame ms min/max=%.1f/%.1f (=frame interval), unix-epoch-ok=%s"
          % (len(ntp), min(d), max(d), unix_ok))
    print("   => NTP wall-clock provenance WORKING")
else:
    print("   => arrival-only (system): expected on gst<1.24 (no add-reference-timestamp-meta)")
PY
  echo "-- recording is STREAM-COPIED MJPEG (demuxes image/jpeg + decodes; NOT re-encoded) --"
  gst-launch-1.0 filesrc location=/data/recordings/rtspmjpeg-00000.mkv ! matroskademux ! jpegdec ! fakesink -v 2>&1 \
    | grep -oE "image/jpeg|Got EOS" | sort -u | head
'
