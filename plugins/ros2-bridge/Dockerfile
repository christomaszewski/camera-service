# C++ rclcpp bridge: shm `application/x-gige-frame` -> sensor_msgs/Image. Multistage: a builder runs
# colcon, the runtime stage keeps only the runtime GStreamer + the built node (no build-essential/-dev).
#
# ROS 2 Lyrical Luth (LTS, May 2026 -> May 2031); distro-agnostic via ${ROS_DISTRO}.
# Build (from repo root):  docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
# Fallback to the prior LTS:  docker build --build-arg ROS_DISTRO=jazzy ...
ARG ROS_DISTRO=lyrical

# ---- builder: colcon build the C++ node ----
FROM ros:${ROS_DISTRO}-ros-base AS builder
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      ros-${ROS_DISTRO}-image-transport \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ws
COPY plugins/ros2-bridge /ws/src/gige_ros2_bridge
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && \
    colcon build --packages-select gige_ros2_bridge --cmake-args -DCMAKE_BUILD_TYPE=Release

# ---- runtime: ros-base + runtime GStreamer + the built node ----
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
# plugins-bad is here only for `shmsrc`; on 24.04 it also ships the GStreamer `onnx` element
# (libgstonnx -> onnxruntime), which we never use and whose arm64 init misbehaves ("Unknown CPU Vendor"
# CPUID probe + in-process heap corruption). Strip the plugin + the heavy lib so it's never loaded.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-compressed-image-transport \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /ws/install /ws/install
COPY plugins/ros2-bridge/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["ros2", "run", "gige_ros2_bridge", "gige_ros2_bridge"]
