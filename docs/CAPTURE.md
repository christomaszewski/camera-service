# Frame capture (Zenoh-triggered JPEG keyframes)

`frame-capture` is an in-image plugin that keeps the last couple of seconds of a camera's frames
and, when something asks over Zenoh, writes the frame nearest a requested time as a JPEG, with GPS
EXIF when the request carries a fix, plus a JSON sidecar. It is the camera half of condition-based
capture: a separate policy (for example a node that watches the INS and fires when the footprint
overlap with the last kept frame drops below a threshold) decides **when**; this plugin turns
"the frame at time *t*" into a georeferenced image in the run.

It never touches the camera or the recorder. Like every plugin it reads the core's plugin endpoint
(plugins/README.md), so it works for every source type, live or played back.

## Enable it

```yaml
plugins:
  - name: frame-capture
    isolation: process        # spawned by the supervisor in the core image; no extra container
    params:
      sinks: [file]
```

All parameters, with defaults:

| Param | Default | Meaning |
|---|---|---|
| `buffer_s` | `2.0` | Seconds of recently received frames kept to choose from |
| `buffer_max_mb` | `256` | Byte cap on that buffer; the oldest frames go first (a 12 MP camera at 20 fps needs ~480 MB for 2 s) |
| `tolerance_ms` | one frame interval | How far the chosen frame may be from the requested time (measured from the stream; 50 ms until known) |
| `wait_ms` | `250` | How long a request may wait for its frame to come through the pipeline |
| `quality` | `90` | JPEG quality, 1–100 |
| `jpeg_encoder` | `auto` | `auto` (the Jetson's hardware `nvjpegenc` when it is present and passes a startup probe, else CPU `jpegenc`), `hw` (the same, but a missing or failing hardware encoder is reported as a fallback), `cpu` |
| `max_width` | `0` | Downscale to this width (keeping the aspect ratio); `0` = full size |
| `mono16` | `stretch` | 16-bit → 8-bit: `stretch` (0.5–99.5 percentile range → 0–255) or `shift` (the high byte) |
| `bayer_pattern` | `auto` | CFA pattern of a mono plane: `auto` (`recording.bayer_pattern`, else the source's `pixel_format`), `none`, or `rggb`/`grbg`/`gbrg`/`bggr` |
| `time_basis` | `auto` | `auto`, `capture` or `arrival` (see [Time](#time)) |
| `time_scale` | `auto` | What capture stamps are: `auto`, `utc` or `tai` |
| `tai_utc_offset_s` | `auto` | TAI − UTC; `auto` = the kernel's value when ptp4l/phc2sys/chrony set it, else 37 |
| `max_latency_ms` | `5000` | Capture → plugin receipt budget used by the plausibility check |
| `queue` | `4` | Requests waiting to be served; one more is refused with `busy` |
| `sinks` | `[file]` | Any of `file`, `zenoh` (see [Outputs](#outputs)) |
| `file_dir` | `keyframes` | `file` sink directory, relative to the camera's recording directory (or absolute) |
| `store_prefix` | `fleet/{vehicle}/store/{run}/{instance}/keyframes` | `zenoh` sink key prefix |
| `transport` | `auto` | `auto`, `unixfd` or `shm` |

A config error (an unknown key, a bad value) exits the plugin with code 2, naming the parameter;
the camera keeps running.

## Keys

On the same base as the core's lifecycle and health ([LIFECYCLE.md](LIFECYCLE.md),
[HEALTH.md](HEALTH.md)); `<vehicle_id>` is `VEHICLE_ID` (else the hostname) and `<instance>` is
`CAM_INSTANCE`.

```
fleet/<vehicle_id>/svc/<instance>/capture           liveliness token + queryable → descriptor
fleet/<vehicle_id>/svc/<instance>/capture/request   queryable: a request → a reply once written
fleet/<vehicle_id>/svc/<instance>/capture/events    publisher: one record per written capture
```

Every queryable replies on its own concrete key. The session is a Zenoh peer with the same connect
settings as the core's control plane (`control.zenoh_connect`, else `ZENOH_CONNECT`, else
`tcp/localhost:7447`); if Zenoh is unreachable the plugin keeps buffering and retries every 5 s.

## Request

`get fleet/<vehicle_id>/svc/<instance>/capture/request` with a JSON object payload (≤ 16 KiB):

```jsonc
{ "v": 1,
  "stamp_ns": 1762610400123456789,          // UTC Unix ns of the wanted moment; omit = the newest frame
  "tolerance_ms": 30,                        // optional, overrides the default
  "wait_ms": 200,                            // optional, overrides the default
  "gps": {                                   // optional
    "lat": 40.4433, "lon": -79.9436,         // degrees, WGS-84
    "alt_m": 276.3, "alt_ref": "ellipsoid",  // or "msl"; GNSS (and NavSatFix) report ellipsoid height
    "geoid_sep_m": -33.9,                    // geoid separation N = ellipsoid − MSL (NovAtel "undulation")
    "stamp_ns": 1762610400120000000,         // when the fix applies; omit = the request's stamp_ns
    "vel_enu": [1.2, 4.8, 0.0],              // m/s east, north, up: moves the fix to the chosen frame
    "h_acc_m": 0.02, "v_acc_m": 0.03 },
  "orientation": { "yaw_deg": 84.0, "pitch_deg": -2.1, "roll_deg": 0.4 },   // optional; yaw = heading,
                                                                            // degrees clockwise from true north
  "tag": "overlap",                          // why: overlap | manual | interval | detector | ...
  "id": "req-17",                            // optional, echoed in the reply
  "meta": { } }                              // optional, copied into the sidecar as-is
```

An empty request takes the newest frame (a manual snapshot). A payload-less get may use selector
parameters for the simple fields: `.../capture/request?stamp_ns=1762610400123456789;tag=interval`.

The plugin writes the orientation you send; it does not apply the camera's mounting. Send the
camera's own heading if you want `GPSImgDirection` to be where the camera points.

## Reply

Sent after the outputs are written, so `ok: true` means the capture exists.

```jsonc
{ "ok": true, "id": "req-17",
  "frame_id": 48211,
  "frame_stamp_ns": 1762610400125000000,    // the frame's match time (UTC); also its file name
  "raw_stamp_ns": 1762610437125000000,      // as the core delivered it
  "basis": "capture_tai",                   // capture | capture_tai | plugin_arrival (see Time)
  "stamp_source": "ptp_chunk",              // JP6 header only; null on JP7 (unixfd carries no source)
  "dt_ms": 1.5,                             // frame time − requested time
  "gps": "extrapolated",                    // extrapolated | as_given | none
  "gps_dt_ms": 5.0,                         // frame time − fix time
  "bytes": 48213, "duplicate": false,
  "sinks": { "file": { "ok": true, "jpg": "…/1762610400125000000.jpg", "json": "…/1762610400125000000.json" },
             "zenoh": { "ok": false, "error": "store_absent" } } }
```

`ok` is true when at least one sink wrote. A refusal is `{"ok": false, "error": <code>,
"detail": …}`:

| `error` | Meaning |
|---|---|
| `bad_request` | The request did not parse; `detail` names the field |
| `no_frames` | Nothing buffered yet (the camera is not streaming, or the plugin just started) |
| `too_old` | The requested time is before everything in the buffer; `detail` has the buffer's span |
| `no_frame_within_tolerance` | No frame close enough, after waiting `wait_ms` for a newer one |
| `busy` | `queue` requests are already waiting |
| `encode_failed` | The frame could not be encoded |
| `write_failed`, `store_absent`, `no_run`, `no_session`, `put_failed` | Every sink failed; the first sink's reason |

The same frame requested twice (for example an overlap trigger and a manual snapshot) is written
once; the second reply repeats the first with `duplicate: true`.

## Choosing the frame

Frames are copied out of the transport into a buffer holding what was received in the last
`buffer_s` (and at most `buffer_max_mb`). A request names a UTC time; the plugin waits until a
frame at or after that time has arrived (up to `wait_ms`), then takes the nearest one within the
tolerance. With frames every 40 ms the chosen frame is at most ~20 ms from the requested time.

## Time

A request's `stamp_ns` is UTC (ROS time). The core delivers each frame's stamp in its source's own
time (cam_driver/timestamps.py), so each frame gets a **match time** and a **basis**:

| The frame's stamp is… | Example sources | Match time | `basis` |
|---|---|---|---|
| UTC, shortly before the plugin received it | `system`, `sof`, `rtp_ntp` | the stamp | `capture` |
| TAI (UTC + 37 s), shortly before receipt | `ptp_chunk` from a TAI grandmaster | stamp − offset | `capture_tai` |
| neither: a free-running counter | `ptp_chunk` / `camera` without a PTP lock | the plugin's receipt time | `plugin_arrival` |

"Shortly before" means within `max_latency_ms` of receipt (and no more than 0.5 s after it). The
JP7 endpoint does not say which source a stamp came from and neither endpoint says whether PTP is
locked, so this is decided per frame by plausibility. The ambiguity is wide: a counter since
power-on is decades from now, and TAI is 37 s from UTC. A GigE camera without PTP therefore
falls back to receipt time on its own, a few ms after the core's arrival stamp. That is good
enough for overlap decisions, and it switches to real capture times by itself once PTP locks.

- `time_basis: capture` trusts the stamps as delivered (after `time_scale`) even far from now.
  `auto` does the same for playback sources (`replay`, `pcap`), whose stamps are recorded capture
  times. `arrival` always uses receipt time.
- `time_scale` restricts which scale is accepted. With `auto`, both UTC and TAI are tried.
- The pose side needs the same care. A GNSS driver stamping from GPS time
  (`stamp_from_gps: true`) is 18 s ahead of UTC unless it converts.

## Image

JPEG is 8-bit, so 16-bit mono (a thermal core's Y16) is scaled by `mono16` first, and a CFA mosaic
is demosaiced (`bayer2rgb`). JP7 advertises an 8-bit mosaic as `video/x-bayer`. On JP6, and for
>8-bit mosaics, the pattern comes from `bayer_pattern`. A frame shorter than its geometry is
refused (`encode_failed`) rather than encoded from garbage.

**Encoder.** With `jpeg_encoder: auto` the plugin uses the Jetson's hardware JPEG encoder
(`nvjpegenc`, from NVIDIA's L4T GStreamer plugins: in the JP6 image's L4T layer, injected by CDI on
JP7) when the element exists **and** encodes a 16×16 probe frame at startup; otherwise the CPU
`jpegenc`. The hardware encoder is fed system-memory I420, so a mono camera's JPEG is a colour JPEG
of a grey image, and an odd-sized frame loses its last column / row (cropped, not resampled; a
`max_width` downscale is already even). One encoder is chosen per process: NVIDIA's libjpeg
replacement and the stock libjpeg have been reported to clash when both are loaded in one process,
so the CPU encoder is only loaded beside the hardware one after the hardware one has failed. If the
hardware encoder fails on a frame at runtime, that frame is retried on the CPU and the plugin stays
on the CPU for the life of the process. The descriptor's `encoder` block (`backend`, `hardware`,
`requested`, `fallback`) and each sidecar's `image.encoder` say which encoder was used.

`tools/jpeg_encode_benchmark.py` compares the two on a host, per frame size, each in its own process
(wall and CPU ms per frame). Run it on the Orin before relying on either: the hardware encoder takes
the work off the CPU, but the repo's own measurement of the hardware JPEG *decoder* found it slower
than the CPU at 720p because of the memory round trip, and small frames may behave the same way.
`jpeg_encoder: cpu` opts out.

## EXIF

| IFD | Tags |
|---|---|
| IFD0 | `Software`, `DateTime` |
| Exif | `ExifVersion`, `DateTimeOriginal` + `SubSecTimeOriginal` (µs) + `OffsetTimeOriginal` `+00:00`: the frame's UTC match time; `PixelXDimension`/`PixelYDimension` |
| GPS (with a fix) | `GPSLatitude`/`GPSLongitude` (+ refs), `GPSTimeStamp`/`GPSDateStamp`, `GPSMapDatum` WGS-84; `GPSAltitude` (+ ref) when a height above **mean sea level** is known (`alt_ref: msl`, or ellipsoid height with `geoid_sep_m`); `GPSSpeed` + `GPSTrack` with a velocity; `GPSHPositioningError` with `h_acc_m` |
| GPS (with a yaw) | `GPSImgDirection` (true north) |

EXIF has no pitch or roll and no ellipsoid height; both are in the sidecar. The segment is spliced
into the encoded JPEG (no re-encode) and was checked with exiftool and Pillow.

## Outputs

Each sink writes the JPEG first and the sidecar second, so the sidecar marks a complete capture.
Files are named by the frame's match time in nanoseconds, zero-padded to 19 digits, so they sort
in time order.

- **`file`**: `<recording dir>/keyframes/<utc_ns>.jpg` and `.json`. The recording directory is the
  core's own (`resolve_recording_dir`, pinned at process start the same way), so under rig the captures
  land in `<data_dir>/runs/<run>/recordings/<instance>/keyframes/` and `rig fleet sync` carries
  them. Each file is written to a temp name and renamed into place.
- **`zenoh`**: one put per file on `<store_prefix>/<utc_ns>.{jpg,json}` (default
  `fleet/<vehicle>/store/<run>/<instance>/keyframes`), image/jpeg and application/json, with
  congestion control BLOCK and low priority. This is for the vehicle store, a Zenoh storage on
  `fleet/<vehicle>/store/<run>/**`. A put nobody subscribes to is lost silently, so the sink checks
  whether a storage matches its prefix (a publisher's matching status) and refuses with
  `store_absent` instead. Without an open run there is no key to use: `no_run`.

The sidecar:

```jsonc
{ "schema_version": 1, "service": "frame-capture", "vehicle_id": "7", "instance": "cam_front",
  "run_id": "20261108T140000Z_flight1",
  "frame":   { "frame_id": …, "utc_ns": …, "basis": "capture", "stamp_ns": …, "stamp_source": …,
               "arrival_ns": …, "format": "GRAY8", "bayer": null, "width": …, "height": … },
  "image":   { "file": "<utc_ns>.jpg", "width": …, "height": …, "quality": 90, "encoder": "nvjpegenc", "bytes": … },
  "request": { "stamp_ns": …, "tag": "overlap", "id": …, "meta": { … } },
  "match":   { "dt_ms": 1.5 },
  "gps":     { "given": { … }, "applied": { … }, "method": "extrapolated", "dt_ms": 5.0, "msl_alt_m": 310.2 },
  "orientation": { "yaw_deg": …, "pitch_deg": …, "roll_deg": … },
  "written_unix_ns": … }
```

## Descriptor and events

The descriptor (`get .../capture`) reports the buffer (frames, bytes, oldest/newest match time,
measured frame interval, current basis), the reader (transport, connected, frames, errors), the
encoder (backend, hardware, requested, fallback), the time settings, the default tolerance and wait, each sink (with the store's state: `present`, `absent`,
`no_run`, `no_session`), and counts with the last successful reply. `.../capture/events` publishes
each successful reply with `instance` added.

## Not yet

- Focal length and other camera-model EXIF, once a camera calibration block exists.
- Pitch and roll in XMP (e.g. Pix4D's `Camera:Yaw/Pitch/Roll`) for photogrammetry tools.
- A lossless 16-bit output (PNG/TIFF) for radiometric thermal frames.
- The hardware encoder has not been run on a Jetson yet: its CI coverage uses `jpegenc` standing in
  for `nvjpegenc` (same I420 input, crop, probe and fallback logic).

## Testing

`core-driver/tests/test_capture_*.py`: time normalization, the buffer and selection, request
parsing and GPS extrapolation, EXIF parsed back, the encoder on every input shape and the hardware
path's selection, probe and fallback (with a stand-in element), the reader
against real shm and unixfd producers (including a producer restart), the request pipeline and
sinks, and the Zenoh adapter against fakes and against a real Zenoh session with a subscriber
standing in for the store. `core-driver/config/capture-fake.yaml` runs the plugin on the Aravis
fake camera: `python3 supervisor.py -c config/capture-fake.yaml`.
