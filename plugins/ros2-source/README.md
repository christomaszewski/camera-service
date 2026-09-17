# ros2-source

A ROS 2 **image topic feeding a camera-service instance** — the reverse of [ros2-bridge](../ros2-bridge).
`sensor_msgs/Image` (or `CompressedImage`, jpeg / png) in; the instance's shm input out
([docs/TRANSPORT.md](../../docs/TRANSPORT.md) "Input"), so a topic published by anything on the graph
— another vehicle's bridge, a simulator, a perception node's debug image — becomes a camera:
recorded, previewed over WebRTC, discoverable, replayable.

```
topic ──rclpy──▶ [convert only if needed: videoconvert/videoscale, jpegdec/pngdec] ──▶ shm.framing:
                                                                                      raw     shmsink, pinned video/x-raw
                                                                                      header  36-byte header + pixels, shmsink
                                                                                      unixfd  memfd buffers, native caps, unixfdsink
```

## Configure

The instance is a normal `camera.type: shm` config; the plugin rides its `shm:` block
([core-driver/config/sensors/cam_ros2.yaml](../../core-driver/config/sensors/cam_ros2.yaml)):

```yaml
camera: { type: shm, frame_rate: 15 }
shm:
  socket_path: /tmp/cam/in
  framing: header            # raw | header | unixfd -- what THIS node writes (sensor_env passes it on)
  pixel_format: RGB          # PINNED: every frame is converted to this; a Bayer label keeps the mosaic
  width: 640
  height: 480
plugins:
  - name: ros2-source
    isolation: container
    params:
      topic: /front/image_raw   # absolute; `compressed: true` subscribes CompressedImage instead
      # qos: sensor             # sensor (best effort, default) | reliable
```

`sensor_env` turns that into `CAM_SOURCE_*` for the container (topic, transport = framing, socket,
pinned format + geometry + fps, compressed, qos); nothing is configured twice.

## Behaviour

- **Conversion is by need.** A message that already is the pinned format and geometry is pushed as-is
  (rows unpadded when `step` carries padding). Anything else goes through
  `videoconvert ! videoscale` (and `jpegdec` / `pngdec` for CompressedImage) to the pinned caps.
  Supported encodings: rgb8, bgr8, rgba8, bgra8, mono8, mono16 (either endianness), yuv422,
  yuv422_yuy2, uyvy, nv12, nv24, bayer_{rggb,bggr,gbrg,grbg}8. Others are dropped and logged once.
- **Stamps.** `header.stamp` is the frame's capture time (`camera` provenance in the header / the
  unixfd `offset_end`); a zero stamp means arrival (`system`). With `raw` framing the core stamps on
  arrival regardless. Frame ids are minted here, monotonic from 1.
- **Bayer.** A pinned `BayerRG8` (etc.) keeps the mosaic: bytes travel as GRAY8 (header / raw) or as
  `video/x-bayer,format=rggb` (unixfd); a `bayer_*8` topic of the same pattern passes through, a
  color topic cannot be turned into a mosaic (dropped, logged).
- **unixfd** needs GStreamer ≥ 1.24 in the core too (JP7); on JP6 the core refuses it at open with the
  reason — use `header`, which carries the same stamps. The node unlinks a stale socket before binding.
- **Restarts.** The node owns the socket; the core reconnects when it restarts (and vice versa). An
  output pipeline error exits the container (compose restarts it).

## Image

Built into the ros2-bridge image (`plugins/ros2-bridge/Dockerfile`): `python3-gi`, `python3-gst-1.0`,
`gir1.2-gst-plugins-base-1.0` (the FdAllocator for unixfd) and this package in the colcon workspace.
The compose service (`compose.yml`, profile `ros2-source`) is included by the top-level
`docker-compose.yml` like every heavy plugin; it runs the node as `python3 -m cam_ros2_source.node`
(`ros2 run cam_ros2_source node` works too once colcon-ros has registered the package).

## Tests

`core-driver/tests/test_ros2_source.py` (host, no ROS): the encoding table, the pinned-format
decisions, row unpadding, and the header — packed here and by `cam_driver.transport`, compared byte
for byte. The end-to-end path is a bench check: a topic on the graph → the instance's WebRTC preview.
