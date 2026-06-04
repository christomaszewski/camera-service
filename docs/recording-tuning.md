# Lossless recording: CFA tiling + temporal-compression tuning

The recorder writes lossless video (HW HEVC on the Orin, ffv1/x265 on CPU). For an 8-bit **Bayer**
camera there are two levers to shrink the files, both under `recording:` in the sensor config. They're
opt-in and recorder-only — transport/preview/raw always see the raw mosaic, so ROS/WebRTC are untouched.

## 1. CFA tiling — `recording.bayer_tile`

A Bayer mosaic is a high-frequency colour checkerboard: adjacent pixels are different colours, which
defeats a lossless codec's spatial predictor and (for video) its motion compensation — a 1-px shift
changes the CFA phase, so the previous frame is a poor predictor. Tiling deinterleaves the mosaic into
its four sub-planes and packs them as the four quadrants of one same-size frame, so each quadrant is a
smooth same-colour image. The recorded file is then tiled (the sidecar's `cfa_tile_mode` flags it;
playback must un-tile with `gige_driver.bayer_tile.untile_cfa` before demosaicing).

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

## The linchpin: does this encoder actually do temporal prediction? — `tools/probe_temporal.py`

Tiling's *temporal* half and the window knob only matter if the encoder emits delta (P/B) frames that
are meaningfully **smaller** than keyframes. Whether NVENC's *lossless* mode does that is
firmware-dependent — **measure it before tuning**:

```bash
docker run --rm -v /data/recordings:/rec --entrypoint python3 gige-core:jp7 \
    tools/probe_temporal.py /rec/<prefix>-00000.mkv
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
# run on the ORIN -- it has nvenc (your real encoder) and is fast enough at full sensor res:
docker run --rm -v /data/recordings:/in -v /tmp/bench:/work --entrypoint python3 gige-core:jp7 \
    tools/tiling_benchmark.py /in/<prefix>-00000.mkv --frames 120 --encoders nvenc,x265,ffv1 --work /work
```
Geometry/pattern come from the sidecar `.json`; fps from the `.csv`. The CPU legs (x265/ffv1) run
anywhere, but x265 at 5 MP is slow off-Jetson — the **nvenc** leg is the one that represents your
recorder, so run the benchmark on the box.

## Recommended path
1. `probe_temporal` an existing recording → know if temporal is real on your encoder.
2. `tiling_benchmark` on the Orin → real numbers per mode × encoder on your scenes.
3. Set `bayer_tile: plain` (or `green_diff`); add `keyframe_interval_s` only if the probe shows temporal
   pays. Treat `rct` as a per-scene benchmark result, not a default.
