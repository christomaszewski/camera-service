#!/usr/bin/env bash
# Headless WebRTC round-trip (no Jetson, no camera, no browser). Exercises BOTH transports the
# bridge supports, end to end:
#
#   1. JP6 raw shm  + mono  (GRAY8 passthrough)        core --raw shm-->  bridge
#   2. JP7 unixfd   + color (Bayer -> bayer2rgb)       core --unixfd-->   bridge
#
# Each scenario:  core (fake cam) -> webrtc-bridge (webrtcsink + signalling) -> webrtcsrc consumer
# (decode + count). shm is shared cross-container via a named volume (not a host bind mount --
# Docker Desktop's macOS bind mounts can't host a unix socket) + --ipc=host. Proves the whole egress
# path without a browser. PASS = each scenario decoded >= 30 frames.
#
# The unixfd scenario needs a GStreamer >= 1.24 core (unixfdsink landed in 1.24). The default cam-dev
# is Ubuntu 22.04 / gst 1.20 (a JP6 userspace mirror -- no unixfd), so scenario 2 is auto-skipped there;
# run it with a gst >= 1.24 core -- on an Orin: CORE_IMG=cam-core:bench WEBRTC_IMG=webrtc-bridge:jp7.
set -euo pipefail
cd "$(dirname "$0")/../../.."          # repo root
REPO="$(pwd)"

CORE_IMG="${CORE_IMG:-cam-dev}"
WEBRTC_IMG="${WEBRTC_IMG:-webrtc-bridge}"
VOL=cam_webrtc_sock
CORE=cam_webrtc_core
BRIDGE=cam_webrtc_bridge

cleanup() {
  docker rm -f "$CORE" "$BRIDGE" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== build images (if needed) =="
docker image inspect "$CORE_IMG"   >/dev/null 2>&1 || docker build -f core-driver/Dockerfile.dev -t "$CORE_IMG" .
docker image inspect "$WEBRTC_IMG" >/dev/null 2>&1 || docker build -f plugins/webrtc-bridge/Dockerfile -t "$WEBRTC_IMG" .

# run_scenario <label> <core-config> <bridge -e env...>
#   Starts the core + bridge for the scenario, runs the headless consumer, prints a verdict, and
#   returns the consumer's exit code (0 = PASS). --entrypoint bash on the core keeps it agnostic to
#   the image's own entrypoint (cam-dev vs cam-core).
run_scenario() {
  local label="$1" config="$2"; shift 2
  echo
  echo "########## SCENARIO: $label ##########"
  docker rm -f "$CORE" "$BRIDGE" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
  docker volume create "$VOL" >/dev/null

  echo "== start core ($config) =="
  docker run -d --rm --name "$CORE" --ipc=host --entrypoint bash \
    -v "$VOL:/tmp/cam" -v "$REPO/core-driver:/app" "$CORE_IMG" \
    -c "cd /app && mkdir -p /data/recordings /tmp/cam && exec python3 main.py -c $config" >/dev/null

  echo "== start webrtc-bridge =="
  docker run -d --rm --name "$BRIDGE" --ipc=host -v "$VOL:/tmp/cam" \
    -v "$REPO/plugins/webrtc-bridge:/app" "$@" "$WEBRTC_IMG" bash run.sh >/dev/null

  echo "== wait for producer (encoder discovery + transport hookup) =="
  sleep 12
  echo "-- bridge transport line --"
  docker logs "$BRIDGE" 2>&1 | grep -E "webrtc-bridge:" | head -1 || true
  docker logs "$BRIDGE" 2>&1 | grep -E "h264 encode:" | head -1 || true   # auto profile/level (H.264 scenarios)

  echo "== headless webrtcsrc consumer (loopback in the bridge netns; need >= 30 frames / 40s) =="
  set +e
  docker exec "$BRIDGE" python3 /app/tools/webrtc_consumer.py ws://127.0.0.1:8443 30 40
  local rc=$?
  set -e
  echo "-- bridge log tail --"
  docker logs "$BRIDGE" 2>&1 | grep -ivE "set_mempolicy" | tail -8
  [ "$rc" -eq 0 ] && echo "-- scenario PASS --" || echo "-- scenario FAIL (rc=$rc) --"
  return "$rc"
}

FAILED=0
run_scenario "JP6 raw shm + mono (GRAY8 passthrough)" \
  config/webrtc-fake.yaml \
  -e CAM_PLATFORM=jp6 -e CAM_SHM_SOCKET=/tmp/cam/raw \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  || FAILED=1

# H.264 with an AUTO-derived level (the fix for the fixed-profile-level-id black tile). Pinning
# VIDEO_CAPS=video/x-h264 forces the H.264 codec so the level/profile path is exercised; the bridge
# derives the level from the streamed resolution (512x512@25 -> level 3) and pins it (+ profile
# constrained-baseline, the one webrtcsink itself forces at discovery) on the encoder output so the
# SDP profile-level-id matches the stream. BOTH knob values must stay green: the default
# (constrained-baseline) and `high`, which must WARN + fall back to constrained-baseline without
# breaking the stream (webrtcsink rejects any other profile for raw input -- see bridge_stream.py).
run_scenario "H.264 auto-level, constrained-baseline" \
  config/webrtc-fake.yaml \
  -e CAM_PLATFORM=jp6 -e CAM_SHM_SOCKET=/tmp/cam/raw \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  -e VIDEO_CAPS=video/x-h264 -e CAM_WEBRTC_PROFILE=constrained-baseline \
  || FAILED=1

run_scenario "H.264 auto-level, high (warns + falls back to constrained-baseline)" \
  config/webrtc-fake.yaml \
  -e CAM_PLATFORM=jp6 -e CAM_SHM_SOCKET=/tmp/cam/raw \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  -e VIDEO_CAPS=video/x-h264 -e CAM_WEBRTC_PROFILE=high \
  || FAILED=1

# unixfd needs a gst >= 1.24 core (the default cam-dev is 22.04/1.20). Skip scenario 2 -- rather than
# fail it -- when the core image can't produce the transport at all.
if docker run --rm --entrypoint bash "$CORE_IMG" -c 'gst-inspect-1.0 unixfdsink >/dev/null 2>&1'; then
  run_scenario "JP7 unixfd + color (Bayer -> bayer2rgb)" \
    config/webrtc-fake-bayer.yaml \
    -e CAM_PLATFORM=jp7 -e CAM_BAYER=rggb \
    || FAILED=1
else
  echo
  echo "########## SCENARIO: JP7 unixfd + color -- SKIPPED ##########"
  echo "   core image '$CORE_IMG' has no unixfdsink (GStreamer < 1.24); unixfd needs gst >= 1.24."
  echo "   Re-run with a 1.24 core, e.g. CORE_IMG=cam-core:bench WEBRTC_IMG=webrtc-bridge:jp7."
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "WEBRTC TEST: PASS"; else echo "WEBRTC TEST: FAIL"; fi
exit "$FAILED"
