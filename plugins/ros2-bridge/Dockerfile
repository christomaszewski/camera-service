# C++ rclcpp bridge: shm `application/x-gige-frame` -> sensor_msgs/Image.
#
# ROS 2 Lyrical Luth (LTS, May 2026 -> May 2031). GStreamer dev + the shm plugin come
# from apt. The node is a normal ROS 2 graph member (default DDS RMW or rmw_zenoh).
# Everything below is distro-agnostic via ${ROS_DISTRO}; only this FROM pins the distro.
#
# Build (from repo root):  docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
# Fallback to the prior LTS:  docker build --build-arg ROS_DISTRO=jazzy ...
ARG ROS_DISTRO=lyrical
FROM ros:${ROS_DISTRO}-ros-base

ENV DEBIAN_FRONTEND=noninteractive
# plugins-bad is here only for `shmsrc`. On 24.04 it also ships the GStreamer `onnx` inference element
# (libgstonnx -> onnxruntime) which we never use; GStreamer's registry scan loads it anyway and its
# arm64 init misbehaves ("Unknown CPU Vendor" CPUID probe + a prime suspect for the in-process heap
# corruption). Strip the plugin (and the heavy onnxruntime lib it pulls in) so it's never loaded.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-compressed-image-transport \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ws
COPY plugins/ros2-bridge /ws/src/gige_ros2_bridge
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && \
    colcon build --packages-select gige_ros2_bridge --cmake-args -DCMAKE_BUILD_TYPE=Release

COPY plugins/ros2-bridge/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["ros2", "run", "gige_ros2_bridge", "gige_ros2_bridge"]
