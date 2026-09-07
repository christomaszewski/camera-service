"""Recorded-run playback source (a GstPipelineSource).

Feeds the service from a run THIS service previously recorded: `<prefix>-NNNNN.mkv`
parts read seamlessly by splitmuxsrc (the reader pair of the splitmuxsink that wrote
them) + the sidecar CSV/JSON to re-stamp every frame with its ORIGINAL FrameStamp
(frame_id, timestamps, provenance) -- so recording, transport, and plugins downstream
behave as if the original camera were live.

A run directory holds one SESSION per lifecycle activate (each its own <prefix> triple);
the replay plays them back to back in timeline order, one reader pipeline per session,
with the recorded gaps between them honoured as silence. `replay.run` pins one session.

Two shapes, resolved by probing the first .mkv part against the sidecar header (every
session of a run must share one shape):

  LOSSLESS RAW runs (ffv1 / hw-hevc-lossless / x265-lossless):
      splitmuxsrc ! decoder ! videoconvert ! <header format> ! rawsink -> on_frame
    Decoded back to the exact recorded bytes (the codecs are lossless); a CFA-tiled
    recording is un-tiled per frame (bayer_tile.untile_cfa, the exact inverse) so
    consumers see the original mosaic.

  STREAM-COPY runs (USB-MJPEG / RTSP H.26x, header pixel_format == I420):
      splitmuxsrc ! tee -> decode branch -> rawsink (consumers)
                       -> encsink (on_encoded)      -- the recording stream-copies AGAIN,
    byte-faithful to the original capture, via the base's dual-output machinery.

Timeline (playback: block, docs/PLAYBACK.md): frames are paced against the data's own
stamps from a ZERO -- the run's first frame by default, or `epoch_unix_ns` when an
orchestrator hands every producer of a replay the same one -- so a session recorded ten
minutes into the run comes out ten minutes after release, where a sibling bag player is.
`from_s`/`to_s` window that timeline. Pacing/retime/loop semantics are shared with the pcap
source -- see cam_driver.playback.

EOF: the last session's EOS -> `finished` (the main pipeline holds or exits, per
config.hold_on_finish), a flush-seek to 0 (one session) or a rebuild of the first session's
reader when `loop` is on. `restart` from any state -- finished included -- is the same
mechanism. (Loop + stream-copy note: PTS values repeat each cycle, so the best-effort decode
branch's stamp correlation may briefly cross cycles; the recording branch is in-order.)
"""
from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from typing import List, Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from .. import playback
from ..bayer_tile import normalize_mode, untile_cfa
from ..formats import encoded_info, select_decoder
from ..timestamps import FrameStamp, TimestampSource
from .gstbase import GstPipelineSource

log = logging.getLogger(__name__)

# mkv track caps -> the software decode chain for the LOSSLESS RAW path (correctness
# first: bit-exactness is the point of replay, and these all hold real-time here).
_RAW_DECODE = {
    "video/x-ffv": "avdec_ffv1",   # FFV1: the caps NAME is x-ffv (version rides `ffvversion`)
    "video/x-h265": "h265parse ! avdec_h265",
    "video/x-h264": "h264parse ! avdec_h264",
    "image/jpeg": "jpegdec",
}
# mkv track caps -> formats.encoded_info key for the STREAM-COPY path.
_ENC_KEY = {"image/jpeg": "MJPEG", "video/x-h264": "H264", "video/x-h265": "H265"}
# Header fields every session of a run must agree on: one reader shape, one consumer format.
_SHAPE_KEYS = ("pixel_format", "width", "height", "cfa_tile_mode", "bayer_pattern")


def _probe_mkv(path: str, timeout_s: int = 5):
    """Read the first part's video-track caps name from the matroska headers (PAUSED is
    enough -- no decode, no full-file scan). Returns e.g. 'video/x-ffv', or None."""
    try:
        pipeline = Gst.parse_launch(f'filesrc location="{path}" ! matroskademux name=d')
    except Exception as e:
        log.warning("replay probe of %s failed to build: %s", path, e)
        return None
    result = {}
    loop = GLib.MainLoop()

    def on_pad(_d, pad):
        caps = pad.get_current_caps()
        s = caps.get_structure(0) if caps and caps.get_size() else None
        if s and (s.get_name().startswith("video/") or s.get_name().startswith("image/")):
            result["caps"] = s.get_name()
            loop.quit()

    pipeline.get_by_name("d").connect("pad-added", on_pad)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::error", lambda *_: loop.quit())
    timeout_id = GLib.timeout_add_seconds(timeout_s, loop.quit)
    pipeline.set_state(Gst.State.PAUSED)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    # tidy the DEFAULT main context: the service's real main loop runs on it later, so a
    # leftover timeout/watch would fire (and hold this closure alive) inside the live run
    src = GLib.MainContext.default().find_source_by_id(timeout_id)
    if src is not None and not src.is_destroyed():
        GLib.source_remove(timeout_id)   # quit came from on_pad/error; the timeout is pending
    bus.remove_signal_watch()
    return result.get("caps")


def _check_glob_hazards(run: playback.RunInfo) -> None:
    """splitmuxsrc matches parts with a simple `*` glob (GPatternSpec: no escapes, no character
    classes) -- refuse the shapes it would silently mis-match rather than deliver a sibling
    run's frames under this run's stamps."""
    if any(ch in run.base for ch in "*?[]"):
        raise ValueError(f"replay: run path {run.base!r} contains glob characters "
                         f"splitmuxsrc cannot handle -- rename/move the run")
    run_dir = os.path.dirname(run.base)
    over = sorted(set(fnmatch.filter(os.listdir(run_dir), os.path.basename(run.base) + "-*.mkv"))
                  - {os.path.basename(p) for p in run.mkv_paths})
    if over:
        raise ValueError(
            f"replay: {run.mkv_glob} would also match other files ({over[0]}, ...) -- "
            f"another run's prefix extends this one; move the runs into separate directories")


class ReplaySource(GstPipelineSource):
    def __init__(self, cfg, pb_cfg=None):   # cfg = config.ReplayConfig, pb_cfg = config.PlaybackConfig
        super().__init__()
        self.cfg = cfg
        self.pb_cfg = pb_cfg
        self._sessions: List[playback.RunInfo] = []
        self._session_stamps: List[List[FrameStamp]] = []   # per session, CSV row N = frame N
        self._session_idx = 0
        self._first_idx = 0         # the first session inside the window (where a cycle starts)
        self._run: Optional[playback.RunInfo] = None   # the session being read right now
        self._csv_stamps: List[FrameStamp] = []
        self._idx = 0
        self._cycle = 0
        self._offset = 0            # retime + loop shift applied to every delivered stamp
        # Runtime playback control (docs/PLAYBACK.md). Restart is the loop wrap's own mechanism
        # (a flush-seek to 0 or a rebuild of the first reader), so the hook IS _restart, on the
        # main loop. Restart works from `finished` too: the reader is simply rebuilt.
        self._playback = playback.PlaybackState(
            "replay", speed=cfg.speed, loop=cfg.loop, on_restart=self._restart,
            initial_state=getattr(pb_cfg, "initial_state", playback.PLAYING),
            start_at_unix_s=getattr(pb_cfg, "start_at_unix_s", None),
            restartable_when_finished=True)
        self._retime_offset = 0
        self._span_ns = 0           # cycle length: last-first (across sessions) + one median interval
        self._median_ns = playback.DEFAULT_INTERVAL_NS
        self._epoch_ns: Optional[int] = None   # timeline zero, in the DATA's domain (unshifted)
        self._from_ns: Optional[int] = None    # window bounds, unshifted; None = unbounded
        self._to_ns: Optional[int] = None
        self._pacer = self._playback.pacer   # ONE pacer: set_speed reaches the feeder's wait
        self._stop_evt = threading.Event()   # cancels a pacing sleep (recorded gaps can be seconds)
        self._finished = False
        self._failed = False
        self._synth_warned = False
        self._mkv_caps = None
        self._enc = None            # (caps, parser, decoder) for a stream-copy run
        self._untile = None         # frame-bytes transform for CFA-tiled runs
        self._gen = 0               # reader generation: a stale halt/EOS must not touch a new reader
        self._epoch_cfg = None      # the CONFIGURED epoch (None = the run's own first frame)
        self._cap_ns = 0            # inter-session silence cap (0 = verbatim; always 0 under an epoch)
        self._discontinuity = False # set at a session boundary, taken by the pipeline's accounting
        self._skipped_ns = 0
        self._user_on_frame = None
        self._user_on_encoded = None

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        super().open()
        self._sessions = playback.discover_sessions(self.cfg.path, getattr(self.cfg, "run", ""))
        self._session_stamps = []
        for run in self._sessions:
            stamps = playback.load_stamps(run.csv_path)
            if not stamps:
                raise ValueError(f"replay: {run.csv_path} has no frame rows")
            _check_glob_hazards(run)
            self._session_stamps.append(stamps)
        hdr = self._sessions[0].header
        for run in self._sessions[1:]:
            drift = [k for k in _SHAPE_KEYS if run.header.get(k) != hdr.get(k)]
            if drift:
                raise ValueError(
                    f"replay: sessions differ in shape ({', '.join(drift)}: {self._sessions[0].base} vs "
                    f"{run.base}) -- one run must play as one camera; pin one with replay.run")

        # ---- the timeline: one zero for the whole run (+ the window on it) ----
        all_ts = [s.timestamp_ns for stamps in self._session_stamps for s in stamps]
        self._median_ns = playback.median_interval_ns([s.timestamp_ns for s in self._session_stamps[0]])
        ts_first, ts_last = all_ts[0], all_ts[-1]
        self._span_ns = (ts_last - ts_first) + self._median_ns
        epoch = getattr(self.pb_cfg, "epoch_unix_ns", None)
        self._epoch_cfg = epoch
        from_s = getattr(self.pb_cfg, "from_s", None)
        to_s = getattr(self.pb_cfg, "to_s", None)
        if epoch is not None:
            self._epoch_ns = int(epoch)
            # a shared zero implies the timeline starts there: what came before it is skipped
            self._from_ns = self._epoch_ns + int((from_s or 0.0) * 1e9)
        else:
            self._epoch_ns = ts_first
            self._from_ns = ts_first + int(from_s * 1e9) if from_s is not None else None
        if to_s is not None:
            self._to_ns = self._epoch_ns + int(to_s * 1e9)
        if self._from_ns is not None and self._from_ns > ts_last:
            raise ValueError(f"playback window starts {(self._from_ns - ts_last) / 1e9:.1f}s after the "
                             f"run's last frame -- nothing to play")
        if self._to_ns is not None and self._to_ns <= ts_first:
            raise ValueError("playback window ends before the run's first frame -- nothing to play")
        # the first session with anything inside the window is where every cycle starts
        self._first_idx = 0
        if self._from_ns is not None:
            for i, stamps in enumerate(self._session_stamps):
                if stamps[-1].timestamp_ns >= self._from_ns:
                    self._first_idx = i
                    break
        end_ns = min(ts_last + self._median_ns, self._to_ns) if self._to_ns is not None \
            else ts_last + self._median_ns
        # One cycle's length. On a SHARED timeline (an epoch) it is the true span; with the
        # inter-session silence capped (the bare tool) it is what will actually play, so a
        # viewer's `position / duration` stays honest across a directory of old runs.
        duration_ns = end_ns - self._epoch_ns
        self._cap_ns = int(float(getattr(self.cfg, "gap_max_s", 0) or 0) * 1e9) \
            if self._epoch_cfg is None else 0
        if self._cap_ns > 0:
            for a, b in zip(self._session_stamps, self._session_stamps[1:]):
                gap = b[0].timestamp_ns - a[-1].timestamp_ns
                if gap > self._cap_ns:
                    duration_ns -= gap - self._cap_ns
        self._skipped_ns = 0        # silence skipped so far this cycle (capped gaps)
        self._playback.duration_s = round(duration_ns / 1e9, 3)
        self._playback.epoch_unix_ns = self._epoch_ns
        self._playback.sessions = len(self._sessions)

        if (self.cfg.retime or "original") == "wall":
            self._retime_offset = time.time_ns() - self._epoch_ns   # the zero lands on NOW
        elif self.cfg.retime not in ("", "original"):
            raise ValueError(f"replay.retime: expected 'original' or 'wall', got {self.cfg.retime!r}")
        self._offset = self._retime_offset
        self._pacer.anchor_src_ns = self._epoch_ns + self._offset

        # ---- the reader shape, from the first part of each session ----
        caps = []
        for run in self._sessions:
            c = _probe_mkv(run.mkv_paths[0])
            if c is None:
                raise ValueError(f"replay: {run.mkv_paths[0]} has no readable video track")
            caps.append(c)
        if len(set(caps)) > 1:
            raise ValueError(f"replay: sessions differ in codec ({', '.join(sorted(set(caps)))}) -- "
                             f"one run must play as one camera; pin one with replay.run")
        self._mkv_caps = caps[0]
        # Stream-copy runs recorded the DELIVERED bitstream; their sidecar header carries the
        # decoded consumer format (I420). Everything else is a lossless re-encode of raw frames.
        stream_copy = self._mkv_caps in _ENC_KEY and hdr.get("pixel_format") == "I420"
        if stream_copy:
            self._enc = encoded_info(_ENC_KEY[self._mkv_caps])
        elif self._mkv_caps not in _RAW_DECODE:
            raise ValueError(f"replay: unsupported recorded codec {self._mkv_caps!r} "
                             f"in {self._sessions[0].mkv_paths[0]}")

        tile_mode = normalize_mode(hdr.get("cfa_tile_mode", "off"))
        if tile_mode != "off" and not stream_copy:
            w, h = int(hdr["width"]), int(hdr["height"])
            pattern = hdr.get("bayer_pattern") or "rggb"
            self._untile = lambda b: untile_cfa(b, w, h, mode=tile_mode, pattern=pattern)
            log.info("replay: CFA-tiled recording (%s/%s) -- un-tiling to the original mosaic",
                     tile_mode, pattern)
        self._activate_session(self._first_idx)
        log.info("replay source: %s (%d session(s), %d frames, %s -> %s, ~%.1f fps, %.1fs on the "
                 "timeline%s%s%s%s)",
                 os.path.dirname(self._sessions[0].base), len(self._sessions), len(all_ts),
                 self._mkv_caps, "stream-copy" if stream_copy else hdr.get("pixel_format"),
                 self.delivered_frame_rate or 0.0, self._playback.duration_s,
                 f", epoch {self._epoch_ns}" if epoch is not None else "",
                 f", from {from_s:g}s" if from_s is not None else "",
                 f", to {to_s:g}s" if to_s is not None else "",
                 f", speed x{self.cfg.speed:g}" if self.cfg.speed not in (0.0, 1.0) else "")
        if self.cfg.loop:
            log.info("replay: looping")

    def _activate_session(self, idx: int) -> None:
        self._session_idx = idx
        self._run = self._sessions[idx]
        self._csv_stamps = self._session_stamps[idx]
        self._idx = 0
        self._synth_warned = False
        self._playback.source_path = self._run.base
        self._playback.session = idx

    def configure(self) -> None:
        self._gen += 1
        super().configure()
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos, self._gen)
        bus.connect("message::error", self._on_error, self._gen)

    def start(self, on_frame, on_encoded=None) -> None:
        self._user_on_frame = on_frame
        self._user_on_encoded = on_encoded
        self._start_ns = time.time_ns()
        self._last_data_ns = 0
        self._started = True
        self._play()

    def _wrapped_callbacks(self):
        """The consumer callbacks with the window filter (frames outside `from`/`to` are decoded
        but never delivered) and, for a CFA-tiled run, the un-tiling in front."""
        inner_frame, inner_enc, untile = self._user_on_frame, self._user_on_encoded, self._untile

        def on_frame(st, data):
            if inner_frame is not None and self._deliverable(st):
                inner_frame(st, untile(data) if untile is not None else data)

        def on_encoded(st, data, caps=None):
            if inner_enc is not None and self._deliverable(st):
                inner_enc(st, data, caps)

        return on_frame, (on_encoded if inner_enc is not None else None)

    def _play(self) -> None:
        """Arm the CURRENT reader pipeline: connect its sinks and set it PLAYING."""
        self._on_frame, self._on_encoded = self._wrapped_callbacks()
        self._stamps.clear()   # the base's PTS-keyed correlation memo is per reader pipeline
        self._rawsink.connect("new-sample", self._on_raw)
        if self._encsink is not None:
            self._encsink.connect("new-sample", self._on_enc)
        self._pipeline.set_state(Gst.State.PLAYING)

    def _rebuild(self, idx: int) -> None:
        """Tear the current reader down and bring up session `idx`'s (main loop)."""
        self._halt_reader()
        self._activate_session(idx)
        self._discontinuity = True   # the next frame id continues from another session's counter
        self.configure()
        self._play()

    def take_discontinuity(self) -> bool:
        r, self._discontinuity = self._discontinuity, False
        return r

    def _halt_reader(self) -> None:
        if self._pipeline is None:
            return
        self._gen += 1                    # anything the old reader still posts is stale
        self._stop_evt.set()              # wake a pacing sleep so NULL isn't blocked by it
        try:
            self._pipeline.get_bus().remove_signal_watch()
        except Exception:   # noqa: BLE001
            pass
        self._pipeline.set_state(Gst.State.NULL)
        self._stop_evt.clear()

    # ---- mini-pipeline -----------------------------------------------------
    def _pipeline_desc(self) -> str:
        hdr = self._run.header
        w, h = int(hdr["width"]), int(hdr["height"])
        src = f'splitmuxsrc location="{self._run.mkv_glob}"'
        raw_sink = "appsink name=rawsink emit-signals=true max-buffers=8 drop=false sync=false"
        if self._enc:
            _caps, parser, sw_decoder = self._enc
            decoder, conv = select_decoder(sw_decoder, self._hw_decode_available(),
                                           getattr(self.cfg, "decoder", "auto"))
            return (
                f"{src} ! tee name=st "
                # decode branch is BEST-EFFORT (consumers); leaky so a slow decoder drops
                # here instead of stalling the must-not-drop stream-copy branch below
                f"st. ! queue leaky=downstream max-size-buffers=8 ! {parser} ! {decoder} ! {conv} ! "
                f"video/x-raw,format=I420,width={w},height={h} ! {raw_sink} "
                f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
            )
        gst_format = self._container_format(hdr)
        return (f"{src} ! {_RAW_DECODE[self._mkv_caps]} ! videoconvert n-threads=2 ! "
                f"video/x-raw,format={gst_format},width={w},height={h} ! {raw_sink}")

    @staticmethod
    def _container_format(hdr: dict) -> str:
        """The GStreamer raw format the recording actually rode in. Aravis-style names
        (Mono8/BayerRG8/Mono16) ride GRAY8/GRAY16_LE containers; GStreamer-native ones
        (GRAY8/GRAY16_LE/I420/...) are literal. This mirrors formats.parse_pixel_format
        without re-deriving bayer/bit metadata the header already carries."""
        pf = hdr.get("pixel_format") or "GRAY8"
        from ..formats import parse_pixel_format
        return parse_pixel_format(pf)[0]

    # ---- the window --------------------------------------------------------
    def _deliverable(self, st: FrameStamp) -> bool:
        raw = st.timestamp_ns - self._offset
        if self._from_ns is not None and raw < self._from_ns:
            return False
        if self._to_ns is not None and raw >= self._to_ns:
            return False
        return True

    # ---- re-stamping (frame N of the recording = CSV row N) ----------------
    def _new_stamp(self, buf) -> FrameStamp:
        self._last_data_ns = time.time_ns()
        idx = self._idx
        self._idx += 1
        if idx < len(self._csv_stamps):
            st = playback.shift_stamp(self._csv_stamps[idx], self._offset)
        else:
            # more frames in the .mkv than CSV rows (e.g. a crash cut the sidecar short):
            # keep delivering with synthesized stamps rather than dying mid-replay
            if not self._synth_warned:
                self._synth_warned = True
                log.warning("replay: recording has more frames than %s has rows -- "
                            "synthesizing stamps from row %d on", self._run.csv_path, idx)
            last = playback.shift_stamp(self._csv_stamps[-1], self._offset)
            ts = last.timestamp_ns + (idx - len(self._csv_stamps) + 1) * self._median_ns
            st = FrameStamp(frame_id=last.frame_id + (idx - len(self._csv_stamps) + 1),
                            timestamp_ns=ts, source=TimestampSource.SYSTEM,
                            system_ns=ts, camera_ns=ts, chunk_ns=None)
        raw = st.timestamp_ns - self._offset
        if self._from_ns is not None and raw < self._from_ns:
            return st                      # before the window: decoded, never paced or delivered
        if self._to_ns is not None and raw >= self._to_ns:
            if not self._finished:
                log.info("replay: window end reached (%d frames delivered)", idx)
                self._finish(self._gen)
            return st
        # blocks the streaming thread = natural backpressure; stop() cancels the sleep
        # (a recorded gap can be seconds long and must not stall shutdown). A pause holds HERE
        # too -- the decode pipeline simply waits on its own thread, exactly like a long gap.
        # A boot hold lets the FIRST frame through (consumers negotiate + show it), then holds.
        if self._playback.take_preroll():
            self._playback.note_frame(st.timestamp_ns,
                                      cycle_base_ns=self._epoch_ns + self._offset + self._skipped_ns)
            return st
        if self._playback.wait_if_paused(cancel=self._stop_evt) == playback.STOP:
            return st
        self._pacer.wait(st.timestamp_ns, cancel=self._stop_evt)
        # a pause that landed during the sleep holds this frame too (position stays put)
        if self._playback.wait_if_paused(cancel=self._stop_evt) == playback.STOP:
            return st
        self._playback.note_frame(st.timestamp_ns,
                                  cycle_base_ns=self._epoch_ns + self._offset + self._skipped_ns)
        return st

    def stop(self) -> None:
        self._stop_evt.set()   # wake a pacing sleep so set_state(NULL) isn't blocked by it
        self._playback.cancel_start_gate()
        super().stop()

    # ---- EOF / sessions / loop ---------------------------------------------
    def _check_row_count(self) -> None:
        """Frame N of the recording is re-stamped from CSV row N (ordinal, not keyed), which holds
        only while every frame the sidecar attests is actually delivered. Fewer frames than rows
        means a frame went missing somewhere in the decode path -- and every row after it was
        attached to the wrong frame. Say so, loudly, rather than let a silently mis-stamped
        reprocess look complete. (More frames than rows is handled in _new_stamp.)"""
        rows = len(self._csv_stamps)
        if 0 < self._idx < rows:
            log.warning("replay: %s has %d rows but only %d frames were delivered -- rows after the "
                        "first missing frame were attached to the wrong frames; the re-recorded "
                        "stamps are NOT trustworthy for this run", self._run.csv_path, rows, self._idx)

    def _finish(self, gen: int) -> None:
        """End of data (streaming thread or main loop): mark finished and, from the main loop,
        quiesce the reader so a `to`-window end doesn't keep decoding the tail for nothing."""
        self._finished = True
        self._playback.mark_finished()
        GLib.idle_add(self._quiesce, gen)

    def _quiesce(self, gen: int) -> bool:
        if gen == self._gen and self._pipeline is not None and self._finished:
            self._pipeline.set_state(Gst.State.PAUSED)   # NULL would drop the reader a restart reuses
        return False

    def _on_eos(self, _bus, _msg, gen: int) -> None:
        if gen != self._gen:
            return   # a reader already replaced (restart raced the EOS)
        self._check_row_count()
        nxt = self._session_idx + 1
        if nxt < len(self._sessions) and (self._to_ns is None or
                                          self._session_stamps[nxt][0].timestamp_ns < self._to_ns):
            gap_ns = self._session_stamps[nxt][0].timestamp_ns - self._csv_stamps[-1].timestamp_ns
            cap_ns = self._cap_ns
            capped = 0 < cap_ns < gap_ns
            log.info("replay: session %d/%d done -> session %d (%s, %.1fs after it on the timeline%s)",
                     self._session_idx + 1, len(self._sessions), nxt + 1,
                     os.path.basename(self._sessions[nxt].base), gap_ns / 1e9,
                     f"; waiting {cap_ns / 1e9:.0f}s, not the recorded gap" if capped else "")
            if capped:
                # stamps stay verbatim (provenance); only the WAIT shrinks: re-anchor the pacer so
                # the next session's first frame is due cap_ns of data-time from now
                first = self._session_stamps[nxt][0].timestamp_ns + self._offset
                self._pacer.reset(first - cap_ns)
                self._skipped_ns += gap_ns - cap_ns   # position keeps counting effective time
            self._rebuild(nxt)
            return
        if not self._playback.loop:
            log.info("replay: end of run (%d session(s))", len(self._sessions))
            self._finish(self._gen)
            return
        self._restart(reason="loop")

    def _restart(self, reason: str = "restart") -> None:
        """Back to the start of the run: the next cycle's stamps are shifted past this one's, the
        pacer re-anchors at now, and the reader either flush-seeks to 0 (one session, still
        reading) or is rebuilt on the first session. The loop wrap, an operator's `restart`, and
        a restart from `finished` are one mechanism (main-loop thread: the bus handler's, and the
        zenoh adapter dispatches there). Raises on a failed seek so a request is a legible refusal."""
        was_finished = self._finished
        self._cycle += 1
        self._offset = self._retime_offset + self._cycle * self._span_ns
        self._playback.mark_cycle(self._cycle)
        self._skipped_ns = 0
        self._playback.take_restart()   # the feeder needs no flag: the seek/rebuild does the work
        self._pacer.reset(self._epoch_ns + self._offset)
        self._finished = False
        self._failed = False
        log.info("replay: %s -> cycle %d (timestamps shifted %+.3fs)",
                 reason, self._cycle, self._cycle * self._span_ns / 1e9)
        single = len(self._sessions) == 1 or self._session_idx == self._first_idx
        if single and not was_finished and self._pipeline is not None:
            self._idx = 0
            self._synth_warned = False
            if self._pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0):
                return
            log.error("replay: %s seek failed (parts moved/deleted?) -- rebuilding the reader", reason)
        try:
            self._rebuild(self._first_idx)
        except Exception as e:   # noqa: BLE001 -- surfaced as a refusal / the end of playback
            log.error("replay: %s could not rebuild the reader: %s -- ending playback", reason, e)
            if reason != "loop":
                raise RuntimeError(f"reader rebuild failed: {e}") from e
            self._failed = True
            self._finished = True

    def _on_error(self, _bus, msg, gen: int) -> None:
        if gen != self._gen:
            return
        err, dbg = msg.parse_error()
        log.error("replay pipeline error: %s | %s -- ending playback", err, dbg)
        self._failed = True     # surfaces as a non-zero exit; recording finalizes regardless
        self._finished = True

    @property
    def playback(self):
        return self._playback

    def provenance(self) -> dict:
        return {"replay_of": [r.base for r in self._sessions], "replay_epoch_unix_ns": self._epoch_ns}

    @property
    def finite(self) -> bool:
        # Always: loop mode never EOFs, but a mini-pipeline ERROR must still end the run
        # (finished is set there too) instead of idling forever.
        return True

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def finished_error(self) -> bool:
        return self._failed

    @property
    def delivered_frame_rate(self):
        if self.cfg.frame_rate:
            return float(self.cfg.frame_rate)
        return 1e9 / self._median_ns if self._median_ns else None

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        hdr = self._sessions[0].header
        return (0, 0, int(hdr["width"]), int(hdr["height"]))

    def pixel_format(self) -> str:
        return "I420" if self._enc else (self._sessions[0].header.get("pixel_format") or "GRAY8")

    @property
    def encoded_caps(self):
        return self._enc[0] if self._enc else None

    @property
    def encoded_parser(self):
        return self._enc[1] if self._enc else None

    @property
    def tick_frequency_hz(self) -> int:
        return int(self._sessions[0].header.get("tick_frequency_hz") or 0)

    @property
    def ptp_locked(self) -> bool:
        return bool(self._sessions[0].header.get("ptp_synced"))

    @property
    def active_timestamp_source(self) -> str:
        return self._sessions[0].header.get("timestamp_source") or TimestampSource.SYSTEM.value
