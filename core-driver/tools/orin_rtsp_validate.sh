#!/usr/bin/env bash
# Orin RTSP validation. Runs the real 4K H.265 camera through cam-core:jp7 (HW NVDEC via CDI),
# timing the stream window from when the pipeline reaches "running" (so a slow startup/probe does
# NOT eat the window), and reporting recording completeness + connect->first-frame latency.
#
# Two configs, to isolate the self-configuring gst-discoverer probe:
#   CFG=config/rtsp-real.yaml          probe ON  (heavy 2nd HW-decode session at open())
#   CFG=config/rtsp-real-noprobe.yaml  probe OFF (codec/geometry pinned; no 2nd connection)
#
# Pass = every run: csv rows > 0 AND mkv bytes > 0, no "frame-id gap" warnings, first frame < ~8s.
set -uo pipefail
REPO="${REPO:-/home/uxv/ws/gige-vision-service}"
IMG=cam-core:jp7
CFG="${CFG:-config/rtsp-real.yaml}"
RUNS="${RUNS:-4}"
STREAM="${STREAM:-25}"     # seconds of streaming AFTER "running"
REC=/tmp/camval/rec
LOGS=/tmp/camval
mkdir -p "$LOGS"

echo "### image=$IMG cfg=$CFG runs=$RUNS stream=${STREAM}s (timed from 'running')"
for i in $(seq 1 "$RUNS"); do
  rm -rf "$REC"; mkdir -p "$REC"
  LOG="$LOGS/v2_$(basename "$CFG" .yaml)_$i.log"
  cid=$(docker run -d --network host --device nvidia.com/gpu=all \
        -v "$REPO/core-driver:/app" -v "$REC:/data/recordings" \
        "$IMG" main.py -c "$CFG")
  # wait (<=35s) for the pipeline to reach "running"
  ran=0
  for _ in $(seq 1 35); do
    docker logs "$cid" 2>&1 | grep -q "pipeline: running" && { ran=1; break; }
    sleep 1
  done
  [ "$ran" = 1 ] && sleep "$STREAM"        # stream window starts only once running
  docker kill --signal=INT "$cid" >/dev/null 2>&1 || true
  timeout 15 docker wait "$cid" >/dev/null 2>&1 || true
  docker logs "$cid" >"$LOG" 2>&1 || true
  docker rm -f "$cid" >/dev/null 2>&1 || true

  shopt -s nullglob
  vids=("$REC"/*.mkv "$REC"/*.mp4 "$REC"/*.ts); csvs=("$REC"/*.csv)
  nseg=${#vids[@]}; bytes=0; for f in "${vids[@]}"; do bytes=$((bytes + $(stat -c%s "$f"))); done
  rows=0; [ ${#csvs[@]} -gt 0 ] && rows=$(( $(wc -l < "${csvs[0]}") - 1 ))
  gaps=$(grep -c "frame-id gap" "$LOG" || true)
  # connect latency: seconds between "running" and the first per-frame ts log line
  t_run=$(grep -m1 "pipeline: running" "$LOG" | grep -oE "[0-9]{2}:[0-9]{2}:[0-9]{2}")
  t_f1=$(grep -m1 "ts\[fid=" "$LOG" | grep -oE "[0-9]{2}:[0-9]{2}:[0-9]{2}")
  lat="n/a"
  if [ -n "$t_run" ] && [ -n "$t_f1" ]; then
    lat=$(( $(date -d "$t_f1" +%s) - $(date -d "$t_run" +%s) ))s
  fi
  verdict="OK"; { [ "$bytes" -gt 0 ] && [ "$rows" -gt 0 ]; } || verdict="FAIL(starved)"
  [ "${gaps:-0}" -eq 0 ] || verdict="$verdict +${gaps}gap"
  printf "run %d: %-14s rows=%-4s segs=%s bytes=%-9s firstframe=%s ran=%s\n" \
    "$i" "$verdict" "$rows" "$nseg" "$bytes" "$lat" "$ran"
  shopt -u nullglob
done
