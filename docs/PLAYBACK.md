# Playback control (Zenoh)

A **system-wide, cross-language convention** for controlling a service whose source is a
**recording being played back** — pause it, change its speed, restart it, loop it — over Zenoh, the
same wire the lifecycle ([LIFECYCLE.md](LIFECYCLE.md)) and media discovery
([DISCOVERY.md](DISCOVERY.md)) ride. camera-service is the first producer (its `pcap` and `replay`
sources); the keys are generic so any service that plays data back can implement them.

> **This document is the source of truth, not any shared library.** Producers self-advertise; there
> is no registry.

## Playback is a capability, not a label

A media descriptor may say `"source": "pcap"` (DISCOVERY.md). That is a **label**. The ability to
*control* playback is advertised **separately and only by a producer that actually has playback
to control** — a finite source. A live camera (`gige`/`usb`/`rtsp`) never declares these keys.
A consumer shows playback controls **iff the `…/playback` liveliness token is live**, never by
inference from the source kind. Recording control is orthogonal: a playback feed has a lifecycle
too (the recorder records what is played back), and the two keyspaces stand side by side:

```
fleet/<vehicle_id>/svc/<instance>/lifecycle   the recorder    (LIFECYCLE.md)
fleet/<vehicle_id>/svc/<instance>/playback    the source      (this document)
```

## Key schema

```
fleet/<vehicle_id>/svc/<instance>/playback            liveliness token + queryable → descriptor
fleet/<vehicle_id>/svc/<instance>/playback/control    queryable: a request → reply
fleet/<vehicle_id>/svc/<instance>/playback/state      publisher: the descriptor on every change,
                                                       and at ~1 Hz while playing (position)
```

`vehicle_id` and `instance` are single segments and the SAME segments the instance's media and
lifecycle keys use. A consumer watches `fleet/*/svc/*/playback`.

| Zenoh primitive | Key | Role |
|---|---|---|
| **Liveliness token** | `…/playback` | presence — this instance's source is controllable playback |
| **Queryable** | `…/playback` | replies the descriptor |
| **Queryable** | `…/playback/control` | runs a request; replies when it has **taken effect** |
| **Publisher** | `…/playback/state` | the descriptor after every change + ~1 Hz while `playing` |

Liveliness semantics are LIFECYCLE.md's: declare once the source is delivering and ready to accept
requests, undeclare on graceful shutdown, nothing on a crash (Zenoh withdraws it).

## State descriptor

UTF-8 JSON, `application/json`. Only `schema_version`, `service`, `instance`, `source`, `state`,
`controls` are **required**.

```jsonc
{
  "schema_version": 1,
  "service": "camera-service",
  "instance": "playback",          // == the key's <instance>
  "source": "pcap",                // pcap | replay | … — what is being played back
  "state": "playing",              // playing | paused | finished
  "controls": ["pause", "set_speed", "restart", "set_loop"],   // what `control` accepts RIGHT NOW
  "speed": 1.0,                    // pacing multiplier; 0 = as fast as the pipeline drains
  "loop": true,                    // restart at end-of-data instead of finishing
  "cycle": 3,                      // 0-based loop cycle (how many times the data has restarted)
  "position_s": 12.4,              // seconds into the current cycle, by the data's own timestamps
  "duration_s": 4.0,               // one cycle's length; null when not known up front
  "frames": 310,                   // frames delivered in the current cycle
  "since_unix_s": 1756700000.0,    // when the current state was entered
  "last_error": null,
  "source_path": "/data/runs/20260907T101500Z_survey/recordings/cam_front/cam-20260907-101612",
                                   // what is being played RIGHT NOW (a replay: the session prefix)
  "session": 1,                    // 0-based index of that session in the run, and
  "sessions": 3,                   //   how many the run holds (a replay plays them in order)
  "epoch_unix_ns": 1757240100000000000   // the timeline zero position_s counts from (see Timeline)
}
```

- `state` — `playing` (data flowing, paced by `speed`), `paused` (held; consumers keep the last
  frame — camera-service re-publishes the last frame to the plugin transport at ~1 Hz whenever
  nothing else reached it for a second, held or playing through silence, so a bridge or viewer
  attaching late still gets a picture. Each re-publish carries a FRESH transport PTS (the wire
  never repeats a timestamp: on unixfd a repeated PTS is a repeated RTP timestamp, which a browser
  drops as a duplicate — a viewer's stall watchdog then cycles the session). Playback transport
  PTS follows elapsed monotonic wall time for both real and held frames: a historical gap already
  filled by held frames is not counted again when data resumes, and changing playback speed does
  not change the RTP clock rate. The recording feed keeps source PTS and the plugin metadata keeps
  original capture timestamps throughout. The recorder, if active, simply receives nothing; a
  playback that BOOTS paused lets its first frame out, then holds), `finished` (a non-looping source
  reached its end; any open recording session is finalized). What happens next is the producer's
  `on_finish` policy: **hold** — the process stays up, the keys stay declared, consumers keep the
  last frame and `restart` is accepted (the orchestrated shape: a run brought up by rig must not
  exit-and-restart under compose) — or **exit** (the bare tool: the descriptor is published once
  before the token is withdrawn).
- `controls` lists what the producer will honour from the current state: `pause` while playing,
  `resume` while paused, and `set_speed` / `set_loop` / `restart` in either; while `finished`,
  `restart` alone for a producer that can start over (camera-service `replay`), nothing for one
  that cannot (`pcap`). Consumers render exactly these — never an assumed state machine.
- Timing: `position_s` advances by the data's timestamps scaled by `speed`, not by wall time, so
  it is honest at any speed and stands still while paused. It counts from `epoch_unix_ns`.

## `control` request / reply

Request — a JSON object on the `get` (or selector parameters for a payload-less get:
`…/control?op=set_speed;speed=2`):

```jsonc
{ "op": "pause" }
{ "op": "resume" }
{ "op": "set_speed", "speed": 2.0 }      // ≥ 0; 0 = as fast as the pipeline drains
{ "op": "set_loop",  "loop": false }
{ "op": "restart" }                       // back to the start of the data, same cycle counter +1,
                                          //   stays playing/paused as it was
```

Reply:

```jsonc
{ "ok": true,                      // the request took effect (or was already satisfied: see noop)
  "state": "paused",               // the state AFTER the request
  "noop": true,                    // present iff nothing had to change (pause while paused, …)
  "error": "…",                    // present iff ok is false
  "descriptor": { … } }            // the full descriptor above
```

Requests are **idempotent** (`pause` while paused is `ok: true, noop: true`). A request that cannot
be honoured — an unknown `op`, a negative speed, an `op` not in `controls` (e.g. anything while
`finished`) — is an `ok: false` reply with `error`, never a Zenoh-level error. The reply is sent
once the request has taken effect on the feeder (sub-second); a query timeout of 5 s is plenty.

## Timeline: several producers, one zero

A replay of a *run* has several producers (cameras, a bag player) that must sit at the same place
on one timeline. The producer-side knobs (camera-service `playback:` config block; an orchestrator
sets them per instance — rig renders them into the replay config):

| Knob | Meaning |
|---|---|
| `initial_state: playing \| paused` | come up held on the first frame |
| `start_at_unix_s` | the release gate: resume itself at this wall instant — every producer of the replay gets the same one, so they start together instead of at their own container start |
| `epoch_unix_ns` | the timeline ZERO (the bag's start): frame k is released `(ts_k − epoch)/speed` after release, wherever the first recorded frame sits — a session recorded ten minutes into the run comes out ten minutes after release, where the bag is. Frames before the epoch are skipped. Unset = the data's own first frame. |
| `from_s`, `to_s` | a window on that timeline (skip before, `finished` at) |
| `on_finish: hold \| exit \| auto` | see `state` above; `auto` = hold iff the control plane is enabled |

A camera-service `replay` plays **every session** the run holds for the instance, in timeline
order, with the recorded gaps between them honoured as silence (`session`/`sessions`/`source_path`
in the descriptor say where it is). `restart` goes back to the first session inside the window.

## Interaction with the recorder (camera-service)

MKV replay opts into a buffer-based frame path: untiled decoded images retain GStreamer memory
through raw output, recording, and the headered-shm transport instead of extracting the full image
into Python bytes and wrapping it again. Each outgoing buffer has independent PTS/frame metadata.
On GStreamer 1.28+, replay offers the raw decoder/converter a reusable shared-memory pool. The
resulting FD-backed buffers pass through unixfd without another pixel copy. If upstream declines
the allocator, unixfdsink copies into its native pool. Older unixfd versions retain the explicit
memfd copy, after the publication rate/queue checks. Feature detection selects these paths.
CFA un-tiling uses the byte path; recording-side CFA tiling materializes bytes only for that transform.
Live GigE/USB/RTSP and pcap sources retain their existing byte callback interface.

When neither local preview nor the raw endpoint is enabled, replay leaves the main appsrc idle;
the plugin transport and recorder continue independently. This does not skip decoding or change
recording rate, source timestamps, pause/restart behavior, or the shared-memory wire format.

`core-driver/tests/test_replay_buffers.py` checks memory lifetime, exact recording roundtrips,
rate-limited streaming, raw output, and held frames on both shm and unixfd. For CPU comparisons,
run `python3 tools/replay_buffer_benchmark.py <session-prefix> --seconds 10` inside the dev image
with the input mounted read-only, using the same image and input for each checkout. It reports
process CPU time (including its lightweight transport consumer), wall time, and drops; it does
not include ROS or WebRTC encoding. No recording files are written.

Recording and playback are independent controls on one process: pausing playback while a session
is `active` simply delivers no frames (the session stays open, its clock stands still — no gap is
recorded, because nothing was played); changing speed changes how fast frames arrive at the
recorder (the recorder writes the data's timestamps, so the FILE is unaffected — a `speed: 4`
replay yields the same recording as `speed: 1`, four times sooner; `speed: 0` is the batch
reprocess). `restart` mid-session records the data again from its start into the same session
(the sidecar's monotonic PTS keeps the join exact). `finished` finalizes an open session; a
re-recorded replay's sidecar header carries `replay_of` (the session prefixes it was played from)
and `replay_epoch_unix_ns`, so the new run links back to its origin.

### Pinned GStreamer 1.28.7 on a development machine

The `dev` platform defaults to Ubuntu 26.04 with GStreamer 1.28.7 in both the core and WebRTC
images. Matching core, base/good/bad/ugly plugins, libav and RTSP server are built from checksummed
upstream tarballs into `/opt/gstreamer`; media elements and Python typelibs are verified during
build. Aravis, libnice and gst-plugins-rs 0.15.3 remain available. Both bridges default to unixfd.
ROS packages and Jetson platform defaults are unchanged.

Build locally from the repo root (this builds every dev image, including ROS topic input):

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
  --profile webrtc-bridge --profile ros2-bridge --profile ros2-source build
./cam-up --dev config/sensors/cam_b.yaml up -d
```

For a registry deployment, `tools/build-images.sh registry.lan:5000 dev` builds and publishes
the same defaults. Versioned tags such as `v1.4.0-dev` work through the usual rig build matrix.
`--dev`, `CAM_PLATFORM=dev` and rig's `platform: dev` all select this stack; remove old
`CAM_DEV_IMAGE`, `CAM_WEBRTC_IMAGE`, `CAM_ROS2_IMAGE` and `CAM_TRANSPORT` overrides to use it.
On Docker Desktop, keep `CAM_NETWORK=host` if required for WebRTC media and the host ROS router.
Rebuild/pull and recreate the containers to apply the new images to an existing deployment.

Compatibility builds remain available: core `--target distro --build-arg BASE=ubuntu:22.04`
(or 24.04); WebRTC `--target runtime` with its default Ubuntu 24.04 base. Compose and the build
script accept `CAM_DEV_TARGET`, `CAM_DEV_BASE`, `CAM_WEBRTC_TARGET`, `CAM_WEBRTC_BASE` and
`CAM_GST_RS_TAG` overrides. Pin `CAM_DEV_IMAGE` / `CAM_WEBRTC_IMAGE` / `CAM_ROS2_IMAGE` to select
existing images. For a 1.20 core, also set `CAM_TRANSPORT=shm` so both bridges use headered shm.
The standalone `tools/gstreamer/Dockerfile` overlay remains available for Ubuntu 26.04 images.

Validation on ARM64 Docker (2026-09-11): all 26 standalone core test scripts passed on
GStreamer 1.20.3, 1.28.2 and 1.28.7. The 1.28.7 image also passed synthetic GigE/USB/RTSP ingest,
recording and transport, exact replay roundtrips, headless WebRTC (raw shm and unixfd/Bayer,
H.264 and downscaling), and delivery to the existing ROS 2 bridge. Physical sensor/Jetson hardware
was not part of this development validation.

A repeated 640x480 NV12/FFV1 replay benchmark at 1x, with recording off and a lightweight unixfd
consumer, used 2.514 CPU seconds before and 2.354 after per 373 frames on average (about 6.4% less).
Two 15-second windows per path, ordered before/after/after/before, delivered all 1,492 frames with
zero drops. Both paths used the same 1.28.7 image; this isolates shared allocation/pooling from
other version changes. Use `--legacy-transport-copy` with the benchmark tool for that baseline.
This is core-process work including the test consumer, not a whole-dashboard CPU measurement.

## Zenoh session (producer side)

The playback keys ride the **same** peer session as the lifecycle keys (one session per process,
LIFECYCLE.md "Zenoh session"), declared in the same `on_playing` moment, queryables and publisher
before the token. Best-effort and retried like the lifecycle; a missing binding leaves playback
config-driven, exactly as it was before this contract.

## Producers

| Producer | Source kinds | Status |
|---|---|---|
| camera-service (core) | `pcap`, `replay` | **implemented** — `cam_driver/playback.py` (policy + pacer), `cam_driver/control_zenoh.py` (`PlaybackControl`), declared only for finite sources |
