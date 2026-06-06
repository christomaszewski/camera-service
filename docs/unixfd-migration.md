# unixfd transport (drop the custom shm header) — JP7

**Status: option A (memfd-copy unixfd) IMPLEMENTED in the core driver + validated in Docker
(2026-06-04).** Option B (NVMM + nvunixfd, true GPU zero-copy) remains future work. The capability gate
is runtime, not build-time: `pipeline.build()` selects `unixfdsink` when `Gst.ElementFactory.find(
"unixfdsink")` succeeds — true on **GStreamer ≥ 1.24** (JP7 / Ubuntu 24.04), false on JP6 (Ubuntu 22.04 /
GStreamer 1.20), which keeps the shm+header transport. So one core codebase, the right transport per host.

## Why
Today the plugin transport is `shmsink`/`shmsrc` carrying a custom 36-byte `application/x-cam-frame`
header (shm drops caps + PTS + GstMeta, so we prepend our own). That header is the root of several
hacks. `unixfd` (gst-plugins-bad, 1.24) passes buffers over a Unix socket via SCM_RIGHTS **with native
caps + serialized GstMeta**, so the header disappears.

## Design (as built): unixfd REPLACES the header endpoint on JP7
The original sketch here was "publish both" (shm+header *and* unixfd, additive). That was dropped in favor
of a cleaner split: **where `unixfdsink` exists (JP7), it replaces the header endpoint** — the core does
*not* also publish the 36-byte header stream. JP6 keeps the header endpoint. On both platforms the
**raw headless shm sink (`raw_endpoint`) is independent and config-gated**, untouched by this — that's the
endpoint a non-GStreamer / `mmap` consumer reads (it never carried the header anyway). So:

| host | plugin transport endpoint | raw headless shm |
|------|---------------------------|------------------|
| JP7  | `unixfdsink` (native caps, no header) | config-gated (`raw_endpoint.enabled`) |
| JP6  | `shmsink` + 36-byte header | config-gated (`raw_endpoint.enabled`) |

- **core-driver** genuinely differs by platform (jp6 = l4t/1.20, jp7 = 24.04/1.24); the transport is
  **runtime-selected in `pipeline.build()`** by probing for `unixfdsink` — no separate code path to ship.
- **ros2-bridge** is ONE image (`ros:lyrical` = 24.04/1.24) on both hosts, so it's *one image with a
  runtime transport switch* (header-shm component on a jp6 host, unixfd component on a jp7 host).
- **Separate memfd appsrc, not a tee branch:** `unixfdsink` needs FD-backed buffers; the tee can't
  negotiate the memfd allocation across its branches (validated — see below). So the feeder pushes
  memfd-copied frames to a dedicated `appsrc name=unixfd_src ! unixfdsink`, parallel to `camsrc`.
- **Stale-socket unlink:** `unixfdsink` binds a fresh AF_UNIX socket and will NOT rebind over a stale one;
  a hard restart on a persistent socket volume otherwise wedges it at "Failed to start". `build()` unlinks
  any leftover socket first. (shmsink manages its own file, so the JP6 endpoint doesn't need this.)

## What it unlocks / changes (the checklist)
**Core (done):**
- [x] **Self-describing format (caps):** the core's unixfd appsrc is tagged
      `video/x-bayer,format=<rggb|grbg|gbrg|bggr>` for 8-bit Bayer, `video/x-raw,GRAY8/16` for mono — same
      bytes; the **recorder keeps its GRAY8 Y-plane trick untouched** (the unixfd branch is a separate
      appsrc, not the recorder path). A consumer reads the format off the negotiated caps -> the bridge can
      **drop the `CAM_ROS_ENCODING` config plumbing** (it only exists because the 36-byte header can't
      carry "this is Bayer rggb"). *Validated: consumer negotiated `video/x-bayer,format=rggb` end-to-end.*
- [x] **Timestamp + frame_id via native buffer fields:** `buffer.offset` = frame_id, `buffer.offset_end`
      = absolute capture ns (these serialize across unixfd and aren't time-interpreted, unlike PTS). PTS
      stays relative. *(Chose native fields over `GstReferenceTimestampMeta` — simpler, and the abs-ns-in-
      PTS stall ruled out the obvious path anyway.)*
- [x] **Drops `--ipc=host`** for unixfd consumers (SCM_RIGHTS fd-passing isn't IPC-namespace-scoped).
      *Validated: producer + consumer in separate containers sharing only a named socket volume, no `--ipc`.*

**Bridge (next phase — ROS2 component refactor):**
- [ ] **Header-free bridge pipeline:** `unixfdsrc ! video/x-bayer ! [bayer2rgb] ! appsink` (vs today's
      `shmsrc ! application/x-cam-frame ! appsink` + C++ header parse).
- [ ] **Color option B becomes a real GStreamer element:** in-pipeline `bayer2rgb` (bilinear, full-res)
      replaces the interim C++ 2x2-cell `demosaic_rgb8()`. (Option A — publish `bayer_*` and let
      `image_proc` debayer — is unchanged and already full quality.)

## Validated on GStreamer 1.24 (2026-06-04, in Docker)
**End-to-end against the real `pipeline.py`** (cam-core:jp7 image, fake camera, a `unixfdsrc` consumer in
a separate container sharing only a named socket volume):
- ✅ Mono8: consumer negotiated `video/x-raw,format=GRAY8,512x512,25/1`; 10 frames -> clean EOS.
- ✅ BayerRG8: consumer negotiated `video/x-bayer,format=rggb,512x512,25/1`; the camsrc/recorder path stays
  `video/x-raw,GRAY8` (same bytes) while only the unixfd branch is tagged Bayer. 6 frames -> clean EOS.
- ✅ Per-frame `offset` = frame_id (monotonic +1), `offset_end` = absolute capture ns, `pts` relative,
  `flags: tag-memory` (FD-backed memfd crossed the socket). No "FD memories" error, no push warnings.
- ✅ Found + fixed a restart bug: a force-killed producer leaves a stale AF_UNIX socket; the next
  producer's `unixfdsink` then fails to start. `build()` now unlinks it (verified: 2nd producer starts).

Mechanism findings:
- ✅ `unixfdsink`/`unixfdsrc` exist on 1.24; **`video/x-bayer,format=rggb` caps cross intact** + data
  flows -> the bridge reads the Bayer format off the negotiated caps (drops the `CAM_ROS_ENCODING`
  config plumbing).
- ⚠️ **Don't put the absolute timestamp in `buffer.pts`** -- a ~56-year "future" epoch-ns PTS stalls
  downstream flow even at `sync=false`. Carry it in `buffer.offset_end` (+ `offset` = frame_id); those
  are native buffer fields that serialize with the buffer and aren't time-interpreted. PTS stays relative.
- ❗ **`unixfdsink` requires FD-backed (memfd) buffers** (`Expecting buffers with FD memories`). A plain
  `tee` tap of regular CPU buffers ERRORS -- the tee can't negotiate the memfd allocation across its
  branches, and `videoconvert` doesn't help. So the **producer must allocate memfd buffers itself**
  (`os.memfd_create` + `GstAllocators.FdAllocator`, confirmed available in the core image) and copy the
  frame in -> a **separate unixfd appsrc**, not a tee branch.

## The zero-copy reality (DECISION: A shipped, B deferred)
The memfd requirement means **plain unixfd is NOT zero-copy for CPU camera (Aravis) frames** -- it's a
producer-side memfd copy, ~the same cost as shm. Its win is *cleanliness* (native caps + metadata, no
header, in-pipeline `bayer2rgb`), not speed. **True** zero-copy needs frames already fd/dmabuf-backed,
which on the Jetson means **NVMM** (GPU) + **nvunixfd** (DeepStream 8). Two directions:
- **A (memfd-copy unixfd) — SHIPPED in the core (2026-06-04):** clean transport now; modest; the
  foundation for B. ~shm cost. This is what `pipeline.build()` does on JP7 today.
- **B (NVMM + nvunixfd) — deferred:** tap the recorder's existing `nvvidconv -> NVMM` for the transport
  branch + `nvunixfd` -> real GPU zero-copy. Bigger lift (DeepStream dependency), but it's what "ensure
  zero-copy" actually requires. Revisit when a consumer is bandwidth-bound (4K / multiple readers).

## Bigger follow-on (separate): nvunixfd / zero-copy GPU
Plain `unixfd` is CPU memfd (≈ same copy cost as shm — the win is *cleanliness*, not speed). The actual
bandwidth prize is **`nvunixfd`** (DeepStream 8): `NvBufSurface` DMABUF across containers, true zero-copy
GPU→GPU. Worth it only when a consumer is bandwidth-bound (4K, multiple readers). Scope separately.

## Interim (header era) — what exists today and why
Until this lands, color uses: a config-derived `CAM_ROS_ENCODING` (option A) + a simple C++ demosaic
(option B). Deliberately interim — both are replaced by the caps-driven, `bayer2rgb`-in-pipeline version
above. So **don't over-invest in the C++ demosaic's quality**; it retires with this migration.
