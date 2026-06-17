# camera-service

A multi-source **camera service** on **GStreamer**, targeting **NVIDIA Jetson** (AGX Orin
on JetPack 6 or 7; portable to Jetson Thor). Capture sources are pluggable behind a small
`Source` interface, and three are implemented:
- **GigE Vision** (GVSP, via Aravis) — per-frame PTP/chunk timestamps; protocol-validated
  end-to-end against a patched chunk-emitting GVSP camera (the on-camera PTP experiment is
  the remaining hardware step);
- **USB/v4l2** — raw color/mono or encoded (MJPEG/H.264 stream-copied to disk verbatim),
  opt-in v4l2 SOF timestamps, hotplug reconnect; validated on the Orin with a real UVC camera;
- **RTSP** — self-configures codec/geometry from a live-stream probe, stream-copies the
  delivered bitstream, RTCP→NTP per-frame timestamps (gst ≥ 1.24), reconnect with re-probe;
  validated on the Orin against a real 4K H.265 camera.

Whatever the source, the core attaches a **per-frame hardware timestamp** (PTP/chunk on GigE,
v4l2 SOF on USB, RTCP→NTP on RTSP — with a graceful provenance-tracked fallback ladder),
records a **lossless, temporally-compressed** video file, and fans the stream out to consumer
"plugins" (ROS2, ROS1, WebRTC, MQTT, ...).

> **Design & decisions** → [docs/DESIGN.md](docs/DESIGN.md) · **Status & roadmap** → [docs/ROADMAP.md](docs/ROADMAP.md) · **PTP experiment** → [docs/ptp-timestamp-experiment.md](docs/ptp-timestamp-experiment.md) · **JetPack 7 (Orin) bring-up** → [docs/jetpack7-bringup.md](docs/jetpack7-bringup.md)

## Why it's built this way

- **The hardware timestamp is the pipeline's time base.** Each frame's PTP timestamp
  (parsed from chunk data) is mapped onto the GstBuffer PTS, so it propagates for free
  to the recording, the sidecar CSV, and every consumer (e.g. a ROS2 `header.stamp`).
  We don't time-stamp on arrival, so we don't bake in network/processing latency or jitter.
- **Producer / consumer split.** The **core** does the frame-loss-critical work — capture,
  timestamping, lossless recording — in one tightly-controlled pipeline. WebRTC, ROS2, and
  MQTT are best-effort **consumers** of a published transport, added without touching the core.
- **Pluggable capture sources, one shared pipeline.** A `Source` owns the frontend — device,
  per-frame timestamp policy, and a feeder — and hands every frame downstream as
  `(timestamp, frame_id, bytes)`. So GigE (Aravis), USB (v4l2), and RTSP share the *same*
  transport → recorder → ROS/WebRTC/discovery and the *same* timestamp-provenance handling;
  `camera.type` (default `gige`) picks the frontend. Encoded sources (USB MJPEG/H.264, RTSP)
  are dual-output: the recorder **stream-copies the delivered bitstream** (faithful, no
  re-encode) while a parallel decode branch feeds live consumers raw frames.
- **The GigE source is a small appsrc feeder, not a custom GStreamer plugin.** The stock `aravissrc`
  element hides `frame_id` and chunk data and recycles the camera buffer internally, so it
  can't give us a per-frame PTP timestamp keyed by frame id. Instead a ~30-line Python loop
  pops Aravis buffers, reads `frame_id` + `ChunkTimestamp`, stamps the PTS, and pushes into a
  standard `appsrc`. Everything after that (`tee → mkv / shm / webrtc`) is ordinary GStreamer.

## Pipeline

```
Aravis stream  ──►  [feeder: read frame_id + PTP ChunkTimestamp;        ──► appsrc ──► tee ──┬─► recorder (lossless, temporal) ─► splitmuxsink .mkv
 (GigE cam,           set PTS = ts − base; write CSV row]                                     ├─► plugin transport (JP7: unixfd · JP6: shm+header) ─► plugins (ROS2, ...)
  PTP slave)                                                                                  └─► webrtcsink (lossy, low-latency) ─► remote viewers
```

The recorder is **pluggable / capability-detecting**:

| Capture format | Encoder (auto) | Lossless | Temporal | HW |
|---|---|---|---|---|
| Mono8 / Bayer*8 | `hw-hevc-lossless` (NVENC, NV24) | ✅ bit-exact | ✅ | ✅ |
| Mono16 / Bayer*16 | `ffv1` | ✅ | ❌ intra-only | ❌ |
| 8-bit + temporal, no NVENC | `x265-lossless` (set explicitly) | ✅ | ✅ | ❌ CPU |

> On Orin, hardware lossless is **8-bit only** — there is no 10/12-bit HW lossless and no AV1
> lossless. Choose 8-bit capture for the full hardware path, or FFV1 for higher bit depth.
> `x265-lossless` is 8-bit-only too: >8-bit capture rides in a GRAY16 container that x265's
> ≤12-bit input formats can't carry, so the recorder falls back to FFV1 (with a warning) rather
> than silently dropping sensor bits. Encoder elements are also **probed at build time**: if the
> selected encoder is missing on the host (e.g. no NVENC on an x86 dev box or in a container
> without the L4T stack), the recorder warns and records FFV1 instead of failing the pipeline.
> The **FFV1 path is multi-threaded** (`slices`): single-threaded FFV1 caps ~27 fps for 16-bit
> 640×512 on an Orin core, so a 60 fps 16-bit (thermal) camera needs the slice parallelism to keep
> up — once it does, sustained disk write (~25–30 MB/s for 16-bit) is the next ceiling to watch.

### 16-bit / thermal (radiometric) cameras
A 16-bit mono camera (Y16 → `GRAY16_LE`) records **FFV1 lossless with all 16 bits intact** and
publishes the raw counts as a `mono16` ROS topic — both kept radiometric for analysis. For the
WebRTC **operator preview**, set `CAM_WEBRTC_NORMALIZE: auto` on the webrtc-bridge: it
percentile-stretches GRAY16→GRAY8 so the picture is visible (a plain 16→8 convert keeps only the
top byte, which renders LSB-aligned thermal data near-black). The stretch is preview-only — the
recording and ROS topic are untouched. See [`cam_thermal.yaml`](core-driver/config/sensors/cam_thermal.yaml)
and the [webrtc-bridge README](plugins/webrtc-bridge/README.md).

## Repo layout

```
core-driver/            # the producer service
  cam_driver/          # config, pipeline, recorder, sidecar, transport
    sources/            # the capture-source seam: base (Source), gstbase, gige (Aravis), usb (v4l2), rtsp, factory
  main.py               # entry point
  config/camera.yaml    # camera + recording + preview settings
  Dockerfile
plugins/                # consumer apps: ros2-bridge, ros1-bridge, webrtc-bridge (mqtt-telemetry, ... as examples)
docker-compose.yml
```

## Outputs

For a recording named `cam`, each RUN gets a UTC-stamped prefix `cam-<YYYYmmdd-HHMMSS>` (so a
restart — e.g. compose restarting a crashed core — starts a new run beside the old one instead
of overwriting it) and the core writes:
- `cam-<stamp>-00000.mkv`, `cam-<stamp>-00001.mkv`, ... — bounded, lossless video segments (Matroska).
- `cam-<stamp>.csv` — one row per frame: `frame_id, pts_ns, timestamp_ns, source, chunk_ns, camera_ns, system_ns`
  (the three candidate timestamps are logged side-by-side — see the PTP experiment below).
- `cam-<stamp>.json` — header: absolute `base_timestamp_ns`, time-base source, pixel format,
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

Build & run — one **sensor config** drives one camera's stack via `cam-up`. Start from the
closest real-camera template under [core-driver/config/sensors/](core-driver/config/sensors/)
(each is heavily commented with the host prep + knobs for that camera class):

| Camera | Template | Notes |
|---|---|---|
| GigE Vision (PTP/chunk, mono or Bayer) | `cam_gige.yaml` | host MTU 9000 + `ptp4l`/`phc2sys`; the [PTP experiment](docs/ptp-timestamp-experiment.md) config |
| RTSP (H.264/H.265, up to 4K) | `cam_rtsp.yaml` | self-probes codec/geometry; stream-copy record |
| USB / UVC (MJPEG or raw) | `cam_usb.yaml` | map the v4l2 device in; MJPEG stream-copies to disk |
| 16-bit radiometric **thermal** (Y16) | `cam_thermal.yaml` | FFV1 16-bit record + `mono16` ROS; `CAM_WEBRTC_NORMALIZE` gives a visible preview |
| fake / dev (no hardware) | `cam_a.yaml`, `cam_b.yaml` | in-process Aravis fake camera |

```bash
mkdir -p recordings
cp core-driver/config/sensors/cam_gige.yaml core-driver/config/sensors/my-cam.yaml   # pick the closest, then edit it
./cam-up config/sensors/my-cam.yaml             # brings up core + the plugins the config enables
```

## Testing without a camera

No GigE Vision hardware needed — Aravis ships a fake camera.

```bash
# In-process fake (Mono8 512x512 @ 25 fps, no network). Exercises the full
# capture -> timestamp -> recorder -> CSV chain end-to-end on the Orin:
docker compose run --rm core-driver main.py -c config/fake-camera.yaml
```

The fake camera has **no chunk/PTP support**, so the timestamp source logs a graceful
fall back to `camera` (host realtime) — that's expected. `config/fake-camera.yaml` records
with `encoder: auto`: on a Jetson that validates the NVENC path; on a host without NVENC
(the dev container, an x86 box) the recorder warns and falls back to FFV1, exercising the
encoder-availability probe end-to-end.

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
docker build -f core-driver/Dockerfile.dev -t cam-dev .
./core-driver/tools/dev_test.sh     # unit tests + fake producer + shm_probe header round-trip + mkv decode
```

For iteration, mount the code live so edits need no rebuild:
`docker run --rm -v "$PWD/core-driver:/app" cam-dev <cmd>`. The [shm_probe](core-driver/tools/shm_probe.py)
tool reads the plugin endpoint and prints each frame's parsed header — the same thing the C++ bridge will do.

## Post-processing (verify lossless + recover original frames)

```bash
# Decode back to raw frames
ffmpeg -i cam-<stamp>-00000.mkv -f rawvideo -pix_fmt gray8 frames.raw      # 8-bit (Y plane = mosaic)
ffmpeg -i cam-<stamp>-00000.mkv -f rawvideo -pix_fmt gray16le frames.raw   # 16-bit (Mono16 / thermal Y16)
# Confirm bit-exact round trip
ffmpeg -i cam-<stamp>-00000.mkv -i source.y4m -lavfi psnr -f null -        # expect psnr_avg:inf
```
For raw Bayer recorded as gray, debayer using `bayer_pattern` from the JSON header after extraction.
The JSON header records `bits_per_pixel`, so pick `gray8` vs `gray16le` from it.

## Transport endpoints (for plugins)

The core publishes frames to same-host consumers on a per-sensor **`plugin_endpoint`**, whose
implementation is picked at build time by capability ([pipeline.py](core-driver/cam_driver/pipeline.py);
see [docs/unixfd-migration.md](docs/unixfd-migration.md)):

- **JP7 (GStreamer ≥ 1.24): `unixfd`** (default `/tmp/cam/unixfd`) — native caps (`video/x-raw`
  GRAY8/16, or `video/x-bayer` for 8-bit CFA) with PTS intact; `frame_id` rides in the buffer
  `offset` and the absolute PTP capture time (ns) in `offset_end`. Self-describing, no header —
  this **replaces** the shm+header endpoint where `unixfdsink` exists.
- **JP6 (GStreamer 1.20): shm + header** (`application/x-cam-frame`, default `/tmp/cam/frames`) —
  shm carries only bytes (PTS/metadata are dropped), so each frame is prefixed with a 36-byte
  FrameHeader ([transport.py](core-driver/cam_driver/transport.py)) carrying absolute PTP
  `timestamp_ns` + `frame_id` + geometry + provenance.
- **`raw_endpoint`** (`video/x-raw`, default off, `/tmp/cam/raw`) — a standard, header-free shm
  stream for generic same-host tools on either platform (no precise timestamps — an inherent shm
  limitation; that's what the egress layer, WebRTC/Zenoh, is for).

The C++ ros2-bridge ships a consumer for each (`CamUnixfdBridge` / `CamHeaderBridge`, selected by
`CAM_PLATFORM`) and stamps its messages from the carried capture time either way. Optional
`max_rate_hz` caps the plugin-endpoint publish rate. Both endpoints are configured under
`transport:` in the camera config. The `plugins:` list is consumed by the per-sensor supervisor
([supervisor.py](core-driver/supervisor.py)) — the core container's entrypoint — which spawns each
enabled `isolation: process` plugin; `isolation: container` plugins run as compose siblings.

## Per-sensor deployment

One sensor = one config under [core-driver/config/sensors/](core-driver/config/sensors/) — the single
source of truth. **[`cam-up`](cam-up)** reads it, turns the enabled `isolation: container` plugins into
Docker Compose **profiles**, and brings up that sensor's stack:

```bash
./cam-up config/sensors/cam_a.yaml          # Jetson: auto-detects JP6 (l4t/nvidia) or JP7 (runc/CDI), host net
./cam-up --dev config/sensors/cam_a.yaml    # no Jetson (laptop/CI): cam-dev core, no NVIDIA
./cam-up config/sensors/cam_a.yaml down     # tear it down
```

- **JP6/JP7 is auto-detected per host** (`/etc/nv_tegra_release`): a JetPack 7 Orin gets the runc + CDI
  NVENC overlay automatically, JetPack 6 the l4t base + nvidia runtime. Override with `--jp6`/`--jp7` or
  `CAM_PLATFORM`. It's a host fact, so `rig` pins it per host, never per sensor.
- **Each heavy plugin is its own compose fragment** (`plugins/<x>/compose.yml`), pulled into
  [docker-compose.yml](docker-compose.yml) via `include:` and run only when its profile is on. Adding a
  plugin to a sensor = flipping `enabled: true` in the config, not editing compose. (Needs Compose ≥ 2.20.)
- **Two plugin homes:** lightweight plugins (`isolation: process`) run in-image, spawned by the
  **supervisor** ([supervisor.py](core-driver/supervisor.py)) — the core container's entrypoint, which also
  forwards shutdown so the core finalizes its recording. Heavy ones (`isolation: container` — ros2-bridge,
  webrtc-bridge) are compose siblings.
- **Multiple cameras** = the same files run as multiple projects. `cam-up cam_a` and `cam-up cam_b`
  coexist — each its own compose project, shm volume, ROS namespace, and WebRTC port, all derived from the
  per-sensor config (host networking is shared, so ports/topics are namespaced per camera).
- **shm is a host-level interface, not stack-scoped:** the transport is a stable external volume
  (`cam_<name>_sock`) + `ipc: host`, so *other* sensor or autonomy stacks read a sensor's frames by
  mounting that volume + `--ipc=host`.
- **Drops into a vehicle-level orchestrator** (e.g. `rig`) without coupling this repo to it. `cam-up`
  accepts a config from **any host path** (a vehicle-wide inventory, not only `core-driver/config/sensors/`
  — it auto-mounts an out-of-repo config into the container); exposes `up -d` / `down` / `ps` / `logs` on
  one config; passes `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION` through so every stack shares one ROS 2 graph
  (default `rmw_zenoh_cpp`, via a shared per-host `rmw_zenohd` router — `--zenohd` / `tools/zenohd.sh`);
  and ships a [`rigging.yaml`](rigging.yaml) descriptor telling the orchestrator how to invoke it. The
  dependency is one-way — this repo stays fully standalone. (`service:` + `name:` are the routing keys;
  the core exposes a health check so `rig status` is real.) See [DESIGN.md](docs/DESIGN.md).

```bash
./tools/orchestration_test.sh            # validate the whole model without a Jetson or camera
./core-driver/tools/supervisor_test.sh   # validate the in-image supervisor path
```

## Status & roadmap

- [x] **P0** project scaffold + container
- [x] **P1** timestamp spine — custom Aravis appsrc, PTP via `GevIEEE1588`, chunk `Timestamp`/`FrameID`, fallback ladder, sidecar CSV + JSON
- [x] **P2** pluggable lossless recorder (HW HEVC-lossless / FFV1 / x265) — software FFV1 validated in
  containers; **NVENC HW lossless validated bit-exact on a JP7.2 Orin AGX** (60/60 random-noise frames,
  worst |Δ| = 0, 1.32× raw — see [docs/jetpack7-bringup.md](docs/jetpack7-bringup.md))
- [x] **P3** transport + consumers — shm publish (header endpoint + optional raw `video/x-raw`), the C++
  `rclcpp` **ros2-bridge** ([plugins/ros2-bridge](plugins/ros2-bridge); raw + lazy compressed `Image` with the
  capture time in `header.stamp`), and the config-driven **per-sensor supervisor**
  ([supervisor.py](core-driver/supervisor.py)). All validated end-to-end in containers.
- [x] **P4** WebRTC consumer — gst-plugins-rs `webrtcsink` (lossy low-latency, encodes internally,
  congestion-controlled, multi-viewer) as a sibling container on the raw shm endpoint
  ([plugins/webrtc-bridge](plugins/webrtc-bridge)). Headless `webrtcsink → webrtcsrc` loopback validated in
  containers **and end-to-end on an R39 Orin AGX with HW NVENC** (webrtcsink selects `nvv4l2h264enc`; note
  webrtcsink pins H.264 **constrained-baseline** through gst-plugins-rs 0.15 — see the
  [bridge README](plugins/webrtc-bridge/README.md)). Browser viewing still to try.
- [ ] **P5** hardening *(in progress)* — **source reconnect/backoff** ✅ (GigE `control-lost` + data-starvation
  watchdogs, USB hotplug, RTSP re-probe-on-reopen; pipeline stays up, monotonic-PTS guard);
  **JetPack 7 bring-up** ✅ (JP7.2 Orin: CDI injection, `unixfd` transport shipped —
  [docs/jetpack7-bringup.md](docs/jetpack7-bringup.md)); disk-full + NVENC session budget next, then
  Thor portability (`nvunixfd` zero-copy, sm_110 is Thor-only).

### Testing tools (no Jetson, no camera)
The data path is validated by actually running it in containers — including the **real Aravis
chunk-parse path** via a patched chunk-emitting GV camera:
- [core-driver/tools/dev_test.sh](core-driver/tools/dev_test.sh) — producer: capture → timestamp → encoder-fallback probe → FFV1 → shm
- [core-driver/tools/usb_test.sh](core-driver/tools/usb_test.sh) — USB source: raw, **MJPEG stream-copy** (dual-output), and color/FFV1 paths
- [core-driver/tools/rtsp_test.sh](core-driver/tools/rtsp_test.sh) — RTSP source: local fake server → stream-copy record + **RTCP→NTP provenance** (CSV-checked)
- [core-driver/tools/rtsp_reconnect_test.sh](core-driver/tools/rtsp_reconnect_test.sh) — RTSP stall/recovery: kill + restart the server, assert detect → reopen → frames resume
- [plugins/ros2-bridge/tools/bridge_test.sh](plugins/ros2-bridge/tools/bridge_test.sh) — full chain → ROS2 raw + compressed `Image`
- [core-driver/tools/supervisor_test.sh](core-driver/tools/supervisor_test.sh) — supervisor spawn / manage / clean teardown
- [tools/gvsp-chunk-emitter/gvsp_test.sh](tools/gvsp-chunk-emitter) — **real GVSP + chunk-timestamp extraction** (patched Aravis fake camera)
- [tools/gvsp-chunk-emitter/roundtrip_test.sh](tools/gvsp-chunk-emitter) — **full input→output round-trip**: known frames+timestamps → GVSP → recording, compared **bit-exact against the exact transmitted bytes** (lossless + timestamp fidelity). Defaults to random noise; pass a video file (`roundtrip_test.sh clip.mkv`) to round-trip real footage instead.
- [plugins/webrtc-bridge/tools/webrtc_test.sh](plugins/webrtc-bridge/tools/webrtc_test.sh) — **WebRTC egress**: raw shm → `webrtcsink` → `webrtcsrc` decode (headless, no browser)
- [tools/orchestration_test.sh](tools/orchestration_test.sh) — **config-driven multi-sensor deploy**: `cam-up` profile selection, two cameras side by side (isolated projects), cross-stack shm read
- [tools/gvsp-chunk-emitter/reconnect_test.sh](tools/gvsp-chunk-emitter) — **camera reconnect/backoff**: kill the GVSP emitter mid-stream, restart it; the core detects, backs off, reconnects, resumes, and finalizes a valid recording

### Still needs a real GigE camera
(The NVENC recorder, the RTSP source, and the USB source are Orin-validated — see
[docs/jetpack7-bringup.md](docs/jetpack7-bringup.md) and the `core-driver/tools/orin_*.sh` scripts.)
- **Camera-specific PTP/chunk behaviour** — the exact chunk node names (`arv-tool-0.8 features`) and which of
  `chunk_ns`/`camera_ns`/`system_ns` is the authoritative PTP capture time (the [PTP experiment](docs/ptp-timestamp-experiment.md));
- **packed pixel formats** (Mono10p/Mono12Packed) need an unpack step not yet implemented.

## License

[Apache-2.0](LICENSE).
