"""Compose the cam ros2-bridge per platform, driven by the env cam-up/sensor_env already export.

One component container (`component_container_mt`, intra-process comms on) loads the transport-specific
bridge component:
  - JP7  -> CamUnixfdBridge  (header-free unixfd transport; debayer is done in its GStreamer pipeline)
  - JP6  -> CamHeaderBridge  (legacy shm + 36-byte header). When debayer is requested on a CFA camera,
            image_proc::DebayerNode is loaded into the SAME container so the bayer frame is shared
            intra-process (zero-copy) and debayered to <ns>/image_color.

Transport selection: CAM_TRANSPORT ({unixfd|header}) wins if set, else unixfd iff CAM_PLATFORM=jp7.
This must match what the core driver does (it exposes unixfd exactly when GStreamer >= 1.24 / JP7).
"""
import os

from launch import LaunchDescription
from launch.actions import SetEnvironmentVariable, UnsetEnvironmentVariable
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _zenoh_env_actions():
    """Fold the friendly zenoh knobs into ZENOH_CONFIG_OVERRIDE for the container process.

    rmw_zenoh applies ZENOH_CONFIG_OVERRIDE (`path/to/key=value;...`, in order, last wins) ON TOP of
    the config it already loaded -- so every rmw_zenoh default we don't name survives. That's why the
    knobs deliberately do NOT go through ZENOH_SESSION_CONFIG_URI: a config URI *replaces* the whole
    default session config, silently dropping everything it doesn't restate.

    Friendly knobs (all optional; sensor_env maps the lowercase ros2-bridge params to these):
      CAM_ZENOH_SHM            true/false -> transport/shared_memory/{,transport_optimization/}enabled.
      CAM_ZENOH_SHM_POOL_SIZE  bytes for the SHM segment (rmw_zenoh default 48 MiB). Implies SHM on
                               unless CAM_ZENOH_SHM says otherwise.
      CAM_ZENOH_SHM_MSG_THRESHOLD  min message size (bytes, power of two) routed through SHM
                               (rmw_zenoh default 512). Also implies SHM on.
    A pre-set ZENOH_CONFIG_OVERRIDE (raw `path=value;...` escape hatch, e.g. straight from sensor
    YAML) is appended LAST, so an explicit raw override beats the friendly knobs on conflicts.
    """
    shm = os.environ.get("CAM_ZENOH_SHM", "").strip()
    pool = os.environ.get("CAM_ZENOH_SHM_POOL_SIZE", "").strip()
    threshold = os.environ.get("CAM_ZENOH_SHM_MSG_THRESHOLD", "").strip()
    frags = []
    if shm or pool or threshold:
        on = "true" if (_truthy(shm) if shm else True) else "false"
        frags += [f"transport/shared_memory/enabled={on}",
                  f"transport/shared_memory/transport_optimization/enabled={on}"]
    if pool:
        frags.append(f"transport/shared_memory/transport_optimization/pool_size={pool}")
    if threshold:
        frags.append(f"transport/shared_memory/transport_optimization/message_size_threshold={threshold}")
    raw = os.environ.get("ZENOH_CONFIG_OVERRIDE", "").strip()
    if raw:
        frags.append(raw)
    merged = ";".join(frags)
    actions = [SetEnvironmentVariable("ZENOH_CONFIG_OVERRIDE", merged)] if merged else []
    # compose exports the passthrough vars as '' when unset; drop the empty ones (any we didn't just
    # set) so rmw_zenoh never sees an empty value where it expects a parseable one.
    for k in ("ZENOH_CONFIG_OVERRIDE", "ZENOH_SESSION_CONFIG_URI", "RMW_ZENOH_BUFFER_POOL_MAX_SIZE_BYTES"):
        if k in os.environ and not os.environ[k].strip() and not (merged and k == "ZENOH_CONFIG_OVERRIDE"):
            actions.append(UnsetEnvironmentVariable(k))
    return actions


def generate_launch_description():
    platform = os.environ.get("CAM_PLATFORM", "jp6").strip().lower()
    transport = os.environ.get("CAM_TRANSPORT", "").strip().lower()
    if transport not in ("unixfd", "header"):
        transport = "unixfd" if platform == "jp7" else "header"
    unixfd = transport == "unixfd"

    instance = os.environ.get("CAM_INSTANCE", "camera").strip()
    ns = "/" + instance.lstrip("/")
    topic = os.environ.get("CAM_ROS_TOPIC", "image_raw").strip()
    frame_id = os.environ.get("CAM_FRAME_ID", instance).strip()
    encoding = os.environ.get("CAM_ROS_ENCODING", "").strip()   # bayer_* for a CFA camera, "" for mono
    debayer = _truthy(os.environ.get("CAM_DEBAYER", "false"))
    publish_rate = float(os.environ.get("CAM_ROS_PUBLISH_RATE", "").strip() or 0)  # Hz; 0 = every frame
    default_sock = "/tmp/cam/unixfd" if unixfd else "/tmp/cam/frames"
    socket_path = os.environ.get("CAM_TRANSPORT_SOCKET", default_sock).strip()

    plugin = ("cam_ros2_bridge::CamUnixfdBridge" if unixfd
              else "cam_ros2_bridge::CamHeaderBridge")

    # Intra-process comms is only useful when there's an in-process subscriber to share the buffer with
    # -- i.e. JP6 + debayer, where image_proc::DebayerNode is composed alongside the bridge. Enabling it
    # otherwise is pointless and, with image_transport under rmw_zenoh, suppresses inter-process delivery
    # of the bridge's own topic. So gate it on the composition. (image_proc still publishes image_color
    # inter-process for external subscribers.)
    compose_image_proc = debayer and not unixfd and encoding.startswith("bayer_")
    ipc = [{"use_intra_process_comms": compose_image_proc}]

    nodes = [ComposableNode(
        package="cam_ros2_bridge", plugin=plugin, name="cam_ros2_bridge", namespace=ns,
        parameters=[{"socket_path": socket_path, "topic": topic, "frame_id": frame_id,
                     "encoding": encoding, "debayer": debayer, "publish_rate": publish_rate}],
        extra_arguments=ipc)]

    # JP6 + debayer on a CFA camera: compose image_proc::DebayerNode in-process. It subscribes to
    # `image_raw` (remapped to our topic if different) and publishes <ns>/image_color. JP7 debayers in
    # the GStreamer pipeline instead, so no image_proc node there.
    if compose_image_proc:
        remaps = [] if topic == "image_raw" else [("image_raw", topic)]
        nodes.append(ComposableNode(
            package="image_proc", plugin="image_proc::DebayerNode", name="debayer", namespace=ns,
            remappings=remaps, extra_arguments=[{"use_intra_process_comms": True}]))

    # The env actions must precede the container action: they mutate the launch process env, which the
    # container process inherits at spawn.
    return LaunchDescription(_zenoh_env_actions() + [ComposableNodeContainer(
        name="cam_bridge_container", namespace=ns,
        package="rclcpp_components", executable="component_container_mt",
        composable_node_descriptions=nodes, output="screen")])
