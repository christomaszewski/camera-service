# Roadmap & status

Where the project is, what's validated, what's left, and how to resume. Pair with
[DESIGN.md](DESIGN.md) (the why). Keep this updated as phases land.

## Phase status

| Phase | What | State |
|---|---|---|
| **P0** | Scaffold + Docker (l4t-jetpack core; ubuntu dev image) | ✅ done |
| **P1** | Timestamp spine — Aravis appsrc, PTP via `GevIEEE1588`, chunk `Timestamp`/`FrameID`, fallback ladder, sidecar CSV + JSON | ✅ done |
| **P2** | Pluggable lossless recorder (HW HEVC-lossless / FFV1 / x265), `splitmuxsink` | ✅ done (FFV1 path validated; NVENC needs hardware) |
| **P3** | Transport (shm header + optional raw endpoint), C++ `rclcpp` ros2-bridge (raw + lazy compressed `Image`), per-sensor supervisor | ✅ done |
| **P4** | **WebRTC** consumer (`webrtcsink`, lossy low-latency, remote viewing) | ✅ done (headless loopback validated; browser viewing + HW encoder need a Jetson) |
| **P5** | Hardening (reconnect, disk-full, NVENC session budget, NIC/PTP tuning) + on-Jetson / on-camera validation | ⬜ todo |

## Validated by actually running it (containers, no hardware)

Capture → PTP/chunk timestamping (**real Aravis chunk-parse path**) → **lossless** recording
(proven bit-exact via the round-trip) → shm transport with header → **C++ ROS2 bridge** (raw +
compressed `Image`, capture time in `header.stamp`) → **per-sensor supervisor** (spawn / manage /
clean teardown) → **WebRTC egress** (raw shm → `webrtcsink` → `webrtcsrc`, decoded frames counted).
Cross-container and cross-GStreamer-version (1.20↔1.24) shm both work.

**Test inventory** (each runs without a Jetson or camera):
- `core-driver/tools/dev_test.sh` — producer: fake camera → timestamp → FFV1 → shm
- `plugins/ros2-bridge/tools/bridge_test.sh` — full chain → ROS2 raw + compressed `Image`
- `core-driver/tools/supervisor_test.sh` — supervisor spawn / manage / teardown
- `tools/gvsp-chunk-emitter/gvsp_test.sh` — real GVSP + chunk-timestamp extraction (patched Aravis)
- `tools/gvsp-chunk-emitter/roundtrip_test.sh` — full input→output round-trip: known frames+timestamps →
  GVSP → recording, byte-compared (lossless + timestamp fidelity; random-noise frames)
- `plugins/webrtc-bridge/tools/webrtc_test.sh` — WebRTC egress: raw shm → `webrtcsink` → signalling →
  `webrtcsrc` decode, frames counted (headless, no browser)
- `python3 core-driver/tests/test_*.py` — pure-logic unit tests (transport wire format, config, timestamp ladder)

## Still needs the Orin / a real Blackfly S

- **NVENC HW recorder** — `nvv4l2h265enc enable-lossless`, NV24/NVMM caps, bit-exact round trip
  (`ffmpeg framemd5`/PSNR=inf). Software FFV1 is validated; HW path is written but unrun. (8-bit only.)
- **FLIR-specific PTP/chunk behaviour** — the [PTP timestamp experiment](ptp-timestamp-experiment.md):
  confirm `GevIEEE1588Status=Slave`, `GevTimestampTickFrequency`, which of `chunk_ns`/`camera_ns`/`system_ns`
  is the authoritative capture time, and the real arrival jitter. Verify the exact chunk node names
  (`arv-tool-0.8 features`).
- **Packed pixel formats** (Mono10p/Mono12Packed) — need a bit-unpack step (not implemented; a warning
  fires if detected).
- **Host/deploy** — NIC MTU 9000 + `net.core.rmem_max`, `ptp4l`/`phc2sys` grandmaster, `default-runtime: nvidia`.

## How to resume (for a future session)

1. Read [DESIGN.md](DESIGN.md) (architecture + decisions) and this file.
2. Build + run the test suites above to confirm the current state is green (`docker build` the four
   images: `gige-dev`, `ros2-bridge`, `gige-chunks`, `webrtc-bridge`, then the `*_test.sh` scripts).
3. Recalled memory (this machine's Claude) holds the same facts in condensed form; the in-repo docs
   are canonical and shareable.

## Decision history (one-liners — see DESIGN.md for rationale)

- Custom Aravis `appsrc` (not `aravissrc`) for frame_id + chunk access.
- Chunk timestamp used whenever available; PTP-lock is provenance (`ptp_synced`).
- Recorder pluggable: 8-bit → HW HEVC-lossless, >8-bit → FFV1 (intra) / x265 (temporal).
- Transport = shm + 36-byte header (shm drops PTS/metas); optional raw endpoint; `unixfd`/Zenoh later.
- Python core + C++ ros2-bridge; one container per sensor; supervisor spawns plugins.
- SEI declined (use the CSV sidecar; RTP header extension for the streaming path).
- WebRTC egress = gst-plugins-rs `webrtcsink` (built from source), sibling container on the raw shm
  endpoint; encodes internally; needs `gstreamer1.0-nice` + `shmsrc do-timestamp` + I420 conversion.
- Zenoh kept as the future data-fabric transport (swappable in).
