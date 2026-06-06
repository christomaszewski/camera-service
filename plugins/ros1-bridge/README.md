# cam_ros1_bridge

The **ROS 1 (Noetic)** mirror of the [ros2-bridge](../ros2-bridge/README.md): a `roscpp` node that
consumes the core's `application/x-cam-frame` shm endpoint and republishes each frame as
`sensor_msgs/Image`, stamping `header.stamp` from the per-frame hardware (PTP) timestamp in the 36-byte
header. The header contract is [`core-driver/cam_driver/transport.py`](../../core-driver/cam_driver/transport.py)
— the same struct the ros2 bridge parses.

## What mirrors, and the ROS 1 boundary

Noetic is the last ROS 1 release — **Ubuntu 20.04 / GStreamer 1.16** — which sets hard limits:

| | ros2-bridge | ros1-bridge |
|---|---|---|
| transport | unixfd (JP7) **or** shm+header (JP6) | **shm+header only** (GStreamer 1.16 has no unixfd) |
| core | JP7 or JP6 | **JP6-class** (the JP6 image runs on a JP7 host too, minus HW NVENC) |
| graph | DDS / **rmw_zenoh** + a shared `rmw_zenohd` router | TCPROS + a shared **`roscore`** (`tools/roscore.sh` / `cam-up --roscore`) |
| structure | rclcpp composable components | a plain `roscpp` node (catkin) |
| debayer | composed `image_proc::DebayerNode` | a `nodelet standalone image_proc/debayer` (launch) |
| timestamp, encoding, compressed | header.stamp = PTP; bayer_*/mono; lazy `<topic>/compressed` | **same** |

So a ROS 1 deployment pairs with a **JP6-class core** (shm+header). On a JP7 Orin you get ROS 1 by running
the **JP6 stack** (`cam-up --jp6`) — its GStreamer 1.20 publishes shm+header and skips unixfd; the
tradeoff is software (FFV1) recording instead of HW HEVC-lossless on that camera.

## Parameters

| param | default | meaning |
|---|---|---|
| `socket_path` | `/tmp/cam/frames` | the core's shm+header endpoint |
| `topic` | `image_raw` | output `sensor_msgs/Image` topic |
| `frame_id` | `camera` | `header.frame_id` (TF frame) |
| `encoding` | `""` (`$CAM_ROS_ENCODING`) | Bayer label (e.g. `bayer_rggb8`); empty = `mono8`/`mono16` from the header. Auto-set by `cam-up` from `camera.pixel_format`. |

Color: the node publishes the raw mosaic labeled `bayer_*` (option A). `CAM_DEBAYER=true` makes the
launch additionally run a standalone `image_proc/debayer` nodelet → `<ns>/image_color`.

## Run

```bash
docker build -f plugins/ros1-bridge/Dockerfile -t ros1-bridge .
tools/roscore.sh up                                  # one shared roscore per host
docker run --rm --network host --ipc=host -v cam_sock:/tmp/cam \
  -e CAM_INSTANCE=cam_a -e ROS_MASTER_URI=http://localhost:11311 ros1-bridge
```

`--ipc=host` is **required** — the shm transport's frame data lives in `/dev/shm` (only the control socket
is in the volume). `--network host` joins the ROS 1 graph (TCPROS needs reachable peers + the master).
