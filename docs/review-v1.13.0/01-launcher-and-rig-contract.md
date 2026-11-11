# 01 — Launcher, descriptor and release process against the rig service contract

Tree under review: camera-service `v1.13.0` (`6d3af8b`). Contract: `rig/docs/SERVICE_CONTRACT.md` as of rig
`v0.2.84` (`df67c97`); requirement IDs below (O5, D5, C8 …) are that document's.
Scope: `cam-up`, `rigging.yaml`, `tools/sensor_env.py`, `tools/runtime_images.py`, the compose files and
overlays, the two GitHub workflows, and how releases are cut.

How the evidence was gathered:

- `rig certify` was run from a rig checkout against the release tree (no Docker daemon was available, so
  the `status` check was skipped; the build check ran under bubblewrap).
- Launcher behaviour was traced with a logging stand-in for `docker` first on `PATH` that records every
  invocation and passes `compose … config` / `compose version` through to the real client. Nothing was started.
- Everything else is by reading, with the line cited.

Findings: 0 critical, 0 high, 4 medium, 8 low, plus 3 release-process findings (1 high, 2 medium).

---

## 1. What is sound

- **`rig certify --require-build-check` passes with 0 errors for all seven example configs**
  (`camera.example`, `cam_usb`, `cam_rtsp`, `cam_gige`, `cam_thermal`, `cam_shm`, `cam_ros2`): `discipline`,
  `project-name`, `registry`, `tag`, `image-overrides`, `ros-env`, `volumes`, `determinism`, `identity`,
  `fixed-names`, `platform` and `build-no-cache` are all green. The only warning is the expected `ros-env`
  note on `cam_shm` (no ROS node in that config).
- **The vendored copy certifies too.** Copying only `launch_surface` + `rigging.yaml` + the examples into an
  empty directory and certifying that copy gives 0 errors (the build check is correctly reported as not
  applicable), and `tools/export-recordings`' imports resolve from the copy. `launch_surface` is complete for
  every verb rig calls.
- **Run-registry handling (D1, D2)** is careful: `current` is resolved once at process start, a link that
  escapes the data root or dangles is refused with a warning, and the whole data root is mounted
  (`core-driver/cam_driver/config.py:649-710`, `docker-compose.yml:52`).
- **`config` is a pure render** (O4, O5): platform detection, the `/dev/shm` probe and `docker volume create`
  are all skipped for it (`cam-up:94`, `:128`, `:264`).
- **Pinned `up`** (P4) recomputes the image set, requires it to equal the pinned keys, drops `build:`, sets
  `pull_policy: never` and restricts the accepted `up` flags (`tools/runtime_images.py:52-84`, `:120-131`).
- **Shared ROS base (M5):** `ros2-bridge` builds `FROM ${BASE_IMAGE}` (rig's `RIG_BASE_IMAGE`) with
  `apt-get install --no-upgrade` in both stages.
- **Image labels (R9, R11):** `io.rig.camera.replay-bindings` and `io.rig.replay.coordination` are set on
  `cam-core` and `cam-dev` (`core-driver/Dockerfile:68-69`, `core-driver/Dockerfile.dev:10-11`).

---

## 2. Release process

### RP-1 — high — the last three releases were cut from a branch that is not `main`

- `v1.11.0` (`f36f633`), `v1.12.0` (`6316f7a`) and `v1.13.0` (`6d3af8b`) are tagged on
  `codex/gstreamer-1.28.8`. `main` is still `v1.10.0` (`ec11fbc`), 18 commits behind, and there is no pull
  request from the release branch to `main`. `main` is an ancestor of `v1.13.0`, so a fast-forward is possible
  today.
- `docs/TESTING.md:80-81` says of this branch: "Keep this branch separate until the intended Jetson platforms
  pass the hardware checks below." Three releases have shipped from it; the document records no result for
  those hardware checks.
- CI's push trigger is `branches: [main, jp6-modern]` (`.github/workflows/certify.yml:5`); `jp6-modern` no
  longer exists and the release branch is not listed. From the Actions history: CI ran on four of the 18
  release-branch commits, all by manual `workflow_dispatch` — `cf3fe39` (failed), then `f36f633`, `6316f7a`
  and `6d3af8b` (green, each minutes before its tag). The other 14 commits were never built or tested as
  pushed.
- Consequences: the default branch lacks released fixes (`9610e54` bit-exact mono/Bayer, `4a9daa6` last FFV1
  frame, `789f013` split timestamps, the replay fixes); every pull request is developed and CI-tested against
  a base three releases old (that is how BR-1 in `07-branches-and-open-prs.md` arose); and any merge into
  `main` now ends the fast-forward.
- **Recommendation:** fast-forward `main` to `v1.13.0` before merging anything else, then release only from
  `main` (or name a release branch pattern and add it, plus `tags: ['v*']`, to the push trigger).

### RP-2 — medium — `registry-release.yml` publishes to the fleet registry with no dependency on CI

`on: release: published` runs one job that writes `source.rev: ${GITHUB_SHA}` into the public registry and
pushes (`.github/workflows/registry-release.yml:26-31`, `:95-133`). It has no `needs:` and does not look at the
commit's check runs, so a release tagged on a red or untested commit is published to every consumer. The
`workflow_dispatch` path publishes "the current commit of the chosen ref under the version you type". Also in
this job: `pip install git+https://github.com/christomaszewski/rig` at an unpinned ref while the job holds the
registry write token, and `ver='${{ inputs.version }}'` interpolates a dispatch input straight into the shell
script (limited to users who can dispatch workflows). The manifest hard-codes `platforms:` and `ros_distro:`
instead of deriving them from `rigging.yaml`.

**Recommendation:** fail the job unless the `ci` workflow concluded `success` for `GITHUB_SHA`; pin the rig
install; pass the input through `env:`.

### RP-3 — medium — the `certify` gate covers two of seven examples against an unpinned rig

`certify.yml` certifies `cam_usb.yaml` and `cam_rtsp.yaml` only. `examples[0]` (`camera.example.yaml`, the
config rig seeds every new instance from) is not certified in CI, nor are the Boson control-device overlay,
the shm-input config or the `ros2-source` profile. No example enables `ros1-bridge`, so certify reports that
image as "declared but not pulled with this config, so not checked" for every example: its registry/tag
wiring has never been examined. The rig checkout has no `ref:`, so the result depends on whatever rig's
default branch is at run time.

**Recommendation:** a matrix over all seven examples plus one config that enables `ros1-bridge`, against a
pinned rig ref.

---

## 3. Launcher and descriptor findings

### LC-1 — medium — every verb except `config` creates the socket volume; `--zenohd`/`--roscore` start helpers on `down`

- **Where:** `cam-up:263-269`
  ```sh
  if [ "$VERB" != config ]; then
    docker volume create "$CAM_SOCK_VOLUME" >/dev/null
    if [ "$ZENOHD" = 1 ]; then … "$REPO/tools/zenohd.sh" up; fi
    if [ "$ROSCORE" = 1 ]; then … "$REPO/tools/roscore.sh" up; fi
  fi
  ```
- **Contract:** O5 ("Any setup the launcher performs is gated on the verb being `up`"), L5 (`down` removes the
  project's containers "and nothing else"), D5 (rig removes declared external volumes on `rig down --purge` and
  `rig cleanup`).
- **Observed** (docker trace, `cam_usb.yaml`): `ps --format json`, `logs --tail 5`, `down` and `pull` each run
  `docker volume create cam_cam_usb_sock` before the compose command. `config`, `images`, `runtime-images` and
  `replay-capabilities` do not. `cam-up --zenohd <cfg> down` ran `docker rm -f cam-zenohd` followed by
  `docker run -d --name cam-zenohd …` — a teardown that starts the shared router.
- **Consequence:** after `rig down --purge` or `rig cleanup` removes `cam_<name>_sock`, the next `rig status`
  or `rig logs` silently re-creates it, so purged instances leave volumes behind indefinitely.
- **Tests:** none; the launcher tests cover `config`, `images`, `runtime-images` and pinned `up` only.
- **Fix:** gate the block on `[ "$VERB" = up ]`.

### LC-2 — medium — UPPERCASE plugin params can override values rig owns

- **Where:** `tools/sensor_env.py:42-49` (a deny-list of reserved keys), `:411-418` (pass-through of any
  UPPERCASE param), `cam-up:75` (the result is `eval`-exported into the launcher's own environment before
  compose runs).
- **Contract:** C8 (refuse config-supplied environment that sets `COMPOSE_PROJECT_NAME` or any `RIG_*`
  variable), E1/E2 (pass `ROS_DOMAIN_ID` and `RMW_IMPLEMENTATION` unchanged; never hard-code a value rig owns),
  P1 (when an `images.override_env` variable is set, that value is the complete image reference).
- **Observed:** with rig's environment set to `ROS_DOMAIN_ID=7`, `RMW_IMPLEMENTATION=rmw_zenoh_cpp`,
  `CAM_CORE_IMAGE`/`CAM_ROS2_IMAGE` digest pins and `VEHICLE_ID=42`, a config whose `ros2-bridge` params contain
  `ROS_DOMAIN_ID: 99`, `RMW_IMPLEMENTATION: rmw_fastrtps_cpp`, `CAM_CORE_IMAGE: …`, `CAM_ROS2_IMAGE: …`,
  `VEHICLE_ID: spoof` rendered a compose document with domain `"99"`, `rmw_fastrtps_cpp`, the config's images
  instead of the pins, and `VEHICLE_ID: spoof` in the core and WebRTC containers. `DOCKER_HOST` and
  `COMPOSE_FILE` given the same way are exported into `cam-up`'s environment (so `DOCKER_HOST` redirects where
  the launcher's own `docker` calls go). Only the names in `_RESERVED_KEYS` and the `RIG_` prefix are refused.
- **Why it matters:** the config already controls devices and bind mounts, so this is not a privilege
  boundary. It is a way for one sensor's YAML to put that sensor on a different ROS domain or RMW than the
  fleet, or to defeat rig's digest pinning, with nothing in `rig certify` noticing (certify only sees the
  example configs).
- **Tests:** `core-driver/tests/test_sensor_env.py` covers the reserved keys that exist; none of the names above.
- **Fix:** make the pass-through an allow-list by prefix (`CAM_WEBRTC_*`, `GST_*`, `ZENOH_*`, `RMW_ZENOH_*`,
  `VIDEO_CAPS`, …) or extend the refusal to `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `VEHICLE_ID`, every
  `images.override_env` name, `CAM_REGISTRY`, `CAM_IMAGE_TAG`, `CAM_TRANSPORT`, `DOCKER_*`, `COMPOSE_*`,
  `BASH_ENV`, `LD_AUDIT`.

### LC-3 — medium (missing capability) — no operational-state verbs although the service has the lifecycle

- **Where:** `rigging.yaml:6-14` declares no `standby`, `activate` or `state`; `cam-up` implements none and
  never reads `RIG_TARGET_STATE` (no occurrence in `cam-up`, `tools/sensor_env.py`, `rigging.yaml` or
  `docs/LIFECYCLE.md`).
- **Contract:** section 15 (T1–T6). An undeclared service "is always active" as far as rig knows.
- **Consequence:** the service implements exactly the standby/active model the contract describes (Zenoh
  `change_state`, SIGUSR1/SIGUSR2, `control.initial_state`), but rig cannot drive or display it: `rig standby`
  and `rig activate` skip cameras, the `OP` column is empty, and `rig up --standby` has no effect — the camera
  starts in whatever `control.initial_state` says, which defaults to recording.
- **Fix:** add the three verbs. `state` can read the lifecycle descriptor (or `lifecycle.state` on the socket
  volume) and must answer `down` for a stopped project; `standby`/`activate` can send SIGUSR2/SIGUSR1 to the
  core container (or use the Zenoh call) with a bounded wait; on `up`, map `RIG_TARGET_STATE` onto the initial
  state ahead of the config value (T4).

### LC-4 — medium — the healthcheck hard-codes default socket paths and says nothing about the camera

- **Where:** `docker-compose.yml:69-76`
  `test -S /tmp/cam/frames || test -S /tmp/cam/unixfd || test -S /tmp/cam/raw`.
- **Contract:** H1/H2 (probe readiness: "process up, configured, device or backend reachable"); `rig sim up`
  requires no container to be unhealthy (H4).
- **What is wrong:** `transport.plugin_endpoint.socket_path` and both endpoints' `enabled` are configurable
  (`config.py:262-278`). A record-only instance (both endpoints disabled) or one with a custom socket path can
  never pass, so it reads `unhealthy` forever. In the other direction the probe passes as soon as the socket
  exists, whether or not the camera is connected. The service already writes a health snapshot to the same
  volume (`health.json`, `config.py:619-627`) that knows the stream and camera state.
- No bridge container has a healthcheck at all, which is why a wedged ROS 2 bridge (PB-1 in
  `05-plugins-and-image-builds.md`) stays invisible to `rig status`.
- **Fix:** probe the freshness and level of `health.json` (or a heartbeat file the core touches) instead of
  socket existence; add healthchecks to the bridges.

### LC-5 — low — a multi-line value is corrupted on its way into the environment

`cam-up:75` is `eval "$(sensor_env.py … | sed 's/^/export /')"`. `sed` prefixes every *line*, so a value with a
newline gains the word `export ` at the start of each continuation line. Observed: `GST_DEBUG: "3,\nwebrtcsink:6"`
reached the container as `3,\nexport webrtcsink:6`. The quoting stays balanced, so nothing executes; the value
is just wrong. Fix: have `sensor_env.py` print `export KEY=…` itself, or reject newlines.

### LC-6 — low — a `sensor_env.py` failure surfaces as an unrelated shell error

The exit status of the command substitution in `cam-up:75` is not checked. With malformed YAML the launcher
prints the Python traceback and then dies at line 247 with `CAM_SOCK_VOLUME: unbound variable`. It does exit
non-zero, but only by accident of `set -u`. A top-level list, or `camera: usb` (a string where a mapping is
expected), produces a raw `AttributeError` traceback rather than a message naming the problem. Fix: run the
helper first, check its status, and validate that the document and its sections are mappings.

### LC-7 — low — the instance name is not validated

`tools/sensor_env.py:266` takes `name` verbatim into `CAM_INSTANCE`, `CAM_SOCK_VOLUME`, the ROS namespace and
the Zenoh key. Under rig the project name is rig's, so `cam-front` (legal for rig) or `9cam` render without
complaint and then fail at runtime in the ROS bridge only, because neither is a valid ROS name. Standalone,
`Front Cam` fails late inside Compose with "invalid project name". C3 asks a launcher that cannot use a name to
reject it on stderr with a non-zero exit. See also CH-15 (key expressions) and PB-15 (namespaces).

### LC-8 — low (latent) — `replay.sessions: []` is rejected although the contract defines it as "all"

`rigging.yaml:144` renders `sessions: "{{replay_input_sessions}}"`. The contract (section 16.5) defines an
empty list as "all sessions", and rig does pass `[]` in `rig bake --include-replay`
(`rig_cli/bake_replay.py:216`). The core refuses it: `replay.sessions: expected a nonempty list of unique exact
session prefixes`. Harmless today because that bake path only runs launcher queries (which do not load the core
config) and a live replay always passes explicit prefixes (`rig_cli/replay_sources.py:320`), but the two sides
disagree. Fix: treat `[]` like an absent key.

### LC-9 — low — a non-default `recording.output_dir` silently takes recordings out of rig's reach

A relative `output_dir` replaces the `<instance>` segment and an absolute one pins a path outside the run
(`config.py:676-710`). `rigging.yaml` tells rig the data lives at `recordings/{name}` for both `replay.source`
and `export`. An instance configured with `output_dir: front` therefore records to `recordings/front`, and rig
replay, export and run sealing do not find it; nothing warns. Fix: warn at startup when `RIG_DATA_DIR` is set
and `output_dir` is non-empty, or declare the layout so rig can follow it.

### LC-10 — low — `config` reads recording sidecars on the rendering host

`tools/sensor_env.py:77-102` (`replay_pixel_format`) opens `<replay.path>/*.json` to choose `CAM_BAYER` and
`CAM_ROS_ENCODING`. For a replay config the rendered document therefore depends on whether the recordings exist
on the machine that renders it, which is a deliberate departure from O4 ("never probes the host"). It falls
back cleanly when the files are absent, so P3 ("must not need recorded data to exist") holds. Either document
it as the one exception or move the decision into the container, where the data is always present.

### LC-11 — low — optional capabilities that are not declared

- `replay.sim_time`: nothing reads `RIG_SIM_TIME` or sets `use_sim_time`. Image stamps come from the frame, not
  a ROS clock, so replayed stamps are correct; the gaps are the publish throttle (wall time) and
  `ros2-source`'s zero-stamp fallback (see `05-plugins-and-image-builds.md`).
- `interface:`: no topics are declared, so `rig graph --check` has nothing to compare and `rig sim` would warn.
- `build.sources`: the WebRTC image build fetches six git repositories and the core image an Aravis tarball
  (PB-16, PB-17); none is declared, so `rig build --offline` cannot work for this service.
- `ros_namespace`: defaults to `/<name>`, which is what the bridges use. Fine as is.

### LC-12 — low — stale documentation

`docs/ROADMAP.md` still lists disk-full handling as "next", calls `service:` "reserved for a future fleet
launcher", inventories test scripts that CI never runs as "binding", and does not mention health telemetry,
coordinated replay or proxy export. `docs/TESTING.md:3` describes CI as running on pushes to `main` and
`jp6-modern` with three runtimes (there are four lanes, and neither branch is where releases come from).
`cam-up:9` documents a bare invocation as "foreground up" while every rig path passes `up -d`.

---

## 4. Tests this area needs

| Behaviour | Level | Notes |
|---|---|---|
| No verb other than `up` issues a mutating docker command, with and without `--zenohd`/`--roscore` | contract (fake `docker`) | fails today (LC-1) |
| A config cannot override `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `VEHICLE_ID` or an image-override variable | contract | fails today (LC-2) |
| Every example × every platform renders; two instances share no name; rename leaves no trace; output is deterministic | contract | true today; pin it in the repo rather than relying on an unpinned rig in CI |
| `rigging.yaml` replay overrides applied to each example produce a config the core accepts | contract | includes the `sessions: []` decision (LC-8) |
| Launch-surface completeness: run every query verb and `export-recordings --preflight` from a copy holding only the launch surface | contract | true today |
| Launcher refusals: missing config, non-mapping YAML, invalid name, multi-line value | contract | LC-5, LC-6, LC-7 |
| State verbs once implemented: `state` answers `down` on a stopped project; `RIG_TARGET_STATE` is validated on `up` only | contract | `rig certify` checks `state-verbs` when declared |

The detailed list, with file placement and effort, is TA-1 and TA-13 to TA-20 in `06-tests-and-ci.md`.
