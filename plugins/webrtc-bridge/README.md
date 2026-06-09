# webrtc-bridge

Remote-viewing consumer. Reads the core's frames and serves the video to remote browsers over
**WebRTC** using [`webrtcsink`](https://gstreamer.freedesktop.org/documentation/rswebrtc/webrtcsink.html)
from gst-plugins-rs. This is the **lossy, low-latency** egress path — distinct from the core's
lossless recording. `webrtcsink` does the encoding, congestion control (GCC), FEC/RTX, and
multi-viewer fan-out itself, so the pipeline just feeds it color frames.

## Transport (mirrors the ros2 bridge)

The transport is selected by `CAM_PLATFORM` (cam-up exports it), so the bridge matches whatever
the core publishes — exactly like the ros2 bridge picking CamUnixfdBridge vs CamHeaderBridge:

```
JP7  core (unixfd  /tmp/cam/unixfd) ─► unixfdsrc ──► bayer2rgb ─► videoconvert ! I420 ─► webrtcsink ─► viewers
       self-describing caps (geometry + Bayer format from the stream)        ▲                  ▲
JP6  core (raw shm /tmp/cam/raw)    ─► shmsrc do-timestamp ! video/x-bayer ─┘   gst-webrtc-signalling-server (:8443)
       caps from config (CAM_WIDTH/HEIGHT/FORMAT + CAM_BAYER)
```

- **JP7 → unixfd.** Rides the core's `plugin_endpoint` (`/tmp/cam/unixfd`) — the **same socket the
  ros2 bridge uses**; `unixfdsink` broadcasts to every connected client, so both consume it at full
  rate. Caps are self-describing: geometry **and** the Bayer pattern come from the stream, so no
  `CAM_*` geometry is needed and no separate endpoint has to be enabled.
- **JP6 → raw shm.** Reads the headless `raw_endpoint` (`/tmp/cam/raw`). Raw shm carries no caps, so
  geometry comes from the sensor config and, for a CFA camera, the Bayer pattern (`CAM_BAYER`) is
  applied as `video/x-bayer` caps. **The core must enable it** (`transport.raw_endpoint.enabled: true`).

## Color (debayer)

For a **Bayer** camera (`CAM_BAYER` set — sensor_env derives it from the camera `pixel_format`) the
bridge debayers to color **in-pipeline** with `bayer2rgb`, so the browser sees RGB, not a grayscale
mosaic. `bayer2rgb` reads the pattern from the input caps (unixfd carries it; the JP6 capsfilter sets
it). Set `CAM_WEBRTC_DEBAYER=false` to preview the raw mosaic instead. **Mono** cameras pass straight
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
docker run --rm -v cam_sock:/tmp/cam --network host \
  -e CAM_PLATFORM=jp7 -e CAM_BAYER=rggb webrtc-bridge
# JP6 (raw shm — geometry must match the camera):
docker run --rm --ipc=host -v cam_sock:/tmp/cam --network host \
  -e CAM_PLATFORM=jp6 -e CAM_BAYER=rggb \
  -e CAM_WIDTH=2448 -e CAM_HEIGHT=2048 -e CAM_FPS=24 webrtc-bridge
```

Or via the per-sensor stack: `cam-up <sensor>.yaml up -d webrtc-bridge` (cam-up exports
`CAM_PLATFORM` + sensor_env derives `CAM_BAYER`/geometry from the config).

| Env | Default | Meaning |
|---|---|---|
| `CAM_PLATFORM` | `jp6` | `jp7` → unixfd, else raw shm (cam-up sets it per host) |
| `CAM_TRANSPORT` | _(auto)_ | override the platform default: `unixfd` \| `shm` |
| `CAM_TRANSPORT_SOCKET` | `/tmp/cam/unixfd` | unixfd socket (JP7; the core's plugin_endpoint) |
| `CAM_SHM_SOCKET` | `/tmp/cam/raw` | raw shm socket (JP6) |
| `CAM_BAYER` | _(empty)_ | Bayer pattern (`rggb`/`grbg`/`gbrg`/`bggr`) → debayer to color; empty → mono |
| `CAM_WEBRTC_DEBAYER` | `auto` | `false` to preview the raw mosaic instead of debayering |
| `CAM_WIDTH` / `CAM_HEIGHT` | `512` | **JP6 raw shm only** — must match the camera geometry |
| `CAM_FORMAT` | `GRAY8` | **JP6 raw shm only** — mono raw format when not debayering |
| `CAM_FPS` | `25` | **JP6 raw shm only** — frame rate |
| `VIDEO_CAPS` | _(unset)_ | e.g. `video/x-h264` to pin the codec; unset → webrtcsink picks |
| `CAM_WEBRTC_PROFILE` | `constrained-baseline` | H.264 profile: `constrained-baseline` \| `high`. `high` is set on the encoder (needs a HW encoder, e.g. `nvv4l2h264enc`); software `x264enc` keeps its native constrained-baseline |
| `CAM_WEBRTC_MAX_LEVEL` | `5.2` | safety clamp on the **auto-derived** H.264 level (the level is computed from the streamed resolution+fps — never fixed) |
| `SIGNALLING_PORT` | `8443` | signalling server port |
| `RUN_SIGNALLING` | `1` | run the bundled signalling server in-container |

## Fleet discovery (Zenoh)

Once the pipeline is streaming, the bridge advertises this stream over Zenoh so an operator dashboard
can find it — presence + a descriptor — following the system-wide convention in
[docs/DISCOVERY.md](../../docs/DISCOVERY.md). It advertises at:

```
fleet/<VEHICLE_ID>/media/<CAM_INSTANCE>
```

a **liveliness token** (presence; appears on `PLAYING`, auto-withdrawn when this process dies — no
heartbeat) and a **queryable** that replies the JSON descriptor (`id`, `role`, `producer`, `protocol`,
`signalling`, `producer_id`, and best-effort `codec`/`width`/`height`/`fps`/`pixel_format`, plus optional
`ros_topic`/`recording` links). `producer_id` is also set as `webrtcsink`'s `meta.name`, so a shared
signalling server's producers line up with discovery.

The advertiser runs **inside this bridge process** (`tools/bridge_stream.py`, which now owns the
pipeline) so the token's lifetime equals the stream's. It's **additive + best-effort**: if Zenoh is
unreachable it logs and keeps streaming; `CAM_ADVERTISE=0` (or `CAM_LAUNCHER=gst-launch`) turns it off
entirely. Generic half: [`tools/zenoh_advertiser.py`](tools/zenoh_advertiser.py) (no webrtc knowledge).

| Env | Default | Meaning |
|---|---|---|
| `CAM_ADVERTISE` | `1` | advertise over Zenoh; `0` disables (video unaffected) |
| `CAM_LAUNCHER` | `python` | `gst-launch` = legacy bare pipeline, no discovery |
| `VEHICLE_ID` | _(hostname)_ | `<vehicle_id>` key segment |
| `CAM_INSTANCE` | `camera` | `<sensor_id>` key segment (sensor_env sets it from the config name) |
| `ZENOH_CONNECT` | `tcp/localhost:7447` | the vehicle's local zenohd; set **empty** to scout |
| `CAM_PRODUCER_ID` | _(`<vehicle>-<sensor>`)_ | descriptor `producer_id` == `webrtcsink` `meta.name` |
| `CAM_STREAM_ROLE` | _(= sensor id)_ | human `role` label |
| `CAM_SIGNALLING_URL` | _(`ws://<host>:<port>`)_ | advertised signalling URL; or set `CAM_SIGNALLING_HOST`/`_SCHEME` |
| `CAM_SIGNALLING_PROTOCOL` | `gstwebrtc-api` | descriptor `protocol` |
| `CAM_ROS_TOPIC` / `CAM_RECORDING_GLOB` | _(unset)_ | optional descriptor cross-links (omitted if unset) |

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

Discovery has its own test (needs a Linux host for host networking + a Zenoh router):

```bash
./plugins/webrtc-bridge/tools/discovery_test.sh
```

Brings up a `rmw_zenohd` router + core + bridge, and a Zenoh probe
([`discovery_probe.py`](tools/discovery_probe.py)) asserts: a liveliness **PUT** at
`fleet/<vehicle>/media/<sensor>` once streaming, a valid JSON **descriptor** from `get(<key>)`, and a
**DELETE** when the bridge stops.

## Jetson notes

- **HW encoder:** `webrtcsink` discovers encoders; to force NVENC set
  `GST_PLUGIN_FEATURE_RANK=nvv4l2h264enc:MAX` + `VIDEO_CAPS=video/x-h264`. Some Orin variants (e.g.
  Orin Nano) have no H.264 HW encoder — it falls back to `x264enc`. Verify with
  `gst-inspect-1.0 nvv4l2h264enc`.
- **H.264 level (auto) & profile:** the SDP `profile-level-id` must match the encoded stream or browsers
  receive RTP but decode nothing (a black tile). The bridge derives the **minimum** H.264 level for the
  resolution+fps fed to `webrtcsink` and pins it on the encoder output, so the advertised level tracks the
  stream and any resolution decodes — no fixed level. `CAM_WEBRTC_PROFILE` selects the profile
  (`constrained-baseline` default; `high` is ~15–25% smaller but is set on the encoder, so it needs a HW
  encoder — sw `x264enc` keeps constrained-baseline). `CAM_WEBRTC_MAX_LEVEL` (default `5.2`) clamps the
  auto level. B-frames are forced off for live either way. (Applied by the Python launcher via
  `webrtcsink`'s `encoder-setup` / `request-encoded-filter` signals; the `CAM_LAUNCHER=gst-launch` hatch
  keeps `webrtcsink`'s fixed defaults.)
- **Adaptive bitrate / quality ceiling:** `webrtcsink` runs Google Congestion Control (`gcc`) and scales
  the encoder bitrate to each viewer's link automatically — nothing to enable. The bounds are optional
  env knobs (bit/sec): `CAM_WEBRTC_MIN_BITRATE` / `CAM_WEBRTC_MAX_BITRATE` / `CAM_WEBRTC_START_BITRATE`,
  plus `CAM_WEBRTC_CONGESTION` (`gcc`|`homegrown`|`disabled`). The element default `max-bitrate` is 8 Mbps,
  which caps quality on a fast link at high res — raise `CAM_WEBRTC_MAX_BITRATE` (e.g. `20000000`) for 4K.
- **5MP color is CPU-heavy.** `bayer2rgb` + `videoconvert` + (software) encode at 2448×2048 is a load;
  the `leaky=downstream` queue drops to the newest frame under pressure (correct for a live preview).
  Add a `videoscale` before the encoder, or force NVENC (above), for a lighter stream.
- **Build on-device:** gst-plugins-rs builds against the Jetson's GStreamer (≥ the 1.20 floor); arm64
  builds are RAM-bound — the Dockerfile already uses LTO-off + limited jobs.

## Known limitations / future

- **JP6 geometry must be configured** (raw shm has no caps). JP7/unixfd is self-describing, so this
  only applies to the JP6 path. A future JP6 option could consume the **header endpoint**
  (`application/x-cam-frame`) and parse the 36-byte header for self-describing geometry there too.
- **Capture PTP timestamp.** unixfd carries the core's buffer fields (capture-ns / frame-id); a future
  version could thread them through `webrtcsink do-clock-signalling=true` + the `ntp-64` RTP header
  extension so a `webrtcsrc` consumer recovers absolute capture time (`GstReferenceTimestampMeta`).
  Browsers can't recover absolute capture time via standard JS regardless.
