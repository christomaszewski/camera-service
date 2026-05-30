#!/usr/bin/env bash
# End-to-end test: dev producer (gige-dev) + C++ ros2-bridge over shm -> sensor_msgs/Image.
# No Jetson required -- validates the full camera -> timestamp -> shm+header -> ROS2 path,
# across containers (--ipc=host) and across GStreamer versions (1.20 producer / 1.24 bridge).
#
# Prereq:
#   docker build -f core-driver/Dockerfile.dev      -t gige-dev   .
#   docker build -f plugins/ros2-bridge/Dockerfile  -t ros2-bridge .
# Run (from anywhere):  ./plugins/ros2-bridge/tools/bridge_test.sh
set -u
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SRC="source /opt/ros/jazzy/setup.bash && source /ws/install/setup.bash"

cleanup() { docker rm -f gige_producer gige_bridge >/dev/null 2>&1; docker volume rm gige_sock >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
docker volume create gige_sock >/dev/null

echo "== producer (gige-dev) =="
docker run -d --rm --name gige_producer --ipc=host -v gige_sock:/tmp/gige -v "$REPO/core-driver:/app" \
  gige-dev bash -c "mkdir -p /data/recordings /tmp/gige && python3 main.py -c config/fake-camera.yaml" >/dev/null
sleep 6

echo "== bridge (ros2-bridge) =="
docker run -d --rm --name gige_bridge --ipc=host -v gige_sock:/tmp/gige \
  ros2-bridge ros2 run gige_ros2_bridge gige_ros2_bridge --ros-args \
    -p socket_path:=/tmp/gige/frames -p topic:=/image_raw -p frame_id:=camera >/dev/null
sleep 6

docker logs gige_bridge 2>&1 | tail -3
echo "== ros2 topic hz /image_raw =="
docker exec gige_bridge bash -c "$SRC && timeout 7 ros2 topic hz /image_raw" 2>&1 | head -3
echo "== ros2 Image header + metadata =="
docker exec gige_bridge bash -c "$SRC && timeout 6 ros2 topic echo /image_raw --once --no-arr" 2>&1 | head -15
echo "== ros2 CompressedImage (lazy JPEG on /image_raw/compressed) =="
docker exec gige_bridge bash -c "$SRC && timeout 7 ros2 topic echo /image_raw/compressed --once --no-arr" 2>&1 | head -10
