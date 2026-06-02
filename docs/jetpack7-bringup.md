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
# A plain 24.04 base is enough -- NVENC goes through the v4l2/CDI path, not CUDA (confirmed on JP7.2).
docker build -f core-driver/Dockerfile -t gige-core --build-arg BASE_IMAGE=ubuntu:24.04 .
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

## L3 — NVENC HW lossless recorder (CONFIRMED on a JP7.2 Orin AGX)
JP7 replaced JP6's CSV-mount model with **CDI**. One-time host setup, then the encoder is reachable in any
24.04 container via a CDI device (validated on JP7.2 — `nvv4l2h265enc`, `nvvidconv`, NvBufSurface,
`/dev/v4l2-nvenc`, and even `nvunixfd` are all injected, into the standard plugin dir):
```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml      # once (re-run after a JetPack update)
docker run --rm --device nvidia.com/gpu=all gige-core gst-inspect-1.0 nvv4l2h265enc | head -3   # "V4L2 H.265 Encoder"
```
Then run the HW lossless recorder — set `recording.encoder: auto` (8-bit → `hw-hevc-lossless`, NV24/NVMM)
and run the core with the CDI device (`--device nvidia.com/gpu=all`, **not** `--runtime nvidia` — the
nvidia runtime no longer injects multimedia on JP7):
```bash
docker run --rm --device nvidia.com/gpu=all --network host --ipc=host \
  -v "$PWD/core-driver/config:/app/config:ro" -v "$PWD/recordings:/data/recordings" \
  gige-core supervisor.py -c config/<my-cam>.yaml -v
```
Prove bit-exact: decode the mkv and check `ffmpeg … -lavfi psnr` = `inf` (or `framemd5` equality).
Baked-in gotchas: the core image installs `kmod` (libnvtvmr runs `lsmod` during NVENC init, else it
aborts with a generic error), and the plugin lands in the standard gstreamer dir so no `GST_PLUGIN_PATH`
change is needed. (Harmless `(Argus) … nvargus-daemon failed` lines during plugin scan are the unused CSI
camera plugin — ignore them.)

## L4 — Full per-sensor stack
`gige-up --jp7` adds the CDI-device overlay (`docker-compose.jp7.yml`) so the composed core gets NVENC
the same way the manual L3 run does. Build the core with a 24.04 base, then:
```bash
GIGE_CORE_BASE=ubuntu:24.04 docker compose -f docker-compose.yml build core-driver   # build once with the JP7 base
./gige-up --jp7 config/sensors/<my-cam>.yaml up -d
./gige-up --jp7 config/sensors/<my-cam>.yaml ps        # health column should read 'healthy'
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

## Notes (resolved on a JP7.2 Orin AGX, driver R595.78)
- **Base image:** a plain `ubuntu:24.04` works — the NVENC path is v4l2/CDI, not CUDA. Use a CUDA base
  (`nvcr.io/nvidia/cuda:13.x-devel-ubuntu24.04`) only if you add custom CUDA processing. `BASE_IMAGE` /
  `GIGE_CORE_BASE` is a one-flag swap.
- **GPU/multimedia injection:** **CDI**, not the JP6 CSV mounts (the old `l4t.csv` is gone). `nvidia-ctk
  cdi generate` once, then run with `--device nvidia.com/gpu=all` (Docker's native CDI; default `runc`
  runtime). The generated spec carries the full multimedia stack incl. `nvunixfd`.
- **sm arch:** Orin stays **sm_87** under JP7 (no rebuild). `sm_110` is a Thor/Blackwell concern only — and
  this repo ships no custom CUDA anyway.
