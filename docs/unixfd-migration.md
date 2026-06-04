# Planned: unixfd transport (drop the custom shm header) — JP7

**Status: planned, not started.** Tracked here so the design isn't lost. The `gige-up` platform
auto-detect (jp6/jp7) is the natural gate — `unixfd` needs **GStreamer ≥ 1.24**, which is JP7 (Ubuntu
24.04). JP6 (Ubuntu 22.04 / GStreamer 1.20) has no `unixfd`, so it keeps the shm+header transport.

## Why
Today the plugin transport is `shmsink`/`shmsrc` carrying a custom 36-byte `application/x-gige-frame`
header (shm drops caps + PTS + GstMeta, so we prepend our own). That header is the root of several
hacks. `unixfd` (gst-plugins-bad, 1.24) passes buffers over a Unix socket via SCM_RIGHTS **with native
caps + serialized GstMeta**, so the header disappears.

## Design: additive, not a fork
Keep **shm+header as the universal transport on BOTH platforms** — it's the only thing non-GStreamer /
out-of-pod consumers can read (`mmap` + a documented header), which is a hard requirement. Add `unixfd`
as an **extra tee branch on JP7**, opt-in, for GStreamer-native consumers. So the JP7 core publishes
*both*; consumers pick. No consumer is forced onto unixfd.

- **core-driver** genuinely differs by platform (jp6 = l4t/1.20, jp7 = 24.04/1.24), so the JP7 image is
  the natural home for the unixfd branch (runtime-selected in Python by GStreamer version / platform).
- **ros2-bridge** is ONE image (`ros:lyrical` = 24.04/1.24) on both hosts, so it's *one image with a
  runtime transport switch* (shm on a jp6 host, unixfd on a jp7 host), not two images.

## What it unlocks / changes (the checklist)
- [ ] **Header-free bridge pipeline:** `unixfdsrc ! video/x-bayer ! [bayer2rgb] ! appsink` (vs today's
      `shmsrc ! application/x-gige-frame ! appsink` + C++ header parse).
- [ ] **Self-describing format (caps):** the core retags the *transport* tee branch
      `GRAY8 -> video/x-bayer,format=<rggb|grbg|gbrg|bggr>` (a `capssetter`; same bytes; the **recorder
      keeps its GRAY8 Y-plane trick untouched**). Mono stays `video/x-raw,GRAY8`. The bridge then reads
      the format off the negotiated caps -> **drops the `GIGE_ROS_ENCODING` config plumbing** (it only
      exists because the 36-byte header can't carry "this is Bayer rggb").
- [ ] **Color option B becomes a real GStreamer element:** in-pipeline `bayer2rgb` (bilinear, full-res)
      replaces the interim C++ 2x2-cell `demosaic_rgb8()`. (Option A — publish `bayer_*` and let
      `image_proc` debayer — is unchanged and already full quality.)
- [ ] **Timestamp via meta:** `GstReferenceTimestampMeta` (serialized by unixfd) carries the PTP capture
      time + frame_id beside the frame, replacing the header fields.
- [ ] **Drops `--ipc=host`** for unixfd consumers (SCM_RIGHTS fd-passing isn't IPC-namespace-scoped).

## Validated on GStreamer 1.24 (2026-06-04, in Docker)
- ✅ `unixfdsink`/`unixfdsrc` exist on 1.24; **`video/x-bayer,format=rggb` caps cross intact** + data
  flows -> the bridge reads the Bayer format off the negotiated caps (drops the `GIGE_ROS_ENCODING`
  config plumbing).
- ⚠️ **Don't put the absolute timestamp in `buffer.pts`** -- a ~56-year "future" epoch-ns PTS stalls
  downstream flow even at `sync=false`. Carry it in `buffer.offset_end` (+ `offset` = frame_id); those
  are native buffer fields that serialize with the buffer and aren't time-interpreted. PTS stays relative.
- ❗ **`unixfdsink` requires FD-backed (memfd) buffers** (`Expecting buffers with FD memories`). A plain
  `tee` tap of regular CPU buffers ERRORS -- the tee can't negotiate the memfd allocation across its
  branches, and `videoconvert` doesn't help. So the **producer must allocate memfd buffers itself**
  (`os.memfd_create` + `GstAllocators.FdAllocator`, confirmed available in the core image) and copy the
  frame in -> a **separate unixfd appsrc**, not a tee branch.

## The zero-copy reality (DECISION POINT)
The memfd requirement means **plain unixfd is NOT zero-copy for CPU camera (Aravis) frames** -- it's a
producer-side memfd copy, ~the same cost as shm. Its win is *cleanliness* (native caps + metadata, no
header, in-pipeline `bayer2rgb`), not speed. **True** zero-copy needs frames already fd/dmabuf-backed,
which on the Jetson means **NVMM** (GPU) + **nvunixfd** (DeepStream 8). Two directions:
- **A (memfd-copy unixfd):** clean transport now; modest; the foundation for B. ~shm cost.
- **B (NVMM + nvunixfd):** tap the recorder's existing `nvvidconv -> NVMM` for the transport branch +
  `nvunixfd` -> real GPU zero-copy. Bigger lift (DeepStream dependency), but it's what "ensure zero-copy"
  actually requires.

## Bigger follow-on (separate): nvunixfd / zero-copy GPU
Plain `unixfd` is CPU memfd (≈ same copy cost as shm — the win is *cleanliness*, not speed). The actual
bandwidth prize is **`nvunixfd`** (DeepStream 8): `NvBufSurface` DMABUF across containers, true zero-copy
GPU→GPU. Worth it only when a consumer is bandwidth-bound (4K, multiple readers). Scope separately.

## Interim (header era) — what exists today and why
Until this lands, color uses: a config-derived `GIGE_ROS_ENCODING` (option A) + a simple C++ demosaic
(option B). Deliberately interim — both are replaced by the caps-driven, `bayer2rgb`-in-pipeline version
above. So **don't over-invest in the C++ demosaic's quality**; it retires with this migration.
