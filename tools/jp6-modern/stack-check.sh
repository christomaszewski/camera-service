#!/usr/bin/env bash
# jp6m stack check -- after `tools/jp6-modern/cam-up-jp6m <cfg> up -d` (docs/jp6-modern-userspace.md,
# step 4): read the running stack's own logs for the facts the experiment is about.
#   tools/jp6-modern/stack-check.sh <sensor-config.yaml>
# Container names come from the compose project cam-up used (sensor_env: COMPOSE_PROJECT_NAME, default
# cam_<name>; rig: <name>-vehicle-<id>) -- pass --project to override.
set -u
CFG="${1:?usage: stack-check.sh <sensor-config.yaml> [--project NAME]}"; shift || true
PROJECT=""
[ "${1:-}" = --project ] && PROJECT="$2"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -z "$PROJECT" ]; then
  PROJECT="$(python3 "$HERE/tools/sensor_env.py" "$CFG" 2>/dev/null | sed -n "s/^COMPOSE_PROJECT_NAME=//p" | tr -d "'\"")"
fi
[ -n "$PROJECT" ] || { echo "stack-check: could not derive the compose project from $CFG; pass --project" >&2; exit 2; }
c() { docker ps --format '{{.Names}}' | grep -E "^${PROJECT}[-_]$1" | head -1; }
ok() { printf '  \342\234\223 %s\n' "$*"; }
bad() { printf '  \342\234\227 %s\n' "$*"; }
CORE="$(c core-driver)"; WEB="$(c webrtc-bridge)"; ROS="$(c ros2-bridge)"
echo "# jp6m stack check: project $PROJECT"
echo "  core=${CORE:-<none>} webrtc=${WEB:-<none>} ros2=${ROS:-<none>}"
echo "## core"
if [ -n "$CORE" ]; then
  h="$(docker inspect "$CORE" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}')"
  [ "$h" = healthy ] && ok "health: $h" || bad "health: $h"
  docker exec "$CORE" gst-inspect-1.0 --version 2>/dev/null | head -1 | sed 's/^/  /'
  enc="$(docker logs "$CORE" 2>&1 | grep -oE 'recorder: encoder=[a-z0-9-]+' | tail -1)"
  case "$enc" in *hw-hevc-lossless*) ok "$enc (NVENC)";; "") bad "no recorder encoder line yet";; *) bad "$enc (software -- NVENC not usable in this container)";; esac
  tr="$(docker logs "$CORE" 2>&1 | grep -oE 'plugin transport endpoint \([^)]*\)' | tail -1)"
  case "$tr" in *unixfd*) ok "$tr";; "") bad "no transport line";; *) bad "$tr (a 1.28 core should publish unixfd)";; esac
  ts="$(docker logs "$CORE" 2>&1 | grep -iE 'active timestamp source|timestamp source' | tail -1 | sed 's/^.*: //')"
  [ -n "$ts" ] && echo "  timestamp source: $ts"
  docker logs "$CORE" 2>&1 | grep -E 'health: frames=' | tail -1 | sed 's/^/  /'
  n="$(docker logs "$CORE" 2>&1 | grep -ciE 'error|traceback')"; [ "$n" = 0 ] && ok "no errors in the core log" || bad "$n error lines in the core log (docker logs $CORE | grep -iE 'error|traceback')"
fi
echo "## webrtc-bridge"
if [ -n "$WEB" ]; then
  docker exec "$WEB" gst-inspect-1.0 --version 2>/dev/null | head -1 | sed 's/^/  /'
  docker exec "$WEB" gst-inspect-1.0 webrtcsink 2>/dev/null | grep -E '^  Version' | sed 's/^/  webrtcsink/'
  inv="$(docker logs "$WEB" 2>&1 | grep -E 'element inventory' | tail -1)"
  [ -n "$inv" ] && echo "  ${inv#*INFO }"
  case "$inv" in
    *"nvv4l2h264enc=rank "*) ok "webrtcsink can rank nvv4l2h264enc (${inv##*nvv4l2h264enc=}" ;;
    *"nvv4l2h264enc=ABSENT"*) bad "nvv4l2h264enc ABSENT in the bridge's inventory (the L4T stack is not reaching this container)" ;;
    *) bad "no element inventory line yet (the bridge logs it at startup)" ;;
  esac
  docker logs "$WEB" 2>&1 | grep -iE 'viewers|consumers|frames' | tail -1 | sed 's/^/  /'
  docker logs "$WEB" 2>&1 | grep -iE 'not negotiated|error' | tail -2 | sed 's/^/  ! /'
fi
echo "## ros2-bridge"
if [ -n "$ROS" ]; then
  if docker logs "$ROS" 2>&1 | grep -q CamUnixfdBridge; then ok "CamUnixfdBridge loaded (unixfd consumer)"; else bad "unixfd consumer not loaded (CAM_TRANSPORT=unixfd reaches the bridge?)"; fi
  docker logs "$ROS" 2>&1 | grep -iE 'publish|Hz|fps' | tail -1 | sed 's/^/  /'
fi
echo "## recordings"
rdir="$(docker logs "$CORE" 2>&1 | grep -oE 'recorder: encoder=.* -> [^ ]+' | tail -1 | sed 's/.* -> //')"
[ -n "$rdir" ] && docker exec "$CORE" sh -c "ls -t '$rdir' 2>/dev/null | head -3" 2>/dev/null | sed "s|^|  $rdir/|"
