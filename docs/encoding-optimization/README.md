# Encoding optimization — handoff

Status as of 2026-11-11: **investigation only, nothing implemented.** This document carries a design
discussion over to a session that has a Jetson AGX Orin, so it can implement and test.

The goal is to lower the CPU cost of encoding, for recordings (16-bit thermal first) and for the WebRTC
preview streams, and to fix slow WebRTC recovery after the radio link drops.

## 0. Read this first

- **Code state:** `main` is `v1.13.0` (`6d3af8b`). Line references below are to that tree.
- **Where the evidence came from:** an x86-64 sandbox (4-core Xeon, Ubuntu 24.04, GStreamer 1.24.2, Python
  3.12). It had no Jetson, no hardware encoder, no Docker daemon and no `webrtcsink` runtime. Every statement
  about the hardware encoder's behaviour, and every CPU number, must be re-measured on the Orin.
- **Third-party source that was read:** gst-plugins-rs **0.15.4**, the version the bridge image builds, from
  the published crates: `https://static.crates.io/crates/<name>/<name>-0.15.4.crate` for
  `gst-plugin-webrtc`, `gst-plugin-webrtc-signalling` and `gst-plugin-rtp`. `file:line` references to those
  crates are given as e.g. `webrtcsink/imp.rs:983`.
- **Related review:** `docs/review-v1.13.0/` on the same branch. Findings it numbers (DP-1, PB-11, BR-9 …)
  are cited where they bear on this work.
- **Scripts:** `scripts/` holds three measurement scripts that run today on the software path and take
  `--encoder hw-hevc-lossless` on a Jetson. See section 5.

### Decisions the owner has already made

1. Encoding the WebRTC stream once for all viewers is wanted, **as an opt-in mode**. Per-viewer encoding
   stays the default.
2. Stale WebRTC session handling is wanted as well.
3. A single bitrate for all viewers is acceptable: the viewers sit on one wired network behind the single
   radio that receives from the vehicles.
4. Nothing below is to be implemented without a Jetson to test on.

---

## 1. Recording

### 1.1 What the recorder does today

| Fact | Where |
|---|---|
| Encoder choices: `auto`, `hw-hevc-lossless`, `ffv1`, `x265-lossless`, `stream-copy`, `x264` | `core-driver/cam_driver/formats.py:30` |
| `auto`: already-encoded source → `stream-copy`; colour (RGB/YUV) → `ffv1`; 8-bit mono or Bayer → `hw-hevc-lossless`; more than 8 bits → `ffv1` | `formats.py:148-162` |
| Any of `x265-lossless`, `hw-hevc-lossless`, `x264` asked for on more-than-8-bit input falls back to FFV1 with a warning | `recorder.py:156-165` |
| FFV1 settings are fixed: `avenc_ffv1 coder=1 context=1 threads=0 slices=4 slicecrc=1` | `recorder.py:246` |
| Hardware path: `videoconvert ! NV24 (full-range pin) ! nvvidconv ! NVMM NV24 ! nvv4l2h265enc enable-lossless=1` — the image rides in the luma plane | `recorder.py:195-217` |
| The only lossy mode is CPU `x264` (8-bit 4:2:0, constant quality) | `recorder.py:179-193`, `docs/recording-tuning.md` |
| There is **no uncompressed mode**; `stream-copy` on a raw source falls back to FFV1 | `recorder.py:146-148` |
| There is **no hardware lossy mode** for any source | — |

So a 16-bit thermal stream is always FFV1 on the CPU, and debayered colour is FFV1 on the CPU too. Only
8-bit mono and Bayer mosaics use the hardware encoder.

The one existing lossy route for 16-bit data is offline: the `h264` export profile stretches GRAY16 to 8-bit
and encodes H.264 as a derived copy (`core-driver/cam_driver/export_recordings.py`,
`export-profiles/h264.yaml`, `docs/proxy-exports.md:65-74`). The pixels become display intensities, not
radiometric counts, and the originals are kept.

### 1.2 What the Orin hardware encoder accepts

From NVIDIA's Jetson Linux r39.2 documentation (links in section 6):

- H.265 hardware encode is supported on Orin.
- Listed inputs: YUV420, YUV444 / NV24 (8-bit 4:4:4, tagged "Jetson Orin only"), P010_10LE (10-bit 4:2:0).
  10-bit 4:4:4 is tagged "Jetson Thor only".
- "For 10-bit encoding, YUV input must be 16-bit MSB aligned."
- No 12-bit or 16-bit input and no monochrome format is listed.

A comment in `recorder.py:157-163` adds that the Orin has no 10- or 12-bit *lossless* mode. That was not
re-checked.

**Consequence:** 16-bit samples cannot go into the hardware encoder as they are.

### 1.3 Measurements (x86, synthetic data — indicative only)

Frames: 640×512 GRAY16, a smooth ~300 K scene in 0.01 K counts plus Gaussian noise (σ = 6 counts unless
noted). Real thermal footage is noisier: `docs/recording-tuning.md` puts FFV1 on real 60 fps thermal at
25–30 MB/s, against 39.3 MB/s raw.

| Path | Size vs raw | Bit-exact | CPU per frame |
|---|---|---|---|
| FFV1 on GRAY16 (today) | 31.3% | 60/60 | 16.2–16.7 ms (24.2 ms at σ = 40) |
| FFV1 with the PR #65 speed knobs (`coder=0 context=0 slicecrc=0`) | 31.0% | — | 15.3–15.5 ms |
| Uncompressed GRAY16 in Matroska | 100% | — | 0.3 ms |
| Byte split → software x265 lossless, plain | 37.1% | 60/60 | 56.8 ms |
| Byte split → software x265 lossless, folded low byte | 33.8% | 60/60 | 56.5 ms |
| Byte split, CPU-side steps only (numpy split + GRAY8→NV24) | — | — | 1.65 ms |

What these do and do not show:

- **The byte split is exactly reversible through the service's own 8-bit lossless recorder fragment and its
  own decode chain.** That is the part that was proven.
- **No CPU saving has been measured**, because the hardware encoder was never run. Software x265 lossless
  costs about 3.5× FFV1 and is not an option. The 1.65 ms row is only the work that would remain on the CPU;
  it leaves out the copy into encoder memory, the driver, and parsing and muxing the output.
- **The FFV1 speed knobs do nothing useful for 16-bit.** libav logs `bits_per_raw_sample > 8, forcing range
  coder`, so `coder=0` is ignored. On 8-bit input the same knobs cut CPU by roughly a quarter in one
  synthetic test, so they may still matter for colour.
- **Matroska takes raw GRAY16 directly.** `gst-inspect-1.0 matroskamux` (1.24.2) lists raw `YUY2, I420, YV12,
  UYVY, AYUV, GRAY8, GRAY10_LE32, GRAY16_LE, BGR, RGB, RGBA64_LE, BGRA64_LE`. Not NV12, not GRAY16_BE.

### 1.4 Options

#### R1 — Uncompressed mode (`encoder: raw`)

The simplest CPU cut. Costs disk: 39.3 MB/s for 640×512 at 60 fps, i.e. roughly 30–55% more than FFV1 on
real thermal by the documented figures.

What it needs:

- A recorder fragment with no encoder: `queue ! splitmuxsink muxer=matroskamux` (`recorder.py:_splitmux`).
  Check on GStreamer 1.20, 1.24 and 1.28 that `splitmuxsink` splits raw video cleanly.
- `VALID_ENCODERS` (`formats.py:30`), `recording_settings.CHOICES`, `select_encoder`, the sidecar's
  `recording_encoder` / `recording_lossy` (`pipeline.py:787-808`, `sidecar.py:69`).
- The read path: `sources/replay.py:64-90` (`_RAW_DECODE`, `raw_decode_chain`) keys on the track's caps name
  and has no entry for raw video; `export_recordings.py` likewise.
- Restrict it to the formats Matroska accepts, and refuse the rest with a named error.
- Mind review finding DP-1: the core pushes tightly packed rows, and GStreamer elements assume rows padded to
  4 bytes. A raw recording of a frame whose row is not a multiple of 4 bytes will be read back wrongly unless
  that is handled or refused.

#### R2 — 16-bit on the hardware encoder by byte split (lossless)

Split each 16-bit pixel into high and low byte, place the two halves side by side as one GRAY8 frame twice
as wide, and feed it to the existing `hw-hevc-lossless` path. Recombine on read.

The recorder already has this pattern for Bayer: a reversible rearrangement before the unchanged lossless
encoder, recorded in the sidecar and undone on read. Follow it:

| Piece | Bayer tiling today | Byte split would add |
|---|---|---|
| Transform | `core-driver/cam_driver/bayer_tile.py` (`tile_cfa` / `untile_cfa`) | a split / join pair (see `scripts/gray16_split_roundtrip.py` for both variants) |
| Config | `recording.bayer_tile` (`config.py:233`) | a mode for 16-bit sources |
| Applied when recording | `pipeline.py:815-832` (`_recording_plan`) | same place |
| Recorded in sidecar | `cfa_tile_mode` (`sidecar.py:49`, `pipeline.py:799`) | a new field |
| Undone in replay | `sources/replay.py:331-335` | same place |
| Undone in export | `export_recordings.py:255-269` | same place |

Two variants were tested: plain, and "folded", where the low byte is mirrored wherever the high byte is odd.
Folding removes the sawtooth edge at each carry and compressed about 9% better in the software test. Both
are exact.

Constraints and risks:

- The split frame's width (2 × width) must keep rows a multiple of 4 bytes (DP-1). 640 → 1280 is fine.
- **Never apply a lossy codec to the split frame.** An error in the high byte is a 256-count error.
- The hardware lossless path was validated bit-exact for 8-bit luma on a JetPack 7 AGX Orin
  (`docs/ROADMAP.md`, `docs/jetpack7-bringup.md`); JetPack 6 is recorded as pending. The split relies on
  exactly that path, so it inherits that status.
- A second layout — low byte in the chroma planes of NV24, same geometry, no `videoconvert` — was not tried.
  It depends on chroma surviving `nvvidconv` and the lossless encoder exactly, which nobody has checked.

#### R3 — Lossy on the hardware (not recommended for radiometric data)

- **10-bit HEVC (P010).** Supported by the hardware. Needs 16 → 10 bit reduction first (a fixed window or a
  shift), which loses radiometric precision before compression. No lossless mode at 10 bits.
- **8-bit normalized.** Reuse `thermal_preview.PercentileStretch` and encode lossy H.265 on the hardware. A
  visual record only. Needs a hardware lossy recorder mode, which does not exist for any source today.

Parked unless the owner asks for a visual-only thermal record.

#### R4 — FFV1 speed knobs (PR #65)

Not useful for 16-bit (section 1.3). Possibly useful for colour, which is also FFV1 on the CPU. PR #65 needs
rework before it can merge: see BR-9, BR-10 and BR-11 in `docs/review-v1.13.0/07-branches-and-open-prs.md`.

### 1.5 What to do on the Orin, in order

1. Run the two scripts in section 5 inside the `cam-core` image with `--encoder hw-hevc-lossless`, first on
   synthetic frames, then on real thermal footage (`--raw-file`). Record: bit-exact count, size, and CPU.
   For CPU, do not trust the script's process figure alone — it excludes kernel-side encoder work. Watch
   `tegrastats` and `top -H` during the run and compare against the FFV1 run.
2. If R2 is bit-exact and clearly cheaper than FFV1, implement it following the table in R2, with tests
   modelled on the Bayer tiling ones (`core-driver/tests/test_replay_recording_changes.py`) and an entry in
   `core-driver/tools/nvenc_lossless_test.py`, the hardware qualification tool.
3. Implement R1 regardless if disk allows; it is small and independent.
4. Repeat step 1 on JetPack 6 if those vehicles carry thermal cameras.

### 1.6 Bayer mosaics on the hardware path — which knobs matter

This is the path 8-bit Bayer cameras already use (`hw-hevc-lossless`). The repo's own Orin measurement
(`docs/recording-tuning.md`, R39 Orin, MAXN, 2448×2048, noise frames) puts the **whole recorder path at
67 ms of CPU per frame, of which the colour step is 7.9 ms**. So most of the CPU is spent after the colour
step — copying into encoder memory, the encoder's CPU side, parsing and muxing a large lossless bitstream,
and writing it — and nobody has measured how that remainder splits.

Knobs that exist (`recording:` in the sensor YAML):

| Knob | Effect on load |
|---|---|
| `nvenc_preset`, `nvenc_maxperf`, `bframes` | **None in lossless mode**, by the repo's own Orin sweep: neither file size nor encode speed changed. `maxperf-enable` is deprecated and B-frames are a no-op on Orin |
| `videoconvert_threads` (default 4) | Spreads the colour step over cores. Faster wall-clock, not less total CPU, and that step is the small part |
| `bayer_tile` (`off` / `plain` / `green_diff` / `rct`) | Tiling runs in numpy on the capture thread: 5 / 7 / 10 ms per 5 MP frame on x86 (more on ARM). It also shrinks the bitstream, which is what the parse, mux and disk work scale with. The net effect on the Orin is unmeasured |
| `keyframe_interval_s` | Only helps if this encoder does temporal prediction in lossless mode, which is firmware-dependent (`core-driver/tools/probe_temporal.py`). If it does, a longer window shrinks the bitstream |
| Camera frame rate and ROI | Everything scales with pixels per second |

There is no hardware *lossy* recorder mode; the lossy option (`x264`) runs on the CPU.

Changes that would cut CPU but are not knobs today:

1. **Feed NV24 straight from the feeder and drop `videoconvert`.** The mosaic is the luma plane as it is; the
   chroma is a constant that can be one shared read-only block appended to every buffer. Measured on x86 at
   5 MP: `videoconvert` 2.8–3.5 ms per frame against roughly zero for the shared-chroma buffer. Worth up to
   the 7.9 ms the Orin spends on that step. Needs a bit-exact check through `nvvidconv` and the encoder.
2. **Find where the other ~59 ms goes.** Run the recorder on the Orin with `top -H` (or the per-thread CPU
   line from PR #65) and split it between `nvvidconv`, the encoder, `h265parse`, the muxer and the feeder.
   This decides whether anything below is worth doing.
3. **Skip the unused main-pipeline copy** when there is no preview and no raw endpoint (the default). Every
   frame is still wrapped and pushed through a pipeline that ends in a `fakesink` — about 120 MB/s of copying
   at 5 MP and 24 fps (review, `02-core-data-path.md`, improvement 1).
4. **Try `nvvidconv` for the GRAY8 → NV24 step** so the conversion happens off the CPU. Unverified: whether
   it accepts GRAY8 for this conversion and whether it keeps the plane exact.

`scripts/bayer_hw_path_cpu.py` measures the CPU-side steps anywhere and the full hardware fragment, with and
without tiling, on a Jetson.

---

## 2. WebRTC streams

### 2.1 How the bridge is built

- `plugins/webrtc-bridge/run.sh:223-256` assembles the pipeline:
  `source ! leaky queue ! [debayer] [scale] videoconvert ! I420 ! [overlay] ! webrtcsink`.
- `run.sh` sets only these `webrtcsink` properties: `signaller::uri`, `video-caps`, `min-bitrate`,
  `max-bitrate`, `start-bitrate`, `congestion-control` (`run.sh:223-231`).
- `plugins/webrtc-bridge/tools/bridge_stream.py` owns the pipeline. It hooks `request-encoded-filter` and
  `encoder-setup` (`:413-416`), and `consumer-added` / `consumer-removed` (`:420`, `:626-632`). The viewer
  count is only logged.
- `tools/encoder_policy.py` sets keyframe interval and preset on whichever encoder the sink creates.
- A signalling server is started in the background in the same container (`run.sh:218-221`).

### 2.2 `webrtcsink` 0.15.4 behaviour that matters (read from its source)

| Behaviour | Where |
|---|---|
| Defaults: `min-bitrate` 1000 bit/s, `start-bitrate` 2,048,000, `max-bitrate` 8,192,000 | `webrtcsink/imp.rs:66-80` |
| Defaults: STUN server `stun://stun.l.google.com:19302`; FEC on (applied above 2 Mbit/s); retransmission on | `webrtcsink/imp.rs:65-97` |
| Low bitrate also shrinks the picture: under 2 Mbit/s → 720p, under 1 Mbit/s → 360p, under 500 kbit/s → half frame rate | `webrtcsink/imp.rs:1102-1135` |
| Congestion control: loss above 10% cuts the target by up to half every 200 ms; recovery is +5% per 200 ms on low loss and at most 8% per second on delay | `gst-plugin-rtp` `gcc/imp.rs:36-38`, `:89-92`, `:889`, `:998-1047` |
| **One encoder per viewer session** | `webrtcsink/imp.rs:309-318` (`SessionInner.encoders`) |
| A session is removed only when its connection state or ICE state becomes `Failed`, or when signalling reports it ended | `webrtcsink/imp.rs:3278-3345`, `:2579-2586` |
| A keyframe is requested upstream when a session's ICE completes | `webrtcsink/imp.rs:3355-3378` |
| **Already-encoded input gets no per-session encoder**, only a parser and payloader | `webrtcsink/imp.rs:983-987`, `:1003-1027` |
| The constrained-baseline pin applies only when the sink itself encodes | `webrtcsink/imp.rs:1042` (`force_profile = … && needs_encoding`) |
| H.265 is a supported codec and `nvv4l2h265enc` is handled explicitly | `utils.rs:768`, `:795`; `webrtcsink/imp.rs:820`, `:1178-1179` |
| An application can list sessions and end one | `get-sessions` action signal, `webrtcsink/imp.rs:5615`; signaller signals `session-ended` and `end-session`, `signaller/iface.rs:86`, `:277` |
| Signalling server: pings only after 30 s of idle and ignores the reply; a peer is dropped only on a WebSocket close or error | `gst-plugin-webrtc-signalling` `server/mod.rs:148-170`, `:203-222` |

### 2.3 Why recovery after a dropout is slow — hypotheses, not yet confirmed on a live link

1. **The bitrate floor is 1 kbit/s.** Heavy loss around a dropout can drive the target down fast, and it
   climbs back slowly. While it is low the picture is also downscaled and the frame rate halved.
2. **A fresh session starts at 2 Mbit/s.** If the link is weaker than that just after it returns, the new
   session overshoots and collapses again.
3. **The viewer waits before reconnecting.** A comment at `tools/encoder_policy.py:32-33` says the dashboard's
   stall watchdog tears a frozen session down after 12 s. The dashboard is not in this repository and was not
   reviewed.

### 2.4 Why encoder sessions pile up after a reconnect — verified

- Each session has its own encoder (table above).
- **On the Ubuntu 24.04 based bridge image, ICE never declares an established connection failed.** Checked
  at runtime with the same GStreamer 1.24.2 and libnice 0.1.21 packages: the ICE agent reports
  `consent-freshness = False` and `keepalive-conncheck = False`. GStreamer only enables consent freshness
  when built against a libnice that carries a specific fix (a build-time flag; links in section 6). The
  Ubuntu 26.04 based images (`jp6m`, `dev`) were not checked.
- The signalling server has no reply deadline, so a viewer that vanishes is removed only when TCP errors:
  either the viewer's machine resets the old connection once the link returns, or the kernel gives up
  retransmitting (about fifteen minutes by default).

So each reconnect adds a session and the old one lingers. A lingering session costs an encoder (today) and
keeps transmitting its own copy over the radio (always).

To check any image, run inside the bridge container:

```
python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None); print(Gst.ElementFactory.make('webrtcbin').get_property('ice-agent').get_property('agent').get_property('consent-freshness'))"
```

To watch it happen: the bridge logs `consumers=N` every `CAM_WEBRTC_STATUS` seconds (default 10).

### 2.5 Settings that exist today (sensor YAML, `webrtc-bridge` params)

| Setting | Default | Note |
|---|---|---|
| `CAM_WEBRTC_MIN_BITRATE` | 1000 | Raise to the lowest bitrate still worth watching, e.g. 500000 |
| `CAM_WEBRTC_START_BITRATE` | 2048000 | Set to what the link reliably carries |
| `CAM_WEBRTC_MAX_BITRATE` | 8192000 | |
| `CAM_WEBRTC_CONGESTION` | `gcc` | `homegrown` or `disabled`; `disabled` is a fixed bitrate |
| `CAM_WEBRTC_KEYFRAME_S` | 2.0 | Lower gets a picture sooner after loss, at some bitrate cost |
| `CAM_WEBRTC_MAX_SIZE` | off | Encode-side downscale; the main CPU lever for large sensors |
| `VIDEO_CAPS` | unset | e.g. `video/x-h264` or `video/x-h265` to pin the codec |
| `GST_PLUGIN_FEATURE_RANK` | unset | e.g. `nvv4l2h264enc:MAX` to prefer the Jetson encoder |
| `GST_DEBUG` | unset | e.g. `3,webrtcsink:6` |

Not settable without editing `run.sh`: the STUN server, TURN, FEC, retransmission, the automatic
downscaling.

### 2.6 Work items

#### W1 — Tune and observe (no code)

Set the bitrate floor and start bitrate, then record a real dropout with `GST_DEBUG: "3,webrtcsink:6"` and
the status line. The aim is to learn which of the three causes in 2.3 dominates before changing code.

#### W2 — Reap stale sessions (small; do first)

In `bridge_stream.py`, which already tracks viewers:

- **Cap policy first.** When a new viewer makes the count exceed a configured maximum, end the oldest
  session. Simple, and right for one or two operators.
- **Liveness policy second.** End a session with no receiver feedback for about 10 s. The sink exposes
  per-session statistics through its `stats` property (`webrtcsink/imp.rs:1446-1460`); which field to key on
  has to be confirmed on a running bridge.
- To end a session: emit `end-session` on the signaller (tells the server and the viewer) and
  `session-ended` (makes the sink remove the session and tear down its encoder). The source confirms the
  second; neither was run.
- Keep the policy in a small pure module so it can be unit-tested without GStreamer, as `encoder_policy.py`
  is. Add a scenario to `plugins/webrtc-bridge/tools/webrtc_test.sh`: two consumers, one killed without a
  clean close, then assert the session count falls.

#### W3 — Encode once for all viewers (opt-in)

Put one encoder and a parser in front of the sink, so every viewer shares one encode:

`… ! videoconvert ! I420 ! [overlay] ! <encoder> ! h264parse (or h265parse) ! webrtcsink`

What changes:

- The bridge must build the encoder chain itself. On a Jetson that is `nvvidconv`, NVMM memory and
  `nvv4l2h264enc` (or `nvv4l2h265enc`); otherwise `x264enc tune=zerolatency`. Today the sink does this
  selection. Confirm property names with `gst-inspect-1.0` on the device.
- Bitrate, keyframe interval and B-frames-off move out of the `encoder-setup` hook, which no longer fires,
  into the bridge's own encoder configuration. A new bitrate setting is needed.
- `CAM_WEBRTC_MIN/MAX/START_BITRATE` and the automatic downscaling stop having any effect. The stream runs at
  one fixed bitrate with no back-off when the radio degrades. Document that next to the switch.
- The H.264 level pin in `_on_request_encoded_filter` (`bridge_stream.py:781-799`) was written for the
  sink-encodes case. Check what profile and level the offer advertises when the stream arrives pre-encoded.
- A viewer joining, or reporting loss, should trigger a keyframe from the shared encoder for everyone.
  Verify that it does, and that it is tolerable with the expected number of viewers.
- Side benefit: the constrained-baseline pin no longer applies, so High profile or H.265 becomes possible.
- Add a config switch, default off. Validate on the Orin before recommending it.

#### W4 — Expose more sink properties

At least the STUN server (so it can be emptied on a closed network), TURN, FEC, retransmission and the
downscaling modes. Small change in `run.sh` and the compose environment.

#### W5 — H.265 for the stream

The plumbing mostly exists: `VIDEO_CAPS: video/x-h265` plus `GST_PLUGIN_FEATURE_RANK: nvv4l2h265enc:MAX`,
and `encoder_policy.py:51` already covers the Jetson H.265 encoder. It has never been run in this repo:
there is no scenario in `webrtc_test.sh`, no documentation, and no low-latency settings for a software
H.265 fallback. It will not shorten reconnects. It helps only if link capacity is the limit, and only if the
operators' browsers can decode it: Chrome 136+ and Safari do; Edge is unresolved; Firefox is unknown.
Whether Chrome needs a hardware decoder on the viewing machine was not confirmed.

#### W6 — Stop working when nobody is watching

With zero viewers the bridge still debayers, scales and converts every frame. PR #65 has an idle throttle
and a frame-rate cap, unmerged and needing rework (BR-9 to BR-11 in the review).

#### Later — a relay on the ground side

Each viewer receives its own copy over the radio in every design above. A relay on the wired network (one
session over the radio, fanned out locally) would cut that to one copy and leave the vehicle with a single
session. It is outside this repository.

### 2.7 Known bridge defects to keep in mind while working here

From `docs/review-v1.13.0/05-plugins-and-image-builds.md`: PB-10 (the discovery descriptor often advertises
default geometry on unixfd), PB-11 (memory grows without bound when `CAM_WEBRTC_STATUS=0`), PB-13
(signalling is unauthenticated on all interfaces), and the unsupervised signalling server (if it dies the
bridge keeps running but nobody can connect).

---

## 3. Suggested order

1. W1 on a real link, step 1 of section 1.5 on the Orin, and the per-thread CPU split of the Bayer hardware
   path (section 1.6, item 2). All three are measurements and decide the rest.
2. W2 (reaper), then R1 (uncompressed) — small, independent, low risk.
3. R2 (byte split) if the Orin numbers justify it.
4. W3 (encode once, opt-in), with W4 alongside since both touch `run.sh`.
5. W5 and W6 as wanted.

## 4. Open questions for the owner

- Which thermal camera, and what range of counts does it actually use? That decides whether R3 is ever
  worth considering.
- How much disk per vehicle can thermal recording use? That decides R1 versus R2.
- Which image variants carry thermal cameras and WebRTC viewers: `jp6`, `jp7`, `jp6m`?
- How many simultaneous viewers per camera, typically and at most? That sets the cap for W2.
- Where does the dashboard code live? Its reconnect logic is half of the reconnect delay.

## 5. Scripts

Run from the repository root. Both need GStreamer with PyGObject and numpy, which the `cam-core` and
`cam-dev` images have.

```
# Reversibility, size and CPU of the byte split through the service's own recorder and decode path
python3 docs/encoding-optimization/scripts/gray16_split_roundtrip.py                       # software stand-in
python3 docs/encoding-optimization/scripts/gray16_split_roundtrip.py --encoder hw-hevc-lossless   # on a Jetson

# CPU per frame: FFV1, FFV1 speed knobs, uncompressed, split (CPU side), split (hardware, Jetson only)
python3 docs/encoding-optimization/scripts/recording_cpu_bench.py

# Either script on real footage: headerless little-endian 16-bit frames
python3 … --raw-file thermal.gray16 --width 640 --height 512

# CPU-side steps of the 8-bit Bayer hardware path; on a Jetson also the full fragment, with and without tiling
python3 docs/encoding-optimization/scripts/bayer_hw_path_cpu.py
```

On a host without the Jetson elements the first script refuses `--encoder hw-hevc-lossless` with a clear
message and the other two skip their hardware rows; all three were run that way on 2026-11-11.

## 6. External sources

- [Jetson Linux API Reference r39.2 — video encode sample](https://docs.nvidia.com/jetson/archives/r39.2/ApiReference/l4t_mm_01_video_encode.html) (encoder input formats, the 10-bit alignment note)
- [Jetson Linux Developer Guide r39.2 — Accelerated GStreamer](https://docs.nvidia.com/jetson/archives/r39.2/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html) (per-platform encode examples)
- [GStreamer: put usage of OPTION_CONSENT_FRESHNESS behind check](https://git.fasttube.de/FaSTTUBe/GST-Tensordecoder-ov_ep/commit/211928ca41d69ee7052b8a886a3156fae63f0d71) and [the libnice 0.1.22 requirement backport to 1.24](https://gitlab.freedesktop.org/gstreamer/gstreamer/-/merge_requests/6431)
- [libnice 0.1.19 release notes](https://lists.freedesktop.org/archives/nice/2022-May/001511.html) (consent freshness)
- [Chromium: Intent to Ship H.265 in WebRTC](https://groups.google.com/a/chromium.org/g/blink-dev/c/3h8lL8a377c), [IETF draft: H.265 Profile for WebRTC](https://datatracker.ietf.org/doc/draft-ietf-avtcore-hevc-webrtc), [Microsoft Q&A on Edge](https://learn.microsoft.com/en-ca/answers/questions/5880331/h-265-(hevc)-not-published-sent-via-webrtc-chrome)
