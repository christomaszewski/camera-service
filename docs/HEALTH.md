# Service health (Zenoh)

A **system-wide, cross-language convention** for how a vehicle's services report their health —
temperatures, link state, whether data is flowing, whether recording is working — over Zenoh, to
the operator dashboard and anything else that listens. camera-service is the first producer; the
keys and the payload are deliberately generic so any service can implement the same contract.

> **This document is the source of truth, not any shared library.** Producers share *this contract*,
> not code. Each service instance publishes its own health — there is no vehicle-wide registry.

**Shape: ROS 2 diagnostics, as JSON, on a native key.** The payload is a field-for-field mirror of
`diagnostic_msgs/msg/DiagnosticArray`, so the dashboard reads plain JSON (no ROS, no CDR) while a
future zenoh→ROS relay is a mechanical translation (see [ROS relay](#ros-relay-deferred)).
Publishing real ROS messages straight onto `rmw_zenoh`'s keys was rejected for the same reason as in
[LIFECYCLE.md](LIFECYCLE.md): its key-expression, attachment and liveliness formats are unversioned
internals, and the type hash in the key makes a distro mismatch drop data silently.

## Key schema

```
fleet/<vehicle_id>/svc/<instance>/health          liveliness token + queryable → the latest snapshot
fleet/<vehicle_id>/svc/<instance>/health/state    publisher: every snapshot (the producer's poll interval)
```

- `vehicle_id` / `instance` are the same single segments as the lifecycle keys.
- The **token** says "this instance publishes health" (a capability advert — a service may have
  health without lifecycle, or the reverse). It disappears when the process dies; no heartbeat.
- The **queryable** replies the latest snapshot **on its concrete key**, so a fleet snapshot is one
  query: `get("fleet/*/svc/*/health")`, one reply per instance.
- The **publisher** puts every snapshot (camera-service: every `health.interval_s`, default 1 s —
  the usual ROS `/diagnostics` rate).

## Snapshot

UTF-8 **JSON**, `application/json`, strict (no `NaN`/`Infinity` — a browser's `JSON.parse` rejects them).

```jsonc
{
  "schema_version": 1,
  "service": "camera-service",            // which stack
  "instance": "cam_thermal",              // == the <instance> key segment
  "stamp_unix_ns": 1759200000000000000,   // wall clock, ns   -> DiagnosticArray.header.stamp
  "level": 1,                             // worst status[].level (a convenience; not in the ROS msg)
  "status": [                             // -> DiagnosticArray.status[]
    {
      "level": 0,                         // -> DiagnosticStatus.level
      "name": "cam_thermal: camera",      // -> .name        "<instance>: <component>"
      "message": "41.2 C",                // -> .message     one human line
      "hardware_id": "FLIR Boson sn 123", // -> .hardware_id "" for host-side components
      "values": {                         // -> .values[]    KeyValue(key, str(value))
        "temp.sensor_c": 41.2, "uptime_s": 5231
      }
    },
    { "level": 1, "name": "cam_thermal: stream", "message": "3 frame(s) lost in the last 10s",
      "hardware_id": "", "values": { "fps.delivered": 59.9, "source_gaps": 1 } }
  ]
}
```

### Levels

The numbers **are** the `DiagnosticStatus` byte values:

| level | name | meaning |
|---|---|---|
| 0 | OK | working as intended |
| 1 | WARN | working, but something needs attention (a limit crossed, frames lost, PTP unlocked) |
| 2 | ERROR | not working (no frames, disconnected, recording failed) |
| 3 | STALE | no data: the component's source of truth stopped answering |

### Producer rules (these keep the ROS relay lossless)

1. **`values` are flat scalars** — number, string, boolean or `null`. No nested objects, no lists.
2. **`level` is 0–3.** No other levels.
3. **Every status has `level`, `name`, `message`, `hardware_id`** (`hardware_id` may be `""`).
4. **`name` is `"<instance>: <component>"`** — the `"<node>: <task>"` form ROS `diagnostic_updater`
   emits, so the aggregator's analyzers can group by prefix.
5. **Temperatures are `temp.<where>_c`, in °C.** Consumers find every temperature by that pattern
   with no knowledge of the device.
6. **`stamp_unix_ns` is wall-clock time** (what a ROS header stamp carries).
7. **`schema_version` increments on any breaking change.** Adding a component or a value is not breaking.

### Well-known value names

A consumer may alarm on or plot these without knowing the device; anything else is shown as plain
key/value.

| name | unit | meaning |
|---|---|---|
| `temp.<where>_c` | °C | a temperature: `temp.sensor_c` (imager / FPA), `temp.device_c` (body/board, unspecified), `temp.mainboard_c`, … |
| `temp.state` | string | the device's own temperature verdict, when it has one |
| `uptime_s` | s | device uptime — a decrease means the device rebooted |
| `supply.voltage_v` / `supply.current_a` | V / A | device power supply (PoE cameras) |
| `link.speed_mbps` | Mbit/s | negotiated link speed |
| `ptp.state` / `ptp.offset_ns` | string / ns | IEEE 1588 port state / offset from master |
| `fps.delivered` / `fps.expected` | Hz | measured over the last interval / configured |
| `frame_age_s` | s | time since the last frame |
| `disk.free_gb` / `disk.free_pct` | GB / % | free space where recordings land |

## Consumer recipe

```python
# fleet snapshot (+ presence)
for token in session.liveliness().get("fleet/*/svc/*/health"):
    snap = json.loads(session.get(token.key_expr).next().ok.payload.to_bytes())

# live
session.liveliness().declare_subscriber("fleet/*/svc/*/health", on_presence, history=True)
session.declare_subscriber("fleet/*/svc/*/health/state", on_snapshot)
```

- A snapshot older than ~3 publish intervals means the producer is hung, even while its token
  stands; the token's disappearance means it is gone.
- Show `level` / `message` per status; show every `temp.*_c`; show the rest as key/value.

## ROS relay (deferred)

Not built: the dashboard talks to the Zenoh router directly and nothing needs `/diagnostics` yet.
When something does (`rqt_robot_monitor`, `diagnostic_aggregator`, a ROS node reacting to health,
health inside rosbags), a single generic relay per vehicle subscribes `fleet/<vehicle_id>/svc/*/health/state`
and publishes, with no change to any producer:

- `/diagnostics` — `diagnostic_msgs/DiagnosticArray`: `header.stamp` ← `stamp_unix_ns`; each
  `status` entry → `DiagnosticStatus` field for field; each value → `KeyValue(key, str(value))`.
- `/<instance>/temperature/<where>` — `sensor_msgs/Temperature` for every `temp.<where>_c`
  (`temperature` = the value, `variance` = 0 = unknown). Diagnostics values are strings in ROS, so
  this is the typed stream for plotting.

ROS 1's `diagnostic_msgs` has the same structure; a ROS 1 relay is the same code.

## camera-service (producer)

Implemented in `core-driver/cam_driver/health.py` (+ `health_genicam.py`), published on the
control plane's Zenoh session (`control_zenoh.py`). Health is **observational only**: nothing in it
ever stops capture, changes the lifecycle state, or refuses a recording.

### Components

| component | always? | from | levels |
|---|---|---|---|
| `stream` | yes | the pipeline's counters (every source type) | ERROR: reconnecting, or no frame for `stall_after_s` (or 3 frame intervals, whichever is longer; a never-started source says "no frames yet"). WARN: frames lost (`source_gaps`/`enqueue_failures` moved) within the last 10 s. OK otherwise, incl. a paused/finished playback. |
| `recording` | yes | the lifecycle descriptor + the output filesystem | ERROR: the open session reports an error. WARN: `last_error` is set (a session ended on an error, a refused transition). OK: `disabled` / `inactive` / `recording`. |
| `camera` | when a device provider applies | the camera itself | the provider's own verdicts (below) + limits |

Then the **limits** apply to every component's values (`health.limits`). The only built-in limit is
`disk.free_pct` (WARN < 10, ERROR < 2); temperature limits are per camera model and never default.

### Providers

| provider | when | reports |
|---|---|---|
| `pipeline` | always | `stream`, `recording` |
| `genicam` | `auto`: the source is GenICam-backed (`camera.type: gige`) | `camera`: `DeviceTemperature` (one `temp.<entry>_c` per `DeviceTemperatureSelector` entry, else `temp.device_c`), `DeviceUptime`, `PowerSupplyVoltage/Current`, `GevLinkSpeed`/`DeviceLinkSpeed`, `PtpStatus`/`GevIEEE1588Status` (+ `PtpOffsetFromMaster`, latched with `PtpDataSetLatch` when the camera has it), Basler's `TemperatureState`. Only features the camera has are read. WARN when PTP was configured (`timestamp_source: ptp_chunk` + `ptp_enable`) and the port is not `Slave`/`Master`; WARN/ERROR on `TemperatureState` `Critical`/`Error`. Plus Aravis's host-side stream counters as `aravis.*` on `stream`. Not polled while the pipeline reconnects (STALE "camera reconnecting"). |
| `boson` | `auto`: a real usb source with `usb.control_protocol: flir-boson` | `camera`, over the Boson's serial **command channel** (the USB CDC-ACM port beside the UVC video — independent interfaces, so it polls while the video streams): `temp.sensor_c` (FPA temperature), `ffc.state` (`none`/`imminent`/`in_progress`/`complete` — said in the message while an FFC freezes the image, level stays OK: it is normal operation), `frame_count`, `ffc.last_frame`, `ffc.frames_since`; `hardware_id` = "FLIR Boson <part number> sn <serial>". The port is opened lazily and reopened after an unplug; a camera that stops answering goes STALE. |

The Boson's command channel is declared on the **source**, because it is a fact about the camera (and
cam-up maps its device node into the container from there — `docker-compose.control.yml`):

```yaml
usb:
  device: /dev/v4l/by-id/usb-FLIR_Boson_<sn>-video-index0
  control_protocol: flir-boson                          # the only protocol so far
  control_device: /dev/serial/by-id/usb-FLIR_Boson_<sn>-if00
```

Flat keys, deliberately: cam-up's stdlib YAML fallback reads source blocks as flat scalars, and a nested
`device:` would clobber `usb.device`. One process per port — don't point FLIR's GUI at it while the
service runs — and if the host runs ModemManager, exclude the Boson (USB `09cb:4007`) with the udev rule
in `docker-compose.control.yml`.

A provider that throws keeps its last good values for `stale_after_s`, then its components go STALE.

### Config

```yaml
health:
  enabled: true           # default
  interval_s: 1.0         # poll + publish period
  stale_after_s: 5.0
  stall_after_s: 5.0
  write_file: true        # file: "" = <dir of transport.plugin_endpoint.socket_path>/health.json
  providers:
    genicam:
      enabled: auto       # auto | true | false
      features:           # add or rename values: <value name>: <GenICam feature>
        temp.lens_c: LensTemperature
    boson:
      enabled: auto       # auto (= on when usb.control_protocol is flir-boson) | true | false
  limits:                 # warn_above / error_above / warn_below / error_below
    temp.sensor_c: {warn_above: 65, error_above: 75}
    fps.delivered: {warn_below: 55}
```

Unknown providers, bounds and non-numeric thresholds are config errors (exit 2), naming the key.

### Outputs

- **Zenoh** — the keys above, on the control plane's session: they need `control.enabled: true`
  (the default) and are best-effort like the rest of the control plane.
- **File** — the latest snapshot, replaced atomically every poll, on the per-sensor socket volume
  every bridge already mounts (`/tmp/cam/health.json`). Removed on a clean stop; a file left by a
  crash is recognizable by its `stamp_unix_ns`.
- **Recording sessions** — each session's JSON gets a `health` object: the samples taken while it
  was open, per status the worst level/message seen, and `first`/`last`/`min`/`max` of every numeric
  value (last only, for strings) — the conditions the recording was made in.
- **Log** — one line per status level change (quiet while everything stays OK).

The lifecycle descriptor's `health` field ([LIFECYCLE.md](LIFECYCLE.md)) predates this and still
carries the raw drop counters; this contract is the full picture.
