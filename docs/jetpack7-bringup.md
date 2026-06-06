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
The `cam-dev` image is plain Ubuntu + apt GStreamer/Aravis (no NVIDIA), so it runs anywhere on arm64 and
exercises the pipeline logic end-to-end.
```bash
git clone https://github.com/christomaszewski/camera-service && cd camera-service
docker build -f core-driver/Dockerfile.dev -t cam-dev .
./core-driver/tools/dev_test.sh            # fake camera -> timestamp -> FFV1 -> shm -> probe round-trip
```
Green here = capture/timestamp/record(FFV1)/transport all work on the Orin. (Aravis from apt on `cam-dev`
may be older than 0.8.32, so chunk/PTP needs the core image in L2 — that's fine, L1 is just a smoke test.)

## L2 — Real camera + the PTP experiment (software FFV1, still no NVENC dependency)
Build the **core** image — it builds Aravis 0.8.34 from source (full extended-chunk support). Builds on any
24.04 base; the build does **not** need `nvv4l2` (that's runtime-only):
```bash
# A plain 24.04 base is enough -- NVENC goes through the v4l2/CDI path, not CUDA (confirmed on JP7.2).
docker build -f core-driver/Dockerfile -t cam-core --build-arg BASE_IMAGE=ubuntu:24.04 .
docker run --rm cam-core gst-inspect-1.0 aravissrc | head -1    # Aravis built OK
```
Point a config at your camera with the **software** recorder (decouples this from the NVENC unknown):
copy `core-driver/config/camera.yaml`, set `camera.fake: false` + `camera.camera_id: <serial>`, and
`recording.encoder: ffv1`. Then run the core against the camera:
```bash
docker run --rm --network host --ipc=host \
  -v "$PWD/core-driver/config:/app/config:ro" -v "$PWD/recordings:/data/recordings" \
  cam-core supervisor.py -c config/<my-cam>.yaml -v
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
# NB: the cam-core ENTRYPOINT is python3 (it's a supervisor runner), so non-python tools need
# --entrypoint to bypass it -- else `cam-core gst-inspect-1.0 ...` runs `python3 gst-inspect-1.0`.
docker run --rm --device nvidia.com/gpu=all --entrypoint gst-inspect-1.0 cam-core nvv4l2h265enc | head -3   # "V4L2 H.265 Encoder"
```
Then run the HW lossless recorder — set `recording.encoder: auto` (8-bit → `hw-hevc-lossless`, NV24/NVMM)
and run the core with the CDI device (`--device nvidia.com/gpu=all`, **not** `--runtime nvidia` — the
nvidia runtime no longer injects multimedia on JP7):
```bash
docker run --rm --device nvidia.com/gpu=all --network host --ipc=host \
  -v "$PWD/core-driver/config:/app/config:ro" -v "$PWD/recordings:/data/recordings" \
  cam-core supervisor.py -c config/<my-cam>.yaml -v
```
Prove bit-exact (the part that matters — a "lossless" codec over a range-scaled Y plane is *not*
lossless): `./core-driver/tools/nvenc_lossless_test.sh` pushes known random GRAY8 frames through the
exact HW encode fragment, decodes them, and compares byte-for-byte. **Validated on a JP7.2 Orin AGX
(driver R595.78): `NVENC LOSSLESS PASS` — 60/60 frames bit-exact, worst |Δ| = 0, 1.32× raw** (noise
is incompressible, so >1× is itself proof nothing was discarded; full [0,255] range survives NV24).
Baked-in gotchas: the core image installs `kmod` (libnvtvmr runs `lsmod` during NVENC init, else it
aborts with a generic error), and the plugin lands in the standard gstreamer dir so no `GST_PLUGIN_PATH`
change is needed. (Harmless `(Argus) … nvargus-daemon failed` lines during plugin scan are the unused CSI
camera plugin — ignore them.)

## L4 — Full per-sensor stack
On a JP7 host `cam-up` **auto-detects** JetPack 7 (`/etc/nv_tegra_release` shows R39) and applies the
runc + CDI overlay (`docker-compose.jp7.yml`) plus the 24.04 build base — so plain `cam-up <config> up -d`
already does the right thing. `--jp7` only *forces* it, and `CAM_PLATFORM=jp7` pins it (how rig selects
per host). Build *through* cam-up so the base is set for you, then bring it up:
```bash
./cam-up config/sensors/<my-cam>.yaml build core-driver   # JP7 host -> auto CAM_CORE_BASE=ubuntu:24.04
./cam-up config/sensors/<my-cam>.yaml up -d
./cam-up config/sensors/<my-cam>.yaml ps                  # health column should read 'healthy'
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
  `CAM_CORE_BASE` is a one-flag swap.
- **GPU/multimedia injection:** **CDI**, not the JP6 CSV mounts (the old `l4t.csv` is gone). `nvidia-ctk
  cdi generate` once, then run with `--device nvidia.com/gpu=all` (Docker's native CDI; default `runc`
  runtime). The generated spec carries the full multimedia stack incl. `nvunixfd`.
- **sm arch:** Orin stays **sm_87** under JP7 (no rebuild). `sm_110` is a Thor/Blackwell concern only — and
  this repo ships no custom CUDA anyway.
