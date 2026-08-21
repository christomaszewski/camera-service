#!/usr/bin/env bash
# Headless RTSP round-trip (no Jetson, no camera, no player UI). Exercises the rtsp-bridge end to
# end against a fake core:
#
#   1. JP6 shm+header + mono (GRAY8), TWO consumer passes -- the second validates the on-demand
#      shared media's teardown-after-last-client + fresh-construct-on-next-DESCRIBE cycle
#   2. H.265 (x265enc + rtph265pay; the consumer's decodebin is codec-agnostic)
#   3. JP7 unixfd + color (Bayer -> bayer2rgb)         [needs a gst >= 1.24 core; auto-skipped]
#   4. GRAY16 thermal + CAM_RTSP_NORMALIZE (the frame pump's 16->8 percentile stretch)
#
# Each scenario:  core (fake cam, PLUGIN endpoint) -> rtsp-bridge (GstRtspServer) -> rtspsrc
# consumer (decode + count, TCP-interleaved, in the bridge netns). shm is shared cross-container
# via a named volume (not a host bind mount -- Docker Desktop's macOS bind mounts can't host a unix
# socket) + --ipc=host. PASS = each consumer pass decoded >= 30 frames.
set -euo pipefail
cd "$(dirname "$0")/../../.."          # repo root
REPO="$(pwd)"

CORE_IMG="${CORE_IMG:-cam-dev}"
RTSP_IMG="${RTSP_IMG:-rtsp-bridge}"
VOL=cam_rtspb_sock
CORE=cam_rtspb_core
BRIDGE=cam_rtspb_bridge
URL=rtsp://127.0.0.1:8554/camera        # CAM_INSTANCE defaults to "camera" -> mount /camera

cleanup() {
  docker rm -f "$CORE" "$BRIDGE" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== build images (if needed) =="
docker image inspect "$CORE_IMG" >/dev/null 2>&1 || docker build -f core-driver/Dockerfile.dev -t "$CORE_IMG" .
docker image inspect "$RTSP_IMG" >/dev/null 2>&1 || docker build -f plugins/rtsp-bridge/Dockerfile -t "$RTSP_IMG" .

# run_scenario <label> <core-config> <consumer-passes> <bridge -e env...>
#   Starts the core + bridge, waits for the server, runs the headless consumer <passes> times
#   (a gap between passes lets the on-demand media tear down so the next pass re-constructs it),
#   prints a verdict, returns nonzero if any pass failed. --entrypoint bash on the core keeps it
#   agnostic to the image's own entrypoint (cam-dev vs cam-core).
run_scenario() {
  local label="$1" config="$2" passes="$3"; shift 3
  echo
  echo "########## SCENARIO: $label ##########"
  docker rm -f "$CORE" "$BRIDGE" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
  docker volume create "$VOL" >/dev/null

  echo "== start core ($config) =="
  docker run -d --rm --name "$CORE" --ipc=host --entrypoint bash \
    -v "$VOL:/tmp/cam" -v "$REPO/core-driver:/app" "$CORE_IMG" \
    -c "cd /app && mkdir -p /data/recordings /tmp/cam && exec python3 main.py -c $config" >/dev/null

  echo "== start rtsp-bridge =="
  docker run -d --rm --name "$BRIDGE" --ipc=host -v "$VOL:/tmp/cam" \
    -v "$REPO/plugins/rtsp-bridge:/app" -e CAM_ADVERTISE=0 "$@" "$RTSP_IMG" bash run.sh >/dev/null

  echo "== wait for the server (socket wait + bind) =="
  local up=0
  for _ in $(seq 1 75); do
    docker logs "$BRIDGE" 2>&1 | grep -q "serving rtsp://" && { up=1; break; }
    sleep 1
  done
  docker logs "$BRIDGE" 2>&1 | grep -E "rtsp-bridge:|serving rtsp://" | head -2 || true
  if [ "$up" != 1 ]; then
    echo "-- scenario FAIL (server never came up) --"
    docker logs "$BRIDGE" 2>&1 | tail -10
    return 1
  fi

  local rc=0 p
  for p in $(seq 1 "$passes"); do
    [ "$p" -gt 1 ] && { echo "== pass $p: on-demand reattach (media tore down after pass $((p-1))) =="; sleep 3; }
    echo "== headless rtspsrc consumer, pass $p/$passes (need >= 30 frames / 40s) =="
    set +e
    docker exec "$BRIDGE" python3 /app/tools/rtsp_consumer.py "$URL" 30 40
    rc=$?
    set -e
    [ "$rc" -ne 0 ] && break
  done
  echo "-- bridge log tail --"
  docker logs "$BRIDGE" 2>&1 | tail -8
  [ "$rc" -eq 0 ] && echo "-- scenario PASS --" || echo "-- scenario FAIL (rc=$rc) --"
  return "$rc"
}

FAILED=0
run_scenario "JP6 shm+header + mono (GRAY8), reconnect x2" \
  config/rtsp-bridge-fake.yaml 2 \
  -e CAM_PLATFORM=jp6 \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  || FAILED=1

run_scenario "H.265 (x265enc, decodebin consumer)" \
  config/rtsp-bridge-fake.yaml 1 \
  -e CAM_PLATFORM=jp6 -e CAM_RTSP_CODEC=h265 \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  || FAILED=1

# unixfd needs a gst >= 1.24 core (the default cam-dev is 22.04/1.20). Skip -- rather than fail --
# when the core image can't produce the transport at all.
if docker run --rm --entrypoint bash "$CORE_IMG" -c 'gst-inspect-1.0 unixfdsink >/dev/null 2>&1'; then
  run_scenario "JP7 unixfd + color (Bayer -> bayer2rgb)" \
    config/webrtc-fake-bayer.yaml 1 \
    -e CAM_PLATFORM=jp7 -e CAM_BAYER=rggb \
    || FAILED=1
else
  echo
  echo "########## SCENARIO: JP7 unixfd + color -- SKIPPED ##########"
  echo "   core image '$CORE_IMG' has no unixfdsink (GStreamer < 1.24); unixfd needs gst >= 1.24."
  echo "   Re-run with a 1.24 core, e.g. CORE_IMG=cam-core:bench RTSP_IMG=rtsp-bridge:jp7."
fi

run_scenario "GRAY16 thermal + normalize (16->8 stretch in the pump)" \
  config/rtsp-bridge-thermal.yaml 1 \
  -e CAM_PLATFORM=jp6 -e CAM_RTSP_NORMALIZE=auto \
  -e CAM_WIDTH=640 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY16_LE -e CAM_FPS=25 \
  || FAILED=1

echo
if [ "$FAILED" -eq 0 ]; then echo "RTSP TEST: PASS"; else echo "RTSP TEST: FAIL"; fi
exit "$FAILED"
