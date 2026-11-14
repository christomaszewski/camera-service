# Encoding optimization — results on the Orin

Measured 2026-11-11 (2026-11-12 UTC) on the bench AGX Orin, working through `README.md` in this directory.
The measurements in sections 1–8 were taken **before anything was implemented**: test builds and prototypes
lived outside the tree, and the scripts that produced the numbers are in `scripts/`. Rows 1–8 of the table
in section 1 were implemented afterwards; **section 9** says what each became in the tree and what the
implementation measured on the same Orin.

- **Host:** AGX Orin, L4T R39 rev 2.1, kernel 6.8.12-tegra, Ubuntu 24.04, 12 CPUs, NVMe. `nvpmodel` MAXN,
  `schedutil` governor.
- **Images:** built on the Orin from `v1.13.0` (`6d3af8b`) with `tools/build-images.sh local jp7`:
  `cam-core:jp7` and `webrtc-bridge:jp7`, both GStreamer 1.24.2; the bridge carries libnice 0.1.21 and
  gst-plugins-rs `0.15.4-1afff37`.
- **Deployment:** `cam-up --jp7` with a copy of `cam_usb.yaml` (ROS bridge off) against the NexiGo USB
  camera: core healthy, MJPEG 1080p30 stream-copy recording, bridge on `nvv4l2h264enc`.
- **CPU figures** are user + system time of the process per frame, with the source **paced at the camera
  rate**, as the service runs. Under `schedutil` a paced load runs at a reduced clock, so the same work
  costs fewer CPU-ms when saturated; both are given where it matters. System-wide accounting
  (`/proc/stat` against an idle baseline) put the work done outside the process — kernel threads and IRQs
  of the encoder and VIC drivers — at 1–5 ms per 5 MP frame, so the process figure is a fair measure.
- **Content:** synthetic, as in the handoff scripts. No Bayer or 16-bit camera was on the bench. The 5 MP
  mosaic (smooth scene, σ = 2.5) compresses to 51.8% of raw; the real Blackfly footage benchmarked on this
  Orin earlier came to 51.4%, so it is representative in size.

## 1. What is worth implementing

| # | Change | Measured effect | Verdict |
|---|---|---|---|
| 1 | **Feed NV12 to the hardware lossless encoder** (frame + one shared constant chroma block, built where the recorder wraps frames; fragment starts at `nvvidconv`) for 8-bit mono/Bayer | Recorder 52.5 → 25.5 ms per 5 MP frame (−51%); whole live core 61.4 → 33.5 ms (−45%). Bit-exact on scene, noise and an all-codes ramp | **Do first.** Replaces the handoff's NV24 idea (variant B), which saves only 12 ms |
| 2 | **16-bit on the hardware encoder by byte split** (R2), folded variant, fed as NV12 | 47.3 → 7.4 ms per 640×512 frame; in the live core FFV1 holds 297% of a core at 60 fps. Bit-exact 1200/1200. Size 34% of raw against FFV1's 31% (56% against 49% on noisier frames) | **Do**, after 1 (it reuses the NV12 feed). Needs real Boson footage for the size figure |
| 3 | **Raise the WebRTC bitrate floor** (`min-bitrate` default 1 kbit/s) | After 10 s of 50% loss the encoder target is at the floor; back above 1 Mbit/s in 34 s with the default, in 5 s with a 500 kbit/s floor | **Do.** A default in `run.sh`; no code |
| 4 | **Reap stale sessions** (W2), liveness + cap | On the jp7 bridge a vanished viewer held its session and encoder for about 15 min 40 s. The reaper prototype ended it 12 s after the blackout; bridge CPU 77% → 43% | **Do** for the 24.04-based bridge images; both signaller signals are needed (section 4.3). A 26.04-based bridge drops the dead session by itself after 30 s (row 4b) |
| 4b | Build the jp7 bridge on Ubuntu 26.04 (`BASE_IMAGE=ubuntu:26.04`, what `jp6m` already does) | GStreamer 1.28.2 + libnice 0.1.23: the black-holed viewer was removed after 29.5 s with no reaper; `nvv4l2h264enc` loads through CDI on this R39 host and encoded both sessions | **Worth a validation round**: it fixes the cause rather than the symptom, but changes the bridge's whole userspace. The cap policy of row 4 is still wanted |
| 5 | **Stop the bridge working with no viewer** (W6) | A 5 MP Bayer stream costs the bridge 80% of a core with zero viewers (debayer + convert); a viewer adds 32% | **Do.** Larger than any per-viewer saving on Bayer cameras |
| 6 | **Encode once for all viewers** (W3, opt-in) | Works. 3 viewers: 64–66% of a core against ~145% per-viewer; High profile becomes available. Needs complete caps and a `force-IDR` hook (section 4.5) | **Do as opt-in**, after 3–5. Fixed bitrate, and the encoder runs with no viewer (20–24%) unless gated by 5 |
| 7 | `videoconvert_threads` default 4 → 1 | 52.5 → 42.7 ms per 5 MP frame; throughput unchanged (encoder-bound at ~50 fps) | Free interim win; moot for mono/Bayer once 1 lands |
| 8 | Skip the unused main-pipeline push | ~1 ms per 5 MP frame (2% of a core at 24 fps) | Small. Take it when touching that code |
| 9 | Uncompressed mode (R1) | 1.3 ms per frame, 100% of raw. `splitmuxsink` splits raw Matroska cleanly on 1.24.2 and 1.28.8 and **aborts the process on 1.20.3** (the `jp6` userspace) | Only as a non-NVENC fallback; R2 gets within 6 ms of it at a third to a half of the disk |
| 10 | Dependency bump to 1.28.9 / `gstreamer-1.28.9` | Checksums in the handoff verified against the downloads; both tags exist | Any time (dev images only) |

Not worth it: `nvvidconv` doing the colour step (unsupported, or not exact); `bayer_tile` as a load knob (it
adds 7–9 ms); `keyframe_interval_s` as a load knob; the FFV1 speed knobs for 16-bit; the chroma-plane
layout for 16-bit; H.265 for the stream as a CPU measure.

Two things surfaced that the handoff did not ask about:

- **Publishing to the unixfd endpoint costs about 6 ms per 5 MP frame with no consumer connected** (a
  memfd and a 5 MB copy per frame on the capture thread, plus the sink thread).
- **The Ubuntu 26.04 userspace has ICE consent freshness on** (GStreamer 1.28 + libnice 0.1.23), and a
  bridge built on it dropped a black-holed viewer after 29.5 s by itself. The stale-session problem belongs
  to the 24.04-based bridge images only (section 4.2).

## 2. Recording, 8-bit mono / Bayer on the hardware path

2448×2048 at 24 fps, `scripts/recorder_variants_bench.py --set bayer`, 240 frames per variant, each
recording decoded with `replay.raw_decode_chain` and compared frame by frame.

### 2.1 Variants (handoff section 1.7)

| Variant | CPU ms/frame | Size vs raw | Bit-exact | Notes |
|---|---:|---:|---|---|
| A — as shipped (`videoconvert n-threads=4`) | 52.5 | 51.8% | 240/240 | |
| A with `n-threads=1` | 42.7 | 51.8% | 240/240 | the three pool threads cost more than they save now that the colour step is a copy |
| B — NV24 from the feeder | 40.9 | 51.8% | 240/240 | `nvvidconv` still uploads 15 MB per frame |
| C — GRAY8 straight into `nvvidconv` → NV24 | — | — | — | `gst_nvvconv_transform: Transform not supported` |
| C via NVMM GRAY8 → NV24 | — | — | — | same refusal |
| C2 — GRAY8 → VIC I420 → VIC NV24 | 25.4 | 51.3% | **0/240** | luma clamped at 235 (1.9% of pixels, error up to 20) |
| C2 with the range pinned on the NVMM caps | 25.9 | 54.3% | **0/240** | range expanded instead (95% of pixels) |
| **D — NV12 from the feeder** (frame + shared constant chroma; the VIC upsamples to NV24) | **25.5** | 51.8% | **240/240** | upload is 7.5 MB; no `videoconvert` |
| D with I420 instead of NV12 | 26.5 | 54.3% | **0/240** | the VIC treats I420 as limited range |
| A + `bayer_tile=plain` | 59.5 | 45.5% | 240/240 | tiling itself is 9 ms on the capture thread |
| D + `bayer_tile=plain` | 32.4 | 45.5% | 240/240 | |
| A with a 10 s GOP | 54.7 | 50.5% | 240/240 | |
| FFV1, for scale | 301 | 59.5% | 240/240 | 12.5 fps: not real time at 5 MP |
| Uncompressed Matroska, for scale | 7.8 | 100% | 240/240 | |

Further runs of the candidates:

| Content | A | A, 1 thread | B | D (NV12) | Size |
|---|---:|---:|---:|---:|---:|
| Scene, paced (above) | 52.5 | 42.7 | 40.9 | 25.5 | 51.8% |
| Uniform noise, paced — worst case | 74.4 | 62.2 | 62.8 | 45.9 | 131.7% |
| All 256 codes in every frame (ramp + noise) | 57.7 | — | — | 29.3 | 65.9% |
| Scene, saturated (`--unpaced`) | 37.3 | 32.8 | — | 24.6 | 51.8% |

All of these are bit-exact, D with and without the colorimetry pin on its input caps. Saturated, every
variant drains at about 50 fps: the encoder is the limit, not the CPU side.

### 2.2 Where the CPU goes (step 1)

Queues between the elements give each its own thread (`A-diag`, `D-NV12-diag`):

| Stage | A as shipped | D (NV12) |
|---|---:|---:|
| Feeder (wrap + push) | 1.2 | 1.0 |
| `videoconvert` (streaming thread + 3 pool threads) | 11.9 | — |
| `nvvidconv` — the copy into encoder memory | 24.4 | 7.8 |
| Encoder, output thread | 5.7 | 5.7 |
| Encoder, `V4L2_EncThread` ×3 (mostly system time) | 5.8 | 6.0 |
| `h265parse` | 2.4 | 2.1 |
| Muxer + file write | 2.0 | 2.0 |
| **Total, ms per frame** | **54.3** | **25.4** |

So the "other 59 ms" of the handoff is mostly `nvvidconv` copying a 15 MB NV24 frame into NVMM, at about
1.6 ms per MB. That is why halving the upload (NV12) beats removing `videoconvert` (NV24 from the feeder).
The two encoder threads scale with the bitstream: on noise (6.6 MB per frame) they take 19 and 14 ms.

### 2.3 The live core (steps 1 and 6)

The real core recording a live 5 MP 24 fps stream through the `shm` input, unixfd plugin endpoint enabled
with no consumer, measured with `scripts/thread_cpu.py`. The test build was a copy of `core-driver` with two
environment-gated changes: `_discard_replay_main` extended to live sources, and the NV12 feed. Every run
was decoded afterwards and checked against the writer's frames (788/788 exact, in order, no drops).

| Build | Whole core, ms/frame | Capture thread |
|---|---:|---:|
| As shipped | 61.4 | 8.0 |
| Main-pipeline push skipped | 63.6 | 9.5 |
| NV12 feed | 33.5 | 8.2 |
| NV12 feed + push skipped | 32.3 | 7.2 |
| NV12 feed + push skipped + plugin endpoint off | 26.0 | 3.0 |
| As shipped, plugin endpoint off | 54.4 | 3.8 |

- The unused main-pipeline push is one 5 MB copy: about 1 ms per frame (3.8 → 3.0, 8.2 → 7.2; the second
  row is run-to-run variation in the publish path, not a regression).
- The unixfd publish costs about 5 ms on the capture thread plus 1.4 ms on the sink thread, with nobody
  connected.

### 2.4 Tiling and temporal prediction (steps 4 and 5)

- **Tiling is a disk knob, not a load knob.** It removes 12% of the bitstream on the synthetic mosaic and
  about 1 ms from the encoder's output thread, and adds 9 ms of numpy on the capture thread. On the real
  Blackfly footage benchmarked here earlier (`tiling_benchmark.py`) the gain was 2%.
- **Temporal prediction is active.** `probe_temporal.py` on a hardware recording of the static synthetic
  scene: 28 keyframes of 4555 KiB, 760 delta frames of 2464 KiB (54%). The default GOP is about 30 frames,
  so a 10 s GOP removes only 2.5%. On the real footage delta and key frames were the same size
  (98–100%). It is content-dependent and does nothing for CPU.

### 2.5 Notes for the implementation of change 1

- Build the buffer as two memories, the frame and one shared read-only chroma block of `width × height / 2`
  bytes of 128; keep the block's Python `bytes` alive for the life of the memory. `nvvidconv` merges the
  two (1.4 ms); a contiguous buffer built in Python costs the same on the feeder instead.
- A `GstVideoMeta` on the buffer changes nothing: `nvvidconv` maps the whole buffer.
- Only NV12 is exact. I420 through the same path is not, and neither VIC-only route is, so the choice of
  format is load-bearing: qualify it per JetPack with noise **and** a ramp of all codes at the sensor
  geometry (`nvenc_lossless_test.py`, review finding TQ-18). JP6 (r36) is unverified.
- Geometry: NV12's default layout needs `width % 4 == 0` and an even height (review finding DP-1); keep
  the present path, or refuse, otherwise.
- The sidecar's `recording_colorimetry` and the read path do not change: the bitstream is the same NV24.

## 3. Recording, 16-bit

640×512 at 60 fps, `recorder_variants_bench.py --set gray16`, 1200 frames (600 at σ = 40).

| Path | CPU ms/frame, σ = 6 | Size, σ = 6 | CPU, σ = 40 | Size, σ = 40 | Saturated CPU |
|---|---:|---:|---:|---:|---:|
| FFV1 (today) | 47.3 | 31.3% | 48.6 | 49.3% | 20.8 (192 fps) |
| Uncompressed (R1) | 1.3 | 100% | 1.4 | 100% | 0.6 |
| Split, plain, shipped 8-bit fragment | 10.8 | 36.8% | | | 6.6 |
| Split, folded, shipped 8-bit fragment | 12.6 | 34.3% | | | |
| Split, plain, fed as NV12 | **5.5** | 36.8% | 6.8 | 62.9% | 5.4 |
| Split, folded, fed as NV12 | **7.4** | 34.3% | 9.5 | 56.0% | 5.0 |
| Planes: Y = high byte, U = low byte, NV24 | 10.1 | 39.3% | 11.4 | 66.4% | |
| Planes, folded | 13.5 | 35.9% | | | |

Every row is bit-exact on every frame (the planes variant included: chroma survives the NV24 upload and
the lossless encoder exactly, and the unused V plane comes back constant).

- **R2 works on the hardware.** The handoff's own scripts agree: `gray16_split_roundtrip.py --encoder
  hw-hevc-lossless` gives 120/120 for both variants at 6.9 / 7.4 ms against FFV1's 21.1 (saturated).
- **The saving is larger live than the scripts show.** Paced at 60 fps FFV1 costs 47 ms per frame; the real
  core on a live 640×512 GRAY16 60 fps stream measured 49.6 ms per frame, **297% of a core**. The split is
  about 0.4 of a core.
- **Use the folded variant.** It costs 2 ms more in numpy (the script's `np.where`; an XOR with
  `-(high & 1)` would be cheaper) and matters most where it counts: on noisier frames it is 56.0% against
  62.9% for the plain split.
- **Disk:** 3 points more than FFV1 on smooth frames, 7 on noisy ones. Real Boson footage is the missing
  input; `docs/recording-tuning.md` puts FFV1 on real 60 fps thermal at 64–76% of raw.
- The FFV1 speed knobs do nothing here (20.9 → 20.1 ms), as predicted.
- **R1:** `splitmuxsink` writes and splits raw `GRAY16_LE` / `GRAY8` Matroska correctly on 1.24.2 and
  1.28.8 (every frame read back exact, width 642 included). On 1.20.3 it aborts the process:
  `gstsplitmuxsink.c:2691:check_completed_gop: assertion failed: (gop != NULL)`.

## 4. WebRTC

### 4.1 Cost per viewer

| Source | Bridge, no viewer | One viewer | Two viewers |
|---|---:|---:|---:|
| USB 1080p30, already I420, NVENC H.264 | 2% of a core | 45–50% | 85–100% |
| 5 MP Bayer at 24 fps, NVENC H.264 | **80%** | 112% | |
| the same with `CAM_WEBRTC_MAX_SIZE=1280` | **83%** | 110% | |

On Bayer the idle cost is `bayer2rgb` plus the convert at sensor resolution, 33 ms per frame, and the
downscale sits after it. A half-resolution debayer (one RGB pixel per CFA cell) ahead of the scale would
remove most of it for any target at or below half size; not tried here (built later: section 10.1).

### 4.2 Stale sessions (handoff section 2.4) — confirmed

- **jp7 bridge image (24.04, GStreamer 1.24.2, libnice 0.1.21):** `consent-freshness = False`,
  `keepalive-conncheck = False`.
- **Ubuntu 26.04 (GStreamer 1.28.2 from the distribution, and the source-built 1.28.8 of the `dev` image,
  libnice 0.1.23):** both `True`. This is the userspace of the `jp6m` and `dev` bridges.
- **A 26.04-based bridge on this host** (`tools/build-images.sh local jp6m`, `IMAGES=webrtc-bridge`:
  GStreamer 1.28.2, libnice 0.1.23, the same gst-plugins-rs 0.15.4): `nvv4l2h264enc`, `nvv4l2h265enc` and
  `nvvidconv` are present through CDI and the sink picked `nvv4l2h264enc` for both of two viewers. One
  viewer black-holed: `consumer-removed` **29.5 s** later with no reaper, the other viewer untouched.
- **Deployed jp7 bridge:** a viewer cut off by dropping all traffic to it stayed counted for **about 15 min 40 s**
  (cut at 02:21:58, removed at 02:37:36 UTC), the kernel's TCP retransmission limit on the signalling connection. Its encoder
  ran the whole time (bridge at 33% of a core for nobody), and a second viewer brought the bridge to 85–90%.
- A viewer killed with the link intact is removed at once (its connection resets).

### 4.3 Reaper prototype (W2) — works

`scripts/webrtc_lab.py`, on the Orin, media over UDP:

- **Liveness field.** With `LAB_DIFF=1`: after a blackout only sender-side counters keep changing
  (`bytes-sent`, `packets-sent`, the SR fields). Frozen from the first sample: the candidate pair's
  `gst-twcc-stats` (changes at every 2 s sample while a receiver is alive), and the receiver-report block
  of the remote source (`rb-lsr`, `rb-dlsr`, `rb-exthighestseq`, `round-trip-time`; about every 5 s). Key
  on those two together.
- **Ending a session needs both signals, in this order:** `end-session` on the signaller (tells the server
  and through it the viewer) and then `session-ended` (the sink's handler for it removes the session and
  releases the encoder). `end-session` alone leaves `get-sessions` unchanged — measured.
- **Liveness policy, 10 s:** the black-holed viewer was ended 12 s after the blackout (threshold plus one
  2 s tick), `consumer-removed` fired, CPU 77% → 43%, the other viewer kept 30 fps.
- **Cap policy, 1 session:** a second viewer joining ended the first within one tick.
- A viewer whose session was reaped does not come back by itself when its link returns (`webrtcsrc` sat at
  0 fps). The dashboard's own reconnect has to do that.

### 4.4 Recovery after a dropout (W1, handoff section 2.3)

No radio link was available; loss was injected between a producer and a viewer in two containers (random
drop in the `FORWARD` chain), media over UDP, 1080p30, defaults unless stated.

| Event | Default floor (1 kbit/s) | `min-bitrate=500000` |
|---|---|---|
| 50% loss for 10 s: encoder target during the loss | 4.7 Mbit/s → 441 → 25 → 2 → **1 kbit/s** within 8 s | → 500 kbit/s |
| Time after the loss ends to reach 1 Mbit/s | **34 s** | **5 s** |
| … 2 Mbit/s | 40 s | 8 s |
| … the previous rate | 46 s | 22 s |
| Total blackout, 10 s | picture back 0.4 s after the link; rate dips to 3.6 Mbit/s and recovers in 4 s | |
| Total blackout, 45 s | session survives; picture back within 1 s; rate halved, then climbs ~60 kbit/s per 2 s | |

- **Hypothesis 1 is confirmed and is the dominant one**: it is the lossy fade, not the outage itself, that
  makes recovery slow. With the default floor the stream delivers 30 frames a second of a few kbit/s for
  half a minute.
- Hypothesis 2 (a new session's 2 Mbit/s start overshooting) only applies to a link below 2 Mbit/s; not
  tested. Hypothesis 3 is the dashboard's.
- A session that survives a blackout resumes faster than a reconnect can, so a viewer-side watchdog that
  tears down at 12 s works against recovery after a clean outage, and each reconnect leaves a stale
  session behind on the 24.04 bridges.

### 4.5 Encode once (W3) — works, with two requirements

| Viewers | Shared NVENC H.264, 4 Mbit/s | Per-viewer (today) |
|---|---:|---:|
| 0 | 20–24% of a core (the encoder never stops) | 2% |
| 1 | 34% | 45–50% |
| 2 | 50% | 85–100% |
| 3 | 64–66% | ~145% (extrapolated) |

- **Complete caps up front.** `h264parse` announces caps when the encoder's arrive and again after parsing
  the SPS; `webrtcsink` refuses the second (`Renegotiation is not supported`) and the pipeline dies
  not-negotiated. Working chain: `nvv4l2h264enc ! video/x-h264,profile=high,level=(string)4,
  stream-format=byte-stream,alignment=au ! h264parse config-interval=-1 ! capssetter join=true
  caps="video/x-h264,coded-picture-structure=(string)frame,chroma-format=(string)4:2:0,
  bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8" ! webrtcsink`. The level has to be the derived one the
  bridge already computes.
- **Keyframes on request.** `nvv4l2h264enc` ignores the upstream force-key-unit events the sink sends when
  a session connects and on every PLI, so a joining viewer waits for the next periodic IDR (first frame
  after 1.3–2.9 s at a 2 s GOP, with dozens of repeated requests per join). Emitting the encoder's `force-IDR` action from a
  probe on those events, at most once a second, brings joins to 0.7–1.0 s and the requests down to about two per join.
- The offer advertises `profile-level-id=640028`: High profile, level 4. The constrained-baseline pin does
  not apply, as the handoff expected.
- 30% loss to one of three viewers for 10 s: that viewer held ~30 fps on retransmissions, the others were
  untouched, no extra keyframes. There is no bitrate adaptation in this mode.

### 4.6 H.265 (W5)

`video-caps=video/x-h265` with `nvv4l2h265enc` ranked up, and the shared-encoder chain with `h265parse`,
both streamed 1080p30 to the GStreamer viewer (first frame in about 1 s; per-viewer one viewer 30% of a
core). No browser was tried.

### 4.7 A trap in the test set-up

With the producer host-networked and the viewer in a container on the same host, every session ran over
**ICE-TCP**: libnice's UDP checks from the host-networked agent to the container went unanswered while
plain UDP between the same addresses worked. TCP hides packet loss, so the first loss runs showed nothing.
All loss figures above are from producer and viewer both on the Docker bridge network, where the selected
pair is UDP host-to-host. Worth one check on a vehicle: the `protocol` of the selected candidate pair in
the sink's stats.

## 5. Dependencies (handoff section 3.6)

- **Step 1, inputs:** the seven 1.28.9 checksums in the handoff match the archives downloaded from
  `gstreamer.freedesktop.org`; Debian's mirror serves the identical `gstreamer` archive. The
  `gstreamer-1.28.9` tag exists in gst-plugins-rs (`5d373ca6`, 2026-11-06). The bump itself was not made.
- **Step 3, what the images run:**

  | Image | GStreamer | libnice | Consent freshness |
  |---|---|---|---|
  | `cam-core:jp7` (24.04) | 1.24.2 | 0.1.21 (unused) | — |
  | `webrtc-bridge:jp7` (24.04); the `jp6` bridge has the same base | 1.24.2, gst-plugins-rs 0.15.4-1afff37 | 0.1.21 | **off** |
  | `cam-dev` (26.04, source-built) | 1.28.8 | 0.1.23 | on |
  | `webrtc-bridge:jp6m` (26.04), run here with the CDI device | 1.28.2, gst-plugins-rs 0.15.4-1afff37 | 0.1.23 | **on**: dead session gone in 29.5 s |

  The `jp6` core (22.04, GStreamer 1.20) was not built here.

## 6. Corrections to the handoff

- **Variant B's `0/120` in `bayer_hw_path_cpu.py` was the script's fault.** It wrapped a temporary `bytes`
  as the shared chroma block; `Gst.Memory.new_wrapped` does not copy, so the block dangled and `nvvidconv`
  read freed memory (NvMap write errors, 123.7% of raw). Fixed in the script: B is 48/48.
- **The NV24 buffer of variant B is not the cheap one.** The copy into NVMM dominates, and NV12 halves it.
- **`session-ended` removes a session; `end-session` does not** (section 4.3). The handoff had this right;
  it is recorded here because the first prototype run suggested otherwise.
- **16-bit FFV1 costs 47–50 ms per frame live**, not the 16–21 ms a saturated run shows.

## 7. Not done, and still open

- Real footage: no Bayer or 16-bit camera on the bench, and no thermal recording to feed `--raw-file`.
- JetPack 6: nothing here was run on r36. The NV12 feed and the split both rest on VIC behaviour that
  differs by format on R39, so they need the lossless proof there before being enabled.
- A real radio link for W1; a browser for W3 (High profile) and W5.
- Power draw was not recorded (`tegrastats`); CPU time only.
- The 26.04-based bridge was only run as the lab producer: not through `cam-up`, not `webrtc_test.sh`, and
  not on a JP6 host, where the encoder plugins arrive by CSV injection instead of CDI.

## 8. Reproducing

```
# images, on the Orin
PUSH=0 IMAGES="cam-core webrtc-bridge" tools/build-images.sh local jp7
R='docker run --rm --device nvidia.com/gpu=all -v '"$PWD"':/repo -v /tmp/work:/work -w /repo --entrypoint python3 local/cam-core:jp7'

# sections 2 and 3
$R docs/encoding-optimization/scripts/recorder_variants_bench.py --set bayer
$R docs/encoding-optimization/scripts/recorder_variants_bench.py --set bayer --content ramp --variants A,D-NV12,D-NV12-nopin
$R docs/encoding-optimization/scripts/recorder_variants_bench.py --set bayer --content noise --variants A,A-1thr,B,D-NV12
$R docs/encoding-optimization/scripts/recorder_variants_bench.py --set gray16 --frames 1200 --distinct 240
$R docs/encoding-optimization/scripts/recorder_variants_bench.py --set gray16 --sigma 40 --frames 600

# section 2.3: a writer (appsrc ! shmsink) feeds a `camera.type: shm` instance; then, on the host
python3 docs/encoding-optimization/scripts/thread_cpu.py <core pid> 20 24

# section 4: producer and viewers as containers of the bridge image on the Docker bridge network
gst-webrtc-signalling-server --host 0.0.0.0 --port 8450 & python3 webrtc_lab.py        # in the producer
python3 webrtc_viewer.py ws://<producer address>:8450 600 A                            # in each viewer
iptables -I FORWARD -s <producer> -d <viewer> -m statistic --mode random --probability 0.5 -j DROP   # loss
iptables -I FORWARD -s <viewer> -d <producer> -j DROP    # with the reverse rule: a dead link
```

## 9. What was implemented

Rows 1–8 of section 1, measured again on the same Orin on 2026-11-12 with the service itself (images rebuilt
from the tree, `cam-up --jp7` for the camera runs). Not done: row 4b (a 26.04-based bridge), row 9
(uncompressed mode) and row 10 (the dependency bump).

| # | In the tree | Measured with the implementation |
|---|---|---|
| 1 | `recording.nvenc_feed: auto \| nv12 \| videoconvert` (default `auto`). The recorder appends one shared constant chroma block to each frame and the fragment starts at `nvvidconv`. `auto` uses it only after a start-up proof on the host (`recorder.probe_nv12_feed`: an all-codes ramp, noise and a reversed ramp through the real fragment, decoded and compared; about 1 s, once per geometry) and otherwise keeps the `videoconvert` feed. Widths not divisible by 4 or odd heights keep it too | Live core, 5 MP at 24 fps: **61.4 → 32.7 ms per frame**; 768/768 frames bit-exact. `tools/nvenc_lossless_test.py --feed both` passes at 2448×2048 and 1280×512 |
| 2 | `recording.gray16_split: fold` (default `off`): the 16-bit frame is stored as GRAY8 of twice the width, low byte mirrored where the high byte is odd. Sidecar `gray16_split_mode`; replay and `export_recordings` join it again. Taken only in front of a lossless 8-bit encoder, and the hardware one only behind the proven feed of row 1 | Live core, 640×512 at 60 fps: FFV1 **294.6% of a core → 47.2%**, 34.3% of raw, 1952/1952 frames bit-exact. Noisy frames: 52.1% of a core, 56.0% of raw |
| 3 | `run.sh` sets `min-bitrate=500000` unless `CAM_WEBRTC_MIN_BITRATE` says otherwise, lowered to the start or max bitrate when one of those is below it | The deployed bridge logs `min-bitrate 500000`; the recovery figures are those of section 4.4 |
| 4 | `tools/session_reaper.py`, driven from `bridge_stream.py` every 2 s: `CAM_WEBRTC_STALE_S` (default 20) ends a session whose receiver feedback has stopped, `CAM_WEBRTC_MAX_SESSIONS` (default off) caps the count | Deployed stack: a black-holed viewer was ended **22 s** after the blackout; with a cap of 3 a fourth viewer ended the oldest |
| 5 | `tools/idle_gate.py`: with no session only `CAM_WEBRTC_IDLE_FPS` (default 1) frames a second pass the bridge's ingress queue; `CAM_WEBRTC_IDLE_GRACE_S` (default 5) | 5 MP Bayer at 24 fps, no viewer: **82.8% → 6.3% of a core**. USB 1080p30: 1.3%. First frame for a viewer 1.3–1.4 s after connecting (1.0 s ungated) |
| 6 | `CAM_WEBRTC_ENCODE=shared` (opt-in) with `CAM_WEBRTC_BITRATE`: `tools/shared_encoder.py` builds one encoder ahead of `webrtcsink` (hardware H.264 / H.265, or `x264enc`); `bridge_stream.py` completes its caps from the stream, forces keyframes on request (at most one a second) and keeps `CAM_WEBRTC_PROFILE` | USB 1080p30, NVENC, 4 Mbit/s: 2.2% idle, **32 / 44 / 58%** of a core for one / two / three viewers (per viewer: 45–50 / 85–100 / about 145). High profile 35 / 45%, H.265 32 / 44%, software `x264enc` 177 / 192% for one / two. VP8 falls back to per-viewer with a log line |
| 7 | The pinned mono / Bayer luma copy in front of the hardware encoder always runs `videoconvert n-threads=1`; `recording.videoconvert_threads` keeps its meaning for real conversions | Live core on the `videoconvert` feed, 5 MP at 24 fps: 61.4 → 48.7 ms per frame |
| 8 | The main pipeline's `camsrc` is not fed when nothing reads it (no preview, no raw endpoint) | Part of the row 1 figure |

What the implementation found that the prototypes had not:

- **The shared encoder saves little on a 5 MP Bayer stream.** At 8 Mbit/s it measured 139 / 170 / 175% of a
  core for one / two / three viewers against 117 / 158% per viewer for one / two. The debayer dominates
  either way. The two runs are also not at one bitrate — per-viewer encoders start at 2 Mbit/s and adapt,
  the shared one holds its 8 — and that was not separated out. With `CAM_WEBRTC_MAX_SIZE=1280` at
  4 Mbit/s: 113 / 133%. The mode's case is several viewers of a stream that is cheap to prepare.
- **The shared encoder needs its own thread.** In line with the debayer and convert a 5 MP stream reached
  viewers at 20 fps of 24; behind a queue, 24.
- **`webrtcsink`'s discovery of an encoded stream needs a keyframe.** It joins the stream mid-GOP and drops
  every slice until one arrives: with `x264enc`'s own 250-frame GOP the bridge registered with the
  signalling server 10 s after the stream started. Keyframes are now forced on the clock until it has
  registered: 1.8 s.
- **`x264enc` takes no properties once it has started** ("setting property in wrong state"), so the software
  shared encoder is configured before the pipeline starts and its keyframe interval is kept on the clock.
- **Connecting a handler to the signaller's `start` signal replaces the default one**: the bridge then never
  registers. The bridge reads the signaller's `client-id` instead to learn that discovery is over, which is
  when the idle gate may first close (a fixed warm-up would cut a ten-second codec discovery short).
- **`Gst.Memory.new_wrapped` aborts the process on the 26.04 PyGObject** when handed Python `bytes`; the
  shared chroma block is a `Gst.Buffer` whose regions are appended to each frame.
- **A stalled camera looks like a dead viewer.** With nothing sent a live receiver has nothing to
  acknowledge; its feedback stopped 8 s after the last frame, and the first version of the reaper ended a
  live viewer when the camera paused for 20 s. Silence now only counts while frames are going out
  (covered in `webrtc_test.sh`).
- **`webrtc_test.sh` raced codec discovery**: it slept 12 s and then connected to "the first producer",
  which on the jp7 images had not registered yet. It now waits for the registration.

A code review before the branch was pushed, with every finding checked on the Orin, changed these:

- **The hardware shared encoder failed at SD preview sizes and at high bitrates**, which the runs above
  (1080p, 5 MP, 1280 wide, 2–8 Mbit/s) had not touched. Two habits of `nvv4l2h264enc`: it announces level
  3 as `3.0`, which a filter pinned to `3` refuses (640×480, a 5 MP camera scaled to 640); and it writes
  into the stream the level its bitrate needs, never less than 2.1, while announcing the one it was asked
  for — the parser then announces the real one and `webrtcsink` refuses that (320 wide at any bitrate;
  480×400 at 6 Mbit/s writes level 3; 640×534 at 20 Mbit/s writes 3.2). The bridge now pins the level the
  encoder will write, in both spellings. Swept afterwards, H.264 from 160 to 2448 wide at 0.15–30 Mbit/s
  and H.265 at five points: every one negotiates and decodes, and the SDP level matches the stream.
- **`x264enc` at `ultrafast` cannot produce main or high.** It labels its caps with the profile asked for
  and writes constrained-baseline (`profile-level-id` 42c0..); that it negotiated at all rested on the
  parser keeping the label. The bridge pins what the encoder will produce and says so; `superfast` and
  slower give main (4d40..) and high (6400..) as asked. `baseline` means constrained-baseline.
- **Which encoder is shared follows the element ranks**, as per-viewer encoding does (on this image
  `nvv4l2h264enc` ranks 257 to `x264enc`'s 256, so the hardware encoder is the default in both modes):
  `GST_PLUGIN_FEATURE_RANK` can keep NVENC for the recorder.
- **The reaper fails open.** It reads `webrtcsink`'s statistics by field name; on a build where they read
  differently every viewer would have been ended every 20 s. It now ends nobody for silence until it has
  seen receiver feedback from some session, and logs once when it has none to go on. (With congestion
  control `disabled` or `homegrown` a live viewer keeps its session, also at a 5 s threshold.)
- **The 16-bit split is always proven.** `nvenc_feed: nv12` used to waive the start-up proof for the split
  as it does for 8-bit; the split runs at twice the camera's width, where nobody qualified the feed, and
  one wrong code there is 256 counts.
- **An inconclusive NV12 probe is not repeated for five minutes.** It blocks the main loop for up to 10 s,
  and ran again on every recording-settings change.
- The single-thread luma copy applies to the hardware encoder only, where it was measured; `x265-lossless`
  keeps `videoconvert_threads`.

Three of these change what a deployed bridge does **by default** and want a release note: the 500 kbit/s
floor, sessions ended after 20 s without receiver feedback, and the idle gate (with no viewer the chain
ahead of `webrtcsink` runs at 1 fps). The shared encoder and the 16-bit split are opt-in; the NV12 feed is
on by default but only after its proof passes on the host.

**Overlap with PR #65 (`load-knobs`).** That PR carries its own idle throttle under the same names
(`CAM_WEBRTC_IDLE_FPS`, `CAM_WEBRTC_IDLE_GRACE_S`); row 5 replaces that part of it, with one difference in
meaning: the grace here runs from the last viewer leaving (default 5 s), there from boot (default 10 s) —
start-up is covered here by waiting for the bridge to register. The rest of the PR (`CAM_WEBRTC_MAX_FPS`,
the FFV1 knobs, the per-thread CPU breakdown) is not in this work and will conflict with it in
`bridge_stream.py` and `run.sh` when rebased.

Tests: the hardware-free suite (755 tests, no skips) passes on GStreamer 1.28.8 (dev image), 1.24.2 (jp7
image) and 1.20.3 (a `cam-core:jp6` image built on the Orin: the JetPack 6 userspace, not JetPack 6
hardware). It executes all but one of the 841 statements this work adds on 1.28.8 — the bridge launcher's
session, gate and shared-encoder code included, on real GStreamer with a stand-in for `webrtcsink`, and
`run.sh`'s decisions through `CAM_DRY_RUN=1`. `plugins/webrtc-bridge/tools/webrtc_test.sh` passes on the
Orin against the jp7 core (unixfd) and that jp6 core (headered shm), with scenarios for the idle gate, the
shared encoder (software, a profile its preset cannot produce, and the hardware encoder at the geometry
and bitrate that used to break it) and a viewer that must survive a stalled camera and be ended once
frozen with `SIGSTOP`.

Still open, on top of section 7: none of this has run on JetPack 6 hardware (the NV12 proof decides there
at start-up, so the feed cannot silently be wrong, but its cost and the split are unmeasured); the split's
size on real thermal footage; the reaper and the floor over a real radio link; the shared stream in a
browser.

## 10. A second round: five more candidates

Evaluated on the same Orin on 2026-11-12, each against a measurement; two were adopted.

| Candidate | Measured | Verdict |
|---|---|---|
| **ROS 2 bridge idles with no subscriber** | 5 MP Bayer at 24 fps, nobody subscribed: 65% of a core with debayer on and 12% with it off, before; under 1% after. A subscriber still receives 150 of 150 frames; a 5 Hz `publish_rate` gives 5 Hz | **Adopted** (`idle_gate` / `CAM_ROS_IDLE_GATE`, default on). The `publish_rate` throttle moved ahead of the debayer with it |
| **Debayer at half resolution before the downscale** (WebRTC bridge) | Tried twice. In numpy, as a pump in place of `bayer2rgb`: 24.5 ms a frame in the live bridge against about 21 ms for what it replaces, 131% of a core against 108% — slower. As a compiled element (`bayerbin`, one C file): 5 MP Bayer at 24 fps, one hardware-encoded viewer, bridge CPU **109.5% → 80.4%** of a core at `CAM_WEBRTC_MAX_SIZE=1280` (the debayer/scale/convert thread 34.4 → 18.1 ms a frame) and **92.1% → 76.7%** at `640` (26.8 → 19.3 ms); the viewer receives 24 fps either way | **Adopted, compiled** (section 10.1). The numpy version was backed out |
| Skip the unixfd publish when no consumer is attached | 6 ms per 5 MP frame (section 2.3). `unixfdsink` exposes neither a client count nor a signal | **Not done.** The only way to know is to scan the process's own sockets; a deployment with no bridge can set `transport.plugin_endpoint.enabled: false` and pay nothing |
| Vectorised colour conversion in the ROS 2 bridge | 1080p30 I420 with one subscriber: 22.4 ms a frame with the per-pixel loop, 19.4 ms through `GstVideoConverter` | **Rejected.** 3 ms of 22: the cost is the 6 MB copy into the message and its publication, not the conversion, and the converter changes the colours (it honours the caps' colorimetry) |
| The split's extra frame copies | 0.044 ms + 0.051 ms a frame at 640x512, of 7.8 ms | **Not worth code** |

What this leaves as the larger remaining costs: publishing full-resolution RGB images over ROS (22 ms a
1080p frame, mostly copies), and `bayer2rgb` at sensor resolution in the WebRTC bridge for a viewer of a
preview that is not bounded to about half size. The first wants a different message path, not tuning.

### 10.1 The half-resolution debayer

`plugins/webrtc-bridge/gst/gstbayerbin.c`: a `GstBaseTransform`, `video/x-bayer` (8-bit, the four
patterns) to `video/x-raw,format=RGBx` at half the width and height. Each 2x2 cell gives one pixel — its
red, its blue, the mean of its two greens. Built with `gcc -O3` in the bridge image's builder stage.

- **Where it is used.** At the runtime format seam (unixfd, headered shm), when `CAM_WEBRTC_MAX_SIZE`
  bounds the preview to half the sensor size or less, or to within 10% above it
  (`format_adapt.bin_plan`). In that band the half-size frame is streamed as it is: 2448x2048 bounded
  to 1280 streams 1224x1024 rather than 1280x1070. Below it the scaler finishes (640 → 640x534). A
  bound well above half size, no bound, `CAM_WEBRTC_DEBAYER=full`, a static front-end or an image
  without the element all keep `bayer2rgb`.
- **In isolation** (`gst-launch`, 240 frames of 2448x2048): `bayer2rgb ! videoscale(1280) !
  videoconvert` costs 32 ms of CPU a frame over the source; `bayerbin ! videoconvert` costs 4 ms.
  The live saving is smaller (16 ms at 1280) because the thread also carries the conversion and
  the hand-off to the encoder.
- **What is given up.** The picture is not the one `bayer2rgb` and a scaler produce: nothing is
  interpolated, and the colour samples of a cell sit half a pixel apart. Not compared by eye on
  real footage or in a browser; `CAM_WEBRTC_DEBAYER=full` is the way back.
- **Checked:** the element against frames with a known answer — the four patterns, odd edges, tight
  and four-byte-padded rows, a short frame dropped without stopping the stream
  (`tools/check_bayerbin.py`, run by `webrtc_test.sh` in the bridge image); the plan and the seam in
  the unit suite; both debayers end to end in the loopback test. The descriptor and the H.264 level
  follow the size streamed.

One thing to watch when measuring on this host: with an image build running, the same bridge measured
47% and 73% of a core minutes apart. Per-frame CPU figures here were taken on an idle machine, or back to
back under the same load.

## 11. Further copy and metadata reductions (local validation)

These changes were checked in ARM64 Linux containers on a development Mac, **not on the Orin**.
They do not change pixel values, recording quality, or capture timestamps.

- **Live USB/RTSP/shm buffers:** the core accepts `GstFrame` payloads from these sources. A deep
  pixel copy in GStreamer releases the upstream capture/decoder/shm pool immediately; downstream
  recording and transport buffers share that owned allocation. This removes the Python image
  round trip, not every memory copy. Keeping a camera-owned buffer indefinitely would exhaust small
  live pools when the recorder or a consumer holds many frames. Tests retain 40 outputs from a
  one-slot source pool and check every pixel after reuse. Bayer tiling and the 16-bit split keep
  the byte path, with the policy following runtime recording settings, so transforms do not acquire
  an extra copy. The header transport also checks its queue limit before concatenating a byte frame.
- **Encoded recording without consumers:** live USB/RTSP omit their decode branch when the
  resolved recorder is `stream-copy` and local preview, raw endpoint, and plugin endpoint are all
  disabled. The encoded branch still owns stamps, accounting, and liveness, including standby and
  reconnect. Re-encoding or any raw output retains decoding. A fake MJPEG source is checked against
  its recorded bytes and sidecar frame IDs/timestamps with no raw sink present.
- **ROS 2 publication:** both bridge transports describe the input YUV layout in `FrameMeta`;
  publication converts directly into the image message's owned vector. The intermediate RGB vector
  and its subsequent copy are removed. The full-range conversion arithmetic is unchanged. The C++
  integration test covers six YUV layouts, RGB passthrough, short inputs, timestamps, and messages
  retained after later source-buffer reuse, through both header and unixfd components.
- **ROS 2 input conversion:** appsrc keeps two waiting frames and drops the oldest before decoding
  or conversion. Overload therefore cannot grow an unbounded conversion backlog. Drop accounting
  works on GStreamer 1.20 without the newer `appsrc.dropped` property and includes the discarded tail
  when input geometry changes or the converter stops. Deterministic overload tests check that the
  surviving frames retain their original pixels and stamps.
- **Replay/export metadata:** CSV stamps use compact integer columns, materializing a `FrameStamp`
  only when consumed. Export retains numeric PTS instead of dictionaries and strings for every row;
  it copies the original CSV byte-for-byte and validates hashes, stamps, PTS, and decoded counts.
  Integer overflow values, nullable chunks, unknown-source fallback, extra CSV fields, quoted
  newlines, and lexical integer formatting are covered. Replay still loads all selected metadata;
  it has not gained fragment seeking or a streaming index.

Identical synthetic CSV, 216,000 rows (one hour at 60 fps), old code versus this implementation:

| Host measurement | Before | After |
|---|---:|---:|
| Replay retained Python allocations (`tracemalloc`) | 62.69 MiB | 9.03 MiB |
| Export retained Python allocations (`tracemalloc`) | 200.87 MiB | 10.74 MiB |
| Replay peak RSS increase, fresh untraced process | 74.33 MiB | 19.44 MiB |
| Export peak RSS increase, fresh untraced process | 234.17 MiB | 21.91 MiB |

RSS is measured with `resource.getrusage` before and after each call; Python allocation measurements
are separate runs. Parsing times varied, so these are memory results, not a CPU-speedup claim.
The median-interval calculation still temporarily builds a list of timestamp differences.

Short live-path A/B runs with `core-driver/tools/live_buffer_benchmark.py`, recording disabled and
an attached transport consumer (three seconds each, sequential on this host):

| Runtime / workload | Bytes CPU per frame | Owned-buffer CPU per frame |
|---|---:|---:|
| GStreamer 1.28.2 unixfd, 2448x2048 GRAY8 at 24 fps | 6.9728 ms | 4.9399 ms |
| GStreamer 1.28.2 unixfd, 1920x1080 I420 at 30 fps | 4.4957 ms | 3.4085 ms |
| GStreamer 1.20.3 shm, 2448x2048 GRAY8 at 24 fps | 8.7177 ms | 7.9534 ms |

Every expected frame arrived (72 or 90 per run), with zero source gaps, enqueue failures, or publish
drops. CPU includes the synthetic source, core, and same-process consumer. These short samples show
the copy-path effect on this host, not a production or Jetson capacity limit. Reproduce each pair
with and without `--legacy-bytes`; `--raw-endpoint --preview` also exercises those branches:

```sh
python3 core-driver/tools/live_buffer_benchmark.py --seconds 3 --legacy-bytes
python3 core-driver/tools/live_buffer_benchmark.py --seconds 3
python3 core-driver/tools/live_buffer_benchmark.py --seconds 3 --width 1920 --height 1080 --format I420 --fps 30 --legacy-bytes
python3 core-driver/tools/live_buffer_benchmark.py --seconds 3 --width 1920 --height 1080 --format I420 --fps 30
```

The ROS 2 component library builds on Lyrical with GStreamer 1.28.2 and passes its `image_copy`
CTest. The full hardware-free Python suite on that runtime reports 792 passed and two failures;
both failures reproduce on the unchanged baseline, where the multipart export fixture tries to
rewrite PTS on an immutable probe buffer under the newer PyGObject. The new tests do not bypass
those checks. Hardware capture, NVENC, and Jetson performance qualification are covered below.
On GStreamer 1.20.3 (Ubuntu 22.04), all 120 focused tests pass: live buffer ownership and reconnect,
ROS input overload, compact metadata, replay buffers/integrity, mixed recording settings, proxy
export (including the multipart fixtures), publication throttling, and shm input. After the final
queue-rejection adjustment, the 41 affected buffer/publication tests also pass again on 1.28.2.

## 12. Orin validation of the copy and metadata changes

Tested on November 13, 2026 on `10.160.1.21`, AGX Orin, L4T R39.2.1,
kernel 6.8.12-1021-tegra, 12 CPUs, MAXN.
Source was an isolated snapshot of this working tree based on `356c656`; the unchanged commit
was staged separately for A/B comparisons. Existing services, camera controls, power mode, and
clock settings were left in place. Test containers used private sockets and read-only source mounts.

Functional results:

- **794/794 Python tests pass** on each of `local/cam-core:jp7` (GStreamer 1.24.2) and
  `local/cam-dev:gst1289-dev` (1.28.9), with no skipped tests. Both multipart export fixtures pass
  on these target images. The suite was CPU-limited to four cores; these timings are not benchmarks.
- **NVDEC and source-copy:** each runtime passes eight synthetic H.264/H.265 cases, 40 frames each: raw output
  off/on, before/after source reopen. Recorded picture NAL payloads are byte-identical, every
  decoded frame and sidecar row survives, and capture timestamps match. Live NVDEC pixels match
  decoding the recording, and retained owned frames stay intact after teardown and reopen.
  Matroska packet framing and parameter-set placement can differ; NAL payload comparison accounts
  for that without relaxing picture-data equality.
- **NVENC fragments:** on JP7, the `videoconvert` and `nv12` feeds separately pass 12/12 random
  2448x2048 frames bit-exact through the hardware encoder and software decoder.
- **Whole recording path:** 48 frames of 2448x2048 GRAY8 at 24 fps pass through live owned buffers,
  the real NV12/NVENC recorder, and replay with exact pixels, IDs, and timestamps. A second run of
  48 640x512 GRAY16 frames at 60 fps proves the folded split's byte fallback and exact 16-bit
  reconstruction. Both have zero source gaps, enqueue failures, or publish drops.
- **Real USB:** the attached NexiGo webcam supplies MJPEG 1280x720 at requested 30 fps: 60 frames
  recorded with raw output disabled, then stop/reopen and 60 with raw output enabled. Both have
  exact CSV IDs/timestamps, exact decoded pixels, and zero reported drops. The second run delivers
  60 owned frames and 60 identical preview frames; retained pixels survive stop. `jpegparse` removes
  1–7 padding bytes after JPEG EOI on some input packets: each affected recorded packet is an exact
  source prefix, with compressed image data unchanged. The camera is released after the test.
- **ROS 2:** the actual current and baseline component libraries build on the Orin and pass the
  same `image_copy` integration test, covering both transports, six YUV layouts, short inputs,
  pixel/metadata equality, and retained message lifetime.

The same 216,000-row CSV was measured in fresh Python processes on the Orin. Peak RSS increase
after imports falls from **72.27 to 9.44 MiB** for replay and **231.95 to 10.12 MiB** for export.
Separate `tracemalloc` observations give retained allocations of 62.72 → 9.04 MiB and
200.90 → 10.75 MiB respectively. No parsing-speed claim is made.

Live-buffer CPU was measured with `core-driver/tools/live_buffer_benchmark.py`: a paced fake USB
source feeds the real `CapturePipeline` and an attached unixfd consumer, with recording, preview,
and the raw endpoint off. The same current code uses `--legacy-bytes` to select the original byte
callback path. Each runtime/workload has a three-second warmup followed by ten-second A/B/B/A
samples. All 4,320 measured frames arrive, with no gaps, drops, errors, or forced EOS shutdown.
CPU includes the synthetic source and consumer callbacks. Values are the mean of two samples:

| Runtime | Workload | Bytes CPU ms/frame | Owned-buffer CPU ms/frame | CPU reduction |
|---|---|---:|---:|---:|
| JP7 / GStreamer 1.24.2 | 2448x2048 GRAY8, 24 fps | 21.8555 | 21.8275 | 0.1% |
| JP7 / GStreamer 1.24.2 | 1920x1080 I420, 30 fps | 16.2895 | 16.3849 | −0.6% |
| GStreamer 1.28.9 | 2448x2048 GRAY8, 24 fps | 19.4089 | 13.7403 | 29.2% |
| GStreamer 1.28.9 | 1920x1080 I420, 30 fps | 15.0276 | 11.3502 | 24.5% |

JP7's older unixfd sink still requires the per-frame memfd copy, and this workload shows no
meaningful CPU improvement there. The 1.28 sink exposes its native memory pool and avoids that
Python-side transport work. These are short, serial comparisons on the running Orin with normal
dynamic clocks and existing background services, not a production throughput or latency guarantee.

The C++ ROS bridge was measured separately with unchanged-baseline/current/baseline libraries,
a paced 1920x1080 I420 unixfd producer, and an interprocess typed ROS subscriber. Each run used
an isolated Zenoh router, 90 warmup frames, and a 20-second measurement window; CPU is for the
bridge process only. All runs sustain 30 fps with zero gaps and zero metadata or timestamp errors;
full pixel equality is sampled every 30 frames, with no mismatches. Baseline samples are 28.1032
and 28.1833 CPU ms/frame; the optimized sample is 28.0167.
The **0.45% difference establishes no material CPU improvement** for ROS publication on this host,
despite removing the intermediate RGB allocation/copy. An earlier FastDDS attempt could not
deliver enough large images during warmup and is excluded from the comparison.

Raw text logs, reproduction harnesses, source/fixture hashes, and JSON results are retained on the
Orin under `/tmp/camera-opt-check.89bAqI/reports/`. Captured webcam media stays on the Orin.

## 13. Follow-up: ROS shared-memory transport

On the same Orin, the current Release bridge was measured with a paced 1920x1080 I420 producer
and a separate typed ROS subscriber receiving `rgb8` at 30 fps. The installed Zenoh configuration
defaults to SHM disabled. Both endpoints and the private router were explicitly configured for
each mode, inside an isolated container with a 1 GiB `/dev/shm`; production services were unchanged.

Each sample runs for 20 seconds after 90 warmup frames, in the order off/on/pool/pool/on/off.
All 3,604 measured frames pass metadata and timestamp-order checks, with zero gaps; full pixel
equality is sampled every 30 frames. The table reports means of the two samples per mode, including
the mean of their individual p95 latencies. Latency runs from the synthetic producer's host timestamp
to the subscriber callback, not from a physical sensor exposure.

| Mode | Bridge CPU ms/frame | Bridge + subscriber + router CPU ms/frame | Subscriber p95 ms | Bridge RSS MiB |
|---|---:|---:|---:|---:|
| SHM off | 27.50 | 39.10 | 34.95 | 73.0 |
| SHM on | 20.64 | 25.69 | 26.93 | 144.3 |
| SHM on, serialization pool capped at 64 MiB | 20.84 | 26.25 | 27.65 | 144.6 |

SHM reduces bridge CPU by **25.0%** and the measured ROS processes' combined CPU by **34.3%**.
The extra serialization-pool capacity shows no benefit in this workload. RSS includes shared
mappings: these per-process values must not be added together as physical RAM usage.

This is verified SHM delivery, rather than merely an enabled setting: both processes map matching
Zenoh shared segments, and loopback receive traffic falls from **6,235,436 to 192 bytes/frame**.
All enabled samples use a **48 MiB transport pool** and a **512-byte message threshold**. The pool
variant changes only `RMW_ZENOH_BUFFER_POOL_MAX_SIZE_BYTES=67108864` at both ROS endpoints.

For same-host subscribers, enable `zenoh_shm: true` on the bridge and the corresponding SHM
configuration on the subscriber; they must share the same IPC namespace. The existing
[ROS bridge configuration](../../plugins/ros2-bridge/README.md#tuning-zenoh-for-large-images) documents these
settings. Remote peers and larger/multiple image streams need their own measurements and pool sizing.
This test does not change deployment defaults.

Reproduction scripts, individual JSON samples, process mappings, version details, and
`ros-shm-summary.json` remain in the isolated Orin staging directory and its `reports/` directory.

## 14. Follow-up: indexed late-window replay

Replay can start decoding at the fragment containing a later window. The first indexed open scans
the compressed session and checks its total packet count and per-packet PTS against the CSV before
trusting a frame offset. A missing first packet cannot be mistaken for a harmless muxer offset.
Reordered/B-frame timing and other ambiguous layouts retain sequential decoding. A window in the
first half of the session, or in its first fragment, also retains the original path to avoid a full
compressed scan for a small skip. The index stays in memory and is reused on restart only while
the CSV and video files' stat identities remain unchanged.

On the Orin's GStreamer 1.28.9 image, the same 3,600-frame, 1280x720 GRAY8/30 fps FFV1 fixture
was used before and after, in A/B/B/A order. It has 12 fragments and the window starts at frame
3,450. File hashes are verified before timing, so these are **warm filesystem cache** measurements.
All 150 delivered frames in each phase have exact pixels, IDs, and timestamps.

| Measurement | Before, two samples | After, two samples |
|---|---|---|
| Open/configure to first delivered frame | 8.260 / 8.179 s | 0.647 / 0.522 s |
| Restart to first delivered frame | 8.039 / 8.141 s | 0.394 / 0.368 s |
| Frames decoded per phase | 3,600 | 300 |
| Compressed packets scanned for first open / restart | 0 / 0 | 3,600 / 0 |

Mean first-frame delay falls **92.9%** and restart delay **95.3%**. Peak RSS remains approximately
96 MiB. The fixture uses synthetic flat patterns; this is not a cold-storage or arbitrary-codec
speedup claim. Initial indexing still reads the entire compressed session. Skipped pixels are not
decoded, so full pixel/decode validation still requires an unwindowed replay.

`core-driver/tools/replay_window_benchmark.py` generates the fixture and measures exact first-start
and restart delivery against two source snapshots. Orin artifacts are
`reports/replay-window-{before,after}-{a,b}.json` and `reports/replay-window-fixture/` under the
isolated staging directory. No persistent recording files or sidecars are rewritten by indexing.

The final source additionally stops indexing immediately when a non-keyframe start or reordered
packet PTS proves the layout unsuitable; it does not scan the remaining archive before falling
back. This fallback-only shortcut followed the measured runs and leaves their eligible FFV1 path
unchanged. Regression coverage includes successful P-only H.264/H.265 windows and restart, plus
B-frame early fallback, missing-first-packet refusal, changed-file invalidation, and early windows.

## 15. Follow-up: ten-minute combined-load soak

On November 13–14, the Orin ran a synthetic 2448x2048 GRAY8 source at 24 fps for 600 seconds through
owned live buffers, lossless NVENC recording, a headless GStreamer preview, unixfd monitoring, and
the actual ROS bridge with verified Zenoh SHM settings. Separate phases slowed each consumer,
disconnected/reconnected the plugin and ROS consumers, and invoked two source watchdog reconnects.
A 45-second target smoke passed first, with all 1,033 captured frames preserved exactly.

The long run passes full verification:

- **14,354 generated = captured = CSV rows = decoded frames**, with every recorded pixel hash,
  frame ID, and capture timestamp exact. Source gaps, missing frames, enqueue failures, core
  publication drops, and PTS rebases are all zero.
- Both source reconnects complete. Effective capture rate is **24.0001 fps**, excluding about two
  seconds of deliberately injected reconnect silence; no generated frame was lost before admission.
- Every delivered preview/plugin frame is hash-checked during capture. All **13,564 delivered ROS
  images** match captured hashes and stamps, including **11,618/11,618 normal-phase frames**.
  Slow/disconnected consumers intentionally receive fewer frames: preview totals 13,857 and plugin
  monitoring 13,562. The plugin's normal-phase count is 11,617/11,618, allowing for the stress boundary.
- The entire recording is replayed afterward: **71.96 GB of decoded pixels** checked in 111.8 seconds.
  This verification time is separate from the 600-second capture run.

Local receipt latency uses the service's host-arrival timestamp and bounded 1 ms histogram bins:

| Consumer | Normal p95 upper bound | Normal p99 upper bound | Stress p99 upper bound |
|---|---:|---:|---:|
| Headless preview | 10 ms | 11 ms | 130 ms |
| unixfd monitor | 12 ms | 13 ms | 90 ms |
| ROS subscriber | 20 ms | 21 ms | 223 ms |

Phase classification uses receipt time, so a delayed frame can cross a stress boundary; observed
normal-phase maxima were 125/89/206 ms respectively. These are instrumented local measurements,
including full-frame SHA256 checks, not physical exposure-to-display or browser latency.

Capture RSS peaks at **649.3 MiB** and settles there after an early increase of about 15 MiB.
The ROS bridge peaks at **142.3 MiB** and its reader at **163.2 MiB**, with under 0.05 MiB change
between their early and final sample means. Per-process RSS includes shared mappings. This ten-minute
observation establishes stable operation for the tested workload; it is not an indefinite leak test.

`core-driver/tools/combined_load_soak.py` contains the capture, ROS reader, and full verification
commands. It keeps only bounded metadata/histograms in memory and streams manifests to disk.
The Orin staging directory retains `reports/combined-soak-600/summary.json`, per-second metrics,
full manifests, and the recordings; `reports/run_camera_combined_soak.py` and the private ROS
launcher drive the isolated test containers. Media stays on the Orin.

Final regression validation on the Orin/GStreamer 1.28.9 snapshot passes **829 tests**:
672 core-driver, 132 WebRTC, and 25 tooling tests, with no final failures or skips. Tooling's offline
Compose checks use read-only CLI binaries without a Docker daemon socket. The combined final JUnit
report is `reports/camera-final-tests.xml`; passing core/tooling reports retain the individual runs.
Local GStreamer 1.24.2 validation also passes the replay/export group and the final 22 focused
index/integrity cases. All temporary test containers are removed after validation; source snapshots,
reproduction scripts, and test artifacts remain in the isolated Orin staging directory.
