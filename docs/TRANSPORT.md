# Same-host frame transport — output endpoints and the shm input

The core moves frames between processes on one host over GStreamer's shared-memory elements. This
document is the contract for both directions: the **output** endpoints every plugin reads (the
bridges), and the **input** a `camera.type: shm` instance reads from any other process — a
simulator's render, a point-cloud preview, another instance's raw endpoint. The wire format itself
lives in [`core-driver/cam_driver/transport.py`](../core-driver/cam_driver/transport.py) (the 36-byte
`FrameHeader`, `application/x-cam-frame`); this page is about who owns what and what happens when
the other side goes away.

## Output (the core writes, plugins read)

| Endpoint | Socket (default) | Caps | Carries |
|---|---|---|---|
| plugin transport, JP6 | `/tmp/cam/frames` | `application/x-cam-frame` | header (timestamp, frame id, geometry, provenance) + pixels |
| plugin transport, JP7 | `/tmp/cam/unixfd` | native `video/x-raw` over `unixfdsink` | caps on the socket; frame id in `.offset`, capture time in `.offset_end` |
| raw endpoint (optional) | `/tmp/cam/raw` | `video/x-raw` | pixels only |

The core owns these sockets (it clears stale ones at build), a consumer is the client: it mounts the
instance's `cam_<name>_sock` volume, runs with `ipc: host` (the shm plugin moves pixels through
`/dev/shm`; the socket only carries control), and reconnects when the core restarts.

## Input (`camera.type: shm` — the core reads, another process writes)

The roles reverse: the **writer owns the socket**, the core is the client. Three framings, one
source (`unixfd` input follows for hosts with GStreamer ≥ 1.24):

| `shm.framing` | The writer sends | Stamps | Geometry / format |
|---|---|---|---|
| `raw` (default) | `video/x-raw` on a plain `shmsink` — any GStreamer pipeline | arrival (`system`), frame ids minted by the core — the USB/RTSP posture | PINNED in `shm:` (shm carries bytes, no caps) |
| `header` | `application/x-cam-frame` — the transport header + pixels | the header's timestamp, frame id and provenance | PINNED in `shm:` and CHECKED per frame |

Rules the writer follows:

- Put the socket in the camera instance's `cam_<name>_sock` volume (`shm.socket_path`, default
  `/tmp/cam/in` — not one of the core's own endpoints) and run with `ipc: host`. Create the volume
  if it does not exist (`docker volume create cam_<name>_sock`); whichever side starts first may.
- `shmsink wait-for-connection=false` (the core may not be up yet) and a `shm-size` that holds a few
  frames. `sync=true` when the frames come from a real clock, `false` for an as-fast-as-possible render.
- Raw: exactly the pinned caps, or the core stops: a frame of the wrong size is a CONFIG mismatch
  (`SourceConfigChanged`), logged once with both sizes, and the process exits for a restart with the
  right `shm:` block — a reopen cannot fix it. Header: geometry and pixel format are checked the same
  way; unparseable headers are dropped and counted (is the writer really sending
  `application/x-cam-frame`?). `ts_source` may be any value in `transport.TS_SOURCE_CODE`.

Rules the core follows:

- It waits for the socket: a missing socket is a cheap retry in the reconnect backoff loop, not a
  fault. A writer restart is a reconnect: the reader's bus ERROR or data starvation
  (`camera.reconnect_timeout_s`) flips the liveness watchdog and the reader pipeline is rebuilt.
  Minted frame ids keep counting across reconnects.
- Everything downstream is the live-camera path: the recorder (a lossless recording of the frames as
  delivered), the lifecycle, the plugin transport, the bridges, discovery (`source: shm`), replay.

A one-line writer for a bench or a smoke test:

```bash
gst-launch-1.0 videotestsrc is-live=true pattern=ball ! video/x-raw,format=RGB,width=640,height=480,framerate=10/1 \
  ! shmsink socket-path=/tmp/cam/in wait-for-connection=false shm-size=10000000 sync=true
```

run in a container with `--ipc=host -v cam_<name>_sock:/tmp/cam`, beside an instance whose config
says `camera: {type: shm}` and `shm: {pixel_format: RGB, width: 640, height: 480, frame_rate: 10}`
([`config/sensors/cam_shm.yaml`](../core-driver/config/sensors/cam_shm.yaml)).
