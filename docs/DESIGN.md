# Design & decision log

The *why* behind the code — architecture, the decisions we made and the reasoning, and the
platform constraints that shaped them. Read this with [README.md](../README.md) (how to run) and
[ROADMAP.md](ROADMAP.md) (status / what's next). If you're a future session picking this up: this
file plus ROADMAP.md should get you oriented without re-deriving anything.

## Goal & context

A **generic GigE Vision (GVSP) camera driver** on **GStreamer + Aravis**, deployed on **NVIDIA
Jetson** (AGX Orin now; portable to Thor / JetPack 7). Target cameras include the **FLIR Blackfly
S**. It must: extract **per-frame hardware (PTP) timestamps** so we record true sensor-capture time
(not arrival time, which carries network + processing latency/jitter); record **losslessly with
temporal compression**; and **distribute** frames to consumer "plugins" (ROS2, WebRTC, MQTT, …).
Dev happens on macOS; everything actually runs on the Jetson in Docker (and in CI-style container
tests — see ROADMAP).

## Architecture spine (two unifying ideas)

1. **The hardware timestamp is the pipeline's time base.** Each frame's PTP/chunk timestamp is
   mapped onto the GstBuffer PTS (offset to a per-recording base; the absolute base is stored in the
   sidecar). It then propagates for free to the recording, the sidecar CSV, and every consumer (e.g.
   a ROS2 `header.stamp`). We never timestamp on arrival.
2. **Producer / consumer split.** The **core** does the frame-loss-critical work — capture,
   timestamping, lossless recording — in one tightly-controlled pipeline. WebRTC, ROS2, MQTT are
   best-effort **consumers** of a published transport, added without touching the core. WebRTC is
   "just another consumer," not an in-core tee.

## Pipeline

```
Aravis stream ─► [feeder: read frame_id + PTP ChunkTimestamp; set PTS = ts−base; write CSV row]
                       │ (custom appsrc, ~Python)
                       ▼
                     appsrc ─► tee ─┬─► recorder (lossless, temporal) ─► splitmuxsink .mkv + .csv/.json
                                    ├─► shm publish (application/x-gige-frame header endpoint) ─► plugins
                                    ├─► (optional) raw video/x-raw shm endpoint ─► generic tools
                                    └─► webrtcsink (lossy, low-latency) ─► remote viewers
```

## Key decisions (and why)

- **Custom Aravis `appsrc`, not the stock `aravissrc` element.** `aravissrc` exposes neither
  `frame_id` nor chunk data and recycles the `ArvBuffer` internally before any downstream probe can
  read it; its PTS is only a relative camera-time delta. So a ~30-line Python feeder pops Aravis
  buffers, reads `frame_id` + `ChunkTimestamp`, stamps PTS, and pushes into a standard `appsrc`.
  Everything after that is ordinary GStreamer. (This is *not* a compiled custom plugin.)

- **Timestamp source ladder + provenance.** Primary = PTP `ChunkTimestamp` (parsed from GVSP chunk
  data); fall back to the Aravis camera-clock (`arv_buffer_get_timestamp`) then host arrival time.
  The chunk timestamp is used **whenever chunk data is available** — PTP-lock is *provenance*
  (recorded as `ptp_synced` in the sidecar: is it wall-clock, or a free-running camera counter?).
  Every CSV row logs all three candidate timestamps (`chunk_ns`/`camera_ns`/`system_ns`) so
  post-processing can see the arrival jitter we're avoiding.

- **Lossless recording — the central tension.** {lossless + temporal + hardware + >8-bit} cannot all
  hold at once on Orin. HW lossless (`nvv4l2h265enc enable-lossless`, NV24/YUV444, bit-exact, with
  temporal P/B frames) is **8-bit only**. For 10/12-bit you go CPU: `x265 --lossless` (temporal,
  throughput-limited at 4K) or **FFV1** (lossless, high-bit-depth, but **intra-only**). The recorder
  is therefore **pluggable/auto-selecting**: 8-bit → HW HEVC-lossless (default), >8-bit → FFV1, with
  x265 as an explicit option. A mono/Bayer mosaic rides in the Y plane of NV24; the Bayer pattern is
  recorded in the sidecar for post-debayer.

- **Transport = shm + a 36-byte header (`application/x-gige-frame`).** GStreamer's `shmsink`/`shmsrc`
  transmit **only raw bytes** — PTS, DTS, and all `GstMeta` are dropped across the process boundary
  (confirmed at source level). So per-frame metadata (absolute timestamp, frame_id, geometry,
  provenance) travels in a fixed header prepended to each frame; the consumer parses it and stamps
  its messages. This makes the payload a *custom* format — a generic `video/x-raw` tool can't read it.
  That's an inherent shm limitation (a generic consumer would get arrival-time anyway), so: our
  plugins know the header; for generic tools there's an **optional clean `video/x-raw` shm endpoint**;
  and the real interop surface is the **egress layer** (WebRTC / Zenoh), not raw shm. The clean
  "standard payload + metadata together" only arrives with **`unixfd`** (GStreamer ≥1.24, so JP7/Thor)
  or **Zenoh attachments** — the transport is a swappable sink so that upgrade is localized.

- **Language: Python core + C++ ROS2 bridge.** GStreamer is C; you build/control pipelines from any
  binding. Standard elements run in C regardless of host language; only `appsrc`/`appsink`/pad probes
  run *your* code per-frame. Python (PyGObject) is gentle and fine at typical GigE rates and doesn't
  affect timestamp accuracy (the value is the camera's). The ROS2 bridge is C++ (`rclcpp`) for
  performance and because it's a separate process anyway. Port the core to C++ later only if
  profiling on hardware demands it.

- **Plugin hosting & the per-sensor supervisor.** "Plugin = appsink" regardless of where it runs —
  a separate-container plugin is just an appsink behind a transport. Two independent dials: *language*
  (Python/C++) and *isolation* (thread / process / container). A worker thread survives soft failures
  but **not a hard crash** (segfault kills the process); only a separate **process** firewalls that —
  and hard isolation ⟹ process ⟹ (cheap, because rate-limited) IPC. So: one **container per sensor**;
  a config-driven **supervisor** (`supervisor.py`, under `tini`/`--init`) spawns the core + lightweight
  in-image plugins as managed processes; heavy plugins (ROS2/DDS) run as **sibling containers**
  sharing the shm transport (`ipc: host` + a socket volume). Same plugin code either way.

- **SEI timestamp injection — considered, declined as primary.** `user_data_unregistered` SEI is the
  standards-correct in-bitstream hook and is lossless-safe (non-VCL), but `nvv4l2*` can't inject
  arbitrary SEI (NVENC-SDK path is Thor/JP7 only), no off-the-shelf GStreamer element inserts it, and
  ffmpeg's `hevc_metadata` has no SEI option. It's also stripped by any re-encode. The CSV sidecar is
  simpler, universal, and we'd keep it regardless — so SEI would be pure addition. For the **streaming**
  path use an **RTP header extension** (abs-capture-time / RFC 6051 `rtphdrextntp64`), not SEI.

- **WebRTC egress via `webrtcsink` (gst-plugins-rs), built from source.** `webrtcsink` isn't packaged
  for Ubuntu — it's the Rust `gst-plugin-webrtc`, built with `cargo cinstall` in the plugin image
  (LTO-off + limited jobs to dodge the Docker OOM-on-link). It takes **raw video and encodes
  internally** — zerolatency/CBR/keyframe tuning, GCC congestion control, FEC/RTX, multi-viewer
  fan-out — so the bridge just feeds it frames; no hand-rolled encoder. It runs as a **sibling
  container** consuming the **raw shm endpoint** (the Rust toolchain doesn't belong in the core image;
  same model as ros2-bridge). Two non-obvious gotchas the container tests caught: (1) webrtcsink/
  webrtcsrc need the **GStreamer libnice elements** (`gstreamer1.0-nice`, a separate package from the
  libnice C library) or `webrtcbin` fails ICE with "libnice elements are not available"; (2)
  shm-sourced buffers must be **re-timestamped** (`shmsrc do-timestamp=true`) and **converted to I420**
  before webrtcsink — shm drops PTS (which RTP needs) and the encoders want YUV, not a mono camera's
  GRAY8. Without either, the producer reaches PLAYING but never announces a negotiable stream, so a
  consumer's `connect-to-first-producer` silently never latches. Validated headlessly (no browser) via
  a `webrtcsink → gst-webrtc-signalling-server → webrtcsrc` loopback. Carrying the capture PTP time
  through WebRTC (`webrtcsink do-clock-signalling=true` + the `ntp-64` RTP header extension →
  `GstReferenceTimestampMeta` on a `webrtcsrc` consumer) is a future upgrade that would also require
  consuming the header endpoint (for geometry + timestamp) instead of the raw endpoint.

- **Zenoh as the future data fabric.** Eclipse Zenoh (mature, v1.x; pub/sub, same-host SHM with
  transparent network fallback, per-message "attachments" ideal for ts+frame_id; `rmw_zenoh` is a
  Tier-1 ROS2 RMW). It slots into the plugin contract as just-another-transport (replacing shm+header)
  and is complementary to WebRTC (Zenoh = machine data fabric; WebRTC = human viewing). The GStreamer
  Zenoh element (`gst-plugin-zenoh`) exists but is experimental → for production, `appsink` → Zenoh
  client `put`. Not adopted yet; the swappable transport keeps it a clean future option.

## Platform constraints (hard facts that shaped the design)

- **Aravis/FLIR:** FLIR exposes PTP via the legacy `GevIEEE1588*` names (not SFNC `Ptp*`). Aravis
  **cannot decode** FLIR's proprietary on-camera compression or standard GEV JPEG/H.264 payloads →
  run uncompressed on the wire (`ImageCompressionMode=Off`, jumbo frames). Need **Aravis ≥0.8.23**
  (prefer ≥0.8.32) for FLIR extended-chunk support. `arv_buffer_get_data()` returns image **+ chunks**
  when chunk mode is on — slice to the image size (a real bug the chunk emitter test caught).
- **Jetson encode:** Orin HW lossless is **8-bit only** (no 10/12-bit HW lossless, no AV1 lossless).
  Orin Nano has **no NVENC** (software only); AGX Orin / Orin NX have it. **Thor (JP7) keeps NVENC+NVDEC**
  (the "Blackwell removed NVENC" rumor is about datacenter GPUs) **but removes AV1 hardware encode** →
  design on HEVC/H.264. JP7 = Ubuntu 24.04 / GStreamer 1.24 / CUDA 13 / sm_110; use `--runtime=nvidia`
  (not `--gpus=all`).
- **Containers on Jetson:** `nvv4l2*`/`nvvidconv` are injected from the host BSP via the nvidia
  runtime's CSV mounts → the base image must match host L4T (`l4t-jetpack:r36.x`), and the container's
  GStreamer is effectively pinned to 1.20 (so no `unixfd`, which needs 1.24, until JP7). GigE needs
  **host networking** (broadcast discovery; the `docker0` bridge breaks Aravis discovery) and **no
  `/dev` passthrough**. PTP daemons run on the host; containers share `CLOCK_REALTIME`.
- **Zero-copy GPU sharing across containers** is effectively a JP7/Thor capability (`nvunixfd` needs
  DeepStream 8 / JP7; CUDA IPC is unsupported on Orin). On JP6 it's prototype-grade.

## Testing strategy

The data path is validated by **actually running it in containers** (no Jetson/camera needed). The
hard part — exercising the real Aravis **chunk-parse** path — the stock fake camera can't do (no chunk
support). So `tools/gvsp-chunk-emitter/` **patches Aravis** to emit real GVSP chunk data and to replay
known frames+timestamps, enabling a full **input→output round-trip** that proves lossless recording
(random-noise frames, bit-exact) + timestamp fidelity. See ROADMAP for the test inventory.
