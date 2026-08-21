# rtsp-bridge

Serves a sensor's frames as a **standard RTSP stream** — `rtsp://<host>:<port>/<name>` — that any
plain client plays: ffplay/VLC, OpenCV `VideoCapture`, QGroundControl, an NVR/recorder, another
GStreamer pipeline. No WebRTC signalling, no browser stack; the trade is **no congestion control**
(see Bitrate below).

```
ffplay -rtsp_transport tcp rtsp://vehicle:8554/cam_a
gst-launch-1.0 playbin uri=rtsp://vehicle:8554/cam_a
```

The server is [tools/rtsp_server.py](tools/rtsp_server.py) (GstRtspServer in-process), launched by
[run.sh](run.sh) which resolves the transport and builds the source chain.

## Transport (must match the core)

Selected by `CAM_PLATFORM` (exported by cam-up), `CAM_TRANSPORT` overrides — the same pair the
ros2-bridge consumes, and unlike the webrtc-bridge's raw-shm fallback:

| Platform | Source | Socket | Caps / metadata |
|---|---|---|---|
| JP7 (gst ≥ 1.24) | `unixfdsrc` | `/tmp/cam/unixfd` | self-describing (`video/x-raw` GRAY8/16, `video/x-bayer,<pattern>`); PTS intact. `unixfdsink` broadcasts — shares the socket with the other bridges. |
| JP6 (gst 1.20) | `shmsrc` | `/tmp/cam/frames` | `application/x-cam-frame`: a 36-byte header/frame carries geometry, pixfmt, frame_id and the **PTP capture time** — the frame pump strips it, re-derives caps, and restamps PTS from it. |

Needs `transport.plugin_endpoint.enabled` (the default) — **not** the raw endpoint. The endpoint
honors the core's `transport.plugin_endpoint.max_rate_hz` cap. A best-effort consumer throughout
(leaky depth-2 queues, `drop=true`): the core's recording branch is never stalled by this bridge.

### The frame pump

[tools/frame_pump.py](tools/frame_pump.py) is an appsink→appsrc bridge inside the media pipeline,
wired per-media at `media-configure`. It runs on JP6 always (header strip + PTP restamp; PTS falls
back **permanently** to arrival time on the first non-monotonic capture timestamp), and on either
platform for the 16-bit normalize. Plain JP7 streams skip it entirely (linear chain). `async=false`
on the pump appsink is load-bearing — see the preroll-deadlock note in run.sh.

## Lifecycle: one shared, on-demand media

`GstRTSPMediaFactory` with `shared=true`: **one** pipeline per mount regardless of client count
(the endpoint is read once, encoded once), constructed on the first client's DESCRIBE and torn down
after the last one leaves — **zero encode cost while nobody watches**; the first client pays ~1 s
of preroll. Core restart self-heals: the dying source unprepares the media and drops the clients;
the next client's DESCRIBE builds fresh media that reconnects. The server process itself stays up.

## Codec / encoder

`CAM_RTSP_CODEC` = `h264` (default; universal decoders) | `h265` (better compression at a bitrate,
spottier client support). Encoder auto-probe per codec: `nvv4l2h26xenc` (HW NVENC) when the
platform grant injected it — CDI device on JP7 ([docker-compose.jp7.yml](../../docker-compose.jp7.yml)),
nvidia-runtime CSV mounts on JP6 (same *unvalidated-on-JP6-hardware* caveat as the webrtc-bridge) —
else CPU `x264enc`/`x265enc` (`tune=zerolatency`, B-frames off). Pin with `CAM_RTSP_ENCODER`.
The payloader re-sends SPS/PPS(+VPS) with every IDR (`config-interval=-1`) and the SDP is derived
from the actual bitstream, so mid-stream joiners and profile/level mismatches are non-issues here.

## Bitrate — no congestion control

RTSP/RTP has no equivalent of webrtcsink's per-viewer gcc: this is a **fixed-bitrate** stream.
`CAM_RTSP_BITRATE` (bit/s, default 4 Mb/s) must be sized to the link — a rate above a (WiFi) link's
momentary capacity bufferbloats (latency grows without recovering). If viewers are on flaky links,
prefer the webrtc-bridge; this bridge shines for LAN/wired consumers, NVRs, and tooling.

## 16-bit thermal (`CAM_RTSP_NORMALIZE`)

`off` (default) | `auto` | `"lo:hi"` percentiles. GRAY16 → percentile-stretched GRAY8 before encode
(EMA-smoothed window; [tools/thermal_preview.py](tools/thermal_preview.py), lifted from the
webrtc-bridge). Without it, videoconvert keeps the top byte — near-black for LSB-aligned
radiometric cameras. Serve-only: recording/ROS keep the raw 16-bit.

## Discovery

On server **bind** (a server is connectable from bind time), advertises over Zenoh at
`fleet/<VEHICLE_ID>/media/<id>` per [docs/DISCOVERY.md](../../docs/DISCOVERY.md): `protocol:
"rtsp"`, the `rtsp://` URL in `signalling`, `codec` known (we encode it). `id` defaults to the
sensor name; when the webrtc-bridge shares the sensor, sensor_env auto-suffixes it `<name>-rtsp` so
the two streams keep distinct keys. Best-effort: `CAM_ADVERTISE=0` disables; a late zenoh router is
retried every 5 s.

## Knobs

All settable from the sensor YAML — friendly params (`port`, `path`, `bitrate`, `codec`, geometry,
`role`) or any `CAM_RTSP_*` UPPERCASE passthrough:

| Env | Default | Meaning |
|---|---|---|
| `CAM_RTSP_PORT` | `8554` | RTSP port (host networking → unique per camera; rig checks clashes) |
| `CAM_RTSP_PATH` | `/<CAM_INSTANCE>` | mount path |
| `CAM_RTSP_CODEC` | `h264` | `h264` \| `h265` |
| `CAM_RTSP_BITRATE` | `4000000` | bit/s, fixed (see above) |
| `CAM_RTSP_KEYINT` | `30` | IDR interval in frames (joiners sync at IDRs) |
| `CAM_RTSP_PROTOCOLS` | server default (udp+tcp) | comma list of `udp`,`udp-mcast`,`tcp` |
| `CAM_RTSP_ENCODER` | `auto` | `auto` = NVENC else CPU; or pin an element name |
| `CAM_RTSP_LATENCY` | `200` | server rtpbin latency (ms) |
| `CAM_RTSP_DEBAYER` | `auto` | debayer a CFA camera to color; `false` serves the raw mosaic |
| `CAM_RTSP_NORMALIZE` | `off` | 16→8 stretch: `auto` or `"lo:hi"` percentiles |
| `CAM_RTSP_STREAM_ID` | sensor name | discovery id (auto `<name>-rtsp` when webrtc coexists) |
| `CAM_RTSP_HOST` / `CAM_RTSP_URL` | hostname / derived | advertised URL host / full override |

## Test

```
bash plugins/rtsp-bridge/tools/rtsp_test.sh
```

Headless containerized round-trips (no Jetson, no camera): JP6 header+mono (plus an on-demand
reconnect pass), H.265, JP7 unixfd+Bayer (auto-skipped on a gst-1.20 core image), and GRAY16
thermal with normalize. Consumer: [tools/rtsp_consumer.py](tools/rtsp_consumer.py)
(`rtspsrc → decodebin → appsink` frame counter). Pure-logic unit tests:
`python3 tools/test_rtsp_launch.py` and `python3 tools/test_frame_pump.py` (no docker, no gi).

Dev viewing (`cam-up --dev`, bridge network): the dev overlay maps the TCP port only — play with
`ffplay -rtsp_transport tcp rtsp://localhost:8554/<name>`.
