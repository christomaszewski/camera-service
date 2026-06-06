#!/bin/bash
# Source the ROS 1 distro + this workspace's overlay, then exec the command.
set -e
source "/opt/ros/${ROS_DISTRO}/setup.bash"
source /ws/install/setup.bash

# Wait for the core's shm transport socket before launching: if shmsrc attaches before the producer has
# created the socket, GStreamer logs a harmless-but-alarming gst_poll assertion at startup. Bounded
# (~30s), then start anyway. ROS 1 reads the shm + 36-byte header endpoint (/tmp/cam/frames).
sock="${CAM_TRANSPORT_SOCKET:-${CAM_SHM_SOCKET:-/tmp/cam/frames}}"
for _ in $(seq 1 150); do [ -S "$sock" ] && break; sleep 0.2; done
[ -S "$sock" ] || echo "ros1-bridge: $sock not present after 30s; starting anyway" >&2

exec "$@"
