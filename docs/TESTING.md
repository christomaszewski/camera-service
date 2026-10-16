# Camera-service tests

CI runs on pull requests, pushes to `main` and `jp6-modern`, and manual dispatch. It builds
the repository's development image for three native Linux runtimes: Ubuntu 22.04 / GStreamer
1.20, Ubuntu 24.04 / GStreamer 1.24, and Ubuntu 26.04 / pinned GStreamer 1.28.8. None requires
a camera, GPU, ROS stack, external Zenoh router, or saved field recording. Image build caches
are separate per runtime.

## Run the CI checks locally

From the repository root, with Docker running:

```sh
docker build -f core-driver/Dockerfile.dev -t cam-dev:dev .
CAM_DEV_IMAGE=cam-dev:dev CAM_EXPECT_GST=1.28.8 bash tools/ci_test.sh

docker build -f core-driver/Dockerfile.dev --target distro \
  --build-arg BASE=ubuntu:22.04 -t cam-ci:legacy .
CAM_DEV_IMAGE=cam-ci:legacy CAM_EXPECT_GST=1.20 \
  CAM_TEST_REPORT_DIR=/tmp/cam-ci-legacy bash tools/ci_test.sh

docker build -f core-driver/Dockerfile.dev --target distro \
  --build-arg BASE=ubuntu:24.04 -t cam-ci:ubuntu24 .
CAM_DEV_IMAGE=cam-ci:ubuntu24 CAM_EXPECT_GST=1.24 \
  CAM_TEST_REPORT_DIR=/tmp/cam-ci-ubuntu24 bash tools/ci_test.sh

python3 tools/test_dev_defaults.py
```

The Python suite mounts the checkout read-only, installs test-only dependencies in its
disposable container, and writes JUnit, text, JSON, and XML coverage reports to `test-results/`
(override with `CAM_TEST_REPORT_DIR`). CI uploads these reports even when tests fail and
includes coverage in the job summary. Skipped tests fail the gate; every test has a 45-second
deadline, and the entire pytest process has a five-minute deadline.

Coverage includes branches in `cam_driver` and the WebRTC bridge's Python tools, excluding
test code. The report deliberately includes unexecuted hardware/entry-point modules. Separate
service processes in the smoke tests are not included in these percentages. Establish and
inspect this baseline before setting a coverage threshold; a high percentage cannot establish
pixel fidelity or hardware support.

Local Linux/arm64 baseline (2026-09-17): 445 tests pass without skips on each runtime.
For `cam_driver` alone, measured line/branch coverage is 76.7%/70.4% on 1.28.7 and
73.2%/66.1% on 1.20.3. These figures exclude `main.py`, service subprocesses and bridge tools;
use the complete uploaded report when comparing the whole measured suite.

The platform-default test runs on the host, using Python/PyYAML and Docker Compose. It only
renders configuration and captures build commands; it does not launch vehicle services.

## Regression contracts

| Suite | What must remain true |
|---|---|
| `test_ffv1_decode.py` | FFV1 EOS preserves every frame, timestamp and pixel for GRAY8, GRAY16_LE, I420 and NV12, including one-frame files and both unsliced and four-slice bitstreams. |
| `test_replay_recording_changes.py` | Stop/configure/start sessions can change raw recording codecs, quality and Bayer tiling without changing frame IDs, timestamps, gaps, or downstream image correspondence. |
| `test_replay_integrity.py` | Missing parts, truncated video, mismatched CSV counts and known failed recordings cannot complete as faithful playback or loop forever. Unindexed frames receive no invented timestamps. A damaged replay exits the service nonzero. Legacy summaries that undercounted the last fragment still replay when all data is present. |
| `test_recording_resilience.py` | Muxer/CSV/header write failures preserve preview and transport; audit-write failures refuse activation; a stalled recorder has bounded queues and accounts for drops; recording can subsequently restart. Two real Zenoh clients cannot overwrite one settings revision or activate under an unreviewed revision. |
| Repeated-session test | 110 live start/configure/stop cycles preserve existing audit files, stop writer threads, release encoders and keep file descriptors, threads and current RSS bounded after warmup. This is a short leak regression, not a long-duration hardware soak. |
| `test_nvenc_validation.py` | The hardware test's verdict rejects missing/extra/partial frames, changed pixels and failed encoder/decoder processes. These tests replace gst-launch; they do not establish GPU correctness. |

The live resilience fixture generates deterministic source bytes on a separate thread and
uses real GStreamer recording, preview and shm/unixfd consumer pipelines. Disk failures are
injected at the writer or GStreamer bus boundary; the tests never fill the host's disk.

CI additionally runs `replay_test.sh`, `usb_test.sh`, `pcap_test.sh`, `lifecycle_test.sh`,
`rtsp_test.sh`, and `rtsp_reconnect_test.sh` from `core-driver/tools` against all three images.
These exercise the actual service process with synthetic USB/RTSP sources and captures,
including signal shutdown, recording sessions, Zenoh lifecycle calls and network recovery.
Each script accepts `CAM_DEV_IMAGE` and has a three-minute CI deadline; logs are retained.
The lifecycle smoke test has loopback-only networking and selects its exact Zenoh service key,
so discovery cannot choose or control a service in another concurrently running container.

Real GigE/USB devices, NVENC/NVDEC, Orin/Thor compatibility, ROS/WebRTC browser end-to-end
behavior, and representative multi-camera CPU/latency measurements still need their separate
hardware or full-stack checks. In particular, `nvenc_lossless_test.py` uses software decoding;
its stricter frame-count check does not qualify hardware decoding.

## Media dependency update: 2026-10-16

Candidate branch: `codex/gstreamer-1.28.8`. Keep this branch separate until the intended
Jetson platforms pass the hardware checks below. The pins are GStreamer **1.28.8** (all seven
source archives, with updated SHA-256 checksums), gst-plugins-rs **0.15.4** (WebRTC, RTP and
the signalling server), and source-built Aravis **0.8.36**.

The GStreamer source overlay applies to the modern development images. Vehicle images still
use their distro GStreamer: JP6 core 1.20, Ubuntu 24.04 core/bridge 1.24, and Ubuntu 26.04
core/bridge 1.28. The development core still uses distro Aravis. Updating these pins does not
replace NVIDIA's plugins or enable H.264 High profile; the bridge retains its baseline fallback.

### Initial pin-bump evidence (`f8ba7ad`)

Built and tested natively as Linux/arm64 containers on Docker Desktop on an arm64 Mac.
Candidate images use separate tags; existing development and vehicle tags were preserved.
Reports and logs are in the ignored `test-results/gstreamer-1.28.8/` directory.

| Check | Result |
|---|---|
| `cam-dev:gst-1.28.8` | Built all seven GStreamer source modules; plugin and Python typelib checks passed. |
| `webrtc-bridge:gst-1.28.8` | Built; runtime reports GStreamer 1.28.8 and both Rust plugins at `0.15.4-1afff37`. |
| `cam-core:aravis-0.8.36-jp7` (Ubuntu 24.04, no NVIDIA injection) | Built; Aravis typelib imports and `aravissrc` reports 0.8.36. Distro GStreamer is 1.24.2. |
| `webrtc-bridge:rs-0.15.4-jp7` (Ubuntu 24.04, no NVIDIA injection) | Built; runtime reports GStreamer 1.24.2 and both Rust plugins at `0.15.4-1afff37`. |
| Full modern runtime suite | 525 passed, no skips on the completed rerun. The old 1.28.7 image also passed all 525. |
| Full legacy runtime suite | 525 passed, no skips on GStreamer 1.20.3 after compilation finished. An earlier run during builds had one shm shutdown timeout. |
| Full Ubuntu 24.04 production suite | 511 passed, 14 failed with Aravis 0.8.36; resolved by the subsequent FFV1 decoder fix below. |
| Configuration/build defaults | 5 tests passed, including vehicle Rust defaults and explicit overrides. |
| Service smoke tests on 1.28.8 | Replay, USB, PCAP, lifecycle, RTSP and RTSP reconnect all passed. Sources were synthetic. |
| Modern WebRTC | All 5 applicable headless scenarios passed: raw shm, H.264 baseline, High-profile fallback, raw downscaling and unixfd Bayer downscaling. |
| Production WebRTC | All 5 applicable scenarios passed with the new Ubuntu 24.04 core and bridge together. |
| Legacy-core WebRTC interoperability | All 6 applicable scenarios passed with the GStreamer 1.20 core and new Ubuntu 24.04 bridge, including self-describing mono/Bayer headered shm, latency overlay and downscaling. Across the three combinations, all 16 headless scenarios passed. |
| ROS 2 interoperability | Existing `ros2-bridge:dev` consumed the new core's unixfd stream; frame count, encoding and capture timestamps passed. |
| Production Aravis | Fake-camera capture, stale socket recovery, FFV1 fallback/recording and EOS decode passed. Killing/restarting the GVSP emitter triggered reconnection, resumed capture and a decodable recording. |

The first modern suite run had two failures in `test_replay_recording_changes.py` involving
decoded-frame/CSV counts. Both passed in isolation on old and new images; the full old/new
comparison passed, followed by 20 repetitions of each affected test on 1.28.8 (40 cases,
zero failures). The initial failures remain recorded; their cause was not established.

### Ubuntu 24.04 FFV1 fix

All 14 original Ubuntu 24.04 failures also reproduced with the old Aravis pin. Investigation
isolated the loss to `avdec_ffv1`'s automatic frame threading in gst-libav 1.24.2: a minimal
five-frame file produced all five encoded packets, and all five entered the decoder, but only
four decoded frames left it. This happened in 50/50 default-decoder runs; selecting slice
threading preserved all five in 50/50 runs. Upstream lists a related multithreaded I-frame
decoder fix in [GStreamer 1.24.4](https://gstreamer.freedesktop.org/releases/1.24/).

Replay and recording validators now use `avdec_ffv1 thread-type=slice`, with the same explicit
setting in command-line checks. Slice threading retains parallel decoding for the recorder's
four-slice FFV1 files. Legacy unsliced files remain readable but cannot use slice parallelism.
The recording format, frame-count checks and byte-exact assertions are unchanged. This avoids
the affected decoder mode while retaining the vehicle's distro GStreamer/NVIDIA stack.

The 24 new pixel/PTS/EOS regression cases produced six failures with the old default decoder
setting and all passed with the fix. Ubuntu 24.04 is now a permanent CI lane alongside 1.20 and
1.28.8. New results live in `test-results/replay-1.24-fix/`; the original failing reports remain
in `test-results/gstreamer-1.28.8/`. The simulated `ENOSPC` messages in resilience tests are
intentional fault injection, not host disk exhaustion.

Full-suite results after the fix, run sequentially on Linux/arm64 (no skips or failures):

| Runtime | Result |
|---|---|
| Production Ubuntu 24.04, Aravis 0.8.36, GStreamer 1.24.2 | 549 passed |
| New CI Ubuntu 24.04 distro image, GStreamer 1.24.2 | 549 passed |
| Legacy Ubuntu 22.04, GStreamer 1.20.3 | 549 passed |
| Modern Ubuntu 26.04, pinned GStreamer 1.28.8 | 549 passed |

All six service smoke tests passed on Ubuntu 24.04: replay, synthetic USB, PCAP, lifecycle,
RTSP and RTSP reconnect. The complete replay smoke test also passed on legacy and modern
runtimes, including byte-exact recording/replay and stream-copy checks.

The production core was rebuilt as `cam-core:replay-fix-jp7`; its installed replay/format
modules match the tested checkout. Candidate image identities are recorded alongside the
test reports. This branch remains separate pending the Jetson checks below.

### Jetson promotion gate

Use the candidate branch and distinct candidate image tags/digests for every deployed platform.
Follow [the hardware procedure](hardware-test-procedure.md),
[JP6 modern-userspace probes](jp6-modern-userspace.md) and
[JP7 bring-up](jetpack7-bringup.md), recording the actual image digests and JetPack/L4T versions.

1. Build the exact vehicle variants, including the matching L4T multimedia layer where used.
   Check plugin loading under CSV/CDI and inspect `aravissrc`, `webrtcsink`, `rtpgccbwe`,
   `nvvidconv`, `nvv4l2h264enc` and `nvv4l2h265enc`. Confirm the intended encoder is selected.
2. Exercise the real GigE and USB cameras at deployed formats/rates: discovery, chunk selectors,
   ChunkTimestamp/FrameID, PTP lock and timestamp provenance, packet loss and unplug/reconnect.
   Check recorded pixels, frame counts and sidecar alignment, including 16-bit/Bayer paths.
3. Run `tools/nvenc_lossless_test.py --frames 60` inside the candidate core with the platform's
   NVIDIA runtime/device arguments. Require all frames and bit-exact pixels. Separately validate
   NVDEC, NVMM conversion and recordings at actual camera dimensions/rates.
4. Test the dashboard in the browsers used on the vehicle, with real NVIDIA H.264 encoding,
   multiple viewers, reconnects and constrained network bandwidth. Verify congestion control
   changes bitrate and latency recovers. Exercise H.265 setup/bitrate handling if enabling that
   new upstream capability; the existing default H.264 tests do not qualify it.
5. Check JP6 headered shm and modern unixfd consumers, ROS timestamps/rates, fleet discovery,
   recording stop/configure/start, replay and clean shutdown in the complete rig deployment.
   Run a representative multi-camera soak and compare CPU, memory, drops, disk rate and p95
   latency with the existing deployment before promoting the branch.
