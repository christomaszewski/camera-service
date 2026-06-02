# gige-vision-service

A generic GigE Vision (GVSP) camera driver built on **GStreamer + Aravis**, targeting
**NVIDIA Jetson** (AGX Orin on JetPack 6 or 7; portable to Jetson Thor). It captures
from any GigE Vision (GVSP) camera, extracts **hardware PTP timestamps**
from the GVSP chunk data, records a **lossless, temporally-compressed** video file, and
fans the stream out to consumer "plugins" (ROS2, WebRTC, MQTT, ...).

> **Design & decisions** → [docs/DESIGN.md](docs/DESIGN.md) · **Status & roadmap** → [docs/ROADMAP.md](docs/ROADMAP.md) · **PTP experiment** → [docs/ptp-timestamp-experiment.md](docs/ptp-timestamp-experiment.md) · **JetPack 7 (Orin) bring-up** → [docs/jetpack7-bringup.md](docs/jetpack7-bringup.md)

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
 (GigE cam,           set PTS = ts − base; write CSV row]                                     ├─► shm transport (unixfd later) ─► plugins (ROS2, ...)
  PTP slave)                                                                                  └─► webrtcsink (lossy, low-latency) ─► remote viewers
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
plugins/                # consumer apps: ros2-bridge, webrtc-bridge (mqtt-telemetry, ... as examples)
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

Build & run — one **sensor config** drives one camera's stack via `gige-up`:
```bash
mkdir -p recordings
cp core-driver/config/sensors/cam_a.yaml core-driver/config/sensors/my-cam.yaml   # then edit it
./gige-up config/sensors/my-cam.yaml             # brings up core + the plugins the config enables
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

One sensor = one config under [core-driver/config/sensors/](core-driver/config/sensors/) — the single
source of truth. **[`gige-up`](gige-up)** reads it, turns the enabled `isolation: container` plugins into
Docker Compose **profiles**, and brings up that sensor's stack:

```bash
./gige-up config/sensors/cam_a.yaml          # Jetson: l4t core, nvidia runtime, host networking
./gige-up --dev config/sensors/cam_a.yaml    # no Jetson (laptop/CI): gige-dev core, no NVIDIA
./gige-up config/sensors/cam_a.yaml down     # tear it down
```

- **Each heavy plugin is its own compose fragment** (`plugins/<x>/compose.yml`), pulled into
  [docker-compose.yml](docker-compose.yml) via `include:` and run only when its profile is on. Adding a
  plugin to a sensor = flipping `enabled: true` in the config, not editing compose. (Needs Compose ≥ 2.20.)
- **Two plugin homes:** lightweight plugins (`isolation: process`) run in-image, spawned by the
  **supervisor** ([supervisor.py](core-driver/supervisor.py)) — the core container's entrypoint, which also
  forwards shutdown so the core finalizes its recording. Heavy ones (`isolation: container` — ros2-bridge,
  webrtc-bridge) are compose siblings.
- **Multiple cameras** = the same files run as multiple projects. `gige-up cam_a` and `gige-up cam_b`
  coexist — each its own compose project, shm volume, ROS namespace, and WebRTC port, all derived from the
  per-sensor config (host networking is shared, so ports/topics are namespaced per camera).
- **shm is a host-level interface, not stack-scoped:** the transport is a stable external volume
  (`gige_<name>_sock`) + `ipc: host`, so *other* sensor or autonomy stacks read a sensor's frames by
  mounting that volume + `--ipc=host`.
- **Drops into a vehicle-level orchestrator** (e.g. `rig`) without coupling this repo to it. `gige-up`
  accepts a config from **any host path** (a vehicle-wide inventory, not only `core-driver/config/sensors/`
  — it auto-mounts an out-of-repo config into the container); exposes `up -d` / `down` / `ps` / `logs` on
  one config; passes `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION` through so every stack shares one DDS graph;
  and ships a [`deploy.yaml`](deploy.yaml) descriptor telling the orchestrator how to invoke it. The
  dependency is one-way — this repo stays fully standalone. (`service:` + `name:` are the routing keys;
  the core exposes a health check so `rig status` is real.) See [DESIGN.md](docs/DESIGN.md).

```bash
./tools/orchestration_test.sh            # validate the whole model without a Jetson or camera
./core-driver/tools/supervisor_test.sh   # validate the in-image supervisor path
```

## Status & roadmap

- [x] **P0** project scaffold + container
- [x] **P1** timestamp spine — custom Aravis appsrc, PTP via `GevIEEE1588`, chunk `Timestamp`/`FrameID`, fallback ladder, sidecar CSV + JSON
- [x] **P2** pluggable lossless recorder (HW HEVC-lossless / FFV1 / x265) — *written, pending on-device validation*
- [x] **P3** transport + consumers — shm publish (header endpoint + optional raw `video/x-raw`), the C++
  `rclcpp` **ros2-bridge** ([plugins/ros2-bridge](plugins/ros2-bridge); raw + lazy compressed `Image` with the
  capture time in `header.stamp`), and the config-driven **per-sensor supervisor**
  ([supervisor.py](core-driver/supervisor.py)). All validated end-to-end in containers.
- [x] **P4** WebRTC consumer — gst-plugins-rs `webrtcsink` (lossy low-latency, encodes internally,
  congestion-controlled, multi-viewer) as a sibling container on the raw shm endpoint
  ([plugins/webrtc-bridge](plugins/webrtc-bridge)). Headless `webrtcsink → webrtcsrc` loopback validated in
  containers; browser viewing + the HW encoder path need a Jetson.
- [ ] **P5** hardening *(in progress)* — **camera reconnect/backoff** ✅ (watchdog + `control-lost`,
  pipeline stays up, monotonic-PTS guard); disk-full + NVENC budget next — then **JetPack 7** (now
  runnable on the Orin via JP7.2: Ubuntu 24.04 / GStreamer 1.24, unlocking `unixfd` + `nvunixfd` zero-copy)
  and Thor portability (sm_110 rebuild). See [docs/jetpack7-bringup.md](docs/jetpack7-bringup.md).

### Testing tools (no Jetson, no camera)
The data path is validated by actually running it in containers — including the **real Aravis
chunk-parse path** via a patched chunk-emitting GV camera:
- [core-driver/tools/dev_test.sh](core-driver/tools/dev_test.sh) — producer: capture → timestamp → FFV1 → shm
- [plugins/ros2-bridge/tools/bridge_test.sh](plugins/ros2-bridge/tools/bridge_test.sh) — full chain → ROS2 raw + compressed `Image`
- [core-driver/tools/supervisor_test.sh](core-driver/tools/supervisor_test.sh) — supervisor spawn / manage / clean teardown
- [tools/gvsp-chunk-emitter/gvsp_test.sh](tools/gvsp-chunk-emitter) — **real GVSP + chunk-timestamp extraction** (patched Aravis fake camera)
- [tools/gvsp-chunk-emitter/roundtrip_test.sh](tools/gvsp-chunk-emitter) — **full input→output round-trip**: known frames+timestamps → GVSP → recording, compared **bit-exact against the exact transmitted bytes** (lossless + timestamp fidelity). Defaults to random noise; pass a video file (`roundtrip_test.sh clip.mkv`) to round-trip real footage instead.
- [plugins/webrtc-bridge/tools/webrtc_test.sh](plugins/webrtc-bridge/tools/webrtc_test.sh) — **WebRTC egress**: raw shm → `webrtcsink` → `webrtcsrc` decode (headless, no browser)
- [tools/orchestration_test.sh](tools/orchestration_test.sh) — **config-driven multi-sensor deploy**: `gige-up` profile selection, two cameras side by side (isolated projects), cross-stack shm read
- [tools/gvsp-chunk-emitter/reconnect_test.sh](tools/gvsp-chunk-emitter) — **camera reconnect/backoff**: kill the GVSP emitter mid-stream, restart it; the core detects, backs off, reconnects, resumes, and finalizes a valid recording

### Still needs the Orin / a real camera
- the **NVENC HW recorder** (`nvv4l2h265enc enable-lossless`, NVMM caps) — the software FFV1 path is validated;
- **Camera-specific PTP/chunk behaviour** — the exact chunk node names (`arv-tool-0.8 features`) and which of
  `chunk_ns`/`camera_ns`/`system_ns` is the authoritative PTP capture time (the [PTP experiment](docs/ptp-timestamp-experiment.md));
- **packed pixel formats** (Mono10p/Mono12Packed) need an unpack step not yet implemented.

## License

[Apache-2.0](LICENSE).
