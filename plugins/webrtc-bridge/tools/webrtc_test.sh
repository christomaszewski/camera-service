#!/usr/bin/env bash
# Headless WebRTC round-trip (no Jetson, no camera, no browser). Exercises ALL transports the
# bridge supports, end to end:
#
#   1. JP6 headered shm + mono  (self-describing; latency metrics + burned-in overlay)
#   2. JP6 headered shm + color (GRAY8 mosaic relabeled video/x-bayer -> bayer2rgb)
#   3. shm-raw (legacy) + mono  (env geometry; also the two H.264 profile/level scenarios and
#                                the CAM_WEBRTC_MAX_SIZE encode-side downscale)
#   4. JP7 unixfd       + color (Bayer -> bayer2rgb; + the downscale behind the runtime seam)
#   5. viewer sessions: the reaper ending a viewer that went silent
#
# Each scenario:  core (fake cam) -> webrtc-bridge (webrtcsink + signalling) -> webrtcsrc consumer
# (decode + count). shm is shared cross-container via a named volume (not a host bind mount --
# Docker Desktop's macOS bind mounts can't host a unix socket) + --ipc=host. Proves the whole egress
# path without a browser. PASS = each scenario decoded >= 30 frames.
#
# The unixfd scenario needs a GStreamer >= 1.24 core (unixfdsink landed in 1.24).
# Dev example: CORE_IMG=cam-dev:dev WEBRTC_IMG=webrtc-bridge:dev CAM_TEST_PLATFORM=dev.
# On an Orin: CORE_IMG=cam-core:bench WEBRTC_IMG=webrtc-bridge:jp7.
# Inversely, the HEADERED scenarios need a core WITHOUT unixfdsink (unixfd replaces the header
# endpoint when available), i.e. a --target distro Ubuntu 22.04 build; skipped on a modern core.
set -euo pipefail
cd "$(dirname "$0")/../../.."          # repo root
REPO="$(pwd)"

CORE_IMG="${CORE_IMG:-cam-dev}"
WEBRTC_IMG="${WEBRTC_IMG:-webrtc-bridge}"
TEST_DIR="$(mktemp -d /tmp/cam_webrtc_test.XXXXXX)"
TEST_NAME="$(basename "$TEST_DIR")"
VOL="${TEST_NAME}_sock"
CORE="${TEST_NAME}_core"
BRIDGE="${TEST_NAME}_bridge"
# Cleanup below is confined to this run's namespace. Refuse an existing resource rather than
# deleting it; the same names are then reused only between scenarios owned by this run.
if docker container inspect "$CORE" >/dev/null 2>&1 \
    || docker container inspect "$BRIDGE" >/dev/null 2>&1 \
    || docker volume inspect "$VOL" >/dev/null 2>&1; then
  rmdir "$TEST_DIR"
  echo "test resource already exists: $TEST_NAME" >&2
  exit 1
fi

cleanup() {
  docker rm -f "$CORE" "$BRIDGE" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap 'cleanup; rmdir "$TEST_DIR"' EXIT

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
  wait_for_producer || { echo "-- scenario FAIL: the bridge never registered as a producer --"; return 1; }
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

# wait_for_producer: the consumer connects to the FIRST producer the signalling server lists, and gets
# nothing if it asks before the bridge has registered -- which the bridge only does once webrtcsink
# has finished discovering its codecs. That takes a second with one pinned hardware codec and ten with
# every software encoder an image carries, so a fixed sleep here was a race (it lost on an Orin with
# the jp7 images, where discovery ran 10 s against a 12 s sleep that started before the pipeline did).
wait_for_producer() {
  for _ in $(seq 1 60); do
    if [[ "$(docker logs "$BRIDGE" 2>&1)" == *"registered as [Producer]"* ]]; then sleep 1; return 0; fi
    sleep 1
  done
  return 1
}

# bridge_log_has <substring>: poll the bridge log (up to 20s) for a substring. Polled because the
# heartbeat is periodic and its first tick can land just AFTER the consumer exits on a slow cold
# start. Substring check, NOT `| grep -q`: under this script's pipefail, grep -q exiting at first
# match SIGPIPEs a still-writing `docker logs` and the pipeline reports 141 -- a false negative that
# triggers exactly when the log is big enough (measured: the chattier 0.15 signalling log tipped it
# while 0.13 stayed under the buffer).
bridge_log_has() {
  local needle="$1"
  for _ in $(seq 1 20); do
    if [[ "$(docker logs "$BRIDGE" 2>&1)" == *"$needle"* ]]; then return 0; fi
    sleep 1
  done
  return 1
}

# in_bridge_signal <SIGNAL> <cmdline substring>: signal the matching processes inside the bridge
# container (the image has no procps).
in_bridge_signal() {
  docker exec "$BRIDGE" python3 -c "
import os, signal, sys
for pid in filter(str.isdigit, os.listdir('/proc')):
    try:
        cmd = open('/proc/%s/cmdline' % pid, 'rb').read().replace(b'\\0', b' ').decode()
    except OSError:
        continue
    if sys.argv[2] in cmd and int(pid) != os.getpid():
        os.kill(int(pid), getattr(signal, sys.argv[1]))
" "$1" "$2"
}

# run_reaper_scenario: a viewer that goes SILENT without closing anything -- what a radio dropout or a
# closed laptop lid looks like from here. Its process is frozen (SIGSTOP): the sockets stay open and the
# kernel keeps acknowledging, but no RTCP and no transport feedback come back. webrtcsink alone keeps
# such a session (and its encoder) until the signalling TCP times out, a quarter of an hour later; the
# reaper must end it CAM_WEBRTC_STALE_S after the feedback stopped. Before that, the two things that
# must NOT end a session: a live viewer, and a live viewer whose camera stalls.
run_reaper_scenario() {
  echo
  echo "########## SCENARIO: a viewer that goes silent is ended (CAM_WEBRTC_STALE_S=5) ##########"
  docker rm -f "$CORE" "$BRIDGE" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
  docker volume create "$VOL" >/dev/null
  docker run -d --rm --name "$CORE" --ipc=host --entrypoint bash \
    -v "$VOL:/tmp/cam" -v "$REPO/core-driver:/app" "$CORE_IMG" \
    -c "cd /app && mkdir -p /data/recordings /tmp/cam && exec python3 main.py -c config/webrtc-fake.yaml" >/dev/null
  docker run -d --rm --name "$BRIDGE" --ipc=host -v "$VOL:/tmp/cam" \
    -v "$REPO/plugins/webrtc-bridge:/app" -e CAM_PLATFORM=jp6 -e CAM_TRANSPORT=shm-raw \
    -e CAM_SHM_SOCKET=/tmp/cam/raw -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
    -e CAM_WEBRTC_STALE_S=5 -e CAM_WEBRTC_STATUS=2 "$WEBRTC_IMG" bash run.sh >/dev/null
  wait_for_producer || { echo "-- scenario FAIL: the bridge never registered as a producer --"; return 1; }
  # a viewer that would watch for a long time (a frame target it never reaches, a 120 s timeout)
  docker exec -d "$BRIDGE" python3 /app/tools/webrtc_consumer.py ws://127.0.0.1:8443 1000000 120
  if ! bridge_log_has "webrtc consumer connected"; then
    echo "-- scenario FAIL: the viewer never connected --"; return 1
  fi
  sleep 8        # well past the 5 s: a LIVE viewer must not be ended
  if [[ "$(docker logs "$BRIDGE" 2>&1)" == *"ending viewer session"* ]]; then
    echo "-- scenario FAIL: a live viewer was ended --"; return 1
  fi
  echo "-- the live viewer was left alone for 8 s; stalling the camera for 20 s --"
  # A stalled camera (a reconnect, a paused replay) silences a LIVE receiver too: with nothing sent it
  # has nothing to acknowledge, and its feedback stops a few seconds after the last frame. The viewer
  # must keep its session through that -- silence only counts while frames are going out.
  docker pause "$CORE" >/dev/null; sleep 20; docker unpause "$CORE" >/dev/null
  if [[ "$(docker logs "$BRIDGE" 2>&1)" == *"ending viewer session"* ]]; then
    echo "-- scenario FAIL: a live viewer was ended because the camera stalled --"; return 1
  fi
  sleep 3
  echo "-- the viewer kept its session through the stall; freezing the viewer --"
  in_bridge_signal SIGSTOP webrtc_consumer.py
  if bridge_log_has "no receiver feedback" && bridge_log_has "webrtc consumer left"; then
    docker logs "$BRIDGE" 2>&1 | grep -E "ending viewer session|consumer left" | tail -2
    echo "-- scenario PASS --"
  else
    docker logs "$BRIDGE" 2>&1 | grep -ivE "set_mempolicy" | tail -8
    echo "-- scenario FAIL: the silent viewer's session was not ended --"; return 1
  fi
  in_bridge_signal SIGKILL webrtc_consumer.py || true
}

FAILED=0
# One probe, two gates: unixfd needs a gst >= 1.24 core; the headered-shm endpoint only EXISTS on a
# core without unixfdsink (unixfd replaces it at the same plugin endpoint when available).
if docker run --rm --entrypoint bash "$CORE_IMG" -c 'gst-inspect-1.0 unixfdsink >/dev/null 2>&1'; then
  CORE_HAS_UNIXFD=1
else
  CORE_HAS_UNIXFD=0
fi

if [ "$CORE_HAS_UNIXFD" = 0 ]; then
  # The JP6 DEFAULT path: no geometry env AT ALL -- the header must self-describe 512x512 GRAY8@25.
  # The overlay knob doubles as the fonts/pango smoke test (textoverlay errors the pipeline when the
  # element or a font is missing). After the consumer passes, the bridge log must show the
  # capture->encode latency percentiles -- that is the heartbeat metric this transport exists for.
  if run_scenario "JP6 headered shm + mono (self-describing + latency)" \
      config/webrtc-fake.yaml \
      -e CAM_PLATFORM=jp6 -e CAM_WEBRTC_LATENCY_OVERLAY=1 -e CAM_WEBRTC_STATUS=5; then
    if bridge_log_has "lat[cap->enc]"; then
      echo "-- latency metrics present in the heartbeat --"
    else
      echo "-- FAIL: no lat[cap->enc] heartbeat line within 20s (capture timestamps didn't survive) --"
      FAILED=1
    fi
  else
    FAILED=1
  fi

  # Color on the headered path: the wire carries the GRAY8 mosaic; CAM_BAYER relabels it
  # video/x-bayer and the fmt_tap seam splices bayer2rgb. + the encode-side downscale BEHIND that
  # seam (tap -> bayer2rgb -> fmt_scale): the plan must come from the stream's caps -- no geometry
  # env on this path -- and land after the debayer.
  if run_scenario "JP6 headered shm + color (Bayer relabel -> bayer2rgb) + CAM_WEBRTC_MAX_SIZE=256" \
      config/webrtc-fake-bayer.yaml \
      -e CAM_PLATFORM=jp6 -e CAM_BAYER=rggb -e CAM_WEBRTC_MAX_SIZE=256; then
    if bridge_log_has "scale: 512x512 -> 256x256"; then
      echo "-- downscale planned from the stream's own caps (behind the fmt_tap seam) --"
    else
      echo "-- FAIL: no 'scale: 512x512 -> 256x256' line (runtime plan behind the seam) --"
      FAILED=1
    fi
  else
    FAILED=1
  fi
else
  echo
  echo "########## SCENARIOS: JP6 headered shm -- SKIPPED ##########"
  echo "   core image '$CORE_IMG' HAS unixfdsink (gst >= 1.24), so its plugin endpoint serves unixfd,"
  echo "   not shm+header. Re-run with a distro-target 22.04/gst 1.20 core to cover these."
fi

run_scenario "shm-raw (legacy) + mono (GRAY8, env geometry)" \
  config/webrtc-fake.yaml \
  -e CAM_PLATFORM=jp6 -e CAM_TRANSPORT=shm-raw -e CAM_SHM_SOCKET=/tmp/cam/raw \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  || FAILED=1

# H.264 with an AUTO-derived level (the fix for the fixed-profile-level-id black tile). Pinning
# VIDEO_CAPS=video/x-h264 forces the H.264 codec so the level/profile path is exercised; the bridge
# derives the level from the streamed resolution (512x512@25 -> level 3) and pins it (+ profile
# constrained-baseline, the one webrtcsink itself forces at discovery) on the encoder output so the
# SDP profile-level-id matches the stream. BOTH knob values must stay green: the default
# (constrained-baseline) and `high`, which must WARN + fall back to constrained-baseline without
# breaking the stream (webrtcsink rejects any other profile for raw input -- see bridge_stream.py).
# Kept on the shm-raw path: the level math reads env geometry there, the fallback worth pinning.
run_scenario "H.264 auto-level, constrained-baseline" \
  config/webrtc-fake.yaml \
  -e CAM_PLATFORM=jp6 -e CAM_TRANSPORT=shm-raw -e CAM_SHM_SOCKET=/tmp/cam/raw \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  -e VIDEO_CAPS=video/x-h264 -e CAM_WEBRTC_PROFILE=constrained-baseline \
  || FAILED=1

run_scenario "H.264 auto-level, high (warns + falls back to constrained-baseline)" \
  config/webrtc-fake.yaml \
  -e CAM_PLATFORM=jp6 -e CAM_TRANSPORT=shm-raw -e CAM_SHM_SOCKET=/tmp/cam/raw \
  -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
  -e VIDEO_CAPS=video/x-h264 -e CAM_WEBRTC_PROFILE=high \
  || FAILED=1

# Encode-side downscale (CAM_WEBRTC_MAX_SIZE): the fake 512x512 must reach the encoder at 256x256.
# Two assertions off the bridge log: the scaler's plan line, and the auto-level line -- which reads
# webrtcsink's NEGOTIATED input caps, so it proves the encoder (not just the scaler) saw the scaled
# geometry (the derived level follows it too -- 1.3 here; the proof is the "for 256x256@" text).
if run_scenario "shm-raw + CAM_WEBRTC_MAX_SIZE=256 (encode-side downscale)" \
    config/webrtc-fake.yaml \
    -e CAM_PLATFORM=jp6 -e CAM_TRANSPORT=shm-raw -e CAM_SHM_SOCKET=/tmp/cam/raw \
    -e CAM_WIDTH=512 -e CAM_HEIGHT=512 -e CAM_FORMAT=GRAY8 -e CAM_FPS=25 \
    -e VIDEO_CAPS=video/x-h264 -e CAM_WEBRTC_MAX_SIZE=256; then
  if bridge_log_has "scale: 512x512 -> 256x256" && bridge_log_has "for 256x256@"; then
    echo "-- downscale planned + encoder negotiated 256x256 --"
  else
    echo "-- FAIL: encoder did not negotiate 256x256 (see the 'scale:' / 'h264 encode:' lines) --"
    FAILED=1
  fi
else
  FAILED=1
fi

run_reaper_scenario || FAILED=1

if [ "$CORE_HAS_UNIXFD" = 1 ]; then
  # + the encode-side downscale BEHIND the runtime format seam (tap -> bayer2rgb -> fmt_scale):
  # the plan must come from the stream's caps (no geometry env on this path).
  if run_scenario "${CAM_TEST_PLATFORM:-jp7} unixfd + color (Bayer -> bayer2rgb) + CAM_WEBRTC_MAX_SIZE=256" \
      config/webrtc-fake-bayer.yaml \
      -e CAM_PLATFORM="${CAM_TEST_PLATFORM:-jp7}" -e CAM_BAYER=rggb -e CAM_WEBRTC_MAX_SIZE=256; then
    if bridge_log_has "scale: 512x512 -> 256x256"; then
      echo "-- downscale planned from the stream's own caps (behind the fmt_tap seam) --"
    else
      echo "-- FAIL: no 'scale: 512x512 -> 256x256' line (runtime plan behind the seam) --"
      FAILED=1
    fi
  else
    FAILED=1
  fi
else
  echo
  echo "########## SCENARIO: JP7 unixfd + color -- SKIPPED ##########"
  echo "   core image '$CORE_IMG' has no unixfdsink (GStreamer < 1.24); unixfd needs gst >= 1.24."
  echo "   Re-run with a 1.24 core, e.g. CORE_IMG=cam-core:bench WEBRTC_IMG=webrtc-bridge:jp7."
fi

echo
if [ "$FAILED" -eq 0 ]; then echo "WEBRTC TEST: PASS"; else echo "WEBRTC TEST: FAIL"; fi
exit "$FAILED"
