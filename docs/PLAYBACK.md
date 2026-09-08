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
  attaching late still gets a picture; the recorder, if active, simply receives nothing; a
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

Recording and playback are independent controls on one process: pausing playback while a session
is `active` simply delivers no frames (the session stays open, its clock stands still — no gap is
recorded, because nothing was played); changing speed changes how fast frames arrive at the
recorder (the recorder writes the data's timestamps, so the FILE is unaffected — a `speed: 4`
replay yields the same recording as `speed: 1`, four times sooner; `speed: 0` is the batch
reprocess). `restart` mid-session records the data again from its start into the same session
(the sidecar's monotonic PTS keeps the join exact). `finished` finalizes an open session; a
re-recorded replay's sidecar header carries `replay_of` (the session prefixes it was played from)
and `replay_epoch_unix_ns`, so the new run links back to its origin.

## Zenoh session (producer side)

The playback keys ride the **same** peer session as the lifecycle keys (one session per process,
LIFECYCLE.md "Zenoh session"), declared in the same `on_playing` moment, queryables and publisher
before the token. Best-effort and retried like the lifecycle; a missing binding leaves playback
config-driven, exactly as it was before this contract.

## Producers

| Producer | Source kinds | Status |
|---|---|---|
| camera-service (core) | `pcap`, `replay` | **implemented** — `cam_driver/playback.py` (policy + pacer), `cam_driver/control_zenoh.py` (`PlaybackControl`), declared only for finite sources |
