# webrtc-bridge

Remote-viewing consumer. Reads the core's frames and serves the video to remote browsers over
**WebRTC** using [`webrtcsink`](https://gstreamer.freedesktop.org/documentation/rswebrtc/webrtcsink.html)
from gst-plugins-rs. This is the **lossy, low-latency** egress path — distinct from the core's
lossless recording. `webrtcsink` does the encoding, congestion control (GCC), FEC/RTX, and
multi-viewer fan-out itself, so the pipeline just feeds it color frames.

## Transport (mirrors the ros2 bridge)

The transport is selected by `GIGE_PLATFORM` (gige-up exports it), so the bridge matches whatever
the core publishes — exactly like the ros2 bridge picking GigeUnixfdBridge vs GigeHeaderBridge:

```
JP7  core (unixfd  /tmp/gige/unixfd) ─► unixfdsrc ──► bayer2rgb ─► videoconvert ! I420 ─► webrtcsink ─► viewers
       self-describing caps (geometry + Bayer format from the stream)        ▲                  ▲
JP6  core (raw shm /tmp/gige/raw)    ─► shmsrc do-timestamp ! video/x-bayer ─┘   gst-webrtc-signalling-server (:8443)
       caps from config (GIGE_WIDTH/HEIGHT/FORMAT + GIGE_BAYER)
```

- **JP7 → unixfd.** Rides the core's `plugin_endpoint` (`/tmp/gige/unixfd`) — the **same socket the
  ros2 bridge uses**; `unixfdsink` broadcasts to every connected client, so both consume it at full
  rate. Caps are self-describing: geometry **and** the Bayer pattern come from the stream, so no
  `GIGE_*` geometry is needed and no separate endpoint has to be enabled.
- **JP6 → raw shm.** Reads the headless `raw_endpoint` (`/tmp/gige/raw`). Raw shm carries no caps, so
  geometry comes from the sensor config and, for a CFA camera, the Bayer pattern (`GIGE_BAYER`) is
  applied as `video/x-bayer` caps. **The core must enable it** (`transport.raw_endpoint.enabled: true`).

## Color (debayer)

For a **Bayer** camera (`GIGE_BAYER` set — sensor_env derives it from the camera `pixel_format`) the
bridge debayers to color **in-pipeline** with `bayer2rgb`, so the browser sees RGB, not a grayscale
mosaic. `bayer2rgb` reads the pattern from the input caps (unixfd carries it; the JP6 capsfilter sets
it). Set `GIGE_WEBRTC_DEBAYER=false` to preview the raw mosaic instead. **Mono** cameras pass straight
through (the encoder reads the format off caps; chroma is neutralized by the I420 conversion).

## Why a sibling container (not in-image)

`webrtcsink` isn't packaged for Debian/Ubuntu — it's the Rust `gst-plugin-webrtc`, built from source
(`cargo cinstall`). That toolchain doesn't belong in the core image, so the bridge is a sibling
container that shares the transport (the socket volume; `ipc: host` for the JP6 raw shm data plane),
exactly like the ros2-bridge.

## Build & run

```bash
docker build -f plugins/webrtc-bridge/Dockerfile -t webrtc-bridge .       # ~15-25 min (Rust)
# JP7 (unixfd, self-describing — no geometry needed), sharing the core's transport volume:
docker run --rm -v gige_sock:/tmp/gige --network host \
  -e GIGE_PLATFORM=jp7 -e GIGE_BAYER=rggb webrtc-bridge
# JP6 (raw shm — geometry must match the camera):
docker run --rm --ipc=host -v gige_sock:/tmp/gige --network host \
  -e GIGE_PLATFORM=jp6 -e GIGE_BAYER=rggb \
  -e GIGE_WIDTH=2448 -e GIGE_HEIGHT=2048 -e GIGE_FPS=24 webrtc-bridge
```

Or via the per-sensor stack: `gige-up <sensor>.yaml up -d webrtc-bridge` (gige-up exports
`GIGE_PLATFORM` + sensor_env derives `GIGE_BAYER`/geometry from the config).

| Env | Default | Meaning |
|---|---|---|
| `GIGE_PLATFORM` | `jp6` | `jp7` → unixfd, else raw shm (gige-up sets it per host) |
| `GIGE_TRANSPORT` | _(auto)_ | override the platform default: `unixfd` \| `shm` |
| `GIGE_TRANSPORT_SOCKET` | `/tmp/gige/unixfd` | unixfd socket (JP7; the core's plugin_endpoint) |
| `GIGE_SHM_SOCKET` | `/tmp/gige/raw` | raw shm socket (JP6) |
| `GIGE_BAYER` | _(empty)_ | Bayer pattern (`rggb`/`grbg`/`gbrg`/`bggr`) → debayer to color; empty → mono |
| `GIGE_WEBRTC_DEBAYER` | `auto` | `false` to preview the raw mosaic instead of debayering |
| `GIGE_WIDTH` / `GIGE_HEIGHT` | `512` | **JP6 raw shm only** — must match the camera geometry |
| `GIGE_FORMAT` | `GRAY8` | **JP6 raw shm only** — mono raw format when not debayering |
| `GIGE_FPS` | `25` | **JP6 raw shm only** — frame rate |
| `VIDEO_CAPS` | _(unset)_ | e.g. `video/x-h264` to pin the codec; unset → webrtcsink picks |
| `SIGNALLING_PORT` | `8443` | signalling server port |
| `RUN_SIGNALLING` | `1` | run the bundled signalling server in-container |

## Viewing

A viewer connects to the signalling server (`ws://<host>:8443`) and gets the stream. For a browser,
use the gst-plugins-rs [`gstwebrtc-api`](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/tree/main/net/webrtc/gstwebrtc-api)
JS client / demo page pointed at that server. (This 0.13.x build has no embedded web server; newer
webrtcsink has `run-web-server` — verify with `gst-inspect-1.0 webrtcsink`.)

## Test (no Jetson, no camera, no browser)

```bash
./plugins/webrtc-bridge/tools/webrtc_test.sh
```

Runs the full loopback: core fake camera → transport → this bridge (`webrtcsink`) →
[`webrtc_consumer.py`](tools/webrtc_consumer.py) (`webrtcsrc` → decode → counts frames). Proves the
whole egress path without a browser. PASS = it decoded ≥30 frames.

## Jetson notes

- **HW encoder:** `webrtcsink` discovers encoders; to force NVENC set
  `GST_PLUGIN_FEATURE_RANK=nvv4l2h264enc:MAX` + `VIDEO_CAPS=video/x-h264`. Some Orin variants (e.g.
  Orin Nano) have no H.264 HW encoder — it falls back to `x264enc`. Verify with
  `gst-inspect-1.0 nvv4l2h264enc`.
- **5MP color is CPU-heavy.** `bayer2rgb` + `videoconvert` + (software) encode at 2448×2048 is a load;
  the `leaky=downstream` queue drops to the newest frame under pressure (correct for a live preview).
  Add a `videoscale` before the encoder, or force NVENC (above), for a lighter stream.
- **Build on-device:** gst-plugins-rs builds against the Jetson's GStreamer (≥ the 1.20 floor); arm64
  builds are RAM-bound — the Dockerfile already uses LTO-off + limited jobs.

## Known limitations / future

- **JP6 geometry must be configured** (raw shm has no caps). JP7/unixfd is self-describing, so this
  only applies to the JP6 path. A future JP6 option could consume the **header endpoint**
  (`application/x-gige-frame`) and parse the 36-byte header for self-describing geometry there too.
- **Capture PTP timestamp.** unixfd carries the core's buffer fields (capture-ns / frame-id); a future
  version could thread them through `webrtcsink do-clock-signalling=true` + the `ntp-64` RTP header
  extension so a `webrtcsrc` consumer recovers absolute capture time (`GstReferenceTimestampMeta`).
  Browsers can't recover absolute capture time via standard JS regardless.
