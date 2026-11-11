# camera-service v1.13.0 — review

Reviewed tree: `v1.13.0` = `6d3af8b`, the head of `codex/gstreamer-1.28.8` (the branch the last three
releases were tagged from; `main` is still `v1.10.0`). Contract reference: rig `v0.2.84`,
`docs/SERVICE_CONTRACT.md`. Review date: 2026-11-11.

This directory holds the summary (this file) and seven area reports with the evidence:

| File | Covers | Finding IDs |
|---|---|---|
| [01-launcher-and-rig-contract.md](01-launcher-and-rig-contract.md) | `cam-up`, `rigging.yaml`, compose files, release process, contract conformance | RP-1…3, LC-1…12 |
| [02-core-data-path.md](02-core-data-path.md) | capture → record / transport pipeline, sidecars, timestamps | DP-1…17 |
| [03-control-plane-config-health.md](03-control-plane-config-health.md) | lifecycle, Zenoh control, signals, supervisor, config schema, health | CH-1…23 |
| [04-sources-replay-export.md](04-sources-replay-export.md) | live sources, replay, pcap, coordinated startup, proxy export | SR-1…17 |
| [05-plugins-and-image-builds.md](05-plugins-and-image-builds.md) | ROS 2 / ROS 1 / WebRTC bridges, `ros2-source`, Dockerfiles, build scripts | PB-1…25 |
| [06-tests-and-ci.md](06-tests-and-ci.md) | test suite quality, coverage, CI, tests to add | TQ-1…20, CI-1…12, TA-1…41 |
| [07-branches-and-open-prs.md](07-branches-and-open-prs.md) | every remote branch and open PR against the release | BR-1…12 |

`repro/` holds two scripts that reproduce the two data-path findings rated high that could be run without
hardware (DP-1 and DP-5). Other `exp/…` and `scratch/…` script names mentioned in the area reports were
throwaway experiments and are not committed; each is described where it is cited.

---

## 1. Verdict

The service is in better shape than its finding count suggests in the places it has been exercised: the
launcher passes `rig certify` cleanly for all seven example configs, the lossless round trip is exact at the
geometries the tests use, the session open/close ordering and settings compare-and-set are careful, and the
619-test suite is green in CI with no skips (617 passed here; the 2 failures are environmental, section 6).

The problems cluster where nothing exercises the code:

1. **Geometry and format assumptions that hold only for the shipped examples.** Any frame whose row is not a
   multiple of 4 bytes records blank or crashes the core (DP-1); pixel formats the parser does not know become
   8-bit mono without a word (DP-6, SR-15).
2. **Encoded (stream-copy) sources**, which have the least test coverage: sessions that never start recording
   (DP-2), do not survive a reconnect (DP-9), and cannot be replayed in the normal orchestrated shape (SR-9, SR-10).
3. **Failure and restart paths**: a stop that lands during a transition, a recorder error with the control
   plane off, a stalled consumer, a stale socket after an unclean stop (CH-1, CH-2, DP-5, PB-1, PB-2).
4. **Release mechanics**: three releases cut from a branch that is not `main`, published without a CI gate (RP-1, RP-2).

No finding was rated critical. Counts for the release tree, by area:

| Area | High | Medium | Low |
|---|---:|---:|---:|
| Release process (RP) | 1 | 2 | – |
| Launcher / rig contract (LC) | – | 4 | 8 |
| Core data path (DP) | 3 | 7 | 7 |
| Control plane, config, health (CH) | 3 | 5 | 15 |
| Sources, replay, export (SR) | 3 | 9 | 5 |
| Plugins and image builds (PB) | 2 | 12 | 11 |
| **Total** | **12** | **39** | **46** |

Separately: 12 findings in unmerged branches (BR), 20 test-quality problems (TQ), 12 CI gaps (CI) and 41
proposed tests (TA).

---

## 2. How the review was done, and its limits

- The seven areas were reviewed in parallel passes, each reading its files in full and running what it could.
  The top findings of every pass were then re-checked independently, by re-running the experiment or by
  re-reading the cited code: DP-1, DP-5 and SR-9 were re-run; LC-1, LC-2, LC-5, LC-6 and LC-8 were found by
  experiment in the first place; BR-1 and the two unmerged-work claims were re-derived with git; DP-2, DP-3,
  DP-6, CH-1, CH-2, CH-8, CH-10, PB-1 to PB-3, PB-8, PB-11, PB-16, PB-17, TQ-1, TQ-5 and TQ-14 were re-read
  at the cited lines. Findings not in that list rest on the area report's own evidence, which states for each
  whether it was run or only read.
- **Run here:** `rig certify` for all seven examples (and for a vendored copy); the launcher under a logging
  `docker` stand-in; the full pytest suite on Python 3.12 + GStreamer 1.24.2 (equivalent to CI's `ubuntu24`
  lane); many targeted experiments against the real modules.
- **Not run:** anything needing a Docker daemon (no image was built, none of the six service smokes ran, the
  `status` check of certify was skipped); the GStreamer 1.20 and 1.28.8 lanes; ROS (the C++ bridges were read,
  not compiled); any camera, NVENC or Jetson. Findings that depend on upstream ROS launcher behaviour (PB-1,
  PB-3) or on hardware say so.
- Severity is about consequence on a vehicle: **high** = recorded data lost or wrong without a clear error, a
  component down without recovery, or a declared capability that does not work in its normal use; **medium** =
  the same under a narrower trigger, or a wrong attestation; **low** = edge cases, cosmetics, hardening.

---

## 3. Highest-priority findings

### 3.1 Recorded data is lost or wrong without an error

| ID | What happens | Trigger | Evidence |
|---|---|---|---|
| DP-1 | Recording is blank (x265/x264 path) while the session reports every frame "ok", or the core aborts (FFV1 path) | Frame row not a multiple of 4 bytes: e.g. a 1626-wide sensor, any such ROI, GRAY16 with odd width. No shipped example has one | re-run: `repro/row_stride_recording.py` |
| DP-2 | A stream-copy session waits for an IDR/IRAP with no timeout: records nothing while state reads `active` and health reads OK | A stream with no IDR/IRAP (documented for the ZR30 in H.265 mode); in its H.264 mode each session start discards up to one 15–20 s GOP | read |
| DP-6, SR-15 | Mono14, Bayer 14-bit, RGB8 and any unrecognised GVSP name are treated as 8-bit mono; GigE frames are sliced by the *configured* format, so frames are truncated | Camera left in, or falling back to, a format other than the configured one | parser run; slicing read |
| DP-3 | The sidecar JSON header is truncated to 0 bytes if the closing summary write fails | Disk full at session close — the case that ends a session | read; run by the area pass |
| CH-2 | After one recorder error the service restarts into permanent standby, with the error forgotten | `control.enabled: false` (signals-only deployments) | lifecycle half run |
| CH-1 | A stop that lands during a transition leaves the state file behind: the next boot resumes `inactive` although the config says `active` | `docker kill -s USR2` followed by `docker stop`, or dashboard "stop recording" followed by `down` | run with a real SIGTERM |
| CH-8 | `recording.enabled: null`, quoted booleans (`"false"` is truthy) and unknown keys are accepted silently; `recordng: {enabled: false}` records, `intial_state: inactive` boots active | Hand-edited or templated config | run |
| DP-4, DP-17, DP-8 | Sidecar attestations that can be false: PTS formula after an earlier rebase, `ptp_synced` (a boot-time snapshot), and `frames_missing: 0` for USB/RTSP/shm (ids are a host counter) | RTSP clock offset; PTP locking after boot or lost on reconnect; any USB/RTSP frame loss | run / read |
| DP-9 | A stream-copy session spanning a reconnect ends in a mux error or contains undecodable frames; the CSV says 60 frames, the file holds 30 | RTSP/USB-H.26x camera reconnects with different parameter sets or mid-GOP | mux-error variant run |

### 3.2 A component goes down and does not come back

| ID | What happens | Trigger | Evidence |
|---|---|---|---|
| DP-5 | One connected-but-silent unixfd consumer pins ~290 frames of memory in the core (1.4 GB at 5 MB/frame), stalls every consumer, and makes shutdown hang before the camera is released | A bridge hung or stopped while connected (JP7 / jp6m / dev transport) | re-run: `repro/unixfd_silent_consumer.py` |
| PB-1 | The ROS 2 bridge container stays `Up` with no node and never recovers | Its first connect fails: stale socket file after power loss / SIGKILL, or the core more than 30 s late | repo side read; GStreamer half run; ROS half not run |
| PB-2 | `ros2-source` silently serves on `<socket>.0`; the core never receives a frame again | Unclean stop of a shm-input instance with `raw` or `header` framing | run |
| PB-3 | ROS 1 bridge exits to reconnect, but `roslaunch` keeps running because the debayer nodelet is alive | ROS 1 + `debayer: true` + any core restart | read; upstream behaviour not run |
| CH-3 | A recorder that cannot open at boot (full or missing disk) makes the core exit, so compose restart-loops and ROS/WebRTC consumers get nothing | Default `initial_state: active` with an unwritable recording directory | read (deliberate; pinned by tests) |
| CH-4 | SIGUSR1/SIGUSR2 arriving before the core installs its handlers kill the sensor | A lifecycle signal right after `up` or during a restart | mechanism run |
| PB-11 | WebRTC bridge memory grows without bound (~140 MB/day at 25 fps) | `CAM_WEBRTC_STATUS: 0` (documented as "off") | run |

### 3.3 Declared rig capabilities that do not work in normal use

| ID | What happens | Trigger | Evidence |
|---|---|---|---|
| SR-9 | Replay of a stream-copy H.264/H.265 recording hangs: 0 frames delivered, state `playing`, never finishes | The replay window or bag epoch starts after the session's first frames (`--from`, or a camera that started before the bag), or `speed: 0`. FFV1 and MJPEG recordings are unaffected | re-run (synthetic H.264 session, GStreamer 1.24.2) |
| SR-10 | Under `--startup coordinated`, a low-frame-rate stream-copy H.26x session never reports `held`; the whole startup times out | Frame rate + 2 below the decoder's thread count: ≤ 13 fps on a 16-core replay host | run (thread count emulated) |
| SR-1 | With `retime: wall`, every stamp is stale by the hold duration (20 s by default) and shifted by `from_s` | `rig replay --wall-clock` | run |
| SR-11, SR-12 | Pause/resume or a speed change during a gap, or a release gate already in the past, shifts the camera's timeline for the rest of the run | Operator control during a lead-in; a slow container start | run |
| LC-3 | `rig standby` / `rig activate` / the `OP` column / `rig up --standby` do nothing for cameras, although the service implements standby/active | Always (the three state verbs are not implemented or declared) | read |
| TQ-14 | Whether FFV1 recordings made on GStreamer 1.20 (JP6) can be exported is unobserved: 1.20 snaps FFV1 PTS to the frame-rate grid, export rejects a mismatch above 1 ms, and the test fixture rewrites PTS after the encoder so CI cannot see it | Any JP6 FFV1 recording with ordinary jitter or a non-25 fps rate | read (the commit tagged v1.11.0 changed only tests and docs to turn that lane green) |

### 3.4 Release process and contract

| ID | What happens | Evidence |
|---|---|---|
| RP-1 | v1.11–v1.13 were tagged on `codex/gstreamer-1.28.8`; `main` is 18 commits behind with no PR. CI is not push-triggered there: 14 of the 18 commits never ran CI, and the three tagged ones ran only by manual dispatch | git + Actions history |
| RP-2 | `registry-release.yml` publishes any published release to the fleet registry without requiring a green CI run | read |
| LC-1 | `ps`, `logs`, `down` and `pull` create the external socket volume, so `rig status` undoes `rig down --purge`; `--zenohd` starts the router on `down` | docker trace |
| LC-2 | An UPPERCASE plugin param can override `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `VEHICLE_ID` and rig's image digest pins | rendered compose |
| LC-4 | The healthcheck tests three hard-coded socket paths: record-only or custom-path instances are unhealthy forever, and a disconnected camera reads healthy. No bridge has a healthcheck | read |
| BR-1 | PR #71 merges without a textual conflict but leaves `build.variants` twice in `rigging.yaml`; YAML keeps the second, so the Jetson variants silently gain `amd64` builds | re-derived with `git merge-tree` |

### 3.5 Exposure

CH-7 (the Zenoh control plane is unauthenticated: any peer can stop recording or switch it to lossy settings;
it cannot choose a path), PB-13 (WebRTC signalling is unauthenticated on `0.0.0.0` with host networking), SR-4
(an RTSP URL with embedded credentials is logged verbatim on every start and reconnect), PB-16/PB-17 (the
Aravis tarball has no checksum; the WebRTC build fetches six undeclared git repositories and runs cargo without
`--locked`).

---

## 4. Rig contract conformance in one table

| Contract area | State |
|---|---|
| Core contract, sections 3–10 (`rig certify`) | **Passes**, 0 errors, all seven examples and the vendored copy |
| O5 / L5 / D5 — no side effects outside `up` | **Not met** (LC-1) |
| C8 / E1 / E2 / P1 — rig-owned values cannot be overridden from config | **Not met** for names outside the reserved list (LC-2) |
| C3 — reject unusable instance names | Not met (LC-7) |
| H1 / H2 — readiness healthcheck | Partly: present on the core only, tied to default paths (LC-4) |
| H5 — clean stop within 30 s, exit 0/130/143 | Met on the normal path; a signal-killed core exits 256−N (CH-11); DP-5 can hang the stop |
| D1 / D2 — data directory and run registry | Met; `output_dir` can move data out of rig's view (LC-9) |
| §13 image pins and inventories (P2–P4) | Met by reading and by the existing tests |
| §15 operational states | **Not implemented** (LC-3) |
| §16.5 replay source (R6–R9) | R6 met (input mounted read-only); R7 not met for `retime: wall` and late gates (SR-1, SR-12); R8 met for explicit session lists, `[]` rejected (LC-8); stream-copy H.26x replay blocked by SR-9 |
| §16.6 coordinated startup (R10–R11) | Implemented and labelled; SR-10 and SR-13 are the gaps |
| §17 export (X3–X5) | Profile shape and write-only-under-destination hold by reading and tests; provenance hash issue SR-14; JP6 input question TQ-14 |
| §12 builds (B1–B4) | `build-no-cache` check passes; `build.sources` undeclared, so no offline build (PB-16, PB-17); edge cases PB-19 |
| Optional declarations | `replay.sim_time`, `interface:` and `build.sources` are not declared (LC-11) |

Row-by-row evidence for sections 16 and 17 is in `04-sources-replay-export.md`.

Two observations for rig itself: descriptors are loaded with `yaml.safe_load`, which accepts duplicate keys
silently (that is what makes BR-1 dangerous — a strict loader would refuse the file); and the contract's "an
empty `replay_input_sessions` means all" is not what the one first-party source service accepts.

---

## 5. Improvements and missing features worth implementing

Ordered by value. Defect fixes are in section 3; these are capabilities.

1. **Operational-state verbs for rig** (`standby`, `activate`, `state`, `RIG_TARGET_STATE`). The service-side
   machinery exists; the launcher glue does not (LC-3).
2. **Recording-progress health and a health-driven healthcheck.** Report WARN/ERROR when a session is active
   but frames are not advancing, when the recorder drops everything, or when a consumer stalls; drive the
   compose healthcheck from `health.json`; add healthchecks to the bridges (LC-4, DP-2, DP-5, PB-1).
3. **Strict configuration.** Refuse or warn on unknown keys, validate enums, booleans and ranges, and make
   `null` mean "default" everywhere (CH-8, CH-12, CH-16, CH-23). On a vehicle a typo currently changes what is
   recorded without a message.
4. **Geometry and format validation at build time.** Compare the tight frame size with GStreamer's layout
   and either attach `GstVideoMeta` or refuse; parse bit depth from the format name; refuse packed GVSP
   formats until they are unpacked (DP-1, DP-6, PB-4, PB-5, PB-25).
5. **Crash-safe sidecars.** Atomic header and summary writes, a time-based CSV flush, fsync at fragment
   close, attest dropped rows (DP-3, DP-10).
6. **Bridges that reconnect in process**, instead of exit-and-restart, removing PB-1 and PB-3 by construction;
   publish `CameraInfo`; expose frame id and timestamp provenance to subscribers.
7. **A boot policy for an unopenable recorder** (stay up inactive with an error, versus exit) and a disk-space
   guard at activate and rollover (CH-3).
8. **Control-plane and signalling hardening**: Zenoh config/ACL pass-through, scouting and bind options,
   payload and rate limits; a bind address and TLS for signalling; redact RTSP credentials (CH-7, PB-13, SR-4).
9. **Reproducible, offline-capable builds**: checksum Aravis, pin gst-plugins-rs by commit, `--locked`, vendor
   or declare the cargo sources in `build.sources`, pin base images by digest (PB-16, PB-17).
10. **Real sequence numbers for USB/RTSP drop accounting** (v4l2 buffer sequence, RTP sequence), or stop
    attesting zero (DP-8).
11. **Load knobs from PR #65**: FFV1 speed settings (the recorder hard-codes the slowest shape, which bounds
    the 60 fps 16-bit thermal path) and a WebRTC frame-rate cap / idle throttle (with no viewer the bridge
    still debayers, scales and converts every frame).
12. **Declare `interface:`** (topics and types) so `rig graph --check` and `rig sim` can use it, and carry the
    Bayer pattern and 16-bit Bayer labels on the JP6 header transport (PB-9).
13. **Decide the parked RTSP egress plugin** (`rtsp-bridge`, commit `4ec9e74`): 1,899 lines with passing unit
    tests on a branch with no PR. Merge it or drop it knowingly — that branch is the only copy.
14. **Performance:** skip the unused `camsrc` copy when there is no preview or raw endpoint (about 120 MB/s
    of copying at 5 MP / 24 fps), and avoid the double copy on the shm+header publish path.

---

## 6. Test suite and CI

What ran here, on Python 3.12 + GStreamer 1.24.2: **619 tests, 617 passed, 2 failed, 0 skipped, 54 s.** Both
failures are environmental (the review host has no IPv6 and the test's Zenoh peers bind `tcp/[::]:0`); they
pass with an IPv4 listener. The same default open is used in production, so an IPv6-disabled vehicle kernel
would lose the control plane with only a warning. Coverage: 72.6 % line / 66.7 % branch overall; `cam_driver`
82.2 / 74.9; the WebRTC bridge tools 14.8 / 18.5.

**Where the suite is strong:** config parsing, lifecycle state machine, session ordering, health, replay of
FFV1 recordings, the pcap parsers, the wire-header packers.

**Where there is no automated test:**

- `plugins/webrtc-bridge/tools/bridge_stream.py` (the bridge itself, 0 %), `zenoh_advertiser.py`,
  `plugins/ros2-source/cam_ros2_source/node.py`, and all C++ bridge code — CI never compiles it; ROS 1 is
  never run anywhere, and no example enables it.
- `sources/usb.py`, `sources/rtsp.py` and `main.py` are reached only by log-grepping smoke scripts;
  `supervisor.py` (PID 1 on every vehicle) is at 37 %; `camera.py` at 35 %.
- `.coveragerc` leaves `main.py`, `supervisor.py`, `tools/` and `ros2-source` out of the report, so the
  headline number hides the least-tested files.

**Quality problems that undermine a green run** (details in `06-tests-and-ci.md`):

- TQ-1: a reproduced flake in `test_shm_source.py` (a sleep stands in for "reader connected"): 1 of 6 batches
  idle, 7 of 8 under load. With `--timeout-method=thread`, one hang erases the whole lane's results (CI-9).
- TQ-14: the proxy-export fixture rewrites PTS after the encoder (section 3.3).
- TQ-2, TQ-6: every GStreamer test shares one GLib main context, there is no `conftest.py` and no teardown on
  failure — the likely source of the two unexplained failures recorded in `docs/TESTING.md`.
- TQ-5, TQ-9: assertions that cannot fail (`assert ctl.advertise() is False or True`), and smoke scripts whose
  names promise more than they check (`replay_test.sh` "bit-identical" never compares frame counts;
  `rtsp_test.sh` passes with zero NTP-stamped rows).
- TQ-8: the C++ bridges are "tested" by searching their source for `case N:` strings.
- TQ-13: the only exact CFA-tiling round trip (`bayer_tile._selftest`) and `check_transport_caps.py` run nowhere.
- TQ-15: tests pin permissive behaviour (`test_unknown_keys_ignored`).

**CI gaps:** release branch not push-triggered (CI-1); registry publish not gated (CI-2); `supervisor_test.sh`,
`dev_test.sh`, the bridge, WebRTC and GVSP scripts never run (CI-3); no vehicle image or C++ build (CI-4);
certify on 2 of 7 examples against unpinned rig (CI-5); no lint, shellcheck or type check — shellcheck finds
40 issues today (CI-6); coverage gates nothing (CI-7); arm64 runs only the 1.28 lane while JP6 vehicles run
1.20 (CI-10).

**The tests most worth adding first** (TA numbers from `06-tests-and-ci.md`; S/M = effort):

| # | Test | Catches |
|---|---|---|
| TA-1 (S) | No launcher verb other than `up` issues a mutating docker command | LC-1 — fails today |
| new (S) | Recorder round trip at widths not divisible by 4, every encoder; `bytes_per_frame` against `GstVideoInfo.size` | DP-1 — fails today |
| new (S) | Mono14 / RGB8 / unknown format names; GigE payload size against the device format | DP-6, SR-15 |
| new (M) | H.264 stream-copy session: sync point never arrives; reconnect with changed caps; replay with `from_s` and with an epoch after the session start | DP-2, DP-9, SR-9 — the replay case fails today |
| TA-2 (M) | Real recorder with jittered stamps → export, asserted per GStreamer lane, no PTS-rewriting probe | TQ-14 |
| TA-6, TA-7 (M) | Supervisor with real child processes; `main.py` exit codes, signal finalize, stop during a transition | CH-1, CH-2, CH-4, CH-11 |
| TA-8 (M) | SIGKILL mid-session: closed fragments decode, the tail is flagged, never replayed as complete | crash consistency |
| TA-32 (M) | A stalled shm or unixfd consumer costs no recorded frames and does not block shutdown | DP-5 |
| TA-3, TA-4 (S) | Exact CFA tiling round trip in pytest; H.264 EOS / part-boundary frame-count matrix | silent last-frame loss |
| TA-9, TA-10 (S/M) | Fix the shm flake; a `conftest.py` that drains the main context and fails the owning test on leaked threads or pipelines | TQ-1, TQ-2, TQ-6 |
| TA-14 (S) | `rigging.yaml` replay overrides applied to every example produce a config the core accepts | LC-8 |
| TA-25 (L) | Build the bridge images in CI; gtest the header parse, pixfmt table and `yuv_to_rgb8` from golden vectors shared with Python | PB-25, TQ-8 |
| TA-41 (M) | CI: release branch and tags in the push trigger, gate the registry publish, shellcheck / ruff / compileall, coverage including entry points with a ratchet, nightly run, pinned rig | CI-1…CI-11 |

---

## 7. Unmerged work

Twenty remote branches were compared with the release by content (PRs are squash-merged, so commit counts
mean nothing): 17 are fully landed and can be deleted, with no dropped fix or test found on any of them.

| Item | State | Recommendation |
|---|---|---|
| `main` | Ancestor of `v1.13.0`; nothing the release lacks | Fast-forward to `v1.13.0` first |
| PR #63, #66, #67 | Superseded by #68 (95–97 % of their lines are verbatim in the release) | Close; delete the branches |
| PR #65 `load-knobs` | Not landed. Its `health.threads` collides with the release's `HealthConfig` and git merges the two silently into a file where the setting is ignored (BR-9); the idle throttle is default-on (BR-11) | Rework, then merge |
| PR #71 frame-capture plugin | Genuinely new, well tested (116 tests pass on the merged tree). BR-1 duplicate `build.variants`; BR-2 a wildcard GET triggers a capture and writes files; BR-3 a partial sink failure is cached by the dedupe; BR-12 `transport: auto` dead-ends on a stale unixfd socket | Rebase onto the release line, drop commit `8d08f6a`, fix BR-2/3/12, add a smoke test, merge |
| `claude/ros2-bridge-base-image` | Holds the unmerged `rtsp-bridge` plugin (`4ec9e74`), never proposed | Open a PR or delete knowingly |

Add a duplicate-key check on `rigging.yaml` to the launcher tests so a BR-1-style merge cannot pass CI.

---

## 8. Suggested order of work

1. **Process (hours).** Fast-forward `main`; put the release branch and `v*` tags in the CI push trigger; gate
   `registry-release.yml` on CI; close #63/#66/#67 and delete the 17 landed branches; decide `rtsp-bridge`.
2. **Recorded-data fidelity.** DP-1 (and the same convention in PB-4, PB-5, PB-25), DP-6 + SR-15, DP-3, DP-2,
   CH-1, CH-2, DP-7, then the attestations DP-4, DP-8, DP-17. Each with its regression test from section 6.
3. **Availability.** DP-5, PB-1, PB-2, PB-3, CH-4, LC-1, LC-4, PB-11.
4. **Replay.** SR-9, SR-10, SR-1, SR-11 to SR-13, LC-8, and settle TQ-14 on the 1.20 lane.
5. **Contract features and hardening.** LC-3 state verbs, LC-2, strict config, CH-7 / PB-13 / SR-4, build
   reproducibility.
6. **Test suite and CI.** The tier-1 tests and TA-41, then the untested components (bridges, `ros2-source`,
   USB/RTSP sources, supervisor).

Items 1 and the test-first parts of 2 are independent of each other and can start in parallel.
