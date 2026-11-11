# camera-service v1.13.0 (6d3af8b) — control plane / config / health review

Reviewer scope: `core-driver/main.py`, `supervisor.py`, `cam_driver/{session,lifecycle,control_zenoh,
config,camera,health,health_boson,health_genicam}.py`. All read in full, plus `recording_settings.py`,
`dropstats.py`, `sidecar.py`, `pipeline.py`, `recorder.py`, `sources/{gige,base}.py`,
`docs/{LIFECYCLE,HEALTH,SERVICE_HEALTH,DISCOVERY}.md`, `config/sensors/camera.example.yaml`,
`docker-compose.yml`, `docker-compose.control.yml`, and the README lifecycle section.

How the evidence was gathered:

- **confirmed-by-running** = a throwaway script against the real module, in
  `exp/`
  (`e1_config.py`, `e2_lifecycle.py`, `e5_event_deadlock.py`, `e6_stderr_reentrant.py`, `e8_health.py`,
  `e9_keys.py`). Only gi-free modules could be executed.
- **confirmed-by-reading** = the code path was traced end to end; nothing was executed.
- **Not executed at all:** anything needing GStreamer, Aravis or zenoh. In the environment this pass ran in, `import gi`
  fails, there is no `zenoh` binding and no `pytest`, so the repo's test suite was not run and no
  zenoh runtime behaviour (router restart, default listen endpoints, query fan-out) was observed.
- Nothing inside `the repository root` was modified (imports ran with `-B`).

Counts: critical 0, high 3, medium 5, low 15.

---

## 1. Defects

### CH-1 — high — a stop that lands during a transition un-forgets the lifecycle state

- **Where:** `core-driver/main.py:153-159` with `core-driver/cam_driver/lifecycle.py:163-166`
  (and `256-266`).
- **What is wrong:** `_stop` is a Python signal handler, so it runs on the main thread at the next
  bytecode boundary — including in the middle of `Lifecycle.request()` running on the GLib loop. The
  handler calls `lifecycle.forget()` (unlink the state file). When it returns, the interrupted
  `request()` carries on and writes the file again:
  ```python
  # main.py:153-156
  def _stop(_signum, _frame):
      log.info("signal received, stopping")
      lifecycle.forget()      # a DELIBERATE stop: the next boot follows the config, not the last command
  # lifecycle.py:163-166
  if r.get("ok"):
      self.since_unix_s = time.time()
      self.last_error = err
      self.remember(self.state)
  ```
  The next boot is then `resumed`, not `config`. docs/LIFECYCLE.md:269-270 promises the opposite
  ("A deliberate stop forgets it, so `down`/`up` boots from config").
- **Trigger:** any stop signal while a transition is in flight. `deactivate` is the wide window:
  `pipeline.deactivate()` blocks the main thread in `finish_close()` for the EOS drain (up to
  `SESSION_DRAIN_S` = 5 s) plus the sidecar join. Concrete sequences:
  - `docker kill -s USR2 <core>` (the README's documented local control) followed by `docker stop`.
    SIGUSR2 has no reply, so nothing makes the caller wait for the drain.
  - Dashboard "stop recording" and a stack `down` issued within the drain.
  Result: state file `inactive`; after the next `up` the camera boots `inactive (resumed)` although
  the config says `initial_state: active`, and records nothing until someone notices. The mirror
  case (stop during `activate`) leaves `active` and overrides `initial_state: inactive`.
- **Confidence:** confirmed-by-running (`exp/e2_lifecycle.py`). A real SIGTERM was raised inside a stub
  pipeline's `deactivate()`, driven through the real `Lifecycle`:
  ```
  after boot           : active
  handler ran           : ['handler: lifecycle.forget()']
  state file after stop : 'inactive\n'
  next boot (config initial_state=active) resolves -> ('inactive', 'resumed')
  ```
  The real `main.py` handler was not run (needs gi); the nesting itself is standard CPython behaviour.
- **Existing tests:** none. No test in `core-driver/tests/` sends or simulates a signal;
  `test_lifecycle.py::test_remember_forget_recall` covers the two calls only in isolation.
- **Suggested fix:** do not run the stop inside the handler. Marshal it onto the loop the way
  `_transition` already does (`GLib.idle_add(_do_stop, priority=GLib.PRIORITY_HIGH)`), so it runs
  between callbacks. Independently, make `forget()` sticky: set a `_forgotten` flag that turns later
  `remember()` calls into no-ops.

### CH-2 — high — with `control.enabled: false`, a recorder error restarts into permanent standby

- **Where:** `core-driver/cam_driver/pipeline.py:1053-1070`, `lifecycle.py:203-211`, `main.py:116-119`.
- **What is wrong:** `session_error_fatal` exists so that a recorder that dies with no control plane
  exits non-zero and gets restarted ("nothing could ever re-activate a recorder that died",
  main.py:117-119). But the session-ended hook runs first and remembers `inactive`:
  ```python
  # pipeline.py:1060-1070
  result = self.deactivate(wait_eos=False)
  if self.on_session_ended is not None:
      ...self.on_session_ended(result)          # -> Lifecycle._on_uncommanded_end -> remember(INACTIVE)
  if self.session_error_fatal:
      ...
      self._fatal = True
      if self.loop:
          self.loop.quit()
  ```
  `main.py` only calls `forget()` from `_stop`, so the file survives the exit. The restarted process
  resolves `inactive / resumed`, computes `session_error_fatal = False`, and has no control plane.
  `last_error` is in memory only, so the restarted descriptor and health show a clean `inactive`.
- **Trigger:** signals-only deployment (`control.enabled: false`, `initial_state: active`), one
  recorder ERROR (momentary disk full, an NVENC failure). The container restarts, shows as Up, the
  `recording` health component reads OK "inactive", and nothing records until SIGUSR1 or a clean
  down/up.
- **Confidence:** confirmed-by-running for the lifecycle half (`exp/e2_lifecycle.py`):
  ```
  state file            : inactive (main.py never calls forget() on the fatal path)
  restarted process     : boot_state=inactive reason=resumed last_error=None
  ```
  The pipeline half (hook before `loop.quit()`) is confirmed-by-reading.
- **Existing tests:** `test_pipeline_session.py::test_session_error_is_fatal_only_when_nothing_could_reactivate`
  and `test_lifecycle.py::test_uncommanded_end_is_remembered_as_inactive` each pin one half; nothing
  tests the two together across a restart.
- **Suggested fix:** when `session_error_fatal` is set, do not remember `inactive` (or forget before
  quitting). More generally persist `last_error` with the state so a `resumed inactive` boot can say
  why. The same loss of `last_error` applies with the control plane on, after any crash or power
  cycle following a recorder error.

### CH-3 — high (design risk) — a recorder that cannot open at boot takes the live transport down

- **Where:** `main.py:145-147`, `lifecycle.py:130-135`, `pipeline.py:1126-1130`, `pipeline.py:948`.
- **What is wrong:** at runtime a recorder failure ends the session and the process keeps serving
  consumers (session.py:24-26: "a recorder that dies must not take the transport with it"). At boot
  the same failure is fatal:
  ```python
  # pipeline.py:1126-1130
  if on_playing is not None and not on_playing():
      self._fatal = True
      log.error("startup hook failed; shutting down without capturing")
      self.shutdown()
      return
  ```
  `activate()` refuses on any exception, including the audit write (`settings.write_snapshot`,
  pipeline.py:948; "a failed audit write refuses activation", recording_settings.py:55). So with the
  default `initial_state: active`, an unwritable or full recording directory, or an encoder that
  will not go PLAYING, makes the core exit 1. Compose (`restart: unless-stopped`) restarts it
  forever, and ROS/WebRTC consumers get no frames at all.
- **Trigger:** recording disk full or data mount missing or read-only at a clean `up`. The control
  plane is enabled and an operator could have fixed and re-activated, but the process never stays up
  long enough.
- **Confidence:** confirmed-by-reading. It is deliberate ("the caller treats that as fatal, as an
  unbuildable recorder is today", lifecycle.py:125-126); I flag it because it inverts the runtime
  policy and turns a storage problem into a camera outage.
- **Existing tests:** `test_pipeline_session.py::test_run_aborts_when_the_boot_hook_fails` and
  `test_lifecycle.py::test_boot_active_activates_and_reports_failure` pin the fatal behaviour.
- **Suggested fix:** when the control plane is enabled, boot `inactive` with `last_error` set and
  health `recording` at ERROR; keep the fatal exit only for the `session_error_fatal` shape (and fix
  CH-2 so that restart actually retries).

### CH-4 — medium — SIGUSR1/SIGUSR2 during core startup kill the sensor

- **Where:** `main.py:174-177` (handlers installed) versus `main.py:82-106` (source open, configure,
  pipeline build); `supervisor.py:157-165`.
- **What is wrong:** the core installs its handlers only after `source.open()`, `source.configure()`
  (GigE: Aravis discovery plus a PTP lock wait of up to `ptp_lock_timeout_s` = 10 s) and
  `pipe.build()`. Until then SIGUSR1/SIGUSR2 have the default disposition, which terminates the
  process. The supervisor forwards them to any live core:
  ```python
  # supervisor.py:160-163
  for s in self.services:
      if s.critical and s.alive():
          try:
              s.proc.send_signal(signum)
  ```
- **Trigger:** an orchestrator or operator runs `docker kill -s USR1 <sensor>` right after `up`, or
  during any restart. The core dies with rc -10, the supervisor tears the sensor down, compose
  restarts it, and the next signal can do it again.
- **Confidence:** confirmed-by-running for the mechanism (a handler-less Python child receiving
  SIGUSR1 returns -10); the ordering in `main.py` is confirmed-by-reading.
- **Related, same window:** SIGINT from the supervisor's teardown arrives as `KeyboardInterrupt`
  inside `source.open()/configure()`, which `except (CameraError, ValueError, OSError, RuntimeError)`
  (main.py:87) does not catch, so `source.close()` is never called. Whether the GigE control
  privilege is still released by object finalisation is **suspected, not verified**.
- **Existing tests:** none (`test_supervisor.py` has no test for `_forward_to_core`).
- **Suggested fix:** install all four handlers at the top of `main()`: USR1/USR2 record a pending
  transition, TERM/INT set a stop flag checked between startup steps. In the supervisor, hold
  forwarded signals until the core has reported ready.

### CH-5 — low — a stop signal during `_on_playing` gives exit 1 or a started-after-stopped source

- **Where:** `main.py:145-159`, `control_zenoh.py:218-224`, `pipeline.py:1126-1131`, `pipeline.py:1335`.
- **What is wrong / trigger:** between handler installation and `loop.run()` the handler interleaves
  with the startup hook.
  - Signal before `lifecycle.boot()`: `request_stop()` sets `_stopping`, so the boot `activate`
    returns `{"ok": False, "error": "stopping"}` (pipeline.py:920-921), `_on_playing` returns False
    and `main` returns 1 with "exited after a pipeline error" — for a clean stop. Under the
    supervisor the 1 is masked (it is already stopping); a bare `main.py` exits 1.
  - Signal after `boot()`, e.g. during `control.start()` → `zenoh.open()`, which can block up to
    `CONNECT_TIMEOUT_MS` = 2 s with no router: `control.close()` runs, then `advertise()` continues.
    It has no `_closed` check, so presence is declared after the close (cleaned up at main.py:180).
    Then `run()` calls `self._start_source()` (pipeline.py:1131) after `request_stop()` already
    stopped the source, and `shutdown()` skips the stop because of
    `stop_source = not self._stopping` (pipeline.py:1335). For GigE the stream is then released
    with acquisition started and `new-buffer` signals still enabled — the situation the comment at
    sources/gige.py:80-85 warns about.
- **Confidence:** confirmed-by-reading. The consequence of the unstopped GigE stream (a late callback
  during finalisation) is suspected.
- **Existing tests:** none.
- **Suggested fix:** falls out of the CH-1 fix (stop runs on the loop); add `if self._closed: return
  False` to `advertise()` and `if self._stopping: return` before `_start_source()`.

### CH-6 — medium — `activate` during a GigE reconnect is refused with an AttributeError and can stall the loop

- **Where:** `pipeline.py:946`, `camera.py:293-304`, `camera.py:120-130`, `camera.py:141-157`,
  `pipeline.py:1294-1299`.
- **What is wrong:** building the audit snapshot queries the device on the main loop:
  ```python
  # pipeline.py:946
  "source": {"pixel_format": self.source.pixel_format(), ...
  # camera.py:299-300 (inside @_serialized)
  try:
      s = self.camera.get_pixel_format_as_string()
  except GLib.Error as e:
  ```
  During a reconnect, `reopen()` first runs `_release()` (`self.camera = None`, camera.py:126) and
  then `open()`. If the camera is not back, `open()` raises and `self.camera` stays `None` for the
  whole backoff (up to `reconnect_backoff_max_s` = 30 s). `None.get_pixel_format_as_string()` raises
  AttributeError, which only `activate()`'s blanket `except Exception` catches.
  Second effect: `pixel_format_string` is `@_serialized`, a blocking `io_lock` acquire, and
  `reopen()` holds `io_lock` across open + configure + the PTP lock wait (camera.py:141-157). An
  `activate` arriving then blocks the GLib main loop (bus, watchdog, every zenoh reply) for the rest
  of that reopen.
- **Trigger:** camera unplugged or rebooting; operator or orchestrator sends `activate`. Reply:
  `session start failed: 'NoneType' object has no attribute 'get_pixel_format_as_string'`,
  `last_error` set to that text, health `recording` WARN. A session opened at that moment would
  simply have waited for frames.
- **Confidence:** confirmed-by-reading (camera.py cannot be imported here).
- **Existing tests:** none; `test_pipeline_session.py` stubs the source.
- **Suggested fix:** use the pixel format cached at `build()` for the snapshot (geometry already is,
  `self._width/_height`); make `pixel_format_string`/`sensor_geometry` tolerate `camera is None`.

### CH-7 — medium (deployment risk) — the control surface is unauthenticated and reachable off-vehicle

- **Where:** `control_zenoh.py:261-274` (session config), `229-236` (queryables), `336-379` (handlers);
  `docker-compose.yml:40` (`network_mode: host`).
- **What is wrong:** `_open_zenoh` sets only `mode`, `connect/endpoints`, `connect/timeout_ms` and
  `connect/exit_on_failure`. No TLS, no ACL, no listen restriction, and scouting is left on
  (docs/LIFECYCLE.md:251 says so deliberately). With host networking, whatever can reach the
  vehicle's zenoh network can:
  - start and stop recording (`change_state`), including stopping it mid-mission;
  - make later recordings lossy or pathological via `configure_recording`: `encoder: x264` with
    `x264_crf: 50`, `x264_preset: placebo`, `segment_seconds: 1`, `videoconvert_threads: 64`
    (recording_settings.py:13-22) — in force until the service restarts;
  - flood activate/deactivate: each cycle writes a settings JSON (fsynced), a CSV and a JSON even
    for zero frames, and `unique_run_prefix` re-lists the directory each time (config.py:752-759);
  - drive playback on replay sources.
  `change_state` has no payload cap (`configure_recording` caps at 16 KiB, control_zenoh.py:368);
  a 5 MB payload was parsed in `exp/e2_lifecycle.py`. Queries are queued with `GLib.idle_add`
  without a bound.
- **What a peer cannot do (checked):** choose a path. `run_id` is sanitised to `[A-Za-z0-9_.-]`
  (`../../etc` → `etc`, `x%05d` → `x-05d`), and `output_dir`, `name_prefix`, `enabled` are rejected
  by `apply_patch`.
- **Confidence:** confirmed-by-reading for the code. The zenoh defaults (peer listens on all
  interfaces, multicast scouting) are taken from the project's own LIFECYCLE.md and were **not
  observed** — no zenoh binding here.
- **Existing tests:** `test_recording_settings.py::test_validation_never_accepts_paths_injection_or_invalid_numbers`
  covers the patch validation; nothing covers authentication, rate or size limits.
- **Suggested fix:** accept a zenoh config file / ACL from `control:`; an option to disable scouting
  and bind to loopback when a local router is used; cap the `change_state` payload; rate-limit
  transitions; optionally make `configure_recording` opt-in.

### CH-8 — medium — `null` and non-boolean values pass through the schema with a meaning

- **Where:** `config.py:410-424` (`_coerce`), `427-431` (`_build`), `517-530` (plugins).
- **What is wrong:** `_coerce` returns early for `None` on every field, not only `Optional` ones
  (`if kind is None or value is None: return value`, config.py:417), and booleans and strings are
  never type-checked. The constraint is that the orchestrator's deep-merge deletes a key on `null`,
  so the schema must not give `null` a meaning. It does:
  - **Booleans:** `recording.enabled: null`, `control.enabled: null`, `health.enabled: null`,
    `camera.reconnect: null` are stored as `None` and behave as False; `plugins[].enabled: null`
    becomes `False` (`bool(p.get("enabled", True))`). Under the orchestrator the same file means
    "default", i.e. True. The same YAML records under rig and does not record under `cam-up`.
  - **`gige.pixel_format: null`:** stored as `None`, and `camera.configure()` then skips setting the
    format (`if cfg.pixel_format:`, camera.py:167) — "keep the device's current format", distinct
    from the default `Mono8` a deleted key gives.
  - **Numerics:** `recording.segment_seconds: null` → `None` (later `int(None)` in the recorder);
    `health.interval_s: null` and `replay.speed: null` raise `TypeError` inside `parse_config`,
    which `main.py:52` / `supervisor.py:261` do not catch (`except (ValueError, OSError)`), so the
    process dies with a traceback and exit 1 instead of the legible exit 2.
  - **Strings:** `recording.name_prefix: null` → `None`, giving files named `None-<stamp>-…`.
  - **Quoted booleans:** `recording.enabled: "false"`, `control.enabled: "no"`, `gige.fake: "false"`,
    `plugins[].enabled: "false"` are all truthy.
- **Trigger:** a hand-edited or templated config run without the orchestrator's merge; any templating
  that yields strings.
- **Confidence:** confirmed-by-running (`exp/e1_config.py`), e.g.
  ```
  recording.enabled: null                -> None
  plugins[0].enabled: null               -> False
  gige.pixel_format: null (default Mono8)-> None
  health.interval_s: null                -> RAISES TypeError: '<=' not supported ...
  recording.enabled: "false" (string)    -> ('false', True)
  ```
  The downstream effects (`int(None)`, the `None-` prefix) are confirmed-by-reading.
- **Existing tests:** `test_config.py::test_optional_numeric_none_stays_none` pins `None` only for an
  `Optional` field; `test_numeric_field_rejects_bool` covers bool-into-numeric. Nothing covers null
  on non-Optional fields or non-bool values in bool fields.
- **Suggested fix:** in `_build`, drop keys whose value is `None` (so null always means "default",
  matching the merge), make `gige.pixel_format` use an explicit sentinel such as `keep` if that
  behaviour is wanted, and validate bool fields as real booleans.

### CH-9 — medium — the health heartbeat is coupled to device I/O

- **Where:** `health.py:440-510` (`poll_once` polls every provider, then publishes), `554-564`;
  `health_boson.py:136,169-205,240-261,280-292`.
- **What is wrong:** one thread polls all providers in sequence and publishes only when all have
  returned. A slow device provider therefore delays `stream` and `recording` too. A Boson whose
  serial node exists but does not answer costs 2 identify commands + 4 reads at 0.5 s each, every
  poll, because the port is closed and reopened after a silent poll (health_boson.py:259-261).
- **Trigger:** Boson command channel grabbed by ModemManager (docs/HEALTH.md warns about this),
  hung firmware, wrong tty. Snapshots then arrive every 3 s while each one advertises
  `health.publish_interval_s: 1.0`; SERVICE_HEALTH.md:38 tells consumers to treat three intervals of
  silence as a hung producer. The whole reporter looks silent while stream and recording are fine.
  The GenICam provider has the same shape when reads start timing out, just before a reconnect.
- **Confidence:** confirmed-by-running for the Boson case (`exp/e8_health.py`, a pty that never
  answers): `publications in 10.5 s: 3 | gaps between publications (s): [3.0, 3.0]`. The GenICam
  case is confirmed-by-reading only; per-read timeout was not measured.
- **Existing tests:** `test_health.py::test_a_slow_provider_cannot_refresh_an_earlier_components_observation`
  covers sample age, not cadence; `test_health_boson.py::test_a_silent_camera_raises_so_the_monitor_can_go_stale`
  covers the STALE result, not the time it takes.
- **Suggested fix:** publish on a fixed tick from cached provider results and poll device providers
  on their own worker (the design SERVICE_HEALTH.md:217-218 describes for Ouster); give each
  provider a time budget; do not re-identify the Boson on every reopen.

### CH-10 — low — `control.enabled: false` arms a retry timer that logs forever

- **Where:** `control_zenoh.py:199-222`.
- **What is wrong:** `start()` treats "disabled" like "unreachable":
  ```python
  if self.advertise():
      return
  ...
  log.info("control plane: zenoh not reachable yet; retrying every %ds", retry_s)
  GLib.timeout_add_seconds(retry_s, self._retry)
  ```
  and `advertise()` returns False for a disabled plane after logging
  `control plane disabled; lifecycle via signals only`. `_retry` then returns True every 5 s.
- **Trigger:** any deployment with `control.enabled: false`: one INFO line every 5 s (about 17,000 a
  day) preceded by a misleading "zenoh not reachable yet". A missing zenoh binding does the same at
  WARNING.
- **Confidence:** confirmed-by-running (`exp/e2_lifecycle.py`, with a stub `GLib`):
  `timers armed: [(5, '_retry')]`, `timer callback keeps itself alive: [True, True, True]`.
- **Existing tests:** `test_control_zenoh.py::test_disabled_control_plane_opens_nothing` calls
  `advertise()` only, never `start()`.
- **Suggested fix:** `if not self._enabled: return` at the top of `start()`; stop retrying on
  `ImportError`.

### CH-11 — low — a signal-killed core yields exit status 256−N

- **Where:** `supervisor.py:186` (`self._exit_rc = rc or 1`), `supervisor.py:266`.
- **What is wrong:** `Popen.returncode` is negative for a signal death; `sys.exit(-9)` exits 247 and
  `sys.exit(-11)` exits 245, not the conventional 137/139.
- **Trigger:** OOM kill or a native crash in a GStreamer/NVENC element. An orchestrator classifying
  exit codes misreads it.
- **Confidence:** confirmed-by-running (`sys.exit(-9) -> 247`, `sys.exit(-11) -> 245`).
- **Existing tests:** `test_supervisor.py::test_dead_core_propagates_its_own_exit_code` pins only
  rc 1 and 2.
- **Suggested fix:** `self._exit_rc = 128 - rc if rc < 0 else (rc or 1)`.

### CH-12 — low — wrong-shaped sections crash with a traceback or are silently reinterpreted

- **Where:** `config.py:427-431`, `446`, `518-520`, `585`.
- **What is wrong (all confirmed-by-running, `exp/e1_config.py`):**
  - `health.providers.genicam: false` becomes `{'enabled': 'auto', 'features': {}}` because of
    `settings = dict(settings or {})` — the provider the user tried to switch off stays on.
  - `health.providers.genicam: true` → `TypeError: 'bool' object is not iterable`.
  - `recording: true`, `camera: gige`, `health: true`, or a top-level list → `AttributeError`.
    Neither exception type is caught by `main.py:52` / `supervisor.py:261`, so the result is a raw
    traceback and exit 1 rather than "config error" and exit 2.
  - `plugins:` written as a map instead of a list → `[]`; every plugin is dropped silently.
- **Existing tests:** `test_health.py::test_health_config_refusals_name_the_key` covers unknown
  providers and bad thresholds, not these shapes.
- **Suggested fix:** check `isinstance(data, dict)` in `_build`/`parse_config`/`_parse_health` and
  raise `ValueError` naming the section; accept or reject a bare bool for a provider explicitly.

### CH-13 — low — a settings edit clears `last_error`, even a no-op one

- **Where:** `lifecycle.py:182`.
- **What is wrong:** `configure_recording` sets `self.last_error = None` on any success. A session
  that ended on a recorder error is surfaced only through `last_error` (health `recording` WARN);
  any client saving settings — unchanged ones included — erases it. LIFECYCLE.md:100 says the error
  stays "until the next clean transition".
- **Confidence:** confirmed-by-running: `noop edit -> True | last_error now: None`.
- **Existing tests:** none (`last_error` does not appear in `test_recording_settings.py`).
- **Suggested fix:** leave `last_error` alone in `configure_recording`.

### CH-14 — low — self-deadlock window on `threading.Event` inside the stop handler

- **Where:** `main.py:157` → `health.py:621` (`self._stop.set()`); main-thread holders at
  `health.py:548` (`self._stop.clear()` in `start()`, called from `_on_playing`) and `health.py:621`
  again via `pipeline.shutdown()` (pipeline.py:1342).
- **What is wrong:** `threading.Event` guards its flag with `Condition(Lock())`, a non-reentrant
  lock. If the handler runs while the main thread is inside `set()`/`clear()` on the same Event,
  the handler's `set()` waits for a lock held by its own thread, lower on the same stack — forever.
- **Trigger:** a stop signal landing in those few bytecodes: during `health.start()` at boot, during
  `health.stop()` when the pipeline is already shutting down on its own (bus ERROR, playback end),
  or a second stop signal while the first handler is inside `health.stop(wait=False)`. The window is
  on the order of a microsecond, so this is very unlikely per stop. If hit, the core hangs until the
  supervisor's 20 s SIGKILL: truncated recording, GigE privilege not released.
- **Confidence:** confirmed-by-running for the mechanism (`exp/e5_event_deadlock.py`: the real
  `HealthMonitor.stop(wait=False)` called from a SIGTERM handler while the main thread holds the
  Event's condition hangs in `threading.py … in set`; a watchdog dumped the stack after 3 s). The
  window was emulated by holding the lock, not hit by timing.
- **Existing tests:** none.
- **Suggested fix:** same as CH-1. See section 5 for the full lock-by-lock answer.

### CH-15 — low — key expressions are built from unvalidated names

- **Where:** `control_zenoh.py:58-67`, `health.py:53-54`, `main.py:111`, `control_zenoh.py:384`.
- **What is wrong:** `CAM_INSTANCE` and `VEHICLE_ID` are interpolated into keys unchecked, although
  LIFECYCLE.md:34 requires single segments. Confirmed-by-running: `front/left` gives
  `fleet/rover3/svc/front/left/lifecycle` (invisible to `fleet/*/svc/*/lifecycle`, and the playback
  descriptor reports instance `left`); `*` gives `fleet/rover3/svc/*/lifecycle`, a queryable that
  would intersect every instance's keys, `change_state` included. Two processes without
  `CAM_INSTANCE` both become `camera` (main.py:111) and share one key.
- **Trigger:** a bare `docker run` / `main.py`, or any launcher that passes a name through. Via
  `cam-up` the name also becomes the compose project name; I did not check what `cam-up` rejects.
  How zenoh fans a query out to two identical queryables was not observed.
- **Existing tests:** `test_control_zenoh.py::test_key_and_encoding` uses well-formed names only.
- **Suggested fix:** validate both against `[A-Za-z0-9_.-]+` at startup and refuse to advertise
  otherwise.

### CH-16 — low — a typo in `gige.timestamp_source` silently turns PTP off while logging "defaulting to ptp_chunk"

- **Where:** `sources/gige.py:44-52`, `config.py:66` (no validation), `health.py:600`.
- **What is wrong:**
  ```python
  want_ptp = self.cfg.timestamp_source == "ptp_chunk"        # False for a typo
  chunks_ok = self.camera.enable_chunks() if want_ptp else False
  ...
  except ValueError:
      log.warning("invalid timestamp_source %r; defaulting to ptp_chunk", ...)
      prefer = TimestampSource.PTP_CHUNK
  ```
  Chunks and PTP are never enabled, so `resolve_active_source` falls to the camera clock
  (timestamps.py:100-104). Health's `ptp_expected` is computed from the same raw string, so there is
  no PTP WARN either.
- **Trigger:** `timestamp_source: ptp-chunk`. The value is documented as `ptp_chunk | camera | system`
  in camera.example.yaml:44 but not validated at load.
- **Confidence:** confirmed-by-reading (the config accepting the typo is confirmed-by-running).
- **Existing tests:** none for an invalid value.
- **Suggested fix:** validate the enum in `parse_config` (exit 2), like `control.initial_state`.

### CH-17 — low (suspected) — GenICam features are probed by XML presence only, and health writes to the camera

- **Where:** `health_genicam.py:187-188`, `223`, `129`, `108`; `camera.py:56-60`, `226`.
- **What is wrong:**
  - `_has()` is `dev.get_feature(feature) is not None`, i.e. "the node exists in the XML". A node
    that exists but is not implemented or available on that model is then read every poll. For
    `DeviceTemperature` / `PowerSupply*` the key is added to `_measurements`
    (health_genicam.py:168-175), the failed read becomes `null` + `health.metric.<k>.state:
    unavailable`, and the camera component sits at WARN "measurement unavailable" permanently —
    where SERVICE_HEALTH.md:52-53 says unsupported capabilities are informational.
  - The candidate loop (`health_genicam.py:143-161`) and `enable_ptp` (camera.py:226) take the first
    name that exists and never fall through to the next on a failed read/write.
  - Each poll writes `DeviceTemperatureSelector` (line 223, left on the last entry) and executes
    `PtpDataSetLatch` (line 129). SERVICE_HEALTH.md:20: "Health collection is observational: it must
    not configure hardware".
  - Device identity is `id(dev)` (line 108); CPython can reuse an address after the old wrapper is
    freed in `_release()`, in which case a reconnect goes unnoticed and cached status survives it.
- **Confidence:** the code facts are confirmed-by-reading. Whether a given camera lists
  unimplemented nodes, and what Aravis returns for them, is **suspected** — it needs
  `arv-tool-0.8 features` on the target models, or a stub whose `get_feature()` returns a node that
  raises on read.
- **Existing tests:** `test_health.py::test_genicam_reads_what_the_camera_has_and_nothing_else` models
  only absent (`None`) features.
- **Suggested fix:** probe with Aravis's availability check; restore the selector; use a per-open
  generation counter on `GigECamera` instead of `id()`.

### CH-18 — low — Boson framing: no resync on a truncated frame, channel not checked, port not exclusive

- **Where:** `health_boson.py:116-129`, `106`, `145`.
- **What is wrong:**
  - `FrameSplitter.feed` takes everything from the first `0x8E` to the next `0xAE`. A reply cut short
    followed by a complete one is merged into a single candidate that fails CRC, so the good frame
    is lost and the command times out.
  - `decode_frame` unpacks `channel` and ignores it.
  - The port is opened without `TIOCEXCL`/`flock`, so "one process per port" (docs/HEALTH.md:168) is
    not enforced; a second reader steals reply bytes.
- **Confidence:** confirmed-by-running for the first two (`exp/e8_health.py`):
  `frames found: 1 -> ['dropped: CRC mismatch'] | leftover buffered: b''`, and a channel-5 frame
  decoded normally. The third is confirmed-by-reading.
- **What I checked and found correct:** CRC-16 init `0x1D0F` over the unescaped body, the escape
  table, the 15-byte minimum, seq + function-id matching, drain before each command, EOF/EIO →
  reopen. The function ids are FLIR's and were not verified against the SDK.
- **Existing tests:** `test_health_boson.py::test_the_splitter_finds_frames_across_reads_and_drops_garbage`
  covers leading garbage, not a truncated frame.
- **Suggested fix:** on a `START` inside a candidate, restart from the last `START`; require
  `channel == 0`; take an exclusive lock on open.

### CH-19 — low — "set `ZENOH_CONNECT` empty to scout only" cannot work through compose

- **Where:** `docker-compose.yml:64-67`, `control_zenoh.py:77`.
- **What is wrong:** the comment says "set empty to scout only", but the line is
  `- ZENOH_CONNECT=${ZENOH_CONNECT:-tcp/localhost:7447}`. `:-` substitutes the default when the
  variable is unset *or empty*, so an empty value never reaches the container.
- **Confidence:** confirmed-by-reading (compose interpolation rule; not run).
- **Suggested fix:** `${ZENOH_CONNECT-tcp/localhost:7447}`, or document `control.zenoh_connect: ""`
  as the only way.

### CH-20 — low — health levels disagree with the docs in four places

- **Where:** `health.py:142-155` (`merge`), `168-177` (`build_snapshot`), `219-220` (`HealthWindow`),
  `control_zenoh.py:455-459`.
- **What is wrong (all confirmed-by-running, `exp/e8_health.py`):**
  - `stream` is fed by two providers (pipeline counters and Aravis stats). When only the GenICam
    one is stale, the whole component becomes level 3 with a "no data" message while the pipeline's
    observation is current:
    `stream: level=3 msg='no data for 7s: …' availability=stale sample_age_s=0.0 fps.delivered=25.0`.
  - During a GigE reconnect `stream` is 3 (STALE) with `health.last_level: 2`; docs/HEALTH.md:141
    documents ERROR for reconnecting.
  - A health query answered before the first poll has `"level": 0` and no statuses;
    SERVICE_HEALTH.md:27 says an empty report "is unknown, never OK".
  - The per-session summary keeps the numerically largest level, so an ERROR is replaced by a later
    STALE (`worst_level: 3, worst_message: 'no data for 0s: camera reconnecting'`), the trap
    SERVICE_HEALTH.md:272 describes.
- **Existing tests:** `test_health.py::test_merged_provider_staleness_cannot_hide_another_providers_fault`
  pins the merge behaviour as intended; `test_control_zenoh.py::test_health_query_before_the_first_poll_is_an_empty_well_formed_snapshot`
  pins the empty snapshot.
- **Suggested fix:** let the component's primary provider own its level and report a secondary
  provider's staleness as a value; rank ERROR above STALE in `HealthWindow`; fix the doc table.

### CH-21 — low — descriptor says `activate` is accepted when recording is disabled; `last_error` misses refusals

- **Where:** `lifecycle.py:227`, `152-153`, `144-158`.
- **What is wrong:** with `recording.enabled: false` the descriptor has
  `transitions: ['activate']` and the request answers "recording disabled by config";
  LIFECYCLE.md:85 defines `transitions` as "what change_state accepts RIGHT NOW". The early refusals
  (unknown transition, wrong state, disabled, stale settings) never set `last_error`, which
  LIFECYCLE.md:100 calls "the last refusal / session error".
- **Confidence:** confirmed-by-running.
- **Existing tests:** `test_lifecycle.py:93` asserts `transitions == ["activate"]` for the disabled
  case, so the current behaviour is pinned.
- **Suggested fix:** return `[]` when recording is disabled or stopping; correct the doc wording.

### CH-22 — low — a back-pressured stderr can swallow the stop signal

- **Where:** `main.py:154`; same shape at `supervisor.py:154-155`.
- **What is wrong:** both handlers log before doing anything else. The image sets no
  `PYTHONUNBUFFERED`, so `sys.stderr.buffer` is a `BufferedWriter`. If the signal interrupts a
  main-thread log write that is blocked on a full stderr pipe, CPython runs the handler inside that
  write, and the handler's own log call raises
  `RuntimeError: reentrant call inside <_io.BufferedWriter name='<stderr>'>`. The handler aborts at
  its first line: no `forget()`, no `request_stop()`; in the supervisor `_stopping` is never set.
  The interrupted logging call swallows the exception, so the process just keeps running until
  SIGKILL.
- **Trigger:** needs the log pipe to be blocked at that instant (slow log driver, heavy GStreamer
  debug output). Rare.
- **Confidence:** confirmed-by-running for the mechanism (`exp/e6_stderr_reentrant.py`, stderr
  redirected to a full pipe): `handler aborted at its first line: RuntimeError: reentrant call inside
  <_io.BufferedWriter name='<stderr>'>`. Not reproduced inside a container.
- **Suggested fix:** set the flag / schedule the stop first and log afterwards, outside the handler.

### CH-23 — low — numeric fields have no range checks

- **Where:** `config.py:410-424`; consumers at `recorder.py:117`, `pipeline.py:1244,1296-1298`,
  `sources/gige.py:151`, `health.py:562`.
- **What is wrong (parsing confirmed-by-running; effects by reading unless noted):**
  - `recording.segment_seconds: 0` or negative is accepted and becomes one file per second
    (`max(1, int(seconds))`); `0.9` is truncated to 0. The runtime patch path enforces 1–86400.
  - `camera.reconnect_backoff_s: 0` → `min(backoff * 2, max)` stays 0: reconnect attempts with no
    delay and a WARNING per attempt. `camera.reconnect_timeout_s: 0` → always "disconnected".
  - `health.interval_s: .nan` passes the `> 0` check (config.py:577) and
    `max(0.0, nan - x)` is `0.0` (run), so the health thread polls the camera without pause;
    `.inf` makes `Event.wait` raise `OverflowError` (run) outside the loop's `try`, ending health.
  - Floats are truncated into int fields (`usb.width: 640.7` → 640).
- **Existing tests:** `test_config.py` covers string typos and bools, not ranges.
- **Suggested fix:** per-field bounds in `parse_config`, reusing `recording_settings.BOUNDS`.

---

## 2. Improvements & missing features

1. **Reject or at least warn on unknown config keys.** `config.py:1-4` ignores them by design, so
   `control: {intial_state: inactive}` boots *active* and `recordng: {enabled: false}` records
   (both confirmed-by-running). For a vehicle I would treat this as the most valuable config change;
   a `strict` mode keeps forward-compatibility where it is wanted.
2. **One validation layer** for enums (`gige.timestamp_source`, `plugins[].isolation`,
   `recording.encoder`, `nvenc_preset`), booleans, ranges and section shapes, raising `ValueError`
   so every config mistake exits 2 with the key named.
3. **Control-plane hardening:** zenoh config/ACL passthrough, scouting and listen options, payload
   cap and rate limit on `change_state`, a request id or deadline so a command the client already
   timed out on is not executed later.
4. **Publish the final descriptor before withdrawing presence.** `_stop` closes the control plane
   first (main.py:158), so subscribers never see the last session's closing `inactive` descriptor.
   Closing first also puts any zenoh close latency ahead of recording finalisation (not measured).
5. **Persist more than one word in the state file:** `last_error`, a timestamp and a boot id. That
   allows "resumed inactive because …" after a restart and an age bound on resume. Note that an
   unclean vehicle power-off counts as a crash, so yesterday's `inactive` is resumed today.
6. **Recording-progress health.** `recording` is OK "recording" whenever a session is open, even if
   `frames` never moves (e.g. a stream-copy session still waiting for a keyframe). Add
   `skipped_awaiting_keyframe` to the values and WARN when the stream advances and the session
   does not.
7. **Decouple health publication from device polling** (see CH-9) and add a per-provider budget.
8. **Supervisor:** conventional 128+N codes; a non-zero exit when the core had to be SIGKILLed on
   stop (today it logs a warning and returns 0); `try/finally: self._teardown()` around `_monitor()`
   so an unexpected exception cannot orphan the core; surface "plugin gave up after 5 restarts"
   somewhere other than the log. The backoff is a fixed 2 s and blocks the monitor loop.
9. **Boot policy option** for CH-3 (`stay inactive` vs `exit`).
10. **Warn when an absolute `recording.output_dir` is not under `RIG_DATA_DIR`.** The docstring
    (config.py:657-659) says such recordings "die with the container's writable layer"; nothing says
    so at runtime.
11. **Allow disabling the built-in `disk.free_pct` limit** (`{}` is a config error today), and
    consider a time-to-full value.
12. **Health for the control plane itself** (session open, router linked) so "no dashboard control"
    is distinguishable from "service down".

Dead code and stale comments noticed:

- `control_zenoh.py:48` `SCHEMA_VERSION` is never used.
- `pipeline.py:1024` `"last_result"` in `get_state()` is never read.
- `lifecycle.py:239` `else self.segment_seconds` cannot be reached with a real pipeline (a session
  implies a settings view); `main.py:115` passes the argument only for that branch.
- `health.py:184` filters `None` thresholds that `config.py:611` already rejects.
- `health.providers.boson.features` is accepted and ignored.
- `pipeline.py:665` `if True:`.
- `main.py:80-81` and `sources/base.py:9` still describe GigE as the only source.
- `session.py:23-26` states close never runs inside the bus dispatch; the signal handler can do it.
- `pipeline.py:1345-1346` "Only reachable when the MAIN pipeline errored" — also reached when a stop
  lands mid-`activate`.
- `docs/SERVICE_HEALTH.md:259` test counts (35 / 13 / 26) no longer match the files (41 / 14 / 26).
- `docs/HEALTH.md:133` omits `health_boson.py`.

---

## 3. Test gaps for this area

| Behaviour with no test | Test file that should hold it |
|---|---|
| Stop signal nested in an in-flight transition must leave no state file (CH-1) | `tests/test_lifecycle.py` |
| Uncommanded end + restart with the control plane off (CH-2); `last_error` across a restart | `tests/test_lifecycle.py`, `tests/test_pipeline_session.py` |
| `configure_recording` must not clear `last_error` (CH-13) | `tests/test_recording_settings.py` |
| `start()` with `enabled=False` arms no timer (CH-10); `advertise()` after `close()`; `close()` during `advertise()` | `tests/test_control_zenoh.py` |
| Oversized `change_state` payload; invalid instance / vehicle names in keys | `tests/test_control_zenoh.py` |
| Handlers installed before the source opens; stop during `_on_playing`; exit codes 0/1/2 | new `tests/test_main.py` (none exists) |
| `_forward_to_core`, `_handle_plugin_exit` (restart, give-up, respawn failure), `_teardown` kill path, negative core rc | `tests/test_supervisor.py` |
| null on non-Optional fields, non-bool values in bool fields, non-mapping sections, `providers.genicam: false`, ranges, `timestamp_source` enum | `tests/test_config.py` |
| `activate` while `GigECamera.camera is None`; `pixel_format_string()` during reconnect | `tests/test_pipeline_session.py`, `tests/test_camera_health_lock.py` |
| A slow provider must not delay other components' publication; `stream` not STALE when only the Aravis-stats contributor is stale; ERROR vs STALE in `HealthWindow` | `tests/test_health.py` |
| GenICam node present but unreadable; device identity across a reconnect | `tests/test_health.py` |
| Splitter resync after a truncated frame; non-zero channel; time budget of a silent camera; `EAGAIN` on write | `tests/test_health_boson.py` |

---

## 4. Checked and found sound

- **Serialisation of transitions.** Zenoh `change_state`, `configure_recording` and SIGUSR1/2 all go
  through `GLib.idle_add` onto the main loop; two clients cannot interleave; a repeated transition
  is an idempotent `noop`. (The exception is the stop handler, CH-1.)
- **Settings compare-and-set.** Generation and revision are checked and bumped inside one main-loop
  call (pipeline.py:881-901); the check is type-strict (`type(...) is not int`); the `activate`
  guard and the activation are one call; edits are refused unless inactive.
- **No path control from the control plane.** `apply_patch` rejects everything outside `EDITABLE`;
  `run_id` cannot contain `/` or `%`.
- **No half-attached recorder after a failed activation.** `_session` is assigned only after
  `sess.start()` returns (pipeline.py:960); a start failure tears down the session pipeline and
  sidecar (session.py:140-143) and restores `INACTIVE`. Only the documented orphan settings JSON
  remains.
- **Audit-file ordering.** Exclusive tmp file, fsync, rename, all before the session is visible to
  the feeder threads (pipeline.py:948 → 960).
- **Replies.** Every handler path answers on the queryable's concrete key, with `ok: false` on parse
  or internal errors; `advertise()` failures are contained and leave nothing half-declared.
- **Health versus camera control.** Health never waits for `io_lock` (`acquire(blocking=False)`),
  releases it between transactions and checks the stop event between reads. I found no lock-order
  inversion between `io_lock`, `stream_lock`, `RecordingSession.lock` and `HealthMonitor._lock`.
- **Staleness bookkeeping.** Failed polls keep values with explicit `health.availability` and
  `health.sample_age_s`; query replies age the cached snapshot; non-finite numbers become null.
- **Recording directory resolution.** A relative `output_dir` that escapes the recordings root falls
  back to the default layout; a run-registry link resolving outside the data root is ignored.
- **Supervisor stop budgets.** Plugin 10 s and core 20 s run concurrently inside compose's 25 s; an
  operator stop returns 0; core rc 1 and 2 are propagated.
- **Example config versus schema.** Every key in `config/sensors/camera.example.yaml`, active or
  commented, exists in the dataclasses (`exp/e9_keys.py`), and the defaults I compared match.

---

## 5. Is `main.py:153-159` safe as a signal handler?

Short answer: **one real self-deadlock, one way the handler can abort itself, several unsafe
interleavings; the other locks are safe by construction.**

The handler runs on the main thread between bytecodes, so it can interrupt any Python code the main
thread is running (a GLib callback, `_run_transition`, `_on_playing`), but never the inside of a
native call.

| Call | Lock or blocking step | Can the main thread already hold it? | Verdict |
|---|---|---|---|
| `log.info` (154) | logging handler lock (RLock) | yes, harmless | reentrant |
| | `sys.stderr` `BufferedWriter` | only when a log write is blocked and interrupted | handler raises and aborts — CH-22 |
| `lifecycle.forget()` (155) | none | — | no deadlock; ordering bug CH-1 |
| `health.stop(wait=False)` (157) | `threading.Event` internal `Lock` (non-reentrant) | yes: `health.py:548` (`start()`), `health.py:621` via `pipeline.shutdown()`, or the handler itself on a second signal | **self-deadlock, CH-14** (window of a few bytecodes) |
| `control.close()` (158) | no Python lock; native zenoh `undeclare()` / `close()` | no | no deadlock; a nested second `close()` is tolerated (errors swallowed). Interleaves with an in-flight `advertise()` (CH-5) |
| `pipe.request_stop()` (159) | `_stop_event.set()` (non-reentrant Event lock) | no: every main-thread `set()` follows `_stopping = True` (pipeline.py:1078-1079, 1336-1337) and the handler returns early on `_stopping` | safe |
| | `GLib.timeout_add_seconds` (main-context mutex) | no, GLib drops it while a callback runs | safe |
| | `sess.begin_close()` → `_close_lock`, `lock` (non-reentrant) | no: `deactivate()` clears `self._session` first (977); `request_stop()`/`shutdown()` set `_stopping` first | safe; may wait for a feeder inside `push()` |
| | `source.stop()` → GigE `stream_lock`, `io_lock` | both are RLocks | no self-deadlock; waits while the reconnect thread holds `io_lock` (its PTP wait is cut short by `_stop_event`) |
| | `health.close_window()` → `HealthMonitor._lock` (non-reentrant) | held on the main thread only in `open_window` (pipeline.py:957, before `_session` is published at 960) and in `close_window` of an already-unpublished session | unreachable, safe |
| | `sidecar.stop()` → `queue.Queue` mutex (non-reentrant) | only inside `sidecar.stop()` of an already-unpublished session | safe |
| | `drops.summary()` | `DropStats` has no lock | safe |

Other things the handler does that are legal but fragile:

- It blocks inside the handler for the session drain (≤ 5 s) plus the sidecar join (≤ 5 s).
- It can run `finish_close()` — `remove_signal_watch()`, a nested `timed_pop_filtered`, `NULL` — from
  inside that session's own bus callback, which session.py:23-26 says never happens. GLib allows a
  source to be destroyed during its own dispatch, so I expect no crash, but this was not run.
- It interleaves with non-atomic main-loop code: CH-1 (state file), CH-5 (startup hook).

All of these go away if the handler only schedules the stop on the loop, as `_transition` already
does for SIGUSR1/2, plus an early flag for the pre-loop phase (CH-4).
