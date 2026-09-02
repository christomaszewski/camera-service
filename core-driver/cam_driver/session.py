"""One recording SESSION: its own GStreamer pipeline (appsrc -> recorder fragment -> splitmuxsink),
its own sidecar, its own accounting -- opened by `activate`, finalized by `deactivate`, any number
of times per process.

Why a separate pipeline rather than a branch added to / removed from the capture tee: the main
pipeline (camsrc -> tee -> raw endpoint / preview, plus the transport appsrcs) is never touched by a
transition, so consumers never see a hiccup, and finalizing is exactly the process-shutdown sequence
scoped to one object -- stop feeding, EOS the appsrc, wait for THIS pipeline's EOS, NULL. A tee
branch can't do that: EOS on one branch is swallowed by the bin's EOS aggregation (the socket sinks
never EOS), so "did the muxer finalize?" would need probes inside splitmuxsink plus a blocking probe,
request-pad release and a bin removal in PLAYING -- machinery this repo has never needed.

Timing: the appsrc is `is-live=true` (PLAYING is NO_PREROLL, so start() never waits for a buffer) and
nothing downstream syncs to the clock -- splitmuxsink's filesink is sync=false, encoders / parsers /
the muxer never sync. Buffers carry the PROCESS PTS unchanged (a session opened an hour in starts at
PTS ~3600 s, which is what segment 00001 of every run already looks like); the sidecar header records
first_pts_ns and keeps the process base so the CSV<->mkv join stays exact. RULE: any sink ever added
to a session pipeline must be sync=false.

Threading: the feeders run on the SOURCE threads and read the pipeline's current session once per
frame; push() and begin_close() share one lock so a frame racing the EOS is skipped (not a drop) and
never lands after end-of-stream. The bus watch runs on the GLib main loop; a session ERROR is
reported through on_error on the NEXT loop iteration (never from inside the bus dispatch, so the
close path may remove the watch), ends the session, and leaves the process running -- a recorder
that dies must not take the transport with it.
"""
from __future__ import annotations

import glob
import logging
import os
import threading
import time
from enum import Enum
from typing import Callable, Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from .dropstats import DropStats
from .keyframe import is_sync_point, parse_caps_kind
from .sidecar import SidecarWriter

log = logging.getLogger(__name__)

# How long finish_close() waits for the session pipeline to drain to EOS (encoder flush + mux
# finalize). The same 5 s the process-level EOS drain (pipeline._force_quit) allows; both sit inside
# the supervisor's CORE_STOP_GRACE_S, which sits inside compose's stop_grace_period -- keep in step.
SESSION_DRAIN_S = 5.0


class PushResult(Enum):
    OK = "ok"              # accepted by the appsrc; a sidecar row was written
    DROPPED = "dropped"    # the recording feed could not take it (queue full / push != OK): a real loss
    SKIPPED = "skipped"    # not a loss: the session is closing, or waiting for a sync point


def _wrap_buffer(payload: bytes, pts: int, frame_id: int):
    buf = Gst.Buffer.new_wrapped(payload)
    buf.pts = pts
    buf.dts = Gst.CLOCK_TIME_NONE
    buf.offset = frame_id
    return buf


def _caps_from_string(caps_str: str):
    return Gst.Caps.from_string(caps_str)


class RecordingSession:
    def __init__(self, index: int, prefix: str, output_dir: str, description: str, *,
                 header_factory: Callable, encoded: bool = False,
                 on_error: Optional[Callable] = None, parse_launch=None,
                 sidecar_factory=SidecarWriter):
        self.index = index
        self.prefix = prefix
        self.output_dir = output_dir
        self.description = description
        self.path_base = os.path.join(output_dir, prefix)
        self.sidecar = sidecar_factory(self.path_base)
        self._header_factory = header_factory     # (stamp, pts, session) -> SidecarHeader
        self._on_error = on_error                 # (session) -> None, on the main loop
        self._parse_launch = parse_launch or Gst.parse_launch
        self.lock = threading.Lock()
        self.closed = False
        self._await_key = bool(encoded)           # stream-copy: wait for a sync point before muxing
        self._caps_applied = False
        self._caps_kind = None                    # parsed once from the first caps string seen
        self._eos_seen = False
        self.frames = 0                           # pushed to the muxer (== sidecar rows)
        self.skipped_awaiting_keyframe = 0
        self.first_pts: Optional[int] = None
        self.first_stamp = None
        self.error: Optional[str] = None
        self.truncated = False
        self.segments: list = []
        self.drops_at_start: Optional[dict] = None
        self.started_unix_s: Optional[float] = None
        self._pipeline = None
        self._appsrc = None
        self._bus = None

    # ---- lifecycle ---------------------------------------------------------
    def start(self, drops_now: dict) -> None:
        """Open the sidecar, build + PLAY the session pipeline. Raises on a build/state failure with
        everything already torn down (no half-open session)."""
        self.drops_at_start = dict(drops_now)
        self.sidecar.start()      # mkdir + writer thread BEFORE PLAYING: splitmuxsink creates no dirs
        try:
            self._pipeline = self._parse_launch(self.description)
            self._appsrc = self._pipeline.get_by_name("recsrc")
            if self._appsrc is None:
                raise RuntimeError("appsrc 'recsrc' not found in the session pipeline")
            self._bus = self._pipeline.get_bus()
            self._bus.add_signal_watch()
            self._bus.connect("message", self._on_bus)
            if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("session pipeline refused to go PLAYING")
        except Exception:
            self._teardown_pipeline()
            self.sidecar.stop()
            raise
        self.started_unix_s = time.time()
        log.info("recording session %d open -> %s-*.mkv", self.index, self.path_base)

    def push(self, payload: bytes, pts: int, stamp, caps_str: Optional[str] = None) -> PushResult:
        """Feed one frame (source thread). The whole check-and-push runs under the session lock so a
        frame can never land after begin_close() emitted end-of-stream."""
        with self.lock:
            if self.closed or self._appsrc is None:
                return PushResult.SKIPPED
            if self._await_key:
                if self._caps_kind is None and caps_str:
                    self._caps_kind = parse_caps_kind(caps_str)
                media, sf, nal_len = self._caps_kind or (None, None, 4)
                if not is_sync_point(media, payload, sf, nal_len):
                    self.skipped_awaiting_keyframe += 1
                    return PushResult.SKIPPED
                self._await_key = False
            if caps_str and not self._caps_applied:
                # The NEGOTIATED encoded caps (stream-format + codec_data: the H.264/H.265 parameter
                # sets that hvc1/avc keep in caps, not in the bytes) so appsrc -> parser -> muxer
                # negotiates. The build-time caps were the bare media type.
                self._appsrc.set_property("caps", _caps_from_string(caps_str))
                self._caps_applied = True
            max_bytes = self._appsrc.get_property("max-bytes")
            if max_bytes and self._appsrc.get_property("current-level-bytes") + len(payload) > max_bytes:
                return PushResult.DROPPED   # appsrc block=false never enforces its own bound
            if self.first_pts is None:
                self.first_pts = int(pts)
                self.first_stamp = stamp
                self.sidecar.write_header(self._header_factory(stamp, pts, self))
            if self._appsrc.emit("push-buffer", _wrap_buffer(payload, pts, stamp.frame_id)) != Gst.FlowReturn.OK:
                return PushResult.DROPPED
            self.frames += 1
            self.sidecar.add(stamp, pts)
            return PushResult.OK

    def begin_close(self) -> None:
        """Stop accepting frames and send EOS down the session pipeline (non-blocking)."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self._appsrc is not None:
                self._appsrc.emit("end-of-stream")

    def finish_close(self, timeout_s: float, drops_now: dict) -> dict:
        """Wait (bounded) for the EOS to drain so splitmuxsink finalizes the open segment, set NULL --
        on EVERY path, because NULL is what releases the encoder (an NVENC session is a budgeted
        resource) -- then close the sidecar and attest the session in its JSON. Main-loop thread."""
        if self._bus is not None:
            self._bus.remove_signal_watch()   # the watch must not race timed_pop for the EOS message
        if self._pipeline is not None:
            if self.error is not None:
                self.truncated = True
            elif not self._eos_seen and timeout_s > 0:
                msg = self._bus.timed_pop_filtered(int(timeout_s * Gst.SECOND),
                                                   Gst.MessageType.EOS | Gst.MessageType.ERROR)
                if msg is None:
                    self.truncated = True
                    log.warning("recording session %d: EOS did not drain within %.0fs; the last "
                                "segment may be truncated", self.index, timeout_s)
                elif msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    self.error = f"{err} | {dbg}"
                    self.truncated = True
            elif not self._eos_seen:
                self.truncated = True
        self._teardown_pipeline()
        if not self.segments:
            self.segments = sorted(glob.glob(self.path_base + "-*.mkv"))
        drops = DropStats.delta(drops_now, self.drops_at_start or {})
        attest = {
            "frames_recorded": self.frames,
            "skipped_awaiting_keyframe": self.skipped_awaiting_keyframe,
            "segments": len(self.segments),
            "truncated": self.truncated,
            "error": self.error,
            "first_pts_ns": self.first_pts,
            "session_index": self.index,
        }
        # stop() BEFORE write_summary: stop() joins the CSV writer, where the final flush happens, so a
        # failure there sets the writer's failed flag in time for the summary to attest it.
        self.sidecar.stop()
        self.sidecar.write_summary(drops, {"session": attest})
        return self.describe(final=True)

    def _teardown_pipeline(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.set_state(Gst.State.NULL)
            except Exception as e:   # noqa: BLE001 -- teardown must finish regardless
                log.warning("recording session %d: NULL transition failed: %s", self.index, e)
        self._pipeline = None
        self._appsrc = None
        self._bus = None

    # ---- bus ---------------------------------------------------------------
    def _on_bus(self, _bus, msg) -> None:
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if self.error is None:
                self.error = f"{err} | {dbg}"
                log.error("recording session %d ERROR: %s | %s", self.index, err, dbg)
                if self._on_error is not None:
                    GLib.idle_add(self._report_error)   # outside this dispatch: the close removes the watch
        elif t == Gst.MessageType.EOS:
            self._eos_seen = True
        elif t == Gst.MessageType.ELEMENT:
            s = msg.get_structure()
            if s is not None and s.get_name() == "splitmuxsink-fragment-closed":
                loc = s.get_string("location")
                if loc:
                    self.segments.append(loc)

    def _report_error(self) -> bool:
        try:
            self._on_error(self)
        except Exception as e:   # noqa: BLE001
            log.error("recording session %d: error handler failed: %s", self.index, e)
        return False   # one-shot

    # ---- introspection -----------------------------------------------------
    def describe(self, final: bool = False) -> dict:
        """A fresh dict of scalars (safe to hand to another thread / serialize)."""
        d = {
            "index": self.index,
            "prefix": self.prefix,
            "output_dir": self.output_dir,
            "started_unix_s": self.started_unix_s,
            "frames": self.frames,
            "segments": len(self.segments),
            "skipped_awaiting_keyframe": self.skipped_awaiting_keyframe,
            "error": self.error,
        }
        if final:
            d.update({
                "truncated": self.truncated,
                "csv": self.sidecar.csv_path,
                "json": self.sidecar.json_path,
                "files": list(self.segments),
            })
        return d
