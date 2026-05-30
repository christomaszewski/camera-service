# C++ rclcpp bridge: shm `application/x-gige-frame` -> sensor_msgs/Image.
#
# ROS 2 Jazzy (Ubuntu 24.04). GStreamer dev + the shm plugin come from apt. The node
# is a normal ROS 2 graph member, so it works with the default DDS RMW or rmw_zenoh.
#
# Build (from repo root):  docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
FROM ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-compressed-image-transport \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ws
COPY plugins/ros2-bridge /ws/src/gige_ros2_bridge
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && \
    colcon build --packages-select gige_ros2_bridge --cmake-args -DCMAKE_BUILD_TYPE=Release

COPY plugins/ros2-bridge/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["ros2", "run", "gige_ros2_bridge", "gige_ros2_bridge"]
