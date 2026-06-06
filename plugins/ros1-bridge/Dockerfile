# ROS 1 Noetic bridge: the core's shm + 36-byte header transport -> sensor_msgs/Image. The ROS 1 mirror of
# plugins/ros2-bridge, but ROS 1 / catkin. Noetic is the last ROS 1 release (Ubuntu 20.04 / GStreamer 1.16),
# so this reads the shm+header transport only -- GStreamer 1.16 has no unixfd (a JP6-class core).
# Multistage: a builder runs catkin, the runtime stage keeps only runtime deps + the built node.
#
# Build (from repo root):  docker build -f plugins/ros1-bridge/Dockerfile -t ros1-bridge .
ARG ROS_DISTRO=noetic

# ---- builder: catkin build the node ----
FROM ros:${ROS_DISTRO}-ros-base AS builder
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      ros-${ROS_DISTRO}-image-transport \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ws
COPY plugins/ros1-bridge /ws/src/cam_ros1_bridge
RUN . /opt/ros/${ROS_DISTRO}/setup.sh && catkin_make install -DCMAKE_BUILD_TYPE=Release

# ---- runtime: ros-base + runtime GStreamer/ROS + the built node ----
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
ENV DEBIAN_FRONTEND=noninteractive
# plugins-bad provides shmsrc (+ bayer2rgb); image-proc/nodelet -> the optional debayer in the launch.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      ros-${ROS_DISTRO}-image-transport ros-${ROS_DISTRO}-compressed-image-transport \
      ros-${ROS_DISTRO}-image-proc ros-${ROS_DISTRO}-nodelet \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /ws/install /ws/install
COPY plugins/ros1-bridge/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["roslaunch", "cam_ros1_bridge", "bridge.launch"]
