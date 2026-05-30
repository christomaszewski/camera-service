# webrtc-bridge

Remote-viewing consumer. Reads the core's **raw shm endpoint** and serves the video to
remote browsers over **WebRTC** using [`webrtcsink`](https://gstreamer.freedesktop.org/documentation/rswebrtc/webrtcsink.html)
from gst-plugins-rs. This is the **lossy, low-latency** egress path — distinct from the
core's lossless recording. `webrtcsink` does the encoding, congestion control (GCC),
FEC/RTX, and multi-viewer fan-out itself, so the pipeline just feeds it raw frames:

```
core (raw shm /tmp/gige/raw)  ──►  shmsrc do-timestamp ──► videoconvert ! I420 ──► webrtcsink ──► viewers
                                                                                      ▲
                                                          gst-webrtc-signalling-server (:8443)
```

## Why a sibling container (not in-image)

`webrtcsink` isn't packaged for Debian/Ubuntu — it's the Rust `gst-plugin-webrtc`, built
from source (`cargo cinstall`). That toolchain doesn't belong in the core image, so the
bridge is a sibling container that shares the shm transport (`ipc: host` + the socket
volume), exactly like the ros2-bridge.

## Build & run

```bash
docker build -f plugins/webrtc-bridge/Dockerfile -t webrtc-bridge .       # ~15-25 min (Rust)
# then, sharing the core's shm:
docker run --rm --ipc=host -v gige_sock:/tmp/gige --network host \
  -e GIGE_SHM_SOCKET=/tmp/gige/raw -e GIGE_WIDTH=2048 -e GIGE_HEIGHT=1536 \
  -e GIGE_FORMAT=GRAY8 -e GIGE_FPS=30 webrtc-bridge
```

Or via the per-sensor stack: `docker compose up webrtc-bridge` (see [docker-compose.yml](../../docker-compose.yml)).

**The core must enable the raw endpoint** (`transport.raw_endpoint.enabled: true`), and the
`GIGE_*` env must match its caps — shm carries no caps, so the consumer states them explicitly.

| Env | Default | Meaning |
|---|---|---|
| `GIGE_SHM_SOCKET` | `/tmp/gige/raw` | the core's raw endpoint socket |
| `GIGE_WIDTH` / `GIGE_HEIGHT` | `512` | must match the camera geometry |
| `GIGE_FORMAT` | `GRAY8` | raw format (`GRAY8` mono, `I420`, ...) |
| `GIGE_FPS` | `25` | frame rate |
| `VIDEO_CAPS` | _(unset)_ | e.g. `video/x-h264` to pin the codec; unset → webrtcsink picks |
| `SIGNALLING_PORT` | `8443` | signalling server port |
| `RUN_SIGNALLING` | `1` | run the bundled signalling server in-container |

## Viewing

A viewer connects to the signalling server (`ws://<host>:8443`) and gets the stream. For a
browser, use the gst-plugins-rs [`gstwebrtc-api`](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/tree/main/net/webrtc/gstwebrtc-api)
JS client / demo page pointed at that server. (This 0.13.x build has no embedded web server;
newer webrtcsink has `run-web-server` — verify with `gst-inspect-1.0 webrtcsink`.)

## Test (no Jetson, no camera, no browser)

```bash
./plugins/webrtc-bridge/tools/webrtc_test.sh
```

Runs the full loopback: core fake camera → raw shm → this bridge (`webrtcsink`) →
[`webrtc_consumer.py`](tools/webrtc_consumer.py) (`webrtcsrc` → decode → counts frames).
Proves the whole egress path without a browser. PASS = it decoded ≥30 frames.

## Jetson notes

- **HW encoder:** `webrtcsink` discovers encoders; to force NVENC set
  `GST_PLUGIN_FEATURE_RANK=nvv4l2h264enc:MAX` + `VIDEO_CAPS=video/x-h264`. Some Orin variants
  (e.g. Orin Nano) have no H.264 HW encoder — it falls back to `x264enc`. Verify with
  `gst-inspect-1.0 nvv4l2h264enc`.
- **Build on-device:** gst-plugins-rs builds against the Jetson's GStreamer 1.20 (≥ the 1.20
  floor); arm64 builds are RAM-bound — the Dockerfile already uses LTO-off + limited jobs.

## Known limitations / future

- **Geometry must be configured** (shm has no caps). A future version could instead consume
  the **header endpoint** (`application/x-gige-frame`), parse the 36-byte header for
  self-describing geometry, and — via `webrtcsink do-clock-signalling=true` + the `ntp-64`
  RTP header extension — carry the **capture PTP timestamp** through to a `webrtcsrc` consumer
  (`GstReferenceTimestampMeta`). That unlocks timestamp-accurate remote consumers; browsers
  can't recover absolute capture time via standard JS regardless.
- A mono (`GRAY8`) camera is shown as grayscale (chroma neutralized by the I420 conversion).
