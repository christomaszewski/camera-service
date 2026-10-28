# C++ rclcpp bridge: the core's transport (JP6 shm+header / JP7 unixfd) -> sensor_msgs/Image, built as
# composable components loaded into a component_container_mt. Multistage: a builder runs colcon, the
# runtime stage keeps only the runtime GStreamer/ROS deps + the built component lib (no -dev/toolchain).
#
# ROS 2 Lyrical Luth (LTS, May 2026 -> May 2031); distro-agnostic via ${ROS_DISTRO}.
# Build (from repo root):  docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
# Fallback to the prior LTS:  docker build --build-arg ROS_DISTRO=jazzy ...
ARG ROS_DISTRO=lyrical
# Rig supplies its resolved deployment base verbatim. Standalone builds retain the
# stock ROS parent; the core driver's BASE_IMAGE is a separate NVIDIA/Ubuntu input.
# Compile and run against the same parent so the component uses the fleet's ROS ABI.
ARG BASE_IMAGE=ros:${ROS_DISTRO}-ros-base
ARG ROS_RMW=rmw_zenoh_cpp

# ---- builder: colcon build the component library ----
FROM ${BASE_IMAGE} AS builder
ARG ROS_DISTRO
ARG ROS_RMW
ENV DEBIAN_FRONTEND=noninteractive
# --no-upgrade: same parent-is-the-version-authority rule as the runtime stage below (this stage
# doesn't ship, but keeping the builder's ROS packages at the parent's versions is consistent).
RUN rmw_pkg="ros-${ROS_DISTRO}-$(echo "${ROS_RMW}" | tr '_' '-')" \
    && apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
      build-essential cmake pkg-config \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-rclcpp-components \
      "${rmw_pkg}" \
      python3-colcon-common-extensions \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ws
COPY plugins/ros2-bridge /ws/src/cam_ros2_bridge
# ros2-source (plugins/ros2-source): the Python writer that feeds a shm-input instance from a topic --
# same image, its own compose profile.
COPY plugins/ros2-source /ws/src/cam_ros2_source
# colcon-common-extensions carries colcon-ros: without it an ament_python package builds as a plain
# setuptools package -- importable, but with no AMENT_PREFIX_PATH hook, so `ros2 run` cannot find it.
# CMake also consults RMW_IMPLEMENTATION. Override any parent runtime default for
# this command only, using the middleware installed above.
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && \
    RMW_IMPLEMENTATION="${ROS_RMW}" colcon build --packages-select cam_ros2_bridge cam_ros2_source --cmake-args -DCMAKE_BUILD_TYPE=Release

# ---- runtime: shared ROS base + camera-specific deps + the built component library ----
FROM ${BASE_IMAGE} AS runtime
ARG ROS_DISTRO
ARG ROS_RMW
ENV DEBIAN_FRONTEND=noninteractive
# plugins-bad provides `shmsrc` (JP6 transport) and `bayer2rgb` (JP7 in-pipeline debayer); on 24.04 it
# also ships the GStreamer `onnx` element (libgstonnx -> onnxruntime), which we never use and whose arm64
# init misbehaves ("Unknown CPU Vendor" CPUID probe + in-process heap corruption). Strip it so it can't load.
# rclcpp-components -> component_container_mt; image-proc -> the DebayerNode composed on JP6 color.
# python3-gi / python3-gst-1.0 / gir1.2-gst-plugins-base-1.0 -> GStreamer from Python for ros2-source
# (the FdAllocator typelib for its unixfd framing rides in gst-plugins-base).
# Install the fleet's declared RMW (Zenoh by default). Runtime selection remains the
# conventional RMW_IMPLEMENTATION environment variable, independent of this build arg.
#
# --no-upgrade preserves explicitly requested packages already installed in the parent.
# Dependency resolution can still upgrade other packages: validate fleet builds with
# `rig image audit` rather than treating this flag as a complete version lock.
RUN rmw_pkg="ros-${ROS_DISTRO}-$(echo "${ROS_RMW}" | tr '_' '-')" \
    && apt-get update && apt-get install -y --no-install-recommends --no-upgrade \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-compressed-image-transport \
      ros-${ROS_DISTRO}-rclcpp-components ros-${ROS_DISTRO}-image-proc \
      "${rmw_pkg}" \
      python3-gi python3-gst-1.0 gir1.2-gst-plugins-base-1.0 \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /ws/install /ws/install
COPY plugins/ros2-bridge/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
# Default to the env-driven launch composition (cam-up exports CAM_PLATFORM/TRANSPORT/INSTANCE/...).
CMD ["ros2", "launch", "cam_ros2_bridge", "bridge.launch.py"]
