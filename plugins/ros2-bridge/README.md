# gige_ros2_bridge

A C++ `rclcpp` node that consumes the core's `application/x-gige-frame` shm endpoint and
republishes each frame as `sensor_msgs/Image`, stamping `header.stamp` from the per-frame
hardware (PTP) timestamp carried in the 36-byte header. It's a normal ROS 2 graph member,
so it works with the default DDS RMW or `rmw_zenoh`.

The wire-format contract is [`core-driver/gige_driver/transport.py`](../../core-driver/gige_driver/transport.py);
the C++ `FrameHeader` mirrors it exactly, guarded by a `static_assert` on the 36-byte size.

## Parameters

| param | default | meaning |
|---|---|---|
| `socket_path` | `/tmp/gige/frames` | the core's plugin shm endpoint |
| `topic` | `image_raw` | output `sensor_msgs/Image` topic |
| `frame_id` | `camera` | `header.frame_id` (TF frame) |
| `encoding` | `""` | override ROS encoding; empty = derive from pixfmt (`mono8`/`mono16`). Set e.g. `bayer_rggb8` for a raw-Bayer source. |

## Compressed images

The node publishes through `image_transport`, so alongside the raw `<topic>` you automatically
get `<topic>/compressed` — a `sensor_msgs/CompressedImage` (JPEG for 8-bit, PNG for 16-bit)
provided by `compressed_image_transport`. It's **lazy**: no CPU is spent compressing unless
something subscribes to the compressed topic. Tune it with the standard image_transport
compressed params (`...compressed.jpeg_quality`, `...compressed.format=png` for 16-bit, etc.).

Verified end-to-end: a 512×512 `mono8` frame (262144 B raw) → ~35 KB JPEG on
`/image_raw/compressed` at ~25 Hz, capture timestamp preserved in the header.

## Build & run

```bash
docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
docker run --rm --ipc=host -v gige_sock:/tmp/gige ros2-bridge \
  ros2 run gige_ros2_bridge gige_ros2_bridge --ros-args -p topic:=/image_raw
```

The bridge reaches the core's shm via `--ipc=host` (shared `/dev/shm`) + a shared socket
volume when in its own container — or, in the per-sensor-container model, simply runs as a
sibling process in the same container (shm is then free).

## End-to-end test (no Jetson)

```bash
./plugins/ros2-bridge/tools/bridge_test.sh
```

Runs the dev producer + bridge and checks the `/image_raw` rate (~25 Hz) and that the
`Image` header carries the capture timestamp + correct geometry/encoding. Confirmed working
across containers and across GStreamer 1.20 (producer) ↔ 1.24 (bridge).
