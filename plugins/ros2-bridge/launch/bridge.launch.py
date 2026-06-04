"""Compose the gige ros2-bridge per platform, driven by the env gige-up/sensor_env already export.

One component container (`component_container_mt`, intra-process comms on) loads the transport-specific
bridge component:
  - JP7  -> GigeUnixfdBridge  (header-free unixfd transport; debayer is done in its GStreamer pipeline)
  - JP6  -> GigeHeaderBridge  (legacy shm + 36-byte header). When debayer is requested on a CFA camera,
            image_proc::DebayerNode is loaded into the SAME container so the bayer frame is shared
            intra-process (zero-copy) and debayered to <ns>/image_color.

Transport selection: GIGE_TRANSPORT ({unixfd|header}) wins if set, else unixfd iff GIGE_PLATFORM=jp7.
This must match what the core driver does (it exposes unixfd exactly when GStreamer >= 1.24 / JP7).
"""
import os

from launch import LaunchDescription
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def generate_launch_description():
    platform = os.environ.get("GIGE_PLATFORM", "jp6").strip().lower()
    transport = os.environ.get("GIGE_TRANSPORT", "").strip().lower()
    if transport not in ("unixfd", "header"):
        transport = "unixfd" if platform == "jp7" else "header"
    unixfd = transport == "unixfd"

    instance = os.environ.get("GIGE_INSTANCE", "camera").strip()
    ns = "/" + instance.lstrip("/")
    topic = os.environ.get("GIGE_ROS_TOPIC", "image_raw").strip()
    frame_id = os.environ.get("GIGE_FRAME_ID", instance).strip()
    encoding = os.environ.get("GIGE_ROS_ENCODING", "").strip()   # bayer_* for a CFA camera, "" for mono
    debayer = _truthy(os.environ.get("GIGE_DEBAYER", "false"))
    default_sock = "/tmp/gige/unixfd" if unixfd else "/tmp/gige/frames"
    socket_path = os.environ.get("GIGE_TRANSPORT_SOCKET", default_sock).strip()

    plugin = ("gige_ros2_bridge::GigeUnixfdBridge" if unixfd
              else "gige_ros2_bridge::GigeHeaderBridge")

    # Intra-process comms is only useful when there's an in-process subscriber to share the buffer with
    # -- i.e. JP6 + debayer, where image_proc::DebayerNode is composed alongside the bridge. Enabling it
    # otherwise is pointless and, with image_transport under rmw_zenoh, suppresses inter-process delivery
    # of the bridge's own topic. So gate it on the composition. (image_proc still publishes image_color
    # inter-process for external subscribers.)
    compose_image_proc = debayer and not unixfd and encoding.startswith("bayer_")
    ipc = [{"use_intra_process_comms": compose_image_proc}]

    nodes = [ComposableNode(
        package="gige_ros2_bridge", plugin=plugin, name="gige_ros2_bridge", namespace=ns,
        parameters=[{"socket_path": socket_path, "topic": topic, "frame_id": frame_id,
                     "encoding": encoding, "debayer": debayer}],
        extra_arguments=ipc)]

    # JP6 + debayer on a CFA camera: compose image_proc::DebayerNode in-process. It subscribes to
    # `image_raw` (remapped to our topic if different) and publishes <ns>/image_color. JP7 debayers in
    # the GStreamer pipeline instead, so no image_proc node there.
    if compose_image_proc:
        remaps = [] if topic == "image_raw" else [("image_raw", topic)]
        nodes.append(ComposableNode(
            package="image_proc", plugin="image_proc::DebayerNode", name="debayer", namespace=ns,
            remappings=remaps, extra_arguments=[{"use_intra_process_comms": True}]))

    return LaunchDescription([ComposableNodeContainer(
        name="gige_bridge_container", namespace=ns,
        package="rclcpp_components", executable="component_container_mt",
        composable_node_descriptions=nodes, output="screen")])
