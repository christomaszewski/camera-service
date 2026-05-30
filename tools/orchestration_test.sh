#!/usr/bin/env bash
# Validate the config-driven, multi-sensor orchestration (no Jetson, no camera):
#   - one sensor config -> the right compose profiles (include + profiles + gige-up)
#   - two cameras side by side as isolated projects, each producing to its own shm volume
#   - a container in NEITHER project reads a sensor's frames via its volume + --ipc=host
# Uses the --dev path (gige-dev core, no NVIDIA). Needs the gige-dev + webrtc-bridge images.
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root
REPO="$(pwd)"

# host PyYAML for sensor_env (PEP668 blocks system pip on macOS -> throwaway venv)
if python3 -c "import yaml" 2>/dev/null; then PY=python3; else
  python3 -m venv /tmp/gige-venv >/dev/null 2>&1 || true
  /tmp/gige-venv/bin/pip -q install pyyaml >/dev/null 2>&1 || true
  PY=/tmp/gige-venv/bin/python
fi
export GIGE_PYTHON="$PY"
up() { ./gige-up --dev "core-driver/config/sensors/$1.yaml" "${@:2}"; }
clean() { for n in cam_a cam_b; do up "$n" down >/dev/null 2>&1 || true; done
          docker volume rm gige_cam_a_sock gige_cam_b_sock >/dev/null 2>&1 || true; }
trap clean EXIT
clean
fail() { echo "ORCHESTRATION TEST: FAIL - $1"; exit 1; }

echo "== profile selection (config -> compose profiles) =="
a_svc="$(up cam_a config --services 2>/dev/null | sort | tr '\n' ' ')"
b_svc="$(up cam_b config --services 2>/dev/null | sort | tr '\n' ' ')"
echo "  cam_a -> $a_svc"; echo "  cam_b -> $b_svc"
[[ "$a_svc" == *core-driver*ros2-bridge* ]] || fail "cam_a should select ros2-bridge"
[[ "$a_svc" == *webrtc* ]] && fail "cam_a should NOT select webrtc-bridge"
[[ "$b_svc" == *core-driver*webrtc-bridge* ]] || fail "cam_b should select webrtc-bridge"

echo "== two cameras side by side (isolated projects + shm volumes) =="
up cam_a up -d core-driver >/dev/null 2>&1
up cam_b up -d core-driver >/dev/null 2>&1
sleep 10
A=$(docker ps --filter name=gige_cam_a --format '{{.Names}}'|head -1)
B=$(docker ps --filter name=gige_cam_b --format '{{.Names}}'|head -1)
fa=$(docker logs "$A" 2>&1 | grep -cE 'ts\[fid='); fb=$(docker logs "$B" 2>&1 | grep -cE 'ts\[fid=')
echo "  cam_a frames=$fa  cam_b frames=$fb"
[ "$fa" -gt 0 ] && [ "$fb" -gt 0 ] || fail "both cores must produce frames"
va=$(docker run --rm -v gige_cam_a_sock:/s alpine ls /s 2>/dev/null | tr '\n' ' ')
vb=$(docker run --rm -v gige_cam_b_sock:/s alpine ls /s 2>/dev/null | tr '\n' ' ')
echo "  gige_cam_a_sock=[$va] gige_cam_b_sock=[$vb]"
[[ "$va" == *frames*raw* ]] && [[ "$vb" == *frames*raw* ]] || fail "each volume must hold its camera's shm sockets"

echo "== cross-stack read (a container outside both projects reads cam_a) =="
docker run --rm --ipc=host -v gige_cam_a_sock:/tmp/gige webrtc-bridge bash -c \
  'gst-launch-1.0 shmsrc socket-path=/tmp/gige/raw num-buffers=15 ! "video/x-raw,format=GRAY8,width=512,height=512,framerate=25/1" ! fakesink' >/dev/null 2>&1 \
  && echo "  other-stack read: OK" || fail "external container could not read cam_a's shm"

echo "ORCHESTRATION TEST: PASS"
