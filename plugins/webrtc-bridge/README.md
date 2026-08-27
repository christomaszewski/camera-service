# webrtc-bridge

Remote-viewing consumer. Reads the core's frames and serves the video to remote browsers over
**WebRTC** using [`webrtcsink`](https://gstreamer.freedesktop.org/documentation/rswebrtc/webrtcsink.html)
from gst-plugins-rs. This is the **lossy, low-latency** egress path — distinct from the core's
lossless recording. `webrtcsink` does the encoding, congestion control (GCC), FEC/RTX, and
multi-viewer fan-out itself, so the pipeline just feeds it color frames.

## Transport (mirrors the ros2 bridge)

The transport is selected by `CAM_PLATFORM` (cam-up exports it), so the bridge matches whatever
the core publishes — exactly like the ros2 bridge picking CamUnixfdBridge vs CamHeaderBridge. Both
platform defaults ride the core's **plugin endpoint**, so geometry/format self-describe and the
**absolute capture timestamp survives onto the buffers** (`offset=frame_id`,
`offset_end=capture-ns`) — which is what the latency instrumentation below reads:

```
JP7  core (unixfd  /tmp/cam/unixfd)  ─► unixfdsrc ─► [fmt_tap: runtime debayer/relabel] ─► videoconvert ! I420 ─► webrtcsink ─► viewers
       self-describing caps (geometry + Bayer format from the stream)                              ▲                     ▲
JP6  core (shm+hdr /tmp/cam/frames)  ─► shmsrc ! appsink ─[strip 36-byte header, restamp]─ appsrc ─┘   gst-webrtc-signalling-server (:8443)
       geometry/format/capture-ts from the per-frame CAMF header (python pump)
LEGACY core (raw shm /tmp/cam/raw)   ─► shmsrc do-timestamp ! video/x-bayer ! bayer2rgb ! videoconvert …   (CAM_TRANSPORT=shm-raw)
       caps from config (CAM_WIDTH/HEIGHT/FORMAT + CAM_BAYER); no capture timestamps
```

- **JP7 → unixfd.** Rides the core's `plugin_endpoint` (`/tmp/cam/unixfd`) — the **same socket the
  ros2 bridge uses**; `unixfdsink` broadcasts to every connected client, so both consume it at full
  rate. Caps are self-describing: geometry **and** the Bayer pattern come from the stream, so no
  `CAM_*` geometry is needed and no separate endpoint has to be enabled.
- **JP6 → headered shm.** Rides the **same** `plugin_endpoint` (`/tmp/cam/frames` on a gst < 1.24
  core, where each buffer is `[36-byte CAMF header][pixels]` under `application/x-cam-frame` — the
  socket the JP6 ros bridges use). The python launcher pumps `hdr_in → hdr_out`, stripping the
  header and stamping caps from it ([`header_transport.py`](tools/header_transport.py), the
  bridge-side mirror of the core's `transport.py` contract), so geometry/format come from the
  stream, not env, and the capture timestamp is re-attached exactly the way unixfd carries it.
  Needs `transport.plugin_endpoint.enabled: true` — with this, `raw_endpoint` can be **disabled**
  on the core unless something else reads it.
- **`CAM_TRANSPORT=shm-raw` → raw shm (legacy).** Reads the headless `raw_endpoint`
  (`/tmp/cam/raw`). Raw shm carries no caps, so geometry comes from the sensor config and, for a
  CFA camera, the Bayer pattern (`CAM_BAYER`) is applied as `video/x-bayer` caps. The core must
  enable it (`transport.raw_endpoint.enabled: true`). No capture timestamps survive — the latency
  instrumentation reads n/a. Kept as an escape hatch, and used automatically by
  `CAM_LAUNCHER=gst-launch` (which can't run the header pump).

## Color (debayer)

For a **Bayer** camera the bridge debayers to color **in-pipeline** with `bayer2rgb`, so the browser
sees RGB, not a grayscale mosaic; `CAM_WEBRTC_DEBAYER=false` previews the raw mosaic instead.
**Mono** cameras pass straight through (the encoder reads the format off caps; chroma is neutralized
by the I420 conversion). *How* the bridge decides differs by transport:

- **unixfd (JP7): decided at runtime from the stream itself.** The pipeline carries an inert
  `identity name=fmt_tap` seam, left unlinked at build; on the stream's first caps the launcher
  splices in `bayer2rgb` (`video/x-bayer` + debayer), a zero-copy GRAY8 relabel (`video/x-bayer` +
  `CAM_WEBRTC_DEBAYER=false` — the 8-bit mosaic is byte-identical to a GRAY8 plane), or links
  straight through (mono/raw). Env is never consulted for the format, so a config that mispredicts
  the device (wrong/unset `pixel_format`, a camera that rejected the configured format, 16-bit Bayer
  riding as GRAY16) can no longer kill the pipeline with a not-negotiated restart loop — the exact
  failure a Bayer GigE camera hit when its mosaic arrived labeled differently than `CAM_BAYER`
  predicted. (`CAM_LAUNCHER=gst-launch` has no launcher to splice, so it keeps the legacy env-driven
  `bayer2rgb` — and the legacy failure mode.)
- **headered shm (JP6): the same `fmt_tap` seam, fed by the pump.** Geometry/format come from the
  header, but the wire carries a Bayer mosaic as its byte-identical **GRAY8** plane (the header
  format table has no CFA entries), so CFA-ness still comes from config: a non-empty `CAM_BAYER`
  (+ debayer wanted) makes the pump label the stream `video/x-bayer` and the seam splices
  `bayer2rgb`. A **wrong** pattern only mis-tints the preview — it cannot crash negotiation the way
  the raw-shm static front-end could. 16-bit formats ignore `CAM_BAYER` (no 16-bit `bayer2rgb`).
- **raw shm (legacy): decided from config.** Raw shm carries no caps, so the config is the only
  truth: `CAM_BAYER` non-empty (sensor_env derives it from the camera `pixel_format`) labels the
  stream `video/x-bayer` and statically inserts `bayer2rgb`.

## Why a sibling container (not in-image)

`webrtcsink` isn't packaged for Debian/Ubuntu — it's the Rust `gst-plugin-webrtc`, built from source
(`cargo cinstall`). That toolchain doesn't belong in the core image, so the bridge is a sibling
container that shares the transport (the socket volume; `ipc: host` for the JP6 raw shm data plane),
exactly like the ros2-bridge.

## Build & run

```bash
docker build -f plugins/webrtc-bridge/Dockerfile -t webrtc-bridge .       # ~15-25 min (Rust)
# JP7 (unixfd, self-describing — no geometry needed), sharing the core's transport volume
# (add --device nvidia.com/gpu=all for HW NVENC — the per-sensor stack grants it via the jp7 overlay):
docker run --rm -v cam_sock:/tmp/cam --network host \
  -e CAM_PLATFORM=jp7 -e CAM_BAYER=rggb webrtc-bridge
# JP6 (headered shm — geometry self-describes from the per-frame header):
docker run --rm --ipc=host -v cam_sock:/tmp/cam --network host \
  -e CAM_PLATFORM=jp6 -e CAM_BAYER=rggb webrtc-bridge
# Legacy raw shm (CAM_TRANSPORT=shm-raw — geometry must match the camera):
docker run --rm --ipc=host -v cam_sock:/tmp/cam --network host \
  -e CAM_PLATFORM=jp6 -e CAM_TRANSPORT=shm-raw -e CAM_BAYER=rggb \
  -e CAM_WIDTH=2448 -e CAM_HEIGHT=2048 -e CAM_FPS=24 webrtc-bridge
```

Or via the per-sensor stack: `cam-up <sensor>.yaml up -d webrtc-bridge` (cam-up exports
`CAM_PLATFORM` + sensor_env derives `CAM_BAYER`/geometry from the config).

| Env | Default | Meaning |
|---|---|---|
| `CAM_PLATFORM` | `jp6` | `jp7` → unixfd, else headered shm (cam-up sets it per host) |
| `CAM_TRANSPORT` | _(auto)_ | override the platform default: `unixfd` \| `shm` (headered) \| `shm-raw` (legacy) |
| `CAM_TRANSPORT_SOCKET` | _(by transport)_ | the core's plugin endpoint: `/tmp/cam/unixfd` (unixfd) \| `/tmp/cam/frames` (headered shm) |
| `CAM_SHM_SOCKET` | `/tmp/cam/raw` | raw shm socket (`shm-raw` only) |
| `CAM_BAYER` | _(empty)_ | Bayer pattern (`rggb`/`grbg`/`gbrg`/`bggr`) → debayer to color; empty → mono. Consulted on **headered shm** (mosaic rides as GRAY8; the pattern relabels it — wrong pattern only mis-tints) and **shm-raw / the gst-launch hatch** (static front-end) — on JP7/unixfd the stream's own caps decide at runtime |
| `CAM_WEBRTC_DEBAYER` | `auto` | `false` to preview the raw mosaic instead of debayering (self-describing paths: a zero-copy GRAY8 relabel) |
| `CAM_WEBRTC_NORMALIZE` | `off` | 16-bit mono preview stretch: `auto` (1–99% percentile window, EMA-smoothed) or `lo:hi` (e.g. `5:99.5`). Stretches GRAY16 → GRAY8 **before** the 8-bit convert, so an LSB-aligned radiometric camera (thermal Y16) previews with full contrast instead of near-black. Preview-only — the recording and ROS topic keep the raw 16-bit. Needs the python launcher (the default) |
| `CAM_WEBRTC_LATENCY_OVERLAY` | `off` | burn the live **capture→encode latency** into the video (`textoverlay`, top-left, e.g. `lat 87 ms (ptp)`) — any viewer sees it with zero client support; `lat --` where no capture stamp survives (shm-raw). Needs the python launcher |
| `CAM_WIDTH` / `CAM_HEIGHT` | `512` | **shm-raw only** — must match the camera geometry |
| `CAM_FORMAT` | `GRAY8` | **shm-raw only** — mono raw format when not debayering |
| `CAM_FPS` | `25` | **shm-raw** geometry; on headered shm a caps rate **hint** (the header carries no rate; feeds the H.264 level derivation) |
| `VIDEO_CAPS` | _(unset)_ | e.g. `video/x-h264` to pin the codec; unset → webrtcsink picks |
| `CAM_WEBRTC_PROFILE` | `constrained-baseline` | effectively fixed: webrtcsink forces constrained-baseline for raw input at codec discovery, so `high` **warns + falls back** (knob kept for future upstream support) |
| `CAM_WEBRTC_MAX_LEVEL` | `5.2` | safety clamp on the **auto-derived** H.264 level (the level is computed from the streamed resolution+fps — never fixed) |
| `SIGNALLING_PORT` | `8443` | signalling server port |
| `RUN_SIGNALLING` | `1` | run the bundled signalling server in-container |
| `CAM_WEBRTC_STATUS` | `10` | seconds between status heartbeat lines — pipeline state, frames received from the core, negotiated caps, connected viewers, and per-interval **latency percentiles** (see Latency below) (`0` = off). Startup also logs an encoder **element inventory** and warns when `GST_PLUGIN_FEATURE_RANK` names an element the registry doesn't have |
| `GST_DEBUG` | _(unset)_ | standard GStreamer debug spec for deep dives (e.g. `3,webrtcsink:6`) — forwarded by compose, settable from sensor YAML params |

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

## Latency (measure + display)

On the plugin-endpoint transports every buffer carries the **absolute capture timestamp**
(`offset_end`; PTP-disciplined when the camera is locked — provenance rides in the JP6 header's
`ts_source`), so the bridge measures per-frame `now − capture` at two points:

- **`lat[cap->rx]`** — capture → bridge ingress (core pipeline + transport hop).
- **`lat[cap->enc]`** — capture → `webrtcsink` input (adds the bridge's queue/debayer/convert), so
  `enc − rx` is the bridge's own processing residency — the number to watch when A/B-ing
  `CAM_WEBRTC_DEBAYER`, `videoscale`, or NVENC-vs-x264.

Both appear as per-interval p50/p95 in the status heartbeat, e.g.
`status: ... lat[cap->rx]=p50 6/p95 11ms(n=248) lat[cap->enc]=p50 21/p95 34ms(n=248) ts_src=ptp_chunk`,
and `CAM_WEBRTC_LATENCY_OVERLAY=true` burns the smoothed `cap->enc` number **into the video itself**
(any viewer sees it; no client support needed). On `shm-raw` no capture stamp survives — heartbeat
shows no `lat[...]` and the overlay reads `lat --`.

Caveats: the reading compares the camera's capture stamp against the **bridge host's** clock, so it
is only as true as that sync (PTP-locked camera + `phc2sys` on the host — a constant offset here
usually means the host clock isn't disciplined; a `ts_src=system` stamp is the host **arrival**
time, so exposure/readout is excluded). And it deliberately stops at the encoder input: the encode
itself, network, and viewer-side jitter-buffer/decode legs are per-consumer — read those from
`webrtcsink`'s RTCP stats / the browser's `getStats()`. Carrying the capture stamp all the way to
the viewer (`do-clock-signalling` + ntp-64) is the future step below.

## Test (no Jetson, no camera, no browser)

```bash
./plugins/webrtc-bridge/tools/webrtc_test.sh
```

Runs the full loopback per transport: core fake camera → transport → this bridge (`webrtcsink`) →
[`webrtc_consumer.py`](tools/webrtc_consumer.py) (`webrtcsrc` → decode → counts frames) — headered
shm (self-describing, no geometry env; asserts the latency heartbeat + exercises the burned-in
overlay), headered shm + Bayer relabel, legacy shm-raw (+ the two H.264 profile/level scenarios),
and unixfd on a gst ≥ 1.24 core. Proves the whole egress path without a browser. PASS = each
scenario decoded ≥30 frames.

Discovery has its own test (needs a Linux host for host networking + a Zenoh router):

```bash
./plugins/webrtc-bridge/tools/discovery_test.sh
```

Brings up a `rmw_zenohd` router + core + bridge, and a Zenoh probe
([`discovery_probe.py`](tools/discovery_probe.py)) asserts: a liveliness **PUT** at
`fleet/<vehicle>/media/<sensor>` once streaming, a valid JSON **descriptor** from `get(<key>)`, and a
**DELETE** when the bridge stops.

## Jetson notes

- **HW encoder:** `webrtcsink` discovers encoders and picks the highest-RANKED one for the negotiated
  codec — by default the CPU `x264enc`. NVENC needs two things. (1) **The platform grant** that puts
  `nvv4l2h264enc` in this container: on JP7 the `webrtc-bridge` CDI device entry in
  [docker-compose.jp7.yml](../../docker-compose.jp7.yml) — **validated end-to-end on an R39 AGX Orin**
  (webrtcsink picked `nvv4l2h264enc`, a `webrtcsrc` consumer decoded the stream); on JP6 the
  `runtime: nvidia` + `NVIDIA_VISIBLE_DEVICES` CSV grant in [compose.yml](compose.yml) —
  **unvalidated on JP6 hardware** (r36 CSV injects gst-1.20/22.04-built plugins into this
  24.04/gst-1.24 image; plugin ABI is forward-compatible, but if it doesn't load it blacklists and
  x264enc is used). (2) **The rank**: `GST_PLUGIN_FEATURE_RANK=nvv4l2h264enc:MAX` **+**
  `VIDEO_CAPS=video/x-h264` (both forwarded by compose; settable from the sensor YAML's
  `webrtc-bridge` params — see `config/sensors/cam_rtsp.yaml`). `webrtcsink` inserts the
  `nvvidconv` → NVMM hop itself for `nvv4l2*` encoders; `kmod` is baked in (NVENC init runs `lsmod`).
  The rank is a no-op if the element is absent (e.g. Orin Nano has no H.264 NVENC) — falls back to
  `x264enc`. Verify on the vehicle:
  `cam-up <sensor>.yaml run --rm --no-deps webrtc-bridge gst-inspect-1.0 nvv4l2h264enc`.
- **H.264 level (auto) & profile:** the SDP `profile-level-id` must match the encoded stream or browsers
  receive RTP but decode nothing (a black tile). The bridge derives the **minimum** H.264 level for the
  resolution+fps fed to `webrtcsink` and pins it on the encoder output, so the advertised level tracks the
  stream and any resolution decodes — no fixed level. `CAM_WEBRTC_MAX_LEVEL` (default `5.2`) clamps the
  auto level. The **profile is always constrained-baseline**: `webrtcsink` hard-pins it on its internal
  parser filter whenever it encodes raw input (`parser_caps(force_profile=true)` at codec discovery,
  unchanged through gst-plugins-rs 0.15), so a higher profile cannot be produced by **any** encoder —
  forcing one on the encoder element makes the SPS contradict that filter and discovery dies with
  "No caps found" (reproduced on-device with NVENC). `CAM_WEBRTC_PROFILE=high` therefore warns + falls
  back; the knob remains for the day upstream honors a requested profile. B-frames are forced off for
  live either way. (Applied by the Python launcher via `webrtcsink`'s `encoder-setup` /
  `request-encoded-filter` signals; the `CAM_LAUNCHER=gst-launch` hatch keeps `webrtcsink`'s fixed
  defaults.)
- **Adaptive bitrate / congestion control:** `webrtcsink` runs Google Congestion Control (`gcc`) by
  default and scales each consumer's encoder bitrate to its link — the image ships the `rtpgccbwe`
  element (gst-plugins-rs `rtp` plugin, built in the Dockerfile) that this requires. Besides quality,
  CC is a **latency** guard: a fixed bitrate above a (WiFi) link's momentary capacity queues RTP in
  the network and the stream's glass-to-glass latency grows without recovering; GCC backs the encoder
  off instead. The bounds knobs (bit/sec): `CAM_WEBRTC_MIN_BITRATE` / `CAM_WEBRTC_MAX_BITRATE` /
  `CAM_WEBRTC_START_BITRATE`, plus `CAM_WEBRTC_CONGESTION` (`gcc`|`homegrown`|`disabled`). The
  element default `max-bitrate` is 8 Mbps, which caps quality on a fast link at high res — raise
  `CAM_WEBRTC_MAX_BITRATE` (e.g. `20000000`) for 4K. (Images built before the rtp plugin was added
  lack `rtpgccbwe` — webrtcsink warns `Failed to find element factory ... rtpgccbwe` and parks every
  consumer at `start-bitrate`, 2 Mbps default; on such an image pin
  `CAM_WEBRTC_CONGESTION=disabled` + `CAM_WEBRTC_START_BITRATE=<bps>` as an honest fixed rate.)
- **5MP color is CPU-heavy.** `bayer2rgb` + `videoconvert` + (software) encode at 2448×2048 is a load;
  the `leaky=downstream` queue drops to the newest frame under pressure (correct for a live preview).
  Add a `videoscale` before the encoder, or force NVENC (above), for a lighter stream.
- **Build on-device:** gst-plugins-rs builds against the Jetson's GStreamer (≥ the 1.20 floor); arm64
  builds are RAM-bound — the Dockerfile already uses LTO-off + limited jobs.

## Known limitations / future

- **Geometry env is `shm-raw`-only now.** Both platform defaults consume the plugin endpoint
  (unixfd / shm+header) and self-describe; only the legacy raw path still needs
  `CAM_WIDTH/HEIGHT/FORMAT` to be right.
- **Capture timestamp stops at the encoder.** Both plugin-endpoint transports now deliver the
  absolute capture time in-bridge (the latency metrics above); it is not yet propagated **to the
  viewer**. A future version could thread it through `webrtcsink do-clock-signalling=true` + the
  `ntp-64` RTP header extension so a `webrtcsrc` consumer recovers absolute capture time
  (`GstReferenceTimestampMeta`) and computes true glass-to-glass; browsers would additionally need
  the `abs-capture-time` extension (ntp-64 isn't consumed by standard JS). The burned-in overlay is
  the zero-client-support stopgap.
- **The header pump's format seam resolves once.** A camera that reconnects mid-stream with a
  different format/geometry re-stamps caps (raw formats renegotiate fine), but a Bayer↔mono flip
  after start needs a bridge restart — the pump warns when this happens.
