# Camera-service tests

CI runs on pull requests, pushes to `main` and `jp6-modern`, and manual dispatch. It builds
the repository's development image for two native Linux runtimes: Ubuntu 22.04 / GStreamer
1.20, and Ubuntu 26.04 / pinned GStreamer 1.28.7. Neither requires a camera, GPU, ROS stack,
external Zenoh router, or saved field recording. Image build caches are separate per runtime.

## Run the CI checks locally

From the repository root, with Docker running:

```sh
docker build -f core-driver/Dockerfile.dev -t cam-dev:dev .
CAM_DEV_IMAGE=cam-dev:dev CAM_EXPECT_GST=1.28.7 bash tools/ci_test.sh

docker build -f core-driver/Dockerfile.dev --target distro \
  --build-arg BASE=ubuntu:22.04 -t cam-ci:legacy .
CAM_DEV_IMAGE=cam-ci:legacy CAM_EXPECT_GST=1.20 \
  CAM_TEST_REPORT_DIR=/tmp/cam-ci-legacy bash tools/ci_test.sh

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
| `test_replay_recording_changes.py` | Stop/configure/start sessions can change raw recording codecs, quality and Bayer tiling without changing frame IDs, timestamps, gaps, or downstream image correspondence. |
| `test_replay_integrity.py` | Missing parts, truncated video, mismatched CSV counts and known failed recordings cannot complete as faithful playback or loop forever. Unindexed frames receive no invented timestamps. A damaged replay exits the service nonzero. Legacy summaries that undercounted the last fragment still replay when all data is present. |
| `test_recording_resilience.py` | Muxer/CSV/header write failures preserve preview and transport; audit-write failures refuse activation; a stalled recorder has bounded queues and accounts for drops; recording can subsequently restart. Two real Zenoh clients cannot overwrite one settings revision or activate under an unreviewed revision. |
| Repeated-session test | 110 live start/configure/stop cycles preserve existing audit files, stop writer threads, release encoders and keep file descriptors, threads and current RSS bounded after warmup. This is a short leak regression, not a long-duration hardware soak. |
| `test_nvenc_validation.py` | The hardware test's verdict rejects missing/extra/partial frames, changed pixels and failed encoder/decoder processes. These tests replace gst-launch; they do not establish GPU correctness. |

The live resilience fixture generates deterministic source bytes on a separate thread and
uses real GStreamer recording, preview and shm/unixfd consumer pipelines. Disk failures are
injected at the writer or GStreamer bus boundary; the tests never fill the host's disk.

CI additionally runs `replay_test.sh`, `usb_test.sh`, `pcap_test.sh`, `lifecycle_test.sh`,
`rtsp_test.sh`, and `rtsp_reconnect_test.sh` from `core-driver/tools` against both images.
These exercise the actual service process with synthetic USB/RTSP sources and captures,
including signal shutdown, recording sessions, Zenoh lifecycle calls and network recovery.
Each script accepts `CAM_DEV_IMAGE` and has a three-minute CI deadline; logs are retained.
The lifecycle smoke test has loopback-only networking and selects its exact Zenoh service key,
so discovery cannot choose or control a service in another concurrently running container.

Real GigE/USB devices, NVENC/NVDEC, Orin/Thor compatibility, ROS/WebRTC browser end-to-end
behavior, and representative multi-camera CPU/latency measurements still need their separate
hardware or full-stack checks. In particular, `nvenc_lossless_test.py` uses software decoding;
its stricter frame-count check does not qualify hardware decoding.
