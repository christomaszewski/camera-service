# 06 — Unmerged branches and open PRs (camera-service)

Reference tree: `v1.13.0` = `6d3af8b` = head of `origin/codex/gstreamer-1.28.8`. `origin/main` = `ec11fbc` (`v1.10.0`).
Method: for every branch, the diff `merge-base(v1.13.0, branch)..branch` was taken and every added line
(>= 6 chars, whitespace-stripped) was looked up in the whole `v1.13.0` tree
(`exp/landed.py`); lines not found were then
checked by hand (`git grep` on `v1.13.0`) to tell "reworded/superseded" from "dropped". Commit counts were not used as evidence.

Tools used to test claims, all read-only on the repository: `git merge-tree --write-tree` trial merges, an export of
the merged tree `v1.13.0 + PR #71` into the scratch directory, and local runs on GStreamer 1.24.2 / PyGObject 3.48.2 /
eclipse-zenoh 1.9.0 (the same versions as the Ubuntu 24.04 runtime).

## 1. Branch table

| Branch | Last commit | PR / state | Classification (content vs v1.13.0) | Action |
|---|---|---|---|---|
| `review-hardening` | 2026-06-09 `98e92ee` | #34 merged | (a) fully landed (90% of added lines verbatim; rest reworked into the session recorder) | delete |
| `ci/launcher-contract` | 2026-06-09 `5353cf5` | #36 merged | (a) fully landed (`cam-up:210` verbatim tag; `certify` job in `certify.yml`) | delete |
| `fix/rtsp-software-decoder` | 2026-06-09 `aee0c97` | #37 merged | (a) fully landed (only the "image lacks rtpgccbwe" workaround text is gone — obsoleted by #56) | delete |
| `gige-example-config` | 2026-06-10 `e21a86b` | #38 merged | (a) fully landed (example evolved further) | delete |
| `thermal-preview-normalize` | 2026-06-12 `da10d46` | #39 merged | (a) fully landed (module moved to `core-driver/cam_driver/thermal_preview.py` + shim) | delete |
| `ffv1-threading` | 2026-06-12 `335bf97` | #40 merged | (a) fully landed (100% of lines) | delete |
| `gige-clean-release` | 2026-06-12 `89ef6bb` | #41 merged | (a) fully landed (100% of lines) | delete |
| `claude/ros2-bridge-base-image` | 2026-08-21 `41ecd44` | **no PR** | (b) partially landed — base-image/RMW commits superseded by #61/#70; **the `rtsp-bridge` plugin (commit `4ec9e74`) is not in the release** | keep; open a PR for `4ec9e74` or decide to drop it (see §2.1) |
| `claude/webrtc-bayer-camera-crash-flleq4` | 2026-08-24 `72cb836` | #60, #61 merged | (a) fully landed (`--no-upgrade`, `tier: sensor`, `RIG_BUILD_NO_CACHE`) | delete |
| `ci/run-repo-tests` | 2026-09-01 `f346307` | #48 merged | (a) fully landed (unit job replaced by the 4-runtime matrix; no-skip rule kept in `tools/ci_pytest.sh`) | delete |
| `fix/stale-shm-socket` | 2026-09-01 `5c0d63e` | #49 merged | (a) fully landed (guard + `dev_test.sh` assertion, now parameterised on `$SOCKET`) | delete |
| `webrtc-max-size` | 2026-09-01 `07ba3ab` | #64 merged | (a) fully landed (99.3%; the one scenario label is now `${CAM_TEST_PLATFORM:-jp7}`) | delete |
| `fix/recording-correctness` | 2026-09-01 `431ac3a` | #46 merged | (a) fully landed (PTS memo, encoder single-owner, >8-bit guard extended to `x264`) | delete |
| `fix/teardown` | 2026-09-01 `cefefbe` | #47 merged | (a) fully landed (99.2%; stop-before-summary moved to `session.py:258`) | delete |
| `load-knobs` | 2026-09-01 `61abcb7` | **#65 open** | (c) not landed — 0 of its features exist in v1.13.0 (see §2.2, §3.2) | needs rework (BR-9), then merge — or close deliberately |
| `feat/session-recorder` | 2026-09-01 `c197238` | **#66 open** | (a) fully landed via #68 (94.9% verbatim, rest reworked) | close PR as superseded, delete |
| `feat/zenoh-control-plane` | 2026-09-01 `b50fa7d` | **#67 open** (base `feat/session-recorder`) | (a) fully landed via #68 (97% verbatim) | close PR as superseded, delete |
| `playback-sources` | 2026-09-08 `eaa2646` | **#63 open** | (a) fully landed via #68 (96.8% of 9,812 added lines verbatim; every file present; rest reworked) | close PR as superseded, delete |
| `codex/camera-shared-runtime` | 2026-10-27 `c5b862b` | #70 merged into the release branch | (a) fully landed (100% of lines; squash `6fa5dcf`) | delete |
| `claude/dreamy-euler-pkeqsh` | 2026-11-09 `507e1d8` | **#71 open** | (c) not landed — genuinely new (frame-capture plugin, 4,258 lines) | rebase onto the release line, fix BR-1/2/3/12, merge (see §3.5) |

Not classified by request: `main`, `codex/gstreamer-1.28.8`, `claude/camera-service-rig-review-s64l2k`.

Counts (20 branches): 17 fully landed (a), 1 partially landed (b), 2 not landed (c).

## 2. Unmerged work worth keeping

### 2.1 `rtsp-bridge` plugin — `claude/ros2-bridge-base-image`, commit `4ec9e74` (2026-08-21) — NOT in the release, no PR

A complete fourth consumer plugin: a GstRtspServer RTSP egress over the plugin endpoint (unixfd on JP7, shm+header on JP6),
H.264/H.265, NVENC auto-probe with CPU fallback, one shared on-demand media, 16-bit normalize, Zenoh discovery.
27 files, +1,899 lines, including `plugins/rtsp-bridge/{Dockerfile,compose.yml,run.sh,README.md}`,
`tools/{rtsp_server,frame_pump,rtsp_launch,rtsp_consumer,cam_header}.py`, `tools/rtsp_test.sh`, two unit-test files and
two test configs.

Evidence it did not land:
- `git ls-tree -r --name-only v1.13.0 | grep rtsp-bridge` → nothing; `plugins/rtsp-bridge/` does not exist.
- `git grep -c -e 'rtsp-bridge' -e 'CAM_RTSP_' v1.13.0` → 0 files (the only `rtsp_server` hits are the unrelated
  `core-driver/tools/fake_rtsp_server.py`).
- `v1.13.0:docker-compose.yml` `include:` lists ros2-bridge, ros2-source, ros1-bridge, webrtc-bridge only.
- No pull request was ever opened from this branch (PR list #1–#71 checked).

State of the code: its 17 unit tests (`test_frame_pump.py`, `test_rtsp_launch.py`) pass standalone here. A cherry-pick
of `4ec9e74` onto v1.13.0 conflicts in 5 glue files only (`README.md`, `cam-up`, `docker-compose.dev.yml`,
`rigging.yaml`, `tools/build-images.sh`); the plugin directory is purely additive. It predates and therefore lacks:
the `jp6m`/`dev` platforms, `build.variants` + architectures, `tools/runtime_images.py` roles and pins,
`images.override_env`, the shared `cam_driver/thermal_preview.py` (it carries its own copy) and the CI runtime matrix.

Deliberate or accidental? Looks **parked, not rejected**: the branch's other two commits were re-implemented in the
release (below), the plugin was simply never proposed. Decide explicitly: open a PR for `4ec9e74` rebased on the
release, or record that RTSP egress is not wanted and delete.

The branch's other two commits are superseded by a different implementation and need nothing:
- `b2856e6` (ros2-bridge builds FROM `RIG_BASE_IMAGE`) → `v1.13.0:plugins/ros2-bridge/Dockerfile:12,16,43`
  (`ARG BASE_IMAGE`, both stages `FROM ${BASE_IMAGE}`), `tools/build-images.sh:131`, via #61 + #70.
- `41ecd44` (install the rmw rig declares) → same feature under a different build-arg name: the branch used
  `RMW_IMPLEMENTATION`, the release uses `ROS_RMW` (`plugins/ros2-bridge/Dockerfile:13,22`,
  `tools/build-images.sh:103`), with tests in `tools/test_dev_defaults.py:167-185`.
  The branch's long prose in `docs/fleet-build-contract.md` / `plugins/ros2-bridge/README.md` was replaced by shorter text.

### 2.2 `load-knobs` (PR #65) — FFV1 speed knobs, WebRTC fps cap / idle throttle, per-thread CPU

Not landed at all; see §3.2 for evidence and the rework needed. The parts worth keeping are the FFV1 knobs
(the release still hard-codes the slowest FFV1 shape, and the 60 fps 16-bit thermal path is CPU-bound by it) and
`CAM_WEBRTC_MAX_FPS`.

### 2.3 Frame-capture plugin (PR #71)

Not landed; see §3.5.

### 2.4 Dropped fixes or tests on the fully-landed branches: none found

Every "missing" line on the 17 landed branches was traced to a rewording or a later rewrite. The checks that looked
most like drops, and where they are now:

| Branch / commit | Candidate | Where it is in v1.13.0 |
|---|---|---|
| `fix/stale-shm-socket` `5c0d63e` | `dev_test.sh` stale-socket assertion (`touch /tmp/cam/frames`, `frames.0`) | `core-driver/tools/dev_test.sh:41-57`, parameterised on `$SOCKET`, also accepts the unixfd guard line |
| `review-hardening` `35040df` | `_account(..., recorded=False)` sidecar 1:1 rule | moved into `RecordingSession.push()` (`pipeline.py:559-565` docstring) |
| `review-hardening` | x265 >8-bit guard | `recorder.py:156`, extended to `hw-hevc-lossless` and `x264` |
| `fix/recording-correctness` `911bd88` | clear the PTS memo on reconnect | `pipeline.py:1276` + `test_pipeline_pts.py:141-151` |
| `fix/recording-correctness` | `test_first_frame_defines_the_base_and_writes_the_header` | renamed `test_first_frame_defines_the_base` (`test_pipeline_pts.py:50`); header writing moved to the session |
| `fix/teardown` `24487ea` | stop the sidecar before `write_summary` | `session.py:258-264` |
| `ci/run-repo-tests` `feb8ac5` | fail CI on skipped tests | `tools/ci_pytest.sh` (junit `skipped` check) |
| `webrtc-max-size` | JP7 unixfd + Bayer + MAX_SIZE scenario | `plugins/webrtc-bridge/tools/webrtc_test.sh:205-207` |
| `fix/rtsp-software-decoder` | `CAM_WEBRTC_CONGESTION: disabled` workaround in `cam_rtsp.yaml` | deliberately removed: the image ships `rtpgccbwe` since #56 (`cam_rtsp.yaml:66-71`) |

## 3. Open PR reviews

Trial merges (`git merge-tree --write-tree <target> origin/<branch>`; exit 0 = clean):

| PR | Branch | into `v1.13.0` | into `origin/main` |
|---|---|---|---|
| #63 | `playback-sources` | 37 conflicted files (22 add/add, 15 content) | 33 conflicted files |
| #65 | `load-knobs` | 4 conflicted files | 4 conflicted files |
| #66 | `feat/session-recorder` | 17 conflicted files | 17 conflicted files |
| #67 | `feat/zenoh-control-plane` | 19 conflicted files (its PR base is `feat/session-recorder`, not main) | 19 conflicted files |
| #71 | `claude/dreamy-euler-pkeqsh` | **clean (exit 0)** | **clean (exit 0)** |

### 3.1 PR #63 — `playback-sources` → main (20 commits, 87 files, +11,718/−259)

**What it adds.** Replay of recorded runs, usbmon UVC pcap replay, runtime playback control over Zenoh, a shm INPUT
source, the `ros2-source` plugin, `dev` as a third platform, the input bind. It also carries the two commits of #66/#67
(merge `9a1e420`).

**Superseded?** Yes, completely, by squash-merge #68 (`58aae8b`, on main since 2026-09-17) and later release work.
Evidence: 9,812 added lines, 9,501 (96.8 %) present verbatim in v1.13.0, and all 87 files exist there
(per-file table: `exp/playback-landed.txt`).
The 311 lines that differ sit in files v1.13.0 rewrote afterwards (`sources/replay.py` +374/−142 vs the branch,
`sources/pcapsrc.py`, `cam-up`, `docker-compose.dev.yml`). Spot checks of the fixes on the branch:
- `c6be9b4` (count the fragment that closes on EOS): all 7 lines of the fix are verbatim in
  `v1.13.0:core-driver/cam_driver/session.py` (`self.segments = sorted(set(self.segments) | set(glob.glob(...)))`).
- `44553e4` (pcap FID toggle across frames): `core-driver/cam_driver/uvcpcap.py` 605/605 lines present.
- `454c309` (playback inherits the Bayer label): all 8 tests it added exist by name in
  `v1.13.0:core-driver/tests/test_sensor_env.py` (e.g. `test_replay_inherits_the_bayer_label_from_the_live_block`).
- The pcap "bounded drain wait" (`pcapsrc.py` on the branch, `did not drain within 10s`) is not verbatim but was
  replaced by a stronger mechanism: `v1.13.0:core-driver/cam_driver/sources/pcapsrc.py:38` `DRAIN_STALL_TIMEOUT_S`,
  `_wait_for_drain` at line 217.
No test file and no fix on this branch is absent from v1.13.0.

**Still applies?** No: 37 conflicted files against v1.13.0, 33 against main (22 of the 37 are add/add of files #68
already created). CI on its last four pushes (2026-09-07/08) was red.

**Recommendation: close as superseded; delete the branch.** Nothing to salvage.

### 3.2 PR #65 — `load-knobs` → main (1 commit `61abcb7`, 17 files, +548/−11)

**What it adds.** (1) WebRTC ingress throttles: `CAM_WEBRTC_IDLE_FPS` (default **1** — on by default: with no viewer the
chain ahead of `webrtcsink` trickles at 1 fps), `CAM_WEBRTC_IDLE_GRACE_S`, `CAM_WEBRTC_MAX_FPS` (preview rate cap);
(2) FFV1 speed knobs `recording.ffv1_coder/ffv1_context/ffv1_slices/ffv1_slicecrc`; (3) per-thread CPU breakdown
(`threadcpu.py`) on the core `health:` log line (`health.threads`) and on the bridge heartbeat
(`CAM_WEBRTC_STATUS_THREADS`); (4) an x264 `tune` change.

**Superseded?** No. None of it is in v1.13.0:
`git grep -c` on `v1.13.0` for `ffv1_coder`, `ffv1_context`, `ffv1_slices`, `ffv1_slicecrc`, `threadcpu`, `ThreadCpu`,
`CAM_WEBRTC_IDLE_FPS`, `CAM_WEBRTC_MAX_FPS`, `CAM_WEBRTC_STATUS_THREADS`, `IDLE_GRACE`, `util_set_object_arg`,
`session-requested` → **0 files each**. The recorder still hard-codes
`avenc_ffv1 coder=1 context=1 threads=0 slices=4 slicecrc=1` (`v1.13.0:core-driver/cam_driver/recorder.py:246`).
`core-driver/cam_driver/threadcpu.py`, `core-driver/tests/test_threadcpu.py`, `plugins/webrtc-bridge/tools/threadcpu.py`
do not exist in v1.13.0. 419 of its 475 added lines are absent.

**Still applies?** 4 conflicted files against both targets: `core-driver/cam_driver/pipeline.py`,
`core-driver/tests/test_config.py`, `core-driver/tests/test_recorder.py`, `core-driver/tools/dev_test.sh`.

**Defects / review.**
- **BR-9 (high if merged as-is) — `health:` config key collision with the release's health telemetry (#69).**
  The PR adds its own `class HealthConfig` with one field `threads: int = 0`
  (`origin/load-knobs:core-driver/cam_driver/config.py:172-178`) and `health=_build(HealthConfig, raw.get("health"))`
  (line 331). v1.13.0 already has a different `HealthConfig` (`v1.13.0:core-driver/cam_driver/config.py:352-372`:
  `enabled`, `interval_s`, `stale_after_s`, `providers`, `limits`, …). `config.py` is **not** in the conflict list — git
  merges it silently into a file with two `class HealthConfig` definitions (merged lines 264 and 370) and a duplicated
  `health:` dataclass field (lines 404, 407). It still parses; the later (release) class wins, and `_build` drops unknown
  keys (`config.py:427-431`), so `health.threads` becomes a silently ignored setting. The feature has to be re-homed
  (e.g. a `threads` field on the release `HealthConfig`, or a health provider per docs/HEALTH.md).
- **BR-10 (low) — the x264 `tune` rationale is not reproducible on the shipped runtime.** The PR comment says
  `set_property("tune", "zerolatency")` raises for a GFlags property "so it silently never applied"
  (`bridge_stream.py:270-277` on the branch). On PyGObject 3.48.2 / GStreamer 1.24.2 (the bridge image's Ubuntu 24.04
  versions) I measured: `set_property` succeeds and the first encoded frame leaves after **1** input frame, identical
  to `Gst.util_set_object_arg` (+ `rc-lookahead=0`); only the untouched default needs 50 frames. So v1.13.0
  (`plugins/webrtc-bridge/tools/bridge_stream.py:273`) is **not** carrying a latent bug here; the change is harmless,
  the comment is wrong. This is not a "dropped fix".
- **BR-11 (behaviour change) — the idle throttle is default-on** (`_env("CAM_WEBRTC_IDLE_FPS", "1")`,
  `bridge_stream.py:343`). Every deployed camera's preview drops to 1 fps until a viewer is counted; correctness depends on
  `consumer-added`/`consumer-removed` and the signaller's `session-requested` signal, with a `get-sessions` resync each
  heartbeat as the net. It needs a release note and a re-test against the release's gst-plugins-rs 0.15.4 /
  GStreamer 1.28.8 bridge (the PR was validated on the 0.13.x / 1.24 bridge only).
- `threadcpu.py` exists twice, byte-identical (`core-driver/cam_driver/` and `plugins/webrtc-bridge/tools/`); v1.13.0
  solved the same problem for `thermal_preview` with one module + a shim.
- Tests: `test_threadcpu.py` (4 tests, pass), `test_ffv1_speed_knobs_land_in_the_fragment`, `test_ffv1_knobs_parse`,
  one `webrtc_test.sh` scenario for the throttle. No unit test for `_ingress_drop` state transitions.

**Recommendation: needs rework, then merge.** Rebase onto the release line; re-home `health.threads` (BR-9); keep the
FFV1 knobs and `CAM_WEBRTC_MAX_FPS` (small, tested, useful for the 60 fps thermal core); ship the idle throttle default-off
or with a release note (BR-11); drop the misleading comment (BR-10); de-duplicate `threadcpu.py`.

### 3.3 PR #66 — `feat/session-recorder` → main (1 commit `c197238`, 26 files, +2,515/−186)

**What it adds.** Standby/active lifecycle: the recorder as a per-session pipeline (`session.py`, `lifecycle.py`,
`keyframe.py`), SIGUSR1/2 control, crash-resume state file.

**Superseded?** Yes, by #68. 2,078 added lines, 1,972 (94.9 %) verbatim in v1.13.0; every file exists. All eight test
files it adds or extends are present (`test_session.py` 297/303 lines, `test_pipeline_session.py` 320/324,
`test_lifecycle.py` 175/175, `test_keyframe.py` 75/75, `test_dropstats.py` 18/18). The 106 differing lines are
signature changes made later (e.g. `RecordingSession.push` gained arguments; `_account` lost `recorded=`).

**Still applies?** No — 17 conflicted files against both targets.

**Recommendation: close as superseded; delete the branch.**

### 3.4 PR #67 — `feat/zenoh-control-plane` → `feat/session-recorder` (1 own commit `b50fa7d`, 17 files, +1,161/−23)

**What it adds.** `control_zenoh.py` (presence token, `get_state`, `change_state`), `lifecycle_probe.py`,
`docs/LIFECYCLE.md`, the zenoh pin in both core images.

**Superseded?** Yes, by #68. Of its own 929 added lines (diff against its base `feat/session-recorder`), 901 (97 %) are
verbatim in v1.13.0; `test_control_zenoh.py` 227/231, `lifecycle_probe.py` 195/195, `docs/LIFECYCLE.md` 132/135.

**Still applies?** Only against its own stale base. 19 conflicted files against v1.13.0 and against main. Its base branch
is itself superseded, so the PR cannot land anywhere meaningful.

**Recommendation: close as superseded; delete the branch (after #66).**

### 3.5 PR #71 — `claude/dreamy-euler-pkeqsh` → main: frame-capture plugin (7 commits, 26 files, +4,258/−2)

**What it adds.** An in-image `isolation: process` plugin (`core-driver/cam_driver/capture/`, 11 modules) spawned by the
supervisor (`core-driver/supervisor.py` gains `_CONFIG_LAUNCHERS`). It reads the core's plugin endpoint
(unixfd or shm+header), keeps ~2 s of frames in a time- and byte-bounded ring, and on a Zenoh query at
`fleet/<vehicle>/svc/<instance>/capture/request` writes the frame nearest a requested UTC time as a JPEG
(`jpegenc`, or `nvjpegenc` behind a startup probe) and/or a 16-bit single-band TIFF, with hand-written EXIF/GPS IFDs and
Pix4D XMP, plus a JSON sidecar, to a file sink (`<recording dir>/keyframes` or the run's `store/`) and/or a Zenoh put sink.
Plus `docs/CAPTURE.md` (358 lines), 5 test files, a benchmark tool, example-config blocks and a ROADMAP note.

**Superseded?** No. `git grep -c "frame-capture\|cam_driver.capture" v1.13.0` = 0 hits. Genuinely new work.

**Does it still apply?** Textually yes against both targets. Every API it imports is byte-identical between `main` and
`v1.13.0` (`resolve_recording_dir`, `load_config`, `control_zenoh.vehicle_id/zenoh_connect_endpoints`,
`transport.unpack_header/CAPS/HEADER_SIZE`, `formats.parse_pixel_format/bytes_per_frame`; `supervisor.py` has no diff
between the two). I exported the merged tree (v1.13.0 + PR) to
`exp/merged71` and ran the PR's tests there on
GStreamer 1.24.2 / PyGObject 3.48.2 / eclipse-zenoh 1.9.0: **116 passed in 4.45 s, 0 skipped** (so the release CI's
"no skipped tests" rule in `tools/ci_pytest.sh` holds on the 1.24 runtime; the 1.20 and 1.28.8 runtimes were not run here).
`tools/test_dev_defaults.py`, `tools/test_gstreamer_archives.py`, `tools/test_runtime_images.py` also pass on the merged tree.
CI on the PR head `507e1d8` is green (main's workflow).

**What it touches / does not touch, against the release-only machinery:**

| Release surface | PR #71 | Verdict |
|---|---|---|
| `rigging.yaml` `launch_surface` | not touched | correct — code runs in the image (`COPY core-driver/ /app/`, `core-driver/Dockerfile:118`, `Dockerfile.dev:28`) |
| `rigging.yaml` `build.images` | not touched | correct — no new image |
| `rigging.yaml` `build.variants` | **adds its own block** (commit `8d08f6a`) | **breaks the release — BR-1** |
| `tools/build-images.sh` | not touched | correct |
| `tools/runtime_images.py` (release-only) | not touched | correct — no image role; its test passes on the merged tree |
| `tools/sensor_env.py` | not touched | correct — ran v1.13.0's `sensor_env.py` on the PR's `capture-fake.yaml` and on `cam_thermal.yaml` with the block enabled: `COMPOSE_PROFILES` is unchanged, exit 0 |
| CI workflow | not touched | tests are picked up by pytest discovery; **not** in the `service smoke tests` loop (BR-7) |
| image dependencies | none added | `python3-numpy`, `plugins-good` (jpegenc), `plugins-bad` (bayer2rgb), `eclipse-zenoh==1.9.0` already in both images; `libgstnvjpeg.so` is kept by the L4T whitelist (`core-driver/Dockerfile:43`) |

**Defects.**

- **BR-1 (high, release-blocking, merge hazard) — duplicate `build.variants` in `rigging.yaml`.**
  The PR adds `build.variants` at `rigging.yaml:53-58` (PR tree; commit `8d08f6a`, images only). The release already has
  `build.variants` with per-variant `architectures` (`v1.13.0:rigging.yaml:55-59`). Git merges the two **without a
  conflict**, producing a file with the key twice (merged tree lines 55 and 62). PyYAML keeps the last one, and rig loads
  with `yaml.safe_load` (`rig/rig_cli/common.py:23`), so the release's `architectures` pins vanish. Evaluated with rig's
  own `build_contract.parse_contract`/`variant_spec`:
  - `v1.13.0`: `jp7 → ['arm64']`, `jp6 → ['arm64']`, `jp6m → ['arm64']`, `dev → ['arm64','amd64']`
  - merged: `jp7`, `jp6`, `jp6m` **→ `['arm64','amd64']`** (falls back to `build.architectures`)

  i.e. `rig build` would start attempting amd64 builds of the L4T/Jetson variants. Nothing in this repo catches it: all
  three `dev-contracts` tests pass on the merged tree. Fix: drop commit `8d08f6a` when rebasing (the release's block is a superset).
- **BR-2 (medium, contract) — a read-style wildcard GET writes files.** `capture/request` treats a payload-less,
  parameter-less query as "manual snapshot of the newest frame" (`capture/request.py:196-198`, `capture/frames.py:120-121`),
  and the queryable is declared on a key under the shared `fleet/<vehicle>/svc/<instance>/` base
  (`capture/service.py:409`). Reproduced over a real Zenoh session: `client.get("fleet/7/svc/**")` returned the descriptor
  **and** wrote `1794407768942103996.jpg` + `.json` (`captures` went 0 → 1). The core's own command queryables are inert
  on an empty GET (`control_zenoh.py:110-111` → `missing 'transition'`), so until now a fleet-wide wildcard GET was
  side-effect free. Fix: require an explicit field (e.g. `tag` or `snapshot=1`) or a payload before capturing.
- **BR-3 (medium) — a partial sink failure is frozen by the dedupe.** `ok` is `any(sink ok)` (`capture/service.py:308`)
  and an `ok` reply is cached per `(frame_id, stamp_ns)` (`service.py:210-221`). With `sinks: [file, zenoh]` and the store
  absent at capture time, a retry of the same trigger after the store comes up returns the cached reply
  (`duplicate: true, zenoh: store_absent`) and the Zenoh sink is never called again (measured: 1 write call for 2 requests).
  The ground never receives that frame over Zenoh. Same mechanism: a second request for the same frame that carries a GPS fix
  when the first had none is answered `duplicate: true, gps: "none"` — the fix is discarded and the file stays un-geotagged.
  Fix: cache per sink / re-run failed sinks on a duplicate; let a duplicate with new metadata rewrite the EXIF + sidecar.
- **BR-4 (low) — non-finite numbers escape request validation.** `_stamp()` catches `(TypeError, ValueError)` only
  (`capture/request.py:132-135`); `{"stamp_ns": Infinity}` (accepted by Python's `json.loads`) raises `OverflowError`,
  which surfaces as `internal_error` with a logged traceback instead of `bad_request`. Same for `gps.stamp_ns`.
- **BR-5 (low) — config type errors traceback instead of the documented exit 2.** `main()` catches
  `(ValueError, OSError)` (`capture/__main__.py:113`), but `quality: null`, `tai_utc_offset_s: [37]`, `file_dir: 5`,
  `tolerance_ms: [1]` raise `TypeError` and an unknown `store_prefix` placeholder raises `KeyError`
  (`service.py:85,110`, `timebase.py:49`, `sinks.py:93`, `encode.py:108`). `docs/CAPTURE.md:57` promises "exits the
  plugin with code 2, naming the parameter". The supervisor then restart-loops it 5 times.
- **BR-6 (low/medium) — `file_root: store` without `RIG_DATA_DIR` writes to the container layer.** `store_root("")`
  returns `/data/store` (`capture/sinks.py:44-45`); compose binds only `./recordings:/data/recordings`
  (`docker-compose.yml`, `${RIG_DATA_DIR:-./recordings}:${RIG_DATA_DIR:-/data/recordings}`), so keyframes die with the
  container and are invisible on the host. Refuse `file_root: store` without a data root, or root it under `/data/recordings`.
- **BR-7 (test gap) — no end-to-end smoke test, and the hardware path has never run.** `core-driver/config/capture-fake.yaml`
  is added but nothing executes supervisor → core → plugin → Zenoh request; the CI smoke loop
  (`for suite in replay usb pcap lifecycle rtsp rtsp_reconnect`, `.github/workflows/certify.yml`) has no capture suite.
  `docs/CAPTURE.md:346-347` states `nvjpegenc` was never run on a Jetson (CI substitutes `jpegenc`), and line 343 that no
  capture was loaded into Pix4D. The "NVIDIA libjpeg clashes with stock libjpeg" design rests on that untested path.
- **BR-8 (doc gap) — interaction with `transport.plugin_endpoint.max_rate_hz` is not documented** (0 mentions in
  `docs/CAPTURE.md`): the plugin only sees the rate-limited endpoint, so nearest-frame precision is bounded by that knob
  as well as by `buffer_fps`.

- **BR-12 (medium, specific to meeting the release) — `transport: auto` dead-ends on a stale `unixfd` socket.**
  `select_endpoint` picks unixfd whenever `<socket dir>/unixfd` *exists* (`capture/reader.py:56-63`) and the reader
  re-dials the same choice forever (`reader.py:151-164`). The core removes a stale unixfd socket only when it is
  itself publishing unixfd (`v1.13.0:core-driver/cam_driver/pipeline.py:334-341`), and the socket lives on the
  persistent `cam_{name}_sock` volume (`rigging.yaml:121-122`, removed only by `rig down --purge`). v1.13.0 adds
  `jp6m` (unixfd on a JP6 host), so a JP6 host switched from `jp6m`/`dev` back to `jp6` (shm+header) leaves exactly
  that file. Reproduced: stale `unixfd` socket + live shm+header producer → `auto` delivered **0 frames in 4 s
  (21 errors)**, `transport: shm` delivered 49 frames in 2 s. Every other bridge selects by `CAM_PLATFORM`, not by
  file existence. Fix: fall back to the other endpoint when the preferred one fails to start, or select by platform.
  (Script: `exp/pr71_stale_unixfd.py`.)

Smaller notes: ring eviction and the plausibility test both use `CLOCK_REALTIME` (`frames.py:69`, `timebase.py:74-75`), so
a backward clock step holds frames until the byte cap evicts them (bounded, but worth a monotonic stamp); with the
hardware encoder an odd-sided frame is cropped yet EXIF focal-plane resolution is scaled by `enc.width / frame.width`
(`service.py:257`) as if it were resampled (0.05 % error).

**Quality overall.** Good: small pure modules, defensive Zenoh adapter mirroring `control_zenoh.py`, honest docs, 116
tests including real shm/unixfd producers and a real Zenoh round trip. Reading frame selection, the EXIF/TIFF layout and
the encoder fallback turned up no defect; the files were not re-validated with exiftool here. The probes behind
BR-2…BR-6 are in `exp/pr71_probe.py`.

**Recommendation: rebase and merge onto the release line — not onto `main` as it stands.** Merging into `main` (v1.10.0)
would make `main` diverge from `codex/gstreamer-1.28.8` and end the fast-forward (§4). Fast-forward `main` to v1.13.0 first
(or retarget the PR to the release branch), drop `8d08f6a` (BR-1), fix BR-2, BR-3 and BR-12 before anything on the vehicle
triggers captures, and add a `capture_test.sh` smoke suite (BR-7).

## 4. `main` vs release divergence

- `git merge-base --is-ancestor origin/main v1.13.0` → true. `git log v1.13.0..origin/main` → empty: **`main` has nothing
  the release lacks.**
- `git merge-tree --write-tree origin/main v1.13.0` → exit 0, tree `a882d117…` = `v1.13.0^{tree}`: a **fast-forward** of
  `main` to `6d3af8b` is possible with no merge commit.
- Size of the gap: 85 files, +4,689/−450. Tags: `v1.11.0` = `f36f633`, `v1.12.0` = `6316f7a`, `v1.13.0` = `6d3af8b`.
- No pull request exists from `codex/gstreamer-1.28.8` into `main` (PR list #1–#71).

The 18 commits on `codex/gstreamer-1.28.8` not in `main` (newest first):

| Commit | Date | Subject |
|---|---|---|
| `6d3af8b` | 2026-10-30 | fix(replay): rebuild the reader on every operator restart  (**v1.13.0**) |
| `c1864b4` | 2026-10-30 | fix(replay): stop the old reader before a restart opens the new cycle |
| `9610e54` | 2026-10-30 | fix(recorder): keep mono/Bayer codes exact through the YUV step |
| `6fa5dcf` | 2026-10-28 | build(ros2): adopt shared fleet base (#70) |
| `6316f7a` | 2026-10-27 | feat(replay): coordinate lossless camera startup  (**v1.12.0**) |
| `f36f633` | 2026-10-22 | test(proxy): preserve fractional fixture PTS  (**v1.11.0**) |
| `cf3fe39` | 2026-10-22 | feat(replay): pin validated runtime images |
| `2c2dde7` | 2026-10-22 | fix(replay): align windows and debayer proxies |
| `789f013` | 2026-10-21 | fix(export): preserve split recording timestamps |
| `71fd741` | 2026-10-21 | feat(export): normalize GRAY16 proxies |
| `59d36eb` | 2026-10-21 | feat(replay): add H264 proxy exports |
| `2b1dced` | 2026-10-20 | fix(replay): mount source recordings read-only |
| `5302f2f` | 2026-10-19 | fix: default dev cameras to host networking |
| `1232b42` | 2026-10-19 | fix: publish both camera CPU architectures |
| `1a22a2d` | 2026-10-19 | fix: extract sources on emulated builders |
| `4e2b0a0` | 2026-10-16 | fix: ship portable camera replay runtimes |
| `4a9daa6` | 2026-10-16 | fix: preserve FFV1 frames at end of replay |
| `f8ba7ad` | 2026-10-16 | build: bump camera media dependencies |

Consequences of leaving `main` at v1.10.0: every open PR targets a base three releases old (#71 was developed and
CI-tested against v1.10.0, which is how BR-1 arose); the repository default branch lacks seven released correctness fixes
(`9610e54` bit-exactness, `4a9daa6` last-FFV1-frame, `789f013` split timestamps, and the replay fixes `6d3af8b`,
`c1864b4`, `2c2dde7`, `2b1dced`); and any merge into `main` now ends the fast-forward.

## 5. Recommended cleanup

1. **Fast-forward `main` to `v1.13.0`** (`6d3af8b`) before merging anything else. It is conflict-free by construction.
   Then either retire `codex/gstreamer-1.28.8` or declare it the release branch and keep `main` in step with each tag.
2. **Close as superseded:** PR #63, #66, #67 (in the order #67, #66, #63). Every line was verified as landed or reworked.
3. **Delete 17 branches** whose content is fully in v1.13.0: `review-hardening`, `ci/launcher-contract`,
   `fix/rtsp-software-decoder`, `gige-example-config`, `thermal-preview-normalize`, `ffv1-threading`,
   `gige-clean-release`, `claude/webrtc-bayer-camera-crash-flleq4`, `ci/run-repo-tests`, `fix/stale-shm-socket`,
   `webrtc-max-size`, `fix/recording-correctness`, `fix/teardown`, `feat/session-recorder`, `feat/zenoh-control-plane`,
   `playback-sources`, `codex/camera-shared-runtime`.
4. **PR #71:** rebase on the fast-forwarded `main`, drop commit `8d08f6a` (BR-1), fix BR-2, BR-3 and BR-12, add a smoke suite, merge.
5. **PR #65:** rework per §3.2 (BR-9 first), then merge; or close with a note if the knobs are no longer wanted.
6. **`claude/ros2-bridge-base-image`:** decide on the `rtsp-bridge` plugin (`4ec9e74`). If wanted, cherry-pick that one
   commit onto the release line as a new PR and wire it into `rigging.yaml`, `tools/runtime_images.py`,
   `tools/build-images.sh` and CI; then delete the branch. If not wanted, delete it knowingly — it is the only copy.
7. Add a duplicate-key check for `rigging.yaml` to `tools/test_dev_defaults.py` (a strict YAML loader) so a BR-1-style
   silent merge cannot pass CI again.
