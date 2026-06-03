#!/usr/bin/env bash
# Build the gige-vision images and push them to a registry, so a fleet of Jetsons can PULL them
# instead of building per-vehicle. Jetsons are arm64, so run this on an arm64 host (an Orin is
# perfect) for a native build, or cross-build from x86 with PLATFORM_FLAG (slow; needs qemu binfmt).
#
#   tools/build-images.sh <registry[:port]> [tag]
#     registry   your local registry, e.g. registry.lan:5000
#     tag        default: jp7   (gige-up pulls <registry>/<img>:<platform>, i.e. jp7 on a JP7 box)
#
#   env knobs:
#     IMAGES="gige-core ros2-bridge webrtc-bridge"   subset to build (default: all three)
#     BASE_IMAGE=ubuntu:24.04        gige-core base for JP7 (JP6: nvcr.io/nvidia/l4t-jetpack:r36.4.0, tag jp6)
#     ROS_DISTRO=lyrical             ros2-bridge ROS 2 distro
#     PUSH=1                         set 0 to build+tag locally without pushing
#     PLATFORM_FLAG=                 e.g. --platform=linux/arm64 to cross-build from x86
#
#   examples:
#     tools/build-images.sh registry.lan:5000                      # all three -> :jp7, pushed
#     IMAGES="gige-core ros2-bridge" tools/build-images.sh registry.lan:5000
#     PUSH=0 tools/build-images.sh registry.lan:5000               # local build only
#     BASE_IMAGE=nvcr.io/nvidia/l4t-jetpack:r36.4.0 tools/build-images.sh registry.lan:5000 jp6
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

REGISTRY="${1:?usage: build-images.sh <registry[:port]> [tag]   (e.g. registry.lan:5000 jp7)}"
TAG="${2:-jp7}"
IMAGES="${IMAGES:-gige-core ros2-bridge webrtc-bridge}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:24.04}"
ROS_DISTRO="${ROS_DISTRO:-lyrical}"
PUSH="${PUSH:-1}"
PLATFORM_FLAG="${PLATFORM_FLAG:-}"

REFS=()
build_one() {                      # build_one <image-name> <dockerfile> [extra docker build args...]
  local name="$1" dockerfile="$2"; shift 2
  local ref="$REGISTRY/$name:$TAG"
  echo "==> building $ref" >&2
  docker build ${PLATFORM_FLAG:+$PLATFORM_FLAG} -f "$dockerfile" -t "$ref" "$@" .   # context = repo root
  if [ "$PUSH" = 1 ]; then echo "==> pushing  $ref" >&2; docker push "$ref"; fi
  REFS+=("$ref")
}

for img in $IMAGES; do
  case "$img" in
    gige-core)     build_one gige-core     core-driver/Dockerfile           --build-arg "BASE_IMAGE=$BASE_IMAGE" ;;
    ros2-bridge)   build_one ros2-bridge   plugins/ros2-bridge/Dockerfile   --build-arg "ROS_DISTRO=$ROS_DISTRO" ;;
    webrtc-bridge) build_one webrtc-bridge plugins/webrtc-bridge/Dockerfile ;;
    *) echo "build-images: unknown image '$img' (want: gige-core|ros2-bridge|webrtc-bridge)" >&2; exit 1 ;;
  esac
done

echo
echo "built$([ "$PUSH" = 1 ] && echo ' + pushed') ${#REFS[@]} image(s):"
printf '  %s\n' "${REFS[@]}"
echo
echo "On a JP$([ "$TAG" = jp6 ] && echo 6 || echo 7) vehicle (after cloning the repo there):"
echo "  export GIGE_REGISTRY=$REGISTRY"
echo "  ./gige-up config/sensors/<sensor>.yaml pull"
echo "  ./gige-up config/sensors/<sensor>.yaml up -d"
