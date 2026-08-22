#!/usr/bin/env bash
# Build the camera-service images and push them to a registry, so a fleet of Jetsons can PULL them
# instead of building per-vehicle. Jetsons are arm64, so run this on an arm64 host (an Orin is
# perfect) for a native build, or cross-build from x86 with PLATFORM_FLAG (slow; needs qemu binfmt).
#
#   tools/build-images.sh [registry[:port]] [tag]
#     registry   your local registry, e.g. registry.lan:5000. Falls back to $RIG_IMAGE_REGISTRY (the
#                fleet registry rig injects), so rig's build phase sets ONE var for build + deploy.
#     tag        pushed EXACTLY as given: a bare platform (jp7 -- the legacy shape, and the default)
#                or a composed <version>-<platform> (v1.4.0-jp7 -- the fleet matrix shape; rig invokes
#                once per platform in `build.platforms`). The jp6/jp7 BUILD VARIANT (base image,
#                image set) comes from $RIG_TARGET_PLATFORM when set, else is derived from the tag
#                (bare platform or its -jp6/-jp7 suffix).
#
#   env knobs:
#     IMAGES="cam-core ros2-bridge webrtc-bridge"   subset to build (default: all for the variant)
#     BASE_IMAGE=...                 cam-core base; defaults from the tag (jp6 -> l4t-base, else ubuntu:24.04).
#                                    The NVIDIA/ubuntu base for the CORE image -- unrelated to RIG_BASE_IMAGE below.
#     ROS_DISTRO=lyrical             ros2-bridge ROS 2 distro
#     PUSH=1                         set 0 to build+tag locally without pushing
#     PLATFORM_FLAG=                 e.g. --platform=linux/arm64 to cross-build from x86
#
#   rig's build channel (all optional; rig sets-or-pops them, so a stale shell value can't leak in).
#   Absent/empty means "don't pass the flag at all" -> the Dockerfile's own default applies:
#     RIG_BASE_IMAGE=reg/fleet-ros:v1.3.0   the DEPLOYMENT's ONE base image (vehicle.yaml `images.base`,
#                                    or a service declaring `build: {..., provides: base}`). Passed VERBATIM
#                                    as ros2-bridge's BASE_IMAGE build-arg -- never re-tagged, never
#                                    suffixed with the platform: rig composed it already on the provider
#                                    side, and this is the consumer side. Only ros2-bridge consumes it --
#                                    it is the sole image here carrying a ROS/RMW apt stack (see below).
#     RIG_BUILD_NO_CACHE=1           `rig build --no-cache`: force a full rebuild of EVERY image here, the
#                                    remediation for a fleet whose images already drifted at the apt layer.
#     RIG_ROS_RMW=rmw_cyclonedds_cpp vehicle.yaml `ros.rmw` (rig >= v0.2.23): the rmw ros2-bridge installs,
#                                    as ros-<distro>-<name, '_' -> '-'> -- the same mapping `rig image
#                                    audit` uses to CHECK it, so builder and checker agree by
#                                    construction. Note it is deliberately NOT the conventional
#                                    RMW_IMPLEMENTATION: that name is exported in most ROS shells, and a
#                                    dev box's .bashrc must not decide what a fleet image contains. For
#                                    builds outside rig use CAM_ROS_RMW; RIG_ROS_RMW wins when both are set.
#                                    (Runtime selection is separate -- compose still honors
#                                    $RMW_IMPLEMENTATION, so a differently-baked image can still be
#                                    pointed at another rmw at `up` time.)
#
#   examples:
#     tools/build-images.sh registry.lan:5000                      # all three -> :jp7 (ubuntu:24.04), pushed
#     IMAGES="cam-core ros2-bridge" tools/build-images.sh registry.lan:5000
#     PUSH=0 tools/build-images.sh registry.lan:5000               # local build only
#     tools/build-images.sh registry.lan:5000 jp6                  # JP6 -> auto slim l4t-base, tag jp6
#     tools/build-images.sh registry.lan:5000 v1.4.0-jp6           # fleet matrix tag (variant from suffix)
#     RIG_TARGET_PLATFORM=jp6 tools/build-images.sh reg v1.4.0-jp6 # what `rig build` invokes per platform
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Registry from $1, else rig's RIG_IMAGE_REGISTRY (one var for build + deploy). PUSH=0 skips the push.
REGISTRY="${1:-${RIG_IMAGE_REGISTRY:-}}"
: "${REGISTRY:?usage: build-images.sh [registry[:port]] [tag], or set RIG_IMAGE_REGISTRY  (e.g. registry.lan:5000 jp7)}"
TAG="${2:-${RIG_TARGET_PLATFORM:-jp7}}"
# Build VARIANT (jp6 vs jp7) selects the cam-core base + the default image set: jp6 -> slim l4t-base +
# the Noetic ros1-bridge (ROS 1 is jp6-class); jp7 -> ubuntu:24.04, no ros1-bridge. RIG_TARGET_PLATFORM
# (rig's standard per-target platform var) wins; else derive from the tag -- a bare platform (legacy)
# or a composed <version>-<platform> suffix. The TAG is pushed verbatim either way. Override
# BASE_IMAGE / IMAGES individually as before.
VARIANT=""
case "${RIG_TARGET_PLATFORM:-}" in
  jp6|jp7) VARIANT="$RIG_TARGET_PLATFORM" ;;
  "") ;;
  *) echo "build-images: RIG_TARGET_PLATFORM='${RIG_TARGET_PLATFORM}' is not jp6|jp7; deriving the variant from the tag" >&2 ;;
esac
if [ -z "$VARIANT" ]; then
  case "$TAG" in
    jp6|*-jp6) VARIANT=jp6 ;;
    *)         VARIANT=jp7 ;;
  esac
fi
case "$VARIANT" in
  jp6) BASE_IMAGE="${BASE_IMAGE:-nvcr.io/nvidia/l4t-base:r36.2.0}"
       IMAGES="${IMAGES:-cam-core ros2-bridge ros1-bridge webrtc-bridge rtsp-bridge}" ;;
  *)   BASE_IMAGE="${BASE_IMAGE:-ubuntu:24.04}"
       IMAGES="${IMAGES:-cam-core ros2-bridge webrtc-bridge rtsp-bridge}" ;;
esac
ROS_DISTRO="${ROS_DISTRO:-lyrical}"
PUSH="${PUSH:-1}"
PLATFORM_FLAG="${PLATFORM_FLAG:-}"
# `${VAR:+...}` throughout: set -> the flag; unset/empty -> NOTHING, so the Dockerfile default stands.
NO_CACHE="${RIG_BUILD_NO_CACHE:-}"
RIG_BASE_IMAGE="${RIG_BASE_IMAGE:-}"
# rig's declared rmw > a standalone CAM_ROS_RMW > today's default. Passed explicitly (not `:+`) so the
# resolved choice is always visible in the build args; the Dockerfile carries the same default anyway.
RMW="${RIG_ROS_RMW:-${CAM_ROS_RMW:-rmw_zenoh_cpp}}"

REFS=()
build_one() {                      # build_one <image-name> <dockerfile> [extra docker build args...]
  local name="$1" dockerfile="$2"; shift 2
  local ref="$REGISTRY/$name:$TAG"
  echo "==> building $ref" >&2
  docker build ${NO_CACHE:+--no-cache} ${PLATFORM_FLAG:+$PLATFORM_FLAG} -f "$dockerfile" -t "$ref" "$@" .   # context = repo root
  if [ "$PUSH" = 1 ]; then echo "==> pushing  $ref" >&2; docker push "$ref"; fi
  REFS+=("$ref")
}

for img in $IMAGES; do
  case "$img" in
    cam-core)     build_one cam-core     core-driver/Dockerfile           --build-arg "BASE_IMAGE=$BASE_IMAGE" ;;
    # The ONLY image built FROM the deployment base, because it is the only one here carrying a ROS/RMW
    # apt stack -- so it is the only one that can drift from the fleet's other ROS containers.
    # cam-core (L4T/ubuntu + Aravis), webrtc-bridge and rtsp-bridge (ubuntu:24.04 + GStreamer) have no
    # /opt/ros at all, and ros1-bridge is ROS 1 Noetic; a ROS 2 base is not a valid FROM for any of them.
    ros2-bridge)   build_one ros2-bridge   plugins/ros2-bridge/Dockerfile   --build-arg "ROS_DISTRO=$ROS_DISTRO" \
                     --build-arg "RMW_IMPLEMENTATION=$RMW" \
                     ${RIG_BASE_IMAGE:+--build-arg "BASE_IMAGE=$RIG_BASE_IMAGE"} ;;
    ros1-bridge)   build_one ros1-bridge   plugins/ros1-bridge/Dockerfile ;;   # Noetic (ROS_DISTRO baked in)
    webrtc-bridge) build_one webrtc-bridge plugins/webrtc-bridge/Dockerfile ;;
    rtsp-bridge)   build_one rtsp-bridge   plugins/rtsp-bridge/Dockerfile ;;
    *) echo "build-images: unknown image '$img' (want: cam-core|ros2-bridge|ros1-bridge|webrtc-bridge|rtsp-bridge)" >&2; exit 1 ;;
  esac
done

echo
echo "built$([ "$PUSH" = 1 ] && echo ' + pushed') ${#REFS[@]} image(s):"
printf '  %s\n' "${REFS[@]}"
echo
echo "On a JP$([ "$VARIANT" = jp6 ] && echo 6 || echo 7) vehicle (after cloning the repo there):"
echo "  export CAM_REGISTRY=$REGISTRY"
echo "  ./cam-up config/sensors/<sensor>.yaml pull"
echo "  ./cam-up config/sensors/<sensor>.yaml up -d"
