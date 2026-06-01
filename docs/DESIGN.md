# Design & decision log

The *why* behind the code — architecture, the decisions we made and the reasoning, and the
platform constraints that shaped them. Read this with [README.md](../README.md) (how to run) and
[ROADMAP.md](ROADMAP.md) (status / what's next). If you're a future session picking this up: this
file plus ROADMAP.md should get you oriented without re-deriving anything.

## Goal & context

A **generic GigE Vision (GVSP) camera driver** on **GStreamer + Aravis**, deployed on **NVIDIA
Jetson** (AGX Orin now; portable to Thor / JetPack 7). It works with any GVSP-compliant
camera. It must: extract **per-frame hardware (PTP) timestamps** so we record true sensor-capture time
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

- **Config-driven multi-sensor orchestration (`gige-up` + Compose `include`/profiles).** Deployment is
  driven by one **sensor config** (`config/sensors/<name>.yaml`) — never hand-edited compose. `gige-up`
  reads the config and turns its enabled `isolation: container` plugins into Compose **profiles**; each
  heavy plugin owns a `plugins/<x>/compose.yml` fragment that the top compose pulls in via **`include`**
  (Compose ≥ 2.20, verified present on JP6's `docker compose` v2). A plugin is thus added to a sensor by
  flipping `enabled` in the config, and the compose files stay static — **no generated compose artifact**
  (a step the user explicitly didn't want). The `plugins[]` list now feeds two consumers, split by
  `isolation`: `process` → the in-image supervisor; `container` → a compose profile (the supervisor skips
  those). **Multiple cameras** run the same files as separate Compose **projects** (`gige-up cam_a` /
  `cam_b`), each with its own project name, shm volume, ROS namespace, and signalling port derived from
  the config; because GigE forces host networking, those ports/topics must be namespaced per camera.
  **shm is a host-level interface, not pod/project-scoped:** a stable **external** named volume
  (`gige_<name>_sock`) + `ipc: host` lets any other sensor/autonomy stack read a sensor's frames by
  mounting that volume + `--ipc=host` (validated: a container in neither project read cam_a's stream).
  Podman pods were weighed and rejected for exactly this reason — pod-scoped IPC would wall the shm off
  from other stacks. `service: gige-vision` in each config is the routing hook for a future machine-level
  launcher (scan `config/sensors/`, dispatch each to its stack); `gige-up` is deliberately the reusable
  per-sensor unit that layer would call. A dev override (`docker-compose.dev.yml`, `gige-up --dev`) swaps
  the l4t core for the `gige-dev` image so the whole model runs on a laptop/CI.

- **Camera reconnect/backoff (a watchdog + a backoff thread, not a pipeline restart).** A GigE camera
  dropping off the link is the most common field failure, so the core recovers without dying or
  corrupting the recording. The validated signal-driven feeder is kept; reconnect is *additive*: a 1 Hz
  watchdog on the main loop trips on either the Aravis `control-lost` signal or "no buffer for
  `reconnect_timeout_s`" and spawns a backoff thread that re-opens the camera (exponential, capped).
  Crucially the **GStreamer pipeline stays PLAYING** throughout — the live `appsrc` just idles — so the
  recording isn't finalized and shm consumers keep their connection; only the camera half is rebuilt.
  Two subtleties the container test surfaced: (1) the **old stream's GVSP receive socket must be released
  before reopen** (drop + gc the old stream/camera) or the new stream binds nothing and gets no frames —
  the bug showed up as a *second* disconnect 3 s after a "successful" reconnect; (2) a reconnected camera's
  clock can **reset**, so a **monotonic-PTS guard** rebases the PTS to stay strictly increasing for the
  muxer — the true (possibly discontinuous) timestamp is still logged per-frame in the sidecar CSV, so
  absolute time stays recoverable. Validated with the GVSP emitter killed + restarted mid-stream
  (`reconnect_test.sh`): detect → back off → reconnect → frames resume (336 recorded across the outage) →
  valid lossless recording, process never dies.

- **"rig-compatible" launcher contract (vehicle-level orchestration, one-way dependency).** A separate
  machine-level tool, **`rig`**, brings up every sensor stack on one vehicle by looping over a manifest
  and DELEGATING to each service's own per-sensor launcher — it never reimplements per-stack logic.
  gige-vision is one such service and **`gige-up` is its launcher** — and the exemplar of the contract
  (read one config → derive instance identity → select+parameterize a STATIC compose via profiles →
  create the external shm volume → run it). The dependency is strictly **one-way**: rig depends on this
  repo; this repo stays fully usable standalone and never imports or knows about rig. `gige-up` stays
  gige-specific (it only brings up gige camera stacks; it is not generalized to other services). The
  contract is four small things:
  (1) `gige-up <config> {up -d | down | ps | logs | config}` operates on ONE sensor config;
  (2) the config may live at an **arbitrary host path** — gige-up auto-detects a config outside
  `core-driver/config/sensors/`, and a small overlay (`docker-compose.external-config.yml`) bind-mounts
  that single (self-contained) file at an absolute in-container path (`/run/gige-sensor.yaml`), pointing
  `GIGE_CONFIG` there. It mounts OUTSIDE the read-only `/app/config` mount on purpose: Docker can't
  create a nested mount point inside a `:ro` mount. In-repo configs take no overlay and are byte-for-byte
  unchanged. One file suffices because a sensor config is self-contained (`load_config` reads one YAML +
  dataclass defaults; it doesn't merge `camera.yaml`);
  (3) the ros2-bridge passes through **`ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION`** so every stack shares one
  DDS graph (rig exports them; the defaults — domain 0, `rmw_fastrtps_cpp`, the ROS 2 default RMW — are
  correct standalone too);
  (4) a repo-root **`deploy.yaml`** descriptor tells rig how to invoke the launcher (logical verbs →
  compose subcommands, `ros_distro`) — descriptive metadata only, not code this repo runs.
  A core-driver **HEALTHCHECK** (the shm transport socket exists) makes `rig status` (= `docker compose
  ps`) report real health instead of "n/a". Invariants kept throughout: `ipc:host` + the external
  `gige_<name>_sock` volume + host networking, so other stacks still read a camera's frames by mounting
  that volume + `--ipc=host`; static compose + profiles (no generated compose, no pod-scoping).

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

- **Aravis / camera quirks:** cameras vary, so the driver handles the common ones generically (FLIR
  cited as a frequent example). Some cameras expose PTP via the legacy `GevIEEE1588*` names rather than
  SFNC `Ptp*` — we try both. Aravis **cannot decode** vendor-proprietary on-camera compression or
  standard GEV JPEG/H.264 payloads → run uncompressed on the wire (`ImageCompressionMode=Off`, jumbo
  frames). Need **Aravis ≥0.8.23** (prefer ≥0.8.32) for extended-chunk support. `arv_buffer_get_data()` returns image **+ chunks**
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
