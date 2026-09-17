# Recording codecs and tuning

`recording.encoder: auto` keeps the existing lossless/source-copy selection. An explicit `x264`
selects lighter, **lossy H.264** recording on the CPU, including the ARM64 Mac dev container.
Recording uses the same MKV segments, lifecycle controls and timestamp sidecars. Live raw transport
and preview receive the original frames; the conversion and quantization apply only to the recorder.

The dashboard's **Cameras → Recording control** cards expose these settings while the recorder is
inactive. Choose an encoder, quality/speed and segment length, then **Apply settings** before
activating. Advanced settings expose GOP, Bayer tiling and hardware tuning. The card shows the
resolved encoder, including fallbacks; edits are locked while recording or transitioning. Runtime
changes last until service restart. Each session saves the full requested settings in its own
`<prefix>.recording-settings.json` beside the video files, independently of the config saved at
service bringup. See [the Zenoh API and snapshot contract](LIFECYCLE.md#recording-settings-camera-service).

Stopping recording, applying a different encoder or compression setting, and starting again creates
a new session in the same run. Whole-run replay handles those session boundaries automatically,
including FFV1/H.264 changes and different Bayer tiling, and preserves the recorded gaps. The
sensor's pixel format and dimensions must remain the same. See [replay behavior and its source-copy
restriction](PLAYBACK.md#timeline-several-producers-one-zero).

## Optional H.264 recording

```yaml
recording:
  enabled: true
  encoder: x264
  x264_crf: 23
  x264_preset: ultrafast
  keyframe_interval_s: 1
  segment_seconds: 60
```

- `x264_crf`: integer **1–50**, default **23**. Lower values retain more detail and use more storage;
  try 18 for higher quality, 23 as a starting point, or 28 for smaller files. This is constant
  quality, not a fixed bitrate or file-size guarantee.
- `x264_preset`: `ultrafast` (default), `superfast`, `veryfast`, `faster`, `fast`, `medium`, `slow`,
  `slower`, `veryslow`, or `placebo`. Slower presets spend more CPU searching for compression;
  `veryfast` is another useful starting point when file size matters more than encoding CPU.
- `keyframe_interval_s` applies to H.264 too. Segment boundaries request keyframes so each MKV can
  decode independently. This recording mode uses no B-frames or lookahead; `bframes` is ignored
  with a warning, keeping the live recorder's bounded queue from stalling on encoder latency.

The encoded representation is **8-bit 4:2:0**, with even width and height required. RGB/4:2:2/4:4:4
sources lose chroma detail and alpha is not retained. Bayer remains an approximate raw mosaic;
CFA tiling is disabled because its reversible residual transforms are unsuitable for quantization.
Keep a lossless encoder for pixel measurements. Full 16-bit/thermal sources fall back to FFV1 with
a warning; this option never silently truncates the sensor's low bits. Missing `x264enc` or
`h264parse` also falls back to FFV1.

New sidecar headers record `recording_encoder`, `recording_lossy` and `recording_settings`
(CRF, preset and chroma for x264), after encoder fallback. `recording_lossy` describes loss
introduced by this encode, not the fidelity of the original source; it is unknown (`null`) for
stream-copy. A lossless re-record of lossy playback cannot recover original sensor values;
`replay_of` retains its provenance. Existing sidecars remain readable. Replay uses these fields to
keep raw I420 recordings on the blocking raw reader instead of mistaking them for source-copy runs.

Already-compressed RTSP H.264/H.265 or USB MJPEG sources remain cheapest with `encoder: auto`
(stream-copy). Explicit x264 on those sources decodes and re-encodes them, adding quality loss and
encoding work. H.264 here uses **software encoding**; this change does not enable Apple VideoToolbox
inside Docker or promise hardware replay decoding.

The implementation uses GStreamer's [x264enc constant-quality mode](https://gstreamer.freedesktop.org/documentation/x264/index.html),
with zero-latency tuning and its VBV buffer disabled for CRF recording, so the element's default
streaming bitrate cap does not override the requested quality. The required x264 and parser plugins
are already in the dev image.

Runnable example: `core-driver/config/usb-fake-h264.yaml`. Apply the `recording:` block above to a
sensor configuration and restart that instance to change its encoder; existing recordings are untouched.

### Development measurement (2026-09-13)

ARM64 Docker, GStreamer 1.28.7: the same 250 NV12 640×480 SMPTE test frames (10 seconds at 25 fps)
encoded twice per setting, reversing order on the second pass. Inputs were generated once before
measurement. CPU times include recorder conversion/muxing and frame submission, or software decoding
back to NV12; they exclude input generation and quality analysis.

| Setting | MKV size | Encode CPU seconds | Decode CPU seconds |
|---|---:|---:|---:|
| FFV1 (existing default) | 6.09 MB | 0.676 | 0.751 |
| x264 ultrafast, CRF 18 | 4.32 MB | 0.307 | 0.119 |
| x264 ultrafast, CRF 23 | 3.51 MB | 0.305 | 0.120 |
| x264 ultrafast, CRF 28 | 2.89 MB | 0.305 | 0.117 |

CRF 23 used about 55% less encoding CPU, 84% less decoding CPU and 42% less storage in this synthetic
comparison. All 250 frames and timestamps survived; every segment decoded independently. This is not
a whole-dashboard benchmark or a guarantee for real footage; noise, motion and resolution change the
tradeoff. A separate live fake-USB test recorded 199 frames across two activate/deactivate sessions,
kept transport flowing while inactive, and replayed/re-recorded every decoded frame and original
capture stamp. Physical sensor/Jetson hardware was not tested for this option.

## Lossless CFA tiling and temporal compression

The lossless paths (HW HEVC on the Orin, FFV1/x265 on CPU) remain unchanged. For an 8-bit **Bayer**
camera, the options below can shrink files while retaining exact pixels. They are recorder-only;
transport, preview and raw endpoints continue to see the original mosaic.

> **>8-bit / 16-bit (e.g. thermal Y16) cameras** take the **FFV1** path instead — see
> [the FFV1 section below](#ffv1-path-for-16-bit-and-thermal-cameras). The Bayer-tiling and
> temporal-window levers here are HW-HEVC/8-bit concepts and don't apply (FFV1 is intra-only); the
> relevant knob there is throughput, not ratio.

## 1. CFA tiling — `recording.bayer_tile`

A Bayer mosaic is a high-frequency colour checkerboard: adjacent pixels are different colours, which
defeats a lossless codec's spatial predictor and (for video) its motion compensation — a 1-px shift
changes the CFA phase, so the previous frame is a poor predictor. Tiling deinterleaves the mosaic into
its four sub-planes and packs them as the four quadrants of one same-size frame, so each quadrant is a
smooth same-colour image. The recorded file is then tiled (the sidecar's `cfa_tile_mode` flags it;
playback must un-tile with `cam_driver.bayer_tile.untile_cfa` before demosaicing).

| mode | what | use when |
|---|---|---|
| `off` | record the raw mosaic (default) | non-Bayer, or you want a standard-playable file |
| `plain` (`true`) | quadrant tiling only | **the big, robust win** — almost always helps |
| `green_diff` | + store `(Gb−Gr)+128` | safe, near-free extra (the greens are the most-correlated pair) |
| `rct` | + reversible `R−G`/`B−G`/`Gb−Gr` (all +128) | higher ceiling, but **measure it** — see below |

All residuals are **+128-centred** because the HW path is 8-bit: residuals must wrap mod 256, and
centring keeps small diffs near smooth mid-grey instead of recreating 0↔255 jumps that a *predictive*
codec (HEVC) punishes. That same wrap is why `rct` can *lose* on a predictive codec for saturated
colour (the wrap tax) even though it wins for an entropy coder (ffv1) — so `rct` is opt-in, not default.

## 2. Temporal window — `recording.keyframe_interval_s` / `bframes`

- `keyframe_interval_s` — the I-frame ("keyframe") spacing in **seconds**; the longest span the encoder
  can predict across. Bigger = smaller files but coarser seek + less corruption resilience. `0` = the
  encoder default. Maps to `iframeinterval` (HW) / x265 `keyint`. Ignored by ffv1 (intra-only).
- `bframes` — B-frames between references (`0` = P-only). HW lossless B-frame support is
  firmware-dependent — verify on-device. Ignored by ffv1.

Every `.mkv` segment still starts on a keyframe (`splitmuxsink send-keyframe-requests`), so it's
independently decodable even with a long GOP.

## NVENC knobs — `recording.nvenc_preset` / `nvenc_maxperf`

`nvenc_preset` (`ultrafast|fast|medium|slow`, or `0-4`) sets the HW encoder's `preset-level`; `nvenc_maxperf`
toggles `maxperf-enable`. **Measured finding: for *lossless* these are moot** — on the Orin, sweeping
ultrafast→slow changed neither file size nor encode speed (preset tunes rate-distortion decisions that
only matter in lossy mode; in lossless the residual is coded exactly). `num-B-Frames` is Xavier-only (a
no-op on Orin); `maxperf-enable` is deprecated. They're exposed for completeness / future lossy use and
so you can re-verify per camera with the benchmark's `--preset` sweep.

## The linchpin: does this encoder actually do temporal prediction? — `tools/probe_temporal.py`

Tiling's *temporal* half and the window knob only matter if the encoder emits delta (P/B) frames that
are meaningfully **smaller** than keyframes. Whether NVENC's *lossless* mode does that is
firmware-dependent — **measure it before tuning**:

```bash
docker run --rm -v /data/recordings:/rec --entrypoint python3 cam-core:jp7 \
    tools/probe_temporal.py /rec/<prefix>-<stamp>-00000.mkv
```
- *all keyframes* → intra-only: the window knob is moot, tiling helps only spatially, and HW buys
  throughput not ratio (ffv1 is competitive on intra).
- *delta ≪ keyframe* → temporal is working: tune the window, lean on `plain`+`green_diff`.
- *delta ≈ keyframe* → inter isn't helping on this content (the mosaic's CFA-phase problem — which tiling
  may fix — or the sensor-noise floor, which it can't). Re-probe a tiled recording to tell which.

## Measure the actual gain on your footage — `tools/tiling_benchmark.py`

Decodes a real mosaic recording back to frames (the mosaic rides in the Y plane), re-encodes the same
frames as mosaic vs `plain`/`green_diff`/`rct` through each available encoder, and prints bytes/frame,
total, ratio-vs-mosaic, and (for HEVC) the P/B-vs-I sizes — so you see both the spatial gain and whether
tiling unlocks temporal.

```bash
# run on the ORIN -- it has nvenc (your real encoder) via CDI, and is fast enough at full sensor res:
docker run --rm --device nvidia.com/gpu=all -v /data/recordings:/in -v /tmp/bench:/work \
    --entrypoint python3 <cam-core image> \
    tools/tiling_benchmark.py /in/<prefix>-<stamp>-00000.mkv --frames 120 --encoders nvenc,ffv1 --work /work
# sweep nvenc presets / match your recording's GOP:
#   ... --encoders nvenc --modes mosaic,plain --preset ultrafast,medium,slow --gop-seconds 10
```
Geometry/pattern come from the sidecar `.json`; fps from the `.csv`. It reports bytes/frame, ratio vs
mosaic, **`enc-fps`** (encode throughput — must be ≥ camera fps to record real-time), and **`P/B vs I`**
(temporal). The CPU legs (x265/ffv1) run anywhere, but x265 at 5 MP is impractically slow off-Jetson —
the **nvenc** leg is the one that represents your recorder, so run the benchmark on the box. The image
needs numpy (the published `cam-core` may predate it: `cam-core:bench` = base `+ apt install python3-numpy`).

## FFV1 path for 16-bit and thermal cameras

A camera the auto-selector can't put on the HW path — **>8-bit** (Mono16, Bayer16, thermal Y16 →
`GRAY16_LE`), color (YUV/RGB), or any host with no NVENC — records **FFV1**: truly lossless, all bits
preserved, but **intra-only** (each frame is independent). So the CFA-tiling and temporal-window knobs
above don't apply (there's no inter-frame prediction to tune, and `keyframe_interval_s`/`bframes` are
ignored with a log note). `x265-lossless` is offered for CPU *temporal* lossless but is **8-bit only**:
a >8-bit request rides in a GRAY16 container x265's ≤12-bit input formats can't carry, so it falls back
to FFV1 rather than silently truncating sensor bits.

The one thing that matters for FFV1 is **throughput**, and it has a real cliff: single-threaded
`avenc_ffv1` caps around **27 fps for 16-bit 640×512 on an Orin core**, so a 60 fps thermal camera
stalls the recorder (the tee backpressures, consumers starve, the feeder drops frames with
`enqueue_failures` climbing). The recorder therefore runs FFV1 **multi-threaded across slices**
(`threads=auto slices=4`), which restores real-time with headroom — no config knob, it's automatic.
Once the encoder keeps up, the next ceiling is **sustained disk write** (~25–30 MB/s for 60 fps 16-bit
640×512 — noisy thermal data barely compresses): if `camsrc queue full` warnings persist with the CPU
idle, the `recording.output_dir` storage is the bottleneck — drop the frame rate or point it at faster
media (NVMe over SD/eMMC). The radiometric data lands in the `.mkv` bit-exact regardless; for the
operator's WebRTC preview see `CAM_WEBRTC_NORMALIZE` in the [webrtc-bridge README](../plugins/webrtc-bridge/README.md).

## Recommended path
1. `probe_temporal` an existing recording → know if temporal is real on your encoder.
2. `tiling_benchmark` on the Orin → real numbers per mode × encoder on your scenes.
3. Set `bayer_tile: plain` (or `green_diff`); add `keyframe_interval_s` only if the probe shows temporal
   pays. Treat `rct` as a per-scene benchmark result, not a default.
