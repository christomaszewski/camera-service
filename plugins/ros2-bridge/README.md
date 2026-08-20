# cam_ros2_bridge

`rclcpp` **composable components** that consume the core's transport and republish each frame as
`sensor_msgs/Image`, stamping `header.stamp` from the per-frame hardware (PTP) timestamp. Two
transport-specific components share a base (`CamBridgeBase`) and are loaded into a
`component_container_mt` by [`launch/bridge.launch.py`](launch/bridge.launch.py), which picks the right
one from the platform `cam-up` exports:

| component | platform | transport | format source | timestamp + frame_id |
|---|---|---|---|---|
| `CamUnixfdBridge` | JP7 (GStreamer ≥ 1.24) | `unixfdsrc` (header-free) | negotiated **caps** | `buffer.offset_end` / `buffer.offset` |
| `CamHeaderBridge` | JP6 (GStreamer 1.20) | `shmsrc` + 36-byte header | header `pixfmt` + `encoding` hint | header fields |

The JP6 header contract is [`core-driver/cam_driver/transport.py`](../../core-driver/cam_driver/transport.py);
the C++ `FrameHeader` mirrors it exactly, guarded by a `static_assert` on the 36-byte size. JP7 carries
native caps + buffer fields over the unixfd socket, so there is no header.

## Middleware: rmw_zenoh (default)

The whole stack defaults to **`RMW_IMPLEMENTATION=rmw_zenoh_cpp`**. Zenoh handles large image messages
far better than the FastDDS default config — measured here, a 786 KB `rgb8` frame at 25 fps flows at the
**full 25 Hz under Zenoh vs ~2 Hz on default FastDDS**. FastDDS stays selectable via `RMW_IMPLEMENTATION`.

rmw_zenoh discovers through a **shared per-host router** (`rmw_zenohd`). `rig` runs one per host; standalone,
run [`tools/zenohd.sh up`](../../tools/zenohd.sh) once (or `cam-up --zenohd …`). On host networking nodes
reach it at the default `tcp/localhost:7447` — no extra config.

> **Debugging caveat:** under rmw_zenoh the daemon-backed `ros2 topic echo`/`hz` often shows nothing even
> though data is flowing. Add **`--no-daemon`** (`ros2 topic echo --no-daemon …`), or just subscribe with a
> real node — typed subscribers receive normally. This is a known rmw_zenoh ↔ ros2 daemon interaction.

### Tuning zenoh for large images

All zenoh tuning goes through **`ZENOH_CONFIG_OVERRIDE`** (`path/to/key=value;…`), which rmw_zenoh
applies **on top of** its built-in default session config — every default we don't name survives.
Deliberately **not** `ZENOH_SESSION_CONFIG_URI`: a config URI *replaces* the whole default config, so
anything it doesn't restate is silently dropped. The bridge exposes the knobs three ways, all from the
sensor YAML (`plugins: → ros2-bridge → params:`):

```yaml
params:
  # Friendly knobs (lowercase) -- the launch file folds these into ZENOH_CONFIG_OVERRIDE:
  zenoh_shm: true                  # transport/shared_memory/{,transport_optimization/}enabled
  zenoh_shm_pool_size: 268435456   # SHM segment bytes (rmw_zenoh default 48 MiB); implies zenoh_shm: true
  zenoh_shm_msg_threshold: 4096    # min bytes routed via SHM (default 512; power of two); implies shm too
  # rmw_zenoh env knobs (UPPERCASE params pass straight through as env vars):
  RMW_ZENOH_BUFFER_POOL_MAX_SIZE_BYTES: 67108864   # serialization buffer pool cap (default 8 MiB)
  # Raw escape hatch for ANY other zenoh key -- appended last, so it wins over the friendly knobs:
  ZENOH_CONFIG_OVERRIDE: "transport/link/tx/batch_size=65535"
```

- **SHM is end-to-end opt-in:** the *subscriber's* session must also enable it
  (`export ZENOH_CONFIG_OVERRIDE='transport/shared_memory/enabled=true;transport/shared_memory/transport_optimization/enabled=true'`
  before launching it), and both endpoints must share `/dev/shm` — the bridge container already runs
  `ipc: host`. Mismatched peers fall back to the network path automatically, so enabling SHM here is
  safe even when some subscribers don't.
- **The pool must fit `/dev/shm`:** the pool is one POSIX segment in the bridge's `/dev/shm`. Under
  `ipc: host` that's the **host's** tmpfs (docker's `shm_size` doesn't apply) — `cam-up` warns at
  up-time when the host tmpfs is smaller than `zenoh_shm_pool_size`. The compose service also carries
  `shm_size: ${CAM_ROS2_SHM_SIZE}` (auto-derived as pool + 64 MiB headroom) so a non-host-IPC variant
  of the stack isn't capped at docker's 64 MiB default. Too small either way isn't fatal: zenoh logs a
  `ShmProvider` error and delivers over the network path instead.
- **Buffer pool:** `RMW_ZENOH_BUFFER_POOL_MAX_SIZE_BYTES` caps rmw_zenoh's pool of serialization
  buffers; once a publish exceeds what's left, buffers come from the system allocator each time. Size
  it to a few in-flight frames (e.g. a 5 MP `rgb8` frame is ~15 MB serialized).
- **Router:** with the default peer mesh the router only does discovery, so it normally needs no
  tuning. When traffic *is* routed (client mode / remote hub), `tools/zenohd.sh` forwards a
  `ZENOH_CONFIG_OVERRIDE` from its environment to the router the same way.

## Parameters

| param | default | meaning |
|---|---|---|
| `socket_path` | `/tmp/cam/unixfd` (JP7) · `/tmp/cam/frames` (JP6) | the core's transport endpoint |
| `topic` | `image_raw` | output `sensor_msgs/Image` topic |
| `frame_id` | `camera` | `header.frame_id` (TF frame) |
| `encoding` | `""` (`$CAM_ROS_ENCODING`) | Bayer label / hint. **Auto-set by `cam-up`** from `camera.pixel_format` (e.g. `BayerRG8` → `bayer_rggb8`); empty = mono. On JP7 the format also comes off the caps. |
| `debayer` | `false` (`$CAM_DEBAYER`) | turn an 8-bit Bayer mosaic into color. Set via the plugin's `params.debayer`. |
| `publish_rate` | `0` (`$CAM_ROS_PUBLISH_RATE`) | max publish rate in **Hz**; frames above it are dropped *before* conversion/copy, so the throttle also saves the bridge CPU. `0` = publish every frame. The core keeps its native `frame_rate` for recording/preview — only the ROS graph sees fewer frames. Set via `params.publish_rate`. |

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
docker run --rm --ipc=host -v cam_sock:/tmp/cam \  # --ipc=host only matters for the JP6 shm transport
  -e CAM_PLATFORM=jp7 -e CAM_INSTANCE=cam_a -e RMW_IMPLEMENTATION=rmw_zenoh_cpp ros2-bridge
```

`--ipc=host` is required for the **JP6 shm** transport (the frame data lives in `/dev/shm`, only the
control socket is in the volume). The **JP7 unixfd** transport passes file descriptors over the socket, so
it needs no shared IPC namespace. In the per-sensor-container model the bridge can also run as a sibling
process in the core's container (shm is then free).
