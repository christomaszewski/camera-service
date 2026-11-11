# 04 — Capture sources, playback / replay / export (camera-service v1.13.0, 6d3af8b)

Scope: `core-driver/cam_driver/sources/*`, `playback.py`, `uvcpcap.py`, `pcapio.py`,
`replay_coordination.py`, `export_recordings.py`, `tools/export-recordings`,
`export-profiles/h264.yaml`. Read-only review; nothing in the tree was modified.
All paths below are relative to the repository root.

Each finding carries its own confidence label.
"confirmed-by-reading" = code path traced end to end; "confirmed-by-running" = reproduced with a
throwaway script on GStreamer 1.24.2 (the `scratch/…` names below are those scripts; they are not committed); "suspected" = plausible from the
code, not yet reproduced, with what would confirm it.

---

## 1. Defects

IDs are in order of discovery; the severity-sorted table is section 6.

### SR-1 — `retime: wall` anchors the timeline at `open()`, not at the release instant (high)

- **Where:** `core-driver/cam_driver/sources/replay.py:267-271`; the offset is only ever reused,
  never recomputed (`replay.py:685`). Same class in `sources/pcapsrc.py:262-264`.
- **What:** the wall-clock retime offset is fixed while the source is still opening:

  ```python
  # replay.py:267
  if (self.cfg.retime or "original") == "wall":
      self._retime_offset = time.time_ns() - self._epoch_ns   # the zero lands on NOW
  ```

  "NOW" is the moment `open()` runs. Output is then held (legacy `start_at_unix_s` gate, or the
  coordinated hold) and released later. `PlaybackState._release()` (`playback.py:451-458`) re-anchors
  only the *pacer*; nothing re-anchors `_retime_offset`/`_offset`. Every delivered stamp is therefore
  behind the wall clock by `(release instant − open instant)`, and additionally ahead by `from_s`
  when a window is used (the epoch, not the window start, is what lands on "now").
- **Trigger:** `rig replay RUN --wall-clock` (rig renders `replay.retime: wall`,
  `rigging.yaml` `replay.source.overrides.replay.retime`). Legacy startup holds ~20 s by default
  (rig docs: "Legacy mode keeps its 20-second default"); coordinated startup holds until every
  consumer is ready (bounded by `--ready-timeout`, up to 3600 s). Camera stamps come out
  20 s … minutes stale relative to wall time and relative to every other wall-clock producer in the
  session. With `--from 600` the first frame is stamped ~10 minutes in the *future*.
- **Why it matters:** SERVICE_CONTRACT R7 — "place data on the bag timeline using the epoch, window
  and retime variables"; §16.2 warns that a consumer comparing stamps with the wall clock "silently
  reject[s] the data". This is exactly that case, produced by the service itself.
- **Reproduced (`scratch/exp1_retime_wall.py`):** one FFV1 session, `retime: wall`,
  `initial_state: paused`, `start_at_unix_s = now + 3 s`, `epoch_unix_ns = T0`:

  ```
  frame 0: stamp - wall_at_delivery = -0.010s   (delivered -2.987s after the gate)   <- the legacy preview frame
  frame 1: stamp - wall_at_delivery = -2.998s   (delivered +0.041s after the gate)
  frame 2: stamp - wall_at_delivery = -2.998s   ...
  ```

  Every released frame is stamped exactly the hold duration in the past. `config.py:178-179`
  documents the purpose of `wall` as "live consumers see fresh timestamps"; with rig's default
  20 s legacy start delay they are 20 s old.
- **Confidence:** confirmed-by-running (and by reading: the offset is written once at
  `replay.py:268`, read at `:271` and `:685`; there is no other writer).
- **Existing tests:** none. `retime: wall` appears only in config parsing
  (`core-driver/tests/test_config.py:288-290`); no test compares a wall-retimed stamp with the
  release instant.
- **Fix:** compute the wall offset at release (`_release()` hook / first `anchor_now()`), as
  `release_wall_ns − (from_ns or epoch_ns)`; for coordinated mode use the committed
  `release_at_unix_s` so all producers share one offset.

### SR-2 — pcap `restart`: cycle offset is derived from a *partial* pass and the pacer is never re-anchored (medium)

- **Where:** `core-driver/cam_driver/sources/pcapsrc.py:302-315` (span), `:325-327` (offset),
  `:281-284` (restart taken, pacer untouched).
- **What:** on a restart the feeder `break`s out of the pass, then computes the cycle span from
  whatever the extractor had read so far and freezes it:

  ```python
  # pcapsrc.py:311
  if span == 0 and st.last_ts_ns:
      ...
      span = (st.last_ts_ns - st.first_ts_ns) + interval
      pb.duration_s = round(span / 1e9, 3)   # one cycle, now that it has been measured
  ...
  cycle += 1
  offset = cycle * span
  ```

  `span` is computed once (`span == 0` guard). Two consequences, depending on when the restart lands:
  1. *Restart during cycle 0* (span not yet known): span = the played part `P`, not the capture
     length `F`. The restart itself is seamless, but `duration_s` is now wrong, and every later
     cycle starts at `k·P` while the previous one ran to `(k−1)·P + F`: stamps go **backwards** by
     `F − P` at the next loop wrap or restart, and because the pacer baseline is shared across
     cycles (`playback.Pacer`, "loop cycles keep the SAME baseline"), `F − P` seconds of frames are
     due "in the past" and are delivered in one unpaced burst.
  2. *Restart during a later cycle* (span = `F`): the next cycle is stamped `k·F` ahead, the pacer
     still holds the old baseline (`request("restart")` never calls `pacer.reset()` for pcap — there
     is no `on_restart` hook, `pcapsrc.py:82-85`), so the first frame after the restart is due
     `F − T` seconds from now. Playback goes silent for the remainder of the abandoned cycle while
     the control reply says `ok`.
- **Trigger:** `camera.type: pcap`, `pcap.loop: true`, operator sends `{"op":"restart"}` mid-capture.
- **Reproduced (`scratch/exp5_pcap_restart.py`; raw Y16 capture, 40 frames @ 100 ms = 4.0 s, `loop: true`, speed 1):**
  - restart 1.0 s into the first pass → reply `ok`, descriptor `duration_s` becomes **1.239**
    (capture is 4.0 s); at every following loop wrap the stamp goes **back 2.66 s** (frame 37 → 0),
    and 75 frames were delivered < 5 ms after their predecessor (each later 4 s cycle played in
    ~1.24 s).
  - restart 1.0 s into the second pass → reply `ok`, then **3.31 s of silence** before frame 0.
- **Confidence:** confirmed-by-running.
- **Existing tests:** none end to end. `core-driver/tests/test_playback.py:95-145` tests the
  `PlaybackState` restart flag only; no test drives a `PcapSource` through a restart.
- **Fix:** on a restart, set `offset` to `last delivered stamp − first raw ts + interval` (not
  `cycle * span`) and `pacer.reset()`; only measure `span` from a pass that ran to the end.

### SR-3 — live-source liveness watchdog uses the wall clock (medium)

- **Where:** `core-driver/cam_driver/sources/gstbase.py:100` (`_start_ns = time.time_ns()`),
  `:126-137` (`is_disconnected`), `:197-198` (`_last_data_ns = now`), and the same clock in
  `sources/shmsrc.py:146,172,192`.
- **What:** USB, RTSP and shm sources decide "stalled" by subtracting two `time.time_ns()` samples:

  ```python
  # gstbase.py:126
  now = time.time_ns()
  ...
  stalled = (now - self._last_data_ns) > int(self._reconnect_timeout_s * 1e9)
  ```

  The GigE source uses `time.monotonic()` for the same job (`sources/gige.py:36,106,150`).
- **Trigger:** any forward step of `CLOCK_REALTIME` larger than `reconnect_timeout_s` (5 s default)
  between two frames — chrony/ntpd `makestep`, a PTP/phc2sys step, or the usual Jetson boot
  sequence where the clock jumps from its stale RTC value once GNSS/NTP arrives. Every USB/RTSP/shm
  instance on the host then reports "stream stalled NNNNs" and tears its stream down and reopens it
  at the same moment (for RTSP: a fresh probe + DESCRIBE/SETUP/PLAY; a recording gap). A *backward*
  step blinds the watchdog for the size of the step.
- **Confidence:** confirmed-by-reading.
- **Existing tests:** none for a clock step. `core-driver/tests/test_pipeline_reconnect.py` drives the
  reconnect loop with a fake source and never touches `_last_data_ns`/`time_ns`;
  `core-driver/tests/test_shm_source.py` uses real time only.
- **Fix:** keep liveness on `time.monotonic_ns()`; keep `time.time_ns()` only for the stamp.

### SR-4 — RTSP URL (with embedded credentials) is written to the log at INFO/WARNING (medium, security)

- **Where:** `core-driver/cam_driver/sources/rtsp.py:56`, `:102`, `:135-136`, `:144-145`; and the
  full launch line at `core-driver/cam_driver/sources/gstbase.py:72`.
- **What:** the URL is logged verbatim five times on a normal start, e.g.

  ```python
  # gstbase.py:72
  log.info("%s pipeline: %s", type(self).__name__, desc)
  # rtsp.py:182  (what desc contains)
  f"rtspsrc name=src location={self.cfg.url} latency=..."
  ```

  and again on every reconnect (`reopen()` → `_probe_rtsp` → `configure()`).
- **Trigger:** `rtsp.url: rtsp://admin:secret@10.0.0.20/stream` — the ordinary way to configure a
  password-protected IP camera, and the only way this source offers (no `user-id`/`user-pw`
  pass-through). The password lands in `docker logs`, in `rig logs`, and in whatever ships
  container logs off the vehicle.
- **Confidence:** confirmed-by-reading. (`RtspConfig` has a single `url` field, `config.py:112`; the URL
  is not used outside `sources/`.)
- **Existing tests:** none — `core-driver/tests/` has no RTSP source test at all (only the shell
  scripts `core-driver/tools/rtsp_test.sh` and `rtsp_reconnect_test.sh`).
- **Fix:** redact userinfo before logging (`urlsplit` → drop `username:password@`), and/or accept
  separate `user`/`password` keys set through `rtspsrc user-id/user-pw`.

### SR-5 — `_probe_rtsp` never removes its bus signal watch; every reconnect attempt leaks a bus + main-context source (medium)

- **Where:** `core-driver/cam_driver/sources/rtsp.py:93-99` (compare the tidy-up the replay probe
  does for the same pattern at `sources/replay.py:135-140`).
- **What:**

  ```python
  # rtsp.py:93
  bus = pipeline.get_bus()
  bus.add_signal_watch()
  bus.connect("message::error", lambda *_: loop.quit())
  GLib.timeout_add_seconds(int(timeout_s), loop.quit)
  pipeline.set_state(Gst.State.PLAYING)
  loop.run()
  pipeline.set_state(Gst.State.NULL)
  ```

  There is no `bus.remove_signal_watch()` and the timeout source is not removed. The watch source
  holds a reference on the bus and stays attached to the default main context (the one the service
  main loop runs), so the bus, its poll fd(s) and the closure are never freed. `RtspSource.reopen()`
  calls the probe on *every* backoff attempt (`rtsp.py:154-155`).
- **Trigger:** an RTSP camera that is off or unreachable for a long time (a gimbal camera powered
  down between sorties). Each retry adds one leaked bus and one more fd to the main loop's poll
  set; the process degrades slowly and eventually runs out of descriptors.
- **Reproduced (`scratch/exp8_rtsp_probe_leak.py`):** 30 probes of an unreachable camera → open fds
  10 → 70, **+2.00 per probe**, not released after `gc.collect()` and draining the main context.
  With the 30 s backoff cap (`config.py:52`) that is ~240 fds per hour of camera outage.
- **Confidence:** confirmed-by-running.
- **Existing tests:** none (no RTSP source test exists).
- **Also:** the probe blocks up to 8 s and never looks at `Source.stop_event`, which
  `sources/base.py:154-160` says any long wait in `reopen()` MUST poll (shutdown joins the
  reconnect worker for 5 s).
- **Fix:** mirror `replay._probe_mkv`: remove the timeout if still pending, `bus.remove_signal_watch()`;
  poll `stop_event` in the wait.

### SR-6 — a non-`ValueError` exception in the pcap feeder ends the run as a *clean* finish (medium)

- **Where:** `core-driver/cam_driver/sources/pcapsrc.py:346-355`.
- **What:**

  ```python
  except ValueError as e:
      log.error("pcap replay failed: %s", e)
      self._failed = True
  finally:
      if not eos_sent:
          self._finished = True
      if self._finished or self._failed:
          self._playback.mark_finished("playback failed" if self._failed else None)
  ```

  Only `ValueError` sets `_failed`. Anything else raised inside the loop (`OSError` when the capture
  file disappears or an I/O error occurs mid-pass, `struct.error`/`IndexError` from the parser — see
  SR-7, or an exception out of the pipeline's `on_frame`) kills the thread through `finally`, which
  reports `finished` with `finished_error == False`: the service finalizes and exits 0 on a
  truncated replay. `sources/base.py:114-119` promises the opposite ("a truncated reprocess is never
  mistaken for a complete one").
- **Trigger:** capture on a removable/network volume that goes away mid-replay; any parser bug.
- **Confidence:** confirmed-by-reading.
- **Existing tests:** none; `core-driver/tests/test_coordinated_sources.py:106-117` covers only the
  MJPEG drain-stall failure path.
- **Fix:** `except Exception` → `_failed = True` (log with traceback).

### SR-7 — pcapng IDB option parsing can raise `IndexError` on a malformed option (low)

- **Where:** `core-driver/cam_driver/pcapio.py:175-181`.
- **What:**

  ```python
  while len(opts) >= 4:
      code, olen = struct.unpack(endian + "HH", opts[0:4])
      ...
      if code == 9 and olen >= 1:   # if_tsresol
          tsresol = opts[4]
  ```

  The loop guarantees 4 bytes, then reads a fifth. An IDB whose last option is `if_tsresol` with a
  length but no value bytes raises `IndexError`, not `PcapFormatError`.
- **Reproduced (`scratch/exp6_parsers.py`):** a pcapng whose IDB ends in a bare `if_tsresol` option
  header raises `IndexError: index out of range` (not a `ValueError`) from `pcapio.iter_packets`.
  A 6000-case mutation fuzz of the fixture captures (`scratch/exp7_parsers2.py`) found no other
  non-`ValueError` exception in `pcapio`/`uvcpcap`.
- **Confidence:** confirmed-by-running.
- **Existing tests:** `core-driver/tests/test_pcapio.py:62-69` covers well-formed `if_tsresol`
  variants only.
- **Fix:** `if code == 9 and olen >= 1 and len(opts) >= 5`.

### SR-8 — UVC reassembly buffer is unbounded when a stream never toggles FID / sets EOF (low)

- **Where:** `core-driver/cam_driver/uvcpcap.py:523` (`self._buf += data`), no cap anywhere in
  `_feed`; the class docstring claims "at most one frame (+ one pcap record) in RAM"
  (`uvcpcap.py:439`).
- **What:** a frame is only closed on EOF or an FID toggle. A corrupt capture, a non-compliant
  device, or a pinned non-video endpoint whose bytes pass the tolerant header check accumulates the
  whole endpoint's traffic in `_buf` — for a multi-GB capture, multi-GB of RAM — before
  `_end_of_capture` can say "no UVC frames reassembled". Raw mode knows `expected_size` and still
  does not stop at it.
- **Reproduced (`scratch/exp7_parsers2.py`):** 16 MB of ISO payloads with a constant FID and no EOF,
  extractor told `expected_frame_size=1024` → peak reassembly buffer **16.0 MB** (the whole
  stream), then the "no UVC frames reassembled" error.
- **Confidence:** confirmed-by-running.
- **Existing tests:** none for an unterminated frame (`core-driver/tests/test_uvcpcap.py`).
- **Fix:** poison the frame (`_SKIP`) once `len(_buf)` exceeds `expected_size` (raw) or
  `max(negotiated dwMaxVideoFrameSize, 64 MiB)` (MJPEG).

### SR-9 — stream-copy H.264/H.265 replay deadlocks at preroll whenever the first frames are unpaced (`from_s`, an epoch after the session start, or `speed: 0`) (high: blocks replay of stream-copy H.264/H.265 recordings in the normal orchestrated shape)

- **Where:** `core-driver/cam_driver/sources/replay.py:513-522` (reader shape for stream-copy runs),
  `:565-566` (pre-window frames are never paced), `core-driver/cam_driver/playback.py:282-283`
  (`speed <= 0` never waits).
- **What:** the non-coordinated stream-copy reader is

  ```python
  # replay.py:513
  decode_queue = "queue max-size-buffers=8" if self._playback.coordinated else \
      "queue leaky=downstream max-size-buffers=8"
  ...
  f"{src} ! tee name=st "
  f"st. ! {decode_queue} ! {parser} ! {decoder} ! {conv} ! ... ! {raw_sink} "
  f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
  ```

  It is a non-live pipeline with two appsinks, so it only leaves PAUSED once **both** have
  prerolled. `encsink` prerolls on the first AU and then blocks its branch until the pipeline is
  PLAYING; its `queue` (default limit: 1 s of timestamps) fills and blocks the `tee`. The decode
  branch must therefore produce a picture from the AUs that pass the tee in that first second. If
  those AUs arrive in a burst — pre-window frames return from `_new_stamp` without pacing:

  ```python
  # replay.py:565
  if self._from_ns is not None and raw < self._from_ns:
      return st                      # before the window: decoded, never paced or delivered
  ```

  — the leaky decode queue drops the oldest ones (the IDR first) while the decoder is still
  negotiating, the decoder is left with P-frames / fewer AUs than its frame-thread latency, emits
  nothing, `rawsink` never prerolls, and the whole reader is wedged: tee blocked, no ERROR, no EOS,
  `finished` never set.
- **Trigger (reproduced):** a stream-copy H.264 session (what `rtsp` and USB-H.264 sources record),
  25 fps, replayed with the ordinary non-coordinated reader:

  | case | result |
  | --- | --- |
  | `epoch=T0`, no window, speed 1 (control) | 150/150 consumer frames, 150/150 recorded AUs, finished |
  | `epoch=T0`, `from_s=2.0` | **hang**: 27 AUs stamped, 0 consumer frames, 0 recorded AUs, never finished |
  | `epoch=T0+2 s` (bag starts 2 s after the camera session) | **hang**, same numbers |
  | rig legacy shape: `initial_state: paused` + `start_at_unix_s` + `epoch` + `from_s=2.0` | **hang**, same numbers |
  | GOP 12 instead of 30, `from_s=2.0` | **hang** (decoder gets the IDR but fewer AUs than its latency) |
  | `speed: 0`, no window ("batch reprocess", PLAYBACK.md) | **hang**: decoder input was AUs `[19..26]`, output `[]` |

  Scripts: `scratch/exp3c.py`, `scratch/exp4_streamcopy_prewindow.py` (GStreamer 1.24.2, 4 cores).
  "Epoch after the session start" is the normal orchestrated case — a camera activated a moment
  before the bag's first message — and `--from` is the other; both are the default (`legacy`)
  startup mode of `rig replay`. The service then sits at `playback playing`, logging
  "health: NO frames in the last Ns" forever; under `on_finish: hold` nothing ever ends it.
- **Why raw (FFV1/lossless) runs are unaffected:** one branch, one appsink, stamps taken after the
  decoder. MJPEG stream-copy does not hang (every AU decodes alone, zero latency; checked with
  `scratch/exp13_mjpeg_prewindow.py`) but has its own failure in the same situations — SR-18. Coordinated mode
  uses a non-leaky decode queue (see SR-10 for its own variant).
- **Confidence:** confirmed-by-running.
- **Existing tests:** none. The only stream-copy replay tests are MJPEG:
  `core-driver/tests/test_coordinated_sources.py:132` (coordinated) and
  `core-driver/tools/replay_test.sh:147` (speed 1, no window). No H.264/H.265 stream-copy replay
  test exists in `core-driver/tests/`.
- **Fix:** do not make the must-not-drop branch depend on the best-effort branch's preroll: set
  `async=false` on both appsinks (or on `rawsink` at least), or give `encsink` its own unblocked
  path (`queue max-size-time=0 max-size-buffers=0 ...` is not enough — the appsink itself blocks in
  preroll). Independently, make the decode queue non-leaky until the first decoded frame, and skip
  pre-window AUs without decoding only from a keyframe boundary.

### SR-10 — coordinated startup: a low-frame-rate stream-copy H.26x session never reports `held` (high)

- **Where:** `core-driver/cam_driver/sources/replay.py:427-431` + `:435-450` (both output callbacks
  hold), `:513-521` (branch queues).
- **What:** in coordinated mode the first **encoded** AU is held inside `on_encoded`
  (`_coordinated_frame(..., decoded=False)` → `wait_if_paused`) while readiness is only declared by
  the first **decoded** frame (`if decoded: self._playback.prepared()`, `replay.py:444-445`). Holding
  the encoded AU blocks `encsink`; its upstream `queue` accepts one second of timestamps and then
  blocks the `tee`, so the decoder only ever receives about `fps + 2` AUs. A software H.26x decoder
  in a non-live pipeline uses frame threading and emits its first picture only after
  `threads` AUs (`threads` = CPU count, capped at 16 by gst-libav). When `fps + 2 < threads` the
  first picture never arrives, `prepared()` is never called, the participant stays `preparing`, and
  rig's whole coordinated startup times out.
- **Trigger (reproduced, `scratch/exp2_coordinated_h264.py`):**

  | session fps | decoder threads | result |
  | --- | --- | --- |
  | 25, 5, 2 | 4 (this host) | `held` in < 0.1 s |
  | 1 | 4 (this host) | **never held** (3 AUs passed the tee) |
  | 25 | 16 (emulated via `max-threads`) | `held` |
  | 10 | 16 (emulated) | **never held** (12 AUs) |
  | 9 | 12 (emulated; an AGX Orin's core count) | **never held** (11 AUs) |

  i.e. on a 16-core replay workstation any stream-copy H.264/H.265 camera at ≤ 13 fps (thermal
  RTSP at 8.6–9 Hz, most low-rate IP cameras) makes `rig replay --startup coordinated` fail with a
  preparation timeout; the same recording is fine on a 4-core laptop. The emulation only adds
  `max-threads=N` to the decoder string in memory; the 1 fps row is unmodified code.
- **Confidence:** confirmed-by-running.
- **Existing tests:** `core-driver/tests/test_coordinated_sources.py:132`
  (`test_encoded_replay_decoder_ready_before_release`) uses MJPEG, whose decoder has no latency, so
  it cannot see this.
- **Fix:** let the decode branch run ahead while the encoded AU is held: make the `encsink` branch
  queue unbounded in time (`max-size-time=0`) with a byte bound, or hold encoded output behind a
  gate *after* the tee that does not back-pressure it; or pin the replay decoder to
  `thread-type=slice` (as FFV1 already is, `formats.py:38`) so it has no frame delay.

### SR-11 — `pause`/`resume` or `set_speed` during a recorded gap or lead-in corrupts the pacing: the gap is not adjusted and the following data bursts out unpaced (medium)

- **Where:** `core-driver/cam_driver/playback.py:268-272` (`Pacer.rebase`), `:278-296` (`Pacer.wait`),
  called from `request()` at `playback.py:648` (resume) and `:659` (set_speed → `:274-276`).
- **What:** `wait()` records the frame it is about to sleep for *before* sleeping, and the sleep
  length is fixed when it starts:

  ```python
  def wait(self, ts_ns, cancel=None):
      self._last_src = ts_ns          # the frame being waited FOR, not the last one delivered
      ...
      target = self._t0_wall + int((ts_ns - self._t0_src) / self.speed)
      if target > now:
          delay_s = (target - now) / 1e9
          cancel.wait(delay_s)        # not re-evaluated on a speed change / resume
  ```

  `rebase()` then anchors the timeline at `(self._last_src, now)` — i.e. it declares that the frame
  still being waited for is due *now*. During continuous playback the error is under one frame
  interval. During silence (the gap between two sessions on a shared timeline, or the lead-in
  before a camera session that starts after the bag) it is the whole remaining wait: the sleeping
  feeder still wakes at its old deadline, and every frame after it is now "overdue" by the
  remaining gap and is delivered without pacing. The camera ends up ahead of the shared timeline
  by that amount for the rest of the run.
- **Trigger (reproduced, `scratch/exp10_rebase_in_gap.py`):** two FFV1 sessions 4 s apart,
  `epoch_unix_ns` set (gap verbatim), speed 1; 1 s into the gap:

  | action | session B first frame | B's 50 frames (1.96 s of data) took |
  | --- | --- | --- |
  | none (control) | 4.36 s (correct) | 1.96 s |
  | `pause`, 0.5 s later `resume` | 4.36 s (should be 4.86 s) | **0.00 s** |
  | `set_speed 2.0` | 4.36 s (should be 2.86 s) | **0.00 s** (should be 0.98 s) |

  Operators pause/resume and change speed from the dashboard (PLAYBACK.md "control"); a pause while
  the camera is still in its lead-in is the ordinary way to hit this.
- **Confidence:** confirmed-by-running.
- **Existing tests:** `core-driver/tests/test_playback.py` covers `rebase` only after a delivered
  frame (no wait in flight); nothing exercises a control request during a gap.
- **Fix:** make the sleep interruptible and re-evaluated — wake the feeder on resume/set_speed
  (the `Condition` is already there) and loop `wait()` until the *recomputed* target; have
  `rebase()` anchor at the timeline position (`_t0_src + (now − _t0_wall)·speed`), not at
  `_last_src`; account paused wall time by shifting `_t0_wall` forward on resume.

### SR-12 — legacy release gate already in the past: the camera starts its timeline "now" instead of catching up, and stays late for the whole run (medium)

- **Where:** `core-driver/cam_driver/playback.py:435-444` (`arm_start_gate`), `:451-458` (`_release`),
  `:261-266` (`anchor_now` is a no-op without an anchor); the anchor is only set later, in
  `sources/replay.py:274-275` (`open()`), while the gate is armed in the `PlaybackState`
  constructor (`playback.py:373-374`, i.e. `ReplaySource.__init__`).
- **What:**

  ```python
  if delay <= 0:
      log.warning("playback: start gate %.3f is %.1fs in the past -- releasing now", ...)
      with self._cond:
          self._release()          # pacer.reset(None); anchor_now() -> no-op: anchor not known yet
      return
  ```

  With no anchor recorded, the first paced frame anchors the timeline at *its own* arrival
  (`Pacer.wait`, `playback.py:285-287`). Every frame is then late by "how late the container was"
  plus the time to open and decode up to the first frame. The coordinated path corrects exactly
  this (`playback.py:402-403`, "Late thread scheduling must not shift this camera's timeline");
  the legacy path does not.
- **Trigger (reproduced, `scratch/exp11_late_gate.py`):** camera session starting 3.0 s after the
  epoch, `start_at_unix_s` 1.5 s in the past when the source is constructed:

  ```
  frame 1: delivered +4.57s after the gate; its place on the shared timeline is gate+3.04s -> error +1.53s
  frame 2: delivered +4.61s after the gate; ...                                            -> error +1.53s
  ```

  (same script with the gate 1 s in the future: error +0.00 s). rig warns about this situation —
  "startup outlasted the release gate by Ns; increase --start-delay to avoid losing early inputs"
  (`rig_cli/replay_workflow.py:979-981`) — i.e. it expects a late producer to *lose* early data,
  not to shift everything.
- **Confidence:** confirmed-by-running.
- **Existing tests:** the behaviour is *pinned*: `core-driver/tests/test_playback.py:332-336`
  ("past gate: same release") constructs a `PlaybackState` with a gate 5 s in the past and asserts
  `position_s` is 0.08–0.4 s after 0.12 s, i.e. the timeline starts at construction rather than 5 s
  in. `test_playback.py:283-285` asserts only that the state becomes `playing`. So starting from
  zero is a deliberate choice; what is not tested is its consequence on a shared timeline (no test
  compares delivery times with `gate + (ts − epoch)`).
- **Fix:** remember the gate instant and, once the anchor is known, set
  `pacer._t0_src = anchor; pacer._t0_wall = monotonic_now − (wall_now − gate)` — the same
  correction the coordinated release applies.

### SR-13 — after release, the camera keeps a hard dependency on the control directory; any read/write hiccup ends an already-running replay (medium)

- **Where:** `core-driver/cam_driver/playback.py:405-413` (post-release loop),
  `core-driver/cam_driver/replay_coordination.py:150-174` (heartbeat), `:91-107` (`_read`).
- **What:** once released, the coordination thread keeps polling for the rest of the playback:

  ```python
  # playback.py:405
  while not self._coordination_closed.wait(0.05):
      self._coordination.poll()
  except Exception as exc:  # a coordination failure always closes the output gate
      ...
      self._coordination_fail(str(exc))      # -> state FINISHED, _stop_evt set, finished_error True
  ```

  and the heartbeat re-reads `command.json` and rewrites the status every 0.1 s. `_read` tolerates
  only 0.2 s of `ENOENT`; every other `OSError`, a JSON parse error, or a failed status write
  (`ENOSPC`, `EIO`, `ESTALE`) is terminal. So for a replay that may run for hours, ~30 reads/s and
  10 fsync'd writes/s each have to succeed or the camera stops output and exits non-zero —
  although the protocol's only post-commit input is `abort`.
- **Trigger (reproduced, `scratch/exp9_coordination.py`):** participant taken through
  hold → arm → prepare_release → commit → `running`; then the control directory is removed (a
  cleaned-up session directory under `var/replay/sessions/`, a detached replay outliving its
  session bookkeeping, a bind-mount hiccup). Within a second: `playback state = finished`,
  `coordination_error = "cannot read replay command.json: [Errno 2] ..."`, the feeder's verdict is
  `stop`; the heartbeat thread also dies on an unhandled `FileNotFoundError` from `_atomic`
  (`replay_coordination.py:31`, raised out of `failed()` inside the `except` at `:165-166`).
- **Measured cadence (same script):** 46 status rewrites + `fsync` per second while
  `preparing`/`held`, 10 per second while `running` — per camera, for the whole playback. R10 asks
  for one refresh every 2 s. On a vehicle replaying onboard with several cameras that is a
  continuous stream of journal commits on the disk the recorder also writes to.
- **Confidence:** confirmed-by-running (behaviour); impact on recording throughput not measured.
- **Existing tests:** `core-driver/tests/test_coordinated_playback.py` covers abort / missing commit
  / deadline; nothing removes or breaks the control directory after release.
- **Note on ownership:** `replay_coordination.py` is vendored byte-for-byte from rig-infra and
  pinned by `tools/replay_coordination.sha256` (hash verified equal to
  `rig-infra/ros2-bag-player/tools/replay_coordination.py`), so the cadence/fsync part
  has to be fixed upstream. The post-release policy is camera-side (`playback.py:405-413`).
- **Fix:** after `started()`, poll at ≤ 1 Hz and treat I/O errors as "coordinator gone": log, stop
  heartbeating, keep playing; only an explicit, well-formed `abort` closes the gate. Upstream: no
  `fsync` on heartbeat rewrites (rename already gives readers atomicity), 1 Hz cadence.

### SR-14 — export result attests the launch bundle's implementation hash for work the image's converter did (low)

- **Where:** `tools/export-recordings:101-102` and `:133-134`; container side
  `core-driver/cam_driver/export_recordings.py:188-189`.
- **What:** the converter inside the image reports its own
  `producer.implementation_sha256 = file_hash(__file__)`. The host helper then overwrites it:

  ```python
  producer["implementation_sha256"] = implementation_hash()     # six files of the HOST bundle
  ...
  result = json.loads((control / "result.json").read_text())
  result["producer"].update(producer)
  ```

  Nothing compares the two. The only image/bundle consistency check is the profile file
  (`export_recordings.py:59-63`). rig keys export reuse on this value
  (`rig_cli/service_exports.py:577-579`, "Converter code/profile/source identity defines the
  requested transformation").
- **Trigger:** bundle updated to a converter fix that does not touch `h264.yaml` (e.g. a
  timestamp-validation fix), local `cam-dev` image not rebuilt. The export runs the old converter,
  is recorded and reused as if produced by the new one. (`producer.image_id` is recorded, so the
  truth is recoverable, but nothing checks it.) The hash list also omits files that shape the
  output and exist only in the image (`bayer_tile.py`, `sources/replay.py` decode chains).
- **Confidence:** confirmed-by-reading.
- **Existing tests:** `core-driver/tests/test_proxy_export.py` does not assert on
  `implementation_sha256` provenance (no match for the key in the test file).
- **Fix:** keep the container's value as `producer.converter_sha256`, have the image compute the
  same multi-file hash and fail if it differs from the request's expected value.

### SR-15 — GigE frame slicing uses the *configured* pixel format, not the device's (medium)

- **Where:** `core-driver/cam_driver/sources/gige.py:64-68` and `:116`; contrast
  `core-driver/cam_driver/camera.py:167-171` and `:294-304`.
- **What:**

  ```python
  # gige.py:64
  def _compute_image_size(self) -> int:
      _x, _y, w, h = self.camera.sensor_geometry()
      pf = self.cfg.pixel_format or "Mono8"
      bytes_pp = 2 if any(tok in pf for tok in ("16", "12", "10")) else 1
      return int(w) * int(h) * bytes_pp
  # gige.py:116
  frame_bytes = bytes(data)[:self._image_size] if self._image_size else bytes(data)
  ```

  Everything else asks the device: `pixel_format()` returns `camera.pixel_format_string()`, whose
  docstring calls the device "the source of truth for what the frames actually are", and
  `camera.configure()` only *warns* when the camera rejects the configured format
  (`log.warning("pixel format %s rejected: %s", ...)`) and carries on in whatever format the camera
  is in.
- **Trigger:** (a) config asks `Mono16`, camera rejects it and stays in `Mono8`: caps are built for
  GRAY8 but each frame is sliced to `2·w·h`, so the trailing chunk bytes are appended to the image;
  (b) config asks `Mono8` (the default, `config.py:60`), camera is left in a 12/16-bit format after
  the rejection: caps say GRAY16_LE, every frame is cut to half its bytes; (c) `pixel_format: null`
  ("use the camera's current format") with a 16-bit camera: same as (b). Packed formats (1.5 B/px)
  and RGB (3 B/px) are also mis-sized by this rule.
- **Confidence:** confirmed-by-reading (no camera available; `core-driver/tests/` has no GigE
  source test — `_image_size` appears only as a stub attribute in
  `tests/test_pipeline_session.py:186`).
- **Fix:** derive the size from the device format via `formats.parse_pixel_format` +
  `formats.bytes_per_frame` (already used by the shm and pcap sources), recompute after `reopen()`,
  and fail `configure()` when the configured format was rejected and differs from the device's.

### SR-16 — a reader rebuild that raises at a part/session boundary leaves replay wedged, not failed (low)

- **Where:** `core-driver/cam_driver/sources/replay.py:632-639` and `:660` (`_on_eos` →
  `configure()` / `_rebuild()` with no guard); compare the guarded rebuild in `_restart`
  (`replay.py:700-707`).
- **What:** `_on_eos` runs as a GLib signal handler. If `configure()` raises there (for example
  `Gst.parse_launch` → "no element …" because the *next* session was recorded with a codec whose
  decoder is missing on this host — per-session codecs are allowed and only the first reader is
  built at startup), PyGObject prints the traceback and drops it. The old reader is already NULL,
  `_finished` and `_failed` stay False, playback state stays `playing`.
- **Trigger (reproduced with an emulated missing element, `scratch/exp12_eos_rebuild_fail.py`):**
  two sessions; the second mapped to a non-existent decoder after `open()`:
  `HANG: finished=False finished_error=False frames=5 playback.state=playing`.
- **Confidence:** confirmed-by-running (emulated trigger).
- **Existing tests:** none (`tests/test_replay_recording_changes.py` covers codec changes that succeed).
- **Fix:** wrap both rebuild sites; on failure set `_failed`, call `_finish`. Better: build (parse)
  every session's reader description once in `open()` so a missing element is a startup error.

### SR-17 — USB source rounds the frame rate to an integer in the v4l2 caps (low)

- **Where:** `core-driver/cam_driver/sources/usb.py:73`, used at `:91` and `:102`.
- **What:** `fps = int(round(self.cfg.frame_rate or 30.0))` → `framerate={fps}/1`. A device mode
  with a fractional rate (15/2 = 7.5 fps, 30000/1001 capture devices, 25/2) cannot be requested:
  7.5 becomes `8/1`, 29.97 becomes `30/1`, and the fixed caps filter fails to negotiate. Because
  the pipeline is rebuilt with the same string on every reopen, the hotplug watchdog then loops
  ("no frame 12.0s after start") instead of reporting a configuration error.
- **Confidence:** confirmed-by-reading for the caps string; negotiation failure itself not run
  (no v4l2 device here) — would be confirmed by `v4l2-ctl --list-formats-ext` on a device with a
  fractional mode and `frame_rate: 7.5`.
- **Existing tests:** none for the USB source (`core-driver/tests/` has no `test_usb*`).
- **Fix:** render the rate as a fraction (`Fraction(frame_rate).limit_denominator(1001)`), as the
  ROS 2 source already does (`framerate=12500/1000`, `tests/test_ros2_source.py:67`).

### SR-18 — stream-copy MJPEG replay delivers consumer images under the wrong frame's stamp after any decode-queue drop (high)

- **Where:** `core-driver/cam_driver/sources/gstbase.py:210-232` (`_stamp_probe` / `_stamp_for`: the
  decode branch finds its stamp by buffer PTS), `core-driver/cam_driver/sources/replay.py:513-520`
  (leaky decode queue followed by `jpegparse`), `:565-566` (pre-window frames are unpaced).
- **What:** both branches look their `FrameStamp` up in a memo keyed by the pre-tee PTS:

  ```python
  # gstbase.py:226
  pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else None
  st = self._stamps.get(pts) if pts is not None else None
  if st is not None:
      return st
  ```

  In the decode branch `jpegparse` re-times what it emits onto a contiguous grid (the docstring at
  `gstbase.py:220-221` already notes "jpegparse re-times MJPEG"). On a recording the pre-tee PTS
  are that same grid (`k × frame interval`), so after the leaky queue drops *n* AUs the decoded
  image of frame `k` carries the PTS of frame `k − n`: the lookup **hits** — on the wrong frame.
  From then on every consumer image (plugin transport → ROS bridge / WebRTC) is delivered with the
  `frame_id` and timestamps of an earlier frame, the ids look perfectly contiguous, and the last
  *n* stamps never get an image. The recording branch (no parser, no leak) stays exact.
- **Trigger (reproduced, `scratch/exp14_mjpeg_misstamp.py`):** 150-frame MJPEG stream-copy session
  (25 fps; each frame a distinct grey level so the image identifies itself), non-coordinated reader:

  | case | recorded AUs wrong | consumer frames wrong | example (stamp id → image really is) |
  | --- | --- | --- | --- |
  | speed 1, no window (control) | 0 / 150 | 0 / 150 | 0→0 … 149→149 |
  | `from_s=0.2` (5 pre-window frames) | 0 / 145 | 0 / 145 | 5→5 |
  | `from_s=0.4` (10 pre-window frames) | 0 / 140 | **138 / 138** | 10→12 … 147→149 |
  | `from_s=1.0` | 0 / 125 | **109 / 109** | 25→41 … 133→149 (0.64 s off) |
  | `from_s=2.0` | 0 / 100 | **77 / 77** | 50→73 … 126→149 (0.92 s off) |
  | `speed: 0`, no window | 0 / 150 | **123 / 124** | 1→14 … 123→149 |

  So any replay of a USB-MJPEG recording whose session starts more than ~8 frames before the
  window/epoch — the ordinary `rig replay` case — hands consumers images that are up to a second
  newer than their header stamp says, silently. (`scratch/exp13c.py` shows the mechanism: queue
  in=150, out=110, PTS reaching `rawsink` contiguous 0..109.) H.26x stream-copy runs do not get
  this far (SR-9).
- **Confidence:** confirmed-by-running (GStreamer 1.24.2).
- **Existing tests:** none compare consumer *image content* with the stamp on a stream-copy run.
  `core-driver/tests/test_coordinated_sources.py:132` checks ids and encoded bytes with the
  non-leaky coordinated queue; `core-driver/tools/replay_test.sh:147-171` compares only the
  re-recorded bitstream at speed 1.
- **Fix:** do not correlate through a re-timing parser by PTS. Carry the CSV row index on the
  buffer in the pre-tee probe (for example a `GstReferenceTimestampMeta` with a private reference —
  whether it survives `jpegparse`/`jpegdec` on every supported GStreamer needs checking) and look
  the stamp up by that; drop `jpegparse` from
  the decode branch for demuxed input (matroskademux already delivers whole JPEGs); and keep the
  decode queue non-leaky while unpaced pre-window frames are flowing.

---

## 2. Contract conformance

Requirements from `rig/docs/SERVICE_CONTRACT.md` §16.5, §16.6, §17.2 and "Session
protocol v1" in `rig/docs/replay-coordination.md`. rig-side behaviour was read in
`rig/rig_cli/` (`replay_coordinator.py`, `replay_sources.py`, `service_exports.py`).

| Req | Verdict | Evidence |
| --- | --- | --- |
| **R6** replay the recordings instead of the live source; never modify them | **Met** (but see SR-9) | `camera.type: replay` selects `ReplaySource` (`sources/factory.py:23-25`). The reader is `filesrc … ! matroskademux` over exact part paths (`sources/replay.py:506-507`); sidecars are opened read-only (`playback.py:128`, `:140`, `:190`); nothing in the area writes under `replay.path`. The input is bind-mounted `:ro` (`docker-compose.input.yml`, `core-driver` volume). Caveat: for stream-copy H.26x recordings with pre-window frames "replay" produces nothing at all (SR-9). |
| **R7** hold output until the release instant; place data on the bag timeline using epoch, window, retime | **Partially met** | *Hold:* coordinated — met: first in-window frame is decoded and held, no preview (`sources/replay.py:435-454`, `playback.py:369-370`; `tests/test_coordinated_sources.py:25-56`). Legacy gate — one preview frame leaves before the release by design (`sources/replay.py:578-579`; PLAYBACK.md "a playback that BOOTS paused lets its first frame out"); measured 0.98–2.99 s before the gate and it is *not* delivered again at its timeline slot (`scratch/exp1_retime_wall.py`, `exp11_late_gate.py`). *Epoch/window:* implemented, half-open `[from, to)` (`sources/replay.py:224-241`, `:538-546`; `tests/test_replay_source.py:146-181`, `tests/test_replay_window_timing.py`). Broken by SR-9 (no output), SR-18 (consumer stamps on the wrong images), SR-11 (pause/speed during silence), SR-12 (gate already past → permanent lag). *Retime:* `original` met; `wall` is anchored at `open()`, not at release — **SR-1**. |
| **R8** direct binding: read the selected directory and sessions in place, incl. another instance name and a subset | **Partially met** | Met for what rig renders today: `replay.sessions` → `playback.discover_sessions(path, sessions=…)` reads exact prefixes in place, no copy, no dependence on the directory/instance name (`sources/replay.py:202-203`, `playback.py:70-107`); proxies via the header `proxy` record + `replay.proxy` (`sources/replay.py:341-342`, `:722-727`). **Gap:** an *empty* list is rejected in two places — `config.py:474-479` ("expected a nonempty list of unique exact session prefixes") and again in `playback.py:70-75` — while §16.5 defines `replay_input_sessions` "empty means all". An **absent/null** key does mean all: `sessions is None` → `_run_names(path)` (`playback.py:96-100`), with finalized-empty sessions skipped (`playback.py:101`, `:127-148`). rig's execution path always renders a non-empty list for camera sidecars (`rig_cli/replay_sources.py:320-323`), but `rig_cli/bake_replay.py:216` substitutes `replay_input_sessions=[]`. The rejection is pinned by `tests/test_proxy_export.py:190` and documented in PLAYBACK.md ("This list must be nonempty"). The export helper treats an empty list as "all" (`export_recordings.py:123`) — the two entry points disagree. |
| **R9** image labels for `binding: direct` | **Met** (outside this area; spot-checked) | `LABEL io.rig.camera.replay-bindings="1"` in `core-driver/Dockerfile:68` and `core-driver/Dockerfile.dev:10`; declared in `rigging.yaml` `replay.source.image_labels`. Role reporting lives in `tools/runtime_images.py` (not reviewed here). |
| **R10** session protocol v1 as component `camera`; mount the control dir; follow `command.json`; write `statuses/<participant>.camera.json` atomically; refresh ≥ every 2 s while not finished | **Met, with two defects at the edges** | Identity `<RIG_REPLAY_PARTICIPANT>.camera` (`playback.py:368`, `replay_coordination.py:56-57`); control dir mounted at `/replay-control` (`docker-compose.replay-control.yml`). Atomic write: temp file in the same directory, `fsync`, `os.replace`, mode 0644 (`replay_coordination.py:30-40`). Refresh: dedicated heartbeat thread, 0.1 s period, stops only in a terminal state (`:150-174`); callbacks run outside the lock (`:227-232`); measured 46/s while preparing/held, 10/s while running (`scratch/exp9_coordination.py`). State names and fields are exactly what rig reads (`rig_cli/replay_coordinator.py:221-233`, `:251-256`, `:306-309`). Defects: **SR-10** (never reaches `held` for low-fps stream-copy H.26x), **SR-13** (post-release fragility; 5–20× the required write rate with `fsync`). |
| — `hold` | Met | Opens, validates, decodes to the first in-window frame, withholds output (`sources/replay.py:439-447`). Raw pcap declares readiness after size validation (`sources/pcapsrc.py:271-272`). |
| — `arm` | Met | Nothing to arm; `ready` follows `arm` immediately (`playback.py:391-393`, `replay_coordination.py:248-262`). Arm before `held` is a protocol error (`:251-252`). |
| — `prepare_release` | Met | Only while `ready`/`release_ack`; ≥ 0.25 s in the future; immutable (`replay_coordination.py:205-213`). |
| — `commit` | Met | Requires the ack, the same instant, and arrival before it (`:214-221`); release at the instant (`:268-269`); the pacer is corrected back to the committed instant (`playback.py:402-403`). A release instant passing without commit fails (`:223-224`). |
| — `abort` | Met | Honoured by `poll()` and by the heartbeat even while the caller is busy (`:197-199`, `:157-160`); closes the output gate (`playback.py:415-423`); stopping a held source reports `aborted` (`playback.py:425-430`). |
| — rejects regressing commands / stale identities | Met | `seq` regression or a changed command at the same `seq` (`:195-196`), regress to `hold`/`arm` (`:201-204`), foreign identity (`:105-106`), unregistered participant (`:80-81`), expired deadline (`:82-84`, `:161-163`). Covered by `tests/test_coordinated_playback.py`. |
| **R11** label `io.rig.replay.coordination=1` on the running image | **Met** (outside this area; spot-checked) | `core-driver/Dockerfile:69`, `core-driver/Dockerfile.dev:11`; `cam-up:252-255` prints the capability with the active role. |
| **X3** profile file: `version: 1`, `id` = name, integer `revision` ≥ 1, `format`, boolean `lossy` | **Met** | `export-profiles/h264.yaml:2-6` (`version 1`, `id "h264"`, `revision 3`, `format "camera-recording-proxy/v1"`, `lossy true`); re-checked by the converter (`export_recordings.py:59-66`). |
| **X4** read the request; result with `version: 1`, request's `profile_sha256`, `validation.ok: true`, files per recording/session; preflight converts nothing and reports `missing_images`/`missing_dependencies` and `required_bytes` | **Partially met** | Result fields: `export_recordings.py:186-189`, `:482-485`. Preflight runs only `preflight()` + `docker image inspect` (`tools/export-recordings:99-105`) and reports `missing_dependencies`/`missing_images` (`:103-104`). **Not met:** `required_bytes` is never produced (no occurrence anywhere in the repository); rig silently substitutes `2 × input size` (`rig_cli/service_exports.py:634`), which is not an upper bound when a compact H.265 stream-copy original is re-encoded to baseline H.264. *Wording mismatch (rig doc, not this code):* files are returned relative to the request root (`recordings/<name>/<file>`, `export_recordings.py:162`, `:483`), which is what rig's verifier requires (`service_exports.py:469-471`), not "relative to the recording directory" as X4 says. See also SR-14 (producer identity). |
| **X5** write only under `destination`; never alter the originals | **Met** | Destination must exist, be empty, and not be the source or its parent (`export_recordings.py:401-405`); every output path goes through `safe_relative(destination, …)` (`:416`); the converter runs with the source mounted read-only, no network, all capabilities dropped (`tools/export-recordings:117-128`); source hashes are taken before and re-checked after each session (`export_recordings.py:422`, `:480-481`). Request paths cannot escape: instance name regex + `relative == recordings/<name>` (`:115-121`), `safe_relative` on the directory and on every file incl. symlink targets (`:40-47`, `:161-165`; `tests/test_proxy_export.py:166-177`), session ids validated (`playback.py:70-75`). Partial outputs of a failed conversion stay in the staging directory; rig owns and removes it (`service_exports.py:639`). |

---

## 3. Improvements & missing features

Not defects — things that would make this area more robust or more useful. Items marked
*(unverified)* were seen while reading and not traced further.

### 3.1 Replay / playback

1. **Start a windowed replay at the right part, not at part 0.** `_activate_session` always sets
   `_part_idx = 0`, `_idx = 0` (`sources/replay.py:326`, `:339`) and every frame before `from_s` is
   fully decoded and then discarded (`:565-566`). Start-up cost grows with `from_s` (a window 30
   minutes into a session decodes 30 minutes of video first), is paid again on every `restart`
   and loop wrap, and in coordinated mode counts against rig's launch-to-held timeout because
   `prepared()` only fires on the first in-window frame. The CSV already has `pts_ns` per row and
   each part's first PTS is in its Matroska header, so the starting part and row can be computed.
   (Doing this also removes the unpaced pre-window burst behind SR-9 and SR-18.)
2. **An empty window should be a clean, empty completion.** A window with no camera frames is a
   startup `ValueError` (`sources/replay.py:237-241`) or, when it falls in a gap between sessions
   under coordination, `failed: camera reached end before coordinated release`
   (`playback.py:606-610`), which fails the whole coordinated replay. rig pre-filters sessions by
   the bag interval (`rig_cli/replay_sources.py:308-319`), so this is an edge today.
3. **Order sessions by their data, not by header/mtime.** `discover_sessions` sorts by the header's
   `first_timestamp_ns` and falls back to sidecar mtime (`playback.py:105-106`); the CSVs are loaded
   anyway (`sources/replay.py:205-210`), so the first row's timestamp is available and survives a
   copy that does not preserve mtimes. `open()` also assumes `all_ts[0]`/`all_ts[-1]` are the
   extremes (`sources/replay.py:222`).
4. **Per-producer `restart` on a shared timeline.** A restart shifts every later stamp by one run
   span (`sources/replay.py:685`) to keep stamps monotonic. Under rig (`retime: original`, epoch
   set) the camera then leaves the bag's timeline. Consider keeping stamps unshifted when an epoch
   is configured, or advertising `restart` only when no epoch is set.
5. **Validate every session's reader at `open()`** (parse each description once) so a missing
   decoder element is a start-up error rather than SR-16.
6. **Missing features:** a `seek` control op (PLAYBACK.md offers pause/resume/set_speed/set_loop/
   restart only); a way for the legacy gate to deliver the preview frame again at its slot.

### 3.2 Coordination

7. Lower the status cadence to ~1 Hz and drop the per-heartbeat `fsync` (upstream, see SR-13).
8. *(unverified)* The participant is created in the `PlaybackState` constructor
   (`playback.py:364-372`), before `Source.open()`. I found no path in the sources that reports an
   `open()` failure (bad path, no runs, window outside the data) as `failed` with its message; rig
   would then only see a stale `preparing` heartbeat. Not traced through `main.py`.

### 3.3 Live sources

9. RTSP: sanity-check the RTCP→NTP stamp against arrival, as the USB SOF path does
   (`sources/usb.py:134`), and flag a camera whose clock is unsynchronised — today any NTP meta is
   taken as wall-clock capture time (`sources/rtsp.py:219-226`). Accept separate credential keys.
10. `_probe_rtsp`: poll `stop_event` (`sources/base.py:154-160`) so a shutdown during an 8 s probe
    does not outlive the 5 s reconnect join.
11. *(unverified)* `GstPipelineSource._stamp_for` iterates `self._stamps` (`gstbase.py:231`) while
    the pre-tee probe inserts from another thread; an `OrderedDict mutated during iteration`
    `RuntimeError` would drop one consumer frame. A lock or a "last stamp" attribute removes the
    doubt.
12. GigE: recompute `_image_size` after `reopen()` (`sources/gige.py:153-159`).

### 3.4 pcap

13. Bound the probe pass. `uvcpcap.probe()` walks the entire capture in pure Python before the
    source starts and takes `max_packets`, but `sources/pcapsrc.py:102` never passes it; a multi-GB
    capture means minutes before the first frame (and against the coordinated deadline).
14. `pcapio` ignores `if_tsoffset` (only `if_tsresol` is read, `pcapio.py:175-182`).
15. Missing feature: frame-based H.264/H.265 UVC payloads are refused (`sources/pcapsrc.py:66-69`).

### 3.5 Export

16. Report `required_bytes` (X4). A safe upper bound is cheap: `frame_count × width × height × 1.5`
    bytes (the uncompressed I420 size), all three known at preflight.
17. Reject `%` in a session prefix (and in a destination path when run outside the container)
    before building the `splitmuxsink location="…-%05d.mkv"` pattern (`export_recordings.py:335`);
    the prefix comes from file names in the run and is used as a printf format.
18. `_rows` refuses any session whose capture timestamps step backwards
    (`export_recordings.py:80-81`), i.e. every GigE session that survived a camera clock reset —
    which replay itself accepts. Say so in `docs/proxy-exports.md` or allow it (PTS is the muxing
    timeline anyway).
19. A decode error takes up to 30 s to surface (`try-pull-sample` timeout before the bus is
    looked at, `export_recordings.py:223-233`).

### 3.6 Dead code, and comments/docs that contradict the code

- `docs/DESIGN.md:309-311` describes replay as "splitmuxsrc over the `.mkv` parts". The code reads
  one `filesrc ! matroskademux` per fragment precisely *because* splitmuxsrc was wrong
  (`sources/replay.py:502-507`; `docs/PLAYBACK.md` "Replay opens each exact Matroska fragment
  directly").
- Left over from splitmuxsrc and still enforced: `playback.check_glob_hazards`
  (`playback.py:50-61`) and `RunInfo.mkv_glob` (`playback.py:44-47`, "Glob for splitmuxsrc").
  They refuse a run path containing `*?[]` and two sessions where one prefix extends the other
  ("splitmuxsrc cannot handle -- rename/move the run") although nothing globs any more;
  `sources/replay.py:144-148` repeats the stale rationale.
- `sources/base.py:9-11`: "GVSP (Aravis) is the only implementation today"; `sources/gige.py:1`:
  "the only source today". There are six.
- `sources/gstbase.py:9-10`: "HW decode + RTP-NTP/SOF timestamp provenance are follow-ups; this
  base stamps arrival time" — both exist (`sources/rtsp.py:207-230`, `sources/usb.py:124-136`).
  `sources/usb.py:12-14` says the same about itself.
- `sources/rtsp.py:126`: "Self-configure from the LIVE stream (gst-discoverer)" — the probe is
  `rtspsrc ! parsebin`, chosen explicitly *instead of* gst-discoverer (`sources/rtsp.py:42-51`).
- `uvcpcap.py:439`: "at most one frame (+ one pcap record) in RAM" — not when a frame never ends (SR-8).
- `config.py:178-179` / `playback.py:8-9`: `retime: wall` "at start", "live consumers see fresh
  timestamps" — not after a hold (SR-1).
- `docs/PLAYBACK.md`: "`speed: 0` is the batch reprocess" — hangs for stream-copy H.26x (SR-9) and
  mis-stamps consumer frames for MJPEG (SR-18).
- Dead code: `playback.discover_run` (`playback.py:116-124`) and `uvcpcap.iter_frames`
  (`uvcpcap.py:675-692`) have no caller outside `core-driver/tests/`. `Participant.wait_release`'s
  `on_arm`/`on_tick` are unused by the camera (vendored file).

---

## 4. Test gaps for this area

Each line is a behaviour with no test today, and the file that should hold it. "new" = the file
does not exist. All paths are under `core-driver/tests/`.

**Replay source**
- H.264/H.265 **stream-copy** replay at all — plain, with `from_s`, with an epoch after the session
  start, at `speed: 0`, across a part boundary. Today only MJPEG stream-copy is replayed in tests.
  → `test_replay_source.py` (fixture as in `scratch/h264fix.py`). *(SR-9)*
- Consumer **image ↔ stamp** correspondence on a stream-copy run when the decode queue has dropped
  (pre-window frames, `speed: 0`): each delivered image must carry its own `frame_id`/timestamp.
  → `test_replay_integrity.py`. *(SR-18)*
- `retime: wall`: delivered stamp ≈ wall time at delivery after a legacy gate and after a
  coordinated release; with `from_s`. → `test_replay_window_timing.py`. *(SR-1)*
- Gate already in the past: delivery times against `gate + (ts − epoch)/speed`.
  → `test_replay_window_timing.py`. *(SR-12)*
- `pause`/`resume` and `set_speed` issued **while the feeder sleeps** through an inter-session gap
  or a lead-in. → `test_playback.py` (a `Pacer` with a wait in flight on another thread) and
  `test_replay_source.py` (two sessions, epoch). *(SR-11)*
- Reader rebuild failure at a session/part boundary ends playback with an error.
  → `test_replay_recording_changes.py`. *(SR-16)*
- Loop wrap and operator `restart` with a `from_s` window on a stream-copy run (the same unpaced
  burst as SR-9/SR-18 on every cycle). → `test_replay_source.py`.
- `replay.sessions: []` vs the contract's "empty means all"; absent key = all.
  → `test_config.py` (the rejection is currently pinned only for `discover_sessions`,
  `test_proxy_export.py:190`).

**Coordination**
- Low-frame-rate stream-copy H.26x reaches `held` (parametrised over decoder thread count).
  → `test_coordinated_sources.py`. *(SR-10)*
- After `running`: control directory removed, `command.json` unreadable, status write failing —
  playback continues or fails by an explicit, tested policy. → `test_coordinated_playback.py`. *(SR-13)*
- Status refresh cadence stays ≤ 2 s apart while the source is busy decoding a long pre-window
  stretch (GIL contention). → `test_coordinated_playback.py`.
- Coordinated replay whose window holds no frames. → `test_coordinated_sources.py`.

**pcap source and parsers**
- `restart` on a `PcapSource` (during the first pass, during a later looped pass): stamps stay
  monotonic, no silence, `duration_s` is the capture's. → new `test_pcap_source.py`. *(SR-2)*
- A non-`ValueError` exception in the feeder (capture file removed mid-pass) yields
  `finished_error`. → new `test_pcap_source.py`. *(SR-6)*
- pcapng IDB with a truncated option. → `test_pcapio.py`. *(SR-7)*
- A stream that never closes a frame is bounded in memory. → `test_uvcpcap.py`. *(SR-8)*
- Corruption fuzz (truncation / byte flips) raising only `ValueError`. → `test_pcapio.py`,
  `test_uvcpcap.py` (seeded; `scratch/exp7_parsers2.py` is a starting point).

**Live sources** (there is no unit test for the GigE, USB or RTSP source classes; only
`test_shm_source.py`, `test_ros2_source.py` and shell scripts under `core-driver/tools/`)
- Liveness watchdog across a wall-clock step (patch `time.time_ns`).
  → `test_pipeline_reconnect.py` (with a `GstPipelineSource` stub). *(SR-3)*
- RTSP: no credentials in log records; the probe leaves no fd / main-context source behind; NTP
  extrapolation across frames without the meta and anchor reset on `reopen()` (testable with fake
  buffers, no camera). → new `test_rtsp_source.py`. *(SR-4, SR-5)*
- USB: caps string for fractional frame rates; SOF plausibility window; `reopen()` raising while
  the device node is absent. → new `test_usb_source.py`. *(SR-17)*
- GigE: image size follows the device format when the configured one is rejected or unset (fake
  camera object). → new `test_gige_source.py`. *(SR-15)*
- shm: an unparseable header is dropped and counted without ending capture (`shmsrc.py:148-155`).
  The config-mismatch and writer-restart paths *are* covered (`test_shm_source.py:139`, `:167`,
  `:286`), as is header parsing (`test_transport.py`). → `test_shm_source.py`.

**Export**
- Preflight result carries `required_bytes`. → `test_proxy_export.py`. *(X4)*
- `producer.implementation_sha256` describes the code that converted; a bundle/image converter
  mismatch is refused. → `test_proxy_export.py`. *(SR-14)*
- A conversion that fails mid-session (encoder error, decode timeout) exits non-zero and reports
  what it left in the destination. → `test_proxy_export_gst.py`.
- A session prefix containing `%`; a session with a backwards capture timestamp.
  → `test_proxy_export.py`.
- Stream-copy (H.264/H.265/MJPEG) originals through the converter, frame count and PTS validated
  (`test_proxy_export_gst.py` has no stream-copy input). → `test_proxy_export_gst.py`.

---

## 5. Checked and found sound

Things I looked for a defect in and did not find one.

**pcap / usbmon parsing (`pcapio.py`, `uvcpcap.py`)**
- Length fields are bounded before use: classic `incl_len` and pcapng block lengths against
  `_MAX_RECORD` and 4-byte alignment (`pcapio.py:97-99`, `:146-147`, `:160-162`); EPB `cap_len`
  against the block and interface id against the declared list (`:187-192`); ISO descriptor
  offset+length against the captured data (`uvcpcap.py:167-178`); `len_cap` against the record
  (`uvcpcap.py:85-90`); config-descriptor walk (`:297-300`); VS probe/commit block (`:334`); bulk
  reassembly capped at 64 MiB (`:616-621`).
- No infinite loop: every parser loop consumes at least 4–8 bytes or returns; a truncated final
  record/block ends the iteration quietly (`pcapio.py:92-103`, `:165-167`).
- Endianness: classic magic → byte order and ns/µs (`pcapio.py:29-34`); pcapng per-section
  byte-order magic and per-interface `if_tsresol` incl. the base-2 form (`:109-118`, `:138-145`);
  usbmon header byte order locked only when exactly one order is plausible (`uvcpcap.py:135-155`).
- Mutation fuzz: 6000 truncated/bit-flipped variants of the four fixture captures through
  `probe()` and `iter_frames()` — only `ValueError`s, nothing slower than 1 s
  (`scratch/exp7_parsers2.py`). The single exception found by hand is SR-7. A hostile
  `dwMaxPayloadTransferSize = 1` costs 0.2 s per 400 kB URB (quadratic, not a hang).

**Coordination protocol (`replay_coordination.py`, `playback.py`)**
- Status writes are atomic (same-directory temp file, `fsync`, `os.replace`; temp name has no
  `.json` suffix so a reader globbing statuses cannot pick it up) (`replay_coordination.py:30-40`).
- `command.json` is never read partially on a correct writer; `ENOENT` during a host-side replace
  is retried for 0.2 s, anything else is an error rather than a stale answer (`:91-107`).
- Refresh cannot be starved by source work: own thread, callbacks outside the lock, measured
  10–46 Hz (`:150-174`, `:227-232`).
- State names, status fields and the release/ack sequence match what rig reads and requires
  (compared with `rig_cli/replay_coordinator.py:196-311`); all protocol violations listed in
  replay-coordination.md are rejected (see the R10 rows).
- Output really is withheld before release: raw and encoded callbacks both hold the first frame,
  no boot preview, operator controls are refused until release (`sources/replay.py:421-454`,
  `playback.py:494-496`, `:626-627`); abort while held publishes nothing
  (`tests/test_coordinated_sources.py:36-41`).
- The vendored client is byte-identical to rig-infra's (SHA-256 `178442b7…ced7f`, pinned in
  `tools/replay_coordination.sha256` and checked by `tests/test_coordinated_playback.py:78-79`).

**Replay fidelity (raw/lossless runs)**
- No stamp is invented: a decoded frame beyond the CSV fails the run and is never delivered
  (`sources/replay.py:553-563`, `:538-540`); fewer frames than rows fails at EOS
  (`:599-611`, `:640-643`); truncated/failed sessions and missing segment numbers are refused
  before delivery (`playback.py:149-172`). Covered by `tests/test_replay_integrity.py`.
- Fragment boundaries: the CSV cursor and pacer survive a part switch; EOS has drained the old
  reader before the next one is built (`sources/replay.py:632-639`).
- Window is half-open `[from, to)` in the data's own domain, independent of retime/loop offset
  (`sources/replay.py:538-546`; `tests/test_replay_source.py:165`).
- The two release fixes hold. `c1864b4`: the old reader is joined before the new cycle's flags are
  reset, and `mark_cycle` drops a stale `FINISHED` but not a coordination failure
  (`sources/replay.py:679-692`, `playback.py:558-568`). `6d3af8b`: an operator restart never
  flush-seeks a running reader; only a loop wrap after EOS does (`sources/replay.py:674-683`), and
  `_finish` ignores a reader being torn down (`:617-618`). I traced a restart against a feeder
  sleeping in the pacer, parked in `wait_if_paused`, and mid-delivery: in each case the frame in
  flight is suppressed by `_deliverable` while `_stop_evt` is set, and stale EOS/quiesce callbacks
  are dropped by the generation check (`:624`, `:630`). Remaining holes of that class are SR-16
  and the per-cycle recurrence of SR-9/SR-18; I found no remaining row-shift on a raw run.
- For a non-coordinated stream-copy run with a `to` window, the tail before `to` was delivered on
  both branches at speed 1 (50/50, `scratch/exp3_streamcopy_to_window.py`). This relies on the
  reader over-running the window before `_quiesce` pauses it; I did not find a failing case.

**Export (`export_recordings.py`, `tools/export-recordings`)**
- Path traversal through the request JSON is closed (details in the X5 row); a destination equal
  to or containing the source is refused, and a non-empty destination is refused, so nothing
  existing can be overwritten.
- Originals: read-only mount, no network, dropped capabilities, hashes re-checked after each
  session.
- Timestamps: the CSV is re-emitted field-for-field and re-parsed (`:461-466`, `:478-479`); every
  proxy frame's stored PTS is checked against the CSV with one fixed offset and 1 ms tolerance,
  across parts (`:238-250`, `:469-477`); frame-count mismatch in either direction fails.
- Encoder errors release the blocked producer and surface as a failure (`:339-347`, `:381-382`);
  the host helper removes its container on interruption (`tools/export-recordings:135-139`).
- Host preflight needs only the standard library: `tests/test_proxy_export.py` (19 tests) passes
  on a Python without `gi`.

**Live sources**
- USB hotplug: the reconnect loop stops the dead pipeline *before* `reopen()` checks for the
  device node (`pipeline.py:1243`, `sources/usb.py:64-70`), so the old v4l2 fd is not held while
  waiting for the node to return.
- RTSP NTP mapping: the 1900→1970 offset is right and applied only to `x-ntp` references;
  frames without the meta are extrapolated on the same timeline; the anchor is dropped on reopen
  (`sources/rtsp.py:35`, `:217-230`, `:164`). `Gst.Buffer.get_reference_timestamp_meta()` with the
  argument omitted works on PyGObject (checked on 1.24.2).
- RTSP probe run from the reconnect thread while the service main loop owns the default context:
  works (the main loop dispatches the watch/timeout; `g_main_loop_run` in the second thread waits
  for `quit`). The leak is SR-5.
- shm input: header magic/version/unknown pixel code are rejected (`transport.py:100-114`), and
  size/geometry/format mismatches are a legible fatal rather than silently mis-sized frames
  (`sources/shmsrc.py:156-164`, `:181-184`, `:197-200`).
- Reconnect thread leaks: one worker per outage, flag cleared in `finally`, old pipeline set to
  NULL and replaced (`pipeline.py:1226-1237`, `sources/gstbase.py:139-149`); I found no growth per
  reconnect other than SR-5.

---

## 6. Severity summary

18 findings: 4 high, 9 medium, 5 low. "Run" = confirmed-by-running; "Read" = confirmed-by-reading.

| ID | Severity | Conf. | Where | One line |
| --- | --- | --- | --- | --- |
| SR-9 | **high** | Run | `sources/replay.py:513-522`, `:565-566` | Stream-copy H.264/H.265 replay deadlocks at preroll whenever leading frames are unpaced (`from_s`, epoch after session start, `speed: 0`): no frames, no error, never finishes. |
| SR-1 | **high** | Run | `sources/replay.py:267-271` | `retime: wall` is anchored at `open()`, not at release: every stamp is stale by the hold time (and ahead by `from_s`). |
| SR-10 | **high** | Run | `sources/replay.py:435-450`, `:513-521` | Coordinated startup never reaches `held` for stream-copy H.26x when `fps + 2 <` decoder threads (≤ 13 fps on a 16-core host). |
| SR-18 | **high** | Run | `sources/gstbase.py:210-232`, `sources/replay.py:513-520` | Stream-copy MJPEG replay: after any decode-queue drop (pre-window burst, `speed: 0`) every consumer image carries an earlier frame's id and timestamp. |
| SR-2 | medium | Run | `sources/pcapsrc.py:302-327` | pcap `restart`: cycle offset from a partial pass and no pacer reset → backwards stamps + unpaced bursts, or seconds of silence. |
| SR-3 | medium | Read | `sources/gstbase.py:100`, `:126-137`, `:197-198` | USB/RTSP/shm liveness watchdog uses the wall clock; a clock step forces a reopen of every such source. |
| SR-4 | medium | Read | `sources/rtsp.py:56,102,135,144`; `sources/gstbase.py:72` | RTSP URL with embedded credentials is logged on every start and reconnect. |
| SR-5 | medium | Run | `sources/rtsp.py:93-99` | `_probe_rtsp` leaks a bus watch (+2 fds) per reconnect attempt; ignores `stop_event`. |
| SR-6 | medium | Read | `sources/pcapsrc.py:346-355` | A non-`ValueError` failure in the pcap feeder ends the run as a clean finish (exit 0). |
| SR-11 | medium | Run | `playback.py:268-296` | `resume`/`set_speed` while the feeder sleeps through a gap or lead-in: gap not adjusted, following data bursts, camera runs ahead of the shared timeline. |
| SR-12 | medium | Run | `playback.py:435-444`, `:261-266` | Legacy gate already past at start: timeline starts "now", camera stays late by the lateness (pinned by `tests/test_playback.py:332-336`). |
| SR-13 | medium | Run | `playback.py:405-413`; `replay_coordination.py:150-174` | After release, any control-dir read/write hiccup ends a running replay; 10–46 fsync'd status rewrites per second. |
| SR-15 | medium | Read | `sources/gige.py:64-68`, `:116` | GigE frames are sliced by the configured pixel format, not the device's actual one. |
| SR-7 | low | Run | `pcapio.py:175-181` | pcapng IDB option with a length but no value raises `IndexError`. |
| SR-8 | low | Run | `uvcpcap.py:523` | UVC reassembly buffer is unbounded when a frame never ends. |
| SR-14 | low | Read | `tools/export-recordings:101-102`, `:133-134` | Export result reports the host bundle's implementation hash for the image's converter; no consistency check. |
| SR-16 | low | Run (emulated) | `sources/replay.py:632-639`, `:660` | A reader rebuild that raises at a part/session boundary leaves replay wedged instead of failed. |
| SR-17 | low | Read | `sources/usb.py:73` | USB caps round the frame rate to an integer; fractional device modes cannot negotiate. |

Contract rows not fully met: **R7** (SR-1, SR-9, SR-11, SR-12, SR-18; legacy preview frame),
**R8** (`replay.sessions: []` rejected, `config.py:474-479` / `playback.py:70-75`),
**X4** (`required_bytes` never reported). R10 is met in form but exposed by SR-10 and SR-13.
