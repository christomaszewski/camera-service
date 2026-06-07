#!/usr/bin/env bash
# A/B the RTSP transport (RTP-over-TCP interleaved vs UDP) against the real 4K H.265 camera on the
# Orin. reconnect=OFF and probe=OFF so the ONLY variable is the transport and a stall shows up plainly
# as 0 (or few) frames -- isolating whether TCP-interleaved is what intermittently stalls.
#   rows ~= 30s * stream-fps for a clean run; firstframe = connect->first-frame latency.
set -u
REPO="${REPO:-/home/uxv/ws/gige-vision-service}"
REC=/tmp/camval/rec
mkdir -p /tmp/camval

mkcfg() {  # $1 = protocols (udp|tcp)
  cat > "/tmp/camval/cfg_$1.yaml" <<YAML
source:
  type: rtsp
rtsp:
  url: rtsp://192.168.6.100:8554/main.264
  protocols: $1
  probe: false
  reconnect: false
  codec: h265
  width: 3840
  height: 2160
  latency_ms: 200
recording:
  enabled: true
  encoder: auto
  output_dir: /data/recordings
  name_prefix: rtsprec
  segment_seconds: 60
preview:
  enabled: false
transport:
  plugin_endpoint:
    enabled: true
    socket_path: /tmp/cam/frames
plugins: []
YAML
}
mkcfg udp; mkcfg tcp

run() {  # $1 = proto  $2 = idx
  rm -rf "$REC"; mkdir -p "$REC"
  cid=$(docker run -d --network host --device nvidia.com/gpu=all \
        -v "$REPO/core-driver:/app" -v /tmp/camval:/cfg -v "$REC:/data/recordings" \
        cam-core:jp7 main.py -c "/cfg/cfg_$1.yaml")
  for _ in $(seq 1 30); do docker logs "$cid" 2>&1 | grep -q "pipeline: running" && break; sleep 1; done
  sleep 25
  docker kill --signal=INT "$cid" >/dev/null 2>&1 || true
  timeout 12 docker wait "$cid" >/dev/null 2>&1 || true
  rows=0; [ -f "$REC"/*.csv ] 2>/dev/null && rows=$(( $(wc -l < "$REC"/*.csv) - 1 )) || true
  log=$(docker logs "$cid" 2>&1)
  t_run=$(echo "$log" | grep -m1 "pipeline: running" | grep -oE "[0-9]{2}:[0-9]{2}:[0-9]{2}")
  t_f1=$(echo "$log" | grep -m1 "ts\[fid=" | grep -oE "[0-9]{2}:[0-9]{2}:[0-9]{2}")
  lat="n/a"; [ -n "$t_run" ] && [ -n "$t_f1" ] && lat=$(( $(date -d "$t_f1" +%s) - $(date -d "$t_run" +%s) ))s
  docker rm -f "$cid" >/dev/null 2>&1 || true
  v="OK"; [ "${rows:-0}" -gt 50 ] || v="STALL"
  printf "  %s run %d: %-5s rows=%-4s firstframe=%s\n" "$1" "$2" "$v" "${rows:-0}" "$lat"
}

echo "### transport A/B (reconnect OFF, probe OFF, 30s window, 12s rest between runs) ###"
echo "(initial 20s rest so the camera reaps prior sessions)"; sleep 20
echo "== TCP (RTP-over-TCP interleaved) =="; for i in 1 2 3 4; do run tcp "$i"; sleep 8; done
echo "(rest 20s)"; sleep 20
echo "== UDP =="; for i in 1 2 3 4; do run udp "$i"; sleep 8; done
