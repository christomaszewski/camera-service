# Frame capture (Zenoh-triggered JPEG keyframes)

`frame-capture` is an in-image plugin that keeps the last couple of seconds of a camera's frames
and, when something asks over Zenoh, writes the frame nearest a requested time as a JPEG and/or a
16-bit TIFF, with GPS EXIF and Pix4D XMP when the request carries a fix and an orientation, plus a
JSON sidecar. The images are meant to go straight into photogrammetry tools
([Using captures in Pix4Dmapper](#using-captures-in-pix4dmapper)). It is the camera half of condition-based
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
| `buffer_fps` | every frame | Keep at most this many frames a second, evenly spaced; the rest are dropped before they are copied (see [Choosing the frame](#choosing-the-frame)) |
| `tolerance_ms` | one frame interval | How far the chosen frame may be from the requested time (measured from the stream; 50 ms until known) |
| `wait_ms` | `250` | How long a request may wait for its frame to come through the pipeline |
| `formats` | `[jpeg]` | Any of `jpeg`, `tiff` (see [TIFF](#tiff)); both are written when both are listed |
| `quality` | `90` | JPEG quality, 1–100 |
| `jpeg_encoder` | `auto` | `auto` (the Jetson's hardware `nvjpegenc` when it is present and passes a startup probe, else CPU `jpegenc`), `hw` (the same, but a missing or failing hardware encoder is reported as a fallback), `cpu` |
| `max_width` | `0` | Downscale to this width (keeping the aspect ratio); `0` = full size |
| `mono16` | `stretch` | 16-bit → the JPEG's 8 bits: `stretch` (each frame's 0.5–99.5 percentile range → 0–255), `fixed` (`mono16_range` → 0–255 for every frame) or `shift` (the high byte) |
| `mono16_range` | — | `[lo, hi]` raw values for `mono16: fixed` |
| `camera_make`, `camera_model` | — | EXIF `Make` / `Model` (printable ASCII) |
| `focal_length_mm` | — | EXIF `FocalLength` |
| `pixel_pitch_um` | — | Sensor pixel size → EXIF `FocalPlaneX/YResolution` (and `FocalLengthIn35mmFilm` with a focal length) |
| `band_name`, `central_wavelength_nm`, `wavelength_fwhm_nm` | — | XMP `Camera:BandName` / `CentralWavelength` / `WavelengthFWHM` (e.g. `LWIR` for a thermal core) |
| `exif_altitude` | `msl` | What EXIF `GPSAltitude` holds: `msl` (above mean sea level, the EXIF definition) or `ellipsoid` (WGS-84 ellipsoid height) |
| `bayer_pattern` | `auto` | CFA pattern of a mono plane: `auto` (`recording.bayer_pattern`, else the source's `pixel_format`), `none`, or `rggb`/`grbg`/`gbrg`/`bggr` |
| `time_basis` | `auto` | `auto`, `capture` or `arrival` (see [Time](#time)) |
| `time_scale` | `auto` | What capture stamps are: `auto`, `utc` or `tai` |
| `tai_utc_offset_s` | `auto` | TAI − UTC; `auto` = the kernel's value when ptp4l/phc2sys/chrony set it, else 37 |
| `max_latency_ms` | `5000` | Capture → plugin receipt budget used by the plausibility check |
| `queue` | `4` | Requests waiting to be served; one more is refused with `busy` |
| `sinks` | `[file]` | Any of `file`, `zenoh` (see [Outputs](#outputs)) |
| `file_dir` | `keyframes` | `file` sink directory, relative to `file_root` (or absolute, with `file_root: recordings` only) |
| `file_root` | `recordings` | `recordings`: under the camera's recording directory. `store`: under the run's `store/<instance>/`, which the vehicle store serves (see [Outputs](#outputs)) |
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
  "orientation": {                          // optional: the CAMERA's orientation, Pix4D's convention
    "yaw_deg": 84.0,                         //   where the top of the image points, clockwise from true north
    "pitch_deg": 0.0,                        //   0 = looking straight down (nadir), 90 = at the horizon
    "roll_deg": 0.4,
    "yaw_acc_deg": 2.0, "pitch_acc_deg": 0.5, "roll_acc_deg": 0.5 },   // optional 1-sigma accuracies
  "tag": "overlap",                          // why: overlap | manual | interval | detector | ...
  "id": "req-17",                            // optional, echoed in the reply
  "meta": { } }                              // optional, copied into the sidecar as-is
```

An empty request takes the newest frame (a manual snapshot). A payload-less get may use selector
parameters for the simple fields: `.../capture/request?stamp_ns=1762610400123456789;tag=interval`.

The plugin writes the orientation you send; it does not apply the camera's mounting. Send the
camera's orientation (vehicle attitude composed with the mount), in Pix4D's convention: it goes
verbatim into `Camera:Yaw/Pitch/Roll`, and the yaw also into `GPSImgDirection`.

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
  "formats": ["jpeg", "tiff"],              // what was written for this frame
  "bytes": 704213, "duplicate": false,      // all image files together
  "sinks": { "file": { "ok": true, "jpg": "…/1762610400125000000.jpg", "tif": "…/1762610400125000000.tif",
                       "json": "…/1762610400125000000.json" },
             "zenoh": { "ok": false, "error": "store_absent" } },
  "warnings": [ … ] }                       // only when a format was skipped (tiff for a non-mono frame)
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
| `unsupported_format` | None of `formats` applies to this frame (`tiff` alone for a colour or mosaic frame) |
| `write_failed`, `store_absent`, `no_run`, `no_session`, `put_failed` | Every sink failed; the first sink's reason |

The same frame requested twice (for example an overlap trigger and a manual snapshot) is written
once; the second reply repeats the first with `duplicate: true`.

## Choosing the frame

Frames are copied out of the transport into a buffer holding what was received in the last
`buffer_s` (and at most `buffer_max_mb`). A request names a UTC time; the plugin waits until a
frame at or after that time has arrived (up to `wait_ms`), then takes the nearest one within the
tolerance. With frames every 40 ms the chosen frame is at most ~20 ms from the requested time.

Copying every frame is the plugin's standing cost, paid whether or not anything asks for a capture.
`buffer_fps` keeps fewer: a frame outside the rate is dropped before its pixels are touched, so CPU
and memory fall with it. Measured on a 2.1 GHz Xeon core with the plugin's reader and buffer (an
Orin core is slower; measure there):

| Camera | Every frame | With `buffer_fps` |
|---|---|---|
| Thermal 640×512 16-bit @ 60 fps | 3.5% of a core, 79 MB | `10`: 1.7%, 14 MB |
| 5 MP mono @ 20 fps | 5.5%, 201 MB | `5`: 1.9%, 50 MB |
| 12 MP mono @ 10 fps | 6.2%, 246 MB | `2`: 1.3%, 49 MB |

The trade is precision: at `buffer_fps: 10` the chosen frame can be up to ~50 ms from the requested
time (the default tolerance follows the kept frames' spacing). The GPS fix is still moved to the
chosen frame's own time, so the geotag stays right; what moves is which instant the image shows.

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
is demosaiced (`bayer2rgb`). `stretch` (the default) maps each frame's own range, which makes a
single image readable but its brightness not comparable with the next frame's; a mosaic of thermal
JPEGs wants `mono16: fixed` with a `mono16_range` covering the scene. The values themselves survive
only in the [TIFF](#tiff). JP7 advertises an 8-bit mosaic as `video/x-bayer`. On JP6, and for
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
`requested`, `fallback`) and each sidecar's `images.jpeg.encoder` say which encoder was used.

`tools/jpeg_encode_benchmark.py` compares the two on a host, per frame size, each in its own process
(wall and CPU ms per frame). Run it on the Orin before relying on either: the hardware encoder takes
the work off the CPU, but the repo's own measurement of the hardware JPEG *decoder* found it slower
than the CPU at 720p because of the memory round trip, and small frames may behave the same way.
`jpeg_encoder: cpu` opts out.

## TIFF

`formats: [tiff]` (or `[jpeg, tiff]`) writes a mono frame (GRAY8, GRAY16) as a baseline single-band
TIFF: the camera's own samples, **unscaled**, little-endian, one uncompressed strip, with the same
EXIF, GPS and XMP as the JPEG (the XMP in tag 700). This is the radiometric output: what the numbers
mean is the camera's business: raw counts, or temperature for a radiometric core in a linear mode
(FLIR's TLinear modes are kelvin × 100 at high gain; check your core's documentation). `max_width`
does not apply. A colour or mosaic frame cannot be written as TIFF: with `[jpeg, tiff]` the reply
carries a warning and only the JPEG is written; with `[tiff]` alone the capture is refused
(`unsupported_format`). A 640×512 16-bit TIFF is 656 KB.

## EXIF and XMP

| Where | Tags |
|---|---|
| IFD0 | `Software`, `DateTime`, `X/YResolution` + `ResolutionUnit` (+ `YCbCrPositioning` in a JPEG); `Make`, `Model` when configured |
| Exif | `ExifVersion`, `FlashpixVersion`, `ColorSpace` (sRGB in a JPEG, uncalibrated in a TIFF), `DateTimeOriginal` + `SubSecTimeOriginal` (µs) + `OffsetTimeOriginal` `+00:00`: the frame's UTC match time; in a JPEG `ComponentsConfiguration` and `PixelXDimension`/`PixelYDimension`; `FocalLength` with `focal_length_mm`; `FocalPlaneX/YResolution` (pixels per cm, scaled for a downscaled JPEG) with `pixel_pitch_um`; `FocalLengthIn35mmFilm` with both |
| GPS (with a fix) | `GPSLatitude`/`GPSLongitude` (+ refs), `GPSTimeStamp`/`GPSDateStamp`, `GPSMapDatum` WGS-84, `GPSProcessingMethod`; `GPSAltitude` (+ ref) when the height for `exif_altitude` is known (`msl`: `alt_ref: msl`, or an ellipsoid height with `geoid_sep_m`; `ellipsoid`: the reverse); `GPSSpeed` + `GPSTrack` with a velocity; `GPSHPositioningError` with `h_acc_m` |
| GPS (with a yaw) | `GPSImgDirection` (true north) |
| XMP, Pix4D's `Camera` namespace (`http://pix4d.com/camera/1.0/`) | `Yaw`, `Pitch`, `Roll` from the request's orientation; `GPSXYAccuracy` / `GPSZAccuracy` from `h_acc_m` / `v_acc_m`; `IMUYawAccuracy` / `IMUPitchAccuracy` / `IMURollAccuracy` from the orientation's accuracies; `BandName`, `CentralWavelength`, `WavelengthFWHM` from the band settings |

EXIF's `GPSAltitudeRef` can only say above or below *sea level*: with `exif_altitude: ellipsoid` the
number is an ellipsoid height in a field labelled sea level, as many drones write it. The sidecar
keeps both heights. In a JPEG, the EXIF and the XMP are each an APP1 segment spliced into the
encoded image (no re-encode). Both containers pass `exiftool -validate` and were read back with
exiftool, Pillow and libtiff's `tiffinfo`.

## Outputs

Each sink writes the image file(s) first and the sidecar last, so the sidecar marks a complete
capture.
Files are named by the frame's match time in nanoseconds, zero-padded to 19 digits, so they sort
in time order.

- **`file`**: `<recording dir>/keyframes/<utc_ns>.jpg` / `.tif` and `.json`. The recording directory is the
  core's own (`resolve_recording_dir`, pinned at process start the same way), so under rig the captures
  land in `<data_dir>/runs/<run>/recordings/<instance>/keyframes/` and `rig fleet sync` carries
  them. Each file is written to a temp name and renamed into place.
  - With **`file_root: store`** the same files go to `<data_dir>/runs/<run>/store/<instance>/keyframes/`
    instead: the run's `store/` folder, which rig-infra's
    [zenoh-store](https://github.com/christomaszewski/rig-infra/tree/main/zenoh-store) serves as
    `fleet/<vehicle>/store/<run>/<instance>/keyframes/<utc_ns>.{jpg,tif,json}` (the keys the
    `zenoh` sink uses), indexes, announces, and lets a ground station fetch mid-run. Its fetch rules
    decide which of them cross the link on their own (by default sidecars and JPEGs; TIFFs on
    request). The plugin does not depend on the store being up: the files are on disk either way,
    and `rig fleet sync` carries `store/` with the run. This is the setting for a camera on the same
    disk as the store; it cannot be combined with the `zenoh` sink (they would write the same keys).
    Without a run registry the folder is `<data_dir>/store/` (served as the run `flat`).
- **`zenoh`**: one put per file on `<store_prefix>/<utc_ns>.{jpg,tif,json}` (default
  `fleet/<vehicle>/store/<run>/<instance>/keyframes`), image/jpeg, image/tiff and application/json, with
  congestion control BLOCK and low priority. This is for a producer that does not share the store's
  disk: zenoh-store subscribes to `fleet/<vehicle>/store/**` and writes puts into the open run. A put
  nobody subscribes to is lost silently, so the sink checks whether a store matches its prefix (a
  publisher's matching status) and refuses with `store_absent` instead. Without an open run there
  is no key to use: `no_run`.

The sidecar:

```jsonc
{ "schema_version": 1, "service": "frame-capture", "vehicle_id": "7", "instance": "cam_front",
  "run_id": "20261108T140000Z_flight1",
  "frame":   { "frame_id": …, "utc_ns": …, "basis": "capture", "stamp_ns": …, "stamp_source": …,
               "arrival_ns": …, "format": "GRAY8", "bayer": null, "width": …, "height": … },
  "images":  { "jpeg": { "file": "<utc_ns>.jpg", "width": …, "height": …, "quality": 90, "encoder": "nvjpegenc", "bytes": … },
               "tiff": { "file": "<utc_ns>.tif", "width": …, "height": …, "bits": 16, "bytes": … } },
  "camera":  { "make": "FLIR", "model": "Boson 640", "focal_length_mm": 9.1, "pixel_pitch_um": 12.0 },
  "request": { "stamp_ns": …, "tag": "overlap", "id": …, "meta": { … } },
  "match":   { "dt_ms": 1.5 },
  "gps":     { "given": { … }, "applied": { … }, "method": "extrapolated", "dt_ms": 5.0,
               "msl_alt_m": 310.2, "ellipsoid_alt_m": 276.3, "exif_altitude": "msl" },
  "orientation": { "yaw_deg": …, "pitch_deg": …, "roll_deg": …, "yaw_acc_deg": … },
  "written_unix_ns": … }
```

## Using captures in Pix4Dmapper

What Pix4Dmapper reads from an image, and where a capture provides it:

| Pix4D needs | From | Configure |
|---|---|---|
| Position (WGS-84 latitude, longitude, altitude), read from EXIF on import | EXIF GPS | send `gps` in the request |
| Position accuracy (else its default: 5 m horizontal, 10 m vertical) | XMP `Camera:GPSXYAccuracy` / `GPSZAccuracy` | send `gps.h_acc_m` / `v_acc_m` |
| Initial orientation, converted to omega/phi/kappa on import | XMP `Camera:Yaw` / `Pitch` / `Roll` | send `orientation`: the camera's, pitch 0 = nadir, yaw 0 = image top towards north |
| Orientation accuracy (else its default: 10° roll and pitch, 15° yaw) | XMP `Camera:IMU*Accuracy` | send `orientation.*_acc_deg` |
| The camera model: matched in its database by `Make` / `Model`, or built from the focal length and pixel size and saved to the user database | EXIF `Make`, `Model`, `FocalLength`, `FocalPlaneX/YResolution` | set `camera_make`, `camera_model`, `focal_length_mm`, `pixel_pitch_um`; otherwise define the camera once by hand |
| Thermal: a single-band 8–16-bit TIFF (Pix4D recommends FLIR's R-JPEG; TIFF is supported) | `formats: [tiff]` | `band_name: LWIR` (+ wavelengths) for the XMP band tags |

Checklist for a dataset:

1. Take the files from `runs/<run>/recordings/<instance>/keyframes/` (`fleet sync` brings them
   home): the `.jpg` for RGB / mono mapping, the `.tif` for thermal. The `.json` sidecars are not
   needed by Pix4D.
2. Keep one camera's images uniform: the same `formats`, `max_width` and camera settings for the
   whole dataset (Pix4D groups images into a camera model by their size and EXIF).
3. Altitude: Pix4D's community reports disagree on whether it treats EXIF `GPSAltitude` as
   ellipsoidal or orthometric, and it lets you choose the vertical coordinate system in the Image
   Properties Editor. Pick `exif_altitude` to match how you set that (`msl`: the geoid your GNSS
   uses for `geoid_sep_m`; `ellipsoid`: geoid height 0 above the ellipsoid), then check one initial
   camera position against a known point.
4. Thermal: use Pix4D's thermal processing template. The TIFF values pass through unchanged: from a
   core in a kelvin × 100 mode, Pix4D's thermal reflectance map is in centi-kelvin (÷ 100, − 273.15 for
   °C, the same convention MicaSense documents for its thermal band); from raw counts it is relative
   only. Don't feed `mono16: stretch` JPEGs to a thermal map: each frame is scaled differently.
5. If you need to override positions afterwards, Pix4D also imports an image geolocation file
   (image name, latitude, longitude, altitude, then optionally orientation and accuracies). Every
   value for one is in the sidecars.

## Descriptor and events

The descriptor (`get .../capture`) reports the buffer (frames, bytes, oldest/newest match time,
measured frame interval, current basis), the reader (transport, connected, frames kept and skipped by `buffer_fps`, errors), the
encoder (backend, hardware, requested, fallback), the time settings, the default tolerance and
wait, each sink (with the store's state: `present`, `absent`, `no_run`, `no_session`), and counts
with the last successful reply. `.../capture/events` publishes
each successful reply with `instance` added.

## Not yet

- Camera intrinsics beyond focal length and pixel pitch (principal point, distortion: Pix4D's
  `Camera:PrincipalPoint` / `PerspectiveDistortion`), once a camera calibration block exists.
- The captures have not been loaded into Pix4D itself here (no installation, and Pix4D's support
  site is outside this environment's network); see the checklist in
  [Using captures in Pix4Dmapper](#using-captures-in-pix4dmapper).
- The hardware encoder has not been run on a Jetson yet: its CI coverage uses `jpegenc` standing in
  for `nvjpegenc` (same I420 input, crop, probe and fallback logic).

## Testing

`core-driver/tests/test_capture_*.py`: time normalization, the buffer and selection, request
parsing and GPS extrapolation, EXIF and XMP parsed back, the TIFF parsed back (samples, image tags,
EXIF/GPS IFDs, XMP), the encoder on every input shape and the hardware
path's selection, probe and fallback (with a stand-in element), the reader
against real shm and unixfd producers (including a producer restart), the request pipeline and
sinks, and the Zenoh adapter against fakes and against a real Zenoh session with a subscriber
standing in for the store. `core-driver/config/capture-fake.yaml` runs the plugin on the Aravis
fake camera: `python3 supervisor.py -c config/capture-fake.yaml`.
