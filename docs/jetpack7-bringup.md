# Bring-up on a JetPack 7.2 Orin AGX

This repo was built and container-validated targeting **JetPack 6**; **nothing has run on real Jetson
hardware yet**. JetPack **7.2** (~June 2026, L4T r39.x) brings JetPack 7 to the **Orin AGX/NX** — Ubuntu
24.04, GStreamer 1.24, CUDA 13 — so this is the first on-hardware bring-up *and* a JP7 port at once.

The plan below validates in **layers**, software-first, so you make progress immediately and isolate the
one genuine JP7 unknown — **how the NVIDIA multimedia stack (`nvv4l2*`) reaches a container on JP7** (the
cloud-native model changed the base image; the runtime injection may have moved from CSV mounts to CDI).
The high-value hardware tests (real camera, PTP/chunk timestamps) do **not** depend on that unknown.

Order: **L0 host sanity → L1 fake-camera smoke → L2 real camera + PTP (software FFV1) → L3 NVENC HW
recorder → L4 full per-sensor stack.** Stop and report at the first layer that fails.

---

## L0 — Host sanity (5 min)
```bash
dpkg -l | grep -i nvidia-jetpack          # confirm 7.2; or: cat /etc/nv_tegra_release  (expect r39.x)
docker info | grep -i runtime              # expect 'nvidia' present (install nvidia-container-toolkit if not)
nvidia-smi || sudo nvidia-ctk --version    # GPU/toolkit reachable
gst-inspect-1.0 nvv4l2h265enc 2>/dev/null && echo "nvenc on host: OK"   # the HW encoder exists on the host
```
Host prep for GigE (once, per the main README): NIC `mtu 9000`, `net.core.rmem_max`, and a PTP
grandmaster (`ptp4l`/`phc2sys`) if you'll test PTP.

## L1 — Fake-camera smoke (no camera, no NVENC, no JP7-base questions)
The `gige-dev` image is plain Ubuntu + apt GStreamer/Aravis (no NVIDIA), so it runs anywhere on arm64 and
exercises the pipeline logic end-to-end.
```bash
git clone https://github.com/christomaszewski/gige-vision-service && cd gige-vision-service
docker build -f core-driver/Dockerfile.dev -t gige-dev .
./core-driver/tools/dev_test.sh            # fake camera -> timestamp -> FFV1 -> shm -> probe round-trip
```
Green here = capture/timestamp/record(FFV1)/transport all work on the Orin. (Aravis from apt on `gige-dev`
may be older than 0.8.32, so chunk/PTP needs the core image in L2 — that's fine, L1 is just a smoke test.)

## L2 — Real camera + the PTP experiment (software FFV1, still no NVENC dependency)
Build the **core** image — it builds Aravis 0.8.34 from source (full extended-chunk support). Builds on any
24.04 base; the build does **not** need `nvv4l2` (that's runtime-only):
```bash
JP7_BASE=nvcr.io/nvidia/cuda:13.0.0-devel-ubuntu24.04     # adjust if NVIDIA's JP7 base differs (see notes)
docker build -f core-driver/Dockerfile -t gige-core --build-arg BASE_IMAGE="$JP7_BASE" .
docker run --rm gige-core gst-inspect-1.0 aravissrc | head -1    # Aravis built OK
```
Point a config at your camera with the **software** recorder (decouples this from the NVENC unknown):
copy `core-driver/config/camera.yaml`, set `camera.fake: false` + `camera.camera_id: <serial>`, and
`recording.encoder: ffv1`. Then run the core against the camera:
```bash
docker run --rm --network host --ipc=host \
  -v "$PWD/core-driver/config:/app/config:ro" -v "$PWD/recordings:/data/recordings" \
  gige-core supervisor.py -c config/<my-cam>.yaml -v
```
This is the **highest-value on-hardware test** and the [PTP timestamp experiment](ptp-timestamp-experiment.md):
- confirm `chunk mode enabled` + `Active timestamp source = ptp_chunk`, `GevIEEE1588Status=Slave`;
- the sidecar `*.csv` logs `chunk_ns` / `camera_ns` / `system_ns` side-by-side every frame — compare them
  (which is the authoritative capture time, and the real `system_ns − chunk_ns` arrival jitter);
- the FFV1 `*.mkv` decodes losslessly. None of this needs NVENC.

## L3 — NVENC HW lossless recorder (THE JP7-specific validation)
The one thing that genuinely needs verifying on JP7. **Key check — does `nvv4l2` reach the container?**
```bash
docker run --rm --runtime nvidia gige-core gst-inspect-1.0 nvv4l2h265enc
```
- **Found** → switch the config to `recording.encoder: auto` (8-bit → `hw-hevc-lossless`, NV24/NVMM) and
  re-run L2's command. Then prove bit-exact: decode the mkv and check `ffmpeg ... -lavfi psnr` = `inf`
  (or `framemd5` equality). That closes the NVENC on-hardware TODO.
- **NOT found** → JP7 plumbs multimedia differently than JP6's CSV mounts. Don't block: stay on
  `encoder: ffv1` (L2 already validated the whole pipeline). To fix the HW path, check on the device:
  - is there a JP7 `l4t-base`/`l4t-jetpack` image (r39.x) that ships/mounts the multimedia stack? try it
    as `BASE_IMAGE`;
  - did the toolkit move to **CDI** (`nvidia-ctk cdi generate` + `--device nvidia.com/gpu=all`) instead of
    `--runtime nvidia`?
  - what `gst-inspect-1.0 nvv4l2h265enc` reports on the **host** vs in the container tells you what mount/
    package is missing. Report findings and we'll pin the right JP7 base + runtime invocation.

## L4 — Full per-sensor stack
Once the core image is good, the rest is unchanged from the validated container model:
```bash
GIGE_CORE_BASE="$JP7_BASE" docker build ...      # (gige-up/compose build the core with this base)
./gige-up config/sensors/<my-cam>.yaml up -d
./gige-up config/sensors/<my-cam>.yaml ps        # health column should read 'healthy'
```
The plugins (ros2-bridge on Lyrical, webrtc-bridge) already run on Ubuntu 24.04, so they're unaffected by
the host JetPack and now match the core's userspace.

---

## What JP7 unlocks (once L3 is green) — optional, not required
- **`unixfd` transport** (GStreamer 1.24): carries PTS + GstMeta natively across the process boundary, so
  the 36-byte header becomes optional for same-host consumers. A localized swap of the publish-branch sink.
- **`nvunixfd` zero-copy GPU sharing** across containers (`NvBufSurface` DMABUF) — the original zero-copy
  goal, no longer Thor-gated. Biggest payoff for high-resolution multi-consumer setups.
Both are follow-ups to evaluate after the core pipeline is validated on JP7; ask and we'll prototype them.

## Notes / unknowns to confirm on the device
- **Base image:** `cuda:13.0.0-devel-ubuntu24.04` is the working assumption; NVIDIA's exact JP7 base for
  Jetson multimedia may be an `l4t-*` r39.x image — `BASE_IMAGE` / `GIGE_CORE_BASE` is a one-flag swap.
- **sm arch:** Orin stays **sm_87** under JP7 (no rebuild). `sm_110` is a Thor/Blackwell concern only — and
  this repo ships no custom CUDA anyway.
- **CUDA driver:** JP7.2 needs R595+ for SBSA CUDA on Orin (per NVIDIA) — already on the flashed image.
