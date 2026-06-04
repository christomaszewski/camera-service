#!/usr/bin/env bash
# Bring up exactly ONE shared zenoh router (`rmw_zenohd`) per host -- the vehicle-wide ROS 2 discovery
# hub for rmw_zenoh_cpp. The gige stacks (and any other ROS 2 nodes on the host) connect to it at the
# default tcp/localhost:7447 over host networking. It is a HOST fact, not a per-sensor one: run it once.
#
#   tools/zenohd.sh up      # start it if not already running (idempotent -- safe to re-run)
#   tools/zenohd.sh down    # stop + remove it
#   tools/zenohd.sh status  # is it up?
#
# In a `rig` deploy, rig owns this (one per host) and gige-up assumes it's present. Standalone, run this
# once (or `gige-up --zenohd <cfg> ...`, which calls it for you) before bringing sensors up.
#
#   env knobs:
#     GIGE_ROS2_IMAGE   image that carries rmw_zenohd (default: ros2-bridge; set to your registry tag)
#     GIGE_ZENOHD_NAME  container name (default: gige-zenohd) -- the fixed name is what makes it singular
#     GIGE_ZENOH_NETWORK  docker network (default: host -- the production model; nodes reach localhost:7447)
#     GIGE_ZENOH_PORT   router port (default: 7447; only published when NETWORK != host)
set -euo pipefail
NAME="${GIGE_ZENOHD_NAME:-gige-zenohd}"
IMAGE="${GIGE_ROS2_IMAGE:-ros2-bridge}"
NET="${GIGE_ZENOH_NETWORK:-host}"
PORT="${GIGE_ZENOH_PORT:-7447}"
CMD="${1:-up}"

running() { [ -n "$(docker ps -q -f "name=^${NAME}$" 2>/dev/null)" ]; }

case "$CMD" in
  up)
    if running; then echo "zenohd: '$NAME' already running" >&2; exit 0; fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    net_args=(--network host)
    [ "$NET" != host ] && net_args=(--network "$NET" -p "$PORT:$PORT")
    # Bypass the bridge entrypoint's socket-wait (no camera socket here); just source ROS and run the
    # router. The router is RMW-agnostic (it IS the zenoh hub), so no RMW_IMPLEMENTATION needed.
    docker run -d --name "$NAME" --restart unless-stopped "${net_args[@]}" \
      --entrypoint bash "$IMAGE" -c \
      'source "/opt/ros/${ROS_DISTRO}/setup.bash"; exec ros2 run rmw_zenoh_cpp rmw_zenohd'
    echo "zenohd: started '$NAME' ($IMAGE) on '$NET'${NET:+ }${NET#host}${NET:+:}${PORT}" >&2
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "zenohd: removed '$NAME'" >&2 || echo "zenohd: '$NAME' not present" >&2
    ;;
  status)
    if running; then echo "zenohd: '$NAME' UP" >&2; else echo "zenohd: '$NAME' down" >&2; exit 1; fi
    ;;
  *) echo "usage: tools/zenohd.sh {up|down|status}" >&2; exit 2;;
esac
