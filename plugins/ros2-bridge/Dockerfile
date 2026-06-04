# C++ rclcpp bridge: the core's transport (JP6 shm+header / JP7 unixfd) -> sensor_msgs/Image, built as
# composable components loaded into a component_container_mt. Multistage: a builder runs colcon, the
# runtime stage keeps only the runtime GStreamer/ROS deps + the built component lib (no -dev/toolchain).
#
# ROS 2 Lyrical Luth (LTS, May 2026 -> May 2031); distro-agnostic via ${ROS_DISTRO}.
# Build (from repo root):  docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
# Fallback to the prior LTS:  docker build --build-arg ROS_DISTRO=jazzy ...
ARG ROS_DISTRO=lyrical

# ---- builder: colcon build the component library ----
FROM ros:${ROS_DISTRO}-ros-base AS builder
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-rclcpp-components \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ws
COPY plugins/ros2-bridge /ws/src/gige_ros2_bridge
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && \
    colcon build --packages-select gige_ros2_bridge --cmake-args -DCMAKE_BUILD_TYPE=Release

# ---- runtime: ros-base + runtime GStreamer/ROS + the built component lib ----
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
# plugins-bad provides `shmsrc` (JP6 transport) and `bayer2rgb` (JP7 in-pipeline debayer); on 24.04 it
# also ships the GStreamer `onnx` element (libgstonnx -> onnxruntime), which we never use and whose arm64
# init misbehaves ("Unknown CPU Vendor" CPUID probe + in-process heap corruption). Strip it so it can't load.
# rclcpp-components -> component_container_mt; image-proc -> the DebayerNode composed on JP6 color.
# rmw-zenoh-cpp -> the default ROS 2 middleware (RMW_IMPLEMENTATION=rmw_zenoh_cpp); it ships the
# `rmw_zenohd` router too, so this same image runs the shared host router. FastDDS stays available
# (ros-base ships rmw_fastrtps_cpp) as a selectable fallback via RMW_IMPLEMENTATION.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-compressed-image-transport \
      ros-${ROS_DISTRO}-rclcpp-components ros-${ROS_DISTRO}-image-proc \
      ros-${ROS_DISTRO}-rmw-zenoh-cpp \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /ws/install /ws/install
COPY plugins/ros2-bridge/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
# Default to the env-driven launch composition (gige-up exports GIGE_PLATFORM/TRANSPORT/INSTANCE/...).
CMD ["ros2", "launch", "gige_ros2_bridge", "bridge.launch.py"]
