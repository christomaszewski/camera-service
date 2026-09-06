# Service lifecycle control (Zenoh)

This is a **system-wide, cross-language convention** for how a vehicle's services come up in a
**standby** state and are switched **active** (and back) by an orchestrator or the operator
dashboard — over Zenoh, so it works for ROS 2 and non-ROS services alike, over a flaky link, and
federated across a fleet. camera-service is the first producer; the keys are deliberately generic so
any service can implement the same contract.

> **This document is the source of truth, not any shared library.** Producers in different languages
> share *this contract*, not code. Each service instance **self-advertises its own lifecycle** — there
> is no vehicle-wide registry.

Why Zenoh, and why a native keyspace: the stack is Zenoh-first — the operator dashboard already speaks
plain Zenoh (`zenoh-ts` → `zenoh-plugin-remote-api`), and the ROS 2 graph rides `rmw_zenoh`. A **native
keyspace with JSON payloads** lets a non-ROS service be controlled with no ROS in the process and lets
the dashboard drive every service through one code path; impersonating an `rmw_zenoh` lifecycle node
was rejected because its key-expression, attachment and liveliness formats are unversioned internals
that have changed between releases, and a unix socket / HTTP endpoint was rejected because it reaches
neither the dashboard nor the fleet. The state and transition **names** mirror ROS 2 managed-node
lifecycle so a ROS-side proxy (an `rclpy` lifecycle node forwarding to these keys) is a mechanical
translation.

## Key schema

Presence **and** the state descriptor live at the **same key**, one per service instance:

```
fleet/<vehicle_id>/svc/<instance>/lifecycle                liveliness token + queryable → descriptor
fleet/<vehicle_id>/svc/<instance>/lifecycle/change_state   queryable: request a transition → reply
fleet/<vehicle_id>/svc/<instance>/lifecycle/state          publisher: the descriptor on every transition
```

- `vehicle_id` and `instance` are **single segments** — no `/`. For camera-service `<instance>` is the
  sensor name (`CAM_INSTANCE`), the same segment its media key uses (`fleet/<vehicle_id>/media/<instance>`,
  [DISCOVERY.md](DISCOVERY.md)).
- A consumer watches `fleet/*/svc/*/lifecycle` (one vehicle: `fleet/<vehicle_id>/svc/*/lifecycle`).
  A **fleet snapshot is one query**: `get("fleet/*/svc/*/lifecycle")`.

| Zenoh primitive | Key | Role | Consumer uses |
|---|---|---|---|
| **Liveliness token** | `…/lifecycle` | presence — the instance's control plane is up | `liveliness().get(...)` + a **liveliness subscriber** for add/remove |
| **Queryable** | `…/lifecycle` | replies the **descriptor** (= get_state) | `get(<key>)` |
| **Queryable** | `…/lifecycle/change_state` | runs a transition; replies when it **completes** | `get(<key>/change_state, payload=<request>)` |
| **Publisher** | `…/lifecycle/state` | the descriptor after every transition (commanded or not) | `declare_subscriber(<pattern>/state)` |

## States and transitions

```
inactive  ──activate──▶  active
inactive  ◀─deactivate── active
```

- `inactive` = **standby**: the service is up and serving whatever it serves in every state (for
  camera-service: the camera streams to the plugin transport / ROS / WebRTC) but its *active-only*
  work is off (the lossless recorder).
- `active` = doing that work (a recording **session** with its own run prefix + sidecar).
- `activating` / `deactivating` are transient and only ever visible in a descriptor read while a
  transition runs; transitions are serialized by the producer.
- A transition request is **idempotent**: `activate` while `active` replies `ok: true, noop: true`.
  An orchestrator re-asserting its desired state gets agreement, not an error.
- A request that cannot be honoured (`recording.enabled: false`, an unknown transition, a mechanism
  failure) is an `ok: false` **reply** with `error` — never a Zenoh-level error, never a missing reply.

## Liveliness semantics (presence)

- **Declare** the token once the service is initialised and ready to accept transitions (for
  camera-service: the pipeline is PLAYING and the boot state is entered) — *not* at process start.
- **Undeclare** it (and close the session) on **graceful shutdown**.
- On **crash / kill / link loss**, do **nothing** — Zenoh withdraws the token when the session drops.
  **No heartbeat.** (This is why the control plane lives *inside the service process*.)

## State descriptor (queryable reply, and the `/state` publication)

UTF-8 **JSON**, `application/json`. The core is generic; a service adds its own fields.

```jsonc
{
  "schema_version": 1,
  "service": "camera-service",     // REQUIRED. which stack
  "instance": "front-left",        // REQUIRED. == the <instance> key segment
  "state": "active",               // REQUIRED. inactive | activating | active | deactivating
  "transitions": ["deactivate"],   // REQUIRED. what change_state accepts RIGHT NOW
  "since_unix_s": 1756700000.0,    // when the current state was entered
  "boot_reason": "config",         // how the boot state was chosen: config | derived | resumed
  "recording_enabled": true,       // camera-service: can it ever be activated
  "health": {                      // camera-service: process-lifetime link/drop counters + stall flag
    "frames": 12345, "source_gaps": 0, "frames_missing": 0, "enqueue_failures": 0,
    "publish_drops": 0, "pts_rebases": 0, "stalled": false, "reconnecting": false },
  "recording": {                   // camera-service, present while active: the open session
    "index": 2, "prefix": "cam-20260901-120000", "output_dir": "/data/runs/42/recordings/front-left",
    "started_unix_s": 1756700000.0, "frames": 1234, "segments": 3, "skipped_awaiting_keyframe": 0,
    "encoder": "hw-hevc-lossless", "segment_seconds": 60, "error": null },
  "last_error": null               // the last refusal / session error, until the next clean transition
}
```

Only `schema_version`, `service`, `instance`, `state`, `transitions` are **required**; the rest are
service-specific. `instance` MUST equal the key's `<instance>` segment.

## `change_state` request / reply

Request — a JSON object payload on the `get`, or, for a payload-less get, selector parameters
(`…/change_state?transition=activate;run_id=leg3`):

```jsonc
{ "transition": "activate",        // REQUIRED. activate | deactivate
  "run_id": "leg3" }               // OPTIONAL. an operator label folded into this session's run
                                   //   prefix (<prefix>-<run_id>-<UTCstamp>); sanitized to [A-Za-z0-9_.-]
```

Reply:

```jsonc
{ "ok": true,                      // the transition succeeded (or was already satisfied: see noop)
  "state": "active",               // the state AFTER the request
  "noop": true,                    // present iff the state already matched
  "error": "…",                    // present iff ok is false, or a finalize reported trouble
  "session": { … },                // camera-service, on deactivate: the closed session — files,
                                   //   frames, truncated, csv/json paths
  "descriptor": { … } }            // the full descriptor above
```

The reply is sent when the transition **completes** — `activate` once the session is open
(sub-second); `deactivate` once the files are finalized (bounded by the producer's drain budget,
≤ 5 s for camera-service, ≤ 10 s worst case). Callers should use a **query timeout ≥ 15 s** on
`change_state`; Zenoh's default (10 s) is tight.

## Consumer recipe

```
# fleet snapshot (+ presence)
for token in session.liveliness().get("fleet/*/svc/*/lifecycle"):
    d = json.loads(session.get(token.key_expr).next().ok.payload.to_bytes())

# live presence + live state
session.liveliness().declare_subscriber("fleet/*/svc/*/lifecycle", on_presence, history=True)
session.declare_subscriber("fleet/*/svc/*/lifecycle/state", on_state)

# a transition
for reply in session.get(key + "/change_state", payload=b'{"transition":"activate"}',
                         encoding=Encoding.APPLICATION_JSON, timeout=15.0):
    result = json.loads(reply.ok.payload.to_bytes())
```

## Zenoh session (producer side)

- **One peer-mode session**, reused for the token, the queryables and the publisher.
- **Endpoints** to connect to: the service's own config first (camera-service `control.zenoh_connect`),
  else the `ZENOH_CONNECT` env, else the vehicle's local `zenohd` (`tcp/localhost:7447`, the
  `rmw_zenohd` rig runs); comma-separated; an **empty** string means scout only.
- **`connect/timeout_ms` finite (2000) + `connect/exit_on_failure: false`.** The stock peer default
  (`-1`) blocks `open()` until every listed endpoint answers, which would hold the service's startup
  hostage to a router. With a finite timeout the endpoints keep retrying in the background (1 s → 4 s
  backoff) and the service starts regardless.
- **Multicast scouting at the stock default (on)**, so a stock `zenohd` / `zenoh-bridge-remote-api` on
  the same segment (the dashboard's backend) is found with **no router at all**. `rmw_zenohd` disables
  scouting (`"ROS setting"`), so it is reached only through the explicit endpoint — which is why the
  default endpoint stays listed. Any order of start-up works: a router or the dashboard's backend that
  comes up an hour later gets linked within seconds.
- **Best-effort**: a missing binding, an unreachable router, a failed declare — the control plane is
  logged, abandoned and **retried on a timer**; the service's real work is never taken down. Every
  producer must also keep a control path that needs no Zenoh at all (camera-service: SIGUSR1/SIGUSR2).
- `vehicle_id` via env (`VEHICLE_ID`), default = hostname; `instance` from the producer's own config.

## Boot state and restarts (camera-service)

- `control.initial_state` (`active`, the default — record from the first frame — or `inactive`)
  picks the boot state; `recording.enabled: false` boots inactive regardless (`boot_reason: derived`).
- A **crash restart resumes the last commanded state** (`boot_reason: resumed`) — the state is kept in
  a file in the per-sensor socket volume, which outlives the container. A **deliberate stop forgets
  it**, so `down`/`up` boots from config. A session that ends on its own (a recorder error) is
  remembered as `inactive` with `last_error` set: a restart does not walk back into the fault unseen.

## Producers

| Producer | Status | Notes |
|---|---|---|
| camera-service (core) | **implemented** | `core-driver/cam_driver/control_zenoh.py` over the `Lifecycle` policy object (`lifecycle.py`); validated router-less by `core-driver/tools/lifecycle_test.sh` (the probe `tools/lifecycle_probe.py` stands in for a router). |
| other rig services | _future_ | implement the same keys + JSON; a ROS 2 managed node maps `on_activate`/`on_deactivate` onto `change_state`. |
