#!/usr/bin/env bash
# Headless WebRTC round-trip (no Jetson, no camera, no browser):
#
#   core (fake cam -> raw shm)  ->  webrtc-bridge (webrtcsink + signalling)  ->  webrtcsrc consumer
#
# Proves the full egress path: raw shm -> webrtcsink (encode + congestion control) -> WebRTC ->
# webrtcsrc (decode) -> counted frames. shm is shared cross-container via a named volume
# (not a host bind mount — Docker Desktop's macOS bind mounts can't host a unix socket) + --ipc=host.
set -euo pipefail
cd "$(dirname "$0")/../../.."          # repo root
REPO="$(pwd)"

CORE_IMG="${CORE_IMG:-gige-dev}"
WEBRTC_IMG="${WEBRTC_IMG:-webrtc-bridge}"
VOL=gige_webrtc_sock
CORE=gige_webrtc_core
BRIDGE=gige_webrtc_bridge

cleanup() {
  docker rm -f "$CORE" "$BRIDGE" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== build images (if needed) =="
docker image inspect "$CORE_IMG"   >/dev/null 2>&1 || docker build -f core-driver/Dockerfile.dev -t "$CORE_IMG" .
docker image inspect "$WEBRTC_IMG" >/dev/null 2>&1 || docker build -f plugins/webrtc-bridge/Dockerfile -t "$WEBRTC_IMG" .

docker volume create "$VOL" >/dev/null

echo "== start core (fake cam -> raw shm /tmp/gige/raw) =="
docker run -d --rm --name "$CORE" --ipc=host -v "$VOL:/tmp/gige" -v "$REPO/core-driver:/app" \
  "$CORE_IMG" bash -c "mkdir -p /data/recordings /tmp/gige && python3 main.py -c config/webrtc-fake.yaml" >/dev/null

echo "== start webrtc-bridge (signalling :8443 + webrtcsink) =="
docker run -d --rm --name "$BRIDGE" --ipc=host -v "$VOL:/tmp/gige" \
  -v "$REPO/plugins/webrtc-bridge:/app" \
  -e GIGE_SHM_SOCKET=/tmp/gige/raw -e GIGE_WIDTH=512 -e GIGE_HEIGHT=512 -e GIGE_FORMAT=GRAY8 -e GIGE_FPS=25 \
  "$WEBRTC_IMG" bash run.sh >/dev/null

echo "== wait for producer to register (encoder discovery + shm hookup) =="
sleep 10

echo "== headless webrtcsrc consumer (inside the bridge netns: loopback signalling + ICE) =="
set +e
docker exec "$BRIDGE" python3 /app/tools/webrtc_consumer.py ws://127.0.0.1:8443 30 40
RC=$?
set -e

echo "== bridge log tail =="
docker logs "$BRIDGE" 2>&1 | tail -16

echo
if [ $RC -eq 0 ]; then echo "WEBRTC TEST: PASS"; else echo "WEBRTC TEST: FAIL (rc=$RC)"; fi
exit $RC
