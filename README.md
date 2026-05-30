# gige-vision-service

A generic GigE Vision (GVSP) camera driver built on **GStreamer + Aravis**, targeting
**NVIDIA Jetson** (AGX Orin today; portable to Jetson Thor / JetPack 7). It captures
from GigE Vision cameras (e.g. FLIR Blackfly S), extracts **hardware PTP timestamps**
from the GVSP chunk data, records a **lossless, temporally-compressed** video file, and
fans the stream out to consumer "plugins" (ROS2, WebRTC, MQTT, ...).

> **Design & decisions** → [docs/DESIGN.md](docs/DESIGN.md) · **Status & roadmap** → [docs/ROADMAP.md](docs/ROADMAP.md) · **PTP experiment** → [docs/ptp-timestamp-experiment.md](docs/ptp-timestamp-experiment.md)

## Why it's built this way

- **The hardware timestamp is the pipeline's time base.** Each frame's PTP timestamp
  (parsed from chunk data) is mapped onto the GstBuffer PTS, so it propagates for free
  to the recording, the sidecar CSV, and every consumer (e.g. a ROS2 `header.stamp`).
  We don't time-stamp on arrival, so we don't bake in network/processing latency or jitter.
- **Producer / consumer split.** The **core** does the frame-loss-critical work — capture,
  timestamping, lossless recording — in one tightly-controlled pipeline. WebRTC, ROS2, and
  MQTT are best-effort **consumers** of a published transport, added without touching the core.
- **A small custom appsrc feeder, not a custom GStreamer plugin.** The stock `aravissrc`
  element hides `frame_id` and chunk data and recycles the camera buffer internally, so it
  can't give us a per-frame PTP timestamp keyed by frame id. Instead a ~30-line Python loop
  pops Aravis buffers, reads `frame_id` + `ChunkTimestamp`, stamps the PTS, and pushes into a
  standard `appsrc`. Everything after that (`tee → mkv / shm / webrtc`) is ordinary GStreamer.

## Pipeline

```
Aravis stream  ──►  [feeder: read frame_id + PTP ChunkTimestamp;        ──► appsrc ──► tee ──┬─► recorder (lossless, temporal) ─► splitmuxsink .mkv
 (FLIR BFS,           set PTS = ts − base; write CSV row]                                     ├─► (later) shm / unixfd transport ─► plugins
  PTP slave)                                                                                  └─► (later) webrtcsink (lossy, low-latency)
```

The recorder is **pluggable / capability-detecting**:

| Capture format | Encoder (auto) | Lossless | Temporal | HW |
|---|---|---|---|---|
| Mono8 / Bayer*8 | `hw-hevc-lossless` (NVENC, NV24) | ✅ bit-exact | ✅ | ✅ |
| Mono16 / Bayer*16 | `ffv1` | ✅ | ❌ intra-only | ❌ |
| 10/12-bit + temporal | `x265-lossless` (set explicitly) | ✅ | ✅ | ❌ CPU |

> On Orin, hardware lossless is **8-bit only** — there is no 10/12-bit HW lossless and no AV1
> lossless. Choose 8-bit capture for the full hardware path, or a CPU path for higher bit depth.

## Repo layout

```
core-driver/            # the producer service
  gige_driver/          # config, camera (Aravis), timestamps, sidecar, recorder, pipeline
  main.py               # entry point
  config/camera.yaml    # camera + recording + preview settings
  Dockerfile
plugins/                # consumer applications (ros2-bridge, mqtt-telemetry, webrtc, ...)
docker-compose.yml
```

## Outputs

For a recording named `gige`, the core writes:
- `gige-00000.mkv`, `gige-00001.mkv`, ... — bounded, lossless video segments (Matroska).
- `gige.csv` — one row per frame: `frame_id, pts_ns, timestamp_ns, source, chunk_ns, camera_ns, system_ns`
  (the three candidate timestamps are logged side-by-side — see the PTP experiment below).
- `gige.json` — header: absolute `base_timestamp_ns`, time-base source, pixel format,
  Bayer pattern, bit depth, geometry, tick frequency, and the PTS↔absolute-time convention.

`absolute_ns = pts_ns + base_timestamp_ns` (epoch defined by `timestamp_source`; PTP time when locked).

## Run (on the Jetson)

Host prep (once):
```bash
sudo ip link set <cam-iface> mtu 9000          # jumbo frames for GigE Vision
sudo sysctl -w net.core.rmem_max=33554432      # larger socket receive buffers
# PTP grandmaster (so the camera can slave its clock):
#   sudo ptp4l -i <cam-iface> -m   &&   sudo phc2sys -a -r
```

Build & run:
```bash
mkdir -p recordings
docker compose up --build core-driver
# edit core-driver/config/camera.yaml for your camera / format / encoder
```

## Testing without a camera

No GigE Vision hardware needed — Aravis ships a fake camera.

```bash
# In-process fake (Mono8 512x512 @ 25 fps, no network). Exercises the full
# capture -> timestamp -> recorder -> CSV chain end-to-end on the Orin:
docker compose run --rm core-driver -c config/fake-camera.yaml
```

The fake camera has **no chunk/PTP support**, so the timestamp source logs a graceful
fall back to `camera` (host realtime) — that's expected. To validate the NVENC path,
change `encoder: ffv1` to `auto` in `config/fake-camera.yaml`.

The chunk-PTP parsing the fake camera *can't* drive is covered by a hardware-free unit test:

```bash
python3 core-driver/tests/test_timestamps.py     # or: pytest core-driver/tests
```

(A networked fake — real GVSP, for discovery/packet-path testing — is available as a
commented `fake-camera` service in `docker-compose.yml`.)

### Dev container (run the producer without a Jetson)

[core-driver/Dockerfile.dev](core-driver/Dockerfile.dev) is an Ubuntu 22.04 image with GStreamer 1.20 +
Aravis 0.8 (mirroring JetPack 6's userspace) but **no NVIDIA stack** — so it runs the entire producer
on any machine: fake camera → timestamps → CSV/JSON → software **FFV1** recording → shm transport.
(It can't exercise the NVENC recorder or real PTP/chunk timestamps — those need the Orin.)

```bash
docker build -f core-driver/Dockerfile.dev -t gige-dev .
./core-driver/tools/dev_test.sh     # unit tests + fake producer + shm_probe header round-trip + mkv decode
```

For iteration, mount the code live so edits need no rebuild:
`docker run --rm -v "$PWD/core-driver:/app" gige-dev <cmd>`. The [shm_probe](core-driver/tools/shm_probe.py)
tool reads the plugin endpoint and prints each frame's parsed header — the same thing the C++ bridge will do.

## Post-processing (verify lossless + recover original frames)

```bash
# Decode back to raw frames
ffmpeg -i gige-00000.mkv -f rawvideo -pix_fmt gray8 frames.raw      # 8-bit (Y plane = mosaic)
# Confirm bit-exact round trip
ffmpeg -i gige-00000.mkv -i source.y4m -lavfi psnr -f null -        # expect psnr_avg:inf
```
For raw Bayer recorded as gray, debayer using `bayer_pattern` from the JSON header after extraction.

## Transport endpoints (for plugins)

The core publishes frames to same-host consumers over GStreamer shm. Because shm carries only
bytes (no PTS/metadata), per-frame metadata travels in a 36-byte header ([transport.py](core-driver/gige_driver/transport.py)):

- **`plugin_endpoint`** (`application/x-gige-frame`, default `/tmp/gige/frames`) — `[36-byte FrameHeader][pixels]`,
  carrying absolute PTP `timestamp_ns` + `frame_id` + geometry + provenance. Our plugins (e.g. the C++
  ros2-bridge) read the header and stamp their messages from it. Optional `max_rate_hz` caps publish rate.
- **`raw_endpoint`** (`video/x-raw`, default off) — a standard, header-free shm stream for generic same-host
  tools that just want frames (no precise timestamps — an inherent shm limitation; that's what the egress
  layer, WebRTC/Zenoh, is for).

Both are configured under `transport:` in the camera config. The `plugins:` list is consumed by the
(forthcoming) per-sensor supervisor, which spawns each enabled plugin as its own process.

## Per-sensor deployment

One sensor = one stack. The **supervisor** ([supervisor.py](core-driver/supervisor.py)) is the core
container's entrypoint: it reads the config, spawns the core + each enabled *in-image* plugin as its own
process (crash-isolated, restarted on failure), and on shutdown sends SIGINT so the core finalizes its
recording. Run it under an init (`docker run --init` / compose `init: true`) for orphan reaping.

Heavy plugins with their own runtime (the ROS2 bridge) run as **sibling containers** sharing the shm
transport (`ipc: host` + a shared socket volume), not in-image. `docker compose up` brings up the whole
per-sensor stack (core + ros2-bridge).

```bash
docker compose up --build                # on the Jetson
./core-driver/tools/supervisor_test.sh   # validate the supervisor without a Jetson
```

## Status & roadmap

- [x] **P0** project scaffold + container
- [x] **P1** timestamp spine — custom Aravis appsrc, PTP via `GevIEEE1588`, chunk `Timestamp`/`FrameID`, fallback ladder, sidecar CSV + JSON
- [x] **P2** pluggable lossless recorder (HW HEVC-lossless / FFV1 / x265) — *written, pending on-device validation*
- [x] **P3** transport + consumers — shm publish (header endpoint + optional raw `video/x-raw`), the C++
  `rclcpp` **ros2-bridge** ([plugins/ros2-bridge](plugins/ros2-bridge); raw + lazy compressed `Image` with the
  capture time in `header.stamp`), and the config-driven **per-sensor supervisor**
  ([supervisor.py](core-driver/supervisor.py)). All validated end-to-end in containers.
- [ ] **P4** WebRTC consumer (`webrtcsink`, lossy low-latency)
- [ ] **P5** hardening (reconnect, disk-full, NVENC budget) + Thor/JP7 portability (`nvunixfd` zero-copy, sm_110 rebuild)

### Testing tools (no Jetson, no camera)
The data path is validated by actually running it in containers — including the **real Aravis
chunk-parse path** via a patched chunk-emitting GV camera:
- [core-driver/tools/dev_test.sh](core-driver/tools/dev_test.sh) — producer: capture → timestamp → FFV1 → shm
- [plugins/ros2-bridge/tools/bridge_test.sh](plugins/ros2-bridge/tools/bridge_test.sh) — full chain → ROS2 raw + compressed `Image`
- [core-driver/tools/supervisor_test.sh](core-driver/tools/supervisor_test.sh) — supervisor spawn / manage / clean teardown
- [tools/gvsp-chunk-emitter/gvsp_test.sh](tools/gvsp-chunk-emitter) — **real GVSP + chunk-timestamp extraction** (patched Aravis fake camera)
- [tools/gvsp-chunk-emitter/roundtrip_test.sh](tools/gvsp-chunk-emitter) — **full input→output round-trip**: known frames+timestamps → GVSP → recording, then byte-compared (lossless + timestamp fidelity)

### Still needs the Orin / a real Blackfly S
- the **NVENC HW recorder** (`nvv4l2h265enc enable-lossless`, NVMM caps) — the software FFV1 path is validated;
- **FLIR-specific PTP/chunk behaviour** — the exact chunk node names (`arv-tool-0.8 features`) and which of
  `chunk_ns`/`camera_ns`/`system_ns` is the authoritative PTP capture time (the [PTP experiment](docs/ptp-timestamp-experiment.md));
- **packed pixel formats** (Mono10p/Mono12Packed) need an unpack step not yet implemented.
