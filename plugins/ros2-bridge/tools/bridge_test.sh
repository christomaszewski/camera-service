#!/usr/bin/env bash
# End-to-end test: dev producer (cam-dev, Ubuntu 22.04 / GStreamer 1.20 = JP6 userspace) + the
# ros2-bridge as a composable component over the shm+header transport -> sensor_msgs/Image. No Jetson
# required; validates the full camera -> timestamp -> shm+header -> CamHeaderBridge -> ROS 2 path across
# containers (--ipc=host for the shm data plane) and GStreamer versions (1.20 producer / newer bridge).
#
# rmw_zenoh: the bridge + verifier discover through a throwaway zenoh router (shared netns here so the
# default tcp/localhost:7447 works without host networking). Verification uses a TYPED subscriber node --
# under rmw_zenoh the daemon-backed `ros2 topic echo/hz` often shows nothing even though data flows.
#
# Prereq:
#   docker build -f core-driver/Dockerfile.dev      -t cam-dev   .
#   docker build -f plugins/ros2-bridge/Dockerfile  -t ros2-bridge .
# Run (from anywhere):  ./plugins/ros2-bridge/tools/bridge_test.sh
set -u
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
IMG="${CAM_ROS2_IMAGE:-ros2-bridge}"

cleanup() { docker rm -f cam_producer cam_bridge cam_test_zenohd >/dev/null 2>&1; docker volume rm cam_sock >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
docker volume create cam_sock >/dev/null

echo "== zenoh router (throwaway, bridge net so we can share its netns) =="
docker run -d --name cam_test_zenohd --entrypoint bash "$IMG" \
  -c 'source "/opt/ros/${ROS_DISTRO}/setup.bash"; exec ros2 run rmw_zenoh_cpp rmw_zenohd' >/dev/null
sleep 3

echo "== producer (cam-dev: shm+header, GStreamer 1.20) =="
docker run -d --rm --name cam_producer --ipc=host -v cam_sock:/tmp/cam -v "$REPO/core-driver:/app" \
  cam-dev bash -c "mkdir -p /data/recordings /tmp/cam && python3 main.py -c config/fake-camera.yaml" >/dev/null
for _ in $(seq 1 30); do docker run --rm -v cam_sock:/tmp/cam --entrypoint bash "$IMG" -c '[ -S /tmp/cam/frames ]' && break; sleep 0.5; done

echo "== bridge (CamHeaderBridge component via launch, rmw_zenoh) =="
docker run -d --rm --name cam_bridge --network container:cam_test_zenohd --ipc=host -v cam_sock:/tmp/cam \
  -e RMW_IMPLEMENTATION=rmw_zenoh_cpp -e CAM_PLATFORM=jp6 -e CAM_INSTANCE=camera \
  -e CAM_ROS_TOPIC=image_raw -e CAM_FRAME_ID=camera "$IMG" >/dev/null
sleep 7
docker logs cam_bridge 2>&1 | grep -iE "Instantiate|consuming|ERROR|exception" | tail -3

echo "== verify: typed subscriber on /camera/image_raw (count + encoding + capture stamp) =="
# Host-level QUOTED heredoc -> zero shell escaping; docker cp into the bridge container; run there. Under
# rmw_zenoh a typed subscriber receives normally (unlike the daemon-backed `ros2 topic echo/hz`).
VERIFY="$(mktemp /tmp/cam_verify.XXXX.py)"
cat > "$VERIFY" <<'PY'
import rclpy, time, threading
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
rclpy.init(); n = Node("verify"); s = {"n": 0, "enc": "", "sec": 0, "w": 0, "h": 0}
def cb(m):
    s["n"] += 1; s["enc"] = m.encoding; s["sec"] = m.header.stamp.sec; s["w"] = m.width; s["h"] = m.height
n.create_subscription(Image, "/camera/image_raw", cb, qos_profile_sensor_data)
threading.Thread(target=rclpy.spin, args=(n,), daemon=True).start()
time.sleep(6)
print("image_raw: {n} msgs in 6s (~150 @25fps), encoding={enc!r}, {w}x{h}, header.stamp.sec={sec}".format(**s))
assert s["n"] > 60, "too few messages -- bridge not delivering"
assert s["enc"] == "mono8", "unexpected encoding {enc!r}".format(**s)
print("PASS")
PY
docker cp "$VERIFY" cam_bridge:/tmp/verify.py >/dev/null; rm -f "$VERIFY"
docker exec cam_bridge bash -c '
source "/opt/ros/${ROS_DISTRO}/setup.bash"; source /ws/install/setup.bash
export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_zenoh_cpp
timeout 12 python3 /tmp/verify.py 2>&1 | grep -E "image_raw:|PASS|Error|assert"'
