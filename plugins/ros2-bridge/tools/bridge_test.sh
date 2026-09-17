#!/usr/bin/env bash
# End-to-end test: dev producer + the ros2-bridge as a composable component over headered shm
# or unixfd -> sensor_msgs/Image. CAM_DEV_IMAGE selects the producer image; its capabilities
# select the matching bridge. No Jetson
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
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
IMG="${CAM_ROS2_IMAGE:-ros2-bridge}"
CORE_IMG="${CAM_DEV_IMAGE:-cam-dev}"
if docker run --rm --entrypoint sh "$CORE_IMG" -c 'gst-inspect-1.0 unixfdsink >/dev/null 2>&1'; then
  PLATFORM=jp7
  SOCKET=/tmp/cam/unixfd
else
  PLATFORM=jp6
  SOCKET=/tmp/cam/frames
fi

# Exercise platform defaults without forcing CAM_TRANSPORT in the bridge container.
PLATFORM="${CAM_TEST_PLATFORM:-$PLATFORM}"
TEST_DIR="$(mktemp -d /tmp/cam_ros_bridge_test.XXXXXX)"
TEST_NAME="$(basename "$TEST_DIR")"
VOL="${TEST_NAME}_sock"
PRODUCER_ID=""; BRIDGE_ID=""; ROUTER_ID=""
VOLUME_CREATED=0
cleanup() {
  for id in "$PRODUCER_ID" "$BRIDGE_ID" "$ROUTER_ID"; do
    [ -z "$id" ] || docker rm -f "$id" >/dev/null 2>&1 || true
  done
  if [ "$VOLUME_CREATED" = 1 ]; then docker volume rm "$VOL" >/dev/null 2>&1 || true; fi
  rm -rf "$TEST_DIR"
}
trap cleanup EXIT
# Never reuse/delete a pre-existing volume, even in the unlikely event of a name collision.
if docker volume inspect "$VOL" >/dev/null 2>&1; then
  echo "test volume already exists: $VOL" >&2
  exit 1
fi
docker volume create "$VOL" >/dev/null
VOLUME_CREATED=1

echo "== zenoh router (throwaway, bridge net so we can share its netns) =="
ROUTER_ID=$(docker run -d --entrypoint bash "$IMG" \
  -c 'source "/opt/ros/${ROS_DISTRO}/setup.bash"; exec ros2 run rmw_zenoh_cpp rmw_zenohd')
sleep 3

echo "== producer ($CORE_IMG, $SOCKET) -- config=${CAM_TEST_CONFIG:-fake-camera.yaml} =="
PRODUCER_ID=$(docker run -d --rm --ipc=host -v "$VOL:/tmp/cam" -v "$REPO/core-driver:/app" \
  "$CORE_IMG" bash -c "mkdir -p /data/recordings /tmp/cam && python3 main.py -c config/${CAM_TEST_CONFIG:-fake-camera.yaml}")
for _ in $(seq 1 30); do docker run --rm -v "$VOL:/tmp/cam" --entrypoint bash "$IMG" -c "[ -S $SOCKET ]" && break; sleep 0.5; done

echo "== bridge (platform=$PLATFORM, component via launch, rmw_zenoh) =="
BRIDGE_ID=$(docker run -d --rm --network "container:$ROUTER_ID" --ipc=host -v "$VOL:/tmp/cam" \
  -e RMW_IMPLEMENTATION=rmw_zenoh_cpp -e CAM_PLATFORM="$PLATFORM" -e CAM_INSTANCE=camera \
  -e CAM_ROS_TOPIC=image_raw -e CAM_FRAME_ID=camera "$IMG")
sleep 7
docker logs "$BRIDGE_ID" 2>&1 | grep -iE "Instantiate|consuming|ERROR|exception" | tail -3

echo "== verify: typed subscriber on /camera/image_raw (count + encoding + capture stamp) =="
# Host-level QUOTED heredoc -> zero shell escaping; docker cp into the bridge container; run there. Under
# rmw_zenoh a typed subscriber receives normally (unlike the daemon-backed `ros2 topic echo/hz`).
VERIFY="$TEST_DIR/verify.py"
cat > "$VERIFY" <<'PY'
import os, rclpy, time, threading
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
exp = os.environ.get("EXPECT_ENC", "mono8")
rclpy.init(); n = Node("verify"); s = {"n": 0, "enc": "", "sec": 0, "w": 0, "h": 0}
def cb(m):
    s["n"] += 1; s["enc"] = m.encoding; s["sec"] = m.header.stamp.sec; s["w"] = m.width; s["h"] = m.height
n.create_subscription(Image, "/camera/image_raw", cb, qos_profile_sensor_data)
threading.Thread(target=rclpy.spin, args=(n,), daemon=True).start()
time.sleep(6)
print("image_raw: {n} msgs in 6s (~150 @25fps), encoding={enc!r}, {w}x{h}, header.stamp.sec={sec}".format(**s))
assert s["n"] > 60, "too few messages -- bridge not delivering"
assert s["enc"] == exp, "expected encoding {!r}, got {!r}".format(exp, s["enc"])
assert 1_600_000_000 < s["sec"] < 2_000_000_000, "capture timestamp was lost"
print("PASS")
PY
docker cp "$VERIFY" "$BRIDGE_ID:/tmp/verify.py" >/dev/null; rm -f "$VERIFY"
# The verifier's status has to become the SCRIPT's status, and two things were swallowing it:
#   1. this docker exec was the last command, so the script exited with the status of the PIPELINE,
#      which is grep's, not python3's;
#   2. `set -o pipefail` is not inherited -- this is a fresh bash inside the container.
# Together they made the test unfailable in the nastiest way: a failing assert prints a traceback
# that ECHOES THE SOURCE LINE, which contains the word `assert`, so the grep matched and returned 0.
# A bridge delivering zero messages, or the wrong encoding, reported PASS.
set +e
docker exec -e EXPECT_ENC="${CAM_TEST_ENCODING:-mono8}" "$BRIDGE_ID" bash -c '
set -o pipefail
source "/opt/ros/${ROS_DISTRO}/setup.bash"; source /ws/install/setup.bash
export ROS_DOMAIN_ID=0 RMW_IMPLEMENTATION=rmw_zenoh_cpp
timeout 12 python3 /tmp/verify.py 2>&1 | grep -E "image_raw:|PASS|Error|assert"'
RC=$?
set -e

# Explicit verdict + status, matching the sibling webrtc_test.sh / discovery_test.sh.
if [ "$RC" -eq 0 ]; then echo "BRIDGE TEST: PASS"; else echo "BRIDGE TEST: FAIL (rc=$RC)"; fi
exit "$RC"
