# gige_ros2_bridge

`rclcpp` **composable components** that consume the core's transport and republish each frame as
`sensor_msgs/Image`, stamping `header.stamp` from the per-frame hardware (PTP) timestamp. Two
transport-specific components share a base (`GigeBridgeBase`) and are loaded into a
`component_container_mt` by [`launch/bridge.launch.py`](launch/bridge.launch.py), which picks the right
one from the platform `gige-up` exports:

| component | platform | transport | format source | timestamp + frame_id |
|---|---|---|---|---|
| `GigeUnixfdBridge` | JP7 (GStreamer ≥ 1.24) | `unixfdsrc` (header-free) | negotiated **caps** | `buffer.offset_end` / `buffer.offset` |
| `GigeHeaderBridge` | JP6 (GStreamer 1.20) | `shmsrc` + 36-byte header | header `pixfmt` + `encoding` hint | header fields |

The JP6 header contract is [`core-driver/gige_driver/transport.py`](../../core-driver/gige_driver/transport.py);
the C++ `FrameHeader` mirrors it exactly, guarded by a `static_assert` on the 36-byte size. JP7 carries
native caps + buffer fields over the unixfd socket, so there is no header.

## Middleware: rmw_zenoh (default)

The whole stack defaults to **`RMW_IMPLEMENTATION=rmw_zenoh_cpp`**. Zenoh handles large image messages
far better than the FastDDS default config — measured here, a 786 KB `rgb8` frame at 25 fps flows at the
**full 25 Hz under Zenoh vs ~2 Hz on default FastDDS**. FastDDS stays selectable via `RMW_IMPLEMENTATION`.

rmw_zenoh discovers through a **shared per-host router** (`rmw_zenohd`). `rig` runs one per host; standalone,
run [`tools/zenohd.sh up`](../../tools/zenohd.sh) once (or `gige-up --zenohd …`). On host networking nodes
reach it at the default `tcp/localhost:7447` — no extra config.

> **Debugging caveat:** under rmw_zenoh the daemon-backed `ros2 topic echo`/`hz` often shows nothing even
> though data is flowing. Add **`--no-daemon`** (`ros2 topic echo --no-daemon …`), or just subscribe with a
> real node — typed subscribers receive normally. This is a known rmw_zenoh ↔ ros2 daemon interaction.

## Parameters

| param | default | meaning |
|---|---|---|
| `socket_path` | `/tmp/gige/unixfd` (JP7) · `/tmp/gige/frames` (JP6) | the core's transport endpoint |
| `topic` | `image_raw` | output `sensor_msgs/Image` topic |
| `frame_id` | `camera` | `header.frame_id` (TF frame) |
| `encoding` | `""` (`$GIGE_ROS_ENCODING`) | Bayer label / hint. **Auto-set by `gige-up`** from `camera.pixel_format` (e.g. `BayerRG8` → `bayer_rggb8`); empty = mono. On JP7 the format also comes off the caps. |
| `debayer` | `false` (`$GIGE_DEBAYER`) | turn an 8-bit Bayer mosaic into color. Set via the plugin's `params.debayer`. |

## Color (Bayer cameras)

The core ships the raw single-channel mosaic; color is a choice, and *how* it's produced now differs by
platform (both are real, full-resolution debayering — the old interim 2×2 in-bridge demosaic is gone):

- **Option A (default, recommended):** publish the mosaic labeled **`bayer_rggb8`** (etc.). Run a standard
  `image_proc` debayer **on demand** — 1 channel on the wire, full quality downstream.
- **Option B (`params.debayer: true`):**
  - **JP7** inserts `bayer2rgb` into the GStreamer pipeline → publishes **`rgb8`** directly.
  - **JP6** composes **`image_proc::DebayerNode`** into the bridge's own container, so the Bayer frame is
    shared **intra-process (zero-copy)** and debayered to `<topic namespace>/image_color` (+ `image_mono`).

A `mono8`/`mono16` camera is unaffected — `encoding` stays empty and the bridge publishes mono.

## Zero-copy

- **JP7:** the core→bridge hop is the unixfd transport (memfd + SCM_RIGHTS fd-passing — no shm double-copy;
  the bridge `mmap`s the frame). In-pipeline `bayer2rgb` avoids a separate debayer hop. (True GPU zero-copy
  — NVMM + nvunixfd — is the future option B in [docs/unixfd-migration.md](../../docs/unixfd-migration.md).)
- **JP6:** intra-process comms is enabled **only** when `image_proc` is composed (debayer on), so the
  Bayer→color hop shares the buffer by pointer. Otherwise it's left off (no in-process subscriber to share with).

## Compressed images

Each component publishes through `image_transport`, so alongside the raw `<topic>` you get a **lazy**
`<topic>/compressed` (JPEG for 8-bit, PNG for 16-bit; no CPU unless something subscribes). Tune with the
standard `...compressed.*` params.

## Build & run

```bash
docker build -f plugins/ros2-bridge/Dockerfile -t ros2-bridge .
tools/zenohd.sh up                                   # one shared zenoh router per host
docker run --rm --ipc=host -v gige_sock:/tmp/gige \  # --ipc=host only matters for the JP6 shm transport
  -e GIGE_PLATFORM=jp7 -e GIGE_INSTANCE=cam_a -e RMW_IMPLEMENTATION=rmw_zenoh_cpp ros2-bridge
```

`--ipc=host` is required for the **JP6 shm** transport (the frame data lives in `/dev/shm`, only the
control socket is in the volume). The **JP7 unixfd** transport passes file descriptors over the socket, so
it needs no shared IPC namespace. In the per-sensor-container model the bridge can also run as a sibling
process in the core's container (shm is then free).
