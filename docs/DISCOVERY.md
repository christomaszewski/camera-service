# Fleet media discovery (Zenoh)

This is a **system-wide, cross-language convention** for how a vehicle's media producers (cameras,
recordings, the ros2-bridge, …) advertise their streams so an operator dashboard can discover **where
to connect, what the stream is, and when it appears or disappears** — over a flaky cellular link,
federated across a fleet.

> **This document is the source of truth, not any shared library.** Producers are written in different
> languages (Python core, C++ ros2-bridge, …); they don't share advertiser code, they share *this
> contract*. Each producer **self-advertises its own stream(s)** — there is no vehicle-wide registry.

Why Zenoh: the stack is Zenoh-first, and Zenoh **liveliness** gives presence/teardown for free,
federates across a fleet via key prefixes, and behaves the same in both dashboard deployments (operator
laptop / in-vehicle web app) and over a lossy WAN. (mDNS can't cross the WAN; a ROS-graph registry
re-adds ROS to the client and collapses into Zenoh keys under `rmw_zenoh` anyway.)

## Key schema

Presence **and** descriptor live at the **same key**, one per stream:

```
fleet/<vehicle_id>/media/<sensor_id>
```

- `vehicle_id` and `sensor_id` are **single segments** — no `/`. (e.g. `fleet/rover3/media/front-left`.)
- A consumer watches `fleet/*/media/*` (one vehicle: `fleet/<vehicle_id>/media/*`).

At that key a producer declares two things on **one** Zenoh session:

| Zenoh primitive | Role | Consumer uses |
|---|---|---|
| **Liveliness token** | presence — exists iff the stream is live | `liveliness().get(...)` for the current set + a **liveliness subscriber** for live add/remove |
| **Queryable** | replies the **descriptor** (JSON below) | `get(<key>)` to fetch it |

## Liveliness semantics (presence)

- **Declare** the token **only once the stream is actually flowing** (e.g. the pipeline reaches
  `PLAYING` / the encoder is ready) — *not* at process start. Presence means "you can connect now."
- **Undeclare** the token (and close the session) on **graceful shutdown**.
- On **crash / kill / link loss**, do **nothing** — Zenoh withdraws the token when the session drops.
  **No heartbeat.** (This is why the advertiser must live *inside the producer process*: the token's
  lifetime equals that process's session lifetime.)

A consumer therefore sees a `PUT` on the liveliness key when a stream appears and a `DELETE` when it
goes away — including the implicit teardown of an unplugged camera or a dropped link.

## Stream descriptor (queryable reply)

UTF-8 **JSON**, replied with `application/json` encoding. It is deliberately **abstract** (not
gige/webrtc specific) so USB / RTSP / other producers emit the same shape. Populate only fields the
producer actually knows; **omit** what it can't substantiate.

```jsonc
{
  "schema_version": 1,
  "id": "front-left",            // REQUIRED. matches the <sensor_id> key segment
  "role": "front-left",          // human label; config-supplied, default = id
  "producer": "gige-vision-service",   // REQUIRED. which stack produced this
  "protocol": "gstwebrtc-api",   // REQUIRED. signalling protocol (gstwebrtc-api | whep | ...)
  "signalling": "ws://host:8443",// REQUIRED. signalling URL the producer serves. scheme MUST match how
                                 //   it's served (ws/wss) — not hardcoded. Remote reachability
                                 //   (NAT/CGNAT/TURN) is a DEPLOYMENT concern, not part of this contract.
  "producer_id": "rover3-front-left",  // REQUIRED. selector for THIS stream on a (possibly shared)
                                 //   signalling server — equals the producer's signalling-level name.
  "codec": "h264",               // OPTIONAL hint only; WebRTC negotiates the real codec in SDP. Omit if unknown.
  "width": 2048, "height": 1536, // OPTIONAL frame geometry
  "fps": 30,                     // OPTIONAL
  "pixel_format": "GRAY8",       // OPTIONAL sensor format (GRAY8 / GRAY16_LE / bayer_rggb8 / ...)
  "ros_topic": "/front/image_raw",  // OPTIONAL cross-link to the same stream on the ROS graph
  "recording": "gige-front-*.mkv"   // OPTIONAL cross-link to its on-vehicle recording
}
```

Only `schema_version`, `id`, `producer`, `protocol`, `signalling`, `producer_id` are **required**; the
rest are best-effort. `id` MUST equal the key's `<sensor_id>` segment.

## Consumer recipe

```
# current streams (+ fetch each descriptor)
for token in session.liveliness().get("fleet/*/media/*"):
    key = token.key_expr
    descriptor = json.loads(session.get(key).next().payload)   # the queryable reply

# live changes
session.liveliness().declare_subscriber("fleet/*/media/*", on_sample, history=True)
#   sample.kind == PUT     -> a stream appeared  (then get(key) for its descriptor)
#   sample.kind == DELETE  -> a stream went away
```

## Zenoh session

- **One peer-mode session**, reused for the token and the queryable.
- **Connect** endpoint via env (`ZENOH_CONNECT`), defaulting to the vehicle's local `zenohd`
  (`tcp/localhost:7447`); set it empty to scout instead.
- `vehicle_id` via env (`VEHICLE_ID`), default = hostname; `sensor_id` from the producer's own config.

## Producers

| Producer | Status | Notes |
|---|---|---|
| webrtc-bridge | **implemented** | advertises its WebRTC stream; see [plugins/webrtc-bridge/README.md](../plugins/webrtc-bridge/README.md). Reference implementation: a generic advertiser (`tools/zenoh_advertiser.py`) + a stack-specific descriptor builder (`tools/bridge_stream.py`). |
| sensors / recordings / ros2-bridge | _future_ | each self-advertises at its own `fleet/<vehicle>/media/<id>` key, same shape. |

The reference advertiser is intentionally split into a **generic** half (session + liveliness token +
descriptor queryable + fail-safe lifecycle) and a **producer-specific** half (builds the descriptor).
The generic half is liftable, but the binding contract is *this document* — a new producer in another
language re-implements the same keys + JSON, it does not depend on that code.
