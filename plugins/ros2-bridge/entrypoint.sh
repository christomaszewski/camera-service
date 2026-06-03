#!/bin/bash
# Source the ROS distro + this package's overlay, then exec the command.
set -e
source "/opt/ros/${ROS_DISTRO}/setup.bash"
source /ws/install/setup.bash

# Wait for the core's shm transport socket before launching: if shmsrc attaches before the producer
# has created the socket, GStreamer logs a harmless-but-alarming `gst_poll_remove_fd` assertion at
# startup. Bounded (~30s) so a never-arriving core doesn't hang us forever -- then start anyway and
# let GStreamer surface the real error. Default matches the compose `socket_path:=/tmp/gige/frames`.
sock="${GIGE_SHM_SOCKET:-/tmp/gige/frames}"
for _ in $(seq 1 150); do [ -S "$sock" ] && break; sleep 0.2; done
[ -S "$sock" ] || echo "ros2-bridge: $sock not present after 30s; starting anyway" >&2

exec "$@"
