#!/usr/bin/env bash
# Headless fleet-discovery round-trip (no Jetson, no camera). Brings up a Zenoh router (rmw_zenohd) +
# the core (fake camera) + the webrtc-bridge in containers, and a Zenoh probe asserts the discovery
# contract (docs/DISCOVERY.md):
#   a) a liveliness TOKEN at fleet/<vehicle>/media/<sensor> appears once the bridge is streaming (PUT),
#   b) get(<key>) returns a valid JSON descriptor matching the schema,
#   c) on bridge shutdown the token disappears (DELETE).
#
# Uses host networking -- the production model: producers reach the vehicle's local zenohd at
# tcp/localhost:7447. So run this on the Orin / a Linux box (not Docker Desktop). The video path is
# NOT exercised here (that's webrtc_test.sh); the pipeline only needs to reach PLAYING to advertise.
#
# Images (override via env): CORE_IMG (gst>=1.24 for unixfd), WEBRTC_IMG, ROUTER_IMG (carries rmw_zenohd).
#   ./discovery_test.sh        # CORE_IMG=cam-core:bench WEBRTC_IMG=webrtc-bridge:jp7 ROUTER_IMG=ros2-bridge:jp7
set -euo pipefail
cd "$(dirname "$0")/../../.."
REPO="$(pwd)"

CORE_IMG="${CORE_IMG:-cam-core:bench}"
BRIDGE_IMG="${WEBRTC_IMG:-webrtc-bridge:jp7}"
ROUTER_IMG="${ROUTER_IMG:-ros2-bridge:jp7}"
VOL=cam_disco_sock
VEH=testvehicle
SENSOR=cam_fake
KEY="fleet/$VEH/media/$SENSOR"

R=cam_disco_router; C=cam_disco_core; B=cam_disco_bridge; P=cam_disco_probe
cleanup() { docker rm -f "$R" "$C" "$B" "$P" >/dev/null 2>&1 || true; docker volume rm "$VOL" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

for img in "$CORE_IMG" "$BRIDGE_IMG" "$ROUTER_IMG"; do
  docker image inspect "$img" >/dev/null 2>&1 || { echo "discovery_test: missing image '$img'"; exit 1; }
done
docker volume create "$VOL" >/dev/null

echo "== start zenoh router (rmw_zenohd, host net :7447) =="
docker run -d --rm --name "$R" --network host --entrypoint bash "$ROUTER_IMG" \
  -c 'source "/opt/ros/${ROS_DISTRO}/setup.bash"; exec ros2 run rmw_zenoh_cpp rmw_zenohd' >/dev/null
sleep 4

echo "== start core (fake BayerRG8 -> unixfd) =="
docker run -d --rm --name "$C" --network host --entrypoint bash \
  -v "$VOL:/tmp/cam" -v "$REPO/core-driver:/app" "$CORE_IMG" \
  -c "cd /app && mkdir -p /tmp/cam && exec python3 main.py -c config/webrtc-fake-bayer.yaml" >/dev/null

echo "== start webrtc-bridge (advertises to tcp/localhost:7447) =="
docker run -d --rm --name "$B" --network host \
  -v "$VOL:/tmp/cam" -v "$REPO/plugins/webrtc-bridge:/app" \
  -e CAM_PLATFORM=jp7 -e CAM_BAYER=rggb -e CAM_INSTANCE="$SENSOR" -e VEHICLE_ID="$VEH" \
  "$BRIDGE_IMG" bash run.sh >/dev/null

echo "== start probe (liveliness subscriber on fleet/*/media/*) =="
docker run -d --name "$P" --network host -v "$REPO/plugins/webrtc-bridge:/app" \
  -e ZENOH_CONNECT=tcp/localhost:7447 \
  --entrypoint python3 "$BRIDGE_IMG" -u /app/tools/discovery_probe.py --timeout 120 >/dev/null

plog() { docker logs "$P" 2>&1; }

echo "== (a) PUT + (b) valid descriptor (<=45s) =="
ok=0
for _ in $(seq 1 45); do
  if plog | grep -q "EVENT PUT $KEY" && plog | grep -q "DESCRIPTOR_OK $KEY"; then ok=1; break; fi
  sleep 1
done
echo "--- probe ---"; plog | grep -E "READY|EVENT|DESCRIPTOR" | head
echo "--- advertised descriptor ---"; docker logs "$B" 2>&1 | grep -oE 'descriptor: \{.*\}' | tail -1
[ "$ok" = 1 ] || { echo; echo "DISCOVERY TEST: FAIL (no PUT / invalid descriptor)"; exit 1; }

echo "== (c) stop the bridge -> expect DELETE (graceful SIGTERM undeclare) =="
docker stop -t 8 "$B" >/dev/null
del=0
for _ in $(seq 1 20); do plog | grep -q "EVENT DELETE $KEY" && { del=1; break; }; sleep 1; done
echo "--- probe tail ---"; plog | grep -E "EVENT|SUMMARY" | tail
[ "$del" = 1 ] || { echo; echo "DISCOVERY TEST: FAIL (token did not disappear on shutdown)"; exit 1; }

echo; echo "DISCOVERY TEST: PASS (PUT + descriptor + DELETE at $KEY)"
