#!/bin/bash
# Source the ROS distro + this package's overlay, then exec the command.
set -e
source "/opt/ros/${ROS_DISTRO}/setup.bash"
source /ws/install/setup.bash
exec "$@"
