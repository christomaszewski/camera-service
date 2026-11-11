# camera-service v1.13.0 (6d3af8b) — test suite and CI review

Scope: tests and CI only. Production defects are noted only where a test (or its absence) exposes them.
Evidence labels used throughout: **[ran]** = verified by executing here; **[read]** = established by reading
the file; **[api]** = from the GitHub Actions / pull-request API for `christomaszewski/camera-service`.
Nothing was written into `the repository root` (`git status --porcelain` is empty after all runs);
everything ran from a copy of the tracked files under
`exp/copy`.

### Headlines

- **Ran:** the full CI pytest command on Python 3.12 + GStreamer 1.24.2 — 619 tests, 617 passed, 2 failed
  (environment: no IPv6), 0 skipped, 54 s; coverage 72.6 % line / 66.7 % branch overall, 82.2 % / 74.9 % for
  `cam_driver`; plus the three launcher unittest scripts (25 tests, all pass). No Docker daemon, so the six service
  smokes and the 1.20 / 1.28 lanes were not run.
- **A real flake was reproduced** in `test_shm_source.py` (1 of 6 idle batches; 7 of 8 batches and 8 of 8 single-file runs under CPU load) — TQ-1/TQ-20.
- **A contract deviation no test covers:** `cam-up` runs `docker volume create` on `ps`, `logs`, `down` and `pull` —
  §1.6, TA-1.
- **No automated test at all:** `bridge_stream.py`, `zenoh_advertiser.py`, `ros2-source/node.py`, every C++ bridge
  (never compiled in CI); `sources/usb.py`, `sources/rtsp.py` and `main.py` only through log-grepping smokes;
  `supervisor.py` at 37 %.
- **CI:** the release branch is not push-triggered (14 of 18 commits since v1.10.0 never ran CI as pushed; the three tags were
  covered by manual dispatch), and `registry-release.yml` publishes without requiring a green run.

---

## 1. What ran here

### 1.1 Environment that was built

| Item | Value |
|---|---|
| Host | Ubuntu 24.04.5, amd64, 4 CPUs, no Docker daemon (CLI + Compose v5.6.1 plugin present), kernel has **no IPv6** (`socket(AF_INET6)` -> `EAFNOSUPPORT`) |
| Interpreter with GI | `/usr/bin/python3.12` (system `python3-gi 3.48.2`); default `python3` is 3.13 and cannot import `gi` |
| GStreamer | distro **1.24.2** — after `apt-get install` of the same package list `core-driver/Dockerfile.dev` uses for its `distro` target (`gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-aravis-0.8 gir1.2-gst-rtsp-server-1.0 gstreamer1.0-{tools,plugins-base,plugins-good,plugins-bad,plugins-ugly,libav} aravis-tools python3-numpy`) |
| Test deps | `pip install --target <scratch>/deps312`: pytest 9.2.0, coverage 7.17.2, pytest-timeout 2.4.0, eclipse-zenoh 1.9.0 (same pins as `tools/ci_pytest.sh:4` / `core-driver/requirements.txt:9`) |

This is the closest local equivalent of CI's `ubuntu24` lane (Ubuntu 24.04 / GStreamer 1.24, `certify.yml:29-34`),
on amd64 and outside Docker. The 1.20 (`legacy`) and 1.28.8 (`modern`, `modern-arm64`) lanes could not be
reproduced: they need the Docker images (no daemon here).

### 1.2 Full pytest suite, CI command line, Python 3.12 + GStreamer 1.24.2 **[ran]**

Command (identical to `tools/ci_pytest.sh:18-20` except report paths and `--durations=25`):

```
cd <copy> && PYTHONPATH=<deps312> PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=<reports>/coverage.data \
  python3.12 -m coverage run -m pytest core-driver/tests plugins/webrtc-bridge/tools \
  -p no:cacheprovider -q -ra --tb=short --durations=25 --timeout=45 --timeout-method=thread --junitxml=...
```

Result: **619 collected — 617 passed, 2 failed, 0 skipped, 54 s wall** (CI budget is 300 s; the three CI lanes
took 50–58 s for this step on the v1.13.0 run **[api]**).

The 2 failures are **environment-caused**, both in `core-driver/tests/test_recording_resilience.py`:

- `test_two_zenoh_editors_cannot_overwrite_each_others_revision`
- `test_zenoh_settings_change_racing_activation_uses_one_consistent_snapshot`

Both die in the fixture at `test_recording_resilience.py:258` (`zenoh.open(cfg)` for a client peer):
`zenoh.ZError: Can not create a new TCP listener bound to tcp/[::]:0 ... Address family not supported by protocol`.
The client peers are opened in zenoh's default *peer* mode with no `listen/endpoints`, so zenoh binds an IPv6
wildcard listener; the review host's kernel has IPv6 disabled. Not a product failure, but it is a hermeticity gap in
the fixture (see TQ-19) — see §1.6 for the confirmation run with an IPv4 listen endpoint.

Slowest tests: `test_repeated_sessions_release_resources_and_keep_streaming` 2.95 s,
`test_restart_from_a_later_fragment_returns_to_the_session_beginning` 2.49 s,
`test_a_silent_camera_raises_so_the_monitor_can_go_stale` 1.86 s. Nothing approaches the 45 s per-test limit.

The suite has grown from the 549 tests `docs/TESTING.md:139-146` records for 2026-10-16 to 619 at v1.13.0;
`docs/TESTING.md` has no baseline for the released commit.

### 1.3 Coverage (`.coveragerc` as shipped: branch=True, source = `core-driver/cam_driver` + `plugins/webrtc-bridge/tools`) **[ran]**

Totals: 8106 statements, **72.6 % line / 66.7 % branch** (71.2 % combined).
`cam_driver` alone: **82.2 % line / 74.9 % branch** (6956 stmts, 2310 branches).
`plugins/webrtc-bridge/tools` alone: **14.8 % line / 18.5 % branch** (1150 stmts).
(The two environment failures cost a few lines in `control_zenoh.py` / `pipeline.py`; numbers are otherwise CI-comparable.)

| Module | Stmts | Miss | Line % | Branches | Br miss | Branch % |
|---|---:|---:|---:|---:|---:|---:|
| `cam_driver/sources/rtsp.py` | 141 | 141 | **0.0** | 32 | 32 | **0.0** |
| `cam_driver/sources/usb.py` | 72 | 72 | **0.0** | 12 | 12 | **0.0** |
| `webrtc-bridge/tools/bridge_stream.py` | 727 | 727 | **0.0** | 254 | 254 | **0.0** |
| `webrtc-bridge/tools/check_encoder_setup.py` | 24 | 24 | **0.0** | 2 | 2 | **0.0** |
| `webrtc-bridge/tools/discovery_probe.py` | 78 | 78 | **0.0** | 26 | 26 | **0.0** |
| `webrtc-bridge/tools/webrtc_consumer.py` | 79 | 79 | **0.0** | 18 | 18 | **0.0** |
| `webrtc-bridge/tools/zenoh_advertiser.py` | 69 | 69 | **0.0** | 14 | 14 | **0.0** |
| `cam_driver/camera.py` | 222 | 144 | 35.1 | 46 | 45 | 2.2 |
| `cam_driver/sources/gige.py` | 100 | 55 | 45.0 | 10 | 9 | 10.0 |
| `cam_driver/sources/shmsrc.py` | 172 | 84 | 51.2 | 54 | 40 | 25.9 |
| `cam_driver/sources/gstbase.py` | 160 | 68 | 57.5 | 46 | 33 | 28.3 |
| `cam_driver/sources/factory.py` | 23 | 10 | 56.5 | 12 | 5 | 58.3 |
| `cam_driver/bayer_tile.py` | 66 | 23 | 65.2 | 24 | 10 | 58.3 |
| `cam_driver/sources/pcapsrc.py` | 279 | 75 | 73.1 | 116 | 48 | 58.6 |
| `cam_driver/sources/replay.py` | 514 | 107 | 79.2 | 166 | 65 | 60.8 |
| `cam_driver/replay_coordination.py` | 230 | 46 | 80.0 | 90 | 30 | 66.7 |
| `cam_driver/sources/base.py` | 66 | 14 | 78.8 | 0 | 0 | – |
| `cam_driver/pcapio.py` | 134 | 21 | 84.3 | 68 | 21 | 69.1 |
| `cam_driver/control_zenoh.py` | 342 | 61 | 82.2 | 82 | 14 | 82.9 |
| `cam_driver/export_recordings.py` | 377 | 55 | 85.4 | 150 | 35 | 76.7 |
| `webrtc-bridge/tools/thermal_preview.py` | 10 | 1 | 90.0 | 2 | 1 | 50.0 |
| `cam_driver/pipeline.py` | 779 | 88 | 88.7 | 260 | 54 | 79.2 |
| `cam_driver/uvcpcap.py` | 476 | 48 | 89.9 | 200 | 41 | 79.5 |
| `cam_driver/health_boson.py` | 230 | 23 | 90.0 | 68 | 14 | 79.4 |
| `cam_driver/timestamps.py` | 92 | 10 | 89.1 | 22 | 4 | 81.8 |
| `webrtc-bridge/tools/h264_level.py` | 24 | 2 | 91.7 | 10 | 2 | 80.0 |
| `cam_driver/sidecar.py` | 131 | 9 | 93.1 | 18 | 5 | 72.2 |
| `cam_driver/thermal_preview.py` | 32 | 2 | 93.8 | 12 | 2 | 83.3 |
| `cam_driver/gst_frame.py` | 37 | 2 | 94.6 | 8 | 2 | 75.0 |
| `cam_driver/recorder.py` | 95 | 6 | 93.7 | 40 | 6 | 85.0 |
| `cam_driver/session.py` | 222 | 14 | 93.7 | 76 | 11 | 85.5 |
| `cam_driver/transport.py` | 46 | 3 | 93.5 | 6 | 1 | 83.3 |
| `cam_driver/health_genicam.py` | 214 | 16 | 92.5 | 90 | 4 | 95.6 |
| `cam_driver/health.py` | 457 | 18 | 96.1 | 182 | 16 | 91.2 |
| `cam_driver/keyframe.py` | 69 | 2 | 97.1 | 32 | 2 | 93.8 |
| `cam_driver/lifecycle.py` | 185 | 5 | 97.3 | 52 | 4 | 92.3 |
| `cam_driver/playback.py` | 451 | 9 | 98.0 | 174 | 9 | 94.8 |
| `cam_driver/formats.py` | 62 | 1 | 98.4 | 30 | 1 | 96.7 |
| `cam_driver/config.py` | 399 | 7 | 98.2 | 112 | 4 | 96.4 |
| `webrtc-bridge/tools/encoder_policy.py` | 33 | 0 | 100 | 16 | 1 | 93.8 |
| `cam_driver/recording_settings.py` | 45 | 0 | 100 | 18 | 1 | 94.4 |
| `cam_driver/dropstats.py`, `webrtc-bridge/tools/{format_adapt,header_transport,scale_plan}.py` | – | 0 | 100 | – | 0 | 100 |

Important caveats on reading this table:

- `.coveragerc:4-6` measures only `core-driver/cam_driver` and `plugins/webrtc-bridge/tools`. **`core-driver/main.py`,
  `core-driver/supervisor.py`, `tools/sensor_env.py`, `tools/runtime_images.py`, `tools/export-recordings` and
  `plugins/ros2-source/cam_ros2_source/*.py` are not in the report at all**, so the headline percentage silently
  omits the entry point, the PID-1 supervisor and the whole launcher. §1.5 measures them.
- The six CI service smokes run the core in a separate process inside `docker run`; none of that is measured, so
  `sources/usb.py` and `sources/rtsp.py` read 0 % although `usb_test.sh` / `rtsp_test.sh` exercise them. The
  reverse also holds: the 0 % tells you there is no *in-process, assertion-level* test of those two sources.

### 1.4 Other runs **[ran]**

| Run | Interpreter | Result |
|---|---|---|
| `python3 tools/test_dev_defaults.py` (real `docker compose config`, fake `docker build`) | 3.13 | 12 tests OK, 10.1 s |
| `python3 tools/test_gstreamer_archives.py` | 3.13 | 3 tests OK, 0.2 s |
| `python3 tools/test_runtime_images.py` (fake docker shim, real Compose rendering) | 3.13 | 10 tests OK, 13.1 s |
| pytest suite under default `python3` 3.13 (no `gi`) | 3.13 | **exit 2, 0 tests run**: `16 errors during collection`, 6 module-level skips. With `--continue-on-collection-errors`: 384 passed, 6 skipped (modules), 16 collection errors |
| `core-driver/tests/check_transport_caps.py` (not collected by pytest: wrong file-name prefix) run by hand | 3.12 | 1 passed — it works, it is simply never run by anything (TQ-13) |

The bare-host result is worth stating plainly: only 6 of the 22 GStreamer-dependent test modules carry the
`pytest.skip(..., allow_module_level=True)` guard (`test_pipeline_pts.py:23-28`, `test_pipeline_publish_rate.py:20-27`,
`test_pipeline_reconnect.py:23-31`, `test_pipeline_session.py:18-30`, `test_recorder.py:14-24`, `test_session.py:20-29`);
the other 16 `import gi` unguarded, so a contributor without the dev image cannot run even the ~384 pure-Python
tests with the documented command — collection aborts first.

*(§1.5 extended coverage and §1.6 confirmation experiments are appended below as they complete.)*

### 1.5 Extended coverage: the files `.coveragerc` leaves out **[ran]**

Same pytest run, scratch rcfile adding `core-driver/*.py`, `tools/`, `plugins/ros2-source/` to `source`:

| File (not in the shipped coverage report) | Stmts | Line+branch % under pytest | Why |
|---|---:|---:|---|
| `core-driver/main.py` | 100 | **0** | Only ever run as a subprocess (`test_replay_integrity.py:102`, the smokes); no subprocess coverage is configured |
| `core-driver/supervisor.py` | 176 | **37** | `test_supervisor.py` bypasses `__init__`/`run()` (TQ-9) |
| `tools/sensor_env.py` | 245 | 88 | `test_sensor_env.py` drives `main()` in-process |
| `tools/export-recordings` | 108 | 76 | `test_proxy_export.py` via `runpy` |
| `tools/runtime_images.py` | 114 | 0 under pytest | Tested only by `tools/test_runtime_images.py`, a unittest script CI runs in the `dev-contracts` job without coverage |
| `plugins/ros2-source/cam_ros2_source/framing.py` | 89 | 97 | `test_ros2_source.py` |
| `plugins/ros2-source/cam_ros2_source/node.py` | 258 | **0** | No test imports it (needs rclpy); never run in CI |

### 1.6 Confirmation experiments **[ran]** (scratch scripts in `<scratch>/review/exp/`)

1. **The two zenoh failures are IPv6-only.** A copy of `test_recording_resilience.py` whose *client* peers also get
   `listen/endpoints=["tcp/127.0.0.1:0"]` passes both tests (`2 passed`). Separately, the production-shaped open
   (`control_zenoh.py:268-274`: peer mode, default listen endpoints) raises the same `tcp/[::]:0 ... Address family
   not supported` here. No pytest exercises that real open path — every `ZenohControl` test injects
   `session_factory` — so an IPv6-disabled vehicle kernel (`ipv6.disable=1`) would lose the lifecycle/health/playback
   control plane with only a warning, and nothing in the suite would notice (TA-21).
2. **Launcher side effects per verb** (fake `docker` shim logging every call; real `docker compose config`):

   | `cam-up --dev cam_a.yaml <verb>` | docker commands issued |
   |---|---|
   | `config` | `compose config` only |
   | `images`, `runtime-images` | `compose config --format json` only |
   | `replay-capabilities` | none |
   | `ps --format json` | **`volume create cam_cam_a_sock`**, `compose ps` |
   | `logs --tail 5` | **`volume create cam_cam_a_sock`**, `compose logs` |
   | `down` | **`volume create cam_cam_a_sock`**, `compose down` |
   | `pull` | **`volume create cam_cam_a_sock`**, `compose pull` |
   | `up -d` | `volume create`, `compose up -d` |

   Cause: `cam-up:264` gates setup on `"$VERB" != config`, not on `up`. The service contract says "Any setup the
   launcher performs is gated on the verb being `up`" (SERVICE_CONTRACT O5) and that `down` removes containers "and
   nothing else" (L5); `rig down --purge` removes `cam_{name}_sock` (D5) and the next `rig status` re-creates it.
   With `--zenohd`/`--roscore` the same branch also starts helper containers on `ps`/`logs`/`down`. **No test
   covers any verb other than `config`/`images`/`runtime-images`/pinned `up`** (TA-1).
3. **All 7 `rigging.yaml` examples × 4 platforms render** (`config --format json`), are byte-deterministic across two
   runs, leave no trace of the old instance name after a rename (`certifyname0` -> `certifyname1`), and share no
   project/volume/network/container name between two instances. Only `core-driver` carries a healthcheck. This is
   what a repo-owned launcher contract test would pin (TA-2); CI today checks 2 of the 28 combinations' worth.
4. **`rigging.yaml` `replay.source.overrides` -> core config.** Applying the override block with rig's merge rules
   (C4: maps merge, `null` deletes) to each of the 7 examples: with explicit session lists the core's `parse_config`
   accepts every result and `sensor_env.py` emits the input bind. With `replay_input_sessions: []` — the value the
   contract documents as "empty means all" (SERVICE_CONTRACT §16.5) and that `rig bake --include-replay` actually
   renders (`rig_cli/bake_replay.py:216`) — **`parse_config` rejects it** (`replay.sessions: expected a nonempty
   list...`) while `sensor_env.py` accepts it. Harmless today (bake only runs launcher queries; live replay always
   passes explicit prefixes), but the two sides disagree and no test in this repo reads `rigging.yaml`'s override
   block at all (TA-4).
5. **shellcheck 0.9.0** over the 35 tracked shell scripts: 40 findings in 13 files, one `error`
   (`core-driver/tools/orin_rtsp_transport_ab.sh:50` SC2144, `-f` with a glob — a hardware script) and 9 warnings.
   No shell lint runs in CI (CI-7).
6. **Flakiness probe** — see TQ-1 and TQ-20 for the result of repeating the timing-sensitive files idle and under CPU load.

---

## 2. Coverage map

Rating scale: **none** (no automated test touches it) / **thin** (touched, but the main behaviour or failure paths
are not asserted) / **moderate** / **strong**. "CI smoke" = exercised only inside a `docker run` service smoke that
CI runs (asserts on logs/files, unmeasured). Percentages are line/branch from §1.3/§1.5.

### 2.1 `core-driver/cam_driver/`

| Module | Exercised by | Rating | One-line reason |
|---|---|---|---|
| `__init__.py` | (import) | n/a | constants only |
| `bayer_tile.py` | `test_replay_recording_changes.py` (rggb only), `test_proxy_export_gst.py` (lossy, tolerance) | **thin** | 65/58 %; the exact round-trip for all modes × patterns lives in `_selftest()` (`bayer_tile.py:100-121`), which nothing runs; `normalize_mode` branches unhit |
| `camera.py` (GigE/Aravis) | `test_camera_health_lock.py` (locking only); CI smoke `lifecycle_test.sh` (Aravis fake camera) | **thin** | 35/2 %; `open`, `configure`, `enable_chunks`, `enable_ptp`, `reopen`, `_on_control_lost` have no assertion-level test; chunk/PTP paths only in the non-CI `gvsp-chunk-emitter` scripts |
| `config.py` | `test_config.py`, `test_health.py`, `test_health_boson.py`, `test_proxy_export.py` + most GStreamer tests | **strong** | 98/96 %; defaults, coercion, named errors, run-registry resolution |
| `control_zenoh.py` | `test_control_zenoh.py` (fake session), `test_recording_resilience.py` (2 real peers), CI smoke `lifecycle_test.sh` | **moderate** | 82/83 %; adapter logic strong, but the real session open/retry timer (`_open_zenoh`, `start`) is untested in-process |
| `dropstats.py` | `test_dropstats.py` | **strong** | 100 % |
| `export_recordings.py` | `test_proxy_export.py`, `test_proxy_export_gst.py` | **moderate** | 85/77 %; happy paths + key rejections real; interrupted/partial-output cleanup, disk errors, resume not covered |
| `formats.py` | `test_formats.py`, `test_transport.py`, `test_ros2_source.py`, `test_ffv1_decode.py` | **strong** | 98/97 % |
| `gst_frame.py` | `test_replay_buffers.py` | **strong** | 95/75 %; ownership/lifetime checked with a real buffer pool |
| `health.py` | `test_health.py`, `test_camera_health_lock.py`, `test_health_boson.py`, `test_control_zenoh.py` | **strong** | 96/91 %; injected clock, staleness, windows, file output |
| `health_boson.py` | `test_health_boson.py` (pty-simulated camera) | **strong** | 90/79 %; framing + protocol end to end, no hardware |
| `health_genicam.py` | `test_health.py`, `test_camera_health_lock.py` (stub device) | **strong** | 93/96 % |
| `keyframe.py` | `test_keyframe.py`, `test_session.py` | **strong** | 97/94 % |
| `lifecycle.py` | `test_lifecycle.py`, `test_recording_settings.py`, `test_recording_resilience.py` | **strong** | 97/92 % |
| `pcapio.py` | `test_pcapio.py`, `test_uvcpcap.py` | **moderate** | 84/69 %; main container variants covered, many malformed-input branches not |
| `pipeline.py` | `test_pipeline_{pts,publish_rate,reconnect,session}.py`, `test_preview_clock.py`, `test_recording_*.py`, `test_replay_{buffers,recording_changes,shutdown}.py` | **moderate–strong** | 89/79 %; PTS/session/shutdown logic well pinned; the bus ERROR/EOS handlers, watchdog-driven reconnect loop and `run()` with a real main loop are mostly stubbed |
| `playback.py` | `test_playback.py`, `test_replay.py`, `test_replay_integrity.py`, `test_coordinated_playback.py`, `test_control_zenoh.py` | **strong** | 98/95 % (but see TQ-2: wall-clock assertions) |
| `recorder.py` | `test_recorder.py` (fake inventory), `test_recording_h264.py`, `test_lossless_colorimetry.py` (real) | **strong** | 94/85 %; the NVENC fragment is string-checked + colour step run, never encoded |
| `recording_settings.py` | `test_recording_settings.py`, `test_recording_resilience.py` | **strong** | 100/94 % |
| `replay_coordination.py` | `test_coordinated_playback.py`, `test_coordinated_sources.py` (via env) + sha256 pin | **moderate** | 80/67 %; happy path, abort, missing commit; malformed/stale command files and identity mismatches largely unhit |
| `session.py` | `test_session.py` (fakes), `test_recording_resilience.py`, `test_replay_shutdown.py` (real) | **strong** | 94/86 % |
| `sidecar.py` | `test_sidecar.py`, `test_recording_resilience.py` | **strong** | 93/72 %; failure path is the focus |
| `thermal_preview.py` | `plugins/webrtc-bridge/tools/test_thermal_preview.py`, `test_proxy_export_gst.py` | **strong** | 94/83 % |
| `timestamps.py` | `test_timestamps.py`, `test_replay.py` | **moderate** | 89/82 %; fallback ladder covered; frame-id/tick wrap and negative/absurd chunk values not |
| `transport.py` | `test_transport.py`, `test_ros2_source.py`, `test_shm_source.py` | **strong** | 94/83 % (Python side; the C++ mirror is only text-grepped, TQ-8) |
| `uvcpcap.py` | `test_uvcpcap.py`, `test_pcapio.py`, CI smoke `pcap_test.sh` | **strong** | 90/80 %; byte-exact synthetic captures with injected faults |

### 2.2 `core-driver/cam_driver/sources/`

| Module | Exercised by | Rating | One-line reason |
|---|---|---|---|
| `__init__.py`, `base.py` | via subclasses | moderate | 79 % (defaults of the base class) |
| `factory.py` | `test_config.py` (unknown type), `test_shm_source.py` (shm) | **thin** | 57/58 %; `make_source` is only checked for `shm` and an unknown type — a broken import for gige/usb/rtsp/pcap/replay would not fail any pytest |
| `gige.py` | `test_camera_health_lock.py` (stop/locking); CI smoke `lifecycle_test.sh` | **thin** | 45/10 %; frame callback, chunk parsing, reopen, disconnect detection untested in-process |
| `gstbase.py` | via `ShmSource` only | **thin** | 58/28 %; the shared appsink feeder / starvation watchdog / stamp matching for usb+rtsp is unasserted |
| `pcapsrc.py` | `test_coordinated_sources.py`; CI smoke `pcap_test.sh` | **moderate** | 73/59 %; only coordinated startup in-process; loop/restart/pause paths via smoke only |
| `replay.py` | `test_replay_source.py`, `test_replay_{buffers,integrity,recording_changes,shutdown,window_timing}.py`, `test_lossless_colorimetry.py`, `test_proxy_export_gst.py`, `test_coordinated_sources.py`; CI smoke `replay_test.sh` | **strong** | 79/61 %; the most thoroughly tested source, with real FFV1/H.264 files |
| `rtsp.py` | **no pytest**; CI smokes `rtsp_test.sh`, `rtsp_reconnect_test.sh` | **thin (smoke only)** | 0 %; NTP provenance not asserted on any lane (TQ-12); probe/no-probe, decoder selection, auth/URL handling untested |
| `shmsrc.py` | `test_shm_source.py` | **moderate** | 51/26 % (measured in-process); raw/header/unixfd framings and mismatch stops covered, error branches not |
| `usb.py` | **no pytest**; CI smokes `usb_test.sh`, `replay_test.sh` (fake `videotestsrc` only) | **thin (smoke only)** | 0 %; real-device branch (v4l2 caps, SOF timestamps, hot-unplug) only in the manual `orin_usb_hotplug_test.sh` |

### 2.3 Entry points

| Module | Exercised by | Rating | One-line reason |
|---|---|---|---|
| `core-driver/main.py` | subprocess in `test_replay_integrity.py:87-105`; every CI smoke | **thin** | 0 % measured; exit codes 2 (config/source/build error) and 1 (pipeline error) are asserted exactly once (`returncode != 0`), signal wiring only by smokes |
| `core-driver/supervisor.py` | `test_supervisor.py`; `supervisor_test.sh` (**not in CI**) | **thin** | 37 %; `__init__`, `_build_services`, `_resolve`, `run`, `_forward_to_core`, `_handle_plugin_exit`, `_teardown` kill path all untested, yet this is PID 1 on every vehicle |

### 2.4 `plugins/webrtc-bridge/tools/`

| Module | Exercised by | Rating | One-line reason |
|---|---|---|---|
| `bridge_stream.py` (1092 lines, the bridge itself) | **no automated test**; `webrtc_test.sh` (**not in CI**) | **none in CI** | 0 %; the whole WebRTC egress — pipeline assembly, format seam, latency metrics, signalling — is validated only by a manual script |
| `zenoh_advertiser.py` | **none**; `discovery_test.sh` (**not in CI**, needs host networking) | **none in CI** | 0 % |
| `check_encoder_setup.py`, `discovery_probe.py`, `webrtc_consumer.py` | none (test tooling) | none | 0 %; helpers of the manual scripts |
| `encoder_policy.py` | `test_encoder_policy.py` | **strong** | 100/94 % |
| `format_adapt.py` | `test_format_adapt.py` | **strong** | 100 % |
| `h264_level.py` | `test_h264_level.py` | **strong** | 92/80 % |
| `header_transport.py` | `test_header_transport.py` | **strong** | 100 %; hand-packed vectors, independent of the core's packer |
| `scale_plan.py` | `test_scale_plan.py` | **strong** | 100 %, includes a small property sweep |
| `thermal_preview.py` (shim) | `test_thermal_preview.py` | **strong** | 90/50 %; bundled-import path verified in a subprocess |

### 2.5 ROS plugins

| Module | Exercised by | Rating | One-line reason |
|---|---|---|---|
| `plugins/ros2-source/cam_ros2_source/framing.py` | `core-driver/tests/test_ros2_source.py` | **strong** | 97 %; header bytes compared with the core's packer |
| `plugins/ros2-source/cam_ros2_source/node.py` (344 lines) | **nothing** | **none** | 0 %; not imported by any test, no smoke script, never built or run in CI |
| `plugins/ros2-bridge/src/cam_bridge_base.{cpp,hpp}` | **nothing** | **none** | never compiled in CI; `bridge_test.sh` (not in CI) is the only exerciser |
| `plugins/ros2-bridge/src/cam_header_bridge.cpp` | text grep in `test_transport.py:84-95`; `bridge_test.sh` (not in CI) | **thin** | a `case N:` substring check — it cannot see a wrong mapping, a wrong offset or a compile error |
| `plugins/ros2-bridge/src/cam_unixfd_bridge.cpp` | text grep in `test_transport.py:98-119`; `bridge_test.sh` (not in CI) | **thin** | format strings must merely appear somewhere in the file |
| `plugins/ros1-bridge/src/cam_ros1_bridge.cpp` | text grep in `test_transport.py:84-95` | **thin → none** | no script runs it at all; no example config enables it, so `rig certify` never examines its image either |

### 2.6 Launcher, build and tools

| Module | Exercised by | Rating | One-line reason |
|---|---|---|---|
| `cam-up` | `tools/test_dev_defaults.py` (render per platform/tag), `tools/test_runtime_images.py` (queries, pins); CI `rig certify` (2 configs); `orchestration_test.sh` (**not in CI**) | **moderate** | `config`/`images`/`runtime-images`/pinned `up` well covered; **`ps`, `logs`, `down`, `pull`, unpinned `up`, `--zenohd`/`--roscore`, platform auto-detect, missing-config and missing-Compose errors untested** |
| `tools/sensor_env.py` | `core-driver/tests/test_sensor_env.py`; indirectly both `tools/test_*.py` | **strong** | 88 %; input bind, Bayer label, reserved keys, both YAML parsers |
| `tools/runtime_images.py` | `tools/test_runtime_images.py` | **strong** | inventory, pins, refusals, architecture; (unmeasured) |
| `tools/build-images.sh` | `tools/test_dev_defaults.py::test_build_matrix`, `test_shared_ros_base_build_matrix`, `test_build_ros_base_fallbacks…`; CI `rig certify --require-build-check` | **moderate** | captured `docker build` argv per platform; `PUSH=1`, `IMAGES=` subsets beyond `ros2-bridge`, L4T variants, failure propagation not asserted |
| `tools/rig-build.sh` | indirectly via `build-images.sh` in `test_dev_defaults.py` (`RIG_BUILD_PLATFORMS` -> `buildx build`) | **thin** | `RIG_BUILD_NO_CACHE`/`RIG_BUILD_PULL`, the `--push`/`--load` choice, the multi-arch+`PUSH=0` refusal and the `RIG_BUILD_RESULT` digest file have no repo test (certify's build check covers part) |
| `tools/export-recordings` | `core-driver/tests/test_proxy_export.py` (`runpy`, mocked `subprocess.run`) | **moderate** | 76 %; image selection + preflight; the real `docker run` conversion call path is mocked out |
| `tools/ci_test.sh`, `tools/ci_pytest.sh` | are the CI | n/a | see §4 |
| `tools/zenohd.sh`, `tools/roscore.sh`, `tools/package-launch-surface.sh`, `tools/jp6-modern/*`, `tools/gstreamer/{build.sh,check.py}` | `test_gstreamer_archives.py` covers `extract_archive` in `build.sh`; the rest **nothing** | **none** (except the extractor) | `tools/jp6-modern/{probe.sh,stack-check.sh,cam-up-jp6m}` ship to vehicles via `launch_surface` (`rigging.yaml:109-111`) untested |

### 2.7 Modules with NO automated test at all (greps over `core-driver/tests`, `plugins/webrtc-bridge/tools/test_*.py`, `tools/test_*.py`, `.github/`)

- `plugins/webrtc-bridge/tools/bridge_stream.py`, `zenoh_advertiser.py` (and helper tools `webrtc_consumer.py`,
  `discovery_probe.py`, `check_encoder_setup.py`) — manual scripts only.
- `plugins/ros2-source/cam_ros2_source/node.py` — nothing, not even a manual script.
- All C++: `cam_bridge_base.cpp/.hpp` (nothing); `cam_header_bridge.cpp`, `cam_unixfd_bridge.cpp`,
  `cam_ros1_bridge.cpp` (text grep only; never compiled or run by CI).
- `tools/zenohd.sh`, `tools/roscore.sh`, `tools/package-launch-surface.sh`, `tools/jp6-modern/{cam-up-jp6m,probe.sh,stack-check.sh,export-images.sh}`.
- In-process (pytest) but not in any way asserted: `cam_driver/sources/usb.py`, `cam_driver/sources/rtsp.py`,
  `core-driver/main.py` — these are reached only by CI service smokes that assert on log lines and output files.

---

## 3. Test quality problems

Ordered roughly by how much they undermine confidence in recorded-data fidelity and in a green CI.
Paths are relative to the repo root; `tests/` means `core-driver/tests/`.

### TQ-1 — A reproducible flaky test in `test_shm_source.py`: `time.sleep` stands in for "reader connected" **[ran]**

Repeating seven timing-sensitive files on an otherwise idle machine, repetition 6 of 6 failed:

```
FAILED core-driver/tests/test_shm_source.py::test_header_frames_carry_the_writers_stamp_id_and_provenance
core-driver/tests/test_shm_source.py:85: AssertionError: timed out after 10.0s
```

Cause, by reading: the writer is built with `shmsink ... wait-for-connection=false` (`tests/test_shm_source.py:42-43`),
which drops buffers while no client is attached. The test starts the reader and immediately pushes exactly three
frames 50 ms apart, then waits for three (`:123-128`):

```python
src.start(c.on_frame)
for i in range(3):
    writer.push_header_frame(100 + i, t0 + i * 50_000_000, ts_source="ptp_chunk", fill=i)
    time.sleep(0.05)
_pump(lambda: c.count() >= 3)
```

If `shmsrc` has not finished connecting when the first push lands, that frame is gone and the count can never reach
3. The same file already contains the correct pattern for unixfd — `_push_until` (`:245-255`, "unixfdsink drops
until the reader connects: keep pushing ... until n arrived") — but the shm/header path does not use it. The second
half of `test_a_frame_that_does_not_match_the_pinned_config_stops_legibly` pushes a single mismatching header frame
right after `start` (`:187-188`) and has the same race. `docs/TESTING.md:103` records "one shm shutdown timeout" on
the 1.20 lane that was never explained; this file is the obvious place to look, and with `--timeout-method=thread`
a hang there takes the whole run down (CI-9). Fix: push until delivered, or wait for a connection signal
(`shmsink` `client-connected`).

### TQ-2 — The two unexplained failures in `test_replay_recording_changes.py`: what can make it flaky **[read]**

`docs/TESTING.md:113-116`: two failures "involving decoded-frame/CSV counts", passing in isolation and in 20
single-test repetitions, "cause was not established". The doc does not even name the two tests. Constructs in the
file that can produce exactly that symptom:

1. **Sleep-as-handshake before an exact-equality assertion.** `tests/test_replay_recording_changes.py:295`
   `time.sleep(.1)  # allow the transport subscriber to connect`, then `:304`
   `assert received == [(st.frame_id, st.timestamp_ns, data) for st, data in expected]`. A consumer that needs more
   than 100 ms to attach loses the first frames and the counts differ. Same construct in
   `tests/test_replay_buffers.py:281` (`time.sleep(0.1)  # let the local socket clients connect before the first frame`)
   feeding `:299` `assert [r[0] for r in received] == [0, 2, 4, 6, 8]`.
2. **Order dependence the reproduction strategy cannot see.** Every GStreamer test shares the process-global
   `GLib.MainContext.default()`; `_pump` (`tests/test_replay_source.py:107-117`) dispatches *whatever* is pending in
   it, including idle callbacks and bus watches left by an earlier test. There is no `conftest.py`, no fixture that
   drains the context or asserts that no source/reader thread survived a test, and most tests stop their sources
   only on the success path (TQ-6). "Passes in isolation, passes 20× alone, fails once in the full suite" is the
   signature of this, and neither isolation nor single-test repetition exercises it. Only repeated *full-suite*
   runs in randomized order would.
3. **An unpinned frame-threaded H.264 decoder.** The 1.24 FFV1 loss was fixed by pinning
   `avdec_ffv1 thread-type=slice` (`cam_driver/formats.py:38`). The H.264 path — which 7 of the 8 tests here cross
   (`{"encoder": "x264", ...}` at `:123-124`, `:172`, `:185`, `:200`, `:217`, `:258`) — still decodes with a bare
   `h264parse ! avdec_h264` both in the test (`:87`) and in replay (`cam_driver/sources/replay.py:68`). Whether
   libav's frame threading can also drop/duplicate at EOS or at a part boundary for H.264 is not established here;
   it is the first hypothesis I would test, with the 24-case matrix of `test_ffv1_decode.py` repeated for x264.
4. **A known reader race, guarded only by repetition.** `:203-206`: "Short H.264 fragments exposed a splitmuxsrc
   prepare/seek race: a duplicate first buffer shifted CSV stamps ... Reopen the exact same recording repeatedly"
   (`for _ in range(20)`). A race whose regression test is "loop 20 times and hope" will recur at low frequency.
5. **Wall-clock pacing assertion on an absolutely anchored pacer.** `:192`
   `assert received[3][2] - received[2][2] >= .28`. `Pacer.wait` schedules against a fixed baseline
   (`cam_driver/playback.py:290` `target = self._t0_wall + int((ts_ns - self._t0_src) / self.speed)`), so if frame 2
   is delivered 90 ms late the 2→3 gap shrinks below 0.28 s although nothing is wrong.
6. **`collect()` treats a 10 s pull timeout as end-of-stream** (`tests/test_recording_h264.py:29-33`): a `None`
   sample is followed by a 2 s bus pop; under heavy load this becomes a "decode timeout" assertion rather than a
   count mismatch, but it is the same shared helper six test files import.

The probe results are in TQ-20 at the end of this section.

### TQ-3 — Wall-clock deadlines as assertions (pure-Python tests that fail on a slow runner) **[read]**

`Pacer` and `PlaybackState` take real time; the tests assert narrow real-time windows instead of injecting a clock
(`tests/test_replay_window_timing.py:35-36` shows the patching approach works):

| Location | Assertion |
|---|---|
| `tests/test_playback.py:21` | `assert 0.03 < (time.monotonic_ns() - t0) / 1e9 < 0.2` |
| `tests/test_playback.py:25`, `:36`, `:269`, `:317` | upper bounds of 0.2 s after a 50 ms sleep |
| `tests/test_playback.py:118` | `assert 0.1 < time.monotonic() - t < 0.9` |
| `tests/test_playback.py:280-281` | `time.sleep(0.5)` then `assert pb.state == PLAYING` (gate armed 0.2 s ahead) |
| `tests/test_playback.py:324-326` | `time.sleep(0.12)` then `assert 0.08 <= d["position_s"] <= 0.4` |
| `tests/test_playback.py:333-334` | same window on a second object |
| `tests/test_replay.py:177` | `assert 0.002 < dt < 0.5, dt` |
| `tests/test_replay_source.py:136` | `assert 0.25 < gap_wall < 1.5, gap_wall` |
| `tests/test_replay_source.py:158`, `:160` | `assert 0.45 < first < 1.5`, `assert 0.2 < gap_wall < 0.9` |
| `tests/test_proxy_export_gst.py:218`, `:225` | `assert .015 < delay < .8`, `... - restarted < .8` |
| `tests/test_replay_shutdown.py:98` | `assert elapsed < 2, f"close exceeded its drain budget..."` (plus a 4 s rescue `threading.Timer`, `:91`) |
| `tests/test_coordinated_sources.py:128` | `self.assertLess(time.monotonic()-started,1)` |
| `tests/coordination_fixture.py:52` | `at = time.time() + .5` — the release instant is fixed *before* two acknowledgement round trips; a participant slower than 0.5 s makes `test_coordinated_playback.py:43` `self.assertEqual(output, [])` fail |

### TQ-4 — "Nothing happened" proved by sleeping **[read]**

Negative assertions that wait a fixed time and then check a counter can only fail if the bug is fast:

- `tests/test_replay_source.py:191-195` — the hold check loops `_pump(lambda: True)` for 0.4 s. `_pump` is
  `while not until(): ...` (`:112`), so with `lambda: True` it **never iterates the main context**; the loop is a
  plain 0.4 s sleep and `assert c.count() == 1` observes only what the streaming thread did by itself.
- `tests/test_replay_buffers.py:181-182` — `time.sleep(0.1)` then `assert len(frames) == 1`.
- `tests/test_coordinated_sources.py:34-35` — `time.sleep(.15)` then `self.assertEqual(raw,[])`.
- `tests/test_camera_health_lock.py:68`, `:177` — `assert not controlled.wait(0.05)`: 50 ms to prove a lock is held.
  (The reader thread's own `assert not controlled.is_set()` at `:54` is the real check; the 50 ms wait is decoration.)
- `tests/test_playback.py:191-192`, `:302-303` — `t.join(0.3)` / `t.join(0.2)` then `assert t.is_alive()`.

### TQ-5 — Assertions that cannot fail, or that test the test **[read]**

- `tests/test_control_zenoh.py:373` — `assert ctl.advertise() is False or True   # never raises after close`.
  `X is False or True` is always true; the line only fails if `advertise()` raises. The intended contract ("advertise
  after close is refused / does not reopen") is not asserted.
- `tests/test_health.py:355-357` — `m.stop(); n = prov.n; assert not m._thread and prov.n == n`. `n` is read and
  compared with itself on the next line; the "stops polling" half of `test_the_thread_polls_and_stops_promptly`
  cannot fail.
- `tests/test_coordinated_sources.py:117` — `self.assertEqual(self.control.wait('failed')['state'],'failed')`:
  `wait(state)` (`tests/coordination_fixture.py:38-46`) only returns once the state equals its argument.
- `tests/test_transport.py:28-34` (`test_header_then_pixels`) — `assert blob[HEADER_SIZE:] == pixels` where
  `blob = h.pack() + pixels` was built two lines earlier; and `parsed.width * parsed.height == len(pixels)` is `4 == 4`.
- `tests/test_coordinated_playback.py:76-80` (`test_packaged_client_matches_canonical_protocol_hash`) compares
  `core-driver/cam_driver/replay_coordination.py` with `tools/replay_coordination.sha256` — two files in *this*
  repo that are edited together. It cannot detect drift from the canonical copy in the infra repo
  (`ros2-bag-player/tools/replay_coordination.py`; the three hashes are equal today **[ran]**).

### TQ-6 — No teardown on the failure path; process-global state shared by every GStreamer test **[read]**

- No `conftest.py` exists anywhere; no fixture owns pipeline/source lifetimes. All six tests in
  `tests/test_shm_source.py` and most of `tests/test_replay_source.py` (`:124-143`, `:146-162`, `:165-181`,
  `:204-219`, …) call `src.stop()` / `writer.stop()` only as the last statement. Any failing assertion leaves a
  PLAYING pipeline, its streaming threads and its bus watch alive for the rest of the pytest process, where the
  next test's `_pump` services them.
- Production module globals are swapped by hand: `tests/test_session.py:34-41` replaces
  `session_mod._wrap_buffer`, `_caps_from_string` **and `session_mod.GLib`**; `tests/test_pipeline_session.py:35-40`
  replaces `pipeline_mod._new_buffer`; `tests/test_recorder.py:56` does `rec.Gst = _FakeGst(present)` (restored in
  `teardown_function`, `:48-50`). A reader thread leaked by an earlier real-GStreamer test would run against those
  fakes.
- `tests/test_recording_resilience.py:328-354` measures process-wide fd count, thread count and RSS; its verdict
  depends on what earlier tests in the same process leaked or are still releasing. The bounds are loose:
  `samples[-1][2] <= samples[0][2] + 64 * 1024 * 1024` (`:353`) lets ~640 kB per session leak across 100 sessions.
- Temp directories created at import or per test and never removed: `tests/test_uvcpcap.py:21`,
  `tests/test_pcapio.py:15` (`_TMP = tempfile.mkdtemp(...)` at module scope), `tests/test_replay.py:42,53,67,75,101`,
  `tests/test_shm_source.py:33`, `tests/test_replay_source.py:121`.
- Import-order coupling: `tests/test_replay_window_timing.py:10-11` and `tests/test_coordinated_sources.py:8-10`
  import `cam_driver` without touching `sys.path`; it resolves only because the preceding line imports
  `test_replay_source`, whose side effect (`tests/test_replay_source.py:18`) inserts `core-driver`.
- Test modules import each other as libraries (`from test_recording_h264 import collect` in six files;
  `from test_control_zenoh import _Session, _Query` at `tests/test_recording_settings.py:25`;
  `from test_replay_source import ...` in eight), so importing one test module runs another's module-level
  `Gst.init(None)` and path edits, and a syntax error in a helper-bearing test file takes down a third of the suite
  at collection.

### TQ-7 — Over-mocking: the tests that pin shutdown/recording order never touch GStreamer **[read]**

- `tests/test_session.py` fakes the pipeline, bus, appsrc and sidecar (`:48-178`) and replaces `GLib.idle_add` with
  an inline call (`:37`). Its central assertion is a call-order list on those fakes — `:316-317`
  `assert order == ["remove", "pop", "state:null", "sidecar-stop", "summary"]`. Real idle scheduling, real bus
  message ordering and real EOS propagation are outside it (they are partly covered by
  `test_recording_resilience.py` / `test_replay_shutdown.py`).
- `tests/test_pipeline_session.py:171-201` builds `CapturePipeline` from `SimpleNamespace` configs, swaps
  `_recorder_desc`, `_session_factory`, `appsrc`; `:392` asserts
  `events == ["begin:1", "source-stop", "eos:camsrc", "finish:1"]`. `_FakeSession.finish_close` (`:138-143`)
  always returns `"truncated": False`, so the pipeline's handling of a truncated/errored finalize is not reachable.
- `tests/test_supervisor.py:46-53` constructs `Supervisor.__new__(Supervisor)` and feeds `_FakeProc` objects that
  have already exited. No child process is ever spawned, signalled or killed: `run()`, `_build_services()`,
  `_resolve()`, `_forward_to_core()` (SIGUSR1/2), `_handle_plugin_exit()` (restart/backoff/give-up) and the
  `kill()` branch of `_teardown()` (`core-driver/supervisor.py:244-247`) are untested, 37 % coverage (§1.5).
- `tests/test_recorder.py` replaces `rec.Gst` with an element inventory (`:27-39`) and asserts substrings of the
  launch string (`:67` `assert "nvv4l2h265enc enable-lossless=1" in d`). The NVENC fragment is never parsed by
  GStreamer in CI, so a misspelt property on `nvv4l2h265enc`/`nvvidconv` passes every lane and fails at the first
  activation on a vehicle. (`test_lossless_colorimetry.py:107-116` does run the CPU colour step for real — good.)
- `tests/test_proxy_export.py:206-218` patches `subprocess.run` and then asserts
  `run.assert_called_once_with(["docker", "image", "inspect", image], ...)`; the actual conversion `docker run`
  argv of `tools/export-recordings` is never asserted.

### TQ-8 — The C++ bridges are "tested" by substring search **[read]**

`tests/test_transport.py:84-95`: `missing = [c for c in sorted(_CODE_TO_GST) if f"case {c}:" not in src]`;
`:107` `missing = [f for f in sorted(_GST_RAW) if f'"{f}"' not in src]`; `:118` the same for Bayer patterns.
These pass if the token appears anywhere in the file — in a comment, in the wrong switch, mapped to the wrong
encoding (code 3 is big-endian mono16; nothing checks that). The 36-byte header layout itself (magic, offsets,
`ts_source` codes, endianness) is mirrored by hand in two C++ files and is not checked at all; the Python mirrors
are (`test_header_transport.py`, `test_ros2_source.py:22-34`). No C++ is compiled or executed by CI (CI-5).

### TQ-9 — Names and docstrings that promise more than is asserted **[read]**

- `tests/test_supervisor.py:69-72` `test_run_returns_the_propagated_code_not_zero` calls `sup._monitor()` and
  asserts `sup._exit_rc != 0`; `run()` — where the hard-coded `return 0` lived — is never called.
- `tests/test_sidecar.py:3`: "The happy path is covered end-to-end by tools/dev_test.sh (it counts CSV rows against
  the .mkv)". `core-driver/tools/dev_test.sh:70-78` checks `[ "$ROWS" -gt 0 ]` and that the mkv decodes to EOS; it
  never compares the two, and it is not run by CI. `test_healthy_writer_records_no_failure_flag` itself asserts only
  `len(rows) == 2` (`tests/test_sidecar.py:100`) — the CSV column order and formatting that replay, export and rig
  all parse are pinned nowhere directly.
- `core-driver/tools/replay_test.sh:37-44` "decoded frames must be BIT-IDENTICAL": it `cmp`s the original decode
  against the re-recorded decode. Neither is compared with `ROWS × frame size`, so a decoder that drops the last
  frame of *both* files (exactly the gst-libav 1.24.2 defect) still passes; `:117-118` and `:141` likewise.
  `pcap_test.sh:48` does it properly (compares against bytes computed from the fixture).
- `core-driver/tools/rtsp_test.sh:4` claims to prove "the RTCP->NTP timestamp provenance". `:41-50`: when no row has
  `source == "rtp_ntp"` the script prints "arrival-only (system): expected on gst<1.24" and passes — on every lane,
  including 1.24 and 1.28 where NTP provenance is the feature.
- `usb_test.sh:25-27`, `dev_test.sh:75-77`: "decode FFV1 mkv (must reach EOS, no error)" — pipes to `fakesink`,
  greps "got eos"; no frame count, no pixel check.
- `tests/test_pipeline_session.py:435-442` `test_session_drain_fits_inside_the_core_stop_budget` computes
  `worst = max(5.0, SESSION_DRAIN_S + 5.0) + _RECONNECT_JOIN_S`. The two literal `5.0`s stand for
  `GLib.timeout_add_seconds(5, self._force_quit)` (`cam_driver/pipeline.py:1083`) and `self._thread.join(timeout=5)`
  (`cam_driver/sidecar.py:192`); changing either production value leaves this "cross-file invariant" green.
- `docs/TESTING.md:58` describes the repeated-session test as keeping "current RSS bounded"; the bound is 64 MiB.

### TQ-10 — Silent passes that bypass the "skipped tests fail the gate" rule **[read]**

`tools/ci_pytest.sh:30-36` fails the run on junit `skipped`. These return early instead, so they are counted as
passes:

- `tests/test_shm_source.py:259-260`, `:287-288` — `if not _unixfd_available(): return`. On the `legacy`
  (GStreamer 1.20) lane both unixfd input tests are vacuous.
- `tests/test_supervisor.py:126-127`, `:142-143` — `if not os.path.exists(...): return`.
- `tests/test_transport.py:92-93` (`continue`), `:105-106`, `:116-117` (`return`) when `plugins/` is not reachable.
- `plugins/webrtc-bridge/tools/webrtc_test.sh:148-153`, `:217-222` print "SKIPPED" for half the scenarios and still
  exit `WEBRTC TEST: PASS`.
- `tools/test_runtime_images.py:21` — `@unittest.skipUnless(DOCKER, ...)` on the whole class; the `dev-contracts`
  job has no skip gate, so a runner without `docker` would report `OK (skipped=10)`.

### TQ-11 — Tests that only the pytest runner sees; a stale second runner **[read]**

Most files end with a hand-rolled `_main()`. Functions defined after the `if __name__ == "__main__": _main()` call
are invisible to it: `tests/test_config.py:461-470` (1 test) and `tests/test_sensor_env.py:297-355` (4 tests,
including `test_a_yaml_param_cannot_forge_a_device_mapping`). `core-driver/tools/dev_test.sh:18-39`
runs 22 files this way and has not been updated for the other ~20; `tools/gvsp-chunk-emitter/gvsp_test.sh:14-16`
runs three. Two runners with different coverage is a maintenance trap; delete `_main()` or make the scripts call
pytest.

### TQ-12 — Smoke scripts synchronize with fixed sleeps and assert on log text **[read]**

Every CI smoke uses `sleep N` as its only handshake (`lifecycle_test.sh:34,45,48,71,85,86,90`,
`replay_test.sh:18,54,80-81,130,153`, `usb_test.sh:17,39,82-84`, `rtsp_test.sh:22,25`,
`rtsp_reconnect_test.sh:20-25`). Two are tight enough to matter on a loaded runner:

- `lifecycle_test.sh:48-49` — `kill -USR2 "$CORE"; sleep 2` then requires the state file to read `inactive`; the
  finalize may legitimately take up to `SESSION_DRAIN_S` (5 s).
- `lifecycle_test.sh:53` — `[ "$N1" -ge 2 ] && [ "$N1" -le 4 ]` segments "for ~5s at segment_seconds=2"; the
  recorded duration is `sleep 5` plus however long the probe at `:46` takes.

They also all bind-mount `core-driver` read-write (`-v "$PWD/core-driver:/app"`) and run without
`PYTHONDONTWRITEBYTECODE`, so the smokes write `__pycache__` into the checkout as root.

### TQ-13 — An orphaned test and an unrun self-test **[ran]**

- `core-driver/tests/check_transport_caps.py` contains `test_native_transport_caps_describe_the_decimated_rate`
  but is named so that pytest does not collect it (docstring: "Kept outside test_*.py unit collection, whose runner
  does not ship these media plugins"). The CI images on the 1.24/1.28 lanes do ship `unixfdsink`; run by hand here
  it passes. Nothing references the file (`grep -rn check_transport_caps` finds only itself). It is the only test
  of the advertised transport frame rate under decimation.
- `cam_driver/bayer_tile.py:100-121` `_selftest()` is the only exact round-trip check of the reversible CFA tiling
  over all four patterns × all modes × three geometries. It runs only via `python3 bayer_tile.py`; no script, test
  or CI step calls it (lines 100-121 are uncovered). The pytest coverage of the tiler is one pattern (`rggb`) at
  64×48 (`tests/test_replay_recording_changes.py:167-180`) plus tolerance-based colour checks through lossy H.264
  (`tests/test_proxy_export_gst.py:231-304`, `assert error.mean() < 10`).

### TQ-14 — Fixtures that route around the behaviour under test **[read + api]**

`tests/test_proxy_export_gst.py:35-51` installs a pad probe *after the real FFV1 encoder* that overwrites every
buffer's PTS with the fixture's intended value, because "gst-libav 1.20 rounds to its 1/25-second codec timebase:
it removes this fixture's jitter and shifts the 2.23-second gap by 10 ms BEFORE Matroska" (`:39-43`). History: CI
run #98 on `cf3fe39` failed **only on the `legacy` (GStreamer 1.20) lane**, in the pytest step; the next commit,
`f36f633` "test(proxy): preserve fractional fixture PTS" — which changes two test files and a doc, no production
code — made run #99 green 14 minutes later and was tagged v1.11.0. Its message says "Legacy gst-libav snaps FFV1
PTS to the advertised frame-rate grid before muxing" and `docs/proxy-exports.md:48-52` now documents that such
recordings "are rejected".

So the limitation is known and documented, but the test suite no longer observes it: the export validator's 1 ms
tolerance is only ever exercised against timestamps the *test* wrote, and every other recorder test uses perfectly
regular 40 ms stamps (`IV = 40_000_000`). What nobody can read off CI is the question an operator will ask: does a
recording made by the real recorder (`appsrc ! videoconvert ! avenc_ffv1 ! splitmuxsink`) on a 1.20 / JP6 vehicle,
with ordinary camera jitter or a non-25 fps rate, export or get rejected — and with what message? If the answer is
"rejected", FFV1 recordings from the JP6 fleet (16-bit thermal and colour always use FFV1) cannot be exported, and
that should be a named, asserted behaviour on the legacy lane rather than something a fixture probe papers over
(TA-2).

### TQ-15 — Tests that pin permissive behaviour instead of rejecting bad input **[ran]**

- `tests/test_config.py:112-114` `test_unknown_keys_ignored` and `tests/test_formats.py:50`
  `assert select_encoder("bogus", 8) == "hw-hevc-lossless"   # unknown -> auto` make silent acceptance the contract.
  Probing `parse_config` here: `recording: {encoder: bogus}`, `recording: {segment_seconds: -5}`,
  `recording: {enabeld: false}` (typo'd key), a typo'd top-level section, `camera: {frame_rate: -1}`,
  `gige: {timestamp_source: bogus}`, `usb: {width: 0, height: -4}`, `transport.plugin_endpoint.max_rate_hz: -3` and
  `camera: {type: GigE}` are **all accepted** at parse time. The comment at `tests/test_config.py:172` says "a YAML
  typo must fail fast, naming the field"; only numeric *type* errors do. A mistyped `output_dir` or `encoder` on a
  vehicle silently records somewhere/somehow else. (Runtime `apply_patch` does validate — `tests/test_recording_settings.py:61-74` —
  the boot config does not.)
- `tools/sensor_env.py:266` takes `name` verbatim. A name with spaces or shell metacharacters is safely quoted
  (verified: no execution) but flows into `COMPOSE_PROJECT_NAME`/`CAM_SOCK_VOLUME` and fails later inside Compose;
  the contract (C3) wants a legible non-zero refusal. No test feeds an invalid or hostile `name`, and although
  `tests/test_sensor_env.py:35-37` says the shlex round trip means "the quoting is exercised", no test value
  actually needs quoting.

### TQ-16 — Missing negative / boundary cases worth adding (by module) **[read, partly ran]**

| Area | Missing case |
|---|---|
| `pipeline._pts_for` | behaviour exactly at `_MAX_PTS_JUMP_NS` (tests use +60 s above and 18.4 s below, `tests/test_pipeline_pts.py:110,121`); a *forward* jump followed by a backward one; non-monotonic stamps inside one interval |
| `timestamps.py` | GVSP 16-bit block-id wrap vs chunk frame id; chunk timestamp 0 / decreasing; `tick_frequency_hz` not dividing 1e9 (rounding) |
| `transport.FrameHeader` | width/height 65536 raises `struct.error`, not `TransportError` **[ran]**; negative timestamp; frame_id ≥ 2⁶⁴ |
| `sidecar.py` | row content/column order; a writer that fails *mid-file* after N good rows (only open-failure is simulated, `tests/test_sidecar.py:32-41`) |
| `recorder.py` | `segment_seconds` → `max-size-time` value; 16-bit Bayer; odd widths for the mono→I420/NV24 pin |
| `lifecycle.py` | unwritable/full state-file directory on `remember`; partial state file |
| `control_zenoh.py` | non-UTF-8 / oversized payload; a query racing `close()` for the lifecycle keys (only health has it, `tests/test_control_zenoh.py:569`) |
| `export_recordings.py` | pre-existing destination, interrupted conversion leaving partial output, ENOSPC mid-encode, profile `revision` mismatch |
| `replay.py` | GRAY16 with CFA tiling; a session whose first part is missing but the attestation has no `segments`; restart while paused at the *first* frame |
| `uvcpcap.py` | 10/12-bit and packed formats; MJPEG frame with embedded `FFD9` in a thumbnail; a capture with two video endpoints |
| `supervisor.py` | plugin crash-loop reaching `RESTART_MAX`; core ignoring SIGINT (kill path); SIGUSR forwarding; signal during startup stagger |
| `cam-up` | missing config, missing Compose plugin, `RIG_TARGET_PLATFORM=bogus`, `--source` without `--dev`, conflicting platform flags |

### TQ-17 — Duplicated helpers and builders **[read]**

- `_pump`: `tests/test_replay_source.py:107-117` and `tests/test_shm_source.py:78-86` (different default timeouts),
  plus `pump()` at `tests/test_coordinated_sources.py:15-16`.
- `_Consumer`: `tests/test_replay_source.py:74-93` and `tests/test_shm_source.py:64-75`.
- `_stamp`: `tests/test_pipeline_pts.py:45`, `tests/test_pipeline_session.py:204`, `tests/test_session.py:181`.
- Fake appsrc: `tests/test_pipeline_session.py:86-101` and `tests/test_session.py:48-75`.
- Four independent builders of the on-disk recording layout with different header fields:
  `_write_session` (`tests/test_replay_source.py:40-71`), `_make_run` (`tests/test_replay.py:28-38`),
  `_fake_session` (`tests/test_playback.py:213-225`), `session` (`tests/test_proxy_export.py:27-36`) — plus
  `_write_segmented_session` (`tests/test_proxy_export_gst.py:19-82`). None is produced by the real
  `SidecarWriter`, so a sidecar schema change can leave replay/export tests green against a layout the recorder no
  longer writes.
- Identical test in two files: `test_pacer_speed_zero_never_sleeps` (`tests/test_playback.py:39`, `tests/test_replay.py:163`).
- Every file repeats `sys.path.insert(0, ...)`; a 10-line `conftest.py` would remove ~45 copies and give the
  suite a place for shared fixtures (main-context drain, leak check, tmp dirs).

### TQ-18 — The hardware qualification tool mirrors the recorder by hand **[read]**

`core-driver/tools/nvenc_lossless_test.py:40-48` rebuilds what its comment calls the "EXACT recorder
hw-hevc-lossless fragment" as a literal argv (`nvv4l2h265enc enable-lossless=1 maxperf-enable=1`), and `:36`
copies `LUMA_COLORIMETRY`. `tests/test_nvenc_validation.py` tests the verdict logic with `gst-launch` replaced and
`tests/test_lossless_colorimetry.py:141-153` tests `decode_argv()`; nothing asserts that `encode_argv()` still
equals what `rec.build_recorder_description(RecordingConfig(encoder="hw-hevc-lossless"), 8, ...)` produces. If the
recorder's NVENC settings change, the Jetson promotion gate (`docs/TESTING.md:169-172`) qualifies a pipeline the
vehicle no longer runs.

### TQ-19 — Hermeticity: a "no router, no network" test that needs IPv6 **[ran]**

`tests/test_recording_resilience.py:254-258` opens the two client peers without `listen/endpoints`, so zenoh binds
`tcp/[::]:0`. On a kernel without IPv6 both control-race tests fail in the fixture (§1.2); with an IPv4 loopback
listen endpoint they pass (§1.6). Give the clients `mode: "client"` or an explicit `tcp/127.0.0.1:0` listener.

### TQ-20 — Flakiness probe results **[ran]** (GStreamer 1.24.2, 4 cores)

Seven timing-sensitive files (`test_replay_recording_changes.py`, `test_shm_source.py`, `test_playback.py`,
`test_replay_source.py`, `test_coordinated_playback.py`, `test_coordinated_sources.py`, `test_replay_buffers.py`;
83 tests) were repeated 6× on an idle machine and 8× with eight busy-loop processes (2× CPU oversubscription):

| Condition | Repetitions with a failure | Failing tests |
|---|---|---|
| idle, 7-file batch | 1 of 6 | `test_shm_source.py::test_header_frames_carry_the_writers_stamp_id_and_provenance` |
| loaded, 7-file batch | 7 of 8 | the same test (6×) and `test_shm_source.py::test_a_frame_that_does_not_match_the_pinned_config_stops_legibly` (5×) |
| loaded, `test_shm_source.py` alone | 8 of 8 | the same two; tracebacks end at `test_shm_source.py:128` and `:189` — the two waits that follow a push made right after `src.start()` |
| idle, `test_shm_source.py` alone | 0 of 8 | – |

Two conclusions. First, TQ-1 is a real, load-sensitive flake located exactly where reading predicted, and its
rate is high enough (1 in 6 batches on an idle 4-core box) that it will fail a CI lane sooner or later; the
original full-suite run and the v1.13.0 CI run simply got lucky. Second, **the hypotheses of TQ-2 were not
reproduced**: `test_replay_recording_changes.py` and the wall-clock tests of TQ-3 passed in all 14 batches, so at
this load their margins hold. They remain risk constructs — the recorded failures happened during image builds on
an arm64 laptop VM, a heavier and burstier load than this probe — but I have no reproduction, and the one
mechanism this probe could not exercise is the order dependence across the *whole* suite (TQ-2 item 2).

---

## 4. CI gaps

Workflows: `.github/workflows/certify.yml` (named `ci`; jobs `unit` ×4 lanes, `dev-contracts`, `certify`) and
`.github/workflows/registry-release.yml`. There is no other automation.

### CI-1 — The release branch is not in the push trigger; three releases were tested only because someone pressed a button **[read + api]**

`certify.yml:3-7`:

```yaml
on:
  push:
    branches: [main, jp6-modern]
  pull_request:
  workflow_dispatch:
```

- `v1.11.0` (f36f633), `v1.12.0` (6316f7a) and `v1.13.0` (6d3af8b) are tagged on `codex/gstreamer-1.28.8`; none is an
  ancestor of `origin/main`, which still sits at `v1.10.0` (ec11fbc). `jp6-modern`, the other listed branch, is
  absent from the remote refs of the local clone (**[ran]** `git branch -r`; it was merged by PR #68 on 2026-09-17).
- Run history between the last push to `main` (2026-10-15) and v1.13.0 (2026-10-30): four `workflow_dispatch` runs on
  the release branch — #98 cf3fe39 **failure**, #99 f36f633 success (14 min later; then tagged v1.11.0), #100 6316f7a
  success (v1.12.0), #102 6d3af8b success (v1.13.0, all six jobs green) — plus one `pull_request` run for PR #70's
  head (c5b862b, a PR *into* the release branch). No PR from the release branch to `main` exists.
- 18 commits separate `main` from v1.13.0. CI ran on 4 of those SHAs. **13 commits were never built or tested as
  pushed**, including `4a9daa6` "preserve FFV1 frames at end of replay", `59d36eb`/`71fd741`/`789f013` (the whole
  export feature), `2c2dde7`, `9610e54` "keep mono/Bayer codes exact through the YUV step" and `c1864b4`; the merge
  commit of PR #70 (`6fa5dcf`) was also never tested as merged.

Consequences: (a) a release can be tagged from a commit that never ran CI — nothing prevents it; (b) when a manual
run is red there is no per-commit history to bisect with; (c) required status checks cannot protect a branch that
pushes do not trigger on; (d) every PR against `main` (e.g. open PR #71) is validated against a base 18 commits
behind what vehicles run. Fix: add the release branch (or a `release/**` / `codex/**` pattern) and `tags: ['v*']`
to `push`, and stop releasing from a branch that is not the default.

### CI-2 — `registry-release.yml` publishes without any dependency on CI **[read]**

`registry-release.yml:26-31` triggers on `release: published` and on `workflow_dispatch` with a typed version. Its
single job `update-registry` (`:42-133`) has no `needs:`, does not query the commit's check runs, and writes
`source.rev: ${GITHUB_SHA}` (`:110`) into the public fleet registry, then `git push` (`:133`). A release cut from a
red or untested commit is published to every consumer; the dispatch path "publishes the current commit of the
chosen ref under the version you type" (`:22-23`) with even less ceremony. The only validation in the job is the
registry's own `./tools/validate` (`:117`), which checks manifest shape, not this service. Minimum fix: a first step
that fails unless the `ci` workflow concluded `success` for `GITHUB_SHA` (`gh api repos/.../commits/$SHA/check-runs`),
or make the release job `workflow_run`-gated. Also here: the manifest hard-codes `platforms: [linux/amd64,
linux/arm64]` and `ros_distro: lyrical` (`:105-106`) instead of deriving them from `rigging.yaml` — silent drift.

### CI-3 — Test scripts that exist but CI never runs **[read]**

CI runs exactly: pytest over `core-driver/tests` + `plugins/webrtc-bridge/tools` (`tools/ci_pytest.sh:18`), the six
smokes named in `certify.yml:71` (`replay usb pcap lifecycle rtsp rtsp_reconnect`), three unittest scripts
(`certify.yml:97-101`) and two `rig certify` invocations. Not run:

| Script | What only it covers | Blocker for CI |
|---|---|---|
| `core-driver/tools/supervisor_test.sh` | The supervisor as PID 1: spawn core + plugin, `docker stop`, clean finalize within the stop budget | none — needs only the dev image (hard-codes image `cam-dev` and container name `cam_sensor`, `:23-26`) |
| `core-driver/tools/dev_test.sh` | Aravis fake-camera capture end to end, **stale-socket guard**, hw→ffv1 fallback log | none — dev image only |
| `tools/orchestration_test.sh` | Two instances side by side, per-instance volumes, cross-stack read | needs dev + webrtc images and a daemon (available on runners) |
| `plugins/ros2-bridge/tools/bridge_test.sh` | The **only** execution of the C++ ROS 2 bridges (header + unixfd): message count, encoding, capture stamp | needs the `ros2-bridge` image built |
| `plugins/webrtc-bridge/tools/webrtc_test.sh` | The **only** execution of `bridge_stream.py`: 4–6 transport/encoder scenarios through a real `webrtcsrc` consumer | needs the `webrtc-bridge` image (Rust plugin build; cacheable) |
| `plugins/webrtc-bridge/tools/discovery_test.sh` | `zenoh_advertiser.py`: token PUT / descriptor / DELETE | host networking + three images |
| `tools/gvsp-chunk-emitter/{gvsp_test,reconnect_test,roundtrip_test}.sh` | The **real Aravis chunk/PTP parse path**, GigE reconnect, and bit-exact GVSP ingest against known bytes | needs the `cam-chunks` image (patched Aravis fake camera) |
| `core-driver/tools/nvenc_lossless_test.sh`, `orin_*.sh` | NVENC bit-exactness, real RTSP/USB | Jetson hardware (legitimately out of hosted CI) |
| `core-driver/tests/check_transport_caps.py` | advertised transport frame rate | none (TQ-13) |
| `cam_driver/bayer_tile.py` `_selftest()` | exact CFA tiling round trip, all modes × patterns | none (TQ-13) |

The first two rows cost nothing to add. The bridge rows are the difference between "the bridges are tested" and
"the bridges were tested by hand once" — `docs/TESTING.md:107-110` records WebRTC and ROS 2 results as manual
evidence for one commit (f8ba7ad).

### CI-4 — No C++ is compiled, and no vehicle image is built **[read]**

The only `docker build` in CI is `core-driver/Dockerfile.dev` (`certify.yml:48-58`). `core-driver/Dockerfile`
(the `cam-core` image vehicles run, with the source-built Aravis), `plugins/ros2-bridge/Dockerfile`,
`plugins/ros1-bridge/Dockerfile`, `plugins/webrtc-bridge/Dockerfile` and `tools/gvsp-chunk-emitter/Dockerfile` are
never built. So `cam_bridge_base.cpp`, `cam_header_bridge.cpp`, `cam_unixfd_bridge.cpp` and `cam_ros1_bridge.cpp`
(about 1,000 lines with the header) are never compiled by CI — a syntax error ships. `rig certify --require-build-check` runs
`tools/build-images.sh` against a *stub* `docker` (SERVICE_CONTRACT §20.1 `build-no-cache`), which proves the argv,
not that anything builds. ROS 1 is worst off: no script runs `ros1-bridge` at all, and no example config enables it,
so certify never even examines its image reference.

### CI-5 — `rig certify` covers 2 of 7 examples, and the orchestrator is checked out unpinned **[read + ran]**

`certify.yml:115-118` certifies `cam_usb.yaml` and `cam_rtsp.yaml`. `rigging.yaml:69-75` declares seven examples;
`camera.example.yaml` is `examples[0]`, the one rig seeds every new instance from (SERVICE_CONTRACT S5) and the
default for `rig certify --repo` — it is not certified in CI. Uncertified shapes: the gige default, the Boson
thermal config with its `control_device` overlay (`docker-compose.control.yml`), `cam_shm.yaml` (shm input, no
bridge) and `cam_ros2.yaml` (the `ros2-source` profile). All seven do pass a local `rig certify` (see `01-launcher-and-rig-contract.md`), and here all 7 × 4 platforms render deterministically with no shared names (§1.6) — so this is cheap to turn
into a matrix.

`certify.yml:107-110` checks out `christomaszewski/rig` with no `ref:`: the default-branch HEAD at run time. A
certify result is therefore not reproducible, a rig change can turn this repo red (or silently weaken the gate)
with no commit here, and the release is "certified" against whatever rig was that minute rather than the contract
version the fleet runs (docs v0.2.84 here). The `certify` job also has no `timeout-minutes` (`certify.yml:103-104`;
the other two jobs have 60 and 5) and installs `pyyaml` unpinned (`:111`).

What certify cannot prove is listed by the contract itself (§20.3): side effects of non-`up` verbs, concurrency,
launch-surface completeness, pin enforcement, export. Those need repo-owned tests (TA-1…TA-5); §1.6 shows the first
one would fail today.

### CI-6 — No lint, type-check, shell lint or formatting gate **[ran]**

`find` for `pyproject.toml`, `ruff.toml`, `.flake8`, `mypy.ini`, `tox.ini`, `.pre-commit-config.yaml`,
`.shellcheckrc`, `.clang-format`, `.clang-tidy`: none exists (only `plugins/ros2-source/setup.cfg`). The tests are
full of `# noqa: E402` markers for a linter nothing runs. ~9 k lines of Python (with heavy threading and
`Optional` state), 35 shell scripts including the launcher that every `rig` verb executes, and about 1,000 lines of C++
ship with no static check. shellcheck finds 40 issues today, one at `error` level (§1.6). `python -m compileall`
and `bash -n` are not run either; a syntax error in `tools/build-images.sh` or `plugins/*/entrypoint.sh` is found
on the vehicle.

### CI-7 — Coverage is collected but gates nothing and omits the entry points **[read + ran]**

- No `--fail-under` / `fail_under`; `docs/TESTING.md:38-40` defers a threshold "until a baseline is inspected". The
  baseline exists now (82 % / 75 % for `cam_driver`). A ratchet at, say, 80/72 for `cam_driver` would have flagged
  nothing today and would catch a large untested addition.
- `.coveragerc:4-6` excludes `core-driver/main.py`, `core-driver/supervisor.py`, `tools/` and `plugins/ros2-source`
  (§1.5: 0 %, 37 %, –, 0 % for `node.py`). The summary a reviewer sees in the job page hides exactly the least
  tested files.
- No subprocess coverage (`COVERAGE_PROCESS_START` / `parallel = True`), so the six smokes and
  `test_replay_integrity.py`'s `main.py` run contribute nothing; four lanes are never combined; the report is an
  artifact with 14-day retention (`certify.yml:88`), so the evidence for a release expires two weeks later (the
  v1.13.0 artifacts on 2026-11-13).

### CI-8 — Actions and tools are pinned to mutable references **[read]**

`actions/checkout@v7` (`certify.yml:46,106,107`; `registry-release.yml:65,67`), `docker/setup-buildx-action@v4`,
`docker/build-push-action@v7`, `actions/upload-artifact@v7`, `actions/setup-python@v7` — major tags, not commit
SHAs. In `registry-release.yml` this matters more: the job holds `RIG_REGISTRY_TOKEN` (a PAT with write access to
the public registry, persisted by the second checkout, `:67-71`) and then runs
`pip install git+https://github.com/christomaszewski/rig` at an unpinned ref (`:77`) followed by scripts from the
registry checkout. Test dependencies are ranges (`tools/ci_pytest.sh:4` `'pytest>=8,<10' 'coverage>=7,<8'
'pytest-timeout>=2,<3'`) installed at run time, and base images are floating tags (`ubuntu:22.04/24.04/26.04`). With
no scheduled run, upstream drift is discovered when someone dispatches CI minutes before a release.

### CI-9 — One hung test erases the whole lane's results **[ran]**

`tools/ci_pytest.sh:19` uses `--timeout=45 --timeout-method=thread`. Demonstrated here with a three-test file whose
second test blocks: pytest-timeout dumps stacks and hard-exits the process — exit 1, **no `junit.xml`, no coverage
data, no summary line, and the third test never runs**. In CI that means `ci_pytest.sh:23` finds no coverage file,
the job summary is empty, and the uploaded artifact contains only `pytest.log`. Given TQ-1 (a shm test that
intermittently stalls) and the recorded "shm shutdown timeout", a single flake turns a lane into an uninformative
red. Options: run GStreamer-backed files with `--forked`/`-n` process isolation, or `faulthandler_timeout` plus a
watchdog fixture that fails the test instead of the process. The outer `timeout 300` (`ci_pytest.sh:18`) has the
same effect; the suite takes ~55 s today, so there is room, but the limit is undocumented as a budget.

### CI-10 — Lane matrix does not match the fleet **[read]**

- arm64 runs only the `modern` (1.28.8) lane (`certify.yml:40-44`). The 1.20 lane — what JP6 vehicles run — and the
  1.24 lane are amd64 only. The FFV1 last-frame loss (`docs/TESTING.md:118-125`) was found on arm64.
- Every lane uses distro GStreamer/Aravis from `Dockerfile.dev`. The production combination recorded in
  `docs/TESTING.md:143` ("Production Ubuntu 24.04, Aravis 0.8.36") was a manual run; nothing re-runs the suite
  inside `cam-core`.
- The launcher tests (`dev-contracts`, `test_sensor_env.py`) run on one host Python (whatever `ubuntu-latest` ships) with
  PyYAML installed. `tools/sensor_env.py` has a stdlib fallback parser specifically for vehicle hosts without
  PyYAML; it is exercised only by flipping `sensor_env._HAVE_YAML` in-process, never on an interpreter that really
  lacks PyYAML, and never on the Python 3.10 a JP6 host has.

### CI-11 — No scheduled run, no flake tracking, stale documentation **[read]**

- No `schedule:` trigger: with floating dependencies (CI-8) and timing-sensitive tests, a nightly run on the
  release branch is the cheapest flake detector there is. The two unexplained failures (TQ-2) were "resolved" by
  re-running.
- `docs/TESTING.md:3` still says CI runs on pushes to `main` and `jp6-modern` and builds "three native Linux
  runtimes" (the matrix has four lanes); its newest test count is 549 (now 619); the "Candidate branch ... Keep this
  branch separate until the intended Jetson platforms pass the hardware checks" note (`:80-81`) describes a branch
  that has since shipped three releases. The Jetson promotion gate (`:156-180`) has no recorded result.
- `certify.yml:59-60` "image-backed runtime contains supervisor" only checks that `/app/supervisor.py` is readable.

### CI-12 — Smaller items

- The `dev-contracts` unittest scripts produce no junit/artifact and have no skip gate (TQ-10).
- Smokes bind-mount the checkout read-write and write `__pycache__` as root into the workspace (TQ-12); `pytest`
  correctly mounts it `:ro` (`tools/ci_test.sh:9`).
- `concurrency.cancel-in-progress: true` keyed on `github.ref` (`certify.yml:12-14`) cancels an in-flight manual
  run on the release branch if a second dispatch follows — fine, but worth knowing when the manual run *is* the
  release evidence.

---

## 5. Tests to add or modify

Levels: **unit** (pure Python, no GStreamer) · **gst** (real GStreamer in the pytest lane) · **proc** (spawns the
real `main.py`/`supervisor.py` from pytest) · **smoke** (Docker service smoke) · **contract** (launcher/descriptor
against `rig`'s SERVICE_CONTRACT, fake `docker` + real `docker compose config`, no daemon) · **ci** (workflow change).
Effort: S ≤ ½ day, M ≈ 1–2 days, L ≈ 3+ days. Ordered by risk to recorded-data fidelity and vehicle operation.

### Tier 1 — would catch silent data loss or a broken vehicle; mostly cheap

| ID | Behaviour to pin | Where | Level | Why (failure it catches) | Effort |
|---|---|---|---|---|---|
| **TA-1** | No launcher verb other than `up` issues a mutating docker command: for `ps`, `logs`, `down`, `pull`, `config`, `images`, `runtime-images`, `replay-capabilities` the fake-docker log contains only `compose <verb>` / `compose config` (with and without `--zenohd`/`--roscore`, with and without `RIG_RUNTIME_IMAGE_PINS` and `RIG_REPLAY_*`) | new `tools/test_launcher_contract.py` (reuse the shim from `tools/test_runtime_images.py:31-43`) | contract | SERVICE_CONTRACT O5/L5/D5/P4/R2. **Fails today**: `cam-up:264` runs `docker volume create` for `ps`/`logs`/`down`/`pull` (§1.6) — `rig status` after `rig down --purge` resurrects the volume; with `--zenohd` a status query starts a router | S |
| **TA-2** | Jittered capture timestamps survive the *real* recorder: feed `CapturePipeline` (ffv1, then x264) stamps with ±240 µs jitter, a late start and a 2.2 s gap; assert MKV PTS vs CSV `pts_ns` within the export tolerance, then `export_recordings.convert` and `ReplaySource` accept the session — on every lane, no PTS-rewriting probe | `core-driver/tests/test_proxy_export_gst.py` (replace the fixture probe at `:35-51` for this case) | gst | TQ-14: GStreamer 1.20 (JP6) snaps FFV1 PTS to the frame-rate grid (`docs/proxy-exports.md:48-52`); whether real JP6 recordings export or are rejected is unobserved because the fixture rewrites PTS. Assert the actual outcome per lane (accepted on 1.24/1.28; on 1.20 either accepted or rejected with the documented message) | M |
| **TA-3** | CFA tiling is exactly reversible: port `bayer_tile._selftest` to pytest — all modes × 4 patterns × geometries incl. width ≡ 2 mod 4 and the 2×2 minimum, data hitting 0/255 (wrap in `green_diff`/`rct`); then one frame per pattern × mode through the real ffv1 recorder and `untile_cfa` | new `core-driver/tests/test_bayer_tile.py` | unit + gst | "Lossless" Bayer recordings depend on it; exact round trip currently runs nowhere (TQ-13); pytest covers `rggb` only | S |
| **TA-4** | H.264/H.265 decode keeps every frame at EOS and across part boundaries: mirror `test_ffv1_decode.py` for the recorder's x264 output decoded with replay's `raw_decode_chain` — counts 1/2/5/32, split and unsplit, PTS exact, 20 repetitions | new `core-driver/tests/test_h264_decode.py` | gst | The FFV1 frame-threading loss was found by accident; the H.264 path is unpinned (`sources/replay.py:68`) and is the leading hypothesis for the unexplained failures (TQ-2) | S |
| **TA-5** | Replay/usb/dev smokes compare **counts**: decoded bytes == CSV rows × frame size for both the original and the re-recording; `usb_test.sh`/`dev_test.sh` count decoded frames against the sidecar | `core-driver/tools/replay_test.sh:37-44,112-119,137-142`, `usb_test.sh`, `dev_test.sh` | smoke | Today a decoder that truncates both files identically passes "bit-identical" (TQ-9) | S |
| **TA-6** | The supervisor with real children (tiny `python -c` stand-ins): `run()` returns the core's exit code; SIGTERM → children get SIGINT, core is waited `CORE_STOP_GRACE_S`, a child that ignores SIGINT is killed and logged; a plugin crash is restarted with backoff up to `RESTART_MAX` then abandoned; SIGUSR1/2 reach only the core; a missing plugin binary does not take PID 1 down; `_build_services`/`_resolve` map `isolation`/`command` correctly | `core-driver/tests/test_supervisor.py` (constants monkeypatched small) | proc | PID 1 on every vehicle at 37 % coverage; a wrong kill ordering truncates the recording on every `docker stop` (TQ-7) | M |
| **TA-7** | `main.py` exit codes and signal finalize: config error → 2 with one line naming the field; unbuildable pipeline → 2; recorder error with no control plane → 1; SIGINT and SIGTERM while recording → exit 0, session JSON `truncated: false`, frames == CSV rows, state file forgotten; SIGUSR1/SIGUSR2 open/close sessions | new `core-driver/tests/test_main_process.py` (usb fake source; enable `COVERAGE_PROCESS_START`) | proc | `main.py` is 0 % measured; `rig status`/restart policy depend on these codes; today asserted once as `returncode != 0` (`test_replay_integrity.py:104`) | M |
| **TA-8** | Power-loss consistency: SIGKILL the core mid-session; closed fragments decode, CSV has at least their frames, and `discover_sessions`/`ReplaySource`/`preflight` either refuse the unattested tail or flag it — never replay it as complete | new `core-driver/tests/test_crash_consistency.py` | proc + gst | The most likely real-world failure on a vehicle; `lifecycle_test.sh` step 4 kills the core but checks only the resumed state | M |
| **TA-9** | Fix and harden the shm input tests: push-until-delivered (as `_push_until` does for unixfd), `try/finally` teardown; add "frames pushed before the reader attached are dropped, the first frame after attach is delivered with the writer's id" | `core-driver/tests/test_shm_source.py:118-136,183-197` | gst | Reproduced flake (TQ-1); under load it fails in most runs and with `--timeout-method=thread` can erase a lane | S |
| **TA-10** | Suite hygiene fixture: `conftest.py` with an autouse fixture that drains `GLib.MainContext.default()`, and fails the *owning* test if it leaves new threads or a non-NULL `Gst.Pipeline` behind; shared `tmp_path`-based builders; plus a CI step that runs the suite 3× in random order | new `core-driver/tests/conftest.py`; `tools/ci_pytest.sh` | gst + ci | Turns "passes in isolation, fails in the suite" (TQ-2, TQ-6) from folklore into a named failing test | M |
| **TA-11** | GigE timestamp spine in CI: build the `cam-chunks` image and run `gvsp_test.sh`, `roundtrip_test.sh` (bit-exact frames + injected `ChunkTimestamp` == sidecar `chunk_ns`) and `reconnect_test.sh` | `.github/workflows/certify.yml`; `tools/gvsp-chunk-emitter/*` | smoke + ci | The primary sensor type's PTP/chunk path is otherwise unit-tested only against `FakeBuffer` (`test_timestamps.py:17-43`); `camera.py` is at 35 % / 2 % | M |
| **TA-12** | The hardware qualification encodes what the recorder encodes: assert the encoder tokens of `nvenc_lossless_test.encode_argv()` equal those of `build_recorder_description(RecordingConfig(encoder="hw-hevc-lossless"), 8, ...)` (availability patched), and `LUMA_COLORIMETRY` matches | `core-driver/tests/test_lossless_colorimetry.py` | unit | TQ-18: otherwise the Jetson promotion gate can qualify a pipeline the vehicle no longer runs | S |

### Tier 2 — launcher/descriptor contract (what `rig certify` says it cannot check, SERVICE_CONTRACT §20.3)

| ID | Behaviour to pin | Where | Level | Why | Effort |
|---|---|---|---|---|---|
| **TA-13** | Every `rigging.yaml` example × every `build.platforms` entry renders; two instances (`certifyname0/1`) share no project, volume, network, container name, socket path or port; renaming leaves no trace of the old name; `COMPOSE_PROJECT_NAME` is honoured and `-p` never passed; output is byte-identical across two runs and contains no timestamp/temp path | `tools/test_launcher_contract.py` | contract | I1–I4, O2/O3, L7. Verified true today for all 28 combinations (§1.6) but CI checks two configs via an unpinned rig; a new overlay with a fixed `container_name` would ship | S |
| **TA-14** | The replay overrides in `rigging.yaml` produce a valid replay config: load `replay.source.overrides`, apply with rig's merge rules (maps merge, lists replace, `null`/absent variable deletes) to every example, then (a) `parse_config` accepts and yields `camera.type == "replay"`, `playback.initial_state == "paused"`, `control.initial_state == "inactive"`; (b) `sensor_env.py` emits a read-only input bind for the path and no USB/control device overlay; (c) the decision for `replay_input_sessions: []` ("empty means all" in the contract; rejected by the core today, §1.6) is asserted either way | new `core-driver/tests/test_rig_replay_overrides.py` | contract | R6–R8. `test_sensor_env.py:173-178` hand-writes what it believes rig renders and omits `sessions`, `proxy`, `run: null` and the whole `playback:` block; a renamed key in `rigging.yaml` or `config.py` breaks `rig replay` with green CI | S |
| **TA-15** | `config`, `images`, `runtime-images` are host-independent: (a) with no rig env, output does not change when a (redirectable) `/etc/nv_tegra_release` says R36 vs R38 — `cam-up:128` already exempts only `config`; (b) a replay config renders identically whether or not `replay.path` exists or holds sidecars — `tools/sensor_env.py:77-102` reads `<path>/*.json` on the rendering host to choose `CAM_BAYER`/`CAM_ROS_ENCODING` | `tools/test_launcher_contract.py`, `core-driver/tests/test_sensor_env.py` | contract | O4, P3 ("must not need recorded data to exist"). (b) is host-dependent by design today; either pin it as a documented exception or move the decision into the container | M |
| **TA-16** | Query verbs: stdout is machine output only (banner on stderr) for every verb (O1); `images` answers for a config still holding `{{identity}}` markers with neither `VEHICLE_ID` nor `COMPOSE_PROJECT_NAME` set and fails if a marker decides the image (P2); `runtime-images` reports the label-bearing image under its role for all four platforms (R9/R11); each within the 15 s / 10 s budgets | `tools/test_runtime_images.py` | contract | P2/P3/R9–R11; the existing tests cover `dev` roles and custom overrides only | S |
| **TA-17** | Launch-surface completeness: copy only `launch_surface` + examples to an empty dir and run `config`, `images`, `runtime-images`, `replay-capabilities` for every example × platform, plus `tools/export-recordings --preflight` | `tools/test_dev_defaults.py::test_vendored_dev_uses_image_code` → generalize | contract | S4/V2. A file added to `cam-up` but not to `rigging.yaml` works in the checkout and fails on every baked vehicle; today only `--dev` + one config is vendored-tested | S |
| **TA-18** | Launcher refusals: missing config → non-zero, message on stderr, empty stdout; invalid/hostile `name` (spaces, `;`, uppercase) → legible refusal, nothing executed; unknown `RIG_TARGET_PLATFORM` → warning + documented default; missing Compose plugin → message | `tools/test_launcher_contract.py` | contract | L4, O1, C3, C8 (TQ-15) | S |
| **TA-19** | Export service mode: `export-profiles/h264.yaml` has `version: 1`, `id`, integer `revision`, `format`, boolean `lossy` (X3); the result document has `version`, the request's profile hash, `validation.ok`, per-session files relative to the recording dir (X4); the source tree hash is unchanged and nothing is written outside `destination`, including on a failed conversion (X5) | `core-driver/tests/test_proxy_export.py` / `_gst.py` | contract + gst | X3–X5; partial-output cleanup after a mid-run failure is untested | M |
| **TA-20** | `certify` matrix + pin: certify all seven examples (and one config enabling `ros1-bridge` so its image is examined) against a pinned rig ref; fail on warnings for `fixed-names`/`volumes` | `.github/workflows/certify.yml:103-118` | ci | CI-5 | S |

### Tier 3 — untested components

| ID | Behaviour to pin | Where | Level | Why | Effort |
|---|---|---|---|---|---|
| **TA-21** | Real zenoh session open: `ZenohControl` with its default factory opens on an IPv4-only host (or degrades with a specific, asserted log) and `start()`'s retry timer attaches once a peer appears; make the resilience fixture's clients hermetic (`mode: client` or `tcp/127.0.0.1:0`) | `core-driver/tests/test_control_zenoh.py`, `test_recording_resilience.py:254-258` | unit (zenoh only) | §1.6: the production open fails where IPv6 is disabled and no test runs that path; the control plane is how `rig` starts/stops recording | S |
| **TA-22** | USB and RTSP sources in-process: `UsbSource(fake)` through `CapturePipeline` — contiguous ids, monotonic stamps, encoded and decoded callbacks carry the same stamp for MJPEG/H.264; `RtspSource` against `tools/fake_rtsp_server.py` — `rtp_ntp` provenance **required** on GStreamer ≥ 1.24, starvation → `is_disconnected()` → `reopen()`, codec change → `SourceConfigChanged` | new `core-driver/tests/test_usb_source.py`, `test_rtsp_source.py` | gst | Both modules are 0 % in pytest; `gstbase.py` (their shared feeder/watchdog) 58/28 % | M–L |
| **TA-23** | `rtsp_test.sh` fails when the image's GStreamer is ≥ 1.24 and no CSV row has `source == rtp_ntp` | `core-driver/tools/rtsp_test.sh:41-50` | smoke | TQ-9: the advertised provenance check passes with zero NTP rows on every lane | S |
| **TA-24** | `camera.py` against a stub Aravis camera/device: `configure` (ROI, pixel format, packet size, frame rate), `enable_chunks` selector sequence, `enable_ptp` — locks, times out, and is interrupted by the stop event; `_on_control_lost` → `reopen` | new `core-driver/tests/test_camera.py` (extend the stubs in `test_camera_health_lock.py:18-33`) | unit | 144 of 222 statements unexecuted in the GigE device layer | M |
| **TA-25** | C++ bridges: (a) a CI job that builds `ros2-bridge` (and `ros1-bridge`) images; (b) move the header parse + pixfmt table into a header with gtest cases fed by golden vectors generated from `FrameHeader.pack()` (one vector file shared by Python, `test_header_transport.py` and C++); (c) run `bridge_test.sh` on the 1.20 (header) and 1.28 (unixfd) cores for mono8, Bayer and mono16 | `plugins/ros2-bridge/`, `.github/workflows/certify.yml` | unit (C++) + smoke + ci | CI-4, TQ-8: ROS consumers get their images and timestamps through code CI never compiles | L |
| **TA-26** | WebRTC bridge: unit-test `bridge_stream.py`'s pipeline assembly per transport (headered shm, shm-raw, unixfd; mono/Bayer/GRAY16; with/without scale) by parsing the description with `Gst.parse_launch` and a fake sink in place of `webrtcsink`; run `webrtc_test.sh` in CI on the modern lane with a cached bridge image | `plugins/webrtc-bridge/tools/test_bridge_stream.py`, CI | gst + smoke | 727 statements at 0 %; the operator's only live view | L |
| **TA-27** | `ros2-source/node.py`: import under a stub `rclpy` and drive the image callback with fake `Image`/`CompressedImage` messages into a fake appsrc (stamp, id minting, padded rows, mismatching encoding refused); run for real inside the `ros2-bridge` image when TA-25(a) exists | new `core-driver/tests/test_ros2_source_node.py` | unit | 258 statements, zero tests, feeds a recording instance | M |
| **TA-28** | Sidecar schema golden test: the real `SidecarWriter` writes header + rows (incl. `chunk_ns` None) → exact CSV header line, row formatting and JSON key set; make it the single builder that replay/export tests use instead of four hand-written layouts | `core-driver/tests/test_sidecar.py`, `conftest.py` | unit | TQ-9/TQ-17: rig, replay and export all parse this format; nothing pins it directly | S |
| **TA-29** | Config strictness (after deciding the policy): unknown key under a known section, unknown `recording.encoder`, `segment_seconds <= 0`, negative `frame_rate`/`max_rate_hz`, zero geometry, unknown `camera.type` — each a named error (or an asserted warning) at parse time | `core-driver/tests/test_config.py` (replace `test_unknown_keys_ignored`, `:112-114`) | unit | TQ-15: a typo'd `output_dir`/`encoder`/`enabled` currently changes what a vehicle records without a word | S |
| **TA-30** | Stop-budget chain from real values: export the force-quit and sidecar-join timeouts as constants and assert `max(FORCE_QUIT_S, SESSION_DRAIN_S + SIDECAR_JOIN_S) + _RECONNECT_JOIN_S < CORE_STOP_GRACE_S < stop_grace_period` for **every** compose file and overlay (the regex at `test_supervisor.py:129` reads only the first match), and `≤ 30 s` (contract H5) | `core-driver/tests/test_pipeline_session.py:435-442`, `test_supervisor.py:121-134` | unit | TQ-9: the invariant test hard-codes two of its four terms | S |
| **TA-31** | Real write failure, not an injected bus message: run a recording in a subprocess with `RLIMIT_FSIZE` (or a tiny tmpfs) so `filesink` genuinely fails; assert truncated attestation, live transport continues, a new session on a healthy dir works | `core-driver/tests/test_recording_resilience.py` | proc + gst | `:135-137` posts a synthetic ERROR; the muxer's real behaviour at ENOSPC (partial cluster, finalization) is unobserved | M |
| **TA-32** | A stalled *consumer* never costs recorded frames: block the shm (1.20) and unixfd consumer; `publish_drops` grows, recording frame count and CSV stay complete | `core-driver/tests/test_recording_resilience.py` | gst | The existing stall test blocks the recorder queue (`:218-245`), not the consumer; a wedged ROS/WebRTC bridge is the common field case | M |
| **TA-33** | 16-bit end to end in pytest: GRAY16_LE through the real recorder (ffv1), header and unixfd transport, replay — exact; packed formats refused legibly | `core-driver/tests/test_replay_buffers.py` (`_exercise_pipeline` variant) | gst | Radiometric thermal is 16-bit; in pytest only the codec round trip is 16-bit (`test_ffv1_decode.py`), the pipeline path is smoke-only | S |
| **TA-34** | Coordination protocol negatives with an injected clock: stale/duplicate `seq`, wrong session/plan id, expired startup deadline, partially written command file, status refresh ≤ 2 s (R10) | `core-driver/tests/test_coordinated_playback.py` | unit | 67 % branch coverage in the client that gates lossless coordinated replay; hash-pinned to a file no test here can validate against the canonical copy | M |
| **TA-35** | `pcapsrc` in-process beyond coordinated start: loop, pause/resume, restart, speed change; PTS strictly increasing across cycles; frame ids repeat | `core-driver/tests/test_pcap_source.py` | gst | 59 % branch; these paths are smoke-only | M |

### Tier 4 — make the suite trustworthy

| ID | Change | Where | Level | Why | Effort |
|---|---|---|---|---|---|
| **TA-36** | Inject the clock into `Pacer`/`PlaybackState` and rewrite the wall-clock windows as exact arithmetic (as `test_replay_window_timing.py` already does) | `cam_driver/playback.py` (a `clock=` parameter), `tests/test_playback.py`, `test_replay.py` | unit | TQ-3 | M |
| **TA-37** | Replace tautologies: `test_control_zenoh.py:373` (assert `advertise()` after `close()` returns the documented value and declares nothing), `test_health.py:355-357` (sample `prov.n`, wait > 2 intervals, compare), `test_supervisor.py:69-72` (call `run()`) | as listed | unit | TQ-5, TQ-9 | S |
| **TA-38** | Turn silent returns into explicit lane expectations: `pytest.mark.skipif` + a per-lane allow-list of expected skips in `ci_pytest.sh` (unixfd on 1.20), instead of `return` | `tests/test_shm_source.py:259,287`, `test_supervisor.py:126,142`, `test_transport.py:92,105,116`, `tools/ci_pytest.sh:30-36` | unit + ci | TQ-10: vacuous passes are invisible today | S |
| **TA-39** | Collect `check_transport_caps.py` (rename to `test_transport_caps.py`, skip-with-allow-list where `unixfdsink` is absent) | `core-driver/tests/` | gst | TQ-13 | S |
| **TA-40** | Property-based tests (hypothesis) for the pure codecs: `FrameHeader` pack/unpack, UVC reassembly under random payload splits and losses, tile/untile, `_pts_for` invariants (never negative, non-decreasing, bounded rebases), `DropStats` | new `core-driver/tests/test_properties.py` | unit | Boundary cases of TQ-16 for free | M |
| **TA-41** | CI: add the release branch + `tags: v*` to `push`; gate `registry-release.yml` on a green `ci` run for the released SHA; add `shellcheck`, `ruff`, `python -m compileall`, `bash -n`; coverage with `main.py`/`supervisor.py`/`tools` in `source`, subprocess coverage and a ratchet; nightly schedule; pin actions and rig by SHA; run `supervisor_test.sh` and `dev_test.sh`; launcher tests on Python 3.10 without PyYAML | `.github/workflows/*.yml`, `.coveragerc`, `tools/ci_pytest.sh` | ci | CI-1…CI-11 | M |
