#!/bin/bash
# Source the ROS distro + this package's overlay, then exec the command.
set -e
source "/opt/ros/${ROS_DISTRO}/setup.bash"
source /ws/install/setup.bash

# Wait for the core's transport socket before launching: if the src attaches before the producer has
# created the socket, GStreamer logs a harmless-but-alarming `gst_poll_remove_fd` assertion at startup.
# Bounded (~30s) so a never-arriving core doesn't hang us forever -- then start anyway and let GStreamer
# surface the real error. The socket differs by transport: unixfd (JP7) -> /tmp/cam/unixfd, shm+header
# (JP6) -> /tmp/cam/frames; mirror the same selection the launch file makes from CAM_PLATFORM/TRANSPORT.
# ros2-source OWNS its socket (the core is its client): nothing to wait for -- its compose sets CAM_WAIT_SOCKET=0.
if [ "${CAM_WAIT_SOCKET:-1}" = 0 ]; then exec "$@"; fi
transport="${CAM_TRANSPORT:-}"
if [ "$transport" != unixfd ] && [ "$transport" != header ]; then
  case "${CAM_PLATFORM:-}" in jp7|jp6m) transport=unixfd;; *) transport=header;; esac   # jp6m: a 1.28 core on a JP6 host
fi
default_sock="/tmp/cam/frames"; [ "$transport" = unixfd ] && default_sock="/tmp/cam/unixfd"
sock="${CAM_TRANSPORT_SOCKET:-${CAM_SHM_SOCKET:-$default_sock}}"
for _ in $(seq 1 150); do [ -S "$sock" ] && break; sleep 0.2; done
[ -S "$sock" ] || echo "ros2-bridge: $sock not present after 30s; starting anyway" >&2

exec "$@"
