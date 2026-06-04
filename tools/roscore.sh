#!/usr/bin/env bash
# Bring up exactly ONE shared roscore (the ROS 1 master) per host -- the vehicle-wide ROS 1 graph hub for
# the ros1-bridge, analogous to the shared rmw_zenohd router for ros2. ROS 1 nodes reach it at
# ROS_MASTER_URI=http://localhost:11311 over host networking. Idempotent: re-running is a no-op if it's up.
# rig runs this once per host in production; standalone, run it (or `gige-up --roscore`) before sensors.
#
#   tools/roscore.sh up      # start it if not already running
#   tools/roscore.sh down    # stop + remove it
#   tools/roscore.sh status  # is it up?
#
#   env knobs:
#     GIGE_ROS1_IMAGE    image that carries roscore (default: ros1-bridge; set to your registry tag)
#     GIGE_ROSCORE_NAME  container name (default: gige-roscore) -- the fixed name keeps it singular
#     GIGE_ROS1_NETWORK  docker network (default: host -- nodes reach localhost:11311)
#     GIGE_ROSCORE_PORT  master port (default: 11311; only published when NETWORK != host)
set -euo pipefail
NAME="${GIGE_ROSCORE_NAME:-gige-roscore}"
IMAGE="${GIGE_ROS1_IMAGE:-ros1-bridge}"
NET="${GIGE_ROS1_NETWORK:-host}"
PORT="${GIGE_ROSCORE_PORT:-11311}"
CMD="${1:-up}"

running() { [ -n "$(docker ps -q -f "name=^${NAME}$" 2>/dev/null)" ]; }

case "$CMD" in
  up)
    if running; then echo "roscore: '$NAME' already running" >&2; exit 0; fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    net_args=(--network host)
    [ "$NET" != host ] && net_args=(--network "$NET" -p "$PORT:$PORT")
    # Bypass the bridge entrypoint's socket-wait (no camera socket here); just source ROS 1 and run the
    # master. roscore = the ROS 1 master + parameter server + rosout.
    docker run -d --name "$NAME" --restart unless-stopped "${net_args[@]}" \
      --entrypoint bash "$IMAGE" -c 'source "/opt/ros/${ROS_DISTRO}/setup.bash"; exec roscore'
    echo "roscore: started '$NAME' ($IMAGE) on '$NET' :$PORT" >&2
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "roscore: removed '$NAME'" >&2 || echo "roscore: '$NAME' not present" >&2
    ;;
  status)
    if running; then echo "roscore: '$NAME' UP" >&2; else echo "roscore: '$NAME' down" >&2; exit 1; fi
    ;;
  *) echo "usage: tools/roscore.sh {up|down|status}" >&2; exit 2;;
esac
