# C++ rclcpp bridge: the core's transport (JP6 shm+header / JP7 unixfd) -> sensor_msgs/Image, built as
# composable components loaded into a component_container_mt. Multistage: a builder runs colcon, the
# runtime stage keeps only the runtime GStreamer/ROS deps + the built component lib (no -dev/toolchain).
#
# ROS 2 Lyrical Luth (LTS, May 2026 -> May 2031); distro-agnostic via ${ROS_DISTRO}.
# Build (from repo root):  docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
# Fallback to the prior LTS:  docker build --build-arg ROS_DISTRO=jazzy ...
ARG ROS_DISTRO=lyrical
# The image BOTH stages build FROM. The default is the stock ROS base, so a plain `docker build` (and
# this repo's CI) needs no rig, no registry, no fleet -- and `--build-arg ROS_DISTRO=jazzy` still
# selects ros:jazzy-ros-base, because an ARG default interpolates an ARG declared before it.
#
# rig overrides it. `rig build` exports RIG_BASE_IMAGE -- the DEPLOYMENT's one base image (vehicle.yaml
# `images.base`, or a service declaring `build: {…, provides: base}`, e.g. rig-infra's fleet-ros) --
# and tools/build-images.sh maps it onto this arg VERBATIM: never re-tagged, never suffixed with
# -$CAM_PLATFORM. It is a CONSUMER input; rig already did any composition on the provider side, and it
# builds the base FIRST, so the ref exists by the time this build runs.
#
# Why it matters: every ROS container in the vehicle then shares ONE apt-level ROS/RMW layer. Two
# images apt-installing the same ROS packages independently, at different times, against a moving ros
# repo is how a fleet ends up with two different `rmw_zenoh_cpp` builds whose sessions cannot talk --
# the failure `rig image audit` reports as `version skew: ros-<distro>-rmw-zenoh-cpp`.
ARG BASE_IMAGE=ros:${ROS_DISTRO}-ros-base
# The rmw to install, as its ROS name. tools/build-images.sh sources it from RIG_ROS_RMW (vehicle.yaml
# `ros.rmw`, rig >= v0.2.23); the default only applies outside rig. The apt package is
# ros-<distro>-<name, '_' -> '-'> -- the SAME mapping `rig image audit` uses to check the declared rmw
# is installed, so the builder and the checker agree by construction rather than by coincidence.
# Hardcoding zenoh here is what makes a non-zenoh fleet build an image whose nodes silently run a
# different middleware than the rest of the graph. (Same arg name and mapping as rig-infra's fleet-ros.)
ARG RMW_IMPLEMENTATION=rmw_zenoh_cpp

# ---- builder: colcon build the component library ----
# Same base as the runtime stage on purpose: the component links against that layer's
# rclcpp/image-transport, so compiling it against a DIFFERENT ROS layer than it runs on would
# reintroduce, at the ABI level, exactly the skew this base image exists to remove.
FROM ${BASE_IMAGE} AS builder
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
# --no-upgrade: see the runtime stage below -- compile against the ROS versions the base PINS, not
# against whatever the ros repo serves today, or the component links to a different rclcpp than it runs on.
RUN apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
      build-essential cmake pkg-config \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-rclcpp-components \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ws
COPY plugins/ros2-bridge /ws/src/cam_ros2_bridge
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && \
    colcon build --packages-select cam_ros2_bridge --cmake-args -DCMAKE_BUILD_TYPE=Release

# ---- runtime: the base + camera-specific GStreamer/ROS deps + the built component lib ----
FROM ${BASE_IMAGE}
ARG ROS_DISTRO
ARG RMW_IMPLEMENTATION
ENV DEBIAN_FRONTEND=noninteractive
# plugins-bad provides `shmsrc` (JP6 transport) and `bayer2rgb` (JP7 in-pipeline debayer); on 24.04 it
# also ships the GStreamer `onnx` element (libgstonnx -> onnxruntime), which we never use and whose arm64
# init misbehaves ("Unknown CPU Vendor" CPUID probe + in-process heap corruption). Strip it so it can't load.
# rclcpp-components -> component_container_mt; image-proc -> the DebayerNode composed on JP6 color.
# The rmw package is ${RMW_IMPLEMENTATION} (default rmw_zenoh_cpp, matching the compose default); on a
# zenoh fleet it ships the `rmw_zenohd` router too, so this same image runs the shared host router.
# FastDDS stays available (ros-base ships rmw_fastrtps_cpp) as a selectable fallback at RUNTIME via the
# conventional RMW_IMPLEMENTATION env var, which is independent of what is baked in here.
#
# --no-upgrade is what makes the shared base actually WORK, and it is easy to leave off by accident:
# `apt-get install <pkg>` on a package the base ALREADY carries silently UPGRADES it to whatever the
# ros repo serves today, so the image drifts off the base within one layer -- the skew comes straight
# back, just under a different package name. (Measured: without it, a fleet-ros base carrying
# rclcpp-components …20260731 got …20260812 installed over it here, and `rig image audit` flagged
# `version skew: ros-<distro>-rclcpp-components`.) With it, anything the base pins stays exactly as
# the base pinned it, and anything the base LACKS is still installed -- which is what keeps a plain
# rig-less `docker build` on stock ros-base (no zenoh, no image-proc) a complete, working image.
RUN rmw_pkg="ros-${ROS_DISTRO}-$(echo "${RMW_IMPLEMENTATION}" | tr '_' '-')" \
    && apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-compressed-image-transport \
      ros-${ROS_DISTRO}-rclcpp-components ros-${ROS_DISTRO}-image-proc \
      "${rmw_pkg}" \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /ws/install /ws/install
COPY plugins/ros2-bridge/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
# Default to the env-driven launch composition (cam-up exports CAM_PLATFORM/TRANSPORT/INSTANCE/...).
CMD ["ros2", "launch", "cam_ros2_bridge", "bridge.launch.py"]
