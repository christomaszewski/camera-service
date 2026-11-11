# 01 — Core frame data path review (`core-driver/cam_driver/`)

Tree under review: `the repository root` @ 6d3af8b (v1.13.0), read-only.
Scope: `pipeline.py`, `recorder.py`, `recording_settings.py`, `sidecar.py`, `transport.py`, `gst_frame.py`,
`formats.py`, `timestamps.py`, `dropstats.py`, `keyframe.py`, `bayer_tile.py`, `thermal_preview.py`
(all read in full), plus `session.py`, `main.py`, `lifecycle.py`, `camera.py`, `sources/{base,gstbase,gige,usb,rtsp,shmsrc}.py`
read as callers/callees.

Experiments were run with `/usr/bin/python3.12` + GStreamer **1.24.2** (the JP7 version) against the
unmodified tree (`sys.dont_write_bytecode`, outputs outside the repository). Scripts:
`exp/`.

Counts: 0 critical, 3 high (DP-1, DP-2, DP-5), 7 medium (DP-3, DP-4, DP-6, DP-7, DP-8, DP-9, DP-17), 7 low (DP-10 … DP-16; DP-14 is *suspected*). IDs are in order of discovery; the summary table at the end is sorted by severity.

---

## 1. Defects

### DP-1 — Tight (unpadded) frames are fed to GStreamer video elements: any width not divisible by 4 records black frames silently, or aborts the process
- **Severity:** high (critical for any camera/ROI it applies to; no shipped sensor config triggers it — all are multiples of 4)
- **Where:** `pipeline.py:285-288` (caps + `bytes_per_frame`), `pipeline.py:113-120` / `session.py:61-68` (`Gst.Buffer.new_wrapped(payload)`, no `GstVideoMeta`), `formats.py:130-145` (`bytes_per_frame` = tight size), `pipeline.py:815-832` (`_recording_plan` only checks `% 2`), `session.py:285-306` (bus WARNING ignored).
- **What is wrong:** The core pushes row-tight buffers (`width*height*bpp`, as the camera delivers them) into `appsrc` with plain `video/x-raw,format=…,width=W,height=H` caps and no `GstVideoMeta`. GStreamer's default layout for those caps uses a stride of `ROUND_UP_4(width*bpp)` (and for I420, `ROUND_UP_4(ROUND_UP_2(w)/2)` chroma strides), so when the width is not a multiple of 4 the buffer is *smaller* than every video element expects.
  - Recorder paths with a converting `videoconvert` (`hw-hevc-lossless`, `x265-lossless`, `x264`): `gst_video_frame_map` fails, `videoconvert` posts a bus **WARNING** ("invalid video buffer received") and pushes a blank output frame. `RecordingSession._on_bus` has no WARNING branch, so nothing is logged; the session reports `frames=N`, `error=None`, `recording_lossy=false`, the CSV has N rows — and every frame in the `.mkv` decodes to **all zeros**.
  - FFV1 path (16-bit/thermal, colour, any host without NVENC, explicit `encoder: ffv1`): `videoconvert` is passthrough, `avenc_ffv1` gets the short buffer → `gst_video_codec_frame_unref: assertion 'frame->ref_count > 0' failed` → `malloc(): unsorted double linked list corrupted` → **SIGABRT of the whole core** (transport and all), i.e. a supervisor crash-loop whenever recording is active.
- **Trigger:** a GigE camera whose width is not a multiple of 4 (e.g. ICX274-class 1626x1236, 659x494, 782x582), or any `camera.roi` with such a width (`config.py` does not validate ROI width; `_recording_plan` only rejects odd sizes for x264/tiling). Also `camera.type: shm` pinned to such a width, and GRAY16 with odd width.
- **Evidence (confirmed by running, GStreamer 1.24.2, real `build_recorder_description` fragment + real `RecordingSession`):**
  ```
  GRAY8 1626x16 enc=x265-lossless: push results={'ok': 20}  session error: ''  frames: 20 truncated: False
  10x WARNING from videoconvert0: … gstvideofilter.c(296): gst_video_filter_transform (): invalid video buffer received
  decoded frame 0: size=26048 pushed value=7   decoded min/max/mean=0/0/0.0
  decoded frame 9: size=26048 pushed value=151 decoded min/max/mean=0/0/0.0
  (control, width 1624: decoded min/max/mean=7/7/7.0 … 151/151/151.0)

  GRAY8 1626x16 enc=ffv1      -> GStreamer-Video-CRITICAL gst_video_codec_frame_unref … ; malloc(): unsorted double linked list corrupted ; exit=134
  GRAY8  659x16 enc=ffv1      -> same, exit=134
  GRAY16_LE 641x16 enc=ffv1   -> same, exit=134
  RGB 1001x16 enc=ffv1        -> 20 frames "ok", no error, 9 KB file for 20 random 48 KB frames, 0/20 match
  GRAY8 1626x16 enc=x264      -> 20 frames "ok", no error, 0/20 match
  ```
  (`repro/row_stride_recording.py`, `exp/stride_one.py`, `exp/stride_bus.py`.) `hw-hevc-lossless` could not be run here (no NVENC) but has the same `videoconvert ! video/x-raw,format=NV24` head as the x265 fragment (`recorder.py:212-217` vs `229-233`).
- **Related fallout of the same root cause:** the preview branch (`pipeline.py:318`, `videoconvert`) and any GStreamer consumer of the unixfd endpoint (native `video/x-raw` caps, tight memfd, `pipeline.py:347-356,676-692`) see the same undersized buffers. The bridges document the convention ("The core pushes tight (width-stride) buffers", `plugins/ros2-bridge/src/cam_unixfd_bridge.cpp:27`) but the core's own recorder does not honour it. Conversely gstbase sources deliver GStreamer-padded buffers (`extract_dup` of a `videoconvert` output), so the two source families disagree on the wire layout whenever padding is non-zero, and `shmsrc` raw framing rejects a correct writer at such widths because `_expected` is the tight size.
- **Confidence:** confirmed-by-running (x265/x264/ffv1 on 1.24.2); hw-hevc by reading.
- **Existing tests:** none for the live path. `tests/test_proxy_export_gst.py:238-254` knows about the problem ("Even widths that need row padding must survive…") but its fixture pads rows by hand before writing; `test_recorder.py`, `test_pipeline_*`, `test_lossless_colorimetry.py` only use widths divisible by 4.
- **Suggested fix:** in `build()` compute `GstVideo.VideoInfo` for the caps and compare `info.size` with the tight size; if they differ either (a) attach `GstVideoMeta` with the tight strides/offsets to every buffer pushed into `camsrc`/`recsrc`/`unixfd_src` (`buf.add_video_meta_full`), or (b) refuse to build with a legible error. Log (at least once) bus WARNINGs from the session pipeline, and treat `invalid video buffer` as a session error.

### DP-2 — Stream-copy keyframe gate has no fallback: a stream with no IDR/IRAP records nothing, forever, while the state reads "active"
- **Severity:** high
- **Where:** `session.py:152-159` (gate), `keyframe.py:22-24, 91-104` (H.264 = NAL type 5 only; H.265 = NAL types 16-21 only), `pipeline.py:746-761`.
- **What is wrong:** A stream-copy session sets `_await_key = True` and skips every frame until `is_sync_point()` is true. There is no timeout, no "recovery-point SEI / intra-refresh" rule and no escalation: `skipped_awaiting_keyframe` just climbs. `frames` stays 0, so `begin_close` sends no EOS and the session finalizes with zero segments and `error: None`.
- **Trigger:** the camera the shipped sensor config is written for. `core-driver/config/sensors/cam_rtsp.yaml:22-25` documents the SIYI ZR30: "H.265 mode: NO random-access point, ever (NAL histogram: 45 s / 1076 frames / 0 IDR/CRA -- rolling intra refresh)", with `recording: {enabled: true, encoder: auto}` (stream-copy). In that mode every activate produces an empty session. The same applies to H.264 encoders that use non-IDR I-slices + recovery-point SEI. In the ZR30's H.264 mode (real IDRs, "~15-20 s GOP") every session silently discards its first 0-20 s, and every mid-session reconnect is *not* re-gated (see DP-9).
- **Evidence:** `session.py:156-158`
  ```python
  if not is_sync_point(media, payload, sf, nal_len):
      self.skipped_awaiting_keyframe += 1
      return PushResult.SKIPPED
  ```
  `keyframe.py:99-104` returns True only for `(hdr & 0x1F) == 5` / `((hdr >> 1) & 0x3F) in range(16, 22)`. `health.py:321-336` reports recording `OK "recording"` whenever state is active and there is no error — it never looks at `frames` not moving.
- **Confidence:** confirmed-by-reading (the "0 IRAP" fact is the repo's own measurement in `cam_rtsp.yaml`/`formats.py:89-93`).
- **Existing tests:** `tests/test_keyframe.py` and `tests/test_session.py:279` pin the gate itself (skips until IDR); nothing covers "the sync point never arrives".
- **Suggested fix:** bound the wait (e.g. N seconds or N frames, then start anyway with a loud warning and a `started_without_sync_point` attestation), accept H.264 recovery-point SEI / H.265 with `intra-refresh`, and make health report recording WARN/ERROR when an active session's `frames` has not advanced while the stream is delivering.

### DP-3 — Sidecar JSON is rewritten in place at close: a failed/aborted summary write destroys the header written at session start
- **Severity:** medium
- **Where:** `sidecar.py:107-127` (`write_summary`), contrast `recording_settings.py:54-67` (`write_snapshot`: tmp + fsync + `os.replace`).
- **What is wrong:** `write_summary` reads the header JSON, then `open(self._json_path, "w")` (truncate) and `json.dump`s the merged dict. If the write fails after the truncate (ENOSPC, EIO, read-only remount) or the process is killed in between, the file is left empty/partial. The exception is caught and only logged as a warning (`"could not write sidecar summary"`), so the session result still says ok. The header carries what replay/export cannot reconstruct: `base_timestamp_ns`, `pixel_format`, `bayer_pattern`, `cfa_tile_mode`, `recording_colorimetry`.
- **Trigger:** disk full — exactly the case that ends a session with an error and immediately runs `finish_close → write_summary` (`session.py:260-264`) while other recorders on the same volume keep consuming freed blocks; or the supervisor's SIGKILL landing during finalization.
- **Evidence:** `sidecar.py:123-127`
  ```python
  with open(self._json_path, "w") as f:
      json.dump(data, f, indent=2)
  log.info("wrote sidecar drop summary: %s", summary)
  except (OSError, ValueError) as e:
      log.warning("could not write sidecar summary: %s", e)
  ```
  `write_header` (`sidecar.py:101-105`) is likewise non-atomic and un-fsynced.
- **Confidence:** confirmed-by-reading.
- **Existing tests:** `tests/test_sidecar.py` / `test_recording_resilience.py` cover header-write failure and CSV-writer failure, not a failing summary rewrite preserving the header.
- **Suggested fix:** write to `<json>.tmp`, flush + fsync, `os.replace` (reuse `write_snapshot`); on failure leave the original header untouched and attest the failure in the session result.

*(DP-2 and DP-3 re-checked by running `exp/quick_checks.py`: a TRAIL_R-only H.265 AU and an H.264 recovery-point-SEI + non-IDR AU both return `is_sync_point == False`; a summary write that raises ENOSPC leaves the 809-byte header at 0 bytes with only a WARNING. Confidence for both: confirmed-by-running.)*

### DP-4 — A PTS rebase that happened before a session opened is invisible in that session's sidecar: the header's `pts_convention`/`absolute_time` formulas are false and `pts_rebases` attests 0
- **Severity:** medium
- **Where:** `pipeline.py:526-536` (`_pts_skew`), `pipeline.py:779-813` (`_session_header`: `base_timestamp_ns=int(self._base_ts)`, never sets `pts_offset_ns`), `sidecar.py:50-51,61`, `session.py:246` (`DropStats.delta`), `README.md:130`.
- **What is wrong:** `_pts_for` absorbs every backward step / >60 s forward step into the process-lifetime `_pts_skew`, so from then on `pts = timestamp_ns - base + skew`. The session header still publishes the process base with `pts_convention: "pts_ns = timestamp_ns - base_timestamp_ns"` and `absolute_time: "absolute_ns = pts_ns + base_timestamp_ns"` (also the README's documented formula). The only attestation that the formula is broken is the `pts_rebases` counter — but a session's JSON carries the *delta* since the session opened, so a rebase that happened earlier (while inactive, or in a previous session) reads `pts_rebases: 0`. `SidecarHeader.pts_offset_ns` ("a session-local rebase applied before the muxer, 0 unless a platform needed it") exists for exactly this and is never written by anything (dead field; only `export_recordings.py:431` hard-codes 0).
- **Trigger:** any RTSP camera whose clock is behind the host (or >60 s ahead): the first frames carry host arrival time until the first RTCP SR, then jump to the camera's clock → one rebase a few seconds after start, every start. Every session activated after that (second and later sessions; or the first, when booted inactive) is affected. Same after any GigE reconnect with a clock reset.
- **Evidence (confirmed by running `exp/quick_checks.py` against the real `CapturePipeline._pts_for` / `_session_header`):**
  ```
  header.pts_convention : pts_ns = timestamp_ns - base_timestamp_ns
  header.base_timestamp_ns + first_pts_ns - first_timestamp_ns = 2000000000 ns  (should be 0 per the convention)
  header.pts_offset_ns  : 0   pipeline._pts_skew: 2000000000
  session drops delta   : {... 'pts_rebases': 0}
  ```
- **Impact:** anything that reconstructs absolute time from the `.mkv` PTS + header (the documented join for third-party tools) is off by the skew — silently. The CSV's per-row `timestamp_ns` stays correct, and the CSV↔mkv join on `pts_ns` stays exact.
- **Confidence:** confirmed-by-running.
- **Existing tests:** `test_pipeline_pts.py` pins the rebase arithmetic and its counter; `test_session.py:234` pins the header being written once. Nothing checks the header against a non-zero skew.
- **Suggested fix:** write `pts_offset_ns = self._pts_skew` (read under `_base_lock`) into the header and make the convention string `pts_ns = timestamp_ns - base_timestamp_ns + pts_offset_ns`; additionally attest "rebased before/within this session" (e.g. `pts_rebases_total` next to the delta).

### DP-5 — One silent unixfd consumer pins ~290 frames of memfd in the core, then stalls the transport for every consumer and makes shutdown hang before the camera is released
- **Severity:** high (JP7 / GStreamer ≥ 1.24 transport, i.e. the production path on Orin JP7)
- **Where:** `pipeline.py:353-356` (`appsrc max-bytes=8 frames ! queue max-size-buffers=8 ! unixfdsink`), `pipeline.py:70-75` (the bound the feeder believes it enforces), `pipeline.py:667-694` (one memfd per published frame), `pipeline.py:1092-1103` (EOS + 5 s force-quit), `pipeline.py:1354-1358` (`pipeline.set_state(NULL)` runs *before* `_join_reconnect()` and `source.close()`).
- **What is wrong:** `unixfdsink` keeps a reference on every buffer it has sent to a client until that client sends a release, and sends with a blocking socket write. A consumer that stays connected but stops reading (a bridge hung in a callback, deadlocked in DDS, SIGSTOPped, being debugged) therefore (1) pins every published frame's memfd until the socket send buffer (`wmem_default` 212992) is full — far beyond the "8 frames, stay lean" bound `_queue_full` enforces on the appsrc; (2) then blocks the sink's streaming thread, so *all other* consumers of the endpoint (ros2-bridge and webrtc-bridge share it) get nothing, with only `publish_drops` climbing; (3) keeps EOS from draining at stop, and `set_state(NULL)` on the main pipeline blocks indefinitely — `shutdown()` never reaches `_join_reconnect()` / `source.close()`, so a GigE camera's control privilege is not released and the process ends by the supervisor's SIGKILL.
- **Trigger:** any connected unixfd consumer that stops servicing its socket while the core keeps publishing.
- **Evidence (confirmed by running on GStreamer 1.24.2, same launch fragment + memfd publish + fill check as the pipeline; client = a unix socket that connects and never reads; `repro/unixfd_silent_consumer.py`, `exp/quick2.py`):**
  ```
  5 MB frames: first publish drop at frame 290: accepted=290
  memfd:cam fds still open in the core: 291 -> 1455 MB of tmpfs pinned; meminfo Shmem delta = 1383 MiB
  (1 MB frames: 295 accepted, 296 fds)
  stalled after 295 frames; sending EOS
  EOS within 5 s: False
  set_state(NULL) STILL BLOCKED after 15 s (sink stuck in a blocking send to the silent client)
  ```
  For 4K I420 (12.4 MB/frame) the same ~290 frames are ~3.6 GB of shmem charged to the core's cgroup — the "grow RAM until the OOM killer takes the recording with it" outcome the comment at `pipeline.py:70-73` says the bound prevents. The recording itself is safe at stop (the session is finalized before the main pipeline is touched).
- **Confidence:** confirmed-by-running (1.24.2) for the pinned memory, the missing EOS and the blocked NULL transition. That *other* consumers starve is inferred from the sink's streaming thread being stuck in render (not run with a second client). Not checked on 1.28.
- **Existing tests:** none. `tests/check_transport_caps.py` / `test_shm_source.py` use unixfd only with well-behaved peers; `test_recording_resilience.py:218` covers a stalled *recorder*, not a stalled consumer.
- **Suggested fix:** bound in-flight frames in the feeder rather than at the appsrc (count published-but-unreleased buffers via a `GstMiniObject` weak-ref/dispose notify on the memfd memory and drop when above N); put the transport chain in its own pipeline (as the recorder already is) so its teardown can be abandoned with a timeout; in `shutdown()` release the source (`_join_reconnect()` + `source.close()`) *before* `pipeline.set_state(NULL)`, or run the NULL transition on a helper thread with a deadline. Health should flag "publish_drops rising while frames are arriving".

### DP-6 — `parse_pixel_format` silently maps every GVSP name it does not recognise to 8-bit GRAY8 (Mono14, Bayer*14, RGB8, YUV422_8, typos)
- **Severity:** medium (high for an affected camera: thermal/scientific GigE cameras commonly deliver Mono14)
- **Where:** `formats.py:110-127` (`bits = 16 if any(tok in pf for tok in ("16", "12", "10")) else 8`; anything not `Bayer*` becomes mono), mirrored in `sources/gige.py:64-68` (`_compute_image_size`), consumed at `pipeline.py:273-278` (only `packed` warns).
- **What is wrong:** `pixel_format()` comes from the *device* (`camera.py:293-304`), so a camera left in / falling back to Mono14, BayerRG14, RGB8, YUV422_8 … is labelled `GRAY8`, 8 bits, 1 byte/pixel. `GigeSource` then slices each 2- or 3-byte/pixel payload to `w*h` bytes (`gige.py:116`), i.e. records and publishes the first half/third of the frame as a full-size GRAY8 image, auto-selects the 8-bit HW-HEVC path, and writes `bits_per_pixel: 8` into the sidecar. No error, no warning. (`camera.configure` only warns when the configured `pixel_format` is rejected, `camera.py:167-171`.)
- **Evidence (confirmed by running `exp/quick2.py`):**
  ```
  Mono14      -> gst=GRAY8 bits= 8 bayer=None packed=False color=False  bytes/frame(640x512)=327680
  BayerRG14   -> gst=GRAY8 bits= 8 bayer=rggb …
  RGB8        -> gst=GRAY8 bits= 8 …      YUV422_8 -> gst=GRAY8 bits= 8 …      nonsense -> gst=GRAY8 bits= 8 …
  ```
- **Confidence:** confirmed-by-running (parser); the GigE truncation by reading.
- **Existing tests:** `tests/test_formats.py` has no Mono14 / RGB8 / unknown-name case (`grep -n "Mono14\|RGB8\|YUV422"` → nothing).
- **Suggested fix:** parse the bit depth from the name (`Mono(\d+)`, `Bayer..(\d+)`): >8 → 16-bit container; reject (raise at `build()`) any name that is neither a known GStreamer raw format nor `Mono<n>`/`Bayer<xx><n>`; in `GigeSource` compare the first frame's payload size with `w*h*bpp` and refuse on mismatch instead of slicing.

### DP-7 — The session's sidecar header is built on the capture thread with live device I/O, and any non-`OSError` from it silently loses the header for the whole session
- **Severity:** medium
- **Where:** `session.py:174-183` (first push: `first_pts` is set, then `write_header(self._header_factory(...))` guarded by `except OSError` only), `pipeline.py:783,793` (`self.source.geometry()` / `self.source.pixel_format()` inside `_session_header`), `camera.py:289-291` (`sensor_geometry` → `camera.get_region()`, GVCP reads under `io_lock`, `GLib.Error` not caught), `sources/gige.py:102-120` (`_on_new_buffer` has no `except`).
- **What is wrong:** (a) For GigE the first frame of every session performs several GVCP register reads on the Aravis new-buffer thread while holding the session lock (and waits for `io_lock`, which the health poller and `reopen()` also take) — the GVSP callback is stalled for the duration, and `begin_close` on the main loop waits behind it. `build()` already cached `_width/_height/_gst_format`. (b) If the factory raises anything but `OSError` (a `GLib.Error` from `get_region()` on a flaky control channel, a `TypeError` from `json.dump`), the exception escapes `push()` → `_feed_recording` → `_on_frame` (skipping the transport publish and `_account` for that frame) into the Aravis callback. `first_pts` was already set, so the header is **never retried**: every later frame is muxed and gets a CSV row, the session closes with `error: None`, and the JSON contains only `drops` + `session` — no `base_timestamp_ns`, pixel format, Bayer pattern or colorimetry.
- **Evidence (confirmed by running `exp/quick2.py`, real `RecordingSession`, factory raising `GLib.Error`):**
  ```
  push results: ['RAISED Error', 'ok', 'ok', 'ok', 'ok']  header_factory calls: 1
  frames: 4 error: None json: dict_keys(['drops', 'session'])
  ```
- **Confidence:** confirmed-by-running (b); (a) confirmed-by-reading.
- **Existing tests:** `test_recording_resilience.py:201` (`test_first_frame_header_write_failure_does_not_kill_capture`) covers the `OSError` case only.
- **Suggested fix:** build the header from values cached at `build()` (no device call on the feed thread); catch `Exception` around the header write and turn it into `_record_error`; set `first_pts` only after the header is on disk.

### DP-8 — Frame-id gap accounting is structurally blind for USB / RTSP / shm-raw sources, but the sidecar still attests `source_gaps: 0, frames_missing: 0`
- **Severity:** medium (misleading fidelity attestation)
- **Where:** `dropstats.py:4-10,27-38` (claims ids are "GVSP block/chunk id, v4l2 buffer sequence, RTP seq"), `pipeline.py:559-572` (`_account`), versus `sources/gstbase.py:196-208` (`frame_id=self._frame_id; self._frame_id += 1` — a host counter minted when the buffer is seen), `sources/usb.py:75` and `sources/shmsrc.py:114` (`appsink … max-buffers=4 drop=true`).
- **What is wrong:** `observe_frame` can only see loss as a jump in the id. For every `GstPipelineSource` the id is a contiguous host counter: on the raw path it is minted in `_on_raw` *after* the appsink has already dropped (`drop=true`) whatever the Python callback was too slow to pull; on the encoded path it is minted at the pre-tee probe, downstream of any RTP/USB loss. So `source_gaps`/`frames_missing` are always 0 for these sources, regardless of how many frames were lost, and that zero is written into every session JSON (`session.py:246`, `sidecar.py:116`) and shown by health as "no drops".
- **Trigger:** raw USB camera (e.g. a 60 fps Y16 thermal core) while `_on_frame` is slow (tiling, large copies, GIL contention): the appsink discards frames; ids stay contiguous. RTSP packet loss likewise.
- **Evidence:** `gstbase.py:205-207`
  ```python
  st = FrameStamp(frame_id=self._frame_id, timestamp_ns=ts, source=src, system_ns=now, camera_ns=cam, chunk_ns=None)
  self._frame_id += 1
  ```
  and `usb.py:75` `raw_sink = "appsink name=rawsink emit-signals=true max-buffers=4 drop=true sync=false"`.
- **Confidence:** confirmed-by-reading.
- **Existing tests:** `tests/test_dropstats.py` tests the arithmetic on synthetic ids only; nothing asserts that a dropped appsink buffer is visible.
- **Suggested fix:** use the real sequence where one exists (v4l2src puts the driver sequence in `buffer.offset`; RTP seq/extended timestamp for RTSP), or at least read the appsink's dropped count / mint the id in a pad probe *upstream* of the dropping appsink; until then write `frame_id_source: "host_counter"` into the header and do not present 0 as a measurement. Fix the `dropstats.py` docstring.

### DP-9 — A stream-copy session is not reconnect-safe: caps are applied once, the keyframe gate is not re-armed, and a parameter-set change ends the session with a mux error
- **Severity:** medium
- **Where:** `session.py:160-165` (`if caps_str and not self._caps_applied`), `session.py:105,152-159` (`_await_key` only at construction), `pipeline.py:1263-1284` (reconnect resets only the publish window and the PTS memo), `sources/rtsp.py:154-161` (`SourceConfigChanged` only when codec/width/height differ).
- **What is wrong:** by design the session stays open across a source reconnect. For H.264/H.265 stream-copy that means: (1) whatever the reopened stream delivers first is muxed — if it joins mid-GOP (the repo's own note: "every reopen joins mid-GOP", `cam_rtsp.yaml:12-13`) the file contains P-frames whose references were never recorded, until the next IDR (the session-start gate exists precisely to prevent this, but only for frame 0); (2) the new stream's caps (`codec_data`) are ignored. If the parameter sets differ (camera encoder setting changed, camera rebooted into a different profile/level at the same resolution), either `h264parse` picks up in-band sets and `matroskamux` refuses the caps change — session ERROR, recording stops until someone re-activates — or (sets only in the SDP) the tail is muxed against the old CodecPrivate.
- **Evidence (confirmed by running `exp/caps_change.py`: real stream-copy fragment from `build_recorder_description`, real `RecordingSession`, 30 AUs of x264 baseline then 30 AUs of x264 high at the same geometry, each with its own caps string):**
  ```
  codec_data differs: True
  session: 60 frames, error: gst-stream-error-quark: Could not multiplex stream. (10) | …gst_matroska_mux_video_pad_setcaps … Caps changes are not supported by Matroska
  decoded 30 of 60 recorded frames (first stream = 30)
  (control, identical parameter sets: 60 frames, no error, 60/60 decoded)
  ```
  Note `frames_recorded` (and the CSV) say 60 while the file holds 30.
- **Confidence:** confirmed-by-running for the mux-error variant; the "silently undecodable tail" and "mid-GOP join" variants are by reading (suspected until run against a real camera).
- **Existing tests:** `test_session.py:273` pins "applies caps once"; `test_pipeline_reconnect.py` never has a session open on an encoded source.
- **Suggested fix:** on a successful reconnect of a stream-copy source, roll the session (finalize + open a new one under the same run id) or at minimum re-arm `_await_key`; compare each delivered `caps_str` with the applied one and roll on change.

### DP-10 — Sidecar CSV durability lags the video by up to ~100 rows, and dropped CSV rows are not attested
- **Severity:** low (medium for slow cameras)
- **Where:** `sidecar.py:147-161` (flush only when `n % 100 == 0`, never time-based, never fsync), `sidecar.py:140-145` (`queue.Full` → `_dropped += 1`, log only), `sidecar.py:119-122` (only `_failed` is attested).
- **What is wrong:** on SIGKILL / OOM / power cut the `.mkv` holds frames whose timestamp rows are still in the Python buffer: up to ~100 frames = 10 s at 10 fps, 100 s at 1 fps (replay then treats them as unindexed). Separately, rows dropped on a full queue make CSV rows < `frames_recorded` with nothing in the JSON saying so.
- **Confidence:** confirmed-by-reading. **Tests:** `test_sidecar.py` has no flush-cadence or dropped-row-attestation case.
- **Suggested fix:** flush on a timer (≤1 s) as well as by count; write `sidecar_rows_dropped` into the summary.

### DP-11 — Failed session starts leak the bus watch (2 fds each)
- **Severity:** low
- **Where:** `session.py:135-143` (`add_signal_watch` then, on failure, `_teardown_pipeline()`), `session.py:267-275` (teardown never calls `remove_signal_watch`; only `finish_close` does, `session.py:218-219`).
- **Evidence (confirmed by running `exp/leak.py`, pipeline that refuses PAUSED):** `100 activations: 100 failed in start(); open fds 11 -> 211 (+200)`.
- **Trigger:** an orchestrator retrying `activate` while the session pipeline cannot start (encoder device unavailable, NVENC session limit).
- **Tests:** `test_session.py:222` (`test_start_failure_leaves_nothing_half_open`) uses stubs and does not check the watch; the 110-cycle leak test only covers successful sessions.
- **Suggested fix:** `remove_signal_watch()` in `_teardown_pipeline` when a watch was added.

### DP-12 — Bus WARNINGs are discarded on both pipelines
- **Severity:** low (it is what makes DP-1 silent)
- **Where:** `pipeline.py:1303-1313` (`_on_bus`: ERROR and EOS only), `session.py:285-306` (ERROR, EOS, ELEMENT only); `finish_close` pops with an `EOS | ERROR` filter (`session.py:226-227`).
- **Confidence:** confirmed-by-running as part of DP-1 (10 WARNINGs posted, none logged). **Suggested fix:** log warnings (throttled) and count them into the session attestation.

### DP-13 — `_clear_stale_socket` unlinks whatever is at the configured path, live or not, socket or not; the unixfd path ignores the configured basename
- **Severity:** low
- **Where:** `pipeline.py:255-260` (`if os.path.lexists(path): os.unlink(path)` — no `S_ISSOCK` check, no connect-probe), `pipeline.py:339` (`os.path.join(os.path.dirname(pe.socket_path) or "/tmp/cam", "unixfd")`).
- **What is wrong:** a mis-set `socket_path` pointing at a regular file deletes it with a "removing a stale … socket" warning; a second core pointed at the same directory (two sensors sharing a socket dir with different `socket_path` basenames works on JP6/shm, but on JP7 both derive `<dir>/unixfd`) unlinks the first instance's *live* socket and silently takes over its consumers. The docstring's safety argument ("each sensor owns its own socket volume") is a deployment convention the code does not check.
- **Confidence:** confirmed-by-reading. **Tests:** none in `tests/` (only the `dev_test.sh` smoke greps the log line).
- **Suggested fix:** unlink only when `stat.S_ISSOCK` (or a dangling symlink) and a non-blocking `connect()` is refused; derive the unixfd name from the configured basename (or add an explicit `unixfd_path`).

### DP-14 — Reconnect worker does not re-check `_stopping` after re-arming the source
- **Severity:** low — **suspected** (narrow window; not reproduced)
- **Where:** `pipeline.py:1249-1262` (stop check is only between `reopen()` and `_start_source()`), `pipeline.py:1284`, `pipeline.py:1335,1349-1350` (`stop_source = not self._stopping` → `shutdown()` skips `source.stop()` when `request_stop()` already ran).
- **What is wrong:** if `request_stop()` (main thread: sets `_stopping`, calls `source.stop()`) lands after the worker's check but before/while it runs `_start_source()`, acquisition is restarted after the stop; `shutdown()` then only calls `source.close()`, which for GstPipelineSource is the base no-op — the source mini-pipeline (v4l2 device / RTSP session) stays PLAYING into interpreter exit; for GigE the stream is finalized with `new-buffer` still armed.
- **What would confirm it:** a test source whose `start()` blocks on an event; call `request_stop()` while it blocks, release, then assert `stop()` was called after the last `start()`.
- **Tests:** `test_pipeline_reconnect.py:101,111,141` cover stop during backoff and during `reopen()`, not during/after `start()`.
- **Suggested fix:** re-check `_stopping` after `_start_source()` and stop+close; make `shutdown()` call `source.stop()` unconditionally (it is idempotent).

### DP-15 — Config knobs parsed but not applied / silently ignored (data-path side)
- **Severity:** low
- `transport.raw_endpoint.max_rate_hz`: the field exists on every `TransportEndpoint` (`config.py:262-268`) but only `plugin_endpoint.max_rate_hz` is ever read (`pipeline.py:409,415`); the raw endpoint always gets every frame.
- `transport.plugin_endpoint.shm_size` and the basename of `socket_path` are ignored whenever `unixfdsink` exists (`pipeline.py:333-356`) — see DP-13.
- `recording.bayer_tile`: an unknown value (`green-diff`, `rtc`) normalizes to `off` with no warning (`bayer_tile.py:46-57`, `pipeline.py:824`); with `recording.bayer_pattern` set on a camera that reports `Mono8`, tiling is silently skipped because `_recording_plan` keys on the parsed `self._bayer`, not the override (`pipeline.py:822` vs `794`).
- `camera.frame_rate` for RTSP: the probed rate never reaches the pipeline (`RtspSource` has no `delivered_frame_rate`), so `_fps` is `None` → caps `framerate=0/1`, `_frame_interval_ns = 1 ms`, decimation falls back to the timestamp gate (`pipeline.py:282-284,410`).
- Caps framerate is rounded to an integer for camsrc/recorder (`pipeline.py:283`: 29.97 → `30/1`, 12.5 → `12/1`, 0.4 → `0/1`) while the unixfd caps use the exact fraction (`pipeline.py:344-346`).
- **Confidence:** confirmed-by-reading.

### DP-16 — Misleading comments / dead code
- **Severity:** low
- `transport.py:1,11-13`: "shm now; unixfd / Zenoh later", "The core prepends this header on the transport tee branch (after the rate-limit drop-probe)" — the header is built in the feeder for a separate appsrc; there is no tee branch or drop-probe.
- `transport.py:16-17,22`: "read `header_len` for forward compatibility" — `unpack_header` ignores `header_len` (`transport.py:104-114`) and the Python reader slices at the constant (`sources/shmsrc.py:160`).
- `dropstats.py:4-6`: ids described as v4l2 sequence / RTP seq (see DP-8).
- `sidecar.py:61` `pts_offset_ns`: never written (see DP-4). `sidecar.py:40,45`: "raw Aravis pixel format string", "GevTimestampTickFrequency" for all sources.
- `pipeline.py:665`: `if True:` scaffolding around the whole publish body.
- `pipeline.py:1100`: `_force_quit` logs "forcing stop (recording may be truncated)" although the session was already finalized synchronously in `request_stop()`; with DP-5 this is the line an operator sees.
- `recorder.py:132-140`: docstring still describes a tee-branch wiring; the recorder is always appsrc-fed (`pipeline.py:904-914`).
- `pipeline.py:537-538`: `if pts < 0: pts = 0` is unreachable for `own=True` (first frame is 0, every later one is forced > `_last_pts`), yet pinned by `test_pipeline_pts.py:126`.
- `docs/recording-tuning.md:16-18` says whole-run replay "preserves the recorded gaps"; `_pts_for`'s 60 s forward-jump collapse (`pipeline.py:90,529-536`) applies to every source, so a re-recorded replay collapses any recorded gap > 60 s in the new file's PTS (counted as a rebase; the CSV keeps the true stamps). `replay.py` has no exemption.

### DP-17 — `ptp_synced` in every session header is a boot-time snapshot; it is not refreshed per session or after a reconnect
- **Severity:** medium
- **Where:** `timestamps.py:87-91` (`_ptp_locked` set only in `resolve_active_source`), `timestamps.py:72-77` (`set_chunk_parser` "keeps the already-resolved active source"), `sources/gige.py:153-159` (`reopen()` never re-resolves; the return of `enable_ptp` inside `camera.reopen`, `camera.py:155-156`, is discarded), `camera.py:253` (`self.ptp_locked = True`, never cleared), `pipeline.py:792` (`ptp_synced=self.source.ptp_locked`).
- **What is wrong:** the header's provenance flag ("True if the chunk timestamp is PTP-disciplined wall-clock") reflects whether the camera reached Slave within `ptp_lock_timeout_s` (10 s) at process start. Two wrong outcomes: (1) boot while the grandmaster is still settling → `ptp_synced: false` for every session of that process, although the camera locks seconds later (and the stamps step to the PTP epoch — a >60 s forward jump that `_pts_for` collapses, see DP-4); (2) boot locked, later the camera power-cycles and comes back unlocked (the reconnect path explicitly allows `enable_ptp` to time out or be abandoned) → every later session still says `ptp_synced: true` over a free-running camera clock.
- **Confidence:** confirmed-by-reading.
- **Existing tests:** `tests/test_timestamps.py:48-113` only exercise `resolve_active_source` once; nothing covers a reconnect or a second session.
- **Suggested fix:** re-resolve after `reopen()` and store the result; sample the PTP status when a session header is built from a cached, health-thread-maintained value (not a device read on the feed thread — DP-7), and record lock transitions in the session JSON.

---

## 2. Improvements & missing features (not defects)

1. **Skip the dead camsrc copy.** With no preview and no raw endpoint (the default config) every frame is still wrapped (a full memcpy in PyGObject) and pushed through `camsrc ! queue ! tee ! queue ! fakesink` (`pipeline.py:599-602`, `319-323`). `_discard_replay_main` already does the right thing for replay only (`pipeline.py:315-316`). At 5 MP / 24 fps that is ~120 MB/s of pointless copying on the capture thread.
2. **One copy per destination.** shm+header publish concatenates `hdr + frame_bytes` (copy) and then `new_wrapped` (copy) (`pipeline.py:707-710`); the GstFrame branch already shows the two-memory form (`gst_frame.py:33-36`).
3. **Escalate a permanently stalled recorder.** A session whose appsrc stays full drops every frame forever with a warning per 100 drops (`pipeline.py:772-776`); state remains `active`. Turn "N seconds of 100 % drops" into a session error / health ERROR. Likewise "active but `frames` not advancing while the stream delivers" (covers DP-2).
4. **Give the transport its own pipeline** (like the recorder): today any ERROR on the main bus — e.g. shmsink's "Shared memory area … is smaller than buffer" when `shm_size` is set below one frame — sets `_fatal` and exits the process (`pipeline.py:1304-1309`). Validate `shm_size >= frame + header` at `build()`.
5. **Validate frame size at the seam.** `_on_frame` never compares `len(frame_bytes)` with `_image_size`; a GigE camera that returns from a power-cycle with a different region/pixel format is not detected (only RTSP raises `SourceConfigChanged`). A cheap length check + counted drop + fatal after N would turn silent garbage into a legible stop.
6. **Crash-safe sidecars.** Atomic header/summary (DP-3), timed CSV flush (DP-10), fsync on `splitmuxsink-fragment-closed` (the hook already exists, `session.py:295-306`), and an fsync of the finished `.mkv`.
7. **Quote/validate paths spliced into launch strings.** `recorder.py:119-120` (`location="{location_base}-%05d.mkv"`: a `"` breaks the description, a `%` in `output_dir`/`name_prefix` becomes part of splitmuxsink's printf pattern), `pipeline.py:311,356,368` (socket paths unquoted: whitespace breaks the parse). `glob.glob(self.path_base + "-*.mkv")` (`session.py:245`) should use `glob.escape`. All are operator-controlled config (remote patches cannot set them; `run_id` is sanitized) — hardening, not an exploit.
8. **Carry the Bayer pattern on the JP6 header transport.** `FrameHeader.pixfmt` is `GRAY8` for a Bayer camera (`pipeline.py:696-699`); `flags`/`reserved` are free. unixfd already carries it in caps.
9. **Record rebase events, not just a count.** `(frame_id, delta_ns, new_skew)` per rebase in the session JSON would make the PTS↔absolute mapping recoverable without the CSV, and would let the 60 s collapse be reviewed; consider exempting finite/replay sources from the forward-jump rule.
10. **Clean up after a refused activation.** `write_snapshot` runs before `sess.start()` (`pipeline.py:948-949`), and `sidecar.start()` has already created the CSV: each failed activate leaves `<prefix>.recording-settings.json` + an empty `<prefix>.csv`.
11. **Disk-space guard at activate / rollover**, instead of learning about a full disk from the muxer's ERROR (after which, with a control plane, recording simply stays off).
12. **Throttle `_push_checked`'s warning** (`pipeline.py:477`): it logs once per frame for as long as an appsrc returns non-OK.
13. **Propagate the delivered rate for RTSP/USB** (`delivered_frame_rate`), so caps, GOP, `_frame_interval_ns` and decimation use the probed rate (DP-15).
14. **Socket/shm access model.** With `ipc: host`, shm segments and the sockets are reachable by any container that mounts the volume / shares the IPC namespace; shmsink `perms` and the socket mode are left at defaults. Document it as the access model or set explicit modes.
15. **Missing feature: packed GVSP formats** (Mono10p/12p) are still "fed as GRAY16 and will misinterpret data" with only a warning (`pipeline.py:276-278`); refuse to build instead (same class as DP-6).

## 3. Test gaps for this area

| Behaviour with no test | Where it should live |
|---|---|
| Live recorder round-trip at a width not divisible by 4 (GRAY8, GRAY16, RGB, I420 with w % 8 == 4), every encoder fragment; `bytes_per_frame` vs `GstVideoInfo.size` (DP-1) | `tests/test_recorder.py` (or a new `tests/test_recording_geometry.py`), `tests/test_formats.py` |
| Session bus WARNING is logged / attested (DP-12) | `tests/test_session.py` |
| Stream-copy session whose sync point never arrives; H.264 recovery-point / H.265 intra-refresh AUs (DP-2) | `tests/test_session.py`, `tests/test_keyframe.py` |
| A failing summary rewrite leaves the header intact (DP-3); time-based CSV flush; dropped rows attested (DP-10) | `tests/test_sidecar.py` |
| Session header vs a non-zero `_pts_skew`; `pts_offset_ns` (DP-4) | `tests/test_pipeline_pts.py`, `tests/test_pipeline_session.py` |
| Silent unixfd consumer: bounded pinned memory, a second consumer keeps receiving, `request_stop`/`shutdown` complete and release the source (DP-5) | `tests/test_recording_resilience.py` (it already has the `live` fixture) |
| Mono14 / Bayer*14 / RGB8 / unknown GVSP names (DP-6) | `tests/test_formats.py` |
| Header factory raising a non-`OSError`; header built without source calls (DP-7) | `tests/test_session.py`, `tests/test_recording_resilience.py` |
| A frame dropped by the source's appsink is visible in the drop accounting (DP-8) | `tests/test_dropstats.py` + `tests/test_coordinated_sources.py` |
| Open stream-copy session across a reconnect: gate re-armed, caps change handled (DP-9) | `tests/test_pipeline_reconnect.py`, `tests/test_session.py` |
| Failed `start()` leaves no bus watch / fd (DP-11) | `tests/test_session.py` (`test_start_failure_leaves_nothing_half_open` only uses stubs) |
| `_clear_stale_socket`: regular file, directory, dangling symlink, live socket (DP-13) | no pipeline-build unit test exists; new `tests/test_pipeline_build.py` |
| Stop landing during/after `_start_source()` in the reconnect worker (DP-14) | `tests/test_pipeline_reconnect.py` |
| `_fps is None` (RTSP): caps `0/1`, timestamp-gate decimation, `_frame_interval_ns` (DP-15) | `tests/test_pipeline_publish_rate.py` |
| `ptp_synced` after a reconnect / in a second session (DP-17) | `tests/test_timestamps.py`, `tests/test_pipeline_session.py` |
| `request_stop()` re-entered from the signal handler in the middle of `activate()` (session published after `_stopping`) | `tests/test_pipeline_session.py` |
| `_publish_transport` shm+header byte layout against the C++ parser for a padded-stride source (gstbase I420, w % 8 == 4) | `tests/test_transport.py` |

## 4. Checked and found sound

- `_pts_for` (`pipeline.py:481-543`): single lock, memo keyed on `(frame_id, timestamp_ns)` with an `is not None` hit test (PTS 0 is served correctly), read-only view for the best-effort branch, two-sided guard that absorbs into a skew, bounded memo; all timestamp math is integer nanoseconds (no float anywhere between `FrameStamp` and the buffer PTS / CSV / header; `u64` masks in `FrameHeader.pack`).
- Session open/close ordering (`pipeline.py:916-983`, `session.py:193-265`): the session is unpublished before it closes; `begin_close` sets `_closing`, EOSes a blocking appsrc before taking the feed lock, joins an in-flight push; no EOS on a zero-frame session; the bus watch is removed before the filtered drain; segments are reconciled against the disk; NULL on every path.
- CSV ↔ muxer correspondence in the normal path: a row is added only after `push-buffer` returned OK, under the session lock; DROPPED vs SKIPPED accounting; queue-full check before the header is written (`session.py:170-191`).
- Lossless round trip at widths divisible by 4: FFV1 1024x16 20/20 byte-exact and x265-lossless 1624x16 exact through the real recorder fragment + `RecordingSession` (control runs of the DP-1 experiment).
- Feeder-side bounds for camsrc / recsrc / shm transport (`pipeline.py:455-470`, `session.py:170-173`) — by reading; the shm (JP6) sink blocks rather than pins, so the appsrc bound holds there (the unixfd exception is DP-5).
- Reconnect worker (`pipeline.py:1226-1301`): `_reconnecting` released in `finally`, `start()` inside the retry `try`, stop during backoff and during `reopen()`, memo and publish window reset; `SourceConfigChanged` → fatal + clean stop.
- Publish decimation (`pipeline.py:400-434`): never exceeds the cap, backward clock step reopens the gate, unixfd caps rate matches the decimated rate.
- `recorder.py`: >8-bit never reaches an 8-bit encoder (falls back to FFV1), availability fallbacks, the colorimetry pin is in the fragment and in the sidecar, x264 CRF/preset validated before the launch string is built, stream-copy disables keyframe requests.
- `recording_settings.py`: remote patches are whitelisted (no path fields), typed and bounded (bool-as-int rejected, non-finite rejected); `write_snapshot` is tmp + fsync + replace and cleans up.
- `transport.py`: 36-byte little-endian layout, additive codes, `unpack_header` checks length / magic / version / pixfmt.
- `keyframe.py`: Annex B 3- and 4-byte start codes, length-prefixed NALs with the size taken from avcC/hvcC, a truncated NAL is never trusted (the *policy* problem is DP-2, not the parser).
- `bayer_tile.py`: tile/untile are exact inverses for all modes × patterns (self-test run: PASS); odd geometry is rejected at plan time.
- `dropstats.py` arithmetic and per-session delta; `sidecar.py` writer-failure path (no deadlock on stop, `sidecar_csv_failed` attested).
- `gst_frame.py`: `copy_region(MEMORY)` + parent-buffer meta, full-write loop in `write_to_fd`.
- `thermal_preview.py`: pure numpy, not on the recording path.
- `run_id` is sanitized (`lifecycle.py:45-54`) before it reaches a file name or launch string.

## Summary table

| ID | Severity | One line | Confidence |
|---|---|---|---|
| DP-1 | high | width % 4 ≠ 0 → black "lossless" recording (videoconvert paths) or core SIGABRT (FFV1) | run |
| DP-2 | high | stream-copy keyframe gate never opens on no-IDR/IRAP streams (the shipped ZR30 H.265 case) → records nothing | run + read |
| DP-5 | high | one silent unixfd consumer pins ~290 frames, stalls all consumers, hangs shutdown before camera release | run |
| DP-3 | medium | summary rewrite truncates the sidecar header on write failure | run |
| DP-4 | medium | pre-session PTS rebase invisible: header formula false, `pts_rebases: 0`, `pts_offset_ns` dead | run |
| DP-6 | medium | Mono14 / RGB8 / unknown GVSP formats silently become GRAY8 (truncated frames) | run + read |
| DP-7 | medium | header built with device I/O on the capture thread; non-OSError → header never written | run |
| DP-8 | medium | gap accounting blind for USB/RTSP/shm (host counter), yet attests 0 | read |
| DP-9 | medium | stream-copy session not reconnect-safe (caps once, gate not re-armed, mux error) | run + read |
| DP-17 | medium | `ptp_synced` is a boot-time snapshot | read |
| DP-10 | low | CSV lags by ≤100 rows; dropped rows unattested | read |
| DP-11 | low | failed session start leaks 2 fds | run |
| DP-12 | low | bus WARNINGs discarded | run |
| DP-13 | low | stale-socket clearing unlinks anything; unixfd basename fixed | read |
| DP-14 | low | reconnect worker can re-arm after stop | suspected |
| DP-15 | low | knobs parsed but not applied | read |
| DP-16 | low | misleading comments / dead code | read |

Note: `git status --ignored` shows `__pycache__/` directories inside the tree (`core-driver/cam_driver`, `plugins/webrtc-bridge/tools`, `plugins/ros2-source/cam_ros2_source`). They were not produced by this review (every script here ran with `sys.dont_write_bytecode`; the cached files are `cpython-313` / for modules this review never imported) and were left untouched.
